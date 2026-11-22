"""Private review artifacts and optional findings-database persistence."""
from __future__ import annotations

import json
import os
from contextlib import nullcontext
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
        self.dir.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.dir.mkdir(mode=0o700)

    def write_evidence(self, probe_id: str, payload: dict) -> str:
        path = self.dir / f"{probe_id}.json"
        data = json.dumps(payload, indent=2)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(data)
        return str(path)


class FindingsDB:
    """Optional writer. A configured database must accept its writes."""

    def __init__(self, database_url: str | None):
        self._conn = None
        if database_url:
            if psycopg is None:
                raise RuntimeError("database configured but Psycopg is unavailable")
            try:
                self._conn = psycopg.connect(database_url, autocommit=True, connect_timeout=5)
            except psycopg.Error:
                raise RuntimeError("could not connect to findings database") from None

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()

    def transaction(self):
        return self._conn.transaction() if self._conn is not None else nullcontext()

    def _exec(self, sql: str, params: tuple = ()) -> Any:
        if self._conn is None:
            return None
        try:
            with self._conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchone() if cur.description else None
        except psycopg.Error:
            raise RuntimeError("findings database write failed") from None

    def start_run(self, run_id: str, target: str, program: str) -> None:
        self._exec(
            "insert into harness.runs (id, target, program, status) values (%s,%s,%s,'running')",
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

    def add_probe(self, op_id: int | None, identity: str, intent: str, interesting: bool | None) -> int | None:
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

    def queue_review(self, probe_id: int | None, evidence_ref: str) -> None:
        if probe_id is None:
            return
        self._exec(
            "insert into harness.verdicts (probe_id, evidence_ref) values (%s,%s)",
            (probe_id, evidence_ref),
        )

    def add_guardrail_event(self, run_id: str, kind: str, host: str | None, detail: str | None) -> None:
        self._exec(
            "insert into harness.guardrail_events (run_id, kind, host, detail) values (%s,%s,%s,%s)",
            (run_id, kind, host, detail),
        )
