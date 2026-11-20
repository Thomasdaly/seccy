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
EOF
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
    # shellcheck disable=SC2086
    dc logs --tail "$n" $svc 2>&1 | grep -E 'ERROR|WARN|Traceback|Exception|FATAL|panic' | tail -40 || echo "no errors in last $n lines"
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
    on_box "docker compose up -d --build --quiet-pull $* 2>&1 | grep -vE '^ *(=>|#[0-9]+|\[\+\])' | tail -20"
    "$0" status
    ;;
  deploy)
    git push "$HOST" HEAD:master 2>&1 | grep -vE '^(To |remote: *$)' || true
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
  *) usage; [ -z "$cmd" ] || exit 2 ;;
esac
