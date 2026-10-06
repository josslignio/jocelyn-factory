# TR_MARKET_HISTORY_V1


Derived task brief. NOT canonical intent — voir projects/twitter-radar/ROADMAP.yaml et projects/twitter-radar/SPEC.md
(section « OWNER-APPROVED PRODUCT REQUIREMENTS — 2026-09-13 »). Ne modifie ni ROADMAP.yaml, ni SPEC.md, ni STRATEGY.md,
ni PROJECT_STATE.json. Depot produit : /Users/jocelyngrosjean/conductor/workspaces/twitter-radar/twitter-radar-foundation-v2
(cwd). Pas de `git stash` (utilise `git show HEAD:<path>`). Commits atomiques ; COMMITTE tes livrables AVANT d'executer la
commande d'acceptance (les verificateurs lisent HEAD). Le JUGE d'acceptance vit dans le DEPOT DE CONTROLE (factory_v2/tr_presentation_verifiers.py, ecrit par l'operateur,
hors de ta portee) : il relit tes artefacts a HEAD et RECALCULE (couverture depuis le CSV, rendements depuis les calls
que tu cites avec leurs clotures, variations de prix depuis le CSV, ordre de tri, texte visible du site). Lance-le
toi-meme avant de conclure : `PYTHONPATH=/Users/jocelyngrosjean/conductor/workspaces/Jocelyn-factory-control/factory-foundation-v2 python3 -m factory_v2.tr_presentation_verifiers TR_MARKET_HISTORY_V1 --project-repo .`
Tes propres tests (tests/test_<producteur>.py) restent bienvenus mais ne sont pas le juge.
REPORT.json : outcome `IMPLEMENTED` uniquement si la commande d'acceptance sort rc=0 et tes tests passent dans cette
session ; sinon PARTIAL avec la raison exacte. Les tests `*web*` du depot qui ouvrent un port ne sont pas requis ici.

## Outcome
Historique de prix quotidien >= 2 ans (ou depuis cotation) pour CHAQUE ticker de research/tr_ticker_table.json, avec provenance, politique corporate actions et lacunes declarees ; couverture mesuree et publiee.

## Deliverables
- scripts/tr_market_history.py
- data/market/daily_prices.csv
- research/tr_market_history_coverage.json

## Acceptance (copie de la roadmap — juge : depot de controle, voir ci-dessus)
- scripts/tr_market_history.py (yfinance, reprenable, borne : --max-tickers, --sleep, curseurs par ticker) remplit data/market/daily_prices.csv (ticker,date,close,adj_close,volume,source,retrieved_at) sur >= 730 jours calendaires ou depuis la premiere cotation, pour tous les tickers de research/tr_ticker_table.json (crypto en -USD, ADR tels quels)
- research/tr_market_history_coverage.json (schema tr-market-history-coverage/v1) liste par ticker : first_date, last_date, rows, gaps (dates manquantes > 5 jours ouvres), status OK|PARTIAL|NOT_FOUND|PROVIDER_ERROR, et un resume : covered_2y_count, total, missing (liste) — jamais un ticker absent passe sous silence
- Couverture : >= 90 % des tickers avec first_date <= as_of - 700 jours OU status NOT_FOUND documente (delisting, IPO recente avec listing_date cite) ; les 10 % restants sont listes dans missing avec la raison
- Juge independant : factory_v2/tr_presentation_verifiers.py TR_MARKET_HISTORY_V1 (DEPOT DE CONTROLE, ecrit par l operateur, jamais par le builder) recalcule la couverture depuis le CSV committe a HEAD (first_date, rows par ticker, covered_2y_count, missing) et exige l egalite avec research/tr_market_history_coverage.json ; colonnes adj_close, source, retrieved_at obligatoires ; corporate_action_policy {close, adj_close, splits, dividends, delistings} versionnee dans le JSON ; rc=0 seulement committe a HEAD
- Aucune valeur fabriquee : un jour sans cotation reste absent ; source et retrieved_at sur chaque ligne

## Notes d'execution
- Point de depart : scripts/market_data_fetcher.py (yfinance, format ticker,date,close) et data/market/daily_prices.csv
  (48 tickers, avril→septembre 2026). Etends plutot que recreer : nouveau script scripts/tr_market_history.py qui lit la
  liste des tickers dans research/tr_ticker_table.json (cle rows[].ticker), fetch yfinance `history(period='2y')` par
  ticker avec curseur par ticker (data/market/history_state/<ticker>.json), --sleep entre requetes, --max-tickers, reprise.
- Crypto : BTC→BTC-USD, ETH→ETH-USD ; ADR/etrangers tels quels ; NOT_FOUND documente (pas de retry infini).
- Le reseau est disponible dans TA session (builder Claude) ; lance le fetch reel, verifie la couverture, committe CSV +
  coverage JSON + curseurs. Budget : le fetch de ~250 tickers avec sleep 0.5 s prend < 10 min.
- as_of = date du run ; regle « 2 ans » = first_date <= as_of - 700 jours OU listing_date documentee.

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
