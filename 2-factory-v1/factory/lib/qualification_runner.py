"""QualificationRunner (F004) — runs the test suite for a candidate SHA. NEVER promotes QRTS.

run_qualification() returns a QualificationResult and writes NOTHING: promotion is a
separate human/CTO decision executed via CandidateRegistry.promote_qrts (which itself
requires passed=True). Fail-closed: if the suite cannot run (missing pytest/binary,
crash), the result is passed=False, never an exception-based silent success.

CLI: python3 factory/lib/qualification_runner.py [--sha <sha>] [--repo-root <path>]
     stdout = one JSON object (the QualificationResult), rc=0 iff passed.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class QualificationResult:
    sha: str
    passed: bool
    returncode: int | None = None
    test_count: int | None = None
    command: str = ""
    summary: str = ""
    executed_at: str = ""
    #: L6_bin_lib_products_2-001: the commit the suite ACTUALLY ran against —
    # observed, never copied from the request. Empty when the runner could
    # not observe it (non-git directory: legacy in-place run).
    tested_sha: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_test_count(output: str) -> int | None:
    counts = re.findall(r"(\d+) (?:passed|failed|skipped|error[s]?)", output)
    total = sum(int(c) for c in counts)
    return total or None


def run_qualification(sha: str, repo_root: Path) -> QualificationResult:
    """Run the pytest suite at repo_root. NEVER promotes QRTS.

    L6_bin_lib_products_2-001: when repo_root is a git repository, the suite
    runs in a clean DETACHED worktree pinned to the exact requested SHA — a
    green suite on a different checkout must never qualify a SHA it did not
    test. The result records the SHA that was actually observed under test.
    A non-git directory keeps the legacy in-place run (tested_sha stays "").
    """
    import shutil
    import tempfile

    repo_root = Path(repo_root).resolve()
    cmd = [sys.executable, "-m", "pytest", "tests/", "-q", "--tb=short"]
    executed_at = _utc_now()

    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo_root),
                          capture_output=True, text=True)
    is_git = head.returncode == 0 and head.stdout.strip()

    run_dir: Path = repo_root
    worktree = ""
    if is_git:
        worktree = tempfile.mkdtemp(prefix="qualify-")
        add = subprocess.run(["git", "worktree", "add", "--detach", worktree, sha],
                             cwd=str(repo_root), capture_output=True, text=True)
        if add.returncode != 0:
            shutil.rmtree(worktree, ignore_errors=True)
            return QualificationResult(
                sha=sha, passed=False, returncode=None, command=" ".join(cmd),
                summary=(f"qualification could not pin requested sha {sha[:12]}: "
                         f"{add.stderr.strip()[:200]}"),
                executed_at=executed_at)
        run_dir = Path(worktree)

    def _finish(result: "subprocess.CompletedProcess | None", summary: str,
                passed: bool) -> QualificationResult:
        tested = ""
        if worktree:
            pin = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(run_dir),
                                 capture_output=True, text=True)
            tested = pin.stdout.strip() if pin.returncode == 0 else ""
            subprocess.run(["git", "worktree", "remove", "--force", worktree],
                           cwd=str(repo_root), capture_output=True)
            shutil.rmtree(worktree, ignore_errors=True)
        return QualificationResult(
            sha=sha, passed=passed and (not is_git or tested == sha),
            returncode=None if result is None else result.returncode,
            test_count=None if result is None else _parse_test_count(
                (result.stdout or "") + "\n" + (result.stderr or "")),
            command=" ".join(cmd),
            summary=summary if not is_git or tested == sha else (
                f"tested HEAD {tested[:12]} != requested {sha[:12]}"),
            executed_at=executed_at, tested_sha=tested or head.stdout.strip(),
        )

    try:
        result = subprocess.run(cmd, cwd=str(run_dir), capture_output=True, text=True)
    except OSError as exc:
        return _finish(None, f"qualification could not run: {exc}", False)
    output = result.stdout + "\n" + result.stderr
    summary_lines = [ln for ln in result.stdout.strip().splitlines() if ln.strip()]
    return _finish(result, summary_lines[-1] if summary_lines else "",
                   result.returncode == 0)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run qualification suite for a candidate SHA (never promotes QRTS)")
    parser.add_argument("--sha", default=None,
                        help="candidate SHA to qualify (default: git rev-parse HEAD of --repo-root)")
    parser.add_argument("--repo-root", default=str(REPO_ROOT), help="repository to qualify (default: this repo)")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    sha = args.sha
    if sha is None:
        cp = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo_root), capture_output=True, text=True)
        if cp.returncode != 0:
            print(json.dumps({"error": f"cannot resolve HEAD in {repo_root}: {cp.stderr.strip()}"}))
            return 2
        sha = cp.stdout.strip()

    result = run_qualification(sha, repo_root)
    print(json.dumps(result.to_dict()))
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
