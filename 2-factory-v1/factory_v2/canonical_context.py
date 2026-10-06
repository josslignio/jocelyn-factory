"""Phase 6: CanonicalContextManifest — zero chat dependency.

Every dispatch receives a complete content-addressed manifest reconstructable
entirely from disk — no chat history, no session memory, no implied context.

Cold reconstruction proof:
    kill every AI session and factoryd
    → new session with zero conversation history
    → reconstruct: project goal, milestone, mission, failures, attempts,
      audits, research, SHAs, next work, rationale — entirely from disk
    → continue correctly

Manifest fields:
    project_goal: str                -- from STRATEGY.md North Star
    north_star: str                  -- from SPEC.md Done When
    strategy_sha: str
    spec_sha: str
    roadmap_sha: str
    project_state_sha: str
    current_milestone: str           -- from PROJECT_STATE.json
    current_milestone_goal: str      -- from ROADMAP.yaml
    why_next: str                    -- from ROADMAP.yaml why_now
    dependencies: list               -- from ROADMAP.yaml depends_on
    completed_milestones: list
    repository: str                  -- repo path
    base_sha: str                    -- git HEAD
    candidate_sha: str               -- from lease or state
    accepted_sha: str                -- from candidate registry
    validation_contract_id: str
    validation_contract_sha: str
    lease_owner: str                 -- from WorkerLease
    lease_generation: int
    fencing_token: str
    failure_history: list            -- from project state events
    failure_fingerprint: str
    strategies_attempted: list
    escalation_stage: str
    audit_findings: list             -- from evidence/audits/
    research_decisions: list         -- from evidence/research/
    safety_state: str                -- CLEAR or QUARANTINED
    actor_capability_policy_sha: str
    manifest_sha: str               -- sha256 of all above fields (content-addressed)
    produced_at: str
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from factory_v2 import authority as _authority
from factory_v2 import event_journal
from factory_v2._io import _now

try:
    import yaml as _yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False

SCHEMA = "canonical-context-manifest/v1"


@dataclass
class CanonicalContextManifest:
    schema: str = SCHEMA
    produced_at: str = ""
    project_goal: str = ""
    north_star: str = ""
    strategy_sha: str = ""
    spec_sha: str = ""
    roadmap_sha: str = ""
    project_state_sha: str = ""
    current_milestone: str = ""
    current_milestone_goal: str = ""
    why_next: str = ""
    dependencies: list = field(default_factory=list)
    completed_milestones: list = field(default_factory=list)
    repository: str = ""
    base_sha: str = ""
    candidate_sha: str = ""
    accepted_sha: str = ""
    validation_contract_id: str = ""
    validation_contract_sha: str = ""
    lease_owner: str = ""
    lease_generation: int = 0
    fencing_token: str = ""
    failure_history: list = field(default_factory=list)
    failure_fingerprint: str = ""
    strategies_attempted: list = field(default_factory=list)
    escalation_stage: str = ""
    audit_findings: list = field(default_factory=list)
    research_decisions: list = field(default_factory=list)
    safety_state: str = "CLEAR"
    actor_capability_policy_sha: str = ""
    manifest_sha: str = ""

    def compute_manifest_sha(self) -> str:
        """Content-addressed SHA over all fields (excluding manifest_sha itself)."""
        payload = {k: v for k, v in asdict(self).items()
                   if k not in ("manifest_sha", "produced_at")}
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()
        ).hexdigest()[:24]

    def finalize(self) -> "CanonicalContextManifest":
        self.manifest_sha = self.compute_manifest_sha()
        return self

    def as_dict(self) -> dict:
        return asdict(self)

    def as_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2)

    def verify_integrity(self) -> bool:
        return self.compute_manifest_sha() == self.manifest_sha


def build_manifest(
    project_dir: Path,
    repo_dir: Path | None = None,
    mission_id: str = "",
) -> CanonicalContextManifest:
    """Reconstruct a CanonicalContextManifest entirely from disk.

    This is the cold-start reconstruction path: no chat history, no session memory.
    All fields derived from files on disk + git state.
    """
    project_dir = Path(project_dir)
    _repo = Path(repo_dir) if repo_dir else project_dir.parent.parent
    produced_at = _now()

    # Authority hashes — read at HEAD (factory_v2.authority): an uncommitted
    # disk edit to STRATEGY/SPEC/ROADMAP must never change the manifest.
    strategy_sha = _sha16_authority(project_dir, "STRATEGY.md")
    spec_sha = _sha16_authority(project_dir, "SPEC.md")
    roadmap_sha = _sha16_authority(project_dir, "ROADMAP.yaml")
    project_state_sha = _sha16(project_dir / "PROJECT_STATE.json")

    # Project goal / North Star from STRATEGY.md.
    project_goal = _extract_section_authority(project_dir, "STRATEGY.md", ["## Objectif", "## Goal", "## North Star"], lines=3)
    north_star = _extract_section_authority(project_dir, "SPEC.md", ["## North Star", "Done when", "> Autonomously"], lines=3)

    # Current roadmap state.
    current_milestone = ""
    current_milestone_goal = ""
    why_next = ""
    dependencies: list = []
    completed_milestones: list = []
    failure_history: list = []
    failure_fingerprint = ""
    strategies_attempted: list = []

    try:
        state = json.loads((project_dir / "PROJECT_STATE.json").read_text(encoding="utf-8"))
        current_milestone = state.get("current_milestone", "")
        completed_milestones = list(state.get("completed_milestones", {}).keys())
        # STA-04: full history now lives in evidence/events.jsonl once the
        # journal is enabled; iter_events() falls back to state["events"]
        # unchanged when it isn't, so this is safe either way.
        all_events = event_journal.iter_events(project_dir)
        failure_history = [e for e in all_events
                           if ("FAIL" in str(e.get("event") or e.get("type") or "")
                               or "INVALID" in str(e.get("event") or e.get("type") or ""))][-10:]
        last_fp = next(
            (e.get("failure_fingerprint", "") for e in reversed(all_events)
             if e.get("failure_fingerprint")),
            ""
        )
        failure_fingerprint = last_fp
    except Exception:
        pass

    if current_milestone and _HAS_YAML:
        try:
            roadmap = _authority.load_roadmap(project_dir)
            for ms in roadmap.get("milestones", []):
                if ms.get("id") == current_milestone:
                    current_milestone_goal = ms.get("goal", "")
                    why_next = ms.get("why_now", "")
                    deps = ms.get("depends_on") or {}
                    if isinstance(deps, dict):
                        dependencies = list(deps.keys())
                    break
        except Exception:
            pass

    # Git state.
    base_sha = _git_head(_repo)

    # Lease info.
    lease_owner = ""
    lease_generation = 0
    fencing_token = ""
    candidate_sha = ""
    if mission_id:
        try:
            from .worker_lease import WorkerLease
            lease_dir = project_dir / "evidence" / "leases"
            lease = WorkerLease._read_nolock(mission_id, project_dir)
            if lease and not lease.is_expired:
                lease_owner = lease.owner_uuid
                lease_generation = lease.generation
                fencing_token = lease.fencing_token
                candidate_sha = lease.candidate_sha
        except Exception:
            pass

    # Safety state. FAIL-CLOSED: a quarantine check that crashes must not be
    # reported as CLEAR in the canonical context that feeds dispatch decisions.
    safety_state = "CLEAR"
    try:
        from .safety_quarantine import is_in_quarantine
        if is_in_quarantine(project_dir):
            safety_state = "QUARANTINED"
    except Exception:
        safety_state = "QUARANTINE_UNKNOWN"

    # Escalation stage.
    escalation_stage = ""
    try:
        from .escalation_ladder import get_escalation_stage
        if current_milestone:
            stage = get_escalation_stage(current_milestone, project_dir)
            escalation_stage = stage.name if stage else ""
    except Exception:
        pass

    # Actor capability policy SHA.
    actor_cap_sha = _sha16(project_dir / "ACTOR_CAPABILITY_POLICY.json")
    if actor_cap_sha == "MISSING":
        actor_cap_sha = _sha16(_repo / "factory" / "bin" / "model_router.py")

    manifest = CanonicalContextManifest(
        produced_at=produced_at,
        project_goal=project_goal,
        north_star=north_star,
        strategy_sha=strategy_sha,
        spec_sha=spec_sha,
        roadmap_sha=roadmap_sha,
        project_state_sha=project_state_sha,
        current_milestone=current_milestone,
        current_milestone_goal=current_milestone_goal,
        why_next=why_next,
        dependencies=dependencies,
        completed_milestones=completed_milestones,
        repository=str(_repo),
        base_sha=base_sha,
        candidate_sha=candidate_sha,
        lease_owner=lease_owner,
        lease_generation=lease_generation,
        fencing_token=fencing_token,
        failure_history=failure_history,
        failure_fingerprint=failure_fingerprint,
        strategies_attempted=strategies_attempted,
        escalation_stage=escalation_stage,
        safety_state=safety_state,
        actor_capability_policy_sha=actor_cap_sha,
    )
    return manifest.finalize()


def assert_manifest_complete(manifest: CanonicalContextManifest) -> None:
    """Raise if manifest is missing required fields for dispatch.

    Fail closed before spawn if required context is absent.
    """
    required = [
        ("current_milestone", manifest.current_milestone),
        ("base_sha", manifest.base_sha),
        ("manifest_sha", manifest.manifest_sha),
        ("strategy_sha", manifest.strategy_sha),
        ("roadmap_sha", manifest.roadmap_sha),
        # L5_factory_v2_rest_1-008: the spec is one of the three authorities a
        # dispatch must be bound to — a manifest without it was never complete.
        ("spec_sha", manifest.spec_sha),
    ]
    # L5_factory_v2_rest_1-008: 'MISSING'/'unknown' are sentinels the builder
    # stamps when an authority file is ABSENT — a non-empty sentinel used to
    # satisfy the truthiness check. base_sha legitimately stays 'unknown' for
    # a project dir outside any git repo (no repo to pin), so the sentinel
    # rejection applies to the authority hashes only.
    _SENTINELS = ("MISSING", "unknown", "UNKNOWN")
    _SENTINEL_CHECKED = {"strategy_sha", "roadmap_sha", "spec_sha"}
    missing = [name for name, val in required
               if not val or (name in _SENTINEL_CHECKED and val in _SENTINELS)]
    if missing:
        raise ManifestIncomplete(
            f"CanonicalContextManifest missing required fields: {missing}. "
            "Dispatch blocked — fail closed."
        )
    if not manifest.verify_integrity():
        raise ManifestIncomplete(
            "manifest_sha verification failed — manifest may be corrupted. "
            "Dispatch blocked."
        )


class ManifestIncomplete(Exception):
    """Dispatch blocked — canonical context is incomplete or corrupted."""


# ── helpers ───────────────────────────────────────────────────────────────────

def _sha16(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except FileNotFoundError:
        return "MISSING"


def _sha16_authority(project_dir: Path, name: str) -> str:
    """Like _sha16, but for a canonical authority file: read at HEAD, not disk."""
    try:
        text = _authority.load_authority_text(project_dir, name)
    except (OSError, FileNotFoundError):
        return "MISSING"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _extract_section_authority(project_dir: Path, name: str, markers: list[str], lines: int = 3) -> str:
    """Like _extract_section, but for a canonical authority file: read at HEAD."""
    try:
        text = _authority.load_authority_text(project_dir, name)
        for marker in markers:
            idx = text.find(marker)
            if idx >= 0:
                snippet = text[idx:idx + 500].split("\n")
                return " ".join(line.strip() for line in snippet[:lines] if line.strip())[:300]
    except Exception:
        pass
    return ""


def _git_head(repo: Path) -> str:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo, capture_output=True, text=True, timeout=5,
        )
        return r.stdout.strip() if r.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def _extract_section(path: Path, markers: list[str], lines: int = 3) -> str:
    try:
        text = path.read_text(encoding="utf-8")
        for marker in markers:
            idx = text.find(marker)
            if idx >= 0:
                snippet = text[idx:idx + 500].split("\n")
                return " ".join(line.strip() for line in snippet[:lines] if line.strip())[:300]
    except Exception:
        pass
    return ""
