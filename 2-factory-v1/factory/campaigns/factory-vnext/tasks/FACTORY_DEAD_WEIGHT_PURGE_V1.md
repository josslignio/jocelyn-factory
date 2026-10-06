# FACTORY_DEAD_WEIGHT_PURGE_V1 — supprimer tout ce qui est mort

## Objectif
Ordre propriétaire 22/09 04:55 Bangkok : « supprimer tout ce qui est mort, sert à rien, peut être nettoyé ». Le dépôt
versionne 15 336 fichiers ; le code vivant, ses tests, configs, juges et consignes en font ~1 000. Le reste :
`campaigns/RUN8` (10 325 fichiers, 155 Mo — preuves d'anciennes campagnes, **gel levé par le propriétaire**),
`factory_v2/receipts/verified_fixes` (2 628 reçus de vieilles réparations), `factory_v2/receipts/audit_cells` (55 bacs à
sable), anciennes campagnes `factory/campaigns/{app007,M5,e1-probe}` (~880), `projects/job-radar`, sondes `projects/f5-*`.
Le contrat exact est le docstring de `factory_v2/verify_dead_weight_purge.py` (le juge).

## À construire
1. **Archiver avant de supprimer** : créer la branche `archive/pre-purge-2026-09` sur le commit courant (rien n'est perdu,
   l'historique git garde tout, les SHA épinglés dans les reçus restent valides).
2. **Supprimer de l'arbre** (git rm, commit) tous les chemins morts listés ci-dessus — puis chasser les orphelins : tout
   fichier versionné qui n'est ni du code/test/config/doc/juge/consigne vivant, ni un reçu référencé par une roadmap ou un
   module. Cible : **≤ 2 000 fichiers versionnés**.
3. **Retirer les gardes et références à ce qui n'existe plus** : `frozen_zone_guard.py` (et ses tests) si la zone gelée
   n'existe plus ; chemins morts dans les configs (`capture_ignore.json`, `.gitignore`, `pytest.ini`), docs et README.
4. Contrat de non-régression : suite complète, banc de chaos v1+v2, benchmark apparié — verts avant et après.
5. Relecture Codex : BLOCK si un fichier référencé par une roadmap, un juge, une config vivante ou un test a été supprimé.

## Interdits
Réécrire l'historique. Supprimer un fichier référencé par ROADMAP.yaml (livrable) ou par un module vivant. Toucher aux
données produit (`products/`, dépôt Twitter Radar). Modifier STRATEGY/SPEC/ROADMAP ou les juges.

## Juge
`python3 factory_v2/verify_dead_weight_purge.py --project-repo .` → `DEAD_WEIGHT_PURGE_VERIFIED`. Niveau A, budget 10 800 s.

## Arbitrage propriétaire (22/09 08:40 Bangkok) — réponse aux deux blocages de l'essai 1
L'essai 1 (Codex, juge vert, 1 272 fichiers) s'est déclaré PARTIAL pour deux questions. Réponses, à appliquer sans redemander :
1. **Les livrables cités par une ROADMAP.yaml ne sont jamais morts** (preuve d'un jalon réglé) — même sous `research/` ou
   `.context/` : `research/deep_research/FACTORY_2026-09/**`, `research/deep_research/FACTORY_SCALE_LOCAL_2026-09/**`,
   `.context/factory-tightening-audit-2026-09-12/RESILIENCE_DRILL.md`, `factory_v2/audit/CODE_REVIEW_2026-09/**`. Les
   garder ; le juge (corrigé le 22/09) ne les compte plus comme orphelins.
2. **`verify_reshape_receipt.py` ne dépend pas de `frozen_zone_guard.py`** : `_load_frozen_zone_guard` retourne `None` si le
   fichier est absent. Ne pas le modifier ; la mention textuelle n'est pas une dépendance. Retirer `frozen_zone_guard.py`
   et ses tests comme prévu au point 3.
3. **Repartir du travail déjà fait** : la branche `candidate/FACTORY_DEAD_WEIGHT_PURGE_V1#M1.archived-1790039565_ff8b9215`
   (commit `8908657f6`, juge vert) contient la purge complète. La rebaser/cherry-picker sur HEAD, restaurer les livrables
   du point 1 depuis HEAD, relancer le juge — ne pas refaire la purge de zéro.
