"""ContinuationController — Python owns the loop. LLM text has zero authority to stop work.

P0-E invariants:
- RunDisposition is the ONLY state that controls workflow continuation.
- ASK_USER, WAIT_FOR_GO, REPORT_AND_STOP do NOT exist in this type system.
- Agent/session completion is NOT workflow completion.
- After any agent exit: reload canonical authority → inspect state → if work remains
  and no verified external block → START NEXT CANONICAL ACTION AUTOMATICALLY.
- Human gate = whitelist + mechanical attestation receipt (not just "evidence present").
- PAUSED_BY_USER: only set by human-written receipt, never by machine.
- SAFETY_STOP: machine-set with cause + evidence + invariant violated (separate from user pause).

The literal agent text "Tell me when you want me to continue." has ZERO authority.
Only verified external dependencies may create a human block.
"""
from __future__ import annotations

import json
import time
from enum import Enum
from pathlib import Path
from typing import NamedTuple
from factory_v2._io import _now


class RunDisposition(Enum):
    """The only valid workflow continuation states.

    States that MUST NOT exist in this type system:
    - ASK_USER
    - WAIT_FOR_GO
    - REPORT_AND_STOP
    - DO_YOU_WANT_ME_TO_CONTINUE
    - NEED_CONFIRMATION
    """
    CONTINUE = "CONTINUE"
    PORTFOLIO_COMPLETE = "PORTFOLIO_COMPLETE"
    BLOCKED_EXTERNAL_VERIFIED = "BLOCKED_EXTERNAL_VERIFIED"
    PAUSED_BY_USER = "PAUSED_BY_USER"
    SAFETY_STOP = "SAFETY_STOP"
    # F5R4: control-plane error — malformed ROADMAP/STATE/authority.
    # MUST NOT be confused with PORTFOLIO_COMPLETE. Daemon logs, backs off,
    # and continues polling (does not exit).
    CONTROL_PLANE_ERROR = "CONTROL_PLANE_ERROR"


# Human-gate whitelist: ONLY these reasons may produce BLOCKED_EXTERNAL_VERIFIED.
# Anything not in this list is an internal failure requiring recovery, not a human gate.
HUMAN_GATE_WHITELIST = frozenset({
    "MISSING_CREDENTIAL",
    "EXTERNAL_PERMISSION_REQUIRED",
    "IRREVERSIBLE_ACTION_AUTHORIZATION",
    "PAYMENT_OR_FINANCIAL_AUTHORIZATION",
    "PRODUCTION_DEPLOYMENT_APPROVAL",
    "GENUINELY_SUBJECTIVE_PRODUCT_DECISION",
})

# These are NOT human gates — they require internal recovery strategies, not human approval.
NOT_HUMAN_GATES = frozenset({
    "test_failure",
    "validator_failure",
    "worker_crash",
    "timeout",
    "three_failed_attempts",
    "architecture_problem",
    "debugging_problem",
    "roadmap_compilation_bug",
    "model_failure",
    "engine_quota",
    "unknown_internal_exception",
})



class ContinuationDecision(NamedTuple):
    disposition: RunDisposition
    reason: str
    human_go_count: int


def _is_agent_asking_for_go(agent_output: str) -> bool:
    """Detect if agent output contains common human-go requests.

    These phrases have ZERO authority in the controller. The presence of such text
    in agent output must NOT change the workflow disposition.
    """
    if not agent_output:
        return False
    phrases = [
        "tell me when", "shall i continue", "want me to launch", "do you want me to proceed",
        "when you want me to", "let me know if", "should i proceed", "go?",
        "waiting for your", "waiting for go", "need your approval", "need your confirmation",
    ]
    lower = agent_output.lower()
    return any(p in lower for p in phrases)


def _load_pause_file(project_dir: Path) -> dict | None:
    """Load PAUSE.json if it exists (human-written pause).

    FAIL-CLOSED: a file that exists but cannot be parsed was truncated by a
    crash mid-write. Treating it as absent would resume a paused project;
    treat it as an active pause with no metadata instead.
    L5_rest_1-013: that fail-closed contract must cover a VALID but EMPTY
    object too — `{}` parses, but an empty pause file is an incomplete stop
    order, never an absence of one.
    """
    pause_file = project_dir / "PAUSE.json"
    if pause_file.exists():
        try:
            data = json.loads(pause_file.read_text(encoding="utf-8"))
        except Exception:
            return {"reason": "PAUSE_FILE_UNREADABLE", "fail_closed": True}
        if isinstance(data, dict) and data:
            return data
        return {"reason": "PAUSE_FILE_INCOMPLETE", "fail_closed": True}
    return None


def _load_safety_stop_file(project_dir: Path) -> dict | None:
    """Load SAFETY_STOP.json if it exists (machine-written safety stop).

    FAIL-CLOSED: an unreadable SAFETY_STOP.json (crash mid-write) must STOP,
    not be ignored. Returning None here would let the controller continue
    exactly where a stop was ordered.
    L5_rest_1-013: a valid-but-empty `{}` is the same incomplete order — the
    stop signal is the file's PRESENCE, not the truthiness of its content.
    """
    stop_file = project_dir / "SAFETY_STOP.json"
    if stop_file.exists():
        try:
            data = json.loads(stop_file.read_text(encoding="utf-8"))
        except Exception:
            return {"reason": "SAFETY_STOP_FILE_UNREADABLE", "fail_closed": True}
        if isinstance(data, dict) and data:
            return data
        return {"reason": "SAFETY_STOP_FILE_INCOMPLETE", "fail_closed": True}
    return None



def decide_continuation(
    portfolio_dir: Path,
    project_dir: Path | None,
    last_advance_action: str,
    agent_output: str = "",
    human_go_count: int = 0,
    portfolio_file: "Path | None" = None,
    single_project: bool = False,
    cursor_path: "Path | None" = None,
    selection=None,
) -> ContinuationDecision:
    """Determine the next workflow disposition.

    P0-E: this is the ONLY place that decides whether to continue.
    LLM text output has ZERO authority here.

    Algorithm:
    1. Check for human-written PAUSE.json (only humans can set this).
    2. Check for machine-written SAFETY_STOP.json.
    3. Agent asking for GO? Log it, ignore it, CONTINUE.
    4. Check if portfolio has remaining work.
    5. Check for verified external blocks with mechanical receipts.
    6. Default: CONTINUE.
    """
    was_asking_for_go = _is_agent_asking_for_go(agent_output)
    new_human_go_count = human_go_count + (1 if was_asking_for_go else 0)

    # Step 1: human pause (PAUSE.json must be written by human, not by machine)
    if project_dir:
        pause = _load_pause_file(project_dir)
        if pause:
            return ContinuationDecision(
                disposition=RunDisposition.PAUSED_BY_USER,
                reason=f"Human pause active: {pause.get('reason', 'no reason given')}",
                human_go_count=new_human_go_count,
            )

    # Step 2: machine safety stop (separate from user pause — different file, different state)
    if project_dir:
        stop = _load_safety_stop_file(project_dir)
        if stop:
            return ContinuationDecision(
                disposition=RunDisposition.SAFETY_STOP,
                reason=f"Safety stop: {stop.get('cause', 'unknown')} — {stop.get('invariant', '')}",
                human_go_count=new_human_go_count,
            )

    # Step 3: agent text asking for GO — log and IGNORE
    # The agent's output is informational. The controller owns the loop.

    # Step 4: check the active scope. A --v2-project-dir run deliberately has no
    # portfolio and must never turn that absence into a control-plane error.
    if single_project:
        if last_advance_action == "PROJECT_COMPLETE":
            return ContinuationDecision(
                disposition=RunDisposition.PORTFOLIO_COMPLETE,
                reason="The selected project is complete",
                human_go_count=new_human_go_count,
            )
        sel = None
    else:
        from .runtime import select_project
        if selection is not None:
            # CTL-04 (suite): the daemon has ALREADY selected this poll's project and
            # advanced the rotation cursor. Re-selecting here would advance it a second
            # time per poll and skip a project each cycle. Reuse the daemon's SelectResult.
            sel = selection
            cursor_path = "__reused_selection__"  # sentinel: skip the fallback below
        if cursor_path is None:
            import logging
            logging.getLogger(__name__).warning(
                "decide_continuation() called without cursor_path — falling back to "
                "select_project()'s default cursor location (historical behaviour). "
                "Callers should pass an explicit cursor_path (e.g. the daemon's "
                "factory/run/portfolio_cursor.json) to avoid a second, divergent cursor file."
            )
        if selection is None:
            sel = select_project(portfolio_dir, portfolio_file=portfolio_file, cursor_path=cursor_path)
    if sel is not None and sel.kind == "PORTFOLIO_UNAVAILABLE":
        # PORTFOLIO_UNAVAILABLE must NOT become PORTFOLIO_COMPLETE — the portfolio
        # file is missing or misconfigured, not legitimately empty. Treat as an
        # idle control-plane state: daemon stays alive and retries next poll.
        return ContinuationDecision(
            disposition=RunDisposition.CONTROL_PLANE_ERROR,
            reason="PORTFOLIO_UNAVAILABLE: portfolio.json not found — control plane misconfiguration",
            human_go_count=new_human_go_count,
        )
    if sel is not None and sel.kind == "CONTROL_PLANE_ERROR":
        # MUST NOT become PORTFOLIO_COMPLETE. Daemon must backoff and retry.
        return ContinuationDecision(
            disposition=RunDisposition.CONTROL_PLANE_ERROR,
            reason=f"CONTROL_PLANE_ERROR: {sel.error}",
            human_go_count=new_human_go_count,
        )
    if sel is not None and sel.kind == "PORTFOLIO_COMPLETE":
        return ContinuationDecision(
            disposition=RunDisposition.PORTFOLIO_COMPLETE,
            reason="All projects in portfolio are complete",
            human_go_count=new_human_go_count,
        )

    # Step 5: check for verified external blocks
    # Only BLOCKED_EXTERNAL_VERIFIED if there's a mechanical receipt from the whitelist.
    if last_advance_action in ("BLOCKED", "MISSION_FAILED", "MILESTONE_GATE_BLOCKED"):
        # Internal failures → recover, not block
        # These are NOT human gates per the whitelist
        pass

    # Step 6: default → CONTINUE (work remains, no verified block)
    # sel.project_dir is set when kind == PROJECT_SELECTED
    next_proj_desc = (str(project_dir) if single_project else
                      (str(sel.project_dir)
                       if sel.kind == "PROJECT_SELECTED" else sel.kind))
    return ContinuationDecision(
        disposition=RunDisposition.CONTINUE,
        reason=f"Work remains in portfolio (next: {next_proj_desc}). "
               + (f"[IGNORED: agent asked for GO ({new_human_go_count} total times)]"
                  if was_asking_for_go else ""),
        human_go_count=new_human_go_count,
    )


def write_safety_stop(project_dir: Path, cause: str, invariant: str,
                      evidence: dict | None = None) -> None:
    """Write a machine-generated safety stop. Separate from PAUSE.json (human-written)."""
    stop_file = project_dir / "SAFETY_STOP.json"
    stop_file.write_text(json.dumps({
        "schema": "safety-stop/v1",
        "written_at": _now(),
        "written_by": "machine",
        "cause": cause,
        "invariant": invariant,
        "evidence": evidence or {},
    }, indent=2), encoding="utf-8")
