"""Mission compiler — un milestone produit des missions DÉRIVÉES.

La mission est une unité d'exécution, jamais une source de direction produit:
DERIVED_ARTIFACT_OVERRIDES_CANONICAL_ROADMAP = 0. Le compiled mission set est
re-calculable à l'identique depuis (roadmap milestone + contract) — décisions
déterministes re-jouables après restart (ADAPT Temporal).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .project_authority import ProjectAuthority

_VALID_EXECUTOR_TYPES = frozenset(
    {"local_command", "autopilot", "weekly_refresh_observed"})


class MissionCompileError(ValueError):
    """Raised when a roadmap milestone cannot be compiled into executable missions.

    P0-B: every compilation failure is explicit and machine-readable — no silent
    skips, no placeholder commands, no unknown executor types accepted.
    """


@dataclass(frozen=True)
class Mission:
    id: str
    milestone: str
    kind: str
    contract_id: str
    executor: dict

    def as_dict(self) -> dict:
        return {"id": self.id, "milestone": self.milestone, "kind": self.kind,
                "contract_id": self.contract_id, "executor": self.executor}


def compile_missions(authority: ProjectAuthority, milestone_entry: dict,
                     contract_id: str) -> list[Mission]:
    """Compile milestone execution steps into Mission objects.

    Raises MissionCompileError for any non-machine-executable step.
    Returns [] only when the milestone has no execution steps at all
    (informational-only milestone → MILESTONE_NOT_COMPILABLE, owner must add steps).

    P0-B invariants enforced:
    - String step → MissionCompileError (documentation is not a mission)
    - Missing executor → MissionCompileError
    - Unknown executor type → MissionCompileError (FAIL CLOSED)
    - local_command without command → MissionCompileError
    - Never generates command="true" as placeholder

    WRONG_REPO_DISPATCH fix: product_repo from ROADMAP.yaml is injected into autopilot
    executor dicts so AutopilotExecutor passes --repo to the correct product repo,
    not factory-foundation-v2.
    """
    mid = milestone_entry["id"]
    spec = milestone_entry.get("execution") or {}
    steps = spec.get("steps") or []
    if not steps:
        return []
    # WRONG_REPO_DISPATCH: thread product_repo from authority into autopilot executors.
    project_product_repo = str(authority.roadmap.get("product_repo") or "")
    # LOT C32 (NO_TESTS_EXECUTED fail-closed): a step's own `test_cmd`
    # (AutopilotExecutor's mechanical T1/T2/T3 build oracle) and
    # product_e2e.test_command (the independent product verifier, checked
    # separately at settlement by factory_v2/runtime.py) are two DELIBERATELY
    # distinct gates — product decision 2026-09-12: the two-gate architecture
    # is intentional (fast local oracle vs. independent e2e proof), so a step
    # MAY declare its own test_cmd that differs from product_e2e.test_command.
    # The only bug this lot fixes is the case where a step declares NEITHER:
    # TR_UNIVERSE_EXPANSION_V1#M1 (2026-09-12) had no test_cmd at all, so
    # dispatcher_v3 never ran ANY test yet still reached ACCEPTED. Threading
    # product_e2e.test_command in as a fallback only (never overriding an
    # explicit step test_cmd) closes exactly that gap.
    milestone_test_command = (milestone_entry.get("product_e2e") or {}).get("test_command")
    missions = []
    for i, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            raise MissionCompileError(
                f"COMPILE_ERROR: {mid}#M{i} is a string (documentation), not a "
                f"machine-executable step. Add executor: {{type: local_command, "
                f"command: '...'}} to make it dispatchable. String steps in executable "
                f"execution.steps are forbidden — move documentation outside steps[] or "
                f"use a non-executable schema."
            )
        executor = step.get("executor")
        if not executor or not isinstance(executor, dict):
            raise MissionCompileError(
                f"COMPILE_ERROR: {mid}#M{i} has no executor dict. "
                f"Add executor: {{type: local_command, command: '...'}} or "
                f"executor: {{type: autopilot}}."
            )
        executor = dict(executor)
        # Inject product_repo into autopilot executors if not already set.
        # This fixes WRONG_REPO_DISPATCH: AutopilotExecutor must use the product repo
        # (e.g. twitter-radar-foundation-v2), not the factory authority repo.
        if executor.get("type") == "autopilot" and project_product_repo:
            executor.setdefault("product_repo", project_product_repo)
        # LOT C30: thread milestone.deliverables (ROADMAP.yaml, the same list
        # factory_v2/project_authority.py's MilestoneView.deliverables resolves)
        # into autopilot executors, so AutopilotExecutor.execute() can pass
        # --deliverable to dispatcher_v3 (LOT C28's CANDIDATE_MISSING_DELIVERABLE
        # invariant: no ACCEPTED verdict when a declared deliverable is absent
        # from the candidate commit). Only autopilot missions dispatch to
        # dispatcher_v3 — local_command/weekly_refresh_observed never read this
        # key.
        #
        # LOT C31 finding 2 (CRITIQUE, CODEX_REVIEW_C24_C30.md): this used to be
        # `executor.setdefault("deliverables", ...)`, which left a step-authored
        # `executor.deliverables` — including an empty or partial list written
        # into the roadmap by mistake — silently WIN over the milestone's
        # canonical list, because setdefault never overwrites an existing key.
        # dispatcher_v3 then verified that (possibly empty) list and accepted a
        # candidate missing the milestone's real deliverables.
        # milestone.deliverables (ROADMAP.yaml) is the sole source of truth. A
        # step MAY also declare `deliverables`, but only if it is authored
        # byte-for-byte identical to the milestone's list — any divergence
        # (including an empty step-level list when the milestone declares
        # deliverables) is a compile-time error, never a silent override.
        if executor.get("type") == "autopilot":
            canonical_deliverables = list(milestone_entry.get("deliverables") or [])
            _split_ok = False
            if "deliverables" in executor:
                step_deliverables = list(executor.get("deliverables") or [])
                # 2026-09-18: a milestone built by SEVERAL autopilot steps (deep
                # research: GLM writes glm.json, Claude writes claude.json, then
                # the synthesis) may split the canonical list across its steps —
                # each step declares a non-empty subset, and the union over all
                # autopilot steps must equal the milestone list exactly, so
                # nothing is ever lost. Anything else is still a compile error.
                # L2_runtime_core-016: a LATER step whose executor is not a
                # dict must reach the main loop's typed MissionCompileError,
                # not blow up here as AttributeError('str'.get).
                _auto_steps = [st for st in steps
                               if isinstance(st, dict)
                               and isinstance(st.get("executor"), dict)
                               and st["executor"].get("type") == "autopilot"]
                _split_ok = False
                if len(_auto_steps) > 1 and step_deliverables \
                        and set(step_deliverables) < set(canonical_deliverables):
                    _union = set()
                    _all_declared = True
                    for st in _auto_steps:
                        _sd = (st.get("executor") or {}).get("deliverables")
                        if not _sd:
                            _all_declared = False
                            break
                        _union |= set(_sd)
                    _split_ok = _all_declared and _union == set(canonical_deliverables)
                if _split_ok:
                    executor["deliverables"] = step_deliverables
                elif step_deliverables != canonical_deliverables:
                    raise MissionCompileError(
                        f"COMPILE_ERROR: {mid}#M{i} executor declares "
                        f"deliverables={step_deliverables!r} which diverges from "
                        f"milestone.deliverables={canonical_deliverables!r}. "
                        f"milestone.deliverables (ROADMAP.yaml) is the sole source "
                        f"of truth for what a candidate must contain — a step-level "
                        f"deliverables override is forbidden, even an empty one. "
                        f"Remove the step-level 'deliverables' key or make it match "
                        f"the milestone's list exactly."
                    )
            if not (("deliverables" in executor) and _split_ok):
                executor["deliverables"] = canonical_deliverables

            # LOT C32: thread product_e2e.test_command into executor.test_cmd
            # ONLY as a fallback, when the step doesn't declare its own —
            # AutopilotExecutor only forwards --test-cmd to dispatcher_v3 when
            # executor['test_cmd'] is set (factory_v2/executors.py `if
            # test_cmd:`), so a milestone whose only step omitted test_cmd
            # previously dispatched with NO test command at all: dispatcher_v3
            # then skipped every test_N step and could still reach an
            # ACCEPTED verdict off a build that was never mechanically
            # checked. A step's own explicit test_cmd is never overridden or
            # rejected here, even when it differs from product_e2e.test_command
            # — the two-gate architecture (fast local oracle vs. independent
            # product_e2e verifier checked separately at settlement) is a
            # deliberate product decision, not a bug.
            if milestone_test_command:
                # LOT C47: always expose the milestone's own oracle to the executor so
                # it can flag a step whose test_cmd is that very command.
                executor["product_e2e_test_cmd"] = str(milestone_test_command).strip()
                existing_test_cmd = executor.get("test_cmd")
                if existing_test_cmd is None or not str(existing_test_cmd).strip():
                    executor["test_cmd"] = str(milestone_test_command).strip()
        exec_type = executor.get("type")
        if not exec_type:
            raise MissionCompileError(
                f"COMPILE_ERROR: {mid}#M{i} executor has no type field. "
                f"Valid types: {sorted(_VALID_EXECUTOR_TYPES)}"
            )
        if exec_type not in _VALID_EXECUTOR_TYPES:
            raise MissionCompileError(
                f"COMPILE_ERROR: {mid}#M{i} has unknown executor type {exec_type!r}. "
                f"Valid types: {sorted(_VALID_EXECUTOR_TYPES)}. "
                f"Unknown executor types FAIL CLOSED — never silently default."
            )
        if exec_type == "local_command":
            command = executor.get("command")
            if not command or not command.strip():
                raise MissionCompileError(
                    f"COMPILE_ERROR: {mid}#M{i} is local_command but has no command. "
                    f"A missing command is forbidden — never default to 'true'. "
                    f"Provide an explicit command or change executor type."
                )
        if exec_type == "weekly_refresh_observed":
            if "observation_dir" in executor:
                raise MissionCompileError(
                    f"COMPILE_ERROR: {mid}#M{i} is weekly_refresh_observed but "
                    f"sets observation_dir. Mission-authored observation_dir is "
                    f"forbidden — the supervisor's observation store location is "
                    f"a constructor-only parameter of WeeklyRefreshObservedExecutor "
                    f"(test-only override), never read from mission data. A mission "
                    f"that could set this could redirect the supervisor-owned "
                    f"observation store into the product repo or anywhere else."
                )
            command = executor.get("command")
            if not command or not command.strip():
                raise MissionCompileError(
                    f"COMPILE_ERROR: {mid}#M{i} is weekly_refresh_observed but has "
                    f"no command. Provide the exact refresh command the supervisor "
                    f"must run and observe."
                )
            refresh_seq = executor.get("refresh_seq")
            if refresh_seq not in (1, 2):
                raise MissionCompileError(
                    f"COMPILE_ERROR: {mid}#M{i} is weekly_refresh_observed but "
                    f"refresh_seq is {refresh_seq!r}. Must be 1 or 2 — the frozen "
                    f"Cycle-2 distinctness rules only recognize two refreshes."
                )
            commit_outputs = executor.get("commit_outputs", False)
            if not isinstance(commit_outputs, bool) or (commit_outputs and refresh_seq != 2):
                raise MissionCompileError(
                    f"COMPILE_ERROR: {mid}#M{i} commit_outputs must be boolean and "
                    f"may be true only for refresh_seq=2, after both refresh legs."
                )
            product_repo = executor.get("product_repo")
            if not product_repo or not str(product_repo).strip():
                raise MissionCompileError(
                    f"COMPILE_ERROR: {mid}#M{i} is weekly_refresh_observed but has "
                    f"no product_repo. The supervisor must be told which product "
                    f"tree to run the command in and recompute hashes from — "
                    f"never a hardcoded default."
                )
        mission_id = f"{mid}#M{i}"
        missions.append(Mission(
            id=mission_id, milestone=mid,
            kind=str(step.get("kind", "implement")),
            contract_id=contract_id, executor=executor,
        ))
    return missions


