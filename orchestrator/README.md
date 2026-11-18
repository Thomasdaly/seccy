# orchestrator

Agent loop for the harness. Runs on the M1; talks to the 2060 services (LiteLLM, Qdrant, Postgres, Langfuse) over Tailscale.

```bash
cp ../.env.example ../.env   # set LITELLM_BASE_URL=http://<2060-tailscale-ip>:4000
uv sync
uv run playwright install chromium
uv run harness
```
