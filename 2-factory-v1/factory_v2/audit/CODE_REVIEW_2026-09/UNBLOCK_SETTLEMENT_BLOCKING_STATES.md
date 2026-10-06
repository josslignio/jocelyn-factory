# Cartographie des états bloquants — FACTORY_REPAIR_UNBLOCK_SETTLEMENT_V1

Livrable de synthèse (tentative 5, 2026-09-21). Ancrages relevés à HEAD `fec99d07ef20` par
`grep -n` (commande citée en fin de document). Invariant de la mission : **aucun état
bloqué n'est terminal sans issue** — chaque ligne ci-dessous nomme la condition de sortie,
le code qui la réévalue et le test qui l'épingle.

## 1. États bloquants et conditions de sortie

| État | Où il est posé | Cause | Condition de sortie | Où elle est réévaluée | Test qui l'épingle |
|---|---|---|---|---|---|
| `MILESTONE_PARKED` (6 h) | `factory_v2/runtime.py:3325` | 3 empreintes d'échec identiques sous autorisation permanente | `parked_until` expiré → streak effacée, retry autorisé, jamais réarmé sans nouvel échec | `factory_v2/runtime.py:3419` | `tests/test_unblock_settlement_repairs.py::test_l2_runtime_core_019_expired_park_allows_retry` |
| jalon parqué, voie alternative | `factory_v2/advance/milestone_selection.py:100` | le jalon résolu est parqué | un autre jalon éligible au sens du **prédicat partagé** (non retiré, non parqué, dépendances canoniques, livrables disjoints) | `factory_v2/advance/milestone_selection.py:193` · `factory_v2/advance/milestone_selection.py:178` | `tests/test_unblock_settlement_repairs.py::test_l1_advance_002_retired_never_selected_after_parking` · `tests/test_unblock_settlement_synthesis.py::test_unblock_synth_001_alternate_path_honours_retired_dependency` |
| famine (`STUCK_MILESTONE_YIELDED`) | `factory_v2/advance/milestone_selection.py:303` | deux issues non-progrès consécutives ou garde d'oisiveté | rotation vers un jalon éligible **et** dont les livrables ne recoupent aucun worker actif | `factory_v2/advance/milestone_selection.py:303` | `tests/test_unblock_settlement_repairs.py::test_l1_advance_003_rotation_respects_active_deliverables` · `tests/test_unblock_settlement_synthesis.py::test_unblock_synth_001_rotation_shares_canonical_dependency_semantics` |
| jalon résolu déjà en vol | `factory_v2/advance/milestone_selection.py:214` | worker vivant sur le jalon | jalon parallèle à livrables disjoints (même prédicat) | `factory_v2/advance/milestone_selection.py:214` | `tests/test_unblock_settlement_repairs.py::test_l1_advance_003_rotation_respects_active_deliverables` |
| `MILESTONE_GATE_HELD` (`gate_hold`) | `factory_v2/runtime.py:2233` | verdict bloquant + escalade indisponible (quarantaine étrangère) | identité (candidat, contrat, code de contrôle) modifiée — `factory_v2/runtime.py:2216` — **ou** quarantaine causale disparue / remplacée (`milestone_id` + `entered_at`) / désormais nôtre → hold invalidé, persisté, validateur relancé | `factory_v2/advance/settlement.py:162` | `tests/test_unblock_settlement_repairs.py::test_l1_advance_012_hold_invalidated_when_quarantine_cause_disappears` · `tests/test_unblock_settlement_synthesis.py::test_unblock_synth_002_hold_kept_while_unscoped_foreign_quarantine_persists` · `tests/test_unblock_settlement_synthesis.py::test_unblock_synth_002_hold_invalidated_when_quarantine_replaced` |
| `EXTERNAL_BLOCK` (chaîne de réparation) | `factory_v2/advance/quarantine_stall.py:61` | contrat de réparation en drift avec l'autorité | hashes du contrat de réparation à nouveau égaux à l'autorité courante → chaîne rouverte ACTIVE sans exiger un nouveau SHA de code | `factory_v2/advance/quarantine_stall.py:44` | `tests/test_unblock_settlement_repairs.py::test_l1_advance_010_authority_refresh_reopens_external_block` |
| quarantaine, terminal (`STALLED`, …) | `factory_v2/advance/quarantine_stall.py:61` | échelle épuisée | changement de code de contrôle + VerifiedFixReceipt rouge→vert, ou porte sondée d'abord quand HEAD ≠ pre_repair_sha | `factory_v2/advance/quarantine_ladder.py:145` | `tests/test_chaos_drill_v2.py` (STALE_QUARANTINE_AFTER_FIX) |
| échelle DIAGNOSE → SAFE_REPAIR | `factory_v2/advance/quarantine_ladder.py:14` | diagnostic terminé en échec | seul un DIAGNOSE `settlement_success=True` autorise SAFE_REPAIR ; un échec terminé n'occupe plus l'étape, un nouveau DIAGNOSE est programmé | `factory_v2/advance/quarantine_ladder.py:14` | `tests/test_unblock_settlement_repairs.py::test_l1_advance_009_failed_diagnose_cannot_authorize_repair` |
| recovery tête de file étrangère | `factory_v2/advance/milestone_guards.py:21` | recovery OTHER précède celle du jalon sélectionné | lecture **scopée au jalon** : le jalon prend sa propre recovery | `factory_v2/advance/milestone_guards.py:21` | `tests/test_unblock_settlement_repairs.py::test_l1_advance_004_scoped_recovery_skips_foreign_head` |
| `RECOVERY_LAUNCH_BLOCKED` sur travail périmé | `factory_v2/runtime.py:1029` | budget épuisé + recovery dont la cause a disparu | clôture « stale » AVANT la porte budget (aucun appel modèle), persistée | `factory_v2/runtime.py:984` | `tests/test_unblock_settlement_repairs.py::test_l2_runtime_core_018_stale_recovery_closes_before_budget_gate` |
| `WAITING_FOR_EVIDENCE` | `factory_v2/advance/settlement.py:276` | vérificateur calendaire | `next_check_at` atteint, ou plafond de waits sans progrès → GATE_BLOCKED normal | `factory_v2/advance/settlement.py:276` | `tests/test_gate_failure_hold_and_rebuild.py` |
| jalons tenus par dépendance / quarantaine | `factory_v2/runtime.py:4126` · `factory_v2/runtime.py:4050` | dépendance non COMPLETE (et non retirée) ; dépendance structurelle sur le jalon quarantainé | dépendance COMPLETE ou RETIRÉE ; quarantaine levée | mêmes fonctions, même sémantique que le resolver canonique | `tests/test_unblock_settlement_synthesis.py::test_unblock_synth_001_rotation_shares_canonical_dependency_semantics` |
| sortie de session (stop gate) | `factory_v2/stop_gate_policy.py:226` | PROJECT_STATE vide | la roadmap fait autorité : un jalon non COMPLETE et non retiré = travail restant, sortie refusée | `factory_v2/stop_gate_policy.py:226` | `tests/test_unblock_settlement_repairs.py::test_l5_factory_v2_rest_2_013_unfinished_roadmap_blocks_exit` |

## 2. Invariants de settlement

| Invariant | Code | Test |
|---|---|---|
| Seuil gelé `min_distinct_accept` jamais abaissé au runtime (roster insatisfaisable = blocage typé `MULTI_AI_REVIEW_UNAVAILABLE`, rotation vers un jalon indépendant) | `factory_v2/advance/settlement.py:440` · `factory_v2/advance/settlement.py:527` | `tests/test_unblock_settlement_repairs.py::test_l1_advance_013_frozen_review_threshold_never_lowered` · `tests/test_unblock_settlement_repairs.py::test_l1_advance_014_hardened_single_remaining_reviewer_keeps_provenance` |
| Un BLOCK sur le SHA exact prime sur les ACCEPTs en cache | `factory_v2/advance/settlement.py:543` | `tests/test_unblock_settlement_repairs.py::test_l1_advance_015_block_overrides_cached_accepts` |
| Invalidations (UNPROVEN, hold) persistées avant toute sortie anticipée | `factory_v2/advance/settlement.py:59` · `factory_v2/advance/settlement.py:162` | `tests/test_unblock_settlement_repairs.py::test_l1_advance_011_invalidations_saved_before_parked_return` · `tests/test_unblock_settlement_repairs.py::test_l1_advance_012_hold_invalidated_when_quarantine_cause_disappears` |
| Idempotence des settlements conservée par `record_attempt` | `factory_v2/escalation_ladder.py:122` | `tests/test_unblock_settlement_repairs.py::test_l2_runtime_core_008_record_attempt_preserves_settlement_ids` |
| Le record mission conserve `executor_type` (garde STEP_TYPE_CHANGED) ; la prévalidation ne mute pas la mission canonique | `factory_v2/advance/mission_dispatch.py` (`_ss5_launch_and_capture`, `_ss4_lease_and_checkpoint`) | `tests/test_unblock_settlement_repairs.py::test_l1_advance_008_capture_preserves_executor_type` · `tests/test_unblock_settlement_repairs.py::test_l1_advance_016_prevalidation_does_not_mutate_canonical_executor` |

## 3. Prédicat d'éligibilité partagé

`_candidate_eligible` (`factory_v2/advance/milestone_selection.py:193`) est l'unique prédicat des trois voies de sélection
non canoniques (voie alternative après parking, `_parallel_alternate`, `_rotate_away_from_stuck`).
Il refuse : jalon retiré, COMPLETE, parqué, exclu (en vol, tenu, quarantainé), dépendances
non satisfaites au sens canonique (`_dependencies_satisfied`, `factory_v2/advance/milestone_selection.py:178` : seule
`required_state: COMPLETE` bloque ; COMPLETE **ou** RETIRÉ satisfait), livrables recoupant
(exact ou imbriqué) ceux d'un worker actif. Le resolver canonique (`project_authority.resolve`)
applique la même sémantique de dépendances ; les deux ne peuvent plus diverger sur « retiré ».

## 4. Écarts trouvés à la contre-audit de la tentative 5 (réparés)

- **UNBLOCK_SYNTH-001** — `_candidate_eligible` exigeait toute dépendance COMPLETE ; un jalon
  dépendant d'un jalon RETIRÉ était exécutable pour le canonique et jamais pour les voies
  alternatives → parcage sans issue / famine. Fichier original `factory_v2/advance/milestone_selection.py:177`.
- **UNBLOCK_SYNTH-002** — un `gate_hold` causé par une quarantaine factoryd sans `milestone_id`
  portait `held_by_quarantine=""` et était invalidé à chaque poll (validateur relancé à chaque
  cycle, boucle C38 rouverte). Fichier original `factory_v2/advance/settlement.py:155` ;
  le hold enregistre désormais `held_by_quarantine_entered_at` (`factory_v2/runtime.py:2273`).

## 5. Commande d'ancrage

```
grep -n '<motif>' <fichier>   # motifs : les en-têtes `def …` et commentaires cités ci-dessus, à HEAD fec99d07ef20
```
