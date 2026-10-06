"""Append-only, hash-chained ledger of validated self-modifications.

Each row records one commit (a challenger SHA) that has been validated by an
independent judge against a benchmark receipt. The chain (`row_hash`) makes
any retroactive edit or reordering detectable: `verify_chain` recomputes
every row_hash from scratch and rejects the file if any row's stored hash
disagrees with the recomputed one, or if `validated` is not exactly `True`.

Row shape (all fields required, `row_hash` last):
    {
      "sha": "<40 hex chars, a commit that is an ancestor of HEAD>",
      "validated": true,
      "validated_by": "<judge identity string>",
      "benchmark_receipt_sha256": "<sha256 of the benchmark receipt content>",
      "note": "<free text>",
      "row_hash": "<sha256(prev_row_hash + json.dumps(row_without_row_hash, sort_keys=True))>"
    }

`prev_row_hash` is the empty string for the first row. This matches exactly
what factory_v2.verify_factory_hygiene.check_self_improvement recomputes.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

_SHA_RE = re.compile(r"[0-9a-f]{40}")
REQUIRED_FIELDS = ("sha", "validated", "validated_by", "benchmark_receipt_sha256", "note")


def _row_hash(prev_row_hash: str, body: dict) -> str:
    canon = json.dumps(body, sort_keys=True)
    return hashlib.sha256((prev_row_hash + canon).encode("utf-8")).hexdigest()


def _read_rows(ledger_path: Path) -> list[dict]:
    if not ledger_path.exists():
        return []
    rows = []
    for line in ledger_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _last_row_hash_raw(fh: "TextIO") -> str:
    """Last row_hash of an already-open ledger stream (caller holds the lock)."""
    last = ""
    for line in fh:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("row_hash"):
            last = str(row["row_hash"])
    return last


def append(
    ledger_path: str | Path,
    *,
    sha: str,
    validated: bool,
    validated_by: str,
    benchmark_receipt_sha256: str,
    note: str = "",
) -> dict:
    """Append one row to the ledger, computing row_hash from the true
    previous row_hash on disk (empty string if the ledger is empty or
    missing). Returns the appended row.

    L5_rest_2-009: previous-hash read and append are ONE serialized step
    (exclusive flock on the ledger itself). Two writers that both saw the
    same last hash used to append two rows each claiming to be its direct
    successor — verify_chain then failed with LEDGER_CHAIN_BROKEN despite
    both appends succeeding."""
    if not _SHA_RE.fullmatch(sha):
        raise ValueError(f"sha must be 40 lowercase hex chars, got: {sha!r}")
    if not isinstance(validated, bool):
        raise ValueError("validated must be a bool")

    path = Path(ledger_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "sha": sha,
        "validated": validated,
        "validated_by": validated_by,
        "benchmark_receipt_sha256": benchmark_receipt_sha256,
        "note": note,
    }
    import fcntl
    with path.open("a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            fh.seek(0)
            prev = _last_row_hash_raw(fh)
            row = dict(body)
            row["row_hash"] = _row_hash(prev, body)
            fh.seek(0, os.SEEK_END)
            fh.write(json.dumps(row, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    return row


def verify_chain(ledger_path: str | Path) -> tuple[bool, str]:
    """Recompute the hash chain from scratch. Returns (ok, reason).
    reason is "" when ok is True, otherwise a short machine-readable code."""
    rows = _read_rows(Path(ledger_path))
    prev = ""
    for i, row in enumerate(rows):
        for field in REQUIRED_FIELDS:
            if field not in row:
                return False, f"ROW_FIELD_MISSING:{i}:{field}"
        if "row_hash" not in row:
            return False, f"ROW_HASH_MISSING:{i}"
        body = {k: v for k, v in row.items() if k != "row_hash"}
        expect = _row_hash(prev, body)
        if row["row_hash"] != expect:
            return False, f"LEDGER_CHAIN_BROKEN:{i}"
        if row.get("validated") is not True:
            return False, f"UNVALIDATED_SELF_MODIFICATION:{i}"
        if not _SHA_RE.fullmatch(str(row.get("sha") or "")):
            return False, f"LEDGER_ROW_SHA_INVALID:{i}"
        prev = row["row_hash"]
    return True, ""
