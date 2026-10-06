#!/usr/bin/env python3
"""Thin CLI: check BOOTSTRAP_LOCK.json integrity alone, without running the full self-host
verification. Used by bootstrap_factory_v1.sh as an early fail-fast step."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_bootstrap_dod import verify_bootstrap_lock  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lock", required=True)
    args = ap.parse_args()
    ok, problems = verify_bootstrap_lock(Path(args.lock))
    print(f"BOOTSTRAP_LOCK_VALID={ok}")
    for p in problems:
        print(p)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
