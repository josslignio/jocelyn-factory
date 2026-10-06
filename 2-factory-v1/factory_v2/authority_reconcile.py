"""Boot-time reconciliation of runtime state against current authority.

CTO ORDER 2026-09-10 §B. factoryd looped for hours on:

    V2_BLOCKED: MILESTONE_NOT_COMPILABLE — TR_TICKER_INTELLIGENCE_V1 was
    NOT_COMPILABLE with roadmap@948b9bda2814 — no new roadmap content

The roadmap had in fact changed hours earlier (6f4f436e wired the missing execution
steps) and the milestone compiled fine. The daemon was refusing work it could do,
because a runtime verdict recorded against a dead authority hash was still believed.

Two rules follow, and they are the section in full:

  * A runtime verdict is only valid against the authority hash it was recorded under.
    When the hash moves, the verdict is STALE — not true, not false, simply void.
  * Resolve every path from the canonical root, never from the accidental cwd. The
    same daemon reported PORTFOLIO_UNAVAILABLE for a portfolio.json that existed,
    because it had been started from somewhere else.

Reconciliation never invents a positive: it removes verdicts that can no longer be
trusted and lets the resolver decide again from current authority.
"""
from __future__ import annotations

import time
from pathlib import Path
from factory_v2._io import _now

#: Runtime verdicts that are only meaningful relative to an authority hash.
HASH_BOUND_STATES = frozenset({
    "NOT_COMPILABLE", "GATE_BLOCKED", "MILESTONE_NOT_COMPILABLE",
    "MILESTONE_GATE_BLOCKED", "BLOCKED_DEPENDENCY",
})


def canonical_root() -> Path:
    """The control repo root, derived from THIS file.

    F-6 doctrine: the code that is executing declares where it lives, so a daemon
    started from an unrelated cwd still resolves the same authority. Never fall back
    to Path.cwd() — that is precisely the PORTFOLIO_UNAVAILABLE bug.
    """
    return Path(__file__).resolve().parent.parent


def find_stale_gates(state: dict, current_roadmap_sha: str) -> list:
    """Runtime verdicts recorded against an authority that has since moved."""
    stale = []
    for mid, rec in (state.get("completed_milestones") or {}).items():
        if not isinstance(rec, dict):
            continue
        st = str(rec.get("state") or "")
        if st not in HASH_BOUND_STATES:
            continue
        recorded = str(rec.get("roadmap_sha_at_record")
                       or rec.get("authority_hash") or "")
        if not recorded:
            # A hash-bound verdict with NO recorded hash is UNFALSIFIABLE: there is
            # nothing to compare it against, so it can never be shown stale and the
            # milestone stays refused for life. `if recorded and ...` silently
            # skipped these — a falsy guard turning a check into a no-op, the same
            # shape as the quarantine copy that let real CLI models launch.
            #
            # Observed: twitter-radar carried TWO such verdicts, including its next
            # milestone, so its whole pipeline was frozen with no route out.
            #
            # Treated as stale, which claims NOTHING about the milestone compiling —
            # reconcile only moves it to PENDING_RERESOLUTION so the resolver can
            # decide again against the authority that actually exists now.
            stale.append({"milestone": mid, "state": st,
                          "recorded_hash": "",
                          "current_hash": current_roadmap_sha,
                          "why": "UNVERIFIABLE_NO_RECORDED_AUTHORITY"})
            continue
        if current_roadmap_sha and recorded != current_roadmap_sha:
            stale.append({"milestone": mid, "state": st,
                          "recorded_hash": recorded,
                          "current_hash": current_roadmap_sha,
                          "why": "AUTHORITY_MOVED"})
    return stale


def reconcile_stale_gates(state: dict, current_roadmap_sha: str) -> tuple[dict, list]:
    """Void hash-bound verdicts whose authority moved. Returns (state, reconciled).

    The verdict is REMOVED, not flipped: we do not claim the milestone now compiles,
    only that the recorded refusal no longer has standing. The resolver re-derives the
    answer from current authority on the next cycle — which is the only thing entitled
    to decide it.
    """
    stale = find_stale_gates(state, current_roadmap_sha)
    if not stale:
        return state, []
    now = _now()
    cm = state.setdefault("completed_milestones", {})
    for s in stale:
        rec = cm.get(s["milestone"])
        if not isinstance(rec, dict):
            continue
        rec["state"] = "PENDING_RERESOLUTION"
        rec["stale_verdict_voided"] = {
            "previous_state": s["state"],
            "recorded_against": s["recorded_hash"],
            "current_authority": s["current_hash"],
            "voided_at": now,
            "note": ("the recorded refusal was bound to an authority hash that has "
                     "moved; no claim is made that this milestone now compiles"),
        }
        rec.pop("not_compilable_reason", None)
        cm[s["milestone"]] = rec
    return state, stale


def authority_snapshot(project_dir: Path) -> dict:
    """Recompute the six canonical hashes plus the resolved next action.

    Never reads a cached identity: the point of a boot check is to compare what the
    disk says NOW against what the runtime believed.
    """
    from .project_authority import load_authority, load_project_state, resolve
    project_dir = Path(project_dir)
    auth = load_authority(project_dir)
    state = load_project_state(project_dir)
    ident = auth.identity.as_dict()
    try:
        nxt = resolve(auth, state).next_milestone
        resolve_error = ""
    except Exception as exc:                       # noqa: BLE001
        nxt, resolve_error = None, f"{type(exc).__name__}: {exc}"
    return {
        "project_dir": str(project_dir),
        "canonical_root": str(canonical_root()),
        "strategy_sha": ident.get("strategy_sha", ""),
        "spec_sha": ident.get("spec_sha", ""),
        "roadmap_sha": ident.get("roadmap_sha", ""),
        "next_milestone": nxt,
        "resolve_error": resolve_error,
        "stale_gates": find_stale_gates(state, ident.get("roadmap_sha", "")),
        "captured_at": _now(),
    }
