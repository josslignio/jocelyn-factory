# JUGES & GATES — MATRICE DE VÉRIFICATION (FACTORY_REPAIR_VERIFY_HARDENING_V1)

Inventaire des juges/gates réparés dans ce lot : pour chacun, la preuve exacte
qu'il contrôle désormais vs ce qu'il ne contrôle pas (par construction).
Principe directeur : **jamais une longueur de liste, une présence textuelle ou
un booléen auto-déclaré — des identifiants distincts, des codes de sortie
réels, des empreintes confrontées aux octets, des observations recomptées.**
Absence de donnée = FAIL explicite (interdiction du succès par défaut).

Bibliothèque commune : `factory_v2/verify_lib.py` — `distinct_or_fail`,
`positive_int_or_fail`, `sha256_matches_bytes`, `exit_code_agrees`
(fail-closed, typées, testées dans `tests/test_verify_lib.py` et `tests/test_repairs_vh_library_unknown.py`). Les identités numériques, booléennes et structurées sont refusées, sans conversion en chaînes. Consommée par
`verify_audit_loop`, `verify_cycle2_manifest`, `verify_long_run_ready`.

| Juge / gate | Fichier | Preuve contrôlée (après réparation) | Ne contrôle PAS (par construction) |
|---|---|---|---|
| Escalade T3 | `factory/bin/dispatcher_v3.py` | un échec pytest répété après réparation inefficace continue vers T3 (L3-007) | diagnostic sémantique de conflit spec/contrat ; la reconnaissance de sortie pytest reste une heuristique |
| Verdict reviewer | `factory_v2/review_dispatch.py` | transcript structuré sans message modèle ⇒ ERROR ; stdout des outils ne fournit pas ACCEPT (L4_resilience_review-010) | authenticité cryptographique du transcript |
| A1/A6 reachable | `factory_v2/_a1a6_gate.py` | vrais nœuds **Call** des 3 symboles dans l'AST du dispatch de production (L5_1-001) | le comportement des fonctions appelées, leur résultat d'exécution |
| wiring gate | `factory_v2/_wiring_gate.py` | appels requis présents comme **Call** ; `runner.execute`/`ctx.runner.execute` gardés par `check_before_launch` **atteignable** (garde statiquement morte exclue) dans runtime.py **ET** advance/*.py (L5_1-002/031) | l'exécution effective à runtime (analyse statique), les chemins dynamiques (`getattr`) |
| snapshot capacités | `factory_v2/capability_snapshot.py` | champ historique du shim conservé ; empreinte **effective_validator_source_sha256** sur shim **ET** vérificateurs effectifs à HEAD, exigée/recomptée, absente ⇒ refus (L5_1-010) | le code chargé à l'exécution hors de ces fichiers (plugins dynamiques) |
| product E2E épinglé | `factory_v2/long_run_truth.py` | HEAD du repo contrôle **et** produit confronté au SHA épinglé **avant et après**, diff des fichiers suivis vide ; divergence ou octets suivis sales ⇒ `Unproven` (L5_1-023) | fichiers ignorés/non suivis ou modification puis restauration durant le run, la network policy du vérificateur |
| sonde fraîcheur F5 | `factory_v2/run_f5_benchmark.py` | **changement observé** : mutation de SPEC.md sur copie temp ⇒ l'identité rechargée doit bouger (L5_2-004) | la concurrence inter-processus de l'autorité, les autres fichiers canoniques |
| settlement full-suite | `factory_v2/settle_fullsuite.py` | code de sortie **réel** lu dans `<log>.rc` ; artefact absent/illisible ⇒ refus typé, aucun settlement (L5_2-010) | l'authenticité du fichier rc lui-même (convention du wrapper) |
| preflight collect-only | `factory_v2/verifier_preflight.py` | `--collect-only` inséré après **chaque** token pytest (commandes composées comprises) (L5_2-018) | les effets de bord des fixtures de collection |
| audit loop | `factory_v2/verify_audit_loop.py` | schema verrouillé, entrées non-null, compteurs **entiers >0** (via `positive_int_or_fail`) (L5_2-020) | la véracité des entrées Level A/B elles-mêmes (reçus du producteur) |
| long-run contrôlé | `factory_v2/verify_controlled_long_run.py` | PASS faute exige ≥1 `outcome=INJECTED` ; crash/restart booléens seuls refusés (L5_2-022) | la véracité du ledger de fautes (artifact producteur) |
| manifeste cycle2 | `factory_v2/verify_cycle2_manifest.py` | handles **distincts** (via `distinct_or_fail`), doublons = erreur, corpus_sha requis (L5_2-023) | l'existence réelle des comptes Twitter (hors-corpus) |
| refresh cycle2 | `factory_v2/verify_cycle2_refresh.py` | HUMAN_GO **recompté** et affiché (`HUMAN_GO=N (recounted)`) même sans flag (L5_2-024) | les événements non journalisés |
| hygiène factory | `factory_v2/verify_factory_hygiene.py` | stages = **imports AST** réels dans runtime.py (L5_2-026) ; restarts recomptés depuis PROJECT_STATE **sans** condition de log HOME (L5_2-027) ; digest validateur via le producteur (L5_1-010) | les import dynamiques, la complétude du dashboard lui-même |
| long-run v2 | `factory_v2/verify_long_run.py` | ≥3 jalons **DISTINCTS** + chaque settlement re-vérifié + autonomie recomptée (L5_3-001) | les jalons non listés dans le reçu |
| LR5 ready | `factory_v2/verify_long_run_ready.py` | `evidence_sha256` **confronté aux octets** (via `sha256_matches_bytes`), unbound ⇒ finding (L5_3-003) ; **cross-bundle §D** : jalons du long-run présents dans la timeline kill/restart (L5_3-002) | l'ordre temporel fin des jalons vs le kill |
| parse preflight | `factory_v2/verify_preflight_receipt.py` | ≥3 jalons **DISTINCTS, chaînes non vides canoniques** dans la preuve contrôlée (L5_3-005) | la véracité des hash-bound bundles (contrôlés par le juge LR5) |
| reshape | `factory_v2/verify_reshape_receipt.py` | commit d'application **strictement postérieur** à base (fenêtre base < commit ≤ integration) (L5_3-006) ; **toute** occurrence failed/error>0 refuse le log (L5_3-007) ; seules les entrées du **ledger gelé** autorisent des nouveaux fichiers (L5_3-008) | les fichiers hors TRACKED_DIRS, les modifications non-ajouts |
| runrecords | `factory_v2/verify_runrecords.py` | `--min-runs` **toujours** actif (L5_3-010) ; validated=True **cohérent** avec verdict/codes de sortie entiers (False/0.0 refusés) (L5_3-011) | la véracité des champs d'un enregistrement cohérent |
| mutation testing | `factory/bin/prove_red_factory.py` | KILLED si rc==1 **et** rapport JUnit avec échec de test sans erreur setup/teardown ; reçu absent/invalide ou usage/broken rc = SURVIVED (L6_1-016) | la couverture des assertions elles-mêmes |
| reçu runtime | `factory/bin/receipt.py` | `exit_code` du reçu **confronté au returncode réel** du processus oracle (L6_1-017) | le contenu des artefacts produits par l'oracle |
| reçu générique | `factory/bin/runtime_receipt.py` | `capability_id` comparé à expected (REJECT_CROSS_SCOPE) (L6_1-019) | les champs non-épinglés dans expected |
| supervisor V1 | `factory/lib/long_run_supervisor.py` | **dernier** verdict de régression décide ; ancien FULL_REGRESSION_PASS ne masque plus (L6_1-034) | les événements antérieurs au début du journal |
| long-run TR | `products/twitter_radar/twitter_radar_long_run.py` | gate auto-start : cible **courante ou observée prochaine dans ROADMAP** uniquement ; courant absent + roadmap inconnue ⇒ refus (L6_2-018) ; status==**PASS** + crash observé requis (L6_2-019) | l'exécution réelle de factoryd (déléguée au contrôlé) |
| feedback owner | `products/twitter_radar/tr_presentation_verifiers.py` | références avant/après **par item**, au moins une image dans chaque groupe + fichiers existants à HEAD (L6_2-024) ; page kols **absente = Unmet** (L6_2-025) ; couverture = fenêtre **ET** récence 14j (L6_2-026) | la pertinence visuelle des captures |
| ranking TR | `products/twitter_radar/product_verifiers.py` | lifecycle + graphes de découverte comparés à la recomputation (L6_2-028) ; doublons d'épisodes refusés : longueur liste == clés distinctes (L6_2-029) | la raw data du corpus elle-même (garantie par la recomputation depuis le disque) |
| milestone gate (wrapper) | `factory/lib/milestone_controller.py` | délègue au gate autoritaire : **exécution** du contrat gelé ; reçu déclaratif sans contrat = INCOMPLETE (L6_1-035, WONTFIX — code cité supprimé par le reshape) | — |

## Régressions adversariales

Chaque ligne du tableau a sa reproduction rouge→verte dans
`tests/test_repairs_vh_*.py` (doublons, rc≠0 caché, préambule neutre puis échec
final, booléens contradictoires, empreintes nulles, absences). Le juge du lot
re-exécute tous ces tests : `python3 factory_v2/verify_code_review_repairs.py
--project-repo . --ids-file factory/config/code_review_repair_verify_hardening_ids.json
--repairs-file factory_v2/audit/CODE_REVIEW_2026-09/REPAIRS_VERIFY_HARDENING.json`.

## Relecture Codex synchrone du 2026-09-20

Le test public `test_self_improvement.py` a été restauré à l’octet du baseline
`178ba6e1f` : le nouveau champ d’empreinte effective durcit la gate sans changer
le contrat du champ historique. Aucun test préexistant au lot ne reste modifié.

Limites déclarées : les gates AST prouvent la présence et certains chemins statiques,
pas une exécution observée ni une dominance générale ; les images sont des fichiers
référencés existants, sans analyse de contenu visuel ; la fenêtre de marché vérifie
les bornes/récence, pas la densité de toutes les séances. Le fichier `.rc` dépend du
wrapper qui l’écrit. Voir `SESSION_VERIFY_HARDENING_EVIDENCE.json` pour les codes de
sortie et les reproductions originales ; aucune de ces limites ne vaut PASS global.
