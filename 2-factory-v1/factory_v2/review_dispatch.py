"""Actually invoke multi-AI review, instead of only announcing it in policy.

`model_router.route(..., task_kind=TaskKind.REVIEW)` and the restricted
read-only GLM/opencode launch machinery (`GLM_REVIEW_AGENT`,
`_restricted_glm_argv`) already existed, but nothing in the dispatch path
ever called either — the dispatcher only ever launched the autopilot build
script. A routing policy could "announce" a reviewer that never ran.

`dispatch_review` is the missing call site: it routes, launches the
restricted read-only engine, and durably records the reviewer identity, the
exact SHA reviewed, a structured verdict and telemetry — mirroring the
discipline `launch_gate.AttemptAccount` already applies to build attempts,
so a review call is accounted for exactly as seriously as a build is.

No unverified fallback, ever: if the routed reviewer's restricted launch
cannot be verified (opencode/codex missing, agent config absent, required
flags undocumented, quota exhausted), this raises `ReviewUnavailable` and
returns nothing to substitute for it. The caller must land in a typed state
— never relaunch the Claude/GLM builder to compensate for a review that
could not run, and never launch an unrestricted/writable session under the
"reviewer" label.

Codex as reviewer (CODEX-LOT-C37, 2026-09-12): Codex now has a verified
read-only launch (`model_router._restricted_codex_argv` /
`codex_restricted_flag_report()`, CODEX-LOT-C1/C15) and the owner roster
(projects/factory/SPEC.md/STRATEGY.md) names Codex the default reviewer —
but this stays refused (`CODEX_REVIEW_FORBIDDEN`) UNLESS the caller passes
`codex_reviewer_enabled=True` *and* the local CLI actually verifies the
restricted flags right now. The opt-in exists so this module's own default
behaviour never changes merely because a workstation happens to have a
working `codex` binary on PATH — only a caller that has deliberately wired
Codex into its reviewer roster (factory_v2.runtime's multi-AI review loop)
ever sets it. Unset (the default): identical to the old permanent
prohibition, byte for byte.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "factory" / "bin"))
import model_router as _mr  # noqa: E402
from ._io import _now

REVIEW_LEDGER_NAME = "review_accounts.jsonl"
UNKNOWN = "UNKNOWN"

_ACCEPT_MARKERS = ("ACCEPT", "PASS", "APPROVE")
_BLOCK_MARKERS = ("BLOCK", "REJECT", "FAIL", "INVALID")


class ReviewUnavailable(Exception):
    """No review ran. Carries why, so a caller can land in a typed state
    instead of guessing — and never as an excuse to relaunch the builder."""

    def __init__(self, reason: str, detail: dict | None = None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or {}


@dataclass
class ReviewAccount:
    """One review call, measured from outside the process that ran it —
    the same discipline `launch_gate.AttemptAccount` applies to builds."""
    schema: str = "review-account/v1"
    mission: str = ""
    milestone: str = ""
    spec_level: str = ""
    reviewed_sha: str = ""
    engine: str = UNKNOWN
    model: str = UNKNOWN
    role: str = UNKNOWN
    read_only: bool = True
    started_at: str = ""
    ended_at: str = ""
    duration_s: "float | None" = None
    rc: "int | None" = None
    verdict: str = ""            # ACCEPT | BLOCK | ERROR
    tokens_in: object = UNKNOWN
    tokens_out: object = UNKNOWN
    reason: str = ""
    #: relative path of the full reviewer transcript (evidence/reviews/…), 2026-09-18
    transcript: str = ""
    #: HMAC seal (see seal_account()/verify_seal() below) over the fields
    #: that make a verdict meaningful — computed with the SAME owner/
    #: supervisor key `recovery_allocation._owner_row_seal` already uses.
    #: Empty when no key is configured in THIS process at persist time
    #: (fail-closed at the READING end: an unsealed row is never trusted as
    #: sealed just because the field is absent).
    seal: str = ""




def review_ledger_path(project_dir) -> Path:
    return Path(project_dir) / "evidence" / REVIEW_LEDGER_NAME


#: Fields a seal actually covers — enough to make the verdict/identity/SHA
#: triple non-forgeable without the key, not the whole row (timings/tokens
#: are forensic detail, not the claim a consumer trusts).
_SEAL_FIELDS = ("mission", "milestone", "reviewed_sha", "engine", "read_only",
               "verdict", "started_at")


def _owner_key() -> "bytes | None":
    """The same owner/supervisor key `recovery_allocation.owner_key()`
    guards its own grants with — a key never shipped into a mission's own
    isolated worktree, so a builder process cannot compute a seal a reader
    would accept. Lazy import (review_dispatch must not hard-depend on
    recovery_allocation's own import graph), fails to None on any error —
    the SAME fail-closed posture `verify_seal()` below already applies to a
    missing key (no key ⇒ nothing verifies).
    """
    try:
        from . import recovery_allocation as _ra
    except Exception:
        return None
    try:
        return _ra.owner_key()
    except Exception:
        return None


def owner_key_available() -> bool:
    """True iff THIS process has an owner/supervisor seal key configured —
    i.e. `verify_seal()` can ever return True here, for any row. CODEX-LOT-
    C41: a caller grading a batch of ACCEPTs (factory_v2.runtime's hardened
    multi-AI review path) needs to tell "no key configured at all, so a
    whole batch is unprovable by construction" (REVIEW_SEAL_KEY_MISSING)
    apart from "a key IS configured but this particular row does not
    verify against it" (REVIEW_ACCOUNT_UNPROVEN) — two different root
    causes that should not be reported as the same event.
    """
    return _owner_key() is not None


def _seal_payload(row: dict) -> bytes:
    payload = {k: row.get(k) for k in _SEAL_FIELDS}
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def seal_account(row: dict, key: "bytes | None" = None) -> str:
    """HMAC-SHA256 hex digest over `_SEAL_FIELDS`, or "" when no owner key is
    configured in this process. `row` is a plain dict (asdict(account) or a
    JSON line already read back) so this is usable on both sides: at write
    time (this module) and at read time (a verifier)."""
    key = key if key is not None else _owner_key()
    if not key:
        return ""
    return hmac.new(key, _seal_payload(row), hashlib.sha256).hexdigest()


def verify_seal(row: dict, key: "bytes | None" = None) -> bool:
    """True iff `row["seal"]` matches a fresh recomputation under the SAME
    owner key. False (never True) when no key is configured here — an
    unkeyed reader can prove nothing, so it must never count an ACCEPT it
    cannot verify. Comparison is constant-time (hmac.compare_digest), same
    discipline recovery_allocation's own seal checks use."""
    key = key if key is not None else _owner_key()
    if not key:
        return False
    expect = seal_account(row, key=key)
    return bool(expect) and hmac.compare_digest(expect, str(row.get("seal") or ""))


def verify_supervised_launch(row: dict, project_dir) -> bool:
    """True iff `row` (a `review_accounts.jsonl` line) cross-references a
    REAL supervised launch recorded INDEPENDENTLY by `launch_gate` in
    `evidence/attempt_accounts.jsonl` — the same ledger every model call,
    review or build alike, gets a terminal row in from OUTSIDE the process
    that ran it.

    CODEX-LOT-C39 counter-audit: `evidence/review_accounts.jsonl` is written
    by `_persist()`, which any process able to import `factory_v2.
    review_dispatch` — including a mission subprocess with filesystem
    access to the project directory — could call directly to fabricate an
    ACCEPT line without ever launching a reviewer. `dispatch_review()`'s own
    real launches ALSO write a terminal attempt-ledger row for the same call
    (`launch_gate.open_account`/`close_account`, `mission=f"multi-ai:
    {milestone}"`, `candidate_sha=reviewed_sha`, `engine_execution={
    "engine_id": engine, ...}`, `mission_pass=(verdict=="ACCEPT")`) — a row
    a forged `review_accounts.jsonl` line has no reason to also have, since
    producing it requires actually going through `launch_gate.open_account`/
    `close_account`. A review account with no matching TERMINAL, PASSED
    attempt-ledger row for the SAME (milestone, engine, reviewed_sha) is not
    trusted as a real launch.

    Deliberately duplicated (not imported) from `audit_observation_producer.
    _review_account_supervised()` — the SAME criteria, so the two call sites
    can never disagree on what "proven" means, but `factory_v2.runtime`'s
    settlement path never depends on an audit-specific private helper.
    """
    try:
        from .launch_gate import _accounts as _attempt_accounts
    except Exception:
        return False
    try:
        rows = _attempt_accounts(Path(project_dir))
    except Exception:
        return False
    milestone = row.get("milestone")
    engine = row.get("engine")
    sha = row.get("reviewed_sha")
    if not (milestone and engine and sha):
        return False
    for a in rows:
        if (a.get("milestone") == milestone
                and a.get("engine") == engine
                and a.get("candidate_sha") == sha
                and a.get("phase") == "TERMINAL"
                and bool(a.get("mission_pass")) is True
                and str(a.get("mission") or "").startswith("multi-ai:")):
            return True
    return False


def _persist(account: ReviewAccount, project_dir) -> None:
    p = review_ledger_path(project_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    row = asdict(account)
    row["seal"] = seal_account(row)
    account.seal = row["seal"]
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def read_reviews(project_dir) -> list:
    p = review_ledger_path(project_dir)
    if not p.is_file():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


_VERDICT_LINE_RE = re.compile(r"\b(ACCEPT|BLOCK)\b")


def _measure_review_launch(project_dir, *, argv, stdout, rc, started_at: str,
                           wall_s: float, choice, project: str, milestone: str,
                           mission_id: str, attempt_key: str, reviewed_sha: str,
                           observed: dict, call_reason: str = "") -> list:
    """FACTORY_TOKEN_TRUTH_V1 — meter one reviewer launch from its stdout.

    Persists the raw stream (`evidence/streams/<attempt_key>.jsonl`), runs the
    adapter `detect_format(argv)` picks, and merges the MEASURED numbers into
    `observed` (the engine_execution the terminal ledger row is projected
    from). Returns the observation dicts to persist with the account. A
    launch that produced no stdout (timeout, launch failure) is typed by the
    adapter — LOG_TRUNCATED / NO_LOG_CAPTURED — never a zero. Never raises:
    a metering failure degrades coverage, not the review.
    """
    try:
        from .telemetry import capture as _capture
        from .telemetry.observe import REVIEW, observe_launch, to_engine_execution
        path = _capture.stream_path(project_dir, attempt_key)
        if stdout is not None:
            _capture.persist_stream(path, stdout)
        obs = observe_launch(
            argv=list(argv), stdout=stdout, log_path=str(path or ""),
            started_at=started_at, wall_time_s=wall_s, rc=rc,
            exit_class=("SUCCESS" if rc == 0 else "FAILURE") if rc is not None
            else (call_reason.split(":")[0] or "KILL"),
            engine=str(choice.engine), model_requested=str(choice.model or ""),
            role="reviewer", lineage=REVIEW, project=project,
            milestone=milestone, mission=mission_id, attempt=1,
            attempt_key=attempt_key, product_sha=reviewed_sha,
            task_type="review", pid=observed.get("pid"), provenance="LIVE")
        observed.update(to_engine_execution(obs, base=observed))
        return [obs.as_dict()]
    except Exception:
        return []

_STRUCTURED_KEYS = ("type", "item", "message", "part")


def _looks_structured_transcript(stdout: str) -> bool:
    """L4_resilience_review-010: True when stdout carries engine stream events
    (JSON object lines with type/item/message/part keys). When the output is
    such a transcript, a verdict may ONLY come from the reviewer's own text —
    never from a tool execution's output."""
    for ln in (stdout or "").splitlines():
        ln_s = ln.strip()
        if not ln_s.startswith("{"):
            continue
        try:
            ev = json.loads(ln_s)
        except (ValueError, TypeError):
            continue
        if isinstance(ev, dict) and any(k in ev for k in _STRUCTURED_KEYS):
            return True
    return False


def _agent_text_lines(stdout: str) -> list:
    """Text the model itself said, from a stream-json transcript (codex `--json`
    item.completed/agent_message, claude stream-json assistant content, opencode
    `type: text` parts). Empty when the output is not such a stream — the caller
    then parses raw text."""
    out: list = []
    for ln in (stdout or "").splitlines():
        ln_s = ln.strip()
        if not ln_s.startswith("{"):
            continue
        try:
            ev = json.loads(ln_s)
        except (ValueError, TypeError):
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("type") == "text":
            # opencode assistant text events: {"type":"text","part":{"text":...}}
            part = ev.get("part") if isinstance(ev.get("part"), dict) else None
            t = (part or {}).get("text") if part is not None else ev.get("text")
            if isinstance(t, str) and t.strip():
                out.append(t.strip())
            continue
        item = ev.get("item") if isinstance(ev.get("item"), dict) else None
        if item and item.get("type") in ("agent_message", "assistant_message", "message"):
            t = item.get("text") or item.get("content") or ""
            if isinstance(t, str) and t.strip():
                out.append(t.strip())
            continue
        msg = ev.get("message") if isinstance(ev.get("message"), dict) else None
        if ev.get("type") == "assistant" and msg:
            for c in (msg.get("content") or []):
                if isinstance(c, dict) and c.get("type") == "text" and str(c.get("text") or "").strip():
                    out.append(str(c["text"]).strip())
        elif ev.get("type") == "result" and isinstance(ev.get("result"), str) and ev["result"].strip():
            out.append(ev["result"].strip())
    return out


def _extract_verdict(stdout: str) -> str:
    """The reviewer is asked to reply `ACCEPT` or `BLOCK` + a one-line reason.

    2026-09-17: the previous rule flagged BLOCK when ANY block marker appeared
    ANYWHERE in the output — a review saying "no failing test, ACCEPT" was a
    BLOCK because of the word "fail" (Codex, 33 s, reason ''). The verdict is
    now the LAST line that carries an explicit ACCEPT/BLOCK word (the reply
    format); a `{"verdict": ...}` JSON line wins when present. Only when no
    explicit verdict word exists do the loose markers decide, and a BLOCK
    marker still wins that fallback (fail-closed).
    """
    text = stdout or ""
    # stream-json engines (codex --json, claude stream-json): the reviewer's
    # own words are in agent_message / assistant text items — a BLOCK word
    # inside a command it ran or a file it read is not its verdict. When such
    # items exist, ONLY their text is parsed.
    spoken = _agent_text_lines(text)
    if spoken:
        text = "\n".join(spoken)
    elif _looks_structured_transcript(text):
        # L4_resilience_review-010: structured transcript in which the reviewer
        # NEVER spoke (only command_execution / tool events) — a tool's output is
        # not a conclusion. Parsing the raw JSON used to turn a command
        # displaying ACCEPT into an ACCEPT verdict. Fail closed instead.
        return "ERROR"
    # structured line (engines that stream JSON): last "verdict" field wins
    last_json = ""
    for ln in text.splitlines():
        ln_s = ln.strip()
        if ln_s.startswith("{") and '"verdict"' in ln_s:
            try:
                v = str((json.loads(ln_s) or {}).get("verdict") or "").upper()
            except (ValueError, TypeError):
                v = ""
            if v in ("ACCEPT", "BLOCK"):
                last_json = v
    if last_json:
        return last_json
    explicit = ""
    for ln in text.splitlines():
        found = _VERDICT_LINE_RE.findall(ln.upper())
        if found:
            explicit = found[-1]
    if explicit:
        return explicit
    up = text.upper()
    if any(m in up for m in _BLOCK_MARKERS):
        return "BLOCK"
    if any(m in up for m in _ACCEPT_MARKERS):
        return "ACCEPT"
    return "ERROR"


def _builder_engine_from_runrecords(milestone: str, reviewed_sha: str) -> "str | None":
    """Best-effort: which engine most recently BUILT `reviewed_sha` for
    `milestone`, read from the durable runrecords ledger (factory_v2.
    runrecords — `record_attempt`/`record_settlement`, written after every
    mission execution with `milestone`, `engine`, `task_type` and
    `candidate_sha` fields).

    CODEX_REVIEW_C2_C4 finding 4: dispatch_review previously routed a REVIEW
    task without ever telling model_router.route() which engine built the
    work under review, so `exclude_engines` (builder != reviewer) was never
    threaded through this, the one production call site that matters. No
    receipt object is passed down from the caller today (factory_v2.runtime's
    multi-AI review call site has the build receipt in scope but does not
    pass it here — see dispatch_review()'s `builder_engine` parameter for the
    preferred, precise path) — this is the fallback used when a caller does
    not supply `builder_engine` explicitly.

    Matches on (milestone, candidate_sha) so a stale/older build of the SAME
    milestone under a DIFFERENT sha never excludes an engine that did not
    build what is actually being reviewed. Returns None (no exclusion, not a
    guess) when nothing matches — including when the runrecords ledger does
    not exist yet, or `record_attempt`/`record_settlement` never carried a
    `task_type` this function recognizes as a build.
    """
    try:
        from . import runrecords as _rr
    except ImportError:
        return None
    try:
        matches = [r for r in _rr.read_runs()
                  if r.get("milestone") == milestone
                  and r.get("candidate_sha") == reviewed_sha
                  and r.get("task_type") in ("build", "repair")]
    except Exception:
        return None
    if not matches:
        return None
    matches.sort(key=lambda r: str(r.get("recorded_at") or ""))
    engine = matches[-1].get("engine")
    return str(engine) if engine else None


def engine_quota_refused(project_dir, milestone: str, engine: str) -> "str | None":
    """None when `engine` may be dispatched for a review right now; otherwise
    the quota_observer refusal reason (CODEX-LOT-C37).

    `model_router.route()`'s OWN quota-aware fallback only ever rations
    WRITER launches — a bounded read-only review is cheap by construction,
    so route() leaves it to run unconditionally (see model_router.route()'s
    "quota-aware fallback" comment: `if not read_only: ...`). That is fine
    for a SINGLE review, but a caller orchestrating several DISTINCT
    reviewers over the same exact SHA (factory_v2.runtime's multi-AI review
    loop) needs to decide, per candidate engine, whether spending a dispatch
    on it is honest right now — an engine already at/above
    quota_observer.REFUSE_AT stays untried, typed, rather than launched
    anyway or silently swapped for another engine's identity. Fail-open on
    any lookup error (same posture `model_router._quota_engine_allowance`
    itself already has): "unknown" is never "refuse".
    """
    project = ""
    try:
        from .recovery_allocation import observed_binding as _observed_binding
        project = str(_observed_binding(project_dir, milestone).get("project") or "")
    except Exception:
        project = ""
    try:
        allowed, reason = _mr._quota_engine_allowance(engine, project or None)
    except Exception:
        return None
    return None if allowed else reason


def dispatch_review(project_dir, *, repo: Path, mission_id: str, milestone: str,
                    spec_level, reviewed_sha: str, prompt: str,
                    runner=subprocess.run, timeout: int = 900,
                    route=None, cli_args=None,
                    builder_engine: "str | None" = None,
                    engine_hint: "str | None" = None,
                    codex_reviewer_enabled: bool = False) -> ReviewAccount:
    """Route, launch the restricted read-only reviewer, and durably record it.

    `route`/`cli_args` default to `model_router.route`/`model_router.cli_args`
    — overridable so tests can assert on the exact argv without spawning a
    real subprocess.

    `builder_engine`: the engine that built `reviewed_sha`, if the caller
    already knows it (e.g. from the mission's own receipt/engine_executions —
    see factory_v2.runtime's advance()/`receipt.get("engine_executions")`
    reads for exactly this identity). Passed to model_router.route() as
    `exclude_engines` so builder != reviewer (FACTORY_LAW) is enforced for
    THIS review, not only the routing table's own default. Default None:
    falls back to `_builder_engine_from_runrecords()` — a best-effort lookup
    from the durable runrecords ledger by (milestone, reviewed_sha) — so the
    invariant still applies even when a caller does not thread the identity
    through explicitly; it is a strictly weaker (may miss a match) but never
    incorrect substitute, since it only ever excludes an engine actually
    recorded as having built this exact sha.

    `engine_hint`: forwarded to `model_router.route()`'s own `engine_hint`
    parameter — lets a caller orchestrating SEVERAL distinct reviewers over
    the same SHA (factory_v2.runtime's multi-AI review loop, CODEX-LOT-C37)
    ask for a SPECIFIC engine on this call, instead of accepting whatever the
    (spec_level, task_kind) routing table would pick by default. Default
    None: identical to every existing call site, unchanged.

    `codex_reviewer_enabled`: opt-in only (default False, CODEX-LOT-C37).
    Codex now has a verified read-only launch
    (`model_router._restricted_codex_argv`/`codex_restricted_flag_report()`),
    but routing Codex to `engine="codex"` here still raises
    `CODEX_REVIEW_FORBIDDEN` UNLESS this is True — so this module's own
    default behaviour cannot change just because a workstation happens to
    have a working `codex` binary on PATH. Only a caller that has
    deliberately wired Codex into its reviewer roster ever sets this.
    """
    route = route or _mr.route
    cli_args = cli_args or _mr.cli_args
    if builder_engine is None:
        builder_engine = _builder_engine_from_runrecords(milestone, reviewed_sha)
    exclude_engines = [builder_engine] if builder_engine else None

    started_at = _now()
    # engine_hint is only ever added to the call when a caller actually asked
    # for it: every existing route()/fake_route() call site (tests included)
    # is invoked with the exact same (spec_level, task_kind, project=,
    # exclude_engines=) signature as before — an unconditional extra keyword
    # would break every fake_route() that does not accept **kwargs.
    _route_kwargs = {"project": None, "exclude_engines": exclude_engines}
    if engine_hint is not None:
        _route_kwargs["engine_hint"] = engine_hint
    try:
        choice = route(spec_level, _mr.TaskKind.REVIEW, **_route_kwargs)
    except _mr.ReviewerEqualsBuilder as exc:
        account = ReviewAccount(
            mission=mission_id, milestone=milestone, spec_level=str(spec_level),
            reviewed_sha=reviewed_sha, engine=builder_engine or UNKNOWN,
            read_only=True, started_at=started_at, ended_at=started_at,
            duration_s=0.0, verdict="ERROR",
            reason=f"REVIEWER_EQUALS_BUILDER: {exc}")
        _persist(account, project_dir)
        raise ReviewUnavailable("REVIEWER_EQUALS_BUILDER",
                                {"builder_engine": builder_engine}) from exc

    quota_reason = engine_quota_refused(project_dir, milestone, choice.engine)
    if quota_reason:
        account = ReviewAccount(
            mission=mission_id, milestone=milestone, spec_level=str(spec_level),
            reviewed_sha=reviewed_sha, engine=choice.engine, model=choice.model,
            role=choice.role, read_only=True, started_at=started_at,
            ended_at=started_at, duration_s=0.0, verdict="ERROR",
            reason=f"REVIEW_ENGINE_QUOTA_REFUSED: {quota_reason}")
        _persist(account, project_dir)
        raise ReviewUnavailable("REVIEW_ENGINE_QUOTA_REFUSED",
                                {"engine": choice.engine, "detail": quota_reason})

    account = ReviewAccount(
        mission=mission_id, milestone=milestone, spec_level=str(spec_level),
        reviewed_sha=reviewed_sha, engine=choice.engine, model=choice.model,
        role=choice.role, read_only=choice.read_only, started_at=started_at)

    if not choice.read_only:
        # A "review" that can write is not a review — FACTORY_LAW builder !=
        # reviewer. Refuse before any launch, never silently downgrade it.
        account.ended_at = account.started_at
        account.duration_s = 0.0
        account.verdict = "ERROR"
        account.reason = "ROUTED_ENGINE_NOT_READ_ONLY"
        _persist(account, project_dir)
        raise ReviewUnavailable("ROUTED_ENGINE_NOT_READ_ONLY",
                                {"engine": choice.engine})

    if str(choice.engine).lower() == "codex":
        # CODEX-LOT-C37: Codex may act as reviewer ONLY when a caller has
        # deliberately opted in (codex_reviewer_enabled=True) AND the
        # restricted read-only launch verifies against the LOCAL codex CLI
        # right now (codex_restricted_flag_report() — same probe
        # _restricted_codex_argv() itself relies on, so a "verified" here can
        # never diverge from what cli_args() is about to build). Both gates
        # fail closed to the SAME typed refusal as before
        # (CODEX_REVIEW_FORBIDDEN) — this is a narrowing of when Codex is
        # forbidden, never a new way to silently launch it: no opt-in, or an
        # opt-in with a CLI that does not verify, is refused exactly as
        # permanently as before.
        _codex_unverified = ""
        if codex_reviewer_enabled:
            _codex_unverified = _mr.codex_restricted_flag_report()
        if not codex_reviewer_enabled or _codex_unverified:
            account.ended_at = account.started_at
            account.duration_s = 0.0
            account.verdict = "ERROR"
            account.reason = "CODEX_REVIEW_FORBIDDEN"
            _persist(account, project_dir)
            raise ReviewUnavailable("CODEX_REVIEW_FORBIDDEN",
                                    {"engine": choice.engine,
                                     "codex_reviewer_enabled": codex_reviewer_enabled,
                                     "unverified": _codex_unverified})
        # Verified and opted in: fall through to the common launch path below
        # — cli_args() builds the SAME _restricted_codex_argv() this check
        # just verified against, never a second, looser probe.

    try:
        argv = cli_args(choice, Path(repo), prompt)
    except _mr.ReadOnlyLaunchNotImplemented as exc:
        account.ended_at = _now()
        account.duration_s = 0.0
        account.verdict = "ERROR"
        account.reason = f"READ_ONLY_LAUNCH_NOT_IMPLEMENTED: {exc}"
        _persist(account, project_dir)
        raise ReviewUnavailable("READ_ONLY_LAUNCH_NOT_IMPLEMENTED",
                                {"detail": str(exc)}) from exc

    t0 = time.monotonic()
    rc = None
    stdout = ""
    call_reason = ""
    # ── common launch gate + accounting + telemetry ─────────────────────────
    # A review call spends REAL model quota, so it passes through the SAME
    # gate as every other model launch: fail-closed check_before_launch
    # BEFORE any spawn (a blocked gate is a typed ReviewUnavailable, never a
    # silent skip), a durable OPEN accounting row before execve under a
    # crash-unique review attempt key, lifecycle telemetry (OPEN before /
    # TERMINAL in finally), and exactly one terminal accounting row. The
    # attempt key makes the launch reconcilable: a crash mid-review closes at
    # boot (PROCESS_LOST + telemetry INTERRUPTED) like every other launch.
    from types import SimpleNamespace as _MissionStub
    from . import launch_gate as _launch_gate
    from .telemetry import gate as _telemetry_gate
    _rv_attempt_key = f"review:{milestone}:{reviewed_sha[:16]}:{time.time_ns()}"
    _rv_observed = {"engine_id": choice.engine, "model": choice.model,
                    "observed": True, "argv": list(argv), "role": "reviewer"}
    try:
        _launch_gate.check_before_launch(
            project_dir, mission_id=mission_id, milestone=milestone,
            contract_frozen=True,
            expected_result_defined=True,
            independent_proof_defined=True,
            mission=_MissionStub(executor={"type": "model_review",
                                           "task_file": milestone}),
            own_lease_generation=None,
            supervisor_proved_no_model=False)
    except _launch_gate.LaunchBlocked as _blocked:
        account.ended_at = _now()
        account.duration_s = 0.0
        account.verdict = "ERROR"
        account.reason = f"LAUNCH_GATE_BLOCKED: {_blocked.reason}"
        _persist(account, project_dir)
        raise ReviewUnavailable("LAUNCH_GATE_BLOCKED",
                                {"reason": _blocked.reason,
                                 "detail": _blocked.detail}) from _blocked
    # the accounting row must carry the project identity the allocation
    # journal derives from PROJECT_STATE — not the milestone name
    try:
        from .recovery_allocation import observed_binding as _observed_binding
        _rv_project = str(_observed_binding(
            project_dir, milestone).get("project") or "")
    except Exception:
        _rv_project = ""
    _rv_account = _launch_gate.open_account(
        project_dir, attempt_key=_rv_attempt_key,
        project=_rv_project, milestone=milestone, mission=mission_id,
        attempt=1, engine=choice.engine, model=choice.model,
        consumes_model_quota=True)
    _rv_closed = {"done": False}
    _rv_telemetry: list = []   # FACTORY_TOKEN_TRUTH_V1: the measured launch observation(s)

    def _rv_close(exit_class: str, *, mission_pass: bool) -> None:
        """Idempotent terminal accounting row: exactly one per review call."""
        if _rv_closed["done"]:
            return
        _rv_closed["done"] = True
        _launch_gate.close_account(
            project_dir, _rv_account, exit_class=exit_class,
            mission_pass=mission_pass,
            duration_s=round(time.monotonic() - t0, 3),
            candidate_sha=reviewed_sha,
            engine_execution=_rv_observed,
            result=_MissionStub(telemetry_observations=list(_rv_telemetry)))

    try:
        with _telemetry_gate.metered_launch(
                project_dir, attempt_key=_rv_attempt_key,
                mission=mission_id, milestone=milestone,
                engine=choice.engine, model=choice.model) as _rv_meter:
            _rv_observed["argv"] = list(argv)
            try:
                r = runner(argv, cwd=str(repo), capture_output=True,
                           text=True, timeout=timeout)
                rc = r.returncode
                stdout = r.stdout or ""
            except subprocess.TimeoutExpired:
                call_reason = "TIMEOUT"
            except OSError as exc:
                call_reason = f"LAUNCH_FAILED: {exc}"
            _rv_observed["pid"] = getattr(r, "pid", None) if rc is not None \
                else None
            # FACTORY_TOKEN_TRUTH_V1: a review spends real quota — measure it
            # with the production adapters (chosen from argv) and keep its raw
            # stream under evidence/streams/<attempt_key>.jsonl. Before this,
            # every review row closed with usage UNKNOWN although the stdout
            # carrying the counters was right here.
            _rv_telemetry[:] = _measure_review_launch(
                project_dir, argv=argv, stdout=stdout if rc is not None else None,
                rc=rc, started_at=started_at,
                wall_s=round(time.monotonic() - t0, 3), choice=choice,
                project=_rv_project, milestone=milestone, mission_id=mission_id,
                attempt_key=_rv_attempt_key, reviewed_sha=reviewed_sha,
                observed=_rv_observed, call_reason=call_reason)
            for _fld, _dst in (("input_tokens", "tokens_in"), ("output_tokens", "tokens_out")):
                if isinstance(_rv_observed.get(_fld), (int, float)):
                    setattr(account, _dst, _rv_observed[_fld])
            _rv_meter.update(
                outcome=("SUCCESS" if rc == 0 else "FAILURE")
                if rc is not None else "INTERRUPTED",
                rc=rc, engine_execution=_rv_observed)
    except BaseException:
        _rv_close("KILL", mission_pass=False)
        raise
    duration = round(time.monotonic() - t0, 3)

    account.duration_s = duration
    account.rc = rc
    account.ended_at = _now()
    if rc is None:
        account.verdict = "ERROR"
        account.reason = call_reason or "NO_RC_OBSERVED"
    elif rc != 0:
        account.verdict = "ERROR"
        account.reason = f"NONZERO_RC_{rc}"
    else:
        account.verdict = _extract_verdict(stdout)
        if not account.reason:
            # keep the reviewer's own one-line reason (the last line carrying
            # the verdict word, from what the model SAID) — a BLOCK with
            # reason '' is unanswerable
            _spoken = _agent_text_lines(stdout) or (stdout or "").splitlines()
            for ln in "\n".join(_spoken).splitlines():
                if _VERDICT_LINE_RE.search(ln.upper()):
                    account.reason = ln.strip()[:300]
    # 2026-09-18: the full transcript is evidence — without it a BLOCK cannot be
    # answered (two mis-parsed reviews were unexplainable after the fact)
    try:
        _rdir = Path(project_dir) / "evidence" / "reviews" / str(milestone)
        _rdir.mkdir(parents=True, exist_ok=True)
        _stamp = (account.started_at or _now()).replace(":", "").replace("-", "")
        (_rdir / f"{account.engine}_{_stamp}.log").write_text(stdout or "", encoding="utf-8")
        account.transcript = str((_rdir / f"{account.engine}_{_stamp}.log").relative_to(Path(project_dir)))
    except Exception:
        pass
    _persist(account, project_dir)
    _rv_close("SUCCESS" if account.verdict == "ACCEPT"
              else ("SEMANTIC_FAILURE" if account.verdict == "BLOCK"
                    else "PROVIDER_FAILURE"),
              mission_pass=account.verdict == "ACCEPT")

    if account.verdict == "ERROR":
        raise ReviewUnavailable("REVIEW_CALL_FAILED",
                                {"rc": rc, "engine": choice.engine,
                                 "reason": account.reason})
    return account
