# FACTORY_REPO_HYGIENE_V1 — un dépôt propre : le code d'un côté, la comptabilité de l'autre

## Objectif
Ordre propriétaire 21/09 : « commencer à tout clean dans la factory, code épuré et relu, plus le bordel, que tout soit
nickel et que ça tourne nickel ». Le 21/09 le disque a été rempli par la factory elle-même (114 861 dossiers temporaires de
tests, 51 Go ; 32 worktrees archivés, 12 Go) et la promotion d'un jalon a échoué faute d'espace. Le dépôt versionne
17 773 fichiers dont 3 233 de comptabilité runtime (reçus, preuves, registres), des checkpoints de mission à la racine
(PLAN.md, REPORT.json, .factory_worktree_meta.json) et 382 fichiers d'archives mortes. Chaque candidat de mission traîne
cette comptabilité → fusions en conflit (5 fusions manuelles le 20/09). Le contrat exact est le docstring de
`factory_v2/verify_repo_hygiene.py` (le juge).

## À construire
1. Checkpoints de mission hors de la branche dorée — mais PAS hors git (leçon de l'essai 1 : le dispatcher lit
   REPORT.json AU SHA du candidat, fail-closed ; un candidat sans REPORT.json committé est refusé `REPORT_JSON_ABSENT`).
   Donc : PLAN.md / REPORT.json / .factory_worktree_meta.json restent committés dans la branche candidate, et c'est
   `_merge_candidate` qui les retire du résultat de fusion (jamais présents sur la branche dorée) ; retirer les copies
   actuellement suivies à la racine du dépôt de contrôle (`git rm --cached`, fichiers laissés sur disque).
2. Comptabilité runtime hors git : `projects/*/evidence/`, `evidence/`, `factory_v2/receipts/{recovery_allocations,
   validation_jobs}/`, `weekly_observations/*.json` retirés de l'index, ignorés ; les reçus de jalon nommés comme
   livrables dans ROADMAP.yaml restent versionnés. Le daemon ne fait plus de commit « état factory (auto) ».
3. Archives mortes : 382 fichiers sous `**/archive/` (hors `campaigns/RUN8/**`, zone gelée) supprimés du dépôt ;
   l'historique git les conserve.
4. Tests et bancs propres : `tempfile.TemporaryDirectory()` / fixtures `tmp_path` partout, plus de `mkdtemp` sans
   nettoyage ; le juge compte, avant/après les bancs de chaos, les dossiers temporaires aux préfixes de la factory
   (`chaos_*`, `factory_*`, `canary*`, `v2_dispatch_*`, `s017_*`, `scale2_*`, `supv_*`, `intra_par*`, `ptf_*`) : 0 fuite —
   les missions voisines n'entrent pas dans le compte.
5. Purge automatique : `prune_stale_worktrees` retire les `.archived-*` de plus de 24 h à chaque cycle du daemon ;
   `factory_v2/disk_guard.py::ensure_free_gb(path, min_gb=10)` appelé avant tout `git worktree add` (dispatcher,
   worktree_manager, promotion) et refuse avec l'événement typé `DISK_SPACE_LOW` (jamais un crash « No space left »).
6. Relecture de propreté : Codex relit le diff — BLOCK si un fichier utile a été retiré ou si un lecteur de checkpoint
   casse.

## Interdits
Supprimer des données du disque (seulement de l'index git). Toucher `campaigns/RUN8/**`. Réécrire l'historique.
Modifier STRATEGY/SPEC/ROADMAP ou les juges.

## Juge
`python3 factory_v2/verify_repo_hygiene.py --project-repo .` → `REPO_HYGIENE_VERIFIED`. Niveau A, budget 10 800 s.
