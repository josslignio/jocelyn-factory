"""Bridge: production dispatch path <-> multi-AI audit cell (A1/A6).

CTO DIRECTIVE 033 §3 R2/R3.  The cell was previously reachable only from
`audit_cell_exercise.run_audit_cell_exercise()` — a harness.  A harness cannot
satisfy A1 ("a REAL failure automatically triggers the cell, no manual trigger
anywhere") or A6 ("the writer is redirected automatically and the product resumes
at the EXACT prior position"), because neither is a property of the harness: both
are properties of `runtime.advance()`.

The hard constraint is that `advance()` runs entirely inside
`project_authority.project_transaction()`, an `fcntl.flock` spanning
read -> decide -> mutate -> save.  A model call inside that window would hold the
project lock for minutes, blocking every other supervisor and turning a slow
reviewer into a project-wide outage.

So the loop is split across two `advance()` turns:

    turn N   (under lock)  detect failure -> persist AuditCellRequest + exact position
             (lock RELEASED)
             (no lock)     execute challenger/reviewer  <- long model calls live HERE
             (no lock)     persist AuditCellResult
    turn N+1 (under lock)  apply synthesis -> redirect writer -> resume exact position

Idempotency: one request per (milestone_id, fingerprint) -> `request_id`.  A crash
at any point replays without duplicating, because both records are content-keyed and
written atomically, and application is guarded by `applied_at`.

A cell never settles a product milestone.  It only records a synthesis and a
redirection; settlement stays with the normal gate path.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from ._io import _atomic_write, _now

SCHEMA_REQUEST = "audit-cell-request/v1"
SCHEMA_RESULT = "audit-cell-result/v1"


def cell_dir(project_dir: Path) -> Path:
    d = Path(project_dir) / "evidence" / "audit_cell"
    d.mkdir(parents=True, exist_ok=True)
    return d


def request_id(milestone_id: str, fingerprint: str) -> str:
    """Content key. One cell activation per (milestone, failure class)."""
    raw = f"{milestone_id}\x00{fingerprint}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


# ── turn N, under lock ───────────────────────────────────────────────────────

def persist_audit_cell_request(project_dir: Path, *, milestone_id: str,
                               fingerprint: str, position: dict,
                               trigger_kind: str = "REAL_FAILURE") -> dict:
    """Record that the cell must run. Called UNDER the project lock; returns fast.

    Returns the request record.  Idempotent: an existing unconsumed request for the
    same (milestone, fingerprint) is returned unchanged rather than duplicated.
    """
    rid = request_id(milestone_id, fingerprint)
    path = cell_dir(project_dir) / f"request_{rid}.json"
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    rec = {
        "schema": SCHEMA_REQUEST,
        "request_id": rid,
        "milestone_id": milestone_id,
        "failure_fingerprint": fingerprint[:300],
        "trigger_kind": trigger_kind,
        "position": position,          # exact resume point, captured under the lock
        "requested_at": _now(),
        "requested_by_pid": os.getpid(),
    }
    _atomic_write(path, rec)
    return rec


def pending_request(project_dir: Path) -> dict | None:
    """The oldest request with no matching result. None when there is nothing to do."""
    d = cell_dir(project_dir)
    for req in sorted(d.glob("request_*.json")):
        rid = req.stem.split("request_", 1)[1]
        if not (d / f"result_{rid}.json").is_file():
            try:
                return json.loads(req.read_text(encoding="utf-8"))
            except Exception:
                continue
    return None


# ── between turns, NO lock held ──────────────────────────────────────────────

def persist_audit_cell_result(project_dir: Path, *, request: dict,
                              synthesis: dict, rounds: int,
                              role_identities: list) -> dict:
    """Record the cell outcome. Called with NO lock held, after the model calls."""
    rid = request["request_id"]
    rec = {
        "schema": SCHEMA_RESULT,
        "request_id": rid,
        "milestone_id": request["milestone_id"],
        # The exact position was captured under the lock when the request was made.
        # Carry it onto the result so the apply step never has to re-derive it — and
        # so a result is self-contained evidence of what it redirects to.
        "position": request.get("position") or {},
        "synthesis": synthesis,               # decision + redirection target
        "rounds": rounds,
        "role_identities": role_identities,   # observed pid/argv/birth per launched role
        "completed_at": _now(),
        "applied_at": None,                   # set by turn N+1, under the lock
    }
    _atomic_write(cell_dir(project_dir) / f"result_{rid}.json", rec)
    return rec


def unapplied_result(project_dir: Path) -> dict | None:
    d = cell_dir(project_dir)
    for res in sorted(d.glob("result_*.json")):
        try:
            rec = json.loads(res.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not rec.get("applied_at"):
            return rec
    return None


# ── turn N+1, under lock ─────────────────────────────────────────────────────

def mark_result_applied(project_dir: Path, request_id_: str) -> None:
    """Guard against double application across a crash/replay."""
    path = cell_dir(project_dir) / f"result_{request_id_}.json"
    rec = json.loads(path.read_text(encoding="utf-8"))
    if rec.get("applied_at"):
        return
    rec["applied_at"] = _now()
    _atomic_write(path, rec)
