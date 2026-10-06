# CHAMPION_VS_RESHAPED_CHALLENGER

## Outcome

(verbatim `goal` du jalon `CHAMPION_VS_RESHAPED_CHALLENGER` — projects/factory/ROADMAP.yaml ; section ajoutée le 2026-09-12 pour satisfaire le préflight check_task NO_OUTCOME_MARKER ; le gate recalcule les acceptances, il ne lit pas cette section.)

Benchmark champion vs reshaped challenger (mêmes workloads réels, restart, failures) puis PROMOTE/REJECT


Derived task brief. Authority is `projects/factory/{STRATEGY.md,SPEC.md,ROADMAP.yaml}` at the
frozen contract SHAs; where this brief and the ROADMAP acceptance disagree, the ROADMAP wins.
Do not edit any verifier, PROJECT_STATE, credential or authority document from inside the
mission. Build the real champion-vs-challenger comparison.

## Why this is an implementation task

`factory_v2/run_f5_benchmark.py` exists, but it performs schema checks rather than a measured
comparison of two builds. The gate (`verify_benchmark_receipt.py`) returns VERIFIER_NOT_IMPLEMENTED (exit 2).
A file that exists, including one containing observed=true, cannot prove who ran an experiment.
Implement the independent producer and reconstruction before freezing the evaluator and
launching either arm. Until then, this milestone cannot settle.

## Deliverables

A producer that runs the SAME workload against champion and challenger with identical start
SHA, acceptance, data and fault injection, and records per arm: verified delivery, product
E2E, context continuity, restart recovery, failure recovery, wall time, quota, LOC, complexity.
The supervisor records raw observations outside claimant-owned product output and selects
them by exact identity and content hash. The independent evaluator reconstructs outcomes,
costs and the comparison from those observations; no claimed path or boolean grants trust.

## Decision rule (fixed BEFORE running)

PROMOTE only if the challenger wins on the declared metric without an acceptance regression.
A measured REJECT is a COMPLETED experiment when an independently qualified evaluator
reconstructs it and verifies that the champion was preserved. The current deferred gate
accepts neither PROMOTE nor REJECT. If the observations do not separate the two
builds, the verdict is `INCONCLUSIVE` and the champion stands. Nothing is promoted because its
author says it is faster.

## Negative cases / budget / stop

Negative: unequal budget accounting between arms must FAIL; a probe citing a nonexistent
observation must FAIL; `authority_hashes={}` must FAIL. Budget 7200 s plus the declared model
spend of the runs, which is SCHEDULED work — never launched in the middle of an audit. Stop:
if the arms cannot be matched, report `ARMS_NOT_MATCHED` and settle nothing.
## DECISION CTO 2026-09-13 — remplace toute consigne contraire ci-dessus
Le juge d'acceptance est EXECUTE DEPUIS LE COMMIT EPINGLE b54a99960119 du depot de controle (git show b54a99967ef2cfc04e63f6c6afc939c7cd93a97a:factory_v2/verify_factory_hygiene.py) : le modifier a HEAD ne sert a rien. Lis la fonction du juge citee ci-dessous : elle est la definition exacte du « fini ». Ne modifie aucun fichier d'autorite. Pas de git stash. Committe avant de lancer le juge (il lit HEAD). Ne lance jamais la suite complete ni les 7 fichiers de tests inter-processus exclus si une mission est en vol.

## Acceptance (copie de la roadmap)
- Decision CTO 2026-09-13 : champion = runtime d avant la Ferrari (commit 5257c8c9), challenger = HEAD ; producteur factory_v2/run_matched_benchmark.py execute la MEME charge (workload_id fige : sondes hermetiques du runtime — dispatch, kill/reprise, quarantaine, regel de contrat, moteur factice FACTORY_NO_REAL_ENGINE=1 ; sondes moteur reel seulement si FACTORY_BENCHMARK_REAL_ENGINES=1, sinon typees NOT_EXERCISED) sur les deux SHA depuis le meme start_sha de projet de test, une observation JSON par sonde et par cote (side, probe_id, workload_id, start_sha, settled, wall_s, restart_recovered, launches_lost) sous factory_v2/benchmarks/<workload_id>/
- Juge independant EPINGLE au commit b54a99960119 (verify_factory_hygiene.py matched-benchmark) : memes sondes des deux cotes, metriques recalculees depuis les observations, PROMOTE seulement si le challenger domine (settled >=, wall_s <=, launches_lost <=, restart_recovered >=, non identique), REJECT laisse le pointeur champion inchange, PROMOTE le deplace exactement sur challenger_sha, decision_evidence_path present a HEAD
- La qualite de la justification est relue par le relecteur independant du jalon (revue multi-IA), pas par le juge
- Suite sure verte : python3 -m pytest tests -q -p no:cacheprovider -x --ignore=tests/test_stabilization_proof.py --ignore=tests/test_external_supervisor_production_path.py --ignore=tests/test_lr4_interprocess.py --ignore=tests/test_p0_supervision_repairs.py --ignore=tests/test_sol_ladder_fairness.py --ignore=tests/test_telemetry_integration.py --ignore=tests/test_tr_weekly_refresh_verifier.py

## Notes de construction
Fonction du juge : check_matched_benchmark. Champion = commit 5257c8c9 (avant la Ferrari), challenger = HEAD. A construire : factory_v2/run_matched_benchmark.py --workload <id> --champion <sha> --challenger <sha> : pour chaque cote, checkout du sha dans un worktree temporaire, projet de test hermetique (tmp) au meme start_sha, sondes : dispatch_settles (mission factice qui doit se regler), kill_and_resume (SIGKILL du dispatcher puis reprise), quarantine_cycle, contract_refreeze, terminal_state_new_attempt ; chaque sonde ecrit factory_v2/benchmarks/<workload_id>/<side>_<probe_id>.json {side, probe_id, workload_id, start_sha, settled, wall_s, restart_recovered, launches_lost, engine_exercised: FAKE|REAL|NOT_EXERCISED} ; recu factory_v2/receipts/BENCHMARK_RECEIPT.json (schema matched-benchmark-receipt/v1 : workload_id, start_sha, champion_sha, challenger_sha, observations_dir, metrics {champion, challenger : probes, settled, wall_s, restart_recovered, launches_lost}, decision PROMOTE|REJECT, champion_pointer_before/after (fichier factory_v2/CHAMPION_POINTER), decision_evidence_path). Attention : le champion 5257c8c9 n'a pas les corrections C43-C49 : c'est le but de la comparaison.

## Raisons PARTIAL typees (REPORT.json.partial_reason)
TOOL_MISSING | DATA_INSUFFICIENT | BUDGET_EXHAUSTED | BEHAVIOUR_CHANGE_REQUIRED.
