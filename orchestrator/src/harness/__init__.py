"""Run the VAmPI lab profile or inspect its OpenAPI surface offline."""
from __future__ import annotations

import argparse
import sys

from .config import Config, LAB_HOST, REPO
from .targets import VampiTarget


EXIT_DONE = 0
EXIT_FAILED = 1
EXIT_KILLED = 130


def exit_code(status: str) -> int:
    """Map a run status to the CLI exit code.

    ``done`` means the run completed and its evidence awaits manual review; it
    says nothing about findings. Any status other than ``done`` or ``killed``
    is a failure. Uncaught errors also exit non-zero.
    """
    if status == "done":
        return EXIT_DONE
    if status == "killed":
        return EXIT_KILLED
    return EXIT_FAILED


def _vampi() -> VampiTarget:
    return VampiTarget(f"http://{LAB_HOST}:5061", f"http://{LAB_HOST}:5062")


def main() -> None:
    ap = argparse.ArgumentParser(prog="harness", description="authorization-testing harness")
    ap.add_argument("--target", default="vampi", choices=["vampi"], help="lab target to run against")
    ap.add_argument("--dry-run", action="store_true", help="map the local OpenAPI surface without network access or writes")
    ap.add_argument("--program", default="lab", help="program/engagement label stored on the run")
    args = ap.parse_args()

    cfg = Config()

    if args.target == "vampi":
        target = _vampi()
        spec = str(REPO / "lab" / "specs" / "vampi-openapi3.yml")
        gt = str(REPO / "lab" / "ground-truth" / "vampi.yaml")
    else:  # pragma: no cover
        ap.error(f"unknown target {args.target}")

    if args.dry_run:
        _dry_run(cfg, spec)
        return

    from .guardrails import KillEngaged, ScopeViolation
    from .loop import run

    try:
        report = run(cfg, target, spec, gt, args.program)
    except KillEngaged:
        sys.exit(EXIT_KILLED)
    except ScopeViolation as exc:
        sys.exit(f"scope violation: {exc}")
    sys.exit(exit_code(report.status))


def _dry_run(cfg: Config, spec: str) -> None:
    from .surface import load_spec, map_operations

    ops = map_operations(load_spec(spec))
    print(f"configured scope entries: {len(cfg.scope_allowlist)}")
    print(f"mapped {len(ops)} operations:")
    for o in ops:
        flag = " [object-ref]" if o.is_object_ref else ""
        print(f"  {str(o):<40}{flag}")
