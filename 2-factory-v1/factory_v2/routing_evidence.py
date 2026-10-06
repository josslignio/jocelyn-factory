"""routing_evidence — derive adaptive-routing evidence from a project's real
attempt ledger (CTO decision 2026-09-13, ADAPTIVE_ROUTING).

The only fact this module trusts is `independently_validated is True` on a
TERMINAL attempt_accounts.jsonl row. `mission_pass` alone — the builder's own
claim about its work — is NEVER counted, matching every other place in the
factory that reads this ledger (factory_v2.settlement_truth,
factory_v2.metrics_dashboard): a builder's self-report is not evidence,
independent validation is.

evidence_from_ledger() is the single producer factory/bin/model_router.py's
runrecords_override() is allowed to be handed evidence from — it never reads
attempt_accounts.jsonl itself.
"""
from __future__ import annotations

from pathlib import Path

from .launch_gate import ENGINE_NONE_REFUSED
from .resource_accounting import fold, read_rows

LEDGER_REL = ("evidence", "attempt_accounts.jsonl")


def evidence_from_ledger(project_dir: "str | Path", spec_level: str, task_kind: str) -> dict:
    """Return {engine: {"n": int, "success": int}} derived from
    `<project_dir>/evidence/attempt_accounts.jsonl`.

    Only TERMINAL rows count. A row is matched against spec_level/task_kind
    only when BOTH fields are present on the (folded) row; a row missing
    either field entirely is excluded from every query rather than assumed
    to match — an attempt this ledger cannot classify is not evidence for
    any particular (spec_level, task_kind), so it counts for none.

    `n` counts rows with `independently_validated is True`. `success` counts,
    among those, rows with `mission_pass is True`. A row that is not
    independently validated contributes to neither n nor success, no matter
    what `mission_pass` says — an unvalidated claim of success is exactly the
    signal this function must never trust (CTO decision 2026-09-13).

    Rows are folded first (factory_v2.resource_accounting.fold): the ledger
    is append-only and two-phase — an OPEN row carries engine/spec_level/
    task_kind, a later validation row carries independently_validated/
    mission_pass — so a single attempt's identifying fields and its
    validated outcome commonly live on different lines sharing the same
    attempt_key. Folding recovers the one full record per attempt this
    function needs; a row with no attempt_key (legacy rows, and the judge's
    synthetic fixture rows) stands alone, unchanged.

    FACTORY_LEDGER_ENGINE_KNOWN_V1: rows with engine == ENGINE_NONE_REFUSED
    ('none' — the launch was refused before execve) are ignored EXPLICITLY.
    They are already excluded by construction on every honest path (a refusal
    never validates and closes as PREFLIGHT_REFUSED, not SEMANTIC_FAILURE),
    but the guard is load-bearing, not decorative: 'none' is a statement about
    routing GATES, not about an engine's ability, so no future exit-class
    widening may let it count as evidence for or against an engine.
    """
    evidence: dict = {}
    if not project_dir:
        return evidence
    ledger_path = Path(project_dir).joinpath(*LEDGER_REL)
    for row in fold(read_rows(ledger_path)):
        if not isinstance(row, dict):
            continue
        if row.get("phase") != "TERMINAL":
            continue
        if "spec_level" in row:
            if row.get("spec_level") != spec_level:
                continue
        else:
            continue
        if "task_kind" in row:
            if row.get("task_kind") != task_kind:
                continue
        else:
            continue
        engine = row.get("engine")
        if not engine:
            continue
        if engine == ENGINE_NONE_REFUSED:
            continue
        if row.get("independently_validated") is True:
            bucket = evidence.setdefault(engine, {"n": 0, "success": 0})
            bucket["n"] += 1
            if row.get("mission_pass") is True:
                bucket["success"] += 1
        elif _observed_failure(row):
            # 2026-09-13: a failure the factory's OWN gates observed (the
            # dispatcher ran the pinned test_cmd / acceptance / C40 gates in
            # the worktree and they went red) is evidence against the engine
            # — it is not a builder self-report. Without it every engine
            # scored 100 % and the router could never learn that one of them
            # fails more than the other. Kills, preflight refusals and quota
            # cuts are NOT engine failures and stay excluded.
            bucket = evidence.setdefault(engine, {"n": 0, "success": 0})
            bucket["n"] += 1
    return evidence


#: exit classes that mean "the engine produced work and the factory's independent
#: gates rejected it" — never a kill, a refusal or an infrastructure event.
OBSERVED_FAILURE_EXIT_CLASSES = frozenset({"SEMANTIC_FAILURE"})


def _observed_failure(row: dict) -> bool:
    return (row.get("mission_pass") is not True
            and row.get("exit_class") in OBSERVED_FAILURE_EXIT_CLASSES
            and row.get("consumes_model_quota") is not False)
