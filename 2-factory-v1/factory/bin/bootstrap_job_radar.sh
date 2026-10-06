#!/usr/bin/env bash
set -euo pipefail
REPO="${1:?usage: bootstrap_job_radar.sh PRODUCT_REPO PACK_ROOT STATE_DIR}"
PACK="${2:?pack root}"
STATE="${3:?state dir}"
REPO="$(cd "$REPO" && pwd)"
PACK="$(cd "$PACK" && pwd)"
mkdir -p "$STATE"
STATE="$(cd "$STATE" && pwd)"
git -C "$REPO" rev-parse --git-dir >/dev/null

for k in OPENAI_API_KEY ANTHROPIC_API_KEY ZAI_API_KEY GLM_API_KEY OPENROUTER_API_KEY; do
  [[ -z "${!k:-}" ]] || { echo "REFUSE: paid-key env present: $k"; exit 3; }
done

# Static product artifacts only. `cp -n` declines to overwrite an existing destination, which is
# the desired no-clobber behavior on re-run — but BSD cp (macOS) exits 1 in that case (GNU cp
# exits 0), so `|| true` is required under `set -e` to make a decline a no-op, not a fatal error.
mkdir -p "$REPO/lessons" "$REPO/decisions/drafts" "$REPO/specs"
cp -n "$PACK/factory/FACTORY_LAW.md" "$REPO/FACTORY_LAW.md" || true
cp -n "$PACK/factory/AGENTS.md" "$REPO/AGENTS.md" || true
cp -n "$PACK/job-radar/lessons/validated.jsonl" "$REPO/lessons/validated.jsonl" || true
cp -n "$PACK/job-radar/lessons/candidates.jsonl" "$REPO/lessons/candidates.jsonl" || true
for d in "$PACK"/job-radar/specs/JR-M*; do
  mid="$(basename "$d")"; mkdir -p "$REPO/specs/$mid"
  cp -n "$d"/*.md "$REPO/specs/$mid/" || true
done

# Dynamic state belongs to Factory Control.
mkdir -p "$STATE/evidence" "$STATE/worktrees" "$STATE/locks" "$STATE/retrospectives"
cp -n "$PACK/job-radar/roadmap.jsonl" "$STATE/roadmap.jsonl" || true
: > "$STATE/events.jsonl"
cat > "$STATE/config.json" <<'JSON'
{
  "max_repairs": 2,
  "max_infra_retries": 3,
  "build_timeout_sec": 2700,
  "review_timeout_sec": 1200,
  "test_timeout_sec": 1800,
  "additional_api_cost_allowed": false
}
JSON
cat > "$STATE/project.json" <<JSON
{
  "slug": "job-radar",
  "product_repo": "$REPO",
  "no_go": true
}
JSON

# The state directory must be inside the Factory Control git repository.
git -C "$STATE" rev-parse --show-toplevel >/dev/null
git -C "$STATE" add .
git -C "$STATE" commit -m "factory(job-radar): initialize durable state" || true

echo "BOOTSTRAP_OK product=$REPO state=$STATE"
