# FACTORY_SIMPLIFY_S4 — retirer la branche --legacy-v1 de factoryd et backlog_refiller

Derived task brief. NOT canonical intent — voir projects/factory/ROADMAP.yaml. Ne modifie ni ROADMAP.yaml,
ni PROJECT_STATE.json, ni factory_v2/audit/SIMPLIFICATION_BATCHES.json, ni factory_v2/verify_simplification_batch.py.

## Outcome
`factory/lib/backlog_refiller.py` n'existe plus a HEAD ; `factory/bin/factoryd.py` et `factory/bin/factoryd_watch.py`
n'importent plus `backlog_refiller`, n'exposent plus le flag `--legacy-v1` ni la branche superviseur V1
(grep -n 'legacy' factory/bin/factoryd.py pour la localiser) ; le chemin V2 (portfolio, select_project, _advance)
est inchange en comportement ; suite sure verte ; `python3 factory_v2/verify_simplification_batch.py S4 --project-repo .`
rc=0 et ecrit `factory_v2/receipts/SIMPLIFICATION_S4_RECEIPT.json` (seul livrable declare).

## Prerequis
- Lot S2 regle (le verificateur exige le recu SIMPLIFICATION_S2_RECEIPT.json).

## Non-goals
- Aucun changement de comportement V2 ; aucun nouveau flag ; pas de `git stash` (`git show HEAD:<path>`).
- Ne pas toucher au daemon en cours d'execution (le worktree de mission est isole).

## Verification
1. `git grep -n 'backlog_refiller\|legacy-v1\|legacy_v1' -- . ':!archive' ':!.context'` = vide (hors manifeste/recus).
2. `python3 factory/bin/factoryd.py --help` fonctionne et ne mentionne plus legacy-v1.
3. Suite sure (voir ROADMAP test_cmd) puis `python3 factory_v2/verify_simplification_batch.py S4 --project-repo .` rc=0.

## Stop conditions
- Si la branche V1 est entrelacee avec du code V2 vivant → outcome PARTIAL avec la liste exacte des fonctions.
