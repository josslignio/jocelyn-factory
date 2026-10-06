#!/usr/bin/env python3
"""Post-commit hook (F004): registers the just-committed SHA per mission. Never touches QRTS.

Called by .git/hooks/post-commit (installed by factory/hooks/install_hooks.py) as:
    python3 post_commit.py <sha> <mission_id>
or imported (on_candidate_commit) for tests. Backward-compat: also advances
`candidate_sha`/`candidate_sha_full` in the EXISTING runtime-receipt.builder.json next
to the registry — SECURITY: if the receipt file does not exist, it is NEVER fabricated
(stop_gate trusts receipts with exit_code==0; creating one here would be a fail-open).
QRTS promotion is out of scope by design: only promote_qrts with passing evidence can.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # factory_v2._io (candidate_registry)

from candidate_registry import CandidateRegistry  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
# FACTORY_STATE_OUT_OF_REPO_V1: candidate_registry.json is daemon runtime state, not
# repo state — it moved to the run-dir (same default as factoryd.py). Prefer the
# legacy in-repo path IFF it still exists on disk (pre-migration repos / test
# fixtures), else the real run-dir location.
_LEGACY_REGISTRY_PATH = REPO_ROOT / "factory/projects/factory-control/evidence/factory-v1/candidate_registry.json"
if _LEGACY_REGISTRY_PATH.exists():
    DEFAULT_REGISTRY_PATH = _LEGACY_REGISTRY_PATH
else:
    DEFAULT_REGISTRY_PATH = (
        Path(os.environ.get("FACTORY_RUN_DIR", "").strip()
             or str(Path.home() / "Library/Application Support/JocelynFactory/run"))
        / "projects" / "factory-control" / "evidence" / "factory-v1" / "candidate_registry.json"
    )
REGISTRY_PATH = Path(os.environ.get("FACTORY_CANDIDATE_REGISTRY", str(DEFAULT_REGISTRY_PATH)))
RECEIPT_FILENAME = "runtime-receipt.builder.json"


def _advance_receipt_sha(sha: str) -> bool:
    """Advance candidate_sha in the existing runtime receipt. Never creates a receipt."""
    receipt_path = REGISTRY_PATH.parent / RECEIPT_FILENAME
    if not receipt_path.exists():
        print(f"[post_commit] no runtime receipt at {receipt_path} — not fabricated (QRTS-safe no-op)",
              file=sys.stderr)
        return False
    lock_path = receipt_path.with_suffix(".receipt.lock")
    with open(lock_path, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            receipt = json.loads(receipt_path.read_text())
            receipt["candidate_sha"] = sha[:8]
            receipt["candidate_sha_full"] = sha
            receipt["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
            tmp = receipt_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
            tmp.replace(receipt_path)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)
    return True


def on_candidate_commit(sha: str, mission_id: str | None = None) -> None:
    registry = CandidateRegistry(REGISTRY_PATH)
    base_sha = registry.load()["qrts"]  # candidate always based from QRTS
    registry.register_candidate(mission_id or "UNKNOWN", sha, base_sha)
    _advance_receipt_sha(sha)
    print(f"[post_commit] {mission_id or 'UNKNOWN'}: candidate sha={sha[:8]}")


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print("usage: post_commit.py <sha> [mission_id]", file=sys.stderr)
        return 2
    sha = args[0]
    mission_id = args[1] if len(args) > 1 else os.environ.get("FACTORY_MISSION_ID")
    on_candidate_commit(sha, mission_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
