# FACTORY_CORE_REWRITE_RUNTIME_V1 — le runtime advance() (4 554 + 4 modules advance/) réécrit en une boucle lisible

## Objectif
Ordre propriétaire 21/09 : la factory doit devenir **plus simple, plus légère, plus efficace** — pas l'inverse. Le code de
contrôle est passé de 66 833 lignes (13/09) à 76557 (21/09) alors que le plan d'épuration prévoyait ~15 500.
`factory_v2/runtime.py` (4 554 lignes) + `factory_v2/advance/*` portent la boucle d'opération de STRATEGY.md :
charger l'autorité → choisir le jalon → geler le contrat → compiler la mission → dispatcher → valider → régler → recharger.
Cible : **≤ 5 600 lignes pour l'ensemble, en forme lisible (aucune ligne > 200 caractères — compresser la mise en forme est refusé par le juge)** ; la seconde passe après le dédoublonnage visera plus bas (runtime.py + les 4 modules advance), une fonction par étape de la boucle,
plus aucun chemin V1/legacy, plus de doublon entre runtime.py et advance/. API publique conservée (`advance`,
`select_project`, `v2_status_summary`, événements PROJECT_STATE, contrats), tests existants verts, banc de chaos vert.

## Contrat de non-régression (le juge le rejoue, un reçu écrit à la main ne vaut rien)
- Suite complète verte (exclusions de la roadmap), banc de chaos série 1 (12/12) et série 2 (8/8) verts.
- Benchmark apparié (`factory_v2/run_matched_benchmark_3arm.py --verify-only`) accepté à HEAD.
- Poids : fichiers ciblés ≤ 5600 lignes, total `factory/ + factory_v2/` ≤ 74600 lignes.
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
`python3 factory_v2/verify_core_rewrite.py --project-repo . --targets factory_v2/runtime.py,factory_v2/advance/milestone_selection.py,factory_v2/advance/contract_and_compile.py,factory_v2/advance/mission_dispatch.py,factory_v2/advance/settlement.py --max-loc 5600 --max-total 74600` → `CORE_REWRITE_VERIFIED`. Relecture Codex (adversaire, lecture seule). Niveau A, budget 10 800 s.
