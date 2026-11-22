"""Exercise the review permissions on a disposable, Unix-socket-only Postgres."""

import os
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from harness.store import FindingsDB

SCHEMA = Path(__file__).with_name("harness.sql")


class ReviewPermissionsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(cls.enterClassContext(tempfile.TemporaryDirectory(prefix="seccy-pg-", dir="/tmp")))
        cls.socket = root / "socket"
        cls.socket.mkdir(mode=0o700)
        data = root / "data"
        cls.env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": str(root),
            "LC_ALL": "C",
        }
        cls.command("initdb", "-D", str(data), "-U", "postgres", "-A", "trust", "--no-locale")
        cls.addClassCleanup(cls.command, "pg_ctl", "-D", str(data), "-m", "immediate", "-w", "stop",
                            check=False)
        cls.command("pg_ctl", "-D", str(data), "-l", str(root / "postgres.log"), "-o",
                    f"-c listen_addresses='' -c unix_socket_directories={cls.socket}", "-w", "start")
        if cls.sql("show listen_addresses").stdout.strip():
            raise AssertionError("temporary PostgreSQL must only listen on its private Unix socket")
        cls.apply_schema()
        cls.apply_schema(provision_writer=True)
        cls.sql("""
            create role reviewer_alex login;
            create role reviewer_jo login;
            create role synthetic_observer login;
            insert into harness.runs (id, target) values ('review-test', 'synthetic');
            insert into harness.operations (run_id, method, path) values ('review-test', 'GET', '/synthetic');
        """)

    @classmethod
    def command(cls, *args, check=True):
        result = subprocess.run(args, env=cls.env, text=True, capture_output=True, timeout=30)
        if check and result.returncode:
            raise AssertionError(f"{args[0]} failed:\n{result.stdout}{result.stderr}")
        return result

    @classmethod
    def psql(cls, *args, role="postgres", check=True):
        return cls.command("psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1", "-h", str(cls.socket),
                           "-d", "postgres", "-U", role, *args, check=check)

    @classmethod
    def sql(cls, statement, **kwargs):
        return cls.psql("-c", statement, **kwargs)

    @classmethod
    def apply_schema(cls, *, provision_writer=False):
        writer = ("-v", "rw_password=synthetic-writer-password") if provision_writer else ()
        cls.psql("-v", "ro_password=synthetic-reader-password", *writer, "-f", str(SCHEMA))

    def probe(self):
        return self.sql("insert into harness.probes (op_id) values (1) returning id",
                        role="harness_rw").stdout.strip()

    def pending_verdict(self, evidence="synthetic-evidence.json"):
        evidence_sql = "null" if evidence is None else f"'{evidence}'"
        return self.sql(f"insert into harness.verdicts (probe_id, evidence_ref) values ({self.probe()}, "
                        f"{evidence_sql}) returning id", role="harness_rw").stdout.strip()

    def assert_denied(self, statement, role):
        result = self.sql(statement, role=role, check=False)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("permission denied", result.stderr)

    def test_writer_can_queue_but_cannot_choose_or_update_a_label(self):
        self.assert_denied(f"insert into harness.verdicts (probe_id, label) values ({self.probe()}, 'confirmed')",
                           "harness_rw")
        verdict = self.pending_verdict()
        self.assertEqual(self.sql(f"select label from harness.verdicts where id={verdict}").stdout.strip(),
                         "needs_review")
        self.assert_denied(f"update harness.verdicts set label='confirmed' where id={verdict}", "harness_rw")
        self.assert_denied(f"select harness.review_verdict({verdict}, 'confirmed', 'low')", "harness_rw")
        self.assert_denied(f"select harness.review_verdict({verdict}, 'confirmed', 'low')", "dashboard_ro")
        self.assert_denied(f"select harness.review_verdict({verdict}, 'confirmed', 'low')", "synthetic_observer")

    def test_review_is_attributed_and_only_one_decision_wins(self):
        self.sql("grant harness_reviewer to reviewer_alex, reviewer_jo")
        verdict = self.pending_verdict()
        self.assert_denied(f"update harness.verdicts set label='rejected' where id={verdict}", "reviewer_alex")
        self.assert_denied("select stopped from harness_guardrails.dispatch_policy", "reviewer_alex")

        def decide(role):
            return self.sql(f"select harness.review_verdict({verdict}, 'confirmed', 'medium')",
                            role=role).stdout.strip()

        roles = ("reviewer_alex", "reviewer_jo")
        with ThreadPoolExecutor(len(roles)) as workers:
            results = list(workers.map(decide, roles))
        self.assertCountEqual(results, ["t", "f"])
        reviewer = roles[results.index("t")]
        row = self.sql(f"select label, severity, reviewed_by from harness.verdicts where id={verdict}")
        self.assertEqual(row.stdout.strip(), f"confirmed|medium|{reviewer}")

    def test_unsupported_decision_or_severity_leaves_verdict_pending(self):
        self.sql("grant harness_reviewer to reviewer_alex")
        verdict = self.pending_verdict()
        for arguments, diagnostic in (
            ("'needs_review'", "review decision must be confirmed or rejected"),
            ("'confirmed', 'unrated'", "confirmation requires a valid severity"),
            ("'rejected', 'low'", "rejection takes no severity"),
        ):
            with self.subTest(arguments=arguments):
                result = self.sql(f"select harness.review_verdict({verdict}, {arguments})",
                                  role="reviewer_alex", check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(diagnostic, result.stderr)
        row = self.sql(f"select label, reviewed_by from harness.verdicts where id={verdict}")
        self.assertEqual(row.stdout.strip(), "needs_review|")

    def test_confirm_needs_evidence_but_a_reviewer_can_reject(self):
        self.sql("grant harness_reviewer to reviewer_alex")
        for evidence in (None, " \t "):
            with self.subTest(evidence=evidence):
                verdict = self.pending_verdict(evidence)
                confirm = self.sql(f"select harness.review_verdict({verdict}, 'confirmed', 'low')",
                                   role="reviewer_alex")
                self.assertEqual(confirm.stdout.strip(), "f")
                reject = self.sql(f"select harness.review_verdict({verdict}, 'rejected')", role="reviewer_alex")
                self.assertEqual(reject.stdout.strip(), "t")
                row = self.sql(f"select label, reviewed_by from harness.verdicts where id={verdict}")
                self.assertEqual(row.stdout.strip(), "rejected|reviewer_alex")

    def test_actual_findings_writer_queues_with_database_defaults(self):
        dsn = f"host={self.socket} dbname=postgres user=harness_rw"
        with patch.dict(os.environ, self.env, clear=True), closing(FindingsDB(dsn)) as db:
            db.start_run("writer-test", "synthetic", "test")
            with db.transaction():
                operation = db.add_operation("writer-test", "GET", "/synthetic")
                probe = db.add_probe(operation, "synthetic-user", "synthetic-intent", True)
                db.add_exchange(probe, 200, "synthetic.json", "synthetic.json")
                db.queue_review(probe, "synthetic.json")
            db.finish_run("writer-test", "done")
        row = self.sql(f"select label, is_duplicate, reviewed_by from harness.verdicts where probe_id={probe}")
        self.assertEqual(row.stdout.strip(), "needs_review|f|")

    def test_reapply_revokes_legacy_grants_without_resetting_existing_writer(self):
        self.sql("""
            grant insert, update on all tables in schema harness to harness_rw;
            alter default privileges in schema harness grant insert, update on tables to harness_rw;
        """)
        legacy = self.sql(f"insert into harness.verdicts (probe_id, label) values ({self.probe()}, "
                          "'confirmed') returning id").stdout.strip()
        self.apply_schema()
        self.assert_denied("update harness.verdicts set label='confirmed'", "harness_rw")
        self.sql("create table harness.synthetic_future_table (value text)")
        self.assert_denied("insert into harness.synthetic_future_table values ('synthetic')", "harness_rw")
        self.sql("update harness.runs set status='done', ended_at=now() where id='review-test'", role="harness_rw")
        row = self.sql(f"select label, reviewed_by from harness.verdicts where id={legacy}")
        self.assertEqual(row.stdout.strip(), "confirmed|")
        self.assertTrue(self.pending_verdict())


if __name__ == "__main__":
    unittest.main()
