#!/usr/bin/env python3
"""Validate cycle2_weekly_refresh.log — real consecutive refreshes, no human gate.
Usage: verify_cycle2_refresh.py <path> [--min-refreshes N] [--verify-no-human-gate]
Schema locked at AUTONOMOUS_CTO_BASELINE_V1.
"""
import argparse, json, sys
from pathlib import Path


def fail(msg):
    print(f"FAIL: {msg}", file=sys.stderr); return 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--min-refreshes", type=int, default=2)
    ap.add_argument("--verify-no-human-gate", action="store_true")
    args = ap.parse_args()

    path = Path(args.path)
    if not path.exists():
        return fail(f"not found: {path}")

    entries = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except Exception:
            return fail(f"non-JSON line in log: {line[:80]}")

    refreshes = [e for e in entries if e.get("event") == "REFRESH_COMPLETE"]
    if len(refreshes) < args.min_refreshes:
        return fail(f"only {len(refreshes)} REFRESH_COMPLETE events — need ≥{args.min_refreshes}")

    required_fields = {"timestamp", "accounts_refreshed", "new_calls", "report_sha", "date_report"}
    for ref in refreshes:
        missing = required_fields - set(ref.keys())
        if missing:
            return fail(f"refresh entry missing fields: {missing}")

    date_reports = [r["date_report"] for r in refreshes]
    if len(set(date_reports)) < len(date_reports):
        return fail(f"duplicate date_report values: {date_reports} — refreshes not distinct")

    report_shas = [r["report_sha"] for r in refreshes]
    if len(set(report_shas)) < len(report_shas):
        return fail(f"duplicate report_sha — report did not change between refreshes")

    # L5_factory_v2_rest_2-024: the count is ALWAYS recomputed from the journal
    # — the printed qualification message must state an observation, never an
    # unverified HUMAN_GO=0. The flag only decides pass/fail, not whether we
    # look.
    human_gates = [e for e in entries if e.get("event") in ("HUMAN_GO", "HUMAN_GATE", "WAITING_HUMAN")]
    if args.verify_no_human_gate and human_gates:
        return fail(f"{len(human_gates)} HUMAN_GO event(s) found — routine human gate violated: {human_gates[0]}")

    print(f"PASS: {len(refreshes)} refreshes, all distinct report_shas, HUMAN_GO={len(human_gates)} (recounted)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
