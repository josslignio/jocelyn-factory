"""Heartbeat renewal that outlives the daemon that started it.

S017-08: `HeartbeatRenewer` is a thread inside the Factory executor, which lives inside
factoryd. SIGKILL the daemon and the thread dies with it — while the model child it
launched keeps working. The heartbeat then goes stale on a worker that is perfectly
healthy, so a relaunched supervisor classifies it as orphaned and is free to start a
second writer on the same project. That is the exact double-writer condition the lease
and transaction lock exist to prevent, reintroduced through liveness reporting.

A thread cannot outlive its process, so this is a process: a small detached sidecar in
its own session that renews the heartbeat while the real worker PID is alive, and exits
when that PID is gone. It reports on behalf of the worker, not on behalf of the daemon,
so daemon death no longer looks like worker death.

Run directly (normally spawned by spawn_detached_heartbeat):
    python3 -m factory_v2.heartbeat_sidecar <project_dir> <mission_id> <worker_pid> \
        [--interval-s 15] [--pgid N] [--generation G]
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def run(project_dir: Path, mission_id: str, worker_pid: int,
        interval_s: float = 15.0, pgid: str = "", generation: str = "",
        birth_time: str = "", max_s: float = 86400.0) -> int:
    from factory_v2.mission_evidence import write_heartbeat, get_process_birth_time

    # F-5: a heartbeat naming a live PID proves nothing unless it also proves WHICH
    # process that PID is.  The sidecar previously wrote no birth_time, so the reuse
    # guard in is_worker_alive was dormant on the only path that renews in production.
    # Resolve it here if the spawner did not supply it, and refuse to report at all if
    # it cannot be resolved -- claiming liveness we cannot substantiate is the failure
    # this whole mechanism exists to prevent.
    if not birth_time:
        birth_time = get_process_birth_time(worker_pid)
    if not birth_time:
        return 2

    started = time.monotonic()
    while _alive(worker_pid) and (time.monotonic() - started) < max_s:
        # Re-check identity every tick, not just at spawn: if the worker exits and the
        # OS hands its PID to something else, _alive() alone would keep renewing a
        # heartbeat for a stranger and hold the mission open forever.
        if get_process_birth_time(worker_pid) != birth_time:
            break
        try:
            write_heartbeat(project_dir, mission_id, worker_pid, phase="executing",
                            pgid=pgid, generation=generation, birth_time=birth_time)
        except Exception:
            # Never let a transient write error kill liveness reporting; the next tick
            # retries.  A silent exit here would recreate the false-orphan bug.
            pass
        time.sleep(interval_s)
    return 0


def spawn_detached_heartbeat(project_dir, mission_id: str, worker_pid: int,
                             interval_s: float = 15.0, pgid: str = "",
                             generation: str = "", birth_time: str = ""):
    """Start the sidecar in its own session so it survives the caller's death.

    Returns the Popen handle, or None if it could not be started — the caller keeps its
    in-process renewer either way, so a failed sidecar degrades to the old behaviour
    rather than losing the heartbeat entirely.
    """
    repo_root = str(Path(__file__).resolve().parent.parent)
    if not birth_time:
        from .mission_evidence import get_process_birth_time
        birth_time = get_process_birth_time(worker_pid)
    cmd = [sys.executable, "-m", "factory_v2.heartbeat_sidecar",
           str(project_dir), mission_id, str(worker_pid),
           "--interval-s", str(interval_s)]
    if birth_time:
        cmd += ["--birth-time", birth_time]
    if pgid:
        cmd += ["--pgid", str(pgid)]
    if generation:
        cmd += ["--generation", str(generation)]
    env = dict(os.environ)
    env["PYTHONPATH"] = repo_root + os.pathsep + env.get("PYTHONPATH", "")
    try:
        return subprocess.Popen(
            cmd, cwd=repo_root, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,      # detach: not in the daemon's process group
        )
    except Exception:
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("project_dir")
    ap.add_argument("mission_id")
    ap.add_argument("worker_pid", type=int)
    ap.add_argument("--interval-s", type=float, default=15.0)
    ap.add_argument("--pgid", default="")
    ap.add_argument("--generation", default="")
    ap.add_argument("--birth-time", default="")
    args = ap.parse_args(argv)
    return run(Path(args.project_dir), args.mission_id, args.worker_pid,
               args.interval_s, args.pgid, args.generation, args.birth_time)


if __name__ == "__main__":
    raise SystemExit(main())
