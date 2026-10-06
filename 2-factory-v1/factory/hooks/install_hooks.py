#!/usr/bin/env python3
"""Install the F004 post-commit hook (NOT a pre-commit hook) into a repo's git hooks dir.

The hooks dir is resolved via `git rev-parse --git-path hooks` because this Factory repo
runs its missions inside git worktrees where `.git` is a FILE (gitdir pointer), not a
directory — a naive repo_root/".git"/"hooks" would fail there. The hook itself is
re-installed by `factoryd start` after any fresh `git clone` (hooks are not versioned).

CLI: python3 factory/hooks/install_hooks.py [--repo-root <path>]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

HOOK_CONTENT = """#!/bin/sh
# Installed by factory/hooks/install_hooks.py (F004) — post-commit candidate registration.
MISSION_ID="${FACTORY_MISSION_ID:-UNKNOWN}"
python3 "$(git rev-parse --show-toplevel)/factory/hooks/post_commit.py" \\
    "$(git rev-parse HEAD)" "$MISSION_ID"
"""


def _hooks_dir(repo_root: Path) -> Path:
    cp = subprocess.run(["git", "rev-parse", "--git-path", "hooks"],
                        cwd=str(repo_root), capture_output=True, text=True)
    if cp.returncode == 0 and cp.stdout.strip():
        p = Path(cp.stdout.strip())
        return p if p.is_absolute() else (repo_root / p)
    return repo_root / ".git" / "hooks"


def install_post_commit_hook(repo_root: Path) -> Path:
    repo_root = Path(repo_root).resolve()
    hook_path = _hooks_dir(repo_root) / "post-commit"
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    hook_path.write_text(HOOK_CONTENT)
    hook_path.chmod(0o755)  # required for git to execute the hook
    return hook_path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Install the F004 post-commit hook")
    parser.add_argument("--repo-root", default=str(REPO_ROOT),
                        help="repository to install into (default: this repo)")
    args = parser.parse_args(argv)
    hook_path = install_post_commit_hook(Path(args.repo_root))
    print(f"[install_hooks] post-commit hook installed: {hook_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
