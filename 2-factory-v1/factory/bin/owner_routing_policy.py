#!/usr/bin/env python3
"""Owner-scoped temporary routing policy — expires by construction.

Three owner instructions, all narrow and time-boxed; none widens global
actor capabilities or edits any authority document.

  Phase 1 (2026-09-11, until 2026-09-12T00:00:00+02:00):
      "utiliser claude jusque demain en builder et reviewer CTO GLM"
      (use Claude as builder until tomorrow, with GLM as CTO reviewer).
      Claude builds, GLM reviews read-only at A/B.

  Phase 2 — the quota reversal (2026-09-11T09:18Z → 10:03Z), nested inside
      phase 1: GLM builds/repairs/plans, Claude reviews read-only, for the
      `factory` and `twitter-radar` projects at A/B only. The reversal is
      ALSO what unblocked GLM on A-level TR contract work: it does not touch
      the ActorCapabilityPolicy (glm still lacks global A capability —
      engine_capable("glm", "A", "build") stays False), it overrides the
      choice for a scoped, expiring window only.

  Phase 3 (2026-09-12T00:00:00Z → 08:00:00Z), the CURRENT instruction:
      GLM-5.3 executes every A/B role in `factory` and `twitter-radar` —
      build/repair/diagnose/replan/plan AND review (strictly read-only).
      No Claude, no Codex in those decisions. S-level stays outside the
      override (fail-closed: the seed policy and its claude-only S routing
      apply untouched), and the window reassigns work without granting
      capability: engine_capable("glm", "A", "build") is still False.

Every window ends by construction. Nothing extends any window: there is no
renewal path in this module, and an expired policy is indistinguishable from
an absent one. S-level (security/PII) and every other project keep the seed
policy at all times.

No model is ranked against another anywhere in this module. The owner chose
who does what for a bounded time; that is an assignment, not a measurement.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field

POLICY_ID = "owner-routing/claude-builder-glm-reviewer-2026-09-11"

# Full instant, timezone included, so "until tomorrow" is a fact and not a reading.
EXPIRES_AT = datetime.datetime(2026, 9, 12, 0, 0, 0,
                              tzinfo=datetime.timezone(datetime.timedelta(hours=2)))

# The exception is granted to these projects, and to nothing else.
SCOPED_PROJECTS = frozenset({"factory", "twitter-radar"})

# Policy restored on expiry: the seed routing table, unmodified.
PRIOR_POLICY_VERSION = "model-routing/seed-2026-09-08"

BUILDER_ENGINE = "claude"
REVIEWER_ENGINE = "glm"
# 2026-09-18 (owner: "augmente les modèles et le raisonnement si ça a du sens") :
# Claude Fable 5.1 en effort high pour les builds A ; quota semaine 8 % utilisé.
BUILDER_EFFORT = "high"

# Explicit owner reversal: GLM builds while Claude quota recovers; scoped A/B
# authority includes repair/diagnosis, not security work or other projects.
QUOTA_POLICY_ID = "owner-routing/glm-builder-claude-reviewer-2026-09-11"
QUOTA_START = datetime.datetime.fromisoformat("2026-09-11T09:18:00+00:00")
QUOTA_END = datetime.datetime.fromisoformat("2026-09-11T10:03:00+00:00")

# Owner reversed roles again: Claude builder, GLM reviewer. Preserve the old window for replay.
QUOTA_REVOKED_AT = datetime.datetime.fromisoformat("2026-09-11T09:54:28+00:00")

def quota_override_active(project, now=None):
    return (normalize_project(project) in SCOPED_PROJECTS
            and QUOTA_START <= _now_instant(now) < min(QUOTA_END, QUOTA_REVOKED_AT))

# Phase 3 — the current owner instruction. Full GLM assignment: GLM-5.3 takes
# build/repair/diagnose/replan/plan AND review (read-only) at spec A/B, for the
# same two projects and nothing else; no Claude, no Codex in those decisions.
# S-level is deliberately outside the override (fail-closed → seed policy).
PHASE3_POLICY_ID = "owner-routing/glm-all-roles-2026-09-12"
PHASE3_START = datetime.datetime.fromisoformat("2026-09-12T00:00:00+00:00")
PHASE3_END = datetime.datetime.fromisoformat("2026-09-12T08:00:00+00:00")
PHASE3_LEVELS = frozenset({"A", "B"})
PHASE3_BUILD_KINDS = frozenset({"build", "repair", "diagnose", "replan", "plan"})
PHASE3_REVIEW_KINDS = frozenset({"review"})


def phase3_override_active(project, now=None) -> bool:
    """True iff the phase-3 full-GLM assignment is in force at this instant."""
    return (normalize_project(project) in SCOPED_PROJECTS
            and PHASE3_START <= _now_instant(now) < PHASE3_END)


# Phase 4 (2026-09-15T14:40Z → 2026-09-22T00:00Z) — the CURRENT owner instruction
# ("il faut build avec glm en moteur là et codex en review et envoyer vénère,
# et 3ème builder claude derrière", GLM at 100 %, Codex at 88 %, Claude at 28 %):
#   build / repair / plan  → GLM-5.3 (writer)         at A/B, both projects
#   review / diagnose / replan → Codex (read-only)   at A/B, both projects
#   T3 escalation (stall_count >= 2 on build/repair) → Claude, the third builder
# S-level untouched. Ends by construction at the GLM weekly reset.
PHASE4_POLICY_ID = "owner-routing/glm-builder-codex-reviewer-claude-t3-2026-09-15"
PHASE4_START = datetime.datetime.fromisoformat("2026-09-15T14:40:00+00:00")
PHASE4_END = datetime.datetime.fromisoformat("2026-09-22T00:00:00+00:00")
PHASE4_LEVELS = frozenset({"A", "B"})
PHASE4_BUILD_KINDS = frozenset({"build", "repair", "plan"})
PHASE4_REVIEW_KINDS = frozenset({"review", "diagnose", "replan"})
PHASE4_T3_STALL = 2


def phase4_override_active(project, now=None) -> bool:
    return (normalize_project(project) in SCOPED_PROJECTS
            and PHASE4_START <= _now_instant(now) < PHASE4_END)


# Owner policies that assign EVERY A/B role — builder and reviewer — for their
# window (the phase-2 quota reversal, the phase-3 all-GLM instruction).
# model_router treats these as total: within the window, engine hints and
# stall escalations do not switch engines; the owner's choice is the routing.
# Phase 5 (2026-09-18 03:15 Bangkok = 2026-09-17T20:15Z, owner: "il reste la masse
# de quota Codex, faut bourriner dessus dans tous les sens — abonnement à 100
# balles, faut le rentabiliser" ; "même Claude on a rien utilisé" (8 % semaine,
# Fable 0 %) ; "GLM pareil, masse de quota" (80 % semaine)). All three engines
# build, every level, spread so that none idles:
#   A build  → Claude (Fable 5.1: its own weekly pool, untouched)
#   A repair → Codex (writable workspace-write launch, fresh eyes on a stall)
#   B build  → Codex
#   B repair → GLM
#   explicit roadmap engine_hint (glm/claude/codex) honoured;
#   quota-refused engine → next in the chain claude → codex → glm → claude;
#   review/diagnose/replan → Codex read-only (reviewer != builder enforced by
#   the caller's exclusion → Claude, then GLM).
PHASE5_POLICY_ID = "owner-routing/all-three-build-2026-09-18"
# 2026-09-18 11:30 Bangkok (owner: "il faut que tu défonces Codex là") — Codex
# takes every build; Claude Fable repairs A, GLM repairs B; hints still honoured.
# 16:25 Bangkok (owner: "tu dégommes Codex en quota là ?" — Codex 64 min vs
# Claude 186 min): Codex builds AND repairs at both levels; GLM then Claude
# are the quota-refusal fallbacks (explicit roadmap hints still win).
# 16:58 Bangkok (owner: "il faut switch sur GLM qui gère le build") — GLM
# builds (both levels), Codex repairs (astra/sol) + reviews, Claude last.
# 19/09 19:15 Bangkok: repairs on GLM too — with Codex at its weekly limit every
# repair step failed at launch (rc=-4) and the reshape escalated to Claude (refused)
# 20/09 16:25 Bangkok (owner screenshots): GLM 8 % weekly left until 22/09,
# Codex 85 % left (reset 26/09 21:53 Bangkok), Claude Max 5x 89 % left — Codex
# builds AND repairs at both levels, Claude second, GLM last (refused by quota
# until its reset anyway). Explicit roadmap hints still win — a GLM hint on a
# refused GLM falls to this chain.
# 20/09 18:30 Bangkok (owner screenshots): Codex burnt 21 % of its week in 2 h
# (astra/high, 4 parallel) — 64 % left for 6 days. Claude Max 5x sits at 12 %
# (Fable pool 11 %, its own weekly limit). Claude builds (Fable), Codex repairs
# and reviews (sol/medium), GLM last until its reset.
# 22/09 01:55 Bangkok: Fable burnt 60 % of its week in 26 h on the core rewrites
# (2.3 %/h — empty before Friday at that pace). GLM's week resets at 07:00
# Bangkok: builds go to GLM first (refused until then → chain falls to Claude),
# Claude keeps the chain's second place, Codex repairs and reviews on Sol.
# 02:00: Codex too is down to 36 % for 4.5 days — GLM (100 % from 07:00) builds
# AND repairs; until its reset the chain falls to Codex (Sol) then Claude.
PHASE5_TABLE = {("A", "build"): "glm", ("A", "repair"): "glm",
                ("B", "build"): "glm", ("B", "repair"): "glm"}
PHASE5_CHAIN = ("glm", "claude", "codex")  # 22/09 14:55 : API — GLM hebdo 100 % (reset 18:56), Codex 76 % (samedi), Claude 7 j 44 % / Fable 73 % (vendredi) → Claude avant Codex
PHASE5_START = datetime.datetime.fromisoformat("2026-09-17T20:15:00+00:00")
PHASE5_END = datetime.datetime.fromisoformat("2026-09-26T15:00:00+00:00")
PHASE5_LEVELS = frozenset({"A", "B"})
#: milestones whose engine_hint is strict (no quota reroute) — FACTORY_MILESTONE env
PHASE5_STRICT_HINT_MILESTONES = ("FACTORY_CODE_REVIEW_SWEEP_V1",)


def phase5_override_active(project, now=None) -> bool:
    return (normalize_project(project) in SCOPED_PROJECTS
            and PHASE5_START <= _now_instant(now) < PHASE5_END)


FULL_ASSIGNMENT_POLICY_IDS = (QUOTA_POLICY_ID, PHASE3_POLICY_ID, PHASE4_POLICY_ID,
                              PHASE5_POLICY_ID)

# The owner's reviewer assignment covers contract-level and mechanical review. S-level is
# security/PII and is deliberately untouched: it stays with the default engine.
_REVIEWER_LEVELS = frozenset({"A", "B"})
_REVIEW_KINDS = frozenset({"review"})
_BUILD_KINDS = frozenset({"build", "repair"})


@dataclass(frozen=True)
class PolicyOverride:
    """A single routing override, or the explicit absence of one."""
    engine: str = ""
    role: str = ""
    read_only: bool = False
    reason: str = ""
    applies: bool = False
    policy_id: str = ""
    expires_at: str = ""


_NO_OVERRIDE = PolicyOverride()


def _now_instant(now: "datetime.datetime | None" = None) -> datetime.datetime:
    if now is None:
        return datetime.datetime.now(datetime.timezone.utc)
    if now.tzinfo is None:
        raise ValueError("owner policy requires a timezone-aware instant; a naive "
                         "datetime cannot be compared to a scoped expiry")
    return now


def normalize_project(project: "str | None") -> str:
    """Project identity as the policy understands it. Unknown/absent → ""."""
    return (project or "").strip().lower()


def is_active(project: "str | None", now: "datetime.datetime | None" = None) -> bool:
    """True iff the owner exception is in force for this project at this instant."""
    if normalize_project(project) not in SCOPED_PROJECTS:
        return False
    return _now_instant(now) < EXPIRES_AT


def status(project: "str | None", now: "datetime.datetime | None" = None) -> dict:
    """Machine-readable policy status — what applies, and why.

    Reports EVERY window accurately: the phase-1 owner exception, the nested
    quota reversal and the phase-3 full-GLM assignment (each with its own
    policy id, window and effective engines), so an operator reading status()
    sees the ACTIVE override, not a stale description of an earlier phase.
    """
    p = normalize_project(project)
    instant = _now_instant(now)
    in_scope = p in SCOPED_PROJECTS
    quota_active = quota_override_active(project, instant)
    phase3_active = phase3_override_active(project, instant)
    phase4_active = phase4_override_active(project, instant)
    phase5_active = phase5_override_active(project, instant)
    active = in_scope and any((not (instant >= EXPIRES_AT), quota_active,
                               phase3_active, phase4_active, phase5_active))
    current_end = (PHASE5_END if phase5_active else PHASE4_END if phase4_active
                   else PHASE3_END if phase3_active else QUOTA_END if quota_active
                   else EXPIRES_AT)
    build_override = override_for(project, "B", "build", instant)
    review_override = override_for(project, "B", "review", instant)
    effective = ({"builder": build_override.engine,
                  "reviewer": review_override.engine + " (read-only)"}
                 if build_override.applies or review_override.applies else
                 {"builder": "seed policy", "reviewer": "seed policy (codex)"})
    current_policy = (PHASE5_POLICY_ID if phase5_active else
                      PHASE4_POLICY_ID if phase4_active else
                      PHASE3_POLICY_ID if phase3_active else
                      QUOTA_POLICY_ID if quota_active else POLICY_ID)
    current_start = (PHASE5_START if phase5_active else
                     PHASE4_START if phase4_active else
                     PHASE3_START if phase3_active else
                     QUOTA_START if quota_active else None)
    if active:
        reason = (f"owner policy {current_policy} ACTIVE for {p!r}: "
                  f"builder={effective['builder']}, reviewer={effective['reviewer']}; "
                  f"expires {current_end.isoformat()}")
    elif in_scope:
        reason = (f"owner policy EXPIRED at {current_end.isoformat()}; "
                  f"{PRIOR_POLICY_VERSION} restored")
    else:
        reason = (f"project {p!r} is outside the policy scope {sorted(SCOPED_PROJECTS)}; "
                  f"{PRIOR_POLICY_VERSION} applies")
    return {
        "policy_id": POLICY_ID,
        "project": p,
        "in_scope": in_scope,
        "expired": in_scope and not active,
        "active": active,
        "expires_at": current_end.isoformat(),
        "evaluated_at": instant.isoformat(),
        "restores_policy_version": PRIOR_POLICY_VERSION,
        "phase3_override": {
            "policy_id": PHASE3_POLICY_ID,
            "active": phase3_active,
            "window_start": PHASE3_START.isoformat(),
            "expires_at": PHASE3_END.isoformat(),
            "effective_engines": {
                "builder": "glm",
                "reviewer": "glm (read-only, restricted launch)",
            } if phase3_active else None,
            "scope": "A/B build/repair/diagnose/replan/plan/review, "
                     "factory + twitter-radar only; S-level fail-closed to seed",
        },
        "quota_override": {
            "policy_id": QUOTA_POLICY_ID,
            "active": quota_active,
            "window_start": QUOTA_START.isoformat(),
            "expires_at": QUOTA_END.isoformat(),
            "effective_engines": {
                "builder": "glm",
                "reviewer": "claude (read-only, restricted launch)",
            } if quota_active else None,
            "scope": "A/B build/repair/diagnose/replan/plan/review, "
                     "factory + twitter-radar only",
        },
        "effective_engines": effective,
        "current_phase": {
            "policy_id": current_policy,
            "active": active,
            "window_start": current_start.isoformat() if current_start else None,
            "expires_at": current_end.isoformat(),
        },
        "reason": reason,
    }


def override_for(project: "str | None", spec_level: str, task_kind: str,
                 now: "datetime.datetime | None" = None, stall_count: int = 0,
                 engine_hint: "str | None" = None,
                 codex_quota_ok: bool = True,
                 quota_refused: "tuple | None" = None) -> PolicyOverride:
    """The override the owner policy imposes on (project, spec_level, task_kind).

    Returns an inapplicable override (applies=False) whenever the policy is out of scope,
    expired, or simply has nothing to say about this (level, kind) pair — including every
    S-level decision (all phases: S is fail-closed to the seed policy).
    """
    if phase5_override_active(project, now) and str(spec_level).upper() in PHASE5_LEVELS:
        kind = str(task_kind).lower()
        lvl = str(spec_level).upper()
        if kind in ("build", "repair"):
            hint = str(engine_hint or "").lower()
            refused = set(e for e in (quota_refused or ()) if e in PHASE5_CHAIN)
            if hint in refused:
                import os as _os
                _ms = str(_os.environ.get("FACTORY_MILESTONE") or "")
                if any(_ms.startswith(x) for x in PHASE5_STRICT_HINT_MILESTONES):
                    # 19/09: the full code review must be CODEX's (owner) — at
                    # its limit the lot WAITS for the reset (launch gate refuses,
                    # interruption, retry) instead of being done by another engine
                    return PolicyOverride(
                        engine=hint, role="builder", read_only=False, applies=True,
                        policy_id=PHASE5_POLICY_ID, expires_at=PHASE5_END.isoformat(),
                        reason=(f"Owner assignment (phase 5): strict engine_hint={hint!r} for "
                                f"{_ms} — waits for quota, never rerouted"))
                hint = ""      # 2026-09-18 16:40: a hinted engine at its quota limit
                               # must not fail the mission (TR research M3, claude
                               # five_hour 0.93) — fall through to the chain
            if hint in PHASE5_CHAIN:
                return PolicyOverride(
                    engine=hint, role="builder", read_only=False, applies=True,
                    policy_id=PHASE5_POLICY_ID, expires_at=PHASE5_END.isoformat(),
                    reason=(f"Owner assignment (phase 5, {PHASE5_POLICY_ID}): explicit roadmap "
                            f"engine_hint={hint!r} honoured for {kind} at {lvl}"))
            first = PHASE5_TABLE[(lvl, kind)]
            order = PHASE5_CHAIN[PHASE5_CHAIN.index(first):] + PHASE5_CHAIN[:PHASE5_CHAIN.index(first)]
            for eng in order:
                if eng not in refused:
                    note = "" if eng == first else f" ({first} quota refused → {eng})"
                    return PolicyOverride(
                        engine=eng, role="builder", read_only=False, applies=True,
                        policy_id=PHASE5_POLICY_ID, expires_at=PHASE5_END.isoformat(),
                        reason=(f"Owner assignment (phase 5, {PHASE5_POLICY_ID}): {eng} builds "
                                f"({kind}) at {lvl}{note}; expires {PHASE5_END.isoformat()}"))
            return PolicyOverride(
                engine=first, role="builder", read_only=False, applies=True,
                policy_id=PHASE5_POLICY_ID, expires_at=PHASE5_END.isoformat(),
                reason=(f"Owner assignment (phase 5, {PHASE5_POLICY_ID}): every engine quota-refused; "
                        f"{first} kept for the launch gate to refuse with its own reason"))
        if kind in PHASE4_REVIEW_KINDS:
            return PolicyOverride(
                engine="codex", role="reviewer", read_only=True, applies=True,
                policy_id=PHASE5_POLICY_ID, expires_at=PHASE5_END.isoformat(),
                reason=(f"Owner assignment (phase 5, {PHASE5_POLICY_ID}): Codex reviews "
                        f"({kind}, read-only) at A/B unless it built the candidate"))
    if phase4_override_active(project, now) and str(spec_level).upper() in PHASE4_LEVELS:
        kind = str(task_kind).lower()
        if kind in PHASE4_BUILD_KINDS:
            # 2026-09-17 amendment (Claude weekly reset, 99 % available): A-level
            # build/repair (product/UX work) goes to Claude directly; B-level stays
            # on GLM; Claude remains the third builder for B after two stalls.
            if str(spec_level).upper() == "A" and kind in ("build", "repair"):
                return PolicyOverride(
                    engine="claude", role="builder", read_only=False, applies=True,
                    policy_id=PHASE4_POLICY_ID, expires_at=PHASE4_END.isoformat(),
                    reason=(f"Owner assignment (phase 4, amended 2026-09-17): Claude builds A-level "
                            f"({kind}); GLM builds B; Codex reviews; expires {PHASE4_END.isoformat()}"))
            if kind in ("build", "repair") and int(stall_count or 0) >= PHASE4_T3_STALL:
                return PolicyOverride(
                    engine="claude", role="builder", read_only=False, applies=True,
                    policy_id=PHASE4_POLICY_ID, expires_at=PHASE4_END.isoformat(),
                    reason=(f"Owner assignment (phase 4, {PHASE4_POLICY_ID}): Claude is the "
                            f"THIRD builder — T3 escalation after GLM (stall_count={stall_count}); "
                            f"expires {PHASE4_END.isoformat()}"))
            return PolicyOverride(
                engine="glm", role="builder", read_only=False, applies=True,
                policy_id=PHASE4_POLICY_ID, expires_at=PHASE4_END.isoformat(),
                reason=(f"Owner assignment (phase 4, {PHASE4_POLICY_ID}): GLM-5.3 builds "
                        f"({kind}) at A/B; Codex reviews; Claude only as T3; expires "
                        f"{PHASE4_END.isoformat()}"))
        if kind in PHASE4_REVIEW_KINDS:
            return PolicyOverride(
                engine="codex", role="reviewer", read_only=True, applies=True,
                policy_id=PHASE4_POLICY_ID, expires_at=PHASE4_END.isoformat(),
                reason=(f"Owner assignment (phase 4, {PHASE4_POLICY_ID}): Codex reviews "
                        f"({kind}, read-only) at A/B; expires {PHASE4_END.isoformat()}"))
    if phase3_override_active(project, now) and str(spec_level).upper() in PHASE3_LEVELS:
        kind = str(task_kind).lower()
        if kind in PHASE3_BUILD_KINDS or kind in PHASE3_REVIEW_KINDS:
            reviewer = kind in PHASE3_REVIEW_KINDS
            return PolicyOverride(
                engine="glm",
                role="reviewer" if reviewer else "builder", read_only=reviewer,
                applies=True, policy_id=PHASE3_POLICY_ID,
                expires_at=PHASE3_END.isoformat(),
                reason=(f"Explicit owner assignment (phase 3, {PHASE3_POLICY_ID}): "
                        f"GLM-5.3 executes every A/B role — {kind}, review strictly "
                        f"read-only — no Claude, no Codex; expires "
                        f"{PHASE3_END.isoformat()}"))
    if quota_override_active(project, now) and str(spec_level).upper() in ("A", "B"):
        kind = str(task_kind).lower()
        if kind in ("build", "repair", "diagnose", "replan", "plan", "review"):
            reviewer = kind == "review"
            return PolicyOverride(
                engine="claude" if reviewer else "glm",
                role="reviewer" if reviewer else "builder", read_only=reviewer,
                applies=True, policy_id=QUOTA_POLICY_ID, expires_at=QUOTA_END.isoformat(),
                reason=f"Explicit owner quota assignment: GLM builds/repairs, Claude reviews; expires {QUOTA_END.isoformat()}")
    if not is_active(project, now):
        return _NO_OVERRIDE
    level = str(spec_level).upper()
    kind = str(task_kind).lower()
    window = f"until {EXPIRES_AT.isoformat()}"

    if kind in _REVIEW_KINDS and level in _REVIEWER_LEVELS:
        return PolicyOverride(
            engine=REVIEWER_ENGINE, role="reviewer", read_only=True, applies=True,
            policy_id=POLICY_ID, expires_at=EXPIRES_AT.isoformat(),
            reason=(f"owner policy {POLICY_ID}: GLM is CTO reviewer at {level}-level, "
                    f"READ ONLY (no build/repair authority at any level), {window}"))

    if kind in _BUILD_KINDS and level in ("A", "B"):
        return PolicyOverride(
            engine=BUILDER_ENGINE, role="builder", read_only=False, applies=True,
            policy_id=POLICY_ID, expires_at=EXPIRES_AT.isoformat(),
            reason=(f"owner policy {POLICY_ID}: Claude is builder at {level}-level "
                    f"({kind}, effort={BUILDER_EFFORT}), {window}"))

    # S-level, plan, diagnose and replan are untouched by the owner instruction.
    return _NO_OVERRIDE


def main(argv=None) -> int:
    import argparse
    import json
    ap = argparse.ArgumentParser(prog="owner_routing_policy")
    ap.add_argument("--project", default="")
    ap.add_argument("--spec-level", default="A")
    ap.add_argument("--task-kind", default="build")
    args = ap.parse_args(argv)
    st = status(args.project)
    ov = override_for(args.project, args.spec_level, args.task_kind)
    print(json.dumps({"status": st, "override": ov.__dict__}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
