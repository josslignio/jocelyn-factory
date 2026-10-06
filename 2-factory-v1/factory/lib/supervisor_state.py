#!/usr/bin/env python3
"""F001 — persistent supervisor state: factory/run/supervisor_state.json.

Atomic writes only (tmp + os.replace) so a crash mid-write never leaves a
corrupted/partial file for the next factoryd start or factoryd_watch read to trip on.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from factory_v2._io import _atomic_write, _now


utc_now = _now


@dataclass
class SupervisorState:
    pid: int
    started_at: str
    last_heartbeat: str
    poll_interval_s: int = 30
    active_workers: list = field(default_factory=list)
    completed_this_session: list = field(default_factory=list)
    failed_this_session: list = field(default_factory=list)
    total_polls: int = 0
    stall_count: int = 0
    stale_active_count: int = 0
    lost_mission_count: int = 0
    duplicate_dispatch_count: int = 0
    idle_poll_count: int = 0        # polls where ALL work was blocked on human input
    true_human_gate_count: int = 0  # actual human interventions required (payment, creds, product decision)
    context_receipt_missing_count: int = 0
    blocked_dependency_count: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SupervisorState":
        return cls(
            pid=data["pid"],
            started_at=data["started_at"],
            last_heartbeat=data["last_heartbeat"],
            poll_interval_s=data.get("poll_interval_s", 30),
            active_workers=data.get("active_workers", []),
            completed_this_session=data.get("completed_this_session", []),
            failed_this_session=data.get("failed_this_session", []),
            total_polls=data.get("total_polls", 0),
            stall_count=data.get("stall_count", 0),
            stale_active_count=data.get("stale_active_count", 0),
            lost_mission_count=data.get("lost_mission_count", 0),
            duplicate_dispatch_count=data.get("duplicate_dispatch_count", 0),
            idle_poll_count=data.get("idle_poll_count", data.get("human_go_count", 0)),
            true_human_gate_count=data.get("true_human_gate_count", 0),
            context_receipt_missing_count=data.get("context_receipt_missing_count", 0),
            blocked_dependency_count=data.get("blocked_dependency_count", 0),
        )


atomic_write_json = _atomic_write   # one durable-write shape (factory_v2/_io.py), a reader never sees a half file


def save_state(path: Path, state: SupervisorState) -> None:
    atomic_write_json(Path(path), state.to_dict())


def load_state(path: Path) -> SupervisorState | None:
    path = Path(path)
    if not path.exists():
        return None
    return SupervisorState.from_dict(json.loads(path.read_text(encoding="utf-8")))
