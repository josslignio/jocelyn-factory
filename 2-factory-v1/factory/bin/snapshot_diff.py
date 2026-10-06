#!/usr/bin/env python3
"""Factory-owned candidate capture — FD1 fix.

dispatcher_v3.py calls snapshot() before and after running a worker step.
diff() returns the set of files that changed. factory_commit() commits them.
Workers never need to run git commands.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class WorkingTreeSnapshot:
    repo: Path
    tracked_hashes: dict  # {relative_path: sha1_blob_hash}
    untracked_paths: set  # relative paths of untracked files

    def __repr__(self) -> str:
        return f"WorkingTreeSnapshot(tracked={len(self.tracked_hashes)}, untracked={len(self.untracked_paths)})"


def snapshot(repo: str | Path) -> WorkingTreeSnapshot:
    """Capture the current working-tree state for a git repo.

    Uses:
    - git ls-files -s  : blob hashes of tracked (staged or committed) files
    - git ls-files --others --exclude-standard : untracked files
    """
    repo = Path(repo).resolve()

    # Tracked file hashes
    tracked = {}
    result = subprocess.run(
        ["git", "ls-files", "-s"],
        cwd=repo, capture_output=True, text=True, check=True
    )
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 4:
            blob_hash = parts[1]
            filepath = " ".join(parts[3:])
            tracked[filepath] = blob_hash

    # Working tree state (includes unstaged modifications)
    status_result = subprocess.run(
        ["git", "status", "--porcelain=v1"],
        cwd=repo, capture_output=True, text=True, check=True
    )
    modified_hashes = {}
    untracked = set()
    for line in status_result.stdout.splitlines():
        if len(line) < 4:
            continue
        xy = line[:2]
        filepath = line[3:].strip()
        if xy.strip() in ("??",):
            untracked.add(filepath)
        elif "M" in xy or "A" in xy or "D" in xy or "R" in xy:
            # File is modified; record current content hash if it exists.
            # L6_bin_lib_products_1-020: a tracked file deleted from the
            # WORKING TREE without staging kept its INDEX hash before — the
            # deletion was invisible to diff(). Drop it from the snapshot so
            # diff() reports it in `deleted`.
            full = repo / filepath
            if not full.exists():
                tracked.pop(filepath, None)
                continue
            h = subprocess.run(
                ["git", "hash-object", filepath],
                cwd=repo, capture_output=True, text=True, check=False
            )
            if h.returncode == 0:
                modified_hashes[filepath] = h.stdout.strip()

    tracked.update(modified_hashes)
    return WorkingTreeSnapshot(repo=repo, tracked_hashes=tracked, untracked_paths=untracked)


@dataclass
class WorkingTreeDiff:
    repo: Path
    added: set    # new files (tracked or untracked before, now exist)
    modified: set # existing files whose content changed
    deleted: set  # files that existed before, now gone
    untracked_new: set  # new untracked files not in before

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.modified or self.deleted or self.untracked_new)

    @property
    def all_changed(self) -> set:
        # L6_bin_lib_products_1-020: deletions ARE changes — a commit composed
        # solely of removals used to be reported as "nothing changed".
        return self.added | self.modified | self.deleted | self.untracked_new


def diff(before: WorkingTreeSnapshot, after: WorkingTreeSnapshot) -> WorkingTreeDiff:
    """Compute what changed between two snapshots."""
    assert before.repo == after.repo, "snapshots must be from the same repo"

    added = set()
    modified = set()
    deleted = set()
    untracked_new = set()

    all_paths = set(before.tracked_hashes) | set(after.tracked_hashes)
    for path in all_paths:
        b = before.tracked_hashes.get(path)
        a = after.tracked_hashes.get(path)
        if b is None and a is not None:
            added.add(path)
        elif b is not None and a is None:
            deleted.add(path)
        elif b != a:
            modified.add(path)

    # New untracked files
    untracked_new = after.untracked_paths - before.untracked_paths

    return WorkingTreeDiff(
        repo=before.repo,
        added=added,
        modified=modified,
        deleted=deleted,
        untracked_new=untracked_new,
    )


def factory_commit(
    repo: str | Path,
    changed_files: set,
    mission_id: str,
    step: str,
    message: Optional[str] = None,
    ai_trace: Optional[str] = None,
) -> str:
    """Stage changed files and commit on behalf of the worker.

    Returns the new HEAD sha.
    """
    repo = Path(repo).resolve()

    if not changed_files:
        raise ValueError("No files to commit")

    # Stage all changed files (including untracked)
    for f in sorted(changed_files):
        full = repo / f
        if full.exists():
            subprocess.run(["git", "add", f], cwd=repo, check=True)
        else:
            # File was deleted
            subprocess.run(["git", "rm", "--cached", f], cwd=repo, check=False)

    # FD1/L001 (X105 class defect): workers that commit directly via git leave
    # the tree clean — the index blob hashes moved (so snapshot diff reports
    # changes) but nothing is stageable. Deterministic staged-empty check: the
    # worker's own HEAD commit IS the factory capture. Return it instead of
    # failing on `git commit` ("nothing to commit" CalledProcessError).
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    if not staged:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo, capture_output=True, text=True, check=True
        ).stdout.strip()

    # Build commit message
    if message is None:
        trace = f"\nAI_EXECUTION_TRACE: {ai_trace}" if ai_trace else ""
        message = (
            f"factory({mission_id.lower()}): {step} — Factory-owned candidate capture\n\n"
            f"Files changed: {sorted(changed_files)}{trace}"
        )

    result = subprocess.run(
        # L6_bin_lib_products_1-021: `--only -- <pathspec>` confines the
        # commit's content to changed_files regardless of what else is staged
        # in the index — a plain `git commit` would silently absorb files
        # staged by any other operation.
        ["git", "commit", "--only", "-m", message, "--", *sorted(changed_files)],
        cwd=repo, capture_output=True, text=True, check=True
    )

    # Return new HEAD sha
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()

    return sha
