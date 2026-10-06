"""Append-only daemon run-window evidence."""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path


def _max_workers() -> int:
    try:
        return max(1, int(os.environ.get("FACTORY_MAX_WORKERS", "1")))
    except (TypeError, ValueError):
        return 1


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _append(run_dir: Path, row: dict) -> None:
    path = run_dir / "run_windows.jsonl"
    run_dir.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def open_run(run_dir, pid, max_workers: int | None = None) -> str:
    """Record an OPEN daemon window and return its crash-unique id."""
    run_id = uuid.uuid4().hex
    _append(Path(run_dir), {"run_id": run_id, "pid": int(pid),
                            "started_at": _now_iso(), "ended_at": None,
                            "max_workers": (_max_workers() if max_workers is None
                                            else max(1, int(max_workers)))})
    return run_id


def close_run(run_dir, run_id) -> None:
    """Append the terminal view of an existing run window, if it is observable."""
    directory = Path(run_dir)
    prior = {}
    path = directory / "run_windows.jsonl"
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("run_id") == run_id:
                prior = row
    _append(directory, {"run_id": run_id, "pid": prior.get("pid", os.getpid()),
                        "started_at": prior.get("started_at", _now_iso()),
                        "ended_at": _now_iso(),
                        "max_workers": prior.get("max_workers", _max_workers())})
