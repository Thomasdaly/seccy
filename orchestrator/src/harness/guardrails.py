"""Guardrails baked into every run (guide §7). Not optional: in Australia there
is no good-faith research defence, so the harness must refuse any host it was
not explicitly authorized to touch, cap its request rate, and stop on a flag.

These enforce *throughout* the loop — every outbound request passes through
`ScopeAllowlist.check` and `RateLimiter.acquire`, and the loop polls the
`KillSwitch` between operations. Denials are surfaced to the caller so they land
in `harness.guardrail_events` and on the dashboard.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse


class ScopeViolation(Exception):
    """Raised when a request targets a host outside the allowlist."""


@dataclass
class GuardrailEvent:
    kind: str          # scope_denied | rate_limited | kill_switch
    host: str | None
    detail: str | None = None


class ScopeAllowlist:
    """An explicit host:port allowlist. Anything else is dropped and logged."""

    def __init__(self, entries: list[str]):
        self.entries = set(entries)

    def host_port(self, url: str) -> str:
        p = urlparse(url)
        port = p.port or (443 if p.scheme == "https" else 80)
        return f"{p.hostname}:{port}"

    def allowed(self, url: str) -> bool:
        return self.host_port(url) in self.entries

    def check(self, url: str) -> None:
        if not self.allowed(url):
            raise ScopeViolation(self.host_port(url))


class RateLimiter:
    """Simple thread-safe token bucket: at most `rps` requests per second,
    matching program rules (breaching a bounty's ceiling gets you banned)."""

    def __init__(self, rps: float):
        self.rps = max(rps, 0.1)
        self.min_interval = 1.0 / self.rps
        self._lock = threading.Lock()
        self._next = 0.0
        self.throttles = 0

    def acquire(self) -> bool:
        """Block until the next request is permitted. Returns True if it had to
        wait (a throttle event worth logging)."""
        with self._lock:
            now = time.monotonic()
            wait = self._next - now
            if wait > 0:
                self.throttles += 1
                time.sleep(wait)
                self._next += self.min_interval
                return True
            self._next = now + self.min_interval
            return False


class KillSwitch:
    """One flag halts all runners. A file on disk (or the HARNESS_KILL env var)
    so it can be tripped from anywhere — another shell, a cron, a watchdog."""

    def __init__(self, path: str):
        self.path = path

    def engaged(self) -> bool:
        return os.path.exists(self.path) or os.environ.get("HARNESS_KILL") == "1"

    def detail(self) -> str:
        if os.environ.get("HARNESS_KILL") == "1":
            return "engaged (HARNESS_KILL=1)"
        return f"engaged ({self.path} present)"


@dataclass
class Guardrails:
    scope: ScopeAllowlist
    rate: RateLimiter
    kill: KillSwitch
    events: list[GuardrailEvent] = field(default_factory=list)

    @classmethod
    def from_config(cls, cfg) -> "Guardrails":
        return cls(
            scope=ScopeAllowlist(cfg.scope_allowlist),
            rate=RateLimiter(cfg.rate_limit_rps),
            kill=KillSwitch(cfg.kill_switch_file),
        )

    def record(self, kind: str, host: str | None, detail: str | None = None) -> None:
        self.events.append(GuardrailEvent(kind, host, detail))

    def guard_request(self, url: str) -> None:
        """Enforce scope + rate before an outbound request. Raises ScopeViolation
        (also logged) if the host is out of scope."""
        try:
            self.scope.check(url)
        except ScopeViolation as v:
            self.record("scope_denied", str(v), f"blocked {url}")
            raise
        if self.rate.acquire():
            self.record("rate_limited", self.scope.host_port(url))
