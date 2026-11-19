# Harness

Authorized authorization-testing harness. Infra runs on the 2060 (WSL2); orchestrator on the M1.
See docs/harness-stack-guide.md. Never commit `.env`.

- `docker-compose.yml`, `litellm.config.yaml` — services on the 2060 (WSL2)
- `orchestrator/` — uv project, agent loop on the M1 (see its README)
- `lab/` — planted-bug targets
- `dashboard/` — read-only console (health, trends, flows, raw) at `http://<TAILNET_IP>:8080`
- `db/harness.sql` — findings schema (runs → verdicts, guardrail events) + the dashboard's read-only role

## 2060 services setup (WSL2)

Ports bind to the Tailscale IP only (`TAILNET_IP` in `.env`), so with WSL mirrored
networking nothing is published on the LAN. Two one-off systemd tweaks (need sudo):

```bash
# Ollama: listen on the docker0 bridge so the LiteLLM container can reach it,
# without exposing it on the LAN or tailnet
sudo systemctl edit ollama
#   [Unit]
#   After=docker.service
#   [Service]
#   Environment="OLLAMA_HOST=172.17.0.1:11434"
echo 'export OLLAMA_HOST=172.17.0.1:11434' >> ~/.bashrc   # so the ollama CLI finds it

# Docker: wait for tailscale0 to have its IP before starting containers bound to it
sudo systemctl edit docker
#   [Unit]
#   After=tailscaled.service
#   Wants=tailscaled.service
#   [Service]
#   ExecStartPre=-/usr/bin/timeout 30 /bin/sh -c 'until ip -4 addr show tailscale0 | grep -q inet; do sleep 1; done'
#   (no `$` — systemd would expand it; leading `-` so a timeout doesn't block Docker)

sudo systemctl daemon-reload && sudo systemctl restart ollama docker
```

Then fill `.env` from `.env.example` and `docker compose up -d`.
