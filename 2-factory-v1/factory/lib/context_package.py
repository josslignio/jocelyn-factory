"""CTO004 — ContextPackage: the serializable unit of context handed to a mission.

The context_sha256 enables the post-mortem distinction between "agent ignored
context that was available" (ADR/lesson present in `sources`) and "context
injection gap" (relevant ADR/lesson never made it into `sources`).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class ContextPackage:
    mission_id: str
    context_version: int
    generated_at: str
    agent: str

    global_immutable: dict = field(default_factory=dict)
    architecture: list = field(default_factory=list)
    adrs: list = field(default_factory=list)
    lessons: list = field(default_factory=list)
    model_performance: dict = field(default_factory=dict)
    dependency_receipts: list = field(default_factory=list)
    blockers: list = field(default_factory=list)

    sources: list = field(default_factory=list)
    context_sha256: str = ""
    # V2 canonical context: set when project_dir is provided (fail-closed path).
    # Includes strategy_sha, spec_sha, roadmap_sha, current_milestone, contract_id.
    # Empty dict on V1/legacy path — worker must never silently dispatch on stale authority.
    canonical_context: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ContextPackage":
        return cls(
            mission_id=d["mission_id"],
            context_version=d["context_version"],
            generated_at=d["generated_at"],
            agent=d["agent"],
            global_immutable=d.get("global_immutable", {}),
            architecture=d.get("architecture", []),
            adrs=d.get("adrs", []),
            lessons=d.get("lessons", []),
            model_performance=d.get("model_performance", {}),
            dependency_receipts=d.get("dependency_receipts", []),
            blockers=d.get("blockers", []),
            sources=d.get("sources", []),
            context_sha256=d.get("context_sha256", ""),
            canonical_context=d.get("canonical_context", {}),
        )
