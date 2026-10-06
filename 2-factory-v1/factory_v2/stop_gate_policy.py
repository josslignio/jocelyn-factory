"""Phase 1: Typed stop-gate policy.

Two mutually exclusive stop contexts:

ONE_TIME_EXTERNAL_MUTATION_GATE
    The next required action is an irreversible external authorization that cannot
    be autonomously resolved by the Factory:
        - first-time launchctl bootstrap of a new service
        - new credential acquisition
        - payment / financial authorization
        - irreversible destructive action requiring explicit human approval
        - production deployment approval

    Human GO is legitimate here. session exit is allowed.

ROUTINE_RUNTIME_CONTINUATION
    All other work: roadmap continuation, next milestone, LR5→LR6, foreground
    qualification, starting workers, starting replacement AI sessions, crash recovery,
    REPLAN, resuming Twitter Radar, routine operation of an already-authorized service.

    Human GO is NEVER required. Session exit is allowed only when factoryd (the
    durable owner) is live and has a persisted durable handoff.

Invariants enforced by this module:
    VALID_CHECKPOINT_AS_CAMPAIGN_COMPLETE = 0
    SESSION_EXIT_WITHOUT_DURABLE_HANDOFF = 0
    OPEN_ROADMAP_WITHOUT_LIVE_OWNER = 0
    READY_CANONICAL_WORK_REQUIRES_HUMAN_GO = 0
    LONG_RUN_READY_BUT_NOT_STARTED = 0
"""
from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import NamedTuple
from factory_v2._io import _now

# Reasons that qualify as ONE_TIME_EXTERNAL_MUTATION_GATE.
# Must match HUMAN_GATE_WHITELIST in continuation_controller.py.
EXTERNAL_GATE_REASONS: frozenset[str] = frozenset({
    "MISSING_CREDENTIAL",
    "EXTERNAL_PERMISSION_REQUIRED",
    "IRREVERSIBLE_ACTION_AUTHORIZATION",
    "PAYMENT_OR_FINANCIAL_AUTHORIZATION",
    "PRODUCTION_DEPLOYMENT_APPROVAL",
    "LAUNCHCTL_BOOTSTRAP_REQUIRED",
    "GENUINELY_SUBJECTIVE_PRODUCT_DECISION",
})

# Work that is NEVER a human gate — always ROUTINE_RUNTIME_CONTINUATION.
NEVER_HUMAN_GATE: frozenset[str] = frozenset({
    "roadmap_continuation",
    "next_milestone",
    "foreground_qualification",
    "worker_start",
    "session_replacement",
    "crash_recovery",
    "replan",
    "twitter_radar_resume",
    "routine_authorized_service",
    "lr5_to_lr6",
    "long_run_start",
})


class StopContextType(Enum):
    ONE_TIME_EXTERNAL_MUTATION_GATE = "ONE_TIME_EXTERNAL_MUTATION_GATE"
    ROUTINE_RUNTIME_CONTINUATION = "ROUTINE_RUNTIME_CONTINUATION"


class StopContext(NamedTuple):
    context_type: StopContextType
    reason: str
    gate_reason: str | None  # only set for ONE_TIME_EXTERNAL_MUTATION_GATE
    has_live_owner: bool       # factoryd live
    has_durable_handoff: bool  # handoff file persisted
    open_roadmap: bool         # roadmap has remaining non-complete milestones
    agent_go_requests: int     # count of agent prose asking for GO (informational only)


def classify_stop_context(
    project_dir: Path,
    portfolio_dir: Path | None = None,
    factoryd_pid_file: Path | None = None,
) -> StopContext:
    """Classify the current stop context as external gate or routine continuation.

    Reads PAUSE.json for external gate, PROJECT_STATE.json for roadmap state,
    factoryd PID file for live ownership.
    """
    # Check for human-written PAUSE.json with an external gate reason.
    pause_file = project_dir / "PAUSE.json"
    gate_reason: str | None = None
    if pause_file.exists():
        try:
            pause = json.loads(pause_file.read_text(encoding="utf-8"))
            reason = pause.get("reason", "")
            if reason in EXTERNAL_GATE_REASONS:
                gate_reason = reason
        except Exception:
            pass

    # Check live owner (factoryd PID).
    has_live_owner = _check_live_owner(factoryd_pid_file or project_dir / "../run/factoryd.pid")

    # Check durable handoff.
    has_durable_handoff = _check_durable_handoff(project_dir)

    # Check open roadmap.
    open_roadmap = _has_open_roadmap(project_dir)

    if gate_reason is not None:
        return StopContext(
            context_type=StopContextType.ONE_TIME_EXTERNAL_MUTATION_GATE,
            reason=f"External gate: {gate_reason}",
            gate_reason=gate_reason,
            has_live_owner=has_live_owner,
            has_durable_handoff=has_durable_handoff,
            open_roadmap=open_roadmap,
            agent_go_requests=0,
        )

    return StopContext(
        context_type=StopContextType.ROUTINE_RUNTIME_CONTINUATION,
        reason="Routine continuation — no verified external gate",
        gate_reason=None,
        has_live_owner=has_live_owner,
        has_durable_handoff=has_durable_handoff,
        open_roadmap=open_roadmap,
        agent_go_requests=0,
    )


def allow_session_exit(ctx: StopContext) -> tuple[bool, str]:
    """Decide whether a session exit is allowed.

    Returns (allowed: bool, reason: str).

    ONE_TIME_EXTERNAL_MUTATION_GATE: always allowed — human approval is legitimate.

    ROUTINE_RUNTIME_CONTINUATION: allowed only when:
        - live factoryd owns the campaign, AND
        - durable handoff is persisted (or no open roadmap)

    If open roadmap exists without a live owner: BLOCK.
    """
    if ctx.context_type == StopContextType.ONE_TIME_EXTERNAL_MUTATION_GATE:
        return True, f"External gate active ({ctx.gate_reason}) — human authorization required"

    # ROUTINE_RUNTIME_CONTINUATION
    if not ctx.open_roadmap:
        return True, "No open roadmap work — portfolio complete or no project selected"

    if ctx.has_live_owner and ctx.has_durable_handoff:
        return True, "Live factoryd owns campaign + durable handoff persisted"

    if ctx.has_live_owner and not ctx.has_durable_handoff:
        return False, (
            "BLOCK: live factoryd running but no durable handoff persisted. "
            "SESSION_EXIT_WITHOUT_DURABLE_HANDOFF invariant violated."
        )

    # No live factoryd — durable handoff IS the ownership transfer mechanism.
    # Handoff present → ownership relayed to next session → allow exit.
    # Handoff absent → gap in ownership → block.
    if not ctx.has_live_owner and ctx.has_durable_handoff:
        return True, (
            "No live factoryd but durable handoff persisted — "
            "ownership transferred to next session via handoff."
        )

    if not ctx.has_live_owner and not ctx.has_durable_handoff and ctx.open_roadmap:
        return False, (
            "BLOCK: open roadmap without live factoryd owner and no durable handoff. "
            "OPEN_ROADMAP_WITHOUT_LIVE_OWNER invariant violated."
        )

    return False, "BLOCK: cannot verify durable handoff — defaulting to safe block"


def record_agent_go_request(project_dir: Path, session_id: str, text_excerpt: str) -> None:
    """Record an AGENT_GO_REQUEST event — informational only, zero scheduling authority."""
    log_file = project_dir / "AGENT_GO_REQUESTS.jsonl"
    import time
    entry = {
        "event": "AGENT_GO_REQUEST",
        "at": _now(),
        "session_id": session_id,
        "authority": "ZERO — agent prose has no scheduling authority",
        "action": "IGNORED — reload canonical authority and continue",
        "excerpt": text_excerpt[:200],
    }
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


# ── internal helpers ──────────────────────────────────────────────────────────

def _check_live_owner(pid_file: Path) -> bool:
    """Return True if factoryd PID file exists and process is alive."""
    try:
        pid_path = Path(pid_file).resolve()
        if not pid_path.exists():
            return False
        pid = int(pid_path.read_text().strip())
        import os
        os.kill(pid, 0)  # signal 0 = existence check, no effect
        return True
    except (ValueError, ProcessLookupError, PermissionError, OSError):
        return False


def _check_durable_handoff(project_dir: Path) -> bool:
    """Return True if a durable handoff file is present and structurally valid."""
    handoff_file = project_dir / "DURABLE_HANDOFF.json"
    if not handoff_file.exists():
        return False
    try:
        h = json.loads(handoff_file.read_text(encoding="utf-8"))
        required = {"schema", "produced_at", "owner_pid", "next_milestone", "current_sha"}
        return required.issubset(h.keys()) and h.get("schema") == "durable-handoff/v1"
    except Exception:
        return False


def _has_open_roadmap(project_dir: Path) -> bool:
    """Return True if PROJECT_STATE.json indicates unfinished roadmap work."""
    state_file = project_dir / "PROJECT_STATE.json"
    if not state_file.exists():
        # No state file = treated as open (safe default — don't allow exit without owner)
        return True
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
        # portfolio_complete only when promotion=DONE or authority says complete
        if state.get("promotion") == "DONE":
            return False
        current = state.get("current_milestone")
        if current:
            return True
        superseded = state.get("superseded_claims", {})
        # If we have superseded claims, roadmap is clearly open
        if superseded:
            return True
        # State can be empty before the first dispatch; the roadmap remains the
        # authority for whether there is work left in that case.
        roadmap_file = project_dir / "ROADMAP.yaml"
        if roadmap_file.is_file():
            import yaml
            roadmap = yaml.safe_load(roadmap_file.read_text(encoding="utf-8")) or {}
            milestones = roadmap.get("milestones", []) if isinstance(roadmap, dict) else []
            complete = state.get("completed_milestones") or {}
            for milestone in milestones:
                if not isinstance(milestone, dict) or milestone.get("retired"):
                    continue
                mid = str(milestone.get("id") or "")
                if mid and (complete.get(mid) or {}).get("state") != "COMPLETE":
                    return True
        return False
    except Exception:
        return True  # fail open (safe default)
