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


# The only method + path shapes the executor will dispatch. Each pattern must
# match the whole raw path (which includes any "?"); the template is what
# evidence records instead of the concrete path, so object identifiers such as
# usernames and titles never reach artifacts.
_SEGMENT = rb"[A-Za-z0-9_-]+"
_ROUTES: tuple[tuple[str, re.Pattern[bytes], str], ...] = (
    ("GET", re.compile(rb"/books/v1"), "/books/v1"),
    ("GET", re.compile(rb"/me"), "/me"),
    ("GET", re.compile(rb"/users/v1/_debug"), "/users/v1/_debug"),
    ("GET", re.compile(rb"/books/v1/" + _SEGMENT), "/books/v1/{book_title}"),
    ("POST", re.compile(rb"/users/v1/login"), "/users/v1/login"),
    ("POST", re.compile(rb"/users/v1/register"), "/users/v1/register"),
    ("PUT", re.compile(rb"/users/v1/" + _SEGMENT + rb"/password"), "/users/v1/{username}/password"),
)


def _route_template(method: str, raw_path: bytes) -> str | None:
    for allowed_method, pattern, template in _ROUTES:
        if method == allowed_method and pattern.fullmatch(raw_path):
            return template
    return None


def _allowed_route(request: httpx.Request) -> bool:
    if request.url.query or request.url.fragment:
        return False
    return _route_template(request.method, request.url.raw_path) is not None


def _evidence_route(method: str, url: str) -> str | None:
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL:
        return None
    if parsed.query or parsed.fragment:
        return None
    return _route_template(method, parsed.raw_path)


# Exchange.error values are set only by this module: fixed strings or httpx
# exception class names. Evidence records a closed vocabulary, never free text.
_ERROR_KINDS = {
    "identity_unavailable", "unsupported_response_encoding", "response_limit",
    "invalid_json", "decoder_unavailable",
}


def _error_kind(error: str | None) -> str | None:
    if error is None:
        return None
    if error in _ERROR_KINDS:
        return error
    return "timeout" if error.endswith("Timeout") else "transport_error"


def _content_kind(content_type: str) -> str | None:
    media = content_type.split(";", 1)[0].strip().lower()
    if not media:
        return None
    if "json" in media:
        return "json"
    return "text" if media.startswith("text/") else "other"


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
    content_kind: str | None = None    # json | text | other, derived from Content-Type
    response_bytes: int | None = None  # body size when it was read in full

    def to_evidence(self) -> dict:
        """Derived, review-safe fields only: no concrete path, query, headers or values."""
        return {
            "request": {
                "method": self.method,
                "origin": _evidence_origin(self.url),
                "route": _evidence_route(self.method, self.url),
                "body": _redact_body(self.request_body),
            },
            "response": {
                "status": self.status_code,
                "content_kind": self.content_kind,
                "bytes": self.response_bytes,
                "body": _redact_body(self.response_body),
            },
            "error": self.error is not None,
            "error_kind": _error_kind(self.error),
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
            exchange.content_kind = _content_kind(response.headers.get("content-type", ""))
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
            exchange.response_bytes = len(body)
            if "json" in response.headers.get("content-type", "").lower():
                try:
                    exchange.response_body = self._decoder.decode(bytes(body), deadline - time.monotonic())
                except ResponseDecodeError as exc:
                    if str(exc) == "decoder_unavailable":
                        raise
                    exchange.error = str(exc)
            elif body:
                exchange.response_body = b"[redacted]"
