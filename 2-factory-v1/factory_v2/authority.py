"""factory_v2.authority — read ROADMAP.yaml, SPEC.md, STRATEGY.md and task files
at the git HEAD blob, never the on-disk copy.

The factory's "done" is defined by what is committed to the control repository,
not by whatever happens to be sitting in a worktree. An uncommitted edit to a
canonical authority file (or to a task file the daemon compiles into a mission)
must be invisible to every runtime reader — otherwise a human or a stray process
editing a file on disk, without committing, could silently redirect what the
daemon builds or how it decides a milestone is complete.

Two escape hatches, both intentional:
  - FACTORY_AUTHORITY_FROM_WORKTREE=1 in the environment reads the on-disk copy
    instead of the HEAD blob (tests / local iteration, where committing on every
    edit is not practical).
  - A project_dir that is not inside any git repository at all (e.g. a bare
    tmp_path fixture with no `.git`) falls back to disk automatically — there is
    no HEAD to read from, so disk is the only source that exists.

Reads are cached by (repo root, HEAD sha, relative path) so a hot daemon loop
does not spawn a `git show` subprocess on every cycle for files that have not
moved.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

try:
    import yaml as _yaml
except ImportError:  # pragma: no cover - PyYAML is a hard runtime dependency elsewhere
    _yaml = None

_ENV_WORKTREE = "FACTORY_AUTHORITY_FROM_WORKTREE"

_AUTHORITY_FILENAMES = ("SPEC.md", "STRATEGY.md", "ROADMAP.yaml")

# (repo_root_str, head_sha, rel_posix_path) -> blob text
_BLOB_CACHE: dict[tuple, str] = {}


def _from_worktree_override() -> bool:
    return os.environ.get(_ENV_WORKTREE) == "1"


def _run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True)


def _repo_root(start: Path) -> "Path | None":
    """Toplevel of the git repository containing `start`, or None if not in one."""
    r = _run_git(start, "rev-parse", "--show-toplevel")
    if r.returncode != 0:
        return None
    return Path(r.stdout.strip())


def _head_sha(repo_root: Path) -> "str | None":
    r = _run_git(repo_root, "rev-parse", "HEAD")
    if r.returncode != 0:
        return None
    return r.stdout.strip()


def _to_repo_relative(abs_path: Path, repo_root: Path) -> str:
    return abs_path.resolve().relative_to(repo_root.resolve()).as_posix()


def _read_head_blob_or_disk(abs_path: Path) -> str:
    """Core primitive shared by every loader below.

    Reads `abs_path` at the git HEAD blob of the repository that contains it.
    Falls back to the on-disk copy when: the override env var is set, the path
    is not inside any git repository, or the path exists on disk but has never
    been committed (nothing to fall back to at HEAD for a brand new file).
    """
    abs_path = Path(abs_path)
    if _from_worktree_override():
        return abs_path.read_text(encoding="utf-8")

    repo_root = _repo_root(abs_path.parent if abs_path.parent.exists() else abs_path)
    if repo_root is None:
        # Not inside a git repository at all (e.g. a bare tmp_path fixture).
        return abs_path.read_text(encoding="utf-8")

    rel = _to_repo_relative(abs_path, repo_root)
    head = _head_sha(repo_root)
    cache_key = (str(repo_root), head, rel)
    if head is not None and cache_key in _BLOB_CACHE:
        return _BLOB_CACHE[cache_key]

    r = _run_git(repo_root, "show", f"{head}:{rel}" if head is not None else f"HEAD:{rel}")
    # L5_rest_1-005 (relecture 2026-09) : `git show HEAD:<rel>` relisait la
    # référence MOBILE après que head eut été capturé pour la clé de cache —
    # un commit entre les deux commandations associait le blob B au SHA A et
    # le cache servait ensuite B pour A à vie. Lire via le SHA déjà capturé
    # rend la clé et le contenu inséparables.
    if r.returncode != 0:
        # Exists on disk but not committed yet — nothing at HEAD to fall back to
        # other than disk itself.
        if abs_path.is_file():
            return abs_path.read_text(encoding="utf-8")
        raise FileNotFoundError(f"{rel!r} not found at HEAD in {repo_root}")

    text = r.stdout
    if head is not None:
        _BLOB_CACHE[cache_key] = text
    return text


def load_authority_text(project_dir, name: str) -> str:
    """SPEC.md / STRATEGY.md / ROADMAP.yaml content, read at HEAD (see module docstring)."""
    if name not in _AUTHORITY_FILENAMES:
        raise ValueError(f"unknown authority file: {name!r}; expected one of {_AUTHORITY_FILENAMES}")
    return _read_head_blob_or_disk(Path(project_dir) / name)


def load_roadmap(project_dir) -> dict:
    """ROADMAP.yaml parsed as a dict, read at HEAD."""
    if _yaml is None:  # pragma: no cover
        raise RuntimeError("PyYAML is not available")
    text = load_authority_text(project_dir, "ROADMAP.yaml")
    parsed = _yaml.safe_load(text)
    return parsed if isinstance(parsed, dict) else {}


def load_task_file(project_dir, task_file_path) -> str:
    """A task file's content, read at HEAD.

    `task_file_path` is normally an absolute path — the daemon always stores
    task files this way (see runtime._set_task_file: "cross-repo missions MUST
    use absolute paths"), because a task file commonly lives under
    factory/campaigns/**, not under `project_dir`. A relative path is resolved
    the ordinary way, against the current working directory — never against
    `project_dir`, which would silently misresolve a path that is already
    relative to the repository root.
    """
    p = Path(task_file_path)
    if not p.is_absolute():
        p = Path.cwd() / p
    return _read_head_blob_or_disk(p)


def task_file_hash(project_dir, task_file_path) -> str:
    """sha256 hex digest of the HEAD blob content of `task_file_path`."""
    text = load_task_file(project_dir, task_file_path)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def authority_hashes(project_dir) -> dict:
    """sha256 hex digests of the three canonical authority files, read at HEAD.

    Used wherever authority identity / contract-freeze / AUTHORITY_DRIFT hashes
    are computed, so that a hash never follows an uncommitted disk edit.
    """
    out = {}
    for name, key in (("STRATEGY.md", "strategy_sha"),
                      ("SPEC.md", "spec_sha"),
                      ("ROADMAP.yaml", "roadmap_sha")):
        try:
            text = load_authority_text(project_dir, name)
        except FileNotFoundError:
            out[key] = "MISSING"
            continue
        out[key] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return out
