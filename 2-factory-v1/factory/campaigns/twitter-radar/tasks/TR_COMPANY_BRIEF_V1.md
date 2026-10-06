# TR_COMPANY_BRIEF_V1 — la fiche entreprise en une lecture (demande propriétaire 2026-09-14)

## Objectif
Pour chaque ticker, une fiche entreprise en français simple (ce qu'elle fait, depuis quand, dette et cash-flow, pourquoi l'action bouge, edge ou vague) : faits yfinance typés + thèse en clair générée une fois par ticker et cachée, intégrée en tête du dossier ticker et dans la table.


## Ce que le propriétaire veut lire pour CHAQUE ticker, en français simple
1. **Ce que fait vraiment l'entreprise, en une phrase** (pas le blurb de 8 lignes) : « Bloom Energy fabrique des piles à combustible pour alimenter les data centers sans réseau. »
2. **Depuis quand elle existe** (fondation ou 1ère cotation) et **ce qu'ils construisent / vendent** concrètement.
3. **Argent** : ont-ils emprunté (dette nette, dette/fonds propres) ? sont-ils **cash-flow positif** (FCF 12 mois) ? chiffre d'affaires et croissance ?
4. **Pourquoi l'action performe** (ou pas) : les 2-3 causes des mouvements récents, reliées aux catalyseurs déjà collectés.
5. **Ont-ils un edge ?** (avantage identifiable : techno, coût, position, contrats) ou **surfent-ils une vague** (thème de marché : IA, bitcoin, optique…) — dire lequel, honnêtement.

## Deux couches, deux sources
- **Faits** (`research/tr_company_facts.json`, un objet par ticker) — via yfinance `Ticker.info` / `.financials` / `.cashflow` :
  `one_liner_source` (longBusinessSummary), `sector`, `industry`, `founded_or_ipo` (firstTradeDateEpochUtc, à défaut « pas de donnée »),
  `employees`, `revenue_ttm`, `revenue_growth_yoy`, `free_cash_flow_ttm`, `operating_cash_flow_ttm`, `total_debt`, `net_debt`,
  `debt_to_equity`, `gross_margin`, `market_cap`, `observed_at`, `source`. Chaque champ absent = `null` + raison en français
  (« non publié », « pas de donnée fournisseur »), jamais 0. Cache par ticker sous `data/company_state/<ticker>.json`, refresh ≥ 7 jours.
- **Thèse en clair** (`research/tr_company_brief.json`) — 5 phrases maximum par ticker, en français, générées par un modèle
  **une seule fois par ticker** puis mises en cache ; régénérées uniquement si les catalyseurs des 30 derniers jours ont changé
  (hash). Entrées du prompt : faits ci-dessus + catalyseurs/news déjà dans `tr_ticker_dossier.json` + variations de prix.
  Sorties typées : `what_they_do` (1 phrase), `since` (1), `money` (1-2 : dette, cash-flow), `why_it_moves` (1-2), `edge_or_wave`
  (1 : « edge : … » ou « surfe sur : … »), `confidence` (haute/moyenne/basse + pourquoi), `sources`. Interdit : inventer un
  chiffre absent des faits ; si un fait manque, la phrase le dit (« dette : non publiée »).
  Moteur : Codex (`codex exec --json`, lecture seule, ~250 tickers × 1 appel, budget total < 2 % hebdo) via un script
  `scripts/tr_company_brief.py --limit N --only-changed` résumable ; tout appel journalisé (ticker, hash, coût estimé).

## Intégration
- `scripts/tr_ticker_dossier.py` : ajoute `company_facts` + `company_brief` à chaque dossier.
- `app/render.py` : carte « L'entreprise en clair » en TÊTE du dossier ticker (avant les prix), et une colonne « en une phrase »
  dans la table tickers (tronquée à 90 caractères, titre complet au survol).
- Tests : parsing yfinance mocké, cache/refresh, prompt typé, rendu.

## Juge indépendant
`PYTHONPATH="${FACTORY_CONTROL_REPO}" python3 -m factory_v2.tr_presentation_verifiers TR_COMPANY_BRIEF_V1 --project-repo .` :
facts présents pour ≥ 90 % des tickers de la table (chaque absence typée en français), brief présent pour 100 % des tickers
avec calls actifs, aucune phrase > 220 caractères, aucun chiffre dans le brief absent des faits (contrôle croisé sur les
montants), texte visible sans jargon (règles V2), carte « L'entreprise en clair » présente sur chaque dossier.

## Contraintes
Niveau B (Codex builder confiné), budget 7200 s ; réseau yfinance autorisé ; aucun appel modèle dans les tests.
