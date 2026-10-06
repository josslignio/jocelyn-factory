#!/usr/bin/env bash
set -euo pipefail

# factoryd_safe_restart.sh — C24 defect 2.
#
# Production fact (2026-09-12): `launchctl kickstart -k com.factory.factoryd`
# was run while a real mission was in flight (V2_IN_FLIGHT=1, a Codex build).
# `kickstart -k` SIGKILLs the whole daemon process group, including the
# dispatcher child it had just spawned — no graceful stop, no drain. The
# result: one dispatch attempt classified KILL, LOST_ATTEMPTS_RECONCILED at
# next boot, and one launch burned off whichever grant paid for it, for
# nothing.
#
# This wrapper is the ONLY sanctioned way to bounce factoryd from now on. It
# reads the daemon's own status (never guesses), sums V2_IN_FLIGHT across
# every project the portfolio reports, counts live dispatch/model-CLI child
# processes as a second, independent signal (V2_IN_FLIGHT is state written by
# the daemon's own loop — a hung write or a stale read must not manufacture a
# false "clear to restart"), and refuses to touch the daemon while either
# signal is nonzero, unless the caller explicitly accepts the loss with
# --force. Fail-closed: any doubt about whether something is in flight keeps
# the daemon alive.
#
# Usage:
#   factoryd_safe_restart.sh [--max-wait-s SECONDS] [--force]
#                             [--run-dir DIR] [--portfolio FILE]
#
# --max-wait-s (default 3600): how long to poll, in 30s steps, before giving
#   up and refusing (rc 2) — or, with --force, proceeding anyway.
# --force: after --max-wait-s is exhausted with something still in flight,
#   restart anyway. Never skips the wait itself, only the final refusal.
#
# Testing hook: FACTORYD_STATUS_CMD, if set, replaces the real
# `python3 factoryd.py status ...` invocation below (via `eval`) — a test
# double stands in for the daemon so this script's decision logic is
# exercised without a real factoryd or real dispatch children.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
FACTORYD_PY="$SCRIPT_DIR/factoryd.py"

RUN_DIR="${FACTORYD_SAFE_RESTART_RUN_DIR:-$HOME/Library/Application Support/JocelynFactory/run}"
PORTFOLIO="${FACTORYD_SAFE_RESTART_PORTFOLIO:-$REPO_ROOT/portfolio.json}"
MAX_WAIT_S=3600
FORCE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --max-wait-s)
      MAX_WAIT_S="$2"; shift 2 ;;
    --force)
      FORCE=1; shift ;;
    --run-dir)
      RUN_DIR="$2"; shift 2 ;;
    --portfolio)
      PORTFOLIO="$2"; shift 2 ;;
    *)
      echo "factoryd_safe_restart: unknown argument: $1" >&2
      exit 64 ;;
  esac
done

# Dispatch/model-CLI process names this line watches for. Kept as one regex so
# the same pattern drives both the code and its docstring/tests.
DISPATCH_PATTERN='dispatcher_v3\.py|codex exec|claude -p|opencode run'

_status_text() {
  # FACTORYD_STATUS_CMD, when set, IS the full command (a string `eval`'d) —
  # the test double for a real factoryd. `|| true`: a NOT_RUNNING daemon
  # exits 1 by design (see cmd_status in factoryd.py); that is itself a
  # legitimate, parseable answer ("nothing in flight"), never a script abort.
  if [[ -n "${FACTORYD_STATUS_CMD:-}" ]]; then
    # Parenthesized: a real subshell fork, not a bare `eval` in the current
    # shell. A test double that calls `exit` (as a real status command's own
    # process boundary would) must only end that subshell, never the script
    # that is trying to catch its nonzero status with `|| true` below.
    ( eval "$FACTORYD_STATUS_CMD" ) 2>&1 || true
  else
    python3 "$FACTORYD_PY" status --run-dir "$RUN_DIR" --portfolio "$PORTFOLIO" 2>&1 || true
  fi
}

# Sets V2_SUM and PROC_COUNT from the current observed state. Two independent
# signals, either one nonzero blocks the restart — V2_SUM is the daemon's own
# accounting (PROJECT_STATE.json, relayed through `status`); PROC_COUNT is an
# OS-level fact that does not depend on the daemon's bookkeeping being fresh.
_observe() {
  local status_out
  status_out="$(_status_text)"
  # a NOT_RUNNING daemon cannot launch anything between two observations —
  # no need for the idle confirmation window in that case
  if printf '%s\n' "$status_out" | grep -q 'FACTORYD_STATUS: NOT_RUNNING'; then
    IDLE_CONFIRM_S=0
  fi
  V2_SUM=$(printf '%s\n' "$status_out" \
    | grep -o 'V2_IN_FLIGHT=[0-9]\+' | cut -d= -f2 \
    | awk '{s+=$1} END{print s+0}') || true
  V2_SUM=${V2_SUM:-0}
  # FACTORYD_DISPATCH_PGREP_CMD, when set, replaces the real `pgrep -f` below —
  # a test double, exactly like FACTORYD_STATUS_CMD above, so a counter-test
  # can assert PROC_COUNT deterministically instead of racing whatever
  # dispatcher/model-CLI processes genuinely happen to be running on the host.
  if [[ -n "${FACTORYD_DISPATCH_PGREP_CMD:-}" ]]; then
    PROC_COUNT=$( ( eval "$FACTORYD_DISPATCH_PGREP_CMD" ) | wc -l | tr -d ' ') || true
  else
    PROC_COUNT=$(pgrep -f "$DISPATCH_PATTERN" | wc -l | tr -d ' ') || true
  fi
  PROC_COUNT=${PROC_COUNT:-0}
}

_in_flight() {
  [[ "$V2_SUM" -gt 0 || "$PROC_COUNT" -gt 0 ]]
}

# 2026-09-17: a single idle observation is a race — a dispatch started between the
# check and the kickstart was killed (rc=-15). Require 3 consecutive idle
# observations 10 s apart (FACTORYD_IDLE_CONFIRM_S) before touching the daemon.
IDLE_CONFIRM_S="${FACTORYD_IDLE_CONFIRM_S:-10}"
_idle_streak() {
  local n=0
  while [[ "$n" -lt 3 ]]; do
    _observe
    if _in_flight; then return 1; fi
    n=$((n + 1)); [[ "$n" -lt 3 && "$IDLE_CONFIRM_S" -gt 0 ]] && sleep "$IDLE_CONFIRM_S"
  done
  return 0
}
_observe
elapsed=0
while _in_flight || ! _idle_streak; do
  echo "[factoryd_safe_restart] in flight: V2_IN_FLIGHT_SUM=$V2_SUM DISPATCH_PROCS=$PROC_COUNT" \
       "(waited ${elapsed}s / max ${MAX_WAIT_S}s)"
  if [[ "$elapsed" -ge "$MAX_WAIT_S" ]]; then
    break
  fi
  sleep 30
  elapsed=$((elapsed + 30))
  _observe
done

if _in_flight; then
  if [[ "$FORCE" -eq 1 ]]; then
    echo "[factoryd_safe_restart] --force set: restarting with work still in flight" \
         "(V2_IN_FLIGHT_SUM=$V2_SUM DISPATCH_PROCS=$PROC_COUNT)." \
         "This WILL kill any live dispatch (KILL attempt, LOST_ATTEMPTS_RECONCILED," \
         "one grant launch burned for nothing)." >&2
  else
    echo "[factoryd_safe_restart] REFUSING restart: still in flight after ${MAX_WAIT_S}s" \
         "(V2_IN_FLIGHT_SUM=$V2_SUM DISPATCH_PROCS=$PROC_COUNT)." \
         "Re-run with --force to override and accept the loss." >&2
    exit 2
  fi
fi

echo "[factoryd_safe_restart] nothing in flight (V2_IN_FLIGHT_SUM=0 DISPATCH_PROCS=0)" \
     "— restarting factoryd"
launchctl kickstart -k "gui/$(id -u)/com.factory.factoryd"

# Best-effort PID report: a second status read, never a second launchctl
# call. If the daemon has not written its PID file back yet, say so plainly
# rather than guessing.
new_status="$(_status_text)"
new_pid="$(printf '%s\n' "$new_status" | grep -o 'PID=[0-9]\+' | head -n1 | cut -d= -f2)" || true
if [[ -n "${new_pid:-}" ]]; then
  echo "[factoryd_safe_restart] restarted: PID=$new_pid"
else
  echo "[factoryd_safe_restart] restart issued; PID not yet observable (daemon still coming up)"
fi
