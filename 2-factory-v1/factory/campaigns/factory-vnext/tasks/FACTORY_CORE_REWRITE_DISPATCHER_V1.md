# FACTORY_CORE_REWRITE_DISPATCHER_V1 — le dispatcher (3 723 lignes) réécrit autour d'un noyau minimal

## Objectif
Ordre propriétaire 21/09 : la factory doit devenir **plus simple, plus légère, plus efficace** — pas l'inverse. Le code de
contrôle est passé de 66 833 lignes (13/09) à 76557 (21/09) alors que le plan d'épuration prévoyait ~15 500.
`factory/bin/dispatcher_v3.py` fait 3 723 lignes pour : préparer un worktree, lancer UN moteur avec la consigne,
capturer le candidat, lancer les tests, répéter build→test→repair dans un budget, écrire un reçu. La décision F4 de la
comparaison à l'état de l'art (`factory_v2/audit/FACTORY_VS_UPSTREAM_MATRIX.json`, ligne dispatcher) : « adopter la forme
boucle-de-messages de DefaultAgent, extraire les politiques de retry, cœur < 400 lignes ». Cible ici : **≤ 1 500 lignes**
en gardant l'API publique (`dispatch_v3`, `_run_in_worktree`, format du reçu, `_merge_candidate`, capture gate,
`prune_stale_worktrees`) et TOUS les tests existants verts. Méthode : écrire le noyau minimal à côté, faire passer les
tests, basculer, supprimer l'ancien code — jamais un `dispatcher_v4.py` qui coexiste.

## Contrat de non-régression (le juge le rejoue, un reçu écrit à la main ne vaut rien)
- Suite complète verte (exclusions de la roadmap), banc de chaos série 1 (12/12) et série 2 (8/8) verts.
- Benchmark apparié (`factory_v2/run_matched_benchmark_3arm.py --verify-only`) accepté à HEAD.
- Poids : fichiers ciblés ≤ 1500 lignes, total `factory/ + factory_v2/` ≤ 74500 lignes.
- Aucun test existant affaibli ou supprimé sans remplacement équivalent (le relecteur le vérifie sur le diff).

## Méthode imposée
1. Lire d'abord `projects/factory/STRATEGY.md` (boucle d'opération, invariants) et la décision correspondante dans
   `factory_v2/audit/FACTORY_VS_UPSTREAM_MATRIX.json` / `FACTORY_CONVERGENCE_LEDGER.json`.
2. Écrire le noyau minimal à côté de l'ancien, faire passer les tests dessus, basculer, **supprimer** l'ancien code dans
   la même mission. Un lot qui ajoute du code sans en retirer est refusé.
3. Commits fréquents (le worktree est conservé et rebasé entre les essais) ; REPORT.json honnête (PARTIAL si partiel).

## Interdits
Modifier STRATEGY/SPEC/ROADMAP, les juges, ou un test pour le faire passer. Lancer un vrai moteur dans un test.
Créer un fichier `*_v4.py` / `*_new.py` qui coexiste avec l'ancien. Zone gelée `campaigns/RUN8/**`.

## Juge
`python3 factory_v2/verify_core_rewrite.py --project-repo . --targets factory/bin/dispatcher_v3.py --max-loc 1500 --max-total 74500` → `CORE_REWRITE_VERIFIED`. Relecture Codex (adversaire, lecture seule). Niveau A, budget 10 800 s.
