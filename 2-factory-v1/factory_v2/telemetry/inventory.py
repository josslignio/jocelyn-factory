"""Coverage inventory — every launch path the Factory actually has.

The failure mode this file exists to prevent: a telemetry report that looks
complete because the paths it cannot see are simply absent from it. A launch
site that is not instrumented has to be VISIBLE as a named gap; silence is the
one outcome that is not allowed.

The Claude/GLM/Codex rows are not authored. `probe()` imports the production
argv builder (`factory/bin/model_router.cli_args`) and asks the real adapter
registry what it could parse from the bytes that builder produces. So the
inventory recomputes itself: if someone adds `--format json` to the opencode
launch tomorrow, GLM flips from GAP to INSTRUMENTED here with no edit to this
file — and if someone removes `--output-format json` from Claude, the inventory
turns red on its own.

Rows whose launch is not a model call at all (bash steps, the Python role
runners audit_loop spawns) are typed NO_PROVIDER_CALL rather than quietly
dropped: "nothing to measure" and "we failed to measure" must not look alike.
"""
from __future__ import annotations

import sys
from pathlib import Path

from . import adapters
from .record import redact_argv

_REPO_ROOT = Path(__file__).resolve().parents[2]

# status values
INSTRUMENTED = "INSTRUMENTED"
GAP = "COVERAGE_GAP"
NO_PROVIDER_CALL = "NO_PROVIDER_CALL"
UNOBSERVABLE = "UNOBSERVABLE"
#: argv + adapter are in place, but nothing in the current runtime routes here.
#: Ready, not live. Kept distinct from INSTRUMENTED so the coverage fraction
#: describes launches that can actually happen.
INSTRUMENTED_UNREACHED = "INSTRUMENTED_NOT_REACHED"

#: WHERE a path's measurement ends up. A path whose argv requests a structured
#: stream is only half-instrumented if nothing downstream ever writes the row —
#: "the parser could read it" is not the same claim as "the number is on disk".
#: Every value here is asserted by a test that runs the consumer and reads back
#: the row, so this column cannot drift into aspiration.
PERSIST_PROJECT_LEDGER = "evidence/telemetry_observations.jsonl (project)"
PERSIST_BENCH_LEDGER = "evidence/telemetry_observations.jsonl (benchmark out_dir)"
PERSIST_VIA_RECEIPT = "dispatcher receipt -> runtime -> project ledger"
PERSIST_BACKFILL = "report-time backfill only (no durable row of its own)"
PERSIST_NONE = "nothing is written"

#: Engine launch sites that go through model_router.cli_args(). Probed live.
_ROUTED = [
    {"site": "factory/bin/dispatcher_v3.py::_run_engine",
     "engine": "claude", "provider": "anthropic", "role": "builder/repair",
     "persists_to": PERSIST_VIA_RECEIPT,
     "note": "the mission build/repair path — the one that spends the most quota"},
    {"site": "factory/bin/dispatcher_v3.py::_run_engine",
     "engine": "glm", "provider": "zai", "role": "builder/repair",
     "persists_to": PERSIST_VIA_RECEIPT,
     "note": "GLM/Z Code via opencode"},
    {"site": "factory/bin/dispatcher_v3.py::_run_engine",
     "engine": "codex", "provider": "openai", "role": "reviewer",
     "persists_to": PERSIST_VIA_RECEIPT,
     "note": "read-only adversarial review"},
    {"site": "factory_v2/cli_canary.py::run_canary",
     "engine": "claude", "provider": "anthropic", "role": "canary",
     "persists_to": PERSIST_PROJECT_LEDGER,
     "note": "reuses model_router.cli_args, so it inherits the same coverage"},
    {"site": "factory_v2/cli_canary.py::run_canary",
     "engine": "glm", "provider": "zai", "role": "canary",
     "persists_to": PERSIST_PROJECT_LEDGER,
     "note": "reuses model_router.cli_args"},
    {"site": "factory_v2/review_dispatch.py::dispatch_review",
     "engine": "claude", "provider": "anthropic", "role": "reviewer",
     "persists_to": PERSIST_PROJECT_LEDGER,
     "note": "review runner is wrapped by metered_launch and persists parsed stdout"},
    {"site": "factory_v2/review_dispatch.py::dispatch_review",
     "engine": "glm", "provider": "zai", "role": "reviewer",
     "persists_to": PERSIST_PROJECT_LEDGER,
     "note": "review runner is wrapped by metered_launch and persists parsed stdout"},
    {"site": "factory_v2/review_dispatch.py::dispatch_review",
     "engine": "codex", "provider": "openai", "role": "reviewer",
     "persists_to": PERSIST_PROJECT_LEDGER,
     "note": "opt-in restricted review runner is metered and persisted"},
]

#: Launch sites that do not build argv through model_router.
_FIXED = [
    {"site": "factory/bin/benchmark.py::_run_claude",
     "engine": "claude", "provider": "anthropic", "role": "benchmark",
     "argv": ["claude", "-p", "--output-format", "json"],
     "persists_to": PERSIST_BENCH_LEDGER,
     "note": "benchmark solo arm; writes its row into the benchmark out_dir"},
    {"site": "factory_v2/executors.py::AutopilotExecutor.execute",
     "engine": "dispatcher", "provider": "none", "role": "wrapper",
     "argv": [sys.executable, "factory/bin/dispatcher_v3.py"],
     "kind": NO_PROVIDER_CALL,
     "note": "spawns the dispatcher, which spawns the engines; usage is "
             "attributed to the engine rows above via the dispatcher receipt"},
    {"site": "factory_v2/executors.py::LocalCommandExecutor.execute",
     "engine": "local", "provider": "none", "role": "test/probe",
     "argv": ["bash", "-c"], "kind": NO_PROVIDER_CALL,
     "note": "deterministic shell step; wall time only, no provider tokens"},
    {"site": "factory_v2/audit_loop.py::run_challenger",
     "engine": "local", "provider": "none", "role": "challenger",
     "argv": [sys.executable, "-I", "factory_v2/cell_challenger.py"],
     "kind": NO_PROVIDER_CALL,
     "note": "Python role runner, not a model call"},
    {"site": "factory_v2/audit_loop.py::run_reviewer",
     "engine": "local", "provider": "none", "role": "reviewer",
     "argv": [sys.executable, "-I", "factory_v2/cell_reviewer.py"],
     "kind": NO_PROVIDER_CALL,
     "note": "Python role runner, not a model call"},
]

#: Consumption that exists, is real, and is structurally invisible from here.
#: Reporting it as UNOBSERVABLE is the whole point: a zero would be a lie, and
#: an omission would read as "there is none".
_OUT_OF_BAND = [
    {"site": "root operator Codex/CTO chat session (cwd = this workspace)",
     "engine": "codex", "provider": "openai", "role": "cto_pilot",
     "kind": INSTRUMENTED,
     "persists_to": PERSIST_BACKFILL,
     "note": "CORRECTED: this was reported UNOBSERVABLE on the assumption that "
             "the operator's chat emitted nothing readable. It does. The Codex "
             "CLI records every session under ~/.codex/sessions with "
             "token_count usage and rate_limits, and `session_meta.payload.cwd` "
             "binds a session to a workspace unambiguously. Sessions whose cwd "
             "is THIS workspace are ingested as CTO_OVERHEAD through the same "
             "codex adapter; only the first line of any other session is read, "
             "and only to test its cwd. An assumption was standing in for a "
             "check that cost nothing to run."},
    {"site": "root operator Codex/CTO chat sessions with any other cwd",
     "engine": "codex", "provider": "openai", "role": "cto_pilot",
     "kind": UNOBSERVABLE,
     "note": "out of scope by choice, not by absence: those logs exist and are "
             "readable, and importing them would be a broad sweep of personal "
             "history with no bearing on this campaign. Reported UNOBSERVABLE "
             "FOR THIS CAMPAIGN, with the count of sessions skipped, so the "
             "boundary is visible rather than implied."},
    {"site": "lead/research/reviewer Claude & GLM CLI sessions (this workspace)",
     "engine": "claude|glm", "provider": "anthropic|zai", "role": "cto_overhead",
     "kind": INSTRUMENTED,
     "persists_to": PERSIST_BACKFILL,
     "note": "external process logs (.context/**/*.jsonl) ARE readable and are "
             "ingested as CTO_OVERHEAD lineage — counted separately from "
             "production mission attempts, never mixed into them."},
]


def _routed_argv(engine: str):
    """Ask the REAL production argv builder what it launches for this engine."""
    bin_dir = str(_REPO_ROOT / "factory" / "bin")
    if bin_dir not in sys.path:
        sys.path.insert(0, bin_dir)
    import model_router as _mr
    choice = _mr.EngineChoice(
        engine=engine, model=_mr._ENGINE_META[engine]["model"], cli=engine,
        role="builder", spec_level="B", task_kind="build",
        reason="coverage probe", read_only=False)
    return _mr.cli_args(choice, _REPO_ROOT, "<prompt>")


#: The (spec_level, task_kind) pairs dispatcher_v3 ACTUALLY asks route() for.
#: It calls _route_and_record("build") and _route_and_record("repair", stall);
#: it never requests review, plan, diagnose or replan. So an engine that only
#: wins those task kinds has a routing entry and no caller.
_DISPATCHER_ROUTE_REQUESTS = [(sl, tk, stall)
                              for sl in ("S", "A", "B")
                              for tk in ("build", "repair")
                              for stall in (0, 1, 2)]


def dispatcher_reachability(engine: str) -> dict:
    """Can dispatcher_v3 actually route to this engine? Computed, not claimed.

    Enumerates every routing question the dispatcher really asks — including
    with the engine forced as a hint — and reports whether any of them yields
    this engine. This exists because the inventory previously listed
    `dispatcher_v3::_run_engine [codex]` as a live instrumented path when no
    dispatcher call can produce a codex launch at all: the ActorCapabilityPolicy
    denies codex both build and repair, and those are the only task kinds the
    dispatcher requests. An instrumented path nobody can reach is not coverage,
    and saying otherwise inflates the denominator.
    """
    bin_dir = str(_REPO_ROOT / "factory" / "bin")
    if bin_dir not in sys.path:
        sys.path.insert(0, bin_dir)
    try:
        import model_router as _mr
    except Exception as exc:                       # pragma: no cover - defensive
        return {"reachable": None, "detail": f"router unimportable: {exc}"}
    hit_default, hit_hint = [], []
    for sl, tk, stall in _DISPATCHER_ROUTE_REQUESTS:
        try:
            if _mr.route(sl, tk, stall).engine == engine:
                hit_default.append(f"{sl}/{tk}@stall{stall}")
            if _mr.route(sl, tk, stall, engine_hint=engine).engine == engine:
                hit_hint.append(f"{sl}/{tk}@stall{stall}")
        except Exception:
            continue
    if hit_default:
        return {"reachable": True, "via": "routing table",
                "examples": hit_default[:4]}
    if hit_hint:
        return {"reachable": True, "via": "engine_hint (escalation ladder)",
                "examples": hit_hint[:4]}
    return {
        "reachable": False,
        "detail": ("no dispatcher route yields this engine: dispatcher_v3 only "
                   "requests task_kind build/repair, and the capability policy "
                   "denies this engine both. Its routing entries (review/plan) "
                   "have no caller in the current runtime."),
    }


def probe() -> list:
    """Recompute the coverage of every launch path, now."""
    rows = []
    for spec in _ROUTED:
        row = dict(spec)
        try:
            argv = _routed_argv(spec["engine"])
            row["argv"] = redact_argv(argv)
            fmt = adapters.detect_format(argv)
        except Exception as exc:
            row["argv"] = []
            row["status"] = GAP
            row["usage_format"] = adapters.UNSTRUCTURED
            row["gap_reason"] = "LAUNCH_PATH_UNINSTRUMENTED"
            row["detail"] = f"argv could not be recomputed: {type(exc).__name__}: {exc}"
            rows.append(row)
            continue
        row["usage_format"] = fmt
        if "dispatcher_v3" in spec["site"]:
            row["reachability"] = dispatcher_reachability(spec["engine"])
        if fmt == adapters.UNSTRUCTURED:
            row["status"] = GAP
            row["gap_reason"] = "NOT_EMITTED_BY_INVOCATION"
            row["detail"] = (
                f"the production argv for {spec['engine']} requests no "
                f"machine-readable usage output, so token counts, cost and "
                f"quota are UNKNOWN for every launch on this path. This is our "
                f"invocation, not a provider limit: the fix is an argv change "
                f"({'--format json' if spec['engine'] == 'glm' else 'a structured output mode'}), "
                f"which is a production routing change and is NOT made here.")
        else:
            row["status"] = INSTRUMENTED
            row["detail"] = f"usage parsed by adapters.{fmt}"
            reach = row.get("reachability")
            if reach is not None and reach.get("reachable") is False:
                # Instrumented and unreachable. Neither a gap (nothing is being
                # missed) nor live coverage (nothing flows). Counting it as
                # either would misstate the denominator, so it gets its own
                # status and is excluded from the coverage fraction below.
                row["status"] = INSTRUMENTED_UNREACHED
                row["detail"] = (
                    f"argv and adapter are ready ({fmt}), but no current "
                    f"dispatcher route reaches this engine — "
                    + reach.get("detail", ""))
        rows.append(row)

    for spec in _FIXED:
        row = dict(spec)
        kind = row.pop("kind", None)
        if kind == NO_PROVIDER_CALL:
            row["status"] = NO_PROVIDER_CALL
            row["usage_format"] = "n/a"
            row["detail"] = row.get("note", "")
        else:
            fmt = adapters.detect_format(row.get("argv") or [])
            row["usage_format"] = fmt
            row["status"] = INSTRUMENTED if fmt != adapters.UNSTRUCTURED else GAP
            if row["status"] == GAP:
                row["gap_reason"] = "NOT_EMITTED_BY_INVOCATION"
            row["detail"] = row.get("note", "")
        rows.append(row)

    for spec in _OUT_OF_BAND:
        row = dict(spec)
        row["status"] = row.pop("kind")
        row["usage_format"] = "n/a"
        row["detail"] = row.get("note", "")
        rows.append(row)
    return rows


def summary() -> dict:
    """Coverage as a number that cannot flatter itself.

    The denominator is provider-calling paths only. Bash steps and Python role
    runners are excluded because they have no tokens to miss — but they stay in
    `paths` so the exclusion is auditable rather than assumed.
    """
    rows = probe()
    provider_paths = [r for r in rows
                      if r["status"] in (INSTRUMENTED, GAP, UNOBSERVABLE)]
    instrumented = [r for r in provider_paths if r["status"] == INSTRUMENTED]
    gaps = [r for r in provider_paths if r["status"] == GAP]
    unobservable = [r for r in provider_paths if r["status"] == UNOBSERVABLE]
    unreached = [r for r in rows if r["status"] == INSTRUMENTED_UNREACHED]
    return {
        "schema": "telemetry/coverage-inventory/v2",
        "provider_calling_paths": len(provider_paths),
        "instrumented": len(instrumented),
        "coverage_gaps": len(gaps),
        "unobservable": len(unobservable),
        "instrumented_not_reached": len(unreached),
        "instrumented_fraction_of_provider_paths": (
            round(len(instrumented) / len(provider_paths), 3)
            if provider_paths else "UNKNOWN"),
        "gap_sites": [f"{r['site']} [{r['engine']}]" for r in gaps],
        "unobservable_sites": [r["site"] for r in unobservable],
        "not_reached_sites": [f"{r['site']} [{r['engine']}]" for r in unreached],
        "persistence_by_site": {
            f"{r['site']} [{r['engine']}]": r.get("persists_to", PERSIST_NONE)
            for r in rows
            if r["status"] in (INSTRUMENTED, INSTRUMENTED_UNREACHED)},
        "paths": rows,
        "note": ("denominator counts provider-calling paths that a current "
                 "caller can actually reach. NO_PROVIDER_CALL rows have no "
                 "provider usage to miss; INSTRUMENTED_NOT_REACHED rows are "
                 "ready but unreachable, so counting them as coverage would "
                 "credit launches that cannot occur. Both stay in `paths`."),
    }
