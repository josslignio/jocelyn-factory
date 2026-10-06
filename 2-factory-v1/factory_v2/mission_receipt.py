"""D1-R3: Durable MissionExecutionReceipt — persisted before MISSION_PASS.

Written atomically to <project_dir>/evidence/receipts/<mission_id>/<attempt_id>.json
before the runtime records MISSION_PASS.  Validation loads from disk — no RAM
result object required after factoryd restart.

receipt_sha = sha256[:24] of (mission_id:attempt_id:candidate_sha:integration_sha:
                               actual_engine_id:contract_id:command_exit_code)
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from factory_v2._io import _now


@dataclass
class MissionExecutionReceipt:
    mission_id: str
    attempt_id: str
    candidate_sha: str
    integration_sha: str
    actual_engine_id: str
    contract_id: str
    command_exit_code: int
    # R2.3: compatibility lineage.  contract_id changes on any authority edit; the
    # signature changes only when the acceptance semantics change, so a receipt stays
    # valid across a refreeze that did not alter what "complete" means.
    contract_signature: str = ""
    receipt_sha: str = ""
    created_at: str = ""

    def compute_receipt_sha(self) -> str:
        payload = ":".join([
            self.mission_id,
            self.attempt_id,
            self.candidate_sha,
            self.integration_sha,
            self.actual_engine_id,
            self.contract_id,
            str(self.command_exit_code),
            self.contract_signature,
        ])
        return hashlib.sha256(payload.encode()).hexdigest()[:24]

    def verify_receipt_sha(self) -> bool:
        return bool(self.receipt_sha) and self.compute_receipt_sha() == self.receipt_sha


class ReceiptWriteError(RuntimeError):
    """Raised when receipt write, fsync, or read-back verification fails."""


def _fsync_dir(directory: Path) -> None:
    """fsync a directory so a rename into it survives power loss.

    Without this, os.replace() can be reordered after a crash and the receipt
    filename may not exist even though its bytes were fsynced.
    """
    fd = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_mission_receipt(project_dir: Path, receipt: MissionExecutionReceipt) -> Path:
    """Persist receipt with fsync + read-back SHA verify.

    Raises ReceiptWriteError if write, sync, or verification fails.
    Callers must treat this as a hard failure — do NOT catch and swallow.
    """
    if not receipt.receipt_sha:
        receipt.receipt_sha = receipt.compute_receipt_sha()
    if not receipt.created_at:
        receipt.created_at = _now()
    rdir = Path(project_dir) / "evidence" / "receipts" / receipt.mission_id
    rdir.mkdir(parents=True, exist_ok=True)
    path = rdir / f"{receipt.attempt_id}.json"
    tmp = path.with_suffix(".json.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(asdict(receipt), f, indent=2)
            f.flush()
            os.fsync(f.fileno())  # F4: mandatory fsync before rename
        os.replace(tmp, path)
        _fsync_dir(rdir)  # F4: rename itself must be durable, not just the bytes
    except OSError as exc:
        raise ReceiptWriteError(f"receipt write failed for {receipt.mission_id}: {exc}") from exc
    # Read-back hash verification.
    loaded = load_mission_receipt(project_dir, receipt.mission_id, receipt.attempt_id)
    if loaded is None or not loaded.verify_receipt_sha():
        raise ReceiptWriteError(
            f"receipt SHA verification failed after write for "
            f"{receipt.mission_id}/{receipt.attempt_id} — "
            f"written={receipt.receipt_sha!r} "
            f"reloaded={getattr(loaded, 'receipt_sha', None)!r}"
        )
    return path


def load_mission_receipt(
    project_dir: Path, mission_id: str, attempt_id: str
) -> "MissionExecutionReceipt | None":
    """Load a receipt from disk; returns None if not found or corrupted."""
    path = Path(project_dir) / "evidence" / "receipts" / mission_id / f"{attempt_id}.json"
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            # L4_resilience_review-005: a valid-JSON non-object ([] / null /
            # scalar) is a corrupted receipt -> None, so the caller raises its
            # typed ReceiptVerificationError instead of an AttributeError.
            return None
        return MissionExecutionReceipt(**{k: v for k, v in d.items()
                                          if k in MissionExecutionReceipt.__dataclass_fields__})
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return None



class ReceiptVerificationError(RuntimeError):
    """Raised when the receipt for a mission/attempt is absent, tampered, or bound
    to a different mission, attempt, or validation contract."""


def load_verified_mission_receipt(
    project_dir: Path, mission_id: str, attempt_id: str, contract_id: str = "",
    contract_signature: str = "",
) -> MissionExecutionReceipt:
    """F4: load the receipt for EXACTLY this mission/attempt and verify it.

    Deliberately not "latest": a stale receipt from attempt a1 must never be
    accepted as proof for attempt a2.  Every rejection path raises — there is no
    PROJECT_STATE SHA fallback and no fail-open branch.
    """
    if not mission_id:
        raise ReceiptVerificationError("MISSION_RECEIPT_NO_MISSION: empty mission_id")
    if not attempt_id:
        raise ReceiptVerificationError(
            f"MISSION_RECEIPT_NO_ATTEMPT_ID: {mission_id} has no last_attempt_id in "
            "PROJECT_STATE — cannot identify which receipt proves this attempt"
        )
    receipt = load_mission_receipt(project_dir, mission_id, attempt_id)
    if receipt is None:
        raise ReceiptVerificationError(
            f"MISSION_RECEIPT_MISSING: no receipt at evidence/receipts/{mission_id}/"
            f"{attempt_id}.json"
        )
    if not receipt.verify_receipt_sha():
        raise ReceiptVerificationError(
            f"MISSION_RECEIPT_TAMPERED: {mission_id}/{attempt_id} receipt_sha="
            f"{receipt.receipt_sha!r} does not match recomputed "
            f"{receipt.compute_receipt_sha()!r}"
        )
    if receipt.mission_id != mission_id or receipt.attempt_id != attempt_id:
        raise ReceiptVerificationError(
            f"MISSION_RECEIPT_WRONG_ATTEMPT: file says "
            f"{receipt.mission_id}/{receipt.attempt_id}, expected {mission_id}/{attempt_id}"
        )
    if contract_id and receipt.contract_id != contract_id:
        # R2.3: an exact-ID mismatch alone must not brick the project.  contract_id
        # folds in authority hashes, so a one-line STRATEGY.md edit — or the REPLAN
        # recovery stage itself — mints a new id while the acceptance semantics are
        # untouched.  Accept the receipt when the compatibility signature proves the
        # validation semantics are the same; reject when they actually changed.
        compatible = bool(contract_signature) and receipt.contract_signature == contract_signature
        if not compatible:
            raise ReceiptVerificationError(
                f"MISSION_RECEIPT_CONTRACT_MISMATCH: receipt={receipt.contract_id!r} "
                f"expected={contract_id!r} and signatures differ "
                f"(receipt={receipt.contract_signature!r} expected={contract_signature!r}) "
                "— acceptance semantics changed, the attempt must be re-proven"
            )
    if not receipt.actual_engine_id:
        raise ReceiptVerificationError(
            f"MISSION_RECEIPT_NO_ENGINE_IDENTITY: {mission_id}/{attempt_id} carries no "
            "observed engine identity"
        )
    return receipt
