# FACTORY_QUOTA_THRIFT_V1 — le quota moteur est mesuré, protégé et optimisé

## Objectif
Le 20/09, ~5 h de GLM (60 % des minutes-moteur de la journée) ont été perdues : 3 dispatchers de prod tués
deux fois par un banc non hermétique, moteurs orphelins qui continuaient à brûler du quota, redémarrages à zéro.
Ordre propriétaire : « ne plus jamais cramer du quota comme ça, s'en rendre compte rapidement, et faire l'inverse :
optimiser le quota ». `factory_v2/quota_waste.py` (déjà livré) mesure les minutes-moteur perdues ; ce jalon rend la
factory capable de les voir, de réagir et de les éviter SEULE.

## À construire (le contrat exact est le docstring de `factory_v2/verify_quota_thrift.py`, le juge)
1. **Attribution honnête** : plus aucun essai TERMINAL avec `engine: UNKNOWN` — `attempt_accounts.jsonl` nomme le
   moteur réel et la cause (`exit_class` + `reconciled_reason` : PROCESS_LOST, BUDGET_KILL, QUOTA_CUT, ORPHAN_SWEEP,
   STALE_BASE, REVIEW_BLOCK…). Corriger le producteur (`factory_v2/attempt_ledger.py` / dispatcher) pas le rapport.
2. **`factory_v2/quota_throttle.py::effective_workers(view, configured)`** : nombre de workers effectif d'après la
   vue quota (`quota_observer.merged_view`) — p. ex. hebdo ≥ 75 % utilisé → moitié des slots ; ≥ 90 % → 1 ; jamais 0,
   jamais > configured. `factoryd` l'appelle pour plafonner `_advance_parallel` (et le journalise quand ça change).
3. **Incident** : `QUOTA_WASTE_INCIDENT` émis par factoryd (log + événement PROJECT_STATE) quand les minutes perdues
   sur 3 h dépassent un seuil (défaut 60) — avec la cause dominante ; l'incident borne le parallélisme (throttle à 1)
   jusqu'à ce que 3 h glissantes repassent sous le seuil. Pas de polling supplémentaire : calcul dans le cycle de
   métriques existant (`_generate_metrics_report`) et ligne dans `BUILD_METRICS_LATEST.md`.
4. **Jamais jeter un PASS** : quand HEAD bouge pendant/après un build, le candidat accepté est rebasé (ou fusionné
   avec résolution bookkeeping) plutôt que la mission redémarrée à zéro ; le worktree « base périmée » n'est archivé
   que si le rebase échoue réellement (conflit de code). Test hermétique rouge→vert.
5. `tests/test_quota_thrift.py` (≥ 6 tests, hermétiques, < 60 s chacun).

## Interdits
Lancer un vrai moteur. Affaiblir un test existant. Toucher ROADMAP.yaml / PROJECT_STATE.json. Zone gelée `campaigns/RUN8/**`.

## Juge
`python3 factory_v2/verify_quota_thrift.py --project-repo .` → `QUOTA_THRIFT_VERIFIED`. Niveau A, budget 10 800 s.
