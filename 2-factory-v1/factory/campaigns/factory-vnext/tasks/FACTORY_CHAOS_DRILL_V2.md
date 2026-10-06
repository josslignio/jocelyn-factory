# FACTORY_CHAOS_DRILL_V2 — banc de chaos, série 2 : les 8 pannes de la nuit du 19 au 20/09

## Objectif
Chaque panne ci-dessous a coûté une redirection MANUELLE de l'opérateur pendant la nuit du 19→20/09,
alors que la factory doit tourner des jours sans humain. Étendre `factory_v2/chaos_drill.py` d'un jeu
`--set v2` (8 scénarios) qui injecte chacune dans la vraie boucle `factory_v2.runtime.advance()` (ou
dans les primitives réelles de `factory/bin/factoryd.py` pour les pannes du daemon) sur un projet jetable
hermétique, et mesure qu'elle se résorbe seule. La CLI V1 (`--all` sans `--set`) reste STRICTEMENT
identique : ses 12 scénarios sont épinglés par un jalon réglé.

Le contrat exact (ids, reçu, règles) est `SCENARIOS_V2` dans le docstring de
`factory_v2/verify_chaos_drill.py` — le juge, qui RE-EXÉCUTE le banc (`--set v2`).

## Les 8 pannes et le code réel qui doit les résorber (ne pas réinventer)
1. `DAEMON_LOOP_SILENT` — la boucle n'itère plus (future bloquée, pool joint à la sortie) :
   `_loop_watchdog` (`LOOP_WATCHDOG_S`) doit sortir en rc 3 et la reprise ne relance pas deux fois la
   même mission (leases/heartbeats adoptés). Injecter avec un seuil court, observer la sortie + reprise.
2. `ENGINE_BEYOND_BUDGET` — un moteur (stub qui dort) dépasse `budget_s` : arbre de processus tué,
   `ATTEMPT_INTERRUPTED`, nouvelle tentative, aucun processus zombie (`pgrep`).
3. `RELOAD_RACE_FRESH_SUBMIT` — `RELOAD_NOW` dans le même poll qu'une soumission d'advance :
   `_arm_reload` / `_armed_exit_allowed` / `_disarm_reload` (factoryd.py) — jamais de sortie tant
   qu'une future jeune tourne ; sortie sur pool vide ; désarmement si la future a lancé un moteur.
4. `IDLE_GUARD_NEVER_LAUNCHED` — `_idle_guard_tripped` (`milestone_selection.py`) ne doit jamais céder
   un jalon jamais lancé (`last_mine == 0.0`), même si le projet n'a rien lancé depuis 30 min.
5. `INHERITED_WORKTREE_META` — un worktree portant `.factory_worktree_meta.json` d'une autre mission :
   `recover_worktree` (`factory/lib/worktree_manager.py`) le récupère ; pas de boucle d'échec d'archive.
6. `STALE_QUARANTINE_AFTER_FIX` — une quarantaine dont la porte est redevenue verte doit être levée par le
   plan de contrôle seul (DIAGNOSE/VALIDATE_REPAIR), sans édition humaine.
7. `HUNG_TEST_SUBPROCESS` — le `test_cmd` d'une étape laisse un enfant à 0 % CPU au-delà du timeout :
   l'arbre entier est tué (`os.killpg`), l'essai finit `FAIL` proprement, en secondes, pas en heures.
8. `PARKED_WITHOUT_MILESTONE` — une entrée de parking sans `parked_milestone` : l'id est dérivé de la clé
   de mission, le jalon n'est pas resélectionné avant `resets_at`.

## À construire
- `factory_v2/chaos_drill.py` : `--set v2` (et `--scenario <ID>` fonctionne pour les ids V2) ; même
  format de reçu (`chaos-drill-receipt/v1`), `control_sha` = HEAD, `human_interventions: 0`, `evidence`
  ≥ 20 caractères citant les événements/journal observés.
- `tests/test_chaos_drill_v2.py` : un test par scénario (≥ 8), hermétique, < 60 s chacun.
- Correctifs : si un scénario n'est PAS résorbé par le code actuel, corriger le code de contrôle dans la
  même mission, avec test rouge→vert. Ne jamais tricher dans le banc.

## Interdits
Lancer un vrai moteur. Modifier les 12 scénarios V1 ou leurs tests. Modifier ROADMAP.yaml /
PROJECT_STATE.json du dépôt. Déclarer `recovered: true` sans l'événement observé.

## Juge
`python3 factory_v2/verify_chaos_drill.py --set v2 --project-repo .` → `CHAOS_DRILL_VERIFIED` (8/8,
< 900 s chacun, 0 intervention, ≥ 8 tests verts). Relecture Codex/GLM (lecture seule). Niveau A, budget 7 200 s.
