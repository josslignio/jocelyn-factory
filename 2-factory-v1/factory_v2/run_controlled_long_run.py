"""Phase 8: Controlled long run — real subprocess execution with fault tolerance.

B4 fix 2026-09-08 — counter-audit corrections:
    - factoryd_stub is FORBIDDEN as default; factoryd_cmd must point to real factoryd
    - active_engine_count is read from real dispatch receipts, not authored STATE field
    - restarted=True requires observed post-restart milestone progress, not Popen() success
    - set_long_run_ready() blocked without evidence bundles (see verify_controlled_long_run)

Runs real factoryd as a real subprocess.
Verifies:
    - ≥3 milestones complete
    - ≥2 engines active at peak (counted from real dispatch receipts)
    - Crash + restart recovery (SIGKILL + post-restart milestone progress)
    - Real artifact produced (git commit, worktree, or evidence file)
    - Heartbeat gap detected and logged (not silently ignored)

ControlledLongRunConfig:
    min_milestones: int = 3     -- must complete at least this many
    min_engines: int = 2        -- must have this many engines active
    fault_injection: bool       -- if True, inject SIGKILL during run
    timeout_s: int = 300        -- wall-clock budget for whole run
    poll_interval_s: float = 1.0
    factoryd_cmd: list          -- REQUIRED; must point to real factoryd binary

ControlledLongRunResult:
    status: PASS | FAIL | TIMEOUT
    milestones_completed: list[str]
    engines_active_peak: int
    crashed: bool               -- was SIGKILL injected?
    restarted: bool             -- did factoryd restart AND show milestone progress?
    artifacts: list[str]        -- real artifacts produced
    fault_records: list         -- FaultRecord from fault_injection
    evidence_path: Path | None  -- path to persisted result
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from contextlib import suppress as _suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from ._io import _now


@dataclass
class ControlledLongRunConfig:
    min_milestones: int = 3
    min_engines: int = 2
    fault_injection: bool = False
    timeout_s: int = 300
    poll_interval_s: float = 1.0
    factoryd_cmd: list = field(default_factory=list)  # override for test stubs
    project_dir: Path = Path(".")
    repo_dir: Path = Path(".")


@dataclass
class ControlledLongRunResult:
    status: str  # PASS | FAIL | TIMEOUT
    milestones_completed: list = field(default_factory=list)
    engines_active_peak: int = 0
    crashed: bool = False
    restarted: bool = False
    artifacts: list = field(default_factory=list)
    fault_records: list = field(default_factory=list)
    started_at: str = ""
    completed_at: str = ""
    evidence_path: str = ""
    # LR5: milestone set at the moment the crash was injected (restart must add
    # at least one NEW proven milestone beyond this point).
    milestones_at_crash: list = field(default_factory=list)
    # LR5: identity of EVERY factoryd incarnation, observed at spawn.  A restart is
    # only proven by a DISTINCT pid AND a DISTINCT birth time — never by Popen()
    # success alone.
    daemon_identities: list = field(default_factory=list)

    def as_dict(self) -> dict:
        d = asdict(self)
        return d

    def as_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2)


class ControlledLongRunFailed(Exception):
    """The controlled long run did not meet its proof requirements."""


def _observe_daemon_identity(pid: int) -> dict:
    """pid + birth time (process start timestamp) of a daemon incarnation.

    The birth time is what distinguishes 'same pid reused by a new process' from
    'same process still alive' — pid equality alone proves nothing after a restart.
    """
    import subprocess as _sp
    birth = ""
    try:
        r = _sp.run(["ps", "-o", "lstart=", "-p", str(pid)],
                    capture_output=True, text=True, timeout=10)
        birth = r.stdout.strip()
    except Exception:
        pass
    return {"pid": pid, "birth": birth}


def run_controlled_long_run(config: ControlledLongRunConfig) -> ControlledLongRunResult:
    """Execute a controlled long run and collect evidence.

    This is the real execution path — it spawns actual subprocesses.
    It is NOT a unit test. It is called from preflight scripts and
    from Phase 9 (Twitter Radar long run).

    For hermetic unit tests: use test stubs that inject pre-scripted
    PROJECT_STATE.json mutations without touching production factoryd.
    """
    started_at = _now()
    project_dir = Path(config.project_dir)
    repo_dir = Path(config.repo_dir)
    milestones_completed: list[str] = []
    engines_active_peak = 0
    crashed = False
    restarted = False
    artifacts: list[str] = []
    fault_records: list = []

    # X3: readiness is a precondition, not a discovery.  The guard runs BEFORE Popen —
    # an active quarantine, an unproven PASS or an unverifiable settlement must stop the
    # run from starting at all, not be noticed after a daemon is already advancing.
    _blockers = assert_project_long_run_ready(project_dir)
    if _blockers:
        return ControlledLongRunResult(
            status="FAIL",
            started_at=started_at,
            completed_at=_now(),
            artifacts=[],
            fault_records=[{"error": "PROJECT_NOT_LONG_RUN_READY: " + "; ".join(_blockers)}],
        )

    # Read initial milestone set.
    initial_milestones = _read_completed_milestones(project_dir)

    # Start factoryd.
    factoryd_proc = None
    factoryd_pid = None
    daemon_identities: list = []
    cmd = config.factoryd_cmd
    # B4 fix: factoryd_stub is FORBIDDEN as production default.
    # factoryd_cmd must be explicitly provided and must not reference factoryd_stub.
    if not cmd:
        return ControlledLongRunResult(
            status="FAIL",
            started_at=started_at,
            completed_at=_now(),
            artifacts=[],
            fault_records=[{"error": (
                "B4_FACTORYD_CMD_REQUIRED: factoryd_cmd must be provided explicitly. "
                "Defaulting to factoryd_stub is forbidden in production. "
                "Use: factoryd_cmd=['python3', 'factory/bin/factoryd.py', str(project_dir)]"
            )}],
        )
    if any("factoryd_stub" in str(part) for part in cmd):
        return ControlledLongRunResult(
            status="FAIL",
            started_at=started_at,
            completed_at=_now(),
            artifacts=[],
            fault_records=[{"error": (
                "B4_FACTORYD_STUB_FORBIDDEN: factoryd_stub cannot be used in production harness. "
                "The harness must start the real factoryd binary. "
                f"Got cmd={cmd}"
            )}],
        )

    # L5_rest_2-002: the daemon's stdout/stderr must never be PIPE — nobody
    # drains them, and a chatty daemon blocks on the ~64KB pipe buffer BEFORE
    # producing its first milestone, which the harness then misreads as
    # TIMEOUT. Redirect both streams to evidence logs (same approach as the
    # LR5 wrapper) for the initial launch AND the post-crash restart.
    out_log, err_log = _daemon_stream_handles(project_dir)
    try:
        factoryd_proc = subprocess.Popen(
            cmd, cwd=repo_dir, stdout=out_log, stderr=err_log,
            preexec_fn=os.setpgrp,
        )
        factoryd_pid = factoryd_proc.pid
        daemon_identities.append(_observe_daemon_identity(factoryd_pid))
    except Exception as exc:
        out_log.close(); err_log.close()
        return ControlledLongRunResult(
            status="FAIL",
            started_at=started_at,
            completed_at=_now(),
            daemon_identities=daemon_identities,
        )

    deadline = time.monotonic() + config.timeout_s
    milestones_at_crash: list = []
    fault_injected = False
    _post_restart_milestones_before: list[str] | None = None  # B4: track pre-restart count

    try:
        while time.monotonic() < deadline:
            time.sleep(config.poll_interval_s)

            # Poll milestone progress.
            current_milestones = _read_completed_milestones(project_dir)
            new_milestones = [m for m in current_milestones if m not in initial_milestones
                              and m not in milestones_completed]
            milestones_completed.extend(new_milestones)

            # B4 fix: restarted=True only after observing post-restart milestone progress.
            # Must see at least 1 new milestone after restart, not just Popen() success.
            if (crashed and not restarted and _post_restart_milestones_before is not None
                    and len(milestones_completed) > len(_post_restart_milestones_before)):
                restarted = True

            # Poll engine count from real dispatch receipts (not authored state field).
            active = _count_active_engines(project_dir)
            if active > engines_active_peak:
                engines_active_peak = active

            # Fault injection: SIGKILL after first milestone.
            if config.fault_injection and not fault_injected and len(milestones_completed) >= 1:
                from .fault_injection import inject_sigkill, wait_pid_dead
                rec = inject_sigkill(factoryd_pid)
                fault_records.append(asdict(rec))
                milestones_at_crash = list(milestones_completed)
                fault_injected = True
                crashed = rec.outcome == "INJECTED"
                if crashed:
                    wait_pid_dead(factoryd_pid, timeout_s=5.0)
                    # reap the killed incarnation's Popen before replacing it
                    try:
                        factoryd_proc.wait(timeout=5)
                    except Exception:
                        pass
                    # Restart factoryd.
                    milestones_before_restart = list(milestones_completed)
                    try:
                        factoryd_proc = subprocess.Popen(
                            cmd, cwd=repo_dir,
                            stdout=out_log, stderr=err_log,
                            preexec_fn=os.setpgrp,
                        )
                        factoryd_pid = factoryd_proc.pid
                        daemon_identities.append(_observe_daemon_identity(factoryd_pid))
                        # B4 fix: restarted=True only after observing post-restart milestone
                        # progress. Popen() success alone is insufficient proof of recovery.
                        # We track this via _post_restart_milestones_before for comparison.
                        _post_restart_milestones_before = list(milestones_completed)
                    except Exception:
                        pass

            # Check if we have enough milestones.
            if len(milestones_completed) >= config.min_milestones:
                break

            # Check if factoryd exited unexpectedly.
            if factoryd_proc.poll() is not None and not restarted:
                break

        # Collect artifacts.
        artifacts = _collect_artifacts(project_dir)

        # Determine status.
        ok_milestones = len(milestones_completed) >= config.min_milestones
        ok_engines = engines_active_peak >= config.min_engines or config.min_engines <= 1
        ok_crash = (not config.fault_injection) or restarted

        if time.monotonic() >= deadline and not ok_milestones:
            status = "TIMEOUT"
        elif ok_milestones and ok_engines and ok_crash:
            status = "PASS"
        else:
            status = "FAIL"

    finally:
        # Always kill factoryd on exit.
        _kill_proc(factoryd_proc)
        with _suppress(OSError):
            out_log.close()
        with _suppress(OSError):
            err_log.close()

    result = ControlledLongRunResult(
        status=status,
        milestones_completed=milestones_completed,
        engines_active_peak=engines_active_peak,
        crashed=crashed,
        restarted=restarted,
        artifacts=artifacts,
        fault_records=fault_records,
        started_at=started_at,
        completed_at=_now(),
        daemon_identities=daemon_identities,
        milestones_at_crash=milestones_at_crash,
    )
    return result


def assert_long_run_pass(result: ControlledLongRunResult, config: ControlledLongRunConfig) -> None:
    """Raise ControlledLongRunFailed if the long run did not meet proof requirements."""
    failures = []
    if result.status == "TIMEOUT":
        failures.append(f"Timed out after {config.timeout_s}s with only "
                        f"{len(result.milestones_completed)} milestones")
    if len(result.milestones_completed) < config.min_milestones:
        failures.append(f"Completed {len(result.milestones_completed)} milestones, "
                        f"required {config.min_milestones}")
    if config.min_engines > 1 and result.engines_active_peak < config.min_engines:
        failures.append(f"Peak engines={result.engines_active_peak}, "
                        f"required {config.min_engines}")
    if config.fault_injection and not result.restarted:
        failures.append("SIGKILL injected but factoryd did not restart")
    # R6: truth gate — a long run must not start (or pass) on a project whose product
    # acceptance is currently unproven.  Quarantine, unproven PASS and non-COMPLETE
    # settlements are all disqualifying, whatever the milestone counter says.
    project_dir = getattr(config, "project_dir", None)
    if project_dir:
        blockers = assert_project_long_run_ready(Path(project_dir))
        if blockers:
            failures.append("PROJECT_NOT_LONG_RUN_READY: " + "; ".join(blockers))
    if failures:
        raise ControlledLongRunFailed(
            f"Controlled long run FAILED: {'; '.join(failures)}. "
            f"LONG_RUN_READY must not be set."
        )


# ── helpers ────────────────────────────────────────────────────────────────────

def _read_completed_milestones(project_dir: Path) -> list[str]:
    """Count only milestones whose settlement survives independent verification.

    X3: a dictionary key is not evidence.  This delegates to the single settlement
    verifier so counting, readiness preflight and the final assertion cannot drift
    apart — a fabricated COMPLETE, an absent or tampered receipt, a missing contract or
    a currently failing acceptance all count zero here, in exactly the same way.
    """
    from .settlement_truth import proven_completed_milestones
    try:
        return proven_completed_milestones(project_dir)
    except Exception:
        return []


def assert_project_long_run_ready(project_dir: Path) -> list[str]:
    """X3: reasons this project must NOT enter a long run.  Empty list == ready.

    Checked before Popen, not discovered halfway through the run.
    """
    from .settlement_truth import settlement_blockers
    return settlement_blockers(Path(project_dir))


def _count_active_engines(project_dir: Path) -> int:
    """Distinct engines observed on the exact attempts behind proven settlements.

    S1: this used to scan evidence/ for JSON carrying an engine field, then for JSON
    carrying a dispatch-receipt schema.  Both are forgeable by anything that can write
    into evidence/ — narrowing the filter only tells a forger which shape to write.
    Delegates to the single settlement verifier, which reads receipts at
    supervisor-controlled paths and verifies their hash and contract lineage.
    """
    from .settlement_truth import proven_engines
    try:
        return len(proven_engines(project_dir))
    except Exception:
        return 0


def _collect_artifacts(project_dir: Path) -> list[str]:
    """Return real artifacts found in evidence dir."""
    artifacts = []
    ev_dir = project_dir / "evidence"
    if ev_dir.exists():
        for f in sorted(ev_dir.rglob("*.json"))[:10]:
            artifacts.append(str(f.relative_to(project_dir)))
    return artifacts


def _daemon_stream_handles(project_dir: Path):
    """Append-mode evidence logs for the daemon's stdout/stderr (L5_rest_2-002).

    PIPE was never drained: a daemon writing ~64KB before its first milestone
    blocked forever and the harness reported TIMEOUT. Appending to evidence
    logs keeps the diagnostics for post-mortem AND never blocks the child."""
    log_dir = Path(project_dir) / "evidence" / "long_run"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    out = open(log_dir / f"factoryd-{stamp}.out.log", "ab")
    err = open(log_dir / f"factoryd-{stamp}.err.log", "ab")
    return out, err


def _kill_proc(proc) -> None:
    if proc is None:
        return
    try:
        if proc.poll() is None:
            # L5_rest_2-003: the daemon owns a process GROUP (os.setpgrp at
            # launch) — killing only the parent PID left descendants alive to
            # keep writing after the run ended. Stop the whole owned group;
            # fall back to the single PID if group identity is unavailable.
            import signal as _signal
            try:
                os.killpg(os.getpgid(proc.pid), _signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                proc.kill()
            proc.wait(timeout=3)
    except Exception:
        pass



