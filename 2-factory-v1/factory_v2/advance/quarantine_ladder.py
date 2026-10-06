"""Étape extraite de `_run_quarantine_cycle` (factory_v2/runtime.py) : la échelle de réparation (phase 3…"""
from __future__ import annotations

from .. import runtime as rt


def run_repair_ladder(project_dir, state, qrec, contract, qmid, cause, authority):
    """Phases 3/4 de `_run_quarantine_cycle`. Retourne toujours un `CycleResult`."""
    # Recovery work is matched by THIS contract's idempotency key, never by kind alone: an old DIAGNOSE from…
    key = contract.idempotency_key

    def _completed(kind: str) -> str:
        for w in rt._all_recovery_work(project_dir):
            carried_recipe_diagnosis = (
                kind == "DIAGNOSE"
                and w.get("carried_from_validation_gap") is True
                and bool(getattr(contract, "repair_command", ""))
            )
            if (w.get("kind") == kind and w.get("completed")
                    and (w.get("settlement_success") is True
                         or carried_recipe_diagnosis)
                    and w.get("idempotency_key") == key):
                return w.get("recovery_id", "")
        return ""

    def _exists(kind: str) -> bool:
        # Completed failures are history; only pending work occupies a step.
        return any(w.get("kind") == kind and not w.get("completed")
                   and w.get("idempotency_key") == key
                   for w in rt._all_recovery_work(project_dir))

    def _failed(kind: str) -> dict:
        """A failure produced by the CURRENT control-plane code."""
        from ..escalation_ladder import control_code_sha as _ccs3
        now_sha = _ccs3()
        for w in rt._all_recovery_work(project_dir):
            if not (w.get("kind") == kind and w.get("completed")
                    and w.get("idempotency_key") == key
                    and w.get("settlement_success") is False):
                continue
            rec_sha = str(w.get("control_code_sha") or "")
            if now_sha and rec_sha != now_sha:
                continue        # produced by other code — history, not this chain
            return w
        return {}

    # ── 3. DIAGNOSE, then the SAFE_REPAIR it implies. ───────────────────────────
    diagnosed = _completed("DIAGNOSE")
    if not diagnosed and not _exists("DIAGNOSE"):
        if rt._quarantine_recovery(project_dir, state, qrec, "DIAGNOSE", contract=contract):
            rt.save_project_state(project_dir, state)
            return rt.CycleResult("QUARANTINE_DIAGNOSE_SCHEDULED", qmid,
                                  f"SAFETY_QUARANTINE({cause}): DIAGNOSE scheduled")
    if diagnosed and not _exists("SAFE_REPAIR"):
        if rt._quarantine_recovery(project_dir, state, qrec, "SAFE_REPAIR",
                                   contract=contract, parent_recovery_id=diagnosed):
            rt.save_project_state(project_dir, state)
            return rt.CycleResult("QUARANTINE_SAFE_REPAIR_SCHEDULED", qmid,
                                  f"SAFETY_QUARANTINE({cause}): SAFE_REPAIR scheduled from "
                                  f"DIAGNOSE {diagnosed}")

    # A SAFE_REPAIR that ran and did not fix anything is a dead end unless the chain moves on.
    failed_repair = _failed("SAFE_REPAIR")
    if failed_repair:
        return rt._quarantine_new_fingerprint_edge(
            project_dir, state, qrec, contract, qmid,
            fingerprint=f"SAFE_REPAIR_FAILED:{failed_repair['recovery_id']}:"
                        f"{failed_repair.get('output_tail', '')[:200]}",
            event="QUARANTINE_SAFE_REPAIR_FAILED",
            detail=(f"SAFE_REPAIR {failed_repair['recovery_id']} completed without "
                    "repairing the frozen reproduction"))

    # ── 4. VALIDATE_REPAIR — the gate re-runs the frozen commands at exact SHAs. ─
    result, why = run_clear_gate(project_dir, state, qrec, contract, qmid, cause, authority)
    if result is None:
        rt._event(state, "SAFETY_CLEAR_REJECTED", cause=cause, reason=why[:300])
        rt.save_project_state(project_dir, state)
        return rt.CycleResult("QUARANTINE_REPAIR_PENDING", qmid,
                              f"SAFETY_QUARANTINE({cause}): repair not proven yet — {why}")
    return result


def run_clear_gate(project_dir, state, qrec, contract, qmid, cause, authority):
    """La porte VALIDATE_REPAIR (phase 4), partagée avec `probe_gate_first`."""
    from ..safety_quarantine import (clear_quarantine_with_receipt,
                                    produce_safety_clear_receipt,
                                    QuarantineClearFailed)
    receipt = produce_safety_clear_receipt(
        project_id=authority.identity.project,
        quarantine_entered_at=qrec.get("entered_at", ""),
        repair_sha=rt._git_head_sha(contract.command_cwd),
        contract=contract,
        authority_hashes=authority.identity.as_dict(),
        run_precheck=False,   # the gate runs the authoritative exact-SHA proofs
    )
    try:
        cleared = clear_quarantine_with_receipt(project_dir, receipt)
    except QuarantineClearFailed as exc:
        return None, str(exc)

    # NB: littéralement `_event(state, "SAFETY_QUARANTINE_CLEARED"` doit rester visible dans…
    rt._emit_safety_quarantine_cleared(
        state, cause=cause,
        repair_sha=cleared.get("repair_sha", "")[:12],
        pre_repair_sha=cleared.get("pre_repair_sha", "")[:12],
        resume_from=cleared.get("resume_from", ""))
    state = rt._reopen_interrupted_milestone(state, cleared.get("resume_from", ""), cause)
    rt.save_project_state(project_dir, state)
    return rt.CycleResult("SAFETY_QUARANTINE_CLEARED", qmid,
                          f"quarantine cleared by gate-executed proofs ({cause}) — "
                          f"resuming {cleared.get('resume_from', '') or 'next milestone'}"), ""


def probe_gate_first(project_dir, state, qrec, qmid, cause, authority):
    """La porte AVANT l'échelle, quand l'arbre produit a déjà dépassé le bug gelé."""
    from ..safety_quarantine import get_repair_contract
    if getattr(authority, "identity", None) is None:
        return None            # sans autorité chargée la porte ne peut rien signer (phase -1 tranche)
    contract = get_repair_contract(project_dir)
    if contract is None or not contract.verify_integrity():
        return None
    if contract.authority_hashes and contract.authority_hashes != authority.identity.as_dict():
        return None            # contrat gelé contre une autorité qui a dérivé : la phase 0b tranche
    head = rt._git_head_sha(contract.command_cwd)
    if not head or head == contract.pre_repair_sha:
        return None            # rien de nouveau à juger : l'échelle doit produire la réparation
    memo = state.setdefault("_quarantine_gate_probe", {})
    if memo.get(contract.contract_id) == head:
        return None
    memo[contract.contract_id] = head
    result, why = run_clear_gate(project_dir, state, qrec, contract, qmid, cause, authority)
    if result is not None:
        return result
    rt._event(state, "QUARANTINE_GATE_PROBE_REJECTED", cause=cause, head=head[:12],
              pre_repair_sha=contract.pre_repair_sha[:12], reason=why[:300])
    rt.save_project_state(project_dir, state)
    return None
