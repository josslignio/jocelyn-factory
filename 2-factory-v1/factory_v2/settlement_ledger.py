"""Tracked settlement-evidence ledger (Sol findings R2/R3/R11 @d0bff136).

Controller-settled milestones previously left their evidence in untracked
runtime state (.context reviews, live PROJECT_STATE notes, per-machine job
receipts).  A detached exact-SHA checkout could not recompute WHAT settled, on
WHICH contract, with WHICH observed verifications — the exact gap the Sol
review rejected.  This module gives settlements a tracked, append-only,
reviewable substrate:

  factory_v2/receipts/settlements/<MILESTONE>.json     one entry per settlement
  factory_v2/receipts/settlements/LEDGER_INDEX.json    sha index of all entries

An entry records the candidate/base SHAs, frozen acceptance contract, the
authority hashes at settle time, every active-verify command WITH its observed
rc, the full-suite receipt, the consolidated Claude gate verdict and the honest
residual.  Every command is re-runnable by a reviewer at the recorded SHA
(git worktree add --detach <sha>) — the ledger documents what was observed and
how to falsify it; it never substitutes for re-execution.

Immutability: entries are never rewritten (record_settlement refuses an
overwrite unless the new entry explicitly supersedes the recorded candidate);
the LEDGER_INDEX pins each file's sha256 so in-place tampering is detectable;
git history anchors the rest.

Live runtime state (PROJECT_STATE.json, factory/run/*, the append-only live
runrecords ledger) stays untracked exactly as before — this ledger is derived
evidence, not a second source of runtime truth.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from factory_v2._io import _now, _atomic_write_bytes, _file_lock

SCHEMA = "settlements/v1"
_INDEX_NAME = "LEDGER_INDEX.json"
#: FACTORY_DEAD_WEIGHT_PURGE_V1 (22/09) : liste explicite des reçus de règlement retenus dans l'arbre
#: (config vivante) — une purge qui en ferait disparaître un est signalée, jamais silencieuse.
_RETENTION_MANIFEST = "factory/config/retained_settlement_receipts.json"
_LOCK_NAME = ".ledger.lock"

_REQUIRED_TOP = ("schema", "milestone", "project", "settled_at",
                 "candidate_sha", "base_sha", "authority_hashes", "contract",
                 "evidence", "honest_residual")
_REQUIRED_EVIDENCE = ("active_verify", "full_suite", "gate")

#: Timestamps are WHEN a settlement was written, never WHAT it proves. They are
#: excluded from settlement identity so that an exact replay after a crash is
#: recognised as the same settlement. The policy is deliberately narrow: only
#: these three keys, and nothing that carries evidence (command, rc, output,
#: verdict, SHAs, contract, authority) is ever discarded.
_VOLATILE_TOP = ("recorded_at", "settled_at")
_VOLATILE_ACTIVE_VERIFY = ("observed_at",)




def _dir(repo: Path) -> Path:
    return Path(repo) / "factory_v2" / "receipts" / "settlements"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(entry: dict) -> bytes:
    return json.dumps(entry, indent=2, sort_keys=True,
                      ensure_ascii=False).encode("utf-8") + b"\n"


def settlement_identity(entry: dict) -> str:
    """Digest of WHAT a settlement proves, independent of when it was written.

    Two entries share an identity only when every evidence-bearing field agrees:
    project, milestone, contract and authority hashes, candidate repo + SHA,
    base SHA, the active-verify commands and their observed rc/output, the gate
    verdict, the full-suite record, honest residual and any supersedes link.
    Only `_VOLATILE_TOP` / `_VOLATILE_ACTIVE_VERIFY` timestamps are ignored.

    This is what makes a crash recoverable WITHOUT weakening anything: an exact
    replay is recognised as the same settlement, while the same candidate SHA
    carrying a different proof, contract or verdict is a DIFFERENT settlement and
    still needs an authorized supersedes.
    """
    core = {k: v for k, v in entry.items() if k not in _VOLATILE_TOP}
    ev = dict(core.get("evidence") or {})
    ev["active_verify"] = [
        {k: v for k, v in (a or {}).items() if k not in _VOLATILE_ACTIVE_VERIFY}
        for a in (ev.get("active_verify") or [])
    ]
    core["evidence"] = ev
    return hashlib.sha256(
        json.dumps(core, sort_keys=True, default=str).encode("utf-8")).hexdigest()


@contextlib.contextmanager
def _ledger_lock(repo: Path):
    """Serialise writers of this SHARED ledger.

    One ledger under the control repo holds every project's settlements, so two
    projects settling at once would otherwise interleave read-modify-write on
    LEDGER_INDEX.json and on rollback. The lock file lives beside the entries and
    is never part of the evidence.
    """
    with _file_lock(_dir(repo) / _LOCK_NAME):
        yield


def _index_entry_for(entry: dict, payload: bytes) -> dict:
    return {"candidate_sha": entry["candidate_sha"],
            "file_sha256": _sha256_bytes(payload),
            "recorded_at": _now(),
            "supersedes": entry.get("supersedes", "")}


def _set_index_entry(repo: Path, milestone: str, value) -> None:
    """Touch ONLY this milestone's row — never rewrite another project's."""
    index = load_index(repo)
    entries = index.setdefault("entries", {})
    if value is None:
        entries.pop(milestone, None)
    else:
        entries[milestone] = value
    _write_index(repo, index)


def _git_ok(repo: Path, *args: str) -> bool:
    try:
        r = subprocess.run(["git", *args], cwd=str(repo),
                           capture_output=True, timeout=30)
        return r.returncode == 0
    except Exception:
        return False


def load_index(repo: Path) -> dict:
    p = _dir(repo) / _INDEX_NAME
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        d = {"schema": "settlements-index/v1", "entries": {}}
    d.setdefault("entries", {})
    # introduced_at: settlements dated >= this moment MUST carry a ledger entry
    # (enforced by settlement_blockers).  Pre-ledger history is exempt.
    d.setdefault("introduced_at", _now())
    return d


def _write_index(repo: Path, index: dict) -> None:
    p = _dir(repo) / _INDEX_NAME
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n")
    tmp.replace(p)


def _settlement_stem(repo: Path, entry: dict) -> str:
    """Index key / file stem for one settlement entry.

    Default: the bare milestone (the historical, backward-compatible layout —
    existing readers and the on-disk ledger use `{milestone}.json`).
    L5_factory_v2_rest_2-011: the ledger is SHARED between projects, so a
    second project settling the same milestone id used to hit the overwrite
    guard on the first project's file. When `{milestone}.json` already exists
    and belongs to a DIFFERENT project, this entry lives under a
    project-qualified stem `{slug(project)}__{milestone}` instead; the index
    key is always the stem actually used (verify_ledger matches by stem).
    """
    milestone = str(entry.get("milestone", ""))
    default_out = _dir(repo) / f"{milestone}.json"
    if default_out.exists():
        try:
            existing = json.loads(default_out.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = {}
        if (isinstance(existing, dict)
                and existing.get("project") not in (None, entry.get("project"))):
            slug = re.sub(r"[^A-Za-z0-9._-]+", "_", str(entry.get("project") or "unknown"))
            return f"{slug}__{milestone}"
    return milestone


def record_settlement(repo: Path, entry: dict) -> Path:
    """Append one settlement entry.  Refuses schema violations and silent
    overwrites; a replacement must name the previous candidate in
    entry['supersedes'] (its own evidence chain is preserved in git history)."""
    repo = Path(repo)
    for field in _REQUIRED_TOP:
        if field not in entry:
            raise ValueError(f"settlement entry missing required field: {field}")
    if entry.get("schema") != SCHEMA:
        raise ValueError(f"schema must be {SCHEMA}")
    for field in _REQUIRED_EVIDENCE:
        if field not in (entry.get("evidence") or {}):
            raise ValueError(f"evidence missing required part: {field}")
    sha = entry.get("candidate_sha", "")
    if not (isinstance(sha, str) and len(sha) == 40
            and all(c in "0123456789abcdef" for c in sha)):
        raise ValueError("candidate_sha must be a full 40-hex git sha")
    for v in entry["evidence"].get("active_verify", []):
        if not isinstance(v, dict) or "command" not in v or "rc" not in v:
            raise ValueError("each active_verify entry needs command + observed rc")
    gate = entry["evidence"].get("gate") or {}
    if gate.get("verdict") not in ("ACCEPT", {"ACCEPT"}):
        # dict-shaped consolidated verdicts must carry at least one ACCEPT and
        # no REJECT; keep the check structural and cheap
        if not (isinstance(gate.get("verdict"), dict)
                and gate["verdict"].values()
                and all(v == "ACCEPT" for v in gate["verdict"].values())):
            raise ValueError("settlement requires an ACCEPT gate verdict")

    stem = _settlement_stem(repo, entry)
    out = _dir(repo) / f"{stem}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        existing = json.loads(out.read_text(encoding="utf-8"))
        if settlement_identity(existing) == settlement_identity(entry):
            # EXACT REPLAY. The daemon recorded this settlement, then died before
            # it could finish (inside verification, or before PROJECT_STATE was
            # marked complete). The stored entry is the same proof, so it stands
            # as the immutable record and is NOT rewritten — replaying must not
            # restamp evidence. Repair only the index, which may be missing or
            # stale if the crash landed between the two writes.
            stored = out.read_bytes()
            idx = (load_index(repo).get("entries") or {}).get(stem)
            if not idx or idx.get("file_sha256") != _sha256_bytes(stored):
                _set_index_entry(repo, stem,
                                 _index_entry_for(existing, stored))
            return out
        if entry.get("supersedes") != existing.get("candidate_sha"):
            # Same milestone, DIFFERENT proof — including the same candidate SHA
            # carrying a different contract, verdict or verification output.
            raise ValueError(
                f"refusing silent overwrite of {out.name}: record supersedes="
                f"{existing.get('candidate_sha', '?')[:12]} to replace it "
                f"(git history preserves the superseded evidence)")
    payload = _canonical(dict(entry, recorded_at=_now()))
    _atomic_write_bytes(out, payload)
    _set_index_entry(repo, stem, _index_entry_for(entry, payload))
    return out


def record_settlement_verified(repo: Path, entry: dict) -> tuple:
    """Record a settlement, and leave NO entry behind if it does not verify.

    `record_settlement` writes before `verify_ledger` can see the result — which
    is deliberate, since a fresh control repo has no ledger to verify until the
    first entry exists. The consequence, observed live on 2026-09-11: a
    settlement whose verification failed still left its entry on disk, and every
    later cycle then hit "refusing silent overwrite" and could never retry. The
    milestone was stranded by the failed attempt's own leftovers.

    The overwrite guard is right and stays exactly as strict. What changes is
    that a settlement which did not verify leaves nothing to collide with: the
    entry file and the index are restored to their prior state, so a retry starts
    from the state that preceded the failure.

    Returns ``(path_or_None, problems)``. A non-empty ``problems`` means nothing
    was durably recorded.
    """
    repo = Path(repo)
    milestone = _settlement_stem(repo, entry)
    out = _dir(repo) / f"{milestone}.json"

    with _ledger_lock(repo):
        prior_entry = out.read_bytes() if out.exists() else None
        prior_index = (load_index(repo).get("entries") or {}).get(milestone)

        def _rollback() -> None:
            # Restore ONLY this milestone. The ledger is shared, so rewriting the
            # whole index file would clobber a settlement another project may have
            # recorded meanwhile.
            if prior_entry is None:
                out.unlink(missing_ok=True)
            else:
                _atomic_write_bytes(out, prior_entry)
            _set_index_entry(repo, milestone, prior_index)

        try:
            path = record_settlement(repo, entry)
            problems = verify_ledger(repo)
        except Exception:
            # A verifier that RAISES must not leave a recorded entry behind
            # either — the previous version only rolled back on returned
            # problems, so an exception stranded the milestone exactly like a
            # failed verification did.
            _rollback()
            raise
        if problems:
            _rollback()
            return None, problems
        return path, []


def retained_receipts(repo: Path) -> list[str]:
    """Basenames named by the retention manifest (empty when the manifest is absent/unreadable)."""
    try:
        doc = json.loads((Path(repo) / _RETENTION_MANIFEST).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    names = doc.get("receipts") if isinstance(doc, dict) else None
    return sorted(str(n) for n in names if isinstance(n, str)) if isinstance(names, list) else []


def retained_receipts_missing(repo: Path) -> list[str]:
    """Retained settlement receipts absent from factory_v2/receipts/settlements/ (sorted basenames)."""
    ledger_dir = _dir(Path(repo))
    return [name for name in retained_receipts(repo) if not (ledger_dir / name).is_file()]


def verify_ledger(repo: Path, *, check_git: bool = True) -> list[str]:
    """Structural verification of every tracked settlement entry.  Returns a
    list of problems (empty == valid).  Never executes the recorded commands —
    re-execution is the reviewer's counter-probe, not a structural check."""
    repo = Path(repo)
    problems: list[str] = []
    index = load_index(repo)
    ledger_dir = _dir(repo)
    if not ledger_dir.is_dir():
        return ["NO_SETTLEMENTS_DIR"]
    files = sorted(p for p in ledger_dir.glob("*.json") if p.name != _INDEX_NAME)
    if not files:
        return ["NO_SETTLEMENT_ENTRIES"]
    problems.extend(f"RETAINED_SETTLEMENT_MISSING:{name}"
                    for name in retained_receipts_missing(repo))
    for p in files:
        try:
            entry = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            problems.append(f"{p.name}:UNREADABLE:{exc}")
            continue
        for field in _REQUIRED_TOP:
            if field not in entry:
                problems.append(f"{p.name}:MISSING:{field}")
        sha = entry.get("candidate_sha", "")
        if not (isinstance(sha, str) and len(sha) == 40):
            problems.append(f"{p.name}:CANDIDATE_SHA_NOT_40HEX")
        elif check_git:
            # A milestone that builds an EXTERNAL product settles on a commit in
            # THAT product's repo, so looking it up here would report a perfectly
            # good settlement as CANDIDATE_SHA_NOT_IN_GIT. Entries record the repo
            # their candidate belongs to; older entries have no such field and are
            # control-repo settlements, which is exactly `repo`.
            cand_repo = Path(entry.get("candidate_repo") or repo)
            if not cand_repo.is_dir():
                problems.append(f"{p.name}:CANDIDATE_REPO_ABSENT:{cand_repo}")
            elif not _git_ok(cand_repo, "cat-file", "-e", f"{sha}^{{commit}}"):
                problems.append(f"{p.name}:CANDIDATE_SHA_NOT_IN_GIT:{sha[:12]}")
        idx = (index.get("entries") or {}).get(p.stem, {})
        if not idx:
            problems.append(f"{p.name}:NOT_IN_INDEX")
        elif idx.get("file_sha256") != _sha256_bytes(p.read_bytes()):
            problems.append(f"{p.name}:FILE_TAMPERED_VS_INDEX")
        ev = entry.get("evidence") or {}
        for part in _REQUIRED_EVIDENCE:
            if part not in ev:
                problems.append(f"{p.name}:EVIDENCE_MISSING:{part}")
        if not ev.get("active_verify"):
            problems.append(f"{p.name}:NO_ACTIVE_VERIFY_COMMANDS")
    return problems


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] != "verify":
        print("usage: python3 -m factory_v2.settlement_ledger verify",
              file=sys.stderr)
        return 2
    repo = Path(argv[2]).resolve() if len(argv) > 2 else Path.cwd()
    problems = verify_ledger(repo)
    if problems:
        print("SETTLEMENT_LEDGER_INVALID:")
        for p in problems:
            print(f"  - {p}")
        return 1
    n = len(list((_dir(repo)).glob("*.json"))) - 1
    print(f"SETTLEMENT_LEDGER_VALID: {n} entries, all structurally sound, "
          f"candidates in git, commands recorded for reviewer re-execution")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
