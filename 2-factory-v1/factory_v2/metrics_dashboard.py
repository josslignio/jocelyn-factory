"""A small, ledger-only 24 hour operational dashboard for ``factoryd``.

This deliberately does not use the richer telemetry report: its aggregates have
different coverage semantics.  These figures are a direct projection of the
attempt-account and project-state ledgers, so every value can be recomputed.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Iterable
from factory_v2 import event_journal


WINDOW_H = 24
_EVENT_TIME_KEYS = ("ts", "at", "timestamp")
_RESTART_EVENTS = {"RUNTIME_STARTED", "CTO_STARTUP_CHECK", "DAEMON_STARTED"}
_LOOP_EVENTS = {"V2_STALL_DETECTED", "GATE_HELD", "MILESTONE_GATE_HELD"}


def _timestamp(value: object) -> float | None:
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(
            dt.timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def _event_timestamp(event: dict) -> float | None:
    return _timestamp(next((event.get(k) for k in _EVENT_TIME_KEYS if event.get(k)), ""))


def _number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _read_jsonl(path: Path) -> Iterable[dict]:
    if not path.is_file():
        return ()
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _events(path: Path) -> Iterable[dict]:
    try:
        rows = json.loads(path.read_text(encoding="utf-8")).get("events") or []
    except (OSError, json.JSONDecodeError):
        return ()
    return (row for row in rows if isinstance(row, dict))


def build_dashboard(project_dirs: Iterable[str | Path], *, now: dt.datetime | None = None) -> dict:
    """Return exact 24-hour metrics from terminal attempt and state events."""
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    now = now.astimezone(dt.timezone.utc)
    cutoff = now.timestamp() - WINDOW_H * 3600
    projects = [Path(p) for p in project_dirs]
    per_project: dict[str, dict] = {}
    per_engine: dict[str, dict] = {}
    launches = success = refused = settlements = parked = loops = restarts = 0
    durations: list[float] = []

    for project in projects:
        name = project.name
        for event in event_journal.iter_events(project):
            if (_event_timestamp(event) or float("-inf")) < cutoff:
                continue
            kind = event.get("event") or event.get("kind") or ""
            settlements += kind == "MILESTONE_SETTLED"
            parked += kind == "MILESTONE_PARKED"
            loops += kind in _LOOP_EVENTS
            restarts += kind in _RESTART_EVENTS
        for row in _read_jsonl(project / "evidence" / "attempt_accounts.jsonl"):
            if row.get("phase") != "TERMINAL" or (_timestamp(row.get("ended_at")) or float("-inf")) < cutoff:
                continue
            if row.get("consumes_model_quota") is False:
                refused += 1
                continue
            launches += 1
            pp = per_project.setdefault(name, {"launches": 0, "success": 0})
            pp["launches"] += 1
            engine = str(row.get("engine") or "UNKNOWN")
            pe = per_engine.setdefault(engine, {"launches": 0, "success": 0, "output_tokens": 0})
            pe["launches"] += 1
            if _number(row.get("output_tokens")):
                pe["output_tokens"] += int(row["output_tokens"])
            if _number(row.get("duration_s")):
                durations.append(float(row["duration_s"]))
            if row.get("mission_pass"):
                success += 1
                pp["success"] += 1
                pe["success"] += 1

    return {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_h": WINDOW_H,
        "per_project": per_project,
        "per_engine": per_engine,
        "launches": launches,
        "launches_success": success,
        "launches_refused_without_engine": refused,
        "parked_milestones": parked,
        "settlements": settlements,
        "mean_duration_s": round(sum(durations) / len(durations), 3) if durations else None,
        "restarts": restarts,
        "loops_detected": loops,
    }


def to_markdown(metrics: dict) -> str:
    """Render the four operational sections without introducing new numbers."""
    lines = ["# Factory metrics dashboard", "", f"Generated: {metrics['generated_at']} (UTC)",
             f"Window: {metrics['window_h']} h", "", "## Vitesse", "",
             f"- Launches: {metrics['launches']}", f"- Success: {metrics['launches_success']}",
             f"- Refus sans moteur: {metrics['launches_refused_without_engine']}",
             f"- Durée moyenne (s): {metrics['mean_duration_s']}", "", "## Quota", ""]
    if metrics["per_engine"]:
        lines += ["| Moteur | Launches | Success | Output tokens |", "| --- | ---: | ---: | ---: |"]
        lines += [f"| {engine} | {row['launches']} | {row['success']} | {row['output_tokens']} |"
                  for engine, row in sorted(metrics["per_engine"].items())]
    else:
        lines.append("Aucun lancement consommant du quota.")
    lines += ["", "## Reprise", "", f"- Redémarrages: {metrics['restarts']}",
              f"- Jalons parqués: {metrics['parked_milestones']}",
              f"- Settlements: {metrics['settlements']}", "", "## Boucles", "",
              f"- Boucles détectées: {metrics['loops_detected']}", ""]
    return "\n".join(lines)
