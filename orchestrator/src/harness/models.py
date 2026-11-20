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
import re
from dataclasses import dataclass, field

import httpx

from .config import Config

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


@dataclass
class Usage:
    calls: int = 0
    cost_usd: float = 0.0
    by_tier: dict[str, float] = field(default_factory=dict)
    calls_by_tier: dict[str, int] = field(default_factory=dict)

    def add(self, tier: str, cost: float) -> None:
        self.calls += 1
        self.cost_usd += cost
        self.by_tier[tier] = self.by_tier.get(tier, 0.0) + cost
        self.calls_by_tier[tier] = self.calls_by_tier.get(tier, 0) + 1


class Models:
    def __init__(self, cfg: Config, run_id: str):
        self.cfg = cfg
        self.run_id = run_id
        self.usage = Usage()
        self._client = httpx.Client(
            base_url=cfg.litellm_base_url,
            headers={"Authorization": f"Bearer {cfg.litellm_key}"},
            timeout=120.0,
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
        r = self._client.post("/v1/chat/completions", json=body)
        r.raise_for_status()
        data = r.json()
        self.usage.add(model, float(data.get("usage", {}).get("cost", 0.0) or 0.0))
        return data["choices"][0]["message"].get("content") or ""

    def _chat_json(self, model: str, step: str, messages: list[dict], max_tokens: int = 512) -> dict:
        """A chat call expected to return JSON. Tolerant of prose wrapping or a
        reasoning model emitting the object inside other text. On a transport or
        HTTP error, returns {} so the loop degrades to its deterministic prior
        rather than aborting the whole run."""
        try:
            txt = self._chat(model, step, messages, max_tokens=max_tokens)
        except httpx.HTTPError as e:
            detail = ""
            if isinstance(e, httpx.HTTPStatusError):
                detail = f" {e.response.status_code}: {e.response.text[:200]}"
            print(f"[models] {model}/{step} call failed ({type(e).__name__}){detail} — falling back")
            return {}
        try:
            return json.loads(txt)
        except Exception:
            m = _JSON_BLOCK.search(txt)
            if m:
                try:
                    return json.loads(m.group(0))
                except Exception:
                    pass
        return {}

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
        try:
            r = self._client.post("/v1/embeddings", json=body)
            r.raise_for_status()
            data = r.json()
            self.usage.add(self.cfg.model_embed, float(data.get("usage", {}).get("cost", 0.0) or 0.0))
            return data["data"][0]["embedding"]
        except Exception as e:
            print(f"[models] embed/{step} failed ({type(e).__name__}) — dedup treats as unique")
            return []
