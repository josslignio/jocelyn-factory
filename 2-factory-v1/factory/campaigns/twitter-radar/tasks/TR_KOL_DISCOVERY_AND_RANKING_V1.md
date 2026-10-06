# TR_KOL_DISCOVERY_AND_RANKING_V1 — discovery graph, lifecycle, qualification, KOL table

## Product outcome

The owner can see, for every followed and candidate account, whether it is worth trusting —
with the score broken into named components and every component traceable to evidence.

Authority: `projects/twitter-radar/STRATEGY.md`, `SPEC.md`, `ROADMAP.yaml`. Read from disk.
This file is derived context, never product authority.

## Depends on

`TR_DATA_BACKFILL_AND_CREDENTIAL_POOL` must be COMPLETE: the corpus and the owner-rooted
universe already exist. Do not re-collect.

## Required behaviour

- Build the discovery graph from retweets, quotes, replies and repeated thesis co-occurrence
  around already-qualified accounts. Tickers are out of scope here; this milestone ranks
  accounts.
- Move accounts through `DISCOVERED → SAMPLED → MONITORED → QUALIFIED | DEMOTED | REJECTED`
  with a recorded reason for every transition. The owner shortlist stays root and is never
  demoted automatically.
- Distinguish domain experts from promotional and copy accounts using observable behaviour,
  not follower count.
- Score every account into **named components** — no opaque aggregate may decide a tier.
- Qualification: more than five genuine LONG calls and at least four months of observable
  history permit `EARLY_EVIDENCE`. Confidence rises continuously with call count and history
  length. `SOLID`/`ELITE` additionally require positive adjusted evidence, disclosed sample
  uncertainty and disclosed OOS status.
- If fewer than ten accounts qualify, return the number that do plus the verdict
  `INSUFFICIENT_QUALIFIED_ACCOUNTS`. Never pad the list.

## Deliverables

- `research/tr_kol_table.json` — one row per account with lifecycle, call count, coverage,
  score components, tier, uncertainty and OOS status.
- `research/tr_discovery_graph.json` — edges with their observed evidence (post ids).

## Black-box acceptance

```
python3 <control-repo>/factory_v2/product_verifiers.py \
    TR_KOL_DISCOVERY_AND_RANKING_V1 --project-repo . --owner-shortlist 'Frenchie'
```

This verifier is supervisor-owned and you cannot edit it. **It is not implemented yet**: it
currently returns `VERIFIER_NOT_IMPLEMENTED` (exit 2) and this milestone therefore cannot
settle. That is deliberate — a milestone whose outcome cannot be independently recomputed
fails closed rather than passing on a shape check.

Build the product and the evidence it needs. Do not attempt to make the gate pass by
producing artifacts in a particular shape; the gate will recompute scores and tiers from the
episode ledger when it exists.

## Constraints

- Do not modify Factory source, canonical authority or `PROJECT_STATE.json`.
- Do not weaken the acceptance command or the verifier.
