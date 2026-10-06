# FACTORY_METRICS_DASHBOARD_V1

Derived task brief. NOT canonical intent — voir projects/factory/ROADMAP.yaml. Ne modifie ni ROADMAP.yaml, ni
PROJECT_STATE.json, ni factory_v2/verify_factory_hygiene.py (le juge est execute depuis le commit epingle b54a99967ef2 : le modifier a HEAD ne change rien), ni aucun fichier d'autorite.
Pas de `git stash` (`git show HEAD:<path>`). Commits atomiques, committe AVANT de lancer le juge (il lit HEAD).
Ne lance JAMAIS la suite complete ni les 7 fichiers de tests inter-processus exclus (ils tuent le daemon en vol).

## Outcome
factory/run/BUILD_METRICS_LATEST.{json,md} recalcules a chaque heure par le daemon depuis attempt_accounts/telemetry : par projet et par moteur sur 24 h : lancements, succes, refus sans moteur, duree moyenne, quota par moteur (tokens), redemarrages, jalons parques, boucles detectees, settlements ; sections Vitesse / Quota / Reprise / Boucles lisibles.

## Acceptance (copie de la roadmap)
- Juge independant (ecrit par l operateur, hors de portee du builder) : git show b54a99967ef2cfc04e63f6c6afc939c7cd93a97a:factory_v2/verify_factory_hygiene.py > /tmp/factory_judge_$$.py && python3 /tmp/factory_judge_$$.py metrics-dashboard --project-repo . sort rc=0 a HEAD
- Chaque chiffre est recalculable depuis les ledgers (le juge recompte les lancements)
- Suite sure verte : python3 -m pytest tests -q -p no:cacheprovider -x --ignore=tests/test_stabilization_proof.py --ignore=tests/test_external_supervisor_production_path.py --ignore=tests/test_lr4_interprocess.py --ignore=tests/test_p0_supervision_repairs.py --ignore=tests/test_sol_ladder_fairness.py --ignore=tests/test_telemetry_integration.py --ignore=tests/test_tr_weekly_refresh_verifier.py
- factory_v2/verify_factory_hygiene.py et les fichiers d autorite ne sont pas modifies par la mission

## Notes
Producteur existant : factory/bin ou factory_v2 (grep BUILD_METRICS_LATEST). window_h=24. Champs exiges par le juge : generated_at, window_h, per_project, per_engine, launches, launches_success, launches_refused_without_engine, parked_milestones, settlements, mean_duration_s, restarts, loops_detected. Sections Markdown : Vitesse, Quota, Reprise, Boucles.

## Raisons PARTIAL typees (REPORT.json.partial_reason)
TOOL_MISSING | DATA_INSUFFICIENT | BUDGET_EXHAUSTED | BEHAVIOUR_CHANGE_REQUIRED (explique quel test l exige) — jamais de texte libre.
