# dashboard

Read-only web console for the 2060 stack: **health, trends, flows, raw data**.
Runs as a compose service on the 2060 and binds to the tailnet IP only, at
`http://<TAILNET_IP>:8080`. Your browser asks for a login: any username, and
`DASHBOARD_PASSWORD` as the password.

```bash
# on the 2060: set DASHBOARD_PASSWORD and DASHBOARD_DB_PASSWORD in .env, then
sudo bash scripts/setup-2060.sh        # applies db/harness.sql, builds + starts the dashboard

# anywhere, no stack needed: synthetic data to see every view populated
cd dashboard && pip install -r requirements.txt
DASHBOARD_DEMO=1 LITELLM_CONFIG=../litellm.config.yaml uvicorn app:app --port 8080
```

FastAPI backend (`backend.py`) plus a static page with no build step and no CDN
(`static/`). Charts are hand-written SVG, so nothing leaves the tailnet.

## What it tracks and why

Each view answers one question, and each metric ties back to a principle in
`docs/harness-stack-guide.md`.

| View | Question | Data |
|---|---|---|
| **Health** | Is the stack up right now? | 30 s probes of LiteLLM, Ollama, Qdrant, Langfuse, Postgres and Langfuse v4's worker, ClickHouse, Redis and MinIO, with 24 h uptime strips. Ollama residency per route: CPU-by-design embeddings are fine, partial offload of qwen3:8b on 6 GB is expected, 0% VRAM on a GPU model means CUDA fallback. Disk, route config. |
| **Trends** | Is it getting cheaper and better? | Spend, calls, tokens, errors and p95 latency per route tier. Exact figures, computed in ClickHouse via Langfuse `v2/metrics`. Unique findings over time and **cost per finding**, the number to drive down (principle 5). |
| **Flows** | Where do the work and money go? | Calls → tokens → spend split per tier (principle 2: glue does the volume, the judge is rare but owns the spend). Agent funnel: operations → probes → executed → interesting → judged → confirmed → evidenced → unique, with the **evidence gate** called out (principle 3). Per-step and per-run spend. Scope, rate-limit and kill-switch events (guide §7). |
| **Raw** | What's behind the numbers? | Langfuse generations and root traces (cursor-paged `v2/observations`), every harness table, Qdrant points, and a read-only SQL console. |

The **Stack checks** panel turns those principles into pass/fail checks:
services reachable, routes concrete (it flags placeholder models like `claude-<current>`),
local models resident, tracing flowing, judge ≤10% of calls, no confirmed verdict
without evidence, scope denials, kill switch, disk.

## Orchestrator contract

The dashboard only reads. Three things make the Flows view work:

1. **Write the run schema** in `db/harness.sql` (guide §8 plus `guardrail_events`).
   A finding is `verdicts.label = 'confirmed'` with an `evidence_ref` and
   `is_duplicate = false`.
2. **Tag every LiteLLM call** with its run and loop step:
   ```python
   litellm.completion(model="judge", messages=...,
       metadata={"session_id": run_id, "generation_name": "judge_finding"})
   ```
   `session_id` joins Langfuse spend to `harness.runs.id` (per-run cost, cost per
   finding). `generation_name` names the rows in the Steps table: use names like
   `classify_op`, `plan_probes`, `observe`, `judge_finding`, `dedup_embed`.
3. **Log guardrails** as `guardrail_events` rows: `scope_denied` and `rate_limited`
   with the host, and `kill_switch` with detail `engaged …` or `released`.

## Security

- Bound to `TAILNET_IP` like the other services. Basic auth on every route
  except `/healthz`. Strict CSP (`default-src 'self'`); untrusted strings
  (generations, payloads, hosts) are rendered with `textContent` only.
- Gets only the secrets it reads with (no `env_file`): the LiteLLM key for the
  route list, the Langfuse project keys, and its own DB password.
- Postgres access is the `dashboard_ro` role: `SELECT` on schema `harness` only,
  `default_transaction_read_only`, 5 s statement timeout. No grants on the
  `langfuse` database. The SQL console also allows one read statement at a time
  and runs it in a read-only transaction that is rolled back.
- **Probe routes** on the Health tab calls LiteLLM `/health`, which sends one
  tiny real request per route. Hosted routes cost a fraction of a cent.

## Config

| Env | Default | |
|---|---|---|
| `DASHBOARD_PASSWORD` | — | Basic-auth password. If empty, auth is off and a warning is logged. |
| `DASHBOARD_DATABASE_URL` | — | `postgresql://dashboard_ro:…@postgres:5432/postgres` |
| `LITELLM_URL`, `LITELLM_MASTER_KEY` | `http://litellm:4000` | |
| `LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | `http://langfuse:3000` | v4 API (`v2/metrics`, `v2/observations`) |
| `LANGFUSE_UI_URL` | `LANGFUSE_HOST` | for "Open in Langfuse" links |
| `QDRANT_URL`, `OLLAMA_URL` | compose names | |
| `PROBE_LANGFUSE_WORKER`, `PROBE_CLICKHOUSE`, `PROBE_REDIS`, `PROBE_MINIO` | unset = not probed | liveness only, no credentials |
| `LITELLM_CONFIG` | `litellm.config.yaml` | used for route→tier mapping when LiteLLM is down |
| `DASHBOARD_DEMO=1` | off | synthetic data |

Uptime history is in memory, so it resets when the container restarts. Everything else
is read live from the sources, with 30–60 s caching.
