"""Supervisor-side observation of a product collection run.

F-1 (zcode FORGERY_MATRIX V1): `verify_tr_data_backfill` recomputes everything it checks
from `data/corpus/<handle>/posts.jsonl` — and that file is written by the worker. So a
worker that writes forty well-formed *invented* posts, a matching report and a correct
owner lineage passes the gate at rc=0. Recomputation removed the arithmetic lie; it could
not remove the provenance lie, because every input it recomputes from is worker-writable.

    Recomputing from an artifact the claimant controls proves the artifact is
    self-consistent. It cannot prove the artifact came from the world.

Provenance has to be established by something the claimant does not run. This module is
that something: the supervisor launches the collection itself, records what it observed
arriving during a session it owns, and writes the record into a directory outside the
product repo. The verifier then requires every post on disk to be covered by an
observation — a post nobody watched arrive is unprovenanced, no matter how well-formed.

What this does and does not prove — stated plainly, because overclaiming here would be
the same mistake in a new place:

  * It proves: these post IDs passed through a collection process the supervisor started,
    at a recorded time, under a session identity the mission cannot author, and the
    corpus on disk contains nothing beyond what was observed.
  * It does NOT prove: that the remote end was genuinely X rather than a stub the product
    was pointed at. Binding to the real provider needs credentialed re-fetch by the
    supervisor at verification time, which is a network capability this gate does not
    have. That residual boundary is real and is recorded in the record itself as
    `provider_attested: false`.

Mission code must never write into the observation directory; that is enforced by the
directory living outside the product repo, and checked by the verifier.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import time
import uuid
from pathlib import Path
from factory_v2._io import _now

SCHEMA = "collection-observation/v1"
REGISTRY_SCHEMA = "collection-session-registry/v1"

# The collection child announces each post it retrieves on this channel. The supervisor
# owns the file: the child appends events as it works, the child never names the path.
_EVENT_ENV = "FACTORY_COLLECTION_OBSERVER_PATH"
_START_GATE_ENV = "FACTORY_COLLECTION_START_GATE"

# Sol r2 @aee8f652 (TR-DATA-OBSERVATION-FORGE): a correctly-shaped JSON in the
# observation directory used to credit its IDs regardless of who wrote it.  Every
# observation must now be bound to a supervisor-REGISTERED session: observe_collection
# appends (session_id, record_sha256, events_sha256, command, cwd) to a registry the
# product mission cannot write (control-repo receipts by default; env override is for
# isolated test fixtures).  Validation requires the record hash to match its registry
# entry AND the accounts to be exactly recomputable from the retained supervisor event
# stream.  Residual boundary, stated plainly: a process that can write the control-repo
# registry can still forge — that boundary is identical to every other supervisor-owned
# evidence path and is NOT provider attestation (provider_attested stays honest).
_REGISTRY_ENV = "FACTORY_COLLECTION_SESSION_REGISTRY"
# FACTORY_STATE_OUT_OF_REPO_V1: collection_sessions.jsonl is append-only runtime
# state, not repo state — default under the daemon run-dir's journals/ (same root
# as --run-dir/FACTORY_RUN_DIR), never inside the git repo.
_REGISTRY_DEFAULT = (
    Path(os.environ.get("FACTORY_RUN_DIR", "").strip()
         or str(Path.home() / "Library/Application Support/JocelynFactory/run"))
    / "journals" / "collection_sessions.jsonl"
)


def _default_registry_path() -> Path:
    env = os.environ.get(_REGISTRY_ENV)
    return Path(env) if env else _REGISTRY_DEFAULT


def _record_sha256(rec: dict) -> str:
    return hashlib.sha256(
        json.dumps(rec, sort_keys=True).encode("utf-8")).hexdigest()


def observe_collection(
    *,
    command: str,
    cwd: Path,
    observation_dir: Path,
    session_id: str = "",
    timeout_s: float = 3600.0,
    provider_attested: bool = False,
) -> Path:
    """Run a collection command under supervision and record what was observed.

    Returns the path of the observation record written.
    """
    from .mission_evidence import get_process_birth_time

    cwd = Path(cwd)
    observation_dir = Path(observation_dir)
    observation_dir.mkdir(parents=True, exist_ok=True)
    session_id = session_id or uuid.uuid4().hex[:16]

    events_path = observation_dir / f".events_{session_id}.jsonl"
    events_path.write_text("", encoding="utf-8")

    env = dict(os.environ)
    env[_EVENT_ENV] = str(events_path)
    start_gate = observation_dir / f".start_{session_id}"
    start_gate.unlink(missing_ok=True)
    env[_START_GATE_ENV] = str(start_gate)

    started = _now()
    # Hold the child alive until its birth identity has been captured.  A short
    # collection command can otherwise exit between Popen() and the metadata
    # lookup (especially when sandboxed macOS must fall back from ps to
    # libproc), leaving an otherwise genuine observation unverifiable.
    proc = subprocess.Popen(
        ["bash", "-c",
         'while [ ! -e "$FACTORY_COLLECTION_START_GATE" ]; do sleep 0.01; done; '
         'exec bash -c "$1"',
         "collection-observer", command],
        cwd=str(cwd), env=env, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        birth = get_process_birth_time(proc.pid)
    finally:
        start_gate.touch()
    try:
        proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        # L5_factory_v2_rest_1-012: kill the WHOLE session (bash + every child
        # still holding the stdout/stderr pipes), then bound the final drain —
        # an immortal descendant must never block the observation record.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            for stream in (proc.stdout, proc.stderr):
                try:
                    stream.close()
                except Exception:
                    pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
    finally:
        start_gate.unlink(missing_ok=True)
    finished = _now()

    accounts: dict = {}
    for raw in events_path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            ev = json.loads(raw)
        except Exception:
            continue
        h = str(ev.get("handle", ""))
        pid_ = str(ev.get("post_id", ""))
        if not h or not pid_:
            continue
        entry = accounts.setdefault(h, {"observed_post_ids": [], "transport": ev.get("transport", "")})
        entry["observed_post_ids"].append(pid_)

    events_bytes = events_path.read_bytes()
    record = {
        "schema": SCHEMA,
        "session_id": session_id,
        "observer_pid": os.getpid(),
        "collection_pid": proc.pid,
        "collection_birth_time": birth,
        "started_at": started,
        "finished_at": finished,
        "command": command,
        "cwd": str(cwd.resolve()),
        "exit_code": proc.returncode,
        # Honest about the boundary this harness does not cross.
        "provider_attested": bool(provider_attested),
        # Supervisor event-stream binding (Sol r2): the retained events file and
        # its hash — validation recomputes accounts from it byte-exactly.
        "events_file": events_path.name,
        "events_sha256": hashlib.sha256(events_bytes).hexdigest(),
        "accounts": accounts,
    }
    out = observation_dir / f"observation_{session_id}.json"
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
    os.replace(tmp, out)
    register_session(record, registry_path=_default_registry_path())
    return out


def register_session(record: dict, *, registry_path: "Path | None" = None) -> None:
    """Append the supervisor-owned session registration for one observation.

    Binds (session_id -> record_sha256, events_sha256, command, cwd) in a
    registry the observed process never writes.  Called by observe_collection
    after the record is finalized; fsync'd so a crash cannot silently lose the
    binding."""
    registry_path = Path(registry_path) if registry_path else _default_registry_path()
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "schema": REGISTRY_SCHEMA,
        "session_id": record.get("session_id", ""),
        "record_sha256": _record_sha256(record),
        "events_sha256": record.get("events_sha256", ""),
        "command": str(record.get("command", "")),
        "cwd": str(record.get("cwd", "")),
        "observer_pid": record.get("observer_pid"),
        "registered_at": _now(),
    }
    with open(registry_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())


def _accounts_from_events_text(text: str) -> dict:
    """Deterministic recompute of accounts from the supervisor event stream.

    Must accumulate exactly like observe_collection — a record whose accounts
    disagree with its own event stream is forged or altered, never credited."""
    accounts: dict = {}
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            ev = json.loads(raw)
        except Exception:
            continue
        h = str(ev.get("handle", ""))
        pid_ = str(ev.get("post_id", ""))
        if not h or not pid_:
            continue
        entry = accounts.setdefault(h, {"observed_post_ids": [], "transport": ev.get("transport", "")})
        entry["observed_post_ids"].append(pid_)
    return accounts


def _load_registry(registry_path: Path) -> dict:
    """session_id -> list of registry entries (missing registry = no bindings)."""
    registry_path = Path(registry_path)
    entries: dict = {}
    if not registry_path.is_file():
        return entries
    for line in registry_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        sid = str(e.get("session_id", ""))
        if sid:
            entries.setdefault(sid, []).append(e)
    return entries


def _session_binding_rejections(rec: dict, observation_dir: Path,
                                registry: dict) -> list[str]:
    """Why this record is not bound to a supervisor-registered session."""
    problems = []
    sid = str(rec.get("session_id", ""))
    events_name = str(rec.get("events_file", ""))
    events_sha = str(rec.get("events_sha256", ""))
    if not events_name or not events_sha:
        problems.append("no supervisor event-stream binding (events_file/"
                        "events_sha256 missing) — record was not produced by "
                        "observe_collection")
        return problems
    expected_name = f".events_{sid}.jsonl"
    if events_name != expected_name:
        problems.append(f"events_file {events_name!r} != expected {expected_name!r}")
        return problems
    events_path = Path(observation_dir) / events_name
    if not events_path.is_file():
        problems.append("supervisor event stream missing — retained events file "
                        "not found next to the record")
        return problems
    digest = hashlib.sha256(events_path.read_bytes()).hexdigest()
    if digest != events_sha:
        problems.append("event stream altered after observation (sha mismatch)")
        return problems
    if rec.get("accounts") != _accounts_from_events_text(
            events_path.read_text(encoding="utf-8")):
        problems.append("accounts not reproducible from the observed event stream")
    entries = registry.get(sid, [])
    rec_sha = _record_sha256(rec)
    if not any(e.get("record_sha256") == rec_sha for e in entries):
        problems.append("record hash not registered by any supervisor session — "
                        "forged or altered after registration")
        return problems
    for e in entries:
        if e.get("record_sha256") == rec_sha:
            if str(e.get("command", "")) != str(rec.get("command", "")) or \
                    str(e.get("cwd", "")) != str(rec.get("cwd", "")):
                problems.append("command/cwd disagree with the registered session")
            break
    return problems


def _observation_rejections(rec: dict) -> list[str]:
    """Why this observation record cannot be trusted as provenance evidence.

    Sol R8 @d0bff136: a correctly-shaped JSON used to credit its IDs regardless
    of exit_code, identity binding or timestamps — a forged file with
    exit_code!=0 and invented IDs passed.  A record now has to look like a REAL
    supervisor observation of a collection that succeeded: identity fields
    present, typed provider_attested flag, ordered timestamps, non-empty
    command/cwd.  (provider_attested=false stays HONEST and acceptable — it
    records that the remote end was not attested; what is rejected is a record
    that could not have come from a completed observed run.)
    """
    problems = []
    if rec.get("exit_code") != 0:
        problems.append(f"exit_code={rec.get('exit_code')!r} — a failed "
                        "collection run observed nothing creditable")
    for field in ("observer_pid", "collection_pid", "collection_birth_time"):
        if not rec.get(field):
            problems.append(f"missing {field} — no birth-identity binding to a "
                            "supervisor-started process")
    if not isinstance(rec.get("provider_attested"), bool):
        problems.append("provider_attested missing or not boolean — the honesty "
                        "flag is mandatory")
    for field in ("started_at", "finished_at", "command", "cwd"):
        if not str(rec.get(field) or "").strip():
            problems.append(f"missing {field}")
    started, finished = str(rec.get("started_at") or ""), str(rec.get("finished_at") or "")
    if started and finished:
        try:
            from datetime import datetime
            s = datetime.fromisoformat(started.replace("Z", "+00:00"))
            f_ = datetime.fromisoformat(finished.replace("Z", "+00:00"))
            if f_ < s:
                problems.append("finished_at precedes started_at")
        except ValueError:
            problems.append("started_at/finished_at not ISO-8601 parseable")
    if not isinstance(rec.get("accounts"), dict):
        problems.append("accounts is not an object")
    return problems


def load_observations_validated(observation_dir: Path,
                                *, registry_path: "Path | None" = None) -> tuple[dict, list]:
    """handle -> credited post ids, plus per-file rejection reasons.

    Only records that pass _observation_rejections AND the supervisor session
    binding (registry + retained event stream) credit their IDs.  Rejected
    files are reported (file, reasons) so verifiers can surface them instead of
    silently ignoring evidence that failed validation."""
    observation_dir = Path(observation_dir)
    registry = _load_registry(registry_path if registry_path is not None
                              else _default_registry_path())
    seen: dict = {}
    rejected: list = []
    if not observation_dir.is_dir():
        return seen, rejected
    for f in sorted(observation_dir.glob("observation_*.json")):
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except Exception as exc:
            rejected.append({"file": f.name, "reasons": [f"unreadable: {exc}"]})
            continue
        if rec.get("schema") != SCHEMA:
            rejected.append({"file": f.name, "reasons": [f"schema {rec.get('schema')!r} != {SCHEMA}"]})
            continue
        problems = _observation_rejections(rec)
        if not problems:
            problems = _session_binding_rejections(rec, observation_dir, registry)
        if problems:
            rejected.append({"file": f.name, "reasons": problems})
            continue
        for handle, entry in (rec.get("accounts") or {}).items():
            ids = entry.get("observed_post_ids") or []
            seen.setdefault(str(handle), set()).update(str(i) for i in ids)
    return seen, rejected


def load_observations(observation_dir: Path) -> dict:
    """handle -> set of post ids observed arriving (validated records only)."""
    seen, _rejected = load_observations_validated(observation_dir)
    return seen


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--command", required=True)
    ap.add_argument("--cwd", required=True)
    ap.add_argument("--observation-dir", required=True)
    ap.add_argument("--timeout-s", type=float, default=3600.0)
    args = ap.parse_args(argv)
    out = observe_collection(command=args.command, cwd=Path(args.cwd),
                             observation_dir=Path(args.observation_dir),
                             timeout_s=args.timeout_s)
    print(f"observation written: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
