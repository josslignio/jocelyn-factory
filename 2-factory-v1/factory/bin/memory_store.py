#!/usr/bin/env python3
"""Single canonical entry point for the Factory's lesson memory.

MEASURED DEFECT (2026-08-07, this repo, branch claude-quota-guard-lifted)
------------------------------------------------------------------------
The Factory declared its lesson memory in three mutually inconsistent places and
implemented a reader for none of them:

  * MASTER_ORDER_FACTORY_V1_FINAL.md:34   -> "Mémoire canonique en fichiers :
    memory/lessons.jsonl, memory/defects.jsonl, memory/decisions.jsonl,
    memory/facts-index.jsonl".  Only the first of the four exists on disk.
  * docs/05_MEMORY_IMPROVEMENT.md:6-7     -> "lessons/validated.jsonl" and
    "lessons/candidates.jsonl", promoted by a `factoryctl promote-lesson`
    subcommand that does not exist (factory/bin/factoryctl.py:1081 lists the
    complete `choices=[...]`; no lesson command is in it).
  * the archived RUN8 campaign order (archive/pre-purge-2026-09) -> a third file,
    "memory/lessons.run8-staging.jsonl", which never existed.

The only code that ever touched a lesson file is factory/bin/bootstrap_job_radar.sh:22-23,
and it copies into the *product* repo's `lessons/` directory -- i.e. a path
different from the one that actually holds data.  `memory/lessons.jsonl` (7
entries) has, at the time this module was written, zero reader and zero writer in
code: it was last modified by hand in commit e011da2.

This module is the consolidation: ONE canonical path, with the other declared
paths kept readable so that no lesson can be lost by the consolidation itself.

CANONICAL PATH: factory/memory/lessons.jsonl (relative to the repo root; was
memory/lessons.jsonl until FACTORY_DEAD_WEIGHT_PURGE_V1 moved the memory under the
live factory/ tree -- the old path stays readable as a legacy path).
Chosen because it is (a) the path named canonical by the highest-authority
document, (b) the only path that actually holds entries, and (c) the path the
Factory's own reference lesson tooling uses.

BACK-COMPATIBILITY: read-through, never delete.  The legacy paths below are read
in addition to the canonical file; entries found there are returned by
`load_lessons()` so an old path can never make a lesson disappear.  Writes go to
the canonical path ONLY -- that is what stops the divergence from coming back.

SCHEMA: deliberately minimal.  Two incompatible lesson shapes exist in the wild
("JR-DEFECT-*" records keyed on defect/required_fix, and "L-<ts>-NN" records keyed
on description/trigger_pattern/fix_pattern).  Requiring a JSON object with a
non-empty string `id` is the largest common contract; anything narrower would
silently drop half the corpus, and dropping lessons is worse than the defect.

ROBUSTNESS: a malformed line is rejected and REPORTED (line number, reason, raw
excerpt) but never aborts the load.  Fail-closed on a single bad record is how a
whole memory disappears at once.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import tempfile

# The one true path. Everything else is legacy.
CANONICAL_REL = "factory/memory/lessons.jsonl"

# Declared-but-not-canonical paths, kept readable for back-compatibility.
# Order matters only for reporting: the canonical file always wins on id conflict.
LEGACY_REL = (
    "memory/lessons.jsonl",   # emplacement d'origine, relocalisé sous factory/ le 22/09 (FACTORY_DEAD_WEIGHT_PURGE_V1)
    "lessons/validated.jsonl",
    "lessons/candidates.jsonl",
)

# Never scanned, never read, never written: campaign fixtures (a campaign's own
# `memory/lessons.jsonl` copies are subject data, not Factory memory).
EXCLUDED_PREFIXES = ("campaigns/", ".git/", "factory/cto/")

RAW_EXCERPT_MAX = 200


class DuplicateLessonId(ValueError):
    """Raised when appending a lesson whose id already exists in the memory."""


def canonical_path(root) -> pathlib.Path:
    """Absolute path of the single canonical lesson file under `root`."""
    return pathlib.Path(root) / CANONICAL_REL


def legacy_paths(root, existing_only: bool = True):
    """Legacy lesson paths under `root`. By default only those present on disk."""
    root = pathlib.Path(root)
    paths = [root / rel for rel in LEGACY_REL]
    return [p for p in paths if p.is_file()] if existing_only else paths


def _excerpt(line: str) -> str:
    return line[:RAW_EXCERPT_MAX]


def read_jsonl(path):
    """Read one JSONL lesson file. Returns (records, ignored).

    Never raises on line content. Raises OSError only if the file itself cannot
    be read (absent / directory / IO error) -- a genuine system fault.
    """
    records, ignored = [], []
    seen = set()
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            stripped = line.strip()
            if not stripped:
                continue  # blank lines are formatting, not rejects
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError as exc:
                ignored.append({"path": str(path), "lineno": lineno,
                                "reason": f"invalid JSON: {exc}",
                                "id_hint": None, "raw": _excerpt(stripped)})
                continue
            if not isinstance(obj, dict):
                ignored.append({"path": str(path), "lineno": lineno,
                                "reason": f"not a JSON object: {type(obj).__name__}",
                                "id_hint": None, "raw": _excerpt(stripped)})
                continue
            lid = obj.get("id")
            if not isinstance(lid, str) or not lid.strip():
                ignored.append({"path": str(path), "lineno": lineno,
                                "reason": "missing or empty string field 'id'",
                                "id_hint": None, "raw": _excerpt(stripped)})
                continue
            if lid in seen:
                ignored.append({"path": str(path), "lineno": lineno,
                                "reason": f"duplicate id within file: {lid!r}",
                                "id_hint": lid, "raw": _excerpt(stripped)})
                continue
            seen.add(lid)
            records.append(obj)
    return records, ignored


def load_lessons(root, include_legacy: bool = True):
    """Load the Factory's lessons through the single canonical entry point.

    Returns (lessons, ignored, sources):
      lessons -- deduplicated by id; the canonical file wins over any legacy copy;
      ignored -- per-line rejects, each with path/lineno/reason/id_hint/raw;
      sources -- the files actually read, canonical first.

    No lesson reachable from any known path is ever dropped by this function.
    """
    root = pathlib.Path(root)
    lessons, ignored, sources = [], [], []
    by_id = {}

    files = [canonical_path(root)]
    if include_legacy:
        files.extend(legacy_paths(root))

    for path in files:
        if not path.is_file():
            continue
        recs, ign = read_jsonl(path)
        sources.append(str(path))
        ignored.extend(ign)
        for rec in recs:
            lid = rec["id"]
            if lid in by_id:
                # Canonical (read first) wins; a legacy duplicate is reported, not lost silently.
                ignored.append({"path": str(path), "lineno": None,
                                "reason": f"id {lid!r} already provided by a higher-priority path",
                                "id_hint": lid, "raw": ""})
                continue
            by_id[lid] = rec
            lessons.append(rec)
    return lessons, ignored, sources


def append_lesson(root, lesson):
    """Append one lesson to the CANONICAL path only, atomically, without loss.

    Refuses an id that already exists anywhere reachable by `load_lessons`.

    L6_bin_lib_products_1-010: promote-lesson commands run OUTSIDE the Factory
    lock, so the whole duplicate-check + read + replace sequence is serialized
    by an exclusive lock file — two concurrent promotions used to read the same
    snapshot and each rewrite the whole file, silently dropping a lesson."""
    if not isinstance(lesson, dict):
        raise TypeError("lesson must be a JSON object")
    lid = lesson.get("id")
    if not isinstance(lid, str) or not lid.strip():
        raise ValueError("lesson must carry a non-empty string 'id'")

    target = canonical_path(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    import fcntl
    with open(target.parent / ".lessons.lock", "a+", encoding="utf-8") as lockfh:
        fcntl.flock(lockfh.fileno(), fcntl.LOCK_EX)
        try:
            existing, _, _ = load_lessons(root)
            if any(rec.get("id") == lid for rec in existing):
                raise DuplicateLessonId(lid)

            previous = target.read_text(encoding="utf-8") if target.is_file() else ""
            if previous and not previous.endswith("\n"):
                previous += "\n"

            fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".lessons.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(previous)
                    fh.write(json.dumps(lesson, ensure_ascii=False) + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, target)
            except BaseException:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
        finally:
            fcntl.flock(lockfh.fileno(), fcntl.LOCK_UN)
    return target


def populated_lesson_paths(root):
    """Every non-empty lesson file under `root`, excluding campaign fixtures.

    The consolidation invariant: this must contain the canonical path and nothing
    else. Used by the gate in tests/test_factory_memory_consolidation.py.
    """
    root = pathlib.Path(root).resolve()
    found = []
    for path in sorted(root.rglob("*.jsonl")):
        rel = path.relative_to(root).as_posix()
        if any(rel.startswith(pfx) for pfx in EXCLUDED_PREFIXES):
            continue
        name = path.name
        parent = path.parent.name
        is_lesson_file = "lesson" in name.lower() or parent == "lessons"
        if not is_lesson_file:
            continue
        try:
            if any(line.strip() for line in path.read_text(encoding="utf-8").splitlines()):
                found.append(rel)
        except OSError:
            continue
    return found


def main(argv=None):
    p = argparse.ArgumentParser(description="Canonical Factory lesson memory.")
    p.add_argument("command", choices=["paths", "load", "append", "scan"])
    p.add_argument("--root", default=".")
    p.add_argument("--lesson", help="JSON object, for `append`")
    p.add_argument("--no-legacy", action="store_true")
    a = p.parse_args(argv)

    if a.command == "paths":
        print(json.dumps({"canonical": str(canonical_path(a.root)),
                          "legacy_existing": [str(x) for x in legacy_paths(a.root)],
                          "legacy_declared": [str(x) for x in legacy_paths(a.root, existing_only=False)]},
                         indent=2))
        return 0
    if a.command == "scan":
        print(json.dumps({"populated_lesson_paths": populated_lesson_paths(a.root)}, indent=2))
        return 0
    if a.command == "load":
        try:
            lessons, ignored, sources = load_lessons(a.root, include_legacy=not a.no_legacy)
        except OSError as exc:
            print(f"MEMORY_SYSTEM_FAIL: {exc}", file=sys.stderr)
            return 1
        for ign in ignored:
            print(f"ignored {ign['path']}:{ign['lineno']}: {ign['reason']}", file=sys.stderr)
        print(json.dumps({"count": len(lessons), "ignored_count": len(ignored),
                          "sources": sources, "lessons": lessons},
                         ensure_ascii=False, indent=2))
        return 0
    if a.command == "append":
        if not a.lesson:
            print("append requires --lesson", file=sys.stderr)
            return 2
        try:
            target = append_lesson(a.root, json.loads(a.lesson))
        except DuplicateLessonId as exc:
            print(f"duplicate lesson id: {exc}", file=sys.stderr)
            return 1
        print(str(target))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
