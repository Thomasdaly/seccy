"""Score a run against a ground-truth file (lab/ground-truth/<target>.yaml).

The baseline numbers (guide §8): recall, false-positive rate, cost per confirmed
bug. Recall counts only authz-scope findings (what the harness is built to find);
`other`-scope bugs are reported but never counted as a miss. A false positive is
any signal on the secure twin for a flag-gated finding (whether or not the
vulnerable twin also fired), a hit on a `negatives` endpoint, or a hit on
something not in the ground truth. A finding without an explicit `gated: false`
is treated as gated, matching dashboard/lab.py.

`basis` says what counts as a hit: "confirmed" (a reviewed or judged verdict)
or "signal" (the deterministic probe oracle). A signal-basis report is a lab
eval of the probes, never a list of confirmed findings.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Outcome:
    finding_id: str
    category: str
    signal: bool                 # deterministic prior
    interesting: bool            # glue tier
    confirmed: bool              # judge tier
    duplicate: bool = False
    control_signal: bool | None = None
    endpoint: str | None = None


@dataclass
class Report:
    target: str
    authz_total: int
    confirmed_authz: list[str] = field(default_factory=list)
    missed_authz: list[str] = field(default_factory=list)
    confirmed_other: list[str] = field(default_factory=list)
    false_positives: list[str] = field(default_factory=list)
    recall: float | None = 0.0
    cost_usd: float = 0.0
    cost_by_tier: dict[str, float] = field(default_factory=dict)
    calls_by_tier: dict[str, int] = field(default_factory=dict)
    requests: int = 0
    status: str = "done"
    unknown_cost_calls: int = 0
    basis: str = "confirmed"

    @property
    def cost_per_bug(self) -> float | None:
        n = len(self.confirmed_authz) + len(self.confirmed_other)
        return self.cost_usd / n if n and not self.unknown_cost_calls else None


def load_ground_truth(path: str | Path) -> dict:
    return yaml.safe_load(Path(path).read_text())


BASES = ("confirmed", "signal")


def score(gt: dict, outcomes: list[Outcome], usage, requests: int, basis: str = "confirmed") -> Report:
    if basis not in BASES:
        raise ValueError(f"unknown scoring basis {basis!r}")
    findings = {f["id"]: f for f in gt.get("findings", [])}
    authz_ids = {fid for fid, f in findings.items() if f.get("scope") == "authz"}

    rep = Report(target=gt["target"]["name"], authz_total=len(authz_ids), basis=basis)
    rep.cost_usd = usage.cost_usd
    rep.unknown_cost_calls = usage.unknown_cost_calls
    rep.cost_by_tier = dict(usage.by_tier)
    rep.calls_by_tier = dict(usage.calls_by_tier)
    rep.requests = requests

    negatives = {
        " ".join((item["endpoint"] if isinstance(item, dict) else item).split())
        for item in gt.get("negatives", [])
    }
    confirmed = set()
    control_fired = set()
    false_positives = set()
    for outcome in outcomes:
        fid = outcome.finding_id
        gated = findings.get(fid, {}).get("gated", True) is not False
        if outcome.control_signal is True and gated:
            # The secure twin is the control: any signal there is a false
            # positive, and the probe cannot tell the twins apart, so no recall.
            false_positives.add(f"{fid} (fired on secure twin)")
            control_fired.add(fid)
            continue
        hit = outcome.signal if basis == "signal" else outcome.confirmed
        if hit is not True:
            continue
        endpoint = " ".join(outcome.endpoint.split()) if outcome.endpoint else None
        if endpoint in negatives:
            false_positives.add(f"{fid} (negative endpoint)")
        else:
            confirmed.add(fid)
    confirmed -= control_fired

    for fid in sorted(confirmed):
        f = findings.get(fid)
        if f is None:
            rep.false_positives.append(f"{fid} (not in ground truth)")
        elif f.get("scope") == "authz":
            rep.confirmed_authz.append(fid)
        else:
            rep.confirmed_other.append(fid)

    rep.missed_authz = sorted(authz_ids - confirmed)

    rep.false_positives.extend(sorted(false_positives))

    rep.recall = len(set(rep.confirmed_authz)) / rep.authz_total if rep.authz_total else 0.0
    return rep


def render(rep: Report) -> str:
    signal = rep.basis == "signal"
    hit = "signalled" if signal else "confirmed"
    lines = []
    lines.append("=" * 60)
    if signal:
        lines.append(f"  SIGNAL EVAL (deterministic probes, not findings) — target: {rep.target}")
    else:
        lines.append(f"  BASELINE RESULTS — target: {rep.target}")
    lines.append("=" * 60)
    if rep.recall is None:
        lines.append("  Recall (authz):   manual review required")
    else:
        lines.append(f"  Recall (authz):   {len(set(rep.confirmed_authz))}/{rep.authz_total} "
                     f"= {rep.recall*100:.0f}%")
    lines.append(f"    {hit + ':':<16}{', '.join(sorted(set(rep.confirmed_authz))) or '—'}")
    lines.append(f"    missed:         {', '.join(rep.missed_authz) or '—'}")
    lines.append(f"  Other bugs found: {', '.join(sorted(set(rep.confirmed_other))) or '—'}")
    lines.append(f"  False positives:  {len(rep.false_positives)}")
    for fp in rep.false_positives:
        lines.append(f"    - {fp}")
    lines.append("-" * 60)
    lines.append(f"  Requests sent:    {rep.requests}")
    lines.append(f"  Model calls:      " +
                 (", ".join(f"{k}×{v}" for k, v in rep.calls_by_tier.items()) or "—"))
    lines.append(f"  Known spend (USD): ${rep.cost_usd:.6f}")
    if rep.unknown_cost_calls:
        lines.append(f"  Calls with unknown cost: {rep.unknown_cost_calls}")
    for tier, c in rep.cost_by_tier.items():
        lines.append(f"    {tier:<10}    ${c:.6f}")
    cpb = None if signal else rep.cost_per_bug
    lines.append(f"  Cost per bug:     " + (f"${cpb:.6f}" if cpb is not None else
                                         "— (signal basis)" if signal else "— (no findings)"))
    lines.append("=" * 60)
    return "\n".join(lines)
