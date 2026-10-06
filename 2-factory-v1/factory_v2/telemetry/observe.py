"""The capture→account bridge: one supervisor observation of one launch.

`observe_launch()` is what a launch site calls. It takes what the SUPERVISOR saw
(argv, pid, the two clock readings, rc) plus whatever the engine printed, picks
the adapter from argv, and returns a `LaunchObservation`.

Three properties the call sites depend on:

  * **It never raises.** A launch site is on the path that spends quota. A
    metrics bug there must degrade the report, not kill the mission — and must
    not silently pass either: every failure becomes a typed unknown with a
    reason, which shows up as missing coverage in the report.
  * **It never sees content.** The engine's stdout is consumed as a stream of
    counters. argv is redacted before it is stored. Nothing that could hold a
    prompt, a file, a secret or a chain of thought reaches the record.
  * **It is idempotent.** The observation key prefers the provider's session id,
    so a resumed session folds onto its earlier row instead of adding a second
    launch's tokens to the totals.

`to_engine_execution()` projects the rich record down to the flat dict the
existing `launch_gate._project_observed` already knows how to read. That is the
seam: the new measurement reaches `AttemptAccount` without the accounting module
learning a new vocabulary, and without a second writer appearing on the ledger.
"""
from __future__ import annotations

from . import adapters, capture
from .record import (LaunchObservation, NO_LOG_CAPTURED, Usage,
                     is_measured, observation_key, redact_argv, utcnow,
                     value_of)

#: Lineages. `PRIMARY` is the build a milestone is trying to land; everything
#: else is work done ABOUT that build. They are all real cost and all counted —
#: separately, so "what did an accepted change cost" can include reviews and
#: repairs without any launch being added twice.
PRIMARY = "PRIMARY"
REVIEW = "REVIEW"
REPAIR = "REPAIR"
TEST = "TEST"
RECOVERY = "RECOVERY"
CTO_OVERHEAD = "CTO_OVERHEAD"

LINEAGES = frozenset({PRIMARY, REVIEW, REPAIR, TEST, RECOVERY, CTO_OVERHEAD})

#: Which canonical engine an argv0 belongs to, and which provider bills it.
_PROVIDER = {"claude": "anthropic", "glm": "zai", "opencode": "zai",
             "codex": "openai", "zcode": "zai", "local": "none"}


def observe_launch(*, argv, stdout=None, log_path: str = "",
                   started_at: str = "", ended_at: str = "",
                   wall_time_s=None, rc=None, exit_class: str = "",
                   engine: str = "", model_requested: str = "",
                   role: str = "", lineage: str = PRIMARY,
                   run_id: str = "", project: str = "", milestone: str = "",
                   mission: str = "", attempt: int = 0, launch_seq: int = 0,
                   attempt_key: str = "",
                   contract_id: str = "", parent_observation_key: str = "",
                   control_sha: str = "", product_sha: str = "",
                   task_type: str = "", spec_level: str = "",
                   pid=None, phase_times_s=None, retries: int = 0,
                   killed_by_signal=None, provenance: str = "LIVE",
                   difficulty_proxy=None) -> LaunchObservation:
    """Build one observation. Returns a record even when nothing was measured."""
    argv = list(argv or [])
    fmt = adapters.detect_format(argv)
    clean_exit = (rc == 0) if isinstance(rc, int) else None

    try:
        if stdout is not None:
            parsed = adapters.parse(fmt, stdout, clean_exit=clean_exit)
        elif log_path:
            parsed = capture.parse_log_file(log_path, fmt=fmt,
                                            clean_exit=clean_exit)
        else:
            parsed = adapters._absent(
                NO_LOG_CAPTURED,
                "the launch site captured no engine output for this launch")
    except Exception as exc:                    # pragma: no cover - defensive
        # A parser defect must not propagate into a dispatch. It becomes a
        # legible hole in the coverage numbers instead.
        parsed = adapters._absent(
            "LOG_UNPARSEABLE", f"{type(exc).__name__}: {exc}")

    engine = engine or _engine_from_argv(argv)
    usage: Usage = parsed.usage
    usage.time_to_first_event_s = _pick_ttfe(usage, parsed, started_at)

    obs = LaunchObservation(
        # Keyed on the SUPERVISOR's identity for this execve, not on the
        # provider session: one `--resume` session is many billed launches.
        observation_key=observation_key(
            engine=engine, session_id=parsed.session_id,
            attempt_key=attempt_key, step=task_type, pid=pid,
            started_at=started_at, launch_seq=launch_seq),
        engine=engine or "UNKNOWN",
        provider=_PROVIDER.get(engine, "UNKNOWN"),
        role=role or "UNKNOWN",
        run_id=run_id, project=project, milestone=milestone, mission=mission,
        attempt=attempt, launch_seq=launch_seq, attempt_key=attempt_key,
        contract_id=contract_id,
        parent_observation_key=parent_observation_key,
        lineage=lineage if lineage in LINEAGES else PRIMARY,
        model_requested=model_requested,
        model_observed=parsed.model_observed or "",
        harness_version=parsed.harness_version or "",
        control_sha=control_sha, product_sha=product_sha,
        argv_redacted=redact_argv(argv),
        pid=pid, cwd=parsed.cwd or "",
        started_at=started_at or "", ended_at=ended_at or utcnow(),
        wall_time_s=_wall(wall_time_s),
        api_time_s=parsed.api_time_s,
        phase_times_s=dict(phase_times_s or {}),
        rc=rc, exit_class=exit_class,
        stop_reason=parsed.stop_reason, terminal_reason=parsed.terminal_reason,
        killed_by_signal=killed_by_signal,
        retries=retries, rate_limit_waits=parsed.rate_limit_waits,
        task_type=task_type,
        difficulty_proxy=_difficulty(difficulty_proxy, spec_level),
        usage=usage, money=parsed.money, quota=parsed.quota,
        raw_usage_events=parsed.raw_events,
        provenance=provenance, log_path=str(log_path or ""),
        session_id=parsed.session_id,
    )
    return obs


def _engine_from_argv(argv) -> str:
    if not argv:
        return ""
    import os
    head = os.path.basename(str(argv[0]))
    return {"opencode": "glm", "claude": "claude", "codex": "codex",
            "zcode": "glm", "bash": "local"}.get(head, "")


def _wall(wall_time_s):
    from .record import measured, unknown
    if isinstance(wall_time_s, bool) or not isinstance(wall_time_s, (int, float)):
        return unknown(NO_LOG_CAPTURED,
                       detail="the launch site did not time this launch")
    if wall_time_s < 0:
        return unknown("LOG_UNPARSEABLE", detail="negative duration")
    return measured(round(float(wall_time_s), 3), unit="s",
                    source="supervisor.monotonic_clock")


def _pick_ttfe(usage: Usage, parsed, started_at: str):
    """Prefer an interval the adapter could close itself; else close it here."""
    if is_measured(usage.time_to_first_event_s):
        return usage.time_to_first_event_s
    return adapters.time_to_first_event(started_at,
                                        getattr(parsed, "first_event_at", ""))


def _difficulty(explicit, spec_level: str) -> dict:
    """A difficulty PROXY, labelled as a heuristic in the data itself.

    Spec level is an authored routing input, not a measurement of how hard the
    task turned out to be. It is useful for grouping and useless as a score, so
    it travels with `heuristic: True` attached and no numeric rank.
    """
    out = {"heuristic": True, "basis": "authored spec_level from the contract",
           "not_a_measurement": True}
    if spec_level:
        out["spec_level"] = spec_level
    if isinstance(explicit, dict):
        out.update({k: v for k, v in explicit.items() if k != "heuristic"})
        out["heuristic"] = True
    return out


# ── projection down to the existing accounting vocabulary ────────────────────
def to_engine_execution(obs: LaunchObservation, *, base: dict = None) -> dict:
    """Flat dict for `launch_gate._project_observed`, plus the new fields.

    Only MEASURED numbers are written. An unknown is left out entirely rather
    than passed as 0 — `_project_observed` copies numbers and ignores absences,
    so omission is what keeps `AttemptAccount` honest.
    """
    ee = dict(base or {})
    ee.update({
        "engine_id": obs.engine if obs.engine != "UNKNOWN" else ee.get("engine_id", ""),
        "model": obs.model_observed or obs.model_requested or ee.get("model", ""),
        "telemetry_observation_key": obs.observation_key,
        "usage_source": obs.usage.source or "NONE",
        "usage_completeness": obs.usage.completeness,
        "provider_session_id": obs.session_id,
        "lineage": obs.lineage,
    })
    if obs.pid:
        ee.setdefault("pid", obs.pid)
    if obs.argv_redacted:
        ee["argv"] = obs.argv_redacted
    for dst, slot in (("input_tokens", obs.usage.input_tokens),
                      ("output_tokens", obs.usage.output_tokens),
                      ("cache_read_tokens", obs.usage.cache_read_tokens),
                      ("cache_write_tokens", obs.usage.cache_write_tokens),
                      ("reasoning_tokens", obs.usage.reasoning_tokens),
                      ("provider_requests", obs.usage.provider_requests),
                      ("model_turns", obs.usage.model_turns)):
        v = value_of(slot)
        if v is not None:
            ee[dst] = v
    est = value_of(obs.money.api_price_estimate_usd)
    if est is not None:
        # Named `api_price_estimate_usd`, never `cost_usd`: under a flat
        # subscription this number is what the tokens WOULD list-price at, and
        # nobody is charged it.
        ee["api_price_estimate_usd"] = est
    if obs.quota.scope and obs.quota.scope != "UNKNOWN":
        ee["quota_scope"] = obs.quota.scope
        ee["quota_status"] = obs.quota.status
        ee["quota_observed_at"] = obs.quota.observed_at
        util = value_of(obs.quota.utilisation)
        if util is not None:
            ee["quota_utilisation"] = util
        resets = value_of(obs.quota.resets_at)
        if resets is not None:
            ee["quota_resets_at"] = resets
    return ee
