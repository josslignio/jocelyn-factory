"""Étape extraite de `_run_quarantine_cycle` (factory_v2/runtime.py) : accounting de blocage typé (phase…"""
from __future__ import annotations

from .. import runtime as rt


def handle_stall_and_drift(project_dir, state, qrec, cause, qmid, authority):
    """Phases -1/0/0b de `_run_quarantine_cycle`."""
    from ..safety_quarantine import (MAX_REPAIR_ITERATIONS, REPAIR_STATUS_EXTERNAL_BLOCK,
                                    REPAIR_STATUS_MAX_ITERATIONS,
                                    TERMINAL_REPAIR_STATUSES, get_repair_chain,
                                    get_repair_contract, update_repair_chain)

    _closed_lost = rt._quarantine_close_candidate_lost(
        project_dir, state, qrec, get_repair_contract(project_dir))
    if _closed_lost is not None:
        return _closed_lost

    # ── 0. Typed stall accounting. ──────────────────────────────────────────────
    # A repair chain whose only outcomes are "cleared" or "pending forever" is not a
    # finished state machine.  Every cycle is counted, and past a hard cap the chain
    # ends in a NAMED status that a human or a later phase can act on, instead of
    # emitting the same *_PENDING line indefinitely.
    chain = get_repair_chain(project_dir)
    status = chain.get("status") or "ACTIVE"
    if status == REPAIR_STATUS_EXTERNAL_BLOCK:
        # EXTERNAL_BLOCK records authority drift, not a code defect.
        _contract = get_repair_contract(project_dir)
        _current_authority = authority.identity.as_dict()
        if (_contract is not None and _contract.authority_hashes
                and _contract.authority_hashes == _current_authority):
            update_repair_chain(
                project_dir, status="ACTIVE", reason="",
                iterations=0, fingerprints=[])
            rt._event(state, "QUARANTINE_EXTERNAL_BLOCK_CLEARED", cause=cause,
                      contract_id=_contract.contract_id,
                      reason="repair contract authority refreshed")
            rt.save_project_state(project_dir, state)
            chain = get_repair_chain(project_dir)
            status = "ACTIVE"
    if status in TERMINAL_REPAIR_STATUSES:
        # E2 — a terminal belongs to its EPISODE, and an episode re-opens only on PROOF.
        from ..escalation_ladder import control_code_sha as _ccs
        from ..repair_epoch import (current_episode, epoch_id, new_episode,
                                   open_new_episode)
        _now_sha = _ccs()
        _cur_ep = current_episode(chain)
        _ep_sha = str(_cur_ep.get("control_code_sha")
                      or chain.get("terminal_code_sha") or "")
        _receipt, _rwhy = rt._supervisor_verified_fix(project_dir, qrec, chain,
                                                       _ep_sha, _now_sha)

        if _now_sha and _ep_sha != _now_sha and _receipt is not None:
            _new_ep = new_episode(
                epoch_id(control_code_sha=_now_sha,
                         context_manifest_sha=str(chain.get("context_manifest_sha") or ""),
                         contract_signature="",
                         failure_fingerprint=str(qrec.get("failure_fingerprint", ""))),
                cause=cause, code_sha=_now_sha,
                context_sha=str(chain.get("context_manifest_sha") or ""),
                contract_signature="",
                fingerprint=str(qrec.get("failure_fingerprint", "")))
            _chain2, _why = open_new_episode(chain, _new_ep, _receipt)
            if _why == "OPENED":
                update_repair_chain(project_dir, **{k: v for k, v in _chain2.items()
                                                    if k != "schema"})
                rt._emit_repair_episode_reopened_by_verified_fix(
                    state, cause=cause, previous_status=status,
                    previous_code_sha=_ep_sha[:16] or "UNSTAMPED",
                    current_code_sha=_now_sha[:16],
                    old_fingerprint=_receipt.old_fingerprint[:120])
                rt.save_project_state(project_dir, state)
                chain = get_repair_chain(project_dir)
                status = "ACTIVE"
            else:
                # C11: `_why` embeds a fresh receipt id every cycle even when the blocked SHA pair has not changed, so…
                from ..blocked_event_dedup import should_emit as _should_emit_reentry
                _reentry_key = f"quarantine_reentry_refused:{qrec.get('milestone_id', '')}"
                _reentry_detail = {"cause": cause, "old_sha": _ep_sha[:16],
                                   "new_sha": _now_sha[:16]}
                if _should_emit_reentry(state, _reentry_key,
                                       "REPAIR_EPISODE_REENTRY_REFUSED", _reentry_detail):
                    rt._event(state, "REPAIR_EPISODE_REENTRY_REFUSED", cause=cause,
                              reason=_why[:300])
                    rt._raise_reentry_maintenance_request(
                        project_dir, state, qrec, _ep_sha, _now_sha, _why[:300])
                    rt.save_project_state(project_dir, state)
                return rt.CycleResult(f"QUARANTINE_{status}", qmid,
                                      f"SAFETY_QUARANTINE({cause}): {_why}")
        else:
            if _now_sha and _ep_sha and _ep_sha != _now_sha:
                # The precise reason from the supervisor, never a generic line: the generic one hid…
                _reentry_why = (_rwhy or "NO_VERIFIED_FIX_RECEIPT")[:300]
                # The refusal is correct, but on its own it produced nothing anyone could act on: the live factory state…
                from ..blocked_event_dedup import should_emit as _should_emit_reentry2
                _reentry_key2 = f"quarantine_reentry_refused:{qrec.get('milestone_id', '')}"
                _reentry_detail2 = {"cause": cause, "old_sha": _ep_sha[:16],
                                    "new_sha": _now_sha[:16]}
                if _should_emit_reentry2(state, _reentry_key2,
                                        "REPAIR_EPISODE_REENTRY_REFUSED", _reentry_detail2):
                    rt._event(state, "REPAIR_EPISODE_REENTRY_REFUSED", cause=cause,
                              reason=_reentry_why)
                    # Raise a durable, idempotent request naming the missing proof, so a blocked position is a record rather…
                    rt._raise_reentry_maintenance_request(
                        project_dir, state, qrec, _ep_sha, _now_sha, _reentry_why)
                    rt.save_project_state(project_dir, state)
            return rt.CycleResult(f"QUARANTINE_{status}", qmid,
                                  f"SAFETY_QUARANTINE({cause}): repair chain ended in "
                                  f"{status} — {chain.get('reason', '')}")
    # An episode must exist before any counter moves, otherwise the projection has nothing to bind to and…
    if not (chain.get("current_episode") or {}):
        from ..escalation_ladder import control_code_sha as _ccs0
        from ..repair_epoch import epoch_id as _eid, new_episode as _newep
        _sha0 = _ccs0()
        _ep0 = _newep(_eid(control_code_sha=_sha0,
                           context_manifest_sha=str(chain.get("context_manifest_sha") or ""),
                           contract_signature="",
                           failure_fingerprint=str(qrec.get("failure_fingerprint", ""))),
                      cause=cause, code_sha=_sha0,
                      context_sha=str(chain.get("context_manifest_sha") or ""),
                      contract_signature="",
                      fingerprint=str(qrec.get("failure_fingerprint", "")))
        _ep0["iterations"] = int(chain.get("iterations", 0))
        _ep0["fingerprints"] = list(chain.get("fingerprints") or [])
        _ep0["status"] = status
        update_repair_chain(project_dir, current_episode=_ep0)
        chain = get_repair_chain(project_dir)

    iterations = int(chain.get("iterations", 0)) + 1
    if iterations > MAX_REPAIR_ITERATIONS:
        reason = (f"{MAX_REPAIR_ITERATIONS} repair cycles produced no verified fix for "
                  f"{cause!r}; last fingerprints={chain.get('fingerprints', [])[-3:]}")
        from ..escalation_ladder import control_code_sha as _ccs2
        _terminal_sha = _ccs2()
        # The repair chain's own terminal status is always updated — that record is the authoritative iteration…
        update_repair_chain(project_dir, status=REPAIR_STATUS_MAX_ITERATIONS,
                            iterations=iterations, reason=reason,
                            terminal_code_sha=_terminal_sha)
        # C11: this status is terminal, so TERMINAL_REPAIR_STATUSES above intercepts every later poll of the SAME…
        from ..blocked_event_dedup import should_emit as _should_emit_maxiter
        _maxiter_key = f"quarantine_max_iterations:{qrec.get('milestone_id', '')}"
        _maxiter_detail = {"cause": cause, "terminal_code_sha": _terminal_sha}
        if _should_emit_maxiter(state, _maxiter_key, "QUARANTINE_MAX_ITERATIONS",
                               _maxiter_detail):
            rt._event(state, "QUARANTINE_MAX_ITERATIONS", cause=cause, reason=reason[:300])
            rt.save_project_state(project_dir, state)
        return rt.CycleResult("QUARANTINE_MAX_ITERATIONS", qmid,
                              f"SAFETY_QUARANTINE({cause}): {reason}")
    update_repair_chain(project_dir, iterations=iterations)

    # ── 0b. A repair contract frozen against drifted authority may not dispatch. ──
    # S017-04: drift was only checked at the CLEAR gate, so the old repair chain kept
    # running work for a product definition the owner had already replaced.  Authority
    # drift has to stop the work BEFORE it is dispatched, not merely refuse to bless it
    # afterwards — otherwise the Factory spends real model calls repairing a target that
    # no longer exists.
    _existing = get_repair_contract(project_dir)
    if _existing is not None and _existing.authority_hashes:
        _current = authority.identity.as_dict()
        if _existing.authority_hashes != _current:
            _drift = {k: (_existing.authority_hashes.get(k), _current.get(k))
                      for k in set(_existing.authority_hashes) | set(_current)
                      if _existing.authority_hashes.get(k) != _current.get(k)}
            rt._event(state, "REPAIR_CONTRACT_STALE_NOT_DISPATCHED", cause=cause,
                      contract_id=_existing.contract_id, drift=str(_drift)[:300])
            update_repair_chain(project_dir, status=REPAIR_STATUS_EXTERNAL_BLOCK,
                                reason=("repair contract frozen against authority that "
                                        "has since drifted; a new contract must be "
                                        "derived from current authority"))
            rt.save_project_state(project_dir, state)
            return rt.CycleResult("QUARANTINE_EXTERNAL_BLOCK", qmid,
                                  f"SAFETY_QUARANTINE({cause}): repair contract "
                                  f"{_existing.contract_id} is frozen against drifted "
                                  f"authority {_drift} — refusing to dispatch superseded "
                                  "repair work")

    return chain, status
