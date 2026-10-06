"""P0-D: Stable mission evidence directory + orphan detection.

F5R2/F5R3: Durable mission leases with continuous heartbeat renewal.

Each mission gets a stable dir under <project>/evidence/missions/<mission-id>/
that persists across process restarts.

Worker liveness protocol:
  1. Before executing: advance() checks is_worker_alive() — if True, return MISSION_IN_FLIGHT
  2. Before executing: advance() calls clear_worker_state() to remove any stale evidence
  3. During execute(): executor creates HeartbeatRenewer and starts it
     - HeartbeatRenewer writes heartbeat.json atomically every HEARTBEAT_INTERVAL_S
     - Heartbeat includes pid + process birth time (PID reuse guard) + pgid + seq
  4. After execute(): executor stops HeartbeatRenewer, calls clear_worker_state()
  5. If process is killed between 3 and 4:
     - HeartbeatRenewer detects proc dead → stops renewing
     - Next advance() sees stale heartbeat → orphan → clear → redispatch

No two writers on one worktree: is_worker_alive() gates every dispatch.

PID reuse guard: heartbeat stores process birth time. On restart, if OS reused the
PID for a different process, birth time mismatch marks worker as dead even if
os.kill(pid, 0) succeeds.
"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import threading
import time
from datetime import timezone
from pathlib import Path
from factory_v2._io import _now

_HEARTBEAT_STALE_S = 90     # heartbeat older than this → orphan
HEARTBEAT_INTERVAL_S = 30   # background thread renewal interval
_SIDECAR_INTERVAL_S = 10    # detached sidecar renewal — 9x margin against staleness


def evidence_dir(project_dir: Path, mission_id: str) -> Path:
    d = Path(project_dir) / "evidence" / "missions" / mission_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_process_birth_time(pid: int) -> str:
    """Return a stable process start identity for PID reuse detection. Empty on error."""
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True, text=True, timeout=5,
        )
        birth_time = result.stdout.strip()
        if result.returncode == 0 and birth_time:
            return birth_time
    except Exception:
        pass

    # Some constrained runtimes deny execution of `ps` while still exposing process
    # metadata through the OS APIs used by psutil. Keep this optional: production
    # environments where `ps` works retain the existing identity format, while a
    # denied observation can still produce a real birth identity instead of weakening
    # is_worker_alive() to accept a bare PID.
    try:
        import psutil
        return f"psutil-create-time:{psutil.Process(int(pid)).create_time():.6f}"
    except Exception:
        pass

    # Sandboxed macOS processes may be allowed to inspect process metadata while
    # being denied permission to exec `ps`.  libproc exposes the same stable start
    # time without launching a subprocess.
    if os.uname().sysname == "Darwin":
        try:
            import ctypes
            import struct

            proc_bsdinfo_size = 136
            proc_pidtbsdinfo = 3
            buf = ctypes.create_string_buffer(proc_bsdinfo_size)
            libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            libproc.proc_pidinfo.argtypes = [
                ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                ctypes.c_void_p, ctypes.c_int,
            ]
            libproc.proc_pidinfo.restype = ctypes.c_int
            size = libproc.proc_pidinfo(
                int(pid), proc_pidtbsdinfo, 0, buf, proc_bsdinfo_size,
            )
            if size == proc_bsdinfo_size:
                seconds, microseconds = struct.unpack_from("=QQ", buf.raw, 120)
                if seconds:
                    return f"{seconds}.{microseconds:06d}"
        except Exception:
            pass
    return ""


def write_heartbeat(
    project_dir: Path,
    mission_id: str,
    pid: int,
    phase: str = "",
    pgid: int | None = None,
    birth_time: str = "",
    generation: str | None = None,
    attempt_id: str | None = None,
    candidate_sha: str = "",
    seq: int = 0,
    lease_started_at: str = "",
) -> None:
    """Write heartbeat atomically (tmp → rename) to prevent torn reads."""
    d = evidence_dir(project_dir, mission_id)
    now = _now()
    # PGID defaults to pid if not provided and we can get it
    if pgid is None:
        try:
            pgid = os.getpgid(pid)
        except Exception:
            pgid = pid
    data = {
        "ts": now,
        "pid": pid,
        "pgid": pgid,
        "birth_time": birth_time,
        "generation": generation or "",
        "attempt_id": attempt_id or "",
        "mission_id": mission_id,
        "phase": phase,
        "candidate_sha": candidate_sha,
        "heartbeat_seq": seq,
        "heartbeat_at": now,
        "lease_started_at": lease_started_at or now,
        "lease_expires_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(time.time() + _HEARTBEAT_STALE_S),
        ),
    }
    hb_path = d / "heartbeat.json"
    # L4_resilience_review-004: the renewer thread and the detached sidecar both
    # publish this file. A SHARED temp name let the second writer replace the
    # temp before the first's rename — FileNotFoundError for one, a lost update
    # for the other. Each writer now owns a unique temp (mkstemp) and publishes
    # with os.replace: both publications succeed, the last one wins whole.
    import tempfile as _tempfile
    fd, tmp_name = _tempfile.mkstemp(prefix="heartbeat.json.", suffix=".tmp", dir=str(d))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(data))
        os.replace(tmp_path, hb_path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


class HeartbeatObservationUnknown(RuntimeError):
    """A heartbeat marker EXISTS but cannot be read (I/O error, truncated or
    non-object JSON). Liveness is UNKNOWN — neither alive nor dead. Callers must
    refuse, hold or fall back to a conservative path; never treat it as absent."""


def read_heartbeat(project_dir: Path, mission_id: str) -> dict | None:
    """Absent marker -> None (definitively no worker evidence).
    Unreadable/corrupt marker -> HeartbeatObservationUnknown (FCO_SYNTH-002):
    this used to return None for ANY exception, so a disk-full/truncated
    heartbeat made is_worker_alive() attest a dead worker and allowed a second
    writer on the same worktree."""
    hb_path = Path(project_dir) / "evidence" / "missions" / mission_id / "heartbeat.json"
    try:
        raw = hb_path.read_text()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise HeartbeatObservationUnknown(f"{hb_path}: unreadable heartbeat: {exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise HeartbeatObservationUnknown(f"{hb_path}: corrupt heartbeat: {exc}") from exc
    if not isinstance(data, dict):
        raise HeartbeatObservationUnknown(
            f"{hb_path}: heartbeat is not an object ({type(data).__name__})")
    return data


def is_worker_alive(project_dir: Path, mission_id: str) -> bool:
    """Return True only if a live, non-stale worker is actively executing this mission.

    Checks:
    1. Heartbeat file exists
    2. Heartbeat is not stale (< _HEARTBEAT_STALE_S old)
    3. PID is live (os.kill(pid, 0) succeeds)
    4. Birth time is present and matches the current process (PID reuse guard)
    """
    hb = read_heartbeat(project_dir, mission_id)
    if hb is None:
        return False
    # Stale timestamp check
    hb_ts = hb.get("ts", "")
    try:
        t = datetime.datetime.strptime(hb_ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        age_s = (datetime.datetime.now(timezone.utc) - t).total_seconds()
        if age_s > _HEARTBEAT_STALE_S:
            return False
    except Exception:
        return False
    # PID liveness check
    pid = hb.get("pid")
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    # Birth identity is REQUIRED, not opportunistic.
    #
    # F-5: this used to read `if recorded_birth:` — so a heartbeat carrying no
    # birth_time skipped the check entirely and any live PID satisfied liveness. The
    # production sidecar wrote exactly such heartbeats, which left the guard dormant on
    # the only path that matters. A heartbeat that names a live PID it cannot prove it
    # owns is indistinguishable from one written by an unrelated process that happens to
    # point at a live PID, so it must not be evidence of anything.
    #
    # Absent birth identity is therefore "not alive", never "assume alive": the failure
    # direction is a redundant fence or a re-dispatch, not two concurrent writers.
    recorded_birth = hb.get("birth_time", "")
    if not recorded_birth:
        return False
    current_birth = get_process_birth_time(int(pid))
    if not current_birth or current_birth != recorded_birth:
        # Either the PID is unreadable, or the OS reused it for a different process.
        return False
    return True


def clear_worker_state(project_dir: Path, mission_id: str) -> None:
    """Remove heartbeat.json. Called on orphan detection or clean executor exit."""
    hb_path = Path(project_dir) / "evidence" / "missions" / mission_id / "heartbeat.json"
    try:
        hb_path.unlink(missing_ok=True)
    except Exception:
        pass
    for tmp_path in Path(project_dir).joinpath("evidence", "missions", mission_id).glob("heartbeat.json.*.tmp"):
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


class HeartbeatRenewer:
    """Background thread that renews heartbeat every interval_s while child process is alive.

    F5R2: Fixes the one-shot heartbeat defect. A healthy worker running >90s no longer
    becomes a false orphan because the renewer keeps the heartbeat fresh.

    Usage:
        renewer = HeartbeatRenewer(project_dir, mission_id, proc)
        renewer.start()
        out, err = proc.communicate()
        renewer.stop()
    """

    def __init__(
        self,
        project_dir: Path,
        mission_id: str,
        pid: int,
        pgid: int | None = None,
        birth_time: str = "",
        generation: str | None = None,
        attempt_id: str | None = None,
        candidate_sha: str = "",
        interval_s: float = HEARTBEAT_INTERVAL_S,
        lease_started_at: str = "",
        fencing_token: str = "",
    ) -> None:
        self.project_dir = project_dir
        self.mission_id = mission_id
        self.pid = pid
        self.pgid = pgid
        self.birth_time = birth_time
        self.generation = generation
        self.fencing_token = fencing_token
        self.attempt_id = attempt_id
        self.candidate_sha = candidate_sha
        self.interval_s = interval_s
        self.lease_started_at = lease_started_at or _now()
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"hb-{mission_id[:16]}")
        self._seq = 0
        self._sidecar = None

    def start(self) -> None:
        self._thread.start()
        # S017-08: the thread dies with this process.  If factoryd is SIGKILLed while the
        # worker it launched keeps running, the heartbeat freezes on a healthy worker and
        # a relaunched supervisor is free to treat it as orphaned and start a second
        # writer.  A detached sidecar process reports on behalf of the WORKER, so daemon
        # death stops looking like worker death.  It exits when the worker PID does.
        from .heartbeat_sidecar import spawn_detached_heartbeat
        self._sidecar = spawn_detached_heartbeat(
            self.project_dir, self.mission_id, self.pid,
            # Well under _HEARTBEAT_STALE_S (90s) so a single missed tick can
            # never make a live worker look orphaned.
            interval_s=min(self.interval_s, _SIDECAR_INTERVAL_S),
            pgid=str(self.pgid or ""), generation=str(self.generation or ""),
            # F-5: without this the sidecar's heartbeats carried no birth identity and
            # the PID-reuse guard never ran on the renewing path.
            birth_time=str(self.birth_time or ""))

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=self.interval_s + 5)
        sidecar = getattr(self, "_sidecar", None)
        if sidecar is not None:
            try:
                sidecar.terminate()
                sidecar.wait(timeout=5)
            except Exception:
                pass
            self._sidecar = None

    def _run(self) -> None:
        while not self._stop_event.wait(timeout=self.interval_s):
            # Check if the child process is still alive before renewing
            try:
                os.kill(self.pid, 0)
            except (ProcessLookupError, OSError):
                break  # Process dead — stop renewing
            self._seq += 1
            try:
                write_heartbeat(
                    self.project_dir,
                    self.mission_id,
                    self.pid,
                    phase="executing",
                    pgid=self.pgid,
                    birth_time=self.birth_time,
                    generation=self.generation,
                    attempt_id=self.attempt_id,
                    candidate_sha=self.candidate_sha,
                    seq=self._seq,
                    lease_started_at=self.lease_started_at,
                )
            except Exception:
                pass  # Best-effort — don't crash the background thread
            # P1 (incident 2026-09-11, worker_lease.py:352-364): the default lease
            # TTL (30 min) is shorter than a long mission's dispatch budget
            # (35 min+) and was never renewed — a healthy long mission wrote its
            # final mutations against an EXPIRED lease and the paid model work
            # was discarded. Renew the lease on the same tick as the heartbeat.
            # L5_factory_v2_rest_3-015: renew as THIS owner (generation + fencing
            # token); a renewer whose lease was replaced by G+1 is refused instead
            # of keeping its replacement alive.
            try:
                from .worker_lease import WorkerLease
                _gen = int(self.generation) if str(self.generation or "").isdigit() else None
                WorkerLease.renew(self.mission_id, self.project_dir,
                                  generation=_gen, fencing_token=self.fencing_token or None)
            except Exception:
                pass  # Best-effort — the lease gate still fail-closes on real expiry


def control_repo_env(base: dict | None = None) -> dict:
    """Environment for running an authority gate command.

    F-6: gate commands used to name the verifier by absolute path into the live
    workspace, so an exact-SHA review in a detached worktree executed the LIVE file
    rather than the one committed at the SHA under review -- and a mutation to the
    verifier was invisible to the very campaign meant to catch it. The gates now
    invoke `python3 -m factory_v2.product_verifiers` and get their root from here.

    Resolved from __file__ on purpose: the code that is executing declares where it
    lives, so the verifier that runs is always the one belonging to the tree being
    reviewed. Never fall back to a hardcoded path -- that is the bug.
    """
    import os
    from pathlib import Path as _P
    env = dict(base if base is not None else os.environ)
    # Authorization stays with the supervisor, never with model or gate children.
    env.pop("FACTORY_OWNER_AUTHORIZATION_KEY", None)
    env.pop("FACTORY_OWNER_AUTHORIZATION_KEYFILE", None)
    root = str(_P(__file__).resolve().parent.parent)
    env["FACTORY_CONTROL_REPO"] = root
    # Runtime observations live outside detached product worktrees. Promotion runs
    # the same contract command as IndependentValidator and needs the supervisor's
    # evidence root as well as the pinned verifier-code root.
    env["FACTORY_SUPERVISOR_EVIDENCE_ROOT"] = root
    env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
    return env
