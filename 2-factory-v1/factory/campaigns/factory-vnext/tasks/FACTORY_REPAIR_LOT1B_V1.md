# FACTORY_REPAIR_LOT1B_V1 — réparer les défauts qui font boucler la factory

## Objectif
Corriger, avec test rouge→vert pour chacun, les constats HIGH/MEDIUM de la relecture complète du code
(`factory_v2/audit/CODE_REVIEW_2026-09/*.json`) des catégories **RACE** et **ERROR_HANDLING**, plus les BUG HIGH d'effort S.
Ce sont les familles qui produisent les boucles et les états bloqués (exception avalée → état jamais mis à jour → poll infini).

## Contrat
Pour CHAQUE id ci-dessous : (1) test qui reproduit le défaut (rouge), (2) correctif minimal au fichier/ligne cités, (3) test vert,
(4) ligne dans `factory_v2/audit/CODE_REVIEW_2026-09/REPAIRS.json` : `{"id", "status": "FIXED|WONTFIX", "commit", "test", "note"}`.
`WONTFIX` autorisé seulement avec une justification vérifiable (faux positif démontré par un test). Le juge : chaque id présent dans REPAIRS.json,
chaque FIXED avec un test existant qui passe, suite sûre verte.

## Constats (id · fichier:ligne · catégorie/sévérité · pourquoi) — 15 constats, budget 3 h : viser 100 % FIXED, sinon ≥ 80 %
- `L5_factory_v2_rest_1-011` · `factory_v2/cas_promotion.py:58` · ERROR_HANDLING/HIGH · Un ledger malformé est assimilé à un ledger absent. Une mission déjà promue, correctement refusée au deuxième appel, reçoit à nouveau CAS_ALLOWED après troncature du JSON. La protection de promotion unique se réinitialis
- `L5_factory_v2_rest_1-016` · `factory_v2/external_supervisor.py:118` · ERROR_HANDLING/HIGH · is_quarantined transforme un GLOBAL_QUARANTINE.json corrompu en None, valeur interprétée au démarrage comme autorisation de passer au spawn. Une corruption du marqueur durable peut donc faire disparaître la quarantaine s
- `L5_factory_v2_rest_1-019` · `factory_v2/long_run_preflight.py:137` · ERROR_HANDLING/HIGH · Toute erreur de lecture du lease est convertie en PASS « no lease ». Une PermissionError simulée sur le lecteur mène à no_stale_lease=PASS et au preflight global PASS, alors que l’absence de worker actif n’a pas pu être 
- `L5_factory_v2_rest_2-002` · `factory_v2/run_controlled_long_run.py:177` · ERROR_HANDLING/HIGH · Les deux PIPE du daemon ne sont jamais drainés. Un daemon bavard se bloque avant de produire son jalon et le harnais conclut TIMEOUT. La reproduction utilise un vrai enfant local écrivant 2 Mo ; seuls les lecteurs de set
- `L5_factory_v2_rest_2-003` · `factory_v2/run_controlled_long_run.py:377` · ERROR_HANDLING/HIGH · Le harnais crée un groupe de processus mais son nettoyage ne tue que le PID parent. Les descendants survivent à la fin du run et peuvent continuer à écrire ou consommer des ressources. La reproduction lance un parent et 
- `L6_bin_lib_products_1-006` · `factory/bin/frozen_zone_guard.py:65` · ERROR_HANDLING/HIGH · Un CAMPAIGN_STATE absent ou illisible produit UNKNOWN ; comme UNKNOWN n’appartient pas à FACTORY_ACTIVE_STATES, le garde lève le gel et renvoie succès sans même vérifier le baseline. L’absence d’autorité est ainsi assimi
- `L6_bin_lib_products_1-027` · `factory/lib/completion_detector.py:26` · ERROR_HANDLING/HIGH · Un reçu tronqué lève JSONDecodeError au lieu d’un résultat contrôlé. factoryd.do_poll_iteration appelle cette fonction dans sa boucle des workers sans try local : un seul reçu écrit partiellement ou corrompu interrompt l
- `L6_bin_lib_products_1-031` · `factory/lib/convergence_gate.py:108` · ERROR_HANDLING/HIGH · Chaque source manquante ou illisible devient une liste de gaps vide ; is_converged est ensuite déduit de cette absence. Quand tous les fichiers d’autorité, de campagne et de plan sont absents, la gate annonce True. Ce ré
- `L3-004` · `factory/bin/dispatcher_v3.py:491` · ERROR_HANDLING/MEDIUM · Le chemin TimeoutExpired remplace toutes les sorties par stdout vide et stderr TIMEOUT. Le sous-processus de reproduction imprime et flush des diagnostics sur les deux flux avant de dépasser le délai ; aucune de ces donn
- `L5_factory_v2_rest_1-004` · `factory_v2/_io.py:60` · ERROR_HANDLING/MEDIUM · Le contrat annonce None pour tout JSON illisible/corrompu, mais un octet UTF-8 invalide lève UnicodeDecodeError, hors des exceptions capturées. Les consommateurs comme owner_console peuvent tomber au lieu de gérer l’abse
- `L5_factory_v2_rest_2-005` · `factory_v2/run_lr5_preflight.py:190` · ERROR_HANDLING/MEDIUM · Les erreurs de lecture du journal et de ps sont avalées, puis comptées comme zéro intervention humaine. Une observation entièrement indisponible est présentée comme une mesure concluante d’autonomie.

Reproduction réexéc
- `L5_factory_v2_rest_2-007` · `factory_v2/runrecords.py:124` · ERROR_HANDLING/MEDIUM · read_runs ne tolère aucune ligne JSON tronquée, contrairement aux lecteurs de phases du même module. Après interruption d’un append, une seule fin de ligne incomplète rend toutes les statistiques moteur indisponibles, y 
- `L6_bin_lib_products_1-036` · `factory/lib/progress_watchdog.py:42` · ERROR_HANDLING/MEDIUM · Le fallback fromisoformat accepte un timestamp ISO sans fuseau et renvoie un datetime naïf. snapshot le compare ensuite au now UTC aware et lève TypeError. factoryd transforme l’exception en WATCHDOG_WARN puis passe au w
- `L6_bin_lib_products_2-002` · `factory/lib/required_state_resolver.py:143` · ERROR_HANDLING/MEDIUM · Un registre JSON valide dont contained_capabilities contient un objet provoque TypeError dans set(contained). check() promet un résultat de blocage sans exception pour un registre invalide, mais cette corruption de schém
- `L6_bin_lib_products_2-003` · `factory/lib/research_reuse_gate.py:278` · ERROR_HANDLING/MEDIUM · load_registry ne convertit que ValueError des décisions, alors que une entrée null provoque AttributeError dans from_dict. Une erreur de lecture comme IsADirectoryError remonte aussi brute. check_gate_for_dispatch ne cap

## Interdits
Modifier un test existant pour le faire passer. Toucher ROADMAP.yaml / PROJECT_STATE.json.

## Juge
`python3 factory_v2/verify_code_review_repairs.py --project-repo . --ids-file factory/config/code_review_repair_lot1b_ids.json --repairs-file factory_v2/audit/CODE_REVIEW_2026-09/REPAIRS_LOT1B.json` puis suite sûre. Relecture Codex. Niveau A, budget 3 h.
