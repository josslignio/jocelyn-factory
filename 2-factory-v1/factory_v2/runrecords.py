"""RunRecord persistence — append-only JSONL, zero external dependencies.

Public API:
  record_attempt(...)   — called after every mission execution (PASS and FAIL).
  record_settlement(...)— called after IndependentValidator confirms milestone COMPLETE.

Both produce a trajectory entry. Only record_settlement produces validated=True evidence
that adaptive routing (F7) may learn from as a positive signal.
"""
import contextlib
import fcntl
import json
import math
import os
import pathlib
import time
from typing import Optional
from factory_v2._io import _file_lock, _now

# FACTORY_STATE_OUT_OF_REPO_V1: runrecords.jsonl is append-only runtime state, not
# repo state — it lives under the daemon run-dir's journals/ (same root the launchd
# daemon already gets via --run-dir / FACTORY_RUN_DIR), never inside the git repo.
_DEFAULT_RUNRECORDS_PATH = (
    pathlib.Path(os.environ.get("FACTORY_RUN_DIR", "").strip()
                 or str(pathlib.Path.home() / "Library/Application Support/JocelynFactory/run"))
    / "journals" / "runrecords.jsonl"
)


def _runrecords_path() -> pathlib.Path:
    """Dynamic path lookup — FACTORY_RUNRECORDS_PATH env var lets tests redirect writes.

    Called per operation so that tests setting the env var before import still work.
    Production code that never sets the env var always gets the stable default path
    under the run-dir's journals/ (see module docstring).
    """
    env = os.environ.get("FACTORY_RUNRECORDS_PATH")
    return pathlib.Path(env) if env else _DEFAULT_RUNRECORDS_PATH


# Stable public alias for code that imports RUNRECORDS_PATH directly (backwards compat).
RUNRECORDS_PATH = _DEFAULT_RUNRECORDS_PATH


@contextlib.contextmanager
def _ledger_lock():
    """Cross-process exclusive lock serializing ledger read->decide->append.

    D029 B1: reconcile_orphan_predictions() reads the ledger, decides which
    PREDICTIONs are orphans, then appends OBSERVATIONs.  Without serialization,
    N concurrent reconcilers all observe "no OBSERVATION" and all append
    (Sol's 40-process probe measured observation_count=2 for
    prediction_count=1).  Same discipline as project_transaction / single_flight:
    fcntl.flock on a sibling lock file — the kernel releases it when the owner
    dies, so a crashed reconciler cannot deadlock the ledger.
    """
    p = _runrecords_path()
    with _file_lock(p.parent / (p.name + ".lock")):
        yield

REQUIRED_FIELDS = {
    "project", "milestone", "mission", "task_type", "engine", "strategy",
    "authority_hashes", "context_hash", "candidate_sha", "attempts",
    "failure_fingerprints", "tests", "product_e2e", "validator_verdict",
    "wall_time_s", "human_interventions", "validated",
}

VALID_ENGINES = {"claude", "glm", "codex", "zcode", "local"}


def _write(record: dict) -> None:
    missing = REQUIRED_FIELDS - set(record.keys())
    if missing:
        raise ValueError(f"RunRecord missing fields: {missing}")
    if record.get("engine") not in VALID_ENGINES:
        raise ValueError(f"engine must be one of {VALID_ENGINES}, got: {record.get('engine')!r}")
    record = dict(record)
    record["recorded_at"] = _now()
    p = _runrecords_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def record_attempt(record: dict) -> None:
    """Record a mission execution attempt (PASS or FAIL). validated=False always.
    Called after every runner.execute() — both successful and failed attempts must
    produce evidence for F7 adaptive routing (negative signal is as valuable as positive).
    """
    r = dict(record)
    r["validated"] = False
    _write(r)


def record_settlement(record: dict) -> None:
    """Record a validated milestone settlement. validated=True — IndependentValidator
    confirmed COMPLETE. Only these records are used as positive learning signal in F7.
    """
    r = dict(record)
    r["validated"] = True
    _write(r)


def append_run(record: dict) -> None:
    """Compat shim — prefer record_attempt() or record_settlement() instead."""
    _write(record)


def read_runs(project: Optional[str] = None, validated_only: bool = False) -> list[dict]:
    p = _runrecords_path()
    if not p.exists():
        return []
    runs = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                # L5_rest_2-007: an interrupted append leaves ONE truncated
                # tail line; the phase readers of this module already tolerate
                # it — dropping every earlier valid row here made all engine
                # stats unavailable after a single crashed append.
                continue
            if not isinstance(r, dict):
                continue
            if project and r.get("project") != project:
                continue
            if validated_only and not r.get("validated"):
                continue
            runs.append(r)
    return runs


def record_prediction(attempt_id: str, prediction: dict) -> None:
    """G5 (Order 024 / RUN_RECORDS_MINIMUM_WIRED): two-phase RunRecords.

    Phase PREDICTION is written BEFORE dispatch — predicted outcome, duration,
    cost and failure modes. Phase OBSERVATION (record_observation) closes the
    attempt with what actually happened. The pair reconciles 1:1 on attempt_id;
    a crash between the phases is closed by reconcile_orphan_predictions().
    """
    rec = {
        "phase": "PREDICTION",
        "attempt_id": attempt_id,
        "predicted_at": _now(),
        "prediction": dict(prediction),
    }
    p = _runrecords_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with _ledger_lock():
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")


def record_observation(attempt_id: str, observation: dict) -> None:
    """Close a PREDICTION with the observed outcome (phase OBSERVATION)."""
    rec = {
        "phase": "OBSERVATION",
        "attempt_id": attempt_id,
        "observed_at": _now(),
        "observation": dict(observation),
    }
    p = _runrecords_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with _ledger_lock():
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")


def _worker_still_alive(prediction: dict) -> bool:
    """L5_rest_2-008: a PREDICTION that names its worker_pid is only closable
    once that PID is PROVEN dead — os.kill(pid, 0) is the cheap proof. A live
    worker means the attempt is still running in another mission's slot; a
    premature WORKER_DEATH closure then produced a second closure when the
    real observation landed (breaking the 1:1 contract)."""
    try:
        pid = int((prediction or {}).get("worker_pid") or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True                      # alive, just not ours to signal
    except OSError:
        return False


def reconcile_orphan_predictions() -> list[dict]:
    """Close PREDICTIONs with no OBSERVATION as WORKER_DEATH (exactly once).

    A crash between dispatch and completion must not leave the learning ledger
    silent: the orphan is closed with close_reason=WORKER_DEATH so prediction
    cost is counted while the outcome never becomes a false positive signal.
    Idempotent: already-closed attempts are never rewritten.

    L5_rest_2-008: this runs at the START of every advance cycle while other
    missions are legitimately in flight — a PREDICTION whose recorded worker
    PID is still alive is NOT an orphan and must not be closed here.
    """
    p = _runrecords_path()
    if not p.exists():
        return []

    def _pending(lines: list[str]) -> tuple[dict, set]:
        preds = {}
        closed = set()
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(r, dict):
                continue
            if r.get("phase") == "PREDICTION":
                preds[r["attempt_id"]] = r
            elif r.get("phase") == "OBSERVATION":
                closed.add(r["attempt_id"])
        return preds, closed

    # A fully reconciled (or still-live) ledger needs no mutation, so do not
    # require write access merely to prove that there is nothing to append.
    # If a candidate exists we scan again under the lock before writing; the
    # locked pass remains the authority and preserves exactly-once closure.
    initial_lines = p.read_text(encoding="utf-8").splitlines()
    initial_preds, initial_closed = _pending(initial_lines)
    if not any(aid not in initial_closed
               and not _worker_still_alive(rec.get("prediction") or {})
               for aid, rec in initial_preds.items()):
        return []
    # B1: the whole read -> decide -> append sequence is serialized under the
    # cross-process ledger lock; concurrent reconcilers then observe each
    # other's OBSERVATIONs and exactly one closure per orphan survives.
    with _ledger_lock():
        lines = p.read_text(encoding="utf-8").splitlines()
        preds, closed = _pending(lines)
        orphans = []
        now = _now()
        for aid, rec in preds.items():
            if aid in closed:
                continue
            if _worker_still_alive(rec.get("prediction") or {}):
                continue                # live worker: not a proven death
            orphan = {
                "phase": "OBSERVATION",
                "attempt_id": aid,
                "observed_at": now,
                "reconciled": True,
                "observation": {"close_reason": "WORKER_DEATH",
                                "note": "closed by reconcile_orphan_predictions"},
            }
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps(orphan) + "\n")
            orphans.append(orphan)
        return orphans


def read_attempt_phases() -> list[dict]:
    """All phase records (PREDICTION/OBSERVATION) for reconciliation checks.

    Non-phase records (record_attempt/record_settlement entries) are excluded —
    this reader exists for two-phase reconciliation, not the full ledger.
    """
    p = _runrecords_path()
    if not p.exists():
        return []
    out = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("phase") in ("PREDICTION", "OBSERVATION"):
                out.append(r)
    return out


def _measured_cost(record: dict) -> float | None:
    """wall_time_s as a real measurement, or None when it is unknown/invalid.

    NaN, inf and negative durations are not measurements. They are recorded as unknown
    rather than silently averaged in as numbers.
    """
    wall = record.get("wall_time_s")
    if isinstance(wall, bool) or not isinstance(wall, (int, float)):
        return None
    value = float(wall)
    if not math.isfinite(value) or value < 0:
        return None
    return value


def _attempt_failed(record: dict) -> bool | None:
    """True/False when the attempt's outcome is OBSERVED, None when it is genuinely pending.

    The live producer writes validator_verdict=None on PASS and on FAIL, so the verdict field
    alone cannot classify anything. The observed outcome is used first (attempt_ok /
    attempt_rc / attempt_outcome, persisted by runtime.py), and the verdict only as a
    fallback for older records.
    """
    if isinstance(record.get("attempt_ok"), bool):
        return not record["attempt_ok"]
    rc = record.get("attempt_rc")
    if isinstance(rc, int) and not isinstance(rc, bool):
        return rc != 0
    outcome = record.get("attempt_outcome")
    if str(outcome or "").upper() in ("PENDING", "UNKNOWN", "IN_FLIGHT"):
        return None
    if isinstance(outcome, str) and outcome:
        return outcome.upper() not in ("PASS", "PASSED", "OK", "ACCEPTED", "COMPLETE")
    verdict = record.get("validator_verdict")
    if verdict in ("ACCEPTED", "COMPLETE"):
        return False
    if verdict in ("PENDING", "UNKNOWN", "IN_FLIGHT"):
        return None
    if isinstance(verdict, str) and verdict:
        return True
    return None


def engine_stats(project: Optional[str] = None) -> dict:
    """Count observed attempts, and credit only the attempts independently settled.

    SHA is not attempt identity. Legacy rows without an id remain separate: identical
    outcomes cannot distinguish duplicated logging from genuine retries. Two-phase rows
    fill missing attempts after crashes; they never duplicate a normal attempt record.
    ``accepted`` counts validated attempts, ``validated_deliveries`` distinct contracts.
    Unknown durations stay None, with a separate count of missing observations.
    """
    rows = read_runs()
    attempts = {}
    phases = {}
    settlements = []

    def project_of(record):
        return record.get("project") or (record.get("authority_hashes") or {}).get("project")

    for index, record in enumerate(rows):
        phase = record.get("phase")
        if phase in ("PREDICTION", "OBSERVATION"):
            aid = record.get("attempt_id")
            if aid:
                phases.setdefault(str(aid), {})[phase] = record
            continue
        if record.get("task_type") == "milestone_settlement":
            settlements.append(record)
            continue
        rp = project_of(record)
        if project is not None and rp != project:
            continue
        aid = record.get("attempt_id")
        key = (rp, str(aid)) if aid else ("legacy-row", index)
        if key not in attempts:
            attempts[key] = dict(record, project=rp)

    # New predictions carry project explicitly. Legacy observations carry authority hashes;
    # a legacy prediction with neither cannot be attributed to a specific project/engine.
    known_ids = {(r.get("project"), str(r["attempt_id"])) for r in attempts.values()
                 if r.get("attempt_id")}
    for aid, pair in phases.items():
        pred = (pair.get("PREDICTION") or {}).get("prediction") or {}
        obs = (pair.get("OBSERVATION") or {}).get("observation") or {}
        rp = project_of(obs) or project_of(pred)
        if project is not None and rp != project:
            continue
        if (rp, aid) in known_ids:
            continue
        # Older prediction records lack project, but an ordinary record may still identify
        # them. Never add an unknown-project duplicate of that same durable attempt.
        if rp is None and any(key[1] == aid for key in known_ids):
            continue
        record = {"project": rp, "attempt_id": aid,
                  "milestone": obs.get("milestone") or pred.get("milestone"),
                  "context_hash": obs.get("contract_id") or pred.get("contract_id"),
                  "engine": obs.get("engine") or "unknown",
                  "wall_time_s": obs.get("duration_s"),
                  "attempt_outcome": obs.get("outcome"),
                  "attempt_rc": obs.get("rc"), "validated": False}
        if not obs:
            record["attempt_outcome"] = "PENDING"
        attempts[(rp, aid)] = record

    credited = set()
    delivery_keys = {}
    # Historical standalone validated rows are individual attempts, not the runtime's
    # synthetic `milestone_settlement` record. Keep their measured cost in the denominator.
    for key, record in attempts.items():
        if (record.get("validated") is True and record.get("validator_verdict") in ("ACCEPTED", "COMPLETE")
                and _attempt_failed(record) is not True):
            credited.add(key)
            delivery_keys[key] = (record.get("project"), record.get("milestone"),
                                  record.get("context_hash") or record.get("candidate_sha") or key)

    for settlement in settlements:
        rp = project_of(settlement)
        if project is not None and rp != project:
            continue
        if settlement.get("validated") is not True or settlement.get("validator_verdict") not in ("COMPLETE", "ACCEPTED"):
            continue
        ids = settlement.get("settled_attempt_ids")
        if not isinstance(ids, list) or not ids:
            # Historical settlements did not record the contributing attempts. Assigning
            # them to every engine that ever tried the milestone would reward failed engines.
            continue
        delivery = (rp, settlement.get("milestone"), settlement.get("context_hash"))
        for aid in ids:
            key = (rp, str(aid))
            record = attempts.get(key)
            if record is None or record.get("milestone") != settlement.get("milestone"):
                continue
            if not record.get("context_hash") or record.get("context_hash") != settlement.get("context_hash"):
                continue
            if _attempt_failed(record) is not False:
                continue
            credited.add(key)
            delivery_keys[key] = delivery

    stats = {}
    deliveries = {}
    for key, record in attempts.items():
        engine = record.get("engine") or "unknown"
        bucket = stats.setdefault(engine, {
            "attempts": 0, "total": 0, "failed": 0, "pending": 0,
            "accepted": 0, "validated_accepted": 0, "validated_deliveries": 0,
            "total_wall_time_s": 0.0, "wall_time_unknown": 0,
            "legacy_attempts_without_id": 0})
        bucket["attempts"] += 1
        bucket["total"] += 1
        if not record.get("attempt_id"):
            bucket["legacy_attempts_without_id"] += 1
        status = _attempt_failed(record)
        if status is True:
            bucket["failed"] += 1
        elif status is None:
            bucket["pending"] += 1
        cost = _measured_cost(record)
        if cost is None:
            bucket["wall_time_unknown"] += 1
        else:
            bucket["total_wall_time_s"] += cost
        if key in credited:
            bucket["accepted"] += 1
            bucket["validated_accepted"] += 1
            deliveries.setdefault(engine, set()).add(delivery_keys[key])
    for engine, bucket in stats.items():
        n_measured = bucket["attempts"] - bucket["wall_time_unknown"]
        bucket["avg_wall_time_s"] = bucket["total_wall_time_s"] / n_measured if n_measured else None
        if not n_measured:
            bucket["total_wall_time_s"] = None
        bucket["acceptance_rate"] = bucket["accepted"] / bucket["attempts"]
        bucket["validated_deliveries"] = len(deliveries.get(engine, ()))
    return stats
