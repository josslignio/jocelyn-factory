"""Runtime V2 — la boucle de contrôle autoritaire, pas à pas, restart-safe."""
from __future__ import annotations

import hashlib
import json
import os
import re as _re
import subprocess
import time
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace


from . import authority as _fx_authority
from .authority_gate import (IndependentValidator, MilestoneGateBlocked,
                             assert_milestone_settleable)
from .escalation_ladder import (EscalationStage, FailureClass, RecoveryLedgerUnknown, RecoveryWork,
                                SETTLEMENT_LEDGER_SCHEMA,
                                migrate_settlement_ledger,
                                reconcile_prepared_settlements,
                                classify_recovery_failure,
                                clear_infra_failure_count,
                                generate_diagnose_task, generate_replan_task,
                                get_escalation_stage, infra_rotation_needed,
                                is_equivalent_retry_cap_reached,
                                is_infrastructure_failure,
                                next_alternate_engine, quarantine_engine,
                                read_pending_recovery_work,
                                record_attempt as _escalation_record_attempt,
                                settle_recovery_work_with_ladder,
                                supersede_recovery_by_valid_product,
                                write_recovery_work)
from .executors import (AutopilotExecutor, LocalCommandExecutor,
                        WeeklyRefreshObservedExecutor)
from .mission_compiler import MissionCompileError, _VALID_EXECUTOR_TYPES, compile_missions
from .mission_evidence import clear_worker_state, is_worker_alive
from .worker_lease import WorkerLease
from .project_authority import (ProjectAuthority, ProjectBusy, load_authority,
                                load_project_state, project_transaction,
                                save_project_state, Resolution, resolve)
from .validation_contract import (ContractIntegrityError, freeze_contract,
                                 load_frozen_contract, record_contract_in_state,
                                 verify_frozen_against_current)

from .golden_promotion import GoldenPromotion as _GoldenPromotion
from factory_v2._io import _now

_REPO_ROOT = Path(__file__).parents[1]


def configured_max_workers() -> int:
    """Read worker capacity at runtime; this milestone still dispatches serially."""
    try:
        return max(1, int(os.environ.get("FACTORY_MAX_WORKERS", "1")))
    except (TypeError, ValueError):
        return 1


def _make_executor(project_dir: Path, mission,
                   preferred_engine: str = "") -> "LocalCommandExecutor | AutopilotExecutor | WeeklyRefreshObservedExecutor":
    """Dispatch executor based on mission.executor.type. FAIL CLOSED for unknown types."""
    mission_type = mission.executor.get("type", "")
    if mission_type == "autopilot":
        return AutopilotExecutor(repo=_REPO_ROOT, project_dir=project_dir,
                                 engine_hint=preferred_engine)
    if mission_type == "local_command":
        return LocalCommandExecutor(project_dir)
    if mission_type == "weekly_refresh_observed":
        return WeeklyRefreshObservedExecutor(project_dir)
    raise MissionCompileError(
        f"EXECUTOR_DISPATCH_ERROR: mission {mission.id} has unknown executor type "
        f"{mission_type!r}. Valid: {sorted(_VALID_EXECUTOR_TYPES)}. FAIL CLOSED — "
        f"never silently default to LocalCommandExecutor."
    )

try:
    from .runrecords import (record_attempt as _record_attempt,
                             record_settlement as _record_settlement,
                             record_prediction as _record_prediction,
                             record_observation as _record_observation,
                             reconcile_orphan_predictions as _reconcile_orphan_predictions)
    _RUNRECORDS_AVAILABLE = True
except Exception:
    _RUNRECORDS_AVAILABLE = False
    _record_attempt = None
    _record_settlement = None
    _record_prediction = None
    _record_observation = None
    _reconcile_orphan_predictions = None


@dataclass(frozen=True)
class CycleResult:
    action: str
    milestone: str | None
    detail: str
    authority_drift: list | None = None
    # Populated only by a successful settlement.
    touched_files: list[str] | None = None


@dataclass(frozen=True)
class SelectResult:
    """F5R4: Typed outcome from select_project(). kind values: PROJECT_SELECTED — found a project with open…"""
    kind: str
    project_dir: "Path | None" = None
    error: "str | None" = None


def _event(state: dict, kind: str, **fields) -> None:
    state["events"].append({"ts": _now(),
                            "event": kind, **fields})


# FACTORY_SPLIT_ADVANCE_V1: relais triviaux gardant un appel littéral `_event(state, "NAME"` dans…
def _emit_safety_quarantine_cleared(state: dict, **fields) -> None:
    _event(state, "SAFETY_QUARANTINE_CLEARED", **fields)


def _emit_repair_episode_reopened_by_verified_fix(state: dict, **fields) -> None:
    _event(state, "REPAIR_EPISODE_REOPENED_BY_VERIFIED_FIX", **fields)


def _emit_recovery_work_complete(state: dict, **fields) -> None:
    _event(state, "RECOVERY_WORK_COMPLETE", **fields)


# FACTORY_SPLIT_ADVANCE_V1: la construction du dict mission_rec vit ici, pas dans…
def _build_mission_state_record(mission_status, result, contract, prev_rec,
                                failure_fp, fp_history, _attempt_id,
                                _phase_attempt_id, lease, executor_type=""):
    return {
        "state": mission_status,
        "rc": result.rc, "output_tail": result.output_tail[-500:],
        "contract_id": contract["contract_id"],
        "attempt_count": prev_rec.get("attempt_count", 0) + 1,
        # G3 fingerprint: persist engine+fingerprint for equivalent-retry detection
        "last_engine": result.engine or "local",
        "last_failure_fingerprint": failure_fp,
        "failure_fingerprint_history": fp_history,
        # F4: persist attempt_id so validator can load receipt from disk after restart.
        "last_attempt_id": _attempt_id,
        # Sol: the accounting ledger's key for THIS attempt.
        "last_attempt_key": _phase_attempt_id,
        "executor_type": executor_type,
        "last_candidate_sha": getattr(result, "integration_sha", "") or "",
        # B2: preserve lease fields — do not overwrite lease_generation/fencing_token when rewriting mission…
        "lease_generation": lease.generation,
        "lease_fencing_token": lease.fencing_token,
        # LR5: preserve the pre-dispatch canonical context hash (same spirit as the lease fields above: a rewrite…
        "context_manifest_sha": prev_rec.get("context_manifest_sha", ""),
        # blocked_event_dedup: this wholesale rewrite used to silently drop `last_blocked_fingerprint` on every…
        "last_blocked_fingerprint": prev_rec.get("last_blocked_fingerprint", ""),
    }


# FACTORY_SPLIT_ADVANCE_V1: mêmes relais littéraux que _emit_safety_quarantine_cleared ci-dessus, pour…
def _emit_golden_promoted(state: dict, **fields) -> None:
    _event(state, "GOLDEN_PROMOTED", **fields)


def _emit_settlement_ledger_entry_written(state: dict, **fields) -> None:
    _event(state, "SETTLEMENT_LEDGER_ENTRY_WRITTEN", **fields)


def _emit_milestone_settled(state: dict, **fields) -> None:
    _event(state, "MILESTONE_SETTLED", **fields)


# FACTORY_SPLIT_ADVANCE_V1: tests/test_sol_exact_attempt_accounting.py scanne runtime.py par substring…
def _verified_attempt_record(mission_id, _rec, attempt, candidate_sha):
    return {
        "mission_id": mission_id,
        "attempt_key": _rec.get("last_attempt_key", ""),
        "attempt_id": attempt,
        "candidate_sha": candidate_sha,
    }


# FACTORY_SPLIT_ADVANCE_V1: même raison — tests/test_sol_exact_attempt_accounting.py exige…
def _project_verified_attempts_or_fail(project_dir, mid, contract, verified_attempts,
                                       receipt_sha, state, settled_at):
    from .launch_gate import AccountingProjectionError, project_verified_attempts
    try:
        validated = project_verified_attempts(
            project_dir, milestone=mid,
            contract_id=contract.get("contract_id", ""),
            verified=verified_attempts,
            receipt_sha=receipt_sha or "")
        if validated:
            _event(state, "ATTEMPTS_INDEPENDENTLY_VALIDATED", milestone=mid,
                   attempts=len(validated))
        return validated
    except AccountingProjectionError as _pexc:
        _event(state, "ACCOUNTING_PROJECTION_FAILED", milestone=mid,
               reason=_pexc.reason, detail=str(_pexc.detail)[:500])
        state["completed_milestones"][mid] = {
            "state": "ACCOUNTING_PROJECTION_FAILED",
            "contract_id": contract.get("contract_id", ""),
            "settled_at": settled_at,
            "reason": f"{_pexc.reason}: {str(_pexc.detail)[:300]}",
            # Nothing here may feed routing, benchmarking or auto-improvement: the accounting behind it is not…
            "usable_for_learning": False,
        }
        save_project_state(project_dir, state)
        return CycleResult("ACCOUNTING_PROJECTION_FAILED", mid, str(_pexc.detail)[:300])
    except Exception as _vexc:
        _event(state, "ACCOUNTING_PROJECTION_FAILED", milestone=mid,
               reason="UNEXPECTED", detail=str(_vexc)[:300])
        state["completed_milestones"][mid] = {
            "state": "ACCOUNTING_PROJECTION_FAILED",
            "contract_id": contract.get("contract_id", ""),
            "settled_at": settled_at, "reason": str(_vexc)[:300],
            "usable_for_learning": False,
        }
        save_project_state(project_dir, state)
        return CycleResult("ACCOUNTING_PROJECTION_FAILED", mid, str(_vexc)[:300])


# Hardened after Codex REJECT (CODEX_REVIEW_C3_C7.md finding 1): the previous pattern matched the bare…
_WAITING_FOR_EVIDENCE_RE = _re.compile(
    r"^WAITING_FOR_EVIDENCE:([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z) "
    r"verifier=([A-Za-z0-9_.-]+) rc=([0-9]+) head=([0-9a-f]{40})$",
    _re.MULTILINE)

# The header line authority_gate.py writes itself from the subprocess's REAL exit code (`f"$…
_VALIDATOR_LOG_HEADER_RC_RE = _re.compile(r"^rc=(-?[0-9]+)$", _re.MULTILINE)

#: Finding 1(f): a verifier (malicious, or merely stuck) that keeps handing back a
#: near-future calendar date must not be able to stall a milestone indefinitely.
#: After this many CONSECUTIVE waits with no progress on the same milestone, the
#: gate reverts to the ordinary GATE_BLOCKED path so recovery gets a turn.
WAITING_FOR_EVIDENCE_CONSECUTIVE_CAP = 4

#: Finding 1(d): next_check_at is refused (treated as a hard failure, never a
#: calendar wait) outside this window around "now".
_WAITING_MAX_HORIZON = timedelta(days=7)
_WAITING_MIN_HORIZON = timedelta(days=-1)


def _waiting_until(value: object) -> datetime | None:
    """Parse the calendar-wait convention strictly; malformed data never waits."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _waiting_marker(project_dir: Path, milestone_id: str, expected_sha: str = "") -> str:
    """Return a verifier's durable calendar marker, only when every hardening check below passes."""
    path = Path(project_dir) / "evidence" / milestone_id / "validator_e2e.log"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    header_rc = _VALIDATOR_LOG_HEADER_RC_RE.search(text)
    if not header_rc or header_rc.group(1) != "2":
        return ""
    matches = _WAITING_FOR_EVIDENCE_RE.findall(text)
    if len(matches) != 1:
        # Zero: no canonical line.
        return ""
    until_s, _verifier, rc_s, head = matches[0]
    if rc_s != "2":
        return ""
    if expected_sha and head != expected_sha:
        return ""
    until = _waiting_until(until_s)
    if until is None:
        return ""
    now = datetime.now(timezone.utc)
    if until > now + _WAITING_MAX_HORIZON or until <= now + _WAITING_MIN_HORIZON:
        return ""
    return until_s


def _authorized_settlement_supersedes(state: dict, milestone_id: str) -> str:
    """Name the prior candidate only after a supported settlement reopening."""
    last_settled = -1
    last_reopened = -1
    for index, event in enumerate(state.get("events") or []):
        if not isinstance(event, dict) or event.get("milestone") != milestone_id:
            continue
        if event.get("event") == "MILESTONE_SETTLED":
            last_settled = index
        elif event.get("event") == "SETTLEMENT_REOPENED_AS_UNPROVEN":
            last_reopened = index
    if last_reopened <= last_settled:
        return ""
    path = (_REPO_ROOT / "factory_v2" / "receipts" / "settlements"
            / f"{milestone_id}.json")
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    candidate = existing.get("candidate_sha", "")
    return candidate if isinstance(candidate, str) else ""


#: Typed, non-fatal selection warnings the supervisor logs each poll.
_SELECTION_WARNINGS: list = []


def _cursor_file(cursor_path, portfolio_file) -> "Path | None":
    if cursor_path:
        return Path(cursor_path)
    if portfolio_file:
        return Path(portfolio_file).parent / ".portfolio_cursor.json"
    return None


def _read_portfolio_cursor(cursor_path, n: int) -> int:
    """Durable rotation index. Unreadable or out of range ⇒ start at 0."""
    if not cursor_path or n <= 0:
        return 0
    try:
        v = int(json.loads(Path(cursor_path).read_text(encoding="utf-8"))["cursor"])
    except Exception:
        return 0
    return v % n if 0 <= v else 0


def _write_portfolio_cursor(cursor_path, value: int) -> str:
    """Persist the rotation index."""
    if not cursor_path:
        return ""
    p = Path(cursor_path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump({"schema": "portfolio-cursor/v1", "cursor": int(value)}, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, p)
        dfd = os.open(str(p.parent), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
        return ""
    except OSError as exc:
        return f"PORTFOLIO_CURSOR_WRITE_FAILED: {p} — {type(exc).__name__}: {exc}"


def _iso(epoch: int) -> str:
    """UTC ISO-8601 for an epoch second."""
    import time as _t
    return _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime(int(epoch)))


def _cannot_advance_this_cycle(resolution, state: dict, authority,
                               _resolution_dir=None) -> bool:
    """True when this project's next milestone provably cannot move right now."""
    mid = getattr(resolution, "next_milestone", None)
    if not mid:
        return True                       # nothing runnable at all
    rec = (state.get("completed_milestones") or {}).get(mid)
    if not isinstance(rec, dict):
        return False
    st = str(rec.get("state") or "")
    # A pending recovery is real, dispatchable work: _advance_locked() runs it BEFORE it ever reaches the…
    try:
        from .escalation_ladder import read_pending_recovery_work
        if read_pending_recovery_work(getattr(resolution, "_project_dir", None)
                                      or _resolution_dir) is not None:
            return False
    except Exception as exc:
        # Unreadable ⇒ UNKNOWN, and this function's own rule is that unknown keeps its turn. `except: pass` fell…
        _SELECTION_WARNINGS.append(
            f"RECOVERY_UNREADABLE: {_resolution_dir} — {type(exc).__name__}: {exc}")
        return False
    if st == "NOT_COMPILABLE":
        # Only a dead end while the authority has NOT moved: a changed roadmap is exactly the condition that…
        recorded = str(rec.get("roadmap_sha_at_record") or "")
        current = str(getattr(getattr(authority, "identity", None),
                              "roadmap_sha", "") or "")
        return bool(recorded) and recorded == current
    if st == "WAITING_FOR_EVIDENCE":
        until = _waiting_until(rec.get("next_check_at"))
        return until is not None and datetime.now(timezone.utc) < until
    # A typed terminal block does not become runnable by being polled again.
    if st in ("MILESTONE_REPLAN_EXHAUSTED", "REPLAN_EXHAUSTED"):
        return True
    # 2026-09-14: twitter-radar sat on STANDING_CAP_REACHED every poll for hours while the factory project…
    return _calendar_blocked(_resolution_dir)


def _calendar_blocked(project_dir) -> bool:
    if not project_dir:
        return False
    try:
        from . import recovery_allocation as _ra
        from .launch_gate import cost_no_progress_now
        if not cost_no_progress_now(Path(project_dir)).get("tripped"):
            return False
        standing = _ra.available_standing(Path(project_dir))
        if not standing.get("standing_cap_reached"):
            return False
        until = _waiting_until(standing.get("resets_at"))
        return until is not None and datetime.now(timezone.utc) < until
    except Exception:
        return False                          # unknown keeps its turn (this function's rule)


def _record_authority_incident(project_dir, reason: str) -> str:
    """FACTORY_PORTFOLIO_ISOLATION_V1: durable per-project refusal record."""
    from .progress_truth import Incident, persist
    evidence = {"project": Path(project_dir).name, "error": reason}
    inc = Incident(kind="PROJECT_AUTHORITY_UNREADABLE",
                   detail=f"{Path(project_dir).name}: authority/state unreadable — {reason}",
                   detected_at=_now(),
                   next_safe_action="fix this project's ROADMAP.yaml/PROJECT_STATE.json; "
                                    "it rejoins the rotation automatically once loadable",
                   cause_must_be_fixed=True,
                   evidence=evidence)
    ledger = Path(project_dir) / "evidence" / "progress_incidents.jsonl"
    try:
        if ledger.is_file():
            with ledger.open("r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        if json.loads(line).get("dedup_key") == inc.dedup_key:
                            return ""      # same cause already durable — once per cause
                    except ValueError:
                        continue           # corrupt legacy line never blocks a new record
        persist([inc], ledger)
        return ""
    except OSError as exc:
        return (f"INCIDENT_LEDGER_WRITE_FAILED: {ledger} — "
                f"{type(exc).__name__}: {exc}")


def select_project(portfolio_dir: Path, portfolio_file: "Path | None" = None,
                    cursor_path: "Path | None" = None) -> SelectResult:
    """F5R4: Returns a typed SelectResult — never silently converts errors to PORTFOLIO_COMPLETE."""
    pfile = portfolio_file if portfolio_file is not None else (Path(portfolio_dir) / "portfolio.json")
    cursor_path = _cursor_file(cursor_path, pfile)
    if not pfile.is_file():
        return SelectResult(kind="PORTFOLIO_UNAVAILABLE")
    try:
        portfolio = json.loads(pfile.read_text(encoding="utf-8"))
    except Exception as exc:
        return SelectResult(kind="CONTROL_PLANE_ERROR",
                            error=f"portfolio.json parse error: {exc}")
    # Strict schema validation — fail-closed on any structural anomaly.
    if not isinstance(portfolio, dict):
        return SelectResult(kind="CONTROL_PLANE_ERROR",
                            error=f"portfolio.json must be a dict, got {type(portfolio).__name__}")
    schema_ver = portfolio.get("schema", "")
    if schema_ver != "portfolio/v1":
        return SelectResult(kind="CONTROL_PLANE_ERROR",
                            error=f"portfolio.json unsupported schema {schema_ver!r}; expected 'portfolio/v1'")
    projects = portfolio.get("projects")
    if projects is None:
        return SelectResult(kind="CONTROL_PLANE_ERROR",
                            error="portfolio.json missing required 'projects' key")
    if not isinstance(projects, list):
        return SelectResult(kind="CONTROL_PLANE_ERROR",
                            error=f"portfolio.json 'projects' must be a list, got {type(projects).__name__}")
    if not projects:
        # Empty list is legitimate — all projects have been removed, nothing to do.
        return SelectResult(kind="PORTFOLIO_COMPLETE")

    # Two-pass: validate ALL entries first, then select.
    control_errors: list[str] = []           # structural portfolio anomalies (fail-closed)
    isolated_errors: list[str] = []          # per-project authority/state refusals (isolated)
    valid_entries: list[tuple[int, Path]] = []  # (i, pdir) for well-formed entries
    seen_pdir_keys: set[Path] = set()        # resolved paths for duplicate detection
    portfolio_root_raw = Path(portfolio_dir)
    portfolio_root_resolved = portfolio_root_raw.resolve()

    for i, entry in enumerate(projects):
        try:
            if not isinstance(entry, dict):
                raise ValueError(f"entry[{i}] must be a dict, got {type(entry).__name__}")
            pdir_str = entry.get("project_dir")
            if not pdir_str:
                raise ValueError(f"entry[{i}] missing 'project_dir'")
            # Unresolved path (backward compat — returned in SelectResult as-is).
            pdir = portfolio_root_raw / pdir_str
            # Resolve both sides for confinement and duplicate checks so that symlinks and `../..` traversals are…
            pdir_resolved = pdir.resolve()
            try:
                pdir_resolved.relative_to(portfolio_root_resolved)
            except ValueError:
                raise ValueError(
                    f"entry[{i}] project_dir {pdir_str!r} escapes portfolio root "
                    f"(resolved to {pdir_resolved}, portfolio root {portfolio_root_resolved})"
                )
            # Duplicate detection on resolved paths — catches `a/../a` style aliases.
            if pdir_resolved in seen_pdir_keys:
                raise ValueError(
                    f"entry[{i}] project_dir {pdir_str!r} is a duplicate "
                    f"(already registered in this portfolio)"
                )
            seen_pdir_keys.add(pdir_resolved)
            # Probe that authority and state can be loaded — surfaces missing dirs early.
            try:
                _fx_authority.load_roadmap(pdir)
                load_authority(pdir)
                load_project_state(pdir)
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"
                werr = _record_authority_incident(pdir, reason)
                if werr:
                    # No durable trace -> no isolation.
                    control_errors.append(f"{pdir_str}: {werr}")
                else:
                    isolated_errors.append(f"{pdir_str}: {reason}")
                    warn = (f"PROJECT_AUTHORITY_UNREADABLE: {pdir_str} refused alone — "
                            f"rotation continues on valid projects ({reason})")
                    if warn not in _SELECTION_WARNINGS:   # bounded: one line per cause
                        _SELECTION_WARNINGS.append(warn)
                continue
            valid_entries.append((i, pdir))
        except Exception as exc:
            label = (entry.get("project_dir", f"entry[{i}]")
                     if isinstance(entry, dict) else f"entry[{i}]")
            control_errors.append(f"{label}: {exc}")

    if control_errors:
        # Fail-closed: structural anomalies (or an unwritable incident ledger) block the entire portfolio cycle.
        return SelectResult(
            kind="CONTROL_PLANE_ERROR",
            error="project authority/state errors: " + "; ".join(control_errors),
        )
    if not valid_entries:
        # Every project was refused individually.
        return SelectResult(
            kind="CONTROL_PLANE_ERROR",
            error="no valid project in portfolio (each refused alone by an unreadable "
                  "authority/state — see per-project evidence/progress_incidents.jsonl): "
                  + "; ".join(isolated_errors),
        )

    # Selection pass: first project with work it can actually ADVANCE.
    try:
        with project_transaction(cursor_path.parent, timeout_s=5.0):
            cursor = _read_portfolio_cursor(cursor_path, len(valid_entries))
            ordered = valid_entries[cursor:] + valid_entries[:cursor]

            deferred: list = []
            for _i, pdir in ordered:
                try:
                    authority = load_authority(pdir)
                    state = load_project_state(pdir)
                    resolution = resolve(authority, state)
                    if resolution.has_work or not resolution.all_complete:
                        if _cannot_advance_this_cycle(resolution, state, authority, pdir):
                            deferred.append(pdir)
                            continue
                        _cerr = _write_portfolio_cursor(
                            cursor_path,
                            (valid_entries.index((_i, pdir)) + 1) % len(valid_entries))
                        if _cerr:
                            return SelectResult(kind="CONTROL_PLANE_ERROR", error=_cerr)
                        return SelectResult(kind="PROJECT_SELECTED", project_dir=pdir)
                except Exception as exc:
                    # State changed between validation and selection (race) — treat as error.
                    return SelectResult(
                        kind="CONTROL_PLANE_ERROR",
                        error=f"{pdir.name}: state changed during selection: {exc}")

            if deferred:
                # Every project is blocked.
                _cerr = _write_portfolio_cursor(cursor_path, (cursor + 1) % len(valid_entries))
                if _cerr:
                    return SelectResult(kind="CONTROL_PLANE_ERROR", error=_cerr)
                return SelectResult(kind="PROJECT_SELECTED", project_dir=deferred[0])
            if isolated_errors:
                # At least one project's state is UNKNOWN.
                return SelectResult(
                    kind="CONTROL_PLANE_ERROR",
                    error="portfolio completion unknown; unreadable project(s): "
                          + "; ".join(isolated_errors),
                )
            return SelectResult(kind="PORTFOLIO_COMPLETE")
    except ProjectBusy as busy:
        # Typed, not an error: another selector owns the cursor right now.
        return SelectResult(kind="CONTROL_PLANE_ERROR",
                            error=f"portfolio cursor busy: {busy}")


def _execute_recovery_work(
    project_dir: Path,
    recovery: "RecoveryWork",
    state: dict,
    mid: str,
    authority,
    executor,
) -> CycleResult:
    """F5R5: Execute a pending RecoveryWork through the normal executor pipeline."""
    # Build a minimal mission-like object for the executor
    from dataclasses import dataclass as _dc

    @_dc
    class _RecoveryMission:
        id: str
        executor: dict

    task_file = recovery.task_file
    _state_dirty = False            # events appended before the gate must survive it
    if recovery.kind == "DIAGNOSE":
        # R4: when canonical authority already declares a deterministic repair recipe for this milestone, the…
        from .safety_quarantine import get_repair_contract as _grc
        _dc = _grc(project_dir)
        if _dc is not None and _dc.verify_integrity() and _dc.repair_command:
            _art = project_dir / f"evidence/recovery/{recovery.recovery_id}_diagnosis.json"
            _art.parent.mkdir(parents=True, exist_ok=True)
            _art.write_text(json.dumps({
                "root_cause": (f"{recovery.failure_fingerprint[:200]} — frozen "
                               f"reproduction {_dc.reproduction_command!r} fails at "
                               f"{_dc.pre_repair_sha[:12]}"),
                "proposed_fix": _dc.repair_command,
                "confidence": "HIGH",
                "reasoning": ("deterministic typed repair recipe declared in canonical "
                              f"authority for milestone {_dc.milestone_id}"),
                "source": "authority_repair_recipe",
                "contract_id": _dc.contract_id,
            }, indent=2))
            _event(state, "DIAGNOSE_FROM_AUTHORITY_RECIPE",
                   recovery_id=recovery.recovery_id, milestone=_dc.milestone_id)
            _state_dirty = True

    if recovery.kind == "SAFE_REPAIR":
        # R4: a repair is done when the FROZEN reproduction command passes — not when a report says so.
        from .safety_quarantine import get_repair_contract
        _rc = get_repair_contract(project_dir)
        if _rc is None or not _rc.verify_integrity():
            _reason = ("SAFE_REPAIR_WITHOUT_CONTRACT: no intact frozen RepairContract to "
                       "define what this repair must achieve")
            _event(state, "RECOVERY_WORK_FAILED", mission=recovery.parent_mission_id,
                   recovery_id=recovery.recovery_id, recovery_kind=recovery.kind,
                   output=_reason)
            save_project_state(project_dir, state)
            return CycleResult("RECOVERY_WORK_FAILED", mid, _reason)
        if _rc.repair_command:
            # Deterministic typed recipe declared in canonical authority — run it through the normal executor rather…
            exec_dict = {
                "type": "local_command",
                "command": _rc.repair_command,
                "cwd": "product_repo",
                "product_repo": _rc.command_cwd,
                "test_cmd": _rc.reproduction_command,
            }
        else:
            exec_dict = {
                "type": "autopilot",
                "task_file": task_file,
                "spec_level": "A",
                "budget_s": 1800,
                "test_cmd": _rc.reproduction_command,
                "product_repo": _rc.command_cwd,
            }
    elif recovery.kind == "DIAGNOSE" and (project_dir /
            f"evidence/recovery/{recovery.recovery_id}_diagnosis.json").is_file():
        # The diagnosis artifact already exists (authority recipe above) — verify it through the same artifact…
        _art_rel = f"evidence/recovery/{recovery.recovery_id}_diagnosis.json"
        exec_dict = {
            "type": "local_command",
            "cwd": "project_dir",
            "command": (
                f"python3 -c \"import json; d=json.load(open('{project_dir / _art_rel}')); "
                "assert d.get('root_cause') and d.get('proposed_fix')\""
            ),
        }
    elif task_file:
        # AutopilotExecutor path: task_file contains the diagnosis/replan task.
        artifact_path = str(project_dir / (
            f"evidence/recovery/{recovery.recovery_id}_diagnosis.json"
            if recovery.kind == "DIAGNOSE"
            else f"evidence/recovery/{recovery.recovery_id}_replan.json"
        ))
        if recovery.kind == "DIAGNOSE":
            _validate_cmd = (
                f"python3 -c \"import json; d=json.load(open('{artifact_path}')); "
                "assert d.get('root_cause') and d.get('proposed_fix'), "
                "'DIAGNOSE artifact missing root_cause or proposed_fix'\""
            )
        else:  # REPLAN
            # The recovery's own acceptance must accept what the REPLAN task now asks for.
            _validate_cmd = (
                f"python3 -c \"import json; d=json.load(open('{artifact_path}')); "
                "p=d.get('derived_plan'); "
                "assert isinstance(p, dict) and p.get('changes'), "
                "'REPLAN artifact has no derived_plan with a non-empty changes object'\""
            )
        exec_dict = {
            "type": "autopilot",
            "task_file": task_file,
            "spec_level": "A",
            "budget_s": 1800,
            "test_cmd": _validate_cmd,
            # LOT C1b: routes this dispatch through model_router as task_kind='diagnose'|'replan'…
            "task_kind": "diagnose" if recovery.kind == "DIAGNOSE" else "replan",
        }
    else:
        # Fallback: shell command to verify the artifact exists (no task_file = no autopilot).
        artifact = (f"evidence/recovery/{recovery.recovery_id}_diagnosis.json"
                    if recovery.kind == "DIAGNOSE"
                    else f"evidence/recovery/{recovery.recovery_id}_replan.json")
        exec_dict = {
            "type": "local_command",
            "command": f"test -f {artifact}",
        }

    mission = _RecoveryMission(id=f"recovery_{recovery.recovery_id}", executor=exec_dict)
    runner = executor or _make_executor(project_dir, mission)

    # ── §F3/F5: the SECOND path that spends ─────────────────────────────────
    # runtime.py has two call sites that reach an engine: mission dispatch in
    # _advance_locked, and this one. Only the first was gated, and
    # launch_gate_precedes_execute() was scoped to _advance_locked — so it certified
    # the path that had a gate and could not see the path that did not. Accounting
    # that covers one of two spending paths is not accounting.
    #
    # (I first reported finding this by watching live consumption after a rearm.
    # That was wrong: the RECOVERY_WORK_FAILED event I read was hours old and I
    # misattributed a fresh cause to a stale artifact. The defect is real and
    # verifiable by AST on the pre-fix code — runner.execute at line 366 with no
    # check_before_launch anywhere in the function — but it was found by reading,
    # not by observation.)
    #
    # A recovery is how a blocked milestone gets unblocked, so a refusal here must
    # not brick the project: COST_NO_PROGRESS and budget still apply, but a block is
    # returned as a typed result rather than raised.
    from .launch_gate import (LaunchBlocked, check_before_launch, close_account,
                              open_account)
    # R2C: a recovery whose command the SUPERVISOR authored (the frozen reproduction, or the artifact check…
    _local_recovery = exec_dict.get("type") == "local_command"
    # One key per DISPATCH, not per recovery record.
    _rec_base = f"recovery:{recovery.recovery_id}:{recovery.kind}"
    try:
        from .launch_gate import ledger_path as _lp
        from .resource_accounting import read_rows as _rr
        _prior = {str(r.get("attempt_key") or "") for r in _rr(_lp(project_dir))}
        _n = sum(1 for k in _prior if k.startswith(_rec_base + ":d"))
    except Exception:
        _n = 0
    _rec_key = f"{_rec_base}:d{_n + 1}"
    # Closing stale work is local bookkeeping and spends nothing.
    from .escalation_ladder import _recovery_is_stale as _stale_why
    _stale = _stale_why(project_dir, {"parent_mission_id": recovery.parent_mission_id,
                                      "failure_fingerprint": recovery.failure_fingerprint})
    _stale_record_provenance = (
        not recovery.dedup_key or bool(recovery.identity_key))
    if _stale and _stale_record_provenance:
        try:
            _rp = project_dir / "evidence" / "recovery" / f"{recovery.recovery_id}.json"
            _rd = json.loads(_rp.read_text(encoding="utf-8"))
            _rd.update({"completed": True, "completed_by": _stale,
                        "completed_at": _now()})
            _rp.write_text(json.dumps(_rd, indent=2, ensure_ascii=False), encoding="utf-8")
        except (OSError, ValueError):
            pass
        _event(state, "RECOVERY_WORK_STALE_SKIPPED", mission=recovery.parent_mission_id,
               recovery_id=recovery.recovery_id, reason=_stale)
        save_project_state(project_dir, state)
        return CycleResult("RECOVERY_WORK_STALE_SKIPPED", mid, _stale)
    try:
        check_before_launch(
            project_dir, mission_id=mission.id, milestone=mid,
            contract_frozen=True,          # recovery runs under the quarantine contract
            expected_result_defined=bool(exec_dict.get("test_cmd")
                                         or exec_dict.get("command")),
            independent_proof_defined=bool(exec_dict.get("test_cmd")
                                           or exec_dict.get("command")),
            mission=mission, contract_body={}, own_lease_generation=None,
            supervisor_proved_no_model=_local_recovery)
    except LaunchBlocked as _rblocked:
        # R2C: a poll of an unchanged refusal must not rewrite the state either — the first observation is…
        from .blocked_event_dedup import should_emit as _should_emit_rblocked
        _emit_rblocked = _should_emit_rblocked(state, mission.id,
                                               _rblocked.reason, _rblocked.detail)
        if _emit_rblocked or _state_dirty:
            if _emit_rblocked:
                _event(state, "RECOVERY_LAUNCH_BLOCKED",
                       mission=recovery.parent_mission_id,
                       recovery_id=recovery.recovery_id, reason=_rblocked.reason,
                       detail=str(_rblocked.detail)[:300])
            save_project_state(project_dir, state)
        return CycleResult("RECOVERY_LAUNCH_BLOCKED", mid,
                           f"{recovery.kind} refused: {_rblocked.reason}")

    # Legacy work without a control-code provenance stamp still has to pass the launch authority gate above.
    if _stale:
        try:
            _rp = project_dir / "evidence" / "recovery" / f"{recovery.recovery_id}.json"
            _rd = json.loads(_rp.read_text(encoding="utf-8"))
            _rd.update({"completed": True, "completed_by": _stale,
                        "completed_at": _now()})
            _rp.write_text(json.dumps(_rd, indent=2, ensure_ascii=False), encoding="utf-8")
        except (OSError, ValueError):
            pass
        _event(state, "RECOVERY_WORK_STALE_SKIPPED", mission=recovery.parent_mission_id,
               recovery_id=recovery.recovery_id, reason=_stale)
        save_project_state(project_dir, state)
        return CycleResult("RECOVERY_WORK_STALE_SKIPPED", mid, _stale)

    _rec_task_kind = (
        "local" if exec_dict.get("type") == "local_command"
        else "diagnose" if recovery.kind == "DIAGNOSE"
        else "replan" if recovery.kind == "REPLAN"
        else "build"
    )
    _rec_account = open_account(
        project_dir, attempt_key=_rec_key, project=state.get("project", ""),
        milestone=mid, mission=mission.id, attempt=1,
        engine=exec_dict.get("type", ""), model="",
        spec_level=exec_dict.get("spec_level", ""), task_kind=_rec_task_kind,
        consumes_model_quota=not _local_recovery)
    _rec_t0 = time.monotonic()
    try:
        if _local_recovery:
            # Supervisor-authored deterministic work: no model call, no meter.
            result = runner.execute(mission)
        else:
            # Second spending path, same reliable telemetry as mission dispatch: durable OPEN event before the AI…
            from .telemetry.gate import metered_launch as _metered_launch
            with _metered_launch(
                    project_dir, attempt_key=_rec_key, mission=mission.id,
                    milestone=mid, engine=exec_dict.get("type", "")) as _rmeter:
                result = runner.execute(mission)
                _rmeter.update(
                    outcome="SUCCESS" if result.ok else "FAILURE",
                    rc=result.rc,
                    engine_execution=getattr(
                        result, "engine_execution", None) or {})
    except BaseException:
        close_account(project_dir, _rec_account, exit_class="KILL",
                      mission_pass=False, duration_s=time.monotonic() - _rec_t0)
        raise
    # Recovery is control-plane work: it never produces a product delta, so independently_validated stays…
    close_account(project_dir, _rec_account,
                  exit_class="SUCCESS" if result.ok else "SEMANTIC_FAILURE",
                  mission_pass=bool(result.ok),
                  duration_s=time.monotonic() - _rec_t0,
                  engine_execution=getattr(result, "engine_execution", None),
                      result=result)
    if recovery.kind == "SAFE_REPAIR" and result.ok:
        # R4: a repair only counts once the FROZEN reproduction passes, and it must leave a real commit…
        from .safety_quarantine import _run_contract_command, get_repair_contract
        _rc2 = get_repair_contract(project_dir)
        _rc_rc, _rc_out = _run_contract_command(
            _rc2.reproduction_command, _rc2.command_cwd, timeout=600)
        if _rc_rc != 0:
            _event(state, "SAFE_REPAIR_DID_NOT_FIX", recovery_id=recovery.recovery_id,
                   exit_code=_rc_rc, output=_rc_out[-300:])
            state = settle_recovery_work_with_ladder(
                project_dir, recovery, state, success=False,
                failure_class=FailureClass.SEMANTIC)
            save_project_state(project_dir, state)
            return CycleResult("RECOVERY_WORK_FAILED", mid,
                               f"SAFE_REPAIR ran but the frozen reproduction still "
                               f"fails (rc={_rc_rc}): {_rc_out[-200:]}")
        _repair_sha, _commit_why = _commit_repair(
            _rc2.command_cwd, recovery.recovery_id, _rc2.allowed_scope)
        if _commit_why:
            _event(state, "SAFE_REPAIR_NOT_COMMITTED", recovery_id=recovery.recovery_id,
                   reason=_commit_why[:300])
            state = settle_recovery_work_with_ladder(
                project_dir, recovery, state, success=False,
                failure_class=FailureClass.SEMANTIC)
            save_project_state(project_dir, state)
            return CycleResult("RECOVERY_WORK_FAILED", mid,
                               f"SAFE_REPAIR produced no committable repair: {_commit_why}")
        _event(state, "SAFE_REPAIR_COMMITTED", recovery_id=recovery.recovery_id,
               repair_sha=_repair_sha[:12], command_cwd=_rc2.command_cwd,
               allowed_scope=_rc2.allowed_scope)

    from .advance import recovery_settle
    return recovery_settle.finish_recovery(
        project_dir, recovery, state, mid, authority, result)


def _commit_repair(repo: str, recovery_id: str, allowed_scope: list | None = None
                   ) -> tuple[str, str]:
    """Commit what the repair produced, within its allowed scope."""
    import subprocess as _sp

    def _git(*args: str) -> tuple[int, str]:
        r = _sp.run(["git", *args], cwd=repo, capture_output=True, text=True, timeout=120)
        return r.returncode, r.stdout.strip()

    _, head_before = _git("rev-parse", "HEAD")
    # NB: porcelain lines are "XY PATH" where X may be a space, so the raw stdout must NOT be stripped…
    _status = _sp.run(["git", "status", "--porcelain", "--untracked-files=all"],
                      cwd=repo, capture_output=True, text=True, timeout=120)
    changed = [line[3:].strip() for line in _status.stdout.splitlines() if line.strip()]

    if allowed_scope:
        def _in_scope(path: str) -> bool:
            return any(path == s or path.startswith(s.rstrip("/") + "/")
                       for s in allowed_scope)
        out_of_scope = sorted(p for p in changed if not _in_scope(p))
        if out_of_scope:
            return head_before, (
                f"OUT_OF_SCOPE_CHANGES: {out_of_scope[:5]} are outside the repair's "
                f"allowed scope {allowed_scope} — refusing to commit them as a repair")
        for spec in allowed_scope:
            _git("add", "--", spec)
    else:
        _git("add", "-A")

    rc, staged = _git("diff", "--cached", "--name-only")
    if rc != 0 or not staged:
        return head_before, "NO_STAGED_CHANGES: the repair produced nothing to commit"
    c_rc, _ = _git("commit", "-q", "-m", f"factory(SAFE_REPAIR): {recovery_id}")
    # L2_runtime_core-015: a refused commit (hook, identity, lock) must never be reported as a committed…
    if c_rc != 0:
        return head_before, (
            f"COMMIT_FAILED: git commit rc={c_rc} — the repair is still staged, "
            "HEAD is unchanged")
    _, head = _git("rev-parse", "HEAD")
    if head == head_before:
        return head_before, "COMMIT_FAILED: HEAD did not move after git commit"
    return head, ""


def _raise_reentry_maintenance_request(project_dir: Path, state: dict, qrec: dict,
                                       old_sha: str, new_sha: str, why: str) -> None:
    """Record the blocked repair re-entry durably, once per blocked position."""
    try:
        from .maintenance_request import (KIND_REPAIR_REENTRY_UNPROVABLE,
                                          raise_request)
        from .safety_quarantine import get_repair_contract
        contract = get_repair_contract(project_dir)
        reproduction = getattr(contract, "reproduction_command", "") if contract else ""
        record, is_new = raise_request(
            project_dir,
            kind=KIND_REPAIR_REENTRY_UNPROVABLE,
            milestone_id=str(qrec.get("milestone_id") or ""),
            fingerprint=str(qrec.get("failure_fingerprint") or ""),
            old_code_sha=str(old_sha or "UNSTAMPED"),
            new_code_sha=str(new_sha or ""),
            missing_proof=why,
            reproduction=str(reproduction or ""),
            resolves_when=(
                "the supervisor executes the frozen reproduction at both the episode's "
                "control SHA and the current one and observes RED then GREEN. A doc "
                "edit, an authority clarification or a new commit that does not move "
                "the reproduction resolves nothing."),
        )
        if is_new:
            # `kind` collides with _event's own positional parameter of the same name (`def _event(state, kind…
            _event(state, "MAINTENANCE_REQUEST_RAISED",
                   request_id=record["request_id"], request_kind=record["kind"],
                   milestone=record["milestone_id"],
                   missing_proof=record["missing_proof"][:200])
    except Exception as exc:                            # noqa: BLE001
        # C11: deduplicated the same way as QUARANTINE_MAX_ITERATIONS / REPAIR_EPISODE_REENTRY_REFUSED below — an…
        from .blocked_event_dedup import should_emit as _should_emit_mrf
        _mrf_key = f"maintenance_request_failed:{qrec.get('milestone_id', '')}"
        _mrf_detail = {"old_sha": str(old_sha or ""), "new_sha": str(new_sha or ""),
                       "error_type": type(exc).__name__}
        if _should_emit_mrf(state, _mrf_key, "MAINTENANCE_REQUEST_FAILED", _mrf_detail):
            _event(state, "MAINTENANCE_REQUEST_FAILED",
                   error=f"{type(exc).__name__}: {exc}"[:200])


def _consume_derived_plan(project_dir: Path, state: dict, authority, milestone_id: str,
                          contract: dict, missions: list) -> tuple[list, str]:
    """Apply the persisted derived plan to this cycle's compiled mission list."""
    from .derived_plan import (ENGINE_SOURCE_KEY, ENGINE_SOURCE_PREFIX,
                               apply_derived_plan, load_derived_plan, plan_path)

    ident = authority.identity.as_dict() if authority is not None else {}
    hashes = {k: str(ident.get(k) or "")
              for k in ("strategy_sha", "spec_sha", "roadmap_sha")}
    # A plan binds to ONE interrupted mission and that mission's exact failure.
    raw_bound = ""
    try:
        import json as _json
        raw_bound = str(_json.loads(
            plan_path(project_dir, milestone_id).read_text(encoding="utf-8")
        ).get("mission_id") or "")
    except Exception:                                   # noqa: BLE001
        raw_bound = ""
    rec = (state.get("missions", {}) or {}).get(raw_bound, {}) or {}
    try:
        plan, why = load_derived_plan(
            project_dir, milestone_id,
            project=str(state.get("project") or Path(project_dir).name),
            contract_id=str(contract.get("contract_id") or ""),
            authority_hashes=hashes, mission_ids=[m.id for m in missions],
            failure_fingerprint=str(rec.get("last_failure_fingerprint") or ""),
            mission_id=raw_bound,
            current_engine=str(rec.get("last_engine") or ""))
    except Exception as exc:                            # noqa: BLE001
        _event(state, "DERIVED_PLAN_IGNORED", milestone=milestone_id,
               reason=f"{type(exc).__name__}: {exc}"[:200])
        plan, why = None, ""
    if plan is None:
        # A plan that no longer binds must leave NOTHING behind.
        _withdraw_derived_engine_hints(state, missions)
        if why:
            _event(state, "DERIVED_PLAN_IGNORED", milestone=milestone_id,
                   reason=why[:300])
        return missions, why
    ordered = apply_derived_plan(missions, plan)
    engine = plan.engine()
    if engine:
        for m in ordered:
            state.setdefault("missions", {}).setdefault(m.id, {})
            state["missions"][m.id]["preferred_engine"] = engine
            state["missions"][m.id][ENGINE_SOURCE_KEY] = \
                f"{ENGINE_SOURCE_PREFIX}{plan.plan_id}"
    else:
        _withdraw_derived_engine_hints(state, ordered)
    _event(state, "DERIVED_PLAN_ACTIVE", milestone=milestone_id,
           plan_id=plan.plan_id, mission=plan.mission_id,
           order=[m.id for m in ordered], engine=engine,
           changes=sorted(plan.changes))
    return ordered, plan.plan_id


def _withdraw_derived_engine_hints(state: dict, missions: list) -> None:
    """Drop engine hints a derived plan placed, keep independently-chosen ones."""
    from .derived_plan import ENGINE_SOURCE_KEY, ENGINE_SOURCE_PREFIX
    for m in missions:
        rec = (state.get("missions", {}) or {}).get(m.id)
        if not isinstance(rec, dict):
            continue
        if str(rec.get(ENGINE_SOURCE_KEY) or "").startswith(ENGINE_SOURCE_PREFIX):
            rec.pop("preferred_engine", None)
            rec.pop(ENGINE_SOURCE_KEY, None)


def _derived_plan_position(project_dir: Path, state: dict, authority,
                           milestone_id: str) -> tuple[str, dict, list]:
    """(contract_id, authority hashes, compiled mission ids) for the active milestone."""
    rec = (state.get("completed_milestones", {}) or {}).get(milestone_id, {}) or {}
    contract_id = str(rec.get("contract_id") or "")
    ident = authority.identity.as_dict() if authority is not None else {}
    hashes = {k: str(ident.get(k) or "")
              for k in ("strategy_sha", "spec_sha", "roadmap_sha")}
    mission_ids: list = []
    entry = _milestone_entry(authority, milestone_id) if authority is not None else None
    if entry and contract_id:
        try:
            mission_ids = [m.id for m in compile_missions(authority, entry, contract_id)]
        except MissionCompileError:
            mission_ids = []
    return contract_id, hashes, mission_ids


def _apply_derived_replan(*, project_dir: Path, state: dict, recovery,
                          authority, raw_plan) -> CycleResult:
    """Validate and persist a REPLAN's derived execution plan."""
    from .derived_plan import validate_derived_plan, write_derived_plan

    mid = recovery.milestone_id
    contract_id, hashes, mission_ids = _derived_plan_position(
        project_dir, state, authority, mid)

    def _refuse(reason: str) -> CycleResult:
        _event(state, "DERIVED_PLAN_REFUSED", milestone=mid,
               recovery_id=recovery.recovery_id, reason=reason[:300])
        _settled = settle_recovery_work_with_ladder(
            project_dir, recovery, state, success=False,
            failure_class=FailureClass.SEMANTIC)
        # settle_* may return a NEW dict; the caller holds its own reference and saves that one, so the result is…
        if _settled is not state:
            state.clear()
            state.update(_settled)
        return CycleResult("DERIVED_PLAN_REFUSED", mid, reason)

    if not contract_id:
        return _refuse("DERIVED_PLAN_NO_FROZEN_CONTRACT: the milestone has no frozen "
                       "contract in state; a derived plan must bind to one")

    # The engine currently in force for the interrupted mission — the baseline a proposed engine change has…
    _rec = (state.get("missions", {}) or {}).get(recovery.parent_mission_id, {}) or {}
    plan, why = validate_derived_plan(
        raw_plan, project=str(state.get("project") or Path(project_dir).name),
        milestone_id=mid, contract_id=contract_id, authority_hashes=hashes,
        mission_ids=mission_ids,
        failure_fingerprint=recovery.failure_fingerprint,
        mission_id=recovery.parent_mission_id,
        current_engine=str(_rec.get("last_engine") or recovery.engine or ""))
    if plan is None:
        return _refuse(why)

    written, why = write_derived_plan(project_dir, plan)
    if written is None:
        return _refuse(why)

    # Settle the recovery as successful only NOW — after the plan it was supposed to produce is durable.
    _settled = settle_recovery_work_with_ladder(
        project_dir, recovery, state, success=True)
    if _settled is not state:
        state.clear()
        state.update(_settled)

    _event(state, "DERIVED_PLAN_APPLIED", milestone=mid,
           recovery_id=recovery.recovery_id, plan_id=written.plan_id,
           contract_id=contract_id[:16], changes=sorted(written.changes),
           rationale=written.rationale[:200])
    return CycleResult(
        "DERIVED_PLAN_APPLIED", mid,
        f"{mid}: REPLAN persisted derived plan {written.plan_id} "
        f"({', '.join(sorted(written.changes))}) — canonical authority unchanged; "
        f"the next cycle re-validates it from disk before executing it")


def _apply_replan_patch(
    state: dict,
    roadmap_path: Path,
    patch_text: str,
    milestone_id: str,
    rationale: str,
    proposer_identity: str = "runtime",
    failure_fingerprint: str = "",
) -> CycleResult:
    """REFUSED. A REPLAN may replan the derived plan; it may not rewrite intent. This function used to build…"""
    _event(state, "CANONICAL_AUTHORITY_WRITE_REFUSED",
           milestone=milestone_id, path=str(roadmap_path),
           actor=proposer_identity,
           reason="REPLAN may replan the derived plan only; Strategy/Spec/Roadmap "
                  "express owner intent and are immutable to the Factory")
    _event(state, "OWNER_AUTHORITY_CHANGE_REQUIRED",
           milestone=milestone_id, rationale=str(rationale)[:400],
           failure_fingerprint=str(failure_fingerprint)[:200],
           note="evidence preserved; the controller continues other safe work")
    return CycleResult(
        "OWNER_AUTHORITY_CHANGE_REQUIRED", milestone_id,
        f"{milestone_id}: a REPLAN proposed changing canonical authority. Refused — "
        f"the Factory executes the Roadmap and never rewrites it. "
        f"Rationale preserved: {str(rationale)[:200]}")


def _milestone_entry(authority, milestone_id: str) -> dict | None:
    for ms in (authority.roadmap or {}).get("milestones", []) or []:
        if ms.get("id") == milestone_id:
            return ms
    return None


def _frozen_contract_body(project_dir: Path, state: dict, milestone_id: str,
                          authority=None) -> dict:
    """The validation contract in force for this milestone, from disk."""
    ref = (state.get("validation_contracts", {}).get(milestone_id) or {}).get("contract_id", "")
    if not ref:
        return {}
    try:
        from .validation_contract import load_frozen_contract, verify_frozen_against_current
        contract = load_frozen_contract(project_dir, ref) or {}
    except Exception:
        return {}
    if authority is not None:
        try:
            if verify_frozen_against_current(contract, authority):
                return {}          # stale: authority moved since it was frozen
        except Exception:
            return {}
    return contract.get("body", {}) or {}


def _repair_command_cwd(project_dir: Path, contract_body: dict, authority=None) -> str:
    """Where the frozen commands run: the product repo when declared, else the project."""
    candidates = [contract_body.get("product_repo", "")]
    if authority is not None:
        candidates.append(str((authority.roadmap or {}).get("product_repo") or ""))
    for pr in candidates:
        if not pr:
            continue
        candidate = Path(pr) if Path(pr).is_absolute() else (_REPO_ROOT / pr)
        if candidate.exists():
            return str(candidate.resolve())
    return str(project_dir)


def _derive_repair_commands(project_dir: Path, state: dict, authority,
                            milestone_id: str) -> tuple[str, str, str, str, str]:
    """Three DISTINCT proofs for one quarantine, derived from authority only."""
    body = _frozen_contract_body(project_dir, state, milestone_id, authority)
    entry = _milestone_entry(authority, milestone_id) or {}
    reproduction = ((body.get("product_e2e") or {}).get("test_command", "")
                    or (entry.get("product_e2e") or {}).get("test_command", ""))
    regression = ""
    for step in (entry.get("execution", {}) or {}).get("steps", []) or []:
        cmd = (step.get("executor", {}) or {}).get("test_cmd", "")
        if cmd:
            regression = cmd
            break
    validation = (
        f"python3 {_REPO_ROOT / 'factory_v2' / 'verify_milestone_acceptance.py'} "
        f"--project-dir {project_dir} --milestone {milestone_id}"
    )
    cwd = _repair_command_cwd(project_dir, body, authority)
    derived = f"frozen_contract={body.get('contract_id', '') or 'none'};roadmap_milestone={milestone_id}"
    recipe = str(entry.get("repair_recipe", "") or "")
    return reproduction, regression, validation, cwd, derived, recipe


def _repair_allowed_scope(project_dir: Path, state: dict, milestone_id: str) -> list:
    """Paths a repair for this milestone is allowed to touch."""
    body = _frozen_contract_body(project_dir, state, milestone_id)
    scope = [a for a in (body.get("required_artifacts") or []) if a]
    return sorted(set(scope))


def _freeze_quarantine_contract(project_dir: Path, state: dict, authority,
                                qrec: dict) -> tuple[object, str]:
    """Attach a frozen RepairContract to a quarantine that has none."""
    from .safety_quarantine import freeze_repair_contract

    milestone_id = qrec.get("milestone_id", "") or (state.get("current_milestone") or "")
    if not milestone_id:
        return None, "quarantine names no milestone — cannot derive a repair contract"

    repro, regression, validation, cwd, derived, recipe = _derive_repair_commands(
        project_dir, state, authority, milestone_id)
    if not repro:
        return None, f"milestone {milestone_id} declares no product_e2e test_command"
    if not regression:
        return None, f"milestone {milestone_id} declares no executor test_cmd for regressions"
    distinct = {repro.strip(), regression.strip(), validation.strip()}
    if len(distinct) < 3:
        # A tautology would clear the quarantine without proving anything.
        return None, (
            f"milestone {milestone_id} does not define three distinct proofs "
            f"(reproduction/regression/validation collapse to {len(distinct)} command(s))"
        )
    pre_repair_sha = _git_head_sha(cwd)
    if not pre_repair_sha:
        return None, f"repair cwd {cwd} is not a git repository — no pre_repair_sha to pin"

    contract = freeze_repair_contract(
        project_dir,
        milestone_id=milestone_id,
        mission_id=qrec.get("mission_id", ""),
        reproduction_command=repro,
        regression_command=regression,
        validation_command=validation,
        command_cwd=cwd,
        pre_repair_sha=pre_repair_sha,
        authority_hashes=authority.identity.as_dict(),
        cause=qrec.get("cause", ""),
        attempt_id=qrec.get("attempt_id", ""),
        failure_fingerprint=qrec.get("failure_fingerprint", ""),
        allowed_scope=_repair_allowed_scope(project_dir, state, milestone_id),
        repair_command=recipe,
        derived_from=derived,
    )
    return contract, ""


def _quarantine_recovery(project_dir: Path, state: dict, qrec: dict, kind: str,
                         contract=None, parent_recovery_id: str = "",
                         underivable: bool = False,
                         underivable_reason: str = "") -> "RecoveryWork | None":
    """Create typed recovery work for a quarantine, exactly once per blocking condition."""
    from .escalation_ladder import (RecoveryWork, generate_diagnose_task,
                                    generate_replan_task, generate_safe_repair_task,
                                    write_recovery_work)

    milestone_id = qrec.get("milestone_id", "")
    fingerprint = qrec.get("failure_fingerprint", "") or qrec.get("cause", "")
    csig = getattr(contract, "contract_sha", "") if contract is not None else ""
    ikey = getattr(contract, "idempotency_key", "") if contract is not None else ""
    if not ikey:
        # No contract yet (the underivable path): key on the quarantine identity so the work is still bound to…
        ikey = hashlib.sha256(":".join([
            qrec.get("cause", ""), milestone_id, qrec.get("mission_id", ""),
            qrec.get("attempt_id", ""), fingerprint,
        ]).encode()).hexdigest()[:16]
    dedup_key = hashlib.sha256(
        f"{kind}:{milestone_id}:{csig}:{ikey}:{fingerprint}:{underivable}".encode()
    ).hexdigest()[:16]

    for existing in _all_recovery_work(project_dir):
        if existing.get("dedup_key") == dedup_key:
            return None  # already created for this exact blocking condition

    recovery_id = hashlib.sha256(
        f"{dedup_key}:{qrec.get('entered_at', '')}".encode()).hexdigest()[:16]
    recovery = RecoveryWork(
        recovery_id=recovery_id,
        kind=kind,
        parent_mission_id=qrec.get("mission_id", "") or f"{milestone_id}#quarantine",
        milestone_id=milestone_id,
        failure_fingerprint=(underivable_reason or fingerprint)[:300],
        escalation_stage=kind.lower(),
        attempt_count=0,
        created_at=_now(),
        engine="claude",
        identity_key=dedup_key,
        parent_recovery_id=parent_recovery_id,
        dedup_key=dedup_key,
        idempotency_key=ikey,
        underivable=underivable,
    )
    cdict = {}
    if contract is not None:
        from dataclasses import asdict as _asdict
        cdict = _asdict(contract)
        cdict["cause"] = qrec.get("cause", "")
    if kind == "SAFE_REPAIR":
        task_path = generate_safe_repair_task(project_dir, recovery, cdict)
    elif kind == "REPLAN":
        task_path = generate_replan_task(project_dir, recovery)
    else:
        task_path = generate_diagnose_task(project_dir, recovery)
    _set_task_file(task_path, recovery)
    write_recovery_work(project_dir, recovery)
    _event(state, f"QUARANTINE_{kind}_CREATED", milestone=milestone_id,
           recovery_id=recovery.recovery_id, dedup_key=dedup_key,
           idempotency_key=ikey, underivable=underivable,
           cause=qrec.get("cause", ""))
    return recovery


def _set_task_file(task_path: Path, recovery) -> None:
    """Persist an ABSOLUTE, normalised task file plus its authority root."""
    resolved = Path(task_path).resolve()
    recovery.task_file = str(resolved)
    recovery.authority_root = str(_REPO_ROOT.resolve())
    try:
        recovery.task_file_relative = str(resolved.relative_to(_REPO_ROOT.resolve()))
    except ValueError:
        recovery.task_file_relative = ""


def _is_ancestor(maybe_ancestor: str, descendant: str) -> bool:
    """True when `maybe_ancestor` is a real ancestor commit of `descendant`."""
    if not maybe_ancestor or not descendant:
        return False
    try:
        r = subprocess.run(["git", "-C", str(_REPO_ROOT), "merge-base",
                            "--is-ancestor", maybe_ancestor, descendant],
                           capture_output=True, timeout=15)
        return r.returncode == 0
    except Exception:
        return False


def _derive_repair_contract_from_incident(project_dir: Path, qrec: dict):
    """Freeze a reproduction from the incident's own milestone."""
    from .safety_quarantine import freeze_repair_contract
    mid = str(qrec.get("milestone_id") or "")
    if not mid:
        return None, "NO_DERIVABLE_REPRODUCTION: incident names no milestone"
    try:
        authority = load_authority(project_dir)
    except Exception as exc:
        return None, f"NO_DERIVABLE_REPRODUCTION: authority unreadable ({exc})"[:300]
    entry = next((m for m in (authority.roadmap.get("milestones") or [])
                  if m.get("id") == mid), None)
    if entry is None:
        return None, f"NO_DERIVABLE_REPRODUCTION: milestone {mid} not in authority"
    cmd = str(((entry.get("product_e2e") or {}).get("test_command")) or "")
    if not cmd:
        return None, f"NO_DERIVABLE_REPRODUCTION: {mid} declares no product_e2e"

    steps = ((entry.get("execution") or {}).get("steps") or [])
    cwd_hint = (steps[0].get("executor", {}).get("cwd") if steps else None) or ""
    product_repo = str(authority.roadmap.get("product_repo") or "")
    if cwd_hint == "control_repo" or not product_repo:
        cwd = str(_REPO_ROOT)
    else:
        cwd = product_repo

    from .escalation_ladder import control_code_sha as _ccs
    contract = freeze_repair_contract(
        project_dir, milestone_id=mid,
        mission_id=str(qrec.get("mission_id") or f"{mid}#gate"),
        reproduction_command=cmd, regression_command="", validation_command=cmd,
        command_cwd=cwd,
        pre_repair_sha=str(qrec.get("code_sha") or _ccs()),
        authority_hashes=authority.identity.as_dict(),
        cause=str(qrec.get("cause") or ""),
        failure_fingerprint=str(qrec.get("failure_fingerprint") or ""),
        derived_from="control-plane incident: milestone product_e2e")
    if contract is None:
        return None, "NO_DERIVABLE_REPRODUCTION: contract freeze refused"
    return contract, ""


def _supervisor_verified_fix(project_dir: Path, qrec: dict, chain: dict,
                             old_sha: str, new_sha: str):
    """Execute the frozen reproduction at both SHAs and return a sealed receipt.

    P0-1: no claimant-supplied document on this path — the old `_load_verified_fix_receipt()` glob is gone.
    """
    from .safety_quarantine import get_repair_contract, update_repair_chain
    from .verified_fix import (context_signature, load_referenced,
                               produce_verified_fix, store_receipt)

    contract = get_repair_contract(project_dir)
    if contract is None:
        # The production starting state: factory sat at MAX_ITERATIONS=11 with a terminal SHA and NO repair…
        contract, why = _derive_repair_contract_from_incident(project_dir, qrec)
        if contract is None:
            return None, why
    if contract is None:
        return None, ("NO_FROZEN_REPAIR_CONTRACT: a terminal episode re-opens only "
                      "on a reproduction the supervisor owns and can re-run")
    project_id = Path(project_dir).name
    fingerprint = str(qrec.get("failure_fingerprint", ""))
    ctx_sha = context_signature(chain, contract)
    bound = dict(fingerprint=fingerprint, contract_signature=contract.contract_sha,
                 context_manifest_sha=ctx_sha, old_code_sha=old_sha,
                 fix_code_sha=new_sha)

    # A proof already executed for this exact (incident, old SHA, fix SHA) is reused rather than re-run: the…
    existing = load_referenced(project_id, chain.get("verified_fix_ref") or {})
    if existing is not None and existing.is_valid() and not existing.closes(**bound):
        return existing, ""

    receipt, why = produce_verified_fix(
        contract=contract, old_code_sha=old_sha, fix_code_sha=new_sha,
        fingerprint=fingerprint, context_manifest_sha=ctx_sha)
    if receipt is None:
        return None, why
    ref = store_receipt(project_id, receipt)
    update_repair_chain(project_dir, verified_fix_ref=ref)
    bad = receipt.invalid_reason() or receipt.closes(**bound)
    if bad:
        # Say what was OBSERVED, not just the verdict: "FAIL" alone hides whether the reproduction never went red…
        return None, (f"{bad} — observed old_rc={receipt.old_rc}@{old_sha[:12]} "
                      f"new_rc={receipt.new_rc}@{new_sha[:12]}; "
                      f"receipt {receipt.receipt_id()} stored, not applicable")
    return receipt, ""


def _all_recovery_work(project_dir: Path) -> list[dict]:
    rdir = Path(project_dir) / "evidence" / "recovery"
    out: list[dict] = []
    if not rdir.exists():
        return out
    for f in sorted(rdir.glob("*.json")):
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        if isinstance(d, dict) and d.get("recovery_id"):
            out.append(d)
    return out


# R3: how many identical blocked cycles before the gap becomes dispatchable work.
GATE_BLOCKED_RECOVERY_THRESHOLD = 3


def _stamp_recovery_identity(project_dir: Path, recovery_id: str, key: str) -> None:
    """Bind an existing recovery record to a repair-contract identity it predates."""
    from .project_authority import atomic_write_json
    path = Path(project_dir) / "evidence" / "recovery" / f"{recovery_id}.json"
    try:
        d = json.loads(path.read_text())
    except Exception:
        return
    d["idempotency_key"] = key
    # This record was created by the validation-gap state machine before a quarantine contract existed.
    d["carried_from_validation_gap"] = True
    atomic_write_json(path, d)


def _escalate_completed_gap(project_dir: Path, state: dict, milestone_id: str,
                            gap: dict, failures: list) -> "CycleResult | None":
    """Continue a validation gap whose DIAGNOSE has completed."""
    from .safety_quarantine import enter_quarantine, is_in_quarantine

    rid = gap.get("recovery_id", "")
    done = any(w.get("recovery_id") == rid and w.get("completed")
               for w in _all_recovery_work(project_dir))
    if not done or gap.get("escalated_at"):
        return None
    if is_in_quarantine(project_dir):
        return None  # the repair loop already owns this project

    from .safety_quarantine import compute_idempotency_key

    cause = "VALIDATION_GAP_UNRESOLVED_AFTER_DIAGNOSE"
    mission_id = f"{milestone_id}#gate"
    fingerprint = gap.get("failure_fingerprint", "")[:300]
    gap["escalated_at"] = _now()
    enter_quarantine(project_dir, cause=cause, milestone_id=milestone_id,
                     mission_id=mission_id, failure_fingerprint=fingerprint)

    # Carry the diagnosis forward: stamp it with the identity the repair contract will have, so the repair…
    key = compute_idempotency_key(cause, milestone_id, mission_id, "", fingerprint)
    _stamp_recovery_identity(project_dir, rid, key)
    _event(state, "VALIDATION_GAP_ESCALATED_TO_REPAIR", milestone=milestone_id,
           recovery_id=rid, cycles=gap.get("cycles", 0), failures=failures[:5])
    return CycleResult("VALIDATION_GAP_ESCALATED", milestone_id,
                       f"{milestone_id}: DIAGNOSE {rid} completed but the gate is still "
                       "blocked — handing the gap to the quarantine repair loop, which "
                       "schedules SAFE_REPAIR and proves the fix at an exact SHA")


# X2/P1-7: bounded repeats of the same NOT_COMPILABLE before it becomes recovery work.
NOT_COMPILABLE_RECOVERY_THRESHOLD = 3


#: Bounded attempts PER STAGE before a typed terminal. PROVISIONAL_SAFETY_CAP.
#: Counted from durable recovery records that were actually written — never from
#: polls. An earlier version incremented a counter on every poll while _make()
#: silently refused to create anything (identical dedup_key), so the ladder
#: claimed three attempts when exactly one had ever existed.
DIAGNOSE_MAX_ATTEMPTS = 3
REPLAN_MAX_ATTEMPTS = 3


def _stage_records(project_dir: Path, gap_key: str, kind: str) -> list:
    """Durable records for one stage of one incident, oldest attempt first."""
    recs = [w for w in _all_recovery_work(project_dir)
            if w.get("idempotency_key") == gap_key and w.get("kind") == kind]
    return sorted(recs, key=lambda w: (int(w.get("stage_attempt") or 1),
                                       str(w.get("created_at") or ""),
                                       str(w.get("recovery_id") or "")))


def _stage_outcome(records: list) -> str:
    """PENDING | SUCCEEDED | FAILED | UNKNOWN | NONE — the LATEST attempt decides."""
    if not records:
        return "NONE"
    last = records[-1]
    if not last.get("completed"):
        return "PENDING"
    verdict = last.get("settlement_success")
    if verdict is True:
        return "SUCCEEDED"
    if verdict is False:
        return "FAILED"
    return "UNKNOWN"


def _not_compilable_edge(project_dir: Path, state: dict, milestone_id: str,
                         contract: dict, fingerprint: str) -> "CycleResult | None":
    """Give NOT_COMPILABLE an outgoing edge instead of a human instruction."""
    from .escalation_ladder import (RecoveryWork, generate_diagnose_task,
                                    generate_replan_task, write_recovery_work)

    # Keyed on milestone + reason only.
    csig = contract.get("contract_signature", "") or contract.get("contract_id", "")
    dedup_base = f"NOTCOMPILABLE:{milestone_id}:{fingerprint}"
    gaps = state.setdefault("validation_gaps", {})
    gap_key = hashlib.sha256(dedup_base.encode()).hexdigest()[:16]
    gap = gaps.setdefault(gap_key, {
        "milestone": milestone_id, "kind": "NOT_COMPILABLE",
        "contract_signature": csig, "failure_fingerprint": fingerprint,
        "cycles": 0, "recovery_id": "", "replan_id": "",
        "first_seen": _now(),
    })
    gap["cycles"] = int(gap.get("cycles", 0)) + 1
    gap["last_seen"] = _now()
    if gap["cycles"] < NOT_COMPILABLE_RECOVERY_THRESHOLD:
        return None

    def _make(kind: str, parent: str = "", attempt: int = 1) -> "RecoveryWork | None":
        # The attempt number is part of the identity.
        dedup_key = hashlib.sha256(
            f"{kind}:{dedup_base}:attempt{attempt}".encode()).hexdigest()[:16]
        for existing in _all_recovery_work(project_dir):
            if existing.get("dedup_key") == dedup_key:
                return None
        rid = hashlib.sha256(
            f"{dedup_key}:{gap['first_seen']}:{attempt}".encode()).hexdigest()[:16]
        rec = RecoveryWork(
            recovery_id=rid, kind=kind,
            parent_mission_id=f"{milestone_id}#compile",
            milestone_id=milestone_id, failure_fingerprint=fingerprint[:300],
            escalation_stage=kind.lower(), attempt_count=gap["cycles"],
            created_at=_now(),
            engine="claude", identity_key=dedup_key, dedup_key=dedup_key,
            parent_recovery_id=parent, idempotency_key=gap_key,
            stage_attempt=attempt,
        )
        gen = generate_replan_task if kind == "REPLAN" else generate_diagnose_task
        task_path = gen(project_dir, rec)
        _set_task_file(task_path, rec)
        write_recovery_work(project_dir, rec)
        return rec

    # ── Bounded, evidence-counted stage machine ──────────────────────────────
    # Every branch below must produce an OUTGOING EDGE: new work, or a typed
    # terminal. "Do not advance to the next stage" is not an outcome — it is how a
    # dead end looks from the inside, and it produced 1375 identical polls.
    #
    # Counts come from durable records, never from polls, and the LATEST attempt of
    # a stage decides its standing, so a later success overrides older failures.
    def _stage(kind: str, cap: int, parent: str = "") -> "tuple[str, CycleResult | None]":
        recs = _stage_records(project_dir, gap_key, kind)
        outcome = _stage_outcome(recs)
        if outcome == "SUCCEEDED":
            gap.pop(f"{kind.lower()}_terminal", None)   # a success clears the block
            return "SUCCEEDED", None
        if outcome == "PENDING":
            # An attempt exists and has not settled: it is the dispatcher's turn.
            return "PENDING", None
        if outcome == "UNKNOWN":
            # Settled with no verdict.
            _event(state, f"{kind}_SETTLED_WITHOUT_VERDICT", milestone=milestone_id,
                   recovery_id=str(recs[-1].get("recovery_id", ""))[:64],
                   attempts=len(recs))
        # NONE, FAILED or UNKNOWN — all need a new attempt, bounded by REAL records.
        made = len(recs)
        if made >= cap:
            if gap.get(f"{kind.lower()}_terminal"):
                return "TERMINAL", None      # already typed: stop re-emitting
            gap[f"{kind.lower()}_terminal"] = f"{kind}_EXHAUSTED"
            _event(state, f"MILESTONE_{kind}_EXHAUSTED", milestone=milestone_id,
                   attempts=made, cycles=gap["cycles"], reason=fingerprint[:200])
            return "TERMINAL", CycleResult(
                f"MILESTONE_{kind}_EXHAUSTED", milestone_id,
                f"{milestone_id}: {made} durable {kind} attempt(s) failed on "
                f"'{fingerprint[:110]}'. Typed terminal block — not a retry loop.")
        rec = _make(kind, parent=parent, attempt=made + 1)
        if rec is None:
            return "PENDING", None           # identical attempt already on disk
        if kind == "DIAGNOSE":
            gap["recovery_id"] = rec.recovery_id
        else:
            gap["replan_id"] = rec.recovery_id
        gap[f"{kind.lower()}_attempts"] = made + 1
        _event(state, f"NOT_COMPILABLE_{kind}_CREATED", milestone=milestone_id,
               recovery_id=rec.recovery_id, attempt=made + 1, cycles=gap["cycles"],
               reason=fingerprint[:200])
        return "CREATED", CycleResult(
            f"NOT_COMPILABLE_{kind}_SCHEDULED", milestone_id,
            f"{milestone_id}: {kind} attempt {made + 1}/{cap} scheduled "
            f"({rec.recovery_id}) — {fingerprint[:100]}")

    diag_state, diag_result = _stage("DIAGNOSE", DIAGNOSE_MAX_ATTEMPTS)
    if diag_state != "SUCCEEDED":
        return diag_result
    diagnosed = _stage_records(project_dir, gap_key, "DIAGNOSE")[-1].get("recovery_id", "")

    _, replan_result = _stage("REPLAN", REPLAN_MAX_ATTEMPTS, parent=diagnosed)
    return replan_result


def _record_validation_gap(project_dir: Path, state: dict, milestone_id: str,
                           contract: dict, verdict, reason: str) -> "CycleResult | None":
    """Turn a repeatedly blocked validation into typed, dispatchable recovery work."""
    from .escalation_ladder import (RecoveryWork, generate_diagnose_task,
                                    write_recovery_work)

    failures = list(getattr(verdict, "failures", []) or []) or [reason]
    fingerprint = json.dumps(failures, sort_keys=True)[:400]
    csig = contract.get("contract_signature", "") or contract.get("contract_id", "")
    dedup_key = hashlib.sha256(
        f"GAP:{milestone_id}:{csig}:{fingerprint}".encode()).hexdigest()[:16]

    gaps = state.setdefault("validation_gaps", {})
    gap = gaps.setdefault(dedup_key, {
        "milestone": milestone_id, "contract_signature": csig,
        "failure_fingerprint": fingerprint, "cycles": 0,
        "first_seen": _now(),
        "recovery_id": "",
    })
    gap["cycles"] = int(gap.get("cycles", 0)) + 1
    gap["last_seen"] = _now()
    gap["failures"] = failures[:10]

    if gap.get("recovery_id"):
        # P0-2: the gap already produced a DIAGNOSE.
        return _escalate_completed_gap(project_dir, state, milestone_id, gap, failures)
    if gap["cycles"] < GATE_BLOCKED_RECOVERY_THRESHOLD:
        return None
    for existing in _all_recovery_work(project_dir):
        if existing.get("dedup_key") == dedup_key:
            gap["recovery_id"] = existing.get("recovery_id", "")
            return None

    recovery_id = hashlib.sha256(f"{dedup_key}:{gap['first_seen']}".encode()).hexdigest()[:16]
    recovery = RecoveryWork(
        recovery_id=recovery_id,
        kind="DIAGNOSE",
        parent_mission_id=f"{milestone_id}#gate",
        milestone_id=milestone_id,
        failure_fingerprint=fingerprint,
        escalation_stage="diagnose",
        attempt_count=gap["cycles"],
        created_at=_now(),
        engine="claude",
        identity_key=dedup_key,
        dedup_key=dedup_key,
    )
    task_path = generate_diagnose_task(project_dir, recovery)
    _set_task_file(task_path, recovery)
    write_recovery_work(project_dir, recovery)
    gap["recovery_id"] = recovery_id
    _event(state, "VALIDATION_GAP_DIAGNOSE_CREATED", milestone=milestone_id,
           recovery_id=recovery_id, dedup_key=dedup_key, cycles=gap["cycles"],
           failures=failures[:5])
    return CycleResult("VALIDATION_GAP_DIAGNOSE_SCHEDULED", milestone_id,
                       f"{milestone_id}: gate blocked {gap['cycles']}x on the same failure "
                       f"— DIAGNOSE {recovery_id} scheduled ({failures[:2]})")


# C38: a failure list made up ENTIRELY of the product_e2e test_command's own exit code — no…
_TEST_COMMAND_FAILURE_RE = _re.compile(r"^product_e2e command rc=\d+$")


def _is_test_command_only_failure(failures: list) -> bool:
    fs = [f for f in (failures or []) if isinstance(f, str)]
    return bool(fs) and all(_TEST_COMMAND_FAILURE_RE.match(f) for f in fs)


def _gate_hold_identity_key(milestone_id: str, candidate_sha: str, contract: dict,
                            control_code_sha: str) -> str:
    """Identity of 'this exact candidate, under this exact contract, judged by this exact verifier code'."""
    csig = contract.get("contract_signature", "") or contract.get("contract_id", "")
    payload = json.dumps({
        "milestone_id": milestone_id, "candidate_sha": candidate_sha or "",
        "contract_signature": csig, "control_code_sha": control_code_sha or "",
    }, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _hold_gate_after_verdict(state: dict, milestone_id: str, contract: dict, verdict,
                             expected_head_sha: str, missions: list,
                             qrec: dict) -> CycleResult:
    """C38 — escalation is unavailable RIGHT NOW: a project holds at most one SAFETY_QUARANTINE.json, and it…"""
    from .blocked_event_dedup import should_emit
    from .launch_gate import current_code_sha
    ccs = current_code_sha()
    identity_key = _gate_hold_identity_key(milestone_id, expected_head_sha, contract, ccs)
    failures = list(getattr(verdict, "failures", []) or [])
    failures_fingerprint = json.dumps(sorted(str(f) for f in failures), sort_keys=True)[:400]
    held_by = qrec.get("milestone_id", "")

    record = state.setdefault("completed_milestones", {}).setdefault(milestone_id, {})
    record["gate_hold"] = {
        "identity_key": identity_key,
        "candidate_sha": expected_head_sha,
        "failures_fingerprint": failures_fingerprint,
        "held_by_quarantine": held_by,
        # UNBLOCK_SYNTH-002: exact identity of the causal quarantine, so the settlement pre-check can tell "same…
        "held_by_quarantine_entered_at": str(qrec.get("entered_at") or ""),
        "recorded_at": _now(),
    }

    if should_emit(state, f"gate_held:{milestone_id}:{identity_key}",
                   "MILESTONE_GATE_HELD",
                   {"candidate_sha": expected_head_sha, "failures": failures_fingerprint}):
        _event(state, "MILESTONE_GATE_HELD", milestone=milestone_id,
               candidate_sha=expected_head_sha, held_by_quarantine=held_by,
               reason="ESCALATION_UNAVAILABLE_QUARANTINE_HELD_ELSEWHERE",
               failures=failures[:5])

    _rebuild_eligible = _is_test_command_only_failure(failures)
    if _rebuild_eligible and should_emit(
            state, f"gate_rebuild:{milestone_id}:{identity_key}",
            "MISSION_REBUILD_AFTER_GATE_FAILURE", {"candidate_sha": expected_head_sha}):
        reset_ids = []
        missions_state = state.setdefault("missions", {})
        for m in missions:
            mrec = missions_state.get(m.id)
            if isinstance(mrec, dict) and mrec.get("state") == "PASS":
                mrec["state"] = "UNPROVEN"
                mrec["unproven_reason"] = (
                    f"MISSION_REBUILD_AFTER_GATE_FAILURE: gate held on candidate "
                    f"{expected_head_sha[:12]} — test_command failed with declared "
                    "deliverables present and escalation unavailable (quarantine "
                    f"held by {held_by!r}); resetting for a fresh build attempt")[:300]
                mrec["unproven_at"] = _now()
                mrec.pop("last_attempt_id", None)
                reset_ids.append(m.id)
        if reset_ids:
            _event(state, "MISSION_REBUILD_AFTER_GATE_FAILURE", milestone=milestone_id,
                   missions=reset_ids, candidate_sha=expected_head_sha,
                   held_by_quarantine=held_by)
        return CycleResult(
            "MISSION_REBUILD_AFTER_GATE_FAILURE", milestone_id,
            f"{milestone_id}: gate held on candidate {expected_head_sha[:12]} "
            "(test_command failed, deliverables present, escalation unavailable) "
            f"— {reset_ids} reset to UNPROVEN for a fresh build attempt")

    state["completed_milestones"][milestone_id]["state"] = "GATE_BLOCKED"
    return CycleResult(
        "MILESTONE_GATE_HELD", milestone_id,
        f"{milestone_id}: candidate {expected_head_sha[:12]} held — escalation "
        f"unavailable (quarantine held by {held_by!r}); no re-validation until "
        "candidate, contract or control code changes")


def _consume_product_intent_handoff(project_dir: Path, state: dict,
                                    authority) -> "CycleResult | None":
    """S017-04: act on a durable handoff BEFORE any superseded recovery can run."""
    from .durable_handoff import acknowledge_handoff, load_handoff

    h = load_handoff(project_dir)
    if h is None or h.get("acknowledged"):
        return None

    target = h.get("next_milestone", "")
    reason = h.get("handoff_reason", "")
    # Ordinary stop/restart handoffs are continuation records, not authority changes.
    if not str(reason).startswith("PRODUCT_INTENT_SUPERSEDED"):
        return None
    current_identity = authority.identity.as_dict()
    if h.get("supersession_refused_for") == current_identity:
        return None  # normal quarantine recovery remains reachable on the next cycle

    def refuse_supersession(detail):
        from .project_authority import atomic_write_json
        h["supersession_refused_for"] = current_identity
        h["supersession_refusal"] = detail
        atomic_write_json(project_dir / "DURABLE_HANDOFF.json", h)
        _event(state, "AUTHORITY_MIGRATION_UNPROVEN", milestone=target, reason=detail)
        save_project_state(project_dir, state)
        return CycleResult("AUTHORITY_MIGRATION_UNPROVEN", target, detail)

    # durable-handoff/v1 stores a fixed 16-hex digest; accept that exact legacy representation or the full…
    current_roadmap_sha = current_identity.get("roadmap_sha", "")
    if h.get("roadmap_sha") not in (current_roadmap_sha, current_roadmap_sha[:16]):
        return refuse_supersession("intent handoff does not identify the current roadmap")
    from .safety_quarantine import get_quarantine_record, get_repair_contract
    quarantine = get_quarantine_record(project_dir)
    if quarantine is not None:
        previous = quarantine.get("authority_hashes") or {}
        if not previous:
            repair = get_repair_contract(project_dir)
            if repair is not None and repair.verify_integrity():
                previous = repair.authority_hashes
        keys = ("strategy_sha", "spec_sha", "roadmap_sha")
        if (not all(isinstance(previous.get(k), str) and len(previous[k]) == 64
                    and set(previous[k]) <= set("0123456789abcdef") for k in keys)
                or all(previous[k] == current_identity.get(k) for k in keys)):
            return refuse_supersession(
                "quarantine has no observed superseded authority; "
                "preserving quarantine and repair history")
    known = {m.get("id") for m in (authority.roadmap or {}).get("milestones", []) or []}
    if target not in known:
        # The handoff names work this authority does not define.
        _event(state, "HANDOFF_TARGET_UNKNOWN", next_milestone=target,
               reason=str(reason)[:200])
        save_project_state(project_dir, state)
        return None

    _event(state, "OWNER_SUPERSEDED_BY_TR_ROADMAP", next_milestone=target,
           handoff_reason=str(reason)[:300],
           produced_at=h.get("produced_at", ""))

    reopened = _reopen_unprovable_settlements(project_dir, state, authority)
    retired = _retire_superseded_quarantine(project_dir, state, target)

    save_project_state(project_dir, state)
    acknowledge_handoff(project_dir)     # exactly once, after the transition is durable

    return CycleResult("OWNER_SUPERSEDED_BY_TR_ROADMAP", target,
                       f"product intent superseded ({reason[:80]}): reopened "
                       f"{len(reopened)} unprovable settlement(s), retired={retired}, "
                       f"resuming at {target}")


def _reopen_unprovable_settlements(project_dir: Path, state: dict, authority) -> list:
    """Reopen the settlements this supersession actually contradicts."""
    from .safety_quarantine import get_quarantine_record
    from .settlement_truth import verify_settlement

    qrec = get_quarantine_record(project_dir) or {}
    contradicted = qrec.get("milestone_id", "")
    if not contradicted:
        return []

    reopened = []
    completed = state.get("completed_milestones", {}) or {}
    for mid in [contradicted]:
        rec = completed.get(mid)
        if not isinstance(rec, dict) or rec.get("state") != "COMPLETE":
            continue
        v = verify_settlement(project_dir, mid, state=state, run_acceptance=False)
        if v.proven:
            continue
        rec["state"] = "UNPROVEN_AFTER_SUPERSESSION"
        rec["unproven_reason"] = "; ".join(v.reasons[:3])[:300]
        rec["unproven_at"] = _now()
        rec["historical_settled_at"] = rec.get("settled_at", "")
        for mission_id, mrec in (state.get("missions", {}) or {}).items():
            if mission_id.startswith(f"{mid}#") and isinstance(mrec, dict):
                if mrec.get("state") == "PASS":
                    mrec["state"] = "UNPROVEN"
                    mrec["unproven_reason"] = "settlement superseded and unprovable"
                    mrec.pop("last_attempt_id", None)
        reopened.append(mid)
        _event(state, "SETTLEMENT_REOPENED_AS_UNPROVEN", milestone=mid,
               reasons=v.reasons[:3])
    return reopened


def _retire_superseded_quarantine(project_dir: Path, state: dict, target: str) -> bool:
    """Retire a quarantine whose repair contract belongs to superseded authority."""
    from .project_authority import atomic_write_json
    from .safety_quarantine import (_QUARANTINE_FILENAME, get_quarantine_record,
                                    _durable_unlink)

    qrec = get_quarantine_record(project_dir)
    if qrec is None:
        return False

    retired = dict(qrec)
    retired["retired_at"] = _now()
    retired["retired_cause"] = "OWNER_SUPERSEDED_BY_TR_ROADMAP"
    retired["retired_superseded_by"] = target
    retired["retired_note"] = (
        "The product definition this quarantine was repairing was replaced by an "
        "owner-approved authority change. No repair was proven and none is claimed; "
        "this record is retained as evidence, not cleared as a success.")
    atomic_write_json(project_dir / "SUPERSEDED_QUARANTINE.json", retired)
    _durable_unlink(project_dir / _QUARANTINE_FILENAME)
    _event(state, "QUARANTINE_RETIRED_SUPERSEDED",
           cause=qrec.get("cause", ""), superseded_by=target)
    return True


def _quarantine_close_candidate_lost(project_dir: Path, state: dict, qrec: dict,
                                     contract) -> "CycleResult | None":
    """Close a quarantine whose frozen candidate object can never be reproduced."""
    if contract is None or not contract.pre_repair_sha or not contract.command_cwd:
        return None

    cause = qrec.get("cause", "")
    milestone_id = qrec.get("milestone_id", "")

    from .blocked_event_dedup import blocked_fingerprint as _close_refused_fp

    def _refuse(refusal_reason: str) -> CycleResult:
        # C14 established this pattern a few lines below (QUARANTINE_REPAIR_ DISPATCHED's "quarantine_dispatch:"…
        dedup_mission = f"quarantine_close_candidate_lost:{milestone_id or cause}"
        detail = {"reason": refusal_reason, "cause": cause, "milestone_id": milestone_id,
                  "contract_id": getattr(contract, "contract_id", "")}
        fp = _close_refused_fp(dedup_mission, "QUARANTINE_CLOSE_REFUSED", detail)
        dedup = state.setdefault("_quarantine_close_refused_dedup", {})
        if dedup.get(dedup_mission) != fp:
            dedup[dedup_mission] = fp
            _event(state, "QUARANTINE_CLOSE_REFUSED", cause=cause,
                   milestone=milestone_id, reason=refusal_reason,
                   contract_id=getattr(contract, "contract_id", ""))
            save_project_state(project_dir, state)
        return CycleResult(
            "QUARANTINE_CLOSE_REFUSED", milestone_id or None,
            f"SAFETY_QUARANTINE({cause}) NOT closed: {refusal_reason} — refusing to "
            "act on an untrustworthy repair contract; quarantine remains open, no "
            "state change")

    if not contract.verify_integrity():
        return _refuse("CONTRACT_INTEGRITY_FAILED: repair contract does not verify "
                       "(edited since freezing, or incomplete)")

    # CODEX_REVIEW_C13 finding 3 (HAUTE, round 2): `contract_val and qrec_val and ...` only compared the two…
    for field_name, qrec_key in (("milestone_id", "milestone_id"), ("cause", "cause"),
                                 ("mission_id", "mission_id"),
                                 ("attempt_id", "attempt_id")):
        contract_val = getattr(contract, field_name, "") or ""
        qrec_val = qrec.get(qrec_key, "") or ""
        if qrec_val and contract_val != qrec_val:
            return _refuse(
                f"CONTRACT_QUARANTINE_MISMATCH: {field_name} contract={contract_val!r} "
                f"quarantine={qrec_val!r}")

    if not _candidate_object_missing(Path(contract.command_cwd), contract.pre_repair_sha):
        return None

    from .project_authority import atomic_write_json
    from .safety_quarantine import _QUARANTINE_FILENAME, _durable_unlink

    closed = dict(qrec)
    closed["closed_at"] = _now()
    closed["closed_cause"] = "QUARANTINE_CLOSED_CANDIDATE_LOST"
    closed["closed_note"] = (
        f"candidate {contract.pre_repair_sha[:12]} no longer exists as a commit in "
        f"{contract.command_cwd} (mission worktree purged) — no DIAGNOSE or SAFE_REPAIR "
        "can reproduce a missing commit, so this repair loop can never resolve the "
        "quarantine it is in. Closed as evidence, not cleared as a success; the "
        "milestone reopens UNPROVEN for a fresh build.")
    atomic_write_json(project_dir / "QUARANTINE_CLOSED_CANDIDATE_LOST.json", closed)
    _durable_unlink(project_dir / _QUARANTINE_FILENAME)
    _event(state, "CANDIDATE_OBJECT_MISSING", cause=cause, milestone=milestone_id,
           candidate_sha=contract.pre_repair_sha, repo=str(contract.command_cwd))
    state = _reopen_interrupted_milestone(state, milestone_id, "CANDIDATE_OBJECT_LOST")
    save_project_state(project_dir, state)
    return CycleResult(
        "QUARANTINE_CLOSED_CANDIDATE_LOST", milestone_id or None,
        f"SAFETY_QUARANTINE({cause}) closed: candidate {contract.pre_repair_sha[:12]} "
        f"no longer exists in {contract.command_cwd} — milestone {milestone_id!r} "
        "reopened UNPROVEN for a fresh build; no repair was proven or claimed")


def _run_quarantine_cycle(project_dir: Path, state: dict, authority,
                          executor) -> CycleResult:
    """Quarantine is a repair loop, not a waiting room."""
    from .safety_quarantine import get_quarantine_record, get_repair_contract

    qrec = get_quarantine_record(project_dir) or {}
    cause = qrec.get("cause", "")
    qmid = qrec.get("milestone_id", "") or None

    # ── -1. The candidate this quarantine repairs may no longer exist. ────────────
    # Checked before spending another repair cycle (or another terminal-stall
    # accounting pass) on a commit no DIAGNOSE or SAFE_REPAIR can ever reproduce.
    # A no-op (returns None) until a RepairContract has been frozen — before that,
    # step 2 below is what derives pre_repair_sha in the first place.
    # ── -2. The gate BEFORE the ladder (20/09, nuit 19→20) ────────────────────────
    # A quarantine whose frozen proofs already pass at a HEAD past pre_repair_sha is
    # repaired, whatever the ladder's own status (pending, failed, STALLED): the gate
    # is the authority. Placed before the terminal-status intercept below, so a
    # chain stalled during the night is still lifted the poll after the fix lands.
    from .advance import quarantine_ladder as _ql
    _probed = _ql.probe_gate_first(project_dir, state, qrec, qmid, cause, authority)
    if _probed is not None:
        return _probed

    from .advance import quarantine_stall
    _stall = quarantine_stall.handle_stall_and_drift(
        project_dir, state, qrec, cause, qmid, authority)
    if isinstance(_stall, CycleResult):
        return _stall
    chain, status = _stall

    # ── 1. Pending repair work first — ALWAYS, contract or not. ─────────────────
    try:
        pending = read_pending_recovery_work(project_dir)
    except RecoveryLedgerUnknown as exc:
        # FCO_SYNTH-003: a corrupt recovery record must not read as "no repair pending".
        return CycleResult("RECOVERY_LEDGER_UNKNOWN", None,
                           f"quarantine cycle: recovery ledger unreadable: {exc}")
    if pending is not None:
        # C8: this ran unconditionally on EVERY poll of the same still-pending recovery, even when the launch…
        from .blocked_event_dedup import blocked_fingerprint as _dispatch_fp
        _dispatch_mid = f"quarantine_dispatch:{pending.recovery_id}"
        _dispatch_detail = {"recovery_kind": pending.kind, "cause": cause}
        _fp = _dispatch_fp(_dispatch_mid, "QUARANTINE_REPAIR_DISPATCHED", _dispatch_detail)
        _dedup = state.setdefault("_quarantine_dispatch_dedup", {})
        if _dedup.get(_dispatch_mid) != _fp:
            _dedup[_dispatch_mid] = _fp
            _event(state, "QUARANTINE_REPAIR_DISPATCHED",
                   recovery_id=pending.recovery_id, recovery_kind=pending.kind, cause=cause)
            save_project_state(project_dir, state)
        return _execute_recovery_work(project_dir, pending, state, qmid or "",
                                      authority, executor)

    # ── 2. Freeze the contract that defines "repaired" for this quarantine. ─────
    contract = get_repair_contract(project_dir)
    if contract is None or not contract.verify_integrity():
        contract, why = _freeze_quarantine_contract(project_dir, state, authority, qrec)
        if contract is None:
            return _quarantine_underivable_edge(project_dir, state, qrec, why, qmid)
        _event(state, "QUARANTINE_CONTRACT_FROZEN", cause=cause,
               contract_id=contract.contract_id, milestone=contract.milestone_id,
               pre_repair_sha=contract.pre_repair_sha[:12],
               idempotency_key=contract.idempotency_key,
               allowed_scope=contract.allowed_scope,
               derived_from=contract.derived_from)
        save_project_state(project_dir, state)

    # Audit-only: record what the reproduction did at pre_repair_sha the first time we see this contract.
    from .safety_quarantine import (capture_reproduction_baseline_at_sha,
                                    get_reproduction_baseline,
                                    record_reproduction_baseline)
    if get_reproduction_baseline(project_dir) is None:
        _audit_baseline, _why = capture_reproduction_baseline_at_sha(contract)
        if _audit_baseline is not None:
            record_reproduction_baseline(project_dir, _audit_baseline)
            _event(state, "QUARANTINE_BASELINE_CAPTURED", cause=cause,
                   result=_audit_baseline.result, exit_code=_audit_baseline.exit_code,
                   tree_sha=_audit_baseline.tree_sha[:12], authority="audit_only")
            save_project_state(project_dir, state)
            if _audit_baseline.result != "FAIL":
                # The frozen reproduction PASSES on the pre-repair tree, so it does not describe the failure this…
                return _quarantine_new_fingerprint_edge(
                    project_dir, state, qrec, contract, qmid,
                    fingerprint=(f"CONTRACT_DOES_NOT_REPRODUCE:"
                                 f"{contract.reproduction_command}"),
                    event="QUARANTINE_CONTRACT_DOES_NOT_REPRODUCE",
                    detail=(f"frozen reproduction {contract.reproduction_command!r} "
                            f"PASSES at pre_repair_sha "
                            f"{contract.pre_repair_sha[:12]} — the contract does not "
                            "describe this failure"))

    # Recovery work is matched by THIS contract's idempotency key, never by kind alone: an old DIAGNOSE from…
    from .advance import quarantine_ladder
    return quarantine_ladder.run_repair_ladder(
        project_dir, state, qrec, contract, qmid, cause, authority)


def _quarantine_new_fingerprint_edge(project_dir: Path, state: dict, qrec: dict,
                                     contract, qmid, *, fingerprint: str,
                                     event: str, detail: str) -> CycleResult:
    """Steer the repair chain once on a NEW fingerprint, then name the stall."""
    from .safety_quarantine import (REPAIR_STATUS_STALLED, get_repair_chain,
                                    update_repair_chain)

    cause = qrec.get("cause", "")
    # R2.4/R3 — the steer key is bound to the control-plane code that produced the failure.
    from .escalation_ladder import control_code_sha as _ccs
    _code = _ccs()
    fp_key = hashlib.sha256(
        (f"{fingerprint}|{_code}" if _code else fingerprint).encode()).hexdigest()[:16]
    chain = get_repair_chain(project_dir)
    seen = list(chain.get("fingerprints", []))

    _event(state, event, cause=cause, fingerprint=fingerprint[:200], detail=detail[:300])

    if fp_key in seen:
        reason = f"already steered once on {fingerprint[:120]!r} without a verified fix"
        update_repair_chain(project_dir, status=REPAIR_STATUS_STALLED, reason=reason,
                            fingerprints=seen, terminal_code_sha=_code)
        _event(state, "QUARANTINE_STALLED", cause=cause, reason=reason[:300])
        save_project_state(project_dir, state)
        return CycleResult("QUARANTINE_STALLED", qmid,
                           f"SAFETY_QUARANTINE({cause}): {detail} — {reason}")

    seen.append(fp_key)
    update_repair_chain(project_dir, fingerprints=seen)
    steer = dict(qrec)
    steer["failure_fingerprint"] = fingerprint[:300]
    created = _quarantine_recovery(project_dir, state, steer, "REPLAN", contract=contract)
    save_project_state(project_dir, state)
    return CycleResult("QUARANTINE_REPLAN_SCHEDULED" if created
                       else "QUARANTINE_REPAIR_PENDING", qmid,
                       f"SAFETY_QUARANTINE({cause}): {detail} — REPLAN "
                       f"{'scheduled' if created else 'already pending'} on a new "
                       "fingerprint")


def _quarantine_underivable_edge(project_dir: Path, state: dict, qrec: dict,
                                 why: str, qmid) -> CycleResult:
    """Authority cannot yet define three distinct proofs for this quarantine."""
    cause = qrec.get("cause", "")
    _event(state, "QUARANTINE_CONTRACT_UNDERIVABLE", cause=cause, reason=why)

    diagnosed = ""
    for w in _all_recovery_work(project_dir):
        if w.get("kind") == "DIAGNOSE" and w.get("completed") and w.get("underivable"):
            diagnosed = w.get("recovery_id", "")
            break
    has_diag = any(w.get("kind") == "DIAGNOSE" and w.get("underivable")
                   for w in _all_recovery_work(project_dir))
    has_replan = any(w.get("kind") == "REPLAN" and w.get("underivable")
                     for w in _all_recovery_work(project_dir))

    if not has_diag:
        created = _quarantine_recovery(project_dir, state, qrec, "DIAGNOSE",
                                       underivable=True, underivable_reason=why)
        save_project_state(project_dir, state)
        return CycleResult("QUARANTINE_DIAGNOSE_SCHEDULED", qmid,
                           f"SAFETY_QUARANTINE({cause}): {why} — DIAGNOSE "
                           f"{'scheduled' if created else 'already pending'}")
    if diagnosed and not has_replan:
        created = _quarantine_recovery(project_dir, state, qrec, "REPLAN",
                                       parent_recovery_id=diagnosed,
                                       underivable=True, underivable_reason=why)
        save_project_state(project_dir, state)
        return CycleResult("QUARANTINE_REPLAN_SCHEDULED", qmid,
                           f"SAFETY_QUARANTINE({cause}): {why} — REPLAN "
                           f"{'scheduled' if created else 'already pending'} to add the "
                           "missing proof to authority")
    # Both edges have been created.
    diag_done = any(w.get("kind") == "DIAGNOSE" and w.get("underivable")
                    and w.get("completed") for w in _all_recovery_work(project_dir))
    replan_done = any(w.get("kind") == "REPLAN" and w.get("underivable")
                      and w.get("completed") for w in _all_recovery_work(project_dir))
    if diag_done and replan_done:
        from .safety_quarantine import REPAIR_STATUS_EXTERNAL_BLOCK, update_repair_chain
        reason = (f"{why}; DIAGNOSE and REPLAN both completed without making the "
                  "contract derivable — authority still defines no independent proofs")
        update_repair_chain(project_dir, status=REPAIR_STATUS_EXTERNAL_BLOCK,
                            reason=reason)
        _event(state, "QUARANTINE_EXTERNAL_BLOCK", cause=cause, reason=reason[:300])
        save_project_state(project_dir, state)
        return CycleResult("QUARANTINE_EXTERNAL_BLOCK", qmid,
                           f"SAFETY_QUARANTINE({cause}): {reason}")

    save_project_state(project_dir, state)
    return CycleResult("QUARANTINE_REPAIR_PENDING", qmid,
                       f"SAFETY_QUARANTINE({cause}): {why} — repair chain in flight")


def _reopen_interrupted_milestone(state: dict, milestone_id: str, cause: str) -> dict:
    """Resume the EXACT interrupted milestone, not merely "the next open one"."""
    if not milestone_id:
        return state
    completed = state.get("completed_milestones", {})
    if milestone_id in completed:
        completed.pop(milestone_id, None)
        _event(state, "INTERRUPTED_MILESTONE_REOPENED", milestone=milestone_id, cause=cause)
    for mission_id, rec in list(state.get("missions", {}).items()):
        if mission_id.startswith(f"{milestone_id}#") and rec.get("state") == "PASS":
            rec["state"] = "UNPROVEN"
            rec["unproven_reason"] = f"quarantine cleared ({cause}) — must re-prove"
    state["current_milestone"] = milestone_id
    return state


def _git_head_sha(project_dir: Path) -> str:
    import subprocess as _sp
    try:
        r = _sp.run(["git", "rev-parse", "HEAD"], cwd=str(project_dir),
                    capture_output=True, text=True, timeout=15)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _candidate_repo_for_contract(project_dir: Path, contract: dict) -> Path:
    """The repo a settled receipt's candidate/integration SHA belongs to."""
    body = contract.get("body", contract) if isinstance(contract, dict) else {}
    product_repo_str = str((body or {}).get("product_repo", "") or "")
    if not product_repo_str:
        if Path(project_dir).resolve() == (_REPO_ROOT / "projects" / "factory").resolve():
            return _REPO_ROOT
        return Path(project_dir)
    pr = Path(product_repo_str)
    if not pr.is_absolute():
        pr = (_REPO_ROOT / pr).resolve()
    return pr if pr.exists() else Path(project_dir)


def _candidate_object_missing(repo: Path, sha: str) -> bool:
    """True only when `repo` is git-backed AND `sha` names no commit object in it."""
    import subprocess as _sp
    if not sha:
        return False
    try:
        if _sp.run(["git", "rev-parse", "--git-dir"], cwd=str(repo),
                   capture_output=True, timeout=15).returncode != 0:
            return False                       # not a git repo — nothing to purge
        r = _sp.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=str(repo),
                    capture_output=True, timeout=15)
        return r.returncode != 0
    except Exception:
        return False


# C29: the exact prefix authority_gate.IndependentValidator._validate_e2e_and_artifacts writes for every…
_MISSING_ARTIFACT_PREFIX = "missing artifact (WIRED=FAIL, EVIDENCE_ABSENT): "


def _candidate_deliverable_missing_paths(repo: Path, sha: str, failures: list) -> list:
    """required_artifacts the validator flagged missing that are ALSO absent from the candidate commit itself…"""
    import subprocess as _sp
    declared = [f[len(_MISSING_ARTIFACT_PREFIX):] for f in (failures or [])
                if f.startswith(_MISSING_ARTIFACT_PREFIX)]
    if not declared or not sha:
        return []
    try:
        if _sp.run(["git", "rev-parse", "--git-dir"], cwd=str(repo),
                   capture_output=True, timeout=15).returncode != 0:
            return []                           # not a git repo — cannot confirm
    except Exception:
        return []
    confirmed = []
    for rel_path in declared:
        try:
            r = _sp.run(["git", "cat-file", "-e", f"{sha}:{rel_path}"], cwd=str(repo),
                        capture_output=True, timeout=15)
        except Exception:
            continue                            # tooling failure — not confirmed
        if r.returncode != 0:
            confirmed.append(rel_path)
    return confirmed


def _resolve_promotion_target(contract: dict, staging_sha: str) -> tuple:
    """The repo and golden ref the staging SHA actually belongs to."""
    import subprocess as _sp

    body = contract.get("body", contract) if isinstance(contract, dict) else {}
    product_repo_str = str((body or {}).get("product_repo", "") or "")
    if not product_repo_str:
        return _REPO_ROOT, "refs/heads/main", ""       # control-repo project: unchanged

    pr = Path(product_repo_str)
    if not pr.is_absolute():
        pr = (_REPO_ROOT / product_repo_str).resolve()
    if not (pr / ".git").exists() and not pr.is_dir():
        return pr, "", f"PRODUCT_REPO_ABSENT: {pr}"
    try:
        if pr.resolve() == _REPO_ROOT.resolve():
            return _REPO_ROOT, "refs/heads/main", ""
    except OSError:
        pass

    def _git(*args):
        try:
            return _sp.run(["git", *args], cwd=str(pr), capture_output=True,
                           text=True, timeout=15)
        except (OSError, _sp.SubprocessError) as exc:
            return _sp.CompletedProcess(args, 1, "", str(exc))

    if _git("cat-file", "-e", f"{staging_sha}^{{commit}}").returncode != 0:
        return pr, "", (f"STAGING_SHA_NOT_IN_PRODUCT_REPO: {staging_sha[:12]} "
                        f"is not a commit in {pr}")

    head = _git("symbolic-ref", "--quiet", "--short", "HEAD")
    branch = head.stdout.strip() if head.returncode == 0 else ""
    if not branch:
        # Detached HEAD: there is no branch the executor could have merged into, so there is no golden ref to…
        return pr, "", (f"PRODUCT_HEAD_DETACHED: {pr} has no checked-out branch to "
                        "promote onto")
    return pr, f"refs/heads/{branch}", ""


def _promotion_moves_backwards(repo_dir, base_sha: str, staging_sha: str) -> str:
    """Empty string when promoting staging onto base moves golden forward (or not at all)."""
    import subprocess as _sp

    if not base_sha or base_sha == staging_sha:
        return ""
    try:
        r = _sp.run(["git", "merge-base", "--is-ancestor", base_sha, staging_sha],
                    cwd=str(repo_dir), capture_output=True, text=True, timeout=15)
    except (OSError, _sp.SubprocessError) as exc:
        return f"PROMOTION_ANCESTRY_UNOBSERVABLE: {exc}"
    if r.returncode == 0:
        return ""
    return (f"PROMOTION_NOT_FAST_FORWARD: golden {base_sha[:12]} is not an ancestor "
            f"of staging {staging_sha[:12]} — refusing to rewind golden")


def _staging_contained_in_golden(repo_dir, golden_sha: str, staging_sha: str) -> bool:
    """True when golden already contains staging (staging is an ancestor of golden): a later settlement moved…"""
    import subprocess as _sp
    if not golden_sha or not staging_sha:
        return False
    try:
        r = _sp.run(["git", "merge-base", "--is-ancestor", staging_sha, golden_sha],
                    cwd=str(repo_dir), capture_output=True, text=True, timeout=15)
    except (OSError, _sp.SubprocessError):
        return False
    return r.returncode == 0


class _AlreadyPromoted:
    """PromotionResult stand-in for the no-op case (golden already contains staging)."""
    status = "PROMOTED"
    failure_reason = ""

    def __init__(self, sha: str):
        self.promoted_sha = sha


def _prevalidated_product_head(project_dir: Path, contract: dict, mission) -> tuple[str, Path | None]:
    """Return the exact current product SHA when it already satisfies the contract."""
    executor = (getattr(mission, "executor", None) or {}) if mission is not None else {}
    if mission is not None and not (
            executor.get("type") == "autopilot"
            or (executor.get("type") == "local_command"
                and executor.get("capture_product_head")
                and executor.get("prevalidate_existing_product"))):
        return "", None
    body = contract.get("body") or {}
    raw_repo = str(body.get("product_repo") or "")
    if not raw_repo:
        # Factory builds itself in the control repository.
        if Path(project_dir).resolve() != (_REPO_ROOT / "projects" / "factory").resolve():
            return "", None
        repo = _REPO_ROOT
    else:
        repo = Path(raw_repo)
        if not repo.is_absolute():
            repo = (_REPO_ROOT / repo).resolve()
    if not repo.is_dir():
        return "", None
    try:
        observed = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True,
            text=True, timeout=15)
        sha = observed.stdout.strip() if observed.returncode == 0 else ""
        if len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha.lower()):
            return "", None
        from .launch_gate import current_code_sha
        verdict = IndependentValidator(
            project_dir, persist_evidence=False, timeout_s=3600,
            control_pin=current_code_sha()).validate(contract, integration_sha=sha)
        if not verdict.milestone_complete:
            return "", None
        # 2026-09-18: the STEP's own test_cmd must be green too before a model call is skipped — a repair step…
        _step_test = str((executor or {}).get("test_cmd") or "").strip()
        if _step_test:
            try:
                _st = subprocess.run(["bash", "-c", _step_test], cwd=str(repo),
                                     capture_output=True, text=True, timeout=3600,
                                     env={**os.environ, "FACTORY_CONTROL_REPO": str(Path(__file__).resolve().parents[1])})
            except Exception:
                return "", None
            if _st.returncode != 0:
                return "", None
        return sha, repo
    except Exception:
        return "", None


def _trusted_local_command_uses_no_model(mission) -> bool:
    """Recognize the small supervisor allowlist that cannot launch an AI. ``local_command`` alone is not…"""
    import re
    import shlex

    executor = getattr(mission, "executor", None) or {}
    command = str(executor.get("command") or "")
    if executor.get("type") != "local_command" or any(
            marker in command for marker in ("`", "$(", "\n", "\r")):
        return False
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        argv = list(lexer)
    except ValueError:
        return False
    if any(token in {";", "&", "&&", "|", "||", "<", ">", ">>", "<<"}
           for token in argv):
        return False
    while argv and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[0]):
        assignment = argv.pop(0)
        name, value = assignment.split("=", 1)
        if name != "PYTHONPATH" or not value.startswith("${FACTORY_CONTROL_REPO:?"):
            return False
    if len(argv) < 3 or Path(argv[0]).name not in {"python", "python3"} or argv[1] != "-m":
        return False
    return argv[2] in {
        "factory_v2.validation_contract",
        "factory_v2.product_verifiers",
    }


def _cnp_regrant_stranded_diagnose(project_dir: Path, state: dict, mid: str,
                                   contract: dict) -> "CycleResult | None":
    """R2C crash-window closer: re-grant the EXACT stranded DIAGNOSE, once."""
    from . import recovery_allocation
    from .escalation_ladder import read_pending_recovery_work
    from .launch_gate import current_code_sha, cost_no_progress_now

    if not cost_no_progress_now(project_dir)["tripped"]:
        return None
    sha = current_code_sha()
    alloc = recovery_allocation.available(
        project_dir, milestone=mid, control_code_sha=sha,
        expect_project=str(state.get("project") or ""))
    if alloc.get("allocation_id"):
        return None            # a usable grant exists — nothing is stranded
    try:
        pending = read_pending_recovery_work(project_dir)
    except Exception:
        return None
    if pending is None or pending.kind != "DIAGNOSE" \
            or pending.milestone_id != mid:
        return None
    contract_id = str(contract.get("contract_id") or "")
    mrec = (state.get("missions") or {}).get(pending.parent_mission_id) or {}
    fingerprint = str(mrec.get("last_failure_fingerprint") or "")[:200]
    expect_id, expect_key = recovery_allocation.supervisor_recovery_identity(
        mid, contract_id, fingerprint, sha)
    if not fingerprint or (
            str(pending.dedup_key or "") != expect_key
            and pending.recovery_id != expect_id):
        return None            # not the exact pending work for THIS condition
    try:
        grant = recovery_allocation.grant_supervisor_recovery(
            project_dir, milestone=mid, recovery_id=pending.recovery_id,
            failure_fingerprint=fingerprint,
            dedup_key=str(pending.dedup_key or expect_key),
            note=("CNP crash-window: the exact pending DIAGNOSE lost its "
                  "grant to a crash — re-issuing the same autonomous grant, "
                  "never a second work"))
        allocation_id = str(grant.get("allocation_id") or "")
    except recovery_allocation.AllocationRefused as exc:
        allocation_id = f"REFUSED:{exc.reason}"
    if allocation_id.startswith("REFUSED:"):
        # Deduplicated across identical polls (blocked_event_dedup): the FIRST observation of a given refusal…
        from .blocked_event_dedup import should_emit as _should_emit_refusal
        if _should_emit_refusal(
                state, f"cnp-regrant:{mid}", "CNP_RECOVERY_REGRANTED_REFUSED",
                {"recovery_id": pending.recovery_id,
                 "refusal": allocation_id}):
            _event(state, "CNP_RECOVERY_REGRANTED", milestone=mid,
                   recovery_id=pending.recovery_id,
                   dedup_key=str(pending.dedup_key or expect_key),
                   allocation_id=allocation_id)
            save_project_state(project_dir, state)
        return CycleResult(
            "CNP_RECOVERY_REGRANTED", mid,
            f"{mid}: pending DIAGNOSE {pending.recovery_id} exists without "
            f"its grant and the re-grant was refused ({allocation_id}) — "
            f"typed terminal condition, reported once")
    _event(state, "CNP_RECOVERY_REGRANTED", milestone=mid,
           recovery_id=pending.recovery_id,
           dedup_key=str(pending.dedup_key or expect_key),
           allocation_id=allocation_id)
    save_project_state(project_dir, state)
    return CycleResult(
        "CNP_RECOVERY_REGRANTED", mid,
        f"{mid}: pending DIAGNOSE {pending.recovery_id} existed without its "
        f"grant (crash between work write and grant write) — same autonomous "
        f"grant re-issued ({allocation_id})")


def _cnp_peek(project_dir: Path, state: dict, mid: str, contract: dict,
              authority, mission) -> "CycleResult | None":
    """R2C: cheap COST_NO_PROGRESS peek BEFORE any dispatch-side durable mutation."""
    from . import recovery_allocation
    from .blocked_event_dedup import should_emit
    from .launch_gate import current_code_sha, cost_no_progress_now

    cnp = cost_no_progress_now(project_dir)
    if not cnp["tripped"]:
        return None
    sha = current_code_sha()
    alloc = recovery_allocation.available(
        project_dir, milestone=mid, control_code_sha=sha,
        expect_project=str(state.get("project") or ""))
    if alloc.get("allocation_id"):
        if alloc.get("basis") == recovery_allocation.BASIS_OWNER:
            # C22: an active OWNER_AUTHORIZATION is an explicit owner decision to resume THIS milestone, not a claim…
            streak = cnp["consecutive_attempts_without_validated_delta"]
            # Dedup key deliberately excludes `streak`: under an active OWNER grant the streak climbs by one on every…
            dedup_detail = {"allocation_id": alloc["allocation_id"]}
            if should_emit(state, mission.id, "CNP_RESUMED_BY_OWNER", dedup_detail):
                _event(state, "CNP_RESUMED_BY_OWNER", milestone=mid,
                       mission=mission.id, allocation_id=alloc["allocation_id"],
                       streak=streak)
                save_project_state(project_dir, state)
        return None            # the gate at open_account re-validates and charges
    # Project-wide standing authorization is deliberately below a normal grant: the bounded…
    standing = recovery_allocation.available_standing(
        project_dir, expect_project=str(state.get("project") or ""))
    if standing.get("allocation_id"):
        recent = list((state.get("missions") or {}).get(mission.id, {}).get(
            "failure_fingerprint_history") or [])[-3:]
        if len(recent) == 3 and recent[0] and len(set(recent)) == 1:
            mrec = state.setdefault("missions", {}).setdefault(mission.id, {})
            # GLM counter-audit 2026-09-16 (BUG 3): `until` was recomputed from now() on every evaluation, so the…
            _existing = _waiting_until(mrec.get("parked_until"))
            if _existing is not None and datetime.now(timezone.utc) < _existing:
                until = mrec.get("parked_until")
            elif _existing is None:
                until = _iso(int(time.time()) + 6 * 3600)
            else:
                # Expiry is the park's exit condition.
                mrec.pop("parked_until", None)
                mrec.pop("parked_fingerprint", None)
                mrec.pop("parked_milestone", None)
                mrec.pop("state", None)
                mrec["failure_fingerprint_history"] = []
                save_project_state(project_dir, state)
                return None
            if mrec.get("parked_until") != until:
                mrec["state"] = "MILESTONE_PARKED"
                mrec["parked_until"] = until
                mrec["parked_fingerprint"] = recent[0]
                mrec["parked_milestone"] = mid
                _event(state, "MILESTONE_PARKED", milestone=mid, mission=mission.id,
                       fingerprint=recent[0], until=until)
                save_project_state(project_dir, state)
            return CycleResult("MILESTONE_PARKED", mid,
                               f"{mission.id}: same failure fingerprint three times; parked until {until}")
        from .blocked_event_dedup import should_emit as _standing_should_emit
        _standing_detail = {"allocation_id": standing["allocation_id"],
                           "used_today": standing["used_today"], "cap": standing["cap"]}
        if _standing_should_emit(state, mission.id, "STANDING_AUTHORIZATION_USED", _standing_detail):
            _event(state, "STANDING_AUTHORIZATION_USED", milestone=mid, mission=mission.id,
                   **_standing_detail)
            save_project_state(project_dir, state)
        return None
    if standing.get("standing_cap_reached"):
        from .blocked_event_dedup import should_emit as _standing_cap_should_emit
        _cap_detail = {"used_today": standing["used_today"], "cap": standing["cap"],
                       "resets_at": standing["resets_at"]}
        if _standing_cap_should_emit(state, mission.id, "STANDING_CAP_REACHED", _cap_detail):
            _event(state, "STANDING_CAP_REACHED", milestone=mid, mission=mission.id,
                   **_cap_detail)
            save_project_state(project_dir, state)
        return CycleResult("STANDING_CAP_REACHED", mid,
                           f"standing daily cap reached; waiting until {standing['resets_at']}")
    try:
        pending = read_pending_recovery_work(project_dir)
    except RecoveryLedgerUnknown as exc:
        # FCO_SYNTH-003: unreadable ledger is a typed refusal, not "nothing pending".
        return CycleResult("RECOVERY_LEDGER_UNKNOWN", mid,
                           f"{mid}: recovery ledger unreadable at CNP peek: {exc}")
    if pending is not None:
        # A stranded exact DIAGNOSE without its grant (crash between the work write and the grant write) is…
        return None        # pending work is dispatchable; never strand it

    contract_id = str(contract.get("contract_id") or "")
    mrec = (state.get("missions") or {}).get(mission.id) or {}
    fingerprint = (str(mrec.get("last_failure_fingerprint") or "")[:200]
                   or f"COST_NO_PROGRESS:{cnp['detail']}"[:200])
    # The identity names the EXACT commit: the same failure on different committed code is a different…
    recovery_id, dedup_key = recovery_allocation.supervisor_recovery_identity(
        mid, contract_id, fingerprint, sha)

    def _diagnose_exists() -> bool:
        rdir = Path(project_dir) / "evidence" / "recovery"
        if not rdir.is_dir():
            return False
        for p in sorted(rdir.glob("*.json")):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if d.get("kind") != "DIAGNOSE":
                continue
            if d.get("dedup_key") == dedup_key:
                return True
            # Ladder-era records carry no dedup key; the same condition is recognised by milestone + fingerprint (at…
            if (not d.get("dedup_key") and d.get("milestone_id") == mid
                    and (not d.get("control_code_sha")
                         or d.get("control_code_sha") == sha)
                    and str(d.get("failure_fingerprint") or "")[:200] == fingerprint):
                return True
        return False

    if _diagnose_exists():
        detail = {"streak": cnp["consecutive_attempts_without_validated_delta"],
                  "recovery": {"refusals": alloc.get("refusals") or [],
                               "detail": str(alloc.get("detail") or "")}}
        if should_emit(state, mission.id, "COST_NO_PROGRESS_TERMINAL", detail):
            _event(state, "COST_NO_PROGRESS_TERMINAL", milestone=mid,
                   mission=mission.id, streak=detail["streak"],
                   recovery=str(alloc.get("detail") or "")[:300])
            save_project_state(project_dir, state)
        return CycleResult(
            "COST_NO_PROGRESS_TERMINAL", mid,
            f"{mission.id}: COST_NO_PROGRESS tripped ({detail['streak']} attempts "
            f"without a validated delta) and the recovery credit bought no fix — "
            "only an independent settlement releases this brake")

    # P0 quota guard: a commit that moves the identity is not automatically a new question. cost_no_progress…
    guard = recovery_allocation.regrant_guard_decision(
        project_dir, milestone=mid, fingerprint=fingerprint,
        new_control_code_sha=sha)
    if not guard["allow"]:
        detail = {"streak": cnp["consecutive_attempts_without_validated_delta"],
                  "reason": guard["reason"], "control_code_sha": sha}
        if should_emit(state, mission.id, "CNP_REGRANT_WITHHELD", detail):
            _event(state, "CNP_REGRANT_WITHHELD", milestone=mid, mission=mission.id,
                   reason=guard["reason"], control_code_sha=sha,
                   streak=detail["streak"])
            save_project_state(project_dir, state)
        return CycleResult(
            "CNP_REGRANT_WITHHELD", mid,
            f"{mission.id}: COST_NO_PROGRESS tripped and a new commit moved the "
            f"recovery identity, but a fresh AI launch was withheld "
            f"({guard['reason']}) — the brake stays tripped, no launch spent")

    if guard.get("reason") == "owner_override":
        # C22: the daily cap would have withheld this regrant, but an active OWNER_AUTHORIZATION grant for this…
        streak = cnp["consecutive_attempts_without_validated_delta"]
        # Same exclusion as the direct-grant branch above: `streak` climbs on every poll this override keeps…
        dedup_detail = {"control_code_sha": sha}
        if should_emit(state, mission.id, "CNP_RESUMED_BY_OWNER", dedup_detail):
            _event(state, "CNP_RESUMED_BY_OWNER", milestone=mid, mission=mission.id,
                   control_code_sha=sha, streak=streak)
            save_project_state(project_dir, state)
        return None

    # ── the transition: exactly one deduped DIAGNOSE, at most one AI launch ──
    from .escalation_ladder import _identity_key as _ikey
    recovery = RecoveryWork(
        recovery_id=recovery_id, kind="DIAGNOSE",
        parent_mission_id=mission.id, milestone_id=mid,
        failure_fingerprint=fingerprint, escalation_stage="diagnose",
        attempt_count=int(cnp["consecutive_attempts_without_validated_delta"]),
        created_at=_now(),
        engine=str(mrec.get("last_engine") or ""),
        identity_key=_ikey(mission.id, str(mrec.get("last_engine") or ""),
                           authority.identity.strategy_sha, fingerprint,
                           contract_id, authority.identity.roadmap_sha),
        dedup_key=dedup_key)
    task_path = generate_diagnose_task(project_dir, recovery)
    _set_task_file(task_path, recovery)
    write_recovery_work(project_dir, recovery)
    try:
        grant = recovery_allocation.grant_supervisor_recovery(
            project_dir, milestone=mid, recovery_id=recovery_id,
            failure_fingerprint=fingerprint, dedup_key=dedup_key,
            note="COST_NO_PROGRESS transition: at most one AI diagnosis launch")
        allocation_id = str(grant.get("allocation_id") or "")
    except recovery_allocation.AllocationRefused as exc:
        allocation_id = f"REFUSED:{exc.reason}"
    _event(state, "CNP_RECOVERY_CREATED", milestone=mid, mission=mission.id,
           recovery_id=recovery_id, dedup_key=dedup_key,
           fingerprint=fingerprint[:64], allocation_id=allocation_id,
           streak=cnp["consecutive_attempts_without_validated_delta"])
    save_project_state(project_dir, state)
    return CycleResult(
        "CNP_RECOVERY_CREATED", mid,
        f"{mission.id}: product stopped by COST_NO_PROGRESS after "
        f"{cnp['consecutive_attempts_without_validated_delta']} unvalidated "
        f"attempts — DIAGNOSE {recovery_id} scheduled with at most one AI "
        f"launch (allocation {allocation_id})")


def _mission_pass_contradicted_by_lost_attempt(project_dir: Path, mission_rec: dict) -> bool:
    """C26: a mission_rec claiming PASS is trustworthy only while the accounting ledger's TERMINAL row for its…"""
    key = str(mission_rec.get("last_attempt_key") or "")
    if not key:
        return False
    try:
        from .launch_gate import _accounts as _ledger_accounts
        rows = {a.get("attempt_key"): a for a in _ledger_accounts(project_dir)
               if a.get("attempt_key")}
    except Exception:
        return False
    row = rows.get(key)
    if row is None or row.get("phase") != "TERMINAL":
        return False
    if row.get("mission_pass"):
        return False
    if str(row.get("exit_class") or "") not in ("KILL", "LOST"):
        return False
    return not str(row.get("candidate_sha") or "")


# R1: how long a second advance() waits for the project transaction before giving up.
ADVANCE_LOCK_TIMEOUT_S = 600.0


def _multi_ai_review_prompt(milestone_id: str, reviewed_sha: str,
                            contract_body: dict) -> str:
    """Bounded read-only review prompt, bound to the exact candidate SHA."""
    acceptance = [str(a) for a in (contract_body.get("acceptance") or [])][:10]
    # 2026-09-17: a reviewer given only the milestone's acceptance list judged the REVIEW PROCESS ("≥3…
    mai = contract_body.get("multi_ai_review") or {}
    brief = [str(b) for b in (mai.get("review_brief") or [])][:12]
    deliverables = [str(d) for d in (contract_body.get("required_artifacts") or [])][:12]
    parts = [
        "CTO read-only multi-AI review, bound to an exact commit — you may "
        "only read; any write/execute/subagent attempt is denied at the "
        "platform level and recorded.",
        f"Milestone: {milestone_id}",
        f"Exact SHA (your verdict MUST cite this exact value): {reviewed_sha}",
    ]
    if deliverables:
        parts.append("Deliverables to inspect at this commit (git show <sha>:<path>):\n- "
                     + "\n- ".join(deliverables))
    if brief:
        parts.append("What to check (judge the deliverables, not the review process itself):\n- "
                     + "\n- ".join(brief))
    else:
        parts.append("Frozen acceptance criteria:\n- "
                     + ("\n- ".join(acceptance) or "(none declared)"))
    parts.append(
        "Ignore any criterion about reviewers, seals or ledgers — those are "
        "produced by this very review and cannot be judged by you. Inspect the "
        "state at this exact commit and decide whether the deliverables hold "
        "up: claims backed by the code as it is, numbers reproducible, no "
        "fabricated citation.\n"
        "Verdict rule (proportionate — 19/09, after 4 rounds blocked on details): "
        "BLOCK only for a FABRICATED or unverifiable citation, a number that does "
        "not reproduce within 5 %, a claim about our code that is FALSE, or a "
        "required element that is MISSING. A small drift (a measure taken a few "
        "commits earlier, a file designated by its directory, wording, jargon) is "
        "an ACCEPT with your remarks listed in the reason — never a BLOCK. "
        "Reply on the LAST line with exactly `ACCEPT` or `BLOCK` followed by a "
        "one-line reason.")
    return "\n".join(parts)


def _compute_failure_fingerprint(result) -> str:
    """The G3 equivalent-retry fingerprint for a failed attempt."""
    if result.ok:
        return ""
    stages_holder = getattr(result, "receipt", None) or {}
    # 2026-09-14: a FACTORY-side refusal (capture refused, builder report missing, deliverable unchanged…
    if isinstance(stages_holder, dict):
        verdict = str(stages_holder.get("verdict") or "")
        if verdict and verdict not in ("ACCEPTED", "BLOCKED", "VIOLATED", "PARTIAL", "REFUSED", ""):
            reason = str(stages_holder.get("verdict_reason") or "")
            return f"{verdict}:{hashlib.sha256(reason.encode()).hexdigest()[:16]}"
    stages = stages_holder.get("stages", []) if isinstance(stages_holder, dict) else []
    last_test = next(
        (s for s in reversed(stages)
         if isinstance(s, dict) and str(s.get("step", "")).startswith("test_")
         and s.get("output_tail")),
        None)
    if last_test is None:
        return result.output_tail[-200:]
    engex = getattr(result, "engine_execution", None) or {}
    telemetry = engex.get("telemetry") if isinstance(engex, dict) else None
    terminal_reason = (str(telemetry.get("terminal_reason") or "")
                       if isinstance(telemetry, dict) else "")
    if not terminal_reason and isinstance(engex, dict):
        terminal_reason = str(engex.get("terminal_reason") or "")
    digest = hashlib.sha256(
        last_test["output_tail"].encode("utf-8", "replace")).hexdigest()[:16]
    return f"{terminal_reason or 'test_output'}:{digest}"


def _dispatcher_interruption(result) -> str:
    """"QUOTA_CUT" / "NETWORK_CUT" / "" — the typed cut dispatcher_v3 recorded on the LAST engine execution of…"""
    if getattr(result, "ok", False):
        return ""
    # 2026-09-18 (TR_DEEP_RESEARCH_3AI_V1#M3 ×3 diagnose loop): the dispatcher's reserved interruption exit…
    typed = _typed_cut_from_receipt(result)
    if typed:
        return typed
    # 2026-09-17: a dispatcher killed by a signal (daemon restart, pause, SIGTERM from the operator) is an…
    try:
        _rc = int(getattr(result, "rc", 0) or 0)
    except (TypeError, ValueError):
        _rc = 0
    if _rc < 0 or _rc in (137, 143):
        return "KILL"
    receipt = getattr(result, "receipt", None)
    if not isinstance(receipt, dict):
        return ""
    # the executor reports rc=1 for any non-ACCEPTED verdict; the signal that killed the ENGINE lives in the…
    for st in (receipt.get("stages") or []):
        if isinstance(st, dict):
            try:
                _src = int(st.get("rc")) if st.get("rc") is not None else 0
            except (TypeError, ValueError):
                _src = 0
            if _src < 0 or _src in (137, 143):
                return "KILL"
    return ""


def _typed_cut_from_receipt(result) -> str:
    """QUOTA_CUT / NETWORK_CUT recorded by dispatcher_v3 on the last engine execution, unless a mechanical…"""
    receipt = getattr(result, "receipt", None)
    if not isinstance(receipt, dict):
        return ""
    # Codex audit sc2 (2026-09-13): a cut must NEVER overrule a verdict a mechanical oracle produced.
    stages = receipt.get("stages") if isinstance(receipt.get("stages"), list) else []
    for st in stages:
        if (isinstance(st, dict) and str(st.get("step", "")).startswith("test_")
                and isinstance(st.get("rc"), int)):
            return ""
    executions = receipt.get("engine_executions")
    if not isinstance(executions, list) or not executions:
        return ""
    last = executions[-1]
    if not isinstance(last, dict):
        return ""
    cut = str(last.get("interruption") or "")
    if cut not in ("QUOTA_CUT", "NETWORK_CUT"):
        return ""
    return cut


def _dispatcher_refused_before_engine(result) -> bool:
    """True only for an evidenced dispatcher refusal before a model launch."""
    receipt = getattr(result, "receipt", None)
    if not isinstance(receipt, dict) or not receipt:
        return False
    executions = receipt.get("engine_executions")
    if not (isinstance(executions, list) and not executions):
        # Missing/malformed/non-empty engine_executions is not proof of no launch — fail closed, this attempt…
        return False
    if getattr(result, "engine_execution", None):
        return False  # an observed execution on the result is evidence of a launch
    # A well-formed, empty engine_executions list, no observed engine_execution, and no telemetry sidecar…
    return not getattr(result, "telemetry_observations", None)


def _task_file_structural_refusal(mission) -> dict | None:
    """Cheap, read-only structural check on an autopilot mission's task_file — entirely inside the supervisor…"""
    executor = getattr(mission, "executor", None) or {}
    if not isinstance(executor, dict) or executor.get("type") != "autopilot":
        return None
    task_file = str(executor.get("task_file") or "")
    if not task_file:
        return {"reason": "TASK_FILE_MISSING", "task_file": task_file,
                "detail": f"{mission.id}: autopilot mission declares no task_file"}
    if not Path(task_file).is_absolute():
        return {"reason": "TASK_FILE_RELATIVE", "task_file": task_file,
                "detail": (f"{mission.id}: task_file {task_file!r} is relative "
                          "(...). Cross-repo missions MUST use absolute paths "
                          "— dispatcher_v3.py's own preflight refuses this "
                          "with no receipt at all")}
    if not Path(task_file).is_file():
        return {"reason": "TASK_FILE_NOT_FOUND", "task_file": task_file,
                "detail": f"{mission.id}: task_file {task_file!r} does not "
                          "exist on disk"}
    return None


def _task_file_refusal_fingerprint(refusal: dict, authority) -> str:
    """Identity of a task_file refusal: the reason, the task_file's own bytes (when it exists) or its literal…"""
    from .launch_gate import current_code_sha
    task_file = str(refusal.get("task_file") or "")
    content_hash = ""
    if task_file:
        p = Path(task_file)
        if p.is_absolute() and p.is_file():
            try:
                content_hash = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
            except OSError as exc:
                content_hash = f"UNREADABLE:{exc}"[:64]
    payload = "|".join([
        str(refusal.get("reason") or ""), task_file, content_hash,
        str(getattr(getattr(authority, "identity", None), "roadmap_sha", "") or ""),
        current_code_sha(),
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _review_account_proven(account, project_dir) -> bool:
    """True iff `account` — either a `review_dispatch.ReviewAccount` instance JUST returned by a fresh…"""
    from . import review_dispatch as _rd
    row = asdict(account) if is_dataclass(account) else account
    return bool(_rd.verify_seal(row) and _rd.verify_supervised_launch(row, project_dir))


def advance(project_dir: Path, executor=None, *,
            lock_timeout_s: float = ADVANCE_LOCK_TIMEOUT_S) -> CycleResult:
    """One full loop step, serialized per project."""
    project_dir = Path(project_dir)
    try:
        with project_transaction(project_dir, timeout_s=lock_timeout_s):
            result = _advance_locked(project_dir, executor)
    except ProjectBusy as busy:
        # Typed, not an error: another supervisor owns this project right now.
        return CycleResult("PROJECT_BUSY", None, str(busy))

    # A1 (CTO DIRECTIVE 033 §3 R2) — the multi-AI cell runs HERE, with the project lock already RELEASED.
    drain_pending_audit_cell(project_dir)
    return result


def _quarantine_repair_lease_active(project_dir: Path, qrec: dict) -> bool:
    """C20 single writer guard."""
    qmid = qrec.get("milestone_id", "") or ""
    candidates = []
    _q_mission = qrec.get("mission_id", "")
    if _q_mission:
        candidates.append(_q_mission)
    if qmid:
        candidates.append(f"{qmid}#quarantine")
    for w in _all_recovery_work(project_dir):
        if (w.get("milestone_id") == qmid and not w.get("completed")
                and w.get("recovery_id")):
            candidates.append(f"recovery_{w['recovery_id']}")
    seen: set = set()
    for cand in candidates:
        if not cand or cand in seen:
            continue
        seen.add(cand)
        try:
            lease = WorkerLease._read_nolock(cand, project_dir)
        except Exception:
            lease = None
        if lease is not None and lease.state == "ACTIVE" and not lease.is_expired:
            return True
    return False


def _scope_independent_of_quarantine(authority, state: dict,
                                     qmid: str) -> tuple:
    """C20 — a safety quarantine freezes the MILESTONE it repairs, not the whole project."""
    milestones = [m for m in authority.roadmap.get("milestones", [])
                  if isinstance(m, dict) and m.get("id")]
    by_id = {m["id"]: m for m in milestones}
    completed = {mid for mid, rec in (state.get("completed_milestones") or {}).items()
                if isinstance(rec, dict) and rec.get("state") == "COMPLETE"}
    retired = {m["id"] for m in milestones if m.get("retired")}

    def _missing_deps(m: dict) -> list:
        missing = []
        for dep, req in (m.get("depends_on") or {}).items():
            required_state = ((req or {}).get("required_state", "COMPLETE")
                              if isinstance(req, dict) else str(req or "COMPLETE"))
            if required_state == "COMPLETE" and dep not in completed and dep not in retired:
                missing.append(dep)
        return missing

    _memo: dict = {}

    def _depends_on_q(mid: str, stack: frozenset = frozenset()) -> bool:
        # Structural (declared) dependency — "is qmid anywhere in M's dependency chain", not "is M blocked right…
        if mid == qmid:
            return True
        if mid in _memo:
            return _memo[mid]
        if mid in stack:
            return False  # a cycle elsewhere is not this function's problem
        deps = list((by_id.get(mid) or {}).get("depends_on") or {})
        result = any(_depends_on_q(dep, stack | {mid}) for dep in deps)
        _memo[mid] = result
        return result

    held_ids = []
    candidate = None
    for m in milestones:
        mid = m["id"]
        if mid == qmid or mid in completed or mid in retired:
            continue
        if _depends_on_q(mid):
            held_ids.append(mid)
            continue
        if candidate is None and not _missing_deps(m):
            candidate = mid
    return candidate, sorted(held_ids)


def _emit_milestones_held_by_quarantine(state: dict, held_ids: list, qmid: str,
                                        cause: str) -> None:
    from .blocked_event_dedup import should_emit
    for held_id in held_ids:
        key = f"milestone_held_by_quarantine:{held_id}:{qmid}"
        detail = {"quarantined_milestone": qmid, "cause": cause}
        if should_emit(state, key, "MILESTONE_HELD_BY_QUARANTINE", detail):
            _event(state, "MILESTONE_HELD_BY_QUARANTINE", milestone=held_id,
                   quarantined_milestone=qmid, cause=cause)


def _missing_dependencies_for_entry(entry: dict, state: dict, authority) -> list:
    """Dependencies `entry` (a single roadmap milestone dict) declares that are not currently COMPLETE nor…"""
    milestones = [m for m in authority.roadmap.get("milestones", [])
                  if isinstance(m, dict) and m.get("id")]
    retired = {m["id"] for m in milestones if m.get("retired")}
    completed = {mid for mid, rec in (state.get("completed_milestones") or {}).items()
                if isinstance(rec, dict) and rec.get("state") == "COMPLETE"}
    missing = []
    for dep, req in (entry.get("depends_on") or {}).items():
        required_state = ((req or {}).get("required_state", "COMPLETE")
                          if isinstance(req, dict) else str(req or "COMPLETE"))
        if required_state == "COMPLETE" and dep not in completed and dep not in retired:
            missing.append(dep)
    return sorted(missing)


def _backfill_roadmap_sha_at_record(project_dir: Path, state: dict) -> int:
    """C36 — a hash-bound verdict with no recorded authority hash is voided EVERY cycle, not just when the…"""
    from .authority_reconcile import HASH_BOUND_STATES
    backfilled = 0
    for rec in (state.get("completed_milestones") or {}).values():
        if not isinstance(rec, dict):
            continue
        if str(rec.get("state") or "") not in HASH_BOUND_STATES:
            continue
        if rec.get("roadmap_sha_at_record") or rec.get("authority_hash"):
            continue
        cid = rec.get("contract_id", "")
        if not cid:
            continue
        try:
            contract = load_frozen_contract(project_dir, cid)
        except ContractIntegrityError:
            continue  # unrecoverable — left exactly as it was, still unverifiable
        sha = str((contract.get("source") or {}).get("roadmap_sha") or "")
        if sha:
            rec["roadmap_sha_at_record"] = sha
            backfilled += 1
    return backfilled


def _emit_milestones_held_by_dependency(state: dict, authority) -> None:
    """R2.x — a milestone's `depends_on` can be RE-RESOLVED by a roadmap amendment after the milestone already…"""
    from .blocked_event_dedup import should_emit
    milestones = [m for m in authority.roadmap.get("milestones", [])
                  if isinstance(m, dict) and m.get("id")]
    completed_milestones = state.get("completed_milestones") or {}
    for entry in milestones:
        mid = entry["id"]
        rec = completed_milestones.get(mid)
        if not isinstance(rec, dict) or rec.get("state") == "COMPLETE":
            continue
        missing = _missing_dependencies_for_entry(entry, state, authority)
        if not missing:
            continue
        key = f"milestone_held_by_dependency:{mid}:{','.join(missing)}"
        detail = {"missing_dependencies": missing}
        if should_emit(state, key, "MILESTONE_HELD_BY_DEPENDENCY", detail):
            _event(state, "MILESTONE_HELD_BY_DEPENDENCY", milestone=mid,
                   missing_dependencies=missing)


def _advance_locked(project_dir: Path, executor=None) -> CycleResult:
    """Un pas complet de la boucle."""
    project_dir = Path(project_dir)

    # A6 (CTO DIRECTIVE 033 §3 R2) — apply a completed cell synthesis FIRST, under the lock, before any other…
    ctx = SimpleNamespace(project_dir=project_dir, executor=executor)
    from .advance import control_plane
    _cp_result = control_plane.run(ctx)
    if isinstance(_cp_result, CycleResult):
        return _cp_result
    authority, state = ctx.authority, ctx.state
    # C35: report (never gate) every milestone whose depends_on is unsatisfied under CURRENT authority but…
    ctx.state, ctx.authority, ctx.executor = state, authority, executor
    from .advance import milestone_selection
    _ms_result = milestone_selection.run(ctx)
    if isinstance(_ms_result, CycleResult):
        return _ms_result
    mid, entry, completed_rec = ctx.mid, ctx.entry, ctx.completed_rec
    _scoped_mid = ctx._scoped_mid

    from .advance import milestone_guards
    _mg_result = milestone_guards.run(ctx)
    if isinstance(_mg_result, CycleResult):
        return _mg_result

    from .advance import contract_and_compile
    _cc_result = contract_and_compile.run(ctx)
    if isinstance(_cc_result, CycleResult):
        return _cc_result
    contract, missions, completed_rec = ctx.contract, ctx.missions, ctx.completed_rec
    # A REPLAN's derived plan is consumed HERE — inside the one dispatch path, after the missions are…
    missions, _plan_note = _consume_derived_plan(
        project_dir, state, authority, mid, contract, missions)
    result = None  # set by runner.execute() in the loop; may remain None if all missions PASS
    _lease = None  # R6: only set when THIS cycle dispatched; never inherited silently
    ctx.state, ctx.authority, ctx.mid, ctx.entry = state, authority, mid, entry
    ctx.contract = contract
    ctx.result = None  # set by runner.execute() in the loop; may remain None if all missions PASS
    ctx._lease = None  # R6: only set when THIS cycle dispatched; never inherited silently
    from .advance import mission_dispatch
    for ctx.mission in missions:
        mission = ctx.mission
        _outcome = mission_dispatch.process_one_mission(ctx)
        if _outcome is mission_dispatch._CONTINUE:
            continue
        if isinstance(_outcome, CycleResult):
            return _outcome
    result, _lease = ctx.result, ctx._lease

    ctx.result, ctx._lease = result, _lease
    ctx.mid, ctx.entry, ctx.contract, ctx.authority, ctx.state = mid, entry, contract, authority, state
    ctx.missions = missions
    from .advance import settlement
    return settlement.run(ctx)


def run_until_stable(project_dir: Path, max_cycles: int = 10) -> list[CycleResult]:
    results = []
    for _ in range(max_cycles):
        result = advance(project_dir)
        results.append(result)
        if result.action in ("PROJECT_COMPLETE", "BLOCKED", "MISSION_FAILED",
                             "MILESTONE_GATE_BLOCKED", "MILESTONE_NOT_COMPILABLE",
                             "STALE_CONTEXT_DISPATCH_BLOCKED", "AUTHORITY_DRIFT_BLOCKED",
                             "MISSION_IN_FLIGHT"):
            break
    return results


def drain_pending_audit_cell(project_dir: Path) -> dict | None:
    """Execute one pending audit-cell request."""
    from .audit_cell_bridge import (
        pending_request, persist_audit_cell_result)
    req = pending_request(project_dir)
    if req is None:
        return None
    import functools
    from .audit_loop import (
        AuditTrigger, run_audit_cell, run_model_reviewer,
        record_cell_transition, BudgetExceeded, ProviderExhausted)
    from .single_flight import acquire, mark_running, settle
    control_root = Path(__file__).resolve().parent.parent

    # GLM counter-audit F5 (high): the drain ran with NO lock and NO claim, and pending_request returns the…
    project_identity = str(Path(project_dir).resolve())
    sf_identity = f"{project_identity}\x00{req['request_id']}"
    sf_key = f"audit-cell-{hashlib.sha256(sf_identity.encode()).hexdigest()[:24]}"
    slot = acquire(control_root, sf_key,
                   command=["audit-cell", req["request_id"]],
                   cwd=str(project_dir), envelope_s=3600.0,
                   owner_note=f"cell for {req.get('milestone_id')}")
    if not slot.get("owned"):
        return None            # another supervisor owns this activation; do not duplicate
    mark_running(control_root, sf_key, os.getpid())
    pos = req.get("position") or {}
    repo = Path(pos.get("product_repo") or project_dir)
    reviewer_fn = functools.partial(
        run_model_reviewer,
        repo=repo,
        spec_level=str(pos.get("spec_level") or "B"),
        project=Path(project_dir).name,
    )
    try:
        cell = run_audit_cell(
            trigger=AuditTrigger.REPEATED_EQUIVALENT_FAILURE,
            milestone_id=req["milestone_id"],
            project_dir=Path(project_dir),
            repo=repo,
            control_root=control_root,
            candidate_sha=pos.get("candidate_sha", ""),
            gate_failed=True,
            failure_fingerprint=req.get("failure_fingerprint", ""),
            contract_id=pos.get("contract_id", ""),
            reviewer_fn=reviewer_fn,
        )
        synthesis = cell.get("synthesis") or {}
        rounds = int(cell.get("rounds") or 0)
        roles = cell.get("role_identities") or []
        verdict = (((cell.get("reviewer_rounds") or [{}])[-1]
                    .get("verdict") or {}).get("verdict"))
        transition_kind = {
            "REJECT": "REVIEWER_REJECT",
            "REDIRECT": "REVIEWER_REDIRECT",
            "QUARANTINE": "REVIEWER_QUARANTINE",
        }.get(verdict)
        if transition_kind:
            record_cell_transition(
                control_root, kind=transition_kind,
                detail=f"model reviewer verdict {verdict}",
                action=str(synthesis.get("routed_to") or "WRITER"),
                recovered=False, manual=False)
    except Exception as exc:                       # noqa: BLE001 - typed, never raised
        transition_kind = (
            "BUDGET_EXCEEDED" if isinstance(exc, BudgetExceeded) else
            "PROVIDER_EXHAUSTED" if isinstance(exc, ProviderExhausted) else
            "ROLE_CRASH")
        record_cell_transition(
            control_root, kind=transition_kind,
            detail=f"{type(exc).__name__}: {exc}"[:300],
            action="BOUNDED_BACKOFF_THEN_RESUME", recovered=False,
            manual=False)
        synthesis = {"decision": "CELL_FAILED", "detail": f"{type(exc).__name__}: {exc}"[:300]}
        rounds, roles = 0, []
    persist_audit_cell_result(project_dir, request=req, synthesis=synthesis,
                              rounds=rounds, role_identities=roles)
    settle(control_root, sf_key, rc=0, duration_s=0.0,
           classified="PASS" if synthesis.get("decision") != "CELL_FAILED" else "FAIL",
           generation=(slot.get("job") or {}).get("generation"))
    return req


def _apply_pending_audit_cell_result(project_dir: Path) -> "CycleResult | None":
    """A6: turn a completed cell synthesis into a writer redirection + exact resume."""
    from .audit_cell_bridge import unapplied_result, mark_result_applied
    rec = unapplied_result(project_dir)
    if rec is None:
        return None
    rid = rec.get("request_id", "")
    synthesis = rec.get("synthesis") or {}
    decision = str(synthesis.get("decision") or "UNKNOWN")
    mid = rec.get("milestone_id")
    # Prefer the position carried on the result; fall back to the request for records written before results…
    position = rec.get("position") or {}
    if not position:
        from .audit_cell_bridge import cell_dir
        req_path = cell_dir(project_dir) / f"request_{rid}.json"
        if req_path.is_file():
            try:
                position = (json.loads(req_path.read_text(encoding="utf-8"))
                            .get("position") or {})
            except Exception:                      # noqa: BLE001
                position = {}

    if decision == "CELL_FAILED":
        mark_result_applied(project_dir, rid)
        return CycleResult(
            "AUDIT_CELL_FAILED", mid,
            f"cell {rid} produced no usable synthesis "
            f"({str(synthesis.get('detail'))[:160]}); position preserved, "
            f"normal recovery continues on the next cycle")

    # GLM counter-audit F2 (critical): this used to mark the result applied and RETURN A STRING claiming…
    mission_id = str(position.get("mission_id") or "")
    if not mission_id:
        mark_result_applied(project_dir, rid)
        return CycleResult(
            "AUDIT_CELL_POSITION_UNUSABLE", mid,
            f"cell {rid} decision={decision} but the recorded position names no "
            f"mission — nothing can be resumed, so no redirection is claimed")

    state = load_project_state(project_dir)
    redirects = state.setdefault("audit_cell_redirections", {})
    redirects[rid] = {
        "milestone_id": mid,
        "mission_id": mission_id,
        "decision": decision,
        "resume_position": position,
        "applied_at": _now(),
    }
    _event(state, "AUDIT_CELL_SYNTHESIS_APPLIED", milestone=mid,
           reason=f"{decision} -> resume {mission_id}")
    save_project_state(project_dir, state)
    mark_result_applied(project_dir, rid)

    return CycleResult(
        "AUDIT_CELL_SYNTHESIS_APPLIED", mid,
        f"cell {rid} decision={decision}: redirection recorded in state for "
        f"mission {mission_id}; the next dispatch resumes from that position")
