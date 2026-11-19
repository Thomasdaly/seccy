"""Synthetic data for DASHBOARD_DEMO=1: same shapes as the live sources.

Tells a plausible story: runs ramp up over 90 days, the glue classifier gets
sharper (fewer probes escalated to the judge), an OpenRouter incident 12 days
ago spikes planner errors, and one recent confirmed verdict lacks evidence.
"""

from __future__ import annotations

import random
import time
from collections import deque

from backend import GRANULARITY, HARNESS_TABLES, HISTORY_SAMPLES, PIPELINE, POLL_SECONDS, Backend, clean, make_buckets, percentile

TARGETS = [("juice-shop.lab", "lab"), ("crapi.lab", "lab"), ("vampi.lab", "lab"), ("tenants-api.lab", "lab")]
PATHS = ["/api/Users/{id}", "/api/BasketItems/{id}", "/rest/basket/{id}", "/identity/api/v2/vehicle/{id}/location",
         "/workshop/api/shop/orders/{id}", "/users/v1/{username}", "/books/v1/{title}", "/tenants/{tid}/invoices/{id}",
         "/tenants/{tid}/members", "/api/Feedbacks/{id}", "/community/api/v2/community/posts/{id}", "/api/Cards/{id}"]
MODELS = {"glue": "qwen3:8b", "embed": "bge-m3", "planner": "z-ai/glm-4.6", "judge": "claude-sonnet-5-5"}
STEPS = {"glue": "observe", "embed": "dedup_embed", "planner": "plan_probes", "judge": "judge_finding"}


def _shape(rnd: random.Random, route: str, age_days: float) -> dict:
    if route == "glue":
        i, o, lat, cost = rnd.randint(600, 1500), rnd.randint(30, 120), rnd.uniform(300, 1400), 0.0
        err = 0.015
    elif route == "embed":
        i, o, lat, cost = rnd.randint(200, 600), 0, rnd.uniform(40, 180), 0.0
        err = 0.002
    elif route == "planner":
        i, o, lat = rnd.randint(2000, 5000), rnd.randint(400, 1200), rnd.uniform(4000, 14000)
        cost = i * 0.6e-6 + o * 2.2e-6
        err = 0.30 if 11.5 < age_days < 12.5 else 0.025
    else:
        i, o, lat = rnd.randint(3000, 8000), rnd.randint(300, 900), rnd.uniform(6000, 25000)
        cost = i * 3e-6 + o * 15e-6
        err = 0.005
    lat *= 1 + 0.5 * rnd.random() ** 6  # long tail
    return {"in_tok": i, "out_tok": o, "latency_ms": lat, "cost": cost, "error": rnd.random() < err}


class DemoBackend(Backend):
    demo = True

    def __init__(self) -> None:
        super().__init__()
        if not self.routes:
            self.routes = {"glue": "ollama/qwen3:8b", "embed": "ollama/bge-m3",
                           "planner": "openrouter/z-ai/glm-4.6", "judge": "anthropic/claude-<current>"}
        self._build(random.Random(7))
        self._fake_history(random.Random(11))

    # ---- world ---------------------------------------------------------------

    def _build(self, rnd: random.Random) -> None:
        now = time.time()
        self.now = now
        self.obs, self.trs, self.runs, self.ops, self.probes = [], [], [], [], []
        self.exchanges, self.verdicts, self.guards = [], [], []
        oid = pid = eid = vid = gid = 0

        def call(route: str, t: float, run_id: str, name: str | None = None):
            s = _shape(rnd, route, (now - t) / 86400)
            tid = f"tr-{len(self.trs):06d}"
            o = {"id": f"ob-{len(self.obs):06d}", "trace_id": tid, "ts": t, "name": name or STEPS[route],
                 "model": MODELS[route], "route": route, "latency_ms": s["latency_ms"], "in_tok": s["in_tok"],
                 "out_tok": s["out_tok"], "cost": 0.0 if s["error"] else s["cost"],
                 "level": "ERROR" if s["error"] else "DEFAULT",
                 "status": ("429 rate limited by upstream" if route == "planner" else "timeout") if s["error"] else None}
            self.obs.append(o)
            self.trs.append({"id": tid, "ts": t, "name": o["name"], "session_id": run_id, "cost": o["cost"],
                             "latency_ms": o["latency_ms"]})
            return o

        for d in range(90, -1, -1):
            nruns = rnd.choice([0, 1, 1, 2, 2, 3]) if d < 45 else rnd.choice([0, 0, 1])
            for _ in range(nruns):
                start = now - d * 86400 - rnd.uniform(0, 72000)
                if start > now - 1800:
                    start = now - rnd.uniform(1800, 7200)
                target, program = rnd.choice(TARGETS)
                rid = f"run-{time.strftime('%m%d', time.gmtime(start))}-{len(self.runs):03d}"
                age = (now - start) / 86400
                interesting_rate = 0.05 + 0.10 * (age / 90)  # classifier improving
                t = start
                n_ops = rnd.randint(15, 40)
                findings = 0
                for _ in range(n_ops):
                    oid += 1
                    method = rnd.choice(["GET", "GET", "GET", "PUT", "DELETE", "PATCH"])
                    self.ops.append({"id": oid, "run_id": rid, "method": method, "path": rnd.choice(PATHS),
                                     "schema_source": rnd.choice(["openapi", "openapi", "graphql", "apk"]), "created_at": t})
                    t += rnd.uniform(2, 8)
                    call("glue", t, rid, "classify_op")
                    t += rnd.uniform(5, 15)
                    call("planner", t, rid)
                    for _ in range(rnd.randint(2, 4)):
                        pid += 1
                        identity = rnd.choice(["alice", "bob", "mallory", "admin-t2"])
                        interesting = rnd.random() < interesting_rate
                        self.probes.append({"id": pid, "op_id": oid, "identity": identity,
                                            "tenant": rnd.choice(["t1", "t2"]), "role": rnd.choice(["user", "admin", "viewer"]),
                                            "intent": rnd.choice(["cross-tenant read", "horizontal read", "vertical write", "owner swap"]),
                                            "interesting": interesting, "created_at": t})
                        t += rnd.uniform(1, 4)
                        if rnd.random() < 0.97:
                            eid += 1
                            self.exchanges.append({"id": eid, "probe_id": pid, "status_code": rnd.choice([200, 200, 403, 404, 401]),
                                                   "request_blob": f"artifacts/{rid}/{pid}.req", "response_blob": f"artifacts/{rid}/{pid}.resp",
                                                   "created_at": t})
                        call("glue", t, rid)
                        if interesting:
                            t += rnd.uniform(5, 20)
                            j = call("judge", t, rid)
                            r = rnd.random()
                            label = "confirmed" if r < 0.22 else ("needs_review" if r < 0.30 else "rejected")
                            evidence = f"artifacts/{rid}/{pid}.har" if label == "confirmed" else None
                            if label == "confirmed" and d == 2 and not any(v["evidence_ref"] is None and v["label"] == "confirmed" for v in self.verdicts):
                                evidence = None
                            dup = label == "confirmed" and rnd.random() < 0.35
                            vid += 1
                            self.verdicts.append({"id": vid, "probe_id": pid, "label": label,
                                                  "severity": rnd.choice(["high", "medium", "medium", "low"]) if label == "confirmed" else None,
                                                  "is_duplicate": dup, "evidence_ref": evidence, "trace_id": j["trace_id"], "judged_at": t})
                            if label == "confirmed":
                                call("embed", t + 1, rid)
                                if evidence and not dup:
                                    findings += 1
                    if rnd.random() < 0.04:
                        gid += 1
                        self.guards.append({"id": gid, "ts": t, "run_id": rid, "kind": "scope_denied",
                                            "host": rnd.choice(["cdn.example.net", "accounts.google.com", "sentry.io"]),
                                            "detail": "host not in allowlist; request dropped"})
                    if rnd.random() < 0.02:
                        gid += 1
                        self.guards.append({"id": gid, "ts": t, "run_id": rid, "kind": "rate_limited",
                                            "host": target, "detail": "per-target ceiling 5 req/s reached; backing off"})
                status = "running" if now - t < 600 else ("killed" if rnd.random() < 0.04 else "done")
                self.runs.append({"id": rid, "target": target, "program": program, "status": status,
                                  "started_at": start, "ended_at": None if status == "running" else t})
                if d <= 20 and not any(g["kind"] == "kill_switch" for g in self.guards):
                    for k, (state, dt) in enumerate((("engaged by operator", 0), ("released", 2400))):
                        gid += 1
                        self.guards.append({"id": gid, "ts": start + 300 + dt, "run_id": rid, "kind": "kill_switch",
                                            "host": None, "detail": state})
        self.obs = [o for o in self.obs if o["ts"] <= now]
        self.trs = [t for t in self.trs if t["ts"] <= now]
        self.obs.sort(key=lambda o: -o["ts"])
        self.trs.sort(key=lambda t: -t["ts"])
        self.run_of_op = {o["id"]: o["run_id"] for o in self.ops}
        self.session_of = {t["id"]: t["session_id"] for t in self.trs}
        self.op_of_probe = {p["id"]: p["op_id"] for p in self.probes}

    def _fake_history(self, rnd: random.Random) -> None:
        now = time.time()
        for name in ("litellm", "ollama", "qdrant", "langfuse", "postgres", *PIPELINE):
            h = deque(maxlen=HISTORY_SAMPLES)
            base = {"litellm": 6, "ollama": 9, "qdrant": 3, "langfuse": 40, "postgres": 12}.get(name, 3)
            for i in range(HISTORY_SAMPLES):
                t = now - (HISTORY_SAMPLES - i) * POLL_SECONDS
                age_h = (now - t) / 3600
                ok = not (name == "ollama" and 6.0 < age_h < 6.4) and not (name == "langfuse" and 15.2 < age_h < 15.3)
                h.append((t, ok, round(base * (1 + rnd.random() ** 3 * 3), 1)))
            self.history[name] = h

    # ---- sources -------------------------------------------------------------

    async def poll_once(self) -> None:
        now = time.time()
        last = self.obs[0]["ts"] if self.obs else None
        services = [
            {"name": "litellm", "ok": True, "ms": 6.2, "error": None,
             "detail": {"version": "1.80.0", "langfuse_callback": True, "routes": len(self.routes)}},
            {"name": "ollama", "ok": True, "ms": 8.9, "error": None, "detail": {
                "version": "0.12.6",
                "loaded": [{"name": "qwen3:8b", "size": 6_200_000_000, "size_vram": 6_200_000_000, "gpu_pct": 100, "expires_at": now + 240}],
                "installed": [
                    {"name": "qwen3:8b", "size": 5_225_000_000, "family": "qwen3", "params": "8.2B", "quant": "Q4_K_M", "modified_at": now - 30 * 86400},
                    {"name": "bge-m3:latest", "size": 1_157_000_000, "family": "bert", "params": "566.70M", "quant": "F16", "modified_at": now - 30 * 86400}]}},
            {"name": "qdrant", "ok": True, "ms": 2.7, "error": None, "detail": {
                "version": "1.15.4",
                "collections": [{"name": "findings", "status": "green", "points": sum(1 for v in self.verdicts if v["label"] == "confirmed"),
                                 "indexed": 0, "segments": 2, "vectors": "default: 1024d Cosine"}]}},
            {"name": "langfuse", "ok": True, "ms": 38.5, "error": None,
             "detail": {"version": "4.79.0", "last_obs_at": last, "gens_24h": sum(1 for o in self.obs if o["ts"] > now - 86400),
                        "cost_24h": sum(o["cost"] for o in self.obs if o["ts"] > now - 86400)}},
            {"name": "postgres", "ok": True, "ms": 11.4, "error": None,
             "detail": {"version": "17.6", "size": 412_000_000, "conns": 9, "max_conns": 100, "harness": True}},
            {"name": "langfuse-worker", "ok": True, "ms": 4.1, "error": None, "detail": {"http": 200}},
            {"name": "clickhouse", "ok": True, "ms": 2.2, "error": None, "detail": {"http": 200}},
            {"name": "redis", "ok": True, "ms": 1.1, "error": None, "detail": {"reply": "auth required (alive)"}},
            {"name": "minio", "ok": True, "ms": 3.0, "error": None, "detail": {"http": 200}},
        ]
        for s in services:
            self.history[s["name"]].append((now, s["ok"], s["ms"]))
        self.snapshot = {"at": now, "services": services,
                         "disk": {"total": 1_000_000_000_000, "used": 690_000_000_000, "free": 310_000_000_000, "free_pct": 31.0}}

    async def observations(self, since: float):
        return [o for o in self.obs if o["ts"] >= since]

    def _agg(self, since: float, until: float, keyf, rows=None) -> dict:
        acc: dict = {}
        for o in rows if rows is not None else self.obs:
            if not (since <= o["ts"] < until):
                continue
            a = acc.setdefault(keyf(o), {"calls": 0, "tokens": 0, "cost": 0.0, "errors": 0, "lat": []})
            a["calls"] += 1
            a["tokens"] += o["in_tok"] + o["out_tok"]
            a["cost"] += o["cost"]
            a["errors"] += o["level"] == "ERROR"
            a["lat"].append(o["latency_ms"])
        return acc

    @staticmethod
    def _bucket(ts: float, gran: str | None) -> float | None:
        if gran is None:
            return None
        step = {v: k for k, v in GRANULARITY.items()}[gran]
        off = 4 * 86400 if gran == "week" else 0
        return ts - (ts - off) % step

    async def usage_rows(self, since, until, gran):
        acc = self._agg(since, until, lambda o: (self._bucket(o["ts"], gran), o["route"]))
        return [{"ts": k[0], "route": k[1], "calls": a["calls"], "tokens": a["tokens"], "cost": a["cost"],
                 "p50": percentile(a["lat"], .5), "p95": percentile(a["lat"], .95)} for k, a in acc.items()]

    async def error_rows(self, since, until, gran):
        acc = self._agg(since, until, lambda o: (self._bucket(o["ts"], gran), o["route"]))
        return [{"ts": k[0], "route": k[1], "errors": a["errors"]} for k, a in acc.items() if a["errors"]]

    async def route_totals(self, since, until):
        acc = self._agg(since, until, lambda o: o["route"])
        return [{"route": k, "calls": a["calls"], "p95": percentile(a["lat"], .95)} for k, a in acc.items()]

    async def step_rows(self, since, until):
        acc = self._agg(since, until, lambda o: (o["name"], o["route"]))
        return [{"name": k[0], "route": k[1], "calls": a["calls"], "tokens": a["tokens"], "cost": a["cost"],
                 "p50": percentile(a["lat"], .5), "p95": percentile(a["lat"], .95), "errors": a["errors"]}
                for k, a in acc.items()]

    async def session_rows(self, since, until):
        acc = self._agg(since, until, lambda o: self.session_of[o["trace_id"]])
        rows = [{"session_id": k, "calls": a["calls"], "cost": a["cost"]} for k, a in acc.items()]
        return sorted(rows, key=lambda r: -r["cost"])[:200]

    async def harness(self, since: float) -> dict:
        probes = {p["id"]: p for p in self.probes if p["created_at"] >= since}
        verdicts = [v for v in self.verdicts if v["probe_id"] in probes]
        conf = [v for v in verdicts if v["label"] == "confirmed"]
        funnel = {
            "operations": sum(1 for o in self.ops if o["created_at"] >= since),
            "planned": len(probes),
            "executed": len({e["probe_id"] for e in self.exchanges if e["probe_id"] in probes}),
            "interesting": sum(1 for p in probes.values() if p["interesting"]),
            "judged": len({v["probe_id"] for v in verdicts}),
            "confirmed": len(conf),
            "evidenced": sum(1 for v in conf if v["evidence_ref"]),
            "unique_findings": sum(1 for v in conf if v["evidence_ref"] and not v["is_duplicate"]),
            "confirmed_no_evidence": sum(1 for v in conf if not v["evidence_ref"]),
            "rejected": sum(1 for v in verdicts if v["label"] == "rejected"),
            "needs_review": sum(1 for v in verdicts if v["label"] == "needs_review"),
        }
        guards = [g for g in self.guards if g["ts"] >= since]
        by_kind: dict = {}
        for g in guards:
            by_kind[g["kind"]] = by_kind.get(g["kind"], 0) + 1
        kills = sorted((g for g in self.guards if g["kind"] == "kill_switch"), key=lambda g: -g["ts"])
        runs = []
        for r in sorted((r for r in self.runs if r["started_at"] >= since), key=lambda r: -r["started_at"])[:50]:
            ops = [o["id"] for o in self.ops if o["run_id"] == r["id"]]
            ops_set = set(ops)
            pr = {p["id"] for p in self.probes if p["op_id"] in ops_set}
            f = sum(1 for v in self.verdicts if v["probe_id"] in pr and v["label"] == "confirmed" and v["evidence_ref"] and not v["is_duplicate"])
            runs.append({**r, "operations": len(ops), "probes": len(pr), "findings": f})
        return {
            "available": True, "funnel": funnel,
            "findings_ts": [v["judged_at"] for v in conf if v["evidence_ref"] and not v["is_duplicate"]],
            "guardrails": {"by_kind": by_kind, "ts": [(g["ts"], g["kind"]) for g in guards],
                           "recent": [{"t": g["ts"], "kind": g["kind"], "host": g["host"], "detail": g["detail"], "run_id": g["run_id"]}
                                      for g in sorted(guards, key=lambda g: -g["ts"])[:25]]},
            "kill_switch": {"state": "engaged" if kills[0]["detail"].startswith("engag") else "released", "at": kills[0]["ts"]} if kills else None,
            "runs": runs,
        }

    async def probe_routes(self) -> dict:
        return {"at": time.time(), "results": [
            {"route": "glue", "model": "ollama/qwen3:8b", "ok": True, "error": None},
            {"route": "embed", "model": "ollama/bge-m3", "ok": True, "error": None},
            {"route": "planner", "model": "openrouter/z-ai/glm-4.6", "ok": True, "error": None},
            {"route": "judge", "model": "anthropic/claude-<current>", "ok": False,
             "error": "litellm.NotFoundError: AnthropicException - model: claude-<current> not found"}]}

    # ---- raw -----------------------------------------------------------------

    def _cursor_page(self, rows: list, cursor: str | None, size: int = 50) -> tuple[list, str | None]:
        start = int(cursor) if cursor and cursor.isdigit() else 0
        return rows[start:start + size], (str(start + size) if start + size < len(rows) else None)

    async def raw_generations(self, rng: str, cursor: str | None) -> dict:
        since = make_buckets(rng)[0][0]
        rows, nxt = self._cursor_page([o for o in self.obs if o["ts"] >= since], cursor)
        out = []
        for o in rows:
            inp = f"[{o['name']}] Operation GET /api/Users/{{id}} as identity alice (tenant t1). Summarise the response diff."
            outp = "" if o["level"] == "ERROR" else ('{"interesting": false, "reason": "403 for foreign object"}' if o["route"] == "glue"
                                                     else "Plan: 3 probes (owner swap, tenant swap, role downgrade).")
            raw = {"id": o["id"], "traceId": o["trace_id"], "type": "GENERATION", "name": o["name"], "model": o["model"],
                   "startTime": o["ts"], "sessionId": self.session_of[o["trace_id"]],
                   "usageDetails": {"input": o["in_tok"], "output": o["out_tok"]},
                   "totalCost": o["cost"], "level": o["level"], "statusMessage": o["status"],
                   "input": f'[{{"role": "user", "content": "{inp}"}}]', "output": outp, "metadata": {"model_group": o["route"]}}
            out.append({**o, "session_id": self.session_of[o["trace_id"]], "input": inp, "output": outp,
                        "link": self._lf_link(o["trace_id"]), "raw": raw})
        return {"rows": out, "next": nxt}

    async def raw_traces(self, rng: str, cursor: str | None) -> dict:
        since = make_buckets(rng)[0][0]
        rows, nxt = self._cursor_page([t for t in self.trs if t["ts"] >= since], cursor)
        return {"rows": [{**t, "tags": [], "type": "SPAN", "input": f"{t['name']} for {t['session_id']}",
                          "link": self._lf_link(t["id"]), "raw": clean(t)} for t in rows], "next": nxt}

    def _table_rows(self, name: str) -> list[dict]:
        return {"runs": self.runs, "operations": self.ops, "probes": self.probes, "exchanges": self.exchanges,
                "verdicts": self.verdicts, "guardrail_events": self.guards}[name]

    async def raw_tables(self) -> dict:
        return {"available": True, "tables": [{"name": t, "rows": len(self._table_rows(t))} for t in HARNESS_TABLES]}

    async def raw_table(self, name: str, page: int) -> dict:
        if name not in HARNESS_TABLES:
            raise ValueError(f"unknown table {name!r}")
        key = HARNESS_TABLES[name]
        allr = sorted(self._table_rows(name), key=lambda r: -(r[key] if isinstance(r[key], (int, float)) else 0))
        rows, _ = self._page(allr, page)
        ts_cols = {"created_at", "started_at", "ended_at", "judged_at", "ts"}
        rows = [{k: (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(v)) if k in ts_cols and v else v) for k, v in r.items()} for r in rows]
        return {"columns": list(rows[0]) if rows else [], "rows": rows, "page": page, "more": page * 50 < len(allr)}

    async def raw_qdrant(self, name, offset=None) -> dict:
        if not name:
            return {"collections": self.snapshot["services"][2]["detail"]["collections"] if self.snapshot else []}
        conf = [v for v in self.verdicts if v["label"] == "confirmed"]
        start = int(offset or 0)
        pts = [{"id": v["id"], "payload": {"probe_id": v["probe_id"], "severity": v["severity"], "duplicate_of": None,
                                           "run_id": self.run_of_op.get(self.op_of_probe.get(v["probe_id"])),
                                           "summary": "Cross-tenant read of another tenant's object"}} for v in conf[start:start + 50]]
        return {"points": pts, "next": start + 50 if start + 50 < len(conf) else None}

    async def raw_sql(self, query: str) -> dict:
        raise ValueError("SQL console needs a live Postgres; it is off in demo mode.")
