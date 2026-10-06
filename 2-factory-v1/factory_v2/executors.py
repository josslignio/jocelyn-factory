"""Executors — adaptateurs d'exécution bornés (interface minuscule).

LocalCommandExecutor: commande shell déterministe (tests, probes).
AutopilotExecutor: WRAP de dispatcher_v3.py du champion — Claude/GLM/Codex réels.
  V2 reste responsable de: authority, roadmap, validation contract, milestone lifecycle.
  L'executor ne décide pas de la direction produit.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .mission_compiler import MissionCompileError, compile_missions
from .mission_evidence import (
    HeartbeatRenewer, clear_worker_state, get_process_birth_time, write_heartbeat,
)
from .process_control import ProcessGroupHandle, capture_pgid, terminate_process_group


def _reap_group_on_failure(proc, handle) -> None:
    """Terminate + reap a worker group after a post-Popen failure (L2_runtime_core-005).
    Best-effort: the exception that brought us here is what the caller raises."""
    if proc is None or proc.poll() is not None:
        return
    try:
        terminate_process_group(handle if handle is not None else proc)
    except Exception:
        pass
    try:
        proc.communicate(timeout=10)
    except Exception:
        try:
            proc.kill()
            proc.communicate(timeout=5)
        except Exception:
            pass

from products.twitter_radar import weekly_observation as WO
from products.twitter_radar.tr_refresh_harness import corpus_facts

_REPO_ROOT = Path(__file__).parents[1]
_DISPATCHER = _REPO_ROOT / "factory" / "bin" / "dispatcher_v3.py"
_MODEL_ROUTER = _REPO_ROOT / "factory" / "bin"


from .mission_evidence import control_repo_env  # noqa: E402


def resolve_execution_cwd(project_dir: Path, executor_spec: dict) -> Path:
    """Resolve the working directory for a local_command executor.

    Defect 1: commands in ROADMAP.yaml run relative to project_dir by default
    (e.g., projects/factory/), but factory_v2/run_f5_benchmark.py lives at
    repo root. The 'cwd' field in the executor spec selects the directory:

      cwd: control_repo   → repo root (_REPO_ROOT)
      cwd: project_dir    → project_dir (default, backward compat)
      cwd: product_repo   → product_repo path (from executor spec, must be absolute)
      cwd: <absent>       → project_dir (backward compat; ROADMAP.yaml should be explicit)

    Raises MissionCompileError for unknown cwd values.
    """
    cwd_hint = executor_spec.get("cwd", "project_dir")
    if cwd_hint == "control_repo":
        return _REPO_ROOT
    if cwd_hint == "project_dir":
        return project_dir
    if cwd_hint == "product_repo":
        product_repo = executor_spec.get("product_repo")
        if not product_repo:
            from .mission_compiler import MissionCompileError
            raise MissionCompileError(
                "executor.cwd=product_repo requires executor.product_repo to be set"
            )
        p = Path(product_repo)
        if not p.is_dir():
            from .mission_compiler import MissionCompileError
            raise MissionCompileError(
                f"executor.product_repo path does not exist: {product_repo!r}"
            )
        return p
    from .mission_compiler import MissionCompileError
    raise MissionCompileError(
        f"executor.cwd={cwd_hint!r} is not a valid cwd specifier. "
        f"Valid: 'control_repo', 'project_dir', 'product_repo'."
    )


def _receipt_fingerprint(path: Path):
    """Identity of a file's current bytes-on-disk state: None when absent.

    (inode, size, mtime_ns) — a rewrite of identical content still moves the
    mtime, so "fingerprint changed" reliably means "written during this
    execution" for the stale-receipt guard (L2_runtime_core-017).
    """
    try:
        st = Path(path).stat()
    except OSError:
        return None
    return (st.st_ino, st.st_size, st.st_mtime_ns)


def _read_dispatch_sidecar(receipt_path: Path) -> list:
    """Launch observations the dispatcher fsynced as each launch ended.

    Survives a group-kill of the dispatcher, which the receipt does not. Named
    off the receipt path so no extra plumbing is needed to find it.
    """
    try:
        sidecar = receipt_path.with_name(receipt_path.stem + ".telemetry.jsonl")
        if not sidecar.is_file():
            return []
        rows = []
        for line in sidecar.read_text(encoding="utf-8",
                                      errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue          # torn tail: the kill landed mid-write
        return rows
    except OSError:
        return []


def _merge_observations(primary: list, secondary: list) -> list:
    """Union on observation_key, keeping the latest durable state for a launch."""
    out, positions = [], {}
    for row in list(primary) + list(secondary):
        if not isinstance(row, dict):
            continue
        key = row.get("observation_key")
        if key and key in positions:
            out[positions[key]] = row
            continue
        if key:
            positions[key] = len(out)
        out.append(row)
    return out


def _utc_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso_from_monotonic_start(t0: float) -> str:
    """Wall-clock instant corresponding to a monotonic reading taken earlier.

    The monotonic clock is the right one for a DURATION and useless as a
    timestamp. Reconstructing the start instant from it keeps both honest:
    durations stay immune to clock jumps, and the recorded start is still a real
    UTC instant that other records can be ordered against.
    """
    from datetime import datetime, timedelta, timezone
    elapsed = max(0.0, time.monotonic() - t0)
    return (datetime.now(timezone.utc) - timedelta(seconds=elapsed)).isoformat(
        timespec="seconds")


@dataclass(frozen=True)
class ExecutionResult:
    mission_id: str
    rc: int
    output_tail: str
    engine: str = ""
    candidate_sha: str = ""
    integration_sha: str = ""   # post-merge HEAD (exact SHA the validator must test against)
    wall_time_s: float = 0.0
    verdict: str = ""
    receipt: dict = field(default_factory=dict, compare=False, hash=False)
    # F4: the process observation that engine identity is bound to.  Empty dict means
    # "engine identity unobserved" — callers must fail closed, never fall back to a hint.
    engine_execution: dict = field(default_factory=dict, compare=False, hash=False)
    # Every measured engine launch this execution produced (telemetry/launch-observation).
    # Empty means nothing was measured, which is a coverage gap the report names —
    # it never means the launches were free.
    telemetry_observations: list = field(default_factory=list, compare=False, hash=False)
    # Wall time per named dispatcher step: queue/build/test/repair/settlement are
    # separable rather than collapsed into one duration.
    phase_times_s: dict = field(default_factory=dict, compare=False, hash=False)

    @property
    def ok(self) -> bool:
        return self.rc == 0


def _shell_run(cmd: str, cwd: Path, timeout_s: int) -> tuple[int, str]:
    """Canonical shell execution: bash errexit+pipefail, classified timeout.

    P0-C invariants:
    - set -eo pipefail: pipelines fail on any non-zero stage (false | true → FAIL)
    - TimeoutExpired → rc=124 (classified, never uncaught)
    - Captured stdout+stderr, always returned
    - No orphan processes left on timeout (communicate(timeout) + kill)

    Returns (rc, combined_output). rc=124 means timeout.
    """
    try:
        # The same environment the independent validator already builds. Milestones
        # declare their proof as `${FACTORY_CONTROL_REPO:?...}` — "the supervisor
        # sets it" — and authority_gate.IndependentValidator does. This executor did
        # not, so the identical command failed closed here while passing there.
        #
        # 15 milestones across both projects declare that variable, including the
        # next milestone of each. Nothing in the repo ever set it outside an agent's
        # hand-typed `export`, which made a routine human intervention a structural
        # precondition for settling any of them. Reusing the existing helper rather
        # than re-deriving the root keeps both paths on one definition.
        proc = subprocess.Popen(
            ["bash", "-c", f"set -eo pipefail\n{cmd}"],
            cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True, env=control_repo_env(),
        )
        handle = capture_pgid(proc)
        try:
            out_b, err_b = proc.communicate(timeout=timeout_s)
            rc = proc.returncode
            combined = (out_b.decode(errors="replace") + err_b.decode(errors="replace"))
            return rc, combined
        except subprocess.TimeoutExpired:
            terminate_process_group(handle)
            out_b, err_b = proc.communicate()
            combined = (out_b.decode(errors="replace") + err_b.decode(errors="replace"))
            return 124, f"TIMEOUT after {timeout_s}s\n" + combined
    except FileNotFoundError:
        return 127, f"COMMAND_NOT_FOUND: {cmd[:200]}"


class LocalCommandExecutor:
    """Commande shell déterministe — pour tests et probes.

    P0-C: uses _shell_run() with bash errexit+pipefail. False|true in a pipeline
    fails the step. TimeoutExpired classified as rc=124 (never uncaught).
    """

    def __init__(self, project_dir: Path, timeout_s: int = 120):
        self.project_dir = Path(project_dir)
        self.timeout_s = timeout_s

    def execute(self, mission) -> ExecutionResult:
        cmd = mission.executor.get("command", "")
        # Defect 1: resolve cwd from executor spec (control_repo/project_dir/product_repo).
        exec_cwd = resolve_execution_cwd(self.project_dir, mission.executor)
        t0 = time.monotonic()
        renewer = None
        rc = 1
        output = ""
        engine_execution: dict = {}
        proc = None
        handle = None
        try:
            # start_new_session=True: child gets its own process group (PGID=child PID).
            # This ensures killpg(child_pgid) cannot accidentally kill factoryd.
            # Same control-repo environment as _shell_run and the independent
            # validator. Fixing _shell_run alone was not enough: this executor has
            # its OWN Popen, so the milestone command still ran without
            # FACTORY_CONTROL_REPO and failed closed on the variable its own
            # product_e2e declares.
            proc = subprocess.Popen(
                ["bash", "-c", f"set -eo pipefail\n{cmd}"],
                cwd=exec_cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True, env=control_repo_env(),
            )
            handle = capture_pgid(proc)
            birth = get_process_birth_time(proc.pid)
            # F4: identity of the process the OS actually started, observed here.
            engine_execution = {
                # canonical engine id ("local"), so state/ladder/rotation keep using
                # the same vocabulary as before; argv0 records what actually ran.
                "engine_id": "local",
                "argv0": "bash",
                "pid": proc.pid,
                "pgid": handle.pgid,
                "birth_time": birth,
                "observed": True,
                "source": "LocalCommandExecutor.Popen",
            }
            # Initial heartbeat with identity fields
            # LR2: read lease_generation from executor spec (injected by runtime.py).
            # generation is no longer decorative (D-F5R5c resolved).
            _generation = mission.executor.get("_lease_generation", "")
            write_heartbeat(
                self.project_dir, mission.id, proc.pid,
                phase="executing", pgid=handle.pgid, birth_time=birth,
                generation=_generation,
            )
            # F5R2: background thread renews heartbeat so long workers are not false orphans
            renewer = HeartbeatRenewer(
                self.project_dir, mission.id, proc.pid,
                pgid=handle.pgid, birth_time=birth,
                generation=_generation,
                fencing_token=mission.executor.get("_lease_fencing_token", ""),
            )
            renewer.start()
            try:
                timeout_s = int(mission.executor.get("budget_s", self.timeout_s))
                out_b, err_b = proc.communicate(timeout=timeout_s)
                rc = proc.returncode
                output = out_b.decode(errors="replace") + err_b.decode(errors="replace")
            except subprocess.TimeoutExpired:
                terminate_process_group(handle)
                out_b, err_b = proc.communicate()
                output = out_b.decode(errors="replace") + err_b.decode(errors="replace")
                rc = 124
        except FileNotFoundError:
            rc, output = 127, f"COMMAND_NOT_FOUND: {cmd[:200]}"
        except BaseException:
            # L2_runtime_core-005: any failure after Popen (heartbeat OSError,
            # interruption…) used to fall through to clear_worker_state with
            # the child still running — an orphan writing into a worktree whose
            # state says "no worker". Terminate and reap the group first.
            _reap_group_on_failure(proc, handle)
            raise
        finally:
            if renewer is not None:
                renewer.stop()
            clear_worker_state(self.project_dir, mission.id)
        wall = time.monotonic() - t0
        if engine_execution:
            engine_execution["rc"] = rc
        captured_sha = ""
        if rc == 0 and mission.executor.get("capture_product_head"):
            product_repo = Path(str(mission.executor.get("product_repo") or ""))
            probe = subprocess.run(["git", "rev-parse", "HEAD"], cwd=product_repo,
                                   capture_output=True, text=True, timeout=15)
            if probe.returncode != 0:
                rc = 1
                output += "\nPRODUCT_HEAD_CAPTURE_FAILED: " + probe.stderr[-300:]
            else:
                captured_sha = probe.stdout.strip()
        # A shell step calls no provider, so it has no tokens and no quota — but
        # it does have wall time, and that time is part of what a validated
        # change costs. Measuring it keeps "the model was slow" distinguishable
        # from "the test suite was slow" instead of merging both into one number.
        observations = []
        try:
            from .telemetry import observe as _telemetry
            obs = _telemetry.observe_launch(
                argv=["bash", "-c"], stdout=None,
                started_at=_iso_from_monotonic_start(t0), ended_at=_utc_iso(),
                wall_time_s=wall, rc=rc, engine="local",
                role="test/probe", lineage=_telemetry.TEST,
                project=self.project_dir.name, mission=mission.id,
                task_type=mission.executor.get("type", "local_command"),
                pid=engine_execution.get("pid"))
            observations = [obs.as_dict()]
        except Exception:
            # Never let the meter break the step it is measuring. The absence
            # shows up as missing coverage in the report, which is the honest
            # outcome of a broken meter.
            observations = []
        return ExecutionResult(
            mission_id=mission.id,
            rc=rc,
            output_tail=output[-2000:],
            engine=engine_execution.get("engine_id", ""),
            candidate_sha=((captured_sha or mission.executor.get(
                "_validated_integration_sha", "")) if rc == 0 else ""),
            integration_sha=((captured_sha or mission.executor.get(
                "_validated_integration_sha", "")) if rc == 0 else ""),
            wall_time_s=wall,
            verdict="ACCEPTED" if rc == 0 else "BLOCKED",
            engine_execution=engine_execution,
            telemetry_observations=observations,
            phase_times_s={"local_command": round(wall, 3)},
        )


class WeeklyRefreshObservedExecutor:
    """Supervisor-owned call site for TR_WEEKLY_DEEP_REFRESH_V1's scheduled leg.

    Runs the milestone's real refresh command exactly once, through the
    weekly-refresh observation helper's `observe_weekly_refresh` — never re-implements the
    run, never trusts a caller-supplied hash claim. On success the resulting
    supervisor-owned `WeeklyRefreshObservation` is stored into the canonical
    control-side STORE (or an explicit constructor-only `observation_dir`
    override, tests only), outside every product worktree. A
    refused/failed/timed-out run stores nothing: the absence of an
    observation is itself the honest result, never papered over with a
    partial or synthetic one.

    mission.executor fields (validated at compile time by mission_compiler):
      command        — the exact refresh invocation to run and observe
      refresh_seq    — 1 or 2
      product_repo   — the product tree to run in and recompute hashes from
      trigger_source — default 'scheduler'
      budget_s       — subprocess timeout, default 900

    `observation_dir` is deliberately NOT a mission.executor field: it is a
    constructor-only parameter (tests only — production's `_make_executor`
    never passes it). mission.executor is data a mission author ultimately
    controls via roadmap YAML; reading a store-location override from it
    would let a mission redirect where the supervisor-owned observation
    lands (inside the product repo, or anywhere else). mission_compiler
    additionally rejects an `observation_dir` key on this executor type at
    compile time — this constructor param is the only way to override it.
    """

    def __init__(self, project_dir: Path, observation_dir: "Path | None" = None):
        self.project_dir = Path(project_dir)
        self._observation_dir = observation_dir

    def execute(self, mission) -> ExecutionResult:

        exc = mission.executor
        command = exc.get("command", "")
        refresh_seq = exc.get("refresh_seq")
        trigger_source = exc.get("trigger_source", "scheduler")
        timeout_s = int(exc.get("budget_s", 900))

        product_repo_str = str(exc.get("product_repo") or "")
        repo_path = Path(product_repo_str)
        if not repo_path.is_absolute():
            repo_path = (_REPO_ROOT / product_repo_str).resolve()
        if not repo_path.is_dir():
            return ExecutionResult(
                mission_id=mission.id, rc=1,
                output_tail=f"WEEKLY_REFRESH_BAD_PRODUCT_REPO: "
                            f"{product_repo_str!r} (resolved: {repo_path}) "
                            f"does not exist.",
                verdict="BLOCKED",
            )

        pre_head = _git_head(repo_path)

        t0 = time.monotonic()
        engine_execution: dict = {}
        obs, why = WO.observe_weekly_refresh(
            refresh_seq=refresh_seq, command=command, cwd=repo_path,
            repo=repo_path, trigger_source=trigger_source, timeout=timeout_s,
            execution_out=engine_execution)
        wall = time.monotonic() - t0
        if obs is None:
            return ExecutionResult(
                mission_id=mission.id, rc=1,
                output_tail=f"WEEKLY_REFRESH_REFUSED: {why}",
                engine=engine_execution.get("engine_id", ""),
                wall_time_s=wall, verdict="BLOCKED",
                engine_execution=engine_execution,
            )

        obs_dir = self._observation_dir if self._observation_dir else WO.STORE
        stored = WO.store_observation(obs, obs_dir)
        integration_sha = ""
        if exc.get("commit_outputs"):
            integration_sha, commit_error = _commit_weekly_outputs(repo_path)
            if commit_error:
                return ExecutionResult(
                    mission_id=mission.id, rc=1,
                    output_tail=f"WEEKLY_OUTPUT_COMMIT_REFUSED: {commit_error}",
                    engine=engine_execution.get("engine_id", ""),
                    wall_time_s=wall, verdict="BLOCKED",
                    engine_execution=engine_execution,
                )
        # weekly-refresh-observation/v2 for the hardened weekly-operate
        # verifier (see products' Twitter Radar package) -- best-effort,
        # additive: it never turns this (already-ACCEPTED, v1-proven) mission
        # into a failure. That milestone is `optional: true` in ROADMAP.yaml;
        # a refresh_seq that does not commit (e.g. leg 1, which carries no
        # `commit_outputs: true`) legitimately produces no v2 evidence.
        v2_note = ""
        v2_receipt_path_str = ""
        # LOT C23: typed cause surfaced (never just free-text v2_note) so a
        # caller enforcing "no receipt => refuse" (weekly_refresh_observed_cli)
        # can build a machine-readable V2_RECEIPT_NOT_PRODUCED:<cause> reason
        # instead of re-parsing output_tail's prose.
        v2_reason_code = ""
        v2_record, v2_reason = WO.build_weekly_refresh_v2(
            obs=obs, repo=repo_path, pre_head=pre_head, post_head=integration_sha)
        if v2_record is None:
            v2_note = f" v2_receipt_skipped={v2_reason}"
            v2_reason_code = v2_reason
        else:
            v2_path, v2_err = WO.write_v2_receipt(repo_path, v2_record)
            if v2_err:
                v2_note = f" v2_receipt_write_failed={v2_err}"
                v2_reason_code = f"WRITE_FAILED:{v2_err}"
            else:
                commit_err2 = _commit_weekly_refresh_v2_receipt(repo_path, v2_path)
                v2_note = (f" v2_receipt={v2_path.name}" if not commit_err2
                          else f" v2_receipt_commit_failed={commit_err2}")
                if not commit_err2:
                    v2_receipt_path_str = str(v2_path)
                else:
                    v2_reason_code = f"COMMIT_FAILED:{commit_err2}"

        return ExecutionResult(
            mission_id=mission.id, rc=0,
            output_tail=f"WEEKLY_REFRESH_STORED: "
                        f"{stored['observation_id']} in {obs_dir}{v2_note}",
            engine=engine_execution.get("engine_id", ""),
            wall_time_s=wall, verdict="ACCEPTED",
            engine_execution=engine_execution,
            candidate_sha=integration_sha,
            integration_sha=integration_sha,
            # LOT C17: factoryctl weekly-refresh-observed's JSON output needs the
            # exact receipt location without re-parsing output_tail's free text.
            # LOT C23: v2_reason carries the typed cause when v2_path is empty.
            receipt={"observation_id": stored["observation_id"],
                    "obs_dir": str(obs_dir), "v2_path": v2_receipt_path_str,
                    "v2_reason": v2_reason_code},
        )


def _git_head(repo: Path) -> str:
    """Read-only: HEAD of `repo`, or "" if unresolvable (not a git repo,
    unborn HEAD, ...). Never raises."""
    proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                          capture_output=True, text=True, timeout=30)
    out = proc.stdout.strip()
    return out if proc.returncode == 0 and len(out) == 40 else ""


def _commit_weekly_refresh_v2_receipt(repo: Path, receipt_path: Path) -> str:
    """Commit exactly the one weekly-refresh-observation/v2 receipt file
    the weekly-refresh observation helper's `write_v2_receipt` just wrote --
    strict pathspec (never `git add -A`), typed commit message. This is the
    ONLY writer authorized to commit into the product repo's
    weekly-refresh-observation/v2 receipts directory.

    Returns "" on success (including the idempotent case where the file's
    content already matches what is committed -- nothing to add), or a
    typed reason string on failure. Never raises.
    """
    repo = Path(repo)
    try:
        rel = receipt_path.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        return "RECEIPT_PATH_OUTSIDE_REPO"
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"], cwd=str(repo),
        capture_output=True, text=True, timeout=30)
    if staged.returncode != 0:
        return f"STAGED_STATE_UNOBSERVABLE:{staged.stderr.strip()[:200]}"
    if staged.stdout.strip():
        return "PREEXISTING_STAGED_CHANGES"
    added = subprocess.run(["git", "add", "--", rel], cwd=str(repo),
                           capture_output=True, text=True, timeout=30)
    if added.returncode != 0:
        return f"GIT_ADD_FAILED:{added.stderr.strip()[:200]}"
    changed = subprocess.run(["git", "diff", "--cached", "--quiet"],
                             cwd=str(repo), timeout=30)
    if changed.returncode == 0:
        return ""  # identical bytes already committed -- idempotent retry
    if changed.returncode != 1:
        return "STAGED_DIFF_UNOBSERVABLE"
    # CODEX_REVIEW_C13 finding 5 (MOYENNE): the pre-check above (lines
    # 475-481) only observes the index at one instant; a race (a concurrent
    # process, a commit hook that stages something) between that check and
    # this commit could still land unrelated staged files in the same
    # commit under a plain `git commit`. `--only -- rel` makes the commit's
    # CONTENT itself scoped to `rel` regardless of what else is staged at
    # commit time — it is the pathspec restriction, not just the earlier
    # observation, that this receipt commit is confined to.
    committed = subprocess.run(
        ["git", "commit", "--only", "-m",
         "data(tr-weekly): record weekly-refresh-observation/v2 receipt",
         "--", rel],
        cwd=str(repo), capture_output=True, text=True, timeout=120)
    if committed.returncode != 0:
        subprocess.run(["git", "reset", "--", rel], cwd=str(repo),
                       capture_output=True, timeout=30)
        return f"GIT_COMMIT_FAILED:{committed.stderr.strip()[:300]}"
    return ""


def _commit_weekly_outputs(repo: Path) -> tuple[str, str]:
    """Commit only the final corpus/report evidence and return its exact SHA."""
    repo = Path(repo)
    probe = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"], cwd=str(repo),
        capture_output=True, text=True, timeout=30)
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        return "", "PRODUCT_REPO_NOT_GIT"
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"], cwd=str(repo),
        capture_output=True, text=True, timeout=30)
    if staged.returncode != 0:
        return "", f"STAGED_STATE_UNOBSERVABLE:{staged.stderr.strip()[:200]}"
    if staged.stdout.strip():
        return "", "PREEXISTING_STAGED_CHANGES"
    added = subprocess.run(
        ["git", "add", "--", "data/corpus", "research"], cwd=str(repo),
        capture_output=True, text=True, timeout=60)
    if added.returncode != 0:
        return "", f"GIT_ADD_FAILED:{added.stderr.strip()[:200]}"
    changed = subprocess.run(
        ["git", "diff", "--cached", "--quiet"], cwd=str(repo), timeout=30)
    if changed.returncode == 1:
        committed = subprocess.run(
            # L2_runtime_core-006: `--only -- <paths>` confines the commit's
            # CONTENT to the weekly outputs regardless of what else lands in
            # the index between the staged-clean pre-check above and this
            # commit (same pattern as the receipt commit above). A plain
            # `git commit` would absorb any foreign staged file.
            ["git", "commit", "--only", "-m",
             "data(tr-weekly): record supervisor-observed refresh",
             "--", "data/corpus", "research"],
            cwd=str(repo), capture_output=True, text=True, timeout=120)
        if committed.returncode != 0:
            subprocess.run(["git", "reset", "--", "data/corpus", "research"],
                           cwd=str(repo), capture_output=True, timeout=30)
            return "", f"GIT_COMMIT_FAILED:{committed.stderr.strip()[:300]}"
    elif changed.returncode != 0:
        return "", "STAGED_DIFF_UNOBSERVABLE"
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True,
        text=True, timeout=30)
    if head.returncode != 0:
        return "", f"PRODUCT_HEAD_UNOBSERVABLE:{head.stderr.strip()[:200]}"
    return head.stdout.strip(), ""


class AutopilotExecutor:
    """WRAP de dispatcher_v3.py — lance de vraies IA (Claude/GLM/Codex) via le champion.

    La mission doit fournir dans mission.executor:
      task_file   : chemin vers le fichier de tâche (relatif à repo)
      test_cmd    : commande de test (str ou null)
      spec_level  : 'S'|'A'|'B' (défaut 'B')
      budget_s    : budget wall-time (défaut: self.default_budget_s)
      engine_hint : 'claude'|'glm'|'codex' (optionnel — routing par model_router si absent)

    V2 runtime remain responsible for authority/roadmap/contract/settlement.
    """

    def __init__(self, repo: Path, default_budget_s: int = 1800,
                 project_dir: "Path | None" = None,
                 engine_hint: str = ""):
        self.repo = Path(repo)
        self.default_budget_s = default_budget_s
        # P6 fix: when project_dir is provided, dispatcher_v3 loads V2 canonical
        # authority (STRATEGY.md/SPEC.md) fail-closed instead of CTO_STRATEGY.md.
        # STALE_CTO_CONTEXT_DISPATCH = 0 on the V2 authoritative path.
        self.project_dir = Path(project_dir) if project_dir else None
        # F5R5: engine_hint from escalation ladder ALTERNATE_ENGINE stage.
        # Overrides model_router routing table for the initial BUILD call.
        self.engine_hint = engine_hint
        if not _DISPATCHER.exists():
            raise FileNotFoundError(f"dispatcher_v3.py not found at {_DISPATCHER}")

    def execute(self, mission) -> ExecutionResult:
        exc = mission.executor
        # Structural guard: this is the only executor that spends money.  A test that
        # exercises a recovery chain must never reach a real model by accident, so the
        # dispatch is refused outright when the guard is set, rather than relying on
        # every test remembering to inject a stub.  Production never sets it.
        if os.environ.get("FACTORY_NO_REAL_ENGINE") == "1":
            return ExecutionResult(
                mission_id=mission.id, rc=1,
                output_tail=("REAL_ENGINE_DISABLED: FACTORY_NO_REAL_ENGINE=1 — refusing "
                             "to dispatch a real CLI model (subscription quota). Inject a stub executor."),
                verdict="BLOCKED",
            )
        task_file = exc.get("task_file", "")
        test_cmd = exc.get("test_cmd")
        spec_level = exc.get("spec_level", "B")
        budget_s = int(exc.get("budget_s", self.default_budget_s))

        # WRONG_REPO_DISPATCH fix: use product_repo from executor dict if present.
        # product_repo is injected by compile_missions() from ROADMAP.yaml.
        # Fail closed on invalid path: missing or non-existent product_repo for autopilot
        # missions → EXECUTOR_ERROR (never silently dispatch to wrong repo).
        product_repo_str = exc.get("product_repo", "")
        if product_repo_str:
            repo_path = Path(product_repo_str)
            if not repo_path.is_absolute():
                repo_path = (_REPO_ROOT / product_repo_str).resolve()
            if not repo_path.exists():
                return ExecutionResult(
                    mission_id=mission.id, rc=1,
                    output_tail=f"WRONG_REPO_DISPATCH: product_repo {product_repo_str!r} "
                                f"(resolved: {repo_path}) does not exist. "
                                "Update ROADMAP.yaml product_repo to the correct product repo path.",
                    wall_time_s=0.0, verdict="BLOCKED",
                )
            effective_repo = repo_path
        else:
            effective_repo = self.repo

        attempt_key = str(exc.get("_attempt_key") or "")
        if self.project_dir is not None and attempt_key:
            import hashlib
            # GLM-REC-06: key the dispatch evidence dir on mission (+contract)
            # identity, NOT on the per-attempt attempt_key. attempt_key changes
            # on every re-dispatch (including a plain kill/restart of factoryd),
            # which used to point MissionState.load_or_create() at a brand-new,
            # empty directory each time — losing build_glm/test_1/... progress
            # that dispatcher_v3.py had already recorded for this mission.
            # Keying on mission_id (+ contract_id when present, consistent with
            # dispatch_checkpoint.checkpoint_key) keeps the directory — and the
            # step state inside it — stable across attempts of the same mission
            # under the same contract, while a contract refreeze (new
            # contract_id) still starts a fresh directory.
            contract_id = str(exc.get("_contract_id") or "")
            dispatch_identity = f"mission:{mission.id}:{contract_id}"
            dispatch_id = hashlib.sha256(dispatch_identity.encode("utf-8")).hexdigest()[:20]
            out_dir = self.project_dir / "evidence" / "dispatches" / dispatch_id
            out_dir.mkdir(parents=True, exist_ok=True)
        else:
            out_dir = Path(tempfile.mkdtemp(prefix=f"v2_dispatch_{mission.id}_"))
        receipt_path = out_dir / f"receipt_{mission.id}.json"
        # L2_runtime_core-017: the receipt directory is stable per
        # mission/contract, so a receipt left by an EARLIER attempt sits at the
        # exact path this attempt will read. Fingerprint it BEFORE the dispatch
        # so a receipt this execution did not (re)write can never decide it.
        receipt_pre_fingerprint = _receipt_fingerprint(receipt_path)

        cmd = [
            sys.executable, str(_DISPATCHER),
            "--repo", str(effective_repo),
            "--task-file", task_file,
            "--mission-id", mission.id,
            "--budget-s", str(budget_s),
            "--out", str(receipt_path),
            "--spec-level", spec_level,
            "--preflight", "--skip-providers",
        ]
        if test_cmd:
            cmd += ["--test-cmd", test_cmd]
            # LOT C47: tell the dispatcher when this test_cmd IS the milestone's own
            # product_e2e oracle, so its "unchanged deliverable" gate does not treat a
            # re-confirmed product verifier run as a mechanical no-op.
            _e2e = str(exc.get("product_e2e_test_cmd") or "").strip()
            # 2026-09-16 (TR_TEST_HYGIENE_V1, 4 green runs refused DELIVERABLE_UNCHANGED):
            # a step whose test_cmd CONTAINS the product oracle ("pytest && <judge>")
            # is judged by that oracle just as much as one equal to it — the pinned
            # judge's own invocation is the recognisable core, compared verbatim.
            def _core(c: str) -> str:
                return c.split("&&")[-1].strip() if "&&" in c else c.strip()
            if _e2e and (_e2e == str(test_cmd).strip() or _core(_e2e) and _core(_e2e) in str(test_cmd)):
                cmd += ["--test-cmd-is-product-e2e"]
        if self.project_dir:
            cmd += ["--project-dir", str(self.project_dir)]
            # Project identity reaches the router, not just the context loader. A routing
            # policy scoped to two projects must be applied to those two and to no other
            # caller; without this the scope would be unrepresentable below dispatch.
            cmd += ["--project", exc.get("project") or self.project_dir.name]
        # F5R5: engine_hint from escalation ladder overrides model_router for this dispatch.
        engine_hint = self.engine_hint or exc.get("engine_hint", "")
        if engine_hint:
            cmd += ["--engine-hint", engine_hint]
        # LOT C1b: a recovery DIAGNOSE/REPLAN mission sets executor.task_kind
        # (factory_v2/runtime.py::_execute_recovery_work's `elif task_file:`
        # branch) so dispatcher_v3 routes it through model_router as
        # task_kind='diagnose'|'replan' — a single read-only Codex launch,
        # never the T0 build/T2 repair/T3 escalate loop — instead of the
        # hard-coded 'build' every other AutopilotExecutor mission gets.
        # SAFE_REPAIR (and every non-recovery mission) never sets this key,
        # so cmd is byte-for-byte unchanged for them: dispatcher_v3 defaults
        # --task-kind to 'build'.
        task_kind = exc.get("task_kind", "")
        if task_kind:
            cmd += ["--task-kind", task_kind]
        # LOT C30 item (a): every path the roadmap milestone declares as a
        # required deliverable (mission_compiler.compile_missions() threads
        # ROADMAP.yaml milestone.deliverables into exc['deliverables'] for
        # autopilot executors) becomes a repeatable --deliverable argument,
        # in declaration order — dispatcher_v3 refuses ACCEPTED (verdict
        # CANDIDATE_MISSING_DELIVERABLE, LOT C28) unless every one of these
        # paths exists in the candidate commit. Recovery DIAGNOSE/REPLAN/
        # SAFE_REPAIR missions (factory_v2/runtime.py::_execute_recovery_work)
        # never set 'deliverables', so this is a no-op for them.
        deliverables = [str(d) for d in (exc.get("deliverables") or [])]
        for _d in deliverables:
            cmd += ["--deliverable", _d]
        # LOT C30 item (b): fail-closed OBSERVABILITY, not a gate — the
        # roadmap is the sole authority on whether a milestone needs a
        # declared deliverable (DERIVED_ARTIFACT_OVERRIDES_CANONICAL_ROADMAP
        # = 0), so an empty list here is never refused by this executor. But
        # a genuine build milestone (spec_level A/B — a mission that writes
        # product code, not a read-only DIAGNOSE/REPLAN judgment or a
        # SAFE_REPAIR) with zero declared deliverables cannot be checked by
        # dispatcher_v3's CANDIDATE_MISSING_DELIVERABLE invariant at all, so
        # a future capture regression on that mission would go undetected —
        # worth a typed warning. Recovery missions are exempt: their
        # mission.id is always "recovery_<id>" (see _RecoveryMission in
        # factory_v2/runtime.py::_execute_recovery_work) and they are not
        # roadmap build milestones.
        deliverables_warning = ""
        if (not deliverables and spec_level in ("A", "B")
                and not str(mission.id).startswith("recovery_")):
            deliverables_warning = "MISSION_WITHOUT_DELIVERABLES"
        _warn_prefix = f"{deliverables_warning}: " if deliverables_warning else ""
        t0 = time.monotonic()
        _evidence_project_dir = self.project_dir or Path(tempfile.mkdtemp(prefix="v2_ev_"))
        renewer = None
        proc = None
        handle = None
        try:
            # start_new_session=True: dispatcher gets its own process group so
            # killpg on timeout cannot propagate to factoryd.
            # The dispatcher and every engine below it inherit this: a cross-repo
            # recovery runs in the PRODUCT repo but must still resolve the CONTROL
            # repo for its task file and verifier.
            dispatch_env = control_repo_env()
            dispatch_env.update({
                "FACTORY_ATTEMPT_KEY": attempt_key,
                "FACTORY_ATTEMPT": str(exc.get("_attempt_number") or 0),
                "FACTORY_MISSION_REOPENED": "1" if exc.get("_reopened") else "",
                "FACTORY_REVIEW_BLOCK_FILE": str(exc.get("_review_block_file") or ""),
                "FACTORY_CONTRACT_ID": str(exc.get("_contract_id") or ""),
                "FACTORY_MILESTONE": str(exc.get("_milestone") or mission.id.split("#", 1)[0]),
            })
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                start_new_session=True, env=dispatch_env,
            )
            handle = capture_pgid(proc)
            birth = get_process_birth_time(proc.pid)
            _generation = mission.executor.get("_lease_generation", "")
            write_heartbeat(
                _evidence_project_dir, mission.id, proc.pid,
                phase="dispatched", pgid=handle.pgid, birth_time=birth,
                generation=_generation,
            )
            # F5R2: background thread renews heartbeat so dispatcher sessions >90s
            # are not falsely identified as orphans and redispatched
            renewer = HeartbeatRenewer(
                _evidence_project_dir, mission.id, proc.pid,
                pgid=handle.pgid, birth_time=birth,
                generation=_generation,
                fencing_token=mission.executor.get("_lease_fencing_token", ""),
            )
            renewer.start()
            try:
                stdout, stderr = proc.communicate(timeout=budget_s + 300)
                proc_stdout = stdout
                proc_stderr = stderr
                proc_returncode = proc.returncode
            except subprocess.TimeoutExpired:
                terminate_process_group(handle)
                stdout, stderr = proc.communicate()
                if renewer:
                    renewer.stop()
                    renewer = None
                clear_worker_state(_evidence_project_dir, mission.id)
                # THIS is the path that used to erase the spend. We just killed
                # the dispatcher's process group, so it never wrote its receipt
                # — but every engine it launched had already burned quota, and
                # the sidecar it fsynced per launch is still on disk. Returning
                # without it reported a timed-out dispatch as having cost
                # nothing at all.
                return ExecutionResult(
                    mission_id=mission.id, rc=1,
                    output_tail=f"{_warn_prefix}TIMEOUT after {budget_s + 300}s",
                    wall_time_s=time.monotonic() - t0,
                    verdict="BLOCKED",
                    telemetry_observations=_read_dispatch_sidecar(receipt_path),
                )
            finally:
                if renewer is not None:
                    renewer.stop()
                    renewer = None
                clear_worker_state(_evidence_project_dir, mission.id)
        except Exception as exc:
            if renewer is not None:
                renewer.stop()
            # L2_runtime_core-005 (autopilot path): same discipline — never
            # report EXECUTOR_ERROR while the dispatcher group is still running.
            _reap_group_on_failure(proc, handle)
            return ExecutionResult(
                mission_id=mission.id, rc=1,
                output_tail=f"{_warn_prefix}EXECUTOR_ERROR: {exc}",
                wall_time_s=time.monotonic() - t0,
                verdict="BLOCKED",
                # Whatever went wrong up here, launches below may already have
                # spent quota. Their durable rows come back with the failure.
                telemetry_observations=_read_dispatch_sidecar(receipt_path),
            )

        wall_time = time.monotonic() - t0

        # Parse dispatcher receipt
        # L2_runtime_core-017 (continued): a receipt only counts when THIS
        # execution wrote it (fingerprint moved) — and the dispatcher's own
        # exit code governs an ACCEPTED verdict, never the receipt alone.
        receipt: dict = {}
        receipt_fresh = (_receipt_fingerprint(receipt_path) is not None
                         and _receipt_fingerprint(receipt_path) != receipt_pre_fingerprint)
        if receipt_fresh:
            try:
                receipt = json.loads(receipt_path.read_text())
            except Exception:
                receipt = {}
        if not receipt_fresh and receipt_path.is_file():
            receipt.setdefault("stages", []).append({
                "step": "receipt", "status": "STALE_RECEIPT_IGNORED",
                "note": "receipt at the dispatch path was not written by this attempt"})

        # LOT C30 item (b): surface the MISSION_WITHOUT_DELIVERABLES warning
        # computed above (before dispatch) on the receipt itself — never a
        # blocking condition, purely so a build milestone with no declared
        # deliverables is visible wherever this receipt is inspected/logged,
        # not just in output_tail on the early-return paths above.
        if deliverables_warning:
            receipt.setdefault("warnings", [])
            if deliverables_warning not in receipt["warnings"]:
                receipt["warnings"].append(deliverables_warning)

        verdict = receipt.get("verdict", "BLOCKED")
        if proc_returncode != 0 and verdict == "ACCEPTED":
            # L2_runtime_core-017: a dispatcher that exited non-zero cannot
            # have its failure masked by an ACCEPTED receipt it (or an earlier
            # attempt) happened to leave behind.
            receipt.setdefault("stages", []).append({
                "step": "verdict", "status": "DISPATCHER_EXIT_NONZERO",
                "rc": proc_returncode})
            verdict = "BLOCKED"
        candidate_sha = receipt.get("candidate_sha", "")
        # F4: engine identity comes from the OBSERVED process executions the dispatcher
        # recorded from argv, never from routing_decisions (a pre-launch intention) and
        # never from an engine hint.  No observation → no identity, and the runtime
        # refuses to write a MISSION_PASS receipt.
        engine = ""
        engine_execution: dict = {}
        for _ex in receipt.get("engine_executions", []):
            if _ex.get("observed") and _ex.get("launched") and _ex.get("engine_id"):
                engine_execution = _ex
                engine = _ex["engine_id"]

        # Every measured launch of this dispatch, not just the last one. The
        # single `engine_execution` above keeps its old meaning (the identity the
        # mission receipt is bound to); this carries the usage of ALL launches,
        # because a mission that built, failed, repaired and retested spent
        # quota four times and reporting only the last one understates it.
        #
        # The SIDECAR is the authority, not the receipt. The dispatcher fsyncs
        # each observation as its launch ends, whereas the receipt is written
        # only on exit paths — so when this executor's own timeout reap kills the
        # dispatcher process group, the receipt never arrives and every launch it
        # made would otherwise vanish along with the quota it spent. The receipt
        # is merged in for anything the sidecar missed.
        telemetry_observations = _merge_observations(
            _read_dispatch_sidecar(receipt_path),
            [_ex["telemetry"] for _ex in receipt.get("engine_executions", [])
             if isinstance(_ex.get("telemetry"), dict)])
        # Phases the supervisor can separate from outside: what the dispatcher
        # spent per named step. Unmeasured steps are absent, never zero.
        phase_times_s = {
            str(s.get("step")): s["duration_s"]
            for s in receipt.get("stages", [])
            if isinstance(s, dict) and isinstance(s.get("duration_s"), (int, float))
        }

        # External repo PROMOTE: dispatcher_v3 skips merge for external repos
        # ("external repo, agent owns its own commits"). We must promote the candidate
        # branch to main so the IndependentValidator can verify the product_e2e test.
        # After merging, delete the candidate branch so future runs can re-create it.
        promoted = False
        if verdict == "ACCEPTED" and effective_repo != self.repo:
            candidate_branch = receipt.get("candidate_branch", f"candidate/{mission.id}")
            # 2026-09-17: the product checkout is never clean — the twscrape backfill
            # LaunchAgent writes its cursors/corpus there around the clock — so
            # `git merge` refused every promotion ("commit your changes"), the
            # validator then saw the candidate as "not an ancestor of HEAD" and
            # held the gate. Product STATE paths are committed first (a real
            # snapshot, not a stash); any other dirt is a typed refusal below.
            _state_prefixes = ("data/backfill_state/", "data/corpus/", "data/cursors/",
                               "research/tr_backfill_", "data/backfill/")
            try:
                _st = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"],
                                     cwd=effective_repo, capture_output=True, text=True, timeout=30)
                _dirty = [ln[3:] for ln in _st.stdout.splitlines() if ln.strip()]
                _state_dirty = [f for f in _dirty if f.startswith(_state_prefixes)]
                _other_dirty = [f for f in _dirty if f not in _state_dirty]
                if _state_dirty and not _other_dirty:
                    subprocess.run(["git", "add", "-A", "--", *_state_prefixes],
                                   cwd=effective_repo, capture_output=True, timeout=60)
                    subprocess.run(["git", "-c", "user.email=factory@local", "-c", "user.name=factory",
                                    "commit", "-qm", f"état backfill/collecte (auto, avant promotion {mission.id})"],
                                   cwd=effective_repo, capture_output=True, timeout=60)
                elif _other_dirty:
                    receipt.setdefault("stages", []).append(
                        {"step": "promote", "status": "REFUSED_DIRTY_PRODUCT_CHECKOUT",
                         "dirty": _other_dirty[:20]})
            except Exception:
                pass
            try:
                merge_result = subprocess.run(
                    ["git", "merge", "--no-ff", "--no-edit", candidate_branch],
                    cwd=effective_repo, capture_output=True, text=True, timeout=60,
                )
                if merge_result.returncode != 0:
                    # conflicts confined to product STATE paths (backfill cursors,
                    # rate budget, run summaries): the live checkout's version is
                    # the truth — keep ours, finish the merge; anything else aborts.
                    _c = subprocess.run(["git", "diff", "--name-only", "--diff-filter=U"],
                                        cwd=effective_repo, capture_output=True, text=True, timeout=30)
                    _conf = [f for f in _c.stdout.splitlines() if f.strip()]
                    if _conf and all(f.startswith(_state_prefixes) for f in _conf):
                        subprocess.run(["git", "checkout", "--ours", "--", *_conf], cwd=effective_repo,
                                       capture_output=True, timeout=30)
                        subprocess.run(["git", "add", "--", *_conf], cwd=effective_repo,
                                       capture_output=True, timeout=30)
                        merge_result = subprocess.run(
                            ["git", "-c", "user.email=factory@local", "-c", "user.name=factory",
                             "commit", "--no-edit", "-qm", f"Merge {candidate_branch} (état produit : version live conservée)"],
                            cwd=effective_repo, capture_output=True, text=True, timeout=60)
                if merge_result.returncode == 0:
                    promoted = True
                    subprocess.run(
                        ["git", "branch", "-d", candidate_branch],
                        cwd=effective_repo, capture_output=True, timeout=30,
                    )
                else:
                    subprocess.run(["git", "merge", "--abort"], cwd=effective_repo, timeout=30)
            except Exception:
                pass

        # DIAGNOSE/REPLAN missions (LOT C1b) end on DIAGNOSIS_PRODUCED / REPLAN_PRODUCED,
        # never ACCEPTED — 2026-09-14/15 every correct diagnosis was booked as a
        # semantic failure and the ladder re-diagnosed the same cause all night.
        rc = 0 if verdict in ("ACCEPTED", "DIAGNOSIS_PRODUCED", "REPLAN_PRODUCED") else 1

        tail_parts = []
        if receipt.get("verdict_reason"):
            tail_parts.append(receipt["verdict_reason"])
        for stage in receipt.get("stages", []):
            if stage.get("stdout_tail"):
                tail_parts.append(stage["stdout_tail"][-500:])
        if not tail_parts:
            tail_parts.append((proc_stdout + proc_stderr)[-2000:])

        # integration_sha: the exact post-merge HEAD that the validator must test against.
        # For factory-repo merges, set by dispatcher_v3 after _merge_candidate().
        # For external repos, set here after our own merge step.
        integration_sha = receipt.get("integration_sha", "")
        if not integration_sha and promoted and effective_repo != self.repo:
            try:
                r = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=effective_repo, capture_output=True, text=True, timeout=10,
                )
                if r.returncode == 0:
                    integration_sha = r.stdout.strip()
            except Exception:
                pass

        return ExecutionResult(
            mission_id=mission.id,
            rc=rc,
            output_tail="\n".join(tail_parts)[-2000:],
            engine=engine,
            candidate_sha=candidate_sha,
            integration_sha=integration_sha,
            wall_time_s=wall_time,
            verdict=verdict,
            receipt=receipt,
            engine_execution=engine_execution,
            telemetry_observations=telemetry_observations,
            phase_times_s=phase_times_s,
        )


# ---------------------------------------------------------------------------
# LOT C17 -- factoryctl `weekly-refresh-observed` dedicated entry point.
#
# Owner decision (2026-09-12): the real weekly refresh of the product is
# triggered BY THE FACTORY (a factoryctl subcommand / launchd schedule), not
# by the product's own scheduler -- because WeeklyRefreshObservedExecutor is
# the only writer authorized to produce and commit the weekly-refresh-
# observation/v2 receipts the hardened weekly-operate verifier requires. Before
# this, the executor was reachable only through a roadmap mission dispatched
# by the supervisor cycle (factory_v2/runtime.py). This is the standalone
# entry point factoryctl calls: it loads the SAME weekly_refresh_observed
# step out of the project's canonical ROADMAP.yaml (never a duplicated or
# hardcoded command), pins a concrete trigger_scheduled_at, and runs the
# executor exactly as the supervisor cycle would -- behind a single-flight
# file lock so a concurrent invocation is refused rather than double-running
# a real, network-touching refresh.
# ---------------------------------------------------------------------------

WEEKLY_REFRESH_CLI_LOCK_NAME = "weekly_refresh_observed.lock"

_TRIGGER_SCHEDULED_AT_RE = re.compile(
    r'--trigger-scheduled-at\s+(?:"[^"]*"|\'[^\']*\'|\S+)')


def _find_weekly_refresh_step(roadmap: dict, refresh_seq: int):
    """Search every milestone's execution.steps for the weekly_refresh_observed
    step matching `refresh_seq`. Returns (milestone_entry, step) or (None, None).

    Never hardcodes a milestone id -- the roadmap is the single source of
    truth for which milestone currently carries this executor type, and for
    which command each refresh leg runs.
    """
    for milestone_entry in (roadmap or {}).get("milestones", []) or []:
        steps = (milestone_entry.get("execution") or {}).get("steps") or []
        for step in steps:
            if not isinstance(step, dict):
                continue
            executor = step.get("executor")
            if not isinstance(executor, dict):
                continue
            if executor.get("type") != "weekly_refresh_observed":
                continue
            if executor.get("refresh_seq") == refresh_seq:
                return milestone_entry, step
    return None, None


def _substitute_trigger_scheduled_at(command: str, scheduled_at: str) -> str | None:
    """Replace the roadmap command's --trigger-scheduled-at argument (normally
    a `$(date ...)` shell substitution meant to be evaluated at run time) with
    a concrete, already-resolved ISO timestamp -- so the invocation this entry
    point actually runs is byte-fixed and reproducible, never dependent on the
    exact wall-clock second bash happens to evaluate `date` at, and never
    silently divergent from the value this call logs and journals.

    Returns None if the command has no --trigger-scheduled-at flag at all (a
    roadmap edit changed the flag shape) -- the caller must fail closed rather
    than run an unpinned command.
    """
    new_command, n = _TRIGGER_SCHEDULED_AT_RE.subn(
        f'--trigger-scheduled-at "{scheduled_at}"', command, count=1)
    return new_command if n == 1 else None


def _append_weekly_refresh_run_log(project_dir: Path, record: dict) -> None:
    """Append-only durable trace of every factoryctl weekly-refresh-observed
    invocation, inside the PROJECT's own evidence/ directory -- not the
    product repo (which is a separate, dedicated-executor-only writer), and
    not PROJECT_STATE.json (this call site holds no live PROJECT_STATE
    transaction; forcing one open just to log a line would risk colliding
    with a concurrently running supervisor cycle's own transaction).
    """
    out_dir = Path(project_dir) / "evidence"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "weekly_refresh_runs.jsonl"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def weekly_refresh_observed_cli(*, project_dir: str | Path, refresh_seq: int,
                                scheduled_at: str | None = None,
                                dry_run: bool = False,
                                lock_dir: str | Path | None = None) -> dict:
    """Body of factoryctl's `weekly-refresh-observed` subcommand.

    Always returns a JSON-able dict carrying an "rc" key: 0 on success (or a
    dry-run), 2 on any refusal or failure, with a machine-readable "reason".
    Never raises for an expected refusal -- a bug that escapes as an
    exception is the one case a caller should see a traceback for.
    """
    from .project_authority import AuthorityError, load_authority

    project_dir = Path(project_dir)
    if refresh_seq not in (1, 2):
        return {"rc": 2, "reason": f"BAD_REFRESH_SEQ: {refresh_seq!r} must be 1 or 2"}

    try:
        authority = load_authority(project_dir)
    except AuthorityError as exc:
        return {"rc": 2, "reason": f"AUTHORITY_UNAVAILABLE: {exc}"}

    milestone_entry, _step = _find_weekly_refresh_step(authority.roadmap, refresh_seq)
    if milestone_entry is None:
        return {"rc": 2,
                "reason": f"NO_WEEKLY_REFRESH_MILESTONE: no milestone in "
                          f"{project_dir}/ROADMAP.yaml carries a "
                          f"weekly_refresh_observed step for refresh_seq={refresh_seq}"}

    contract_id = f"cli-weekly-refresh-observed-seq{refresh_seq}"
    try:
        missions = compile_missions(authority, milestone_entry, contract_id)
    except MissionCompileError as exc:
        return {"rc": 2, "reason": f"MISSION_COMPILE_ERROR: {exc}"}
    mission = next((m for m in missions
                    if m.executor.get("type") == "weekly_refresh_observed"
                    and m.executor.get("refresh_seq") == refresh_seq), None)
    if mission is None:
        return {"rc": 2,
                "reason": "NO_WEEKLY_REFRESH_MILESTONE: compiled milestone lost "
                          "its weekly_refresh_observed step"}

    if scheduled_at is None:
        scheduled_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        try:
            datetime.strptime(scheduled_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            return {"rc": 2,
                    "reason": f"BAD_SCHEDULED_AT: {scheduled_at!r} is not "
                              f"'%Y-%m-%dT%H:%M:%SZ'"}

    original_command = str(mission.executor.get("command") or "")
    substituted = _substitute_trigger_scheduled_at(original_command, scheduled_at)
    if substituted is None:
        return {"rc": 2,
                "reason": "COMMAND_MISSING_TRIGGER_SCHEDULED_AT: roadmap command "
                          "has no --trigger-scheduled-at flag to pin"}

    if dry_run:
        return {"rc": 0, "dry_run": True, "refresh_seq": refresh_seq,
                "milestone": milestone_entry.get("id", ""),
                "product_repo": mission.executor.get("product_repo", ""),
                "trigger_scheduled_at": scheduled_at, "command": substituted}

    # Mutate this call's own freshly-compiled mission dict only -- compile_missions()
    # returns a new Mission/executor dict on every call, never a cached/shared one.
    mission.executor["command"] = substituted
    mission.executor["trigger_scheduled_at"] = scheduled_at

    lock_dir = Path(lock_dir) if lock_dir else (_REPO_ROOT / "factory" / "run")
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / WEEKLY_REFRESH_CLI_LOCK_NAME
    with open(lock_path, "a+") as lock_fh:
        try:
            try:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return {"rc": 2,
                        "reason": "REFRESH_ALREADY_IN_PROGRESS: another "
                                  f"weekly-refresh-observed run holds {lock_path}"}
            result = WeeklyRefreshObservedExecutor(project_dir).execute(mission)
            receipt = result.receipt or {}
            # LOT C23: fail-closed. A mission-level rc=0/ACCEPTED (the executor's
            # own, deliberately tolerant verdict -- see its docstring: an
            # optional leg that carries no `commit_outputs: true` legitimately
            # produces no v2 evidence) is NOT enough for this CLI, which is the
            # ops-facing entry point real humans/launchd trust to report whether
            # a refresh actually landed durable, verifiable evidence. Absent a
            # committed weekly-refresh-observation/v2 receipt, this call must
            # refuse typed rather than echo the executor's rc=0.
            cli_rc = 2
            cli_reason = ""
            if result.rc != 0:
                cli_reason = f"EXECUTION_FAILED: {result.output_tail}"
            elif not receipt.get("v2_path"):
                cause = receipt.get("v2_reason") or "UNKNOWN"
                cli_reason = f"V2_RECEIPT_NOT_PRODUCED:{cause}"
            else:
                cli_rc = 0
            record = {
                "schema": "weekly-refresh-observed-cli-run/v1",
                "logged_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "refresh_seq": refresh_seq, "trigger_scheduled_at": scheduled_at,
                "trigger_source": "factoryctl",
                "executor_rc": result.rc, "rc": cli_rc, "reason": cli_reason,
                "verdict": result.verdict, "output_tail": result.output_tail,
                "integration_sha": result.integration_sha,
                "observation_id": receipt.get("observation_id", ""),
            }
            _append_weekly_refresh_run_log(project_dir, record)
            if cli_rc != 0:
                return {"rc": 2, "reason": cli_reason, "verdict": result.verdict,
                        "observation_id": receipt.get("observation_id", "")}
            return {"rc": 0, "refresh_seq": refresh_seq,
                    "milestone": milestone_entry.get("id", ""),
                    "trigger_scheduled_at": scheduled_at,
                    "refresh_sha": result.integration_sha,
                    "receipt_path": receipt.get("v2_path", ""),
                    "observation_id": receipt.get("observation_id", ""),
                    "output_tail": result.output_tail}
        finally:
            try:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass


def _find_weekly_refresh_product_repo(roadmap: dict) -> str:
    """Any weekly_refresh_observed step's `product_repo` -- both refresh_seq
    legs of a given milestone name the same product repo in ROADMAP.yaml, so
    unlike `_find_weekly_refresh_step` this does not filter by refresh_seq at
    all. Used by materialize, which acts on an already-stored observation
    (identified by its own observation_id, not by a fresh refresh_seq
    dispatch) and therefore has no refresh_seq of its own to match on.
    Returns "" if no such step exists anywhere in the roadmap.
    """
    for milestone_entry in (roadmap or {}).get("milestones", []) or []:
        steps = (milestone_entry.get("execution") or {}).get("steps") or []
        for step in steps:
            if not isinstance(step, dict):
                continue
            executor = step.get("executor")
            if not isinstance(executor, dict):
                continue
            if executor.get("type") != "weekly_refresh_observed":
                continue
            product_repo = str(executor.get("product_repo") or "")
            if product_repo:
                return product_repo
    return ""


def weekly_refresh_materialize_cli(*, project_dir: str | Path, observation_id: str,
                                    lock_dir: str | Path | None = None,
                                    observation_dir: Path | None = None) -> dict:
    """Body of factoryctl's `weekly-refresh-observed --materialize-observation`.

    Recovery path for LOT C23: a real, supervisor-observed refresh_seq=1 leg
    stores a genuine v1 `WeeklyRefreshObservation` (see `observe_weekly_refresh`)
    but -- by mission_compiler's own frozen invariant (`commit_outputs` may be
    true only for refresh_seq=2) -- `WeeklyRefreshObservedExecutor.execute()`
    never commits its outputs or emits a v2 receipt for that leg. If the run's
    real outputs are still sitting uncommitted in the product tree, this call
    commits EXACTLY them (same strict pathspec as `_commit_weekly_outputs`,
    the sole authorized writer) and emits the v2 receipt the observation
    earned -- WITHOUT re-invoking the refresh command: no scrape, no network
    call, no new observation. `command`/`rc`/`observed_at` in the resulting
    receipt are the ALREADY-OBSERVED ones from `observation_id`, never
    re-derived.

    Refuses (rc=2) fail-closed, never partially materializes:
      OBSERVATION_NOT_FOUND    -- observation_id is not a genuinely stored,
                                  index-authorized, hash-verified v1 record
                                  under the STORE (or `observation_dir`
                                  override, tests only).
      OBSERVATION_STALE        -- the product repo's CURRENT on-disk corpus
                                  no longer hashes to the observation's own
                                  corpus_sha (recomputed with the exact same
                                  function `observe_weekly_refresh` used) --
                                  someone touched the corpus since the run
                                  this observation is evidence for.
      V2_RECEIPT_NOT_PRODUCED:<cause> -- the commit itself failed, or the
                                  resulting commit still does not satisfy
                                  `build_weekly_refresh_v2` (e.g. nothing was
                                  actually different to commit).
    """
    from .project_authority import AuthorityError, load_authority

    project_dir = Path(project_dir)
    if not observation_id or not WO._ID_RE.match(str(observation_id)):
        return {"rc": 2, "reason": f"BAD_OBSERVATION_ID: {observation_id!r}"}

    try:
        authority = load_authority(project_dir)
    except AuthorityError as exc:
        return {"rc": 2, "reason": f"AUTHORITY_UNAVAILABLE: {exc}"}

    product_repo_str = _find_weekly_refresh_product_repo(authority.roadmap)
    if not product_repo_str:
        return {"rc": 2,
                "reason": "NO_WEEKLY_REFRESH_MILESTONE: no weekly_refresh_observed "
                          f"step in {project_dir}/ROADMAP.yaml names a product_repo"}
    repo_path = Path(product_repo_str)
    if not repo_path.is_absolute():
        repo_path = (_REPO_ROOT / product_repo_str).resolve()
    if not repo_path.is_dir():
        return {"rc": 2,
                "reason": f"WEEKLY_REFRESH_BAD_PRODUCT_REPO: {product_repo_str!r} "
                          f"(resolved: {repo_path}) does not exist."}

    obs_dir = observation_dir if observation_dir is not None else WO.STORE
    obs = next((o for o in WO.list_valid_observations(obs_dir)
               if o.observation_id() == observation_id), None)
    if obs is None:
        return {"rc": 2,
                "reason": f"OBSERVATION_NOT_FOUND: {observation_id!r} is not an "
                          f"indexed, hash-verified, valid observation under {obs_dir}"}

    lock_dir = Path(lock_dir) if lock_dir else (_REPO_ROOT / "factory" / "run")
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / WEEKLY_REFRESH_CLI_LOCK_NAME
    with open(lock_path, "a+") as lock_fh:
        try:
            try:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return {"rc": 2,
                        "reason": "REFRESH_ALREADY_IN_PROGRESS: another "
                                  f"weekly-refresh-observed run holds {lock_path}"}

            def _log_and_return(cli_rc: int, reason: str, extra: dict | None = None) -> dict:
                record = {
                    "schema": "weekly-refresh-observed-cli-run/v1",
                    "logged_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "trigger_source": "factoryctl-materialize", "rc": cli_rc,
                    "reason": reason, "observation_id": observation_id,
                    "refresh_seq": obs.refresh_seq,
                }
                if extra:
                    record.update(extra)
                _append_weekly_refresh_run_log(project_dir, record)
                out = {"rc": cli_rc, "observation_id": observation_id}
                if reason:
                    out["reason"] = reason
                if extra:
                    out.update(extra)
                return out

            pre_head = _git_head(repo_path)
            facts = corpus_facts(repo_path / "data" / "corpus")
            current_corpus_sha = facts.get("corpus_sha", "")
            if current_corpus_sha != obs.corpus_sha:
                return _log_and_return(
                    2, f"OBSERVATION_STALE: current corpus_sha {current_corpus_sha!r} "
                       f"!= observation corpus_sha {obs.corpus_sha!r}")

            integration_sha, commit_error = _commit_weekly_outputs(repo_path)
            if commit_error:
                return _log_and_return(
                    2, f"V2_RECEIPT_NOT_PRODUCED:COMMIT_FAILED:{commit_error}")

            v2_record, v2_reason = WO.build_weekly_refresh_v2(
                obs=obs, repo=repo_path, pre_head=pre_head, post_head=integration_sha)
            if v2_record is None:
                return _log_and_return(
                    2, f"V2_RECEIPT_NOT_PRODUCED:{v2_reason}",
                    {"integration_sha": integration_sha})

            v2_path, v2_err = WO.write_v2_receipt(repo_path, v2_record)
            if v2_err:
                return _log_and_return(
                    2, f"V2_RECEIPT_NOT_PRODUCED:WRITE_FAILED:{v2_err}",
                    {"integration_sha": integration_sha})

            commit_err2 = _commit_weekly_refresh_v2_receipt(repo_path, v2_path)
            if commit_err2:
                return _log_and_return(
                    2, f"V2_RECEIPT_NOT_PRODUCED:COMMIT_FAILED:{commit_err2}",
                    {"integration_sha": integration_sha})

            return _log_and_return(
                0, "", {"refresh_sha": integration_sha, "receipt_path": str(v2_path)})
        finally:
            try:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
