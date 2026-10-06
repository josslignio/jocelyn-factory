#!/usr/bin/env python3
"""Phase C — état de mission durable, relançable après tout crash.

Écrit atomiquement (tmp + os.replace) à chaque transition.
Le fichier mission.json EST la source de vérité : harnais, cascade, dispatcher_v3
s'y réfèrent tous. Si le fichier existe au démarrage, la mission reprend à l'étape courante.

Usage :
    state = MissionState.load_or_create(
        path=Path("mission.json"),
        mission_id="M42",
        task_file="TASK.md",
        budget_s=3600,
        step_names=["build", "test", "repair", "test2"],
    )
    state.start()

    with state.step("build") as step:
        rc = subprocess.call(...)
        step["rc"] = rc
        if rc != 0:
            raise StepFailed("build failed")
"""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional
from factory_v2._io import _now, _now as _now_iso

SCHEMA = "factory-mission-state/v1"


class StepFailed(RuntimeError):
    pass


@dataclass
class StepRecord:
    name: str
    status: str = "pending"
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    duration_s: Optional[float] = None
    rc: Optional[int] = None
    note: Optional[str] = None
    receipt: Optional[dict] = None
    # CTO_LENS_RELIABILITY §1 lever 2: a writer call that hit error_max_turns
    # hands back a resumable session_id. Persisted here (not just in-memory) so
    # a crash between the failed attempt and its --resume retry still leaves
    # the id on disk for the next process to pick up. None for every step that
    # never needed a resume — old mission_*.json files without this key still
    # load fine via the dataclass default.
    session_id: Optional[str] = None


@dataclass
class MissionState:
    mission_id: str
    task_file: str
    budget_s: int
    step_names: list
    steps: list = field(default_factory=list)
    current_step: int = 0
    status: str = "pending"
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    outcome: Optional[str] = None

    @classmethod
    def load_or_create(
        cls,
        path: Path,
        mission_id: str,
        task_file: str,
        budget_s: int,
        step_names: list[str],
    ) -> "MissionState":
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            obj = cls(
                mission_id=data["mission_id"],
                task_file=data["task_file"],
                budget_s=data["budget_s"],
                step_names=data["step_names"],
                steps=[StepRecord(**s) for s in data["steps"]],
                current_step=data["current_step"],
                status=data["status"],
                started_at=data.get("started_at"),
                ended_at=data.get("ended_at"),
                outcome=data.get("outcome"),
            )
            print(
                f"[mission {mission_id}] reprise depuis {path} "
                f"(étape {data['current_step']}/{len(step_names)}, "
                f"status={data['status']})",
                flush=True,
            )
            return obj
        steps = [StepRecord(name=n) for n in step_names]
        return cls(
            mission_id=mission_id,
            task_file=task_file,
            budget_s=budget_s,
            step_names=step_names,
            steps=steps,
        )

    def save(self, path: Path) -> None:
        tmp = path.with_suffix(".tmp")
        data = {
            "schema": SCHEMA,
            "mission_id": self.mission_id,
            "task_file": self.task_file,
            "budget_s": self.budget_s,
            "budget_remaining_s": self.budget_remaining_s(),
            "step_names": self.step_names,
            "current_step": self.current_step,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "outcome": self.outcome,
            "steps": [asdict(s) for s in self.steps],
        }
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, path)

    def start(self) -> None:
        if self.status in ("done", "failed"):
            return
        self.status = "running"
        if self.started_at is None:
            self.started_at = _now_iso()

    def budget_remaining_s(self) -> float:
        spent = sum(s.duration_s or 0.0 for s in self.steps)
        return max(0.0, self.budget_s - spent)

    def is_terminal(self) -> bool:
        """Whether this state belongs to a completed attempt, not a crash resume."""
        return (
            self.status in ("done", "failed")
            or bool(self.steps)
            and all(step.status in ("done", "skipped", "failed") for step in self.steps)
        )

    def is_step_done(self, name: str) -> bool:
        for s in self.steps:
            if s.name == name:
                return s.status in ("done", "skipped")
        return False

    @contextmanager
    def step(self, name: str, path: Optional[Path] = None):
        rec = next((s for s in self.steps if s.name == name), None)
        if rec is None:
            rec = StepRecord(name=name)
            self.steps.append(rec)

        if rec.status in ("done", "skipped"):
            yield rec
            return

        # L6_bin_lib_products_1-012: a retry of a failed/interrupted step must
        # ADD its duration to the time already burnt, never replace it —
        # budget_remaining_s sums duration_s, so replacing refunded the past.
        already_spent = float(rec.duration_s or 0.0)
        rec.status = "running"
        rec.started_at = _now_iso()
        t0 = time.monotonic()
        if path:
            self.save(path)

        try:
            yield rec
            rec.status = "done"
        except StepFailed as exc:
            rec.status = "failed"
            rec.note = str(exc)
            raise
        except Exception:
            rec.status = "failed"
            raise
        finally:
            rec.ended_at = _now_iso()
            rec.duration_s = round(already_spent + (time.monotonic() - t0), 2)
            self.current_step = max(self.current_step, self.steps.index(rec) + 1)
            if path:
                self.save(path)

    def finish(self, outcome: str, path: Optional[Path] = None) -> None:
        self.status = "done"
        self.ended_at = _now_iso()
        self.outcome = outcome
        if path:
            self.save(path)
