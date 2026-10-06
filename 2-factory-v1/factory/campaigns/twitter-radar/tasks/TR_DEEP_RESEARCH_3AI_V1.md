# TR_DEEP_RESEARCH_3AI_V1 — recherche approfondie multi-IA : Twitter Radar face aux produits similaires

## Objectif
Comparer Twitter Radar (ce dépôt, à HEAD : extraction des calls, classement KOL, briefs sociétés,
profils, site statique) aux produits et projets qui font la même chose ou une partie — trackers de
« fintwit », suivi de KOL / influenceurs boursiers, extracteurs de tickers et de sentiment, outils
de track record public, dashboards de calls — et en tirer une liste classée d'améliorations
MESURABLES : précision et rappel de l'extraction, couverture des comptes, fraîcheur, temps pour
décider, coût de collecte, lisibilité (logique d'humain, cf. `projects/twitter-radar/SPEC.md`).
Deux moteurs cherchent séparément (GLM puis Claude), un synthétise, Codex relit en adversaire.

Le contrat exact des fichiers est le docstring de `factory_v2/verify_deep_research.py` (dépôt de
contrôle) — le juge. Le lire avant d'écrire un seul fichier.

## Missions

### M1 — GLM : recherche (écrit `research/deep_research/TR_2026-09/glm.json`)
- ≥ 8 sources DISTINCTES réellement lues (WebFetch, dépôts publics, docs produit, posts techniques
  avec méthode) : produits commerciaux et projets open source qui suivent des comptes X/Twitter
  financiers, extraient des tickers / calls, calculent un track record ; ET au moins 3 projets de
  builders solo publiés (dépôts GitHub avec code lisible).
- Pour chaque source : URL, citation textuelle (≥ 40 caractères), ce que ça résout, comparaison
  honnête avec NOTRE code (chemin chez nous : `products/twitter_radar/...`, `scripts/...`,
  `app/...`), décision ADOPT / ADAPT / KEEP_OURS / REJECT, élément réutilisable, métrique
  (vocabulaire du juge : `call_extraction_precision`, `call_extraction_recall`,
  `account_coverage`, `data_freshness_h`, `time_to_decision_s`, `kol_ranking_stability`,
  `backfill_cost_per_account`, `ux_task_completion_s`), effet attendu.
- Problèmes à couvrir en priorité : comptes vides / suspendus (11 sur 45 cette semaine),
  détection des calls implicites, horizon de performance, stabilité du classement d'une semaine
  à l'autre, coût des collectes, et la présentation « sans jargon, scores /100 ».

### M2 — Claude : recherche indépendante (`claude.json`, mêmes règles, sans lire glm.json avant)

### M3 — Claude : synthèse (`SYNTHESIS.json` + `SYNTHESIS.md`)
- ≥ 5 items classés, chacun avec métrique, **baseline mesurée chez nous** (depuis
  `data/`, `research/`, les reçus de refresh, ou une commande reproductible — la citer),
  cible chiffrée, URLs sources présentes dans les fichiers moteur, `found_by`, et une
  proposition de jalon prête pour `projects/twitter-radar/ROADMAP.yaml`.
- `SYNTHESIS.md` : liste de courses en français simple, une ligne par item.

## Ce qui compte comme « fini » pour TA mission (lire avant de déclarer l'outcome)
- **M1 (GLM)** : `glm.json` conforme au contrat, ≥ 8 sources ; c'est le `test_cmd` de l'étape M1
  (vérification du seul fichier glm.json). Le juge global (2 moteurs + synthèse) n'a PAS à être vert
  pour M1 : il ne le sera qu'après M3. → outcome `IMPLEMENTED` dès que glm.json est bon et commité.
- **M2 (Claude)** : idem avec `claude.json`.
- **M3 (Claude)** : synthèse + juge global vert.
Déclarer `PARTIAL` parce que le juge global attend M2/M3 est une erreur (M1 de TR l'a fait le
18/09 : recherche correcte, mission comptée en échec pour rien).

## Interdits
- Inventer une source, une citation, un chiffre. Modifier le code produit, la roadmap ou l'état :
  cette mission ne produit QUE `research/deep_research/TR_2026-09/*`.

## Juge
`PYTHONPATH="$FACTORY_CONTROL_REPO" python3 "$FACTORY_CONTROL_REPO/factory_v2/verify_deep_research.py" TR_DEEP_RESEARCH_3AI_V1 --project-repo . --dir research/deep_research/TR_2026-09 --project-dir "$FACTORY_CONTROL_REPO/projects/twitter-radar" --min-engines 2 --min-sources 8 --min-items 5`
puis relecture multi-IA (Codex + GLM, lecture seule).
Niveau A. Budget 5 400 s par mission.
