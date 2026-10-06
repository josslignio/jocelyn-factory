"""Roll-ups that carry their own uncertainty.

Every total here ships with the count of rows that could NOT contribute to it.
A sum of 12 measured rows out of 40 is not "the token cost"; it is the token
cost of 30% of the work, and a report that prints the first number without the
second is worse than printing nothing, because it invites a budget decision.

The rules, each of which exists because its opposite is the easy mistake:

  * **Denominator is `independently_validated`.** Never `mission_pass`. A
    builder's own PASS is a claim; only the independent settlement path projects
    validation. Dividing by claims makes every cost-per-result look smaller
    than it is.
  * **Milestones, not attempts.** A milestone needing two missions is ONE
    accepted milestone. Counting validated attempts inflated every ratio.
  * **PARTIAL_FLOOR rows are bounds, not totals.** They are summed into
    `*_lower_bound` and excluded from the exact totals, and both are published.
  * **Nothing nested is counted twice.** Reviews, repairs and tests each carry a
    lineage and a parent; they are added to the cost OF the accepted change and
    never into the parent launch's own usage.
  * **Zero denominator is undefined, not zero.** With no accepted milestone,
    cost-per-accepted-milestone is UNDEFINED and the burned spend is shown
    beside it — which is the number that actually matters in that situation.
"""
from __future__ import annotations

from .record import UNKNOWN, is_measured, value_of

#: Which lineages are work done ABOUT a change rather than the change itself.
NESTED_LINEAGES = ("REVIEW", "REPAIR", "TEST", "RECOVERY")

_TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens",
                 "cache_write_tokens", "reasoning_tokens")


def _usage(row: dict) -> dict:
    u = row.get("usage")
    return u if isinstance(u, dict) else {}



class _Bucket:
    """Totals plus the coverage that produced them. Never one without the other."""

    def __init__(self):
        self.launches = 0
        self.provider_requests = 0
        self.provider_requests_unknown = 0
        # Model turns are a DIFFERENT unit from provider requests and are kept
        # separately: a turn may hide a retry, a continuation or a compaction.
        self.model_turns = 0
        self.model_turns_unknown = 0
        self.exact = {f: 0 for f in _TOKEN_FIELDS}
        self.lower_bound = {f: 0 for f in _TOKEN_FIELDS}
        # Per-FIELD coverage. Without it a field no provider reports sums to 0
        # and is published as "zero reasoning tokens" when the truth is "nobody
        # reported reasoning tokens" — an UNKNOWN wearing a measurement's face.
        self.exact_n = {f: 0 for f in _TOKEN_FIELDS}
        self.lower_bound_n = {f: 0 for f in _TOKEN_FIELDS}
        self.measured_rows = 0
        self.floor_rows = 0
        self.unmeasured_rows = 0
        self.unknown_reasons: dict = {}
        self.wall_time_s = 0.0
        self.wall_time_unknown = 0
        self.api_price_estimate_usd = 0.0
        self.api_price_rows = 0
        self.exit_classes: dict = {}
        self.rate_limit_waits = 0
        self.retries = 0

    def add(self, row: dict) -> None:
        self.launches += 1
        u = _usage(row)
        completeness = u.get("completeness", "ABSENT")
        target = (self.exact if completeness == "COMPLETE"
                  else self.lower_bound if completeness == "PARTIAL_FLOOR"
                  else None)
        if target is None:
            self.unmeasured_rows += 1
            for f in _TOKEN_FIELDS:
                reason = _reason_of(u.get(f))
                if reason:
                    self.unknown_reasons[reason] = \
                        self.unknown_reasons.get(reason, 0) + 1
                    break
        else:
            counts = (self.exact_n if completeness == "COMPLETE"
                      else self.lower_bound_n)
            if completeness == "COMPLETE":
                self.measured_rows += 1
            else:
                self.floor_rows += 1
            for f in _TOKEN_FIELDS:
                v = value_of(u.get(f))
                if v is not None:
                    target[f] += v
                    counts[f] += 1

        pr = value_of(u.get("provider_requests"))
        if pr is None:
            self.provider_requests_unknown += 1
        else:
            self.provider_requests += pr

        mt = value_of(u.get("model_turns"))
        if mt is None:
            self.model_turns_unknown += 1
        else:
            self.model_turns += mt

        wall = value_of(row.get("wall_time_s"))
        if wall is None:
            self.wall_time_unknown += 1
        else:
            self.wall_time_s += wall

        money = row.get("money") if isinstance(row.get("money"), dict) else {}
        est = value_of(money.get("api_price_estimate_usd"))
        if est is not None:
            self.api_price_estimate_usd += est
            self.api_price_rows += 1

        ec = row.get("exit_class") or ("rc=%s" % row.get("rc"))
        self.exit_classes[str(ec)] = self.exit_classes.get(str(ec), 0) + 1
        self.rate_limit_waits += int(row.get("rate_limit_waits") or 0)
        self.retries += int(row.get("retries") or 0)

    def _tokens(self, totals: dict, counts: dict, rows: int):
        """Totals with per-field unknowns preserved as UNKNOWN, not as 0."""
        if not rows:
            return UNKNOWN
        return {f: (totals[f] if counts[f] else UNKNOWN) for f in _TOKEN_FIELDS}

    def as_dict(self) -> dict:
        covered = self.measured_rows + self.floor_rows
        return {
            "launches": self.launches,
            "provider_requests": (self.provider_requests
                                  if self.provider_requests_unknown < self.launches
                                  else UNKNOWN),
            "provider_requests_unknown_launches": self.provider_requests_unknown,
            "provider_requests_note": (
                "actual calls to the provider. UNKNOWN unless an event counts "
                "them: a turn count is not a request count"),
            "model_turns": (self.model_turns
                            if self.model_turns_unknown < self.launches
                            else UNKNOWN),
            "model_turns_unknown_launches": self.model_turns_unknown,
            "model_turns_note": (
                "assistant turns the provider reported. A LOWER BOUND on "
                "provider requests — retries, continuations and compaction "
                "passes can hide inside one turn"),
            "tokens_exact": self._tokens(self.exact, self.exact_n,
                                         self.measured_rows),
            "tokens_lower_bound_only": self._tokens(
                self.lower_bound, self.lower_bound_n, self.floor_rows),
            "tokens_field_coverage": {
                f: {"rows_reporting": self.exact_n[f] + self.lower_bound_n[f],
                    "of_rows": self.launches} for f in _TOKEN_FIELDS},
            "rows_measured_complete": self.measured_rows,
            "rows_measured_lower_bound": self.floor_rows,
            "rows_unmeasured": self.unmeasured_rows,
            "unmeasured_reasons": dict(self.unknown_reasons),
            # Two different coverages, because "we have a number for it" and
            # "the number is exact" are different claims. A window where every
            # launch was killed before its totals is 100% covered and 0% exact,
            # and only the second tells you the totals are floors.
            "token_coverage_pct": (round(100.0 * covered / self.launches, 1)
                                   if self.launches else UNKNOWN),
            "token_coverage_exact_pct": (
                round(100.0 * self.measured_rows / self.launches, 1)
                if self.launches else UNKNOWN),
            "wall_time_s": (round(self.wall_time_s, 2)
                            if self.wall_time_unknown < self.launches else UNKNOWN),
            "wall_time_unknown_launches": self.wall_time_unknown,
            # Named an ESTIMATE everywhere it appears. Under flat subscriptions
            # nobody is charged it, and it is never added to anything called cost.
            "api_price_estimate_usd": (round(self.api_price_estimate_usd, 6)
                                       if self.api_price_rows else UNKNOWN),
            "api_price_estimate_rows": self.api_price_rows,
            "charged_money_usd": UNKNOWN,
            "charged_money_note": ("no launched CLI reports money actually "
                                   "charged; never derived from tokens"),
            "exit_classes": dict(self.exit_classes),
            "rate_limit_waits": self.rate_limit_waits,
            "retries": self.retries,
        }


def _reason_of(slot):
    if isinstance(slot, dict) and slot.get("value") == UNKNOWN:
        return slot.get("reason") or "UNSPECIFIED"
    return ""


def _empty_model_bucket() -> dict:
    """A per-model bucket with the SAME keys every other bucket has.

    The provider splits tokens by model and nothing else: wall time, exits and
    price are properties of the launch, not of one model inside it. Those keys
    are present and UNKNOWN rather than absent, so every consumer can read a
    bucket without knowing which kind it is — and so "not split by model" never
    renders as a zero.
    """
    return {
        "launches": 0,
        "tokens_exact": {f: 0 for f in _TOKEN_FIELDS},
        "tokens_lower_bound_only": UNKNOWN,
        "attribution": "provider per-model split",
        "wall_time_s": UNKNOWN,
        "wall_time_unknown_launches": UNKNOWN,
        "token_coverage_pct": UNKNOWN,
        "token_coverage_exact_pct": UNKNOWN,
        "rows_measured_complete": UNKNOWN,
        "rows_measured_lower_bound": UNKNOWN,
        "rows_unmeasured": UNKNOWN,
        "provider_requests": UNKNOWN,
        "model_turns": UNKNOWN,
        "api_price_estimate_usd": UNKNOWN,
        "charged_money_usd": UNKNOWN,
        "exit_classes": {},
        "per_model_note": ("the provider splits TOKENS by model; wall time, "
                           "exit class and price belong to the launch as a "
                           "whole and are not divisible between its models"),
    }


def _by_model(rows) -> dict:
    """Per-model token attribution, using the provider's own split when given.

    Falls back to whole-launch attribution only for rows with no split, and says
    so per bucket, so a mixed-model launch is never silently credited to one.
    """
    out: dict = {}
    split_rows, plain_rows = [], []
    for row in rows:
        per_model = (_usage(row) or {}).get("per_model")
        (split_rows if isinstance(per_model, dict) and per_model
         else plain_rows).append(row)

    grouped = _group(plain_rows, lambda r: (r.get("model_observed")
                                            or r.get("model_requested")))
    for name, bucket in grouped.items():
        bucket["attribution"] = "whole launch (provider reported no split)"
        out[name] = bucket
    exact_seen: set = set()

    for row in split_rows:
        # A PARTIAL_FLOOR row's per-model numbers are BOUNDS, exactly as its
        # totals are. Adding them to `tokens_exact` because they arrived through
        # a different field published a lower bound as a measurement in the one
        # view the verifier's floor check does not inspect.
        floor = _usage(row).get("completeness") == "PARTIAL_FLOOR"
        target_key = "tokens_lower_bound_only" if floor else "tokens_exact"
        for name, entry in (_usage(row).get("per_model") or {}).items():
            bucket = out.setdefault(str(name), _empty_model_bucket())
            if bucket.get("attribution") != "provider per-model split":
                # A model seen both ways: keep the numbers, flag the mixture.
                bucket["attribution"] = "mixed: per-model split and whole launch"
            bucket["launches"] += 1
            if not floor:
                exact_seen.add(str(name))
            totals = bucket.get(target_key)
            if not isinstance(totals, dict):
                totals = {f: 0 for f in _TOKEN_FIELDS}
                bucket[target_key] = totals
            for f in _TOKEN_FIELDS:
                v = entry.get(f)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    prior = totals.get(f)
                    totals[f] = (prior if isinstance(prior, (int, float))
                                 and not isinstance(prior, bool) else 0) + v
            if floor:
                bucket["rows_measured_lower_bound"] = (
                    (bucket.get("rows_measured_lower_bound") or 0) + 1
                    if isinstance(bucket.get("rows_measured_lower_bound"), int)
                    else 1)
    # A bucket fed only by floors has an untouched all-zero `tokens_exact`,
    # which renders as "0" — the module's own rule (see `_Bucket._tokens`) is
    # that a field nothing reported is UNKNOWN, not zero.
    for name, bucket in out.items():
        if (name not in exact_seen
                and bucket.get("attribution") == "provider per-model split"):
            bucket["tokens_exact"] = UNKNOWN
    return dict(sorted(out.items()))


def _group(rows, key_fn) -> dict:
    out: dict = {}
    for row in rows:
        key = key_fn(row) or "UNKNOWN"
        out.setdefault(str(key), _Bucket()).add(row)
    return {k: v.as_dict() for k, v in sorted(out.items())}


def summarise(observations: list, accounts: list = None) -> dict:
    """The report body. `accounts` are folded AttemptAccount rows, when available.

    Observations measure what the ENGINES did. Accounts carry what the factory
    decided about it — mission_pass, and the only fact that matters for a cost
    ratio, `independently_validated`. They are kept as two inputs because they
    answer two different questions and merging them is how a claim becomes a
    validation.
    """
    observations = [o for o in (observations or []) if isinstance(o, dict)]
    accounts = [a for a in (accounts or []) if isinstance(a, dict)]

    production = [o for o in observations if o.get("lineage") != "CTO_OVERHEAD"]
    overhead = [o for o in observations if o.get("lineage") == "CTO_OVERHEAD"]

    overall = _Bucket()
    for o in production:
        overall.add(o)

    accepted = [a for a in accounts if a.get("independently_validated")]
    accepted_milestones = sorted({(a.get("project") or "UNKNOWN", a.get("milestone"))
                                  for a in accepted if a.get("milestone")})
    claimed_pass = [a for a in accounts if a.get("mission_pass")]

    body = overall.as_dict()
    n_ms = len(accepted_milestones)

    return {
        "schema": "telemetry/summary/v1",
        "totals": body,
        "accepted": {
            # THE denominator. `mission_pass` is reported beside it purely so the
            # gap between "the builder said it worked" and "an independent
            # settlement proved it" is visible rather than assumed away.
            "independently_accepted_milestones": n_ms,
            "independently_validated_attempts": len(accepted),
            "attempts_claiming_pass": len(claimed_pass),
            "claims_not_independently_validated": max(
                0, len(claimed_pass) - len(accepted)),
            "milestones": [{"project": p, "milestone": m}
                           for p, m in accepted_milestones],
            "denominator": "independently_validated",
        },
        "per_accepted_milestone": _per_accepted(body, n_ms, overall),
        "non_progress": _non_progress(accounts, overall),
        "by_engine": _group(production, lambda r: r.get("engine")),
        # Attributing a whole launch to its dominant model put every haiku
        # side-call's spend in the sonnet bucket. When the provider reports a
        # per-model split, that split is the answer.
        "by_model": _by_model(production),
        "by_role": _group(production, lambda r: r.get("role")),
        "by_lineage": _group(production, lambda r: r.get("lineage")),
        "by_project": _group(production, lambda r: r.get("project")),
        "by_milestone": _group(production, lambda r: r.get("milestone")),
        # LIVE rows were measured as the launch happened; BACKFILL rows were
        # reconstructed afterwards from whatever log survived. Both are real,
        # they are not equally complete, and a total that merges them without
        # saying so reads as one uniform measurement.
        "by_provenance": _group(production, lambda r: r.get("provenance")),
        # The current campaign is the question being asked; an imported
        # historical corpus is a different one. They are never added together
        # without both being visible separately.
        "by_window": _group(observations, lambda r: r.get("window")),
        "cto_overhead": {
            "note": ("lead/research/reviewer CLI sessions run by the operator. "
                     "Real consumption, counted SEPARATELY from production "
                     "mission attempts and never mixed into the ratios above."),
            **_Bucket_of(overhead),
        },
        "quota_windows": _quota(observations),
        "phases_s": _phases(production),
        "unobservable": _unobservable(observations),
    }


def _unobservable(observations: list) -> dict:
    """What this report genuinely could not see — decided by the data, not by a
    standing assumption.

    This block used to assert flatly that the operator's Codex chat "is not
    emitted to any file this supervisor can read". That was an assumption, and
    once the cwd-scoped scan was wired the same report both ingested that usage
    AND printed the claim that it could not. A report that contradicts itself in
    two adjacent sections is worse than one that admits a gap.
    """
    operator_rows = [o for o in observations
                     if o.get("engine") == "codex"
                     and o.get("role") in ("cto_pilot", "cto_overhead")]
    if operator_rows:
        return {
            "root_operator_chat_quota": "OBSERVED_FOR_THIS_WORKSPACE",
            "reason": "",
            "note": (f"{len(operator_rows)} operator Codex session(s) whose "
                     f"recorded cwd is this workspace were measured and counted "
                     f"as CTO_OVERHEAD. Operator sessions with any other cwd are "
                     f"out of scope BY CHOICE, not absent — see "
                     f"`operator_codex_scan` for how many were skipped."),
        }
    return {
        "root_operator_chat_quota": UNKNOWN,
        "reason": "UNOBSERVABLE_FROM_SUPERVISOR",
        "note": ("no operator Codex session bound to this workspace was "
                 "ingested in this run. The Codex CLI DOES record usage per "
                 "session; run with --codex-operator-sessions to bind the ones "
                 "whose cwd is this workspace. Reported UNKNOWN, never 0."),
    }


def _Bucket_of(rows) -> dict:
    b = _Bucket()
    for r in rows:
        b.add(r)
    return b.as_dict()


def _per_accepted(body: dict, n_ms: int, overall: _Bucket) -> dict:
    """Cost per accepted milestone — or a stated reason there is no such number."""
    if not n_ms:
        return {
            "status": "UNDEFINED_ZERO_DENOMINATOR",
            "note": ("no milestone has been independently validated in this "
                     "window, so cost-per-accepted-milestone is undefined. The "
                     "spend below was still burned."),
            "burned_wall_time_s": body["wall_time_s"],
            "burned_launches": body["launches"],
            "burned_tokens_exact": body["tokens_exact"],
            "burned_api_price_estimate_usd": body["api_price_estimate_usd"],
        }
    exact = body["tokens_exact"]
    # Sum only the fields something actually reported. An UNKNOWN field is left
    # out of the sum AND named in `tokens_summed_over`, so the ratio says which
    # units it is a ratio of rather than implying it covers all of them.
    summed = ([f for f, v in exact.items() if isinstance(v, (int, float))]
              if isinstance(exact, dict) else [])
    total = sum(exact[f] for f in summed) if summed else None
    return {
        "status": "DEFINED",
        "accepted_milestones": n_ms,
        "launches_per_accepted_milestone": round(overall.launches / n_ms, 2),
        "tokens_per_accepted_milestone": (round(total / n_ms, 1)
                                          if total is not None else UNKNOWN),
        "tokens_summed_over": summed,
        "wall_time_s_per_accepted_milestone": (
            round(overall.wall_time_s / n_ms, 1)
            if overall.wall_time_unknown < overall.launches else UNKNOWN),
        "api_price_estimate_usd_per_accepted_milestone": (
            round(overall.api_price_estimate_usd / n_ms, 6)
            if overall.api_price_rows else UNKNOWN),
        "includes": ("every launch in the window — builds, repairs, reviews, "
                     "tests, retries and recovery — because all of them were "
                     "paid for before the milestone was accepted"),
        "token_coverage_pct": body["token_coverage_pct"],
        "caveat": ("ratios inherit the coverage above: with coverage below 100% "
                   "the true cost is HIGHER than published, never lower"),
    }


def _non_progress(accounts: list, overall: _Bucket) -> dict:
    """Consumption that produced no independently validated delta."""
    if not accounts:
        return {"status": UNKNOWN,
                "note": "no attempt accounts in scope for this report"}
    n = len(accounts)
    validated = sum(1 for a in accounts if a.get("independently_validated"))
    streak = 0
    for a in reversed(accounts):
        if a.get("independently_validated"):
            break
        streak += 1
    return {
        "attempts": n,
        "attempts_without_validated_delta": n - validated,
        "consumption_without_material_change_pct": round(
            100.0 * (n - validated) / n, 1),
        "consecutive_trailing_attempts_without_delta": streak,
        "note": ("heartbeats and activity commits are not attempts and cannot "
                 "reset this streak; see resource_accounting.cost_no_progress"),
    }


def _instant(value):
    """An ISO timestamp as a comparable number; -inf when it cannot be parsed."""
    import datetime as _dt
    if not value or value == UNKNOWN:
        return float("-inf")
    try:
        t = _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return float("-inf")
    if t.tzinfo is None:
        t = t.replace(tzinfo=_dt.timezone.utc)
    return t.timestamp()


def _quota(observations: list, *, now_epoch=None) -> dict:
    """Latest provider quota snapshot per window scope, chosen BY OBSERVED TIME.

    Previously "latest" meant "last row encountered", which is file traversal
    order — so which utilisation got published depended on how the glob happened
    to sort, and a stale reading could overwrite a fresher one. Ordering is now
    on the provider's own timestamp, undatable readings never displace a dated
    one, and a window whose reset time has already passed is marked EXPIRED
    rather than presented as the current state of the account.
    """
    import time as _time
    now = now_epoch if now_epoch is not None else _time.time()
    latest: dict = {}
    undated: dict = {}
    for o in observations:
        q = o.get("quota")
        if not isinstance(q, dict):
            continue
        scope = q.get("scope")
        if not scope or scope == UNKNOWN:
            continue
        seen = q.get("observed_at") or ""
        resets = value_of(q.get("resets_at"), None)
        entry = {
            "scope": scope,
            "status": q.get("status", UNKNOWN),
            "utilisation": value_of(q.get("utilisation"), UNKNOWN),
            "resets_at_epoch_s": resets if resets is not None else UNKNOWN,
            "observed_at": seen or UNKNOWN,
            "observed_at_source": q.get("observed_at_source") or UNKNOWN,
            "imported_at": q.get("imported_at", ""),
            "source": q.get("source", ""),
            "engine": o.get("engine", ""),
            "provenance": o.get("provenance", "LIVE"),
        }
        # A window that has already reset says nothing about the quota now.
        if isinstance(resets, (int, float)) and resets <= now:
            entry["freshness"] = "EXPIRED"
            entry["freshness_note"] = (
                "this window's reset time has passed, so the utilisation below "
                "describes a window that has since rolled over — it is history, "
                "not the current balance")
        else:
            entry["freshness"] = "CURRENT_WINDOW"
        bucket = latest if seen else undated
        # PARSE before comparing. Claude stamps `…39.500Z`, the mtime proxy
        # `…39+00:00`; comparing those as strings picks the wrong one inside the
        # same second, which is exactly when it matters.
        if scope not in bucket or _instant(seen) > _instant(
                bucket[scope].get("observed_at")):
            bucket[scope] = entry
    # A dated reading always beats an undated one for the same scope.
    for scope, entry in undated.items():
        latest.setdefault(scope, entry)
    return {
        "windows": latest,
        "note": ("provider-reported windows only, ordered by the provider's own "
                 "observation time — never by file traversal order. Engines that "
                 "report no window (opencode) have none here, and an absent "
                 "window means UNKNOWN remaining, never unlimited."),
        "never_derived_from_tokens": True,
    }


def _phases(observations: list) -> dict:
    """Wall time by named phase, summed across launches. Unmeasured stays out."""
    out: dict = {}
    for o in observations:
        phases = o.get("phase_times_s")
        if not isinstance(phases, dict):
            continue
        for name, value in phases.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                cur = out.setdefault(str(name), {"total_s": 0.0, "n": 0})
                cur["total_s"] += value
                cur["n"] += 1
    for cur in out.values():
        cur["total_s"] = round(cur["total_s"], 2)
    return out


def in_flight(observations: list, accounts: list, *, now_iso: str = "") -> list:
    """Everything still running: OPEN product attempts AND live CTO/tool jobs.

    Reporting only OPEN AttemptAccount rows made the section read "nothing is
    running" while a real reviewer process was mid-flight — true about product
    missions, and misleading as a status line. A launch whose supervisor record
    has no terminal outcome is running whether or not it belongs to a product
    milestone, so both sources feed this list and each row says which it is.

    Age is computed from the row's own `started_at`, so a supervisor restart
    does not reset it. An unparseable start instant yields UNKNOWN age rather
    than a zero that would make a stuck attempt look fresh.
    """
    import datetime as _dt
    if now_iso:
        try:
            now = _dt.datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
        except ValueError:
            now = _dt.datetime.now(_dt.timezone.utc)
    else:
        now = _dt.datetime.now(_dt.timezone.utc)
    def _age(started):
        try:
            t = _dt.datetime.fromisoformat(str(started).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return UNKNOWN
        if t.tzinfo is None:
            t = t.replace(tzinfo=_dt.timezone.utc)
        return round((now - t).total_seconds(), 1)

    out = []
    for a in accounts or []:
        if a.get("phase") != "OPEN":
            continue
        started = a.get("started_at") or ""
        out.append({
            "kind": "PRODUCT_ATTEMPT",
            "attempt_key": a.get("attempt_key", ""),
            "project": a.get("project", ""),
            "milestone": a.get("milestone", ""),
            "mission": a.get("mission", ""),
            "engine": a.get("engine", UNKNOWN),
            "model": a.get("model", UNKNOWN),
            "role": a.get("role", UNKNOWN),
            "started_at": started,
            "age_s": _age(started),
            "pid": a.get("launched_pid"),
        })
    for o in observations or []:
        if o.get("exit_class") != "RUNNING":
            continue
        started = o.get("started_at") or ""
        out.append({
            "kind": "TOOL_OR_CTO_JOB",
            "attempt_key": o.get("observation_key", ""),
            "project": o.get("project", ""),
            "milestone": o.get("milestone", ""),
            "mission": o.get("mission", ""),
            "engine": o.get("engine", UNKNOWN),
            "model": o.get("model_requested") or o.get("model_observed") or UNKNOWN,
            "role": o.get("role", UNKNOWN),
            "lineage": o.get("lineage", ""),
            "started_at": started,
            "age_s": _age(started),
            "pid": o.get("pid"),
        })
    # Oldest first — a stuck attempt is the one worth seeing. Rows whose age
    # could not be computed sort last rather than crashing the sort or being
    # silently treated as age 0.
    return sorted(out, key=lambda r: (r["age_s"] == UNKNOWN,
                                      -r["age_s"] if isinstance(r["age_s"], (int, float))
                                      else 0))


def restarts_and_recovery(accounts: list) -> dict:
    """Restart count and recovery latency, only where the ledger shows them.

    A reconciled row is a process the supervisor lost: the ledger records it and
    the reason. Recovery LATENCY — how long between the loss and the next launch
    — is only real when both instants are recorded, so the count and the latency
    are reported separately and the latency may legitimately be UNKNOWN while
    the count is not.
    """
    reconciled = [a for a in accounts or [] if a.get("reconciled_reason")]
    kills = [a for a in accounts or [] if a.get("exit_class") == "KILL"]
    timeouts = [a for a in accounts or [] if a.get("exit_class") == "TIMEOUT"]
    quota = [a for a in accounts or [] if a.get("exit_class") == "QUOTA"]
    provider = [a for a in accounts or []
                if a.get("exit_class") == "PROVIDER_FAILURE"]
    latencies = _recovery_latencies(accounts or [])
    return {
        "lost_processes_reconciled": len(reconciled),
        "reconcile_reasons": _count(a.get("reconciled_reason") for a in reconciled),
        "killed_attempts": len(kills),
        "timed_out_attempts": len(timeouts),
        "quota_terminated_attempts": len(quota),
        "provider_failure_attempts": len(provider),
        "recovery_latency_s": ({"n": len(latencies),
                                "median": _median(latencies),
                                "max": max(latencies)} if latencies else UNKNOWN),
        "recovery_latency_note": (
            "seconds from a reconciled loss to the next launch on the same "
            "milestone; UNKNOWN when either instant was not recorded"),
    }


def _recovery_latencies(accounts: list) -> list:
    import datetime as _dt

    def _t(value):
        try:
            d = _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
        return d if d.tzinfo else d.replace(tzinfo=_dt.timezone.utc)

    ordered = sorted(accounts, key=lambda a: str(a.get("started_at") or ""))
    out = []
    pending: dict = {}
    for a in ordered:
        ms = a.get("milestone") or ""
        if a.get("reconciled_reason"):
            pending[ms] = _t(a.get("ended_at"))
            continue
        lost_at = pending.get(ms)
        started = _t(a.get("started_at"))
        # An attempt whose start instant was not recorded cannot close a
        # recovery interval — and must not CONSUME the pending marker either,
        # or the next attempt that could measure the latency would find nothing
        # to measure against and the interval would vanish silently.
        if lost_at and started and started >= lost_at:
            out.append(round((started - lost_at).total_seconds(), 1))
            pending.pop(ms, None)
    return out


def _count(values) -> dict:
    out: dict = {}
    for v in values:
        if v:
            out[str(v)] = out.get(str(v), 0) + 1
    return out


def _median(values: list):
    if not values:
        return UNKNOWN
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else round((s[mid - 1] + s[mid]) / 2, 1)
