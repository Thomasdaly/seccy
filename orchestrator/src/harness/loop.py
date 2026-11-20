"""The agent loop (guide §6), concrete and metered.

    build identities          -> target.identities
    map operations            -> surface.map_operations (from the OpenAPI spec)
    for each probe:
        plan  = planner(...)   # which identity SHOULD / SHOULD NOT access
        result = replay(probe) # captured req/resp = evidence
        obs   = glue(result)   # normalize, looks_interesting
        if interesting:
            verdict = judge(probe, rule, evidence)   # frontier confirms
            if confirmed and has_evidence and not duplicate(embed(obs)):
                record_finding
    enforce throughout: scope allowlist, rate limit, kill switch

Tiers are called through LiteLLM with run tagging, so Langfuse/the dashboard see
spend per run and per step. The run ends by scoring itself against ground truth.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .config import Config
from .dedup import Dedup
from .executor import Executor
from .guardrails import Guardrails, ScopeViolation
from .models import Models
from .scoring import Outcome, Report, load_ground_truth, render, score
from .store import ArtifactStore, FindingsDB
from .surface import load_spec, map_operations

PLANNER_SYS = (
    "You plan authorization probes for API security testing. Given an operation "
    "and the identities, state who may and may not access the object. Reply ONLY "
    'with JSON: {"allow": [...], "deny": [...], "rule": "<one line>"}.'
)
GLUE_SYS = (
    "You normalize one HTTP authorization-test observation. Reply ONLY with JSON: "
    '{"interesting": true|false, "signal": "<short>"}. interesting=true when the '
    "response suggests an identity accessed a resource or function it should not."
)
JUDGE_SYS = (
    "You are the final judge for authorization findings. Given the access rule and "
    "the observed request/response, reply ONLY with JSON: "
    '{"confirmed": true|false, "severity": "low|medium|high|critical", "reason": "<one line>"}. '
    "Confirm only if the evidence shows access across an authorization boundary."
)


def run(cfg: Config, target, spec_path: str, gt_path: str, program: str) -> Report:
    cfg.require_key()
    run_id = f"{target.name}-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}"
    print(f"[run] {run_id}  target={target.name}  base={target.base}")

    g = Guardrails.from_config(cfg)
    if g.kill.engaged():
        raise SystemExit(f"kill switch {g.kill.detail()} — refusing to start")

    artifacts = ArtifactStore(cfg.artifacts_dir, run_id)
    db = FindingsDB(cfg.database_url)
    dedup = Dedup(cfg.dedup_threshold, cfg.qdrant_url)
    outcomes: list[Outcome] = []

    db.start_run(run_id, target.name, program)
    ex = Executor(g)
    status = "done"
    try:
        with Models(cfg, run_id) as models:
            print("[run] building identities…")
            ids = target.identities(ex)
            print(f"[run] identities: {', '.join(f'{i.name}({i.role})' for i in ids.values())}")

            ops = map_operations(load_spec(spec_path))
            print(f"[run] mapped {len(ops)} operations from the spec")

            probes = target.build_probes(ex, ids, ops)
            print(f"[run] {len(probes)} probes planned\n")

            for p in probes:
                if g.kill.engaged():
                    g.record("kill_switch", None, g.kill.detail())
                    print("[run] kill switch engaged — stopping")
                    status = "killed"
                    break

                op_id = db.add_operation(run_id, *p.operation.split(" ", 1)) if " " in p.operation else None

                # plan: which identities should / should not access (tier: planner)
                plan = models.planner(
                    "plan_probes",
                    f"Operation: {p.operation}\nProbe: {p.description}\n"
                    f"Identities: {[ (i.name, i.role) for i in ids.values() ]}",
                    system=PLANNER_SYS,
                )
                rule = plan.get("rule", f"only the owner may access the object in {p.operation}")

                # replay the probe (captures evidence); scope-guarded inside executor
                try:
                    res = p.run(ex)
                except ScopeViolation as v:
                    print(f"  [{p.id}] SCOPE DENIED {v} — skipped")
                    continue

                # glue: normalize + looks_interesting (tier: glue), fallback to the prior
                obs = models.glue("observe", f"Rule: {rule}\nObservation: {res.observation}", system=GLUE_SYS)
                interesting = bool(obs.get("interesting")) if "interesting" in obs else res.signal

                evidence = {"probe": p.id, "operation": p.operation, "rule": rule,
                            "observation": res.observation, "attacker": res.attacker.to_evidence()}
                if res.control is not None:
                    evidence["control"] = res.control.to_evidence()
                evidence_ref = artifacts.write_evidence(p.id, evidence)

                probe_id = db.add_probe(op_id, p.attacker, p.category, interesting)
                db.add_exchange(probe_id, res.attacker.status_code, evidence_ref, evidence_ref)

                confirmed = False
                severity = None
                duplicate = False
                if interesting:
                    # judge: confirm across the boundary (tier: judge)
                    verdict = models.judge(
                        "judge_finding",
                        f"Rule: {rule}\nProbe: {p.description}\n"
                        f"Request: {p.operation}\nObservation: {res.observation}\n"
                        f"Status: {res.attacker.status_code}",
                        system=JUDGE_SYS,
                    )
                    confirmed = bool(verdict.get("confirmed")) if "confirmed" in verdict else res.signal
                    severity = verdict.get("severity")
                    if confirmed:
                        vec = models.embed("dedup_embed", f"{p.finding_id}:{res.observation}")
                        duplicate, sim = dedup.is_duplicate(vec, p.finding_id)
                        label = "confirmed"
                    else:
                        label = "rejected"
                else:
                    label = "rejected"

                db.add_verdict(probe_id, label, severity, duplicate, evidence_ref if confirmed else None)

                mark = "✓ CONFIRMED" if confirmed and not duplicate else (
                    "· duplicate" if duplicate else ("? interesting" if interesting else "— rejected"))
                print(f"  [{p.id}] {mark}  ({p.category.split('(')[0].strip()})")
                print(f"      {res.observation}")

                outcomes.append(Outcome(
                    finding_id=p.finding_id, category=p.category, signal=res.signal,
                    interesting=interesting, confirmed=confirmed, duplicate=duplicate,
                    control_signal=_control_signal(res),
                ))

            usage = models.usage
    except Exception:
        status = "failed"
        raise
    finally:
        # flush guardrail events + close
        for evt in g.events:
            db.add_guardrail_event(run_id, evt.kind, evt.host, evt.detail)
        db.finish_run(run_id, status)
        db.close()
        ex.close()

    gt = load_ground_truth(gt_path)
    rep = score(gt, outcomes, usage, ex.request_count)
    print("\n" + render(rep))
    artifacts.write_evidence("_report", {
        "run_id": run_id, "status": status,
        "recall": rep.recall, "confirmed_authz": rep.confirmed_authz,
        "missed_authz": rep.missed_authz, "confirmed_other": rep.confirmed_other,
        "false_positives": rep.false_positives, "cost_usd": rep.cost_usd,
        "cost_per_bug": rep.cost_per_bug, "requests": rep.requests,
        "guardrail_events": [vars(e) for e in g.events],
    })
    print(f"\n[run] evidence + report in {artifacts.dir}")
    return rep


def _control_signal(res) -> bool | None:
    """Did the secure twin / control also exhibit the signal? Used for FP scoring."""
    c = res.control
    if c is None:
        return None
    body = c.response_body if isinstance(c.response_body, dict) else {}
    if "secret" in body:
        return True
    if body.get("victim_password_changed") is True:
        return True
    if isinstance(body.get("data"), dict) and body["data"].get("admin") is True:
        return True
    return False
