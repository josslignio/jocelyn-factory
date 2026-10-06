"""WorktreeManager — isolated git worktree per concurrent mission (F002).

Pure subprocess wrapper around `git worktree` (no GitPython/dulwich). Each mission
gets its own worktree + a dedicated `candidate/<mission_id>` branch so multiple
writers can operate on the same repo simultaneously without git conflicts. Merge and
dispatcher-level integration are out of scope here (F003/F004) — this is the library.
"""

import json
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from worktree_info import WorktreeInfo

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
from factory_v2.disk_guard import DiskSpaceLow, ensure_free_gb  # noqa: E402  FACTORY_REPO_HYGIENE_V1

META_FILENAME = ".factory_worktree_meta.json"


class WorktreeError(RuntimeError):
    pass


class WorktreeManager:
    def __init__(self, repo_root: Path, worktrees_base: Path):
        # Resolved eagerly: `git worktree list` reports canonical paths (e.g. macOS
        # resolves /var -> /private/var), so comparisons against unresolved input
        # paths would silently miss every managed worktree.
        self.repo_root = Path(repo_root).resolve()
        self.worktrees_base = Path(worktrees_base).resolve()
        self.worktrees_base.mkdir(parents=True, exist_ok=True)

    def _run(self, args, cwd=None):
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd or self.repo_root),
            check=True,
            capture_output=True,
            text=True,
        )

    def _branch_name(self, mission_id: str) -> str:
        return f"candidate/{mission_id}"

    def worktree_path(self, mission_id: str) -> Path:
        return self.worktrees_base / mission_id

    def create_worktree(self, mission_id: str, base_branch: str = "HEAD") -> WorktreeInfo:
        path = self.worktree_path(mission_id)
        if path.exists():
            raise WorktreeError(f"worktree already exists for mission {mission_id!r} at {path}")
        branch = self._branch_name(mission_id)
        base_commit = self._run(["rev-parse", base_branch]).stdout.strip()

        # Prune stale worktree registrations (registered path no longer exists on disk).
        # Safe: only removes git's bookkeeping, never deletes branches or commits.
        try:
            self._run(["worktree", "prune"])
        except subprocess.CalledProcessError:
            pass

        # Check if the candidate branch already exists.
        branch_exists = False
        try:
            self._run(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"])
            branch_exists = True
        except subprocess.CalledProcessError:
            pass

        if branch_exists:
            # Detect whether this branch is still attached to a live worktree.
            attached_path = self._find_worktree_for_branch(branch)
            if attached_path is not None:
                raise WorktreeError(
                    f"branch {branch!r} is attached to a live worktree at {attached_path}: "
                    "second writer detected — cannot create duplicate"
                )

            # True orphan: branch exists but no live worktree is registered.
            # Determine whether the branch tip is an ancestor of (or equal to) base_commit.
            # If yes: safe to reattach — the branch carries no extra commits.
            # If no: the branch has WIP commits; quarantine the ref before creating fresh.
            branch_tip = self._run(["rev-parse", f"refs/heads/{branch}"]).stdout.strip()
            ancestry = subprocess.run(
                ["git", "merge-base", "--is-ancestor", branch_tip, base_commit],
                cwd=str(self.repo_root),
                capture_output=True,
            )
            if ancestry.returncode == 0:
                if branch_tip == base_commit:
                    # Exact equality: safe reattach — no divergence at all.
                    self._worktree_add(path, branch)
                else:
                    # Branch is BEHIND base (ancestor but not equal).
                    # CAS fast-forward: update only if ref still points to branch_tip.
                    try:
                        self._run(["update-ref",
                                   f"refs/heads/{branch}", base_commit, branch_tip])
                    except subprocess.CalledProcessError:
                        raise WorktreeError(
                            f"branch {branch!r} changed concurrently during fast-forward "
                            "to base — refusing to overwrite concurrent writer's commit"
                        )
                    self._worktree_add(path, branch)
            else:
                # Branch has commits ahead of base. Preserve them in a quarantine ref
                # so no commit is destroyed, then create a fresh attempt branch.
                import os as _os
                ts = int(datetime.now(timezone.utc).timestamp())
                guard = _os.urandom(4).hex()  # D1-R4: collision-safe within the same second
                quarantine_ref = f"refs/quarantine/candidate/{mission_id}/{ts}_{guard}"
                # Atomically create the quarantine ref pointing to the current branch tip.
                self._run(["update-ref", quarantine_ref, f"refs/heads/{branch}"])
                # CAS delete: only delete the branch ref if it STILL points to branch_tip.
                # If a concurrent writer advanced the branch between our read and this delete,
                # the delete fails (CalledProcessError) — we raise WorktreeError rather than
                # silently destroying the concurrent writer's commit.
                try:
                    self._run(["update-ref", "-d", f"refs/heads/{branch}", branch_tip])
                except subprocess.CalledProcessError:
                    # Clean up the quarantine ref we just created and refuse to proceed.
                    try:
                        self._run(["update-ref", "-d", quarantine_ref])
                    except subprocess.CalledProcessError:
                        pass
                    raise WorktreeError(
                        f"branch {branch!r} changed concurrently during quarantine — "
                        "refusing to destroy concurrent writer's commit"
                    )
                # L6_bin_lib_products_2-007: pin the ALREADY-RESOLVED commit,
                # never the moving branch name — HEAD advancing between the
                # rev-parse above and this call gave the worktree a NEWER
                # commit than the base_commit metadata records.
                self._worktree_add(path, "-b", branch, base_commit)
        else:
            # Normal path: candidate branch does not exist yet.
            # L6_bin_lib_products_2-007: same pinning of the resolved commit.
            self._worktree_add(path, "-b", branch, base_commit)

        info = WorktreeInfo(
            mission_id=mission_id,
            path=path,
            branch=branch,
            created_at=datetime.now(timezone.utc).isoformat(),
            base_commit=base_commit,
        )
        self._write_meta(info)
        return info

    def _worktree_add(self, path: Path, *extra_args: str) -> None:
        """Run `git worktree add <path> [extra_args]`, translating git errors to WorktreeError.

        Converts subprocess.CalledProcessError (e.g. branch-already-exists exit 255
        from a concurrent create_worktree racing on the same mission) into a
        WorktreeError so callers never see a raw subprocess exception.
        """
        try:
            # FACTORY_REPO_HYGIENE_V1: disk guard BEFORE git touches anything — a typed DISK_SPACE_LOW refusal,
            # never a checkout half-written on a full volume.
            ensure_free_gb(self.worktrees_base if self.worktrees_base.exists() else self.repo_root)
        except DiskSpaceLow as exc:
            raise WorktreeError(f"{exc.event}: git worktree add refused — {exc}") from exc
        try:
            self._run(["worktree", "add", str(path), *extra_args])
        except subprocess.CalledProcessError as exc:
            raise WorktreeError(
                f"git worktree add failed (rc={exc.returncode}): "
                f"{exc.stderr.strip()[:300]}"
            ) from exc

    def _find_worktree_for_branch(self, branch: str) -> "Path | None":
        """Return the path of the registered worktree using `branch`, or None.

        Only returns a path when the worktree directory actually exists on disk —
        stale registrations (directory deleted without `git worktree remove`) are
        ignored.  Call `git worktree prune` before this to clear most stale entries.
        """
        result = self._run(["worktree", "list", "--porcelain"])
        for block in self._porcelain_blocks(result.stdout):
            block_path: "Path | None" = None
            block_branch: "str | None" = None
            for line in block.splitlines():
                if line.startswith("worktree "):
                    block_path = Path(line[len("worktree "):])
                elif line.startswith("branch refs/heads/"):
                    block_branch = line[len("branch refs/heads/"):]
            if block_branch == branch and block_path is not None and block_path.exists():
                return block_path
        return None

    def _write_meta(self, info: WorktreeInfo) -> None:
        meta = dict(asdict(info))
        meta["path"] = str(info.path)
        (info.path / META_FILENAME).write_text(json.dumps(meta))

    def _read_meta(self, path: Path):
        meta_file = path / META_FILENAME
        if not meta_file.exists():
            return None
        try:
            return json.loads(meta_file.read_text())
        except (OSError, json.JSONDecodeError):
            return None

    def remove_worktree(self, mission_id: str, force: bool = False) -> None:
        path = self.worktree_path(mission_id)
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(path))
        self._run(args)

    def _porcelain_blocks(self, output: str):
        return [b for b in output.strip("\n").split("\n\n") if b.strip()]

    def list_worktrees(self) -> list:
        result = self._run(["worktree", "list", "--porcelain"])
        infos = []
        for block in self._porcelain_blocks(result.stdout):
            path = None
            branch = None
            detached = False
            for line in block.splitlines():
                if line.startswith("worktree "):
                    path = Path(line[len("worktree "):])
                elif line.startswith("branch refs/heads/"):
                    branch = line[len("branch refs/heads/"):]
                elif line == "detached":
                    detached = True
            if path is None or not path.is_relative_to(self.worktrees_base):
                continue  # skip the main repo's own worktree entry / anything unmanaged
            mission_id = path.name
            meta = self._read_meta(path) or {}
            infos.append(WorktreeInfo(
                mission_id=mission_id,
                path=path,
                branch=branch or meta.get("branch") or ("detached" if detached else ""),
                created_at=meta.get("created_at", ""),
                base_commit=meta.get("base_commit", ""),
            ))
        return infos

    def recover_worktree(self, mission_id: str):
        path = self.worktree_path(mission_id)
        if not path.exists():
            return None
        result = self._run(["worktree", "list", "--porcelain"])
        known_paths = {
            line[len("worktree "):]
            for block in self._porcelain_blocks(result.stdout)
            for line in block.splitlines()
            if line.startswith("worktree ")
        }
        if str(path) not in known_paths:
            return None
        meta = self._read_meta(path) or {}
        # 20/09 00:20 Bangkok: `.factory_worktree_meta.json` is committed by the
        # capture step and merged into HEAD, so a NEW worktree checks out the
        # meta of ANOTHER mission (repair worktree carried ORPHAN's meta → the
        # dispatcher tried to archive ORPHAN's branch → 8 fast failures). A
        # meta whose mission/path is not ours is inherited noise: ignore it and
        # fall back to the branch actually checked out.
        if str(meta.get("mission_id") or mission_id) != mission_id or \
                str(meta.get("path") or str(path)) != str(path):
            meta = {}
        _branch = meta.get("branch") or self._branch_name(mission_id)
        try:
            _cur = subprocess.run(["git", "branch", "--show-current"], cwd=path,
                                  capture_output=True, text=True, timeout=10).stdout.strip()
            if _cur:
                _branch = _cur
        except Exception:
            pass
        return WorktreeInfo(
            mission_id=mission_id,
            path=path,
            branch=_branch,
            created_at=meta.get("created_at", ""),
            base_commit=meta.get("base_commit", ""),
        )

    # --- LOT C40 item (a): stale-worktree refresh ------------------------------
    # 2026-09-12 TR_UNIVERSE_EXPANSION_V1#M1 incident: a mission worktree created
    # at 18:46Z (base 08e3cb7) was blindly reused by recover_worktree() at 21:4xZ
    # after the product repo had advanced to 16f91e8 (a verifier fix) — the
    # builder worked against the STALE verifier and the receipt's base_sha
    # reflected the worktree's old HEAD, not the product repo's real current
    # HEAD. The methods below let the caller (dispatcher_v3._run_in_worktree)
    # detect and correct that drift before ever running dispatch_v3 in the
    # worktree.

    def is_worktree_dirty(self, path: Path) -> bool:
        """True if `path` has any uncommitted change (ignoring the bookkeeping
        meta file this manager itself writes)."""
        result = self._run(["status", "--porcelain"], cwd=path)
        for line in result.stdout.splitlines():
            if len(line) >= 4 and line[3:].strip() != META_FILENAME:
                return True
        return False

    def worktree_head(self, path: Path) -> str:
        return self._run(["rev-parse", "HEAD"], cwd=path).stdout.strip()

    def refresh_worktree(self, info: "WorktreeInfo", base_sha: str) -> "WorktreeInfo":
        """Reset a CLEAN, stale worktree's branch to `base_sha` in place.

        Only safe when the worktree carries no uncommitted changes and no
        commits ahead of its own recorded base — see archive_worktree() for the
        dirty/unmerged case, which must never be silently overwritten this way.
        """
        self._run(["reset", "--hard", base_sha], cwd=info.path)
        refreshed = WorktreeInfo(
            mission_id=info.mission_id,
            path=info.path,
            branch=info.branch,
            created_at=info.created_at,
            base_commit=base_sha,
        )
        self._write_meta(refreshed)
        return refreshed

    def rebase_worktree(self, info: "WorktreeInfo", base_sha: str) -> "WorktreeInfo | None":
        """Move a CLEAN worktree's own commits (ahead of its recorded base) onto
        `base_sha` — the previous attempt's work survives a HEAD move.

        20/09 18:40 Bangkok: every control-repo commit made a stale base, and a
        stale base with commits meant ARCHIVE + fresh worktree: a 37-finding
        repair lot restarted from zero three times in an afternoon (quota burnt,
        nothing settled). Returns the refreshed info on a clean rebase; None (and
        the worktree exactly as it was) when the rebase conflicts — the caller
        then archives as before.
        """
        r = subprocess.run(["git", "rebase", "--quiet", base_sha], cwd=str(info.path),
                           capture_output=True, text=True)
        if r.returncode != 0:
            subprocess.run(["git", "rebase", "--abort"], cwd=str(info.path), capture_output=True)
            return None
        rebased = WorktreeInfo(
            mission_id=info.mission_id,
            path=info.path,
            branch=info.branch,
            created_at=info.created_at,
            base_commit=base_sha,
        )
        self._write_meta(rebased)
        return rebased

    def archive_worktree(self, info: "WorktreeInfo") -> "WorktreeInfo":
        """Move a dirty/unmerged worktree aside under a timestamped name instead
        of ever silently overwriting it — this is exactly the checkout C28
        preserves after a non-ACCEPTED verdict specifically because it can carry
        WIP a reset/recreate would destroy.

        Renames the candidate branch first (the mission_id's branch name must be
        free again immediately afterwards for the fresh worktree) then relocates
        the directory via `git worktree move` (registration-safe, unlike a bare
        filesystem rename). Returns a WorktreeInfo describing the archived
        location for logging; the caller is expected to create a fresh worktree
        for the original mission_id right after this.
        """
        import os as _os
        ts = int(datetime.now(timezone.utc).timestamp())
        guard = _os.urandom(4).hex()  # collision-safe within the same second
        archived_id = f"{info.mission_id}.archived-{ts}_{guard}"
        archived_branch = f"{info.branch}.archived-{ts}_{guard}"
        archived_path = self.worktrees_base / archived_id
        # Rename from INSIDE the worktree that has the branch checked out: from the
        # main checkout some git versions refuse to rename a branch checked out
        # elsewhere, the rename silently failed and the next create_worktree hit
        # "branch attached to a live worktree at …archived…" (2026-09-17).
        self._run(["branch", "-m", info.branch, archived_branch], cwd=info.path)
        self._run(["worktree", "move", str(info.path), str(archived_path)])
        archived = WorktreeInfo(
            mission_id=archived_id,
            path=archived_path,
            branch=archived_branch,
            created_at=info.created_at,
            base_commit=info.base_commit,
        )
        self._write_meta(archived)
        return archived
