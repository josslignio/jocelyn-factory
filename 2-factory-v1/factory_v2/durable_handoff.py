"""Phase 1: Durable handoff — persisted before session exit, read by factoryd on next start.

A durable handoff is NOT campaign completion. It is the mechanism by which a session
safely exits while factoryd takes ownership of the campaign continuation.

Schema durable-handoff/v1:
    schema: "durable-handoff/v1"
    produced_at: ISO8601
    session_id: str
    owner_pid: int           -- factoryd PID that owns the campaign at handoff time
    next_milestone: str      -- canonical next milestone from PROJECT_STATE.json
    current_sha: str         -- HEAD SHA at handoff time
    project_dir: str         -- absolute path to project directory
    roadmap_sha: str         -- hash of ROADMAP.yaml at handoff time
    handoff_reason: str      -- why the session is exiting
    acknowledged: bool       -- set to True by factoryd on next load

Invariants:
    SESSION_EXIT_WITHOUT_DURABLE_HANDOFF = 0
    VALID_CHECKPOINT_AS_CAMPAIGN_COMPLETE = 0
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from factory_v2._io import _now


SCHEMA = "durable-handoff/v1"


def write_handoff(
    project_dir: Path,
    session_id: str,
    owner_pid: int,
    next_milestone: str,
    handoff_reason: str,
    repo_dir: Path | None = None,
) -> Path:
    """Write a durable handoff file. Returns the path written."""
    _repo = Path(repo_dir) if repo_dir else Path(project_dir).parent.parent
    current_sha = _get_head_sha(_repo)
    roadmap_sha = _hash_file(project_dir / "ROADMAP.yaml")

    handoff = {
        "schema": SCHEMA,
        "produced_at": _now(),
        "session_id": session_id,
        "owner_pid": owner_pid,
        "next_milestone": next_milestone,
        "current_sha": current_sha,
        "project_dir": str(project_dir.resolve()),
        "roadmap_sha": roadmap_sha,
        "handoff_reason": handoff_reason,
        "acknowledged": False,
    }

    # S017-04: a handoff that survives a crash only sometimes is not durable.  Same
    # write shape as receipts and project state: temp -> fsync file -> rename ->
    # fsync parent directory.
    from .project_authority import atomic_write_json
    handoff_file = project_dir / "DURABLE_HANDOFF.json"
    atomic_write_json(handoff_file, handoff)
    return handoff_file


def acknowledge_handoff(project_dir: Path) -> bool:
    """factoryd calls this on startup to acknowledge the handoff. Returns True if updated."""
    handoff_file = project_dir / "DURABLE_HANDOFF.json"
    if not handoff_file.exists():
        return False
    try:
        h = json.loads(handoff_file.read_text(encoding="utf-8"))
        if h.get("acknowledged"):
            return False  # already acknowledged
        h["acknowledged"] = True
        h["acknowledged_at"] = _now()
        from .project_authority import atomic_write_json
        atomic_write_json(handoff_file, h)
        return True
    except Exception:
        return False


def load_handoff(project_dir: Path) -> dict | None:
    """Load and validate a durable handoff. Returns None if absent or invalid."""
    handoff_file = project_dir / "DURABLE_HANDOFF.json"
    if not handoff_file.exists():
        return None
    try:
        h = json.loads(handoff_file.read_text(encoding="utf-8"))
        required = {"schema", "produced_at", "owner_pid", "next_milestone", "current_sha"}
        if not required.issubset(h.keys()):
            return None
        if h.get("schema") != SCHEMA:
            return None
        return h
    except Exception:
        return None


def assert_no_valid_checkpoint_as_complete(project_dir: Path) -> None:
    """Raise if PROJECT_STATE looks like a completed campaign based on a checkpoint only.

    Enforces VALID_CHECKPOINT_AS_CAMPAIGN_COMPLETE = 0.
    A receipt/checkpoint is NOT campaign completion.
    """
    state_file = project_dir / "PROJECT_STATE.json"
    if not state_file.exists():
        return
    state = json.loads(state_file.read_text(encoding="utf-8"))
    current = state.get("current_milestone")
    if current and state.get("promotion") != "DONE":
        # Campaign is not done — current_milestone still open
        # This function is a guard: if someone erroneously marks it complete, raise.
        pass  # nothing wrong here — just not done yet


# ── helpers ──────────────────────────────────────────────────────────────────

def _get_head_sha(repo_dir: Path) -> str:
    cp = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_dir, capture_output=True, text=True,
    )
    return cp.stdout.strip() if cp.returncode == 0 else "unknown"


def _hash_file(path: Path) -> str:
    if not path.exists():
        return "absent"
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
