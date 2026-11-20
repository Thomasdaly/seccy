"""Runtime configuration, read from the environment.

The orchestrator runs on the M1 and talks to the 2060's services (LiteLLM,
Postgres, Qdrant) over Tailscale. Everything here comes from env so no secret
is ever committed; see .env.example at the repo root.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def _split(name: str, default: str) -> list[str]:
    raw = _env(name, default) or ""
    return [p.strip() for p in raw.split(",") if p.strip()]


@dataclass(frozen=True)
class Config:
    # --- model router (LiteLLM, OpenAI-compatible) ---
    litellm_base_url: str = field(default_factory=lambda: _env("LITELLM_BASE_URL", "http://100.102.105.11:4000"))
    litellm_key: str = field(default_factory=lambda: _env("LITELLM_MASTER_KEY", "") or "")
    model_glue: str = field(default_factory=lambda: _env("HARNESS_MODEL_GLUE", "glue"))
    model_planner: str = field(default_factory=lambda: _env("HARNESS_MODEL_PLANNER", "planner"))
    model_judge: str = field(default_factory=lambda: _env("HARNESS_MODEL_JUDGE", "judge"))
    model_embed: str = field(default_factory=lambda: _env("HARNESS_MODEL_EMBED", "embed"))

    # --- guardrails (guide §7: scope allowlist, rate limit, kill switch) ---
    # host:port entries the harness is permitted to touch. Anything else is
    # dropped and logged. Defaults to the VAmPI lab twins only.
    scope_allowlist: list[str] = field(
        default_factory=lambda: _split("HARNESS_SCOPE_ALLOWLIST", "100.102.105.11:5061,100.102.105.11:5062")
    )
    rate_limit_rps: float = field(default_factory=lambda: float(_env("HARNESS_RATE_LIMIT_RPS", "4") or "4"))
    kill_switch_file: str = field(default_factory=lambda: _env("HARNESS_KILL_SWITCH", ".kill") or ".kill")

    # --- persistence (all optional; the run scores without them) ---
    database_url: str | None = field(default_factory=lambda: _env("HARNESS_DATABASE_URL"))
    qdrant_url: str | None = field(default_factory=lambda: _env("QDRANT_URL"))
    artifacts_dir: str = field(default_factory=lambda: _env("HARNESS_ARTIFACTS_DIR", "artifacts") or "artifacts")

    # --- dedup ---
    dedup_threshold: float = field(default_factory=lambda: float(_env("HARNESS_DEDUP_THRESHOLD", "0.95") or "0.95"))

    def require_key(self) -> None:
        if not self.litellm_key:
            raise SystemExit(
                "LITELLM_MASTER_KEY is not set. Export it (same value as the 2060's .env) "
                "or put it in the orchestrator's environment."
            )
