"""Independent validation + authoritative milestone gate (ferme GAP-029/GAP-030).

Le gate n'est PLUS advisory: un milestone ne peut PAS être settle si son contrat
gelé n'est pas entièrement vérifié par un validateur indépendant du worker.
Niveaux exigés (milestones critiques):
    IMPLEMENTED / WIRED / RUNTIME_EXERCISED / AUTHORITATIVE /
    CONTINUOUSLY_ENFORCED / PRODUCT_E2E_PASS / EVIDENCE_PRESENT

Décisions externes: ADAPT Factory Missions (fresh validator, black box);
ADAPT Spec Kit converge (implémentation comparée à l'intention gelée).

LR1 (2026-09-08): when integration_sha is provided, validation runs in an isolated
detached worktree at that exact SHA — never in the shared checkout. This prevents
the HEAD-race where a concurrent worker merges another branch between the build's
merge and the validator's SHA check. The worktree is guaranteed to be pinned at the
post-merge commit and is destroyed immediately after validation.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

_REPO_ROOT = Path(__file__).parents[1]


@dataclass(frozen=True)
class ValidationVerdict:
    contract_id: str
    milestone: str
    implemented: bool
    wired: bool
    runtime_exercised: bool
    product_e2e_pass: bool
    evidence_present: bool
    forbidden_regressions_clean: bool
    failures: list = field(default_factory=list)

    @property
    def milestone_complete(self) -> bool:
        return not self.failures and all((
            self.implemented, self.wired, self.runtime_exercised,
            self.product_e2e_pass, self.evidence_present,
            self.forbidden_regressions_clean,
        ))


class IndependentValidator:
    """Exécute le contrat en black box depuis le workdir du projet — ne lit jamais
    la sortie du worker comme preuve."""

    def __init__(self, project_dir: Path, timeout_s: int = 1800,
                 persist_evidence: bool = True, control_pin: str = ""):
        """persist_evidence=False makes validate() a pure read: it still runs the
        contract's commands, but writes no validator_e2e.log, no validator_verdict.json
        and does not create the milestone evidence directory.

        S3: settlement counting and long-run readiness call this verifier on every
        check.  A "read-only" verifier that leaves evidence behind is not read-only —
        it mutates the very project whose truth it is being asked to report on, and two
        consecutive reads would not see the same bytes.

        control_pin (S5 dual pin): an exact commit of the control repo.  When set,
        the contract's commands run with FACTORY_CONTROL_REPO/PYTHONPATH pointing at
        a detached worktree pinned to that commit, so the verifier CODE that judges
        acceptance is the code the settlement was accepted under — never the live
        tree.  Empty string keeps the legacy behavior (live control tree).
        """
        self.project_dir = Path(project_dir)
        self.timeout_s = timeout_s
        self.persist_evidence = persist_evidence
        self.control_pin = control_pin.strip()
        self._active_control_root: "Path | None" = None

    def _run(self, command: str, cwd: "Path | None" = None) -> tuple[int, str]:
        """Run one contract command.

        S4: cwd is an explicit argument.  It used to be read from self.project_dir,
        which callers temporarily reassigned and then restored — so any check running
        after the restore silently executed somewhere else.  That is exactly how
        forbidden_regressions ended up running in the authority directory instead of
        the worktree pinned to the SHA being settled.
        """
        # G5 fix: use explicit pipefail semantics when shell=True is required.
        # Wrap in `set -eo pipefail` so that `cmd1 | cmd2` failure propagates.
        # Prefer argv/list form when possible — shell=True only for commands
        # that genuinely need shell syntax (pipes, expansions).
        #
        # A timeout is a classified validator FAILURE — not an uncaught exception.
        # TimeoutExpired → rc=124, output logged, caller sees as FAIL not CRASH.
        wrapped = f"set -eo pipefail\n{command}"
        try:
            from .mission_evidence import control_repo_env
            env = control_repo_env()
            # Runtime evidence deliberately lives outside detached code
            # worktrees. Pin verifier code separately from its supervisor-owned,
            # content-addressed evidence store.
            env["FACTORY_SUPERVISOR_EVIDENCE_ROOT"] = str(_REPO_ROOT)
            # 19/09: `python3 -m factory_v2.product_verifiers` run with cwd = the
            # validation worktree of an OLD integration SHA imported THAT tree's
            # factory_v2 (cwd precedes PYTHONPATH) — the verifier registered in
            # the control repo was invisible (VERIFIER_NOT_IMPLEMENTED, benchmark
            # gate-blocked for 3 days). The verifier comes from the control
            # root / pinned control worktree, the product from cwd.
            env["PYTHONSAFEPATH"] = "1"
            if self._active_control_root is not None:
                # S5 dual pin: the pinned control worktree REPLACES the live tree as
                # FACTORY_CONTROL_REPO and leads PYTHONPATH, so both the verifier the
                # command invokes and any ${FACTORY_CONTROL_REPO} reference resolve
                # inside the commit the settlement named — not wherever HEAD is now.
                pinned = str(self._active_control_root)
                env["FACTORY_CONTROL_REPO"] = pinned
                env["PYTHONPATH"] = pinned + os.pathsep + env.get("PYTHONPATH", "")
            proc = subprocess.run(
                wrapped, shell=True, cwd=(cwd or self.project_dir),
                capture_output=True, text=True,
                timeout=self.timeout_s,
                executable="/bin/bash",
                env=env,
            )
            return proc.returncode, (proc.stdout + proc.stderr)[-4000:]
        except subprocess.TimeoutExpired as exc:
            out = ""
            if exc.stdout:
                out += exc.stdout[-2000:] if isinstance(exc.stdout, str) else exc.stdout[-2000:].decode(errors="replace")
            if exc.stderr:
                out += exc.stderr[-2000:] if isinstance(exc.stderr, str) else exc.stderr[-2000:].decode(errors="replace")
            return 124, f"TIMEOUT after {self.timeout_s}s — classified VALIDATOR_FAILURE\n{out}"

    def _resolve_product_repo(self, body: dict) -> Path:
        """The repo a contract's commands and SHAs belong to.

        One rule, used by the SHA checks and by command execution alike: they resolved
        `product_repo` slightly differently before, which meant the SHA could be checked
        in one directory while the commands ran in another.
        """
        product_repo_str = body.get("product_repo", "")
        if not product_repo_str:
            if self.project_dir.resolve() == (_REPO_ROOT / "projects" / "factory").resolve():
                return _REPO_ROOT
            return self.project_dir
        pr = Path(product_repo_str)
        if not pr.is_absolute():
            pr = (_REPO_ROOT / product_repo_str).resolve()
        return pr if pr.exists() else self.project_dir

    def _artifact_exists(self, artifact: str) -> bool:
        return (self.project_dir / artifact).exists()

    def _create_detached_worktree(self, repo: Path, sha: str) -> Path:
        """Create a detached git worktree at `sha` and return its path.

        Raises RuntimeError if worktree creation fails or SHA verification fails.
        The caller is responsible for calling _remove_detached_worktree() afterwards.
        """
        worktree_dir = Path(tempfile.mkdtemp(prefix="factory-gate-"))
        try:
            r = subprocess.run(
                ["git", "worktree", "add", "--detach", str(worktree_dir), sha],
                cwd=repo, capture_output=True, text=True, timeout=60,
            )
            if r.returncode != 0:
                raise RuntimeError(
                    f"git worktree add failed (rc={r.returncode}): {r.stderr.strip()}")
            # Verify the worktree is pinned at the exact SHA (defensive check).
            head_r = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=worktree_dir, capture_output=True, text=True, timeout=10,
            )
            actual = head_r.stdout.strip()
            if actual != sha:
                raise RuntimeError(
                    f"Worktree SHA mismatch: expected {sha!r}, got {actual!r}")
            return worktree_dir
        except Exception:
            shutil.rmtree(worktree_dir, ignore_errors=True)
            raise

    def _remove_detached_worktree(self, repo: Path, worktree_dir: Path) -> None:
        """Remove a detached worktree created by _create_detached_worktree()."""
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_dir)],
            cwd=repo, capture_output=True, timeout=30,
        )
        shutil.rmtree(worktree_dir, ignore_errors=True)

    def validate(self, contract: dict, candidate_sha: str = "",
                 integration_sha: str = "") -> ValidationVerdict:
        """Validate the contract.

        integration_sha: post-merge HEAD recorded by dispatcher_v3 after _merge_candidate().
          When non-empty, validation runs in an isolated DETACHED WORKTREE at that exact SHA
          (LR1: exact-SHA worktree validation). This prevents the HEAD-race where a concurrent
          worker merges another branch between the build's merge and the validator's check.
          The worktree is guaranteed pinned at integration_sha and destroyed after validation.

        candidate_sha: the candidate branch tip (pre-merge). Used only as fallback when
          integration_sha is absent (e.g. external-repo missions that promote their own branch).
          Falls back to the original ancestor check in that case.

        LocalCommandExecutor missions pass both as "" — no git check performed.
        """
        body = contract["body"]
        mid = body["milestone"]
        failures: list[str] = []
        evidence_dir = self.project_dir / "evidence" / mid
        if self.persist_evidence:
            evidence_dir.mkdir(parents=True, exist_ok=True)

        def _sha_check_repo() -> Path:
            return self._resolve_product_repo(body)

        def _head_sha(repo: Path) -> str:
            r = subprocess.run(["git", "rev-parse", "HEAD"],
                               cwd=repo, capture_output=True, text=True, timeout=10)
            return r.stdout.strip() if r.returncode == 0 else "UNKNOWN"

        def _fail_sha(msg: str) -> ValidationVerdict:
            failures.append(msg)
            return ValidationVerdict(
                contract_id=contract["contract_id"], milestone=mid,
                implemented=False, wired=False, runtime_exercised=False,
                product_e2e_pass=False, evidence_present=False,
                forbidden_regressions_clean=False, failures=list(failures),
            )

        # LR1: when integration_sha is provided, create a DETACHED WORKTREE at that
        # exact SHA. Validation (product_e2e test_cmd, artifact checks) runs from the
        # worktree — completely isolated from concurrent HEAD movement in the shared
        # checkout. The worktree is destroyed in the finally block regardless of outcome.
        validation_worktree: Path | None = None
        worktree_repo: Path | None = None
        control_worktree: Path | None = None
        control_repo: Path | None = None

        if self.control_pin:
            # S5 dual pin: pin the CONTROL code to the commit the receipt named.
            # The control repo is the repo owning the project authority dir; when
            # that is not a git repo there is nothing trustworthy to pin and the
            # pin request fails closed instead of silently using the live tree.
            try:
                r = subprocess.run(
                    ["git", "rev-parse", "--show-toplevel"],
                    cwd=str(self.project_dir), capture_output=True, text=True,
                    timeout=10)
                control_repo = (Path(r.stdout.strip())
                                if r.returncode == 0 and r.stdout.strip() else None)
                if control_repo is None:
                    return _fail_sha(
                        "CONTROL_PIN_UNRESOLVABLE: project dir is not inside a git "
                        "repo, so the control pin cannot be materialized")
                control_worktree = self._create_detached_worktree(
                    control_repo, self.control_pin)
            except Exception as exc:
                return _fail_sha(f"CONTROL_PIN_WORKTREE_ERROR: {exc}")

        # L2_runtime_core-003: every early return below (worktree creation
        # failure, SHA mismatch) now happens INSIDE the try whose finally
        # removes BOTH worktrees — the control worktree used to leak when the
        # integration worktree could not be created.
        try:
            if integration_sha:
                try:
                    worktree_repo = _sha_check_repo()
                    validation_worktree = self._create_detached_worktree(
                        worktree_repo, integration_sha)
                except Exception as exc:
                    return _fail_sha(f"WORKTREE_CREATE_ERROR: {exc}")
            elif candidate_sha:
                # Fallback ancestor check for external-repo missions where
                # integration_sha may not be set (e.g., agent-owned commits on
                # an external product repo).
                try:
                    repo = _sha_check_repo()
                    anc = subprocess.run(
                        ["git", "merge-base", "--is-ancestor", candidate_sha, "HEAD"],
                        cwd=repo, capture_output=True, timeout=10,
                    )
                    if anc.returncode != 0:
                        return _fail_sha(
                            f"SHA_MISMATCH: candidate_sha={candidate_sha!r} is not an "
                            f"ancestor of HEAD={_head_sha(repo)!r} in {repo}. "
                            "Validation is bound to the candidate commit — "
                            "cannot validate a different commit."
                        )
                except Exception as exc:
                    return _fail_sha(f"SHA_CHECK_ERROR: {exc}")

            self._active_control_root = control_worktree
            return self._validate_body(
                contract=contract,
                body=body,
                mid=mid,
                failures=failures,
                evidence_dir=evidence_dir,
                validation_worktree=validation_worktree,
            )
        finally:
            self._active_control_root = None
            if validation_worktree is not None and worktree_repo is not None:
                self._remove_detached_worktree(worktree_repo, validation_worktree)
            if control_worktree is not None and control_repo is not None:
                self._remove_detached_worktree(control_repo, control_worktree)

    def _validate_body(
        self,
        contract: dict,
        body: dict,
        mid: str,
        failures: list,
        evidence_dir: Path,
        validation_worktree: "Path | None",
    ) -> ValidationVerdict:
        """Run the product_e2e test_cmd and artifact checks.

        When validation_worktree is set, product_e2e runs from the worktree (LR1).
        When None, runs from e2e_cwd as before (local_command, external-repo fallback).
        """
        # G1 fix: FABRICATED_VALIDATION_SIGNAL = 0.
        # implemented/wired/runtime_exercised must be derived from OBSERVABLE evidence only.
        # Hardcoding True fabricates proof — an unwired/unexercised capability must FAIL.

        # runtime_exercised: product_e2e test must actually pass
        test_cmd = (body.get("product_e2e") or {}).get("test_command")
        product_e2e_pass = False
        runtime_exercised = False
        # S4: ONE validation cwd, resolved once and used by every check in this
        # verdict — product_e2e, required artifacts and forbidden_regressions alike.
        # When a worktree is pinned to integration_sha, all three run there; splitting
        # them across directories is what let a commit carrying a forbidden regression
        # settle, because only the regression check ran somewhere else.
        #
        # WRONG_REPO_DISPATCH fix: without a pinned SHA, run in product_repo when the
        # contract declares one — the project authority dir (STRATEGY/SPEC/ROADMAP) is
        # not the product code repo.
        validation_cwd = (validation_worktree if validation_worktree is not None
                          else self._resolve_product_repo(body))
        e2e_cwd = validation_cwd
        if test_cmd:
            rc, out = self._run(test_cmd, cwd=validation_cwd)
            runtime_exercised = rc == 0
            product_e2e_pass = rc == 0
            if self.persist_evidence:
                (evidence_dir / "validator_e2e.log").write_text(
                    f"$ {test_cmd}\nrc={rc}\n{out}\n", encoding="utf-8")
            if rc != 0:
                failures.append(f"product_e2e command rc={rc}")
        else:
            failures.append("contract has no product_e2e test_command — EVIDENCE_NOT_DERIVABLE")

        # required_artifacts: all declared deliverables must exist on disk.
        # The contract body maps milestone.deliverables → required_artifacts.
        # wired = all required_artifacts present on disk. No separate "deliverables" field.
        # When product_repo is declared, artifacts live in the product repo, not project_dir.
        required = body.get("required_artifacts") or []
        evidence_present = True
        wired = True
        for artifact in required:
            # e2e_cwd is the correct authority for artifact lookup in all cases:
            # worktree (LR1), product_repo, or self.project_dir (default).
            artifact_path = e2e_cwd / str(artifact)
            exists = artifact_path.exists()
            if not exists:
                evidence_present = False
                wired = False
                failures.append(f"missing artifact (WIRED=FAIL, EVIDENCE_ABSENT): {artifact}")
        if not required:
            # No declared deliverables — infer wired/evidence from product_e2e
            wired = product_e2e_pass
            evidence_present = product_e2e_pass

        # implemented: required artifacts exist AND wired OR (no required AND product_e2e passed)
        implemented = (bool(required) and evidence_present) or (not required and product_e2e_pass)
        if not implemented and not required:
            failures.append("implemented=UNKNOWN — no required_artifacts specified to verify")

        verdict = ValidationVerdict(
            contract_id=contract["contract_id"], milestone=mid,
            implemented=implemented, wired=wired,
            runtime_exercised=runtime_exercised,
            product_e2e_pass=product_e2e_pass,
            evidence_present=evidence_present,
            forbidden_regressions_clean=self._check_regressions(
                body, failures, cwd=validation_cwd),
            failures=failures,
        )
        receipt = {"schema": "validator-verdict/v2",
                   **{k: getattr(verdict, k) for k in (
                       "contract_id", "milestone", "implemented", "wired",
                       "runtime_exercised", "product_e2e_pass", "evidence_present",
                       "forbidden_regressions_clean", "failures")},
                   "milestone_complete": verdict.milestone_complete}
        if self.persist_evidence:
            (evidence_dir / "validator_verdict.json").write_text(
                json.dumps(receipt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return verdict

    def _check_regressions(self, body: dict, failures: list,
                           cwd: "Path | None" = None) -> bool:
        """Run every forbidden-regression command in the SAME cwd as the rest of the
        verdict — the worktree pinned to integration_sha when there is one.

        S4: this used to inherit self.project_dir, which the caller had just restored
        to the authority directory, so the regressions were evaluated against the live
        checkout rather than the commit being settled.  Both directions were wrong: a
        bad receipt SHA passed because the regression was not present in the authority
        dir, and a good receipt SHA failed because a later HEAD had introduced one.
        """
        clean = True
        for cmd in body.get("forbidden_regressions") or []:
            if not cmd:
                continue
            rc, _ = self._run(cmd, cwd=cwd)
            if rc == 0:
                clean = False
                failures.append(f"forbidden regression reproduced: {cmd}")
            elif rc != 1:
                # rc=1 is the predicate's ordinary "not reproduced" answer.
                # A timeout, missing command, or crash is an UNKNOWN observation.
                clean = False
                failures.append(
                    f"forbidden regression unobservable (rc={rc}): {cmd}")
        return clean


def assert_milestone_settleable(verdict: ValidationVerdict) -> None:
    """Gate AUTHORITATIF — lève au lieu de warn (contrairement au champion
    factoryd.py:411 qui est advisory). Mission PASS != milestone COMPLETE."""
    if not verdict.milestone_complete:
        raise MilestoneGateBlocked(
            f"{verdict.milestone}: milestone settlement BLOCKED — {verdict.failures}")


class MilestoneGateBlocked(Exception):
    """Settlement impossible: le contrat gelé n'est pas satisfait."""
