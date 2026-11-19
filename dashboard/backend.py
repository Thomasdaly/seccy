"""Data access + aggregation for the harness dashboard.

Every upstream is read-only: LiteLLM (routes, liveness), Ollama (models, VRAM),
Qdrant (collections), Langfuse public API (generations, traces, daily metrics)
and Postgres as the `dashboard_ro` role (harness schema, see db/harness.sql).
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import shutil
import time
from collections import Counter, defaultdict, deque
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import quote

import httpx
import psycopg
import yaml
from psycopg import sql
from psycopg.rows import dict_row

log = logging.getLogger("dashboard")

# Fixed route order = fixed colour slot per route (colour follows the entity).
TIERS = ["glue", "planner", "judge", "embed"]
# range -> (span seconds, bucket seconds)
RANGES = {
    "24h": (86400, 3600),
    "7d": (7 * 86400, 86400),
    "30d": (30 * 86400, 86400),
    "90d": (91 * 86400, 7 * 86400),
}
HARNESS_TABLES = {  # table -> column to sort newest-first by
    "runs": "started_at",
    "operations": "id",
    "probes": "id",
    "exchanges": "id",
    "verdicts": "id",
    "guardrail_events": "ts",
}
HISTORY_SAMPLES = 2880  # 24h at 30s
POLL_SECONDS = 30


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key) or default


# --------------------------------------------------------------------------- helpers


def parse_ts(v: Any) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


def percentile(vals: list[float], q: float) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    k = (len(s) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def make_buckets(rng: str, now: float | None = None) -> tuple[list[float], int]:
    span, step = RANGES[rng]
    now = time.time() if now is None else now
    end = (math.floor(now / step) + 1) * step
    n = span // step
    return [end - (n - i) * step for i in range(n)], step


def clean(v: Any, limit: int = 4000) -> Any:
    """JSON-safe copy with long strings trimmed (raw views can hold big bodies)."""
    if isinstance(v, dict):
        return {str(k): clean(x, limit) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [clean(x, limit) for x in v]
    if isinstance(v, str):
        return v if len(v) <= limit else v[:limit] + f"… [+{len(v) - limit:,} chars]"
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (bytes, memoryview)):
        b = bytes(v)
        return f"<{len(b):,} bytes> " + b[:64].hex()
    if v is None or isinstance(v, (int, float, bool)):
        return v
    return str(v)


def preview(v: Any, n: int = 160) -> str:
    """One-line preview of a chat input/output for table cells."""
    if v is None:
        return ""
    if isinstance(v, dict):
        if "messages" in v:
            v = v["messages"]
        elif "content" in v:
            v = v["content"]
    if isinstance(v, list) and v and isinstance(v[-1], dict):
        v = v[-1].get("content", v[-1])
    s = v if isinstance(v, str) else str(v)
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


class TTLCache:
    def __init__(self) -> None:
        self._d: dict[Any, tuple[float, Any]] = {}
        self._locks: dict[Any, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def get(self, key: Any, ttl: float, fn):
        hit = self._d.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        async with self._locks[key]:
            hit = self._d.get(key)
            if hit and time.time() - hit[0] < ttl:
                return hit[1]
            val = await fn()
            self._d[key] = (time.time(), val)
            if len(self._d) > 256:  # drop stale keys from old buckets
                cutoff = time.time() - 600
                self._d = {k: x for k, x in self._d.items() if x[0] > cutoff}
            return val


async def probe(name: str, fn) -> dict:
    t0 = time.perf_counter()
    try:
        detail, ok, err = await fn(), True, None
    except Exception as e:  # noqa: BLE001 - any failure means "down"
        detail, ok, err = {}, False, f"{type(e).__name__}: {e}"[:240]
    return {"name": name, "ok": ok, "ms": round((time.perf_counter() - t0) * 1000, 1), "error": err, "detail": detail}


def load_config_routes(path: str) -> dict[str, str]:
    try:
        with open(path) as f:
            cfg = yaml.safe_load(f) or {}
        return {m["model_name"]: m["litellm_params"]["model"] for m in cfg.get("model_list", [])}
    except (OSError, KeyError, TypeError, yaml.YAMLError):
        return {}


# --------------------------------------------------------------------------- SQL

FUNNEL_SQL = """
with p as (select * from harness.probes where created_at >= %(since)s),
     v as (select v.* from harness.verdicts v join p on p.id = v.probe_id)
select
  (select count(*) from harness.operations where created_at >= %(since)s) as operations,
  (select count(*) from p) as planned,
  (select count(distinct e.probe_id) from harness.exchanges e join p on p.id = e.probe_id) as executed,
  (select count(*) from p where interesting) as interesting,
  (select count(distinct probe_id) from v) as judged,
  (select count(*) from v where label = 'confirmed') as confirmed,
  (select count(*) from v where label = 'confirmed' and evidence_ref is not null) as evidenced,
  (select count(*) from v where label = 'confirmed' and evidence_ref is not null and not is_duplicate) as unique_findings,
  (select count(*) from v where label = 'confirmed' and evidence_ref is null) as confirmed_no_evidence,
  (select count(*) from v where label = 'rejected') as rejected,
  (select count(*) from v where label = 'needs_review') as needs_review
"""

RUNS_SQL = """
select r.id, r.target, r.program, r.status,
  extract(epoch from r.started_at)::float as started_at,
  extract(epoch from r.ended_at)::float as ended_at,
  (select count(*) from harness.operations o where o.run_id = r.id) as operations,
  (select count(*) from harness.probes p join harness.operations o on o.id = p.op_id
     where o.run_id = r.id) as probes,
  (select count(*) from harness.verdicts v join harness.probes p on p.id = v.probe_id
     join harness.operations o on o.id = p.op_id
     where o.run_id = r.id and v.label = 'confirmed' and v.evidence_ref is not null
       and not v.is_duplicate) as findings
from harness.runs r where r.started_at >= %(since)s
order by r.started_at desc limit 50
"""

FINDINGS_TS_SQL = """
select extract(epoch from judged_at)::float as t from harness.verdicts
where judged_at >= %(since)s and label = 'confirmed' and evidence_ref is not null and not is_duplicate
limit 20000
"""

GUARD_TS_SQL = """
select extract(epoch from ts)::float as t, kind from harness.guardrail_events
where ts >= %(since)s limit 20000
"""

GUARD_RECENT_SQL = """
select extract(epoch from ts)::float as t, kind, host, detail, run_id from harness.guardrail_events
where ts >= %(since)s order by ts desc limit 25
"""

KILL_SQL = """
select detail, extract(epoch from ts)::float as t from harness.guardrail_events
where kind = 'kill_switch' order by ts desc limit 1
"""


# --------------------------------------------------------------------------- backend


class Backend:
    demo = False

    def __init__(self) -> None:
        self.litellm = _env("LITELLM_URL", "http://litellm:4000").rstrip("/")
        self.litellm_key = _env("LITELLM_MASTER_KEY")
        self.langfuse = _env("LANGFUSE_HOST", "http://langfuse:3000").rstrip("/")
        self.lf_auth = (_env("LANGFUSE_PUBLIC_KEY"), _env("LANGFUSE_SECRET_KEY"))
        self.langfuse_ui = _env("LANGFUSE_UI_URL", self.langfuse).rstrip("/")
        self.lf_project = _env("LANGFUSE_PROJECT_ID", "harness")
        self.qdrant = _env("QDRANT_URL", "http://qdrant:6333").rstrip("/")
        self.ollama = _env("OLLAMA_URL", "http://host.docker.internal:11434").rstrip("/")
        self.db_url = _env("DASHBOARD_DATABASE_URL")
        self.obs_cap = int(_env("DASHBOARD_OBS_CAP", "5000"))
        self.trace_cap = int(_env("DASHBOARD_TRACE_CAP", "5000"))
        self.http = httpx.AsyncClient(timeout=10)
        self.routes: dict[str, str] = load_config_routes(_env("LITELLM_CONFIG", "litellm.config.yaml"))
        self.cache = TTLCache()
        self.history: dict[str, deque] = defaultdict(lambda: deque(maxlen=HISTORY_SAMPLES))
        self.snapshot: dict | None = None

    async def close(self) -> None:
        await self.http.aclose()

    @property
    def lf_ready(self) -> bool:
        return all(self.lf_auth)

    def meta(self) -> dict:
        return {
            "demo": self.demo,
            "langfuse_ui": self.langfuse_ui,
            "langfuse_project": self.lf_project,
            "routes": self.route_order(),
            "ranges": list(RANGES),
        }

    # ---- routes / tiers ------------------------------------------------------

    def route_order(self) -> list[str]:
        extra = sorted(r for r in self.routes if r not in TIERS)
        return TIERS + extra + ["other"]

    def route_of(self, model: str | None, metadata: Any = None) -> str:
        md = metadata if isinstance(metadata, dict) else {}
        for k in ("model_group", "litellm_model_group", "route"):
            g = md.get(k)
            if isinstance(g, str) and g in self.routes:
                return g
        m = (model or "").lower()
        if not m:
            return "other"
        if m in self.routes:
            return m
        for route, under in self.routes.items():
            u = under.lower()
            bare = u.split("/", 1)[-1]
            if m in (u, bare) or u.endswith("/" + m) or m.endswith("/" + bare):
                return route
        return "other"

    async def refresh_routes(self) -> None:
        if not self.litellm_key:
            return
        r = await self.http.get(f"{self.litellm}/model/info", headers={"Authorization": f"Bearer {self.litellm_key}"})
        r.raise_for_status()
        routes = {m["model_name"]: m.get("litellm_params", {}).get("model", "") for m in r.json().get("data", [])}
        if routes:
            self.routes = routes

    def route_table(self) -> list[dict]:
        return [{"route": r, "model": m, "placeholder": "<" in m or not m} for r, m in self.routes.items()]

    # ---- health probes -------------------------------------------------------

    async def _litellm(self) -> dict:
        (await self.http.get(f"{self.litellm}/health/liveliness")).raise_for_status()
        d: dict = {}
        try:
            rd = (await self.http.get(f"{self.litellm}/health/readiness")).json()
            cbs = rd.get("success_callbacks") or []
            d = {"version": rd.get("litellm_version"), "langfuse_callback": any("langfuse" in str(c).lower() for c in cbs)}
        except (httpx.HTTPError, ValueError):
            pass
        try:
            await self.refresh_routes()
        except (httpx.HTTPError, ValueError, KeyError) as e:
            d["routes_error"] = str(e)[:160]
        d["routes"] = len(self.routes)
        return d

    async def _ollama(self) -> dict:
        v = (await self.http.get(f"{self.ollama}/api/version")).raise_for_status().json()
        ps = (await self.http.get(f"{self.ollama}/api/ps")).raise_for_status().json().get("models") or []
        tags = (await self.http.get(f"{self.ollama}/api/tags")).raise_for_status().json().get("models") or []
        loaded = []
        for m in ps:
            size, vram = m.get("size") or 0, m.get("size_vram") or 0
            loaded.append({
                "name": m.get("name"), "size": size, "size_vram": vram,
                "gpu_pct": round(100 * vram / size) if size else None,
                "expires_at": parse_ts(m.get("expires_at")),
            })
        installed = [{
            "name": m.get("name"), "size": m.get("size"),
            "family": (m.get("details") or {}).get("family"),
            "params": (m.get("details") or {}).get("parameter_size"),
            "quant": (m.get("details") or {}).get("quantization_level"),
            "modified_at": parse_ts(m.get("modified_at")),
        } for m in tags]
        return {"version": v.get("version"), "loaded": loaded, "installed": installed}

    async def _qdrant(self) -> dict:
        (await self.http.get(f"{self.qdrant}/healthz")).raise_for_status()
        root = (await self.http.get(f"{self.qdrant}/")).json()
        cols = (await self.http.get(f"{self.qdrant}/collections")).raise_for_status().json()["result"]["collections"]
        out = []
        for c in cols:
            info = (await self.http.get(f"{self.qdrant}/collections/{quote(c['name'], safe='')}")).json().get("result", {})
            vec = ((info.get("config") or {}).get("params") or {}).get("vectors") or {}
            if "size" in vec:
                vec = {"": vec}
            out.append({
                "name": c["name"], "status": info.get("status"),
                "points": info.get("points_count"), "indexed": info.get("indexed_vectors_count"),
                "segments": info.get("segments_count"),
                "vectors": ", ".join(f"{k or 'default'}: {v.get('size')}d {v.get('distance', '')}".strip()
                                     for k, v in vec.items() if isinstance(v, dict)),
            })
        return {"version": root.get("version"), "collections": out}

    async def _langfuse(self) -> dict:
        h = (await self.http.get(f"{self.langfuse}/api/public/health")).raise_for_status().json()
        d: dict = {"version": h.get("version")}
        if not self.lf_ready:
            d["api_error"] = "LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY not set"
            return d
        try:
            j = await self.lf_get("/api/public/traces", limit=1, page=1)
            data = j.get("data") or []
            d["last_trace_at"] = parse_ts(data[0].get("timestamp")) if data else None
            d["traces_total"] = (j.get("meta") or {}).get("totalItems")
        except (httpx.HTTPError, ValueError) as e:
            d["api_error"] = str(e)[:200]
        return d

    async def _postgres(self) -> dict:
        if not self.db_url:
            raise RuntimeError("DASHBOARD_DATABASE_URL not set")
        async with await psycopg.AsyncConnection.connect(self.db_url, connect_timeout=5, row_factory=dict_row) as c:
            r = await (await c.execute("""
                select current_setting('server_version') as version,
                       pg_database_size(current_database()) as size,
                       (select numbackends from pg_stat_database where datname = current_database()) as conns,
                       current_setting('max_connections')::int as max_conns,
                       to_regclass('harness.verdicts') is not null as harness
            """)).fetchone()
        return dict(r)

    async def poll_once(self) -> None:
        checks = {"litellm": self._litellm, "ollama": self._ollama, "qdrant": self._qdrant,
                  "langfuse": self._langfuse, "postgres": self._postgres}
        results = await asyncio.gather(*(probe(n, f) for n, f in checks.items()))
        now = time.time()
        for r in results:
            self.history[r["name"]].append((now, r["ok"], r["ms"]))
        u = shutil.disk_usage("/")
        disk = {"total": u.total, "used": u.used, "free": u.free, "free_pct": round(100 * u.free / u.total, 1)}
        self.snapshot = {"at": now, "services": results, "disk": disk}

    async def poll_forever(self) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception:  # noqa: BLE001 - keep polling whatever happens
                log.exception("poll failed")
            await asyncio.sleep(POLL_SECONDS)

    def _history_buckets(self, name: str, now: float, n: int = 96, step: int = 900) -> list[dict]:
        start = now - n * step
        acc = [[0, 0, 0.0] for _ in range(n)]  # samples, up, ms sum
        for t, ok, ms in self.history[name]:
            i = int((t - start) // step)
            if 0 <= i < n:
                acc[i][0] += 1
                acc[i][1] += 1 if ok else 0
                acc[i][2] += ms
        return [{"t": round(start + i * step), "up": (u / s) if s else None, "ms": round(m / s, 1) if s else None}
                for i, (s, u, m) in enumerate(acc)]

    @staticmethod
    def _service_status(s: dict) -> tuple[str, list[str]]:
        if not s["ok"]:
            return "critical", [s.get("error") or "unreachable"]
        reasons, d = [], s.get("detail") or {}
        if s["ms"] > 2000:
            reasons.append(f"slow probe ({s['ms']:.0f} ms)")
        if s["name"] == "ollama":
            for m in d.get("loaded", []):
                if m.get("gpu_pct") is not None and m["gpu_pct"] < 100:
                    reasons.append(f"{m['name']} only {m['gpu_pct']}% in VRAM (CPU fallback)")
        if s["name"] == "langfuse" and d.get("api_error"):
            reasons.append(f"API: {d['api_error']}")
        if s["name"] == "postgres" and d.get("max_conns") and d.get("conns", 0) > 0.8 * d["max_conns"]:
            reasons.append(f"{d['conns']}/{d['max_conns']} connections")
        if s["name"] == "litellm" and d.get("routes_error"):
            reasons.append(f"route list: {d['routes_error']}")
        return ("warning" if reasons else "good"), reasons

    async def health(self) -> dict:
        if not self.snapshot:
            await self.poll_once()
        snap = self.snapshot
        now = snap["at"]
        services = []
        for s in snap["services"]:
            hist = self.history[s["name"]]
            status, reasons = self._service_status(s)
            services.append({
                **s, "status": status, "reasons": reasons,
                "uptime": (sum(1 for _, ok, _ in hist if ok) / len(hist)) if hist else None,
                "samples": len(hist),
                "history": self._history_buckets(s["name"], now),
            })
        return {"at": now, "services": services, "disk": snap["disk"], "routes": self.route_table(),
                "checks": await self._checks(services, snap["disk"])}

    async def _checks(self, services: list[dict], disk: dict) -> list[dict]:
        """Opinionated assertions: the stack's design principles, made checkable."""
        out: list[dict] = []
        by = {s["name"]: s for s in services}

        down = [s["name"] for s in services if not s["ok"]]
        out.append({"id": "reachable", "label": "All services reachable",
                    "status": "critical" if down else "good",
                    "value": f"{len(services) - len(down)}/{len(services)} up",
                    "detail": ("Down: " + ", ".join(down)) if down else "LiteLLM, Ollama, Qdrant, Langfuse and Postgres answered the last probe."})

        bad = [r["route"] for r in self.route_table() if r["placeholder"]]
        out.append({"id": "routes", "label": "Model routes configured",
                    "status": "critical" if bad else ("good" if self.routes else "unknown"),
                    "value": f"{len(self.routes)} routes",
                    "detail": (f"{', '.join(bad)} points at a placeholder model; calls to it will fail. Fix litellm.config.yaml." if bad
                               else "Every route in litellm.config.yaml names a concrete model.")})

        ol = by.get("ollama", {})
        loaded = (ol.get("detail") or {}).get("loaded") or []
        if not ol.get("ok"):
            g = {"status": "unknown", "value": "—", "detail": "Ollama unreachable."}
        elif not loaded:
            g = {"status": "unknown", "value": "cold", "detail": "No model resident; the next glue call pays the load time."}
        else:
            worst = min(loaded, key=lambda m: m.get("gpu_pct") or 0)
            pct = worst.get("gpu_pct") or 0
            g = {"status": "good" if pct >= 100 else ("warning" if pct >= 50 else "critical"),
                 "value": f"{pct}% VRAM",
                 "detail": f"{worst['name']} is {'fully on the 2060' if pct >= 100 else 'partly on CPU: check the CUDA-on-WSL driver or model size'}."}
        out.append({"id": "gpu", "label": "Local models on GPU", **g})

        lf = (by.get("langfuse") or {}).get("detail") or {}
        if lf.get("api_error") or not by.get("langfuse", {}).get("ok"):
            t = {"status": "warning", "value": "no API", "detail": lf.get("api_error") or "Langfuse unreachable; spend is not being recorded."}
        elif not lf.get("last_trace_at"):
            t = {"status": "unknown", "value": "no traces", "detail": "Nothing traced yet. Every LiteLLM call should land here."}
        else:
            t = {"status": "good", "value": f"{lf.get('traces_total') or 0:,} traces",
                 "detail": "If calls run but the last-trace time stops moving, the LiteLLM to Langfuse callback is broken.",
                 "at": lf["last_trace_at"]}
        out.append({"id": "tracing", "label": "Tracing flowing", **t})

        try:
            fl = await asyncio.wait_for(self.flows("7d"), 25)
            split = fl["split"]
            calls = sum(v["calls"] for v in split.values())
            cost = sum(v["cost"] for v in split.values())
            if calls:
                judge_c = split.get("judge", {}).get("calls", 0) / calls
                glue_c = (split.get("glue", {}).get("calls", 0) + split.get("embed", {}).get("calls", 0)) / calls
                judge_s = (split.get("judge", {}).get("cost", 0) / cost) if cost else 0
                st = "good" if judge_c <= 0.10 else ("warning" if judge_c <= 0.25 else "critical")
                out.append({"id": "tiers", "label": "Tier discipline (7d)", "status": st,
                            "value": f"judge {judge_c:.0%} of calls",
                            "detail": f"Local glue/embed carry {glue_c:.0%} of calls; judge takes {judge_s:.0%} of spend. Judge should stay rare (≤10% of calls)."})
            else:
                out.append({"id": "tiers", "label": "Tier discipline (7d)", "status": "unknown", "value": "no calls",
                            "detail": "No LLM calls traced in the last 7 days."})
            h = fl["harness"]
            if h.get("available"):
                f = h["funnel"]
                n = f.get("confirmed_no_evidence", 0)
                out.append({"id": "evidence", "label": "Evidence gate (7d)", "status": "critical" if n else "good",
                            "value": f"{n} unevidenced",
                            "detail": (f"{n} confirmed verdicts have no replayable evidence_ref. They must not become findings." if n
                                       else "Every confirmed verdict has a replayable request/response pair.")})
                g = h["guardrails"]["by_kind"]
                sd = g.get("scope_denied", 0)
                out.append({"id": "scope", "label": "Scope guard (7d)", "status": "warning" if sd else "good",
                            "value": f"{sd} denied",
                            "detail": (f"Allowlist dropped {sd} out-of-scope requests. The guard works, but the planner is straying." if sd
                                       else "No out-of-scope requests attempted.")})
                ks = h.get("kill_switch")
                out.append({"id": "kill", "label": "Kill switch",
                            "status": "unknown" if not ks else ("warning" if ks["state"] == "engaged" else "good"),
                            "value": ks["state"] if ks else "never used",
                            "detail": "Engaged: runners should be halted." if ks and ks["state"] == "engaged"
                            else "Runners free to run."})
            else:
                out.append({"id": "evidence", "label": "Harness findings DB", "status": "unknown", "value": "not set up",
                            "detail": h.get("error") or "Apply db/harness.sql to enable the funnel, evidence and scope checks."})
        except Exception as e:  # noqa: BLE001 - a failed check shouldn't break /health
            out.append({"id": "tiers", "label": "Tier discipline (7d)", "status": "unknown", "value": "—", "detail": str(e)[:200]})

        fp = disk.get("free_pct", 100)
        out.append({"id": "disk", "label": "Disk free (docker volumes)", "status": "good" if fp > 20 else ("warning" if fp > 10 else "critical"),
                    "value": f"{fp:.0f}% free", "detail": "Postgres, Qdrant and Langfuse volumes live on the WSL2 disk."})
        return out

    async def probe_routes(self) -> dict:
        """Calls LiteLLM /health, which sends a tiny real request to every route."""
        r = await self.http.get(f"{self.litellm}/health", headers={"Authorization": f"Bearer {self.litellm_key}"}, timeout=180)
        r.raise_for_status()
        j = r.json()

        def pick(e: dict, ok: bool) -> dict:
            model = e.get("model") or ""
            return {"route": e.get("model_name") or self.route_of(model), "model": model, "ok": ok,
                    "error": None if ok else preview(e.get("error"), 300)}
        return {"at": time.time(),
                "results": [pick(e, True) for e in j.get("healthy_endpoints", [])]
                + [pick(e, False) for e in j.get("unhealthy_endpoints", [])]}

    # ---- langfuse ------------------------------------------------------------

    async def lf_get(self, path: str, **params) -> dict:
        r = await self.http.get(f"{self.langfuse}{path}", params=params, auth=self.lf_auth, timeout=30)
        r.raise_for_status()
        return r.json()

    async def lf_paged(self, path: str, params: dict, cap: int) -> tuple[list[dict], int]:
        first = await self.lf_get(path, **params, page=1, limit=100)
        out = list(first.get("data") or [])
        meta = first.get("meta") or {}
        total = meta.get("totalItems") or len(out)
        pages = min(meta.get("totalPages") or 1, math.ceil(cap / 100))
        sem = asyncio.Semaphore(4)

        async def one(p: int) -> list[dict]:
            async with sem:
                return (await self.lf_get(path, **params, page=p, limit=100)).get("data") or []
        for chunk in await asyncio.gather(*(one(p) for p in range(2, pages + 1))):
            out.extend(chunk)
        return out[:cap], max(total, len(out))

    def norm_obs(self, o: dict) -> dict:
        st, et = parse_ts(o.get("startTime")), parse_ts(o.get("endTime"))
        lat = (et - st) * 1000 if st and et else (o["latency"] * 1000 if isinstance(o.get("latency"), (int, float)) else None)
        usage = o.get("usage") or {}
        md = o.get("metadata")
        return {
            "id": o.get("id"), "trace_id": o.get("traceId"), "ts": st or 0, "name": o.get("name") or "(unnamed)",
            "model": o.get("model") or "", "route": self.route_of(o.get("model"), md),
            "latency_ms": lat,
            "in_tok": usage.get("input") or o.get("promptTokens") or 0,
            "out_tok": usage.get("output") or o.get("completionTokens") or 0,
            "cost": o.get("calculatedTotalCost") or o.get("totalCost") or (o.get("costDetails") or {}).get("total") or 0,
            "level": o.get("level") or "DEFAULT", "status": o.get("statusMessage"),
        }

    async def observations(self, since: float) -> tuple[list[dict], int]:
        async def fetch():
            raw, total = await self.lf_paged("/api/public/observations",
                                             {"type": "GENERATION", "fromStartTime": iso(since)}, self.obs_cap)
            return [self.norm_obs(o) for o in raw], total
        return await self.cache.get(("obs", since), 60, fetch)

    async def traces(self, since: float) -> tuple[list[dict], int]:
        async def fetch():
            raw, total = await self.lf_paged("/api/public/traces", {"fromTimestamp": iso(since)}, self.trace_cap)
            return [{
                "id": t.get("id"), "ts": parse_ts(t.get("timestamp")) or 0, "name": t.get("name") or "(unnamed)",
                "session_id": t.get("sessionId"), "cost": t.get("totalCost") or 0,
                "latency_ms": (t["latency"] * 1000) if isinstance(t.get("latency"), (int, float)) else None,
            } for t in raw], total
        return await self.cache.get(("traces", since), 60, fetch)

    async def daily(self, since: float) -> list[dict]:
        """Exact per-day, per-model usage from Langfuse (not capped like the samples)."""
        async def fetch():
            raw, _ = await self.lf_paged("/api/public/metrics/daily", {"fromTimestamp": iso(since)}, 200)
            rows = []
            for d in raw:
                t = parse_ts(str(d.get("date"))[:10] + "T00:00:00Z")
                if t is None:
                    continue
                usage = d.get("usage") or []
                for u in usage:
                    rows.append({"ts": t, "route": self.route_of(u.get("model")),
                                 "calls": u.get("countObservations") or 0,
                                 "tokens": u.get("totalUsage") or 0, "cost": u.get("totalCost") or 0})
                if not usage and d.get("totalCost"):
                    rows.append({"ts": t, "route": "other", "calls": d.get("countObservations") or 0,
                                 "tokens": 0, "cost": d.get("totalCost") or 0})
            return rows
        return await self.cache.get(("daily", since), 60, fetch)

    # ---- harness DB ----------------------------------------------------------

    async def _db(self):
        return await psycopg.AsyncConnection.connect(self.db_url, connect_timeout=5, row_factory=dict_row)

    async def harness(self, since: float) -> dict:
        async def fetch():
            if not self.db_url:
                return {"available": False, "error": "DASHBOARD_DATABASE_URL not set."}
            try:
                async with await self._db() as c:
                    ok = (await (await c.execute("select to_regclass('harness.verdicts') is not null as ok")).fetchone())["ok"]
                    if not ok:
                        return {"available": False, "error": "Harness schema missing. Apply db/harness.sql (setup-2060.sh does it)."}
                    p = {"since": datetime.fromtimestamp(since, timezone.utc)}

                    async def rows(q: str, params=p) -> list[dict]:
                        return await (await c.execute(q, params)).fetchall()
                    funnel = (await rows(FUNNEL_SQL))[0]
                    findings = [r["t"] for r in await rows(FINDINGS_TS_SQL)]
                    guard_ts = [(r["t"], r["kind"]) for r in await rows(GUARD_TS_SQL)]
                    recent = await rows(GUARD_RECENT_SQL)
                    kill = await rows(KILL_SQL, None)
                    runs = await rows(RUNS_SQL)
            except psycopg.Error as e:
                return {"available": False, "error": f"Postgres: {str(e).strip()[:200]}"}
            return {
                "available": True, "funnel": {k: int(v or 0) for k, v in funnel.items()},
                "findings_ts": findings,
                "guardrails": {"by_kind": dict(Counter(k for _, k in guard_ts)), "ts": guard_ts, "recent": recent},
                "kill_switch": ({"state": "engaged" if (kill[0]["detail"] or "").lower().startswith("engag") else "released",
                                 "at": kill[0]["t"]} if kill else None),
                "runs": runs,
            }
        return await self.cache.get(("harness", since), 30, fetch)

    # ---- aggregations --------------------------------------------------------

    async def trends(self, rng: str) -> dict:
        async def build():
            starts, step = make_buckets(rng)
            since, n = starts[0], len(starts)
            order = self.route_order()
            notes: list[str] = []

            def idx(t: float) -> int | None:
                i = int((t - since) // step)
                return i if 0 <= i < n else None

            def zero():
                return {r: [0.0] * n for r in order}
            calls, tokens, cost, errors = zero(), zero(), zero(), zero()
            lat: dict[str, list[list[float]]] = {r: [[] for _ in range(n)] for r in order}
            lat_all: dict[str, list[float]] = defaultdict(list)
            sample_calls = sample_errors = 0
            obs: list[dict] = []
            try:
                obs, total = await self.observations(since)
                if len(obs) < total:
                    notes.append(f"Latency and error figures use the latest {len(obs):,} of {total:,} generations.")
            except Exception as e:  # noqa: BLE001
                notes.append(f"Langfuse generations unavailable: {str(e)[:160]}")
            for o in obs:
                i = idx(o["ts"])
                if i is None:
                    continue
                r = o["route"] if o["route"] in calls else "other"
                calls[r][i] += 1
                tokens[r][i] += o["in_tok"] + o["out_tok"]
                cost[r][i] += o["cost"]
                sample_calls += 1
                if o["level"] == "ERROR":
                    errors[r][i] += 1
                    sample_errors += 1
                if o["latency_ms"] is not None:
                    lat[r][i].append(o["latency_ms"])
                    lat_all[r].append(o["latency_ms"])
            if step >= 86400:
                try:
                    daily = await self.daily(since)
                    if daily:
                        calls, tokens, cost = zero(), zero(), zero()
                        for d in daily:
                            i = idx(d["ts"])
                            if i is None:
                                continue
                            r = d["route"] if d["route"] in calls else "other"
                            calls[r][i] += d["calls"]
                            tokens[r][i] += d["tokens"]
                            cost[r][i] += d["cost"]
                except Exception as e:  # noqa: BLE001
                    notes.append(f"Langfuse daily metrics unavailable, using samples: {str(e)[:120]}")

            h = await self.harness(since)
            findings = [0] * n
            for t in h.get("findings_ts", []) if h.get("available") else []:
                i = idx(t)
                if i is not None:
                    findings[i] += 1

            total_cost = sum(sum(v) for v in cost.values())
            n_find = sum(findings)
            return {
                "range": rng, "step": step, "buckets": starts, "routes": order,
                "calls": calls, "tokens": tokens, "cost": cost, "errors": errors,
                "p50": {r: [percentile(b, .5) for b in lat[r]] for r in order},
                "p95": {r: [percentile(b, .95) for b in lat[r]] for r in order},
                "findings": findings, "harness": h.get("available", False),
                "totals": {
                    "calls": sum(sum(v) for v in calls.values()),
                    "tokens": sum(sum(v) for v in tokens.values()),
                    "cost": total_cost,
                    "error_rate": (sample_errors / sample_calls) if sample_calls else None,
                    "errors": sample_errors,
                    "p95": {r: percentile(v, .95) for r, v in lat_all.items()},
                    "findings": n_find if h.get("available") else None,
                    "cost_per_finding": (total_cost / n_find) if n_find else None,
                },
                "notes": notes,
            }
        return await self.cache.get(("trends", rng, make_buckets(rng)[0][0]), 30, build)

    async def flows(self, rng: str) -> dict:
        async def build():
            tr = await self.trends(rng)
            since = tr["buckets"][0]
            notes = list(tr["notes"])
            split = {r: {"calls": sum(tr["calls"][r]), "tokens": sum(tr["tokens"][r]), "cost": sum(tr["cost"][r])}
                     for r in tr["routes"]}

            steps: dict[str, dict] = {}
            try:
                obs, _ = await self.observations(since)
            except Exception:  # noqa: BLE001 - already noted by trends
                obs = []
            lats: dict[str, list[float]] = defaultdict(list)
            for o in obs:
                s = steps.setdefault(o["name"], {"name": o["name"], "calls": 0, "cost": 0.0, "tokens": 0, "errors": 0, "routes": Counter()})
                s["calls"] += 1
                s["cost"] += o["cost"]
                s["tokens"] += o["in_tok"] + o["out_tok"]
                s["errors"] += o["level"] == "ERROR"
                s["routes"][o["route"]] += 1
                if o["latency_ms"] is not None:
                    lats[o["name"]].append(o["latency_ms"])
            step_rows = []
            for name, s in steps.items():
                step_rows.append({**s, "route": s["routes"].most_common(1)[0][0], "routes": dict(s["routes"]),
                                  "p50": percentile(lats[name], .5), "p95": percentile(lats[name], .95)})
            step_rows.sort(key=lambda s: (-s["cost"], -s["calls"]))

            sessions: dict[str, dict] = {}
            try:
                trs, ttotal = await self.traces(since)
                if len(trs) < ttotal:
                    notes.append(f"Per-run spend uses the latest {len(trs):,} of {ttotal:,} traces.")
                for t in trs:
                    sid = t["session_id"]
                    if not sid:
                        continue
                    s = sessions.setdefault(sid, {"traces": 0, "cost": 0.0, "first": t["ts"], "last": t["ts"]})
                    s["traces"] += 1
                    s["cost"] += t["cost"]
                    s["first"], s["last"] = min(s["first"], t["ts"]), max(s["last"], t["ts"])
            except Exception as e:  # noqa: BLE001
                notes.append(f"Langfuse traces unavailable: {str(e)[:160]}")

            h = await self.harness(since)
            runs = []
            seen = set()
            for r in h.get("runs", []) if h.get("available") else []:
                s = sessions.get(r["id"], {})
                seen.add(r["id"])
                runs.append({**r, "tracked": True, "traces": s.get("traces", 0), "cost": s.get("cost", 0.0),
                             "cost_per_finding": (s.get("cost", 0) / r["findings"]) if r["findings"] else None})
            for sid, s in sorted(sessions.items(), key=lambda kv: -kv[1]["last"]):
                if sid in seen or len(runs) >= 60:
                    continue
                runs.append({"id": sid, "target": None, "program": None, "status": "untracked", "tracked": False,
                             "started_at": s["first"], "ended_at": s["last"], "operations": None, "probes": None,
                             "findings": None, "traces": s["traces"], "cost": s["cost"], "cost_per_finding": None})
            runs.sort(key=lambda r: -(r["started_at"] or 0))

            guard_series = None
            if h.get("available"):
                n, step = len(tr["buckets"]), tr["step"]
                kinds = ["scope_denied", "rate_limited", "kill_switch"]
                guard_series = {k: [0] * n for k in kinds}
                for t, k in h["guardrails"]["ts"]:
                    i = int((t - since) // step)
                    if 0 <= i < n and k in guard_series:
                        guard_series[k][i] += 1
            return {
                "range": rng, "buckets": tr["buckets"], "step": tr["step"], "routes": tr["routes"],
                "split": split, "steps": step_rows[:30], "runs": runs,
                "harness": {k: v for k, v in h.items() if k not in ("findings_ts",)} | (
                    {"guardrails": {**h["guardrails"], "ts": None, "series": guard_series}} if h.get("available") else {}),
                "notes": notes,
            }
        return await self.cache.get(("flows", rng, make_buckets(rng)[0][0]), 30, build)

    # ---- raw -----------------------------------------------------------------

    def _lf_link(self, trace_id: str | None) -> str | None:
        return f"{self.langfuse_ui}/project/{self.lf_project}/traces/{trace_id}" if trace_id else None

    async def raw_generations(self, rng: str, page: int) -> dict:
        since = make_buckets(rng)[0][0]
        j = await self.lf_get("/api/public/observations", type="GENERATION", fromStartTime=iso(since), page=page, limit=50)
        rows = [{**self.norm_obs(o), "input": preview(o.get("input")), "output": preview(o.get("output")),
                 "link": self._lf_link(o.get("traceId")), "raw": clean(o)} for o in j.get("data") or []]
        meta = j.get("meta") or {}
        return {"rows": rows, "page": page, "pages": meta.get("totalPages") or 1, "total": meta.get("totalItems")}

    async def raw_traces(self, rng: str, page: int) -> dict:
        since = make_buckets(rng)[0][0]
        j = await self.lf_get("/api/public/traces", fromTimestamp=iso(since), page=page, limit=50)
        rows = [{"id": t.get("id"), "ts": parse_ts(t.get("timestamp")), "name": t.get("name"),
                 "session_id": t.get("sessionId"), "tags": t.get("tags") or [],
                 "latency_ms": (t["latency"] * 1000) if isinstance(t.get("latency"), (int, float)) else None,
                 "cost": t.get("totalCost") or 0, "observations": len(t.get("observations") or []),
                 "input": preview(t.get("input")), "link": self._lf_link(t.get("id")), "raw": clean(t)}
                for t in j.get("data") or []]
        meta = j.get("meta") or {}
        return {"rows": rows, "page": page, "pages": meta.get("totalPages") or 1, "total": meta.get("totalItems")}

    async def raw_tables(self) -> dict:
        if not self.db_url:
            return {"available": False, "error": "DASHBOARD_DATABASE_URL not set."}
        async with await self._db() as c:
            have = {r["t"] for r in await (await c.execute(
                "select table_name as t from information_schema.tables where table_schema = 'harness'")).fetchall()}
            out = []
            for t in HARNESS_TABLES:
                if t in have:
                    n = (await (await c.execute(sql.SQL("select count(*) as n from harness.{}").format(sql.Identifier(t)))).fetchone())["n"]
                    out.append({"name": t, "rows": n})
        if not out:
            return {"available": False, "error": "Harness schema missing. Apply db/harness.sql."}
        return {"available": True, "tables": out}

    async def raw_table(self, name: str, page: int) -> dict:
        if name not in HARNESS_TABLES:
            raise ValueError(f"unknown table {name!r}")
        size = 50
        async with await self._db() as c:
            cur = await c.execute(
                sql.SQL("select * from harness.{} order by {} desc limit %s offset %s").format(
                    sql.Identifier(name), sql.Identifier(HARNESS_TABLES[name])),
                (size + 1, (page - 1) * size))
            rows = await cur.fetchall()
            cols = [d.name for d in cur.description or []]
        return {"columns": cols, "rows": [clean(r, 600) for r in rows[:size]], "page": page, "more": len(rows) > size}

    async def raw_qdrant(self, name: str | None, offset: Any = None) -> dict:
        if not name:
            return {"collections": (await self._qdrant())["collections"]}
        body: dict = {"limit": 50, "with_payload": True, "with_vector": False}
        if offset is not None:
            body["offset"] = offset
        r = await self.http.post(f"{self.qdrant}/collections/{quote(name, safe='')}/points/scroll", json=body)
        r.raise_for_status()
        res = r.json().get("result") or {}
        return {"points": [clean({"id": p.get("id"), "payload": p.get("payload")}, 600) for p in res.get("points") or []],
                "next": res.get("next_page_offset")}

    async def raw_sql(self, query: str) -> dict:
        q = query.strip().rstrip(";").strip()
        if not q:
            raise ValueError("empty query")
        if ";" in q:
            raise ValueError("one statement at a time (no ';')")
        if not self.db_url:
            raise ValueError("DASHBOARD_DATABASE_URL not set")
        t0 = time.perf_counter()
        async with await psycopg.AsyncConnection.connect(self.db_url, connect_timeout=5) as c:
            await c.execute("set transaction read only")
            await c.execute("set local statement_timeout = '5s'")
            cur = await c.execute(q, prepare=False)
            if cur.description is None:
                await c.rollback()
                return {"columns": [], "rows": [], "status": cur.statusmessage, "ms": 0}
            rows = await cur.fetchmany(501)
            cols = [d.name for d in cur.description]
            await c.rollback()
        return {"columns": cols, "rows": [clean(list(r), 2000) for r in rows[:500]], "truncated": len(rows) > 500,
                "ms": round((time.perf_counter() - t0) * 1000, 1)}
