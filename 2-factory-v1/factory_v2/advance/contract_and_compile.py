"""Étape extraite de `_advance_locked` (factory_v2/runtime.py) : gel/rechargement du contrat pour le jalon…"""
from __future__ import annotations

from .. import runtime as rt


def run(ctx):
    """Bloc « contrat + compile_missions » de `_advance_locked`."""
    # G3 fix: NOT_COMPILABLE hot-spin prevention.
    if ctx.completed_rec and ctx.completed_rec.get("state") == "NOT_COMPILABLE":
        ctx.last_roadmap_sha = ctx.completed_rec.get("roadmap_sha_at_record")
        ctx.current_roadmap_sha = ctx.authority.identity.as_dict().get("roadmap_sha", "")
        if ctx.last_roadmap_sha == ctx.current_roadmap_sha:
            # Same fingerprint as the compile site: this is the same blocking condition seen again, so it must…
            ctx.edge = rt._not_compilable_edge(
                ctx.project_dir, ctx.state, ctx.mid, {},
                ctx.completed_rec.get("not_compilable_reason",
                                  f"NOT_COMPILABLE@{ctx.current_roadmap_sha[:12]}"))
            rt.save_project_state(ctx.project_dir, ctx.state)
            return ctx.edge or rt.CycleResult(
                "MILESTONE_NOT_COMPILABLE", ctx.mid,
                f"{ctx.mid} was NOT_COMPILABLE with roadmap@{ctx.current_roadmap_sha[:12]} — "
                "no new roadmap content; a REPLAN edge is in flight to supply the "
                "missing execution steps")
        # Roadmap changed since last NOT_COMPILABLE — clear the state and retry
        ctx.completed_rec = None

    if ctx.completed_rec and ctx.completed_rec.get("state") in (
            "IN_PROGRESS", "GATE_BLOCKED", "WAITING_FOR_EVIDENCE"):
        try:
            ctx.contract = rt.load_frozen_contract(ctx.project_dir, ctx.completed_rec["contract_id"])
        except rt.ContractIntegrityError as _cie:
            # The branch taken on every cycle after the first, and the one that reaches dispatch and the settlement…
            rt._event(ctx.state, "FROZEN_CONTRACT_INTEGRITY_FAILED", milestone=ctx.mid,
                   reason=str(_cie), path="IN_PROGRESS_LOAD")
            rt.save_project_state(ctx.project_dir, ctx.state)
            return rt.CycleResult("FROZEN_CONTRACT_INTEGRITY_FAILED", ctx.mid, str(_cie))
    else:
        try:
            ctx.contract = rt.freeze_contract(ctx.project_dir, ctx.authority, ctx.entry)
        except rt.ContractIntegrityError as _cie:
            # The stored definition of "complete" disagrees with the canonical one.
            rt._event(ctx.state, "FROZEN_CONTRACT_INTEGRITY_FAILED", milestone=ctx.mid,
                   reason=str(_cie))
            rt.save_project_state(ctx.project_dir, ctx.state)
            return rt.CycleResult("FROZEN_CONTRACT_INTEGRITY_FAILED", ctx.mid, str(_cie))
        ctx.state = rt.record_contract_in_state(ctx.project_dir, ctx.state, ctx.contract)
        # C36: stamp the authority this record is bound to AT CREATION — the NOT_COMPILABLE branches already did…
        ctx.state["completed_milestones"][ctx.mid] = {
            "state": "IN_PROGRESS",
            "contract_id": ctx.contract["contract_id"],
            "roadmap_sha_at_record": ctx.authority.identity.roadmap_sha,
        }
        rt.save_project_state(ctx.project_dir, ctx.state)

    ctx.drift = rt.verify_frozen_against_current(ctx.contract, ctx.authority)
    if ctx.drift:
        # Auto-clear the stale contract record so the next cycle freezes a fresh one from the current authority.
        ctx.state["completed_milestones"].pop(ctx.mid, None)
        rt._event(ctx.state, "STALE_CONTRACT_CLEARED", milestone=ctx.mid, drift=ctx.drift)
        rt._event(ctx.state, "AUTHORITY_DRIFT_BLOCKED", milestone=ctx.mid, drift=ctx.drift)
        rt.save_project_state(ctx.project_dir, ctx.state)
        return rt.CycleResult("AUTHORITY_DRIFT_BLOCKED", ctx.mid,
                           "frozen contract predates current canonical authority; "
                           "stale contract cleared — next cycle freezes fresh",
                           authority_drift=ctx.drift)

    try:
        ctx.missions = rt.compile_missions(ctx.authority, ctx.entry, ctx.contract["contract_id"])
    except rt.MissionCompileError as compile_err:
        # P0-B: strict compiler — string steps, missing executor, unknown type, missing local_command command are…
        rt._event(ctx.state, "MILESTONE_NOT_COMPILABLE", milestone=ctx.mid,
               reason=str(compile_err))
        ctx.state["completed_milestones"][ctx.mid]["state"] = "NOT_COMPILABLE"
        ctx.state["completed_milestones"][ctx.mid]["roadmap_sha_at_record"] = \
            ctx.authority.identity.as_dict().get("roadmap_sha", "")
        ctx._nc_reason = f"MISSION_COMPILE_ERROR: {compile_err}"
        ctx.state["completed_milestones"][ctx.mid]["not_compilable_reason"] = ctx._nc_reason[:300]
        ctx.edge = rt._not_compilable_edge(ctx.project_dir, ctx.state, ctx.mid, ctx.contract, ctx._nc_reason)
        rt.save_project_state(ctx.project_dir, ctx.state)
        return ctx.edge or rt.CycleResult("MILESTONE_NOT_COMPILABLE", ctx.mid, str(compile_err))
    if not ctx.missions:
        rt._event(ctx.state, "MILESTONE_NOT_COMPILABLE", milestone=ctx.mid,
               reason="no execution steps defined in roadmap — awaiting owner orders")
        ctx.state["completed_milestones"][ctx.mid]["state"] = "NOT_COMPILABLE"
        # G3: record roadmap SHA so we can detect when ROADMAP.yaml is updated
        ctx.state["completed_milestones"][ctx.mid]["roadmap_sha_at_record"] = \
            ctx.authority.identity.as_dict().get("roadmap_sha", "")
        ctx._nc_reason = "NO_EXECUTION_STEPS: roadmap milestone defines no execution.steps"
        ctx.state["completed_milestones"][ctx.mid]["not_compilable_reason"] = ctx._nc_reason
        ctx.edge = rt._not_compilable_edge(ctx.project_dir, ctx.state, ctx.mid, ctx.contract, ctx._nc_reason)
        rt.save_project_state(ctx.project_dir, ctx.state)
        return ctx.edge or rt.CycleResult("MILESTONE_NOT_COMPILABLE", ctx.mid,
                                   "roadmap milestone has no execution steps — cannot "
                                   "fabricate work (FALSE_SATISFIED = 0)")

    return None
