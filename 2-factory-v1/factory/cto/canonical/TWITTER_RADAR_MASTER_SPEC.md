<!-- AUTORITÉ DÉPLACÉE 2026-09-07T15:21:00Z — l'autorité canonique est désormais projects/twitter-radar/SPEC.md. Ce fichier est GELÉ (contenu inchangé pour compatibilité champion). Violation d'intégrité = check C9 de authority_audit.py. -->

---
schema: twitter-radar-master-spec/v1
produced_at: "2026-09-06T12:00:00Z"
authority: "Compiled from CTO_STATE.json + CTO_ARCHITECTURE.json + APP007_CAMPAIGN_STATE.json + canonical_roadmap_v2.json + CTO_RESEARCH_REGISTRY.json + CTO000_AUDIT_REPORT.md"
version: "v1"
---

# TWITTER RADAR MASTER SPEC — v1

## North Star

> Continuously discover, evaluate and rank financial X accounts based on historical quality,
> profitability, originality and precision of their calls.

**Identify** independent financial X accounts and clusters whose calls exhibit genuine historical
edge above market, sector, momentum and randomness. **Detect** new signals before consensus
is evident.

**Done when** the system automatically produces:
```
ACCOUNT #1 — @X
187 valid historical calls / 61.2% hit rate / median 90D +14.8%
market-adjusted alpha +6.1% / momentum-adjusted alpha +4.4%
random-entry percentile 94 / PSR 0.93 / DSR positive / OOS PASS
originator_score 0.89 / independence_score 0.91 / lead_days_avg 7.3
top theses: ASTS +147%, XYZ +82%
current active thesis: DEF LONG (3 independent top accounts converging)
```

---

## Account Profile V2 (full scoring pipeline)

```
RAW_PERFORMANCE
  → MARKET_ADJUSTED_ALPHA
  → SECTOR_ADJUSTED_ALPHA
  → MOMENTUM_ADJUSTED_EDGE
  → RANDOM_ENTRY_PERCENTILE
  → RISK / DRAWDOWN (MFE, MAE, MAX_DRAWDOWN)
  → SAMPLE_CONFIDENCE (n calls, PSR, DSR, PBO)
  → OOS_VALIDATION (walk-forward purgedcv)
  → ACCOUNT_EDGE (final composite)
```

Dimensions: raw / risk_adjusted / statistical / quality / conviction / originality / temporal.

---

## Capability Registry

| ID | Capability | Status | Component | Mission |
|----|-----------|--------|-----------|---------|
| XREQ-001 | Account universe (6433 canonical accounts) | IMPLEMENTED | recovery/CANONICAL_ACCOUNT_UNIVERSE.csv | X000C |
| XREQ-002 | Historical scraper (twscrape v0.20.1) | PARTIAL | scripts/ — 10802 posts from 3 accounts; full corpus not yet scraped | X101 |
| XREQ-003 | Semantic CALL extraction (PositionEvent) | IMPLEMENTED | scripts/t2_rule_classifier.py + radar.py | M008H |
| XREQ-004 | Call direction vs author position invariant | IMPLEMENTED | is_call(): actor=AUTHOR AND modality∈{COMMITTED,PAST} AND polarity=AFFIRMED AND source∈{DIRECT,QUOTE_TWEET} AND action∉{HOLD,UNKNOWN} | M008G+M008H |
| XREQ-005 | CLI display / ranked output | IMPLEMENTED | scripts/radar.py --corpus-dir | M010 |
| XREQ-006 | Data reconciliation / quality gate | IMPLEMENTED | M009 — 16/16 tests | M009 |
| XREQ-007 | Social graph (NetworkX, 1502 nodes) | IMPLEMENTED | research/xr02_social_graph.py — PageRank/HITS/Louvain, 20 communities | XR02 |
| XREQ-008 | Abnormal returns / CAR (statsmodels OLS) | IMPLEMENTED | research/xr03_abnormal_returns.py — AbnormalReturnsEngine, CAR/AAR/t-stat | XR03 |
| XREQ-009 | Momentum/sector/regime adjustment (random entry controls) | PLANNED | — | XR04 |
| XREQ-010 | OOS validation (purgedcv walk-forward, embargo, CPCV) | PLANNED | — | XR05 |
| XREQ-011 | Statistical confidence PSR/DSR/PBO | PLANNED | — | XR06 |
| XREQ-012 | Historical corpus expansion (≥10 accounts, ≥6m) | PLANNED | twscrape gap-fill | X101 |
| XREQ-013 | Full corpus M008H CALL extraction | PLANNED | — | X102 |
| XREQ-014 | Thesis reconstruction (OPEN→ADD→REDUCE→CLOSE state machine) | PLANNED | — | X103 |
| XREQ-015 | Outcome ledger (real entry/exit/P&L from M008H signals) | PLANNED | scripts/r5_ledger.py (needs wiring) | X104 |
| XREQ-016 | Account skill score V2 (ACCOUNT_PROFILE_V2) | PLANNED | — | X106 |
| XREQ-017 | Cross-account consensus (independence-weighted) | PLANNED | — | X107 |
| XREQ-018 | Lead/lag / originality scoring | PLANNED | — | X108 |
| XREQ-019 | Recursive account discovery | PLANNED | — | X109 |
| XREQ-020 | Weekly intelligence refresh (automated) | PLANNED | — | X110 |
| XREQ-021 | Account ranking (end-to-end, all dimensions) | BLOCKED | requires XREQ-001–018 complete | — |

---

## Qualified Components

| Component | Path | Tests | Status |
|-----------|------|-------|--------|
| twitter.position_event | scripts/t2_rule_classifier.py | test_position_event.py (14), test_human_review_v7.py (10) | ACCEPTED_M008H — FROZEN |
| twitter.radar | scripts/radar.py | test_e2e_radar.py (18), test_position_event.py (14) | ACCEPTED_M007B |
| twitter.corpus | data/corpus/ | CORPUS_MANIFEST.json sha256 | ACCEPTED_M008A — READ-ONLY |
| twitter.canonical_universe | recovery/CANONICAL_ACCOUNT_UNIVERSE.csv | — | ACCEPTED_X000C — 6432 accounts |
| twitter.social_graph | research/xr02_social_graph.py | XR02 ACCEPTED 8/8 | ACCEPTED_XR02 |
| twitter.abnormal_returns | research/xr03_abnormal_returns.py | XR03_RECEIPT.json ACCEPTED | ACCEPTED_XR03 |
| twitter.r5_scorer | scripts/r5_scoring.py | — | PARTIAL — not wired to M008H |
| twitter.ledger | scripts/r5_ledger.py | — | PARTIAL — no M008H signals |

---

## CALL Invariant (frozen — ADR001)

```python
is_call(event: PositionEvent) -> bool:
    return (
        event.actor == "AUTHOR"
        and event.modality in ("COMMITTED", "PAST")
        and event.polarity == "AFFIRMED"
        and event.source in ("DIRECT", "QUOTE_TWEET")
        and event.action not in ("HOLD", "UNKNOWN")  # extra guard — CTO000 confirmed
    )
```

No mission may modify this invariant without an explicit CTO ADR superseding ADR001.

---

## Open Source Decisions

| Library | License | Decision | Mission |
|---------|---------|----------|---------|
| NetworkX ≥3.3 | BSD-3-Clause | ADOPT | XR02 (ACCEPTED) |
| purgedcv 0.1.6 | MIT | ADOPT (API verified) | XR05 |
| oos-lab | MIT | EVALUATE/COMPARE | XR05 |
| eventstudy | GPL-3.0 | REJECT (ADR007) — use statsmodels | — |
| statsmodels | BSD-3-Clause | ADOPT (CAR implementation) | XR03 |
| qlib ≥0.9 | MIT | ADAPT_SELECTIVE | pending |
| FinBERT | MIT | CHALLENGER_ONLY — do NOT replace M008H | — |
| FinGPT | MIT | CHALLENGER_ONLY — do NOT replace M008H | — |

**Critical API pitfalls** (CTO_LESSONS L015/L016):
- `purgedcv.probabilistic_sharpe_ratio(returns_array, benchmark)` — takes raw array, NOT scalars. Raw kurtosis (normal=3.0), not excess.
- `deflated_sharpe_ratio(..., bars_per_year=252)` — must pass bars_per_year or DSR≈0 always.
- Verified test vector: SR=0.1, n=100, skew=0, raw_kurt=3 → PSR≈0.8395.

---

## Human Gates

| Gate | Description | Status | Blocks |
|------|-------------|--------|--------|
| M008_VALIDATION | Human verdict on HUMAN_REVIEW_PACKAGE_V7 (477 calls quality audit) | WAITING_HUMAN | X103 thesis reconstruction production use |
| XR01_EVENTSTUDY_GPL | eventstudy GPL-3.0 decision | RESOLVED_BY_POLICY (ADR007) | nothing |

---

## X-Track Roadmap

| Mission | Title | Status | Depends On |
|---------|-------|--------|-----------|
| X000C | Forensic Consolidation | ACCEPTED | — |
| XR01 | Open-Source Intake | ACCEPTED | X000C |
| XR02 | Social Graph Engine | ACCEPTED (8/8, 430s) | XR01 |
| XR03 | Abnormal Returns / CAR | ACCEPTED (1002s) | XR01 |
| XR04 | Momentum/Random Controls | QUEUED | XR03 |
| XR05 | OOS / Walk-Forward Validation | QUEUED | XR04 |
| XR06 | Statistical Confidence (PSR/DSR/PBO) | QUEUED | XR05 |
| X101 | Historical Corpus Fill | QUEUED | X000C |
| X102 | M008H Full Corpus | QUEUED | X101 |
| X103 | Thesis Reconstruction | QUEUED | X102 (+ M008_VALIDATION for production use) |
| X104 | Outcome Ledger | STUB | X103 |
| X105 | P&L + MFE/MAE | STUB | X104 |
| X106 | Account Skill Score V2 | STUB | X105 + XR06 |
| X107 | Consensus with Independence Counting | STUB | X106 + XR02 |
| X108 | Lead/Lag / Originality | STUB | X107 |
| X109 | Recursive Discovery | STUB | X108 |
| X110 | Weekly Intelligence Report V2 | STUB | X109 |

**XR DAG**: XR01 unlocks XR02+XR03+XR04+XR05 (parallel, but writer constraint: sequential). XR06 depends on XR05. XR02 does NOT block XR03-XR05.

---

## Known Defects

| ID | Severity | Description | Fix |
|----|----------|-------------|-----|
| TW-D001 | HIGH | r5_scoring.py not wired to M008H signals (raw counts only) | X104 |
| TW-D002 | HIGH | r5_ledger.py no real M008H entry/exit/P&L — fixture_signals() placeholder | X104 |
| TW-D003 | HIGH | test_full_corpus_extractor.py: 29 pre-existing failures block X-track auto-verdict | dedicated repair |
| TW-D004 | MEDIUM | Thesis reconstruction absent — each tweet treated independently | X103 |
| TW-D005 | MEDIUM | Social graph not yet wired to scoring / consensus pipeline | X107 |
| TW-D006 | MEDIUM | Edge not yet statistically validated (XR03-XR06 required) | XR04-XR06 |
| TW-D007 | LOW | XR02 social graph coverage 23.3% of 6433 accounts (1502 nodes) — needs X101 corpus expansion | X101+XR02 re-run |

---

## Consensus Formula V2

```
CONSENSUS_STRENGTH = independence × account_alpha × conviction × temporal_proximity × originality

Where:
  independence = count unique originator sources (A + RT_of_A + quote_of_A = 1 source, not 3)
  account_alpha = market/sector/momentum adjusted edge
  conviction = modality COMMITTED vs WATCHLIST weighting
  temporal_proximity = decay function on time since call
  originality = originality_score × lead_score
```

*LunarCrush insight: 50 tweets from same influencer ≠ 50 independent people.*

---

*This document is compiled from repository state and is updated only on explicit CTO decision.*
