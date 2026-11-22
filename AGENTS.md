# seccy

Authorized authorization-testing harness. Python 3.13 uv project in `orchestrator/`;
dashboard in `dashboard/`; offline vulnerable lab + ground truth in `lab/`.
Architecture and principles: docs/harness-stack-guide.md (principles 1-5, guardrails §7, eval §8).

- Never read or print `.env`. Never run the harness or curl the lab: it needs the tailnet.
- Guardrails (scope allowlist, rate limit, kill switch) are safety-critical. Treat any bypass as critical.

## Code Review Rules
- Report file:line, severity, and a concrete failure scenario for every finding.
- Prioritise guardrail bypass, secret leakage into evidence/DB/dashboard, and scoring errors over style.
