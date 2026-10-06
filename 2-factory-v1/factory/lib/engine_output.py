"""Reading an engine's output — prose, resumable session, truncation, DIAGNOSE/REPLAN judgment.

Policy module of dispatcher_v3 (F4 extraction). Pure functions over argv + captured stdout: no launch, no git,
no receipt. Every reader is fail-closed: nothing parseable → None / "" — never a guess.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

try:
    from factory_v2.telemetry import adapters as _tel_adapters
except Exception:  # pragma: no cover
    _tel_adapters = None

# codex envelopes that are NEVER the agent's own words, whatever JSON they carry (LOT C27)
CODEX_INFRA_EVENT_TYPES = frozenset({"thread.started", "turn.started", "turn.completed", "turn.failed",
                                     "session_meta", "turn_context", "token_count", "error"})


def json_line(line: str):
    """One JSON value from a line, or None (blank/unparseable)."""
    line = line.strip()
    if not line:
        return None
    try:
        return json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None


def last_json_event(out: str) -> "dict | None":
    """Last line of `out` that parses as a JSON object (evidence only, never accounting)."""
    last = None
    for line in (out or "").splitlines():
        obj = json_line(line)
        if isinstance(obj, dict):
            last = obj
    return last


def engine_text(cmd: list, out: str) -> str:
    """The agent's prose recovered from a structured stream (raw stdout when no final message can be read)."""
    if _tel_adapters is None or not out:
        return out
    try:
        return _tel_adapters.final_text(_tel_adapters.detect_format(cmd), out) or out
    except Exception:
        return out


def extract_resumable_session(out: str) -> "str | None":
    """Session id when `out` carries a claude terminal {"type": "result", "subtype": "error_max_turns"} event
    (last one wins; a whole-stdout single object still works). None for GLM/Codex streams."""
    if not out:
        return None
    data = None
    for line in out.splitlines():
        obj = json_line(line)
        if isinstance(obj, dict) and obj.get("type") == "result":
            data = obj
    if data is None:
        data = json_line(out)
    if not isinstance(data, dict) or data.get("subtype") != "error_max_turns":
        return None
    sid = data.get("session_id")
    return sid if isinstance(sid, str) and sid else None


def stream_truncation_verdict(cmd: list, out: str, rc: int) -> "dict | None":
    """Fail-closed: a claude stream-json launch that ended WITHOUT its terminal `result` event is unmeasurable AND
    unresumable. None for other formats, complete streams, or an empty `out` (already TIMEOUT/MOTOR_UNAVAILABLE)."""
    if _tel_adapters is None:
        return None
    try:
        if _tel_adapters.detect_format(cmd) != _tel_adapters.CLAUDE_STREAM_JSON:
            return None
        parsed = _tel_adapters.parse_claude_stream_json((out or "").splitlines(), clean_exit=(rc == 0))
    except Exception:
        return None
    if parsed.terminal_reason != "TRUNCATED":
        return None
    last_event = last_json_event(out)
    return {"verdict": "ENGINE_STREAM_TRUNCATED", "last_event": last_event,
            "why": f"claude stream-json ended without a terminal result event — last_event={last_event!r}"}


def codex_out_file_text(cmd: list) -> str:
    """codex's own `-o <path>` last-agent-message sink named in argv, stripped — or ""."""
    argv = [str(a) for a in (cmd or [])]
    if "-o" not in argv or argv.index("-o") + 1 >= len(argv):
        return ""
    try:
        return Path(argv[argv.index("-o") + 1]).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def codex_last_agent_message_from_stream(out: str) -> str:
    """LAST agent-authored message in a `codex exec --json` JSONL stream (dotted and older event_msg shapes)."""
    last = ""
    for line in (out or "").splitlines():
        ev = json_line(line)
        if not isinstance(ev, dict) or ev.get("type") in CODEX_INFRA_EVENT_TYPES:
            continue
        etype, text = ev.get("type"), ""
        if etype == "item.completed":
            item = ev.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = str(item.get("text") or "")
        elif etype == "event_msg":
            body = ev.get("payload") if isinstance(ev.get("payload"), dict) else ev.get("msg")
            if isinstance(body, dict) and body.get("type") == "agent_message":
                text = str(body.get("message") or "")
            elif isinstance(body, dict) and body.get("type") == "task_complete":
                text = str(body.get("last_agent_message") or "")
        elif etype == "agent_message":
            text = str(ev.get("message") or ev.get("text") or "")
        elif etype == "task_complete":
            text = str(ev.get("last_agent_message") or "")
        if text.strip():
            last = text
    return last


def recovery_agent_message(cmd: list, out: str) -> tuple[str, str]:
    """The DIAGNOSE/REPLAN engine's own final message, or ("", "NO_AGENT_MESSAGE") — never raw event-stream stdout."""
    argv0 = os.path.basename(str(cmd[0])) if cmd else ""
    if argv0 == "codex":
        text = codex_out_file_text(cmd) or codex_last_agent_message_from_stream(out)
    elif argv0 == "claude" and _tel_adapters is not None:
        try:
            text = _tel_adapters.final_text(_tel_adapters.detect_format(cmd), out) if out else ""
        except (json.JSONDecodeError, TypeError, ValueError, KeyError, AttributeError):
            text = ""
    else:
        text = engine_text(cmd, out)
    return (text, "") if text.strip() else ("", "NO_AGENT_MESSAGE")


def extract_json_object(text: str) -> dict | None:
    """The single JSON object in a final message: whole text, a fenced block, or the first {...} span.

    L3-005 (relecture 2026-09): delegated to factory_v2.shared_parsers — the
    brace-counting fallback here rejected a valid object whose string value
    contained `}` when prose preceded it. raw_decode respects strings/escapes.
    """
    try:
        from factory_v2.shared_parsers import extract_json_object as _shared
    except ImportError:  # launched directly from factory/lib: repo root not on sys.path yet
        import sys
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        from factory_v2.shared_parsers import extract_json_object as _shared
    return _shared(text)


def validate_recovery_artifact(task_kind: str, parsed: dict) -> tuple[bool, str]:
    """Same shape runtime._execute_recovery_work checks — fail-closed at write time (typed, never truthy)."""
    if not isinstance(parsed, dict):
        return False, "not a JSON object"
    if task_kind == "diagnose":
        rc, fix = parsed.get("root_cause"), parsed.get("proposed_fix")
        if isinstance(rc, str) and rc.strip() and isinstance(fix, str) and fix.strip():
            return True, ""
        return False, "missing non-empty string root_cause or proposed_fix"
    plan = parsed.get("derived_plan")
    if not isinstance(plan, dict):
        return False, "missing derived_plan (must be a JSON object)"
    changes = plan.get("changes")
    if isinstance(changes, dict) and changes:
        return True, ""
    return False, "missing derived_plan.changes (must be a non-empty JSON object)"


def recovery_artifact_path(project_dir: Path, mission_id: str, task_kind: str) -> Path:
    """Exactly the path runtime._execute_recovery_work reads back: evidence/recovery/{recovery_id}_{kind}.json."""
    suffix = "diagnosis" if task_kind == "diagnose" else "replan"
    return project_dir / "evidence" / "recovery" / f"{mission_id.removeprefix('recovery_')}_{suffix}.json"
