# FACTORY_CHAOS_DRILL_V1 — banc de chaos : les 12 pannes réelles, résorbées sans humain

## Objectif
On ne teste plus la robustesse en production. Construire `factory_v2/chaos_drill.py` : un banc
**hermétique** (FACTORY_NO_REAL_ENGINE=1, exécuteurs stubs, projet jetable créé dans un tmpdir avec
STRATEGY/SPEC/ROADMAP minimaux) qui fait tourner la vraie boucle `factory_v2.runtime.advance()` et
injecte, un scénario à la fois, les 12 pannes réellement subies entre le 12 et le 19/09, puis mesure
que la factory les résorbe **seule** (0 intervention) en moins de 15 min (en pratique : quelques
cycles d'`advance()`, secondes).

Le contrat exact (liste des 12 scénarios, format du reçu, règles) est le docstring de
`factory_v2/verify_chaos_drill.py` — le juge. Il RE-EXÉCUTE le banc : un reçu écrit à la main ne vaut rien.

## À construire
1. `factory_v2/chaos_drill.py` — CLI `--all --out <json>` (et `--scenario <ID>`) :
   - `make_disposable_project(tmp)` : projet jetable avec 2-3 jalons `local_command` / stub autopilot,
     `PROJECT_STATE.json` vierge, contrats gelables.
   - Un exécuteur stub injectable (`executor=` d'`advance()`) qui simule : build OK, build coupé quota
     (stderr « You've hit your usage limit », rc≠0 — cf. `factory_v2/resilience.py::_QUOTA_PATTERNS`),
     moteur tué (rc=-15), refus moteur (message de filtre), candidat sans livrable, etc.
   - Une relecture stub (`review_dispatch.dispatch_review` monkeypatchée par `route=`/`runner=`) qui rend
     BLOCK puis ACCEPT, et un roster déclaré impossible (constructeur ∈ relecteurs, min 2, 1 restant).
   - Pour chaque scénario : injecter, boucler `advance()` (≤ 60 cycles, horloge réelle), observer les
     événements de `PROJECT_STATE.json` et écrire `{id, injected, recovered, recovery_cycles,
     recovery_s, human_interventions: 0, evidence}` où `evidence` cite les événements vus
     (ex. `ATTEMPT_INTERRUPTED(QUOTA_CUT) → MISSION_PASS → MILESTONE_SETTLED`).
   - « Résorbé » = le jalon visé finit `MILESTONE_SETTLED` **ou** la factory a rotaté vers un autre
     jalon qui se règle, sans que le même événement bloquant se répète > 3 fois.
2. `tests/test_chaos_drill.py` : un test par scénario (≥ 12) qui appelle le banc sur ce seul scénario
   et affirme `recovered` ; hermétique, < 60 s par test.
3. Correctifs : si un scénario n'est PAS résorbé par le code actuel, **corriger le code de contrôle**
   (`factory_v2/`, `factory/bin/factoryd.py`) dans la même mission — c'est le but du banc — avec le
   test rouge→vert comme preuve. Ne jamais tricher dans le banc pour faire passer un scénario.

## Références du code réel à réutiliser (ne pas réinventer)
`factory_v2/resilience.py` (classification des coupures), `factory_v2/advance/settlement.py`
(BLOCK → reconstruction, roster auto-dégradé, `_mai_*`), `factory_v2/advance/milestone_selection.py`
(garde 30 min `IDLE_GUARD_S`, rotation), `factory_v2/launch_gate.py` (plafond), `factory/bin/factoryd.py`
(drain reload — testable via `_reload_in_flight` / `_begin_reload_drain`), `factory/bin/dispatcher_v3.py`
(`_merge_candidate` bookkeeping). Les tests existants dans `tests/test_resilience.py`,
`tests/test_hot_reload.py`, `tests/test_intra_project_parallel.py` montrent comment monter un projet jetable.

## Interdits
Lancer un vrai moteur. Modifier ROADMAP.yaml ou PROJECT_STATE.json du dépôt. Écrire un reçu sans
exécuter le banc. Déclarer `recovered: true` sur une simple absence d'erreur : il faut l'événement de
règlement ou de rotation observé.

## Juge
`python3 factory_v2/verify_chaos_drill.py --project-repo .` → `CHAOS_DRILL_VERIFIED` (12/12, < 900 s,
0 intervention, tests ≥ 12 verts). Relecture Codex (lecture seule). Niveau A, budget 7 200 s.
