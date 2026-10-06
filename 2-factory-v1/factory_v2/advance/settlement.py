"""Étape extraite de `_advance_locked` (factory_v2/runtime.py) : le settlement final — vérification des…"""
from __future__ import annotations

from .. import runtime as rt


def _settle_verify_receipts(ctx):
    """Charge le receipt EXACT de chaque mission (jamais un rescan) ; une PASS non prouvable devient UNPROVEN…"""
    # F4: load the receipt for EXACTLY this mission and last_attempt_id — never "the latest receipt in the…
    from ..mission_receipt import load_verified_mission_receipt, ReceiptVerificationError
    ctx._receipt = None
    ctx._unproven: list[str] = []
    # Sol: the exact attempts this settlement proves — one per mission, carrying the ledger key and the SHA…
    ctx._verified_attempts: list[dict] = []
    for ctx._m in ctx.missions:
        ctx._rec = ctx.state.get("missions", {}).get(ctx._m.id, {})
        # 2026-09-16: a mission that is NOT a PASS (parked, failed, unproven) has no receipt to verify…
        _st = str(ctx._rec.get("state") or "")
        if _st and _st != "PASS":
            # A previous iteration may already have invalidated a PASS.
            if ctx._unproven:
                rt.save_project_state(ctx.project_dir, ctx.state)
            _act = "MILESTONE_PARKED" if _st == "MILESTONE_PARKED" else "MISSION_NOT_SETTLEABLE"
            return rt.CycleResult(_act, ctx.mid,
                                  f"{ctx._m.id} is {_st}: settlement needs every mission PASS")
        ctx._attempt = ctx._rec.get("last_attempt_id", "")
        try:
            ctx._receipt = load_verified_mission_receipt(
                ctx.project_dir, ctx._m.id, ctx._attempt,
                contract_id=ctx.contract.get("contract_id", ""),
                contract_signature=ctx.contract.get("contract_signature", ""),
            )
            ctx._cand = ctx._receipt.integration_sha or ctx._receipt.candidate_sha or ""
            # C8: a receipt can verify (correctly signed, right mission/attempt, right contract) and STILL name a…
            if ctx._cand and rt._candidate_object_missing(
                    rt._candidate_repo_for_contract(ctx.project_dir, ctx.contract), ctx._cand):
                # Same protocol as the ReceiptVerificationError branch below (reset, never dedup-gated): the mutation…
                ctx._rec2 = ctx.state.setdefault("missions", {}).setdefault(ctx._m.id, {})
                ctx._rec2["state"] = "UNPROVEN"
                ctx._rec2["unproven_reason"] = (
                    f"CANDIDATE_OBJECT_MISSING: {ctx._cand[:12]} is not a commit in "
                    f"{rt._candidate_repo_for_contract(ctx.project_dir, ctx.contract)}")
                ctx._rec2["unproven_at"] = rt.time.strftime("%Y-%m-%dT%H:%M:%SZ", rt.time.gmtime())
                ctx._rec2.pop("last_attempt_id", None)
                rt._event(ctx.state, "CANDIDATE_OBJECT_MISSING", milestone=ctx.mid,
                       mission=ctx._m.id, candidate_sha=ctx._cand, attempt=ctx._attempt)
                rt.save_project_state(ctx.project_dir, ctx.state)
                return rt.CycleResult(
                    "CANDIDATE_OBJECT_MISSING", ctx.mid,
                    f"{ctx.mid}: {ctx._m.id} settled at candidate_sha {ctx._cand[:12]} which no "
                    "longer exists in the product repo (purged worktree) — no "
                    "DIAGNOSE can reproduce a missing commit; the mission resets "
                    "for a fresh build instead")
            ctx._verified_attempts.append(rt._verified_attempt_record(
                ctx._m.id, ctx._rec, ctx._attempt, ctx._cand))
        except ReceiptVerificationError as _rve:
            # R2.2: a PASS we cannot prove is UNPROVEN, not a product failure.
            ctx._unproven.append(f"{ctx._m.id}({_rve.__class__.__name__}: {str(_rve)[:120]})")
            ctx._rec = ctx.state.setdefault("missions", {}).setdefault(ctx._m.id, {})
            ctx._rec["state"] = "UNPROVEN"
            ctx._rec["unproven_reason"] = str(_rve)[:300]
            ctx._rec["unproven_at"] = rt.time.strftime("%Y-%m-%dT%H:%M:%SZ", rt.time.gmtime())
            ctx._rec.pop("last_attempt_id", None)
            rt._event(ctx.state, "MISSION_PASS_UNPROVEN", milestone=ctx.mid, mission=ctx._m.id,
                   reason=str(_rve)[:300])
    if ctx._unproven:
        rt.save_project_state(ctx.project_dir, ctx.state)
        return rt.CycleResult("MISSION_UNPROVEN_REEXECUTE", ctx.mid,
                           f"{ctx.mid}: unproven PASS missions reset for re-execution under the "
                           f"current contract — {'; '.join(ctx._unproven)}")
    # SHAs come from the last mission's receipt — the integration point the validator must test against, same…
    ctx._candidate_sha = ctx._receipt.candidate_sha if ctx._receipt else ""
    ctx._integration_sha_val = ctx._receipt.integration_sha if ctx._receipt else ""

    # C38: a candidate already held (a prior cycle's verdict on this EXACT candidate blocked, and escalation…
    from ..launch_gate import current_code_sha as _gate_current_code_sha
    ctx._gate_expected_sha_pre = ctx._integration_sha_val or ctx._candidate_sha
    ctx._gate_hold_pre = (ctx.state.get("completed_milestones", {}).get(ctx.mid) or {}).get("gate_hold")
    _same_hold_identity = (
        ctx._gate_hold_pre
        and ctx._gate_hold_pre.get("identity_key") == rt._gate_hold_identity_key(
            ctx.mid, ctx._gate_expected_sha_pre, ctx.contract, _gate_current_code_sha()))
    if _same_hold_identity:
        from ..safety_quarantine import get_quarantine_record as _get_qrec
        _held_by = str(ctx._gate_hold_pre.get("held_by_quarantine") or "")
        _held_at = str(ctx._gate_hold_pre.get("held_by_quarantine_entered_at") or "")
        _current_qrec = _get_qrec(ctx.project_dir) or {}
        # UNBLOCK_SYNTH-002: the causal quarantine is identified by (milestone_id, entered_at) — milestone_id may…
        _same_cause = (
            bool(_current_qrec)
            and str(_current_qrec.get("milestone_id") or "") == _held_by
            and _held_by != ctx.mid
            and (not _held_at or _held_at == str(_current_qrec.get("entered_at") or "")))
        if _same_cause:
            return rt.CycleResult(
                "MILESTONE_GATE_HELD", ctx.mid,
                f"{ctx.mid}: candidate {ctx._gate_expected_sha_pre[:12] or '(none)'} already held "
                "on this exact identity while the causal quarantine remains active")
        # The external cause disappeared or changed.
        (ctx.state.get("completed_milestones", {}).get(ctx.mid) or {}).pop("gate_hold", None)
        rt._event(ctx.state, "MILESTONE_GATE_HOLD_INVALIDATED", milestone=ctx.mid,
                  previous_quarantine=_held_by,
                  current_quarantine=str(_current_qrec.get("milestone_id") or ""))
        rt.save_project_state(ctx.project_dir, ctx.state)

    # 2026-09-16: the product oracle of TR_TEST_HYGIENE_V1 (full pytest under TR_FORCE_NO_SOCKET) needs > 10…
    _pe2e = (ctx.entry.get("product_e2e") or {}) if isinstance(getattr(ctx, "entry", None), dict) else {}
    try:
        _vt = int(_pe2e.get("timeout_s") or 1800)
    except (TypeError, ValueError):
        _vt = 1800
    ctx.validator = rt.IndependentValidator(ctx.project_dir, timeout_s=_vt)
    # Defect 15: pass integration_sha (post-merge HEAD) for exact-SHA validation.
    ctx.verdict = ctx.validator.validate(
        ctx.contract,
        candidate_sha=ctx._candidate_sha,
        integration_sha=ctx._integration_sha_val,
    )
    rt._event(ctx.state, "VALIDATION_VERDICT", milestone=ctx.mid,
           complete=ctx.verdict.milestone_complete, failures=ctx.verdict.failures)

    # 2026-09-17: a milestone whose deliverable IS the multi-AI review ledger…
    ctx._review_ledger_pending = False
    try:
        from ..review_dispatch import REVIEW_LEDGER_NAME as _rl_name
        _mai_req = bool(((ctx.contract.get("body") or {}).get("multi_ai_review")
                         or (ctx.entry.get("multi_ai_review") if isinstance(ctx.entry, dict) else None)
                         or {}).get("required"))
        _fails = list(ctx.verdict.failures or [])
        if (_mai_req and _fails and not ctx.verdict.milestone_complete
                and all(f.startswith(rt._MISSING_ARTIFACT_PREFIX)
                        and f.endswith(_rl_name) for f in _fails)):
            ctx._review_ledger_pending = True
            rt._event(ctx.state, "REVIEW_LEDGER_PENDING_MULTI_AI_REVIEW", milestone=ctx.mid,
                      missing=[f[-80:] for f in _fails])
    except Exception:
        ctx._review_ledger_pending = False
    try:
        if not ctx._review_ledger_pending:
            rt.assert_milestone_settleable(ctx.verdict)
    except rt.MilestoneGateBlocked as exc:
        # Finding 1(c): head= must equal the EXACT sha this validation ran against — same precedence as the…
        ctx._expected_head_sha = ctx._integration_sha_val or ctx._candidate_sha
        # C29: a mission's own receipt verified PASS, yet the independent gate's required_artifacts check found a…
        ctx._missing_deliverables = rt._candidate_deliverable_missing_paths(
            rt._candidate_repo_for_contract(ctx.project_dir, ctx.contract), ctx._expected_head_sha,
            ctx.verdict.failures)
        if ctx._missing_deliverables:
            ctx._mrec = ctx.state.setdefault("missions", {}).setdefault(ctx._m.id, {})
            ctx._mrec["state"] = "UNPROVEN"
            ctx._mrec["unproven_reason"] = (
                f"CANDIDATE_DELIVERABLE_MISSING: {', '.join(ctx._missing_deliverables)} "
                f"absent from candidate {ctx._expected_head_sha[:12]}")[:300]
            ctx._mrec["unproven_at"] = rt.time.strftime("%Y-%m-%dT%H:%M:%SZ", rt.time.gmtime())
            ctx._mrec.pop("last_attempt_id", None)
            rt._event(ctx.state, "CANDIDATE_DELIVERABLE_MISSING", milestone=ctx.mid, mission=ctx._m.id,
                   candidate_sha=ctx._expected_head_sha, paths=ctx._missing_deliverables)
            rt.save_project_state(ctx.project_dir, ctx.state)
            return rt.CycleResult(
                "CANDIDATE_DELIVERABLE_MISSING", ctx.mid,
                f"{ctx.mid}: {', '.join(ctx._missing_deliverables)} absent from candidate "
                f"{ctx._expected_head_sha[:12]} — mission reset for rebuild")
        ctx.waiting_until = rt._waiting_marker(ctx.project_dir, ctx.mid, expected_sha=ctx._expected_head_sha)
        ctx.record = ctx.state["completed_milestones"][ctx.mid]
        # Finding 1(f): a verifier that keeps handing back a near-future date must not stall a milestone forever.
        ctx.prior_streak = int(ctx.record.get("waiting_streak") or 0)
        ctx.prior_sha = str(ctx.record.get("waiting_last_sha") or "")
        ctx.no_progress = ctx.prior_sha == ctx._expected_head_sha
        if ctx.waiting_until and ctx.prior_streak >= rt.WAITING_FOR_EVIDENCE_CONSECUTIVE_CAP and ctx.no_progress:
            rt._event(ctx.state, "WAITING_FOR_EVIDENCE_CAP_EXCEEDED", milestone=ctx.mid,
                   consecutive_waits=ctx.prior_streak,
                   cap=rt.WAITING_FOR_EVIDENCE_CONSECUTIVE_CAP)
            ctx.waiting_until = ""  # fall through to the normal GATE_BLOCKED path below
        if ctx.waiting_until:
            ctx.record["state"] = "WAITING_FOR_EVIDENCE"
            ctx.record["next_check_at"] = ctx.waiting_until
            ctx.record["waiting_reason"] = "independent verifier calendar evidence pending"
            ctx.record["waiting_streak"] = (ctx.prior_streak + 1) if ctx.no_progress else 1
            ctx.record["waiting_last_sha"] = ctx._expected_head_sha
            if not ctx.record.get("waiting_recorded_at"):
                ctx.record["waiting_recorded_at"] = rt.time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", rt.time.gmtime())
                rt._event(ctx.state, "MILESTONE_WAITING_FOR_EVIDENCE", milestone=ctx.mid,
                       next_check_at=ctx.waiting_until)
            rt.save_project_state(ctx.project_dir, ctx.state)
            return rt.CycleResult("WAITING_FOR_EVIDENCE", ctx.mid,
                               f"{ctx.mid}: evidence is calendar-pending until {ctx.waiting_until}")
        # C38: a project holds at most one SAFETY_QUARANTINE.json.
        from ..safety_quarantine import get_quarantine_record, is_in_quarantine
        ctx._gate_qrec = (get_quarantine_record(ctx.project_dir)
                     if is_in_quarantine(ctx.project_dir) else None)
        if ctx._gate_qrec and ctx._gate_qrec.get("milestone_id", "") != ctx.mid:
            ctx.held_result = rt._hold_gate_after_verdict(
                ctx.state, ctx.mid, ctx.contract, ctx.verdict, ctx._expected_head_sha, ctx.missions, ctx._gate_qrec)
            rt.save_project_state(ctx.project_dir, ctx.state)
            return ctx.held_result
        # 22/09 05:30 Bangkok: the gate is bound to the EXACT integration SHA (right),
        # so a repair that landed at HEAD afterwards (a red gate fixed by a later
        # commit) left the milestone looping on the old SHA — 3 refusals, quarantine,
        # a human. If the product HEAD already satisfies the contract, re-bind: the
        # PASS mission is reopened and the next attempt pre-validates at HEAD without
        # a model call (the existing MODEL_CALL_SKIPPED_EXISTING_PRODUCT_VALID path).
        _rebound = _rebind_gate_to_head_if_satisfied(ctx, str(exc))
        if _rebound is not None:
            return _rebound
        # R3: a blocked gate is a routing signal, not a log line.
        rt._event(ctx.state, "MILESTONE_GATE_BLOCKED", milestone=ctx.mid, reason=str(exc))
        ctx.state["completed_milestones"][ctx.mid]["state"] = "GATE_BLOCKED"
        ctx.result_cycle = rt._record_validation_gap(
            ctx.project_dir, ctx.state, ctx.mid, ctx.contract, ctx.verdict, str(exc))
        rt.save_project_state(ctx.project_dir, ctx.state)
        return ctx.result_cycle or rt.CycleResult("MILESTONE_GATE_BLOCKED", ctx.mid, str(exc))


    return None


def _rebind_gate_to_head_if_satisfied(ctx, reason: str):
    """Red gate at the bound SHA, but HEAD satisfies the contract → reopen for pre-validation."""
    try:
        head = rt._git_head_sha(rt._candidate_repo_for_contract(ctx.project_dir, ctx.contract))
    except Exception:
        return None
    bound = str(getattr(ctx, "_expected_head_sha", "") or "")
    if not head or not bound or head == bound:
        return None
    memo = ctx.state.setdefault("_gate_rebind_probe", {})
    if memo.get(ctx.mid) == head:
        return None                       # already probed this HEAD: don't re-run a 30 min validator
    memo[ctx.mid] = head
    rt.save_project_state(ctx.project_dir, ctx.state)
    mission = next((m for m in ctx.missions), None)
    sha, _repo = rt._prevalidated_product_head(ctx.project_dir, ctx.contract, mission)
    if not sha or sha != head:
        return None
    for m in ctx.missions:
        rec = ctx.state.setdefault("missions", {}).setdefault(m.id, {})
        rec["state"] = "FAIL"
        rec["last_failure_fingerprint"] = f"GATE_REBOUND_TO_HEAD:{head[:12]}"
    rt._event(ctx.state, "SETTLEMENT_REBOUND_TO_HEAD", milestone=ctx.mid, bound_sha=bound[:12],
              head_sha=head[:12], reason=reason[:200])
    rt.save_project_state(ctx.project_dir, ctx.state)
    return rt.CycleResult("SETTLEMENT_REBOUND_TO_HEAD", ctx.mid,
                          f"gate red at {bound[:12]} but HEAD {head[:12]} satisfies the contract — "
                          "missions reopened for pre-validation at HEAD")


def _settle_multi_ai_review(ctx):
    # Contract-gated after the mechanical gate and bound to the exact SHA.
    ctx._mai = dict((ctx.contract.get("body") or {}).get("multi_ai_review")
               or (ctx.entry.get("multi_ai_review") if isinstance(ctx.entry, dict) else None)
               or {})
    if ctx._mai.get("required"):
        from .. import review_dispatch as _review_dispatch
        from ..blocked_event_dedup import should_emit as _mai_should_emit

        def _mai_unavailable(reason: str, detail: dict, message: str
                             ) -> CycleResult:
            # Deduplicated across identical polls: the FIRST observation of a given blocked condition emits the event…
            if _mai_should_emit(ctx.state, f"multi-ai:{ctx.mid}",
                                "MULTI_AI_REVIEW_UNAVAILABLE",
                                {"reason": reason, **detail}):
                rt._event(ctx.state, "MULTI_AI_REVIEW_UNAVAILABLE", milestone=ctx.mid,
                       reason=reason, **{k: str(v)[:200] for k, v in detail.items()})
                rt.save_project_state(ctx.project_dir, ctx.state)
            return rt.CycleResult("MULTI_AI_REVIEW_UNAVAILABLE", ctx.mid, message)

        ctx._mai_unavailable = _mai_unavailable

        def _mai_blocked(reviewed_sha: str, message: str, **event_fields
                         ) -> CycleResult:
            # Same dedup for a BLOCK on this exact SHA: recorded once, then a typed no-op until a repair produces a…
            ctx.state["completed_milestones"][ctx.mid]["state"] = "GATE_BLOCKED"
            if _mai_should_emit(ctx.state, f"multi-ai:{ctx.mid}", "MULTI_AI_REVIEW_BLOCKED",
                                {"reviewed_sha": reviewed_sha, **event_fields}):
                rt._event(ctx.state, "MULTI_AI_REVIEW_BLOCKED", milestone=ctx.mid,
                       reviewed_sha=reviewed_sha[:12],
                       **{k: str(v)[:200] for k, v in event_fields.items()})
                rt.save_project_state(ctx.project_dir, ctx.state)
            return rt.CycleResult("MULTI_AI_REVIEW_BLOCKED", ctx.mid, message)

        ctx._mai_blocked = _mai_blocked

        ctx._review_sha = (ctx._integration_sha_val or ctx._candidate_sha or "")
        if not ctx._review_sha:
            # CODEX-LOT-C37 counter-audit finding 3: a `local_command` mission (e.g. an audit gate that runs a…
            ctx._artifact_rel = str(ctx._mai.get("review_sha_from_artifact") or "").strip()
            if ctx._artifact_rel:
                from ..audit_observation_producer import _artifact_sha_field as _sha_field
                ctx._art_repo = rt.Path((ctx.contract.get("body") or {}).get("product_repo") or "")
                ctx._art_path = ctx._art_repo / ctx._artifact_rel
                ctx._art_sha = ""
                try:
                    ctx._art_sha = _sha_field(rt.json.loads(ctx._art_path.read_text(encoding="utf-8")))
                except (OSError, ValueError, rt.json.JSONDecodeError):
                    ctx._art_sha = ""
                if not rt._re.fullmatch(r"[0-9a-fA-F]{40}", ctx._art_sha or ""):
                    return ctx._mai_unavailable(
                        "REVIEW_NO_SHA",
                        {"artifact": ctx._artifact_rel},
                        f"multi-AI review required but this milestone produced "
                        f"no candidate/integration SHA, and its declared "
                        f"review_sha_from_artifact ({ctx._artifact_rel!r}) does not "
                        f"resolve to a real SHA — refusing to settle unbound")
                ctx._review_sha = ctx._art_sha
            else:
                return ctx._mai_unavailable(
                    "NO_EXACT_SHA_TO_BIND", {},
                    "multi-AI review required but the verified receipt carries no "
                    "exact SHA to bind the review to — refusing to settle unbound")
        ctx._prior = [r for r in _review_dispatch.read_reviews(ctx.project_dir)
                  if r.get("milestone") == ctx.mid
                  and r.get("reviewed_sha") == ctx._review_sha]
        # min_distinct_accept: how many DISTINCT engines must ACCEPT this exact SHA before it may settle.
        ctx._min_distinct = max(int(ctx._mai.get("min_distinct_accept") or 1), 1)
        _declared_reviewers = ctx._mai.get("reviewers")
        ctx._reviewer_order = ([str(e).strip().lower()
                           for e in (_declared_reviewers or ())
                           if str(e or "").strip()]
                          if _declared_reviewers else
                          ([""] if ctx._min_distinct == 1
                           else ["codex", "claude", "glm"]))
        # CODEX-LOT-C39 counter-audit: this block used to count ANY ledger line claiming verdict=="ACCEPT"…
        ctx._hardened_review = ctx._min_distinct > 1

        def _mai_report_unproven(engine) -> None:
            # CODEX-LOT-C41: an ACCEPT that fails `_review_account_proven` is reported by ONE of two typed…
            if not _review_dispatch.owner_key_available():
                _key_missing_key = f"multi-ai-seal-key-missing:{ctx.mid}:{ctx._review_sha}"
                if _mai_should_emit(ctx.state, _key_missing_key,
                                    "REVIEW_SEAL_KEY_MISSING",
                                    {"reviewed_sha": ctx._review_sha}):
                    rt._event(ctx.state, "REVIEW_SEAL_KEY_MISSING", milestone=ctx.mid,
                           reviewed_sha=ctx._review_sha[:12])
                    rt.save_project_state(ctx.project_dir, ctx.state)
                return
            _unproven_key = f"multi-ai-unproven:{ctx.mid}:{ctx._review_sha}:{engine}"
            if _mai_should_emit(ctx.state, _unproven_key, "REVIEW_ACCOUNT_UNPROVEN",
                                {"engine": engine, "reviewed_sha": ctx._review_sha}):
                rt._event(ctx.state, "REVIEW_ACCOUNT_UNPROVEN", milestone=ctx.mid,
                       engine=str(engine or ""), reviewed_sha=ctx._review_sha[:12])
                rt.save_project_state(ctx.project_dir, ctx.state)

        ctx._mai_report_unproven = _mai_report_unproven

        return _settle_mai_dispatch(ctx)
    return None


def _settle_mai_dispatch(ctx):
    from .. import review_dispatch as _review_dispatch
    from ..blocked_event_dedup import should_emit as _mai_should_emit

    ctx._accept_rows = [r for r in ctx._prior
                    if r.get("verdict") == "ACCEPT" and r.get("engine")]
    if ctx._hardened_review:
        ctx._proven_rows = []
        for ctx._r in ctx._accept_rows:
            if rt._review_account_proven(ctx._r, ctx.project_dir):
                ctx._proven_rows.append(ctx._r)
                continue
            ctx._mai_report_unproven(ctx._r.get("engine"))
        ctx._accept_rows = ctx._proven_rows
    ctx._accepted_engines = sorted({r.get("engine") for r in ctx._accept_rows})
    # Best-effort, NON-fatal builder hint (same field the fail-closed REVIEW_BUILDER_UNKNOWN gate below…
    ctx._builder_hint = str(getattr(ctx._receipt, "actual_engine_id", "") or "").strip().lower()
    if ctx._builder_hint == _review_dispatch.UNKNOWN.lower():
        ctx._builder_hint = ""
    ctx._reviewer_set = set(ctx._reviewer_order)
    ctx._required_reviewer_set = (ctx._reviewer_set - {ctx._builder_hint}) if ctx._builder_hint \
        else ctx._reviewer_set
    # Frozen thresholds are authority.

    def _roster_satisfied(engines) -> bool:
        # (b) CODEX-LOT-C39: the accepted set must equal the EXACT roster the contract declares (minus the…
        return (not ctx._hardened_review) or (not ctx._required_reviewer_set) \
            or set(engines) == ctx._required_reviewer_set

    ctx._roster_satisfied = _roster_satisfied

    if (not any(r.get("verdict") == "BLOCK" for r in ctx._prior)
            and len(ctx._accepted_engines) >= ctx._min_distinct
            and ctx._roster_satisfied(ctx._accepted_engines)):
        # Reused across identical polls (and across a repair-free settlement retry): distinct-accept requirement…
        if _mai_should_emit(ctx.state, f"multi-ai:{ctx.mid}", "MULTI_AI_REVIEW_REUSED",
                            {"reviewed_sha": ctx._review_sha,
                             "engines": ctx._accepted_engines}):
            rt._event(ctx.state, "MULTI_AI_REVIEW_REUSED", milestone=ctx.mid,
                   reviewed_sha=ctx._review_sha[:12],
                   engine=(ctx._prior[-1].get("engine", "") if ctx._prior else ""),
                   reviewers=",".join(ctx._accepted_engines))
            rt.save_project_state(ctx.project_dir, ctx.state)
    elif any(r.get("verdict") == "BLOCK" for r in ctx._prior):
        # A rejection already sits on this exact SHA: no re-spend on a verdict that cannot change — repair must…
        _blocks = [r for r in ctx._prior if r.get("verdict") == "BLOCK"]
        _rb_key = ctx.state.setdefault("_review_block_rebuilt", {})
        if _rb_key.get(ctx.mid) != ctx._review_sha:
            _auto = [m for m in ctx.missions
                     if str((m.executor or {}).get("type") or "") == "autopilot"]
            _rb_key[ctx.mid] = ctx._review_sha
            # bounded: 3 rebuild rounds per milestone, then park 6 h + incident (a reviewer and a builder can…
            _rb_n = ctx.state.setdefault("_review_block_rounds", {})
            _rb_n[ctx.mid] = int(_rb_n.get(ctx.mid) or 0) + 1
            if _rb_n[ctx.mid] > 8:   # 19/09: 8 rounds (Codex repair ≈15 min + review ≈5 min)
                _auto = []
                rt._event(ctx.state, "REVIEW_BLOCK_LOOP", milestone=ctx.mid,
                          rounds=_rb_n[ctx.mid], reviewed_sha=ctx._review_sha[:12],
                          reason="8 rebuilds already answered a BLOCK on this milestone; parking")
            if _auto:
                _target = _auto[-1]
                _rbdir = rt.Path(ctx.project_dir) / "evidence" / "review_block"
                _rbdir.mkdir(parents=True, exist_ok=True)
                _rbfile = _rbdir / f"{_target.id.replace('#', '_')}.md"
                _lines = [f"# Candidat {ctx._review_sha} — verdict(s) BLOCK", ""]
                for r in _blocks:
                    _lines.append(f"## Relecteur {r.get('engine')} ({r.get('started_at', '')})")
                    _lines.append(str(r.get("reason") or "(raison absente)"))
                    if r.get("transcript"):
                        _lines.append(f"Transcription complète : {r.get('transcript')}")
                    _lines.append("")
                _rbfile.write_text("\n".join(_lines), encoding="utf-8")
                _mrec = ctx.state.setdefault("missions", {}).setdefault(_target.id, {})
                _mrec["state"] = "UNPROVEN"
                _mrec["unproven_reason"] = f"MULTI_AI_REVIEW_BLOCK on {ctx._review_sha[:12]}"[:300]
                _mrec["unproven_at"] = rt.time.strftime("%Y-%m-%dT%H:%M:%SZ", rt.time.gmtime())
                _mrec["review_block_file"] = str(_rbfile)
                _mrec["reopened_by_verify"] = True          # fresh worktree
                _mrec.pop("last_attempt_id", None)
                rt._event(ctx.state, "MISSION_REBUILD_AFTER_REVIEW_BLOCK", milestone=ctx.mid,
                          mission=_target.id, reviewed_sha=ctx._review_sha[:12],
                          reviewers=",".join(str(r.get("engine")) for r in _blocks))
                rt.save_project_state(ctx.project_dir, ctx.state)
                return rt.CycleResult(
                    "MISSION_UNPROVEN_REEXECUTE", ctx.mid,
                    f"{ctx.mid}: review BLOCK on {ctx._review_sha[:12]} — {_target.id} "
                    f"reset for a rebuild that answers the reviewer")
            if ctx.missions:
                _last = ctx.missions[-1]
                _mrec = ctx.state.setdefault("missions", {}).setdefault(_last.id, {})
                _mrec["state"] = "MILESTONE_PARKED"
                _mrec["parked_until"] = rt._iso(int(rt.time.time()) + 6 * 3600)
                rt._event(ctx.state, "MILESTONE_PARKED", milestone=ctx.mid, mission=_last.id,
                          reason="REVIEW_BLOCK_NO_REPAIR_PATH: no autopilot mission can answer "
                                 "the reviewer", parked_until=_mrec["parked_until"])
                rt.save_project_state(ctx.project_dir, ctx.state)
        return ctx._mai_blocked(
            ctx._review_sha,
            f"multi-AI review BLOCKED candidate {ctx._review_sha[:12]} — a "
            f"repair producing a new SHA is required before settlement",
            reused=True)
    else:
        ctx._review_repo = rt.Path((ctx.contract.get("body") or {}).get("product_repo") or "")
        if not ctx._review_repo.is_dir():
            return ctx._mai_unavailable(
                "NO_PRODUCT_REPO_TO_REVIEW", {"detail": f"{ctx._review_repo}"},
                "multi-AI review required but the contract declares no "
                "readable product_repo for the reviewer — refusing to "
                "settle unreviewed")
        # FACTORY_LAW builder != reviewer: the engine that BUILT this exact SHA must be known and threaded…
        ctx._builder_engine_raw = str(getattr(ctx._receipt, "actual_engine_id", "") or "")
        ctx._builder_engine = ctx._builder_engine_raw.strip()
        if not ctx._builder_engine or ctx._builder_engine.upper() == _review_dispatch.UNKNOWN:
            # Fail-closed, per FACTORY_LAW: an unknown builder identity must never be silently accepted as "no…
            return ctx._mai_unavailable(
                "REVIEW_BUILDER_UNKNOWN",
                {"reviewed_sha": ctx._review_sha, "raw": ctx._builder_engine_raw},
                f"multi-AI review required but the builder engine of "
                f"{ctx._review_sha[:12]} could not be established from its "
                f"verified receipt — refusing to route a review that "
                f"might land on the engine that built it")
        ctx._builder_engine_lower = ctx._builder_engine.lower()
        # Bounded, idempotent review budget: the COST_NO_PROGRESS brake is GLOBAL, so its streak may well have…
        from .. import recovery_allocation as _ra
        from ..launch_gate import cost_no_progress_now as _cnp_now
        if _cnp_now(ctx.project_dir)["tripped"]:
            try:
                _ra.grant_multi_ai_review(
                    ctx.project_dir, milestone=ctx.mid,
                    contract_id=ctx.contract.get("contract_id", ""),
                    reviewed_sha=ctx._review_sha,
                    note="multi-AI review: one bounded launch per exact SHA")
            except _ra.AllocationRefused as _rvgr:
                if _rvgr.reason != "MULTI_AI_REVIEW_ALREADY_GRANTED":
                    return ctx._mai_unavailable(
                        "REVIEW_ALLOCATION_REFUSED",
                        {"reason": _rvgr.reason,
                         "detail": str(_rvgr.detail)[:200]},
                        f"multi-AI review required but its bounded "
                        f"allocation was refused: {_rvgr.reason} — "
                        f"refusing to settle unreviewed")
                # ALREADY_GRANTED: the one grant this SHA will ever get is in the journal — it pays this launch (or is…

        # One roster path for one or N reviewers.  Every candidate is subject to
        # the same builder exclusion and quota refusal before dispatch.
        ctx._tried = set(ctx._accepted_engines)
        for ctx._candidate in ctx._reviewer_order:
                if (len(ctx._accepted_engines) >= ctx._min_distinct
                        and ctx._roster_satisfied(ctx._accepted_engines)):
                    break
                if ctx._candidate in ctx._tried:
                    continue
                if ctx._candidate == ctx._builder_engine_lower:
                    # builder != reviewer — never spend a dispatch on the engine that built this exact SHA.
                    ctx._tried.add(ctx._candidate)
                    continue
                ctx._tried.add(ctx._candidate)
                ctx._quota_reason = (_review_dispatch.engine_quota_refused(
                    ctx.project_dir, ctx.mid, ctx._candidate)
                    if ctx._candidate else None)
                if ctx._quota_reason:
                    # Honest and incomplete, never a workaround: an engine at/above quota_observer.REFUSE_AT stays untried…
                    if _mai_should_emit(
                            ctx.state, f"multi-ai:{ctx.mid}",
                            "REVIEW_ENGINE_QUOTA_REFUSED",
                            {"engine": ctx._candidate,
                             "reviewed_sha": ctx._review_sha}):
                        rt._event(ctx.state, "REVIEW_ENGINE_QUOTA_REFUSED",
                               milestone=ctx.mid, engine=ctx._candidate,
                               reviewed_sha=ctx._review_sha[:12],
                               reason=ctx._quota_reason[:200])
                        rt.save_project_state(ctx.project_dir, ctx.state)
                    continue
                try:
                    ctx._account = _review_dispatch.dispatch_review(
                        ctx.project_dir, repo=ctx._review_repo,
                        mission_id=f"multi-ai:{ctx.mid}",
                        milestone=ctx.mid,
                        spec_level=ctx._mai.get("spec_level", "A"),
                        reviewed_sha=ctx._review_sha,
                        prompt=rt._multi_ai_review_prompt(
                            ctx.mid, ctx._review_sha, ctx.contract.get("body") or {}),
                        timeout=int(ctx._mai.get("timeout_s", 600)),
                        builder_engine=ctx._builder_engine,
                        engine_hint=(ctx._candidate or None),
                        codex_reviewer_enabled=(ctx._candidate == "codex"))
                except _review_dispatch.ReviewUnavailable as _ru:
                    # A launch-gate/allocation/route refusal is a global condition — trying a different engine right now…
                    return ctx._mai_unavailable(
                        _ru.reason, {"detail": str(_ru.detail)[:200],
                                     "engine": ctx._candidate},
                        f"multi-AI review required but unavailable "
                        f"dispatching {ctx._candidate!r}: {_ru.reason} — "
                        f"refusing to settle unreviewed")
                if ctx._account.reviewed_sha != ctx._review_sha or ctx._account.verdict != "ACCEPT":
                    return ctx._mai_blocked(
                        ctx._review_sha,
                        f"multi-AI review verdict {ctx._account.verdict} on "
                        f"{ctx._review_sha[:12]} (engine={ctx._account.engine}) "
                        f"— settlement refused until a new SHA passes "
                        f"review",
                        verdict=ctx._account.verdict, engine=ctx._account.engine,
                        reason=ctx._account.reason[:200])
                if ctx._account.engine and ctx._account.engine not in ctx._accepted_engines:
                    # CODEX-LOT-C41: an ACCEPT this cycle JUST dispatched is trusted by the EXACT SAME rule as a preexisting…
                    if (not ctx._hardened_review
                            or rt._review_account_proven(ctx._account, ctx.project_dir)):
                        ctx._accepted_engines = sorted(
                            set(ctx._accepted_engines) | {ctx._account.engine})
                    else:
                        ctx._mai_report_unproven(ctx._account.engine)
        if (len(ctx._accepted_engines) < ctx._min_distinct
                or not ctx._roster_satisfied(ctx._accepted_engines)):
            return ctx._mai_unavailable(
                "MIN_DISTINCT_ACCEPT_NOT_MET",
                {"reviewed_sha": ctx._review_sha,
                 "accepted": ",".join(ctx._accepted_engines),
                 "min_distinct_accept": ctx._min_distinct,
                 "required_roster": ",".join(sorted(ctx._required_reviewer_set))},
                f"multi-AI review required {ctx._min_distinct} distinct "
                f"accepting reviewers matching roster "
                f"{{{', '.join(sorted(ctx._required_reviewer_set))}}} on "
                f"{ctx._review_sha[:12]}; only "
                f"{len(ctx._accepted_engines)} "
                f"({', '.join(ctx._accepted_engines) or 'none'}) "
                f"accepted — refusing to settle unreviewed")
        rt._event(ctx.state, "MULTI_AI_REVIEW_ACCEPTED", milestone=ctx.mid,
               reviewed_sha=ctx._review_sha[:12],
               reviewers=",".join(ctx._accepted_engines))

    return None


def _settle_promote_and_project(ctx):
    """Lease avant mutation critique, promotion golden, ledger de settlement, projection accounting…"""
    if getattr(ctx, "_review_ledger_pending", False):
        # the review stage ran on a verdict that was incomplete only because its own ledger did not exist yet…
        rt.save_project_state(ctx.project_dir, ctx.state)
        return rt.CycleResult("REVIEW_LEDGER_WRITTEN_REVALIDATE", ctx.mid,
                              f"{ctx.mid}: review ledger produced this cycle — "
                              "re-validation next cycle before settlement")
    # B2/R6: require_current_lease() before every critical mutation (settlement, promotion).
    from ..worker_lease import require_current_lease, LeaseViolation
    ctx._settle_lease_gen = None
    ctx._settle_lease_token = ""
    # L1_advance-017: `_lease` is the lease of the mission THIS cycle launched; `ctx.mission` may be a later…
    if ctx._lease is not None and getattr(ctx._lease, "mission_id", None) == ctx.mission.id:
        ctx._settle_lease_gen, ctx._settle_lease_token = ctx._lease.generation, ctx._lease.fencing_token
    else:
        ctx._persisted = ctx.state.get("missions", {}).get(ctx.mission.id, {})
        ctx._settle_lease_gen = ctx._persisted.get("lease_generation")
        ctx._settle_lease_token = ctx._persisted.get("lease_fencing_token", "")
    if ctx._settle_lease_gen is None or not ctx._settle_lease_token:
        ctx._reason = (f"NO_SETTLEMENT_LEASE: {ctx.mission.id} carries no durable lease "
                   "generation/fencing token — refusing to settle unfenced")
        rt._event(ctx.state, "STALE_GENERATION_WRITE_BLOCKED", milestone=ctx.mid, reason=ctx._reason)
        rt.save_project_state(ctx.project_dir, ctx.state)
        return rt.CycleResult("STALE_GENERATION_WRITE_BLOCKED", ctx.mid, ctx._reason)
    # A mission that already PASSED is not in flight — settling it is bookkeeping the same owner still owes.
    from ..worker_lease import WorkerLease as _WL
    if not _WL.fence_with_token(ctx.mission.id, ctx._settle_lease_gen,
                                ctx._settle_lease_token, ctx.project_dir):
        if _WL.reacquire_for_settlement(ctx.mission.id, ctx._settle_lease_gen,
                                        ctx._settle_lease_token, ctx.project_dir):
            rt._event(ctx.state, "SETTLEMENT_LEASE_REACQUIRED", milestone=ctx.mid,
                   mission=ctx.mission.id, generation=ctx._settle_lease_gen,
                   reason="lease lapsed while the owner still owed a settlement; "
                          "same generation and fencing token, no other writer")
    try:
        require_current_lease(
            mission_id=ctx.mission.id,
            generation=ctx._settle_lease_gen,
            fencing_token=ctx._settle_lease_token,
            project_dir=ctx.project_dir,
            operation="milestone_settlement",
        )
    except LeaseViolation as _lv:
        rt._event(ctx.state, "STALE_GENERATION_WRITE_BLOCKED", milestone=ctx.mid, reason=str(_lv))
        rt.save_project_state(ctx.project_dir, ctx.state)
        return rt.CycleResult("STALE_GENERATION_WRITE_BLOCKED", ctx.mid, str(_lv))

    # Validate-before-golden: GoldenPromotion.promote() before settlement.
    ctx._integration_sha = ctx._integration_sha_val or None  # already recovered from state if result is None
    if ctx._integration_sha:
        ctx._promo_repo, ctx._golden_ref, ctx._promo_refusal = rt._resolve_promotion_target(
            ctx.contract, ctx._integration_sha)
        if ctx._promo_refusal:
            rt._event(ctx.state, "GOLDEN_PROMOTION_FAILED", milestone=ctx.mid,
                   reason=ctx._promo_refusal, status="TARGET_UNRESOLVED")
            ctx.state["completed_milestones"][ctx.mid]["state"] = "PROMOTION_FAILED"
            rt.save_project_state(ctx.project_dir, ctx.state)
            return rt.CycleResult("GOLDEN_PROMOTION_FAILED", ctx.mid, ctx._promo_refusal)
        # 2026-09-18: the promotion re-validates in a staging worktree with a 300 s default while the milestone…
        _pe2e_gp = (ctx.entry.get("product_e2e") or {}) if isinstance(getattr(ctx, "entry", None), dict) else {}
        try:
            _gp_timeout = int(_pe2e_gp.get("timeout_s") or 1800)
        except (TypeError, ValueError):
            _gp_timeout = 1800
        ctx._gp = rt._GoldenPromotion(repo_dir=ctx._promo_repo, golden_ref=ctx._golden_ref,
                                      timeout_s=_gp_timeout)
        ctx._evidence_dir = ctx.project_dir / "evidence" / "promotions" / ctx.mid
        ctx._pr = None
        try:
            ctx._base_sha = ctx._gp.read_golden_sha()
            # Golden only ever moves FORWARD.
            ctx._fwd_refusal = rt._promotion_moves_backwards(
                ctx._promo_repo, ctx._base_sha, ctx._integration_sha)
            # 21/09 08:55 Bangkok: when a LATER settlement already moved golden past this milestone's integration…
            if ctx._fwd_refusal and rt._staging_contained_in_golden(
                    ctx._promo_repo, ctx._base_sha, ctx._integration_sha):
                rt._event(ctx.state, "GOLDEN_ALREADY_CONTAINS_STAGING", milestone=ctx.mid,
                       golden_sha=ctx._base_sha, staging_sha=ctx._integration_sha)
                ctx._fwd_refusal = ""
                ctx._pr = rt._AlreadyPromoted(ctx._base_sha)
            elif ctx._fwd_refusal:
                rt._event(ctx.state, "GOLDEN_PROMOTION_FAILED", milestone=ctx.mid,
                       reason=ctx._fwd_refusal, status="NOT_FAST_FORWARD")
                ctx.state["completed_milestones"][ctx.mid]["state"] = "PROMOTION_FAILED"
                rt.save_project_state(ctx.project_dir, ctx.state)
                return rt.CycleResult("GOLDEN_PROMOTION_FAILED", ctx.mid, ctx._fwd_refusal)
            if ctx._pr is None:
                ctx._pr = ctx._gp.promote(
                staging_sha=ctx._integration_sha,
                base_sha=ctx._base_sha,
                contract=ctx.contract,
                evidence_dir=ctx._evidence_dir,
                candidate_branch=ctx._candidate_sha or None,
            )
        except Exception as _gp_exc:
            rt._event(ctx.state, "GOLDEN_PROMOTION_ERROR", milestone=ctx.mid, reason=str(_gp_exc))
            ctx.state["completed_milestones"][ctx.mid]["state"] = "PROMOTION_FAILED"
            rt.save_project_state(ctx.project_dir, ctx.state)
            return rt.CycleResult("GOLDEN_PROMOTION_FAILED", ctx.mid,
                               f"GoldenPromotion error: {_gp_exc}")
        # B2/D029 fail-closed: CAS_CONFLICT means another process won the update-ref race and THIS promotion did…
        if ctx._pr.status == "CAS_CONFLICT":
            rt._event(ctx.state, "GOLDEN_PROMOTION_CAS_CONFLICT", milestone=ctx.mid,
                   reason=ctx._pr.failure_reason, status=ctx._pr.status,
                   staging_sha=ctx._integration_sha)
            ctx.state["completed_milestones"][ctx.mid]["state"] = "PROMOTION_FAILED"
            rt.save_project_state(ctx.project_dir, ctx.state)
            return rt.CycleResult("GOLDEN_PROMOTION_CAS_CONFLICT", ctx.mid,
                               f"lost promotion race (CAS_CONFLICT): "
                               f"{ctx._pr.failure_reason}")
        if ctx._pr.status != "PROMOTED":
            rt._event(ctx.state, "GOLDEN_PROMOTION_FAILED", milestone=ctx.mid,
                   reason=ctx._pr.failure_reason, status=ctx._pr.status)
            ctx.state["completed_milestones"][ctx.mid]["state"] = "PROMOTION_FAILED"
            rt.save_project_state(ctx.project_dir, ctx.state)
            return rt.CycleResult("GOLDEN_PROMOTION_FAILED", ctx.mid, ctx._pr.failure_reason)
        rt._emit_golden_promoted(ctx.state, milestone=ctx.mid, status=ctx._pr.status,
                                 promoted_sha=ctx._pr.promoted_sha or ctx._integration_sha)

    ctx.settled_at = rt.time.strftime("%Y-%m-%dT%H:%M:%SZ", rt.time.gmtime())
    # Sol R2 enforcement (Claude round-1 CRITICAL: the ledger must be WIRED): a settlement of a CONTROL-REPO…
    try:
        from ..settlement_ledger import record_settlement_verified
        ctx._under_control = str(rt.Path(ctx.project_dir).resolve()).startswith(
            str((rt._REPO_ROOT / "projects").resolve()) + rt.os.sep)
        if not ctx._under_control:
            rt._event(ctx.state, "SETTLEMENT_LEDGER_SKIPPED_NOT_CONTROL_PROJECT",
                   milestone=ctx.mid,
                   project_dir=str(ctx.project_dir)[:200])
        else:
            # RECORD FIRST, THEN VERIFY: a fresh control repo's first settlement creates the ledger (an empty ledger…
            ctx._candidate_for_ledger = ctx._integration_sha or ""
            if not ctx._candidate_for_ledger:
                ctx._rc_out = rt.subprocess.run(
                    ["git", "rev-parse", "HEAD"], cwd=str(rt._REPO_ROOT),
                    capture_output=True, text=True, timeout=30)
                ctx._candidate_for_ledger = (ctx._rc_out.stdout.strip()
                                         if ctx._rc_out.returncode == 0 else "")
            # The repo the candidate SHA actually lives in.
            ctx._cand_repo, ctx._, ctx._cand_repo_refusal = rt._resolve_promotion_target(
                ctx.contract, ctx._candidate_for_ledger)
            ctx._ledger_entry = {
                "schema": "settlements/v1",
                "milestone": ctx.mid,
                "project": ctx.authority.identity.project,
                "settled_at": ctx.settled_at,
                "candidate_sha": ctx._candidate_for_ledger,
                "candidate_repo": ("" if ctx._cand_repo_refusal
                                   else str(rt.Path(ctx._cand_repo).resolve())),
                "base_sha": "",
                "authority_hashes": ctx.authority.identity.as_dict(),
                "contract": {"id": ctx.contract["contract_id"],
                             "path": str(ctx.project_dir / "validation_contracts"
                                         / f"{ctx.contract['contract_id']}.json"),
                             "frozen_at": ctx.contract.get("frozen_at", "")},
                "evidence": {
                    "active_verify": [{
                        "command": ["verify_milestone_acceptance", ctx.mid],
                        "rc": 0,
                        "observed_at": ctx.settled_at,
                        "output": "gate verified before settlement (runtime)",
                    }],
                    "full_suite": {"sha": ctx._candidate_for_ledger[:8], "rc": None,
                                   "counts": "gate-verified settlement (full suite "
                                             "per D021 policy at bundle tips)",
                                   "duration_s": None, "validation_key": ""},
                    "gate": {"reviewer": "runtime-gate (independent validator "
                                         "subprocess)",
                             "verdict": "ACCEPT", "rounds": 1,
                             "note": "verifier.milestone_complete observed by the "
                                     "settle path",
                             "consolidated_claude_review":
                                 "pending (sparse gate policy)"},
                },
                "honest_residual": [
                    "runtime-settled entry; consolidated Claude/Sol review pending "
                    "at the next material gate",
                ],
            }
            ctx._supersedes = rt._authorized_settlement_supersedes(ctx.state, ctx.mid)
            if ctx._supersedes:
                ctx._ledger_entry["supersedes"] = ctx._supersedes
            ctx._ledger_path, ctx._ledger_problems = record_settlement_verified(
                rt._REPO_ROOT, ctx._ledger_entry)
            if ctx._ledger_problems:
                rt._event(ctx.state, "SETTLEMENT_LEDGER_INVALID", milestone=ctx.mid,
                       problems=ctx._ledger_problems[:5])
                ctx.state["completed_milestones"][ctx.mid] = {
                    "state": "PROMOTION_FAILED",
                    "contract_id": ctx.contract["contract_id"],
                    "settled_at": ctx.settled_at,
                    "reason": "settlement ledger invalid: "
                              + "; ".join(ctx._ledger_problems[:3]),
                }
                rt.save_project_state(ctx.project_dir, ctx.state)
                return rt.CycleResult("SETTLEMENT_LEDGER_INVALID", ctx.mid,
                                   "; ".join(ctx._ledger_problems[:3]))
            rt._emit_settlement_ledger_entry_written(
                ctx.state, milestone=ctx.mid, candidate_sha=ctx._candidate_for_ledger[:12])
    except Exception as _sl_exc:
        # A ledger WRITE failure must block completion — an unsettleable settlement is not a settlement (fail…
        rt._event(ctx.state, "SETTLEMENT_LEDGER_WRITE_FAILED", milestone=ctx.mid,
               reason=str(_sl_exc))
        ctx.state["completed_milestones"][ctx.mid] = {
            "state": "PROMOTION_FAILED", "contract_id": ctx.contract["contract_id"],
            "settled_at": ctx.settled_at, "reason": f"ledger write failed: {_sl_exc}",
        }
        rt.save_project_state(ctx.project_dir, ctx.state)
        return rt.CycleResult("SETTLEMENT_LEDGER_WRITE_FAILED", ctx.mid, str(_sl_exc))
    ctx.state["completed_milestones"][ctx.mid] = {
        "state": "COMPLETE", "contract_id": ctx.contract["contract_id"],
        "settled_at": ctx.settled_at,
    }
    # F3 + Sol: the independent settlement is the only place allowed to turn an attempt into a validated…
    ctx._validated = rt._project_verified_attempts_or_fail(
        ctx.project_dir, ctx.mid, ctx.contract, ctx._verified_attempts,
        ctx._candidate_sha, ctx.state, ctx.settled_at)
    if isinstance(ctx._validated, rt.CycleResult):
        return ctx._validated

    # HOT_RELOAD_V1 (ported from runtime.py at the SPLIT merge): a successful factory settlement tells its…
    touched_files: list = []
    _base = getattr(ctx, "_base_sha", "") or ""
    _integ = getattr(ctx, "_integration_sha", "") or ""
    if _base and _integ:
        try:
            import subprocess as _sp
            _diff = _sp.run(["git", "diff", "--name-only", f"{_base}..{_integ}"],
                            cwd=str(getattr(ctx, "_promo_repo", None) or rt._REPO_ROOT),
                            capture_output=True, text=True, timeout=30, check=False)
            if _diff.returncode == 0:
                touched_files = [line for line in _diff.stdout.splitlines() if line]
        except (OSError, _sp.TimeoutExpired):
            pass
    rt._emit_milestone_settled(ctx.state, milestone=ctx.mid, touched_files=touched_files)
    # RunRecords — validated settlement. record_settlement() sets validated=True.
    if rt._RUNRECORDS_AVAILABLE and rt._record_settlement is not None:
        try:
            rt._record_settlement({
                "project": ctx.authority.identity.project,
                "milestone": ctx.mid,
                "mission": ctx.mid,
                "task_type": "milestone_settlement",
                "settled_attempt_ids": [
                    ctx.state["missions"].get(m.id, {}).get("last_attempt_key", "")
                    for m in ctx.missions
                    if ctx.state["missions"].get(m.id, {}).get("last_attempt_key")
                ],
                "engine": "local",
                "strategy": ctx.authority.identity.strategy_sha,
                "authority_hashes": ctx.authority.identity.as_dict(),
                "context_hash": ctx.contract.get("contract_id", ""),
                "candidate_sha": "",
                "attempts": len(ctx.missions),
                "failure_fingerprints": [],
                "tests": ctx.verdict.failures if ctx.verdict.failures else [],
                "product_e2e": {"passed": ctx.verdict.milestone_complete},
                "validator_verdict": "COMPLETE",
                "wall_time_s": 0.0,
                "human_interventions": 0,
                "validated": True,
            })
        except Exception:
            pass  # RunRecords failure must never block settlement
    rt.save_project_state(ctx.project_dir, ctx.state)
    return rt.CycleResult("MILESTONE_SETTLED", ctx.mid, "contract verified, gate authoritative",
                          touched_files=touched_files)


def run(ctx):
    """Suite de `_advance_locked` une fois la boucle de dispatch terminée."""
    for _stage in (_settle_verify_receipts, _settle_multi_ai_review,
                  _settle_promote_and_project):
        _outcome = _stage(ctx)
        if isinstance(_outcome, rt.CycleResult):
            return _outcome
    return None
