#!/usr/bin/env bash
# One-off services setup for the 2060 (WSL2 Ubuntu). Idempotent; safe to re-run.
#   cd ~/harness && git pull && sudo bash scripts/setup-2060.sh
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
REPO=$(cd "$(dirname "$0")/.." && pwd)
USER_HOME=$(getent passwd "${SUDO_USER:-root}" | cut -d: -f6)
OLLAMA_ADDR=172.17.0.1:11434

step() { printf '\n== %s\n' "$*"; }

step "disk"
df -h /
if dmesg 2>/dev/null | grep -qiE 'i/o error|ext4-fs error'; then
  dmesg | grep -iE 'i/o error|ext4-fs error' | tail -5
  echo "disk errors in dmesg — stopping"; exit 1
fi

step "systemd overrides (ollama on docker0, docker after tailscale)"
mkdir -p /etc/systemd/system/ollama.service.d /etc/systemd/system/docker.service.d
cat > /etc/systemd/system/ollama.service.d/override.conf <<EOF
[Unit]
After=docker.service
[Service]
Environment="OLLAMA_HOST=$OLLAMA_ADDR"
# keep models resident: a qwen3:8b reload from the WSL disk takes ~90s
Environment="OLLAMA_KEEP_ALIVE=-1"
EOF
cat > /etc/systemd/system/docker.service.d/override.conf <<'EOF'
[Unit]
After=tailscaled.service
Wants=tailscaled.service
[Service]
ExecStartPre=-/usr/bin/timeout 30 /bin/sh -c 'until ip -4 addr show tailscale0 | grep -q inet; do sleep 1; done'
EOF
grep -q OLLAMA_HOST "$USER_HOME/.bashrc" || echo "export OLLAMA_HOST=$OLLAMA_ADDR" >> "$USER_HOME/.bashrc"
systemctl daemon-reload
systemctl restart docker
systemctl restart ollama
for _ in $(seq 15); do ss -tln | grep -q "$OLLAMA_ADDR" && break; sleep 1; done
ss -tln | grep -q "$OLLAMA_ADDR" || { echo "ollama not listening on $OLLAMA_ADDR"; journalctl -u ollama -n 20 --no-pager; exit 1; }
echo "ollama on $OLLAMA_ADDR"

step "ollama models"
export OLLAMA_HOST=$OLLAMA_ADDR
for m in qwen3:8b bge-m3; do
  ollama list | awk 'NR>1{print $1}' | grep -qE "^${m}(:latest)?$" || ollama pull "$m"
done
# CPU-only embedding model: qwen3:8b already overflows the 6 GB GPU, and letting
# bge-m3 share it makes Ollama evict and reload models (20-90s per switch).
printf 'FROM bge-m3\nPARAMETER num_gpu 0\n' > /tmp/Modelfile.bge-m3-cpu
ollama create bge-m3-cpu -f /tmp/Modelfile.bge-m3-cpu >/dev/null
ollama list

step "compose"
cd "$REPO"
[ -f .env ] || { echo "missing $REPO/.env (copy .env.example and fill it)"; exit 1; }
docker compose pull --ignore-buildable   # dashboard is built locally
docker compose up -d --wait postgres
docker compose exec -T postgres psql -U postgres -tAc "SELECT 1 FROM pg_database WHERE datname='langfuse'" | grep -q 1 \
  || docker compose exec -T postgres createdb -U postgres langfuse
# Harness findings schema + the dashboard's read-only role (idempotent)
DB_RO=$(grep '^DASHBOARD_DB_PASSWORD=' .env | cut -d= -f2-)
[ -n "$DB_RO" ] || { echo "set DASHBOARD_DB_PASSWORD in .env"; exit 1; }
docker compose exec -T postgres psql -U postgres -q -v ON_ERROR_STOP=1 -v ro_password="$DB_RO" < db/harness.sql
docker compose up -d --build --remove-orphans

step "health (waiting up to 3 min for langfuse + clickhouse migrations)"
IP=$(tailscale ip -4)
KEY=$(grep '^LITELLM_MASTER_KEY=' .env | cut -d= -f2-)
wait_for() { # name url
  for _ in $(seq 90); do
    code=$(curl -s -o /dev/null -w '%{http_code}' -m 3 "$2" || true)
    [ "$code" = 200 ] && { echo "$1: ok"; return 0; }
    sleep 2
  done
  echo "$1: FAILED (last http $code)"; return 1
}
fail=0
wait_for litellm  "http://$IP:4000/health/liveliness"  || fail=1
wait_for qdrant   "http://$IP:6333/healthz"            || fail=1
wait_for langfuse "http://$IP:3000/api/public/health"  || fail=1
wait_for dashboard "http://$IP:8080/healthz"           || fail=1

step "glue route: litellm -> ollama"
curl -s -m 120 "http://$IP:4000/v1/chat/completions" \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"glue","max_tokens":20,"messages":[{"role":"user","content":"reply with: ok"}]}' \
  | head -c 400 || fail=1
echo

docker compose ps
[ "$fail" = 0 ] && echo "ALL OK" || { echo "SOME CHECKS FAILED — see: docker compose logs --tail 30 <service>"; exit 1; }
