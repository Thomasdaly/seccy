"""Run the lab profile with guarded dispatch and manual-review evidence."""
from __future__ import annotations

from contextlib import closing
from uuid import uuid4

import httpx

from .config import Config
from .executor import Executor
from .guardrails import Guardrails, KillEngaged
from .scoring import Report, load_ground_truth
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

                    evidence = {"probe": probe.id, "operation": probe.operation}
                    if result is None:
                        evidence["error"] = "probe_response_invalid"
                    else:
                        evidence["attacker"] = result.attacker.to_evidence()
                        if result.control is not None and result.control.method != "LOGIN":
                            evidence["control"] = result.control.to_evidence()
                    evidence_ref = artifacts.write_evidence(probe.id, evidence)

                    method, path = probe.operation.split(" ", 1)
                    with db.transaction():
                        op_id = db.add_operation(run_id, method, path)
                        probe_id = db.add_probe(op_id, probe.attacker, probe.category,
                                                result.signal if result else None)
                        if result:
                            db.add_exchange(probe_id, result.attacker.status_code, evidence_ref, evidence_ref)
                        db.add_verdict(probe_id, "needs_review", None, False, evidence_ref)
                    print(f"  [{probe.id}] needs review")

                guardrails.check_stop()
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

    print(f"[run] {report.requests} request attempts. Evidence needs manual review: {artifacts.dir}")
    return report
