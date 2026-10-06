"""Supervisor-side resource accounting. §D.

Every number here is observed by the SUPERVISOR at the launch boundary, never parsed
out of an agent's prose. A token count inside a worker-written receipt is
self-attestation; the same class of evidence as the three autonomy booleans that made
verify_long_run.py a shape check.

The rule that governs every field: an unknown is UNKNOWN. Never 0, never an estimate
dressed as a measurement. A cost model applied to a token guess is a number with the
shape of evidence and none of its content — and the factory runs on flat CLI
subscriptions, where dollars-per-task does not exist at all.

COST_NO_PROGRESS is the fail-closed counterpart: quota consumed without a validated
product delta, past a frozen threshold, suspends new model calls. It cannot be
satisfied by heartbeats or activity commits, because those are not deltas.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from factory_v2._io import _now

UNKNOWN = "UNKNOWN"

#: v2 adds measurement provenance (usage_source / usage_completeness), the
#: reasoning and provider-request counters, the API price ESTIMATE kept apart
#: from cost_usd, and the provider quota window. Purely additive: v1 rows still
#: fold and still aggregate, and every added field defaults to UNKNOWN, so an
#: old row reads as "not measured" rather than as zero.
ACCOUNT_SCHEMA = "attempt-account/v2"

PHASE_OPEN = "OPEN"
PHASE_TERMINAL = "TERMINAL"


class LivenessObservationUnknown(RuntimeError):
    """The worker's VIVANT/MORT state could not be observed."""


class AccountingLedgerUnknown(RuntimeError):
    """A durable attempt ledger exists but cannot be read completely."""


EXIT_CLASSES = frozenset({
    "SUCCESS", "SEMANTIC_FAILURE", "PROVIDER_FAILURE", "TIMEOUT", "QUOTA", "KILL",
    # LOT C25: the RUNTIME refused before ever calling the executor (e.g. an
    # autopilot mission's task_file is not a usable absolute path) — distinct
    # from SEMANTIC_FAILURE because the runtime itself is the witness that no
    # engine launched, not an inference from an ambiguous or missing receipt.
    "PREFLIGHT_REFUSED",
    # RESILIENCE (2026-09-13): the engine's own stderr showed a network cut that
    # could not be resumed in place within the budget. Not an engine failure:
    # routing evidence and failure fingerprints ignore it. (QUOTA above is the
    # matching class for an observed quota cut.)
    "NETWORK",
})


@dataclass
class AttemptAccount:
    """One launch, measured from outside the process that was launched."""
    run_id: str
    project: str
    milestone: str
    mission: str
    attempt: int
    engine: str
    model: str                      # the model the SUPERVISOR launched, from argv
    started_at: str
    launched_pid: int | None = None
    launched_argv: list = field(default_factory=list)
    ended_at: str = ""
    duration_s: float | None = None
    input_tokens: object = UNKNOWN
    output_tokens: object = UNKNOWN
    cache_read_tokens: object = UNKNOWN
    cache_write_tokens: object = UNKNOWN
    call_count: int = 1
    retry_count: int = 0
    exit_class: str = ""
    cost_usd: object = UNKNOWN      # flat subscriptions: usually genuinely unknown
    quota_remaining: object = UNKNOWN
    candidate_sha: str = ""
    receipt_sha: str = ""
    independent_verdict: str = ""
    #: Routing coordinates observed at launch.  Empty spec level is an explicit
    #: unknown; task_kind always names the launch class rather than being
    #: inferred later from an engine or a worker-written receipt.
    spec_level: str = ""
    task_kind: str = "build"

    #: Crash-unique identity for this dispatch (mission+contract+attempt+lease gen).
    #: Without it, an OPEN row written before execve cannot be matched to its
    #: terminal row after a restart.
    attempt_key: str = ""
    #: OPEN (durable before execve) or TERMINAL (durable after the process ends).
    phase: str = PHASE_OPEN
    #: The claimant's own result. NOT evidence of a product delta.
    mission_pass: bool = False
    #: Projected ONLY by the independent settlement path. This is the fact that
    #: cost_no_progress reads. `mission_pass` can be true while the product_e2e
    #: fails, and counting that as a delta would silently release the brake.
    independently_validated: bool = False
    #: Why a terminal row exists without its process having reported.
    reconciled_reason: str = ""
    #: FACTORY_LEDGER_ENGINE_KNOWN_V1 — why a launch refused BEFORE execve closed
    #: this account (preflight/quota/budget gate, dispatcher refusal). Set with
    #: engine == "none" (launch_gate.ENGINE_NONE_REFUSED); empty on every row
    #: where an engine actually ran.
    refusal_reason: str = ""
    #: Material code SHA this attempt ran at. Recorded for forensics ONLY. It used
    #: to scope cost_no_progress, which made any new commit — a doc edit, an empty
    #: one — reset the streak and buy a whole new budget. See
    #: recovery_allocation.py for the release path that replaced it.
    code_sha: str = ""
    #: Set when the brake was already tripped and this launch was paid for out of a
    #: finite recovery allocation. The allocation is a bounded chance to produce a
    #: delta; it is never itself a delta.
    recovery_allocation_id: str = ""
    #: Which call of that allocation this attempt was (1-based).
    recovery_spend_seq: int = 0

    # ── measurement provenance (factory_v2/telemetry) ────────────────────────
    #: WHERE the token numbers above came from — "claude.result_json",
    #: "opencode.step_finish", or a reason they are missing. A number without a
    #: source is indistinguishable from a number someone typed in.
    usage_source: str = UNKNOWN
    #: COMPLETE (provider printed end-of-session totals), PARTIAL_FLOOR
    #: (reconstructed from a log with no totals — a LOWER BOUND), or ABSENT.
    #: A PARTIAL_FLOOR row must never be summed as if it were exact.
    usage_completeness: str = "ABSENT"
    #: Join key into evidence/telemetry_observations.jsonl for the full record.
    telemetry_observation_key: str = ""
    #: Tokens the provider spent thinking. Counted, never quoted.
    reasoning_tokens: object = UNKNOWN
    #: FACTORY_TOKEN_TRUTH_V1 — launches this attempt made / launches whose usage
    #: was measured. When launches_measured > 1 the token fields are the SUM over
    #: the measured launches and usage_source lists every source ("a+b").
    launches_total: int = 0
    launches_measured: int = 0
    #: Provider requests INSIDE this one launch (messages/steps). Distinct from
    #: call_count, which counts launches. Merging them was how one dispatch of
    #: eighteen turns could be reported as one "call".
    provider_requests: object = UNKNOWN
    #: Assistant turns the provider reported. A LOWER BOUND on requests, kept
    #: under its own name so it can never be read as a request count.
    model_turns: object = UNKNOWN
    #: What the provider said these tokens would list-price at. NOT money spent:
    #: under a flat subscription nobody is charged it. `cost_usd` above stays
    #: UNKNOWN because money actually charged is not observable from any CLI we
    #: launch, and copying the estimate into it would invent a transaction.
    api_price_estimate_usd: object = UNKNOWN
    #: Provider-reported quota window, when the provider reports one at all.
    #: Never derived from tokens: a token count cannot be converted into a
    #: fraction of a subscription window, and an unknown window is not "plenty".
    quota_scope: str = UNKNOWN
    quota_status: str = UNKNOWN
    quota_utilisation: object = UNKNOWN
    #: Whether this launch could spend model quota at all (local/autopilot attempts
    #: that never invoke a model persist False). Absent on rows written before this
    #: field existed: readers treat 'absent' as 'unknown, count it' (never under-count).
    consumes_model_quota: object = UNKNOWN
    quota_observed_at: str = ""
    #: Logical worker partition for this launch.  Dispatch remains serial in this
    #: milestone, but the fact is durable now so a later parallel dispatcher does
    #: not have to reinterpret historical accounts.
    worker_slot: int = 0
    #: Supervisor worker capacity observed when this account was opened.
    max_workers: int = 1

    def close(self, *, exit_class: str) -> "AttemptAccount":
        if exit_class not in EXIT_CLASSES:
            raise ValueError(f"exit_class {exit_class!r} not in {sorted(EXIT_CLASSES)}")
        self.exit_class = exit_class
        self.ended_at = _now()
        return self

    def as_dict(self) -> dict:
        return {"schema": ACCOUNT_SCHEMA, **asdict(self)}


def configured_max_workers() -> int:
    """Return the configured worker capacity, with a safe serial default."""
    try:
        return max(1, int(os.environ.get("FACTORY_MAX_WORKERS", "1")))
    except (TypeError, ValueError):
        return 1


def configured_worker_slot(max_workers: int | None = None) -> int:
    """Return this process's logical slot without inventing parallel dispatch."""
    capacity = max_workers if max_workers is not None else configured_max_workers()
    try:
        requested = int(os.environ.get("FACTORY_WORKER_SLOT", "0"))
    except (TypeError, ValueError):
        requested = 0
    return requested % max(1, capacity)


def attempt_account_fixture_row() -> dict:
    """Serialized hermetic account used to verify the worker-slot ledger shape.

    FACTORY_LEDGER_ENGINE_KNOWN_V1: the fixture carries a KNOWN engine (glm /
    glm-5.3, the factory's canonical opencode launch), not a placeholder — a
    hermetic row must demonstrate the shape a real engine-known row has.
    """
    capacity = configured_max_workers()
    return AttemptAccount(
        run_id="fixture", project="fixture", milestone="fixture", mission="fixture",
        attempt=1, engine="glm", model="glm-5.3", started_at="1970-01-01T00:00:00Z",
        worker_slot=configured_worker_slot(capacity), max_workers=capacity,
    ).as_dict()


def _num(v):
    return v if isinstance(v, (int, float)) else None


def append(account: AttemptAccount, ledger: Path) -> None:
    ledger = Path(ledger)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(account.as_dict(), sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def _tokens_completeness(accounts: list, unknown_rows: int) -> str:
    """COMPLETE | PARTIAL_FLOOR | UNKNOWN for a token sum over `accounts`.

    Rows with numeric tokens contribute their `usage_completeness`; a row that
    predates the provenance fields (no field at all) is taken as the provider's
    end-of-session total it always was. Any PARTIAL_FLOOR row or any row whose
    tokens are UNKNOWN turns the whole aggregate into a lower bound."""
    counted = [a for a in accounts
               if _num(a.get("input_tokens")) is not None
               or _num(a.get("output_tokens")) is not None]
    if not counted:
        return UNKNOWN
    comps = {str(a.get("usage_completeness") or "COMPLETE") for a in counted}
    if unknown_rows or comps - {"COMPLETE", "ABSENT"}:
        return "PARTIAL_FLOOR"
    return "COMPLETE"


def metrics(accounts: list) -> dict:
    """The §D metrics. Any figure that cannot be measured is reported UNKNOWN.

    `unknown_*` counters are first-class output, not a footnote: an accounting system
    that hides how much it could not measure is not an accounting system.
    """
    accepted = [a for a in accounts if a.get("independently_validated")]
    failures = [a for a in accounts if a.get("exit_class") not in ("", "SUCCESS")]

    def _sum(field_, rows):
        vals = [_num(r.get(field_)) for r in rows]
        known = [v for v in vals if v is not None]
        return (sum(known) if known else UNKNOWN), (len(vals) - len(known))

    tok_in, unk_in = _sum("input_tokens", accounts)
    tok_out, unk_out = _sum("output_tokens", accounts)
    fail_in, _ = _sum("input_tokens", failures)

    n_acc = len(accepted)
    n_milestones = len({a.get("milestone") for a in accepted if a.get("milestone")})
    total_tok = (tok_in + tok_out) if isinstance(tok_in, (int, float)) \
        and isinstance(tok_out, (int, float)) else UNKNOWN
    # L2_runtime_core-011: a sum that includes a PARTIAL_FLOOR row, or that
    # skipped UNKNOWN rows, is a LOWER BOUND — it is published as one, not as
    # a measured total. COMPLETE only when every counted row says so.
    tok_completeness = _tokens_completeness(accounts, unk_in + unk_out)

    first_validated = ""
    for a in sorted(accounts, key=lambda r: r.get("started_at") or ""):
        if a.get("independently_validated"):
            first_validated = a.get("ended_at") or ""
            break

    return {
        "schema": "resource-metrics/v1",
        "attempts": len(accounts),
        # Sol: this counted validated ATTEMPTS. A milestone needing two missions
        # then reported as two accepted milestones, inflating every derived ratio.
        "independently_accepted_milestones": n_milestones,
        "independently_validated_attempts": n_acc,
        "tokens_total": total_tok,
        "tokens_total_completeness": tok_completeness,
        # Per-MILESTONE ratios must divide by milestones. Dividing by validated
        # attempts made every cost-per-result look smaller than it is.
        "tokens_per_accepted_milestone": (
            round(total_tok / n_milestones, 1)
            if isinstance(total_tok, (int, float)) and n_milestones else UNKNOWN),
        "tokens_per_accepted_milestone_completeness": tok_completeness,
        "calls_total": sum(int(a.get("call_count") or 0) for a in accounts),
        "calls_per_accepted_milestone": (
            round(sum(int(a.get("call_count") or 0) for a in accounts) / n_milestones, 2)
            if n_milestones else UNKNOWN),
        "tokens_spent_on_failures": fail_in,
        "time_to_first_validated_result": first_validated or UNKNOWN,
        "consumption_without_material_change_pct": (
            round(100.0 * (len(accounts) - n_acc) / len(accounts), 1)
            if accounts else UNKNOWN),
        "unknown_input_token_attempts": unk_in,
        "unknown_output_token_attempts": unk_out,
        "cost_usd": UNKNOWN,
        "cost_note": ("flat CLI subscriptions — dollars-per-task does not exist in this "
                      "model; quota window consumption is the real scarce resource"),
    }


def cost_no_progress(accounts: list, *, max_attempts_without_delta: int,
                     current_code_sha: str = "") -> dict:
    """Fail-closed gate: consumption without a validated delta suspends new calls.

    Counts CONSECUTIVE trailing attempts that produced no validated delta.
    Heartbeats and activity commits are not attempts and cannot reset it.

    NOTHING about the code SHA resets the streak. It used to: attempts made at an
    older material SHA stopped counting, so the brake was released by the ACT of
    committing rather than by the code being fixed. Reproduced: three failures at
    SHA `a…` trip it; hand the identical rows an unrelated SHA `b…` and the streak
    is 0. A comment, a doc edit or `--allow-empty` bought a full fresh budget, and
    a loop that commits between retries had an infinite one.

    `current_code_sha` is now recorded in the verdict for forensics only — which
    system the streak was observed on — and cannot shorten it.

    The release path is elsewhere and is proof-bound, finite and durable: see
    recovery_allocation.py. A tripped brake can be paid past only by spending a
    supervisor-authorized allocation, one call at a time, and only until that
    allocation is exhausted, expires, or its control pin goes stale. The permanent
    release is unchanged and unbuyable: an independent settlement projecting a real
    validated delta.
    """
    streak = 0
    for a in reversed(accounts):
        if a.get("independently_validated"):
            break
        # COST_NO_PROGRESS is about CONSUMPTION without a validated delta. An
        # attempt that never launched a model — preflight refusal, local
        # command, `consumes_model_quota=False` persisted by open_account —
        # spent nothing scarce and must not trip the brake (2 preflight
        # refusals of 0.19 s each did, 2026-09-12, and bought a Claude DIAGNOSE).
        # Legacy rows without an `engine` key are still counted (never
        # under-count history); only an explicit non-model marker is skipped.
        # Narrow by design (Sol/Codex contract: a local attempt that fails IS an
        # unvalidated attempt): skip only a row whose engine stayed UNKNOWN —
        # the launch never happened (preflight refusal) — or one explicitly
        # marked consumes_model_quota=False by open_account.
        # Only an EXPLICIT marker excludes a row: `consumes_model_quota=False`
        # persisted by open_account (local proof, supervisor-proved no model).
        # engine==UNKNOWN is NOT enough: stubbed executors in the established
        # CNP test contract report UNKNOWN too. Follow-up: the dispatcher's own
        # preflight refusal (PREFLIGHT_FAIL before any engine) should close its
        # account with consumes_model_quota=False so it stops counting here.
        if a.get("consumes_model_quota") is False:
            continue
        streak += 1
    tripped = streak >= max_attempts_without_delta
    return {
        "schema": "cost-no-progress/v2",
        "consecutive_attempts_without_validated_delta": streak,
        "threshold": max_attempts_without_delta,
        "tripped": tripped,
        "new_model_calls_allowed": not tripped,
        "observed_at_code_sha": current_code_sha or UNKNOWN,
        "detail": (f"{streak} consecutive attempt(s) produced no independently "
                   f"validated product delta"
                   + (" — new model calls SUSPENDED until an independent "
                      "settlement validates a delta, or a finite recovery "
                      "allocation pays for the next launch" if tripped else "")),
    }


def preflight(*, contract_frozen: bool, next_action_compilable: bool,
              budget_remaining: bool, identical_attempt_active: bool,
              expected_result_defined: bool,
              independent_proof_defined: bool) -> dict:
    """Five conditions that must all hold before ANY new model call. Fail-closed."""
    checks = {
        "contract_frozen": contract_frozen,
        "next_action_compilable": next_action_compilable,
        "budget_remaining": budget_remaining,
        "no_identical_attempt_active": not identical_attempt_active,
        "expected_result_and_independent_proof_defined": (
            expected_result_defined and independent_proof_defined),
    }
    failed = sorted(k for k, v in checks.items() if not v)
    return {"schema": "model-call-preflight/v1", "checks": checks,
            "allowed": not failed, "blocking": failed}

# ── F2/F3: two-phase, append-only, exactly-once ──────────────────────────────
def read_rows(ledger) -> list:
    """Read the durable ledger, refusing every unreadable or corrupt row."""
    ledger = Path(ledger)
    if not ledger.is_file():
        return []
    try:
        raw = ledger.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise AccountingLedgerUnknown(f"accounting ledger unreadable: {exc}") from exc
    out = []
    for index, line in enumerate(raw.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as exc:
            # Even a torn tail proves that the durable observation is incomplete.
            # Treating it as absent could hide the only terminal state for an OPEN row.
            raise AccountingLedgerUnknown(
                f"accounting ledger corrupt at line {index + 1}: {exc}") from exc
    return out


def fold(rows: list) -> list:
    """Collapse append-only rows into one record per attempt, in first-seen order.

    Later rows refine earlier ones; `independently_validated` is monotonic (once
    an independent settlement projects it, nothing walks it back). Rows without an
    attempt_key are legacy singletons and stand alone.
    """
    order: list = []
    bykey: dict = {}
    for r in rows:
        key = r.get("attempt_key") or f"__legacy__{len(order)}"
        if key not in bykey:
            bykey[key] = dict(r)
            order.append(key)
            continue
        cur = bykey[key]
        for k, v in r.items():
            if k == "independently_validated":
                cur[k] = bool(cur.get(k)) or bool(v)
            elif v not in ("", None, UNKNOWN) or k not in cur:
                cur[k] = v
    return [bykey[k] for k in order]


def append_open(account: "AttemptAccount", ledger) -> None:
    """Durable OPEN row BEFORE execve, so a SIGKILL cannot erase the attempt.

    Previously the first write happened in close_account(), after the engine
    returned. A kill during the model call therefore deleted the attempt from
    history along with the quota it had already burned — the ledger's blind spot
    was exactly the window that costs the most.
    """
    account.phase = PHASE_OPEN
    append(account, ledger)


def reconcile_open_attempts(ledger, *, reason: str = "PROCESS_LOST",
                            observed_by_attempt: dict | None = None,
                            is_live=None) -> list:
    """Close every OPEN attempt that never reached a terminal row. Exactly once.

    Idempotent by construction: an attempt whose fold already shows TERMINAL is
    skipped, so a second restart adds nothing. Never claims the work succeeded —
    a lost process produced no validated delta.

    `is_live(rec) -> bool`: 2026-09-17 — this runs on EVERY advance, and with
    intra-project parallel advances a sibling slot's launch is legitimately OPEN
    while it runs. An OPEN row whose mission worker is alive is not lost.
    """
    ledger = Path(ledger)
    rows = read_rows(ledger)
    closed = []
    for rec in fold(rows):
        if rec.get("phase") != PHASE_OPEN:
            continue
        if not rec.get("attempt_key"):
            continue                      # legacy row: no identity to reconcile against
        if is_live is not None:
            try:
                if is_live(rec):
                    continue
            except Exception as exc:
                # UNKNOWN must remain OPEN; it is not evidence that the worker is dead.
                raise LivenessObservationUnknown(
                    f"liveness unavailable for {rec.get('attempt_key')}: {exc}") from exc
        term = dict(rec)
        term.update({
            "phase": PHASE_TERMINAL,
            "exit_class": "KILL",
            "ended_at": _now(),
            "mission_pass": False,
            "independently_validated": False,
            "reconciled_reason": reason,
        })
        observed = (observed_by_attempt or {}).get(term["attempt_key"]) or {}
        for key, value in observed.items():
            if value not in (None, "", UNKNOWN):
                term[key] = value
        _append_row(term, ledger)
        closed.append(term["attempt_key"])
    return closed


def project_independent_validation(ledger, attempt_keys: list, *,
                                   receipt_sha: str = "",
                                   verdict: str = "SETTLED") -> list:
    """The ONLY way independently_validated becomes true.

    Called from the independent settlement path with an EXPLICIT list of attempt
    keys the settlement actually verified — never a rescan.

    Idempotent: a key already carrying independently_validated is skipped, so a
    crash between the ledger append and the caller's completion replays without
    double-counting. Returns the keys newly projected on THIS call.
    """
    ledger = Path(ledger)
    folded = {r.get("attempt_key"): r for r in fold(read_rows(ledger))
              if r.get("attempt_key")}
    done = []
    for key in attempt_keys:
        rec = folded.get(key)
        if rec is None:
            continue                      # never invent an attempt that did not run
        if rec.get("independently_validated"):
            continue                      # already projected — exactly once
        _append_row({"schema": ACCOUNT_SCHEMA, "attempt_key": key,
                     "phase": PHASE_TERMINAL,
                     "spec_level": rec.get("spec_level", ""),
                     "task_kind": rec.get("task_kind", ""),
                     "independently_validated": True,
                     "independent_verdict": verdict, "receipt_sha": receipt_sha,
                     "validated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                   time.gmtime())}, ledger)
        done.append(key)
    return done


def _append_row(row: dict, ledger) -> None:
    ledger = Path(ledger)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
