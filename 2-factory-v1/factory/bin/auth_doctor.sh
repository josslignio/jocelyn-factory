#!/usr/bin/env bash
set -euo pipefail
LIVE=0
[[ "${1:-}" == "--live" ]] && LIVE=1

HERMES_PINNED="${AUTH_DOCTOR_HERMES_PINNED:-/Users/jocelyngrosjean/Library/Python/3.12/bin/hermes}"
if [[ -x "$HERMES_PINNED" ]]; then
  hermes() { "$HERMES_PINNED" "$@"; }
elif command -v hermes >/dev/null 2>&1; then
  echo "WARN pinned hermes path missing, falling back to PATH lookup: $(command -v hermes)"
else
  hermes() { echo "MISSING hermes (checked $HERMES_PINNED and PATH)" >&2; return 127; }
fi

bad=0
for k in OPENAI_API_KEY ANTHROPIC_API_KEY ZAI_API_KEY GLM_API_KEY OPENROUTER_API_KEY; do
  if [[ -n "${!k:-}" ]]; then echo "FAIL paid-key env present: $k"; bad=1; fi
done
(( bad == 0 )) || exit 3

for c in git jq python3 node claude codex opencode; do
  command -v "$c" >/dev/null && echo "OK $c: $(command -v "$c")" || { echo "MISSING $c"; bad=1; }
done
if [[ -x "$HERMES_PINNED" ]]; then
  echo "OK hermes: $HERMES_PINNED (pinned)"
elif command -v hermes >/dev/null 2>&1; then
  echo "OK hermes: $(command -v hermes)"
else
  echo "MISSING hermes"; bad=1
fi
(( bad == 0 )) || exit 2

git --version
python3 --version
node --version
claude --version || true
codex --version || true
opencode --version || true
hermes --version || true

echo "OpenCode Z.AI models:"
opencode models --refresh 2>/dev/null | grep 'zai-coding-plan/' | head -10 || {
  echo "FAIL no zai-coding-plan model detected"; exit 4;
}

if (( LIVE )); then
  live_bad=0

  out="$(env -u ANTHROPIC_API_KEY claude -p 'Réponds uniquement CLAUDE_SUBSCRIPTION_OK' --output-format text --max-turns 1 2>&1)" || true
  echo "$out"
  grep -q 'CLAUDE_SUBSCRIPTION_OK' <<<"$out" || { echo "FAIL claude live check: marker not found (command may have failed/auth revoked)"; live_bad=1; }

  tmp="$(mktemp -d)"; git -C "$tmp" init -q
  out="$(cd "$tmp" && env -u OPENAI_API_KEY codex exec --sandbox read-only 'Réponds uniquement CODEX_CHATGPT_OK' 2>&1)" || true
  echo "$out"
  grep -q 'CODEX_CHATGPT_OK' <<<"$out" || { echo "FAIL codex live check: marker not found (command may have failed/auth revoked)"; live_bad=1; }
  rm -rf "$tmp"

  # --format json was found unreliable here: opencode 1.17.20 intermittently
  # emits step_start/step_finish with no text part for this model over JSON
  # streaming, even though the plain-text renderer always shows the answer.
  # Plain text is used for this healthcheck; JSON stays available for callers
  # that need structured output.
  # Picking the first zai-coding-plan/* entry landed on glm-4.5-air, which
  # reproducibly returns an empty completion for this trivial prompt. Prefer
  # glm-5.2 explicitly; fall back to the first available model otherwise.
  models_list="$(opencode models --refresh 2>&1)" || true
  model="$(awk '/zai-coding-plan\/glm-5\.2$/{print $1; exit}' <<<"$models_list")"
  if [[ -z "$model" ]]; then
    model="$(awk '/zai-coding-plan\//{print $1; exit}' <<<"$models_list")"
  fi
  zai_ok=0
  for attempt in 1 2 3; do
    out="$(opencode run --model "$model" 'Réponds uniquement ZAI_CODING_PLAN_OK' 2>&1)" || true
    if grep -q 'ZAI_CODING_PLAN_OK' <<<"$out"; then zai_ok=1; echo "$out"; break; fi
    echo "WARN opencode/zai live check attempt $attempt/3: marker not found (infra flake, not auth), retrying"
  done
  (( zai_ok == 1 )) || { echo "FAIL opencode/zai live check: marker not found after 3 infra retries"; live_bad=1; }

  out="$(hermes chat -q 'Réponds uniquement HERMES_CODEX_OAUTH_OK' 2>&1)" || true
  echo "$out"
  grep -q 'HERMES_CODEX_OAUTH_OK' <<<"$out" || { echo "FAIL hermes live check: marker not found (command may have failed/auth revoked)"; live_bad=1; }

  (( live_bad == 0 )) || { echo "STATUS=BLOCKED_AUTH"; exit 6; }
fi
echo "DOCTOR_PASS"
