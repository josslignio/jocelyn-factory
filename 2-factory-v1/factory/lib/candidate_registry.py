"""CandidateRegistry — per-mission candidate SHA registry (F004).

Replaces the single global `candidate_sha` (stale after every agent commit, advanced
manually by the Conductor) with one entry per mission under
factory/projects/factory-control/evidence/factory-v1/candidate_registry.json.

Safety invariants enforced here:
- QRTS NEVER advances without passing qualification evidence (promote_qrts guards on
  evidence.passed — fail-closed, QualificationError otherwise);
- every mutation happens under an exclusive flock + atomic tmp-replace, so concurrent
  writers (4 parallel workers, F003 burst) cannot corrupt state (last-writer-wins
  forbidden);
- a corrupted registry file raises RegistryError instead of being silently re-seeded
  (a silent wipe could resurrect a lost/advanced QRTS);
- `global_candidate_sha` is kept for stop_gate compatibility = sha of the most recently
  updated candidate (max by updated_at), falling back to QRTS when no candidate exists.
"""

import contextlib
import fcntl
import json
from datetime import datetime, timezone
from pathlib import Path
from factory_v2._io import _file_lock

DEFAULT_QRTS = "d6398b21"  # qualified runtime subject at F004 time — requires full qualification evidence to change
CANDIDATE_STATUSES = ("BUILDING", "TESTING", "REVIEWING", "ACCEPTED", "REJECTED")


class QualificationError(RuntimeError):
    """QRTS promotion attempted without passing qualification evidence."""


class RegistryError(RuntimeError):
    """Registry state corrupted or inconsistent — fail-closed, never auto-wiped."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CandidateRegistry:
    def __init__(self, registry_path: Path):
        self.path = Path(registry_path)

    def _lock(self):
        return _file_lock(self.path.with_suffix(".lock"))

    def _defaults(self) -> dict:
        return {"qrts": DEFAULT_QRTS, "candidates": {}, "global_candidate_sha": DEFAULT_QRTS}

    def _load(self) -> dict:
        if not self.path.exists():
            return self._defaults()
        try:
            data = json.loads(self.path.read_text())
        except json.JSONDecodeError as exc:
            raise RegistryError(f"corrupted registry {self.path}: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("candidates", {}), dict):
            raise RegistryError(f"corrupted registry {self.path}: unexpected structure")
        data.setdefault("qrts", DEFAULT_QRTS)
        data.setdefault("candidates", {})
        data.setdefault("global_candidate_sha", data["qrts"])
        return data

    def _save(self, data: dict) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        tmp.replace(self.path)

    def load(self) -> dict:
        with self._lock():
            return self._load()

    @staticmethod
    def _latest_candidate_sha(data: dict) -> str:
        candidates = data.get("candidates", {})
        if not candidates:
            return data.get("qrts", DEFAULT_QRTS)
        latest = max(candidates.values(), key=lambda c: c.get("updated_at", ""))
        return latest.get("sha", data.get("qrts", DEFAULT_QRTS))

    def register_candidate(self, mission_id: str, sha: str, base_sha: str) -> None:
        """Atomic: add/update candidate for mission_id. Never touches QRTS."""
        with self._lock():
            data = self._load()
            data["candidates"][mission_id] = {
                "sha": sha,
                "base_sha": base_sha,
                "status": "BUILDING",
                "updated_at": _utc_now(),
            }
            data["global_candidate_sha"] = self._latest_candidate_sha(data)
            self._save(data)

    def update_status(self, mission_id: str, status: str) -> None:
        """Atomic: update status (BUILDING/TESTING/REVIEWING/ACCEPTED/REJECTED). No-op if unknown."""
        if status not in CANDIDATE_STATUSES:
            raise RegistryError(f"unknown candidate status {status!r} (allowed: {CANDIDATE_STATUSES})")
        with self._lock():
            data = self._load()
            if mission_id in data["candidates"]:
                data["candidates"][mission_id]["status"] = status
                data["candidates"][mission_id]["updated_at"] = _utc_now()
                data["global_candidate_sha"] = self._latest_candidate_sha(data)
            self._save(data)

    def promote_qrts(self, sha: str, evidence) -> None:
        """ONLY promote QRTS with passing qualification evidence for THIS sha. Atomic."""
        if evidence is None or not getattr(evidence, "passed", False):
            raise QualificationError(f"cannot promote QRTS without passing evidence for {sha}")
        # L6_bin_lib_products_1-026: getattr(evidence, "sha", sha) defaulted a
        # MISSING sha to the requested one — any passed=True evidence object
        # without a sha promoted any QRTS, breaking 'passing evidence for THIS
        # sha'. The evidence's sha must be PRESENT and EXACTLY the target.
        evidence_sha = getattr(evidence, "sha", None)
        if evidence_sha is None:
            raise QualificationError(
                f"qualification evidence carries no sha — cannot bind it to {sha!r}")
        if evidence_sha != sha:
            raise QualificationError(
                f"qualification evidence sha mismatch: evidence={evidence_sha!r} != {sha!r}")
        with self._lock():
            data = self._load()
            data["qrts"] = sha[:8]
            data["qrts_promoted_at"] = _utc_now()
            self._save(data)
