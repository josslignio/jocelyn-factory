"""Project authority — charge le trio canonique (STRATEGY / SPEC / ROADMAP.yaml)
et calcule l'identité d'autorité (hashes). Toute divergence de hash invalide
le contexte compilé en aval ( DISPATCH_WITHOUT_FRESH_CANONICAL_PROJECT_CONTEXT = 0 ).

Décisions externes: ADAPT Spec Kit (autorité = fichiers, résolus live, jamais copiés).
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from factory_v2 import authority as _authority
from factory_v2 import event_journal
from factory_v2._io import _atomic_write_text

STRATEGY_FILENAME = "STRATEGY.md"
SPEC_FILENAME = "SPEC.md"
ROADMAP_FILENAME = "ROADMAP.yaml"
STATE_FILENAME = "PROJECT_STATE.json"
LOCK_FILENAME = ".project_transaction.lock"

REQUIRED_FILES = (STRATEGY_FILENAME, SPEC_FILENAME, ROADMAP_FILENAME)


class AuthorityError(Exception):
    """Trio canonique absent, illisible ou structurellement invalide."""


# ─────────────────────────── resolve() — GAP-025 ───────────────────────────
# Relocalisé verbatim depuis factory_v2/roadmap_resolver.py (supprimé par la
# décision DELETE CampaignState_next_mission du FACTORY_CONVERGENCE_LEDGER,
# appliquée par FACTORY_CONVERGENCE_RESHAPE) : le resolver est une dérivation
# PURE de l'autorité projet — son unique dépendance était ce module, le fusionner
# ici supprime un chemin de module sans créer de fichier ni boucle d'import.
#
# L'UNIQUE source de direction produit est le trio STRATEGY/SPEC/ROADMAP.yaml.
# Le resolver DÉRIVE la position courante — le roadmap ne stocke AUCUN statut:
# pas de duplication de vérité milestone (GAP-031: aucun pointer next_mission).
#
# Décisions externes: ADAPT Spec Kit « resolve intent live »; ADAPT Temporal
# (décisions déterministes re-jouables: même entrée -> même sortie après restart).

_REQUIRED_STATE_COMPLETE = "COMPLETE"


@dataclass(frozen=True)
class MilestoneView:
    id: str
    goal: str
    depends_on: dict
    deliverables: list
    acceptance: list
    product_e2e: dict
    # S017-05: set by authority when an owner supersedes this execution path.  The
    # milestone stays in the roadmap for its history; it is simply no longer selectable.
    retired: bool = False


@dataclass(frozen=True)
class Resolution:
    project: str
    current_milestone: str | None
    next_milestone: str | None
    why_next: str
    blocked: list = field(default_factory=list)
    completed_milestones: list = field(default_factory=list)
    all_complete: bool = False
    final_dod_reached: bool = False
    # S-CTO-2026-09-11: work the roadmap carries but final_dod does not require.
    # SCALE_N is the live example: real, selectable, optional. Keeping it
    # visible separates "the owner's requested Definition of Done is met" from
    # "there is nothing left to build", which are different facts.
    optional_remaining: list = field(default_factory=list)
    unreachable_required: list = field(default_factory=list)

    @property
    def has_work(self) -> bool:
        return self.next_milestone is not None or bool(self.blocked)


def _milestone_view(entry: dict) -> MilestoneView:
    return MilestoneView(
        id=str(entry["id"]),
        goal=str(entry.get("goal", "")),
        depends_on=dict(entry.get("depends_on") or {}),
        deliverables=list(entry.get("deliverables") or []),
        acceptance=list(entry.get("acceptance") or []),
        product_e2e=dict(entry.get("product_e2e") or {}),
        retired=bool(entry.get("retired", False)),
    )


def resolve(authority: ProjectAuthority, project_state: dict) -> Resolution:
    milestones = [_milestone_view(m) for m in authority.roadmap["milestones"]
                  if isinstance(m, dict) and m.get("id")]
    completed = {mid for mid, rec in (project_state.get("completed_milestones") or {}).items()
                 if isinstance(rec, dict) and rec.get("state") == _REQUIRED_STATE_COMPLETE}
    required = _required_milestones(authority)
    unreachable = sorted(required - _all_selectable(authority))
    if unreachable:
        return Resolution(
            project=authority.project, current_milestone=None, next_milestone=None,
            why_next="INVALID_FINAL_DOD: required milestones are missing or retired",
            blocked=[{"id": mid, "reason": "REQUIRED_MILESTONE_UNREACHABLE"}
                     for mid in unreachable],
            completed_milestones=sorted(completed), unreachable_required=unreachable)
    if not milestones:
        return Resolution(project=authority.project, current_milestone=None,
                          next_milestone=None, why_next="roadmap empty",
                          all_complete=True, final_dod_reached=True,
                          completed_milestones=sorted(completed))

    ordered, seen = [], set()

    def _visit(m: MilestoneView, stack: tuple) -> None:
        if m.id in seen:
            return
        if m.id in stack:
            raise ValueError(f"roadmap dependency cycle at {m.id}")
        for dep in m.depends_on:
            if any(x.id == dep for x in milestones):
                _visit(next(x for x in milestones if x.id == dep), stack + (m.id,))
        seen.add(m.id)
        ordered.append(m)

    for m in milestones:
        _visit(m, ())

    # S017-05: a milestone explicitly RETIRED in authority is not executable work.
    # When an owner supersedes a product definition, its old execution path must stop
    # being selected — but the milestone itself stays in the roadmap so its historical
    # evidence and dependency references remain readable.  Retiring is therefore a
    # property of authority, never a deletion and never a fake completion.
    retired = {m.id for m in milestones if m.retired}

    blocked, next_ms, why = [], None, ""
    for m in ordered:
        if m.id in completed or m.id in retired:
            continue
        missing = []
        for dep, req in m.depends_on.items():
            required = (req or {}).get("required_state", _REQUIRED_STATE_COMPLETE) \
                if isinstance(req, dict) else str(req or _REQUIRED_STATE_COMPLETE)
            if (required == _REQUIRED_STATE_COMPLETE
                    and dep not in completed and dep not in retired):
                missing.append(dep)
        if missing:
            blocked.append({"id": m.id, "missing_deps": missing})
        elif next_ms is None:
            next_ms = m.id
            why = (f"first incomplete milestone in dependency order; "
                   f"all dependencies COMPLETE; goal: {m.goal[:120]}")

    all_complete = next_ms is None and not blocked
    # S-CTO-2026-09-11 (counter-audit): final_dod was `all_complete AND required ⊆
    # completed`. With every one of the 24 milestones that final_dod actually declares
    # COMPLETE, the resolver still reported final_dod_reached=False and next=SCALE_N –
    # a milestone final_dod does not list. The requested Definition of Done was therefore
    # unreachable by construction: it silently required unlisted optional scaling.
    #
    # The DoD is now exactly what the owner declared it to be. Optional work is NOT
    # discarded — next_milestone still selects it, so SCALE_N remains executable
    # and are reported in optional_remaining. Base readiness and optional scaling are two
    # facts, and the resolver now returns both instead of conflating them.
    required = _required_milestones(authority)
    final_dod = required.issubset(completed)
    optional_remaining = [m.id for m in ordered
                          if m.id not in completed and m.id not in retired
                          and m.id not in required]
    return Resolution(
        project=authority.project,
        current_milestone=_last_completed_in_order(ordered, completed),
        next_milestone=next_ms,
        why_next=why or ("all milestones complete" if all_complete else "blocked"),
        blocked=blocked,
        completed_milestones=sorted(completed),
        all_complete=all_complete,
        final_dod_reached=final_dod,
        optional_remaining=optional_remaining,
    )


def _last_completed_in_order(ordered: list, completed: set) -> str | None:
    for m in reversed(ordered):
        if m.id in completed:
            return m.id
    return None


def _all_selectable(authority: ProjectAuthority) -> set:
    """Every milestone that is still executable work — RETIRED ones excluded.

    A retired milestone is superseded authority: it can never become COMPLETE, so including
    it in a required set makes the DoD unreachable forever.
    """
    return {m.get("id") for m in authority.roadmap["milestones"]
            if isinstance(m, dict) and m.get("id") and not m.get("retired")}


def _required_milestones(authority: ProjectAuthority) -> set:
    """The milestone set final_dod actually declares.

    Falls back to every NON-RETIRED milestone when the authority declares no final_dod:
    without a declaration nothing distinguishes required from optional, so everything still
    executable is required. Retired ids must stay out — a superseded milestone that can never
    settle would otherwise make the DoD permanently unreachable (regression caught by the
    second CTO review: the old no-final_dod behaviour was "done when active work is done").
    """
    final = authority.roadmap.get("final_dod")
    if not isinstance(final, dict):
        return _all_selectable(authority)
    declared = set(final.get("requires_milestones") or [])
    if declared:
        return declared
    return _all_selectable(authority)


def _final_dod_ok(authority: ProjectAuthority, completed: set) -> bool:
    """Kept for existing callers: the declared DoD set is satisfied by `completed`."""
    required = _required_milestones(authority)
    return required.issubset(_all_selectable(authority)) and required.issubset(completed)


# ───────────────────────── fin relocalisation resolve() ─────────────────────────


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AuthorityIdentity:
    project: str
    strategy_sha: str
    spec_sha: str
    roadmap_sha: str

    def as_dict(self) -> dict:
        return {"project": self.project, "strategy_sha": self.strategy_sha,
                "spec_sha": self.spec_sha, "roadmap_sha": self.roadmap_sha}

    def differs_from(self, other: "AuthorityIdentity | None") -> list[str]:
        if other is None:
            return ["no_prior_identity"]
        diffs = []
        for field in ("project", "strategy_sha", "spec_sha", "roadmap_sha"):
            if getattr(self, field) != getattr(other, field):
                diffs.append(field.replace("_sha", ".sha"))
        return diffs


@dataclass(frozen=True)
class ProjectAuthority:
    project_dir: Path
    identity: AuthorityIdentity
    strategy_text: str
    spec_text: str
    roadmap: dict

    @property
    def project(self) -> str:
        return self.identity.project


def load_authority(project_dir: Path) -> ProjectAuthority:
    """Load the canonical trio at HEAD (factory_v2.authority) — never a bare disk
    read: an uncommitted edit to any of the three files must not change what the
    daemon believes authority says. See factory_v2/authority.py for the two
    escape hatches (FACTORY_AUTHORITY_FROM_WORKTREE=1, non-git project_dir)."""
    project_dir = Path(project_dir)
    # L2_runtime_core-007: no physical-presence pre-check. Authority is what is
    # committed at HEAD — an uncommitted DELETION of a canonical file must not
    # block loading the very blobs that define the engaged authority. A file
    # that is genuinely absent (never committed, not on disk) still fails below
    # through the OSError path with an explicit message.
    try:
        strategy_text = _authority.load_authority_text(project_dir, STRATEGY_FILENAME)
        spec_text = _authority.load_authority_text(project_dir, SPEC_FILENAME)
        roadmap_raw = _authority.load_authority_text(project_dir, ROADMAP_FILENAME)
    except FileNotFoundError as exc:
        raise AuthorityError(f"{project_dir}: canonical file missing: {exc}") from exc
    except OSError as exc:
        raise AuthorityError(f"{project_dir}: canonical file unreadable at HEAD: {exc}") from exc
    try:
        roadmap = yaml.safe_load(roadmap_raw)
    except yaml.YAMLError as exc:
        raise AuthorityError(f"{project_dir}: unparseable {ROADMAP_FILENAME}: {exc}") from exc
    if not isinstance(roadmap, dict) or "milestones" not in roadmap:
        raise AuthorityError(f"{project_dir}: {ROADMAP_FILENAME} lacks 'milestones'")
    project = str(roadmap.get("project") or project_dir.name)
    identity = AuthorityIdentity(
        project=project,
        strategy_sha=_sha256_text(strategy_text),
        spec_sha=_sha256_text(spec_text),
        roadmap_sha=_sha256_text(roadmap_raw),
    )
    return ProjectAuthority(project_dir=project_dir, identity=identity,
                            strategy_text=strategy_text, spec_text=spec_text,
                            roadmap=roadmap)


def load_project_state(project_dir: Path) -> dict:
    path = Path(project_dir) / STATE_FILENAME
    if not path.is_file():
        return {"schema": "project-state/v2", "project": Path(project_dir).name,
                "completed_milestones": {}, "validation_contracts": {},
                "missions": {}, "events": []}
    state = json.loads(path.read_text(encoding="utf-8"))
    state.setdefault("completed_milestones", {})
    state.setdefault("validation_contracts", {})
    state.setdefault("missions", {})
    state.setdefault("events", [])
    return state


def atomic_write_json(path: Path, payload, *, indent: int = 2,
                      sort_keys: bool = False) -> None:
    """R1: the one durable-write shape (factory_v2/_io.py) for every project-critical JSON."""
    _atomic_write_text(path, json.dumps(payload, indent=indent, sort_keys=sort_keys, ensure_ascii=False) + "\n")


def save_project_state(project_dir: Path, state: dict) -> None:
    """STA-04: flush any events new since the last save into the append-only
    journal (evidence/events.jsonl) and truncate state["events"] to a bounded
    tail before writing PROJECT_STATE.json. See event_journal.py -- this is a
    no-op unless FACTORY_EVENTS_JOURNAL=1 is set, so default behavior (full
    history inline in state["events"]) is unchanged until readers of that key
    have been migrated to event_journal.iter_events().
    """
    state = event_journal.sync_events_to_journal(project_dir, state)
    atomic_write_json(Path(project_dir) / STATE_FILENAME, state, sort_keys=True)


class ProjectBusy(Exception):
    """Another process owns this project's execution transaction.

    Typed on purpose: a caller must be able to tell "someone else is advancing this
    project" apart from a real failure, and must never respond by running a second
    mutation path on the same state file.
    """

    def __init__(self, project_dir: Path, owner: str = ""):
        self.project_dir = str(project_dir)
        self.owner = owner
        super().__init__(
            f"PROJECT_BUSY: {self.project_dir} is owned by another advance() "
            f"transaction{f' ({owner})' if owner else ''}"
        )


@contextlib.contextmanager
def project_transaction(project_dir: Path, *, timeout_s: float = 0.0,
                        poll_s: float = 0.05):
    """Single-writer lock around a whole read -> decide -> mutate -> save transaction.

    R1: locking only save_project_state() does not help — two processes can both read
    the same state, both decide, and the second save silently discards the first
    decision.  Observed as 4 lost events out of 10 concurrent advance() calls.  The
    lock therefore spans the entire transaction.

    flock is used (not a PID file) because the kernel releases it when the owning
    process dies, so a killed daemon cannot leave the project permanently locked.

    timeout_s=0 fails fast with ProjectBusy; a positive value blocks up to that long.
    """
    project_dir = Path(project_dir)
    project_dir.mkdir(parents=True, exist_ok=True)
    lock_path = project_dir / LOCK_FILENAME
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.monotonic() + max(0.0, timeout_s)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if time.monotonic() >= deadline:
                    try:
                        owner = os.read(fd, 128).decode(errors="replace").strip()
                    except OSError:
                        owner = ""
                    raise ProjectBusy(project_dir, owner) from None
                time.sleep(poll_s)
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, f"pid={os.getpid()} since={time.time():.3f}".encode())
        except OSError:
            pass  # the lock is the contract; the owner note is only for diagnostics
        _held = _HELD_LOCKS.__dict__.setdefault("by_dir", {})
        _held[str(project_dir)] = fd
        try:
            yield
        finally:
            _held.pop(str(project_dir), None)
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# FACTORY_INTRA_PROJECT_PARALLEL_V1 (2026-09-17): the lock held by THIS thread for a
# project, so the dispatch path can release it while an engine runs (minutes to
# hours) and take it back for the bookkeeping. Thread-local: two advance() threads
# on the same project each track their own descriptor.
import threading as _threading
_HELD_LOCKS = _threading.local()


@contextlib.contextmanager
def release_project_lock_for_launch(project_dir: Path, *, reacquire_timeout_s: float = 600.0,
                                    poll_s: float = 0.2):
    """Release the project transaction lock held by this thread for the duration of
    the block (an engine launch), then take it back — blocking up to
    `reacquire_timeout_s`. The mission's lease + dedicated worktree stay the only
    owners of the work; everything that touches PROJECT_STATE happens outside the
    block, under the lock again, after the caller reloads the state from disk.
    A no-op when this thread holds no lock (tests calling stages directly)."""
    fd = _HELD_LOCKS.__dict__.setdefault("by_dir", {}).get(str(Path(project_dir)))
    if fd is None:
        yield False
        return
    fcntl.flock(fd, fcntl.LOCK_UN)
    try:
        yield True
    finally:
        deadline = time.monotonic() + max(0.0, reacquire_timeout_s)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if time.monotonic() >= deadline:
                    raise ProjectBusy(Path(project_dir), "reacquire after launch timed out")
                time.sleep(poll_s)
