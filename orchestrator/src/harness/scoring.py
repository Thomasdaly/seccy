"""Score a run against a ground-truth file (lab/ground-truth/<target>.yaml).

The baseline numbers (guide §8): recall, false-positive rate, cost per confirmed
bug. Recall counts only authz-scope findings (what the harness is built to find);
`other`-scope bugs are reported but never counted as a miss. A false positive is
a confirmation on the secure twin for a flag-gated finding, or a confirmation of
something not in the ground truth.
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
    control_signal: bool | None = None   # did the secure twin also show it?


@dataclass
class Report:
    target: str
    authz_total: int
    confirmed_authz: list[str] = field(default_factory=list)
    missed_authz: list[str] = field(default_factory=list)
    confirmed_other: list[str] = field(default_factory=list)
    false_positives: list[str] = field(default_factory=list)
    recall: float = 0.0
    cost_usd: float = 0.0
    cost_by_tier: dict[str, float] = field(default_factory=dict)
    calls_by_tier: dict[str, int] = field(default_factory=dict)
    requests: int = 0

    @property
    def cost_per_bug(self) -> float | None:
        n = len(self.confirmed_authz) + len(self.confirmed_other)
        return self.cost_usd / n if n else None


def load_ground_truth(path: str | Path) -> dict:
    return yaml.safe_load(Path(path).read_text())


def score(gt: dict, outcomes: list[Outcome], usage, requests: int) -> Report:
    findings = {f["id"]: f for f in gt.get("findings", [])}
    authz_ids = {fid for fid, f in findings.items() if f.get("scope") == "authz"}

    rep = Report(target=gt["target"]["name"], authz_total=len(authz_ids))
    rep.cost_usd = usage.cost_usd
    rep.cost_by_tier = dict(usage.by_tier)
    rep.calls_by_tier = dict(usage.calls_by_tier)
    rep.requests = requests

    confirmed = {o.finding_id for o in outcomes if o.confirmed and not o.duplicate}
    for fid in confirmed:
        f = findings.get(fid)
        if f is None:
            rep.false_positives.append(f"{fid} (not in ground truth)")
        elif f.get("scope") == "authz":
            rep.confirmed_authz.append(fid)
        else:
            rep.confirmed_other.append(fid)

    rep.missed_authz = sorted(authz_ids - confirmed)

    # false positives against the secure twin: a gated finding whose control also fired
    for o in outcomes:
        if o.confirmed and o.control_signal and findings.get(o.finding_id, {}).get("gated") is True:
            rep.false_positives.append(f"{o.finding_id} (fired on secure twin)")

    rep.recall = len(set(rep.confirmed_authz)) / rep.authz_total if rep.authz_total else 0.0
    return rep


def render(rep: Report) -> str:
    lines = []
    lines.append("=" * 60)
    lines.append(f"  BASELINE RESULTS — target: {rep.target}")
    lines.append("=" * 60)
    lines.append(f"  Recall (authz):   {len(set(rep.confirmed_authz))}/{rep.authz_total} "
                 f"= {rep.recall*100:.0f}%")
    lines.append(f"    confirmed:      {', '.join(sorted(set(rep.confirmed_authz))) or '—'}")
    lines.append(f"    missed:         {', '.join(rep.missed_authz) or '—'}")
    lines.append(f"  Other bugs found: {', '.join(sorted(set(rep.confirmed_other))) or '—'}")
    lines.append(f"  False positives:  {len(rep.false_positives)}")
    for fp in rep.false_positives:
        lines.append(f"    - {fp}")
    lines.append("-" * 60)
    lines.append(f"  Requests sent:    {rep.requests}")
    lines.append(f"  Model calls:      " +
                 ", ".join(f"{k}×{v}" for k, v in rep.calls_by_tier.items()) or "—")
    lines.append(f"  Spend (USD):      ${rep.cost_usd:.6f}")
    for tier, c in rep.cost_by_tier.items():
        lines.append(f"    {tier:<10}    ${c:.6f}")
    cpb = rep.cost_per_bug
    lines.append(f"  Cost per bug:     " + (f"${cpb:.6f}" if cpb is not None else "— (no findings)"))
    lines.append("=" * 60)
    return "\n".join(lines)
