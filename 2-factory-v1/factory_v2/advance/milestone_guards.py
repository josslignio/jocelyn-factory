"""Étape extraite de `_advance_locked` (factory_v2/runtime.py) : les gardes qui suivent la sélection du…"""
from __future__ import annotations

from .. import runtime as rt
from ..escalation_ladder import RecoveryLedgerUnknown


def recovery_first(ctx):
    """Read this milestone's pending recovery into ctx._scoped."""
    try:
        ctx._scoped = rt.read_pending_recovery_work(ctx.project_dir, milestone_id=ctx.mid)
    except RecoveryLedgerUnknown as exc:
        rt._event(ctx.state, "RECOVERY_LEDGER_UNKNOWN", milestone=ctx.mid, error=str(exc)[:200])
        rt.save_project_state(ctx.project_dir, ctx.state)
        return rt.CycleResult("RECOVERY_LEDGER_UNKNOWN", ctx.mid,
                              f"{ctx.mid}: recovery ledger unreadable — refusing to proceed "
                              f"as if nothing were pending: {exc}")
    return None


def run(ctx):
    """Bloc « gardes post-sélection + recovery scopé au jalon » de `_advance_locked`."""
    # C35: defense in depth, directly in the settlement loop. resolve() (and, under quarantine…
    if ctx.completed_rec and ctx.completed_rec.get("state") != "COMPLETE":
        ctx._held_missing = rt._missing_dependencies_for_entry(ctx.entry, ctx.state, ctx.authority)
        if ctx._held_missing:
            from ..blocked_event_dedup import should_emit
            ctx._held_key = f"milestone_held_by_dependency:{ctx.mid}:{','.join(ctx._held_missing)}"
            if should_emit(ctx.state, ctx._held_key, "MILESTONE_HELD_BY_DEPENDENCY",
                          {"missing_dependencies": ctx._held_missing}):
                rt._event(ctx.state, "MILESTONE_HELD_BY_DEPENDENCY", milestone=ctx.mid,
                       missing_dependencies=ctx._held_missing)
                rt.save_project_state(ctx.project_dir, ctx.state)
            return rt.CycleResult(
                "MILESTONE_HELD_BY_DEPENDENCY", ctx.mid,
                f"{ctx.mid}: held — depends_on not yet satisfied: {ctx._held_missing}")

    # Calendar evidence is neither a code defect nor a recovery candidate.
    if ctx.completed_rec and ctx.completed_rec.get("state") == "WAITING_FOR_EVIDENCE":
        ctx.until = rt._waiting_until(ctx.completed_rec.get("next_check_at"))
        if ctx.until is not None and rt.datetime.now(rt.timezone.utc) < ctx.until:
            return rt.CycleResult("WAITING_FOR_EVIDENCE", ctx.mid,
                               f"{ctx.mid}: independent verifier requests a calendar retry at "
                               f"{ctx.completed_rec['next_check_at']}")

    # R3/X2: milestone-scoped recovery work (from a blocked gate, a NOT_COMPILABLE milestone, or a…
    ctx._blocked_recovery = recovery_first(ctx)
    if ctx._blocked_recovery is not None:
        return ctx._blocked_recovery
    if ctx._scoped is not None and ctx._scoped.milestone_id == ctx.mid and any(
            ctx._scoped.parent_mission_id.endswith(sfx)
            for sfx in ("#gate", "#compile", "#quarantine")):
        # The product can advance after a mission exits.
        ctx._fresh_product_sha = ""
        if ctx._scoped.parent_mission_id.endswith("#gate") and ctx.completed_rec:
            try:
                ctx._existing_contract = rt.load_frozen_contract(
                    ctx.project_dir, ctx.completed_rec.get("contract_id", ""))
                ctx._fresh_product_sha, ctx._ = rt._prevalidated_product_head(
                    ctx.project_dir, ctx._existing_contract, None)
            except Exception:
                ctx._fresh_product_sha = ""
        ctx._recorded_product_shas = {
            str(rec.get("last_candidate_sha") or "")
            for rec in ctx.state.get("missions", {}).values()
            if isinstance(rec, dict)
            and rec.get("contract_id") == (ctx.completed_rec or {}).get("contract_id")
            and rec.get("state") == "PASS"
        }
        if (ctx._fresh_product_sha
                and ctx._fresh_product_sha not in ctx._recorded_product_shas
                and rt.supersede_recovery_by_valid_product(
                    ctx.project_dir, ctx._scoped.recovery_id, ctx._fresh_product_sha)):
            for ctx._mission_rec in ctx.state.get("missions", {}).values():
                if (isinstance(ctx._mission_rec, dict)
                        and ctx._mission_rec.get("contract_id") == ctx.completed_rec.get("contract_id")
                        and ctx._mission_rec.get("state") == "PASS"):
                    ctx._mission_rec["state"] = "UNPROVEN"
                    ctx._mission_rec["unproven_reason"] = (
                        "product HEAD advanced and independently validates; "
                        "fresh exact-SHA receipt required")
            for ctx._gap in ctx.state.get("validation_gaps", {}).values():
                if (isinstance(ctx._gap, dict) and ctx._gap.get("milestone") == ctx.mid
                        and ctx._gap.get("recovery_id") == ctx._scoped.recovery_id):
                    ctx._gap["resolved_at_product_sha"] = ctx._fresh_product_sha
                    ctx._gap["resolved_reason"] = "INDEPENDENT_PRODUCT_HEAD_VALIDATION"
            rt._event(ctx.state, "STALE_GATE_RECOVERY_SUPERSEDED", milestone=ctx.mid,
                   recovery_id=ctx._scoped.recovery_id,
                   product_sha=ctx._fresh_product_sha,
                   proof="independent exact-SHA validation")
            rt.save_project_state(ctx.project_dir, ctx.state)
            ctx._scoped = None
        if ctx._scoped is not None:
            rt._event(ctx.state, "MILESTONE_SCOPED_RECOVERY_DISPATCHED", milestone=ctx.mid,
                   recovery_id=ctx._scoped.recovery_id, recovery_kind=ctx._scoped.kind,
                   parent=ctx._scoped.parent_mission_id)
            rt.save_project_state(ctx.project_dir, ctx.state)
            return rt._execute_recovery_work(ctx.project_dir, ctx._scoped, ctx.state, ctx.mid,
                                          ctx.authority, ctx.executor)

    return None
