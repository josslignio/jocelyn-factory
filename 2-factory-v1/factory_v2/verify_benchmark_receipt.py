#!/usr/bin/env python3
"""Matched benchmark qualification awaits an independent observation producer.

The legacy runner checks structure; it does not execute comparable champion/challenger
arms. A claimant-owned existing file is no stronger than a claimant-owned boolean.
Keep the gate closed until the producer and independent reconstruction are implemented
under CHAMPION_VS_RESHAPED_CHALLENGER, before freezing the experiment's evaluator.
A measured REJECT may complete that future experiment; no verdict alone proves it.
"""
import sys
from pathlib import Path


def verify(receipt_path: Path) -> int:
    print("VERIFIER_NOT_IMPLEMENTED: CHAMPION_VS_RESHAPED_CHALLENGER requires "
          "supervisor-owned paired observations and independent reconstruction; "
          "see factory/campaigns/factory-vnext/tasks/CHAMPION_VS_RESHAPED_CHALLENGER.md",
          file=sys.stderr)
    return 2


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt_path")
    args = parser.parse_args(argv)
    return verify(Path(args.receipt_path))


if __name__ == "__main__":
    raise SystemExit(main())
