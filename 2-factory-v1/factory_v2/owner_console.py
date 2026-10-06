"""Owner exception & progress console — decision CTO 2026-09-13.

READ-ONLY. Never imports runtime/executors/launch_gate/dispatcher_v3/recovery_allocation
and never writes anything outside the directory the caller passes via ``--out`` (except the
optional, explicitly-requested ``--receipt`` path, which the owner asked for separately).

It answers exactly two owner questions from artefacts that already exist on disk:

  1. What needs a HUMAN decision right now? (``exceptions`` — typed, sourced, never an
     auto-resolvable wait: those go under ``health.waiting`` instead)
  2. What shipped, and how far are we? (``progress``, ``increments_24h``, ``health``)

Every number is a direct recomputation of PROJECT_STATE.json / ROADMAP.yaml / the
attempt-account ledger / the daemon run-dir — nothing here is cached or estimated.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Iterable

import yaml

from factory_v2._io import _read_json, _parse_utc as _parse_ts

SCHEMA = "owner-console/v1"
RECEIPT_SCHEMA = "owner-console-receipt/v1"

#: Kinds a human must act on. Anything else is a wait the system will clear on its own.
VALID_EXCEPTION_KINDS = ("OWNER_DECISION_REQUIRED", "MAINTENANCE_REQUEST", "AUTHORITY_AMENDMENT_PENDING",
                          "VISUAL_VALIDATION", "GRANT_NEEDED", "CREDENTIAL_OR_PAYMENT", "PRODUCT_DECISION")

#: Never shown as an exception — the daemon clears these by itself over time.
AUTO_RESOLVABLE_KINDS = ("STANDING_CAP_REACHED", "WAITING_FOR_EVIDENCE", "CALENDAR_WAIT", "RELOAD_SCHEDULED",
                          "COST_NO_PROGRESS_TERMINAL", "CNP_REGRANT_WITHHELD", "RECOVERY_LAUNCH_BLOCKED")

_EVENT_TIME_KEYS = ("ts", "at", "timestamp")
_AUTHORITY_DRIFT_GRACE_S = 3600  # 1h — task says "seulement si persistant > 1h"

#: Twitter Radar product repo screenshots dir. Overridable for hermetic tests.
_DEFAULT_TR_SCREENSHOTS_DIR = (
    "/Users/jocelyngrosjean/conductor/workspaces/twitter-radar/twitter-radar-foundation-v2/"
    "docs/PRESENTATION_SCREENSHOTS")


def _tr_screenshots_dir() -> Path:
    return Path(os.environ.get("OWNER_CONSOLE_TR_SCREENSHOTS_DIR", _DEFAULT_TR_SCREENSHOTS_DIR))


def _now_iso(now: dt.datetime) -> str:
    return now.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _event_ts(event: dict) -> dt.datetime | None:
    for k in _EVENT_TIME_KEYS:
        if event.get(k):
            t = _parse_ts(event[k])
            if t is not None:
                return t
    return None


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _short_id(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


def _rel_or_str(repo_root: Path | None, path: Path) -> str:
    if repo_root is not None:
        try:
            return str(path.resolve().relative_to(repo_root.resolve()))
        except ValueError:
            pass
    return str(path)


# ------------------------------------------------------------------ exceptions per project

def _maintenance_exceptions(project_dir: Path, project: str, repo_root: Path | None) -> list[dict]:
    out = []
    mdir = project_dir / "maintenance_requests"
    if not mdir.is_dir():
        return out
    for f in sorted(mdir.glob("*.json")):
        mr = _read_json(f)
        if not mr or mr.get("state") != "OPEN":
            continue
        src = _rel_or_str(repo_root, f)
        kind = mr.get("kind") or "REPAIR_REENTRY_UNPROVABLE"
        milestone = mr.get("milestone_id") or "?"
        out.append({
            "id": _short_id(project, "MAINTENANCE_REQUEST", src),
            "project": project,
            "kind": "MAINTENANCE_REQUEST",
            "since": mr.get("first_seen") or mr.get("last_seen") or "",
            "source": src,
            "title": f"Requête de maintenance ouverte — {milestone} ({kind})",
            "suggested_action": (mr.get("resolves_when") or "Rejouer la reproduction gelée et statuer.")[:280],
        })
    return out


def _handoff_exceptions(project_dir: Path, project: str, repo_root: Path | None) -> list[dict]:
    out = []
    hf_path = project_dir / "DURABLE_HANDOFF.json"
    hf = _read_json(hf_path)
    if not hf:
        return out
    if hf.get("acknowledged") is True:
        return out
    src = _rel_or_str(repo_root, hf_path)
    out.append({
        "id": _short_id(project, "OWNER_DECISION_REQUIRED", src),
        "project": project,
        "kind": "OWNER_DECISION_REQUIRED",
        "since": hf.get("produced_at") or "",
        "source": src,
        "title": f"Handoff durable non acquitté — bascule vers {hf.get('next_milestone', '?')}",
        "suggested_action": ("Statuer sur : " + str(hf.get("handoff_reason") or ""))[:280],
    })
    return out


def _visual_validation_exceptions(project: str) -> list[dict]:
    if project != "twitter-radar":
        return []
    shots_dir = _tr_screenshots_dir()
    if not shots_dir.is_dir():
        return []
    pngs = sorted(shots_dir.glob("*.png"))
    if not pngs:
        return []
    if (shots_dir / "OWNER_DECISION.json").exists():
        return []
    newest = max(pngs, key=lambda p: p.stat().st_mtime)
    since = dt.datetime.fromtimestamp(min(p.stat().st_mtime for p in pngs), dt.timezone.utc)
    src = str(newest)
    return [{
        "id": _short_id(project, "VISUAL_VALIDATION", str(shots_dir)),
        "project": project,
        "kind": "VISUAL_VALIDATION",
        "since": _now_iso(since),
        "source": src,
        "title": f"{len(pngs)} capture(s) de présentation en attente de validation visuelle",
        "suggested_action": f"Regarder {shots_dir} et écrire OWNER_DECISION.json (accepté/refusé).",
    }]


def _authority_drift_exceptions(state: dict, project: str, source: str, now: dt.datetime) -> list[dict]:
    events = state.get("events") or []
    last_by_milestone: dict[str, dict] = {}
    last_ts_by_milestone: dict[str, dt.datetime] = {}
    # L5_factory_v2_rest_1-027: the grace period runs from the START of the
    # unresolved drift episode (first AUTHORITY_DRIFT_BLOCKED not followed by
    # any other event for that milestone), not from the latest repeat — a
    # poll that repeats the block every minute must not reset the clock.
    episode_start: dict[str, dt.datetime] = {}
    dated = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        ms = ev.get("milestone")
        if not ms:
            continue
        t = _event_ts(ev)
        if t is None:
            continue
        dated.append((t, ev, ms))
    dated.sort(key=lambda x: x[0])
    for t, ev, ms in dated:
        last_ts_by_milestone[ms] = t
        last_by_milestone[ms] = ev
        if ev.get("event") == "AUTHORITY_DRIFT_BLOCKED":
            episode_start.setdefault(ms, t)
        else:
            episode_start.pop(ms, None)
    completed = state.get("completed_milestones") or {}
    out = []
    for ms, ev in last_by_milestone.items():
        if ev.get("event") != "AUTHORITY_DRIFT_BLOCKED":
            continue
        if (completed.get(ms) or {}).get("state") == "COMPLETE":
            continue
        t = episode_start.get(ms, last_ts_by_milestone[ms])
        if (now - t).total_seconds() <= _AUTHORITY_DRIFT_GRACE_S:
            continue
        drift = "; ".join(ev.get("drift") or []) or "authority hash mismatch"
        out.append({
            "id": _short_id(project, "AUTHORITY_AMENDMENT_PENDING", source, ms),
            "project": project,
            "kind": "AUTHORITY_AMENDMENT_PENDING",
            "since": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": source,
            "title": f"{ms} bloqué par dérive d'autorité depuis plus d'1h",
            "suggested_action": f"Amender l'autorité ou re-figer le contrat : {drift}"[:280],
        })
    return out


# ------------------------------------------------------------------ progress per project

def _milestone_ids(roadmap: dict) -> list[dict]:
    return list(roadmap.get("milestones") or [])


def _progress_for_project(project_dir: Path, state: dict, roadmap: dict, attempts: list[dict]) -> dict:
    milestones = _milestone_ids(roadmap)
    ids = [m["id"] for m in milestones]
    retired_ids = {m["id"] for m in milestones if m.get("retired")}
    completed = state.get("completed_milestones") or {}
    complete_ids = [i for i in ids if (completed.get(i) or {}).get("state") == "COMPLETE"]
    remaining = [i for i in ids if i not in complete_ids and i not in retired_ids]

    open_keys = {a.get("attempt_key") for a in attempts if a.get("phase") == "OPEN" and a.get("attempt_key")}
    terminal_keys = {a.get("attempt_key") for a in attempts if a.get("phase") == "TERMINAL" and a.get("attempt_key")}
    in_flight = sorted(open_keys - terminal_keys)

    return {
        "total": len(ids),
        "complete": len(complete_ids),
        "retired": len(retired_ids),
        "in_progress": len(remaining),
        "remaining": remaining,
        "current_milestone": state.get("current_milestone"),
        "in_flight": in_flight,
        "last_settlement": state.get("last_settlement"),
    }


def _increments_for_project(project_dir: Path, project: str, state: dict,
                            roadmap: dict, now: dt.datetime) -> list[dict]:
    goals = {m["id"]: str(m.get("goal") or "")[:140] for m in _milestone_ids(roadmap)}
    cutoff = now - dt.timedelta(hours=24)
    out = []
    from factory_v2 import event_journal
    for ev in event_journal.iter_events(project_dir):
        if not isinstance(ev, dict):
            continue
        if (ev.get("event") or ev.get("kind")) != "MILESTONE_SETTLED":
            continue
        t = _event_ts(ev)
        if t is None or t < cutoff:
            continue
        ms = ev.get("milestone")
        out.append({
            "project": project,
            "milestone": ms,
            "settled_at": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "what": goals.get(ms, ""),
        })
    return out


# ------------------------------------------------------------------ health

def _standing_cap_for_project(state: dict, attempts: list[dict], now: dt.datetime) -> dict:
    latest, latest_t = None, None
    for ev in state.get("events") or []:
        if not isinstance(ev, dict) or ev.get("event") != "STANDING_CAP_REACHED":
            continue
        t = _event_ts(ev)
        if t is None:
            continue
        if latest_t is None or t >= latest_t:
            latest_t, latest = t, ev
    if latest is not None:
        return {"cap": latest.get("cap"), "used_today": latest.get("used_today"),
                "resets_at": latest.get("resets_at"), "source": "STANDING_CAP_REACHED"}
    cutoff = now - dt.timedelta(hours=24)
    used = 0
    # L5_factory_v2_rest_1-026: count ATTEMPTS, not ledger rows — an attempt
    # has an OPEN row and a TERMINAL row; folded, it is one launch.
    from .resource_accounting import fold as _fold
    for a in _fold(list(attempts)):
        t = _parse_ts(a.get("started_at") or a.get("ended_at"))
        if t is not None and t >= cutoff and a.get("consumes_model_quota") is True:
            used += 1
    return {"cap": "UNKNOWN", "used_today": used, "resets_at": None, "source": "attempt_accounts_24h"}


def _parked_milestones(states: dict[str, dict]) -> list[dict]:
    out = []
    for project, state in states.items():
        last_by_ms: dict[str, dict] = {}
        last_ts: dict[str, dt.datetime] = {}
        for ev in state.get("events") or []:
            if not isinstance(ev, dict):
                continue
            ms = ev.get("milestone")
            if not ms:
                continue
            t = _event_ts(ev)
            if t is None:
                continue
            if ms not in last_ts or t >= last_ts[ms]:
                last_ts[ms] = t
                last_by_ms[ms] = ev
        for ms, ev in last_by_ms.items():
            if (ev.get("event") or ev.get("kind")) == "MILESTONE_PARKED":
                out.append({"project": project, "milestone": ms, "since": last_ts[ms].strftime("%Y-%m-%dT%H:%M:%SZ")})
    return out


def _waiting(states: dict[str, dict], now: dt.datetime) -> list[dict]:
    out = []
    for project, state in states.items():
        last_by_kind: dict[str, dict] = {}
        last_ts: dict[str, dt.datetime] = {}
        for ev in state.get("events") or []:
            if not isinstance(ev, dict):
                continue
            kind = ev.get("event") or ev.get("kind")
            if kind not in AUTO_RESOLVABLE_KINDS:
                continue
            t = _event_ts(ev)
            if t is None:
                continue
            if kind not in last_ts or t >= last_ts[kind]:
                last_ts[kind] = t
                last_by_kind[kind] = ev
        for kind, ev in last_by_kind.items():
            resume = ev.get("resets_at")
            if resume:
                r = _parse_ts(resume)
                if r is not None and r < now:
                    continue  # already past its own resume time — stale, don't show as waiting
            out.append({
                "project": project, "kind": kind,
                "since": last_ts[kind].strftime("%Y-%m-%dT%H:%M:%SZ"),
                "resume_at": resume,
                "reason": ev.get("reason") or ev.get("milestone") or "",
            })
    return out


def _last_heartbeat_age_s(log_path: Path | None, now: dt.datetime) -> float | None:
    if log_path is None or not log_path.is_file():
        return None
    try:
        mtime = dt.datetime.fromtimestamp(log_path.stat().st_mtime, dt.timezone.utc)
    except OSError:
        return None
    tail = []
    try:
        with log_path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                tail.append(line)
                if len(tail) > 4000:
                    tail.pop(0)
    except OSError:
        return None
    for line in reversed(tail):
        if "HEARTBEAT" not in line:
            continue
        m = re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})", line)
        if m:
            t = _parse_ts(m.group(0))
            if t is not None:
                return max(0.0, (now - t).total_seconds())
        # No embedded timestamp in this daemon's log format: the file's own mtime is the
        # best available proxy for "when was the last line written".
        return max(0.0, (now - mtime).total_seconds())
    return None


def _quota_snapshot(run_dir: Path | None) -> dict:
    if run_dir is None:
        return {}
    data = _read_json(run_dir / "QUOTA_SNAPSHOT.json")
    if not data:
        return {}
    providers = data.get("providers") or {}
    out = {}
    for name, p in providers.items():
        if not isinstance(p, dict):
            continue
        out[name] = {"plan": p.get("plan"), "policy_hint": p.get("policy_hint"), "windows": p.get("windows")}
    return out


# ------------------------------------------------------------------ build

def build(projects: Iterable[str | Path], run_dir: str | Path | None = None,
          log_path: str | Path | None = None, *, repo_root: str | Path | None = None,
          now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    repo_root = Path(repo_root) if repo_root is not None else None
    run_dir = Path(run_dir) if run_dir else None
    log_path = Path(log_path) if log_path else None

    exceptions: list[dict] = []
    progress: dict = {}
    increments: list[dict] = []
    states: dict[str, dict] = {}

    for raw in projects:
        project_dir = Path(raw)
        project = project_dir.name
        state = _read_json(project_dir / "PROJECT_STATE.json") or {}
        roadmap = {}
        rm_path = project_dir / "ROADMAP.yaml"
        if rm_path.is_file():
            try:
                roadmap = yaml.safe_load(rm_path.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError:
                roadmap = {}
        attempts = _read_jsonl(project_dir / "evidence" / "attempt_accounts.jsonl")
        states[project] = state

        exceptions.extend(_maintenance_exceptions(project_dir, project, repo_root))
        exceptions.extend(_handoff_exceptions(project_dir, project, repo_root))
        exceptions.extend(_visual_validation_exceptions(project))
        exceptions.extend(_authority_drift_exceptions(
            state, project, _rel_or_str(repo_root, project_dir / "PROJECT_STATE.json"), now))

        progress[project] = _progress_for_project(project_dir, state, roadmap, attempts)
        increments.extend(_increments_for_project(project_dir, project, state, roadmap, now))

    exceptions.sort(key=lambda e: (e["project"], e["kind"], e["id"]))
    increments.sort(key=lambda i: i["settled_at"])

    standing_cap = {}
    for raw in projects:
        project_dir = Path(raw)
        project = project_dir.name
        attempts = _read_jsonl(project_dir / "evidence" / "attempt_accounts.jsonl")
        standing_cap[project] = _standing_cap_for_project(states.get(project, {}), attempts, now)

    daemon_pid = None
    if run_dir is not None:
        pid_txt = None
        pid_path = run_dir / "factoryd.pid"
        if pid_path.is_file():
            try:
                pid_txt = pid_path.read_text(encoding="utf-8").strip()
            except OSError:
                pid_txt = None
        if pid_txt:
            try:
                daemon_pid = int(pid_txt)
            except ValueError:
                daemon_pid = pid_txt

    loops = {}
    if run_dir is not None:
        loops = _read_json(run_dir / "BUILD_METRICS_LATEST.json") or {}

    health = {
        "daemon_pid": daemon_pid,
        "last_heartbeat_age_s": _last_heartbeat_age_s(log_path, now),
        "standing_cap": standing_cap,
        "quota": _quota_snapshot(run_dir),
        "parked": _parked_milestones(states),
        "loops": loops,
        "waiting": _waiting(states, now),
    }

    return {
        "schema": SCHEMA,
        "generated_at": _now_iso(now),
        "exceptions": exceptions,
        "progress": progress,
        "increments_24h": increments,
        "health": health,
    }


# ------------------------------------------------------------------ markdown rendering

def render_md(data: dict) -> str:
    lines = [f"# Console propriétaire — {data.get('generated_at', '')}", ""]

    lines.append("## Exceptions à trancher")
    exc = data.get("exceptions") or []
    if not exc:
        lines.append("- Aucune. Rien n'attend de décision humaine.")
    else:
        for e in exc:
            lines.append(f"- [{e.get('kind')}] {e.get('project')} — {e.get('title', '')} "
                         f"(depuis {e.get('since', '?')}, source: {e.get('source')}) "
                         f"→ {e.get('suggested_action', '')}")
    lines.append("")

    lines.append("## Progression")
    prog = data.get("progress") or {}
    if not prog:
        lines.append("- Aucun projet.")
    for project, p in prog.items():
        lines.append(f"- {project} : {p.get('complete')}/{p.get('total')} jalons complets "
                     f"({p.get('retired', 0)} retirés, {p.get('in_progress', 0)} restants) — "
                     f"jalon courant : {p.get('current_milestone') or '—'} — "
                     f"en vol : {len(p.get('in_flight') or [])}")
    lines.append("")

    lines.append("## Incréments livrés (24 h)")
    inc = data.get("increments_24h") or []
    if not inc:
        lines.append("- Aucun jalon réglé dans les dernières 24 heures.")
    else:
        for i in inc:
            lines.append(f"- {i.get('settled_at')} — {i.get('project')} : {i.get('milestone')} — {i.get('what', '')}")
    lines.append("")

    lines.append("## Santé")
    h = data.get("health") or {}
    lines.append(f"- Daemon : pid={h.get('daemon_pid')}, dernier battement il y a "
                 f"{h.get('last_heartbeat_age_s')} s")
    cap = h.get("standing_cap") or {}
    for project, c in cap.items():
        lines.append(f"- Plafond quotidien {project} : {c.get('used_today')}/{c.get('cap')} "
                     f"(reprise {c.get('resets_at') or '—'})")
    quota = h.get("quota") or {}
    for engine, q in quota.items():
        lines.append(f"- Quota {engine} : {q.get('policy_hint') or ''}")
    parked = h.get("parked") or []
    lines.append(f"- Jalons en pause : {len(parked)}" + (
        " — " + ", ".join(f"{p['project']}/{p['milestone']}" for p in parked) if parked else ""))
    waiting = h.get("waiting") or []
    if waiting:
        for w in waiting:
            lines.append(f"- Attente auto-résoluble : {w.get('project')} {w.get('kind')} "
                         f"(reprise {w.get('resume_at') or '—'})")
    else:
        lines.append("- Aucune attente auto-résoluble en cours.")
    lines.append("")

    return "\n".join(lines)


# ------------------------------------------------------------------ CLI

def _default_run_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / "JocelynFactory" / "run"


def _default_log_path() -> Path:
    return Path.home() / "Library" / "Logs" / "JocelynFactory" / "factoryd.stdout.log"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="factory_v2.owner_console", description=__doc__)
    ap.add_argument("--out", required=True, help="Directory to write OWNER_CONSOLE.json/.md into")
    ap.add_argument("--projects", nargs="+", required=True, help="Project directories (read-only)")
    ap.add_argument("--run-dir", default=None, help="Daemon run-dir (defaults to the standard launchd run-dir)")
    ap.add_argument("--log", default=None, help="Daemon stdout log path (defaults to the standard log path)")
    ap.add_argument("--repo-root", default=None, help="Repo root, for relativizing exception sources")
    ap.add_argument("--receipt", default=None,
                    help="Optional path to also write the owner-console-receipt/v1 JSON")
    ap.add_argument("--control-sha", default=None,
                    help="Control repo HEAD sha for the receipt (defaults to git rev-parse HEAD in --repo-root)")
    args = ap.parse_args(argv)

    run_dir = args.run_dir or _default_run_dir()
    log_path = args.log or _default_log_path()
    repo_root = args.repo_root or os.getcwd()

    data = build(args.projects, run_dir=run_dir, log_path=log_path, repo_root=repo_root)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "OWNER_CONSOLE.json").write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                                                encoding="utf-8")
    (out_dir / "OWNER_CONSOLE.md").write_text(render_md(data), encoding="utf-8")

    if args.receipt:
        control_sha = args.control_sha
        if not control_sha:
            import subprocess
            r = subprocess.run(["git", "-C", str(repo_root), "rev-parse", "HEAD"],
                               capture_output=True, text=True, timeout=30)
            control_sha = r.stdout.strip() if r.returncode == 0 else ""
        auto_hidden = 0
        for state in (_read_json(Path(p) / "PROJECT_STATE.json") or {} for p in args.projects):
            for ev in state.get("events") or []:
                if isinstance(ev, dict) and (ev.get("event") or ev.get("kind")) in AUTO_RESOLVABLE_KINDS:
                    auto_hidden += 1
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "generated_at": data["generated_at"],
            "exceptions_count": len(data["exceptions"]),
            "auto_resolvable_hidden_count": auto_hidden,
            "control_sha": control_sha,
        }
        receipt_path = Path(args.receipt)
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(json.dumps(receipt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
