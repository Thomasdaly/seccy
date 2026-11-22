"""CLI exit codes. The live run is replaced by a stub; nothing is dispatched."""

import io
import logging
import os
from contextlib import redirect_stderr, redirect_stdout
from unittest import TestCase
from unittest.mock import patch

import httpx

import harness
from harness import EXIT_DONE, EXIT_FAILED, EXIT_KILLED, exit_code
from harness.guardrails import KillEngaged, ScopeViolation
from harness.scoring import Report


class ExitCodeTests(TestCase):
    def test_status_mapping(self):
        self.assertEqual(exit_code("done"), 0)
        self.assertEqual(exit_code("killed"), 130)
        for status in ("failed", "running", "", "DONE"):
            with self.subTest(status=status):
                self.assertEqual(exit_code(status), 1)
        self.assertEqual((EXIT_DONE, EXIT_FAILED, EXIT_KILLED), (0, 1, 130))


class MainTests(TestCase):
    def setUp(self):
        # Even if the stub below were bypassed, this environment cannot dispatch:
        # no shared policy database, an empty scope, and the stop flag engaged.
        env = patch.dict(os.environ, {"HARNESS_SCOPE_ALLOWLIST": "", "HARNESS_KILL": "1"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("HARNESS_DATABASE_URL", None)
        argv = patch("sys.argv", ["harness", "--target", "vampi"])
        argv.start()
        self.addCleanup(argv.stop)

    def _main(self, *, returns=None, raises=None):
        with patch("harness.loop.run", return_value=returns, side_effect=raises) as run, \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            try:
                harness.main()
            finally:
                run.assert_called_once()

    def _exit(self, **kwargs):
        with self.assertRaises(SystemExit) as raised:
            self._main(**kwargs)
        return raised.exception.code

    def _report(self, status: str) -> Report:
        return Report(target="vampi", authz_total=0, recall=None, status=status)

    def test_done_exits_zero_without_confirmed_findings(self):
        self.assertEqual(self._exit(returns=self._report("done")), 0)

    def test_killed_report_exits_130(self):
        self.assertEqual(self._exit(returns=self._report("killed")), 130)

    def test_kill_raised_before_the_run_exits_130(self):
        self.assertEqual(self._exit(raises=KillEngaged("engaged (HARNESS_KILL=1)")), 130)

    def test_failed_report_exits_non_zero(self):
        for status in ("failed", "running"):
            with self.subTest(status=status):
                self.assertEqual(self._exit(returns=self._report(status)), 1)

    def test_scope_violation_exits_non_zero_with_message(self):
        code = self._exit(raises=ScopeViolation("target authority is outside scope"))
        # sys.exit with a message prints it to stderr and exits with status 1.
        self.assertIsInstance(code, str)
        self.assertEqual(code, "scope violation: target authority is outside scope")

    def test_run_errors_propagate(self):
        with self.assertRaisesRegex(RuntimeError, "shared dispatch database unavailable"):
            self._main(raises=RuntimeError("shared dispatch database unavailable"))

    def test_target_http_diagnostics_stay_private_with_root_debug_logging(self):
        self._assert_http_diagnostics_stay_private(logging.NOTSET)

    def test_target_http_diagnostics_stay_private_with_explicit_transport_debug_logging(self):
        self._assert_http_diagnostics_stay_private(logging.DEBUG)

    def _assert_http_diagnostics_stay_private(self, transport_level):
        root = logging.getLogger()
        configured_levels = {"httpx": logging.NOTSET, "httpcore": logging.NOTSET,
                             "httpcore.http11": transport_level}
        for name, level in configured_levels.items():
            logger = logging.getLogger(name)
            self.addCleanup(logger.setLevel, logger.level)
            logger.setLevel(level)
        self.addCleanup(root.setLevel, root.level)
        root.setLevel(logging.DEBUG)
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        self.addCleanup(root.removeHandler, handler)
        root.addHandler(handler)

        def respond(request):
            logging.getLogger("httpcore.http11").debug("canary header trace")
            return httpx.Response(200, extensions={"reason_phrase": b"canary reason"})

        def run(*args):
            with httpx.Client(transport=httpx.MockTransport(respond)) as client:
                client.get("http://example.invalid/canary-title")
            logging.getLogger("harness.test_cli").info("unrelated diagnostic")
            return self._report("done")

        self.assertEqual(self._exit(raises=run), 0)
        self.assertNotIn("canary", output.getvalue())
        self.assertIn("unrelated diagnostic", output.getvalue())
        for name, level in configured_levels.items():
            self.assertEqual(logging.getLogger(name).level, level, name)
