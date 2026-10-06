---
schema: model-role-policy/v1
produced_at: "2026-09-06T12:00:00Z"
authority: "Compiled from CTO_MODEL_PERFORMANCE.json + CTO_STATE model_assignment + CTO_STRATEGY Principle 3"
version: "v1"
---

# MODEL ROLE POLICY — v1

## Principle: Planner ≠ Builder ≠ Reviewer

For P0/P1 missions, a single model cannot be author + reviewer + judge on the same mission.

```
Claude  →  PLAN / CONTRACT / SPEC
GLM     →  IMPLEMENTATION
Codex   →  ADVERSARIAL REVIEW
Z Code  →  CHALLENGE / ALTERNATIVE ARCHITECTURE
```

---

## Role Definitions

### CLAUDE (claude-sonnet-5) — CTO / Principal Architect
- **spec_level**: A
- **Missions**: Architecture, roadmap, complex semantic models, integration review, contract writing
- **Proven missions**: M008D, M008E, M008F, M008H, M007B, F001 (30 total)
- **Typical duration**: 2000–2700s for complex semantic; architecture missions vary
- **Known issues**:
  - `factory_capture` FAILS for spec_level=A — commits directly before capture runs (ADR002: acceptable, tracked as BLOCKED not FAIL)
  - Sometimes commits multiple times across one session
- **When to use**:
  - spec_level=A missions
  - Blocked GLM (timeout >1800s) → escalate to Claude
  - CTO architectural review (attempt 3 same failure)
  - Any mission that defines a new invariant, ADR, or canonical model

### GLM (glm-5.2) — Primary Builder
- **spec_level**: B
- **Missions**: Bounded implementation, tests, data pipelines, refactors, CLI/scripts
- **Proven missions**: FD1–FD9, M008G, M009, M010, CTO002, CTO002B, CTO003, F002–F005, XR02, XR03 (15+)
- **Typical duration**: 300–800s first attempt; data pipelines 400–1000s
- **Success rate**: High for well-specified tasks (FD1 FD3 FD5 FD9: first attempt clean)
- **Known issues**:
  - May hallucinate APIs for complex/unfamiliar libraries (L011, L015, L016)
  - Timeout risk on complex semantic tasks (M008C timed out at 1800s → correctly escalated)
- **When to use**:
  - spec_level=B missions
  - Bounded implementation with explicit contract
  - Data pipelines with clear input/output spec
  - Test generation for known components
- **When NOT to use**:
  - Statistical formula implementation without Codex adversarial review (XR03-XR06: mandatory Codex review)
  - Architecture design or new invariant definition

### CODEX — Adversarial Reviewer
- **Status**: PENDING_INTEGRATION (CTO005)
- **Role**: Adversarial correctness, API hallucination detection, edge case enumeration, statistical formula review
- **Mandatory for**: XR03–XR06 statistical implementations (PSR/DSR/PBO formulas, purgedcv API)
- **Integration target**: CTO003 (reporter), CTO005 (review matrix)
- **Why**: GLM hallucinated PSR API signature (L015) and DSR bars_per_year (L016) — Codex review would have caught both

### Z CODE — Challenger Engineer
- **Status**: PENDING_INTEGRATION (CTO005)
- **Role**: Alternative architecture proposals, code archaeology, legacy code mining, difficult debugging, second opinion
- **Proven value**: CTO002 Z Code challenge identified 3 blockers → triggered CTO002B (MT001/MT002/MT003 all fixed)
- **Integration target**: CTO005 (review matrix)
- **When to use**: Adversarial architecture review, forensic audits, "is there a better approach?"

---

## Current Routing Rules (STATIC — will become dynamic after CTO006)

```python
ROUTING = {
    "spec_level_A": "claude-sonnet-5",
    "spec_level_B": "glm-5.2",
    "escalation_timeout_s": 1800,
    "escalation_target": "claude-sonnet-5",
}
```

### Escalation Protocol
1. GLM started → timeout >1800s → escalate to Claude
2. Mission BLOCKED (same failure signature) → attempt 2: different model
3. Mission BLOCKED (same failure signature, attempt 2) → attempt 3: Claude architectural review
4. Claude architectural review produces fix → new mission with GLM build

---

## Target Routing Formula (after N=50 missions)

```python
route = argmax(pass1_rate / (median_duration * token_cost))
    per task_type in [
        "bounded_python",
        "architecture",
        "semantic_model",
        "data_pipeline",
        "test_generation",
        "adversarial_review"
    ]
```

Performance data tracked in: `factory/cto/CTO_MODEL_PERFORMANCE.json`

---

## Risk-Based Reviewer Count

| Risk Level | Example | Reviewers Required |
|-----------|---------|-------------------|
| P0 — invariant change | Modify CALL invariant (ADR001) | Claude PLAN + GLM BUILD + Codex REVIEW + Z Code CHALLENGE |
| P0 — architecture | New factoryd dispatch logic | Claude PLAN + GLM BUILD + Codex REVIEW |
| P1 — statistical formula | PSR/DSR implementation | GLM BUILD + Codex REVIEW |
| P1 — integration | Wire cto_planner to factoryd | GLM BUILD + Claude sign-off |
| P2 — bounded impl | New CLI script | GLM BUILD (single model OK) |
| Typo / bookkeeping | Fix comment, update JSON | Any model (no adversarial review needed) |

**No 4 models on a typo. Risk gates determine reviewer count.**

---

## Anti-Hallucination Contract

Every technical claim from any model must be tagged:
- `PROVEN` — executable test result or runtime evidence (receipt)
- `OBSERVED` — seen in logs/output
- `INFERRED` — derived from evidence
- `PROPOSED` — hypothesis, not yet tested
- `UNKNOWN` — explicitly not known

Hallucinations to watch for (from CTO_LESSONS):
- Library version strings (L011: eventstudy MIT→GPL; purgedcv >=0.4→0.1.6)
- Statistical API signatures (L015: PSR takes array not scalars)
- File paths (CTO000: scripts/ not src/joss_orchestrator/)
- Test pass claims without receipt

---

## Model Performance Data

Current samples (2026-09-06):
- Claude: 6 missions (M008D/E/F/H, M007B, F001) — architecture + complex semantic
- GLM: 15+ missions (FD1-FD9, M008G, M009, M010, CTO002-003, F002-F005) — bounded impl + pipelines
- Codex: 0 production missions (not yet integrated)
- Z Code: 1 challenge review (CTO002 adversarial — 3 blockers found)

Full data: `factory/cto/CTO_MODEL_PERFORMANCE.json`
