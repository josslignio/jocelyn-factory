"""Validation contract (GAP-028) — le contrat de complétion est gelé AVANT le build.

Décision externe: ADAPT Factory Missions — « the agent that implemented is never
the sole judge of its own work »: le contrat est dérivé de STRATEGY/SPEC/ROADMAP
(hashes liés), jamais écrit par le worker. Le worker ne peut pas deviner ni
réécrire les critères qui déclareront son travail complet.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path

from .project_authority import (ProjectAuthority, STATE_FILENAME,
                                atomic_write_json, load_authority)
from factory_v2._io import _now

CONTRACTS_DIRNAME = "validation_contracts"

#: Length of a contract_id. The id is the anchor that binds a frozen contract to
#: the ref PROJECT_STATE files it under, so it is compared at exactly this length
#: — never as a prefix of whatever the caller happened to supply.
CONTRACT_ID_LEN = 16


def contract_id(authority: ProjectAuthority, milestone_id: str, body: dict) -> str:
    payload = json.dumps({"identity": authority.identity.as_dict(),
                          "milestone": milestone_id, "body": body},
                         sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:CONTRACT_ID_LEN]


def contract_id_from_body(body: dict) -> str:
    """Recompute a contract's id from the CONTENT it claims to be.

    `contract_id` folds the authority identity, the milestone and the whole body
    — and the frozen body already carries `authority_identity`, so the id can be
    recomputed from the stored contract alone and checked against the id the
    caller asked for. That caller-supplied ref comes from PROJECT_STATE, i.e.
    from outside the file, which is what makes this a real binding rather than a
    self-report: an edited body no longer hashes to the ref it is filed under.

    This is the anchor a self-reported `contract_signature` cannot be. A
    signature travels inside the file and can simply be recomputed — or deleted —
    by whoever edited the body. The id cannot, because the name it must match is
    held elsewhere.
    """
    payload = json.dumps({"identity": body.get("authority_identity"),
                          "milestone": body.get("milestone"), "body": body},
                         sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def contract_signature(body: dict) -> str:
    """Compatibility signature over the VALIDATION SEMANTICS only.

    R2.3: contract_id folds in authority hashes, so editing one comment line in
    STRATEGY.md mints a new contract_id and every existing PASS receipt stops matching
    — a routine authority edit (or the REPLAN stage, which is itself a recovery step)
    invalidated all proofs and dead-ended the milestone.

    The signature covers only what actually decides whether work is complete:
    acceptance conditions, required artifacts, the product_e2e command and forbidden
    regressions.  Same semantics -> same signature -> an existing receipt still proves
    the same thing.  Change any acceptance condition and the signature changes, which
    forces re-execution.  Reuse is permitted by demonstrated compatibility, never by
    ignoring a mismatch.
    """
    semantics = {
        "acceptance": body.get("acceptance") or [],
        "required_artifacts": sorted(body.get("required_artifacts") or []),
        "product_e2e": body.get("product_e2e") or {},
        "forbidden_regressions": sorted(body.get("forbidden_regressions") or []),
        "product_repo": body.get("product_repo", ""),
    }
    # Multi-AI review requirement: part of the validation semantics ONLY when
    # declared. Guarded inclusion keeps every contract frozen before the field
    # existed byte-identical in signature (receipts stay valid per R2.3), while
    # a contract that declares it binds the requirement to the signature —
    # toggling it forces re-execution, it can never be silently edited away.
    if body.get("multi_ai_review"):
        semantics["multi_ai_review"] = body["multi_ai_review"]
    # 2026-09-18: the WORK is part of the semantics. A mission id (#M1) whose
    # step changed from a local re-check to an autopilot repair had its old
    # PASS "adopted as compatible" and the repair never ran (audit review
    # blocked 7 h). Guarded like multi_ai_review: contracts frozen without
    # the field keep their signature byte-identical.
    # (execution_identity is kept in the body for the adoption check in
    # mission_dispatch, but NOT in the signature: putting it there on
    # 2026-09-18 invalidated every PASS of every refrozen contract and the
    # factory started rebuilding already-merged lots — ORPHAN_BY_SESSION,
    # SAFE_SELF_IMPROVEMENT — with real engines.)
    payload = json.dumps(semantics, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _execution_identity(milestone_entry: dict) -> list:
    """What kind of work each step IS (kind + executor type, in order). A
    task-file path fix or a command swap stays compatible (an OWNER grant
    survives it — test_owner_grant_local_command); a local re-check turned
    into an autopilot build, or a step added/removed, does not."""
    out = []
    for st in ((milestone_entry.get("execution") or {}).get("steps") or []):
        if not isinstance(st, dict):
            continue
        ex = st.get("executor") or {}
        out.append({"kind": str(st.get("kind") or ""),
                    "type": str(ex.get("type") or "")})
    return out


def _merge_forbidden_regressions(global_list, milestone_list) -> list:
    out: list = []
    for item in list(global_list or []) + list(milestone_list or []):
        if item not in out:
            out.append(item)
    return out


def build_contract(authority: ProjectAuthority, milestone_entry: dict) -> dict:
    mid = milestone_entry["id"]
    # product_repo: the canonical product code repository for this project.
    # Distinct from the project authority directory (where STRATEGY/SPEC/ROADMAP live).
    # Flows from ROADMAP.yaml → contract → executor → dispatcher --repo flag → validator cwd.
    product_repo = str(authority.roadmap.get("product_repo") or "")
    body = {
        "milestone": mid,
        "goal": milestone_entry.get("goal", ""),
        "acceptance": list(milestone_entry.get("acceptance") or []),
        "required_artifacts": list(milestone_entry.get("deliverables") or []),
        "product_e2e": dict(milestone_entry.get("product_e2e") or {}),
        "product_repo": product_repo,
        # L2_runtime_core-001: the global final_dod list used to REPLACE the
        # milestone's own via `or` — a local regression control vanished from
        # the frozen contract as soon as any global one existed. Merge both,
        # order-preserving, deduplicated.
        "forbidden_regressions": _merge_forbidden_regressions(
            (authority.roadmap.get("final_dod") or {}).get("forbidden_regressions"),
            milestone_entry.get("forbidden_regressions")),
        # Canonical multi-AI review requirement, declared per milestone in
        # ROADMAP.yaml. Absent → empty dict → no review required (signature
        # unchanged). Present → the settlement path must obtain a bounded
        # read-only review bound to the exact candidate SHA before settling.
        "multi_ai_review": dict(milestone_entry.get("multi_ai_review") or {}),
        "execution_identity": _execution_identity(milestone_entry),
        "authority_identity": authority.identity.as_dict(),
    }
    # R2.3: lineage marker — stable across authority edits that do not change what
    # "complete" means.  Stored inside the body so it is frozen with the contract.
    body["contract_signature"] = contract_signature(body)
    return {"schema": "validation-contract/v2",
            "contract_id": contract_id(authority, mid, body),
            "contract_signature": body["contract_signature"],
            "frozen_at": _now(),
            "source": {"strategy_sha": authority.identity.strategy_sha,
                       "spec_sha": authority.identity.spec_sha,
                       "roadmap_sha": authority.identity.roadmap_sha},
            "body": body}


#: The complete, closed set of contracts frozen before the current id derivation
#: existed, each pinned to the sha256 of its exact bytes.
#:
#: These are real historical freezes — the twitter-radar trio was hand-frozen by a
#: gen4 controller session, the rest are the LR/containment harness contracts —
#: whose refs are not canonical ids at all (24-char digests, or plain names like
#: `lr2_interprocess_frozen`). Seven documented variants of the derivation were
#: tried against them and none reproduces those refs, so they cannot be verified
#: the way a modern contract is.
#:
#: An earlier version accepted "any non-canonical ref that matches its committed
#: git blob". That was a loophole: an editor could file a tampered contract under
#: a non-canonical ref, commit it, and be waved through by the weaker anchor.
#: Pinning the exact digests closes it — this list is finite, it is code, and it
#: can only grow through a reviewed change. No contract the Factory writes today
#: can ever land here, because every new contract has a recomputable id.
_LEGACY_CONTRACT_DIGESTS = {
    "24213dae84de029e2a9571ba":
        "519fc24dcad2bc38763c6e88ee413c8838f0b056c02fd18626828bd686906da2",
    "35aee1394ff0e02968624066":
        "3aea3dfd1cc2c850405a6b3c2228bdfd7c0320e4cc9e8c79c07acd5b3b794c6c",
    "74ee52f7f07d4264547ea08a":
        "5a94fbf7bd131e3d563590dea6096a6b91dc145f1e23cf87b6d7429234aef5ce",
    "automated_multi_ai_cto_loop_0a08a045b599b62ea796de35":
        "4f03120d82e25a2aeb6d896a258c5e2f1e63bf76ae9405bd7c63532ca4e2e3f6",
    "ext_supervisor_containment_frozen":
        "fc767d0849fce743af35c4a75020a001ba5d13241bb89c137187cbc0040a66da",
    "lr2_adopt_holder_frozen":
        "83d7ea16e926ca63c167f6ed77fddd1d8c2782fe9831a9b52bf6fe2a49d94e93",
    "lr2_interprocess_frozen":
        "4099966f8dc2105a61301da5130e8be771780d39524441bf67ae61b95c1e6b54",
    "lr3_interprocess_frozen":
        "ea15abe08aaf6c48396692248b1a99a3f071d9f97201736b93aa98ce5f828439",
    "lr4_interprocess_frozen":
        "a34eb152db75058d31543e2347b37406c259c03a716ac21b1fbc89d0e7247f7f",
    "lr5_real_harness_frozen":
        "25f6a432d27a1d873c4d418d195f5d77cf75be5e4183299f0e24d7b50675ec1d",
    "tr_call_episodes_frozen":
        "818030f28054cce0847c0f5431bb8dcfad23cba99002336723b251b309dc7d87",
    "tr_data_first_session_frozen":
        "36d9ee04b38a23553a515a501bef35b370339a1b79164f67585ea302df57e366",
    "tr_kol_framework_frozen":
        "f6977e293d7279e4b9452624f5c1421269cee519c955513bf1ccff5d18e64c75",
}


def _legacy_digest_mismatch(contract_ref: str, raw: bytes) -> str:
    """Empty string when this ref is a known legacy contract and its bytes are unchanged."""
    pinned = _LEGACY_CONTRACT_DIGESTS.get(contract_ref)
    if pinned is None:
        return ("its id does not recompute and it is not one of the "
                f"{len(_LEGACY_CONTRACT_DIGESTS)} contracts frozen before the "
                "current derivation existed")
    seen = hashlib.sha256(raw).hexdigest()
    if seen != pinned:
        return (f"it is a known legacy contract but its bytes changed "
                f"(pinned {pinned[:12]}, found {seen[:12]})")
    return ""


class ContractIntegrityError(RuntimeError):
    """A stored frozen contract does not match the canonical one it claims to be.

    Fail closed: a milestone whose definition of "complete" cannot be trusted must
    stop the line, not quietly fall back to a rebuild (which would leave the bad
    file in place, still claiming to be the contract) and not be used as-is.
    """


def _differing_keys(stored: dict, canonical: dict) -> str:
    """Where a stored contract and the canonical one disagree — for the refusal
    message, so a corrupt or edited contract says WHAT changed."""
    keys = sorted(set(stored) | set(canonical))
    out = [k for k in keys
           if k != "frozen_at" and stored.get(k) != canonical.get(k)]
    if "body" in out:
        sb, cb = stored.get("body") or {}, canonical.get("body") or {}
        out = [k for k in out if k != "body"] + [
            f"body.{k}" for k in sorted(set(sb) | set(cb)) if sb.get(k) != cb.get(k)]
    return ", ".join(out) or "<no field differs>"


def _normalize_for_compare(contract: dict) -> dict:
    """Drop the multi_ai_review body key where it is absent/empty.

    Contracts frozen before the field existed carry no `multi_ai_review` body
    key; the canonical rebuild now always carries one (empty when undeclared).
    For comparison purposes absent and empty are the same contract — anything
    else still differs fatally. Scope: this normalization may NEVER be applied
    to a contract that is actually USED; it exists only so the
    freeze-time comparison does not reject historical contracts whose
    semantics did not change.
    """
    out = dict(contract)
    body = dict(out.get("body") or {})
    if not body.get("multi_ai_review"):
        body.pop("multi_ai_review", None)
        out["body"] = body
    return out


def freeze_contract(project_dir: Path, authority: ProjectAuthority,
                    milestone_entry: dict) -> dict:
    """Freeze (or re-open) the contract that defines "complete" for a milestone.

    Returns the DURABLE contract. When one is already frozen under this
    contract_id, the stored file is the answer — not a freshly built object.

    The stored file is VERIFIED against the canonical rebuild before any of it is
    trusted, and only `frozen_at` is taken from it. An earlier version accepted the
    stored dict whenever its `contract_id` field matched — but that id is written
    inside the file, so it is self-declared: a stored contract with an edited
    acceptance list or a neutered `product_e2e` command was returned as canonical
    (reproduced in A/probe_contract_tamper.py). Matching an id string is not
    content verification.

    Preserving `frozen_at` is the point: rebuilding it stamped "now" on every
    cycle, and a contract frozen at T must keep reporting T — callers record
    `frozen_at` as evidence, and a freeze time that moves every time it is read is
    simply false.

    Observed 2026-09-11 (root's restart counter-probe): that moving timestamp
    flowed into the settlement ledger entry, so a settlement recorded before a
    crash and the identical settlement retried after it did not compare equal,
    and the completed milestone could not finish.
    """
    contract = build_contract(authority, milestone_entry)
    cdir = Path(project_dir) / CONTRACTS_DIRNAME
    cdir.mkdir(parents=True, exist_ok=True)
    path = cdir / f"{contract['contract_id']}.json"
    if not path.exists():
        # X5: a frozen contract is the definition of "complete" for a milestone — as
        # load-bearing as a receipt, so it gets the same durable write.
        atomic_write_json(path, contract, sort_keys=True)
        return contract
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ContractIntegrityError(
            f"FROZEN_CONTRACT_UNREADABLE: {path}: {exc}") from exc
    if not isinstance(stored, dict):
        raise ContractIntegrityError(f"FROZEN_CONTRACT_NOT_AN_OBJECT: {path}")

    # `contract_id` lives INSIDE the file, so matching it proves nothing on its
    # own — it is a self-declared string, and trusting it would load a tampered
    # acceptance list or a neutered product_e2e command as if it were canonical.
    # Verify the whole contract against the canonical rebuild instead, and let
    # exactly one field differ: `frozen_at`, which is the thing being preserved.
    expected_modulo_time = _normalize_for_compare(
        dict(stored, frozen_at=contract["frozen_at"]))
    if expected_modulo_time != _normalize_for_compare(contract):
        raise ContractIntegrityError(
            f"FROZEN_CONTRACT_BODY_MISMATCH: {path}: "
            f"differs from the canonical contract at {_differing_keys(stored, contract)} "
            "— a stored contract is never authority for its own content")

    frozen_at = stored.get("frozen_at", "")
    if not isinstance(frozen_at, str) or not frozen_at:
        raise ContractIntegrityError(f"FROZEN_CONTRACT_BAD_FROZEN_AT: {path}")
    # Return the CANONICAL object carrying the durable freeze time. Nothing read
    # from disk is passed through, so no stored byte can become semantics.
    return dict(contract, frozen_at=frozen_at)


def load_frozen_contract(project_dir: Path, contract_ref: str) -> dict:
    """Load a frozen contract, refusing one whose semantics do not recompute.

    This is the path taken on EVERY cycle after the first (the IN_PROGRESS
    branch), which is when dispatch and settlement actually happen — so hardening
    only `freeze_contract` secured the branch taken once and left open the branch
    that matters. A file edited between cycles used to flow straight into
    `compile_missions` and into `IndependentValidator.validate(contract)`, i.e.
    the authoritative settlement gate would execute a tampered `product_e2e`
    command and settle the milestone COMPLETE.

    No authority object is available here, so the self-consistency checks are the
    ones that do not need it: the declared id must match the file it was loaded
    from, and `contract_signature` must recompute over the body. That signature
    covers exactly the semantics that decide completion — acceptance conditions,
    required artifacts, the product_e2e command, forbidden regressions and the
    product repo — which is the tamper surface that matters. Same check
    `audit_loop.bind_frozen_contract` already applies.

    Raises ContractIntegrityError; never returns a contract it could not verify.
    """
    # The ref reaches us from PROJECT_STATE, so it is not trusted as a path
    # component. A ref containing separators or `..` could never be ACCEPTED (it
    # neither recomputes nor is pinned), but it would still make this function
    # read a file outside the contracts directory and name it in the error.
    if contract_ref != Path(contract_ref).name or contract_ref in ("", ".", ".."):
        raise ContractIntegrityError(
            f"FROZEN_CONTRACT_REF_NOT_A_NAME: {contract_ref!r} is not a bare "
            "contract id; a ref is a name, never a path")
    path = Path(project_dir) / CONTRACTS_DIRNAME / f"{contract_ref}.json"
    try:
        raw = path.read_bytes()
        contract = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise ContractIntegrityError(
            f"FROZEN_CONTRACT_UNREADABLE: {path}: {exc}") from exc
    # Pinned legacy contracts are settled FIRST and on bytes alone. Their shape is
    # historical — some carry no `body` dict and one declares an id that differs
    # from its filename — so the modern structural checks below would reject real,
    # already-reviewed freezes. The digest pin is their verification in full: these
    # exact bytes, or nothing.
    if contract_ref in _LEGACY_CONTRACT_DIGESTS:
        why = _legacy_digest_mismatch(contract_ref, raw)
        if why:
            raise ContractIntegrityError(
                f"FROZEN_CONTRACT_UNVERIFIABLE: {path}: {why}. A contract that "
                "cannot be bound to anything outside itself is not authority for "
                "its own content")
        if not isinstance(contract, dict):
            raise ContractIntegrityError(f"FROZEN_CONTRACT_NOT_AN_OBJECT: {path}")
        return contract

    if not isinstance(contract, dict) or not isinstance(contract.get("body"), dict):
        raise ContractIntegrityError(f"FROZEN_CONTRACT_NOT_AN_OBJECT: {path}")
    if contract.get("contract_id") != contract_ref:
        raise ContractIntegrityError(
            f"FROZEN_CONTRACT_ID_MISMATCH: {path}: declares "
            f"{contract.get('contract_id')!r}, loaded as {contract_ref!r}")
    body = contract["body"]

    # THE ANCHOR: recompute the id from the content and require it to equal the
    # ref the CALLER asked for (which comes from PROJECT_STATE, outside this
    # file). Editing the body, deleting a signature, or re-signing both copies
    # all change what the content hashes to, and none of them can change the
    # name it is filed under.
    # Exactly CONTRACT_ID_LEN, never a prefix of arbitrary length: the ref comes
    # from PROJECT_STATE, which the same editor could shorten. A one-character ref
    # would otherwise match almost any digest, turning the anchor into a formality.
    recomputed = contract_id_from_body(body)
    if len(contract_ref) == CONTRACT_ID_LEN \
            and recomputed[:CONTRACT_ID_LEN] == contract_ref:
        _verified = "ID_RECOMPUTED"
    elif len(contract_ref) == CONTRACT_ID_LEN:
        # A MODERN contract must recompute. The git fallback below is NOT
        # available to it: otherwise anyone who edits a contract and commits the
        # edit would be verified by the weaker anchor, routing around the strong
        # one entirely. Committing a tamper must not launder it.
        raise ContractIntegrityError(
            f"FROZEN_CONTRACT_UNVERIFIABLE: {path}: id does not recompute from "
            f"its own body (expected ref {contract_ref}, content hashes to "
            f"{recomputed[:CONTRACT_ID_LEN]}). A contract is not authority for "
            "its own content, and committing an edit does not make it one")
    else:
        # A non-canonical ref that is not on the pinned list has nothing outside
        # itself to be bound to. Filing a tampered contract under such a ref — and
        # committing it — reaches here and is refused, because the only escape is
        # a digest already written in code.
        raise ContractIntegrityError(
            f"FROZEN_CONTRACT_UNVERIFIABLE: {path}: ref {contract_ref!r} is "
            f"neither a canonical {CONTRACT_ID_LEN}-character id nor one of the "
            f"{len(_LEGACY_CONTRACT_DIGESTS)} contracts frozen before the current "
            "derivation existed. A contract that cannot be bound to anything "
            "outside itself is not authority for its own content")

    # A signature that is present must still recompute. This is corruption
    # detection, NOT the anchor: it travels inside the file and whoever edited
    # the body can recompute it. Absence is not evidence of anything — the field
    # postdates most of these contracts.
    stored_sig = body.get("contract_signature")
    if stored_sig is not None and stored_sig != contract_signature(body):
        raise ContractIntegrityError(
            f"FROZEN_CONTRACT_SIGNATURE_MISMATCH: {path}: frozen semantics do not "
            "recompute — acceptance, required artifacts, product_e2e command, "
            "forbidden regressions or product repo were edited after freezing")

    # `source` is NOT inside the body, so it is not covered by the id. Left
    # unchecked, editing it could make a STALE contract look current to
    # verify_frozen_against_current and un-drift work frozen against older
    # authority. Where the body carries the same keys, they must agree.
    src = contract.get("source")
    ident = body.get("authority_identity")
    if isinstance(src, dict) and isinstance(ident, dict):
        disagree = [k for k, v in src.items() if k in ident and ident[k] != v]
        if disagree:
            raise ContractIntegrityError(
                f"FROZEN_CONTRACT_SOURCE_DISAGREES: {path}: source {disagree} "
                "does not match the authority identity frozen in the body — "
                "drift must not be clearable by editing the copy outside the id")
    top_sig = contract.get("contract_signature")
    if top_sig is not None and stored_sig is not None and top_sig != stored_sig:
        # Two copies of the same signature are written at freeze time, so a
        # disagreement means one was edited — including the case of an editor who
        # re-signed the body but missed this copy.
        raise ContractIntegrityError(
            f"FROZEN_CONTRACT_SIGNATURE_DISAGREES: {path}: top-level signature "
            "does not match the body's")
    return contract


def verify_frozen_against_current(contract: dict, authority: ProjectAuthority) -> list[str]:
    """Le contrat gelé doit correspondre à l'autorité courante — sinon contexte périmé."""
    frozen = contract["source"]
    current = authority.identity
    drift = []
    for key, cur in (("strategy_sha", current.strategy_sha),
                     ("spec_sha", current.spec_sha),
                     ("roadmap_sha", current.roadmap_sha)):
        if frozen.get(key) != cur:
            drift.append(f"{key.replace('_sha', '.sha')}: contract frozen against "
                         f"{str(frozen.get(key))[:8]} but authority is {cur[:8]}")
    return drift


def record_contract_in_state(project_dir: Path, state: dict, contract: dict) -> dict:
    state["validation_contracts"][contract["body"]["milestone"]] = {
        "contract_id": contract["contract_id"],
        "frozen_at": contract["frozen_at"],
        "status": "FROZEN",
    }
    return state


def refreeze_for_milestone(project_dir: Path, milestone_id: str) -> dict:
    """Freeze a contract for `milestone_id` against the CURRENT authority.

    A frozen contract binds a milestone's acceptance to the exact STRATEGY/SPEC/ROADMAP
    it was frozen against. When the owner legitimately edits canonical intent, every
    contract frozen against the previous text reports drift and its milestone can no
    longer settle — correctly, because the acceptance it froze may no longer be the
    acceptance the owner declares.

    Re-freezing is therefore a real, bounded execution step: it re-derives the contract
    from current authority and writes it. It does NOT weaken any acceptance and it does
    NOT touch PROJECT_STATE — a re-frozen contract still has to be satisfied.
    """
    authority = load_authority(Path(project_dir))
    entries = [m for m in authority.roadmap["milestones"]
               if isinstance(m, dict) and m.get("id") == milestone_id]
    if not entries:
        raise KeyError(f"milestone {milestone_id!r} is not in "
                       f"{project_dir}/ROADMAP.yaml — nothing to freeze")
    return freeze_contract(Path(project_dir), authority, entries[0])


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        prog="factory_v2.validation_contract",
        description="Freeze a milestone validation contract against current authority.")
    ap.add_argument("--project-dir", required=True)
    ap.add_argument("--milestone", required=True)
    args = ap.parse_args(argv)
    contract = refreeze_for_milestone(Path(args.project_dir), args.milestone)
    drift = verify_frozen_against_current(
        contract, load_authority(Path(args.project_dir)))
    print(json.dumps({"contract_id": contract["contract_id"],
                      "milestone": args.milestone,
                      "frozen_against": contract["source"],
                      "drift": drift}, indent=1, sort_keys=True))
    return 1 if drift else 0


if __name__ == "__main__":
    raise SystemExit(main())
