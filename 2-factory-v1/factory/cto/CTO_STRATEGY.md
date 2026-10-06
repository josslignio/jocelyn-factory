<!-- AUTORITÉ DÉPLACÉE 2026-09-07T15:21:00Z — l'autorité canonique est désormais projects/factory/STRATEGY.md. Ce fichier est GELÉ (contenu inchangé pour compatibilité champion). Violation d'intégrité = check C9 de authority_audit.py. -->

# CTO STRATEGY — Stable Vision Document

**Authority**: This document is the stable vision. It changes only on explicit CTO strategic decision.
**The conversation is never the authority. This file is.**

---

## Factory North Star

> Autonomous scalable software-building system with evidence-backed delivery and recovery.
> Build, verify, recover, scale and improve itself without babysitting.

The Factory is done when Jocelyn can return after 72 hours and find:
- missions progressed automatically
- failures detected and repaired without human input
- cross-review performed between model roles
- roadmap updated based on outcomes
- status report current and complete

## Twitter North Star

> Identify financial X accounts and independent clusters whose calls exhibit genuine historical
> edge above market, sector, momentum and randomness.
> Detect new signals before consensus is evident.

The Twitter Radar is done when it can produce automatically:
```
ACCOUNT #1 — @X
187 valid historical calls / 61.2% hit rate / median 90D +14.8%
market-adjusted alpha +6.1% / momentum-adjusted alpha +4.4%
random-entry percentile 94 / PSR 0.93 / DSR positive / OOS PASS
originator_score 0.89 / independence_score 0.91 / lead_days_avg 7.3
top theses: ASTS +147%, XYZ +82%
current active thesis: DEF LONG (3 independent top accounts converging)
```

---

## Principles

### 1. Evidence Hierarchy (never invert this)
1. executable test results
2. runtime evidence (receipts, logs)
3. repository / data state
4. canonical specifications
5. external authoritative source
6. model reasoning

A model never overrides runtime evidence.

### 2. Anti-Hallucination Contract
Every technical claim must be classified:
- `PROVEN` — test or runtime evidence exists
- `OBSERVED` — seen in logs/output
- `INFERRED` — derived from evidence
- `PROPOSED` — hypothesis, not yet tested
- `UNKNOWN` — explicitly not known

"706 tests passed" without a receipt = FORBIDDEN.

### 3. Planner ≠ Builder ≠ Reviewer
For P0/P1 missions:
- Claude → PLAN / CONTRACT
- GLM → IMPLEMENTATION
- Codex → ADVERSARIAL REVIEW

One model cannot be author + reviewer + judge on the same mission.

### 4. No Zombie State
`CAMPAIGN_STATE=RUNNING + active=0 + ready=0` → INVARIANT VIOLATION.
System must automatically: REFILL → REPAIR → RESEARCH → REPLAN → COMPLETE → WAITING_HUMAN.
Never ask "should I continue?" in nominal operation.

### 5. Before Build Hook
Before creating any new module:
1. CHECK component registry
2. CHECK legacy recovery artifacts
3. CHECK research registry
4. CHECK open-source
5. BUILD ONLY the missing delta

### 6. Failure Memory
Every non-trivial bug → `CTO_LESSONS.jsonl` entry with:
- failure signature (hash of error class + stack + failing test)
- root cause
- failed attempts
- working fix
- regression test
- applies_to pattern

Same failure signature on attempt 2 → different model. Attempt 3 → CTO architectural review.

### 7. Human Interruption Policy
Ask Jocelyn ONLY for:
- major strategy change
- paid service authorization
- credentials
- external publication
- irreversible destructive mutation
- legal / licensing decision
- final subjective product acceptance

NEVER ask: "Should I retry?" / "Should I launch the next mission?" / "Should I continue?"

---

## Autonomous CTO Loop (target behavior)

```python
while not global_done():
    reconcile_runtime()
    detect_completed(); detect_failed(); detect_stalled()
    qualify_candidates()
    update_cto_state()

    if significant_event():
        cto_review()

    resolve_ready_repairs()
    ready = dag.ready_missions()

    if ready:
        dispatch_best_ready(ready)
    elif has_researchable_blocker():
        schedule_research()
    elif requires_replan():
        cto_replan()
    elif only_human_gates_remain():
        transition("WAITING_HUMAN")
    else:
        raise CONTROL_PLANE_DEFECT
```

---

## Lanes (always maintain productive work across lanes)

| Lane | Content |
|------|---------|
| A | Factory Core (F-track) |
| B | Twitter Product (X-track) |
| C | Qualification / Testing |
| D | Research / Optimisation (XR-track) |

A blocker in one lane does NOT stop the program.

---

## Definition of Done — Autonomous CTO System

Run a representative campaign for several hours. Jocelyn gives zero instructions.
At the end, `CTO_STATUS_LATEST.md` must show without recalculation:
- what was built
- what is running
- what failed and how it was repaired
- what was learned
- what research was used
- why the roadmap changed
- what runs next
- no manual "continue?" issued
