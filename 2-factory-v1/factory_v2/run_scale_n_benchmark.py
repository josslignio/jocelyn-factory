#!/usr/bin/env python3
"""SCALE_N benchmark producer — 1w/2w/Nw arms (N >= 3), SAME workload, real daemon.

The pinned gate (factory_v2/verify_scale_n_benchmark.py) names this mission's producer
as owed: "a measured SCALE_2 baseline throughput and an N-worker run against it". The
SCALE_2 baseline exists and is measured (factory_v2/receipts/SCALE_2_BENCHMARK_RECEIPT.
json: 1w median 0.15702, 2w median 0.27035 missions/s); this script is the N-worker run
against it.

Same hermetic discipline as the SCALE_2 producer (factory_v2/run_scale_benchmark.py,
which this file deliberately does NOT modify — its own gate re-runs it): the REAL
supervisor path factoryd.run_daemon_loop in portfolio mode over a deterministically
generated throwaway portfolio; missions are local_command executors with real sleeps
and real subprocesses, so every scaled cost is real scheduling machinery (project
locks, worker leases, heartbeats, attempt ledgers, settlement, run windows). No AI
quality is measured and none is claimed.

Workload SCALE_N_W1 exposes N independent chains (the SCALE_2_W1 workload has only two,
so N>2 workers could never be distinguished from 2): projects p1..pN, each a 2-mission
sequential chain. p1: P1_BUILD (writes knowledge/lesson.json) then P1_RESEARCH (reads a
STATIC pre-planted input). p2..pN: Pi_BUILD then Pi_REUSE, which reads p(i-1)'s lesson
READ-ONLY and embeds its sha256 — cross-product knowledge reuse, zero authority
sharing. The dependency graph is ACYCLIC and 1w-safe: every REUSE source is a BUILD
that can settle earlier at 1 worker (a cycle would deadlock the 1-worker arm).

Arms differ ONLY by FACTORY_MAX_WORKERS (1 / 2 / N). Repeats are interleaved. The run
STOPS at the first isolation failure (bounded scope: "a corrupted candidate ancestry
ends the run") — remaining runs are skipped and the SUMMARY records the stop.

Pre-declared decision rule (computed, never hand-set): SCALE_N_RECOMMENDED iff
median throughput strictly increases with workers (1w < 2w < Nw) AND every isolation
check passes on every Nw run AND reliability counters at Nw are no worse than at 2w
on this same workload AND the Nw arm shows >= 1 real engine overlap. Anything else is
reported as NO_SCALE. Sub-linear scaling is an expected, honest outcome and is
documented via measured numbers, never fabricated away.

Raw observations land in --out (one JSON per run + SUMMARY.json + d019_drill.json).
Nothing here edits the repo; git status is snapshotted before/after.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# Pure helpers reused from the SCALE_2 producer (it stays byte-identical: its own
# gate re-runs it; only leaf utilities with no SCALE_2-specific state are imported).
from factory_v2.run_scale_benchmark import (  # noqa: E402
    _init_project,
    _isolation,
    _launch_count,
    _launch_windows,
    _milestone,
    _mission_complete,
    _reliability,
    _roadmap,
    _snapshot_tree,
    build_d019_workload,
    run_daemon_arm,
)

WORKLOAD_ID = "SCALE_N_W1"
N_DEFAULT = 4
POLL_INTERVAL_S = 0.05
BACKOFF_CEILING_S = 1.0
RUN_TIMEOUT_S = 360.0
REPEATS_DEFAULT = 3
LESSON = "n workers only pay off with n independent chains"
#: pre-declared linear-scaling threshold: linear iff speedup(Nw over 1w) >= 0.80 * N
#: (80% per-worker efficiency). Below it, the receipt must carry a measured
#: sub_linear_scale_justification instead — a fabricated linear number is a defect.
LINEAR_EFFICIENCY = 0.80

BENCH_WORKER_N = r'''#!/usr/bin/env python3
"""Hermetic benchmark mission worker (SCALE_N). One process per mission launch.

Usage: bench_worker.py <KIND> <SLEEP_S> <OUT_RELPATH> [READ_RELPATH ...]

KIND is <Pn>_<ROLE> with ROLE in {BUILD, RESEARCH, REUSE} — generic, no per-project
branching. Writes, under the calling project dir:
  .bench/<KIND>.launch.txt  — one line per LAUNCH (pid,t0) appended O_APPEND
  .bench/<KIND>.<pid>.start/.end — epoch floats bracketing the sleep+work
  <OUT_RELPATH>             — the deterministic deliverable
Reads are READ-ONLY (cross-product knowledge reuse never writes back).
"""
import hashlib, json, os, sys, time
from pathlib import Path

kind, sleep_s, out = sys.argv[1], float(sys.argv[2]), sys.argv[3]
reads = sys.argv[4:]
role = kind.rsplit("_", 1)[1] if "_" in kind else ""
b = Path(".bench"); b.mkdir(exist_ok=True)
pid = os.getpid()
t0 = time.time()
with open(b / f"{kind}.launch.txt", "a") as fh:
    fh.write(f"{pid},{t0!r}\n"); fh.flush(); os.fsync(fh.fileno())
(b / f"{kind}.{pid}.start").write_text(repr(t0))
deadline = time.time() + 120.0
payloads = {}
for r in reads:
    p = Path(r)
    while not p.is_file():
        if time.time() > deadline:
            sys.exit(f"BENCH_READ_TIMEOUT:{r}")
        time.sleep(0.05)
    payloads[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
time.sleep(sleep_s)
if role == "BUILD":
    body = {"lesson": "n workers only pay off with n independent chains",
            "produced_by": kind, "workload": "SCALE_N_W1"}
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(body, sort_keys=True, indent=2) + "\n")
elif role == "RESEARCH":
    digests = {k: v for k, v in payloads.items()}
    text = ("# Research report\n\nsource: " + (reads[0] if reads else "-") +
            "\ndigests: " + json.dumps(digests, sort_keys=True) +
            "\nfinding: chain count, not worker count, bounds the speedup\n")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(text)
elif role == "REUSE":
    src = reads[0] if reads else ""
    sha = payloads.get(src, "")
    lesson = json.loads(Path(src).read_text())["lesson"] if src else ""
    body = {"reused_from": src, "sha256": sha, "lesson": lesson,
            "produced_by": kind}
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(body, sort_keys=True, indent=2) + "\n")
else:
    sys.exit(f"BENCH_UNKNOWN_KIND:{kind}")
(b / f"{kind}.{pid}.end").write_text(repr(time.time()))
'''

STATIC_INPUT = ("# Spec excerpt (static workload input, pre-planted)\n"
                "Section 1: a worker that finishes a chain must never wait on a later one.\n")


def _sleep_arg(s: float) -> str:
    return "%g" % s


def _expected_lesson(kind: str) -> bytes:
    return (json.dumps({"lesson": LESSON, "produced_by": kind,
                        "workload": WORKLOAD_ID},
                       sort_keys=True, indent=2) + "\n").encode()


def build_workload_n(root: Path, chains: int, mission_sleep_s: float) -> dict:
    """SCALE_N_W1 — `chains` independent 2-mission chains (default N projects).

    p1: P1_BUILD -> P1_RESEARCH (static input). p2..pN: Pi_BUILD -> Pi_REUSE
    (reads p(i-1)'s lesson READ-ONLY). Acyclic by construction: at 1 worker the
    daemon settles p1 fully, then p2 (whose source is already there), etc.
    """
    (root / "bench_worker.py").write_text(BENCH_WORKER_N, encoding="utf-8")
    sl = _sleep_arg(mission_sleep_s)
    lesson_rel = "knowledge/lesson.json"
    projects = [f"p{i}" for i in range(1, chains + 1)]
    milestones: list[str] = []
    deliverables: dict[str, list[str]] = {}
    expected: dict[str, object] = {}
    for i in range(1, chains + 1):
        proj = f"p{i}"
        build_mid = f"P{i}_BUILD"
        if i == 1:
            second_mid, second_out = "P1_RESEARCH", "research/report.md"
            second_cmd_extra = "inputs/spec_excerpt.md"
        else:
            second_mid, second_out = f"P{i}_REUSE", "reuse/reuse.json"
            second_cmd_extra = f"../p{i - 1}/{lesson_rel}"
        milestones.extend([build_mid, second_mid])
        deliverables[proj] = [lesson_rel, second_out]
        expected[f"{proj}/{lesson_rel}"] = _expected_lesson(build_mid)
        expected[f"{proj}/{second_out}"] = "structural"   # embeds runtime sha
        _init_project(
            root / proj,
            f"# Strategy\nNorth Star: chain {i} of {chains} scales independently.\n",
            "# Spec\n",
            _roadmap([
                _milestone(build_mid, [lesson_rel],
                           f"python3 ../bench_worker.py {build_mid} {sl} {lesson_rel}",
                           f"test -s {lesson_rel}"),
                _milestone(second_mid, [second_out],
                           f"python3 ../bench_worker.py {second_mid} {sl} "
                           f"{second_out} {second_cmd_extra}",
                           f"test -s {second_out}"),
            ]))
    (root / "p1" / "inputs").mkdir(parents=True, exist_ok=True)
    (root / "p1" / "inputs" / "spec_excerpt.md").write_text(STATIC_INPUT, encoding="utf-8")
    (root / "portfolio.json").write_text(json.dumps(
        {"schema": "portfolio/v1",
         "projects": [{"project_dir": p} for p in projects]}), encoding="utf-8")
    kinds_map = {m: m.split("_", 1)[0].lower() for m in milestones}
    return {"workload_id": WORKLOAD_ID, "projects": projects,
            "milestones": milestones, "deliverables": deliverables,
            "expected": expected, "kinds_map": kinds_map}


def _should_stop_after(obs: dict) -> bool:
    """Bounded scope: stop at the FIRST isolation failure — a corrupted candidate
    ancestry ends the run; later runs would only add noise on top of a broken base."""
    return not all(obs.get("isolation", {}).values())


def run_daemon_arm_n(run_root: Path, run_dir: Path, max_workers: int,
                     spec: dict, log_sink: list) -> dict:
    """Generic-SCALE_2 run_daemon_arm: same dance, but the kind->project map and the
    per-deliverable expectations come from the spec instead of SCALE_2 constants."""
    sys.path.insert(0, str(REPO_ROOT / "factory" / "bin"))
    import factoryd

    env = {"FACTORY_MAX_WORKERS": str(max_workers),
           "FACTORY_NO_REAL_ENGINE": "1",
           "FACTORY_RUNRECORDS_PATH": str(run_root / "runrecords.jsonl"),
           "FACTORY_AUTHORITY_FROM_WORKTREE": "1"}
    saved_env = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    redirects = {"CTO_EVENTS_PATH": run_root / "CTO_EVENTS.jsonl",
                 "CTO_STATUS_LATEST_PATH": run_root / "CTO_STATUS_LATEST.md",
                 "CANDIDATE_REGISTRY_PATH": run_root / "candidate_registry.json"}
    saved_attrs = {k: getattr(factoryd, k, None) for k in redirects}
    for k, v in redirects.items():
        if saved_attrs[k] is not None:
            setattr(factoryd, k, v)
    saved_state = (factoryd._ADVANCE_POOL, dict(factoryd._ADVANCE_FUTURES),
                   list(factoryd._PARALLEL_HARVEST), factoryd._RELOAD_DRAINING,
                   factoryd._shutdown_requested)
    factoryd._ADVANCE_POOL = None
    factoryd._ADVANCE_FUTURES.clear()
    factoryd._PARALLEL_HARVEST.clear()
    factoryd._RELOAD_DRAINING = False
    factoryd._shutdown_requested = False
    saved_log = factoryd._log
    factoryd._log = log_sink.append
    saved_cto_check = factoryd._cto_startup_check
    factoryd._cto_startup_check = lambda *a, **kw: True
    from factory_v2 import recovery_allocation as _ra
    saved_store = _ra.STORE
    _ra.STORE = run_root / "recovery_allocations"

    trees_pre = {p: _snapshot_tree(run_root / p) for p in spec["projects"]}

    t0 = time.monotonic()
    err: list = []
    timed_out = [False]
    completion = {"t": None, "settled": 0}
    done = threading.Event()

    def _watch() -> None:
        deadline = time.monotonic() + RUN_TIMEOUT_S
        while not done.is_set():
            settled = sum(1 for p in spec["projects"] for m in spec["milestones"]
                          if _mission_complete(run_root / p, m))
            if settled >= len(spec["milestones"]):
                completion["t"] = time.monotonic()
                completion["settled"] = settled
                factoryd._shutdown_requested = True
                return
            if time.monotonic() > deadline:
                timed_out[0] = True
                completion["t"] = time.monotonic()
                completion["settled"] = settled
                factoryd._shutdown_requested = True
                return
            time.sleep(0.02)

    watcher = threading.Thread(target=_watch, name="scalen-watch", daemon=True)
    watcher.start()

    def _loop() -> None:
        try:
            factoryd.run_daemon_loop(
                run_root / "unused_campaign_state.json", run_dir,
                poll_interval_s=POLL_INTERVAL_S, max_polls=1_000_000,
                portfolio_path=str(run_root / "portfolio.json"),
                idle_poll_interval_s=POLL_INTERVAL_S,
                backoff_ceiling_s=BACKOFF_CEILING_S)
        except BaseException as exc:                               # noqa: BLE001
            err.append("".join(traceback.format_exception(exc)))
        finally:
            done.set()
            factoryd._shutdown_requested = True

    _loop()
    watcher.join(timeout=5)
    daemon_joined = not err
    wall_s = (completion["t"] or time.monotonic()) - t0
    settled = completion["settled"]
    if timed_out[0]:
        err.append("RUN_TIMEOUT")
    factoryd._log = saved_log
    factoryd._cto_startup_check = saved_cto_check
    _ra.STORE = saved_store
    for k, v in saved_attrs.items():
        if v is not None:
            setattr(factoryd, k, v)
    (factoryd._ADVANCE_POOL, factoryd._ADVANCE_FUTURES,
     factoryd._PARALLEL_HARVEST, factoryd._RELOAD_DRAINING,
     factoryd._shutdown_requested) = saved_state
    factoryd._ADVANCE_FUTURES.clear()
    factoryd._PARALLEL_HARVEST.clear()
    for k, v in saved_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

    trees_post = {p: _snapshot_tree(run_root / p) for p in spec["projects"]}

    kinds = spec["kinds_map"]
    windows = []
    rw = run_dir / "run_windows.jsonl"
    if rw.is_file():
        windows = [json.loads(l) for l in rw.read_text().splitlines() if l.strip()]
    windows_folded = {}
    for w in windows:
        windows_folded[w.get("run_id")] = w
    run_ids = sorted(k for k, v in windows_folded.items() if v.get("started_at"))
    closed_windows = sum(1 for v in windows_folded.values() if v.get("ended_at"))
    window_max_workers = sorted({v.get("max_workers") for v in windows_folded.values()
                                 if v.get("started_at")})

    markers = {}
    launches = {}
    for kind, proj in kinds.items():
        pdir = run_root / proj
        wins = _launch_windows(pdir, kind)
        if wins:
            markers[kind] = wins
        launches[kind] = _launch_count(pdir, kind)

    overlaps = []
    flat = [(kind, w[0], w[1]) for kind, wins in markers.items() for w in wins]
    for i in range(len(flat)):
        for j in range(i + 1, len(flat)):
            k1, a0, a1 = flat[i]
            k2, b0, b1 = flat[j]
            if k1 == k2:
                continue                    # same mission retried: not parallelism
            if max(a0, b0) < min(a1, b1):
                overlaps.append(sorted([k1, k2]))
    same_mission_concurrent = []
    for kind, wins in markers.items():
        for i in range(len(wins)):
            for j in range(i + 1, len(wins)):
                if max(wins[i][0], wins[j][0]) < min(wins[i][1], wins[j][1]):
                    same_mission_concurrent.append(kind)

    deliverables_ok = {}
    deliverable_detail = {}
    for proj, rels in spec["deliverables"].items():
        for rel in rels:
            f = run_root / proj / rel
            exp = spec["expected"].get(f"{proj}/{rel}")
            if exp == "structural" or exp is None:
                ok = f.is_file() and f.stat().st_size > 0
                deliverable_detail[f"{proj}/{rel}"] = "structural"
            else:
                ok = f.is_file() and f.read_bytes() == exp
                deliverable_detail[f"{proj}/{rel}"] = (
                    "byte_exact" if ok else "MISMATCH")
            deliverables_ok[f"{proj}/{rel}"] = ok

    authority_ok = {}
    for proj in spec["projects"]:
        for authority_file in ("STRATEGY.md", "SPEC.md", "ROADMAP.yaml"):
            key = f"{proj}/{authority_file}"
            authority_ok[key] = (trees_pre[proj].get(authority_file)
                                 == trees_post[proj].get(authority_file))

    new_files = {}
    for proj in spec["projects"]:
        new_files[proj] = sorted(set(trees_post[proj]) - set(trees_pre[proj]))
    deliverable_owners: dict[str, set[str]] = {}
    for proj, rels in spec["deliverables"].items():
        deliverable_owners.setdefault(proj, set()).update(rels)
    bench_kinds_by_project: dict[str, set[str]] = {}
    for kind, proj in kinds.items():
        bench_kinds_by_project.setdefault(proj, set()).add(kind)
    cross_write_violations = []
    for proj, files in new_files.items():
        foreign_deliverables: set[str] = set()
        for other, rels in deliverable_owners.items():
            if other != proj:
                foreign_deliverables.update(rels)
        # Unlike SCALE_2_W1 (unique deliverable paths per project), SCALE_N_W1
        # projects intentionally share rel paths (knowledge/lesson.json): a rel
        # the project itself owns is local authorship, never a foreign write —
        # byte-exactness of the local deliverable is checked separately.
        foreign_deliverables -= deliverable_owners.get(proj, set())
        foreign_bench = {f".bench/{k}.{s}"
                         for other, ks in bench_kinds_by_project.items()
                         if other != proj for k in ks
                         for s in ("launch.txt", "start", "end")}
        for rel in files:
            if rel in foreign_deliverables or rel in foreign_bench:
                cross_write_violations.append(f"{proj}/{rel}")

    from factory_v2.launch_gate import ledger_path
    from factory_v2.resource_accounting import read_rows, fold
    ledger_rows = []
    torn_lines = 0
    for proj in spec["projects"]:
        lp = ledger_path(run_root / proj)
        if lp.is_file():
            raw_lines = [l for l in lp.read_text().splitlines() if l.strip()]
            for l in raw_lines:
                try:
                    ledger_rows.append(json.loads(l))
                except json.JSONDecodeError:
                    torn_lines += 1
    folded = fold(ledger_rows)
    runrecords_rows = []
    rr = run_root / "runrecords.jsonl"
    if rr.is_file():
        for l in rr.read_text().splitlines():
            if l.strip():
                try:
                    runrecords_rows.append(json.loads(l))
                except json.JSONDecodeError:
                    pass
    attempt_keys = [r.get("attempt_key") for r in folded if r.get("attempt_key")]
    open_orphans = [r.get("attempt_key") for r in folded if r.get("phase") == "OPEN"]
    non_success = [r.get("attempt_key") for r in folded
                   if r.get("exit_class") not in ("", "SUCCESS", None)
                   or (r.get("mission_pass") is False
                       and r.get("exit_class") == "")]
    ledger_max_workers = sorted({r.get("max_workers") for r in folded
                                 if r.get("max_workers") is not None})

    from factory_v2.project_authority import load_project_state
    state_events = []
    mission_records = {}
    missions_detail = {}
    for proj in spec["projects"]:
        st = load_project_state(run_root / proj)
        mission_records[proj] = sorted(st.get("missions", {}).keys())
        missions_detail[proj] = {
            mid: {k: rec.get(k) for k in
                  ("state", "attempts", "lease_generation", "last_attempt_id")}
            for mid, rec in st.get("missions", {}).items()}
        state_events.extend((proj, e.get("event"), e) for e in st.get("events", []))
    failure_summary: dict = {}
    for proj, ev, _e in state_events:
        if ev and any(t in str(ev) for t in
                      ("FAIL", "BLOCKED", "BUSY", "STALE",
                       "VIOLATION", "QUARANTINE")):
            failure_summary[f"{proj}:{ev}"] = failure_summary.get(f"{proj}:{ev}", 0) + 1

    parallel_selected = [e for e in state_events
                         if e[1] == "PARALLEL_MILESTONE_SELECTED"]

    observation = {
        "max_workers": max_workers,
        "workload": spec.get("workload_id", WORKLOAD_ID),
        "workload_milestones": spec["milestones"],
        "wall_s": round(wall_s, 3),
        "settled": settled,
        "expected_settled": len(spec["milestones"]),
        "throughput_missions_per_s": round(settled / wall_s, 5) if wall_s else 0.0,
        "worker_seconds": round(wall_s * max_workers, 3),
        "throughput_per_worker_s": round(settled / (wall_s * max_workers), 5)
        if wall_s else 0.0,
        "daemon_joined": daemon_joined,
        "daemon_error": err[:1],
        "run_window_ids": run_ids,
        "run_windows_closed": closed_windows,
        "run_window_max_workers": window_max_workers,
        "engine_markers": {k: [[round(w[0], 3), round(w[1], 3), w[2]]
                               for w in wins]
                           for k, wins in markers.items()},
        "engine_overlaps": overlaps,
        "same_mission_concurrent_overlaps": sorted(set(same_mission_concurrent)),
        "launches_per_mission": launches,
        "deliverables_ok": deliverables_ok,
        "deliverable_detail": deliverable_detail,
        "authority_files_unchanged": authority_ok,
        "new_files": new_files,
        "cross_write_violations": cross_write_violations,
        "attempt_rows_raw": len(ledger_rows),
        "attempt_keys": len(attempt_keys),
        "attempt_keys_unique": len(set(attempt_keys)) == len(attempt_keys),
        "open_orphans": open_orphans,
        "non_success_attempts": non_success,
        "torn_ledger_lines": torn_lines,
        "ledger_max_workers": ledger_max_workers,
        "runrecords_rows": runrecords_rows,
        "mission_records": mission_records,
        "missions_detail": missions_detail,
        "failure_event_summary": failure_summary,
        "parallel_milestone_selected_events": parallel_selected,
        "logs_tail": log_sink[-80:],
    }
    return observation


def _med(vals):
    s = sorted(vals)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--repeats", type=int, default=REPEATS_DEFAULT)
    ap.add_argument("--workers", type=int, default=N_DEFAULT,
                    help="N of the Nw arm (must be >= 3: the milestone is N > 2)")
    ap.add_argument("--mission-sleep", type=float, default=6.0)
    ap.add_argument("--chains", type=int, default=None,
                    help="independent chains in the workload (default: max(2, N))")
    ap.add_argument("--d019", action="store_true",
                    help="also run the D019 elastic fan-out drill at N workers")
    ap.add_argument("--only-arm", type=int, default=None,
                    help="sample ONE arm only (1, 2 or N) — reliability pooling")
    args = ap.parse_args(argv)
    if args.workers < 3:
        ap.error("--workers must be >= 3 (milestone SCALE_N is about N > 2)")
    if args.repeats < 1:
        ap.error("--repeats must be >= 1")
    if args.only_arm is not None and args.only_arm not in (1, 2, args.workers):
        ap.error(f"--only-arm must be one of 1, 2 or {args.workers}")

    n = args.workers
    chains = args.chains if args.chains is not None else max(2, n)
    out_dir = args.out.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    git_pre = subprocess.run(["git", "status", "--porcelain", "-uall"],
                             cwd=str(REPO_ROOT),
                             capture_output=True, text=True).stdout

    arms = (args.only_arm,) if args.only_arm is not None else (1, 2, n)
    observations = []
    stopped_at_isolation_failure = None
    for i in range(args.repeats):
        for max_workers in arms:
            if stopped_at_isolation_failure is not None:
                break
            with tempfile.TemporaryDirectory(prefix=f"scalen_{max_workers}w_") as td:
                run_root = Path(td)
                spec = build_workload_n(run_root, chains, args.mission_sleep)
                run_dir = run_root / "run"
                logs: list = []
                obs = run_daemon_arm_n(run_root, run_dir, max_workers, spec, logs)
                obs["repeat"] = i
                obs["reliability"] = _reliability(obs)
                obs["isolation"] = _isolation(obs)
                name = f"run_{i}_{max_workers}w.json"
                (out_dir / name).write_text(json.dumps(obs, indent=2, sort_keys=True))
                observations.append(obs)
                print(f"RUN repeat={i} workers={max_workers} wall={obs['wall_s']}s "
                      f"settled={obs['settled']}/{obs['expected_settled']} "
                      f"overlaps={len(obs['engine_overlaps'])} "
                      f"reliability={all(obs['reliability'].values())} "
                      f"isolation={all(obs['isolation'].values())}", flush=True)
                if _should_stop_after(obs):
                    stopped_at_isolation_failure = name
                    print(f"ISOLATION_FAILURE in {name}: remaining runs SKIPPED "
                          f"(bounded scope: stop at the first isolation failure)",
                          flush=True)

    def _arm(mw: int) -> dict:
        rs = [o for o in observations if o["max_workers"] == mw]
        walls = [o["wall_s"] for o in rs]
        thr = [o["throughput_missions_per_s"] for o in rs]
        return {"runs": len(rs), "walls_s": walls,
                "throughputs_missions_per_s": thr,
                "median_wall_s": _med(walls) if walls else None,
                "median_throughput": _med(thr) if thr else None,
                "run_window_ids": [rid for o in rs for rid in o["run_window_ids"]],
                "engine_overlaps_total": sum(len(o["engine_overlaps"]) for o in rs),
                "reliability_all_runs": [all(o["reliability"].values()) for o in rs],
                "isolation_all_runs": [all(o["isolation"].values()) for o in rs],
                "reliability_clean_runs": sum(all(o["reliability"].values()) for o in rs),
                "evidence": [f"run_{o['repeat']}_{mw}w.json" for o in rs]}

    summary = {
        "schema": "scale-n-benchmark-summary/v1",
        "workload_id": WORKLOAD_ID,
        "chains": chains,
        "mission_sleep_s": args.mission_sleep,
        "poll_interval_s": POLL_INTERVAL_S,
        "backoff_ceiling_s": BACKOFF_CEILING_S,
        "repeats": args.repeats,
        "n": n,
        "arms": {f"{mw}w": _arm(mw)
                 for mw in sorted({o["max_workers"] for o in observations})},
        "same_workload": True,
        "verdict_rule": ("SCALE_N_RECOMMENDED iff median throughput strictly increases "
                         "1w < 2w < Nw AND every isolation check passes on every Nw run "
                         "AND reliability counters at Nw are no worse than at 2w on this "
                         "same workload AND the Nw arm shows >= 1 real engine overlap; "
                         "anything else is NO_SCALE (an honest measured outcome)"),
        "linear_efficiency_threshold": LINEAR_EFFICIENCY,
    }
    if stopped_at_isolation_failure is not None:
        summary["stopped_at_isolation_failure"] = stopped_at_isolation_failure

    a1 = summary["arms"].get("1w")
    a2 = summary["arms"].get("2w")
    an = summary["arms"].get(f"{n}w")
    if a1 and a2 and an and a1["median_wall_s"] and an["median_wall_s"]:
        speedup = a1["median_wall_s"] / an["median_wall_s"]
        efficiency = speedup / n
        rel_n_ok = (an["reliability_clean_runs"] >= a2["reliability_clean_runs"]
                    and all(an["isolation_all_runs"]))
        iso_n_ok = all(an["isolation_all_runs"])
        thr_increase = (an["median_throughput"] > a2["median_throughput"] > a1["median_throughput"])
        summary["scaling"] = {
            "speedup_nw_over_1w": round(speedup, 3),
            "efficiency_per_worker": round(efficiency, 3),
            "linear": bool(speedup >= LINEAR_EFFICIENCY * n),
            "linear_threshold_speedup": round(LINEAR_EFFICIENCY * n, 3),
            "throughput_ratio_nw_over_1w": round(an["median_throughput"] / a1["median_throughput"], 3),
            "throughput_ratio_2w_over_1w": round(a2["median_throughput"] / a1["median_throughput"], 3),
            "throughput_ratio_nw_over_2w": round(an["median_throughput"] / a2["median_throughput"], 3),
        }
        summary["measured"] = {
            "throughput_strictly_increasing": thr_increase,
            "isolation_perfect_nw": iso_n_ok,
            "reliability_nw_no_worse_than_2w": rel_n_ok,
            "real_parallelism_nw": an["engine_overlaps_total"] >= 1,
        }
        summary["verdict"] = ("SCALE_N_RECOMMENDED"
                              if (thr_increase and iso_n_ok and rel_n_ok
                                  and an["engine_overlaps_total"] >= 1)
                              else "NO_SCALE")

    d019_obs = None
    if args.d019:
        with tempfile.TemporaryDirectory(prefix="scalen_d019_") as td:
            run_root = Path(td)
            spec = build_d019_workload(run_root)
            logs: list = []
            d019_obs = run_daemon_arm(run_root, run_root / "run", n, spec, logs)
            d019_obs["repeat"] = 0
            d019_obs["reliability"] = _reliability(d019_obs)
            d019_obs["isolation"] = _isolation(d019_obs)
            gamma_report = run_root / "gamma" / "research" / "g2.md"
            lesson = run_root / "older_product" / "knowledge" / "lesson.md"
            try:
                body = json.loads(gamma_report.read_text())
                expected_sha = hashlib.sha256(lesson.read_bytes()).hexdigest()
                d019_obs["d019"] = {
                    "workers": n,
                    "knowledge_sha_match": body.get("sha256") == expected_sha,
                    "reused_from": body.get("reused_knowledge_from"),
                    "fan_out_event": bool(d019_obs["parallel_milestone_selected_events"]),
                    "g1_g2_overlap": any(set(o) == {"G1_BUILD", "G2_RESEARCH"}
                                         for o in d019_obs["engine_overlaps"]),
                    "older_product_tree_unchanged": d019_obs["frozen_tree_unchanged"],
                }
            except (OSError, ValueError) as exc:
                d019_obs["d019"] = {"error": f"{type(exc).__name__}: {exc}"}
            (out_dir / "d019_drill.json").write_text(
                json.dumps(d019_obs, indent=2, sort_keys=True))
            print(f"D019 drill (N={n}): "
                  f"{json.dumps(d019_obs.get('d019', {}), sort_keys=True)}", flush=True)

    git_post = subprocess.run(["git", "status", "--porcelain", "-uall"],
                              cwd=str(REPO_ROOT),
                              capture_output=True, text=True).stdout

    def _repo_lines(text: str) -> list[str]:
        try:
            rel_out = str(out_dir.relative_to(REPO_ROOT)) + "/"
        except ValueError:
            rel_out = None
        return [l for l in text.strip().splitlines()
                if rel_out is None or not l[3:].startswith(rel_out)]

    summary["repo_clean_before"] = _repo_lines(git_pre)
    summary["repo_clean_after"] = _repo_lines(git_post)
    summary["repo_untouched"] = _repo_lines(git_pre) == _repo_lines(git_post)
    if d019_obs is not None:
        summary["d019"] = d019_obs.get("d019")
    (out_dir / "SUMMARY.json").write_text(json.dumps(summary, indent=2, sort_keys=True))
    print(json.dumps({k: v for k, v in summary.items()
                      if k in ("arms", "scaling", "measured", "verdict",
                               "repo_untouched", "stopped_at_isolation_failure",
                               "d019")}, indent=2, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
