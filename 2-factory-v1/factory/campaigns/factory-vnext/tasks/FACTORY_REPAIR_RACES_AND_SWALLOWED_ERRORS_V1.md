# FACTORY_REPAIR_RACES_AND_SWALLOWED_ERRORS_V1 — réparer les défauts qui font boucler la factory

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
- `L3-008` · `factory/bin/factoryd.py:1373` · RACE/HIGH · Les futures ont des clés par slot mais le détail et les fichiers touchés sont stockés uniquement par projet. Deux avances du même projet terminées avant la récolte écrasent le même enregistrement. La reproduction fait pr
- `L3-017` · `factory/bin/model_router.py:395` · RACE/HIGH · La lecture du journal, le contrôle du cap et l’ajout de la réservation ne sont pas atomiques. Deux appels synchronisés juste après leur lecture du même journal vide avec cap=1 sont tous deux autorisés et produisent deux 
- `L5_factory_v2_rest_1-003` · `factory_v2/_io.py:44` · RACE/HIGH · Deux écritures visant le même chemin utilisent le même inode temporaire. Dans l’entrelacement reproduit, B remplace le fichier temporaire de A et termine, puis A échoue FileNotFoundError. D’autres entrelacements peuvent 
- `L5_factory_v2_rest_1-005` · `factory_v2/authority.py:94` · RACE/HIGH · La clé de cache utilise le SHA lu avant git show, mais git show relit la référence mobile HEAD. Un commit entre ces deux commandes associe le blob B au SHA A ; revenir ensuite sur A sert B depuis le cache. La reproductio
- `L5_factory_v2_rest_2-008` · `factory_v2/runrecords.py:201` · RACE/HIGH · La réconciliation ferme toute PREDICTION sans OBSERVATION comme WORKER_DEATH sans vérifier la vie du worker ni le projet. Elle est appelée au début des cycles (advance/control_plane.py:122-124), pendant que d’autres miss
- `L6_bin_lib_products_1-002` · `factory/bin/budget_ledger.py:104` · RACE/HIGH · Deux enregistrements qui lisent la même version du JSONL remplacent ensuite chacun le fichier entier : un des deux appels réussis disparaît. Le compteur sous-estime alors les tokens/coûts utilisés. La barrière du test im
- `L5_factory_v2_rest_2-009` · `factory_v2/self_modification_ledger.py:67` · RACE/MEDIUM · Le calcul du précédent hash et l’append ne sont pas sérialisés. Deux écrivains lisant le même dernier hash ajoutent deux lignes qui prétendent être ses successeurs directs ; le registre devient invalide malgré deux appen
- `L5_factory_v2_rest_2-016` · `factory_v2/verified_fix.py:404` · RACE/MEDIUM · store_receipt écrit chaque reçu puis remplace INDEX.json après une lecture sans verrou. Dans un entrelacement de deux stockages, les deux appels réussissent mais le dernier index ne contient qu’un reçu : load_referenced 
- `L6_bin_lib_products_1-010` · `factory/bin/memory_store.py:190` · RACE/MEDIUM · append_lesson lit puis réécrit l’intégralité du fichier sans verrou ; les commandes promote-lesson sont explicitement hors du verrou Factory. Deux promotions distinctes peuvent réussir et perdre une leçon. La reproductio
- `L6_bin_lib_products_2-006` · `factory/lib/supervisor_state.py:68` · RACE/MEDIUM · Deux écritures du même état partagent exactement le même fichier .tmp. Avec les deux écritures terminées avant les remplacements, le premier remplacement consomme le temporaire et le second lève FileNotFoundError ; la va
- `L6_bin_lib_products_2-007` · `factory/lib/worktree_manager.py:133` · RACE/MEDIUM · base_commit est résolu avant la création, mais git worktree add reçoit ensuite la référence mobile base_branch. Si HEAD avance entre les deux, le worktree contient le nouveau commit et sa métadonnée base_commit reste l’a
- `L6_bin_lib_products_2-022` · `products/twitter_radar/weekly_observation.py:155` · RACE/MEDIUM · L’index est mis à jour par lecture-modification-remplacement sans verrou. Deux stores réussis peuvent publier chacun une version issue du même index ; une observation durable devient alors invisible à list_valid_observat
- `L3-002` · `factory/bin/dispatcher_v3.py:952` · ERROR_HANDLING/HIGH · Après un reçu ACCEPTED/promotable=True et un échec de fusion, seule la copie retournée devient BLOCKED. Le reçu déjà écrit reste ACCEPTED et même la copie conserve promotable=True. La reproduction exerce _run_in_worktree
- `L3-009` · `factory/bin/factoryd.py:1264` · ERROR_HANDLING/HIGH · v2_status_summary représente un état illisible par observed=False et in_flight=0 sans lever d’exception. Les deux compteurs de drainage (_project_in_flight et _reload_in_flight) prennent ce zéro pour une absence de trava
- `L3-010` · `factory/bin/factoryd.py:1057` · ERROR_HANDLING/HIGH · Le contrat de _live_engine_lease_ids promet None si l’appartenance ne peut pas être établie, mais les erreurs de lecture d’état sont sautées et la fonction retourne un ensemble vide. Dans la reproduction, état et lease i

## Interdits
Modifier un test existant pour le faire passer. Toucher ROADMAP.yaml / PROJECT_STATE.json.

## Juge
`python3 factory_v2/verify_code_review_repairs.py --project-repo . --ids-file factory/config/code_review_repair_lot1_ids.json --repairs-file factory_v2/audit/CODE_REVIEW_2026-09/REPAIRS.json` puis suite sûre. Relecture Codex. Niveau A, budget 3 h.
