# orchestrator

The agent loop (guide §6). Runs on the M1; drives the 2060's services (LiteLLM,
Qdrant, Postgres, Langfuse) over Tailscale and scores itself against the offline
lab's ground truth.

```bash
cp ../.env.example ../.env   # set LITELLM_BASE_URL + LITELLM_MASTER_KEY (2060 values)
uv sync

uv run harness --target vampi            # run against the VAmPI lab, then score
uv run harness --target vampi --dry-run  # map surface + build probes, no model calls
```

## What a run does

```
build identities      target logs in the seeded users (A, B, admin)
map operations        OpenAPI spec -> operations; path-param ops = object refs
for each probe:
    plan   = planner(op, identities)        # tier 1: who may / may not access
    replay = execute(probe)                 # captured req/resp = evidence
    obs    = glue(replay)                   # tier 0: normalize, looks_interesting
    if interesting:
        verdict = judge(probe, rule, obs)   # tier 2: confirm across the boundary
        if confirmed and not duplicate(embed(obs)):   # tier 0 embed + Qdrant
            finding
enforce throughout: scope allowlist, rate limit, kill switch (guide §7)
```

Every model call is tagged `metadata={session_id: run_id, generation_name: step}`
so Langfuse joins spend to the run and the dashboard's Flows/Steps populate. The
run prints recall, false-positive count and cost per confirmed bug, and writes
evidence + a report JSON to `../artifacts/<run_id>/`.

## Config (env)

| Var | Default | |
|---|---|---|
| `LITELLM_BASE_URL`, `LITELLM_MASTER_KEY` | `http://100.102.105.11:4000` | the router |
| `HARNESS_SCOPE_ALLOWLIST` | the two VAmPI twins | host:port the harness may touch; everything else is dropped + logged |
| `HARNESS_RATE_LIMIT_RPS` | `4` | per-run request ceiling |
| `HARNESS_KILL` / `HARNESS_KILL_SWITCH` | — / `.kill` | env=1 or a file halts the run |
| `HARNESS_DATABASE_URL` | unset | when set, writes the `harness.*` schema the dashboard reads; the run scores without it |
| `QDRANT_URL` | unset | persist dedup vectors (else in-memory for the run) |
| `HARNESS_ARTIFACTS_DIR` | `artifacts` | evidence + report output |

## Baseline (VAmPI)

First measured run — the "before results" to improve against:

```
Recall (authz):   4/4 = 100%   (BOLA books, mass-assign admin, BFLA password, excessive _debug)
False positives:  0            (secure twin diverged on every gated probe)
Spend:            ~$0.010/run  (judge ~67%, planner ~33%, glue+embed $0 local)
Cost per bug:     ~$0.0026
```

## Still stubbed / next

- `HARNESS_DATABASE_URL` is unset by default, so runs don't yet populate the
  dashboard's Flows + lab-eval views. Point it at a Postgres writer role to wire that.
- Replay runs in-process; production wants the isolated repro sandbox (guide §4.1,
  principle 4) that never holds the target creds or the LiteLLM keys.
- Only the VAmPI target is implemented; crAPI is next (its ground truth is in
  `lab/ground-truth/crapi.yaml`).
