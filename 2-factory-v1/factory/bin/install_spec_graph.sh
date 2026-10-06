#!/usr/bin/env bash
# Optional artifact/quality tooling installer — NEVER runtime authority.
#
# spec-kit and graphify produce templates and spec graphs. They do not decide what the
# Factory builds, they do not orchestrate, and no gate may depend on their presence.
# When they are missing or fail to install, the honest outcome is TOOLING_DEFERRED — not
# a claimed success, and not a blocked runtime.
#
# CTO counter-audit 2026-09-11 found two problems here:
#   * SPEC_TAG defaulted to v0.8.7 while VENDOR_LOCK.json pins spec_kit to v0.9.0
#     (commit b40ec4160f8e8f5a48c3f7ceba42342417c171c6). The lock file was the authority
#     nobody read.
#   * Every step ran --force, including `specify init --here --force`, which overwrites
#     files in the TARGET repo, and two `uv tool install --force`, which overwrite
#     already-installed tools. A pin check that runs after an unconditional overwrite is
#     not a pin check.
#
# The default pin now comes from VENDOR_LOCK.json and is validated before anything is
# installed. --force is opt-in via ALLOW_FORCE=1 and never implied.
set -euo pipefail

REPO="${1:?repo path}"
LOCK="${VENDOR_LOCK:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/VENDOR_LOCK.json}"

defer() { echo "TOOLING_DEFERRED: $*" >&2; exit 3; }

[[ -f "$LOCK" ]] || defer "VENDOR_LOCK.json not found at $LOCK — refusing to install an unpinned tool"

command -v python3 >/dev/null || defer "python3 is unavailable, so the vendor lock cannot be read; spec-kit is optional tooling and the runtime does not depend on it"

# Every failure path here is TYPED. Under `set -e` a malformed lock aborted with a Python
# traceback and no TOOLING_DEFERRED line, which reads as a crash instead of as optional tooling
# being unavailable. The substitution is therefore guarded and any read failure defers.
read_lock() {
  local value
  if ! value="$(python3 -c 'import json,sys
try:
    lock = json.load(open(sys.argv[1]))
except Exception as exc:
    sys.exit("unreadable VENDOR_LOCK.json: %s" % exc)
c = lock.get("components", {}).get("spec_kit")
if not isinstance(c, dict):
    sys.exit("VENDOR_LOCK.json declares no spec_kit component")
print(c.get(sys.argv[2], ""))' "$LOCK" "$1" 2>&1)"; then
    defer "cannot read spec_kit.$1 from $LOCK: $value"
  fi
  printf '%s' "$value"
}

LOCKED_TAG="$(read_lock tag)"
LOCKED_COMMIT="$(read_lock commit)"
LOCK_STATUS="$(read_lock status)"

[[ -n "$LOCKED_TAG" ]]    || defer "VENDOR_LOCK.json declares no spec_kit tag"
[[ -n "$LOCKED_COMMIT" ]] || defer "VENDOR_LOCK.json declares no spec_kit commit for $LOCKED_TAG"

# The lock is the default. An explicit SPEC_TAG may only agree with it: a silent
# divergence between the installed tool and the recorded provenance is the whole failure
# mode this check exists for.
SPEC_TAG="${SPEC_TAG:-$LOCKED_TAG}"
if [[ "$SPEC_TAG" != "$LOCKED_TAG" ]]; then
  defer "SPEC_TAG=$SPEC_TAG contradicts VENDOR_LOCK spec_kit tag $LOCKED_TAG. Update VENDOR_LOCK.json (with the resolved commit) before installing a different pin."
fi
[[ "$LOCKED_TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || defer "spec_kit tag $LOCKED_TAG is not a pinned release tag"
[[ "$LOCKED_COMMIT" =~ ^[0-9a-f]{40}$ ]]         || defer "spec_kit commit $LOCKED_COMMIT is not a full 40-hex SHA"

GRAPHIFY_VERSION="${GRAPHIFY_VERSION:-0.9.26}"
[[ "$GRAPHIFY_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || defer "GRAPHIFY_VERSION=$GRAPHIFY_VERSION is not an exact version"

# --force overwrites the target repo (specify init --here --force) and already-installed
# tools. It is a destructive default, so it is now opt-in and reported.
FORCE_ARGS=("")   # set -u: an empty element keeps ${FORCE_ARGS[@]:+"${FORCE_ARGS[@]}"} expandable on bash 3.2
if [[ "${ALLOW_FORCE:-0}" == "1" ]]; then
  FORCE_ARGS=("--force")
  echo "ALLOW_FORCE=1 — will overwrite existing tool installs and target files in $REPO" >&2
fi

echo "spec_kit pin: $LOCKED_TAG @ $LOCKED_COMMIT (VENDOR_LOCK status: ${LOCK_STATUS:-unknown})"
echo "graphify pin: $GRAPHIFY_VERSION"

if [[ "${DRY_RUN:-0}" == "1" ]]; then
  echo "DRY_RUN=1 — pins validated, nothing installed"
  exit 0
fi

command -v uv >/dev/null || defer "uv is not installed; spec-kit is optional tooling and the runtime does not depend on it"

# Install from the LOCKED COMMIT, not the tag: a tag is mutable upstream, so "@v0.9.0" can
# fetch a different tree than the commit this lock recorded. The commit was previously only
# format-checked and then ignored.
uv tool install specify-cli ${FORCE_ARGS[@]:+"${FORCE_ARGS[@]}"} --from "git+https://github.com/github/spec-kit.git@${LOCKED_COMMIT}" \
  || defer "uv tool install specify-cli@${LOCKED_COMMIT} (${LOCKED_TAG}) failed"
uv tool install "graphifyy==${GRAPHIFY_VERSION}" ${FORCE_ARGS[@]:+"${FORCE_ARGS[@]}"} \
  || defer "uv tool install graphifyy==${GRAPHIFY_VERSION} failed"
uv tool update-shell || true

cd "$REPO"
# `specify init --here` writes into REPO. Without ALLOW_FORCE it must refuse rather than
# overwrite whatever is already there.
specify init --here ${FORCE_ARGS[@]:+"${FORCE_ARGS[@]}"} --integration claude --script sh \
  || defer "specify init failed in $REPO (set ALLOW_FORCE=1 only if overwriting existing files is intended)"
specify integration install codex --script sh    || defer "specify integration codex failed"
specify integration install opencode --script sh || defer "specify integration opencode failed"

graphify install --project                        || defer "graphify install failed"
graphify install --project --platform codex       || defer "graphify install (codex) failed"
graphify install --project --platform opencode    || defer "graphify install (opencode) failed"
graphify install --project --platform hermes      || defer "graphify install (hermes) failed"

echo "Review all generated files before commit. VENDOR_LOCK.json spec_kit.status is NOT"
echo "updated by this script: installed status is an observation, recorded by whoever"
echo "observed it."
git status --short
