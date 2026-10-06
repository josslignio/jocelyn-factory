#!/usr/bin/env python3
"""F001 — CompletionDetector: poll evidence dirs for finished receipts, detect dead
workers (autopilot PID gone but no receipt written)."""
from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass
from pathlib import Path
from factory_v2._io import _parse_utc

HEARTBEAT_STALE_S = 300  # 5 min


@dataclass
class CompletionResult:
    done: bool
    verdict: str | None = None
    candidate_sha: str | None = None
    elapsed_s: float | None = None


def check_mission_completion(mission_id: str, evidence_dir: str | Path) -> CompletionResult:
    receipt_path = Path(evidence_dir) / f"receipt_{mission_id}.json"
    if receipt_path.exists():
        # L6_bin_lib_products_1-027: a truncated/corrupt receipt (crash
        # mid-write) raised JSONDecodeError straight into factoryd's worker
        # loop — one bad receipt stopped the polling of every other mission.
        # A receipt that cannot be read is NOT a completion: report not-done
        # with a typed marker so the poll loop moves on and the mission's own
        # retry/recovery path decides.
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            return CompletionResult(
                done=False,
                verdict=f"RECEIPT_UNREADABLE:{type(exc).__name__}",
                candidate_sha=None,
                elapsed_s=None,
            )
        if not isinstance(receipt, dict):
            return CompletionResult(
                done=False,
                verdict="RECEIPT_UNREADABLE:not-an-object",
                candidate_sha=None,
                elapsed_s=None,
            )
        return CompletionResult(
            done=True,
            verdict=receipt.get("verdict"),
            candidate_sha=receipt.get("candidate_sha"),
            elapsed_s=receipt.get("elapsed_step_s"),
        )
    return CompletionResult(done=False)


def is_worker_dead(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OverflowError:
        return True
    except PermissionError:
        return False
    # os.kill(pid, 0) succeeded — process entry exists. But it may be a zombie:
    # zombies still appear alive to os.kill() because they retain a process table
    # entry until the parent reaps them. Check process state via ps(1).
    import subprocess
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="],
            capture_output=True, text=True, timeout=2,
        )
        stat = result.stdout.strip()
        # 'Z' (macOS/Linux) = zombie; treat as dead so factoryd can settle the mission
        return stat.startswith("Z")
    except Exception:
        return False


def parse_ts(ts: str) -> dt.datetime:
    t = _parse_utc(ts)
    if t is None:
        raise ValueError(f"not an ISO-8601 instant: {ts!r}")
    return t


def is_heartbeat_stale(heartbeat_path: str | Path, max_age_s: int = HEARTBEAT_STALE_S,
                        now: dt.datetime | None = None) -> bool:
    """A missing heartbeat file counts as stale (fail toward 'investigate', not 'trust
    silently'). now= is injectable for deterministic tests."""
    path = Path(heartbeat_path)
    if not path.exists():
        return True
    data = json.loads(path.read_text(encoding="utf-8"))
    ts = parse_ts(data["ts"])
    now = now or dt.datetime.now(dt.timezone.utc)
    return (now - ts).total_seconds() > max_age_s


def is_worker_stalled(pid: int, heartbeat_path: str | Path,
                       max_age_s: int = HEARTBEAT_STALE_S,
                       now: dt.datetime | None = None) -> bool:
    """Per TASK §E: dead only if heartbeat is stale AND the PID is not alive — a slow but
    alive worker with a stale heartbeat writer hiccup must not be killed."""
    return is_heartbeat_stale(heartbeat_path, max_age_s, now) and is_worker_dead(pid)
