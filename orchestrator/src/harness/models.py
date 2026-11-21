"""The model router client. One OpenAI-compatible endpoint (LiteLLM) fronts all
four tiers; each call is tagged so Langfuse joins spend to the run and names the
loop step (dashboard Flows contract):

    metadata = {"session_id": run_id, "generation_name": "<step>"}

`session_id` -> harness.runs.id (per-run cost, cost per finding).
`generation_name` -> the step name in the dashboard's Steps table.

Tiers (guide §5): glue = local free; planner = hosted-open cheap reasoning;
judge = frontier, rare. Every call here logs its measured USD cost so the run
can report cost per confirmed bug without re-querying Langfuse.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field

import httpx

from .config import Config

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def _cost_of(response: httpx.Response, data: dict) -> float | None:
    """Return the first usable USD cost, or flag missing spend."""
    usage = data.get("usage")
    candidates = (
        response.headers.get("x-litellm-response-cost"),
        usage.get("cost") if isinstance(usage, dict) else None,
    )
    for value in candidates:
        if isinstance(value, bool):
            continue
        try:
            cost = float(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(cost) and cost >= 0:
            return cost
    return None


@dataclass
class Usage:
    calls: int = 0
    cost_usd: float = 0.0
    by_tier: dict[str, float] = field(default_factory=dict)
    calls_by_tier: dict[str, int] = field(default_factory=dict)

    unknown_cost_calls: int = 0

    def add(self, tier: str, cost: float | None) -> None:
        self.calls += 1
        self.calls_by_tier[tier] = self.calls_by_tier.get(tier, 0) + 1
        if cost is None:
            self.unknown_cost_calls += 1
            return
        self.cost_usd += cost
        self.by_tier[tier] = self.by_tier.get(tier, 0.0) + cost


class Models:
    def __init__(self, cfg: Config, run_id: str):
        self.cfg = cfg
        self.run_id = run_id
        self.usage = Usage()
        self._client = httpx.Client(
            base_url=cfg.litellm_base_url,
            headers={"Authorization": f"Bearer {cfg.litellm_key}"},
            timeout=120.0,
            trust_env=False,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "Models":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- raw chat completion with run tagging ---
    def _chat(self, model: str, step: str, messages: list[dict], max_tokens: int = 512,
              temperature: float = 0.0) -> str:
        body = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            # LiteLLM forwards these to the Langfuse callback.
            "metadata": {
                "session_id": self.run_id,
                "generation_name": step,
                "tags": ["harness", f"tier:{model}"],
            },
        }
        cost = None
        try:
            response = self._client.post("/v1/chat/completions", json=body)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("expected a response object")
            cost = _cost_of(response, data)
            content = data["choices"][0]["message"].get("content")
            return content if isinstance(content, str) else ""
        finally:
            self.usage.add(model, cost)

    def _chat_json(self, model: str, step: str, messages: list[dict], max_tokens: int = 512) -> dict:
        """Return a decoded object, leaving unavailable responses empty."""
        try:
            txt = self._chat(model, step, messages, max_tokens=max_tokens)
        except (httpx.HTTPError, ValueError, TypeError, AttributeError, KeyError, IndexError):
            return {}
        try:
            data = json.loads(txt)
        except ValueError:
            match = _JSON_BLOCK.search(txt)
            if not match:
                return {}
            try:
                data = json.loads(match.group(0))
            except ValueError:
                return {}
        return data if isinstance(data, dict) else {}

    # --- tier helpers ---
    def glue(self, step: str, prompt: str, system: str | None = None, max_tokens: int = 384) -> dict:
        msgs = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": prompt}
        ]
        return self._chat_json(self.cfg.model_glue, step, msgs, max_tokens=max_tokens)

    def planner(self, step: str, prompt: str, system: str | None = None, max_tokens: int = 768) -> dict:
        msgs = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": prompt}
        ]
        return self._chat_json(self.cfg.model_planner, step, msgs, max_tokens=max_tokens)

    def judge(self, step: str, prompt: str, system: str | None = None, max_tokens: int = 512) -> dict:
        msgs = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": prompt}
        ]
        return self._chat_json(self.cfg.model_judge, step, msgs, max_tokens=max_tokens)

    def embed(self, step: str, text: str) -> list[float]:
        body = {
            "model": self.cfg.model_embed,
            "input": text,
            "metadata": {"session_id": self.run_id, "generation_name": step, "tags": ["harness"]},
        }
        cost = None
        try:
            r = self._client.post("/v1/embeddings", json=body)
            r.raise_for_status()
            data = r.json()
            cost = _cost_of(r, data) if isinstance(data, dict) else None
            return data["data"][0]["embedding"]
        except (httpx.HTTPError, ValueError, TypeError, AttributeError, KeyError, IndexError):
            return []
        finally:
            self.usage.add(self.cfg.model_embed, cost)
