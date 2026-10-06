# FACTORY_AUTONOMOUS_CTO_BASE_READY

## Outcome

(verbatim `goal` du jalon `FACTORY_AUTONOMOUS_CTO_BASE_READY` — projects/factory/ROADMAP.yaml ; section ajoutée le 2026-09-12 pour satisfaire le préflight check_task NO_OUTCOME_MARKER ; le gate recalcule les acceptances, il ne lit pas cette section.)

Readiness de base: tous les compteurs runtime de l'Operating Model 015 atteints


Derived task brief. Authority is `projects/factory/{STRATEGY.md,SPEC.md,ROADMAP.yaml}` at the
frozen contract SHAs; where this brief and the ROADMAP acceptance disagree, the ROADMAP wins.
Do not edit any verifier, PROJECT_STATE, credential or authority document from inside the
mission. Build the independent qualification aggregator.

## Why this is an implementation task

The recipe for this milestone was verify-only, and its gate is itself deferred. Re-running an
unimplemented verifier forever is not a path to readiness. Acceptance requires a receipt
"produit par un vérificateur indépendant, jamais auto-attesté" — so the aggregator that
produces it must exist.

## Deliverables

An aggregator that RECOMPUTES each base-readiness metric from the durable evidence its
dependency milestones produced — `MANUAL_DAEMON_RESTARTS`, `MANUAL_AGENT_HANDOFFS`,
`MANUAL_AUDIT_TRIGGERS`, `ROUTINE_HUMAN_INTERVENTIONS`, `FALSE_SETTLEMENTS`,
`CONTEXT_REEXPLANATIONS`, `EXACT_SHA_REVIEWS_AUTOMATED`, `MULTI_AI_DECISIONS_SYNTHESIZED`,
`MATCHED_BENCHMARK_RUNS_PER_ARM`, `FACTORY_ADVANTAGE_MEASURED`,
`SAFE_SELF_IMPROVEMENT_DECISIONS` — plus the gate that grades it.

Each metric names its source records. A metric with no source is `UNKNOWN`, and `UNKNOWN`
blocks readiness. `0` means "0 observed over an explicit window", never "risk-free".

## Reuse

`settlement_truth`, `progress_truth`, `runrecords.engine_stats` (whose denominator now counts
every attempt), the receipts of the dependency milestones. Scaling (`SCALE_2`/`SCALE_N`) is
NOT part of base readiness — `product_definition` says the scaling requires the base, not the
reverse.

## Negative cases / budget / stop

Negative: a metric read from a receipt the auditee wrote must not count; a missing dependency
receipt yields `UNKNOWN`, not `0`; an authored aggregate must fail. Budget 5400 s. If any
metric has no independent source, emit it as `UNKNOWN` and leave the milestone unsettled.
