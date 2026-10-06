# TR_WEEKLY_OPERATE_V1 — vérificateur d'exploitation hebdomadaire (niveau B, mécanique)

## Outcome
OUTCOME: `factory_v2/verify_tr_weekly_operate.py` existe, est fail-closed, et `python3 -m pytest tests/test_tr_weekly_operate_v1.py -q` passe (6 cas, dont 4 négatifs). Aucun modèle ni réseau appelé par le vérificateur.

## Deliverables
- `factory_v2/verify_tr_weekly_operate.py`
- `tests/test_tr_weekly_operate_v1.py`

## Depends On
- Jalon `TR_WEEKLY_DEEP_REFRESH_V1` COMPLETE (receipts `factory_v2/receipts/weekly_observations/` existants et schéma stable)
- Moteur : build de niveau B routé sur GLM (`engine_hint: glm`) — ce build vit dans le DÉPÔT DE CONTRÔLE, donc Codex builder est exclu par le roster (jamais d'accès au control-repo)

## Objectif
Construire `factory_v2/verify_tr_weekly_operate.py` : un vérificateur indépendant, fail-closed, qui lit les receipts
`factory_v2/receipts/weekly_observations/*.json` (écrits par le superviseur, jamais par le builder) et prouve :
1. au moins deux refreshs réels (trigger_source == "scheduler", jamais "manual"),
2. espacés d'au moins `--min-gap-days` jours calendaires (défaut 6) sur `observed_at`/`trigger_scheduled_at`,
3. avec des `corpus_sha` distincts,
4. zéro intervention humaine entre les deux (aucun receipt avec trigger_source manuel dans l'intervalle).

## Contrat
- CLI : `python3 factory_v2/verify_tr_weekly_operate.py <dir_receipts> --project-repo <repo> --min-gap-days 6`
- Sorties : rc 0 = OPERATING prouvé (imprime le résumé : dates, gap, sha) ; rc 1 = UNMET typé sur stderr
  (`TR_WEEKLY_OPERATE_UNMET: <raison>` — receipts absents, gap insuffisant, sha identiques, trigger manuel) ;
  rc 2 réservé à une entrée illisible (dossier absent, JSON corrompu).
- Ne croit aucun booléen écrit par un builder ; recompute tout depuis les receipts. Aucun appel réseau, aucun modèle.
- Écrit `factory_v2/receipts/TR_WEEKLY_OPERATE_V1_RECEIPT.json` (schéma `tr-weekly-operate/v1`, champs recomputés,
  `control_sha` = HEAD) uniquement en cas de rc 0.

## Tests obligatoires (tests/test_tr_weekly_operate_v1.py, sans réseau ni modèle)
- dossier vide → rc 1 ; un seul receipt → rc 1 ; deux receipts à 2 jours → rc 1 ; deux receipts à 7 jours mais même
  corpus_sha → rc 1 ; deux receipts à 7 jours, sha distincts, scheduler → rc 0 et receipt écrit ; receipt corrompu → rc 2.
- Modèle à suivre : `factory_v2/verify_milestone_acceptance.py` (fail-closed) et le schéma des receipts existants
  sous `factory_v2/receipts/weekly_observations/`.

## Interdits
Aucune modification de ROADMAP/SPEC/STRATEGY/PROJECT_STATE, des receipts existants, ni de `factory_v2/weekly_observation.py`.
