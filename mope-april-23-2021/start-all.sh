#!/usr/bin/env bash
# start-all.sh -- install, configure, start and maintain the local mope.io stack (Phase 6, task 35).
#
# Usage: ./start-all.sh [command] [options]
#
#   start            (default) set up if needed, start every service in the background, wait until
#                    they are healthy and show the status table
#   up               like start, but stay in the foreground; Ctrl-C stops everything gracefully
#   stop [svc...]    graceful shutdown: regions tell connected players and deregister from the master
#   restart [svc...] restart everything, or only the named services (master, account, web, regions,
#                    or a region id such as prod-lnd-us-ny-1)
#   status           service table (process, port, master view, players)
#   logs [svc] [-n N] [-f] [--debug]
#                    central logs (logs/services/*.log): the last N lines of every service merged by
#                    time (INFO and above unless --debug), or follow them live with -f
#   setup            only install / check: Python venv + requirements, .env, configuration, snapshot,
#                    optional tools (Bun, Ruby, Chromium)
#   doctor           check the environment without changing anything
#   verify           unit tests (Python, Bun, Ruby) + phase6_verify.py against the running stack
#
# Environment: MOPE_VENV (virtualenv to use; default ./.venv, then ../.venv), PYTHON (interpreter used
# to create the venv), MOPE_START_TIMEOUT (seconds to wait for health, default 60).
set -Eeuo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT"
LOG_DIR="$PROJECT/logs"
SCRIPT_LOG="$LOG_DIR/start-all.log"
mkdir -p "$LOG_DIR/services"

if [[ -t 1 ]]; then B=$'\e[1m'; G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; N=$'\e[0m'; else B=""; G=""; Y=""; R=""; N=""; fi
log()  { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >>"$SCRIPT_LOG"; }
say()  { printf '%s\n' "$*"; log "$*"; }
ok()   { printf '  %s[ok]%s   %s\n' "$G" "$N" "$*"; log "ok: $*"; }
warn() { printf '  %s[warn]%s %s\n' "$Y" "$N" "$*"; log "warn: $*"; }
fail() { printf '  %s[fail]%s %s\n' "$R" "$N" "$*" >&2; log "fail: $*"; }
die()  { fail "$*"; exit 1; }
trap 'fail "start-all.sh: command failed at line $LINENO: $BASH_COMMAND"' ERR

# -- Python environment -----------------------------------------------------------------------------
find_python() {
  local candidate
  for candidate in "${PYTHON:-}" python3.13 python3.12 python3.11 python3; do
    [[ -n "$candidate" ]] || continue
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      command -v "$candidate"; return 0
    fi
  done
  return 1
}

find_venv() {
  local dir
  for dir in "${MOPE_VENV:-}" "$PROJECT/.venv" "$PROJECT/../.venv"; do
    [[ -n "$dir" && -x "$dir/bin/python" ]] && { (cd "$dir" && pwd); return 0; }
  done
  return 1
}

VENV=""
PY=""
use_venv() {
  if VENV="$(find_venv)"; then PY="$VENV/bin/python"; return 0; fi
  return 1
}

ensure_venv() {
  if use_venv; then
    ok "Python environment: $VENV ($("$PY" -c 'import platform; print(platform.python_version())'))"
  else
    local base
    base="$(find_python)" || die "Python 3.11 or newer is required (set PYTHON=/path/to/python3)"
    VENV="${MOPE_VENV:-$PROJECT/.venv}"
    say "  creating virtualenv $VENV with $base"
    "$base" -m venv "$VENV"
    PY="$VENV/bin/python"
    ok "Python environment created: $VENV"
  fi
  local stamp="$VENV/.mope-requirements.sha256" want have=""
  want="$(sha256sum requirements.txt | cut -d' ' -f1)"
  [[ -f "$stamp" ]] && have="$(cat "$stamp")"
  if [[ "$want" != "$have" ]] || ! "$PY" -c 'import websockets, requests, tree_sitter' 2>/dev/null; then
    say "  installing Python requirements (pip install -r requirements.txt)"
    "$PY" -m pip install --quiet --upgrade pip >>"$SCRIPT_LOG" 2>&1 || true
    "$PY" -m pip install --quiet -r requirements.txt >>"$SCRIPT_LOG" 2>&1 || die "pip install failed (see $SCRIPT_LOG)"
    echo "$want" >"$stamp"
    ok "Python requirements installed"
  else
    ok "Python requirements up to date"
  fi
}

# -- configuration and data ---------------------------------------------------------------------------
ensure_config() {
  if [[ ! -f .env ]]; then
    "$PY" localstack.py init >>"$SCRIPT_LOG" 2>&1 && ok ".env created with documented defaults (edit it to change settings)"
  else
    ok ".env present"
  fi
  local out
  if out="$("$PY" localstack.py check-config 2>&1)"; then
    ok "configuration valid ($(printf '%s\n' "$out" | tail -n 1))"
  else
    printf '%s\n' "$out" | tail -n 15 >&2
    die "configuration invalid: fix config/local_stack.json or .env (python localstack.py check-config)"
  fi
}

config_value() {  # config_value <python expression on the loaded config `c`>
  "$PY" - "$1" <<'PYEOF'
import sys
sys.path.insert(0, ".")
from services import common
c = common.load_stack_config()
print(eval(sys.argv[1], {"c": c, "common": common}))
PYEOF
}

check_data() {
  local root
  root="$(config_value 'common.project_path(c, c["client_root"])')"
  if [[ -f "$root/index.html" && -f "$root/client.js" ]]; then
    ok "archived client: $root"
  else
    fail "archived client not found in $root"
    say "    fetch it once with:"
    say "      $PY fetch_mope_archive.py --date 2025-12-10 --output snapshots/2025-12-10 --provider arquivo --all"
    say "      $PY rebuild_client_js.py --output snapshots/2025-12-10 --timestamp 20251210042757 --provider arquivo"
    return 1
  fi
  if [[ -d archive/images/files ]]; then
    ok "archived images: $(find archive/images/files -type f | wc -l | tr -d ' ') files served after the snapshot"
  else
    warn "archived images not fetched yet (optional): bun install && bun decode_images.ts"
  fi
}

check_hosts() {
  local host addr
  host="$(config_value 'c["public_host"]')"
  addr="$(getent hosts "$host" 2>/dev/null | awk '{print $1; exit}' || true)"
  if [[ "$addr" == "127.0.0.1" || "$addr" == "::1" ]]; then
    ok "$host resolves to $addr"
  else
    warn "$host does not resolve to this machine; add to your hosts file:  127.0.0.1 $host"
  fi
}

check_tools() {
  if command -v bun >/dev/null 2>&1; then ok "bun $(bun --version) (decode_images.ts)"; else warn "bun not installed (only needed for decode_images.ts)"; fi
  if command -v ruby >/dev/null 2>&1; then ok "ruby $(ruby -e 'print RUBY_VERSION') (fetch_live_assets.rb)"; else warn "ruby not installed (only needed for fetch_live_assets.rb)"; fi
  if "$PY" -c 'import playwright' 2>/dev/null; then ok "playwright (browser checks in the verifiers)"; else warn "playwright not importable (browser checks will fail)"; fi
}

check_ports() {
  local busy=0 line
  while read -r line; do
    [[ -z "$line" ]] && continue
    local name="${line%% *}" port="${line##* }"
    if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
      if ! "$PY" localstack.py status 2>/dev/null | grep -E "^$name +[0-9]+ +$port +running" >/dev/null; then
        warn "port $port ($name) is used by another program"; busy=1
      fi
    fi
  done < <(config_value '"\n".join(f"{s.name} {s.port}" for s in __import__("localstack").services(c))')
  [[ $busy -eq 0 ]] && ok "service ports free or used by this stack"
  return 0
}

setup() {
  say "${B}Setup${N}"
  ensure_venv
  ensure_config
  check_data || die "archived client missing (see above)"
  check_hosts
  check_tools
}

# -- service control ----------------------------------------------------------------------------------------
wait_healthy() {
  local timeout="${MOPE_START_TIMEOUT:-60}" start=$SECONDS table
  while (( SECONDS - start < timeout )); do
    table="$("$PY" localstack.py status 2>&1 || true)"
    if ! grep -qE ' (stopped|not running|starting|offline|unreachable)' <<<"$table" && grep -q " running" <<<"$table"; then
      return 0
    fi
    sleep 1
  done
  return 1
}

cmd_start() {
  setup
  say "${B}Starting services${N}"
  "$PY" localstack.py start all 2>&1 | tee -a "$SCRIPT_LOG"
  if wait_healthy; then
    ok "all services healthy"
  else
    warn "not every service is healthy yet; see ./start-all.sh status and ./start-all.sh logs"
  fi
  "$PY" localstack.py status 2>&1 | tee -a "$SCRIPT_LOG"
}

cmd_up() {
  setup
  say "${B}Starting services in the foreground (Ctrl-C stops everything)${N}"
  exec "$PY" localstack.py up
}

cmd_logs() {
  local service="" lines=40 follow=0 level=' (INFO|WARNING|ERROR|CRITICAL) '
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -n) lines="$2"; shift 2 ;;
      -f|--follow) follow=1; shift ;;
      -d|--debug) level=' '; shift ;;
      *) service="$1"; shift ;;
    esac
  done
  local pattern="$LOG_DIR/services/${service:+$service}*.log"
  shopt -s nullglob
  local files=($pattern)
  [[ ${#files[@]} -gt 0 ]] || die "no logs match ${service:-any service} in $LOG_DIR/services"
  if [[ $follow -eq 1 ]]; then
    tail -n "$lines" -F "${files[@]}" | grep --line-buffered -E "$level|^==> "
    return
  fi
  # merge: every service's last N matching lines, ordered by their timestamp prefix
  local f
  for f in "${files[@]}"; do grep -E "$level" "$f" | tail -n "$lines" || true; done | sort -s -k1,2 | tail -n "$lines"
}

cmd_verify() {
  use_venv || die "no Python environment yet: run ./start-all.sh setup"
  say "${B}Unit tests${N}"
  "$PY" -m unittest discover -s tests 2>&1 | tail -n 3
  if command -v bun >/dev/null 2>&1 && [[ -d node_modules ]]; then bun test tests/ 2>&1 | grep -E "pass|fail" | head -n 2; fi
  if command -v ruby >/dev/null 2>&1; then
    ruby tests/test_live_assets.rb 2>&1 | tail -n 1
    ruby tests/test_pumpkins.rb 2>&1 | tail -n 1
  fi
  say "${B}Integration checks (phase6_verify.py)${N}"
  "$PY" phase6_verify.py "$@"
}

cmd_doctor() {
  say "${B}Doctor${N}"
  if use_venv; then ok "Python environment: $VENV"; else warn "no Python environment yet (./start-all.sh setup creates one)"; return 0; fi
  "$PY" -c 'import websockets, requests, tree_sitter' 2>/dev/null && ok "Python requirements importable" || warn "Python requirements missing (./start-all.sh setup)"
  [[ -f .env ]] && ok ".env present" || warn ".env missing (./start-all.sh setup creates it)"
  "$PY" localstack.py check-config >/dev/null 2>&1 && ok "configuration valid" || warn "configuration invalid (python localstack.py check-config)"
  check_data || true
  check_hosts
  check_tools
  check_ports
}

usage() { sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; }

main() {
  local command="${1:-start}"
  [[ $# -gt 0 ]] && shift
  log "start-all.sh $command $*"
  case "$command" in
    start)   cmd_start ;;
    up)      cmd_up ;;
    stop)    use_venv || die "no Python environment"; "$PY" localstack.py stop "${@:-all}" 2>&1 | tee -a "$SCRIPT_LOG" ;;
    restart) use_venv || die "no Python environment"; "$PY" localstack.py restart "${@:-all}" 2>&1 | tee -a "$SCRIPT_LOG"
             wait_healthy && ok "all services healthy" || warn "not every service is healthy yet"
             "$PY" localstack.py status ;;
    status)  use_venv || die "no Python environment"; "$PY" localstack.py status ;;
    logs)    cmd_logs "$@" ;;
    setup)   setup ;;
    doctor)  cmd_doctor ;;
    verify)  cmd_verify "$@" ;;
    -h|--help|help) usage ;;
    *) usage; die "unknown command: $command" ;;
  esac
}

main "$@"
