# TR_PRESENTATION_V2 — UX/UI mix Job Radar (base sombre) × deck Quant Pilot (composition), scores /100, français lisible

## Objectif
Rendu du site Twitter Radar dans le mix UX/UI voulu par le propriétaire (base sombre Job Radar UI V2 × composition du deck Quant Pilot) : scores sur 100 avec anneau, « pourquoi » en français sans jargon, décomposition du score, ce qui matche / ce qui manque, mouvements de classement avec direction, prochain refresh, page couverture reléguée — rendu uniquement, données inchangées.


## Contexte
TR_PRESENTATION_V1 (réglé 13/09) a livré les surfaces (brief hebdo, classement KOL, tickers, dossiers). Le propriétaire a
validé la STRUCTURE et demandé le 14/09 : reprendre exactement l'UX/UI de sa plateforme Job Radar UI V2 (bleu nuit, tuiles, cartes translucides), scores
sur 100, texte humain. Ne rien changer aux données ni aux calculs : uniquement le rendu (app/render.py, app/static/theme.css,
app/export_static.py) et les tests de rendu.

## Langage visuel (obligatoire) — LE MIX : base sombre Job Radar « UI V2 par JOÃO.AI » + composition du deck Quant Pilot
Deux fichiers de référence dans ce dossier, **reprendre leurs codes tels quels** (pas de réinterprétation) :
- `JOBRADAR_UI_V2_DESIGN_SYSTEM.css` + `JOBRADAR_UI_V2_REFERENCE.html` → **la base** : fond `linear-gradient(160deg,#0b0e1a,#12172e 55%,#1a1440)`,
  grain, texte `#e8eaf0`, muted `#8b93a8`, bleu `#6ea8ff`, vert `#3ddc85`, ambre `#ffb454`, violet `#c96eff`, cartes translucides
  à bord `rgba(255,255,255,.07)`, top bar, tuiles `.stats`.
- `QUANTPILOT_DESIGN_SYSTEM.css` → **la composition** transposée en sombre : `.kicker` (14px/800/letter-spacing 2.4px/uppercase, en bleu `#6ea8ff`),
  **un message par écran** (`h1` 44px/800/-0.8px), tuiles chiffres `.stat` à dégradé (bleu→`#6ea8ff`, vert→`#3ddc85`, orange→`#ffb454`),
  cartes à **liseré coloré** (gauche ou haut), pilules `.pill` uppercase, badge **VÉRIFIÉ** (vert) / **ESTIMÉ** (ambre), `.foot` discret.

Composants à reproduire depuis la capture Job Radar du propriétaire (14/09) — mêmes noms, mêmes gestes :
1. **Anneau de score /100** (cercle vert/ambre/rouge, chiffre au centre) sur chaque ligne de ticker et de compte.
2. **Bandeau de tuiles** en tête : « 14 comptes évalués · 1 727 calls · 3 battent le marché · état au … ».
3. **Chips de pipeline** avec flèches (ex. tickers : `Nouveau call → Confirmé → Actif → Clos`).
4. **Carte à liseré** par objet (ambre = à vérifier, bleu = en cours, vert = à suivre, rouge = à éviter), titre en 800, sous-ligne en une phrase.
5. **Panneau de détail** à droite (ou sous la carte en mobile) avec lignes `libellé … valeur ✓` (Source ✓, Prix vérifié ✓, Fraîcheur 3 jours ✓).
6. **« SCORE — DÉCOMPOSITION (jamais gonflé) »** : `indépendance des comptes +30 · track record +12 · fraîcheur +10 · momentum +9 · qualité des données +9 = 70`.
7. **« CE QUI MATCHE » / « CE QUI MANQUE (honnête) »** en deux listes courtes — pour un ticker : ce qui soutient le call / ce qui inquiète ;
   pour un KOL : ce qui le rend crédible / ce qui manque.
8. **« Pourquoi ce call : … »** / **« Pourquoi ce compte : … »** : une phrase en français, jamais un code.
9. Boutons/pilules d'action en dégradé bleu→violet quand il y a une action (ouvrir le tweet, voir le dossier).
- police `-apple-system,"Helvetica Neue",sans-serif` ; monospace uniquement tickers/dates ; responsive < 800px ; aucune dépendance externe.

## Règles de contenu
1. **Scores sur 100** partout (comptes et tickers) : afficher `NN / 100` (entier), jamais 0.xxx. Barre de progression
   colorée (vert ≥ 70, ambre 40-69, rouge < 40). Le score interne reste inchangé dans les JSON.
2. **« Pourquoi » en français, en phrases** — interdits dans le texte visible : `clé=valeur`, `snake_case`, codes
   (`ACTIVE_CALL`, `NEW_EPISODE`, `UNRANKED`, `EARLY_EVIDENCE`, `LONG_POSITION`…), `(score 0.7)`, nombres à 3+ décimales.
   Exemple attendu : « À éviter : un seul compte derrière ce call, compte sans historique, score 40/100 ».
   Une table de traduction code → phrase vit dans app/render.py (fonction pure, testée).
3. **Mouvements de classement** avec direction : ▲ vert (monte), ▼ rouge (recule), = gris, avec « +N places » / « −N places »,
   et une légende en tête : « rang = position parmi les N tickers, classés par score d'opportunité ; 1 = meilleur ».
   Le « pourquoi » d'un mouvement explique **ce qui a changé** entre les deux refreshs (composante du score qui a bougé,
   nouveau call, catalyseur, prix), en une phrase simple ; s'il n'y a aucun changement propre au ticker, l'écrire tel quel :
   « Rien de nouveau sur ce titre : d'autres tickers ont progressé ». Interdit : la même phrase générique répétée pour
   tous les tickers (le juge compare les « pourquoi » de la table des mouvements : au plus 1/3 identiques).
4. **Brief hebdo** : « État au <date heure UTC> · prochain refresh <date> » en tête.
5. **Classement KOL** : score /100 + barre, rendements par horizon colorés (vert/rouge), « n/d » en muted ; ligne repliable
   « pourquoi ce score » (3 phrases max). Ne pas inventer de données : n/d reste n/d.
6. **Dossier ticker** : mêmes cartes ; « Pour / Contre » en deux colonnes ; timeline conservée.
7. **Page « Couverture »** : sortie de la navigation principale (lien discret en pied de page « données & couverture »), réécrite
   en une phrase utile par compte : « Frenchie : 6 432 tweets sur 24 mois, collecte complète » / « BoubouSGC : pas encore collecté ».
8. Captures Playwright refaites (≥ 6, > 20 Ko, après la dernière modification de app/) + README qui les liste.

## Juge indépendant
`PYTHONPATH="${FACTORY_CONTROL_REPO}" python3 -m factory_v2.tr_presentation_verifiers TR_PRESENTATION_V2 --project-repo .`
(tout V1 + tokens JOAO présents dans theme.css + aucun jargon visible + `NN / 100` sur kols/tickers + aucun 0.xxx visible +
« prochain refresh » + direction des mouvements). Tests : `tests/test_tr_presentation.py` étendus (traduction code→phrase,
score /100, direction).

## Contraintes
Niveau A (Claude), budget 7200 s, max_turns : livrer un REPORT.json IMPLEMENTED avec commit AVANT de manquer de tours ;
si le budget serre, prioriser 1-2-3 (scores, français, direction) et déclarer PARTIAL honnêtement.
