"""Target HTTP dispatch and redacted exchange records."""
from __future__ import annotations

from contextlib import closing
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .guardrails import Guardrails, ScopeViolation
from .response_decode import MAX_BODY_BYTES, ResponseDecodeError, ResponseDecoder


_SAFE_BODY_KEYS = {
    "admin", "auth_token", "book_title", "Books", "data", "email",
    "message", "owner", "password", "secret", "status", "user", "username", "users",
}


def _redact_body(body: Any, depth: int = 0) -> Any:
    if depth >= 8:
        return "[redacted]"
    if isinstance(body, dict):
        safe = {key: _redact_body(value, depth + 1) for key, value in body.items()
                if key in _SAFE_BODY_KEYS}
        if len(safe) != len(body):
            safe["redacted_fields"] = len(body) - len(safe)
        return safe
    if isinstance(body, list):
        return [_redact_body(item, depth + 1) for item in body[:50]]
    if body is None or isinstance(body, bool):
        return body
    return "[redacted]"


def _evidence_origin(url: str) -> str | None:
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.host:
        return None
    return f"{parsed.scheme}://{parsed.netloc.decode('ascii')}"


def _allowed_route(request: httpx.Request) -> bool:
    if request.url.query or request.url.fragment:
        return False
    path = request.url.raw_path
    if request.method == "GET":
        return path in {b"/books/v1", b"/me", b"/users/v1/_debug"} or bool(
            re.fullmatch(rb"/books/v1/[A-Za-z0-9_-]+", path)
        )
    if request.method == "POST":
        return path in {b"/users/v1/login", b"/users/v1/register"}
    if request.method == "PUT":
        return bool(re.fullmatch(rb"/users/v1/[A-Za-z0-9_-]+/password", path))
    return False


@dataclass
class Exchange:
    """A captured request/response pair. The evidence record."""
    method: str
    url: str
    request_headers: dict[str, str] = field(default_factory=dict)
    request_body: Any = None
    status_code: int | None = None
    response_headers: dict[str, str] = field(default_factory=dict)
    response_body: Any = None
    error: str | None = None

    def to_evidence(self) -> dict:
        return {
            "request": {
                "method": self.method,
                "origin": _evidence_origin(self.url),
                "body": _redact_body(self.request_body),
            },
            "response": {
                "status": self.status_code,
                "body": _redact_body(self.response_body),
            },
            "error": self.error is not None,
        }


class Executor:
    def __init__(self, guardrails: Guardrails, timeout: float = 20.0):
        self.g = guardrails
        self._timeout = timeout
        self._client = httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False)
        self._decoder = ResponseDecoder()
        self._decoder_ready = False
        self._dispatch_lock = threading.Lock()
        self.request_count = 0

    def close(self) -> None:
        self._client.close()

    def send(self, method: str, url: str, *, headers: dict | None = None,
             json_body: Any = None) -> Exchange:
        headers = dict(headers or {})
        if any(name.lower() in {"host", "cookie", "accept-encoding"} for name in headers):
            self.g.record("scope_denied", None, "caller supplied a restricted request header")
            raise ScopeViolation("restricted request header")
        exchange = Exchange(method=method.upper(), url=url, request_body=json_body)

        with self._dispatch_lock:
            self.g.check_stop()
            auth = next((value for name, value in headers.items() if name.lower() == "authorization"), None)
            if auth is not None and (not isinstance(auth, str) or auth.strip() in {"", "Bearer", "Bearer None"}):
                exchange.error = "identity_unavailable"
                return exchange
            try:
                request = self._client.build_request(
                    method, url, headers={**headers, "Accept-Encoding": "identity"}, json=json_body,
                )
            except httpx.InvalidURL:
                self.g.record("scope_denied", None, "invalid target URL")
                raise ScopeViolation("invalid target URL") from None
            if not _allowed_route(request):
                self.g.record("scope_denied", None, "target method or path denied")
                raise ScopeViolation("target method or path is outside scope")
            self.g.check_scope(request.url)
            if not self._decoder_ready:
                self._decoder.decode(b"null")
                self._decoder_ready = True
            self.g.guard_request(request.url)
            self.request_count += 1
            deadline = time.monotonic() + self._timeout
            try:
                self._capture(request, exchange, deadline)
            except (httpx.HTTPError, httpx.InvalidURL) as exc:
                exchange.error = type(exc).__name__
            finally:
                self._client.cookies.clear()
                self.g.rate.defer()
                self.g.shared.defer()
        return exchange

    def _capture(self, request: httpx.Request, exchange: Exchange, deadline: float) -> None:
        with closing(self._client.send(request, stream=True)) as response:
            exchange.status_code = response.status_code
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                exchange.error = "unsupported_response_encoding"
                return
            body = bytearray()
            for chunk in response.iter_raw():
                self.g.check_stop()
                if time.monotonic() >= deadline or len(body) + len(chunk) > MAX_BODY_BYTES:
                    exchange.error = "response_limit"
                    return
                body.extend(chunk)
            if time.monotonic() >= deadline:
                exchange.error = "response_limit"
                return
            if "json" in response.headers.get("content-type", "").lower():
                try:
                    exchange.response_body = self._decoder.decode(bytes(body), deadline - time.monotonic())
                except ResponseDecodeError as exc:
                    if str(exc) == "decoder_unavailable":
                        raise
                    exchange.error = str(exc)
            elif body:
                exchange.response_body = b"[redacted]"
