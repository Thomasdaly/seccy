#!/usr/bin/env bash
# Fetch and start crAPI as a tailnet-only target on the 2060. crAPI ships its own
# ~10-service compose (Postgres, Mongo, Chroma, gateway, 4 app services), so this
# pulls it upstream pinned to a tag rather than vendoring a stale copy, then binds
# every published port to the tailnet via crAPI's own LISTEN_IP knob.
#
#   cd ~/harness && bash lab/crapi/setup.sh up      # fetch + start
#   bash lab/crapi/setup.sh down                     # stop, keep volumes
#   bash lab/crapi/setup.sh down -v                  # stop + wipe data
#
# Heavy: ~2 GB RAM with the limits crAPI sets. Bring it up only when running an
# eval against it; leave VAmPI (lab/docker-compose.lab.yml) as the day-to-day
# target. Ground truth: lab/ground-truth/crapi.yaml.
set -euo pipefail

REPO=$(cd "$(dirname "$0")/../.." && pwd)
HERE="$REPO/lab/crapi"
UP="$HERE/upstream"
REF="${CRAPI_REF:-main}"          # upstream git ref for the compose file
VERSION="${CRAPI_VERSION:-test}"  # image tag; override once you pin digests
RAW="https://raw.githubusercontent.com/OWASP/crAPI/$REF/deploy/docker"

# Tailnet IP: bind crAPI to it, never to the LAN. Reuse the services .env.
TAILNET_IP="${TAILNET_IP:-$(grep -m1 '^TAILNET_IP=' "$REPO/.env" 2>/dev/null | cut -d= -f2-)}"
[ -n "${TAILNET_IP:-}" ] || { echo "set TAILNET_IP (in $REPO/.env or the environment)"; exit 1; }

cmd="${1:-up}"

case "$cmd" in
  up)
    mkdir -p "$UP/keys"
    echo "== fetching crAPI compose (ref=$REF)"
    curl -fsSL "$RAW/docker-compose.yml" -o "$UP/docker-compose.yml"
    # crAPI needs a JWKS key file mounted at ./keys for the identity service.
    curl -fsSL "https://raw.githubusercontent.com/OWASP/crAPI/$REF/deploy/docker/keys/jwks.json" \
      -o "$UP/keys/jwks.json" 2>/dev/null || true

    echo "== fetching OpenAPI spec for surface mapping"
    curl -fsSL "https://raw.githubusercontent.com/OWASP/crAPI/$REF/openapi-spec/crapi-openapi-spec.json" \
      -o "$REPO/lab/specs/crapi-openapi3.json"

    # Our env overlay: bind to the tailnet, pin the image tag, TLS off so the
    # harness talks plain HTTP. Injection/log4j challenges stay off — this lab
    # run is for authorization findings.
    cat > "$UP/.env" <<EOF
LISTEN_IP="$TAILNET_IP"
VERSION=$VERSION
TLS_ENABLED=false
ENABLE_SHELL_INJECTION=false
ENABLE_LOG4J=false
IDENTITY_SERVER_PORT=8080
COMMUNITY_SERVER_PORT=8087
WORKSHOP_SERVER_PORT=8000
CHATBOT_SERVER_PORT=5002
LOG_LEVEL=INFO
EOF
    echo "== starting crAPI (tailnet $TAILNET_IP, web on :8888)"
    docker compose --project-name harness-crapi --project-directory "$UP" \
      -f "$UP/docker-compose.yml" --env-file "$UP/.env" up -d
    echo "crAPI web: http://$TAILNET_IP:8888  (register two users for the A/B object pair)"
    ;;
  down)
    shift || true
    docker compose --project-name harness-crapi --project-directory "$UP" \
      -f "$UP/docker-compose.yml" down "$@"
    ;;
  *)
    echo "usage: $0 {up|down [-v]}"; exit 1
    ;;
esac
