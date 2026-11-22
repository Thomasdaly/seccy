"""Response decoding tests use synthetic input only."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase, skipUnless
from unittest.mock import Mock, patch

import httpx

from harness.executor import Executor
from harness.guardrails import Guardrails, KillSwitch, RateLimiter, ScopeAllowlist
from harness.response_decode import ResponseDecodeError, ResponseDecoder


class ResponseDecoderTests(TestCase):
    def test_framework_interpreter_gets_a_literal_permission_for_its_library(self):
        with tempfile.TemporaryDirectory() as directory:
            framework = Path(directory).resolve() / "Python.framework"
            version = framework / "Versions" / "3.13"
            interpreter = version / "bin" / "python3.13"
            config = {
                "LIBDIR": str(version / "lib"),
                "LDLIBRARY": "Python.framework/Versions/3.13/Python",
                "PYTHONFRAMEWORK": "Python",
            }
            bundled_python = version / "Resources/Python.app/Contents/MacOS/Python"
            for executable in (interpreter, bundled_python):
                with self.subTest(executable=executable.name):
                    if executable == bundled_python:
                        bundled_python.parent.mkdir(parents=True)
                        bundled_python.touch()
                    with (patch("harness.response_decode.sys.platform", "darwin"),
                          patch("harness.response_decode.sys.executable", str(interpreter)),
                          patch("harness.response_decode.sysconfig.get_config_var", side_effect=config.get),
                          patch("harness.response_decode.sysconfig.get_path",
                                return_value=str(version / "lib/python3.13"))):
                        _, _, profile, selected, *_ = ResponseDecoder()._command()

                    self.assertEqual(selected, str(executable))
                    self.assertIn(f"(literal {json.dumps(str(version / 'Python'))})", profile)
                    old_library_path = version / "lib" / config["LDLIBRARY"]
                    self.assertNotIn(f"(literal {json.dumps(str(old_library_path))})", profile)
                    self.assertNotIn(f"(subpath {json.dumps(str(version))})", profile)
                    self.assertNotIn(f"(subpath {json.dumps(str(framework))})", profile)

    def test_no_in_process_fallback(self):
        with patch("harness.response_decode.sys.platform", "unsupported"):
            with self.assertRaisesRegex(ResponseDecodeError, "decoder_unavailable"):
                ResponseDecoder().decode(b'{"synthetic": true}')

    def test_executor_refuses_dispatch_when_decoder_is_unavailable(self):
        shared = Mock(**{"stopped.return_value": False})
        kill = Mock(spec=KillSwitch, **{"engaged.return_value": False})
        guard = Guardrails(ScopeAllowlist(["example.invalid:80"]), RateLimiter(4), kill, shared=shared)
        target = Mock(side_effect=AssertionError("target transport was reached"))
        with patch("harness.executor.httpx.Client",
                   return_value=httpx.Client(transport=httpx.MockTransport(target))):
            executor = Executor(guard)
        self.addCleanup(executor.close)
        with patch("harness.response_decode.sys.platform", "unsupported"):
            with self.assertRaisesRegex(ResponseDecodeError, "decoder_unavailable"):
                executor.send("GET", "http://example.invalid/me")
        self.assertEqual(executor.request_count, 0)
        shared.reserve.assert_not_called()
        target.assert_not_called()


@skipUnless(sys.platform == "darwin", "tests the macOS sandbox")
class MacOSDecoderTests(TestCase):
    def setUp(self):
        self.decoder = ResponseDecoder()

    def test_decodes_valid_json_for_existing_callers(self):
        body = '{"data": {"valid": true}, "list": [false, null], "name": "caf\u00e9"}'.encode()
        self.assertEqual(
            self.decoder.decode(body),
            {"data": {"valid": True}, "list": [False, None], "name": "caf\u00e9"},
        )

    def test_invalid_deep_and_non_finite_bodies_are_rejected(self):
        for body in (b"not json", b"[" * 64 + b"0" + b"]" * 64, b"1e999"):
            with self.subTest(body=body), self.assertRaisesRegex(ResponseDecodeError, "invalid_json"):
                self.decoder.decode(body)

    def test_worker_cannot_read_parent_environment_or_canary_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "canary"
            path.write_text("synthetic secret")
            command = self.decoder._command()
            code = """
import os, sys
assert 'SECCY_TEST_SECRET' not in os.environ
try:
    open(sys.argv[1], 'rb').read()
except PermissionError:
    raise SystemExit(0)
raise SystemExit(1)
"""
            with patch.dict(os.environ, {"SECCY_TEST_SECRET": "synthetic environment secret"}):
                result = subprocess.run(
                    [*command[:-1], code, str(path)], env={}, cwd="/", capture_output=True, timeout=2,
                )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_sandbox_policy_denies_network_without_opening_connections(self):
        code = """
import ctypes
import os
library = ctypes.CDLL("/usr/lib/libsandbox.1.dylib")
check = library.sandbox_check
check.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
check.restype = ctypes.c_int
assert check(os.getpid(), b"network-outbound", 0) == 1
assert check(os.getpid(), b"network-inbound", 0) == 1
"""
        result = subprocess.run(
            [*self.decoder._command()[:-1], code], env={}, cwd="/", capture_output=True, timeout=2,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
