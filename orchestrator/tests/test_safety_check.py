"""Offline safety diagnostics use only synthetic settings and local files."""

import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, skipUnless
from unittest.mock import patch

from harness.safety_check import main
from harness.store import ArtifactStore


class SafetyCheckTests(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.stop_file = root / "stop\nfile"
        self.stop_file.touch()
        self.artifacts = root / "artifacts"
        self.settings = {
            "HARNESS_KILL_SWITCH": str(self.stop_file),
            "HARNESS_SCOPE_ALLOWLIST": "example.invalid:80, [::1]:8080",
            "HARNESS_RATE_LIMIT_RPS": "2",
            "HARNESS_ARTIFACTS_DIR": str(self.artifacts),
            "HARNESS_DATABASE_URL": "synthetic-database-secret",
        }

    def run_check(self):
        out, err = io.StringIO(), io.StringIO()
        with (patch.dict(os.environ, self.settings, clear=True), redirect_stdout(out), redirect_stderr(err),
              patch("socket.socket", side_effect=AssertionError("socket opened")) as socket,
              patch("psycopg.connect", side_effect=AssertionError("database opened")) as connect):
            status = main()
        socket.assert_not_called()
        connect.assert_not_called()
        self.assertEqual(err.getvalue(), "")
        self.assertNotIn("synthetic-database-secret", out.getvalue())
        return status, out.getvalue().splitlines()

    def test_valid_settings_report_five_results_and_clean_up_canary(self):
        with patch("harness.safety_check._verify_decoder_isolation"):
            status, lines = self.run_check()

        self.assertEqual(status, 0)
        self.assertEqual(len(lines), 5)
        self.assertTrue(all(line.startswith("ok ") for line in lines), lines)
        self.assertIn(f"ok local stop | engaged, path {json.dumps(str(self.stop_file))}", lines)
        self.assertIn("ok scope allowlist | 2 configured authorities", lines)
        self.assertEqual(list(self.artifacts.iterdir()), [])

    def test_independent_failures_are_reported_without_echoing_inputs(self):
        self.settings.update({"HARNESS_SCOPE_ALLOWLIST": "http://synthetic-scope-secret",
                              "HARNESS_RATE_LIMIT_RPS": "synthetic-rate-secret"})
        self.artifacts.write_text("keep this file")
        with patch("harness.safety_check._verify_decoder_isolation",
                   side_effect=subprocess.TimeoutExpired("synthetic-decoder-secret", 2)):
            status, lines = self.run_check()

        self.assertEqual(status, 1)
        self.assertEqual(len(lines), 5)
        self.assertEqual([line.split(" | ")[0] for line in lines if line.startswith("FAIL ")],
                         ["FAIL scope allowlist", "FAIL rate limit", "FAIL decoder isolation",
                          "FAIL artifact permissions"])
        self.assertNotIn("-secret", "\n".join(lines))
        self.assertEqual(self.artifacts.read_text(), "keep this file")

    def test_explicit_empty_scope_is_a_valid_deny_all_configuration(self):
        self.settings.update({"HARNESS_SCOPE_ALLOWLIST": "", "HARNESS_KILL": "1"})
        self.stop_file.unlink()
        with patch("harness.safety_check._verify_decoder_isolation"):
            status, lines = self.run_check()

        self.assertEqual(status, 0)
        self.assertIn("ok scope allowlist | 0 configured authorities", lines)
        self.assertIn(f"ok local stop | engaged, path {json.dumps(str(self.stop_file))}", lines)

    def test_reports_clear_state_for_the_resolved_tilde_path(self):
        root = self.stop_file.parent
        self.settings.update({"HOME": str(root), "HARNESS_KILL_SWITCH": "~/clear"})
        with patch("harness.safety_check._verify_decoder_isolation"):
            status, lines = self.run_check()

        self.assertEqual(status, 0)
        self.assertIn(f"ok local stop | clear, path {json.dumps(str(root / 'clear'))}", lines)

    def test_nonpositive_and_nonfinite_rates_fail(self):
        with patch("harness.safety_check._verify_decoder_isolation"):
            for value in ("0", "-1", "nan", "inf"):
                with self.subTest(value=value):
                    self.settings["HARNESS_RATE_LIMIT_RPS"] = value
                    status, lines = self.run_check()
                    self.assertEqual(status, 1)
                    self.assertIn("FAIL rate limit | expected a finite positive request ceiling", lines)

    def test_malformed_stop_path_fails_without_echoing_it(self):
        self.settings["HARNESS_KILL_SWITCH"] = str(self.stop_file.parent / ("synthetic-secret-" * 80))
        with patch("harness.safety_check._verify_decoder_isolation"):
            for flag in ("0", "1"):
                with self.subTest(flag=flag):
                    self.settings["HARNESS_KILL"] = flag
                    status, lines = self.run_check()

                    self.assertEqual(status, 1)
                    self.assertIn("FAIL local stop | could not inspect the configured stop file", lines)
                    self.assertNotIn("synthetic-secret", "\n".join(lines))

    def test_artifact_check_rejects_a_public_evidence_file(self):
        def write_public(self, probe_id, payload):
            path = self.dir / f"{probe_id}.json"
            path.touch(mode=0o644)
            path.chmod(0o644)
            return str(path)

        with (patch("harness.safety_check._verify_decoder_isolation"),
              patch("harness.safety_check.ArtifactStore.write_evidence", write_public)):
            status, lines = self.run_check()

        self.assertEqual(status, 1)
        self.assertTrue(any(line.startswith("FAIL artifact permissions") for line in lines))
        self.assertEqual(list(self.artifacts.iterdir()), [])

    def test_artifact_check_removes_its_canary_but_preserves_existing_files(self):
        self.artifacts.mkdir()
        existing = self.artifacts / "existing.json"
        existing.write_text("synthetic existing file")

        class PublicRunDirectory(ArtifactStore):
            def __init__(self, base_dir, run_id):
                super().__init__(base_dir, run_id)
                self.dir.chmod(0o755)

        with patch("harness.safety_check._verify_decoder_isolation"):
            status, lines = self.run_check()
            self.assertEqual(status, 0)
            self.assertIn("ok artifact permissions | 0700 run directory and 0600 evidence file", lines)
            self.assertEqual(list(self.artifacts.iterdir()), [existing])
            self.assertEqual(existing.read_text(), "synthetic existing file")

            with patch("harness.safety_check.ArtifactStore", PublicRunDirectory):
                status, lines = self.run_check()
            self.assertEqual(status, 1)
            self.assertIn("FAIL artifact permissions | could not create private synthetic artifacts", lines)
            self.assertEqual(list(self.artifacts.iterdir()), [existing])
            self.assertEqual(existing.read_text(), "synthetic existing file")

    def test_successful_decode_cannot_mask_a_failed_isolation_worker(self):
        with (patch("harness.safety_check.ResponseDecoder.decode", return_value={"canary": True}),
              patch("harness.safety_check.ResponseDecoder._command", return_value=["worker", "decoder"]),
              patch("harness.safety_check.subprocess.run", return_value=subprocess.CompletedProcess([], 1))):
            status, lines = self.run_check()

        self.assertEqual(status, 1)
        self.assertIn("FAIL decoder isolation | sandbox unavailable or isolation check failed", lines)

    @skipUnless(sys.platform == "darwin", "tests the macOS sandbox")
    def test_real_decoder_passes_the_offline_isolation_check(self):
        status, lines = self.run_check()
        self.assertEqual(status, 0, lines)
        self.assertTrue(any(line.startswith("ok decoder isolation") for line in lines))

    @skipUnless(sys.platform == "darwin", "tests the module entry point with the macOS sandbox")
    def test_module_entry_point_reports_success_and_failure(self):
        root = self.stop_file.parent
        # Python loads this module before the documented -m entry point.
        (root / "sitecustomize.py").write_text("""\
import socket
import ssl
import psycopg

def forbidden(*args, **kwargs):
    raise AssertionError("real socket or database connection attempted")

socket.socket = forbidden
psycopg.connect = forbidden
""")
        expected = [
            f"ok local stop | engaged, path {json.dumps(str(self.stop_file))}",
            "ok scope allowlist | 2 configured authorities",
            "ok rate limit | finite positive request ceiling",
            "ok decoder isolation | synthetic JSON decoded, file and network access denied",
            "ok artifact permissions | 0700 run directory and 0600 evidence file",
        ]
        settings = {**self.settings, "PYTHONPATH": str(root)}
        command = [sys.executable, "-m", "harness.safety_check"]
        for rate, code in (("2", 0), ("synthetic-invalid-rate-secret", 1)):
            with self.subTest(code=code):
                result = subprocess.run(command, env={**settings, "HARNESS_RATE_LIMIT_RPS": rate},
                                        capture_output=True, text=True, timeout=10)
                lines = expected.copy()
                if code:
                    lines[2] = "FAIL rate limit | expected a finite positive request ceiling"
                self.assertEqual(result.returncode, code, result.stdout)
                self.assertEqual(result.stdout.splitlines(), lines)
                self.assertEqual(result.stderr, "")
                self.assertEqual(list(self.artifacts.iterdir()), [])
                self.assertTrue(self.stop_file.exists())
