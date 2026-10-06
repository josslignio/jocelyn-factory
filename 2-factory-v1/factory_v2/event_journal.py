"""Append-only event journal (STA-04 / STA-13 first step).

Measured problem: PROJECT_STATE.json (338 KB, 73% of which is the unbounded
`events` list) was rewritten in full >=2x per tick on a stuck project
(~180 MB wasted over 266 ticks). `state["events"]` grows forever and every
save() pays an O(n) rewrite of the whole history to persist one new event.

Fix: events live in an append-only `evidence/events.jsonl` (one JSON object
per line). PROJECT_STATE.json keeps only a bounded tail (last
EVENTS_TAIL_MAX entries) plus a running `events_total` counter and the
journal path, so old readers that inspect `state["events"]` for *recent*
activity keep working unchanged. Callers that need full history must switch
to `iter_events()`.

This module is intentionally standalone (no import of runtime.py, which is
off-limits for this change): `project_authority.save_project_state()` calls
`sync_events_to_journal()` right before it writes PROJECT_STATE.json, using
a private bookkeeping key already carried on `state` to know which events
were already flushed to the journal (see `sync_events_to_journal` docstring).

The feature is OFF by default (see `journal_enabled()`) because turning it
on truncates `state["events"]` for every consumer that still reads that key
directly (there are ~20 across factory_v2/, factory/bin/factoryd.py and
tests/). Flip it on with FACTORY_EVENTS_JOURNAL=1 once those consumers have
been migrated to `iter_events()` (see NEEDS_MERGE note for factoryd.py in
the LOT-22 report).
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

EVENTS_TAIL_MAX = 200
EVIDENCE_DIRNAME = "evidence"
EVENTS_JOURNAL_FILENAME = "events.jsonl"

# Bound on how far back we look for duplicate keys on a normal (steady-state)
# append. Migration (first-ever flush of a whole history) instead dedupes
# against the *entire* existing journal once -- see _append_events_batch.
_DEDUP_WINDOW = 500

# Private key stashed on `state` (not part of the public schema) recording
# how many of `state["events"]" had already been written to the journal as
# of the last save. Anything beyond this index is "new" and must be
# appended. This survives json round-trips because it is a plain int.
_TAIL_MARKER_KEY = "_events_journal_tail_len"


def journal_enabled() -> bool:
    """Feature flag. Defaults OFF -- see module docstring."""
    return os.environ.get("FACTORY_EVENTS_JOURNAL", "") == "1"


def journal_path(project_dir) -> Path:
    return Path(project_dir) / EVIDENCE_DIRNAME / EVENTS_JOURNAL_FILENAME


def _event_key(event: dict) -> tuple:
    """Idempotence key: (ts, kind, mission, content-hash).

    ts+kind+mission alone can collide (e.g. two DISPATCH events at the same
    second for the same mission carrying different detail), so a short hash
    of the full normalized event is folded in as a tiebreaker.
    """
    ts = str(event.get("ts", ""))
    kind = str(event.get("event") or event.get("type") or "")
    mission = str(event.get("mission") or event.get("milestone") or "")
    blob = json.dumps(event, sort_keys=True, default=str, ensure_ascii=False)
    content_hash = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
    return (ts, kind, mission, content_hash)


def _read_all_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8") as f:
        return [ln for ln in (line.strip() for line in f) if ln]


def _read_tail_lines(path: Path, limit: int) -> list[str]:
    """Read at most ``limit`` non-empty lines from the end of a journal."""
    if not path.is_file() or limit <= 0:
        return []
    data = b""
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        pos = fh.tell()
        while pos > 0 and data.count(b"\n") <= limit:
            size = min(8192, pos)
            pos -= size
            fh.seek(pos)
            data = fh.read(size) + data
    return [line.decode("utf-8", errors="replace").strip()
            for line in data.splitlines()[-limit:] if line.strip()]


def _keys_from_lines(lines: list[str]) -> set:
    keys = set()
    for ln in lines:
        try:
            ev = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict):
            keys.add(_event_key(ev))
    return keys


def _append_events_batch(project_dir, events: list[dict], *, full_dedup: bool = False) -> int:
    """Append `events` (in order) to the journal, skipping duplicates.

    full_dedup=True reads the whole existing journal once to build the
    dedup set -- only used for the one-time migration flush, where a prior
    crashed attempt may have already written some/all of the same history
    (see sync_events_to_journal). Steady-state appends use a bounded tail
    window instead, so a normal tick never pays an O(n) read.
    """
    if not events:
        return 0
    path = journal_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    if full_dedup:
        existing_keys = _keys_from_lines(_read_all_lines(path))
    else:
        existing_keys = _keys_from_lines(_read_tail_lines(path, _DEDUP_WINDOW))

    written = 0
    fd = os.open(str(path), os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        for event in events:
            if not isinstance(event, dict):
                continue
            key = _event_key(event)
            if key in existing_keys:
                continue
            existing_keys.add(key)
            line = json.dumps(event, sort_keys=True, default=str, ensure_ascii=False) + "\n"
            os.write(fd, line.encode("utf-8"))
            written += 1
        os.fsync(fd)
    finally:
        os.close(fd)
    return written


def append_event(project_dir, event: dict) -> bool:
    """Append a single event. Returns True if newly written, False if it was
    a duplicate of something already at the tail of the journal."""
    return _append_events_batch(project_dir, [event]) == 1


def iter_events(project_dir, *, since_ts=None, kinds=None, limit=None) -> list:
    """Read events oldest-first.

    Reads the append-only journal when it exists. For a project whose state
    has never been migrated (no events.jsonl yet -- e.g. the feature flag is
    off, or this project predates the journal), falls back to whatever
    events queue is still inline in PROJECT_STATE.json, so callers get
    identical results whether or not the journal is active.
    """
    path = journal_path(project_dir)
    if path.is_file():
        result = []
        for line in _read_all_lines(path):
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(ev, dict):
                result.append(ev)
    else:
        result = []
    try:
        state = json.loads((Path(project_dir) / "PROJECT_STATE.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {}
    # The journal flag may be disabled after migration.  Merge newer inline
    # events instead of letting the mere presence of an old journal hide them.
    seen = {_event_key(e) for e in result}
    for event in (state.get("events") or []):
        if isinstance(event, dict) and _event_key(event) not in seen:
            result.append(event)
            seen.add(_event_key(event))

    if since_ts:
        result = [e for e in result if str(e.get("ts", "")) >= str(since_ts)]
    if kinds:
        kset = set(kinds)
        result = [e for e in result if (e.get("event") or e.get("type")) in kset]
    if limit:
        result = result[-limit:]
    return result


def sync_events_to_journal(project_dir, state: dict) -> dict:
    """Flush any events new since the last save into the journal and
    truncate `state["events"]` to the bounded tail.

    No-op (returns state unchanged) unless journal_enabled() is True, so
    default behavior is exactly what it was before this module existed.

    Called by project_authority.save_project_state() right before it writes
    PROJECT_STATE.json. `state["events"]` is append-only in memory (runtime's
    `_event()` only ever appends), so anything beyond the previous
    `_events_journal_tail_len` marker is new since the last save -- that is
    the whole diff, computed in O(new events) rather than O(total history).

    First call for a given project (no events.jsonl yet): treated as the
    one-time migration -- the *entire* existing `state["events"]" list is
    flushed (full_dedup=True, so a prior crashed migration attempt can't
    double-write), then the tail is kept in state.
    """
    if not journal_enabled():
        return state

    events = [e for e in (state.get("events") or []) if isinstance(e, dict)]
    path = journal_path(project_dir)
    is_first_flush = not path.is_file()

    if is_first_flush:
        _append_events_batch(project_dir, events, full_dedup=True)
        new_count = len(events)
    else:
        tail_marker = int(state.get(_TAIL_MARKER_KEY) or 0)
        new_events = events[tail_marker:]
        _append_events_batch(project_dir, new_events, full_dedup=False)
        new_count = len(new_events)

    prior_total = int(state.get("events_total") or 0) if not is_first_flush else 0
    state["events_total"] = prior_total + new_count
    state["events_journal"] = str(path)
    tail = events[-EVENTS_TAIL_MAX:]
    state["events"] = tail
    state[_TAIL_MARKER_KEY] = len(tail)
    return state
