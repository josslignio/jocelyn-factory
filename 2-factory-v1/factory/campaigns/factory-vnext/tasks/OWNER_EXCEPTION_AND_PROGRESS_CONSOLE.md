# OWNER_EXCEPTION_AND_PROGRESS_CONSOLE

Derived task brief. NOT canonical intent — voir projects/factory/ROADMAP.yaml. Ne modifie aucun fichier d'autorite ni le
juge (execute depuis le commit epingle b54a99967ef2). Pas de git stash. Committe avant de lancer le juge (il lit HEAD).

## Outcome
Console proprietaire en lecture seule : `python3 -m factory_v2.owner_console --out <dir> --projects projects/factory
projects/twitter-radar` produit OWNER_CONSOLE.md (francais simple, lisible en 2 minutes) + OWNER_CONSOLE.json ; juge
owner-console rc=0 ; recu + docs/OWNER_CONSOLE.md + plist optionnel.

## Acceptance (copie de la roadmap)
- Decision CTO 2026-09-13 : factory_v2/owner_console.py, LECTURE SEULE (aucun import de runtime/executors/launch_gate/dispatcher/recovery_allocation, n ecrit que sous --out), CLI `python3 -m factory_v2.owner_console --out <dir> --projects projects/factory projects/twitter-radar` produit OWNER_CONSOLE.json + OWNER_CONSOLE.md avec 4 sections : exceptions (id, project, kind dans {OWNER_DECISION_REQUIRED, MAINTENANCE_REQUEST, AUTHORITY_AMENDMENT_PENDING, VISUAL_VALIDATION, GRANT_NEEDED, CREDENTIAL_OR_PAYMENT, PRODUCT_DECISION}, since, source = chemin d artefact existant, suggested_action), progress par projet (total, complete, current_milestone, in_flight, restants), increments_24h (settlements des 24 h), health (daemon_pid, last_heartbeat_age_s, standing_cap, quota par moteur, parked, loops)
- Aucune attente auto-resoluble (STANDING_CAP_REACHED, WAITING_FOR_EVIDENCE, CALENDAR_WAIT, RELOAD_SCHEDULED, COST_NO_PROGRESS_TERMINAL, CNP_REGRANT_WITHHELD, RECOVERY_LAUNCH_BLOCKED) n est presentee comme exception (AUTO_RESOLVABLE_HUMAN_BLOCK = 0) ; chaque exception est typee VALID human gate et cite sa source
- Juge independant EPINGLE au commit b54a99967ef2 (verify_factory_hygiene.py owner-console) : execute la CLI dans un tmp et recalcule progression (PROJECT_STATE + ROADMAP), increments 24 h (MILESTONE_SETTLED), types et sources des exceptions, champs de sante ; rc=0 a HEAD
- Recu factory_v2/receipts/OWNER_EXCEPTION_CONSOLE_RECEIPT.json (schema owner-console-receipt/v1 : generated_at, exceptions_count, auto_resolvable_hidden_count, control_sha) ; docs/OWNER_CONSOLE.md documente la CLI et un plist launchd optionnel (toutes les 15 min vers ~/.local/share/factory-console/)
- Suite sure verte : python3 -m pytest tests -q -p no:cacheprovider -x --ignore=tests/test_stabilization_proof.py --ignore=tests/test_external_supervisor_production_path.py --ignore=tests/test_lr4_interprocess.py --ignore=tests/test_p0_supervision_repairs.py --ignore=tests/test_sol_ladder_fairness.py --ignore=tests/test_telemetry_integration.py --ignore=tests/test_tr_weekly_refresh_verifier.py

## Sources a lire (lecture seule)
- projects/<p>/PROJECT_STATE.json (events, completed_milestones, current_milestone, missions), projects/<p>/ROADMAP.yaml
- projects/<p>/maintenance_requests/*.json (state OPEN => MAINTENANCE_REQUEST), projects/<p>/DURABLE_HANDOFF.json
- projects/<p>/evidence/attempt_accounts.jsonl (in_flight = OPEN sans TERMINAL), journal recovery_allocations (standing cap)
- run-dir du daemon ~/Library/Application Support/JocelynFactory/run : factoryd.pid, BUILD_METRICS_LATEST.json,
  QUOTA_SNAPSHOT.json, run_windows.jsonl ; log ~/Library/Logs/JocelynFactory/factoryd.stdout.log (dernier HEARTBEAT)
- Depot produit Twitter Radar docs/PRESENTATION_SCREENSHOTS => exception VISUAL_VALIDATION tant qu'aucune decision owner
  n'est enregistree (fichier docs/PRESENTATION_SCREENSHOTS/OWNER_DECISION.json)
- Tout ce qui est calendaire/auto-resoluble va dans Sante (attentes), jamais en exception ; le juge porte la liste exacte des kinds.

## Raisons PARTIAL typees
TOOL_MISSING | DATA_INSUFFICIENT | BUDGET_EXHAUSTED.
