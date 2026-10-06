# ADAPTIVE_ROUTING_AGGREGATOR_BUILT — agrégateur de RunRecords pour le routage (niveau B, mécanique)

## Outcome
OUTCOME: `factory_v2/routing_aggregator.py` existe et `python3 -m pytest tests/test_adaptive_routing_aggregator.py -q` passe. Aucun modèle ni réseau appelé. Aucune bascule du routage production dans ce jalon (construction seule).

## Deliverables
- `factory_v2/routing_aggregator.py` : lit `factory_v2/receipts/runrecords.jsonl` (paires PREDICTION/OBSERVATION) et `projects/*/evidence/attempt_accounts.jsonl`, et écrit `routing_stats/routing_summary.json` : par (engine, spec_level, task_kind) → lancements, succès validés (dénominateur `independently_validated`, jamais les claims du builder), taux, tokens et secondes médians, dernière observation, couverture de mesure.
- `tests/test_adaptive_routing_aggregator.py` : jeux de records synthétiques dans un tmpdir ; cas vide → summary vide sans erreur ; lignes torn/corrompues ignorées et comptées dans `unreadable` ; aucun record de projet de test (project ∉ portfolio) dans les stats ; recomputation déterministe (même entrée → même sortie).

## Depends On
- `RUN_RECORDS_MINIMUM_WIRED` COMPLETE (format des records stable : `runrecords.py`, `engine_stats()`).
- Moteur : build de niveau B (`engine_hint: glm`) ; build dans le dépôt de contrôle → Codex builder exclu.

## Objectif
Rendre le routage OPTIMISABLE par les données avant toute bascule : le jalon ADAPTIVE_ROUTING consommera ce summary après les benchmarks. Réutiliser `factory_v2/runrecords.py::engine_stats` si possible (ne pas dupliquer la logique) ; sortie append-safe (écriture atomique tmp+rename).

## Interdits
Aucune modification de `model_router.py`, `escalation_ladder.py`, ROADMAP/SPEC/STRATEGY, PROJECT_STATE, receipts existants. Pas de bascule de politique de routage.
