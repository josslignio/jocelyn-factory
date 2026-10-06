# FACTORY_RESILIENCE_V1 — coupures = interruptions, reprise sans rien perdre

## Objectif
Coupures réseau, quota, kill et reboot traitées comme des interruptions reprises au checkpoint (jamais des échecs moteur) ; bascule de moteur avant la coupure de quota ; attente réseau typée et bornée ; le tout jugé par le juge épinglé `resilience` et prouvé par un drill réel.


## Contexte (décision propriétaire 2026-09-13)
Internet, courant, quota et fenêtre plantée coupent souvent. Avant : chaque coupure = SEMANTIC_FAILURE
(imputée au moteur, empreinte d'échec vers MILESTONE_PARKED, travail non committé perdu, redémarrage à zéro).

## Déjà livré au commit 9c0eeaf1 (Claude builder, factory à l'arrêt)
- `factory_v2/resilience.py` : classify_interruption (QUOTA_CUT/NETWORK_CUT), network_available/wait_for_network,
  checkpoint_worktree/latest_checkpoint/restore_checkpoint (refs/factory-checkpoints/<mission>, stash-create + update-ref,
  index et fichiers intacts), WorktreeCheckpointer (toutes les 180 s, FACTORY_CHECKPOINT_INTERVAL_S), projected_allowance /
  choose_engine_with_quota (bascule AVANT la coupure).
- dispatcher_v3 `_run_engine` : checkpoints pendant le run ; NETWORK_CUT → attente réseau bornée puis reprise EN PLACE ;
  QUOTA_CUT → provider marqué épuisé (QUOTA_SNAPSHOT) + rc typé ; `interruption`/`resumed_in_place`/`checkpoints` sur
  chaque engine_execution ; préambule REPRISE au builder quand le worktree récupéré contient du travail en cours.
- runtime/mission_dispatch : exit_class QUOTA/NETWORK (jamais SEMANTIC_FAILURE), empreinte d'échec inchangée, événement
  ATTEMPT_INTERRUPTED ; routing_evidence ignore les coupures ; model_router juge l'utilisation projetée.
- factoryd : WAITING_NETWORK sans ouvrir de tentative.
- Backfill twscrape : LaunchAgent persistant (RunAtLoad+KeepAlive) piloté par le même PAUSE.json.

## Reste à prouver (ce jalon)
1. Juge épinglé `resilience` (verify_factory_hygiene.py @ 9d5314f6ae536992f65233c3451fce58257d65bd) vert à HEAD.
2. Contre-audit multi-IA (Codex + GLM) des scénarios : kill du dispatcher en plein build → reprise au checkpoint ;
   coupure réseau simulée (FACTORY_FORCE_OFFLINE=1) → WAITING_NETWORK, zéro tentative, zéro charge ; quota épuisé →
   bascule pré-emptive puis reprise ; reboot → daemon + backfill repartent seuls (PAUSE.json absent).
3. Drill en conditions réelles (à la reprise de la factory, autorisé par le propriétaire) : tuer un build réel en vol
   (SIGKILL du dispatcher), vérifier que la tentative suivante reprend le worktree avec le préambule REPRISE et que le
   ledger porte KILL (pas SEMANTIC_FAILURE) ; rapport dans .context/factory-tightening-audit-2026-09-12/RESILIENCE_DRILL.md.
