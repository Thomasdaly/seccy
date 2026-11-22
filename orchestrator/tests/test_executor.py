"""Executor route allowlist and evidence redaction. Synthetic input only."""

import json
import math
import random
from unittest import TestCase
from unittest.mock import Mock, patch

import httpx

from harness.executor import (
    _ERROR_KINDS,
    _SAFE_BODY_KEYS,
    Exchange,
    Executor,
    _allowed_route,
    _content_kind,
    _error_kind,
    _evidence_route,
    _redact_body,
    _route_template,
)
from harness.guardrails import Guardrails, KillSwitch, RateLimiter, ScopeAllowlist, ScopeViolation

BASE = "http://example.invalid"


class AllowedRouteTests(TestCase):
    ALLOWED = [
        ("GET", "/books/v1"),
        ("GET", "/me"),
        ("GET", "/users/v1/_debug"),
        ("GET", "/books/v1/synthetic_Title-01"),
        ("get", "/books/v1/abc"),
        ("POST", "/users/v1/login"),
        ("POST", "/users/v1/register"),
        ("PUT", "/users/v1/name1/password"),
        ("PUT", "/users/v1/user_02-x/password"),
        # httpx removes dot segments before dispatch; the normalised path is what is checked.
        ("GET", "/books/v1/x/../abc"),
        ("PUT", "/users/v1/name1/../admin/password"),
    ]

    DENIED = [
        # query strings and fragments
        ("GET", "/books/v1?title=x"),
        ("GET", "/me?"),
        ("GET", "/me#fragment"),
        # methods outside the per-path allowlist
        ("DELETE", "/users/v1/name1"),
        ("DELETE", "/books/v1/abc"),
        ("PATCH", "/users/v1/name1/password"),
        ("HEAD", "/me"),
        ("OPTIONS", "/me"),
        ("POST", "/books/v1"),
        ("POST", "/me"),
        ("GET", "/users/v1/login"),
        ("GET", "/users/v1/name1/password"),
        ("PUT", "/users/v1/login"),
        ("PUT", "/books/v1/abc"),
        # paths outside the allowlist or not exact matches
        ("GET", "/"),
        ("GET", "/users/v1"),
        ("GET", "/users/v1/name1"),
        ("GET", "/createdb"),
        ("GET", "/me/"),
        ("GET", "//me"),
        ("GET", "/ME"),
        ("GET", "/books/v1/"),
        ("GET", "/books/v1/a/b"),
        ("GET", "/books/v1/../users/v1/name1"),
        ("GET", "/books/v1/%2e%2e"),
        ("GET", "/books/v1/a%2Fb"),
        ("GET", "/books/v1/a%0A"),
        ("GET", "/books/v1/a.b"),
        ("GET", "/books/v1/caf%C3%A9"),
        ("PUT", "/users/v1//password"),
        ("PUT", "/users/v1/a/b/password"),
        ("PUT", "/users/v1/name1/password/"),
        ("PUT", "/users/v1/name1/../../admin/password"),
        ("POST", "/users/v1/login/"),
        ("POST", "/users/v1/register/extra"),
    ]

    def test_allowed_routes(self):
        for method, path in self.ALLOWED:
            with self.subTest(method=method, path=path):
                self.assertTrue(_allowed_route(httpx.Request(method, BASE + path)))

    def test_denied_routes(self):
        for method, path in self.DENIED:
            with self.subTest(method=method, path=path):
                self.assertFalse(_allowed_route(httpx.Request(method, BASE + path)))

    def test_denied_route_is_refused_before_transport(self):
        kill = Mock(spec=KillSwitch, **{"engaged.return_value": False})
        shared = Mock(**{"stopped.return_value": False, "reserve.return_value": True})
        guard = Guardrails(ScopeAllowlist(["example.invalid:80"]), RateLimiter(4), kill, shared=shared)
        transport = Mock(side_effect=AssertionError("target transport was reached"))
        with patch("harness.executor.httpx.Client",
                   return_value=httpx.Client(transport=httpx.MockTransport(transport))):
            executor = Executor(guard)
        self.addCleanup(executor.close)
        for method, path in (("DELETE", "/users/v1/name1"), ("GET", "/books/v1?x=1"), ("GET", "/createdb")):
            with self.subTest(method=method, path=path):
                with self.assertRaisesRegex(ScopeViolation, "method or path"):
                    executor.send(method, BASE + path)
                self.assertEqual(guard.events[-1].kind, "scope_denied")
        self.assertEqual(executor.request_count, 0)
        shared.reserve.assert_not_called()
        transport.assert_not_called()


CANARY_STRINGS = ("canary-password-7f3a", "eyJcanary.token.sig", "canary@example.invalid", "", " ")
CANARY_NUMBERS = (424242, -17, 3.14159, 1e300, 2**70, math.inf, -math.inf, math.nan, 0, 0.0)


def _assert_no_raw_scalars(test: TestCase, output, path: str = "$") -> None:
    """Every leaf must be None, a bool, the redaction marker, or a field count."""
    if isinstance(output, dict):
        for key, value in output.items():
            test.assertIsInstance(key, str, path)
            if key == "redacted_fields":
                test.assertIs(type(value), int, f"{path}.{key}")
                test.assertGreater(value, 0, f"{path}.{key}")
                continue
            test.assertIn(key, _SAFE_BODY_KEYS, f"{path}.{key}")
            _assert_no_raw_scalars(test, value, f"{path}.{key}")
    elif isinstance(output, list):
        test.assertLessEqual(len(output), 50, path)
        for index, item in enumerate(output):
            _assert_no_raw_scalars(test, item, f"{path}[{index}]")
    elif isinstance(output, str):
        test.assertEqual(output, "[redacted]", path)
    else:
        test.assertTrue(output is None or type(output) is bool, f"{path}: {output!r}")


class RedactBodyTests(TestCase):
    def test_scalars_are_redacted(self):
        for value in CANARY_STRINGS + CANARY_NUMBERS + (b"raw bytes", bytearray(b"x"), ("tuple",), {"set"}):
            with self.subTest(value=value):
                self.assertEqual(_redact_body(value), "[redacted]")

    def test_none_and_booleans_are_kept(self):
        for value in (None, True, False):
            with self.subTest(value=value):
                self.assertIs(_redact_body(value), value)

    def test_safe_keys_keep_structure_but_not_values(self):
        body = {key: value for key, value in zip(sorted(_SAFE_BODY_KEYS), CANARY_STRINGS * 3)}
        body["admin"] = True
        body["status"] = 424242
        out = _redact_body(body)
        self.assertEqual(set(out), set(body))
        self.assertIs(out["admin"], True)
        self.assertEqual(out["status"], "[redacted]")
        _assert_no_raw_scalars(self, out)

    def test_unsafe_keys_are_dropped_and_counted(self):
        out = _redact_body({"id": 424242, "token": "eyJcanary.token.sig", "canary-key": True,
                            "redacted_fields": "canary-password-7f3a", "username": "canary"})
        self.assertEqual(out, {"username": "[redacted]", "redacted_fields": 4})

    def test_nested_values_are_redacted(self):
        body = {
            "data": {"user": {"username": "canary", "password": "canary-password-7f3a",
                              "email": "canary@example.invalid", "admin": False, "id": 424242}},
            "users": [{"username": "canary", "email": "canary@example.invalid"}, 424242, "canary", None],
            "Books": [[[3.14159]]],
        }
        out = _redact_body(body)
        self.assertEqual(out["data"]["user"]["redacted_fields"], 1)
        self.assertEqual(out["users"][1:], ["[redacted]", "[redacted]", None])
        self.assertEqual(out["Books"], [[["[redacted]"]]])
        _assert_no_raw_scalars(self, out)

    def test_deep_and_long_bodies_are_bounded(self):
        deep = "canary-password-7f3a"
        for _ in range(20):
            deep = {"data": deep}
        _assert_no_raw_scalars(self, _redact_body(deep))
        self.assertEqual(len(_redact_body(["canary"] * 500)), 50)

    def test_random_bodies_never_emit_raw_scalars(self):
        rng = random.Random(1602)
        keys = sorted(_SAFE_BODY_KEYS) + ["id", "token", "redacted_fields", "Password", "ADMIN", ""]
        scalars = CANARY_STRINGS + CANARY_NUMBERS + (True, False, None)

        def build(depth: int):
            roll = rng.random()
            if depth > 11 or roll < 0.25 + 0.06 * depth:
                return rng.choice(scalars)
            if depth == 0 and roll > 0.9:
                return [build(1) for _ in range(rng.randint(51, 60))]
            if roll < 0.65:
                return {rng.choice(keys): build(depth + 1) for _ in range(rng.randint(0, 4))}
            return [build(depth + 1) for _ in range(rng.randint(0, 4))]

        for index in range(300):
            body = build(0)
            with self.subTest(index=index):
                out = _redact_body(body)
                _assert_no_raw_scalars(self, out)
                encoded = json.dumps(out)
                for canary in CANARY_STRINGS[:3]:
                    self.assertNotIn(canary, encoded)

    def test_evidence_carries_no_raw_request_or_response_data(self):
        exchange = Exchange(
            method="PUT",
            url="http://user:canary-password-7f3a@example.invalid:5061/users/v1/canary/password?token=eyJcanary",
            request_headers={"Authorization": "Bearer eyJcanary.token.sig"},
            request_body={"password": "canary-password-7f3a", "id": 424242},
            status_code=200,
            response_headers={"Set-Cookie": "session=canary"},
            response_body={"message": "canary@example.invalid", "auth_token": "eyJcanary.token.sig"},
            error="ReadTimeout",
        )
        evidence = exchange.to_evidence()
        self.assertEqual(evidence["request"]["origin"], "http://example.invalid:5061")
        self.assertIsNone(evidence["request"]["route"])  # a query string has no template
        self.assertIs(evidence["error"], True)
        self.assertEqual(evidence["error_kind"], "timeout")
        encoded = json.dumps(evidence)
        for raw in ("canary", "424242", "user:", "/users/v1", "token=", "Bearer", "session", "ReadTimeout"):
            self.assertNotIn(raw, encoded)
        _assert_no_raw_scalars(self, evidence["request"]["body"])
        _assert_no_raw_scalars(self, evidence["response"]["body"])

    def test_evidence_records_the_route_template_not_the_identifier(self):
        evidence = Exchange(method="PUT", url=BASE + "/users/v1/canary_user/password").to_evidence()
        self.assertEqual(evidence["request"]["route"], "/users/v1/{username}/password")
        self.assertNotIn("canary", json.dumps(evidence))
        self.assertEqual(set(evidence), {"request", "response", "error", "error_kind"})
        self.assertEqual(set(evidence["request"]), {"method", "origin", "route", "body"})
        self.assertEqual(set(evidence["response"]), {"status", "content_kind", "bytes", "body"})


class EvidenceFieldTests(TestCase):
    def test_route_templates_cover_exactly_the_allowed_routes(self):
        for method, path in AllowedRouteTests.ALLOWED:
            with self.subTest(method=method, path=path):
                request = httpx.Request(method, BASE + path)
                template = _route_template(request.method, request.url.raw_path)
                self.assertIsNotNone(template)
                self.assertNotIn("canary", template)
        for method, path in AllowedRouteTests.DENIED:
            with self.subTest(method=method, path=path):
                request = httpx.Request(method, BASE + path)
                if not (request.url.query or request.url.fragment):
                    self.assertIsNone(_route_template(request.method, request.url.raw_path))

    def test_evidence_route_templates(self):
        cases = {
            ("GET", "/books/v1/bookTitle123"): "/books/v1/{book_title}",
            ("GET", "/books/v1"): "/books/v1",
            ("PUT", "/users/v1/name2/password"): "/users/v1/{username}/password",
            ("POST", "/users/v1/register"): "/users/v1/register",
            ("LOGIN", "/users/v1/login"): None,
            ("GET", "/books/v1/x?y=1"): None,
            ("GET", "/createdb"): None,
        }
        for (method, path), expected in cases.items():
            with self.subTest(method=method, path=path):
                self.assertEqual(_evidence_route(method, BASE + path), expected)
        self.assertIsNone(_evidence_route("GET", "http://[::1"))

    def test_error_kind_is_a_closed_vocabulary(self):
        for kind in _ERROR_KINDS:
            self.assertEqual(_error_kind(kind), kind)
        self.assertIsNone(_error_kind(None))
        self.assertEqual(_error_kind("ReadTimeout"), "timeout")
        self.assertEqual(_error_kind("ConnectTimeout"), "timeout")
        for raw in ("ConnectError", "RemoteProtocolError", "canary-password-7f3a", ""):
            with self.subTest(raw=raw):
                self.assertEqual(_error_kind(raw), "transport_error")

    def test_content_kind_is_a_closed_vocabulary(self):
        cases = {
            "application/json": "json",
            "Application/JSON; charset=utf-8": "json",
            "application/problem+json": "json",
            "text/html; charset=utf-8": "text",
            "text/plain": "text",
            "image/png": "other",
            "canary<script>": "other",
            "": None,
            " ; charset=utf-8": None,
        }
        for header, expected in cases.items():
            with self.subTest(header=header):
                self.assertEqual(_content_kind(header), expected)


def _streamed(body: bytes, content_type: str) -> httpx.Response:
    # The executor reads the raw stream, so the mock must not pre-read the body.
    return httpx.Response(200, stream=httpx.ByteStream(body), headers={"Content-Type": content_type})


class FakeDecoder:
    def decode(self, body: bytes, timeout: float | None = None):
        return json.loads(body)


class CapturedEvidenceTests(TestCase):
    def _executor(self, handler) -> Executor:
        kill = Mock(spec=KillSwitch, **{"engaged.return_value": False})
        shared = Mock(**{"stopped.return_value": False, "reserve.return_value": True})
        guard = Guardrails(ScopeAllowlist(["example.invalid:80"]), RateLimiter(1000), kill, shared=shared)
        with patch("harness.executor.httpx.Client",
                   return_value=httpx.Client(transport=httpx.MockTransport(handler))):
            executor = Executor(guard)
        executor._decoder = FakeDecoder()
        self.addCleanup(executor.close)
        return executor

    def test_json_response_records_kind_and_size_only(self):
        body = json.dumps({"book_title": "canary-title", "secret": "canary-secret", "owner": "name2"}).encode()
        executor = self._executor(lambda request: _streamed(body, "application/json"))
        exchange = executor.send("GET", BASE + "/books/v1/canary-title")
        evidence = exchange.to_evidence()
        self.assertEqual(evidence["response"]["content_kind"], "json")
        self.assertEqual(evidence["response"]["bytes"], len(body))
        self.assertEqual(evidence["request"]["route"], "/books/v1/{book_title}")
        self.assertIsNone(evidence["error_kind"])
        self.assertEqual(evidence["response"]["body"],
                         {"book_title": "[redacted]", "secret": "[redacted]", "owner": "[redacted]"})
        self.assertNotIn("canary", json.dumps(evidence))

    def test_non_json_response_is_not_decoded(self):
        executor = self._executor(lambda request: _streamed(b"<p>canary</p>", "text/html"))
        evidence = executor.send("GET", BASE + "/me").to_evidence()
        self.assertEqual(evidence["response"]["content_kind"], "text")
        self.assertEqual(evidence["response"]["bytes"], len(b"<p>canary</p>"))
        self.assertEqual(evidence["response"]["body"], "[redacted]")

    def test_transport_failure_records_only_the_error_kind(self):
        def fail(request):
            raise httpx.ConnectError("canary diagnostic")

        evidence = self._executor(fail).send("GET", BASE + "/me").to_evidence()
        self.assertEqual(evidence["error_kind"], "transport_error")
        self.assertIsNone(evidence["response"]["bytes"])
        self.assertNotIn("canary", json.dumps(evidence))
