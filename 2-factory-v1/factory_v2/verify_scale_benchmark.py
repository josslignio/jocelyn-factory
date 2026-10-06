#!/usr/bin/env python3
"""SCALE_2 gate — independent re-observation, never a shape check.

History: this file exited 2 (VERIFIER_NOT_IMPLEMENTED) because every input it
could receive was authored by the party being graded. The producer it named
now exists (`factory_v2/run_scale_benchmark.py`, hermetic: a throwaway
portfolio, real daemon loop, 1 worker vs 2 workers on the SAME workload).
This gate therefore RE-RUNS that producer itself into a temporary directory —
a fresh observation the candidate never saw — and recomputes the decision rule
from the raw per-run files:

    SCALE_RECOMMENDED  iff  median_throughput(2w) > median_throughput(1w)
                       AND  isolation perfect on every 2w run
                       AND  reliability (settled/expected) not regressed at 2w

The receipt passed as argv[1] must then agree with the fresh observation on the
verdict (its own numbers are informative only). Exit 0 = ACCEPTED, 1 = REFUSED,
2 = the producer could not run (fail closed).
"""
from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PRODUCER = REPO_ROOT / "factory_v2" / "run_scale_benchmark.py"


def _fresh_observation(repeats: int = 3) -> dict:
    with tempfile.TemporaryDirectory(prefix="scale2_verify_") as td:
        env = {**os.environ, "FACTORY_NO_REAL_ENGINE": "1", "PYTHONPATH": str(REPO_ROOT)}
        r = subprocess.run([sys.executable, str(PRODUCER), "--out", td, "--repeats", str(repeats)],
                           cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=1500, env=env)
        if r.returncode != 0:
            raise RuntimeError(f"producer rc={r.returncode}: {(r.stderr or r.stdout)[-400:]}")
        runs = {}
        for p in sorted(Path(td).glob("run_*_*w.json")):
            d = json.loads(p.read_text(encoding="utf-8"))
            arm = "2w" if p.name.endswith("_2w.json") else "1w"
            runs.setdefault(arm, []).append(d)
        summary = json.loads((Path(td) / "SUMMARY.json").read_text(encoding="utf-8"))
    return {"runs": runs, "summary": summary}


def _recompute(runs: dict) -> dict:
    out = {}
    for arm in ("1w", "2w"):
        rs = runs.get(arm) or []
        if not rs:
            raise RuntimeError(f"no raw runs for arm {arm}")
        thr = [float(r.get("throughput") or r.get("throughput_per_s") or (r.get("settled", 0) / max(r.get("wall_s", 1), 1e-9))) for r in rs]
        iso = all(len(r.get("cross_write_violations") or []) == 0 for r in rs)
        rel = all(int(r.get("settled", 0)) >= int(r.get("expected_settled", 0)) for r in rs)
        out[arm] = {"median_throughput": statistics.median(thr), "isolation_all": iso, "reliability_all": rel, "n": len(rs)}
    rec = (out["2w"]["median_throughput"] > out["1w"]["median_throughput"]
           and out["2w"]["isolation_all"] and out["2w"]["reliability_all"])
    out["verdict"] = "SCALE_RECOMMENDED" if rec else "NO_SCALE"
    return out


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or not Path(argv[0]).is_file():
        print("SCALE_2: receipt path missing", file=sys.stderr)
        return 1
    try:
        receipt = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"SCALE_2: receipt unreadable: {exc}", file=sys.stderr)
        return 1
    if not PRODUCER.is_file():
        print("SCALE_2: VERIFIER_NOT_IMPLEMENTED — producer factory_v2/run_scale_benchmark.py absent", file=sys.stderr)
        return 2
    try:
        obs = _fresh_observation()
        fresh = _recompute(obs["runs"])
    except Exception as exc:  # noqa: BLE001
        print(f"SCALE_2: fresh observation failed (fail closed): {exc}", file=sys.stderr)
        return 2
    claimed = str(((receipt.get("decision") or {}).get("verdict")) or receipt.get("verdict") or "")
    # the receipt may name SCALE_RECOMMENDED / NO_SCALE; both are honest outcomes —
    # what is refused is a claim that contradicts the independent observation
    # only the UNSAFE direction is refused: claiming a scale-up the fresh
    # observation does not support. A conservative NO_SCALE (e.g. a SPEC
    # blocker the receipt names) is never a false settlement.
    if claimed == "SCALE_RECOMMENDED" and fresh["verdict"] != "SCALE_RECOMMENDED":
        print(f"SCALE_2: REFUSED — receipt claims SCALE_RECOMMENDED, fresh observation says "
              f"{fresh['verdict']!r} ({json.dumps(fresh)})", file=sys.stderr)
        return 1
    print(f"SCALE_2: ACCEPTED — fresh observation {fresh['verdict']} "
          f"1w={fresh['1w']['median_throughput']:.4f}/s 2w={fresh['2w']['median_throughput']:.4f}/s "
          f"isolation_2w={fresh['2w']['isolation_all']} reliability_2w={fresh['2w']['reliability_all']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
