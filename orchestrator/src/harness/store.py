"""Persistence. Two independent sinks, both optional:

- Artifact store (always on): the replayable request/response evidence for each
  probe, written as JSON under artifacts/<run_id>/. evidence_ref points here.
- Findings DB (if HARNESS_DATABASE_URL is set): the harness.* schema the
  dashboard reads (runs -> operations -> probes -> exchanges -> verdicts, plus
  guardrail_events). Tagged with the run id so the dashboard joins Langfuse
  spend to the run. If no DB is configured the run still scores and reports.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import psycopg
except Exception:  # psycopg optional at import time
    psycopg = None  # type: ignore


class ArtifactStore:
    def __init__(self, base_dir: str, run_id: str):
        self.dir = Path(base_dir) / run_id
        self.dir.mkdir(parents=True, exist_ok=True)

    def write_evidence(self, probe_id: str, payload: dict) -> str:
        path = self.dir / f"{probe_id}.json"
        path.write_text(json.dumps(payload, indent=2, default=str))
        return str(path)


class FindingsDB:
    """Thin writer for the harness schema. No-op-safe: construct with None to
    disable. All writes are best-effort and never crash a run."""

    def __init__(self, database_url: str | None):
        self.enabled = bool(database_url) and psycopg is not None
        self._url = database_url
        self._conn = None
        if self.enabled:
            try:
                self._conn = psycopg.connect(self._url, autocommit=True)
            except Exception as e:
                print(f"[store] DB disabled ({type(e).__name__}: {e})")
                self.enabled = False

    def close(self) -> None:
        if self._conn:
            self._conn.close()

    def _exec(self, sql: str, params: tuple = ()) -> Any:
        if not self.enabled:
            return None
        try:
            with self._conn.cursor() as cur:
                cur.execute(sql, params)
                if cur.description:
                    return cur.fetchone()
        except Exception as e:
            print(f"[store] write skipped ({type(e).__name__}: {e})")
        return None

    def start_run(self, run_id: str, target: str, program: str) -> None:
        self._exec(
            "insert into harness.runs (id, target, program, status) values (%s,%s,%s,'running') "
            "on conflict (id) do nothing",
            (run_id, target, program),
        )

    def finish_run(self, run_id: str, status: str) -> None:
        self._exec(
            "update harness.runs set status=%s, ended_at=%s where id=%s",
            (status, datetime.now(timezone.utc), run_id),
        )

    def add_operation(self, run_id: str, method: str, path: str) -> int | None:
        row = self._exec(
            "insert into harness.operations (run_id, method, path, schema_source) "
            "values (%s,%s,%s,'openapi') returning id",
            (run_id, method, path),
        )
        return row[0] if row else None

    def add_probe(self, op_id: int | None, identity: str, intent: str, interesting: bool) -> int | None:
        if op_id is None:
            return None
        row = self._exec(
            "insert into harness.probes (op_id, identity, intent, interesting) "
            "values (%s,%s,%s,%s) returning id",
            (op_id, identity, intent, interesting),
        )
        return row[0] if row else None

    def add_exchange(self, probe_id: int | None, status_code: int | None, request_ref: str, response_ref: str) -> None:
        if probe_id is None:
            return
        self._exec(
            "insert into harness.exchanges (probe_id, status_code, request_blob, response_blob) "
            "values (%s,%s,%s,%s)",
            (probe_id, status_code, request_ref, response_ref),
        )

    def add_verdict(self, probe_id: int | None, label: str, severity: str | None,
                    is_duplicate: bool, evidence_ref: str | None, trace_id: str | None = None) -> None:
        if probe_id is None:
            return
        self._exec(
            "insert into harness.verdicts (probe_id, label, severity, is_duplicate, evidence_ref, trace_id) "
            "values (%s,%s,%s,%s,%s,%s)",
            (probe_id, label, severity, is_duplicate, evidence_ref, trace_id),
        )

    def add_guardrail_event(self, run_id: str, kind: str, host: str | None, detail: str | None) -> None:
        self._exec(
            "insert into harness.guardrail_events (run_id, kind, host, detail) values (%s,%s,%s,%s)",
            (run_id, kind, host, detail),
        )
