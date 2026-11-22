"""PostgreSQL-backed stop signal and shared request ceiling."""
from __future__ import annotations

import psycopg


class SharedDispatch:
    def __init__(self, connection: psycopg.Connection):
        self._connection = connection

    @classmethod
    def connect(cls, dsn: str | None) -> SharedDispatch:
        if not dsn:
            raise RuntimeError("live dispatch requires HARNESS_DATABASE_URL")
        try:
            connection = psycopg.connect(
                dsn, autocommit=True, connect_timeout=5,
                options="-c statement_timeout=2000 -c lock_timeout=1000",
            )
        except psycopg.Error:
            raise RuntimeError("shared dispatch database unavailable") from None
        return cls(connection)

    def close(self) -> None:
        self._connection.close()

    def lock_runner(self) -> None:
        # This session lock is held until close. A reconnect must acquire it again.
        if not self._scalar("select pg_try_advisory_lock(193602442, 1)"):
            raise RuntimeError("another runner holds the shared dispatch lock")

    def stopped(self) -> bool:
        return self._scalar("select stopped from harness_guardrails.dispatch_policy where singleton")

    def reserve(self) -> bool:
        return self._scalar("select harness_guardrails.advance_dispatch(true)")

    def defer(self) -> None:
        if not self._scalar("select harness_guardrails.advance_dispatch(false)"):
            raise RuntimeError("shared dispatch policy unavailable")

    def _scalar(self, statement: str) -> bool:
        try:
            row = self._connection.execute(statement).fetchone()
        except psycopg.Error:
            raise RuntimeError("shared dispatch control unavailable") from None
        if not isinstance(row, tuple) or len(row) != 1 or type(row[0]) is not bool:
            raise RuntimeError("shared dispatch policy unavailable")
        return row[0]
