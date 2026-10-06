#!/usr/bin/env python3
"""REAL_AI_EXECUTION_RECEIPT gate.

DEFERRED — quadruple audit 2026-09-12 (P0: gate purement formelle).

This verifier used to ACCEPT on strings written by the same harness that wrote
`verdict=ACCEPTED`: the "independent validator" was a free-text field whose only
check was a 3-word blacklist (`trivial|stub|placeholder`). Writing "real" passed.
That is the exact trust defect already repaired for SCALE_2 / ADAPTIVE_ROUTING:
a receipt authored by the graded party cannot grade itself.

Two honest outcomes exist, and "pass" is not among them:

  * recompute from raw observations made by an independent producer (the
    supervisor's telemetry ledger + process table), bound to the exact run;
  * refuse, name the owed producer, and let the milestone stay unsettled.

Exits 2 (VERIFIER_NOT_IMPLEMENTED): fail closed. Do not replace this with a
shape check to obtain a settlement.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from factory_v2.evidence_grading import NOT_IMPLEMENTED, not_implemented  # noqa: E402

MILESTONE = "REAL_AI_EXECUTION"
PRODUCER_TASK = ("wire the receipt producer to the external supervisor's observed "
                 "launch ledger (external_supervisor state_dir) and the telemetry "
                 "observations, then recompute launches/usage from those raw records")
MISSING = ("independently observed model launches (supervisor process table + "
           "telemetry ledger), not harness-authored claims")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    code, message = not_implemented(
        f"{MILESTONE} (input: {argv[0] if argv else '<none>'})",
        f"{PRODUCER_TASK}. Missing observation producer: {MISSING}")
    print(message, file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
