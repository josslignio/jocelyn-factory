"""LR5: LONG_RUN_READY is a transaction, never a flag write.

Roadmap counterexample (LR5_PREFLIGHT_EXACT_SHA): "LONG_RUN_READY=True only via a
transaction re-verifying the 3 evidence bundles from disk in a separate verifier
process (a direct set_long_run_ready() call without bundles = FAIL)".

The three bundles:
  1. the LR5 preflight receipt (with observation evidence paths);
  2. the preflight checks result (long_run_preflight.PreflightResult JSON);
  3. the controlled long run result (run_controlled_long_run result JSON).

set_long_run_ready() spawns `python3 -m factory_v2.verify_long_run_ready` as a REAL
subprocess.  The verifier re-reads all three bundles from disk, re-checks their
structure and cross-consistency (statuses, integration SHA reality, observation
artifacts present and parseable) and exits 0 only when everything holds.  Only then
does the caller set `long_run_ready=true` — atomically, with the integration SHA and
timestamp — in PROJECT_STATE.json.  Any failure leaves the flag untouched and raises
LongRunReadyRejected with the verifier's typed findings.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from factory_v2._io import _now


class LongRunReadyRejected(Exception):
    """The evidence bundles did not verify — LONG_RUN_READY stays unset."""


def set_long_run_ready(
    project_dir: Path,
    *,
    receipt_path: Path,
    preflight_evidence_path: Path,
    long_run_evidence_path: Path,
    repo_dir: "Path | None" = None,
    verifier_timeout_s: int = 300,
) -> str:
    """Set long_run_ready=true only after a separate verifier accepts the bundles.

    Returns the verified integration SHA.  Raises LongRunReadyRejected otherwise;
    in that case PROJECT_STATE.json is not modified.
    """
    project_dir = Path(project_dir)
    # The verifier module lives in THIS repo, regardless of where the target project
    # sits — never derive the verifier's repo from the project layout.
    repo_dir = Path(repo_dir) if repo_dir else Path(__file__).resolve().parent.parent
    verifier = Path(__file__).resolve().parent / "verify_long_run_ready.py"

    cmd = [
        sys.executable, "-m", "factory_v2.verify_long_run_ready",
        "--project-dir", str(project_dir),
        "--repo-dir", str(repo_dir),
        "--receipt", str(Path(receipt_path).resolve()),
        "--preflight", str(Path(preflight_evidence_path).resolve()),
        "--long-run", str(Path(long_run_evidence_path).resolve()),
    ]
    env = dict(os.environ)
    proc = subprocess.run(cmd, cwd=str(repo_dir), capture_output=True, text=True,
                          timeout=verifier_timeout_s, env=env)
    if proc.returncode != 0:
        raise LongRunReadyRejected(
            f"verify_long_run_ready exited {proc.returncode}:\n"
            f"{proc.stdout[-2000:]}\n{proc.stderr[-1000:]}")
    try:
        verdict = json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception as exc:
        raise LongRunReadyRejected(f"verifier output unparseable: {exc}") from exc
    integration_sha = verdict.get("integration_sha", "")

    state_path = project_dir / "PROJECT_STATE.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["long_run_ready"] = True
    state["long_run_ready_sha"] = integration_sha
    state["long_run_ready_set_at"] = _now()
    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    os.replace(tmp, state_path)
    return integration_sha
