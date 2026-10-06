# FACTORY_TOKEN_TRUTH_V1 — chaque lancement de moteur a un coût mesuré ; la factory se compare aux moteurs seuls en tokens réels

## Objectif
Question du propriétaire, posée depuis le début et jamais répondue avec des chiffres : « la factory dépense-t-elle
beaucoup plus de tokens qu'un GLM seul, un Claude seul ou un Codex seul ? ». Aujourd'hui 24 essais sur 28 n'ont AUCUN
compte de tokens : les adaptateurs (`factory_v2/telemetry/adapters.py`) attendent d'anciens formats (codex `token_count`),
alors que codex 0.154 émet `{"type":"turn.completed","usage":{"input_tokens":…,"cached_input_tokens":…,"output_tokens":…,
"reasoning_output_tokens":…}}` (visible dans les `output_tail` des reçus de dispatch), et le flux brut n'est pas conservé.
Le contrat exact est le docstring de `factory_v2/verify_token_truth.py` (le juge).

## À construire
1. Adaptateurs à jour pour les trois CLI, prouvés sur des **échantillons réels** capturés dans
   `tests/fixtures/token_truth/{codex,opencode,claude}.jsonl` (prendre les flux des prochains lancements, pas des
   échantillons inventés ; pour opencode, capturer un vrai `run --format json`).
2. Flux brut conservé par tentative : `projects/<p>/evidence/streams/<attempt_key>.jsonl` (bookkeeping : ajouter au
   `capture_ignore.json`), écrit par le dispatcher au fil de l'eau.
3. Registre : chaque ligne TERMINAL porte les tokens mesurés (≥ 90 % des essais depuis l'arrivée du lot) ; une coupure
   avant le premier événement est typée (`usage_source`), jamais un zéro.
4. `factory_v2/token_cost_report.py --since-hours N [--json]` : par jalon réglé et par moteur — tokens entrée / cache /
   sortie, essais, minutes moteur, et « coût par résultat validé » selon le contrat de mesure de SPEC.md (dénominateur nul
   → non défini + coût brûlé affiché, jamais 0). Une ligne dans `BUILD_METRICS_LATEST.md`.
5. Benchmark apparié rejoué avec tokens : `run_matched_benchmark_3arm.py` sur la charge existante, 3 runs par bras, chaque
   bras avec tokens mesurés > 0, verdict enrichi de `tokens_vs_best_single` (ratio) à côté des résultats vérifiés.
   Réponse attendue au propriétaire, en une phrase dans le reçu : « la factory coûte X× le meilleur solo pour Y% de résultats
   vérifiés en plus ».

## Interdits
Inventer des échantillons. Traduire un abonnement en prix API. Modifier les juges, STRATEGY/SPEC/ROADMAP.
Lancer un vrai moteur dans un test (les échantillons sont des fichiers).

## Juge
`python3 factory_v2/verify_token_truth.py --project-repo .` → `TOKEN_TRUTH_VERIFIED`. Relecture Codex (lecture seule).
Niveau A, budget 10 800 s.
