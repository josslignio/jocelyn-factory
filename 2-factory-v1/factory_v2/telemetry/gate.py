"""Durable lifecycle telemetry for every model launch through the common gate.

The launch gate already makes the ACCOUNTING durable (OPEN row fsynced before
execve, terminal row after). But accounting rows answer "what was spent"; they
were never a launch TELEMETRY journal, and two blind spots remained:

  * nothing durable existed at the telemetry layer BEFORE the model call, so
    an interrupted launch (SIGKILL, factoryd crash) left no telemetry-side
    trace of the launch having begun until the accounting reconcile ran;
  * on in-process interruption (KeyboardInterrupt, SystemExit from a SIGTERM
    handler, executor exceptions) no telemetry terminal event was written at
    all — `persist_telemetry` only runs on the close_account path with a
    result in hand.

This module brackets every model-quota launch that passes through the common
launch gate/accounting path (mission dispatch AND recovery work — any
provider: GLM, Claude, Codex or future, since the meter records the observed
engine execution, never a hardcoded provider list):

    with metered_launch(project_dir, attempt_key=..., ...) as m:
        result = call_the_model()
        m.update(outcome="SUCCESS" if result.ok else "FAILURE",
                 rc=result.rc, engine_execution=result.engine_execution)

Guarantees:

  * OPEN event durable (flock + fsync) BEFORE the launch — fail closed: if it
    cannot be made durable, TelemetryPersistenceError raises and nothing is
    launched;
  * TERMINAL event (SUCCESS / FAILURE / INTERRUPTED / ERROR) emitted in
    `finally`, so success, failure, exception and interruption all land;
  * a TERMINAL persistence failure is VISIBLE and TYPED
    (TelemetryPersistenceError) and NEVER masks a primary exception: when the
    bracket body is already raising, the telemetry error is attached to the
    primary as a note and written to stderr, and the primary propagates;
  * hard kills (no finally runs at all) are covered by the durable OPEN event
    plus `emit_interrupted_for_lost`, which reconcile_on_boot calls for every
    attempt it closes — the pre-launch event is what makes the interruption
    provable after the fact.
"""
from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from .record import redact_argv
from factory_v2._io import _now, _now as _utcnow

LAUNCH_EVENT_SCHEMA = "launch-gate-telemetry/v1"

#: Terminal outcomes. INTERRUPTED is interruption-shaped (KeyboardInterrupt,
#: SystemExit/SIGTERM-converted, or a reconciled lost process); ERROR is any
#: other in-flight exception; FAILURE is a completed call whose observed
#: outcome was not ok.
OUTCOMES = frozenset({"SUCCESS", "FAILURE", "INTERRUPTED", "ERROR"})

#: Interruption-shaped exceptions: the process was asked to stop.
_INTERRUPTING = (KeyboardInterrupt, SystemExit)


class TelemetryPersistenceError(RuntimeError):
    """A telemetry durability write failed. Typed, visible, never masking."""


def events_path(project_dir) -> Path:
    return Path(project_dir) / "evidence" / "telemetry" / "launch_gate.jsonl"


def read_events(project_dir) -> list:
    """Every lifecycle event, torn tail lines dropped."""
    path = events_path(project_dir)
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _append_event(project_dir, event: dict) -> None:
    """Durably append one lifecycle event (flock-serialized, fsync).

    Raises TelemetryPersistenceError on any write failure — the caller decides
    whether that fails the launch closed (OPEN) or surfaces without masking
    (TERMINAL).
    """
    path = events_path(project_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = path.parent / (path.name + ".lock")
        with open(lock, "a+", encoding="utf-8") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(event, sort_keys=True, default=str) + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)
    except Exception as exc:
        raise TelemetryPersistenceError(
            f"launch-gate telemetry append failed for "
            f"{event.get('attempt_key', '?')}/{event.get('phase', '?')}: {exc}"
        ) from exc


def emit_launch_open(project_dir, *, attempt_key: str, mission: str,
                     milestone: str = "", engine: str = "", model: str = "",
                     consumes_model_quota: bool = True) -> dict:
    """Durable OPEN telemetry event — BEFORE the model call, fsynced."""
    event = {
        "schema": LAUNCH_EVENT_SCHEMA,
        "phase": "OPEN",
        "recorded_at": _utcnow(),
        "attempt_key": attempt_key,
        "mission": mission,
        "milestone": milestone,
        "engine_requested": engine,
        "model_requested": model,
        "consumes_model_quota": bool(consumes_model_quota),
    }
    _append_event(project_dir, event)
    return event


def emit_launch_terminal(project_dir, *, attempt_key: str, outcome: str,
                         duration_s: "float | None" = None, rc=None,
                         engine_execution: "dict | None" = None,
                         error: str = "", reconciled: bool = False) -> dict:
    """Durable TERMINAL telemetry event — success/failure/interruption/error."""
    if outcome not in OUTCOMES:
        raise ValueError(f"untyped telemetry outcome {outcome!r}")
    ee = engine_execution or {}
    event = {
        "schema": LAUNCH_EVENT_SCHEMA,
        "phase": "TERMINAL",
        "recorded_at": _utcnow(),
        "attempt_key": attempt_key,
        "outcome": outcome,
        "duration_s": (round(float(duration_s), 3)
                       if duration_s is not None else None),
        "rc": rc,
        # OBSERVED engine identity when the caller has it — provider-agnostic:
        # whatever engine actually ran (glm, claude, codex, local) is recorded
        # as observed, never assumed from the routing hint.
        "engine_observed": ee.get("engine_id", ""),
        "model_observed": ee.get("model", ""),
        "pid": ee.get("pid"),
        # Security invariant: prompt operands never enter durable telemetry.
        "argv_head": redact_argv(ee.get("argv") or ee.get("argv0") or [])[:4],
        "observed": bool(ee.get("observed")),
        "error": str(error)[:300],
        "reconciled": bool(reconciled),
    }
    _append_event(project_dir, event)
    return event


@contextmanager
def metered_launch(project_dir, *, attempt_key: str, mission: str,
                   milestone: str = "", engine: str = "", model: str = "",
                   consumes_model_quota: bool = True):
    """Bracket one model launch through the common gate with durable telemetry.

    OPEN is durable BEFORE the launch (fail closed: a telemetry event that
    cannot be made durable refuses the launch rather than launching
    unmeasured). TERMINAL is emitted in `finally` for every path — success,
    failure, exception, interruption.

    Yields a mutable state dict; the body records the OBSERVED outcome on it:
        m.update(outcome="SUCCESS"|"FAILURE", rc=..., engine_execution=...)
    Unset outcome after a clean body means the call never completed its
    bookkeeping — recorded as INTERRUPTED, never guessed as SUCCESS.

    TERMINAL persistence failure: typed (TelemetryPersistenceError) and
    visible. With a primary exception already propagating it NEVER masks it —
    the telemetry error is attached to the primary via `add_note` and written
    to stderr, and the primary propagates unchanged. Without one, it raises.
    """
    emit_launch_open(project_dir, attempt_key=attempt_key, mission=mission,
                     milestone=milestone, engine=engine, model=model,
                     consumes_model_quota=consumes_model_quota)
    t0 = time.monotonic()
    state = {"outcome": "INTERRUPTED", "rc": None,
             "engine_execution": None, "error": ""}
    primary: "BaseException | None" = None
    try:
        yield state
    except BaseException as exc:
        primary = exc
        state["outcome"] = ("INTERRUPTED" if isinstance(exc, _INTERRUPTING)
                            else "ERROR")
        state["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        try:
            emit_launch_terminal(
                project_dir, attempt_key=attempt_key,
                outcome=state["outcome"] if state["outcome"] in OUTCOMES
                else "INTERRUPTED",
                duration_s=time.monotonic() - t0,
                rc=state.get("rc"), engine_execution=state.get("engine_execution"),
                error=state.get("error", ""))
        except TelemetryPersistenceError as tpe:
            if primary is not None:
                # Visible and typed, without masking the primary exception.
                note = f"TELEMETRY_PERSISTENCE_FAILED: {tpe}"
                if hasattr(primary, "add_note"):
                    primary.add_note(note)
                print(f"[telemetry.gate] {note}", file=sys.stderr)
            else:
                raise


def emit_interrupted_for_lost(project_dir, *, attempt_key: str,
                              mission: str = "", milestone: str = "") -> bool:
    """Terminal INTERRUPTED event for a lost attempt (reconcile_on_boot).

    Best-effort and visible: reconcile must never be bricked by a telemetry
    write failure, so the typed error is reported on stderr instead of raised.
    Returns True when the event was durably appended.
    """
    try:
        emit_launch_terminal(project_dir, attempt_key=attempt_key,
                             outcome="INTERRUPTED", error="process lost "
                             "mid-launch (reconciled from durable OPEN event)",
                             reconciled=True)
        return True
    except TelemetryPersistenceError as tpe:
        print(f"[telemetry.gate] TELEMETRY_PERSISTENCE_FAILED (reconcile, "
              f"non-fatal): {tpe}", file=sys.stderr)
        return False
