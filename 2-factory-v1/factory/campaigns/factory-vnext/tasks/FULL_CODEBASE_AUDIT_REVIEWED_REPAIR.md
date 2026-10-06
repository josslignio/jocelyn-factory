# FULL_CODEBASE_AUDIT_REVIEWED — réparer l'audit selon les relecteurs

## Objectif
Le relecteur Claude (lecture seule, 18/09 03:44 Bangkok, transcription :
`projects/factory/evidence/reviews/FULL_CODEBASE_AUDIT_REVIEWED/claude_20260917T204417Z.log`,
registre : `projects/factory/evidence/review_accounts.jsonl`) a rendu BLOCK sur le commit
`aa295959` avec des preuves. Cette mission corrige les artefacts d'audit pour que les chiffres
soient RE-MESURÉS au HEAD courant, jamais recopiés, puis régénère le reçu. Lis d'abord la
transcription en entier (les lignes `assistant`/`result` du flux JSON) : elle contient la
liste exacte, fichier par fichier.

## Les 4 constats à fermer (tous, pas un sous-ensemble)

1. **LOC par décision irreproductibles** dans `factory_v2/audit/FACTORY_CONVERGENCE_LEDGER.json`
   (ex. runtime.py annoncé 168 lignes, réel 4432 ; model_router 198 vs 1874 ; dispatcher_v3
   918 vs 3631 ; factoryd 1039 vs 2335 ; candidate_registry 224 vs 128…). Re-mesurer CHAQUE
   entrée avec `wc -l` au HEAD (commande citée dans l'entrée, champ `loc_measured_cmd`), et
   réécrire la justification quand elle reposait sur un chiffre faux (« 168 LOC, stateless »).
2. **Décisions dont le champ `file` n'est pas le sous-système** (BacklogRefiller_V1,
   CTOPlanner_V1, CampaignState_next_mission, autopilot_V1 pointent roadmap_resolver.py /
   runtime.py ; DC-002 cite `factory/bin/campaign_run.py` qui n'existe plus). Une décision DELETE
   sur un module supprimé doit citer le commit de suppression (`git log --diff-filter=D`), pas
   un autre fichier.
3. **`FACTORY_F3_COUNTER_AUDIT_RECEIPT.json` ne root-cause pas
   `PREMATURE_IDLE_AFTER_MILESTONE_SETTLEMENT`** et laisse CA-007 à « MUST VERIFY ». Écrire la
   cause racine unique (trancher entre « ordre ROADMAP v2 » et « chemin V1 backlog_refiller »
   en lisant `tests/test_def001_premature_idle.py` et l'historique git), nommer le test de
   régression, fermer CA-007 avec la commande exécutée et sa sortie.
4. **Moins de 8 références publiques avec une vraie citation de code**
   (`FACTORY_PUBLIC_STATE_OF_ART_COMPARISON.md`, `FACTORY_F3_EXTERNAL_RESEARCH_RECEIPT.json`,
   `UPSTREAM_SOURCE_MANIFEST.json`) : « N/A » et « (fetched via WebFetch) » ne sont pas des
   commit SHA ; OpenHands, BMAD, Goose, MetaGPT, AutoGen sont « conceptuels » ; GitHub Spec Kit
   est confondu avec BMAD. Pour ≥ 8 références : dépôt, commit SHA réel, chemin de fichier,
   extrait de code copié (≥ 3 lignes), et ce qu'on en retient. WebFetch autorisé.
   Corriger aussi : RunRecords « NOT WIRED » (faux : 9 appels `record_*` dans runtime.py),
   backlog_refiller « ACTIVE » dans la carte des autorités (faux), lignes citées de factoryd.

## Contrat mécanique de la réparation (juge de TA mission — lis `factory_v2/verify_audit_repair.py`)
- grand livre : chaque décision dont le fichier existe → `loc` à ±10 % de `wc -l` ; fichier supprimé → champ `deleted_in_commit` (SHA 40 hex réel) ;
- contre-audit : clé `def001_root_cause` = {`defect`: "PREMATURE_IDLE_AFTER_MILESTONE_SETTLEMENT", `root_cause`, `regression_test`: "tests/…" existant} ;
- recherche externe : ≥ 8 `frameworks_inspected` avec `repo`, `commit_sha` (40 hex), `file`, `code_excerpt` (≥ 3 lignes copiées) ;
- carte des autorités : backlog_refiller plus jamais ACTIVE.
`python3 factory_v2/verify_audit_repair.py .` doit imprimer AUDIT_REPAIR_VERIFIED.

## Puis
Régénérer `factory_v2/receipts/FULL_CODEBASE_AUDIT_RECEIPT.json` en important
`factory_v2.audit_observation_producer` (`build_observation(milestone='FULL_CODEBASE_AUDIT_REVIEWED')`,
jamais `python3 -m`), `control_sha` = HEAD de ton worktree après tes commits d'artefacts.
Vérifier : `python3 -c "…build_observation…"` → `unmet(non-reviewer)= []`.

## Interdits
Recopier un chiffre sans le mesurer. Inventer une citation. Toucher au code de la Factory, à
ROADMAP.yaml ou PROJECT_STATE.json. Écrire `reviewers_min_3_distinct_accept: true`.

## Juge
Les 3 relecteurs (Codex, Claude, GLM, lecture seule) relisent le nouveau SHA avec le brief du
jalon ; puis l'oracle mécanique du jalon. Niveau A, budget 5 400 s.
