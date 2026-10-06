#!/usr/bin/env python3
"""factoryd — the factory's persistent supervisor: poll loop (run_daemon_loop), parallel
advances (_advance_parallel), safe reload (drain → armed → pool empty → canary → exit 3),
loop watchdog, orphan-engine sweep (hermetic = own registry only), heartbeat/metrics,
quota cap.  `python3 factory/bin/factoryd.py start|stop|status|restart`."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _BIN_DIR.parents[1]
_LIB_DIR = _REPO_ROOT / "factory" / "lib"
_CTO_DIR = _REPO_ROOT / "factory" / "cto"
for _p in (str(_REPO_ROOT), str(_BIN_DIR), str(_LIB_DIR), str(_CTO_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import supervisor_state as sstate          # noqa: E402
import completion_detector as cdet          # noqa: E402
from factory_v2._io import _atomic_write_text, _now

# Mutable runtime state lives OUTSIDE the repo (launchd passes --run-dir; FACTORY_RUN_DIR overrides).
DEFAULT_RUN_DIR = Path(os.environ.get("FACTORY_RUN_DIR", "").strip()
                       or str(Path.home() / "Library/Application Support/JocelynFactory/run"))
DEFAULT_CAMPAIGN_STATE = _REPO_ROOT / "factory" / "campaigns" / "app007" / "APP007_CAMPAIGN_STATE.json"
CTO_EVENTS_PATH = Path(os.environ.get("FACTORY_CTO_EVENTS_PATH")
                       or str(DEFAULT_RUN_DIR / "journals" / "CTO_EVENTS.jsonl"))
CTO_STATUS_LATEST_PATH = _CTO_DIR / "CTO_STATUS_LATEST.md"        # control-plane files the
PORTFOLIO_STATE_PATH = _CTO_DIR / "PORTFOLIO_STATE.json"           # test harness redirects
CANDIDATE_REGISTRY_PATH = Path(os.environ.get("FACTORY_CANDIDATE_REGISTRY_PATH", "").strip()
                               or str(DEFAULT_RUN_DIR / "projects/factory-control/evidence/factory-v1/candidate_registry.json"))
FAILURE_LOG_PATH = DEFAULT_RUN_DIR / "failure_director.jsonl"
FACTORY_PROJECT_DIR = _REPO_ROOT / "projects" / "factory"
_DEFAULT_PORTFOLIO_JSON = _REPO_ROOT / "portfolio.json"

LOCK_TIMEOUT_S = 5.0
DEFAULT_POLL_INTERVAL_S = 30
SUPERVISOR_POLL_LOCK_TIMEOUT_S = 2.0    # a busy project is typed, never a blocked poll loop
SHUTDOWN_GRACE_S = 5.0
NETWORK_WAIT_OWNER_THRESHOLD = 20       # consecutive waits before the owner is signalled
NETWORK_WAIT_BACKOFF_CEILING_S = 120.0
ORPHAN_ENGINE_MAX_AGE_S = 3 * 3600      # an engine with no live owner older than this is a straggler
ENGINE_WORKTREES_BASE = Path("/private/tmp/factory-worktrees")
RELOAD_DRAIN_SOFT_S = 90 * 60           # reload pending this long → idle projects start nothing new
RELOAD_ARMED_HUNG_FUTURE_S = 600.0      # armed reload never waits longer on a poller with nothing in flight
RELOAD_CANARY_RETRY_COOLDOWN_S = 300.0  # a red canary is held this long before re-running
LOOP_WATCHDOG_S = 20 * 60               # max seconds between two loop iterations
_INCIDENT_REPEAT_EVERY = 20             # identical incidents re-logged every N polls

_shutdown_requested = False
_QUOTA_WASTE_ACTIVE = False
_LAST_QUOTA_WORKER_LIMIT = None
_INCIDENT_SEEN: dict = {}

class LockTimeout(RuntimeError):
    pass

def _log(msg: str) -> None:
    print(f"[factoryd] {msg}")

def paths_for(run_dir) -> dict:
    run_dir = Path(run_dir)
    return {"run_dir": run_dir, "pid": run_dir / "factoryd.pid", "lock": run_dir / "factoryd.lock",
            "state": run_dir / "supervisor_state.json", "log": run_dir / "factoryd.log"}

def log_target_is_live(log_path) -> bool:
    """True when the readable log is the SAME inode this process writes to (a rotated log misleads every reader)."""
    try:
        on_disk = os.stat(log_path).st_ino
    except OSError:
        return False
    try:
        return on_disk == os.fstat(sys.stdout.fileno()).st_ino
    except (OSError, ValueError):
        return True                     # stdout is not a file: nothing can detach

def acquire_lock(lock_path: Path, timeout_s: float = LOCK_TIMEOUT_S):
    """Exclusive non-blocking flock, retried for at most timeout_s."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+")
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except BlockingIOError:
            if time.monotonic() >= deadline:
                fh.close()
                raise LockTimeout(f"could not acquire {lock_path} within {timeout_s}s")
            time.sleep(0.05)

def read_pid_file(pid_path: Path) -> int | None:
    try:
        return int(pid_path.read_text().strip())
    except (ValueError, OSError):
        return None

_atomic_text = _atomic_write_text

def write_pid_file(pid_path: Path, pid: int) -> None:
    _atomic_text(pid_path, str(pid))
    write_pid_identity(pid_path, pid)   # L3-012: who this pid was when written

def pid_identity_path(pid_path: Path) -> Path:
    return pid_path.with_name(pid_path.name + ".identity")

def _process_birth_identity(pid: int) -> str:
    try:
        from factory_v2.worker_lease import _process_birth_identity as _birth
        return _birth(pid)
    except Exception:
        return ""

def write_pid_identity(pid_path: Path, pid: int, birth: str | None = None) -> None:
    ident = {"pid": int(pid), "birth": _process_birth_identity(pid) if birth is None else birth}
    _atomic_text(pid_identity_path(pid_path), json.dumps(ident))

def pidfile_names_daemon(pid_path: Path, pid: int) -> "tuple[bool, str]":
    """(True, "") iff `pid` is still the daemon that wrote `pid_path` (L3-012: identity sidecar; unobservable = refusal)."""
    try:
        ident = json.loads(pid_identity_path(pid_path).read_text())
    except (OSError, ValueError):
        return False, "pid identity sidecar missing or unreadable"
    if int(ident.get("pid") or -1) != int(pid):
        return False, "pid identity sidecar names another pid"
    recorded, live = str(ident.get("birth") or ""), _process_birth_identity(pid)
    if not recorded or not live:
        return False, "birth identity unobservable"
    if live != recorded:
        return False, f"birth identity changed ({recorded!r} -> {live!r}): pid reused"
    return True, ""

def running_pid(pid_path: Path) -> int | None:
    """The pid in the pid file, iff that pid is actually alive."""
    pid = read_pid_file(pid_path)
    return None if pid is None or cdet.is_worker_dead(pid) else pid

def configured_max_workers() -> int:
    try:
        return max(1, int(os.environ.get("FACTORY_MAX_WORKERS", "1")))
    except (TypeError, ValueError):
        return 1

def _effective_max_workers(project_dirs=None) -> int:
    """Configured capacity capped by quota headroom and the waste incident (unreadable view: configured cap)."""
    configured = configured_max_workers()
    try:
        from factory_v2 import quota_observer as qo, quota_throttle
        view = dict(qo.merged_view(qo.load_snapshot(_REPO_ROOT), qo.latest_cli_signals(
            [Path(p) for p in (project_dirs or [])], control_root=_REPO_ROOT)))
        view["quota_waste_incident"] = bool(_QUOTA_WASTE_ACTIVE)
        return quota_throttle.effective_workers(view, configured)
    except Exception:
        return configured

def _emit_cto_event(event_type: str, mission_id: str, verdict: str | None,
                    events_path: "Path | None" = None, extra: dict | None = None) -> None:
    """Append one line to CTO_EVENTS.jsonl (path resolved at call time so overrides work)."""
    events_path = Path(events_path) if events_path is not None else CTO_EVENTS_PATH
    events_path.parent.mkdir(parents=True, exist_ok=True)
    with open(events_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"timestamp": sstate.utc_now(), "event": event_type, "mission_id": mission_id,
                            "verdict": verdict, **(extra or {})}) + "\n")

def _cto_startup_check(cto_dir: Path, log=_log) -> bool:
    """CTOK-007: a cold-started daemon answers the durable-state questions from factory/cto alone. Never fatal."""
    try:
        from cto_kernel import CTOKernel, CTOContextIncomplete
        kernel = CTOKernel(cto_dir)
        state = kernel.load()
        kernel.validate(state)
        kernel.assert_invariants()
    except Exception as exc:
        incomplete = type(exc).__name__ == "CTOContextIncomplete"
        log(f"CTO_STARTUP_CHECK: {'FAIL' if incomplete else 'ERROR'} — {exc}")
        if incomplete:
            _emit_cto_event("RECONSTRUCT_PROJECT_STATE", "", None, extra={"reason": str(exc)})
        return False
    log(f"CTO_STARTUP_CHECK: PASS — milestone={state.current_milestone}, next_action={state.next_required_action}")
    _emit_cto_event("CTO_STARTUP_CHECK_PASS", "", None, extra={
        "current_milestone": state.current_milestone, "next_required_action": state.next_required_action})
    return True

def _load_or_create_state(state_path: Path, pid: int, poll_interval_s: float, log=_log) -> sstate.SupervisorState:
    """Restart recovery: reuse supervisor_state.json; a corrupt file yields a fresh state."""
    loaded = None
    if state_path.exists():
        try:
            loaded = sstate.load_state(state_path)
        except Exception as exc:
            log(f"RECOVERY_WARN: could not load state: {exc} — starting fresh")
    if loaded is None:
        return sstate.SupervisorState(pid=pid, started_at=sstate.utc_now(), last_heartbeat=sstate.utc_now(),
                                      poll_interval_s=int(poll_interval_s))
    log(f"RECOVERY: reconstructed {len(loaded.active_workers)} active workers from disk")
    loaded.pid, loaded.poll_interval_s = pid, int(poll_interval_s)
    return loaded

def _classify_progress(project_dir: Path, log) -> list:
    """progress_truth on the project's event log: incidents persisted once, re-logged every _INCIDENT_REPEAT_EVERY polls."""
    from factory_v2.progress_truth import classify, persist
    try:
        state = json.loads((Path(project_dir) / "PROJECT_STATE.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    incidents = classify(events=state.get("events", []) or [], daemon_pid=os.getpid())
    if not incidents:
        _INCIDENT_SEEN.pop(str(project_dir), None)
        return incidents
    seen, fresh = _INCIDENT_SEEN.setdefault(str(project_dir), {}), []
    for inc in incidents:
        n = seen[inc.dedup_key] = seen.get(inc.dedup_key, 0) + 1
        if n == 1:
            fresh.append(inc)
            log(f"PROGRESS_INCIDENT: {inc.kind} — {inc.detail[:160]} (next_safe_action={inc.next_safe_action})")
        elif n % _INCIDENT_REPEAT_EVERY == 0:
            log(f"PROGRESS_INCIDENT x{n}: {inc.kind} — unchanged for {n} cycles")
    if fresh:
        persist(fresh, Path(project_dir) / "evidence" / "progress_incidents.jsonl")
    return incidents

def _live_engine_lease_ids(project_dirs) -> "set | None":
    """Lease identities owning a live worker; None = membership NOT establishable (the sweep then uses the age cap alone)."""
    import calendar
    try:
        from factory_v2.mission_evidence import HeartbeatObservationUnknown, is_worker_alive
    except Exception:
        return None
    live: set = set()
    for pd in [Path(p) for p in project_dirs]:
        try:
            state = json.loads((pd / "PROJECT_STATE.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue                    # no state file: no missions recorded, definitive
        except (OSError, json.JSONDecodeError):
            return None                 # L3-010: exists but unreadable = unknown
        for mid, rec in (state.get("missions") or {}).items():
            try:
                if isinstance(rec, dict) and is_worker_alive(pd, str(mid)):
                    live.add(str(mid))
                    exc = rec.get("executor") if isinstance(rec.get("executor"), dict) else {}
                    live.update(str(exc[k]) for k in ("_attempt_key", "attempt_key", "_lease_generation")
                                if exc.get(k) not in (None, ""))
            except HeartbeatObservationUnknown:
                return None             # FCO_SYNTH-002: corrupt heartbeat ≠ no worker
            except Exception:
                continue
        for lp in list((pd / "evidence" / "leases").glob("*.json")) if (pd / "evidence" / "leases").is_dir() else []:
            try:
                data = json.loads(lp.read_text(encoding="utf-8"))
                expires = calendar.timegm(time.strptime(str(data.get("expires_at", "")), "%Y-%m-%dT%H:%M:%SZ"))
            except (OSError, json.JSONDecodeError):
                return None
            except ValueError:
                continue
            if data.get("state") in ("ACTIVE", "ADOPTED") and data.get("mission_id") and time.time() <= expires:
                live.add(str(data["mission_id"]))   # an unexpired ACTIVE/ADOPTED lease is live
    return live

def _engine_registry_files(project_dirs) -> list:
    """Registries the sweep judges: hermetic = ITS projects only; production adds the control repo and mission worktrees."""
    hermetic = os.environ.get("FACTORY_NO_REAL_ENGINE") == "1"
    roots = [Path(pd) for pd in project_dirs] + ([] if hermetic else [_REPO_ROOT])
    files = [root / "evidence" / "engine_registry.jsonl" for root in roots]
    if not hermetic and ENGINE_WORKTREES_BASE.is_dir():
        files.extend(sorted(ENGINE_WORKTREES_BASE.glob("*/evidence/engine_registry.jsonl")))
    return [f for f in files if f.is_file()]

_REPO_HYGIENE_EVERY_S = 600.0
_REPO_HYGIENE_LAST = {"ts": 0.0, "thread": None}


def _repo_hygiene_pass(log, prune, heal) -> dict:
    """One hygiene pass (prune + heal), never raises — the body of _repo_hygiene_cycle."""
    result = {"pruned": [], "restored": []}
    try:
        if prune is None:
            import dispatcher_v3 as _d3
            prune = _d3.prune_stale_worktrees
        result["pruned"] = list(prune(_REPO_ROOT) or [])
        if result["pruned"]:
            log(f"WORKTREE_PRUNE_CYCLE: {len(result['pruned'])} checkout(s) removed — {result['pruned'][:6]}")
    except Exception as exc:
        log(f"WORKTREE_PRUNE_CYCLE_WARN: {type(exc).__name__}: {exc}")
    try:
        if heal is None:
            from factory_v2.repo_hygiene import heal_recent_merges as heal
        result["restored"] = list(heal(_REPO_ROOT) or [])
        if result["restored"]:
            log(f"MERGE_BOOKKEEPING_RESTORED: {len(result['restored'])} ignored file(s) written back on disk, "
                f"untracked — {result['restored'][:3]}")
    except Exception as exc:
        log(f"REPO_HYGIENE_HEAL_WARN: {type(exc).__name__}: {exc}")
    return result


def _repo_hygiene_cycle(log, every_s: float = _REPO_HYGIENE_EVERY_S, prune=None, heal=None,
                        background: bool = True) -> dict:
    """FACTORY_REPO_HYGIENE_V1: every daemon cycle (one real pass per `every_s`) — (a) stale mission checkouts and
    `.archived-*` checkouts older than 24 h are removed (dispatcher_v3.prune_stale_worktrees: branches and commits
    stay, dirty WIP is committed on the archive branch first); (b) bookkeeping a recent merge deleted from disk and
    that the repo now ignores is written back, untracked (repo_hygiene.heal_recent_merges). Never raises.
    `prune(repo_root) -> list` / `heal(repo_root) -> list` are injectable so tests never touch the real worktrees.
    Essai 3 (banc DAEMON_LOOP_SILENT sur la dorée fusionnée) : la passe tourne dans un thread de fond — après une
    grosse fusion le heal fait des milliers d'appels git et bloquait la boucle jusqu'au LOOP_WATCHDOG (rc 3) ;
    en mode hermétique (FACTORY_NO_REAL_ENGINE=1) sans prune/heal injectés, rien n'est fait : un banc ne touche
    jamais la vraie base de worktrees ni le disque du dépôt de contrôle."""
    if prune is None and heal is None and os.environ.get("FACTORY_NO_REAL_ENGINE") == "1":
        return {}
    now = time.monotonic()
    if now - _REPO_HYGIENE_LAST["ts"] < every_s:
        return {}
    prev = _REPO_HYGIENE_LAST.get("thread")
    if prev is not None and prev.is_alive():
        return {}                                   # the previous pass is still running: never two at once
    _REPO_HYGIENE_LAST["ts"] = now
    if not background:
        return _repo_hygiene_pass(log, prune, heal)
    t = threading.Thread(target=_repo_hygiene_pass, args=(log, prune, heal), name="repo-hygiene", daemon=True)
    _REPO_HYGIENE_LAST["thread"] = t
    t.start()
    return {"scheduled": True}


def _sweep_orphan_engines(log, project_dirs=()) -> int:
    """Kill engines whose lease is closed/expired: registry first (membership + age); argv net only for unregistered, never hermetically."""
    killed, protected_pids = 0, set()   # L3-011: registry-judged pids are never re-judged
    try:
        from factory_v2 import engine_registry as _er
        live = _live_engine_lease_ids(project_dirs)
        for reg in _engine_registry_files(project_dirs):
            root = reg.parent.parent
            try:
                protected_pids.update(int(r.get("pid")) for r in _er.read_registry(root) if str(r.get("pid") or "").isdigit())
                orphans = _er.orphans_from_registry(root, live_leases=live, max_age_s=ORPHAN_ENGINE_MAX_AGE_S)
            except Exception:
                continue
            doomed = _er.production_killable(orphans, max_age_s=ORPHAN_ENGINE_MAX_AGE_S)
            if doomed:
                killed += len(_er.kill_orphans(root, doomed, log=log))
    except Exception:
        pass
    if os.environ.get("FACTORY_NO_REAL_ENGINE") == "1":
        return killed
    try:
        return killed + _argv_net_kill(log, protected_pids=protected_pids)
    except TypeError:                   # a test double with the old `lambda log: 0` shape
        return killed + _argv_net_kill(log)

def _argv_net_kill(log, protected_pids=()) -> int:
    """Secondary net: PPID-1 engine-shaped processes older than the age cap that no registry knows (`protected_pids` skipped)."""
    from factory_v2.engine_registry import _parse_etime
    try:
        out = subprocess.run(["ps", "-eo", "pid=,ppid=,etime=,command="], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return 0
    killed = 0
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4 or parts[1] != "1" or not re.match(
                r"^(codex exec|claude -p|/usr/local/bin/opencode run|opencode run)", parts[3].strip()):
            continue
        try:
            pid, age = int(parts[0]), int(_parse_etime(parts[2]))
            if pid in set(protected_pids or ()) or age <= ORPHAN_ENGINE_MAX_AGE_S:
                continue
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except Exception:
                os.kill(pid, signal.SIGKILL)
        except Exception:
            continue
        killed += 1
        log(f"ORPHAN_ENGINE_KILLED: pid={pid} age_s={age} cmd={parts[3][:80]!r}")
    return killed

def _record_network_wait(project_dir: Path, state: str, log) -> dict:
    """Durable count of consecutive network waits; past the threshold, one owner-visible incident (never invisible offline)."""
    path = Path(project_dir) / "evidence" / "network_wait.json"
    now = _now()
    try:
        cur = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cur = {}
    rec = {"state": state, "consecutive": int(cur.get("consecutive", 0)) + 1, "since": cur.get("since", now),
           "last": now, "owner_alerted": bool(cur.get("owner_alerted", False))}
    try:
        if rec["consecutive"] >= NETWORK_WAIT_OWNER_THRESHOLD and not rec["owner_alerted"]:
            rec["owner_alerted"] = True
            with (Path(project_dir) / "evidence" / "progress_incidents.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"kind": "NETWORK_WAIT_PERSISTENT", "state": state, "consecutive": rec["consecutive"],
                                     "since": rec["since"], "at": now,
                                     "note": "daemon has been unable to reach the engine API for many consecutive "
                                             "polls — owner attention (local DNS/firewall/API outage?)"}) + "\n")
            log(f"NETWORK_WAIT_PERSISTENT: {state} consecutive={rec['consecutive']} since {rec['since']} — owner signalled")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rec), encoding="utf-8")
    except OSError:
        pass
    return rec

# advance() runs per project in a bounded pool, harvested by the loop. A reload first DRAINS (idle
# projects start nothing new), then ARMS (nothing new anywhere), exits once the pool is empty + canary green.
_ADVANCE_POOL = None
_ADVANCE_FUTURES: dict = {}          # "<project>#<slot>" -> Future
_ADVANCE_STARTED: dict = {}          # future key -> monotonic submit time
_ADVANCE_RESULTS: dict = {}          # str(project_dir) -> (detail, touched_files), legacy record
_PARALLEL_HARVEST: list = []         # (project_dir, action, detail, touched) finished since last poll
_LAST_HARVEST: dict = {"project": None, "detail": "", "touched": []}   # harvest the loop is consuming
_LAST_ADVANCE_DETAIL: str = ""                                          # serial-mode CycleResult stash
_LAST_ADVANCE_TOUCHED_FILES: list[str] = []
_ADVANCE_CALL_META = threading.local()   # L3-008: per-CALL metadata, never overwritten by a sibling
_RELOAD_DRAINING: bool = False
_RELOAD_DRAIN_PROJECTS: list = []
_RELOAD_DRAIN_STARTED: float = 0.0
_RELOAD_ARMED: bool = False
_LOOP_LAST_ITER: dict = {"ts": 0.0}
_LOOP_WATCHDOG_STOP: dict = {"stop": False}

def _live_dispatchers() -> int:
    """Dispatcher children of THIS daemon still running (tests patch this)."""
    try:
        return len(subprocess.run(["pgrep", "-P", str(os.getpid()), "-f", "factory/bin/dispatcher_v3.py"],
                                  capture_output=True, text=True, timeout=5).stdout.split())
    except Exception:
        return 1

def _live_work_children() -> int:
    """Children of THIS daemon that are real work in flight: dispatchers AND the
    in-daemon validation subprocesses (bash → pytest / judges run by an advance
    future after the engine returned). 21/09 13:15 Bangkok: the armed exit only
    counted dispatcher_v3 children, so two rewrites whose engines had finished
    and whose candidates were being validated in-thread looked like "hung
    pollers" — the daemon exited and both validations (2 h 37 + 1 h 55 of build)
    were lost. Heartbeat sidecars are not work."""
    try:
        out = subprocess.run(["pgrep", "-P", str(os.getpid()), "-l"], capture_output=True,
                             text=True, timeout=5).stdout.splitlines()
    except Exception:
        return 1
    return sum(1 for ln in out if ln.strip() and "heartbeat_side" not in ln)


def _begin_reload_drain(project_dirs) -> None:
    global _RELOAD_DRAINING, _RELOAD_DRAIN_PROJECTS, _RELOAD_DRAIN_STARTED
    if not _RELOAD_DRAINING:
        _RELOAD_DRAIN_STARTED = time.monotonic()
    _RELOAD_DRAINING = True
    _RELOAD_DRAIN_PROJECTS = [Path(p) for p in (project_dirs or [])]

def _arm_reload(log) -> None:
    global _RELOAD_ARMED
    if not _RELOAD_ARMED:
        _RELOAD_ARMED = True
        log(f"RELOAD_ARMED: {len(_ADVANCE_FUTURES)} advance(s) still running — "
            "starting nothing new; exiting when the pool is empty")

def _disarm_reload(log) -> None:
    global _RELOAD_ARMED
    if _RELOAD_ARMED:
        _RELOAD_ARMED = False
        log("RELOAD_DISARMED: a running advance dispatched an engine — back to soft drain")

def _armed_exit_allowed() -> bool:
    """Armed reload may exit: pool empty (after reaping), or every running future is a hung poller with no dispatcher child."""
    _harvest_parallel(lambda *_a, **_k: None)
    if not _ADVANCE_FUTURES:
        return True
    if _live_dispatchers() or _live_work_children():
        return False
    now = time.monotonic()
    return all(now - _ADVANCE_STARTED.get(k, now) > RELOAD_ARMED_HUNG_FUTURE_S for k in _ADVANCE_FUTURES)

def _project_in_flight(project_dir) -> int:
    """Conservative in-flight count (L3-009): a present-but-unreadable state file is UNKNOWN = 1, never a fake zero."""
    try:
        pd = Path(project_dir)
        summary = v2_status_summary(pd)
        if summary.get("observed") is True:
            return int(summary.get("in_flight", 0))
        return 1 if (pd / "PROJECT_STATE.json").exists() else 0
    except Exception:
        return 1

def _advance_pool():
    """The pool, rebuilt after RELOAD_NOW shut it down (an in-process host would otherwise fail every submit)."""
    global _ADVANCE_POOL
    if _ADVANCE_POOL is None or getattr(_ADVANCE_POOL, "_shutdown", False):
        from concurrent.futures import ThreadPoolExecutor
        _ADVANCE_POOL = ThreadPoolExecutor(max_workers=max(1, configured_max_workers()), thread_name_prefix="advance")
    return _ADVANCE_POOL

def _advance_v2_harvestable(project_dir: Path, log):
    """Pool wrapper: the Future carries THIS call's (action, detail, touched_files)."""
    action = _advance_v2(project_dir, log)
    return (action, getattr(_ADVANCE_CALL_META, "detail", "") or "",
            list(getattr(_ADVANCE_CALL_META, "touched", []) or []))

def _harvest_parallel(log) -> None:
    for key, fut in list(_ADVANCE_FUTURES.items()):
        if not getattr(fut, "done", lambda: False)():
            continue                    # unknown placeholders count as still running
        del _ADVANCE_FUTURES[key]
        _ADVANCE_STARTED.pop(key, None)
        pdir = key.split("#", 1)[0]
        try:
            result = fut.result()
        except Exception as exc:                       # noqa: BLE001
            result = "CONTROL_PLANE_ERROR"
            log(f"PARALLEL_ADVANCE_ERROR: {Path(pdir).name}: {type(exc).__name__}: {str(exc)[:200]}")
        if isinstance(result, tuple) and len(result) == 3:
            action, detail, touched = result
        else:
            action, (detail, touched) = result, _ADVANCE_RESULTS.pop(pdir, ("", []))
        _PARALLEL_HARVEST.append((Path(pdir), action, detail, touched))
        log(f"PARALLEL_ADVANCE_DONE: {Path(pdir).name} action={action}")

def _advance_parallel(project_dir: Path, log) -> str:
    """Submit advance() unless the project's slots are full; return a JUST-FINISHED action if any, else a typed placeholder."""
    global _LAST_QUOTA_WORKER_LIMIT
    _harvest_parallel(log)
    base = str(project_dir)
    mine = [k for k in _ADVANCE_FUTURES if k.split("#", 1)[0] == base]
    per_project = max(1, int(os.environ.get("FACTORY_ADVANCES_PER_PROJECT", "2")))
    drain_dirs = _RELOAD_DRAIN_PROJECTS or [project_dir]
    if _RELOAD_DRAINING and not _ADVANCE_FUTURES and all(_project_in_flight(pd) == 0 for pd in drain_dirs):
        return "RELOAD_READY"           # nothing runs, nothing in flight anywhere: free to exit
    if (_RELOAD_DRAINING and not mine and time.monotonic() - _RELOAD_DRAIN_STARTED > RELOAD_DRAIN_SOFT_S
            and _project_in_flight(project_dir) == 0):
        return "RELOAD_DRAINING"        # soft phase: finish what runs, start nothing new here
    if _RELOAD_ARMED:                   # a future that dispatched an engine lifts the arm
        if any(_project_in_flight(pd) > 0 for pd in drain_dirs):
            _disarm_reload(log)
        else:
            return "RELOAD_DRAINING" if _ADVANCE_FUTURES else "RELOAD_READY"
    worker_limit = _effective_max_workers([project_dir])
    if worker_limit != _LAST_QUOTA_WORKER_LIMIT:
        if worker_limit != configured_max_workers():
            log(f"QUOTA_THROTTLE: workers={worker_limit}/{configured_max_workers()}")
        _LAST_QUOTA_WORKER_LIMIT = worker_limit
    if len(mine) < per_project and len(_ADVANCE_FUTURES) < worker_limit:
        slot = next(i for i in range(per_project) if f"{base}#{i}" not in _ADVANCE_FUTURES)
        _ADVANCE_FUTURES[f"{base}#{slot}"] = _advance_pool().submit(_advance_v2_harvestable, project_dir, log)
        _ADVANCE_STARTED[f"{base}#{slot}"] = time.monotonic()
        log(f"PARALLEL_ADVANCE_STARTED: {project_dir.name} slot={slot} (running={len(_ADVANCE_FUTURES)})")
    if _PARALLEL_HARVEST:
        pdir, action, detail, touched = _PARALLEL_HARVEST.pop(0)
        _LAST_HARVEST.update({"project": pdir, "detail": detail, "touched": list(touched)})
        return action
    _LAST_HARVEST.update({"project": None, "detail": "", "touched": []})
    return "ADVANCE_RUNNING" if any(k.split("#", 1)[0] == base for k in _ADVANCE_FUTURES) else "WORKERS_BUSY"

def _advance_v2(project_dir: Path, log) -> str:
    """One authoritative advance() (every product decision is runtime's); with the network down, wait typed and free."""
    global _LAST_ADVANCE_DETAIL, _LAST_ADVANCE_TOUCHED_FILES
    from factory_v2.runtime import advance
    try:
        from factory_v2 import resilience as _rs
        if os.environ.get("FACTORY_NO_REAL_ENGINE") != "1" and not _rs.network_available():
            state = _rs.connectivity_state()          # ENGINE_UP / API_UNAVAILABLE / OFFLINE
            waits = _record_network_wait(project_dir, state, log)
            _LAST_ADVANCE_DETAIL = _ADVANCE_CALL_META.detail = (
                f"{state} — no attempt opened (consecutive={waits['consecutive']}, since {waits['since']})")
            _LAST_ADVANCE_TOUCHED_FILES = _ADVANCE_CALL_META.touched = []
            log(f"V2_ADVANCE: action=WAITING_NETWORK state={state} consecutive={waits['consecutive']} milestone=- ({project_dir.name})")
            return "WAITING_NETWORK"
        (Path(project_dir) / "evidence" / "network_wait.json").unlink(missing_ok=True)
    except Exception:
        pass
    r = advance(project_dir, lock_timeout_s=SUPERVISOR_POLL_LOCK_TIMEOUT_S)
    _LAST_ADVANCE_DETAIL = _ADVANCE_CALL_META.detail = r.detail or ""
    _LAST_ADVANCE_TOUCHED_FILES = _ADVANCE_CALL_META.touched = list(r.touched_files or [])
    _ADVANCE_RESULTS[str(project_dir)] = (r.detail or "", list(r.touched_files or []))
    log(f"V2_ADVANCE: action={r.action} milestone={r.milestone or '-'}")
    try:                                # §F3: V2_ADVANCE is activity; progress_truth says what advanced
        _classify_progress(project_dir, log)
    except Exception as exc:
        log(f"PROGRESS_TRUTH_ERROR: {type(exc).__name__}: {str(exc)[:200]}")
    if r.action == "PROJECT_COMPLETE":
        log("V2_PROJECT_COMPLETE — all milestones settled. Daemon idle.")
    elif r.action == "MILESTONE_SETTLED":
        log(f"V2_MILESTONE_SETTLED: {r.milestone}")
        if r.milestone == "LR5_PREFLIGHT_EXACT_SHA":
            _auto_start_twitter_radar(project_dir, log)
    elif r.action == "MISSION_FAILED":
        log(f"V2_MISSION_FAILED: {r.detail}")
    elif r.action in ("MILESTONE_GATE_BLOCKED", "AUTHORITY_DRIFT_BLOCKED", "STALE_CONTEXT_DISPATCH_BLOCKED",
                      "MILESTONE_NOT_COMPILABLE"):
        log(f"V2_BLOCKED: {r.action} — {r.detail}")
    return r.action

def _is_factory_project(project_dir: Path | None) -> bool:
    """Only the factory's own settlement may reload this daemon."""
    if project_dir is None:
        return False
    try:
        return Path(project_dir).resolve() == FACTORY_PROJECT_DIR.resolve()
    except OSError:
        return Path(project_dir).name == "factory"

def _reload_in_flight(project_dir: Path | None, action: str | None) -> int:
    """Conservative in-flight count for the reload policy (draining: missions/engines in flight + live dispatcher children)."""
    if _RELOAD_DRAINING:
        if action == "RELOAD_READY":
            return 0
        _harvest_parallel(lambda *_a, **_k: None)
        dirs = list(_RELOAD_DRAIN_PROJECTS) or ([project_dir] if project_dir else [])
        return sum(_project_in_flight(pd) for pd in dirs) + _live_dispatchers() if dirs else 1  # no dirs: never exit blind
    if action in _BUSY_ACTIONS:
        return 1
    if _ADVANCE_FUTURES:
        return len(_ADVANCE_FUTURES)
    return 0 if project_dir is None else _project_in_flight(project_dir)

def _current_head() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(_REPO_ROOT),
                              capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return ""

def _record_reload_canary_failure(why: str, log=_log, *, code_sha: str = "", project_dir: "Path | None" = None) -> None:
    """Durable, typed record of a red canary: CTO event + progress incident + OPEN maintenance request. Never raises."""
    project_dir = Path(project_dir) if project_dir is not None else FACTORY_PROJECT_DIR
    why_s = str(why)
    try:
        _emit_cto_event("RELOAD_CANARY_FAILED", "", None, extra={
            "reason": why_s[:400], "retry": f"next settlement (cooldown {RELOAD_CANARY_RETRY_COOLDOWN_S:.0f}s)"})
    except Exception as exc:
        log(f"RELOAD_CANARY_EVENT_WARN: {exc}")
    try:
        from factory_v2.progress_truth import Incident, persist
        persist([Incident(kind="RELOAD_CANARY_FAILED", detail=why_s[:600], detected_at=sstate.utc_now(),
                          next_safe_action="fix HEAD (canary red) — the reload is retried at the next settlement",
                          cause_must_be_fixed=True, evidence={"canary": "hermetic advance() subprocess at HEAD"})],
                project_dir / "evidence" / "progress_incidents.jsonl")
    except Exception as exc:
        log(f"RELOAD_CANARY_INCIDENT_WARN: {exc}")
    try:
        from factory_v2 import maintenance_request as mr
        mr.raise_request(project_dir, kind=mr.KIND_RELOAD_CANARY_FAILED, milestone_id="hot-reload", fingerprint=why_s[:400],
                         old_code_sha=str(code_sha or ""), new_code_sha=_current_head(), reproduction=why_s[:600],
                         missing_proof="green reload_canary() on the current HEAD",
                         resolves_when="HEAD imports and one hermetic advance() settles (canary green)")
    except Exception as exc:
        log(f"RELOAD_CANARY_REQUEST_WARN: {exc}")

def _canary_allows_reload(log, cooldown: dict, *, code_sha: str = "") -> bool:
    """RELOAD_NOW gate: HEAD must import and settle one hermetic advance(); red/crashing canary defers, held for the cooldown."""
    if time.monotonic() - float(cooldown.get("ts", 0.0)) < RELOAD_CANARY_RETRY_COOLDOWN_S:
        return False
    from factory_v2 import hot_reload
    try:
        ok, why = hot_reload.reload_canary(_REPO_ROOT)
    except Exception as exc:
        ok, why = False, f"RELOAD_CANARY_FAILED: CANARY_CRASH: {type(exc).__name__}: {exc}"
    if ok:
        log(f"RELOAD_CANARY_OK: {why}")
        return True
    cooldown["ts"] = time.monotonic()
    log(f"{why} — reload deferred, daemon keeps supervising")
    _record_reload_canary_failure(why, log, code_sha=code_sha)
    return False

def _auto_start_twitter_radar(factory_project_dir: Path, log) -> None:
    """B5: after LR5_PREFLIGHT_EXACT_SHA settles, start the Twitter Radar long run when LONG_RUN_READY. Never raises."""
    try:
        from factory_v2.twitter_radar_long_run import auto_start_twitter_radar_if_ready
        radar = factory_project_dir.parent / "twitter-radar"
        auto_start_twitter_radar_if_ready(
            factory_project_dir=factory_project_dir, twitter_radar_project_dir=radar, log=log,
            factory_repo_dir=factory_project_dir.parent.parent,
            factoryd_cmd=["python3", str(Path(__file__).resolve()), "start", "--v2-project-dir", str(radar)])
    except Exception as exc:
        log(f"B5_AUTO_START_ERROR: {exc} — daemon continues")

def _interruptible_sleep(seconds: float, slice_s: float = 0.25) -> None:
    """Sleep in slices so a shutdown request is honoured within one slice (P0-3)."""
    deadline = time.monotonic() + max(0.0, float(seconds))
    while time.monotonic() < deadline and not _shutdown_requested:
        time.sleep(min(slice_s, max(0.0, deadline - time.monotonic())))

def _portfolio_project_dirs(v2_project_dir, portfolio_file) -> list:
    """Project dirs of a single-project run, or of the portfolio (repo-relative entries resolved against the file)."""
    if v2_project_dir:
        return [Path(v2_project_dir)]
    try:
        projects = json.loads(Path(portfolio_file).read_text(encoding="utf-8")).get("projects") if portfolio_file else []
    except (OSError, json.JSONDecodeError, AttributeError):
        return []
    dirs = []
    for proj in projects or []:
        pd = (proj.get("project_dir") or proj.get("path")) if isinstance(proj, dict) else None
        if pd:
            dirs.append(Path(pd) if Path(pd).is_absolute() else Path(portfolio_file).resolve().parent / pd)
    return dirs

def _persist_quota_waste(project_dir: Path, waste: dict, threshold: float, dominant: dict) -> None:
    """Flip the project's quota_waste incident (with an event) when its state changed."""
    from datetime import datetime, timezone
    from factory_v2.project_authority import load_project_state, save_project_state
    state = load_project_state(project_dir)
    if _QUOTA_WASTE_ACTIVE == bool((state.get("quota_waste") or {}).get("active")):
        return
    event = {"event": "QUOTA_WASTE_INCIDENT" if _QUOTA_WASTE_ACTIVE else "QUOTA_WASTE_INCIDENT_RESOLVED",
             "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "window_hours": 3,
             "lost_minutes": waste["lost_minutes_total"], "threshold_minutes": threshold}
    if _QUOTA_WASTE_ACTIVE:
        event["dominant_cause"] = dominant
    state.setdefault("events", []).append(event)
    state["quota_waste"] = {"active": _QUOTA_WASTE_ACTIVE, "lost_minutes": waste["lost_minutes_total"], "dominant_cause": dominant}
    save_project_state(project_dir, state)

def _generate_metrics_report(run_dir: Path, project_dirs: "list | None" = None) -> Path:
    """Regenerate the 24 h dashboard + rolling 3 h quota-waste incident (raises: the loop logs METRICS_REPORT_FAILED)."""
    global _QUOTA_WASTE_ACTIVE
    from datetime import datetime, timedelta, timezone
    from factory_v2.metrics_dashboard import build_dashboard, to_markdown
    from factory_v2.quota_waste import collect as collect_quota_waste
    dirs = [Path(p) for p in (project_dirs or [])]
    report = build_dashboard(dirs)
    waste = collect_quota_waste(dirs, datetime.now(timezone.utc) - timedelta(hours=3))
    try:
        threshold = max(0.0, float(os.environ.get("FACTORY_QUOTA_WASTE_ALERT_MIN", "60")))
    except (TypeError, ValueError):
        threshold = 60.0
    _QUOTA_WASTE_ACTIVE = waste["lost_minutes_total"] > threshold
    dominant = (waste.get("lost_minutes_by_cause") or [{}])[0]
    waste.update({"threshold_minutes": threshold, "incident_active": _QUOTA_WASTE_ACTIVE, "dominant_cause": dominant})
    report["quota_waste_3h"] = waste
    try:   # FACTORY_TOKEN_TRUTH_V1: tokens per settled milestone / per engine, recomputed from the ledgers (24 h)
        from factory_v2.token_cost_report import collect as collect_token_cost, one_line as token_cost_line
        report["token_cost_24h"] = collect_token_cost(dirs, datetime.now(timezone.utc) - timedelta(hours=24))
        token_line = token_cost_line(report["token_cost_24h"])
    except Exception as exc:   # the dashboard must not die on its newest line; the gap is named
        token_line = f"indisponible ({type(exc).__name__}: {exc})"
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    md_path = run_dir / "BUILD_METRICS_LATEST.md"
    md_path.write_text(to_markdown(report) + (
        f"\n## Quota waste (rolling 3 h)\n\n- Lost minutes: {waste['lost_minutes_total']} / {threshold:g}\n"
        f"- QUOTA_WASTE_INCIDENT: {'ACTIVE' if _QUOTA_WASTE_ACTIVE else 'clear'}\n"
        f"- Dominant cause: {dominant.get('engine', 'none')} / {dominant.get('cause', 'none')}\n"
        f"\n## Coût en tokens (24 h)\n\n- {token_line}\n"), encoding="utf-8")
    (run_dir / "BUILD_METRICS_LATEST.json").write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n",
                                                       encoding="utf-8")
    for project_dir in dirs:
        try:
            _persist_quota_waste(project_dir, waste, threshold, dominant)
        except Exception as exc:
            _log(f"QUOTA_WASTE_INCIDENT_PERSIST_FAILED: {project_dir}: {exc}")
    return md_path

def _start_loop_watchdog(stop: dict) -> None:
    """No iteration within LOOP_WATCHDOG_S → dump threads, exit 3 (launchd restarts); `stop` disarms this run only."""
    def _watch():
        import faulthandler
        armed_at = time.monotonic()
        while not stop["stop"]:
            time.sleep(60)
            if not stop["stop"] and time.monotonic() - max(_LOOP_LAST_ITER["ts"], armed_at) > LOOP_WATCHDOG_S:
                try:
                    sys.stderr.write(f"[factoryd] LOOP_WATCHDOG_TRIPPED: no loop iteration for {LOOP_WATCHDOG_S:.0f}s "
                                     "— dumping threads and exiting 3\n")
                    faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
                    sys.stderr.flush()
                except Exception:
                    pass
                os._exit(3)
    _LOOP_LAST_ITER["ts"] = time.monotonic()
    threading.Thread(target=_watch, name="loop-watchdog", daemon=True).start()

def _descendants_of(pid: int) -> list:
    """Transitive children, recorded while the parent link still exists."""
    seen, frontier = [], [str(pid)]
    while frontier:
        r = subprocess.run(["pgrep", "-P", frontier.pop()], capture_output=True, text=True)
        kids = [k for k in r.stdout.split() if k.isdigit()]
        seen.extend(int(k) for k in kids)
        frontier.extend(kids)
    return seen

def _reap_pids(pids: list, grace_s: float = 2.0) -> list:
    """SIGTERM then SIGKILL the process GROUPS of `pids` (never our own); returns the groups that needed SIGKILL."""
    groups = set()
    for pid in pids:
        try:
            groups.add(os.getpgid(pid))
        except (ProcessLookupError, PermissionError):
            continue
    groups.discard(os.getpgid(os.getpid()))

    def _signal(sig):
        alive = set()
        for pgid in groups:
            try:
                os.killpg(pgid, sig)
                alive.add(pgid)
            except (ProcessLookupError, PermissionError):
                pass
        return alive
    groups = _signal(signal.SIGTERM)
    deadline = time.monotonic() + grace_s
    while groups and time.monotonic() < deadline:
        time.sleep(0.1)
        groups = _signal(0)
    return sorted(_signal(signal.SIGKILL))

def _handle_signal(signum, frame) -> None:
    """Set the flag AND reap what is in flight within SHUTDOWN_GRACE_S (P0-3: a blocked poll never reaches the flag)."""
    global _shutdown_requested
    _shutdown_requested = True
    kids = _descendants_of(os.getpid())
    if kids:                            # else the loop notices the flag on its own
        _reap_pids(kids, grace_s=SHUTDOWN_GRACE_S)
        _log(f"FACTORYD_SHUTDOWN: reaped {len(kids)} descendant(s) on signal {signum}")
        raise SystemExit(0)             # unwind the loop's pid/run-window cleanup

_NON_PROGRESS_ACTIONS = ("BLOCKED", "MISSION_FAILED", "MILESTONE_GATE_BLOCKED", "MILESTONE_NOT_COMPILABLE",
                         "STALE_CONTEXT_DISPATCH_BLOCKED", "AUTHORITY_DRIFT_BLOCKED", "CONTROL_PLANE_ERROR",
                         "WAITING_NETWORK")      # activity without progress (network wait included)
_BUSY_ACTIONS = ("MISSION_IN_FLIGHT", "ADVANCE_RUNNING", "WORKERS_BUSY", "PROJECT_BUSY")
_V2_STALL_THRESHOLD = 5

def _safety_stop_cause(project_dir: Path) -> "str | None":
    """The cause in <project>/SAFETY_STOP.json, or None when no stop is written."""
    path = project_dir / "SAFETY_STOP.json"
    try:
        return str(json.loads(path.read_text()).get("cause", "unknown")) if path.exists() else None
    except Exception:
        return "SAFETY_STOP.json parse error"

def _next_sleep_s(action, reason, poll_interval_s, ceiling_s, backoff: dict) -> float:
    """ECO-02: busy actions keep a floor; an unchanged (action, reason) backs off to ceiling_s; calendar waits never oversleep."""
    if action in _BUSY_ACTIONS:
        backoff.update({"key": None, "streak": 0})
        return max(poll_interval_s, 1.0)
    backoff["streak"] = backoff["streak"] + 1 if (action, reason) == backoff.get("key") else 0
    backoff["key"] = (action, reason)
    sleep_s = min(poll_interval_s * (2 ** backoff["streak"]), ceiling_s)
    if action == "WAITING_NETWORK":
        sleep_s = min(sleep_s, NETWORK_WAIT_BACKOFF_CEILING_S)
    m = re.search(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z", str(reason or "")) \
        if action in ("STANDING_CAP_REACHED", "MILESTONE_PARKED") else None
    if m:
        from datetime import datetime, timezone
        left = (datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
                - datetime.now(timezone.utc)).total_seconds()
        sleep_s = max(poll_interval_s, min(sleep_s, left + 5)) if left > 0 else poll_interval_s
    return sleep_s

def _heartbeat_line(pid, polls, action, reason, streak, sleep_s, v2_project_dir) -> str:
    """One at-a-glance liveness line (its own line, never deduped by the backoff key)."""
    extra = ""
    try:
        s = v2_status_summary(v2_project_dir) if v2_project_dir is not None else {}
        if s.get("observed"):
            extra = f" in_flight={s.get('in_flight')} blocked={s.get('blocked')}"
    except Exception:
        pass
    return (f"[factoryd] HEARTBEAT pid={pid} polls={polls} action={action} reason={(reason or '')[:80]} "
            f"streak={streak} next_sleep_s={sleep_s}{extra}")

def _continuation(decide, RunDisposition, go: dict, log, **kw):
    """P0-E: ContinuationController disposition → CONTINUE_NOW / IDLE / BACKOFF / BREAK / (QUARANTINE, reason) / None."""
    decision = decide(agent_output="", human_go_count=go["human"], **kw)
    go["agent"] += max(0, decision.human_go_count - go["human"])
    log(f"CONTINUATION: disposition={decision.disposition.value} AGENT_GO_REQUESTS={go['agent']} HUMAN_GO={go['human']}")
    d, reason = decision.disposition, decision.reason
    if d == RunDisposition.CONTINUE:
        return "CONTINUE_NOW" if kw["last_advance_action"] == "MILESTONE_SETTLED" else None
    if d == RunDisposition.PORTFOLIO_COMPLETE:
        return "IDLE"                   # F5R1: the daemon stays alive, idling
    if d == RunDisposition.CONTROL_PLANE_ERROR:
        log(f"DAEMON_BACKOFF: CONTROL_PLANE_ERROR — {reason}")
        return "BACKOFF"
    if d == RunDisposition.SAFETY_STOP:
        log(f"SAFETY_STOP_RECEIVED: {reason}")
        return ("QUARANTINE", reason)
    if d in (RunDisposition.PAUSED_BY_USER, RunDisposition.BLOCKED_EXTERNAL_VERIFIED):
        log(f"CONTINUATION_STOP: {d.value} — {reason}")
        return "BREAK"
    return None

def run_daemon_loop(campaign_state_path: Path, run_dir: Path, poll_interval_s: float,
                    max_polls: int | None = None, v2_project_dir: Path | None = None,
                    portfolio_path: "str | None" = None, idle_poll_interval_s: int = 30,
                    backoff_ceiling_s: float = 1800.0, heartbeat_log_interval_s: float = 600.0,
                    metrics_report_interval_s: float = 3600.0) -> None:
    """Main daemon loop. Portfolio mode (default, `portfolio_path`): select_project() chooses
    work each poll; single-project mode (`v2_project_dir`): advance that project, serially.
    Only max_polls ends the loop in production (PORTFOLIO_COMPLETE idles). `campaign_state_path`
    is kept for CLI/API continuity and unused. Intervals of 0 disable heartbeat / metrics."""
    global _shutdown_requested, _RELOAD_DRAINING, _RELOAD_DRAIN_PROJECTS, _ADVANCE_POOL, _RELOAD_ARMED
    _shutdown_requested = False
    paths, pid = paths_for(run_dir), os.getpid()
    from factory_v2 import run_windows       # daemon-owned evidence: a missing terminal row is crash truth
    run_window_id = run_windows.open_run(run_dir, pid, configured_max_workers())
    state = _load_or_create_state(paths["state"], pid, poll_interval_s)
    sstate.save_state(paths["state"], state)
    _portfolio_dir = _portfolio_file = None
    if portfolio_path:
        _portfolio_file = Path(portfolio_path).resolve()
        _portfolio_dir = _portfolio_file.parent
        _log(f"V2_PORTFOLIO_MODE: portfolio={portfolio_path} — select_project() governs next work. DEFAULT_RUNTIME=V2.")
    elif v2_project_dir is not None:
        _log(f"V2_SUPERVISOR_MODE: project_dir={v2_project_dir} — advance() is sole authority")
    else:
        raise ValueError("a portfolio or V2 project directory is required")
    _cto_startup_check(_CTO_DIR, _log)   # CTOK-007: cold restart continuity, non-fatal
    try:
        from factory_v2.continuation_controller import RunDisposition, decide_continuation
    except Exception:
        decide_continuation = None
    try:
        from factory_v2.safety_quarantine import adopt_quarantine, enter_quarantine, is_in_quarantine
    except Exception:
        enter_quarantine = None
    # LR4: SAFETY_STOP quarantines a project; the daemon stays alive and keeps driving the
    # control plane (repair) until a verified repair receipt clears it.
    _safety_quarantine = False
    if enter_quarantine and _portfolio_dir is None and adopt_quarantine(v2_project_dir) is not None:
        _log(f"SAFETY_QUARANTINE_ADOPTED: resuming quarantine after restart (project={v2_project_dir.name})")
        _safety_quarantine = True
    polls = _v2_consecutive_blocked = 0
    go = {"human": 0, "agent": 0}        # HUMAN_GO vs LLM GO requests, counted apart
    backoff = {"key": None, "streak": 0}
    # sub-second polling is the test/canary regime: no backoff there
    ceiling_s = float(poll_interval_s) if (poll_interval_s < 1.0 or backoff_ceiling_s <= 0) else float(backoff_ceiling_s)
    _log_target_was_detached = _reload_pending = False
    _heartbeat_last_ts = _metrics_report_last_ts = float("-inf")
    _canary_cooldown: dict = {"ts": 0.0}
    try:                                 # boot HEAD: reload too when HEAD moves on daemon code
        _boot_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(_REPO_ROOT),
                                    capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        _boot_head = ""
    _head_check_last_ts = time.monotonic()
    _project_dirs = _portfolio_project_dirs(v2_project_dir, _portfolio_file)
    _stop_this_loop = {"stop": False}
    action, _sleep_s = None, float(poll_interval_s)
    try:
        import faulthandler
        faulthandler.register(signal.SIGUSR1, file=sys.stderr, all_threads=True, chain=False)
    except Exception:
        pass
    try:
        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)
        write_pid_file(paths["pid"], pid)    # handlers first: the pid invites SIGTERM
        _LOOP_WATCHDOG_STOP["stop"] = False
        _start_loop_watchdog(_stop_this_loop)   # armed adjacent to the loop head: setup never counts
        while not _shutdown_requested:
            _LOOP_LAST_ITER["ts"] = time.monotonic()
            try:
                _sweep_orphan_engines(_log, _project_dirs)
            except Exception:
                pass
            _repo_hygiene_cycle(_log)
            if log_target_is_live(paths["log"]):
                _log_target_was_detached = False
            elif not _log_target_was_detached:
                _log("LOG_TARGET_DETACHED: factoryd.log no longer resolves to the inode this process "
                     "writes to — readers are on a stale snapshot")
                _emit_cto_event("LOG_TARGET_DETACHED", "", None)
                _log_target_was_detached = True

            if _safety_quarantine:
                # F1: quarantine gates product dispatch only — advance() keeps running the
                # control plane (reconcile + DIAGNOSE / SAFE_REPAIR / VALIDATE_REPAIR)
                state.last_heartbeat = sstate.utc_now()
                sstate.save_state(paths["state"], state)
                if v2_project_dir is None:
                    _log(f"SAFETY_QUARANTINE_IDLE: heartbeat at {state.last_heartbeat} — no V2 project to repair")
                else:
                    try:
                        _q_action = _advance_v2(v2_project_dir, _log)
                        _log(f"SAFETY_QUARANTINE_REPAIR_CYCLE: action={_q_action} — control plane running, product dispatch blocked")
                        if enter_quarantine and not is_in_quarantine(v2_project_dir):
                            _safety_quarantine = False
                            _log("SAFETY_QUARANTINE_CLEARED: verified repair receipt — resuming normal dispatch with no human gate")
                    except Exception as _qexc:
                        _log(f"SAFETY_QUARANTINE_REPAIR_ERROR: {_qexc}")
                polls += 1
                if (max_polls is not None and polls >= max_polls) or _shutdown_requested:
                    break
                _interruptible_sleep(idle_poll_interval_s)
                continue

            action = _selected = sel = None
            _reason = ""
            try:
                if _portfolio_dir is None:
                    _selected = v2_project_dir
                else:
                    from factory_v2.runtime import select_project
                    sel = select_project(_portfolio_dir, portfolio_file=_portfolio_file, cursor_path=run_dir / "portfolio_cursor.json")
                    _reason = getattr(sel, "error", "") or ""
                    if sel.kind == "PROJECT_SELECTED":
                        _selected = sel.project_dir
                    elif sel.kind == "PORTFOLIO_COMPLETE":
                        action = "PORTFOLIO_COMPLETE"
                        _log("PORTFOLIO_COMPLETE: all projects done")
                    elif sel.kind == "PORTFOLIO_UNAVAILABLE":   # misconfiguration, never "done"
                        action = "PORTFOLIO_UNAVAILABLE"
                        _log("PORTFOLIO_UNAVAILABLE: portfolio.json not found — idling (control plane misconfiguration)")
                    else:                                       # a malformed project is never "done"
                        action = "CONTROL_PLANE_ERROR"
                        _log(f"CONTROL_PLANE_ERROR: {sel.error if sel.kind == 'CONTROL_PLANE_ERROR' else 'unknown SelectResult kind %r' % sel.kind}"
                             " — backing off, will retry next poll")
                if _selected is not None:
                    # F5R5: SAFETY_STOP read BEFORE advance, so a stop written last cycle holds now
                    _cause = _safety_stop_cause(_selected)
                    if _cause is not None:
                        _log(f"SAFETY_STOP_PREFLIGHT: {_selected.name} — {_cause}")
                        action, _reason = "SAFETY_STOP", _cause
                        if enter_quarantine is None:
                            _shutdown_requested = True
                        else:                                   # LR4: quarantine, never exit
                            enter_quarantine(_selected, cause=_cause)
                            _safety_quarantine = _portfolio_dir is None
                            _log(f"SAFETY_QUARANTINE_ENTERED: {_selected.name} — no new dispatches; heartbeat continues")
                    if action != "SAFETY_STOP" or (_portfolio_dir is not None and enter_quarantine):
                        # a quarantined portfolio project's control-plane repair keeps its turn;
                        # single-project mode always advances serially
                        action = (_advance_parallel(_selected, _log) if _portfolio_dir is not None
                                  and configured_max_workers() > 1 else _advance_v2(_selected, _log))
                        _reason = _LAST_HARVEST["detail"] if _LAST_HARVEST.get("project") is not None else _LAST_ADVANCE_DETAIL

                # HOT_RELOAD_V1: only the factory's own settlement (the HARVESTED project in
                # parallel mode) schedules a reload; touched_files come from the settlement
                harvested = _LAST_HARVEST.get("project") is not None
                _settled_project = _LAST_HARVEST["project"] if harvested else _selected
                _settled_touched = _LAST_HARVEST["touched"] if harvested else _LAST_ADVANCE_TOUCHED_FILES
                from factory_v2 import hot_reload
                if action == "MILESTONE_SETTLED" and _is_factory_project(_settled_project) and hot_reload.reload_needed(_settled_touched):
                    _reload_pending = True
                    _begin_reload_drain(_project_dirs)
                    _log(f"RELOAD_SCHEDULED: factory settlement changed {', '.join(_settled_touched)}")
                if not _reload_pending and _boot_head and time.monotonic() - _head_check_last_ts > 120:
                    _head_check_last_ts = time.monotonic()
                    try:
                        _hn, _hh, _ht = hot_reload.head_moved_reload_needed(_REPO_ROOT, _boot_head)
                    except Exception:
                        _hn = False
                    if _hn:
                        _reload_pending = True
                        _begin_reload_drain(_project_dirs)
                        _log(f"RELOAD_SCHEDULED: control HEAD moved {_boot_head[:8]}→{_hh[:8]} on daemon code ({', '.join(_ht[:6])})")
                _harvest_parallel(_log)     # a completed Future is bookkeeping, not work
                if hot_reload.decide(_reload_in_flight(_selected, action), _reload_pending) == "RELOAD_NOW":
                    if not _canary_allows_reload(_log, _canary_cooldown, code_sha=_boot_head):
                        # red canary: reload stays pending (retried at the next settlement); the
                        # drain is lifted so the factory keeps working while HEAD is broken
                        _reload_pending, _RELOAD_DRAINING, _RELOAD_DRAIN_PROJECTS = True, False, []
                    elif not _armed_exit_allowed():
                        _arm_reload(_log)   # a future submitted THIS poll may be about to dispatch
                        _reload_pending = True
                    else:
                        _log("RELOAD_NOW: no mission in flight; exiting for launchd restart")
                        try:                # never join pool threads: the next daemon adopts leases
                            if _ADVANCE_POOL is not None:
                                _ADVANCE_POOL.shutdown(wait=False, cancel_futures=True)
                        except Exception:
                            pass
                        sys.exit(3)

                # G2: liveness proof + stall detection ("WAITING_NETWORK" is non-progress too)
                state.last_heartbeat = sstate.utc_now()
                state.total_polls = getattr(state, "total_polls", 0) + 1
                if action in ("PROJECT_COMPLETE", "MILESTONE_SETTLED", "PORTFOLIO_COMPLETE"):
                    _v2_consecutive_blocked = 0
                elif action in _NON_PROGRESS_ACTIONS:
                    _v2_consecutive_blocked += 1
                    if _v2_consecutive_blocked >= _V2_STALL_THRESHOLD:
                        _log(f"V2_STALL_DETECTED: {_v2_consecutive_blocked} consecutive non-progress cycles "
                             f"(action={action}) — intervention may be needed")
                        state.stall_count = getattr(state, "stall_count", 0) + 1

                _effective_portfolio_dir = _portfolio_dir if _portfolio_dir is not None else (_selected.parent if _selected else None)
                if decide_continuation is not None and _effective_portfolio_dir is not None:
                    disp = _continuation(
                        decide_continuation, RunDisposition, go, _log, portfolio_dir=_effective_portfolio_dir,
                        project_dir=_selected, last_advance_action=action, portfolio_file=_portfolio_file,
                        single_project=_portfolio_dir is None, selection=sel,
                        cursor_path=run_dir / "portfolio_cursor.json")   # CTL-04: same cursor as select_project()
                    if disp == "CONTINUE_NOW":          # progress: advance again without sleeping
                        sstate.save_state(paths["state"], state)
                        polls += 1
                        if max_polls is not None and polls >= max_polls:
                            break
                        continue
                    if disp == "IDLE":
                        _log(f"DAEMON_IDLE: portfolio complete — IDLE_WAITING_FOR_NEW_WORK (next check in {idle_poll_interval_s}s)")
                    if disp in ("IDLE", "BACKOFF"):
                        _interruptible_sleep(idle_poll_interval_s)
                    elif disp == "BREAK" or (isinstance(disp, tuple) and not (enter_quarantine and _selected is not None)):
                        break
                    elif isinstance(disp, tuple):           # SAFETY_STOP → quarantine, daemon stays alive
                        enter_quarantine(_selected, cause=disp[1])
                        _safety_quarantine = _portfolio_dir is None
                        _log(f"SAFETY_QUARANTINE_ENTERED: {_selected.name} — no new dispatches; heartbeat continues")
            except Exception as exc:        # a bad/racing read must not kill a persistent daemon
                print(f"[factoryd] poll_iteration error (non-fatal): {exc}", file=sys.stderr)
            sstate.save_state(paths["state"], state)
            polls += 1
            if (max_polls is not None and polls >= max_polls) or _shutdown_requested:
                break
            _sleep_s = _next_sleep_s(action, _reason, poll_interval_s, ceiling_s, backoff)
            now_ts = time.monotonic()
            if heartbeat_log_interval_s and heartbeat_log_interval_s > 0 and now_ts - _heartbeat_last_ts >= heartbeat_log_interval_s:
                _heartbeat_last_ts = now_ts
                _log(_heartbeat_line(pid, polls, action, _reason, backoff["streak"], _sleep_s, v2_project_dir))
            if metrics_report_interval_s and metrics_report_interval_s > 0 and now_ts - _metrics_report_last_ts >= metrics_report_interval_s:
                _metrics_report_last_ts = now_ts
                try:
                    _generate_metrics_report(run_dir, _portfolio_project_dirs(v2_project_dir, _portfolio_file))
                except Exception as _mexc:
                    _log(f"METRICS_REPORT_FAILED: {_mexc}")
            _interruptible_sleep(_sleep_s)
    finally:
        _stop_this_loop["stop"] = _LOOP_WATCHDOG_STOP["stop"] = True   # disarm THIS run's watchdog only
        run_windows.close_run(run_dir, run_window_id)
        # drain flags and the (possibly shut-down) pool live only for this loop's lifetime
        _RELOAD_DRAINING, _RELOAD_DRAIN_PROJECTS, _RELOAD_ARMED, _ADVANCE_POOL = False, [], False, None
        _ADVANCE_STARTED.clear()
        try:
            paths["pid"].unlink()
        except OSError:
            pass

def cmd_start(args) -> int:
    run_dir = Path(args.run_dir)
    paths = paths_for(run_dir)
    try:
        lock_fh = acquire_lock(paths["lock"], LOCK_TIMEOUT_S)
    except LockTimeout:
        st = sstate.load_state(paths["state"])
        print(f"FACTORYD_ALREADY_RUNNING: PID={read_pid_file(paths['pid'])}, started={st.started_at if st else 'unknown'}")
        return 1
    stale_pid = read_pid_file(paths["pid"])   # the lock is the truth; a live pid here is a bug
    if stale_pid is not None and not cdet.is_worker_dead(stale_pid):
        print(f"FACTORYD_ALREADY_RUNNING: PID={stale_pid}, started=unknown")
        lock_fh.close()
        return 1
    portfolio_p = None if args.v2_project_dir else Path(args.portfolio)
    if portfolio_p is not None and not portfolio_p.exists():
        print(f"ERROR: portfolio.json not found at {portfolio_p}.", file=sys.stderr)
        return 1
    if portfolio_p is not None:
        print(f"[factoryd] V2_PORTFOLIO_MODE: portfolio={portfolio_p} — select_project() governs next work.")
    v2_project_dir = Path(args.v2_project_dir).resolve() if args.v2_project_dir else portfolio_p.parent.resolve()
    kwargs = dict(max_polls=args.max_polls, v2_project_dir=v2_project_dir, portfolio_path=str(portfolio_p) if portfolio_p else None,
                  idle_poll_interval_s=getattr(args, "idle_poll_interval_s", 30),
                  backoff_ceiling_s=getattr(args, "backoff_ceiling_s", 1800.0),
                  heartbeat_log_interval_s=getattr(args, "heartbeat_log_interval_s", 600.0),
                  metrics_report_interval_s=getattr(args, "metrics_report_interval_s", 3600.0))
    if args.foreground:
        run_daemon_loop(Path(args.campaign_state), run_dir, args.poll_interval_s, **kwargs)
        return 0
    # Double-fork daemonize; `lock_fh` stays open across the forks: the final daemon holds the flock.
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    sys.stdout.flush()
    sys.stderr.flush()
    os.dup2(os.open(os.devnull, os.O_RDWR), 0)
    log_fd = os.open(str(paths["log"]), os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o644)
    os.dup2(log_fd, 1)
    os.dup2(log_fd, 2)
    run_daemon_loop(Path(args.campaign_state), run_dir, args.poll_interval_s, **kwargs)
    os._exit(0)

def cmd_stop(args) -> int:
    paths = paths_for(Path(args.run_dir))
    pid = read_pid_file(paths["pid"])
    if pid is None:
        print("FACTORYD_NOT_RUNNING")
        return 1
    if cdet.is_worker_dead(pid):
        print(f"FACTORYD_NOT_RUNNING: stale PID file for dead pid={pid}, removing")
        paths["pid"].unlink(missing_ok=True)
        return 1
    ok, why = pidfile_names_daemon(paths["pid"], pid)   # L3-012: a live pid may have been reused
    if not ok:
        print(f"FACTORYD_STALE_PIDFILE: pid={pid} not verified as this daemon — {why}; no signal sent")
        return 1
    descendants = _descendants_of(pid)   # recorded BEFORE the daemon dies (they reparent to 1)
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + args.stop_timeout_s
    while paths["pid"].exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    stopped = not paths["pid"].exists()
    reaped = _reap_pids(descendants)     # stopping the supervisor stops what it launched
    if reaped:
        print(f"FACTORYD_DESCENDANTS_REAPED: {len(reaped)} (pgids={reaped})")
    print(f"FACTORYD_STOPPED: PID={pid}" if stopped else
          f"FACTORYD_STOP_TIMEOUT: PID={pid} did not exit within {args.stop_timeout_s}s")
    return 0 if stopped else 1

def v2_status_summary(project_dir: "Path") -> dict:
    """IN_FLIGHT / BLOCKED / LIVENESS_UNKNOWN mission counts of a V2 project (observed=False: state unreadable)."""
    project_dir = Path(project_dir)
    try:
        state = json.loads((project_dir / "PROJECT_STATE.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"observed": False, "in_flight": 0, "blocked": 0, "missions": []}
    try:
        from factory_v2.mission_evidence import HeartbeatObservationUnknown, is_worker_alive
    except Exception:
        is_worker_alive, HeartbeatObservationUnknown = None, ()  # type: ignore[assignment]
    rows = []
    for mid, rec in (state.get("missions") or {}).items():
        if not isinstance(rec, dict):
            continue
        try:
            alive = bool(is_worker_alive and is_worker_alive(project_dir, mid))
        except HeartbeatObservationUnknown:   # FCO_SYNTH-002: corrupt heartbeat = UNKNOWN, never silence
            rows.append({"mission_id": mid, "status": "LIVENESS_UNKNOWN"})
            continue
        if alive:
            rows.append({"mission_id": mid, "status": "IN_FLIGHT"})
        elif rec.get("last_blocked_fingerprint") and rec.get("state") != "PASS":
            rows.append({"mission_id": mid, "status": "BLOCKED"})
    count = lambda st: sum(1 for r in rows if r["status"] == st)   # noqa: E731
    return {"observed": True, "in_flight": count("IN_FLIGHT"), "blocked": count("BLOCKED"),
            "liveness_unknown": count("LIVENESS_UNKNOWN"), "missions": rows}

def cmd_status(args) -> int:
    paths = paths_for(Path(args.run_dir))
    pid = running_pid(paths["pid"])
    if pid is None:
        print("FACTORYD_STATUS: NOT_RUNNING")
        return 1
    state = sstate.load_state(paths["state"])
    print(f"FACTORYD_STATUS: RUNNING PID={pid}")
    if state:
        s = state
        print(f"STARTED_AT={s.started_at} LAST_HEARTBEAT={s.last_heartbeat} TOTAL_POLLS={s.total_polls} "
              f"ACTIVE_WORKERS={len(s.active_workers)}")
        print(f"STALL_COUNT={s.stall_count} STALE_ACTIVE_COUNT={s.stale_active_count} LOST_MISSION_COUNT={s.lost_mission_count} "
              f"DUPLICATE_DISPATCH_COUNT={s.duplicate_dispatch_count} BLOCKED_DEPENDENCY_COUNT={s.blocked_dependency_count} "
              f"IDLE_POLL_COUNT={s.idle_poll_count} TRUE_HUMAN_GATE_COUNT={s.true_human_gate_count}")
        for w in s.active_workers:
            print(f"  WORKER mission={w['mission_id']} pid={w['pid']}")
    # V2 truth: IN_FLIGHT/BLOCKED from PROJECT_STATE.json, never the (empty) V1 worker list
    for pd in _portfolio_project_dirs(getattr(args, "v2_project_dir", None), getattr(args, "portfolio", None)):
        summary = v2_status_summary(pd)
        if summary["observed"]:
            print(f"V2_PROJECT={pd} V2_IN_FLIGHT={summary['in_flight']} V2_BLOCKED={summary['blocked']}")
            for m in summary["missions"]:
                print(f"  V2_MISSION mission={m['mission_id']} status={m['status']}")
    return 0

def cmd_restart(args) -> int:
    cmd_stop(args)
    return cmd_start(args)

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="factoryd", description="Factory daemon. DEFAULT_RUNTIME=V2 (portfolio supervisor).")
    p.add_argument("command", choices=["start", "stop", "status", "restart"])
    p.add_argument("--run-dir", default=str(DEFAULT_RUN_DIR))
    p.add_argument("--poll-interval-s", type=float, default=DEFAULT_POLL_INTERVAL_S)
    p.add_argument("--max-polls", type=int, default=None, help="stop after N polls instead of running forever (testing)")
    for flag, typ, default, help_ in (
            ("--backoff-ceiling-s", float, 1800.0, "ECO-02: max sleep when the same (action, reason) repeats (0 disables)"),
            ("--idle-poll-interval-s", int, 30, "seconds between portfolio re-checks when IDLE_WAITING_FOR_NEW_WORK"),
            ("--heartbeat-log-interval-s", float, 600.0, "max seconds between '[factoryd] HEARTBEAT' lines (0 disables)"),
            ("--metrics-report-interval-s", float, 3600.0, "max seconds between BUILD_METRICS_LATEST regenerations (0 disables)")):
        p.add_argument(flag, type=typ, default=default, dest=flag[2:].replace("-", "_"), help=f"{help_} (default {default})")
    p.add_argument("--foreground", action="store_true", help="do not daemonize; block in the current process (launchd, testing)")
    p.add_argument("--stop-timeout-s", type=float, default=10.0)
    p.add_argument("--v2-project-dir", default=None, help="single-project mode: advance(project_dir) instead of the portfolio")
    p.add_argument("--portfolio", default=str(_DEFAULT_PORTFOLIO_JSON),
                   help=f"portfolio.json the supervisor selects projects from (default: {_DEFAULT_PORTFOLIO_JSON})")
    p.add_argument("--campaign-state", default=str(DEFAULT_CAMPAIGN_STATE),
                   help="accepted for continuity of the CLI contract; unused by the V2 loop")
    return p

def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)
    return {"start": cmd_start, "stop": cmd_stop, "status": cmd_status, "restart": cmd_restart}[args.command](args)

def _cli() -> None:
    try:
        raise SystemExit(main())
    except SystemExit as exc:
        if exc.code == 3:               # a hung pool worker must not hold launchd's restart
            hard_exit = threading.Timer(30.0, lambda: os._exit(3))
            hard_exit.daemon = True
            hard_exit.start()
        raise

if __name__ == "__main__":
    _cli()
