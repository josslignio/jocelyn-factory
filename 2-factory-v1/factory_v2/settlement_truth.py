"""One read-only verifier for "is this milestone actually settled?".

X3/P0-5: the long run used to derive truth from authored state.  A milestone counted as
progress because its dictionary entry said COMPLETE — no receipt was loaded, no contract
was verified and no acceptance was re-run, so a fabricated key counted exactly the same
as a milestone the Factory had genuinely proven.  Three call sites each had their own
partial idea of "settled", which is how they drifted apart.

There is now one function.  `_read_completed_milestones`, the long-run readiness
preflight and the final assertion all call it, so they cannot disagree.

A settlement is proven only when ALL of these hold:

  * the milestone's recorded state is COMPLETE and names a contract;
  * that frozen contract exists on disk;
  * every mission of the milestone carries a receipt loaded by its EXACT
    last_attempt_id, whose hash verifies and whose contract lineage matches;
  * no mission of the milestone is UNPROVEN;
  * the milestone is not the subject of an open quarantine;
  * current acceptance still passes, re-run now through IndependentValidator.

Read-only by construction: it loads, hashes and runs acceptance, and writes nothing.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SettlementVerdict:
    milestone: str
    proven: bool = False
    reasons: list = field(default_factory=list)
    contract_id: str = ""
    receipts_verified: int = 0
    # S1/S2: the verified receipt objects are RETAINED, not discarded.  They are the
    # only supervisor-controlled record of which attempt produced what, so both the
    # integration SHA that validation must pin to and the engines that may be counted
    # are derived from them — never from files found by scanning evidence/.
    receipts: list = field(default_factory=list)
    integration_sha: str = ""

    def __bool__(self) -> bool:
        return self.proven

    @property
    def engines(self) -> set:
        """Engines observed on the exact attempts behind this proven settlement."""
        if not self.proven:
            return set()
        return {r.actual_engine_id.strip().lower() for r in self.receipts
                if getattr(r, "actual_engine_id", "").strip()}


def _load_state(project_dir: Path) -> dict:
    try:
        return json.loads((Path(project_dir) / "PROJECT_STATE.json")
                          .read_text(encoding="utf-8"))
    except Exception:
        return {}


def quarantined_milestone(project_dir: Path) -> str:
    """Milestone named by an open quarantine, or "" when there is none."""
    try:
        rec = json.loads((Path(project_dir) / "SAFETY_QUARANTINE.json")
                         .read_text(encoding="utf-8"))
    except Exception:
        return ""
    return rec.get("milestone_id", "") or "*"   # "*" = quarantined, milestone unnamed


def verify_settlement(project_dir: Path, milestone_id: str,
                      state: dict | None = None,
                      run_acceptance: bool = True,
                      timeout_s: int = 600) -> SettlementVerdict:
    """Verify one milestone's settlement against durable evidence. Never writes."""
    project_dir = Path(project_dir)
    state = state if state is not None else _load_state(project_dir)
    verdict = SettlementVerdict(milestone=milestone_id)

    record = (state.get("completed_milestones", {}) or {}).get(milestone_id)
    if not isinstance(record, dict):
        verdict.reasons.append("NO_SETTLEMENT_RECORD")
        return verdict
    if record.get("state") != "COMPLETE":
        verdict.reasons.append(f"STATE_IS_{record.get('state', 'UNKNOWN')}")
        return verdict

    qm = quarantined_milestone(project_dir)
    if qm and qm in ("*", milestone_id):
        verdict.reasons.append("OPEN_QUARANTINE_CONTRADICTS_COMPLETE")
        return verdict

    contract_id = record.get("contract_id", "")
    verdict.contract_id = contract_id
    if not contract_id:
        verdict.reasons.append("SETTLEMENT_NAMES_NO_CONTRACT")
        return verdict
    contract_path = (project_dir / "validation_contracts" / f"{contract_id}.json")
    if not contract_path.is_file():
        verdict.reasons.append(f"CONTRACT_NOT_ON_DISK:{contract_id}")
        return verdict
    # Through the verifying loader, never a raw read: this function decides whether
    # a COMPLETE is real, so it must not accept a contract whose frozen semantics no
    # longer recompute. A tampered acceptance list or product_e2e command read raw
    # here would let a fabricated settlement audit as sound — the exact failure this
    # module exists to catch.
    from .validation_contract import ContractIntegrityError, load_frozen_contract
    try:
        contract = load_frozen_contract(project_dir, contract_id)
    except ContractIntegrityError as exc:
        verdict.reasons.append(f"CONTRACT_INTEGRITY_FAILED:{exc}")
        return verdict
    except Exception as exc:                                    # noqa: BLE001
        verdict.reasons.append(f"CONTRACT_UNREADABLE:{exc}")
        return verdict

    # Every mission of this milestone must present a verified receipt for its exact
    # attempt.  Zero missions is not proof of anything — a settlement with no work
    # behind it is precisely the fabricated COMPLETE this function exists to reject.
    from .mission_receipt import (ReceiptVerificationError,
                                  load_verified_mission_receipt)
    missions = {mid: rec for mid, rec in (state.get("missions", {}) or {}).items()
                if isinstance(rec, dict) and mid.startswith(f"{milestone_id}#")}
    if not missions:
        verdict.reasons.append("NO_MISSIONS_BEHIND_SETTLEMENT")
        return verdict

    signature = (contract.get("contract_signature")
                 or (contract.get("body", {}) or {}).get("contract_signature", ""))
    # GAT-01: last_attempt_key is the ledger's own join key for this exact attempt
    # (set alongside last_attempt_id at the same dispatch — see runtime.py's
    # "Sol: the accounting ledger's key for THIS attempt"). Retained per mission_id
    # so _control_pin can look up the attempt's attempt_accounts.jsonl row instead
    # of trusting a receipt field that does not carry the control commit.
    attempt_keys: dict = {}
    for mid, rec in sorted(missions.items()):
        if rec.get("state") == "UNPROVEN":
            verdict.reasons.append(f"UNPROVEN_MISSION:{mid}")
            continue
        if rec.get("state") != "PASS":
            verdict.reasons.append(f"MISSION_NOT_PASS:{mid}:{rec.get('state')}")
            continue
        try:
            receipt = load_verified_mission_receipt(
                project_dir, mid, rec.get("last_attempt_id", ""),
                contract_id=contract_id, contract_signature=signature)
        except ReceiptVerificationError as exc:
            verdict.reasons.append(f"RECEIPT_REJECTED:{mid}:{str(exc)[:120]}")
            continue
        verdict.receipts.append(receipt)      # S1/S2: retained, not discarded
        verdict.receipts_verified += 1
        attempt_keys[mid] = rec.get("last_attempt_key", "")
    if verdict.reasons:
        return verdict

    # S2: the receipts name the commits this settlement was produced at.  Validation
    # must run THERE, not wherever the working tree happens to be now — otherwise a
    # receipt naming a committed bad SHA is "proven" by a later good HEAD, which is the
    # substitution this whole verifier exists to prevent.
    repo = _product_repo(project_dir, contract)
    integration_sha, why = _final_integration_sha(repo, verdict.receipts)
    if why:
        verdict.reasons.append(why)
        return verdict
    verdict.integration_sha = integration_sha

    # S5 (dual pin): the receipts also name the CONTROL commit whose verifier code
    # accepted the settlement (candidate_sha).  Re-running acceptance with TODAY'S
    # verifier code against the pinned product state is the mirror image of the
    # HEAD-race S2 exists to prevent: a receipt accepted by the code of its day is
    # "disproven" by later, stricter code that the settlement never ran under.  The
    # verifier code is therefore pinned exactly like the product state — acceptance
    # re-runs as (verifier@candidate_sha x product@integration_sha).  Fail-closed:
    # a git-backed project whose receipts carry no control pin cannot be proven.
    control_pin = ""
    control_root = _git_toplevel(project_dir)
    if control_root:
        control_pin, why = _control_pin(project_dir, control_root, verdict.receipts,
                                        attempt_keys)
        if why:
            verdict.reasons.append(why)
            return verdict

    if run_acceptance:
        # A receipt proves an attempt happened as recorded; it does not prove the
        # product still meets its contract at that commit.  Re-run acceptance now,
        # pinned to the exact final integration SHA, and persist nothing (S3).
        try:
            from .authority_gate import IndependentValidator
            result = IndependentValidator(
                project_dir, timeout_s=timeout_s, persist_evidence=False,
                control_pin=control_pin,
            ).validate(contract, integration_sha=integration_sha)
            if not result.milestone_complete:
                verdict.reasons.append(f"CURRENT_ACCEPTANCE_FAILS:{result.failures[:3]}")
                return verdict
        except Exception as exc:
            verdict.reasons.append(f"ACCEPTANCE_NOT_EVALUABLE:{exc}")
            return verdict

    verdict.proven = True
    return verdict


def _product_repo(project_dir: Path, contract: dict) -> Path:
    body = contract.get("body", {}) or {}
    pr = body.get("product_repo", "")
    if pr:
        candidate = Path(pr)
        if candidate.exists():
            return candidate
    return Path(project_dir)


from ._io import _git as _git_io


def _git_rc(repo: Path, *args: str) -> tuple[int, str]:
    try:
        r = _git_io(repo, *args, timeout=60)
        return r.returncode, r.stdout.strip()
    except Exception as exc:
        return 1, str(exc)


def _git_toplevel(project_dir: Path) -> "Path | None":
    """Repo root owning the project authority dir (the control repo), or None.

    Note Path('') == Path('.') is truthy, so the no-repo case must be None — an
    empty Path here would silently make every temp-dir project 'git-backed'.
    """
    rc, out = _git_rc(Path(project_dir), "rev-parse", "--show-toplevel")
    return Path(out) if rc == 0 and out else None


def _control_pin(project_dir: Path, control_root: Path, receipts: list,
                 attempt_keys: dict) -> tuple[str, str]:
    """The single control commit the receipts' attempts actually ran under (S5).

    GAT-01: this used to read receipt.candidate_sha as if it named the control
    commit. In real dispatch it does not — executors.py sets a receipt's
    candidate_sha identically to integration_sha (the PRODUCT commit), so this
    compared a product SHA against control-repo history and rejected almost every
    real settlement (CONTROL_PIN_NOT_A_COMMIT). The control commit IS observed,
    independently, once per attempt: launch_gate.open_account() records it as
    `code_sha` (from current_code_sha()) in attempt_accounts.jsonl before the
    attempt even starts. `attempt_keys` maps mission_id -> the exact ledger row
    that attempt opened (PROJECT_STATE.missions[mid]['last_attempt_key']), so the
    control commit is read from THAT row, never from the receipt.

    A project with no ledger rows at all predates the ledger — the same exemption
    settlement_blockers() already grants pre-ledger-introduction settlements — and
    falls back to the legacy receipt-carried candidate_sha so history proven under
    it is not retroactively broken. Once a ledger exists, an attempt this function
    cannot resolve to a code_sha in it is rejected outright: there is no fail-open
    branch back to a receipt field that does not hold what this function needs.
    """
    from .launch_gate import _accounts
    rows_by_key = {a.get("attempt_key"): a for a in _accounts(project_dir)
                  if a.get("attempt_key")}
    legacy_no_ledger = not rows_by_key
    shas = []
    for r in receipts:
        if legacy_no_ledger:
            shas.append((r.mission_id, (getattr(r, "candidate_sha", "") or "").strip()))
            continue
        row = rows_by_key.get(attempt_keys.get(r.mission_id, ""))
        shas.append((r.mission_id, (row.get("code_sha") or "").strip() if row else ""))
    blank = [m for m, s in shas if not s]
    if blank:
        return "", ("CONTROL_PIN_MISSING: receipts of a git-backed settlement must "
                    f"resolve a control code_sha (the control commit whose verifier "
                    f"accepted); blank for {blank[:3]}")
    for mission_id, sha in shas:
        if _git_rc(control_root, "cat-file", "-e", f"{sha}^{{commit}}")[0] != 0:
            return "", f"CONTROL_PIN_NOT_A_COMMIT:{mission_id}:{sha[:12]}"
    for _, candidate in shas:
        if all(other == candidate
               or _git_rc(control_root, "merge-base", "--is-ancestor",
                       other, candidate)[0] == 0
               for _, other in shas):
            return candidate, ""
    return "", ("CONTROL_PINS_ON_UNRELATED_LINEAGES:"
                f"{[s[:12] for _, s in shas]} — no single control commit "
                "contains them all")


def _final_integration_sha(repo: Path, receipts: list) -> tuple[str, str]:
    """The exact commit this milestone integrated at, or a rejection reason.

    S2: with several missions there are several integration SHAs, and only one of them
    is the point the milestone actually reached.  The final SHA is the one that has
    every other mission's SHA as an ancestor; anything else means the attempts are on
    unrelated lineages and no single commit represents the settlement.

    A repo that is not under git has no commit to pin to and none to substitute either,
    so validation runs unpinned there.  When the repo IS under git, a blank integration
    SHA is a rejection: we cannot say which commit was settled.
    """
    if _git_rc(repo, "rev-parse", "--git-dir")[0] != 0:
        return "", ""                      # not a git repo: nothing to pin, nothing to swap

    shas = [(r.mission_id, (r.integration_sha or "").strip()) for r in receipts]
    blank = [m for m, sha in shas if not sha]
    if blank:
        return "", (f"RECEIPT_HAS_NO_INTEGRATION_SHA:{blank[:3]} — the product repo is "
                    "under git, so a settlement must name the commit it reached")

    for mission_id, sha in shas:
        if _git_rc(repo, "cat-file", "-e", f"{sha}^{{commit}}")[0] != 0:
            return "", f"INTEGRATION_SHA_NOT_A_COMMIT:{mission_id}:{sha[:12]}"

    # The final SHA is the unique tip: every other SHA is one of its ancestors.
    for _, candidate in shas:
        if all(other == candidate
               or _git_rc(repo, "merge-base", "--is-ancestor", other, candidate)[0] == 0
               for _, other in shas):
            return candidate, ""
    return "", ("INTEGRATION_SHAS_ON_UNRELATED_LINEAGES:"
                f"{[s[:12] for _, s in shas]} — no single commit contains them all")


def proven_completed_milestones(project_dir: Path, run_acceptance: bool = True) -> list:
    """Every milestone whose settlement survives verify_settlement().

    STA-03: only entries recorded state==COMPLETE can possibly be proven —
    verify_settlement rejects anything else on its first check anyway — so an
    IN_PROGRESS/PENDING_RERESOLUTION/UNPROVEN_AFTER_SUPERSESSION entry must not
    even reach it. Same convention project_authority.resolve() already uses.
    """
    state = _load_state(project_dir)
    return sorted(
        mid for mid, rec in (state.get("completed_milestones", {}) or {}).items()
        if isinstance(rec, dict) and rec.get("state") == "COMPLETE"
        and verify_settlement(project_dir, mid, state=state,
                             run_acceptance=run_acceptance).proven
    )


def proven_settlements(project_dir: Path, run_acceptance: bool = True) -> list:
    """Every SettlementVerdict that is proven, with its verified receipts retained.

    STA-03: same COMPLETE-only filter as proven_completed_milestones — see there.
    """
    state = _load_state(project_dir)
    out = []
    for mid, rec in sorted((state.get("completed_milestones", {}) or {}).items()):
        if not (isinstance(rec, dict) and rec.get("state") == "COMPLETE"):
            continue
        v = verify_settlement(project_dir, mid, state=state,
                              run_acceptance=run_acceptance)
        if v.proven:
            out.append(v)
    return out


def proven_engines(project_dir: Path, run_acceptance: bool = True) -> set:
    """Engines that ran on the exact mission attempts behind proven settlements.

    S1: engine diversity used to be counted by walking evidence/ for JSON that looked
    like a dispatch receipt.  Narrowing that to a schema string did not help — a worker
    can write the schema too, and did: two forged files produced two "distinct engines".
    Any rule of the form "scan a directory and trust what you find" is forgeable by
    whatever can write to that directory.

    So nothing is scanned.  The engines come from MissionExecutionReceipts loaded at
    supervisor-controlled paths — evidence/receipts/<mission_id>/<last_attempt_id>.json,
    where both components come from PROJECT_STATE — whose hash and contract lineage
    verify, and which belong to a settlement that is itself proven.  A file the
    supervisor never asks for is never read.
    """
    engines: set = set()
    for verdict in proven_settlements(project_dir, run_acceptance=run_acceptance):
        engines |= verdict.engines
    return engines


def settlement_blockers(project_dir: Path, run_acceptance: bool = True) -> list:
    """Reasons this project must not enter a long run.  Empty list == ready."""
    project_dir = Path(project_dir)
    blockers: list[str] = []
    if (project_dir / "SAFETY_QUARANTINE.json").is_file():
        try:
            cause = json.loads((project_dir / "SAFETY_QUARANTINE.json")
                               .read_text(encoding="utf-8")).get("cause", "?")
        except Exception:
            cause = "unreadable"
        blockers.append(f"ACTIVE_QUARANTINE({cause})")

    # Sol R2 enforcement (wired by Claude round-1): the tracked settlement
    # ledger must VERIFY, and every milestone settled after the ledger was
    # introduced must carry a ledger entry — promotion is gated on tracked,
    # reviewable evidence.  Pre-ledger history stays exempt (it predates the
    # substrate and was reconciled by its own counter-audits).
    try:
        from .settlement_ledger import load_index, verify_ledger
        _repo_root = Path(__file__).resolve().parent.parent
        _problems = verify_ledger(_repo_root, check_git=True)
        if _problems:
            blockers.append(f"SETTLEMENT_LEDGER_INVALID({_problems[0]})")
        _idx = load_index(_repo_root)
        _introduced = str(_idx.get("introduced_at", ""))
        spath = project_dir / "PROJECT_STATE.json"
        if _introduced and spath.is_file():
            _st = _load_state(project_dir) or {}
            _entries = set((_idx.get("entries") or {}).keys())
            # L5_factory_v2_rest_2-011: a milestone settled by a SECOND project
            # in the shared ledger lives under a project-qualified key
            # `{slug}__{milestone}` — it still proves this milestone settled.
            def _covered(mid: str) -> bool:
                return (mid in _entries
                        or any(k.endswith("__" + mid) for k in _entries))
            for mid, rec in sorted((_st.get("completed_milestones") or {}).items()):
                if (isinstance(rec, dict) and rec.get("state") == "COMPLETE"
                        and str(rec.get("settled_at", "")) >= _introduced
                        and not _covered(mid)):
                    blockers.append(
                        f"SETTLED_WITHOUT_LEDGER_ENTRY({mid} settled "
                        f"{rec.get('settled_at')} >= ledger introduction "
                        f"{_introduced} — no tracked evidence entry)")
    except Exception as _exc:
        blockers.append(f"SETTLEMENT_LEDGER_UNEVALUABLE({_exc})")

    spath = project_dir / "PROJECT_STATE.json"
    if not spath.is_file():
        return blockers          # nothing has run here; no settled truth to contradict
    state = _load_state(project_dir)
    if not state:
        return blockers + ["PROJECT_STATE_UNREADABLE"]

    unproven = sorted(mid for mid, rec in (state.get("missions", {}) or {}).items()
                      if isinstance(rec, dict) and rec.get("state") == "UNPROVEN")
    if unproven:
        blockers.append(f"UNPROVEN_PASS_MISSIONS({','.join(unproven[:5])})")

    for mid, rec in sorted((state.get("completed_milestones", {}) or {}).items()):
        if not isinstance(rec, dict) or rec.get("state") != "COMPLETE":
            blockers.append(f"NON_COMPLETE_SETTLEMENT({mid})")
            continue
        v = verify_settlement(project_dir, mid, state=state,
                              run_acceptance=run_acceptance)
        if not v.proven:
            blockers.append(f"UNPROVEN_SETTLEMENT({mid}:{';'.join(v.reasons[:2])})")
    return blockers
