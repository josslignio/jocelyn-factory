> SUPERSEDED — voir projects/factory/SPEC.md
<!-- AUTORITÉ DÉPLACÉE 2026-09-07T15:21:00Z — l'autorité canonique est désormais projects/factory/SPEC.md. Ce fichier est GELÉ (contenu inchangé pour compatibilité champion). Violation d'intégrité = check C9 de authority_audit.py. -->

---
schema: factory-master-spec/v1
produced_at: "2026-09-06T12:00:00Z"
authority: "Compiled from CTO_STATE.json + CTO_ARCHITECTURE.json + CTO_DECISIONS.jsonl + CTO_LESSONS.jsonl + APP007_CAMPAIGN_STATE.json + canonical_roadmap_v2.json + CTO000_AUDIT_REPORT.md"
version: "v1"
---

# FACTORY MASTER SPEC — v1

## North Star

> Autonomously execute, verify, recover and improve multi-mission software campaigns for days
> without human babysitting.

**Done when**: Jocelyn disappears for 72 hours and returns to find missions progressed, failures
detected and repaired without human input, cross-review performed, roadmap updated based on
outcomes, and `CTO_STATUS_LATEST.md` current — with HUMAN_GO_COUNT=0 for the entire run.

---

## Evidence Hierarchy (never invert)

1. Executable test results
2. Runtime evidence (receipts, logs)
3. Repository / data state
4. Canonical specifications (this file)
5. External authoritative source
6. Model reasoning

A model never overrides runtime evidence.

---

## Capability Registry

| ID | Capability | Status | Component | Mission |
|----|-----------|--------|-----------|---------|
| FREQ-001 | Persistent runtime supervisor | IMPLEMENTED | factoryd | F001 |
| FREQ-002 | Event-driven CTO scheduler | IMPLEMENTED | CTO002 event loop | CTO002 |
| FREQ-003 | Kill-9 restart recovery | IMPLEMENTED | supervisor_state.json reload | CTO002B |
| FREQ-004 | Per-mission worktree isolation | IMPLEMENTED | WorktreeManager | F002+F003 |
| FREQ-005 | Candidate registry per mission | IMPLEMENTED | CandidateRegistry + fcntl | F004 |
| FREQ-006 | Zombie PID detection | IMPLEMENTED | completion_detector.is_worker_dead | F005 |
| FREQ-007 | Stale-active reconciliation at startup | IMPLEMENTED | factoryd._reconcile_stale_active | F005 |
| FREQ-008 | Progress watchdog | IMPLEMENTED | progress_watchdog.py snapshot/is_stalled | F005 |
| FREQ-009 | Anti-stall counters in supervisor state | IMPLEMENTED | stall_count/stale_active_count/lost_mission_count/duplicate_dispatch_count/human_go_count | F005 |
| FREQ-010 | Automated status reporting | IMPLEMENTED | cto_reporter.py + factoryd post-settlement hook | CTO003 |
| FREQ-011 | Context injection / ContextCompiler | PLANNED_FROZEN | factory/lib/context_compiler.py (not built) | CTO004 |
| FREQ-012 | Cross-model adversarial review | PLANNED | review_matrix (not built) | CTO005 |
| FREQ-013 | QRTS/candidate separation — promote_qrts requires evidence | IMPLEMENTED | CandidateRegistry.promote_qrts(QualificationResult(passed=True)) | F004 |
| FREQ-014 | Transactional promotion — base_sha check before merge | PARTIAL | base check present but not enforced on every write path | — |
| FREQ-015 | Parallel workers (2-worker qualified) | PARTIAL | worktrees ready; 2-worker concurrency not yet qualified | F002 |
| FREQ-016 | E2E torture test (kill-9, auth fail, network cut, bad commit) | MISSING | — | F012 |
| FREQ-017 | SOAK_6H / SOAK_24H / SOAK_72H qualification | MISSING | SOAK_6H_QUALIFICATION queued | — |
| FREQ-018 | Failure signature memory / anti-loop escalation | MISSING | CTO_LESSONS.jsonl exists but NOT wired to retry logic | F010 |
| FREQ-019 | Adaptive model routing with performance feedback | PARTIAL | static router exists; no feedback loop; CTO_MODEL_PERFORMANCE.json populated | CTO006 |
| FREQ-020 | Auto backlog refill | IMPLEMENTED | backlog_refiller.py + factoryd H2→H1 promotion | F001+CTO002 |
| FREQ-021 | Context receipt per dispatch (ADRs + lessons injected) | MISSING | agents receive task file only; CTO_STATE.json / ADRs never injected | CTO004 |
| FREQ-022 | CTO000B full independent counter-audit (Z Code + Codex) | MISSING | queued after SOAK_6H | CTO000B |
| FREQ-023 | Heartbeat written during builds | MISSING | heartbeat.py exists but never called in autopilot/dispatcher | F005 patch |
| FREQ-024 | DAG dependency scheduler (depends_on enforcement) | MISSING | missions declare depends_on; no enforcement in factoryd | F008 |
| FREQ-025 | Autonomous failure classification + bounded repair mission | MISSING | blocked missions stay blocked; no auto-repair creation | F010 |
| FREQ-026 | FACTORY_STABLE / FACTORY_CANDIDATE safe self-upgrade | MISSING | Factory can overwrite itself while running | F011 |
| FREQ-027 | Quota-aware model router (cost/latency/capacity) | MISSING | static spec_level=A→Claude, B→GLM only | F009 |
| FREQ-028 | Two parallel backlog systems unified | PARTIAL | rolling_backlog.py (autopilot path) vs backlog_refiller.py (factoryd path) — risk of divergence | — |
| FREQ-029 | cto_planner integration wired into factoryd | IMPLEMENTED | cto_planner called from factoryd._handle_mission_settled (CTO002B) | CTO002B |
| FREQ-030 | Plan files created before campaign queuing | IMPLEMENTED | all queued missions have plan files (F.1 resolved) | CTO002 |

---

## Qualified Components (PROVEN at sha d6398b21)

| Component | Path | Tests | Qualified SHA |
|-----------|------|-------|--------------|
| factory.supervisor | factory/bin/factoryd.py | tests/test_supervisor.py (27+) | d6398b21 |
| factory.dispatcher_v3 | factory/bin/dispatcher_v3.py | test_tb1, test_p0_routing_budget_integration | QUALIFIED |
| factory.model_router | factory/lib/model_router.py | tests/test_model_router.py (25) | QUALIFIED |
| factory.stop_gate | factory/hooks/stop_gate.py | tests/test_stop_gate.py (9) | QUALIFIED |
| factory.backlog_refiller | factory/lib/backlog_refiller.py | tests/test_supervisor.py T07-T10 | d6398b21 |
| factory.rolling_backlog | factory/lib/rolling_backlog.py | — | ACCEPTED_FD5_FD7 |
| factory.cto_planner | factory/bin/cto_planner.py | — | PARTIAL (no test_cto_planner.py) |
| factory.verdict_taxonomy | factory/lib/verdict_taxonomy.py | — | ACCEPTED_FD3 |
| factory.completion_detector | factory/lib/completion_detector.py | tests/test_supervisor.py T04-T06 | d6398b21 |
| factory.supervisor_state | factory/lib/supervisor_state.py | tests/test_supervisor.py | d6398b21 |
| factory.worktree_manager | factory/lib/worktree_manager.py | tests/test_worktree_manager.py (8) | d6398b21 |
| factory.candidate_registry | factory/lib/candidate_registry.py | tests/test_candidate_lifecycle.py (25) | dcee0f22 |
| factory.progress_watchdog | factory/lib/progress_watchdog.py | tests/test_stall_recovery.py (7+) | 347f150f |
| factory.cto_reporter | factory/cto/cto_reporter.py | 5 tests | CTO003 |

---

## Active Defects / Known Issues

| ID | Severity | Description | Fix Mission |
|----|----------|-------------|-------------|
| D001 | HIGH | Context injection absent — agents build without ADRs/strategy (CTO000 F.3) | CTO004 |
| D002 | MEDIUM | Heartbeat never written in autopilot/dispatcher path → hung workers undetectable (CTO000 F.5) | patch |
| D003 | MEDIUM | Two parallel backlog systems (rolling_backlog + backlog_refiller) may diverge | unify |
| D004 | LOW→HIGH | _mark_dispatched/_mark_settled RMW without inter-process lock — safe single-threaded, HIGH with F007 | F007 |
| D005 | HIGH | test_full_corpus_extractor.py: 29 pre-existing failures block X-track auto-verdict — ADR005 workaround | dedicated repair |

---

## Active Architecture Decisions (ADRs)

| ADR | Decision | Status |
|-----|----------|--------|
| ADR001 | POSITION_EVENT model frozen — CALL invariant immutable without explicit superseding ADR | ACTIVE |
| ADR002 | factory_capture BLOCKED acceptable for spec_level=A; tests pass → ACCEPTED | ACTIVE |
| ADR003 | FinGPT/FinBERT are challengers only — do NOT replace M008H | ACTIVE |
| ADR004 | No parallel writers on same repo before F002 (worktrees) ACCEPTED | ACTIVE |
| ADR005 | test_full_corpus_extractor.py excluded from X-track test_cmd | ACTIVE |
| ADR006 | CTO control plane before more product features | ACTIVE |
| ADR007 | eventstudy GPL-3.0 BLOCKED — implement CAR on statsmodels/scipy | ACTIVE |

---

## Strategic Risks

| ID | Severity | Risk | Mitigation |
|----|----------|------|-----------|
| R001 | HIGH | Parallel workers on same repo before worktrees cause git conflicts | F002 complete; ADR004 active |
| R002 | HIGH | GLM may hallucinate statistical APIs (PSR/DSR, purgedcv interface) | Codex adversarial review mandatory for XR03-XR06 |
| R003 | MEDIUM | Edge validation may show most accounts have no real edge | Accept evidence; filter to confirmed-edge accounts |
| R004 | HIGH | CTO loop not yet event-driven (resolved by CTO002) | RESOLVED |

---

## Factory-Track Roadmap

| Mission | Title | Status | Blocks |
|---------|-------|--------|--------|
| F001 | Persistent Supervisor | ACCEPTED (d6398b21) | F002 |
| F002 | Worktree Isolation | ACCEPTED (99169f28) | F003, parallel |
| F003 | Dispatcher Worktree Integration | ACCEPTED | F004 |
| F004 | Candidate Lifecycle | ACCEPTED (dcee0f22) | FREQ-005/013 |
| F005 | Stall Recovery | ACCEPTED (347f150f) | SOAK_6H |
| F006 | — (renumbered — see FREQ-015) | — | — |
| SOAK_6H | 6h overnight qualification | QUEUED | CTO000B |
| CTO000B | Full counter-audit Z Code+Codex | QUEUED | CTO005 |
| CTO005 | Cross-model review | QUEUED | — |
| F007 | Parallel Workers (MAX_WORKERS=2) | STUB | F008 |
| F008 | DAG Dependency Scheduler | STUB | F009/F010 |
| F009 | Quota-Aware Router | STUB | — |
| F010 | Autonomous Failure Classification + Repair | STUB | — |
| F011 | FACTORY_STABLE/CANDIDATE Safe Self-Upgrade | STUB | — |
| F012 | 72h Torture Test | STUB | — |

---

*This document is compiled from repository state and is updated only on explicit CTO decision. The conversation is never the authority — this file is.*
