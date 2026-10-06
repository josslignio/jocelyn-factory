"""Banc de chaos, série 2 (FACTORY_CHAOS_DRILL_V2) — les 8 pannes de la nuit du 19 au 20/09.

Chaque scénario injecte UNE panne réellement subie dans la vraie boucle
`factory_v2.runtime.advance()` ou dans les primitives réelles du daemon
(`factory/bin/factoryd.py`), du dispatcher (`factory/bin/dispatcher_v3.py`) et du
gestionnaire de worktrees (`factory/lib/worktree_manager.py`), sur un projet JETABLE
hermétique (FACTORY_NO_REAL_ENGINE=1, stubs), puis mesure qu'elle se résorbe SEULE.
Aucun moteur réel n'est jamais lancé. Le reçu est la sortie du banc exécuté :

    python3 -m factory_v2.chaos_drill --set v2 --all --out recu.json
    python3 -m factory_v2.chaos_drill --scenario PARKED_WITHOUT_MILESTONE --out r.json

Les ids sont ceux du juge (`factory_v2.verify_chaos_drill.SCENARIOS_V2`), au caractère
près. Les 12 scénarios V1 (`factory_v2/chaos_drill.py`) ne sont pas touchés.
"""
from __future__ import annotations

import importlib.util
import json
import os
import random
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from factory_v2.chaos_drill import (  # noqa: E402 — l'environnement hermétique est posé là
    _REPO_ROOT, BLOCKING_EVENT_MAX_REPEATS, MAX_CYCLES, _WAITING_E2E, Run, StubExecutor,
    _backdate_ledger, _chain, _count_event, _finish, _git, _has_event, _local_milestone,
    _recovered_on_settle, _settled_milestones, make_disposable_project)
from factory_v2.executors import ExecutionResult
from factory_v2.project_authority import load_project_state, save_project_state
from factory_v2.runtime import advance

_BIN_DIR = _REPO_ROOT / "factory" / "bin"
_LIB_DIR = _REPO_ROOT / "factory" / "lib"


# ═══════════════════════════════ utilitaires ══════════════════════════════════

def _marker() -> str:
    """Marqueur unique par exécution : `pgrep -f` ne peut voir QUE nos processus."""
    return "CHAOS_V2_%08x" % random.getrandbits(32)


def _pgrep(marker: str) -> list:
    r = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True, timeout=10)
    return [p for p in r.stdout.split() if p.strip() and int(p) != os.getpid()]


def _hung_tree_cmd(marker: str, seconds: int = 600) -> str:
    """Un enfant Python à 0 % CPU qui laisse lui-même un petit-enfant endormi —
    exactement l'arbre qu'un `test_cmd` ou un moteur pendu laisse derrière lui."""
    inner = f"import time; time.sleep({seconds})  # {marker}"
    outer = (f"import subprocess, time; subprocess.Popen(['python3', '-c', {inner!r}]); "
             f"time.sleep({seconds})  # {marker}")
    return f"python3 -c {json.dumps(outer)}"


def _load_dispatcher():
    spec = importlib.util.spec_from_file_location(
        "chaos_v2_dispatcher_v3", _BIN_DIR / "dispatcher_v3.py")
    d3 = importlib.util.module_from_spec(spec)
    sys.modules["chaos_v2_dispatcher_v3"] = d3
    spec.loader.exec_module(d3)
    return d3


def _load_factoryd():
    if str(_BIN_DIR) not in sys.path:
        sys.path.insert(0, str(_BIN_DIR))
    import factoryd
    return factoryd


def _iso_in(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ledger_rows(pd: Path) -> list:
    ledger = pd / "evidence" / "attempt_accounts.jsonl"
    rows = []
    if ledger.is_file():
        for line in ledger.read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    return rows


# ═══════════════ 8. PARKED_WITHOUT_MILESTONE ═══════════════════════════════════

def scenario_parked_without_milestone(root: Path) -> dict:
    """Parking (quota/owner) SANS `parked_milestone` : l'id est dérivé de la clé de
    mission (milestone_selection.py) — le jalon n'est pas resélectionné avant
    `resets_at`, le jalon suivant prend la main."""
    sid = "PARKED_WITHOUT_MILESTONE"
    pd, _ = make_disposable_project(root, [_local_milestone("A"), _local_milestone("B")])
    resets_at = _iso_in(3600)
    st = load_project_state(pd)
    # injection : l'entrée de parking telle que la nuit l'a laissée — pas de parked_milestone
    st.setdefault("missions", {})["A#M1"] = {
        "state": "MILESTONE_PARKED", "parked_until": resets_at, "resets_at": resets_at,
        "parked_reason": "quota window exhausted (claude)", "failure_fingerprint_history": []}
    save_project_state(pd, st)
    injected = "parked_milestone" not in st["missions"]["A#M1"]
    stub = StubExecutor(pd, lambda m, a, p, s: s.ok(m, engine=p))
    run = Run(pd, stub)
    for _ in range(MAX_CYCLES):
        run.cycle()
        if "B" in _settled_milestones(run.events()):
            break
    evs = run.events()
    rows = _ledger_rows(pd)
    a_attempts = sorted({str(r.get("attempt_key")) for r in rows
                         if str(r.get("milestone")) == "A" and r.get("attempt_key")})
    b_settled = "B" in _settled_milestones(evs)
    parked_again = _count_event(evs, "MILESTONE_PARKED", milestone="A")
    still_parked = (load_project_state(pd).get("missions", {}).get("A#M1", {})
                    .get("state") == "MILESTONE_PARKED")
    recovered = b_settled and not a_attempts and still_parked and parked_again == 0
    chain = _chain(evs, "MISSION_PASS", "MILESTONE_SETTLED", "MILESTONE_PARKED", "LAUNCH_BLOCKED")
    evidence = (f"A#M1 parqué sans parked_milestone jusqu'à {resets_at} → tentatives A={len(a_attempts)}, "
                f"MILESTONE_PARKED ré-émis×{parked_again}, A toujours parqué={still_parked} ; "
                f"jalon B pris à la place : {chain}")
    return _finish(run, sid, injected, recovered, evidence)


# ═══════════════ 4. IDLE_GUARD_NEVER_LAUNCHED ══════════════════════════════════

def scenario_idle_guard_never_launched(root: Path) -> dict:
    """La garde idle (`_idle_guard_tripped`) ne doit JAMAIS céder un jalon jamais lancé
    (last_mine == 0.0), même si le projet n'a rien lancé depuis 30 min."""
    sid = "IDLE_GUARD_NEVER_LAUNCHED"
    from factory_v2.advance.milestone_selection import _idle_guard_tripped
    waiting = {"product_e2e": {"test_command": _WAITING_E2E.format(mid="A")}}
    pd, _ = make_disposable_project(root, [_local_milestone("A", waiting),
                                           _local_milestone("B"), _local_milestone("C")])
    stub = StubExecutor(pd, lambda m, a, p, s: s.ok(m, engine=p))
    run = Run(pd, stub)
    run.cycle()                       # A lancé une fois, preuve au calendrier → en attente
    # injection : 31 min sans AUCUN lancement ; B et C n'ont JAMAIS été lancés
    _backdate_ledger(pd, 31 * 60 + 60)
    rows = _ledger_rows(pd)
    launched = {str(r.get("milestone")) for r in rows if r.get("engine") not in (None, "", "local")}
    injected = ("A" in launched) and not ({"B", "C"} & launched)
    # la primitive elle-même, interrogée sur un jalon jamais lancé
    ctx_never = SimpleNamespace(project_dir=pd, mid="C", state=load_project_state(pd))
    ctx_stale = SimpleNamespace(project_dir=pd, mid="A", state=load_project_state(pd))
    guard_never = _idle_guard_tripped(ctx_never)     # attendu : False
    guard_stale = _idle_guard_tripped(ctx_stale)     # attendu : True (A est bien immobile)
    for _ in range(MAX_CYCLES - 1):
        run.cycle()
        if "B" in _settled_milestones(run.events()) or "C" in _settled_milestones(run.events()):
            break
    evs = run.events()
    yields_on_never = (_count_event(evs, "IDLE_GUARD_TRIPPED", milestone="B")
                       + _count_event(evs, "IDLE_GUARD_TRIPPED", milestone="C"))
    yields_total = _count_event(evs, "STUCK_MILESTONE_YIELDED")
    settled = _settled_milestones(evs)
    recovered = (guard_never is False and guard_stale is True and yields_on_never == 0
                 and bool({"B", "C"} & set(settled)) and yields_total <= BLOCKING_EVENT_MAX_REPEATS)
    chain = _chain(evs, "MILESTONE_WAITING_FOR_EVIDENCE", "IDLE_GUARD_TRIPPED",
                   "STUCK_MILESTONE_YIELDED", "MISSION_PASS", "MILESTONE_SETTLED")
    evidence = (f"_idle_guard_tripped(jamais lancé C)={guard_never}, (A immobile 31 min)={guard_stale} ; "
                f"IDLE_GUARD_TRIPPED sur B/C×{yields_on_never}, STUCK_MILESTONE_YIELDED×{yields_total} ; "
                f"réglés={settled} ; {chain}")
    return _finish(run, sid, injected, recovered, evidence)


# ═══════════════ 5. INHERITED_WORKTREE_META ════════════════════════════════════

def scenario_inherited_worktree_meta(root: Path) -> dict:
    """`.factory_worktree_meta.json` d'une AUTRE mission committé à HEAD : un worktree
    neuf l'extrait (ou un reset/refresh le rétablit). `recover_worktree` doit ignorer
    ce meta hérité et rendre la branche réellement extraite ; l'archivage réussit du
    premier coup (la nuit : 8 échecs rapides à archiver la branche d'ORPHAN)."""
    sid = "INHERITED_WORKTREE_META"
    if str(_LIB_DIR) not in sys.path:
        sys.path.insert(0, str(_LIB_DIR))
    from worktree_manager import META_FILENAME, WorktreeManager

    repo = root / "control_repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("base\n")
    orphan_meta = {"mission_id": "ORPHAN#M1", "path": str(root / "wt" / "ORPHAN#M1"),
                   "branch": "candidate/ORPHAN#M1", "created_at": "2026-09-19T20:00:00+00:00",
                   "base_commit": "0" * 40}
    (repo / META_FILENAME).write_text(json.dumps(orphan_meta))   # committé par une capture
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base + meta d'ORPHAN committé par la capture")
    wm = WorktreeManager(repo_root=repo, worktrees_base=root / "wt")
    info = wm.create_worktree("A#M1")
    # injection : le meta committé à HEAD revient dans le worktree (checkout/reset/refresh)
    _git(info.path, "checkout", "--", META_FILENAME)
    inherited = json.loads((info.path / META_FILENAME).read_text())
    injected = inherited.get("mission_id") == "ORPHAN#M1"
    actual_branch = _git(info.path, "branch", "--show-current").stdout.strip()

    t0 = time.monotonic()
    rec = wm.recover_worktree("A#M1")
    recovered_ok = (rec is not None and rec.mission_id == "A#M1"
                    and Path(rec.path) == Path(info.path) and rec.branch == actual_branch)
    archive_failures = 0
    archived = None
    try:
        archived = wm.archive_worktree(rec) if rec is not None else None
    except Exception as exc:                        # noqa: BLE001
        archive_failures += 1
        archive_err = f"{type(exc).__name__}: {str(exc)[:120]}"
    else:
        archive_err = ""
    archived_ok = (archived is not None and Path(archived.path).exists()
                   and _git(repo, "rev-parse", "--verify", "-q",
                            f"refs/heads/{archived.branch}", check=False).returncode == 0)
    recovered = recovered_ok and archived_ok and archive_failures == 0
    run = Run(root)                                   # primitives directes : pas de boucle advance
    run.cycle = lambda: None                          # type: ignore[method-assign]
    run.t0 = t0
    evidence = (f"meta hérité mission_id={inherited.get('mission_id')} branch={inherited.get('branch')} "
                f"→ recover_worktree: mission_id={getattr(rec, 'mission_id', None)} "
                f"branch={getattr(rec, 'branch', None)} (réelle={actual_branch}) ; "
                f"archive_worktree: échecs={archive_failures}{(' ' + archive_err) if archive_err else ''}, "
                f"branche archivée={getattr(archived, 'branch', None)}")
    return _finish(run, sid, injected, recovered, evidence)


# ═══════════════ 7. HUNG_TEST_SUBPROCESS ═══════════════════════════════════════

def scenario_hung_test_subprocess(root: Path) -> dict:
    """Le test/commande d'une étape laisse un enfant + petit-enfant à 0 % CPU au-delà du
    timeout : le groupe entier est tué (start_new_session + terminate_process_group /
    setsid + killpg), l'essai finit FAIL en secondes, l'essai suivant passe."""
    sid = "HUNG_TEST_SUBPROCESS"
    marker = _marker()
    d3 = _load_dispatcher()
    # (a) la primitive du dispatcher qui exécute `test_cmd` (_run_test → _run, killpg SIGKILL)
    t0 = time.monotonic()
    rc, _out, err = d3._run(["bash", "-c", f"set -eo pipefail\n{_hung_tree_cmd(marker)}"],
                            cwd=root, timeout=2)
    d3_s = time.monotonic() - t0
    time.sleep(0.3)
    d3_left = _pgrep(marker)
    d3_ok = rc == -1 and "TIMEOUT" in err and d3_s < 30 and not d3_left

    # (b) la même panne dans la vraie boucle advance() : étape local_command, budget 2 s
    marker2 = _marker()
    pd, _ = make_disposable_project(root, [_local_milestone("A")])
    second = pd / "second_try"
    cmd = (f"if test -f {second}; then mkdir -p out && touch out/A.txt && exit 0; fi; "
           f"touch {second}; {_hung_tree_cmd(marker2)}")
    import yaml
    roadmap = yaml.safe_load((pd / "ROADMAP.yaml").read_text())
    roadmap["milestones"][0]["execution"]["steps"] = [
        {"kind": "implement", "executor": {"type": "local_command", "command": cmd,
                                           "cwd": "project_dir", "budget_s": 2}}]
    (pd / "ROADMAP.yaml").write_text(yaml.safe_dump(roadmap, sort_keys=False))
    run = Run(pd, None)                               # exécuteur RÉEL (LocalCommandExecutor)
    t1 = time.monotonic()
    run.cycle()
    first_s = time.monotonic() - t1
    evs = run.events()
    # LocalCommandExecutor classe le TimeoutExpired en rc=124 (jamais une exception)
    failed_fast = _has_event(evs, "MISSION_FAIL", mission="A#M1", rc=124) and first_s < 60
    time.sleep(0.3)
    left = _pgrep(marker2)
    injected = second.is_file() and (failed_fast or first_s >= 60)
    fail_detail = str(getattr(run.results[0], "detail", ""))[:80]
    for _ in range(MAX_CYCLES - 1):
        run.cycle()
        if "A" in _settled_milestones(run.events()):
            break
    settled, chain = _recovered_on_settle(run, "A")
    recovered = d3_ok and failed_fast and not left and settled
    evidence = (f"dispatcher _run: rc={rc} TIMEOUT={('TIMEOUT' in err)} en {d3_s:.1f}s, survivants={d3_left} ; "
                f"advance(): MISSION_FAIL en {first_s:.1f}s ({fail_detail!r}), survivants={left} ; {chain}")
    return _finish(run, sid, injected, recovered, evidence)


# ═══════════════ 2. ENGINE_BEYOND_BUDGET ═══════════════════════════════════════

def scenario_engine_beyond_budget(root: Path) -> dict:
    """Un moteur (stub qui dort, avec un petit-enfant) dépasse `budget_s` : à la frontière
    execve du dispatcher (_run : setsid + killpg SIGKILL sur TimeoutExpired) l'arbre entier
    est tué, aucun zombie ; côté runtime l'essai est fermé ATTEMPT_INTERRUPTED (stage rc<0)
    et une nouvelle tentative règle le jalon."""
    sid = "ENGINE_BEYOND_BUDGET"
    marker = _marker()
    d3 = _load_dispatcher()
    budget_s = 2
    t0 = time.monotonic()
    rc, _out, err = d3._run(["bash", "-c", _hung_tree_cmd(marker)], cwd=root, timeout=budget_s)
    kill_s = time.monotonic() - t0
    time.sleep(0.3)
    left = _pgrep(marker)
    tree_killed = rc == -1 and "TIMEOUT" in err and kill_s < 30 and not left

    def engine_timeout(mission, engine="glm") -> ExecutionResult:
        # le reçu que dispatcher_v3 écrit quand _run a tué le build à budget_s (stage rc=-1)
        return ExecutionResult(
            mission_id=mission.id, rc=1, engine=engine,
            output_tail=f"TIMEOUT: engine {engine} exceeded budget_s={budget_s} — process group killed",
            receipt={"verdict": "INTERRUPTED",
                     "stages": [{"step": f"build_{engine}", "rc": -1, "status": "TIMEOUT"}],
                     "engine_executions": [{"engine": engine, "rc": -1, "launched": True}]},
            engine_execution={"engine_id": engine, "observed": True,
                              "argv0": "chaos-stub", "pid": os.getpid()})

    pd, _ = make_disposable_project(root, [_local_milestone("A")])

    def script(mission, attempt, preferred, stub):
        if attempt == 1:
            return engine_timeout(mission, engine=preferred or "glm")
        return stub.ok(mission, engine=preferred)

    stub = StubExecutor(pd, script)
    run = Run(pd, stub)
    run.t0 = t0
    for _ in range(MAX_CYCLES):
        run.cycle()
        if "A" in _settled_milestones(run.events()):
            break
    evs = run.events()
    interrupted = _count_event(evs, "ATTEMPT_INTERRUPTED", milestone="A", interruption="KILL")
    settled, chain = _recovered_on_settle(run, "A")
    attempts = sorted({str(r.get("attempt_key")) for r in _ledger_rows(pd)
                       if str(r.get("milestone")) == "A" and r.get("attempt_key")})
    injected = tree_killed and interrupted >= 1
    recovered = (tree_killed and settled and 1 <= interrupted <= BLOCKING_EVENT_MAX_REPEATS
                 and len(attempts) >= 2)
    evidence = (f"_run(budget {budget_s}s): rc={rc} TIMEOUT en {kill_s:.1f}s, survivants pgrep={left} ; "
                f"ATTEMPT_INTERRUPTED(KILL)×{interrupted}, tentatives={len(attempts)} ; {chain}")
    return _finish(run, sid, injected, recovered, evidence)



# ═══════════════ 1. DAEMON_LOOP_SILENT ═════════════════════════════════════════

def _silent_daemon_child_main(project_dir: str, run_dir: str, watchdog_s: float) -> int:
    """Enfant du scénario : le VRAI `factoryd.run_daemon_loop` (mode série) dont
    l'advance() ouvre une tentative puis bloque à jamais dans le moteur stub — la
    boucle n'itère plus. Seul `_loop_watchdog` peut faire sortir ce processus."""
    factoryd = _load_factoryd()
    pd = Path(project_dir)
    never = threading.Event()

    def script(mission, attempt, preferred, stub):
        never.wait(3600)                          # la future ne rend jamais la main
        return stub.semantic_fail(mission, engine=preferred)

    stub = StubExecutor(pd, script)
    factoryd._cto_startup_check = lambda *a, **k: True
    sys.modules["factory_v2.continuation_controller"] = None   # type: ignore[assignment]
    factoryd._advance_v2 = lambda p, log: str(advance(p, executor=stub).action)
    factoryd.LOOP_WATCHDOG_S = float(watchdog_s)
    real_sleep = time.sleep
    factoryd.time.sleep = lambda s: real_sleep(min(s, 0.2))   # tic du watchdog 60 s → 0,2 s
    rd = Path(run_dir)
    rd.mkdir(parents=True, exist_ok=True)
    factoryd.run_daemon_loop(rd / "campaign.json", rd, poll_interval_s=0.1, max_polls=5,
                             v2_project_dir=pd, idle_poll_interval_s=1)
    return 0


def scenario_daemon_loop_silent(root: Path) -> dict:
    """La boucle du daemon n'itère plus (future bloquée) : `_loop_watchdog` doit sortir
    en rc 3 (`LOOP_WATCHDOG_TRIPPED`), et la reprise ne relance pas deux fois la même
    mission : l'OPEN orphelin est réconcilié (KILL) et une seule reprise règle le jalon."""
    sid = "DAEMON_LOOP_SILENT"
    pd, _ = make_disposable_project(root, [_local_milestone("A")])
    run_dir = root / "run"
    watchdog_s = 3.0
    env = {**os.environ, "PYTHONPATH": str(_REPO_ROOT), "FACTORY_MAX_WORKERS": "1"}
    env["FACTORY_RUN_DIR"] = str(run_dir)
    t0 = time.monotonic()
    child = subprocess.Popen(
        [sys.executable, "-c",
         "import factory_v2.chaos_drill_v2 as m; import sys; "
         "sys.exit(m._silent_daemon_child_main(%r, %r, %r))" % (str(pd), str(run_dir), watchdog_s)],
        cwd=str(_REPO_ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        out, err = child.communicate(timeout=120)
    except subprocess.TimeoutExpired:
        child.kill()
        out, err = child.communicate()
    exit_s = time.monotonic() - t0
    tripped = "LOOP_WATCHDOG_TRIPPED" in err
    rows = _ledger_rows(pd)
    opened_in_child = any(r.get("phase") == "OPEN" and str(r.get("mission")) == "A#M1" for r in rows)
    injected = opened_in_child and child.returncode == 3 and tripped

    # reprise : le daemon suivant (ici la boucle advance) reprend seul
    stub = StubExecutor(pd, lambda m, a, p, s: s.ok(m, engine=p))
    run = Run(pd, stub)
    run.t0 = t0
    for _ in range(MAX_CYCLES):
        run.cycle()
        if "A" in _settled_milestones(run.events()):
            break
    evs = run.events()
    settled, chain = _recovered_on_settle(run, "A")
    rows = _ledger_rows(pd)
    a1 = [r for r in rows if str(r.get("mission")) == "A#M1"]
    kills = [r for r in a1 if r.get("phase") == "TERMINAL" and str(r.get("exit_class")) == "KILL"]
    attempts = {r.get("attempt_key") for r in a1 if r.get("attempt_key")}
    no_double = len(kills) == 1 and len(attempts) == 2
    recovered = injected and settled and no_double and _has_event(evs, "LOST_ATTEMPTS_RECONCILED")
    evidence = (f"daemon enfant: rc={child.returncode} en {exit_s:.1f}s, LOOP_WATCHDOG_TRIPPED={tripped} "
                f"(seuil {watchdog_s:.0f}s) ; reprise: {chain} ; orphelin fermé KILL×{len(kills)}, "
                f"tentatives={len(attempts)} (pas de double lancement={no_double})")
    return _finish(run, sid, injected, recovered, evidence)


# ═══════════════ 3. RELOAD_RACE_FRESH_SUBMIT ═══════════════════════════════════

def scenario_reload_race_fresh_submit(root: Path) -> dict:
    """RELOAD_NOW tombe dans le poll qui vient de soumettre un advance (future jeune,
    rien encore en vol) : la sortie doit s'ARMER (`_arm_reload`), jamais sortir tant que
    la future jeune tourne (`_armed_exit_allowed`), se DÉSARMER quand cette future lance
    un moteur (`_disarm_reload`), et ne sortir (SystemExit 3) que sur pool vide."""
    sid = "RELOAD_RACE_FRESH_SUBMIT"
    from factory_v2.mission_evidence import (clear_worker_state, get_process_birth_time,
                                             write_heartbeat)
    factoryd = _load_factoryd()
    pd, _ = make_disposable_project(root, [_local_milestone("A")])
    run_dir = root / "run"
    run_dir.mkdir()
    # le pool de futures n'existe qu'en mode PORTEFEUILLE (factoryd: _advance_parallel)
    portfolio = root / "portfolio.json"
    portfolio.write_text(json.dumps({"schema": "portfolio/v1",
                                     "projects": [{"project_dir": pd.name}]}))
    real_sleep = time.sleep
    log_lines: list = []
    marks: dict = {}
    calls: list = []

    def fake_advance_v2(p, log):
        n = len(calls)
        calls.append(time.monotonic())
        if n == 0:
            # règlement d'un jalon factory touchant du code daemon → reload programmé
            factoryd._ADVANCE_CALL_META.detail = "settled"
            factoryd._ADVANCE_CALL_META.touched = ["factory_v2/runtime.py"]
            return "MILESTONE_SETTLED"
        if n == 1:
            # la future JEUNE soumise dans le poll du RELOAD_NOW : quelques instants
            # plus tard elle lance un moteur (mission en vol + heartbeat), puis finit
            real_sleep(0.4)
            st = load_project_state(pd)
            st.setdefault("missions", {})["A#M1"] = {"state": "IN_FLIGHT"}
            save_project_state(pd, st)
            write_heartbeat(pd, "A#M1", pid=os.getpid(), phase="build",
                            birth_time=get_process_birth_time(os.getpid()))
            marks["engine_started"] = time.monotonic()
            real_sleep(0.8)
            clear_worker_state(pd, "A#M1")
            st = load_project_state(pd)
            st["missions"]["A#M1"] = {"state": "PASS"}
            save_project_state(pd, st)
            marks["engine_done"] = time.monotonic()
            factoryd._ADVANCE_CALL_META.detail = "dispatched"
            factoryd._ADVANCE_CALL_META.touched = []
            return "MISSION_DISPATCHED"
        factoryd._ADVANCE_CALL_META.detail = ""
        factoryd._ADVANCE_CALL_META.touched = []
        return "IDLE"

    def capture_log(msg: str) -> None:
        log_lines.append((time.monotonic(), str(msg)))

    saved = {k: getattr(factoryd, k) for k in (
        "_cto_startup_check", "_advance_v2", "_is_factory_project", "_canary_allows_reload",
        "_interruptible_sleep", "_log", "_RELOAD_DRAINING", "_RELOAD_DRAIN_PROJECTS",
        "_RELOAD_DRAIN_STARTED", "_RELOAD_ARMED", "_ADVANCE_POOL", "CTO_EVENTS_PATH")}
    saved_futures = dict(factoryd._ADVANCE_FUTURES)
    saved_started = dict(factoryd._ADVANCE_STARTED)
    saved_env = {k: os.environ.get(k) for k in ("FACTORY_MAX_WORKERS", "FACTORY_ADVANCES_PER_PROJECT")}
    saved_cc = sys.modules.get("factory_v2.continuation_controller", "<absent>")
    exit_code = None
    t0 = time.monotonic()
    try:
        os.environ["FACTORY_MAX_WORKERS"] = "2"          # mode parallèle : pool + futures
        os.environ["FACTORY_ADVANCES_PER_PROJECT"] = "1"
        factoryd._ADVANCE_FUTURES.clear()
        factoryd._ADVANCE_STARTED.clear()
        factoryd._ADVANCE_POOL = None
        factoryd.CTO_EVENTS_PATH = run_dir / "journals" / "CTO_EVENTS.jsonl"
        factoryd._RELOAD_DRAINING = False
        factoryd._RELOAD_DRAIN_PROJECTS = []
        factoryd._RELOAD_ARMED = False
        factoryd._cto_startup_check = lambda *a, **k: True
        factoryd._advance_v2 = fake_advance_v2
        factoryd._is_factory_project = lambda p: True
        factoryd._canary_allows_reload = lambda *a, **k: True
        factoryd._interruptible_sleep = lambda s, *a, **k: real_sleep(0.05)
        factoryd._log = capture_log
        sys.modules["factory_v2.continuation_controller"] = None   # type: ignore[assignment]
        try:
            factoryd.run_daemon_loop(run_dir / "campaign.json", run_dir, poll_interval_s=0.05,
                                     max_polls=400, portfolio_path=str(portfolio),
                                     idle_poll_interval_s=1)
        except SystemExit as exc:
            exit_code = exc.code
        exit_at = time.monotonic()
        futures_at_exit = dict(factoryd._ADVANCE_FUTURES)
    finally:
        for k, v in saved.items():
            setattr(factoryd, k, v)
        factoryd._ADVANCE_FUTURES.clear()
        factoryd._ADVANCE_FUTURES.update(saved_futures)
        factoryd._ADVANCE_STARTED.clear()
        factoryd._ADVANCE_STARTED.update(saved_started)
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if saved_cc == "<absent>":
            sys.modules.pop("factory_v2.continuation_controller", None)
        else:
            sys.modules["factory_v2.continuation_controller"] = saved_cc

    def first(prefix: str):
        return next((t for t, m in log_lines if m.startswith(prefix)), None)

    t_armed = first("RELOAD_ARMED")
    t_disarmed = first("RELOAD_DISARMED")
    t_now = first("RELOAD_NOW")
    starts = [t for t, m in log_lines if m.startswith("PARALLEL_ADVANCE_STARTED")]
    started_while_armed = [t for t in starts
                           if t_armed is not None and t_armed <= t <= (t_disarmed or float("inf"))]
    injected = (t_armed is not None and len(calls) >= 2
                and t_armed >= calls[1] - 0.5)            # armé alors que la future jeune tourne
    recovered = (exit_code == 3 and t_now is not None and t_armed is not None
                 and t_disarmed is not None and t_armed < t_disarmed < t_now
                 and "engine_done" in marks and marks["engine_done"] <= t_now
                 and not futures_at_exit and not started_while_armed)
    run = Run(pd)
    run.cycle = lambda: None                              # type: ignore[method-assign]
    run.t0 = t0
    run.results = [None] * len(calls)
    evidence = (f"journal daemon: RELOAD_ARMED@{(t_armed or 0) - t0:.2f}s → RELOAD_DISARMED@"
                f"{(t_disarmed or 0) - t0:.2f}s (moteur en vol@{marks.get('engine_started', 0) - t0:.2f}s) "
                f"→ RELOAD_NOW@{(t_now or 0) - t0:.2f}s après fin de la future@"
                f"{marks.get('engine_done', 0) - t0:.2f}s ; SystemExit={exit_code}, futures à la sortie="
                f"{len(futures_at_exit)}, soumissions pendant l'armement={len(started_while_armed)}")
    return _finish(run, sid, injected, recovered, evidence)


# ═══════════════ 6. STALE_QUARANTINE_AFTER_FIX ═════════════════════════════════

def _quarantined_product(root: Path) -> tuple:
    """Produit dont l'artefact d'acceptation est faux (le bug), avec trois preuves
    DISTINCTES dérivables de l'autorité (reproduction / régression / validation)."""
    import textwrap
    prod = root / "product"
    (prod / "research").mkdir(parents=True)
    (prod / "research" / "manifest.json").write_text(json.dumps({"accounts": [], "corpus_sha": ""}))
    (prod / "verify_manifest.py").write_text(textwrap.dedent("""\
        import json, sys
        d = json.load(open("research/manifest.json"))
        ok = len(d.get("accounts") or []) >= 2 and bool(d.get("corpus_sha"))
        print("MANIFEST " + ("OK" if ok else "FAIL"))
        sys.exit(0 if ok else 1)
        """))
    (prod / "regression_suite.py").write_text(textwrap.dedent("""\
        import json
        json.load(open("research/manifest.json"))
        print("REGRESSIONS OK")
        """))
    _git(prod, "init", "-q", "-b", "main")
    _git(prod, "add", "-A")
    _git(prod, "commit", "-qm", "product base (bug: empty manifest)")
    c0 = _git(prod, "rev-parse", "HEAD").stdout.strip()

    pd = root / "project"
    (pd / "evidence" / "recovery").mkdir(parents=True)
    (pd / "tasks").mkdir()
    (pd / "tasks" / "MS.md").write_text("# MS\nManifeste véridique.\n")
    (pd / "STRATEGY.md").write_text("# Strategy\nNorth Star: a truthful manifest.\n")
    (pd / "SPEC.md").write_text("# Spec\nThe manifest lists real accounts.\n")
    import yaml
    (pd / "ROADMAP.yaml").write_text(yaml.safe_dump({
        "version": "v3", "project": "qstale", "product_repo": str(prod),
        "milestones": [{
            "id": "MS", "goal": "build a truthful account manifest",
            "deliverables": ["research/manifest.json"],
            "acceptance": ["at least 2 accounts in the manifest", "corpus sha recorded"],
            "product_e2e": {"test_command": "python3 verify_manifest.py"},
            "execution": {"steps": [{"kind": "implement", "executor": {
                "type": "autopilot", "task_file": str(pd / "tasks" / "MS.md"),
                "test_cmd": "python3 regression_suite.py"}}]},
        }]}, sort_keys=False))
    from factory_v2.project_authority import load_authority
    from factory_v2.validation_contract import freeze_contract
    authority = load_authority(pd)
    contract = freeze_contract(pd, authority, authority.roadmap["milestones"][0])
    save_project_state(pd, {
        "schema": "project-state/v2", "project": "qstale",
        "completed_milestones": {"MS": {"state": "COMPLETE", "contract_id": contract["contract_id"],
                                        "settled_at": "2026-09-19T20:00:00Z"}},
        "validation_contracts": {"MS": {"contract_id": contract["contract_id"], "status": "FROZEN",
                                        "frozen_at": "2026-09-19T19:59:00Z"}},
        "current_milestone": "MS", "missions": {}, "escalation_ladder": {}, "events": [],
    })
    from factory_v2.safety_quarantine import enter_quarantine
    enter_quarantine(pd, cause="FALSE_PRODUCT_SETTLEMENT", milestone_id="MS", mission_id="MS#M1",
                     failure_fingerprint="MANIFEST FAIL: 0 accounts", candidate_sha=c0)
    return pd, prod, c0


def scenario_stale_quarantine_after_fix(root: Path) -> dict:
    """Quarantaine posée sur un règlement faux ; le plan de contrôle gèle le contrat et
    observe la reproduction ROUGE à C0. Puis le correctif atterrit à C1 (la porte redevient
    verte) alors qu'aucun moteur n'est disponible pour DIAGNOSE/SAFE_REPAIR. La
    quarantaine doit être levée par le plan de contrôle SEUL (porte VALIDATE_REPAIR aux
    SHA exacts), sans qu'un humain touche SAFETY_QUARANTINE.json."""
    sid = "STALE_QUARANTINE_AFTER_FIX"
    from factory_v2.safety_quarantine import is_in_quarantine
    pd, prod, c0 = _quarantined_product(root)

    def script(mission, attempt, preferred, stub):
        # la nuit : aucun moteur disponible pour l'échelle (quota coupé)
        return stub.quota_cut(mission, engine=preferred or "glm")

    stub = StubExecutor(pd, script, product_repo=prod)
    run = Run(pd, stub)
    run.cycle()                       # contrat gelé à C0, baseline observée, échelle lancée
    evs = run.events()
    frozen = _has_event(evs, "QUARANTINE_CONTRACT_FROZEN")
    baseline_fail = _has_event(evs, "QUARANTINE_BASELINE_CAPTURED", result="FAIL")
    # injection : le correctif atterrit dans le produit (fusion d'une autre mission)
    import hashlib
    accounts = ["alice", "bob", "carol"]
    (prod / "research" / "manifest.json").write_text(json.dumps(
        {"accounts": accounts, "corpus_sha": hashlib.sha256(",".join(accounts).encode()).hexdigest()}))
    _git(prod, "add", "-A")
    _git(prod, "commit", "-qm", "fix: truthful manifest (merged by another mission)")
    c1 = _git(prod, "rev-parse", "HEAD").stdout.strip()
    gate_green = subprocess.run(["python3", "verify_manifest.py"], cwd=prod,
                                capture_output=True, text=True).returncode == 0
    injected = frozen and baseline_fail and gate_green and c1 != c0 and is_in_quarantine(pd)
    for _ in range(MAX_CYCLES - 1):
        run.cycle()
        if not is_in_quarantine(pd):
            break
    evs = run.events()
    cleared = _has_event(evs, "SAFETY_QUARANTINE_CLEARED")
    lifted = cleared and not is_in_quarantine(pd)
    cleared_ev = next((e for e in evs if isinstance(e, dict) and e.get("event") == "SAFETY_QUARANTINE_CLEARED"), {})
    repair_ok = str(cleared_ev.get("repair_sha") or "") and c1.startswith(str(cleared_ev.get("repair_sha")))
    # la levée est PROUVÉE par la porte : reçu écrit par le plan de contrôle, chaque
    # observation exécutée aux SHA exacts (reproduction ROUGE à C0, VERTE à C1)
    from factory_v2.safety_quarantine import SafetyClearReceipt
    receipt = SafetyClearReceipt.load(pd)
    gate = {o.get("phase"): o.get("result") for o in (getattr(receipt, "gate_observations", None) or [])}
    gate_ok = (receipt is not None and receipt.reproduction_before_result == "FAIL"
               and gate.get("reproduction_after") == "PASS"
               and gate.get("relevant_regressions") == "PASS"
               and gate.get("independent_validation") == "PASS"
               and receipt.repair_sha == c1)
    recovered = injected and lifted and bool(repair_ok) and gate_ok
    chain = _chain(evs, "QUARANTINE_CONTRACT_FROZEN", "QUARANTINE_BASELINE_CAPTURED",
                   "QUARANTINE_DIAGNOSE_CREATED", "QUARANTINE_REPAIR_DISPATCHED",
                   "RECOVERY_WORK_FAILED", "QUARANTINE_GATE_PROBED", "SAFETY_CLEAR_REJECTED",
                   "SAFETY_QUARANTINE_CLEARED", "INTERRUPTED_MILESTONE_REOPENED",
                   "QUARANTINE_STALLED", "QUARANTINE_REPLAN_SCHEDULED")
    actions = [str(getattr(r, "action", "")) for r in run.results]
    evidence = (f"contrat gelé à {c0[:8]} (baseline FAIL={baseline_fail}), correctif à {c1[:8]} "
                f"(porte verte={gate_green}) ; cycles={actions[-6:]} ; {chain} ; "
                f"levée={lifted} repair_sha={cleared_ev.get('repair_sha', '')} ; reçu de porte: "
                f"before={getattr(receipt, 'reproduction_before_result', None)} {gate}")
    return _finish(run, sid, injected, recovered, evidence)


_SCENARIOS_V2 = {
    "DAEMON_LOOP_SILENT": scenario_daemon_loop_silent,
    "ENGINE_BEYOND_BUDGET": scenario_engine_beyond_budget,
    "RELOAD_RACE_FRESH_SUBMIT": scenario_reload_race_fresh_submit,
    "IDLE_GUARD_NEVER_LAUNCHED": scenario_idle_guard_never_launched,
    "INHERITED_WORKTREE_META": scenario_inherited_worktree_meta,
    "STALE_QUARANTINE_AFTER_FIX": scenario_stale_quarantine_after_fix,
    "HUNG_TEST_SUBPROCESS": scenario_hung_test_subprocess,
    "PARKED_WITHOUT_MILESTONE": scenario_parked_without_milestone,
}
