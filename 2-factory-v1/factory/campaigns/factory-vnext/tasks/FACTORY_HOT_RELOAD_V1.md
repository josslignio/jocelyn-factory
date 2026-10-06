# FACTORY_HOT_RELOAD_V1

Derived task brief. NOT canonical intent — voir projects/factory/ROADMAP.yaml et projects/factory/STRATEGY.md (section
« Amelioration continue sans arret »). Ne modifie aucun fichier d'autorite. Pas de git stash. Le juge est execute depuis le
commit epingle b54a99967ef2 : `git show b54a99967ef2cfc04e63f6c6afc939c7cd93a97a:factory_v2/verify_factory_hygiene.py > /tmp/factory_judge_$$.py && python3 /tmp/factory_judge_$$.py hot-reload --project-repo .` ; lis sa fonction check_hot_reload, c'est la definition du fini.

## Outcome
factory_v2/hot_reload.py : RELOAD_SENSITIVE (7 modules), reload_needed(touched_files) -> bool, decide(in_flight, pending)
-> 'WAIT' | 'RELOAD_NOW' | 'NOOP'. factoryd.py : apres chaque settlement du projet factory, touched_files = git diff
--name-only <base_sha>..<integration_sha> du candidat ; si reload_needed -> pending=True + log RELOAD_SCHEDULED ; a chaque
cycle, si decide(...) == 'RELOAD_NOW' -> log RELOAD_NOW et sys.exit(3) (launchd KeepAlive SuccessfulExit=false relance).
Jamais de sortie avec une mission en vol. Tests hermetiques tests/test_hot_reload.py (decision matrix + wiring par mock).

## Raisons PARTIAL typees
TOOL_MISSING | BEHAVIOUR_CHANGE_REQUIRED | BUDGET_EXHAUSTED.
