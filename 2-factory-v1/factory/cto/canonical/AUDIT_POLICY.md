---
schema: audit-policy/v1
produced_at: "2026-09-06T12:00:00Z"
authority: "Compiled from APP007_CAMPAIGN_STATE cto000b_note + CTO000_AUDIT_REPORT + CTO_STATE + CTO_STRATEGY"
version: "v1"
---

# AUDIT POLICY — v1

## Purpose

Audits are mandatory quality gates that run at specific milestones to independently verify that
Factory and Twitter Radar state matches documented state. An audit finding is not optional feedback —
it becomes a required gap entry that must be closed before the next milestone gate.

**Auditors must be different models/agents from the builders they are auditing.**

---

## Audit Checkpoint Schedule

### AUDIT-01 — FULL FACTORY COUNTER-AUDIT (CTO000B)
- **Trigger**: F005 ACCEPTED AND SOAK_6H PASS
- **Mission**: CTO000B_COUNTER_AUDIT
- **Auditors**: Z Code + Codex (independent — not Claude CTO)
- **Scope**:
  - Entire Factory repo (factory/bin/, factory/lib/, factory/hooks/, factory/cto/)
  - All CTO000 findings — verify each one was fixed or explicitly accepted
  - All new code since CTO000 (F002, F003, F004, F005, CTO002, CTO002B, CTO003)
  - Competitor research registry coverage check
  - Produce: RESEARCH_TO_CODE_COVERAGE matrix (formally close open_gaps)
- **Output**: `factory/cto/reports/CTO000B_COUNTER_AUDIT_REPORT.md`
- **Gate**: CTO000B ACCEPTED → unblocks CTO005 (cross-model review)

### AUDIT-02 — CONCURRENCY COUNTER-AUDIT
- **Trigger**: F007 (2-worker parallel) ACCEPTED qualification run complete
- **Mission**: CONCURRENCY_COUNTER_AUDIT
- **Auditors**: Codex (race condition focus)
- **Scope**:
  - All RMW paths in factoryd + candidate_registry under concurrent access
  - _mark_dispatched/_mark_settled inter-process lock validation
  - CandidateRegistry fcntl contention under 2 workers
  - No duplicate dispatch, no state corruption
- **Output**: `factory/cto/reports/CONCURRENCY_AUDIT_REPORT.md`
- **Gate**: CONCURRENCY AUDIT ACCEPTED → unblocks MAX_WORKERS=4 consideration

### AUDIT-03 — SCALE COUNTER-AUDIT
- **Trigger**: 4-worker (MAX_WORKERS=4) qualification complete
- **Mission**: SCALE_COUNTER_AUDIT
- **Auditors**: Z Code + Codex
- **Scope**:
  - All state files under 4-writer concurrency
  - Heartbeat stagger analysis
  - DAG scheduler correctness under parallel dispatch
  - Long-run memory / PID leak checks
- **Output**: `factory/cto/reports/SCALE_AUDIT_REPORT.md`

### AUDIT-04 — LONG-RUN AUDIT (pre-SOAK_24H)
- **Trigger**: Before SOAK_24H qualification
- **Mission**: LONG_RUN_AUDIT
- **Auditors**: Claude CTO review
- **Scope**:
  - SupervisorState counter analysis (stall_count, lost_mission_count trends)
  - CTO_EVENTS.jsonl anomaly pattern scan
  - Anti-stall counter thresholds appropriate for 24h
  - Budget ledger drift check
- **Output**: `factory/cto/reports/LONG_RUN_AUDIT_REPORT.md`
- **Gate**: LONG_RUN AUDIT ACCEPTED → SOAK_24H may proceed

### AUDIT-05 — QUANT COUNTER-AUDIT
- **Trigger**: XR06 ACCEPTED (PSR/DSR/PBO statistical confidence complete)
- **Mission**: QUANT_COUNTER_AUDIT
- **Auditors**: Codex (mandatory for statistical formula review)
- **Scope**:
  - XR03 AbnormalReturnsEngine CAR formula correctness vs paper
  - XR04 momentum controls methodology
  - XR05 purgedcv OOS implementation (L015/L016 pitfalls verified not present)
  - XR06 PSR/DSR/PBO API usage verified against XR_EDGE_TEST_VECTORS.json
  - No lookahead bias in any formula
- **Output**: `factory/cto/reports/QUANT_COUNTER_AUDIT_REPORT.md`
- **Gate**: QUANT AUDIT ACCEPTED → account ranking scores credible for human consumption

### AUDIT-06 — SEMANTIC/THESIS COUNTER-AUDIT
- **Trigger**: X103 ACCEPTED (thesis reconstruction complete)
- **Mission**: SEMANTIC_COUNTER_AUDIT
- **Auditors**: Claude + Codex
- **Scope**:
  - CALL invariant unchanged vs ADR001 (no silent drift)
  - Thesis state machine (OPEN→ADD→REDUCE→CLOSE) correctness
  - No thesis leaks across accounts
  - M008H semantic gate applied consistently across full corpus
  - HUMAN_REVIEW_PACKAGE compliance check
- **Output**: `factory/cto/reports/SEMANTIC_COUNTER_AUDIT_REPORT.md`
- **Gate**: SEMANTIC AUDIT ACCEPTED → X104+ (outcome ledger + scoring) unblocked

---

## Audit Finding → Gap → Mission Pipeline

Every audit finding becomes a required work item via this pipeline:

```
AUDIT FINDING
  → classify severity (CRITICAL / HIGH / MEDIUM / LOW)
  → assign req_id from REQUIREMENTS_REGISTRY (or create new FREQ-/XREQ-)
  → create GAP-NNN entry in GAP_REGISTRY.json
  → create mission task file in factory/campaigns/app007/tasks/
  → create plan file in factory/campaigns/app007/plans/
  → insert into campaign_state.queued[]
  → factoryd auto-dispatches when ready
  → implementation + tests
  → verdict ACCEPTED
  → close GAP-NNN (open_gaps: [])
  → update REQUIREMENTS_REGISTRY status to IMPLEMENTED
```

**No audit finding may be silently ignored. CRITICAL/HIGH findings block the next milestone gate.**

### Severity → Action

| Severity | Action | Timeline |
|----------|--------|----------|
| CRITICAL | Immediate — block current milestone, fix before any new dispatch | Same day |
| HIGH | Block next milestone gate | Before gate |
| MEDIUM | Queue as standard mission | Next wave |
| LOW | Queue as tech debt | Opportunistic |

---

## Audit Report Format

Each audit report must:
1. State scope and auditor(s)
2. Tag every claim: PROVEN / OBSERVED / INFERRED / PROPOSED / UNKNOWN
3. List findings table (severity, component, description, fix)
4. List "verified clean" items (what was checked and passed)
5. Produce required gap IDs for every HIGH+ finding
6. State audit verdict: CLEAN / FINDINGS_MINOR / FINDINGS_BLOCKING

---

## Anti-Patterns (audit failures to prevent repeating)

| Pattern | Lesson | Guard |
|---------|--------|-------|
| Audit ran outside Factory supervision (no PID, no heartbeat, no receipt) | L009 | Every audit = task file + plan + evidence_dir + campaign.active entry |
| Auditor is same model as builder | — | Mandatory different model for adversarial audits |
| Audit finding ignored | — | CRITICAL/HIGH block milestone gate |
| Path hallucination in audit report | CTO000 correction of src/ paths | Forensic audit reads actual paths, not spec paths |
| Spec claims not verified against runtime | L011 (eventstudy MIT claim) | Auditor must verify from primary source (PyPI, GitHub LICENSE file) |

---

## Current Audit State (2026-09-06)

| Audit | Status | Notes |
|-------|--------|-------|
| CTO000 (initial forensic) | COMPLETE | 2026-09-06 — 10 findings; all HIGH findings tracked as gaps |
| CTO000B (counter-audit) | QUEUED | Blocked on SOAK_6H |
| CONCURRENCY | NOT_STARTED | Blocked on F007 |
| SCALE | NOT_STARTED | Blocked on 4-worker qualification |
| LONG_RUN | NOT_STARTED | Blocked on SOAK_24H |
| QUANT | NOT_STARTED | Blocked on XR06 |
| SEMANTIC | NOT_STARTED | Blocked on X103 |

---

*Audits are not optional reviews. They are milestone gates. A CLEAN audit verdict is required
before the next phase of the program proceeds. The conversation is never the authority on audit
findings — the written report is.*
