# TR_MISSING_ACCOUNTS_RECOVERY_V1 — les 11 comptes sans tweets

## Objectif
Chaque compte de l'univers (research/tr_kol_universe.json, 25 comptes) a soit un corpus réel (data/corpus/<handle>/posts.jsonl, ≥ 50 posts), soit une cause typée et datée dans research/tr_missing_accounts.json (compte suspendu / privé / renommé / refus fournisseur / quota twscrape), avec le nombre de tentatives. Jamais un trou silencieux.

## À construire
1. `scripts/tr_missing_accounts.py` : recense les comptes sans corpus, tente une collecte bornée via twscrape (réutiliser scripts/tr_historical_backfill.py, --accounts <handle>, budget 30 min/compte, rate budget partagé), consigne le résultat ; les échecs deviennent des causes typées (`cause`, `observed_at`, `attempts`, `provider_message`).
2. Résumable : relancer ne refait pas les comptes déjà collectés ; les causes sont réévaluées après 7 jours.
3. Tests hermétiques (fetch mocké : succès, suspendu, privé).

## Juge indépendant
`PYTHONPATH="${FACTORY_CONTROL_REPO}" python3 -m factory_v2.tr_presentation_verifiers TR_MISSING_ACCOUNTS_RECOVERY_V1 --project-repo .`
Niveau B (GLM builder, réseau twscrape autorisé), budget 7200 s.
