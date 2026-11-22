"""Enforce host scope, dispatch pacing, and the operator stop signal."""
from __future__ import annotations

import math
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

import httpx

from .config import resolve_kill_switch_path
from .shared_dispatch import SharedDispatch


class ScopeViolation(Exception):
    """Raised when a request targets a host outside the allowlist."""


class KillEngaged(Exception):
    """Raised when the operator stop signal is active."""


@dataclass
class GuardrailEvent:
    kind: str          # scope_denied | rate_limited | kill_switch
    host: str | None
    detail: str | None = None


class ScopeAllowlist:
    """An explicit host:port allowlist. Anything else is dropped and logged."""

    def __init__(self, entries: list[str]):
        self.entries = set()
        for entry in entries:
            try:
                host, port = entry.rsplit(":", 1)
                if ":" in host and not host.startswith("["):
                    host = f"[{host}]"
                url = httpx.URL(f"http://{host}:{port}")
                if not port.isascii() or not port.isdecimal() or url.raw_path != b"/":
                    raise ValueError
                self.entries.add(self.host_port(url))
            except (ValueError, httpx.InvalidURL, ScopeViolation):
                raise ValueError("invalid scope entry, expected host:port") from None

    def host_port(self, url: str | httpx.URL) -> str:
        try:
            parsed = httpx.URL(url)
        except httpx.InvalidURL:
            raise ScopeViolation("invalid target URL") from None
        if parsed.scheme not in {"http", "https"} or not parsed.raw_host or parsed.userinfo:
            raise ScopeViolation("invalid target authority")
        port = parsed.port
        if port is None:
            port = 443 if parsed.scheme == "https" else 80
        if not 1 <= port <= 65535:
            raise ScopeViolation("invalid target port")
        host = parsed.raw_host.decode("ascii")
        if ":" in host:
            host = f"[{host}]"
        return f"{host}:{port}"

    def allowed(self, url: str | httpx.URL) -> bool:
        try:
            self.check(url)
        except ScopeViolation:
            return False
        return True

    def check(self, url: str | httpx.URL) -> str:
        authority = self.host_port(url)
        if authority not in self.entries:
            raise ScopeViolation("target authority is outside scope")
        return authority


class RateLimiter:
    """Space permits using a monotonic clock, without idle bursts."""

    def __init__(self, rps: float):
        if not math.isfinite(rps) or rps <= 0 or not math.isfinite(1.0 / rps):
            raise ValueError("request rate must be finite and positive")
        self.rps = rps
        self.min_interval = 1.0 / rps
        self._lock = threading.Lock()
        self._next = 0.0
        self.throttles = 0

    def defer(self) -> None:
        """Measure the next interval from a completed or failed dispatch."""
        with self._lock:
            self._next = time.monotonic() + self.min_interval

    def acquire(self, check_stop: Callable[[], None]) -> bool:
        with self._lock:
            check_stop()
            waited = self._next > time.monotonic()
            if waited:
                self.throttles += 1
            while (remaining := self._next - time.monotonic()) > 0:
                time.sleep(min(remaining, 0.1))
                check_stop()
            self._next = time.monotonic() + self.min_interval
            return waited


class KillSwitch:
    """Local emergency stop via a file or the runner's environment."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = resolve_kill_switch_path(path)

    def engaged(self) -> bool:
        if os.environ.get("HARNESS_KILL") == "1":
            return True
        try:
            self.path.lstat()
        except FileNotFoundError:
            return False
        except OSError:
            return True
        return True

    def detail(self) -> str:
        if os.environ.get("HARNESS_KILL") == "1":
            return "engaged (HARNESS_KILL=1)"
        return "engaged (stop file present or inaccessible)"


@dataclass
class Guardrails:
    scope: ScopeAllowlist
    rate: RateLimiter
    kill: KillSwitch
    shared: SharedDispatch
    events: list[GuardrailEvent] = field(default_factory=list)

    @classmethod
    def from_config(cls, cfg, shared: SharedDispatch) -> "Guardrails":
        return cls(
            scope=ScopeAllowlist(cfg.scope_allowlist),
            rate=RateLimiter(cfg.rate_limit_rps),
            kill=KillSwitch(cfg.kill_switch_file),
            shared=shared,
        )

    def record(self, kind: str, host: str | None, detail: str | None = None) -> None:
        self.events.append(GuardrailEvent(kind, host, detail))

    def check_stop(self) -> None:
        if self.kill.engaged():
            detail = self.kill.detail()
        elif self.shared.stopped():
            detail = "engaged (shared dispatch policy)"
        else:
            return
        self.record("kill_switch", None, detail)
        raise KillEngaged(detail)

    def check_scope(self, url: httpx.URL) -> str:
        try:
            return self.scope.check(url)
        except ScopeViolation:
            self.record("scope_denied", None, "target authority denied")
            raise

    def guard_request(self, url: httpx.URL) -> None:
        self.check_stop()
        authority = self.check_scope(url)
        waited = self.rate.acquire(self.check_stop)
        while not self.shared.reserve():
            waited = True
            self.check_stop()
            time.sleep(0.1)
        if waited:
            self.record("rate_limited", authority)
        self.check_stop()
