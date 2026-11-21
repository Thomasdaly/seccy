"""Decode bounded, untrusted JSON in a separate macOS sandbox."""
from __future__ import annotations

import json
import subprocess
import sys
import sysconfig
from pathlib import Path
from typing import Any

MAX_BODY_BYTES = 1024 * 1024
_MAX_WORKER_SECONDS = 2.0
_WORKER = """
import json
import sys

try:
    value = json.load(sys.stdin.buffer)
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > 32:
            raise ValueError
        if isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
        elif isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
    encoded = json.dumps(value, allow_nan=False, separators=(",", ":"))
except (ValueError, RecursionError):
    raise SystemExit(2)
sys.stdout.write(encoded)
"""


class ResponseDecodeError(RuntimeError):
    """A fixed diagnostic safe to store with an exchange."""


class ResponseDecoder:
    def _command(self) -> list[str]:
        if sys.platform != "darwin":
            raise ResponseDecodeError("decoder_unavailable")

        interpreter = Path(sys.executable).resolve()
        directories = {
            Path("/usr/lib"),
            Path("/System/Library"),
            Path("/Library/Apple/System/Library"),
            Path(sysconfig.get_path("stdlib")).resolve(),
        }
        files = {interpreter, Path("/dev/null")}
        libdir = sysconfig.get_config_var("LIBDIR")
        libpython = sysconfig.get_config_var("LDLIBRARY")
        if libdir and libpython:
            files.add((Path(libdir) / libpython).resolve())

        ancestors = {parent for path in directories | files for parent in path.parents}
        readable = [f"(subpath {json.dumps(str(path))})" for path in sorted(directories)]
        readable.extend(f"(literal {json.dumps(str(path))})" for path in sorted(files | ancestors))
        profile = (
            "(version 1) (deny default) (deny network*) (allow sysctl-read) "
            "(allow file-read-metadata) "
            f"(allow process-exec (literal {json.dumps(str(interpreter))})) "
            f"(allow file-read* {' '.join(readable)})"
        )
        return ["/usr/bin/sandbox-exec", "-p", profile, str(interpreter), "-I", "-S", "-B", "-c", _WORKER]

    def decode(self, body: bytes, timeout: float = _MAX_WORKER_SECONDS) -> Any:
        if len(body) > MAX_BODY_BYTES or timeout <= 0:
            raise ResponseDecodeError("response_limit")
        try:
            result = subprocess.run(
                self._command(), input=body, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env={}, cwd="/", close_fds=True, timeout=min(timeout, _MAX_WORKER_SECONDS),
            )
        except subprocess.TimeoutExpired:
            raise ResponseDecodeError("response_limit") from None
        except OSError:
            raise ResponseDecodeError("decoder_unavailable") from None
        if result.returncode == 2:
            raise ResponseDecodeError("invalid_json")
        if result.returncode != 0 or len(result.stdout) > 6 * MAX_BODY_BYTES:
            raise ResponseDecodeError("decoder_unavailable")
        try:
            # Only the sandbox's depth-limited, canonical output enters this parser.
            return json.loads(result.stdout)
        except (ValueError, RecursionError):
            raise ResponseDecodeError("decoder_unavailable") from None
