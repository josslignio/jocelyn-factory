"""WorkerLease — time-bounded worker ownership of a mission attempt.

Phase 3 (upgrade from LR2): behavioral, transactional, process-identity-bound.

Required fields (Phase 3):
    mission_id, attempt_id, owner_uuid, generation, fencing_token,
    pid, pgid, process_birth_identity, repo_identity, worktree_identity,
    candidate_branch, candidate_sha, heartbeat_seq, heartbeat_time, expiry.

Inter-process safety:
    Lease acquisition and mutation are serialized under fcntl.flock() on a
    per-project lock file. This prevents two concurrent factoryd/worker processes
    from racing on the lease file.

Worker-side heartbeat:
    WorkerHeartbeat (see worker_heartbeat.py) runs inside the worker process and
    updates heartbeat_seq + heartbeat_time independently. It survives factoryd SIGKILL
    because it executes in the worker, not factoryd.

require_current_lease():
    Every critical mutation (commit, receipt write, staging integration, validation,
    promotion, PROJECT_STATE mutation, settlement) must call this guard.

Restart adoption:
    - fresh valid lease + matching live PID/PGID → ADOPT same generation (no G+1)
    - stale lease + matching PGID → terminate PGID → grant G+1
    - identity mismatch → FENCED + quarantine

Invariants:
    STALE_GENERATION_WRITE = 0
    DUPLICATE_DISPATCH = 0
    DUPLICATE_WORKTREE_WRITER = 0
    LOST_WORK = 0
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import platform
import subprocess
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from factory_v2._io import _file_lock, _utc_epoch, _now

_DEFAULT_TTL_S = 1800  # 30 minutes
_LOCK_FILENAME = "worker_lease.lock"


# ── inter-process lock ─────────────────────────────────────────────────────────

@contextmanager
def _exclusive_lock(lease_dir: Path):
    """Exclusive fcntl.flock() over the lease directory.

    Serializes concurrent grant/adopt/fence/expire calls across OS processes.
    Lock is automatically released when the context manager exits (including on
    exception or SIGKILL of the lock holder — lock is held only while the
    context is active, not across process boundaries).
    """
    with _file_lock(lease_dir / _LOCK_FILENAME):
        yield


# ── process identity helpers ───────────────────────────────────────────────────

def _get_pgid(pid: int) -> int:
    """Return the process group ID for the given PID."""
    try:
        return os.getpgid(pid)
    except (ProcessLookupError, PermissionError, OSError):
        return -1


def _process_birth_identity(pid: int) -> str:
    """Stable process birth identifier (starttime from /proc or platform alternative).

    On Linux: reads /proc/<pid>/stat starttime field (ticks since boot).
    On macOS: uses lsof or ps -o lstart — falls back to empty string.
    """
    try:
        if platform.system() == "Linux":
            stat = Path(f"/proc/{pid}/stat").read_text()
            fields = stat.split(")")[-1].split()
            return f"linux-starttime:{fields[19]}"
        elif platform.system() == "Darwin":
            try:
                r = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
            except Exception:
                r = None
            if r is not None and r.returncode == 0 and r.stdout.strip():
                return f"darwin-lstart:{r.stdout.strip()}"
            from .mission_evidence import get_process_birth_time
            birth = get_process_birth_time(pid)
            return f"darwin-lstart:{birth}" if birth else ""
    except Exception:
        pass
    return ""


def _repo_identity(repo_dir: Path) -> str:
    """sha256[:16] of the git remote origin URL or the repo root inode."""
    try:
        r = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=repo_dir, capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            return hashlib.sha256(r.stdout.strip().encode()).hexdigest()[:16]
    except Exception:
        pass
    try:
        st = repo_dir.stat()
        return f"inode:{st.st_ino}"
    except Exception:
        return "unknown"


def _fencing_token(mission_id: str, generation: int, granted_at: str) -> str:
    """Unforgeable fencing token: random UUID (not derivable from public fields).

    B2 fix 2026-09-08: the old SHA-of-public-fields approach was forgeable by anyone
    knowing mission_id, generation, and granted_at. A random UUID persisted with the
    lease is only knowable by reading the lease file — not computable from inputs.
    The mission_id/generation parameters are kept for signature compatibility but
    are no longer included in the token derivation.
    """
    return str(uuid.uuid4())


def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


# ── WorkerLease dataclass ──────────────────────────────────────────────────────

@dataclass
class WorkerLease:
    """Full identity record for a worker dispatched to a mission attempt."""
    mission_id: str
    generation: int
    granted_at: str
    expires_at: str
    state: str  # "ACTIVE" | "EXPIRED" | "ADOPTED" | "FENCED"
    # Phase 3 identity fields (with defaults for backwards compat).
    attempt_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    owner_uuid: str = field(default_factory=lambda: str(uuid.uuid4()))
    fencing_token: str = ""
    pid: int = 0
    pgid: int = 0
    process_birth_identity: str = ""
    repo_identity: str = ""
    worktree_identity: str = ""
    candidate_branch: str = ""
    candidate_sha: str = ""
    heartbeat_seq: int = 0
    heartbeat_time: str = ""

    @property
    def is_expired(self) -> bool:
        try:
            return time.time() > _parse_iso(self.expires_at)
        except Exception:
            return True

    @property
    def is_process_alive(self) -> bool:
        """True if the worker PID is still alive."""
        return self.pid > 0 and _is_pid_alive(self.pid)

    @property
    def is_process_group_alive(self) -> bool:
        """True if any process in the worker PGID is alive."""
        if self.pgid <= 0:
            return False
        try:
            os.killpg(self.pgid, 0)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            return False

    # ── paths ──────────────────────────────────────────────────────────────

    @classmethod
    def _lease_dir(cls, project_dir: Path) -> Path:
        return Path(project_dir) / "evidence" / "leases"

    @classmethod
    def _lease_path(cls, mission_id: str, project_dir: Path) -> Path:
        return cls._lease_dir(project_dir) / f"{_safe_mission_id(mission_id)}.json"

    @classmethod
    def _read_nolock(cls, mission_id: str, project_dir: Path) -> "WorkerLease | None":
        """Read lease without lock — caller must hold lock.

        L5_factory_v2_rest_3-018 (relecture 2026-09): the file name is now
        injective (see `_safe_mission_id`) AND the loaded record must carry the
        requested mission_id — a file naming another mission is never "our"
        lease. Leases written under the old lossy name (`A#M1` -> `A_M1.json`)
        are still read when no new-name file exists and their mission_id
        matches, so a running fleet migrates on its next write.
        """
        path = cls._lease_path(mission_id, project_dir)
        try:
            lease = cls._load_lease_file(path, mission_id)
        except FileNotFoundError:
            legacy = cls._lease_dir(project_dir) / f"{_legacy_safe_mission_id(mission_id)}.json"
            if legacy == path or not legacy.exists():
                return None
            try:
                lease = cls._load_lease_file(legacy, mission_id, strict_id=False)
            except FileNotFoundError:
                return None
        return lease

    @classmethod
    def _load_lease_file(cls, path: Path, mission_id: str, *, strict_id: bool = True):
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            # Support both old (5-field) and new format.
            known = {f.name for f in cls.__dataclass_fields__.values()}
            filtered = {k: v for k, v in d.items() if k in known}
            lease = cls(**filtered)
        except FileNotFoundError:
            raise
        except (OSError, UnicodeError, KeyError, TypeError, json.JSONDecodeError) as exc:
            # Corruption is persistent UNKNOWN, never equivalent to an absent lease.
            raise LeaseObservationUnknown(
                f"lease unreadable for {mission_id}: {type(exc).__name__}: {exc}") from exc
        if lease.mission_id != mission_id:
            if not strict_id:
                return None          # legacy file belongs to another mission
            raise LeaseObservationUnknown(
                f"lease file for {mission_id!r} carries mission_id={lease.mission_id!r}")
        return lease

    def _write_nolock(self, project_dir: Path) -> None:
        """Write lease atomically — caller must hold lock."""
        lease_dir = self._lease_dir(project_dir)
        lease_dir.mkdir(parents=True, exist_ok=True)
        path = self._lease_path(self.mission_id, project_dir)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False) + "\n",
                       encoding="utf-8")
        os.replace(tmp, path)

    # ── public transactional API ───────────────────────────────────────────

    @classmethod
    def grant(
        cls,
        mission_id: str,
        project_dir: Path,
        ttl_s: int = _DEFAULT_TTL_S,
        pid: int = 0,
        pgid: int = 0,
        repo_dir: Path | None = None,
        worktree_dir: Path | None = None,
        candidate_branch: str = "",
        candidate_sha: str = "",
    ) -> "WorkerLease":
        """Grant a new lease. Transactional under inter-process lock.

        Increments generation above any existing lease for this mission_id.
        Fencing token is computed from (mission_id, generation, granted_at).
        """
        lease_dir = cls._lease_dir(project_dir)
        with _exclusive_lock(lease_dir):
            existing = cls._read_nolock(mission_id, project_dir)
            next_gen = (existing.generation + 1) if existing is not None else 1
            now = time.time()
            granted_at = _fmt_iso(now)
            worktree_id = (
                hashlib.sha256(str(worktree_dir).encode()).hexdigest()[:16]
                if worktree_dir else ""
            )
            lease = cls(
                mission_id=mission_id,
                generation=next_gen,
                granted_at=granted_at,
                expires_at=_fmt_iso(now + ttl_s),
                state="ACTIVE",
                attempt_id=str(uuid.uuid4()),
                owner_uuid=str(uuid.uuid4()),
                fencing_token=_fencing_token(mission_id, next_gen, granted_at),
                pid=pid,
                pgid=pgid if pgid > 0 else (_get_pgid(pid) if pid > 0 else 0),
                process_birth_identity=_process_birth_identity(pid) if pid > 0 else "",
                repo_identity=_repo_identity(repo_dir) if repo_dir else "",
                worktree_identity=worktree_id,
                candidate_branch=candidate_branch,
                candidate_sha=candidate_sha,
                heartbeat_seq=0,
                heartbeat_time=granted_at,
            )
            lease._write_nolock(project_dir)
            return lease

    @classmethod
    def adopt(
        cls, mission_id: str, project_dir: Path, holder_pid: int = 0
    ) -> "WorkerLease | None":
        """Return existing unexpired lease if process identity fully validates.

        Restart adoption (B2):
            PID alive + birth_identity matches → ADOPT same generation (no G+1)
            PID dead → worker gone; None (caller must grant G+1)
            PID alive but birth_identity mismatch → FENCED (different process stole PID)
            lease expired/FENCED → None

        Holder binding (dual-holder fix, gate 37ff37c4 finding): when holder_pid
        is supplied, adoption RE-BINDS owner identity (pid/pgid/birth) to the
        adopter and issues a NEW fencing_token at the SAME generation. The
        previous holder's (generation, old token) then fails
        require_current_lease — exactly one live holder at any time. holder_pid=0
        preserves the legacy read-only adoption (identity unchanged) for
        callers that only inspect.

        Returns None in all rejection cases; caller is responsible for grant G+1.
        """
        lease_dir = cls._lease_dir(project_dir)
        with _exclusive_lock(lease_dir):
            lease = cls._read_nolock(mission_id, project_dir)
            if lease is None or lease.is_expired or lease.state in ("EXPIRED", "FENCED"):
                return None

            # Verify PID and birth_identity if lease carries a PID.
            if lease.pid > 0:
                if not _is_pid_alive(lease.pid):
                    # Worker process is gone — cannot adopt; caller must grant G+1.
                    return None
                # PID alive — verify it is the SAME process (birth identity check).
                if lease.process_birth_identity:
                    actual_birth = _process_birth_identity(lease.pid)
                    if not actual_birth:
                        # L5_factory_v2_rest_3-016: the lease names a birth identity
                        # but ps//proc could not observe the live one. That is not
                        # a proven mismatch (no FENCE) and not a match (no ADOPT):
                        # refuse, the caller retries or grants G+1 when the PID dies.
                        return None
                    if actual_birth != lease.process_birth_identity:
                        # Different process took the same PID — FENCE immediately.
                        fenced = WorkerLease(**{**asdict(lease), "state": "FENCED"})
                        fenced._write_nolock(project_dir)
                        return None

            adopted_dict = {**asdict(lease), "state": "ADOPTED"}
            if holder_pid > 0:
                # Re-bind ownership to the adopter; same generation, fresh token.
                adopted_dict.update({
                    "pid": holder_pid,
                    "pgid": _get_pgid(holder_pid),
                    "process_birth_identity": _process_birth_identity(holder_pid),
                    "owner_uuid": str(uuid.uuid4()),
                    "fencing_token": _fencing_token(
                        mission_id, lease.generation,
                        _now()),
                })
            adopted = WorkerLease(**adopted_dict)
            adopted._write_nolock(project_dir)
            return adopted

    @staticmethod
    def _owner_matches(lease: "WorkerLease", generation, fencing_token) -> bool:
        """L5_factory_v2_rest_3-015: a renew/tick must prove WHO is renewing.

        With (generation, fencing_token) both must equal the stored lease —
        the old worker's G1 token can no longer keep its G2 replacement alive.
        Without them (legacy callers) only a lease that was never bound to an
        owner process (pid == 0), or whose bound owner IS the calling process
        (lease.pid == os.getpid(): a live process cannot be a reused pid of
        itself), may be touched: a lease bound to ANOTHER process has someone
        to fence against, so an anonymous caller is refused.
        """
        if generation is None and fencing_token is None:
            return lease.pid <= 0 or lease.pid == os.getpid()
        if generation is None or fencing_token is None:
            return False
        try:
            same_gen = int(generation) == int(lease.generation)
        except (TypeError, ValueError):
            return False
        return same_gen and bool(fencing_token) and fencing_token == lease.fencing_token

    @classmethod
    def renew(cls, mission_id: str, project_dir: Path,
              ttl_s: int = _DEFAULT_TTL_S, *, generation=None,
              fencing_token=None) -> "WorkerLease | None":
        """Extend TTL without changing generation. Returns renewed lease or None.

        `generation`/`fencing_token` identify the caller (see `_owner_matches`)."""
        lease_dir = cls._lease_dir(project_dir)
        with _exclusive_lock(lease_dir):
            lease = cls._read_nolock(mission_id, project_dir)
            if lease is None or lease.is_expired or lease.state in ("EXPIRED", "FENCED"):
                return None
            if not cls._owner_matches(lease, generation, fencing_token):
                return None
            renewed = WorkerLease(
                **{**asdict(lease),
                   "expires_at": _fmt_iso(time.time() + ttl_s),
                   "state": "ACTIVE"}
            )
            renewed._write_nolock(project_dir)
            return renewed

    @classmethod
    def reacquire_for_settlement(cls, mission_id: str, generation: int,
                                 fencing_token: str, project_dir: Path,
                                 ttl_s: int = _DEFAULT_TTL_S) -> bool:
        """Let the SAME owner finish settling a mission whose lease merely lapsed.

        `renew` refuses once a lease is expired, which is right while work is in
        flight — a worker that stopped heartbeating must not keep writing. But a
        mission that already PASSED is not in flight: its settlement is bookkeeping
        the owner still has to finish. Observed 2026-09-11: the daemon was stopped
        for a repair, the 30-minute TTL lapsed, and the completed milestone could
        then NEVER settle — every cycle produced
        STALE_GENERATION_WRITE_BLOCKED/LEASE_VIOLATION. Any crash/restart gap longer
        than the TTL strands finished product work the same way.

        The fence that matters is preserved exactly: this succeeds ONLY when the
        stored lease still carries the caller's own generation AND fencing token,
        i.e. nobody else took the mission over. A competing writer bumps the
        generation, so a genuinely stale writer is still refused, and a FENCED
        lease — a deliberate kill — is never revived.
        """
        lease_dir = cls._lease_dir(project_dir)
        with _exclusive_lock(lease_dir):
            lease = cls._read_nolock(mission_id, project_dir)
            if lease is None or lease.state == "FENCED":
                return False
            if lease.generation != generation or lease.fencing_token != fencing_token:
                return False        # someone else owns it now — stay blocked
            revived = WorkerLease(
                **{**asdict(lease),
                   "expires_at": _fmt_iso(time.time() + ttl_s),
                   "state": "ACTIVE"}
            )
            revived._write_nolock(project_dir)
            return True

    @classmethod
    def expire(cls, mission_id: str, project_dir: Path) -> None:
        """Explicitly mark a lease as expired."""
        lease_dir = cls._lease_dir(project_dir)
        with _exclusive_lock(lease_dir):
            lease = cls._read_nolock(mission_id, project_dir)
            if lease is None:
                return
            expired = WorkerLease(**{**asdict(lease), "state": "EXPIRED"})
            expired._write_nolock(project_dir)

    @classmethod
    def fence(cls, mission_id: str, generation: int, project_dir: Path) -> bool:
        """True iff generation matches current ACTIVE/ADOPTED unexpired lease."""
        lease_dir = cls._lease_dir(project_dir)
        with _exclusive_lock(lease_dir):
            lease = cls._read_nolock(mission_id, project_dir)
            if lease is None:
                return False
            if lease.is_expired or lease.state in ("EXPIRED", "FENCED"):
                return False
            return lease.generation == generation

    @classmethod
    def fence_with_token(cls, mission_id: str, generation: int,
                          fencing_token: str, project_dir: Path) -> bool:
        """Stricter fence: generation AND fencing_token must match."""
        lease_dir = cls._lease_dir(project_dir)
        with _exclusive_lock(lease_dir):
            lease = cls._read_nolock(mission_id, project_dir)
            if lease is None:
                return False
            if lease.is_expired or lease.state in ("EXPIRED", "FENCED"):
                return False
            return (lease.generation == generation
                    and lease.fencing_token == fencing_token)

    @classmethod
    def mark_fenced(cls, mission_id: str, project_dir: Path) -> None:
        """Mark lease as FENCED — identity mismatch detected. Blocks all writes."""
        lease_dir = cls._lease_dir(project_dir)
        with _exclusive_lock(lease_dir):
            lease = cls._read_nolock(mission_id, project_dir)
            if lease is None:
                return
            fenced = WorkerLease(**{**asdict(lease), "state": "FENCED"})
            fenced._write_nolock(project_dir)

    @classmethod
    def tick_heartbeat(cls, mission_id: str, project_dir: Path, *, generation=None,
                       fencing_token=None) -> "WorkerLease | None":
        """Increment heartbeat_seq + update heartbeat_time. Returns updated lease or None.

        L5_factory_v2_rest_3-017: a lease whose `expires_at` has passed is refused
        exactly like `current_generation` refuses it — the heartbeat thread then
        stops by itself instead of decorating a dead lease with fresh sequence
        numbers. `generation`/`fencing_token` bind the tick to its owner (-015).
        """
        lease_dir = cls._lease_dir(project_dir)
        with _exclusive_lock(lease_dir):
            lease = cls._read_nolock(mission_id, project_dir)
            if lease is None or lease.is_expired or lease.state in ("EXPIRED", "FENCED"):
                return None
            if not cls._owner_matches(lease, generation, fencing_token):
                return None
            updated = WorkerLease(
                **{**asdict(lease),
                   "heartbeat_seq": lease.heartbeat_seq + 1,
                   "heartbeat_time": _fmt_iso(time.time())}
            )
            updated._write_nolock(project_dir)
            return updated

    @classmethod
    def current_generation(cls, mission_id: str, project_dir: Path) -> int | None:
        lease_dir = cls._lease_dir(project_dir)
        with _exclusive_lock(lease_dir):
            lease = cls._read_nolock(mission_id, project_dir)
            if lease is None or lease.is_expired or lease.state in ("EXPIRED", "FENCED"):
                return None
            return lease.generation


def require_current_lease(
    mission_id: str,
    generation: int,
    fencing_token: str,
    project_dir: Path,
    operation: str = "mutation",
) -> None:
    """Guard: raise LeaseViolation if the caller's lease is no longer current.

    Must be called before every critical mutation:
        - candidate commit/capture
        - terminal receipt write
        - staging integration
        - validation request
        - Golden promotion
        - PROJECT_STATE mutation
        - milestone settlement

    Enforces STALE_GENERATION_WRITE = 0.
    """
    if not WorkerLease.fence_with_token(mission_id, generation, fencing_token, project_dir):
        raise LeaseViolation(
            f"LEASE_VIOLATION: {operation} rejected — "
            f"lease for mission_id={mission_id!r} generation={generation} "
            f"fencing_token={fencing_token!r} is no longer current. "
            "STALE_GENERATION_WRITE blocked."
        )


class LeaseObservationUnknown(RuntimeError):
    """A durable lease marker exists but its VIVANT/MORT state is unreadable."""


class LeaseViolation(Exception):
    """Critical mutation blocked — worker's lease is stale or fenced."""


# ── internal helpers ──────────────────────────────────────────────────────────

def _fmt_iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _parse_iso(s: str) -> float:
    t = _utc_epoch(s)
    if t is None:
        raise ValueError(f"not an ISO-8601 UTC instant: {s!r}")
    return t


def _legacy_safe_mission_id(mission_id: str) -> str:
    """Pre-2026-09 lossy name: `A#M1` and `A_M1` collided on `A_M1.json`."""
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in mission_id)[:128]


def _safe_mission_id(mission_id: str) -> str:
    """Injective file stem for a mission_id (L5_factory_v2_rest_3-018).

    An id that is already file-safe and short keeps its own name. Any id that
    normalisation or truncation would alter gets a sha256 suffix of the FULL
    original id, so two distinct ids never share a lease file; the loader also
    checks the stored mission_id (see `_read_nolock`).
    """
    norm = _legacy_safe_mission_id(mission_id)
    if norm == mission_id:
        return norm
    digest = hashlib.sha256(mission_id.encode("utf-8")).hexdigest()[:16]
    return f"{norm[:100]}-{digest}"
