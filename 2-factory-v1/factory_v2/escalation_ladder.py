"""G3 — Deterministic escalation ladder. UNBOUNDED_EQUIVALENT_RETRY = 0.

F5R5: Escalation stages now CAUSE executable RecoveryWork — not just recorded.

Two retries are "equivalent" when the full identity tuple matches:
    (mission_id, engine, strategy_sha, failure_fingerprint, context_hash, authority_hash)

If the same identity has been attempted >= _MAX_EQUIVALENT before, escalation is required.
Escalation is deterministic and persistent — restart must not reset the ladder.

Ladder:
  attempt 1-3: normal retry
  attempt 4:   ALTERNATE_ENGINE — rotate engine, resume retry
  attempt 5:   DIAGNOSE — create RecoveryWork(DIAGNOSE) → execute via normal pipeline
  attempt 6:   REPLAN — create RecoveryWork(REPLAN) → execute via normal pipeline
  attempt 7+:  SAFETY_STOP — write durable receipt, hard stop

RecoveryWork is stored as a durable JSON file in <project_dir>/evidence/recovery/.
It is NOT injected into ROADMAP.yaml — ONE canonical authority is preserved.
RecoveryWork travels through the same compile→route→execute→validate→settle pipeline.

Escalation stage is persisted in PROJECT_STATE.json under:
  state["escalation_ladder"][identity_key] = {
      "stage": "...",
      "attempts": n,
      "last_ts": "...",
  }

Restart recovery: advance() reloads PROJECT_STATE.json fresh on every call,
so escalation stage survives factoryd restarts with no in-memory state.
"""
from __future__ import annotations

import re

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path

from .project_authority import atomic_write_json
from factory_v2._io import _now

_MAX_EQUIVALENT = 3          # AI failures before first escalation
_INFRA_ROTATION_THRESHOLD = 2  # consecutive infra failures before forcing engine rotation


class EscalationStage(Enum):
    RETRY = "retry"
    ALTERNATE_ENGINE = "alternate_engine"
    DIAGNOSE = "diagnose"
    REPLAN = "replan"
    SAFETY_STOP = "safety_stop"


def control_code_sha() -> str:
    """Material SHA of the control-plane/executor code actually running.

    Empty when unobservable, never a guess: an unknown SHA shares the legacy
    identity, so it can only make the ladder stricter.
    """
    if "sha" in _CODE_SHA:
        return _CODE_SHA["sha"]
    import subprocess
    try:
        root = Path(__file__).resolve().parent.parent
        r = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=10)
        _CODE_SHA["sha"] = r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        _CODE_SHA["sha"] = ""
    return _CODE_SHA["sha"]


def _identity_key(mission_id: str, engine: str, strategy_sha: str,
                  failure_fingerprint: str, context_hash: str, authority_hash: str,
                  code_sha: str = "") -> str:
    """Identity of an equivalent retry.

    code_sha is part of it. Without it, failures produced by the OLD executor and
    failures produced by the FIXED one share an identity and their attempts
    accumulate — which is how three pre-fix failures pushed a milestone all the way
    to REPLAN, and the REPLAN then weakened the Roadmap to change the identity of
    the failure rather than fix its cause.

    The corresponding ABUSE (a doc commit buying a fresh ladder) is not closed
    here but at the supersession layer: re-opening a ladder identity requires
    `mark_superseded_by_code_change`, which is only invoked with a proof-backed
    code change (see runtime re-entry path), while the SHA-insensitive
    COST_NO_PROGRESS brake (resource_accounting) caps the spend no matter how
    many fresh ladder identities a loop walks through.

    Old records are NOT deleted: they keep their own key and stay visible as
    history. They simply describe a different system.
    """
    payload = json.dumps({
        "mission_id": mission_id,
        "engine": engine,
        "strategy_sha": strategy_sha,
        "failure_fingerprint": failure_fingerprint,
        "context_hash": context_hash,
        "authority_hash": authority_hash,
        "control_code_sha": code_sha,
    }, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def get_escalation_stage(state: dict, mission_id: str, engine: str, strategy_sha: str,
                          failure_fingerprint: str, context_hash: str,
                          authority_hash: str,
                          code_sha: str | None = None) -> tuple[EscalationStage, int]:
    """Return (stage, attempt_count) for this identity. Does NOT mutate state."""
    key = _identity_key(mission_id, engine, strategy_sha, failure_fingerprint,
                        context_hash, authority_hash,
                        control_code_sha() if code_sha is None else code_sha)
    record = (state.get("escalation_ladder") or {}).get(key, {})
    attempts = record.get("attempts", 0)
    return (_stage_for(attempts), attempts)


def record_attempt(state: dict, mission_id: str, engine: str, strategy_sha: str,
                    failure_fingerprint: str, context_hash: str,
                    authority_hash: str, code_sha: str | None = None) -> dict:
    """Increment attempt count for this identity. Returns updated state (caller must save)."""
    sha = control_code_sha() if code_sha is None else code_sha
    key = _identity_key(mission_id, engine, strategy_sha, failure_fingerprint,
                        context_hash, authority_hash, sha)
    ladder = state.setdefault("escalation_ladder", {})
    rec = ladder.get(key, {"attempts": 0, "stage": EscalationStage.RETRY.value})
    new_attempts = rec["attempts"] + 1
    new_stage = _stage_for(new_attempts)
    # Settlement projection metadata is idempotence memory. A later attempt for
    # the same identity advances counters but must never erase that memory.
    ladder[key] = {
        **rec,
        "attempts": new_attempts,
        "stage": new_stage.value,
        "mission_id": mission_id,
        "engine": engine,
        "control_code_sha": sha,
        "last_ts": _now(),
    }
    return state


def _stage_for(attempts: int) -> EscalationStage:
    if attempts <= _MAX_EQUIVALENT:
        return EscalationStage.RETRY
    elif attempts == _MAX_EQUIVALENT + 1:
        return EscalationStage.ALTERNATE_ENGINE
    elif attempts == _MAX_EQUIVALENT + 2:
        return EscalationStage.DIAGNOSE
    elif attempts == _MAX_EQUIVALENT + 3:
        return EscalationStage.REPLAN
    else:
        return EscalationStage.SAFETY_STOP


def mark_superseded_by_code_change(state: dict, current_code_sha: str = "") -> int:
    """Type ladder records produced by OTHER control-plane code.

    They are kept and stay visible — deleting them would erase the history of what
    was actually attempted. They are typed so nothing counts them against the
    current code, and so routing and learning can exclude them with a reason.
    """
    sha = current_code_sha or control_code_sha()
    if not sha:
        return 0
    n = 0
    for rec in (state.get("escalation_ladder") or {}).values():
        if not isinstance(rec, dict):
            continue
        rec_sha = str(rec.get("control_code_sha") or "")
        if not rec_sha:
            # Unknown provenance: it is NOT superseded. Superseding it would stop
            # it counting against the current code on nothing but an absence,
            # which is fail-open. It keeps its own identity anyway — an empty
            # code_sha hashes differently from any real one — so it can never
            # block the fixed code either.
            continue
        if rec_sha != sha and not rec.get("superseded_reason"):
            rec["superseded_reason"] = "SUPERSEDED_BY_CODE_CHANGE"
            rec["superseded_at_code_sha"] = sha
            n += 1
    return n


def is_equivalent_retry_cap_reached(state: dict, mission_id: str, engine: str,
                                     strategy_sha: str, failure_fingerprint: str,
                                     context_hash: str, authority_hash: str,
                                     code_sha: str | None = None) -> bool:
    """True if this exact identity has reached the equivalent-retry cap (must escalate)."""
    _, attempts = get_escalation_stage(state, mission_id, engine, strategy_sha,
                                        failure_fingerprint, context_hash,
                                        authority_hash, code_sha)
    return attempts >= _MAX_EQUIVALENT


# ---------------------------------------------------------------------------
# F5R5: RecoveryWork — durable, executable, ONE CANONICAL AUTHORITY preserved
# ---------------------------------------------------------------------------

# Delta CTO 002: GLM = Z Code. Rotation is ["glm", "claude"] — no zcode entry.
# B-level: try glm first → rotate to claude (A/S always claude).
_ALTERNATE_ENGINES = ["glm", "claude"]

# Engine capability policy (must stay in sync with model_router._ACTOR_CAPABILITY).
# zcode removed: it normalises to glm. All state/metrics use canonical engine id.
# Call normalize_engine_id() before recording engine in state or escalation ladder.
_ENGINE_SPEC_LEVELS: dict[str, set[str]] = {
    "claude": {"S", "A", "B"},
    "glm":    {"B"},
    "local":  {"S", "A", "B"},  # deterministic command, not an AI — all levels ok
}

# Consecutive infrastructure failures before an engine is live-quarantined.
_CODE_SHA: dict = {}

_QUARANTINE_THRESHOLD = 3

# Infrastructure failure markers — these are control-plane/infra failures, NOT AI failures.
# Must not advance the escalation ladder (faux attempt protection).
_INFRA_FAILURE_PREFIXES = (
    "EXECUTOR_ERROR",
    "COMMAND_NOT_FOUND",
    "WRONG_REPO_DISPATCH",
    "TIMEOUT",
)

# Shell/environment failure markers — these can appear anywhere in the leading
# slice of output_tail (not just at the start), unlike _INFRA_FAILURE_PREFIXES.
# Real shell errors (missing binary, unset required env var, bad interpreter)
# are control-plane problems, not AI-produced failures, and must not advance
# the escalation ladder either.
_INFRA_FAILURE_MARKERS = (
    "bash: line",
    ": command not found",
    "must point at",
    "unbound variable",
)

# "No such file or directory" alone is NOT an infra marker: an ordinary Python
# FileNotFoundError from AI-written product code carries the same words, and
# classifying it as infra would silently stop the escalation ladder on a real
# AI failure. Only the SHELL form (the launcher itself could not find a
# script/binary) is infra — recognised by the shell's own prefix on that line.
_INFRA_SHELL_NOT_FOUND_RE = re.compile(
    r"(?m)^(?:[\w./-]*(?:bash|sh|zsh|dash)(?:: line \d+)?|/bin/[\w-]+|/usr/bin/env)"
    r": .*No such file or directory"
)

# How many leading characters of output_tail to scan for _INFRA_FAILURE_MARKERS.
_INFRA_MARKER_SCAN_WINDOW = 500


def is_infrastructure_failure(rc: int, output_tail: str) -> bool:
    """True if this failure is infra (timeout/executor error), not an AI-produced failure.

    Infrastructure failures must NOT advance the escalation ladder — doing so
    would waste escalation budget on control-plane problems the AI has no hand in.
    """
    if rc == 124:  # canonical timeout exit code
        return True
    stripped = output_tail.lstrip()
    snippet = stripped[:200]
    if any(snippet.startswith(p) for p in _INFRA_FAILURE_PREFIXES):
        return True
    window = stripped[:_INFRA_MARKER_SCAN_WINDOW]
    if any(marker in window for marker in _INFRA_FAILURE_MARKERS):
        return True
    return _INFRA_SHELL_NOT_FOUND_RE.search(window) is not None


def engine_capable(engine: str, spec_level: str) -> bool:
    """True if engine is declared capable of the given spec_level.

    Fail-closed: an engine NOT in _ENGINE_SPEC_LEVELS is treated as incapable
    (unknown capability = cannot be trusted). Add new engines to _ENGINE_SPEC_LEVELS
    explicitly — implicit capability grants are banned (ActorCapabilityPolicy).
    """
    levels = _ENGINE_SPEC_LEVELS.get(engine)
    if levels is None:
        return False  # fail-closed: unknown engine is never assumed capable
    return spec_level in levels


def quarantine_engine(state: dict, engine: str, reason: str) -> dict:
    """Increment the consecutive infra-failure count for engine.

    When the count reaches _QUARANTINE_THRESHOLD, the engine is marked quarantined
    and will be skipped by next_alternate_engine().
    """
    q = state.setdefault("engine_quarantine", {})
    entry = q.get(engine, {"consecutive_infra_failures": 0, "quarantined": False})
    entry["consecutive_infra_failures"] = entry.get("consecutive_infra_failures", 0) + 1
    if entry["consecutive_infra_failures"] >= _QUARANTINE_THRESHOLD:
        entry["quarantined"] = True
        entry["quarantine_reason"] = reason
        entry["quarantined_at"] = _now()
    q[engine] = entry
    return state


def clear_infra_failure_count(state: dict, engine: str) -> dict:
    """Reset the consecutive infra-failure count for engine on successful dispatch."""
    if engine in state.get("engine_quarantine", {}):
        state["engine_quarantine"][engine]["consecutive_infra_failures"] = 0
        # Do NOT lift quarantine automatically — requires manual or REPLAN action.
    return state


def is_engine_quarantined(state: dict, engine: str) -> bool:
    """True if the engine has been live-quarantined due to repeated infra failures."""
    return state.get("engine_quarantine", {}).get(engine, {}).get("quarantined", False)


def infra_rotation_needed(state: dict, engine: str) -> bool:
    """True if the engine has hit the infra-failure rotation threshold.

    Unlike quarantine (which persists), this triggers a one-shot rotation:
    the engine is tried again after it has been rotated away and back.
    Prevents infinite infra-retry loops where the same engine is redispatched
    indefinitely because infra failures don't advance the escalation ladder.
    """
    count = state.get("engine_quarantine", {}).get(engine, {}).get(
        "consecutive_infra_failures", 0)
    return count >= _INFRA_ROTATION_THRESHOLD


@dataclass
class RecoveryWork:
    """Durable record of pending recovery work for a failed mission.

    Persisted to <project_dir>/evidence/recovery/<recovery_id>.json.
    NOT injected into ROADMAP.yaml — canonical authority is unchanged.
    Travels through the same compile→execute→validate→settle pipeline as normal missions.
    """
    recovery_id: str
    kind: str              # DIAGNOSE | REPLAN | SAFE_REPAIR
    parent_mission_id: str
    milestone_id: str
    failure_fingerprint: str
    escalation_stage: str
    attempt_count: int
    created_at: str
    engine: str = ""
    #: ABSOLUTE, normalised path to the generated task .md. Absolute because a
    #: cross-repo recovery runs its worker in the PRODUCT repo, where a path
    #: relative to the control repo resolves against the wrong root.
    task_file: str = ""
    #: The control repo the task file belongs to, recorded explicitly rather than
    #: rediscovered from the cwd at dispatch time.
    authority_root: str = ""
    #: Same file expressed inside authority_root — kept for readability and
    #: portability, never used to resolve by trial.
    task_file_relative: str = ""
    #: Control-plane code that produced this recovery. A failure recorded under
    #: other code is history, not a verdict on the current one.
    control_code_sha: str = field(default_factory=control_code_sha)
    identity_key: str = "" # escalation ladder key for this recovery's parent identity
    # R3/R4: the DIAGNOSE this work descends from, so a SAFE_REPAIR can read its
    # diagnosis artifact and so a milestone cannot accumulate duplicate repair work.
    parent_recovery_id: str = ""
    # R3: dedup key = milestone + contract signature + failure fingerprint.  Recovery
    # work is created at most once per distinct blocking condition.
    dedup_key: str = ""
    # X5: which live repair contract this work belongs to.  Recovery is selected by
    # this, not by kind, so an old DIAGNOSE from an unrelated quarantine cannot
    # authorise scheduling a SAFE_REPAIR for the current one.
    idempotency_key: str = ""
    # True when this work exists because authority could not yet define the proofs —
    # its job is to make the contract derivable, not to repair the product.
    underivable: bool = False
    # Which attempt of its stage this record is (1-based). A real dataclass FIELD,
    # because to_dict() is asdict(): an attribute set with setattr() would be
    # silently dropped at serialisation and the retry count would be unreadable
    # from disk — which is precisely the durable evidence the cap must count.
    stage_attempt: int = 1
    completed: bool = False
    completed_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "RecoveryWork":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def _recovery_dir(project_dir: Path) -> Path:
    d = Path(project_dir) / "evidence" / "recovery"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_recovery_work(project_dir: Path, work: RecoveryWork) -> Path:
    """Persist RecoveryWork atomically. Returns path to written file."""
    rdir = _recovery_dir(project_dir)
    path = rdir / f"{work.recovery_id}.json"
    # R1: settlement/recovery writes need the same durability as mission receipts —
    # a rename that is not fsynced can be lost even when the bytes are on disk.
    atomic_write_json(path, work.to_dict())
    return path


def _recovery_is_stale(project_dir: Path, d: dict) -> str:
    """A pending recovery whose failure fingerprint no longer matches its parent
    mission's current fingerprint diagnoses a cause that is already gone
    (2026-09-15: four diagnoses of a task-file heading fixed the evening before).
    Returns the reason, or "" when the work is still relevant."""
    try:
        import json as _json
        state = _json.loads((Path(project_dir) / "PROJECT_STATE.json").read_text(encoding="utf-8"))
        rec = (state.get("missions") or {}).get(d.get("parent_mission_id") or "", {})
    except Exception:
        return ""
    if not rec:
        return ""
    current = str(rec.get("last_failure_fingerprint") or "")
    mine = str(d.get("failure_fingerprint") or "")
    if mine and current and current != mine:
        return f"STALE_FINGERPRINT: recovery for {mine[:60]!r}, mission now at {current[:60]!r}"
    return ""


class RecoveryLedgerUnknown(RuntimeError):
    """A recovery record EXISTS but cannot be read or decoded. Pending recovery is
    UNKNOWN — never "nothing pending". FCO_SYNTH-003: the reader used to skip the
    record silently, so a corrupt DIAGNOSE/REPLAN marker let the milestone be
    dispatched as if its repair ladder had never fired."""


def read_pending_recovery_work(project_dir: Path,
                               milestone_id: str | None = None) -> "RecoveryWork | None":
    """Return the oldest non-completed RecoveryWork, or None if none pending.
    Stale items (cause already gone) are closed durably on the way, never run.
    Raises RecoveryLedgerUnknown on an unreadable/corrupt/misshapen record."""
    rdir = Path(project_dir) / "evidence" / "recovery"
    if not rdir.exists():
        return None
    # The same directory also contains output artifacts produced by recovery work.
    # They are evidence consumed by later stages, not RecoveryWork ledger records.
    candidates = [
        p for p in sorted(rdir.glob("*.json"))
        if not p.stem.endswith(("_diagnosis", "_replan"))
    ]
    for p in candidates:
        try:
            d = json.loads(p.read_text())
        except (OSError, ValueError) as exc:
            raise RecoveryLedgerUnknown(f"{p.name}: unreadable recovery record: {exc}") from exc
        if not isinstance(d, dict):
            raise RecoveryLedgerUnknown(f"{p.name}: recovery record is not an object")
        if (not d.get("completed")
                and (milestone_id is None or d.get("milestone_id") == milestone_id)):
            try:
                return RecoveryWork.from_dict(d)
            except Exception as exc:
                raise RecoveryLedgerUnknown(f"{p.name}: malformed recovery record: {exc}") from exc
    return None


def supersede_pending_recovery_work(project_dir: Path,
                                    current_code_sha: str = "") -> list[str]:
    """Close recovery work created by an older committed control/authority tree.

    Recovery is an instruction derived from the failure observed at
    ``control_code_sha``.  Replaying it after the committed tree changes spends a
    model call diagnosing a condition that may no longer exist.  Keep the record as
    history, but type it SUPERSEDED before pending-work selection.  An empty legacy
    SHA remains pending: absence of provenance is never treated as proof of drift.
    """
    sha = current_code_sha or control_code_sha()
    if not sha:
        return []
    rdir = Path(project_dir) / "evidence" / "recovery"
    if not rdir.exists():
        return []
    superseded: list[str] = []
    now = _now()
    for path in sorted(rdir.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except Exception:
            continue
        if data.get("completed") or data.get("superseded_reason"):
            continue
        recorded = str(data.get("control_code_sha") or "")
        if not recorded or recorded == sha:
            continue
        data["completed"] = True
        data["completed_at"] = now
        data["outcome"] = "SUPERSEDED"
        data["superseded_reason"] = "SUPERSEDED_BY_COMMITTED_TREE_CHANGE"
        data["superseded_from_code_sha"] = recorded
        data["superseded_at_code_sha"] = sha
        atomic_write_json(path, data)
        superseded.append(str(data.get("recovery_id") or path.stem))
    return superseded


SETTLEMENT_LEDGER_SCHEMA = "applied-settlement-ids/v1"


def migrate_settlement_ledger(project_dir: Path, state: dict) -> tuple[dict, int]:
    """R2.1/X4: one-time, crash-safe upgrade from `last_settlement_id` to a durable set.

    The old code stored a single `last_settlement_id` and re-applied every completed
    settlement it saw on every boot, so the first upgraded boot took an entry from 7 to
    9.  Marking everything applied fixes that but commits the opposite error: a
    settlement written completed just before a crash — never projected — would be
    silently swallowed, losing a recovery bump for good.

    Neither guess is acceptable, so each record is classified by what durable evidence
    actually proves:

      last_settlement_id itself     -> APPLIED.  The old reconciler recorded it, which
                                       is exactly what that field meant.
      completed_at <= entry.last_ts -> APPLIED.  last_ts is when the ladder was last
                                       projected, so a record already completed by then
                                       was seen and applied in that pass.
      completed_at > entry.last_ts  -> OUTSTANDING.  It was written after the last
                                       projection, so it was never counted.  Left
                                       unapplied so normal reconciliation bumps it
                                       exactly once — this is the crashed settlement.
      no usable timestamps          -> AMBIGUOUS.  Neither direction is provable, so it
                                       is neither applied nor bumped; it gets a typed
                                       reconciliation edge and reconciliation is blocked
                                       from guessing at it.

    Ambiguity is therefore never guessed away in either direction: nothing is silently
    marked applied, nothing is silently discarded, and attempts are never incremented
    here.

    Idempotent: re-running recomputes the same classification from the same evidence, so
    a SIGKILL between writes costs at most a repeat.

    Returns (state, number_of_entries_migrated).
    """
    ladder = state.get("escalation_ladder") or {}
    if not ladder:
        state.setdefault("_settlement_ledger_schema", SETTLEMENT_LEDGER_SCHEMA)
        return state, 0

    # Durable projection evidence: completed settlements grouped by identity, with the
    # timestamps that let us order them against last_settlement_id.
    by_identity: dict[str, list[dict]] = {}
    rdir = Path(project_dir) / "evidence" / "recovery"
    if rdir.exists():
        for f in sorted(rdir.glob("*.json")):
            try:
                d = json.loads(f.read_text())
            except Exception:
                continue
            if not d.get("completed"):
                continue
            sid, ikey = d.get("settlement_id"), d.get("identity_key")
            if sid and ikey:
                by_identity.setdefault(ikey, []).append(
                    {"sid": sid, "completed_at": d.get("completed_at", ""),
                     "recovery_id": d.get("recovery_id", "")})

    migrated = 0
    pending = state.setdefault("_settlement_reconciliation", {})
    for ikey, entry in ladder.items():
        if not isinstance(entry, dict) or "applied_settlement_ids" in entry:
            continue
        last = entry.get("last_settlement_id")
        records = by_identity.get(ikey, [])
        applied: set = set()
        ambiguous: list[dict] = []

        if last:
            applied.add(last)
            last_ts = str(entry.get("last_ts") or "")
            for r in records:
                if r["sid"] == last:
                    continue
                if not last_ts or not r["completed_at"]:
                    ambiguous.append(r)             # nothing to order it against
                elif r["completed_at"] <= last_ts:
                    applied.add(r["sid"])           # completed before the last projection
                # else: completed after the last projection -> outstanding, leave
                # unapplied so reconcile_prepared_settlements bumps it exactly once.
        else:
            # No projection pass ever ran for this identity, so nothing is applied and
            # normal reconciliation will bump these exactly once (the crash-recovery
            # path this whole mechanism exists for).
            pass

        entry["applied_settlement_ids"] = sorted(applied)
        entry["_ledger_migrated_from"] = "last_settlement_id" if last else "no_prior_projection"
        for r in ambiguous:
            key = f"{ikey}:{r['sid']}"
            pending.setdefault(key, {
                "identity_key": ikey, "settlement_id": r["sid"],
                "recovery_id": r["recovery_id"],
                "completed_at": r["completed_at"],
                "reason": ("completed at or after last_settlement_id — cannot prove it "
                           "was projected before the upgrade"),
                "state": "AWAITING_RECONCILIATION",
                "created_at": _now(),
            })
        migrated += 1

    state["_settlement_ledger_schema"] = SETTLEMENT_LEDGER_SCHEMA
    if not pending:
        state.pop("_settlement_reconciliation", None)
    return state, migrated


def reconcile_prepared_settlements(project_dir: Path, state: dict) -> tuple[dict, int]:
    """D1-R1: On-boot reconciliation of prepared-but-not-projected settlements.

    A crash between Write-1 (recovery file: completed=True + settlement_id) and
    Write-2 (state save with ladder bump) leaves the recovery record complete but
    the ladder un-bumped.  read_pending_recovery_work() skips completed records,
    so the factory never retries them — the ladder stays frozen.

    This function scans all recovery files.  For each file with completed=True and
    a settlement_id, if the ladder entry for identity_key does not yet carry that
    settlement_id as last_settlement_id, the bump is re-applied idempotently.

    Called at the start of advance() BEFORE pending-work selection.  Callers must
    persist the returned state with save_project_state() if reconciled > 0.

    Returns (updated_state, number_of_bumps_applied).
    """
    rdir = Path(project_dir) / "evidence" / "recovery"
    if not rdir.exists():
        return state, 0

    ladder = state.setdefault("escalation_ladder", {})
    reconciled = 0

    for p in sorted(rdir.glob("*.json")):
        try:
            d = json.loads(p.read_text())
        except Exception:
            continue
        if not d.get("completed"):
            continue
        sid = d.get("settlement_id", "")
        if not sid:
            continue
        ikey = d.get("identity_key", "")
        if not ikey:
            continue
        if ikey not in ladder:
            continue
        existing = ladder[ikey]
        # F2: use a durable set so multiple historical settlements for the
        # same identity are each applied exactly once regardless of boot order.
        applied = set(existing.get("applied_settlement_ids", []))
        if sid in applied:
            continue  # already projected — idempotent
        # X4: a settlement the migration could not classify must not be guessed at
        # here either.  It carries a typed reconciliation edge instead of a bump.
        if f"{ikey}:{sid}" in (state.get("_settlement_reconciliation") or {}):
            continue
        applied.add(sid)
        # Re-apply the ladder bump that was lost in the crash.
        new_n = existing.get("attempts", 0) + 1
        ladder[ikey] = {
            **existing,
            "attempts": new_n,
            "stage": _stage_for(new_n).value,
            "last_ts": _now(),
            "last_settlement_id": sid,
            "applied_settlement_ids": sorted(applied),
            "_reconciled_on_boot": True,
        }
        reconciled += 1

    return state, reconciled


def mark_recovery_complete(project_dir: Path, recovery_id: str) -> None:
    """Mark RecoveryWork as completed (not deleted — preserves audit trail).

    Prefer settle_recovery_work_with_ladder() for new call sites — it is atomic,
    idempotent, and bumps the escalation ladder in a single operation.
    This function is kept for callers that do not hold the ladder state.
    """
    rdir = _recovery_dir(project_dir)
    path = rdir / f"{recovery_id}.json"
    try:
        d = json.loads(path.read_text())
        d["completed"] = True
        d["completed_at"] = _now()
        atomic_write_json(path, d)
    except Exception:
        pass


def supersede_recovery_by_valid_product(project_dir: Path, recovery_id: str,
                                        product_sha: str) -> bool:
    """Close stale gate recovery after the product independently validates."""
    path = _recovery_dir(project_dir) / f"{recovery_id}.json"
    try:
        data = json.loads(path.read_text())
        if data.get("completed"):
            return False
        data["completed"] = True
        data["completed_at"] = _now()
        data["outcome"] = "SUPERSEDED"
        data["superseded_reason"] = "SUPERSEDED_BY_INDEPENDENTLY_VALID_PRODUCT_HEAD"
        data["superseded_at_product_sha"] = product_sha
        atomic_write_json(path, data)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# F3: Failure classification — only semantic failures consume the ladder stage
# ---------------------------------------------------------------------------

class FailureClass(Enum):
    SEMANTIC = "semantic"             # AI produced wrong/missing artifact
    TRANSIENT_PROVIDER = "transient_provider"  # 5xx / rate-limit from provider
    PROCESS_KILLED = "process_killed"  # SIGKILL / SIGTERM of subprocess
    TIMEOUT = "timeout"              # wall-clock timeout (rc=124)
    UNKNOWN = "unknown"              # unclassified


# Exit codes that indicate the subprocess was killed externally.
_KILLED_EXIT_CODES: frozenset[int] = frozenset({-9, -15, 137, 143})

# Output patterns that indicate a transient provider-side failure.
_TRANSIENT_PATTERNS: tuple[str, ...] = (
    "503 ", "502 ", "504 ", "429 ", "rate limit", "Rate limit",
    "quota exceeded", "Quota exceeded", "overloaded", "server error",
)


def classify_recovery_failure(rc: int, output_tail: str) -> FailureClass:
    """Classify a recovery-work failure for escalation policy.

    Only SEMANTIC failures consume the escalation ladder stage.
    TRANSIENT failures are retried (bounded) without advancing the cursor.
    """
    if rc == 124:
        return FailureClass.TIMEOUT
    if rc in _KILLED_EXIT_CODES:
        return FailureClass.PROCESS_KILLED
    snippet = (output_tail or "").lstrip()[:400]
    if any(p in snippet for p in _TRANSIENT_PATTERNS):
        return FailureClass.TRANSIENT_PROVIDER
    return FailureClass.SEMANTIC


# Maximum transient retries before we consume the stage anyway.
_MAX_TRANSIENT_RETRIES = 2


# ---------------------------------------------------------------------------
# F2: Atomic, idempotent recovery settlement with ladder bump
# ---------------------------------------------------------------------------

def settle_recovery_work_with_ladder(
    project_dir: Path,
    recovery: "RecoveryWork",
    state: dict,
    *,
    success: bool,
    failure_class: "FailureClass | None" = None,
) -> dict:
    """Mark RecoveryWork complete and bump the escalation ladder atomically.

    Idempotency contract:
    - If the RecoveryWork file already carries a ``settlement_id`` and the ladder
      already records that same id, the call is a no-op (safe to replay after crash).
    - If the file is marked completed but the ladder bump is missing (crash between
      the two writes), the bump is re-applied using the stored settlement_id.

    Failure classification:
    - TRANSIENT failures (PROCESS_KILLED, TIMEOUT, TRANSIENT_PROVIDER) do NOT
      consume the escalation stage unless ``_MAX_TRANSIENT_RETRIES`` is exceeded.
    - SEMANTIC failures always consume the stage.

    Returns the updated ``state`` dict (caller must persist with save_project_state).
    """
    import uuid as _uuid

    rdir = _recovery_dir(project_dir)
    path = rdir / f"{recovery.recovery_id}.json"

    # Read the persisted record for idempotency.
    try:
        on_disk: dict = json.loads(path.read_text())
    except Exception:
        on_disk = {}

    # Reuse an existing settlement_id so replays are no-ops.
    sid: str = on_disk.get("settlement_id") or str(_uuid.uuid4())
    already_completed = on_disk.get("completed", False)

    if not already_completed:
        # Classify: should this failure consume the ladder stage?
        transient = (
            not success
            and failure_class is not None
            and failure_class in (FailureClass.PROCESS_KILLED, FailureClass.TIMEOUT,
                                  FailureClass.TRANSIENT_PROVIDER)
        )

        transient_count = on_disk.get("transient_retry_count", 0)
        if transient and transient_count < _MAX_TRANSIENT_RETRIES:
            # Soft failure: increment counter, leave recovery open for retry.
            new_data = {
                **on_disk,
                "transient_retry_count": transient_count + 1,
                "last_failure_class": failure_class.value,
            }
            atomic_write_json(path, new_data)
            return state  # ladder NOT consumed

        # Hard failure (semantic or transient limit exhausted) OR success.
        # Mark the record complete in one atomic write.
        new_data = {
            **on_disk,
            "completed": True,
            "settlement_id": sid,
            "settlement_success": success,
            "failure_class": (failure_class.value if failure_class else None),
            "completed_at": _now(),
        }
        atomic_write_json(path, new_data)

    # Bump the escalation ladder (idempotent via settlement_id CAS).
    ladder = state.setdefault("escalation_ladder", {})
    ikey = recovery.identity_key
    if not ikey:
        # D1-R2: Legacy record without identity_key — enter durable SafetyQuarantine
        # so dispatch is blocked until autonomous repair resolves the ladder entry.
        # A typed _safety_events append alone is insufficient (does not block dispatch).
        try:
            from .safety_quarantine import enter_quarantine as _enter_quarantine
            _enter_quarantine(
                project_dir,
                cause="LEGACY_RECOVERY_MISSING_IDENTITY_KEY",
                mission_id=recovery.parent_mission_id,
                milestone_id=recovery.milestone_id,
                failure_fingerprint=recovery.failure_fingerprint,
            )
        except Exception:
            pass  # quarantine write failure must not mask the original event
        state.setdefault("_safety_events", []).append({
            "kind": "LEGACY_RECOVERY_MISSING_IDENTITY_KEY",
            "recovery_id": recovery.recovery_id,
            "ts": _now(),
            "action": "SAFETY_QUARANTINE_ENTERED — dispatch blocked; autonomous migration required",
        })
        return state

    if ikey not in ladder:
        return state  # ladder entry was cleared (e.g. REPLAN already reset it)

    existing = ladder[ikey]
    # F2: durable set so two completed settlements for the same identity never
    # double-bump, regardless of boot order or how many times advance() is called.
    applied = set(existing.get("applied_settlement_ids", []))
    if sid in applied:
        return state  # already applied — idempotent replay

    applied.add(sid)
    new_n = existing.get("attempts", 0) + 1
    ladder[ikey] = {
        **existing,
        "attempts": new_n,
        "stage": _stage_for(new_n).value,
        "last_ts": _now(),
        "last_settlement_id": sid,
        "applied_settlement_ids": sorted(applied),
    }
    return state


def next_alternate_engine(
    current_engine: str,
    spec_level: str = "B",
    state: "dict | None" = None,
    project: "str | None" = None,
) -> str:
    """Return the next engine in rotation for ALTERNATE_ENGINE stage.

    Skips:
    - Engines incapable of the required spec_level (engine capability policy)
    - Engines marked quarantined due to repeated infrastructure failures (live quarantine)

    ADAPTIVE (learning-loop consumer): among capable, non-quarantined candidates,
    prefers the engine with the higher VALIDATED acceptance rate observed in the
    durable runrecords ledger — this is the consumer of `record_settlement(
    validated=True)`, which until now was written and never read. An engine with
    too few observed attempts (< _MIN_SAMPLE) stays neutral (0.5) so a fresh
    engine is never buried by an empty history; an engine at 0 validated
    successes over enough attempts is tried last. Deterministic: rate desc,
    then fixed rotation order.

    `project` scopes the learning to the rotating project's OWN validated
    history (engine_stats(project=...)): a project with no history of its own
    is neutral for every candidate and falls back to the fixed rotation —
    foreign projects' outcomes never steer this project's rotation.

    If no capable, non-quarantined engine is found, returns current_engine as fallback
    (caller's SAFETY_STOP path handles the dead-end case).
    """
    try:
        start_idx = _ALTERNATE_ENGINES.index(current_engine)
    except ValueError:
        start_idx = -1
    rotation = [_ALTERNATE_ENGINES[(start_idx + offset) % len(_ALTERNATE_ENGINES)]
                for offset in range(1, len(_ALTERNATE_ENGINES) + 1)]
    # A rotation never "rotates" to the engine it started from: the current
    # engine is excluded from the candidates and returned only when nothing
    # else is eligible.
    eligible = [c for c in rotation if c != current_engine
                and engine_capable(c, spec_level)
                and not (state is not None and is_engine_quarantined(state, c))]
    if not eligible:
        return current_engine  # all alternatives blocked — escalation path handles this
    ranked = sorted(eligible, key=lambda c: (-_validated_rate(c, project), rotation.index(c)))
    return ranked[0]


# Neutral rate for engines without enough observed history: never buried, never
# favoured — the fixed rotation decides between two equally-unknown engines.
_MIN_SAMPLE = 3
_NEUTRAL_RATE = 0.5


def _validated_rate(engine: str, project: "str | None" = None) -> float:
    """Validated acceptance rate for `engine` from the durable runrecords ledger.

    Read-only and fail-open: an unreadable or absent ledger must never break
    dispatch — the fallback is the neutral rate, i.e. the historical rotation.

    `project` scopes the rate to that project's own attempts (foreign projects
    never steer this one's rotation); None keeps the global, cross-project
    reading for back-compat direct callers.
    """
    try:
        from . import runrecords
        bucket = (runrecords.engine_stats(project=project) or {}).get(engine) or {}
        attempts = bucket.get("attempts") or 0
        accepted = bucket.get("accepted") or 0
        if attempts < _MIN_SAMPLE:
            return _NEUTRAL_RATE
        return accepted / attempts
    except Exception:
        return _NEUTRAL_RATE


def generate_diagnose_task(project_dir: Path, recovery: RecoveryWork) -> Path:
    """Generate a DIAGNOSE task .md in the recovery evidence dir.

    The task asks an AI to diagnose why the mission keeps failing and propose
    a targeted fix without modifying the canonical ROADMAP.yaml.
    Returns path to the generated task file (relative to project_dir parent).
    """
    rdir = _recovery_dir(project_dir)
    task_path = rdir / f"DIAGNOSE_{recovery.recovery_id}.md"
    content = f"""# DIAGNOSE Recovery Task — {recovery.parent_mission_id}

## Context

This is an autonomous diagnostic mission generated by the Factory escalation ladder.

**Parent mission**: {recovery.parent_mission_id}
**Milestone**: {recovery.milestone_id}
**Escalation stage**: {recovery.escalation_stage}
**Equivalent attempt count**: {recovery.attempt_count}
**Failure fingerprint**: {recovery.failure_fingerprint[:200]}
**Created**: {recovery.created_at}

## Objective

Diagnose why mission `{recovery.parent_mission_id}` keeps failing with the same failure fingerprint.
Produce a diagnosis report that identifies the root cause and proposes a targeted fix.

Do NOT modify ROADMAP.yaml or PROJECT_STATE.json.
Do NOT produce a general solution — focus on the specific failure fingerprint above.

## Deliverable

Read-only launch: do NOT write any file. Answer with exactly one JSON object containing
`root_cause`, `proposed_fix`, `confidence` (HIGH | MEDIUM | LOW) and `reasoning`; the
dispatcher writes `evidence/recovery/{recovery.recovery_id}_diagnosis.json` from that answer.

## Acceptance

`evidence/recovery/{recovery.recovery_id}_diagnosis.json` (written by the dispatcher) carries root_cause and proposed_fix.
"""
    task_path.write_text(content)
    return task_path


def generate_safe_repair_task(project_dir: Path, recovery: RecoveryWork,
                             contract: dict | None = None) -> Path:
    """Generate a SAFE_REPAIR task .md — the concrete repair a DIAGNOSE asked for.

    R4: quarantine used to have DIAGNOSE as its only recovery kind, so a diagnosed
    failure had nowhere to go.  SAFE_REPAIR is already in ALLOWED_ACTIONS; this gives
    it a producer.  Acceptance is not "write a report": it is the frozen contract's own
    reproduction command going green, run by the executor, so the repair cannot be
    declared done by describing it.
    """
    contract = contract or {}
    rdir = _recovery_dir(project_dir)
    task_path = rdir / f"SAFE_REPAIR_{recovery.recovery_id}.md"
    diagnosis_path = rdir / f"{recovery.parent_recovery_id or recovery.recovery_id}_diagnosis.json"
    diagnosis_hint = ""
    try:
        d = json.loads(diagnosis_path.read_text())
        diagnosis_hint = (
            f"\n**Diagnosed root cause**: {d.get('root_cause', '')}\n"
            f"**Proposed fix**: {d.get('proposed_fix', '')}\n"
        )
    except Exception:
        diagnosis_hint = "\n(no diagnosis artifact available — diagnose from the fingerprint)\n"

    content = f"""# SAFE_REPAIR Recovery Task — {recovery.parent_mission_id}

## Context

Autonomous repair mission generated while the project is in SAFETY_QUARANTINE.
Only repair work is permitted; product dispatch stays blocked until this succeeds.

**Parent mission**: {recovery.parent_mission_id}
**Milestone**: {recovery.milestone_id}
**Failure fingerprint**: {recovery.failure_fingerprint[:300]}
**Quarantine cause**: {contract.get('cause', recovery.escalation_stage)}
{diagnosis_hint}
## Frozen repair contract

You do not get to choose what "repaired" means. It is frozen:

- reproduction (must go from FAIL to PASS): `{contract.get('reproduction_command', '')}`
- relevant regressions (must stay PASS):     `{contract.get('regression_command', '')}`
- independent validation (must PASS):        `{contract.get('validation_command', '')}`
- run from: `{contract.get('command_cwd', '')}`
- pre-repair SHA: `{contract.get('pre_repair_sha', '')}`

## Objective

Make the reproduction command pass, without breaking the regression command, by
committing a real change in the repository above.

Do NOT modify STRATEGY.md, SPEC.md, ROADMAP.yaml or PROJECT_STATE.json.
Do NOT weaken, skip or rewrite the verification commands — they are the contract.
Do NOT edit SAFETY_QUARANTINE.json.

## Acceptance

`{contract.get('reproduction_command', '')}` exits 0 when run in
`{contract.get('command_cwd', '')}`, and the change is committed so the repair has a
real commit SHA descended from the pre-repair SHA.
"""
    task_path.write_text(content)
    return task_path


def generate_replan_task(project_dir: Path, recovery: RecoveryWork) -> Path:
    """Generate a REPLAN task .md in the recovery evidence dir.

    The task asks for a DERIVED EXECUTION PLAN, not a ROADMAP mutation. It used to
    ask for the mutation, and the runtime CAS-wrote it; the owner directive of
    2026-09-10 revoked that capability, so a task still asking for a roadmap patch
    would spend the ladder's last recovery stage producing something that can only
    be refused. See factory_v2/derived_plan.py for the allowed change set.

    Returns path to the generated task file.
    """
    from .derived_plan import ALLOWED_CHANGE_KEYS, SCHEMA as _PLAN_SCHEMA

    rdir = _recovery_dir(project_dir)
    task_path = rdir / f"REPLAN_{recovery.recovery_id}.md"
    content = f"""# REPLAN Recovery Task — {recovery.milestone_id}

## Context

This is an autonomous replanning mission generated by the Factory escalation ladder.
Diagnosis has not resolved the failure. A different EXECUTION PLAN is needed for the
same, unchanged, owner-authored objective.

**Failing milestone**: {recovery.milestone_id}
**Equivalent attempt count**: {recovery.attempt_count}
**Failure fingerprint**: {recovery.failure_fingerprint[:200]}
**Created**: {recovery.created_at}

## Objective

Propose a DERIVED EXECUTION PLAN for `{recovery.milestone_id}`.

STRATEGY.md, SPEC.md and ROADMAP.yaml express owner intent. The Factory executes
them and never rewrites them. A proposal that changes the goal, the acceptance
criteria, the product_e2e command, the required artifacts, the evaluator or the
budget is REFUSED — and changing the shape of the failure so the retry counter
starts again is the specific abuse this rule exists to stop.

You may change only: {sorted(ALLOWED_CHANGE_KEYS)}

- `engine`: a different engine from the known rotation.
- `step_order`: a PERMUTATION of the compiled mission ids. Same steps, new order —
  dropping or inventing a step changes the work, not the plan.

That is the whole list, because that is exactly what the dispatcher consumes. A plan
that changes nothing the dispatcher reads is refused as DERIVED_PLAN_NO_OP: it would
settle this recovery as a success for no change.

## Deliverable

Write `evidence/recovery/{recovery.recovery_id}_replan.json` containing a
`derived_plan` object:

```json
{{
  "derived_plan": {{
    "schema": "{_PLAN_SCHEMA}",
    "milestone_id": "{recovery.milestone_id}",
    "mission_id": "{recovery.parent_mission_id}",
    "failure_fingerprint": "<the EXACT fingerprint above, whole, not truncated>",
    "contract_id": "<the frozen contract id in force>",
    "authority_hashes": {{"strategy_sha": "...", "spec_sha": "...", "roadmap_sha": "..."}},
    "changes": {{"engine": "..."}},
    "rationale": "why this execution change addresses the fingerprint",
    "supersedes": "<plan_id of the active derived plan, or \\"\\" if none>"
  }},
  "rationale": "...",
  "confidence": "HIGH | MEDIUM | LOW"
}}
```

The plan is refused unless `contract_id`, all three authority hashes, `mission_id`
and the WHOLE `failure_fingerprint` match the position actually in force, so read
them from the current state — do not guess. A plan written for a different failure
cannot drive this one.

## Acceptance

File `evidence/recovery/{recovery.recovery_id}_replan.json` exists and carries a
`derived_plan` object that validates against the position in force.
"""
    task_path.write_text(content)
    return task_path
