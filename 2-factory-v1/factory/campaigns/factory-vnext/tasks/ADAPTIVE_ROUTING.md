# ADAPTIVE_ROUTING

## Outcome

(verbatim `goal` du jalon `ADAPTIVE_ROUTING` — projects/factory/ROADMAP.yaml ; section ajoutée le 2026-09-12 pour satisfaire le préflight check_task NO_OUTCOME_MARKER ; le gate recalcule les acceptances, il ne lit pas cette section.)

Routage des IA par performance mesurée (task_type × engine)


Derived task brief. NOT canonical intent: the authority for this work is
`projects/factory/STRATEGY.md` / `SPEC.md` / `ROADMAP.yaml` at the frozen contract SHAs.
If this brief and the ROADMAP acceptance ever disagree, the ROADMAP wins and this file is
wrong. Do not edit ROADMAP.yaml, PROJECT_STATE.json, any verifier, any credential or any
owner authority from inside this mission.

## Deliverables (from ROADMAP)
- routing_stats/routing_summary.json
- the routing code that actually consults RunRecords

## Acceptance (verbatim from ROADMAP — the gate recomputes these, it does not read claims)
- the engine choice CHANGES between two sessions when RunRecords show a better engine
- correct fallback when the preferred engine is unavailable
- no hardcoded rule outranks the RunRecords
- calibrated prediction of action/engine outcome; PREDICTION_CALIBRATION_MEASURED=1

## Bounded scope
- `factory/bin/model_router.py` is TODAY a hardcoded table. Do not claim it is adaptive.
- Read the honest denominator: `factory_v2.runrecords.engine_stats()` counts ALL attempts,
  failures included (attempts / validated_accepted / acceptance_rate / wall_time_unknown).
- routing_summary.json must carry task_type_engine_stats with real counts and, per decision,
  the observation it was based on (`based_on`) plus task_type/spec_level. The verifier
  rejects an empty stats block and a decision with no basis.
- Do NOT wire adaptive selection into the production dispatch path until comparable
  observations exist for at least two engines on the same task type. Ship the measurement
  first; the switch is a separate, evidenced step.
- The ActorCapabilityPolicy is not negotiable by statistics: no amount of evidence moves
  an S/A build to an engine the policy excludes.

## Stop conditions
- Budget exhausted → stop and leave the evidence written. A truthful incomplete run beats
  an authored receipt.
- Any acceptance clause you cannot produce independent evidence for → leave it unmet and
  say so in the receipt. `VERIFIER_NOT_IMPLEMENTED` / unmet is an outcome; a fabricated
  boolean is a defect.
- Never write a receipt field the verifier would grade unless a run produced it.
## DECISION CTO 2026-09-13 — remplace toute consigne contraire ci-dessus
Le juge d'acceptance est EXECUTE DEPUIS LE COMMIT EPINGLE b54a99960119 du depot de controle (git show b54a99967ef2cfc04e63f6c6afc939c7cd93a97a:factory_v2/verify_factory_hygiene.py) : le modifier a HEAD ne sert a rien. Lis la fonction du juge citee ci-dessous : elle est la definition exacte du « fini ». Ne modifie aucun fichier d'autorite. Pas de git stash. Committe avant de lancer le juge (il lit HEAD). Ne lance jamais la suite complete ni les 7 fichiers de tests inter-processus exclus si une mission est en vol.

## Acceptance (copie de la roadmap)
- Decision CTO 2026-09-13 (mesure : Codex 1/9 vs Claude 6/8 en missions produit) : model_router expose runrecords_override(spec_level, task_kind, evidence) et route(..., evidence=...) le consulte ; constantes MIN_EVIDENCE_N (>= 5 lancements valides par moteur) et MIN_SUCCESS_GAP (>= 0.20) ; une preuve forte change le moteur choisi, une preuve mince laisse la table de depart ; dispatcher_v3 passe la preuve derivee du ledger (settlements independamment valides par moteur, jamais les revendications du builder) a chaque route()
- Juge independant EPINGLE au commit b54a99960119 (verify_factory_hygiene.py adaptive-routing) : sonde synthetique forte (claude 7/8 vs codex 1/8 => claude), mince (n=2 => table de depart), cablage reel du dispatcher ; rc=0 a HEAD
- Aucune regle codee en dur ne prime sur la preuve : la table _ROUTING est la valeur par defaut sans preuve suffisante, pas un veto ; les exceptions proprietaire (owner_routing_policy) restent prioritaires et documentees
- Suite sure verte : python3 -m pytest tests -q -p no:cacheprovider -x --ignore=tests/test_stabilization_proof.py --ignore=tests/test_external_supervisor_production_path.py --ignore=tests/test_lr4_interprocess.py --ignore=tests/test_p0_supervision_repairs.py --ignore=tests/test_sol_ladder_fairness.py --ignore=tests/test_telemetry_integration.py --ignore=tests/test_tr_weekly_refresh_verifier.py

## Notes de construction
Fonction du juge : check_adaptive_routing. A construire : (1) factory/bin/model_router.py : runrecords_override(spec_level, task_kind, evidence) -> engine|None ; evidence = {engine: {n, success}} ou n et success ne comptent QUE les lignes independently_validated=true (mission_pass seul = 0) ; MIN_EVIDENCE_N=5, MIN_SUCCESS_GAP=0.20 ; route(spec_level, task_kind, evidence=None, ...) l'appelle avant la table _ROUTING (les exceptions owner_routing_policy restent prioritaires) ; (2) une fonction factory_v2/routing_evidence.py::evidence_from_ledger(project_dir, spec_level, task_kind) qui derive n/success par moteur depuis attempt_accounts.jsonl + settlements independamment valides (independently_validated=true), jamais mission_pass seul ; (3) dispatcher_v3 expose route_for_dispatch(spec_level, task_kind, project_dir) — UNIQUE point d entree du routage utilise par dispatch_v3 (>= 2 appels) — qui derive la preuve via evidence_from_ledger(project_dir, ...) et appelle model_router.route(..., evidence=...) ; (4) recu factory_v2/receipts/ADAPTIVE_ROUTING_RECEIPT.json (schema adaptive-routing-receipt/v1 : evidence observee par (spec_level, task_kind), moteur choisi, table par defaut) ; tests hermetiques.

## Raisons PARTIAL typees (REPORT.json.partial_reason)
TOOL_MISSING | DATA_INSUFFICIENT | BUDGET_EXHAUSTED | BEHAVIOUR_CHANGE_REQUIRED.
