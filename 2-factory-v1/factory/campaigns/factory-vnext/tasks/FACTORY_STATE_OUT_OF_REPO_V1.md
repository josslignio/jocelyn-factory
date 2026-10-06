# FACTORY_STATE_OUT_OF_REPO_V1

Derived task brief. NOT canonical intent — voir projects/factory/ROADMAP.yaml. Ne modifie ni ROADMAP.yaml, ni
PROJECT_STATE.json, ni factory_v2/verify_factory_hygiene.py (le juge est execute depuis le commit epingle b54a99967ef2 : le modifier a HEAD ne change rien), ni aucun fichier d'autorite.
Pas de `git stash` (`git show HEAD:<path>`). Commits atomiques, committe AVANT de lancer le juge (il lit HEAD).
Ne lance JAMAIS la suite complete ni les 7 fichiers de tests inter-processus exclus (ils tuent le daemon en vol).

## Outcome
L etat runtime (factory/run/*, factory/projects/* etat V1, journaux runrecords/collection_sessions/CTO_EVENTS) n est plus suivi par git : deplace sous ~/Library/Application Support/JocelynFactory/ (deja utilise par le daemon launchd) ou gitignore ; apres un cycle du daemon, git status ne montre aucun fichier suivi modifie.

## Acceptance (copie de la roadmap)
- Juge independant (ecrit par l operateur, hors de portee du builder) : git show b54a99967ef2cfc04e63f6c6afc939c7cd93a97a:factory_v2/verify_factory_hygiene.py > /tmp/factory_judge_$$.py && python3 /tmp/factory_judge_$$.py state-out-of-repo --project-repo . sort rc=0 a HEAD
- Aucune perte : les fichiers existants sont migres (copie) avant d etre retires du suivi
- Suite sure verte : python3 -m pytest tests -q -p no:cacheprovider -x --ignore=tests/test_stabilization_proof.py --ignore=tests/test_external_supervisor_production_path.py --ignore=tests/test_lr4_interprocess.py --ignore=tests/test_p0_supervision_repairs.py --ignore=tests/test_sol_ladder_fairness.py --ignore=tests/test_telemetry_integration.py --ignore=tests/test_tr_weekly_refresh_verifier.py
- factory_v2/verify_factory_hygiene.py et les fichiers d autorite ne sont pas modifies par la mission

## Notes
Run-dir par defaut : ~/Library/Application Support/JocelynFactory/run (deja passe par --run-dir par launchd). Journaux append-only : deplacer et laisser un README dans l ancien emplacement. Ne touche pas a projects/*/PROJECT_STATE.json (chantier separe).

## Raisons PARTIAL typees (REPORT.json.partial_reason)
TOOL_MISSING | DATA_INSUFFICIENT | BUDGET_EXHAUSTED | BEHAVIOUR_CHANGE_REQUIRED (explique quel test l exige) — jamais de texte libre.
