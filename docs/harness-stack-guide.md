# AI authorization-testing harness — cutting-edge stack & build guide

**Scope:** a model-agnostic harness for **authorized** work only (bug-bounty scopes you're enrolled in, or client targets under a signed SoW). The scope allowlist, rate limiting and kill-switch below are not optional extras — in Australia there is no good-faith research defence, so the harness must refuse any host it wasn't explicitly authorized to touch.

---

## 1. Design principles

1. **The harness is the moat, not the model.** Every model is a swappable endpoint behind one router. When a better/cheaper open model ships, you change one config line.
2. **Three cost tiers.** Local models do high-volume glue for free; hosted *open* models (GLM/DeepSeek/Qwen) do cheap reasoning at ~1/6 frontier price; a frontier model is called only on hard steps or to make the final finding judgment.
3. **No evidence, no report.** Nothing becomes a "finding" until the harness has a replayable request/response pair that proves it. This is what separates earning hunters from the slop that got programs shut down.
4. **Isolate reproduction from credentials.** The box that holds your API keys and session tokens must never be the box that executes untrusted replay steps. (Elastic's public triage design self-destructs the repro VM after 30 minutes for exactly this reason.)
5. **Everything is metered.** You can't manage "cost per accepted report" if you can't see token spend per run.

---

## 2. Hardware roles (two machines + a mesh)

| Machine | Role | Why |
|---|---|---|
| **M1 laptop** | Dev box + orchestrator + local reasoning/embeddings (MLX). Holds the target session tokens | Apple Silicon + MLX is the fastest local inference you own; best place to run the agent loop |
| **RTX 2060 laptop (Windows + WSL2)** | Always-on runner: Ollama, Docker services (router, DBs, tracing), the **reproduction sandbox**, and the offline **target lab** — all inside WSL2 (Ubuntu) | 24/7 x86 worker; 6 GB handles 7–8B glue models + embeddings; WSL2 passes the GPU through to Ollama |

Mesh both with **Tailscale** so they form one private network with no port-forwarding. The orchestrator on the M1 calls the 2060's services over the tailnet, and the offline lab is reachable only inside it.

**Keeping isolation with two boxes.** A third machine would have given the repro sandbox its own host. With two, run it on the 2060 in a **separate throwaway VM** — a second WSL2 distro (Windows) or `runsc`/gVisor (Linux) — on a locked-down network, each instance handed only the single probe to replay, never the target credential store (which stays on the M1) and never the LiteLLM keys. That preserves principle 4.

---

## 3. The stack

| Layer | Tool (2026 cutting-edge) | Role |
|---|---|---|
| Orchestrator | **LangGraph** (stateful graph loop) or a lean custom async loop | Drives plan → probe → observe → judge → report, with retries |
| Model router | **LiteLLM** (proxy mode) | One OpenAI-compatible endpoint; routes each call to local / hosted-open / frontier |
| Local inference | **MLX / LM Studio** (M1), **Ollama** (2060) | Qwen3-8B, GLM-4-9B, `bge-m3` embeddings |
| Hosted open | **OpenRouter** or **DeepInfra / Fireworks** | GLM-4.6, DeepSeek-V3, Qwen3-235B for probe planning |
| Frontier | Anthropic / OpenAI API (Cyber-verified) | Hard chains + final judgment only |
| Identity manager | Custom module | N users × M tenants × roles, their live sessions/tokens — the heart of authz testing |
| Surface mapping | OpenAPI/GraphQL schema ingest, APK endpoint extraction | Builds the operation map the planner walks |
| HTTP engine | **httpx** + **mitmproxy**, or drive **Caido / Burp** via their MCP servers | Capture + replay |
| Browser | **Playwright** | Authenticated login flows, SSO, MFA |
| Vector store | **Qdrant** (Docker) or **LanceDB** (embedded) | Dedup incoming findings by embedding similarity |
| Findings DB | **Postgres** (Docker) | Findings, evidence pointers, run metadata |
| Artifact store | Local FS or **MinIO** | Raw req/resp blobs, screenshots |
| Repro sandbox | Ephemeral **Docker** containers (optionally **gVisor**) | Isolated replay, torn down per run |
| Observability | **Langfuse** (self-host, Docker) | Traces every loop; token + USD cost per run |
| Mesh | **Tailscale** | Private network across the three laptops |

---

## 4. High-level install

### 4.0 Mesh (all machines)
Install Tailscale on each, then `tailscale up` — per OS:
- **M1 (macOS):** `brew install --cask tailscale` (or the App Store app)
- **RTX 2060:** inside WSL2 with `curl -fsSL https://tailscale.com/install.sh | sh` (or the Tailscale Windows installer on the host + WSL2 mirrored networking)

### 4.1 RTX 2060 box (Windows + WSL2) — services runner + sandbox + lab

Run everything from a WSL2 Linux shell; the Windows host only supplies the GPU driver.

```powershell
# elevated PowerShell on Windows (once)
wsl --install -d Ubuntu          # WSL2 + Ubuntu; reboot if asked
```
Install NVIDIA's **"CUDA on WSL"** driver on the Windows host (the standard Game Ready / Studio driver includes it) so the 2060 appears inside WSL2. Do **not** install a Linux NVIDIA driver in the distro — the GPU comes from the host driver.

Everything else runs in the **Ubuntu (WSL2) shell**:
```bash
# enable systemd so Docker and Tailscale run as services
printf '[boot]\nsystemd=true\n' | sudo tee /etc/wsl.conf
#   then, back in PowerShell: `wsl --shutdown`, and reopen Ubuntu

# native Linux installs — the same commands as any Linux box
curl -fsSL https://get.docker.com | sh && sudo usermod -aG docker $USER
curl -fsSL https://ollama.com/install.sh | sh            # CUDA build
curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up

# verify the GPU, then pull models and start services
nvidia-smi
ollama pull qwen3:8b && ollama pull bge-m3
docker compose up -d                                     # litellm, qdrant, postgres, langfuse
docker run -d --name juice -p 3001:3000 bkimminich/juice-shop
# + crAPI, VAmPI, and your own apps with planted authz bugs
```

- **GPU check:** `ollama run qwen3:8b` should load on the 2060; a CPU fallback means the CUDA-on-WSL driver is missing.
- **Networking:** running Tailscale inside the distro puts the services straight on the tailnet. (Alternative: Tailscale on the Windows host + WSL2 **mirrored networking** on Windows 11.) Bind the lab to the tailnet only — never a public interface.
- **Repro sandbox:** install a **second Ubuntu distro** (`wsl --install -d Ubuntu-sandbox`, or import a second copy) and run the ephemeral repro containers there — a clean VM boundary from the services distro, each handed only the single probe.
- Don't want to manage `dockerd`? Install Docker Desktop for Windows and turn on WSL integration for this distro instead; `docker` then works in the shell with GPU support and you can skip the systemd + Docker-Engine step.

**Tune WSL2 as an always-on runner.** Four settings stop Windows and the VM contending for resources and keep the box up headless:

```powershell
# 1. Windows host — resource caps + mirrored networking (size to your laptop)
@"
[wsl2]
memory=12GB            # leave Windows a few GB; this assumes a 16 GB machine
processors=6
swap=8GB
networkingMode=mirrored

[experimental]
autoMemoryReclaim=gradual
sparseVhd=true
"@ | Set-Content "$env:UserProfile\.wslconfig" -Encoding ascii

# 2. Don't let the laptop sleep on AC while it works
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0

# 3. Keep the distro up headless from login (systemd then runs docker/tailscale)
schtasks /create /tn "WSL runner" /sc onlogon /rl highest /f /tr "wsl -d Ubuntu -e sleep infinity"

wsl --update            # ensure a current kernel (needed for systemd + GPU)
wsl --shutdown          # apply .wslconfig
```
```bash
# 4. Inside WSL2 — start the services at boot, then verify after a reboot
sudo systemctl enable --now docker tailscaled
nvidia-smi && docker ps && tailscale status
```
Keep the repo, models and container volumes inside the Linux filesystem (`~/…`), never under `/mnt/c` — the cross-OS path is slow. Adjust `memory`/`processors` to your actual RAM and cores (cap WSL2 at roughly two-thirds, leaving Windows the rest).

`docker-compose.yml` (services box):
```yaml
services:
  litellm:
    image: ghcr.io/berriai/litellm:main-latest
    command: ["--config", "/app/config.yaml", "--port", "4000"]
    volumes: ["./litellm.config.yaml:/app/config.yaml"]
    ports: ["4000:4000"]
    env_file: .env            # OPENROUTER_API_KEY, ANTHROPIC_API_KEY, etc.
  qdrant:
    image: qdrant/qdrant:latest
    ports: ["6333:6333"]
  postgres:
    image: postgres:17
    environment: { POSTGRES_PASSWORD: change-me }
    ports: ["5432:5432"]
  langfuse:
    image: langfuse/langfuse:latest
    ports: ["3000:3000"]
    environment: { DATABASE_URL: postgresql://postgres:change-me@postgres:5432/langfuse }
```

### 4.2 M1 — orchestrator + local reasoning
```bash
# fast Python toolchain
curl -LsSf https://astral.sh/uv/install.sh | sh
uv init harness && cd harness
uv add langgraph litellm httpx playwright qdrant-client psycopg[binary] langfuse
uv run playwright install chromium

# local models on Apple Silicon
brew install ollama   # or LM Studio for MLX-backed serving
ollama pull qwen3:8b
```
Point the harness at the router over the tailnet: `LITELLM_BASE_URL=http://<2060-tailscale-ip>:4000`.

---

## 5. Model routing (LiteLLM `config.yaml`)

```yaml
model_list:
  # Tier 0 — local, free, high-volume glue
  - model_name: glue
    litellm_params: { model: ollama/qwen3:8b, api_base: http://host.docker.internal:11434 }
  - model_name: embed
    litellm_params: { model: ollama/bge-m3, api_base: http://host.docker.internal:11434 }
  # Tier 1 — hosted open, cheap reasoning (~1/6 frontier cost)
  - model_name: planner
    litellm_params: { model: openrouter/z-ai/glm-4.6 }
  # Tier 2 — frontier, hard steps only
  - model_name: judge
    litellm_params: { model: anthropic/claude-<current> }
litellm_settings:
  success_callback: ["langfuse"]     # every call → cost + trace
```
In the harness: classify/normalize/dedup → `glue`; plan probes → `planner`; final finding judgment or a stalled run → `judge`.

---

## 6. The agent loop (abstract — no payloads)

```
build identity set     # N users × M tenants × roles  → live sessions
map operations         # OpenAPI + GraphQL + APK  → operation list
for each operation:
    plan  = planner(operation, identities)          # which identity SHOULD/shouldn't access
    for each probe in plan:
        resp = replay(probe) in SANDBOX             # isolated container
        obs  = glue(resp)                           # normalize, extract signal
        if looks_interesting(obs):
            verdict = judge(probe, obs, identities) # frontier confirms cross-boundary access
            if verdict.confirmed and has_replayable_evidence(probe, resp):
                if not duplicate(embed(obs)):       # Qdrant similarity
                    record_finding(evidence=req_resp_pair)
enforce throughout: scope_allowlist, rate_limit, kill_switch
```
The methodology (same identity, vary the object/tenant, **diff the responses**) is standard BOLA/IDOR authorization testing — the edge is coverage, evidence discipline and dedup, not novel attacks.

---

## 7. Guardrails baked into the harness

- **Scope allowlist:** every outbound request checked against an explicit host/path allowlist derived from the program scope or SoW; anything else is dropped and logged.
- **Rate limiting:** per-target request ceilings matching program rules (HackerOne/Bugcrowd both enforce this; breaching it gets you banned).
- **Kill switch:** one flag halts all runners across the mesh.
- **Data handling:** captured PII stays in the artifact store on your own machines; for client work, nothing leaves the tailnet.

---

## 8. Model improvement — the data flywheel

Don't train a base model, and don't fine-tune an LLM early. The compounding asset is your **labelled corpus + eval harness**, not weights — the harness matters more than the model. Work the ladder cheapest-first:

| Technique | Where it runs | ROI | When |
|---|---|---|---|
| Eval harness vs your planted-bug lab | CPU | Highest | Day one |
| Retrieval memory (past patterns, known-non-issues, threat models) | CPU / M1 | Very high | Week 3+ |
| Few-shot exemplars from confirmed findings | — | High | As findings accrue |
| Small classifier (interesting vs noise) on `bge-m3` or a 1–3B model | M1 / 2060 | High, narrow | After a few hundred labels |
| LoRA/QLoRA of a 7–14B open model | M1 (MLX, slow) or rented GPU | Marginal | Thousands of traces, one task |
| RL (GRPO) on a verifiable reward | Rented H100s | High, costly | Late |

**Log every run** to a clean schema so any later training — or an acquirer — can use it:
```sql
runs(id, ts, target, program)
operations(run_id, method, path, schema_source)
probes(op_id, identity, tenant, role, intent)
exchanges(probe_id, request_blob, response_blob)
verdicts(probe_id, label, severity, is_duplicate, evidence_ref)
```

**The eval harness is the keystone.** Point **promptfoo** (or a custom runner) at your planted-bug lab and score every prompt/model/router change on recall, false-positive rate, and cost per confirmed bug. This is the "training" that actually moves the needle, and it lets you swap in a cheaper model the moment it ties the incumbent.

**The one local weight-training win** is the small classifier that kills glue-tier false positives — trainable on the M1 from your own "interesting / not" labels. A LoRA of a 7–14B model stays a worse *reasoner* than the hosted GLM-4.6 you'd route to anyway, so reserve it for a narrow repetitive task and rent the GPU.

**RL is real but later.** Authorization testing has a rare gift — a *verifiable* reward: in the lab you know the planted ground truth, so a probe either confirmed the cross-tenant read or it didn't. That clean signal suits GRPO, but it needs a solid verifier and serious compute; do it on rented hardware once the eval harness is mature.

**Hard boundary.** Client/target data (PII, tokens, NDA'd responses) must not enter weights or a hosted fine-tune API. Keep a **sanitised, consented training corpus** separate — strip secrets and PII; only own-lab and explicitly-permitted data ever touch training. Authorised to *test* a system is not authorised to *train on* its data.

---

## 9. First-run checklist

- [ ] Tailscale mesh up; services reachable only over the tailnet
- [ ] Langfuse showing token + USD per run
- [ ] Repro sandbox runs in an isolated VM/distro (second WSL2 distro on Windows, or gVisor on Linux) that never receives the target tokens (which stay on the M1) or the LiteLLM keys
- [ ] Harness refuses an out-of-scope host in a dry run
- [ ] Beats a bare `Strix` baseline on your planted-bug lab before you point it at anything real
