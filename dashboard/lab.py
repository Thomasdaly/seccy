"""Score harness runs against the offline lab's ground truth (lab/ground-truth/*.yaml).

A run is a lab run when its `harness.runs.target` names a lab target (see
`LabIndex.target_of`). Each confirmed, evidenced verdict on that run is matched
to the ground-truth file by its operation's METHOD + path template, and scored:

  tp      — matches a `scope: authz` finding valid on this twin       (counts for recall)
  other   — matches a `scope: other` finding (real bug, not authz)     (neither)
  fp      — matches a `negatives` endpoint, a flag-gated finding on the
            secure control twin, or anything at all on the control twin
  unmatched — vulnerable twin, no ground-truth entry: novel or false,
            needs a human

Recall = distinct authz findings hit ÷ authz findings with a matchable endpoint.
Findings whose endpoint can't be matched automatically (e.g. "undocumented
endpoint", "* (Authorization: Bearer)") are listed but left out of the
denominator, and the UI says so.
"""

from __future__ import annotations

import glob
import logging
import os
import re
from typing import Any
from urllib.parse import urlsplit

import yaml

log = logging.getLogger("dashboard")

ENDPOINT_RE = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(/[^\s,&]*)", re.I)
CONTROL_WORDS = {"control", "secure", "safe"}


def parse_endpoints(text: Any) -> list[tuple[str, str]]:
    """'POST /a & PUT /b/{id}' -> [('POST', '/a'), ('PUT', '/b/{id}')]; unparseable -> []."""
    return [(m.upper(), _norm_path(p)) for m, p in ENDPOINT_RE.findall(str(text or ""))]


def _norm_path(p: str) -> str:
    p = str(p or "").strip()
    if "://" in p:
        p = urlsplit(p).path
    p = p.split("?", 1)[0].split("#", 1)[0]
    if len(p) > 1:
        p = p.rstrip("/")
    return p or "/"


class Endpoint:
    """A ground-truth METHOD + path template; `{param}` matches one segment."""

    def __init__(self, method: str, template: str) -> None:
        self.method, self.template = method, template
        segs = template.strip("/").split("/") if template != "/" else []
        self.literal = sum(1 for s in segs if not (s.startswith("{") and s.endswith("}")))
        self.re = re.compile("^/" + "/".join("[^/]+" if s.startswith("{") and s.endswith("}") else re.escape(s) for s in segs) + "$")

    def match(self, method: str, path: str) -> bool:
        return method.upper() == self.method and bool(self.re.match(_norm_path(path)))


class LabTarget:
    def __init__(self, doc: dict, source: str, tailnet_ip: str) -> None:
        t = doc.get("target") or {}
        self.name = str(t.get("name") or os.path.splitext(os.path.basename(source))[0]).lower()
        self.source = os.path.basename(source)
        sub = lambda u: str(u or "").replace("${TAILNET_IP}", tailnet_ip or "${TAILNET_IP}")  # noqa: E731
        self.base_url, self.control_url = sub(t.get("base_url")), sub(t.get("control_url"))
        self.findings: list[dict] = []
        self.rules: list[tuple[Endpoint, dict]] = []  # (endpoint, entry) for findings and negatives
        for f in doc.get("findings") or []:
            eps = parse_endpoints(f.get("endpoint"))
            entry = {"id": str(f.get("id")), "endpoint": str(f.get("endpoint") or ""), "category": f.get("category"),
                     "scope": f.get("scope") or "authz", "gated": f.get("gated", True) is not False,
                     "matchable": bool(eps), "kind": "finding"}
            self.findings.append(entry)
            self.rules += [(Endpoint(m, p), entry) for m, p in eps]
        self.negatives: list[dict] = []
        for i, n in enumerate(doc.get("negatives") or []):
            text = n.get("endpoint") if isinstance(n, dict) else n
            entry = {"id": f"{self.name}-neg-{i + 1}", "endpoint": str(text or "").strip(), "kind": "negative",
                     "note": (n.get("note") if isinstance(n, dict) else None)}
            self.negatives.append(entry)
            self.rules += [(Endpoint(m, p), entry) for m, p in parse_endpoints(text)]
        self.authz = [f for f in self.findings if f["scope"] == "authz"]
        self.denominator = [f for f in self.authz if f["matchable"]]

    def summary(self) -> dict:
        return {"name": self.name, "source": self.source, "base_url": self.base_url, "control_url": self.control_url or None,
                "findings": self.findings, "negatives": self.negatives,
                "authz": len(self.authz), "scorable": len(self.denominator)}

    def classify(self, method: str, path: str, twin: str) -> tuple[str, dict | None]:
        """One confirmed verdict -> (outcome, ground-truth entry or None)."""
        hits = [(ep, e) for ep, e in self.rules if ep.match(method, path)]
        if hits:  # most specific template wins: /users/v1/_debug beats /users/v1/{username}
            best = max(ep.literal for ep, _ in hits)
            hits = [e for ep, e in hits if ep.literal == best]
        findings = [e for e in hits if e["kind"] == "finding"]
        negative = next((e for e in hits if e["kind"] == "negative"), None)
        authz = next((e for e in findings if e["scope"] == "authz"), None)
        if twin == "control":
            live = next((e for e in findings if not e["gated"]), None)  # present on both twins
            if live:
                return ("tp" if live["scope"] == "authz" else "other"), live
            return "fp", (negative or (findings[0] if findings else None))
        if authz:
            return "tp", authz
        if negative:
            return "fp", negative
        if findings:
            return "other", findings[0]
        return "unmatched", None


class LabIndex:
    def __init__(self, directory: str | None, tailnet_ip: str = "") -> None:
        self.dir = directory
        self.targets: dict[str, LabTarget] = {}
        self.errors: list[str] = []
        if not directory or not os.path.isdir(directory):
            return
        for path in sorted(glob.glob(os.path.join(directory, "*.yaml")) + glob.glob(os.path.join(directory, "*.yml"))):
            try:
                with open(path) as f:
                    doc = yaml.safe_load(f) or {}
                t = LabTarget(doc, path, tailnet_ip)
                self.targets[t.name] = t
            except (OSError, yaml.YAMLError, AttributeError, TypeError) as e:
                self.errors.append(f"{os.path.basename(path)}: {str(e).splitlines()[0][:160]}")
                log.warning("ground truth %s unreadable: %s", path, e)

    def target_of(self, run_target: str | None) -> tuple[LabTarget | None, str | None]:
        """harness.runs.target -> (lab target, 'vulnerable' | 'control').

        Matches the run's base URL against the file's base_url / control_url
        (host + port), or a hostname/label whose first token is the lab name,
        e.g. 'vampi', 'vampi.lab', 'vampi-control', 'http://vampi-secure:5000'.
        """
        raw = str(run_target or "").strip().lower()
        if not raw:
            return None, None
        u = urlsplit(raw if "://" in raw else f"//{raw}")
        host, port = (u.hostname or raw), u.port
        for t in self.targets.values():
            for twin, url in (("control", t.control_url), ("vulnerable", t.base_url)):
                if not url:
                    continue
                v = urlsplit(url.lower())
                if port and v.port == port and (host == v.hostname or host in ("localhost", "127.0.0.1", "::1")):
                    return t, twin
        tokens = [x for x in re.split(r"[^a-z0-9]+", host) if x]
        if tokens and tokens[0] in self.targets:
            return self.targets[tokens[0]], ("control" if CONTROL_WORDS & set(tokens) else "vulnerable")
        return None, None

    def annotate(self, row: dict) -> dict:
        """Ground-truth outcome for one verdict row (needs target, method, path)."""
        t, twin = self.target_of(row.get("target"))
        if not t:
            return {"lab": None}
        outcome, entry = t.classify(row.get("method") or "", row.get("path") or "", twin)
        return {"lab": t.name, "twin": twin, "outcome": outcome, "gt_id": entry["id"] if entry else None,
                "gt_category": (entry or {}).get("category")}

    def score(self, runs: list[dict], verdicts: list[dict], limit_cols: int = 12) -> dict:
        """Per-run scores and a per-target coverage matrix.

        runs: rows with id, target, status, started_at, cost.
        verdicts: confirmed + evidenced rows with run_id, method, path.
        """
        by_run: dict[str, list[dict]] = {}
        for v in verdicts:
            by_run.setdefault(v["run_id"], []).append(v)
        scored: list[dict] = []
        for r in runs:
            t, twin = self.target_of(r.get("target"))
            if not t:
                continue
            found: dict[str, int] = {}
            fps: dict[str, int] = {}
            counts = {"tp": 0, "fp": 0, "other": 0, "unmatched": 0}
            for v in by_run.get(r["id"], []):
                outcome, entry = t.classify(v["method"], v["path"], twin)
                counts[outcome] += 1
                key = entry["id"] if entry else f"{v['method']} {v['path']}"
                if outcome in ("tp", "other"):
                    found[key] = found.get(key, 0) + 1
                elif outcome == "fp":
                    fps[key] = fps.get(key, 0) + 1
            hit = [f["id"] for f in t.denominator if f["id"] in found]
            recall_applies = twin == "vulnerable" or any(not f["gated"] for f in t.denominator)
            denom = t.denominator if twin == "vulnerable" else [f for f in t.denominator if not f["gated"]]
            cost = r.get("cost") or 0.0
            scored.append({
                "id": r["id"], "lab": t.name, "twin": twin, "status": r.get("status"), "started_at": r.get("started_at"),
                "ended_at": r.get("ended_at"), "cost": cost, "calls": r.get("calls"),
                "found": found, "fps": fps, "counts": counts,
                "recall": (len([f for f in hit if f in {d['id'] for d in denom}]) / len(denom)) if recall_applies and denom else None,
                "hit": len(hit), "of": len(denom) if recall_applies else None,
                "cost_per_bug": (cost / len(hit)) if hit and cost else None,
            })
        scored.sort(key=lambda s: s["started_at"] or 0, reverse=True)
        targets = []
        for t in self.targets.values():
            mine = [s for s in scored if s["lab"] == t.name]
            vuln = [s for s in mine if s["twin"] == "vulnerable" and s["status"] != "running"]
            latest = vuln[0] if vuln else None
            ctrl = next((s for s in mine if s["twin"] == "control" and s["status"] != "running"), None)
            spent = sum(s["cost"] for s in vuln)
            bugs = sum(s["hit"] for s in vuln)
            targets.append({
                **t.summary(), "runs": len(mine),
                "latest": latest, "latest_control": ctrl,
                "recall_series": [{"id": s["id"], "t": s["started_at"], "recall": s["recall"]} for s in reversed(vuln)],
                "fp_latest": (latest["counts"]["fp"] if latest else 0) + (ctrl["counts"]["fp"] if ctrl else 0) if (latest or ctrl) else None,
                "cost_per_bug": (spent / bugs) if bugs and spent else None,
                "columns": mine[:limit_cols],
            })
        return {"available": bool(self.targets), "dir": self.dir, "errors": self.errors, "targets": targets, "runs": scored}
