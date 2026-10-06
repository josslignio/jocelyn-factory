# Bench repairs held OUTSIDE the frozen zone

`campaigns/RUN8` is frozen (`factory-v1-run8-baseline-v2`): it holds the paused
benchmark's evidence, and a guard fails the suite if anything there is written.

On 2026-09-10 at 06:10 (`54e5eb0e`) I repaired
`campaigns/RUN8/P5/holdout_mcnemar.py` in place — inside the frozen zone. The
repair itself was substantive:

* `effective_per_cell()` returned only the last attempt per cell (last-write-wins),
  discarding the append-only history; it now returns all attempts.
* `_cell_outcome()` now takes the majority of VALID attempts, ties resolving False.
* `paired_mcnemar()` keyed on `(task, rep)`, so replays of the same task counted as
  independent pairs and inflated n; it now keys on TASK and aggregates replays first.

Those are real statistical defects, so the fix should not be thrown away. But the
freeze exists precisely so bench evidence cannot drift, and writing to it silently
is the failure the guard is there to catch — so the frozen copy is restored to the
baseline and the corrected script lives here instead.

**Nothing has been recomputed.** The numbers published in `PHASE_B_RESULT.md`,
`PHASE_B_V2_RESULT.md` and `FACTORY_AUDIT_DOSSIER.md` were produced by the ORIGINAL
script and therefore carry the defects above — most importantly, an inflated n from
rep-level pairing. They should not be cited as final until the bench is formally
resumed and re-run with this script under a NEW baseline tag.

That is an owner decision, not a routine one: it changes published benchmark
figures.
