#!/usr/bin/env python3
"""LR5 preflight receipt verifier.

Validates factory_v2/receipts/LR5_PREFLIGHT_RECEIPT.json against all six
LR5 acceptance criteria:

  1. integration_sha in receipt equals HEAD post-merge (exact SHA recorded)
  2. ACCEPTED branch preserved after execution (branch_preserved: true in receipt)
  3. route(A, build, hint=codex) selects claude — ActorCapabilityPolicy verified
  4. kill+restart recovery: lease adopted after restart (kill_restart_recovery: PASS)
  5. Double dispatch same mission_id → second REJECTED (cas_double_dispatch: REJECTED)
  6. ROUTINE_HUMAN_INTERVENTIONS = 0 during the preflight run

Exit 0 if all checks pass; exit 1 with diagnostic output if any fail.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


# ── structural observation parsers (S017-03: existence alone proves nothing) ───

def parse_kill_restart_evidence(path: Path) -> dict:
    """Structurally verify a kill/restart observation artifact.  Raises on any
    malformed or self-contradictory shape; returns the parsed record when honest.

    Required shape (written by run_lr5_preflight from what it OBSERVED):
      pid_a/pid_b: two distinct real daemon pids
      birth_a/birth_b: two distinct process birth times
      milestones_before/milestones_after: strict growth across the kill boundary
      fault_record: the injected SIGKILL record (outcome INJECTED)
    """
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    def _need(key: str):
        if not d.get(key):
            raise ValueError(f"missing {key!r}")
        return d[key]
    pid_a, pid_b = _need("pid_a"), _need("pid_b")
    birth_a, birth_b = _need("birth_a"), _need("birth_b")
    if pid_a == pid_b:
        raise ValueError(f"pid_a == pid_b ({pid_a}): a restart needs a NEW process")
    if birth_a == birth_b:
        raise ValueError("birth_a == birth_b: pid reuse or a copied observation")
    before = _need("milestones_before")
    after = _need("milestones_after")
    if not isinstance(before, list) or not isinstance(after, list):
        raise ValueError("milestone timelines must be lists")
    if not set(before) < set(after):
        raise ValueError(f"no post-restart milestone progress: {before} -> {after}")
    fr = _need("fault_record")
    if fr.get("outcome") != "INJECTED":
        raise ValueError(f"fault_record.outcome={fr.get('outcome')!r} — no injected kill")
    return d


def parse_cas_evidence(path: Path) -> dict:
    """Structurally verify a CAS double-dispatch observation artifact."""
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    def _need(key: str):
        if not d.get(key):
            raise ValueError(f"missing {key!r}")
        return d[key]
    mission = _need("mission_id")
    first = _need("first_outcome")
    second = _need("second_outcome")
    if "CAS_ALLOWED" not in str(first):
        raise ValueError(f"first dispatch not allowed: {first!r}")
    if "CAS_DUPLICATE" not in str(second):
        raise ValueError(f"second dispatch not rejected: {second!r}")
    if not mission:
        raise ValueError("empty mission_id")
    return d


def parse_branch_preserved_evidence(path: Path) -> dict:
    """Structurally verify the external-ACCEPTED-branch-preserved observation.

    The recorded tmp receipt_path is provenance only (the harness fixture is
    gone by design); the structural fields are what the claim stands on."""
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    if not d.get("mission_id"):
        raise ValueError("missing mission_id")
    if d.get("verdict") != "ACCEPTED":
        raise ValueError(f"verdict={d.get('verdict')!r} — only an ACCEPTED "
                         "external branch is preserved by contract")
    if not d.get("candidate_branch"):
        raise ValueError("missing candidate_branch")
    if d.get("branch_exists_after") is not True:
        raise ValueError(f"branch_exists_after={d.get('branch_exists_after')!r} "
                         "— the branch did not survive the cleanup path")
    if not d.get("observed_at"):
        raise ValueError("missing observed_at")
    return d


def parse_long_run_evidence(path: Path) -> dict:
    """Structurally verify the controlled-run observation the receipt stands on.

    Derives ROUTINE_HUMAN_INTERVENTIONS from the recorded intervention ledger
    of the run itself — an authored 0 in the receipt proves nothing (Sol R7)."""
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    if d.get("status") != "PASS":
        raise ValueError(f"status={d.get('status')!r}")
    if d.get("restarted") is not True:
        raise ValueError("restarted is not observed True")
    if not d.get("crashed"):
        raise ValueError("no injected crash recorded — restart proof vacuous")
    ms = d.get("milestones_completed")
    if not isinstance(ms, list) or len(ms) < 3:
        raise ValueError(f"milestones_completed={ms!r} — the controlled run "
                         "requires >=3 proven milestones")
    # L5_factory_v2_rest_3-005: three DISTINCT milestones, not one milestone
    # counted three times (distinct boundary from verify_long_run's own check).
    if any(not isinstance(m, str) or not m.strip() or m != m.strip() for m in ms):
        raise ValueError("milestones_completed must contain nonempty canonical milestone ids")
    if len(set(ms)) < 3:
        raise ValueError(f"milestones_completed={ms!r} — the milestones are not "
                         "DISTINCT: a single settlement listed three times is "
                         "one milestone, not three")
    if not d.get("fault_records"):
        raise ValueError("no fault_records — the kill was never recorded")
    if "routine_human_interventions" not in d:
        raise ValueError("routine_human_interventions absent from the run "
                         "record — an unrecorded count cannot be claimed 0")
    if d["routine_human_interventions"] != 0:
        raise ValueError(f"routine_human_interventions="
                         f"{d['routine_human_interventions']!r} in the run record")
    if d.get("human_intervention_events"):
        raise ValueError("human_intervention_events non-empty")
    return d


def _sha256(path: Path) -> str:
    import hashlib
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _check(label: str, condition: bool, detail: str = "") -> tuple[bool, str]:
    status = "PASS" if condition else "FAIL"
    msg = f"  {status}: {label}"
    if detail:
        msg += f" — {detail}"
    return condition, msg


def verify(receipt_path: Path, evidence_root: "Path | None" = None) -> int:
    print(f"LR5_PREFLIGHT_VERIFIER: reading {receipt_path}")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"FAIL: receipt file not found: {receipt_path}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"FAIL: receipt is not valid JSON: {exc}", file=sys.stderr)
        return 1

    results: list[tuple[bool, str]] = []

    # --- Check 1: schema (v2: observation-carrying receipt from the real harness) ---
    schema = receipt.get("schema", "")
    ok, msg = _check("schema", schema == "lr5-preflight-receipt/v2",
                     f"got {schema!r}, expected 'lr5-preflight-receipt/v2' "
                     "(the authored v1 shape is INVALIDATED by counter-audit)")
    results.append((ok, msg))

    # --- Check 2: integration_sha (non-empty, looks like a git SHA) ---
    integration_sha = receipt.get("integration_sha", "")
    ok, msg = _check(
        "integration_sha",
        bool(integration_sha) and len(integration_sha) >= 7 and
        all(c in "0123456789abcdef" for c in integration_sha.lower()),
        f"sha={integration_sha!r}",
    )
    results.append((ok, msg))

    # --- Check 3: ACCEPTED branch preserved — DERIVED below (3b) from the
    # hash-bound observation; the authored boolean alone stopped counting
    # (Sol R7). ---

    # --- Check 4: capability policy — RECOMPUTED, not read from the receipt ---
    # S017-03: reading selected_engine out of the receipt asks the claimant whether the
    # claimant was right.  Route it live instead: the answer comes from model_router,
    # which the receipt's author does not control.
    try:
        import sys as _sys
        _bin = str(Path(__file__).resolve().parent.parent / "factory" / "bin")
        if _bin not in _sys.path:
            _sys.path.insert(0, _bin)
        import model_router as _mr
        _choice = _mr.route("A", "build", 0, engine_hint="codex")
        _recomputed = _mr.normalize_engine_id(_choice.engine)
        ok, msg = _check(
            "capability_policy route(A,build,hint=codex)→claude [recomputed]",
            _recomputed == "claude",
            f"model_router returned {_recomputed!r}",
        )
    except Exception as exc:
        ok, msg = _check("capability_policy [recomputed]", False,
                         f"could not recompute routing: {exc}")
    results.append((ok, msg))

    # --- Check 4b: the claimed integration SHA must be a real commit ---
    # (always in the verifier's OWN repo, never the evidence root: a staged
    #  evidence bundle is not a git checkout)
    import subprocess as _sp
    _gitrepo = Path(__file__).resolve().parent.parent
    _repo = evidence_root if evidence_root is not None \
        else Path(__file__).resolve().parent.parent
    try:
        _rc = _sp.run(["git", "cat-file", "-e", f"{integration_sha}^{{commit}}"],
                      cwd=_gitrepo, capture_output=True, timeout=30).returncode
    except Exception:
        _rc = 1
    ok, msg = _check("integration_sha is a real commit", _rc == 0,
                     f"{integration_sha[:12]} not found in {_gitrepo.name}")
    results.append((ok, msg))

    # --- Check 4c: event claims need observation evidence the verifier can check ---
    # S017-03 + LR5 contract E3 + Sol R7 @d0bff136: kill/restart, CAS, branch
    # preservation and the unattended-run claim cannot be re-observed after the
    # fact, so authored booleans prove nothing.  Each referenced artifact must
    # exist, parse structurally, AND match the sha256 the receipt bound to it —
    # a bundle edited after the receipt was written fails the hash binding.
    _obs = receipt.get("observations", {}) if isinstance(receipt.get("observations"), dict) else {}
    _parsers = {
        "kill_restart_recovery": parse_kill_restart_evidence,
        "cas_double_dispatch": parse_cas_evidence,
        "branch_preserved": parse_branch_preserved_evidence,
        "controlled_run": parse_long_run_evidence,
    }
    derived: dict = {}
    for _claim, _parser in _parsers.items():
        _ref = _obs.get(_claim) or {}
        _path = _ref.get("evidence_path", "")
        _exists = bool(_path) and (_repo / _path).is_file()
        if _exists:
            _bound = str(_ref.get("evidence_sha256", ""))
            if not _bound:
                ok, msg = _check(f"{_claim} evidence [hash-bound]", False,
                                 f"{_path}: receipt binds no evidence_sha256 — "
                                 "the bundle has no immutable binding")
                results.append((ok, msg))
            else:
                _actual = _sha256(_repo / _path)
                ok, msg = _check(f"{_claim} evidence [hash-bound]",
                                 _actual == _bound,
                                 f"{_path}: bound {_bound[:12]} vs actual "
                                 f"{_actual[:12]}")
                results.append((ok, msg))
            try:
                _parser(_repo / _path)
                derived[_claim] = True
                ok, msg = _check(f"{_claim} observation evidence [parsed]", True,
                                 _path)
            except Exception as _exc:
                derived[_claim] = False
                ok, msg = _check(f"{_claim} observation evidence [parsed]", False,
                                 f"{_path}: {_exc}")
        else:
            derived[_claim] = False
            ok, msg = _check(
                f"{_claim} observation evidence [parsed]",
                False,
                "no checkable observations.%s.evidence_path — an authored PASS is not an "
                "observation (VERIFIER_CANNOT_OBSERVE_RETROACTIVELY)" % _claim,
            )
        results.append((ok, msg))

    # --- Check 5: kill+restart recovery — DERIVED from evidence, receipt must agree ---
    kr = receipt.get("kill_restart_recovery", "")
    _derived_kr = "PASS" if derived.get("kill_restart_recovery") else "UNPROVEN"
    ok, msg = _check("kill_restart_recovery (derived)",
                     derived.get("kill_restart_recovery") is True and kr == _derived_kr,
                     f"receipt says {kr!r}, evidence derives {_derived_kr!r} — "
                     "a claim contradicting (or floating above) its evidence fails")
    results.append((ok, msg))

    # --- Check 6: double dispatch rejected — DERIVED, receipt must agree ---
    dd = receipt.get("cas_double_dispatch", "")
    _derived_dd = "REJECTED" if derived.get("cas_double_dispatch") else "UNPROVEN"
    ok, msg = _check("cas_double_dispatch second attempt (derived)",
                     derived.get("cas_double_dispatch") is True and dd == _derived_dd,
                     f"receipt says {dd!r}, evidence derives {_derived_dd!r}")
    results.append((ok, msg))

    # --- Check 3b: branch preserved — DERIVED, receipt must agree ---
    bp = receipt.get("branch_preserved", False)
    ok, msg = _check("branch_preserved (derived)",
                     derived.get("branch_preserved") is True and bp is True,
                     f"receipt says {bp!r}, evidence derives "
                     f"{bool(derived.get('branch_preserved'))!r}")
    results.append((ok, msg))

    # --- Check 7: ROUTINE_HUMAN_INTERVENTIONS — DERIVED from the run record ---
    rhi = receipt.get("routine_human_interventions", -1)
    ok, msg = _check("routine_human_interventions=0 (derived from run record)",
                     derived.get("controlled_run") is True and rhi == 0,
                     f"receipt says {rhi!r}; the recorded run ledger must show 0 "
                     "and the receipt must agree with it")
    results.append((ok, msg))

    # --- Summary ---
    print()
    all_pass = all(ok for ok, _ in results)
    for ok, msg in results:
        print(msg)
    print()
    if all_pass:
        print("LR5_PREFLIGHT_VERIFIER: ALL CHECKS PASS")
        return 0
    else:
        failed = [msg for ok, msg in results if not ok]
        print(f"LR5_PREFLIGHT_VERIFIER: {len(failed)} check(s) FAILED", file=sys.stderr)
        return 1


if __name__ == "__main__":
    import argparse
    _ap = argparse.ArgumentParser(prog="verify_preflight_receipt")
    _ap.add_argument("receipt_path")
    _ap.add_argument("--evidence-root", default=None,
                     help="resolve relative evidence paths against this root "
                          "(default: the repo this verifier lives in — the "
                          "production product_e2e gate never passes this flag)")
    _a = _ap.parse_args()
    _root = Path(_a.evidence_root) if _a.evidence_root else None
    sys.exit(verify(Path(_a.receipt_path), evidence_root=_root))
