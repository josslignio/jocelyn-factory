#!/usr/bin/env python3
"""Independent milestone acceptance check — the third, distinct proof of a repair.

R5 forbids a clear gate whose regression and independent-validation commands silently
collapse into the reproduction command; three identical commands are one proof wearing
three hats.  A quarantine RepairContract therefore binds three genuinely different
checks:

  reproduction  — the milestone's own product_e2e command, run raw
  regression    — the mission's executor test_cmd, a different suite
  independent   — this module: reload canonical authority, refreeze the milestone's
                  validation contract, and run it through IndependentValidator, which
                  also checks required_artifacts and forbidden_regressions

The independent check is not "the same command again": it goes through the validator
that settles milestones, from freshly loaded authority, so a repair that only makes one
raw command exit 0 does not satisfy it.

Usage:
    python3 -m factory_v2.verify_milestone_acceptance --project-dir <dir> --milestone <id>

Exit codes:
    0  acceptance verified
    1  acceptance not met (this is the expected pre-repair result)
    2  cannot evaluate (authority unreadable, milestone unknown)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from factory_v2.authority_gate import IndependentValidator  # noqa: E402
from factory_v2.project_authority import load_authority  # noqa: E402
from factory_v2.validation_contract import freeze_contract  # noqa: E402


def verify(project_dir: Path, milestone_id: str, timeout_s: int = 600) -> tuple[int, str]:
    project_dir = Path(project_dir)
    try:
        authority = load_authority(project_dir)
    except Exception as exc:
        return 2, f"AUTHORITY_UNREADABLE: {exc}"

    milestone = None
    for ms in (authority.roadmap or {}).get("milestones", []) or []:
        if ms.get("id") == milestone_id:
            milestone = ms
            break
    if milestone is None:
        return 2, f"UNKNOWN_MILESTONE: {milestone_id!r} is not in ROADMAP.yaml"

    try:
        contract = freeze_contract(project_dir, authority, milestone)
    except Exception as exc:
        return 2, f"CONTRACT_UNAVAILABLE: {exc}"

    verdict = IndependentValidator(project_dir, timeout_s=timeout_s).validate(contract)
    if verdict.milestone_complete:
        return 0, f"ACCEPTANCE_VERIFIED: {milestone_id}"
    return 1, f"ACCEPTANCE_NOT_MET: {milestone_id} — {verdict.failures}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project-dir", required=True)
    ap.add_argument("--milestone", required=True)
    ap.add_argument("--timeout-s", type=int, default=600)
    args = ap.parse_args(argv)
    rc, message = verify(Path(args.project_dir), args.milestone, args.timeout_s)
    print(message)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
