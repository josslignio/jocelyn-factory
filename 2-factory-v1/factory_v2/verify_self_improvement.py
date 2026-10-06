#!/usr/bin/env python3
"""SAFE_SELF_IMPROVEMENT gate — CHALLENGER_AUTOMATION_RECEIPT.json.

DEFERRED — CTO second review, 2026-09-11.

This verifier had no independent observation producer. Every input it could receive was
authored by the party being graded, so it accepted five booleans, an empty stats block, or
four skipped probes and returned 0. The first repair (requiring an `evidence` key) moved the
same trust defect one field: a claimant can write `evidence: "invented"` as easily as
`challenger_created: true`.

There are exactly two honest outcomes, and "pass" is not among them:

  * recompute the outcome from raw immutable inputs a supervisor observed — no such producer
    exists for SAFE_SELF_IMPROVEMENT;
  * refuse, name the producer that is owed, and let the milestone stay unsettled.

So this exits 2 (VERIFIER_NOT_IMPLEMENTED): the milestone fails closed. Rejecting every input
does NOT implement the capability either — that is why the concrete producer task is named
below rather than left implicit. Do not replace this with a shape check to obtain a settlement.
"""
import json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from factory_v2.evidence_grading import NOT_IMPLEMENTED, not_implemented  # noqa: E402

MILESTONE = "SAFE_SELF_IMPROVEMENT"
PRODUCER_TASK = "factory/campaigns/factory-vnext/tasks/SAFE_SELF_IMPROVEMENT.md (build the challenger-automation producer that records a real champion-vs-challenger comparison; a measured REJECT preserving the champion is a completed experiment and must settle)"
MISSING = "a supervisor-observed record of challenger creation, of the benchmark that compared it, and of the champion's immutability during it"


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    code, message = not_implemented(
        f"{MILESTONE} (input: {argv[0] if argv else '<none>'})",
        f"{PRODUCER_TASK}. Missing observation producer: {MISSING}")
    print(message, file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
