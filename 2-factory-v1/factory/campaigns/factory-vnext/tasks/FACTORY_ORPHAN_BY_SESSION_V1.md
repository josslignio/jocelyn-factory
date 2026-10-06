# FACTORY_ORPHAN_BY_SESSION_V1 — orphelins par appartenance de session, pas par nom de commande

## Objectif
Un processus moteur est un orphelin parce que le bail (lease) qui l'a lancé est fermé ou expiré — pas parce que sa ligne de commande ressemble à `codex exec`. Registre durable des processus lancés + balayage fondé sur ce registre.

## À construire
1. `factory_v2/engine_registry.py` : `register_engine_process(run_or_project_dir, *, lease_id, mission_id, pid)` écrit (pid, pgid, sid, birth_time, lease_id, mission_id, registered_at) dans `evidence/engine_registry.jsonl` (append, fsync). `orphans_from_registry(dir, *, live_leases:set, max_age_s)` → liste des entrées dont le pid est vivant (même birth_time — garde contre la réutilisation de pid), dont le lease n'est pas dans `live_leases` OU dont l'âge dépasse `max_age_s`. `kill_orphans(dir, entries)` : SIGTERM au groupe, puis SIGKILL après 5 s ; journalise `ORPHAN_ENGINE_KILLED`.
2. `factory/bin/dispatcher_v3.py` : à chaque execve d'un moteur (`_run`), appeler `register_engine_process(...)` avec le lease courant (`FACTORY_LEASE_ID` / attempt_key en env) — jamais un launch sans enregistrement.
3. `factory/bin/factoryd.py` : `_sweep_orphan_engines` utilise `orphans_from_registry(...)` avec les leases vivants lus des missions en vol ; la regex argv ne reste qu'en filet de sécurité secondaire (ou disparaît).
4. Tests hermétiques (sous-processus `sleep`, pid réutilisé, lease vivant non tué).

## Juge épinglé
`orphan-by-session` (factory_v2/verify_factory_hygiene.py) : câblage AST (`register_engine_process` dans dispatcher, `orphans_from_registry` dans factoryd), drill réel (processus enregistré → lease fermé → flagué → tué ; lease vivant → jamais flagué).
Interdit : tuer un processus dont le lease est vivant ; enregistrer sans birth_time.
