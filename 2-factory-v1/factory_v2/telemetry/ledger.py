"""The telemetry ledger: append-only JSONL, idempotent, retention-bounded.

Deliberately the same shape as `resource_accounting`'s attempt ledger — one file,
one writer per project, fsync on append, torn tails dropped on read. No service,
no database, no framework. A measurement system that needs its own daemon to
stay up is a second thing that can fail while the factory is running.

Idempotency is by `observation_key`, which is derived from the provider session
id when there is one. That is what makes a RESUMED session safe: replaying the
same session appends a row that folds onto the previous one instead of adding a
second launch's worth of tokens to the totals.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path

LEDGER_NAME = "telemetry_observations.jsonl"

#: Rows retained per project ledger. Older rows are rolled into a `.1` file
#: rather than deleted, so a long run cannot grow the live file without bound
#: and history is still reconstructible.
DEFAULT_MAX_ROWS = 200_000


def ledger_path(project_dir) -> Path:
    return Path(project_dir) / "evidence" / LEDGER_NAME


def append(record, project_dir) -> Path:
    """Durably append one observation. Single writer, flock-serialized.

    The lock is held for the append only. It exists so a dispatcher and a
    settlement running in different processes cannot interleave half-lines into
    the same file — the same discipline runrecords already uses.
    """
    path = ledger_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = record.as_dict() if hasattr(record, "as_dict") else dict(record)
    line = json.dumps(payload, sort_keys=True, default=str)
    lock = path.parent / (path.name + ".lock")
    with open(lock, "a+", encoding="utf-8") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            # Repair only an unterminated tail left by a crashed writer.  If we
            # append to it, the next durable observation becomes part of the
            # same invalid JSON line and is lost on reconstruction.
            if path.is_file() and path.stat().st_size:
                with path.open("rb+") as repair:
                    repair.seek(-1, os.SEEK_END)
                    if repair.read(1) != b"\n":
                        pos = repair.tell() - 1
                        while pos > 0:
                            pos -= 1
                            repair.seek(pos)
                            if repair.read(1) == b"\n":
                                pos += 1
                                break
                        repair.truncate(pos if pos > 0 else 0)
                        repair.flush()
                        os.fsync(repair.fileno())
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)
    return path


def read_rows(project_dir_or_path, *, include_rolled: bool = True) -> list:
    """Every row, INCLUDING rolled generations. Torn tail lines dropped.

    `include_rolled` defaults True because every consumer — the report, the
    receipt, the verifier — reconstructs totals from this. Reading only the live
    file meant a compaction silently removed history from all three at once,
    and the verifier's "reconstructs exactly" check stayed green against a
    ledger that had just lost everything before the roll.

    Oldest generation first, so ordering still reflects append order.
    """
    p = Path(project_dir_or_path)
    if p.is_dir():
        p = ledger_path(p)
    out = []
    if include_rolled:
        for gen in (".2", ".1"):
            rolled = p.with_suffix(p.suffix + gen)
            if rolled.is_file():
                out.extend(_read_one(rolled))
    if not p.is_file():
        return out
    out.extend(_read_one(p))
    return out


def _read_one(p: Path) -> list:
    out = []
    with p.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def fold(rows: list) -> list:
    """One record per observation_key, first-seen order, later rows refining.

    Refinement rule: a later row overwrites a field only when it carries an
    actual measurement. A re-observation that lost its log (log rotated away,
    process died before the totals) therefore cannot downgrade a number that was
    measured earlier — which is exactly what a replayed or resumed session does.
    """
    order: list = []
    by_key: dict = {}
    for r in rows:
        key = r.get("observation_key") or f"__unkeyed__{len(order)}"
        if key not in by_key:
            by_key[key] = dict(r)
            order.append(key)
            continue
        cur = by_key[key]
        for k, v in r.items():
            if k == "usage":
                cur[k] = _better_usage(cur.get(k), v)
            elif k in ("raw_usage_events",):
                cur[k] = _merge_events(cur.get(k) or [], v or [])
            elif _is_present(v):
                cur[k] = v
    return [by_key[k] for k in order]


def _is_present(value) -> bool:
    if value in ("", None, {}, []):
        return False
    if isinstance(value, dict) and value.get("value") == "UNKNOWN":
        return False
    return True


def _rank(usage) -> int:
    return {"COMPLETE": 2, "PARTIAL_FLOOR": 1}.get(
        (usage or {}).get("completeness"), 0)


def _better_usage(current, incoming):
    """Keep the more complete block and refine equal ranks field by field."""
    if not isinstance(incoming, dict):
        return current
    if not isinstance(current, dict):
        return incoming
    if _rank(incoming) < _rank(current):
        return current
    if _rank(incoming) > _rank(current):
        base, refinement = dict(incoming), current
    else:
        base, refinement = dict(current), incoming
    for key, value in refinement.items():
        if _is_present(value):
            base[key] = value
    return base


def _merge_events(current: list, incoming: list) -> list:
    """Union of raw events, de-duplicated on their own identity.

    Without this, a resumed session's replayed events are appended a second time
    and any later re-aggregation from raw doubles the same tokens.
    """
    seen = set()
    out = []
    for ev in list(current) + list(incoming):
        if not isinstance(ev, dict):
            continue
        ident = (ev.get("message_id") or "", ev.get("request_id") or "",
                 ev.get("event") or "", ev.get("timestamp") or "")
        if ident in seen and any(ident):
            continue
        seen.add(ident)
        out.append(ev)
    return out


def compact(project_dir, *, max_rows: int = DEFAULT_MAX_ROWS) -> dict:
    """Roll the ledger when it passes `max_rows`. Retention, not deletion.

    The overflow is moved to `<name>.1` (previous overflow is kept as `.2`, and
    nothing beyond that), so the live file stays bounded during a long run while
    the rolled files remain on disk for any recomputation that needs them.
    """
    path = ledger_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.parent / (path.name + ".lock")
    # The WHOLE read→decide→rewrite must hold the same lock `append` takes.
    # Without it: compact reads N rows, a dispatcher appends row N+1 and fsyncs,
    # compact then os.replace()s its N-row snapshot over the file and row N+1 is
    # gone with no error — an append-only ledger that silently loses appends is
    # worse than no ledger, because the verifier reconstructs from it.
    with open(lock, "a+", encoding="utf-8") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            return _compact_locked(path, max_rows)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _compact_locked(path: Path, max_rows: int) -> dict:
    # LIVE file only. Including rolled generations here would re-roll rows that
    # are already rolled, duplicating them into `.1` on every compaction.
    rows = read_rows(path, include_rolled=False)
    if len(rows) <= max_rows:
        return {"rolled": 0, "retained": len(rows)}
    keep = rows[-max_rows:]
    overflow = rows[:-max_rows]
    one = path.with_suffix(path.suffix + ".1")
    two = path.with_suffix(path.suffix + ".2")
    if one.exists():
        os.replace(one, two)
    with one.open("w", encoding="utf-8") as fh:
        for r in overflow:
            fh.write(json.dumps(r, sort_keys=True, default=str) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for r in keep:
            fh.write(json.dumps(r, sort_keys=True, default=str) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return {"rolled": len(overflow), "retained": len(keep),
            "rolled_to": str(one)}
