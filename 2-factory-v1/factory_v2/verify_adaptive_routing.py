#!/usr/bin/env python3
"""ADAPTIVE_ROUTING gate — routing_stats/routing_summary.json.

Replaces the 2026-09-12 VERIFIER_NOT_IMPLEMENTED stub. model_router.route() is
still a frozen seed table (no code in this repo changes engine choice based on
RunRecords automatically) — that fact does not change here. What changes is
that the verifier now recomputes every claim the producer could otherwise
invent, against the two real, pre-existing observation sources named in the
stub's own MISSING note:

  * factory_v2.runrecords.engine_stats() over the production ledger
    (factory_v2/receipts/runrecords.jsonl) — never a claimant-chosen copy;
  * factory.bin.model_router.engine_capable()/route() — the live
    ActorCapabilityPolicy, called directly, never read out of the receipt.

Clauses covered (see VERIFIERS_SPEC.md for the acceptance-clause mapping):

  1. routing_stats/routing_summary.json binds to the real runrecords ledger
     path (not an arbitrary file)
  2. each session's recommended_engine is RECOMPUTED as the argmax acceptance
     rate among capable engines with real attempts in engine_stats() — an
     authored recommendation that does not match the recomputation fails
  3. at least two sessions recommend different engines for the same
     (spec_level, task_kind) — evidence of a real change, not a static claim
  4. the fallback pair is recomputed against the live ActorCapabilityPolicy
     (both engines must be genuinely capable; an "unavailable" capable engine
     is a runtime condition, not an incapability)
  5. PREDICTION_CALIBRATION_MEASURED: n_predictions/n_correct are recomputed
     from real PREDICTION/OBSERVATION phase pairs in the same ledger, not
     read from the receipt as a boolean

"aucune règle hardcodée ne prime sur les RunRecords" is NOT machine-checked
here (it would require static analysis of model_router.py) — see
VERIFIERS_SPEC.md "non machine-verifiable" section.

Exit 0: all recomputable clauses pass. Exit 1: UNMET, typed reason on stderr.
Exit 2 is reserved for VERIFIER_NOT_IMPLEMENTED and must not occur here.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

CONTROL_ROOT = Path(__file__).resolve().parent.parent
SUCCESS_OUTCOMES = {"PASS", "ACCEPTED", "COMPLETE"}


def fail(msg: str) -> int:
    print(f"UNMET: {msg}", file=sys.stderr)
    return 1


def _load_router(bin_dir: Path):
    import sys as _sys
    _bin = str(bin_dir)
    if _bin not in _sys.path:
        _sys.path.insert(0, _bin)
    import model_router as _mr
    return _mr


def _load_runrecords(control_root: Path):
    _sys_path = str(control_root)
    if _sys_path not in sys.path:
        sys.path.insert(0, _sys_path)
    from factory_v2 import runrecords as _rr
    return _rr


def _best_engine(stats: dict, mr, spec_level: str, task_kind: str) -> str | None:
    candidates = []
    for engine, bucket in stats.items():
        if not bucket.get("attempts"):
            continue
        if not mr.engine_capable(engine, spec_level, task_kind):
            continue
        candidates.append((bucket.get("acceptance_rate", 0.0),
                           bucket.get("validated_deliveries", 0), engine))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][2]


def _calibration(rr) -> tuple[int, int]:
    phases: dict = {}
    for rec in rr.read_attempt_phases():
        aid = rec.get("attempt_id")
        if not aid:
            continue
        phases.setdefault(str(aid), {})[rec["phase"]] = rec
    n_predictions = 0
    n_correct = 0
    for pair in phases.values():
        pred = pair.get("PREDICTION")
        obs = pair.get("OBSERVATION")
        if not pred or not obs:
            continue
        predicted = (pred.get("prediction") or {}).get("outcome")
        observed = (obs.get("observation") or {}).get("outcome")
        # L5_factory_v2_rest_2-019: an absent/unknown outcome on either side
        # is NOT a prediction that came true. Only pairs whose two outcomes
        # are typed non-empty strings are calibrated; the rest are skipped
        # rather than folded into False == False.
        if not (isinstance(predicted, str) and predicted.strip()
                and isinstance(observed, str) and observed.strip()):
            continue
        n_predictions += 1
        if (predicted in SUCCESS_OUTCOMES) == (observed in SUCCESS_OUTCOMES):
            n_correct += 1
    return n_predictions, n_correct


def verify(stats_dir: Path, control_root: Path | None = None,
          verify_evidence_based: bool = True) -> int:
    control_root = control_root or CONTROL_ROOT
    if not verify_evidence_based:
        return fail("--verify-evidence-based is the only supported mode; "
                    "a shape-only check would trust authored claims")
    if not stats_dir.is_dir():
        return fail(f"routing_stats directory not found: {stats_dir}")
    summary_path = stats_dir / "routing_summary.json"
    if not summary_path.is_file():
        return fail(f"routing_summary.json not found in {stats_dir}")
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return fail(f"routing_summary.json is not valid JSON: {exc}")

    if summary.get("schema") != "routing-stats-summary/v1":
        return fail(f"schema={summary.get('schema')!r}, expected 'routing-stats-summary/v1'")

    try:
        mr = _load_router(control_root / "factory" / "bin")
        rr = _load_runrecords(control_root)
    except Exception as exc:
        return fail(f"could not import live router/runrecords modules: {exc}")

    real_ledger = rr._runrecords_path()
    claimed_ledger = str(summary.get("runrecords_path", ""))
    try:
        real_resolved = real_ledger.resolve()
    except Exception:
        real_resolved = real_ledger
    claimed_path = Path(claimed_ledger)
    if not claimed_path.is_absolute():
        claimed_path = control_root / claimed_path
    if claimed_path.resolve() != real_resolved:
        return fail(f"runrecords_path {claimed_ledger!r} does not resolve to the real "
                    f"production ledger {real_resolved}")

    sessions = summary.get("sessions")
    if not isinstance(sessions, list) or len(sessions) < 2:
        return fail("need >=2 sessions to demonstrate a change in engine choice")

    by_task_key: dict = {}
    for i, session in enumerate(sessions):
        spec_level = session.get("spec_level")
        task_kind = session.get("task_type")
        claimed_engine = session.get("recommended_engine")
        if not (spec_level and task_kind and claimed_engine):
            return fail(f"session[{i}] missing spec_level/task_type/recommended_engine")
        stats = rr.engine_stats(project=session.get("project"))
        recomputed = _best_engine(stats, mr, spec_level, task_kind)
        if recomputed is None:
            return fail(f"session[{i}]: no capable engine with real attempts in "
                        f"engine_stats for (spec_level={spec_level!r}, "
                        f"task_type={task_kind!r}, project={session.get('project')!r}) — "
                        "cannot recompute a recommendation")
        if recomputed != claimed_engine:
            return fail(f"session[{i}]: recommended_engine={claimed_engine!r} does not match "
                        f"recomputed best engine {recomputed!r} from real engine_stats")
        by_task_key.setdefault((spec_level, task_kind), set()).add(claimed_engine)

    if not any(len(v) >= 2 for v in by_task_key.values()):
        return fail("no (spec_level, task_type) pair shows a different recommended_engine "
                    "across sessions — no evidence of an engine choice CHANGE")

    fb = summary.get("fallback_check") or {}
    fb_spec = fb.get("spec_level")
    fb_task = fb.get("task_type")
    unavailable = fb.get("unavailable_engine")
    expected_fallback = fb.get("expected_fallback_engine")
    if not (fb_spec and fb_task and unavailable and expected_fallback):
        return fail("fallback_check missing spec_level/task_type/unavailable_engine/"
                    "expected_fallback_engine")
    if expected_fallback == unavailable:
        return fail("fallback_check: expected_fallback_engine == unavailable_engine")
    if not mr.engine_capable(unavailable, fb_spec, fb_task):
        return fail(f"fallback_check: {unavailable!r} is not ActorCapabilityPolicy-capable "
                    f"for ({fb_spec!r}, {fb_task!r}) — it cannot be a meaningful "
                    "'preferred but unavailable' engine")
    if not mr.engine_capable(expected_fallback, fb_spec, fb_task):
        return fail(f"fallback_check: expected_fallback_engine {expected_fallback!r} is not "
                    f"ActorCapabilityPolicy-capable for ({fb_spec!r}, {fb_task!r})")

    calib = summary.get("prediction_calibration") or {}
    claimed_n = calib.get("n_predictions")
    claimed_correct = calib.get("n_correct")
    recomputed_n, recomputed_correct = _calibration(rr)
    if recomputed_n < 1:
        return fail("no real PREDICTION/OBSERVATION pairs in the ledger — "
                    "PREDICTION_CALIBRATION_MEASURED cannot be 1")
    if claimed_n != recomputed_n or claimed_correct != recomputed_correct:
        return fail(f"prediction_calibration mismatch: receipt claims "
                    f"n_predictions={claimed_n}, n_correct={claimed_correct}; recomputed "
                    f"n_predictions={recomputed_n}, n_correct={recomputed_correct}")

    print(f"ADAPTIVE_ROUTING_VERIFIER: PASS — {len(sessions)} sessions, "
          f"engine change observed, calibration {recomputed_correct}/{recomputed_n}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="verify_adaptive_routing")
    ap.add_argument("stats_dir")
    ap.add_argument("--verify-evidence-based", action="store_true", default=True)
    ap.add_argument("--repo-root", default=None,
                     help="control root to import model_router/runrecords from "
                          "(default: this verifier's own repo)")
    args = ap.parse_args(argv)
    control_root = Path(args.repo_root) if args.repo_root else None
    return verify(Path(args.stats_dir), control_root=control_root,
                  verify_evidence_based=args.verify_evidence_based)


if __name__ == "__main__":
    sys.exit(main())
