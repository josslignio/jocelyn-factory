# FACTORY_DEEP_RESEARCH_SCALE_LOCAL_V2 — la Factory est-elle le bon harnais pour scaler (auto-amélioration, multiplication d'agents, machine locale) ?

## Objectif
Ordre propriétaire 22/09 06:00 Bangkok : recherche approfondie pour être **certain** que la factory actuelle est la
meilleure base pour : (1) s'auto-améliorer run après run, (2) multiplier les agents, (3) intégrer une machine locale
(modèles ouverts) à côté des IA payantes. Déclencheur : la vidéo « Adieu les Apps : la nouvelle stratégie IA d'Apple »
(chaîne YouTube « IA et Stratégie », 78,9 k abonnés, sept. 2026) — thèse : *le modèle mange le logiciel ; ce qui reste,
c'est la machine, le contexte, la permission d'agir et le harnais*. Le contrat exact des fichiers est le docstring de
`factory_v2/verify_deep_research.py` — le juge. Rien sans citation ; rien sans métrique et chiffre de départ chez nous.

## Questions auxquelles la recherche DOIT répondre (avec sources)
1. **Le harnais de cette vidéo** : que sait-on de son setup (MacBook Pro M3 Max 128 Go, Qwen 2.5 72B) et de la couche
   qu'il utilise (LM Studio / Ollama / MLX / autre) ? Quelles affirmations de la vidéo sont des faits vérifiables
   (Apple Core AI / Foundation Models ouvert à d'autres modèles, Mac Studio 512 Go, Nvidia–Hugging Face, prix DRAM)
   et lesquelles sont des opinions ? Une ligne par affirmation : FAIT (source) / OPINION.
2. **Harnais open source 2026** — OpenHands, SWE-agent, Aider, Goose (Block), OpenCode, Cline/Roo, AutoGen, CrewAI,
   LangGraph, MetaGPT/ChatDev, agents « always-on » (produits à 200 $/mois) : pour chacun, architecture, horizon
   d'autonomie (minutes / heures / jours), vérification indépendante du builder (oui/non), multi-agent, boucle
   d'auto-amélioration (existe ? mesurée ?), support des modèles locaux, licence, activité du dépôt (commits 90 j).
3. **Ce que la Factory fait que ces harnais ne font pas, et l'inverse** — matrice sur les 8 critères du propriétaire :
   simple, rapide, économe en quota, enchaînement fluide entre moteurs, ne boucle pas / ne crashe pas, suit
   roadmap-spec-stratégie sans perdre le contexte, s'auto-améliore avec preuve, multiplie les agents + machine locale.
   Chaque case = un fait cité (fichier chez nous à HEAD, ou source externe), jamais une impression.
4. **Inférence locale** — Ollama, MLX, LM Studio, vLLM/llama.cpp : quels modèles ouverts tiennent sur 16 Go (la machine
   actuelle : Mac M3, 16 Go — Qwen2.5-Coder 7B, Qwen3 8B déjà installés), 64 Go, 128 Go, 512 Go ; leurs scores sur
   des bancs de code publics (SWE-bench Verified, LiveCodeBench, Aider polyglot) face à Claude / Codex / GLM ; quelles
   tâches de la factory et de Twitter Radar sont exécutables localement sans perte (classification de posts,
   extraction de tickers, relecture de forme, tri de constats…) et lesquelles non.
5. **Point mort machine** : avec la dépense réelle du propriétaire (~350 €/mois : Claude Max 20x, Codex Pro, GLM Coding
   Max) et les tokens mesurés chez nous (`PYTHONPATH=. python3 factory_v2/token_cost_report.py --since-hours 168 --json`),
   à partir de quelle part de travail local un Mac Studio 128 / 512 Go s'amortit en < 12 mois ? Donner la formule et
   le calcul, pas un avis.
6. **Verdict et décisions** : pour chaque sous-système de la factory (routage, dispatch, vérification, mémoire/contexte,
   auto-amélioration, multi-agent, local) — KEEP_OURS / ADAPT (quoi, depuis quelle source, quel fichier chez nous) /
   ADOPT / REJECT, avec la métrique du juge et l'effet attendu. Et une réponse en une phrase à « la factory est-elle la
   meilleure base pour scaler ? » — oui / non / oui-si, avec les 3 raisons.

## Sources fournies par le propriétaire — à croiser obligatoirement
`research/deep_research/FACTORY_SCALE_LOCAL_2026-09/inputs/` : transcription de la vidéo, deux articles surhumain.ai
(évaluer un agent de veille ; second cerveau fiable). Chaque affirmation → FAIT (source primaire retrouvée) ou OPINION ;
chaque recommandation de méthode → déjà appliquée chez nous (fichier) ou manquante (proposition de jalon). Question 8 : les 11 affirmations (a)-(k) et le tableau « à étudier avant de recoder » de
`inputs/analyse_prealable_claude_cowork_2026-09-22.txt` — chacune CONFIRMÉE (URL + citation) ou INFIRMÉE ; pour chaque brique
(harness.lol, Omnigent, Temporal, Harbor, AHE, llama.cpp serveur), la décision ADOPT/ADAPT/KEEP_OURS/REJECT avec le fichier
chez nous et la métrique.
Question 7 :
la méthode d'évaluation de surhumain.ai (critères avant réponses, cas tenus à l'écart, injection indirecte, coût complet
avec temps humain) — qu'est-ce que la factory n'applique pas encore ?

## Missions (moteurs distincts, sources différentes quand c'est possible)
### M1 — GLM : recherche (`research/deep_research/FACTORY_SCALE_LOCAL_2026-09/glm.json`, ≥ 12 sources lues réellement : dépôts, docs, papiers, bancs publics)
### M2 — Claude : recherche indépendante (`research/deep_research/FACTORY_SCALE_LOCAL_2026-09/claude.json`, ne pas lire glm.json avant d'avoir fini)
### M3 — Codex : synthèse (`research/deep_research/FACTORY_SCALE_LOCAL_2026-09/SYNTHESIS.json` + `SYNTHESIS.md`) — ≥ 8 items classés, baselines mesurées chez nous
(commande citée), proposition de jalon par item, `found_by`. `SYNTHESIS.md` en français simple : la réponse aux 6
questions, puis la liste « quoi / pourquoi / gain / coût », puis la recommandation machine (chiffrée).

## Ce qui compte comme « fini » pour TA mission
M1 : glm.json conforme, ≥ 12 sources → IMPLEMENTED. M2 : idem claude.json. M3 : synthèse + juge global vert.
Déclarer PARTIAL parce que le juge global attend M2/M3 est une erreur.

## Interdits
Inventer une source, une citation, un score de banc ou un prix. Modifier du code, ROADMAP.yaml ou PROJECT_STATE.json :
cette mission ne produit QUE `research/deep_research/FACTORY_SCALE_LOCAL_2026-09/*`. Traduire un abonnement en prix API inventé.

## Juge
`python3 factory_v2/verify_deep_research.py FACTORY_DEEP_RESEARCH_SCALE_LOCAL_V2 --project-repo . --dir research/deep_research/FACTORY_SCALE_LOCAL_2026-09 --project-dir projects/factory --min-engines 2 --min-sources 12 --min-items 8`
