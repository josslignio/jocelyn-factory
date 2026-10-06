"""Phase 8: Real fault injection for controlled long run.

Injects real faults against real processes and real git state:
    - SIGKILL: kill a real PID (raises if not permitted or PID gone)
    - CAS conflict: write a stale SHA to a git ref to force next CAS to fail
    - Engine failure: kill engine subprocess by PID
    - Heartbeat interrupt: remove lease heartbeat file to simulate timeout
    - Golden ref corruption: write a wrong SHA to a git golden ref

All injectors record what they did so verify_controlled_long_run can confirm
the system detected and recovered from each fault.

Safety:
    - Never inject into a PID that is not a child of our own process group
      (unless caller provides require_in_pgrp=False explicitly).
    - Fault injection is TEST-ONLY code — not imported by production paths.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from ._io import _now



@dataclass
class FaultRecord:
    """Record of an injected fault, for post-hoc verification."""
    fault_type: str
    target: str
    injected_at: str = ""
    outcome: str = ""  # INJECTED | REFUSED | ERROR
    detail: str = ""


# ── SIGKILL ────────────────────────────────────────────────────────────────────

def inject_sigkill(pid: int, require_in_pgrp: bool = True) -> FaultRecord:
    """Send SIGKILL to a real process.

    Args:
        pid: target PID.
        require_in_pgrp: if True, the PID must belong to THIS process group —
            the module's perimeter guard. A live PID outside the perimeter
            (provided by mistake) is REFUSED, never killed (L5_rest_1-018: the
            parameter used to be accepted and ignored).
    """
    injected_at = _now()
    try:
        os.kill(pid, 0)  # raises OSError if no such process
    except OSError:
        return FaultRecord(
            fault_type="SIGKILL", target=str(pid), injected_at=injected_at,
            outcome="REFUSED", detail=f"PID {pid} does not exist",
        )
    if require_in_pgrp:
        try:
            target_pgrp = os.getpgid(pid)
        except OSError as exc:
            return FaultRecord(
                fault_type="SIGKILL", target=str(pid), injected_at=injected_at,
                outcome="REFUSED",
                detail=f"PID {pid} group could not be established: {exc}",
            )
        if target_pgrp != os.getpgrp():
            return FaultRecord(
                fault_type="SIGKILL", target=str(pid), injected_at=injected_at,
                outcome="REFUSED",
                detail=(f"PID {pid} is outside this process group "
                        f"(pgid={target_pgrp}, own={os.getpgrp()}) — "
                        "require_in_pgrp refuses a perimeter-foreign PID"),
            )
    try:
        os.kill(pid, signal.SIGKILL)
        return FaultRecord(
            fault_type="SIGKILL", target=str(pid), injected_at=injected_at,
            outcome="INJECTED", detail=f"SIGKILL sent to PID {pid}",
        )
    except OSError as exc:
        return FaultRecord(
            fault_type="SIGKILL", target=str(pid), injected_at=injected_at,
            outcome="ERROR", detail=str(exc),
        )


def wait_pid_dead(pid: int, timeout_s: float = 5.0) -> bool:
    """Return True when PID is gone, False if still alive after timeout."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
            time.sleep(0.05)
        except OSError:
            return True
    return False


# ── CAS conflict ───────────────────────────────────────────────────────────────

def inject_cas_conflict(
    repo_dir: Path,
    ref: str,
    stale_sha: str,
) -> FaultRecord:
    """Force a CAS conflict by writing a stale SHA to a git ref.

    After this injection, any golden_promotion.git_cas_update_ref() that expects
    the current SHA will receive a CAS_CONFLICT.

    Args:
        repo_dir: path to the git repository.
        ref: git ref to corrupt (e.g. "refs/golden/factory").
        stale_sha: a valid-looking but wrong SHA to write.
    """
    injected_at = _now()
    try:
        result = subprocess.run(
            ["git", "update-ref", ref, stale_sha],
            cwd=repo_dir, capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return FaultRecord(
                fault_type="CAS_CONFLICT", target=f"{ref}@{repo_dir}",
                injected_at=injected_at, outcome="INJECTED",
                detail=f"ref={ref} set to stale_sha={stale_sha[:12]}",
            )
        else:
            return FaultRecord(
                fault_type="CAS_CONFLICT", target=f"{ref}@{repo_dir}",
                injected_at=injected_at, outcome="ERROR",
                detail=result.stderr.strip(),
            )
    except Exception as exc:
        return FaultRecord(
            fault_type="CAS_CONFLICT", target=str(ref),
            injected_at=injected_at, outcome="ERROR", detail=str(exc),
        )


# ── lease heartbeat interrupt ──────────────────────────────────────────────────

def inject_heartbeat_interrupt(mission_id: str, project_dir: Path) -> FaultRecord:
    """Remove the lease heartbeat to simulate a timeout.

    The lease file is removed — next require_current_lease() call will see
    the lease as expired/missing.
    """
    injected_at = _now()
    # L5_factory_v2_rest_1-028: the lease lives under WorkerLease's normalised
    # name (`M#1` -> `M_1-<hash>.json`), never under the raw mission_id.
    from .worker_lease import WorkerLease
    lease_path = WorkerLease._lease_path(mission_id, Path(project_dir))
    if not lease_path.exists():
        return FaultRecord(
            fault_type="HEARTBEAT_INTERRUPT", target=mission_id,
            injected_at=injected_at, outcome="REFUSED",
            detail=f"Lease file not found: {lease_path}",
        )
    try:
        lease_path.unlink()
        return FaultRecord(
            fault_type="HEARTBEAT_INTERRUPT", target=mission_id,
            injected_at=injected_at, outcome="INJECTED",
            detail=f"Removed {lease_path}",
        )
    except Exception as exc:
        return FaultRecord(
            fault_type="HEARTBEAT_INTERRUPT", target=mission_id,
            injected_at=injected_at, outcome="ERROR", detail=str(exc),
        )


# ── golden ref corruption ──────────────────────────────────────────────────────

def inject_golden_ref_corruption(
    repo_dir: Path,
    ref: str = "refs/golden/factory",
    corruption_sha: str = "",
) -> FaultRecord:
    """Set the golden ref to a wrong SHA to simulate stale-ref anomaly.

    Args:
        corruption_sha: a real git object SHA to use (must exist in the repo).
            If empty, falls back to setting the ref to HEAD (pointing to the
            wrong expected SHA for CAS purposes).
    """
    if not corruption_sha:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo_dir, capture_output=True, text=True, timeout=5,
            )
            corruption_sha = result.stdout.strip() if result.returncode == 0 else ""
        except Exception:
            pass
    return inject_cas_conflict(repo_dir, ref, corruption_sha)


# ── helpers ────────────────────────────────────────────────────────────────────

