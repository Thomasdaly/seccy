"""Cross-run archive behavior using Qdrant's local storage engine."""

import io
import os
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from uuid import UUID

import httpx
from qdrant_client import QdrantClient

from harness.review_archive import ReviewedFinding, archive_review, main


SCOPE = UUID(int=1)
FINDING = UUID(int=2)
FIRST_RUN = UUID(int=3)
SECOND_RUN = UUID(int=4)


class ReviewArchiveTests(TestCase):
    def test_another_client_finds_the_same_human_reviewed_id(self):
        with TemporaryDirectory() as directory:
            path = str(Path(directory) / "qdrant")
            with closing(QdrantClient(path=path, force_disable_check_same_thread=True)) as first:
                self.assertFalse(archive_review(first, ReviewedFinding(SCOPE, FINDING, FIRST_RUN)))
            with closing(QdrantClient(path=path, force_disable_check_same_thread=True)) as second:
                self.assertTrue(archive_review(second, ReviewedFinding(SCOPE, FINDING, SECOND_RUN)))

    def test_retry_of_the_same_run_does_not_count_as_another_run(self):
        with closing(QdrantClient(":memory:")) as client:
            review = ReviewedFinding(SCOPE, FINDING, FIRST_RUN)
            self.assertFalse(archive_review(client, review))
            self.assertFalse(archive_review(client, review))
            collection = client.get_collections().collections[0].name
            self.assertEqual(client.count(collection).count, 1)

    def test_matching_is_scoped_to_the_review_namespace_and_finding(self):
        with closing(QdrantClient(":memory:")) as client:
            self.assertFalse(archive_review(client, ReviewedFinding(SCOPE, FINDING, FIRST_RUN)))
            self.assertFalse(archive_review(client, ReviewedFinding(UUID(int=5), FINDING, SECOND_RUN)))
            self.assertFalse(archive_review(client, ReviewedFinding(SCOPE, UUID(int=6), SECOND_RUN)))

    def test_archive_persists_only_a_derived_identity(self):
        with closing(QdrantClient(":memory:")) as client:
            archive_review(client, ReviewedFinding(SCOPE, FINDING, FIRST_RUN))
            collection = client.get_collections().collections[0].name
            points, _ = client.scroll(collection, with_payload=True, with_vectors=True)
            self.assertEqual(set(points[0].payload), {"review_key"})
            self.assertEqual(points[0].vector, {})
            for original in (SCOPE, FINDING, FIRST_RUN):
                self.assertNotIn(str(original), str(points[0]))

    def test_cli_uses_the_archive_api_without_passing_source_ids(self):
        requests = []
        responses = [
            {"exists": False},
            True,
            {"operation_id": 0, "status": "completed"},
            {"points": [], "next_page_offset": None},
        ]

        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={"status": "ok", "result": responses.pop(0), "time": 0})

        def make_client(**kwargs):
            return QdrantClient(**kwargs, transport=httpx.MockTransport(respond))

        with (patch.dict(os.environ, {"QDRANT_URL": "https://archive.invalid"}, clear=True),
              patch("harness.review_archive.QdrantClient", side_effect=make_client),
              redirect_stdout(io.StringIO()) as stdout):
            main(["--scope-id", str(SCOPE), "--finding-id", str(FINDING), "--run-id", str(FIRST_RUN)])
        self.assertEqual([request.method for request in requests], ["GET", "PUT", "PUT", "POST"])
        self.assertIn("No matching review", stdout.getvalue())
        for request in requests:
            for original in (SCOPE, FINDING, FIRST_RUN):
                self.assertNotIn(str(original).encode(), request.content)

    def test_cli_refuses_to_send_an_api_key_over_http(self):
        with (patch.dict(os.environ, {"QDRANT_URL": "http://archive.invalid", "QDRANT_API_KEY": "synthetic"},
                         clear=True),
              patch("harness.review_archive.QdrantClient") as make_client,
              redirect_stderr(io.StringIO()) as stderr):
            with self.assertRaises(SystemExit) as failure:
                main(["--scope-id", str(SCOPE), "--finding-id", str(FINDING), "--run-id", str(FIRST_RUN)])
        self.assertEqual(failure.exception.code, 2)
        self.assertIn("requires an HTTPS QDRANT_URL", stderr.getvalue())
        make_client.assert_not_called()
