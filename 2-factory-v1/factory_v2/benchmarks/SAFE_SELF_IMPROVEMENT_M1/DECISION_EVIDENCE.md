# CHAMPION_VS_RESHAPED_CHALLENGER — decision evidence

workload_id: `SAFE_SELF_IMPROVEMENT_M1`  
start_sha (hermetic test project, identical both sides): `2805d31254d4fbc3cc29f3e423861b79c48da09e`  
champion_sha: `5257c8c9689aba1e85eb7638b621458eb3a067e8` (runtime d'avant la Ferrari)  
challenger_sha: `af8f0ed276d384343084456a259d1618a22160a4` (HEAD au moment du run)  

## Metrics (recomputed identically to the pinned judge's `agg`)

| side | probes | settled | wall_s (sum) | restart_recovered | launches_lost |
|---|---|---|---|---|---|
| champion | 6 | 4 | 8.91 | 0 | 3 |
| challenger | 6 | 5 | 20.291 | 0 | 2 |

## Decision rule

PROMOTE only if the challenger dominates on ALL four axes (settled >=, wall_s <=, launches_lost <=, restart_recovered >=) and the two metric rows are not identical.

Domination holds: **False**.
Decision: **REJECT**.

The challenger settled more probes with fewer lost launches (5/6 vs 4/6 settled, 2 vs 3 launches lost) but did not dominate on wall-clock time in this hermetic microbenchmark (20.291s vs 8.91s) — the reshape adds measurable per-cycle overhead that this workload does not amortize. Per the fixed decision rule, a measured REJECT is a COMPLETED experiment: the champion pointer is left unchanged.

## Session note — SAFE_SELF_IMPROVEMENT#M1 (2026-09-18)

- Identités : champion `5257c8c9689aba1e85eb7638b621458eb3a067e8` (valeur du pointeur
  `factory_v2/CHAMPION_POINTER` avant ET après le run) ; challenger
  `af8f0ed276d384343084456a259d1618a22160a4` (base de la mission) ; start_sha hermétique
  `2805d31254d4fbc3cc29f3e423861b79c48da09e` (identique des deux côtés, fixture committée
  à dates/identités fixes).
- Producteur : `factory_v2/run_matched_benchmark.py` exécuté DEPUIS le checkout committé,
  inchangé (MANIFEST.producer_sha256 == sha256(HEAD:producer)).
- Décision re-dérivée par le juge épinglé b54a99967ef2cfc04e63f6c6afc939c7cd93a97a
  (`check_matched_benchmark` recompte les métriques depuis les observations
  content-addressed, `check_self_improvement` vérifie snapshots/ledger/CAS) : les deux
  renvoient VERIFIED à HEAD après le commit B.
- Ledger : re-lié au sha256 de CE reçu (une ligne validée, chaîne recomputée) ; les lignes
  antérieures — liées au reçu `08eef34a…` remplacé au commit 4a734ed71 — restent dans
  l'historique git (93feffc76) mais n'étaient plus liables à aucun fichier existant à HEAD.
- Un REJECT mesuré est une expérience COMPLÈTE : le champion reste, aucune promotion,
  `promotions: []`, pointeur inchangé (vérifié par le juge qui lit le pointeur À HEAD).
