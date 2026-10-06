# SAFE_SELF_IMPROVEMENT

## Outcome

(verbatim `goal` du jalon `SAFE_SELF_IMPROVEMENT` — projects/factory/ROADMAP.yaml ; section ajoutée le 2026-09-12 pour satisfaire le préflight check_task NO_OUTCOME_MARKER ; le gate recalcule les acceptances, il ne lit pas cette section.)

Champion/challenger automatisé avec promotion CAS sur benchmark


Derived task brief. NOT canonical intent: the authority for this work is
`projects/factory/STRATEGY.md` / `SPEC.md` / `ROADMAP.yaml` at the frozen contract SHAs.
If this brief and the ROADMAP acceptance ever disagree, the ROADMAP wins and this file is
wrong. Do not edit ROADMAP.yaml, PROJECT_STATE.json, any verifier, any credential or any
owner authority from inside this mission.

## Deliverables (from ROADMAP)
- factory_v2/receipts/CHALLENGER_AUTOMATION_RECEIPT.json

## Acceptance (verbatim from ROADMAP — the gate recomputes these, it does not read claims)
- UNVALIDATED_SELF_MODIFICATION = 0
- benchmark documented
- PROMOTE/REJECT decision justified
- no self-widening of capability, network or validation authority
- isolated challenger population/archive, independent evaluators active
- BUILDER_CAN_MODIFY_EVALUATOR_OR_REWARD=0

## Bounded scope
- `factory_v2/cell_challenger.py --package --out` already runs a challenger; reuse it.
- A measured REJECT is a COMPLETED experiment. The champion stays. Record the measured decision and the observed champion identity. A future independently
  qualified evaluator may accept a completed REJECT experiment; the current gate defers (exit 2).
- Every decision must be reconstructed from supervisor-owned observations bound to exact
  code, corpus, contract and attempt identities. An existing file or a run_id string is not
  proof. The current gate defers until that independent producer/consumer path exists.
- The builder may not touch the evaluator, the reward or the capability envelope. If the
  experiment needs that, it stops and reports the need.

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
- Decision CTO 2026-09-13 : producteur factory_v2/capability_snapshot.py fige {actor_capabilities, owner_policy_ids, validator_source_sha256 (sha256 de factory_v2/product_verifiers.py a HEAD), network_policy} avant creation d un challenger et apres promotion ; ledger append-only factory_v2/receipts/self_modification_ledger.jsonl (une ligne par modification issue d un challenger : sha, validated true|false, validated_by) ; promotions = deplacement exact-SHA d un pointeur avec compare-and-swap (cas_expected == cas_observed)
- Juge independant EPINGLE au commit b54a99960119 (verify_factory_hygiene.py self-improvement) : snapshots avant/apres identiques (D018 : aucun elargissement de capacite/reseau/autorite), validator_source_sha256 recalcule a HEAD, zero entree non validee (UNVALIDATED_SELF_MODIFICATION=0), promotions exact-SHA CAS, decision_evidence_path a HEAD
- Suite sure verte : python3 -m pytest tests -q -p no:cacheprovider -x --ignore=tests/test_stabilization_proof.py --ignore=tests/test_external_supervisor_production_path.py --ignore=tests/test_lr4_interprocess.py --ignore=tests/test_p0_supervision_repairs.py --ignore=tests/test_sol_ladder_fairness.py --ignore=tests/test_telemetry_integration.py --ignore=tests/test_tr_weekly_refresh_verifier.py

## Notes de construction
Fonction du juge : check_self_improvement. A construire : factory_v2/capability_snapshot.py (snapshot() -> {actor_capabilities: dict fige de _ACTOR_CAPABILITY, owner_policy_ids: liste triee, validator_source_sha256: sha256 de factory_v2/product_verifiers.py a HEAD, network_policy: dict}) ecrit sous factory_v2/receipts/capability_snapshots/<ts>_<before|after>.json ; ledger factory_v2/receipts/self_modification_ledger.jsonl ; recu CHALLENGER_AUTOMATION_RECEIPT.json (schema challenger-automation-receipt/v1 : snapshot_before, snapshot_after (chemins), ledger, promotions [{sha, cas_expected, cas_observed}], decision, decision_evidence_path). Le mecanisme de challenger = reutiliser run_matched_benchmark (jalon precedent) : un challenger = un SHA candidat ; promotion = ecriture CAS de factory_v2/CHAMPION_POINTER.

## Raisons PARTIAL typees (REPORT.json.partial_reason)
TOOL_MISSING | DATA_INSUFFICIENT | BUDGET_EXHAUSTED | BEHAVIOUR_CHANGE_REQUIRED.
