# TR_DATA_BACKFILL_AND_CREDENTIAL_POOL — safe resumable collection over the owner-rooted KOL universe

## Product outcome

Twitter Radar can collect and re-collect the owner's KOL universe from X safely, for as long
as the product runs, without burning the credential pool and without ever turning a provider
problem into a false "no signal".

Authority: `projects/twitter-radar/STRATEGY.md`, `SPEC.md`, `ROADMAP.yaml`. Read them from
disk. This file is derived context, never product authority.

## What exists already — do not rebuild

- `scripts/scrape_manager.py` — twscrape orchestration with cooldown/auth-failure handling
  and terminal statuses (`terminal`, `blocked_auth`, `unavailable`).
- `scripts/incremental_post_ingestion.py`, `scripts/x101_scrape_new_accounts.py`,
  `scripts/live_collector.py`.
- `accounts_live.db` — the credential pool (4 authenticated X accounts).
- `data/corpus/<handle>/posts.jsonl` + `data/corpus/CORPUS_MANIFEST.json` — 13 accounts of
  existing history.

Reuse these. Extend them only where the acceptance below is not already met, and only with
evidence of the gap.

## Required behaviour

### Owner-rooted universe

Produce `research/tr_kol_universe.json` describing the root universe: the owner shortlist
(starting with **Frenchie** and the already-selected accounts) plus any account already
carrying corpus history. Every entry carries a lifecycle state from
`DISCOVERED | SAMPLED | MONITORED | QUALIFIED | DEMOTED | REJECTED` and the reason for it.
The owner shortlist is flagged as root and is never pruned automatically.

Do **not** invent a ticker universe here. This milestone collects accounts, not tickers.

### Credential-pool safety

- Never write a credential, cookie, token or password into source, prompt, log, receipt or
  report. Redact by construction, not by post-processing.
- Respect the frozen rate limits: 1 request / 2s, max 100/h, ToS-compliant.
- Detect cooldown and auth failure; rotate accounts; back off. No rapid retry storm.
- Record per-account provider health as a typed state.

### Durable cursors and resumable backfill

- One durable cursor/checkpoint per account, written atomically.
- A crash mid-backfill must resume without skipping a time range and without duplicating a
  post. Post identity is the X post ID.
- Attempt one to two years of history where available. Four months is the minimum
  eligibility bar. Where the target is not reachable, record the shortfall explicitly —
  never round it up and never present a partial range as complete.

### Honest provider semantics

A cooldown, a blocked credential, a suspended account or an incomplete API result is a typed
provider state. It is never recorded as "no posts", "no calls" or "no signal".

## Deliverables

- `research/tr_kol_universe.json`
- `research/tr_collection_report.json` — per account: handle, lifecycle state, root flag,
  post count, first/last post timestamps, months covered, cursor position, provider state,
  and any explicit shortfall.
- Durable per-account cursors on disk.
- Updated `data/corpus/CORPUS_MANIFEST.json` covering every collected account.

## Black-box acceptance

The single supervisor-owned verifier named by canonical authority:

```
python3 <control-repo>/factory_v2/product_verifiers.py \
    TR_DATA_BACKFILL_AND_CREDENTIAL_POOL --project-repo . --owner-shortlist 'Frenchie'
```

It is the same command `ROADMAP.yaml` uses — there is exactly one definition of success,
and this mission cannot edit either the verifier or the shortlist. It **recomputes** from
raw immutable inputs rather than reading your report's own numbers:

1. the universe file is valid and every entry carries a legal lifecycle state;
2. the owner root universe is checked against the shortlist declared in AUTHORITY — you
   cannot nominate your own root account, and you cannot omit one of the owner's;
3. every account claimed as collected is **recounted from `data/corpus/<handle>/posts.jsonl`
   on disk**, and a `post_count` that disagrees with the file is a failure;
4. every corpus line must be a real post with an id and a timestamp — `{}` is not a post;
5. coverage in months is **recomputed from the post timestamps**, not read from the report;
   under 4 months requires an explicit typed `shortfall_reason`;
6. `corpus_sha`, when present, must match the bytes on disk;
7. cursors must exist; provider states must be typed;
8. no credential-shaped material anywhere in the universe or report.

Exit 0 means proven, 1 means not met, 2 means the verifier could not evaluate. There is no
authored boolean you can set to make this pass.

## Constraints

- Do not modify Factory source (`factory/`, `factory_v2/`) from this mission.
- Do not modify `STRATEGY.md`, `SPEC.md`, `ROADMAP.yaml` or `PROJECT_STATE.json`.
- Do not weaken the acceptance command or the verifier to make the mission pass.
- Commit only the deliverables listed above and the code needed to produce them.
