# Requalify authority migration and derived REPLAN

Offline maintenance brief derived from the six canonical authorities. Run before claiming
that the pre-existing terminal Factory state can resume under the new contracts. This brief
is not a new milestone or a second scheduler. Never remove an old failure to make progress
appear successful.

## Inputs and bounded work

Freeze control/product SHAs, all six authority hashes, both PROJECT_STATE byte hashes,
DURABLE_HANDOFF and quarantine/repair contracts. Use isolated copies of the complete project
state, not a hand-built happy-path state. Builder budget: 3600 s, maximum two patch/review
iterations; reserve review and validation costs separately. No daemon against the live states
while preparing this migration. No provider/engine call is needed for the first reproduction.

## Build order

1. Replay the actual interrupted position on a copy, with model execution disabled. Record
   the selected milestone, old contract id/signature, repair epoch, failure fingerprint and
   first outgoing transition. Classify authority drift separately from the original bug.
2. A normal stop handoff must preserve quarantine. PRODUCT_INTENT_SUPERSEDED requires the
   exact current roadmap identity and independently observed previous authority. Missing
   previous authority is AUTHORITY_MIGRATION_UNPROVEN, followed by ordinary recovery on the
   next cycle, never an invented prior hash or a hand-edited quarantine clear.
3. If the original runtime defect survives, reproduce it against the old committed SHA,
   repair only the implicated source in a detached candidate, run the frozen reproduction
   in two pinned worktrees, and use the existing supervisor receipt/index path. A failed
   reproduction or identical before/after results never opens a fresh repair budget.
4. Requalify LR3 separately. The legacy `tests/test_lr3_autonomous_replan.py` still expects
   autonomous ROADMAP mutation: at the baseline it has 16 failures / 5 passes. Those tests
   cannot establish the newly required derived-plan semantics. Read `_apply_replan_patch`
   and its production caller; implement persisted derived-plan continuation without changing
   STRATEGY/SPEC/ROADMAP. Replace obsolete positive mutation expectations with actual derived
   plan/restart behavior, while keeping adversarial authority-write refusal and CAS tests
   for separately authorized owner migrations. Do not merely remove the failed tests.
5. Freeze the current contract, bind the active reference through the runtime transition,
   and observe one useful continuation from the same interrupted mission. Merely creating
   a new validation_contracts file is insufficient. Historical settlements remain historical
   until independently revalidated; never mass-convert them to fresh COMPLETE.

## Acceptance and negative controls

On copied real states: ordinary stop, missing/unchanged prior identity and stale handoff
preserve quarantine; documented intent supersession preserves its history and occurs once.
Crash before and after each persistence boundary, retry twice in fresh processes, and prove
one active writer, stable failed-attempt accounting and no lost/duplicated mission. Missing
repair proof remains blocked; no-repair-needed authority clarification cannot claim bug FIXED.
For derived REPLAN: valid derived change reaches a different useful next action after restart;
malformed/empty plan, changed acceptance, foreign project and stale contract are refused with
bounded continuation. All three canonical file hashes remain unchanged during autonomous REPLAN.

## Exact next transition

Pass on copies → independently review exact control SHA and migration evidence → perform the
already-authorized migration through the runtime API with a durable before-image → bounded
production canary under an external supervisor → resume the resolver-selected Twitter mission.
Any failure → preserve the terminal record, emit the smallest repair task with fingerprint,
remaining budget and reproduction. Exhausted budget → durable typed block, no immediate spin,
no reset of the episode, no claim that autonomy or the full suite passed.
