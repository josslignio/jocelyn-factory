"""Phase 5: SafetyQuarantine — project-scoped quarantine with SafetyClearReceipt.

Phase 5 upgrade from LR4:

Quarantine scope (full context preserved):
    - interrupted mission_id / attempt_id
    - root failure fingerprint
    - authority hashes at entry time
    - candidate SHA
    - entered_at, last_seen
    - allowed actions (DIAGNOSE, AUDIT, RESEARCH, SAFE_REPAIR, validation)
    - recovery_state (for restart adoption)

Forbidden while quarantined:
    - DISPATCH_PRODUCT (normal product dispatch)
    - UNRELATED_MILESTONE (advance to unrelated milestone)
    - GOLDEN_PROMOTION (git update-ref)
    - UNSAFE_AUTHORITY_MUTATION (authority change without repair)

Quarantine cleared ONLY by SafetyClearReceipt proving:
    - reproduction_before_result = FAIL  (bug reproduced before repair)
    - repair_sha: str                    (exact SHA of repair commit)
    - reproduction_after_result = PASS   (bug gone after repair)
    - relevant_regressions_result = PASS (no regressions)
    - independent_validation_result = PASS
    - authority_hashes_valid = True      (fresh hashes match expected)
    - verifier_subprocess_executed = True (commands actually ran as subprocesses)

Then: reload authority and resume interrupted work at exact interrupted state.

Reproduction baseline (F1, 2026-09-08):
    reproduction_before=FAIL and reproduction_after=PASS are separated by the repair,
    so they cannot be observed in the same cycle — after the repair the command can
    only return PASS.  capture_reproduction_baseline() therefore records the pre-repair
    observation on the first quarantine cycle, stores it on the quarantine record, and
    produce_safety_clear_receipt() replays it via reproduction_before_observation.
    Only a baseline whose own hash verifies and whose observed_by_subprocess is True
    is honoured, so the replay cannot be used to forge a clear.

B3 structural guarantee (2026-09-08):
    SafetyClearReceipt.verifier_subprocess_executed is False by default.
    It is set to True ONLY by produce_safety_clear_receipt() which executes all
    verification commands as real subprocesses.  A receipt constructed in memory
    (without going through that function) will have verifier_subprocess_executed=False
    and cannot satisfy all_proofs_satisfied.

    Counter-proof FORGED_SAFETY_CLEAR_ACCEPTED must be False:
        receipt = SafetyClearReceipt(
            reproduction_before_result="FAIL",
            reproduction_after_result="PASS",
            ...all fields set...
        )
        receipt.all_proofs_satisfied is False  # verifier_subprocess_executed defaults False
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal
from factory_v2._io import _atomic_write, _now, _now as _utc_now


_QUARANTINE_FILENAME = "SAFETY_QUARANTINE.json"
_CLEAR_RECEIPT_FILENAME = "SAFETY_CLEAR_RECEIPT.json"

ALLOWED_ACTIONS: frozenset[str] = frozenset({
    "DIAGNOSE",
    "AUDIT",
    "RESEARCH",
    "SAFE_REPAIR",
    "VALIDATE_REPAIR",
})

FORBIDDEN_ACTIONS: frozenset[str] = frozenset({
    "DISPATCH_PRODUCT",
    "UNRELATED_MILESTONE",
    "GOLDEN_PROMOTION",
    "UNSAFE_AUTHORITY_MUTATION",
})


# ── RepairContract ─────────────────────────────────────────────────────────────

@dataclass
class RepairContract:
    """The frozen, immutable definition of what "repaired" means for one quarantine.

    G3: a SafetyClearReceipt must not choose its own verification commands — a caller
    that picks `true` for every phase can author a receipt that satisfies every proof
    without repairing anything.  The commands are therefore frozen here, at quarantine
    time, derived from canonical authority, and the clear gate runs THESE commands
    itself.  The receipt only reports; the contract decides.

    pre_repair_sha pins the tree the bug was observed on, so reproduction evidence can
    be recovered honestly from an isolated checkout even after the repair has landed,
    instead of being fabricated after the fact.

    contract_sha is an integrity check over the frozen fields, never a proof of
    execution — a matching hash means "this contract was not edited", nothing more.
    """
    schema: str = "quarantine-repair-contract/v2"
    contract_id: str = ""
    # X5: full repair identity.  Without cause/attempt/fingerprint on the contract,
    # recovery records could only be matched by kind, so an old unrelated DIAGNOSE
    # could authorise scheduling a SAFE_REPAIR for a different quarantine entirely.
    cause: str = ""
    milestone_id: str = ""
    mission_id: str = ""
    attempt_id: str = ""
    failure_fingerprint: str = ""
    # Paths the repair is allowed to touch, as git pathspecs relative to command_cwd.
    # Empty means "the whole repo"; the commit step still refuses unrelated dirty files.
    allowed_scope: list = field(default_factory=list)
    idempotency_key: str = ""
    reproduction_command: str = ""
    regression_command: str = ""
    validation_command: str = ""
    command_cwd: str = ""
    # Optional deterministic typed repair recipe, declared in canonical authority.
    # When present SAFE_REPAIR runs THIS through the normal executor instead of asking
    # an engine to invent a fix — mechanical failures (a regenerable artifact, a
    # pipeline to re-run) should not cost a model call.  Absent -> the engine path.
    repair_command: str = ""
    pre_repair_sha: str = ""
    authority_hashes: dict = field(default_factory=dict)
    derived_from: str = ""          # which authority artefact the commands came from
    frozen_at: str = ""
    contract_sha: str = ""

    def compute_contract_sha(self) -> str:
        payload = ":".join([
            self.cause, self.milestone_id, self.mission_id, self.attempt_id,
            self.failure_fingerprint, json.dumps(sorted(self.allowed_scope)),
            self.reproduction_command, self.regression_command, self.validation_command,
            self.command_cwd, self.repair_command, self.pre_repair_sha,
            json.dumps(self.authority_hashes, sort_keys=True),
            self.frozen_at,
        ])
        return hashlib.sha256(payload.encode()).hexdigest()[:24]

    def verify_integrity(self) -> bool:
        """True when the contract is complete and has not been edited since freezing."""
        return (
            bool(self.contract_id)
            and bool(self.reproduction_command)
            and bool(self.regression_command)
            and bool(self.validation_command)
            and bool(self.command_cwd)
            and bool(self.pre_repair_sha)
            and bool(self.contract_sha)
            and self.compute_contract_sha() == self.contract_sha
        )

    @classmethod
    def from_dict(cls, d: dict | None) -> "RepairContract | None":
        if not isinstance(d, dict) or d.get("schema") != "quarantine-repair-contract/v2":
            return None
        try:
            return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        except TypeError:
            return None


# X2: the typed statuses a repair chain can end in.  A state machine whose only
# terminal states are "cleared" or "pending forever" is not finished — every escape has
# to be nameable.  Adapted from the small typed-stall pattern in OctopusGarden and AWS
# stuck_guard (steer once on a new fingerprint, then stop); no framework, no dependency.
REPAIR_STATUS_ACTIVE = "ACTIVE"
REPAIR_STATUS_STALLED = "STALLED"
REPAIR_STATUS_BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
REPAIR_STATUS_MAX_ITERATIONS = "MAX_ITERATIONS"
REPAIR_STATUS_EXTERNAL_BLOCK = "EXTERNAL_BLOCK"
TERMINAL_REPAIR_STATUSES = frozenset({
    REPAIR_STATUS_STALLED, REPAIR_STATUS_BUDGET_EXCEEDED,
    REPAIR_STATUS_MAX_ITERATIONS, REPAIR_STATUS_EXTERNAL_BLOCK,
})

# Hard cap on repair cycles for one quarantine.  Bounded so an unrepairable condition
# stops consuming cycles and becomes a named, inspectable outcome instead of an
# indefinite *_PENDING loop.
MAX_REPAIR_ITERATIONS = 8


def get_repair_chain(project_dir: Path) -> dict:
    record = _read_quarantine(Path(project_dir) / _QUARANTINE_FILENAME)
    if record is None:
        return {}
    return dict(record.get("repair_chain") or {})


def update_repair_chain(project_dir: Path, **fields) -> dict:
    """Persist repair-chain progress on the quarantine record."""
    qpath = Path(project_dir) / _QUARANTINE_FILENAME
    record = _read_quarantine(qpath)
    if record is None:
        return {}
    chain = dict(record.get("repair_chain") or {})
    chain.setdefault("status", REPAIR_STATUS_ACTIVE)
    chain.setdefault("iterations", 0)
    chain.setdefault("fingerprints", [])
    chain.update(fields)

    # P0-2 — current_episode is the ONLY mutable source of the counter, the
    # fingerprints, the status, the reason and the terminal SHA. The top-level
    # fields are a read-only PROJECTION of it.
    #
    # Before this, production wrote only the top-level fields and repair_epoch.bump()
    # had no caller at all, so the two drifted: measured at 2900f4c8,
    # TOP_LEVEL_ITERATIONS=7 while EPISODE_ITERATIONS=3, and
    # TOP_LEVEL_STATUS=MAX_ITERATIONS while EPISODE_STATUS=ACTIVE. A re-entry reset
    # the episode and the production counter carried on from the old number, which
    # is why re-entry "succeeded" and changed nothing.
    _EPISODE_OWNED = ("iterations", "fingerprints", "status", "reason",
                      "terminal_code_sha")
    ep = dict(chain.get("current_episode") or {})
    if ep:
        for k in _EPISODE_OWNED:
            if k in fields:
                ep[k] = fields[k]
        chain["current_episode"] = ep
        for k in _EPISODE_OWNED:
            if k in ep:
                chain[k] = ep[k]        # projection, never an independent value

    record["repair_chain"] = chain
    record["recovery_state"] = chain["status"]
    _atomic_write(qpath, record)
    return chain


def compute_idempotency_key(cause: str, milestone_id: str, mission_id: str,
                            attempt_id: str, failure_fingerprint: str) -> str:
    """Identity of a blocking CONDITION, stable across restarts and re-entries.

    Recovery work is filtered by this rather than by kind, so an old DIAGNOSE from an
    unrelated quarantine cannot authorise a SAFE_REPAIR for the current one — and so
    work already done for this exact condition (e.g. a validation gap's diagnosis) is
    recognised instead of being repeated.
    """
    return hashlib.sha256(":".join([
        cause, milestone_id, mission_id, attempt_id, (failure_fingerprint or "")[:300],
    ]).encode()).hexdigest()[:16]


def freeze_repair_contract(
    project_dir: Path, *,
    milestone_id: str,
    mission_id: str,
    reproduction_command: str,
    regression_command: str,
    validation_command: str,
    command_cwd: str,
    pre_repair_sha: str,
    authority_hashes: dict,
    cause: str = "",
    attempt_id: str = "",
    failure_fingerprint: str = "",
    allowed_scope: list | None = None,
    repair_command: str = "",
    derived_from: str = "",
) -> RepairContract | None:
    """Attach a frozen RepairContract to the live quarantine record.

    Frozen means frozen: an existing contract that still verifies is returned
    unchanged, so a later cycle cannot quietly swap the commands for easier ones.
    """
    qpath = Path(project_dir) / _QUARANTINE_FILENAME
    record = _read_quarantine(qpath)
    if record is None:
        return None
    existing = RepairContract.from_dict(record.get("repair_contract"))
    if existing is not None and existing.verify_integrity():
        return existing

    contract = RepairContract(
        cause=cause or record.get("cause", ""),
        milestone_id=milestone_id,
        mission_id=mission_id,
        attempt_id=attempt_id or record.get("attempt_id", ""),
        failure_fingerprint=(failure_fingerprint
                             or record.get("failure_fingerprint", ""))[:300],
        allowed_scope=sorted(allowed_scope or []),
        reproduction_command=reproduction_command,
        regression_command=regression_command,
        validation_command=validation_command,
        command_cwd=str(command_cwd),
        repair_command=repair_command,
        pre_repair_sha=pre_repair_sha,
        authority_hashes=dict(authority_hashes or {}),
        derived_from=derived_from,
        frozen_at=_utc_now(),
    )
    # idempotency_key identifies the blocking CONDITION, not this particular freeze:
    # re-entering the same quarantine after a restart must map to the same repair, so
    # recovery work can be filtered by it instead of by kind alone.
    contract.idempotency_key = compute_idempotency_key(
        contract.cause, contract.milestone_id, contract.mission_id,
        contract.attempt_id, contract.failure_fingerprint)
    contract.contract_sha = contract.compute_contract_sha()
    contract.contract_id = contract.contract_sha
    record["repair_contract"] = asdict(contract)
    record["recovery_state"] = "AWAITING_DIAGNOSIS"
    _atomic_write(qpath, record)
    return contract


def get_repair_contract(project_dir: Path) -> "RepairContract | None":
    record = _read_quarantine(Path(project_dir) / _QUARANTINE_FILENAME)
    if record is None:
        return None
    return RepairContract.from_dict(record.get("repair_contract"))


def _as_shell(command: "str | list[str]") -> str:
    """Normalize a command to a shell string.  Frozen contract commands are shell
    strings (they come from product_e2e.test_command); the older list form is still
    accepted and joined."""
    if isinstance(command, str):
        return command
    return " ".join(command)


def _run_contract_command(
    command: "str | list[str]", cwd: str | None, timeout: int = 600,
) -> tuple[int, str]:
    """Run one frozen contract command with the factory's canonical shell semantics.

    errexit+pipefail so `false | true` fails, TimeoutExpired classified as rc 124 —
    identical to IndependentValidator, so the gate cannot be more lenient than the
    validator that originally found the failure.
    """
    shell_cmd = _as_shell(command)
    try:
        # The control-repo environment, same as _shell_run, LocalCommandExecutor and
        # the independent validator. This was the THIRD spawn site to be missing it:
        # the gate ran the frozen reproduction with FACTORY_CONTROL_REPO unset, so a
        # command that returns rc=0 by hand failed closed here and the quarantine
        # could never clear. The variable is exactly what the frozen command declares
        # it needs.
        from .mission_evidence import control_repo_env
        r = subprocess.run(
            ["bash", "-c", f"set -eo pipefail\n{shell_cmd}"],
            capture_output=True, text=True, timeout=timeout, cwd=cwd,
            env=control_repo_env(),
        )
        return r.returncode, (r.stdout + r.stderr).strip()[-4000:]
    except subprocess.TimeoutExpired:
        return 124, "TIMEOUT"
    except Exception as exc:
        return 125, f"ERROR:{exc}"


class _DetachedCheckout:
    """A supervisor-created git worktree pinned to one exact commit.

    X1: the shared working tree is not evidence for a SHA.  Proofs run against a
    mutable repo can pass on uncommitted edits while the claimed commit is still
    broken — that is exactly how a dirty tree plus an unchanged repair_sha cleared
    quarantine.  Every proof therefore runs inside a checkout of the exact commit it
    claims to be about, which by construction contains only committed content.
    """

    def __init__(self, repo: str, sha: str):
        self.repo = str(repo)
        self.sha = sha
        self._tmp: str | None = None
        self.path: str | None = None

    def __enter__(self) -> "_DetachedCheckout":
        rc, _ = _git_out(self.repo, "cat-file", "-e", f"{self.sha}^{{commit}}")
        if rc != 0:
            raise QuarantineClearFailed(
                f"SHA_NOT_A_COMMIT: {self.sha!r} does not exist in {self.repo}")
        self._tmp = tempfile.mkdtemp(prefix="q_checkout_")
        path = str(Path(self._tmp) / "tree")
        rc, out = _git_out(self.repo, "worktree", "add", "--detach", path, self.sha)
        if rc != 0:
            shutil.rmtree(self._tmp, ignore_errors=True)
            raise QuarantineClearFailed(
                f"CHECKOUT_FAILED for {self.sha[:12]}: {out[:200]}")
        self.path = path
        return self

    def __exit__(self, *exc) -> None:
        if self.path:
            _git_out(self.repo, "worktree", "remove", "--force", self.path)
        if self._tmp:
            shutil.rmtree(self._tmp, ignore_errors=True)
        return None

    def run(self, command: "str | list[str]", timeout: int = 600) -> tuple[int, str]:
        """Run a frozen command inside this checkout.

        `{checkout}` in the command is substituted with the checkout path, so a frozen
        command can point an external validator at the exact tree under test instead of
        at the mutable product repo.
        """
        shell_cmd = _as_shell(command).replace("{checkout}", self.path or "")
        return _run_contract_command(shell_cmd, self.path, timeout)


def _git_out(cwd: str, *args: str) -> tuple[int, str]:
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                           text=True, timeout=60)
        return r.returncode, r.stdout.strip()
    except Exception as exc:
        return 1, str(exc)


# ── ReproductionBaseline ───────────────────────────────────────────────────────

@dataclass
class ReproductionBaseline:
    """Subprocess-observed reproduction of the quarantine cause, captured BEFORE repair.

    Why this exists: `all_proofs_satisfied` requires reproduction_before=FAIL and
    reproduction_after=PASS.  Those two observations are separated by the repair, so
    they cannot both be made in the same cycle — once the repair lands, re-running the
    reproduction command can only ever return PASS.  Without a durable baseline the
    quarantine can never be cleared autonomously, which is exactly the F1 dead end.

    The baseline is therefore captured on the first quarantine cycle, persisted inside
    SAFETY_QUARANTINE.json, and replayed into the clear receipt later.  It carries its
    own hash and an observed_by_subprocess flag set only by capture_reproduction_baseline(),
    so a hand-authored baseline cannot unlock a clear.
    """
    schema: str = "reproduction-baseline/v1"
    command: str = ""
    exit_code: int = -1
    result: Literal["FAIL", "PASS", "NOT_RUN"] = "NOT_RUN"
    output_tail: str = ""
    captured_at: str = ""
    observed_by_subprocess: bool = False
    # G1/G3: which frozen contract this observation belongs to, and the exact tree it
    # was observed on.  The clear gate requires tree_sha == contract.pre_repair_sha, so
    # a baseline captured against the already-repaired tree cannot be passed off as
    # pre-repair evidence.
    contract_id: str = ""
    contract_sha: str = ""
    tree_sha: str = ""
    baseline_sha: str = ""

    def compute_baseline_sha(self) -> str:
        payload = (f"{self.command}:{self.exit_code}:{self.result}:"
                   f"{self.captured_at}:{self.observed_by_subprocess}:"
                   f"{self.contract_id}:{self.contract_sha}:{self.tree_sha}")
        return hashlib.sha256(payload.encode()).hexdigest()[:24]

    def verify(self) -> bool:
        """Integrity + shape check.  NOT proof of execution — see verify_against().

        A hand-authored baseline can satisfy this by recomputing its own hash, which
        is exactly why the clear gate never relies on it alone.
        """
        return (
            self.observed_by_subprocess
            and self.result == "FAIL"
            and bool(self.baseline_sha)
            and self.compute_baseline_sha() == self.baseline_sha
        )

    def verify_against(self, contract: "RepairContract") -> tuple[bool, str]:
        """Bind this observation to a frozen contract and its pre-repair tree.

        Returns (ok, reason).  The tree_sha check is what makes fabricated pre-repair
        evidence useless: the only way to produce a FAIL on contract.pre_repair_sha is
        to have actually run the frozen command against that exact tree.
        """
        if not self.verify():
            return False, "baseline integrity/shape check failed"
        if self.contract_sha != contract.contract_sha:
            return False, (f"baseline belongs to contract {self.contract_sha!r}, "
                           f"frozen contract is {contract.contract_sha!r}")
        if self.command != contract.reproduction_command:
            return False, "baseline command is not the frozen reproduction command"
        if not self.tree_sha or self.tree_sha != contract.pre_repair_sha:
            return False, (f"baseline observed on tree {self.tree_sha!r}, "
                           f"contract pre_repair_sha is {contract.pre_repair_sha!r}")
        return True, ""

    @classmethod
    def from_dict(cls, d: dict | None) -> "ReproductionBaseline | None":
        if not isinstance(d, dict) or d.get("schema") != "reproduction-baseline/v1":
            return None
        try:
            return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        except TypeError:
            return None


def capture_reproduction_baseline(
    command: "str | list[str]", *, cwd: str | None = None, timeout: int = 600,
    contract: "RepairContract | None" = None, tree_sha: str = "",
) -> ReproductionBaseline:
    """Run the reproduction command NOW and record what actually happened.

    When a frozen contract is supplied the command is taken FROM the contract — the
    caller does not get to pick an easier one — and the observation is stamped with the
    contract identity and the tree it ran against.
    """
    if contract is not None:
        command = contract.reproduction_command
    # L2_runtime_core-013: `{checkout}` means "the tree this command runs
    # against" — exactly what _DetachedCheckout.run substitutes. Without this
    # the pre-repair baseline tested a literal placeholder path and failed for
    # a reason that had nothing to do with the product.
    run_cmd = _as_shell(command).replace("{checkout}", cwd or "")
    rc, out = _run_contract_command(run_cmd, cwd, timeout)
    baseline = ReproductionBaseline(
        command=_as_shell(command),
        exit_code=rc,
        result="FAIL" if rc != 0 else "PASS",
        output_tail=out[-2000:],
        captured_at=_utc_now(),
        observed_by_subprocess=True,
        contract_id=contract.contract_id if contract else "",
        contract_sha=contract.contract_sha if contract else "",
        tree_sha=tree_sha,
    )
    baseline.baseline_sha = baseline.compute_baseline_sha()
    return baseline


def capture_reproduction_baseline_at_sha(
    contract: "RepairContract", *, timeout: int = 600,
) -> tuple["ReproductionBaseline | None", str]:
    """Observe the frozen reproduction against an ISOLATED checkout of pre_repair_sha.

    G1 requirement 4: after the repair lands, re-running the reproduction in the live
    tree can only ever pass, so pre-repair evidence must come from the pre-repair tree
    — recovered honestly, never fabricated.  A detached `git worktree` at
    contract.pre_repair_sha gives that without touching the working tree.

    Returns (baseline, reason).  baseline is None when the isolated checkout could not
    be created, in which case no evidence is invented.
    """
    repo = contract.command_cwd
    rc, _ = _git_out(repo, "cat-file", "-e", f"{contract.pre_repair_sha}^{{commit}}")
    if rc != 0:
        return None, f"pre_repair_sha {contract.pre_repair_sha!r} is not a commit in {repo}"
    tmpdir = tempfile.mkdtemp(prefix="q_prerepair_")
    checkout = str(Path(tmpdir) / "tree")
    rc, out = _git_out(repo, "worktree", "add", "--detach", checkout,
                       contract.pre_repair_sha)
    if rc != 0:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return None, f"could not create isolated pre-repair checkout: {out[:200]}"
    try:
        baseline = capture_reproduction_baseline(
            contract.reproduction_command, cwd=checkout, timeout=timeout,
            contract=contract, tree_sha=contract.pre_repair_sha,
        )
    finally:
        _git_out(repo, "worktree", "remove", "--force", checkout)
        shutil.rmtree(tmpdir, ignore_errors=True)
    return baseline, ""


# ── SafetyClearReceipt ─────────────────────────────────────────────────────────

@dataclass
class SafetyClearReceipt:
    """Proof bundle required to exit quarantine.

    All fields must be derived from observable subprocess outcomes, not authored.
    """
    schema: str = "safety-clear-receipt/v1"
    produced_at: str = ""
    project_id: str = ""
    quarantine_entered_at: str = ""
    # Reproduction before repair — must be FAIL.
    reproduction_before_command: str = ""
    reproduction_before_exit_code: int = -1
    reproduction_before_result: Literal["FAIL", "PASS", "NOT_RUN"] = "NOT_RUN"
    # Repair.
    repair_sha: str = ""
    # Reproduction after repair — must be PASS.
    reproduction_after_command: str = ""
    reproduction_after_exit_code: int = -1
    reproduction_after_result: Literal["FAIL", "PASS", "NOT_RUN"] = "NOT_RUN"
    # Regression check — must be PASS.
    regression_command: str = ""
    regression_exit_code: int = -1
    relevant_regressions_result: Literal["FAIL", "PASS", "NOT_RUN"] = "NOT_RUN"
    # Independent validation — must be PASS.
    independent_validation_command: str = ""
    independent_validation_exit_code: int = -1
    independent_validation_result: Literal["FAIL", "PASS", "NOT_RUN"] = "NOT_RUN"
    # Authority hashes at clear time.
    authority_hashes_valid: bool = False
    authority_hashes: dict = field(default_factory=dict)
    # B3 structural guard: True ONLY when set by produce_safety_clear_receipt().
    # NOTE (G3): this is a public field, so it is a hint, not a proof.  A caller can
    # set it and recompute receipt_sha.  The clear gate therefore never trusts it —
    # see clear_quarantine_with_receipt(), which re-runs the frozen contract itself.
    verifier_subprocess_executed: bool = False
    # G3: which frozen RepairContract this receipt answers.  A receipt that does not
    # name the live contract cannot clear anything.
    contract_id: str = ""
    contract_sha: str = ""
    # Filled in by the clear gate with what IT observed, not by the receipt's author.
    gate_observations: list = field(default_factory=list)
    # Computed at write time.
    receipt_sha: str = ""

    @property
    def all_proofs_satisfied(self) -> bool:
        """Cheap precondition only.  Every field here is caller-writable, so a True
        result means "worth taking to the gate", never "quarantine may be cleared."
        """
        return (
            self.reproduction_before_result == "FAIL"
            and self.reproduction_after_result == "PASS"
            and self.relevant_regressions_result == "PASS"
            and self.independent_validation_result == "PASS"
            and self.authority_hashes_valid
            and bool(self.repair_sha)
            and self.verifier_subprocess_executed  # B3: subprocess execution mandatory
        )

    def compute_receipt_sha(self) -> str:
        payload = (
            f"{self.reproduction_before_exit_code}:"
            f"{self.reproduction_after_exit_code}:"
            f"{self.regression_exit_code}:"
            f"{self.independent_validation_exit_code}:"
            f"{self.repair_sha}:"
            f"{self.produced_at}:"
            f"{self.verifier_subprocess_executed}:"  # B3: included in tamper-evident SHA
            f"{self.contract_id}:{self.contract_sha}"
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:24]

    def write(self, project_dir: Path) -> Path:
        self.receipt_sha = self.compute_receipt_sha()
        path = Path(project_dir) / _CLEAR_RECEIPT_FILENAME
        _atomic_write(path, asdict(self))
        return path

    @classmethod
    def load(cls, project_dir: Path) -> "SafetyClearReceipt | None":
        path = Path(project_dir) / _CLEAR_RECEIPT_FILENAME
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            if d.get("schema") != "safety-clear-receipt/v1":
                return None
            return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        except (FileNotFoundError, json.JSONDecodeError, TypeError):
            return None

    def verify_receipt_sha(self) -> bool:
        return self.compute_receipt_sha() == self.receipt_sha


# ── quarantine record ──────────────────────────────────────────────────────────

def _consume_bound_stop(project_dir: Path, record: dict) -> None:
    """Transfer one exact stop trigger into durable quarantine custody.

    Called only AFTER the quarantine is durable. Its digest binds the trigger:
    a later/different stop is never consumed by clearing this incident. Replaying
    after a crash between archive and unlink is idempotent.
    """
    stop = Path(project_dir) / "SAFETY_STOP.json"
    expected = record.get("stop_trigger_sha256")
    if not expected or not stop.exists():
        return
    raw = stop.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected:
        return
    archive = Path(project_dir) / "evidence" / "safety_stops" / f"{expected}.json"
    archive.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(archive, {"schema": "consumed-safety-stop/v1", "sha256": expected,
                           "trigger_raw": raw.decode("utf-8", errors="replace"),
                           "quarantine_entered_at": record.get("entered_at"),
                           "note": "Transferred to quarantine; no repair claimed."})
    # Stop producers and quarantine transitions share the project's single writer.
    if stop.exists() and hashlib.sha256(stop.read_bytes()).hexdigest() == expected:
        _durable_unlink(stop)


def enter_quarantine(
    project_dir: Path,
    cause: str,
    milestone_id: str = "",
    mission_id: str = "",
    attempt_id: str = "",
    failure_fingerprint: str = "",
    candidate_sha: str = "",
    authority_hashes: dict | None = None,
) -> dict:
    """Write SAFETY_QUARANTINE.json atomically with full scope.

    Idempotent: if quarantine already active, updates last_seen without
    changing the original entry context.
    """
    project_dir = Path(project_dir)
    qpath = project_dir / _QUARANTINE_FILENAME
    existing = _read_quarantine(qpath)
    if existing is not None:
        record = dict(existing)
        record["last_seen"] = _utc_now()
    else:
        record = {
            "schema": "safety-quarantine/v2",
            "state": "SAFETY_QUARANTINE",
            "cause": cause,
            "milestone_id": milestone_id,
            "mission_id": mission_id,
            "attempt_id": attempt_id,
            "failure_fingerprint": failure_fingerprint,
            "candidate_sha": candidate_sha,
            "authority_hashes": authority_hashes or {},
            "allowed_actions": sorted(ALLOWED_ACTIONS),
            "forbidden_actions": sorted(FORBIDDEN_ACTIONS),
            "recovery_state": "AWAITING_DIAGNOSIS",
            "entered_at": _utc_now(),
            "last_seen": _utc_now(),
        }
    stop = project_dir / "SAFETY_STOP.json"
    if stop.exists() and not record.get("stop_trigger_sha256"):
        raw = stop.read_bytes()
        try:
            trigger = json.loads(raw)
        except (ValueError, UnicodeError):
            trigger = {}
        # A new quarantine owns the trigger that caused it. A legacy quarantine
        # may adopt its still-unconsumed matching trigger; a foreign stop stays pending.
        if existing is None or (isinstance(trigger, dict)
                                and trigger.get("cause") == record.get("cause")):
            record["stop_trigger_sha256"] = hashlib.sha256(raw).hexdigest()
    _atomic_write(qpath, record)
    _consume_bound_stop(project_dir, record)
    return record


def is_in_quarantine(project_dir: Path) -> bool:
    qpath = Path(project_dir) / _QUARANTINE_FILENAME
    return _read_quarantine(qpath) is not None


def adopt_quarantine(project_dir: Path) -> dict | None:
    """Return quarantine record on restart (touch last_seen)."""
    qpath = Path(project_dir) / _QUARANTINE_FILENAME
    record = _read_quarantine(qpath)
    if record is None:
        return None
    # Also captures an unconsumed matching trigger from pre-upgrade quarantines.
    return enter_quarantine(project_dir, cause=record.get("cause", ""))


def record_reproduction_baseline(
    project_dir: Path, baseline: "ReproductionBaseline",
) -> dict | None:
    """Persist the pre-repair reproduction observation into the quarantine record.

    Stored on the quarantine record rather than a side file so it is adopted with
    the quarantine on restart and disappears with it when the quarantine clears.
    """
    qpath = Path(project_dir) / _QUARANTINE_FILENAME
    record = _read_quarantine(qpath)
    if record is None:
        return None
    record["reproduction_baseline"] = asdict(baseline)
    record["recovery_state"] = ("AWAITING_REPAIR" if baseline.verify()
                                else "NOT_REPRODUCED")
    _atomic_write(qpath, record)
    return record


def get_reproduction_baseline(project_dir: Path) -> "ReproductionBaseline | None":
    """Return the persisted pre-repair baseline, or None if none was captured."""
    record = _read_quarantine(Path(project_dir) / _QUARANTINE_FILENAME)
    if record is None:
        return None
    return ReproductionBaseline.from_dict(record.get("reproduction_baseline"))


def assert_action_allowed(project_dir: Path, action: str) -> None:
    """Raise QuarantineActionForbidden if action is not allowed in quarantine."""
    if not is_in_quarantine(project_dir):
        return
    if action in FORBIDDEN_ACTIONS:
        raise QuarantineActionForbidden(
            f"QUARANTINE_ACTION_FORBIDDEN: {action!r} is not allowed while project is in "
            f"SAFETY_QUARANTINE. Allowed: {sorted(ALLOWED_ACTIONS)}. "
            "Clear quarantine with a valid SafetyClearReceipt first."
        )


def clear_quarantine_with_receipt(
    project_dir: Path,
    receipt: SafetyClearReceipt,
    *,
    timeout: int = 600,
) -> dict:
    """The clear gate.  Quarantine is released only by evidence the gate produced itself.

    Nothing the caller supplies is authority.  Every field on a SafetyClearReceipt and
    on a stored ReproductionBaseline is caller-writable and their hashes are unkeyed, so
    a hash proves the bytes were not edited and nothing else.  Two things follow, and
    they are the whole design of this function:

    1. The gate runs the frozen contract's OWN commands, never the receipt's.
    2. It runs them in supervisor-created detached checkouts pinned to exact commits —
       `pre_repair_sha` for the reproduction, `repair_sha` for everything after.  The
       mutable working tree is never evidence for a SHA: proofs run there can pass on
       uncommitted edits while the claimed commit is still broken, which is precisely
       how a dirty tree with an unchanged repair_sha used to clear quarantine.

    So the gate re-observes the bug at pre_repair_sha (must FAIL) and re-observes the
    fix at repair_sha (must PASS), both from committed content only.  A stored baseline
    is audit data; it is never trusted as the pre-repair proof.

    Raises QuarantineClearFailed on every rejection path — there is no fail-open branch.
    """
    from .project_authority import load_authority  # local import: avoids a cycle

    project_dir = Path(project_dir)
    qpath = project_dir / _QUARANTINE_FILENAME
    record = _read_quarantine(qpath)
    if record is None:
        raise QuarantineClearFailed("Not in quarantine — nothing to clear")

    # ── 1. A frozen contract must exist. No contract → nothing defines "repaired". ──
    contract = RepairContract.from_dict(record.get("repair_contract"))
    if contract is None or not contract.verify_integrity():
        raise QuarantineClearFailed(
            "NO_FROZEN_REPAIR_CONTRACT: quarantine carries no intact RepairContract, so "
            "there is no authority-derived definition of repair to verify against. "
            "A receipt cannot supply its own commands."
        )

    # ── 2. Receipt integrity, and that it answers THIS contract. ──────────────────
    if not receipt.verify_receipt_sha():
        raise QuarantineClearFailed(
            "SafetyClearReceipt receipt_sha verification failed — may be authored"
        )
    if receipt.contract_id != contract.contract_id or receipt.contract_sha != contract.contract_sha:
        raise QuarantineClearFailed(
            f"RECEIPT_CONTRACT_MISMATCH: receipt names contract "
            f"{receipt.contract_id!r}/{receipt.contract_sha!r}, live frozen contract is "
            f"{contract.contract_id!r}/{contract.contract_sha!r}"
        )

    # ── 3. repair_sha must be a strictly NEW commit descended from pre_repair_sha. ─
    repo = contract.command_cwd
    if not receipt.repair_sha:
        raise QuarantineClearFailed("repair_sha is empty")
    if receipt.repair_sha == contract.pre_repair_sha:
        raise QuarantineClearFailed(
            f"UNCHANGED_REPAIR_SHA: claimed repair {receipt.repair_sha[:12]} IS the "
            f"frozen pre_repair_sha — nothing was committed, so nothing was repaired"
        )
    rc, _ = _git_out(repo, "cat-file", "-e", f"{receipt.repair_sha}^{{commit}}")
    if rc != 0:
        raise QuarantineClearFailed(
            f"REPAIR_SHA_NOT_A_COMMIT: {receipt.repair_sha!r} does not exist in {repo}"
        )
    rc, _ = _git_out(repo, "merge-base", "--is-ancestor",
                     contract.pre_repair_sha, receipt.repair_sha)
    if rc != 0:
        raise QuarantineClearFailed(
            f"REPAIR_SHA_NOT_DESCENDED_FROM_PRE_REPAIR: {receipt.repair_sha[:12]} does not "
            f"descend from frozen pre_repair_sha {contract.pre_repair_sha[:12]}"
        )

    gate_observations: list[dict] = []

    def _observe(phase: str, sha: str, checkout: "_DetachedCheckout",
                 command: str) -> tuple[int, str]:
        rc, tail = checkout.run(command, timeout)
        gate_observations.append({
            "phase": phase, "command": command, "tested_sha": sha,
            "checkout": checkout.path, "exit_code": rc,
            "result": "PASS" if rc == 0 else "FAIL", "observed_at": _utc_now(),
            "output_tail": tail[-1000:],
            "executed_by": "clear_quarantine_with_receipt",
        })
        return rc, tail

    # ── 4. Re-observe the BUG at pre_repair_sha. The gate must see it fail. ───────
    # A stored baseline cannot substitute for this: it is caller-writable, so a project
    # whose reproduction always passed could be handed a hand-authored FAIL and clear
    # without a bug ever existing.
    with _DetachedCheckout(repo, contract.pre_repair_sha) as pre:
        rc, tail = _observe("reproduction_before", contract.pre_repair_sha, pre,
                            contract.reproduction_command)
    if rc == 0:
        receipt.gate_observations = gate_observations
        raise QuarantineClearFailed(
            f"GATE_REPRODUCTION_BEFORE_PASSED: the frozen reproduction "
            f"{contract.reproduction_command!r} SUCCEEDS at pre_repair_sha "
            f"{contract.pre_repair_sha[:12]} — there is no reproduced bug to have "
            f"repaired, so nothing can be cleared. Output: {tail[-400:]}"
        )

    # ── 5. Re-observe the FIX at repair_sha, from committed content only. ────────
    with _DetachedCheckout(repo, receipt.repair_sha) as post:
        for phase, command in (
            ("reproduction_after", contract.reproduction_command),
            ("relevant_regressions", contract.regression_command),
            ("independent_validation", contract.validation_command),
        ):
            rc, tail = _observe(phase, receipt.repair_sha, post, command)
            if rc != 0:
                receipt.gate_observations = gate_observations
                raise QuarantineClearFailed(
                    f"GATE_{phase.upper()}_FAILED: frozen contract command "
                    f"{command!r} exited {rc} at repair_sha {receipt.repair_sha[:12]} — "
                    f"the product is not repaired at the commit being claimed. "
                    f"Output: {tail[-400:]}"
                )

    # ── 6. Authority verified by reloading it, not by a caller boolean. ──────────
    try:
        fresh = load_authority(project_dir).identity.as_dict()
    except Exception as exc:
        raise QuarantineClearFailed(f"AUTHORITY_UNLOADABLE: {exc}") from exc
    drifted = {k: (contract.authority_hashes.get(k), fresh.get(k))
               for k in set(contract.authority_hashes) | set(fresh)
               if contract.authority_hashes.get(k) != fresh.get(k)}
    if drifted:
        raise QuarantineClearFailed(
            f"AUTHORITY_DRIFT_SINCE_CONTRACT_FROZEN: {drifted} — canonical authority "
            "changed under the repair; the frozen contract no longer describes it"
        )

    # ── All proofs are the gate's own. Persist them, then release durably. ───────
    # Backfill the receipt's phase fields from what the GATE observed, so the persisted
    # receipt reports the authoritative exact-SHA run rather than whatever the producer
    # happened to see in a mutable tree.
    _phase_fields = {
        "reproduction_before": ("reproduction_before_exit_code",
                                "reproduction_before_result"),
        "reproduction_after": ("reproduction_after_exit_code",
                               "reproduction_after_result"),
        "relevant_regressions": ("regression_exit_code", "relevant_regressions_result"),
        "independent_validation": ("independent_validation_exit_code",
                                   "independent_validation_result"),
    }
    for obs in gate_observations:
        code_field, result_field = _phase_fields[obs["phase"]]
        setattr(receipt, code_field, obs["exit_code"])
        # reproduction_before is expected to FAIL; the gate asserted that above.
        setattr(receipt, result_field,
                "FAIL" if obs["phase"] == "reproduction_before" else obs["result"])
    receipt.gate_observations = gate_observations
    receipt.write(project_dir)
    _durable_unlink(qpath)

    return {
        "cleared_at": _utc_now(),
        "repair_sha": receipt.repair_sha,
        "pre_repair_sha": contract.pre_repair_sha,
        "contract_id": contract.contract_id,
        "idempotency_key": contract.idempotency_key,
        "interrupted_milestone": record.get("milestone_id", ""),
        "interrupted_mission": record.get("mission_id", ""),
        "resume_from": record.get("milestone_id", ""),
        "authority_hashes": fresh,
        "gate_observations": gate_observations,
    }


def produce_safety_clear_receipt(
    *,
    project_id: str,
    quarantine_entered_at: str,
    repair_sha: str,
    contract: "RepairContract | None" = None,
    reproduction_before_command: "str | list[str] | None" = None,
    reproduction_after_command: "str | list[str] | None" = None,
    regression_command: "str | list[str] | None" = None,
    independent_validation_command: "str | list[str] | None" = None,
    authority_hashes: dict | None = None,
    cwd: str | None = None,
    timeout: int = 600,
    reproduction_before_observation: "ReproductionBaseline | None" = None,
    run_precheck: bool = True,
) -> SafetyClearReceipt:
    """Run the verification commands and report what happened.

    When a frozen RepairContract is supplied — the production path — every command and
    the working directory come FROM the contract.  A caller cannot substitute easier
    commands, which is the hole that let a receipt "prove" a repair with three `true`s.

    This function only reports.  It does not decide: clear_quarantine_with_receipt()
    re-runs the frozen commands itself before releasing anything, because every field
    here is caller-writable and its hash is unkeyed.
    """
    if contract is not None:
        reproduction_before_command = contract.reproduction_command
        reproduction_after_command = contract.reproduction_command
        regression_command = contract.regression_command
        independent_validation_command = contract.validation_command
        cwd = contract.command_cwd
    if reproduction_after_command is None:
        raise ValueError("produce_safety_clear_receipt requires a contract or explicit commands")

    def _run(cmd) -> tuple[int, str]:
        if not run_precheck:
            # The clear gate re-runs every command in detached checkouts pinned to the
            # exact SHAs, so running them here too would only double the cost and
            # produce weaker evidence (mutable tree).  Report NOT_RUN and let the gate
            # backfill what it actually observed.
            return -1, "NOT_RUN — clear gate runs the authoritative exact-SHA proofs"
        return _run_contract_command(cmd, cwd, timeout)

    # reproduction_before: replay the durable pre-repair observation when one exists.
    # Re-running the command here would execute it against the REPAIRED tree, where it
    # passes — which is precisely why an unaided quarantine can never clear itself.
    # Only a baseline that verifies (subprocess-observed, FAIL, hash intact) is honoured.
    if reproduction_before_observation is not None and reproduction_before_observation.verify():
        before_cmd_str = reproduction_before_observation.command
        before_rc = reproduction_before_observation.exit_code
    else:
        before_cmd_str = _as_shell(reproduction_before_command or reproduction_after_command)
        before_rc, _ = _run(before_cmd_str)
    after_rc, _ = _run(reproduction_after_command)
    regression_rc, _ = _run(regression_command)
    validation_rc, _ = _run(independent_validation_command)

    now = _utc_now()
    receipt = SafetyClearReceipt(
        produced_at=now,
        project_id=project_id,
        quarantine_entered_at=quarantine_entered_at,
        reproduction_before_command=before_cmd_str,
        reproduction_before_exit_code=before_rc,
        reproduction_before_result="FAIL" if before_rc != 0 else "PASS",
        repair_sha=repair_sha,
        reproduction_after_command=_as_shell(reproduction_after_command),
        reproduction_after_exit_code=after_rc,
        reproduction_after_result="PASS" if after_rc == 0 else "FAIL",
        regression_command=_as_shell(regression_command),
        regression_exit_code=regression_rc,
        relevant_regressions_result="PASS" if regression_rc == 0 else "FAIL",
        independent_validation_command=_as_shell(independent_validation_command),
        independent_validation_exit_code=validation_rc,
        independent_validation_result="PASS" if validation_rc == 0 else "FAIL",
        authority_hashes_valid=bool(authority_hashes),
        authority_hashes=authority_hashes or {},
        verifier_subprocess_executed=True,  # only set here — after real subprocess execution
        contract_id=contract.contract_id if contract else "",
        contract_sha=contract.contract_sha if contract else "",
    )
    receipt.receipt_sha = receipt.compute_receipt_sha()
    return receipt


def clear_quarantine(project_dir: Path) -> None:
    """Legacy clear — remove quarantine file (for test reset or human override).

    Production code should use clear_quarantine_with_receipt() instead.
    """
    qpath = Path(project_dir) / _QUARANTINE_FILENAME
    try:
        qpath.unlink()
    except FileNotFoundError:
        pass


def get_quarantine_cause(project_dir: Path) -> str | None:
    qpath = Path(project_dir) / _QUARANTINE_FILENAME
    record = _read_quarantine(qpath)
    return record.get("cause") if record else None


def get_quarantine_record(project_dir: Path) -> dict | None:
    qpath = Path(project_dir) / _QUARANTINE_FILENAME
    return _read_quarantine(qpath)


# ── internal helpers ──────────────────────────────────────────────────────────

def _read_quarantine(qpath: Path) -> dict | None:
    try:
        d = json.loads(qpath.read_text(encoding="utf-8"))
        if d.get("state") == "SAFETY_QUARANTINE":
            return d
        return None
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError):
        # FAIL-CLOSED: a quarantine file that exists but is truncated/corrupt
        # (crash mid-write) keeps the project quarantined — an unreadable
        # record must never read as "not in quarantine".
        return {"state": "SAFETY_QUARANTINE", "corrupt": True,
                "reason": "QUARANTINE_FILE_UNREADABLE"}


def _durable_unlink(path: Path) -> None:
    """Remove a file and fsync its parent directory.

    X5: an unlink that is not followed by a directory fsync can be lost in a crash, so
    a project could come back believing it is still quarantined after the clear was
    granted — or, worse, the reverse on a different filesystem.  Deletion of a control
    artefact is as load-bearing as its creation.
    """
    path = Path(path)
    parent = path.parent
    try:
        path.unlink()
    except FileNotFoundError:
        return
    fd = os.open(str(parent), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# ── exceptions ────────────────────────────────────────────────────────────────

class QuarantineActionForbidden(Exception):
    """Action not allowed while in SAFETY_QUARANTINE."""


class QuarantineClearFailed(Exception):
    """SafetyClearReceipt did not satisfy all proofs."""
