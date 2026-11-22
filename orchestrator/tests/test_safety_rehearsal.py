"""Rehearse guardrails and evidence handling using only synthetic responses."""

import io
import json
import logging
import os
from contextlib import closing, nullcontext, redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import httpx

import harness
from harness.executor import Executor
from harness.guardrails import Guardrails, KillEngaged, KillSwitch, RateLimiter, ScopeAllowlist, ScopeViolation
from harness.targets import Probe, ProbeResult

ORIGIN = "http://example.invalid"
ROUTE = "/books/v1/{book_title}"


class SafetyRehearsalTests(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.stop_file = Path(directory.name) / "stop"
        self.artifacts = Path(directory.name) / "artifacts"
        self.enterContext(patch.dict(os.environ, {
            "HARNESS_KILL_SWITCH": str(self.stop_file),
            "HARNESS_SCOPE_ALLOWLIST": "example.invalid:80",
            "HARNESS_ARTIFACTS_DIR": str(self.artifacts),
            "HARNESS_RATE_LIMIT_RPS": "1000",
            "HARNESS_DATABASE_URL": "synthetic-database",
        }, clear=True))
        socket = self.enterContext(patch("socket.socket", side_effect=AssertionError("socket opened")))
        connect = self.enterContext(patch("psycopg.connect", side_effect=AssertionError("database opened")))
        self.addCleanup(socket.assert_not_called)
        self.addCleanup(connect.assert_not_called)
        self.shared = Mock(**{"stopped.return_value": False, "reserve.return_value": True})
        self.guardrails = Guardrails(ScopeAllowlist(["example.invalid:80"]), RateLimiter(1000),
                                    KillSwitch(self.stop_file), shared=self.shared)
        self.transport = Mock(side_effect=AssertionError("mock transport was reached unexpectedly"))
        client = httpx.Client(transport=httpx.MockTransport(self.transport))
        self.enterContext(patch("harness.executor.httpx.Client", return_value=client))
        self.enterContext(patch("harness.executor.ResponseDecoder",
                                return_value=SimpleNamespace(decode=lambda body, timeout=None: json.loads(body))))
        self.addCleanup(client.close)

    def test_local_and_shared_stop_refuse_dispatch_before_transport(self):
        with closing(Executor(self.guardrails)) as executor:
            self.stop_file.touch()
            with self.assertRaisesRegex(KillEngaged, "stop file"):
                executor.send("GET", ORIGIN + "/me")
            self.stop_file.unlink()
            self.shared.stopped.return_value = True
            with self.assertRaisesRegex(KillEngaged, "shared dispatch policy"):
                executor.send("GET", ORIGIN + "/me")
            self.assertEqual(executor.request_count, 0)

        self.transport.assert_not_called()
        self.assertEqual([(event.kind, event.host, event.detail) for event in self.guardrails.events], [
            ("kill_switch", None, "engaged (stop file present or inaccessible)"),
            ("kill_switch", None, "engaged (shared dispatch policy)"),
        ])

    def test_bad_authority_path_and_method_are_refused_before_transport(self):
        with closing(Executor(self.guardrails)) as executor:
            for method, url, reason in (
                ("GET", "http://outside.invalid/me", "target authority is outside scope"),
                ("GET", ORIGIN + "/outside", "target method or path is outside scope"),
                ("DELETE", ORIGIN + "/me", "target method or path is outside scope"),
            ):
                with self.subTest(method=method, url=url), self.assertRaisesRegex(ScopeViolation, f"^{reason}$"):
                    executor.send(method, url)
            self.assertEqual(executor.request_count, 0)

        self.transport.assert_not_called()
        self.assertEqual([(event.kind, event.host, event.detail) for event in self.guardrails.events], [
            ("scope_denied", None, "target authority denied"),
            ("scope_denied", None, "target method or path denied"),
            ("scope_denied", None, "target method or path denied"),
        ])

    def _probe(self, receive, *, probe_id="synthetic"):
        return Probe(id=probe_id, finding_id="synthetic", category="synthetic",
                     operation=f"GET {ROUTE}", description="exercise evidence handling",
                     attacker="synthetic-user", owner=None, run=receive)

    def _run_cli(self, probes, *, diagnostic=False):
        target = SimpleNamespace(base=ORIGIN, control=None, identities=lambda executor: {},
                                 build_probes=lambda *args: probes)
        ground_truth = {"target": {"name": "synthetic"}, "findings": [{"id": "synthetic", "scope": "authz"}]}
        db = Mock()
        db.transaction.side_effect = nullcontext
        db.add_operation.return_value = 1
        db.add_probe.return_value = 2
        for name in ("httpx", "httpcore.http11"):
            logger = logging.getLogger(name)
            self.addCleanup(logger.setLevel, logger.level)
            logger.setLevel(logging.DEBUG if diagnostic else logging.NOTSET)

        stdout, stderr = io.StringIO(), io.StringIO()
        with (patch("sys.argv", ["harness", "--program", "synthetic"]),
              patch("harness._vampi", return_value=target),
              patch("harness.loop.load_spec", return_value={}),
              patch("harness.loop.load_ground_truth", return_value=ground_truth),
              patch("harness.loop.SharedDispatch.connect", return_value=self.shared),
              patch("harness.loop.FindingsDB", return_value=db),
              redirect_stdout(stdout), redirect_stderr(stderr),
              self.assertLogs(level=logging.DEBUG if diagnostic else logging.WARNING) as logs,
              self.assertRaises(SystemExit) as raised):
            harness.main()

        self.shared.lock_runner.assert_called_once()
        (run_dir,) = self.artifacts.iterdir()
        return raised.exception.code, run_dir, db, stdout.getvalue(), stderr.getvalue(), "\n".join(logs.output)

    def test_synthetic_response_is_redacted_and_kept_for_manual_review(self):
        self._assert_private_rehearsal(diagnostic=False)

    def test_diagnostic_logging_keeps_the_synthetic_response_private(self):
        self._assert_private_rehearsal(diagnostic=True)

    def _assert_private_rehearsal(self, *, diagnostic):
        body = {"owner": "canary-body", "status": True}
        raw_body = json.dumps(body).encode()

        def respond(request):
            if request.url.path == "/me":
                raise httpx.ReadError("canary-error", request=request)
            self.assertEqual(request.headers["Authorization"], "Bearer canary-authorization")
            response = httpx.Response(
                200, stream=httpx.ByteStream(raw_body),
                headers={"Content-Type": "application/json; marker=canary-response-header"},
            )
            # MockTransport omits httpcore's real receive-header trace.
            logging.getLogger("httpcore.http11").debug("receive headers %r", response.headers.raw)
            return response

        self.transport.side_effect = respond

        def receive(executor):
            logging.getLogger("harness.rehearsal").warning("synthetic run diagnostic")
            exchange = executor.send("GET", ORIGIN + "/books/v1/canary-identifier",
                                     headers={"Authorization": "Bearer canary-authorization"})
            self.assertEqual(exchange.response_body, body)
            control = executor.send("GET", ORIGIN + "/me")
            return ProbeResult(attacker=exchange, signal=True, observation="canary-observation",
                               details={"value": "canary-details"}, control=control)

        code, run_dir, db, stdout, stderr, logs = self._run_cli([self._probe(receive)], diagnostic=diagnostic)
        self.assertEqual(code, harness.EXIT_DONE)
        self.assertEqual(self.transport.call_count, 2)
        self.assertIn("needs review", stdout)
        self.assertEqual(stderr, "")
        self.assertIn("synthetic run diagnostic", logs)

        evidence_path = run_dir / "synthetic.json"
        evidence = json.loads(evidence_path.read_text())
        report = json.loads((run_dir / "_report.json").read_text())
        self.assertEqual((report["status"], report["requests"], report["evaluation"]),
                         ("done", 2, "manual_review_required"))
        self.assertEqual(report["signal_eval"]["signalled_authz"], ["synthetic"])
        self.assertEqual(evidence["operation"], f"GET {ROUTE}")
        self.assertIs(evidence["signal"], True)
        self.assertEqual(evidence["attacker"]["request"], {
            "method": "GET", "origin": ORIGIN, "route": ROUTE, "body": None,
        })
        self.assertEqual(evidence["attacker"]["response"], {
            "status": 200, "content_kind": "json", "bytes": len(raw_body),
            "body": {"owner": "[redacted]", "status": True},
        })
        self.assertEqual(evidence["control"]["error_kind"], "transport_error")
        db.add_operation.assert_called_once_with(report["run_id"], "GET", ROUTE)
        db.add_probe.assert_called_once_with(1, "synthetic-user", "synthetic", True)
        db.add_exchange.assert_called_once_with(2, 200, str(evidence_path), str(evidence_path))
        db.queue_review.assert_called_once_with(2, str(evidence_path))
        for name, output in {"stdout": stdout, "stderr": stderr, "logs": logs, "database": repr(db.mock_calls),
                             **{path.name: path.read_text() for path in run_dir.iterdir()}}.items():
            with self.subTest(output=name):
                self.assertNotIn("canary-", output)

    def test_stopped_run_has_no_completed_signal_summary(self):
        self.transport.side_effect = lambda _: httpx.Response(204, stream=httpx.ByteStream(b""))

        def receive(executor):
            exchange = executor.send("GET", ORIGIN + "/books/v1/synthetic")
            return ProbeResult(attacker=exchange, signal=True, observation="synthetic")

        def stop(executor):
            logging.getLogger("harness.rehearsal").warning("synthetic stop diagnostic")
            self.stop_file.touch()
            return receive(executor)

        code, run_dir, db, stdout, _, _ = self._run_cli([
            self._probe(receive), self._probe(stop, probe_id="stopped"),
        ])
        self.assertEqual(code, harness.EXIT_KILLED)
        self.transport.assert_called_once()
        report = json.loads((run_dir / "_report.json").read_text())
        self.assertEqual((report["status"], report["requests"], report["signal_eval"]), ("killed", 1, None))
        stop_events = [event for event in report["guardrail_events"] if event["kind"] == "kill_switch"]
        self.assertEqual(stop_events, [
            {"kind": "kill_switch", "host": None, "detail": "engaged (stop file present or inaccessible)"},
        ])
        self.assertNotIn("signal eval", stdout)
        self.assertEqual({path.name for path in run_dir.iterdir()}, {"synthetic.json", "_report.json"})
        db.queue_review.assert_called_once_with(2, str(run_dir / "synthetic.json"))
        db.add_guardrail_event.assert_any_call(
            report["run_id"], "kill_switch", None, "engaged (stop file present or inaccessible)",
        )
        db.finish_run.assert_called_once_with(report["run_id"], "killed")
