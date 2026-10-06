"""The production launch gate. §F3 (r1), §F2/F4/F5 (r2).

r1 put the accounting modules on the one path that spends money. Codex's r2 audit
showed the seam was in the right place but told the truth about too little:

  * the attempt was written only AFTER the engine returned, so a SIGKILL during a
    model call erased the attempt and the quota it had already burned (F2);
  * `settled` was set from `result.ok` — the claimant reading its own artifact —
    so a mission that passed and then failed its product_e2e still released the
    COST_NO_PROGRESS brake (F3);
  * three of preflight's five checks were hardcoded True/False, so the gate
    displayed five controls and observed two (F4);
  * the observed engine identity sat in `result.engine_execution` and was never
    projected into the account, leaving model/engine UNKNOWN even when the
    supervisor had watched the process (F5).

Everything here is fail-closed: what cannot be observed blocks, with a typed
reason, and an unmeasured quantity stays UNKNOWN rather than becoming 0.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from . import recovery_allocation
from .resource_accounting import (UNKNOWN, AttemptAccount, append, append_open,
                                  configured_max_workers, configured_worker_slot,
                                  cost_no_progress, fold, preflight,
                                  project_independent_validation, read_rows,
                                  reconcile_open_attempts)

#: FACTORY_LEDGER_ENGINE_KNOWN_V1 — engine value written on a TERMINAL row whose
#: launch was refused BEFORE execve (runtime preflight, dispatcher refusal,
#: quota, budget). 'none' is a TRUE engine statement — "no engine ran" — not an
#: unknown: UNKNOWN stays reserved for "a launch happened but its identity
#: could not be observed". Refusal rows also carry `refusal_reason`.
ENGINE_NONE_REFUSED = "none"

#: argv0 basenames whose CLI binary IS the engine. `opencode` is deliberately
#: absent: it fronts several providers, so its engine is whichever model the
#: argv names (see _engine_from_argv) — same projection as
#: factory/bin/model_router.engine_from_argv, kept local because launch_gate
#: must stay importable without factory/bin on the path.
_ARGV0_ENGINE = {"claude": "claude", "codex": "codex"}

#: opencode's default engine when the argv names no model (Delta CTO 002:
#: opencode == GLM in this factory).
_OPENCODE_DEFAULT_ENGINE = "glm"

#: PROVISIONAL_SAFETY_CAP — chosen to stop runaway consumption, NOT optimised.
#: Recalibrate only from real validated RunRecords; never present as tuned.
PROVISIONAL_SAFETY_CAP = True

#: Consecutive attempts with no INDEPENDENTLY VALIDATED delta before calls suspend.
MAX_ATTEMPTS_WITHOUT_DELTA = 3

#: Real CLI model launches allowed per milestone before the budget is spent.
#: Claude and GLM run under CLI subscriptions, so there is no per-call billing:
#: the cost is quota, and the observable budget is a launch count. A contract may
#: override it via body["max_model_calls"].
#: 2026-09-17 (owner, quotas at full): 12 was hit by a single afternoon of
#: daemon-side kills (sweeper bug) and locked two milestones with the quota
#: untouched. The equivalent-retry ladder (3 same-fingerprint → diagnose →
#: replan) is what bounds waste; the launch cap is only a backstop.
#: Overridable with FACTORY_MAX_MODEL_CALLS_PER_MILESTONE.
DEFAULT_MAX_MODEL_CALLS_PER_MILESTONE = int(
    os.environ.get("FACTORY_MAX_MODEL_CALLS_PER_MILESTONE") or 80)

LEDGER_NAME = "attempt_accounts.jsonl"


class LaunchBlocked(Exception):
    """A launch was refused. Carries the machine-readable reason."""

    def __init__(self, reason: str, detail: dict):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


_CODE_SHA_CACHE: dict = {}


def current_code_sha() -> str:
    """Material SHA of the control repo, or "" when it cannot be observed.

    Empty on failure, never a guess: an attempt with an unknown SHA counts toward
    the brake, so an unobservable repo makes the gate stricter, not looser.
    """
    if "sha" in _CODE_SHA_CACHE:
        return _CODE_SHA_CACHE["sha"]
    import subprocess
    try:
        root = Path(__file__).resolve().parent.parent
        r = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=10)
        sha = r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        sha = ""
    _CODE_SHA_CACHE["sha"] = sha
    return sha


def ledger_path(project_dir: Path) -> Path:
    return Path(project_dir) / "evidence" / LEDGER_NAME


def _accounts(project_dir: Path) -> list:
    return fold(read_rows(ledger_path(project_dir)))


def cost_no_progress_now(project_dir: Path) -> dict:
    """R2C: cheap COST_NO_PROGRESS peek — read-only, no allocation, no mutation.

    Used BEFORE any dispatch-side durable write (worker-state clear, lease grant,
    context manifest, state save) so an already-braked poll learns from the
    ledger alone what it used to learn only after mutating half the project.
    """
    return cost_no_progress(_accounts(project_dir),
                            max_attempts_without_delta=MAX_ATTEMPTS_WITHOUT_DELTA,
                            current_code_sha=current_code_sha())


# ── F4: the three checks that used to be constants ───────────────────────────
def observe_next_action_compilable(mission) -> bool:
    """Structural check on the COMPILED action, not a constant.

    An executor with no type, or an autopilot step with no task file, is not a
    runnable next action; launching an engine to find that out costs money.
    """
    exc = getattr(mission, "executor", None) or {}
    kind = exc.get("type", "")
    if not kind:
        return False
    if kind == "local_command":
        return bool(exc.get("command"))
    if kind == "autopilot":
        return bool(exc.get("task_file"))
    return True


def _started_counts_since(started_at, since_ts: float) -> bool:
    """L2_runtime_core-004: a launch whose `started_at` is PRESENT but cannot
    be parsed is indeterminable, not "before the freeze" — it counts (the
    ledger fails toward over-counting). Only a parseable instant strictly
    before the freeze is excluded."""
    epoch = _iso_epoch(started_at)
    return epoch is None or epoch >= since_ts


def _iso_epoch(value) -> "float | None":
    """Epoch seconds of an ISO-8601 instant written "…Z" or "…+00:00", or None.

    2026-09-19: the two writers of this repo use both suffixes for the same UTC
    instant; comparing them as strings orders identical seconds differently
    depending on the suffix. Never raises — unparseable input is the caller's
    "unknown", never a guess.
    """
    from datetime import datetime, timezone
    try:
        s = str(value)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def observe_budget(project_dir: Path, milestone: str, contract_body: dict) -> dict:
    """Model launches already spent on this milestone, against its declared cap.

    Counted from OPEN rows, so a killed attempt still consumes budget — that is
    the whole point of persisting OPEN before execve.

    Counts LAUNCHES, not attempt rows: one attempt (after fold) is one row here,
    but a 'local'/'autopilot' row that never invoked a model — `engine` unset or
    `"local"`, or `consumes_model_quota=False` persisted by open_account — never
    spent the scarce resource this cap protects, and must not be charged as if
    it had. A row that predates `consumes_model_quota` (the field is simply
    absent) is still counted: staying silent about an old fact must never
    UNDER-count history, only an explicit False may exclude a row.

    C19: the contract's `max_model_calls` is a fixed cap, but a supervisor's
    OWNER_AUTHORIZATION grant on THIS milestone is also, by construction, an
    instruction to allow more launches than the contract alone declared — the
    grant exists precisely because the milestone needs to keep going. Once the
    contract cap alone is spent (`contract_exhausted`), the ledger is topped up
    by whatever `recovery_allocation.owner_budget_extension()` re-derives as
    still-remaining on a valid OWNER grant for this exact project, milestone,
    frozen contract, incident and control commit — never by a VERIFIED_FIX /
    SUPERVISOR_RECOVERY / MULTI_AI_REVIEW grant, which release COST_NO_PROGRESS
    on their own terms but do not raise a milestone's declared budget unless
    their own contract already says so (it does not, today). The owner
    extension is consulted ONLY once the contract cap is actually spent — an
    unused grant sitting on a milestone that has budget left is not "extending"
    anything yet, and is not reported as if it were.
    """
    cap = contract_body.get("max_model_calls")
    if not isinstance(cap, int) or cap <= 0:
        cap = DEFAULT_MAX_MODEL_CALLS_PER_MILESTONE
    # 2026-09-17: the cap is per CONTRACT, not per milestone-for-all-time — a
    # milestone whose authority was re-frozen (replan, new task file) starts a
    # new budget. Callers pass the freeze instant as body["_frozen_at"]; without
    # it (recovery path, legacy) the whole history counts as before.
    since = str(contract_body.get("_frozen_at") or "")
    # 2026-09-19 (FACTORY_CHAOS_DRILL_V1#M1): the contract freezes `frozen_at` as
    # "…Z" (time.strftime) while ledger rows write `started_at` as "…+00:00"
    # (datetime.isoformat). The comparison used to be LEXICOGRAPHIC: within the
    # same second '+' sorts before 'Z', so a launch that happened in the very
    # second of the freeze was silently NOT counted and the cap fired one launch
    # too late, non-deterministically. Instants are compared as instants; a row
    # whose timestamp cannot be parsed still counts (fail toward over-counting,
    # the direction this ledger has always chosen for unreadable facts).
    since_ts = _iso_epoch(since)
    used = sum(1 for a in _accounts(project_dir)
               if a.get("milestone") == milestone and a.get("attempt_key")
               and a.get("engine") not in (None, "", "local")
               and a.get("consumes_model_quota") is not False
               and (not since_ts or not a.get("started_at")
                    or _started_counts_since(a.get("started_at"), since_ts)))
    contract_exhausted = used >= cap
    result = {"max_model_calls": cap, "used": used,
              "contract_exhausted": contract_exhausted}
    owner_remaining = 0
    if contract_exhausted:
        ext = recovery_allocation.owner_budget_extension(
            project_dir, milestone=milestone, control_code_sha=current_code_sha())
        if ext.get("grants"):
            owner_remaining = int(ext.get("remaining") or 0)
            result["owner_extension"] = {
                "event": "BUDGET_EXTENDED_BY_OWNER",
                "grants": [g["allocation_id"] for g in ext["grants"]],
                "remaining": owner_remaining,
                "detail": (f"{owner_remaining} owner-authorized launch(es) still "
                          f"available for {milestone} beyond its {cap}-launch "
                          f"contract cap"),
            }
    result["remaining"] = owner_remaining if contract_exhausted else max(0, cap - used)
    result["exhausted"] = contract_exhausted and owner_remaining <= 0
    return result


def observe_identical_attempt_active(project_dir: Path, mission_id: str,
                                     own_generation: int | None) -> dict:
    """Is another live process already running THIS mission?

    Observed from the lease registry plus process liveness — not assumed False.
    A lease held by a dead pid is not an active attempt, and our own lease for
    this dispatch is not a competitor.
    """
    try:
        from .worker_lease import WorkerLease
        lease = WorkerLease._read_nolock(mission_id, Path(project_dir))
    except Exception as exc:                      # registry unreadable ⇒ UNKNOWN
        return {"active": None, "detail": f"lease registry unreadable: {exc}"}
    if lease is None:
        return {"active": False, "detail": "no lease on record"}
    if own_generation is not None and lease.generation == own_generation:
        return {"active": False, "detail": "lease belongs to this dispatch"}
    try:
        if lease.is_expired:
            return {"active": False, "detail": "lease expired"}
        alive = lease.is_process_alive
    except Exception as exc:
        return {"active": None, "detail": f"liveness unobservable: {exc}"}
    return {"active": bool(alive),
            "detail": f"lease gen={lease.generation} alive={alive}"}


def peek_launch_blockers(project_dir: Path, *, mission_id: str, milestone: str,
                         contract_body: dict | None = None, mission=None,
                         own_lease_generation: int | None = None,
                         supervisor_proved_no_model: bool = False) -> dict | None:
    """ECO-01 residual: cheap, read-only peek for the two reasons that recur
    most under a capped milestone — BUDGET_EXHAUSTED and
    IDENTICAL_ATTEMPT_ACTIVE/UNOBSERVABLE.

    Same posture as `cost_no_progress_now` (read the ledger, decide, never
    write) applied to the two checks `check_before_launch` raises via
    `observe_budget()`/`observe_identical_attempt_active()` — REUSED here
    verbatim, not reimplemented, so the two call sites can never drift. A
    milestone that already spent its launch cap, or whose mission is already
    running under a live lease held by another process, is refused here
    BEFORE a lease is granted, a context manifest is built, or a RunRecords
    PREDICTION is opened — none of which `reconcile_orphan_predictions` would
    then need to later close as a false WORKER_DEATH.

    Returns None when neither check blocks — `check_before_launch` still runs
    unchanged afterward as defense in depth, re-deriving both facts itself.
    Otherwise returns {"reason": ..., "detail": ...}, shaped like
    `LaunchBlocked`'s (reason, detail) pair, ready to raise or report as-is.

    `own_lease_generation` is normally None here: this peek runs before this
    dispatch's own lease exists, so any lease found on disk is necessarily
    someone else's — there is nothing of "ours" to exclude yet.
    `supervisor_proved_no_model` defaults to False (fail-closed): the facts
    that could prove this specific dispatch is model-free (prevalidated head,
    checkpoint resume) are only known later in the dispatch path, after the
    lease this peek exists to avoid granting — so, absent that knowledge, a
    launch is conservatively assumed to spend model quota.
    """
    executor_type = ((getattr(mission, "executor", {}) or {}).get("type")
                     if mission is not None else "")
    consumes_model_quota = not (
        executor_type == "weekly_refresh_observed" or supervisor_proved_no_model)

    budget = observe_budget(project_dir, milestone, contract_body or {})
    if budget["exhausted"] and consumes_model_quota:
        return {"reason": "BUDGET_EXHAUSTED", "detail": {
            **budget, "detail": (f"{budget['used']} model launches already spent on "
                                 f"{milestone}, cap {budget['max_model_calls']}")}}

    if own_lease_generation is None:
        # Pre-lease peek: a lease from a PREVIOUS generation may still look
        # "active" (owner pid alive — e.g. a cold restart right after an
        # in-process run) although granting our own lease will fence it.
        # Only the post-lease check_before_launch, which knows its own
        # generation, can judge IDENTICAL_ATTEMPT honestly. Budget peeked above.
        return None

    identical = observe_identical_attempt_active(project_dir, mission_id,
                                                 own_lease_generation)
    if identical["active"] is None:
        return {"reason": "IDENTICAL_ATTEMPT_UNOBSERVABLE", "detail": {
            **identical, "detail": "cannot prove no duplicate attempt is running — "
                                   "refusing rather than assuming"}}
    if identical["active"]:
        return {"reason": "IDENTICAL_ATTEMPT_ACTIVE", "detail": identical}

    return None


def check_before_launch(project_dir: Path, *, mission_id: str, milestone: str,
                        contract_frozen: bool, expected_result_defined: bool,
                        independent_proof_defined: bool,
                        mission=None, contract_body: dict | None = None,
                        own_lease_generation: int | None = None,
                        supervisor_proved_no_model: bool = False,
                        verifier_preflight_result: dict | None = None) -> dict:
    """Fail-closed gate evaluated before every real engine launch.

    `verifier_preflight_result` is the (optional) output of
    `verifier_preflight.check_verifier_not_implemented()` run by the caller
    BEFORE this gate — a milestone whose verifier structurally cannot pass
    (VERIFIER_NOT_IMPLEMENTED) is refused here, before any budget is
    inspected or any account opened, so no model is ever dispatched to learn
    a fact a cheap subprocess call already knows.
    """
    if verifier_preflight_result and verifier_preflight_result.get("not_implemented"):
        raise LaunchBlocked("VERIFIER_NOT_IMPLEMENTED", {
            **verifier_preflight_result,
            "detail": ("the milestone verifier itself reports "
                      "VERIFIER_NOT_IMPLEMENTED — no independent observation "
                      "producer can pass this milestone yet; refusing to "
                      "spend model quota to learn that the expensive way")})
    accounts = _accounts(project_dir)
    cnp = cost_no_progress(accounts,
                           max_attempts_without_delta=MAX_ATTEMPTS_WITHOUT_DELTA,
                           current_code_sha=current_code_sha())
    executor_type = ((getattr(mission, "executor", {}) or {}).get("type")
                     if mission is not None else "")
    consumes_model_quota = not (
        executor_type == "weekly_refresh_observed" or supervisor_proved_no_model)
    allocation = {}
    standing = {}
    if cnp["tripped"] and consumes_model_quota:
        # The brake is on. The ONLY thing that may pay past it is a finite
        # supervisor-authorized recovery allocation whose basis re-validates now
        # and whose project, frozen contract, incident and control commit match
        # what the supervisor state says at this instant. This is a PEEK: the
        # dispatch attempt is charged in open_account, before execve, so a gate
        # that passes and never launches has spent nothing, and a launch can never
        # skip the charge.
        allocation = recovery_allocation.available(
            project_dir, milestone=milestone, control_code_sha=current_code_sha())
        if not allocation.get("allocation_id"):
            # A normal per-milestone owner grant always wins.  Standing is only
            # consulted after every ordinary allocation is unusable.
            standing = recovery_allocation.available_standing(project_dir)
            if not standing.get("allocation_id"):
                if standing.get("standing_cap_reached"):
                    recovery_allocation.record_standing_event(
                        project_dir, "STANDING_CAP_REACHED",
                        used_today=standing["used_today"], cap=standing["cap"],
                        resets_at=standing["resets_at"])
                    raise LaunchBlocked("STANDING_CAP_REACHED", {**cnp, "standing": standing,
                                         "waiting": "calendar"})
                raise LaunchBlocked("COST_NO_PROGRESS", {**cnp, "recovery": allocation,
                                                           "standing": standing})

    body = contract_body or {}
    budget = observe_budget(project_dir, milestone, body)
    if budget["exhausted"] and consumes_model_quota:
        raise LaunchBlocked("BUDGET_EXHAUSTED", {
            **budget, "detail": (f"{budget['used']} model launches already spent on "
                                 f"{milestone}, cap {budget['max_model_calls']}")})

    identical = observe_identical_attempt_active(project_dir, mission_id,
                                                 own_lease_generation)
    if identical["active"] is None:
        raise LaunchBlocked("IDENTICAL_ATTEMPT_UNOBSERVABLE", {
            **identical, "detail": "cannot prove no duplicate attempt is running — "
                                   "refusing rather than assuming"})
    if identical["active"]:
        raise LaunchBlocked("IDENTICAL_ATTEMPT_ACTIVE", identical)

    if mission is None:
        raise LaunchBlocked("NEXT_ACTION_UNOBSERVABLE", {
            "detail": "no compiled mission supplied — cannot observe whether the "
                      "next action is runnable, so refusing rather than assuming"})
    compilable = observe_next_action_compilable(mission)
    pf = preflight(
        contract_frozen=contract_frozen,
        next_action_compilable=compilable,
        budget_remaining=(not budget["exhausted"] or not consumes_model_quota),
        identical_attempt_active=bool(identical["active"]),
        expected_result_defined=expected_result_defined,
        independent_proof_defined=independent_proof_defined,
    )
    if not pf["allowed"]:
        raise LaunchBlocked("PREFLIGHT_BLOCKED", pf)
    return {"cost_no_progress": cnp, "preflight": pf, "budget": budget,
            "identical_attempt": identical, "recovery_allocation": allocation,
            "standing_authorization": standing,
            "consumes_model_quota": consumes_model_quota}


# ── F2: the attempt exists on disk before the process does ───────────────────
def open_account(project_dir: Path, *, attempt_key: str, project: str,
                 milestone: str, mission: str = "", mission_id: str = "",
                 attempt: int, engine: str = "", model: str = "", run_id: str = "",
                 consumes_model_quota: bool = True,
                 spec_level: str = "", task_kind: str = "build",
                 contract_body: dict | None = None, cnp: dict | None = None) -> AttemptAccount:
    """Build AND durably persist the OPEN row, before execve.

    This is also where a tripped brake is PAID, not merely inspected. If
    COST_NO_PROGRESS is tripped at this instant, one call is charged to a finite
    recovery allocation and the OPEN row records which allocation and which call —
    before the row is written and before the process exists. Three consequences,
    all deliberate:

      * a launch cannot skip the charge by calling open_account directly; the
        check in check_before_launch is a peek, this is the ledger entry;
      * a SIGKILL during the model call has still spent the call, because the
        spend is fsynced first, so a restart cannot replenish;
      * when nothing is left to charge, this raises LaunchBlocked rather than
        opening an attempt. In the normal flow check_before_launch has already
        refused; reaching it here means the allocation ran out, expired or went
        stale in the window between the two, and refusing is the only honest
        answer to that race.

    C19: a launch that is NOT paying for COST_NO_PROGRESS may still be spending
    an OWNER_AUTHORIZATION grant's extension of the milestone's own
    `max_model_calls` cap (see `observe_budget`). That is a SEPARATE charge from
    the one above — it is only taken when the CNP branch did not already spend
    a call for this exact attempt, so the same dispatch is never billed twice —
    and it is restricted to `BASIS_OWNER` so a grant issued for something else
    (a verified fix, a supervisor recovery, a review) can never be drained by an
    ordinary product launch that merely happens to also be valid against it.
    `contract_body` is the same dict `check_before_launch` was given; without it
    the cap this recognises is the milestone default, which can only make the
    recognition too conservative against a contract that actually overrides
    `max_model_calls`, never spend an extension that was not really needed.
    """
    # `mission_id`/`cnp` are accepted for the stable supervisor probe contract.
    # The gate still derives CNP from durable rows rather than trusting a caller.
    mission = mission or mission_id
    max_workers = configured_max_workers()
    acct = AttemptAccount(
        run_id=run_id or os.environ.get("FACTORY_RUN_ID", ""),
        project=project, milestone=milestone, mission=mission, attempt=attempt,
        engine=engine or UNKNOWN, model=model or UNKNOWN,
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        spec_level=spec_level if spec_level in ("A", "B", "C", "") else "",
        task_kind=(task_kind if task_kind in ("build", "diagnose", "replan", "local", "")
                   else "build"),
        attempt_key=attempt_key, code_sha=current_code_sha(),
        worker_slot=configured_worker_slot(max_workers), max_workers=max_workers,
    )
    cnp = cost_no_progress(_accounts(project_dir),
                           max_attempts_without_delta=MAX_ATTEMPTS_WITHOUT_DELTA,
                           current_code_sha=current_code_sha())
    charged = False
    if cnp["tripped"] and consumes_model_quota:
        try:
            spend = recovery_allocation.consume(
                project_dir, attempt_key=attempt_key, milestone=milestone,
                control_code_sha=current_code_sha(), expect_project=project)
        except recovery_allocation.AllocationUnavailable:
            # Standing has no finite allocation to consume: the durable OPEN
            # ledger row itself is its charge and is re-counted on every gate.
            standing = recovery_allocation.available_standing(project_dir,
                                                                expect_project=project)
            if not standing.get("allocation_id"):
                raise LaunchBlocked("COST_NO_PROGRESS", {**cnp, "standing": standing})
            recovery_allocation.record_standing_event(
                project_dir, "STANDING_AUTHORIZATION_USED",
                allocation_id=standing["allocation_id"], used_today=standing["used_today"],
                cap=standing["cap"], attempt_key=attempt_key)
            # Attribute the row to the standing authorization so the daily cap
            # counts exactly the launches it paid for (see _standing_used_today).
            acct.recovery_allocation_id = standing["allocation_id"]
            charged = True
        else:
            acct.recovery_allocation_id = spend["allocation_id"]
            acct.recovery_spend_seq = int(spend.get("seq") or 0)
            charged = True

    if consumes_model_quota and not charged:
        pre = observe_budget(project_dir, milestone, contract_body or {})
        if pre.get("contract_exhausted") and pre.get("owner_extension"):
            try:
                spend = recovery_allocation.consume(
                    project_dir, attempt_key=attempt_key, milestone=milestone,
                    control_code_sha=current_code_sha(), expect_project=project,
                    basis=recovery_allocation.BASIS_OWNER)
            except recovery_allocation.AllocationUnavailable as exc:
                raise LaunchBlocked("BUDGET_EXHAUSTED",
                                    {**pre, "recovery": exc.detail}) from exc
            acct.recovery_allocation_id = spend["allocation_id"]
            acct.recovery_spend_seq = int(spend.get("seq") or 0)

    acct.consumes_model_quota = bool(consumes_model_quota)
    append_open(acct, ledger_path(project_dir))
    return acct



# ── F5: project what the supervisor OBSERVED, never what was requested ───────
def _engine_from_argv(argv) -> str:
    """Canonical engine id projected from the argv handed to the OS.

    argv0 names the CLI: `claude -p` → claude, `codex exec` → codex. `opencode`
    fronts several providers, so its engine is whichever canonical engine the
    `--model` token names (`zai-coding-plan/glm-5.3` → glm), defaulting to GLM
    (Delta CTO 002). "" when argv0 is not a known engine binary — callers must
    treat that as "identity unobserved" and fail closed, never fall back to a
    guess.
    """
    toks = [str(t) for t in (argv or [])]
    if not toks:
        return ""
    argv0 = toks[0].rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    model = ""
    if "--model" in toks:
        i = toks.index("--model")
        if i + 1 < len(toks):
            model = toks[i + 1]
    if argv0 == "opencode":
        for engine in ("glm", "claude", "codex"):
            if engine in model.lower():
                return engine
        return _OPENCODE_DEFAULT_ENGINE
    return _ARGV0_ENGINE.get(argv0, "")


def engine_for_close(receipt_or_refusal: dict) -> str:
    """The engine a TERMINAL row must carry, derived ONLY from what the close
    knows (FACTORY_LEDGER_ENGINE_KNOWN_V1).

    `receipt_or_refusal` is the engine-execution receipt of the launch, or the
    refusal record of a launch that never happened:

      * a non-empty `refusal` (or `refusal_reason`) → ENGINE_NONE_REFUSED — the
        process was refused before execve, so 'none' is the true engine, never
        UNKNOWN;
      * else an observed `engine_id` (≠ UNKNOWN) wins — the supervisor watched
        the process (F5);
      * else the engine is projected from the argv (`argv_head`, `argv` or
        `launched_argv`): `opencode run … glm-5.3` → glm, `codex exec` → codex,
        `claude -p` → claude;
      * else UNKNOWN — a launch whose identity is genuinely unobservable keeps
        the honest UNKNOWN rather than an invented name.
    """
    d = receipt_or_refusal if isinstance(receipt_or_refusal, dict) else {}
    if str(d.get("refusal") or d.get("refusal_reason") or ""):
        return ENGINE_NONE_REFUSED
    observed = str(d.get("engine_id") or "")
    if observed and observed != UNKNOWN:
        return observed
    argv = d.get("argv_head") or d.get("argv") or d.get("launched_argv") or []
    engine = _engine_from_argv(argv)
    return engine or UNKNOWN


def _project_observed(account: AttemptAccount, engine_execution: dict) -> None:
    ee = engine_execution or {}
    observed_engine = ee.get("engine_id") or ""
    observed_model = ee.get("model") or ""
    if observed_engine:
        account.engine = observed_engine
    if observed_model:
        account.model = observed_model
    if ee.get("pid"):
        account.launched_pid = ee["pid"]
    if ee.get("argv"):
        account.launched_argv = list(ee["argv"])
    elif ee.get("argv0"):
        account.launched_argv = [ee["argv0"]]
    # Usage only if the provider actually exposed it. Absent stays UNKNOWN.
    for src, dst in (("input_tokens", "input_tokens"),
                     ("output_tokens", "output_tokens"),
                     ("cache_read_tokens", "cache_read_tokens"),
                     ("cache_write_tokens", "cache_write_tokens"),
                     ("reasoning_tokens", "reasoning_tokens"),
                     ("provider_requests", "provider_requests"),
                     ("model_turns", "model_turns"),
                     ("api_price_estimate_usd", "api_price_estimate_usd"),
                     ("quota_utilisation", "quota_utilisation")):
        v = ee.get(src)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            setattr(account, dst, v)
    # Provenance travels WITH the numbers. A token count whose source is
    # PARTIAL_FLOOR is a lower bound, and an aggregator that cannot see which
    # rows are bounds will publish them as totals — so the label is not
    # decoration, it is what keeps the sum honest.
    for src, dst in (("usage_source", "usage_source"),
                     ("usage_completeness", "usage_completeness"),
                     ("telemetry_observation_key", "telemetry_observation_key"),
                     ("quota_scope", "quota_scope"),
                     ("quota_status", "quota_status"),
                     ("quota_observed_at", "quota_observed_at")):
        v = ee.get(src)
        if isinstance(v, str) and v:
            setattr(account, dst, v)
    # `cost_usd` is deliberately NOT set from the price estimate. They are
    # different facts: one is what these tokens list-price at, the other is
    # money that left an account. Under the flat CLI subscriptions this factory
    # runs on, the second is still genuinely unknown.


def close_account(project_dir: Path, account: AttemptAccount, *,
                  exit_class: str, mission_pass: bool,
                  duration_s: float | None = None, candidate_sha: str = "",
                  receipt_sha: str = "", engine_execution: dict | None = None,
                  result=None, consumes_model_quota: bool | None = None) -> None:
    """Terminal row. `mission_pass` is the claim; validation is projected later.

    `result` is the whole ExecutionResult when the caller has it. It is used for
    ONE thing: persisting the full telemetry observations of every launch this
    execution made, under the lease the caller already holds. Passing the result
    rather than a list keeps every terminal path — including the `except
    BaseException` ones — on the same single line of code, so a kill cannot skip
    the measurement of quota it already burned.
    """
    from .resource_accounting import PHASE_TERMINAL
    account.ended_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if duration_s is not None:
        account.duration_s = round(duration_s, 3)
    account.mission_pass = bool(mission_pass)
    account.independently_validated = False   # only settlement may set this
    account.candidate_sha = candidate_sha
    account.receipt_sha = receipt_sha
    # A dispatcher preflight refusal is observed after the durable OPEN row but
    # before any engine process exists. Keep that fact on the TERMINAL row so
    # COST_NO_PROGRESS does not charge a model call that never happened.
    if consumes_model_quota is not None:
        account.consumes_model_quota = bool(consumes_model_quota)
        if (consumes_model_quota is False and account.recovery_allocation_id
                and account.attempt_key):
            # This exact attempt already charged a recovery-allocation launch
            # at open_account, before execve — durable ahead of a SIGKILL, by
            # design. Learning HERE, only after the fact, that no engine ever
            # launched (a dispatcher-side preflight refusal with an empty
            # engine_executions receipt, or a runtime-side refusal that never
            # reached the executor at all) must credit that launch back: a
            # grant exists to pay for launches, not to be drained by attempts
            # that never happened. Idempotent per attempt_key — never a
            # double credit — and never dispatch-fatal: a refund failure is
            # typed and swallowed here exactly like every other bookkeeping
            # side effect in this module (RunRecords, telemetry persistence).
            from . import recovery_allocation as _ra
            try:
                _ra.refund(project_dir, attempt_key=account.attempt_key,
                          allocation_id=account.recovery_allocation_id,
                          reason=f"consumes_model_quota=False at close "
                                 f"(exit_class={exit_class})")
            except _ra.AllocationUnavailable:
                pass  # already refunded, or this attempt never actually spent
    account.phase = PHASE_TERMINAL
    _project_observed(account, engine_execution or {})
    _fold_launch_usage(account, result)
    # FACTORY_LEDGER_ENGINE_KNOWN_V1: the TERMINAL row must carry a TRUE engine.
    # A launch refused before execve (the typed PREFLIGHT_REFUSED exit class —
    # runtime preflight, dispatcher refusal, quota/budget gates) is 'none' with
    # its reason, never UNKNOWN; a launch whose receipt carries an argv or an
    # observed engine_id gets that projection. Only a genuinely unobservable
    # launch keeps the honest UNKNOWN.
    ee = engine_execution or {}
    refusal = str(ee.get("refusal") or ee.get("refusal_reason") or "")
    if exit_class == "PREFLIGHT_REFUSED" and not refusal:
        refusal = exit_class
    if refusal:
        account.engine = engine_for_close({"refusal": refusal})
        account.refusal_reason = refusal
    else:
        projected = engine_for_close({**ee, "argv_head": account.launched_argv})
        if projected not in ("", UNKNOWN):
            account.engine = projected
    account.close(exit_class=exit_class)
    append(account, ledger_path(project_dir))
    written = persist_telemetry(project_dir, result, account=account)
    observations = list(getattr(result, "telemetry_observations", None) or [])
    if written == 0 and observations:
        _record_telemetry_persist_failed(project_dir, account, len(observations))


def _obs_value(obs: dict, field_: str):
    v = ((obs.get("usage") or {}).get(field_) or {}).get("value") if isinstance(obs, dict) else None
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _fold_launch_usage(account: AttemptAccount, result) -> None:
    """FACTORY_TOKEN_TRUTH_V1 — the TERMINAL row carries the tokens of EVERY
    measured launch of its attempt, not only the last one.

    `_project_observed` copies the single `engine_execution` the caller binds
    the receipt to (the last observed launch). A mission that built (glm,
    182 213 tokens measured) and then repaired (codex, unmeasured) closed with
    input_tokens=UNKNOWN — the measured quota vanished from the row. Here the
    execution's telemetry observations (every launch, sidecar-authoritative)
    are folded: >1 measured launch → the fields are SUMS, `usage_source` lists
    the distinct sources, and the completeness is COMPLETE only when every
    launch was measured complete — a sum missing a launch is a floor. No
    measured launch → the row keeps its typed reason (never a zero).
    """
    obs = [o for o in (getattr(result, "telemetry_observations", None) or [])
           if isinstance(o, dict) and o.get("provenance") != "LIVE_OPEN"]
    if not obs:
        return
    measured = [o for o in obs
                if _obs_value(o, "input_tokens") is not None and _obs_value(o, "output_tokens") is not None]
    account.launches_total = len(obs)
    account.launches_measured = len(measured)
    sources = sorted({str((o.get("usage") or {}).get("source") or "") for o in (measured or obs)} - {""})
    if not measured:
        if account.usage_source in ("", UNKNOWN) and sources:
            account.usage_source = "+".join(sources)     # a typed cut, e.g. LOG_TRUNCATED
            account.usage_completeness = "ABSENT"
        return
    already = isinstance(account.input_tokens, (int, float)) and not isinstance(account.input_tokens, bool)
    if len(measured) == 1 and already:
        return                                            # the single launch is already projected exactly
    for field_ in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
                   "reasoning_tokens", "model_turns"):
        known = [v for v in (_obs_value(o, field_) for o in measured) if v is not None]
        if known:
            setattr(account, field_, sum(known))
    if sources:
        account.usage_source = "+".join(sources)
    comps = {str((o.get("usage") or {}).get("completeness") or "") for o in measured}
    account.usage_completeness = ("COMPLETE" if comps <= {"COMPLETE"} and len(measured) == len(obs)
                                  else "PARTIAL_FLOOR")


def _telemetry_gaps_path(project_dir: Path) -> Path:
    return Path(project_dir) / "evidence" / "telemetry_gaps.jsonl"


def _record_telemetry_persist_failed(project_dir: Path, account: AttemptAccount,
                                     observation_count: int) -> None:
    """P1-10: persist_telemetry() swallows a broken `factory_v2.telemetry` import
    (or every row failing to append) and returns 0 with no trace whatsoever — a
    deployment with a broken telemetry dependency then looks identical, in every
    counter close_account touches, to one that simply had no launches to record.
    Best-effort and fail-open by construction: this is a coverage-gap breadcrumb,
    never a gate, so it must never raise or block settlement.
    """
    try:
        gaps_path = _telemetry_gaps_path(project_dir)
        gaps_path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "kind": "TELEMETRY_PERSIST_FAILED",
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "attempt_key": getattr(account, "attempt_key", ""),
            "project": getattr(account, "project", ""),
            "milestone": getattr(account, "milestone", ""),
            "mission": getattr(account, "mission", ""),
            "observation_count": observation_count,
        }
        with open(gaps_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    except Exception:
        pass


def persist_telemetry(project_dir: Path, result, *,
                      account: AttemptAccount | None = None) -> int:
    """Append this execution's launch observations to the telemetry ledger.

    Best-effort by design, and bounded by that design: it runs AFTER the attempt
    row is durable, so a failure here can delay a metric but can never lose an
    attempt, release the COST_NO_PROGRESS brake, or change a verdict. Returns
    the number of rows written — 0 is a coverage gap the report will show, not a
    silent success.
    """
    observations = list(getattr(result, "telemetry_observations", None) or [])
    if not observations:
        return 0
    try:
        from .telemetry import ledger as _tel_ledger
    except Exception:
        return 0
    written = 0
    for obs in observations:
        if not isinstance(obs, dict):
            continue
        # Bind the observation to the attempt that paid for it. The dispatcher
        # runs in its own process and does not always know the attempt key, so
        # the supervisor — which does — stamps it here.
        if account is not None:
            obs = dict(obs)
            obs.setdefault("attempt_key", "")
            if not obs["attempt_key"]:
                obs["attempt_key"] = account.attempt_key
            for field_, value in (("project", account.project),
                                  ("milestone", account.milestone),
                                  ("mission", account.mission),
                                  ("run_id", account.run_id)):
                if not obs.get(field_) and value:
                    obs[field_] = value
        try:
            _tel_ledger.append(obs, project_dir)
            written += 1
        except Exception:
            continue
    return written


# ── F3: the only path that may declare a validated delta ─────────────────────
class AccountingProjectionError(Exception):
    """A verified settlement could not be projected onto its exact attempts."""

    def __init__(self, reason: str, detail: dict):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def _pending_path(project_dir: Path) -> Path:
    return Path(project_dir) / "evidence" / "pending_accounting_projection.json"


def validate_projection_targets(project_dir: Path, *, milestone: str,
                                contract_id: str, verified: list) -> list:
    """Accept ONLY the exact attempts this settlement verified. Sol finding.

    The previous version selected every historical mission_pass attempt of the
    milestone. A settlement resting on the last exact receipts therefore also
    validated superseded attempts that never produced the accepted result — which
    understates wasted quota, inflates accepted results, and wrongly releases
    COST_NO_PROGRESS.

    `verified` carries one entry per mission the settlement actually proved:
    {mission_id, attempt_key, attempt_id, candidate_sha}. Every entry must match a
    ledger row that is TERMINAL, mission_pass, and belongs to this milestone,
    mission and contract, with a matching candidate SHA. Anything else is refused
    rather than quietly skipped: a silent skip is how the over-selection stayed
    invisible.
    """
    rows = {a.get("attempt_key"): a for a in _accounts(project_dir)
            if a.get("attempt_key")}
    keys, refusals = [], []
    for v in verified:
        key = v.get("attempt_key") or ""
        mission_id = v.get("mission_id") or ""
        if not key:
            refusals.append({"mission": mission_id, "why": "NO_ATTEMPT_KEY"})
            continue
        rec = rows.get(key)
        if rec is None:
            refusals.append({"mission": mission_id, "key": key, "why": "KEY_ABSENT"})
            continue
        if rec.get("phase") != "TERMINAL":
            refusals.append({"mission": mission_id, "key": key,
                             "why": f"PHASE_{rec.get('phase')}"})
            continue
        if not rec.get("mission_pass"):
            refusals.append({"mission": mission_id, "key": key,
                             "why": f"NOT_PASS_exit={rec.get('exit_class')}"})
            continue
        if rec.get("milestone") != milestone:
            refusals.append({"mission": mission_id, "key": key,
                             "why": f"WRONG_MILESTONE_{rec.get('milestone')}"})
            continue
        if rec.get("mission") != mission_id:
            refusals.append({"mission": mission_id, "key": key,
                             "why": f"WRONG_MISSION_{rec.get('mission')}"})
            continue
        want_sha = v.get("candidate_sha") or ""
        got_sha = rec.get("candidate_sha") or ""
        if want_sha and got_sha and want_sha != got_sha:
            refusals.append({"mission": mission_id, "key": key,
                             "why": "CANDIDATE_SHA_MISMATCH"})
            continue
        keys.append(key)
    if refusals:
        raise AccountingProjectionError("PROJECTION_TARGET_REFUSED",
                                        {"milestone": milestone,
                                         "contract_id": contract_id,
                                         "refusals": refusals})
    return keys


def project_verified_attempts(project_dir: Path, *, milestone: str,
                              contract_id: str, verified: list,
                              receipt_sha: str = "") -> list:
    """Project the independent settlement onto its exact attempts, exactly once.

    A PENDING marker is written first, so a crash between here and completion is
    replayed at boot rather than lost. project_independent_validation() skips keys
    already carrying the fact, so replay cannot double-count.
    """
    keys = validate_projection_targets(project_dir, milestone=milestone,
                                       contract_id=contract_id, verified=verified)
    pending = _pending_path(project_dir)
    pending.parent.mkdir(parents=True, exist_ok=True)
    payload = {"milestone": milestone, "contract_id": contract_id,
               "attempt_keys": keys, "receipt_sha": receipt_sha}
    tmp = pending.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, pending)

    done = project_independent_validation(ledger_path(project_dir), keys,
                                          receipt_sha=receipt_sha)
    pending.unlink(missing_ok=True)
    return done


def resume_pending_projection(project_dir: Path) -> list:
    """Replay an interrupted projection at boot. Idempotent."""
    pending = _pending_path(project_dir)
    if not pending.is_file():
        return []
    try:
        payload = json.loads(pending.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pending.unlink(missing_ok=True)
        return []
    done = project_independent_validation(
        ledger_path(project_dir), payload.get("attempt_keys") or [],
        receipt_sha=payload.get("receipt_sha", ""))
    pending.unlink(missing_ok=True)
    return done


# ── F2: restart reconciliation ───────────────────────────────────────────────
def reconcile_on_boot(project_dir: Path) -> list:
    """Close attempts whose process died mid-flight. Exactly once, never a claim."""
    account_ledger = ledger_path(project_dir)
    open_keys = {
        row.get("attempt_key") for row in fold(read_rows(account_ledger))
        if row.get("phase") == "OPEN" and row.get("attempt_key")
    }
    observed: dict = {}
    if open_keys:
        try:
            from .telemetry import ledger as _tel_ledger
            for sidecar in (Path(project_dir) / "evidence" / "dispatches").glob(
                    "*/*.telemetry.jsonl"):
                for line in sidecar.read_text(encoding="utf-8",
                                              errors="replace").splitlines():
                    try:
                        obs = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    key = obs.get("attempt_key")
                    if key not in open_keys:
                        continue
                    _tel_ledger.append(obs, project_dir)
                    usage = obs.get("usage") or {}
                    values = {}
                    for field in ("input_tokens", "output_tokens", "cache_read_tokens",
                                  "cache_write_tokens", "reasoning_tokens", "model_turns",
                                  "provider_requests"):
                        value = (usage.get(field) or {}).get("value")
                        if isinstance(value, (int, float)):
                            values[field] = value
                    observed[key] = {
                        "engine": obs.get("engine", ""),
                        "model": obs.get("model_observed") or obs.get("model_requested", ""),
                        "launched_argv": obs.get("argv_redacted") or [],
                        "launched_pid": obs.get("pid"),
                        "telemetry_observation_key": obs.get("observation_key", ""),
                        "usage_source": usage.get("source", ""),
                        "usage_completeness": usage.get("completeness", "ABSENT"),
                        **values,
                    }
        except Exception:
            pass
    def _is_live(rec: dict) -> bool:
        # a sibling slot's launch (intra-project parallel) is OPEN while its
        # worker heartbeats — that is a live attempt, not a lost one.
        # FCO_SYNTH-001 (fail-closed observation): a probe failure is NOT "dead".
        # This used to swallow every exception as False, so an unreadable
        # heartbeat or a broken probe closed a live attempt as KILL/PROCESS_LOST —
        # bypassing the LivenessObservationUnknown contract of
        # reconcile_open_attempts (L2_runtime_core-012) at its only production
        # caller. Let the error propagate: reconcile_open_attempts types it as
        # UNKNOWN and the control plane stops the cycle (LOST_ATTEMPT_RECONCILE_UNKNOWN).
        from .mission_evidence import is_worker_alive
        return bool(rec.get("mission")) and is_worker_alive(
            Path(project_dir), str(rec.get("mission")))
    lost = reconcile_open_attempts(account_ledger, observed_by_attempt=observed,
                                   is_live=_is_live)
    if lost:
        # Reliable telemetry, interruption included: a lost attempt whose
        # model launch was metered (durable OPEN event) gets its terminal
        # INTERRUPTED event HERE — a hard kill runs no `finally`, so the
        # pre-launch OPEN event is what proves the interruption afterwards.
        # Non-metered (local, no model quota) attempts emit nothing: there was
        # no model call. Best-effort and visible, never boot-bricking.
        try:
            from .telemetry.gate import emit_interrupted_for_lost, read_events
            metered = {e.get("attempt_key") for e in read_events(project_dir)
                       if e.get("phase") == "OPEN"}
            for key in lost:
                if key in metered:
                    emit_interrupted_for_lost(project_dir, attempt_key=key)
        except Exception:
            pass
    return lost
