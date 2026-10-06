"""Provider adapters — one per FORMAT THE FACTORY ACTUALLY LAUNCHES.

Each adapter turns a real engine log into the common `Usage`/`Money`/`QuotaSnapshot`
vocabulary. They were written against recorded production output, not invented
fixtures:

  * ``claude -p --output-format json``   — the argv model_router builds today.
    One JSON object on stdout: the same payload as the stream's `result` event.
    Verified against .context/stabilization/CLI_CANARY_RECEIPT.json.
  * ``claude … --output-format stream-json`` — NDJSON, one event per line.
    Verified against ~/.cache/factory-app007/**/stdout.jsonl (real m01/m03 runs).
  * ``opencode run --format json``       — NDJSON of parts; usage rides on
    `step_finish`. Verified against .context/**/glm-*.jsonl.
  * ``opencode run`` / ``codex <prompt>`` — plain prose. Nothing to parse, and
    saying so is the point: NOT_EMITTED_BY_INVOCATION is a coverage gap with a
    name, not a zero.

Two hazards drove the shapes below, both measured rather than assumed:

1. **Deltas vs cumulative.** In a real 94-event stream-json log there were 31
   `assistant` events carrying only 15 distinct `message.id`s. Summing every
   event double-counted cache reads 1 577 157 against a true 781 970. Dedupe by
   message id, last write wins, and the reconstruction matches the `result`
   event exactly for input and cache.

2. **Output tokens are not reconstructible mid-stream.** With the same dedupe,
   output summed to 75 where the terminal `result` said 16 181: per-message
   usage snapshots are taken before the message finishes. So when a log has no
   terminal total — a crash, a kill, a truncated tail — output is published as a
   LOWER BOUND with `completeness=PARTIAL_FLOOR`, never as the count.
"""
from __future__ import annotations

import json

from .record import (ABSENT, COMPLETE, LOG_TRUNCATED, LOG_UNPARSEABLE,
                     NOT_EMITTED_BY_INVOCATION, NOT_EMITTED_BY_PROVIDER,
                     OBSERVED_ADJACENT_EVENT, OBSERVED_NONE,
                     OBSERVED_PROVIDER_EVENT, PARTIAL_FLOOR, Money,
                     QuotaSnapshot, Usage, allowlist_event, measured, unknown)

#: Hard ceiling on distinct in-flight message ids held while folding a stream.
#: A pathological or adversarial log cannot grow supervisor memory without bound;
#: passing it is recorded as a caveat rather than silently truncating the maths.
MAX_TRACKED_MESSAGES = 50_000

# format ids — these strings appear verbatim in every record's usage.source
CLAUDE_RESULT_JSON = "claude.result_json"
CLAUDE_STREAM_JSON = "claude.stream_json"
OPENCODE_JSON = "opencode.step_finish"
CODEX_JSONL = "codex.token_count"
#: codex >= 0.154 `exec --json`: usage rides on `turn.completed`, not `token_count`.
CODEX_TURN_COMPLETED = "codex.turn_completed"
UNSTRUCTURED = "unstructured"


class ParsedLog:
    """What one adapter extracted: usage, money, quota, and identity crumbs."""

    def __init__(self, *, usage: Usage, money: Money, quota: QuotaSnapshot,
                 session_id: str = "", model_observed: str = "",
                 harness_version: str = "", stop_reason: str = "",
                 terminal_reason: str = "", api_time_s=None,
                 rate_limit_waits: int = 0, raw_events=None, cwd: str = "",
                 first_event_at: str = ""):
        self.usage = usage
        self.money = money
        self.quota = quota
        self.session_id = session_id
        self.model_observed = model_observed
        self.harness_version = harness_version
        self.stop_reason = stop_reason
        self.terminal_reason = terminal_reason
        self.api_time_s = api_time_s
        self.rate_limit_waits = rate_limit_waits
        self.raw_events = list(raw_events or [])
        self.cwd = cwd
        #: Timestamp of the FIRST event the engine emitted, in the log's own
        #: format. The supervisor knows when it called execve; only it can turn
        #: this into an interval, so the adapter reports the instant and refuses
        #: to guess the origin.
        self.first_event_at = first_event_at


def _n(value):
    """A usable number, or None. Booleans are not numbers."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _absent(reason: str, detail: str = "") -> ParsedLog:
    return ParsedLog(
        usage=Usage(source=reason, completeness=ABSENT,
                    input_tokens=unknown(reason, detail=detail),
                    output_tokens=unknown(reason, detail=detail),
                    cache_read_tokens=unknown(reason, detail=detail),
                    cache_write_tokens=unknown(reason, detail=detail),
                    reasoning_tokens=unknown(reason, detail=detail),
                    total_tokens=unknown(reason, detail=detail),
                    provider_requests=unknown(reason, detail=detail)),
        money=Money(api_price_estimate_usd=unknown(reason, detail=detail)),
        quota=QuotaSnapshot(source=reason))


# ── claude: -p --output-format json (the production argv) ────────────────────
def _usage_from_claude_payload(payload: dict, *, source: str,
                               completeness: str) -> tuple:
    u = payload.get("usage") or {}
    if not isinstance(u, dict):
        u = {}

    def slot(key, *aliases):
        for name in (key,) + aliases:
            v = _n(u.get(name))
            if v is not None:
                return measured(v, source=f"{source}.usage.{name}")
        return unknown(NOT_EMITTED_BY_PROVIDER)

    inp = slot("input_tokens")
    out = slot("output_tokens")
    cr = slot("cache_read_input_tokens")
    cw = slot("cache_creation_input_tokens")

    per_model = {}
    mu = payload.get("modelUsage")
    if isinstance(mu, dict):
        for name, m in mu.items():
            if not isinstance(m, dict):
                continue
            entry = {}
            for dst, src in (("input_tokens", "inputTokens"),
                             ("output_tokens", "outputTokens"),
                             ("cache_read_tokens", "cacheReadInputTokens"),
                             ("cache_write_tokens", "cacheCreationInputTokens")):
                v = _n(m.get(src))
                if v is not None:
                    entry[dst] = v
            # A per-model price estimate is still an ESTIMATE, and it is kept in
            # the money namespace so it can never be mistaken for a token count.
            cost = _n(m.get("costUSD"))
            if cost is not None:
                entry["api_price_estimate_usd"] = cost
            for dst, src in (("context_window", "contextWindow"),
                             ("max_output_tokens", "maxOutputTokens")):
                v = _n(m.get(src))
                if v is not None:
                    entry[dst] = v
            if m.get("provider"):
                entry["provider"] = str(m["provider"])
            per_model[str(name)] = entry

    # CAPACITY is observed; UTILISATION is not. `usage` here is the session's
    # CUMULATIVE total across every request and every compaction, so dividing it
    # by the window measures nothing: a long session exceeds 1.0 by orders of
    # magnitude, and a short one understates the pressure of its biggest request.
    # Context utilisation is a property of a SINGLE request, and the terminal
    # payload does not carry one — so it stays UNKNOWN, with the reason.
    # Clamping to 1.0 would have been worse than the overflow: a fabricated
    # number that looks plausible.
    ctx_window = unknown(NOT_EMITTED_BY_PROVIDER)
    ctx_util = unknown(
        NOT_EMITTED_BY_PROVIDER,
        detail="terminal payload reports cumulative session tokens, not any "
               "single request's context occupancy; per-request or peak input "
               "is required and is not emitted here")
    windows = [e["context_window"] for e in per_model.values()
               if isinstance(e.get("context_window"), (int, float))]
    if windows:
        # Capacity of the largest model this launch used. Real, and reported.
        ctx_window = measured(max(windows),
                              source=f"{source}.modelUsage.contextWindow",
                              unit="tokens")

    turns = _n(payload.get("num_turns"))
    usage = Usage(
        source=source, completeness=completeness,
        input_tokens=inp, output_tokens=out,
        cache_read_tokens=cr, cache_write_tokens=cw,
        # Claude does not break reasoning out as a billed counter; the stream's
        # `thinking_tokens` events are explicitly `estimated_*`, so they are not
        # promoted into a measured field.
        reasoning_tokens=unknown(
            NOT_EMITTED_BY_PROVIDER,
            detail="stream emits system/thinking_tokens as estimated_tokens only"),
        total_tokens=unknown(NOT_EMITTED_BY_PROVIDER),
        # `num_turns` counts MODEL TURNS, which is not the same as HTTP requests
        # to the provider: a turn can hide a retry, a continuation or a
        # compaction pass. It is kept — it is real and useful — under its own
        # name and unit, and `provider_requests` stays UNKNOWN because no event
        # in this payload counts actual requests.
        model_turns=(measured(turns, unit="turns",
                              source=f"{source}.num_turns")
                     if turns is not None
                     else unknown(NOT_EMITTED_BY_PROVIDER)),
        provider_requests=unknown(
            NOT_EMITTED_BY_PROVIDER,
            detail="no event in this payload counts provider requests; "
                   "num_turns counts model turns and may hide retries, "
                   "continuations and compaction passes — see model_turns"),
        per_model=per_model, context_window=ctx_window,
        context_utilisation=ctx_util,
    )
    cost = _n(payload.get("total_cost_usd"))
    money = Money(
        api_price_estimate_usd=(measured(cost, unit="usd",
                                         source=f"{source}.total_cost_usd")
                                if cost is not None
                                else unknown(NOT_EMITTED_BY_PROVIDER)),
        billing_mode="UNKNOWN",
    )
    return usage, money


def parse_claude_result_json(stdout: str, *, killed=None) -> ParsedLog:
    """`claude -p --output-format json` — one JSON object, end-of-session totals."""
    text = (stdout or "").strip()
    if not text:
        # Nothing on stdout after a kill or a timeout is a TRUNCATED log — the
        # launch ran and spent quota. Only an engine that exited cleanly with no
        # output is genuinely unparseable.
        return _absent(LOG_TRUNCATED if killed else LOG_UNPARSEABLE,
                       "engine produced no stdout"
                       + (" before it was killed" if killed else ""))
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        # A killed CLI can leave a half-written object. That is a truncated log,
        # not a log without usage — and the difference decides whether a gap is
        # our bug or the provider's silence.
        return _absent(LOG_TRUNCATED if text.startswith("{") else LOG_UNPARSEABLE,
                       "stdout is not a complete JSON object")
    if not isinstance(payload, dict):
        return _absent(LOG_UNPARSEABLE, "stdout JSON is not an object")
    usage, money = _usage_from_claude_payload(
        payload, source=CLAUDE_RESULT_JSON, completeness=COMPLETE)
    # A payload that parses but carries `usage: {}` is not a measurement. Left
    # as COMPLETE it counted toward exact coverage and toward the verifier's
    # minimum evidence while containing no number at all.
    if not _any_measured(usage):
        return _absent(NOT_EMITTED_BY_PROVIDER,
                       "result payload parsed but reported no usage counters")
    api_ms = _n(payload.get("duration_api_ms"))
    return ParsedLog(
        usage=usage, money=money,
        quota=QuotaSnapshot(source=CLAUDE_RESULT_JSON),
        session_id=str(payload.get("session_id") or ""),
        model_observed=_dominant_model(usage.per_model),
        stop_reason=str(payload.get("stop_reason") or ""),
        terminal_reason=str(payload.get("terminal_reason") or ""),
        api_time_s=(measured(round(api_ms / 1000.0, 3), unit="s",
                             source=f"{CLAUDE_RESULT_JSON}.duration_api_ms")
                    if api_ms is not None else unknown(NOT_EMITTED_BY_PROVIDER)),
        raw_events=[allowlist_event({
            "schema_version": 1, "event": "result", "provider": "anthropic",
            "input_tokens": (payload.get("usage") or {}).get("input_tokens"),
            "output_tokens": (payload.get("usage") or {}).get("output_tokens"),
            "cache_read_tokens": (payload.get("usage") or {}).get("cache_read_input_tokens"),
            "cache_write_tokens": (payload.get("usage") or {}).get("cache_creation_input_tokens"),
            "stop_reason": payload.get("stop_reason"),
            "cost_reported": payload.get("total_cost_usd"),
        })],
    )


def _any_measured(usage: Usage) -> bool:
    """Did the provider actually print at least one token counter?"""
    from .record import is_measured
    return any(is_measured(getattr(usage, f)) for f in
               ("input_tokens", "output_tokens", "cache_read_tokens",
                "cache_write_tokens", "reasoning_tokens"))


def _dominant_model(per_model: dict) -> str:
    """The model that produced the most output. Multi-model launches are normal:
    a Claude session bills haiku side-calls alongside the routed model, and
    calling the whole launch "haiku" because it appeared first would be wrong."""
    best, best_out = "", -1
    for name, entry in (per_model or {}).items():
        out = entry.get("output_tokens")
        if isinstance(out, (int, float)) and out > best_out:
            best, best_out = name, out
    return best


# ── claude: --output-format stream-json ──────────────────────────────────────
def parse_claude_stream_json(lines, *, clean_exit=None) -> ParsedLog:
    """NDJSON stream. Authoritative when the terminal `result` event is present.

    `clean_exit` is the SUPERVISOR's observation of how the process ended. It is
    only consulted when no `result` event arrived: a stream that stops without
    totals after a clean exit is still missing its totals, so the reconstruction
    stays PARTIAL_FLOOR either way — the flag distinguishes "killed" from
    "ended quietly" in the caveat, and never upgrades a floor to a total.
    """
    last_usage: dict = {}          # message.id -> latest usage dict
    msg_model: dict = {}           # message.id -> model
    overflow = 0
    result_payload = None
    quota = QuotaSnapshot(source=CLAUDE_STREAM_JSON)
    rate_limit_waits = 0
    harness_version = ""
    session_id = ""
    init_model = ""
    cwd = ""
    torn_tail = False
    last_ts = ""
    first_user_ts = ""
    first_assistant_ts = ""
    raw_events: list = []
    saw_any = False

    for line in lines:
        line = (line or "").strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            # A torn line is almost always the last one: the process died
            # mid-write. It is dropped from the maths and remembered as evidence.
            torn_tail = True
            continue
        if not isinstance(ev, dict):
            continue
        saw_any = True
        etype = ev.get("type")
        if ev.get("timestamp"):
            last_ts = str(ev["timestamp"])
        if etype == "assistant":
            msg = ev.get("message") or {}
            mid = msg.get("id") or ev.get("uuid")
            ts = str(ev.get("timestamp") or "")
            if ts and not first_assistant_ts:
                first_assistant_ts = ts
            if not mid:
                continue
            if mid not in last_usage and len(last_usage) >= MAX_TRACKED_MESSAGES:
                overflow += 1
                continue
            u = msg.get("usage") or {}
            if isinstance(u, dict):
                last_usage[mid] = u          # last write wins — the dedupe
            if msg.get("model"):
                msg_model[mid] = str(msg["model"])
        elif etype == "user":
            ts = str(ev.get("timestamp") or "")
            if ts and not first_user_ts:
                first_user_ts = ts
        elif etype == "result":
            result_payload = ev
        elif etype == "system" and ev.get("subtype") == "init":
            session_id = str(ev.get("session_id") or session_id)
            harness_version = str(ev.get("claude_code_version") or harness_version)
            init_model = str(ev.get("model") or init_model)
            cwd = str(ev.get("cwd") or cwd)
        elif etype == "rate_limit_event":
            info = ev.get("rate_limit_info") or {}
            if isinstance(info, dict):
                # `rate_limit_event` carries no timestamp of its own, so the
                # reading is dated by the last timestamped event before it in
                # the same stream — accurate to the turn, and labelled a proxy
                # rather than passed off as the provider's own instant.
                seen_at = ev.get("timestamp") or last_ts
                seen_src = (OBSERVED_PROVIDER_EVENT if ev.get("timestamp")
                            else (OBSERVED_ADJACENT_EVENT if last_ts
                                  else OBSERVED_NONE))
                rate_limit_waits += 1 if str(info.get("status")) not in (
                    "allowed", "") else 0
                util = _n(info.get("utilization"))
                resets = _n(info.get("resetsAt"))
                quota = QuotaSnapshot(
                    source=f"{CLAUDE_STREAM_JSON}.rate_limit_event",
                    scope=str(info.get("rateLimitType") or "UNKNOWN"),
                    status=str(info.get("status") or "UNKNOWN"),
                    utilisation=(measured(util, unit="ratio",
                                          source="rate_limit_info.utilization")
                                 if util is not None
                                 else unknown(NOT_EMITTED_BY_PROVIDER)),
                    resets_at=(measured(resets, unit="epoch_s",
                                        source="rate_limit_info.resetsAt")
                               if resets is not None
                               else unknown(NOT_EMITTED_BY_PROVIDER)),
                    observed_at=str(seen_at or ""),
                    observed_at_source=seen_src,
                )
                raw_events.append(allowlist_event({
                    "schema_version": 1, "event": "rate_limit",
                    "provider": "anthropic",
                    "rate_limit_status": info.get("status"),
                    "rate_limit_scope": info.get("rateLimitType"),
                    "rate_limit_utilisation": info.get("utilization"),
                    "rate_limit_resets_at": info.get("resetsAt"),
                }))
        if ev.get("session_id") and not session_id:
            session_id = str(ev["session_id"])

    if not saw_any:
        # A no-op ternary here (`LOG_UNPARSEABLE if torn_tail else
        # LOG_UNPARSEABLE`) used to collapse both causes into one reason. A
        # stream that broke off mid-object (torn_tail: at least one line
        # failed json.loads) spent quota and is unresumable-but-measured
        # evidence of a real launch; a stream with zero valid JSON lines and
        # no torn tail never produced a recognisable NDJSON event at all.
        # Same distinction parse_claude_result_json already draws via
        # `text.startswith("{")` for the single-object format.
        return _absent(LOG_TRUNCATED if torn_tail else LOG_UNPARSEABLE,
                       "no parseable NDJSON events")

    if result_payload is not None:
        usage, money = _usage_from_claude_payload(
            result_payload, source=CLAUDE_STREAM_JSON, completeness=COMPLETE)
        if not _any_measured(usage):
            return _absent(NOT_EMITTED_BY_PROVIDER,
                           "terminal result carried no numeric usage counters")
        api_ms = _n(result_payload.get("duration_api_ms"))
        api_time = (measured(round(api_ms / 1000.0, 3), unit="s",
                             source=f"{CLAUDE_STREAM_JSON}.duration_api_ms")
                    if api_ms is not None else unknown(NOT_EMITTED_BY_PROVIDER))
        stop_reason = str(result_payload.get("stop_reason") or "")
        terminal_reason = str(result_payload.get("terminal_reason") or "")
        model_observed = _dominant_model(usage.per_model) or init_model
        if torn_tail:
            usage.caveats.append(
                "log contained at least one torn line; totals come from the "
                "terminal result event, which was present")
    else:
        # No totals. Reconstruct the floor, and say so in three places so it
        # cannot be read as a measurement by accident.
        def _sum(key):
            return sum(_n(u.get(key)) or 0 for u in last_usage.values())

        reason_detail = ("stream ended without a terminal result event"
                         + ("; supervisor observed a non-clean exit"
                            if clean_exit is False else ""))
        n_msgs = len(last_usage)
        usage = Usage(
            source=CLAUDE_STREAM_JSON, completeness=PARTIAL_FLOOR,
            input_tokens=measured(_sum("input_tokens"),
                                  source=f"{CLAUDE_STREAM_JSON}.assistant[dedup]"),
            output_tokens=measured(_sum("output_tokens"),
                                   source=f"{CLAUDE_STREAM_JSON}.assistant[dedup]"),
            cache_read_tokens=measured(
                _sum("cache_read_input_tokens"),
                source=f"{CLAUDE_STREAM_JSON}.assistant[dedup]"),
            cache_write_tokens=measured(
                _sum("cache_creation_input_tokens"),
                source=f"{CLAUDE_STREAM_JSON}.assistant[dedup]"),
            reasoning_tokens=unknown(
                NOT_EMITTED_BY_PROVIDER,
                detail="stream emits system/thinking_tokens as estimated_tokens "
                       "only; an estimate must not land in a billed counter"),
            total_tokens=unknown(LOG_TRUNCATED, detail=reason_detail),
            # Distinct assistant message ids are MODEL TURNS observed in the
            # stream. They are a lower bound on provider requests — a retried or
            # continued turn keeps one id — so they are reported as turns and
            # provider_requests stays unknown.
            model_turns=measured(n_msgs, unit="turns",
                                 source=f"{CLAUDE_STREAM_JSON}.distinct_message_ids"),
            provider_requests=unknown(
                NOT_EMITTED_BY_PROVIDER,
                detail="stream counts assistant turns, not provider requests; "
                       "retries and continuations reuse a message id"),
            per_model=_per_model_from_stream(last_usage, msg_model),
        )
        usage.caveats.append(
            "PARTIAL_FLOOR: " + reason_detail
            + ". input/cache reconstruct exactly from de-duplicated assistant "
              "events; output_tokens is a LOWER BOUND because per-message usage "
              "is snapshotted before the message completes (measured: 75 vs a "
              "terminal 16181 on a real m01 run)")
        money = Money(api_price_estimate_usd=unknown(
            LOG_TRUNCATED, detail=reason_detail))
        api_time = unknown(LOG_TRUNCATED, detail=reason_detail)
        stop_reason = ""
        terminal_reason = "TRUNCATED"
        model_observed = init_model or _dominant_model(usage.per_model)
        if torn_tail:
            usage.caveats.append("log contained at least one torn line (dropped)")

    if overflow:
        usage.caveats.append(
            f"{overflow} assistant event(s) beyond the {MAX_TRACKED_MESSAGES}-message "
            f"memory bound were not folded; token totals are incomplete")

    # Per-REQUEST context occupancy, which the stream can support and the
    # terminal payload cannot: each assistant message's own usage describes the
    # one request that produced it. The PEAK across messages is the closest
    # honest answer to "how full did the window get", and it is only computed
    # when every message agrees on the model — a mixed-model stream has no
    # single window to divide by.
    usage.context_utilisation = _stream_context_utilisation(
        last_usage, msg_model, usage.context_window)
    return ParsedLog(
        usage=usage, money=money, quota=quota, session_id=session_id,
        model_observed=model_observed, harness_version=harness_version,
        stop_reason=stop_reason, terminal_reason=terminal_reason,
        api_time_s=api_time, rate_limit_waits=rate_limit_waits,
        raw_events=raw_events, cwd=cwd,
        first_event_at=(first_assistant_ts or first_user_ts),
    )


def _stream_context_utilisation(last_usage: dict, msg_model: dict, ctx_window):
    """Peak single-request context occupancy, or UNKNOWN with the reason.

    Two sessions with the same per-request context but different turn counts
    must report the SAME context pressure — that is what makes this a measure of
    the window rather than of session length. Summing across turns fails that
    test, so the peak over per-message occupancy is used instead.
    """
    from .record import is_measured
    if not is_measured(ctx_window) or ctx_window["value"] <= 0:
        return unknown(NOT_EMITTED_BY_PROVIDER,
                       detail="no context window reported for this launch")
    if not last_usage:
        return unknown(NOT_EMITTED_BY_PROVIDER,
                       detail="no per-message usage to measure occupancy from")
    models = {m for m in msg_model.values() if m}
    if len(models) > 1:
        return unknown(
            NOT_EMITTED_BY_PROVIDER,
            detail=f"launch spans {len(models)} models with different context "
                   f"windows; no single window to divide by")
    peak = 0
    for u in last_usage.values():
        occupancy = sum(v for v in (_n(u.get("input_tokens")),
                                    _n(u.get("cache_read_input_tokens")),
                                    _n(u.get("cache_creation_input_tokens")))
                        if v is not None)
        peak = max(peak, occupancy)
    if peak <= 0:
        return unknown(NOT_EMITTED_BY_PROVIDER,
                       detail="per-message usage reported no input occupancy")
    # NOT clamped. A ratio above 1 would mean the window we were told about is
    # not the window that was used, and hiding that behind a cap would turn a
    # detectable inconsistency into a plausible-looking number.
    return measured(round(peak / ctx_window["value"], 6), unit="ratio",
                    source=f"{CLAUDE_STREAM_JSON}.assistant[peak per-request "
                           f"input+cache]/modelUsage.contextWindow")


def _per_model_from_stream(last_usage: dict, msg_model: dict) -> dict:
    out: dict = {}
    for mid, u in last_usage.items():
        name = msg_model.get(mid) or "UNKNOWN"
        entry = out.setdefault(name, {"input_tokens": 0, "output_tokens": 0,
                                      "cache_read_tokens": 0,
                                      "cache_write_tokens": 0})
        for dst, src in (("input_tokens", "input_tokens"),
                         ("output_tokens", "output_tokens"),
                         ("cache_read_tokens", "cache_read_input_tokens"),
                         ("cache_write_tokens", "cache_creation_input_tokens")):
            entry[dst] += _n(u.get(src)) or 0
    return out


def time_to_first_event(launched_at: str, first_event_at: str):
    """execve → the engine's first emitted event. The observable TTFT proxy.

    Both ends are needed and both must parse: `launched_at` is the supervisor's
    own clock at execve, `first_event_at` the engine's first log timestamp. A
    negative interval means the two clocks disagree, which is a fact about the
    measurement and is reported as unknown rather than clamped to 0.
    """
    if not (launched_at and first_event_at):
        return unknown(NOT_EMITTED_BY_PROVIDER,
                       detail="no launch instant and/or no engine event timestamp")
    import datetime as _dt
    try:
        a = _dt.datetime.fromisoformat(str(launched_at).replace("Z", "+00:00"))
        b = _dt.datetime.fromisoformat(str(first_event_at).replace("Z", "+00:00"))
    except ValueError:
        return unknown(LOG_UNPARSEABLE, detail="unparseable timestamps")
    if a.tzinfo is None or b.tzinfo is None:
        return unknown(LOG_UNPARSEABLE, detail="timestamp without a timezone")
    delta = (b - a).total_seconds()
    if delta < 0:
        return unknown(LOG_UNPARSEABLE,
                       detail="engine event precedes the observed execve")
    return measured(round(delta, 3), unit="s",
                    source="supervisor.execve_to_first_engine_event")


# ── opencode / GLM: run --format json ────────────────────────────────────────
def parse_opencode_json(lines, *, clean_exit=None) -> ParsedLog:
    """NDJSON parts. Usage rides on `step_finish.part.tokens`, one per request.

    opencode emits no end-of-session totals event, so a cut stream and a finished
    one look identical from the log alone. The supervisor's exit observation is
    therefore load-bearing here: a clean exit promotes the sum to COMPLETE, and
    anything else leaves it a floor.
    """
    steps = 0
    tok = {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0,
           "cache_write": 0, "total": 0}
    cost_sum = 0.0
    cost_seen = False
    session_id = ""
    torn_tail = False
    saw_any = False
    first_step_ts = None
    first_text_ts = None
    raw_events: list = []
    seen_parts: set = set()
    usage_seen = False
    overflow = 0

    for line in lines:
        line = (line or "").strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            torn_tail = True
            continue
        if not isinstance(ev, dict):
            continue
        saw_any = True
        if ev.get("sessionID") and not session_id:
            session_id = str(ev["sessionID"])
        etype = ev.get("type")
        part = ev.get("part") or {}
        if etype == "step_start" and first_step_ts is None:
            first_step_ts = _n(ev.get("timestamp"))
        elif etype == "text" and first_text_ts is None:
            t = (part.get("time") or {}) if isinstance(part, dict) else {}
            first_text_ts = _n(t.get("start")) or _n(ev.get("timestamp"))
        elif etype == "step_finish" and isinstance(part, dict):
            # A resumed opencode session replays earlier parts. Part ids are
            # stable, so folding on them makes replay idempotent instead of
            # doubling every token the session had already spent.
            pid = part.get("id")
            if pid:
                if pid in seen_parts:
                    continue
                if len(seen_parts) >= MAX_TRACKED_MESSAGES:
                    overflow += 1
                    continue
                seen_parts.add(pid)
            steps += 1
            t = part.get("tokens") or {}
            cache = t.get("cache") or {}
            usage_seen = usage_seen or any(
                _n(t.get(k)) is not None
                for k in ("input", "output", "reasoning", "total")
            ) or any(_n(cache.get(k)) is not None for k in ("read", "write"))
            tok["input"] += _n(t.get("input")) or 0
            tok["output"] += _n(t.get("output")) or 0
            tok["reasoning"] += _n(t.get("reasoning")) or 0
            tok["total"] += _n(t.get("total")) or 0
            tok["cache_read"] += _n(cache.get("read")) or 0
            tok["cache_write"] += _n(cache.get("write")) or 0
            c = _n(part.get("cost"))
            if c is not None:
                cost_sum += c
                cost_seen = True
            raw_events.append(allowlist_event({
                "schema_version": 1, "event": "step_finish", "provider": "zai",
                "message_id": part.get("messageID"),
                "timestamp": ev.get("timestamp"),
                "input_tokens": t.get("input"), "output_tokens": t.get("output"),
                "reasoning_tokens": t.get("reasoning"),
                "cache_read_tokens": cache.get("read"),
                "cache_write_tokens": cache.get("write"),
                "total_tokens": t.get("total"),
                "step_reason": part.get("reason"),
                "cost_reported": part.get("cost"),
            }))

    if not saw_any:
        return _absent(LOG_UNPARSEABLE, "no parseable NDJSON parts")
    if steps == 0:
        return _absent(LOG_TRUNCATED if torn_tail else LOG_UNPARSEABLE,
                       "no step_finish part carried usage")
    if not usage_seen:
        return _absent(NOT_EMITTED_BY_PROVIDER,
                       "step_finish parts carried no numeric usage counters")

    completeness = (COMPLETE if clean_exit is True and overflow == 0
                    else PARTIAL_FLOOR)
    src = OPENCODE_JSON
    usage = Usage(
        source=src, completeness=completeness,
        input_tokens=measured(tok["input"], source=f"{src}.tokens.input"),
        output_tokens=measured(tok["output"], source=f"{src}.tokens.output"),
        cache_read_tokens=measured(tok["cache_read"],
                                   source=f"{src}.tokens.cache.read"),
        cache_write_tokens=measured(tok["cache_write"],
                                    source=f"{src}.tokens.cache.write"),
        reasoning_tokens=measured(tok["reasoning"],
                                  source=f"{src}.tokens.reasoning"),
        # The provider's own `total` already includes cache reads, so it is kept
        # for reconciliation and never used as a summand anywhere downstream.
        total_tokens=measured(tok["total"], source=f"{src}.tokens.total"),
        # `step_finish` parts are LOGICAL MODEL STEPS. opencode emits one per
        # completed step, and a step that the client retried on a transport
        # error still reports once — so this is a lower bound on network
        # requests, not a count of them.
        model_turns=measured(steps, unit="turns",
                             source=f"{src}.step_finish.count"),
        provider_requests=unknown(
            NOT_EMITTED_BY_PROVIDER,
            detail="opencode emits logical step boundaries, not network "
                   "request records; a retried step still reports once"),
    )
    usage.session_log_semantics = "CUMULATIVE_OR_REPLAY"
    usage.caveats.append(
        "opencode `tokens.total` includes cache reads; aggregates are computed "
        "from the components, never from total")
    if completeness == PARTIAL_FLOOR:
        usage.caveats.append(
            "PARTIAL_FLOOR: opencode emits no end-of-session totals, and the "
            "supervisor did not observe a clean exit, so this sum is a lower "
            "bound over the step_finish parts present in the log")
    if overflow:
        usage.caveats.append(
            f"PARTIAL_FLOOR: message ceiling overflow; {overflow} step_finish "
            "part(s) were not retained")
    if torn_tail:
        usage.caveats.append("log contained at least one torn line (dropped)")
    # First step_start → first emitted text. A proxy, and one that includes any
    # tool calls the model made before speaking, so it is never called TTFT.
    usage.time_to_first_event_s = (
        measured(round((first_text_ts - first_step_ts) / 1000.0, 3), unit="s",
                 source=f"{src}.first_text_minus_first_step_start")
        if (first_step_ts and first_text_ts and first_text_ts >= first_step_ts)
        else unknown(NOT_EMITTED_BY_PROVIDER))
    money = Money(
        api_price_estimate_usd=(measured(round(cost_sum, 10), unit="usd",
                                         source=f"{src}.cost")
                                if cost_seen else unknown(NOT_EMITTED_BY_PROVIDER)),
        billing_mode="UNKNOWN",
    )
    return ParsedLog(usage=usage, money=money,
                     quota=QuotaSnapshot(
                         source=src,
                         scope="UNKNOWN",
                         status="UNKNOWN"),
                     session_id=session_id, raw_events=raw_events)


# ── codex: exec --json ───────────────────────────────────────────────────────
def _codex_payload(ev: dict) -> dict:
    """The event body, across the envelopes this CLI has shipped.

    `codex exec --json` and the rollout session files carry the same event
    stream under different wrappers depending on version: `{"type":"event_msg",
    "payload":{...}}`, the older `{"msg":{...}}`, or a bare `{"type":"token_count"}`.
    All three are accepted; anything else is left alone rather than guessed at.
    """
    for key in ("payload", "msg"):
        body = ev.get(key)
        if isinstance(body, dict) and body.get("type"):
            return body
    return ev if ev.get("type") else {}


def parse_codex_jsonl(lines, *, clean_exit=None) -> ParsedLog:
    """codex `exec --json` events, and the rollout files that carry the same stream.

    The double-count hazard here is the opposite of Claude's. Codex emits
    `token_count` with a CUMULATIVE `info.total_token_usage` — one real session
    emitted 641 of them — so the totals come from the LAST such event and are
    never summed. `last_token_usage` is the per-turn delta and is deliberately
    ignored for totals; it is what you would sum if you wanted the same number
    twice.

    codex 0.154 (`exec --json`) no longer prints `token_count` at all: the
    stream is `thread.started` / `turn.started` / `item.*` / `turn.completed`,
    and the usage rides on `turn.completed.usage` with the SAME cumulative
    semantics (`total_token_usage` of the session at that instant — verified on
    tests/fixtures/token_truth/codex.jsonl, a real 0.154 review transcript).
    Both envelopes are accepted; `turn.completed` is folded exactly like
    `token_count` (last wins, never summed) and reported under its own source
    id so a reader can tell which CLI generation produced the number.

    Field nesting is normalised to DISJOINT_TOKEN_FIELDS: codex counts cached
    input inside `input_tokens` and reasoning inside `output_tokens`, so the
    nested parts are subtracted. The result reconciles to the provider's own
    `total_tokens` exactly, which the test asserts on real numbers.

    Reasoning is COUNTED here and never read: `response_item`/`reasoning`
    payloads carry the model's private chain of thought and this parser does not
    touch them.
    """
    last_total = None
    last_turn = None
    last_total_at = ""
    context_window = None
    rate_limits = None
    rate_limits_at = ""
    n_token_events = 0
    session_id = ""
    cli_version = ""
    model = ""
    first_event_at = ""
    completed = False
    torn_tail = False
    saw_any = False
    saw_thread_events = False      # codex >= 0.154 envelope seen (thread/turn/item events)
    turn_failed = False
    from_turn_completed = False
    turns_started = 0
    turn_ids: set = set()
    raw_events: list = []

    for line in lines:
        line = (line or "").strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            torn_tail = True
            continue
        if not isinstance(ev, dict):
            continue
        saw_any = True
        ts = str(ev.get("timestamp") or "")
        if ts and not first_event_at:
            first_event_at = ts
        etype = ev.get("type")
        body = _codex_payload(ev)
        btype = body.get("type")

        if etype == "session_meta":
            # session_meta's payload carries no inner "type", so _codex_payload
            # cannot recognise it; read the envelope's payload directly.
            meta = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
            session_id = str(meta.get("id") or meta.get("session_id") or session_id)
            cli_version = str(meta.get("cli_version") or cli_version)
        elif etype == "turn_context":
            tc = ev.get("payload") or {}
            model = str(tc.get("model") or model)
            if tc.get("turn_id"):
                turn_ids.add(tc["turn_id"])
        elif etype == "thread.started":
            saw_thread_events = True
            session_id = str(ev.get("thread_id") or session_id)
        elif etype == "turn.started":
            saw_thread_events = True
            turns_started += 1
        elif etype == "turn.completed":
            # codex >= 0.154: the turn's cumulative usage. LAST wins, like
            # token_count — a resumed thread replays the running total.
            saw_thread_events = True
            u = ev.get("usage")
            if isinstance(u, dict):
                n_token_events += 1
                last_total = u
                last_total_at = ts or last_total_at
                from_turn_completed = True
            completed = True
        elif etype == "turn.failed":
            saw_thread_events = True
            turn_failed = True
        elif isinstance(etype, str) and etype.startswith("item."):
            saw_thread_events = True
        elif btype == "token_count":
            n_token_events += 1
            info = body.get("info") or {}
            total = info.get("total_token_usage")
            if isinstance(total, dict):
                # LAST wins. Summing these is the codex double-count.
                last_total = total
                last_total_at = ts or last_total_at
            # The per-TURN delta. Not used for totals — that is the trap — but
            # its input_tokens is how much context the last request carried,
            # which is the only honest basis for a context-utilisation figure.
            turn = info.get("last_token_usage")
            if isinstance(turn, dict):
                last_turn = turn
            cw = _n(info.get("model_context_window"))
            if cw is not None:
                context_window = cw
            rl = body.get("rate_limits")
            if isinstance(rl, dict):
                rate_limits = rl
                rate_limits_at = ts or rate_limits_at
            # The raw snapshot is built ONCE, after the fold, from `last_total`.
            # Rebuilding it per event from that event's own `total` meant a
            # token_count carrying no total_token_usage overwrote the kept
            # snapshot with all-None counters — the totals survived (they come
            # from last_total) but the raw evidence a re-aggregation would use
            # was silently emptied.
            pass
        elif btype == "task_complete":
            completed = True
        if ev.get("session_id") and not session_id:
            session_id = str(ev["session_id"])

    if not saw_any:
        return _absent(LOG_UNPARSEABLE, "no parseable codex JSONL events")
    if last_total is None:
        if saw_thread_events:
            # The 0.154 envelope was there, so `--json` WAS honoured; the stream
            # simply ended (turn.failed, kill, quota cut) before turn.completed
            # carried the usage. A cut before the first usage event is typed,
            # never a zero.
            return _absent(
                LOG_TRUNCATED,
                "codex exec --json stream ended before `turn.completed` carried "
                "usage" + (" (turn.failed observed)" if turn_failed else ""))
        return _absent(
            LOG_TRUNCATED if torn_tail else NOT_EMITTED_BY_INVOCATION,
            "no token_count/turn.completed event carried usage; `codex exec` "
            "without --json emits no usage at all")
    if not any(_n(last_total.get(key)) is not None for key in (
            "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
            "output_tokens", "reasoning_output_tokens", "total_tokens")):
        return _absent(NOT_EMITTED_BY_PROVIDER,
                       "terminal codex usage object carried no numeric counters")

    src = CODEX_TURN_COMPLETED if from_turn_completed else CODEX_JSONL
    if not turn_ids and turns_started:
        turn_ids = {f"turn.started#{i}" for i in range(1, turns_started + 1)}
    inp = _n(last_total.get("input_tokens")) or 0
    cached = _n(last_total.get("cached_input_tokens")) or 0
    cwrite = _n(last_total.get("cache_write_input_tokens")) or 0
    out = _n(last_total.get("output_tokens")) or 0
    reasoning = _n(last_total.get("reasoning_output_tokens")) or 0
    provider_total = _n(last_total.get("total_tokens"))

    # Un-nest so the five common fields are mutually exclusive. max(0, …) guards
    # a provider that ever reports a nested part larger than its parent; that
    # would be a provider bug, and clamping is safer than emitting a negative
    # token count into an aggregate.
    uncached_in = max(0, inp - cached)
    non_reasoning_out = max(0, out - reasoning)

    usage = Usage(
        source=src,
        completeness=COMPLETE if (completed or clean_exit is True) else PARTIAL_FLOOR,
        input_tokens=measured(uncached_in,
                              source=f"{src}.total_token_usage.input_tokens"
                                     f"-cached_input_tokens"),
        output_tokens=measured(non_reasoning_out,
                               source=f"{src}.total_token_usage.output_tokens"
                                      f"-reasoning_output_tokens"),
        cache_read_tokens=measured(
            cached, source=f"{src}.total_token_usage.cached_input_tokens"),
        cache_write_tokens=measured(
            cwrite, source=f"{src}.total_token_usage.cache_write_input_tokens"),
        reasoning_tokens=measured(
            reasoning, source=f"{src}.total_token_usage.reasoning_output_tokens"),
        total_tokens=(measured(provider_total,
                               source=f"{src}.total_token_usage.total_tokens")
                      if provider_total is not None
                      else unknown(NOT_EMITTED_BY_PROVIDER)),
        # Codex reports cumulative usage per session, not per request. The count
        # of token_count events is NOT a request count — it ticks on tool calls
        # too — so claiming one would be inventing a number.
        model_turns=(measured(len(turn_ids), unit="turns",
                              source=f"{src}.turn_context.turn_id[distinct]")
                     if turn_ids else unknown(NOT_EMITTED_BY_PROVIDER)),
        provider_requests=unknown(
            NOT_EMITTED_BY_PROVIDER,
            detail="codex token_count events are cumulative snapshots and "
                   "turn_context marks turns; neither counts provider requests"),
        context_window=(measured(context_window, source=f"{src}.model_context_window")
                        if context_window is not None
                        else unknown(NOT_EMITTED_BY_PROVIDER)),
        # Context utilisation is a property of ONE request, so it is computed
        # from the last turn's input — never from the session's cumulative
        # total, which on a long session divides into a window many times over
        # and produced a nonsensical 317.8 before this was fixed.
        context_utilisation=_codex_context_utilisation(
            last_turn, context_window, src),
    )
    usage.session_log_semantics = "CUMULATIVE_OR_REPLAY"
    usage.disjoint_normalisation = (
        "codex nests cached input inside input_tokens and reasoning inside "
        "output_tokens; both nested parts were subtracted so the five common "
        "fields are disjoint and re-sum to total_token_usage.total_tokens")
    usage.caveats.append(
        "totals are the LAST cumulative "
        f"{'turn.completed' if from_turn_completed else 'token_count'} event "
        f"({n_token_events} seen); summing these events would multiply the "
        "session's real usage by the number of snapshots")
    if turn_failed:
        usage.caveats.append("turn.failed observed after the last usage event")
    if usage.completeness == PARTIAL_FLOOR:
        usage.caveats.append(
            "PARTIAL_FLOOR: no task_complete/turn.completed event and no clean exit observed, "
            "so this is the last cumulative snapshot before the log ended, not "
            "necessarily the session's final total")
    if torn_tail:
        usage.caveats.append("log contained at least one torn line (dropped)")

    quota = QuotaSnapshot(source=f"{src}.rate_limits")
    if isinstance(rate_limits, dict):
        primary = rate_limits.get("primary")
        if isinstance(primary, dict):
            used = _n(primary.get("used_percent"))
            resets = _n(primary.get("resets_at"))
            minutes = _n(primary.get("window_minutes"))
            quota = QuotaSnapshot(
                source=f"{src}.rate_limits.primary",
                scope=(f"{int(minutes)}min" if minutes else
                       str(rate_limits.get("limit_id") or "UNKNOWN")),
                status=str(rate_limits.get("rate_limit_reached_type") or "allowed"),
                utilisation=(measured(round(used / 100.0, 6), unit="ratio",
                                      source="rate_limits.primary.used_percent")
                             if used is not None
                             else unknown(NOT_EMITTED_BY_PROVIDER)),
                resets_at=(measured(resets, unit="epoch_s",
                                    source="rate_limits.primary.resets_at")
                           if resets is not None
                           else unknown(NOT_EMITTED_BY_PROVIDER)),
                # codex DOES timestamp its token_count events, so this is the
                # provider's own instant, not a proxy.
                observed_at=str(rate_limits_at or last_total_at or ""),
                observed_at_source=(OBSERVED_PROVIDER_EVENT
                                    if (rate_limits_at or last_total_at)
                                    else OBSERVED_NONE),
            )
            raw_events.append(allowlist_event({
                "schema_version": 1, "event": "rate_limit", "provider": "openai",
                "timestamp": rate_limits_at,
                "rate_limit_status": rate_limits.get("rate_limit_reached_type"),
                "rate_limit_scope": primary.get("window_minutes"),
                "rate_limit_utilisation": primary.get("used_percent"),
                "rate_limit_resets_at": primary.get("resets_at"),
            }))

    # Exactly one cumulative snapshot, from the fold. Storing all 641 of a real
    # session's token_count events invites a future re-aggregation to sum them —
    # the exact 98% inflation this adapter exists to avoid. The `cumulative`
    # marker rides along so nothing downstream can mistake it for a delta.
    codex_raw = [allowlist_event({
        "schema_version": 1, "event": "token_count", "provider": "openai",
        "timestamp": last_total_at,
        "input_tokens": last_total.get("input_tokens"),
        "output_tokens": last_total.get("output_tokens"),
        "cache_read_tokens": last_total.get("cached_input_tokens"),
        "cache_write_tokens": last_total.get("cache_write_input_tokens"),
        "reasoning_tokens": last_total.get("reasoning_output_tokens"),
        "total_tokens": last_total.get("total_tokens"),
        "cumulative": True,
    })]
    raw_events = codex_raw + raw_events
    return ParsedLog(
        usage=usage,
        # Codex reports no per-call price. A subscription plan name is not a
        # price, and deriving one from tokens is exactly what must not happen.
        money=Money(api_price_estimate_usd=unknown(NOT_EMITTED_BY_PROVIDER),
                    billing_mode="UNKNOWN"),
        quota=quota, session_id=session_id, model_observed=model,
        harness_version=cli_version,
        terminal_reason=("failed" if turn_failed else "completed" if completed else ""),
        raw_events=raw_events, first_event_at=first_event_at,
    )


def _codex_context_utilisation(last_turn, context_window, src):
    """How full the window was on the LAST request, when both ends are real."""
    if not context_window or not isinstance(last_turn, dict):
        return unknown(NOT_EMITTED_BY_PROVIDER)
    loaded = _n(last_turn.get("input_tokens"))
    if loaded is None:
        return unknown(NOT_EMITTED_BY_PROVIDER)
    return measured(round(loaded / context_window, 6), unit="ratio",
                    source=f"{src}.last_token_usage.input_tokens"
                           f"/model_context_window")


def parse_unstructured(_text=None, *, detail: str = "") -> ParsedLog:
    """The invocation asked for prose. Nothing is measurable — say which and why."""
    return _absent(NOT_EMITTED_BY_INVOCATION,
                   detail or "the launched argv requested no machine-readable "
                             "usage output")


# ── format detection ─────────────────────────────────────────────────────────
def detect_format(argv) -> str:
    """Which adapter fits THIS argv. Derived from the bytes handed to execve.

    This is the function that turns "we don't have GLM token counts" from a
    mystery into a one-line fact: `opencode run` without `--format json` is
    UNSTRUCTURED, and that is a property of our argv, not of the provider.
    """
    argv = [str(a) for a in (argv or [])]
    if not argv:
        return UNSTRUCTURED
    import os
    head = os.path.basename(argv[0])
    if head == "claude":
        if "--output-format" in argv:
            i = argv.index("--output-format")
            fmt = argv[i + 1] if i + 1 < len(argv) else ""
            if fmt == "stream-json":
                return CLAUDE_STREAM_JSON
            if fmt == "json":
                return CLAUDE_RESULT_JSON
        return UNSTRUCTURED
    if head in ("opencode", "glm", "zcode"):
        if "--format" in argv:
            i = argv.index("--format")
            if i + 1 < len(argv) and argv[i + 1] == "json":
                return OPENCODE_JSON
        return UNSTRUCTURED
    if head == "codex":
        # `--json` only exists under the `exec` subcommand. Bare `codex <prompt>`
        # forwards to the interactive CLI and emits no event stream at all.
        if "exec" in argv and "--json" in argv:
            return CODEX_JSONL
        return UNSTRUCTURED
    return UNSTRUCTURED


def parse(fmt: str, source, *, clean_exit=None) -> ParsedLog:
    """Dispatch to the adapter for `fmt`. `source` is text or an iterable of lines."""
    if fmt == CLAUDE_RESULT_JSON:
        text = source if isinstance(source, str) else "\n".join(source)
        return parse_claude_result_json(text, killed=(clean_exit is False))
    if fmt == CLAUDE_STREAM_JSON:
        lines = source.splitlines() if isinstance(source, str) else source
        return parse_claude_stream_json(lines, clean_exit=clean_exit)
    if fmt == OPENCODE_JSON:
        lines = source.splitlines() if isinstance(source, str) else source
        return parse_opencode_json(lines, clean_exit=clean_exit)
    if fmt == CODEX_JSONL:
        lines = source.splitlines() if isinstance(source, str) else source
        return parse_codex_jsonl(lines, clean_exit=clean_exit)
    return parse_unstructured()


def parse_argv_output(argv, source, *, clean_exit=None) -> ParsedLog:
    """The production entry point: pick the adapter from argv, then parse."""
    return parse(detect_format(argv), source, clean_exit=clean_exit)


# ── the agent's own last words, for callers that already handle prose ────────
#: Hard cap on extracted display text. The dispatcher only ever keeps a tail.
MAX_DISPLAY_TEXT = 20_000


def final_text(fmt: str, source) -> str:
    """The agent's final human-readable message, pulled out of a structured stream.

    This exists because asking an engine for JSON changes what its stdout IS.
    The dispatcher has always treated stdout as prose — it tails it into the
    receipt and runs `classify_blocked_reason` over it — so turning on
    `--format json` would have handed those two a wall of event envelopes and
    silently degraded both. This gives them back the same thing they had.

    It is NOT telemetry and never enters the telemetry ledger: this is the
    existing receipt/classification path, which already stored stdout tails.
    Callers keep whatever retention policy they already had.

    Returns "" when the stream carries no final message — callers then fall back
    to the raw stdout they were using before.
    """
    text = source if isinstance(source, str) else "\n".join(source)
    if not text.strip():
        return ""
    try:
        if fmt == CLAUDE_RESULT_JSON:
            payload = json.loads(text)
            if isinstance(payload, dict):
                return str(payload.get("result") or "")[:MAX_DISPLAY_TEXT]
            return ""
        if fmt == CLAUDE_STREAM_JSON:
            return _last_of(text, _claude_stream_text)[:MAX_DISPLAY_TEXT]
        if fmt == OPENCODE_JSON:
            return _last_of(text, _opencode_text)[:MAX_DISPLAY_TEXT]
        if fmt == CODEX_JSONL:
            return _last_of(text, _codex_text)[:MAX_DISPLAY_TEXT]
    except (json.JSONDecodeError, TypeError, ValueError):
        return ""
    return ""


def _last_of(text: str, extract) -> str:
    """Last non-empty extraction over NDJSON. Torn lines are skipped, not fatal:
    a killed engine's earlier output is still the best description we have of
    why it was doing what it was doing."""
    out = ""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        got = extract(ev)
        if got:
            out = got
    return out


def _claude_stream_text(ev: dict) -> str:
    if ev.get("type") == "result":
        return str(ev.get("result") or "")
    if ev.get("type") == "assistant":
        blocks = ((ev.get("message") or {}).get("content") or [])
        # `thinking` blocks are skipped: reasoning is counted, never surfaced.
        return "".join(b.get("text", "") for b in blocks
                       if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _opencode_text(ev: dict) -> str:
    if ev.get("type") == "text":
        return str((ev.get("part") or {}).get("text") or "")
    return ""


def _codex_text(ev: dict) -> str:
    body = _codex_payload(ev)
    if body.get("type") == "task_complete":
        return str(body.get("last_agent_message") or "")
    if body.get("type") == "agent_message":
        return str(body.get("message") or "")
    return ""
