#!/usr/bin/env python3
"""Candidate-side M0 runner. NON-AUTHORITATIVE (P0.1 patch).

Originally specified (order §4.1) as "the only script authorized to produce the self-host
proof." Superseded mid-campaign by the P0.1 patch: this script cannot be trusted to prove its
own correctness, because it ships as part of the code an M0 builder is allowed to modify. It now
only does the builder's own fast-feedback checkout+test run, for convenience — it never receives
the external proof-directory path, has no code path that writes M0_SELF_HOST_PASS.json, and its
own output is informational only. The actual gate is factory/bootstrap/verify_bootstrap_dod.py,
run separately and independently by bootstrap_factory_v1.sh.

No LLM decision of any kind happens in this script (order P1 clarification): it is a fixed,
deterministic sequence of subprocess calls with no branching on any model output.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "factory" / "vendored"))
import swebench_sentinels as sentinels  # noqa: E402


def isolated_env(scratch: Path) -> dict:
    env = dict(os.environ)
    home = scratch / "isolated_home"
    tmp = scratch / "isolated_tmp"
    cache = scratch / "isolated_cache"
    for d in (home, tmp, cache):
        d.mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(home)
    env["TMPDIR"] = str(tmp)
    env["XDG_CACHE_HOME"] = str(cache)
    return env


def run_candidate_self_check(repo: Path, candidate_sha: str, scratch: Path) -> dict:
    scratch.mkdir(parents=True, exist_ok=True)
    checkout = scratch / "checkout"
    cp = subprocess.run(["git", "worktree", "add", "--detach", str(checkout), candidate_sha],
                         cwd=repo, capture_output=True, text=True)
    if cp.returncode != 0:
        return {"ok": False, "reason": "checkout_failed", "stderr": cp.stderr}
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=checkout,
                               capture_output=True, text=True).stdout.strip()
        if head != candidate_sha:
            return {"ok": False, "reason": "sha_mismatch", "got": head, "expected": candidate_sha}
        env = isolated_env(scratch)
        wrapped = subprocess.run(
            ["bash", "-c",
             f'echo "{sentinels.START_TEST_OUTPUT}"; python3 -m pytest tests/ -q; '
             f'echo "{sentinels.END_TEST_OUTPUT}"'],
            cwd=checkout, env=env, capture_output=True, text=True,
        )
        section = sentinels.extract_test_output(wrapped.stdout)
        return {
            "ok": section is not None and wrapped.returncode == 0,
            "candidate_sha": head,
            "sentinels_present": section is not None,
            "pytest_exit_code": wrapped.returncode,
            "stdout_tail": wrapped.stdout[-2000:],
        }
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(checkout)],
                        cwd=repo, capture_output=True, text=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--candidate-sha", required=True)
    ap.add_argument("--scratch", default=None,
                     help="Builder's own scratch dir — never the external proof directory.")
    args = ap.parse_args()
    repo = Path(args.repo).resolve()
    scratch = Path(args.scratch) if args.scratch else Path(tempfile.mkdtemp(prefix="bootstrap_gate_"))
    result = run_candidate_self_check(repo, args.candidate_sha, scratch)
    print(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
