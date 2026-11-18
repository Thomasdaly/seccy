# Harness

Authorized authorization-testing harness. Infra runs on the 2060 (WSL2); orchestrator on the M1.
See docs/harness-stack-guide.md. Never commit `.env`.

- `docker-compose.yml`, `litellm.config.yaml` — services on the 2060 (WSL2)
- `orchestrator/` — uv project, agent loop on the M1 (see its README)
- `lab/` — planted-bug targets
