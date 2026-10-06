#!/usr/bin/env python3
"""CTOK-006B — TaskContractValidator: enqueue-time validation gate.

Hard invariant enforced: QUEUED_INVALID_MISSION_CONTRACT = 0 — a mission whose
task file or plan does not meet the required contract can never enter the
rolling backlog queue. The gate runs BEFORE enqueue (rolling_backlog.py
`enqueue_mission`), so a malformed mission is rejected with a
ContractViolationError (carrying every violation) instead of wasting a dispatch
slot and surfacing as a confusing BLOCKED verdict at preflight time.

Contract checked:
  - task file (markdown): real ATX headings `## Outcome`, `## Deliverables`,
    `## Depends On` OUTSIDE fenced code blocks — a section mentioned inside a
    code block is documentation, not a section (A1).
  - plan (JSON): required fields `id`, `repo`, `task_file`, `test_cmd`,
    `spec_level`, each present and non-empty.
  - mission (plan dict + task file on disk): combined check; the plan's
    task_file must resolve to a readable file satisfying the section contract
    (a plan pointing at a nonexistent task file fails, A2).
"""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from factory_v2._io import _now
from factory_v2.shared_parsers import iter_outside_fences

# Required plan fields (exact spec vocabulary) and required task sections.
REQUIRED_PLAN_FIELDS = ("id", "repo", "task_file", "test_cmd", "spec_level")
REQUIRED_TASK_SECTIONS = ("## Outcome", "## Deliverables", "## Depends On")
# Spec-level vocabulary (factory/bin/model_router.py SpecLevel: S / A / B).
SPEC_LEVELS = frozenset({"S", "A", "B"})

# Invariant counter (A5): number of invalid-contract enqueue attempts the gate
# blocked. The invariant QUEUED_INVALID_MISSION_CONTRACT = 0 (invalid missions
# actually queued) holds because the gate never lets one through; this counter
# monitors gate activity across attempts.
COUNTERS = {"QUEUED_INVALID_MISSION_CONTRACT": 0}

# ATX heading: 0-3 leading spaces, exactly two '#', one+ spaces, exact section
# name, nothing else on the line. A list item or deeper indentation is not a
# real section heading. (Section literals above already carry the "## " prefix.)
_SECTION_NAME_RE = {
    section: re.compile(r"^\s{0,3}##\s+" + re.escape(section[3:].strip()) + r"\s*$")
    for section in REQUIRED_TASK_SECTIONS
}

# Fenced code block opener: 0-3 leading spaces then ``` or ~~~ (3+ chars).
_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")



@dataclass
class ContractViolation:
    field: str
    reason: str


@dataclass
class ValidationResult:
    valid: bool
    violations: list[ContractViolation] = field(default_factory=list)

    def add(self, field_: str, reason: str) -> None:
        self.violations.append(ContractViolation(field=field_, reason=reason))
        self.valid = False


class ContractViolationError(Exception):
    """Raised by the enqueue gate when validate_mission() returns violations.
    Carries the full violation list for the caller (CTO event stream, BLOCKED
    verdict rendering)."""

    def __init__(self, violations: list[ContractViolation]):
        self.violations = list(violations)
        details = "; ".join(f"{v.field}: {v.reason}" for v in self.violations)
        super().__init__(f"invalid mission contract — {len(self.violations)} "
                         f"violation(s): {details}")


_utc_now = _now



class TaskContractValidator:
    """Enqueue-time contract gate — pure checks, no side effects. The caller
    (rolling_backlog.enqueue_mission) owns the raise/counter/event policy."""

    # ------------------------------------------------------------------
    # task file
    # ------------------------------------------------------------------

    def validate_task_file(self, task_path: Path) -> ValidationResult:
        """Check task file has required sections: ## Outcome, ## Deliverables,
        ## Depends On — as REAL headings outside fenced code blocks."""
        result = ValidationResult(valid=True)
        task_path = Path(task_path)
        if not task_path.exists():
            result.add("task_file", f"task file not found: {task_path}")
            return result
        if not task_path.is_file():
            result.add("task_file", f"task file is not a regular file: {task_path}")
            return result
        try:
            content = task_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            result.add("task_file", f"task file unreadable: {exc}")
            return result
        if not content.strip():
            result.add("task_file", f"task file is empty: {task_path}")
            return result

        found = {section: False for section in REQUIRED_TASK_SECTIONS}
        # A1: a section named inside a code block is not a section.
        # L6_bin_lib_products_2-004: fence tracking (character + length,
        # CommonMark) is the shared parser's — the local toggle closed a
        # four-backtick block on three backticks.
        for line in iter_outside_fences(content):
            stripped = line.rstrip()
            for section, rex in _SECTION_NAME_RE.items():
                if rex.match(stripped):
                    found[section] = True
        for section in REQUIRED_TASK_SECTIONS:
            if not found[section]:
                result.add(section,
                           f"required section '{section}' not found as a real "
                           f"heading outside code blocks in {task_path}")
        return result

    # ------------------------------------------------------------------
    # plan file / plan dict
    # ------------------------------------------------------------------

    def _check_plan_dict(self, plan: dict) -> ValidationResult:
        """Structural check of one flat plan dict (spec: id, repo, task_file,
        test_cmd, spec_level — present and non-empty)."""
        result = ValidationResult(valid=True)
        for name in REQUIRED_PLAN_FIELDS:
            if name not in plan:
                result.add(name, f"required plan field '{name}' is missing")
                continue
            value = plan[name]
            # L6_bin_lib_products_2-005: [] / {} / 0 / False are not an id, a
            # repo, a command or a level — only a non-empty string is (test_cmd
            # may also be a non-empty argv list of non-empty strings).
            if name == "test_cmd" and isinstance(value, list):
                if not value or not all(isinstance(t, str) and t.strip() for t in value):
                    result.add(name, f"required plan field '{name}' is empty")
                continue
            if not isinstance(value, str) or not value.strip():
                result.add(name, f"required plan field '{name}' is empty or not a string")
                continue
            if name == "spec_level" and value.strip() not in SPEC_LEVELS:
                result.add(name, f"plan field 'spec_level' {value!r} not in {sorted(SPEC_LEVELS)}")
        return result

    def validate_plan_file(self, plan_path: Path) -> ValidationResult:
        """Check plan JSON has required fields: id, repo, task_file, test_cmd,
        spec_level. Accepts the flat mission dict OR the canonical repo wrapper
        {"missions": [ ... ]} (every mission entry is then checked)."""
        result = ValidationResult(valid=True)
        plan_path = Path(plan_path)
        if not plan_path.exists():
            result.add("plan_file", f"plan file not found: {plan_path}")
            return result
        try:
            data = json.loads(plan_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            result.add("plan_file", f"plan file unreadable: {exc}")
            return result
        except json.JSONDecodeError as exc:
            result.add("plan_file", f"plan file is not valid JSON: {exc}")
            return result
        return self._check_plan_payload(data)

    def _check_plan_payload(self, data) -> ValidationResult:
        result = ValidationResult(valid=True)
        if not isinstance(data, dict):
            result.add("plan_file", "plan root must be a JSON object")
            return result
        if isinstance(data.get("missions"), list) and data["missions"]:
            # canonical repo wrapper — check every mission entry
            for i, entry in enumerate(data["missions"]):
                if not isinstance(entry, dict):
                    result.add(f"missions[{i}]",
                               f"plan missions[{i}] must be a JSON object")
                    continue
                sub = self._check_plan_dict(entry)
                for v in sub.violations:
                    result.add(f"missions[{i}].{v.field}", v.reason)
            return result
        return self._check_plan_dict(data)

    # ------------------------------------------------------------------
    # full mission contract
    # ------------------------------------------------------------------

    def validate_mission(self, plan: dict) -> ValidationResult:
        """Full contract check: validate plan + task file. Returns combined
        result. The plan's task_file must point to a readable file satisfying
        the section contract (nonexistent task file → violation, A2)."""
        result = self._check_plan_payload(plan)
        # Task file checks are meaningful only when a usable task_file exists.
        task_file = plan.get("task_file") if isinstance(plan, dict) else None
        if isinstance(task_file, str) and task_file.strip():
            sub = self.validate_task_file(Path(task_file))
            result.violations.extend(sub.violations)
            if sub.violations:
                result.valid = False
        elif not any(v.field.endswith("task_file") for v in result.violations):
            result.add("task_file",
                       "cannot check task file: plan has no usable task_file")
        return result

    def validate_or_raise(self, plan: dict) -> ValidationResult:
        """validate_mission + raise ContractViolationError on violations —
        the exact call the enqueue gate makes."""
        result = self.validate_mission(plan)
        if not result.valid:
            raise ContractViolationError(result.violations)
        return result
