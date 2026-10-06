"""Defect 021 — single-flight validation and resource-aware execution (minimal slice).

Invariant: for a normalized validation identity there is AT MOST ONE live execution.
A second requester attaches to the existing durable job; it never launches an
equivalent process. `exit 143`-style envelope kills are classified, never treated as
a product FAIL while the child lives/progresses.

This reuses the Factory's durability shapes (atomic_write_json) and the worker
identity conventions (pid + pgid + birth identity + generation). It does NOT create
a parallel scheduler: callers (dispatchers, gates, harnesses) keep driving their own
work; this module only owns the uniqueness of expensive identical executions.

validation_key = hash(candidate_sha, contract_id, suite_manifest_sha, verifier_sha,
                      immutable_environment_identity)

Job states (typed, per Defect 021):
    PREPARED, RUNNING, PASS, FAIL, HARNESS_TIMED_OUT, PROCESS_LOST, CANCELLED,
    INDETERMINATE
"""
from __future__ import annotations

import calendar
import fcntl
import hashlib
import json
import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

from .project_authority import atomic_write_json
from factory_v2._io import _file_lock, _utc_epoch as _parse_ts, _now

SCHEMA = "validation-job/v1"


class StaleSettlement(Exception):
    """The settler no longer owns the current generation of the job."""
JOB_STATES = frozenset({
    "PREPARED", "RUNNING", "PASS", "FAIL",
    "HARNESS_TIMED_OUT", "PROCESS_LOST", "CANCELLED", "INDETERMINATE",
    # CTO DIRECTIVE 032 §8: a PREPARED job whose owner died BETWEEN prepare
    # and spawn (no pid/pgid was ever observed) is its own typed class — it is
    # not PROCESS_LOST (no process was ever launched to lose) and it must never
    # silently block a boundary or wait forever.
    "ORPHAN_PREPARED",
})
_REUSABLE_STATES = frozenset({"PASS", "FAIL"})  # envelope timeouts are never reusable
_BOUNDED_RETRY_NOTE = ("exactly-once fence; one replacement per generation "
                       "bump; no unbounded wait (D032 s8)")


def validation_key(candidate_sha: str, contract_id: str, suite_manifest_sha: str,
                   verifier_sha: str, env_identity: str) -> str:
    payload = ":".join([candidate_sha, contract_id, suite_manifest_sha,
                        verifier_sha, env_identity])
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


def jobs_dir(control_root: Path) -> Path:
    d = Path(control_root) / "factory_v2" / "receipts" / "validation_jobs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _job_path(control_root: Path, key: str) -> Path:
    return jobs_dir(control_root) / f"{key}.json"


def _read(path: Path) -> dict | None:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if d.get("schema") == SCHEMA else None
    except Exception:
        return None


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True


def _child_alive(job: dict) -> bool:
    """pid alive AND same birth identity — the ONE liveness predicate for a
    RUNNING job (L5_factory_v2_rest_2-012). `acquire` already compared the
    birth; reconcile and the envelope scan now share the same judgement, so a
    reused pid never keeps a job RUNNING that its owner has left."""
    pid = job.get("pid")
    if not _pid_alive(pid):
        return False
    birth = job.get("birth")
    return not birth or birth == _birth_of(pid)


def _birth_of(pid: int | None) -> str:
    """Best-effort process birth identity (lstart), '' when unavailable."""
    if not pid:
        return ""
    try:
        r = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


@contextmanager
def _job_lock(control_root: Path, key: str):
    """Cross-process CAS: one writer per validation key at a time."""
    with _file_lock(jobs_dir(control_root) / f"{key}.lock"):
        yield


def acquire(control_root: Path, key: str, *, command: list, cwd: str,
            envelope_s: float, owner_note: str = "") -> dict:
    """Acquire the single-flight slot for `key`.

    Returns a dict: {"owned": bool, "job": record}.
      owned=True  -> caller may launch/owns the execution (fresh or fenced replacement)
      owned=False -> an equivalent job exists with a LIVE owner: ATTACH, do not
                     launch. This covers RUNNING jobs (live child) and PREPARED
                     jobs (owner in the acquire→mark_running window). The job
                     record carries its pid/pgid for observation.
    A terminal PASS/FAIL record for the same identity inputs is returned as a
    reusable receipt (owned=False, reuse=True). HARNESS_TIMED_OUT/INDETERMINATE are
    never reusable.
    """
    path = _job_path(control_root, key)
    with _job_lock(control_root, key):
        job = _read(path)
        now = _now()
        if job and job.get("state") == "RUNNING":
            alive = _pid_alive(job.get("pid")) and (
                not job.get("birth") or job.get("birth") == _birth_of(job.get("pid")))
            if alive:
                return {"owned": False, "attach": True, "job": job}
            # stale/dead owner: fence exactly once, then one replacement may start
            fenced = dict(job)
            fenced["state"] = "PROCESS_LOST"
            fenced["fenced_at"] = now
            fenced["fenced_reason"] = "owner pid dead or birth identity mismatch"
            atomic_write_json(path, fenced)
            job = fenced
        if job and job.get("state") == "PREPARED":
            # D021 F1 (Claude gate round 1): a PREPARED job has a REAL owner — the
            # caller between acquire() and mark_running(). Concurrent first-callers
            # must ATTACH to that owner, never each become a replacement owner (the
            # generation bump is reserved for a provably DEAD owner: crash between
            # acquire and launch). Without this branch N simultaneous first-callers
            # all return owned=True even under the flock — the lock serializes the
            # file, not ownership.
            owner_pid = job.get("owner_pid")
            alive = _pid_alive(owner_pid) and (
                not job.get("owner_birth")
                or job.get("owner_birth") == _birth_of(owner_pid))
            if alive:
                return {"owned": False, "attach": True, "job": job,
                        "attach_reason": "PREPARED owner live (pre-launch window)"}
            fenced = dict(job)
            if job.get("pid") or job.get("pgid"):
                # a child WAS observed at some point: a real process was lost
                fenced["state"] = "PROCESS_LOST"
                fenced["fenced_reason"] = "prepared owner dead with an observed child pid"
            else:
                # D032 s8: wrapper died BETWEEN PREPARED and spawn — no job
                # process ever existed; typed ORPHAN_PREPARED, never an
                # infinite wait and never a silent boundary block.
                fenced["state"] = "ORPHAN_PREPARED"
                fenced["fenced_reason"] = "owner pid dead before spawn (no job process observed)"
            fenced["fenced_at"] = now
            fenced["retry_policy"] = _BOUNDED_RETRY_NOTE
            atomic_write_json(path, fenced)
            job = fenced
        if job and job.get("state") in _REUSABLE_STATES:
            return {"owned": False, "attach": False, "reuse": True, "job": job}
        generation = (job.get("generation", 0) + 1) if job else 1
        fresh = {
            "schema": SCHEMA,
            "key": key,
            "state": "PREPARED",
            "generation": generation,
            "command": list(command),
            "cwd": str(cwd),
            "envelope_s": envelope_s,
            "owner_note": owner_note[:200],
            "prepared_at": now,
            "owner_pid": os.getpid(),
            "owner_birth": _birth_of(os.getpid()),
            "pid": None, "pgid": None, "birth": "",
            "result": None,
        }
        atomic_write_json(path, fresh)
        return {"owned": True, "attach": False, "job": fresh}


def mark_running(control_root: Path, key: str, pid: int, pgid: int | None = None) -> dict:
    path = _job_path(control_root, key)
    with _job_lock(control_root, key):
        job = _read(path) or {}
        job.update({"state": "RUNNING", "pid": int(pid),
                    "pgid": int(pgid) if pgid else None,
                    "birth": _birth_of(pid),
                    "started_at": _now()})
        atomic_write_json(path, job)
        return job


def settle(control_root: Path, key: str, *, rc: int, classified: str,
           duration_s: float, evidence_path: str = "",
           generation: int | None = None) -> dict:
    """Terminal settlement. With `generation`, refuse stale-owner overwrites:
    a wrapper whose job was fenced and replaced (generation bumped by a new
    owner) must not settle over its replacement's verdict.
    """
    if classified not in JOB_STATES:
        raise ValueError(f"untyped settlement class {classified!r}")
    path = _job_path(control_root, key)
    with _job_lock(control_root, key):
        job = _read(path) or {}
        if generation is not None and job.get("generation") != generation:
            raise StaleSettlement(
                f"settle refused: job generation {job.get('generation')} != "
                f"settler generation {generation} (owner was fenced and replaced)")
        job["state"] = classified
        job["result"] = {"rc": int(rc), "classified": classified,
                         "duration_s": round(float(duration_s), 2),
                         "evidence_path": evidence_path,
                         "settled_at": _now()}
        atomic_write_json(path, job)
        return job


def classify(rc: int, *, envelope_expired: bool = False, child_alive: bool = False,
             child_progressed: bool = False) -> str:
    """Timeout taxonomy (Defect 021 §3).

    Envelope expiry with a live/progressing child is HARNESS_TIMED_OUT (an
    observation about the tool envelope), never product FAIL. A child that died
    without a receipt is PROCESS_LOST. An envelope kill on a dead, non-progressing
    child with no assertion output is INDETERMINATE, still not FAIL.
    """
    if envelope_expired:
        if child_alive or child_progressed:
            return "HARNESS_TIMED_OUT"
        return "INDETERMINATE"
    if rc == 0:
        return "PASS"
    if rc in (143, -15) or rc == 124:
        # SIGTERM-from-envelope (143) or timeout-kill (124) without envelope flag:
        # classify by what we know — never silently a product failure.
        return "HARNESS_TIMED_OUT" if child_alive else "PROCESS_LOST"
    return "FAIL"


def assert_replay_depth_ok(env: dict | None = None) -> None:
    """Self-host replay nesting depth maximum one (Defect 021 §5).

    Reuses the existing recursion guard variable: a replay running inside another
    replay is a typed rejection, not a recursion.
    """
    e = env if env is not None else os.environ
    if e.get("FACTORY_SELF_HOST_REPLAY_GUARD"):
        raise RuntimeError(
            "SELF_HOST_REPLAY_DEPTH_EXCEEDED: a self-host replay may not launch "
            "another self-host replay (max depth 1, Defect 021)")


def reconcile(control_root: Path) -> list[dict]:
    """Boot reconciliation (D032 §8 closes the class, not the instance).

    RUNNING jobs whose child is dead become PROCESS_LOST; PREPARED jobs whose
    owner died before spawn (no pid/pgid observed) become ORPHAN_PREPARED.
    Both are fenced EXACTLY ONCE (under the job lock, re-checking state) with
    a bounded retry policy: one replacement per generation bump, never an
    infinite wait, never a silent boundary block.
    """
    out = []
    now = _now()
    for path in sorted(jobs_dir(control_root).glob("*.json")):
        job = _read(path)
        if not job:
            continue
        state = job.get("state")
        if state == "RUNNING":
            if not _child_alive(job):
                with _job_lock(control_root, job["key"]):
                    cur = _read(path)
                    if cur and cur.get("state") == "RUNNING" and not _child_alive(cur):
                        cur["state"] = "PROCESS_LOST"
                        cur["fenced_at"] = now
                        cur["fenced_reason"] = "reconcile: owner dead at boot"
                        cur["retry_policy"] = _BOUNDED_RETRY_NOTE
                        atomic_write_json(path, cur)
                        out.append(cur)
        elif state == "PREPARED":
            owner_pid = job.get("owner_pid")
            owner_alive = _pid_alive(owner_pid) and (
                not job.get("owner_birth")
                or job.get("owner_birth") == _birth_of(owner_pid))
            launched = bool(job.get("pid") or job.get("pgid"))
            if not owner_alive and not launched:
                with _job_lock(control_root, job["key"]):
                    cur = _read(path)
                    if (cur and cur.get("state") == "PREPARED"
                            and not _pid_alive(cur.get("owner_pid"))
                            and not (cur.get("pid") or cur.get("pgid"))):
                        cur["state"] = "ORPHAN_PREPARED"
                        cur["fenced_at"] = now
                        cur["fenced_reason"] = ("reconcile: prepared owner dead "
                                                "before spawn (no job process)")
                        cur["retry_policy"] = _BOUNDED_RETRY_NOTE
                        atomic_write_json(path, cur)
                        out.append(cur)
    return out


def envelope_expired_running(control_root: Path) -> list[dict]:
    """Detect RUNNING jobs whose still-alive pid has overrun `envelope_s`.

    AUDIT_QUADRUPLE #9: `acquire()` records `envelope_s` (see above) but
    nothing ever reads it back to act. `reconcile()` (above) only fences the
    ALREADY-DEAD-pid case (PROCESS_LOST / ORPHAN_PREPARED). This function
    covers the distinct ALIVE-but-past-its-envelope case: state == 'RUNNING'
    AND the pid is still alive AND more time than `envelope_s` has elapsed
    since the job actually started running.

    This is DETECTION ONLY: it never mutates a job's state and never kills
    the process. The caller (a periodic scheduler loop, not yet wired — see
    follow_up) is responsible for killing the pid and then calling
    settle()/classify(envelope_expired=True, ...) to close the job typed.

    Age is measured from the first field present of: 'acquired_at' (reserved
    name for a future explicit acquire-time anchor, not currently written by
    any function in this module), 'started_at' (written by mark_running() on
    the RUNNING transition — the natural envelope anchor), or 'prepared_at'
    (written by acquire(), used as a fallback if a job is RUNNING without a
    started_at, which should not normally happen but is handled defensively).
    """
    out = []
    now = time.time()
    for path in sorted(jobs_dir(control_root).glob("*.json")):
        job = _read(path)
        if not job or job.get("state") != "RUNNING":
            continue
        if not _child_alive(job):
            continue  # dead pid is reconcile()'s job, not this one
        envelope_s = job.get("envelope_s")
        if envelope_s is None:
            continue
        anchor = job.get("acquired_at") or job.get("started_at") or job.get("prepared_at")
        anchor_epoch = _parse_ts(anchor)
        if anchor_epoch is None:
            continue
        if (now - anchor_epoch) > float(envelope_s):
            out.append(job)
    return out
