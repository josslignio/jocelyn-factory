"""Étape extraite de `_advance_locked` (factory_v2/runtime.py) : le corps de la boucle « pour chaque…"""
from __future__ import annotations

from .. import runtime as rt
from ..escalation_ladder import RecoveryLedgerUnknown


def _read_pending_recovery(ctx):
    """Read the oldest pending recovery into ctx.pending_recovery."""
    try:
        ctx.pending_recovery = rt.read_pending_recovery_work(ctx.project_dir)
    except RecoveryLedgerUnknown as exc:
        rt._event(ctx.state, "RECOVERY_LEDGER_UNKNOWN", milestone=ctx.mid, error=str(exc)[:200])
        rt.save_project_state(ctx.project_dir, ctx.state)
        return rt.CycleResult("RECOVERY_LEDGER_UNKNOWN", ctx.mid,
                              f"{ctx.mid}: recovery ledger unreadable — refusing to dispatch "
                              f"as if nothing were pending: {exc}")
    return None

_CONTINUE = object()



def _budget_body(contract) -> dict:
    """Contract body + the freeze instant, so the launch cap counts only the launches made under THIS contract…"""
    body = dict((contract or {}).get("body") or {})
    fz = (contract or {}).get("frozen_at")
    if fz:
        body["_frozen_at"] = str(fz)
    return body

def _ss1_adopt_or_skip_pass(ctx):
    """Un PASS existant contradictoire (ledger) est ecarte ; un PASS compatible (meme contrat, ou lignee…"""
    from ..safety_quarantine import assert_action_allowed, QuarantineActionForbidden

    ctx.mission_rec = ctx.state["missions"].get(ctx.mission.id, {})
    if (ctx.mission_rec.get("state") == "MILESTONE_PARKED"
            and (rt._waiting_until(ctx.mission_rec.get("parked_until")) or rt.datetime.min.replace(tzinfo=rt.timezone.utc))
            > rt.datetime.now(rt.timezone.utc)):
        return _CONTINUE                    # expiry is self-healing: next cycle resumes normally
    if (ctx.mission_rec.get("state") == "PASS"
            and rt._mission_pass_contradicted_by_lost_attempt(ctx.project_dir, ctx.mission_rec)):
        # C26: the ledger, not the PROJECT_STATE claim, is the evidence of record for this exact attempt — a PASS…
        from ..blocked_event_dedup import should_emit as _should_emit_redispatch
        ctx._rd_detail = {"last_attempt_key": ctx.mission_rec.get("last_attempt_key", "")}
        if _should_emit_redispatch(ctx.state, ctx.mission.id,
                                  "MISSION_REDISPATCH_AFTER_LOST", ctx._rd_detail):
            rt._event(ctx.state, "MISSION_REDISPATCH_AFTER_LOST", milestone=ctx.mid,
                   mission=ctx.mission.id,
                   last_attempt_key=ctx.mission_rec.get("last_attempt_key", ""),
                   reason="TERMINAL_KILL_OR_LOST_NO_CANDIDATE")
            rt.save_project_state(ctx.project_dir, ctx.state)
        ctx._rd_rec = ctx.state["missions"][ctx.mission.id]
        ctx._rd_rec.pop("state", None)
        ctx._rd_rec.pop("last_attempt_id", None)
        ctx._rd_rec.pop("last_attempt_key", None)
        ctx.mission_rec = ctx._rd_rec
    if ctx.mission_rec.get("state") == "PASS":
        # 2026-09-18: a PASS earned by a step of another TYPE (local_command re-check → autopilot repair, same…
        _prev_type = str(ctx.mission_rec.get("executor_type") or "")
        _cur_type = str((ctx.mission.executor or {}).get("type") or "")
        if _prev_type and _cur_type and _prev_type != _cur_type:
            ctx._rec = ctx.state["missions"][ctx.mission.id]
            ctx._rec["state"] = "UNPROVEN"
            ctx._rec["unproven_reason"] = f"STEP_TYPE_CHANGED: {_prev_type} → {_cur_type}"
            ctx._rec["unproven_at"] = rt.time.strftime("%Y-%m-%dT%H:%M:%SZ", rt.time.gmtime())
            ctx._rec.pop("last_attempt_id", None)
            rt._event(ctx.state, "MISSION_PASS_UNPROVEN", milestone=ctx.mid,
                      mission=ctx.mission.id,
                      reason=f"STEP_TYPE_CHANGED: PASS came from a {_prev_type} step, "
                             f"the mission is now {_cur_type}")
            rt.save_project_state(ctx.project_dir, ctx.state)
            return rt.CycleResult("MISSION_UNPROVEN_REEXECUTE", ctx.mid,
                                  f"{ctx.mid}: {ctx.mission.id} step type changed "
                                  f"({_prev_type} → {_cur_type}); re-executes")
        if ctx.mission_rec.get("contract_id") == ctx.contract.get("contract_id"):
            return _CONTINUE
        # R2.3: a mission id is stable across roadmap edits, but its meaning is not.
        from ..mission_receipt import (ReceiptVerificationError,
                                      load_verified_mission_receipt)
        try:
            load_verified_mission_receipt(
                ctx.project_dir, ctx.mission.id,
                ctx.mission_rec.get("last_attempt_id", ""),
                contract_id=ctx.contract.get("contract_id", ""),
                contract_signature=ctx.contract.get("contract_signature", ""))
        except ReceiptVerificationError as _rve:
            # INCOMPATIBLE_CONTRACT: the acceptance semantics changed, so the old receipt/checkpoint can NEVER settle…
            ctx.history = ctx.state.setdefault("superseded_mission_results", [])
            ctx.history.append({
                "mission_id": ctx.mission.id,
                "superseded_reason": "CONTRACT_CHANGED",
                "superseded_at_contract_id": ctx.contract.get("contract_id", ""),
                **ctx.mission_rec,
            })
            if len(ctx.history) > 200:
                del ctx.history[:-200]
            ctx._rec = ctx.state["missions"][ctx.mission.id]
            ctx._rec["state"] = "UNPROVEN"
            ctx._rec["unproven_reason"] = str(_rve)[:300]
            ctx._rec["unproven_at"] = rt.time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", rt.time.gmtime())
            ctx._rec.pop("last_attempt_id", None)
            rt._event(ctx.state, "MISSION_PASS_UNPROVEN", milestone=ctx.mid,
                   mission=ctx.mission.id,
                   reason=f"CONTRACT_INCOMPATIBLE: {str(_rve)[:240]}")
            rt.save_project_state(ctx.project_dir, ctx.state)
            return rt.CycleResult(
                "MISSION_UNPROVEN_REEXECUTE", ctx.mid,
                f"{ctx.mid}: {ctx.mission.id} PASS is bound to superseded contract "
                f"{ctx.mission_rec.get('contract_id', '')!r} whose validation "
                "semantics differ from the current frozen contract — the "
                "old receipt and checkpoint can never settle it; the "
                "mission re-executes under the new contract")
        # Compatible lineage: the authority hashes moved (owner edit, or the REPLAN recovery stage itself) but…
        _previous_contract = ctx.mission_rec.get("contract_id", "")
        ctx.state["missions"][ctx.mission.id]["contract_id"] = ctx.contract["contract_id"]
        rt._event(ctx.state, "MISSION_PASS_ADOPTED_COMPATIBLE_CONTRACT",
               mission=ctx.mission.id,
               previous_contract=_previous_contract,
               current_contract=ctx.contract.get("contract_id", ""),
               reused_attempt=ctx.mission_rec.get("last_attempt_id", ""))
        rt.save_project_state(ctx.project_dir, ctx.state)
        ctx.mission_rec = ctx.state["missions"][ctx.mission.id]
        return _CONTINUE

    return None


def _ss2_escalation_and_recovery_gate(ctx):
    """Echelle d'escalade fingerprint (RETRY -> ALTERNATE_ENGINE -> DIAGNOSE -> REPLAN -> SAFETY_STOP), CNP…"""
    owner_dispatch_precedence = False
    # G3 fingerprint-based escalation ladder (replaces simplistic attempt_count cap).
    ctx._regrant = rt._cnp_regrant_stranded_diagnose(ctx.project_dir, ctx.state, ctx.mid, ctx.contract)
    if ctx._regrant is not None:
        return ctx._regrant
    ctx._blocked_recovery = _read_pending_recovery(ctx)
    if ctx._blocked_recovery is not None:
        return ctx._blocked_recovery
    # C26: a CNP-created DIAGNOSE (recovery_id prefixed "cnp_") can be left pending on disk from BEFORE an…
    if (ctx.pending_recovery and ctx.pending_recovery.parent_mission_id == ctx.mission.id
            and str(ctx.pending_recovery.recovery_id or "").startswith("cnp_")):
        from .. import recovery_allocation as _recovery_allocation
        from ..launch_gate import current_code_sha as _current_code_sha
        ctx._owner_alloc = _recovery_allocation.available(
            ctx.project_dir, milestone=ctx.mid,
            control_code_sha=_current_code_sha(),
            expect_project=str(ctx.state.get("project") or ""),
            basis=_recovery_allocation.BASIS_OWNER)
        if ctx._owner_alloc.get("allocation_id"):
            owner_dispatch_precedence = True
            from ..blocked_event_dedup import should_emit as _should_emit_owner_prec
            ctx._prec_detail = {"allocation_id": ctx._owner_alloc["allocation_id"],
                            "superseded_recovery_id": ctx.pending_recovery.recovery_id}
            if _should_emit_owner_prec(ctx.state, ctx.mission.id,
                                      "OWNER_GRANT_DISPATCH_PRECEDENCE", ctx._prec_detail):
                rt._event(ctx.state, "OWNER_GRANT_DISPATCH_PRECEDENCE", milestone=ctx.mid,
                       mission=ctx.mission.id,
                       allocation_id=ctx._owner_alloc["allocation_id"],
                       superseded_recovery_id=ctx.pending_recovery.recovery_id)
                rt.save_project_state(ctx.project_dir, ctx.state)
            ctx.pending_recovery = None  # fall through to direct dispatch below
    if ctx.pending_recovery and ctx.pending_recovery.parent_mission_id == ctx.mission.id:
        return rt._execute_recovery_work(ctx.project_dir, ctx.pending_recovery, ctx.state, ctx.mid, ctx.authority, ctx.executor)

    # R2C: cheap COST_NO_PROGRESS peek BEFORE clear_worker_state, the lease grant, the context manifest and…
    ctx._cnp = rt._cnp_peek(ctx.project_dir, ctx.state, ctx.mid, ctx.contract, ctx.authority, ctx.mission)
    if ctx._cnp is not None:
        return ctx._cnp

    if ctx.mission_rec.get("state") == "FAIL":
        ctx.last_fp = ctx.mission_rec.get("last_failure_fingerprint", "")
        ctx.last_engine = ctx.mission_rec.get("last_engine", "")
        if not ctx.last_fp:
            if owner_dispatch_precedence:
                # The owner has already resumed this mission over its pending CNP diagnosis.
                return None
            # 2026-09-18: an empty fingerprint on a FAIL record means no engine ever produced a verdict (quota cut…
            ctx.spec_level = ctx.mission.executor.get("spec_level", "B")
            ctx.alt_engine = rt.next_alternate_engine(
                ctx.last_engine or "glm", spec_level=ctx.spec_level, state=ctx.state,
                project=ctx.authority.identity.project)
            ctx.state["missions"][ctx.mission.id]["preferred_engine"] = ctx.alt_engine
            ctx.state["missions"][ctx.mission.id]["state"] = "UNPROVEN"
            rt._event(ctx.state, "EMPTY_FINGERPRINT_ENGINE_ROTATION", mission=ctx.mission.id,
                      from_engine=(ctx.last_engine or ""), to_engine=ctx.alt_engine)
            rt.save_project_state(ctx.project_dir, ctx.state)
            return rt.CycleResult("MISSION_FAILED", ctx.mid,
                                  f"{ctx.mission.id} empty failure fingerprint — engine rotated "
                                  f"{ctx.last_engine!r} → {ctx.alt_engine!r}, mission back to UNPROVEN")
        ctx.cap_reached = rt.is_equivalent_retry_cap_reached(
            state=ctx.state,
            mission_id=ctx.mission.id,
            engine=ctx.last_engine,
            strategy_sha=ctx.authority.identity.strategy_sha,
            failure_fingerprint=ctx.last_fp,
            context_hash=ctx.contract.get("contract_id", ""),
            authority_hash=ctx.authority.identity.roadmap_sha,
        )
        if ctx.cap_reached:
            # A1 (CTO DIRECTIVE 033 §3 R2) — THE trigger.
            try:
                from ..audit_cell_bridge import persist_audit_cell_request
                persist_audit_cell_request(
                    ctx.project_dir,
                    milestone_id=str(ctx.mid),
                    fingerprint=str(ctx.last_fp),
                    position={
                        "mission_id": ctx.mission.id,
                        "contract_id": ctx.contract.get("contract_id", ""),
                        "candidate_sha": ctx.mission_rec.get("candidate_sha", ""),
                        "last_engine": ctx.last_engine,
                        "strategy_sha": ctx.authority.identity.strategy_sha,
                        "roadmap_sha": ctx.authority.identity.roadmap_sha,
                        "spec_level": ctx.mission.executor.get("spec_level", "B"),
                    },
                    trigger_kind="REPEATED_EQUIVALENT_FAILURE",
                )
            except Exception:                       # noqa: BLE001
                pass                                # never block recovery on the cell
            ctx.stage, ctx.n = rt.get_escalation_stage(
                state=ctx.state,
                mission_id=ctx.mission.id,
                engine=ctx.last_engine,
                strategy_sha=ctx.authority.identity.strategy_sha,
                failure_fingerprint=ctx.last_fp,
                context_hash=ctx.contract.get("contract_id", ""),
                authority_hash=ctx.authority.identity.roadmap_sha,
            )
            rt._event(ctx.state, "ESCALATION_REQUIRED", mission=ctx.mission.id,
                   stage=ctx.stage.value, equivalent_attempts=ctx.n,
                   failure_fingerprint=ctx.last_fp[:64] if ctx.last_fp else "")
            # F5R5: escalation stage CAUSES an action, not just a return value.
            if ctx.stage in (rt.EscalationStage.RETRY, rt.EscalationStage.ALTERNATE_ENGINE):
                ctx.spec_level = ctx.mission.executor.get("spec_level", "B")
                ctx.alt_engine = rt.next_alternate_engine(ctx.last_engine, spec_level=ctx.spec_level, state=ctx.state, project=ctx.authority.identity.project)
                ctx.state["missions"][ctx.mission.id]["preferred_engine"] = ctx.alt_engine
                # 2026-09-19 (FACTORY_CHAOS_DRILL_V1#M1) : la rotation ne sert à rien si la mission reste FAIL — _ss2…
                ctx.state["missions"][ctx.mission.id]["state"] = "UNPROVEN"
                # F5R5 Defect-2: advance the escalation cursor so that if the alternate engine also fails with the same…
                ctx.state = rt._escalation_record_attempt(
                    ctx.state, ctx.mission.id, ctx.last_engine,
                    ctx.authority.identity.strategy_sha,
                    ctx.last_fp,
                    ctx.contract.get("contract_id", ""),
                    ctx.authority.identity.roadmap_sha,
                )
                rt.save_project_state(ctx.project_dir, ctx.state)
                return rt.CycleResult("MISSION_FAILED", ctx.mid,
                                   f"{ctx.mission.id} ALTERNATE_ENGINE: rotating from "
                                   f"{ctx.last_engine!r} → {ctx.alt_engine!r} for next dispatch")
            elif ctx.stage in (rt.EscalationStage.DIAGNOSE, rt.EscalationStage.REPLAN):
                import hashlib as _hl
                ctx.recovery_id = _hl.sha256(
                    f"{ctx.mission.id}:{ctx.stage.value}:{ctx.n}:{rt.time.strftime('%Y%m%d%H%M%S')}".encode()
                ).hexdigest()[:16]
                ctx.task_gen = rt.generate_diagnose_task if ctx.stage == rt.EscalationStage.DIAGNOSE \
                           else rt.generate_replan_task
                from ..escalation_ladder import _identity_key as _ikey
                ctx._ikey_val = _ikey(
                    ctx.mission.id, ctx.last_engine,
                    ctx.authority.identity.strategy_sha, ctx.last_fp,
                    ctx.contract.get("contract_id", ""), ctx.authority.identity.roadmap_sha,
                )
                ctx.recovery = rt.RecoveryWork(
                    recovery_id=ctx.recovery_id,
                    kind=ctx.stage.value.upper(),
                    parent_mission_id=ctx.mission.id,
                    milestone_id=ctx.mid,
                    failure_fingerprint=ctx.last_fp[:200],
                    escalation_stage=ctx.stage.value,
                    attempt_count=ctx.n,
                    created_at=rt.time.strftime("%Y-%m-%dT%H:%M:%SZ", rt.time.gmtime()),
                    engine=ctx.last_engine,
                    identity_key=ctx._ikey_val,
                )
                ctx.task_path = ctx.task_gen(ctx.project_dir, ctx.recovery)
                # Absolute, with its authority root recorded.
                rt._set_task_file(ctx.task_path, ctx.recovery)
                rt.write_recovery_work(ctx.project_dir, ctx.recovery)
                rt.save_project_state(ctx.project_dir, ctx.state)
                return rt.CycleResult("RECOVERY_WORK_CREATED", ctx.mid,
                                   f"{ctx.mission.id} → RecoveryWork({ctx.stage.value}, id={ctx.recovery_id})")
            else:  # SAFETY_STOP
                from ..continuation_controller import write_safety_stop
                write_safety_stop(ctx.project_dir, cause=f"{ctx.mission.id} exhausted all recovery stages "
                                  f"(equivalent_attempts={ctx.n})",
                                  invariant="ESCALATION_SAFETY_STOP",
                                  evidence={"mission_id": ctx.mission.id, "attempts": ctx.n,
                                            "failure_fingerprint": ctx.last_fp[:200]})
                rt.save_project_state(ctx.project_dir, ctx.state)
                return rt.CycleResult("SAFETY_STOP", ctx.mid,
                                   f"{ctx.mission.id} SAFETY_STOP: all recovery stages exhausted "
                                   f"(equivalent_attempts={ctx.n})")

    return None


def _ss3_pre_dispatch_gates(ctx):
    """Rechargement frais + drift, in-flight, quarantaine (gate produit), preflight task_file/verifier, engine…"""
    from ..safety_quarantine import assert_action_allowed, QuarantineActionForbidden

    ctx.fresh = rt.load_authority(ctx.project_dir)
    try:
        ctx.contract_now = rt.load_frozen_contract(ctx.project_dir, ctx.contract["contract_id"])
    except rt.ContractIntegrityError as _cie:
        rt._event(ctx.state, "FROZEN_CONTRACT_INTEGRITY_FAILED", milestone=ctx.mid,
               mission=ctx.mission.id, reason=str(_cie), path="PRE_DISPATCH_RELOAD")
        rt.save_project_state(ctx.project_dir, ctx.state)
        return rt.CycleResult("FROZEN_CONTRACT_INTEGRITY_FAILED", ctx.mid, str(_cie))
    ctx.drift = rt.verify_frozen_against_current(ctx.contract_now, ctx.fresh)
    if ctx.drift:
        rt._event(ctx.state, "STALE_CONTEXT_DISPATCH_BLOCKED",
               mission=ctx.mission.id, drift=ctx.drift)
        rt.save_project_state(ctx.project_dir, ctx.state)
        return rt.CycleResult("STALE_CONTEXT_DISPATCH_BLOCKED", ctx.mid,
                           "authority changed under frozen contract",
                           authority_drift=ctx.drift)
    # P0-D: no two writers on one worktree.
    if rt.is_worker_alive(ctx.project_dir, ctx.mission.id):
        rt._event(ctx.state, "MISSION_IN_FLIGHT_DETECTED", mission=ctx.mission.id)
        rt.save_project_state(ctx.project_dir, ctx.state)
        return rt.CycleResult("MISSION_IN_FLIGHT", ctx.mid,
                           f"{ctx.mission.id}: live worker detected — refusing double-dispatch. "
                           "NO_TWO_WRITERS_ON_ONE_WORKTREE = 1")
    # F1: product-dispatch gate enforced at the actual dispatch point, after the control plane has already…
    if ctx._scoped_mid is None or ctx.mid != ctx._scoped_mid:
        try:
            assert_action_allowed(ctx.project_dir, "DISPATCH_PRODUCT")
        except QuarantineActionForbidden as _qf:
            rt._event(ctx.state, "PRODUCT_DISPATCH_BLOCKED", mission=ctx.mission.id,
                   reason=str(_qf)[:200])
            rt.save_project_state(ctx.project_dir, ctx.state)
            return rt.CycleResult("SAFETY_QUARANTINE_BLOCKED", ctx.mid, str(_qf))
    rt.clear_worker_state(ctx.project_dir, ctx.mission.id)  # remove any stale heartbeat before dispatch
    # LOT C25: structural task_file preflight — entirely inside the supervisor, before…
    ctx._tf_refusal = (rt._task_file_structural_refusal(ctx.mission)
                  if ctx.executor is None else None)
    if ctx._tf_refusal is not None:
        ctx._tf_fp = rt._task_file_refusal_fingerprint(ctx._tf_refusal, ctx.authority)
        ctx._tf_mrec = ctx.state.setdefault("missions", {}).setdefault(ctx.mission.id, {})
        if ctx._tf_mrec.get("preflight_refused_fingerprint") == ctx._tf_fp:
            # Identical condition already recorded once — same mission, reason, task_file bytes/string, authority and…
            return rt.CycleResult(
                "MISSION_HELD_PREFLIGHT", ctx.mid,
                f"{ctx.mission.id}: held on {ctx._tf_refusal['reason']} — "
                f"{ctx._tf_refusal['detail']} (unchanged since first observed "
                "— fix the task_file, or change the authority/control "
                "code, to re-arm)")
        # FIRST observation of this exact condition: exactly one durable TERMINAL row.
        from ..launch_gate import close_account as _tf_close_account
        from ..launch_gate import open_account as _tf_open_account
        ctx._tf_account = _tf_open_account(
            ctx.project_dir,
            attempt_key=f"preflight-refused:{ctx.mission.id}:{ctx._tf_fp}",
            project=ctx.state.get("project", ""), milestone=ctx.mid,
            mission=ctx.mission.id, attempt=0, engine="none", model="",
            spec_level=ctx.mission.executor.get("spec_level", ""),
            task_kind=("local" if ctx.mission.executor.get("type") == "local_command"
                       else "build"),
            consumes_model_quota=False,
            contract_body=_budget_body(ctx.contract))
        _tf_close_account(
            ctx.project_dir, ctx._tf_account, exit_class="PREFLIGHT_REFUSED",
            mission_pass=False, duration_s=0.0, consumes_model_quota=False)
        ctx._tf_mrec["preflight_refused_fingerprint"] = ctx._tf_fp
        rt._event(ctx.state, "PREFLIGHT_REFUSED", mission=ctx.mission.id,
               reason=ctx._tf_refusal["reason"],
               detail=ctx._tf_refusal["detail"][:400],
               task_file=ctx._tf_refusal.get("task_file", "")[:300])
        rt.save_project_state(ctx.project_dir, ctx.state)
        return rt.CycleResult(
            "PREFLIGHT_REFUSED", ctx.mid,
            f"{ctx.mission.id}: {ctx._tf_refusal['reason']} — {ctx._tf_refusal['detail']} "
            "(refused before lease/manifest/account/executor — no engine "
            "could possibly have launched; spent no model quota, no "
            "recovery allocation)")
    # R2B1: cheap VERIFIER_NOT_IMPLEMENTED preflight, run BEFORE lease grant, context manifest, account open…
    ctx._preflight_body = _budget_body(ctx.contract)
    ctx._preflight_test_cmd = (ctx._preflight_body.get("product_e2e") or {}).get("test_command")
    ctx._verifier_preflight_result = None
    if ctx._preflight_test_cmd:
        from ..verifier_preflight import check_test_command_not_implemented
        ctx._verifier_preflight_result = check_test_command_not_implemented(
            ctx._preflight_test_cmd,
            cwd=rt._repair_command_cwd(ctx.project_dir, ctx._preflight_body, ctx.authority))
        if ctx._verifier_preflight_result.get("not_implemented"):
            from ..blocked_event_dedup import should_emit as _should_emit_blocked
            if _should_emit_blocked(ctx.state, ctx.mission.id, "VERIFIER_NOT_IMPLEMENTED",
                                    ctx._verifier_preflight_result):
                rt._event(ctx.state, "LAUNCH_BLOCKED", mission=ctx.mission.id,
                       reason="VERIFIER_NOT_IMPLEMENTED",
                       detail=str(ctx._verifier_preflight_result)[:400])
                rt.save_project_state(ctx.project_dir, ctx.state)
            return rt.CycleResult(
                "LAUNCH_BLOCKED", ctx.mid,
                f"engine launch refused for {ctx.mission.id}: VERIFIER_NOT_IMPLEMENTED — "
                "milestone verifier reports NOT_IMPLEMENTED; refusing to spend model "
                "quota to learn that the expensive way")
        # TST-01: a pytest test_command whose -k/-m filter selects ZERO tests (typo, renamed test, stale marker)…
        if rt._re.search(r"(^|[\s/])pytest(\s|$)", ctx._preflight_test_cmd) or " -m pytest" in ctx._preflight_test_cmd:
            from ..verifier_preflight import check_no_tests_selected
            ctx._nts_result = check_no_tests_selected(
                rt._repair_command_cwd(ctx.project_dir, ctx._preflight_body, ctx.authority),
                ctx._preflight_test_cmd)
            if ctx._nts_result.get("no_tests_selected"):
                from ..blocked_event_dedup import should_emit as _should_emit_blocked
                if _should_emit_blocked(ctx.state, ctx.mission.id, "NO_TESTS_SELECTED", ctx._nts_result):
                    rt._event(ctx.state, "LAUNCH_BLOCKED", mission=ctx.mission.id,
                           reason="NO_TESTS_SELECTED",
                           detail=str(ctx._nts_result)[:400])
                    rt.save_project_state(ctx.project_dir, ctx.state)
                return rt.CycleResult(
                    "LAUNCH_BLOCKED", ctx.mid,
                    f"engine launch refused for {ctx.mission.id}: NO_TESTS_SELECTED — "
                    "the frozen product_e2e.test_command collects 0 tests "
                    "(pytest rc=5); the milestone cannot be settled by building — "
                    "fix the test_command/-k filter in the roadmap")
    # ECO-01 residual: cheap BUDGET_EXHAUSTED / IDENTICAL_ATTEMPT_* peek, run BEFORE lease grant, context…
    from ..verifier_preflight import check_engine_capacity as _check_engine_capacity
    ctx._exec_type = str((getattr(ctx.mission, "executor", None) or {}).get("type", "")) if isinstance(getattr(ctx.mission, "executor", None), dict) else ""
    # Only missions that will LAUNCH an engine need engine capacity; a local_command /…
    ctx._cap = _check_engine_capacity(ctx._preflight_body) if ctx._exec_type == "autopilot" else {"observed": False, "capable": True}
    if ctx._cap.get("observed") and not ctx._cap.get("capable", True):
        # Block only when a BUILD/REPAIR engine cannot launch: that is the expensive path.
        ctx._missing = [m for m in (ctx._cap.get("missing") or []) if isinstance(m, dict)]
        ctx._blocking = [m for m in ctx._missing if m.get("role") in ("build", "repair")]
        from ..blocked_event_dedup import should_emit as _should_emit_blocked
        if ctx._blocking:
            if _should_emit_blocked(ctx.state, ctx.mission.id, "ENGINE_CAPACITY_MISSING", ctx._cap):
                rt._event(ctx.state, "LAUNCH_BLOCKED", mission=ctx.mission.id,
                       reason="ENGINE_CAPACITY_MISSING",
                       detail=str(ctx._blocking)[:400])
                rt.save_project_state(ctx.project_dir, ctx.state)
            return rt.CycleResult(
                "LAUNCH_BLOCKED", ctx.mid,
                f"engine launch refused for {ctx.mission.id}: ENGINE_CAPACITY_MISSING — "
                f"{str(ctx._blocking)[:300]} (refused before lease/manifest/prediction)")
        if _should_emit_blocked(ctx.state, ctx.mission.id, "ENGINE_CAPACITY_REVIEW_DEGRADED", ctx._cap):
            rt._event(ctx.state, "ENGINE_CAPACITY_REVIEW_DEGRADED", mission=ctx.mission.id,
                   detail=str(ctx._missing)[:400])
            rt.save_project_state(ctx.project_dir, ctx.state)
    from ..launch_gate import peek_launch_blockers as _peek_launch_blockers
    ctx._early_block = _peek_launch_blockers(
        ctx.project_dir, mission_id=ctx.mission.id, milestone=ctx.mid,
        contract_body=ctx._preflight_body, mission=ctx.mission,
        own_lease_generation=None,
        # L1_advance-006: the supervisor's allowlist proof (an audited local verifier that launches no model) is…
        supervisor_proved_no_model=rt._trusted_local_command_uses_no_model(ctx.mission))
    if ctx._early_block is not None:
        from ..blocked_event_dedup import should_emit as _should_emit_blocked
        if _should_emit_blocked(ctx.state, ctx.mission.id, ctx._early_block["reason"],
                                ctx._early_block["detail"]):
            rt._event(ctx.state, "LAUNCH_BLOCKED", mission=ctx.mission.id,
                   reason=ctx._early_block["reason"],
                   detail=str(ctx._early_block["detail"])[:400])
            rt.save_project_state(ctx.project_dir, ctx.state)
        return rt.CycleResult(
            "LAUNCH_BLOCKED", ctx.mid,
            f"engine launch refused for {ctx.mission.id}: {ctx._early_block['reason']} — "
            "refused before lease grant/manifest/prediction (ECO-01 residual peek)")
    # LR2: grant a worker lease for this dispatch.
    ctx._lease = rt.WorkerLease.grant(ctx.mission.id, ctx.project_dir)
    ctx.state.setdefault("missions", {}).setdefault(ctx.mission.id, {})
    ctx.state["missions"][ctx.mission.id]["lease_generation"] = ctx._lease.generation
    ctx.mission.executor["_lease_generation"] = str(ctx._lease.generation)
    # L5_factory_v2_rest_3-015: the renewer proves ownership with the token too.
    ctx.mission.executor["_lease_fencing_token"] = ctx._lease.fencing_token
    # LR5: CanonicalContextManifest is built and hashed BEFORE EACH dispatch — not only at preflight.
    try:
        from ..canonical_context import build_manifest as _build_manifest
        ctx._manifest = _build_manifest(ctx.project_dir, mission_id=ctx.mission.id)
        ctx.state["missions"][ctx.mission.id]["context_manifest_sha"] = ctx._manifest.manifest_sha
        rt._event(ctx.state, "DISPATCH_CONTEXT_MANIFEST", mission=ctx.mission.id,
               manifest_sha=ctx._manifest.manifest_sha)
    except Exception as _mexc:
        ctx.state["missions"][ctx.mission.id]["context_manifest_sha"] = ""
        rt._event(ctx.state, "DISPATCH_MANIFEST_FAILED", mission=ctx.mission.id,
               error=str(_mexc)[:200])
    rt.save_project_state(ctx.project_dir, ctx.state)  # durable before executor launch
    # F5R5: read preferred_engine from state (set by ALTERNATE_ENGINE action) and forward to executor so the…
    ctx.preferred_engine = ctx.state.get("missions", {}).get(ctx.mission.id, {}).get("preferred_engine", "")

    return None


def _ss4_lease_and_checkpoint(ctx):
    """Manifeste canonique, lease worker, preparation du checkpoint de dispatch (reprise apres crash)…"""
    ctx.runner = ctx.executor or rt._make_executor(ctx.project_dir, ctx.mission, preferred_engine=ctx.preferred_engine)
    # Spend local compute before model quota.
    ctx._satisfied_sha = ""
    _mrec_rb = (ctx.state.get("missions") or {}).get(ctx.mission.id) or {}
    if ctx.executor is None and not (ctx.mission.executor or {}).get("_review_block_file") \
            and not _mrec_rb.get("review_block_file"):
        # 2026-09-18: a rebuild ordered by a reviewer's BLOCK must run a model — the mechanical oracle is green…
        ctx._satisfied_sha, ctx._satisfied_repo = rt._prevalidated_product_head(
            ctx.project_dir, ctx.contract, ctx.mission)
        if ctx._satisfied_sha and ctx._satisfied_repo is not None:
            # Keep the compiled mission immutable: settlement uses ctx.missions to find its canonical autopilot…
            from dataclasses import replace as _replace
            ctx.mission = _replace(ctx.mission, executor=dict(ctx.mission.executor or {}))
            ctx._lease_generation = ctx.mission.executor.get("_lease_generation", "")
            ctx.mission.executor.clear()
            ctx.mission.executor.update({
                "type": "local_command",
                "cwd": "product_repo",
                "product_repo": str(ctx._satisfied_repo),
                "command": ("test \"$(git rev-parse HEAD)\" = "
                            f"\"{ctx._satisfied_sha}\""),
                "_lease_generation": ctx._lease_generation,
                "_validated_integration_sha": ctx._satisfied_sha,
            })
            ctx.runner = rt.LocalCommandExecutor(ctx.project_dir)
            rt._event(ctx.state, "MODEL_CALL_SKIPPED_EXISTING_PRODUCT_VALID",
                   mission=ctx.mission.id, integration_sha=ctx._satisfied_sha,
                   proof="independent exact-SHA prevalidation")
            rt.save_project_state(ctx.project_dir, ctx.state)
    # G5 two-phase RunRecords — PREDICTION is durable BEFORE dispatch.
    ctx._attempt_count_before = ctx.state.get("missions", {}).get(ctx.mission.id, {}).get("attempt_count", 0)
    ctx._attempt_id = f"a{ctx._attempt_count_before + 1}"
    ctx._phase_attempt_id = (f"{ctx.mission.id}:{ctx.contract['contract_id'][:16]}:"
                         f"{ctx._attempt_id}:g{ctx._lease.generation}")
    # Bind dispatcher-side launch telemetry to the supervisor's durable attempt identity before the child…
    ctx.mission.executor["_attempt_key"] = ctx._phase_attempt_id
    ctx.mission.executor["_attempt_number"] = ctx._attempt_count_before + 1
    # a mission re-opened by a failed verify step must start from a fresh worktree (dispatcher_v3…
    _mrec = ctx.state.get("missions", {}).get(ctx.mission.id) or {}
    ctx.mission.executor["_reopened"] = bool(_mrec.get("reopened_by_verify"))
    # 2026-09-18: a rebuild ordered by a multi-AI review BLOCK carries the reviewer's verdict file (written…
    ctx.mission.executor["_review_block_file"] = str(_mrec.get("review_block_file") or "")
    _mrec["executor_type"] = str((ctx.mission.executor or {}).get("type") or "")
    if _mrec.get("reopened_by_verify"):
        # one fresh worktree per re-open, not one per attempt (each attempt was archiving again and again on…
        _mrec.pop("reopened_by_verify", None)
        _mrec["reopened_consumed_at"] = rt.time.strftime("%Y-%m-%dT%H:%M:%SZ", rt.time.gmtime())
    ctx.mission.executor["_contract_id"] = ctx.contract["contract_id"]
    ctx.mission.executor["_milestone"] = ctx.mid
    # ── stable dispatch checkpoint (crash-safe builder result) ───────────
    # One live builder result per (mission, contract), persisted the moment
    # runner.execute() returns and consumed once the attempt reaches a
    # durable outcome. A crash between the builder result and the
    # MISSION_<status> recording used to lose the result — the restart
    # re-dispatched and REBUILT it, spending a second model budget for work
    # already produced. Resume is only offered while the state's
    # attempt_count has not moved, i.e. while the checkpointed attempt
    # never reached a durable outcome. Launch journals stay PER ATTEMPT: a
    # resumed dispatch still opens/closes its own accounting row under its
    # own _phase_attempt_id and consumes no new model quota — the builder
    # does not run again.
    from ..dispatch_checkpoint import (
        CheckpointWriteError as _CheckpointWriteError,
        consume_dispatch_checkpoint as _consume_ckpt,
        execution_result_from_checkpoint as _ckpt_result,
        journal_event as _ckpt_journal,
        load_dispatch_checkpoint as _load_ckpt,
        write_dispatch_checkpoint as _write_ckpt,
    )
    ctx._resume_ckpt = None
    try:
        ctx._resume_ckpt = _load_ckpt(
            ctx.project_dir, mission_id=ctx.mission.id,
            contract_id=ctx.contract["contract_id"],
            expected_attempt_count_before=ctx._attempt_count_before)
    except Exception as _ckexc:
        rt._event(ctx.state, "DISPATCH_CHECKPOINT_LOAD_FAILED", mission=ctx.mission.id,
               error=str(_ckexc)[:200])
    ctx._ckpt_resumed = ctx._resume_ckpt is not None

    def _checkpoint_consumed() -> None:
        # The attempt reached a durable typed outcome (recorded state, settlement continuation, or a typed…
        try:
            _consume_ckpt(ctx.project_dir, mission_id=ctx.mission.id,
                          contract_id=ctx.contract["contract_id"],
                          attempt_key=ctx._phase_attempt_id)
        except Exception as _ckc:
            rt._event(ctx.state, "DISPATCH_CHECKPOINT_CONSUME_FAILED",
                   mission=ctx.mission.id, error=str(_ckc)[:200])
    # Le script de réécriture ne peut pas transformer un site de DÉFINITION de fonction imbriquée en attribut…
    ctx._checkpoint_consumed = _checkpoint_consumed
    if rt._RUNRECORDS_AVAILABLE and rt._record_prediction is not None:
        try:
            rt._record_prediction(ctx._phase_attempt_id, {
                "project": ctx.authority.identity.project,
                "outcome": "PASS",
                "engine": ctx.preferred_engine or "local",
                "executor_type": ctx.mission.executor.get("type", ""),
                "spec_level": ctx.mission.executor.get("spec_level", ""),
                "contract_id": ctx.contract["contract_id"],
                "milestone": ctx.mid,
                "predicted_duration_s": None,
                "predicted_cost_usd": None,
                "basis": "no-validated-history",
            })
        except Exception:
            pass  # RunRecords failure must never block dispatch
    # ── §F3 production launch gate ───────────────────────────────────────
    # This is the one line in the system that spends money. Before e708c14d
    # the accounting modules existed but nothing called them, so consumption
    # could run indefinitely ahead of validated progress. The gate is
    # fail-closed and sits HERE, not in a harness.
    from ..launch_gate import (LaunchBlocked, check_before_launch,
                              open_account, close_account)
    # Preflight reads the REAL frozen-contract schema (validation-contract/v2): the definition of "complete"…
    ctx._body = _budget_body(ctx.contract)
    try:
        ctx._launch_preflight = check_before_launch(
            ctx.project_dir, mission_id=ctx.mission.id, milestone=ctx.mid,
            contract_frozen=bool(ctx.contract.get("frozen_at")
                                 and ctx.contract.get("contract_signature")),
            expected_result_defined=bool(ctx._body.get("acceptance")
                                         or ctx._body.get("required_artifacts")),
            independent_proof_defined=bool(ctx._body.get("product_e2e")
                                           or ctx._body.get("acceptance")),
            # F4: observed, not assumed — the compiled action, the milestone's spent launches, and whether a live…
            mission=ctx.mission, contract_body=ctx._body,
            own_lease_generation=ctx._lease.generation,
            supervisor_proved_no_model=(
                bool(ctx._satisfied_sha) or rt._trusted_local_command_uses_no_model(ctx.mission)
                or ctx._ckpt_resumed),
            verifier_preflight_result=ctx._verifier_preflight_result,
        )
    except LaunchBlocked as _blocked:
        # Deduplicate: an unchanged blocked condition (same reason, same detail) must not write a fresh event and…
        from ..blocked_event_dedup import should_emit as _should_emit_blocked
        if _should_emit_blocked(ctx.state, ctx.mission.id, _blocked.reason, _blocked.detail):
            rt._event(ctx.state, "LAUNCH_BLOCKED", mission=ctx.mission.id,
                   reason=_blocked.reason, detail=str(_blocked.detail)[:400])
            rt.save_project_state(ctx.project_dir, ctx.state)
        # ECO-01 residual: the PREDICTION for this attempt was already written (unconditionally, above) BEFORE…
        if rt._RUNRECORDS_AVAILABLE and rt._record_observation is not None:
            try:
                rt._record_observation(ctx._phase_attempt_id, {
                    "outcome": "BLOCKED", "duration_s": 0.0,
                    "engine": ctx.preferred_engine or "local", "rc": None,
                    "tokens": None, "result_sha": "",
                    "authority_hashes": ctx.authority.identity.as_dict(),
                    "failure_fingerprint": str(_blocked.detail)[:200],
                    "close_reason": "LAUNCH_BLOCKED"})
            except Exception:
                pass  # RunRecords failure must never block dispatch
        return rt.CycleResult(
            "LAUNCH_BLOCKED", ctx.mid,
            f"engine launch refused for {ctx.mission.id}: {_blocked.reason} — "
            f"{_blocked.detail.get('detail', '')}")

    # F2: the OPEN row is durable (fsync) BEFORE execve.
    ctx._account = open_account(
        ctx.project_dir, attempt_key=ctx._phase_attempt_id,
        project=ctx.state.get("project", ""), milestone=ctx.mid, mission=ctx.mission.id,
        attempt=ctx._attempt_count_before + 1,
        engine=ctx.preferred_engine or "", model="",
        spec_level=ctx.mission.executor.get("spec_level", ""),
        task_kind=("local" if ctx.mission.executor.get("type") == "local_command"
                   else "build"),
        consumes_model_quota=(bool(
            ctx._launch_preflight.get("consumes_model_quota", True))
            and not ctx._ckpt_resumed))
    ctx.t_start = rt.time.monotonic()

    return None


def _ss5_launch_and_capture(ctx):
    """Lancement reel (ou reprise depuis checkpoint), capture du resultat, settlement (receipt, quarantaine…"""
    from ..launch_gate import close_account
    from ..dispatch_checkpoint import (
        CheckpointWriteError as _CheckpointWriteError,
        execution_result_from_checkpoint as _ckpt_result,
        journal_event as _ckpt_journal,
        write_dispatch_checkpoint as _write_ckpt,
    )

    try:
        if ctx._ckpt_resumed:
            # Crash resume: rebuild the recorded result; the builder does NOT run again.
            ctx.result = _ckpt_result(ctx.mission.id, ctx._resume_ckpt)
            rt._event(ctx.state, "DISPATCH_RESUMED_FROM_CHECKPOINT",
                   mission=ctx.mission.id,
                   checkpoint_key=ctx._resume_ckpt.get("checkpoint_key", ""),
                   original_attempt_key=ctx._resume_ckpt.get("attempt_key", ""))
            _ckpt_journal(ctx.project_dir, "RESUME", mission=ctx.mission.id,
                          attempt_key=ctx._phase_attempt_id,
                          resumed_from=ctx._resume_ckpt.get("attempt_key", ""))
        else:
            # Reliable model-call telemetry through the common gate: a durable OPEN telemetry event BEFORE the launch…
            from ..project_authority import release_project_lock_for_launch as _release_lock
            rt.save_project_state(ctx.project_dir, ctx.state)
            with _release_lock(ctx.project_dir) as _lock_released_for_launch:
                if ctx._launch_preflight.get("consumes_model_quota", True):
                    from ..telemetry.gate import metered_launch as _metered_launch
                    with _metered_launch(
                            ctx.project_dir, attempt_key=ctx._phase_attempt_id,
                            mission=ctx.mission.id, milestone=ctx.mid,
                            engine=ctx.preferred_engine or "") as ctx._meter:
                        ctx.result = ctx.runner.execute(ctx.mission)
                        ctx._meter.update(
                            outcome="SUCCESS" if ctx.result.ok else "FAILURE",
                            rc=ctx.result.rc,
                            engine_execution=getattr(
                                ctx.result, "engine_execution", None) or {})
                else:
                    ctx.result = ctx.runner.execute(ctx.mission)
            if _lock_released_for_launch:
                _fresh = rt.load_project_state(ctx.project_dir)
                # keep this cycle's in-memory objects but adopt everything the other threads saved meanwhile (their…
                ctx.state.clear(); ctx.state.update(_fresh)
            # Durable BEFORE any bookkeeping: a crash in the receipt/state recording below must not erase the builder…
            try:
                _write_ckpt(
                    ctx.project_dir, mission_id=ctx.mission.id,
                    contract_id=ctx.contract["contract_id"],
                    attempt_key=ctx._phase_attempt_id, attempt_id=ctx._attempt_id,
                    attempt_number=ctx._attempt_count_before + 1,
                    lease_generation=ctx._lease.generation,
                    attempt_count_before=ctx._attempt_count_before,
                    result=ctx.result)
            except _CheckpointWriteError as _ckw:
                # Typed and visible, never dispatch-fatal: the result exists and flows on; what is lost is only the…
                rt._event(ctx.state, "DISPATCH_CHECKPOINT_WRITE_FAILED",
                       mission=ctx.mission.id, error=str(_ckw)[:300])
                rt.save_project_state(ctx.project_dir, ctx.state)
    except BaseException:
        close_account(ctx.project_dir, ctx._account, exit_class="KILL",
                      mission_pass=False,
                      duration_s=rt.time.monotonic() - ctx.t_start)
        raise
    ctx.wall_time = rt.time.monotonic() - ctx.t_start
    ctx.mission_status = "PASS" if ctx.result.ok else "FAIL"

    # F4: Receipt is mandatory before MISSION_PASS — write + fsync + read-back verify.
    if ctx.result.ok:
        from ..mission_receipt import (MissionExecutionReceipt, write_mission_receipt,
                                      ReceiptWriteError)
        from ..safety_quarantine import enter_quarantine as _enter_quar
        # F4: actual_engine_id must come from an OBSERVED process execution.
        ctx._engine_exec = getattr(ctx.result, "engine_execution", None) or {}
        ctx._actual_engine = ctx._engine_exec.get("engine_id", "") if ctx._engine_exec.get("observed") else ""
        if not ctx._actual_engine:
            ctx._reason = (f"ENGINE_IDENTITY_UNOBSERVED for {ctx.mission.id}: executor returned "
                       f"no process execution observation (engine hint="
                       f"{ctx.result.engine!r}) — refusing MISSION_PASS")
            rt._event(ctx.state, "ENGINE_IDENTITY_UNOBSERVED", mission=ctx.mission.id, reason=ctx._reason[:300])
            if rt._RUNRECORDS_AVAILABLE and rt._record_observation is not None:
                try:
                    rt._record_observation(ctx._phase_attempt_id, {
                        "outcome": "ERROR", "duration_s": round(ctx.wall_time, 2),
                        "engine": ctx.result.engine or "local", "rc": ctx.result.rc,
                        "tokens": None, "result_sha": "",
                        "authority_hashes": ctx.authority.identity.as_dict(),
                        "failure_fingerprint": ctx._reason[-200:],
                        "close_reason": "ENGINE_IDENTITY_UNOBSERVED"})
                except Exception:
                    pass
            _enter_quar(ctx.project_dir, cause="ENGINE_IDENTITY_UNOBSERVED",
                        mission_id=ctx.mission.id, attempt_id=ctx._attempt_id)
            # L1_advance-007: the execution DID finish — close the account with its typed cause (like…
            close_account(ctx.project_dir, ctx._account, exit_class="PROVIDER_FAILURE",
                          mission_pass=False, duration_s=ctx.wall_time,
                          engine_execution=getattr(ctx.result, "engine_execution", None),
                          result=ctx.result)
            rt.save_project_state(ctx.project_dir, ctx.state)
            # Typed terminal outcome: the checkpointed result was adjudicated (unusable as-is) — drop it so a later…
            ctx._checkpoint_consumed()
            return rt.CycleResult("ENGINE_IDENTITY_UNOBSERVED", ctx.mid, ctx._reason)
        ctx._mr = MissionExecutionReceipt(
            mission_id=ctx.mission.id,
            attempt_id=ctx._attempt_id,
            candidate_sha=ctx.result.candidate_sha or "",
            integration_sha=ctx.result.integration_sha or "",
            actual_engine_id=ctx._actual_engine,
            contract_id=ctx.contract.get("contract_id", ""),
            contract_signature=ctx.contract.get("contract_signature", ""),
            command_exit_code=ctx.result.rc,
        )
        try:
            write_mission_receipt(ctx.project_dir, ctx._mr)
        except ReceiptWriteError as _rwe:
            rt._event(ctx.state, "MISSION_RECEIPT_WRITE_FAILED", mission=ctx.mission.id,
                   reason=str(_rwe)[:300])
            if rt._RUNRECORDS_AVAILABLE and rt._record_observation is not None:
                try:
                    rt._record_observation(ctx._phase_attempt_id, {
                        "outcome": "ERROR", "duration_s": round(ctx.wall_time, 2),
                        "engine": ctx.result.engine or "local", "rc": ctx.result.rc,
                        "tokens": None, "result_sha": "",
                        "authority_hashes": ctx.authority.identity.as_dict(),
                        "failure_fingerprint": str(_rwe)[-200:],
                        "close_reason": "MISSION_RECEIPT_WRITE_FAILED"})
                except Exception:
                    pass
            _enter_quar(ctx.project_dir, cause="MISSION_RECEIPT_WRITE_FAILED",
                        mission_id=ctx.mission.id)
            close_account(ctx.project_dir, ctx._account, exit_class="PROVIDER_FAILURE",
                          mission_pass=False, duration_s=ctx.wall_time,
                          engine_execution=getattr(ctx.result, "engine_execution", None),
                          result=ctx.result)
            rt.save_project_state(ctx.project_dir, ctx.state)
            # Typed terminal outcome — the result was adjudicated and the attempt entered quarantine; replaying it…
            ctx._checkpoint_consumed()
            return rt.CycleResult("MISSION_RECEIPT_WRITE_FAILED", ctx.mid, str(_rwe))

    # §F3: the attempt is accounted HERE, past the receipt. `settled` means a durable receipt carrying an…
    ctx._refused_before_engine = rt._dispatcher_refused_before_engine(ctx.result)
    # RESILIENCE (owner 2026-09-13): a quota/network cut observed on the engine's own output is closed as…
    ctx._interruption = rt._dispatcher_interruption(ctx.result)
    _cut_class = {"QUOTA_CUT": "QUOTA", "NETWORK_CUT": "NETWORK", "KILL": "KILL"}.get(ctx._interruption, "")
    if ctx._interruption:
        rt._event(ctx.state, "ATTEMPT_INTERRUPTED", milestone=str(ctx.mid),
                  mission=ctx.mission.id, interruption=ctx._interruption,
                  exit_class=_cut_class)
    # FACTORY_LEDGER_ENGINE_KNOWN_V1: on a refusal evidenced before any engine launched, hand close_account…
    ctx._close_engine_execution = getattr(ctx.result, "engine_execution", None)
    if ctx._refused_before_engine:
        _refusal = ""
        _receipt = getattr(ctx.result, "receipt", None)
        if isinstance(_receipt, dict):
            _refusal = str(_receipt.get("verdict") or "")
        ctx._close_engine_execution = {**(ctx._close_engine_execution or {}),
                                       "refusal": (_refusal or "PREFLIGHT_REFUSED")}
    close_account(ctx.project_dir, ctx._account,
                  exit_class=("PREFLIGHT_REFUSED" if ctx._refused_before_engine
                              else (_cut_class or ("SUCCESS" if ctx.result.ok else "SEMANTIC_FAILURE"))),
                  mission_pass=bool(ctx.result.ok), duration_s=ctx.wall_time,
                  candidate_sha=getattr(ctx.result, "integration_sha", "") or "",
                  engine_execution=ctx._close_engine_execution,
                  result=ctx.result,
                  consumes_model_quota=(False if ctx._refused_before_engine else None))

    ctx.prev_rec = ctx.state["missions"].get(ctx.mission.id, {})
    ctx.failure_fp = rt._compute_failure_fingerprint(ctx.result)
    ctx.fp_history = list(ctx.prev_rec.get("failure_fingerprint_history") or [])
    if ctx._interruption:
        # An interruption is not a failure of this position: the fingerprint history is left exactly as it was…
        ctx.failure_fp = ""
    elif ctx.failure_fp:
        ctx.fp_history = (ctx.fp_history + [ctx.failure_fp])[-3:]
    else:
        ctx.fp_history = []
    # NB: le dict est construit dans runtime._build_mission_state_record — un test scanne runtime.py par…
    ctx.state["missions"][ctx.mission.id] = rt._build_mission_state_record(
        ctx.mission_status, ctx.result, ctx.contract, ctx.prev_rec,
        ctx.failure_fp, ctx.fp_history, ctx._attempt_id,
        ctx._phase_attempt_id, ctx._lease,
        executor_type=str((ctx.mission.executor or {}).get("type") or ""))
    # Codex counter-audit 2026-09-16 (BUG 2): a `kind: verify` step that FAILS is a verdict on the…
    if ctx.mission_status == "FAIL" and str(getattr(ctx.mission, "kind", "") or "") == "verify":
        for _prev in getattr(ctx, "missions", []) or []:
            if _prev.id == ctx.mission.id:
                break
            _prec = ctx.state["missions"].get(_prev.id) or {}
            if _prec.get("state") == "PASS" and str(getattr(_prev, "kind", "") or "") != "verify":
                _prec["state"] = "FAIL"
                _prec["reopened_by_verify"] = ctx.mission.id
                _prec["reopened_at"] = rt.time.strftime("%Y-%m-%dT%H:%M:%SZ", rt.time.gmtime())
                _prec.pop("last_attempt_id", None)
                rt._event(ctx.state, "IMPLEMENT_REOPENED_BY_VERIFY_FAILURE", milestone=ctx.mid,
                          mission=_prev.id, verify_mission=ctx.mission.id,
                          reason=(ctx.failure_fp or "")[:200])
    # G3: record this attempt in the escalation ladder (persisted → restart-safe).
    ctx.exec_engine = ctx.result.engine or "local"
    if not ctx.result.ok and ctx._interruption:
        # 2026-09-18 (TR_DEEP_RESEARCH_3AI_V1#M3 diagnose loop ×5): a typed interruption (QUOTA_CUT / NETWORK_CUT…
        rt._event(ctx.state, "ATTEMPT_NOT_ESCALATED_INTERRUPTED",
                  mission=ctx.mission.id, interruption=ctx._interruption)
        if ctx._interruption == "QUOTA_CUT":
            # 19/09 14:15 Bangkok: a mission pinned to ONE engine (strict hint, e.g. the Codex code review) was…
            try:
                from ..quota_observer import load_snapshot as _qls
                import os as _os
                _hint = str((ctx.mission.executor or {}).get("engine_hint") or "").lower()
                _strict = False
                try:
                    import owner_routing_policy as _orp
                    _strict = any(ctx.mid.startswith(x) for x in getattr(_orp, "PHASE5_STRICT_HINT_MILESTONES", ()))
                except Exception:
                    _strict = False
                _until = None
                if _strict and _hint:
                    _snap = _qls(rt.Path.home() / "Library" / "Application Support" / "JocelynFactory" / "run")
                    for _w in ((_snap.get("providers") or {}).get(_hint) or {}).get("windows", {}).values():
                        _ra = str(_w.get("resets_at") or "")
                        if _ra > rt.time.strftime("%Y-%m-%dT%H:%M:%SZ", rt.time.gmtime()) and (_until is None or _ra < _until):
                            _until = _ra
                    if _until is None:
                        _until = rt._iso(int(rt.time.time()) + 3600)
                    _mrec_q = ctx.state.setdefault("missions", {}).setdefault(ctx.mission.id, {})
                    _mrec_q["state"] = "MILESTONE_PARKED"
                    _mrec_q["parked_milestone"] = ctx.mid
                    _mrec_q["parked_until"] = _until
                    _mrec_q["failure_fingerprint_history"] = []
                    rt._event(ctx.state, "MISSION_PARKED_FOR_QUOTA", mission=ctx.mission.id,
                              engine=_hint, until=_until)
            except Exception:
                pass
    elif not ctx.result.ok:
        ctx._infra = rt.is_infrastructure_failure(ctx.result.rc, ctx.result.output_tail)
        if ctx._infra:
            ctx.state = rt.quarantine_engine(
                ctx.state, ctx.exec_engine,
                reason=f"infra failure rc={ctx.result.rc}: {ctx.result.output_tail[:120]}")
            # Separate infra cursor: if this engine has hit the rotation threshold, rotate to the next capable engine.
            if rt.infra_rotation_needed(ctx.state, ctx.exec_engine):
                ctx.spec_level = ctx.mission.executor.get("spec_level", "B")
                ctx.alt = rt.next_alternate_engine(ctx.exec_engine, spec_level=ctx.spec_level, state=ctx.state, project=ctx.authority.identity.project)
                if ctx.alt != ctx.exec_engine:
                    ctx.state["missions"][ctx.mission.id]["preferred_engine"] = ctx.alt
                    rt._event(ctx.state, "INFRA_ENGINE_ROTATION", mission=ctx.mission.id,
                           from_engine=ctx.exec_engine, to_engine=ctx.alt)
        else:
            ctx.state = rt.clear_infra_failure_count(ctx.state, ctx.exec_engine)
            ctx.state = rt._escalation_record_attempt(
                state=ctx.state,
                mission_id=ctx.mission.id,
                engine=ctx.exec_engine,
                strategy_sha=ctx.authority.identity.strategy_sha,
                failure_fingerprint=ctx.failure_fp,
                context_hash=ctx.contract.get("contract_id", ""),
                authority_hash=ctx.authority.identity.roadmap_sha,
            )
    elif ctx.result.ok:
        ctx.state = rt.clear_infra_failure_count(ctx.state, ctx.exec_engine)
    rt._event(ctx.state, "MISSION_" + ctx.mission_status, mission=ctx.mission.id, rc=ctx.result.rc)
    # RunRecords — every attempt (PASS and FAIL). record_attempt() always sets validated=False — only…
    if rt._RUNRECORDS_AVAILABLE and rt._record_attempt is not None:
        try:
            rt._record_attempt({
                "project": ctx.authority.identity.project,
                "milestone": ctx.mid,
                "mission": ctx.mission.id,
                "task_type": ctx.mission.executor.get("type", "unknown"),
                "engine": ctx.result.engine or "local",
                "strategy": ctx.authority.identity.strategy_sha,
                "authority_hashes": ctx.authority.identity.as_dict(),
                "context_hash": ctx.contract.get("contract_id", ""),
                "candidate_sha": ctx.result.candidate_sha or "",
                "attempts": 1,
                "failure_fingerprints": [] if ctx.result.ok else [ctx.result.output_tail[-200:]],
                "tests": [],
                "product_e2e": {},
                # Counter-audit 2026-09-11: this wrote validator_verdict=None on PASS and FAIL alike, so engine_stats…
                "validator_verdict": None,
                "attempt_outcome": ctx.mission_status,
                "attempt_rc": ctx.result.rc,
                "attempt_ok": bool(ctx.result.ok),
                # The exact attempt identity, shared with the two-phase PREDICTION/OBSERVATION pair.
                "attempt_id": ctx._phase_attempt_id,
                "wall_time_s": round(ctx.wall_time, 2),
                "human_interventions": 0,
                "validated": False,
            })
        except Exception:
            pass  # RunRecords failure must never block mission execution
    # G5 two-phase RunRecords — OBSERVATION closes the PREDICTION written before dispatch, on the same…
    if rt._RUNRECORDS_AVAILABLE and rt._record_observation is not None:
        try:
            rt._record_observation(ctx._phase_attempt_id, {
                "outcome": ctx.mission_status,
                "duration_s": round(ctx.wall_time, 2),
                "engine": ctx.result.engine or "local",
                "rc": ctx.result.rc,
                "tokens": None,
                "result_sha": (ctx.result.integration_sha or ctx.result.candidate_sha or ""),
                "authority_hashes": ctx.authority.identity.as_dict(),
                "failure_fingerprint": ctx.failure_fp or "",
                "close_reason": "ATTEMPT_COMPLETED",
            })
        except Exception:
            pass  # RunRecords failure must never block mission execution
    rt.save_project_state(ctx.project_dir, ctx.state)
    # The attempt's outcome is now durably recorded (state + receipt + accounting row) — the checkpoint has…
    ctx._checkpoint_consumed()
    if not ctx.result.ok:
        return rt.CycleResult("MISSION_FAILED", ctx.mid,
                           f"{ctx.mission.id} rc={ctx.result.rc}: {ctx.result.output_tail[-200:]}")


    return None


def process_one_mission(ctx):
    """Un tour de la boucle de dispatch pour `ctx.mission`."""
    for _stage in (_ss1_adopt_or_skip_pass, _ss2_escalation_and_recovery_gate,
                  _ss3_pre_dispatch_gates, _ss4_lease_and_checkpoint,
                  _ss5_launch_and_capture):
        _outcome = _stage(ctx)
        if _outcome is _CONTINUE or isinstance(_outcome, rt.CycleResult):
            return _outcome
    return None
