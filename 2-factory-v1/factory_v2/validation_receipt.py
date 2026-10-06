"""Phase 2: Immutable ValidationReceipt — bound to staging_sha + contract_sha.

A ValidationReceipt is NOT the same as a unit-test pass or an authored boolean.
It is computed from observed subprocess output (product_e2e test_command exit code)
in a detached worktree at an exact SHA.

Schema validation-receipt/v1:
    schema: "validation-receipt/v1"
    produced_at: ISO8601 UTC
    staging_sha: str          -- exact SHA the worktree was pinned to
    contract_id: str          -- frozen contract identifier
    contract_sha: str         -- sha256[:16] of frozen contract JSON
    milestone: str
    product_e2e_pass: bool    -- derived from subprocess exit code (NOT authored)
    product_e2e_command: str  -- the exact command executed
    product_e2e_exit_code: int  -- raw subprocess exit code
    artifacts_present: bool   -- all required_artifacts verified on disk
    missing_artifacts: list[str]
    receipt_sha: str          -- sha256 of (staging_sha+contract_sha+exit_code+produced_at)
    valid: bool               -- all checks passed

Settlement requires a valid ValidationReceipt:
    - valid == True
    - staging_sha == promoted_sha (checked by golden_promotion.py)
    - receipt_sha verifiable (not authored)
    - produced_at within TTL (prevents reuse of stale receipts)
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from factory_v2._io import _now

SCHEMA = "validation-receipt/v1"
RECEIPT_TTL_SECONDS = 3600  # receipts older than 1h are stale


def _sha16(data: str) -> str:
    return hashlib.sha256(data.encode()).hexdigest()[:16]


def _compute_receipt_sha(staging_sha: str, contract_sha: str,
                         exit_code: int, produced_at: str,
                         *, receipt: "dict | None" = None) -> str:
    payload = f"{staging_sha}:{contract_sha}:{exit_code}:{produced_at}"
    if receipt is not None:
        # L5_rest_2-014: the seal must cover the semantic booleans and the
        # artifact-presence results — a receipt whose `valid` flag can be
        # flipped without breaking the hash is not sealed at all.
        payload += ":" + json.dumps(
            {"p": bool(receipt.get("product_e2e_pass")),
             "a": bool(receipt.get("artifacts_present")),
             "m": list(receipt.get("missing_artifacts") or []),
             "v": bool(receipt.get("valid"))},
            sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


class ValidationReceipt:
    """Immutable record of a validation outcome at an exact staging SHA."""

    def __init__(self, data: dict):
        self._data = data

    @property
    def valid(self) -> bool:
        return bool(self._data.get("valid"))

    @property
    def staging_sha(self) -> str:
        return self._data.get("staging_sha", "")

    @property
    def contract_id(self) -> str:
        return self._data.get("contract_id", "")

    @property
    def milestone(self) -> str:
        return self._data.get("milestone", "")

    @property
    def receipt_sha(self) -> str:
        return self._data.get("receipt_sha", "")

    def is_stale(self, ttl_seconds: int = RECEIPT_TTL_SECONDS) -> bool:
        produced_at = self._data.get("produced_at", "")
        if not produced_at:
            return True
        try:
            import datetime
            ts = datetime.datetime.fromisoformat(produced_at.replace("Z", "+00:00"))
            now = datetime.datetime.now(datetime.timezone.utc)
            return (now - ts).total_seconds() > ttl_seconds
        except Exception:
            return True

    def verify_receipt_sha(self) -> bool:
        """Recompute receipt_sha from fields — detects authored booleans."""
        expected = _compute_receipt_sha(
            self._data.get("staging_sha", ""),
            self._data.get("contract_sha", ""),
            self._data.get("product_e2e_exit_code", -1),
            self._data.get("produced_at", ""),
            receipt=self._data,
        )
        return expected == self._data.get("receipt_sha", "")

    def as_dict(self) -> dict:
        return dict(self._data)

    def write(self, path: Path) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: Path) -> "ValidationReceipt":
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema") != SCHEMA:
            raise ValueError(f"Invalid schema: {data.get('schema')!r}")
        return cls(data)


def produce_validation_receipt(
    staging_sha: str,
    contract: dict,
    worktree_dir: Path,
    timeout_s: int = 300,
) -> ValidationReceipt:
    """Run product_e2e in worktree_dir at staging_sha and produce an immutable receipt.

    The receipt_sha is derived from (staging_sha, contract_sha, exit_code, produced_at)
    so it cannot be authored without knowing the exact exit code from the subprocess.
    """
    body = contract.get("body", contract)
    milestone = body.get("milestone", "UNKNOWN")
    contract_id = contract.get("contract_id", "UNKNOWN")
    contract_sha = _sha16(json.dumps(contract, sort_keys=True))

    test_cmd = (body.get("product_e2e") or {}).get("test_command", "")
    required_artifacts = body.get("required_artifacts") or []

    # L5_rest_2-015: OBSERVE the worktree's HEAD before attesting anything.
    # The producer may not copy a claimed staging_sha that HEAD contradicts —
    # attesting SHA A after testing checkout B is exactly the detached-proof
    # defect. A worktree outside any git repo cannot be observed at all; the
    # observation is recorded so the gate can refuse it when it matters.
    head_observed = ""
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"],
                           cwd=str(worktree_dir), capture_output=True,
                           text=True, timeout=30)
        if r.returncode == 0:
            head_observed = r.stdout.strip()
    except Exception:
        head_observed = ""
    staging_mismatch = bool(head_observed) and head_observed != staging_sha

    # Run product_e2e test_command in worktree — real subprocess, no mock.
    exit_code = -1
    e2e_output = ""
    if test_cmd:
        try:
            from .mission_evidence import control_repo_env
            proc = subprocess.run(
                ["bash", "-c", f"set -eo pipefail\n{test_cmd}"],
                cwd=worktree_dir, capture_output=True, text=True, timeout=timeout_s,
                env=control_repo_env(),
            )
            exit_code = proc.returncode
            e2e_output = (proc.stdout + proc.stderr)[-4000:]
        except subprocess.TimeoutExpired:
            exit_code = 124
            e2e_output = f"TIMEOUT after {timeout_s}s"
        except Exception as exc:
            exit_code = -1
            e2e_output = f"SUBPROCESS_ERROR: {exc}"
    else:
        exit_code = 1
        e2e_output = "no product_e2e test_command in contract"

    product_e2e_pass = exit_code == 0

    # Check artifacts in worktree.
    missing_artifacts: list[str] = []
    for artifact in required_artifacts:
        if not (worktree_dir / artifact).exists():
            missing_artifacts.append(str(artifact))
    artifacts_present = len(missing_artifacts) == 0

    valid = product_e2e_pass and artifacts_present and not staging_mismatch
    if staging_mismatch:
        e2e_output = (f"STAGING_SHA_MISMATCH: worktree HEAD is {head_observed}, "
                      f"receipt is asked to attest {staging_sha}\n" + e2e_output)

    produced_at = _now()
    data = {
        "schema": SCHEMA,
        "produced_at": produced_at,
        "staging_sha": staging_sha,
        "head_sha_observed": head_observed,
        "contract_id": contract_id,
        "contract_sha": contract_sha,
        "milestone": milestone,
        "product_e2e_pass": product_e2e_pass,
        "product_e2e_command": test_cmd,
        "product_e2e_exit_code": exit_code,
        "product_e2e_output_tail": e2e_output,
        "artifacts_present": artifacts_present,
        "missing_artifacts": missing_artifacts,
        "valid": valid,
    }
    data["receipt_sha"] = _compute_receipt_sha(
        staging_sha, contract_sha, exit_code, produced_at, receipt=data)
    return ValidationReceipt(data)


def assert_receipt_valid_for_promotion(receipt: ValidationReceipt, staging_sha: str) -> None:
    """Raise if the receipt cannot authorize a Golden promotion.

    Check order:
    1. Staleness (TTL) — fast structural check first
    2. SHA binding — receipt bound to the right commit
    3. receipt_sha integrity — detects authored booleans
    4. valid flag — subprocess result
    """
    if receipt.is_stale():
        raise ValidationReceiptInvalid(
            f"ValidationReceipt is stale (>{RECEIPT_TTL_SECONDS}s). "
            "Re-run validation before promoting."
        )
    if receipt.staging_sha != staging_sha:
        raise ValidationReceiptInvalid(
            f"Receipt staging_sha={receipt.staging_sha!r} != staging_sha={staging_sha!r}. "
            "Receipt is bound to a different commit — settlement blocked."
        )
    if not receipt.verify_receipt_sha():
        raise ValidationReceiptInvalid(
            "receipt_sha verification failed — receipt may have been authored. "
            "Settlement blocked."
        )
    # L5_rest_2-014 (round 2): the booleans are RECOMPUTED, never trusted.
    # A sealed hash over an authored boolean only catches post-hoc edits; the
    # gate itself must refuse a receipt whose declarative fields disagree with
    # the observed exit code and artifact presence it also carries.
    data = receipt.as_dict()
    exit_code = data.get("product_e2e_exit_code", -1)
    if bool(data.get("product_e2e_pass")) != (exit_code == 0):
        raise ValidationReceiptInvalid(
            f"Receipt product_e2e_pass={data.get('product_e2e_pass')} contradicts "
            f"product_e2e_exit_code={exit_code}. Settlement blocked."
        )
    missing = data.get("missing_artifacts") or []
    if bool(data.get("artifacts_present")) != (len(missing) == 0):
        raise ValidationReceiptInvalid(
            f"Receipt artifacts_present={data.get('artifacts_present')} contradicts "
            f"missing_artifacts={missing}. Settlement blocked."
        )
    # L5_rest_2-015: when the producer observed the worktree HEAD, that
    # observation must agree with the SHA the receipt attests.
    head_observed = str(data.get("head_sha_observed") or "")
    if head_observed and head_observed != staging_sha:
        raise ValidationReceiptInvalid(
            f"Receipt attests staging_sha={staging_sha!r} but was produced in a "
            f"worktree whose observed HEAD was {head_observed!r}. Settlement blocked."
        )
    if not receipt.valid:
        raise ValidationReceiptInvalid(
            f"Receipt valid=False (exit_code={receipt._data.get('product_e2e_exit_code')}, "
            f"missing_artifacts={receipt._data.get('missing_artifacts')}). "
            "Settlement blocked."
        )


class ValidationReceiptInvalid(Exception):
    """Receipt cannot authorize Golden promotion."""
