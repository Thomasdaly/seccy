# Codex review kit for seccy

## 0. Setup (once)

```bash
cd ~/repos/seccy
git add -A -n          # dry run: check nothing secret (.env, artifacts/) would be staged
codex --model gpt-6.2-sol --sandbox read-only --ask-for-approval on-request
# inside the session: /model -> gpt-6.2-sol -> High   (consumer plans start Sol on Light, so bump it)
# /status shows your remaining Plus limits
```

Optional `~/.codex/config.toml`:

```toml
model = "gpt-6.2-sol"
model_reasoning_effort = "high"
review_model = "gpt-6.2-sol"
sandbox_mode = "read-only"
```

## 1. AGENTS.md (optional, lean; repo root)

```markdown
# seccy

Authorized authorization-testing harness. Python 3.13 uv project in `orchestrator/`;
dashboard in `dashboard/`; offline vulnerable lab + ground truth in `lab/`.
Architecture and principles: docs/harness-stack-guide.md (principles 1-5, guardrails §7, eval §8).

- Never read or print `.env`. Never run the harness or curl the lab: it needs the tailnet.
- Guardrails (scope allowlist, rate limit, kill switch) are safety-critical. Treat any bypass as critical.

## Code Review Rules
- Report file:line, severity, and a concrete failure scenario for every finding.
- Prioritise guardrail bypass, secret leakage into evidence/DB/dashboard, and scoring errors over style.
```

## 2. Pass 1: whole-repo audit (gpt-6.2-sol, High)

Paste as a normal prompt, not /review. /review only sees diffs, and most of this code is the base commit.

```text
Goal: find every real defect in this repo that could (a) let the harness send traffic
outside its authorized scope or keep running after it should stop, (b) leak credentials
or secrets into evidence, the DB, logs, or the dashboard, (c) report a finding without
real evidence or mis-score a run, or (d) crash or silently misbehave at runtime. I will
use your report to decide what to fix before pointing this at an authorized real target.

Context: seccy is an authorized API authorization-testing harness. The orchestrator
(orchestrator/src/harness, Python 3.13) runs on a laptop, calls LiteLLM, Postgres and Qdrant
on a second box over Tailscale, and probes an offline vulnerable lab (VAmPI: vulnerable
twin :5061, secure twin :5062). The loop is identities -> map OpenAPI ops -> build probes ->
planner -> replay via Executor -> glue model -> judge model -> dedup -> evidence + verdict
-> score against lab/ground-truth/*.yaml. Read docs/harness-stack-guide.md section 1
(design principles) and lab/README.md (scoring rules) first; they define intended behaviour.
The newest work is the last ~8 commits (git log 3912d09..HEAD); give it extra scrutiny.

Check each of these and give a verdict on each (confirmed bug / not a bug / unsure),
citing file:line. Treat them as hypotheses, not facts:

Guardrails (guardrails.py, executor.py, config.py, __init__.py)
1. Can ScopeAllowlist.host_port (urllib.parse) disagree with how httpx actually resolves
   the URL? Consider userinfo (@), backslashes, uppercase hosts, trailing dots, IPv6,
   missing scheme, hostname None -> "None:80", explicit default ports.
2. Does any outbound network path skip Executor.send and so skip scope + rate limits?
   List every httpx/psycopg/qdrant client in the repo and say whether that is intended.
3. The kill switch is polled only between probes in loop.run. Is it checked during
   identities(), build_probes(), or inside multi-request probes? Worst-case number of
   requests sent after .kill appears? Is a relative ".kill" path cwd-dependent?
4. RateLimiter: is the token-bucket maths right under concurrency and after idle periods?
   Is sleeping while holding the lock a problem?
5. The lab IP and the scope allowlist come from separate sources (LiteLLM host vs
   HARNESS_SCOPE_ALLOWLIST). What happens when they diverge?

Evidence and secrets (executor.py, store.py, targets.py, dashboard/)
6. _mask_headers masks only Authorization. Do passwords in login/register request bodies,
   auth_token values in response bodies, cookies, or /_debug password dumps land unmasked
   in artifacts/, Postgres, or the dashboard? Is artifacts/ gitignored?
7. The dashboard renders target-controlled response bodies. Look for XSS or HTML
   injection in dashboard/static/app.js and in dashboard/backend.py responses. Check that
   the dashboard DB role really is read-only (db/harness.sql).

Finding integrity and scoring (loop.py, models.py, scoring.py, dedup.py, targets.py, lab/ground-truth/)
8. When the glue or judge model errors or returns JSON without the expected key, the loop
   falls back to the deterministic signal. Can a model outage mark findings "confirmed"
   with no frontier judgment? Does that break principle 3 (no evidence, no report)?
9. The judge sees only the observation string, not the captured request/response. Can a
   finding be confirmed when the evidence doesn't prove it?
10. Dedup persists to Qdrant across runs. Does a second run mark true positives as
    duplicates, and how does scoring.py treat duplicates for recall?
11. Do the false-positive and recall rules in lab/README.md match scoring.py exactly
    (secure-twin control, negatives: list, scope: authz vs other)? Do probe finding_ids
    in targets.py match ids in vampi.yaml (it was edited recently)?
12. Probe oracles in targets.py: can any signal be true for the wrong reason, or crash
    when login returns None, the control twin is down, or a body isn't a dict?
13. models.py _cost_of and Usage: can cost be double-counted, read from the wrong field,
    or silently zero, which would distort cost per confirmed bug?
14. store.py SQL vs db/harness.sql: column/type mismatches, swallowed DB errors, and
    what happens to run status when the loop raises mid-run.

Also report anything else you find that fits the goal, even if it's not listed above.

Output:
- First, a findings table sorted by severity (critical / high / medium / low): id, severity,
  file:line, one-line defect, concrete failure scenario (inputs/state -> wrong result),
  confidence (high/med/low), and a fix sketch of one or two lines.
- Then a verdict for each of the 14 checks above. Mark the not-a-bug ones as well, so I
  know they were checked.
- Last, a short list of the 3 changes you would make first.
Done means every numbered check has a verdict backed by code you actually read.

Boundaries: read-only review, don't edit files. Don't read or print .env, and don't run
the harness, docker, or any network command: the lab is only reachable on my tailnet.
Static reading plus local commands like grep, git diff, and python -c "import ast" are fine.
If something can't be decided without running it, say what you'd run and why.
```

## 3. Pass 2: diff review (/review, Sol)

`/review` -> "Custom review instructions" (everything is committed, so "uncommitted changes" would be empty):

```text
Review the diff `git diff 3912d09..HEAD`. Focus on regressions it introduces: dashboard backend
queries and rendering of target-controlled data, models.py cost accounting, compose
port bindings (everything must bind to TAILNET_IP, never 0.0.0.0), and the vampi.yaml
ground-truth edits versus targets.py probe ids.
```

## 4. Pass 3: adversarial verify (/model -> gpt-6.2-astra, Extra High)

Run this in the same session, so Astra sees Pass 1 and 2 output. Otherwise paste the findings in.

```text
Goal: decide which of the findings above are real, and find any guardrail bypass that
was missed. I will fix only what you confirm.

For each finding from the earlier passes, re-read the cited code and mark it CONFIRMED
(show the exact path through the code), REFUTED (say why), or PLAUSIBLE (say what would
settle it). Drop duplicates.

Then attack the guardrails yourself: assume a malicious or buggy probe, target response,
OpenAPI spec, or env var, and try to get a request to a host:port outside
HARNESS_SCOPE_ALLOWLIST, keep sending after the kill switch trips, exceed the rate limit,
or get a finding recorded as confirmed without a replayable request/response proving it.
Files: orchestrator/src/harness/{guardrails,executor,loop,targets,surface,config}.py.

Output: the final, deduplicated findings table in the same columns as before, followed by
any new bypasses, each with a minimal code-level repro. Boundaries are unchanged:
read-only, no network, no .env.
```
