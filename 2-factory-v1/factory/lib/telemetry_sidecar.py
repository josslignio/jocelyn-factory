"""Crash-safe telemetry sidecar of a dispatcher receipt + best-effort quota projections for the router.

Policy module of dispatcher_v3 (F4 extraction). Every writer here is an observer: it never raises into the
dispatch it measures, and it never invents a number it did not read from the engine's own output.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import model_router as _mr

try:
    from factory_v2 import quota_observer as _quota_observer
except Exception:  # pragma: no cover
    _quota_observer = None

_CONTROL_ROOT_FALLBACK = Path(__file__).resolve().parents[2]


def now_iso() -> str:
    """Timezone-aware instant with MICROSECONDS: launch identity keys minted inside one second must differ."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def telemetry_sidecar_path(out_path: Path) -> Path:
    """Crash-safe companion of the receipt, one row per launch."""
    out_path = Path(out_path)
    return out_path.with_name(out_path.stem + ".telemetry.jsonl")


def append_telemetry_sidecar(out_path: Path, record: dict) -> None:
    """Persist one launch observation the instant it ends; silent on failure (a missing row shows as coverage)."""
    tel = record.get("telemetry")
    if not isinstance(tel, dict):
        return
    try:
        path = telemetry_sidecar_path(out_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(tel, sort_keys=True, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    except Exception:
        pass


def read_telemetry_sidecar(out_path: Path) -> list:
    """Observations durable on disk, whatever happened to the dispatcher (a torn tail is skipped)."""
    path = telemetry_sidecar_path(out_path)
    if not path.is_file():
        return []
    rows = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return rows


def refresh_cli_quota_snapshot(record: dict) -> None:
    """Project a MEASURED Claude quota ratio for the router; unknown/unitless/non-Claude readings are rejected."""
    if _quota_observer is None:
        return
    tel = record.get("telemetry")
    quota = tel.get("quota") if isinstance(tel, dict) and tel.get("engine") == "claude" else None
    util = quota.get("utilisation") if isinstance(quota, dict) else None
    value = util.get("value") if isinstance(util, dict) and util.get("unit") == "ratio" else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return
    try:
        control_root = getattr(_mr, "_CONTROL_ROOT", _CONTROL_ROOT_FALLBACK)
        override = os.environ.get("FACTORY_QUOTA_SNAPSHOT_ROOT", "").strip()
        if (override and control_root is getattr(_mr, "_CONTROL_ROOT_DEFAULT", control_root)
                and os.environ.get("FACTORY_NO_REAL_ENGINE") == "1"):
            control_root = Path(override)
        _quota_observer.refresh_from_cli(control_root, "claude", utilisation=float(value),
                                         status=str(quota.get("status") or "UNKNOWN"),
                                         scope=str(quota.get("scope") or "UNKNOWN"),
                                         observed_at=str(quota.get("observed_at") or ""))
    except Exception:
        pass


def mark_provider_exhausted(provider: str, *, reason: str) -> None:
    """A QUOTA_CUT on the engine's own stderr is the most reliable reading: persist utilisation=1.0 so the router
    switches BEFORE the next launch. Best-effort."""
    if _quota_observer is None or provider not in getattr(_quota_observer, "PROVIDERS", ()):
        return
    try:
        _quota_observer.refresh_from_cli(getattr(_mr, "_CONTROL_ROOT", _CONTROL_ROOT_FALLBACK), provider,
                                         utilisation=1.0, status="EXHAUSTED", scope="observed_cut",
                                         observed_at=now_iso())
    except Exception:
        pass
