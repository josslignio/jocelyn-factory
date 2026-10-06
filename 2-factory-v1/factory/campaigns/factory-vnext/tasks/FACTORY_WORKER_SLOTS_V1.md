# FACTORY_WORKER_SLOTS_V1

## Outcome
Prerequis mesurable de SCALE_2/SCALE_N : chaque tentative porte worker_slot et max_workers (FACTORY_MAX_WORKERS, defaut 1), chaque run du daemon ecrit une fenetre (started_at/ended_at) dans <run-dir>/run_windows.jsonl ; sans cela aucun debit ne peut etre divise par un nombre d ouvriers.

Derived task brief. NOT canonical intent — voir projects/factory/ROADMAP.yaml.
## DECISION CTO 2026-09-13 — remplace toute consigne contraire ci-dessus
Le juge d'acceptance est EXECUTE DEPUIS LE COMMIT EPINGLE b54a99960119 du depot de controle (git show b54a99967ef2cfc04e63f6c6afc939c7cd93a97a:factory_v2/verify_factory_hygiene.py) : le modifier a HEAD ne sert a rien. Lis la fonction du juge citee ci-dessous : elle est la definition exacte du « fini ». Ne modifie aucun fichier d'autorite. Pas de git stash. Committe avant de lancer le juge (il lit HEAD). Ne lance jamais la suite complete ni les 7 fichiers de tests inter-processus exclus si une mission est en vol.

## Acceptance (copie de la roadmap)
- Juge independant EPINGLE au commit b54a99960119 (verify_factory_hygiene.py worker-slots) : champs worker_slot/max_workers dans AttemptAccount et le ledger, FACTORY_MAX_WORKERS lu par factoryd/runtime/launch_gate, run_windows.jsonl ecrit par le daemon dans le run-dir
- Suite sure verte : python3 -m pytest tests -q -p no:cacheprovider -x --ignore=tests/test_stabilization_proof.py --ignore=tests/test_external_supervisor_production_path.py --ignore=tests/test_lr4_interprocess.py --ignore=tests/test_p0_supervision_repairs.py --ignore=tests/test_sol_ladder_fairness.py --ignore=tests/test_telemetry_integration.py --ignore=tests/test_tr_weekly_refresh_verifier.py

## Notes de construction
Fonction du juge : check_worker_slots. A construire : champs worker_slot (int, defaut 0) et max_workers (int, defaut 1) dans AttemptAccount (factory_v2/resource_accounting.py) et le ledger ; FACTORY_MAX_WORKERS lu dans factoryd.py/runtime.py/launch_gate.py (valeur 1 aujourd'hui : aucun dispatch parallele dans ce jalon) ; nouveau module factory_v2/run_windows.py (open_run(run_dir, pid) -> run_id, close_run(run_dir, run_id)) appele par factoryd au demarrage et a l arret propre, qui ecrit <run-dir>/run_windows.jsonl {run_id, pid, started_at, ended_at|null, max_workers} au demarrage et a l'arret propre ; helper resource_accounting.attempt_account_fixture_row() (ligne serialisee d un compte fixture, lit FACTORY_MAX_WORKERS) ; launch_gate.open_account estampille worker_slot/max_workers ; tests hermetiques.

## Raisons PARTIAL typees (REPORT.json.partial_reason)
TOOL_MISSING | DATA_INSUFFICIENT | BUDGET_EXHAUSTED | BEHAVIOUR_CHANGE_REQUIRED.
