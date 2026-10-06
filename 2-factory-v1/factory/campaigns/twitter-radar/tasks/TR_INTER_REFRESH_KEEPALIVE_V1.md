# TR_INTER_REFRESH_KEEPALIVE_V1 — le produit vit entre deux refreshs hebdo

## Objectif
Chaque jour, sans humain : collecte incrémentale des comptes suivis (nouveaux tweets depuis le curseur), ré-analyse (ledger → records → horizons → table tickers → dossiers), export du site, et un reçu daté dans research/tr_keepalive_receipts.json (`day`, `collected_new_posts`, `reanalysis_ok`, `export_ok`, `duration_s`, `script_sha`). Le refresh hebdo (TR_WEEKLY_PUBLISH) reste la publication officielle ; le keepalive garantit que le site du matin n'a jamais plus de 24 h de retard.

## À construire
1. `scripts/tr_keepalive.py` : orchestre les étapes existantes (backfill incrémental, `factory_v2.tr_call.build_ledger` via FACTORY_CONTROL_REPO, `products.twitter_radar.tr_kol_records`, `scripts/tr_kol_horizons.py`, `scripts/build_tr_ticker_table.py`, `scripts/tr_ticker_dossier.py`, `app/export_static.py --out-dir ~/.local/share/twitter-radar-site/latest`) ; idempotent ; un reçu par jour (relance le même jour = même reçu mis à jour).
2. LaunchAgent `ai.twitter-radar.keepalive` (plist dans deploy/, StartCalendarInterval 06:00 local) — installation documentée, pas exécutée par le builder.
3. Tests hermétiques (étapes mockées ; reçu écrit ; échec d'une étape = reçu avec ok=false, jamais absent).

## Juge indépendant
`TR_INTER_REFRESH_KEEPALIVE_V1` : ≥ 3 reçus sur 3 jours distincts, tous ok. Le builder produit les 3 premiers reçus en exécutant réellement le script 3 fois avec `--day` forcé sur 3 dates (données réelles, pas de mock).
Niveau B, budget 7200 s.
