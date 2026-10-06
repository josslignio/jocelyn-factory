# TR_WEEKLY_PUBLISH_V1

Derived task brief. NOT canonical intent — voir projects/twitter-radar/ROADMAP.yaml et SPEC.md (section 2026-09-13).
Ne modifie ni ROADMAP.yaml, ni SPEC.md, ni PROJECT_STATE.json. Depot produit : /Users/jocelyngrosjean/conductor/workspaces/twitter-radar/twitter-radar-foundation-v2. Pas de `git stash`.

## Outcome
`scripts/tr_weekly_publish.py` enchaine sans IA : historique de marche incremental -> horizons KOL -> dossiers tickers ->
export statique -> publication, ecrit `factory_v2/receipts/TR_WEEKLY_PUBLISH_RECEIPT.json` (schema
tr-weekly-publish-receipt/v1 : control_sha, started_at, steps[{name, rc, duration_s, note}], site_generated_at,
published, reason), est branche sur le planificateur hebdo existant (documente dans docs/AUTOMATION.md), et un run reel
est committe. Outcome IMPLEMENTED seulement si le run reel est rc=0 sur toutes les etapes.

## Notes
- Publication : reprendre la sequence de `.context/run_tr_backfill_autopilot.zsh` (depot de controle) : export vers
  ~/.local/share/twitter-radar-site/latest puis rsync + commit + push vers le depot de site. Si le push echoue (reseau,
  credentials), published=false avec raison — jamais un faux true.
- Reprenable : chaque etape idempotente ; une etape en echec n'empeche pas d'ecrire le recu.
- Le planificateur `com.factory.tr-weekly-refresh` execute `factoryctl weekly-refresh-observed` dans le depot de
  controle ; documente comment ce script est appele ensuite (hook, commande chainee ou plist dedie lundi 08:30).

## Non-goals
- Aucun appel IA ; aucune modification des verificateurs ; aucune valeur fabriquee.

## Raisons PARTIAL typees (obligatoires dans REPORT.json.partial_reason)
PROVIDER_UNAVAILABLE | PROVIDER_RATE_LIMITED | NETWORK_UNAVAILABLE | TOOL_MISSING | DATA_INSUFFICIENT | BUDGET_EXHAUSTED.
