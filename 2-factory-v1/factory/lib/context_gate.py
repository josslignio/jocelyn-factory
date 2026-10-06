"""Pre-dispatch gates: the mission's context package (CTO004) and the research-reuse decision (CTOK-003-Q).

Policy module of dispatcher_v3 (F4 extraction). Runs BEFORE any worktree or subprocess exists:
  - V2 (project_dir set): context injection is FAIL-CLOSED on canonical authority
    (DISPATCH_WITHOUT_FRESH_CANONICAL_PROJECT_CONTEXT = 0); V1 (project_dir None) is best-effort.
  - A blocked_reason (MAJOR_BUILD_WITHOUT_RESEARCH_REUSE_DECISION = 0) means the caller must NOT dispatch.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional


def _log(msg: str) -> None:
    print(msg, flush=True)


def _write_json(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def check_research_reuse_gate(mission_id: str, repo_root: Path, component: Optional[str],
                              out_path: Path) -> Optional[str]:
    """The decision — or the block — is persisted as evidence. Returns a blocked_reason or None; an empty
    component is never gated (nothing declared to check against)."""
    if not component:
        return None
    try:
        from research_reuse_gate import MissingReuseDecision, ResearchReuseGate, ReuseRegistryError
    except ImportError as exc:
        _log(f"RESEARCH_REUSE_GATE_SKIPPED: {mission_id} — module unavailable ({exc})")
        return None
    gate = ResearchReuseGate(Path(repo_root) / "factory" / "cto" / "CTO_RESEARCH_REGISTRY.json")
    evidence_path = out_path.parent / "research_reuse_decision.json"
    evidence = {"mission_id": mission_id, "component": component}
    try:
        result = gate.check_gate(component)
    except MissingReuseDecision as exc:
        _log(f"MAJOR_BUILD_WITHOUT_RESEARCH_REUSE_DECISION_BLOCKED: {mission_id} — {exc}")
        _write_json(evidence_path, {**evidence, "gate_result": "BLOCKED", "reason": str(exc)})
        return f"MISSING_RESEARCH_REUSE_DECISION: {exc}"
    except ReuseRegistryError as exc:
        _log(f"RESEARCH_REUSE_REGISTRY_INVALID_BLOCKED: {mission_id} — {exc}")
        return f"RESEARCH_REUSE_REGISTRY_INVALID: {exc}"
    _write_json(evidence_path, {**evidence, "gate_result": "REQUIRED_AND_DECIDED" if result.required else "NOT_REQUIRED",
                                "decision": result.decision.to_dict() if result.decision else None})
    return None


def prepare_context_package(mission_id: str, repo_root: Path, out_path: Path, plan_file: Optional[Path],
                            project_dir: Optional[Path] = None) -> tuple[Optional[Path], Optional[str]]:
    """Build + persist the mission's context package. Returns (context_path, blocked_reason)."""
    plan: dict = {}
    if plan_file is not None:
        try:
            plan = json.loads(Path(plan_file).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _log(f"CONTEXT_INJECTION_PLAN_WARN: {mission_id} — cannot read plan_file {plan_file}: {exc}")
        if not isinstance(plan, dict):
            plan = {}
    blocked = check_research_reuse_gate(mission_id, repo_root, plan.get("component"), out_path)
    if blocked is not None:
        return None, blocked
    try:
        from context_compiler import ContextCompiler
    except ImportError as exc:
        if project_dir is not None:
            raise RuntimeError("DISPATCH_WITHOUT_FRESH_CANONICAL_PROJECT_CONTEXT: "
                               f"context_compiler unavailable on V2 path: {exc}") from exc
        _log(f"CONTEXT_INJECTION_SKIPPED: {mission_id} — context_compiler unavailable ({exc})")
        return None, None
    try:
        compiler = ContextCompiler(repo_root, project_dir=project_dir)
        package = compiler.compile(mission_id, component=plan.get("component"), touched_paths=plan.get("touched_paths"),
                                   dependency_mission_ids=plan.get("dependency_mission_ids"),
                                   current_milestone=plan.get("current_milestone"), contract_id=plan.get("contract_id"))
        context_path = out_path.parent / "context_package.json"
        compiler.save(package, context_path)
        _log(f"CONTEXT_INJECTED: {mission_id} sources={len(package.sources)} sha256={package.context_sha256[:12]}")
        return context_path, None
    except Exception as exc:
        if project_dir is not None:
            raise RuntimeError("DISPATCH_WITHOUT_FRESH_CANONICAL_PROJECT_CONTEXT: "
                               f"V2 context compilation failed for {mission_id}: {exc}") from exc
        _log(f"CONTEXT_INJECTION_FAILED: {mission_id} — {exc}")
        return None, None
