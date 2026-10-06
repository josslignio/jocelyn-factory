# FACTORY_CODE_REVIEW_SWEEP_V1 — revue de qualité du code, fichier par fichier

## Objectif
Relire **tout** le code de contrôle de la Factory (≈ 62 000 lignes, 9 lots) comme un relecteur de
qualité exigeant, et produire des constats **vérifiables** (fichier, ligne, citation) : bugs, code mort,
doublons, conditions de course, gestion d'erreur, performance, complexité, trous de tests, doc périmée.
(Ceci est une revue de qualité logicielle interne sur notre propre dépôt — pas une recherche de failles.) Chaque
lot = une mission ; la dernière mission synthétise et propose les jalons de réparation.

Le contrat exact du fichier de sortie est le docstring de `factory_v2/verify_code_review_sweep.py`
(le juge). Le lire avant d'écrire.

## Missions M1..M9 — un lot chacune
Les 9 lots sont les clés de `factory/config/code_review_lots_2026-09.json`. **Ton lot est celui nommé
dans le `test_cmd` de ton étape** (`verify_code_review_sweep.py <lot>`) et dans sa description — pas
un numéro déduit de M<n>. Vérifie-le avant de commencer.

Pour TA mission (le numéro est dans ton id de mission `#M<n>`) :
1. Identifier ton lot (voir ci-dessus) et charger sa liste de fichiers depuis `factory/config/code_review_lots_2026-09.json`.
2. **Lire chaque fichier en entier** (pas de survol : `lines` doit être à ±2 du `wc -l`, le juge le vérifie).
3. Pour chaque fichier : `verdict` CLEAN ou FINDINGS + `summary` (≥ 20 caractères, ce que fait le fichier
   et son état). Pour chaque constat : `file`, `line`, `evidence` = **citation textuelle copiée** de la ligne
   (ou ±3 lignes), `severity`, `category`, `why`, `recommendation`, `fix_effort`. Ids `L<lot>-NNN` uniques.
4. Écrire `factory_v2/audit/CODE_REVIEW_2026-09/<lot>.json` avec `reviewed_sha` = `git rev-parse HEAD`.
5. Lancer le juge : `python3 factory_v2/verify_code_review_sweep.py <lot> --project-repo .` → VERIFIED.
6. Committer ce seul fichier. Outcome IMPLEMENTED dès que le juge est vert pour TON lot.

Ce qu'on cherche en priorité (défauts mesurés cette semaine) : états bloqués que personne ne transforme
en action (boucles de polling), sauts d'appel modèle abusifs, chemins V1 morts encore importés,
doublons de concepts (4 « prochain travail », 3 « gate »), exceptions avalées (`except Exception: pass`),
timeouts en dur, chaînes de fallback incomplètes, tests absents sur du code critique.

## Interdits
Modifier du code, la roadmap, l'état. Inventer une citation (le juge la cherche à ±3 lignes). Déclarer
un fichier CLEAN sans l'avoir lu en entier.

## M10 — synthèse (GLM)
Lire les 9 lots. Écrire `factory_v2/audit/CODE_REVIEW_2026-09/SYNTHESIS.json` :
`{"schema":"code-review-synthesis/v1","totals":{...},"top":[{"rank":1,"finding_ids":[...],"title":...,
"metric":...,"milestone_proposal":{"id":...,"goal":...,"deliverables":[...],"judge":...}}...]}`
(≥ 10 items classés, chaque id existant dans un lot) et `SYNTHESIS.md` en français simple (une ligne
par item : quoi, pourquoi, gain, coût). Juge : `python3 factory_v2/verify_code_review_sweep.py --synthesis --project-repo .`.
Niveau A. Budget 5 400 s par mission.
