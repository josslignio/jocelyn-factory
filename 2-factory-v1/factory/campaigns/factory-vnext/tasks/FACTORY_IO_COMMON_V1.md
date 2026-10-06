# FACTORY_IO_COMMON_V1

Derived task brief. NOT canonical intent — voir projects/factory/ROADMAP.yaml. Ne modifie ni ROADMAP.yaml, ni
PROJECT_STATE.json, ni factory_v2/verify_factory_hygiene.py (le juge est execute depuis le commit epingle b54a99967ef2 : le modifier a HEAD ne change rien), ni aucun fichier d'autorite.
Pas de `git stash` (`git show HEAD:<path>`). Commits atomiques, committe AVANT de lancer le juge (il lit HEAD).
Ne lance JAMAIS la suite complete ni les 7 fichiers de tests inter-processus exclus (ils tuent le daemon en vol).

## Outcome
Un seul module factory_v2/_io.py porte _now, _atomic_write, _read_json, _git ; toutes les copies locales (11 _now, 5 _read_json, 5 _git, 3 _atomic_write mesurees le 13/09) sont remplacees par un import ; comportement identique.

## Acceptance (copie de la roadmap)
- Juge independant (ecrit par l operateur, hors de portee du builder) : git show b54a99967ef2cfc04e63f6c6afc939c7cd93a97a:factory_v2/verify_factory_hygiene.py > /tmp/factory_judge_$$.py && python3 /tmp/factory_judge_$$.py io-common --project-repo . sort rc=0 a HEAD
- Chaque helper a EXACTEMENT une definition de premier niveau dans factory/ + factory_v2/ (hors campaigns/evidence), dans factory_v2/_io.py
- Suite sure verte : python3 -m pytest tests -q -p no:cacheprovider -x --ignore=tests/test_stabilization_proof.py --ignore=tests/test_external_supervisor_production_path.py --ignore=tests/test_lr4_interprocess.py --ignore=tests/test_p0_supervision_repairs.py --ignore=tests/test_sol_ladder_fairness.py --ignore=tests/test_telemetry_integration.py --ignore=tests/test_tr_weekly_refresh_verifier.py
- factory_v2/verify_factory_hygiene.py et les fichiers d autorite ne sont pas modifies par la mission

## Notes
Semantique la plus stricte conservee : _atomic_write = tmp + fsync + os.replace ; _read_json fail-closed (leve ou retourne None documente) ; _git(repo,*args,timeout) capture_output text ; _now ISO8601 UTC secondes. Remplace un a un, lance les tests du fichier touche a chaque commit.

## Raisons PARTIAL typees (REPORT.json.partial_reason)
TOOL_MISSING | DATA_INSUFFICIENT | BUDGET_EXHAUSTED | BEHAVIOUR_CHANGE_REQUIRED (explique quel test l exige) — jamais de texte libre.
