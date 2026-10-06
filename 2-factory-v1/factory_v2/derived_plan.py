"""Derived execution plans — what a REPLAN is actually allowed to change.

A REPLAN used to propose a ROADMAP mutation, and `_apply_replan_patch()` CAS-wrote
it. The owner directive of 2026-09-10 revoked that: STRATEGY, SPEC and ROADMAP are
owner intent, the Factory executes them. But revoking the write left REPLAN with no
implementation at all — the last rung of the escalation ladder was consumed by a
stage that could only refuse, so a stuck mission spent its final recovery budget
changing nothing and landed on SAFETY_STOP.

This module is the missing half. A REPLAN produces a DERIVED PLAN: a durable,
validated, milestone-scoped record that changes only how the already-authorised work
is executed. It is a DERIVED artifact in the exact sense mission_compiler.py already
uses: DERIVED_ARTIFACT_OVERRIDES_CANONICAL_ROADMAP = 0.

The allowed change set is exactly what production consumes
-----------------------------------------------------------
`engine` and `step_order`, and nothing else. An earlier draft of this module also
accepted `max_attempts` and `task_file`, which no consumer read — so a plan carrying
only those reported DERIVED_PLAN_APPLIED, settled the recovery as a success, and
changed nothing about the next execution. That is the same no-op-that-reports-success
defect this whole file exists to remove, one level down. A change key is admissible
here only once something reads it; until then it is refused by name.

Why each refusal exists, stated once so the rules are not re-litigated later:

  the missing step-set rule
      The production REPLAN that motivated the revocation replaced a `${VAR:?...}`
      with `${VAR:-$(git rev-parse --show-toplevel)}`. It supplied no missing
      environment: it changed the IDENTITY of the failure so the retry counter
      started again. A plan that may drop steps can do the same thing one level
      down. So `step_order` must be a PERMUTATION of the compiled mission ids.

  the no-op rule
      A plan whose step order equals the compiled order and whose engine equals the
      one already in force changes nothing, so accepting it would settle a recovery
      as successful for free. Refused as DERIVED_PLAN_NO_OP.

  the binding rule
      A plan is bound to (project, milestone, frozen contract, three authority
      hashes, the interrupted mission id, and that mission's EXACT failure
      fingerprint). The fingerprint is stored whole and compared whole: a truncated
      or merely-similar fingerprint would let an unrelated old incident drive a new
      execution under the same contract. Both the producer and the consumer check
      it, so a plan cannot be written for one failure and obeyed for another.

  corruption is not absence
      An unreadable plan file is reported as unreadable. Mapping it to "no plan"
      would let a corrupt active plan be silently overwritten by the next writer.

Consumption is deterministic: (compiled missions + plan) -> ordered missions, with
no second orchestrator and no new dispatch path.

Concurrency: the CAS below is compare-and-swap against the plan id on disk, not a
lock. It makes a lost update VISIBLE to the loser; it does not by itself serialise
two racing writers. In production there is exactly one writer per project — the
runtime holds the project lock across a cycle (see `advance`), and REPLAN only ever
runs inside one — so the CAS is a correctness backstop for crash/replay, which is
where it actually earns its place: replaying an already-persisted identical plan
succeeds idempotently instead of failing a recovery that already did its work.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from factory_v2._io import _now

SCHEMA = "derived-execution-plan/v1"

#: Subdirectory under the project dir. Runtime state, never authority.
PLAN_DIR = "derived_plans"

#: The complete set of things a derived plan may express — and every one of them has
#: a production consumer in `_consume_derived_plan`. Adding a key here without a
#: consumer re-creates the no-op defect described in the module docstring.
ALLOWED_CHANGE_KEYS = frozenset({"engine", "step_order"})

#: Keys whose presence is proof the proposal is an authority rewrite wearing a
#: derived-plan hat. Named explicitly so the refusal reason is legible.
FORBIDDEN_CHANGE_KEYS = frozenset({
    "milestones", "goal", "depends_on", "product_e2e", "acceptance",
    "acceptance_criteria", "test_command", "required_artifacts", "evaluator",
    "validator", "capabilities", "capability", "allowed_actions", "budget",
    "max_attempts", "attempts", "timeout_s", "budget_s", "product_repo",
    "execution", "steps", "spec", "strategy", "roadmap", "task_file",
})

_AUTHORITY_KEYS = ("strategy_sha", "spec_sha", "roadmap_sha")

#: Marks a mission's engine hint as owned by a derived plan, so the hint can be
#: withdrawn when the plan stops applying without touching an independent
#: ALTERNATE_ENGINE rotation, which carries no source and must survive.
ENGINE_SOURCE_KEY = "preferred_engine_source"
ENGINE_SOURCE_PREFIX = "derived_plan:"



@dataclass(frozen=True)
class DerivedPlan:
    project: str
    milestone_id: str
    mission_id: str
    contract_id: str
    authority_hashes: dict
    failure_fingerprint: str
    changes: dict
    rationale: str = ""
    recovery_id: str = ""
    created_at: str = ""
    supersedes: str = ""
    schema: str = SCHEMA
    plan_id: str = field(default="")

    def as_dict(self) -> dict:
        d = asdict(self)
        d["plan_id"] = self.plan_id or compute_plan_id(self)
        return d

    def engine(self) -> str:
        return str(self.changes.get("engine") or "")

    def step_order(self) -> list:
        return list(self.changes.get("step_order") or [])


def compute_plan_id(plan: DerivedPlan) -> str:
    """Content id over everything that makes the plan mean what it means.

    The stored id is re-derived and compared on every load, so an edited plan file
    is detected rather than obeyed.
    """
    payload = json.dumps({
        "project": plan.project, "milestone_id": plan.milestone_id,
        "mission_id": plan.mission_id, "contract_id": plan.contract_id,
        "authority_hashes": plan.authority_hashes,
        "failure_fingerprint": plan.failure_fingerprint, "changes": plan.changes,
        "supersedes": plan.supersedes,
    }, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def plan_path(project_dir: Path, milestone_id: str) -> Path:
    safe = "".join(c if (c.isalnum() or c in "._-") else "_" for c in str(milestone_id))
    return Path(project_dir) / PLAN_DIR / f"{safe}.json"


def _engines() -> tuple:
    from .escalation_ladder import _ALTERNATE_ENGINES
    return tuple(_ALTERNATE_ENGINES)


def validate_derived_plan(raw, *, project: str, milestone_id: str, contract_id: str,
                          authority_hashes: dict, mission_ids,
                          failure_fingerprint: str, mission_id: str = "",
                          current_engine: str = ""):
    """Validate a proposed derived plan against the position it claims to be for.

    `failure_fingerprint` and `mission_id` are the CURRENT ones — from the recovery
    on the producer side, from the mission record on the consumer side. They are not
    taken from the proposal; the proposal must match them.

    Returns (DerivedPlan, "") on success, or (None, TYPED_REASON). Never raises for
    bad input: a malformed proposal is an ordinary refusal, not a crash in the
    recovery path.
    """
    if not isinstance(raw, dict) or not raw:
        return None, (f"DERIVED_PLAN_MALFORMED: proposal is a "
                      f"{type(raw).__name__}, not a non-empty JSON object")

    if str(raw.get("schema") or SCHEMA) != SCHEMA:
        return None, (f"DERIVED_PLAN_MALFORMED: schema {raw.get('schema')!r} is not "
                      f"{SCHEMA!r}")

    claimed_project = str(raw.get("project") or "")
    if claimed_project and claimed_project != str(project):
        return None, (f"DERIVED_PLAN_FOREIGN_PROJECT: plan names project "
                      f"{claimed_project!r}, this project is {project!r}")

    claimed_ms = str(raw.get("milestone_id") or "")
    if claimed_ms and claimed_ms != str(milestone_id):
        return None, (f"DERIVED_PLAN_FOREIGN_MILESTONE: plan names milestone "
                      f"{claimed_ms!r}, the active milestone is {milestone_id!r}")

    known = list(mission_ids or [])
    claimed_mission = str(raw.get("mission_id") or "")
    if not claimed_mission:
        return None, ("DERIVED_PLAN_UNBOUND_MISSION: a plan must name the interrupted "
                      "mission it was written for")
    if claimed_mission != str(mission_id):
        return None, (f"DERIVED_PLAN_FOREIGN_MISSION: plan is bound to "
                      f"{claimed_mission!r}, the position in force is {mission_id!r}")
    if known and claimed_mission not in known:
        return None, (f"DERIVED_PLAN_UNKNOWN_MISSION: {claimed_mission!r} is not one "
                      f"of the compiled missions {known}")

    claimed_contract = str(raw.get("contract_id") or "")
    if claimed_contract != str(contract_id):
        return None, (f"DERIVED_PLAN_STALE_CONTRACT: plan is bound to contract "
                      f"{claimed_contract[:16]!r}, the frozen contract in force is "
                      f"{str(contract_id)[:16]!r}")

    claimed_auth = raw.get("authority_hashes")
    if not isinstance(claimed_auth, dict):
        return None, "DERIVED_PLAN_MALFORMED: authority_hashes must be an object"
    for k in _AUTHORITY_KEYS:
        want = str((authority_hashes or {}).get(k) or "")
        got = str(claimed_auth.get(k) or "")
        if not want or got != want:
            return None, (f"DERIVED_PLAN_STALE_AUTHORITY: {k} is {got[:12]!r} in the "
                          f"plan and {want[:12]!r} in current authority")

    # Whole fingerprint, compared whole. A prefix/substring match here is how an old
    # incident drives a new execution under the same contract.
    want_fp = str(failure_fingerprint or "")
    got_fp = str(raw.get("failure_fingerprint") or "")
    if not want_fp:
        return None, ("DERIVED_PLAN_NO_FINGERPRINT: there is no recorded failure for "
                      f"{mission_id!r}; a derived plan replans a specific failure")
    if got_fp != want_fp:
        return None, (f"DERIVED_PLAN_FOREIGN_FINGERPRINT: plan was written for a "
                      f"different failure (plan {_fp_digest(got_fp)} vs current "
                      f"{_fp_digest(want_fp)})")

    changes = raw.get("changes")
    if not isinstance(changes, dict) or not changes:
        # An empty plan is the LR3 "empty patch" case one level down: a REPLAN that
        # reports success while replanning nothing consumed the ladder's last stage.
        return None, "DERIVED_PLAN_EMPTY: changes must be a non-empty object"

    forbidden = sorted(set(changes) & FORBIDDEN_CHANGE_KEYS)
    if forbidden:
        return None, (f"DERIVED_PLAN_FORBIDDEN_CHANGE: {forbidden} would change owner "
                      f"intent, acceptance or budget, or has no production consumer; "
                      f"a derived plan changes execution only")
    unknown = sorted(set(changes) - ALLOWED_CHANGE_KEYS)
    if unknown:
        return None, (f"DERIVED_PLAN_UNKNOWN_CHANGE: {unknown} is not in the allowed "
                      f"set {sorted(ALLOWED_CHANGE_KEYS)} — unknown keys FAIL CLOSED")

    if "engine" in changes:
        eng = changes["engine"]
        if not isinstance(eng, str) or eng not in _engines():
            return None, (f"DERIVED_PLAN_UNKNOWN_ENGINE: {eng!r} is not one of "
                          f"{list(_engines())}")

    if "step_order" in changes:
        order = changes["step_order"]
        if not isinstance(order, list) or not all(isinstance(s, str) for s in order):
            return None, "DERIVED_PLAN_MALFORMED: step_order must be a list of strings"
        if sorted(order) != sorted(known):
            missing = sorted(set(known) - set(order))
            extra = sorted(set(order) - set(known))
            return None, (f"DERIVED_PLAN_STEP_SET_CHANGED: step_order must be a "
                          f"permutation of the compiled missions {known}; "
                          f"missing={missing} unknown={extra}. Dropping or inventing a "
                          f"step changes the work, not the plan")

    # No-op check, last: everything else must already be valid for "changes nothing"
    # to be the honest description.
    effective = []
    if str(changes.get("engine") or "") and changes["engine"] != str(current_engine or ""):
        effective.append("engine")
    if changes.get("step_order") and list(changes["step_order"]) != known:
        effective.append("step_order")
    if not effective:
        return None, (f"DERIVED_PLAN_NO_OP: the plan reproduces the order and engine "
                      f"already in force (order={known}, engine={current_engine!r}); "
                      f"accepting it would settle a recovery for no change")

    plan = DerivedPlan(
        project=str(project), milestone_id=str(milestone_id),
        mission_id=claimed_mission, contract_id=str(contract_id),
        authority_hashes={k: str((authority_hashes or {}).get(k) or "")
                          for k in _AUTHORITY_KEYS},
        failure_fingerprint=want_fp,
        changes=dict(changes), rationale=str(raw.get("rationale") or "")[:400],
        recovery_id=str(raw.get("recovery_id") or ""),
        created_at=_now(),
        supersedes=str(raw.get("supersedes") or ""),
    )
    return DerivedPlan(**{**plan.as_dict(), "plan_id": compute_plan_id(plan)}), ""


def _fp_digest(fp: str) -> str:
    return hashlib.sha256(str(fp).encode("utf-8")).hexdigest()[:12]


def write_derived_plan(project_dir: Path, plan: DerivedPlan):
    """Persist the plan durably, CAS on the currently-active plan id.

    Three outcomes:
      * no active plan, or the active one is exactly the plan's declared predecessor
        -> written;
      * the active plan IS this plan (identical content id) -> idempotent success, so
        a replay after a crash between persistence and settlement does not fail a
        recovery that already did its work;
      * anything else -> typed refusal. An unreadable active plan refuses too: an
        error must not be read as absence and overwritten.
    """
    from .project_authority import atomic_write_json

    path = plan_path(project_dir, plan.milestone_id)
    existing_raw, read_err = _read_plan_json(path)
    if read_err:
        return None, (f"DERIVED_PLAN_CORRUPT_ACTIVE: the active plan at {path.name} is "
                      f"unreadable ({read_err}); refusing to overwrite it blindly")
    if existing_raw:
        try:
            prior = DerivedPlan(**{k: v for k, v in existing_raw.items()
                                   if k in DerivedPlan.__dataclass_fields__})
            if existing_raw["plan_id"] != compute_plan_id(prior):
                return None, "DERIVED_PLAN_CORRUPT_ACTIVE: content identity mismatch"
        except (TypeError, ValueError):
            return None, "DERIVED_PLAN_CORRUPT_ACTIVE: invalid identity fields"
    existing_id = str((existing_raw or {}).get("plan_id") or "")
    plan_id = plan.plan_id or compute_plan_id(plan)
    if existing_id and existing_id == plan_id:
        return plan, ""
    if existing_id and existing_id != plan.supersedes:
        return None, (f"DERIVED_PLAN_CAS_CONFLICT: an active plan {existing_id!r} is "
                      f"already persisted for {plan.milestone_id}; this plan supersedes "
                      f"{plan.supersedes!r}")
    atomic_write_json(path, plan.as_dict())
    return plan, ""


def _read_plan_json(path: Path):
    """(payload, error). A missing file is (None, ''); a broken one is (None, why)."""
    p = Path(path)
    if not p.exists():
        return None, ""
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not raw:
            return None, "active plan must be a non-empty JSON object"
        required = {"plan_id", "project", "milestone_id", "mission_id", "contract_id",
                    "authority_hashes", "failure_fingerprint", "changes"}
        if not required <= raw.keys():
            return None, "active plan is missing identity fields"
        return raw, ""
    except Exception as exc:                            # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"[:200]


def load_derived_plan(project_dir: Path, milestone_id: str, *, project: str,
                      contract_id: str, authority_hashes: dict, mission_ids,
                      failure_fingerprint: str, mission_id: str = "",
                      current_engine: str = ""):
    """Re-validate the persisted plan against the CURRENT position.

    Returns (plan, "") when the plan on disk still binds, (None, "") when there is no
    plan, and (None, TYPED_REASON) when a plan exists but no longer binds. Re-reading
    and re-validating on every cycle is what makes restart honest: a plan is obeyed
    because it still matches, never because it was written.
    """
    path = plan_path(project_dir, milestone_id)
    raw, read_err = _read_plan_json(path)
    if read_err:
        return None, f"DERIVED_PLAN_UNREADABLE: {path.name} — {read_err}"
    if raw is None:
        return None, ""
    plan, why = validate_derived_plan(
        raw, project=project, milestone_id=milestone_id, contract_id=contract_id,
        authority_hashes=authority_hashes, mission_ids=mission_ids,
        failure_fingerprint=failure_fingerprint, mission_id=mission_id,
        current_engine=current_engine)
    if plan is None:
        return None, why
    stored_id = str(raw.get("plan_id") or "")
    if stored_id != plan.plan_id:
        return None, (f"DERIVED_PLAN_ID_MISMATCH: {path.name} stores plan_id "
                      f"{stored_id!r} but its content hashes to {plan.plan_id!r} — "
                      f"the file was edited after it was written")
    return plan, ""


def apply_derived_plan(missions: list, plan: DerivedPlan) -> list:
    """Reorder compiled missions per the plan. Never adds, never drops.

    Validation already proved `step_order` is a permutation; this stays defensive
    because a consumer that silently drops a mission on a bad plan would be the very
    failure the validation exists to prevent.
    """
    order = plan.step_order()
    if not order:
        return list(missions)
    by_id = {m.id: m for m in missions}
    if sorted(order) != sorted(by_id):
        return list(missions)
    return [by_id[i] for i in order]
