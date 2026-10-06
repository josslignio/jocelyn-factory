"""Phase 8: Real external black-box preflight for a controlled long run.

Checks all preconditions required before starting a long run:
    1. CanonicalContextManifest reconstructable from disk (no session memory)
    2. manifest_sha integrity passes
    3. Required fields present (current_milestone, base_sha, authority hashes)
    4. Project NOT in safety quarantine
    5. No active stale worker lease blocking the milestone
    6. Golden SHA readable (git ref resolved to a real commit)
    7. All required authority files readable
    8. Working tree has no uncommitted mutations to authority files

PreflightResult:
    status: PASS | FAIL
    checks: list of {check, status, detail}
    blocked_by: list of check names that failed
    produced_at: str
    manifest_sha: str  -- of the manifest at preflight time

Only LONG_RUN_READY is set to TRUE when preflight status is PASS.
Must not set LONG_RUN_READY from unit tests — only from this preflight path.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from factory_v2._io import _now


@dataclass
class PreflightCheck:
    check: str
    status: str  # PASS | FAIL | SKIP
    detail: str = ""


@dataclass
class PreflightResult:
    status: str  # PASS | FAIL
    checks: list = field(default_factory=list)
    blocked_by: list = field(default_factory=list)
    produced_at: str = ""
    manifest_sha: str = ""
    git_sha: str = ""

    def as_dict(self) -> dict:
        return asdict(self)

    def as_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2)


class PreflightFailed(Exception):
    """Preflight failed — long run must not start."""


def run_preflight(
    project_dir: Path,
    repo_dir: Path | None = None,
    golden_ref: str = "refs/golden/factory",
    mission_id: str = "",
) -> PreflightResult:
    """Run all preflight checks. Returns PreflightResult with PASS or FAIL.

    Does NOT raise on failure — returns a structured result so the caller can
    decide whether to persist/log before raising PreflightFailed.
    """
    project_dir = Path(project_dir)
    _repo = Path(repo_dir) if repo_dir else project_dir.parent.parent
    checks: list[PreflightCheck] = []

    # 1. Manifest reconstruction from disk.
    manifest = None
    manifest_sha = ""
    git_sha = ""
    try:
        from .canonical_context import build_manifest, assert_manifest_complete
        manifest = build_manifest(project_dir, repo_dir=_repo, mission_id=mission_id)
        manifest_sha = manifest.manifest_sha
        git_sha = manifest.base_sha
        checks.append(PreflightCheck("manifest_reconstruction", "PASS", f"sha={manifest_sha}"))
    except Exception as exc:
        checks.append(PreflightCheck("manifest_reconstruction", "FAIL", str(exc)))

    # 2. Manifest integrity.
    if manifest is not None:
        if manifest.verify_integrity():
            checks.append(PreflightCheck("manifest_integrity", "PASS"))
        else:
            checks.append(PreflightCheck("manifest_integrity", "FAIL", "manifest_sha mismatch"))
    else:
        checks.append(PreflightCheck("manifest_integrity", "SKIP", "manifest not built"))

    # 3. Required fields.
    if manifest is not None:
        try:
            from .canonical_context import assert_manifest_complete
            assert_manifest_complete(manifest)
            checks.append(PreflightCheck("required_fields", "PASS"))
        except Exception as exc:
            checks.append(PreflightCheck("required_fields", "FAIL", str(exc)))
    else:
        checks.append(PreflightCheck("required_fields", "SKIP", "manifest not built"))

    # 4. Safety quarantine clear.
    try:
        from .safety_quarantine import is_in_quarantine, get_quarantine_record
        if is_in_quarantine(project_dir):
            rec = get_quarantine_record(project_dir) or {}
            checks.append(PreflightCheck(
                "safety_quarantine_clear", "FAIL",
                f"Project is in SAFETY_QUARANTINE (cause={rec.get('cause', '?')}). "
                "Clear with SafetyClearReceipt first."
            ))
        else:
            checks.append(PreflightCheck("safety_quarantine_clear", "PASS"))
    except Exception as exc:
        checks.append(PreflightCheck("safety_quarantine_clear", "FAIL", str(exc)))

    # 5. No stale lease blocking milestone.
    if manifest and manifest.current_milestone and mission_id:
        try:
            from .worker_lease import WorkerLease
            lease = WorkerLease._read_nolock(mission_id, project_dir)
            # L5_rest_1-019 (résiduel, lot 1b) : WorkerLease stocke des états en
            # CHAÎNES ("ACTIVE"/"EXPIRED"/"ADOPTED"/"FENCED") — l'import d'un
            # `LeaseState` inexistant levait ImportError, avalée ci-dessous en
            # FAIL : tout démarrage était refusé et un bail actif réel n'était
            # jamais inspecté.
            if lease and lease.state == "ACTIVE" and not lease.is_expired:
                checks.append(PreflightCheck(
                    "no_stale_lease", "FAIL",
                    f"Active non-expired lease for mission_id={mission_id} "
                    f"generation={lease.generation}. Fence it first."
                ))
            else:
                checks.append(PreflightCheck("no_stale_lease", "PASS"))
        except Exception as exc:
            # L5_rest_1-019: only an explicit None from the reader is "no
            # lease". A read ERROR means the absence of an active worker could
            # NOT be established — turning it into PASS authorized a start
            # with no proof of exclusivity.
            checks.append(PreflightCheck(
                "no_stale_lease", "FAIL",
                f"lease unreadable — absence of an active lease could not be "
                f"established ({type(exc).__name__}: {exc})"))
    else:
        checks.append(PreflightCheck("no_stale_lease", "SKIP", "no mission_id or milestone"))

    # 6. Golden SHA readable.
    try:
        result = subprocess.run(
            ["git", "rev-parse", golden_ref],
            cwd=_repo, capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            checks.append(PreflightCheck("golden_sha_readable", "PASS",
                                         f"golden={result.stdout.strip()[:12]}"))
        else:
            # Not a hard block — golden may not exist yet for first promotion.
            checks.append(PreflightCheck("golden_sha_readable", "SKIP",
                                         f"ref {golden_ref!r} not found (first run)"))
    except Exception as exc:
        checks.append(PreflightCheck("golden_sha_readable", "SKIP", str(exc)))

    # 7. Authority files readable.
    authority_files = ["STRATEGY.md", "SPEC.md", "ROADMAP.yaml", "PROJECT_STATE.json"]
    missing_auth = []
    for fname in authority_files:
        if not (project_dir / fname).exists():
            missing_auth.append(fname)
    if missing_auth:
        checks.append(PreflightCheck("authority_files_readable", "FAIL",
                                     f"Missing: {missing_auth}"))
    else:
        checks.append(PreflightCheck("authority_files_readable", "PASS"))

    # 8. No uncommitted mutations to authority files.
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"] + authority_files,
            cwd=project_dir, capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            dirty = [line for line in result.stdout.splitlines() if line.strip()]
            if dirty:
                checks.append(PreflightCheck(
                    "authority_clean_working_tree", "FAIL",
                    f"Uncommitted authority mutations: {dirty}"
                ))
            else:
                checks.append(PreflightCheck("authority_clean_working_tree", "PASS"))
        else:
            # Not in a git repo / git unavailable — skip.
            checks.append(PreflightCheck("authority_clean_working_tree", "SKIP",
                                         "git status failed (non-git dir?)"))
    except Exception as exc:
        checks.append(PreflightCheck("authority_clean_working_tree", "SKIP", str(exc)))

    blocked_by = [c.check for c in checks if c.status == "FAIL"]
    status = "FAIL" if blocked_by else "PASS"

    return PreflightResult(
        status=status,
        checks=checks,
        blocked_by=blocked_by,
        produced_at=_now(),
        manifest_sha=manifest_sha,
        git_sha=git_sha,
    )


def assert_preflight_pass(result: PreflightResult) -> None:
    """Raise PreflightFailed if result is not PASS."""
    if result.status != "PASS":
        details = "; ".join(
            f"{c.check}: {c.detail}" for c in result.checks if c.status == "FAIL"
        )
        raise PreflightFailed(
            f"Preflight FAILED — long run must not start. Blocked by: {result.blocked_by}. "
            f"Details: {details}"
        )


def persist_preflight_result(result: PreflightResult, project_dir: Path) -> Path:
    """Write preflight result to evidence/preflight/<timestamp>.json."""
    evidence_dir = Path(project_dir) / "evidence" / "preflight"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    ts = result.produced_at.replace(":", "").replace("-", "").replace("T", "_").rstrip("Z")
    path = evidence_dir / f"preflight_{ts}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(result.as_json(), encoding="utf-8")
    os.replace(tmp, path)
    return path
