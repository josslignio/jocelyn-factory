"""The six canonical authorities are read-only to the Factory. Owner directive.

`.context/OWNER_DIRECTIVE_CANONICAL_AUTHORITY_IMMUTABLE_2026-09-10.md` revokes the
earlier rule that allowed an autonomous REPLAN of the Roadmap.

These files express owner intent. The Factory reads and executes them; it never
adapts them to route around a failure, reset an attempt, make a gate easier, or
choose a new direction. A REPLAN may replan DERIVED artefacts — execution plan,
attempt strategy, engine, task decomposition, sub-action order, bounded budget,
task file, isolated challenger — and nothing else.

The ban is a CAPABILITY, checked before any write, not a convention each call site
remembers. Every route was tried: relative paths, `..` traversal and symlinks all
resolve to the same refusal, because the check compares resolved real paths.

An owner may still change these files. That happens OUTSIDE the autonomous loop,
with a diff presented and an independent counter-audit. No agent message,
diagnosis, REPLAN or absence of progress is authorisation:
OWNER_MESSAGE_COUNTS_AS_AUTHORIZATION=0.
"""
from __future__ import annotations

import os
from pathlib import Path

#: Filenames that carry owner intent, in any project directory under projects/.
CANONICAL_FILENAMES = frozenset({"STRATEGY.md", "SPEC.md", "ROADMAP.yaml"})

#: The exact six, relative to the control repo root.
CANONICAL_RELATIVE_PATHS = (
    "projects/factory/STRATEGY.md",
    "projects/factory/SPEC.md",
    "projects/factory/ROADMAP.yaml",
    "projects/twitter-radar/STRATEGY.md",
    "projects/twitter-radar/SPEC.md",
    "projects/twitter-radar/ROADMAP.yaml",
)

_REPO_ROOT = Path(__file__).resolve().parent.parent

# There is NO runtime bypass. An earlier version accepted an environment variable,
# FACTORY_OWNER_CANONICAL_MIGRATION=1, reasoning that it made an owner migration
# visible in `ps`. That reasoning was wrong: any agent able to open a shell can set
# it, so it was a capability the Factory held, not one the owner held.
#
# An owner change happens OUTSIDE this process — daemon stopped, bounded diff,
# dedicated commit, reviewed. It never travels through a runtime flag.


class CanonicalAuthorityWriteRefused(PermissionError):
    """A Factory role tried to write an owner-intent file. Nothing was written."""

    def __init__(self, path, actor: str = ""):
        self.path = str(path)
        self.actor = actor or "unknown"
        super().__init__(
            f"CANONICAL_AUTHORITY_WRITE_REFUSED: {self.actor} attempted to write "
            f"{self.path}. Strategy, Spec and Roadmap express owner intent; the "
            "Factory executes them and never rewrites them. REPLAN may replan the "
            "derived plan only. If the authority is genuinely contradictory or "
            "unexecutable, raise OWNER_AUTHORITY_CHANGE_REQUIRED, preserve the "
            "evidence and continue any other safe work.")


def canonical_paths() -> list:
    return [(_REPO_ROOT / rel) for rel in CANONICAL_RELATIVE_PATHS]


def _real(p) -> Path:
    """Resolved real path, symlinks followed, whether or not the file exists."""
    q = Path(p)
    if not q.is_absolute():
        q = Path.cwd() / q
    try:
        return q.resolve()
    except OSError:
        return Path(os.path.normpath(str(q)))


def is_canonical_authority(path) -> bool:
    """True when `path` names one of the six, however it is spelled.

    Compares RESOLVED real paths, so `../projects/factory/ROADMAP.yaml`, a symlink
    pointing at it, and the absolute path are all the same file. Also catches any
    STRATEGY.md / SPEC.md / ROADMAP.yaml directly under projects/<name>/, so a new
    project is protected the moment it exists rather than when someone remembers
    to add it here.
    """
    target = _real(path)
    for known in canonical_paths():
        if target == _real(known):
            return True
    parts = target.parts
    if target.name in CANONICAL_FILENAMES and len(parts) >= 3:
        if parts[-3] == "projects":
            return _real(_REPO_ROOT / "projects") in [_real(Path(*parts[:-2]))]
    return False


def owner_migration_authorised() -> bool:
    """Always False. Kept so any caller still asking gets the honest answer.

    Deliberately not readable from the environment, a config file or state: every
    one of those is writable by the same agent the ban exists to constrain.
    """
    return False


def assert_writable(path, actor: str = "") -> None:
    """Refuse before a single byte is written. Call at the write site.

    Raises CanonicalAuthorityWriteRefused for the six. There is no exception and no
    flag: an owner change happens outside this process entirely.
    """
    if not is_canonical_authority(path):
        return
    raise CanonicalAuthorityWriteRefused(path, actor)


def sha256_of_canonicals() -> dict:
    """Current digests, for before/after immutability proofs."""
    import hashlib
    out = {}
    for rel in CANONICAL_RELATIVE_PATHS:
        p = _REPO_ROOT / rel
        out[rel] = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
    return out
