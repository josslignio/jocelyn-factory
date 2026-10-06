#!/usr/bin/env python3
"""Validate LONG_RUN_2_RECEIPT.json — real long-run proof with failures and restart.
Schema locked at AUTONOMOUS_CTO_BASELINE_V1.
"""
import json, sys
from pathlib import Path


def fail(msg):
    print(f"FAIL: {msg}", file=sys.stderr); return 1


def main():
    if len(sys.argv) < 2:
        return fail("usage: verify_long_run.py <path>")
    path = Path(sys.argv[1])
    if not path.exists():
        return fail(f"not found: {path}")
    try:
        r = json.loads(path.read_text())
    except Exception as e:
        return fail(f"parse: {e}")

    for field in ("milestones_settled", "incidents", "restart_events",
                  "routine_human_interventions", "product_e2e_proven"):
        if field not in r:
            return fail(f"missing field: {field}")

    settled = r["milestones_settled"]
    if not isinstance(settled, list) or len(settled) < 3:
        return fail(f"milestones_settled has {len(settled) if isinstance(settled,list) else '?'} entries — need ≥3")
    # L5_factory_v2_rest_3-001: the threshold is about DISTINCT milestones — the
    # same settlement repeated three times is one milestone, not three.
    distinct = {str(m) for m in settled}
    if len(distinct) < 3:
        return fail(f"milestones_settled lists {len(settled)} entries but only "
                    f"{len(distinct)} DISTINCT milestone(s) {sorted(distinct)} — "
                    f"the same settlement proof is being counted repeatedly")

    # S017-03: a list of milestone NAMES is a claim, not a proof.  Sol passed this
    # verifier three invented names and it returned 0.  Each claimed settlement is now
    # re-verified against durable evidence in the named project, using the same
    # read-only settlement verifier the rest of the system uses.  Without a project to
    # verify against, the receipt cannot be checked at all and we fail closed rather
    # than accept its word.
    project_dir = r.get("project_dir", "")
    if not project_dir:
        return fail("receipt names no project_dir — claimed settlements cannot be "
                    "re-verified (VERIFIER_CANNOT_OBSERVE_RETROACTIVELY)")
    pd = Path(project_dir)
    if not pd.is_dir():
        return fail(f"project_dir {project_dir!r} does not exist — nothing to verify")
    try:
        from factory_v2.settlement_truth import verify_settlement
    except Exception as exc:
        return fail(f"settlement verifier unavailable: {exc}")
    for mid in settled:
        v = verify_settlement(pd, str(mid))
        if not v.proven:
            return fail(f"claimed settlement {mid!r} is not proven in {pd.name}: "
                        f"{'; '.join(v.reasons[:2])}")

    # Restart events must reference a real observed process, not an authored boolean.
    for restart in (r.get("restart_events") or []):
        if not restart.get("observed_pid_before") or not restart.get("observed_pid_after"):
            return fail(f"restart {restart.get('id','?')}: no observed PIDs — an authored "
                        "restart flag is not an observation")
        if restart["observed_pid_before"] == restart["observed_pid_after"]:
            return fail(f"restart {restart.get('id','?')}: PID unchanged, no restart occurred")

    incidents = r["incidents"]
    if not isinstance(incidents, list) or len(incidents) < 1:
        return fail("incidents list empty — failure+replan must be exercised")
    failure_replans = [i for i in incidents if i.get("type") in ("failure_replan", "failure_reroute")]
    if not failure_replans:
        return fail("no failure_replan or failure_reroute incident — anti-loop not proven")

    restarts = r["restart_events"]
    if not isinstance(restarts, list) or len(restarts) < 1:
        return fail("restart_events empty — crash+restart not proven")
    # CTO DIRECTIVE 033 §4 — the three headline autonomy claims are RECOMPUTED from
    # supervisor-owned evidence. Reading them out of the receipt made this a shape
    # check: arbitrary non-equal PIDs plus three authored booleans satisfied it, so
    # it proved a settlement sequence and not an unattended run.
    from factory_v2.long_run_truth import (
        Unproven, recompute_restart_recovery, recompute_human_interventions,
        recompute_product_e2e)

    control_root = Path(__file__).resolve().parent.parent
    run_id = str(r.get("run_id") or "")
    if not run_id:
        return fail("receipt carries no run_id — an unbounded run cannot be attributed "
                    "to a supervisor-owned window, so autonomy cannot be recomputed")

    try:
        for restart in restarts:
            ok, detail = recompute_restart_recovery(control_root, run_id, restart)
            if not ok:
                return fail(detail)

        window = (str(r.get("window_start") or ""), str(r.get("window_end") or ""))
        if not (window[0] and window[1]):
            return fail("receipt carries no supervisor-owned run window — human "
                        "interventions cannot be counted over an undefined interval")
        n_human, detail = recompute_human_interventions(control_root, run_id, window)
        if n_human != 0:
            return fail(f"recomputed routine_human_interventions={n_human} "
                        f"(receipt claims {r['routine_human_interventions']}) — {detail}")

        e2e = r.get("product_e2e_evidence") or {}
        ok, detail = recompute_product_e2e(
            control_root,
            str(e2e.get("milestone_id") or ""),
            Path(str(e2e.get("product_repo") or "")),
            str(e2e.get("control_sha") or ""),
            str(e2e.get("product_sha") or ""))
        if not ok:
            return fail(f"product E2E did not reproduce: {detail}")
    except Unproven as u:
        return fail(f"AUTONOMY_UNPROVEN: {u}")

    print(f"PASS: {len(settled)} milestones, {len(incidents)} incidents, {len(restarts)} restarts, HUMAN_GO=0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
