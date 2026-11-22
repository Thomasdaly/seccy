"""Review queue behavior with synthetic verdicts and no service connections."""

import time
from unittest import IsolatedAsyncioTestCase

from demo import DemoBackend


class ReviewQueueTests(IsolatedAsyncioTestCase):
    async def test_old_pending_work_stays_visible_outside_the_findings_range(self):
        backend = DemoBackend()
        self.addAsyncCleanup(backend.close)
        old_time = time.time() - 180 * 86400
        older = dict(backend.verdicts[0], id=1, judged_at=old_time, label="needs_review", evidence_ref=None)
        newer = dict(older, id=2, judged_at=old_time + 1, evidence_ref="synthetic.json")
        closed = dict(older, id=3, label="rejected", reviewed_by="synthetic-reviewer")
        backend.verdicts = [newer, closed, older]

        findings = await backend.findings("24h")

        self.assertEqual(findings["rows"], [])
        self.assertEqual(findings["totals"]["findings"], 0)
        pending = findings["pending_rows"]
        self.assertEqual([row["id"] for row in pending], [1, 2])
        self.assertEqual([row["evidence_ref"] for row in pending], [None, "synthetic.json"])
        self.assertFalse(any(row["finding"] for row in pending))
