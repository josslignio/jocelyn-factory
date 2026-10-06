# TR_PRESENTATION_V1


Derived task brief. NOT canonical intent — voir projects/twitter-radar/ROADMAP.yaml et projects/twitter-radar/SPEC.md
(section « OWNER-APPROVED PRODUCT REQUIREMENTS — 2026-09-13 »). Ne modifie ni ROADMAP.yaml, ni SPEC.md, ni STRATEGY.md,
ni PROJECT_STATE.json. Depot produit : /Users/jocelyngrosjean/conductor/workspaces/twitter-radar/twitter-radar-foundation-v2
(cwd). Pas de `git stash` (utilise `git show HEAD:<path>`). Commits atomiques ; COMMITTE tes livrables AVANT d'executer la
commande d'acceptance (les verificateurs lisent HEAD). Le JUGE d'acceptance vit dans le DEPOT DE CONTROLE (factory_v2/tr_presentation_verifiers.py, ecrit par l'operateur,
hors de ta portee) : il relit tes artefacts a HEAD et RECALCULE (couverture depuis le CSV, rendements depuis les calls
que tu cites avec leurs clotures, variations de prix depuis le CSV, ordre de tri, texte visible du site). Lance-le
toi-meme avant de conclure : `PYTHONPATH=/Users/jocelyngrosjean/conductor/workspaces/Jocelyn-factory-control/factory-foundation-v2 python3 -m factory_v2.tr_presentation_verifiers TR_PRESENTATION_V1 --project-repo .`
Tes propres tests (tests/test_<producteur>.py) restent bienvenus mais ne sont pas le juge.
REPORT.json : outcome `IMPLEMENTED` uniquement si la commande d'acceptance sort rc=0 et tes tests passent dans cette
session ; sinon PARTIAL avec la raison exacte. Les tests `*web*` du depot qui ouvrent un port ne sont pas requis ici.

## Outcome
Surfaces produit lisibles : page d'accueil = brief hebdo (nouveaux tickers/comptes/calls, mouvements de classement, pourquoi), page CV par compte, page dossier par ticker ; tables triees par score, filtrables ; typographie et couleurs coherentes ; aucun JSON brut ni nom d'etat interne visible ; jeu de captures date livre ; publication statique.

## Deliverables
- app/render.py
- app/export_static.py
- app/static/theme.css
- docs/PRESENTATION_SCREENSHOTS/README.md

## Acceptance (copie de la roadmap — juge : depot de controle, voir ci-dessus)
- app/export_static.py --out-dir <dir> genere : index.html (brief hebdo : nouveaux tickers, nouveaux comptes, nouveaux calls, top mouvements de classement avec raison), kols.html trie par score decroissant avec les fenetres 2y..1w, kols_<handle>.html (CV : identite, expertise, track record par fenetre, meilleurs/pires calls avec liens vers les posts, courbe de performance SVG, decomposition du score), tickers.html trie par score d'opportunite avec price_change 2y..1w, tickers_<t>.html (dossier : snapshot, timeline pourquoi-ca-a-bouge, catalyseurs dates, consensus KOL, prix par horizon, risques, decomposition du score)
- Aucune page ne contient de JSON brut ni de nom d'etat interne en texte visible (liste interdite versionnee dans le verificateur : INSUFFICIENT_EVIDENCE_PENDING_*, WIRED_TO_LEDGER, NOT_COLLECTED_YET_*, OOS_NOT_EVALUATED_V1, EARLY_EVIDENCE brut) — ces codes ne peuvent apparaitre que dans des attributs title/data-* ou un panneau preuves replie
- docs/PRESENTATION_SCREENSHOTS/ contient >= 6 captures PNG datees (accueil, table KOL, CV, table tickers, dossier, mobile) generees par un navigateur headless apres la derniere modification de app/ (fraicheur verifiee par l'ordre des commits, pas par le nom), et un README les listant avec le SHA
- Tables triables et filtrables cote client (JS sans dependance reseau) ; les pages restent lisibles hors ligne (aucun CDN)
- Juge independant : factory_v2/tr_presentation_verifiers.py TR_PRESENTATION_V1 (depot de controle) regenere le site depuis HEAD dans un repertoire temporaire (app/export_static.py --out-dir), verifie index/kols/tickers + une page par compte et par ticker, l ordre de la table KOL = ranking_order, les 7 fenetres affichees, aucun code interne ni JSON brut dans le texte visible, >= 6 captures PNG nommees avec le sha court de HEAD et un README les citant ; rc=0 seulement committe a HEAD
- La qualite visuelle est validee par le proprietaire sur le jeu de captures (decision enregistree dans le dossier de preuve du jalon), jamais auto-declaree par le builder

## Notes d'execution
- Base : app/render.py, app/export_static.py, app/store.py (lecture de research/*.json). Ajoute les lectures de
  research/tr_kol_horizons.json et research/tr_ticker_dossier.json.
- Un seul theme CSS (app/static/theme.css) : palette sobre, typographie systeme, tableaux denses lisibles, badges pour
  risque/lifecycle, sparklines SVG inline pour les courbes ; tri/filtre en JS vanilla embarque (aucun CDN).
- Textes visibles en francais simple ; les codes internes vont dans title= ou un panneau « preuves » replie.
- Captures (outil PINNE) : `python3 -m pip install --user playwright==1.47.0 && python3 -m playwright install chromium`
  puis un script `scripts/tr_screenshots.py` (playwright sync API, viewport 1440x900 et 390x844 pour mobile) ; nommer
  `<page>_<tag>.png` (tag libre, ex. date ou sha8 courant — le juge ne verifie PAS le nom : il verifie que le commit qui
  ajoute/modifie chaque PNG est posterieur ou egal au dernier commit touchant app/, donc que les captures ont ete
  (re)prises APRES la derniere modification du code de rendu ; >= 6 PNG > 20 Ko, toutes listees dans le README). Si l'installation echoue :
  partial_reason=TOOL_MISSING avec la sortie exacte de pip.
- Publication : app/export_static.py --out-dir /Users/jocelyngrosjean/.local/share/twitter-radar-site/latest reste la
  commande de publication ; le script .context/run_tr_backfill_autopilot.zsh du depot de controle la reutilise.

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
