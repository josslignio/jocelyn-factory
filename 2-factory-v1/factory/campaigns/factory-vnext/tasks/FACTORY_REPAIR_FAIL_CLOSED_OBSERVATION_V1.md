# FACTORY_REPAIR_FAIL_CLOSED_OBSERVATION_V1 — Une observation impossible est convertie en verdict favorable (fail-open généralisé)

## Objectif
Une observation indisponible (fichier illisible, commande en erreur, registre corrompu, exception de sonde) ne produit jamais un verdict favorable : état INCONNU typé et bloquant.

## Contrat
Pour CHAQUE id ci-dessous : test qui reproduit le défaut (rouge), correctif minimal au fichier:ligne cités, test vert, et une ligne dans
`factory_v2/audit/CODE_REVIEW_2026-09/REPAIRS_FAIL_CLOSED_OBSERVATION.json` : `{"id", "status": "FIXED|WONTFIX", "commit", "test", "note"}` (WONTFIX seulement démontré par un test). ≥ 80 % FIXED.
Livrables de la synthèse (à honorer en plus des constats) :
- Grep systématique des except/None→succès dans les lecteurs d'état, preflights et juges
- Trinité VIVANT/MORT/INCONNU appliquée aux contrôles de vie, leases, journaux et quarantaines
- Corruption de marqueur durable (GLOBAL_QUARANTINE, ledger) = état persistant bloquant, pas absent
- Tests d'injection d'erreur par lecteur : indisponibilité => refus, jamais PASS/zéro

## Constats (id · fichier:ligne · catégorie/sévérité · pourquoi)
- `L1_advance-001` · `factory_v2/advance/control_plane.py:124` · ERROR_HANDLING/MEDIUM · Une erreur de réconciliation des RunRecords orphelins disparaît sans événement ni diagnostic. Une panne durable du ledger est ainsi indiscernable d’une réconciliation réussie pendant que le contrôle continue. Injection i
- `L2_runtime_core-002` · `factory_v2/authority_gate.py:413` · ERROR_HANDLING/HIGH · Une erreur d’exécution du contrôle de régression vaut absence de régression. _check_regressions ne refuse que rc=0 ; une commande introuvable (127) ou interrompue par timeout (124) donne un jalon complet si les autres pr
- `L2_runtime_core-012` · `factory_v2/resource_accounting.py:437` · ERROR_HANDLING/MEDIUM · Une panne du contrôle de vie devient une mort attestée du worker. reconcile_open_attempts avale toute exception de is_live puis écrit TERMINAL/KILL/PROCESS_LOST. La reproduction injecte une indisponibilité du contrôle : 
- `L2_runtime_core-014` · `factory_v2/runtime.py:766` · BUG/HIGH · Le portefeuille peut être déclaré complet alors qu’un projet est illisible. Les erreurs isolées ne bloquent que si valid_entries est vide ; si un autre projet valide a fini, la sélection arrive à PORTFOLIO_COMPLETE malgr
- `L5_factory_v2_rest_1-021` · `factory_v2/long_run_truth.py:70` · BUG/HIGH · La preuve de reprise repose sur trois recherches any indépendantes, sans ordre temporel ni comparaison des identités de naissance. Des événements ordonnés progrès puis naissance puis décès, avec la même valeur birth, sat
- `L5_factory_v2_rest_1-022` · `factory_v2/long_run_truth.py:94` · BUG/HIGH · Le contrôle d’interventions refuse uniquement un journal entièrement vide. Un seul événement de 2025, hors du run et de la fenêtre demandée en septembre 2026, suffit à retourner zéro intervention. Un manque total de couv
- `L5_factory_v2_rest_2-017` · `factory_v2/verifier_preflight.py:69` · BUG/MEDIUM · Le préflight de script assimile tout rc=2 à VERIFIER_NOT_IMPLEMENTED. Or Python utilise aussi rc=2 lorsque le fichier est absent. Une erreur de chemin devient donc un diagnostic erroné de capacité manquante, contrairemen
- `L5_factory_v2_rest_3-009` · `factory_v2/verify_reshape_receipt.py:103` · ERROR_HANDLING/MEDIUM · Les erreurs de git ls-tree sont traitées comme des répertoires vides ; _added_files fait de même avec git diff (lignes 126-127). En injectant rc=128 sur ces commandes tout en laissant les autres requêtes Git fonctionner,
- `L5_factory_v2_rest_3-012` · `factory_v2/verify_simplification_batch.py:194` · BUG/HIGH · Le token est passé à git grep sans -e. Un token commençant par '--', comme --legacy-v1 dans le manifeste courant, est interprété comme une option Git ; git grep sort 129, mais le code ignore returncode et conclut qu’il n
- `L5_factory_v2_rest_3-014` · `factory_v2/worker_heartbeat.py:80` · ERROR_HANDLING/MEDIUM · Toute exception du heartbeat est avalée sans journal ni état d’erreur. Deux OSError('disk full') consécutives ne produisent aucun diagnostic et la boucle continue avec last_seq=0. Un défaut de stockage durable peut donc 
- `L6_bin_lib_products_1-025` · `factory/bin/verify_campaign_freeze.py:414` · BUG/HIGH · Une racine M5 inexistante se réduit à MANUAL_CONTINUE=0 ; une liste roadmap_paths vide donne aussi zéro violation. evaluate considère ces compteurs comme mesurés et peut rendre campaign=PASS / freeze=ALLOWED avec des mis

## Interdits
Modifier un test existant pour le faire passer. Toucher ROADMAP.yaml / PROJECT_STATE.json. Supprimer un constat sans preuve.

## Juge
`python3 factory_v2/verify_code_review_repairs.py --project-repo . --ids-file factory/config/code_review_repair_fail_closed_observation_ids.json --repairs-file factory_v2/audit/CODE_REVIEW_2026-09/REPAIRS_FAIL_CLOSED_OBSERVATION.json` puis suite sûre. Relecture Codex. Niveau A, budget 3 h.
