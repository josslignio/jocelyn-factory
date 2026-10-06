"""The common telemetry record every provider adapter must produce.

One vocabulary for every AI launch in the Factory — Claude, GLM/opencode, Codex,
reviewers, research, recovery — so a number means the same thing whoever emitted
it. Provider-specific units survive in `raw_usage`; the common fields are the
part that can be summed.

Four units, never merged (the §D invariant, restated as structure rather than
prose):

    calls      dispatch launches, and provider requests inside a launch
    tokens     input / output / cache read / cache write / reasoning COUNTS
    quota      a provider-reported window snapshot, with its scope and timestamp
    money      an API *price estimate* the provider printed, and — separately —
               money actually charged, which we never observe and never invent

The one rule that makes the rest trustworthy: an unmeasured quantity is
``Unknown(reason)``. Never 0, never an estimate wearing a measurement's clothes.
``0`` in this file always means "the provider said zero".

Nothing here stores prompt text, file contents, secrets, or reasoning content.
Reasoning is counted, never quoted. argv is redacted to flags and lengths.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field, asdict
from factory_v2._io import _now, _now as utcnow

SCHEMA = "telemetry/launch-observation/v2"

UNKNOWN = "UNKNOWN"

# ── why a quantity is unknown ────────────────────────────────────────────────
#: The provider's binary never emits it in ANY mode.
NOT_EMITTED_BY_PROVIDER = "NOT_EMITTED_BY_PROVIDER"
#: The provider CAN emit it, but the argv production actually uses does not ask
#: for it. This is the honest verdict for `opencode run` without `--format json`:
#: our gap, not theirs, and fixable by changing the invocation.
NOT_EMITTED_BY_INVOCATION = "NOT_EMITTED_BY_INVOCATION"
#: Emitted, but the log we hold stops before the totals (crash, kill, truncation).
LOG_TRUNCATED = "LOG_TRUNCATED"
#: The log was unreadable or not valid in its declared format.
LOG_UNPARSEABLE = "LOG_UNPARSEABLE"
#: No log was captured for this launch at all.
NO_LOG_CAPTURED = "NO_LOG_CAPTURED"
#: The launch site does not go through telemetry yet. A coverage gap, stated.
LAUNCH_PATH_UNINSTRUMENTED = "LAUNCH_PATH_UNINSTRUMENTED"
#: Structurally unobservable from here — e.g. the root operator's own chat quota.
UNOBSERVABLE_FROM_SUPERVISOR = "UNOBSERVABLE_FROM_SUPERVISOR"

UNKNOWN_REASONS = frozenset({
    NOT_EMITTED_BY_PROVIDER, NOT_EMITTED_BY_INVOCATION, LOG_TRUNCATED,
    LOG_UNPARSEABLE, NO_LOG_CAPTURED, LAUNCH_PATH_UNINSTRUMENTED,
    UNOBSERVABLE_FROM_SUPERVISOR,
})

# ── how complete a usage block is ────────────────────────────────────────────
#: The provider printed end-of-session totals. Authoritative.
COMPLETE = "COMPLETE"
#: Reconstructed from per-message events with no terminal total. Input and cache
#: reconstruct exactly; output is a LOWER BOUND (see adapters.claude_stream_json).
PARTIAL_FLOOR = "PARTIAL_FLOOR"
#: Nothing measured.
ABSENT = "ABSENT"

COMPLETENESS = frozenset({COMPLETE, PARTIAL_FLOOR, ABSENT})


class Slot(dict):
    """A usage slot: the plain dict every consumer already reads (`slot["value"]`,
    JSON-serialisable, equal to its dict), plus `.value` for readers written
    against the attribute form — `factory_v2/verify_token_truth.py` reads
    `getattr(usage.input_tokens, "value", …)`. Same bytes on disk either way."""
    __slots__ = ()

    @property
    def value(self):
        return self.get("value")

    @property
    def reason(self):
        return self.get("reason")


def unknown(reason: str, *, detail: str = "", observed_at: str = "") -> dict:
    """An unknown that carries WHY and WHEN. The only permitted non-number."""
    if reason not in UNKNOWN_REASONS:
        raise ValueError(f"unknown reason {reason!r} not in {sorted(UNKNOWN_REASONS)}")
    out = Slot({"value": UNKNOWN, "reason": reason,
                "observed_at": observed_at or utcnow()})
    if detail:
        out["detail"] = detail
    return out


def measured(value, *, source: str, unit: str = "tokens") -> dict:
    """A number the provider actually printed, tagged with where it came from."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"measured() takes a real number, got {value!r}")
    return Slot({"value": value, "unit": unit, "source": source})


def is_measured(slot) -> bool:
    return isinstance(slot, dict) and slot.get("value") != UNKNOWN \
        and isinstance(slot.get("value"), (int, float))


def value_of(slot, default=None):
    """The number in a slot, or `default` when it is unknown/absent."""
    return slot["value"] if is_measured(slot) else default


# ── argv redaction ───────────────────────────────────────────────────────────
#: Flags whose VALUE is safe metadata and is kept verbatim.
_SAFE_FLAG_VALUES = frozenset({
    "--model", "--effort", "--output-format", "--format", "--dir", "-C",
    "--spec-level", "--mission-id", "--budget-s", "--project", "--engine-hint",
    "--permission-mode", "--agent",
})
#: Flags that take no value.
_VALUELESS = re.compile(r"^-{1,2}[A-Za-z0-9][A-Za-z0-9-]*$")
#: Anything that smells like a credential is redacted even if it is a flag value.
_CRED = re.compile(r"(token|secret|password|api[_-]?key|bearer|cookie|auth)", re.I)
#: Sub-commands that are part of the invocation's IDENTITY, not its content.
#: `opencode run` and `codex exec` are how you tell one launch shape from
#: another; redacting them would blind the coverage inventory to its own subject.
_SUBCOMMANDS = frozenset({"run", "exec", "chat", "review"})

#: Valueless flags the engines we launch actually accept. Combined with
#: _SAFE_FLAG_VALUES this is the COMPLETE set of argv tokens kept verbatim.
#: An allowlist rather than a shape test, because "starts with a dash" is a
#: property prompts can have too.
_KNOWN_VALUELESS_FLAGS = frozenset({
    "-p", "-v", "-h", "-c", "-s", "-i", "-f", "-u", "-m", "-C", "-o",
    "--json", "--verbose", "--print-logs", "--pure", "--continue", "--fork",
    "--share", "--thinking", "--interactive", "--auto", "--help", "--version",
    "--dangerously-skip-permissions", "--dangerously-bypass-approvals-and-sandbox",
    "--skip-git-repo-check", "--ephemeral", "--ignore-user-config",
    "--ignore-rules", "--strict-config", "--oss", "--last", "--resume",
})


def _is_known_flag(tok: str) -> bool:
    return tok in _SAFE_FLAG_VALUES or tok in _KNOWN_VALUELESS_FLAGS


def redact_argv(argv) -> list:
    """argv as EVIDENCE, never as content.

    Flags are kept (they identify the invocation — which is exactly what tells us
    whether usage was requested). Free-standing operands are the prompt, so they
    become a length, not text. A credential-shaped flag name redacts its value
    whatever the allowlist says.
    """
    out: list = []
    argv = [str(a) for a in (argv or [])]
    i = 0
    while i < len(argv):
        tok = argv[i]
        # A leading dash is NOT proof of a flag. A prompt that opens with a
        # bullet — "- fix auth: <secret>" — is a single argv token starting with
        # "-", and treating every such token as a flag stored the whole prompt
        # verbatim in the ledger. Only tokens on the known-flag list are kept as
        # written; anything else becomes a length, dash or no dash.
        if tok.startswith("-") and _is_known_flag(tok):
            out.append(tok)
            nxt = argv[i + 1] if i + 1 < len(argv) else None
            nxt_is_flag = nxt is not None and _is_known_flag(nxt)
            takes_value = (tok in _SAFE_FLAG_VALUES
                           and nxt is not None and not nxt_is_flag)
            if takes_value:
                out.append("<redacted>" if _CRED.search(tok) else nxt)
                i += 2
                continue
            if (nxt is not None and not nxt_is_flag
                    and not _VALUELESS.match(tok)):
                out.append(f"<redacted:{len(nxt)}c>")
                i += 2
                continue
            i += 1
            continue
        # argv[0] is the binary and argv[1] may be a sub-command: both identity,
        # both kept. Everything else — including an unrecognised dash-leading
        # token — is an operand, and in every engine we launch that is the
        # prompt. Length only.
        if i == 0 or (i == 1 and tok in _SUBCOMMANDS):
            out.append(tok)
        else:
            out.append(f"<redacted:{len(tok)}c>")
        i += 1
    return out


# ── the record ───────────────────────────────────────────────────────────────
#: THE DISJOINTNESS CONTRACT.
#:
#: These five common fields MUST be mutually exclusive, so that summing them is
#: arithmetic rather than wishful thinking. Providers do NOT agree on this, and
#: mapping their field names straight across is how a total silently doubles:
#:
#:   anthropic  input / output / cache_read / cache_write are already disjoint.
#:   opencode   input / output / reasoning / cache.read / cache.write are already
#:              disjoint (their own `total` is the sum, and it includes cache).
#:   codex      `input_tokens` INCLUDES `cached_input_tokens`, and
#:              `output_tokens` INCLUDES `reasoning_output_tokens`. Copying those
#:              across as-is overcounted one real 641-event session by 79,557,633
#:              tokens — 98% inflation — so the codex adapter subtracts the
#:              nested parts and the result reconciles to the provider's own
#:              `total_tokens` exactly.
#:
#: Any adapter that cannot make its fields disjoint must leave the overlapping
#: ones UNKNOWN rather than emit a number that cannot be added.
DISJOINT_TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens",
                         "cache_write_tokens", "reasoning_tokens")


@dataclass
class Usage:
    """Token COUNTS. Never content. Every slot is measured() or unknown().

    The five token fields obey DISJOINT_TOKEN_FIELDS: they never overlap, so
    aggregate sums are valid across providers that disagree about nesting.
    """
    source: str = ""
    completeness: str = ABSENT
    input_tokens: object = None
    output_tokens: object = None
    cache_read_tokens: object = None
    cache_write_tokens: object = None
    reasoning_tokens: object = None
    total_tokens: object = None
    #: PROVIDER REQUESTS: actual calls to the provider inside this one launch.
    #: Only set when an event in the log genuinely counts them. A turn count is
    #: not a request count — a turn can hide a retry, a continuation or a
    #: compaction pass — so this stays UNKNOWN rather than borrowing one.
    provider_requests: object = None
    #: MODEL TURNS: assistant turns the provider reported, in `turns`. Real and
    #: useful, and deliberately a different field from provider_requests so that
    #: neither number is read as the other. A lower bound on requests.
    model_turns: object = None
    #: Per-model split when the provider reports one (Claude's `modelUsage`).
    #: A single launch routinely bills more than one model (haiku side-calls).
    per_model: dict = field(default_factory=dict)
    #: Context window / max output the provider declared, when it declares them.
    context_window: object = None
    context_utilisation: object = None
    #: True time-to-first-TOKEN. No engine log we launch timestamps individual
    #: tokens, so in practice this stays unknown; the field exists so that a
    #: provider which does emit it has somewhere honest to land.
    ttft_s: object = None
    #: The observable neighbour of TTFT: launch (or first step) to the first
    #: event the engine emitted. A PROXY, and named like one — calling this
    #: "ttft" would quietly overstate what the log supports.
    time_to_first_event_s: object = None
    #: Why output_tokens must not be read as exact, when that is the case.
    caveats: list = field(default_factory=list)
    #: How this adapter made its fields satisfy DISJOINT_TOKEN_FIELDS. Recorded
    #: so the transformation is auditable rather than buried in a parser.
    disjoint_normalisation: str = "provider fields were already disjoint"
    #: How this provider's logs behave when ONE session spans several files.
    #: PER_INVOCATION_DELTA — each file reports only its own invocation, so
    #:   every file is a distinct billed launch and all of them count (claude
    #:   `-p --resume`: measured at 119/19/84/78/54/160 input tokens, not a
    #:   rising cumulative).
    #: CUMULATIVE_OR_REPLAY — a later file restates earlier usage, so two files
    #:   of one session overlap and summing them double-counts the prefix
    #:   (codex `total_token_usage`; an opencode resume replaying earlier parts).
    session_log_semantics: str = "PER_INVOCATION_DELTA"

    def __post_init__(self):
        for slot in ("input_tokens", "output_tokens", "cache_read_tokens",
                     "cache_write_tokens", "reasoning_tokens", "total_tokens",
                     "provider_requests", "model_turns", "context_window",
                     "context_utilisation", "time_to_first_event_s"):
            if getattr(self, slot) is None:
                setattr(self, slot, unknown(NOT_EMITTED_BY_PROVIDER))
        if self.ttft_s is None:
            self.ttft_s = unknown(
                NOT_EMITTED_BY_PROVIDER,
                detail="no launched engine log timestamps individual tokens; "
                       "see time_to_first_event_s for the observable proxy")
        if self.completeness not in COMPLETENESS:
            raise ValueError(f"completeness {self.completeness!r} invalid")


@dataclass
class Money:
    """Two different things that both look like dollars, kept apart on purpose.

    `api_price_estimate_usd` is what the CLI printed: what these tokens WOULD
    cost at list API prices. Under a flat subscription nobody is charged it.
    `charged_money_usd` is money that actually left an account. No CLI we launch
    reports it, so it is UNKNOWN here forever until a billing source is wired —
    and UNKNOWN is the honest value, not the estimate copied across.
    """
    api_price_estimate_usd: object = None
    charged_money_usd: object = None
    billing_mode: str = UNKNOWN          # "subscription" | "api" | UNKNOWN

    def __post_init__(self):
        if self.api_price_estimate_usd is None:
            self.api_price_estimate_usd = unknown(NOT_EMITTED_BY_PROVIDER)
        if self.charged_money_usd is None:
            self.charged_money_usd = unknown(
                UNOBSERVABLE_FROM_SUPERVISOR,
                detail="no launched CLI reports money actually charged; a token "
                       "count must never be converted into one")


#: How an `observed_at` was arrived at. A quota reading is only as current as
#: its timestamp, so where that timestamp came from is part of the reading.
OBSERVED_PROVIDER_EVENT = "provider_event_timestamp"
#: The provider's quota event carried no timestamp of its own (Claude's
#: `rate_limit_event` does not), so the timestamp of the nearest preceding
#: timestamped event in the SAME log is used. Accurate to the turn, and a proxy.
OBSERVED_ADJACENT_EVENT = "adjacent_log_event_proxy"
#: Last resort for an imported file: the log's mtime. A proxy for the whole
#: file, not for the event. Never presented as the provider's own timestamp.
OBSERVED_FILE_MTIME = "file_mtime_proxy"
#: No basis at all for dating this observation.
OBSERVED_NONE = ""

OBSERVED_AT_SOURCES = frozenset({OBSERVED_PROVIDER_EVENT, OBSERVED_ADJACENT_EVENT,
                                 OBSERVED_FILE_MTIME, OBSERVED_NONE})


@dataclass
class QuotaSnapshot:
    """A quota window as the PROVIDER described it, or nothing.

    Never derived from tokens. Claude's stream emits `rate_limit_event`, codex
    emits `rate_limits` inside `token_count`; those — and only those — are quota
    observations. opencode emits none, so its windows stay unknown, and "unknown
    quota" never means "unlimited".

    `observed_at` is WHEN THE PROVIDER SAW IT and is left EMPTY when nothing in
    the log can date it. It used to default to now(), which quietly re-dated
    every imported historical reading to the moment of import — so a five-hour
    window that had already reset weeks earlier was published as a current
    observation. `imported_at` is the separate, always-known fact of when we
    read the file, and the two are never conflated again.
    """
    source: str = ""
    scope: str = UNKNOWN                 # e.g. "five_hour" | "seven_day"
    status: str = UNKNOWN                # provider's own word
    utilisation: object = None           # 0..1 when the provider gives it
    resets_at: object = None             # epoch seconds, provider-supplied
    observed_at: str = ""                # provider/proxy time; "" = undatable
    observed_at_source: str = OBSERVED_NONE
    imported_at: str = ""                # when WE parsed it. Never a substitute.

    def __post_init__(self):
        if self.utilisation is None:
            self.utilisation = unknown(NOT_EMITTED_BY_PROVIDER)
        if self.resets_at is None:
            self.resets_at = unknown(NOT_EMITTED_BY_PROVIDER)
        if not self.imported_at:
            self.imported_at = utcnow()
        if self.observed_at and not self.observed_at_source:
            self.observed_at_source = OBSERVED_PROVIDER_EVENT
        if self.observed_at_source not in OBSERVED_AT_SOURCES:
            raise ValueError(
                f"observed_at_source {self.observed_at_source!r} is not one of "
                f"{sorted(OBSERVED_AT_SOURCES)}")


@dataclass
class LaunchObservation:
    """ONE launch of ONE engine process, observed from the supervisor side."""
    observation_key: str
    engine: str = UNKNOWN                # canonical: claude | glm | codex | local
    provider: str = UNKNOWN              # anthropic | zai | openai | none
    role: str = UNKNOWN                  # builder | reviewer | research | recovery…
    # identity / linkage
    run_id: str = ""
    project: str = ""
    milestone: str = ""
    mission: str = ""
    #: Which ATTEMPT of the mission this was, when the launch site knows it.
    #: 0 means the launch site did not know — it is not "attempt zero".
    attempt: int = 0
    #: Which launch this was WITHIN one dispatch (1-based). A mission that
    #: builds, repairs and rebuilds makes three launches inside one attempt;
    #: collapsing the two counters loses exactly that distinction.
    launch_seq: int = 0
    attempt_key: str = ""
    contract_id: str = ""
    #: Links a reviewer/repair observation back to the build it judges, so nested
    #: work is attributable WITHOUT being added twice into the parent's totals.
    parent_observation_key: str = ""
    lineage: str = ""                    # PRIMARY | REVIEW | REPAIR | TEST | RECOVERY | CTO_OVERHEAD
    # what ran
    model_requested: str = ""
    model_observed: str = ""
    #: The PROVIDER's own session id. Identity across a resume, which is what
    #: makes replayed events foldable instead of double-counted.
    session_id: str = ""
    harness_version: str = ""            # e.g. claude_code_version
    control_sha: str = ""
    product_sha: str = ""
    argv_redacted: list = field(default_factory=list)
    pid: object = None
    cwd: str = ""
    # when
    started_at: str = ""
    ended_at: str = ""
    wall_time_s: object = None
    api_time_s: object = None            # provider-reported in-API time
    phase_times_s: dict = field(default_factory=dict)  # queue/setup/build/test/review/…
    # outcome
    rc: object = None
    exit_class: str = ""
    stop_reason: str = ""
    terminal_reason: str = ""
    killed_by_signal: object = None
    retries: int = 0
    rate_limit_waits: int = 0
    task_type: str = ""
    difficulty_proxy: dict = field(default_factory=dict)  # explicitly heuristic
    # measurement
    usage: object = None
    money: object = None
    quota: object = None
    #: Minimal provider-native usage events kept verbatim so aggregates can be
    #: recomputed after a schema change. Allowlisted keys only — never content.
    raw_usage_events: list = field(default_factory=list)
    #: Where this record came from, honestly: LIVE | BACKFILL_BEST_EFFORT.
    provenance: str = "LIVE"
    #: Which campaign/time window this launch belongs to. The current campaign
    #: and an imported historical corpus answer different questions, and a total
    #: that silently spans both answers neither.
    window: str = "CURRENT_CAMPAIGN"
    log_path: str = ""
    recorded_at: str = ""

    def __post_init__(self):
        if self.usage is None:
            self.usage = Usage()
        if self.money is None:
            self.money = Money()
        if self.quota is None:
            self.quota = QuotaSnapshot()
        if self.wall_time_s is None:
            self.wall_time_s = unknown(NO_LOG_CAPTURED)
        if self.api_time_s is None:
            self.api_time_s = unknown(NOT_EMITTED_BY_PROVIDER)
        if not self.recorded_at:
            self.recorded_at = utcnow()

    def as_dict(self) -> dict:
        d = asdict(self)
        d["schema"] = SCHEMA
        return d


def observation_key(*, engine: str, session_id: str = "", attempt_key: str = "",
                    step: str = "", pid=None, started_at: str = "",
                    launch_seq: int = 0, content_fingerprint: str = "") -> str:
    """Stable idempotency key for ONE LAUNCH — not one session.

    The distinction is load-bearing and was measured, not assumed. Six files in
    this campaign are `claude -p --resume` continuations of session
    `e87e73bd…`, and each one reports its OWN usage (119/19/84/78/54/160 input
    tokens, not a rising cumulative). A session is therefore many billed
    launches, and keying on the session id collapsed six real launches into one
    — discarding five launches' worth of spend.

    Meanwhile thirteen copies of one `stdout.jsonl` exist across sandbox
    snapshots of the same evidence. Those ARE one launch, and must fold.

    So:
      * live capture keys on the SUPERVISOR's identity for the execve —
        attempt, step, sequence, pid, start instant — which is fixed for a given
        launch however many times the same observation is re-persisted;
      * imported logs key on the session plus a CONTENT fingerprint, so
        duplicate copies of one log collapse while distinct invocations of one
        session stay distinct.
    """
    if content_fingerprint:
        basis = f"{engine}|session={session_id}|content={content_fingerprint}"
    else:
        basis = (f"{engine}|attempt={attempt_key}|step={step}|seq={launch_seq}"
                 f"|pid={pid}|t={started_at}")
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]


def content_fingerprint(*, first_event_at: str = "", totals=()) -> str:
    """A launch's identity from what it actually consumed.

    Two byte-identical copies of one log produce the same fingerprint; two
    invocations of the same session produce different ones, because they start
    at different instants and spend different amounts.
    """
    parts = [str(first_event_at)] + [str(t) for t in totals]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


# ── raw-event allowlist ──────────────────────────────────────────────────────
#: The ONLY keys copied out of a provider event into raw_usage_events. Anything
#: not on this list — text, thinking, signatures, tool arguments, file contents,
#: environment — never enters the telemetry ledger.
RAW_EVENT_ALLOWLIST = frozenset({
    "schema_version", "event", "provider", "model", "request_id", "message_id",
    "timestamp", "input_tokens", "output_tokens", "cache_read_tokens",
    "cache_write_tokens", "reasoning_tokens", "total_tokens", "stop_reason",
    "service_tier", "step_reason", "cost_reported", "rate_limit_status",
    "rate_limit_scope", "rate_limit_utilisation", "rate_limit_resets_at",
    # Whether this event's counters are CUMULATIVE for the session or a delta.
    # Without it a later re-aggregation cannot know that summing codex's 641
    # token_count snapshots multiplies the session — the allowlist was silently
    # stripping the one field that prevents that mistake.
    "cumulative",
})


def allowlist_event(event: dict) -> dict:
    """Keep the counters, drop everything else. Enforced, not merely intended."""
    return {k: v for k, v in (event or {}).items() if k in RAW_EVENT_ALLOWLIST}


def dumps(record) -> str:
    payload = record.as_dict() if hasattr(record, "as_dict") else dict(record)
    return json.dumps(payload, sort_keys=True, default=str)
