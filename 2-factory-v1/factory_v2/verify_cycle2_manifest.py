#!/usr/bin/env python3
"""Verify cycle2_universe_manifest.json for CYCLE_2_INGESTION_ELASTIQUE.

Exit 0 = acceptance criteria met.
Exit 1 = criteria not met.

Usage:
    python3 factory_v2/verify_cycle2_manifest.py research/cycle2_universe_manifest.json \\
        --min-accounts 20 --min-calls-per-account 30
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # standalone: make factory_v2 importable


def verify(manifest_path: Path, min_accounts: int, min_calls: int) -> int:
    if not manifest_path.is_file():
        print(f"FAIL: manifest not found at {manifest_path}", file=sys.stderr)
        return 1

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"FAIL: cannot parse manifest: {exc}", file=sys.stderr)
        return 1

    errors = []

    accounts = manifest.get("accounts", [])
    call_counts = manifest.get("call_counts", {})
    corpus_sha = manifest.get("corpus_sha", "")

    # L5_factory_v2_rest_2-023: thresholds are about DISTINCT accounts — twenty
    # occurrences of the same handle are one account. Empty handles count for
    # nothing. (shared primitive: factory_v2.verify_lib.distinct_or_fail)
    from factory_v2.verify_lib import VerificationRefused, distinct_or_fail
    handles = [acct if isinstance(acct, str) else acct.get("handle", "")
               for acct in accounts]
    non_empty = [h for h in handles if h]
    distinct = list(dict.fromkeys(non_empty))
    try:
        distinct_or_fail(non_empty, what="accounts", minimum=min_accounts)
    except VerificationRefused as refused:
        errors.append(str(refused))
        errors.append(
            f"duplicates: {sorted({h for h in non_empty if non_empty.count(h) > 1})[:5]}")
    if len(distinct) != len(non_empty):
        errors.append("duplicate handles in accounts — an account listed twice is not two accounts")

    qualifying = 0
    for handle in distinct:
        count = call_counts.get(handle, 0)
        if count >= min_calls:
            qualifying += 1

    if qualifying < min_accounts:
        errors.append(f"only {qualifying}/{len(distinct)} distinct accounts have ≥{min_calls} calls — need ≥{min_accounts}")

    if not corpus_sha:
        errors.append("corpus_sha is missing — cannot verify corpus integrity")

    if errors:
        print(f"CYCLE2_MANIFEST FAIL ({len(errors)} errors):", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    print(f"CYCLE2_MANIFEST PASS: {qualifying} accounts with ≥{min_calls} calls, corpus_sha={corpus_sha[:12]}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="verify_cycle2_manifest")
    parser.add_argument("manifest_path")
    parser.add_argument("--min-accounts", type=int, default=20)
    parser.add_argument("--min-calls-per-account", type=int, default=30)
    args = parser.parse_args(argv)
    return verify(Path(args.manifest_path), args.min_accounts, args.min_calls_per_account)


if __name__ == "__main__":
    raise SystemExit(main())
