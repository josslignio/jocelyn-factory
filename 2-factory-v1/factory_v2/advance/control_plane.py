"""Étape extraite de `_advance_locked` (factory_v2/runtime.py) : le plan de contrôle qui tourne à CHAQUE…"""
from __future__ import annotations

from .. import runtime as rt


def run(ctx):
    """Bloc « CONTROL PLANE » de `_advance_locked`."""
    rt._apply_pending_audit_cell_result(ctx.project_dir)

    # ── CONTROL PLANE (F1) ────────────────────────────────────────────────────
    # Authority load, settlement reconciliation and allowed recovery work run on
    # EVERY cycle — including while quarantined.  Only product dispatch is gated.
    # Without this split, quarantine is a dead end: nothing repairs it and
    # AUTONOMOUS_SAFE_REPAIR_DISPATCHED stays 0 forever.
    from ..safety_quarantine import (assert_action_allowed, QuarantineActionForbidden,
                                    get_quarantine_record, is_in_quarantine)
    ctx.authority: rt.ProjectAuthority = rt.load_authority(ctx.project_dir)
    ctx.state = rt.load_project_state(ctx.project_dir)

    # D1-R1: Reconcile any prepared-but-not-projected settlements before pending-work selection.
    ctx.state, ctx._migrated = rt.migrate_settlement_ledger(ctx.project_dir, ctx.state)
    if ctx._migrated:
        rt._event(ctx.state, "SETTLEMENT_LEDGER_MIGRATED", entries=ctx._migrated,
               schema=rt.SETTLEMENT_LEDGER_SCHEMA)
        rt.save_project_state(ctx.project_dir, ctx.state)

    # ── R1.2 supersede ladder records produced by other control-plane code ───
    # Failures produced by the OLD executor must not consume the retries of the
    # FIXED one, and must never push it to REPLAN. They are typed, not deleted:
    # erasing them would erase the history of what was actually attempted.
    try:
        from ..escalation_ladder import (mark_superseded_by_code_change,
                                        supersede_pending_recovery_work)
        ctx._sup = mark_superseded_by_code_change(ctx.state)
        if ctx._sup:
            rt._event(ctx.state, "LADDER_RECORDS_SUPERSEDED_BY_CODE_CHANGE", count=ctx._sup)
            rt.save_project_state(ctx.project_dir, ctx.state)
        ctx._sup_work = supersede_pending_recovery_work(ctx.project_dir)
        if ctx._sup_work:
            rt._event(ctx.state, "RECOVERY_WORK_SUPERSEDED_BY_CODE_CHANGE",
                   count=len(ctx._sup_work), recovery_ids=ctx._sup_work[:20])
            rt.save_project_state(ctx.project_dir, ctx.state)
    except Exception as _sexc:
        rt._event(ctx.state, "LADDER_SUPERSEDE_FAILED", error=str(_sexc)[:200])

    # ── §F2 lost-attempt reconciliation ──────────────────────────────────────
    # An OPEN row with no terminal row means the process died mid-flight. Close it
    # exactly once, as KILL, claiming nothing about what it produced. Idempotent:
    # a second restart finds it already TERMINAL and adds nothing.
    try:
        from ..launch_gate import reconcile_on_boot, resume_pending_projection
        ctx._resumed = resume_pending_projection(ctx.project_dir)
        if ctx._resumed:
            rt._event(ctx.state, "ACCOUNTING_PROJECTION_RESUMED", count=len(ctx._resumed))
        ctx._lost = reconcile_on_boot(ctx.project_dir)
        if ctx._lost:
            rt._event(ctx.state, "LOST_ATTEMPTS_RECONCILED", count=len(ctx._lost),
                   attempts=[str(k)[:80] for k in ctx._lost[:5]])
            rt.save_project_state(ctx.project_dir, ctx.state)
    except Exception as _lexc:
        rt._event(ctx.state, "LOST_ATTEMPT_RECONCILE_FAILED", error=str(_lexc)[:200])
        rt.save_project_state(ctx.project_dir, ctx.state)
        return rt.CycleResult(
            "LOST_ATTEMPT_RECONCILE_UNKNOWN", None,
            f"lost-attempt ledger/liveness observation unavailable: {_lexc}")

    # ── §F3 hash-bound verdict reconciliation ────────────────────────────────
    # NOT_COMPILABLE / GATE_BLOCKED are verdicts about a SPECIFIC authority. When
    # the roadmap moves, a verdict recorded against the old hash is stale, and a
    # milestone frozen under it would never be reconsidered. authority_reconcile
    # existed for this and nothing called it. Voiding a stale verdict asserts only
    # that the old verdict no longer applies — never that the milestone compiles.
    #
    # C36: recover a hash-bound record's real recorded authority from its own
    # frozen contract BEFORE reconcile_stale_gates runs — otherwise a record with
    # no `roadmap_sha_at_record` (the IN_PROGRESS branch below never stamped it)
    # is UNVERIFIABLE_NO_RECORDED_AUTHORITY on every single cycle, voided whether
    # or not the roadmap actually moved. See _backfill_roadmap_sha_at_record.
    try:
        if rt._backfill_roadmap_sha_at_record(ctx.project_dir, ctx.state):
            rt.save_project_state(ctx.project_dir, ctx.state)
    except Exception as _bexc:
        rt._event(ctx.state, "ROADMAP_SHA_BACKFILL_FAILED", error=str(_bexc)[:200])

    try:
        from ..authority_reconcile import reconcile_stale_gates
        ctx.state, ctx._stale = reconcile_stale_gates(ctx.state, ctx.authority.identity.roadmap_sha)
        if ctx._stale:
            for ctx._sm in ctx._stale:
                rt._event(ctx.state, "STALE_VERDICT_VOIDED", milestone=ctx._sm.get("milestone", ""),
                       recorded_under=str(ctx._sm.get("roadmap_sha_at_record", ""))[:16],
                       current=str(ctx.authority.identity.roadmap_sha)[:16],
                       note="verdict voided; no claim that the milestone now compiles")
            rt.save_project_state(ctx.project_dir, ctx.state)
    except Exception as _rexc:
        rt._event(ctx.state, "STALE_VERDICT_RECONCILE_FAILED", error=str(_rexc)[:200])

    ctx.state, ctx._reconciled = rt.reconcile_prepared_settlements(ctx.project_dir, ctx.state)
    if ctx._reconciled:
        rt.save_project_state(ctx.project_dir, ctx.state)

    # G5 two-phase RunRecords (Order 024 G5 / RUN_RECORDS_MINIMUM_WIRED): close any PREDICTION left orphaned…
    if rt._RUNRECORDS_AVAILABLE and rt._reconcile_orphan_predictions is not None:
        try:
            rt._reconcile_orphan_predictions()
        except Exception as _rr_exc:
            # A ledger failure is UNKNOWN, never equivalent to "no orphan".
            rt._event(ctx.state, "RUNRECORD_RECONCILE_UNKNOWN",
                      error=str(_rr_exc)[:200])
            rt.save_project_state(ctx.project_dir, ctx.state)
            return rt.CycleResult(
                "RUNRECORD_RECONCILE_UNKNOWN", None,
                f"RunRecord reconciliation unavailable: {_rr_exc}")

    # S017-04: a superseded product intent is consumed BEFORE any old recovery work.
    ctx._superseded = rt._consume_product_intent_handoff(ctx.project_dir, ctx.state, ctx.authority)
    if ctx._superseded is not None:
        return ctx._superseded

    return None
