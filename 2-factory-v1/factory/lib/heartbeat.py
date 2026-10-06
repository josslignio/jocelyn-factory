#!/usr/bin/env python3
"""F001 — worker heartbeat: written by the autopilot worker every 60s, read by the
supervisor to tell a slow-but-alive worker from a stalled one."""
from __future__ import annotations

import json
import os
from pathlib import Path

from supervisor_state import atomic_write_json, utc_now

HEARTBEAT_FILENAME = "worker_heartbeat.json"


def heartbeat_path(evidence_dir: str | Path) -> Path:
    return Path(evidence_dir) / HEARTBEAT_FILENAME


def write_heartbeat(evidence_dir: str | Path, pid: int, step: str, status: str) -> Path:
    path = heartbeat_path(evidence_dir)
    atomic_write_json(path, {"pid": pid, "ts": utc_now(), "step": step, "status": status})
    return path


def read_heartbeat(evidence_dir: str | Path) -> dict | None:
    path = heartbeat_path(evidence_dir)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
