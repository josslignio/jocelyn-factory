# CYCLE_2_REFRESH_1_AUTO — First Autonomous Weekly Refresh

## Context

CYCLE_2_INGESTION_ELASTIQUE produced `research/cycle2_universe_manifest.json` (25 accounts).
CYCLE_2_INGESTION_ELASTIQUE#M2 produced `research/cycle2_enriched_manifest.json` (enriched).

This is the first autonomous refresh run: re-run the pipeline on the selected 25 accounts,
collect any new call data since M2, and record the refresh event.

**Repo**: `/Users/jocelyngrosjean/conductor/workspaces/twitter-radar/twitter-radar-foundation-v2`

## Task

1. Load `research/cycle2_enriched_manifest.json` — get the 25 selected accounts.
2. For each account, scan `research/X104_OUTCOME_LEDGER.json` and the cycle2 corpus for calls
   since the `cycle2_enriched_manifest.json` was produced (use its `produced_at` or creation date).
3. Count `new_calls` (calls timestamped after the last manifest date; may be 0 if none).
4. Count `accounts_refreshed` (always = number of accounts processed, even if new_calls=0).
5. Generate a short refresh report SHA (sha256 of the JSON summary).
6. Append a JSONL entry to `research/cycle2_refresh_1.log` with this exact schema:
   ```json
   {"event": "REFRESH_COMPLETE", "timestamp": "YYYY-MM-DDTHH:MM:SSZ", "accounts_refreshed": N, "new_calls": M, "report_sha": "hex16", "date_report": "YYYY-MM-DD"}
   ```
   - `timestamp`: UTC ISO8601 now
   - `accounts_refreshed`: number of accounts processed (from manifest)
   - `new_calls`: integer count of genuinely new calls found (0 if none)
   - `report_sha`: sha256[:16] of the summary dict
   - `date_report`: today's date YYYY-MM-DD

## Acceptance

File `research/cycle2_refresh_1.log` exists and contains at least one REFRESH_COMPLETE
event line validated by:
```
python3 factory_v2/verify_cycle2_refresh.py research/cycle2_refresh_1.log --min-refreshes 1
```

Run from: `/Users/jocelyngrosjean/conductor/workspaces/Jocelyn-factory-control/factory-foundation-v2`

## Notes

- 0 new calls is a valid honest result — the refresh records the attempt.
- Do NOT invent call data. Record only genuinely new calls from the corpus.
- The log must be valid JSONL (one JSON object per line).
- Write to the product repo path: `research/cycle2_refresh_1.log` relative to the product repo.
