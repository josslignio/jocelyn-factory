"""Phase 2: Golden promotion with real git CAS — git update-ref <ref> <new> <expected-old>.

Flow (validate-before-golden):
    base_sha B (current golden ref tip — read before any write)
    → candidate_sha C (branch tip, pre-merge)
    → staging_sha S (post-merge, WITHOUT changing golden ref)
    → detached worktree at exact S
    → produce ValidationReceipt (real subprocess, not authored)
    → assert_receipt_valid_for_promotion(receipt, S)
    → git update-ref <golden_ref> S B    ← REAL GIT CAS
        If B moved since we read it: CAS_CONFLICT — golden unchanged, recovery handoff written
    → PromotionReceipt bound to promoted S
    → settlement allowed only at promoted SHA

Invariants:
    GOLDEN_UNCHANGED_ON_VALIDATOR_FAIL = 0  (Golden ref never touched before receipt.valid)
    GOLDEN_UNCHANGED_ON_CRASH = 0           (try/finally ensures worktree cleanup)
    CONCURRENT_PROMOTIONS_EXACTLY_ONE_WINS  (git update-ref atomic compare-swap)
    PROMOTED_SHA_EQUALS_VALIDATED_SHA = 0   (receipt.staging_sha == promoted_sha enforced)
    STALE_RECEIPT_BLOCKS_SETTLEMENT = 0     (TTL check in assert_receipt_valid_for_promotion)
    EXTERNAL_CANDIDATE_RECOVERABLE = 0      (candidate branch preserved on CAS conflict)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .disk_guard import ensure_free_gb  # FACTORY_REPO_HYGIENE_V1: DISK_SPACE_LOW before each worktree add
from .validation_receipt import (
    ValidationReceipt,
    ValidationReceiptInvalid,
    assert_receipt_valid_for_promotion,
    produce_validation_receipt,
)
from factory_v2._io import _now

_REPO_ROOT = Path(__file__).parents[1]


@dataclass
class PromotionReceipt:
    """Immutable record of a successful CAS promotion."""
    schema: str = "promotion-receipt/v1"
    produced_at: str = ""
    golden_ref: str = ""
    base_sha: str = ""          # SHA golden was at when we read it
    staging_sha: str = ""       # SHA we validated and promoted
    promoted_sha: str = ""      # SHA golden ref now points to (== staging_sha)
    contract_id: str = ""
    milestone: str = ""
    validation_receipt_sha: str = ""
    status: Literal["PROMOTED", "CAS_CONFLICT", "VALIDATION_FAILED", "ERROR"] = "ERROR"
    failure_reason: str = ""

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}

    def write(self, path: Path) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.as_dict(), indent=2), encoding="utf-8")
        os.replace(tmp, path)



class GoldenPromotion:
    """Orchestrates validate-before-golden promotion with real git CAS."""

    def __init__(
        self,
        repo_dir: Path,
        golden_ref: str = "refs/heads/main",
        timeout_s: int = 300,
    ):
        self.repo_dir = Path(repo_dir)
        self.golden_ref = golden_ref
        self.timeout_s = timeout_s

    # ── public API ────────────────────────────────────────────────────────────

    def read_golden_sha(self) -> str:
        """Read current tip of golden ref (base SHA B)."""
        r = subprocess.run(
            ["git", "rev-parse", self.golden_ref],
            cwd=self.repo_dir, capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            raise RuntimeError(
                f"Cannot read golden ref {self.golden_ref!r}: {r.stderr.strip()}")
        return r.stdout.strip()

    def create_staging_commit(
        self,
        candidate_branch: str,
        base_sha: str,
        message: str = "staging integration",
    ) -> str:
        """Merge candidate_branch into a staging commit WITHOUT touching HEAD, index, or working tree.

        Creates a temporary DETACHED WORKTREE at base_sha, merges candidate_branch there,
        returns the resulting commit SHA. The main working tree HEAD is never touched.

        Counter-audit fix 2026-09-08: the previous implementation ran `git merge` in the
        main working tree, moving HEAD (HEAD_BEFORE ≠ HEAD_AFTER). This version uses
        git worktree add --detach so the merge happens in an isolated directory.
        """
        ensure_free_gb(tempfile.gettempdir())   # raises DiskSpaceLow (typed DISK_SPACE_LOW) before anything is created
        wt_dir = Path(tempfile.mkdtemp(prefix="factory-staging-"))
        try:
            # Add a detached worktree at base_sha — main working tree HEAD unchanged.
            r = subprocess.run(
                ["git", "worktree", "add", "--detach", str(wt_dir), base_sha],
                cwd=self.repo_dir, capture_output=True, text=True, check=True, timeout=60,
            )
            # Verify the worktree is at base_sha before merging.
            actual = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=wt_dir, capture_output=True, text=True, timeout=10,
            ).stdout.strip()
            if actual != base_sha:
                raise RuntimeError(
                    f"staging worktree SHA mismatch: expected {base_sha!r} got {actual!r}")
            # Merge candidate into the detached worktree (HEAD there = base_sha).
            r = subprocess.run(
                ["git", "merge", "--no-ff", "-m", message, candidate_branch],
                cwd=wt_dir, capture_output=True, text=True, timeout=120,
            )
            if r.returncode != 0:
                raise RuntimeError(f"staging merge failed: {r.stderr.strip()}")
            # Read the staging SHA from the worktree's HEAD (not from any ref).
            sha_r = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=wt_dir, capture_output=True, text=True, check=True, timeout=10,
            )
            return sha_r.stdout.strip()
        finally:
            # Remove worktree — main working tree HEAD was NEVER touched.
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(wt_dir)],
                cwd=self.repo_dir, capture_output=True, timeout=30,
            )
            shutil.rmtree(wt_dir, ignore_errors=True)

    def create_detached_worktree(self, sha: str) -> Path:
        """Create an isolated detached worktree at exact sha."""
        ensure_free_gb(tempfile.gettempdir())   # raises DiskSpaceLow (typed DISK_SPACE_LOW) before anything is created
        wt = Path(tempfile.mkdtemp(prefix="factory-golden-"))
        try:
            r = subprocess.run(
                ["git", "worktree", "add", "--detach", str(wt), sha],
                cwd=self.repo_dir, capture_output=True, text=True, timeout=60,
            )
            if r.returncode != 0:
                raise RuntimeError(f"git worktree add failed: {r.stderr.strip()}")
            actual = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=wt, capture_output=True, text=True, timeout=10,
            ).stdout.strip()
            if actual != sha:
                raise RuntimeError(f"Worktree SHA mismatch: expected {sha!r} got {actual!r}")
            return wt
        except Exception:
            shutil.rmtree(wt, ignore_errors=True)
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(wt)],
                cwd=self.repo_dir, capture_output=True, timeout=10,
            )
            raise

    def remove_worktree(self, wt: Path) -> None:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(wt)],
            cwd=self.repo_dir, capture_output=True, timeout=10,
        )
        shutil.rmtree(wt, ignore_errors=True)

    def git_cas_update_ref(
        self, staging_sha: str, expected_old_sha: str
    ) -> tuple[bool, str]:
        """Atomic git update-ref — real CAS with expected-old-sha.

        git update-ref <golden_ref> <staging_sha> <expected_old_sha>
        Returns (success, message).
        Fails atomically if current golden tip != expected_old_sha.
        """
        r = subprocess.run(
            ["git", "update-ref", self.golden_ref, staging_sha, expected_old_sha],
            cwd=self.repo_dir, capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            return True, f"Golden promoted: {expected_old_sha[:12]}→{staging_sha[:12]}"
        return False, f"CAS_CONFLICT: {r.stderr.strip() or 'golden ref moved concurrently'}"

    def promote(
        self,
        staging_sha: str,
        base_sha: str,
        contract: dict,
        evidence_dir: Path,
        candidate_branch: str | None = None,
    ) -> PromotionReceipt:
        """Full validate-before-golden promotion flow.

        1. Create detached worktree at staging_sha.
        2. Run product_e2e → ValidationReceipt.
        3. Assert receipt valid (fail closed — Golden never touched on failure).
        4. git update-ref (real CAS).
        5. Write PromotionReceipt.

        Golden is NEVER touched before step 4, and step 4 is atomic.
        """
        body = contract.get("body", contract)
        milestone = body.get("milestone", "UNKNOWN")
        contract_id = contract.get("contract_id", "UNKNOWN")
        produced_at = _now()

        wt: Path | None = None
        receipt: PromotionReceipt = PromotionReceipt(
            produced_at=produced_at,
            golden_ref=self.golden_ref,
            base_sha=base_sha,
            staging_sha=staging_sha,
            contract_id=contract_id,
            milestone=milestone,
        )

        try:
            # Step 1: isolated worktree at exact staging_sha.
            wt = self.create_detached_worktree(staging_sha)

            # Step 2: produce ValidationReceipt from real subprocess.
            val_receipt = produce_validation_receipt(staging_sha, contract, wt, self.timeout_s)
            receipt.validation_receipt_sha = val_receipt.receipt_sha

            # Persist ValidationReceipt before any Golden mutation.
            evidence_dir.mkdir(parents=True, exist_ok=True)
            val_receipt_path = evidence_dir / f"validation_receipt_{staging_sha[:12]}.json"
            val_receipt.write(val_receipt_path)

            # Step 3: fail closed — Golden never touched if validation fails.
            assert_receipt_valid_for_promotion(val_receipt, staging_sha)

            # Step 4: git update-ref (real CAS).
            ok, msg = self.git_cas_update_ref(staging_sha, base_sha)
            if not ok:
                # Golden unchanged — write CAS conflict recovery info.
                actual_sha = self.read_golden_sha()
                receipt.status = "CAS_CONFLICT"
                receipt.failure_reason = msg
                # Preserve candidate branch (EXTERNAL_CANDIDATE_RECOVERABLE).
                # The staging_sha commit remains reachable; candidate_branch untouched.
                receipt.write(evidence_dir / f"promotion_receipt_{staging_sha[:12]}.json")
                return receipt

            # Step 5: promoted.
            # L4_resilience_review-002: by this point the golden ref HAS moved.
            # A receipt-write failure here is NOT a promotion failure — the old
            # code let it fall into the generic `except` below, wrote an ERROR
            # receipt and returned ERROR while golden was already at
            # staging_sha (settlement then read PROMOTION_FAILED for a real
            # promotion). Write best-effort with one retry; never downgrade
            # the status.
            receipt.status = "PROMOTED"
            receipt.promoted_sha = staging_sha
            for _attempt in (1, 2):
                try:
                    receipt.write(evidence_dir / f"promotion_receipt_{staging_sha[:12]}.json")
                    break
                except Exception:
                    if _attempt == 2:
                        # Status stays PROMOTED: the ref state is the truth, the
                        # caller holds the receipt in memory either way.
                        pass
            return receipt

        except ValidationReceiptInvalid as exc:
            receipt.status = "VALIDATION_FAILED"
            receipt.failure_reason = str(exc)
            evidence_dir.mkdir(parents=True, exist_ok=True)
            receipt.write(evidence_dir / f"promotion_receipt_{staging_sha[:12]}.json")
            return receipt

        except Exception as exc:
            receipt.status = "ERROR"
            receipt.failure_reason = str(exc)
            evidence_dir.mkdir(parents=True, exist_ok=True)
            receipt.write(evidence_dir / f"promotion_receipt_{staging_sha[:12]}.json")
            return receipt

        finally:
            # Golden never touched in cleanup — only worktree removed.
            if wt is not None:
                self.remove_worktree(wt)


