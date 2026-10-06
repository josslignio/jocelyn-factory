# CLAUDE_ALONE_VS_GLM_ALONE_VS_FACTORY_BENCHMARK

## Outcome

(verbatim `goal` du jalon `CLAUDE_ALONE_VS_GLM_ALONE_VS_FACTORY_BENCHMARK` — projects/factory/ROADMAP.yaml ; section ajoutée le 2026-09-12 pour satisfaire le préflight check_task NO_OUTCOME_MARKER ; le gate recalcule les acceptances, il ne lit pas cette section.)

Benchmark apparié held-out: Claude seul vs GLM/Z Code seul vs Factory multi-IA


Derived task brief. NOT canonical intent: the authority for this work is
`projects/factory/STRATEGY.md` / `SPEC.md` / `ROADMAP.yaml` at the frozen contract SHAs.
If this brief and the ROADMAP acceptance ever disagree, the ROADMAP wins and this file is
wrong. Do not edit ROADMAP.yaml, PROJECT_STATE.json, any verifier, any credential or any
owner authority from inside this mission.

## Deliverables (from ROADMAP)
- factory_v2/run_matched_benchmark.py (the missing producer)
- factory_v2/receipts/MATCHED_BENCHMARK_RECEIPT.json

## Acceptance (verbatim from ROADMAP — the gate recomputes these, it does not read claims)
- three arms: claude_alone, glm_alone, factory
- identical task, start SHA, acceptance, data, budget and fault injection across arms
- >=3 held-out runs per arm when cost allows
- metrics: verified delivery, elapsed/active time, tokens/cost, false acceptances, temperature of retries
- acceptance and budget accounting equal between arms
- explicit verdict, INCLUDING when the Factory does not win

## Bounded scope
- Build the harness first, with the arms as data, so a fourth arm needs no new code.
- Budget accounting must be per-arm and comparable; an arm that spent more must show it.
- A verdict of 'factory does not win' is a successful benchmark. Report it as such.
- Real model calls cost money: the harness takes --dry-run and --max-cost-usd and refuses
  to start when the budget is absent.

## Stop conditions
- Budget exhausted → stop and leave the evidence written. A truthful incomplete run beats
  an authored receipt.
- Any acceptance clause you cannot produce independent evidence for → leave it unmet and
  say so in the receipt. `VERIFIER_NOT_IMPLEMENTED` / unmet is an outcome; a fabricated
  boolean is a defect.
- Never write a receipt field the verifier would grade unless a run produced it.
