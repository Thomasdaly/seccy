"""Run bookkeeping with a synthetic target. No probe touches a transport or database."""

import io
import json
from contextlib import nullcontext, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import httpx

from harness.config import REPO
from harness.executor import Exchange
from harness.guardrails import Guardrails, KillSwitch, RateLimiter, ScopeAllowlist
from harness.loop import _run
from harness.scoring import load_ground_truth
from harness.targets import Identity, Probe, ProbeResult

VAMPI = load_ground_truth(REPO / "lab" / "ground-truth" / "vampi.yaml")
BASE = "http://example.invalid"
OPERATIONS = {
    "vampi-bola-books": ("GET /books/v1/{book_title}", "GET", "/books/v1/canary-title"),
    "vampi-massassign-admin": ("POST /users/v1/register", "GET", "/me"),
    "vampi-bfla-password": ("PUT /users/v1/{username}/password", "PUT", "/users/v1/canary-victim/password"),
    "vampi-excessive-debug": ("GET /users/v1/_debug", "GET", "/users/v1/_debug"),
}


def _result(fid: str, signal, control_signal=None) -> ProbeResult:
    _, method, path = OPERATIONS[fid]
    attacker = Exchange(method=method, url=BASE + path, status_code=200,
                        response_body={"secret": "canary-secret", "owner": "canary-owner"})
    control = None
    if control_signal is not None:
        control = Exchange(method=method, url="http://example.invalid:8080" + path, status_code=404,
                           response_body={"message": "canary-not-found"})
    return ProbeResult(attacker=attacker, observation="canary observation", signal=signal,
                       control=control, control_signal=control_signal)


class FakeTarget:
    base = BASE
    control = None

    def __init__(self, behaviours):
        self.behaviours = behaviours

    def identities(self, executor):
        return {"name1": Identity("name1", "user", "synthetic-token")}

    def build_probes(self, executor, identities, operations):
        return [Probe(id=f"p-{fid}", finding_id=fid, category="synthetic", operation=OPERATIONS[fid][0],
                      description="synthetic", attacker="name1", owner=None, run=run)
                for fid, run in self.behaviours]


class RunReportTests(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.artifacts = Path(directory.name)
        self.cfg = SimpleNamespace(artifacts_dir=str(self.artifacts), database_url=None)
        self.stopped = False
        self.kill = Mock(spec=KillSwitch)
        self.kill.engaged.side_effect = lambda: self.stopped
        self.kill.detail.return_value = "engaged (stop file present or inaccessible)"
        shared = Mock(**{"stopped.return_value": False, "reserve.return_value": True})
        self.guard = Guardrails(ScopeAllowlist(["example.invalid:80"]), RateLimiter(1000), self.kill, shared=shared)
        self.db = Mock()
        self.db.transaction.side_effect = lambda: nullcontext()
        self.db.add_operation.return_value = 1
        self.db.add_probe.return_value = 2
        transport = httpx.MockTransport(Mock(side_effect=AssertionError("target transport was reached")))
        for target, value in (("harness.executor.httpx.Client", httpx.Client(transport=transport)),
                              ("harness.loop.FindingsDB", self.db)):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _run(self, behaviours):
        with redirect_stdout(io.StringIO()) as out:
            report = _run(self.cfg, FakeTarget(behaviours), "lab", self.guard, VAMPI, [])
        (run_dir,) = self.artifacts.iterdir()
        files = {path.stem: json.loads(path.read_text()) for path in run_dir.glob("*.json")}
        return report, files, out.getvalue()

    def test_completed_run_scores_signals_but_confirms_nothing(self):
        def invalid(executor):
            raise ValueError("synthetic")

        report, files, out = self._run([
            ("vampi-bola-books", lambda e: _result("vampi-bola-books", True, control_signal=False)),
            ("vampi-massassign-admin", lambda e: _result("vampi-massassign-admin", True, control_signal=True)),
            ("vampi-bfla-password", invalid),
            ("vampi-excessive-debug", lambda e: _result("vampi-excessive-debug", True)),
        ])
        self.assertEqual(report.status, "done")
        self.assertIsNone(report.recall)
        self.assertEqual(report.confirmed_authz, [])

        summary = files["_report"]
        self.assertEqual(summary["evaluation"], "manual_review_required")
        signal_eval = summary["signal_eval"]
        self.assertEqual(signal_eval["basis"], "deterministic_probe_signal")
        self.assertEqual(signal_eval["signalled_authz"], ["vampi-bola-books", "vampi-excessive-debug"])
        self.assertEqual(signal_eval["missed_authz"], ["vampi-bfla-password", "vampi-massassign-admin"])
        self.assertEqual(signal_eval["false_positives"], ["vampi-massassign-admin (fired on secure twin)"])
        self.assertEqual(signal_eval["recall"], 0.5)
        self.assertIn("not findings", out)

        bola = files["p-vampi-bola-books"]
        self.assertEqual((bola["signal"], bola["control_signal"]), (True, False))
        self.assertEqual(bola["finding_id"], "vampi-bola-books")
        self.assertEqual(bola["attacker"]["request"]["route"], "/books/v1/{book_title}")
        self.assertEqual((files["p-vampi-excessive-debug"]["control_signal"]), None)
        invalid_probe = files["p-vampi-bfla-password"]
        self.assertEqual((invalid_probe["signal"], invalid_probe["error"]), (False, "probe_response_invalid"))
        for name, payload in files.items():
            with self.subTest(file=name):
                self.assertNotIn("canary", json.dumps(payload))

        labels = {call.args[1] for call in self.db.add_verdict.call_args_list}
        self.assertEqual(labels, {"needs_review"})
        self.assertEqual([call.args[3] for call in self.db.add_probe.call_args_list], [True, True, None, True])
        self.db.finish_run.assert_called_once()
        self.assertEqual(self.db.finish_run.call_args.args[1], "done")

    def test_stopped_run_has_no_signal_eval(self):
        def stop_after(executor):
            self.stopped = True
            return _result("vampi-bola-books", True)

        report, files, out = self._run([
            ("vampi-bola-books", stop_after),
            ("vampi-excessive-debug", lambda e: _result("vampi-excessive-debug", True)),
        ])
        self.assertEqual(report.status, "killed")
        self.assertEqual(files["_report"]["status"], "killed")
        self.assertIsNone(files["_report"]["signal_eval"])
        self.assertNotIn("signal eval", out)
        self.assertEqual(self.db.finish_run.call_args.args[1], "killed")
