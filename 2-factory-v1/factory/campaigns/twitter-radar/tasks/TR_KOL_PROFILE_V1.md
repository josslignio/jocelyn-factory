# TR_KOL_PROFILE_V1 — qui sont les KOL, en français simple (demande propriétaire 2026-09-14)

## Objectif
Pour chaque compte KOL, un portrait en français simple (qui c'est, depuis quand, spécialités déduites des calls, reconnaissance dans le milieu, track record en une phrase) : faits typés + portrait en clair généré une fois par compte et caché, intégré en tête de la page compte et dans la table.


## Ce que le propriétaire veut lire pour CHAQUE compte
1. **Qui c'est** : bio en une phrase, depuis quand sur X (date de création si exportée, sinon premier tweet observé), abonnés,
   fréquence de publication, langue.
2. **Ses spécialités** : déduites des calls (secteurs / thèmes des tickers appelés, pondérés par le nombre de calls) — « semi-conducteurs
   & IA (58 % des calls), mineurs bitcoin (22 %) ». Style : swing / long terme (durée médiane des épisodes), taille de conviction
   (relances par call).
3. **Reconnaissance dans le milieu** : qui le reprend / le cite (quotes, réponses, mentions) parmi les autres comptes de l'univers et
   parmi les comptes à forte audience présents dans le corpus ; qui il reprend lui-même (originator vs follower dans le consensus).
   Sortie factuelle : « repris 41 fois par 7 comptes de l'univers, dont CKCapitalxx (12) ; cité par 3 comptes > 100 k abonnés ».
4. **Son track record en une phrase** (déjà calculé par TR_KOL_HORIZONS_V1) : « 46 calls gagnants / 35 perdants sur 2 ans, +34 % ajusté
   marché ; meilleur call IONQ +180 % ».

## Deux couches
- **Faits** (`research/tr_kol_profile_facts.json`, un objet par compte) — depuis le corpus (data/corpus/<compte>/posts.jsonl : bio si
  présente, dates, mentions/quotes/réponses entre comptes), `tr_call_ledger.json` (tickers → secteurs via tr_ticker_table), 
  `tr_kol_horizons.json`. Champs absents = `null` + raison en français. Aucun appel réseau obligatoire ; twscrape `user` autorisé
  pour bio/abonnés si disponible, sinon « non collecté ».
- **Portrait en clair** (`research/tr_kol_profile.json`) — 4 phrases max par compte (`who`, `specialties`, `recognition`, `track_record`),
  générées une fois par compte par Codex (lecture seule) et cachées par hash des faits ; aucun chiffre absent des faits.

## Intégration
- Page compte (`kols_<handle>.html`) : carte « Qui c'est » en tête ; table KOL : colonne « spécialité » (2 mots) + badge « repris par N ».
- Tests : extraction mentions/quotes, pondération des spécialités, cache, rendu.

## Juge : `TR_KOL_PROFILE_V1` dans tr_presentation_verifiers — faits pour 100 % des comptes de l'univers (absences typées), portrait
pour 100 % des comptes avec ≥ 1 call évalué, phrases ≤ 220 caractères, sans jargon, chiffres présents dans les faits, carte rendue.

Niveau B (Codex builder confiné), budget 7200 s.
