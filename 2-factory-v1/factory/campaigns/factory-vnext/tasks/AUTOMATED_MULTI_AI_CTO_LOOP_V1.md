# AUTOMATED_MULTI_AI_CTO_LOOP_V1 — production wiring (bounded)

## Outcome

(verbatim `goal` du jalon `AUTOMATED_MULTI_AI_CTO_LOOP_V1` — projects/factory/ROADMAP.yaml ; section ajoutée le 2026-09-12 pour satisfaire le préflight check_task NO_OUTCOME_MARKER ; le gate recalcule les acceptances, il ne lit pas cette section.)

Cellule multi-IA automatique: writer → challenger/recherche read-only → reviewer exact-SHA → synthèse déterministe → redirection writer → reprise à la position produit exacte


Derived task brief. Authority is `projects/factory/{STRATEGY.md,SPEC.md,ROADMAP.yaml}` at the
frozen contract SHAs. Where this brief and the ROADMAP acceptance disagree, the ROADMAP wins.

## Why this task exists (read before touching code)

`factory_v2/audit_cell_exercise.py` drives the cell machinery end to end, and it is genuinely
useful — but it is **not** the production proof of this milestone and must never be presented as
one:

* `factory_v2/cell_challenger.py` and `factory_v2/cell_reviewer.py` are DETERMINISTIC programs
  (hash / HEAD / fingerprint checks), launched by `audit_loop.run_reviewer` through `python -I`.
  No model is invoked. A deterministic reviewer is a structural validator, not a CTO review.
* The exercise builds its own sandbox failure signal and its own control root. It therefore
  cannot establish A1 (a REAL failure in the production path triggered the loop) or the
  production continuation semantics, which are properties of `advance()` — not of a harness.

So the exercise stays as a **regression test**, and this task builds the missing production
behaviour.

## Frozen inputs (do not change any of these to pass)

- `projects/factory/ROADMAP.yaml` acceptance for this milestone (read it in full; D018/D019 and
  the SOL-R1 continuation clauses are part of it).
- `factory_v2/audit_loop.py` — trigger detection, capability envelope, evidence bundle,
  `synthesize` / `finalize_synthesis`, `MAX_ADVERSARIAL_ROUNDS`.
- `factory_v2/audit_cell_bridge.py` — the bridge from the runtime to the cell.
- `factory_v2/runtime.py` — `advance()` is where the loop must actually be reachable; the
  `_a1a6_gate` already refuses a settlement when the cell is not reachable from the runtime.
- `factory_v2/product_verifiers.py` — the gate. It is supervisor-owned: do not edit it, do not
  import product scoring into it, do not weaken it.
- `factory/bin/model_router.py` + `factory/bin/owner_routing_policy.py` — routing identity and
  the scoped, expiring owner policy.

## Source to reuse (do not rewrite)

Single-flight (`factory_v2/single_flight.py`), the two-phase RunRecords API
(`record_prediction` / `record_observation` / `reconcile_orphan_predictions`), the worker lease
and fencing code, `repair_epoch`, `settlement_truth`, `verified_fix`. This task WIRES existing
parts; a new framework is out of scope and will be rejected.

## Deliverables

1. A real production trigger path: a failure observed by the supervisor in the normal dispatch
   path (not a sandbox-authored one) opens the cell, with the frozen work package, bounded
   budget and bounded adversarial rounds.
2. Continuation semantics on the production chain: every typed failure outcome (role crash,
   budget/deadline timeout, reviewer REJECT/REDIRECT/QUARANTINE, quota exhaustion) writes a
   DURABLE outgoing transition and the product resumes at the exact position, no loss and no
   duplicate.
3. The **GLM read-only CTO reviewer path**, actually spawned: the reviewer role reaches an
   engine subprocess whose argv is observed, whose write tools are denied, and whose spawn is
   recorded. `EngineChoice.read_only=True` is metadata only until this exists — today
   `dispatcher_v3` only ever routes `build` and `repair`, so no production caller reaches the
   `review` branch at all. Closing that is the core of this deliverable.
4. `factory_v2/receipts/AUDIT_LOOP_RECEIPT.json`, produced by the run — never authored.

## Explicit non-goals and hard prohibitions

- Do NOT relabel any canonical task's `spec_level` from A to B to make routing succeed.
- Do NOT widen `_ACTOR_CAPABILITY`. GLM as an A-level BUILDER needs a separate owner decision.
- Do NOT remove the independent Codex review where canonical acceptance requires three engines:
  the temporary Claude-builder / GLM-reviewer assignment is additive and expiring, and it may
  not silently erase the independent Codex gate.
- Do NOT let the deterministic `cell_reviewer.py` be described, in code or receipt, as a model
  review. Keep it, name it what it is.
- Do NOT touch PROJECT_STATE, credentials, the evaluator, or any authority document.

## Positive and negative cases (both required)

POSITIVE — a real dispatch failure in the production path opens the cell exactly once
(single-flight), a read-only reviewer subprocess is spawned with observed argv and denied write
tools, synthesis is deterministic and re-derivable from the recorded inputs, the writer is
redirected, and the interrupted milestone resumes at its exact position.

NEGATIVE — each of these must FAIL CLOSED and be covered by a test:
- an authored/sandbox failure signal does not open the production cell;
- a reviewer that attempts a write is denied and the attempt is recorded;
- two concurrent triggers produce ONE cell run (no duplicate launch);
- a peer message is never an instruction or an authorization
  (`PEER_CONSENSUS_OVERRIDES_GATE=0`, `BUILDER_CONTROLLED_VALIDATOR=0`);
- a third adversarial round is refused (`MAX_ADVERSARIAL_ROUNDS`);
- a crash between trigger and synthesis replays without loss or duplicate.

## Budget, typed failure and exact next transition

- Budget: 5400 s wall, two adversarial rounds maximum, no model spend beyond the routed
  reviewer/writer calls this milestone needs.
- Typed failure: if the read-only reviewer subprocess path cannot be built inside the budget,
  stop and report `UNPROVEN_IMPLEMENTATION: production reviewer path` in the receipt. Leave the
  milestone NOT settled. Do not settle on the exercise alone, and do not claim any readiness
  milestone that depends on this one.
- Next transition on success: the gate
  (`python3 -m factory_v2.product_verifiers AUTOMATED_MULTI_AI_CTO_LOOP_V1`) runs and, if it
  passes, the resolver selects the next milestone in dependency order.
- Next transition on typed failure: `FULL_CODEBASE_STATE_OF_ART_AUDIT` and everything after it
  stay blocked; the honest state is "base readiness not proven", not "loop complete".
