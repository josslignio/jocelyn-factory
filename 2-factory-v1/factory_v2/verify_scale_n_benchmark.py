#!/usr/bin/env python3
"""SCALE_N gate — independent re-observation, never a shape check.

History: this file exited 2 (VERIFIER_NOT_IMPLEMENTED) because every input it
could receive was authored by the party being graded and no producer existed.
The producer it named now exists (`factory_v2/run_scale_n_benchmark.py`,
hermetic: a throwaway N-chain portfolio, real daemon loop, 1w/2w/Nw arms on
the SAME workload; the SCALE_2 baseline it must run against is measured in the
committed `factory_v2/receipts/SCALE_2_BENCHMARK_RECEIPT.json`).

Following the SCALE_2 precedent (verify_scale_benchmark.py, 2026-09-20), this
gate RE-RUNS that producer itself into a temporary directory — a fresh
observation the candidate never saw — and recomputes the pre-declared rule
from the raw per-run files:

    SCALE_N_RECOMMENDED  iff  median throughput strictly increases
                              1w < 2w < Nw
                         AND  isolation perfect on every Nw run
                         AND  reliability (settled + single launch per mission)
                              holds on every Nw run
                         AND  the Nw arm shows >= 1 real engine overlap

Every receipt-side check is one-sided: the gate only ever REFUSES a claim the
evidence does not support (a SCALE_N_RECOMMENDED or linear-scaling claim, a
"perfect isolation" claim, a scale_2_baseline.throughput that is not one of
the numbers actually measured in the canonical SCALE_2 receipt). A
conservative NO_SCALE with a documented sub-linear justification is never
refused. Exit 0 = ACCEPTED, 1 = REFUSED, 2 = could not re-observe (fail
closed).
"""
from __future__ import annotations

import json
import math
import os
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PRODUCER = REPO_ROOT / "factory_v2" / "run_scale_n_benchmark.py"
CANONICAL_SCALE_2 = REPO_ROOT / "factory_v2" / "receipts" / "SCALE_2_BENCHMARK_RECEIPT.json"
VERDICTS = ("SCALE_N_RECOMMENDED", "NO_SCALE")
LINEAR_EFFICIENCY = 0.80


def _canonical_scale_2_numbers() -> set[float] | None:
    """Every throughput number actually measured in the canonical SCALE_2 receipt
    (arm arrays + medians). None if the canonical receipt is unreadable."""
    if not CANONICAL_SCALE_2.is_file():
        return None
    try:
        r = json.loads(CANONICAL_SCALE_2.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    nums: set[float] = set()
    for arm in ("arm_1w_baseline", "arm_2w"):
        block = (r.get("measurements") or {}).get(arm) or {}
        for key in ("throughputs_missions_per_s", "median_throughput"):
            vals = block.get(key)
            if isinstance(vals, list):
                nums.update(float(v) for v in vals if isinstance(v, (int, float)))
            elif isinstance(vals, (int, float)):
                nums.add(float(vals))
    return nums


def _isolation_from_raw(run: dict) -> bool:
    """Recompute the isolation verdict from RAW fields of one run file (never from
    the producer's own summary booleans)."""
    deliverables = run.get("deliverables_ok") or {}
    authority = run.get("authority_files_unchanged") or {}
    return (len(run.get("cross_write_violations") or []) == 0
            and bool(deliverables) and all(deliverables.values())
            and bool(authority) and all(authority.values())
            and bool(run.get("attempt_keys_unique"))
            and int(run.get("torn_ledger_lines", 0) or 0) == 0)


def _reliability_from_raw(run: dict) -> bool:
    """Settled everything AND every workload mission launched exactly once (the
    SCALE_2 defect F1 — re-dispatch in the engine-exit->settlement window — is
    exactly a double launch and must not be laundered into a clean verdict)."""
    if int(run.get("settled", 0) or 0) < int(run.get("expected_settled", 0) or 0):
        return False
    launches = run.get("launches_per_mission") or {}
    missions = set(run.get("workload_milestones") or [])
    if missions and not all(k in launches for k in missions):
        return False
    return all(v == 1 for k, v in launches.items() if k in missions)


def _fresh_observation(repeats: int = 2) -> dict:
    with tempfile.TemporaryDirectory(prefix="scalen_verify_") as td:
        env = {**os.environ, "FACTORY_NO_REAL_ENGINE": "1", "PYTHONPATH": str(REPO_ROOT)}
        r = subprocess.run(
            [sys.executable, str(PRODUCER), "--out", td, "--repeats", str(repeats)],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=1500, env=env)
        if r.returncode != 0:
            raise RuntimeError(f"producer rc={r.returncode}: {(r.stderr or r.stdout)[-400:]}")
        runs: dict[str, list[dict]] = {}
        for p in sorted(Path(td).glob("run_*_*w.json")):
            d = json.loads(p.read_text(encoding="utf-8"))
            arm = p.name.rsplit("_", 1)[1].removesuffix(".json")   # e.g. "1w", "4w"
            runs.setdefault(arm, []).append(d)
        summary = json.loads((Path(td) / "SUMMARY.json").read_text(encoding="utf-8"))
    return {"runs": runs, "summary": summary}


def _recompute(fresh: dict) -> dict:
    runs = fresh["runs"]
    out: dict[str, dict] = {}
    for arm, rs in runs.items():
        if not rs:
            raise RuntimeError(f"no raw runs for arm {arm}")
        thr = [float(r.get("throughput_missions_per_s")
                     or (r.get("settled", 0) / max(r.get("wall_s", 1), 1e-9))) for r in rs]
        out[arm] = {
            "median_throughput": statistics.median(thr),
            "median_wall_s": statistics.median(float(r.get("wall_s", 0)) for r in rs),
            "isolation_all": all(_isolation_from_raw(r) for r in rs),
            "reliability_all": all(_reliability_from_raw(r) for r in rs),
            "overlaps_total": sum(len(r.get("engine_overlaps") or []) for r in rs),
            "n": len(rs),
        }
    nw = [a for a in out if a not in ("1w", "2w")]
    if "1w" not in out or "2w" not in out or not nw:
        raise RuntimeError(f"arms 1w/2w/Nw expected, got {sorted(out)}")
    n_arm = nw[0]
    n = int(n_arm.removesuffix("w"))
    rec = (out[n_arm]["median_throughput"] > out["2w"]["median_throughput"]
           > out["1w"]["median_throughput"]
           and out[n_arm]["isolation_all"] and out[n_arm]["reliability_all"]
           and out[n_arm]["overlaps_total"] >= 1)
    speedup = (out["1w"]["median_wall_s"] / out[n_arm]["median_wall_s"]
               if out[n_arm]["median_wall_s"] else 0.0)
    out["verdict"] = "SCALE_N_RECOMMENDED" if rec else "NO_SCALE"
    out["n"] = n
    out["n_arm"] = n_arm
    out["speedup_nw_over_1w"] = speedup
    out["linear_efficiency_per_worker"] = speedup / n if n else 0.0
    out["linear"] = bool(speedup >= LINEAR_EFFICIENCY * n)
    return out


def _scaling_block(receipt: dict) -> dict:
    """The scaling block may live at the top level or nested under measurements
    (where the producer's SUMMARY puts it); same substance either way."""
    top = receipt.get("scaling") or {}
    nested = (receipt.get("measurements") or {}).get("scaling") or {}
    merged = dict(top)
    for k, v in nested.items():
        merged.setdefault(k, v)
    return merged


def static_refusal(receipt: dict, canonical_numbers: set[float] | None
                   ) -> tuple[int, str] | None:
    """Cheap one-sided refusals decidable from the receipt alone (no producer
    run): a forged receipt is refused in < 1 s, before any observation cost."""
    decision = receipt.get("decision") or {}
    claimed = str(decision.get("verdict") if isinstance(decision, dict)
                  else receipt.get("verdict") or "")
    if claimed not in VERDICTS:
        return 1, (f"REFUSED — decision.verdict {claimed!r} is not in the closed "
                   f"vocabulary {VERDICTS}")
    baseline = receipt.get("scale_2_baseline") or {}
    base_thr = baseline.get("throughput")
    if not isinstance(base_thr, (int, float)) or not math.isfinite(float(base_thr)):
        return 1, (f"REFUSED — scale_2_baseline.throughput {base_thr!r} is not a "
                   f"finite measured number")
    if canonical_numbers is not None and float(base_thr) not in canonical_numbers:
        return 1, (f"REFUSED — scale_2_baseline.throughput {base_thr} is not among "
                   f"the numbers actually measured in the canonical SCALE_2 receipt "
                   f"({CANONICAL_SCALE_2.name})")
    scaling = _scaling_block(receipt)
    justification = str(scaling.get("sub_linear_scale_justification")
                        or receipt.get("sub_linear_scale_justification") or "").strip()
    linear_claim = bool(scaling.get("linear"))
    if not linear_claim and not justification:
        return 1, ("REFUSED — acceptance requires linear throughput scaling OR a "
                   "documented sub_linear_scale_justification; the receipt has "
                   "neither")
    d019 = receipt.get("d019_elastic_fanout_and_knowledge_reuse") or {}
    if str(d019.get("verdict") or "").startswith("DEMONSTRATED"):
        drill = d019.get("drill_file")
        if not drill or not (REPO_ROOT / drill).is_file():
            return 1, (f"REFUSED — receipt claims the D019 drill is DEMONSTRATED but "
                       f"its raw drill file {drill!r} is absent")
        try:
            raw = json.loads((REPO_ROOT / drill).read_text(encoding="utf-8"))
            block = raw.get("d019") or {}
        except (OSError, ValueError) as exc:
            return 1, f"REFUSED — D019 drill file unreadable: {exc}"
        needed = ("knowledge_sha_match", "fan_out_event", "g1_g2_overlap",
                  "older_product_tree_unchanged")
        missing = [k for k in needed if block.get(k) is not True]
        if missing:
            return 1, (f"REFUSED — D019 drill file lacks true {missing} "
                       f"(claims without raw evidence)")
    return None


def evaluate_receipt_against(receipt: dict, fresh: dict,
                             canonical_numbers: set[float] | None) -> tuple[int, str]:
    """Pure decision core (unit-testable): one-sided refusals only."""
    static = static_refusal(receipt, canonical_numbers)
    if static is not None:
        return static
    decision = receipt.get("decision") or {}
    claimed = str(decision.get("verdict") if isinstance(decision, dict)
                  else receipt.get("verdict") or "")
    scaling = _scaling_block(receipt)
    justification = str(scaling.get("sub_linear_scale_justification")
                        or receipt.get("sub_linear_scale_justification") or "").strip()
    linear_claim = bool(scaling.get("linear"))
    n_arm = fresh["n_arm"]
    if linear_claim and not fresh["linear"]:
        return 1, (f"REFUSED — receipt claims linear scaling but the fresh "
                   f"observation measures speedup {fresh['speedup_nw_over_1w']:.2f} "
                   f"at N={fresh['n']} (efficiency "
                   f"{fresh['linear_efficiency_per_worker']:.2f} < "
                   f"{LINEAR_EFFICIENCY})")
    iso_claim = str((receipt.get("context_isolation") or {}).get("verdict") or "")
    if iso_claim.startswith("PERFECT") and not fresh[n_arm]["isolation_all"]:
        return 1, ("REFUSED — receipt claims perfect context isolation at N workers "
                   "but the fresh observation measured an isolation violation")
    if claimed == "SCALE_N_RECOMMENDED" and fresh["verdict"] != "SCALE_N_RECOMMENDED":
        return 1, (f"REFUSED — receipt claims SCALE_N_RECOMMENDED, fresh observation "
                   f"says {fresh['verdict']!r} "
                   f"(1w={fresh['1w']['median_throughput']:.4f}/s "
                   f"2w={fresh['2w']['median_throughput']:.4f}/s "
                   f"{n_arm}={fresh[n_arm]['median_throughput']:.4f}/s "
                   f"isolation_N={fresh[n_arm]['isolation_all']} "
                   f"reliability_N={fresh[n_arm]['reliability_all']})")
    return 0, (f"ACCEPTED — fresh observation {fresh['verdict']} "
               f"1w={fresh['1w']['median_throughput']:.4f}/s "
               f"2w={fresh['2w']['median_throughput']:.4f}/s "
               f"{n_arm}={fresh[n_arm]['median_throughput']:.4f}/s "
               f"isolation_N={fresh[n_arm]['isolation_all']} "
               f"reliability_N={fresh[n_arm]['reliability_all']} "
               f"linear(fresh)={fresh['linear']}")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or not Path(argv[0]).is_file():
        print("SCALE_N: receipt path missing", file=sys.stderr)
        return 1
    try:
        receipt = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"SCALE_N: receipt unreadable: {exc}", file=sys.stderr)
        return 1
    if not PRODUCER.is_file():
        print("SCALE_N: VERIFIER_NOT_IMPLEMENTED — producer "
              "factory_v2/run_scale_n_benchmark.py absent", file=sys.stderr)
        return 2
    canonical = _canonical_scale_2_numbers()
    if canonical is None:
        print("SCALE_N: cannot re-observe — canonical SCALE_2 receipt unreadable "
              f"({CANONICAL_SCALE_2})", file=sys.stderr)
        return 2
    # Fast one-sided refusals FIRST: a forged receipt never pays for an
    # observation it could not survive anyway.
    static = static_refusal(receipt, canonical)
    if static is not None:
        rc, message = static
        print(f"SCALE_N: {message}", file=sys.stderr)
        return rc
    try:
        fresh = _recompute(_fresh_observation())
    except Exception as exc:                                    # noqa: BLE001
        print(f"SCALE_N: fresh observation failed (fail closed): {exc}", file=sys.stderr)
        return 2
    rc, message = evaluate_receipt_against(receipt, fresh, canonical)
    print(f"SCALE_N: {message}", file=sys.stderr if rc else sys.stdout)
    return rc


if __name__ == "__main__":
    sys.exit(main())
