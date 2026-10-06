"""Finite, supervisor-authorized recovery allocations — the COST_NO_PROGRESS release path.

COST_NO_PROGRESS suspends new launches after N consecutive dispatch attempts with
no independently validated product delta. It has to, and it also has to have a way
out, because release needs a delta, a delta needs a launch, and a launch is what
the brake forbids.

The first way out was SHA scoping: an attempt made at an older control SHA stopped
counting. That is not a release path, it is an amnesty anyone can buy. A comment,
a doc edit, a `git commit --allow-empty` — any new commit at all reset the streak
to zero and handed the loop a fresh full budget, forever. Reproduced: three
failures at SHA `aaa…` trip the brake; evaluate the SAME three rows against an
unrelated SHA `bbb…` and the streak is 0. The brake was released by the act of
committing, not by the code being fixed. `cost_no_progress` no longer reads the SHA.

## The read side is the authority, not the write side

r1 of this module put the journal under `<project_dir>/evidence/` and validated the
basis only at ISSUE time. Codex reproduced the obvious consequence: write a row
with `schema: recovery-allocation/v1`, the current project/milestone/control SHA,
`basis_ref: {}` and `max_calls: 999`, and `available()` handed out 999 launches.
Deleting the spend rows from the same worker-owned file refilled it indefinitely.
An issuer that verifies once proves nothing if the reader trusts whatever it finds.

So authority now comes from re-derivation on EVERY read, never from file presence:

  * the journal lives in the supervisor namespace (`factory_v2/receipts/
    recovery_allocations/<digest of the canonical project path>/journal.jsonl`),
    outside every project directory, and each row names the resolved project path
    it was issued for;
  * `allocation_id` IS the digest of the grant's own content, recomputed on read —
    edit any bound field and the row stops being the allocation it claims to be;
  * the BASIS is re-validated on every read. A VERIFIED_FIX grant re-loads its
    receipt through `verified_fix.load_referenced` (supervisor-indexed only) and
    re-checks it is PASS, RED→GREEN, and about the code we would actually launch.
    An OWNER grant re-verifies its HMAC over the exact binding. `basis_ref: {}`
    therefore authorizes nothing, wherever it is written;
  * project, milestone, frozen contract id and incident id are compared against
    what the SUPERVISOR STATE says right now, at both check and OPEN — not just
    stored and admired;
  * spend is counted as the MAX of the supervisor journal and the attempt rows the
    project ledger stamped with the allocation id, so deleting one source cannot
    refill; and an attempt referencing an allocation the journal does not have
    makes the whole journal refuse, rather than reading absence as budget;
  * a malformed line or a wrongly typed field makes the journal CORRUPT and every
    allocation unusable. Absence is zero budget; damage is a typed refusal.

## R2C: the supervisor's own diagnosis

COST_NO_PROGRESS used to deadlock: the brake needs a validated delta to release,
the delta needs a launch, and every launch was braked. The way out is now part
of the transition itself — at the FIRST observation of a tripped brake with no
allocation, the runtime creates exactly ONE deduped DIAGNOSE RecoveryWork
(milestone + frozen contract + failure fingerprint) and grants it at most ONE
outer AI launch on the SUPERVISOR_RECOVERY basis. That basis re-validates on
every read and every spend that the pending work on disk is still exactly the
recovery the grant names — same id, kind, milestone and fingerprint — so a
project-only forged RecoveryWork authorizes nothing, and the single launch can
only be spent by that recovery's own dispatch, never by a product builder. A
local (deterministic) recovery consumes no grant at all. When the credit is
spent without a fix the state is terminal and typed; the only durable release
remains an independent settlement projecting a validated delta.

Production added one clause to that. The "forever" dedup was keyed on the
failure alone, so after a REAL commit moved the control SHA the old grant died
of its control pin (correct) but its corpse still blocked the grant for the new
code: no usable allocation, no issuable one, COST_NO_PROGRESS_TERMINAL forever.
The recovery identity and the grant dedup are therefore keyed on the failure
AND the exact commit: one grant per (milestone, contract, fingerprint, commit),
forever. A new commit supersedes the old recovery and may be diagnosed exactly
once more — the brake itself is not reset, the streak is not shortened, a dirty
tree cannot move the committed HEAD, and the old grant stays dead.

## What a call is, honestly

`max_launches` counts OUTER DISPATCH ATTEMPTS — one engine process launched by the
supervisor, one OPEN row, one charge. It is NOT a count of model API calls: what an
engine does inside one attempt is bounded by that engine's own per-attempt
allowance, which this module does not observe and does not claim to. Two launches
means two dispatches, not two model calls.

An allocation is never evidence of a product delta and never sets
`independently_validated`. It buys a bounded number of chances to produce one. When
the launches are spent, the grant expires, or a pin goes stale, the brake is back
on. The only permanent release is unchanged: an independent settlement projecting a
real validated delta.

## Stated limits

The supervisor namespace is protected by filesystem ownership, not by a sandbox:
anything running as this user can write there. That is the same boundary
`verified_fix`'s receipt store already rests on, and it is the honest limit of this
mechanism. The basis re-validation is what survives a same-user writer who does not
hold the owner key or a supervisor-produced receipt.

The supervisor keeps its owner key; control_repo_env removes both authorization
environment variables before launching workers. This is environment isolation,
not an operating-system sandbox against processes sharing the same user.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import json
import os
import re
import sys
import time
from pathlib import Path
from factory_v2._io import _file_lock

SCHEMA = "recovery-allocation/v2"
SPEND_SCHEMA = "recovery-allocation-spend/v2"
REVOKE_SCHEMA = "recovery-allocation-revoke/v2"
#: A spend credited back because the attempt it paid for never launched a
#: model at all — a preflight refusal (runtime-side or dispatcher-side)
#: discovered only AFTER open_account had already charged the launch (durable
#: before execve, by design, so a SIGKILL cannot un-spend it). See
#: `refund()`. Idempotent per attempt_key: replaying the same TERMINAL write
#: can never credit the same attempt twice.
REFUND_SCHEMA = "recovery-allocation-refund/v2"
STANDING_SCHEMA = "recovery-standing-authorization/v1"

#: The supervisor namespace. Deliberately NOT under any project directory: a
#: project dir is where the workers write.
_REPO_ROOT = Path(__file__).resolve().parent.parent
STORE = _REPO_ROOT / "factory_v2" / "receipts" / "recovery_allocations"
JOURNAL_NAME = "journal.jsonl"

#: A supervisor-observed verified fix for the incident that interrupted the work.
BASIS_VERIFIED_FIX = "VERIFIED_FIX_RECEIPT"
#: A one-time, owner-signed bounded resumption.
BASIS_OWNER = "OWNER_AUTHORIZATION"
#: A project-wide owner decision.  Unlike BASIS_OWNER it deliberately names no
#: milestone, contract or incident: those are observations, not its scope.
BASIS_OWNER_STANDING = "OWNER_STANDING_AUTHORIZATION"
#: R2C: the single AI launch the supervisor grants its OWN diagnosis at the
#: COST_NO_PROGRESS transition. Not a receipt and not a signature: the
#: attestation is the supervisor's act of creating exactly that recovery work,
#: and every read re-derives that act from the pending work on disk.
BASIS_SUPERVISOR_RECOVERY = "SUPERVISOR_RECOVERY"
#: The single bounded launch the supervisor grants a multi-AI review of ONE
#: exact candidate SHA. Same discipline as SUPERVISOR_RECOVERY: one grant per
#: (milestone, frozen contract, reviewed SHA) forever, one launch, and the
#: grant retires itself the moment a terminal verdict for that SHA exists in
#: the review ledger — a settled review can never pay for another launch.
BASIS_MULTI_AI_REVIEW = "MULTI_AI_REVIEW"
BASES = (BASIS_VERIFIED_FIX, BASIS_OWNER, BASIS_SUPERVISOR_RECOVERY,
         BASIS_MULTI_AI_REVIEW)

#: Hard ceiling on the dispatch attempts one allocation may pay for.
MAX_LAUNCHES_CEILING = 3
#: Hard ceiling on how long a grant stays live. A proof ages out with its context.
MAX_TTL_S = 24 * 3600
MAX_STANDING_TTL_S = 14 * 24 * 3600
MAX_DAILY_CAP = 240   # 19/09 15:30 Bangkok : 60 atteint en une matinée (dont ~30 refus Codex avant lancement) → reshape bloqué 30 min ; 3 slots × 24 h
DEFAULT_TTL_S = 6 * 3600

_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")

KEY_ENV = "FACTORY_OWNER_AUTHORIZATION_KEY"
KEYFILE_ENV = "FACTORY_OWNER_AUTHORIZATION_KEYFILE"
DEFAULT_KEYFILE = Path.home() / ".factory" / "owner_authorization.key"

#: Fields hashed into allocation_id. Editing any of them invalidates the row.
_BOUND_FIELDS = ("schema", "project", "project_path", "milestone", "contract_id",
                 "incident_id", "control_code_sha", "product_code_sha",
                 "product_cwd", "basis", "basis_ref", "max_launches",
                 "issued_at", "expires_at")


class AllocationRefused(Exception):
    """A grant was refused at issue time. Carries the machine-readable reason."""

    def __init__(self, reason: str, detail: dict | str = ""):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


class AllocationUnavailable(Exception):
    """No usable allocation. Carries why, per candidate, so a block is legible."""

    def __init__(self, reason: str, detail: dict):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


# ── the supervisor journal ───────────────────────────────────────────────────
def canonical_project_path(project_dir) -> str:
    return str(Path(project_dir).resolve())


def journal_dir(project_dir) -> Path:
    key = hashlib.sha256(canonical_project_path(project_dir).encode()).hexdigest()[:24]
    return STORE / key


def journal_path(project_dir) -> Path:
    return journal_dir(project_dir) / JOURNAL_NAME


class _Journal:
    """Parsed journal, or a typed corruption. Never a silent partial read."""

    def __init__(self, rows: list, corrupt: str = ""):
        self.rows = rows
        self.corrupt = corrupt


def _read_journal(project_dir) -> _Journal:
    p = journal_path(project_dir)
    if not p.is_file():
        return _Journal([])
    rows = []
    for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            # r1 skipped these. A journal we cannot read in full is damaged, and a
            # damaged budget ledger must not be read as "no spend recorded".
            return _Journal([], f"UNPARSEABLE_LINE_{n}")
        if not isinstance(row, dict):
            return _Journal([], f"NON_OBJECT_LINE_{n}")
        rows.append(row)
    return _Journal(rows)


def _append(row: dict, project_dir) -> None:
    p = journal_path(project_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def journal_head(project_dir) -> str:
    """Digest of the journal as it stands, for compare-and-swap on an owner grant.

    "EMPTY" when there is none. An authorization names the state it was written
    against; if the state moved, it is refused rather than applied to a world its
    author never saw.
    """
    p = journal_path(project_dir)
    if not p.is_file():
        return "EMPTY"
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _Lock(project_dir):
    """Exclusive lock over the select-and-spend cycle. Two dispatchers racing for
    the last launch of an allocation must not both get it."""
    return _file_lock(journal_dir(project_dir) / "journal.lock")


# ── what the supervisor state says RIGHT NOW ─────────────────────────────────
class BindingUnobservable(Exception):
    """The current project/contract/incident identity could not be read."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def _load_state(project_dir) -> dict:
    p = Path(project_dir) / "PROJECT_STATE.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BindingUnobservable("PROJECT_STATE_UNREADABLE", f"{p}: {exc}")
    if not isinstance(data, dict):
        raise BindingUnobservable("PROJECT_STATE_NOT_AN_OBJECT", str(p))
    return data


def observed_incident_id(project_dir) -> str:
    """Identity of the incident that interrupted this project, or "".

    Derived from SAFETY_QUARANTINE.json — the milestone, mission and frozen
    failure fingerprint the supervisor wrote when it stopped. "" means the project
    is not quarantined, and that is itself a binding: a grant issued for an
    incident cannot be spent once that incident is gone, and vice versa.
    """
    p = Path(project_dir) / "SAFETY_QUARANTINE.json"
    if not p.is_file():
        return ""
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BindingUnobservable("QUARANTINE_UNREADABLE", f"{p}: {exc}")
    if not isinstance(rec, dict):
        raise BindingUnobservable("QUARANTINE_NOT_AN_OBJECT", str(p))
    payload = json.dumps({"milestone": rec.get("milestone_id", ""),
                          "mission": rec.get("mission_id", ""),
                          "fingerprint": rec.get("failure_fingerprint", ""),
                          "entered_at": rec.get("entered_at", "")}, sort_keys=True)
    return "quarantine:" + hashlib.sha256(payload.encode()).hexdigest()[:16]


def observed_binding(project_dir, milestone: str, *,
                     control_code_sha: str = "") -> dict:
    """Project, frozen contract and incident as the SUPERVISOR STATE has them now.

    This is the shape production actually writes:
    `PROJECT_STATE.json["validation_contracts"][<milestone>]["contract_id"]`, with
    the project name at `["project"]`. Both the issuer and the reader derive the
    binding here, from the same file, so they agree by construction and neither
    takes it from a caller that could be a worker.
    """
    state = _load_state(project_dir)
    project = str(state.get("project") or "")
    if not project:
        raise BindingUnobservable("PROJECT_NAME_ABSENT", canonical_project_path(project_dir))
    contracts = state.get("validation_contracts")
    if not isinstance(contracts, dict):
        raise BindingUnobservable("VALIDATION_CONTRACTS_ABSENT", project)
    entry = contracts.get(milestone)
    if not isinstance(entry, dict):
        raise BindingUnobservable("MILESTONE_HAS_NO_CONTRACT", milestone)
    contract_id = str(entry.get("contract_id") or "")
    if not contract_id:
        raise BindingUnobservable("CONTRACT_ID_ABSENT", milestone)
    if str(entry.get("status") or "") != "FROZEN":
        # An allocation pays for work under a frozen contract. An unfrozen one is
        # not the thing the proof was about.
        raise BindingUnobservable("CONTRACT_NOT_FROZEN",
                                  f"{milestone}: {entry.get('status')!r}")
    return {"project": project, "project_path": canonical_project_path(project_dir),
            "milestone": milestone, "contract_id": contract_id,
            "incident_id": observed_incident_id(project_dir),
            "control_code_sha": control_code_sha or _observed_control_sha()}


def _observed_control_sha() -> str:
    # Lazy: launch_gate imports this module, so the edge must not run at import.
    from .launch_gate import current_code_sha
    return current_code_sha()


def _observed_product_sha(product_cwd: str) -> str:
    from .verified_fix import product_code_sha
    return product_code_sha(product_cwd)


def _iso(epoch: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


# ── content-bound identity ───────────────────────────────────────────────────
def _content_id(row: dict) -> str:
    payload = {k: row.get(k) for k in _BOUND_FIELDS}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode()).hexdigest()[:24]


def _typed(row: dict) -> str:
    """Empty string when the row has the shape a grant must have; a typed reason otherwise.

    Scalars that should be ints, dicts that arrive as lists: a forged or truncated
    row must produce a refusal, never a TypeError inside the gate.
    """
    for k in ("project", "project_path", "milestone", "contract_id", "basis"):
        if not isinstance(row.get(k), str) or not row.get(k):
            return f"BAD_FIELD_{k}"
    if not isinstance(row.get("incident_id"), str):
        return "BAD_FIELD_incident_id"
    for k in ("product_code_sha", "product_cwd"):
        if not isinstance(row.get(k), str):
            return f"BAD_FIELD_{k}"
    if row.get("basis") not in BASES:
        return "UNKNOWN_BASIS"
    if not isinstance(row.get("basis_ref"), dict):
        return "BAD_FIELD_basis_ref"
    for k in ("max_launches", "issued_at", "expires_at"):
        v = row.get(k)
        if not isinstance(v, int) or isinstance(v, bool):
            return f"BAD_FIELD_{k}"
    if not 1 <= row["max_launches"] <= MAX_LAUNCHES_CEILING:
        return f"MAX_LAUNCHES_OUT_OF_RANGE_{row['max_launches']}"
    if row["expires_at"] - row["issued_at"] > MAX_TTL_S:
        return "TTL_OUT_OF_RANGE"
    if not _HEX40.match(str(row.get("control_code_sha") or "")):
        return "CONTROL_SHA_NOT_EXACT"
    return ""


# ── basis re-validation, on every read ───────────────────────────────────────
def supervisor_recovery_identity(milestone: str, contract_id: str,
                                 failure_fingerprint: str,
                                 control_code_sha: str = "") -> tuple[str, str]:
    """Deterministic identity of the ONE DIAGNOSE a CNP transition may create.

    The runtime peek and the grant both derive (recovery_id, dedup_key) here,
    from supervisor-observed inputs only — milestone, frozen contract id,
    failure fingerprint and the EXACT control commit the condition was observed
    at — so a recovery record whose identity was not derived this way can never
    be granted, and the same blocking condition can never be granted twice
    under two names at one commit.

    The commit is part of the identity because a diagnosis is a question about
    code AT a commit. A real new commit supersedes the old recovery and is a NEW
    question that may be asked exactly once more — see grant_supervisor_recovery.
    A dirty tree cannot ask it: the SHA is the committed HEAD, so uncommitted
    bytes leave the identity, and the budget attached to it, exactly where they
    were.
    """
    payload = json.dumps({"milestone": milestone,
                          "contract_id": str(contract_id or ""),
                          "failure_fingerprint": str(failure_fingerprint)[:200],
                          "control_code_sha": str(control_code_sha or "")},
                         sort_keys=True)
    dedup = hashlib.sha256(payload.encode()).hexdigest()[:16]
    return f"cnp_{dedup}", dedup


# ── the regrant guard: a new commit is not automatically a new question ──────
#: Code paths whose change can plausibly repair a COST_NO_PROGRESS blocking
#: condition. `supervisor_recovery_identity` names the EXACT control commit,
#: which is correct (a diagnosis is a question about code AT a commit) — but
#: production showed the honest cost of that design read alone: cost_no_progress
#: no longer resets on a commit, yet EVERY commit, including a comment or a doc
#: edit, moves the identity to a fresh (never-yet-granted) recovery and hands
#: the loop a brand-new AI launch. Three real recoveries burned three real
#: model launches in one day (2026-09-12), each right after a commit that had
#: nothing to do with the failure. This list is the other half of the fix: the
#: commit must plausibly touch the recovery machinery itself, or the launch is
#: withheld — see `regrant_guard_decision`.
REGRANT_RELEVANT_PATHS = (
    "factory_v2/runtime.py",
    "factory_v2/recovery_allocation.py",
    "factory_v2/launch_gate.py",
    "factory_v2/escalation_ladder.py",
    "factory/bin/dispatcher_v3.py",
    "factory/bin/model_router.py",
    # Prefixes (trailing "/"): an owner authority change (projects/<p>/ROADMAP,
    # SPEC, STRATEGY) or a builder task-file change (factory/campaigns/.../tasks)
    # is a legitimate NEW cause for a CNP-tripped milestone — the contract
    # itself changed — so it may unlock exactly one more grant.
    "projects/",
    "factory/campaigns/",
)


def is_regrant_relevant_path(path: str, extra_paths: tuple = ()) -> bool:
    """Exact match, or prefix match for entries ending with '/'."""
    path = str(path or "").strip()
    for p in tuple(REGRANT_RELEVANT_PATHS) + tuple(extra_paths):
        if path == p or (p.endswith("/") and path.startswith(p)):
            return True
    return False

#: "At most one AI grant per (milestone, 24h) unless the fingerprint changed."
REGRANT_DAILY_CAP_S = 24 * 3600


def _diff_touches_relevant_paths(old_sha: str, new_sha: str, *,
                                 extra_paths: tuple = ()) -> bool | None:
    """True/False when the diff between two REAL commits can be inspected;
    None when it cannot (an unresolvable SHA, or git itself unavailable).

    `regrant_guard_decision` treats None as "cannot prove either way" and
    fails OPEN on it — never manufacturing a refusal from a diff it could not
    compute. That is safe because in production both SHAs are always ones
    `current_code_sha()` itself observed via `git rev-parse HEAD` on this same
    repo, so they are always resolvable there; an unresolvable pair is a
    property of a test double standing in for a commit, never of production.
    """
    import subprocess
    old_sha, new_sha = str(old_sha or ""), str(new_sha or "")
    if not _HEX40.match(old_sha) or not _HEX40.match(new_sha):
        return None
    if old_sha == new_sha:
        return False
    try:
        r = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "diff", "--name-only", old_sha, new_sha],
            capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    changed = {ln.strip() for ln in r.stdout.splitlines() if ln.strip()}
    return any(is_regrant_relevant_path(f, extra_paths) for f in changed)




def regrant_guard_decision(project_dir, *, milestone: str, fingerprint: str,
                           new_control_code_sha: str,
                           extra_relevant_paths: tuple = (),
                           now: int | None = None) -> dict:
    """Should the CNP transition mint a NEW SUPERVISOR_RECOVERY grant right now?

    Called from the runtime's CNP transition immediately before it would
    otherwise create a new DIAGNOSE + grant for a commit whose identity has
    never been granted before (see `_cnp_peek` / `_diagnose_exists`). Looks at
    the most recently issued SUPERVISOR_RECOVERY grant for this (project,
    milestone), across every past commit, and returns
    ``{"allow": True}`` or ``{"allow": False, "reason": <typed>}``:

      * no prior SUPERVISOR_RECOVERY grant for this milestone at all → allow
        (first diagnosis always gets its one launch);
      * the observed failure fingerprint DIFFERS from the last grant's →
        allow, unconditionally — a different failure is a different question
        and the daily cap does not apply to it either;
      * same fingerprint, same commit as the last grant → refused as
        "same_fingerprint" (this is the identical condition the exact-commit
        dedup in `grant_supervisor_recovery` already refuses forever; reached
        here only defensively);
      * same fingerprint, a real new commit, but the diff between the last
        grant's commit and this one touches none of
        `REGRANT_RELEVANT_PATHS`/`extra_relevant_paths` → refused as
        "no_relevant_code_change" — a comment, a doc edit or an unrelated
        commit is not a repair attempt and must not buy a fresh model launch;
      * same fingerprint, a relevant commit, but another grant for the SAME
        fingerprint was already issued within the last `REGRANT_DAILY_CAP_S`
        → the daily cap normally refuses as "daily_cap" — UNLESS an
        OWNER_AUTHORIZATION grant is active for this exact (project,
        milestone) right now, re-derived through `available(basis=OWNER)`
        exactly as every other read of that basis is (milestone, contract,
        incident, TTL, revocation and exhaustion all re-checked — only its
        own exact-commit pin is exempt, see `_unusable_reason`). The daily
        cap exists to bound how often an AUTOMATED regrant can buy itself a
        launch; it was never meant to overrule an explicit, TTL-bounded
        decision the owner made out of band. When that grant is live, this
        returns {"allow": True, "reason": "owner_override"} instead — a
        THIRD outcome the caller must not treat as an ordinary allow: it
        means "resume directly, mint no new DIAGNOSE, spend no new
        SUPERVISOR_RECOVERY grant" (see `_cnp_peek`'s CNP_RESUMED_BY_OWNER
        branch), because the launch this poll needs is already paid for by
        the owner's own grant, not by a fresh diagnosis of this commit;
      * same fingerprint, a relevant commit, outside the cap window → allow.

    A refused decision here withholds the launch WITHOUT resetting the brake,
    shortening the streak, or touching any existing grant — the caller writes
    one typed CNP_REGRANT_WITHHELD state, deduplicated like every other
    blocked condition, and mints neither a RecoveryWork nor a grant.
    """
    now = int(time.time()) if now is None else int(now)
    fingerprint = str(fingerprint or "")[:200]
    new_sha = str(new_control_code_sha or "")
    j = _read_journal(project_dir)
    if j.corrupt:
        # A damaged journal must not silently allow a fresh launch: refuse,
        # exactly as every other allocation read on it does.
        return {"allow": False, "reason": "JOURNAL_CORRUPT"}
    grants = [r for r in j.rows if r.get("schema") == SCHEMA
              and r.get("basis") == BASIS_SUPERVISOR_RECOVERY
              and r.get("milestone") == milestone]
    if not grants:
        return {"allow": True, "reason": ""}
    grants.sort(key=lambda r: int(r.get("issued_at") or 0))
    last = grants[-1]
    last_fp = str((last.get("basis_ref") or {}).get("failure_fingerprint")
                 or "")[:200]
    last_sha = str(last.get("control_code_sha") or "")
    if fingerprint != last_fp:
        return {"allow": True, "reason": ""}
    if new_sha == last_sha:
        return {"allow": False, "reason": "same_fingerprint"}
    relevant = _diff_touches_relevant_paths(last_sha, new_sha,
                                            extra_paths=extra_relevant_paths)
    if relevant is None:
        # Cannot compute the diff (unresolvable SHA) — never manufacture a
        # refusal from a comparison we could not make. See the docstring of
        # `_diff_touches_relevant_paths` for why this is safe.
        return {"allow": True, "reason": ""}
    if not relevant:
        return {"allow": False, "reason": "no_relevant_code_change"}
    window_start = now - REGRANT_DAILY_CAP_S
    recent_same_fp = [r for r in grants
                      if int(r.get("issued_at") or 0) >= window_start
                      and str((r.get("basis_ref") or {}).get("failure_fingerprint")
                              or "")[:200] == fingerprint]
    if recent_same_fp:
        # An active OWNER grant is a standing, explicit authorization — the
        # daily cap protects against automated regrant churn, not against the
        # owner's own decision. `available()` re-derives everything else
        # (milestone, contract, incident, TTL, revocation, exhaustion) for
        # real; only the STALE_CONTROL_PIN check is waived for this basis.
        owner = available(project_dir, milestone=milestone,
                          control_code_sha=new_sha, basis=BASIS_OWNER, now=now)
        if owner.get("allocation_id"):
            return {"allow": True, "reason": "owner_override"}
        return {"allow": False, "reason": "daily_cap"}
    return {"allow": True, "reason": ""}


def _supervisor_recovery_invalid(row: dict, ref: dict) -> str:
    """Empty string while the supervisor's own diagnosis is still THE pending work.

    R2C: this is the clause that makes a project-only forged RecoveryWork
    worthless even when a journal row exists. On EVERY peek and EVERY spend the
    pending work is re-read from the project's evidence directory and must be
    exactly the one this grant names — same recovery_id, DIAGNOSE kind, same
    milestone, same failure fingerprint — and still uncompleted. A completed,
    superseded or replaced recovery retires the grant with it. The grant also
    never pays for more than one launch, by construction and by re-check.
    """
    from .escalation_ladder import read_pending_recovery_work
    if int(row.get("max_launches") or 0) != 1:
        return "SUPERVISOR_RECOVERY_NOT_SINGLE_LAUNCH"
    try:
        pending = read_pending_recovery_work(Path(row["project_path"]))
    except Exception:
        return "RECOVERY_UNREADABLE"
    if pending is None:
        return "RECOVERY_NOT_PENDING"
    if pending.recovery_id != str(ref.get("recovery_id") or ""):
        return "RECOVERY_ID_MISMATCH"
    if pending.kind != "DIAGNOSE":
        return "RECOVERY_KIND_MISMATCH"
    if pending.milestone_id != row["milestone"]:
        return "RECOVERY_MILESTONE_MISMATCH"
    if (str(pending.failure_fingerprint or "")[:200]
            != str(ref.get("failure_fingerprint") or "")[:200]):
        return "RECOVERY_FINGERPRINT_MISMATCH"
    return ""


def multi_ai_review_identity(milestone: str, contract_id: str,
                             reviewed_sha: str) -> tuple:
    """Deterministic identity of the ONE review budget per exact candidate SHA.

    The runtime and the grant both derive (review_id, dedup_key) here from
    supervisor-observed inputs — milestone, frozen contract id, the EXACT SHA
    under review — so the same candidate can never be granted a second review
    launch under a second name, and a repair producing a NEW SHA is a NEW
    condition with its own single launch.
    """
    payload = json.dumps({"milestone": milestone,
                          "contract_id": str(contract_id or ""),
                          "reviewed_sha": str(reviewed_sha or "")},
                         sort_keys=True)
    dedup = hashlib.sha256(payload.encode()).hexdigest()[:16]
    return f"mai_{dedup}", dedup


def _multi_ai_review_invalid(row: dict, ref: dict) -> str:
    """Empty string while THIS exact SHA still needs its one review launch.

    Re-derived on every peek and every spend: single launch by construction and
    by re-check, and the grant RETIRES itself the moment the project's review
    ledger carries a terminal verdict (ACCEPT or BLOCK) for this exact SHA —
    a decided review can never pay for another launch, which is what makes the
    budget idempotent across polls rather than merely small.
    """
    if int(row.get("max_launches") or 0) != 1:
        return "MULTI_AI_REVIEW_NOT_SINGLE_LAUNCH"
    sha = str(ref.get("reviewed_sha") or "")
    if not sha:
        return "MULTI_AI_REVIEW_SHA_MISSING"
    try:
        from .review_dispatch import read_reviews
        decided = any(r.get("milestone") == row["milestone"]
                      and r.get("reviewed_sha") == sha
                      and r.get("verdict") in ("ACCEPT", "BLOCK")
                      for r in read_reviews(Path(row["project_path"])))
    except Exception:
        return "MULTI_AI_REVIEW_LEDGER_UNREADABLE"
    if decided:
        return "MULTI_AI_REVIEW_ALREADY_DECIDED"
    return ""


def _receipt_binding_invalid(row: dict, receipt) -> str:
    """A real proof must close this frozen incident, not merely name this SHA."""
    from .safety_quarantine import get_repair_contract, get_repair_chain
    from .verified_fix import context_signature
    if (row.get("basis_ref") or {}).get("store_project") != row["project"]:
        return "RECEIPT_PROJECT_MISMATCH"
    project_dir = Path(row["project_path"])
    try:
        q = json.loads((project_dir / "SAFETY_QUARANTINE.json").read_text())
        contract = get_repair_contract(project_dir)
        chain = get_repair_chain(project_dir)
    except (OSError, ValueError, TypeError, AttributeError):
        return "RECEIPT_INCIDENT_UNOBSERVABLE"
    if contract is None or not contract.verify_integrity():
        return "RECEIPT_REPAIR_CONTRACT_INVALID"
    if q.get("milestone_id") != row["milestone"] or contract.milestone_id != row["milestone"]:
        return "RECEIPT_MILESTONE_MISMATCH"
    if not q.get("failure_fingerprint"):
        return "RECEIPT_INCIDENT_UNOBSERVABLE"
    why = receipt.closes(
        fingerprint=q["failure_fingerprint"],
        contract_signature=contract.contract_sha,
        context_manifest_sha=context_signature(chain, contract),
        old_code_sha=contract.pre_repair_sha,
        fix_code_sha=row["control_code_sha"])
    if why:
        return "RECEIPT_" + why.split(":")[0]
    if receipt.product_code_sha != row["product_code_sha"]:
        return "RECEIPT_PRODUCT_PIN_MISMATCH"
    return ""


def _owner_row_seal(row: dict, key: bytes) -> str:
    """Authenticate absolute expiry and checkout path as well as the signed request."""
    payload = {k: row.get(k) for k in _BOUND_FIELDS}
    payload["basis_ref"] = {k: v for k, v in row["basis_ref"].items() if k != "row_seal"}
    return hmac.new(key, json.dumps(payload, sort_keys=True).encode(), hashlib.sha256).hexdigest()


def _basis_invalid(row: dict) -> str:
    """Empty string when the row's BASIS still authorizes it; a typed reason otherwise.

    This is the clause that makes a hand-written journal row worthless. It runs on
    every peek and every spend, not once at issue.
    """
    ref = row.get("basis_ref") or {}
    if row["basis"] == BASIS_VERIFIED_FIX:
        from .verified_fix import load_referenced
        store_project = str(ref.get("store_project") or "")
        receipt = load_referenced(store_project, {
            "receipt_id": str(ref.get("receipt_id") or ""),
            "content_hash": str(ref.get("content_hash") or "")})
        if receipt is None:
            return "RECEIPT_NOT_IN_TRUSTED_STORE"
        why = receipt.invalid_reason()
        if why:
            return f"RECEIPT_INVALID_{why.split(':')[0]}"
        if receipt.fix_code_sha != row["control_code_sha"]:
            return "RECEIPT_IS_NOT_ABOUT_THE_RUNNING_CODE"
        return _receipt_binding_invalid(row, receipt)
    if row["basis"] == BASIS_SUPERVISOR_RECOVERY:
        return _supervisor_recovery_invalid(row, ref)
    if row["basis"] == BASIS_MULTI_AI_REVIEW:
        return _multi_ai_review_invalid(row, ref)
    key = owner_key()
    if not key:
        # No key in this process ⇒ no owner-authorized launch. Fail-closed, and
        # the reason a keyfile is preferred over an inherited env var.
        return "OWNER_KEY_UNAVAILABLE"
    if not hmac.compare_digest(_owner_row_seal(row, key), str(ref.get("row_seal") or "")):
        return "OWNER_ROW_SEAL_INVALID"
    expect = _owner_signature(row, key)
    if not hmac.compare_digest(expect, str(ref.get("signature") or "")):
        return "OWNER_SIGNATURE_INVALID"
    return ""


def owner_key() -> bytes | None:
    """The owner authorization key, or None when none is configured.

    None means owner-authorized grants are refused — at issue AND at read. A
    release path whose authority defaults to "present" is not an authorization.
    """
    path = Path(os.environ.get(KEYFILE_ENV, "") or DEFAULT_KEYFILE)
    try:
        data = path.read_text(encoding="utf-8").strip()
        if data:
            return data.encode()
    except OSError:
        pass
    raw = os.environ.get(KEY_ENV, "")
    return raw.strip().encode() if raw.strip() else None


def owner_payload(bind: dict, *, max_launches: int, ttl_s: int, nonce: str,
                  expect_journal_head: str) -> str:
    """Canonical bytes an owner signature covers. Every field that bounds the
    grant is inside it: re-pointing any of them invalidates the signature."""
    return json.dumps({"v": 2, "binding": {k: bind.get(k, "") for k in
                                           ("project", "project_path", "milestone",
                                            "contract_id", "incident_id",
                                            "control_code_sha", "product_code_sha")},
                       "max_launches": max_launches, "ttl_s": ttl_s,
                       "nonce": nonce, "expect_journal_head": expect_journal_head},
                      sort_keys=True)


def sign_owner_authorization(bind: dict, *, max_launches: int, ttl_s: int,
                             nonce: str, expect_journal_head: str,
                             key: bytes | None = None) -> str:
    key = key if key is not None else owner_key()
    if not key:
        raise AllocationRefused("NO_OWNER_KEY_CONFIGURED",
                                f"set {KEYFILE_ENV} or {KEY_ENV}")
    return hmac.new(key, owner_payload(bind, max_launches=max_launches, ttl_s=ttl_s,
                                       nonce=nonce,
                                       expect_journal_head=expect_journal_head)
                    .encode(), hashlib.sha256).hexdigest()


def standing_payload(bind: dict, *, daily_launch_cap: int, ttl_s: int,
                     nonce: str, expect_journal_head: str) -> str:
    """Canonical project-only payload for a standing owner authorization."""
    return json.dumps({"v": 1, "basis": BASIS_OWNER_STANDING,
                       "project": bind.get("project", ""),
                       "project_path": bind.get("project_path", ""),
                       # Informational only: it is signed for audit, never pinned.
                       "control_code_sha": bind.get("control_code_sha", ""),
                       "daily_launch_cap": daily_launch_cap, "ttl_s": ttl_s,
                       "nonce": nonce, "expect_journal_head": expect_journal_head},
                      sort_keys=True)


def sign_standing_authorization(bind: dict, *, daily_launch_cap: int, ttl_s: int,
                                 nonce: str, expect_journal_head: str,
                                 key: bytes | None = None) -> str:
    key = key if key is not None else owner_key()
    if not key:
        raise AllocationRefused("NO_OWNER_KEY_CONFIGURED", f"set {KEYFILE_ENV} or {KEY_ENV}")
    return hmac.new(key, standing_payload(bind, daily_launch_cap=daily_launch_cap,
                                          ttl_s=ttl_s, nonce=nonce,
                                          expect_journal_head=expect_journal_head).encode(),
                    hashlib.sha256).hexdigest()


def _standing_id(row: dict) -> str:
    fields = ("schema", "basis", "project", "project_path", "control_code_sha",
              "daily_launch_cap", "issued_at", "expires_at", "nonce", "signature")
    return hashlib.sha256(json.dumps({k: row.get(k) for k in fields}, sort_keys=True).encode()).hexdigest()[:24]


def _standing_binding(project_dir) -> dict:
    state = _load_state(project_dir)
    project = str(state.get("project") or "")
    if not project:
        raise BindingUnobservable("PROJECT_NAME_ABSENT", canonical_project_path(project_dir))
    return {"project": project, "project_path": canonical_project_path(project_dir),
            "control_code_sha": _observed_control_sha()}


def grant_standing_authorization(project_dir, *, nonce: str, signature: str,
                                 expect_journal_head: str, daily_launch_cap: int,
                                 ttl_s: int, note: str = "", now: int | None = None) -> dict:
    """Persist one signed, project-wide authorization; CAS and nonce are mandatory."""
    now = int(time.time()) if now is None else int(now)
    if not isinstance(daily_launch_cap, int) or isinstance(daily_launch_cap, bool) or not 1 <= daily_launch_cap <= MAX_DAILY_CAP:
        raise AllocationRefused("DAILY_CAP_OUT_OF_RANGE", f"{daily_launch_cap!r} not in 1..{MAX_DAILY_CAP}")
    if not isinstance(ttl_s, int) or not 60 <= ttl_s <= MAX_STANDING_TTL_S:
        raise AllocationRefused("TTL_OUT_OF_RANGE", f"{ttl_s!r} not in 60..{MAX_STANDING_TTL_S}")
    if not str(nonce or "").strip():
        raise AllocationRefused("NONCE_MISSING", "")
    bind = _standing_binding(project_dir)
    key = owner_key()
    if not key:
        raise AllocationRefused("NO_OWNER_KEY_CONFIGURED", f"set {KEYFILE_ENV} or {KEY_ENV}")
    with _Lock(project_dir):
        j = _read_journal(project_dir)
        if j.corrupt:
            raise AllocationRefused("JOURNAL_CORRUPT", j.corrupt)
        if journal_head(project_dir) != expect_journal_head:
            raise AllocationRefused("JOURNAL_CAS_MISMATCH", {"expected": expect_journal_head, "observed": journal_head(project_dir)})
        expected = sign_standing_authorization(bind, daily_launch_cap=daily_launch_cap,
                                               ttl_s=ttl_s, nonce=nonce,
                                               expect_journal_head=expect_journal_head, key=key)
        if not hmac.compare_digest(expected, str(signature or "")):
            raise AllocationRefused("OWNER_SIGNATURE_INVALID", {"nonce": nonce})
        if any(r.get("nonce") == nonce and r.get("schema") == STANDING_SCHEMA for r in j.rows):
            raise AllocationRefused("NONCE_ALREADY_USED", nonce)
        row = {"schema": STANDING_SCHEMA, "basis": BASIS_OWNER_STANDING,
               **bind, "daily_launch_cap": daily_launch_cap, "issued_at": now,
               "expires_at": now + ttl_s, "nonce": nonce, "signature": expected,
               "expect_journal_head": expect_journal_head, "note": note,
               "issued_at_iso": _iso(now), "expires_at_iso": _iso(now + ttl_s)}
        row["allocation_id"] = _standing_id(row)
        _append(row, project_dir)
    return row


def _standing_window_frees_at(project_dir, *, now: int, allocation_id: str = "") -> int:
    """Epoch at which the OLDEST launch still inside the 24 h window ages out —
    the true "resets_at" of a sliding cap (2026-09-14: `now + 24h` told the owner
    the project was frozen for a full day when the next slot freed in minutes)."""
    from datetime import datetime
    cutoff = now - 24 * 3600
    oldest = None
    for row in _attempt_ledger_rows(project_dir):
        if not row.get("attempt_key") or row.get("consumes_model_quota") is False:
            continue
        paid_by = row.get("recovery_allocation_id")
        if paid_by and paid_by != allocation_id:
            continue
        raw = row.get("started_at") or row.get("ended_at") or ""
        try:
            stamp = int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
        except (TypeError, ValueError):
            continue
        if stamp >= cutoff and (oldest is None or stamp < oldest):
            oldest = stamp
    return (oldest + 24 * 3600) if oldest is not None else now


def _standing_used_today(project_dir, *, now: int, allocation_id: str = "") -> int:
    """OPEN/TERMINAL attempts are folded first; explicit refunds never count.

    2026-09-13 (owner console, cap 30/12 the morning after the Ferrari night):
    a launch paid by a per-milestone OWNER grant (`recovery_allocation_id` set
    to some OTHER allocation) was separately and explicitly authorized — it is
    not a standing launch and must not eat the standing cap. Rows attributed
    to this standing authorization, and unattributed quota-consuming rows
    (contract budget, legacy rows), still count: fail-closed on what cannot
    be attributed, exact on what can.
    """
    from datetime import datetime, timezone
    cutoff = now - 24 * 3600
    used = 0
    for row in _attempt_ledger_rows(project_dir):
        if not row.get("attempt_key") or row.get("consumes_model_quota") is False:
            continue
        paid_by = row.get("recovery_allocation_id")
        if paid_by and paid_by != allocation_id:
            continue                         # another allocation paid for this one
        raw = row.get("started_at") or row.get("ended_at") or ""
        try:
            stamp = int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
        except (TypeError, ValueError):
            continue                         # unobservable timestamp cannot buy quota
        if stamp >= cutoff:
            used += 1
    return used


def available_standing(project_dir, *, expect_project: str = "", now: int | None = None) -> dict:
    """Fail-closed project-wide authorization peek, independent of milestones."""
    now = int(time.time()) if now is None else int(now)
    try:
        bind = _standing_binding(project_dir)
    except BindingUnobservable as exc:
        return {"allocation_id": "", "detail": f"binding unobservable: {exc.reason}"}
    if expect_project and bind["project"] != expect_project:
        return {"allocation_id": "", "detail": "project identity disagrees"}
    key, j = owner_key(), _read_journal(project_dir)
    if not key or j.corrupt:
        return {"allocation_id": "", "detail": "owner key absent or journal corrupt"}
    refusals = []
    # L2_runtime_core-010: a persisted REVOKE applies to standing grants exactly
    # as `_unusable_reason` applies it to ordinary allocations.
    revoked = {r.get("allocation_id") for r in j.rows if r.get("schema") == REVOKE_SCHEMA}
    for row in (r for r in reversed(j.rows) if r.get("schema") == STANDING_SCHEMA):
        why = ""
        if row.get("allocation_id") != _standing_id(row): why = "CONTENT_ID_MISMATCH"
        elif row.get("allocation_id") in revoked: why = "REVOKED"
        elif row.get("project") != bind["project"] or row.get("project_path") != bind["project_path"]: why = "WRONG_PROJECT"
        elif not isinstance(row.get("daily_launch_cap"), int) or not 1 <= row["daily_launch_cap"] <= MAX_DAILY_CAP: why = "DAILY_CAP_OUT_OF_RANGE"
        elif not isinstance(row.get("issued_at"), int) or not isinstance(row.get("expires_at"), int) or row["expires_at"] - row["issued_at"] > MAX_STANDING_TTL_S: why = "TTL_OUT_OF_RANGE"
        elif now >= row.get("expires_at", 0): why = "EXPIRED"
        else:
            expected = sign_standing_authorization(row, daily_launch_cap=row["daily_launch_cap"],
                ttl_s=row["expires_at"] - row["issued_at"], nonce=str(row.get("nonce") or ""),
                expect_journal_head=str(row.get("expect_journal_head") or ""), key=key)
            if not hmac.compare_digest(expected, str(row.get("signature") or "")): why = "OWNER_SIGNATURE_INVALID"
        if not why:
            used = _standing_used_today(project_dir, now=now, allocation_id=row["allocation_id"])
            if used < row["daily_launch_cap"]:
                return {"allocation_id": row["allocation_id"], "basis": BASIS_OWNER_STANDING,
                        "used_today": used, "cap": row["daily_launch_cap"], "expires_at": row["expires_at"]}
            return {"allocation_id": "", "standing_cap_reached": True, "used_today": used,
                    "cap": row["daily_launch_cap"],
                    "resets_at": _iso(_standing_window_frees_at(project_dir, now=now, allocation_id=row["allocation_id"]))}
        refusals.append(why)
    return {"allocation_id": "", "refusals": refusals, "detail": "no valid standing authorization"}


def record_standing_event(project_dir, event: str, **fields) -> dict:
    """Durable audit event for standing use/cap; event rows never affect CAS grants."""
    row = {"schema": "recovery-standing-event/v1", "event": event,
           "at": _iso(int(time.time())), **fields}
    with _Lock(project_dir):
        _append(row, project_dir)
    return row


def _owner_signature(row: dict, key: bytes) -> str:
    """Recompute the signature a stored OWNER row must carry, from the row itself."""
    ref = row.get("basis_ref") or {}
    return sign_owner_authorization(
        row, max_launches=row["max_launches"],
        ttl_s=int(ref.get("ttl_s") or (row["expires_at"] - row["issued_at"])),
        nonce=str(ref.get("nonce") or ""),
        expect_journal_head=str(ref.get("expect_journal_head") or ""), key=key)


# ── issue ────────────────────────────────────────────────────────────────────
def _new_row(bind: dict, *, basis: str, basis_ref: dict, max_launches: int,
             ttl_s: int, now: int, note: str, product_cwd: str,
             product_code_sha: str) -> dict:
    row = {"schema": SCHEMA,
           "project": bind["project"], "project_path": bind["project_path"],
           "milestone": bind["milestone"], "contract_id": bind["contract_id"],
           "incident_id": bind["incident_id"],
           "control_code_sha": bind["control_code_sha"],
           "product_code_sha": product_code_sha, "product_cwd": product_cwd,
           "basis": basis, "basis_ref": basis_ref, "max_launches": max_launches,
           "issued_at": now, "expires_at": now + ttl_s}
    row["allocation_id"] = _content_id(row)
    row["issued_at_iso"] = _iso(now)
    row["expires_at_iso"] = _iso(row["expires_at"])
    row["issued_by"] = "supervisor"
    row["note"] = note
    row["unit"] = "outer_dispatch_attempt"
    return row


def _validate_ttl_and_calls(max_launches: int, ttl_s: int) -> None:
    if not isinstance(max_launches, int) or not 1 <= max_launches <= MAX_LAUNCHES_CEILING:
        raise AllocationRefused("MAX_LAUNCHES_OUT_OF_RANGE",
                                f"{max_launches!r} not in 1..{MAX_LAUNCHES_CEILING}")
    if not isinstance(ttl_s, int) or not 60 <= ttl_s <= MAX_TTL_S:
        raise AllocationRefused("TTL_OUT_OF_RANGE", f"{ttl_s!r} not in 60..{MAX_TTL_S}")


def grant_from_verified_fix(project_dir, *, milestone: str, receipt_ref: dict,
                            receipt_project_id: str = "", max_launches: int = 1,
                            ttl_s: int = DEFAULT_TTL_S, product_cwd: str = "",
                            note: str = "", now: int | None = None) -> dict:
    """Issue an allocation backed by a VerifiedFixReceipt from the trusted store.

    The binding is DERIVED from supervisor state, never supplied: project, frozen
    contract id and incident id come from PROJECT_STATE.json and
    SAFETY_QUARANTINE.json, and the control SHA from the repo. The receipt is
    resolved through `verified_fix.load_referenced`, so a claimant that writes its
    own JSON and computes its own digest is not selectable, and its fix SHA must be
    the code we would actually launch.
    """
    now = int(time.time()) if now is None else int(now)
    _validate_ttl_and_calls(max_launches, ttl_s)
    bind = observed_binding(project_dir, milestone)
    store_project = receipt_project_id or bind["project"]

    from .verified_fix import load_referenced
    receipt = load_referenced(store_project, receipt_ref or {})
    if receipt is None:
        raise AllocationRefused("RECEIPT_NOT_IN_TRUSTED_STORE",
                                {"ref": receipt_ref, "store_project": store_project})
    why = receipt.invalid_reason()
    if why:
        raise AllocationRefused("RECEIPT_INVALID", why)
    if not _HEX40.match(bind["control_code_sha"] or ""):
        raise AllocationRefused("CONTROL_SHA_UNOBSERVABLE", bind["control_code_sha"])
    if receipt.fix_code_sha != bind["control_code_sha"]:
        raise AllocationRefused("RECEIPT_FIX_SHA_IS_NOT_RUNNING_CODE",
                                {"fix_code_sha": receipt.fix_code_sha,
                                 "observed": bind["control_code_sha"]})
    product_sha = receipt.product_code_sha or ""
    if product_sha and not product_cwd:
        raise AllocationRefused("PRODUCT_PIN_NEEDS_CWD",
                                "the receipt pins an external product HEAD; the "
                                "grant must name the checkout so the reader can "
                                "re-observe it")
    if product_sha and _observed_product_sha(product_cwd) != product_sha:
        raise AllocationRefused("PRODUCT_SHA_MOVED",
                                {"pinned": product_sha,
                                 "observed": _observed_product_sha(product_cwd)})

    ref = {"kind": BASIS_VERIFIED_FIX, "store_project": store_project,
           "receipt_id": receipt.receipt_id(), "content_hash": receipt.content_hash(),
           "old_fingerprint": receipt.old_fingerprint,
           "old_code_sha": receipt.old_code_sha, "fix_code_sha": receipt.fix_code_sha}
    row = _new_row(bind, basis=BASIS_VERIFIED_FIX, basis_ref=ref,
                   max_launches=max_launches, ttl_s=ttl_s, now=now, note=note,
                   product_cwd=product_cwd if product_sha else "",
                   product_code_sha=product_sha)
    why = _receipt_binding_invalid(row, receipt)
    if why:
        raise AllocationRefused(why)
    with _Lock(project_dir):
        j = _read_journal(project_dir)
        if j.corrupt:
            raise AllocationRefused("JOURNAL_CORRUPT", j.corrupt)
        if any(r.get("schema") == SCHEMA
               and (r.get("basis_ref") or {}).get("receipt_id") == ref["receipt_id"]
               for r in j.rows):
            # One proof, one allocation. Otherwise the same receipt re-grants a
            # fresh budget on every poll and the bound is decorative.
            raise AllocationRefused("RECEIPT_ALREADY_USED", ref["receipt_id"])
        _append(row, project_dir)
    return row


def grant_owner_authorization(project_dir, *, milestone: str, nonce: str,
                              signature: str, expect_journal_head: str,
                              max_launches: int = 1, ttl_s: int = DEFAULT_TTL_S,
                              product_cwd: str = "", note: str = "",
                              now: int | None = None) -> dict:
    """Issue a one-time bounded resumption the OWNER authorized.

    The binding is derived from supervisor state exactly as above; the owner signs
    that binding out of band. Four independent things must hold, each closing a way
    the runtime could otherwise authorize itself: the signature verifies against a
    key this repo does not carry; `expect_journal_head` still matches (CAS); the
    nonce has never been used; and the control SHA signed is the one observed.
    """
    now = int(time.time()) if now is None else int(now)
    _validate_ttl_and_calls(max_launches, ttl_s)
    if not str(nonce or "").strip():
        raise AllocationRefused("NONCE_MISSING", "")
    bind = observed_binding(project_dir, milestone)
    if not _HEX40.match(bind["control_code_sha"] or ""):
        raise AllocationRefused("CONTROL_SHA_UNOBSERVABLE", bind["control_code_sha"])
    key = owner_key()
    if not key:
        raise AllocationRefused("NO_OWNER_KEY_CONFIGURED",
                                f"set {KEYFILE_ENV} or {KEY_ENV}")
    product_sha = _observed_product_sha(product_cwd) if product_cwd else ""
    bind = {**bind, "product_code_sha": product_sha}

    with _Lock(project_dir):
        j = _read_journal(project_dir)
        if j.corrupt:
            raise AllocationRefused("JOURNAL_CORRUPT", j.corrupt)
        head = journal_head(project_dir)
        if head != expect_journal_head:
            raise AllocationRefused("JOURNAL_CAS_MISMATCH",
                                    {"expected": expect_journal_head,
                                     "observed": head})
        expect_sig = sign_owner_authorization(
            bind, max_launches=max_launches, ttl_s=ttl_s, nonce=nonce,
            expect_journal_head=expect_journal_head, key=key)
        if not hmac.compare_digest(expect_sig, str(signature or "")):
            raise AllocationRefused("OWNER_SIGNATURE_INVALID", {"nonce": nonce})
        if any(r.get("schema") == SCHEMA
               and (r.get("basis_ref") or {}).get("nonce") == nonce for r in j.rows):
            raise AllocationRefused("NONCE_ALREADY_USED", nonce)
        ref = {"kind": BASIS_OWNER, "nonce": nonce, "signature": expect_sig,
               "ttl_s": ttl_s, "expect_journal_head": expect_journal_head}
        row = _new_row(bind, basis=BASIS_OWNER, basis_ref=ref,
                       max_launches=max_launches, ttl_s=ttl_s, now=now, note=note,
                       product_cwd=product_cwd if product_sha else "",
                       product_code_sha=product_sha)
        row["basis_ref"]["row_seal"] = _owner_row_seal(row, key)
        row["allocation_id"] = _content_id(row)
        _append(row, project_dir)
    return row


def grant_supervisor_recovery(project_dir, *, milestone: str, recovery_id: str,
                              failure_fingerprint: str, dedup_key: str = "",
                              ttl_s: int = DEFAULT_TTL_S, note: str = "",
                              now: int | None = None) -> dict:
    """R2C: issue the single AI launch bound to one pending supervisor DIAGNOSE.

    Called ONLY from the runtime's COST_NO_PROGRESS transition, and it may only
    ever succeed for the recovery the supervisor itself just created there:

      * the binding is derived from supervisor state as for every other basis
        (project, FROZEN contract, milestone, control SHA);
      * the work must be THE pending DIAGNOSE on disk right now, for this
        milestone, describing the failure the project state actually observed
        on its failing mission — a fingerprint typed into a project-owned JSON
        is not the observed failure and is refused;
      * the recovery_id and dedup key must be the deterministic identity of
        (milestone, contract, fingerprint, EXACT control commit), so the same
        blocking condition can never be granted a second launch under a second
        name at one commit;
      * max_launches is exactly ONE outer dispatch attempt — the most the
        supervisor ever grants its diagnosis — and every read re-checks it.

    One grant per blocking condition PER EXACT COMMIT, forever. A commit that
    moves the control SHA is not an amnesty — the brake stays tripped and the
    old grant dies of its own STALE_CONTROL_PIN — but it IS a new question:
    the runtime supersedes the old recovery, and the new commit's condition
    may be diagnosed exactly once more, by a NEW grant pinned to the NEW
    commit. A dirty tree moves nothing: the pin is the committed HEAD.

    A forged RecoveryWork alone therefore authorizes nothing: without this
    grant there is no allocation at all, and with it, the work on disk must
    still match it on every read and every spend.
    """
    now = int(time.time()) if now is None else int(now)
    _validate_ttl_and_calls(1, ttl_s)
    bind = observed_binding(project_dir, milestone)
    if not _HEX40.match(bind["control_code_sha"] or ""):
        raise AllocationRefused("CONTROL_SHA_UNOBSERVABLE", bind["control_code_sha"])
    fp = str(failure_fingerprint or "")[:200]
    if not fp:
        raise AllocationRefused("FINGERPRINT_MISSING", "")

    from .escalation_ladder import read_pending_recovery_work
    pending = read_pending_recovery_work(Path(project_dir))
    if pending is None or pending.recovery_id != recovery_id:
        raise AllocationRefused("RECOVERY_NOT_PENDING",
                                {"asked": recovery_id,
                                 "pending": getattr(pending, "recovery_id", "")})
    if pending.kind != "DIAGNOSE":
        raise AllocationRefused("RECOVERY_KIND_MISMATCH", pending.kind)
    if pending.milestone_id != milestone:
        raise AllocationRefused("RECOVERY_MILESTONE_MISMATCH",
                                {"grant": milestone, "work": pending.milestone_id})
    if str(pending.failure_fingerprint or "")[:200] != fp:
        raise AllocationRefused("RECOVERY_FINGERPRINT_MISMATCH",
                                {"grant": fp,
                                 "work": str(pending.failure_fingerprint or "")[:200]})
    # The fingerprint must be the one supervisor state OBSERVED on the failing
    # mission — not a story typed into the project-owned recovery JSON.
    state = _load_state(project_dir)
    mrec = (state.get("missions") or {}).get(pending.parent_mission_id) or {}
    if str(mrec.get("state") or "") != "FAIL" or not mrec.get("last_failure_fingerprint"):
        raise AllocationRefused("RECOVERY_PARENT_NOT_FAILING",
                                {"mission": pending.parent_mission_id,
                                 "recorded_state": str(mrec.get("state") or "")})
    if str(mrec.get("last_failure_fingerprint") or "")[:200] != fp:
        raise AllocationRefused("FINGERPRINT_NOT_THE_OBSERVED_FAILURE", fp)
    # And the identity must be the deterministic one for this condition AT
    # THIS EXACT COMMIT.
    expect_id, expect_key = supervisor_recovery_identity(
        milestone, bind["contract_id"], fp, bind["control_code_sha"])
    if recovery_id != expect_id or str(dedup_key or "") != expect_key \
            or str(pending.dedup_key or "") != expect_key:
        raise AllocationRefused("RECOVERY_ID_NOT_DERIVED",
                                {"recovery_id": recovery_id,
                                 "expected": expect_id,
                                 "dedup_key": str(dedup_key or ""),
                                 "expected_dedup_key": expect_key})

    ref = {"kind": BASIS_SUPERVISOR_RECOVERY, "recovery_id": recovery_id,
           "failure_fingerprint": fp, "dedup_key": expect_key}
    row = _new_row(bind, basis=BASIS_SUPERVISOR_RECOVERY, basis_ref=ref,
                   max_launches=1, ttl_s=ttl_s, now=now, note=note,
                   product_cwd="", product_code_sha="")
    with _Lock(project_dir):
        j = _read_journal(project_dir)
        if j.corrupt:
            raise AllocationRefused("JOURNAL_CORRUPT", j.corrupt)
        for r in j.rows:
            if r.get("schema") != SCHEMA \
                    or r.get("basis") != BASIS_SUPERVISOR_RECOVERY:
                continue
            rref = r.get("basis_ref") or {}
            if (r.get("milestone") == milestone
                    and r.get("control_code_sha") == bind["control_code_sha"]
                    and (rref.get("recovery_id") == recovery_id
                         or rref.get("dedup_key") == expect_key
                         or rref.get("failure_fingerprint") == fp)):
                # One grant per blocking condition PER EXACT COMMIT, forever:
                # retrying the same failure on the same committed code must not
                # buy the diagnosis a second AI launch. A grant pinned to an
                # OLDER commit cannot block this one — it is already dead under
                # its own STALE_CONTROL_PIN — which is what un-blocks a real
                # commit without handing out any amnesty: the brake stays
                # tripped, only the one new bounded launch re-arms.
                raise AllocationRefused(
                    "SUPERVISOR_RECOVERY_ALREADY_GRANTED",
                    {"milestone": milestone,
                     "recovery_id": rref.get("recovery_id"),
                     "failure_fingerprint": fp,
                     "control_code_sha": bind["control_code_sha"],
                     "detail": "one grant per blocking condition per exact "
                               "commit, forever"})
        _append(row, project_dir)
    return row


def grant_multi_ai_review(project_dir, *, milestone: str, contract_id: str,
                          reviewed_sha: str, ttl_s: int = DEFAULT_TTL_S,
                          note: str = "", now: int | None = None) -> dict:
    """Issue the single bounded launch a multi-AI review of ONE exact SHA gets.

    Called ONLY from the runtime's settlement-time review dispatch, and only
    when the COST_NO_PROGRESS brake is actually tripped (the streak may well
    have been driven by failures of OTHER milestones — the brake is global):
    without an allocation the launch gate would refuse the review forever and
    the milestone could never settle. This is NOT a generic exemption — the
    review passes through check_before_launch, its launch is charged by
    consume() like every model call, metered by the common telemetry, and
    bounded to ONE launch per exact (milestone, frozen contract, SHA):

      * the grant is deduped FOREVER per condition — a repeated poll re-grants
        nothing (typed MULTI_AI_REVIEW_ALREADY_GRANTED), so 20 identical polls
        cost exactly one grant and at most one launch;
      * the grant retires itself once the review ledger carries a terminal
        verdict for that exact SHA (see _multi_ai_review_invalid), and verdict
        reuse on the same SHA short-circuits before any dispatch at all;
      * the attempt-key binding means only the review attempt of THIS exact
        SHA can ever spend it — a product builder cannot.
    """
    now = int(time.time()) if now is None else int(now)
    _validate_ttl_and_calls(1, ttl_s)
    sha = str(reviewed_sha or "").strip()
    if not sha:
        raise AllocationRefused("REVIEWED_SHA_MISSING", "")
    bind = observed_binding(project_dir, milestone)
    if not _HEX40.match(bind["control_code_sha"] or ""):
        raise AllocationRefused("CONTROL_SHA_UNOBSERVABLE", bind["control_code_sha"])
    expect_id, expect_key = multi_ai_review_identity(
        milestone, str(bind.get("contract_id") or contract_id or ""), sha)
    ref = {"kind": BASIS_MULTI_AI_REVIEW, "review_id": expect_id,
           "reviewed_sha": sha, "dedup_key": expect_key,
           "contract_id": str(bind.get("contract_id") or contract_id or "")}
    row = _new_row(bind, basis=BASIS_MULTI_AI_REVIEW, basis_ref=ref,
                   max_launches=1, ttl_s=ttl_s, now=now, note=note,
                   product_cwd="", product_code_sha="")
    with _Lock(project_dir):
        j = _read_journal(project_dir)
        if j.corrupt:
            raise AllocationRefused("JOURNAL_CORRUPT", j.corrupt)
        for r in j.rows:
            if r.get("schema") != SCHEMA \
                    or r.get("basis") != BASIS_MULTI_AI_REVIEW:
                continue
            rref = r.get("basis_ref") or {}
            if (r.get("milestone") == milestone
                    and rref.get("reviewed_sha") == sha):
                # One grant per exact candidate SHA, forever: a re-poll must
                # never buy the review a second launch.
                raise AllocationRefused(
                    "MULTI_AI_REVIEW_ALREADY_GRANTED",
                    {"milestone": milestone, "reviewed_sha": sha,
                     "allocation_id": r.get("allocation_id"),
                     "detail": "one bounded review launch per exact SHA, forever"})
        _append(row, project_dir)
    return row


def revoke(project_dir, allocation_id: str, *, reason: str = "") -> dict:
    """Durably cancel an allocation's remaining launches. Never deletes history."""
    row = {"schema": REVOKE_SCHEMA, "allocation_id": allocation_id,
           "reason": reason, "at": _iso(int(time.time()))}
    with _Lock(project_dir):
        _append(row, project_dir)
    return row


# ── spend, counted from two independent sources ──────────────────────────────
def _attempt_ledger_rows(project_dir) -> list:
    from .launch_gate import ledger_path
    from .resource_accounting import fold, read_rows
    try:
        return fold(read_rows(ledger_path(project_dir)))
    except Exception:
        return []


def _used(journal_rows: list, attempt_rows: list, allocation_id: str) -> int:
    """Launches already charged, as the MAX of two independently-owned records.

    The journal is the supervisor's; the attempt ledger is the project's. Deleting
    the spend rows from one does not refill the allocation, because the other still
    remembers. Neither can inflate it either: both count the same attempt keys.

    A REFUND_SCHEMA row for an attempt_key removes it from BOTH sets before the
    max is taken: a refund credits back a specific charged attempt, wherever it
    is recorded, never a bare decrement of a count that both records keep
    independently (that would let one refund silently outrun the other).
    """
    refunded = {r.get("attempt_key") for r in journal_rows
               if r.get("schema") == REFUND_SCHEMA
               and r.get("allocation_id") == allocation_id}
    from_journal = {r.get("attempt_key") for r in journal_rows
                    if r.get("schema") == SPEND_SCHEMA
                    and r.get("allocation_id") == allocation_id} - refunded
    from_ledger = {r.get("attempt_key") for r in attempt_rows
                   if r.get("recovery_allocation_id") == allocation_id} - refunded
    return max(len(from_journal), len(from_ledger | from_journal))


def _orphan_allocation_ids(journal_rows: list, attempt_rows: list) -> set:
    """Allocation ids the project ledger has spent against but the journal lacks.

    That is a deleted or truncated journal. Reading it as "no grants, no spend"
    would hand the next grant a clean slate, so it makes every allocation refuse.
    """
    # 2026-09-18 (TR_DEEP_RESEARCH_3AI_V1 budget lock): the attempt ledger also
    # spends against OWNER_STANDING_AUTHORIZATION ids, which live in this SAME
    # journal file under their own schema (recovery-standing-authorization/v1),
    # not under the per-launch SCHEMA. Restricting "known" to SCHEMA rows made
    # every standing authorization an orphan, so every allocation — including
    # fresh OWNER_AUTHORIZATION grants — refused fail-closed and the milestone
    # stayed BUDGET_EXHAUSTED forever. An orphan is an id referenced by the
    # attempt ledger with NO row of ANY schema behind it in the journal.
    known = {r.get("allocation_id") for r in journal_rows
             if r.get("allocation_id")}
    return {str(r.get("recovery_allocation_id")) for r in attempt_rows
            if r.get("recovery_allocation_id")} - known


def _owner_contract_compatible(project_path: str, old_contract_id: str,
                               new_contract_id: str) -> bool:
    """C42: same reasoning as STALE_CONTROL_PIN/PRODUCT_PIN above, extended to
    the milestone's frozen contract_id — for BASIS_OWNER only.

    R2.3 already draws exactly this line for PASS receipts
    (`mission_receipt.load_verified_mission_receipt`, `validation_contract.
    contract_signature`): `contract_id` folds in the authority identity
    (strategy/spec/roadmap hashes), so a routine authority edit — a roadmap
    task-file path fix, an AUTHORITY_DRIFT repair, a comment — mints a brand
    new `contract_id` even when the ACCEPTANCE SEMANTICS (acceptance
    conditions, required artifacts, product_e2e, forbidden regressions,
    multi_ai_review, product_repo) are byte-identical. An OWNER_AUTHORIZATION
    is the owner's own decision to resume this exact milestone, signed out of
    band before any of those edits existed — not a claim about any one
    contract_id — so a same-signature refreeze must not silently retire it,
    exactly as an intervening commit does not (STALE_CONTROL_PIN) and an
    intervening product commit does not (STALE_PRODUCT_PIN). A REAL semantics
    change (a different acceptance condition, a different product_e2e command)
    still changes the signature and still requires a fresh grant — this is
    reuse by demonstrated compatibility, never by ignoring a mismatch.

    Fail-closed: either frozen contract missing, unreadable, or failing its
    own integrity check (`validation_contract.load_frozen_contract`) refuses
    compatibility — the exemption only ever narrows CONTRACT_MISMATCH, never
    widens what a grant may pay for past what re-derives cleanly.
    """
    from . import validation_contract
    try:
        old = validation_contract.load_frozen_contract(project_path, old_contract_id)
        new = validation_contract.load_frozen_contract(project_path, new_contract_id)
    except validation_contract.ContractIntegrityError:
        return False
    old_sig = str(old.get("contract_signature") or "")
    new_sig = str(new.get("contract_signature") or "")
    return bool(old_sig) and old_sig == new_sig


def _unusable_reason(row: dict, journal_rows: list, attempt_rows: list, *,
                     bind: dict, now: int) -> str:
    """Empty string when this grant may pay for a launch right now; a typed reason otherwise."""
    bad = _typed(row)
    if bad:
        return bad
    if row.get("allocation_id") != _content_id(row):
        return "CONTENT_ID_MISMATCH"      # a bound field was edited after issue
    if row["project_path"] != bind["project_path"]:
        return "WRONG_PROJECT_PATH"
    if row["project"] != bind["project"]:
        return f"WRONG_PROJECT_{row['project']}"
    if row["milestone"] != bind["milestone"]:
        return f"WRONG_MILESTONE_{row['milestone']}"
    if row["contract_id"] != bind["contract_id"] and (
            row["basis"] != BASIS_OWNER or not _owner_contract_compatible(
                bind["project_path"], row["contract_id"], bind["contract_id"])):
        return "CONTRACT_MISMATCH"        # not the frozen contract now in force
    if row["incident_id"] != bind["incident_id"]:
        return "INCIDENT_MISMATCH"        # not the incident that interrupted us
    if not _HEX40.match(bind["control_code_sha"] or ""):
        return "CONTROL_SHA_UNOBSERVABLE"
    if row["control_code_sha"] != bind["control_code_sha"] \
            and row["basis"] != BASIS_OWNER:
        # Deliberate: a new commit does not refresh an allocation, it retires it.
        # The pin is the opposite of the SHA amnesty it replaces.
        #
        # C22: BASIS_OWNER is the one exception. Every other basis names a
        # diagnosis or a proof ABOUT a specific commit, so a new commit really
        # is a new question the old grant cannot answer. An OWNER_AUTHORIZATION
        # is not a claim about any commit at all — it is the owner saying "keep
        # going, N more launches, for M hours", signed out of band before any
        # of those commits existed. Re-pinning it to every intervening commit
        # would silently revoke a live human decision every time the factory
        # (or another worker) commits anything, which is exactly the failure
        # this basis exists to prevent. Milestone/contract/incident/project
        # identity, revocation, TTL and exhaustion below still apply in full —
        # only the exact-commit pin is not one of them for this basis.
        return "STALE_CONTROL_PIN"
    if row["product_code_sha"] and row["basis"] != BASIS_OWNER:
        # C24: the same reasoning as STALE_CONTROL_PIN above, extended to the
        # PRODUCT repo's commit, not just the control repo's. An owner grant
        # observed a product SHA at issue time only because `--product-cwd`
        # happened to be passed in; that observation was never the thing the
        # owner was authorizing. The owner's intent is bounded by milestone,
        # contract, incident, launch count and TTL/revocation — never by
        # "and also nobody, including the supervisor itself, may commit to
        # the product repo in the meantime". Pinning it to that SHA meant a
        # weekly observation-receipt commit made by the supervisor's own
        # loop (or any other worker) silently retired a live human decision
        # the moment it landed — the exact STALE_PRODUCT_PIN failure this
        # exemption exists to close. CONTRACT_MISMATCH above still applies in
        # full: a milestone whose frozen contract_id has since changed still
        # requires a fresh grant, because that is a different authority, not
        # a new product commit.
        if not row["product_cwd"]:
            return "PRODUCT_PIN_UNVERIFIABLE"
        seen = _observed_product_sha(row["product_cwd"])
        if not seen:
            return "PRODUCT_SHA_UNOBSERVABLE"
        if seen != row["product_code_sha"]:
            return "STALE_PRODUCT_PIN"
    if any(r.get("schema") == REVOKE_SCHEMA
           and r.get("allocation_id") == row["allocation_id"] for r in journal_rows):
        return "REVOKED"
    if now >= row["expires_at"]:
        return "EXPIRED"
    used = _used(journal_rows, attempt_rows, row["allocation_id"])
    if used >= row["max_launches"]:
        return f"EXHAUSTED_{used}/{row['max_launches']}"
    why = _basis_invalid(row)
    if why:
        return why
    return ""


def available(project_dir, *, milestone: str, control_code_sha: str = "",
              expect_project: str = "", now: int | None = None,
              attempt_key: str = "", basis: str = "",
              resuming_attempt_key: str = "") -> dict:
    """Peek: which allocation would pay for the next dispatch, and why not the rest.

    Never spends — the charge happens in `launch_gate.open_account`, before execve.
    Every refusal is typed; an unreadable binding or a damaged journal is a
    refusal, never an empty result that looks like a clean slate.

    `attempt_key` is optional spend-side context: when given, a SUPERVISOR_RECOVERY
    grant only pays for the dispatch of ITS OWN recovery record (the key the
    dispatcher derived from it) — never for a product builder that happens to be
    the next launch past the brake.

    `basis` is optional spend-side context too: when given, only a grant issued
    on exactly that basis is considered. Used to charge a launch admitted past
    the milestone's own `max_model_calls` cap by `observe_budget`'s
    OWNER_AUTHORIZATION extension against THAT owner grant specifically — never
    against a VERIFIED_FIX/SUPERVISOR_RECOVERY/MULTI_AI_REVIEW grant that
    happens to also be usable but does not raise the cap.

    `resuming_attempt_key` (L2_runtime_core-009): when `consume` re-validates a
    reservation already charged to that attempt (crash between the charge and
    the OPEN row), the attempt's OWN spend must not count against the grant —
    otherwise the last reserved launch is refused precisely because it was
    reserved. Every other check (revocation, expiry, identity, pins) still runs.
    """
    now = int(time.time()) if now is None else int(now)
    try:
        bind = observed_binding(project_dir, milestone,
                                control_code_sha=control_code_sha)
    except BindingUnobservable as exc:
        return {"allocation_id": "", "refusals": [],
                "detail": f"binding unobservable: {exc.reason}: {exc.detail}"}
    if expect_project and bind["project"] != expect_project:
        return {"allocation_id": "", "refusals": [],
                "detail": (f"project identity disagrees: caller says "
                           f"{expect_project!r}, state says {bind['project']!r}")}
    j = _read_journal(project_dir)
    if j.corrupt:
        return {"allocation_id": "", "refusals": [],
                "detail": f"allocation journal is damaged: {j.corrupt}"}
    rows = j.rows
    if resuming_attempt_key:
        rows = [r for r in rows if not (r.get("schema") == SPEND_SCHEMA
                                        and r.get("attempt_key") == resuming_attempt_key)]
    attempts = _attempt_ledger_rows(project_dir)
    orphans = _orphan_allocation_ids(rows, attempts)
    if orphans:
        return {"allocation_id": "", "refusals": [],
                "detail": (f"attempts reference allocation(s) the journal does not "
                           f"have: {sorted(orphans)} — journal deleted or truncated")}
    refusals = []
    for row in (r for r in rows if r.get("schema") == SCHEMA
                and (not basis or r.get("basis") == basis)):
        why = _unusable_reason(row, rows, attempts, bind=bind, now=now)
        if not why and attempt_key and row.get("basis") == BASIS_SUPERVISOR_RECOVERY:
            # R2C: the grant belongs to the recovery it was issued for. A
            # product builder's attempt key does not match it, so the single
            # AI launch cannot be stolen by the next dispatch past the brake.
            ref = row.get("basis_ref") or {}
            if not str(attempt_key).startswith(
                    f"recovery:{ref.get('recovery_id')}:"):
                why = "RECOVERY_BOUND_ATTEMPT_MISMATCH"
        if not why and attempt_key and row.get("basis") == BASIS_MULTI_AI_REVIEW:
            # Same anti-theft binding for the review budget: the grant pays
            # only the review attempt OF ITS OWN exact SHA — never a product
            # builder, never a review of a different candidate.
            mref = row.get("basis_ref") or {}
            if not str(attempt_key).startswith(
                    f"review:{row.get('milestone')}:"
                    f"{str(mref.get('reviewed_sha') or '')[:16]}:"):
                why = "MULTI_AI_REVIEW_BOUND_ATTEMPT_MISMATCH"
        if not why:
            used = _used(rows, attempts, row["allocation_id"])
            return {"allocation_id": row["allocation_id"], "basis": row["basis"],
                    "max_launches": row["max_launches"], "used": used,
                    "remaining": row["max_launches"] - used,
                    "unit": "outer_dispatch_attempt",
                    "expires_at_iso": row.get("expires_at_iso", ""),
                    "contract_id": row["contract_id"],
                    "incident_id": row["incident_id"],
                    "note": ("a bounded number of dispatch attempts — NOT a product "
                             "delta, and NOT a count of model calls")}
        refusals.append({"allocation_id": str(row.get("allocation_id"))[:24],
                         "why": why})
    return {"allocation_id": "", "refusals": refusals,
            "detail": ("no usable recovery allocation: "
                       + ("; ".join(f"{r['allocation_id']}:{r['why']}"
                                    for r in refusals) if refusals
                          else "none granted for this project/milestone"))}


def owner_budget_extension(project_dir, *, milestone: str, control_code_sha: str = "",
                           expect_project: str = "", now: int | None = None) -> dict:
    """Read-only: how many extra launches active OWNER_AUTHORIZATION grants add
    to this milestone's own `max_model_calls` cap, right now.

    C19: a milestone's launch cap (`launch_gate.observe_budget`) used to be
    blind to recovery allocations entirely — an owner grant issued specifically
    to release COST_NO_PROGRESS on a milestone that had also exhausted its
    `max_model_calls` cap released the brake and then hit BUDGET_EXHAUSTED
    anyway, making the grant inoperative. This is the read side of the fix: it
    reuses the SAME re-derivation `available()` performs (binding, signature,
    pins, TTL, spend, each re-checked now, not admired from the journal) —
    restricted to grants issued on `BASIS_OWNER` and to this milestone, because
    only an owner's own authorization is understood to widen a milestone's
    declared budget; a VERIFIED_FIX / SUPERVISOR_RECOVERY / MULTI_AI_REVIEW
    grant releases COST_NO_PROGRESS on its own terms but does not, by itself,
    change what the milestone's contract declared as its cap.

    Never spends. Consumption goes through `consume(..., basis=BASIS_OWNER)`,
    exactly as every other basis charges through `consume()` — see
    `launch_gate.open_account`.

    Returns `{"remaining": N, "grants": [...]}` — `remaining` is the SUM of
    `max_launches - used` over every OWNER grant that is valid for this
    project/milestone/contract/incident/control-SHA right now, not expired,
    not revoked, and whose signature/seal still verify. An unobservable
    binding, a damaged journal, or a project identity mismatch fails closed to
    `remaining: 0` — the same posture as any other "cannot prove it" case in
    this module: it can only ever narrow what is authorized, never widen it
    past what re-derives cleanly.
    """
    now = int(time.time()) if now is None else int(now)
    try:
        bind = observed_binding(project_dir, milestone,
                                control_code_sha=control_code_sha)
    except BindingUnobservable as exc:
        return {"remaining": 0, "grants": [],
                "detail": f"binding unobservable: {exc.reason}: {exc.detail}"}
    if expect_project and bind["project"] != expect_project:
        return {"remaining": 0, "grants": [],
                "detail": (f"project identity disagrees: caller says "
                           f"{expect_project!r}, state says {bind['project']!r}")}
    j = _read_journal(project_dir)
    if j.corrupt:
        return {"remaining": 0, "grants": [],
                "detail": f"allocation journal is damaged: {j.corrupt}"}
    attempts = _attempt_ledger_rows(project_dir)
    orphans = _orphan_allocation_ids(j.rows, attempts)
    if orphans:
        return {"remaining": 0, "grants": [],
                "detail": (f"attempts reference allocation(s) the journal does not "
                           f"have: {sorted(orphans)} — journal deleted or truncated")}
    grants = []
    for row in (r for r in j.rows if r.get("schema") == SCHEMA
                and r.get("basis") == BASIS_OWNER):
        if _unusable_reason(row, j.rows, attempts, bind=bind, now=now):
            continue                      # wrong milestone, expired, revoked,
                                           # stale pin, bad seal/signature, spent
        used = _used(j.rows, attempts, row["allocation_id"])
        remaining = max(0, row["max_launches"] - used)
        if remaining > 0:
            grants.append({"allocation_id": row["allocation_id"],
                           "max_launches": row["max_launches"], "used": used,
                           "remaining": remaining,
                           "expires_at_iso": row.get("expires_at_iso", "")})
    return {"remaining": sum(g["remaining"] for g in grants), "grants": grants}


def consume(project_dir, *, attempt_key: str, milestone: str,
            control_code_sha: str = "", expect_project: str = "",
            now: int | None = None, basis: str = "") -> dict:
    """Charge exactly one dispatch attempt, durably, bound to THIS attempt. Fail-closed.

    Called from `launch_gate.open_account` before the OPEN row and before execve, so
    a process killed during the model call has still spent its launch and a restart
    replays the same arithmetic rather than replenishing anything.

    A reservation is used ONCE. Replaying the same attempt_key does not buy a second
    launch: it is honoured only when the attempt ledger has no row for that key at
    all — the narrow window where the crash landed between the charge and the OPEN
    row — and even then the expiry, the pins and the basis are re-checked. An
    attempt_key that already has an OPEN or TERMINAL row has already launched, and
    asking again is refused.

    `basis`, when given, restricts the charge to a grant issued on exactly that
    basis — see `available()`.
    """
    now = int(time.time()) if now is None else int(now)
    if not str(attempt_key or "").strip():
        raise AllocationUnavailable("NO_ATTEMPT_KEY",
                                    {"detail": "a charge must name the attempt it "
                                               "paid for"})
    with _Lock(project_dir):
        j = _read_journal(project_dir)
        if j.corrupt:
            raise AllocationUnavailable("JOURNAL_CORRUPT", {"detail": j.corrupt})
        picked = available(project_dir, milestone=milestone,
                           control_code_sha=control_code_sha,
                           expect_project=expect_project, now=now,
                           attempt_key=attempt_key, basis=basis)
        prior = [r for r in j.rows if r.get("schema") == SPEND_SCHEMA
                 and r.get("attempt_key") == attempt_key]
        if prior:
            launched = any(r.get("attempt_key") == attempt_key
                           for r in _attempt_ledger_rows(project_dir))
            if launched:
                raise AllocationUnavailable(
                    "RESERVATION_ALREADY_USED",
                    {"attempt_key": attempt_key,
                     "detail": "this attempt already opened an account against its "
                               "charge; a replay is a second launch, not a resume"})
            # L2_runtime_core-009: re-validate the reservation WITHOUT counting
            # its own spend — the last reserved launch was otherwise refused
            # as EXHAUSTED by the very charge it is resuming.
            picked = available(project_dir, milestone=milestone,
                               control_code_sha=control_code_sha,
                               expect_project=expect_project, now=now,
                               attempt_key=attempt_key, basis=basis,
                               resuming_attempt_key=attempt_key)
            if picked.get("allocation_id") != prior[-1].get("allocation_id"):
                raise AllocationUnavailable(
                    "RESERVATION_NO_LONGER_VALID",
                    {"attempt_key": attempt_key, "reason": picked})
            return dict(prior[-1])        # same dispatch resuming its own charge
        if not picked.get("allocation_id"):
            raise AllocationUnavailable("NO_RECOVERY_ALLOCATION", picked)
        seq = picked["used"] + 1
        spend = {"schema": SPEND_SCHEMA, "allocation_id": picked["allocation_id"],
                 "attempt_key": attempt_key, "project": expect_project,
                 "project_path": canonical_project_path(project_dir),
                 "milestone": milestone,
                 "control_code_sha": control_code_sha or _observed_control_sha(),
                 "seq": seq, "max_launches": picked["max_launches"],
                 "remaining_after": picked["max_launches"] - seq,
                 "unit": "outer_dispatch_attempt", "spent_at": _iso(now)}
        _append(spend, project_dir)
    return spend


def refund(project_dir, *, attempt_key: str, allocation_id: str,
          reason: str = "", now: int | None = None) -> dict:
    """Credit back exactly one launch for `attempt_key`, idempotently.

    `open_account` charges a launch BEFORE execve — durable ahead of a SIGKILL,
    by design (see `consume`'s docstring). That means an attempt that turns out,
    only AFTER the fact, to have never launched a model at all — a dispatcher-side
    preflight refusal whose own receipt says so
    (`runtime._dispatcher_refused_before_engine`), or a runtime-side refusal that
    never even reached the executor (PREFLIGHT_REFUSED) — has already spent a
    grant launch for nothing. `close_account` calls this when it closes such an
    attempt with `consumes_model_quota=False`, so the grant is not silently
    drained by attempts that never happened.

    Refunding is bound to a SPEND this exact attempt_key actually made against
    THIS exact allocation: it cannot invent a credit for a launch nobody charged,
    and it cannot be replayed into a second credit — one attempt_key refunds at
    most once, exactly like one attempt_key spends at most once.
    """
    now = int(time.time()) if now is None else int(now)
    if not str(attempt_key or "").strip():
        raise AllocationUnavailable("NO_ATTEMPT_KEY",
                                    {"detail": "a refund must name the attempt "
                                               "it credits back"})
    if not str(allocation_id or "").strip():
        raise AllocationUnavailable("NO_ALLOCATION_ID",
                                    {"detail": "a refund must name the allocation "
                                               "it credits back to"})
    with _Lock(project_dir):
        j = _read_journal(project_dir)
        if j.corrupt:
            raise AllocationUnavailable("JOURNAL_CORRUPT", {"detail": j.corrupt})
        prior_refund = next(
            (r for r in j.rows if r.get("schema") == REFUND_SCHEMA
             and r.get("attempt_key") == attempt_key
             and r.get("allocation_id") == allocation_id), None)
        if prior_refund is not None:
            return dict(prior_refund)     # already credited — never a second time
        spent = any(r.get("schema") == SPEND_SCHEMA
                   and r.get("attempt_key") == attempt_key
                   and r.get("allocation_id") == allocation_id
                   for r in j.rows)
        if not spent:
            raise AllocationUnavailable(
                "NO_SPEND_TO_REFUND",
                {"attempt_key": attempt_key, "allocation_id": allocation_id,
                 "detail": "this attempt never charged this allocation — "
                           "nothing to credit back"})
        row = {"schema": REFUND_SCHEMA, "allocation_id": allocation_id,
               "attempt_key": attempt_key, "reason": str(reason or "")[:200],
               "refunded_at": _iso(now)}
        _append(row, project_dir)
    return row


def state(project_dir, *, now: int | None = None) -> list:
    """Every allocation with its live spend, for the operator and for receipts."""
    now = int(time.time()) if now is None else int(now)
    j = _read_journal(project_dir)
    if j.corrupt:
        return [{"journal": "CORRUPT", "detail": j.corrupt}]
    attempts = _attempt_ledger_rows(project_dir)
    out = []
    for row in (r for r in j.rows if r.get("schema") == SCHEMA):
        try:
            bind = observed_binding(project_dir, str(row.get("milestone") or ""))
            why = _unusable_reason(row, j.rows, attempts, bind=bind, now=now)
        except BindingUnobservable as exc:
            why = f"BINDING_UNOBSERVABLE_{exc.reason}"
        used = _used(j.rows, attempts, row.get("allocation_id"))
        out.append({**{k: row.get(k) for k in
                       ("allocation_id", "project", "milestone", "contract_id",
                        "incident_id", "basis", "control_code_sha",
                        "product_code_sha", "max_launches", "expires_at_iso")},
                    "unit": "outer_dispatch_attempt", "used": used,
                    "remaining": max(0, int(row.get("max_launches") or 0) - used),
                    "usable": not why, "blocked_by": why})
    return out


# ── the trusted supervisor command ───────────────────────────────────────────
def _p(args) -> Path:
    return Path(args.project_dir).resolve()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m factory_v2.recovery_allocation",
        description="Issue and inspect finite COST_NO_PROGRESS recovery "
                    "allocations. --launches counts OUTER DISPATCH ATTEMPTS.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--project-dir", required=True)
        p.add_argument("--milestone", required=True)
        p.add_argument("--launches", type=int, default=1,
                       help="outer dispatch attempts, not model calls")
        p.add_argument("--ttl-s", type=int, default=DEFAULT_TTL_S)
        p.add_argument("--note", default="")
        p.add_argument("--product-cwd", default="")

    g1 = sub.add_parser("grant-verified-fix",
                        help="allocate from an indexed VerifiedFixReceipt")
    common(g1)
    g1.add_argument("--receipt-id", required=True)
    g1.add_argument("--content-hash", required=True)
    g1.add_argument("--store-project", default="")

    g2 = sub.add_parser("sign-owner", help="print the signature for an owner grant")
    common(g2)
    g2.add_argument("--nonce", required=True)

    g3 = sub.add_parser("grant-owner", help="apply a signed owner authorization")
    common(g3)
    g3.add_argument("--nonce", required=True)
    g3.add_argument("--signature", required=True)
    g3.add_argument("--expect-journal-head", required=True)

    gstanding_sign = sub.add_parser("sign-standing",
                                    help="print signature for a project-wide standing authorization")
    gstanding_sign.add_argument("--project-dir", required=True)
    gstanding_sign.add_argument("--daily-launch-cap", type=int, required=True)
    gstanding_sign.add_argument("--ttl-s", type=int, required=True)
    gstanding_sign.add_argument("--nonce", required=True)

    gstanding_grant = sub.add_parser("grant-standing",
                                     help="apply a signed project-wide standing authorization")
    gstanding_grant.add_argument("--project-dir", required=True)
    gstanding_grant.add_argument("--daily-launch-cap", type=int, required=True)
    gstanding_grant.add_argument("--ttl-s", type=int, required=True)
    gstanding_grant.add_argument("--nonce", required=True)
    gstanding_grant.add_argument("--signature", required=True)
    gstanding_grant.add_argument("--expect-journal-head", required=True)
    gstanding_grant.add_argument("--note", default="")

    g4 = sub.add_parser("state", help="show allocations and their spend")
    g4.add_argument("--project-dir", required=True)

    g5 = sub.add_parser("head", help="print the journal head for a CAS grant")
    g5.add_argument("--project-dir", required=True)

    g6 = sub.add_parser("binding", help="show the binding derived from state")
    g6.add_argument("--project-dir", required=True)
    g6.add_argument("--milestone", required=True)

    g7 = sub.add_parser("revoke", help="cancel an allocation's remaining launches")
    g7.add_argument("--project-dir", required=True)
    g7.add_argument("--allocation-id", required=True)
    g7.add_argument("--reason", default="")

    g8 = sub.add_parser(
        "grant-supervisor-recovery",
        help="issue the single AI launch bound to the pending supervisor "
             "DIAGNOSE (normally granted automatically at the CNP transition)")
    g8.add_argument("--project-dir", required=True)
    g8.add_argument("--milestone", required=True)
    g8.add_argument("--recovery-id", required=True)
    g8.add_argument("--fingerprint", required=True)
    g8.add_argument("--dedup-key", default="")
    g8.add_argument("--ttl-s", type=int, default=DEFAULT_TTL_S)
    g8.add_argument("--note", default="")

    a = ap.parse_args(argv)
    try:
        if a.cmd == "head":
            print(journal_head(_p(a)))
        elif a.cmd == "binding":
            print(json.dumps(observed_binding(_p(a), a.milestone), indent=2,
                             sort_keys=True))
        elif a.cmd == "state":
            print(json.dumps(state(_p(a)), indent=2, sort_keys=True))
        elif a.cmd == "revoke":
            print(json.dumps(revoke(_p(a), a.allocation_id, reason=a.reason),
                             sort_keys=True))
        elif a.cmd == "grant-verified-fix":
            print(json.dumps(grant_from_verified_fix(
                _p(a), milestone=a.milestone,
                receipt_ref={"receipt_id": a.receipt_id,
                             "content_hash": a.content_hash},
                receipt_project_id=a.store_project, max_launches=a.launches,
                ttl_s=a.ttl_s, product_cwd=a.product_cwd, note=a.note),
                indent=2, sort_keys=True))
        elif a.cmd == "sign-owner":
            pd = _p(a)
            head = journal_head(pd)
            bind = observed_binding(pd, a.milestone)
            bind = {**bind, "product_code_sha":
                    _observed_product_sha(a.product_cwd) if a.product_cwd else ""}
            sig = sign_owner_authorization(bind, max_launches=a.launches,
                                           ttl_s=a.ttl_s, nonce=a.nonce,
                                           expect_journal_head=head)
            print(json.dumps({"binding": bind, "expect_journal_head": head,
                              "signature": sig,
                              "unit": "outer_dispatch_attempt"},
                             indent=2, sort_keys=True))
        elif a.cmd == "grant-owner":
            print(json.dumps(grant_owner_authorization(
                _p(a), milestone=a.milestone, nonce=a.nonce, signature=a.signature,
                expect_journal_head=a.expect_journal_head, max_launches=a.launches,
                ttl_s=a.ttl_s, product_cwd=a.product_cwd, note=a.note),
                indent=2, sort_keys=True))
        elif a.cmd == "sign-standing":
            pd = _p(a)
            head = journal_head(pd)
            bind = _standing_binding(pd)
            sig = sign_standing_authorization(bind, daily_launch_cap=a.daily_launch_cap,
                                               ttl_s=a.ttl_s, nonce=a.nonce,
                                               expect_journal_head=head)
            print(json.dumps({"binding": bind, "expect_journal_head": head,
                              "signature": sig, "daily_launch_cap": a.daily_launch_cap,
                              "ttl_s": a.ttl_s}, indent=2, sort_keys=True))
        elif a.cmd == "grant-standing":
            print(json.dumps(grant_standing_authorization(
                _p(a), nonce=a.nonce, signature=a.signature,
                expect_journal_head=a.expect_journal_head,
                daily_launch_cap=a.daily_launch_cap, ttl_s=a.ttl_s,
                note=a.note), indent=2, sort_keys=True))
        elif a.cmd == "grant-supervisor-recovery":
            print(json.dumps(grant_supervisor_recovery(
                _p(a), milestone=a.milestone, recovery_id=a.recovery_id,
                failure_fingerprint=a.fingerprint, dedup_key=a.dedup_key,
                ttl_s=a.ttl_s, note=a.note), indent=2, sort_keys=True))
    except (AllocationRefused, BindingUnobservable) as exc:
        print(f"REFUSED: {exc.reason}: {exc.detail}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":       # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
