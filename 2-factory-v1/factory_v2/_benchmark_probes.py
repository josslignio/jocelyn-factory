#!/usr/bin/env python3
"""Hermetic per-side probe driver for CHAMPION_VS_RESHAPED_CHALLENGER.

Executed as a SEPARATE subprocess by factory_v2/run_matched_benchmark.py, once per
(side, probe_id) pair, with PYTHONPATH prepended to the side's temporary git
worktree and cwd set to that worktree — so the imported factory_v2/factory code is
the code AT THAT SIDE'S SHA, while this driver script itself (read from a fixed
path, never copied into either worktree) is byte-identical for both sides.

Every probe first commits the SAME canonical fixture (a tiny hermetic test
project: STRATEGY.md/SPEC.md/ROADMAP.yaml with two milestones A, B — B depends on
A) in its own subdirectory, using a fixed author/committer identity and fixed
commit dates — so the resulting ``start_sha`` is bit-identical across every
probe, every side, every run. That canonical commit is the workload's identity
anchor; a probe that needs a different roadmap shape (a failing command, a slow
command, a different milestone graph, ...) builds ITS OWN scenario project in a
separate working subdirectory, but still reports the one shared ``start_sha``.

No real AI engine is ever exec'd: FACTORY_NO_REAL_ENGINE=1 is set for every probe,
and probes that must exercise the dispatcher's build step monkeypatch
``dispatcher_v3._run`` in-process (identical to tests/test_c43_*.py and
tests/test_c47_*.py) — never launches a subprocess named claude/codex/glm/etc.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

os.environ["FACTORY_NO_REAL_ENGINE"] = "1"

FIXED_ENV = {
    "GIT_AUTHOR_NAME": "bench", "GIT_AUTHOR_EMAIL": "bench@example.com",
    "GIT_COMMITTER_NAME": "bench", "GIT_COMMITTER_EMAIL": "bench@example.com",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00",
}


# --- shared hermetic project fixture (same shape as tests/test_factory_v2.py) ---

def roadmap_yaml(milestones: list, project: str = "bench", final: dict | None = None) -> str:
    import yaml
    body = {"schema": "roadmap/v2", "project": project,
            "final_dod": final or {"requires_milestones": [m["id"] for m in milestones]},
            "milestones": milestones}
    return yaml.safe_dump(body, sort_keys=False)


def ms(mid: str, deps: dict | None = None, command: str = "true",
       deliverables: list | None = None, e2e: str | None = "true",
       acceptance: list | None = None) -> dict:
    return {"id": mid, "goal": f"goal of {mid}", "depends_on": deps or {},
            "deliverables": deliverables or [],
            "acceptance": acceptance or [f"{mid} works"],
            "product_e2e": {"test_command": e2e},
            "execution": {"steps": [{"kind": "implement",
                                     "executor": {"type": "local_command", "command": command}}]}}


def write_project(root: Path, milestones: list, project: str = "bench",
                  final: dict | None = None) -> Path:
    pdir = root / project
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "STRATEGY.md").write_text("# STRATEGY\nNorth Star: bench.\n", encoding="utf-8")
    (pdir / "SPEC.md").write_text("# SPEC\nBench product.\n", encoding="utf-8")
    (pdir / "ROADMAP.yaml").write_text(roadmap_yaml(milestones, project, final), encoding="utf-8")
    return pdir


def _git_fixed_env(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(FIXED_ENV)
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, env=env)


def commit_fixture(pdir: Path) -> str:
    _git_fixed_env(pdir, "init", "-q", "-b", "main")
    _git_fixed_env(pdir, "add", "-A")
    _git_fixed_env(pdir, "commit", "-q", "-m", "bench: start fixture")
    return _git_fixed_env(pdir, "rev-parse", "HEAD").stdout.strip()


def canonical_sha(tmp: Path) -> str:
    """The one fixture ALL probes commit, so start_sha is identical everywhere —
    the workload's identity anchor, independent of each probe's own scenario."""
    canon_dir = tmp / "_canon"
    pdir = write_project(canon_dir, [ms("A"), ms("B", deps={"A": {"required_state": "COMPLETE"}})])
    return commit_fixture(pdir)


def _init_product_repo(repo: Path) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    _git_fixed_env(repo, "init", "-q", "-b", "main")
    _git_fixed_env(repo, "config", "user.email", "bench@example.com")
    _git_fixed_env(repo, "config", "user.name", "bench")
    (repo / "f.txt").write_text("v1", encoding="utf-8")
    _git_fixed_env(repo, "add", "-A")
    _git_fixed_env(repo, "commit", "-q", "-m", "init")
    return _git_fixed_env(repo, "rev-parse", "HEAD").stdout.strip()


def _obs(side: str, side_sha: str, probe_id: str, workload_id: str, start_sha: str,
         settled: bool, wall_s: float, restart_recovered: bool, launches_lost: int,
         engine_exercised: str, notes: str) -> dict:
    return {"side": side, "side_sha": side_sha, "probe_id": probe_id,
            "workload_id": workload_id, "start_sha": start_sha, "settled": bool(settled),
            "wall_s": round(float(wall_s), 3), "restart_recovered": bool(restart_recovered),
            "launches_lost": int(launches_lost), "engine_exercised": engine_exercised,
            "notes": notes}


# --- probes -------------------------------------------------------------------

def probe_dispatch_settles(side_root: Path, tmp: Path) -> dict:
    from factory_v2.runtime import run_until_stable
    from factory_v2.project_authority import load_project_state

    start_sha = canonical_sha(tmp)
    pdir = write_project(tmp / "work", [ms("A"), ms("B", deps={"A": {"required_state": "COMPLETE"}})])
    t0 = time.time()
    run_until_stable(pdir, max_cycles=10)
    wall = time.time() - t0
    completed = load_project_state(pdir).get("completed_milestones", {})
    settled = {"A", "B"} <= set(completed)
    return dict(start_sha=start_sha, wall_s=wall, settled=settled, restart_recovered=False,
               launches_lost=0, engine_exercised="NOT_EXERCISED",
               notes=f"run_until_stable, completed={sorted(completed)}")


def probe_kill_and_resume(side_root: Path, tmp: Path) -> dict:
    start_sha = canonical_sha(tmp)
    marker = "runs.txt"
    pdir = write_project(tmp / "work",
                         [ms("A", command=f"printf start >> {marker}; sleep 3; printf done >> {marker}")])
    code = (f"import sys; sys.path.insert(0, {str(side_root)!r}); "
            f"from pathlib import Path; from factory_v2.runtime import advance; "
            f"advance(Path({str(pdir)!r}))")
    t0 = time.time()
    proc = subprocess.Popen([sys.executable, "-c", code], preexec_fn=os.setsid,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            cwd=str(side_root))
    time.sleep(1.2)
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    partial = (pdir / marker).read_text(encoding="utf-8") if (pdir / marker).exists() else ""
    launches_lost = 0 if "done" in partial else 1

    sys.path.insert(0, str(side_root))
    from factory_v2.runtime import advance
    r2 = advance(pdir)
    wall = time.time() - t0
    final = (pdir / marker).read_text(encoding="utf-8") if (pdir / marker).exists() else ""
    settled = "done" in final
    restart_recovered = bool(settled and partial != final)
    return dict(start_sha=start_sha, wall_s=wall, settled=settled,
               restart_recovered=restart_recovered,
               launches_lost=launches_lost, engine_exercised="NOT_EXERCISED",
               notes=f"SIGKILL mid-command, partial={partial!r} final={final!r} r2={r2.action}")


def probe_quarantine_cycle(side_root: Path, tmp: Path) -> dict:
    from factory_v2.runtime import advance
    from factory_v2.project_authority import load_project_state
    from factory_v2.safety_quarantine import (
        enter_quarantine, freeze_repair_contract, update_repair_chain,
        REPAIR_STATUS_MAX_ITERATIONS,
    )

    start_sha = canonical_sha(tmp)
    pdir = write_project(tmp / "work",
                         [ms("A"), ms("B"), ms("C", deps={"A": {"required_state": "COMPLETE"}})])
    product = tmp / "product"
    real_sha = _init_product_repo(product)

    cause = "VALIDATION_GAP_UNRESOLVED_AFTER_DIAGNOSE"
    enter_quarantine(pdir, cause=cause, milestone_id="A", mission_id="A#gate",
                     attempt_id="A#a1", failure_fingerprint="probe failure")
    freeze_repair_contract(pdir, milestone_id="A", mission_id="A#gate", attempt_id="A#a1",
                           reproduction_command="true", regression_command="true",
                           validation_command="true", command_cwd=str(product),
                           pre_repair_sha=real_sha, authority_hashes={},
                           cause=cause, failure_fingerprint="probe failure")
    stale = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    update_repair_chain(pdir, current_episode={
        "schema": "repair-epoch/v1", "epoch_id": "bench-epoch", "cause": cause,
        "control_code_sha": stale, "context_manifest_sha": "", "contract_signature": "",
        "failure_fingerprint": "probe failure", "iterations": 9, "fingerprints": [],
        "status": REPAIR_STATUS_MAX_ITERATIONS, "reason": "8 repair cycles produced no verified fix",
        "terminal_code_sha": stale, "opened_at": "2026-01-01T00:00:00Z"})

    t0 = time.time()
    for _ in range(20):
        advance(pdir)
    wall = time.time() - t0
    completed = load_project_state(pdir).get("completed_milestones", {})
    b_done = "B" in completed
    c_held = "C" not in completed
    settled = b_done and c_held
    return dict(start_sha=start_sha, wall_s=wall, settled=settled, restart_recovered=False,
               launches_lost=0 if settled else 1, engine_exercised="NOT_EXERCISED",
               notes=f"A quarantined; B independent done={b_done}; C held={c_held}")


def probe_contract_refreeze(side_root: Path, tmp: Path) -> dict:
    from factory_v2.runtime import advance
    from factory_v2.project_authority import load_project_state, save_project_state

    start_sha = canonical_sha(tmp)
    marker = "runs.txt"
    pdir = write_project(tmp / "work", [ms("A", command=f"printf run >> {marker}")])

    t0 = time.time()
    r1 = advance(pdir)
    state = load_project_state(pdir)
    state["completed_milestones"] = {}
    save_project_state(pdir, state)
    (pdir / "ROADMAP.yaml").write_text(
        roadmap_yaml([ms("A", command=f"printf run >> {marker}",
                        acceptance=["A works differently"])]), encoding="utf-8")

    actions = [r1.action]
    settled_under_new_contract = False
    launches_lost = 0
    for _ in range(4):
        r = advance(pdir)
        actions.append(r.action)
        if r.action == "MILESTONE_SETTLED":
            settled_under_new_contract = True
            break
        launches_lost += 1
    wall = time.time() - t0
    old_never_resumed = (pdir / marker).read_text(encoding="utf-8").count("run") <= 2
    settled = settled_under_new_contract and old_never_resumed
    return dict(start_sha=start_sha, wall_s=wall, settled=settled, restart_recovered=False,
               launches_lost=launches_lost, engine_exercised="NOT_EXERCISED",
               notes=f"actions={actions}")


def _dispatcher_module(side_root: Path):
    sys.path.insert(0, str(side_root / "factory" / "bin"))
    import dispatcher_v3 as d3
    return d3


def _make_dispatch_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    (repo / "TASK.md").write_text("Tache de test bench.\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init", "--author=Bench <b@b>"], cwd=repo, check=True)
    return repo


def probe_terminal_state_new_attempt(side_root: Path, tmp: Path) -> dict:
    """LOT C43 shape: a NEW attempt must not resume a TERMINAL dispatcher state —
    it must archive it and start a fresh, correctly-metered launch."""
    d3 = _dispatcher_module(side_root)
    from mission_state import MissionState, StepRecord

    start_sha = canonical_sha(tmp)
    repo = _make_dispatch_repo(tmp / "work")
    mission_id = "M-terminal"
    out_path = tmp / "work" / f"receipt_{mission_id}.json"
    state_path = tmp / "work" / f"mission_{mission_id}.json"

    names = ["build_glm", "test_1", "repair_glm", "test_2", "build_claude", "test_3"]
    st = MissionState(mission_id, "repo", 120, names)
    st.steps = [StepRecord(name=n) for n in names]
    st.status = "done"
    for s in st.steps:
        s.status = "done"
    st.save(state_path)
    out_path.write_text('{"prior": true}\n', encoding="utf-8")

    real_run = d3._run
    calls: list = []

    def smart(cmd, cwd, timeout):
        if cmd and cmd[0] in ("/bin/sh", "bash", "git"):
            return real_run(cmd, cwd, timeout)
        calls.append(list(cmd))
        return 0, json.dumps({"type": "result", "subtype": "success",
                              "usage": {"input_tokens": 1, "output_tokens": 1}}), ""

    t0 = time.time()
    with mock.patch.object(d3, "_run", side_effect=smart):
        rec = d3.dispatch_v3(repo, "task", mission_id, "exit 0", 120, out_path)
    wall = time.time() - t0
    archive_dir = tmp / "work" / "archive"
    archived = list(archive_dir.glob("*_done")) if archive_dir.exists() else []
    exactly_one_call = len(calls) == 1
    settled = bool(archived) and exactly_one_call
    return dict(start_sha=start_sha, wall_s=wall, settled=settled, restart_recovered=False,
               launches_lost=0 if exactly_one_call else max(0, len(calls) - 1),
               engine_exercised="NOT_EXERCISED",
               notes=f"verdict={rec.get('verdict')} calls={len(calls)} archived={bool(archived)}")


def probe_unchanged_deliverable_reconfirmed(side_root: Path, tmp: Path) -> dict:
    """LOT C47 shape: a mechanically-green re-run that reconfirms an already
    delivered file must be judged against the PRODUCT base, not the previous
    attempt's own worktree HEAD — else genuine delivered work looks like a no-op
    forever. Only the challenger can pass ``product_base_sha`` through
    dispatch_v3(); the champion signature has no such parameter."""
    d3 = _dispatcher_module(side_root)
    start_sha = canonical_sha(tmp)

    repo = tmp / "work" / "product_repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "b@b"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "b"], cwd=repo, check=True)
    (repo / "universe.json").write_text("14 accounts\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "product base"], cwd=repo, check=True)
    true_base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
                               text=True, check=True).stdout.strip()
    (repo / "universe.json").write_text("25 accounts\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "attempt 1 delivers"], cwd=repo, check=True)

    real_run = d3._run
    committed = {"done": False}

    def smart(cmd, cwd, timeout):
        if cmd and cmd[0] in ("/bin/sh", "bash", "git"):
            return real_run(cmd, cwd, timeout)
        if not committed["done"]:
            (repo / "REPORT.json").write_text(json.dumps({"outcome": "IMPLEMENTED"}), encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "attempt 2 reconfirms"], cwd=repo, check=True)
            committed["done"] = True
        return 0, json.dumps({"type": "result", "subtype": "success",
                              "usage": {"input_tokens": 1, "output_tokens": 1}}), ""

    out_path = tmp / "work" / "receipt.json"
    kwargs = {}
    if "product_base_sha" in d3.dispatch_v3.__code__.co_varnames:
        kwargs["product_base_sha"] = true_base
    t0 = time.time()
    with mock.patch.object(d3, "_run", side_effect=smart):
        rec = d3.dispatch_v3(repo, "task", "M-unchanged", "exit 0", 120, out_path,
                             deliverables=["universe.json"], **kwargs)
    wall = time.time() - t0
    settled = rec.get("verdict") == "ACCEPTED"
    return dict(start_sha=start_sha, wall_s=wall, settled=settled, restart_recovered=False,
               launches_lost=0 if settled else 1, engine_exercised="NOT_EXERCISED",
               notes=f"verdict={rec.get('verdict')} product_base_sha_supported={'product_base_sha' in kwargs}")


PROBES = {
    "dispatch_settles": probe_dispatch_settles,
    "kill_and_resume": probe_kill_and_resume,
    "quarantine_cycle": probe_quarantine_cycle,
    "contract_refreeze": probe_contract_refreeze,
    "terminal_state_new_attempt": probe_terminal_state_new_attempt,
    "unchanged_deliverable_reconfirmed": probe_unchanged_deliverable_reconfirmed,
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--side", required=True, choices=("champion", "challenger"))
    ap.add_argument("--side-root", required=True)
    ap.add_argument("--side-sha", required=True)
    ap.add_argument("--probe", required=True, choices=sorted(PROBES))
    ap.add_argument("--workload", required=True)
    args = ap.parse_args()

    side_root = Path(args.side_root).resolve()
    sys.path.insert(0, str(side_root))
    tmp = Path(tempfile.mkdtemp(prefix=f"bench-{args.side}-{args.probe}-"))
    fn = PROBES[args.probe]
    try:
        result = fn(side_root, tmp)
    except Exception as exc:  # a probe failure is itself an observation, not a crash
        result = dict(start_sha=canonical_sha(tmp / "_fallback"), wall_s=0.0, settled=False,
                      restart_recovered=False, launches_lost=0, engine_exercised="NOT_EXERCISED",
                      notes=f"PROBE_EXCEPTION: {type(exc).__name__}: {exc}")
    obs = _obs(args.side, args.side_sha, args.probe, args.workload,
              result.pop("start_sha"), result.pop("settled"), result.pop("wall_s"),
              result.pop("restart_recovered"), result.pop("launches_lost"),
              result.pop("engine_exercised"), result.pop("notes"))
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)   # FACTORY_REPO_HYGIENE_V1 : l'observation est imprimée, la sonde ne laisse rien
    print(json.dumps(obs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
