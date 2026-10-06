# TR_WEB_PRODUCT_V1 — the surface the owner actually uses

## Product outcome

In under five minutes the owner opens a local web product and answers: which KOLs to believe,
which tickers to research now, what changed this week, and why — every claim clickable down to
the source post.

Authority: `projects/twitter-radar/STRATEGY.md`, `SPEC.md`, `ROADMAP.yaml`. Read from disk.

## Depends on

`TR_WEEKLY_DEEP_REFRESH_V1` COMPLETE.

## What exists already — do not rebuild

- `research/tr_kol_ranking.json`, `research/tr_kol_universe.json` — KOL side.
- `research/tr_ticker_table.json` — ticker side.
- `research/tr_call_ledger.json` — episodes, timelines, source post ids.
- `research/tr_weekly_report.json`, `research/tr_weekly_delta.json` — the weekly surface.

This milestone **presents** existing truth. It must not recompute scores, re-derive episodes
or invent any field that the pipeline did not produce. A discrepancy between a page and its
JSON source is a defect in the page.

## Required behaviour

### Four linked first-class surfaces

1. **KOL table** — handle; expertise and AI sub-industries; lifecycle state; genuine call
   count and historical coverage; LONG profitability and hit rate per fixed horizon
   (1/5/20/60/120); market/sector/momentum-adjusted edge; MFE, MAE, drawdown; PSR/DSR/PBO and
   OOS status; originality and lead/lag role; consistency, recency, data-quality confidence;
   current active theses; explainable final score and tier.
2. **KOL profile** — full call history, thesis timelines, performance curves, best and worst
   calls, current theses, evidence posts.
3. **Ticker table** — lifecycle, name and plain-language description, sub-industry and
   value-chain role, geography, size, market cap and valuation, fundamentals, price and
   momentum including extended-hype flag, first KOL call and conviction evolution, number and
   independence of supporting/contradicting KOLs, those KOLs' historical performance on
   comparable calls, liquidity and data-quality warnings, risk tier, explainable opportunity
   score with reasons for and against.
4. **Ticker detail** — company snapshot, thesis timeline, KOL graph, original evidence, price
   path, fundamentals, risks, consensus evolution, score explanation.

### Non-negotiable UX invariants

- Usable with hundreds of rows. The universe must scale to 200–300 KOLs with no contract change.
- Filters on industry, sub-industry, size, geography, lifecycle, recency, KOL, risk tier,
  confidence, momentum state and score range.
- Every displayed score decomposes into named components and links to source evidence. **No
  opaque aggregate alone determines a recommendation.**
- **Cross-surface consistency**: the same episode reads identically from the KOL side and the
  ticker side. This is a testable invariant, not a style goal.
- Typed honest states render as themselves — `INSUFFICIENT_DATA`, `OOS_INCONCLUSIVE`,
  `DEGRADED_DATA_SOURCE`, `INSUFFICIENT_QUALIFIED_ACCOUNTS` are displayed, never blanked or
  rounded into a number.

### Forbidden

No trading path, no order placement, no broker connection, no profitability promise. No
network egress to third parties from the page. No credential ever reaches the client.

## Deliverables

- The web application source, started by a single documented command, serving on localhost.
- `research/tr_web_product_manifest.json` — routes, the JSON source backing each surface, and
  the field-to-source mapping used for the consistency invariant.

## Black-box acceptance

```
PYTHONPATH="${FACTORY_CONTROL_REPO}" python3 -m factory_v2.product_verifiers \
    TR_WEB_PRODUCT_V1 --project-repo . --owner-shortlist 'Frenchie'
```

Supervisor-owned. The verifier **starts the product at the exact SHA** and exercises HTTP and
DOM for tables, profiles, detail pages, filters and links. A screenshot, a template or an
authored boolean proves nothing.

## Constraints

- Do not modify Factory source, canonical authority, `PROJECT_STATE.json` or any verifier.
- Do not recompute product truth in the presentation layer.
- Local-only. No deployment, no public exposure, no external hosting.
