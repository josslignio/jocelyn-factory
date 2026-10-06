# FACTORY_RELOAD_CANARY_V1 — canari avant rechargement à chaud

## Objectif
Avant `RELOAD_NOW` (sys.exit(3) → launchd relance sur le nouveau code), exécuter un canari : un sous-processus Python qui importe factory_v2 + factory/bin/factoryd au HEAD courant et exécute un `advance()` hermétique sur un projet jetable (local_command `true`, FACTORY_NO_REAL_ENGINE=1). Canari rouge → pas de reload, incident typé `RELOAD_CANARY_FAILED` durable + exception console propriétaire, nouvelle tentative au prochain settlement.

## À construire
1. `factory_v2/hot_reload.py` : `reload_canary(repo_root, *, python=sys.executable, timeout_s=120) -> (ok: bool, why: str)` ; `why` commence par `RELOAD_CANARY_FAILED:` en cas d'échec (import, exception, timeout, action inattendue).
2. `factory/bin/factoryd.py` : appeler `reload_canary` dans la décision RELOAD_NOW ; rouge → `_reload_pending` reste vrai, événement + `progress_incidents.jsonl`.
3. Tests : arbre sain → ok ; arbre cassé (ImportError) → rouge typé ; timeout.

## Juge épinglé
`reload-canary` : câblage AST + drill (canari vert sur HEAD, rouge sur un arbre cassé).
