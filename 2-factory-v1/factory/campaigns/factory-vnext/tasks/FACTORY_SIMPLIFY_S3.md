# FACTORY_SIMPLIFY_S3

Derived task brief. NOT canonical intent — voir projects/factory/ROADMAP.yaml. Ne modifie aucun fichier d'autorite ni
projects/twitter-radar/ROADMAP.yaml (ses commandes doivent continuer de marcher via des shims). Pas de git stash.
Le juge est execute depuis le commit epingle b54a99967ef2 : `git show b54a99967ef2cfc04e63f6c6afc939c7cd93a97a:factory_v2/verify_factory_hygiene.py > /tmp/factory_judge_$$.py && python3 /tmp/factory_judge_$$.py product-out-of-runtime --project-repo .`
Ne lance JAMAIS la suite complete ni les 7 fichiers de tests inter-processus exclus.

## Outcome
Modules produit TR deplaces (git mv) sous products/twitter_radar/ (package avec __init__.py), imports corriges partout
(tests inclus), shims <= 25 lignes aux anciens chemins cites par la roadmap TR (factory_v2/product_verifiers.py,
factory_v2/verify_tr_weekly_operate.py, ...), aucun `from . import tr_*` dans le runtime, suite sure verte, juge rc=0.

## Notes
- Inventaire : factory_v2/tr_call.py, tr_kol.py, tr_product_ready_observer.py, tr_radar_harness.py, tr_ready_harness.py,
  tr_refresh_harness.py, tr_web_harness.py, twitter_radar_long_run.py, weekly_observation.py, run_tr_collection.py,
  verify_tr_weekly_operate.py, et les branches verify_tr_* de product_verifiers.py (scinder : la partie factory reste).
- Un shim = `from products.twitter_radar.<module> import *` + `main` re-exporte ; <= 25 lignes.
- executors.py importe weekly_observation : passer par products.twitter_radar.weekly_observation.

## Raisons PARTIAL typees (REPORT.json.partial_reason)
TOOL_MISSING | DATA_INSUFFICIENT | BUDGET_EXHAUSTED | BEHAVIOUR_CHANGE_REQUIRED.
