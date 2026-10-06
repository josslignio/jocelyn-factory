"""Stable dispatch checkpoint: one live builder result per (mission, contract).

The dispatch path's expensive step is `runner.execute(mission)` — the builder.
Everything after it (receipt write, accounting close, mission-state recording)
is bookkeeping, but a crash anywhere in that window used to lose the result
outright: the restart re-dispatched the mission and REBUILT work that had
already been produced, spending a second model budget for the same outcome.

This module persists the builder result the moment it exists, keyed by
(mission_id, contract_id):

  * one live checkpoint per frozen contract semantics — a contract refreeze
    changes the contract_id, hence the key, hence never resumes a result built
    under superseded authority;
  * resumable ONLY while the state's recorded attempt_count has not moved,
    i.e. while the checkpointed attempt never reached a durable outcome —
    once MISSION_PASS/MISSION_FAILED (or a typed terminal quarantine) is
    recorded, the checkpoint is consumed and a later dispatch builds fresh;
  * integrity-addressed: the payload carries its own sha; a corrupt or
    tampered checkpoint fails closed to "absent" and is journaled, never
    silently resumed;
  * launch journals stay PER ATTEMPT: every checkpoint lifecycle event
    (WRITE / RESUME / CONSUME / CORRUPT) is appended to
    evidence/dispatch_checkpoints/attempts.jsonl carrying the crash-unique
    attempt_key (mission+contract+attempt+lease generation), and a resumed
    dispatch still opens and closes its own resource-accounting row under
    its own attempt key — the checkpoint never collapses two attempts into
    one.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from factory_v2._io import _now, _now as _utcnow

CHECKPOINT_SCHEMA = "dispatch-checkpoint/v1"

# Fields of executors.ExecutionResult persisted in the checkpoint. Everything
# downstream bookkeeping needs to resume without rebuilding: the observed
# engine execution (F4 engine-identity proof), SHAs, telemetry observations.
_RESULT_FIELDS = (
    "rc", "output_tail", "engine", "candidate_sha", "integration_sha",
    "wall_time_s", "verdict", "receipt", "engine_execution",
    "telemetry_observations", "phase_times_s",
)


class CheckpointWriteError(RuntimeError):
    """Raised when a checkpoint cannot be faithfully written or verified."""


def checkpoint_key(mission_id: str, contract_id: str) -> str:
    """Content key: sha16 of (mission_id, contract_id)."""
    return hashlib.sha256(f"{mission_id}:{contract_id}".encode()).hexdigest()[:16]


def checkpoint_path(project_dir: Path, *, mission_id: str, contract_id: str) -> Path:
    return (Path(project_dir) / "evidence" / "dispatch_checkpoints"
            / f"{checkpoint_key(mission_id, contract_id)}.json")


def journal_path(project_dir: Path) -> Path:
    return Path(project_dir) / "evidence" / "dispatch_checkpoints" / "attempts.jsonl"


def journal_event(project_dir: Path, kind: str, **fields) -> None:
    """Append one per-attempt lifecycle line to the checkpoint journal.

    Best-effort by design: the journal is a launch log, and a broken launch
    log must never break the dispatch it describes. Failures are swallowed —
    the durable checkpoint file and the accounting ledger carry the truth.
    """
    try:
        jpath = journal_path(project_dir)
        jpath.parent.mkdir(parents=True, exist_ok=True)
        entry = {"schema": "dispatch-checkpoint-journal/v1",
                 "kind": kind, "recorded_at": _utcnow(), **fields}
        with open(jpath, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, sort_keys=True) + "\n")
    except Exception:
        pass


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def result_to_payload(result) -> dict:
    """Extract the persistable subset of an ExecutionResult.

    Raises CheckpointWriteError if the result is not faithfully JSON
    serializable — a lossy checkpoint must never masquerade as a full one.
    """
    payload = {name: getattr(result, name, None) for name in _RESULT_FIELDS}
    try:
        _canon(payload)
    except (TypeError, ValueError) as exc:
        raise CheckpointWriteError(
            f"builder result not faithfully serializable: {exc}") from exc
    return payload


def write_dispatch_checkpoint(project_dir: Path, *, mission_id: str,
                              contract_id: str, attempt_key: str,
                              attempt_id: str, attempt_number: int,
                              lease_generation: int,
                              attempt_count_before: int, result) -> Path:
    """Durably persist the builder result for (mission, contract).

    Atomic write + fsync of file and directory + read-back integrity verify.
    Raises CheckpointWriteError on any failure — visible and typed, so the
    runtime can record the loss of crash-resume capability instead of
    discovering it at the next restart.
    """
    project_dir = Path(project_dir)
    body = {
        "schema": CHECKPOINT_SCHEMA,
        "mission_id": mission_id,
        "contract_id": contract_id,
        "checkpoint_key": checkpoint_key(mission_id, contract_id),
        "attempt_key": attempt_key,
        "attempt_id": attempt_id,
        "attempt_number": int(attempt_number),
        "lease_generation": int(lease_generation),
        # Resume guard: only resumable while the state's attempt_count has
        # not moved past this value (no durable outcome was ever recorded).
        "attempt_count_before": int(attempt_count_before),
        "created_at": _utcnow(),
        "result": result_to_payload(result),
    }
    checkpoint = {**body,
                  "checkpoint_sha": hashlib.sha256(
                      _canon(body).encode()).hexdigest()[:24]}
    path = checkpoint_path(project_dir, mission_id=mission_id,
                           contract_id=contract_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(path) + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(checkpoint, indent=1))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        # Read-back verify: a checkpoint that cannot be re-read intact is
        # no checkpoint at all.
        reread = json.loads(path.read_text(encoding="utf-8"))
        if reread.get("checkpoint_sha") != checkpoint["checkpoint_sha"]:
            raise CheckpointWriteError("read-back verification failed")
    except CheckpointWriteError:
        raise
    except Exception as exc:
        raise CheckpointWriteError(
            f"checkpoint write failed for {mission_id}: {exc}") from exc
    journal_event(project_dir, "WRITE", mission=mission_id,
                  attempt_key=attempt_key, attempt_id=attempt_id,
                  lease_generation=int(lease_generation))
    return path


def load_dispatch_checkpoint(project_dir: Path, *, mission_id: str,
                             contract_id: str,
                             expected_attempt_count_before: int) -> "dict | None":
    """Return the resumable checkpoint payload, or None.

    None means "no resume available": absent file, corrupt/tampered payload
    (fails closed and is journaled), or stale — the recorded
    attempt_count_before no longer matches the state's attempt_count, i.e.
    that attempt already reached a durable outcome and must not be replayed.
    """
    path = checkpoint_path(Path(project_dir), mission_id=mission_id,
                           contract_id=contract_id)
    if not path.exists():
        return None
    try:
        checkpoint = json.loads(path.read_text(encoding="utf-8"))
        body = {k: v for k, v in checkpoint.items() if k != "checkpoint_sha"}
        if (checkpoint.get("schema") != CHECKPOINT_SCHEMA
                or checkpoint.get("checkpoint_key")
                != checkpoint_key(mission_id, contract_id)
                or hashlib.sha256(_canon(body).encode()).hexdigest()[:24]
                != checkpoint.get("checkpoint_sha")):
            raise ValueError("integrity verification failed")
    except Exception as exc:
        # Corrupt or tampered: fail closed to absent, but visibly — never a
        # silent resume of an unverifiable result.
        journal_event(project_dir, "CORRUPT", mission=mission_id,
                      contract_id=contract_id, error=str(exc)[:200])
        return None
    if int(checkpoint.get("attempt_count_before", -1)) != \
            int(expected_attempt_count_before):
        return None  # stale: that attempt already reached a durable outcome
    return checkpoint


def consume_dispatch_checkpoint(project_dir: Path, *, mission_id: str,
                                contract_id: str, attempt_key: str) -> bool:
    """Remove the checkpoint once its attempt reached a durable outcome.

    Returns True when a file was removed. Never raises for a missing file —
    consuming an already-consumed checkpoint is a no-op, and the load-side
    attempt_count guard makes any leftover stale anyway.
    """
    path = checkpoint_path(Path(project_dir), mission_id=mission_id,
                           contract_id=contract_id)
    removed = False
    try:
        if path.exists():
            path.unlink()
            removed = True
    except Exception:
        # Visible via the journal; the load-side staleness guard means a
        # leftover checkpoint cannot resume, so this cannot corrupt dispatch.
        journal_event(project_dir, "CONSUME_FAILED", mission=mission_id,
                      attempt_key=attempt_key)
        return False
    if removed:
        journal_event(project_dir, "CONSUME", mission=mission_id,
                      attempt_key=attempt_key)
    return removed


def execution_result_from_checkpoint(mission_id: str, checkpoint: dict):
    """Rebuild an ExecutionResult from a verified checkpoint payload."""
    from .executors import ExecutionResult
    payload = dict(checkpoint.get("result") or {})
    return ExecutionResult(
        mission_id=mission_id,
        rc=int(payload.get("rc", 1)),
        output_tail=str(payload.get("output_tail", "")),
        engine=str(payload.get("engine", "")),
        candidate_sha=str(payload.get("candidate_sha", "")),
        integration_sha=str(payload.get("integration_sha", "")),
        wall_time_s=float(payload.get("wall_time_s") or 0.0),
        verdict=str(payload.get("verdict", "")),
        receipt=dict(payload.get("receipt") or {}),
        engine_execution=dict(payload.get("engine_execution") or {}),
        telemetry_observations=list(payload.get("telemetry_observations") or []),
        phase_times_s=dict(payload.get("phase_times_s") or {}),
    )
