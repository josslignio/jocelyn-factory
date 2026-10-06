"""Idempotent LAUNCH_BLOCKED event/manifest writes across repeated polls.

Before this module, `runtime.py`'s dispatch loop appended a LAUNCH_BLOCKED
event and called `save_project_state()` on EVERY poll of an unchanged blocked
condition — e.g. COST_NO_PROGRESS tripped with no recovery allocation
available. A daemon polling every 30s therefore wrote a durable event and
rewrote the whole project-state manifest forever, with nothing new to
report, long after the condition had become terminal.

`should_emit` makes that write idempotent per (mission, reason, detail)
fingerprint: the first observation of a given blocked condition is reported
and persisted exactly once; every identical repeat is a no-op. The moment
anything about the block changes (a new allocation appears, the reason
changes, the code SHA moves), the fingerprint changes and the next poll
reports it again — this is deduplication of IDENTICAL noise, not suppression
of new information.
"""
from __future__ import annotations

import hashlib
import json

#: Detail keys that vary between otherwise-identical polls without carrying
#: new information (e.g. wall-clock timestamps embedded in a recovery-
#: allocation peek). Excluded from the fingerprint so cosmetic churn does not
#: defeat deduplication.
_VOLATILE_DETAIL_KEYS = ("checked_at", "now", "observed_at", "detail")


def _stable(detail: dict) -> dict:
    return {k: v for k, v in sorted((detail or {}).items())
            if k not in _VOLATILE_DETAIL_KEYS}


def blocked_fingerprint(mission_id: str, reason: str, detail: dict) -> str:
    payload = json.dumps({"mission_id": mission_id, "reason": reason,
                          "detail": _stable(detail)},
                         sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def should_emit(state: dict, mission_id: str, reason: str, detail: dict) -> bool:
    """True the first time this exact blocked condition is observed for this
    mission; False on every identical repeat. Mutates `state` in place when
    True, recording the new fingerprint so the caller's own durable save
    persists it atomically with the event/manifest write it is guarding —
    a caller that returns without saving after a True result reports the
    same condition again next time, which is the fail-safe direction.
    """
    fp = blocked_fingerprint(mission_id, reason, detail)
    missions = state.setdefault("missions", {})
    rec = missions.setdefault(mission_id, {})
    if rec.get("last_blocked_fingerprint") == fp:
        return False
    rec["last_blocked_fingerprint"] = fp
    return True
