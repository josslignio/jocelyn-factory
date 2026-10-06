"""Supervisor-side progress truth. `PID alive` is not health.

CTO ORDER 2026-09-10 §A. factoryd ran 6678 polls with stall_count 1407, zero workers
and zero product movement, while every liveness signal said healthy: the process was
up, the heartbeat was fresh, polls incremented and stop_event commits kept landing.
None of those is progress.

    A poll, a heartbeat, a stop_event, a log line or an evidence commit is ACTIVITY.
    Progress is a durable change the supervisor can name.

The distinction is not pedantic. Activity metrics are exactly what the METR RCT found
inverted from outcome — developers wrote 47% more code per hour while finishing 19%
slower — and it is what let a factory burn a night of quota looking busy.

Six conditions must be detectable inside 300 seconds. Each returns a TYPED incident,
never a bare boolean, because the remediation differs per class and "restart it" is
not a remediation for any of them.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from factory_v2._io import _now, _utc_epoch

DETECTION_BUDGET_S = 300.0

#: Events that constitute PRODUCT progress. Everything else is activity.
PROGRESS_EVENTS = frozenset({
    "MILESTONE_SETTLED", "MISSION_DISPATCHED", "MISSION_PASS", "MISSION_FAILED",
    "SETTLEMENT_RECORDED", "GOLDEN_PROMOTED", "AUDIT_CELL_SYNTHESIS_APPLIED",
    # CTL-03: QUARANTINE_CLEARED, REPLAN_APPLIED, RECEIPT_VERIFIED removed — never
    # emitted by runtime.py under those literal names (see tests/test_progress_truth_events.py).
    "SAFETY_QUARANTINE_CLEARED", "DERIVED_PLAN_APPLIED",
    "RECOVERY_WORK_COMPLETE", "SETTLEMENT_LEDGER_ENTRY_WRITTEN",
})

#: Signals that look like life and are not progress. Named so nobody re-adds them.
ACTIVITY_ONLY = frozenset({
    "POLL", "HEARTBEAT", "STOP_EVENT", "LOG", "DAEMON_BACKOFF", "IDLE",
    "V2_ADVANCE", "CONTINUATION", "EVIDENCE_COMMIT",
})


#: Evidence keys that measure elapsed time. They change every poll by construction
#: (the whole point of a still-ongoing incident) and must never enter a dedup key,
#: or an identical, unresolved cause mints a "new" incident on every tick.
_ELAPSED_EVIDENCE_KEYS = frozenset({"seconds_since_progress"})


def _dedup_key(kind: str, evidence: dict) -> str:
    """Stable identity for an incident: the CAUSE, never the elapsed-time text.

    `f"{kind}:{detail[:80]}"` used to double as the dedup key, but `detail` embeds
    "...for {since:.0f}s..." inside those first 80 characters — a different value
    every poll — so a genuinely unchanged incident never matched its predecessor
    and was persisted in full every cycle instead of aggregated.
    """
    stable = {k: v for k, v in (evidence or {}).items() if k not in _ELAPSED_EVIDENCE_KEYS}
    return f"{kind}:{json.dumps(stable, sort_keys=True, default=str)}"


@dataclass
class Incident:
    """A typed non-progress condition plus the safe action it implies."""
    kind: str
    detail: str
    detected_at: str
    next_safe_action: str
    cause_must_be_fixed: bool = True
    evidence: dict = field(default_factory=dict)
    #: Stable identity for dedup, independent of any elapsed-time text in `detail`.
    #: Auto-derived from (kind, evidence) when not passed explicitly.
    dedup_key: str = ""

    def __post_init__(self) -> None:
        if not self.dedup_key:
            self.dedup_key = _dedup_key(self.kind, self.evidence)

    def as_dict(self) -> dict:
        return {"schema": "progress-incident/v1", **self.__dict__}




def _age_s(ts: str) -> float:
    """Seconds since an ISO-8601 Z timestamp. inf when unparseable — never 0."""
    t = _utc_epoch(ts)
    return float("inf") if t is None else time.time() - t


def last_progress(events: list) -> tuple[str, str]:
    """(timestamp, event) of the most recent PRODUCT progress. ('','') when none."""
    for e in reversed(events):
        if str(e.get("event", "")) in PROGRESS_EVENTS:
            return str(e.get("ts") or e.get("at") or ""), str(e.get("event"))
    return "", ""


def _pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def classify(*, events: list, writer_pid=None, daemon_pid=None,
             active_workers: list | None = None,
             pending_reviewer: dict | None = None,
             stale_gates: list | None = None,
             budget_s: float = DETECTION_BUDGET_S) -> list:
    """Return every typed incident visible right now. Empty list == genuinely healthy.

    Deliberately returns ALL matching incidents rather than the first: a daemon can be
    simultaneously idle and sitting on a stale gate, and reporting only one hides the
    cause that actually needs fixing.
    """
    out: list = []
    ts, ev = last_progress(events)
    since = _age_s(ts)
    stalled = since > budget_s

    writer_alive = writer_pid is not None and _pid_alive(writer_pid)
    if writer_pid is not None and not writer_alive:
        out.append(Incident(
            kind="WRITER_DEAD", detected_at=_now(),
            detail=f"writer pid {writer_pid} is gone; last product progress {ev or 'NONE'} "
                   f"{since:.0f}s ago",
            next_safe_action="reconcile the durable cursor, then relaunch ONE writer "
                             "from canonical authority",
            evidence={"writer_pid": writer_pid, "seconds_since_progress": since}))

    if writer_alive and stalled:
        out.append(Incident(
            kind="WRITER_ALIVE_NO_PROGRESS", detected_at=_now(),
            detail=f"writer pid {writer_pid} is alive but no product progress for "
                   f"{since:.0f}s (budget {budget_s:.0f}s). A fresh heartbeat is a claim; "
                   f"progress is evidence",
            next_safe_action="capture the writer's current position, diagnose the block, "
                             "fix the cause; a bare restart resolves nothing",
            evidence={"writer_pid": writer_pid, "seconds_since_progress": since,
                      "last_progress_event": ev}))

    if pending_reviewer:
        rpid = pending_reviewer.get("pid")
        if rpid is not None and not _pid_alive(rpid) and not pending_reviewer.get("verdict"):
            out.append(Incident(
                kind="REVIEWER_DEAD_NO_VERDICT", detected_at=_now(),
                detail=f"reviewer pid {rpid} exited without publishing a verdict",
                next_safe_action="record a typed terminal state for the review job; never "
                                 "infer ACCEPT or REJECT from absence",
                evidence=dict(pending_reviewer)))

    fps = [str(e.get("failure_fingerprint") or "") for e in events[-12:]
           if e.get("failure_fingerprint")]
    if len(fps) >= 3 and len(set(fps[-3:])) == 1 and fps[-1]:
        out.append(Incident(
            kind="EQUIVALENT_RETRY_LOOP", detected_at=_now(),
            detail=f"the last 3 attempts share failure fingerprint {fps[-1][:60]!r} — "
                   f"an unbounded equivalent retry converts a transient fault into a "
                   f"permanent one",
            next_safe_action="escalate to a materially different strategy or the audit "
                             "cell; do not retry the same shape again",
            evidence={"fingerprint": fps[-1][:200], "repeats": 3}))

    if daemon_pid is not None and _pid_alive(daemon_pid) and not (active_workers or []) \
            and stalled:
        out.append(Incident(
            kind="DAEMON_IDLE_NO_USEFUL_WORK", detected_at=_now(),
            detail=f"daemon pid {daemon_pid} is polling with zero workers and no product "
                   f"progress for {since:.0f}s — a live runtime without material progress "
                   f"is an incident, not a healthy state",
            next_safe_action="resolve why the next action is not compilable or not "
                             "dispatchable, fix that, then resume",
            evidence={"daemon_pid": daemon_pid, "seconds_since_progress": since}))

    for gate in (stale_gates or []):
        out.append(Incident(
            kind="STALE_GATE_BLOCKED", detected_at=_now(),
            detail=f"{gate.get('milestone')} is {gate.get('state')} against "
                   f"{gate.get('recorded_hash','?')[:12]} but current authority is "
                   f"{gate.get('current_hash','?')[:12]} — the block is no longer true",
            next_safe_action="reconcile the runtime state with current authority before "
                             "any dispatch decision",
            evidence=dict(gate)))
    return out


def persist(incidents: list, ledger_path: Path) -> int:
    """Append typed incidents. Returns how many were written."""
    if not incidents:
        return 0
    ledger_path = Path(ledger_path)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_path.open("a", encoding="utf-8") as fh:
        for inc in incidents:
            fh.write(json.dumps(inc.as_dict(), sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return len(incidents)
