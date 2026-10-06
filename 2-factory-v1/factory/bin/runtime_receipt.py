#!/usr/bin/env python3
"""Generic factory-runtime-receipt/v1 validator (order §8 / forensic gap 2).

factoryctl.py's own `validate_runtime_receipt()` is Job-Radar-specific (schema name
"job-radar-runtime-receipt/v1", hardcoded product fields like cv_personalization_pass) and is
left untouched — it is a real, tested, currently-relied-upon validator for the existing
JR-LIVE-RECOVERY work (zero regression rule). This module is the NEW generic validator any
product (CSV Statistics, Equipment Tracker, Notes API, Reminder Scheduler, ICS Parser, and
eventually Job Radar itself) can use, implementing the anti-spoof field set and rejection list
from order §8 exactly, with no product-specific field ever hardcoded here.
"""
from __future__ import annotations

import json
from pathlib import Path

SCHEMA = "factory-runtime-receipt/v1"

REQUIRED_FIELDS = (
    "schema", "campaign_id", "project_id", "mission_id", "capability_id", "attempt_id",
    "run_nonce", "idempotency_key", "role", "provider", "candidate_sha", "oracle_sha256",
    "acceptance_lock_sha256", "network_policy_sha256", "toolchain_lock_sha256",
    "dependency_lock_sha256", "workspace_tree_before", "workspace_tree_after", "command",
    "started_at", "finished_at", "exit_code", "sentinels_valid", "stdout_sha256",
    "stderr_sha256", "artifacts_manifest_sha256", "factory_event_hash",
)


def _event_hash_exists(event_hash: str, events_jsonl: Path) -> bool:
    if not events_jsonl.exists():
        return False
    for line in events_jsonl.read_text().splitlines():
        if not line.strip():
            continue
        if json.loads(line).get("event_hash") == event_hash:
            return True
    return False


def collect_consumed_nonces(events_jsonl: Path) -> set:
    """Fault-injection F021 fix (order §7.3/§8, "nonce rejoué"): `validate()`'s own
    REJECT_REPLAYED_NONCE check (below) can only ever fire against whatever `consumed_nonces` set
    its caller supplies — and until this function existed, no caller had any way to derive that
    set from real persisted history, so every real integration of this module (as documented)
    passed a fresh, history-blind set and replay protection was a permanent no-op. This scans the
    same `events_jsonl` `_event_hash_exists` already reads, collecting every `run_nonce` a caller
    has recorded on an acceptance event — the only durable trace this module has of "already
    consumed"."""
    consumed = set()
    if not events_jsonl.exists():
        return consumed
    for line in events_jsonl.read_text().splitlines():
        if not line.strip():
            continue
        nonce = json.loads(line).get("run_nonce")
        if nonce:
            consumed.add(nonce)
    return consumed


def validate(receipt: dict, *, expected: dict, consumed_nonces: set,
             events_jsonl: Path) -> list[str]:
    """`expected` carries the fields this receipt MUST match: campaign_id, project_id,
    mission_id, attempt_id, candidate_sha, oracle_sha256, acceptance_lock_sha256,
    network_policy_sha256, toolchain_lock_sha256. `consumed_nonces` is the set of run_nonce
    values already used by a prior accepted receipt (order §7.3/§8: reject a replayed nonce)."""
    problems = []
    if not isinstance(receipt, dict):
        return ["INVALID_REVIEW: receipt is not a dict"]

    missing = [f for f in REQUIRED_FIELDS if f not in receipt]
    if missing:
        problems.append(f"REJECT_MALFORMED_RECEIPT: missing fields {missing}")
        return problems  # nothing else is safe to check on a malformed receipt

    if receipt["schema"] != SCHEMA:
        problems.append(f"REJECT_MALFORMED_RECEIPT: schema {receipt['schema']!r} != {SCHEMA!r}")

    # L6_bin_lib_products_1-019: capability_id is a REQUIRED field (REQUIRED_FIELDS)
    # but was never compared to expected — a receipt for capability OTHER passed
    # while the caller expected EXPECTED, same mission.
    for key in ("campaign_id", "project_id", "mission_id", "capability_id",
                "attempt_id", "candidate_sha",
                "oracle_sha256", "acceptance_lock_sha256", "network_policy_sha256",
                "toolchain_lock_sha256"):
        if key in expected and receipt.get(key) != expected[key]:
            problems.append(f"REJECT_CROSS_SCOPE: {key} {receipt.get(key)!r} != expected {expected[key]!r}")

    if receipt["run_nonce"] in consumed_nonces:
        problems.append(f"REJECT_REPLAYED_NONCE: run_nonce {receipt['run_nonce']!r} already consumed")

    if not _event_hash_exists(receipt["factory_event_hash"], events_jsonl):
        problems.append("REJECT_RECEIPT_WITHOUT_EVENT: factory_event_hash has no matching event")

    if receipt["exit_code"] != 0:
        problems.append(f"exit_code={receipt['exit_code']!r} != 0")

    if not receipt["sentinels_valid"]:
        problems.append("sentinels_valid=False — oracle log could not be verified as real")

    if receipt["role"] == "reviewer" and receipt["workspace_tree_before"] != receipt["workspace_tree_after"]:
        problems.append("REJECT_REVIEWER_WRITE_VIOLATION: reviewer's workspace_tree changed during the run")

    return problems
