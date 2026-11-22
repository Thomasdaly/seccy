#!/usr/bin/env bash
# Operate the 2060 services box from the M1 with terse output.
#
# One-time setup on the M1:
#   ~/.ssh/config:  Host 2060 / HostName <tailscale ip> / User daly /
#                   ControlMaster auto / ControlPath ~/.ssh/cm-%r@%h:%p /
#                   ControlPersist 10m / SetEnv LC_ALL=C.UTF-8 LANG=C.UTF-8
#   docker context create 2060 --docker host=ssh://2060
#   git remote add 2060 2060:harness
# and on the box:
#   git -C ~/harness config receive.denyCurrentBranch updateInstead
#
# Read-only and exec go through the docker context (no compose file needed).
# up/deploy run ON the box: bind mounts (./litellm.config.yaml, lab/ground-truth)
# and .env must resolve there, not on the M1.
set -euo pipefail

HOST=${REMOTE_HOST:-2060}
DIR=${REMOTE_DIR:-harness}
PROJECT=${REMOTE_PROJECT:-harness}
dc() { docker --context "$HOST" compose -p "$PROJECT" "$@"; }
on_box() { ssh "$HOST" "cd ~/$DIR && $*" </dev/null; }

usage() {
  cat <<'EOF'
usage: scripts/remote.sh <cmd> [args]
  status                 one line per service: name state health
  logs <svc> [n]         last n log lines (default 50), no prefix
  errors [svc] [n]       ERROR/WARN/Traceback lines from last n (default 500)
  exec <svc> <cmd...>    run command in container (no TTY)
  restart <svc...>       restart services
  up [svc...]            docker compose up -d --build on the box
  deploy [svc...]        git push 2060 HEAD:master, then up
  sh <cmd...>            run shell command in ~/harness on the box
  rev                    commit checked out on the box vs local HEAD
  dispatch [status]      shared dispatch policy: stopped or allowed, and min interval
  dispatch stop          stop live dispatch for every runner (no prompt)
  dispatch start         allow live dispatch; needs a person at a terminal to confirm
EOF
}

# Shared stop for all runners. Runs as the postgres superuser inside the
# container: the orchestrator's role can read the flag but never change it.
POLICY=harness_guardrails.dispatch_policy
POLICY_SHOW="select case when stopped then 'stopped' else 'dispatch allowed' end || ' min_interval_ms=' || min_interval_ms from $POLICY where singleton"
policy_sql() { dc exec -T postgres psql -U postgres -qtAX -v ON_ERROR_STOP=1 -c "$1" </dev/null; }
policy_show() {
  local state
  local pattern='^(stopped|dispatch allowed) min_interval_ms=[1-9][0-9]*$'
  state=$(policy_sql "$POLICY_SHOW")
  [ -n "$state" ] || { echo "dispatch policy row missing: apply db/harness.sql (runners refuse to dispatch)" >&2; return 1; }
  if [ "${1:-}" = "stopped" ]; then
    if ! [[ "$state" =~ $pattern ]]; then
      echo "dispatch stop verification failed: unexpected policy state" >&2
      return 1
    fi
    if [ "${BASH_REMATCH[1]}" != "stopped" ]; then
      echo "dispatch stop verification failed: dispatch allowed" >&2
      return 1
    fi
  fi
  echo "$state"
}

cmd=${1:-}; [ $# -gt 0 ] && shift
case "$cmd" in
  status)
    dc ps -a --format '{{.Service}} {{.State}} {{.Health}}' | column -t
    ;;
  logs)
    [ $# -ge 1 ] || { usage; exit 2; }
    dc logs --no-log-prefix --tail "${2:-50}" "$1"
    ;;
  errors)
    svc=${1:-}; n=${2:-500}
    args=(logs --tail "$n")
    [ -z "$svc" ] || args+=("$svc")
    lines=$(dc "${args[@]}" 2>&1) || { printf '%s\n' "$lines" >&2; exit 1; }
    matches=$(printf '%s\n' "$lines" | awk '/ERROR|WARN|Traceback|Exception|FATAL|panic/' | tail -40)
    if [ -n "$matches" ]; then
      printf '%s\n' "$matches"
    else
      echo "no errors in last $n lines"
    fi
    ;;
  exec)
    [ $# -ge 2 ] || { usage; exit 2; }
    svc=$1; shift
    dc exec -T "$svc" "$@" </dev/null
    ;;
  restart)
    [ $# -ge 1 ] || { usage; exit 2; }
    dc restart "$@" 2>&1 | tail -n "$#"
    ;;
  up)
    services=""
    if [ "$#" -gt 0 ]; then
      printf -v services ' %q' "$@"
    fi
    on_box "docker compose up -d --build --quiet-pull$services"
    "$0" status
    ;;
  deploy)
    git push "$HOST" HEAD:master
    "$0" up "$@"
    ;;
  sh)
    [ $# -ge 1 ] || { usage; exit 2; }
    on_box "$*"
    ;;
  rev)
    echo "box:   $(on_box 'git log --oneline -1')"
    echo "local: $(git log --oneline -1)"
    dirty=$(on_box 'git status --porcelain --untracked-files=no')
    [ -z "$dirty" ] || { echo "box has uncommitted tracked changes:"; echo "$dirty"; }
    ;;
  dispatch)
    case "${1:-status}" in
      status)
        policy_show
        ;;
      stop)
        policy_sql "update $POLICY set stopped = true where singleton"
        policy_show stopped
        ;;
      start)
        # The terminal prompt protects against accidental activation.
        if [ ! -t 0 ] || [ ! -t 1 ]; then
          echo "dispatch start needs an interactive terminal: a person must confirm live dispatch" >&2
          exit 2
        fi
        read -r -p "Allow live dispatch for every runner using this database? Type 'start' to confirm: " answer
        [ "$answer" = "start" ] || { echo "not started; dispatch remains stopped" >&2; exit 1; }
        policy_sql "update $POLICY set stopped = false where singleton"
        policy_show
        ;;
      *) usage; exit 2 ;;
    esac
    ;;
  *) usage; [ -z "$cmd" ] || exit 2 ;;
esac
