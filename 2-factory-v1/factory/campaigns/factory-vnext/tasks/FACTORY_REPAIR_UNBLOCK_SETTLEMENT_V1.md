# FACTORY_REPAIR_UNBLOCK_SETTLEMENT_V1 — Orchestrateur advance : états bloqués sans issue et invariants de settlement non tenus

## Objectif
Aucun état bloqué n'est terminal sans issue : holds, parcages, quarantaines et recoveries se réévaluent quand leur cause externe disparaît, et la sélection respecte les invariants (retired, disjonction de livrables, seuils gelés).

## Contrat
Pour CHAQUE id ci-dessous : test qui reproduit le défaut (rouge), correctif minimal au fichier:ligne cités, test vert, et une ligne dans
`factory_v2/audit/CODE_REVIEW_2026-09/REPAIRS_UNBLOCK_SETTLEMENT.json` : `{"id", "status": "FIXED|WONTFIX", "commit", "test", "note"}` (WONTFIX seulement démontré par un test). ≥ 80 % FIXED.
Livrables de la synthèse (à honorer en plus des constats) :
- Cartographie des états bloquants (gate_hold, EXTERNAL_BLOCK, parked, head-of-line recovery) avec condition de sortie
- Invalidation des holds sur changement de cause (quarantaine disparue, autorité mise à jour)
- Prédicat d'éligibilité partagé par toutes les voies de sélection (retired, livrables occupés)
- Seuils gelés jamais abaissés au runtime ; invalidations persistées avant toute sortie anticipée

## Constats (id · fichier:ligne · catégorie/sévérité · pourquoi)
- `L1_advance-002` · `factory_v2/advance/milestone_selection.py:105` · BUG/HIGH · La voie alternative après parking réimplémente la sélection sans exclure retired. Le resolver canonique choisit A et ignore OLD retiré ; lorsque A est parqué, cette boucle sélectionne OLD avant B pourtant éligible. Du tr
- `L1_advance-003` · `factory_v2/advance/milestone_selection.py:304` · RACE/HIGH · La rotation anti-famine ne tient pas compte des livrables des workers actifs. Avec C actif sur shared.txt, A bloqué et B candidat sur shared.txt, _parallel_alternate retourne None car A n’est pas actif ; _rotate_away_fro
- `L1_advance-004` · `factory_v2/advance/milestone_guards.py:60` · BUG/HIGH · La garde lit un seul recovery global puis exige qu’il appartienne au jalon sélectionné. Si un recovery OTHER précède celui de A dans la liste retournée par read_pending_recovery_work, A ignore son propre A#compile à chaq
- `L1_advance-005` · `factory_v2/advance/mission_dispatch.py:154` · BUG/LOW · Le record mission et ctx.mission_rec référencent le même dictionnaire. L’affectation du nouveau contract_id à la ligne 151 remplace donc aussi la valeur lue comme previous_contract à la ligne 154. Une adoption old → c es
- `L1_advance-008` · `factory_v2/advance/mission_dispatch.py:1102` · BUG/HIGH · La capture remplace le record par _build_mission_state_record, qui ne conserve pas executor_type posé par _ss4. Au cycle suivant, la garde STEP_TYPE_CHANGED lit donc une chaîne vide et ne s’applique jamais aux résultats 
- `L1_advance-009` · `factory_v2/advance/quarantine_ladder.py:26` · BUG/MEDIUM · _completed("DIAGNOSE") ne distingue pas succès et terminaison en échec. Un record du bon idempotency_key avec completed=True et settlement_success=False est pris pour un diagnostic accompli et autorise SAFE_REPAIR avec c
- `L1_advance-010` · `factory_v2/advance/quarantine_stall.py:65` · BUG/HIGH · Le drift d’autorité rend la chaîne EXTERNAL_BLOCK terminale aux lignes 199–202, mais toute sortie de terminal exige ici un changement du code de contrôle et un VerifiedFixReceipt rouge→vert. Même après mise à jour du con
- `L1_advance-011` · `factory_v2/advance/settlement.py:56` · BUG/MEDIUM · La boucle peut marquer une première mission UNPROVEN après ReceiptVerificationError, puis retourner immédiatement sur une mission suivante parquée. Le save situé après la boucle n’est alors jamais appelé. La reproduction
- `L1_advance-012` · `factory_v2/advance/settlement.py:143` · BUG/HIGH · Le cache gate_hold est réutilisé uniquement sur candidate/contrat/code. Il ne recontrôle pas la quarantaine étrangère qui avait rendu l’escalade indisponible. Après disparition de cette quarantaine, un gate non admissibl
- `L1_advance-013` · `factory_v2/advance/settlement.py:512` · BUG/HIGH · Le seuil de validation gelé est abaissé par le runtime lorsque le builder figure dans le roster. Un contrat min_distinct_accept=3 avec codex/claude/glm et builder=codex franchit la revue sur seulement deux ACCEPTs. La co
- `L1_advance-014` · `factory_v2/advance/settlement.py:683` · BUG/HIGH · Après abaissement du minimum de 2 à 1, _hardened_review reste vrai mais cette branche choisit le chemin legacy. Il ne fixe aucun engine_hint, ne re-vérifie pas la provenance de l’ACCEPT reçu et ne contrôle plus le roster
- `L1_advance-015` · `factory_v2/advance/settlement.py:527` · BUG/HIGH · Le cache des ACCEPTs est testé avant les BLOCKs. Si le même SHA possède les ACCEPTs requis puis un BLOCK (par exemple après une nouvelle exigence de revue ou une relecture), le premier if réussit et le elif BLOCK est ina
- `L1_advance-016` · `factory_v2/advance/settlement.py:557` · BUG/HIGH · La prévalidation du produit dans _ss4 modifie en place le dictionnaire executor de la mission autopilot en local_command. Le settlement de ce même cycle recherche ensuite les réparateurs en consultant ce dictionnaire mut
- `L2_runtime_core-008` · `factory_v2/escalation_ladder.py:134` · BUG/HIGH · record_attempt détruit la mémoire d’idempotence des settlements. Il remplace tout l’enregistrement de l’échelle sans conserver applied_settlement_ids ni last_settlement_id. Une reprise déjà réglée et comptée est alors re
- `L2_runtime_core-018` · `factory_v2/runtime.py:1001` · BUG/HIGH · Le budget empêche de fermer une reprise devenue périmée. Le test de staleness est présent, mais vient après check_before_launch et son retour RECOVERY_LAUNCH_BLOCKED. Avec trois appels sans progrès, aucun grant et une em
- `L2_runtime_core-019` · `factory_v2/runtime.py:3356` · BUG/HIGH · La pause de six heures se réarme elle-même après expiration. Sous autorisation permanente, l’historique des trois empreintes identiques reste inchangé pendant la pause. À l’échéance, le else recalcule maintenant+6h et re
- `L5_factory_v2_rest_2-013` · `factory_v2/stop_gate_policy.py:244` · BUG/HIGH · La détection du travail restant ne lit pas la roadmap : sans current_milestone ni superseded_claims, un PROJECT_STATE valide mais vide est assimilé à une campagne terminée. Avec un jalon non exécuté et aucun propriétaire

## Interdits
Modifier un test existant pour le faire passer. Toucher ROADMAP.yaml / PROJECT_STATE.json. Supprimer un constat sans preuve.

## Juge
`python3 factory_v2/verify_code_review_repairs.py --project-repo . --ids-file factory/config/code_review_repair_unblock_settlement_ids.json --repairs-file factory_v2/audit/CODE_REVIEW_2026-09/REPAIRS_UNBLOCK_SETTLEMENT.json` puis suite sûre. Relecture Codex. Niveau A, budget 3 h.
