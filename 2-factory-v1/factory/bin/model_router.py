#!/usr/bin/env python3
"""model_router — deterministic engine routing. Delta CTO 002: GLM = Z Code.

Three AIs, four engine aliases:

  Engine  Canonical  Model                    Role         Constraint
  ------  ---------  -----------------------  -----------  ----------------------------
  glm     glm        zai-coding-plan/glm-5.3  builder      NEVER glm-highspeed
  zcode   → glm      (alias, normalized)      builder      kept for CLI backward-compat
  claude  claude     claude-sonnet-5 high      builder+sup  primary for S/A and escalation
  claude  claude     claude-opus-5   xhigh    deep         DIAGNOSE/REPLAN fallback (S-level, or Codex unavailable)
  codex   codex      gpt-5.6-sol               reviewer     read-only, no build/repair

  GLM and Z Code are the SAME engine (Delta CTO 002).
  All runtime state, leases, receipts, metrics use canonical_engine="glm".

Spec levels (from FACTORY_STACK_SPEC §5):
  S — security / PII  → claude only
  A — contract        → claude (build) + codex (review)
  B — mechanical      → glm (build) + codex (review)

Task kinds:
  build    → produce code / commits
  review   → read-only audit (codex or claude)
  plan     → produce plan artefacts (codex or claude)
  repair   → targeted fix after failed tests
  diagnose → diagnose failure root-cause (codex read-only; claude-opus-5 fallback)
  replan   → propose ROADMAP mutation (codex read-only; claude-opus-5 fallback)

Routing seed policy (model-routing/seed-2026-09-08, diagnose/replan updated
CODEX-LOT-C1 2026-09-12 — Codex has quota headroom, Claude Opus 5 is scarce):
  S/A build/repair → claude-sonnet-5 effort=high
  B build          → glm-5.3 first
  B repair stall<2 → glm-5.3
  B repair stall≥2 → claude-sonnet-5 effort=high
  review/plan      → codex (read-only)
  diagnose/replan  → codex read-only by default (A/B only; S stays Claude);
                     claude-opus-5 effort=xhigh fallback only when Codex is
                     unverifiable locally or quota-refused

Usage:
    python3 factory/bin/model_router.py --spec-level B --task-kind build
    python3 factory/bin/model_router.py --spec-level S --task-kind build --json
"""
from __future__ import annotations

import argparse
import json
import os as _os
import subprocess
import sys
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import owner_routing_policy as _owner_policy  # noqa: E402


# ---------------------------------------------------------------------------
# Model pins (policy version: model-routing/seed-2026-09-08)
# ---------------------------------------------------------------------------

POLICY_VERSION = "model-routing/seed-2026-09-08"

# ---------------------------------------------------------------------------
# Adaptive routing by measured performance (CTO decision 2026-09-13,
# ADAPTIVE_ROUTING). A measured evidence override may replace the seed
# _ROUTING table's default engine for a (spec_level, task_kind) pair, but
# ONLY when the evidence is strong: at least MIN_EVIDENCE_N independently
# validated attempts for at least two engines, and the best engine's success
# rate beats the seed engine's by at least MIN_SUCCESS_GAP. A thin sample
# (fewer than two engines with MIN_EVIDENCE_N) or unvalidated claims
# (mission_pass without independently_validated) never move the choice — see
# factory_v2/routing_evidence.py::evidence_from_ledger, which derives
# evidence and refuses to count anything but independently validated rows.
# The seed _ROUTING table below stays the DEFAULT absent sufficient
# evidence — it is not a veto: owner_routing_policy exceptions are computed
# and applied separately in route() and always take precedence over evidence.
MIN_EVIDENCE_N = 5
MIN_SUCCESS_GAP = 0.20

GLM_MODEL = "zai-coding-plan/glm-5.3"
# 2026-09-18 (owner): Fable 5.1 — the most capable model AND a separate weekly
# pool (0 % used); Claude Code CLI updated to 2.1.274 (Fable needs ≥ 2.1.251).
CLAUDE_MODEL = "claude-fable-5-1"
CLAUDE_OPUS_MODEL = "claude-opus-5"
CODEX_MODEL = "gpt-5.6-sol"
#: 2026-09-18 (owner: "hésite pas à taper du astra si ça vaut le coup sur codex
#: pour des tâches bien précises") — the strong Codex model for A-level work:
#: A-level repairs (writable), A-level reviews (the 3-AI audit review, research
#: cross-checks), diagnose/replan. B-level stays on gpt-5.6-sol.
CODEX_MODEL_STRONG = "gpt-6-astra"


# ---------------------------------------------------------------------------
# Engine identity normalisation (Delta CTO 002: GLM = Z Code)
# ---------------------------------------------------------------------------

_CANONICAL_ENGINE_MAP: dict[str, str] = {
    "glm":    "glm",
    "zcode":  "glm",   # Z Code = GLM (same engine, same model)
    "z.ai":   "glm",
    "zai":    "glm",
    "claude": "claude",
    "codex":  "codex",
    "local":  "local",
}


def normalize_engine_id(raw: str) -> str:
    """Normalize any engine alias to its canonical engine id.

    zcode / z.ai / zai / glm → "glm"   (Delta CTO 002: same engine)
    claude                   → "claude"
    codex                    → "codex"
    local                    → "local"
    anything else            → raises ValueError (fail-closed)

    Must be called before storing engine id in state, lease, receipts, or metrics.
    """
    key = (raw or "").strip().lower()
    canonical = _CANONICAL_ENGINE_MAP.get(key)
    if canonical is None:
        raise ValueError(
            f"Unknown engine alias {raw!r}. "
            f"Known aliases: {list(_CANONICAL_ENGINE_MAP)}. "
            f"FAIL_CLOSED: unrecognized engine cannot be dispatched."
        )
    return canonical


# ---------------------------------------------------------------------------
# SpecLevel / TaskKind enums
# ---------------------------------------------------------------------------

class SpecLevel(str, Enum):
    S = "S"   # security / PII
    A = "A"   # contract
    B = "B"   # mechanical


class TaskKind(str, Enum):
    BUILD    = "build"
    REVIEW   = "review"
    PLAN     = "plan"
    REPAIR   = "repair"
    DIAGNOSE = "diagnose"   # CTO 002: Opus 5 diagnosis task
    REPLAN   = "replan"     # CTO 002: Opus 5 replan task


# ---------------------------------------------------------------------------
# EngineChoice result type
# ---------------------------------------------------------------------------

@dataclass
class EngineChoice:
    engine: str       # canonical: "glm" | "claude" | "codex" | "local"
    model: str        # pinned model string passed to subprocess
    cli: str          # CLI binary to invoke
    role: str         # "builder" | "reviewer" | "supervisor"
    spec_level: str
    task_kind: str
    reason: str
    read_only: bool   # True → reviewer: must NOT write to repo
    effort: str = ""  # "" | "high" | "xhigh" — passed as --effort to Claude CLI


# ---------------------------------------------------------------------------
# Engine metadata (canonical engines only; zcode is not a separate entry)
# ---------------------------------------------------------------------------

_ENGINE_META: dict[str, dict] = {
    "glm":    {"model": GLM_MODEL,        "cli": "opencode", "role": "builder",   "read_only": False},
    "codex":  {"model": CODEX_MODEL,      "cli": "codex",    "role": "reviewer",  "read_only": True},
    "claude": {"model": CLAUDE_MODEL,     "cli": "claude",   "role": "supervisor","read_only": False},
    "local":  {"model": "",               "cli": "",         "role": "local",     "read_only": False},
}

# ActorCapabilityPolicy — which spec_levels each canonical engine can handle.
# glm: B-level mechanical only (no S/A — PII risk, no contract authority).
# claude: all levels (supervisor, security, contracts, deep escalation).
# codex: A and B (read-only reviewer; no S-level).
_ACTOR_CAPABILITY: dict[str, set[str]] = {
    "claude": {"S", "A", "B"},
    "glm":    {"B"},
    "codex":  {"A", "B"},
    "local":  {"S", "A", "B"},
}

_WRITE_TASK_KINDS: frozenset[str] = frozenset({"build", "repair"})

#: Owner opt-in only (CODEX-LOT-C2, 2026-09-12: "Codex B builder", roster in
#: projects/factory/SPEC.md / STRATEGY.md). Unset by default, so nothing here
#: ever assigns Codex a writer role unless the owner has explicitly turned
#: this on for a given process. When set, consulted by the quota-exhaustion
#: fallback below: GLM stays the default B-level builder; ONLY when GLM is
#: quota-refused for B build/repair does this let Codex take the writer role
#: (workspace-write, never read-only) instead of escalating straight to
#: Claude — sparing Claude's quota (2026-09-12 allocation note: Claude is the
#: scarcer engine; route spare load to Codex/GLM). Codex never gets any other
#: level or task_kind through this flag: never S/A, never review/plan/
#: diagnose/replan, never via engine_hint. ActorCapabilityPolicy
#: (_ACTOR_CAPABILITY / engine_capable()) is NOT widened by this flag — it is
#: a separate, narrower, explicitly-flagged path checked only in the one
#: branch below that reads it.
ALLOW_CODEX_BUILDER_ENV = "FACTORY_ALLOW_CODEX_BUILDER_B"


def _codex_builder_b_enabled(sl: SpecLevel, tk: TaskKind) -> bool:
    """True iff the owner opt-in is set AND this is B-level build/repair —
    the only (spec_level, task_kind) pair CODEX-LOT-C2 authorizes a writable
    Codex launch for. Read fresh from os.environ on every call (not cached),
    same posture as every other env-gated check in this module, so tests can
    monkeypatch os.environ per-case without import-order games.
    """
    if not _os.environ.get(ALLOW_CODEX_BUILDER_ENV):
        return False
    return sl == SpecLevel.B and tk in (TaskKind.BUILD, TaskKind.REPAIR)


ROUTING_LOOKUP_CANARY_KIND = "QUOTA_LOOKUP_ERROR_ROUTING"


def _quota_routing_lookup_guard(engine: str, exc: "BaseException") -> "tuple[bool, str]":
    """Non-spending (routing/CLI) quota-lookup failure — pinned resilience judge
    9d5314f6: not an unlimited fail-open either, but never the 2026-09-13 incident
    (12 route() calls burned the launch cap and refused GLM).

    Rules, all under the 24 h window of _quota_unknown_ration:
      - the journal file ABSENT  -> (True, "") and NEVER created: routing is a
        decision, only the spending process (launching=True) creates journal rows;
      - journal present with NO row for this provider -> append ONE canary row
        (kind QUOTA_LOOKUP_ERROR_ROUTING:*) — a warning counted by no launch cap,
        it never spends budget -> (True, "");
      - canary row already present (24 h) -> typed QUOTA_UNKNOWN_CAP refusal: the
        quota pipeline is observably broken, routing switches engines instead of
        asking a third time blind (the execve-time launching=True check stays the
        hard, journaled, capped gate);
      - only execve-time rows present -> (True, "") silently: that budget belongs
        to the spending process (repo judge: ROUTING_CALL_RATIONED);
      - any bookkeeping failure here -> (True, ""): routing never fail-closes on
        its own accounting.
    """
    import json as _json
    import time as _time
    prov = {"glm": "glm", "claude": "claude", "codex": "codex"}.get(engine, engine)
    try:
        path = _quota_unknown_journal(Path(_CONTROL_ROOT))
        if not path.exists():
            return True, ""
        canary = other = False
        now = _time.time()
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = _json.loads(line)
            except ValueError:
                continue
            if row.get("provider") != prov or now - float(row.get("ts", 0)) >= 86400:
                continue
            if str(row.get("kind", "")).startswith(ROUTING_LOOKUP_CANARY_KIND):
                canary = True
            else:
                other = True
        if canary:
            return False, (f"QUOTA_UNKNOWN_CAP: {prov} — repeated quota lookup failure at "
                           f"routing time (canary unconsumed within 24 h); routing switches "
                           f"engines, the execve-time allowance check remains the hard gate")
        if other:
            return True, ""
        with path.open("a", encoding="utf-8") as fh:
            fh.write(_json.dumps({"provider": prov, "ts": now,
                                  "kind": f"{ROUTING_LOOKUP_CANARY_KIND}:{type(exc).__name__}"}) + "\n")
        return True, ""
    except Exception:
        return True, ""


def _quota_engine_allowance(engine: str, project: "str | None", *, launching: bool = False) -> "tuple[bool, str]":
    """(allowed, reason) for launching `engine` right now, per quota_observer.

    Lazy import, and every failure mode (module missing, no snapshot, no CLI
    ledger, corrupt data, unexpected exception) resolves to `(True, "")` —
    "allowed, no reason to say otherwise" — which is today's behaviour before
    this function existed. Quota data can only ever narrow a decision here,
    never invent an exhaustion this router cannot actually observe.

    `launching=True` is passed ONLY by the spending process at execve time
    (dispatcher_v3._one_launch): that is where an unknown quota is journaled and
    rationed. Pure routing decisions (route(), CLI, tests) call this many times
    per real launch and must never journal — 2026-09-13 the first version counted
    route() calls and refused GLM after 12 of them in a test suite.
    """
    try:
        from factory_v2 import quota_observer as _qo
    except Exception as exc:
        if not launching:
            return _quota_routing_lookup_guard(engine, exc)
        # Codex re-audit #4 (finding 10): an import failure is "quota unknown" too —
        # journaled and capped like every other unknown, never an unlimited pass.
        try:
            ok, why = _quota_unknown_ration(Path(_CONTROL_ROOT), engine, kind=f"QUOTA_LOOKUP_ERROR:{type(exc).__name__}")
            return (True, "") if ok else (False, why)
        except Exception:
            return False, "QUOTA_JOURNAL_UNAVAILABLE: cannot ration an unknown quota"
    try:
        control_root = _CONTROL_ROOT
        _env_root = _os.environ.get(QUOTA_SNAPSHOT_ROOT_ENV, "").strip()
        if (_env_root and control_root is _CONTROL_ROOT_DEFAULT
                and _os.environ.get("FACTORY_NO_REAL_ENGINE") == "1"):
            # Test-only override of WHERE quota is observed. It is inert unless
            # the no-real-engine guard is armed, so a production process (which
            # never sets that guard) cannot be pointed at an empty/forged root
            # to fail open on a real quota (Codex review C2/C4, finding 3).
            control_root = Path(_env_root)
        project_dirs = [control_root / "projects" / project] if project else []
        snapshot = _qo.load_snapshot(control_root)
        cli_signals = _qo.latest_cli_signals(project_dirs, control_root=control_root)
        view = _qo.merged_view(snapshot, cli_signals)
        provider = {"glm": "glm", "claude": "claude", "codex": "codex"}.get(engine)
        if provider is None:
            return True, ""
        # RESILIENCE (owner 2026-09-13): judge utilisation PLUS the cost of the
        # attempt about to launch, so the switch happens before the cut.
        try:
            from factory_v2 import resilience as _rs
            allowance = _rs.projected_allowance(provider, view.get(provider, {}))
        except Exception:
            allowance = _qo.engine_allowance(provider, view.get(provider, {}))
        if allowance.get("allowed", True):
            # Codex re-audit #2 (finding 10): a green light that rests on NO data
            # (quota_known False: no numeric window observed) is not proven room.
            # It is journaled in a dedicated durable file and CAPPED — at most
            # FACTORY_QUOTA_UNKNOWN_CAP launches per provider per 24 h while the
            # quota stays unknown — instead of being an unlimited blind spot.
            if allowance.get("quota_known") is False and launching:
                ok, why = _quota_unknown_ration(control_root, provider)
                if not ok:
                    return False, why
            return True, ""
        return False, (f"ENGINE_QUOTA_EXHAUSTED: {provider} — "
                       f"{allowance.get('reason', 'quota refused')}")
    except Exception as exc:
        if not launching:
            return _quota_routing_lookup_guard(engine, exc)
        # Codex re-audit #3 (finding 10): a lookup failure (corrupt snapshot, import
        # or I/O error) is still "quota unknown" — same dedicated journal, same 24 h
        # cap, never a silent unlimited fail-open.
        try:
            _root = Path(_CONTROL_ROOT)
            _prov = {"glm": "glm", "claude": "claude", "codex": "codex"}.get(engine, engine)
            ok, why = _quota_unknown_ration(_root, _prov, kind=f"QUOTA_LOOKUP_ERROR:{type(exc).__name__}")
            return (True, "") if ok else (False, why)
        except Exception as exc2:
            # Codex re-audit #5: the ration itself failing is not a pass either
            return False, f"QUOTA_JOURNAL_UNAVAILABLE: {type(exc2).__name__}: cannot ration an unknown quota"


QUOTA_UNKNOWN_CAP_ENV = "FACTORY_QUOTA_UNKNOWN_CAP"
QUOTA_UNKNOWN_CAP_DEFAULT = 12          # = the daily standing-authorization cap


def _quota_unknown_journal(control_root: "Path") -> "Path":
    try:
        from factory_v2.quota_observer import snapshot_path
        return Path(snapshot_path(control_root)).parent / "QUOTA_UNKNOWN_LAUNCHES.jsonl"
    except Exception:
        return Path(control_root) / "factory_v2" / "receipts" / "QUOTA_UNKNOWN_LAUNCHES.jsonl"


def _quota_unknown_ration(control_root: "Path", provider: str, *, kind: str = "QUOTA_UNKNOWN_LAUNCH") -> "tuple[bool, str]":
    """Journal a would-be launch on unknown quota; refuse past the 24 h cap.
    A journal that cannot be written refuses (Codex re-audit #4): the ration IS a
    launch gate for the unknown-quota case.

    L3-017 (relecture 2026-09): read-count-append is ONE transaction guarded by
    an exclusive lock on the journal file itself — two concurrent dispatchers
    (threads or processes) can no longer both read `recent < cap` and both
    reserve the last place."""
    import fcntl as _fcntl
    import json as _json
    import time as _time
    cap = int(_os.environ.get(QUOTA_UNKNOWN_CAP_ENV) or QUOTA_UNKNOWN_CAP_DEFAULT)
    path = _quota_unknown_journal(control_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # "a+" so the journal inode always exists before the lock is taken:
        # locking the journal itself (not a sidecar) means every writer —
        # including one from another process — serializes on the same inode.
        fh = path.open("a+", encoding="utf-8")
    except OSError as exc:
        return False, f"QUOTA_JOURNAL_UNAVAILABLE: {provider} — {type(exc).__name__}: cannot open the unknown-quota journal"
    with fh:
        try:
            _fcntl.flock(fh.fileno(), _fcntl.LOCK_EX)
        except OSError as exc:
            return False, f"QUOTA_JOURNAL_UNAVAILABLE: {provider} — {type(exc).__name__}: cannot lock the unknown-quota journal"
        now = _time.time()
        recent = 0
        try:
            fh.seek(0)
            for line in fh.read().splitlines():
                try:
                    row = _json.loads(line)
                except ValueError:
                    continue
                # L3-016 (relecture 2026-09) examined, kept: the routing canary
                # (at most ONE per provider per 24 h, written only when the
                # journal already exists) deliberately takes one unit of this
                # cap — pinned public judge tests/test_resilience.py::
                # test_quota_routing_lookup_canary_matches_pinned_judge (9d5314f6):
                # with cap=1 and one canary the execve gate MUST refuse. A blind
                # decision is a blind decision, observation or launch.
                if row.get("provider") == provider and now - float(row.get("ts", 0)) < 86400:
                    recent += 1
        except OSError as exc:
            # Codex re-audit #5 (finding 10): an unreadable journal cannot prove the cap
            # is respected — refuse rather than count from zero.
            return False, f"QUOTA_JOURNAL_UNAVAILABLE: {provider} — {type(exc).__name__}: cannot read the unknown-quota journal"
        if recent >= cap:
            return False, (f"QUOTA_UNKNOWN_CAP: {provider} — {recent} launches in 24 h with no "
                           f"observed quota window (cap {cap}, {QUOTA_UNKNOWN_CAP_ENV}); refresh the "
                           f"quota snapshot or raise the cap explicitly")
        try:
            fh.seek(0, _os.SEEK_END)
            fh.write(_json.dumps({"ts": now, "provider": provider, "kind": kind,
                                  "recent_24h": recent + 1, "cap": cap}) + "\n")
            fh.flush()
            _os.fsync(fh.fileno())
        except OSError as exc:
            # Codex re-audit #4 (finding 10): without the journal the cap cannot be
            # enforced — fail CLOSED (a launch on unknown quota that cannot even be
            # counted is exactly the blind spend this ration exists to prevent).
            return False, f"QUOTA_JOURNAL_UNAVAILABLE: {provider} — {type(exc).__name__}: cannot record an unknown-quota launch"
    return True, ""


def engine_capable(engine: str, spec_level: "SpecLevel | str",
                   task_kind: "TaskKind | str | None" = None) -> bool:
    """True iff canonical engine is authorized for (spec_level [, task_kind]).

    Normalize the engine id first — zcode → glm, etc. Unknown aliases → False (fail-closed).
    """
    try:
        canonical = normalize_engine_id(engine)
    except ValueError:
        return False
    sl = SpecLevel(spec_level) if not isinstance(spec_level, SpecLevel) else spec_level
    capable = _ACTOR_CAPABILITY.get(canonical)
    if capable is None:
        return False
    if sl.value not in capable:
        # 2026-09-18: the level table guards WRITING. A REVIEW is read-only
        # judgment: the multi-AI roster (codex+claude+glm, A-level audit)
        # needs GLM as third reviewer and its restricted opencode launch
        # cannot write whatever the level. Writes stay refused.
        if task_kind is not None:
            tk0 = TaskKind(task_kind) if not isinstance(task_kind, TaskKind) else task_kind
            if tk0 == TaskKind.REVIEW and canonical == "glm":
                return True
        return False
    if task_kind is not None:
        tk = TaskKind(task_kind) if not isinstance(task_kind, TaskKind) else task_kind
        if _ENGINE_META.get(canonical, {}).get("read_only") and tk.value in _WRITE_TASK_KINDS:
            return False
    return True


# ---------------------------------------------------------------------------
# Routing table (seed policy 2026-09-08)
# ---------------------------------------------------------------------------

_ROUTING: dict[tuple[SpecLevel, TaskKind], tuple[str, str, str]] = {
    # (spec_level, task_kind) → (engine, effort, reason)

    # S-level: Claude only (security/PII)
    (SpecLevel.S, TaskKind.BUILD):    ("claude", "high",  "S-level security/PII → Claude Sonnet 5 high"),
    (SpecLevel.S, TaskKind.REVIEW):   ("claude", "high",  "S-level review → Claude only"),
    (SpecLevel.S, TaskKind.PLAN):     ("claude", "high",  "S-level plan → Claude only"),
    (SpecLevel.S, TaskKind.REPAIR):   ("claude", "high",  "S-level repair → Claude Sonnet 5 high"),
    (SpecLevel.S, TaskKind.DIAGNOSE): ("claude", "xhigh", "S-level diagnose → Claude Opus 5"),
    (SpecLevel.S, TaskKind.REPLAN):   ("claude", "xhigh", "S-level replan → Claude Opus 5"),

    # A-level: Claude builds/repairs, GLM reviews read-only (Codex exceptional-only)
    (SpecLevel.A, TaskKind.BUILD):    ("claude", "high",  "A-level contract build → Claude Sonnet 5 high"),
    # Same fix as B-level review below (Codex R2 finding: this row was left
    # unchanged, so an A-level review fell back to Codex the moment any
    # time-boxed owner override expired). GLM is NEVER an A-level builder —
    # _ACTOR_CAPABILITY still has no "A" for glm — but the bounded read-only
    # opencode launch (GLM_REVIEW_AGENT) writes nothing, so read-only review
    # authority is independent of build/write capability.
    (SpecLevel.A, TaskKind.REVIEW):   ("glm",    "",      "A-level review → bounded read-only GLM reviewer (Codex reserved for exceptional adversarial escalation)"),
    (SpecLevel.A, TaskKind.PLAN):     ("codex",  "",      "A-level plan → Codex"),
    (SpecLevel.A, TaskKind.REPAIR):   ("claude", "high",  "A-level repair → Claude Sonnet 5 high"),
    (SpecLevel.A, TaskKind.DIAGNOSE): ("claude", "xhigh", "A-level diagnose → Claude Opus 5"),
    (SpecLevel.A, TaskKind.REPLAN):   ("claude", "xhigh", "A-level replan → Claude Opus 5"),

    # B-level: GLM-5.3 builds first, Codex reviews, Claude escalates
    (SpecLevel.B, TaskKind.BUILD):    ("glm",    "",      "B-level mechanical build → GLM-5.3"),
    # FACTORY_LAW/AGENTS.md role definitions: Codex/Sol is the "falsificateur
    # adversarial exceptionnel" — an exceptional escalation, not the default
    # production reviewer. The restricted read-only GLM/opencode review path
    # (GLM_REVIEW_AGENT, model_router._restricted_glm_argv) already existed
    # but nothing in the default production loop routed to it, so a B-level
    # review always launched Codex even though Claude is the routine
    # "reviewer CTO read-only" role. B-level mechanical review now uses the
    # bounded read-only GLM reviewer by default; Codex remains available via
    # explicit engine_hint for a genuinely exceptional adversarial pass.
    (SpecLevel.B, TaskKind.REVIEW):   ("glm",    "",      "B-level review → bounded read-only GLM reviewer (Codex reserved for exceptional adversarial escalation)"),
    (SpecLevel.B, TaskKind.PLAN):     ("codex",  "",      "B-level plan → Codex"),
    (SpecLevel.B, TaskKind.REPAIR):   ("glm",    "",      "B-level repair → GLM-5.3 (targeted strategy)"),
    (SpecLevel.B, TaskKind.DIAGNOSE): ("claude", "xhigh", "B-level diagnose → Claude Opus 5"),
    (SpecLevel.B, TaskKind.REPLAN):   ("claude", "xhigh", "B-level replan → Claude Opus 5"),
}


def runrecords_override(
    spec_level: "SpecLevel | str",
    task_kind: "TaskKind | str",
    evidence: "dict | None",
) -> "str | None":
    """Return the canonical engine measured evidence says to prefer for
    (spec_level, task_kind), or None if the evidence is not strong enough to
    move the seed table's default choice.

    evidence: {engine: {"n": int, "success": int}} — n and success MUST
    already be restricted to independently validated attempts (mission_pass
    alone never counts); factory_v2.routing_evidence.evidence_from_ledger is
    the only producer this router trusts.

    Rules (CTO decision 2026-09-13):
      - at least two engines must have n >= MIN_EVIDENCE_N, otherwise the
        sample is too thin to compare rates at all → None.
      - the best-rate engine among those qualified must beat the seed
        table's default engine's rate by >= MIN_SUCCESS_GAP (an engine with
        no qualifying evidence is treated as rate 0.0 for this comparison —
        it cannot itself be dethroned by an unmeasured baseline).
      - the winning engine must still pass ActorCapabilityPolicy
        (engine_capable) for this exact (spec_level, task_kind) — evidence
        never widens capability.
      - if the winner IS already the seed engine, there is nothing to
        override → None.
    """
    sl = SpecLevel(spec_level) if not isinstance(spec_level, SpecLevel) else spec_level
    tk = TaskKind(task_kind) if not isinstance(task_kind, TaskKind) else task_kind
    seed = _ROUTING.get((sl, tk))
    if seed is None or not evidence:
        return None
    seed_engine = seed[0]
    qualified = {
        eng: rec for eng, rec in evidence.items()
        if isinstance(rec, dict) and int(rec.get("n", 0) or 0) >= MIN_EVIDENCE_N
    }
    if len(qualified) < 2:
        return None
    rates = {
        eng: (float(rec.get("success", 0) or 0) / int(rec["n"]))
        for eng, rec in qualified.items()
    }
    best_engine = max(rates, key=lambda e: rates[e])
    best_rate = rates[best_engine]
    seed_rate = rates.get(seed_engine, 0.0)
    if best_rate - seed_rate < MIN_SUCCESS_GAP:
        return None
    if best_engine == seed_engine:
        return None
    if not engine_capable(best_engine, sl, tk):
        return None
    return best_engine


class ReviewerEqualsBuilder(RuntimeError):
    """route() was asked to exclude the engine(s) that built the work (builder
    != reviewer, FACTORY_LAW), but no ActorCapabilityPolicy-authorized
    alternative exists for the requested (spec_level, task_kind) once those
    engines are removed from consideration.

    Fail-closed: the caller gets no EngineChoice at all rather than one that
    silently names the excluded engine anyway. CODEX_REVIEW_C2_C4 finding 4 —
    before this, the tail-only exclusion check (and the earlier owner/quota
    `return`s that bypassed it entirely) fell back to "keep the excluded
    engine, note it in `reason`" whenever no alternative existed, which is
    exactly the unsafe case: a review or plan of a mission that only its own
    builder is capable of "reviewing" must be refused, not rubber-stamped.
    """


#: Codex reasoning effort per model (owner 2026-09-18: more reasoning when it
#: buys speed to a correct result). Passed as `-c model_reasoning_effort=…`.
CODEX_REASONING = {CODEX_MODEL_STRONG: "high", CODEX_MODEL: "medium"}


def _codex_model_for(sl: "SpecLevel", tk: "TaskKind") -> str:
    """Sol (medium) for every build/repair/review; Astra only for diagnose/replan.

    20/09 18:30 Bangkok: astra/high on four parallel A-level repairs burnt 21 % of
    the Codex week in two hours. Quota over ceremony: the repair lots are ordinary
    correctness work Sol handles; Astra stays for the rare root-cause/replan calls.
    """
    if tk in (TaskKind.DIAGNOSE, TaskKind.REPLAN):
        return CODEX_MODEL_STRONG
    return CODEX_MODEL


def _finalize_choice(choice: EngineChoice, excluded_engines: set[str],
                     sl: SpecLevel, tk: TaskKind) -> EngineChoice:
    """The single place `exclude_engines` (builder != reviewer, FACTORY_LAW)
    is enforced, called at EVERY `return` inside route() instead of only the
    function's own final one.

    CODEX_REVIEW_C2_C4 finding 4: `exclude_engines` was checked once, in a
    block placed just before route()'s own tail `return EngineChoice(...)`.
    Every earlier `return` inside route() — the owner FULL_ASSIGNMENT branch,
    the diagnose/replan Codex-or-Claude split, and both quota-fallback
    branches (Codex-B-builder opt-in, Claude<->GLM reassignment) — skipped
    that block entirely, so a caller passing exclude_engines could still get
    the excluded engine back the moment the request also happened to hit one
    of those branches: an API a caller could set and not have honoured,
    silently. Routing every return through this one function makes the
    invariant apply uniformly regardless of which branch produced the choice.

    Never widens capability: the replacement engine still has to pass
    engine_capable() for this exact (spec_level, task_kind), same check every
    other reassignment in this module uses. When no alternative is
    authorized, raises ReviewerEqualsBuilder — fail-closed, never the old
    "keep the excluded engine with a note" behaviour.
    """
    if choice.engine == "codex":
        _m = _codex_model_for(sl, tk)
        if choice.model != _m:
            import dataclasses as _dc
            choice = _dc.replace(choice, model=_m)
    if not excluded_engines or choice.engine not in excluded_engines:
        return choice
    # L3-019 (relecture 2026-09): `local` is NOT a launchable alternative —
    # cli_args() raises "unknown canonical engine" for it, so handing back an
    # EngineChoice(local) announced a routing success that could never start.
    # Only engines cli_args can launch are candidates; when none remains the
    # typed exhaustion error below is the answer.
    for _alt in ("claude", "glm", "codex"):
        if _alt in excluded_engines or _alt == choice.engine:
            continue
        if not engine_capable(_alt, sl, tk):
            continue
        alt_meta = _ENGINE_META[_alt]
        alt_read_only = alt_meta["read_only"]
        alt_role = alt_meta["role"]
        # Same GLM-as-bounded-reviewer carve-out route()'s own REVIEW branch
        # applies: GLM's static meta marks it a builder because it normally
        # is one, but a REVIEW task routed to it is the restricted read-only
        # opencode launch, never the builder session again.
        # L3-018 (relecture 2026-09): DIAGNOSE/REPLAN get the same guarantee —
        # they are read-only judgment task kinds (_WRITE_TASK_KINDS excludes
        # them); the fallback must never widen a diagnosis into an
        # unrestricted writer (--dangerously-skip-permissions).
        if _alt in ("glm", "claude") and tk in (TaskKind.REVIEW, TaskKind.DIAGNOSE,
                                                TaskKind.REPLAN):
            # 2026-09-18: Claude too — its REVIEW launch is the verified
            # restricted read-only argv (_restricted_claude_argv), never the
            # supervisor writer (review_dispatch refused it otherwise).
            alt_read_only = True
            alt_role = "reviewer"
        return EngineChoice(
            engine=_alt, model=(_codex_model_for(sl, tk) if _alt == "codex" else alt_meta["model"]),
            cli=alt_meta["cli"],
            role=alt_role, spec_level=sl.value, task_kind=tk.value,
            reason=(f"engine={choice.engine!r} EXCLUDED by caller (builder "
                   f"!= reviewer); reassigned to {_alt!r} — was: "
                   f"{choice.reason}"),
            read_only=alt_read_only, effort="")
    raise ReviewerEqualsBuilder(
        f"REVIEWER_EQUALS_BUILDER: engine={choice.engine!r} is excluded "
        f"(builder != reviewer) for (spec_level={sl.value!r}, "
        f"task_kind={tk.value!r}), and no ActorCapabilityPolicy-authorized "
        f"alternative remains among {sorted(excluded_engines)!r} excluded — "
        f"refusing rather than silently handing the work back to its own "
        f"builder. reason was: {choice.reason}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def route(
    spec_level: SpecLevel | str,
    task_kind: TaskKind | str,
    stall_count: int = 0,
    engine_hint: "str | None" = None,
    project: "str | None" = None,
    now=None,
    budget_s: "int | None" = None,
    exclude_engines: list[str] | tuple[str, ...] | set[str] | None = None,
    evidence: "dict | None" = None,
) -> EngineChoice:
    """Return the EngineChoice for (spec_level, task_kind, stall_count).

    engine_hint: normalized via normalize_engine_id() before capability check.
    stall_count ≥ 2 escalates B-level repair to claude-sonnet-5.
    diagnose/replan task kinds always return claude-opus-5 regardless of stall.

    project: the project identity this decision belongs to. A time-boxed owner policy may
    be scoped to specific projects (see factory/bin/owner_routing_policy.py); without the
    project, a per-project exception would silently become a global routing change. When
    project is None the seed table applies unchanged — the exception is opt-in by identity.

    budget_s: accepted for signature symmetry with cli_args(), which is the function that
    actually consumes it (via writer_max_turns()) to scale the writer's --max-turns. route()
    does not use it today — engine/effort selection is unaffected by budget_s — but a caller
    that already has budget_s in scope (e.g. dispatcher_v3._route_and_record) can thread it
    through both calls without two different parameter shapes. Default None: unused, no
    behaviour change.

    exclude_engines: builder != reviewer (FACTORY_LAW). A caller that already knows which
    engine built a mission (e.g. it was routed a writable Codex builder under
    FACTORY_ALLOW_CODEX_BUILDER_B) may pass that engine here so it is never the SAME
    engine route() then hands back for a review/plan of the same work. Enforced via
    _finalize_choice() at EVERY `return` in this function (CODEX_REVIEW_C2_C4 finding 4 —
    a single tail-only check let the owner FULL_ASSIGNMENT branch, the diagnose/replan
    split and both quota-fallback branches hand back an excluded engine unchecked), so it
    catches routing-table defaults, hint overrides, owner overrides and every quota
    fallback uniformly: if the engine any of those rules selected is excluded, this
    reassigns to the next ActorCapabilityPolicy-authorized engine (never widening
    capability — same engine_capable() check every other reassignment in this function
    uses), or — if nothing else is authorized for this (spec_level, task_kind) — raises
    ReviewerEqualsBuilder rather than returning an EngineChoice that names the excluded
    engine anyway. Default None: no exclusion, byte-for-byte unchanged behaviour.
    """
    sl = SpecLevel(spec_level)
    tk = TaskKind(task_kind)

    excluded_engines: set[str] = set()
    if exclude_engines:
        for _e in exclude_engines:
            try:
                excluded_engines.add(normalize_engine_id(_e))
            except ValueError:
                continue

    engine, effort, reason = _ROUTING[(sl, tk)]

    # Adaptive routing by measured performance (CTO decision 2026-09-13):
    # consulted BEFORE the owner_routing_policy exceptions below and before
    # any hint/stall/quota logic, but owner_routing_policy always wins —
    # every owner_override branch further down unconditionally overwrites
    # `engine` when it applies, so a measured override here can never survive
    # a scoped owner exception. Absent strong evidence (see
    # runrecords_override docstring), the seed table default above is left
    # untouched.
    if evidence:
        _seed_engine = engine
        _override_engine = runrecords_override(sl, tk, evidence)
        if _override_engine and _override_engine != engine:
            engine = _override_engine
            effort = "high" if _override_engine == "claude" else effort
            reason = (f"runrecords evidence override (n>={MIN_EVIDENCE_N}/engine, "
                      f"gap>={MIN_SUCCESS_GAP}): measured performance prefers "
                      f"{_override_engine!r} over seed default {_seed_engine!r}")

    # Owner-scoped, expiring exception. Evaluated FIRST so that its reason survives into
    # the receipt, and never applied outside its declared scope/window/levels. It cannot
    # grant write authority: read_only is carried through to the EngineChoice below.
    owner_override = _owner_policy.override_for(project, sl.value, tk.value, now=now,
                                                stall_count=stall_count)

    # A FULL owner assignment (phase-2 quota reversal, phase-3 all-GLM window)
    # reassigns the engine for A/B work in the scoped projects for a bounded
    # window — GLM may then take build/repair/diagnose/replan/plan at A even
    # though engine_capable("glm", "A", "build") stays False: a scoped policy
    # may reassign work, never widen capability, and hints/stall escalations
    # do not switch engines inside the window. The scoped decision (with its
    # policy id and expiry) is carried into the launch receipt.
    if owner_override.applies and owner_override.policy_id in _owner_policy.FULL_ASSIGNMENT_POLICY_IDS:
        # Phase 4 names Claude as the THIRD builder. The escalation ladder's
        # ALTERNATE_ENGINE stage expresses exactly that (engine_hint="claude" after
        # repeated equivalent failures) — under phase 4 that hint is honoured for
        # build/repair instead of being swallowed by the full assignment
        # (GLM+Codex relaunch audit 2026-09-15: "Claude 3e builder n'arrive jamais").
        if (owner_override.policy_id == getattr(_owner_policy, "PHASE4_POLICY_ID", None)
                and tk in (TaskKind.BUILD, TaskKind.REPAIR)
                and str(engine_hint or "").lower() == "claude"):
            owner_override = _owner_policy.override_for(project, sl.value, tk.value, now=now,
                                                        stall_count=max(int(stall_count or 0),
                                                                        _owner_policy.PHASE4_T3_STALL))
        # Phase 5 (2026-09-18): Codex builds everywhere; explicit roadmap hints
        # (glm/claude) honoured; Codex quota refused → Claude (A) / GLM (B).
        if (owner_override.policy_id == getattr(_owner_policy, "PHASE5_POLICY_ID", None)
                and tk in (TaskKind.BUILD, TaskKind.REPAIR)):
            _refused = []
            _why5 = []
            for _e5 in ("claude", "codex", "glm"):
                _ok5, _w5 = _quota_engine_allowance(_e5, project)
                if not _ok5:
                    _refused.append(_e5)
                    _why5.append(f"{_e5}: {_w5}")
            _hint5 = None
            try:
                _hint5 = normalize_engine_id(engine_hint) if engine_hint else None
            except ValueError:
                _hint5 = None
            owner_override = _owner_policy.override_for(
                project, sl.value, tk.value, now=now, stall_count=int(stall_count or 0),
                engine_hint=_hint5, quota_refused=tuple(_refused))
            if _why5:
                import dataclasses as _dc
                owner_override = _dc.replace(
                    owner_override, reason=f"{owner_override.reason} [{'; '.join(_why5)}]")
        meta = _ENGINE_META[owner_override.engine]
        override_read_only = owner_override.read_only
        override_role = owner_override.role
        # CODEX_REVIEW_C13 finding 1: a FULL_ASSIGNMENT override is evaluated
        # before the diagnose/replan carve-out below, so a policy that marks
        # diagnose/replan read_only=False (phase3/quota policies both put
        # "diagnose"/"replan" in their build-kind set — see
        # owner_routing_policy.PHASE3_BUILD_KINDS) would otherwise hand back a
        # writer (GLM/Claude) for what must always be read-only judgment.
        # diagnose/replan are NEVER write task kinds (see _WRITE_TASK_KINDS
        # above, which deliberately excludes them) — no override may widen
        # that, so this branch forces read_only regardless of what the policy
        # asked for. Only glm/claude/codex have a verified restricted launch
        # (_restricted_glm_argv / _restricted_claude_argv /
        # _restricted_codex_argv in cli_args()); an override naming any other
        # engine for diagnose/replan has no safe read-only path, so it is
        # refused fail-closed here rather than silently returned as a writer.
        if tk in (TaskKind.DIAGNOSE, TaskKind.REPLAN):
            if owner_override.engine not in ("glm", "claude", "codex"):
                raise ReadOnlyLaunchNotImplemented(
                    f"READ_ONLY_LAUNCH_NOT_IMPLEMENTED: owner policy "
                    f"{owner_override.policy_id!r} assigned engine="
                    f"{owner_override.engine!r} to task_kind={tk.value!r}, "
                    f"which has no verified restricted (read-only) launch — "
                    f"diagnose/replan may never run as a writer, and there is "
                    f"no unrestricted fallback by design")
            override_read_only = True
            override_role = "reviewer"
        return _finalize_choice(EngineChoice(
                            engine=owner_override.engine, model=meta["model"],
                            cli=meta["cli"], role=override_role,
                            spec_level=sl.value, task_kind=tk.value,
                            reason=owner_override.reason,
                            read_only=override_read_only,
                            effort="low" if override_read_only else ""),
                            excluded_engines, sl, tk)

    # diagnose/replan — not overridable by hint. CODEX-LOT-C1 (2026-09-12,
    # quota allocation note: Codex has full quota headroom, Claude Opus 5 is
    # the scarce/costly engine): diagnose/replan is READ-ONLY judgment — a
    # root-cause report or a proposed ROADMAP mutation for a human/DIAGNOSE
    # consumer to act on, never a commit — so it now defaults to the same
    # verified restricted read-only Codex launch review/plan already use
    # (`_restricted_codex_argv`: `-s read-only --ephemeral`), sparing Claude's
    # quota on every routine impasse. Claude Opus 5 xhigh remains the ONLY
    # fallback, used only when Codex is not actually usable right now:
    #   - S-level: engine_capable("codex", "S", ...) is False by
    #     ActorCapabilityPolicy (codex has no "S" — security/PII stays
    #     Claude-only) — codex is never even considered.
    #   - codex binary/flags unverifiable locally (same
    #     codex_restricted_flag_report()/_codex_probe_cache probe
    #     `_restricted_codex_argv` uses below — one real local `--help`
    #     probe, cached, not a second one).
    #   - codex quota-refused (_quota_engine_allowance("codex", project)).
    # SAFE_REPAIR/build/repair task kinds are untouched by this block: they
    # never reach here (write task kinds go through the routing table below).
    if tk in (TaskKind.DIAGNOSE, TaskKind.REPLAN):
        codex_ready = False
        codex_unavailable_reason = ""
        if engine_capable("codex", sl, tk):
            if "report" not in _codex_probe_cache:
                _codex_probe_cache["report"] = codex_restricted_flag_report()
            _missing = _codex_probe_cache["report"]
            if _missing:
                codex_unavailable_reason = (
                    f"codex CLI unverified for read-only {tk.value} ({_missing})")
            else:
                _quota_ok, _quota_reason = _quota_engine_allowance("codex", project)
                if not _quota_ok:
                    codex_unavailable_reason = _quota_reason
                else:
                    codex_ready = True
        if codex_ready:
            codex_meta = _ENGINE_META["codex"]
            return _finalize_choice(EngineChoice(
                engine="codex",
                model=codex_meta["model"],
                cli=codex_meta["cli"],
                role="reviewer",
                spec_level=sl.value,
                task_kind=tk.value,
                reason=(f"{reason} — routed to Codex read-only instead "
                       f"(routing seed 2026-09-12: {tk.value} is read-only "
                       f"judgment, spares Claude Opus 5 quota)"),
                read_only=True,
                effort="",
            ), excluded_engines, sl, tk)
        # CODEX_REVIEW_C13 finding 1 (CRITICAL): this used to return
        # read_only=False — an unrestricted Claude WRITER — the instant Codex
        # was unavailable, even though diagnose/replan is read-only judgment
        # by design (see _WRITE_TASK_KINDS above, and the FULL_ASSIGNMENT
        # override block a few lines up, which already refuses to hand this
        # task kind a writer). The fallback must be the same verified
        # restricted read-only Claude launch review/plan already use
        # (claude_restricted_flag_report() / _restricted_claude_argv()) —
        # never silently widened to a writer just because Codex happened to
        # be unavailable right now. If the local CLI cannot verify the
        # restricted launch either, there is no safe route left: refuse
        # fail-closed (typed DIAGNOSE_NO_READONLY_ROUTE) rather than launch
        # a writer for what must never write.
        meta = _ENGINE_META["claude"]
        fallback_note = f" [{codex_unavailable_reason}]" if codex_unavailable_reason else ""
        if "report" not in _claude_probe_cache:
            _claude_probe_cache["report"] = claude_restricted_flag_report()
        _claude_unverified = _claude_probe_cache["report"]
        if _claude_unverified:
            raise ReadOnlyLaunchNotImplemented(
                "DIAGNOSE_NO_READONLY_ROUTE: "
                f"{tk.value} at spec_level={sl.value!r} has no verified "
                f"read-only route — codex unavailable "
                f"({codex_unavailable_reason or 'not codex-capable'}) and the "
                f"restricted Claude launch could not be verified against the "
                f"local CLI ({_claude_unverified}). No unrestricted fallback "
                f"exists by design: diagnose/replan may never run as a writer.")
        return _finalize_choice(EngineChoice(
            engine="claude",
            model=CLAUDE_OPUS_MODEL,
            cli=meta["cli"],
            role="reviewer",
            spec_level=sl.value,
            task_kind=tk.value,
            reason=f"{reason}{fallback_note}",
            read_only=True,
            effort="xhigh",
        ), excluded_engines, sl, tk)

    # B-level repair escalation on stall
    if tk == TaskKind.REPAIR and sl == SpecLevel.B and stall_count >= 2:
        engine = "claude"
        effort = "high"
        reason = f"B-level repair stall={stall_count} → Claude Sonnet 5 high (no cheap retry)"
    elif tk == TaskKind.REPAIR and sl == SpecLevel.A and stall_count >= 1:
        engine = "claude"
        effort = "high"
        reason = f"A-level repair stall={stall_count} → Claude Sonnet 5 high"

    # engine_hint override (escalation ladder ALTERNATE_ENGINE)
    if engine_hint:
        try:
            canonical_hint = normalize_engine_id(engine_hint)
        except ValueError:
            reason = (f"engine_hint={engine_hint!r} REJECTED: unknown alias; "
                      f"routing table used: {engine!r}")
            canonical_hint = None
        if canonical_hint and engine_capable(canonical_hint, sl, tk):
            engine = canonical_hint
            reason = f"engine_hint override from escalation ladder: {canonical_hint!r}"
        elif canonical_hint:
            reason = (f"engine_hint={canonical_hint!r} REJECTED by ActorCapabilityPolicy "
                      f"(spec_level={sl.value!r}, task_kind={tk.value!r}); "
                      f"routing table used: {engine!r}")

    read_only = _ENGINE_META[engine]["read_only"]
    role = _ENGINE_META[engine]["role"]

    # GLM's static engine meta marks it a builder (read_only=False) because it
    # normally IS one — B-level build routes here too. A REVIEW task_kind
    # routed to glm is never that: it is the bounded restricted opencode
    # launch (model_router._restricted_glm_argv / GLM_REVIEW_AGENT), which
    # exists precisely so "reviewer" can never mean "the builder, again,
    # grading itself". Scoped to engine=="glm" so every existing codex/claude
    # review path (already read_only by its own static meta) is unaffected.
    if engine == "glm" and tk == TaskKind.REVIEW:
        read_only = True
        role = "reviewer"

    # 2026-09-18: same for Claude — a REVIEW routed to claude (multi-AI roster,
    # engine_hint) is the verified restricted launch (_restricted_claude_argv:
    # --tools Read,Glob,Grep, no write/exec), never the supervisor writer.
    # review_dispatch refused it as ROUTED_ENGINE_NOT_READ_ONLY and the 3-AI
    # review of the audit could never get past Codex. Fail-closed when the
    # local CLI cannot verify the restricted flags (same rule as diagnose).
    if engine == "claude" and tk == TaskKind.REVIEW:
        # the restricted flags are verified at cli_args() time
        # (_restricted_claude_argv refuses an unverified launch — never an
        # unrestricted fallback), so routing itself needs no CLI probe.
        read_only = True
        role = "reviewer"

    if owner_override.applies and owner_override.read_only:
        # READ-ONLY reviewer assignment. The owner may name any engine as reviewer at
        # A/B, because reviewing writes nothing: the risk the level guards against is
        # what an engine may WRITE. _ACTOR_CAPABILITY is deliberately NOT widened, so
        # engine_capable("glm", "A", "build") stays False and this assignment carries no
        # build authority — at A, at S, by hint or by escalation.
        engine = owner_override.engine
        role = owner_override.role or "reviewer"
        read_only = True
        effort = ""
        reason = owner_override.reason
    elif owner_override.applies:
        # WRITE assignment (build/repair). This one goes through the capability check
        # unchanged: a scoped policy may reassign work, never widen capability.
        if engine_capable(owner_override.engine, sl, tk):
            engine = owner_override.engine
            role = owner_override.role or _ENGINE_META[engine]["role"]
            read_only = _ENGINE_META[engine]["read_only"]
            effort = (_owner_policy.BUILDER_EFFORT
                      if engine == "claude" else effort)
            reason = owner_override.reason
        else:
            reason = (f"owner policy {owner_override.policy_id} override to "
                      f"{owner_override.engine!r} REFUSED by ActorCapabilityPolicy "
                      f"(spec_level={sl.value!r}, task_kind={tk.value!r}); seed table "
                      f"used: {engine!r}. A scoped policy may reassign work, never widen "
                      f"capability.")

    # ── quota-aware fallback (owner quota context, 2026-09-12) ───────────────
    # Only gates WRITER launches: a builder can burn a whole subscription
    # window on one long session, while this router's read-only launches are
    # already bounded by construction (REVIEW_MAX_TURNS / codex --ephemeral),
    # so they are left to run rather than refused here. Exhausting a
    # builder's quota reroutes the work — it never leaves a caller with no
    # route at all — and the fallback reuses exactly the policy already
    # enforced above: it may reassign work to a still-capable engine, never
    # widen capability (never GLM as an A/S-level builder).
    #
    # CODEX-LOT-C2 (2026-09-12): Codex IS now assignable a writer role here,
    # but only through the narrow, explicitly-flagged path below
    # (_codex_builder_b_enabled) — GLM quota-refused, B-level build/repair,
    # FACTORY_ALLOW_CODEX_BUILDER_B=1. Preference order when GLM is refused:
    # Codex (if the flag is on) BEFORE Claude — Claude is the scarcer quota
    # (2026-09-12 allocation note), so sparing it is preferred over spending
    # it whenever a flagged, verified writable Codex launch is available.
    # ActorCapabilityPolicy/_ACTOR_CAPABILITY is unchanged: engine_capable
    # ("codex", "B", "build") still returns False, and the general fallback
    # loop below (engine_capable-based) still never picks Codex as a writer —
    # this is a separate branch, not a capability widening.
    if not read_only:
        _quota_allowed, _quota_reason = _quota_engine_allowance(engine, project)
        if not _quota_allowed:
            _codex_ok, _codex_reason = _quota_engine_allowance("codex", project)
            if engine == "glm" and _codex_builder_b_enabled(sl, tk) and _codex_ok:
                codex_meta = _ENGINE_META["codex"]
                return _finalize_choice(EngineChoice(
                    engine="codex",
                    model=codex_meta["model"],
                    cli=codex_meta["cli"],
                    role="builder",
                    spec_level=sl.value,
                    task_kind=tk.value,
                    reason=(f"{_quota_reason}; owner opt-in "
                           f"{ALLOW_CODEX_BUILDER_ENV}=1 → Codex B-level "
                           f"writable builder (spares Claude quota; verified "
                           f"workspace-write launch required at cli_args() "
                           f"time, never read-only for this role)"),
                    read_only=False,
                    effort="",
                ), excluded_engines, sl, tk)
            # Codex audit sc5:784 — a fallback that is ITSELF over quota would burn
            # exactly the window we are trying to spare. Verify the candidate's
            # projected allowance before switching; if it has no room either, keep
            # the original engine with a typed reason rather than switch blindly.
            fallback = None
            _fb_reason = ""
            _candidates = []
            if engine == "glm" and engine_capable("claude", sl, tk):
                _candidates = ["claude"]
            elif engine == "claude" and engine_capable("glm", sl, tk):
                _candidates = ["glm"]
            for _cand in _candidates:
                _cand_ok, _cand_why = _quota_engine_allowance(_cand, project)
                if _cand_ok:
                    fallback = _cand
                    break
                _fb_reason = _cand_why
            if fallback:
                fb_meta = _ENGINE_META[fallback]
                fb_effort = effort or ("high" if fallback == "claude" else effort)
                return _finalize_choice(EngineChoice(
                    engine=fallback,
                    model=fb_meta["model"],
                    cli=fb_meta["cli"],
                    role=fb_meta["role"],
                    spec_level=sl.value,
                    task_kind=tk.value,
                    reason=(f"{_quota_reason}; fell back to {fallback!r} "
                           f"(existing routing policy: reassign, never widen "
                           f"capability)"),
                    read_only=fb_meta["read_only"],
                    effort=fb_effort,
                ), excluded_engines, sl, tk)
            # Codex re-audit #2 (finding 9): the kept engine is NOT launched blindly —
            # dispatcher_v3._one_launch re-checks _quota_engine_allowance() at execve
            # time and turns a refusal into a typed QUOTA_CUT (no process created,
            # attempt waits for the window to reopen). Routing stays a decision;
            # the refusal lives in the spending process.
            reason = (f"{reason} [{_quota_reason}; no safe fallback with quota headroom "
                      f"under ActorCapabilityPolicy ({_fb_reason or 'none capable'}) "
                      f"— keeping {engine!r}; execve-time allowance check will refuse]")

    meta = _ENGINE_META[engine]

    # For claude engine, model depends on effort: xhigh → opus-5, else sonnet-5
    model = meta["model"]
    if engine == "claude" and effort == "xhigh":
        model = CLAUDE_OPUS_MODEL

    # builder != reviewer exclusion (CODEX-LOT-C2, 2026-09-12; made
    # unconditional across every return in CODEX_REVIEW_C2_C4 finding 4 —
    # see _finalize_choice()) — this is the routing-table/hint/owner-partial
    # path; the other branches above already ran their own EngineChoice
    # through the same _finalize_choice() before returning.
    return _finalize_choice(EngineChoice(
        engine=engine,
        model=model,
        cli=meta["cli"],
        role=role,
        spec_level=sl.value,
        task_kind=tk.value,
        reason=reason,
        read_only=read_only,
        effort=effort,
    ), excluded_engines, sl, tk)


class ReadOnlyLaunchNotImplemented(RuntimeError):
    """A read-only role was routed, but no VERIFIED restricted launch exists for it.

    Second CTO review: EngineChoice.read_only=True is METADATA until cli_args can
    build an argv whose restriction is verified against the local CLI. Returning
    an unrestricted argv would let a "read-only reviewer" write to the repo, so
    the honest behaviour is refusal.

    Current state (2026-09-11): Claude HAS a restricted read-only launch
    (`_restricted_claude_argv`), built only when every flag it relies on is
    verified against the local `claude` CLI (hard tool allowlist via --tools,
    write-tool denylist, bounded turns, pinned model, structured JSON, no
    permission bypass). GLM/opencode also has one now (`_restricted_glm_argv`),
    verified against `opencode run --help` plus a committed per-repo
    `.opencode/agent/<name>.md` permission denylist — but ONLY for a repo that
    actually carries that file; any other repo still raises this. Codex now
    has one too (`_restricted_codex_argv`, 2026-09-12), verified against
    `codex exec --help` for `-s read-only`, `--ephemeral`, `--json`, `-C`,
    `--skip-git-repo-check`, `-m`, `-o` — with no per-repo config to resolve,
    so the only remaining refusal path is the `codex` binary itself being
    absent or exposing a CLI this router cannot verify.
    """


class CodexBuilderNotImplemented(RuntimeError):
    """A writable Codex-B-builder role was routed (CODEX-LOT-C2, owner opt-in
    FACTORY_ALLOW_CODEX_BUILDER_B, 2026-09-12), but no VERIFIED
    `-s workspace-write` launch exists for it. Same posture as
    ReadOnlyLaunchNotImplemented: EngineChoice(engine="codex", read_only=False)
    is METADATA until cli_args() can build an argv verified against the local
    CLI (`codex exec --help` documents `-s`/`workspace-write`, `-C`,
    `--skip-git-repo-check`, `--ephemeral`, `--json`, `-o`). No unrestricted /
    unverified fallback exists here either.
    """


# ── restricted read-only Claude review launch (owner quota window) ──────────
#
# Owner asked for Claude as the reviewer while GLM builds. A reviewer must not
# write, so the launch is restricted AT THE ARGV LEVEL, not by convention:
#
#   --tools Read,Glob,Grep     hard allowlist of AVAILABLE tools (documented:
#                              "Specify the list of available tools from the
#                              built-in set") — no Bash/Edit/Write/Task, hence
#                              no shell, no file mutation, no subagents.
#   --disallowedTools ...      belt-and-braces deny of every write/execution
#                              tool, in case --tools semantics ever change.
#   --max-turns                bounded agentic turns (hidden from --help in
#                              some builds; verified by parse-probe below).
#   --output-format json       structured, machine-readable result.
#   --model <pinned>           the model route() selected, never a default.
#   --setting-sources ''       no project/user/local settings loaded — the
#                              reviewer sees only the argv-level allowlist,
#                              never ambient permissions or hooks.
#   --strict-mcp-config        MCP servers come ONLY from --mcp-config below.
#   --mcp-config '{}'          empty server set: no MCP tool surface at all.
#   --disable-slash-commands   no skills/slash-command execution.
#   NO --dangerously-skip-permissions: nothing is bypassed; -p is
#   non-interactive, so anything that would need approval simply cannot run.
#
# Every flag must be VERIFIED against the local CLI before this argv is built.
# Unknown or unverifiable flag semantics => typed refusal, never an
# unrestricted fallback.

REVIEW_TOOLS_ALLOWED = ("Read", "Glob", "Grep")
REVIEW_TOOLS_DENIED = (
    "Bash", "Edit", "MultiEdit", "Write", "NotebookEdit",
    "Task", "Agent", "WebFetch", "WebSearch", "TodoWrite",
    "SendUserMessage", "KillShell",
)
REVIEW_MAX_TURNS = "24"
# A writer used 86 turns / 93k output tokens on one observed milestone.  Bound the
# first attempt so one broad task cannot consume an entire subscription window.
# The runtime can continue from the durable commit or route a focused repair; it
# must never buy an unbounded conversation in one launch.
#
# Historical single-value cap, kept as the WRITER_MAX_TURNS constant for
# backward compatibility (existing callers/tests reference it directly, and it
# is what writer_max_turns() below returns when budget_s is unknown). Measured
# failure mode this constant alone could not address: a generously-budgeted
# mission (budget_s=5400) still got killed at 48 turns / 1252s / 11.9M
# cache-read tokens wasted on FULL_CODEBASE launches, while a short-budget
# mission could burn its whole window on a single launch. writer_max_turns()
# scales the cap with budget_s instead of using one number for every mission.
WRITER_MAX_TURNS = "48"

_WRITER_MAX_TURNS_BASE = 24
_WRITER_MAX_TURNS_THRESHOLD_S = 600
_WRITER_MAX_TURNS_STEP_S = 150
_WRITER_MAX_TURNS_CAP = 64


def writer_max_turns(spec_level: "SpecLevel | str", budget_s: "int | None" = None) -> str:
    """Bounded writer --max-turns, derived from the mission's budget_s.

    base 24 turns; +1 turn per 150s of budget_s beyond the first 600s;
    hard cap 64 regardless of budget_s (never a runaway launch).

    spec_level is validated/normalized here for API symmetry with route() and
    cli_args(), and to leave room for a future spec_level-differentiated
    policy; the current seed policy scales on budget_s alone, uniformly
    across S/A/B.

    budget_s=None (no budget known at the call site) returns the historical
    WRITER_MAX_TURNS constant unchanged. Every existing call site today does
    not pass budget_s, so this keeps prior behaviour byte-identical instead
    of silently reinterpreting "no budget given" as "smallest budget".
    """
    SpecLevel(spec_level)  # raises ValueError on unknown level; normalizes str/enum
    if budget_s is None:
        return WRITER_MAX_TURNS
    if budget_s <= _WRITER_MAX_TURNS_THRESHOLD_S:
        turns = _WRITER_MAX_TURNS_BASE
    else:
        bonus = int((budget_s - _WRITER_MAX_TURNS_THRESHOLD_S) // _WRITER_MAX_TURNS_STEP_S)
        turns = _WRITER_MAX_TURNS_BASE + bonus
    turns = min(turns, _WRITER_MAX_TURNS_CAP)
    return str(turns)

#: flags that must appear in `claude --help` for the restricted launch to exist
_REQUIRED_HELP_FLAGS = (
    "--print", "--model", "--output-format", "--tools",
    "--allowed", "--disallowed", "--effort",
    "--setting-sources", "--strict-mcp-config", "--mcp-config",
    "--disable-slash-commands",
)

_claude_probe_cache: "dict[str, str]" = {}


def _run_local(argv: list) -> "tuple[int, str]":
    import subprocess
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=20)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except Exception as exc:          # binary missing, timeout, ...
        return 127, f"cannot run {argv[0]!r}: {exc}"


def claude_restricted_flag_report(runner=_run_local) -> str:
    """Empty string when the local claude CLI verifies every flag the
    restricted read-only launch needs; otherwise a human-readable report of
    what is missing.  Costs no Claude quota: `claude --help` is local, and
    --max-turns is probed with an INVALID value, which fails argument
    parsing before any session or API call can start.
    """
    rc, help_text = runner(["claude", "--help"])
    if rc != 0 or not help_text:
        return f"claude --help unavailable (rc={rc})"
    missing = [f for f in _REQUIRED_HELP_FLAGS if f not in help_text]
    if missing:
        return ("claude --help does not document: " + ", ".join(missing))
    # --max-turns is hidden from --help in current builds; verify the option
    # itself parses by handing it a value it must reject. An unknown option
    # reports "unknown option"; a known one reports the invalid argument.
    rc, out = runner(["claude", "-p", "--max-turns", "notanumber", "x"])
    if "--max-turns" not in out or "invalid" not in out:
        return ("--max-turns cannot be verified against the local CLI "
                f"(probe rc={rc}, got: {out.strip()[:120]!r})")
    return ""


def _restricted_claude_argv(choice: EngineChoice, prompt: str) -> list[str]:
    """The verified read-only Claude argv, or raise. Never falls back."""
    if "report" not in _claude_probe_cache:
        _claude_probe_cache["report"] = claude_restricted_flag_report()
    missing = _claude_probe_cache["report"]
    if missing:
        raise ReadOnlyLaunchNotImplemented(
            f"READ_ONLY_LAUNCH_NOT_IMPLEMENTED: engine='claude' was routed as "
            f"a read-only {choice.role} ({choice.reason}), but the restricted "
            f"launch could not be verified against the local CLI ({missing}). "
            f"No unrestricted fallback exists by design: returning the builder "
            f"argv would hand a read-only reviewer write access to the repo.")
    if not choice.model:
        raise ReadOnlyLaunchNotImplemented(
            "READ_ONLY_LAUNCH_NOT_IMPLEMENTED: read-only claude review routed "
            "without a pinned model — a review launch may not fall back to a "
            "CLI session default model")
    return ["claude", "-p",
            "--output-format", "json",
            "--model", choice.model,
            "--max-turns", REVIEW_MAX_TURNS,
            "--tools", ",".join(REVIEW_TOOLS_ALLOWED),
            "--disallowedTools", ",".join(REVIEW_TOOLS_DENIED),
            "--setting-sources", "",
            "--strict-mcp-config",
            "--mcp-config", '{"mcpServers":{}}',
            "--disable-slash-commands",
            *([] if not choice.effort else ["--effort", choice.effort]),
            prompt]


# ── restricted read-only GLM/opencode review launch ──────────────────────────
#
# opencode has no --tools/--disallowedTools flag. Its restriction primitive is a
# per-agent permission map (`opencode agent create --help`, "Available:" line):
# bash, read, edit, glob, grep, webfetch, task, todowrite, websearch, lsp, skill.
# A committed `<repo>/.opencode/agent/<name>.md` with frontmatter
# `permission: {bash: deny, edit: deny, ...}` is discovered by opencode when the
# process cwd is <repo> (verified empirically: `opencode agent list` resolves a
# project-scoped agent's per-key deny AFTER the global allow defaults — deny wins).
# dispatcher_v3._run() already launches every engine with cwd=repo, so this file
# is reachable exactly where the real subprocess already runs. No unrestricted
# fallback exists here either: any verification failure raises
# ReadOnlyLaunchNotImplemented, same contract as the Claude path above.

GLM_REVIEW_AGENT = "factory-cto-reviewer"
_AGENT_RELPATH = (".opencode", "agent", f"{GLM_REVIEW_AGENT}.md")

# Product repos launched under review (e.g. projects/twitter-radar) do not all
# commit their own .opencode/agent/factory-cto-reviewer.md. This control repo
# (factory-foundation) does — factory/bin/model_router.py is two levels below
# its root — so a target repo missing the file falls back to the control
# repo's copy rather than refusing outright. Still fail-closed: if neither
# location has a verified agent file, the launch is refused exactly as before.
_CONTROL_ROOT = Path(__file__).resolve().parents[2]
#: import-time value; a test that patches `_CONTROL_ROOT` wins over the env
#: override below, so quota fixtures keep working while the real snapshot
#: never leaks into unrelated tests (tests/conftest.py sets the env).
_CONTROL_ROOT_DEFAULT = _CONTROL_ROOT
QUOTA_SNAPSHOT_ROOT_ENV = "FACTORY_QUOTA_SNAPSHOT_ROOT"

# Sentinel so the control_root default is read from the (patchable) module
# attribute at call time, not baked in as a stale value at def time.
_UNSET_CONTROL_ROOT = object()

_opencode_probe_cache: "dict[str, str]" = {}


def _opencode_write_capable_categories(runner=_run_local) -> "set[str] | None":
    """Write/execution-capable permission categories, parsed from the local CLI's
    own documentation rather than hard-coded — a category opencode adds later
    fails this probe (and therefore the launch) instead of silently escaping
    the denylist. Returns None if the categories cannot be determined."""
    rc, out = runner(["opencode", "agent", "create", "--help"])
    if rc != 0 or not out:
        return None
    marker = "Available:"
    idx = out.find(marker)
    if idx == -1:
        return None
    tail = out[idx + len(marker):]
    # The help text quotes the category list ("bash, read, ... skill"), wrapping
    # across lines — take exactly the quoted span, not the next stray "]" (that
    # belongs to yargs' trailing [string] type annotation, one line further on).
    start = tail.find('"')
    if start == -1:
        return None
    end = tail.find('"', start + 1)
    if end == -1:
        return None
    quoted = tail[start + 1:end].replace("\n", " ")
    categories = {c.strip().strip('"').strip("'") for c in quoted.split(",")}
    categories = {c for c in categories if c}
    read_only = {"read", "glob", "grep"}
    write_capable = categories - read_only
    return write_capable or None


def opencode_restricted_flag_report(
        repo: "Path | None", runner=_run_local,
        control_root: "Path | None" = _UNSET_CONTROL_ROOT) -> str:
    """Empty string when a verified restricted GLM/opencode review launch can be
    built for `repo`; otherwise a human-readable reason. Costs no GLM quota:
    `opencode --help`/`agent create --help` are local, and the agent file is
    parsed off disk.

    `control_root` is the fallback location for the agent config file when
    `repo` (the product repo under review) does not commit its own — see
    _CONTROL_ROOT. Pass None to disable the fallback (used by tests that need
    to prove the "missing from both" refusal)."""
    rc, help_text = runner(["opencode", "run", "--help"])
    if rc != 0 or not help_text:
        return f"opencode run --help unavailable (rc={rc})"
    missing = [f for f in ("--agent", "--dir", "--model", "--format")
              if f not in help_text]
    if missing:
        return "opencode run --help does not document: " + ", ".join(missing)
    write_capable = _opencode_write_capable_categories(runner)
    if not write_capable:
        return ("could not determine opencode's write-capable permission "
                "categories from 'opencode agent create --help' — refusing "
                "to trust an unverified denylist")
    if repo is None:
        return "no repo given to resolve the project-scoped agent config against"
    if control_root is _UNSET_CONTROL_ROOT:
        control_root = _CONTROL_ROOT
    agent_path = Path(repo).joinpath(*_AGENT_RELPATH)
    if not agent_path.is_file():
        control_agent_path = (Path(control_root).joinpath(*_AGENT_RELPATH)
                               if control_root is not None else None)
        if control_agent_path is not None and control_agent_path.is_file():
            agent_path = control_agent_path
        else:
            return f"restricted review agent config missing: {agent_path}"
    try:
        import yaml
        raw = agent_path.read_text(encoding="utf-8")
        if not raw.startswith("---"):
            return f"{agent_path}: no YAML frontmatter"
        end = raw.find("\n---", 3)
        if end == -1:
            return f"{agent_path}: unterminated YAML frontmatter"
        front = yaml.safe_load(raw[3:end]) or {}
    except Exception as exc:
        return f"{agent_path}: could not parse frontmatter ({exc})"
    if front.get("mode") != "primary":
        return (f"{agent_path}: mode must be 'primary' so `opencode run "
                f"--agent` cannot fall back to a writable default agent")
    perms = front.get("permission") or {}
    not_denied = sorted(c for c in write_capable if perms.get(c) != "deny")
    if not_denied:
        return (f"{agent_path}: permission map does not deny every "
                f"write-capable category — still open: {not_denied}")
    return ""


def _restricted_glm_argv(choice: EngineChoice, repo: Path, prompt: str) -> list[str]:
    """The verified read-only GLM/opencode argv, or raise. Never falls back."""
    cache_key = str(repo)
    if cache_key not in _opencode_probe_cache:
        _opencode_probe_cache[cache_key] = opencode_restricted_flag_report(repo)
    missing = _opencode_probe_cache[cache_key]
    if missing:
        raise ReadOnlyLaunchNotImplemented(
            f"READ_ONLY_LAUNCH_NOT_IMPLEMENTED: engine='glm' was routed as a "
            f"read-only {choice.role} ({choice.reason}), but the restricted "
            f"launch could not be verified for {repo} ({missing}). No "
            f"unrestricted fallback exists by design: returning the builder "
            f"argv would hand a read-only reviewer write access to the repo.")
    if not choice.model:
        raise ReadOnlyLaunchNotImplemented(
            "READ_ONLY_LAUNCH_NOT_IMPLEMENTED: read-only glm review routed "
            "without a pinned model — a review launch may not fall back to a "
            "CLI session default model")
    return ["opencode", "run", "--dir", str(repo),
            "--agent", GLM_REVIEW_AGENT,
            "--model", choice.model,
            "--format", "json",
            prompt]


# ── restricted read-only Codex review launch ─────────────────────────────────
#
# Verified locally against codex-cli 0.144.3 (`codex exec --help`):
#   -s read-only              codex's own sandbox mode: no filesystem writes,
#                              no shell escape, no network — the platform-level
#                              restriction, not a convention.
#   --ephemeral                keeps this run out of codex's own session/resume
#                              store: a reviewer round leaves nothing a later
#                              call could `codex resume` into a write.
#   --json                     "Print events to stdout as JSONL" — the only way
#                              codex emits a parseable event stream at all
#                              (`factory_v2.telemetry.adapters.detect_format`
#                              already requires `exec` + `--json` together).
#   -C <repo>                  scopes the sandbox to the repo under review,
#                              never the supervisor's own cwd.
#   --skip-git-repo-check      lets this run against a worktree/detached
#                              checkout, same posture as every other reviewer
#                              launch in this router.
#   -m <model>                 the model route() actually selected (CTO 002
#                              §4), never a CLI session default.
#   -o <out_file>              codex's own "last agent message" sink — a plain
#                              text copy of the final reply next to the --json
#                              event stream, so a caller that only wants the
#                              prose never has to re-derive it from JSONL.
# No `--dangerously-bypass-approvals-and-sandbox`, no
# `--dangerously-auto-approve-everything`, no interactive approval mode:
# nothing here is bypassed. `codex review` (a git-diff-scoped subcommand) was
# considered instead of `codex exec`: review/plan task_kinds need a general
# "inspect this repo state and answer" launch, not a diff-bound one, and
# `codex exec --json` is what `detect_format`/`final_text` in
# telemetry/adapters.py already parse — `codex review` would need a second,
# undocumented event shape verified from scratch for no capability this
# router's callers (review_dispatch, audit_loop.run_model_reviewer) need.
#
# Every flag must be VERIFIED against the local CLI before this argv is
# built. Unknown/unverifiable flag semantics => typed refusal, same contract
# as Claude/GLM above: no unrestricted fallback exists here either.

_REQUIRED_CODEX_HELP_FLAGS = (
    "--json", "-s", "--ephemeral", "-C", "--skip-git-repo-check", "-m", "-o",
)

_codex_probe_cache: "dict[str, str]" = {}


def codex_restricted_flag_report(runner=_run_local) -> str:
    """Empty string when the local `codex` CLI verifies every flag the
    restricted read-only launch needs; otherwise a human-readable report of
    what is missing (including the binary being absent at all: `_run_local`
    reports that as rc=127). Costs no Codex quota: `codex exec --help` is
    local and never talks to a model, exactly like
    claude_restricted_flag_report()/opencode_restricted_flag_report() above.
    """
    rc, help_text = runner(["codex", "exec", "--help"])
    if rc != 0 or not help_text:
        return f"codex exec --help unavailable (rc={rc})"
    missing = [f for f in _REQUIRED_CODEX_HELP_FLAGS if f not in help_text]
    if missing:
        return "codex exec --help does not document: " + ", ".join(missing)
    return ""


def _git_info_exclude_path(repo: Path) -> "Path | None":
    """Best-effort resolve `<repo's common .git dir>/info/exclude`, or None
    when `repo` is not (verifiably, cheaply) a git working tree.

    `repo` here is almost always a `git worktree add` checkout (this factory
    dispatches every mission into its own worktree), where `.git` is a FILE
    containing `gitdir: <path>/.git/worktrees/<name>`, not a directory — a
    naive `repo / ".git" / "info" / "exclude"` would silently write to a path
    that is never read by git for that worktree. `info/exclude` itself is
    stored once in the COMMON dir shared by every worktree (resolved via the
    worktree gitdir's own `commondir` file), which is fine: it is a per-repo
    ignore rule, not a per-worktree one.

    No `git` subprocess is shelled out here — this only ever reads two small
    plain-text files git itself writes in a fixed, documented layout — so
    this stays as cheap and dependency-free as the rest of this module's
    local-CLI probing.
    """
    try:
        git_path = repo / ".git"
        if git_path.is_dir():
            return git_path / "info" / "exclude"
        if git_path.is_file():
            content = git_path.read_text(encoding="utf-8").strip()
            if not content.startswith("gitdir:"):
                return None
            gitdir = Path(content.split(":", 1)[1].strip())
            if not gitdir.is_absolute():
                gitdir = (repo / gitdir).resolve()
            commondir_file = gitdir / "commondir"
            if commondir_file.is_file():
                common = commondir_file.read_text(encoding="utf-8").strip()
                common_path = Path(common)
                if not common_path.is_absolute():
                    common_path = (gitdir / common_path).resolve()
                return common_path / "info" / "exclude"
            return gitdir / "info" / "exclude"
    except OSError:
        return None
    return None


def _ensure_git_excluded(repo: Path, pattern: str) -> None:
    """Best-effort: add `pattern` as a line in `<repo>`'s `.git/info/exclude`
    if not already present. `.git/info/exclude` (not a tracked `.gitignore`)
    is the right mechanism for this: it is local-only, never committed, never
    part of any mission's diff — a tracked `.gitignore` edit would itself be
    an unrelated change landing in every mission that ever writes a Codex
    builder output file. Never raises: a `repo` this module cannot verify is
    a git working tree (e.g. a plain tmp dir in a unit test) just skips this,
    the same fail-soft posture cli_args() already has for anything that is
    housekeeping rather than the launch contract itself.
    """
    exclude_path = _git_info_exclude_path(repo)
    if exclude_path is None:
        return
    try:
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
        if pattern in existing.splitlines():
            return
        with exclude_path.open("a", encoding="utf-8") as fh:
            if existing and not existing.endswith("\n"):
                fh.write("\n")
            fh.write(pattern + "\n")
    except OSError:
        pass


_CODEX_OUT_DIRNAME = ".factory/codex_out"


def _codex_confined_out_path(repo: Path, prefix: str) -> str:
    """A fresh `-o` output-sink path UNDER `repo`, for a Codex launch whose
    confinement is supposed to mean something (the writable B-builder).

    CODEX_REVIEW_C2_C4 finding 2: `-o` names a file Codex writes its final
    message to — a SEPARATE argument from `-s`/`-C`, not constrained by
    codex's own sandbox value. Building it via bare `tempfile.mkstemp()`
    (the historical behaviour) resolves to system `/tmp`, so a "confined to
    the mission worktree" builder still wrote one output file outside that
    worktree — exactly the contradiction Codex's own review flagged.

    The directory is created 0700 (owner-only: this sink can carry build
    prose/diagnostics, and a mission worktree may sit under a shared
    /tmp-like root on a multi-tenant box) and registered in the repo's
    `.git/info/exclude` (see _ensure_git_excluded()) so it never shows up as
    an untracked-file surprise in the mission's own diff.
    """
    out_dir = repo / _CODEX_OUT_DIRNAME
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        _os.chmod(out_dir, 0o700)
    except OSError:
        pass
    _ensure_git_excluded(repo, _CODEX_OUT_DIRNAME + "/")
    import tempfile
    out_fd, out_path = tempfile.mkstemp(prefix=prefix, suffix=".md", dir=str(out_dir))
    _os.close(out_fd)
    return out_path


def _restricted_codex_argv(choice: EngineChoice, repo: Path, prompt: str) -> list[str]:
    """The verified read-only Codex argv, or raise. Never falls back.

    `-o` here is intentionally NOT routed through _codex_confined_out_path():
    this launch is `-s read-only` — it writes nothing to `repo` by
    construction (that is the entire point of the restricted reviewer
    launch), so there is no confinement claim for an out-of-repo `-o` sink to
    contradict, unlike the writable B-builder launch below. Kept under the
    system temp dir exactly as before.
    """
    if "report" not in _codex_probe_cache:
        _codex_probe_cache["report"] = codex_restricted_flag_report()
    missing = _codex_probe_cache["report"]
    if missing:
        raise ReadOnlyLaunchNotImplemented(
            f"READ_ONLY_LAUNCH_NOT_IMPLEMENTED: engine='codex' was routed as "
            f"a read-only {choice.role} ({choice.reason}), but the restricted "
            f"launch could not be verified against the local CLI ({missing}). "
            f"No unrestricted fallback exists by design.")
    if not choice.model:
        raise ReadOnlyLaunchNotImplemented(
            "READ_ONLY_LAUNCH_NOT_IMPLEMENTED: read-only codex review routed "
            "without a pinned model — a review launch may not fall back to a "
            "CLI session default model")
    import tempfile
    out_fd, out_path = tempfile.mkstemp(prefix="codex-review-", suffix=".txt")
    _os.close(out_fd)
    return ["codex", "exec", "--json",
            "-s", "read-only",
            "--ephemeral",
            "-C", str(repo),
            "--skip-git-repo-check",
            "-m", choice.model,
            "-c", f'model_reasoning_effort="{CODEX_REASONING.get(choice.model, "medium")}"',
            "-o", out_path,
            prompt]


# ── writable Codex-B-builder launch (CODEX-LOT-C2, owner opt-in, 2026-09-12) ─
#
# route() only ever returns this EngineChoice (engine="codex", read_only=False,
# role="builder") when FACTORY_ALLOW_CODEX_BUILDER_B=1, spec_level=="B" and
# task_kind in {build, repair}, and only after GLM (the default B builder) was
# quota-refused — see _codex_builder_b_enabled()/ALLOW_CODEX_BUILDER_ENV
# above. The launch itself is verified the same way as every other restricted
# argv in this module (`codex exec --help`, no quota spent), the only
# difference from the read-only reviewer launch being the sandbox value:
#
#   -s workspace-write   codex's own sandbox mode: filesystem writes ARE
#                        allowed, but confined to the directories `-C`/
#                        `--add-dir` name — never `-s read-only`, never
#                        `--dangerously-bypass-approvals-and-sandbox`.
#   -C <repo>            confines those writes to the mission's own isolated
#                        worktree, never the supervisor's cwd.
#   --skip-git-repo-check / --ephemeral / --json / -o <out>  same meaning and
#                        same verification requirement as the read-only launch
#                        above — a worktree/detached checkout is fine, no
#                        session persists, output is machine-readable, and the
#                        final message is also written to a plain-text sink.
#
# budget/timeout: unlike Claude's --max-turns, codex exec has no turn-budget
# flag (verified: absent from `codex exec --help`) — the mission's budget_s is
# already enforced the same way for every engine, by dispatcher_v3._run()'s
# subprocess timeout, so nothing engine-specific is needed here.

_REQUIRED_CODEX_WRITE_HELP_TOKENS = (
    "--json", "-s", "workspace-write", "-C", "--skip-git-repo-check",
    "--ephemeral", "-o",
)

_codex_write_probe_cache: "dict[str, str]" = {}


def codex_writable_flag_report(runner=_run_local) -> str:
    """Empty string when the local `codex` CLI verifies every flag AND the
    `workspace-write` sandbox value the writable Codex-B-builder launch needs;
    otherwise a human-readable report of what is missing. Same no-quota-spent
    contract as codex_restricted_flag_report(): `codex exec --help` only,
    never a real launch.
    """
    rc, help_text = runner(["codex", "exec", "--help"])
    if rc != 0 or not help_text:
        return f"codex exec --help unavailable (rc={rc})"
    missing = [f for f in _REQUIRED_CODEX_WRITE_HELP_TOKENS if f not in help_text]
    if missing:
        return "codex exec --help does not document: " + ", ".join(missing)
    return ""


def _codex_builder_argv(choice: EngineChoice, repo: Path, prompt: str) -> list[str]:
    """The verified writable (`-s workspace-write`) Codex-B-builder argv, or
    raise CodexBuilderNotImplemented. Never falls back to an unverified or
    read-only launch.

    `-o` is built via _codex_confined_out_path(repo, ...) — CODEX_REVIEW_C2_C4
    finding 2 — so the one file this launch is told to write outside its own
    prompt/diff (the final-message sink) lands under the SAME mission
    worktree `-C`/`-s workspace-write` already confine every other write to,
    instead of system `/tmp`. Unlike the read-only reviewer launch below,
    this one actually claims confinement, so its `-o` has to honour it.
    """
    if "report" not in _codex_write_probe_cache:
        _codex_write_probe_cache["report"] = codex_writable_flag_report()
    missing = _codex_write_probe_cache["report"]
    if missing:
        raise CodexBuilderNotImplemented(
            f"CODEX_BUILDER_NOT_IMPLEMENTED: engine='codex' was routed as a "
            f"writable {choice.role} ({choice.reason}), but the workspace-write "
            f"launch could not be verified against the local CLI ({missing}). "
            f"No unverified fallback exists by design.")
    out_path = _codex_confined_out_path(repo, "codex-builder-")
    # 2026-09-18: explicit model + reasoning effort (was: the CLI config default).
    # 16:55 Bangkok: a mission worktree's `.git` is a FILE pointing at the main
    # repo's .git/worktrees/<name> — outside the sandbox — so Codex could not
    # create index.lock and "committed" nothing (audit repair, 14 min lost).
    # `--add-dir <git-common-dir>` makes the shared git dir writable.
    _extra = []
    try:
        _gd = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=str(repo),
                             capture_output=True, text=True, timeout=10).stdout.strip()
        if _gd:
            _gdp = Path(_gd) if Path(_gd).is_absolute() else (Path(repo) / _gd)
            _gdp = _gdp.resolve()
            if not str(_gdp).startswith(str(Path(repo).resolve()) + "/"):
                _extra = ["--add-dir", str(_gdp)]
    except Exception:
        _extra = []
    return ["codex", "exec", "--json",
            "-s", "workspace-write",
            "-C", str(repo),
            "--skip-git-repo-check",
            "--ephemeral",
            "-m", choice.model,
            "-c", f'model_reasoning_effort="{CODEX_REASONING.get(choice.model, "medium")}"',
            *_extra,
            "-o", out_path,
            prompt]


def cli_args(choice: EngineChoice, repo: Path, prompt: str,
             budget_s: "int | None" = None,
             resume_session: "str | None" = None) -> list[str]:
    """Construct the subprocess argv for a given EngineChoice.

    budget_s: the mission's remaining/total budget in seconds, if known. Threaded into
    writer_max_turns() to scale the writer's --max-turns cap. Default None preserves the
    historical fixed WRITER_MAX_TURNS behaviour byte-for-byte — no existing call site
    passes budget_s today, so nothing changes silently until a caller opts in.

    resume_session: CTO_LENS_RELIABILITY §1 lever 2. When set, adds `--resume
    <resume_session>` to the unrestricted Claude writer argv ONLY — a session hit
    error_max_turns is resumable there, not a re-read from zero. No other engine
    has an equivalent flag in this router, and the restricted (read-only) Claude
    review launch is untouched: a reviewer session is bounded and single-shot by
    design. Default None preserves every existing argv byte-for-byte.

    GLM/Z Code (writer) → opencode run --dir <repo> --model <model> --format json <prompt>
    GLM/Z Code (read-only) → restricted review argv (see _restricted_glm_argv):
                          verified per-agent permission denylist, no unrestricted fallback
    Claude (writer) → claude -p --output-format stream-json --verbose --model <model>
                          [--effort <effort>] --dangerously-skip-permissions <prompt>
    Claude (read-only) → restricted review argv (see _restricted_claude_argv):
                          hard tool allowlist, bounded turns, no bypass
    Codex      → codex exec --json --model <model> <prompt>  (reviewer, no --dir)

    The model and effort from EngineChoice are passed explicitly so the actual
    subprocess uses the model/effort chosen by route(), not the CLI session default.
    (CTO 002 section 4: argv must contain the model actually selected.)

    EVERY engine here now requests a machine-readable event stream. That is a
    deliberate property of this function and not an incidental flag: what the
    supervisor can measure about a launch is decided entirely by the bytes built
    here, so an engine launched without a structured mode is unmeasurable by
    construction, whatever the rest of the accounting does.
    factory_v2/telemetry/inventory.py probes this function directly, so removing
    one of these flags turns the coverage inventory red rather than going unnoticed.

    Consumers of the resulting stdout must read it through
    factory_v2.telemetry.adapters.final_text() when they want the agent's prose;
    raw stdout is now events, not a message.
    """
    # A read_only choice may NOT be launched through an unrestricted CLI. Claude
    # has a restricted launch whose flags are verified against the local CLI;
    # glm (opencode) has none, so it refuses. Unknown flags also refuse — there
    # is never an unrestricted fallback for a read-only role.
    if choice.read_only and choice.engine == "claude":
        return _restricted_claude_argv(choice, prompt)
    if choice.read_only and choice.engine == "glm":
        return _restricted_glm_argv(choice, repo, prompt)
    if choice.read_only and choice.engine == "codex":
        return _restricted_codex_argv(choice, repo, prompt)
    if choice.engine == "glm":
        # `--format json` makes opencode emit its `step_finish` parts, which
        # carry input/output/reasoning/cache token counts per request. Without
        # it opencode prints prose and every GLM launch is unmeasurable — that
        # was our argv, not a provider limit. Same binary, same model, same
        # session semantics; only the output encoding changes.
        return ["opencode", "run", "--dir", str(repo), "--model", choice.model,
                "--format", "json", prompt]
    if choice.engine == "claude":
        # LOT C4 (2026-09-12): switched from `--output-format json` to
        # `--output-format stream-json --verbose` (the CLI requires
        # `--verbose` for stream-json under -p). This is where
        # `rate_limit_event` and the typed terminal `result` event actually
        # live — plain `json` never emits a rate_limit_event at all, so
        # mid-session quota was unobservable under it.
        # dispatcher_v3._extract_resumable_session and
        # factory_v2.telemetry.adapters.{detect_format,parse_claude_stream_json}
        # already read this shape (format is derived from argv, so the switch
        # alone re-routes them); the coordinated fixture migration landed in
        # the same change (tests/test_p0_routing_budget_integration.py). A
        # stream that ends without its terminal `result` event is now caught
        # fail-closed by dispatcher_v3._stream_truncation_verdict rather than
        # silently losing its usage/session_id.
        cmd = ["claude", "-p", "--output-format", "stream-json", "--verbose",
               "--model", choice.model,
               "--max-turns", writer_max_turns(choice.spec_level, budget_s),
               "--dangerously-skip-permissions"]
        if resume_session:
            cmd += ["--resume", resume_session]
        if choice.effort:
            cmd += ["--effort", choice.effort]
        cmd.append(prompt)
        return cmd
    if choice.engine == "codex" and choice.role == "builder":
        # CODEX-LOT-C2 (2026-09-12): the only way route() hands back a
        # non-read-only Codex EngineChoice today — the writable B-builder
        # fallback under FACTORY_ALLOW_CODEX_BUILDER_B. `-s workspace-write`,
        # never `-s read-only`; see _codex_builder_argv() for the full
        # verification contract.
        return _codex_builder_argv(choice, repo, prompt)
    if choice.engine == "codex":
        # Three changes, all required for this launch to be observable at all:
        #   `exec`     bare `codex <prompt>` forwards to the INTERACTIVE CLI,
        #              which is the wrong mode for an automated dispatch and has
        #              no event stream; `exec` is the documented non-interactive
        #              entry point.
        #   `--json`   "Print events to stdout as JSONL" — the only way codex
        #              emits token_count and rate_limits.
        #   `--model`  argv must name the model actually selected (CTO 002 §4).
        #              CODEX_MODEL is already this engine's declared model, so
        #              this states the existing choice rather than changing it,
        #              and makes model_from_argv() able to observe it.
        return ["codex", "exec", "--json", "--model", choice.model, prompt]
    raise ValueError(f"unknown canonical engine: {choice.engine!r}")



# argv0 -> canonical engine id.  This is the ONLY honest source of engine identity:
# it names the binary the OS actually executed, not the engine route() asked for.
_ARGV0_ENGINE = {"opencode": "glm", "claude": "claude", "codex": "codex"}


def engine_from_argv(argv: list[str]) -> str:
    """Canonical engine id derived from the argv actually handed to the OS.

    Returns "" when argv0 is not a known engine binary — callers must treat that
    as "engine identity unobserved" and fail closed, never fall back to a hint.
    """
    if not argv:
        return ""
    import os as _os
    return _ARGV0_ENGINE.get(_os.path.basename(argv[0]), "")


def model_from_argv(argv: list[str]) -> str:
    """Model name as it appears in the executed argv (`--model X`, `-m X`,
    `--model=X`), else "". L3-015 (relecture 2026-09): Codex launches use
    `-m`; the old `--model`-only lookup returned "" for every Codex argv.
    One parser (factory_v2.shared_parsers) — the prompt token is never read
    as an option."""
    try:
        from factory_v2.shared_parsers import model_from_argv as _shared
    except ImportError:  # launched directly from factory/bin: repo root not on sys.path yet
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from factory_v2.shared_parsers import model_from_argv as _shared
    return _shared(argv)

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--spec-level", required=True, choices=[s.value for s in SpecLevel])
    ap.add_argument("--task-kind", required=True, choices=[t.value for t in TaskKind])
    ap.add_argument("--stall-count", type=int, default=0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    choice = route(args.spec_level, args.task_kind, args.stall_count)

    if args.json:
        print(json.dumps(asdict(choice), indent=2, ensure_ascii=False))
        return 0

    rw = "read-only" if choice.read_only else "writer"
    effort_str = f"  effort={choice.effort}" if choice.effort else ""
    print(f"engine={choice.engine}  model={choice.model}  role={choice.role}  [{rw}]{effort_str}")
    print(f"reason: {choice.reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
