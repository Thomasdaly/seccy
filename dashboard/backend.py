"""Data access + aggregation for the harness dashboard.

Every upstream is read-only: LiteLLM (routes, liveness), Ollama (models, VRAM),
Qdrant (collections), Langfuse v4 public API (v2 metrics for exact aggregates,
v2 observations for raw rows), Langfuse's backing stores (liveness only) and
Postgres as the `dashboard_ro` role (harness schema, see db/harness.sql).
"""

from __future__ import annotations

import asyncio
import json
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
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row, tuple_row

from lab import LabIndex

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
GENERATION_TYPES = ["GENERATION", "EMBEDDING"]
GEN_FILTER = [{"column": "type", "operator": "any of", "value": GENERATION_TYPES, "type": "stringOptions"}]
ERROR_FILTER = [{"column": "level", "operator": "=", "value": "ERROR", "type": "string"}]
USAGE_METRICS = [("count", "count"), ("totalTokens", "sum"), ("totalCost", "sum"), ("latency", "p50"), ("latency", "p95")]
GRANULARITY = {3600: "hour", 86400: "day", 7 * 86400: "week"}
PIPELINE = ["langfuse-worker", "clickhouse", "redis", "minio"]  # Langfuse v4 backing services
HISTORY_SAMPLES = 2880  # 24h at 30s
DEFAULT_LAB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "lab", "ground-truth")
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
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()


def num(v: Any) -> float:
    """Langfuse metrics can return numbers as strings (ClickHouse decimals)."""
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def err_text(e: Exception) -> str:
    """Write diagnostics without forwarding upstream text or URLs."""
    if isinstance(e, httpx.HTTPStatusError):
        return f"Upstream responded with HTTP {e.response.status_code}"
    return type(e).__name__


def opt_num(v: Any) -> float | None:
    return None if v is None or v == "" else num(v)


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
    # weekly buckets start Monday 00:00 UTC (epoch day 0 was a Thursday) to match Langfuse
    offset = 4 * 86400 if step == 7 * 86400 else 0
    end = (math.floor((now - offset) / step) + 1) * step + offset
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
        detail, ok, err = {}, False, err_text(e)
    return {"name": name, "ok": ok, "ms": round((time.perf_counter() - t0) * 1000, 1), "error": err, "detail": detail}


def _bare(model: str) -> str:
    return model.split("/", 1)[-1].removesuffix(":latest")


def _cpu_by_design(model: str) -> bool:
    """setup-2060.sh builds CPU-only copies (num_gpu 0) named *-cpu."""
    return _bare(model).split(":")[0].endswith("-cpu")


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
order by r.started_at desc limit %(limit)s
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
select stopped, extract(epoch from clock_timestamp())::float as checked_at
from harness_guardrails.dispatch_policy where singleton
"""


# Confirmed + needs-review verdicts with their operation, probe, run and latest exchange.
VERDICTS_SQL = """
select v.id, extract(epoch from v.judged_at)::float as judged_at, v.label, v.severity, v.is_duplicate,
  v.evidence_ref, v.trace_id, o.run_id, r.target, r.status as run_status, o.method, o.path,
  p.id as probe_id, p.identity, p.tenant, p.role, p.intent,
  e.status_code, e.request_blob, e.response_blob
from harness.verdicts v
join harness.probes p on p.id = v.probe_id
join harness.operations o on o.id = p.op_id
left join harness.runs r on r.id = o.run_id
left join lateral (select x.status_code, x.request_blob, x.response_blob from harness.exchanges x
                   where x.probe_id = p.id order by x.id desc limit 1) e on true
where {where}
order by v.judged_at desc limit 5000
"""

RUN_SQL = """
select r.id, r.target, r.program, r.status,
  extract(epoch from r.started_at)::float as started_at,
  extract(epoch from r.ended_at)::float as ended_at,
  (select count(*) from harness.operations o where o.run_id = r.id) as operations,
  (select count(*) from harness.probes p join harness.operations o on o.id = p.op_id where o.run_id = r.id) as probes,
  (select count(*) from harness.probes p join harness.operations o on o.id = p.op_id
     where o.run_id = r.id and p.interesting) as interesting,
  (select count(distinct e.probe_id) from harness.exchanges e join harness.probes p on p.id = e.probe_id
     join harness.operations o on o.id = p.op_id where o.run_id = r.id) as executed
from harness.runs r where r.id = %(id)s
"""

RUN_GUARD_SQL = """
select extract(epoch from ts)::float as t, kind, host, detail from harness.guardrail_events
where run_id = %(id)s order by ts desc limit 200
"""


def finding_key(v: dict) -> bool:
    """A finding is confirmed + evidenced + not a duplicate (db/harness.sql contract)."""
    return v.get("label") == "confirmed" and bool(v.get("evidence_ref")) and not v.get("is_duplicate")


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
        if not self.db_url and _env("DASHBOARD_DB_HOST"):
            self.db_url = make_conninfo(
                host=_env("DASHBOARD_DB_HOST"), dbname="postgres",
                user="dashboard_ro", password=_env("DASHBOARD_DB_PASSWORD"),
            )
        # Langfuse v4 pipeline: only probed when configured (they're unpublished, compose-network only)
        self.extra_probes = {k: _env(f"PROBE_{k.upper().replace('-', '_')}") for k in PIPELINE}
        self.http = httpx.AsyncClient(timeout=10, trust_env=False)
        self.routes: dict[str, str] = load_config_routes(_env("LITELLM_CONFIG", "litellm.config.yaml"))
        self.cache = TTLCache()
        self.history: dict[str, deque] = defaultdict(lambda: deque(maxlen=HISTORY_SAMPLES))
        self.snapshot: dict | None = None
        self.budget_24h = opt_num(_env("DASHBOARD_BUDGET_24H_USD") or None)
        self.lab_dir = _env("LAB_GROUND_TRUTH", DEFAULT_LAB_DIR)
        self.tailnet_ip = _env("TAILNET_IP")
        self._lab: LabIndex | None = None
        self._lab_stamp: tuple | None = None

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
            "lab_targets": sorted(self.lab.targets),
        }

    @property
    def lab(self) -> LabIndex:
        """Ground truth, re-read when a file under LAB_GROUND_TRUTH changes."""
        try:
            stamp = tuple(sorted((e.name, e.stat().st_mtime) for e in os.scandir(self.lab_dir)
                                 if e.name.endswith((".yaml", ".yml"))))
        except OSError:
            stamp = ()
        if self._lab is None or stamp != self._lab_stamp:
            self._lab, self._lab_stamp = LabIndex(self.lab_dir if stamp else None, self.tailnet_ip), stamp
        return self._lab

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
            d = {"version": rd.get("litellm_version")}
            if "success_callbacks" in rd:  # only some LiteLLM builds report callbacks here
                d["langfuse_callback"] = any("langfuse" in str(c).lower() for c in rd["success_callbacks"] or [])
        except (httpx.HTTPError, ValueError):
            pass
        try:
            await self.refresh_routes()
        except (httpx.HTTPError, ValueError, KeyError) as e:
            d["routes_error"] = err_text(e)
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
        now = time.time()
        try:
            j = await self.lf_get("/api/public/v2/observations", fromStartTime=iso(now - 30 * 86400),
                                  toStartTime=iso(now + 60), limit=1, fields="core")
            data = j.get("data") or []
            d["last_obs_at"] = parse_ts(data[0].get("startTime")) if data else None
            rows = await self.lf_metrics(now - 86400, now, [], [("count", "count"), ("totalCost", "sum")])
            d["gens_24h"] = int(num(rows[0].get("count_count"))) if rows else 0
            d["cost_24h"] = num(rows[0].get("sum_totalCost")) if rows else 0.0
        except (httpx.HTTPError, ValueError) as e:
            d["api_error"] = err_text(e)
        return d

    async def _pipeline(self, name: str) -> dict:
        """Liveness of Langfuse v4's worker and stores. No credentials needed."""
        target = self.extra_probes[name]
        if name == "redis":
            host, _, port = target.rpartition(":")
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, int(port)), 5)
            try:
                writer.write(b"PING\r\n")
                await writer.drain()
                line = (await asyncio.wait_for(reader.readline(), 5)).decode(errors="replace").strip()
            finally:
                writer.close()
            if not (line == "+PONG" or line.startswith("-NOAUTH")):  # NOAUTH still proves a live server
                raise RuntimeError(f"unexpected reply {line[:40]!r}")
            return {"reply": "PONG" if line == "+PONG" else "auth required (alive)"}
        path = {"clickhouse": "/ping", "minio": "/minio/health/live", "langfuse-worker": "/api/health"}[name]
        r = await self.http.get(target.rstrip("/") + path, timeout=5)
        if r.status_code >= 500:
            r.raise_for_status()
        return {"http": r.status_code}

    async def _postgres(self) -> dict:
        if not self.db_url:
            raise RuntimeError("DASHBOARD_DATABASE_URL not set")
        async with await self._db() as c:
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
        for name, target in self.extra_probes.items():
            if target:
                checks[name] = (lambda n=name: self._pipeline(n))
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
                if m.get("gpu_pct") == 0 and not _cpu_by_design(m["name"]):
                    reasons.append(f"{m['name']} running entirely on CPU (CUDA fallback?)")
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
                    "detail": ("Down: " + ", ".join(down)) if down else "Every service answered the last probe."})

        bad = [r["route"] for r in self.route_table() if r["placeholder"]]
        out.append({"id": "routes", "label": "Model routes configured",
                    "status": "critical" if bad else ("good" if self.routes else "unknown"),
                    "value": f"{len(self.routes)} routes",
                    "detail": (f"{', '.join(bad)} points at a placeholder model; calls to it will fail. Fix litellm.config.yaml." if bad
                               else "Every route in litellm.config.yaml names a concrete model.")})

        ol = by.get("ollama", {})
        loaded = {_bare(m["name"]): m for m in (ol.get("detail") or {}).get("loaded") or []}
        local = {r: _bare(m) for r, m in self.routes.items() if m.startswith("ollama/")}
        if not ol.get("ok"):
            g = {"status": "unknown", "value": "—", "detail": "Ollama unreachable."}
        elif not local:
            g = {"status": "unknown", "value": "no local routes", "detail": "No ollama/ routes in litellm.config.yaml."}
        else:
            parts, worst = [], "good"
            rank = {"good": 0, "warning": 1, "critical": 2}
            for route, model in local.items():
                m = loaded.get(model)
                if not m:
                    st, txt = "warning", f"{route} ({model}) not resident: evicted or Ollama restarted (keep_alive should hold it)"
                elif _cpu_by_design(model):
                    st, txt = "good", f"{route} on CPU by design"
                elif (m.get("gpu_pct") or 0) == 0:
                    st, txt = "critical", f"{route} ({model}) fully on CPU: CUDA-on-WSL driver missing?"
                elif m["gpu_pct"] < 50:
                    st, txt = "warning", f"{route} only {m['gpu_pct']}% in VRAM: slow; another model may be crowding the 6 GB card"
                else:
                    st, txt = "good", f"{route} {m['gpu_pct']}% in VRAM" + (" (partial offload expected on 6 GB)" if m["gpu_pct"] < 100 else "")
                parts.append(txt)
                worst = st if rank[st] > rank[worst] else worst
            gpu = [loaded[m]["gpu_pct"] for r, m in local.items() if m in loaded and not _cpu_by_design(m)]
            g = {"status": worst, "value": f"{min(gpu)}% VRAM" if gpu else f"{len(loaded)} resident",
                 "detail": "; ".join(parts) + "."}
        out.append({"id": "gpu", "label": "Local models resident", **g})

        lf = (by.get("langfuse") or {}).get("detail") or {}
        if lf.get("api_error") or not by.get("langfuse", {}).get("ok"):
            t = {"status": "warning", "value": "no API", "detail": lf.get("api_error") or "Langfuse unreachable; spend is not being recorded."}
        elif not lf.get("last_obs_at"):
            t = {"status": "unknown", "value": "no traces", "detail": "Nothing traced in 30 days. Every LiteLLM call should land here over OTLP."}
        else:
            stalled = [n for n in ("langfuse-worker", "clickhouse") if n in by and not by[n]["ok"]]
            t = {"status": "warning" if stalled else "good", "value": f"{lf.get('gens_24h', 0):,} calls / 24h",
                 "detail": (f"{' and '.join(stalled)} down: new traces are queued but not processed. " if stalled else "")
                 + "If calls run but the last-trace time stops moving, LiteLLM's OTLP export or the Langfuse worker is broken.",
                 "at": lf["last_obs_at"]}
        out.append({"id": "tracing", "label": "Tracing flowing", **t})

        try:
            fl = await asyncio.wait_for(self.flows("7d"), 25)
            split = fl["split"]
            calls = sum(v["calls"] for v in split.values())
            cost = sum(v["cost"] for v in split.values())
            lf_down = [n for n in fl["notes"] if "unavailable" in n]
            if lf_down:
                out.append({"id": "tiers", "label": "Tier discipline (7d)", "status": "unknown", "value": "no data",
                            "detail": lf_down[0]})
            elif calls:
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
                out.append({"id": "kill", "label": "Shared stop",
                            "status": "unknown" if not ks else ("warning" if ks["state"] == "engaged" else "good"),
                            "value": ks["state"] if ks else "unavailable",
                            "detail": "Live shared policy. A local stop file can also halt its runner."})
            else:
                out.append({"id": "evidence", "label": "Harness findings DB", "status": "unknown", "value": "not set up",
                            "detail": h.get("error") or "Apply db/harness.sql to enable the funnel, evidence and scope checks."})
        except Exception as e:  # noqa: BLE001 - a failed check shouldn't break /health
            out.append({"id": "tiers", "label": "Tier discipline (7d)", "status": "unknown", "value": "—", "detail": str(e)[:200]})

        if self.budget_24h:
            spent = ((by.get("langfuse") or {}).get("detail") or {}).get("cost_24h")
            if spent is None:
                out.append({"id": "budget", "label": "LLM spend vs budget (24h)", "status": "unknown", "value": "—",
                            "detail": "Langfuse spend unavailable."})
            else:
                frac = spent / self.budget_24h
                out.append({"id": "budget", "label": "LLM spend vs budget (24h)",
                            "status": "good" if frac < 0.8 else ("warning" if frac <= 1 else "critical"),
                            "value": f"${spent:,.2f} of ${self.budget_24h:,.2f}",
                            "detail": f"{frac:.0%} of DASHBOARD_BUDGET_24H_USD used in the last 24 hours."
                            + (" Over budget: consider engaging the kill switch." if frac > 1 else "")})

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
                    "error": None if ok else "Upstream route check failed"}
        return {"at": time.time(),
                "results": [pick(e, True) for e in j.get("healthy_endpoints", [])]
                + [pick(e, False) for e in j.get("unhealthy_endpoints", [])]}

    # ---- langfuse ------------------------------------------------------------

    async def lf_get(self, path: str, **params) -> dict:
        r = await self.http.get(f"{self.langfuse}{path}", params=params, auth=self.lf_auth, timeout=30)
        r.raise_for_status()
        return r.json()

    async def lf_metrics(self, since: float, until: float, dims: list[str], metrics: list[tuple[str, str]],
                         filters: list[dict] | None = None, gran: str | None = None,
                         order: list[dict] | None = None, limit: int = 1000) -> list[dict]:
        """One Langfuse v2 metrics query over generations; exact, aggregated in ClickHouse."""
        q: dict = {"view": "observations", "dimensions": [{"field": d} for d in dims],
                   "metrics": [{"measure": m, "aggregation": a} for m, a in metrics],
                   "filters": GEN_FILTER + (filters or []),
                   "fromTimestamp": iso(since), "toTimestamp": iso(until), "config": {"row_limit": limit}}
        if gran:
            q["timeDimension"] = {"granularity": gran}
        if order:
            q["orderBy"] = order
        body = json.dumps(q, sort_keys=True)

        async def fetch():
            return (await self.lf_get("/api/public/v2/metrics", query=body)).get("data") or []
        return await self.cache.get(("metrics", body), 30, fetch)

    # Each returns normalized rows; DemoBackend overrides these five.

    async def usage_rows(self, since: float, until: float, gran: str | None) -> list[dict]:
        rows = await self.lf_metrics(since, until, ["providedModelName"], USAGE_METRICS, gran=gran)
        return [{"ts": parse_ts(r.get("time_dimension")), "route": self.route_of(r.get("providedModelName")),
                 "calls": num(r.get("count_count")), "tokens": num(r.get("sum_totalTokens")),
                 "cost": num(r.get("sum_totalCost")), "p50": opt_num(r.get("p50_latency")),
                 "p95": opt_num(r.get("p95_latency"))} for r in rows]

    async def error_rows(self, since: float, until: float, gran: str | None) -> list[dict]:
        rows = await self.lf_metrics(since, until, ["providedModelName"], [("count", "count")], ERROR_FILTER, gran)
        return [{"ts": parse_ts(r.get("time_dimension")), "route": self.route_of(r.get("providedModelName")),
                 "errors": num(r.get("count_count"))} for r in rows]

    async def step_rows(self, since: float, until: float) -> list[dict]:
        dims = ["name", "providedModelName"]
        rows = await self.lf_metrics(since, until, dims, USAGE_METRICS)
        errs = {(r.get("name"), r.get("providedModelName")): num(r.get("count_count"))
                for r in await self.lf_metrics(since, until, dims, [("count", "count")], ERROR_FILTER)}
        return [{"name": r.get("name") or "(unnamed)", "route": self.route_of(r.get("providedModelName")),
                 "calls": num(r.get("count_count")), "tokens": num(r.get("sum_totalTokens")),
                 "cost": num(r.get("sum_totalCost")), "p50": opt_num(r.get("p50_latency")),
                 "p95": opt_num(r.get("p95_latency")),
                 "errors": errs.get((r.get("name"), r.get("providedModelName")), 0)} for r in rows]

    async def session_rows(self, since: float, until: float) -> list[dict]:
        """Top sessions by spend (sessionId is high-cardinality: top-N only, no time dimension)."""
        rows = await self.lf_metrics(since, until, ["sessionId"], [("count", "count"), ("totalCost", "sum")],
                                     order=[{"field": "sum_totalCost", "direction": "desc"}], limit=200)
        return [{"session_id": r.get("sessionId"), "calls": num(r.get("count_count")), "cost": num(r.get("sum_totalCost"))}
                for r in rows if r.get("sessionId")]

    async def route_totals(self, since: float, until: float) -> list[dict]:
        rows = await self.lf_metrics(since, until, ["providedModelName"], [("count", "count"), ("latency", "p95")])
        return [{"route": self.route_of(r.get("providedModelName")), "calls": num(r.get("count_count")),
                 "p95": opt_num(r.get("p95_latency"))} for r in rows]

    def norm_obs(self, o: dict) -> dict:
        st, et = parse_ts(o.get("startTime")), parse_ts(o.get("endTime"))
        ud = o.get("usageDetails") or {}
        md = o.get("metadata")
        if isinstance(md, str):
            try:
                md = json.loads(md)
            except ValueError:
                pass
        return {
            "id": o.get("id"), "trace_id": o.get("traceId"), "ts": st or 0, "name": o.get("name") or "(unnamed)",
            "type": o.get("type"), "model": o.get("model") or "", "route": self.route_of(o.get("model"), md),
            "session_id": o.get("sessionId"),
            "latency_ms": (et - st) * 1000 if st and et else None,
            "in_tok": num(o.get("inputUsage") or ud.get("input")),
            "out_tok": num(o.get("outputUsage") or ud.get("output")),
            "cost": opt_num(o.get("totalCost", (o.get("costDetails") or {}).get("total"))),
            "level": o.get("level") or "DEFAULT",
        }

        # ---- harness DB ----------------------------------------------------------

    async def _db(self, row_factory=dict_row):
        conn = await psycopg.AsyncConnection.connect(
            self.db_url, connect_timeout=5, row_factory=row_factory,
        )
        try:
            await conn.execute("set transaction read only")
            cur = conn.cursor(row_factory=tuple_row)
            await cur.execute("select current_user = 'dashboard_ro'")
            row = await cur.fetchone()
            if row != (True,):
                raise RuntimeError("dashboard requires the dashboard_ro database role")
            return conn
        except BaseException:
            await conn.close()
            raise

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
                    has_guardrails = await rows(
                        "select has_schema_privilege(to_regnamespace('harness_guardrails'), 'USAGE') as allowed",
                        None,
                    )
                    kill = await rows(KILL_SQL, None) if has_guardrails[0]["allowed"] else []
                    runs = await rows(RUNS_SQL, {**p, "limit": 50})
            except psycopg.Error as e:
                return {"available": False, "error": f"Postgres: {err_text(e)}"}
            return {
                "available": True, "funnel": {k: int(v or 0) for k, v in funnel.items()},
                "findings_ts": findings,
                "guardrails": {"by_kind": dict(Counter(k for _, k in guard_ts)), "ts": guard_ts, "recent": recent},
                "kill_switch": ({"state": "engaged" if kill[0]["stopped"] else "released",
                                 "at": kill[0]["checked_at"]} if kill else None),
                "runs": runs,
            }
        return await self.cache.get(("harness", since), 30, fetch)

    # ---- aggregations --------------------------------------------------------

    async def trends(self, rng: str) -> dict:
        async def build():
            starts, step = make_buckets(rng)
            since, n, now = starts[0], len(starts), time.time()
            gran = GRANULARITY[step]
            order = self.route_order()
            notes: list[str] = []

            def idx(t: float | None) -> int | None:
                if t is None:
                    return None
                i = int((t - since) // step)
                return i if 0 <= i < n else None

            def zero(fill=0.0):
                return {r: [fill] * n for r in order}
            calls, tokens, cost, errors = zero(), zero(), zero(), zero()
            p50, p95 = zero(None), zero(None)
            totals_p95: dict[str, float | None] = {}
            try:
                for u in await self.usage_rows(since, now, gran):
                    i, r = idx(u["ts"]), u["route"] if u["route"] in calls else "other"
                    if i is None:
                        continue
                    calls[r][i] += u["calls"]
                    tokens[r][i] += u["tokens"]
                    cost[r][i] += u["cost"]
                    for agg, key in ((p50, "p50"), (p95, "p95")):  # several models on one route: keep the slowest
                        if u[key] is not None:
                            agg[r][i] = max(agg[r][i] or 0, u[key])
                for e in await self.error_rows(since, now, gran):
                    i, r = idx(e["ts"]), e["route"] if e["route"] in errors else "other"
                    if i is not None:
                        errors[r][i] += e["errors"]
                for rt in await self.route_totals(since, now):
                    if rt["p95"] is not None:
                        totals_p95[rt["route"]] = max(totals_p95.get(rt["route"]) or 0, rt["p95"])
            except Exception as e:  # noqa: BLE001
                notes.append(f"Langfuse metrics unavailable: {err_text(e)}")

            h = await self.harness(since)
            findings = [0] * n
            for t in h.get("findings_ts", []) if h.get("available") else []:
                i = idx(t)
                if i is not None:
                    findings[i] += 1
            tot = lambda m: sum(sum(v) for v in m.values())  # noqa: E731
            n_calls, n_err, total_cost, n_find = tot(calls), tot(errors), tot(cost), sum(findings)
            return {
                "range": rng, "step": step, "buckets": starts, "routes": order,
                "calls": calls, "tokens": tokens, "cost": cost, "errors": errors, "p50": p50, "p95": p95,
                "findings": findings, "harness": h.get("available", False),
                "totals": {
                    "calls": n_calls, "tokens": tot(tokens), "cost": total_cost, "errors": n_err,
                    "error_rate": (n_err / n_calls) if n_calls else None, "p95": totals_p95,
                    "findings": n_find if h.get("available") else None,
                    "cost_per_finding": (total_cost / n_find) if n_find else None,
                },
                "notes": notes,
            }
        return await self.cache.get(("trends", rng, make_buckets(rng)[0][0]), 30, build)

    async def flows(self, rng: str) -> dict:
        async def build():
            tr = await self.trends(rng)
            since, now = tr["buckets"][0], time.time()
            notes = list(tr["notes"])
            split = {r: {"calls": sum(tr["calls"][r]), "tokens": sum(tr["tokens"][r]), "cost": sum(tr["cost"][r])}
                     for r in tr["routes"]}

            steps: dict[str, dict] = {}
            sessions: dict[str, dict] = {}
            if not notes:
                try:
                    for s in await self.step_rows(since, now):
                        a = steps.setdefault(s["name"], {"name": s["name"], "calls": 0.0, "cost": 0.0, "tokens": 0.0,
                                                         "errors": 0.0, "p50": None, "p95": None, "routes": Counter()})
                        for k in ("calls", "cost", "tokens", "errors"):
                            a[k] += s[k]
                        for k in ("p50", "p95"):
                            if s[k] is not None:
                                a[k] = max(a[k] or 0, s[k])
                        a["routes"][s["route"]] += s["calls"]
                except Exception as e:  # noqa: BLE001
                    notes.append(f"Per-step metrics unavailable: {err_text(e)}")
                try:
                    sessions = {s["session_id"]: s for s in await self.session_rows(since, now)}
                except Exception as e:  # noqa: BLE001
                    notes.append(f"Per-run spend unavailable (needs sessionId on generations): {err_text(e)}")
            step_rows = [{**s, "route": s["routes"].most_common(1)[0][0] if s["routes"] else "other",
                          "routes": dict(s["routes"])} for s in steps.values()]
            step_rows.sort(key=lambda s: (-s["cost"], -s["calls"]))

            h = await self.harness(since)
            runs, seen = [], set()
            for r in h.get("runs", []) if h.get("available") else []:
                s = sessions.get(r["id"], {})
                seen.add(r["id"])
                runs.append({**r, "tracked": True, "calls": s.get("calls", 0), "cost": s.get("cost", 0.0),
                             "cost_per_finding": (s.get("cost", 0) / r["findings"]) if r["findings"] else None})
            for sid, s in sessions.items():
                if sid in seen or len(runs) >= 60:
                    continue
                runs.append({"id": sid, "target": None, "program": None, "status": "untracked", "tracked": False,
                             "started_at": None, "ended_at": None, "operations": None, "probes": None,
                             "findings": None, "calls": s["calls"], "cost": s["cost"], "cost_per_finding": None})
            runs.sort(key=lambda r: (r["tracked"], r["started_at"] or 0, r["cost"]), reverse=True)

            harness = {k: v for k, v in h.items() if k != "findings_ts"}
            if h.get("available"):
                harness["guardrails"] = {**h["guardrails"], "ts": None}
            return {"range": rng, "buckets": tr["buckets"], "step": tr["step"], "routes": tr["routes"],
                    "split": split, "steps": step_rows[:30], "runs": runs, "harness": harness, "notes": notes}
        return await self.cache.get(("flows", rng, make_buckets(rng)[0][0]), 30, build)

        # ---- raw -----------------------------------------------------------------

    # ---- findings / eval / run detail ----------------------------------------

    async def verdict_rows(self, since: float | None = None, run_id: str | None = None) -> list[dict]:
        """Verdicts joined to op/probe/run/exchange. By judged_at range, or every label for one run."""
        async def fetch():
            if not self.db_url:
                raise ValueError("DASHBOARD_DATABASE_URL not set")
            if run_id is not None:
                where, params = "o.run_id = %(id)s", {"id": run_id}
            else:
                where = "v.judged_at >= %(since)s and v.label in ('confirmed', 'needs_review')"
                params = {"since": datetime.fromtimestamp(since or 0, timezone.utc)}
            async with await self._db() as c:
                return await (await c.execute(VERDICTS_SQL.format(where=where), params)).fetchall()
        return await self.cache.get(("verdicts", since, run_id), 30, fetch)

    def _with_lab(self, rows: list[dict]) -> list[dict]:
        lab = self.lab
        out = []
        for v in rows:
            gt = lab.annotate(v)
            if v.get("label") != "confirmed":  # only confirmed verdicts are scored
                gt = {"lab": gt["lab"], "twin": gt.get("twin")}
            out.append({**clean(v, 600), "finding": finding_key(v), "trace_link": self._lf_link(v.get("trace_id")), **gt})
        return out

    async def findings(self, rng: str) -> dict:
        since = make_buckets(rng)[0][0]
        h = await self.harness(since)
        if not h.get("available"):
            return {"available": False, "error": h.get("error"), "rows": []}
        rows = self._with_lab(await self.verdict_rows(since))
        conf = [r for r in rows if r["label"] == "confirmed"]
        sev = Counter((r.get("severity") or "unrated").lower() for r in conf if r["finding"])
        return {
            "available": True, "range": rng, "rows": rows,
            "totals": {"findings": sum(1 for r in conf if r["finding"]), "needs_review": sum(1 for r in rows if r["label"] == "needs_review"),
                       "no_evidence": sum(1 for r in conf if not r.get("evidence_ref")),
                       "duplicates": sum(1 for r in conf if r.get("is_duplicate")),
                       "lab_fp": sum(1 for r in conf if r.get("outcome") == "fp"),
                       "unmatched": sum(1 for r in conf if r.get("outcome") == "unmatched")},
            "severity": dict(sev),
        }

    async def eval_scores(self, rng: str) -> dict:
        lab = self.lab
        if not lab.targets:
            return {"available": False, "dir": self.lab_dir, "errors": lab.errors,
                    "error": f"No ground truth found in {self.lab_dir}. Mount lab/ground-truth there (docker-compose.yml does) or set LAB_GROUND_TRUTH."}
        since = make_buckets(rng)[0][0]
        fl = await self.flows(rng)
        h = fl["harness"]
        if not h.get("available"):
            return {"available": False, "error": h.get("error"), "targets": [t.summary() for t in lab.targets.values()]}
        spend = {}
        notes = list(fl["notes"])
        try:
            spend = {row["session_id"]: row for row in await self.session_rows(since, time.time())}
        except Exception as exc:
            notes.append(f"Eval spend unavailable: {err_text(exc)}")
        runs = [{**r, "cost": (spend.get(r["id"]) or {}).get("cost"), "calls": (spend.get(r["id"]) or {}).get("calls")}
                for r in await self.run_rows(since, 1000)]
        out = lab.score(runs, await self.verdict_rows(since))
        return {**out, "range": rng, "notes": notes}

    async def run_rows(self, since: float, limit: int) -> list[dict]:
        async def fetch():
            async with await self._db() as c:
                return await (await c.execute(RUNS_SQL, {"since": datetime.fromtimestamp(since, timezone.utc), "limit": limit})).fetchall()
        return await self.cache.get(("run_rows", since, limit), 30, fetch)

    async def run_step_rows(self, run_id: str, since: float, until: float) -> list[dict]:
        rows = await self.lf_metrics(since, until, ["name", "providedModelName"], [("count", "count"), ("totalCost", "sum"), ("totalTokens", "sum")],
                                     filters=[{"column": "sessionId", "operator": "=", "value": run_id, "type": "string"}])
        return [{"name": r.get("name") or "(unnamed)", "route": self.route_of(r.get("providedModelName")),
                 "calls": num(r.get("count_count")), "cost": num(r.get("sum_totalCost")), "tokens": num(r.get("sum_totalTokens"))}
                for r in rows]

    async def run_row(self, run_id: str) -> tuple[dict | None, list[dict]]:
        if not self.db_url:
            raise ValueError("DASHBOARD_DATABASE_URL not set")
        async with await self._db() as c:
            run = await (await c.execute(RUN_SQL, {"id": run_id})).fetchone()
            guards = await (await c.execute(RUN_GUARD_SQL, {"id": run_id})).fetchall() if run else []
        return run, guards

    async def run_detail(self, run_id: str) -> dict:
        run, guards = await self.run_row(run_id)
        if not run:
            raise ValueError(f"no harness run {run_id!r}")
        notes: list[str] = []
        steps: list[dict] = []
        start, end = run["started_at"] or 0, run["ended_at"] or time.time()
        try:
            agg: dict[str, dict] = {}
            for r in await self.run_step_rows(run_id, start - 300, end + 300):
                a = agg.setdefault(r["name"], {"name": r["name"], "route": r["route"], "calls": 0.0, "cost": 0.0, "tokens": 0.0})
                for k in ("calls", "cost", "tokens"):
                    a[k] += r[k]
            steps = sorted(agg.values(), key=lambda x: -x["cost"])
        except Exception as e:  # noqa: BLE001
            notes.append(f"Per-step spend unavailable: {err_text(e)}")
        verdicts = self._with_lab(await self.verdict_rows(run_id=run_id))
        cost = sum(s["cost"] for s in steps)
        lab_t, twin = self.lab.target_of(run.get("target"))
        score = None
        if lab_t:
            res = self.lab.score([{**run, "cost": cost if steps else None}], verdicts)
            score = res["runs"][0] if res["runs"] else None
            if score:
                gt = lab_t.summary()
                score["missed"] = [f for f in lab_t.denominator if f["id"] not in score["found"]
                                   and (twin == "vulnerable" or not f["gated"])]
                score["unscorable"] = [f for f in gt["findings"] if f["scope"] == "authz" and not f["matchable"]]
        findings = sum(1 for v in verdicts if v["finding"])
        return {
            "run": {**clean(run), "cost": cost, "calls": sum(s["calls"] for s in steps), "findings": findings,
                    "cost_per_finding": (cost / findings) if findings and cost else None,
                    "session_link": f"{self.langfuse_ui}/project/{self.lf_project}/sessions/{quote(run_id, safe='')}"},
            "verdicts": verdicts, "steps": steps, "guardrails": guards, "score": score, "notes": notes,
        }

    def _lf_link(self, trace_id: str | None) -> str | None:
        return f"{self.langfuse_ui}/project/{self.lf_project}/traces/{quote(trace_id, safe='')}" if trace_id else None

    async def _raw_obs(self, rng: str, cursor: str | None, fields: str, **extra) -> dict:
        now = time.time()
        params = {"fromStartTime": iso(make_buckets(rng)[0][0]), "toStartTime": iso(now + 60), "limit": 50, "fields": fields, **extra}
        if cursor:
            params["cursor"] = cursor
        j = await self.lf_get("/api/public/v2/observations", **params)
        return j

    async def raw_generations(self, rng: str, cursor: str | None) -> dict:
        j = await self._raw_obs(rng, cursor, "core,basic,model,usage", filter=json.dumps(GEN_FILTER))
        rows = []
        for observation in j.get("data") or []:
            metadata = self.norm_obs(observation)
            rows.append({**metadata, "link": self._lf_link(metadata["trace_id"]), "raw": metadata})
        return {"rows": rows, "next": (j.get("meta") or {}).get("cursor"), "comment": (j.get("meta") or {}).get("comment")}

    async def raw_traces(self, rng: str, cursor: str | None) -> dict:
        """Root observations stand in for traces (v4 has no trace list endpoint)."""
        j = await self._raw_obs(rng, cursor, "core,basic,usage,trace_context", isRootObservation="true")
        rows = []
        for observation in j.get("data") or []:
            metadata = self.norm_obs(observation)
            trace_id = metadata["trace_id"]
            rows.append({**metadata, "id": trace_id, "link": self._lf_link(trace_id), "raw": metadata})
        return {"rows": rows, "next": (j.get("meta") or {}).get("cursor"), "comment": (j.get("meta") or {}).get("comment")}

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
        if q.split(None, 1)[0].lower() not in ("select", "with", "table", "values", "explain", "show"):
            raise ValueError("read-only console: start with SELECT, WITH, TABLE, VALUES, EXPLAIN or SHOW")
        if not self.db_url:
            raise ValueError("DASHBOARD_DATABASE_URL not set")
        t0 = time.perf_counter()
        async with await self._db(row_factory=tuple_row) as c:
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
