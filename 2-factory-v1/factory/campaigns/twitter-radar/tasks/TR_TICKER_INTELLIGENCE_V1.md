# TR_TICKER_INTELLIGENCE_V1 — ticker qualification, lifecycle, risk and opportunity

## Product outcome

For every ticker that KOLs actually talked about, the owner can see what the company is, where
it sits in the AI value chain, how many *independent* KOLs back it, what the risk is, and an
opportunity score they can take apart into named reasons.

Authority: `projects/twitter-radar/STRATEGY.md`, `SPEC.md`, `ROADMAP.yaml`. Read from disk.
This file is derived context, never product authority.

## Depends on

`TR_CALL_THESIS_TRACK_RECORD_V1` COMPLETE.

## What exists already — do not rebuild

- `research/tr_call_ledger.json` — 389 episodes over 216 distinct tickers, each carrying
  `account`, `ticker`, `thesis`, `opening_tweet_id`, `tweet_date`, `pushes`, `outcome`,
  `closure`. This is the ONLY admissible source of the ticker universe.
- `research/tr_kol_universe.json`, `research/tr_kol_ranking.json` — account lifecycle and tier.
- `scripts/build_master_tickers.py` — existing aggregation over clean CSVs. Inspect before
  writing anything new; reuse or state why it does not fit.
- `yfinance>=0.2` is already an accepted dependency (Apache-2.0, XR03 price intake).

## Required behaviour

### Universe provenance — KOL-first, never screener-first

Every ticker in the output MUST trace to at least one episode in `tr_call_ledger.json`. A
ticker that no KOL called does not exist for this product. Never seed from an exchange list,
an index, a screener or a generic market feed.

### Lifecycle

Each ticker carries exactly one of `NEW_CALL | ACTIVE_CALL | OLD_CALL | CLOSED | INVALIDATED`,
derived from its episodes' dates and closure states — not authored. Record the rule that
produced the state and the episode ids it read.

### Independence — the counting rule that must not be broken

Ten tweets from one KOL are one KOL. Supporting-KOL count is a count of **distinct accounts**
with a qualified LONG episode on that ticker. Where an account followed rather than
originated, that is recorded and weighted, never silently equal to origination.

### Enrichment with honest provenance

Company name, plain-language description, AI sub-industry and value-chain role, market /
country, size bucket, market capitalisation, valuation, revenue/growth, cash, debt,
profitability, current price and momentum state including whether hype is already extended.

Every enriched field carries its source and retrieval timestamp. A provider that is
unavailable, rate-limited or missing a field yields a typed degraded state
(`PROVIDER_UNAVAILABLE`, `FIELD_NOT_PROVIDED`) — **never** a zero, a blank treated as fact, or
an inferred number. `INSUFFICIENT_DATA` is a valid, shippable answer.

### Risk and opportunity

- Risk tier `LOW | MODERATE | HIGH | SPECULATIVE`, from size, liquidity, data quality and
  dispersion of evidence. Record the inputs that decided the tier.
- Opportunity score decomposable into named components, each with its own value and weight,
  plus explicit `reasons_for` and `reasons_against`. No opaque aggregate may stand alone.
- Liquidity and data-quality warnings are surfaced on the row, not buried.

### Forbidden

No SHORT path. No trade execution. No profitability promise. No entry/exit re-selection after
observing the future — this milestone consumes frozen episodes, it never recomputes entries.

## Deliverables

- `research/tr_ticker_table.json` — one row per ticker with every field above, each score
  decomposed, each claim traceable to episode ids and source posts.
- `research/tr_ticker_enrichment_provenance.json` — per-field source, timestamp and typed
  provider state for every enriched value.

## Black-box acceptance

```
PYTHONPATH="${FACTORY_CONTROL_REPO}" python3 -m factory_v2.product_verifiers \
    TR_TICKER_INTELLIGENCE_V1 --project-repo . --owner-shortlist 'Frenchie'
```

Supervisor-owned. The mission MUST NOT create, edit or weaken it. The verifier independently
re-derives the ticker universe from `tr_call_ledger.json` and recomputes distinct-KOL
agreement; shaping the artifact will not pass it.

## Constraints

- Do not modify Factory source, canonical authority, `PROJECT_STATE.json` or any verifier.
- Do not touch `is_call()` / `PositionEvent` (frozen, ADR001).
- Never write a credential, cookie or token into source, log, receipt or report.
