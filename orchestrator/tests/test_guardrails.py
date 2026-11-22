"""Guardrail unit tests. Synthetic hosts only; nothing here touches a network."""

import math
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch
from uuid import uuid4

import httpx

from harness.config import REPO, Config, resolve_kill_switch_path
from harness.executor import Executor
from harness.guardrails import (
    Guardrails,
    KillEngaged,
    KillSwitch,
    RateLimiter,
    ScopeAllowlist,
    ScopeViolation,
)


def _not_stopped_shared() -> Mock:
    return Mock(**{"stopped.return_value": False, "reserve.return_value": True})


class KillSwitchPathTests(TestCase):
    def setUp(self):
        home = TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.home = Path(home.name)
        env = patch.dict(os.environ, {"HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        for name in ("HARNESS_KILL", "HARNESS_KILL_SWITCH"):
            os.environ.pop(name, None)

    def test_tilde_expands_before_repo_join(self):
        path = resolve_kill_switch_path("~/x.kill")
        self.assertEqual(path, self.home / "x.kill")
        self.assertFalse(path.is_relative_to(REPO))

    def test_relative_paths_resolve_from_repo(self):
        self.assertEqual(resolve_kill_switch_path(".kill"), REPO / ".kill")
        self.assertEqual(resolve_kill_switch_path("state/stop"), REPO / "state" / "stop")

    def test_absolute_paths_are_kept(self):
        absolute = self.home / "elsewhere" / "stop"
        self.assertEqual(resolve_kill_switch_path(str(absolute)), absolute)

    def test_resolution_is_idempotent(self):
        for raw in ("~/x.kill", ".kill"):
            with self.subTest(raw=raw):
                once = resolve_kill_switch_path(raw)
                self.assertEqual(resolve_kill_switch_path(str(once)), once)
                self.assertEqual(KillSwitch(str(once)).path, once)

    def test_unknown_home_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "HARNESS_KILL_SWITCH"):
            resolve_kill_switch_path(f"~no-such-user-{uuid4().hex}/x.kill")

    def test_config_resolves_environment_value(self):
        cases = {
            "~/x.kill": self.home / "x.kill",
            ".kill": REPO / ".kill",
            "": REPO / ".kill",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw), patch.dict(os.environ, {"HARNESS_KILL_SWITCH": raw}):
                self.assertEqual(Config().kill_switch_file, str(expected))
        self.assertEqual(Config().kill_switch_file, str(REPO / ".kill"))

    def test_guardrails_watch_the_configured_path(self):
        env = {
            "HARNESS_KILL_SWITCH": "~/x.kill",
            "HARNESS_SCOPE_ALLOWLIST": "example.invalid:80",
            "HARNESS_RATE_LIMIT_RPS": "4",
        }
        with patch.dict(os.environ, env):
            guard = Guardrails.from_config(Config(), _not_stopped_shared())
        self.assertEqual(guard.kill.path, self.home / "x.kill")

    def test_stop_file_under_home_stops_dispatch(self):
        kill = KillSwitch("~/x.kill")
        guard = Guardrails(ScopeAllowlist(["example.invalid:80"]), RateLimiter(4), kill,
                           shared=_not_stopped_shared())
        self.assertFalse(kill.engaged())
        guard.check_stop()
        (self.home / "x.kill").touch()
        self.assertTrue(kill.engaged())
        with self.assertRaises(KillEngaged):
            guard.check_stop()
        self.assertEqual(guard.events[-1].kind, "kill_switch")

    def test_dangling_symlink_engages(self):
        (self.home / "x.kill").symlink_to(self.home / "missing-target")
        self.assertTrue(KillSwitch("~/x.kill").engaged())

    def test_environment_flag_engages_without_a_file(self):
        kill = KillSwitch("~/x.kill")
        with patch.dict(os.environ, {"HARNESS_KILL": "1"}):
            self.assertTrue(kill.engaged())
        self.assertFalse(kill.engaged())


class ScopeAllowlistTests(TestCase):
    def test_valid_entries_are_normalised(self):
        scope = ScopeAllowlist(["example.invalid:80", "API.example.invalid:8443"])
        self.assertEqual(scope.entries, {"example.invalid:80", "api.example.invalid:8443"})
        self.assertEqual(scope.check("http://example.invalid/books/v1"), "example.invalid:80")
        self.assertEqual(scope.check("http://EXAMPLE.invalid:80/"), "example.invalid:80")
        self.assertEqual(scope.check("https://api.example.invalid:8443/"), "api.example.invalid:8443")

    def test_default_ports_follow_the_scheme(self):
        scope = ScopeAllowlist(["example.invalid:80"])
        self.assertTrue(scope.allowed("http://example.invalid/"))
        self.assertFalse(scope.allowed("https://example.invalid/"))
        self.assertFalse(scope.allowed("http://example.invalid:8080/"))

    def test_ipv6_entries(self):
        for entry in ("[::1]:8080", "::1:8080"):
            with self.subTest(entry=entry):
                scope = ScopeAllowlist([entry])
                self.assertEqual(scope.entries, {"[::1]:8080"})
                self.assertEqual(scope.check("http://[::1]:8080/me"), "[::1]:8080")
                self.assertFalse(scope.allowed("http://[::1]:8081/me"))
                self.assertFalse(scope.allowed("http://[::2]:8080/me"))
                # A non-canonical spelling is refused rather than reinterpreted.
                self.assertFalse(scope.allowed("http://[0:0:0:0:0:0:0:1]:8080/me"))

    def test_entries_with_userinfo_are_rejected(self):
        for entry in ("user@example.invalid:80", "user:pw@example.invalid:80"):
            with self.subTest(entry=entry), self.assertRaisesRegex(ValueError, "invalid scope entry"):
                ScopeAllowlist([entry])

    def test_urls_with_userinfo_are_denied_even_for_allowed_hosts(self):
        scope = ScopeAllowlist(["example.invalid:80"])
        for url in ("http://user@example.invalid/", "http://user:pw@example.invalid:80/"):
            with self.subTest(url=url):
                with self.assertRaisesRegex(ScopeViolation, "invalid target authority"):
                    scope.check(url)
                self.assertFalse(scope.allowed(url))

    def test_bad_ports_are_rejected(self):
        for entry in ("example.invalid:0", "example.invalid:65536", "example.invalid:99999",
                      "example.invalid:abc", "example.invalid:+80", "example.invalid:-1",
                      "example.invalid: 80", "example.invalid:٨٠", "example.invalid:",
                      "example.invalid"):
            with self.subTest(entry=entry), self.assertRaisesRegex(ValueError, "invalid scope entry"):
                ScopeAllowlist([entry])

    def test_bad_target_ports_are_denied(self):
        scope = ScopeAllowlist(["example.invalid:80"])
        for url in ("http://example.invalid:0/", "http://example.invalid:65536/"):
            with self.subTest(url=url), self.assertRaises(ScopeViolation):
                scope.check(url)

    def test_malformed_entries_are_rejected(self):
        for entry in ("", ":80", "example.invalid:80/path", "example.invalid:80?q", "example.invalid:80#f"):
            with self.subTest(entry=entry), self.assertRaisesRegex(ValueError, "invalid scope entry"):
                ScopeAllowlist([entry])

    def test_non_http_targets_are_denied(self):
        scope = ScopeAllowlist(["example.invalid:80"])
        for url in ("ftp://example.invalid:80/", "//example.invalid/", "example.invalid:80", "not a url"):
            with self.subTest(url=url), self.assertRaises(ScopeViolation):
                scope.check(url)

    def test_empty_allowlist_fails_closed(self):
        scope = ScopeAllowlist([])
        self.assertEqual(scope.entries, set())
        for url in ("http://example.invalid/", "http://127.0.0.1:80/", "http://[::1]:80/"):
            with self.subTest(url=url):
                self.assertFalse(scope.allowed(url))
                with self.assertRaisesRegex(ScopeViolation, "outside scope"):
                    scope.check(url)

    def test_explicitly_empty_environment_denies_everything(self):
        for raw in ("", " ", " , ,"):
            with self.subTest(raw=raw), patch.dict(os.environ, {"HARNESS_SCOPE_ALLOWLIST": raw}):
                cfg = Config()
                self.assertEqual(cfg.scope_allowlist, [])
                self.assertFalse(ScopeAllowlist(cfg.scope_allowlist).allowed("http://100.102.105.11:5061/"))

    def test_empty_allowlist_records_denial_and_never_reaches_transport(self):
        kill = Mock(spec=KillSwitch, **{"engaged.return_value": False})
        shared = _not_stopped_shared()
        guard = Guardrails(ScopeAllowlist([]), RateLimiter(4), kill, shared=shared)
        transport = Mock(side_effect=AssertionError("target transport was reached"))
        with patch("harness.executor.httpx.Client",
                   return_value=httpx.Client(transport=httpx.MockTransport(transport))):
            executor = Executor(guard)
        self.addCleanup(executor.close)
        with self.assertRaises(ScopeViolation):
            executor.send("GET", "http://example.invalid/me")
        self.assertEqual(guard.events[-1].kind, "scope_denied")
        self.assertEqual(executor.request_count, 0)
        shared.reserve.assert_not_called()
        transport.assert_not_called()


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.now = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RateLimiterTests(TestCase):
    def setUp(self):
        self.clock = FakeClock()
        for name in ("monotonic", "sleep"):
            patcher = patch(f"harness.guardrails.time.{name}", getattr(self.clock, name))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.limiter = RateLimiter(4)

    def _no_stop(self) -> None:
        return None

    def test_invalid_rates_are_rejected(self):
        for rps in (0, -1, math.inf, math.nan, 5e-324):
            with self.subTest(rps=rps), self.assertRaises(ValueError):
                RateLimiter(rps)

    def test_first_permit_is_immediate(self):
        self.assertFalse(self.limiter.acquire(self._no_stop))
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(self.limiter.throttles, 0)

    def test_back_to_back_permits_are_spaced(self):
        start = self.clock.now
        granted = []
        for _ in range(4):
            self.limiter.acquire(self._no_stop)
            granted.append(self.clock.now - start)
        for actual, expected in zip(granted, (0.0, 0.25, 0.5, 0.75)):
            self.assertAlmostEqual(actual, expected, places=9)
        self.assertEqual(self.limiter.throttles, 3)
        # Waits are polled in short slices so a stop is noticed promptly.
        self.assertTrue(all(0 < s <= 0.1 for s in self.clock.sleeps))

    def test_idle_time_does_not_bank_a_burst(self):
        self.limiter.acquire(self._no_stop)
        self.clock.advance(10)
        self.assertFalse(self.limiter.acquire(self._no_stop))
        before = self.clock.now
        self.assertTrue(self.limiter.acquire(self._no_stop))
        self.assertAlmostEqual(self.clock.now - before, 0.25, places=9)

    def test_defer_measures_from_dispatch_completion(self):
        start = self.clock.now
        self.limiter.acquire(self._no_stop)
        self.clock.advance(0.2)  # the request itself took 200 ms
        self.limiter.defer()
        self.limiter.acquire(self._no_stop)
        self.assertAlmostEqual(self.clock.now - start, 0.45, places=9)

    def test_stop_before_waiting_raises_without_sleeping(self):
        self.limiter.acquire(self._no_stop)

        def stop():
            raise KillEngaged("synthetic")

        with self.assertRaises(KillEngaged):
            self.limiter.acquire(stop)
        self.assertEqual(self.clock.sleeps, [])

    def test_stop_during_wait_raises_and_grants_no_permit(self):
        start = self.clock.now
        self.limiter.acquire(self._no_stop)
        calls = []

        def stop_after_first_sleep():
            calls.append(self.clock.now)
            if self.clock.sleeps:
                raise KillEngaged("synthetic")

        with self.assertRaises(KillEngaged):
            self.limiter.acquire(stop_after_first_sleep)
        self.assertEqual(len(self.clock.sleeps), 1)
        self.assertLess(self.clock.now - start, 0.25)
        self.assertEqual(len(calls), 2)

        # The aborted wait neither consumed nor pushed back the next permit.
        self.limiter.acquire(self._no_stop)
        self.assertAlmostEqual(self.clock.now - start, 0.25, places=9)

    def test_kill_during_local_wait_prevents_shared_reservation(self):
        kill = Mock(spec=KillSwitch)
        kill.detail.return_value = "engaged (stop file present or inaccessible)"
        kill.engaged.side_effect = lambda: bool(self.clock.sleeps)
        shared = _not_stopped_shared()
        guard = Guardrails(ScopeAllowlist(["example.invalid:80"]), self.limiter, kill, shared=shared)
        self.limiter.defer()  # a dispatch just finished, so the next one must wait
        with self.assertRaises(KillEngaged):
            guard.guard_request(httpx.URL("http://example.invalid/me"))
        shared.reserve.assert_not_called()
        self.assertEqual(guard.events[-1].kind, "kill_switch")
