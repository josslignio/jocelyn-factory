#!/usr/bin/env python3
"""SCALE_2 benchmark producer — 1-worker baseline vs 2-worker run, SAME workload.

The pinned gate (factory_v2/verify_scale_benchmark.py) is fail-closed and names this
mission's task file as the owed producer: "measure a 1-worker baseline and a 2-worker
run on the SAME workload and emit both measurements". This script is that producer.

What it runs is the REAL supervisor path: factoryd.run_daemon_loop in portfolio mode
(exactly like production and like tests/test_portfolio_quarantine_isolation.py drives
it), over a hermetic, deterministically generated portfolio. Missions are local_command
executors with real sleeps and real subprocesses, so every scheduling cost being scaled
is real: project locks, worker leases, heartbeats, attempt ledgers, settlement, run
windows. No AI quality is measured and none is claimed — the measured quantity is the
throughput and reliability of the MAX_WORKERS scheduling machinery itself.

Arms differ ONLY by FACTORY_MAX_WORKERS (1 vs 2). Workload bytes are identical across
arms and repeats (commands are relative to each project dir; trees are regenerated
fresh per run). Repeats are interleaved 1w,2w,1w,2w,... to decorrelate machine noise.

Raw observations land in --out (one JSON per run + SUMMARY.json). Nothing here edits
the repo; the driver snapshots git status before/after and records it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

WORKLOAD_ID = "SCALE_2_W1"
D019_WORKLOAD_ID = "SCALE_2_D019"
MISSION_SLEEP_S = 6.0
POLL_INTERVAL_S = 0.05
BACKOFF_CEILING_S = 1.0
RUN_TIMEOUT_S = 300.0
DAEMON_JOIN_S = 60.0
REPEATS_DEFAULT = 3

#: Pre-declared decision rule (mirrored in the receipt; computed, never hand-set):
#: SCALE_RECOMMENDED iff median throughput(2w) > median throughput(1w) AND every
#: reliability counter of arm 2w is equal or better than arm 1w AND every isolation
#: check passes on arm 2w. A measured no-improvement is reported as NO_SCALE.

BENCH_WORKER = r'''#!/usr/bin/env python3
"""Hermetic benchmark mission worker (SCALE_2). One process per mission launch.

Usage: bench_worker.py <KIND> <SLEEP_S> <OUT_RELPATH> [READ_RELPATH ...]

Writes, under the calling project dir:
  .bench/<KIND>.launch.txt  — one line per LAUNCH (pid,t0) appended O_APPEND
  .bench/<KIND>.start/.end  — epoch floats bracketing the sleep+work
  <OUT_RELPATH>             — the deterministic deliverable
Reads are READ-ONLY (cross-product knowledge reuse never writes back).
"""
import hashlib, json, os, sys, time
from pathlib import Path

kind, sleep_s, out = sys.argv[1], float(sys.argv[2]), sys.argv[3]
reads = sys.argv[4:]
b = Path(".bench"); b.mkdir(exist_ok=True)
pid = os.getpid()
t0 = time.time()
with open(b / f"{kind}.launch.txt", "a") as fh:
    fh.write(f"{pid},{t0!r}\n"); fh.flush(); os.fsync(fh.fileno())
# Per-LAUNCH markers (a retried mission overwrites nothing — the collector
# reconstructs every launch window from these).
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
if kind == "A1_BUILD":
    body = {"lesson": "freeze the manifest before any fan-out", "produced_by": kind,
            "workload": "SCALE_2_W1"}
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(body, sort_keys=True, indent=2) + "\n")
elif kind == "A2_RESEARCH":
    digests = {k: v for k, v in payloads.items()}
    text = ("# Research report (A2)\n\nsource: " + (reads[0] if reads else "-") +
            "\ndigests: " + json.dumps(digests, sort_keys=True) +
            "\nfinding: fan-out is safe when deliverables are disjoint\n")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(text)
elif kind == "B1_BUILD":
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text("beta build output v1\nline2\n")
elif kind == "B2_REUSE":
    src = reads[0] if reads else ""
    sha = payloads.get(src, "")
    lesson = json.loads(Path(src).read_text())["lesson"] if src else ""
    body = {"reused_from": src, "sha256": sha, "lesson": lesson,
            "produced_by": kind}
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(body, sort_keys=True, indent=2) + "\n")
elif kind == "G1_BUILD":
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text("gamma build output v1\n")
elif kind == "G2_RESEARCH":
    src = reads[0] if reads else ""
    sha = payloads.get(src, "")
    body = {"reused_knowledge_from": src, "sha256": sha, "produced_by": kind}
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(body, sort_keys=True, indent=2) + "\n")
else:
    sys.exit(f"BENCH_UNKNOWN_KIND:{kind}")
(b / f"{kind}.{pid}.end").write_text(repr(time.time()))
'''

ALPHA_STRATEGY = "# Strategy\nNorth Star: measured scaling truth.\n"
ALPHA_SPEC = "# Spec\n"
BETA_STRATEGY = "# Strategy\nNorth Star: reuse proven knowledge.\n"
BETA_SPEC = "# Spec\n"
BETA_PREKNOWLEDGE_NOTE = None

ALPHA_INPUT = ("# Spec excerpt (static workload input, pre-planted)\n"
               "Section 1: the scheduler must release the project lock while an engine runs.\n")


def _sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _roadmap(milestones: list[dict]) -> str:
    import yaml
    return yaml.safe_dump({"version": "v3", "milestones": milestones})


def _milestone(mid: str, deliverables: list[str], command: str,
               e2e: str) -> dict:
    return {"id": mid, "goal": f"goal of {mid}", "depends_on": {},
            "deliverables": deliverables, "acceptance": [f"{mid} works"],
            "product_e2e": {"test_command": e2e},
            "execution": {"steps": [{"kind": "implement",
                                     "executor": {"type": "local_command",
                                                  "command": command}}]}}


def _init_project(pdir: Path, strategy: str, spec: str, roadmap_text: str,
                  state_extra: dict | None = None) -> None:
    sys.path.insert(0, str(REPO_ROOT))
    from factory_v2.project_authority import save_project_state
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "STRATEGY.md").write_text(strategy, encoding="utf-8")
    (pdir / "SPEC.md").write_text(spec, encoding="utf-8")
    (pdir / "ROADMAP.yaml").write_text(roadmap_text, encoding="utf-8")
    save_project_state(pdir, {
        "schema": "project-state/v2", "project": pdir.name,
        "completed_milestones": {}, "validation_contracts": {},
        "current_milestone": None, "missions": {}, "escalation_ladder": {},
        "events": [], **(state_extra or {}),
    })


def build_workload(root: Path) -> dict:
    """SCALE_2_W1 — portfolio {alpha, beta}, 4 missions x 6s, disjoint chains.

    alpha: A1_BUILD (writes knowledge/lesson.json) then A2_RESEARCH (reads a STATIC
    pre-planted input, writes research/report.md — deliverables disjoint from A1 so
    the intra-project alternate is legal). beta: B1_BUILD, then B2_REUSE which reads
    alpha's A1 artifact READ-ONLY (bounded wait) and embeds its sha256 — cross-product
    knowledge reuse with zero authority sharing.
    """
    (root / "bench_worker.py").write_text(BENCH_WORKER, encoding="utf-8")
    alpha = root / "alpha"
    _init_project(alpha, ALPHA_STRATEGY, ALPHA_SPEC, _roadmap([
        _milestone("A1_BUILD", ["knowledge/lesson.json"],
                   "python3 ../bench_worker.py A1_BUILD %d knowledge/lesson.json"
                   % int(MISSION_SLEEP_S),
                   "test -s knowledge/lesson.json"),
        _milestone("A2_RESEARCH", ["research/report.md"],
                   "python3 ../bench_worker.py A2_RESEARCH %d research/report.md inputs/spec_excerpt.md"
                   % int(MISSION_SLEEP_S),
                   "test -s research/report.md"),
    ]))
    (alpha / "inputs").mkdir(exist_ok=True)
    (alpha / "inputs" / "spec_excerpt.md").write_text(ALPHA_INPUT, encoding="utf-8")
    beta = root / "beta"
    _init_project(beta, BETA_STRATEGY, BETA_SPEC, _roadmap([
        _milestone("B1_BUILD", ["build_out.txt"],
                   "python3 ../bench_worker.py B1_BUILD %d build_out.txt"
                   % int(MISSION_SLEEP_S),
                   "test -s build_out.txt"),
        _milestone("B2_REUSE", ["reuse/reuse.json"],
                   "python3 ../bench_worker.py B2_REUSE %d reuse/reuse.json ../alpha/knowledge/lesson.json"
                   % int(MISSION_SLEEP_S),
                   "test -s reuse/reuse.json"),
    ]))
    (root / "portfolio.json").write_text(json.dumps(
        {"schema": "portfolio/v1",
         "projects": [{"project_dir": "alpha"}, {"project_dir": "beta"}]}), encoding="utf-8")
    return {"projects": ["alpha", "beta"],
            "milestones": ["A1_BUILD", "A2_RESEARCH", "B1_BUILD", "B2_REUSE"],
            "deliverables": {"alpha": ["knowledge/lesson.json", "research/report.md"],
                             "beta": ["build_out.txt", "reuse/reuse.json"]}}


def build_d019_workload(root: Path) -> dict:
    """SCALE_2_D019 — elastic fan-out + cross-product reuse drill (2 workers only).

    gamma has two milestones with DISJOINT deliverables (G1_BUILD 5s, G2_RESEARCH 3s):
    with MAX_WORKERS=2 the daemon's second per-project slot dispatches G2 while G1's
    engine runs (intra-project elastic fan-out on the real daemon path). G2 reads the
    OLDER product's pre-planted knowledge READ-ONLY and embeds its sha256 — cross-
    product knowledge reuse; the older product tree must stay byte-identical (no
    authority sharing).
    """
    (root / "bench_worker.py").write_text(BENCH_WORKER, encoding="utf-8")
    older = root / "older_product"
    _init_project(older, "# Strategy\nNorth Star: accumulated lessons.\n", "# Spec\n",
                  _roadmap([_milestone("LEGACY", ["done.txt"],
                                       "true", "true")]))
    (older / "knowledge").mkdir(exist_ok=True)
    (older / "knowledge" / "lesson.md").write_text(
        "# Lesson 7\nNever scale workers before the lock release is proven.\n",
        encoding="utf-8")
    (older / "done.txt").write_text("legacy complete\n", encoding="utf-8")
    gamma = root / "gamma"
    _init_project(gamma, "# Strategy\nNorth Star: elastic research.\n", "# Spec\n",
                  _roadmap([
                      _milestone("G1_BUILD", ["build/g1.txt"],
                                 "python3 ../bench_worker.py G1_BUILD 5 build/g1.txt",
                                 "test -s build/g1.txt"),
                      _milestone("G2_RESEARCH", ["research/g2.md"],
                                 "python3 ../bench_worker.py G2_RESEARCH 3 research/g2.md ../older_product/knowledge/lesson.md",
                                 "test -s research/g2.md"),
                  ]))
    (root / "portfolio.json").write_text(json.dumps(
        {"schema": "portfolio/v1",
         "projects": [{"project_dir": "gamma"}]}), encoding="utf-8")
    return {"projects": ["gamma"],
            "milestones": ["G1_BUILD", "G2_RESEARCH"],
            "deliverables": {"gamma": ["build/g1.txt", "research/g2.md"]},
            "frozen_tree": older}


def _snapshot_tree(root: Path) -> dict:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root))] = _sha256_file(p)
    return out


def _expected_deliverable(project: str, rel: str, run_root: Path) -> bytes:
    if rel == "knowledge/lesson.json":
        return (json.dumps({"lesson": "freeze the manifest before any fan-out",
                            "produced_by": "A1_BUILD", "workload": "SCALE_2_W1"},
                           sort_keys=True, indent=2) + "\n").encode()
    if rel == "build_out.txt":
        return b"beta build output v1\nline2\n"
    if rel == "build/g1.txt":
        return b"gamma build output v1\n"
    if rel == "reuse/reuse.json" or rel == "research/g2.md":
        return b""          # content checked structurally (embeds runtime sha)
    if rel == "research/report.md":
        return b""          # content checked structurally (embeds static input digest)
    raise KeyError(rel)


def _mission_complete(pdir: Path, mid: str) -> bool:
    sys.path.insert(0, str(REPO_ROOT))
    from factory_v2.project_authority import load_project_state
    try:
        st = load_project_state(pdir)
    except Exception:
        return False
    entry = (st.get("completed_milestones") or {}).get(mid) or {}
    return entry.get("state") == "COMPLETE"


def _launch_windows(pdir: Path, kind: str) -> list[tuple[float, float, int]]:
    """All launch windows of one mission, from per-launch markers (pid-keyed)."""
    b = pdir / ".bench"
    out = []
    if not b.is_dir():
        return out
    for start in sorted(b.glob(f"{kind}.*.start")):
        stem = start.name.rsplit(".", 1)[0]
        try:
            pid = int(stem.rsplit(".", 1)[1])
        except ValueError:
            continue
        end = b / f"{stem}.end"
        try:
            t0 = float(start.read_text())
            t1 = float(end.read_text()) if end.exists() else None
        except (OSError, ValueError):
            continue
        if t1 is not None:
            out.append((t0, t1, pid))
    return out


def _launch_count(pdir: Path, kind: str) -> int:
    f = pdir / ".bench" / f"{kind}.launch.txt"
    try:
        return len([l for l in f.read_text().splitlines() if l.strip()])
    except OSError:
        return 0


def run_daemon_arm(run_root: Path, run_dir: Path, max_workers: int,
                   spec: dict, log_sink: list) -> dict:
    """Drive factoryd.run_daemon_loop (portfolio mode) until every milestone of the
    workload is COMPLETE, then stop it cleanly. Returns raw measurements."""
    sys.path.insert(0, str(REPO_ROOT / "factory" / "bin"))
    import factoryd

    env = {"FACTORY_MAX_WORKERS": str(max_workers),
           "FACTORY_NO_REAL_ENGINE": "1",
           "FACTORY_RUNRECORDS_PATH": str(run_root / "runrecords.jsonl"),
           "FACTORY_AUTHORITY_FROM_WORKTREE": "1"}
    saved_env = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    # Hermetic redirects for daemon-owned repo paths (never touch the real repo).
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
    # The COST_NO_PROGRESS brake journals recovery allocations into the REAL repo
    # (factory_v2/receipts/recovery_allocations — path derived from the module
    # file). A hermetic benchmark must not pollute the repo: redirect the store
    # for the duration of the run (journal_dir() reads STORE at call time).
    from factory_v2 import recovery_allocation as _ra
    saved_store = _ra.STORE
    _ra.STORE = run_root / "recovery_allocations"

    trees_pre = {p: _snapshot_tree(run_root / p) for p in spec["projects"]}
    frozen_pre = None
    if spec.get("frozen_tree") is not None:
        frozen_pre = _snapshot_tree(run_root / spec["frozen_tree"].name
                                    if isinstance(spec["frozen_tree"], Path)
                                    else run_root / str(spec["frozen_tree"]))

    t0 = time.monotonic()
    err: list = []
    timed_out = [False]
    completion = {"t": None, "settled": 0}
    done = threading.Event()

    def _watch() -> None:
        """Completion/timeout watcher: stops the daemon loop via its own flag.
        The loop honours _shutdown_requested at every poll boundary (<= ~1 s)."""
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

    watcher = threading.Thread(target=_watch, name="scale2-watch", daemon=True)
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

    # ── raw evidence collection ─────────────────────────────────────────────
    kinds = {"A1_BUILD": "alpha", "A2_RESEARCH": "alpha",
             "B1_BUILD": "beta", "B2_REUSE": "beta",
             "G1_BUILD": "gamma", "G2_RESEARCH": "gamma"}
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
            exp = _expected_deliverable(proj, rel, run_root)
            if exp == b"":
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
    # Cross-tree write = a file authored by ANOTHER project's missions appearing in
    # this project's tree: another project's deliverable path, or its .bench markers.
    # (Knowledge REUSE is the designed read-only flow: B2_REUSE embeds alpha's lesson
    # INSIDE beta's own deliverable — that is reuse evidence, not a cross write.)
    deliverable_owners: dict[str, set[str]] = {}
    for proj, rels in spec["deliverables"].items():
        deliverable_owners.setdefault(proj, set()).update(rels)
    bench_kinds_by_project = {"alpha": {"A1_BUILD", "A2_RESEARCH"},
                              "beta": {"B1_BUILD", "B2_REUSE"},
                              "gamma": {"G1_BUILD", "G2_RESEARCH"}}
    cross_write_violations = []
    for proj, files in new_files.items():
        foreign_deliverables: set[str] = set()
        for other, rels in deliverable_owners.items():
            if other != proj:
                foreign_deliverables.update(rels)
        foreign_bench = {f".bench/{k}.{s}"
                         for other, kinds in bench_kinds_by_project.items()
                         if other != proj for k in kinds
                         for s in ("launch.txt", "start", "end")}
        for rel in files:
            if rel in foreign_deliverables or rel in foreign_bench:
                cross_write_violations.append(f"{proj}/{rel}")

    # attempt ledgers (one per project evidence dir)
    sys.path.insert(0, str(REPO_ROOT))
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

    # project state: mission records coexist, no failure events
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
    failure_events = [e for e in state_events
                      if e[1] and any(t in str(e[1]) for t in
                                      ("FAIL", "BLOCKED", "BUSY", "STALE",
                                       "VIOLATION", "QUARANTINE"))]

    failure_summary: dict = {}
    for proj, ev, _e in state_events:
        if ev and any(t in str(ev) for t in
                      ("FAIL", "BLOCKED", "BUSY", "STALE", "VIOLATION", "QUARANTINE")):
            failure_summary[f"{proj}:{ev}"] = failure_summary.get(f"{proj}:{ev}", 0) + 1

    parallel_selected = [e for e in state_events
                         if e[1] == "PARALLEL_MILESTONE_SELECTED"]

    frozen_ok = True
    if spec.get("frozen_tree") is not None:
        frozen_name = (spec["frozen_tree"].name
                       if isinstance(spec["frozen_tree"], Path)
                       else str(spec["frozen_tree"]))
        frozen_post = _snapshot_tree(run_root / frozen_name)
        frozen_ok = frozen_pre == frozen_post

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
        "same_mission_concurrent_overlaps": sorted(set(same_mission_concurrent)),
        "parallel_milestone_selected_events": parallel_selected,
        "frozen_tree_unchanged": frozen_ok,
        "logs_tail": log_sink[-80:],
    }
    return observation


def _reliability(obs: dict) -> dict:
    exp = obs["expected_settled"]
    workload_launches = {k: v for k, v in obs["launches_per_mission"].items()
                         if k in set(obs.get("workload_milestones", []))}
    fails = {k: v for k, v in obs.get("failure_event_summary", {}).items()
             if k.endswith(":MISSION_FAIL")}
    violations = {k: v for k, v in obs.get("failure_event_summary", {}).items()
                  if "VIOLATION" in k}
    return {
        "settled_all": obs["settled"] == exp,
        "single_launch_per_mission": (len(workload_launches) == exp
                                      and all(v == 1
                                              for v in workload_launches.values())),
        "no_mission_failures": not fails,
        "no_lease_violations": not violations,
        "no_same_mission_concurrent_overlaps": (
            not obs.get("same_mission_concurrent_overlaps")),
        "daemon_clean_exit": obs["daemon_joined"] and not obs["daemon_error"],
        "run_windows_closed": obs["run_windows_closed"] >= 1,
        "no_open_orphans": not obs["open_orphans"],
        "no_failed_attempts": not obs["non_success_attempts"],
        "no_torn_ledger_lines": obs["torn_ledger_lines"] == 0,
    }


def _isolation(obs: dict) -> dict:
    return {
        "deliverables_exact": all(obs["deliverables_ok"].values()),
        "authority_files_unchanged": all(obs["authority_files_unchanged"].values()),
        "no_cross_tree_writes": not obs["cross_write_violations"],
        "ledger_integrity": (obs["attempt_keys_unique"]
                             and obs["torn_ledger_lines"] == 0),
        "mission_records_coexist": (all(len(v) >= 1 for v in obs["mission_records"].values())
                                    and sum(len(v) for v in obs["mission_records"].values()) >= obs["expected_settled"]),
    }


def main(argv=None) -> int:
    global MISSION_SLEEP_S  # noqa: PLW0603 — CLI override of the workload constant
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--repeats", type=int, default=REPEATS_DEFAULT)
    ap.add_argument("--workers", type=int, default=None,
                    help="restrict to ONE worker count (1 or 2) — extra sampling of "
                         "a single arm; the headline comparison always uses both")
    ap.add_argument("--mission-sleep", type=float, default=MISSION_SLEEP_S)
    ap.add_argument("--d019", action="store_true",
                    help="also run the D019 elastic fan-out drill (2 workers)")
    args = ap.parse_args(argv)

    MISSION_SLEEP_S = args.mission_sleep

    out_dir = args.out.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    git_pre = subprocess.run(["git", "status", "--porcelain", "-uall"],
                             cwd=str(REPO_ROOT),
                             capture_output=True, text=True).stdout

    import tempfile
    observations = []
    order = []
    arms = (1, 2) if args.workers is None else (args.workers,)
    for i in range(args.repeats):
        for max_workers in arms:
            with tempfile.TemporaryDirectory(prefix=f"scale2_{max_workers}w_") as td:
                run_root = Path(td)
                spec = build_workload(run_root)
                spec["workload_id"] = WORKLOAD_ID
                run_dir = run_root / "run"
                logs: list = []
                obs = run_daemon_arm(run_root, run_dir, max_workers, spec, logs)
                obs["repeat"] = i
                obs["reliability"] = _reliability(obs)
                obs["isolation"] = _isolation(obs)
                name = f"run_{i}_{max_workers}w.json"
                (out_dir / name).write_text(json.dumps(obs, indent=2, sort_keys=True))
                observations.append(obs)
                order.append((max_workers, obs["wall_s"], obs["settled"]))
                print(f"RUN repeat={i} workers={max_workers} wall={obs['wall_s']}s "
                      f"settled={obs['settled']}/{obs['expected_settled']} "
                      f"overlaps={len(obs['engine_overlaps'])} "
                      f"reliability={all(obs['reliability'].values())} "
                      f"isolation={all(obs['isolation'].values())}", flush=True)

    def _med(vals):
        s = sorted(vals)
        n = len(s)
        return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2

    walls1 = [o["wall_s"] for o in observations if o["max_workers"] == 1]
    walls2 = [o["wall_s"] for o in observations if o["max_workers"] == 2]
    thr1 = [o["throughput_missions_per_s"] for o in observations if o["max_workers"] == 1]
    thr2 = [o["throughput_missions_per_s"] for o in observations if o["max_workers"] == 2]
    rel1 = [all(o["reliability"].values()) for o in observations if o["max_workers"] == 1]
    rel2 = [all(o["reliability"].values()) for o in observations if o["max_workers"] == 2]
    iso2 = [all(o["isolation"].values()) for o in observations if o["max_workers"] == 2]
    iso1 = [all(o["isolation"].values()) for o in observations if o["max_workers"] == 1]
    overlaps1 = sum(len(o["engine_overlaps"]) for o in observations if o["max_workers"] == 1)
    overlaps2 = sum(len(o["engine_overlaps"]) for o in observations if o["max_workers"] == 2)

    summary = {
        "schema": "scale2-benchmark-summary/v1",
        "workload_id": WORKLOAD_ID,
        "mission_sleep_s": MISSION_SLEEP_S,
        "poll_interval_s": POLL_INTERVAL_S,
        "backoff_ceiling_s": BACKOFF_CEILING_S,
        "repeats": args.repeats,
        "arm_1w": {"walls_s": walls1, "throughputs": thr1,
                   "median_wall_s": _med(walls1) if walls1 else None,
                   "median_throughput": _med(thr1) if thr1 else None,
                   "reliability_all_runs": rel1, "isolation_all_runs": iso1,
                   "engine_overlaps_total": overlaps1},
        "arm_2w": {"walls_s": walls2, "throughputs": thr2,
                   "median_wall_s": _med(walls2) if walls2 else None,
                   "median_throughput": _med(thr2) if thr2 else None,
                   "reliability_all_runs": rel2, "isolation_all_runs": iso2,
                   "engine_overlaps_total": overlaps2},
        "same_workload": True,
        "verdict_rule": ("SCALE_RECOMMENDED iff median_throughput(2w) > "
                         "median_throughput(1w) AND all reliability 2w AND all "
                         "isolation 2w AND arm2 shows >=1 real engine overlap"),
    }
    if walls1 and walls2:
        improved = _med(thr2) > _med(thr1)
        reliable = all(rel2) and all(rel1)
        isolated = all(iso2)
        summary["measured"] = {
            "throughput_improved": improved,
            "reliability_non_regressed": reliable,
            "isolation_perfect_2w": isolated,
            "real_parallelism_2w": overlaps2 >= 1,
            "speedup_median_wall": round(_med(walls1) / _med(walls2), 3)
            if _med(walls2) else None,
        }
        summary["verdict"] = ("SCALE_RECOMMENDED"
                              if (improved and reliable and isolated and overlaps2 >= 1)
                              else "NO_SCALE")

    d019_obs = None
    if args.d019:
        with tempfile.TemporaryDirectory(prefix="scale2_d019_") as td:
            run_root = Path(td)
            spec = build_d019_workload(run_root)
            spec["workload_id"] = D019_WORKLOAD_ID
            logs: list = []
            d019_obs = run_daemon_arm(run_root, run_root / "run", 2, spec, logs)
            d019_obs["repeat"] = 0
            d019_obs["reliability"] = _reliability(d019_obs)
            d019_obs["isolation"] = _isolation(d019_obs)
            gamma_report = run_root / "gamma" / "research" / "g2.md"
            lesson = run_root / "older_product" / "knowledge" / "lesson.md"
            try:
                body = json.loads(gamma_report.read_text())
                expected_sha = hashlib.sha256(lesson.read_bytes()).hexdigest()
                d019_obs["d019"] = {
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
            print(f"D019 drill: {json.dumps(d019_obs.get('d019', {}), sort_keys=True)}",
                  flush=True)

    git_post = subprocess.run(["git", "status", "--porcelain", "-uall"],
                              cwd=str(REPO_ROOT),
                              capture_output=True, text=True).stdout
    # The benchmark's own --out dir legitimately appears as untracked output;
    # exclude it from the untouched check (tracked-file mutations and any OTHER
    # untracked path still count as pollution).
    def _repo_lines(text: str) -> list[str]:
        try:
            rel_out = str(out_dir.relative_to(REPO_ROOT)) + "/"
        except ValueError:
            rel_out = None                       # --out outside the repo
        return [l for l in text.strip().splitlines()
                if rel_out is None or not l[3:].startswith(rel_out)]
    summary["repo_clean_before"] = _repo_lines(git_pre)
    summary["repo_clean_after"] = _repo_lines(git_post)
    summary["repo_untouched"] = _repo_lines(git_pre) == _repo_lines(git_post)
    if d019_obs is not None:
        summary["d019"] = d019_obs.get("d019")
    (out_dir / "SUMMARY.json").write_text(json.dumps(summary, indent=2, sort_keys=True))
    print(json.dumps({k: v for k, v in summary.items()
                      if k in ("arm_1w", "arm_2w", "measured", "verdict",
                               "repo_untouched", "d019")}, indent=2, sort_keys=True),
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
