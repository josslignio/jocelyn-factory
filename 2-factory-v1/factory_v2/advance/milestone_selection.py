"""Étape extraite de `_advance_locked` (factory_v2/runtime.py) : la sélection du jalon du cycle — mise…"""
from __future__ import annotations

from .. import runtime as rt
from ..mission_evidence import HeartbeatObservationUnknown


def run(ctx):
    """Bloc « sélection du jalon » de `_advance_locked`."""
    from ..safety_quarantine import get_quarantine_record, is_in_quarantine

    ctx._events_before_dep_held = len(ctx.state.get("events", []))
    rt._emit_milestones_held_by_dependency(ctx.state, ctx.authority)
    if len(ctx.state.get("events", [])) != ctx._events_before_dep_held:
        rt.save_project_state(ctx.project_dir, ctx.state)

    ctx._quarantined = is_in_quarantine(ctx.project_dir)
    ctx._scoped_mid = None
    if ctx._quarantined:
        ctx.qrec = get_quarantine_record(ctx.project_dir) or {}
        ctx.qmid = ctx.qrec.get("milestone_id", "") or None
        # C20: a safety quarantine freezes the MILESTONE it repairs, not the whole project.
        if ctx.qmid:
            ctx._candidate_mid, ctx._held_ids = rt._scope_independent_of_quarantine(
                ctx.authority, ctx.state, ctx.qmid)
            if ctx._held_ids:
                rt._emit_milestones_held_by_quarantine(ctx.state, ctx._held_ids, ctx.qmid,
                                                    ctx.qrec.get("cause", ""))
                rt.save_project_state(ctx.project_dir, ctx.state)
            # Single writer: Q's repair worktree/branch and an independent milestone's mission worktree must never be…
            if (ctx._candidate_mid is not None
                    and not rt._quarantine_repair_lease_active(ctx.project_dir, ctx.qrec)):
                ctx._scoped_mid = ctx._candidate_mid
        # 22/09 05:20 Bangkok: with an independent milestone always selectable, the
        # quarantine cycle NEVER ran (2 h of MISSION_IN_FLIGHT on lot 2C while the
        # hygiene gate was already green at HEAD — no contract frozen, no probe, no
        # exit; a human lifted it). The repair loop gets a turn at least every
        # QUARANTINE_CYCLE_EVERY_S even when independent work exists.
        if ctx._scoped_mid is not None:
            _ts = ctx.state.setdefault("_quarantine_cycle_ts", {})
            _key = ctx.qmid or "_"
            if _key not in _ts:
                _ts[_key] = rt.time.time()          # independent work keeps the first poll
                rt.save_project_state(ctx.project_dir, ctx.state)
            elif rt.time.time() - float(_ts[_key]) >= QUARANTINE_CYCLE_EVERY_S:
                _ts[_key] = rt.time.time()
                rt.save_project_state(ctx.project_dir, ctx.state)
                return rt._run_quarantine_cycle(ctx.project_dir, ctx.state, ctx.authority, ctx.executor)
        if ctx._scoped_mid is None:
            # Allowed control-plane work (DIAGNOSE / SAFE_REPAIR / VALIDATE_REPAIR) runs autonomously; a verified…
            return rt._run_quarantine_cycle(ctx.project_dir, ctx.state, ctx.authority, ctx.executor)
        from ..blocked_event_dedup import should_emit as _should_emit_scope
        ctx._scope_key = f"quarantine_scoped_to_milestone:{ctx.qmid}"
        ctx._scope_detail = {"quarantined_milestone": ctx.qmid,
                         "cause": ctx.qrec.get("cause", ""),
                         "entered_at": ctx.qrec.get("entered_at", "")}
        if _should_emit_scope(ctx.state, ctx._scope_key, "QUARANTINE_SCOPED_TO_MILESTONE",
                              ctx._scope_detail):
            rt._event(ctx.state, "QUARANTINE_SCOPED_TO_MILESTONE",
                   quarantined_milestone=ctx.qmid, selected_milestone=ctx._scoped_mid,
                   cause=ctx.qrec.get("cause", ""))
            rt.save_project_state(ctx.project_dir, ctx.state)

    if ctx._scoped_mid is not None:
        ctx.mid = ctx._scoped_mid
        ctx.entry = next(m for m in ctx.authority.roadmap["milestones"]
                     if m["id"] == ctx.mid)
    else:
        ctx.resolution: rt.Resolution = rt.resolve(ctx.authority, ctx.state)

        if ctx.resolution.all_complete and ctx.resolution.final_dod_reached:
            return rt.CycleResult("PROJECT_COMPLETE", None,
                               f"all milestones COMPLETE, final DoD reached "
                               f"({len(ctx.resolution.completed_milestones)} milestones)")

        if ctx.resolution.next_milestone is None:
            return rt.CycleResult("BLOCKED", None,
                               f"no runnable milestone: {ctx.resolution.blocked}")

        ctx.mid = ctx.resolution.next_milestone
        ctx.entry = next(m for m in ctx.authority.roadmap["milestones"]
                     if m["id"] == ctx.mid)
        # A standing authorization parks only the looping milestone, never the whole project.
        ctx.parked_missions = ctx.state.get("missions") or {}
        ctx.now_dt = rt.datetime.now(rt.timezone.utc)
        # 19/09: derive the milestone from the mission key when `parked_milestone` is absent (quota/owner…
        ctx.parked_ids = {str(r.get("parked_milestone") or k.split("#", 1)[0])
                      for k, r in ctx.parked_missions.items()
                      if isinstance(r, dict) and r.get("state") == "MILESTONE_PARKED"
                      and (rt._waiting_until(r.get("parked_until")) or ctx.now_dt) > ctx.now_dt}
        if ctx.mid in ctx.parked_ids:
            ctx.complete = {k for k, r in (ctx.state.get("completed_milestones") or {}).items()
                        if isinstance(r, dict) and r.get("state") == "COMPLETE"}
            for ctx.candidate in ctx.authority.roadmap["milestones"]:
                ctx.cid = ctx.candidate.get("id", "")
                ctx.deps = (ctx.candidate.get("depends_on") or {})
                if _candidate_eligible(ctx.candidate, ctx.complete, ctx.parked_ids,
                                       retired=_retired_ids(ctx)):
                    ctx.mid, ctx.entry = ctx.cid, ctx.candidate
                    break

    # SCALE_2 (owner 2026-09-15, "parallélise au max") : when the resolved milestone's mission is ALREADY in…
    _alt = _parallel_alternate(ctx)
    if _alt is not None:
        ctx.mid, ctx.entry = _alt
        rt._event(ctx.state, "PARALLEL_MILESTONE_SELECTED", milestone=ctx.mid,
                  reason="resolved milestone in flight; disjoint deliverables")
        rt.save_project_state(ctx.project_dir, ctx.state)
    else:
        # Starvation guard (night 15→16/09: FULL_CODEBASE_AUDIT_RECEIPT_CURRENT was resolved first every cycle…
        _next = _rotate_away_from_stuck(ctx)
        if _next is not None:
            ctx.mid, ctx.entry = _next
            if getattr(ctx, "_scoped_mid", None) is not None:
                # the rotation only ever picks milestones independent of the quarantined one (held ids excluded) — keep…
                ctx._scoped_mid = ctx.mid
            rt._event(ctx.state, "STUCK_MILESTONE_YIELDED", milestone=ctx.mid,
                      reason="resolved milestone non-progressing; rotating to next eligible")
            rt.save_project_state(ctx.project_dir, ctx.state)

    ctx.completed_rec = ctx.state["completed_milestones"].get(ctx.mid)

    return None


def _in_flight_milestones(ctx) -> dict:
    """{milestone_id: deliverables} for every mission with a live worker."""
    out = {}
    project_dir = getattr(ctx, "project_dir", None)
    if project_dir is None:
        return out
    known = list((ctx.state.get("missions") or {}).keys())
    for m in ctx.authority.roadmap["milestones"]:
        mid = m.get("id", "")
        if not mid:
            continue
        # every mission of this milestone (#M1, #M2, …), not only the first (audit 2026-09-15, GLM R6…
        ids = {f"{mid}#M1"} | {k for k in known if k.startswith(mid + "#")}
        try:
            in_flight = any(rt.is_worker_alive(project_dir, k) for k in ids)
        except HeartbeatObservationUnknown:
            # FCO_SYNTH-002: unknown liveness reserves the deliverables (a worker MAY be there); the fail-open…
            in_flight = True
        if in_flight:
            out[mid] = {str(d).strip().rstrip("/") for d in (m.get("deliverables") or [])}
    return out


def _retired_ids(ctx) -> set:
    """Milestones explicitly retired by authority (never selectable, but a dependency on one of them is…"""
    return {str(m.get("id") or "") for m in ctx.authority.roadmap["milestones"]
            if isinstance(m, dict) and m.get("retired")}


def _dependencies_satisfied(cand, complete, retired) -> bool:
    """Canonical dependency semantics shared with `project_authority.resolve`: only `required_state: COMPLETE`…"""
    for dep, req in (cand.get("depends_on") or {}).items():
        required = ((req or {}).get("required_state", "COMPLETE")
                    if isinstance(req, dict) else str(req or "COMPLETE"))
        if required == "COMPLETE" and dep not in complete and dep not in retired:
            return False
    return True


def _candidate_eligible(cand, complete, parked, *, excluded=(), busy_files=None,
                        require_declared_deliverables=False, retired=()) -> bool:
    """Shared selection invariant for every alternate milestone path."""
    cid = cand.get("id", "")
    if (not cid or cid in set(excluded) or cid in complete or cid in parked
            or cand.get("retired")):
        return False
    if not _dependencies_satisfied(cand, complete, set(retired)):
        return False
    if busy_files is None:
        return True
    mine = {str(d).strip().rstrip("/") for d in (cand.get("deliverables") or [])}
    if require_declared_deliverables and not mine:
        return False
    # Protect both exact paths and parent/child path overlap with active workers.
    return not (mine & busy_files) and not any(
        f.startswith(b + "/") or b.startswith(f + "/")
        for f in mine for b in busy_files
    )


def _parallel_alternate(ctx):
    """(mid, entry) of the next milestone runnable beside the in-flight ones, or None."""
    if getattr(ctx, "_scoped_mid", None) is not None:
        return None
    try:
        import os as _os
        if int(_os.environ.get("FACTORY_MAX_WORKERS", "1")) < 2:
            return None
    except ValueError:
        return None
    flying = _in_flight_milestones(ctx)
    if not flying or ctx.mid not in flying:
        return None
    busy_files = set().union(*flying.values()) if flying else set()
    complete = {k for k, r in (ctx.state.get("completed_milestones") or {}).items()
                if isinstance(r, dict) and r.get("state") == "COMPLETE"}
    parked = getattr(ctx, "parked_ids", set()) or set()
    for cand in ctx.authority.roadmap["milestones"]:
        cid = cand.get("id", "")
        if not _candidate_eligible(
                cand, complete, parked, excluded=flying, busy_files=busy_files,
                require_declared_deliverables=True, retired=_retired_ids(ctx)):
            continue
        return cid, cand
    return None



_NON_PROGRESS = ("AUTHORITY_DRIFT_BLOCKED", "MISSION_PASS_UNPROVEN", "MISSION_UNPROVEN_REEXECUTE",
                 "MILESTONE_PARKED", "STALE_CONTEXT_DISPATCH_BLOCKED", "MILESTONE_GATE_BLOCKED",
                 "MILESTONE_NOT_COMPILABLE", "STANDING_CAP_REACHED", "RECOVERY_LAUNCH_BLOCKED",
                 "LAUNCH_BLOCKED", "MISSION_NOT_SETTLEABLE",
                 # 2026-09-18: a milestone held at settlement (gate held, multi-AI review blocked/unavailable, promotion…
                 "MILESTONE_GATE_HELD", "MULTI_AI_REVIEW_BLOCKED",
                 "MULTI_AI_REVIEW_UNAVAILABLE", "GOLDEN_PROMOTION_FAILED",
                 "REVIEW_LEDGER_PENDING_MULTI_AI_REVIEW")


#: no engine launch for the resolved milestone nor for the project during this
#: window → the resolved milestone yields (see _rotate_away_from_stuck)
#: the quarantine repair loop's guaranteed cadence while independent work runs (22/09)
QUARANTINE_CYCLE_EVERY_S = 10 * 60
IDLE_GUARD_S = 30 * 60


def _idle_guard_tripped(ctx) -> bool:
    try:
        from ..launch_gate import ledger_path as _lp
        from ..resource_accounting import read_rows as _rr
        rows = _rr(_lp(ctx.project_dir))
    except Exception:
        return False
    import time as _t
    from datetime import datetime, timezone
    now = _t.time()
    last_any = 0.0
    last_mine = 0.0
    for r in rows:
        if r.get("engine") in (None, "", "local"):
            continue
        ts = str(r.get("started_at") or "")
        try:
            t = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        last_any = max(last_any, t)
        if str(r.get("milestone") or "") == ctx.mid:
            last_mine = max(last_mine, t)
    if not rows or last_any == 0.0:
        return False                       # a brand-new project: nothing to judge
    if last_mine == 0.0:
        # 20/09 10:50 Bangkok: a milestone that was NEVER launched is not "stuck" — the guard fired on a freshly…
        return False
    # a live worker on this milestone is progress, whatever the ledger says
    try:
        from ..mission_evidence import is_worker_alive
        for mid_ in (ctx.state.get("missions") or {}):
            if str(mid_).startswith(ctx.mid + "#") and is_worker_alive(ctx.project_dir, str(mid_)):
                return False
    except HeartbeatObservationUnknown:
        return False   # FCO_SYNTH-002: unknown liveness never trips the guard
    except Exception:
        pass
    return (now - last_any) > IDLE_GUARD_S and (now - last_mine) > IDLE_GUARD_S


def _rotate_away_from_stuck(ctx):
    """(mid, entry) of another eligible milestone when the resolved one's last two recorded outcomes are typed…"""
    held = set(getattr(ctx, "_held_ids", None) or [])
    qmid = getattr(ctx, "qmid", None)
    _neutral = {"STANDING_AUTHORIZATION_USED", "DISPATCH_CONTEXT_MANIFEST", "STALE_CONTRACT_CLEARED",
                "CONTRACT_FROZEN", "STUCK_MILESTONE_YIELDED", "PARALLEL_MILESTONE_SELECTED"}
    events = [e for e in (ctx.state.get("events") or [])[-80:]
              if isinstance(e, dict)
              and (str(e.get("milestone") or "") == ctx.mid
                   or str(e.get("mission") or "").startswith(ctx.mid + "#"))
              and str(e.get("event") or "") not in _neutral]
    last = [str(e.get("event") or "") for e in events[-2:]]
    stuck = len(last) >= 2 and all(ev in _NON_PROGRESS for ev in last)
    if not stuck and last and last[-1] in _NON_PROGRESS:
        # 2026-09-19 (FACTORY_CHAOS_DRILL_V1#M1) : blocked_event_dedup rapporte une condition bloquante IDENTIQUE…
        _mrecs = ctx.state.get("missions") or {}
        stuck = any(
            isinstance(_mrecs.get(k), dict) and _mrecs[k].get("last_blocked_fingerprint")
            for k in _mrecs
            if (str(k).startswith(ctx.mid + "#")
                or str(k) == f"multi-ai:{ctx.mid}"))
    if not stuck:
        # 2026-09-18 idle guard (owner: "ton bug était là dès le début") — the generic form of every…
        if not _idle_guard_tripped(ctx):
            return None
        rt._event(ctx.state, "IDLE_GUARD_TRIPPED", milestone=ctx.mid,
                  idle_s=IDLE_GUARD_S,
                  reason="no engine launch for this milestone nor the project within the window")
    complete = {k for k, r in (ctx.state.get("completed_milestones") or {}).items()
                if isinstance(r, dict) and r.get("state") == "COMPLETE"}
    parked = getattr(ctx, "parked_ids", set()) or set()
    flying = _in_flight_milestones(ctx)
    busy_files = set().union(*flying.values()) if flying else set()
    rr = ctx.state.setdefault("_milestone_rotation", {})
    candidates = []
    for cand in ctx.authority.roadmap["milestones"]:
        cid = cand.get("id", "")
        excluded = {ctx.mid, *held, *flying.keys()}
        if qmid:
            excluded.add(qmid)
        if not _candidate_eligible(
                cand, complete, parked, excluded=excluded, busy_files=busy_files,
                retired=_retired_ids(ctx)):
            continue
        candidates.append((str(rr.get(cid) or ""), cid, cand))
    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0])           # least recently rotated-to first
    _, cid, cand = candidates[0]
    rr[cid] = rt.datetime.now(rt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return cid, cand
