# FACTORY_SIMPLIFY_S2 — supprimer le chemin V1 (8 modules), lot S2

Derived task brief. NOT canonical intent — voir projects/factory/ROADMAP.yaml. Ne modifie ni ROADMAP.yaml,
ni PROJECT_STATE.json, ni factory_v2/audit/SIMPLIFICATION_BATCHES.json, ni factory_v2/verify_simplification_batch.py.

## Outcome
Les 9 fichiers du lot S2 (lis-les dans `factory_v2/audit/SIMPLIFICATION_BATCHES.json`, cle `batches.S2.paths`)
n'existent plus a HEAD ; aucune reference (import, `from x import`, chemin) ne subsiste dans l'arbre suivi,
tests compris ; la suite sure est verte ; `python3 factory_v2/verify_simplification_batch.py S2 --project-repo .`
sort rc=0 et ecrit `factory_v2/receipts/SIMPLIFICATION_S2_RECEIPT.json` (seul livrable declare).

## Fichiers a supprimer (lot S2, ~2 590 lignes)
- factory/bin/autopilot.py, campaign_run.py, campaign_state.py, cto_planner.py, rolling_backlog.py,
  auto_lessons.py, dashboard.py, mission_obs.py, verify_factory_dod.py (oracle V1 : charge campaign_state
  par spec_from_file_location ; cite seulement par FACTORY_LAW.md et factory/bootstrap/, hors perimetre)

## A NE PAS supprimer (importeurs vivants, verifie le 2026-09-13)
- factory/bin/ledger.py (launch_gate.py, product_verifiers.py, factoryctl.py, benchmark.py, receipt.py, milestone.py)
- factory/bin/mission_state.py (dispatcher_v3.py)
- factory/lib/backlog_refiller.py et la branche --legacy-v1 de factoryd.py : lot S4, pas ici.

## References restantes a traiter (mesurees par le verificateur, 2026-09-13)
- 19 fichiers de tests referencent ces modules (liste ci-dessous). Regle : un test qui n'existe QUE pour un
  module supprime est supprime avec lui — UNIQUEMENT ceux listes dans `batches.S2.tests_removable` du manifeste
  (tout autre test supprime = TEST_DELETED_OUTSIDE_LIST). Un test qui utilisait le module comme FIXTURE/helper
  est REECRIT sans lui et garde son intention ; jamais `skip`.
- tests/test_autopilot.py  (removable)
- tests/test_campaign_fault_manifest_reconcile.py  (a reecrire)
- tests/test_campaign_run.py  (removable)
- tests/test_campaign_state.py  (removable)
- tests/test_campaign_state_cli.py  (removable)
- tests/test_cto_planner.py  (removable)
- tests/test_dashboard.py  (removable)
- tests/test_factory_auto_lessons.py  (removable)
- tests/test_fault_f012_stale_lease_orphelin.py  (a reecrire)
- tests/test_fault_f013_events_jsonl_tronque.py  (a reecrire)
- tests/test_fault_f014_hash_chain_corrompue.py  (a reecrire)
- tests/test_fault_f020_deux_writers_concurrents.py  (a reecrire)
- tests/test_mission_obs.py  (removable)
- tests/test_p0_routing_budget_integration.py  (a reecrire)
- tests/test_recovery_qualification.py  (a reecrire)
- tests/test_rolling_backlog.py  (removable)
- tests/test_task_contract_validator.py  (a reecrire)
- tests/test_tb1_campaign_run_dispatcher.py  (removable)
- tests/test_verify_factory_dod.py  (a reecrire)
- factory/campaigns/fault-injection.yaml : 7 entrees `mechanism: factory/bin/campaign_run.py::...` (campagne V1) —
  retirer ces entrees (ou le fichier s'il ne decrit que le chemin V1) ; c'est une config, pas une autorite canonique.
- Les appels internes au lot (autopilot.py -> rolling_backlog, mission_obs) disparaissent avec les fichiers.
- Les JSON de preuve historiques (factory/campaigns/*/evidence, factory_v2/audit, factory/cto) citent ces chemins
  comme texte : NE PAS les modifier, le verificateur les ignore.

## Non-goals
- Aucun nouveau module, aucun refactor hors des references a retirer.
- factory/cto/cto_reporter.py : seule une docstring mentionne autopilot — ne pas toucher au code.
- Pas de `git stash` ; pour voir l'original : `git show HEAD:<path>`.

## Verification (dans cet ordre, dans le worktree de mission)
1. `git grep -n -P '(^|[^A-Za-z0-9_.])(import\s+(autopilot|campaign_run|campaign_state|cto_planner|rolling_backlog|auto_lessons|dashboard|mission_obs)\b|from\s+([A-Za-z0-9_.]+\.)?(autopilot|campaign_run|campaign_state|cto_planner|rolling_backlog|auto_lessons|dashboard|mission_obs)\s+import)' -- . ':!archive' ':!.context'` = vide
2. Suite sure : python3 -m pytest tests -q -p no:cacheprovider -x --ignore=tests/test_stabilization_proof.py --ignore=tests/test_external_supervisor_production_path.py --ignore=tests/test_lr4_interprocess.py --ignore=tests/test_p0_supervision_repairs.py --ignore=tests/test_sol_ladder_fairness.py --ignore=tests/test_telemetry_integration.py --ignore=tests/test_tr_weekly_refresh_verifier.py
3. `python3 factory_v2/verify_simplification_batch.py S2 --project-repo .` rc=0 (il lit le manifeste a HEAD : commits AVANT de verifier).

## Stop conditions
- Un test de fixture impossible a reecrire sans le module → outcome PARTIAL, dire lequel et pourquoi ; ne jamais le skip.
