# SCALE_N

## Outcome

(verbatim `goal` du jalon `SCALE_N` — projects/factory/ROADMAP.yaml ; section ajoutée le 2026-09-12 pour satisfaire le préflight check_task NO_OUTCOME_MARKER ; le gate recalcule les acceptances, il ne lit pas cette section.)

Généralisation du scaling à N workers sur preuve SCALE_2 établie


Derived task brief. NOT canonical intent: the authority for this work is
`projects/factory/STRATEGY.md` / `SPEC.md` / `ROADMAP.yaml` at the frozen contract SHAs.
If this brief and the ROADMAP acceptance ever disagree, the ROADMAP wins and this file is
wrong. Do not edit ROADMAP.yaml, PROJECT_STATE.json, any verifier, any credential or any
owner authority from inside this mission.

## Deliverables (from ROADMAP)
- factory_v2/receipts/SCALE_N_BENCHMARK_RECEIPT.json

## Acceptance (verbatim from ROADMAP — the gate recomputes these, it does not read claims)
- N > 2 workers on a real workload
- linear throughput scaling, or a documented justification
- perfect context isolation at N workers
- no regression vs SCALE_2

## Bounded scope
- OPTIONAL, and gated on a real SCALE_2 measurement: scale_2_baseline.throughput must be
  a finite measured number, not an assertion.
- Sub-linear scaling is acceptable WITH sub_linear_scale_justification. Fabricating a
  linear number is not.
- Stop at the first isolation failure; a corrupted candidate ancestry ends the run.

## Stop conditions
- Budget exhausted → stop and leave the evidence written. A truthful incomplete run beats
  an authored receipt.
- Any acceptance clause you cannot produce independent evidence for → leave it unmet and
  say so in the receipt. `VERIFIER_NOT_IMPLEMENTED` / unmet is an outcome; a fabricated
  boolean is a defect.
- Never write a receipt field the verifier would grade unless a run produced it.
