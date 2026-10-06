"""Factory-only verifiers split out of the historic factory_v2/product_verifiers.py
(FACTORY_SIMPLIFY_S3): everything here gates factory-owned milestones (external
supervisor mechanics, run-record wiring, resource telemetry true-cost, the
automated multi-AI CTO audit loop). The Twitter Radar (TR_*) verifiers moved to
products/twitter_radar/product_verifiers.py; this module never imports from
there except the small shared verifier scaffolding (VerifierResult, _fail, _ok,
_not_implemented, _load), which is not TR-specific.

Invoked (combined with the TR verifiers) via the factory_v2/product_verifiers.py
shim: python3 -m factory_v2.product_verifiers <MILESTONE_ID> --project-repo .
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from products.twitter_radar.product_verifiers import VerifierResult, _fail, _ok, _not_implemented, _load

def verify_external_supervisor(repo: Path) -> VerifierResult:
    """EXTERNAL_SUPERVISOR_NO_MANUAL_RELAUNCH — recomputed live, never authored.

    The verifier RUNS the isolated launchd proof itself (dummy process, unique
    label, disposable namespace) and recomputes every acceptance criterion from
    raw launchctl/ps evidence: forced-kill relaunch with NEW pid+birth,
    launchd-owned from birth, stable executable bytes, no restart storm, clean
    exit stays down, no residue. It never reads a proof receipt — a receipt is
    a claim, and this gate exists to recompute.

    launchd unavailability means the outcome CANNOT be evaluated (exit 2), never
    a pass. The production factoryd install stays deferred to independent review
    (Order 024 G4); this proves the supervisor mechanics in isolation.
    """
    from .external_supervisor_harness import REQUIRED_CRITERIA, run_isolated_proof, run_containment_proof
    try:
        observed = run_isolated_proof()
    except RuntimeError as harness_err:
        return _not_implemented(
            "EXTERNAL_SUPERVISOR_NO_MANUAL_RELAUNCH",
            f"the harness could not exercise launchd, so the outcome cannot be "
            f"evaluated: {harness_err}")
    try:
        # D018: typed containment violation -> fence + evidence + global quarantine
        # surviving supervisor death, cleared only by the safe-clear contract.
        containment = run_containment_proof()
    except RuntimeError as containment_err:
        return _not_implemented(
            "EXTERNAL_SUPERVISOR_NO_MANUAL_RELAUNCH",
            f"the containment harness could not exercise launchd: {containment_err}")
    _evid = dict(observed.get("evidence") or {})
    _evid.update(containment.get("evidence") or {})
    # Shared boolean criteria AND together: a False in EITHER proof must never be
    # masked by a True in the other through the dict merge.
    for _k, _v in containment.items():
        if _k in observed and isinstance(_v, bool) and isinstance(observed[_k], bool):
            containment[_k] = observed[_k] and _v
    observed.update(containment)
    observed["evidence"] = _evid
    failed = [c for c in REQUIRED_CRITERIA if not observed.get(c)]
    if failed:
        return _fail(
            f"isolated supervisor proof observed live, but criteria failed: {failed} "
            f"(evidence: {observed.get('evidence')})")
    return _ok(
        f"observed live in an isolated launchd namespace: forced SIGKILL relaunch "
        f"with new pid/birth {observed['evidence']['pid1']}→{observed['evidence']['pid2']}, "
        f"launchd-owned (ppid=1), executable sha stable, no storm, clean exit stays "
        f"down, no residue; D018 containment proven live: typed CONTAINMENT_VIOLATION "
        f"-> worker fenced + evidence preserved + GLOBAL_QUARANTINE surviving the "
        f"supervisor's own SIGKILL/relaunch, cleared ONLY by safe-clear v2 (typed "
        f"owner-sensitive exception + gate-observed remediation; text reason, "
        f"exception-less receipt and unremediated boundary each observed REFUSED) "
        f"(supervisor pid {observed['evidence'].get('supervisor_pid1')}→"
        f"{observed['evidence'].get('supervisor_pid2')}). Boundary scope: the "
        f"supervisor enforces its state-dir filesystem boundary; credential access, "
        f"network egress and validator mutation outside that boundary are NOT "
        f"observed by this unit (OS sandbox + engine policy own them). "
        "DUMMY_ISOLATED_NOT_PRODUCTION — production install still deferred to "
        "independent review")


def verify_runrecords_wired(repo: Path) -> VerifierResult:
    """RUN_RECORDS_MINIMUM_WIRED — recomputed from a real killed/restarted run.

    The verifier drives a disposable sandbox through the REAL V2 runtime in
    subprocesses: one run is SIGKILLed mid-mission, a cold restart completes it.
    It then recomputes the two-phase contract from the append-only ledger alone:
    PREDICTION durable before its attempt, OBSERVATION exactly once per
    attempt_id, the crash orphan closed WORKER_DEATH exactly once, observations
    carrying engine/duration/authority identity, and validated=True present only
    on settlement records (never on attempts). No authored report is read.
    """
    from .runrecords_wire_harness import read_ledger, run_killed_then_restarted_run
    try:
        run = run_killed_then_restarted_run()
    except Exception as harness_err:
        return _not_implemented(
            "RUN_RECORDS_MINIMUM_WIRED",
            f"the harness could not exercise the runtime, so the outcome cannot be "
            f"evaluated: {harness_err}")
    if not run.get("killed_mid_run"):
        return _not_implemented(
            "RUN_RECORDS_MINIMUM_WIRED",
            "the first run finished before the forced kill landed — the kill/restart "
            "criterion cannot be evaluated this time (rerun)")
    if run.get("restart_rc") != 0:
        return _fail(f"cold restart did not complete the run: rc={run['restart_rc']} "
                     f"{run['restart_stdout_tail'][-200:]}")
    records = read_ledger(Path(run["ledger"]))
    preds = [r for r in records if r.get("phase") == "PREDICTION"]
    obs = [r for r in records if r.get("phase") == "OBSERVATION"]
    if not preds:
        return _fail("no PREDICTION records — the runtime dispatched without predicting")
    pred_ids = [p["attempt_id"] for p in preds]
    obs_ids = [o["attempt_id"] for o in obs]
    dup_preds = {i for i in pred_ids if pred_ids.count(i) > 1}
    if dup_preds:
        return _fail(f"duplicate PREDICTION for the same attempt: {sorted(dup_preds)}")
    missing_close = sorted(set(pred_ids) - set(obs_ids))
    if missing_close:
        return _fail(f"PREDICTIONs never closed (reconciliation broken): {missing_close}")
    dup_obs = {i for i in obs_ids if obs_ids.count(i) > 1}
    if dup_obs:
        return _fail(f"attempt closed more than once: {sorted(dup_obs)}")
    # ordering: each PREDICTION line precedes its OBSERVATION line in the file
    last_pred_line = {}
    first_obs_line = {}
    for idx, r in enumerate(records):
        if r.get("phase") == "PREDICTION":
            last_pred_line[r["attempt_id"]] = idx
        elif r.get("phase") == "OBSERVATION":
            first_obs_line.setdefault(r["attempt_id"], idx)
    misordered = [aid for aid in pred_ids
                  if aid in last_pred_line and aid in first_obs_line
                  and last_pred_line[aid] > first_obs_line[aid]]
    if misordered:
        return _fail(f"observation precedes its prediction for: {misordered}")
    deaths = [o for o in obs
              if (o.get("observation") or {}).get("close_reason") == "WORKER_DEATH"]
    if len(deaths) != 1:
        return _fail(
            f"exactly one WORKER_DEATH close expected from the forced kill, found "
            f"{len(deaths)}")
    if not deaths[0].get("reconciled"):
        return _fail("the WORKER_DEATH close must be marked reconciled (idempotent close)")
    for o in obs:
        ob = o.get("observation") or {}
        if ob.get("close_reason") == "WORKER_DEATH":
            continue  # the crash close's contract is close_reason+reconciled only
        for field in ("outcome", "duration_s", "engine", "authority_hashes"):
            if ob.get(field) in (None, ""):
                return _fail(f"observation {o['attempt_id']} missing {field}")
    settlements = [r for r in records if r.get("validated") is True]
    attempts_bad = [r for r in records
                    if r.get("validated") is True and r.get("phase")]
    if attempts_bad:
        return _fail("phase records must never carry validated=True")
    if not settlements:
        return _fail("no validated settlement record — routing signal cannot exist")
    return _ok(
        f"recomputed from the append-only ledger of a real SIGKILL+restart run: "
        f"{len(preds)} predictions each closed exactly once (1 "
        f"WORKER_DEATH), prediction-before-observation ordering holds, observations "
        f"carry engine/duration/authority identity, validated=True only on "
        f"{len(settlements)} settlement record(s)")


#: Minimum real evidence before this milestone may settle. Not a quality bar —
#: a liveness bar. Below these the recomputation is checking that nothing equals
#: nothing, which is true of a factory that never measured anything.
MIN_TELEMETRY_ROWS = 3
MIN_MEASURED_ROWS = 1

#: Fields a measured row must actually carry. A row that reports COMPLETE usage
#: while leaving its own identity UNKNOWN is not a measurement anyone can use.
REQUIRED_MEASURED_FIELDS = ("observation_key", "engine", "usage")


def _first_unknown_reason(row: dict) -> str:
    usage = row.get("usage") or {}
    for field in ("input_tokens", "output_tokens"):
        slot = usage.get(field)
        if isinstance(slot, dict) and slot.get("value") == "UNKNOWN":
            return str(slot.get("reason") or "UNSPECIFIED")
    return ""


def verify_resource_telemetry_true_cost(repo: Path) -> VerifierResult:
    """RESOURCE_TELEMETRY_TRUE_COST — every published number RECOMPUTED from raw.

    Nothing in the published receipt is trusted. The verifier rebuilds the
    aggregates from the append-only telemetry ledger and the attempt ledger, and
    fails closed on any of the ways this measurement could quietly become a
    fiction:

      * a published total that does not reconstruct from the raw rows;
      * an UNKNOWN that has been replaced by 0 — the single most tempting lie
        here, because 0 sums silently and UNKNOWN does not;
      * an UNKNOWN with no reason, which is an unknown nobody can act on;
      * a PARTIAL_FLOOR row summed into an exact total, which publishes a lower
        bound as a measurement;
      * a cost-per-result divided by `mission_pass` rather than
        `independently_validated`;
      * a price ESTIMATE copied into `cost_usd`, i.e. an invented transaction;
      * a quota figure derived from token counts;
      * prompt, source or reasoning CONTENT in the telemetry ledger.

    The last one is checked structurally: every telemetry row must consist only
    of allowlisted keys, and its raw provider events only of allowlisted counter
    keys. A verifier that trusted the writer not to log a prompt would be
    checking the intention rather than the artifact.
    """
    from .telemetry import aggregate as _agg
    from .telemetry import ledger as _tl
    from .telemetry.record import RAW_EVENT_ALLOWLIST, UNKNOWN as _UNK
    from .telemetry.record import UNKNOWN_REASONS

    receipt_rel = "factory_v2/receipts/RESOURCE_TELEMETRY_RECEIPT.json"
    receipt, err = _load(repo, receipt_rel)
    if receipt is None:
        return _not_implemented(
            "RESOURCE_TELEMETRY_TRUE_COST",
            f"the published receipt ({err}). Nothing to recompute against: a "
            f"milestone cannot settle on a measurement that was never published.")

    project_dirs = receipt.get("project_dirs")
    if not isinstance(project_dirs, list) or not project_dirs:
        return _fail("receipt does not name the project_dirs its numbers came "
                     "from, so no independent recomputation is possible")

    # Ledger roots must sit inside the repo under review. A receipt that points
    # its evidence at an arbitrary absolute path is not independently checkable:
    # whoever wrote the receipt also chose where the "evidence" lives, and the
    # verifier would be recomputing against a directory the builder controls.
    resolved_dirs = []
    for pd in project_dirs:
        p = Path(pd)
        p = p if p.is_absolute() else (repo / pd)
        try:
            p = p.resolve()
            root = repo.resolve()
        except OSError as exc:
            return _fail(f"project_dir {pd!r} unresolvable: {exc}")
        if root != p and root not in p.parents:
            return _fail(
                f"project_dir {pd!r} resolves outside the repo under review "
                f"({p} not under {root}). Evidence must live in the tree being "
                f"verified, or the recomputation proves nothing about it")
        resolved_dirs.append(p)

    rows = []
    accounts = []
    for p in resolved_dirs:
        rows.extend(_tl.fold(_tl.read_rows(p)))
        try:
            from .launch_gate import ledger_path as _lp
            from .resource_accounting import fold as _fold, read_rows as _rr
            accounts.extend(_fold(_rr(_lp(p))))
        except Exception as exc:
            return _fail(f"attempt ledger for {p} unreadable: {exc}")

    # POSITIVE EVIDENCE, not merely self-consistency. An empty ledger trivially
    # reconstructs an empty aggregate, so "the numbers match" was satisfiable by
    # measuring nothing at all. This milestone is the claim that the factory
    # MEASURES ITS ENGINES, so it requires launches that were actually measured.
    measured_rows = [r for r in rows
                     if (r.get("usage") or {}).get("completeness") in
                     ("COMPLETE", "PARTIAL_FLOOR")]
    if len(rows) < MIN_TELEMETRY_ROWS or len(measured_rows) < MIN_MEASURED_ROWS:
        return _not_implemented(
            "RESOURCE_TELEMETRY_TRUE_COST",
            f"positive evidence that the producer→consumer chain ran: found "
            f"{len(rows)} telemetry row(s) of which {len(measured_rows)} carry a "
            f"real measurement, below the required {MIN_TELEMETRY_ROWS}/"
            f"{MIN_MEASURED_ROWS}. An empty ledger reconstructs an empty "
            f"aggregate perfectly and proves nothing — this milestone cannot be "
            f"settled by measuring no launches")

    # PRODUCER→CONSUMER BINDING. Measured rows must be reconcilable with the
    # attempt ledger that the supervisor writes on a different code path, at a
    # different moment, under the worker lease. A telemetry row with no attempt
    # behind it is a number with no launch behind it.
    #
    # This does NOT make the evidence unforgeable, and the receipt has to SAY so
    # — checked below, not assumed: the builder engine runs with write access to
    # this tree, so anything in-tree is in principle authorable by the thing
    # being measured. What the join buys is that a forgery must now be
    # CONSISTENT across two ledgers with different schemas, different writers
    # and different keys, and that the failure is loud rather than silent. The
    # real fix is an out-of-tree observation channel.
    account_keys = {a.get("telemetry_observation_key") for a in accounts
                    if a.get("telemetry_observation_key")}
    bound = [r for r in measured_rows
             if r.get("observation_key") in account_keys]
    if account_keys and not bound:
        return _fail(
            f"not one of {len(measured_rows)} measured telemetry row(s) joins "
            f"the attempt ledger on telemetry_observation_key "
            f"({len(account_keys)} attempt row(s) carry one). The two ledgers "
            f"describe different launches, so neither corroborates the other")
    if not account_keys:
        return _not_implemented(
            "RESOURCE_TELEMETRY_TRUE_COST",
            "an attempt ledger that references its telemetry rows. No "
            "AttemptAccount row carries a telemetry_observation_key, so the "
            "telemetry ledger stands alone and nothing independently corroborates "
            "that these launches happened. Run a real dispatch at this SHA")

    # Per-ENGINE coverage. Every engine that actually ran must have produced a
    # measured row, or be honestly typed as unmeasurable for a reason that is
    # NOT our own argv — "we forgot to ask for usage" is not an excuse the
    # milestone accepts.
    engines_seen = {str(r.get("engine") or "UNKNOWN") for r in rows
                    if r.get("engine") not in (None, "", "local", "UNKNOWN")}
    unproven = []
    for eng in sorted(engines_seen):
        eng_rows = [r for r in rows if r.get("engine") == eng]
        if any(r in measured_rows for r in eng_rows):
            continue
        reasons = {_first_unknown_reason(r) for r in eng_rows}
        if reasons & {"NOT_EMITTED_BY_INVOCATION", "LAUNCH_PATH_UNINSTRUMENTED"}:
            unproven.append(f"{eng} (reason {sorted(reasons)} — our invocation)")
        elif not reasons - {"NOT_EMITTED_BY_PROVIDER",
                            "UNOBSERVABLE_FROM_SUPERVISOR"}:
            continue                      # honestly unmeasurable, not our doing
        else:
            unproven.append(f"{eng} (reason {sorted(reasons)})")
    if unproven:
        return _fail(
            "engines ran without a single measured row, for reasons that are "
            "ours to fix: " + "; ".join(unproven))

    # 1. content safety, checked on the artifact rather than trusted
    allowed_top = set(_LaunchObservation_fields())
    for row in rows:
        stray = sorted(set(row) - allowed_top - {"schema"})
        if stray:
            return _fail(f"telemetry row carries non-schema keys {stray} — the "
                         f"ledger must hold counters, never captured content")
        for ev in row.get("raw_usage_events") or []:
            if not isinstance(ev, dict):
                continue
            bad = sorted(set(ev) - set(RAW_EVENT_ALLOWLIST))
            if bad:
                return _fail(f"raw provider event carries non-allowlisted keys "
                             f"{bad} — prompts/reasoning content must never be stored")

    # 2. no UNKNOWN was replaced by 0, and every UNKNOWN carries a reason
    for row in rows:
        usage = row.get("usage") or {}
        for field in ("input_tokens", "output_tokens", "cache_read_tokens",
                      "cache_write_tokens", "reasoning_tokens"):
            slot = usage.get(field)
            if not isinstance(slot, dict):
                return _fail(f"{field} is not a typed slot in row "
                             f"{row.get('observation_key', '?')} — a bare number "
                             f"has no source and no unknown-reason")
            if slot.get("value") == _UNK and slot.get("reason") not in UNKNOWN_REASONS:
                return _fail(f"{field} is UNKNOWN without a recognised reason in "
                             f"row {row.get('observation_key', '?')}")
        if usage.get("completeness") == "ABSENT":
            for field in ("input_tokens", "output_tokens"):
                if isinstance(usage.get(field), dict) and \
                        usage[field].get("value") == 0:
                    return _fail(
                        f"row {row.get('observation_key', '?')} reports ABSENT "
                        f"usage but published {field}=0 — an unmeasured quantity "
                        f"must stay UNKNOWN, never become zero")

    # 2b. a measured row must carry its own identity and a disjointness note.
    # A COMPLETE usage block on a row whose engine is UNKNOWN cannot be
    # attributed to anything, and an unattributable number is not evidence.
    for row in measured_rows:
        missing = [f for f in REQUIRED_MEASURED_FIELDS
                   if not row.get(f) or row.get(f) == _UNK]
        if missing:
            return _fail(f"measured row {row.get('observation_key', '?')} is "
                         f"missing required field(s) {missing}")
        usage = row.get("usage") or {}
        if not usage.get("source") or usage["source"] == _UNK:
            return _fail(f"measured row {row.get('observation_key', '?')} has no "
                         f"usage source — a number without a provenance is "
                         f"indistinguishable from one that was typed in")
        # Disjointness: when the provider also reported its own total, the five
        # common fields must re-sum to it. This is what catches an adapter that
        # mapped a nested field straight across (codex nests cached input inside
        # input_tokens; copying it doubled one real session by 79.5M tokens).
        provider_total = usage.get("total_tokens")
        if isinstance(provider_total, dict) and \
                isinstance(provider_total.get("value"), (int, float)) and \
                usage.get("completeness") == "COMPLETE" and \
                str(usage.get("source", "")).startswith("codex."):
            parts = 0
            for f in ("input_tokens", "output_tokens", "cache_read_tokens",
                      "cache_write_tokens", "reasoning_tokens"):
                slot = usage.get(f)
                if isinstance(slot, dict) and isinstance(slot.get("value"),
                                                         (int, float)):
                    parts += slot["value"]
            if parts != provider_total["value"]:
                return _fail(
                    f"row {row.get('observation_key', '?')}: the five common "
                    f"token fields sum to {parts} but the provider's own total "
                    f"is {provider_total['value']} — they are not disjoint, so "
                    f"every aggregate built on them is wrong")

    # 3. cost_usd was never filled in from the price estimate
    for a in accounts:
        if a.get("cost_usd") not in ("", None, _UNK):
            est = a.get("api_price_estimate_usd")
            return _fail(
                f"attempt {a.get('attempt_key', '?')} published cost_usd="
                f"{a['cost_usd']!r} (price estimate {est!r}). Money actually "
                f"charged is not reported by any launched CLI; publishing it "
                f"invents a transaction")

    # 4. the aggregates reconstruct, exactly
    recomputed = _agg.summarise(rows, accounts)
    published = receipt.get("summary")
    if not isinstance(published, dict):
        return _fail("receipt has no `summary` block to reconstruct")
    mismatches = _diff_numbers(published, recomputed, path="summary")
    if mismatches:
        return _fail("published aggregates do not reconstruct from the raw "
                     "ledger: " + "; ".join(mismatches[:6]))

    # 5. the denominator is the validated one
    acc = recomputed.get("accepted") or {}
    if acc.get("denominator") != "independently_validated":
        return _fail("cost ratios are not divided by independently_validated")
    per = recomputed.get("per_accepted_milestone") or {}
    n_ms = acc.get("independently_accepted_milestones")
    if not n_ms and per.get("status") != "UNDEFINED_ZERO_DENOMINATOR":
        return _fail("zero accepted milestones must make cost-per-result "
                     "UNDEFINED, not a number")

    # 6. floors were not summed as totals
    totals = recomputed.get("totals") or {}
    if totals.get("rows_measured_lower_bound") and \
            totals.get("tokens_lower_bound_only") == _UNK:
        return _fail("lower-bound rows exist but no lower-bound total is "
                     "published — they were folded into the exact total")

    # 7. quota was observed, never derived
    windows = (recomputed.get("quota_windows") or {}).get("windows") or {}
    for scope, w in windows.items():
        # Every adapter that can emit a quota window, not a stale subset: a
        # legitimate codex reading was being rejected as unsourced.
        if not str(w.get("source", "")).startswith(
                ("claude.", "opencode.", "codex.")):
            return _fail(f"quota window {scope} has no provider source — a quota "
                         f"figure must be a provider observation, never derived "
                         f"from token counts")

    # 7b. the receipt must PUBLISH its own limits. This verifier previously
    # claimed the forgeability residual was "named in known_unknowns" while no
    # such field existed anywhere — a limit documented only in a source comment
    # is not disclosed to whoever settles the milestone on the receipt.
    disclosed = receipt.get("known_unknowns")
    if not isinstance(disclosed, list) or not disclosed:
        return _fail(
            "receipt publishes no `known_unknowns`. The evidence is in-tree and "
            "therefore writable by the engine being measured; a receipt that "
            "does not state that limit overstates what it proves")
    if not any(d.get("id") == "IN_TREE_EVIDENCE_IS_CLAIMANT_WRITABLE"
               for d in disclosed if isinstance(d, dict)):
        return _fail(
            "receipt's `known_unknowns` omits IN_TREE_EVIDENCE_IS_CLAIMANT_WRITABLE, "
            "which is the residual this verifier cannot close")

    # 8. a quota reading must not be re-dated to now. An imported historical
    # window presented with a fresh timestamp is the difference between "your
    # account was 90% used a week ago" and "your account is 90% used".
    for scope, w in windows.items():
        src = str(w.get("observed_at_source") or "")
        if w.get("observed_at") in ("", _UNK) and src not in ("", _UNK):
            return _fail(f"quota window {scope} claims a dating source {src!r} "
                         f"with no observed_at to date")
        if src == "provider_event_timestamp" and not w.get("observed_at"):
            return _fail(f"quota window {scope} claims a provider timestamp it "
                         f"does not have")

    covered = (totals.get("rows_measured_complete", 0)
               + totals.get("rows_measured_lower_bound", 0))
    return _ok(
        f"recomputed from {len(rows)} raw telemetry row(s) ({len(measured_rows)} "
        f"carrying a real measurement across engines "
        f"{sorted(engines_seen) or ['none']}) and {len(accounts)} attempt row(s), "
        f"all under {[str(p) for p in resolved_dirs]} inside the repo: every "
        f"published aggregate reconstructs; {covered} row(s) measured "
        f"({totals.get('rows_measured_lower_bound', 0)} as lower bounds), "
        f"{totals.get('rows_unmeasured', 0)} unmeasured and reported as UNKNOWN "
        f"with a reason; token fields disjoint and re-summing to provider totals; "
        f"cost_usd never filled from a price estimate; denominator is "
        f"independently_validated ({n_ms} accepted milestone(s)); quota windows "
        f"provider-observed and provider-dated only; no non-allowlisted key in "
        f"any row")


def _LaunchObservation_fields() -> set:
    from dataclasses import fields as _fields
    from .telemetry.record import LaunchObservation
    return {f.name for f in _fields(LaunchObservation)}


def _diff_numbers(published, recomputed, *, path: str = "") -> list:
    """Every numeric leaf the receipt published must equal the recomputation.

    Walks the published structure, not the recomputed one: a receipt may omit a
    section, but anything it DOES claim has to survive being recomputed. Extra
    keys in the recomputation are new measurements, not discrepancies.
    """
    out = []
    if isinstance(published, dict):
        if not isinstance(recomputed, dict):
            return [f"{path}: recomputation has no such block"]
        for key, value in published.items():
            out.extend(_diff_numbers(value, recomputed.get(key),
                                     path=f"{path}.{key}"))
        return out
    if isinstance(published, list):
        if not isinstance(recomputed, list) or len(published) != len(recomputed):
            return [f"{path}: list length {len(published)} vs "
                    f"{len(recomputed) if isinstance(recomputed, list) else 'absent'}"]
        for i, value in enumerate(published):
            out.extend(_diff_numbers(value, recomputed[i], path=f"{path}[{i}]"))
        return out
    if isinstance(published, bool) or not isinstance(published, (int, float)):
        if isinstance(published, str) and published != recomputed:
            out.append(f"{path}: published {published!r} vs recomputed "
                       f"{recomputed!r}")
        return out
    if not isinstance(recomputed, (int, float)) or isinstance(recomputed, bool):
        return [f"{path}: published {published} but recomputation gives "
                f"{recomputed!r}"]
    # L5_factory_v2_rest_1-017: NaN compares unequal to everything, so
    # abs(NaN - x) > 1e-6 was False and a NaN aggregate survived verification.
    from factory_v2.shared_parsers import is_finite_number
    if not is_finite_number(published) or not is_finite_number(recomputed):
        return [f"{path}: published {published!r} vs recomputed {recomputed!r} "
                f"(non-finite number)"]
    if abs(float(published) - float(recomputed)) > 1e-6:
        return [f"{path}: published {published} vs recomputed {recomputed}"]
    return out


def verify_audit_loop_cell(repo: Path) -> VerifierResult:
    """AUTOMATED_MULTI_AI_CTO_LOOP_V1 — recomputed live, never authored (A11).

    The verifier RUNS the isolated-but-real exercise itself (real failure
    signal through the two-phase RunRecords API -> real trigger -> real
    challenger/reviewer subprocesses -> deterministic synthesis -> redirect ->
    position fingerprints), then recomputes every acceptance criterion from
    the durable records the exercise leaves on disk: bundle integrity, role
    process identities, single-flight exactly-one, synthesis reproducibility,
    capability fail-closed probes re-executed live, peer-consensus-vs-gate
    ordering re-executed live, zero attributable writes, exact-position
    resume. A missing or malformed record fails closed; the exercise receipt
    is never trusted as proof.
    """
    import shutil
    import tempfile
    from ._a1a6_gate import audit_cell_reachable_from_runtime

    # CTO DIRECTIVE 033 §3 R1 — A1/A6 reachability, checked BEFORE the exercise runs.
    #
    # A1 ("a REAL failure automatically triggers the cell") and A6 ("the writer is
    # redirected automatically and the product resumes at the EXACT prior position")
    # are properties of the PRODUCTION DISPATCH PATH.  run_audit_cell_exercise()
    # cannot establish them at any level of fidelity: it is a harness, not advance().
    #
    # This ordering is load-bearing.  On 2026-09-09 this milestone settled COMPLETE at
    # f253f439 while runtime.py contained zero references to the cell, because the
    # verifier ran the exercise first and then graded its output.  Gate first.
    reachable, _a1a6_detail = audit_cell_reachable_from_runtime(Path(__file__).resolve().parent.parent)
    if not reachable:
        return _fail(_a1a6_detail)

    from .audit_cell_exercise import run_audit_cell_exercise
    from .audit_loop import (
        CapabilityDenial, CapabilityEnvelope, CredentialLeakRefused,
        EvidenceBundle, finalize_synthesis, synthesize, MAX_ADVERSARIAL_ROUNDS)
    keep = Path(tempfile.mkdtemp(prefix="factory-verify-audit-cell-"))
    try:
        try:
            receipt = run_audit_cell_exercise(keep_under=keep)
        except Exception as harness_err:
            return _not_implemented(
                "AUTOMATED_MULTI_AI_CTO_LOOP_V1",
                f"the exercise could not run, so the outcome cannot be "
                f"evaluated: {harness_err}")
        cell_path = Path(receipt["cell_record"])
        if not cell_path.exists():
            return _fail(f"cell record missing: {cell_path}")
        cell = json.loads(cell_path.read_text(encoding="utf-8"))
        if cell.get("schema") != "audit-cell-record/v1":
            return _fail(f"cell record schema {cell.get('schema')!r} unexpected")
        bundle = EvidenceBundle(**{k: v for k, v in cell["bundle"].items()
                                   if k in EvidenceBundle.__dataclass_fields__})
        if not bundle.verify_integrity():
            return _fail("bundle sha does not recompute — frozen evidence tampered")
        trig = cell.get("trigger")
        if trig != "REPEATED_EQUIVALENT_FAILURE":
            return _fail(f"trigger {trig!r} is not the real repeated-failure trigger")
        if cell["bundle"]["failure_count"] < 2 or \
                len(cell["bundle"]["strategies_attempted"]) < 2:
            return _fail("trigger records do not show two distinct failed strategies")
        # A2 + real processes: every role round carries an observed pid+argv
        # and the challenger left the tracked tree untouched.
        for rnd in cell["challenger_rounds"]:
            ident = rnd.get("identity") or {}
            if not ident.get("pid") or not ident.get("argv"):
                return _fail("challenger round lacks a real process identity")
            if not rnd.get("repo_digest_before") or \
                    rnd["repo_digest_before"] != rnd.get("repo_digest_after"):
                return _fail("challenger attributable write detected (A2)")
        reviewer_jobs = set()
        for rnd in cell["reviewer_rounds"]:
            ident = rnd.get("identity") or {}
            if not ident.get("pid") or not ident.get("argv"):
                return _fail("reviewer round lacks a real process identity")
            verdict = rnd.get("verdict") or {}
            if verdict.get("verdict") not in ("ACCEPT", "REJECT", "REDIRECT",
                                              "QUARANTINE"):
                return _fail(f"untyped reviewer verdict {verdict.get('verdict')!r}")
            if verdict.get("reviewed_sha") != cell["candidate_sha"]:
                return _fail("verdict not bound to the exact candidate SHA (A3)")
            reviewer_jobs.add(rnd.get("job_key"))
        if not reviewer_jobs:
            return _fail("no reviewer single-flight job recorded")
        # D29: exactly one job per key, one generation, settled PASS once.
        for key in reviewer_jobs:
            job_path = cell_path.parent.parent / "validation_jobs" / f"{key}.json"
            if not job_path.exists():
                return _fail(f"single-flight job record missing: {key}")
            job = json.loads(job_path.read_text(encoding="utf-8"))
            if job.get("generation") != 1:
                return _fail(f"job {key} generation {job.get('generation')} — "
                             f"a duplicate/replacement ran")
            if (job.get("result") or {}).get("classified") != "PASS":
                return _fail(f"job {key} not settled PASS")
            ev = Path(job["result"].get("evidence_path") or "")
            if not ev.exists():
                return _fail(f"job {key} durable verdict artifact missing")
            if hashlib.sha256(ev.read_bytes()).hexdigest()[:16] != \
                    next(r["verdict_sha16"] for r in cell["reviewer_rounds"]
                         if r.get("job_key") == key):
                return _fail(f"job {key} verdict artifact hash drift")
        # A4: synthesis reproducible from its RECORDED inputs.
        recorded = cell["synthesis"]
        redone = finalize_synthesis(synthesize(
            cell["bundle"]["bundle_sha"],
            gate_failed=cell["gate_failed"],
            challenger_findings=cell["challenger_rounds"][-1]["findings"],
            reviewer_verdicts=[cell["reviewer_rounds"][-1]["verdict"]],
            rounds_used=recorded["rounds_used"]))
        if redone["decision"] != recorded["decision"] or \
                redone["synthesis_sha16"] != recorded["synthesis_sha16"]:
            return _fail("synthesis not reproducible from its recorded inputs (A4)")
        if recorded["rounds_used"] > MAX_ADVERSARIAL_ROUNDS:
            return _fail("adversarial rounds exceeded the cap (A5)")
        if recorded["decision"] not in (
                "GATE_FAILS_REDIRECT_WRITER", "QUARANTINE_ESCALATE",
                "REPAIR_THEN_RESUBMIT", "RUN_MINIMAL_EXPERIMENT",
                "ACCEPT_CONTINUE"):
            return _fail(f"untyped decision {recorded['decision']!r}")
        # A6: exact-position resume + automatic redirect.
        if cell["position_fingerprint_before"] != cell["position_fingerprint_after"]:
            return _fail("position fingerprint drifted across the cell (A6)")
        if cell["redirect"].get("manual") or cell.get("manual_triggers") != 0:
            return _fail("manual trigger/handoff detected in the chain (A1)")
        if cell["redirect"].get("routed_to") != recorded["routed_to"]:
            return _fail("redirect does not follow the synthesized decision")
        # A8 re-executed LIVE (never read from the receipt):
        try:
            CapabilityEnvelope(allowed_tools=frozenset({"python3"})).check(tool="curl")
            return _fail("unknown tool was NOT denied — capability not fail-closed")
        except CapabilityDenial:
            pass
        try:
            CapabilityEnvelope(allowed_tools=frozenset({"python3"})).check(
                tool="python3", writes=("/tmp/x",))
            return _fail("read-only role write was NOT denied")
        except CapabilityDenial:
            pass
        from .audit_loop import WorkPackage
        try:
            WorkPackage(role="challenger", bundle_sha="x" * 24, budget_s=10,
                        authority_hashes={"api_token": "leak"},
                        capability={}, allowed_inputs=[]).finalize()
            return _fail("credential-shaped key entered a work package unrefused")
        except CredentialLeakRefused:
            pass
        # PEER_CONSENSUS_OVERRIDES_GATE=0 re-executed LIVE on this bundle:
        g = finalize_synthesis(synthesize(
            cell["bundle"]["bundle_sha"], gate_failed=True,
            challenger_findings=[], reviewer_verdicts=[{"verdict": "ACCEPT"}] * 3,
            rounds_used=1))
        if g["decision"] != "GATE_FAILS_REDIRECT_WRITER":
            return _fail(f"peer consensus overrode a failed gate: {g['decision']}")
        # typed work packages (A7): both roles carry their package sha
        for rnd in cell["challenger_rounds"] + cell["reviewer_rounds"]:
            if not rnd.get("package_sha"):
                return _fail("role round lacks its typed work package sha (A7)")
        # SOL-R1 continuation edges: recompute from the DURABLE fault records.
        trans_path = keep / "faults" / "ctrl-all" / "factory_v2" / "receipts" / \
            "audit_cells" / "transitions.jsonl"
        if not trans_path.exists():
            return _fail(f"continuation transitions missing: {trans_path}")
        transitions = [json.loads(l) for l in
                       trans_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        required_kinds = {"ROLE_CRASH", "BUDGET_EXCEEDED", "REVIEWER_REJECT",
                          "REVIEWER_REDIRECT", "REVIEWER_QUARANTINE",
                          "PROVIDER_EXHAUSTED"}
        kinds = {t.get("kind") for t in transitions}
        if not required_kinds <= kinds:
            return _fail(f"continuation branches missing: {sorted(required_kinds - kinds)}")
        for t in transitions:
            if t.get("schema") != "cell-transition/v1":
                return _fail(f"untyped transition {t.get('kind')!r}")
            if not (isinstance(t.get("backoff_s"), (int, float))
                    and 0 <= t["backoff_s"] <= 30.0):
                return _fail(f"transition {t.get('kind')} backoff unbounded")
            if t.get("manual") is not False:
                return _fail(f"transition {t.get('kind')} required a manual handoff")
        # Sol REJECT@f253f439 F1: continuations must be OBSERVED — each
        # transition carries an observed_continuation whose next_pid is
        # CORRELATED against independently generated durable records
        # (validation-job receipts, engine-budget ledgers, cell identities).
        # Authored fields (recovered/continuation_marker/all_equal) are
        # deliberately NOT read here: falsifying them proves nothing.
        independent_pids: dict[int, str] = {}
        for job_file in keep.glob("**/factory_v2/receipts/validation_jobs/*.json"):
            try:
                jj = json.loads(job_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            for pid_key in ("pid", "owner_pid"):
                pid = jj.get(pid_key)
                if pid and (jj.get("state") or "") not in ("", None):
                    independent_pids[int(pid)] = f"job:{jj.get('key')}"
        for led_file in keep.glob("**/engine_budget_*.json"):
            try:
                ll = json.loads(led_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            for launch in ll.get("launches", []):
                if launch.get("pid"):
                    independent_pids[int(launch["pid"])] = f"ledger:{led_file.name}"
        for launch_file in keep.glob("**/audit_cells/launches.jsonl"):
            try:
                for line in launch_file.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    rec = json.loads(line)
                    if rec.get("pid"):
                        independent_pids[int(rec["pid"])] = (
                            f"launch-ledger:{rec.get('role')}")
            except Exception:
                continue
        for rnd in cell["challenger_rounds"] + cell["reviewer_rounds"]:
            pid = (rnd.get("identity") or {}).get("pid")
            if pid:
                independent_pids[int(pid)] = "cell-round"
        for t in transitions:
            oc = t.get("observed_continuation") or {}
            if oc.get("source") != "production_launch" or not oc.get("next_pid"):
                return _fail(
                    f"transition {t.get('kind')} has no OBSERVED continuation "
                    f"from a real subsequent launch (Sol F1)")
            if int(oc.get("next_pid")) not in independent_pids:
                return _fail(
                    f"transition {t.get('kind')} continuation pid "
                    f"{oc.get('next_pid')} not found in any independent durable "
                    f"record (job/ledger/cell) — continuation not correlated")
            if not oc.get("position_fingerprint"):
                return _fail(f"transition {t.get('kind')} continuation lost the "
                             f"position fingerprint")
        # positions recomputed from the per-branch OBSERVED snapshot files
        snap_dir = keep / "faults" / "positions"
        snaps = sorted(snap_dir.glob("*.json")) if snap_dir.exists() else []
        if len(snaps) != len(required_kinds):
            return _fail(f"expected {len(required_kinds)} observed position "
                         f"snapshots, found {len(snaps)}")
        snap_positions = [json.loads(s.read_text(encoding="utf-8"))
                          .get("position_fingerprint") for s in snaps]
        if len(set(snap_positions)) != 1 or not snap_positions[0]:
            return _fail("observed position snapshots drifted across branches")
        if snap_positions[0] != cell["position_fingerprint_before"] or \
                snap_positions[0] != cell["position_fingerprint_after"]:
            return _fail("fault branches ran against a different product position")
        receipt_ids = set((receipt.get("fault_transitions") or {}).values())
        on_disk_ids = {t.get("transition_id") for t in transitions}
        if not receipt_ids <= on_disk_ids:
            return _fail("receipt cites fault transitions absent from the durable ledger")
        # live re-derivation of the three verdict-driven branches:
        rej = finalize_synthesis(synthesize(
            "f" * 24, gate_failed=False,
            challenger_findings=[{"description": "drift", "is_blocking": True}],
            reviewer_verdicts=[{"verdict": "REJECT"}], rounds_used=1))
        if rej["decision"] != "REPAIR_THEN_RESUBMIT":
            return _fail("REJECT branch does not redirect the writer for repair")
        red = finalize_synthesis(synthesize(
            "f" * 24, gate_failed=False, challenger_findings=[],
            reviewer_verdicts=[{"verdict": "REDIRECT"}], rounds_used=1))
        if red["decision"] != "REPAIR_THEN_RESUBMIT" or red["routed_to"] != "WRITER":
            return _fail("REDIRECT branch does not redirect the writer (Sol F2)")
        qrk = finalize_synthesis(synthesize(
            "f" * 24, gate_failed=False, challenger_findings=[],
            reviewer_verdicts=[{"verdict": "QUARANTINE"}], rounds_used=1))
        if qrk["decision"] != "QUARANTINE_ESCALATE":
            return _fail("QUARANTINE branch does not escalate to the typed owner path")
        # Sol F3: contract bound BEFORE dispatch — every dispatched round in
        # the cell record carries the exercise's frozen contract id, and an
        # UNBOUND package is refused pre-spawn (re-executed LIVE).
        from .audit_loop import ContractBindingFailure, WorkPackage as _WP
        demo_cid = cell.get("contract_id") or ""
        if not demo_cid:
            return _fail("cell record carries no contract_id (Sol F3)")
        for rnd in cell["challenger_rounds"] + cell["reviewer_rounds"]:
            if rnd.get("contract_id") != demo_cid:
                return _fail("a dispatched role round lacks the frozen "
                             "contract binding (Sol F3)")
        try:
            unbound = _WP(
                role="reviewer", bundle_sha="u" * 24, authority_hashes={},
                candidate_sha=cell["candidate_sha"], budget_s=10,
                allowed_inputs=[], capability=CapabilityEnvelope(
                    allowed_tools=frozenset({"python3"}),
                    allowed_read=frozenset(), allowed_write=frozenset()
                ).as_dict()).finalize()
            from .audit_loop import run_reviewer as _rr
            _rr(unbound, cell_path.parent.parent.parent / "ctrl",
                project_dir=None)
            return _fail("an UNBOUND dispatch was accepted — pre-spawn contract "
                         "binding is not fail-closed (Sol F3)")
        except ContractBindingFailure:
            pass
        except Exception:
            pass
        # SOL-R1 canonical contract binding on the LIVE repo under verification:
        from .validation_contract import (load_frozen_contract,
                                          verify_frozen_against_current,
                                          contract_signature)
        from .project_authority import load_authority
        ps_path = repo / "projects" / "factory" / "PROJECT_STATE.json"
        if not ps_path.exists():
            return _fail(f"PROJECT_STATE missing at {ps_path}")
        ps = json.loads(ps_path.read_text(encoding="utf-8"))
        entry = ((ps.get("validation_contracts") or {})
                 .get("AUTOMATED_MULTI_AI_CTO_LOOP_V1") or {})
        cid = entry.get("contract_id", "")
        if not cid:
            return _fail("PROJECT_STATE carries no contract_id for this milestone")
        try:
            frozen = load_frozen_contract(repo / "projects" / "factory", cid)
        except Exception as exc:
            return _fail(f"frozen contract NOT loadable by contract_id {cid!r} "
                         f"through the production loader: {exc} (Sol F1)")
        if frozen.get("contract_id") != cid:
            return _fail("loaded contract id mismatch")
        if frozen["body"].get("contract_signature") != \
                contract_signature(frozen["body"]):
            return _fail("frozen contract signature does not recompute")
        authority = load_authority(repo / "projects" / "factory")
        drift = verify_frozen_against_current(frozen, authority)
        if drift:
            return _fail(f"frozen contract drifted from current authority: {drift}")
        if entry.get("signature") and entry["signature"] != \
                frozen["body"]["contract_signature"]:
            return _fail("PROJECT_STATE signature does not match the frozen contract")
        if len(frozen["body"].get("acceptance") or []) < 14:
            return _fail("frozen acceptance suspiciously thin")
        return _ok(
            f"recomputed live from the isolated-but-real exercise: trigger "
            f"{trig} fired from 2 real failed strategies, bundle "
            f"{cell['bundle']['bundle_sha'][:12]} integrity verified, "
            f"{len(cell['challenger_rounds'])} challenger + "
            f"{len(cell['reviewer_rounds'])} reviewer REAL subprocesses "
            f"(pid+argv observed) with zero attributable writes, single-flight "
            f"exactly-one (generation 1, durable verdict artifact hash-bound), "
            f"synthesis {recorded['decision']} reproducible from recorded "
            f"inputs (sha {recorded['synthesis_sha16']}), rounds<=2, "
            f"redirect automatic, position fingerprints equal "
            f"({cell['position_fingerprint_before']}), capability denials + "
            f"credential refusal + peer-consensus-vs-failed-gate re-executed "
            f"live and fail-closed; SOL-R1 continuation edges recomputed from "
            f"the durable ledger ({sorted(required_kinds)}) each with bounded "
            f"backoff and an OBSERVED continuation whose next pid CORRELATES "
            f"to an independent durable record (validation job / engine "
            f"budget ledger / cell round), positions recomputed from the "
            f"per-branch observed snapshots (authored markers carry no "
            f"weight); every dispatched round binds the frozen contract "
            f"{demo_cid} pre-spawn and an unbound dispatch is refused "
            f"fail-closed (re-executed live); the milestone contract "
            f"{cid} loads by contract_id through the production loader with "
            f"recomputing signature and zero authority drift")
    finally:
        shutil.rmtree(keep, ignore_errors=True)




def verify_matched_3arm_benchmark(repo: Path) -> VerifierResult:
    """CLAUDE_ALONE_VS_GLM_ALONE_VS_FACTORY_BENCHMARK — recomputed from disk,
    never authored (2026-09-17: the milestone failed closed for 36 h on
    VERIFIER_NOT_IMPLEMENTED while its receipt and observations were on disk).

    Runs the producer's own `--verify-only` reconstruction (observation
    hashes vs MANIFEST, matched equalities, aggregates and verdict recomputed
    from the raw observations) as a subprocess under FACTORY_NO_REAL_ENGINE=1,
    then checks the receipt names the three arms, `matched` is all-true and the
    verdict is explicit (a winner or an explicit no-winner), whatever it says.
    """
    import os
    import subprocess
    import sys
    repo = Path(repo)
    receipt_p = repo / "factory_v2" / "receipts" / "MATCHED_BENCHMARK_RECEIPT.json"
    if not receipt_p.exists():
        return _fail("MATCHED_BENCHMARK_RECEIPT.json absent")
    try:
        receipt = json.loads(receipt_p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _fail(f"receipt unreadable: {exc}")
    arms = set((receipt.get("arms") or {}).keys())
    if arms != {"claude_alone", "glm_alone", "factory"}:
        return _fail(f"three matched arms required, receipt has {sorted(arms)}")
    matched = receipt.get("matched") or {}
    if not matched or not all(bool(v) for v in matched.values()):
        return _fail(f"arms not matched: {[k for k, v in matched.items() if not v]}")
    verdict = receipt.get("verdict") or {}
    if "winner" not in verdict:
        return _fail("verdict has no explicit `winner` field")
    out_rel = f"factory_v2/benchmarks/{receipt.get('workload_id') or 'CLAUDE_ALONE_VS_GLM_ALONE_VS_FACTORY_BENCHMARK_V1'}"
    r = subprocess.run(
        [sys.executable, "factory_v2/run_matched_benchmark_3arm.py", "--verify-only",
         "--out", out_rel],
        cwd=str(repo), capture_output=True, text=True, timeout=600,
        env={**os.environ, "FACTORY_NO_REAL_ENGINE": "1"})
    if r.returncode != 0:
        return _fail(f"--verify-only reconstruction rc={r.returncode}: "
                     f"{(r.stderr or r.stdout)[-400:]}")
    return _ok(f"3 arms matched, verdict winner={verdict.get('winner')!r}, "
               f"reconstruction from observations OK (mode={receipt.get('mode')})")


def verify_base_ready(repo: Path) -> VerifierResult:
    """FACTORY_AUTONOMOUS_CTO_BASE_READY — the eleven Operating-Model-015
    counters recomputed by the independent aggregator, never authored.

    The aggregator (factory_v2/base_ready_aggregator.py) recomputes each metric
    from the durable evidence its dependency milestones produced, through the
    existing independence channels (settlement_truth for post-ledger
    settlements, the hash-chained settlement ledger for pre-ledger ones,
    benchmark MANIFEST raw observations, HMAC-sealed + supervised review
    accounts, git-intact durable records). A metric with no independent source
    is UNKNOWN and blocks readiness; `0` always means "0 observed over an
    explicit window", never "risk-free".

    This gate NEVER reads the receipt it grades against: FACTORY_BASE_READY_
    RECEIPT.json is the OUTPUT of the recompute, written here, and an authored
    file at that path is replaced — an authored aggregate cannot influence
    grading because grading never reads it.
    """
    from .base_ready_aggregator import RECEIPT_REL, MILESTONE, EXPECTED, aggregate, write_receipt
    repo = Path(repo)
    receipt_path = repo / RECEIPT_REL
    replaced_authored = False
    if receipt_path.is_file():
        try:
            existing = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = None
        if not (isinstance(existing, dict)
                and existing.get("schema") == "factory-base-ready-receipt/v1"
                and existing.get("produced_by", "").startswith("factory_v2.base_ready_aggregator")):
            replaced_authored = True
    try:
        agg = aggregate(repo)
        write_receipt(repo, agg)
    except Exception as exc:                                   # noqa: BLE001
        return _fail(f"aggregator failed to recompute: {type(exc).__name__}: {exc}")
    metrics = agg.get("metrics", {})
    table = []
    for name in EXPECTED:
        mm = metrics.get(name, {})
        value = mm.get("value")
        table.append(f"{name}={value if value is not None else 'UNKNOWN'}"
                     f"[{mm.get('status', 'UNKNOWN')}]")
    blocking = agg.get("blocking", [])
    head = f"{MILESTONE}: {agg.get('decision')} — " + " ".join(table)
    if replaced_authored:
        head += ("; a pre-existing AUTHORED receipt was found and REPLACED by this "
                 "recompute (grading never read it)")
    if blocking:
        reasons = "; ".join(
            f"{n}: {metrics.get(n, {}).get('reason')
                    or ('value=' + str(metrics.get(n, {}).get('value')) + ' vs '
                        + metrics.get(n, {}).get('expected', ''))}"
            for n in blocking)
        return _fail(f"{head} — BLOCKED by {reasons}")
    return _ok(head)


FACTORY_MILESTONES = {
    "EXTERNAL_SUPERVISOR_NO_MANUAL_RELAUNCH":
        lambda repo, o, obs, **kw: verify_external_supervisor(repo),
    "RUN_RECORDS_MINIMUM_WIRED": lambda repo, o, obs, **kw: verify_runrecords_wired(repo),
    "AUTOMATED_MULTI_AI_CTO_LOOP_V1": lambda repo, o, obs, **kw: verify_audit_loop_cell(repo),
    "RESOURCE_TELEMETRY_TRUE_COST":
        lambda repo, o, obs, **kw: verify_resource_telemetry_true_cost(repo),
    "CLAUDE_ALONE_VS_GLM_ALONE_VS_FACTORY_BENCHMARK":
        lambda repo, o, obs, **kw: verify_matched_3arm_benchmark(repo),
    "FACTORY_AUTONOMOUS_CTO_BASE_READY":
        lambda repo, o, obs, **kw: verify_base_ready(repo),
}
