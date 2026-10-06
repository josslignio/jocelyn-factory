---
schema: autonomy-contract/v1
produced_at: "2026-09-06T12:00:00Z"
authority: "Compiled from CTO_STRATEGY.md Principle 7 + CTO_STATE human_gates + CTO_LESSONS L006/L009"
version: "v1"
---

# AUTONOMY CONTRACT — v1

## Definition

This document defines the boundary between autonomous Factory decisions and human decisions.
The system executes autonomously within this contract. Any deviation is an AUTONOMY_DEBT and must be
recorded. Violations are bugs, not features.

---

## Rule 1 — Routine Technical Decisions: NO HUMAN

The Factory makes these decisions autonomously. Never ask Jocelyn:

| Decision | Autonomous Action |
|----------|------------------|
| Mission failed → retry | Retry with same model (attempt 1) |
| Same failure signature (attempt 2) | Route to different model per CTO_LESSONS |
| Same failure signature (attempt 3) | Trigger CTO architectural review |
| Mission stalled (heartbeat stale + PID dead) | REQUEUE (not BLOCKED) |
| Stale-active on restart | Reconcile: requeue or verify completion |
| Active=0, ready missions exist | Auto-dispatch next ready mission |
| Backlog below REFILL_THRESHOLD | H2→H1 promotion via cto_planner |
| Test repair needed (bounded, within mission) | Auto-repair (T2 path in dispatcher_v3) |
| Model escalation (GLM timeout >1800s) | Route to Claude |
| Next mission in queue | Auto-launch without human input |
| Bookkeeping commit (PLAN.md, REPORT.json) | Pass through stop_gate (BOOKKEEPING_SAFE_PREFIXES) |
| CONTINUABLE_BLOCKED verdict | Continue to next mission automatically |
| Campaign lane blocked | Continue other lanes |

**"Should I retry?" / "Should I launch the next mission?" / "Should I continue?" are FORBIDDEN questions.**

---

## Rule 2 — Human Gates: STOP AND WAIT

The Factory stops and records a human gate only for:

| Category | Examples |
|----------|---------|
| **Spend / paid services** | New API subscription, paid scraping quota, >$X token budget |
| **Credentials / secrets** | New API keys, OAuth tokens, SSH access |
| **Irreversible destructive** | Delete canonical data, drop production database, purge corpus |
| **External publication** | Push to public repo, tweet, publish report, contact third party |
| **Genuine legal ambiguity** | License exception requests, GPL usage decisions requiring counsel |
| **Explicit subjective product validation** | "Is this the right ranking?" / "Is this the right output format?" |
| **QRTS promotion** | promote_qrts() requires QualificationResult(passed=True) — human validates qualification run |
| **Major strategy change** | Abandon a North Star, pivot product direction |

Human gates are recorded in `CTO_STATE.json → human_gates[]` with status WAITING_HUMAN.

---

## Rule 3 — AUTONOMY_DEBT: Mandatory Recording

Any manual bypass of the autonomous loop (Jocelyn doing what the system should have done) must
be recorded:

```jsonl
{"id":"AD-001","date":"2026-09-06","bypass":"Launched XR01 manually instead of factoryd auto-dispatch","reason":"factoryd deadlocked on missing plan files (F.1 finding)","fix_mission":"CTO002","resolved":"CTO002 ACCEPTED"}
```

File: `factory/cto/AUTONOMY_DEBT.jsonl`

AUTONOMY_DEBT entries are inputs for:
- CTO replan (pattern of same debt → architectural fix)
- SOAK audits (HUMAN_GO_COUNT must match number of planned human gates, not bypass events)
- CTO000B counter-audit (unexpected autonomy debt → gap discovery)

---

## Rule 4 — Zombie State is Always a Bug

```
CAMPAIGN_STATE=RUNNING + active=0 + ready=0 → INVARIANT VIOLATION
```

The system must automatically cycle through: REFILL → REPAIR → RESEARCH → REPLAN → COMPLETE → WAITING_HUMAN.

If none of these apply and the system is stuck, it must raise CONTROL_PLANE_DEFECT and write to CTO_EVENTS.jsonl.
It must never silently idle.

---

## Rule 5 — Evidence Hierarchy (autonomy decisions require evidence)

Autonomous decisions must be grounded in evidence:
- `PROVEN` — test or runtime evidence exists
- `OBSERVED` — seen in logs/output
- `INFERRED` — derived from evidence
- `PROPOSED` — hypothesis, not yet tested

**"706 tests passed" without a receipt = FORBIDDEN.**

Decisions based on PROPOSED or UNKNOWN state require human gate or explicit CTO acknowledgment.

---

## Current Human Gates (live)

| Gate ID | Description | Status | Blocks |
|---------|-------------|--------|--------|
| M008_VALIDATION | Human verdict on HUMAN_REVIEW_PACKAGE_V7 (477 calls quality audit) | WAITING_HUMAN | X103 production use of M008H classifier |
| XR01_EVENTSTUDY_GPL | eventstudy GPL decision | RESOLVED_BY_POLICY (ADR007) | nothing |

---

## Autonomy Milestones

| Milestone | Criteria | Status |
|-----------|----------|--------|
| Level 1: Auto-dispatch | active=0 + ready=non-empty → auto-launch (no human) | ACHIEVED (CTO002) |
| Level 2: Kill-9 recovery | factoryd restart recovers active missions from disk | ACHIEVED (CTO002B) |
| Level 3: Stall detection | stalled worker → REQUEUE automatically | ACHIEVED (F005) |
| Level 4: 6h autonomous run | SOAK_6H: HUMAN_GO_COUNT=0, LOST=0, STALL_UNRECOVERED=0 | PENDING |
| Level 5: Context injection | agents receive ADRs+lessons per dispatch | PENDING (CTO004) |
| Level 6: Cross-model review | adversarial reviewer catches hallucinations | PENDING (CTO005) |
| Level 7: Failure-signature routing | same failure → different model → CTO review | PENDING (F010) |
| Level 8: 72h autonomous run | SOAK_72H all counters zero | PENDING |

---

*This contract is binding on all Factory agents. A fresh agent that has not received CTO_STATE
context must behave conservatively: if unclear whether something requires a human gate, record
AUTONOMY_DEBT and ask, rather than proceeding autonomously.*
