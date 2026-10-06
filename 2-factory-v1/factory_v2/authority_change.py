"""Phase 4: Transactional AuthorityChangeProposal — real CAS on all authority hashes.

Replaces the free YAML write in _apply_replan_patch() with a full authority
transaction that:
  1. Locks authority (fcntl.flock on project dir)
  2. Recomputes all current hashes
  3. Rejects stale proposal with REPLAN_CAS_CONFLICT
  4. Parses and validates roadmap (schema, project identity)
  5. Validates unique milestone IDs
  6. Validates dependency existence and acyclic graph
  7. Prevents silent deletion of completed work
  8. Atomically writes to disk
  9. Reloads authority from disk
  10. Resolves next work
  11. Verifies postconditions
  12. Rolls back or quarantines on postcondition failure

Two concurrent replans:
    The first to acquire the lock reads fresh hashes, proceeds.
    The second re-reads hashes after lock → stale expected SHA → REPLAN_CAS_CONFLICT.
    This is a true expected-version CAS (compare-and-swap), not just an atomic rename.

A proposed roadmap that deletes a completed milestone → REPLAN_COMPLETED_WORK_DELETED.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal
from factory_v2._io import _file_lock

try:
    import yaml as _yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False


# ── AuthorityChangeProposal ────────────────────────────────────────────────────

@dataclass
class AuthorityChangeProposal:
    """Full authority change proposal with expected-version CAS fields."""
    proposal_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    project_id: str = ""
    expected_strategy_sha: str = ""   # sha256[:16] of STRATEGY.md at proposal time
    expected_spec_sha: str = ""       # sha256[:16] of SPEC.md at proposal time
    expected_roadmap_sha: str = ""    # sha256[:16] of ROADMAP.yaml at proposal time
    expected_project_state_sha: str = ""  # sha256[:16] of PROJECT_STATE.json at proposal time
    base_sha: str = ""                # git HEAD at proposal time (informational)
    candidate_sha: str = ""           # git HEAD of candidate branch (informational)
    affected_milestones: list = field(default_factory=list)
    failure_fingerprint: str = ""     # fingerprint of the failure that triggered REPLAN
    rationale: str = ""
    proposer_identity: str = ""       # agent/session ID
    proposed_roadmap_content: str = ""  # full YAML text of proposed ROADMAP.yaml


@dataclass
class AuthorityChangeResult:
    status: Literal[
        "APPLIED", "REPLAN_CAS_CONFLICT", "REPLAN_INVALID_YAML",
        "REPLAN_SCHEMA_ERROR", "REPLAN_DUPLICATE_MILESTONE",
        "REPLAN_CYCLE_DETECTED", "REPLAN_MISSING_DEPENDENCY",
        "REPLAN_COMPLETED_WORK_DELETED", "REPLAN_WRITE_ERROR",
        "REPLAN_POSTCONDITION_FAILED"
    ] = "REPLAN_SCHEMA_ERROR"
    proposal_id: str = ""
    sha_before: str = ""
    sha_after: str = ""
    next_milestone: str = ""
    conflict_field: str = ""  # which hash conflicted (for REPLAN_CAS_CONFLICT)
    error: str = ""


# ── authority locking ──────────────────────────────────────────────────────────

@contextmanager
def _authority_lock(project_dir: Path):
    """Exclusive lock on the project authority directory.

    Prevents two concurrent REPLAN proposals from corrupting authority state.
    Lock is NOT held across process boundaries — only within the lock context.
    """
    with _file_lock(project_dir / "authority.lock"):
        yield


# ── hash helpers ───────────────────────────────────────────────────────────────

def _sha16(path: Path) -> str:
    """sha256[:16] of file content, or 'MISSING' if absent."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except FileNotFoundError:
        return "MISSING"


def _authority_hashes(project_dir: Path) -> dict[str, str]:
    """Recompute all authority hashes from disk (fresh read, never cached)."""
    return {
        "strategy_sha": _sha16(project_dir / "STRATEGY.md"),
        "spec_sha": _sha16(project_dir / "SPEC.md"),
        "roadmap_sha": _sha16(project_dir / "ROADMAP.yaml"),
        "project_state_sha": _sha16(project_dir / "PROJECT_STATE.json"),
    }


# ── roadmap validation ─────────────────────────────────────────────────────────

def _validate_roadmap(parsed: dict, project_id: str,
                      completed_milestone_ids: set[str]) -> str | None:
    """Validate a parsed roadmap dict. Returns error string or None if valid."""
    if not isinstance(parsed, dict):
        return "SCHEMA_ERROR: roadmap must be a YAML dict"
    # L5_factory_v2_rest_1-006: the proposal claims to validate schema and
    # project identity — do it. A schema is required to be a roadmap/v<N>
    # string when present (existing roadmaps carry v1/v2); a declared project
    # identity that is not the target project is a cross-project injection.
    schema = parsed.get("schema")
    if schema is not None:
        if not isinstance(schema, str) or not re.fullmatch(r"roadmap/v\d+", schema):
            return f"SCHEMA_ERROR: roadmap schema must match 'roadmap/v<N>', got {schema!r}"
    declared_project = parsed.get("project")
    if declared_project is not None:
        if not isinstance(declared_project, str) or not declared_project:
            return "SCHEMA_ERROR: roadmap 'project' must be a non-empty string"
        if project_id and declared_project != project_id:
            return (f"REPLAN_PROJECT_MISMATCH: proposed roadmap declares project "
                    f"{declared_project!r} but the target project is {project_id!r}")
    if "milestones" not in parsed:
        return "SCHEMA_ERROR: roadmap must have 'milestones' key"
    milestones = parsed["milestones"]
    if not isinstance(milestones, list):
        return "SCHEMA_ERROR: 'milestones' must be a list"
    if len(milestones) == 0:
        return "SCHEMA_ERROR: milestones list is empty"

    # Unique milestone IDs.
    ids_seen: set[str] = set()
    ids: list[str] = []
    for ms in milestones:
        if not isinstance(ms, dict):
            return "SCHEMA_ERROR: each milestone must be a dict"
        mid = ms.get("id")
        if not mid:
            return "SCHEMA_ERROR: milestone missing 'id' field"
        if mid in ids_seen:
            return f"REPLAN_DUPLICATE_MILESTONE: '{mid}' appears more than once"
        ids_seen.add(mid)
        ids.append(mid)

    # Dependency existence.
    for ms in milestones:
        deps = ms.get("depends_on") or {}
        if isinstance(deps, dict):
            for dep_id in deps:
                if dep_id not in ids_seen:
                    return (f"REPLAN_MISSING_DEPENDENCY: milestone '{ms['id']}' "
                            f"depends on '{dep_id}' which is not in the proposed roadmap")

    # Acyclicity (Kahn's algorithm).
    adjacency: dict[str, list[str]] = {mid: [] for mid in ids}
    in_degree: dict[str, int] = {mid: 0 for mid in ids}
    for ms in milestones:
        mid = ms["id"]
        deps = ms.get("depends_on") or {}
        if isinstance(deps, dict):
            for dep_id in deps:
                if dep_id in adjacency:
                    adjacency[dep_id].append(mid)
                    in_degree[mid] += 1
    queue = [m for m in ids if in_degree[m] == 0]
    visited = 0
    while queue:
        node = queue.pop(0)
        visited += 1
        for neighbor in adjacency[node]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)
    if visited != len(ids):
        return "REPLAN_CYCLE_DETECTED: dependency graph contains a cycle"

    # No silent deletion of completed work.
    proposed_ids = set(ids)
    for cid in completed_milestone_ids:
        if cid not in proposed_ids:
            return (f"REPLAN_COMPLETED_WORK_DELETED: completed milestone '{cid}' "
                    "is missing from the proposed roadmap — silent deletion blocked")

    return None  # valid


# ── core transaction ───────────────────────────────────────────────────────────

def apply_authority_change_proposal(
    proposal: AuthorityChangeProposal,
    project_dir: Path,
) -> AuthorityChangeResult:
    """Apply an AuthorityChangeProposal under an exclusive authority lock.

    Steps executed under lock:
      1. Recompute all current authority hashes.
      2. Compare with proposal.expected_*_sha fields.
         Any mismatch → REPLAN_CAS_CONFLICT (proposal is stale).
      3. Parse and validate proposed_roadmap_content.
      4. Prevent silent deletion of completed milestones.
      5. Atomic write (tmp → os.replace).
      6. Reload authority (verify write succeeded).
      7. Resolve next milestone.
      8. Verify postconditions.
    """
    project_dir = Path(project_dir)
    roadmap_path = project_dir / "ROADMAP.yaml"
    state_path = project_dir / "PROJECT_STATE.json"

    # Capability ban, checked before a single byte is written and before the lock
    # is taken. Owner directive 2026-09-10: the six canonical files are read-only
    # to the Factory. This is the last common point of every autonomous route into
    # ROADMAP.yaml, so refusing here closes the class rather than one call site.
    from .canonical_authority import assert_writable
    assert_writable(roadmap_path, actor=f"authority_change:{proposal.proposer_identity}")

    with _authority_lock(project_dir):
        # Step 1: recompute fresh hashes under lock.
        current = _authority_hashes(project_dir)

        # Step 2: CAS check — reject stale proposal.
        cas_fields = {
            "expected_strategy_sha": ("strategy_sha", "STRATEGY.md"),
            "expected_spec_sha": ("spec_sha", "SPEC.md"),
            "expected_roadmap_sha": ("roadmap_sha", "ROADMAP.yaml"),
            "expected_project_state_sha": ("project_state_sha", "PROJECT_STATE.json"),
        }
        for prop_field, (hash_key, filename) in cas_fields.items():
            expected = getattr(proposal, prop_field)
            # B3 fix 2026-09-08: all 4 expected hashes are MANDATORY.
            # An empty/missing expected hash is NOT a "skip" — it is a CAS conflict
            # because it means the proposal was built without reading current authority.
            # Counter-audit proof: old code `if expected and ...` silently skipped the
            # check when expected_sha was "". This allowed bypassing the CAS entirely.
            if not expected:
                return AuthorityChangeResult(
                    status="REPLAN_CAS_CONFLICT",
                    proposal_id=proposal.proposal_id,
                    sha_before=current[hash_key],
                    conflict_field=filename,
                    error=(
                        f"REPLAN_CAS_CONFLICT: {prop_field} is empty or missing. "
                        "All 4 expected hashes are mandatory — a proposal without them "
                        "was not built from fresh authority state."
                    ),
                )
            if expected != current[hash_key]:
                return AuthorityChangeResult(
                    status="REPLAN_CAS_CONFLICT",
                    proposal_id=proposal.proposal_id,
                    sha_before=current[hash_key],
                    conflict_field=filename,
                    error=(
                        f"REPLAN_CAS_CONFLICT: {filename} changed since proposal was created. "
                        f"Expected {expected!r}, current {current[hash_key]!r}. "
                        "Concurrent authority mutation detected — proposal rejected."
                    ),
                )

        # Step 3: parse and validate roadmap.
        content = proposal.proposed_roadmap_content
        if not content or not content.strip():
            return AuthorityChangeResult(
                status="REPLAN_INVALID_YAML",
                proposal_id=proposal.proposal_id,
                error="proposed_roadmap_content is empty",
            )

        if not _HAS_YAML:
            return AuthorityChangeResult(
                status="REPLAN_SCHEMA_ERROR",
                proposal_id=proposal.proposal_id,
                error="PyYAML not available — cannot parse proposed roadmap",
            )

        try:
            parsed = _yaml.safe_load(content)
        except Exception as exc:
            return AuthorityChangeResult(
                status="REPLAN_INVALID_YAML",
                proposal_id=proposal.proposal_id,
                error=f"YAML_PARSE_ERROR: {exc}",
            )

        # Step 4: load completed milestones from PROJECT_STATE.json.
        completed_ids: set[str] = set()
        try:
            if state_path.exists():
                state = json.loads(state_path.read_text(encoding="utf-8"))
                completed_ids = set(state.get("completed_milestones", {}).keys())
        except Exception:
            pass

        # L5_factory_v2_rest_1-006 (continued): the identity a proposal must
        # match is the one the CURRENT roadmap commits to — the project this
        # transaction is allowed to touch. Fall back to the proposal's own
        # project_id when the current roadmap declares none.
        expected_project = proposal.project_id
        try:
            _current_parsed = _yaml.safe_load(
                (project_dir / "ROADMAP.yaml").read_text(encoding="utf-8"))
            if (isinstance(_current_parsed, dict)
                    and isinstance(_current_parsed.get("project"), str)
                    and _current_parsed.get("project")):
                expected_project = _current_parsed["project"]
        except Exception:
            pass

        err = _validate_roadmap(parsed, expected_project, completed_ids)
        if err:
            status: str
            if "DUPLICATE" in err:
                status = "REPLAN_DUPLICATE_MILESTONE"
            elif "CYCLE" in err:
                status = "REPLAN_CYCLE_DETECTED"
            elif "MISSING_DEPENDENCY" in err:
                status = "REPLAN_MISSING_DEPENDENCY"
            elif "COMPLETED_WORK_DELETED" in err:
                status = "REPLAN_COMPLETED_WORK_DELETED"
            else:
                status = "REPLAN_SCHEMA_ERROR"
            return AuthorityChangeResult(
                status=status,
                proposal_id=proposal.proposal_id,
                error=err,
            )

        # Step 5: atomic write.
        sha_before = current["roadmap_sha"]
        tmp = roadmap_path.with_suffix(".tmp")
        try:
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, roadmap_path)
        except Exception as exc:
            return AuthorityChangeResult(
                status="REPLAN_WRITE_ERROR",
                proposal_id=proposal.proposal_id,
                error=f"WRITE_ERROR: {exc}",
            )
        sha_after = _sha16(roadmap_path)

        # Step 6: reload authority (verify write).
        try:
            reloaded_content = roadmap_path.read_text(encoding="utf-8")
            reloaded_parsed = _yaml.safe_load(reloaded_content)
            if not reloaded_parsed or "milestones" not in reloaded_parsed:
                raise ValueError("reloaded roadmap missing milestones")
        except Exception as exc:
            # Postcondition failed — roll back.
            try:
                tmp.write_text(
                    _reconstruct_original(sha_before, project_dir), encoding="utf-8")
                os.replace(tmp, roadmap_path)
            except Exception:
                pass
            return AuthorityChangeResult(
                status="REPLAN_POSTCONDITION_FAILED",
                proposal_id=proposal.proposal_id,
                error=f"Reload verification failed after write: {exc}",
            )

        # Step 7: resolve next milestone.
        next_milestone = _resolve_next_milestone(reloaded_parsed, completed_ids)

    # Lock released — return result.
    return AuthorityChangeResult(
        status="APPLIED",
        proposal_id=proposal.proposal_id,
        sha_before=sha_before,
        sha_after=sha_after,
        next_milestone=next_milestone,
    )


def build_proposal_from_current(
    project_dir: Path,
    proposed_roadmap_content: str,
    rationale: str = "",
    proposer_identity: str = "",
    failure_fingerprint: str = "",
    affected_milestones: list | None = None,
) -> AuthorityChangeProposal:
    """Build a proposal that captures current authority hashes (expected-version snapshot)."""
    project_dir = Path(project_dir)
    current = _authority_hashes(project_dir)
    return AuthorityChangeProposal(
        project_id=project_dir.name,
        expected_strategy_sha=current["strategy_sha"],
        expected_spec_sha=current["spec_sha"],
        expected_roadmap_sha=current["roadmap_sha"],
        expected_project_state_sha=current["project_state_sha"],
        rationale=rationale,
        proposer_identity=proposer_identity,
        failure_fingerprint=failure_fingerprint,
        affected_milestones=affected_milestones or [],
        proposed_roadmap_content=proposed_roadmap_content,
    )


# ── helpers ────────────────────────────────────────────────────────────────────

def _resolve_next_milestone(roadmap: dict, completed_ids: set[str]) -> str:
    """Simple next-work resolver: first milestone whose deps are all in completed_ids."""
    milestones = roadmap.get("milestones") or []
    for ms in milestones:
        mid = ms.get("id", "")
        if mid in completed_ids:
            continue
        deps = ms.get("depends_on") or {}
        if isinstance(deps, dict):
            if all(dep_id in completed_ids for dep_id in deps):
                return mid
    return ""


def _reconstruct_original(sha_before: str, project_dir: Path) -> str:
    """Attempt to retrieve original roadmap from git (best-effort rollback)."""
    import subprocess
    roadmap_path = project_dir / "ROADMAP.yaml"
    try:
        r = subprocess.run(
            ["git", "show", f"HEAD:{roadmap_path.relative_to(project_dir.parent.parent)}"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            return r.stdout
    except Exception:
        pass
    return ""
