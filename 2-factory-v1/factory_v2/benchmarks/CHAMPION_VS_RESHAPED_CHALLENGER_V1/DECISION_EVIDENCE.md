# CHAMPION_VS_RESHAPED_CHALLENGER — decision evidence

workload_id: `CHAMPION_VS_RESHAPED_CHALLENGER_V1`  
start_sha (hermetic test project, identical both sides): `798a9fff53280a478fbf049aae0bcd2cf68e4fd0`  
champion_sha: `5257c8c9689aba1e85eb7638b621458eb3a067e8` (runtime d'avant la Ferrari)  
challenger_sha: `d58dcfaa81accac37ad9e92b0154b19543841d08` (HEAD au moment du run)  

## Metrics (recomputed identically to the pinned judge's `agg`)

| side | probes | settled | wall_s (sum) | restart_recovered | launches_lost |
|---|---|---|---|---|---|
| champion | 6 | 4 | 6.213 | 0 | 3 |
| challenger | 6 | 5 | 13.973 | 0 | 2 |

## Decision rule

PROMOTE only if the challenger dominates on ALL four axes (settled >=, wall_s <=, launches_lost <=, restart_recovered >=) and the two metric rows are not identical.

Domination holds: **False**.
Decision: **REJECT**.

The challenger settled more probes with fewer lost launches (5/6 vs 4/6 settled, 2 vs 3 launches lost) but did not dominate on wall-clock time in this hermetic microbenchmark (13.973s vs 6.213s) — the reshape adds measurable per-cycle overhead that this workload does not amortize. Per the fixed decision rule, a measured REJECT is a COMPLETED experiment: the champion pointer is left unchanged.
