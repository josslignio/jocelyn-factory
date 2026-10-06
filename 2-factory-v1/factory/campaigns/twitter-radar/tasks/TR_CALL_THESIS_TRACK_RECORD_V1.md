# TR_CALL_THESIS_TRACK_RECORD_V1 — thesis reconstruction and frozen counterfactual returns

## Product outcome

For every account and ticker, the owner can see what the account actually called, when, and
what a disciplined follower would have earned — computed once, by a frozen method, with no
look-ahead.

Authority: `projects/twitter-radar/STRATEGY.md`, `SPEC.md`, `ROADMAP.yaml`. Read from disk.

## Depends on

`TR_KOL_DISCOVERY_AND_RANKING_V1` COMPLETE.

## Required behaviour

- The canonical unit is one **`account × ticker × thesis episode`**.
- Entry is the first reasonably tradable market price **after that account's first qualified
  LONG tweet** about the ticker. Never an earlier intraday low. Never a hindsight entry.
- Later pushes, repetitions and strengthening tweets update conviction and the timeline. They
  **never** reset the entry price and never create additional winning calls. Only an explicitly
  closed or invalidated episode followed by a genuinely new thesis opens a new episode.
- V1 is LONG-only. No SHORT episode may exist.
- Record every transition with its timestamp and source post id: first mention, conviction,
  additions, reductions, exit, reversal, abandonment, and whether the author originated the
  thesis or followed someone else.
- Evaluate at fixed horizons of 1/5/20/60/120 trading sessions plus the observable exit when
  the account explicitly closes or invalidates.
- Report raw **and** market/sector/momentum-adjusted returns, plus MFE, MAE and maximum
  drawdown.
- Freeze the methodology before computing any return: market hours, execution delay, corporate
  actions, currency, delistings, liquidity and missing prices. Record it with a hash.

## Deliverables

- `research/tr_methodology_frozen.json` — the frozen rules and their hash.
- `research/tr_thesis_episodes.json` — every episode with entry provenance, timeline, returns
  and path statistics.

## Black-box acceptance

```
python3 <control-repo>/factory_v2/product_verifiers.py \
    TR_CALL_THESIS_TRACK_RECORD_V1 --project-repo . --owner-shortlist 'Frenchie'
```

Supervisor-owned and not editable by this mission. **Not implemented yet** — it returns
`VERIFIER_NOT_IMPLEMENTED` (exit 2) and the milestone cannot settle. When implemented it will
recompute entry prices, episode boundaries and returns from the raw corpus and market data;
shaping the artifact will not help.

## Constraints

- Do not modify Factory source, canonical authority or `PROJECT_STATE.json`.
- Do not weaken the acceptance command or the verifier.
