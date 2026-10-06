"""Étape extraite de `_execute_recovery_work` (factory_v2/runtime.py) : ce qui se passe une fois le…"""
from __future__ import annotations

import json

from .. import runtime as rt


def finish_recovery(project_dir, recovery, state, mid, authority, result):
    """Suite de `_execute_recovery_work` une fois `result` connu."""
    if result.ok:
        # Atomic: mark RecoveryWork complete + bump ladder in one idempotent operation.
        _defer_settlement = recovery.kind == "REPLAN"
        if not _defer_settlement:
            state = rt.settle_recovery_work_with_ladder(
                project_dir, recovery, state, success=True,
            )
        extra: dict = {}
        # LR3: for REPLAN, read back the patch and auto-apply it.
        if recovery.kind == "REPLAN":
            # R6: a REPLAN whose patch artifact is missing, unreadable or empty did not replan anything.
            artifact_p = project_dir / f"evidence/recovery/{recovery.recovery_id}_replan.json"
            replan_d = {}
            try:
                replan_d = json.loads(artifact_p.read_text())
                extra["proposed_roadmap_patch"] = replan_d.get("proposed_roadmap_patch", "") or ""
                extra["replan_rationale"] = replan_d.get("rationale", "")
            except Exception as _rex:
                # Covers unreadable JSON AND a well-formed artifact that is not an object: a top-level list or scalar has…
                replan_d = {}
                extra["proposed_roadmap_patch"] = ""
                extra["replan_artifact_error"] = f"{type(_rex).__name__}: {_rex}"
            if not isinstance(replan_d, dict):
                extra["replan_artifact_error"] = (
                    f"artifact is a {type(replan_d).__name__}, not a JSON object")
                replan_d = {}
                extra["proposed_roadmap_patch"] = ""
            # A REPLAN now delivers a DERIVED PLAN.
            _derived_raw = replan_d.get("derived_plan") if isinstance(replan_d, dict) else None
            if _derived_raw is not None:
                extra["derived_plan_proposed"] = "1"
            if not _derived_raw and not extra.get("proposed_roadmap_patch"):
                _reason = (f"REPLAN_ARTIFACT_INVALID: {artifact_p.name} missing, unreadable "
                           f"or carries neither a derived_plan nor a proposed_roadmap_patch "
                           f"({extra.get('replan_artifact_error', 'empty artifact')})")
                rt._event(state, "RECOVERY_WORK_FAILED", mission=recovery.parent_mission_id,
                          recovery_id=recovery.recovery_id, recovery_kind=recovery.kind,
                          failure_class=rt.FailureClass.SEMANTIC.value, output=_reason[:300])
                state = rt.settle_recovery_work_with_ladder(
                    project_dir, recovery, state, success=False,
                    failure_class=rt.FailureClass.SEMANTIC,
                )
                rt.save_project_state(project_dir, state)
                return rt.CycleResult("RECOVERY_WORK_FAILED", mid, _reason)
        if recovery.kind != "REPLAN":
            # NB: littéralement `_event(state, "RECOVERY_WORK_COMPLETE"` doit rester visible dans…
            rt._emit_recovery_work_complete(
                state, mission=recovery.parent_mission_id,
                recovery_id=recovery.recovery_id, recovery_kind=recovery.kind, **extra)
        if recovery.kind == "REPLAN" and _derived_raw:
            apply_result = rt._apply_derived_replan(
                project_dir=project_dir, state=state, recovery=recovery,
                authority=authority, raw_plan=_derived_raw)
            rt.save_project_state(project_dir, state)
            return apply_result
        if recovery.kind == "REPLAN" and extra.get("proposed_roadmap_patch"):
            patch_text = extra["proposed_roadmap_patch"]
            roadmap_path = project_dir / "ROADMAP.yaml"
            apply_result = rt._apply_replan_patch(
                state=state,
                roadmap_path=roadmap_path,
                patch_text=patch_text,
                milestone_id=recovery.milestone_id,
                rationale=extra.get("replan_rationale", ""),
            )
            # A REPLAN that produced only a refused authority patch produced nothing this loop can execute.
            state = rt.settle_recovery_work_with_ladder(
                project_dir, recovery, state, success=False,
                failure_class=rt.FailureClass.SEMANTIC)
            rt.save_project_state(project_dir, state)
            return apply_result
        if _defer_settlement:
            state = rt.settle_recovery_work_with_ladder(
                project_dir, recovery, state, success=True)
        rt.save_project_state(project_dir, state)
        return rt.CycleResult("RECOVERY_WORK_COMPLETE", mid,
                              f"RecoveryWork({recovery.kind}, {recovery.recovery_id}) succeeded — "
                              "resuming normal pipeline next cycle")
    else:
        fc = rt.classify_recovery_failure(result.rc, result.output_tail)
        rt._event(state, "RECOVERY_WORK_FAILED", mission=recovery.parent_mission_id,
                  recovery_id=recovery.recovery_id, recovery_kind=recovery.kind,
                  failure_class=fc.value, output=result.output_tail[-200:])
        # Atomic: classify failure + mark complete + bump ladder (only for semantic/exhausted).
        state = rt.settle_recovery_work_with_ladder(
            project_dir, recovery, state, success=False, failure_class=fc,
        )
        rt.save_project_state(project_dir, state)
        return rt.CycleResult("RECOVERY_WORK_FAILED", mid,
                              f"RecoveryWork({recovery.kind}, {recovery.recovery_id}) "
                              f"failed ({fc.value}): {result.output_tail[-200:]}")
