# CYCLE_2_REFRESH_2_AND_PRODUCT_E2E — Second Autonomous Refresh + Product E2E

## Context

CYCLE_2_REFRESH_1_AUTO produced `research/cycle2_refresh_1.log`.
This is the second autonomous refresh, proving repeatable autonomy.

**Repo**: `/Users/jocelyngrosjean/conductor/workspaces/twitter-radar/twitter-radar-foundation-v2`

## Task

1. Load `research/cycle2_enriched_manifest.json` — 25 accounts.
2. Load `research/cycle2_refresh_1.log` to get the reference timestamp from refresh 1.
3. Scan for any calls timestamped AFTER the timestamp in refresh 1's REFRESH_COMPLETE event.
4. Count `new_calls` (may be 0 — that is a valid honest result).
5. Generate a refresh report SHA distinct from refresh 1's report_sha.
6. Append a JSONL entry to `research/cycle2_refresh_2.log`:
   ```json
   {"event": "REFRESH_COMPLETE", "timestamp": "YYYY-MM-DDTHH:MM:SSZ", "accounts_refreshed": N, "new_calls": M, "report_sha": "hex16", "date_report": "YYYY-MM-DD"}
   ```
7. Also produce `research/cycle2_product_e2e.json` containing:
   - `refresh_count`: 2
   - `total_accounts`: accounts from manifest
   - `total_new_calls_all_refreshes`: sum across refresh 1 and 2
   - `pipeline_verdict`: "OPERATIONAL" if refresh_count >= 2, else "INCOMPLETE"
   - `produced_at`: ISO8601 UTC timestamp

## Acceptance

Files exist and validate:
1. `research/cycle2_refresh_2.log` contains >= 1 REFRESH_COMPLETE event with distinct report_sha vs refresh 1:
   ```
   python3 factory_v2/verify_cycle2_refresh.py research/cycle2_refresh_2.log --min-refreshes 1 --verify-distinct-from research/cycle2_refresh_1.log
   ```
2. `research/cycle2_product_e2e.json` exists with pipeline_verdict = "OPERATIONAL".

## Notes

- 0 new calls is valid — records the honest attempt.
- report_sha MUST differ from refresh 1's report_sha (timestamp or date_report will differ).
- Do NOT copy refresh 1's log verbatim.
