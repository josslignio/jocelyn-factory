# SCALE_2

## Outcome

(verbatim `goal` du jalon `SCALE_2` — projects/factory/ROADMAP.yaml ; section ajoutée le 2026-09-12 pour satisfaire le préflight check_task NO_OUTCOME_MARKER ; le gate recalcule les acceptances, il ne lit pas cette section.)

MAX_WORKERS 1→2 uniquement si throughput amélioré ET fiabilité non régressée sur benchmark réel


Derived task brief. NOT canonical intent: the authority for this work is
`projects/factory/STRATEGY.md` / `SPEC.md` / `ROADMAP.yaml` at the frozen contract SHAs.
If this brief and the ROADMAP acceptance ever disagree, the ROADMAP wins and this file is
wrong. Do not edit ROADMAP.yaml, PROJECT_STATE.json, any verifier, any credential or any
owner authority from inside this mission.

## Deliverables (from ROADMAP)
- factory_v2/receipts/SCALE_2_BENCHMARK_RECEIPT.json

## Acceptance (verbatim from ROADMAP — the gate recomputes these, it does not read claims)
- measured throughput improvement
- perfect context isolation
- no qualification regression
- elastic plan/research fan-out and cross-product knowledge reuse

## Bounded scope
- OPTIONAL work: scaling is not part of FACTORY_AUTONOMOUS_CTO_BASE_READY and must never
  block it. Run this only after base readiness is independently proven.
- 1-worker baseline and 2-worker run on the SAME workload, or the comparison is void.
- Every receipt claim needs its measurement attached (evidence/receipt/run_ids).
- A measured no-improvement is a valid result: report it and do not scale.

## Stop conditions
- Budget exhausted → stop and leave the evidence written. A truthful incomplete run beats
  an authored receipt.
- Any acceptance clause you cannot produce independent evidence for → leave it unmet and
  say so in the receipt. `VERIFIER_NOT_IMPLEMENTED` / unmet is an outcome; a fabricated
  boolean is a defect.
- Never write a receipt field the verifier would grade unless a run produced it.
