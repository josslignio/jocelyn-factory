# TR_KOL_HORIZONS_V1


Derived task brief. NOT canonical intent — voir projects/twitter-radar/ROADMAP.yaml et projects/twitter-radar/SPEC.md
(section « OWNER-APPROVED PRODUCT REQUIREMENTS — 2026-09-13 »). Ne modifie ni ROADMAP.yaml, ni SPEC.md, ni STRATEGY.md,
ni PROJECT_STATE.json. Depot produit : /Users/jocelyngrosjean/conductor/workspaces/twitter-radar/twitter-radar-foundation-v2
(cwd). Pas de `git stash` (utilise `git show HEAD:<path>`). Commits atomiques ; COMMITTE tes livrables AVANT d'executer la
commande d'acceptance (les verificateurs lisent HEAD). Le JUGE d'acceptance vit dans le DEPOT DE CONTROLE (factory_v2/tr_presentation_verifiers.py, ecrit par l'operateur,
hors de ta portee) : il relit tes artefacts a HEAD et RECALCULE (couverture depuis le CSV, rendements depuis les calls
que tu cites avec leurs clotures, variations de prix depuis le CSV, ordre de tri, texte visible du site). Lance-le
toi-meme avant de conclure : `PYTHONPATH=/Users/jocelyngrosjean/conductor/workspaces/Jocelyn-factory-control/factory-foundation-v2 python3 -m factory_v2.tr_presentation_verifiers TR_KOL_HORIZONS_V1 --project-repo .`
Tes propres tests (tests/test_<producteur>.py) restent bienvenus mais ne sont pas le juge.
REPORT.json : outcome `IMPLEMENTED` uniquement si la commande d'acceptance sort rc=0 et tes tests passent dans cette
session ; sinon PARTIAL avec la raison exacte. Les tests `*web*` du depot qui ouvrent un port ne sont pas requis ici.

## Outcome
Rendement LONG realise par compte sur fenetres calendaires 2y/1y/6m/3m/2m/1m/1w (portefeuille equipondere des calls LONG qualifies ouverts dans la fenetre, brut et ajuste marche), INSUFFICIENT_DATA sous 3 calls, integre au classement KOL trie par score.

## Deliverables
- scripts/tr_kol_horizons.py
- research/tr_kol_horizons.json

## Acceptance (copie de la roadmap — juge : depot de controle, voir ci-dessus)
- research/tr_kol_horizons.json (schema tr-kol-horizons/v1) : as_of, frozen_rules, ranking_order (handles tries par final_score decroissant), accounts{handle: final_score, good_calls, failed_calls, windows{2y,1y,6m,3m,2m,1m,1w: status OK|INSUFFICIENT_DATA (< 3 calls evaluables), n_calls, n_evaluable, return_raw, return_market_adjusted, hit_rate, best_call, worst_call, calls[{ticker, tweet_id, tweet_date, evaluable, entry_date, entry_close, exit_date, exit_close, return}]}} — jamais 0 a la place d absent
- Regle figee et versionnee (frozen_rules dans le JSON) : entree = cloture du jour du premier tweet qualifie (ou jour ouvre suivant), sortie = min(fin de fenetre, exit explicite du KOL enregistre dans le ledger), equipondere, pas de look-ahead ; toute approximation calendaire etiquetee
- ranking_order est l ordre canonique du classement KOL trie par score final decroissant ; chaque ligne de la table KOL affichee porte good_calls, failed_calls, hit_rate et les 7 fenetres
- Juge independant : factory_v2/tr_presentation_verifiers.py TR_KOL_HORIZONS_V1 (depot de controle) recalcule TOUTES les fenetres OK de TOUS les comptes depuis les calls cites : entry_close = cloture du CSV le premier jour >= tweet_date (pas de look-ahead), exit_close = cloture du CSV le dernier jour <= exit_date, return_raw = moyenne equiponderee a 1e-6, n_evaluable exact, INSUFFICIENT_DATA impossible avec >= 3 calls evaluables, ranking_order strictement decroissant ; rc=0 seulement committe a HEAD

## Notes d'execution
- Ledger d'episodes : research/tr_call_ledger.json + data/t3 / research/X104_OUTCOME_LEDGER.json (calls LONG qualifies par
  compte, avec tweet_id et date) — lis d'abord docs/SCORING.md et docs/KOL_SCHEMA.md pour les champs exacts.
- Prix : data/market/daily_prices.csv (TR_MARKET_HISTORY_V1). Benchmark marche = SPY (present dans le CSV ; sinon status
  INSUFFICIENT_DATA pour l'ajustement, jamais 0).
- Fenetres calendaires : 2y=730j, 1y=365j, 6m=182j, 3m=91j, 2m=61j, 1m=30j, 1w=7j, ancrees sur as_of (date du run).
- Ecris frozen_rules dans le JSON (entree/sortie/equiponderation/absence de look-ahead/approximation calendaire).
- Le classement KOL doit etre relu trie par score decroissant : si research/tr_kol_ranking.json n'est pas trie, ajoute un
  champ `ranking_order` (liste de handles) dans research/tr_kol_horizons.json et documente-le.

## Non-goals
- Aucune modification des verificateurs existants (factory_v2/verify_tr_*.py deja livres) ni des fichiers d'autorite.
- Aucune valeur fabriquee : absent reste absent (type), jamais 0.
- Pas de trading, pas de prediction de prix.

## Raisons PARTIAL typees (obligatoires dans REPORT.json.partial_reason)
PROVIDER_UNAVAILABLE | PROVIDER_RATE_LIMITED | NETWORK_UNAVAILABLE | TOOL_MISSING | DATA_INSUFFICIENT | BUDGET_EXHAUSTED —
avec le detail exact (ticker/etape) ; toute autre formulation libre est refusee.

## Stop conditions
- Fournisseur reseau indisponible ou quota : livrer ce qui est observe, coverage/gaps honnetes, outcome PARTIAL avec la liste
  exacte de ce qui manque.
