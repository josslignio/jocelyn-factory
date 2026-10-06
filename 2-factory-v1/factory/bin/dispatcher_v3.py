#!/usr/bin/env python3
"""dispatcher-v3 — noyau minimal : un worktree isolé, UN moteur par étape, capture Factory, oracle mécanique.

Boucle (build) : T0 build → T1 test → T2 repair (stall=1) → test → T3 escalade (stall=2) → test ; diagnose/replan :
un seul lancement en lecture seule. Jamais de verdict par LLM (seul le rc de test-cmd décide) ; repair ne réécrit
jamais l'outcome déclaré ; tout est écrit atomiquement ; un moteur mort = MOTOR_UNAVAILABLE tracé, jamais un
ACCEPTED fabriqué. Routage : model_router (P0-A). Budget : budget_ledger (P0-B), usage mesuré par la télémétrie.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "lib"))
sys.path.insert(0, str(HERE.parent.parent))   # launched directly: the repo root is not on sys.path yet

from mission_state import MissionState  # noqa: E402
import model_router as _mr  # noqa: E402
from budget_ledger import BudgetLedger, BudgetExceeded  # noqa: E402
import snapshot_diff as _sd  # noqa: E402
from verdict_taxonomy import (classify_blocked_reason, VIOLATED, SPEC_TEST_CONFLICT,  # noqa: E402
                              detect_spec_test_conflict, _has_pytest_markers, real_pytest_report)
from worktree_manager import WorktreeManager  # noqa: E402
from heartbeat import write_heartbeat  # noqa: E402
from factory_v2._io import _now  # noqa: E402
from factory_v2 import disk_guard as _disk_guard  # noqa: E402  FACTORY_REPO_HYGIENE_V1: DISK_SPACE_LOW before worktree add
from factory_v2 import repo_hygiene as _hyg  # noqa: E402  FACTORY_REPO_HYGIENE_V1: checkpoints/bookkeeping out of golden
import engine_output as _eo  # noqa: E402  F4 policy modules: engine output, context gate, prompts
from dispatcher_prompts import (BUILDER_PROMPT, RESUME_PREAMBLE, REVIEW_BLOCK_PREAMBLE, DIAGNOSE_PROMPT,  # noqa: E402
                                REPLAN_PROMPT, REPAIR_PROMPT, T3_REPAIR_PROMPT)
from context_gate import prepare_context_package as _prepare_context_package  # noqa: E402
from telemetry_sidecar import (now_iso as _now_iso, telemetry_sidecar_path, read_telemetry_sidecar,  # noqa: E402
                               append_telemetry_sidecar as _append_telemetry_sidecar,
                               refresh_cli_quota_snapshot as _refresh_cli_quota_snapshot,
                               mark_provider_exhausted as _mark_provider_exhausted)
_engine_text, _extract_resumable_session = _eo.engine_text, _eo.extract_resumable_session
_stream_truncation_verdict, _validate_recovery_artifact = _eo.stream_truncation_verdict, _eo.validate_recovery_artifact

# Observers (telemetry, quota, resilience, engine registry) are guarded imports: an observer that can break
# the launch it observes is worse than no observer at all.
try:
    from factory_v2.telemetry import observe as _telemetry  # noqa: E402
    from factory_v2.telemetry import adapters as _tel_adapters  # noqa: E402
    from factory_v2.telemetry import capture as _tel_capture  # noqa: E402
    from factory_v2 import resilience as _resilience  # noqa: E402
    _TELEMETRY_IMPORT_ERROR = ""
except Exception as _tel_exc:  # pragma: no cover
    _telemetry = _tel_adapters = _tel_capture = _resilience = None
    _TELEMETRY_IMPORT_ERROR = f"{type(_tel_exc).__name__}: {_tel_exc}"
try:
    from factory_v2 import routing_evidence as _routing_evidence  # noqa: E402
except Exception:  # pragma: no cover
    _routing_evidence = None
try:
    from factory_v2 import engine_registry as _engine_registry  # noqa: E402
except Exception:  # pragma: no cover
    _engine_registry = None


def route_for_dispatch(spec_level: str, task_kind: str, project_dir: "Path | str | None" = None,
                       **kw) -> "_mr.EngineChoice":
    """UNIQUE routing entry (ADAPTIVE_ROUTING 2026-09-13): the project's validated-attempt evidence is handed to
    model_router.route(); no project_dir → evidence={} (routing byte-identical to route() alone)."""
    evidence = {}
    if project_dir is not None and _routing_evidence is not None:
        try:
            evidence = _routing_evidence.evidence_from_ledger(project_dir, spec_level, task_kind)
        except Exception:
            evidence = {}
    return _mr.route(spec_level, task_kind, evidence=evidence, **kw)


SCHEMA = "factory-dispatch-v3/v2"
PROTOCOL_V4 = Path(__file__).resolve().parents[2] / "campaigns" / "RUN8" / "FACTORY_ARM_PROTOCOL.md"
WORKTREES_BASE = Path("/tmp/factory-worktrees")                    # F003: outside the repo, one worktree per mission
FACTORY_ROOT_COMMIT = "d85e8e955a9fc940b74f3edcbe0968634131d689"   # the control repo's root commit (never moves)
USAGE_UNKNOWN = -1                                                 # provider gave no token counts — never a zero
ENGINE_REFUSED_RC = -3                                             # refused before execve (FACTORY_NO_REAL_ENGINE=1)
QUOTA_REFUSED_RC = -6                                              # refused before execve (no projected quota headroom)
T3_REPAIR_MAX_TEST_OUTPUT, T3_REPAIR_MAX_DIFF_STAT, T3_REPAIR_MAX_FILES = 3000, 1000, 12
_BUILD_STEPS = ("build_glm", "test_1", "repair_glm", "test_2", "build_claude", "test_3")
_LINEAGE_BY_STEP = {"build_glm": "PRIMARY", "build_claude": "REPAIR", "repair_glm": "REPAIR",
                    "test_1": "TEST", "test_2": "TEST", "test_3": "TEST"}

_FAILING_FILE_PATTERNS = (   # pytest FAILED/ERROR node ids, short-summary markers, traceback File lines
    re.compile(r"^(?:FAILED|ERROR)\s+(\S+?\.py)(?:::|\s|$)", re.M),
    re.compile(r"^--+\s+(\S+?\.py):\d+\s+--+", re.M),
    re.compile(r'^File "([^"]+\.py)"', re.M),
)


def _extract_failing_files(*outputs, limit: int = T3_REPAIR_MAX_FILES) -> list:
    """Bounded, deduped file paths mined from failing test outputs (empty is stated as such, never guessed)."""
    seen, files = set(), []
    for output in outputs:
        for pattern in _FAILING_FILE_PATTERNS:
            for m in pattern.finditer(output or ""):
                path = (m.group(1) if m.groups() else "").strip()
                if path and path not in seen:
                    seen.add(path)
                    files.append(path)
    return files[:limit]


def build_t3_repair_prompt(*, mission_id: str, base_sha: str, head_sha: str, test_cmd: "str | None",
                           test_output: str, diff_stat: str, failing_files: list, budget_s: int) -> str:
    """Pure function: the bounded T3 prompt from OBSERVED stage data only (every section tail-truncated/capped)."""
    return T3_REPAIR_PROMPT.format(
        mission_id=mission_id, base_sha=(base_sha or "")[:12], head_sha=(head_sha or "")[:12],
        incident=("build T0 exécuté, test_1 rouge, réparation T2 exécutée, "
                  "test_2 encore rouge — conflit spéc/test écarté par FD4"),
        test_cmd=(test_cmd or "").strip() or "(aucune commande de test)",
        test_output=(test_output or "")[-T3_REPAIR_MAX_TEST_OUTPUT:],
        diff_stat=(diff_stat or "(aucun diff)")[-T3_REPAIR_MAX_DIFF_STAT:],
        failing_files=("\n".join(failing_files[:T3_REPAIR_MAX_FILES]) if failing_files
                       else "(non identifiés dans la sortie des tests)"),
        budget_s=budget_s)


def _resume_preamble(repo: Path, mission_id: str, base_sha: str = "") -> str:
    """REPRISE block, ONLY when a durable cut marker of THIS mission at THIS base exists AND the tree has WIP."""
    if _resilience is None:
        return ""
    marker = _resilience.consume_interruption_marker(repo, mission_id, base_sha)
    if not marker:
        return ""
    try:
        rc, status, _ = _git_plumbing(repo, "git", "status", "--porcelain", "--untracked-files=all",
                                       "--", ".", ":!.factory", ":!.factory/**")
        if rc != 0 or not status.strip():
            return ""
        files = [line[3:].strip() for line in status.splitlines() if len(line) >= 4]
        ck = _resilience.checkpoint_summary(repo, mission_id)
        note = f" — dernier checkpoint {ck['sha'][:12]} à {ck.get('committed_at', '?')}" if ck else ""
        if marker.get("attempt") is not None:
            note += (f" — coupure {marker.get('cut', '?')} de la tentative {marker.get('attempt')}"
                     f" (checkpoint au moment de la coupure : {(marker.get('checkpoint') or 'aucun')[:12]})")
        return RESUME_PREAMBLE.format(file_count=len(files), checkpoint_note=note,
                                      files=", ".join(files[:40]) + (" …" if len(files) > 40 else ""))
    except Exception:
        return ""


# ------------------------------------------------------------------ execve boundary


def real_engine_disabled() -> bool:
    return os.environ.get("FACTORY_NO_REAL_ENGINE") == "1"


def _refuse_engine(cmd: list[str], step_name: str = "") -> "dict | None":
    """§F1 — the single decision point before any engine execve: None = permitted, else a typed refusal record."""
    if not real_engine_disabled():
        return None
    return {"refused": True, "refusal": "REAL_ENGINE_DISABLED", "rc": ENGINE_REFUSED_RC, "step": step_name,
            "argv": [str(c) for c in cmd][:12],
            "detail": f"FACTORY_NO_REAL_ENGINE=1 — refused before execve{' at ' + step_name if step_name else ''}"}


def _is_engine_cmd(cmd: list) -> bool:
    """Would this argv start a real CLI model? Structure, not substring."""
    return bool(cmd) and Path(str(cmd[0])).name in ("claude", "opencode", "codex", "glm", "zcode")


def _killpg(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass


#: FACTORY_TOKEN_TRUTH_V1 — the raw-stream file of the engine launch in flight (set by _run_engine around its execve).
_STREAM_SINK: list = [None]


def _pump_to_sink(proc, sink: "Path | None", timeout: int) -> tuple:
    """Drain stdout/stderr on threads; every stdout line is appended to `sink` the instant it arrives, so a kill
    mid-launch leaves on disk everything the engine had emitted. → (rc, stdout, stderr, timed_out)."""
    import threading
    chunks: dict = {"out": [], "err": []}

    def _pump(pipe, key, fh):
        for line in iter(pipe.readline, b""):
            chunks[key].append(line)
            if fh is not None:
                try:
                    fh.write(_tel_capture.redact_line(line) if _tel_capture is not None else line)
                    fh.flush()
                except OSError:
                    fh = None
        pipe.close()

    fh = None
    if sink is not None:
        try:
            Path(sink).parent.mkdir(parents=True, exist_ok=True)
            fh = open(sink, "ab")
        except OSError:
            fh = None
    threads = [threading.Thread(target=_pump, args=(proc.stdout, "out", fh), daemon=True),
               threading.Thread(target=_pump, args=(proc.stderr, "err", None), daemon=True)]
    for t in threads:
        t.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _killpg(proc.pid)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
    _killpg(proc.pid)   # the turn is over when the parent exits — nothing outlives the launch that paid for it
    for t in threads:
        t.join(timeout=10)
    if fh is not None:
        try:
            fh.close()
        except OSError:
            pass
    rc = proc.returncode if proc.returncode is not None else -1
    return (rc, b"".join(chunks["out"]).decode(errors="replace"),
            b"".join(chunks["err"]).decode(errors="replace"), timed_out)


def _run(cmd: list[str], cwd: Path, timeout: int) -> tuple[int, str, str]:
    """The execve boundary: setsid group, SIGKILL on the whole group at exit or timeout → (rc, stdout, stderr)."""
    refusal = _refuse_engine(cmd, "execve") if _is_engine_cmd(cmd) else None
    if refusal is not None:
        return ENGINE_REFUSED_RC, "", refusal["detail"]
    try:
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, preexec_fn=os.setsid)
    except FileNotFoundError:
        return -2, "", f"CLI absente: {cmd[0]}"
    if _engine_registry is not None and _is_engine_cmd(cmd):
        try:   # FACTORY_ORPHAN_BY_SESSION_V1: no launch without its durable lease record — fail CLOSED
            _engine_registry.register_engine_process(
                cwd, pid=proc.pid,
                lease_id=os.environ.get("FACTORY_LEASE_ID") or os.environ.get("FACTORY_ATTEMPT_KEY") or "",
                mission_id=os.environ.get("FACTORY_ATTEMPT_KEY") or os.environ.get("FACTORY_MILESTONE") or "")
        except Exception as exc:
            _killpg(proc.pid)
            proc.communicate(timeout=5)
            return ENGINE_REFUSED_RC, "", f"ENGINE_REGISTRATION_FAILED: {exc}"
    group = proc.pid   # setsid: the child leads a NEW group whose id is its own pid (valid even after reaping)
    if _STREAM_SINK[0] is not None:   # FACTORY_TOKEN_TRUTH_V1: raw stream conserved line by line, as it flows
        rc, out_s, err_s, timed_out = _pump_to_sink(proc, _STREAM_SINK[0], timeout)
        if not timed_out:
            return rc, out_s, err_s
        if "TIMEOUT" not in err_s:
            err_s = (err_s + "\n" if err_s else "") + "TIMEOUT"
        return -1, out_s, err_s
    try:
        out, err = proc.communicate(timeout=timeout)
        _killpg(group)   # the turn is over when the parent exits — nothing outlives the launch that paid for it
        return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")
    except subprocess.TimeoutExpired:
        _killpg(group)
        try:
            out, err = proc.communicate(timeout=10)   # drain what was flushed before the kill (L3-004)
        except Exception:
            out, err = b"", b""
        drained_err = err.decode(errors="replace")
        if "TIMEOUT" not in drained_err:
            drained_err = (drained_err + "\n" if drained_err else "") + "TIMEOUT"
        return -1, out.decode(errors="replace"), drained_err


def _git_via_run(repo: Path, *args: str) -> str:
    """git through _run() so tests that patch _run() still see these calls; bound to `_git` (tests patch it)."""
    rc, out, _ = _run(["git"] + list(args), cwd=repo, timeout=30)
    return out.strip() if rc == 0 else ""


_git = _git_via_run


def _git_plumbing(repo: Path, *args: str, timeout: int = 30) -> "tuple[int, str, str]":
    """Bookkeeping git NEVER through _run(): invisible to the model-call accounting tests patch _run for."""
    try:
        r = subprocess.run(list(args), cwd=repo, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired as exc:
        return -1, (exc.stdout or ""), (exc.stderr or "")
    except OSError as exc:
        return -2, "", str(exc)


def _log(msg: str) -> None:
    print(msg, flush=True)


def _write_json(path: Path, rec: dict) -> None:
    """Atomic JSON write (tmp + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _write_heartbeat(out_path: Path, step: str, status: str = "RUNNING", **extra) -> None:
    """D002: best-effort liveness for factoryd; `extra` is folded into the status string (schema unchanged)."""
    if extra:
        status = status + " " + " ".join(f"{k}={v}" for k, v in sorted(extra.items()))
    try:
        write_heartbeat(out_path.parent, os.getpid(), step, status)
    except Exception as exc:
        _log(f"HEARTBEAT_WARN: step={step} — {exc}")


# ------------------------------------------------------------------ golden merge + worktree lifecycle
def _is_factory_repo(repo_root: Path) -> bool:
    """True only for the Factory's own control repo (by root commit) — the sole auto-merge target."""
    return FACTORY_ROOT_COMMIT in _git(repo_root, "rev-list", "--max-parents=0", "HEAD").splitlines()


_BOOKKEEPING_PREFIXES = ("factory_v2/receipts/", "projects/", "factory/run/", "factory/cto/",
                         ".portfolio_cursor.json", "evidence/")
_MISSION_FILES = _hyg.MISSION_CHECKPOINTS   # PLAN.md, REPORT.json, .factory_worktree_meta.json


def _is_state_file(f: str) -> bool:
    """L3-001: auto-resolution is limited to EXPLICIT state files — code/authority under projects/ must conflict."""
    if f.startswith(("factory_v2/receipts/", "factory/run/", "factory/cto/", ".portfolio_cursor.json", "evidence/")):
        return True
    parts = f.split("/")
    return f.startswith("projects/") and len(parts) >= 3 and parts[2] in ("PROJECT_STATE.json", "evidence")


def _factory_commit(repo_root: Path, *args: str) -> tuple:
    return _run(["git", "-c", "user.email=factory@local", "-c", "user.name=factory", "commit", *args],
                cwd=repo_root, timeout=60)


def _merge_candidate(repo_root: Path, branch: str, mission_id: str) -> bool:
    """Merge an ACCEPTED candidate into golden. True on a clean merge; conflicts limited to state files / the
    candidate's own checkpoints resolve (live side / absent from the result); anything else aborts, golden untouched.
    FACTORY_REPO_HYGIENE_V1 (22/09): the daemon no longer commits « état factory (auto) » (runtime bookkeeping is
    out of git, tracked dirt is only reported); the merge is made `--no-commit`, the mission checkpoints
    (PLAN.md, REPORT.json, .factory_worktree_meta.json) and any tracked-but-ignored bookkeeping are removed from the
    RESULT before the merge commit (index only), and whatever the merge deleted from disk that the repo now ignores
    is written back, untracked — a merge never makes bookkeeping disappear from disk."""
    try:
        _, st_out, _ = _run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=repo_root, timeout=30)
        dirty = [ln[3:] for ln in st_out.splitlines() if ln.strip()]
        if dirty:
            _log(f"MERGE_PRECONDITION: {mission_id} — {len(dirty)} dirty tracked file(s) in control checkout, "
                 f"left as is (no auto-commit): {dirty[:8]}")
        parked = _hyg.park_untracked_checkpoints(repo_root, mission_id)
        if parked:
            _log(f"MERGE_CHECKPOINTS_PARKED: {mission_id} — untracked root checkpoint copies moved aside: {parked}")
    except Exception:
        pass
    before_sha = _git(repo_root, "rev-parse", "HEAD")
    if _run(["git", "merge-base", "--is-ancestor", branch, "HEAD"], cwd=repo_root, timeout=30)[0] == 0:
        # Essai 4 : daemon coupé entre la fusion et le reçu → la reprise refusionne le même candidat. « Already up
        # to date » n'est pas un conflit (l'ancien `git merge` retournait 0) : True, HEAD inchangé, aucun commit vide.
        _log(f"MERGE_ALREADY_DONE: {mission_id} branch={branch} is already an ancestor of golden HEAD {before_sha[:12]}")
        return True
    rc, out, err = _run(["git", "merge", "--no-ff", "--no-commit", branch], cwd=repo_root, timeout=60)
    message = f"Merge branch '{branch}' into golden ({mission_id})"
    if rc != 0:
        conf = [f for f in _run(["git", "diff", "--name-only", "--diff-filter=U"], cwd=repo_root, timeout=30)[1].splitlines()
                if f.strip()]
        foreign = [f for f in conf if not _is_state_file(f) and f not in _MISSION_FILES]
        if not conf or foreign:
            _run(["git", "merge", "--abort"], cwd=repo_root, timeout=30)
            detail = (f"non-state conflict(s) {foreign[:8]} refuse auto-resolution; golden preserved, merge aborted "
                      f"({(out + err)[-300:]})" if foreign else f"golden preserved, merge aborted ({(out + err)[-500:]})")
            _log(f"MERGE_CONFLICT: {mission_id} branch={branch} — {detail}")
            return False
        ours = [f for f in conf if _is_state_file(f)]
        if ours:
            _run(["git", "checkout", "--ours", "--", *ours], cwd=repo_root, timeout=30)
            _run(["git", "add", "--", *ours], cwd=repo_root, timeout=30)
        message = f"Merge {branch} (état factory : version live conservée)"
        _log(f"MERGE_BOOKKEEPING_RESOLVED: {mission_id} kept live versions of {len(ours)} state file(s)")
    cleaned = _hyg.clean_merge_result(repo_root)   # checkpoints + tracked-ignored bookkeeping: never on golden
    if cleaned["checkpoints"] or cleaned["bookkeeping"]:
        _log(f"MERGE_RESULT_CLEANED: {mission_id} — checkpoints kept out of golden: {cleaned['checkpoints']}, "
             f"bookkeeping removed from the index: {len(cleaned['bookkeeping'])} file(s)")
    rc, out, err = _factory_commit(repo_root, "--no-edit", "-qm", message)
    if rc != 0:
        _run(["git", "merge", "--abort"], cwd=repo_root, timeout=30)
        _log(f"MERGE_CONFLICT: {mission_id} branch={branch} — merge commit failed, golden preserved "
             f"({(out + err)[-300:]})")
        return False
    restored = _hyg.restore_ignored_deletions(repo_root, before_sha, "HEAD")
    if restored:
        _log(f"MERGE_BOOKKEEPING_RESTORED: {mission_id} — {len(restored)} ignored file(s) kept on disk, untracked "
             f"(ex. {restored[:3]})")
    parked = _hyg.park_untracked_checkpoints(repo_root, mission_id)   # the candidate's copies: kept, not at the root
    if parked:
        _log(f"MERGE_CHECKPOINTS_PARKED: {mission_id} — candidate checkpoint copies kept under {parked[0].rsplit('/', 1)[0]}")
    return True


def _clear_inherited_checkpoints(worktree: Path) -> None:
    """Remove generic checkpoints inherited from a prior milestone's commit."""
    for name in ("PLAN.md", "REPORT.json"):
        path = Path(worktree) / name
        if path.exists() or path.is_symlink():
            path.unlink()


def _worktree_has_live_process(path: Path) -> bool:
    """ECO-07 liveness: True = do not touch (a process has `path` open / as cwd, OR detection was inconclusive)."""
    path_str = str(path)
    try:
        rc, out, _ = _run(["lsof", "+D", path_str], cwd=Path("/"), timeout=10)
        if rc in (0, 1):
            return rc == 0 and bool(out.strip())
    except Exception:
        pass
    try:
        import psutil  # type: ignore
        resolved = path.resolve()
        for proc in psutil.process_iter(["cwd"]):
            try:
                cwd = proc.info.get("cwd")
            except Exception:
                continue
            if cwd and Path(cwd).resolve() == resolved:
                return True
        return False
    except ImportError:
        pass
    except Exception:
        return True
    try:
        rc, out, _ = _run(["ps", "-eo", "pid,command"], cwd=Path("/"), timeout=10)
        return path_str in out if rc == 0 else True
    except Exception:
        return True


def _matches_keep_pattern(mission_id: str, path: Path, keep_patterns: tuple) -> bool:
    """True if mission_id or path is protected by keep_patterns (exact id/path, or fnmatch glob on both)."""
    path_str = str(path)
    return any(str(p) in (mission_id, path_str) or fnmatch.fnmatch(mission_id, str(p))
               or fnmatch.fnmatch(path_str, str(p)) for p in keep_patterns)


ARCHIVED_WORKTREE_MAX_AGE_H = 24.0   # FACTORY_REPO_HYGIENE_V1: an `.archived-*` checkout lives one day at most


def _is_archived_worktree(mission_id: str) -> bool:
    return ".archived-" in str(mission_id)


def _preserve_wip_on_archive_branch(path: Path) -> bool:
    """An archived checkout is a dead-end branch nobody works in: commit its uncommitted WIP on that branch before
    the checkout goes, so purging never loses data (the branch and its commits stay). True when a commit was made."""
    rc, out, _ = _run(["git", "status", "--porcelain"], cwd=path, timeout=30)
    if rc != 0 or not any(ln.strip() for ln in out.splitlines()):
        return False
    _run(["git", "add", "-A", "--", "."], cwd=path, timeout=60)
    rc, _, _ = _factory_commit(path, "-qm", "WIP préservé avant purge du worktree archivé (FACTORY_REPO_HYGIENE_V1)")
    return rc == 0


def prune_stale_worktrees(repo_root: Path, max_age_h: float = 48, keep_patterns: tuple = (),
                          worktrees_base: Path = WORKTREES_BASE,
                          archived_max_age_h: float = ARCHIVED_WORKTREE_MAX_AGE_H) -> list:
    """ECO-07: remove abandoned mission CHECKOUTS (never a branch or commit) older than max_age_h, unprotected by
    keep_patterns and without a live process. FACTORY_REPO_HYGIENE_V1: `.archived-*` checkouts (archive_worktree)
    use archived_max_age_h (24 h) and get their dirty WIP committed on the archive branch first. Best-effort, never
    raises; returns the mission ids removed. Called per dispatch and every daemon cycle (factoryd)."""
    worktrees_base = Path(worktrees_base).resolve()
    try:
        wm = WorktreeManager(repo_root=repo_root, worktrees_base=worktrees_base)
        infos = wm.list_worktrees()
    except Exception as exc:
        _log(f"WORKTREE_PRUNE_LIST_WARN: {exc}")
        return []
    now, removed = time.time(), []
    for info in infos:
        path = Path(info.path)
        try:
            path.resolve().relative_to(worktrees_base)   # hard invariant: never a path outside worktrees_base
            if _matches_keep_pattern(info.mission_id, path, keep_patterns) or not path.exists():
                continue
            age_h = (now - path.stat().st_mtime) / 3600.0
        except (OSError, ValueError):
            continue
        archived = _is_archived_worktree(info.mission_id)
        if age_h < (archived_max_age_h if archived else max_age_h):
            continue
        if _worktree_has_live_process(path):
            _log(f"WORKTREE_PRUNE_SKIP_BUSY: {info.mission_id} age_h={age_h:.1f} path={path}")
            continue
        try:
            if archived and _preserve_wip_on_archive_branch(path):
                _log(f"WORKTREE_ARCHIVE_WIP_COMMITTED: {info.mission_id} — uncommitted WIP kept on the archive branch")
            wm.remove_worktree(info.mission_id, force=True)
            _run(["git", "worktree", "prune"], cwd=repo_root, timeout=30)
            removed.append(info.mission_id)
            _log(f"WORKTREE_PRUNED_STALE: {info.mission_id} age_h={age_h:.1f} path={path} "
                 "(branch preserved — only the checkout was removed)")
        except Exception as exc:
            _log(f"WORKTREE_PRUNE_WARN: {info.mission_id} — {exc}")
    return removed


def _acquire_worktree(wm: WorktreeManager, mission_id: str, base_sha: str):
    """The mission's worktree: recovered when its base still equals the product HEAD; a stale base is never a safe
    base (C40/C46) — rebased when clean, else archived on a timestamped branch and recreated at HEAD. A mission
    RE-OPENED at attempt 1 never inherits its old worktree; an orphan directory is set aside, never deleted."""
    def fresh():
        try:
            info = wm.create_worktree(mission_id)
        except Exception as exc:
            wt = Path(wm.worktree_path(mission_id))
            if "already exists" not in str(exc) or not wt.exists():
                raise
            aside = wt.with_name(f"{wt.name}.orphan-{int(time.time())}")
            wt.rename(aside)
            _log(f"WORKTREE_ORPHAN_SET_ASIDE: {mission_id} path={wt} -> {aside} (not a worktree of this repo)")
            info = wm.create_worktree(mission_id)
        _clear_inherited_checkpoints(info.path)
        _log(f"WORKTREE_CREATED: {mission_id} branch={info.branch}")
        return info

    def archive(tag: str, note: str):
        archived = wm.archive_worktree(existing)
        _log(f"WORKTREE_ARCHIVED_{tag}: {mission_id} -> {archived.mission_id} "
             f"(path={archived.path}, branch={archived.branch}) — {note}")
        return fresh()

    existing = wm.recover_worktree(mission_id)
    if existing is None:
        return fresh()
    if not (existing.base_commit and existing.base_commit != base_sha):
        if (os.environ.get("FACTORY_MISSION_REOPENED") == "1"
                and wm.worktree_head(existing.path) != (existing.base_commit or base_sha)):
            return archive("REOPENED", "previous attempt's commits kept; fresh worktree for the re-opened mission")
        _log(f"WORKTREE_RECOVERED: {mission_id} at {existing.path}")
        return existing
    advanced = f"base advanced {existing.base_commit[:12]} -> {base_sha[:12]}"
    dirty = wm.is_worktree_dirty(existing.path)
    if wm.worktree_head(existing.path) != existing.base_commit:   # carries its own commits (even failed ones)
        rebased = None if dirty else wm.rebase_worktree(existing, base_sha)
        if rebased is not None:
            _log(f"WORKTREE_REBASED: {mission_id} {advanced}")
            return rebased
        return archive("STALE_BASE", f"{advanced}, commits preserved on the archive branch, mission restarts at HEAD")
    if dirty:
        return archive("DIRTY", f"{advanced}, uncommitted WIP preserved rather than overwritten")
    info = wm.refresh_worktree(existing, base_sha)
    _log(f"WORKTREE_REFRESHED: {mission_id} from={existing.base_commit[:12]} to={base_sha[:12]}")
    return info


def _cleanup_worktree(wm, repo_root: Path, info, mission_id: str, base_sha: str, rec: dict, merged: bool) -> None:
    """Never masks the verdict. A dirty tree, or WIP under a non-ACCEPTED verdict, is preserved for the retry
    (LOST_VALIDATED_WORK = 0); a clean tree is removed. Branch: -d after a merge, kept for an external ACCEPTED."""
    try:
        status = _git(info.path, "status", "--porcelain")
        dirty_wip = any(line[3:].strip() != ".factory_worktree_meta.json"
                        for line in status.splitlines() if len(line) >= 4)
        tip_sha = _git(info.path, "rev-parse", "HEAD")
        accepted = rec.get("verdict") == "ACCEPTED"
        preserve_wip = dirty_wip or (not accepted and bool(tip_sha and tip_sha != base_sha))
        if preserve_wip:
            _log(f"WORKTREE_PRESERVED_FOR_RETRY: {mission_id} branch={info.branch} — blocked result carries WIP")
        else:
            wm.remove_worktree(mission_id, force=True)
            _run(["git", "worktree", "prune"], cwd=repo_root, timeout=30)
        if merged:
            drc, dout, derr = _run(["git", "branch", "-d", info.branch], cwd=repo_root, timeout=30)
            if drc != 0:
                _log(f"BRANCH_DELETE_WARN: {mission_id} branch={info.branch} — {(dout + derr)[-300:]}")
        elif preserve_wip:
            pass
        elif accepted and not _is_factory_repo(repo_root):
            _log(f"BRANCH_PRESERVED: {mission_id} branch={info.branch} — external repo ACCEPTED, "
                 "awaiting AutopilotExecutor promotion")
        else:
            tip_rc, tip_out, _ = _run(["git", "rev-parse", info.branch], cwd=repo_root, timeout=10)
            _log(f"BRANCH_CLEANUP: {mission_id} branch={info.branch} "
                 f"tip={(tip_out.strip() if tip_rc == 0 else 'UNKNOWN')[:12]} — verdict={rec.get('verdict')}, force-deleting")
            _run(["git", "branch", "-D", info.branch], cwd=repo_root, timeout=30)
        if not preserve_wip:
            _log(f"WORKTREE_CLEANED: {mission_id}")
    except Exception as exc:
        _log(f"WORKTREE_CLEANUP_WARN: {mission_id} — {exc}")


def _run_in_worktree(
    mission_id: str, repo_root: Path, task: str, test_cmd: str | None, budget_s: int, out_path: Path,
    spec_level: str = "B", ledger: Optional[BudgetLedger] = None, plan_file: Optional[Path] = None,
    project_dir: Optional[Path] = None, engine_hint: "str | None" = None, project: "str | None" = None,
    task_kind: str = "build", deliverables: "list[str] | None" = None, test_cmd_is_product_e2e: bool = False,
) -> dict:
    """A whole dispatch_v3 mission inside an isolated worktree (F003). ACCEPTED on the Factory's own repo is merged
    into golden (a failed merge downgrades to BLOCKED, on disk too); external repos keep their candidate branch."""
    started_at = _now()
    base_sha = _git(repo_root, "rev-parse", "HEAD")
    context_path, blocked_reason = _prepare_context_package(mission_id, repo_root, out_path, plan_file,
                                                              project_dir=project_dir)
    if blocked_reason is not None:   # CTOK-003-Q hard gate: no worktree, no subprocess
        return _blocked_before_dispatch_receipt(mission_id, started_at, base_sha, blocked_reason, out_path)
    if context_path is not None:
        os.environ["FACTORY_CONTEXT_PATH"] = str(context_path)
    try:
        prune_stale_worktrees(repo_root, keep_patterns=(mission_id,))   # ECO-07, best-effort
    except Exception as exc:
        _log(f"WORKTREE_PRUNE_CALL_WARN: {mission_id} — {exc}")
    try:   # FACTORY_REPO_HYGIENE_V1: typed refusal BEFORE any `git worktree add` — never "No space left on device"
        _disk_guard.ensure_free_gb(WORKTREES_BASE if WORKTREES_BASE.exists() else repo_root)
    except _disk_guard.DiskSpaceLow as exc:
        _log(f"DISK_SPACE_LOW: {mission_id} — {json.dumps(exc.to_event())}")
        return _blocked_before_dispatch_receipt(mission_id, started_at, base_sha, str(exc), out_path)
    wm = WorktreeManager(repo_root=repo_root, worktrees_base=WORKTREES_BASE)
    worktree_info = _acquire_worktree(wm, mission_id, base_sha)
    merged, rec = False, {}
    try:
        rec = dispatch_v3(
            worktree_info.path, task, mission_id, test_cmd, budget_s, out_path, spec_level=spec_level,
            ledger=ledger, engine_hint=engine_hint, project=project or (project_dir.name if project_dir else None),
            task_kind=task_kind, project_dir=project_dir, deliverables=deliverables,
            product_base_sha=getattr(worktree_info, "base_commit", None) or None,
            test_cmd_is_product_e2e=test_cmd_is_product_e2e)
        if rec.get("verdict") != "ACCEPTED":
            _log(f"WORKTREE_NO_MERGE: {mission_id} verdict={rec.get('verdict')} — golden preserved")
        elif not _is_factory_repo(repo_root):
            _log(f"WORKTREE_NO_MERGE: {mission_id} — external repo, agent owns its own commits")
        else:
            merged = _merge_candidate(repo_root, worktree_info.branch, mission_id)
            rec = dict(rec)
            if merged:   # integration_sha = post-merge HEAD (the validator checks HEAD == integration_sha exactly)
                rec["integration_sha"] = _git(repo_root, "rev-parse", "HEAD")
                _log(f"WORKTREE_MERGED: {mission_id} -> main @ {rec['integration_sha'][:12]}")
            else:        # golden does NOT contain the candidate: never ACCEPTED, on disk either (L3-002)
                _log(f"WORKTREE_MERGE_FAILED: {mission_id} — downgrading ACCEPTED to BLOCKED")
                rec.update(verdict="BLOCKED", promotable=False,
                           verdict_reason=("MERGE_FAILED: candidate passed tests but merge into main failed. "
                                           "Golden branch is unchanged. Manual merge required."))
            _write_json(out_path, rec)
        return rec
    finally:
        _cleanup_worktree(wm, repo_root, worktree_info, mission_id, base_sha, rec, merged)


def _blocked_before_dispatch_receipt(mission_id: str, started_at: str, base_sha: str, reason: str,
                                     out_path: Path) -> dict:
    """BLOCKED receipt for a mission that never reached dispatch_v3() (hard gate before any worktree/process)."""
    rec = {"schema": SCHEMA, "mission_id": mission_id, "started_at": started_at, "ended_at": _now(),
           "elapsed_step_s": 0.0, "base_sha": base_sha, "candidate_sha": None, "verdict": "BLOCKED",
           "verdict_reason": reason, "promotable": False, "routing_decisions": [], "engine_executions": [],
           "stages": []}
    _write_json(out_path, rec)
    _log(f"{mission_id}: BLOCKED — {reason}")
    _log(f"reçu → {out_path}")
    return rec


def _load_protocol() -> str:
    if PROTOCOL_V4.exists():
        return PROTOCOL_V4.read_text(encoding="utf-8")
    return "(protocole V4 non trouvé — appliquer les principes du master plan)"


# ------------------------------------------------------------------ budget + telemetry glue
def _builder_declared_done(repo: Path, base_sha: str) -> bool:
    """HEAD moved past base_sha AND REPORT.json (worktree or HEAD) declares outcome=IMPLEMENTED."""
    try:
        head = _git(repo, "rev-parse", "HEAD")
        if not head or head == base_sha:
            return False
        rp = Path(repo) / "REPORT.json"
        raw = rp.read_text(encoding="utf-8") if rp.is_file() else _git(repo, "show", "HEAD:REPORT.json")
        return json.loads(raw).get("outcome") == "IMPLEMENTED"
    except Exception:
        return False


def _usage_for_budget(record: dict) -> tuple:
    """(tokens_in, tokens_out) from the measured observation; USAGE_UNKNOWN when nothing was measured."""
    ti, to = record.get("input_tokens"), record.get("output_tokens")
    if isinstance(ti, (int, float)) and isinstance(to, (int, float)):
        return int(ti), int(to)
    return USAGE_UNKNOWN, USAGE_UNKNOWN


def _telemetry_observe(**kwargs) -> tuple:
    """Measure one launch → (engine_execution_fields, observation); a failure never interrupts the dispatch."""
    if _telemetry is None:
        return ({"usage_source": "TELEMETRY_UNAVAILABLE", "usage_completeness": "ABSENT",
                 "telemetry_error": _TELEMETRY_IMPORT_ERROR}, None)
    try:
        obs = _telemetry.observe_launch(**kwargs)
        return _telemetry.to_engine_execution(obs), obs
    except Exception as exc:  # pragma: no cover
        return ({"usage_source": "TELEMETRY_FAILED", "usage_completeness": "ABSENT",
                 "telemetry_error": f"{type(exc).__name__}: {exc}"}, None)


def _budget_check(ledger: Optional[BudgetLedger], mission_id: str, step: str) -> Optional[dict]:
    """ledger.check() before a model call; BudgetExceeded becomes a BLOCKED record, never an exception."""
    if ledger is None:
        return None
    try:
        ledger.check(mission_id)
        return None
    except BudgetExceeded as exc:
        return {"_budget_blocked": True, "reason": str(exc), "step": step}


def _resume_allowed(ledger: Optional[BudgetLedger], mission_id: str, step: str, stages: list) -> bool:
    """L3-006: a `--resume` is a SECOND physical launch, arbitrated against the budget like the first."""
    blocked = _budget_check(ledger, mission_id, step)
    if blocked is None:
        return True
    stages.append({"step": f"{step}_resume", "status": "BUDGET_BLOCKED", "note": str(blocked["reason"])[:300]})
    _log(f"RESUME_REFUSED_BUDGET: {mission_id} step={step} — {blocked['reason']}")
    return False


def _budget_record(ledger: Optional[BudgetLedger], mission_id: str, engine: str, step: str,
                   tokens_in: int, tokens_out: int, wall_s: float) -> None:
    """Record actual usage; USAGE_UNKNOWN tokens count as 0 but the model call is always counted."""
    if ledger is not None:
        ledger.record(mission_id, engine=engine, step=step, tokens_in=max(0, tokens_in),
                      tokens_out=max(0, tokens_out), wall_s=wall_s, model_calls=1)


def _safe_snapshot(repo: Path):
    """FD1: best-effort working-tree snapshot before a worker step (None when git metadata is unusable)."""
    try:
        return _sd.snapshot(repo)
    except Exception:
        return None


# ------------------------------------------------------------------ candidate capture (FD1 / LOT C28-C34)
CAPTURE_MAX_FILES = 200            # a runaway worker, never a legitimate build
CAPTURE_MAX_BINARY_MB = 5.0
CAPTURE_MAX_FILE_MB = 2.0          # LOT C31: NEW oversized files that are not declared deliverables
CAPTURE_MAX_IMAGE_MB = 6.0
CAPTURE_IGNORE_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "capture_ignore.json"   # LOT C34
GIT_STATUS_UNVERIFIABLE_PREFIX = "GIT_STATUS_UNVERIFIABLE"
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif")
_SECRET_PATH_DENYLIST = (".env", "*.env", "*.pem", "*.key", "id_rsa*", "*.p12", "*.pfx",
                         "credentials*", "*secret*", "*token*", ".netrc", "*.sqlite", "*.db")
_SECRET_CONTENT_PATTERNS = tuple(re.compile(p) for p in (
    r"AKIA[0-9A-Z]{16}", r"-----BEGIN (RSA|EC|OPENSSH) PRIVATE KEY-----", r"ghp_[A-Za-z0-9]{36}",
    r"xox[baprs]-", r"sk-[A-Za-z0-9]{20,}"))


def _load_capture_ignore_patterns() -> "list[str]":
    """Bookkeeping-path ignore list; an unreadable/malformed config yields NO exclusions (fail-closed)."""
    try:
        data = json.loads(CAPTURE_IGNORE_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    paths = data.get("paths") if isinstance(data, dict) else None
    return [p for p in paths if isinstance(p, str) and p.strip()] if isinstance(paths, list) else []


def _is_bookkeeping_path(rel_path: str, patterns: "list[str] | None" = None) -> bool:
    """True when `rel_path` matches a capture_ignore.json entry (directory prefix `x/`, glob, or exact)."""
    patterns = _load_capture_ignore_patterns() if patterns is None else patterns
    rel = str(rel_path).replace(os.sep, "/")
    for pat in (p.strip() for p in patterns):
        if pat.endswith("/") and (rel == pat.rstrip("/") or fnmatch.fnmatch(rel, pat + "*")):
            return True
        if pat and not pat.endswith("/") and fnmatch.fnmatch(rel, pat):
            return True
    return False


def _looks_binary(full_path: Path) -> bool:
    """A NUL byte in the first 8KB; False on any read error."""
    try:
        with open(full_path, "rb") as fh:
            return b"\x00" in fh.read(8192)
    except OSError:
        return False


def _capture_size_guard(repo: Path, changed_files) -> "str | None":
    """LOT C28 (a): refuse an implausibly large capture — too many paths, or an unexpected oversized binary."""
    n = len(changed_files)
    if n > CAPTURE_MAX_FILES:
        return f"CANDIDATE_CAPTURE_REFUSED_TOO_MANY_FILES: {n} path(s) staged, limit is {CAPTURE_MAX_FILES}"
    for rel in sorted(changed_files):
        full = repo / rel
        try:
            size = full.stat().st_size if full.is_file() else -1
        except OSError:
            continue
        if size > CAPTURE_MAX_BINARY_MB * 1024 * 1024 and _looks_binary(full):
            return (f"CANDIDATE_CAPTURE_REFUSED_OVERSIZED_BINARY: {rel} is {size / (1024 * 1024):.1f}MB "
                    f"(limit {CAPTURE_MAX_BINARY_MB}MB for an unexpected binary)")
    return None


def _matches_secret_path(rel: str) -> "str | None":
    """The first denylist pattern `rel` matches (basename or full relpath, case-insensitive glob), or None."""
    name, full = Path(rel).name.lower(), rel.lower()
    return next((p for p in _SECRET_PATH_DENYLIST if fnmatch.fnmatch(name, p) or fnmatch.fnmatch(full, p)), None)


def _capture_secrets_and_size_guard(repo: Path, changed_files, deliverables) -> "str | None":
    """LOT C31 (3): refuse fail-closed a denylisted secret-shaped path, an oversized NEW non-deliverable file
    (images get a higher ceiling, files already tracked at HEAD are exempt) or live-credential-shaped text."""
    declared = {str(d).strip() for d in (deliverables or []) if str(d).strip()}
    declared_dirs = {d.rstrip("/") for d in declared if d.endswith("/")} | {
        str(Path(d).parent) for d in declared if not d.endswith("/") and "/" in d}
    for rel in sorted(changed_files):
        pattern = _matches_secret_path(rel)
        if pattern is not None:
            return (f"CANDIDATE_CAPTURE_REFUSED_SECRET: {rel} matches denylisted path pattern {pattern!r} — "
                    "refused fail-closed, never staged into a candidate commit")
        full = repo / rel
        try:
            if not full.is_file():
                continue
            size = full.stat().st_size
        except OSError:
            continue
        in_declared_dir = any(rel.startswith(d + "/") for d in declared_dirs if d and d != ".")
        limit_mb = CAPTURE_MAX_IMAGE_MB if Path(rel).suffix.lower() in _IMAGE_SUFFIXES else CAPTURE_MAX_FILE_MB
        already_tracked = _git_plumbing(repo, "git", "cat-file", "-e", f"HEAD:{rel}", timeout=15)[0] == 0
        if size > limit_mb * 1024 * 1024 and rel not in declared and not in_declared_dir and not already_tracked:
            return (f"CANDIDATE_CAPTURE_REFUSED_OVERSIZED_FILE: {rel} is {size / (1024 * 1024):.1f}MB "
                    f"(limit {CAPTURE_MAX_FILE_MB}MB for a file not declared as a milestone deliverable)")
        if _looks_binary(full):
            continue
        try:
            text = full.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for content_pattern in _SECRET_CONTENT_PATTERNS:
            if content_pattern.search(text):
                return (f"CANDIDATE_CAPTURE_REFUSED_SECRET: {rel} content matches secret pattern "
                        f"{content_pattern.pattern!r} — refused fail-closed, never staged into a candidate commit")
    return None


def _capture_worker_changes(repo: Path, snapshot_before, mission_id: str, step_name: str, stages: list,
                            deliverables: "list[str] | None" = None) -> "tuple[Optional[str], Optional[str]]":
    """FD1/LOT C28: Factory owns the commit. (sha, None) captured or the worker committed itself; (None, None)
    nothing changed / capture failed (traced, the oracle decides); (None, reason) refused fail-closed."""
    def _stage(status: str, **kw) -> None:
        stages.append({"step": "factory_capture", "after_step": step_name, "status": status, **kw})

    try:
        if snapshot_before is None:
            _stage("SKIPPED", note="working-tree baseline unavailable (git metadata unusable)")
            return None, None
        rc, out, err = _git_plumbing(repo, "git", "add", "-A", "--", ".", ":!.factory", ":!.factory/**", timeout=120)
        if rc != 0:
            _stage("FAILED", note=f"git add -A failed rc={rc}: {(out + err)[-500:]}")
            return None, None
        present = [f for f in _MISSION_FILES if (Path(repo) / f).is_file()]
        if present:   # FACTORY_REPO_HYGIENE_V1: the checkpoints are ALWAYS in the candidate (the accept gate reads
            _git_plumbing(repo, "git", "add", "-f", "--", *present, timeout=60)   # REPORT.json AT the sha)
        changed = {p for p in _git_plumbing(repo, "git", "diff", "--cached", "--name-only")[1].splitlines() if p.strip()}
        patterns = _load_capture_ignore_patterns()
        bookkeeping = {p for p in changed if _is_bookkeeping_path(p, patterns)}
        if bookkeeping:   # LOT C34: unstaged one by one — the tree keeps the daemon's write, the candidate does not
            for rel in sorted(bookkeeping):
                _git_plumbing(repo, "git", "reset", "--", rel)
            stages.append({"step": "factory_capture_bookkeeping_excluded", "after_step": step_name,
                           "paths": sorted(bookkeeping)})
            changed -= bookkeeping
        if not changed:   # no change at all, or the worker committed directly (only the snapshot pair can tell)
            d = _sd.diff(snapshot_before, _sd.snapshot(repo))
            if not d.has_changes:
                _stage("NO_ARTIFACT", note="worker produced no working-tree changes")
                return None, None
            changed = {p for p in d.all_changed if not _is_bookkeeping_path(p, patterns)}
            if not changed:
                _stage("NO_ARTIFACT", note="only factory-owned bookkeeping changed (LOT C34)")
                return None, None
        reason = _capture_size_guard(repo, changed) or _capture_secrets_and_size_guard(repo, changed, deliverables)
        if reason is not None:
            _git_plumbing(repo, "git", "reset", "--", ".")
            _stage("REFUSED", reason=reason, files_count=len(changed))
            return None, reason
        t0 = time.monotonic()
        sha = _sd.factory_commit(repo=repo, changed_files=changed, mission_id=mission_id, step=step_name)
        _stage("COMMITTED", files=sorted(changed), candidate_sha=sha, duration_s=round(time.monotonic() - t0, 2))
        return sha, None
    except Exception as exc:
        _stage("FAILED", note=f"{type(exc).__name__}: {exc}")
        return None, None


def _capture_gate_verdict(reason: str) -> str:
    """Verdict for a non-None _post_capture_gate() reason (both are blocking, never promotable)."""
    return ("CANDIDATE_CAPTURE_UNVERIFIABLE" if reason.startswith(GIT_STATUS_UNVERIFIABLE_PREFIX)
            else "CANDIDATE_CAPTURE_INCOMPLETE")


def _post_capture_gate(repo: Path, step_name: str, stages: list) -> "str | None":
    """LOT C28 (c): the worktree MUST be PROVEN clean (modulo bookkeeping) before any test run; an unverifiable
    `git status` is fail-closed too (C31). None when clean, else a reason."""
    def _stage(status: str, **kw) -> None:
        stages.append({"step": "candidate_capture_gate", "after_step": step_name, "status": status, **kw})

    rc, status, err = _git_plumbing(repo, "git", "status", "--porcelain", "--untracked-files=all",
                                    "--", ".", ":!.factory", ":!.factory/**")
    if rc != 0:
        _stage("UNVERIFIABLE", note=f"git status unavailable rc={rc}: {err[-300:]}")
        return (f"{GIT_STATUS_UNVERIFIABLE_PREFIX}: cannot prove the worktree is clean after {step_name} — "
                f"git status rc={rc}: {err[-300:]}")
    if not status.strip():
        _stage("CLEAN")
        return None
    raw = [line[3:].strip() for line in status.splitlines() if len(line) >= 4]
    patterns = _load_capture_ignore_patterns()
    excluded = [p for p in raw if _is_bookkeeping_path(p, patterns)]
    dirty = [p for p in raw if p not in set(excluded)]
    if not dirty:
        _stage("CLEAN", bookkeeping_excluded=excluded)
        return None
    _stage("DIRTY", paths=dirty[:50], bookkeeping_excluded=excluded)
    shown = ", ".join(dirty[:10]) + ("…" if len(dirty) > 10 else "")
    return f"worktree still dirty after candidate capture ({len(dirty)} path(s) after {step_name}): {shown}"


def _check_deliverables(repo: Path, candidate_sha: str, deliverables) -> list:
    """LOT C28 (b): declared deliverables missing IN THE CANDIDATE COMMIT (`git cat-file -e <sha>:<path>`)."""
    return [p for p in (str(raw).strip() for raw in deliverables or []) if p
            and _git_plumbing(repo, "git", "cat-file", "-e", f"{candidate_sha}:{p}", timeout=15)[0] != 0]


def _is_product_e2e_test_cmd(test_cmd: str | None) -> bool:
    """True when `test_cmd` is the product's own end-to-end oracle (an unchanged deliverable is legitimate there)."""
    return bool(test_cmd) and "product_e2e" in test_cmd


def _read_builder_report_outcome(repo: Path, candidate_sha: str | None) -> tuple[str | None, str]:
    """The builder's own `outcome` from REPORT.json AT candidate_sha (never the disk copy), fail-closed:
    (None, reason) when absent/invalid/blank; an IN_PROGRESS PARTIAL is noted INTERIM_CHECKPOINT."""
    if not candidate_sha:
        return None, "NO_CANDIDATE_SHA"
    rc, out, _ = _git_plumbing(repo, "git", "show", f"{candidate_sha}:REPORT.json", timeout=15)
    if rc != 0:
        return None, "REPORT_JSON_ABSENT"
    try:
        data = json.loads(out)
    except (ValueError, TypeError):
        return None, "REPORT_JSON_INVALID"
    outcome = data.get("outcome") if isinstance(data, dict) else None
    if not isinstance(outcome, str) or not outcome.strip():
        return None, "REPORT_JSON_NO_OUTCOME"
    outcome = outcome.strip()
    return outcome, ("INTERIM_CHECKPOINT" if data.get("status") == "IN_PROGRESS" and outcome == "PARTIAL" else "OK")


def _builder_declared_verdict(outcome: str) -> str:
    """Safe BUILDER_DECLARED_<X> verdict from a raw REPORT.json outcome string."""
    return f"BUILDER_DECLARED_{re.sub(r'[^A-Za-z0-9]+', '_', outcome).strip('_').upper() or 'UNKNOWN'}"


def _unchanged_declared_deliverables(repo: Path, base_sha: str, candidate_sha: str, deliverables) -> list:
    """Declared deliverables that existed at base_sha and are byte-identical in candidate_sha (a brand-new file
    is real work, excluded here; _check_deliverables proves it exists)."""
    unchanged = []
    for path in (str(raw).strip() for raw in deliverables or []):
        if not path:
            continue
        rc_base, base_blob, _ = _git_plumbing(repo, "git", "rev-parse", f"{base_sha}:{path}", timeout=15)
        if rc_base != 0:
            continue
        rc_cand, cand_blob, _ = _git_plumbing(repo, "git", "rev-parse", f"{candidate_sha}:{path}", timeout=15)
        if rc_cand == 0 and cand_blob.strip() == base_blob.strip():
            unchanged.append(path)
    return unchanged


# ------------------------------------------------------------------ the dispatch loop
def dispatch_v3(
    repo: Path, task: str, mission_id: str, test_cmd: str | None, budget_s: int, out_path: Path,
    spec_level: str = "B", ledger: Optional[BudgetLedger] = None, engine_hint: "str | None" = None,
    project: "str | None" = None, task_kind: str = "build", project_dir: "Path | None" = None,
    deliverables: "list[str] | None" = None, product_base_sha: "str | None" = None,
    test_cmd_is_product_e2e: bool = False,
) -> dict:
    """Dispatch a mission in `repo` (its isolated worktree). task_kind "build": T0 build → T1 test → T2 repair →
    test → T3 escalate → test, every launch routed by model_router, arbitrated by the budget ledger, measured by
    telemetry. "diagnose"/"replan": one read-only judgment launch whose final JSON the dispatcher writes under
    project_dir/evidence/recovery/. `deliverables` are proven at the exact candidate commit before ANY ACCEPTED;
    `product_base_sha` (C47) is the base an "unchanged deliverable" is judged against."""
    protocol = _load_protocol()
    state_path = out_path.parent / f"mission_{mission_id}.json"
    step_names = [task_kind] if task_kind in ("diagnose", "replan") else list(_BUILD_STEPS)
    state = MissionState.load_or_create(path=state_path, mission_id=mission_id, task_file=str(repo),
                                        budget_s=budget_s, step_names=step_names)
    if state.is_terminal():   # C43: a prior attempt's terminal state must never let a new grant skip every stage
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        status = re.sub(r"[^A-Za-z0-9_.-]+", "_", state.status) or "terminal"
        archive_dir, n = out_path.parent / "archive" / f"{stamp}_{status}", 1
        while archive_dir.exists():
            archive_dir, n = out_path.parent / "archive" / f"{stamp}_{status}_{n}", n + 1
        archive_dir.mkdir(parents=True)
        canonical = out_path.parent / f"receipt_{mission_id}.json"
        for artifact in dict.fromkeys((state_path, canonical, telemetry_sidecar_path(canonical),
                                       out_path, telemetry_sidecar_path(out_path))):
            if artifact.exists():
                os.replace(artifact, archive_dir / artifact.name)
        print(f"[mission {mission_id}] TERMINAL_STATE_ARCHIVED -> {archive_dir}", flush=True)
        state = MissionState(mission_id=mission_id, task_file=str(repo), budget_s=budget_s, step_names=step_names)
        state.save(state_path)
    state.start()
    state.save(state_path)
    _write_heartbeat(out_path, "start")

    started_at = _now()
    base_sha = _git(repo, "rev-parse", "HEAD")
    stages: list[dict] = []
    routing_decisions: list[dict] = []    # what route() asked for
    engine_executions: list[dict] = []    # what the OS actually ran (F4) — one entry per process launched
    _telemetry_parent: dict = {}
    t_mission = time.monotonic()

    def _fail(verdict: str, why: str, candidate: "str | None" = None, test_command: "str | None" = None) -> dict:
        return _receipt(mission_id, started_at, base_sha, candidate, stages, verdict, why, out_path, state,
                        state_path, routing_decisions, engine_executions, test_command=test_command)

    def _remaining() -> int:
        return max(0, budget_s - int(time.monotonic() - t_mission))

    book = [str(d) for d in (deliverables or []) if _is_bookkeeping_path(str(d), _load_capture_ignore_patterns())]
    if book:   # LOT C34: a factory-owned bookkeeping path can never be a milestone's deliverable
        return _fail("CANDIDATE_DELIVERABLE_BOOKKEEPING_PATH",
                     "milestone declares deliverable(s) inside factory-owned bookkeeping path(s) (see "
                     f"factory/config/capture_ignore.json), never a valid deliverable: {', '.join(book)}")

    def _run_test() -> tuple[bool, str]:
        """The mechanical oracle: bash errexit+pipefail, TimeoutExpired typed as TIMEOUT (never ignored)."""
        if not test_cmd:
            return False, "TEST_CMD_MISSING — cannot qualify without a test command"
        rc, out, err = _run(["bash", "-c", f"set -eo pipefail\n{test_cmd}"], cwd=repo, timeout=_remaining())
        combined = (out + err)[-8000:]
        return (False, f"TIMEOUT: {combined}") if rc == -1 else (rc == 0, combined)

    def _accept_gate(candidate_sha: str, accepted_reason: str, allow_repair_once: bool = False) -> "dict | None":
        """The last checks before ANY ACCEPTED (shared by T1/T2/T3): the builder's own REPORT.json outcome (C40 a),
        declared deliverables untouched since the product base (C40 b / C45 / C47), declared deliverables present
        in the candidate (C28 b). None only when allow_repair_once and the builder honestly declares PARTIAL."""
        short = candidate_sha[:12] if candidate_sha else candidate_sha
        outcome, note = _read_builder_report_outcome(repo, candidate_sha)
        gate = {"step": "builder_outcome_gate", "candidate_sha": candidate_sha, "outcome": outcome}
        if outcome is None:
            stages.append({**gate, "status": note})
            return _fail("BUILDER_REPORT_MISSING",
                         f"REPORT.json absent/illisible au candidat {short} ({note}) — un verdict ACCEPTED "
                         "exige que le builder déclare lui-même son outcome", candidate_sha, test_cmd)
        if outcome != "IMPLEMENTED":
            interim = note == "INTERIM_CHECKPOINT"
            if allow_repair_once and outcome == "PARTIAL":
                stages.append({**gate, "status": "INTERIM_CHECKPOINT" if interim else "CONTINUE_TO_REPAIR"})
                return None
            stages.append({**gate, "status": "INTERIM_CHECKPOINT" if interim else "REFUSED"})
            return _fail(_builder_declared_verdict(outcome),
                         f"REPORT.json du candidat {short} déclare outcome={outcome} — le builder dit lui-même "
                         "que le travail n'est pas fait ; non promotable, jamais ACCEPTED sur la seule base d'un "
                         "test mécanique vert", candidate_sha, test_cmd)
        stages.append({**gate, "status": "OK"})
        if not (_is_product_e2e_test_cmd(test_cmd) or test_cmd_is_product_e2e):
            unchanged = _unchanged_declared_deliverables(repo, product_base_sha or base_sha, candidate_sha,
                                                         deliverables)
            declared = [str(d).strip() for d in (deliverables or []) if str(d).strip()]
            if unchanged and len(unchanged) < len(declared):   # C45: only ALL unchanged evidences skipped work
                stages.append({"step": "deliverable_unchanged_gate", "candidate_sha": candidate_sha,
                               "unchanged": unchanged, "status": "PARTIAL_UNCHANGED_TOLERATED"})
                unchanged = []
            if unchanged:
                stages.append({"step": "deliverable_unchanged_gate", "candidate_sha": candidate_sha,
                               "unchanged": unchanged, "status": "UNCHANGED"})
                return _fail("DELIVERABLE_UNCHANGED",
                             "déclaré(s) inchangé(s) entre base_sha et candidate_sha malgré un test mécanique "
                             f"vert : {', '.join(unchanged)} — le builder n'a pas fait le travail déclaré",
                             candidate_sha, test_cmd)
        missing = _check_deliverables(repo, candidate_sha, deliverables)
        stages.append({"step": "deliverables_check", "candidate_sha": candidate_sha,
                       "declared": list(deliverables or []), "status": "MISSING" if missing else "OK",
                       "missing": missing})
        if missing:
            return _fail("CANDIDATE_MISSING_DELIVERABLE",
                         f"declared deliverable(s) absent from candidate commit {short}: {', '.join(missing)}",
                         candidate_sha, test_cmd)
        return _fail("ACCEPTED", accepted_reason, candidate_sha, test_cmd)

    def _route_and_record(kind: str, stall_count: int = 0) -> "_mr.EngineChoice":
        hint = engine_hint if kind == "build" else None   # F5R5: the ladder's hint on the first BUILD call only
        choice = route_for_dispatch(spec_level, kind, project_dir=project_dir, stall_count=stall_count,
                                    engine_hint=hint, project=project)
        routing_decisions.append({"ts": _now(), "task_kind": kind, "spec_level": spec_level,
                                  "stall_count": stall_count, "project": project or "", "engine": choice.engine,
                                  "model": choice.model, "role": choice.role, "read_only": choice.read_only,
                                  "reason": choice.reason, "router_version": "model_router/v1"})
        return choice

    def _run_engine(cmd: list[str], choice: "_mr.EngineChoice", step_name: str) -> tuple[int, str, str]:
        """Launch an engine CLI and record what was ACTUALLY executed (identity from argv, never from routing).
        Each physical execve is measured on its own: a NETWORK_CUT waits for the engine API (bounded by the
        budget) then relaunches once; a QUOTA_CUT — or a pre-launch allowance refusal — writes a durable marker
        and returns a typed rc so the mission is resumed later instead of burning the window."""
        observed_engine, observed_model = _mr.engine_from_argv(cmd), _mr.model_from_argv(cmd)
        env = os.environ.get
        ctx0 = {"argv": cmd, "engine": observed_engine, "model_requested": choice.model, "role": choice.role,
                "lineage": _LINEAGE_BY_STEP.get(step_name, "PRIMARY"), "run_id": env("FACTORY_RUN_ID", ""),
                "project": project or "", "milestone": env("FACTORY_MILESTONE", ""), "mission": mission_id,
                "attempt": int(env("FACTORY_ATTEMPT") or 0), "attempt_key": env("FACTORY_ATTEMPT_KEY", ""),
                "contract_id": env("FACTORY_CONTRACT_ID", ""), "control_sha": env("FACTORY_CONTROL_SHA", ""),
                "product_sha": base_sha, "task_type": choice.task_kind, "spec_level": spec_level,
                "parent_observation_key": _telemetry_parent.get("key", "")}
        can_launch = (not real_engine_disabled() and bool(cmd)
                      and bool(Path(cmd[0]).is_file() or shutil.which(cmd[0])))

        def _one_launch(seq: int) -> dict:
            at = _now_iso()
            ctx = dict(ctx0, launch_seq=seq)
            _quota_refusal = ""
            if can_launch and observed_engine in ("claude", "codex", "glm"):
                try:   # the spending process is the last line: re-check the allowance of the engine about to run
                    _q_ok, _q_why = _mr._quota_engine_allowance(observed_engine, project or None, launching=True)
                    if not _q_ok:
                        _quota_refusal = _q_why or "ENGINE_QUOTA_EXHAUSTED"
                except Exception:
                    _quota_refusal = ""
            if can_launch and not _quota_refusal:   # durable BEFORE execve: a kill mid-call leaves an honest row
                _, opening = _telemetry_observe(stdout=None, started_at=at, ended_at=at, wall_time_s=None, rc=None,
                                                exit_class="IN_FLIGHT", provenance="LIVE_OPEN", **ctx)
                if opening is not None:
                    _append_telemetry_sidecar(out_path, {"telemetry": opening.as_dict()})
            t0 = time.monotonic()
            _cut, _cut_detail = "", {}
            if _quota_refusal:
                _rc, _out, _err = QUOTA_REFUSED_RC, "", f"QUOTA_REFUSED_BEFORE_LAUNCH: {_quota_refusal}"
                _cut = _resilience.QUOTA_CUT if _resilience is not None else ""
                _cut_detail = {"cut": _cut, "basis": "PRE_LAUNCH_ALLOWANCE", "connectivity": ""}
            else:
                # FACTORY_TOKEN_TRUTH_V1: the raw stream of this launch is teed to
                # projects/<p>/evidence/streams/<attempt_key>[.L<seq>].jsonl while it runs (_run), or written from
                # the captured stdout when _run is simulated — an empty file for a launch cut before its first event.
                _sink = (_tel_capture.stream_path(project_dir, ctx["attempt_key"] or mission_id, launch_seq=seq)
                         if _tel_capture is not None else None)
                _STREAM_SINK[0] = _sink
                try:
                    _rc, _out, _err = _run(cmd, cwd=repo, timeout=_remaining())
                finally:
                    _STREAM_SINK[0] = None
                if _sink is not None and _rc not in (-2, ENGINE_REFUSED_RC):   # a process ran: its stream exists
                    _tel_capture.persist_stream(_sink, _out)
                    ctx["log_path"] = str(_sink)
                if _resilience is not None and can_launch and _rc not in (0, -2, ENGINE_REFUSED_RC):
                    _cut_detail = _resilience.classify_interruption_detail(_rc, _out, _err, engine=observed_engine,
                                                                           probe=True)
                    _cut = _cut_detail.get("cut", "")
                    if _cut == _resilience.QUOTA_CUT:
                        _mark_provider_exhausted(observed_engine, reason=(_err or _out)[-300:])
            _wall = round(time.monotonic() - t0, 3)
            _refused = _rc == ENGINE_REFUSED_RC
            rec = {"ts": _now(), "step": step_name, "argv0": cmd[0] if cmd else "",
                   "engine_id": "" if _refused else observed_engine, "model": "" if _refused else observed_model,
                   **({"refused": True, "refusal": "REAL_ENGINE_DISABLED"} if _refused else {}),
                   "routed_engine": choice.engine, "routed_model": choice.model,
                   "engine_matches_routing": observed_engine == choice.engine,
                   "rc": _rc, "launched": _rc not in (-2, ENGINE_REFUSED_RC, QUOTA_REFUSED_RC),
                   "observed": not _refused, "started_at": at, "wall_time_s": _wall, "launch_seq": seq,
                   "interruption": _cut, "interruption_basis": _cut_detail.get("basis", ""),
                   "connectivity_at_failure": _cut_detail.get("connectivity", ""),
                   "stream_path": str(ctx.get("log_path") or ""),
                   **({"quota_refusal": _quota_refusal} if _quota_refusal else {})}
            if rec["launched"]:
                fields, obs = _telemetry_observe(stdout=_out, started_at=at, ended_at=_now_iso(),
                                                 wall_time_s=_wall, rc=_rc, **ctx)
                rec.update(fields)
                if obs is not None:
                    rec["telemetry"] = obs.as_dict()
                    if not _telemetry_parent.get("key"):
                        _telemetry_parent["key"] = obs.observation_key
            else:
                rec.update({"usage_source": "NO_LAUNCH", "usage_completeness": "ABSENT",
                            "usage_note": "no process was created — not zero usage"})
            rec["_out"], rec["_err"] = _out, _err
            return rec

        _ckpt = None   # checkpoints the worktree every few minutes across ALL launches of this step
        if _resilience is not None and can_launch:
            try:
                _ckpt = _resilience.WorktreeCheckpointer(
                    repo, mission_id,
                    on_checkpoint=lambda sha: _write_heartbeat(out_path, step_name, checkpoint=sha)).start()
            except Exception:
                _ckpt = None

        def _stop_ckpt() -> list:
            try:
                return list(_ckpt.stop(final=True)) if _ckpt is not None else []
            except Exception:
                return list(getattr(_ckpt, "shas", []) or [])

        def _write_cut_marker(cut: str, ckpt) -> None:
            try:
                _last = ((list(getattr(ckpt, "shas", []) or []) or [""])[-1]
                         or _resilience.latest_checkpoint(repo, mission_id))
            except Exception:
                _last = ""
            _resilience.write_interruption_marker(repo, mission_id, base_sha, cut,
                                                  attempt=int(os.environ.get("FACTORY_ATTEMPT") or 0),
                                                  checkpoint=_last)

        def _unpack(rec: dict) -> tuple:
            return rec["rc"], rec.pop("_out"), rec.pop("_err"), rec["interruption"]

        rec = _one_launch(len(engine_executions) + 1)
        rc, out, err, interruption = _unpack(rec)
        resumed_in_place = False
        if interruption == getattr(_resilience, "NETWORK_CUT", "__none__"):
            wait_s = max(0, min(_remaining() - 120, 45 * 60))
            _write_heartbeat(out_path, step_name, status="WAITING_NETWORK")
            rec["checkpoints"] = list(getattr(_ckpt, "shas", []) or [])
            _append_telemetry_sidecar(out_path, rec)
            engine_executions.append(rec)
            _refresh_cli_quota_snapshot(rec)
            if _resilience.wait_for_network(wait_s, engine=observed_engine, on_wait=lambda left, st="": _write_heartbeat(
                    out_path, step_name, status="WAITING_NETWORK", seconds_left=left, conn=st)):
                rec = _one_launch(len(engine_executions) + 1)
                rec["resumed_in_place"] = True
                rc, out, err, interruption = _unpack(rec)
                resumed_in_place = True
            else:
                _stop_ckpt()
                _write_cut_marker(_resilience.NETWORK_CUT, _ckpt)
                return _resilience.NETWORK_CUT_RC, out, err
        if _ckpt is not None:
            rec["checkpoints"] = _stop_ckpt()
        rec["resumed_in_place"] = resumed_in_place
        engine_executions.append(rec)
        _append_telemetry_sidecar(out_path, rec)
        _refresh_cli_quota_snapshot(rec)
        if interruption == getattr(_resilience, "QUOTA_CUT", "__none__") and rc != 0:
            _write_cut_marker(_resilience.QUOTA_CUT, _ckpt)
            return _resilience.QUOTA_CUT_RC, out, err
        if interruption == getattr(_resilience, "NETWORK_CUT", "__none__") and rc != 0:
            # a second text-only "network" diagnostic while the API answers is a semantic failure: label withdrawn
            if rec.get("interruption_basis") == getattr(_resilience, "UNCONFIRMED", "TEXT_TAIL_UNCONFIRMED"):
                rec["interruption_withdrawn"] = "UNCONFIRMED_TEXT_ONLY_TWICE"
                rec["interruption"] = ""
                return rc, out, err
            _write_cut_marker(_resilience.NETWORK_CUT, _ckpt)
            return _resilience.NETWORK_CUT_RC, out, err
        return rc, out, err

    def _truncated(cmd: list, out: str, rc: int, candidate: "str | None") -> "dict | None":
        """C10a: after EVERY launch, regardless of rc — an unmeasured, unresumable stream never reaches tests."""
        trunc = _stream_truncation_verdict(cmd, out, rc)
        if trunc is None:
            return None
        if engine_executions:
            engine_executions[-1]["truncated"] = True
        return _fail(trunc["verdict"], trunc["why"], candidate)

    def _engine_step(step_name: str, kind: str, stall: int, prompt_fn, *, budget_label: str, fail_label: str,
                     strict: bool, candidate: "str | None", keep_done: bool = False,
                     extra_stage: "dict | None" = None) -> "dict | None":
        """One routed, budget-arbitrated launch with ONE bounded `--resume` after error_max_turns (the first launch
        is booked BEFORE the resume is arbitrated, L3-006). keep_done: a builder that committed and declared
        IMPLEMENTED before running out of turns is complete, not failed. strict: a missing CLI or a non-zero exit
        is terminal (MOTOR_UNAVAILABLE / sub-typed BLOCKED); a repair continues to the oracle regardless."""
        with state.step(step_name, state_path) as step:
            if state.is_step_done(step_name):
                return None
            choice = _route_and_record(kind, stall_count=stall)
            blocked = _budget_check(ledger, mission_id, step_name)
            if blocked:
                return _fail("BLOCKED", f"BUDGET_EXCEEDED before {budget_label}: {blocked['reason']}", candidate)
            prompt = prompt_fn()
            cmd = _mr.cli_args(choice, repo, prompt, budget_s=_remaining())
            t0 = time.monotonic()
            rc, out, err = _run_engine(cmd, choice, step_name)
            wall = round(time.monotonic() - t0, 2)
            resume_sid = _extract_resumable_session(out) if rc != 0 else None
            done = keep_done and bool(resume_sid) and _builder_declared_done(repo, base_sha)
            if done:
                _log(f"BUILD_COMPLETE_AT_MAX_TURNS: {mission_id} step={step_name} session_id={resume_sid} — "
                     "work committed + REPORT IMPLEMENTED, proceeding to tests")
                stages.append({"step": f"{step_name}_max_turns", "rc": rc, "status": "COMPLETE_AT_MAX_TURNS",
                               "session_id": resume_sid})
                rc = 0
            ti, to = _usage_for_budget(engine_executions[-1] if engine_executions else {})
            _budget_record(ledger, mission_id, choice.engine, step_name, ti, to, wall)
            if resume_sid and not done and _resume_allowed(ledger, mission_id, step_name, stages):
                step.session_id = resume_sid
                state.save(state_path)
                _log(f"RESUME_AFTER_MAX_TURNS: {mission_id} step={step_name} session_id={resume_sid}")
                cmd = _mr.cli_args(choice, repo, prompt, budget_s=_remaining(), resume_session=resume_sid)
                t0 = time.monotonic()
                rc, out, err = _run_engine(cmd, choice, step_name)
                wall_resume = round(time.monotonic() - t0, 2)
                if keep_done and rc != 0 and _extract_resumable_session(out) and _builder_declared_done(repo, base_sha):
                    stages.append({"step": f"{step_name}_max_turns", "rc": rc, "status": "COMPLETE_AT_MAX_TURNS",
                                   "session_id": resume_sid, "after_resume": True})
                    rc = 0
                ti, to = _usage_for_budget(engine_executions[-1] if engine_executions else {})
                _budget_record(ledger, mission_id, choice.engine, step_name, ti, to, wall_resume)
                wall = round(wall + wall_resume, 2)
            step.rc, step.duration_s = rc, wall
            step.note = "MOTOR_UNAVAILABLE" if rc == -2 else ("OK" if rc == 0 else "FAIL")
            text = _engine_text(cmd, out)
            stages.append({"step": step_name, "rc": rc, "status": step.note, "engine": choice.engine,
                           "model": choice.model, "tokens_in": ti, "tokens_out": to, "duration_s": wall,
                           "stdout_tail": text[-2000:], "stderr_tail": err[-1000:],
                           "resumed_session_id": resume_sid or "", **(extra_stage or {})})
            bail = _truncated(cmd, out, rc, candidate if candidate is not None else base_sha)
            if bail is not None:
                return bail
            if strict and rc == -2:
                return _fail("MOTOR_UNAVAILABLE", "CLI absente")
            if strict and rc != 0:   # FD3: sub-type BLOCKED from the worker's own output
                reason = classify_blocked_reason(text, err)
                return _fail(reason, f"{fail_label} failed: rc={rc}"
                             + (f" — sub-typed {reason}" if reason != "BLOCKED" else ""), base_sha)
        return None

    def _test_step(step_name: str, candidate: str, accepted_reason: str, *, allow_repair_once: bool = False,
                   on_red: "tuple | None" = None) -> "dict | None":
        """The mechanical oracle for one step → terminal receipt or None (continue). No test command at all is
        NO_TESTS_EXECUTED (LOT C32), never "tests verts" by omission; green goes through _accept_gate."""
        _write_heartbeat(out_path, step_name)
        with state.step(step_name, state_path) as step:
            if state.is_step_done(step_name):
                return None
            if not test_cmd:
                step.rc, step.duration_s, step.note = None, 0.0, "NO_TEST_CMD"
                stages.append({"step": step_name, "green": False, "rc": None, "status": "NO_TEST_CMD",
                               "duration_s": 0.0, "output_tail": "TEST_CMD_MISSING — no test command defined "
                                                                 "for this milestone/step"})
                return _fail("NO_TESTS_EXECUTED",
                             "aucune commande de test definie — build_glm a produit un candidat mais aucun test_N "
                             "n'a ete execute ; verdict non promotable, jamais ACCEPTED sans test reellement execute",
                             candidate, None)
            t0 = time.monotonic()
            ok, test_out = _run_test()
            step.rc, step.duration_s = (0 if ok else 1), round(time.monotonic() - t0, 2)
            step.note = "green" if ok else "red"
            stages.append({"step": step_name, "green": ok, "rc": step.rc, "status": step.note,
                           "duration_s": step.duration_s, "output_tail": test_out[-2000:]})
            if not ok:
                return _fail(on_red[0], on_red[1], candidate) if on_red else None
            _write_heartbeat(out_path, "tests_pass")
            return _accept_gate(candidate, accepted_reason, allow_repair_once=allow_repair_once)

    def _capture(step_name: str, snap) -> "tuple[str | None, dict | None]":
        """Factory-owned capture of the worker's changes + clean-tree proof → (candidate_sha, receipt or None)."""
        sha, refusal = _capture_worker_changes(repo, snap, mission_id, step_name, stages, deliverables=deliverables)
        if refusal is not None:
            return None, _fail("CANDIDATE_CAPTURE_REFUSED", refusal, _git(repo, "rev-parse", "HEAD"))
        candidate = sha if sha is not None else _git(repo, "rev-parse", "HEAD")
        dirty = _post_capture_gate(repo, step_name, stages)
        return candidate, (_fail(_capture_gate_verdict(dirty), dirty, candidate) if dirty is not None else None)

    def _test_output(step_name: str) -> str:
        return next((s.get("output_tail", "") for s in stages if s.get("step") == step_name), "")

    # --------------------------------------------------------- DIAGNOSE / REPLAN
    if task_kind in ("diagnose", "replan"):
        label = "DIAGNOSIS" if task_kind == "diagnose" else "REPLAN"
        if project_dir is None:   # nowhere durable to place the artifact: never silently drop the output
            return _fail("BLOCKED", f"{task_kind} requires --project-dir to place the recovery artifact — "
                                    "none was provided", base_sha)
        _write_heartbeat(out_path, task_kind)
        with state.step(task_kind, state_path) as step:
            if state.is_step_done(task_kind):   # died between the step and its receipt: fail closed, re-dispatch
                return _fail("BLOCKED", f"{task_kind} step already marked done but no receipt was recorded — "
                                        "fail-closed, re-dispatch", base_sha)
            choice = _route_and_record(task_kind)
            blocked = _budget_check(ledger, mission_id, task_kind)
            if blocked:
                return _fail("BLOCKED", f"BUDGET_EXCEEDED before {task_kind}: {blocked['reason']}", base_sha)
            prompt = (DIAGNOSE_PROMPT if task_kind == "diagnose" else REPLAN_PROMPT).format(
                task=task, repo=repo, budget_s=_remaining())
            cmd = _mr.cli_args(choice, repo, prompt, budget_s=_remaining())
            t0 = time.monotonic()
            rc, out, err = _run_engine(cmd, choice, task_kind)
            step.rc, step.duration_s = rc, round(time.monotonic() - t0, 2)
            ti, to = _usage_for_budget(engine_executions[-1] if engine_executions else {})
            _budget_record(ledger, mission_id, choice.engine, task_kind, ti, to, step.duration_s)
            text = _engine_text(cmd, out)
            bail = _truncated(cmd, out, rc, base_sha)
            if bail is not None:
                return bail
            if rc == -2:
                step.note = "MOTOR_UNAVAILABLE"
                return _fail("MOTOR_UNAVAILABLE", "CLI absente", base_sha)
            stage = {"step": task_kind, "rc": rc, "engine": choice.engine, "model": choice.model, "tokens_in": ti,
                     "tokens_out": to, "duration_s": step.duration_s, "stdout_tail": text[-2000:],
                     "stderr_tail": err[-1000:]}

            def _empty(why: str) -> dict:
                step.note = "EMPTY"
                stages.append({**stage, "status": "EMPTY"})
                return _fail(f"{label}_EMPTY", why, base_sha)

            if rc != 0:   # a non-zero exit is never evidence of a completed judgment (C13)
                return _empty(f"{task_kind} engine exited rc={rc} — no artifact accepted from a failed run")
            agent_text, why = _eo.recovery_agent_message(cmd, out)
            if not agent_text:
                return _empty(f"{task_kind} produced no agent message to parse (rc={rc}): {why}")
            parsed = _eo.extract_json_object(agent_text)
            if parsed is None:
                return _empty(f"{task_kind} produced no parseable JSON output (rc={rc}): JSON_NOT_FOUND")
            ok, why = _validate_recovery_artifact(task_kind, parsed)
            if not ok:
                return _empty(f"{task_kind} JSON schema invalid (rc={rc}): SCHEMA_INVALID:{why}")
            artifact = _eo.recovery_artifact_path(Path(project_dir), mission_id, task_kind)
            _write_json(artifact, parsed)
            step.note = "OK"
            stages.append({**stage, "status": "OK", "artifact": str(artifact)})
            return _fail(f"{label}_PRODUCED", f"{task_kind} → artifact written at {artifact}", base_sha)

    # --------------------------------------------------------- T0 build → T1 test
    candidate_sha = base_sha
    snap = _safe_snapshot(repo)
    _write_heartbeat(out_path, "build_glm")

    def _build_prompt() -> str:
        resume = _resume_preamble(repo, mission_id, base_sha)
        if resume:
            stages.append({"step": "resume_after_cut", "status": "RESUMED_FROM_WORKTREE",
                           "note": resume.splitlines()[3][:300]})
            _log(f"RESUME_AFTER_CUT: {mission_id} — builder continues from uncommitted work")
        return BUILDER_PROMPT.format(protocol=protocol, task=resume + task, repo=repo, budget_s=_remaining())

    done = _engine_step("build_glm", "build", 0, _build_prompt, budget_label="build", fail_label="build",
                        strict=True, candidate=None, keep_done=True)
    if done is None:
        candidate_sha, done = _capture("build_glm", snap)
    if done is None:
        done = _test_step("test_1", candidate_sha, "build → tests verts en T1", allow_repair_once=True)
    if done is not None:
        return done

    # --------------------------------------------------------- T2 repair (stall=1) → test
    if _remaining() < 120:
        return _fail("BLOCKED", "budget insuffisant pour repair (< 120 s)", candidate_sha)
    test_out_1 = _test_output("test_1")
    diff = _git(repo, "diff", base_sha, "HEAD", "--stat")
    snap = _safe_snapshot(repo)
    _write_heartbeat(out_path, "repair_glm")
    done = _engine_step("repair_glm", "repair", 1,
                        lambda: REPAIR_PROMPT.format(repo=repo, test_output=test_out_1[-3000:], diff=diff[-1000:],
                                                     budget_s=_remaining()),
                        budget_label="repair", fail_label="repair", strict=False, candidate=candidate_sha)
    if done is None:
        candidate_sha, done = _capture("repair_glm", snap)
    if done is None:
        done = _test_step("test_2", candidate_sha, "repair → tests verts en T2")
    if done is not None:
        return done

    # FD4 / L3-007: identical failures twice are a spec/test conflict ONLY for non-pytest outputs; a real pytest
    # oracle failing twice may just be an ineffective repair — that takes the bounded T3 escalation first.
    test_out_2 = _test_output("test_2")
    real_pytest = (_has_pytest_markers(test_out_1) or _has_pytest_markers(test_out_2)) and (
        real_pytest_report(test_out_1) or real_pytest_report(test_out_2))
    if not real_pytest and detect_spec_test_conflict(test_out_1, test_out_2):
        return _fail(SPEC_TEST_CONFLICT, "test_1 et test_2 échouent sur les mêmes tests — conflit spec/contrat, "
                                         "pas une erreur d'implémentation; réécriture du contrat requise", candidate_sha)

    # --------------------------------------------------------- T3 escalade (stall=2) → test
    if _remaining() < 300:
        return _fail("BLOCKED", "budget insuffisant pour escalade T3 (< 300 s)", candidate_sha)
    _write_heartbeat(out_path, "build_claude")
    done = _engine_step("build_claude", "repair", 2, lambda: build_t3_repair_prompt(
        mission_id=mission_id, base_sha=base_sha, head_sha=_git(repo, "rev-parse", "HEAD"), test_cmd=test_cmd,
        test_output=test_out_2, diff_stat=_git(repo, "diff", base_sha, "HEAD", "--stat"),
        failing_files=_extract_failing_files(test_out_1, test_out_2), budget_s=_remaining()),
        budget_label="T3 escalation", fail_label="build_claude", strict=True, candidate=candidate_sha,
        extra_stage={"prompt_kind": "T3_REPAIR"})
    if done is not None:
        return done
    candidate_sha = _git(repo, "rev-parse", "HEAD")   # T3 relies on the worker committing itself (X105)
    dirty = _post_capture_gate(repo, "build_claude", stages)
    if dirty is not None:
        return _fail(_capture_gate_verdict(dirty), dirty, candidate_sha)
    done = _test_step("test_3", candidate_sha, "T3 escalation → tests verts",
                      on_red=(VIOLATED, "T3 escalation échoue aussi — test oracle says code is wrong: VIOLATED"))
    return done if done is not None else _fail("BLOCKED", "état inattendu — fail-closed", candidate_sha)


def _receipt(
    mission_id: str, started_at: str, base_sha: str, candidate_sha: str | None, stages: list, verdict: str,
    why: str, out_path: Path, state: MissionState, state_path: Path, routing_decisions: list[dict] | None = None,
    engine_executions: list[dict] | None = None, test_command: str | None = None,
) -> dict:
    """The receipt: written atomically on EVERY exit path, then the mission state is finished."""
    rec = {
        "schema": SCHEMA, "mission_id": mission_id, "started_at": started_at, "ended_at": _now(),
        "elapsed_step_s": round(sum(s.get("duration_s", 0) for s in stages if isinstance(s, dict)), 2),
        "base_sha": base_sha, "candidate_sha": candidate_sha, "verdict": verdict, "verdict_reason": why,
        "test_command": test_command,                    # None = NO_TESTS_EXECUTED case, never silently absent
        "promotable": verdict == "ACCEPTED",
        "routing_decisions": routing_decisions or [],    # what route() asked for
        "engine_executions": engine_executions or [],    # what the OS ran — the only non-forgeable identity
        "stages": stages,
    }
    _write_json(out_path, rec)
    state.finish(verdict, state_path)
    _write_heartbeat(out_path, "verdict", status=verdict)
    print(f"{mission_id}: {verdict} — {why}", flush=True)
    print(f"reçu → {out_path}", flush=True)
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description="dispatcher-v3 : model_router + budget_ledger")
    for flag, kw in (
        ("--repo", {"required": True}), ("--task-file", {"required": True}), ("--mission-id", {"required": True}),
        ("--test-cmd", {"default": None}), ("--budget-s", {"type": int, "default": 3600}), ("--out", {"required": True}),
        ("--spec-level", {"default": "B", "choices": ["S", "A", "B"], "help": "task spec level for model_router"}),
        ("--max-tokens", {"type": int, "default": None, "help": "token ceiling for this mission (budget_ledger)"}),
        ("--max-model-calls", {"type": int, "default": None, "help": "model call ceiling (enforced even when tokens unknown)"}),
        ("--ledger", {"default": None, "help": "path to budget JSONL ledger file"}),
        ("--preflight", {"action": "store_true", "help": "run check_task preflight gate before dispatch"}),
        ("--skip-providers", {"action": "store_true", "help": "pass --skip-providers to check_task (offline / CI)"}),
        ("--plan-file", {"default": None, "help": "mission plan JSON (component/touched_paths/...) for CTO004 context injection"}),
        ("--project-dir", {"default": None, "help": "V2 canonical project directory (STRATEGY.md/SPEC.md/ROADMAP.yaml); "
                                                   "context injection is FAIL-CLOSED on it — STALE_CTO_CONTEXT_DISPATCH = 0"}),
        ("--project", {"default": None, "help": "canonical project identity, for project-scoped routing policy"}),
        ("--engine-hint", {"default": None, "help": "F5R5: preferred engine from the escalation ladder for the "
                                                   "initial BUILD call (claude|glm|zcode|codex)"}),
        ("--test-cmd-is-product-e2e", {"action": "store_true", "default": False,
                                       "help": "LOT C47: --test-cmd is byte-identical to the milestone's product_e2e.test_command"}),
        ("--deliverable", {"action": "append", "default": None,
                           "help": "LOT C28 (b): a path (relative to --repo) the milestone declares as required output "
                                   "(repeatable), proven with `git cat-file -e <candidate_sha>:<path>` before ANY ACCEPTED; "
                                   "missing → CANDIDATE_MISSING_DELIVERABLE. Populated by the executor from the roadmap."}),
    ):
        ap.add_argument(flag, **kw)
    ap.add_argument("--task-kind", default="build", choices=["build", "diagnose", "replan"],
                    help="build (T0 build → test → T2 repair → test → T3 escalate → test) or diagnose/replan (one "
                         "read-only judgment launch; the dispatcher writes the artifact; requires --project-dir)")
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    if not (repo / ".git").exists():
        print(f"ERREUR : {repo} n'est pas un dépôt git", file=sys.stderr)
        return 2
    # No startup refusal here: the guard sits at the execve boundary in _run(), where the distinction between a
    # stubbed and a real launch is exact and no caller can route around it.
    import check_task as _ct
    if args.preflight:
        try:
            pf = _ct.run_checks(repo, _ct.resolve_task_file(repo, args.task_file), args.mission_id, args.skip_providers)
            if not pf["ok"]:
                print(f"PREFLIGHT_FAIL — {', '.join(pf['failures'])}", file=sys.stderr)
                return 3
            print(f"PREFLIGHT_PASS  ({len(pf['checks'])} checks, {pf['elapsed_s']}s)")
        except Exception as exc:
            print(f"ERREUR preflight : {exc}", file=sys.stderr)
            return 3
    ledger: Optional[BudgetLedger] = None
    if args.ledger or args.max_tokens or args.max_model_calls:
        ledger = BudgetLedger(args.ledger or (Path(args.out).parent / f"budget_{args.mission_id}.jsonl"),
                              max_tokens=args.max_tokens, max_model_calls=args.max_model_calls)
    task = _ct.resolve_task_file(repo, args.task_file).read_text(encoding="utf-8")
    _rb = os.environ.get("FACTORY_REVIEW_BLOCK_FILE", "")   # a rebuild ordered by a review BLOCK answers it first
    if _rb and Path(_rb).is_file():
        try:
            task = REVIEW_BLOCK_PREAMBLE + Path(_rb).read_text(encoding="utf-8").strip() + "\n\n---\n\n" + task
        except OSError:
            pass
    project_dir = Path(args.project_dir).resolve() if args.project_dir else None
    rec = _run_in_worktree(args.mission_id, repo, task, args.test_cmd, args.budget_s, Path(args.out),
                           spec_level=args.spec_level, ledger=ledger,
                           plan_file=Path(args.plan_file) if args.plan_file else None, project_dir=project_dir,
                           engine_hint=args.engine_hint, project=args.project, task_kind=args.task_kind or "build",
                           deliverables=args.deliverable, test_cmd_is_product_e2e=bool(args.test_cmd_is_product_e2e))
    return 0 if rec["verdict"] == "ACCEPTED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
