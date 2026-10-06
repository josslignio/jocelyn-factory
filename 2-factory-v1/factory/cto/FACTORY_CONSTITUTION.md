# FACTORY CONSTITUTION
## Durable CTO Reference — Conductor + Factory

**Version:** V1-SEALED  
**Canonical SHA:** e2cc21db (factory/e1-probe)  
**Sealed:** 2026-09-07  
**Status:** FACTORY_CONDUCTOR_V1_READY

---

## 1. NORTH STAR

Factory exists to solve the long-horizon software delivery problem that solo agents cannot solve reliably.

**The core problem:** A solo AI agent (Claude, GLM, Codex) running for 6 minutes on a well-scoped task performs well. The same agent on a 4-milestone, 3-day project with failures, restarts, model switches, and requirement changes loses context, repeats failed strategies, hallucinates completion, and drifts from the original spec without anyone noticing.

**What Factory provides:**
- Persistent CTO-level state that survives agent exits, daemon restarts, model switches, and multi-day gaps
- Anti-loop enforcement: FailureDirector, ResearchReuseGate, ConvergenceGate prevent repeated bad patterns
- Validated candidate lifecycle: code doesn't advance without evidence
- Autonomous continuation: queue empty ≠ done; LongRunSupervisor computes and enqueues next work
- Dual-lane operation: Factory self-improves while product (Twitter Radar) continues

**What Factory does NOT do:**
- Replace product judgment (subjective product decisions remain human)
- Provide payment/credentials (IRREVERSIBLE_EXTERNAL class)
- Continuously redesign itself (improvements only from measured weaknesses)

---

## 2. OPERATING MODEL

### Conductor (CTO Brain)
Persistent, stateful orchestrator. Never exits between missions. Holds canonical project state. Makes roadmap and promotion decisions from durable evidence. Implemented as `factoryd.py` + `ctok_kernel.py` + CTO_STATE files.

### Factory (Execution Layer)
Manages mission dispatch, agent lifecycle, worktree isolation, evidence collection, candidate registry, and result verification. Calls `dispatcher_v3.py` to route to engines.

### Agents (Workers)
Stateless subprocess instances (Claude, GLM, Codex). Receive a fully-compiled context from ContextCompiler. Write evidence to campaign directories. Never hold authoritative state.

### Project State
`APP007_CAMPAIGN_STATE.json` — campaign lifecycle (state, active, queued, blocked, completed).  
`factory/cto/CTOK_CHAIN_STATE.json` — Factory self-improvement chain.  
`factory/cto/CTO_EVENTS.jsonl` — immutable audit log of all decisions.  
`factory/run/supervisor_state.json` — live factoryd heartbeat state.

### Candidate Lifecycle
Attempt → tested → `CandidateRegistry` entry → qualification (`stop_gate.py`) → ACCEPTED/REJECTED → QRTS promotion via CAS → canonical_head advances.

CHILD_BASE_NOT_ACCEPTED = 0 invariant: no mission may dispatch on a parent that wasn't accepted.

### Validation
Each mission requires: task contract, plan, evidence artifacts, test pass, Product E2E (where applicable). `ConvergenceGate` checks that implemented code matches spec. `MilestoneController` blocks milestone completion without Product E2E.

### Promotion
QRTS (Qualified Runtime Test Subject): the frozen baseline SHA. CAS (Chain Authenticity Stamp): the promotion mechanism. A new immutable tag is created; old tags are NEVER force-moved.

### Learning (RunRecords — POST-V1)
Each validated run will emit a RunRecord: task_type, engine, strategy, result, wall_time, quota, retries, regressions, reuse_decision. Routing decisions update from validated trajectories only.

### Recovery
factoryd watchdog monitors PID/PGID. Stall detection: UNCERTAIN state → reclassify → requeue. LongRunSupervisor: queue empty + roadmap open → compute_next_required_actions → enqueue. PREMATURE_IDLE_WITH_OPEN_ROADMAP = 0.

### Scaling
Single-worker proven (V1). Multi-worker: candidate ancestry + DAG dependencies + quota arbitration must be safe before expanding. MAX_WORKERS 1 → 2 → N only with measured throughput improvement.

---

## 3. ARCHITECTURE — COMPONENT CLASSIFICATION

See `factory/cto/V1_CAPABILITY_MATRIX.md` for detailed per-component status.

Key runtime path (E2E_PROVEN):
- `factoryd.py` → main daemon, poll loop, zombie recovery, LongRunSupervisor
- `dispatcher_v3.py` → engine selection, worktree allocation, agent spawn
- `ctok_kernel.py` → CTO state management, context compilation
- `context_compiler.py` → assembles agent context from CTO_STATE + campaign + task
- `candidate_registry.py` → tracks attempts, candidates, promotion history
- `failure_director.py` → FailureDirector: strategy rotation, loop detection
- `research_reuse_gate.py` → REUSE/ADAPT/WRAP/BUILD/REJECT decisions
- `milestone_controller.py` → Product E2E enforcement
- `convergence_gate.py` → spec-code alignment checking
- `stop_gate.py` → QRTS qualification gate
- `long_run_supervisor.py` → PREMATURE_IDLE + AUTO_BLOCK prevention (CTOK008)

---

## 4. REAL RUNTIME FLOW

```
PROJECT INTENT (task file + spec)
    ↓
PLAN (plan.json — approved approach)
    ↓
CONTEXT COMPILATION (CTOKernel + ContextCompiler)
    - CTO_STATE loaded
    - Prior attempts, failures, accepted candidates
    - Project context, spec, acceptance criteria
    - ResearchReuseGate decision
    ↓
MISSION DISPATCH (dispatcher_v3)
    - Engine selected (Claude/GLM/Codex)
    - Worktree allocated
    - Task contract validated
    ↓
AGENT EXECUTION (stateless worker)
    - Implements deliverables
    - Runs acceptance tests
    - Writes evidence to campaigns/app007/evidence/
    ↓
VALIDATION (stop_gate + ConvergenceGate + MilestoneController)
    - Tests pass?
    - Product E2E pass?
    - Spec-code convergence?
    ↓
CANDIDATE SETTLEMENT (CandidateRegistry)
    - ACCEPTED: advance
    - REJECTED: FailureDirector, new strategy, requeue
    ↓
PRODUCT E2E (if milestone boundary)
    ↓
PROMOTION (CAS if QRTS boundary)
    ↓
NEXT MISSION (LongRunSupervisor auto-computes)
```

Authority at each stage:
- Roadmap: CTOKernel (CTO_STATE)
- Dispatch: dispatcher_v3 (engine routing)
- Candidate: CandidateRegistry + stop_gate
- Promotion: CAS (immutable, audited)
- Recovery: LongRunSupervisor + FailureDirector

---

## 5. CONTEXT RETENTION

Context survives via **durable file state only**. Chat history is NOT authoritative.

| Event | How Survived |
|---|---|
| Agent exits | Evidence written to campaign dir before exit |
| Mission settlement | CTO_EVENTS.jsonl append, campaign state updated |
| Daemon restart | supervisor_state.json + campaign state file |
| Process kill | Same — file state is the only truth |
| Next day | Same |
| Model switch | ContextCompiler reads from files, not agent memory |
| Milestone transition | MilestoneController updates campaign state |
| Factory ↔ Twitter Radar switch | Separate campaign states, separate scopes |

**Reconstructed on restart:** CTO context compiled from: CTOK_CHAIN_STATE.json + CTO_EVENTS.jsonl + APP007_CAMPAIGN_STATE.json + task files + evidence receipts  
**Persisted as-is:** candidate SHA, attempt history, failure patterns, accepted milestones

---

## 6. ANTI-LOOP SYSTEM

| Loop Type | Prevention Mechanism |
|---|---|
| Identical retries | FailureDirector: attempt history, strategy rotation |
| Repeated failed strategies | FailureDirector: strategy exhaustion → HUMAN_BLOCKED |
| Repeated diffs | CandidateRegistry: diff fingerprinting |
| No-progress agent loops | Watchdog: heartbeat + stall timeout → requeue |
| Architecture rewrites | ResearchReuseGate: REUSE/ADAPT before BUILD |
| Rebuilding existing code | ResearchReuseGate: REJECT if equivalent exists |
| Retrying unavailable engines | MOTOR_UNAVAILABLE → route to alternate |
| Silent idle with open roadmap | LongRunSupervisor: PREMATURE_IDLE=0 |

---

## 7. CRASH / STALL RECOVERY

- **Heartbeat:** factoryd writes `supervisor_state.json` every poll
- **Watchdog:** checks PID alive, last heartbeat age
- **PID/PGID:** tracked per mission in supervisor state
- **Lifecycle:** STARTING → RUNNING → UNCERTAIN (stall suspected) → reclassify
- **Stall classification:** time since last evidence write + heartbeat gap
- **Process termination:** PGID kill → requeue with stall note
- **Requeue:** FailureDirector consulted before re-dispatch (different strategy)
- **Candidate preservation:** last accepted candidate SHA saved in registry
- **Restart reconciliation:** factoryd reads campaign state on start → resumes from last known position

---

## 8. MULTI-AI STRATEGY

Current routing (runtime-exercised, not yet adaptive):
- **Claude Sonnet 4.6:** architecture, complex research, multi-file refactors, context-heavy tasks
- **GLM:** CREDENTIALS_UNAVAILABLE in current runtime
- **Codex:** CREDENTIALS_UNAVAILABLE in current runtime

Future adaptive routing (POST-V1):
- P(success | engine, task_type) learned from validated RunRecords
- Routing updates only from VALIDATED=true trajectories
- MOTOR_UNAVAILABLE → fallback to available engine with stall note

---

## 9. RESEARCH / REUSE POLICY

Before any major capability:
```
REUSE   → identical existing implementation → use it directly
ADAPT   → similar implementation → modify minimally
WRAP    → external library/solution → thin interface
BUILD   → no equivalent exists → implement from spec
REJECT  → existing sufficient, no new capability justified
```

Enforced by ResearchReuseGate at MISSION DISPATCH time. MAJOR_BUILD_WITHOUT_RESEARCH_REUSE_DECISION = 0 invariant.

---

## 10. CANDIDATE / PROMOTION MODEL

```
Agent implementation attempt
    ↓
Evidence written to campaigns/app007/evidence/MISSION_ID/
    ↓
stop_gate.py qualification
    ↓
CandidateRegistry: ACCEPTED or REJECTED
    - REJECTED: FailureDirector, new strategy
    - ACCEPTED: candidate SHA recorded
    ↓
If Factory self-improvement (CTOK):
    → QRTS: current frozen baseline
    → CAS: create new immutable tag, advance canonical_head
    → Old tags NEVER force-moved (git tag -f FORBIDDEN)
```

CHILD_BASE_NOT_ACCEPTED = 0: `dispatcher_v3` checks parent SHA is ACCEPTED before dispatch.

---

## 11. FACTORY ROADMAP

### DONE (V1-SEALED)
- CTOK001 Canonical CTO State (E2E_PROVEN)
- CTOK002/002B Context Compiler + Dispatcher Hard Gate (E2E_PROVEN)
- CTOK003 Research/Reuse Gate Qualified (E2E_PROVEN)
- CTOK004 Milestone Product E2E (E2E_PROVEN)
- CTOK005 Spec/Code Convergence (E2E_PROVEN)
- CTOK006/006B/006C Failure Director + Task Contract + Required State (E2E_PROVEN)
- CTOK007 Cold Restart Continuity (E2E_PROVEN)
- CTOK008 Long Run Supervisor (E2E_PROVEN)
- SOLO_BENCHMARK complete (Lane A 6/6, Lane D 12/12)
- FACTORY_CONDUCTOR_V1_READY sealed at 635b9f75

### CURRENT
- LONG_HORIZON_AUTONOMY_PROOF: dual-lane execution (Factory + Twitter Radar)

### NEXT (POST-V1 priorities)
1. RunRecord infrastructure — validated learning from real runs
2. Champion/challenger model — Factory N (champion) vs N+1 (challenger), same benchmark
3. Adaptive model routing — route from P(success | engine, task_type)
4. Multi-worker scaling — MAX_WORKERS 1→2→N with DAG dependencies

### BLOCKED
- Multi-worker: requires RunRecord validation first
- GLM/Codex routing: CREDENTIALS_UNAVAILABLE

---

## 12. FACTORY CURRENT STATE

```
canonical_sha:        e2cc21db (factory/e1-probe, 635b9f75 is the seal commit)
chain_status:         DONE
v1_seal_issued:       True (2026-09-07)
campaign_phase:       PRODUCT_DELIVERY (Twitter Radar)
active_missions:      [XR04_MOMENTUM_CONTROLS] ← RUNNING pid=63879
queued_missions:      []
blocked_missions:     []
open_invariants:      0
known_regressions:    0 (931/931 clean)
current_next_action:  XR04_MOMENTUM_CONTROLS delivery → XR05 → XR06 → product E2E
```

---

## 13. HARD INVARIANTS (ALL MUST REMAIN 0)

```
FALSE_SATISFIED=0, GOLDEN_CORRUPTION=0, LOST_VALIDATED_WORK=0
UNRECOVERED_STALL=0, UNRECOVERED_ZOMBIE=0
CONTEXTLESS_AGENT_DISPATCH=0, STALE_CTO_CONTEXT_DISPATCH=0
CROSS_PROJECT_CONTEXT_LEAK=0
UNBOUNDED_EQUIVALENT_RETRY=0
MAJOR_BUILD_WITHOUT_RESEARCH_REUSE_DECISION=0
MILESTONE_COMPLETE_WITHOUT_PRODUCT_E2E=0
MILESTONE_COMPLETE_WITHOUT_REQUIRED_ARTIFACT=0
QUEUED_INVALID_MISSION_CONTRACT=0
EPHEMERAL_CONTROL_PLANE_DEPENDENCY=0
INVALID_UPSTREAM_CONTEXT_PROPAGATION=0
CRITICAL_CAPABILITY_NOT_ON_RUNTIME_PATH=0
CHILD_BASE_NOT_ACCEPTED=0
PREMATURE_IDLE_WITH_OPEN_ROADMAP=0
AUTO_RESOLVABLE_HUMAN_BLOCK=0
```
