# Orchestrator

The CLI supports the configured VAmPI lab twins. Dry run reads the vendored
OpenAPI spec and prints the operation inventory.

    uv run harness --target vampi --dry-run

Live lab runs write redacted artifacts and record probes as needing manual
review. The CLI returns 1 when no confirmed findings are available and 130 when
stopped. It does not send lab data to models or Qdrant.

| Variable | Default | Meaning |
|---|---|---|
| HARNESS_SCOPE_ALLOWLIST | VAmPI lab twins | Permitted host:port entries. An explicitly empty value denies all requests. Lab addressing is independent of LiteLLM. |
| HARNESS_RATE_LIMIT_RPS | 4 | Finite positive local request ceiling. The shared policy can impose a slower rate. |
| HARNESS_KILL_SWITCH | .kill | Stop file. Relative paths resolve from the repository root. |
| HARNESS_KILL | unset | Set to 1 in the runner environment to prevent dispatch. Use the file to stop an existing process. |
| HARNESS_DATABASE_URL | unset | Required for live runs. Use the same direct, session-preserving Postgres database for all runners. |
| HARNESS_ARTIFACTS_DIR | artifacts | Directory for private, redacted artifacts and the run report. |

Apply `db/harness.sql` before live use. It creates a shared policy that starts
stopped with a 250 ms minimum interval. The database operator controls
`harness_guardrails.dispatch_policy.stopped` and `min_interval_ms`. The runner
can read the stop signal and reserve a permit, and its role cannot change the
operator's settings. Applying the schema again preserves the policy.

The gate allows one live run across all runners using the same database. A
local stop file, a shared stop, or an unavailable policy prevents further
dispatch at a checked boundary. A request already sent can still complete on
the server. Dry runs do not require Postgres.

Live response decoding requires macOS and `/usr/bin/sandbox-exec`. The
orchestrator checks the decoder before sending any requests. It passes only
the current response body to a short-lived process with an empty environment,
restricted filesystem reads, and no network access. The process may see
credentials returned in that response. HTTP dispatch remains in the
orchestrator process.

## Cross-run review archive

After reviewing a finding, assign opaque UUIDs to the review scope and finding.
Reuse those IDs when a human identifies the same finding in a different run.
Use the UUID suffix of the source run ID in `_report.json` as the run ID.

    uv run harness-archive-review --scope-id <uuid> --finding-id <uuid> --run-id <uuid>

Set `QDRANT_URL` to your review archive endpoint. A configured
`QDRANT_API_KEY` requires HTTPS. The command stores derived identifiers
and reports whether another run contains the same review. Repeat submissions
of the same run are idempotent. The archive receives no target data or
embeddings and is separate from live runs and scoring.
