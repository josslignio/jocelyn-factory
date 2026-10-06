#!/usr/bin/env bash
# M0A — non-autonomous bootstrap (order §4.2, clarified by mid-campaign P0.1/P1 patches).
#
# P1 clarification: this script may invoke Claude with a fixed implementation order, but no
# validation/acceptance/promotion/self-certification decision is ever made here or by any LLM.
# Every gate below is a subprocess exit code check. This script does not try to write every M0
# sub-capability itself — that is the builder's job, across separate commits.
#
# P0.1: the final self-host authority is verify_bootstrap_dod.py, NOT the candidate's own
# verify_factory_dod.py. This script calls both, but only verify_bootstrap_dod.py's exit code
# and its M0_SELF_HOST_PASS.json gate `campaign-run --from M1`.
set -euo pipefail

REPO="${1:?usage: bootstrap_factory_v1.sh <repo> <candidate_sha> <proof_dir>}"
CANDIDATE_SHA="${2:?usage: bootstrap_factory_v1.sh <repo> <candidate_sha> <proof_dir>}"
PROOF_DIR="${3:?usage: bootstrap_factory_v1.sh <repo> <candidate_sha> <proof_dir>}"
BOOTSTRAP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "== M0A step 1: repo path / branch / worktree cleanliness =="
cd "$REPO"
git rev-parse --show-toplevel >/dev/null
BRANCH="$(git branch --show-current)"
echo "branch=$BRANCH"
if [ -n "$(git status --porcelain)" ]; then
  echo "FAIL: worktree not clean, refusing to bootstrap"
  exit 1
fi

echo "== M0A step 2: capture baseline SHA =="
BASELINE_SHA="$(git rev-parse HEAD)"
echo "baseline_sha=$BASELINE_SHA"

echo "== M0A step 3: verify BOOTSTRAP_LOCK.json integrity (external, before anything else) =="
python3 "$BOOTSTRAP_DIR/verify_lock_only.py" --lock "$BOOTSTRAP_DIR/BOOTSTRAP_LOCK.json"

echo "== M0A step 4: run existing tests (informational baseline, not a gate by itself) =="
python3 -m pytest tests/ -q || echo "WARNING: baseline suite not fully green — recorded, not fatal at this step"

echo "== M0A step 5: candidate-side fast self-check (bootstrap_gate.py, non-authoritative) =="
python3 "$BOOTSTRAP_DIR/bootstrap_gate.py" --repo "$REPO" --candidate-sha "$CANDIDATE_SHA" || \
  echo "WARNING: bootstrap_gate.py reported problems — informational only, verify_bootstrap_dod.py is authoritative"

echo "== M0A step 6 (M0B): external, locked, independent self-host verification =="
python3 "$BOOTSTRAP_DIR/verify_bootstrap_dod.py" \
  --repo "$REPO" --candidate-sha "$CANDIDATE_SHA" --proof-dir "$PROOF_DIR" \
  --lock "$BOOTSTRAP_DIR/BOOTSTRAP_LOCK.json"
STATUS=$?

if [ "$STATUS" -eq 0 ]; then
  echo "M0_SELF_HOST_EXTERNAL_VERIFICATION=PASS"
  echo "READY_TO_RESUME — campaign-run --from M1 now permitted"
else
  echo "M0_SELF_HOST_EXTERNAL_VERIFICATION=FAIL"
  echo "campaign-run --from M1 remains FORBIDDEN until this passes"
fi
exit "$STATUS"
