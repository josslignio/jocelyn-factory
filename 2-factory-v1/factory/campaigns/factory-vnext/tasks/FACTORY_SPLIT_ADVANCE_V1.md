# FACTORY_SPLIT_ADVANCE_V1

Derived task brief. NOT canonical intent — voir projects/factory/ROADMAP.yaml. Ne modifie ni ROADMAP.yaml, ni
PROJECT_STATE.json, ni factory_v2/verify_factory_hygiene.py (le juge est execute depuis le commit epingle b54a99967ef2 : le modifier a HEAD ne change rien), ni aucun fichier d'autorite.
Pas de `git stash` (`git show HEAD:<path>`). Commits atomiques, committe AVANT de lancer le juge (il lit HEAD).
Ne lance JAMAIS la suite complete ni les 7 fichiers de tests inter-processus exclus (ils tuent le daemon en vol).

## Outcome
runtime._advance_locked (2 429 lignes, complexite 390 le 13/09), _execute_recovery_work (407), _run_quarantine_cycle (395), _cnp_peek (252/48) decoupes en tranches nommees dans factory_v2/advance/*.py ; aucune fonction de runtime.py ni de factory_v2/advance/ ne depasse 400 lignes ou complexite 60 ; les noms publics restent importables ; comportement identique (suite sure verte).

## Acceptance (copie de la roadmap)
- Juge independant (ecrit par l operateur, hors de portee du builder) : git show b54a99967ef2cfc04e63f6c6afc939c7cd93a97a:factory_v2/verify_factory_hygiene.py > /tmp/factory_judge_$$.py && python3 /tmp/factory_judge_$$.py split-advance --project-repo . sort rc=0 a HEAD
- Aucune modification de comportement : les tests existants passent sans modification de leurs assertions (seuls des imports peuvent changer)
- Suite sure verte : python3 -m pytest tests -q -p no:cacheprovider -x --ignore=tests/test_stabilization_proof.py --ignore=tests/test_external_supervisor_production_path.py --ignore=tests/test_lr4_interprocess.py --ignore=tests/test_p0_supervision_repairs.py --ignore=tests/test_sol_ladder_fairness.py --ignore=tests/test_telemetry_integration.py --ignore=tests/test_tr_weekly_refresh_verifier.py
- factory_v2/verify_factory_hygiene.py et les fichiers d autorite ne sont pas modifies par la mission

## Notes
Pur deplacement : extraire des blocs contigus en fonctions de factory_v2/advance/<etape>.py qui recoivent/retournent explicitement l etat (state, project_dir, resolution, ...). Interdit : changer un ordre d evaluation, une condition, un evenement. Les tests qui patchent runtime.<nom> continuent de marcher via des wrappers/re-exports dans runtime.py.

## Raisons PARTIAL typees (REPORT.json.partial_reason)
TOOL_MISSING | DATA_INSUFFICIENT | BUDGET_EXHAUSTED | BEHAVIOUR_CHANGE_REQUIRED (explique quel test l exige) — jamais de texte libre.
