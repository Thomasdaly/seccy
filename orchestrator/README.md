# Orchestrator

The CLI supports the configured VAmPI lab twins. Dry run reads the vendored
OpenAPI spec and prints the operation inventory.

    uv run harness --target vampi --dry-run

Live lab runs write redacted artifacts and record probes as needing manual
review. It does not send lab data to models or Qdrant. Only a human review
confirms a finding.

A completed run also checks each probe's deterministic signal against the lab
ground truth and writes the result to `signal_eval` in `_report.json`. It lists
the authz findings that were signalled or missed, and the false positives. A
signal on the secure twin for a gated finding is a false positive, and that
finding does not count towards recall. A finding counts as gated unless the
ground truth sets `gated: false`. Use the signal eval to compare probe changes
between runs. It is not a list of findings, it does not change database
verdicts, and a stopped run has none.

Each probe artifact records the probe, finding and category IDs, the signal
and secure-twin signal, and redacted attacker and control exchanges. An
exchange records the method, origin, route template (such as
`/books/v1/{book_title}`), status, content kind (`json`, `text` or `other`),
response size in bytes, and an error kind from a fixed list. It never records
the concrete path, query, headers or response values. Body values become
`[redacted]`, apart from booleans and nulls under known keys.

| Exit code | Meaning |
|---|---|
| 0 | Run completed (`status` `done`). Evidence still needs manual review; this does not mean findings were confirmed. |
| 130 | Stopped by the stop file, `HARNESS_KILL=1`, the shared stop, or Ctrl-C (`status` `killed`). |
| 1 | Run failed, or a scope violation was refused (the message starts with `scope violation:`). |
| 2 | Invalid command-line arguments. |

| Variable | Default | Meaning |
|---|---|---|
| HARNESS_SCOPE_ALLOWLIST | VAmPI lab twins | Permitted host:port entries. An explicitly empty value denies all requests. Lab addressing is independent of LiteLLM. |
| HARNESS_RATE_LIMIT_RPS | 4 | Finite positive local request ceiling. The shared policy can impose a slower rate. |
| HARNESS_KILL_SWITCH | .kill | Stop file. A leading `~` expands to the home directory first, so `~/x.kill` is `$HOME/x.kill`. Other relative paths resolve from the repository root, so `.kill` is `<repo>/.kill`. |
| HARNESS_KILL | unset | Set to 1 in the runner environment to prevent dispatch. Use the file to stop an existing process. |
| HARNESS_DATABASE_URL | unset | Required for live runs. Use the same direct, session-preserving Postgres database for all runners. |
| HARNESS_ARTIFACTS_DIR | artifacts | Directory for private, redacted artifacts and the run report. |

Apply `db/harness.sql` before live use. It creates a shared policy that starts
stopped with a 250 ms minimum interval. The database operator controls
`harness_guardrails.dispatch_policy.stopped` and `min_interval_ms`. The runner
can read the stop signal and reserve a permit, and its role cannot change the
operator's settings. Applying the schema again preserves the policy.

Keep the policy stopped except during an attended run. The operator commands
run as the Postgres superuser on the services box:

    scripts/remote.sh dispatch            # show the state and minimum interval
    scripts/remote.sh dispatch start      # allow dispatch; asks you to type "start"
    scripts/remote.sh dispatch stop       # stop every runner at its next check

`start` requires terminal input and output plus the exact confirmation text.
A program with a pseudoterminal can supply the same input. Treat the prompt
as protection against accidental starts. `stop` runs without a prompt and
returns success only after reading back the stopped state. Run it after each
attended run, and whenever a run needs to stop on every runner.

The gate allows one live run across all runners using the same database. An
engaged local stop at startup exits with code 130 before connecting to Postgres.
A local stop file, a shared stop, or an unavailable policy prevents further
dispatch at a checked boundary. A request already sent can still complete on
the server. A stop arriving just after the final successful check can race
with one request from the single locked runner. Dry runs do not require Postgres.

Live response decoding requires macOS and `/usr/bin/sandbox-exec`. The
orchestrator checks the decoder before sending any requests. It passes only
the current response body to a short-lived process with an empty environment,
restricted filesystem reads, and no network access. The process may see
credentials returned in that response. HTTP dispatch remains in the
orchestrator process.

## Offline safety rehearsal

From `orchestrator/`, use the local Python environment for the standalone check
and the mock-only rehearsal:

    .venv/bin/python -m harness.safety_check
    .venv/bin/python -m unittest discover -s tests -p 'test_safety_rehearsal.py'

The standalone check prints one result per check. It reports the resolved
local stop path and its state when the path can be inspected. It validates any
explicit scope override, including an empty `HARNESS_SCOPE_ALLOWLIST` for
deny-all, and requires a finite positive rate limit. On macOS, it decodes
synthetic JSON and verifies that the decoder sandbox denies file reads outside
its allowlist and denies network operations. An unavailable sandbox fails the
check. The permissions check verifies `0700` on a run directory and `0600` on
an evidence file, creates the configured artifact directory if needed, and
removes its temporary canary even when the permission check fails.

Exit `0` means all local checks passed. Exit `1` means at least one check failed.
These results cover local safety controls. Live dispatch requires separate
authorization and service reachability.

The rehearsal uses a fake shared policy, fake database writer, and HTTP mock
transport. It checks that stops and scope denials block requests and that
synthetic secrets stay out of artifacts, database write arguments, CLI output,
and logs with default and explicit diagnostic levels. A positive signal
produces a `needs_review` verdict. A stopped run leaves the completed signal
summary empty. Both commands run offline.

The [Offline safety workflow](../.github/workflows/offline-safety.yml) runs
the suite and standalone module check on macOS using locked dependencies.
It also tests the dashboard queue and the SQL role boundaries in a disposable
PostgreSQL 17 cluster that listens on a private Unix socket. To run the SQL
tests locally, put PostgreSQL's bin directory on `PATH` and run:

    .venv/bin/python ../db/test_review_permissions.py

## Human verdicts

The runner's database role can insert a probe ID and evidence reference into
`harness.verdicts`. PostgreSQL supplies `needs_review` and prevents that role
from setting or updating the label. Named members of `harness_reviewer` record
decisions through `harness.review_verdict`. The dashboard surfaces the
read-only queue and evidence references. See [Human review](../dashboard/README.md#human-review).

Apply the schema and update the runner in the same maintenance window while
dispatch is stopped. The old runner's explicit label insert will be refused
after the privileges change. Reapplying the schema updates the permissions of
an existing `harness_rw` role even when provisioning supplies no new writer
password.

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
