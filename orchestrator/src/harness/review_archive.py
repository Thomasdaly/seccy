"""Archive opaque identifiers supplied after human review."""
from __future__ import annotations

import argparse
import hashlib
import os
from contextlib import closing
from dataclasses import dataclass
from uuid import UUID, uuid5

import httpx
from qdrant_client import QdrantClient, models
from qdrant_client.http.exceptions import ApiException, UnexpectedResponse

_COLLECTION = "harness_review_archive_v1"
_POINT_NAMESPACE = UUID("6dd62658-8041-4c38-ab9e-c60ac9bbb3ee")


@dataclass(frozen=True)
class ReviewedFinding:
    """A reviewer's opaque IDs for the scope, finding, and source run."""

    scope: UUID
    finding: UUID
    run: UUID


def archive_review(client: QdrantClient, review: ReviewedFinding) -> bool:
    """Save this review idempotently and check for another run's review."""
    review_key = hashlib.sha256(review.scope.bytes + review.finding.bytes).hexdigest()
    point_id = str(uuid5(_POINT_NAMESPACE, review_key + review.run.hex))

    if not client.collection_exists(_COLLECTION):
        try:
            created = client.create_collection(_COLLECTION, vectors_config={})
        except UnexpectedResponse:
            # Another operator may have created the collection since our check.
            if not client.collection_exists(_COLLECTION):
                raise
        else:
            if not created:
                raise RuntimeError("review archive collection creation failed")

    result = client.upsert(
        _COLLECTION,
        [models.PointStruct(id=point_id, vector={}, payload={"review_key": review_key})],
        wait=True,
    )
    if result.status != models.UpdateStatus.COMPLETED:
        raise RuntimeError("review archive write did not complete")

    prior, _ = client.scroll(
        _COLLECTION,
        scroll_filter=models.Filter(
            must=[models.FieldCondition(key="review_key", match=models.MatchValue(value=review_key))],
            must_not=[models.HasIdCondition(has_id=[point_id])],
        ),
        limit=1,
        with_payload=False,
        with_vectors=False,
    )
    return bool(prior)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Archive one manually reviewed finding identifier.")
    parser.add_argument("--scope-id", required=True, type=UUID, help="opaque UUID for the review scope")
    parser.add_argument("--finding-id", required=True, type=UUID, help="reviewer-assigned finding UUID")
    parser.add_argument("--run-id", required=True, type=UUID, help="UUID portion of the source run ID")
    args = parser.parse_args(argv)
    review = ReviewedFinding(args.scope_id, args.finding_id, args.run_id)

    try:
        url = httpx.URL(os.environ.get("QDRANT_URL", ""))
    except httpx.InvalidURL:
        parser.error("QDRANT_URL must be a valid HTTP or HTTPS origin")
    if (url.scheme not in {"http", "https"} or not url.host or url.userinfo
            or url.query or url.fragment or url.path not in {"", "/"}):
        parser.error("QDRANT_URL must be an HTTP or HTTPS origin without credentials")
    key = os.environ.get("QDRANT_API_KEY") or None
    if key and url.scheme != "https":
        parser.error("QDRANT_API_KEY requires an HTTPS QDRANT_URL")

    try:
        with closing(QdrantClient(url=str(url), api_key=key, timeout=5, check_compatibility=False,
                                  trust_env=False)) as client:
            matched = archive_review(client, review)
    except (ApiException, httpx.HTTPError, RuntimeError, ValueError):
        raise SystemExit("Review archive unavailable. Retrying with the same IDs is safe.") from None
    print("Archived. Matching review in another run." if matched else "Archived. No matching review in another run.")
