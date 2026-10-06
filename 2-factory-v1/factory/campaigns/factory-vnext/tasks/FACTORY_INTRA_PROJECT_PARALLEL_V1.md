# FACTORY_INTRA_PROJECT_PARALLEL_V1 — deux missions d'un même projet en parallèle

## Objectif
Aujourd'hui `advance()` tient le verrou projet pendant TOUT le lancement moteur : un second `advance()` répond PROJECT_BUSY. Libérer le verrou pendant l'exécution du moteur (la mission est protégée par son bail/lease + worktree dédié + `is_worker_alive`), le reprendre pour le settlement. Avec `_parallel_alternate` (déjà en place) deux jalons à livrables disjoints tournent alors ensemble.

## À construire
1. `factory_v2/advance/mission_dispatch.py` : autour de `runner.execute(mission)`, `release_project_lock_for_launch` (context manager) — état sauvegardé avant, verrou libéré pendant, repris après ; le bail reste le seul propriétaire de la mission.
2. Sécurité : deux missions ne peuvent jamais viser le même worktree (NO_TWO_WRITERS) ni des livrables qui se chevauchent (`_parallel_alternate`) ; le merge/settlement se fait sous verrou.
3. Tests : deux threads `advance()` sur un projet à deux jalons disjoints → deux missions distinctes, aucune PROJECT_BUSY ; livrables chevauchants → la seconde attend.

## Juge épinglé
`intra-project-parallel` : câblage + drill réel (deux advance() concurrents → jalons A et B, pas de PROJECT_BUSY).
