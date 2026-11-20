"""Harness orchestrator — the agent loop for authorized authorization testing.

Runs on the M1, drives the 2060's services over Tailscale, and scores itself
against the offline lab's ground truth. See the repo's docs/harness-stack-guide.md.

    uv run harness --target vampi            # run against the VAmPI lab + score
    uv run harness --target vampi --dry-run  # map + plan only, no model calls
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import Config
from .targets import VampiTarget

# repo root = two levels up from this file's package (orchestrator/src/harness)
REPO = Path(__file__).resolve().parents[3]


def _vampi(cfg: Config):
    base = f"http://{_lab_ip(cfg)}:5061"
    control = f"http://{_lab_ip(cfg)}:5062"
    return VampiTarget(base, control)


def _lab_ip(cfg: Config) -> str:
    # the lab lives on the same box as the services; reuse the LiteLLM host
    from urllib.parse import urlparse
    host = urlparse(cfg.litellm_base_url).hostname or "100.102.105.11"
    return host


def main() -> None:
    ap = argparse.ArgumentParser(prog="harness", description="authorization-testing harness")
    ap.add_argument("--target", default="vampi", choices=["vampi"], help="lab target to run against")
    ap.add_argument("--dry-run", action="store_true", help="map surface + build probes, no model calls or writes")
    ap.add_argument("--program", default="lab", help="program/engagement label stored on the run")
    args = ap.parse_args()

    cfg = Config()

    if args.target == "vampi":
        target = _vampi(cfg)
        spec = str(REPO / "lab" / "specs" / "vampi-openapi3.yml")
        gt = str(REPO / "lab" / "ground-truth" / "vampi.yaml")
    else:  # pragma: no cover
        ap.error(f"unknown target {args.target}")

    if args.dry_run:
        _dry_run(cfg, target, spec)
        return

    from .loop import run
    rep = run(cfg, target, spec, gt, args.program)
    # non-zero exit if the baseline found nothing, so CI can gate
    sys.exit(0 if rep.confirmed_authz else 1)


def _dry_run(cfg: Config, target, spec: str) -> None:
    from .executor import Executor
    from .guardrails import Guardrails
    from .surface import load_spec, map_operations

    ops = map_operations(load_spec(spec))
    print(f"scope allowlist: {cfg.scope_allowlist}")
    print(f"mapped {len(ops)} operations:")
    for o in ops:
        flag = " [object-ref]" if o.is_object_ref else ""
        print(f"  {str(o):<40}{flag}")
    g = Guardrails.from_config(cfg)
    ex = Executor(g)
    try:
        ids = target.identities(ex)
        print(f"identities: {', '.join(f'{i.name}({i.role}) token={bool(i.token)}' for i in ids.values())}")
        probes = target.build_probes(ex, ids, ops)
        print(f"{len(probes)} probes:")
        for p in probes:
            print(f"  {p.id:<20} -> {p.finding_id:<24} {p.operation}")
    finally:
        ex.close()
