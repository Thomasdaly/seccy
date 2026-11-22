"""Runtime configuration, read from the environment.

The orchestrator runs on the M1 and talks to the 2060's services (LiteLLM,
Postgres, Qdrant) over Tailscale. Everything here comes from env so no secret
is ever committed; see .env.example at the repo root.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def _split(name: str, default: str) -> list[str]:
    raw = os.environ.get(name, default)
    return [p.strip() for p in raw.split(",") if p.strip()]


REPO = Path(__file__).resolve().parents[3]
LAB_HOST = "100.102.105.11"


def resolve_kill_switch_path(raw: str | os.PathLike[str]) -> Path:
    """Expand ``~`` first, then anchor relative paths at the repository root.

    ``~/x.kill`` becomes ``$HOME/x.kill`` and ``.kill`` becomes ``REPO/.kill``.
    Absolute paths are kept as given. Symlinks are not followed, so the stop
    file may itself be a (possibly dangling) link.
    """
    try:
        path = Path(raw).expanduser()
    except RuntimeError:
        raise ValueError("HARNESS_KILL_SWITCH: could not expand the home directory") from None
    return path if path.is_absolute() else REPO / path


def configured_rate_limit_rps() -> float:
    return float(os.environ.get("HARNESS_RATE_LIMIT_RPS") or "4")


def configured_kill_switch_file() -> str:
    return str(resolve_kill_switch_path(os.environ.get("HARNESS_KILL_SWITCH") or ".kill"))


def configured_artifacts_dir() -> str:
    return os.environ.get("HARNESS_ARTIFACTS_DIR") or "artifacts"


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
        default_factory=lambda: _split("HARNESS_SCOPE_ALLOWLIST", f"{LAB_HOST}:5061,{LAB_HOST}:5062")
    )
    rate_limit_rps: float = field(default_factory=configured_rate_limit_rps)
    kill_switch_file: str = field(default_factory=configured_kill_switch_file)

    # --- shared live dispatch policy and persistence ---
    database_url: str | None = field(default_factory=lambda: _env("HARNESS_DATABASE_URL"))
    qdrant_url: str | None = field(default_factory=lambda: _env("QDRANT_URL"))
    artifacts_dir: str = field(default_factory=configured_artifacts_dir)

    # --- dedup ---
    dedup_threshold: float = field(default_factory=lambda: float(_env("HARNESS_DEDUP_THRESHOLD", "0.95") or "0.95"))

    def require_key(self) -> None:
        if not self.litellm_key:
            raise SystemExit(
                "LITELLM_MASTER_KEY is not set. Export it (same value as the 2060's .env) "
                "or put it in the orchestrator's environment."
            )
