#!/usr/bin/env python3
"""benchmark — F-NEXT-07 Factory vs solo benchmark.

Measures factory performance (GLM-first + oracle + repair) against a solo
Claude baseline (no preflight, no oracle, single pass).

Benchmark protocol (per-task):
  - wall_s          : wall-clock time from dispatch start to verdict
  - repairs_count   : number of repair rounds executed
  - verdict         : ACCEPTED | BLOCKED | MOTOR_UNAVAILABLE
  - cost_usd_est    : estimated cost from dispatch receipt stages

Two modes are compared:
  factory  — dispatcher_v3 (preflight → T0 GLM → test → T2 repair → T3 Claude)
  solo     — Claude only, no test oracle, single pass (unconstrained)

A gain is reported only when NUMERICALLY MEASURED — no self-certification.
A gain < 0 (factory slower) is reported honestly; no fabrication.

Usage:
    python3 factory/bin/benchmark.py --tasks tasks.json \\
        --out bench_results.json --mode factory
    python3 factory/bin/benchmark.py --tasks tasks.json \\
        --out bench_results.json --mode solo
    python3 factory/bin/benchmark.py --compare factory_results.json solo_results.json
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
# Control-repo root, so `factory_v2.telemetry` is importable from this script's
# own process. Derived from __file__, never from a cwd or an absolute path, so
# the benchmark keeps working wherever the repo is checked out.
sys.path.insert(0, str(HERE.parent.parent))

# L6_bin_lib_products_1-001: this import MUST come after the sys.path inserts
# above — importing factory_v2 before them made even --help crash with
# ModuleNotFoundError whenever PYTHONPATH did not already include the repo root.
from factory_v2._io import _now  # noqa: E402


SCHEMA = "factory-benchmark/v1"
MODES = ("factory", "solo")


# ---------------------------------------------------------------------------
# task spec
# ---------------------------------------------------------------------------

def _load_tasks(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    tasks = data if isinstance(data, list) else data.get("tasks", [])
    if not tasks:
        raise ValueError("tasks file is empty or missing 'tasks' key")
    return tasks


# ---------------------------------------------------------------------------
# per-task runners
# ---------------------------------------------------------------------------



def _run_factory_task(
    task: dict,
    out_dir: Path,
    budget_s: int = 3600,
) -> dict:
    """Run a task through dispatcher_v3 (factory mode)."""
    mid = task["id"]
    repo = Path(task["repo"])
    task_file = task["task_file"]
    test_cmd = task.get("test_cmd")
    receipt_path = out_dir / f"factory_{mid}.json"

    dispatcher = HERE / "dispatcher_v3.py"
    cmd = [
        sys.executable, str(dispatcher),
        "--repo", str(repo),
        "--task-file", task_file,
        "--mission-id", mid,
        "--budget-s", str(budget_s),
        "--out", str(receipt_path),
        "--skip-providers",
    ]
    if test_cmd:
        cmd += ["--test-cmd", test_cmd]

    t0 = time.monotonic()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=budget_s + 60)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        rc = -1
    wall_s = round(time.monotonic() - t0, 2)

    receipt: dict = {}
    if receipt_path.is_file():
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    stages = receipt.get("stages", [])
    repairs = sum(1 for s in stages if "repair" in s.get("step", ""))
    verdict = receipt.get("verdict", "BLOCKED" if rc != 0 else "UNKNOWN")

    return {
        "task_id": mid,
        "mode": "factory",
        "verdict": verdict,
        "wall_s": wall_s,
        "repairs_count": repairs,
        "stages_count": len(stages),
        "elapsed_step_s": receipt.get("elapsed_step_s"),
        "receipt_path": str(receipt_path),
    }


def _run_solo_task(
    task: dict,
    out_dir: Path,
    budget_s: int = 3600,
) -> dict:
    """Run a task in solo Claude mode (no test oracle, single pass)."""
    mid = task["id"]
    repo = Path(task["repo"])
    task_file_path = Path(task["task_file"])
    if not task_file_path.is_absolute():
        task_file_path = repo / task_file_path

    try:
        task_text = task_file_path.read_text(encoding="utf-8")
    except OSError:
        task_text = task.get("statement", "")

    prompt = (
        f"You are the SOLO builder. No test oracle. Single pass.\n\n"
        f"Repo: {repo}\n\n"
        f"Task:\n{task_text}"
    )

    cmd = [
        "claude", "-p", "--output-format", "json",
        "--dangerously-skip-permissions",
        prompt,
    ]

    from datetime import datetime, timezone
    started_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=budget_s,
            cwd=str(repo),
        )
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        rc = -1
    wall_s = round(time.monotonic() - t0, 2)

    # The solo arm is a real Claude launch and spends real quota, so it is
    # measured and its row is PERSISTED — the benchmark's own out_dir is the
    # ledger root, because a benchmark arm belongs to the benchmark, not to a
    # product project. Without this the coverage inventory would call this path
    # instrumented on the strength of its argv alone, while nothing downstream
    # ever wrote a row: a parser is not a consumer.
    telemetry_rows = 0
    try:
        from factory_v2.telemetry import ledger as _tel_ledger
        from factory_v2.telemetry import observe as _tel_observe
        # The launch identity MUST vary per task. Without `attempt_key` and a
        # start instant every solo task hashed to the same observation_key, so
        # a two-task benchmark wrote two rows that folded into one and task 1's
        # spend silently vanished. The task id and the µs start instant are both
        # already known here; there was never a reason not to pass them.
        _obs = _tel_observe.observe_launch(
            argv=cmd, stdout=(proc.stdout if rc >= 0 else ""),
            rc=rc, wall_time_s=wall_s, engine="claude",
            model_requested="", role="benchmark_solo",
            lineage=_tel_observe.PRIMARY, project="benchmark",
            mission=mid, attempt_key=f"benchmark:solo:{mid}",
            started_at=started_at, task_type="solo_build",
            exit_class="SUCCESS" if rc == 0 else
                       ("TIMEOUT" if rc == -1 else "SEMANTIC_FAILURE"))
        _tel_ledger.append(_obs, out_dir)
        telemetry_rows = 1
    except Exception:
        # A benchmark must still produce its comparison when the meter breaks.
        telemetry_rows = 0

    solo_receipt_path = out_dir / f"solo_{mid}.json"
    solo_receipt = {
        "task_id": mid,
        "mode": "solo",
        "rc": rc,
        "wall_s": wall_s,
        "telemetry_rows_written": telemetry_rows,
        "stdout_tail": (proc.stdout if rc >= 0 else "TIMEOUT")[-2000:],
    }
    solo_receipt_path.write_text(
        json.dumps(solo_receipt, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    verdict = "ACCEPTED" if rc == 0 else "BLOCKED"

    return {
        "task_id": mid,
        "mode": "solo",
        "verdict": verdict,
        "wall_s": wall_s,
        "repairs_count": 0,
        "stages_count": 1,
        "elapsed_step_s": wall_s,
        "receipt_path": str(solo_receipt_path),
    }


# ---------------------------------------------------------------------------
# comparison
# ---------------------------------------------------------------------------

def compare(factory_results: list[dict], solo_results: list[dict]) -> dict:
    """Compare factory vs solo per-task and compute aggregate gains."""
    factory_map = {r["task_id"]: r for r in factory_results}
    solo_map = {r["task_id"]: r for r in solo_results}
    all_ids = sorted(set(factory_map) | set(solo_map))

    per_task = []
    for tid in all_ids:
        f = factory_map.get(tid)
        s = solo_map.get(tid)
        if not f or not s:
            per_task.append({
                "task_id": tid,
                "note": "missing in one mode",
                "factory": f,
                "solo": s,
            })
            continue
        wall_delta = s["wall_s"] - f["wall_s"]
        per_task.append({
            "task_id": tid,
            "factory_verdict": f["verdict"],
            "solo_verdict": s["verdict"],
            "wall_s_factory": f["wall_s"],
            "wall_s_solo": s["wall_s"],
            "wall_s_delta": round(wall_delta, 2),
            "factory_repairs": f["repairs_count"],
            "factory_stages": f["stages_count"],
        })

    accepted_factory = sum(1 for r in factory_results if r["verdict"] == "ACCEPTED")
    accepted_solo = sum(1 for r in solo_results if r["verdict"] == "ACCEPTED")
    n = len(per_task)

    def _mean(vals: list[float]) -> Optional[float]:
        return round(sum(vals) / len(vals), 2) if vals else None

    wall_deltas = [t["wall_s_delta"] for t in per_task if "wall_s_delta" in t]

    return {
        "schema": SCHEMA,
        "tasks_compared": n,
        "accepted_factory": accepted_factory,
        "accepted_solo": accepted_solo,
        "mean_wall_delta_s": _mean(wall_deltas),
        "note": ("positive mean_wall_delta = factory faster than solo; "
                 "negative = factory slower"),
        "per_task": per_task,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="benchmark — F-NEXT-07 Factory vs solo")
    sub = ap.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="run benchmark tasks in one mode")
    run.add_argument("--tasks", required=True, help="JSON tasks file")
    run.add_argument("--out", required=True, help="output results JSON")
    run.add_argument("--out-dir", default=None, help="directory for per-task receipts")
    run.add_argument("--mode", required=True, choices=MODES)
    run.add_argument("--budget-s", type=int, default=3600)

    cmp = sub.add_parser("compare", help="compare factory vs solo results")
    cmp.add_argument("factory_results", help="factory results JSON")
    cmp.add_argument("solo_results", help="solo results JSON")
    cmp.add_argument("--out", default=None, help="write comparison JSON")

    args = ap.parse_args(argv)

    if args.cmd == "run":
        tasks = _load_tasks(Path(args.tasks))
        out_path = Path(args.out)
        out_dir = Path(args.out_dir) if args.out_dir else out_path.parent
        out_dir.mkdir(parents=True, exist_ok=True)

        runner = _run_factory_task if args.mode == "factory" else _run_solo_task
        results = []
        for task in tasks:
            print(f"[benchmark] {args.mode} {task['id']} ...", flush=True)
            result = runner(task, out_dir, args.budget_s)
            results.append(result)
            print(f"  → {result['verdict']} ({result['wall_s']}s)")

        report = {
            "schema": SCHEMA,
            "mode": args.mode,
            "tasks_run": len(results),
            "results": results,
        }
        out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        accepted = sum(1 for r in results if r["verdict"] == "ACCEPTED")
        print(f"[benchmark] {accepted}/{len(results)} ACCEPTED")
        return 0

    if args.cmd == "compare":
        factory_report = json.loads(Path(args.factory_results).read_text(encoding="utf-8"))
        solo_report = json.loads(Path(args.solo_results).read_text(encoding="utf-8"))
        cmp_result = compare(
            factory_report.get("results", []),
            solo_report.get("results", []),
        )
        output = json.dumps(cmp_result, indent=2, ensure_ascii=False)
        if args.out:
            Path(args.out).write_text(output, encoding="utf-8")
        else:
            print(output)
        delta = cmp_result.get("mean_wall_delta_s")
        if delta is not None:
            direction = "factory faster" if delta > 0 else "solo faster"
            print(f"mean_wall_delta_s={delta}s ({direction})")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
