# CTO000 — FORENSIC AUDIT REPORT

**Produced:** 2026-09-06
**Auditor:** CTO000 Explore Agent (claude-sonnet-4-6)
**Method:** Forensic — every claim tagged PROVEN / OBSERVED / INFERRED / UNKNOWN
**Scope:** Factory (control plane) + Twitter Radar (product)

---

## CRITICAL FINDINGS (Fix immediately)

### F.1 Missing Plan Files → Dispatch Deadlock (CRITICAL)

6/7 queued missions have NO plan file on disk. When XR01 completes, factoryd attempts CTO002, finds no plan, `_default_dispatch()` returns `None`, the dispatch loop `break`s silently. **The daemon stops dispatching forever.** No error log. System appears alive but produces nothing.

Missing plan files:
- `cto002_event_driven_loop_plan.json` — MISSING
- `cto003_auto_reporter_plan.json` — MISSING
- `cto004_context_injection_plan.json` — MISSING
- `cto005_cross_model_review_plan.json` — MISSING
- `x101_historical_corpus_fill_plan.json` — MISSING
- `x102_m008h_full_corpus_plan.json` — MISSING
- `x103_thesis_reconstruction_plan.json` — MISSING

**Root cause code:**
```python
# factoryd.py — _default_dispatch()
plan_path = campaign_root / "plans" / f"{mission_id.lower()}_plan.json"
if not plan_path.exists():
    return None  # ← silent None, no log

# do_poll_iteration()
worker = dispatch(mission_id, cs_path, run_dir)
if worker is None:
    break  # ← loop terminates, zero log explaining why
```

### F.2 cto_planner.py Not Called → H1/H2 Never Auto-Promoted

`cto_planner.py` exists as a standalone CLI. Zero references in factoryd.py, autopilot.py, dispatcher_v3.py. Mission completions never trigger `mark_mission_accepted()`. H1 queue exhaustion is not handled autonomously.

### F.3 No Context Injection → Agents Build Without Strategy

Agents receive ONLY: task file + hardcoded `campaigns/RUN8/FACTORY_ARM_PROTOCOL.md`. They do NOT receive: CTO_STATE.json, ADRs, CTO_DECISIONS.jsonl, CTO_LESSONS.jsonl, known defects, or the canonical roadmap context.

Direct consequence: an agent building X101 without ADR005 will run the full test suite (including test_full_corpus_extractor.py's 29 pre-existing failures) and produce VIOLATED for a passing mission.

### F.4 No Failure Signature / Escalation

When a mission fails → goes to `blocked`. Same error next mission → same fate. No:
- failure signature (hash of error class + stack + failing test)
- retry-with-different-model escalation
- CTO_LESSONS.jsonl write from factoryd
- alert mechanism

### F.5 Heartbeat Not Written During Builds

`heartbeat.write_heartbeat()` is never called by autopilot.py or dispatcher_v3.py. Supervisor cannot distinguish healthy slow build from hung process. Stall detection relies solely on PID-alive (os.kill 0). A truly hung process (alive but stuck) is never detected.

---

## PART A — Factory Component Truth Table

### autopilot.py
- **PATH**: `factory/bin/autopilot.py` (PROVEN)
- **STATUS**: QUALIFIED
- **TESTS**: `tests/test_autopilot.py` — 14 tests (PROVEN)
- **PURPOSE**: Long-horizon mission runner. Loads plan JSON, executes missions sequentially via dispatcher_v3.py subprocess, enforces cumulative budget, writes progress checkpoint for resume, supports rolling backlog mode.
- **TECH_DEBT**:
  1. `--skip-providers` always set → offline/CI mode only; live provider check bypassed
  2. `--quiet` + factoryd `stdout=DEVNULL` → build output permanently lost for forensics
  3. `mission_obs.observe()` failure silently swallowed
- **INTEGRATION_GAPS**: Never writes heartbeat during dispatcher execution

### dispatcher_v3.py
- **PATH**: `factory/bin/dispatcher_v3.py` (PROVEN)
- **STATUS**: QUALIFIED
- **TESTS**: Referenced in test_tb1, test_p0_routing_budget_integration, test_verdict_taxonomy. No dedicated unit tests.
- **PURPOSE**: T0(build) → T1(test) → T2(repair) → T2bis(test) → T3(escalate) pipeline. Routes via model_router. Atomic receipt write.
- **TECH_DEBT**:
  1. BUILDER_PROMPT hardcoded French — not injected from CTO_STATE.json or ADRs
  2. Protocol path hardcoded: `campaigns/RUN8/FACTORY_ARM_PROTOCOL.md`
- **INTEGRATION_GAPS**: No context injection (CTO004 not built)

### factoryd.py (F001)
- **PATH**: `factory/bin/factoryd.py` (PROVEN)
- **STATUS**: QUALIFIED (but E2E autonomous chain not proven — TEST_AUTONOMOUS_CHAIN_001 missing)
- **TESTS**: `tests/test_supervisor.py` — 27 tests (PROVEN)
- **PURPOSE**: Double-fork daemon. Poll loop: check completion → detect zombie → dispatch queued. Owns PID file + flock.
- **TECH_DEBT**:
  1. `_mark_dispatched`/`_mark_settled` read-modify-write on campaign_state.json with no inter-process lock (only daemon's own flock protects against second daemon, not external writers)
  2. `_default_dispatch` silently returns None when plan file missing — no log, no alert
  3. No cto_planner integration — completions never trigger H1/H2 promotion
- **INTEGRATION_GAPS**: cto_planner.py NOT called anywhere in factoryd

### model_router.py
- **PATH**: `factory/bin/model_router.py` (PROVEN)
- **STATUS**: QUALIFIED
- **TESTS**: `tests/test_model_router.py` — 25 tests (PROVEN)
- **PURPOSE**: Static routing: (spec_level, task_kind, stall_count) → EngineChoice. Constructs CLI args for opencode/claude/codex.
- **TECH_DEBT**:
  1. Prompt passed as positional CLI arg — very long prompts will exceed shell arg limits
  2. `--dangerously-skip-permissions` unconditional in Claude invocations
  3. Codex listed as "read-only reviewer" but no validation that codex CLI is present or ran
- **INTEGRATION_GAPS**: Codex presence not verified at startup. No test for codex CLI availability.

### backlog_refiller.py
- **PATH**: `factory/lib/backlog_refiller.py` (PROVEN)
- **STATUS**: QUALIFIED
- **TESTS**: T07-T10 in test_supervisor.py (PROVEN)
- **PURPOSE**: Pure data functions: `load_campaign_state()`, `should_refill()`, `select_next_mission()` (returns string ID only — does NOT call autopilot), `all_blocked_waiting_human()`, `is_zombie_state()`.
- **TECH_DEBT**: Naming "Refiller" misleading — it selects, dispatch is in factoryd._default_dispatch

### rolling_backlog.py vs backlog_refiller.py — DUPLICATE SYSTEMS
- **rolling_backlog.py** (factory/bin): used by autopilot `--campaign-state` mode. REFILL_THRESHOLD=4, REFILL_TARGET=6.
- **backlog_refiller.py** (factory/lib): used by factoryd poll loop. MAX_WORKERS=1.
- **Risk**: Two parallel backlog systems that could diverge as maintained independently.

### completion_detector.py
- **PATH**: `factory/lib/completion_detector.py` (PROVEN)
- **STATUS**: QUALIFIED
- **TESTS**: T04, T05, T06 + 3 heartbeat tests in test_supervisor.py
- **TECH_DEBT**: `is_worker_stalled` = heartbeat stale AND PID dead. Since heartbeat is never written, heartbeat is ALWAYS stale → stall detection is effectively PID-alive only. Alive-but-stuck workers never detected as stalled.

### cto_planner.py
- **PATH**: `factory/bin/cto_planner.py` (PROVEN)
- **STATUS**: PARTIAL — standalone CLI only, NOT integrated
- **TESTS**: UNKNOWN — no test_cto_planner.py found
- **INTEGRATION_GAPS**: CRITICAL — never called from factoryd, autopilot, or dispatcher_v3

### stop_gate.py
- **PATH**: `factory/hooks/stop_gate.py` (PROVEN)
- **STATUS**: QUALIFIED
- **TESTS**: `tests/test_stop_gate.py` — 9 tests (PROVEN)
- **TECH_DEBT**:
  1. Reads `factory/campaigns/CAMPAIGN_STATE` (flat text) but actual state is `factory/campaigns/app007/APP007_CAMPAIGN_STATE.json` — relationship UNKNOWN
  2. BOOKKEEPING_SAFE_PREFIXES is hand-maintained

---

## PART B — Factory Critical Questions

### B1. Does factoryd dispatch when active=[] and queued non-empty?
YES — but immediately blocked by missing plan files (see F.1). The wiring is correct; the content is missing.

### B2. Does backlog_refiller.select_next_mission() call autopilot?
NO. Returns a string mission ID. Dispatch is in factoryd._default_dispatch().

### B3. Is there a wired factoryd → autopilot → dispatcher chain?
YES (PROVEN):
```
factoryd.do_poll_iteration()
→ _default_dispatch(mission_id, ...)
  → Popen([autopilot.py, --plan, --out-dir, --quiet])
    → subprocess.run([dispatcher_v3.py, --repo, --task-file, --mission-id, ...])
      → model_router.route() → cli_args() → Popen([opencode|claude|codex])
```
But factoryd spawns autopilot with `stdout=DEVNULL, stderr=DEVNULL` — build output is irrecoverably lost.

### B4. Does context injection exist?
NO. Zero references to CTO_STATE.json, CTO_DECISIONS.jsonl, or ADRs in any dispatch path.

### B5. What happens when a mission fails 3× with same error?
Mission → `blocked`. Stays blocked. No retry, no escalation, no failure signature, no alert.

### B6. Actual test coverage for factoryd?
27 tests: T01-T12 (adversarial) + HeartbeatTests(2) + WatchFormatTests(1) + EndToEndDispatchTests(2) + extras. TEST_AUTONOMOUS_CHAIN_001 (queue→auto-launch without human) is NOT written.

### B7. Race conditions in supervisor state?
POTENTIAL: _mark_dispatched/_mark_settled do read-modify-write without inter-process lock on campaign_state.json. Safe today (single-threaded daemon), HIGH SEVERITY if parallel workers added.

---

## PART C — Twitter Radar Component Truth Table

### t2_rule_classifier.py
- **ACTUAL PATH**: `scripts/t2_rule_classifier.py` (NOT `src/joss_orchestrator/` — that path doesn't exist)
- **STATUS**: QUALIFIED
- **KEY_TESTS**: test_human_review_v7.py (10), test_corpus_e2e.py (10)
- **INTEGRATION_GAPS**: `is_call()` invariant lives in `scripts/radar.py` as `_position_event_call_invariant()`, not in t2_rule_classifier itself

### radar.py
- **PATH**: `scripts/radar.py` (PROVEN, 1500+ lines)
- **STATUS**: QUALIFIED
- **KEY_TESTS**: test_e2e_radar.py (18), test_position_event.py (14)
- **INTEGRATION_GAPS**: R5 ledger NOT wired into radar.py output

### r5_scoring.py
- **ACTUAL PATH**: `scripts/r5_scoring.py` (NOT `scoring/r5_scorer.py`)
- **STATUS**: PARTIAL
- **INTEGRATION_GAPS**: CRITICAL — ZERO references to M008H, POSITION_EVENT, is_call, actor, modality, polarity. Scores raw historical performance, not semantic-gated calls.

### r5_ledger.py
- **PATH**: `scripts/r5_ledger.py` (PROVEN)
- **STATUS**: PARTIAL
- **INTEGRATION_GAPS**: No M008H semantic fields in schema. Production signals from M008H-gated calls likely not in ledger (uses fixture_signals() in weekly pipeline).

### CANONICAL_ACCOUNT_UNIVERSE.csv
- **PATH**: `recovery/CANONICAL_ACCOUNT_UNIVERSE.csv` (PROVEN)
- **STATUS**: QUALIFIED (X000C)
- **CONTENT**: 6432 accounts. Top 5: ckcapitalxx(81), frenchie(81), photonbull(81), rjccapital(81), bryzonx(76).

---

## PART D — Twitter Critical Questions

### D1. is_call() invariant correctly implemented?
YES, with one refinement beyond ADR001:
```python
def _position_event_call_invariant(event: PositionEvent) -> bool:
    return (
        event.actor == "AUTHOR"
        and event.modality in ("COMMITTED", "PAST")
        and event.polarity == "AFFIRMED"
        and event.source in ("DIRECT", "QUOTE_TWEET")
        and event.action not in ("HOLD", "UNKNOWN")  # ← extra guard (correct, not contradicting ADR001)
    )
```

### D2. Are 29 test_full_corpus_extractor.py failures pre-existing?
YES — pre-existing at M008H baseline. Confirmed pre-existing by CTO_STATE.json, ADR005, and X000C report.

### D3. Is r5_scorer wired to M008H?
NO. Zero semantic references in r5_scoring.py. Scores price-based historical performance only.

### D4. Does ledger have real M008H entry/exit?
UNKNOWN / LIKELY PLACEHOLDER. Weekly pipeline uses `fixture_signals()` when no real signals provided. Ledger schema lacks M008H-specific fields.

---

## PART E — Gaps Summary

| Gap | Severity |
|-----|----------|
| 6/7 queued missions have no plan files → dispatch deadlock after XR01 | CRITICAL |
| cto_planner not called from factoryd → H1/H2 never auto-promoted | HIGH |
| No context injection → agents build without ADRs/strategy | HIGH |
| r5_scoring not wired to M008H signals | HIGH |
| test_full_corpus_extractor.py 29 failures block X-track auto-verdict | HIGH |
| No failure signature / escalation | HIGH |
| Heartbeat never written → no fine-grained proof-of-life | MEDIUM |
| CONTINUABLE_BLOCKED verdicts not acted on | MEDIUM |
| Two parallel backlog systems (rolling_backlog + backlog_refiller) | MEDIUM |
| Race condition in _mark_dispatched/_mark_settled | LOW today, HIGH with parallelism |

---

## PART F — What Prevents 2h Autonomy

1. **Missing plan files** → factoryd deadlocks after XR01 (immediate, deterministic)
2. **No cto_planner integration** → H1 queue exhausts, system can't self-refill
3. **No context injection** → agents miss ADR005, produce VIOLATED for passing missions
4. **No failure signature** → systematic infrastructure failures accumulate silently in `blocked`
5. **Heartbeat absent** → hung workers not detectable

**Fix order for 2h autonomy:**
1. Create plan files for CTO002-CTO005 (immediate, unblocks dispatch chain)
2. Wire CTO002 (event-driven loop) — makes this structural
3. Wire CTO004 (context injection) — agents receive ADRs
4. Add TEST_AUTONOMOUS_CHAIN_001

---

## Architecture Map (Actual, Not Assumed)

### Factory (actual paths)
```
factory/bin/
  autopilot.py          ← mission runner (long-horizon)
  dispatcher_v3.py      ← single-mission executor (T0→T3)
  factoryd.py           ← persistent supervisor daemon (F001)
  model_router.py       ← static routing Claude/GLM/Codex
  rolling_backlog.py    ← backlog manager (autopilot path)
  cto_planner.py        ← H1/H2 horizon manager (NOT WIRED)
  verdict_taxonomy.py   ← verdict classification
  factoryd_watch.py     ← read-only status viewer
factory/lib/
  backlog_refiller.py   ← campaign state reader (factoryd path)
  completion_detector.py← PID/heartbeat checker
  supervisor_state.py   ← atomic state persistence
  heartbeat.py          ← heartbeat r/w (NEVER CALLED in production path)
  mission_state.py
  mission_obs.py
  snapshot_diff.py
  budget_ledger.py
factory/hooks/
  stop_gate.py          ← commit gate (BOOKKEEPING_SAFE_PREFIXES)
factory/cto/            ← NEW (2026-09-06) — metadata only, not wired to code yet
```

### Twitter Radar (actual paths, corrections from CTO_ARCHITECTURE.json)
```
scripts/
  t2_rule_classifier.py ← CALL classifier (NOT in src/joss_orchestrator/)
  radar.py              ← End-to-end pipeline + PositionEvent + is_call() invariant
  r5_scoring.py         ← R5 scorer (NOT in scoring/r5_scorer.py)
  r5_ledger.py          ← Signal ledger
  r5_shadow_run.py      ← Weekly shadow runner
  weekly_product_run.py ← Weekly pipeline orchestrator
recovery/
  CANONICAL_ACCOUNT_UNIVERSE.csv ← 6432 accounts (X000C)
data/corpus/            ← 10802 posts (M008A)
```

---

*Produced by forensic read of actual repository files. No hallucinated paths or capabilities.*
