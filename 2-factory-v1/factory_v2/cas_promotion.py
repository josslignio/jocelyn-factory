"""CAS (Compare-And-Swap) promotion protocol — LR1 2026-09-08.

Prevents double-dispatch of the same mission_id and ensures that a milestone
settlement is only recorded if the expected integration_sha matches the one
that was actually validated.

Two orthogonal guarantees:
  1. Mission dedup: once a mission_id is promoted (ACCEPTED or REJECTED), a
     second promotion attempt with the same mission_id is rejected immediately.
     This prevents the case where two concurrent workers dispatch the same
     mission and both write a COMPLETE entry.

  2. SHA binding: when settling a milestone, the integration_sha in the receipt
     must match the integration_sha that was validated. A mismatch means the
     validator ran against a different commit than the one in the receipt —
     the settlement is rejected.

The promotion ledger is a JSON file (<project_dir>/evidence/cas_promotion_ledger.json)
written atomically (tmp → rename). Each entry records the mission_id, the
integration_sha it was settled at, and the timestamp.

Inter-process safety (D004): the read-modify-write cycle is serialized under an
exclusive fcntl.flock() on a sibling lock file. Without it, two concurrent OS
processes can both observe "no entry yet", both record, and both receive
CAS_ALLOWED — a double promotion. Proven by real concurrent-process race tests
(tests/test_lr1_interprocess.py); removing the lock makes them red.
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from factory_v2._io import _file_lock, _now


_LEDGER_FILENAME = "cas_promotion_ledger.json"
_LOCK_FILENAME = "cas_promotion_ledger.lock"


class LedgerCorruptError(RuntimeError):
    """The promotion ledger exists but is unreadable/unparseable — refuse."""


@contextmanager
def _ledger_lock(project_dir: Path):
    """Exclusive inter-process lock over the ledger RMW cycle."""
    with _file_lock(Path(project_dir) / "evidence" / _LOCK_FILENAME):
        yield


def _read_ledger(ledger_path: Path) -> dict:
    """Fresh empty ledger ONLY for a missing file (L5_rest_1-011).

    A ledger that exists but cannot be parsed is a corrupted proof: treating it
    as absent silently resets the unique-promotion protection exactly when its
    evidence became unreadable. The caller refuses instead."""
    try:
        return json.loads(ledger_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"schema": "cas-ledger/v1", "promotions": {}}
    except (OSError, json.JSONDecodeError) as exc:
        raise LedgerCorruptError(
            f"CAS_LEDGER_CORRUPT: {ledger_path} exists but cannot be parsed "
            f"({type(exc).__name__}) — the unique-promotion protection cannot be "
            f"re-established from an unreadable ledger; manual repair required") from exc


def _write_ledger(ledger_path: Path, ledger: dict) -> None:
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = ledger_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(ledger, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    os.replace(tmp, ledger_path)


def check_and_record_promotion(
    mission_id: str,
    integration_sha: str,
    project_dir: Path,
) -> tuple[bool, str]:
    """Attempt to record a CAS promotion for mission_id at integration_sha.

    Returns (allowed, reason).
      allowed=True: first promotion of this mission_id — recorded atomically.
      allowed=False: mission_id already promoted, or SHA mismatch.

    When allowed=True the promotion is written to the ledger before returning.
    The caller must not write a COMPLETE entry if allowed=False.
    """
    ledger_path = Path(project_dir) / "evidence" / _LEDGER_FILENAME
    with _ledger_lock(project_dir):
        try:
            ledger = _read_ledger(ledger_path)
        except LedgerCorruptError as exc:
            # L5_rest_1-011: never let an unreadable ledger pass for an empty
            # one — that re-allowed an already-promoted mission to promote a
            # second time once its proof became unreadable.
            return False, str(exc)
        promotions: dict = ledger.get("promotions", {})

        existing = promotions.get(mission_id)
        if existing is not None:
            existing_sha = existing.get("integration_sha", "")
            if existing_sha == integration_sha:
                return False, (
                    f"CAS_DUPLICATE: mission_id={mission_id!r} already promoted "
                    f"at integration_sha={integration_sha!r}. Double-dispatch rejected."
                )
            return False, (
                f"CAS_SHA_MISMATCH: mission_id={mission_id!r} previously promoted "
                f"at integration_sha={existing_sha!r}, now requested "
                f"integration_sha={integration_sha!r}. Settlement rejected."
            )

        promotions[mission_id] = {
            "integration_sha": integration_sha,
            "promoted_at": _now(),
        }
        ledger["promotions"] = promotions
        _write_ledger(ledger_path, ledger)
    return True, f"CAS_ALLOWED: mission_id={mission_id!r} promoted at {integration_sha!r}"


def is_already_promoted(mission_id: str, project_dir: Path) -> bool:
    """True if mission_id is already recorded in the CAS ledger."""
    ledger_path = Path(project_dir) / "evidence" / _LEDGER_FILENAME
    ledger = _read_ledger(ledger_path)
    return mission_id in ledger.get("promotions", {})


def get_promotion_sha(mission_id: str, project_dir: Path) -> str | None:
    """Return the integration_sha for a previously promoted mission, or None."""
    ledger_path = Path(project_dir) / "evidence" / _LEDGER_FILENAME
    ledger = _read_ledger(ledger_path)
    entry = ledger.get("promotions", {}).get(mission_id)
    return entry.get("integration_sha") if entry else None
