# FULL_CODEBASE_STATE_OF_ART_AUDIT

## Outcome

(verbatim `goal` du jalon `FULL_CODEBASE_STATE_OF_ART_AUDIT` — projects/factory/ROADMAP.yaml ; section ajoutée le 2026-09-12 pour satisfaire le préflight check_task NO_OUTCOME_MARKER ; le gate recalcule les acceptances, il ne lit pas cette section.)

Audit multi-AI complet du codebase Factory contre les meilleures implémentations publiques. Claude (architecture/north-star), Codex (correctness/race/durabilité), GLM/Z Code (simplification/duplication/throughput/dead paths/wiring/legacy — même moteur). Deep research: Spec Kit, Temporal, LangGraph, OpenHands, SWE-agent, BMAD, Goose, Microsoft Agent Framework, nouveaux repos publics. Pour chaque subsystem: FUNCTION/CURRENT_IMPL/WHY/LOC/TESTS/E2E/KNOWN_FAILURES/PUBLIC_ALT/DECISION. DECISION ∈ {KEEP, REUSE, ADAPT, WRAP, REPLACE, DEMOTE, DELETE, REJECT_EXTERNAL}.


Derived task brief. NOT canonical intent: the authority for this work is
`projects/factory/STRATEGY.md` / `SPEC.md` / `ROADMAP.yaml` at the frozen contract SHAs.
If this brief and the ROADMAP acceptance ever disagree, the ROADMAP wins and this file is
wrong. Do not edit ROADMAP.yaml, PROJECT_STATE.json, any verifier, any credential or any
owner authority from inside this mission.

## Deliverables (from ROADMAP)
- factory_v2/receipts/FULL_CODEBASE_AUDIT_RECEIPT.json
- the eight audit artifacts already present under `factory_v2/audit/` must be brought current

## Acceptance (verbatim from ROADMAP — the gate recomputes these, it does not read claims)
- every major subsystem audited (canonical brain, supervisor, dispatcher, router, validator, settlement, state)
- >=3 independent AI reviewers, >=8 public references read AS SOURCE (not from memory)
- LOC before/after, planners/retry/state-authority/next-work-path counts
- every KEEP/REUSE/ADAPT/WRAP/REPLACE/DEMOTE/DELETE decision justified by concrete evidence
- FACTORY_CONVERGENCE_LEDGER.json machine-readable, all decisions present

## Bounded scope
- Read the existing `factory_v2/audit/*` artifacts first; this is an UPDATE, not a rewrite.
- Each public reference must be cited with repo + commit + the file actually read.
- The receipt records counts you measured. Do not estimate a count you did not run.
- No code is deleted here — DELETE decisions are APPLIED by FACTORY_CONVERGENCE_RESHAPE.

## Stop conditions
- Budget exhausted → stop and leave the evidence written. A truthful incomplete run beats
  an authored receipt.
- Any acceptance clause you cannot produce independent evidence for → leave it unmet and
  say so in the receipt. `VERIFIER_NOT_IMPLEMENTED` / unmet is an outcome; a fabricated
  boolean is a defect.
- Never write a receipt field the verifier would grade unless a run produced it.
