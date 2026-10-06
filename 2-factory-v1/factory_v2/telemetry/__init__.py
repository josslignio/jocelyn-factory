"""Production measurement for every AI call the Factory makes.

Not a dashboard, not a service, not a framework: four small stdlib modules and a
CLI, sitting beside the accounting that already exists.

    record.py     the common vocabulary every provider is translated into
    adapters.py   one parser per format the Factory ACTUALLY launches
    capture.py    bounded, non-blocking, never-raising log capture
    ledger.py     append-only JSONL, idempotent by observation key
    inventory.py  every launch path, each INSTRUMENTED or a named gap
    aggregate.py  roll-ups that carry their own unknown-coverage
    report.py     `python3 -m factory_v2.telemetry` — JSON + Markdown

The invariants, in one place:

  1. Four disjoint units — calls, tokens, quota window, money — never merged.
  2. An unmeasured quantity is UNKNOWN with a reason and a timestamp. Never 0.
  3. The denominator for "what did a result cost" is `independently_validated`,
     never `mission_pass`.
  4. Reviews, retries, repairs and waits are IN the cost of an accepted change,
     and are linked to their parent so they are attributable without being
     counted twice.
  5. Published aggregates must be reconstructible from the raw ledger. The
     verifier recomputes them and fails closed when they are not.
  6. No prompt, no source, no secret, no reasoning CONTENT enters telemetry.
     Reasoning is counted; it is never quoted.
  7. A telemetry failure degrades the report, never the quota brake and never
     the independent validation of a mission.
"""
from .record import (ABSENT, COMPLETE, PARTIAL_FLOOR, SCHEMA, UNKNOWN,
                     LaunchObservation, Money, QuotaSnapshot, Usage,
                     is_measured, measured, observation_key, redact_argv,
                     unknown, value_of)

__all__ = [
    "ABSENT", "COMPLETE", "PARTIAL_FLOOR", "SCHEMA", "UNKNOWN",
    "LaunchObservation", "Money", "QuotaSnapshot", "Usage",
    "is_measured", "measured", "observation_key", "redact_argv", "unknown",
    "value_of",
]
