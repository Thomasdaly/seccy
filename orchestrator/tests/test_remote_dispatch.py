"""scripts/remote.sh dispatch, against fake docker and ssh binaries. Nothing leaves this machine."""

import os
import pty
import select
import subprocess
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from harness.config import REPO

SCRIPT = REPO / "scripts" / "remote.sh"
FAKE_DOCKER = """#!/bin/sh
printf '%s\\n' "$*" >> "$DOCKER_LOG"
case "$*" in
  *"set stopped = true"*) exit "${STOP_EXIT_CODE:-0}" ;;
  *"select case"*)
    [ "${STATUS_EXIT_CODE:-0}" = 0 ] || exit "$STATUS_EXIT_CODE"
    [ -z "$POLICY_STATE" ] || printf '%s\\n' "$POLICY_STATE" ;;
esac
"""
FAKE_SSH = """#!/bin/sh
echo "ssh must not be used by dispatch" >&2
exit 99
"""


class RemoteDispatchTests(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.bin = Path(directory.name)
        self.log = self.bin / "docker.log"
        for name, body in (("docker", FAKE_DOCKER), ("ssh", FAKE_SSH)):
            path = self.bin / name
            path.write_text(body)
            path.chmod(0o755)
        # The real docker (/usr/local/bin) and any docker context are off this PATH.
        self.env = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": directory.name,
                    "DOCKER_LOG": str(self.log), "POLICY_STATE": "stopped min_interval_ms=250"}

    def _dispatch(self, *args, stdin=subprocess.DEVNULL, **env):
        return subprocess.run(["/bin/bash", str(SCRIPT), "dispatch", *args], cwd=REPO,
                              env={**self.env, **env}, stdin=stdin, capture_output=True,
                              text=True, timeout=30)

    def _calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_status_reads_the_policy_as_postgres(self):
        result = self._dispatch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "stopped min_interval_ms=250")
        (call,) = self._calls()
        self.assertTrue(call.startswith("--context 2060 compose -p harness exec -T postgres psql -U postgres"))
        self.assertIn("-v ON_ERROR_STOP=1", call)
        self.assertNotIn("update", call)

    def test_stop_sets_stopped_without_a_prompt(self):
        result = self._dispatch("stop")
        self.assertEqual(result.returncode, 0, result.stderr)
        update, show = self._calls()
        self.assertIn("update harness_guardrails.dispatch_policy set stopped = true where singleton", update)
        self.assertIn("select case", show)

    def test_stop_fails_if_verification_reports_dispatch_is_still_allowed(self):
        result = self._dispatch("stop", POLICY_STATE="dispatch allowed min_interval_ms=250")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("dispatch allowed", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_stop_propagates_failure_from_update_or_verification(self):
        for variable, code in (("STOP_EXIT_CODE", 125), ("STATUS_EXIT_CODE", 2)):
            with self.subTest(variable=variable):
                result = self._dispatch("stop", **{variable: str(code)})
                self.assertEqual(result.returncode, code)
                self.assertEqual(result.stdout, "")

    def test_stop_rejects_malformed_readback_without_echoing_it(self):
        for state in ("stopped min_interval_ms=", "stopped min_interval_ms=0",
                      "stopped min_interval_ms=250\nsynthetic-diagnostic"):
            with self.subTest(state=state):
                result = self._dispatch("stop", POLICY_STATE=state)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertIn("dispatch stop verification failed", result.stderr)
                self.assertNotIn("synthetic-diagnostic", result.stderr)

    def test_start_refuses_without_a_terminal(self):
        for stdin in (subprocess.DEVNULL, None):
            with self.subTest(stdin=stdin):
                result = (self._dispatch("start") if stdin is not None else
                          subprocess.run(["/bin/bash", str(SCRIPT), "dispatch", "start"], cwd=REPO,
                                         env=self.env, input="start\n", capture_output=True,
                                         text=True, timeout=30))
                self.assertEqual(result.returncode, 2)
                self.assertIn("interactive terminal", result.stderr)
                self.assertEqual(self._calls(), [])

    def test_missing_policy_row_is_an_error(self):
        for action in ("status", "stop"):
            with self.subTest(action=action):
                result = self._dispatch(action, POLICY_STATE="")
                self.assertEqual(result.returncode, 1)
                self.assertIn("dispatch policy row missing", result.stderr)

    def test_unknown_subcommand_is_rejected(self):
        result = self._dispatch("flip")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self._calls(), [])

    def _start_on_a_terminal(self, answer: bytes) -> tuple[int, str]:
        controller, terminal = pty.openpty()
        process = subprocess.Popen(["/bin/bash", str(SCRIPT), "dispatch", "start"], cwd=REPO,
                                   env={**self.env, "POLICY_STATE": "dispatch allowed min_interval_ms=250"},
                                   stdin=terminal, stdout=terminal, stderr=terminal, close_fds=True)
        os.close(terminal)
        output = b""
        try:
            os.write(controller, answer + b"\n")
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                ready, _, _ = select.select([controller], [], [], 0.2)
                if ready:
                    try:
                        chunk = os.read(controller, 4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    output += chunk
                elif process.poll() is not None:
                    break
            return process.wait(timeout=30), output.decode(errors="replace")
        finally:
            if process.poll() is None:
                process.kill()
            os.close(controller)

    def test_start_needs_the_typed_confirmation(self):
        code, output = self._start_on_a_terminal(b"yes")
        self.assertEqual(code, 1)
        self.assertIn("dispatch remains stopped", output)
        self.assertEqual(self._calls(), [])

    def test_start_on_a_terminal_allows_dispatch(self):
        code, output = self._start_on_a_terminal(b"start")
        self.assertEqual(code, 0, output)
        update, show = self._calls()
        self.assertIn("update harness_guardrails.dispatch_policy set stopped = false where singleton", update)
        self.assertIn("dispatch allowed", output)
