#!/usr/bin/env python3
"""Tombstones for dead registry entries (FE — « tombstones des repos morts »).

MEASURED DEFECT (2026-08-07, this repo)
---------------------------------------
The per-project registry is `projects/<p>/roadmap.jsonl`: each row carries an absolute
`worktree` path (factoryctl.Factory.prepare writes it, factoryctl.py:378/393). The registry
has no notion of an entry whose path no longer exists. Measured on this machine:

  * 11 rows carry a worktree path; 6 of them point at directories that are gone
    (projects/job-radar/roadmap.jsonl lines 2-7, JR-M1..JR-M5, JR-M3B).
  * 7 of 188 `evidence` references in the events ledgers are unresolvable, all in
    projects/job-radar/events.jsonl (lines 1, 9, 10, 24, 38, 48, 49).
  * None of those 6 rows ever reached `done`/`merge_sha`, so factoryctl.promote()
    (factoryctl.py:955, the only code path that removes a worktree on purpose) never ran on
    them. The directories were deleted out of band and the registry recorded nothing.

Today's behaviour on such an entry, as executed:
  * `factoryctl status` prints all 7 job-radar milestones as `retro_done`, exit 0. A dead
    entry is indistinguishable from a live one. Silence.
  * `factoryctl mark-spec-ready --milestone JR-M1` dies with an uncaught
    `RuntimeError: missing/incomplete spec: .../JR-M1/specs/JR-M1/spec.md`
    (factoryctl.py:408) — it blames the spec, not the vanished worktree. Misdiagnosis.
  * `factoryctl verify` prints `OK` (factoryctl.py:1109 only walks the hash chain).
  * `_require_runtime_receipt` (factoryctl.py:647-651) and
    `_spec_ready_evidence_problems` (factoryctl.py:275-277) do notice a missing worktree,
    but only per-call and only as an ad-hoc string; nothing is ever recorded, so the next
    call rediscovers it from scratch, forever.

WHAT THIS MODULE ADDS
---------------------
An append-only, hash-chained tombstone ledger `<state_dir>/tombstones.jsonl`, plus the
metrics/report projections that read it. Rules, in order of importance:

  1. NEVER ERASE. A tombstone is a new line in a new file. roadmap.jsonl and events.jsonl
     are never touched. `revive` appends a REVIVED record; it does not remove the
     TOMBSTONED one. This is an audit register — history does not disappear.
  2. NEVER SUPPOSE. `mark_dead` accepts only an observation produced by `observe_entry`,
     which stat()s the real path and stamps `checked_at`. An observation whose verdict is
     not DEAD is refused, and the path is re-stat()ed at write time so a stale observation
     cannot bury a resurrected entry.
  3. TOMBSTONED ENTRIES LEAVE THE DENOMINATOR, NOT THE DISPLAY. `metrics()` computes
     `denominator` over live entries only, while `tombstoned` / `tombstoned_by_status` are
     reported alongside and `report()` still prints every tombstoned entry, marked.

INTEGRATION POINT (deliberately NOT wired here)
-----------------------------------------------
factory/bin/factoryctl.py is owned by another agent in this campaign, so it is untouched.
To wire this in, `Factory.status()` (factoryctl.py:241) should call
`tombstone.report(self.state)` after `verify_evidence()`, and `Factory.prepare()` /
`mark_spec_ready()` should call `tombstone.observe_entry(row, self.state)` and raise a
named DEAD_REGISTRY_ENTRY error instead of the current "missing/incomplete spec". Both are
additive: this module is a pure read/append sidecar and importing it changes no behaviour.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import sys

SCHEMA = "factory-registry-tombstone/v1"
TOMBSTONES_FILENAME = "tombstones.jsonl"

# Liveness verdicts. Only DEAD may be tombstoned.
LIVE = "LIVE"
DEAD = "DEAD"
NOT_APPLICABLE = "NOT_APPLICABLE"

# Reasons, each tied to a specific filesystem observation — never a guess.
REASON_MISSING = "WORKTREE_PATH_MISSING"
REASON_EMPTY = "WORKTREE_PATH_EMPTY"
REASON_NOT_A_DIR = "WORKTREE_PATH_NOT_A_DIRECTORY"
DEAD_REASONS = frozenset({REASON_MISSING, REASON_EMPTY, REASON_NOT_A_DIR})

STATE_TOMBSTONED = "TOMBSTONED"
STATE_REVIVED = "REVIVED"

# Death KINDS (B-03). `reason` says WHY the path is gone; `kind` says WHAT the entry IS —
# an archived campaign run whose disposable sandbox is gone by design, or a live project whose
# worktree vanished out of band. The registry could not tell these apart (same dead worktree,
# opposite meaning), so it tombstoned nothing outside job-radar. Classification is evidence-based:
# a finished campaign run leaves a result.json proving completion; a project leaves none.
KIND_ARCHIVED_RUN = "ARCHIVED_RUN"
KIND_LOST_WORKTREE = "LOST_WORKTREE"
KIND_UNCLASSIFIED = "UNCLASSIFIED"  # tombstones written before B-03 carry no stored kind
DEATH_KINDS = frozenset({KIND_ARCHIVED_RUN, KIND_LOST_WORKTREE})

# A finished campaign run records its outcome here, as a sibling of the state dir. Its presence
# is the measurable difference between "archived run" and "live project": a project registry has
# no such file.
RESULT_FILENAME = "result.json"
RESULT_COMPLETION_KEYS = ("time_to_pass_seconds", "oracle_result")

def utc() -> str:
    """Same stamp format as factoryctl.utc() — one clock vocabulary across the registry."""
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_jsonl(path) -> list[dict]:
    p = pathlib.Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def roadmap_path(state_dir) -> pathlib.Path:
    return pathlib.Path(state_dir) / "roadmap.jsonl"


def events_path(state_dir) -> pathlib.Path:
    return pathlib.Path(state_dir) / "events.jsonl"


def tombstones_path(state_dir) -> pathlib.Path:
    return pathlib.Path(state_dir) / TOMBSTONES_FILENAME


def rows(state_dir) -> list[dict]:
    return load_jsonl(roadmap_path(state_dir))


# ---------------------------------------------------------------------------
# 1. OBSERVE — real stat() calls, dated. Nothing here writes.
# ---------------------------------------------------------------------------

def observe_entry(row: dict, state_dir=None, now: str | None = None) -> dict:
    """stat() this registry row's worktree path and return a dated observation.

    A row with no `worktree` is NOT_APPLICABLE, not dead: it was simply never prepared
    (factoryctl only sets `worktree` in prepare(), factoryctl.py:378). Calling that dead
    would be exactly the supposition this module exists to forbid.

    An existing but empty directory counts as DEAD: `git worktree add` never produces an
    empty directory (it always writes at least a `.git` file), so an empty one is the
    residue of a deletion, not a worktree.
    """
    mid = row.get("id")
    wt = row.get("worktree")
    obs = {
        "schema": SCHEMA,
        "milestone": mid,
        "worktree": wt,
        "checked_at": now or utc(),
        "status_on_row": row.get("status"),
    }
    if not wt:
        obs.update({"liveness": NOT_APPLICABLE, "reason": None, "exists": None,
                    "is_dir": None, "entry_count": None})
        return obs
    p = pathlib.Path(wt)
    exists = p.exists()
    is_dir = p.is_dir() if exists else False
    entry_count = None
    if is_dir:
        try:
            entry_count = len(os.listdir(p))
        except OSError:
            entry_count = None
    obs.update({"exists": exists, "is_dir": is_dir, "entry_count": entry_count})
    if not exists:
        obs.update({"liveness": DEAD, "reason": REASON_MISSING})
    elif not is_dir:
        obs.update({"liveness": DEAD, "reason": REASON_NOT_A_DIR})
    elif entry_count == 0:
        obs.update({"liveness": DEAD, "reason": REASON_EMPTY})
    else:
        obs.update({"liveness": LIVE, "reason": None})
    return obs


def scan(state_dir, now: str | None = None) -> list[dict]:
    """Observe every row in this registry. Read-only."""
    return [observe_entry(r, state_dir, now=now) for r in rows(state_dir)]


def dead_observations(state_dir, now: str | None = None) -> list[dict]:
    return [o for o in scan(state_dir, now=now) if o["liveness"] == DEAD]


# ---------------------------------------------------------------------------
# 1b. CLASSIFY — same dead worktree, opposite meaning (B-03)
# ---------------------------------------------------------------------------
# A campaign run lays out as <run>/state/{roadmap,events}.jsonl with <run>/result.json beside it.
# The worktree it prepared was a disposable sandbox; once the run finishes, that sandbox is gone by
# design. A project registry has no result.json — its missing worktree is a defect, not a lifecycle.


def _campaign_run_result(state_dir) -> dict | None:
    """Read the result.json sibling of a campaign-run state dir, if any. Never raises — a missing
    or malformed result is reported as None, not an exception (Law 19: an accessory never breaks
    its base). Absent result is itself the signal: it means this is not (or not yet) a finished run."""
    run_dir = pathlib.Path(state_dir).parent  # state/ -> <run>/
    result = run_dir / RESULT_FILENAME
    if not result.is_file():
        return None
    try:
        obj = json.loads(result.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def classify_death(state_dir, observation: dict) -> dict:
    """Place a DEAD observation in one of two opposite situations.

      * ARCHIVED_RUN — a finished campaign run whose disposable worktree is gone by design.
        Evidence (both required, never guessed):
          1. the state dir sits under a campaign-run layout (.../<phase>-runs/run-<N>/state), AND
          2. a sibling result.json records a finished run (carries a completion key).
      * LOST_WORKTREE — anything else: a project registry whose worktree vanished out of band.

    A campaign run WITHOUT a completion record is classified LOST_WORKTREE, not ARCHIVED_RUN: we
    cannot claim "archived" without proof the run ended, and an unfinished run whose worktree
    vanished is a defect, not bookkeeping. This is the supposition this module exists to forbid.
    """
    if observation.get("liveness") != DEAD:
        return {"kind": None, "reason": "not_dead", "evidence": None}
    sd = pathlib.Path(state_dir)
    run_name = sd.parent.name
    phase_name = sd.parent.parent.name
    is_run_layout = run_name.startswith("run-") and phase_name.endswith("-runs")
    result = _campaign_run_result(sd)
    completed = bool(result) and any(result.get(k) for k in RESULT_COMPLETION_KEYS)
    if is_run_layout and completed:
        return {
            "kind": KIND_ARCHIVED_RUN,
            "reason": ("campaign run recorded a finished outcome; its worktree was a disposable "
                       "sandbox, gone by design"),
            "evidence": {
                "result_path": str(sd.parent / RESULT_FILENAME),
                "run_layout": f"{phase_name}/{run_name}",
                "oracle_result": result.get("oracle_result"),
                "time_to_pass_seconds": result.get("time_to_pass_seconds"),
            },
        }
    return {
        "kind": KIND_LOST_WORKTREE,
        "reason": ("no completion record: a registry whose worktree vanished out of band, not a "
                   "finished run"),
        "evidence": {
            "run_layout": is_run_layout,
            "result_path": str(sd.parent / RESULT_FILENAME) if is_run_layout else None,
            "result_present": result is not None,
        },
    }


# ---------------------------------------------------------------------------
# 2. LAST KNOWN PROOF — derived from the ledger that is still on disk.
# ---------------------------------------------------------------------------

def last_known_proof(state_dir, mid: str, row: dict | None = None) -> dict:
    """What the registry can still prove about this milestone after its worktree is gone.

    Derived, never invented: the last hash-chained event for the milestone, the SHAs the
    roadmap row pinned, and a count of how many of the milestone's recorded evidence paths
    still resolve on disk right now.
    """
    if row is None:
        row = next((r for r in rows(state_dir) if r.get("id") == mid), None) or {}
    evs = [e for e in load_jsonl(events_path(state_dir)) if e.get("milestone") == mid]
    recorded = [e.get("evidence") for e in evs if e.get("evidence")]
    base = pathlib.Path(state_dir)
    resolvable = [e for e in recorded
                  if (pathlib.Path(e) if pathlib.Path(e).is_absolute()
                      else base / pathlib.Path(e)).exists()]
    last = evs[-1] if evs else None
    return {
        "status_on_row": row.get("status"),
        "candidate_sha": row.get("candidate_sha"),
        "base_sha": row.get("base_sha"),
        "branch": row.get("branch"),
        "merge_sha": row.get("merge_sha"),
        "spec_sha256": row.get("spec_sha256"),
        "last_event": None if last is None else {
            "stage": last.get("stage"),
            "actor": last.get("actor"),
            "ts": last.get("ts"),
            "event_hash": last.get("event_hash"),
        },
        "evidence_paths_recorded": len(recorded),
        "evidence_paths_resolvable": len(resolvable),
    }


# ---------------------------------------------------------------------------
# 3. THE LEDGER — append-only, hash-chained, never rewritten.
# ---------------------------------------------------------------------------

def _record_hash(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def read_ledger(state_dir) -> list[dict]:
    return load_jsonl(tombstones_path(state_dir))


def verify_ledger(state_dir) -> None:
    """Same tamper-evidence contract as factoryctl.verify_events(): each record hashes its
    own body including the previous record's hash. Raises on any divergence."""
    prev = ""
    for n, rec in enumerate(read_ledger(state_dir), 1):
        got = rec.get("record_hash")
        body = dict(rec)
        body.pop("record_hash", None)
        if _record_hash(body) != got or body.get("prev_hash") != prev:
            raise RuntimeError(f"tombstone ledger chain invalid at line {n}")
        prev = got


def _append(state_dir, body: dict) -> dict:
    path = tombstones_path(state_dir)
    existing = read_ledger(state_dir)
    body["prev_hash"] = existing[-1]["record_hash"] if existing else ""
    body["record_hash"] = _record_hash(body)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(body, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return body


def current_states(state_dir) -> dict[str, dict]:
    """Replay the append-only ledger: last record per milestone wins. Earlier records are
    never removed — this is a projection, not a compaction."""
    out: dict[str, dict] = {}
    for rec in read_ledger(state_dir):
        out[rec["milestone"]] = rec
    return out


def tombstoned_ids(state_dir) -> set[str]:
    return {mid for mid, rec in current_states(state_dir).items()
            if rec.get("state") == STATE_TOMBSTONED}


def mark_dead(state_dir, mid: str, observation: dict, actor: str, note: str | None = None,
              now: str | None = None) -> dict:
    """Tombstone one registry entry. Refuses anything that is not a dated DEAD observation
    of this very milestone, and re-stat()s the path at write time so a stale observation
    cannot bury an entry that came back."""
    if not actor:
        raise ValueError("mark_dead requires an actor — a tombstone is an attributed act")
    if not isinstance(observation, dict):
        raise ValueError("mark_dead requires an observation dict from observe_entry()")
    if observation.get("milestone") != mid:
        raise ValueError(f"observation is for {observation.get('milestone')!r}, not {mid!r}")
    if not observation.get("checked_at"):
        raise ValueError("observation carries no checked_at — refusing an undated marking")
    if observation.get("liveness") != DEAD:
        raise ValueError(
            f"refusing to tombstone {mid!r} on a supposition: observed liveness is "
            f"{observation.get('liveness')!r}, not {DEAD!r}")
    if observation.get("reason") not in DEAD_REASONS:
        raise ValueError(f"unknown death reason {observation.get('reason')!r}")
    row = next((r for r in rows(state_dir) if r.get("id") == mid), None)
    if row is None:
        raise KeyError(f"{mid} is not in this registry")
    # Re-observe now: the observation justifies the marking, but the marking must still be
    # true at the instant it is written.
    fresh = observe_entry(row, state_dir, now=now)
    if fresh["liveness"] != DEAD:
        raise ValueError(
            f"refusing to tombstone {mid!r}: its path is {fresh['liveness']} at write time "
            f"({fresh['worktree']}) — the observation is stale")
    if mid in tombstoned_ids(state_dir):
        raise ValueError(f"{mid} is already tombstoned — a tombstone is written once")
    classification = classify_death(state_dir, fresh)
    body = {
        "schema": SCHEMA,
        "state": STATE_TOMBSTONED,
        "milestone": mid,
        "worktree": observation.get("worktree"),
        "reason": fresh["reason"],
        "death_kind": classification["kind"],
        "death_kind_reason": classification["reason"],
        "death_kind_evidence": classification["evidence"],
        "observed_at": observation["checked_at"],
        "recorded_at": now or utc(),
        "actor": actor,
        "note": note,
        "observation": fresh,
        "last_known_proof": last_known_proof(state_dir, mid, row),
    }
    return _append(state_dir, body)


def revive(state_dir, mid: str, actor: str, note: str | None = None,
           now: str | None = None) -> dict:
    """Un-tombstone an entry whose path is back. Appends; never deletes the TOMBSTONED
    record. The history of the death stays readable forever."""
    if not actor:
        raise ValueError("revive requires an actor")
    if mid not in tombstoned_ids(state_dir):
        raise ValueError(f"{mid} is not currently tombstoned")
    row = next((r for r in rows(state_dir) if r.get("id") == mid), None)
    if row is None:
        raise KeyError(f"{mid} is not in this registry")
    fresh = observe_entry(row, state_dir, now=now)
    if fresh["liveness"] != LIVE:
        raise ValueError(
            f"refusing to revive {mid!r}: observed liveness is {fresh['liveness']!r}, "
            f"not {LIVE!r} — a revival is an observation too")
    body = {
        "schema": SCHEMA,
        "state": STATE_REVIVED,
        "milestone": mid,
        "worktree": fresh.get("worktree"),
        "reason": None,
        "observed_at": fresh["checked_at"],
        "recorded_at": now or utc(),
        "actor": actor,
        "note": note,
        "observation": fresh,
        "last_known_proof": last_known_proof(state_dir, mid, row),
    }
    return _append(state_dir, body)


def mark_all_dead(state_dir, actor: str, note: str | None = None,
                  now: str | None = None, kind: str | None = None) -> list[dict]:
    """Scan, then tombstone every entry measured dead and not already tombstoned.

    `kind` restricts the pass to one death kind (KIND_ARCHIVED_RUN or KIND_LOST_WORKTREE): only
    entries the classifier places in that kind are tombstoned. This is how B-03 separates archived
    runs from lost worktrees — the two populations are marked in different passes and never
    conflated. `kind=None` marks every dead entry, the original FE behaviour.
    """
    already = tombstoned_ids(state_dir)
    written = []
    for obs in scan(state_dir, now=now):
        if obs["liveness"] != DEAD or obs["milestone"] in already:
            continue
        if kind is not None and classify_death(state_dir, obs)["kind"] != kind:
            continue
        written.append(mark_dead(state_dir, obs["milestone"], obs, actor, note=note, now=now))
    return written


def _campaign_run_state_dirs(campaign_root) -> list[pathlib.Path]:
    """Every campaign-run state dir under `campaign_root`: .../<phase>-runs/run-<N>/state.
    A directory-shape test, never a guess — the classifier re-checks completion before marking."""
    root = pathlib.Path(campaign_root)
    out = []
    if not root.is_dir():
        return out
    for phase in sorted(root.glob("*-runs")):
        if not phase.is_dir():
            continue
        for run in sorted(phase.glob("run-*")):
            state = run / "state"
            if (state / "roadmap.jsonl").is_file():
                out.append(state)
    return out


def mark_archived_runs(campaign_root, actor: str, note: str | None = None,
                       now: str | None = None) -> list[dict]:
    """B-03 marking pass over a campaign tree: tombstone ONLY the entries the classifier places in
    ARCHIVED_RUN. A dead entry with no completion record is left alone (it stays in
    `dead_not_tombstoned`, the honesty field) — never silently buried as 'archived' on a guess."""
    written = []
    for state in _campaign_run_state_dirs(campaign_root):
        written.extend(
            mark_all_dead(state, actor, note=note, now=now, kind=KIND_ARCHIVED_RUN))
    return written


# ---------------------------------------------------------------------------
# 4. PROJECTIONS — denominator excludes tombstoned, display never does.
# ---------------------------------------------------------------------------

def metrics(state_dir, now: str | None = None) -> dict:
    """Registry metrics with tombstoned entries out of the denominator and counted
    separately. `dead_not_tombstoned` is the honesty field: entries measured dead right now
    that nobody has marked, which stay IN the denominator until somebody records the
    observation. Nothing is silently dropped."""
    all_rows = rows(state_dir)
    tombs = tombstoned_ids(state_dir)
    live_rows = [r for r in all_rows if r.get("id") not in tombs]
    tomb_rows = [r for r in all_rows if r.get("id") in tombs]

    def by_status(rs):
        out: dict[str, int] = {}
        for r in rs:
            out[r.get("status", "pending")] = out.get(r.get("status", "pending"), 0) + 1
        return out

    observations = scan(state_dir, now=now)
    dead_now = {o["milestone"] for o in observations if o["liveness"] == DEAD}

    # Death-kind breakdown of tombstoned entries. A tombstone written before B-03 carries no
    # stored kind (the job-radar six); it is counted as UNCLASSIFIED — the honest answer, since the
    # classification did not exist when it was written. Never re-guessed after the fact.
    states = current_states(state_dir)
    tomb_by_kind: dict[str, int] = {}
    for mid, rec in states.items():
        if rec.get("state") == STATE_TOMBSTONED:
            k = rec.get("death_kind") or KIND_UNCLASSIFIED
            tomb_by_kind[k] = tomb_by_kind.get(k, 0) + 1

    # Of the entries dead right now but not yet marked, how many would classify as archived vs
    # lost — the B-03 question, answered before anyone marks, so the two populations are never
    # conflated by a blanket `mark --all-dead`.
    dead_unmarked = dead_now - tombs
    dead_unmarked_by_kind: dict[str, list[str]] = {KIND_ARCHIVED_RUN: [], KIND_LOST_WORKTREE: []}
    for o in observations:
        if o["milestone"] in dead_unmarked:
            dead_unmarked_by_kind[classify_death(state_dir, o)["kind"]].append(o["milestone"])

    return {
        "schema": SCHEMA,
        "state_dir": str(pathlib.Path(state_dir)),
        "computed_at": now or utc(),
        "total_entries": len(all_rows),
        "denominator": len(live_rows),
        "live_entries": len(live_rows),
        "tombstoned_entries": len(tomb_rows),
        "live_by_status": by_status(live_rows),
        "tombstoned_by_status": by_status(tomb_rows),
        "tombstoned_by_kind": tomb_by_kind,
        "tombstoned_ids": sorted(tombs),
        "dead_not_tombstoned": sorted(dead_now - tombs),
        "dead_not_tombstoned_by_kind": {k: sorted(v) for k, v in dead_unmarked_by_kind.items()},
    }


def report(state_dir, now: str | None = None) -> str:
    """Human-readable projection. Every entry is printed, including tombstoned ones — they
    are marked, never hidden."""
    m = metrics(state_dir, now=now)
    states = current_states(state_dir)
    lines = [f"registry: {m['state_dir']}",
             f"entries: {m['total_entries']}  live(denominator): {m['denominator']}  "
             f"tombstoned: {m['tombstoned_entries']}"]
    for r in rows(state_dir):
        mid = r.get("id")
        rec = states.get(mid)
        if rec is not None and rec.get("state") == STATE_TOMBSTONED:
            kind = rec.get("death_kind") or KIND_UNCLASSIFIED
            kind_tag = "" if kind == KIND_UNCLASSIFIED else f" <{kind}>"
            lines.append(
                f"{mid:8} {r.get('status','pending'):16} [TOMBSTONED {rec['reason']}{kind_tag} "
                f"observed_at={rec['observed_at']} by={rec['actor']}] {r.get('title','')}")
        else:
            lines.append(f"{mid:8} {r.get('status','pending'):16} [live] {r.get('title','')}")
    if m["dead_not_tombstoned"]:
        parts = []
        for k in (KIND_ARCHIVED_RUN, KIND_LOST_WORKTREE):
            ids = m["dead_not_tombstoned_by_kind"].get(k, [])
            if ids:
                parts.append(f"{k}: {', '.join(ids)}")
        kinds = (" (" + "; ".join(parts) + ")") if parts else ""
        lines.append("MEASURED DEAD BUT NOT TOMBSTONED (still in denominator)"
                     + kinds)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("command",
                   choices=["scan", "mark", "mark-archived-runs", "classify",
                            "revive", "report", "metrics", "verify"])
    p.add_argument("--state-dir", required=False)
    p.add_argument("--campaign-root", required=False,
                   help="for mark-archived-runs: the campaign tree to walk")
    p.add_argument("--milestone")
    p.add_argument("--all-dead", action="store_true")
    p.add_argument("--kind", choices=sorted(DEATH_KINDS),
                   help="restrict mark --all-dead to one death kind (B-03)")
    p.add_argument("--actor")
    p.add_argument("--note")
    a = p.parse_args(argv)

    if a.command == "mark-archived-runs":
        if not a.actor:
            raise SystemExit("mark-archived-runs requires --actor")
        if not a.campaign_root:
            raise SystemExit("mark-archived-runs requires --campaign-root")
        written = mark_archived_runs(a.campaign_root, a.actor, note=a.note)
        print(json.dumps(written, indent=2, sort_keys=True))
        return 0

    if not a.state_dir:
        raise SystemExit("this command requires --state-dir")
    sd = pathlib.Path(a.state_dir)

    if a.command == "scan":
        print(json.dumps(scan(sd), indent=2, sort_keys=True))
    elif a.command == "metrics":
        print(json.dumps(metrics(sd), indent=2, sort_keys=True))
    elif a.command == "report":
        print(report(sd))
    elif a.command == "verify":
        verify_ledger(sd)
        print("OK")
    elif a.command == "classify":
        out = []
        for o in scan(sd):
            if o["liveness"] == DEAD:
                out.append({**classify_death(sd, o), "milestone": o["milestone"]})
        print(json.dumps(out, indent=2, sort_keys=True))
    elif a.command == "mark":
        if not a.actor:
            raise SystemExit("mark requires --actor")
        if a.all_dead:
            written = mark_all_dead(sd, a.actor, note=a.note, kind=a.kind)
        elif a.milestone:
            row = next((r for r in rows(sd) if r.get("id") == a.milestone), None)
            if row is None:
                raise SystemExit(f"{a.milestone} is not in this registry")
            written = [mark_dead(sd, a.milestone, observe_entry(row, sd), a.actor, note=a.note)]
        else:
            raise SystemExit("mark requires --milestone or --all-dead")
        print(json.dumps(written, indent=2, sort_keys=True))
    elif a.command == "revive":
        if not a.actor or not a.milestone:
            raise SystemExit("revive requires --milestone and --actor")
        print(json.dumps(revive(sd, a.milestone, a.actor, note=a.note), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
