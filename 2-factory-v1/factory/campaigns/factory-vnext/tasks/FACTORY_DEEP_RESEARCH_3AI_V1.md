# FACTORY_DEEP_RESEARCH_3AI_V1 — recherche approfondie multi-IA : la Factory face au monde

## Objectif
Comparer, preuves à l'appui, la Factory (ce dépôt, tel qu'il est à HEAD) à ce qui existe dehors :
frameworks d'agents autonomes, pipelines de build « solo builder » publics, outils d'orchestration
multi-modèles, et en tirer une liste classée d'améliorations MESURABLES. Deux moteurs cherchent
séparément (GLM puis Claude), un des deux synthétise, Codex relit en adversaire (relecture multi-IA
au règlement). Rien n'est écrit sans citation ; rien n'est classé sans métrique et sans chiffre de
départ mesuré chez nous.

Le contrat exact des fichiers est le docstring de `factory_v2/verify_deep_research.py` — le juge.
Le lire avant d'écrire un seul fichier.

## Missions (une par étape, moteurs distincts)

### M1 — GLM : recherche (écrit `research/deep_research/FACTORY_2026-09/glm.json`)
- ≥ 8 sources DISTINCTES, chacune lue réellement (WebFetch / lecture du code source public) :
  frameworks (OpenHands, SWE-agent, Aider, Goose, MetaGPT, LangGraph, Temporal, Claude Agent SDK,
  Codex CLI, opencode…), ET au moins 3 « solo builders » ou petites équipes qui publient leur pipeline
  d'autopilot (dépôts GitHub, posts techniques avec code) — pas des vidéos marketing.
- Pour chaque source : URL, citation textuelle (≥ 40 caractères, copiée, pas paraphrasée),
  ce que ça résout, comparaison honnête avec NOTRE code (chemin de fichier chez nous),
  décision ADOPT / ADAPT / KEEP_OURS / REJECT, l'élément réutilisable, la métrique visée
  (vocabulaire du juge) et l'effet attendu.
- Problèmes à couvrir en priorité (ce sont nos points faibles mesurés cette semaine) :
  moteurs tués / orphelins, temps entre build et règlement, faux acceptés, boucles de retry,
  rechargement à chaud sans coupure, tests trop longs (3 150 tests ≈ 10 min à chaque candidat),
  coût en quota par jalon, 44 % de code mort.

### M2 — Claude : recherche indépendante (écrit `claude.json`, mêmes règles)
- Ne PAS lire `glm.json` avant d'avoir fini : l'intérêt est le recoupement.
- Même contrat, mêmes priorités ; viser des sources différentes quand c'est possible.

### M3 — Claude : synthèse (`SYNTHESIS.json` + `SYNTHESIS.md`)
- Lire les deux fichiers. ≥ 5 items classés (rang 1 = meilleur rapport effet / coût).
- Chaque item : métrique (vocabulaire du juge), **baseline mesurée chez nous** (depuis
  `projects/factory/evidence/attempt_accounts.jsonl`, `progress_incidents.jsonl`,
  `factory_v2/receipts/`, ou une mesure reproductible — citer la commande), cible chiffrée,
  URLs sources (présentes dans glm.json / claude.json), `found_by` (quel moteur l'a trouvé ;
  un item trouvé par les deux vaut plus), et une **proposition de jalon** (id, but, livrable,
  juge) prête à copier dans ROADMAP.yaml.
- `SYNTHESIS.md` : la liste de courses en français simple, sans jargon, une ligne par item :
  « quoi, pourquoi, combien on gagne, combien ça coûte ».

## Ce qui compte comme « fini » pour TA mission (lire avant de déclarer l'outcome)
- **M1 (GLM)** : `glm.json` conforme au contrat, ≥ 8 sources ; c'est le `test_cmd` de l'étape M1
  (vérification du seul fichier glm.json). Le juge global (2 moteurs + synthèse) n'a PAS à être vert
  pour M1 : il ne le sera qu'après M3. → outcome `IMPLEMENTED` dès que glm.json est bon et commité.
- **M2 (Claude)** : idem avec `claude.json`.
- **M3 (Claude)** : synthèse + juge global vert.
Déclarer `PARTIAL` parce que le juge global attend M2/M3 est une erreur (M1 de TR l'a fait le
18/09 : recherche correcte, mission comptée en échec pour rien).

## Interdits
- Inventer une source, une citation ou un chiffre. Une source non lisible → ne pas la citer.
- Modifier du code de la Factory, ROADMAP.yaml ou PROJECT_STATE.json : cette mission ne produit
  QUE `research/deep_research/FACTORY_2026-09/*`.
- Noter un item sans métrique du vocabulaire du juge.

## Juge
`python3 factory_v2/verify_deep_research.py FACTORY_DEEP_RESEARCH_3AI_V1 --project-repo . --dir research/deep_research/FACTORY_2026-09 --project-dir projects/factory --min-engines 2 --min-sources 8 --min-items 5`
puis relecture multi-IA (Codex + GLM, lecture seule) : BLOCK si une citation ne correspond pas
à sa source ou si une baseline n'est pas reproductible.
Niveau A. Budget 5 400 s par mission.
