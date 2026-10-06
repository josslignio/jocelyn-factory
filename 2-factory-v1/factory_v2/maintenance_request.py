"""Durable typed maintenance requests — the alternative to spinning.

The live Factory state is the argument for this file. `projects/factory` sits at a
terminal repair chain (MAX_ITERATIONS, 11 iterations) whose episode is stamped with
an old control SHA. Every cycle since has re-entered the same branch, failed to
produce a VerifiedFixReceipt, emitted `REPAIR_EPISODE_REENTRY_REFUSED`, and stopped
— seven identical events in a row in the tail of PROJECT_STATE.json. The refusal is
CORRECT: an episode re-opens on proof, never on a changed SHA. What was missing is
that the refusal produced nothing an owner or a later phase could act on. The exact
reason lived only in an event line that the next identical event buried.

A maintenance request is that missing output: one durable JSON record, keyed by the
blocked position, naming the precise missing proof and what would resolve it. It is
IDEMPOTENT — re-raising the same request for the same position updates a counter and
a last-seen timestamp, never a second record and never a second escalation. It grants
nothing: it does not clear a quarantine, does not open a repair budget, does not
touch canonical authority, and is not itself evidence that anything was fixed.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from factory_v2._io import _now

SCHEMA = "maintenance-request/v1"
REQUEST_DIR = "maintenance_requests"

#: Typed kinds. A new kind is a deliberate addition, not a free-text string.
KIND_AUTHORITY_MIGRATION_UNPROVEN = "AUTHORITY_MIGRATION_UNPROVEN"
KIND_REPAIR_REENTRY_UNPROVABLE = "REPAIR_REENTRY_UNPROVABLE"
#: FACTORY_RELOAD_CANARY_V1 — a red reload canary blocks RELOAD_NOW: the owner
#: must fix HEAD before the daemon can restart onto it.
KIND_RELOAD_CANARY_FAILED = "RELOAD_CANARY_FAILED"

_KINDS = frozenset({KIND_AUTHORITY_MIGRATION_UNPROVEN, KIND_REPAIR_REENTRY_UNPROVABLE,
                    KIND_RELOAD_CANARY_FAILED})


def request_id(*, kind: str, milestone_id: str, fingerprint: str,
               old_code_sha: str, new_code_sha: str = "") -> str:
    """Identity of a blocked POSITION: (kind, milestone, fingerprint, old sha).

    2026-09-13: `new_code_sha` (the control HEAD at observation time) used to be
    part of the identity, so every control commit minted a fresh "new" request
    for the same unchanged block — 9 open records for one TR_WEEKLY_OPERATE_V1
    position on the owner console. The current SHA is observation detail
    (kept on the record as `new_code_sha`/`seen_at_code_shas`), not identity.
    """
    del new_code_sha
    payload = json.dumps({"kind": kind, "milestone_id": milestone_id,
                          "fingerprint": fingerprint, "old_code_sha": old_code_sha},
                         sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _fingerprint_digest(fingerprint) -> str:
    return hashlib.sha256(str(fingerprint).encode("utf-8")).hexdigest()


def request_path(project_dir: Path, rid: str) -> Path:
    return Path(project_dir) / REQUEST_DIR / f"{rid}.json"


def raise_request(project_dir: Path, *, kind: str, milestone_id: str,
                  fingerprint: str, old_code_sha: str, new_code_sha: str,
                  missing_proof: str, reproduction: str = "",
                  resolves_when: str = "") -> tuple[dict, bool]:
    """Write (or touch) the request for this exact blocked position.

    Returns (record, is_new). `is_new` is False on every repeat, which is what a
    caller uses to stay quiet instead of re-announcing a block it already raised.
    """
    from .project_authority import atomic_write_json

    if kind not in _KINDS:
        raise ValueError(f"unknown maintenance request kind {kind!r}; known: {sorted(_KINDS)}")
    rid = request_id(kind=kind, milestone_id=milestone_id, fingerprint=fingerprint,
                     old_code_sha=old_code_sha, new_code_sha=new_code_sha)
    path = request_path(project_dir, rid)
    now = _now()
    existing = None
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except Exception:                               # noqa: BLE001
            existing = None
    record = {
        "schema": SCHEMA, "request_id": rid, "kind": kind,
        "project": Path(project_dir).name, "milestone_id": milestone_id,
        "failure_fingerprint": str(fingerprint)[:400],       # display only
        # L5_factory_v2_rest_1-025: identity of the FULL fingerprint — the
        # 400-char display field made two distinct incidents "duplicates".
        "failure_fingerprint_sha256": _fingerprint_digest(fingerprint),
        "old_code_sha": old_code_sha, "new_code_sha": new_code_sha,
        "missing_proof": str(missing_proof)[:600],
        "reproduction": str(reproduction)[:600],
        "resolves_when": str(resolves_when)[:600],
        "state": "OPEN",
        "first_seen": (existing or {}).get("first_seen", now),
        "last_seen": now,
        "observed_count": int((existing or {}).get("observed_count", 0)) + 1,
        "seen_at_code_shas": sorted(set((existing or {}).get("seen_at_code_shas", [])
                                        + ([new_code_sha] if new_code_sha else []))),
        # Stated so the record cannot be mistaken for authorisation later.
        "grants": [],
        "note": ("This request grants nothing. It does not clear a quarantine, open "
                 "a repair budget or alter canonical authority."),
    }
    if existing is not None and existing.get("state") != "OPEN":
        record["state"] = existing["state"]      # a closed position stays closed
    atomic_write_json(path, record)
    _supersede_legacy_duplicates(project_dir, record)
    return record, existing is None


def _supersede_legacy_duplicates(project_dir: Path, record: dict) -> None:
    """Close OPEN records minted under the pre-2026-09-13 identity (same blocked
    position, different new_code_sha). One position, one open request."""
    from .project_authority import atomic_write_json
    d = Path(project_dir) / REQUEST_DIR
    if not d.is_dir():
        return
    same = ("kind", "milestone_id", "old_code_sha")
    for p in d.glob("*.json"):
        if p.name == f"{record['request_id']}.json":
            continue
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except Exception:                               # noqa: BLE001
            continue
        if not isinstance(r, dict):
            continue
        # L5_factory_v2_rest_1-025: same position = same FULL fingerprint. A
        # record carrying the digest is compared on it; a legacy record (no
        # digest) is comparable only when its stored fingerprint was never
        # truncated (< 400 chars) — otherwise the identity is ambiguous and
        # the record is left OPEN rather than closed as a false duplicate.
        if r.get("failure_fingerprint_sha256"):
            same_fp = r["failure_fingerprint_sha256"] == record["failure_fingerprint_sha256"]
        else:
            legacy = str(r.get("failure_fingerprint") or "")
            same_fp = len(legacy) < 400 and _fingerprint_digest(legacy) == record["failure_fingerprint_sha256"]
        if r.get("state") == "OPEN" and same_fp and all(r.get(k) == record.get(k) for k in same):
            r["state"] = "SUPERSEDED"
            r["superseded_by"] = record["request_id"]
            atomic_write_json(p, r)


def open_requests(project_dir: Path) -> list:
    d = Path(project_dir) / REQUEST_DIR
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.json")):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except Exception:                               # noqa: BLE001
            continue
        if r.get("state") == "OPEN":
            out.append(r)
    return out
