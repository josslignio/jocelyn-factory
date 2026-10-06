#!/usr/bin/env python3
"""External M0 self-host authority (P0.1 patch to MASTER_ORDER_FACTORY_V1_FINAL.md).

The M0 candidate's own verify_factory_dod.py cannot certify its own self-host proof — it is
code the candidate is allowed to modify, so a candidate that weakens its own invariant checker
could self-certify. This script is the actual external, locked authority:

  verify_bootstrap_dod.py (this file, locked)  = authority over the M0 self-host proof
  verify_factory_dod.py (candidate's own file) = a component UNDER TEST, never trusted blindly

It is the ONLY code in this repository authorized to write M0_SELF_HOST_PASS.json. Every check
below is a real subprocess/filesystem operation, not a declarative judgment — no LLM verdict of
any kind is consulted (order rules 7/8/P1).
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

BOOTSTRAP_DIR = Path(__file__).resolve().parent
REPO_ROOT = BOOTSTRAP_DIR.parents[1]
sys.path.insert(0, str(REPO_ROOT / "factory" / "vendored"))
import swebench_sentinels as sentinels  # noqa: E402


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sha256_json_stable(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _glob_locked(pattern: str, base: Path) -> list[Path]:
    return sorted(p for p in base.glob(pattern)
                  if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc")


def compute_current_hashes(lock_spec: dict, base: Path = REPO_ROOT) -> dict:
    """Recompute sha256 for every file/glob named in a BOOTSTRAP_LOCK.json's `files` map."""
    hashes = {}
    for rel in lock_spec.get("files", {}):
        p = base / rel
        if p.is_dir() or "*" in rel:
            sub, pattern = (rel.split("/**", 1)[0], "**/*")
            for f in _glob_locked(pattern, base / sub):
                key = str(f.relative_to(base))
                hashes[key] = sha256_file(f)
        elif p.exists():
            hashes[rel] = sha256_file(p)
    return hashes


def verify_bootstrap_lock(lock_path: Path, base: Path = REPO_ROOT) -> tuple[bool, list[str]]:
    """BOOTSTRAP_LOCK_VIOLATION if any locked file's hash changed since the lock was cut.
    `base` defaults to this factory repo's own root (the normal case: M0 hardens the factory
    itself) but is overridable so tests can verify against an isolated synthetic tree."""
    if not lock_path.exists():
        return False, [f"BOOTSTRAP_LOCK_VIOLATION: {lock_path} does not exist"]
    lock = json.loads(lock_path.read_text())
    problems = []
    for rel, expected in lock.get("files", {}).items():
        p = base / rel
        if not p.exists():
            problems.append(f"BOOTSTRAP_LOCK_VIOLATION: locked file missing: {rel}")
            continue
        actual = sha256_file(p)
        if actual != expected:
            problems.append(f"BOOTSTRAP_LOCK_VIOLATION: {rel} hash changed (locked={expected[:12]}.. actual={actual[:12]}..)")
    return (len(problems) == 0), problems


def isolated_env(scratch: Path) -> dict:
    env = dict(os.environ)
    for name, sub in (("HOME", "home"), ("TMPDIR", "tmp"), ("XDG_CACHE_HOME", "cache")):
        d = scratch / sub
        d.mkdir(parents=True, exist_ok=True)
        env[name] = str(d)
    return env


def independent_checkout_and_test(repo: Path, candidate_sha: str, scratch: Path) -> dict:
    """Own disposable checkout, separate from bootstrap_gate.py's — never trusts the builder's
    own report of its test results, re-executes independently."""
    checkout = scratch / "verifier_checkout"
    cp = subprocess.run(["git", "worktree", "add", "--detach", str(checkout), candidate_sha],
                         cwd=repo, capture_output=True, text=True)
    if cp.returncode != 0:
        return {"ok": False, "reason": "REJECT_BAD_SHA: checkout failed", "stderr": cp.stderr}
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=checkout,
                               capture_output=True, text=True).stdout.strip()
        if head != candidate_sha:
            return {"ok": False, "reason": "REJECT_BAD_SHA", "got": head, "expected": candidate_sha}
        # --untracked-files=no: pytest's own .pytest_cache/__pycache__ artifacts are expected
        # run byproducts, not a workspace mutation — this check is about tracked-file integrity
        # (order §9's clean-room requirement), not about test-runner cache litter.
        dirty_before = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=checkout,
                                       capture_output=True, text=True).stdout.strip()
        env = isolated_env(scratch)
        wrapped = subprocess.run(
            ["bash", "-c",
             f'echo "{sentinels.START_TEST_OUTPUT}"; python3 -m pytest tests/ -q; '
             f'echo "{sentinels.END_TEST_OUTPUT}"'],
            cwd=checkout, env=env, capture_output=True, text=True,
        )
        section = sentinels.extract_test_output(wrapped.stdout)
        dod_component = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/test_verify_factory_dod.py", "-q"],
            cwd=checkout, env=env, capture_output=True, text=True,
        )
        dirty_after = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=checkout,
                                      capture_output=True, text=True).stdout.strip()
        return {
            "ok": True,
            "candidate_sha": head,
            "workspace_clean_before": dirty_before == "",
            "workspace_unchanged_after": dirty_after == dirty_before,
            "sentinels_present": section is not None,
            "test_suite_exit_zero": wrapped.returncode == 0,
            "factory_dod_component_exit_zero": dod_component.returncode == 0,
            "raw_stdout_tail": wrapped.stdout[-2000:],
        }
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(checkout)],
                        cwd=repo, capture_output=True, text=True)


def check_no_double_writer(writer_lock_path: Path, stale_after_seconds: int = 240) -> tuple[bool, str]:
    """Mirrors the JOAO writer-lock guardrail: no persistent OS PID across a Claude Code
    session, so liveness is judged by an UPDATED_UTC freshness field, not kill -0."""
    if not writer_lock_path.exists():
        return True, "no writer lock present"
    data = json.loads(writer_lock_path.read_text())
    updated = data.get("updated_utc")
    if not updated:
        return False, "REJECT_DOUBLE_WRITER: lock present with no updated_utc, cannot judge liveness"
    age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(updated.replace("Z", "+00:00"))).total_seconds()
    if age < stale_after_seconds:
        return False, f"REJECT_DOUBLE_WRITER: live writer lock (age={age:.0f}s < {stale_after_seconds}s)"
    return True, f"stale writer lock (age={age:.0f}s), safe to proceed"


def check_receipt_matches_event(receipt: dict, events_jsonl: Path) -> tuple[bool, str]:
    """REJECT a fake receipt (no matching event) or a receipt without a corresponding event."""
    if not isinstance(receipt, dict) or not receipt.get("factory_event_hash"):
        return False, "REJECT_FAKE_RECEIPT: no factory_event_hash on receipt"
    if not events_jsonl.exists():
        return False, "REJECT_RECEIPT_WITHOUT_EVENT: no events.jsonl to check against"
    wanted = receipt["factory_event_hash"]
    for line in events_jsonl.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("event_hash") == wanted:
            return True, "receipt matches a real event"
    return False, f"REJECT_RECEIPT_WITHOUT_EVENT: no event with hash {wanted[:12]}.. found"


def invalidate_old_pass(proof_dir: Path, reason_code: str = "BOOTSTRAP_ROOT_OF_TRUST_CHANGED") -> None:
    """Never deletes prior evidence — marks it INVALIDATED alongside the original."""
    old = proof_dir / "M0_SELF_HOST_PASS.json"
    if not old.exists():
        return
    marker = proof_dir / "M0_SELF_HOST_PASS.INVALIDATED.json"
    payload = {"invalidated_at": utc_now(), "reason_code": reason_code,
               "original_receipt": json.loads(old.read_text())}
    marker.write_text(json.dumps(payload, indent=2))


def write_pass_receipt(proof_dir: Path, payload: dict) -> Path:
    """The ONLY call site in this repository that writes M0_SELF_HOST_PASS.json."""
    proof_dir.mkdir(parents=True, exist_ok=True)
    out = proof_dir / "M0_SELF_HOST_PASS.json"
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    os.replace(tmp, out)
    return out


def run(repo: Path, candidate_sha: str, proof_dir: Path, lock_path: Path,
        writer_lock_path: Path | None = None, events_jsonl: Path | None = None,
        receipt_to_check: dict | None = None, lock_base: Path = REPO_ROOT) -> int:
    notes = []
    lock_ok, lock_problems = verify_bootstrap_lock(lock_path, base=lock_base)
    notes += lock_problems
    if not lock_ok:
        print("BOOTSTRAP_LOCK_VALID=false")
        for p in lock_problems:
            print(p)
        return 1

    dw_ok, dw_note = (True, "not checked") if writer_lock_path is None else check_no_double_writer(writer_lock_path)
    notes.append(dw_note)

    receipt_ok = True
    if receipt_to_check is not None and events_jsonl is not None:
        receipt_ok, note = check_receipt_matches_event(receipt_to_check, events_jsonl)
        notes.append(note)

    with tempfile.TemporaryDirectory(prefix="verify_bootstrap_dod_") as tmp:
        scratch = Path(tmp)
        result = independent_checkout_and_test(repo, candidate_sha, scratch)

    checks = {
        "bootstrap_lock_valid": lock_ok,
        "no_double_writer_detected": dw_ok,
        "candidate_checkout_sha_matches": result.get("candidate_sha") == candidate_sha,
        "candidate_workspace_clean_before": result.get("workspace_clean_before", False),
        "candidate_workspace_unchanged_after": result.get("workspace_unchanged_after", False),
        "candidate_test_suite_sentinels_present": result.get("sentinels_present", False),
        "candidate_test_suite_exit_zero": result.get("test_suite_exit_zero", False),
        "candidate_factory_dod_tested_as_component": result.get("factory_dod_component_exit_zero", False),
        "receipt_matches_event": receipt_ok,
    }
    all_pass = result.get("ok", False) and all(checks.values())

    verifier_sha = sha256_file(Path(__file__))
    lock_sha = sha256_file(lock_path)
    payload = {
        "schema": "factory-bootstrap-receipt/v1",
        "candidate_sha": candidate_sha,
        "bootstrap_lock_sha256": lock_sha,
        "verifier_sha256": verifier_sha,
        "checks": checks,
        "all_pass": all_pass,
        "verified_at": utc_now(),
        "verifier": "verify_bootstrap_dod.py",
        "notes": notes,
    }
    print(json.dumps(payload, indent=2))
    if all_pass:
        out = write_pass_receipt(proof_dir, payload)
        print(f"M0_SELF_HOST_PASS written to {out}")
        return 0
    print("VERIFY_BOOTSTRAP_DOD_EXIT_CODE=1 (see checks above) — no pass receipt written")
    return 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--candidate-sha", required=True)
    ap.add_argument("--proof-dir", required=True, help="External, outside the candidate's own repo tree")
    ap.add_argument("--lock", default=str(BOOTSTRAP_DIR / "BOOTSTRAP_LOCK.json"))
    ap.add_argument("--writer-lock")
    ap.add_argument("--events-jsonl")
    ap.add_argument("--receipt-json", help="Path to a receipt to cross-check against events.jsonl")
    args = ap.parse_args()
    receipt = json.loads(Path(args.receipt_json).read_text()) if args.receipt_json else None
    return run(
        Path(args.repo).resolve(), args.candidate_sha, Path(args.proof_dir),
        Path(args.lock), Path(args.writer_lock) if args.writer_lock else None,
        Path(args.events_jsonl) if args.events_jsonl else None, receipt,
    )


if __name__ == "__main__":
    raise SystemExit(main())
