#!/usr/bin/env python3
"""FULL_CODEBASE_STATE_OF_ART_AUDIT gate — FULL_CODEBASE_AUDIT_RECEIPT.json.

r1/r2 accepted whatever booleans the party being graded wrote — five
booleans, an empty stats block, an `evidence: "invented"` string, all passed,
because nothing recomputed the underlying facts independently. That defect is
now closed: `audit_observation_producer.build_observation()` recomputes the
control SHA, which required artifacts under `factory_v2/audit/` are actually
committed AT that exact SHA (not merely present on disk from an earlier
audit), a LOC count read from git blob content, and the reviewer identities
the settlement ledger — not the claimant — recorded.

A receipt is accepted ONLY when it names the exact digest of an observation
this script recomputes itself, right now, against the current checkout. A
receipt that does not carry `observation_hash`/`control_sha` at all (the old
shape), or names an observation that does not match a fresh recomputation
(stale, fabricated, or produced against a different SHA), is rejected.

Three honest outcomes now exist, in place of the old "pass or refuse":

  * PASS (exit 0)  — the receipt matches a fresh recomputation, and that
    observation is SETTLED (every artifact bound to HEAD, reviewers observed
    from the ledger).
  * FAIL/UNMET (exit 1) — recomputation succeeds and the receipt honestly
    matches it, but the milestone itself is not complete (PARTIAL/UNMET).
    This gate reports a pure typed diagnostic (unmet clauses, missing/stale
    artifacts) here; it does NOT write a RecoveryWork record — this verifier
    has no way to confirm any runtime actually reads `RECOVERY_DIR`, and a
    message claiming "recovery request written to X" when nothing downstream
    consumes X is exactly the kind of unverified claim this gate exists to
    refuse. Wiring a real RecoveryWork handoff is a separate, deliberate
    change, not a side effect of this diagnostic.
  * REJECTED (exit 1) — the receipt does not match what this script itself
    just recomputed: stale, fabricated, or shaped like the old claimant-only
    format.

Exit 2 (VERIFIER_NOT_IMPLEMENTED) is reserved for when recomputation itself
is impossible — e.g. HEAD cannot be observed (no git, detached corrupt repo).
That is the only case this verifier still fails closed on a producer gap.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from factory_v2 import audit_observation_producer as _producer  # noqa: E402
from factory_v2.evidence_grading import NOT_IMPLEMENTED, not_implemented  # noqa: E402

MILESTONE = _producer.MILESTONE
PRODUCER_TASK = "factory/campaigns/factory-vnext/tasks/FULL_CODEBASE_STATE_OF_ART_AUDIT.md (measure the counts, cite each public reference by repo+commit+file actually read, and record the reviewers that actually ran)"


def _load_receipt(path: Path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"RECEIPT_UNREADABLE: {path}: {exc}"
    if not isinstance(data, dict):
        return None, f"RECEIPT_NOT_AN_OBJECT: {path}"
    return data, ""


def evaluate(receipt_path: str, *, repo_root: "Path | None" = None,
            project_dir: "Path | None" = None,
            recovery_dir: "Path | None" = None,
            milestone: "str | None" = None) -> tuple[int, str]:
    # `recovery_dir` is accepted for call-site compatibility but unused here:
    # this gate no longer writes a RecoveryWork as a side effect of a pure
    # diagnostic (see module docstring, FAIL/UNMET case).
    """Pure evaluation, no sys.exit — importable by tests and callers.

    `milestone` (CODEX-LOT-C37): defaults to `MILESTONE`
    (FULL_CODEBASE_STATE_OF_ART_AUDIT, every existing caller unaffected) —
    threaded straight through to `build_observation()` so this gate is
    reusable for another milestone's own "≥3 distinct AI reviewers, exact
    roster" requirement without duplicating the recomputation/hash-matching
    logic above.
    """
    milestone = milestone or MILESTONE
    observation = _producer.build_observation(repo_root=repo_root, project_dir=project_dir,
                                              milestone=milestone)
    observation_root = Path(repo_root) if repo_root else _producer.REPO_ROOT
    control_sha = observation.get("control_sha", "")
    if not control_sha:
        return not_implemented(
            f"{milestone} (control SHA unobservable)",
            f"{PRODUCER_TASK}. HEAD could not be recomputed from git — the "
            "producer itself is unusable in this checkout.")

    if not receipt_path:
        return not_implemented(
            f"{milestone} (input: <none>)",
            f"{PRODUCER_TASK}. No receipt path supplied.")

    receipt, err = _load_receipt(Path(receipt_path))
    if receipt is None:
        return 1, f"AUDIT_RECEIPT_REJECTED: {err}"

    expected_hash = _producer.observation_hash(observation)
    receipt_hash = receipt.get("observation_hash", "")
    receipt_sha = receipt.get("control_sha", "")

    if not receipt_hash:
        return 1, ("AUDIT_RECEIPT_REJECTED: no observation_hash present — this looks "
                   "like the old claimant-authored shape (booleans/evidence strings "
                   "the auditee wrote about itself), which this gate no longer accepts")

    if receipt_sha and receipt_sha != control_sha:
        # A receipt cannot name the hash of the commit that records it, and the
        # Stop hook commits bookkeeping state at every session stop: accept the
        # receipt's SHA only if every commit since then touched nothing but the
        # audit artifacts, this receipt, or bookkeeping paths (same allowlist and
        # bounded walk as the producer). Then recompute the observation AT THAT
        # SHA — the hash comparison below stays a full independent recomputation.
        fresh = _producer._freshness_targets(observation_root, control_sha)
        if receipt_sha not in fresh:
            return 1, (f"AUDIT_RECEIPT_REJECTED: STALE — receipt bound to control_sha="
                       f"{receipt_sha!r}, current HEAD is {control_sha!r} and a commit "
                       f"since then touched audited code")
        observation = _producer.build_observation(repo_root=repo_root, project_dir=project_dir,
                                                  head=receipt_sha, milestone=milestone)
        control_sha = receipt_sha
        expected_hash = _producer.observation_hash(observation)

    if receipt_hash != expected_hash:
        return 1, (f"AUDIT_RECEIPT_REJECTED: observation_hash {receipt_hash!r} does not "
                   f"match a fresh recomputation ({expected_hash!r}) at control_sha="
                   f"{control_sha!r} — receipt is stale or fabricated")

    status = observation.get("status", "UNMET")
    if status == "SETTLED":
        return 0, (f"AUDIT_RECEIPT_ACCEPTED: {milestone} SETTLED at {control_sha} — "
                   f"observation_hash={expected_hash} matches independent recomputation")

    missing = observation.get("missing_or_stale_artifacts", [])
    unmet_clauses = observation.get("unmet_clauses", [])
    return 1, (f"AUDIT_RECEIPT_UNMET: {milestone} is {status} at {control_sha} — "
              f"unmet clauses: {unmet_clauses}; missing/stale artifacts: {missing}. "
              f"No RecoveryWork was created by this diagnostic — that handoff is "
              f"wired separately. Producer task: {PRODUCER_TASK}")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    receipt_path = argv[0] if argv else ""
    milestone = argv[1] if len(argv) > 1 else None
    code, message = evaluate(receipt_path, milestone=milestone)
    print(message, file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
