"""Inspect local safety settings and exercise isolation with synthetic data."""
from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from .config import configured_artifacts_dir, configured_kill_switch_file, configured_rate_limit_rps
from .guardrails import KillSwitch, RateLimiter, ScopeAllowlist
from .response_decode import ResponseDecodeError, ResponseDecoder
from .store import ArtifactStore

_ISOLATION_WORKER = """
import ctypes
import os
import sys

try:
    with open(sys.argv[1], 'rb') as canary:
        canary.read()
except PermissionError:
    pass
else:
    raise SystemExit(1)

library = ctypes.CDLL('/usr/lib/libsandbox.1.dylib')
check = library.sandbox_check
check.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
check.restype = ctypes.c_int
for operation in (b'network-inbound', b'network-outbound'):
    if check(os.getpid(), operation, 0) != 1:
        raise SystemExit(1)
"""


def _check_local_stop() -> str:
    stop = KillSwitch(configured_kill_switch_file())
    state = "engaged" if stop.engaged(inspect_file=True) else "clear"
    return f"{state}, path {json.dumps(str(stop.path))}"


def _check_scope() -> str:
    raw = os.environ.get("HARNESS_SCOPE_ALLOWLIST")
    if raw is None:
        return "no configured override"
    scope = ScopeAllowlist([entry.strip() for entry in raw.split(",") if entry.strip()])
    return f"{len(scope.entries)} configured authorities"


def _check_rate() -> str:
    RateLimiter(configured_rate_limit_rps())
    return "finite positive request ceiling"


def _verify_decoder_isolation() -> str:
    decoder = ResponseDecoder()
    if decoder.decode(b'{"canary":true}') != {"canary": True}:
        raise ValueError("unexpected decoder output")
    with TemporaryDirectory(prefix="decoder-safety-") as scratch:
        canary = Path(scratch) / "canary"
        canary.write_text("synthetic only", encoding="utf-8")
        # Use the decoder's profile and interpreter, with trusted local test code.
        command = [*decoder._command()[:-1], _ISOLATION_WORKER, str(canary)]
        result = subprocess.run(command, env={}, cwd="/", stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                close_fds=True, timeout=2)
        if result.returncode != 0:
            raise ValueError("isolation check failed")
    return "synthetic JSON decoded, file and network access denied"


def _check_artifact_permissions() -> str:
    base = Path(configured_artifacts_dir())
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    with TemporaryDirectory(prefix=".safety-check-", dir=base) as scratch:
        artifacts = ArtifactStore(scratch, "permissions")
        artifact = Path(artifacts.write_evidence("canary", {"synthetic": True}))
        if stat.S_IMODE(artifacts.dir.stat().st_mode) != 0o700 or stat.S_IMODE(artifact.stat().st_mode) != 0o600:
            raise ValueError("unexpected artifact permissions")
    return "0700 run directory and 0600 evidence file"


def main() -> int:
    checks = (
        ("local stop", _check_local_stop, "could not inspect the configured stop file"),
        ("scope allowlist", _check_scope, "expected a list of host:port entries"),
        ("rate limit", _check_rate, "expected a finite positive request ceiling"),
        ("decoder isolation", _verify_decoder_isolation, "sandbox unavailable or isolation check failed"),
        ("artifact permissions", _check_artifact_permissions, "could not create private synthetic artifacts"),
    )
    failed = False
    for label, check, failure in checks:
        try:
            detail = check()
        except (ValueError, OSError, ResponseDecodeError, subprocess.TimeoutExpired):
            failed = True
            print(f"FAIL {label} | {failure}")
        else:
            print(f"ok {label} | {detail}")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
