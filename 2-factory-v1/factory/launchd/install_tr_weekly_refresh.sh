#!/usr/bin/env bash
# LOT C17 — idempotent installer for the TR weekly-refresh launchd schedule.
#
# NOT executed automatically by anything in this lot: run it by hand, once,
# after reviewing factory/launchd/com.factory.tr-weekly-refresh.plist (paths
# are hardcoded to this machine's control-repo checkout and this user's
# home — a different checkout location needs an edited plist first).
#
# Idempotent: safe to re-run after editing the plist. `launchctl bootout`
# on a not-yet-loaded label exits non-zero; that failure is expected and
# ignored (`|| true`) rather than aborting the install.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST_NAME="com.factory.tr-weekly-refresh.plist"
SRC="${HERE}/${PLIST_NAME}"
DEST_DIR="${HOME}/Library/LaunchAgents"
DEST="${DEST_DIR}/${PLIST_NAME}"
LABEL="com.factory.tr-weekly-refresh"
UID_GUI="gui/$(id -u)"

if [[ ! -f "${SRC}" ]]; then
    echo "install_tr_weekly_refresh: missing ${SRC}" >&2
    exit 1
fi

mkdir -p "${DEST_DIR}"

# Unload any previously installed copy of this label first — bootstrap on an
# already-loaded label fails, and a stale copy could carry an outdated
# schedule or command.
launchctl bootout "${UID_GUI}/${LABEL}" 2>/dev/null || true

cp "${SRC}" "${DEST}"

launchctl bootstrap "${UID_GUI}" "${DEST}"

echo "Installed and bootstrapped ${LABEL} from ${SRC} -> ${DEST}"
echo "Next scheduled run: Monday 08:00 local time (StartCalendarInterval)."
echo "Manual test run now:  launchctl start ${LABEL}"
echo "Logs: ~/Library/Logs/JocelynFactory/tr-weekly-refresh.{stdout,stderr}.log"
