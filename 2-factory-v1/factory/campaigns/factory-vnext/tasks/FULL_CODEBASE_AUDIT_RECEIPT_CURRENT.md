# FULL_CODEBASE_AUDIT_RECEIPT_CURRENT (v3, apres fusion CODEX-LOT-C37 sur HEAD fb2e3808)

## Outcome
Amener les huit artefacts d'audit a jour (mesures, jamais estimes) et REGENERER reellement
`factory_v2/receipts/FULL_CODEBASE_AUDIT_RECEIPT.json` en import ant
`factory_v2.audit_observation_producer` directement (le module N'A PAS de CLI `-m` : verifie
toi-meme, aucun bloc `if __name__` n'existe dans `audit_observation_producer.py`). Le receipt doit
etre ecrit avec `milestone='FULL_CODEBASE_AUDIT_REVIEWED'` (pas le nom historique du jalon) --
`observation_hash` inclut le champ `milestone`, donc un receipt ecrit sous un autre nom de jalon ne
matchera JAMAIS la recomputation du jalon suivant.

Derived task brief. NOT canonical intent -- voir ROADMAP.yaml. Do not edit ROADMAP.yaml,
PROJECT_STATE.json, any verifier, any credential or owner authority from inside this mission.

## Etat reel observe a HEAD (2026-09-12, verifie en executant le code)
`python3 -c "...build_observation(milestone='FULL_CODEBASE_AUDIT_REVIEWED')..."` retourne
`unmet(non-reviewer)= ['artifacts_bound_to_head']` (rc=1) : le receipt existant (control_sha
32362f77) est PERIME -- des commits depuis (dont C37 lui-meme, fb2e3808) ont touche du code hors
de l'allowlist de fraicheur (`factory_v2/audit_observation_producer._AUDIT_FRESHNESS_SAFE_PREFIXES`).
C'est exactement le travail de cette mission : refaire les huit artefacts a jour au HEAD courant,
puis regenerer le receipt.

## Deliverables / Acceptance
Identiques aux versions precedentes (sept clauses mecaniques : subsystems, >=8 refs, LOC/decisions/
subsystems mesures, DEF-001 fixe). `reviewers_min_3_distinct_accept` reste HORS PERIMETRE ICI.

## Changement v3 (corrige les 5 findings du contre-audit + 1 finding supplementaire)
1. C37 est fusionne (fb2e3808) -- ne PAS re-implementer la boucle multi-relecteurs ici, elle vit
   dans `factory_v2/runtime.py`.
2. Le receipt DOIT porter `control_sha` (un des `SHA_FIELDS` reconnus par
   `audit_observation_producer._artifact_sha_field`) car `FULL_CODEBASE_AUDIT_REVIEWED` le lit via
   `multi_ai_review.review_sha_from_artifact` -- sans cela le runtime refuse REVIEW_NO_SHA.
3. `build_observation`/`observation_hash` sont appeles avec `milestone='FULL_CODEBASE_AUDIT_REVIEWED'`
   partout, jamais le nom historique.
4. `python3 -m factory_v2.audit_observation_producer` NE MARCHE PAS (pas de CLI) -- importer le
   module et appeler ses fonctions directement, comme le test_cmd de ce jalon le fait.
5. Ne jamais appeler `review_dispatch.dispatch_review` depuis cette mission -- c'est le runtime qui
   orchestre les 3 relecteurs sur le jalon suivant.

## Stop conditions
- Budget epuise -> laisser l'evidence ecrite, receipt honnete PARTIAL.
- Toute clause sans preuve independante -> UNMET, jamais un booleen fabrique.
- Ne jamais ecrire `reviewers_min_3_distinct_accept: true` depuis cette mission.

## Outcome de TON REPORT.json (lu et applique) — ajoute le 2026-09-13 apres 2 lancements perdus
Le RECEIPT d'audit (factory_v2/receipts/FULL_CODEBASE_AUDIT_RECEIPT.json) reste honnetement `status=PARTIAL`
parce que la clause `reviewers_min_3_distinct_accept` est HORS PERIMETRE ici (jalon suivant). Ce statut du
RECEIPT n'est PAS ton outcome. TON outcome dans REPORT.json est `IMPLEMENTED` des que la commande de test du
jalon (`unmet(non-reviewer)= []`, rc=0) est verte dans ta session — c'est la definition complete du travail
de cette mission. Declarer PARTIAL parce que le receipt est PARTIAL fait perdre un lancement pour rien.
