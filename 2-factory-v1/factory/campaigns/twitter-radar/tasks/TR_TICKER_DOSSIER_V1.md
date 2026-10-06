# TR_TICKER_DOSSIER_V1


Derived task brief. NOT canonical intent — voir projects/twitter-radar/ROADMAP.yaml et projects/twitter-radar/SPEC.md
(section « OWNER-APPROVED PRODUCT REQUIREMENTS — 2026-09-13 »). Ne modifie ni ROADMAP.yaml, ni SPEC.md, ni STRATEGY.md,
ni PROJECT_STATE.json. Depot produit : /Users/jocelyngrosjean/conductor/workspaces/twitter-radar/twitter-radar-foundation-v2
(cwd). Pas de `git stash` (utilise `git show HEAD:<path>`). Commits atomiques ; COMMITTE tes livrables AVANT d'executer la
commande d'acceptance (les verificateurs lisent HEAD). Le JUGE d'acceptance vit dans le DEPOT DE CONTROLE (factory_v2/tr_presentation_verifiers.py, ecrit par l'operateur,
hors de ta portee) : il relit tes artefacts a HEAD et RECALCULE (couverture depuis le CSV, rendements depuis les calls
que tu cites avec leurs clotures, variations de prix depuis le CSV, ordre de tri, texte visible du site). Lance-le
toi-meme avant de conclure : `PYTHONPATH=/Users/jocelyngrosjean/conductor/workspaces/Jocelyn-factory-control/factory-foundation-v2 python3 -m factory_v2.tr_presentation_verifiers TR_TICKER_DOSSIER_V1 --project-repo .`
Tes propres tests (tests/test_<producteur>.py) restent bienvenus mais ne sont pas le juge.
REPORT.json : outcome `IMPLEMENTED` uniquement si la commande d'acceptance sort rc=0 et tes tests passent dans cette
session ; sinon PARTIAL avec la raison exacte. Les tests `*web*` du depot qui ouvrent un port ne sont pas requis ici.

## Outcome
Dossier par ticker : type, societe, secteur/sous-industrie IA, description en clair de l'activite et de l'action, capitalisation et valorisation, variation de prix 2y/1y/6m/3m/2m/1m/1w, journal de catalyseurs date et source (posts KOL + flux d'actualites nomme), explication « pourquoi ca a bouge » et signaux de suite (momentum, extension 52 semaines, tendance du consensus KOL, catalyseurs dates a venir) — jamais une prediction.

## Deliverables
- scripts/tr_ticker_dossier.py
- research/tr_ticker_dossier.json

## Acceptance (copie de la roadmap — juge : depot de controle, voir ci-dessus)
- research/tr_ticker_dossier.json (schema tr-ticker-dossier/v1) : as_of, news_provider (un seul fournisseur nomme), coverage_note (perimetre observable : depuis quelle date, quels tickers sans actu), tickers{t: asset_type, company, sector, ai_subindustry, business_summary, what_kols_call (avec tweet_ids), market_cap, valuation, price_change{2y..1w depuis le CSV, INSUFFICIENT_DATA si historique court}, catalysts[{date, kind KOL_POST|NEWS|EARNINGS|EVENT, title, source, url|tweet_id, uncertainty pour toute date future}], why_it_moved[{window (chacune des 7 fenetres calculables), move_pct, coinciding_catalysts[]}], continuation_signals{momentum_state, pct_of_52w_high, kol_consensus_trend, upcoming_catalysts[], typed_output, reasons[]}}
- Le flux d'actualites est UN fournisseur nomme (yfinance news ou equivalent public) ; chaque catalyseur porte source, date et lien ; zero catalyseur invente ; un ticker sans actu a catalysts=[] et why_it_moved base sur les posts KOL seulement
- Aucun champ ne predit un prix ; continuation_signals.typed_output est l'une des sorties honnetes de la SPEC (RESEARCH_NOW|WATCH|TOO_EXTENDED|LOW_CONFIDENCE|AVOID|INSUFFICIENT_DATA) avec raisons
- Juge independant : factory_v2/tr_presentation_verifiers.py TR_TICKER_DOSSIER_V1 (depot de controle) : un dossier par ticker de la table, price_change recalcule depuis le CSV a HEAD pour CHAQUE ticker et fenetre calculable a 1e-6 et INSUFFICIENT_DATA impose sinon, chaque NEWS avec url+publisher+date, chaque KOL_POST avec un tweet_id present dans le corpus committe, tout catalyseur futur avec source+uncertainty, aucun champ de prediction de prix, typed_output dans la liste SPEC avec reasons ; rc=0 seulement committe a HEAD

## Notes d'execution
- Base : research/tr_ticker_table.json (enrichment yfinance deja present : company_name, description, market_cap, momentum,
  etc.) et le corpus data/corpus/<handle>/posts.jsonl pour what_kols_call et les catalyseurs KOL_POST (tweet_id reel).
- Flux d'actualites : yfinance `Ticker(t).news` (publisher, title, link, providerPublishTime) — UN seul fournisseur nomme
  dans le JSON (`news_provider`). Si vide : catalysts sans NEWS, jamais invente. Le reseau est disponible dans ta session.
- why_it_moved : pour chaque fenetre {1w,1m,3m}, move_pct depuis le CSV et les catalyseurs dont la date tombe dans la
  fenetre ; c'est une explication factuelle, pas une causalite affirmee (formulation « coincide avec »).
- continuation_signals : momentum_state (deja calcule), pct_of_52w_high, kol_consensus_trend (nombre de comptes ayant un
  episode ouvert sur 30/60/90 jours : rising/flat/fading), upcoming_catalysts (earnings date via yfinance calendar si
  disponible), typed_output selon les regles de la SPEC. Aucune prediction de prix.

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
