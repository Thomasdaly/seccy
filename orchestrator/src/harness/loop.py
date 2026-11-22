"""Run the lab profile with guarded dispatch and manual-review evidence.

Every probe is recorded as needing manual review: nothing here confirms a
finding. A completed lab run also scores the deterministic probe signals
against the lab ground truth (`signal_eval` in `_report.json`). That is an
eval of the probes, not a list of findings, and it never touches the DB
verdicts or sends lab data to a model.
"""
from __future__ import annotations

from contextlib import closing
from uuid import uuid4

import httpx

from .config import Config
from .executor import Executor
from .guardrails import Guardrails, KillEngaged
from .models import Usage
from .scoring import Outcome, Report, load_ground_truth, score
from .shared_dispatch import SharedDispatch
from .store import ArtifactStore, FindingsDB
from .surface import load_spec, map_operations


def run(cfg: Config, target, spec_path: str, gt_path: str, program: str) -> Report:
    ground_truth = load_ground_truth(gt_path)
    operations = map_operations(load_spec(spec_path))

    with closing(SharedDispatch.connect(cfg.database_url)) as shared:
        guardrails = Guardrails.from_config(cfg, shared)
        guardrails.check_stop()
        guardrails.check_scope(httpx.URL(target.base))
        if target.control:
            guardrails.check_scope(httpx.URL(target.control))
        shared.lock_runner()
        guardrails.rate.defer()
        return _run(cfg, target, program, guardrails, ground_truth, operations)


def _run(cfg, target, program, guardrails, ground_truth, operations) -> Report:
    run_id = f"{ground_truth['target']['name']}-{uuid4().hex}"
    artifacts = ArtifactStore(cfg.artifacts_dir, run_id)
    with closing(Executor(guardrails)) as executor, closing(FindingsDB(cfg.database_url)) as db:
        db.start_run(run_id, ground_truth["target"]["name"], program)
        status = "failed"
        outcomes: list[Outcome] = []
        signal_eval = None
        try:
            try:
                identities = target.identities(executor)
                if any(not isinstance(identity.token, str) or not identity.token for identity in identities.values()):
                    raise RuntimeError("required lab identities could not be established")
                probes = target.build_probes(executor, identities, operations)
                for probe in probes:
                    guardrails.check_stop()
                    try:
                        result = probe.run(executor)
                    except (AttributeError, KeyError, TypeError, ValueError):
                        result = None
                    guardrails.check_stop()

                    signal = result.signal is True if result else False
                    control_signal = _strict_bool(result.control_signal) if result else None
                    evidence = {
                        "probe": probe.id,
                        "operation": probe.operation,
                        "finding_id": probe.finding_id,
                        "category": probe.category,
                        "signal": signal,
                        "control_signal": control_signal,
                    }
                    if result is None:
                        evidence["error"] = "probe_response_invalid"
                    else:
                        evidence["attacker"] = result.attacker.to_evidence()
                        if result.control is not None and result.control.method != "LOGIN":
                            evidence["control"] = result.control.to_evidence()
                    evidence_ref = artifacts.write_evidence(probe.id, evidence)
                    outcomes.append(Outcome(
                        finding_id=probe.finding_id, category=probe.category, signal=signal,
                        interesting=signal, confirmed=False, control_signal=control_signal,
                        endpoint=probe.operation,
                    ))

                    method, path = probe.operation.split(" ", 1)
                    with db.transaction():
                        op_id = db.add_operation(run_id, method, path)
                        probe_id = db.add_probe(op_id, probe.attacker, probe.category,
                                                signal if result else None)
                        if result:
                            db.add_exchange(probe_id, result.attacker.status_code, evidence_ref, evidence_ref)
                        db.add_verdict(probe_id, "needs_review", None, False, evidence_ref)
                    print(f"  [{probe.id}] needs review")

                guardrails.check_stop()
                signal_eval = score(ground_truth, outcomes, Usage(), executor.request_count, basis="signal")
                status = "done"
            except (KillEngaged, KeyboardInterrupt):
                status = "killed"
                print("[run] stopped")

            report = Report(target=ground_truth["target"]["name"], authz_total=0,
                            recall=None, requests=executor.request_count, status=status)
            artifacts.write_evidence("_report", {
                "run_id": run_id,
                "status": status,
                "evaluation": "manual_review_required",
                "signal_eval": _signal_eval_summary(signal_eval),
                "requests": report.requests,
                "guardrail_events": [vars(event) for event in guardrails.events],
            })
        except BaseException:
            status = "failed"
            raise
        finally:
            try:
                for event in guardrails.events:
                    db.add_guardrail_event(run_id, event.kind, event.host, event.detail)
            finally:
                db.finish_run(run_id, status)

    if signal_eval is not None:
        print(f"[run] signal eval vs lab ground truth (not findings): "
              f"{len(signal_eval.confirmed_authz)}/{signal_eval.authz_total} authz signalled, "
              f"{len(signal_eval.false_positives)} false positives")
    print(f"[run] {report.requests} request attempts. Evidence needs manual review: {artifacts.dir}")
    return report


def _strict_bool(value) -> bool | None:
    return value if isinstance(value, bool) else None


def _signal_eval_summary(rep: Report | None) -> dict | None:
    """The deterministic probe score, named so it cannot be mistaken for findings.

    None when the run did not complete: a partial run would count unrun probes
    as misses.
    """
    if rep is None:
        return None
    return {
        "basis": "deterministic_probe_signal",
        "note": "Lab ground-truth check of probe oracles. Not confirmed findings; "
                "every probe still needs manual review.",
        "authz_total": rep.authz_total,
        "recall": rep.recall,
        "signalled_authz": rep.confirmed_authz,
        "missed_authz": rep.missed_authz,
        "signalled_other": rep.confirmed_other,
        "false_positives": rep.false_positives,
    }
