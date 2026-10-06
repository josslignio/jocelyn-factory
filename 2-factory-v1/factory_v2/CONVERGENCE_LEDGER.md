# Factory Architecture Convergence Ledger

**Produced:** 2026-09-07  
**Method:** Factory subsystem-by-subsystem comparison against public implementations  
**References:** Temporal, LangGraph, OpenHands (CodeAct), SWE-agent, BMAD Method, Prefect, LiteLLM, AgentOps, MetaGPT, AutoGen  

**Rule:** Factory's real differentiation = CONDUCTOR CTO (canonical brain + authority hierarchy + independent validation + champion/challenger discipline). Commodity plumbing is replaceable.

---

## Subsystem Map

| # | Factory Subsystem | File/Component | Public Best | Decision | Action | Effort |
|---|---|---|---|---|---|---|
| 1 | Canonical Brain | Strategy/Spec/Roadmap | BMAD (partial) | **KEEP** | — | 0 |
| 2 | Durable Supervisor | factoryd | Temporal | **ADAPT** | Add crash recovery; replace with Temporal in V2+ | S |
| 3 | AI Execution | dispatcher_v3.py | OpenHands | **KEEP** | — | 0 |
| 4 | Engine Router | model_router.py | LiteLLM | **REPLACE** | Wrap LiteLLM; delete model_router.py | XS |
| 5 | Multi-mission Orch | autopilot.py | — | **DELETE** | Already marked legacy. Remove. | XS |
| 6 | Roadmap Resolver | roadmap_resolver.py | — | **KEEP** | Factory-specific dependency DAG. No OSS equivalent. | 0 |
| 7 | Mission Compiler | mission_compiler.py | — | **KEEP** | Brain→bounded task. Factory-specific. | 0 |
| 8 | Context Compiler | context_compiler.py | — | **KEEP** | Token budget + context assembly. Factory-specific. | 0 |
| 9 | Independent Validator | independent_validator.py | — | **KEEP** | Core differentiator: executor ≠ validator. No OSS equivalent. | 0 |
| 10 | Convergence Gate | convergence_gate.py | — | **KEEP** | Settlement logic. Factory-specific. | 0 |
| 11 | Failure Director | failure_director.py | LangGraph retry | **KEEP** | Simple enough. Replace if loop complexity grows. | 0 |
| 12 | Security Gate | stop_gate.py | — | **KEEP** | Security invariant. Factory-specific. | 0 |
| 13 | Run Records | (NOT BUILT) | AgentOps | **BUILD MINIMAL** | JSONL, 30 LOC. Don't use AgentOps (external). | XS |
| 14 | Adaptive Routing | (NOT BUILT) | LiteLLM | **WRAP** | LiteLLM interface + RunRecord evidence layer | S |
| 15 | Champion/Challenger | (NOT BUILT) | — | **BUILD MINIMAL** | Benchmark script + promote/reject flow | S |
| 16 | Workspace Isolation | worktrees (in dispatcher) | devcontainers | **KEEP** | git worktrees are lighter and sufficient. | 0 |
| 17 | Event Storage | JSONL receipts | Temporal Events | **KEEP** | JSONL is readable, grep-able, zero-dependency. | 0 |
| 18 | Scheduling | — | Temporal Schedules | **WRAP** | Use cron/at for now; Temporal for V2 long runs | XS |

---

## Decision Rationale

### KEEP — Canonical Brain (Strategy/Spec/Roadmap)
BMAD Method uses a similar document hierarchy (PRD + Architecture + Stories). But BMAD has no authority enforcement — documents are advisory. Factory's canonical brain is enforced: authority_gate.py rejects execution if hashes diverge from last-committed state. **This enforcement is the differentiator.** BMAD = inspiration; Factory = stricter implementation. KEEP.

### ADAPT — factoryd (supervisor daemon)
**Temporal** provides exactly what factoryd tries to do: durable workflows that survive process death, automatic retries, history replay, worker pools, schedules. Temporal is proven at scale (Uber, Stripe, Coinbase). factoryd is a ~300 LOC Python daemon with manual PID tracking and JSON state files. **The gap is large.** However, Temporal requires a running cluster (Docker/K8s). For V1 local execution, keep factoryd + add crash recovery (write PID + state atomically, restore on restart). Plan Temporal migration for V2 when we need distributed workers. ADAPT now, REPLACE later.

### REPLACE — model_router.py → LiteLLM  
**LiteLLM** is the industry standard for unified multi-LLM routing. It handles: Claude/OpenAI/GLM/Codex/Ollama through one interface, retry logic, fallback chains, cost tracking, streaming, token counting. Factory's model_router.py reimplements a subset (~200 LOC). LiteLLM has 15k+ stars, active maintenance, and handles all edge cases we'd eventually hit (rate limits, model availability, context window differences). **This is textbook commodity plumbing.** REPLACE with `litellm.completion(model=..., messages=...)`. Delta: delete ~200 LOC, add `pip install litellm`, write thin adapter in executor.

### DELETE — autopilot.py
Already marked legacy in SPEC. dispatcher_v3 does the bounded execution. autopilot.py is a multi-mission orchestrator that predates the current architecture. DELETE. Removes dead code, reduces confusion.

### KEEP — dispatcher_v3.py (AI execution)
**OpenHands** (formerly OpenDevin) is the closest public equivalent: sandboxed code execution, tool interface, event stream. BUT OpenHands is designed for interactive sessions (web UI, user feedback loop). dispatcher_v3 is designed for batch mission execution: task file → worktree → AI execution → tests → commit → receipt. The bounded-mission-with-receipt model is unique to Factory and is the execution primitive that makes everything else work. KEEP.

### BUILD MINIMAL — RunRecords
**AgentOps** does agent telemetry but requires external service + API key. **MLflow** does experiment tracking but is ML-specific. For Factory's needs (per-run schema with engine/candidate_sha/tests/wall_time), a simple append-to-JSONL approach in ~30 LOC is sufficient and zero-dependency. Build it. Don't overcomplicate.

### WRAP — Adaptive Routing → LiteLLM  
Once RunRecords exist, adaptive routing = read RunRecords → compute engine stats → pass model hint to LiteLLM. LiteLLM provides the execution interface; Factory provides the evidence-based selection logic on top. WRAP means: LiteLLM handles the low-level routing; Factory's adaptive_router.py (~50 LOC) reads RunRecord evidence and passes `preferred_model` to LiteLLM. Total: ~80 LOC instead of rebuilding a router from scratch.

---

## What Factory Is NOT (compared to public systems)

| Public System | What it does | Why Factory ≠ it |
|---|---|---|
| **LangGraph** | Graph-based agent state machine with checkpointing | LangGraph loops are *generic* agent graphs. Factory's loop is *authority-constrained*: roadmap drives node transitions, not arbitrary edges. The authority invariant is not expressible in LangGraph without Factory's validation layer on top. |
| **OpenHands** | Interactive agent with browser/terminal/code sandbox | Session-first (human in loop). Factory is batch-first (HUMAN_GO=0). OpenHands has no canonical brain concept. |
| **SWE-agent** | Trajectory-based code editing with ACI | No project authority hierarchy. No independent validator. No champion/challenger. SWE-agent is one-shot; Factory is multi-milestone. |
| **MetaGPT** | Multi-agent with role specialization | Role specialization but no authority enforcement. Documents are generated, not frozen. No champion/challenger. |
| **BMAD Method** | Document-driven multi-agent (PRD/Architecture/Stories) | Closest to Factory's canonical brain. BUT: no enforcement. Documents advisory only. Factory's authority_gate.py = what BMAD lacks. |
| **AutoGen** | Multi-agent conversation graph | Conversation-first, not mission-first. No canonical brain. No worktree isolation. No independent validation. |

---

## Immediate Actions (ordered by ROI)

### Action 1: DELETE autopilot.py (XS — 30 min)
```bash
git rm factory/bin/autopilot.py  # or move to archive/
# Update any import references
```
Removes 400+ LOC of dead code. Zero risk (already unused).

### Action 2: REPLACE model_router.py with LiteLLM (XS — 2h)
```python
# Before (model_router.py ~200 LOC):
from factory.bin.model_router import route_model
engine = route_model(spec_level=spec_level)

# After (litellm adapter ~20 LOC):
import litellm
def route_to_model(spec_level: str) -> str:
    return {"S": "claude-opus-5", "A": "claude-sonnet-5", "B": "openai/glm-4-9b"}[spec_level]
response = litellm.completion(model=route_to_model(spec_level), messages=messages)
```
Deletes 200 LOC custom router, gains: retry logic, fallback chains, cost tracking, streaming, all future models for free.

### Action 3: BUILD RunRecords minimal (XS — 1h)
```python
# factory_v2/runrecords.py — ~30 LOC
import json, pathlib, time
RUNRECORDS_PATH = pathlib.Path("factory_v2/receipts/runrecords.jsonl")

def append_run(record: dict) -> None:
    record["recorded_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(RUNRECORDS_PATH, "a") as f:
        f.write(json.dumps(record) + "\n")
```
Unblocks: REAL_MULTI_AI_EXECUTION_V2 milestone (requires RunRecords), AdaptiveRouting milestone.

### Action 4: WRAP LiteLLM for Adaptive Routing (S — 4h)
After RunRecords have ≥5 runs per engine: read stats, select preferred engine, pass to LiteLLM. ~80 LOC total.

### Action 5: ADAPT factoryd crash recovery (S — 4h)
Add atomic state write + PID lockfile + recovery on restart. Unblocks LONG_RUN_PROOF_2.

---

## What We Do NOT Build (ever)

- Custom durable workflow engine → Temporal exists
- Custom multi-LLM router → LiteLLM exists  
- Custom agent telemetry → simple JSONL (AgentOps is external coupling)
- Custom sandbox → git worktrees are sufficient for code tasks
- Custom retry framework → Python `tenacity` or LiteLLM retries
- A second canonical brain → one per project, never two

---

## Verdict

Factory is **80% converged** already. The core architecture (canonical brain → roadmap resolver → mission compiler → dispatcher → independent validator → convergence gate) is correct and has no mature OSS equivalent for authority-constrained batch AI execution.

The remaining 20% delta:
1. model_router.py → LiteLLM (saves 200 LOC, gains full multi-LLM ecosystem) ← **do this now**
2. RunRecords (30 LOC, unblocks two milestones) ← **do this now**
3. autopilot.py → DELETE ← **do this now**
4. factoryd → Temporal (when we need distributed workers, not now)

**Total immediate code reduction:** ~650 LOC deleted or replaced by LiteLLM. Zero architectural risk.

After these 3 actions: FREEZE → run Twitter Radar → observe → improve.
