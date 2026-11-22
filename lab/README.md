# lab — offline target range

Intentionally-vulnerable APIs the harness is **measured against**, not attacked
in the wild. They give the eval harness (guide §8) known ground truth so every
prompt/model/router change can be scored on **recall**, **false-positive rate**
and **cost per confirmed bug** before the harness ever points at an authorized
real target.

> **Safety.** These apps are deliberately insecure. Everything here binds to
> `TAILNET_IP` only — reachable on the tailnet, nowhere else. Runs on the 2060
> (guide §4.1), beside Docker and the services; the M1 orchestrator reaches it
> over Tailscale. Never bind to `0.0.0.0`/public, and never point the harness at
> anything outside this lab without an authorized scope + the guardrails on.

## Targets

| Target | Why | Weight | Spec | Ground truth |
|---|---|---|---|---|
| **VAmPI** | Tiny Flask API, OWASP API Top 10, OpenAPI3 spec, and a `vulnerable=0/1` toggle. The default day-to-day target. | ~2 containers, <0.5 GB | `specs/vampi-openapi3.yml` (committed) | `ground-truth/vampi.yaml` |
| **crAPI** | Realistic multi-service app with documented BOLA/BFLA/mass-assignment challenges. The harder, more lifelike run. | ~10 containers, ~2 GB | fetched by `crapi/setup.sh` | `ground-truth/crapi.yaml` |

Chosen over the alternatives (vAPI, Damn Vulnerable RESTaurant, Pixi,
InsecureAPI, DVGA) because these two pair a **machine-readable OpenAPI spec**
(surface mapping) with **documented, per-endpoint authz ground truth** — the two
things the eval needs. VAmPI's secure twin is the clincher: a same-surface
control for false positives that the others don't give.

## Run it (on the 2060)

```bash
cd ~/harness && git pull

# VAmPI — light, default. Vulnerable twin on :5061, secure control on :5062.
docker compose -f lab/docker-compose.lab.yml --profile vampi up -d
curl -s "http://$TAILNET_IP:5061/createdb"      # seed dummy users + books (both twins)
curl -s "http://$TAILNET_IP:5062/createdb"

# crAPI — heavy, opt-in. Fetches upstream compose pinned to a tag, binds to tailnet.
bash lab/crapi/setup.sh up                        # web on :8888
# ... run an eval ...
bash lab/crapi/setup.sh down                      # or `down -v` to wipe data
```

Tear VAmPI down with `docker compose -f lab/docker-compose.lab.yml --profile vampi down`.

## How the ground truth scores a run

Each `ground-truth/*.yaml` lists the known flaws with their OWASP-API category
and a `scope`:

- `scope: authz` — broken object/function-level authorization. **This is what the
  harness is built to find**, so these are the recall denominator.
- `scope: other` — a real bug (SQLi, SSRF, NoSQL, weak-JWT, enumeration) that an
  authorization tester isn't designed to catch. Listed for completeness; a miss
  here is **not** counted against the harness.

A run is scored by:
- **Recall** = confirmed `authz` findings ÷ `authz` findings in the file.
- **False positives** include hits on a flag-gated finding on VAmPI's secure
  twin (`:5062`) and hits on endpoints in a file's `negatives:` list. Findings
  marked `gated: false` can be present on both twins.
- **Cost per confirmed bug** = Langfuse run spend ÷ confirmed findings (guide §8,
  the number to drive down).

The files name *where* a bug is and *which category* — never how to exploit it.
Proof of a finding is the replayable request/response pair the harness captures
at run time, per principle 3 (no evidence, no report).
