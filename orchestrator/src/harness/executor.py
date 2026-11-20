"""HTTP engine: every outbound request passes through the guardrails (scope +
rate), and each returns a captured request/response record — the replayable
evidence a finding needs (principle 3: no evidence, no report).

MVP note: in production the replay step runs in an isolated sandbox that never
holds the target credentials or the LiteLLM keys (guide principle 4). Against the
offline lab the orchestrator replays directly; the scope allowlist still means it
can reach nothing but the lab.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from .guardrails import Guardrails, ScopeViolation


def _mask_headers(headers: dict[str, str]) -> dict[str, str]:
    out = {}
    for k, v in headers.items():
        if k.lower() == "authorization":
            out[k] = v[:12] + "…" if v else v   # keep scheme/prefix, drop the token
        else:
            out[k] = v
    return out


def _truncate(body: Any, limit: int = 4000) -> Any:
    s = body if isinstance(body, str) else None
    if s is not None and len(s) > limit:
        return s[:limit] + f"…[+{len(s) - limit} chars]"
    return body


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
                "url": self.url,
                "headers": _mask_headers(self.request_headers),
                "body": _truncate(self.request_body),
            },
            "response": {
                "status": self.status_code,
                "headers": self.response_headers,
                "body": _truncate(self.response_body),
            },
            "error": self.error,
        }


class Executor:
    def __init__(self, guardrails: Guardrails, timeout: float = 20.0):
        self.g = guardrails
        self._client = httpx.Client(timeout=timeout, verify=False, follow_redirects=False)
        self.request_count = 0

    def close(self) -> None:
        self._client.close()

    def send(self, method: str, url: str, *, headers: dict | None = None,
             json_body: Any = None) -> Exchange:
        """Guarded request. Returns an Exchange even on transport error so the
        caller always has a record. Raises ScopeViolation (logged) if out of scope."""
        headers = dict(headers or {})
        ex = Exchange(method=method.upper(), url=url, request_headers=headers, request_body=json_body)
        self.g.guard_request(url)        # scope + rate; ScopeViolation propagates
        self.request_count += 1
        try:
            r = self._client.request(method, url, headers=headers, json=json_body)
            ex.status_code = r.status_code
            ex.response_headers = {k: v for k, v in r.headers.items()
                                   if k.lower() in ("content-type", "content-length", "server")}
            ctype = r.headers.get("content-type", "")
            if "json" in ctype:
                try:
                    ex.response_body = r.json()
                except Exception:
                    ex.response_body = r.text
            else:
                ex.response_body = r.text
        except ScopeViolation:
            raise
        except Exception as e:  # transport-level failure is still a record
            ex.error = f"{type(e).__name__}: {e}"
        return ex
