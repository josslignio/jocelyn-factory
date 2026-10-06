#!/bin/zsh
# Reprise après coupure propre (2026-09-13). Idempotent. Ne relance rien si un build est en vol.
set -e
CTRL=/Users/jocelyngrosjean/conductor/workspaces/Jocelyn-factory-control/factory-foundation-v2
cd "$CTRL"
echo "[resume] 1/3 lever la pause (factory + twitter-radar)"
for p in factory twitter-radar; do
  [ -f projects/$p/PAUSE.json ] && git rm -q projects/$p/PAUSE.json
done
git diff --cached --quiet || git commit -qm "reprise après coupure propre $(date -u +%FT%TZ) : PAUSE levée"
echo "[resume] 2/3 daemon (launchd, exit 0 sur pause => kickstart nécessaire)"
launchctl kickstart -k "gui/$(id -u)/com.factory.factoryd"
echo "[resume] 3/3 backfill twscrape (LaunchAgent persistant, KeepAlive ; reprenable : curseurs data/backfill_state)"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/ai.twitter-radar.backfill.plist" 2>/dev/null || launchctl kickstart -k "gui/$(id -u)/ai.twitter-radar.backfill"
sleep 20; tail -3 "$HOME/Library/Logs/JocelynFactory/factoryd.stdout.log" | cut -c1-140
echo "[resume] console : python3 -m factory_v2.owner_console --out ~/.local/share/factory-console --projects projects/factory projects/twitter-radar"
