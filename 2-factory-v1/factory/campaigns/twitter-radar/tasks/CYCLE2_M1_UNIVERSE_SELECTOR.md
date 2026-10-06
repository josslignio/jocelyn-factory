# CYCLE_2 M1 — Universe Selector

## Context

Twitter Radar V1 (35d6fc1) delivered the canonical query on 5 accounts.
CYCLE_2 milestone `CYCLE_2_INGESTION_ELASTIQUE` requires expanding to 20+ accounts.

X109 Recursive Discovery produced 32 candidates in:
`research/X109_DISCOVERY_CANDIDATES.json`

Schema per candidate:
```json
{"handle": "...", "edge_count_to_elite": 1, "edge_count_to_solid": 1,
 "authority_score": 0.026, "hub_score": 0.001, "pagerank": 0.0015,
 "discovery_score": 1.0, "rank": 1}
```

## Task

Create `research/cycle2_universe_selector.py` that:

1. Loads `research/X109_DISCOVERY_CANDIDATES.json`
2. Scores each candidate using a composite score:
   `composite = 0.4 * discovery_score + 0.3 * authority_score_normalized + 0.2 * pagerank_normalized + 0.1 * edge_bonus`
   where:
   - `authority_score_normalized` = authority_score / max(authority_scores)
   - `pagerank_normalized` = pagerank / max(pageranks)
   - `edge_bonus` = 1.0 if edge_count_to_elite >= 2 else (0.5 if edge_count_to_elite == 1 else 0.0)
3. Selects top 25 candidates by composite score
4. Produces `research/cycle2_universe_manifest.json` with schema:
   ```json
   {
     "accounts": [
       {"handle": "...", "rank": 1, "composite_score": 0.95,
        "call_count": 0, "status": "pending_scrape",
        "discovery_score": ..., "authority_score": ...}
     ],
     "total_selected": 25,
     "selection_method": "composite_score_v1",
     "source": "X109_DISCOVERY_CANDIDATES.json",
     "corpus_sha": "",
     "scraped_at": "",
     "produced_at": "<ISO timestamp>"
   }
   ```
   Note: `call_count=0` and `status="pending_scrape"` are correct — this is the pre-scrape manifest.
   `corpus_sha` and `scraped_at` will be filled in after scraping.

5. Also produces a human-readable summary to stdout showing the top 25 selected handles and their scores.

## Tests

Create `tests/test_cycle2_universe_selector.py` with at least 3 tests:
- Test that 25 accounts are selected from 32 candidates
- Test that composite score formula is correct for a known input
- Test that output JSON matches the required schema

## Acceptance

- `python3 research/cycle2_universe_selector.py` runs without error
- `research/cycle2_universe_manifest.json` is produced with ≥20 accounts (target 25)
- `python3 -m pytest tests/test_cycle2_universe_selector.py -q` — all tests pass

## Notes

- Work in the twitter-radar-foundation-v2 workspace
- Do NOT start scraping — this task only creates the selection logic and manifest skeleton
- Do NOT modify X109_DISCOVERY_CANDIDATES.json (read-only input)
- The composite score formula is fixed — do not redesign it
