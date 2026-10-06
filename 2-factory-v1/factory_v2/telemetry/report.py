"""`python3 -m factory_v2.telemetry` — the local status the build needed now.

Two outputs from one computation, so they cannot disagree:

    report.json   machine-readable, every number beside its coverage
    report.md     the same thing readable in a terminal

Both label their data coverage on every section. There is no chart, no server
and no web app: the ask was measurement, and a number you can `jq` is worth more
during a live run than a page you have to open.

`backfill` ingests engine logs that already exist on disk — the app007 mission
stdout captures, the operator's own CLI session logs — and marks every row
`provenance=BACKFILL_BEST_EFFORT`. That label is load-bearing: those logs were
written before this instrumentation existed, so what they cover is what happened
to be captured, not what was launched. The report says so rather than presenting
a partial history as a complete one.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import adapters, aggregate, capture, inventory, ledger
from .record import (UNKNOWN, LaunchObservation, content_fingerprint,
                     observation_key, utcnow, value_of)


def _fingerprint(parsed) -> str:
    """Identify an imported launch by what it consumed, not by its file path.

    Path-keying made thirteen sandbox copies of one log look like thirteen
    launches; session-keying made six resumed invocations look like one.
    """
    u = parsed.usage
    return content_fingerprint(
        first_event_at=getattr(parsed, "first_event_at", ""),
        totals=(value_of(u.input_tokens), value_of(u.output_tokens),
                value_of(u.cache_read_tokens), value_of(u.cache_write_tokens),
                value_of(u.reasoning_tokens), u.completeness))


#: Limits that survive every check in this module. Published in the receipt so
#: a reader settling a milestone sees them next to the numbers, rather than
#: having to find them in a source comment.
KNOWN_UNKNOWNS = [
    {"id": "IN_TREE_EVIDENCE_IS_CLAIMANT_WRITABLE",
     "severity": "MAJOR",
     "what": ("the telemetry and attempt ledgers live inside the repo under "
              "review, and the builder engine runs with write access to that "
              "tree. A worker could author both ledgers and the receipt."),
     "what_the_check_does_buy": ("a forgery must now be CONSISTENT across two "
                                 "ledgers with different schemas, different "
                                 "writers and different keys, and any "
                                 "inconsistency fails loudly"),
     "real_fix": ("an out-of-tree observation channel the worker cannot write, "
                  "or a supervisor signature over the ledger"),
     "status": "OPEN"},
    {"id": "PROVIDER_REQUESTS_NEVER_COUNTED",
     "severity": "MINOR",
     "what": ("no engine emits a count of actual API requests. `model_turns` is "
              "reported instead and is a LOWER BOUND: retries, continuations "
              "and compaction passes hide inside a turn."),
     "status": "OPEN_BY_PROVIDER_LIMIT"},
    {"id": "CONTEXT_UTILISATION_ONLY_FROM_STREAMS",
     "severity": "MINOR",
     "what": ("terminal `-p --output-format json` payloads report cumulative "
              "session tokens, so no single request's occupancy can be derived "
              "from them; utilisation is UNKNOWN there and measured only from "
              "per-message stream events with one model"),
     "status": "OPEN_BY_PROVIDER_LIMIT"},
    {"id": "MONEY_CHARGED_NEVER_OBSERVED",
     "severity": "MINOR",
     "what": ("no launched CLI reports money actually charged. Price ESTIMATES "
              "are published under their own name and never summed into a cost"),
     "status": "OPEN_BY_PROVIDER_LIMIT"},
    {"id": "OPERATOR_USAGE_OUTSIDE_THIS_WORKSPACE",
     "severity": "MINOR",
     "what": ("operator Codex sessions whose cwd is another workspace are "
              "readable but deliberately not imported; they are out of scope "
              "for this campaign rather than absent, and the skipped count is "
              "published"),
     "status": "OUT_OF_SCOPE_BY_CHOICE"},
    {"id": "KNOWN_UNKNOWNS_GATE_IS_ID_PRESENCE_ONLY",
     "severity": "MINOR",
     "what": ("the verifier checks that the forgeability id is disclosed, not "
              "that the disclosure has content. A hand-authored receipt with "
              "just the id and no `what` would pass"),
     "status": "OPEN"},
    {"id": "STREAM_CONTEXT_WINDOW_PICKS_THE_LARGEST_MODEL",
     "severity": "MINOR",
     "what": ("peak per-request occupancy divides by max(contextWindow) across "
              "the launch's models. With one tagged model that is correct; when "
              "no assistant message carries a model the single-model guard "
              "passes vacuously and the divisor may belong to another model. "
              "Understates, never inflates"),
     "status": "OPEN"},
    {"id": "VERIFIER_RECOMPUTES_WITHOUT_THE_COLLAPSE_TRANSFORMS",
     "severity": "MINOR",
     "what": ("the verifier recomputes from fold(read_rows) without "
              "`_collapse_cumulative_sessions`/`_drop_reimports`, which the "
              "receipt applies. A ledger holding two same-session "
              "CUMULATIVE_OR_REPLAY rows would make a legitimate receipt FAIL — "
              "fail-closed and loud, but the transform is unaudited by the "
              "verifier"),
     "status": "OPEN"},
    {"id": "OPENCODE_SESSION_ID_REUSE_UNPROVEN",
     "severity": "MINOR",
     "what": ("opencode is marked CUMULATIVE_OR_REPLAY so two files of one "
              "sessionID collapse to the superset. If opencode ever reuses a "
              "sessionID across genuinely distinct non-replay invocations this "
              "would drop real spend. The claude exemption is MEASURED on this "
              "campaign's logs; the opencode claim is reasoned from its replay "
              "behaviour and not yet evidenced by a recorded pair"),
     "status": "OPEN"},
    {"id": "DISPATCHER_REACHABILITY_IGNORES_PROJECT_SCOPE",
     "severity": "MINOR",
     "what": ("`inventory.dispatcher_reachability` probes route() without a "
              "project, while the dispatcher passes one. A future "
              "project-scoped owner policy could make the codex "
              "INSTRUMENTED_NOT_REACHED claim stale without failing anything"),
     "status": "OPEN"},
]


def build_report(project_dirs, *, extra_observations=None,
                 include_inventory: bool = True) -> dict:
    """Everything the report knows, from the ledgers of the given projects."""
    observations = []
    accounts = []
    sources = []
    for pd in project_dirs:
        pd = Path(pd)
        rows = ledger.fold(ledger.read_rows(pd))
        attempts = _accounts(pd)
        observations.extend(rows)
        accounts.extend(attempts)
        sources.append({"project_dir": str(pd),
                        "telemetry_rows": len(rows),
                        "attempt_rows": len(attempts)})
    extras = list(extra_observations or [])
    observations.extend(extras)
    # A launch already measured LIVE must not be counted again when its log is
    # later imported. The two paths key differently on purpose — live rows use
    # the supervisor's execve identity, imported rows a session+content
    # fingerprint — so `fold` alone cannot see that they are the same launch.
    # Dropping the import is the right direction: the live row was measured at
    # the boundary and carries the supervisor's own facts.
    observations = ledger.fold(observations)
    observations = _drop_reimports(observations, extras)
    observations = _collapse_cumulative_sessions(observations, preserve_launches=True)

    summary = aggregate.summarise(observations, accounts)
    report = {
        "schema": "telemetry/report/v1",
        "generated_at": utcnow(),
        "sources": sources,
        "summary": summary,
        "in_flight_attempts": aggregate.in_flight(observations, accounts),
        "crash_and_recovery": aggregate.restarts_and_recovery(accounts),
        "provenance_split": _provenance(observations),
    }
    if include_inventory:
        report["coverage_inventory"] = inventory.summary()
    return report


def _usage_identity(row: dict):
    """What makes two rows the SAME launch across the live/import boundary.

    A provider session plus the exact counters it reported. Two rows agreeing on
    both are one launch observed twice, whatever key namespace produced them.
    Rows with no session id are left alone — there is nothing to match on, and
    guessing would merge unrelated launches.
    """
    session = row.get("session_id") or ""
    if not session:
        return None
    usage = row.get("usage") or {}
    counters = tuple(
        (usage.get(f) or {}).get("value") if isinstance(usage.get(f), dict) else None
        for f in ("input_tokens", "output_tokens", "cache_read_tokens",
                  "cache_write_tokens", "reasoning_tokens"))
    # Completeness is deliberately NOT part of the identity. The same log
    # measured live (rc=0 → COMPLETE) and re-imported without the exit code
    # (→ PARTIAL_FLOOR) is still ONE launch, and including completeness made the
    # two look different and kept both.
    return (row.get("engine"), session, counters)


def _collapse_cumulative_sessions(observations: list, *, preserve_launches: bool = False) -> list:
    """For providers whose logs RESTATE earlier usage, keep launch-cost rows.

    codex writes a cumulative `total_token_usage`, and a resumed opencode
    session replays its earlier parts into the later file. So two files of one
    session overlap: the smaller is a prefix of the larger, and summing them
    counts the prefix twice. Claude is exempt and must stay exempt — each of its
    `--resume` invocations reports only its own usage, so its files are distinct
    billed launches.

    Token counters after the first observation are converted from cumulative
    snapshots to deltas.  Independent wall time and launch count stay attached
    to every execve instead of disappearing with the redundant snapshot.
    """
    def _total(row):
        usage = row.get("usage") or {}
        return sum((usage.get(f) or {}).get("value") or 0
                   for f in ("input_tokens", "output_tokens",
                             "cache_read_tokens", "cache_write_tokens",
                             "reasoning_tokens")
                   if isinstance(usage.get(f), dict)
                   and isinstance((usage.get(f) or {}).get("value"), (int, float)))

    # Compatibility for callers that ask specifically for token snapshots:
    # the historical helper contract is one largest snapshot per session.
    if not preserve_launches:
        best, passthrough = {}, []
        for row in observations:
            usage = row.get("usage") or {}
            session = row.get("session_id") or ""
            if (usage.get("session_log_semantics") != "CUMULATIVE_OR_REPLAY"
                    or not session):
                passthrough.append(row)
                continue
            key = (row.get("engine"), session)
            if key not in best or _total(row) > _total(best[key]):
                best[key] = row
        survivors = {id(row) for row in best.values()}
        return [row for row in observations
                if row in passthrough or id(row) in survivors]

    import copy
    previous: dict = {}
    adjusted = []
    for row in observations:
        usage = row.get("usage") or {}
        session = row.get("session_id") or ""
        if (usage.get("session_log_semantics") != "CUMULATIVE_OR_REPLAY"
                or not session):
            adjusted.append(row)
            continue
        key = (row.get("engine"), session)
        current = copy.deepcopy(row)
        prior = previous.get(key)
        if prior is not None:
            for field in ("input_tokens", "output_tokens", "cache_read_tokens",
                          "cache_write_tokens", "reasoning_tokens", "total_tokens"):
                slot = (current.get("usage") or {}).get(field)
                old = (prior.get("usage") or {}).get(field)
                if (isinstance(slot, dict) and isinstance(old, dict)
                        and isinstance(slot.get("value"), (int, float))
                        and not isinstance(slot.get("value"), bool)
                        and isinstance(old.get("value"), (int, float))
                        and not isinstance(old.get("value"), bool)):
                    slot["value"] = max(0, slot["value"] - old["value"])
                    slot["source"] = str(slot.get("source") or "") + ".cumulative_delta"
        previous[key] = row
        adjusted.append(current)
    return adjusted


def _drop_reimports(observations: list, extras: list) -> list:
    """Remove an imported row when the same launch is already recorded live."""
    extra_keys = {o.get("observation_key") for o in extras
                  if isinstance(o, dict)}
    live_identities = {
        _usage_identity(o) for o in observations
        if o.get("observation_key") not in extra_keys
        and _usage_identity(o) is not None}
    if not live_identities:
        return observations
    kept = []
    for o in observations:
        if (o.get("observation_key") in extra_keys
                and _usage_identity(o) in live_identities):
            continue          # already measured at the launch boundary
        kept.append(o)
    return kept


def _accounts(project_dir: Path) -> list:
    try:
        from ..launch_gate import ledger_path
        from ..resource_accounting import fold, read_rows
        return fold(read_rows(ledger_path(project_dir)))
    except Exception:
        return []


def _provenance(observations: list) -> dict:
    out: dict = {}
    for o in observations:
        key = str(o.get("provenance") or "UNKNOWN")
        out[key] = out.get(key, 0) + 1
    if "BACKFILL_BEST_EFFORT" in out:
        out["backfill_note"] = (
            "backfilled rows come from logs captured before this instrumentation "
            "existed. Their coverage is whatever happened to be written to disk, "
            "NOT a complete history of what was launched.")
    return out


# ── backfill from logs that already exist ────────────────────────────────────
def sidecar_for(log_path: Path) -> dict:
    """The launcher's own record of a log, when one sits beside it.

    `run_glm.py` and its siblings write `<label>-process.json` next to
    `<label>.jsonl` containing engine, model, role, pid, birth, started_at,
    budget and — once the process ends — rc and wall_time_s. That file is the
    SUPERVISOR's observation of the launch, which is exactly the half a log
    cannot supply: a transcript knows what the model said, not when the
    supervisor started it or how long it ran.

    Without this join every backfilled row reported wall time UNKNOWN and a
    guessed role, which is how a GLM proof-review ended up labelled "builder".
    """
    sidecar = log_path.with_name(log_path.stem + "-process.json")
    if not sidecar.is_file():
        return {}
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def backfill(paths, *, lineage: str = "CTO_OVERHEAD", project: str = "",
             fmt: str = "", limit: int = 0, now_epoch=None,
             window: str = "CURRENT_CAMPAIGN") -> list:
    """Parse existing engine logs into observations. Best-effort, and labelled.

    Never fabricates an attempt: a log that yields no usage produces a row whose
    usage is ABSENT with its reason, which is then visible as missing coverage.

    Where a `-process.json` sidecar exists its supervisor-side facts win over
    anything inferred here — role, model and wall time are OBSERVED there, and
    an observation always beats a default.
    """
    import time as _time
    out = []
    for i, path in enumerate(sorted(paths)):
        if limit and i >= limit:
            break
        p = Path(path)
        if p.name.endswith("-process.json"):
            continue                     # the sidecar itself is not a log
        chosen = fmt or _sniff(p)
        if not chosen:
            continue
        side = sidecar_for(p)
        # The launcher's own exit code decides completeness for formats with no
        # terminal totals event. Parsing imports without it made the SAME log
        # read PARTIAL_FLOOR on import and COMPLETE live.
        _rc = side.get("rc")
        parsed = capture.parse_log_file(
            p, fmt=chosen,
            clean_exit=(_rc == 0) if isinstance(_rc, int) else None)
        engine = str(side.get("engine") or _engine_of(chosen))
        role = str(side.get("role") or
                   ("cto_overhead" if lineage == "CTO_OVERHEAD" else "builder"))

        # Wall time: terminal runs record it; a run still in flight has not
        # finished, so its ELAPSED time is reported as such and never as a
        # duration. Reporting an in-flight job as complete is how a live review
        # disappears from "running now".
        wall, terminal = _sidecar_wall(side, now_epoch or _time.time())

        quota = parsed.quota
        if quota and not getattr(quota, "observed_at", ""):
            # The log's own first event beats its mtime: a historical log copied
            # with `cp` carries the COPY time, which would re-date an August
            # reading to today and publish it as the current window.
            _date_quota_from_log(quota, parsed, p)

        obs = LaunchObservation(
            observation_key=observation_key(
                engine=engine, session_id=parsed.session_id,
                content_fingerprint=_fingerprint(parsed)),
            engine=engine,
            provider={"claude": "anthropic", "glm": "zai",
                      "codex": "openai"}.get(engine, "UNKNOWN"),
            role=role,
            lineage=lineage, project=project,
            model_requested=str(side.get("model") or ""),
            model_observed=parsed.model_observed,
            harness_version=parsed.harness_version,
            session_id=parsed.session_id,
            pid=side.get("pid"),
            cwd=str(side.get("cwd") or parsed.cwd or ""),
            started_at=str(side.get("started_at") or ""),
            ended_at="" if not terminal else str(side.get("ended_at") or ""),
            wall_time_s=wall,
            rc=side.get("rc"),
            exit_class=_sidecar_exit_class(side, terminal),
            usage=parsed.usage, money=parsed.money, quota=quota,
            raw_usage_events=parsed.raw_events,
            stop_reason=parsed.stop_reason,
            terminal_reason=parsed.terminal_reason,
            api_time_s=parsed.api_time_s,
            rate_limit_waits=parsed.rate_limit_waits,
            provenance="BACKFILL_BEST_EFFORT",
            window=window,
            log_path=str(p),
        )
        out.append(obs.as_dict())
    return out


def _sidecar_wall(side: dict, now: float):
    """(wall_time_slot, is_terminal) from the launcher's own record."""
    from .record import measured, unknown
    w = side.get("wall_time_s")
    if isinstance(w, (int, float)) and not isinstance(w, bool) and w >= 0:
        return measured(round(float(w), 3), unit="s",
                        source="launcher_process_record.wall_time_s"), True
    if (isinstance(side.get("rc"), int) or side.get("ended_at")
            or side.get("timed_out")):
        return unknown("NO_LOG_CAPTURED",
                       detail="launcher is terminal but emitted no reliable duration"), True
    started = side.get("started_at")
    if started:
        try:
            import datetime as _dt
            t = _dt.datetime.fromisoformat(str(started).replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=_dt.timezone.utc)
            elapsed = now - t.timestamp()
            if elapsed >= 0:
                # ELAPSED so far, not a duration: this launch has not ended.
                return measured(round(elapsed, 1), unit="s",
                                source="launcher_process_record.started_at"
                                       "->now (STILL RUNNING, elapsed not final)"), False
        except ValueError:
            pass
    return unknown("NO_LOG_CAPTURED",
                   detail="no launcher process record for this log, so the "
                          "supervisor-side duration was never written down"), False


def _sidecar_exit_class(side: dict, terminal: bool) -> str:
    """How the launch ended, per the launcher's own record.

    RUNNING is claimed ONLY when a launcher record exists and shows a start with
    no terminal fields. With no record at all the outcome is simply unknown —
    saying RUNNING there put every historical import into "running now", which
    is a worse lie than an empty status line.
    """
    if not side:
        return ""
    if not terminal:
        return "RUNNING" if side.get("started_at") else ""
    if side.get("timed_out"):
        return "TIMEOUT"
    rc = side.get("rc")
    if isinstance(rc, int):
        return "SUCCESS" if rc == 0 else "PROVIDER_FAILURE"
    return ""


def _date_quota_from_log(quota, parsed, log_path: Path) -> None:
    """Date an undated reading, preferring evidence from inside the log."""
    from .record import OBSERVED_ADJACENT_EVENT
    first = getattr(parsed, "first_event_at", "")
    if first:
        quota.observed_at = str(first)
        quota.observed_at_source = OBSERVED_ADJACENT_EVENT
        return
    _date_quota_by_mtime(quota, log_path)


def _date_quota_by_mtime(quota, log_path: Path) -> None:
    """Last-resort dating for an imported reading, labelled as the proxy it is.

    mtime is a property of the FILE, not of the observation: a copied log shows
    the copy time. Only used when the log carries no timestamp of its own, and
    always labelled `file_mtime_proxy` so a reader can discount it.
    """
    from .record import OBSERVED_FILE_MTIME
    try:
        import datetime as _dt
        mtime = log_path.stat().st_mtime
    except OSError:
        return
    quota.observed_at = _dt.datetime.fromtimestamp(
        mtime, _dt.timezone.utc).isoformat(timespec="seconds")
    quota.observed_at_source = OBSERVED_FILE_MTIME


def _sniff(path: Path) -> str:
    """Decide a log's format from its FIRST parseable line, not its filename."""
    for line in capture.iter_lines(path, max_lines=50):
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("type") in ("system", "assistant", "user", "result",
                              "rate_limit_event"):
            return adapters.CLAUDE_STREAM_JSON
        if ev.get("type") in ("step_start", "step_finish", "text", "tool_use"):
            return adapters.OPENCODE_JSON
        if ev.get("type") in ("session_meta", "event_msg", "turn_context",
                              "response_item"):
            return adapters.CODEX_JSONL
        if "total_cost_usd" in ev or "modelUsage" in ev:
            return adapters.CLAUDE_RESULT_JSON
        return ""
    return ""


def current_root_session_id() -> str:
    """The root operator's CURRENT Codex session, as the environment reports it.

    The Codex CLI exports CODEX_SESSION_ID / CODEX_THREAD_ID, and a child process
    inherits them — so the session that is actually driving this campaign can be
    named exactly, instead of being guessed from a directory.
    """
    import os
    for var in ("CODEX_SESSION_ID", "CODEX_THREAD_ID"):
        val = (os.environ.get(var) or "").strip()
        if val:
            return val
    return ""


def codex_sessions_for_workspace(workspace, *, roots=None,
                                 current_session_id=None) -> list:
    """Codex rollout logs whose OWN cwd is this workspace. Nothing else.

    The root operator's CTO chat runs through the Codex CLI, which records every
    session under ~/.codex/sessions with a `session_meta.payload.cwd`.

    A cwd identifies the WORKSPACE, not this campaign. Ten sessions share this
    directory, and treating all of them as the current campaign attributed months
    of unrelated work to tonight's run. The sessions are therefore split:

      * `current_root` — the ONE session whose id matches CODEX_SESSION_ID, i.e.
        the chat actually driving this campaign. Its usage is real and attributable,
        but it is the session's CUMULATIVE lifetime, which is still not the same
        thing as this campaign; it is reported in its own window and never folded
        into CURRENT_CAMPAIGN.
      * `workspace_history` — every other cwd-matching session. Real consumption in
        this workspace, at some earlier time, on work this report cannot identify.

    Scope is deliberately narrow and is the whole safety argument:
      * ONLY the first line of each file is read, and ONLY to test its cwd;
      * a session whose cwd is anywhere else is skipped untouched — no broad
        import of personal history;
      * nothing reads credentials, and the codex adapter never touches
        `response_item`/`reasoning` payloads.

    Sessions outside this workspace stay UNOBSERVABLE-by-scope, and the report
    says how many were skipped so the coverage claim is bounded, not implied.
    """
    import os
    from glob import glob
    workspace = str(Path(workspace).resolve())
    roots = roots or [os.path.expanduser("~/.codex/sessions")]
    if current_session_id is None:
        current_session_id = current_root_session_id()
    matched, current, skipped, unreadable = [], [], 0, 0
    for root in roots:
        for path in sorted(glob(os.path.join(root, "**", "*.jsonl"),
                                recursive=True)):
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    first = fh.readline()
            except OSError:
                unreadable += 1
                continue
            try:
                ev = json.loads(first)
            except (json.JSONDecodeError, ValueError):
                skipped += 1
                continue
            if ev.get("type") != "session_meta":
                skipped += 1
                continue
            payload = ev.get("payload") or {}
            cwd = str(payload.get("cwd") or "")
            if cwd == workspace or cwd.startswith(workspace + os.sep):
                sid = str(payload.get("id") or payload.get("session_id") or "")
                if current_session_id and sid == current_session_id:
                    current.append(Path(path))
                else:
                    matched.append(Path(path))
            else:
                skipped += 1
    return {"matched": matched, "current_root": current,
            "current_root_session_id": current_session_id,
            "skipped_other_cwd": skipped, "unreadable": unreadable}


def _engine_of(fmt: str) -> str:
    return {adapters.CLAUDE_STREAM_JSON: "claude",
            adapters.CLAUDE_RESULT_JSON: "claude",
            adapters.OPENCODE_JSON: "glm",
            adapters.CODEX_JSONL: "codex"}.get(fmt, "UNKNOWN")


# ── markdown ─────────────────────────────────────────────────────────────────
def to_markdown(report: dict) -> str:
    s = report.get("summary", {})
    t = s.get("totals", {})
    acc = s.get("accepted", {})
    per = s.get("per_accepted_milestone", {})
    lines = []
    add = lines.append

    add("# Factory telemetry snapshot")
    add("")
    add(f"Generated {report.get('generated_at', UNKNOWN)} · "
        f"schema `{report.get('schema')}`")
    add("")
    add("> Every total below is printed beside the number of launches that "
        "could not be measured. A token figure with 40% coverage is the cost of "
        "40% of the work, not the cost of the work.")
    add("")

    # The current campaign is the question being asked. An imported historical
    # corpus answers a different one, and leading with it made a snapshot of
    # August read as the state of today.
    windows = s.get("by_window") or {}
    add("## Windows in this snapshot")
    add("")
    if not windows:
        add("No launches in scope.")
    else:
        add("| window | launches | wall s | tokens (exact) | exact coverage |")
        add("| --- | --- | --- | --- | --- |")
        for name in sorted(windows, key=lambda w: (w != "CURRENT_CAMPAIGN", w)):
            b = windows[name]
            add(f"| {'**' + name + '**' if name == 'CURRENT_CAMPAIGN' else name} "
                f"| {b['launches']} | {b['wall_time_s']} | "
                f"{_tok(b['tokens_exact'])} | {b['token_coverage_exact_pct']}% |")
        add("")
        add("`CURRENT_CAMPAIGN` is this run. Any other window is imported "
            "history kept separate on purpose — the two are never added into a "
            "single headline figure.")
    add("")

    add("## Measured spend — PRODUCTION missions, all windows")
    add("")
    add("| quantity | value | coverage |")
    add("| --- | --- | --- |")
    add(f"| engine launches | {t.get('launches', UNKNOWN)} | — |")
    add(f"| model turns (lower bound on requests) | "
        f"{t.get('model_turns', UNKNOWN)} | "
        f"{t.get('model_turns_unknown_launches', UNKNOWN)} launch(es) unknown |")
    add(f"| provider requests (actual API calls) | "
        f"{t.get('provider_requests', UNKNOWN)} | "
        f"{t.get('provider_requests_unknown_launches', UNKNOWN)} launch(es) unknown |")
    add(f"| wall time (s) | {t.get('wall_time_s', UNKNOWN)} | "
        f"{t.get('wall_time_unknown_launches', UNKNOWN)} launch(es) untimed |")
    add(f"| tokens (exact) | {_tok(t.get('tokens_exact'))} | "
        f"{t.get('rows_measured_complete', 0)} complete row(s) |")
    add(f"| tokens (lower bound only) | {_tok(t.get('tokens_lower_bound_only'))} | "
        f"{t.get('rows_measured_lower_bound', 0)} truncated row(s) |")
    add(f"| token coverage (any number) | {t.get('token_coverage_pct', UNKNOWN)}% | "
        f"{t.get('rows_unmeasured', 0)} unmeasured launch(es) |")
    add(f"| token coverage (exact totals) | "
        f"{t.get('token_coverage_exact_pct', UNKNOWN)}% | "
        f"{t.get('rows_measured_lower_bound', 0)} launch(es) are lower bounds |")
    add(f"| API price ESTIMATE (USD) | {t.get('api_price_estimate_usd', UNKNOWN)} | "
        f"{t.get('api_price_estimate_rows', 0)} row(s) reporting |")
    add(f"| money actually charged | {UNKNOWN} | not reported by any launched CLI |")
    add("")
    if t.get("unmeasured_reasons"):
        add("Why launches were unmeasured:")
        add("")
        for reason, n in sorted(t["unmeasured_reasons"].items()):
            add(f"- `{reason}` — {n} launch(es)")
        add("")

    add("## Accepted progress")
    add("")
    add(f"- independently accepted milestones: **{acc.get('independently_accepted_milestones', UNKNOWN)}**")
    add(f"- independently validated attempts: {acc.get('independently_validated_attempts', UNKNOWN)}")
    add(f"- attempts merely CLAIMING pass: {acc.get('attempts_claiming_pass', UNKNOWN)}")
    add(f"- claims never independently validated: "
        f"**{acc.get('claims_not_independently_validated', UNKNOWN)}**")
    add("")
    add("Denominator is `independently_validated`. A builder's own PASS is a "
        "claim, and dividing by claims understates what a result costs.")
    add("")

    add("## Cost per accepted milestone")
    add("")
    if per.get("status") == "DEFINED":
        add(f"- launches: {per.get('launches_per_accepted_milestone')}")
        add(f"- tokens: {per.get('tokens_per_accepted_milestone')} "
            f"(summed over {', '.join(per.get('tokens_summed_over') or []) or 'nothing'})")
        add(f"- wall time (s): {per.get('wall_time_s_per_accepted_milestone')}")
        add(f"- API price estimate (USD): "
            f"{per.get('api_price_estimate_usd_per_accepted_milestone')}")
        add(f"- includes: {per.get('includes')}")
        add(f"- caveat: {per.get('caveat')}")
    else:
        add(f"**{per.get('status', UNKNOWN)}** — {per.get('note', '')}")
        add("")
        add(f"- burned launches: {per.get('burned_launches', UNKNOWN)}")
        add(f"- burned wall time (s): {per.get('burned_wall_time_s', UNKNOWN)}")
        add(f"- burned tokens: {_tok(per.get('burned_tokens_exact'))}")
    add("")

    np = s.get("non_progress", {})
    add("## Non-progress")
    add("")
    add(f"- attempts: {np.get('attempts', UNKNOWN)}")
    add(f"- attempts with no validated delta: "
        f"{np.get('attempts_without_validated_delta', UNKNOWN)}")
    add(f"- consumption without material change: "
        f"{np.get('consumption_without_material_change_pct', UNKNOWN)}%")
    add(f"- consecutive trailing attempts without a delta: "
        f"{np.get('consecutive_trailing_attempts_without_delta', UNKNOWN)}")
    add("")

    cr = report.get("crash_and_recovery", {})
    add("## Crashes, timeouts, recovery")
    add("")
    add(f"- lost processes reconciled: {cr.get('lost_processes_reconciled', UNKNOWN)}")
    add(f"- killed: {cr.get('killed_attempts', UNKNOWN)} · "
        f"timed out: {cr.get('timed_out_attempts', UNKNOWN)} · "
        f"quota-terminated: {cr.get('quota_terminated_attempts', UNKNOWN)} · "
        f"provider failures: {cr.get('provider_failure_attempts', UNKNOWN)}")
    add(f"- recovery latency (s): {cr.get('recovery_latency_s', UNKNOWN)}")
    add("")

    flying = report.get("in_flight_attempts", [])
    add("## Running now")
    add("")
    if not flying:
        add("No product attempt is OPEN and no tool/CTO job is mid-flight.")
    else:
        add("| kind | id | role | engine | age (s) | pid |")
        add("| --- | --- | --- | --- | --- | --- |")
        for r in flying[:25]:
            add(f"| {r.get('kind', '?')} | `{r['attempt_key'][:32]}` | "
                f"{r.get('role', '?')} | {r['engine']} | {r['age_s']} | "
                f"{r.get('pid')} |")
        add("")
        add("`PRODUCT_ATTEMPT` rows are OPEN mission attempts; "
            "`TOOL_OR_CTO_JOB` rows are launcher processes with no terminal "
            "record yet. Both spend quota while they run.")
    add("")

    add("## Quota windows (provider-reported only)")
    add("")
    windows = (s.get("quota_windows") or {}).get("windows") or {}
    if not windows:
        add(f"No provider reported a quota window in this data. "
            f"Remaining quota is {UNKNOWN} — which is not the same as unlimited.")
    else:
        add("| scope | status | utilisation | freshness | observed at | dated by |")
        add("| --- | --- | --- | --- | --- | --- |")
        for scope, w in sorted(windows.items()):
            add(f"| {scope} | {w['status']} | {w['utilisation']} | "
                f"{w.get('freshness', UNKNOWN)} | {w['observed_at']} | "
                f"{w.get('observed_at_source', UNKNOWN)} |")
        add("")
        add("Ordered by the provider's own observation time, not by the order "
            "files happened to be read. An EXPIRED row describes a window that "
            "has since rolled over — it is history, not the current balance.")
    add("")
    root_quota = (s.get("unobservable") or {}).get(
        "root_operator_chat_quota", UNKNOWN)
    suffix = (" (`UNOBSERVABLE_FROM_SUPERVISOR`)"
              if root_quota == UNKNOWN else "")
    add(f"Root operator chat quota: **{root_quota}**{suffix}.")
    add("")

    for title, key in (("By engine", "by_engine"), ("By model", "by_model"),
                       ("By role", "by_role"), ("By lineage", "by_lineage"),
                       ("By project", "by_project"),
                       ("By milestone", "by_milestone"),
                       ("By provenance (live vs reconstructed)", "by_provenance")):
        group = s.get(key) or {}
        if not group:
            continue
        add(f"## {title}")
        add("")
        add("| key | launches | wall s | tokens (exact) | coverage | exits |")
        add("| --- | --- | --- | --- | --- | --- |")
        for name, b in group.items():
            add(f"| {name} | {b['launches']} | {b['wall_time_s']} | "
                f"{_tok(b['tokens_exact'])} | {b['token_coverage_pct']}% | "
                f"{_short(b['exit_classes'])} |")
        add("")

    over = s.get("cto_overhead") or {}
    add("## CTO overhead (counted separately)")
    add("")
    add(over.get("note", ""))
    add("")
    add(f"- launches: {over.get('launches', UNKNOWN)} · "
        f"tokens: {_tok(over.get('tokens_exact'))} · "
        f"coverage: {over.get('token_coverage_pct', UNKNOWN)}%")
    add("")

    inv = report.get("coverage_inventory")
    if inv:
        add("## Launch-path coverage inventory")
        add("")
        add(f"{inv['instrumented']}/{inv['provider_calling_paths']} "
            f"provider-calling paths instrumented "
            f"({inv['coverage_gaps']} gap(s), {inv['unobservable']} unobservable).")
        add("")
        add("| status | engine | site | usage format |")
        add("| --- | --- | --- | --- |")
        for row in inv.get("paths", []):
            add(f"| {row['status']} | {row['engine']} | `{row['site']}` | "
                f"{row.get('usage_format', '')} |")
        add("")
        gaps = [r for r in inv.get("paths", []) if r["status"] == "COVERAGE_GAP"]
        if gaps:
            add("### Open gaps")
            add("")
            for r in gaps:
                add(f"- **{r['engine']}** at `{r['site']}`: {r.get('detail', '')}")
            add("")

    prov = report.get("provenance_split") or {}
    add("## Data provenance")
    add("")
    for k, v in sorted(prov.items()):
        if k == "backfill_note":
            continue
        add(f"- `{k}`: {v} row(s)")
    if prov.get("backfill_note"):
        add("")
        add(f"> {prov['backfill_note']}")
    add("")
    return "\n".join(lines) + "\n"


def _tok(value) -> str:
    """Render a token breakdown without letting an UNKNOWN field read as 0.

    A field no provider reported is printed as `?`, and it is excluded from the
    headline sum — which is why the sum is labelled a sum of the fields that
    were reported, not of the tokens that were spent.
    """
    if not isinstance(value, dict):
        return str(value)
    known = {k: v for k, v in value.items() if isinstance(v, (int, float))}
    missing = sorted(k for k in value if k not in known)
    total = sum(known.values())
    parts = ", ".join(f"{k.replace('_tokens', '')}={v}"
                      for k, v in sorted(known.items()) if v)
    out = f"{total} ({parts})" if parts else str(total)
    if missing:
        out += " · unreported: " + ", ".join(
            k.replace("_tokens", "") for k in missing)
    return out


def _short(exits) -> str:
    if not isinstance(exits, dict) or not exits:
        return "—"
    return " ".join(f"{k}:{v}" for k, v in sorted(exits.items()))


# ── CLI ──────────────────────────────────────────────────────────────────────
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python3 -m factory_v2.telemetry",
        description="production measurement for every AI call the Factory makes")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("report", help="JSON + Markdown snapshot")
    r.add_argument("--project-dir", action="append", default=[],
                   help="project directory holding evidence/ (repeatable)")
    r.add_argument("--backfill-glob", action="append", default=[],
                   help="glob of existing engine logs to ingest, labelled "
                        "BACKFILL_BEST_EFFORT (repeatable). Prefix with "
                        "'LINEAGE=' to set this glob's lineage, e.g. "
                        "'PRIMARY=~/.cache/factory-*/**/stdout.jsonl'")
    r.add_argument("--backfill-lineage", default="CTO_OVERHEAD",
                   help="lineage for backfilled rows (default CTO_OVERHEAD)")
    r.add_argument("--backfill-limit", type=int, default=0)
    r.add_argument("--codex-operator-sessions", default="",
                   help="workspace path; ingest Codex rollout logs whose OWN "
                        "session cwd is that workspace, as CTO_OVERHEAD. "
                        "Sessions with any other cwd are skipped untouched.")
    r.add_argument("--json-out", default="")
    r.add_argument("--md-out", default="")
    r.add_argument("--quiet", action="store_true")

    c = sub.add_parser("coverage", help="launch-path coverage inventory only")
    c.add_argument("--json", action="store_true")

    p = sub.add_parser("parse", help="parse one engine log and print its usage")
    p.add_argument("log")
    p.add_argument("--format", default="")

    rc = sub.add_parser(
        "receipt",
        help="publish RESOURCE_TELEMETRY_RECEIPT.json for the milestone verifier")
    rc.add_argument("--project-dir", action="append", default=[], required=True)
    rc.add_argument("--out", default="factory_v2/receipts/"
                                     "RESOURCE_TELEMETRY_RECEIPT.json")

    args = ap.parse_args(argv)

    if args.cmd == "coverage":
        inv = inventory.summary()
        if args.json:
            print(json.dumps(inv, indent=2, sort_keys=True))
            return 0
        for row in inv["paths"]:
            print(f"{row['status']:<18} {row['engine']:<12} {row['site']}")
        print(f"\n{inv['instrumented']}/{inv['provider_calling_paths']} "
              f"provider-calling paths instrumented; "
              f"{inv['coverage_gaps']} gap(s), {inv['unobservable']} unobservable")
        # A gap is not an error — it is a fact this command exists to surface.
        return 0

    if args.cmd == "parse":
        fmt = args.format or _sniff(Path(args.log))
        parsed = capture.parse_log_file(args.log, fmt=fmt)
        print(json.dumps({
            "format": fmt or UNKNOWN,
            "completeness": parsed.usage.completeness,
            "usage": {k: getattr(parsed.usage, k)
                      for k in ("input_tokens", "output_tokens",
                                "cache_read_tokens", "cache_write_tokens",
                                "reasoning_tokens", "provider_requests",
                                "model_turns", "context_window",
                                "context_utilisation")},
            "caveats": parsed.usage.caveats,
            "api_price_estimate_usd": parsed.money.api_price_estimate_usd,
            "session_id": parsed.session_id,
        }, indent=2, sort_keys=True, default=str))
        return 0

    if args.cmd == "receipt":
        # The receipt publishes the aggregates AND the ledgers they came from.
        # Without project_dirs the verifier has nothing to recompute against, so
        # it would fail closed — which is the correct outcome for a number that
        # arrives without its evidence.
        report = build_report(args.project_dir)
        payload = {
            "schema": "resource-telemetry-receipt/v1",
            "generated_at": utcnow(),
            "project_dirs": [str(p) for p in args.project_dir],
            "summary": report["summary"],
            "coverage_inventory": report.get("coverage_inventory"),
            # PUBLISHED, not commented. The verifier's own docstring claimed the
            # forgeability residual was "named in known_unknowns"; no such field
            # existed anywhere, so the one limit that matters most was documented
            # only where nobody settling a milestone would read it.
            "known_unknowns": KNOWN_UNKNOWNS,
            "reconstruction": (
                "every number in `summary` is recomputed from the append-only "
                "telemetry and attempt ledgers under `project_dirs` by "
                "factory_v2.product_verifiers RESOURCE_TELEMETRY_TRUE_COST; a "
                "figure that does not reconstruct fails the milestone closed"),
        }
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2, sort_keys=True,
                                  default=str) + "\n", encoding="utf-8")
        print(f"wrote {out}")
        return 0

    extra = []
    for pattern in args.backfill_glob:
        # `LINEAGE=glob` lets one invocation ingest operator sessions and
        # production mission captures without merging them: they are different
        # kinds of spend and the report keeps them apart.
        lineage = args.backfill_lineage
        window = "CURRENT_CAMPAIGN"
        if "=" in pattern and pattern.split("=", 1)[0].isupper():
            head, pattern = pattern.split("=", 1)
            # `LINEAGE@WINDOW=glob` — window defaults to the current campaign,
            # so importing an old corpus has to say so explicitly.
            lineage, _, win = head.partition("@")
            window = win or window
        extra.extend(backfill(_glob(pattern), lineage=lineage, window=window,
                              limit=args.backfill_limit))
    operator_scan = {}
    if args.codex_operator_sessions:
        found = codex_sessions_for_workspace(args.codex_operator_sessions)
        # The current root session is the chat driving this campaign, so its usage
        # is attributable — but what the rollout holds is that session's CUMULATIVE
        # lifetime, which is not the same as this campaign. It gets its own window.
        extra.extend(backfill(found["current_root"], lineage="CTO_OVERHEAD",
                              fmt=adapters.CODEX_JSONL,
                              window="CURRENT_ROOT_SESSION_CUMULATIVE",
                              limit=args.backfill_limit))
        # Every other session sharing this directory is earlier work this report
        # cannot identify. Real spend, wrong campaign.
        extra.extend(backfill(found["matched"], lineage="CTO_OVERHEAD",
                              fmt=adapters.CODEX_JSONL,
                              window="WORKSPACE_HISTORY",
                              limit=args.backfill_limit))
        operator_scan = {
            "workspace": str(args.codex_operator_sessions),
            "current_root_session_id": found.get("current_root_session_id") or UNKNOWN,
            "current_root_session_files": len(found["current_root"]),
            "workspace_history_sessions": len(found["matched"]),
            "scope_rule": ("a cwd identifies the WORKSPACE, not this campaign. Only "
                           "the session whose id matches CODEX_SESSION_ID is the "
                           "current root chat; its window is that session's "
                           "cumulative lifetime, NOT this campaign. Every other "
                           "cwd-matching session is WORKSPACE_HISTORY."),
            "known_limitation": ("no per-task baseline exists for the root chat, so "
                                 "its lifetime total cannot be split into this "
                                 "campaign's share. Measuring from an activation "
                                 "baseline forward is the fix; inventing a split is "
                                 "not."),
            "codex_sessions_matched_this_workspace": len(found["matched"]),
            "codex_sessions_skipped_other_cwd": found["skipped_other_cwd"],
            "codex_session_files_unreadable": found["unreadable"],
            "basis": "session_meta.payload.cwd equals this workspace",
            "not_checked": ("Codex sessions whose cwd is anywhere else, and any "
                            "operator usage that never touched this workspace — "
                            "those remain UNOBSERVABLE by scope, not by absence"),
        }

    report = build_report(args.project_dir, extra_observations=extra)
    if operator_scan:
        report["operator_codex_scan"] = operator_scan

    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(
            json.dumps(report, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8")
    if args.md_out:
        Path(args.md_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.md_out).write_text(to_markdown(report), encoding="utf-8")
    if not args.quiet and not (args.json_out or args.md_out):
        print(to_markdown(report))
    elif not args.quiet:
        wrote = " ".join(p for p in (args.json_out, args.md_out) if p)
        print(f"wrote {wrote}")
    return 0


def _glob(pattern: str) -> list:
    from glob import glob
    return [Path(p) for p in glob(pattern, recursive=True) if Path(p).is_file()]


if __name__ == "__main__":
    sys.exit(main())
