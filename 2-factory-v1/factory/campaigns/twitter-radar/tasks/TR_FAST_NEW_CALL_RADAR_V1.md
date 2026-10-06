# TR_FAST_NEW_CALL_RADAR_V1 — early incremental detection with real deduplication

## Product outcome

When a trusted KOL opens a genuinely new LONG thesis, the owner learns about it early — once,
not five times — with the detection latency measured and stated.

Authority: `projects/twitter-radar/STRATEGY.md`, `SPEC.md`, `ROADMAP.yaml`. Read from disk.

## Depends on

`TR_TICKER_INTELLIGENCE_V1` COMPLETE.

## What exists already — do not rebuild

- `scripts/incremental_post_ingestion.py` — incremental ingestion with durable cursors.
- `scripts/scrape_manager.py` — cooldown, rotation, typed provider states.
- `research/tr_call_ledger.json` — the frozen episode ledger; episode identity is
  `account × ticker × thesis`.
- `research/tr_ticker_table.json` — ticker lifecycle, already carrying `NEW_CALL`.

Reuse the existing cursor machinery. Do not invent a second ingestion path.

## Required behaviour

### Incremental, not a full rescan

Detection reads only what arrived after a durable watermark, written atomically. A crash
between two runs must not re-emit an already-emitted alert and must not skip a window.

### The deduplication rule — this is the milestone

The unit is the **episode**, not the tweet.

- A repeat, push, or strengthening tweet on an already-open episode is **not** a new call. It
  updates conviction and the timeline.
- A new alert requires a genuinely new episode: a first qualified LONG tweet by that account
  on that ticker, or a new thesis after an explicit close/invalidation.
- Coordinated hype must not manufacture confidence. N accounts on one ticker count as N only
  when they are independent originators; retweets, quotes and echoes of the same origin
  collapse to one source, exactly as `CONSENSUS_STRENGTH` already defines independence.

### Latency, measured not claimed

For every emitted alert record: source post timestamp, detection timestamp, and the delta.
Latency is an observed measurement written from the run, never a target written by hand.

### Honest output types

Each alert carries one of `RESEARCH_NOW | WATCH | TOO_EXTENDED | LOW_CONFIDENCE | AVOID |
INSUFFICIENT_DATA`, with reasons, evidence post ids, uncertainty and timestamp. Emitting
nothing during a quiet window is a correct outcome — never pad the feed to look active.

A provider cooldown or blocked credential is a typed provider state on the run, never
"no new calls".

## Deliverables

- `research/tr_new_call_radar.json` — emitted alerts with type, reasons, evidence ids,
  measured latency and dedup decision for each.
- `research/tr_radar_watermark.json` — the durable, atomically written detection watermark.
- `research/tr_radar_dedup_ledger.json` — every candidate considered and why it was emitted
  or suppressed. Suppression must be auditable, not invisible.

## Black-box acceptance

```
PYTHONPATH="${FACTORY_CONTROL_REPO}" python3 -m factory_v2.product_verifiers \
    TR_FAST_NEW_CALL_RADAR_V1 --project-repo . --owner-shortlist 'Frenchie'
```

Supervisor-owned. The verifier observes an incremental detection with measured latency and
real deduplication — it replays a controlled corpus delta and requires the dedup ledger to
match what the episode rule implies. An authored counter will not pass.

## Constraints

- Do not modify Factory source, canonical authority, `PROJECT_STATE.json` or any verifier.
- Do not touch `is_call()` / `PositionEvent` (frozen, ADR001).
- LONG-only. No alert may originate from a SHORT reading.
- Respect frozen rate limits: 1 req / 2s, max 100/h.
