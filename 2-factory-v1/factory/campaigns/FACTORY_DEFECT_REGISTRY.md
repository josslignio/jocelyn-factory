# FACTORY DEFECT REGISTRY

Status: ACTIVE
Last updated: 2026-09-03
Managed by: Factory Engineering Program

Registry of all Factory runtime defects observed during real product missions.
No defect is closed merely because a workaround was found.
Status: OPEN | DIAGNOSED | FIXING | FIXED_UNQUALIFIED | QUALIFIED | DEFERRED_WITH_REASON

---

## FD1 — FACTORY_OWNED_CANDIDATE_CAPTURE

DEFECT_ID: FD1
TITLE: Candidate promotion must not depend on worker performing git commit
SEVERITY: P1
STATUS: OPEN
FIRST_SEEN_MISSION: APP007_MISSION_002
AFFECTED_RUNTIME_PATH: dispatcher_v3.py → candidate promotion → stop_gate check

ROOT_CAUSE:
Current flow: worker is expected to git-commit its own artifacts. If worker omits the commit
(GLM in M002 wrote files but did not commit), candidate_sha == base_sha and the promotion
fails silently or requires manual intervention.

USER/PRODUCT_IMPACT:
M002 required manual supervisor commit of GLM artifacts (radar_report.py + 18 tests).
Any mission where the worker fails to commit produces a phantom "no artifact" result.
This is a reliability gap in every mission.

REPRODUCTION:
Run a GLM mission where the worker writes files but does not explicitly git-commit.
Factory observes candidate_sha == base_sha and either promotes nothing or BLOCKS.

CURRENT_WORKAROUND:
Supervisor manually detects missing commit, verifies tests, creates canonical commit.

TARGET_FIX:
Factory captures tree snapshot BEFORE and AFTER worker invocation.
Computes diff. If diff is non-empty:
  1. runs public tests in candidate worktree
  2. terminates candidate worker
  3. runs private verifier
  4. if verifier passes: Factory creates canonical commit (not the worker)
Worker writes; Factory commits.

ACCEPTANCE_TESTS:
- Simulate worker that writes files but never calls git commit
- Factory still creates candidate_sha ≠ base_sha
- Promotion proceeds normally

REGRESSION_TESTS:
- Normal worker that does commit: outcome unchanged

QUALIFICATION_REQUIRED: yes — must replay a real APP007 mission with this fix active

BEFORE_METRICS:
- M002: 1 human intervention required for missing commit
- M003: GLM repair_glm correctly committed (no issue)

AFTER_METRICS: (to be recorded when QUALIFIED)

EVIDENCE: APP007_MISSION_002 evidence, autopilot_receipt.json

CLOSED_AT_COMMIT: —

---

## FD2 — MISSION_OBSERVABILITY_CONTRACT

DEFECT_ID: FD2
TITLE: mission_obs.observe crashes with 'list' has no attribute 'items'
SEVERITY: P1
STATUS: OPEN
FIRST_SEEN_MISSION: APP007_MISSION_002 or earlier
AFFECTED_RUNTIME_PATH: autopilot.py → mission_obs.observe()

ROOT_CAUSE:
mission_obs.observe() receives a dict-like structure in some call paths but a list in others.
Python crash: AttributeError: 'list' object has no attribute 'items'.

USER/PRODUCT_IMPACT:
Factory observability is broken. Cannot reliably collect: mission duration, attempts, routing,
budget, test failures, repair count, model used, final verdict. Cannot track Factory improvement
metrics longitudinally.

REPRODUCTION:
Run autopilot.py with a mission that triggers the observe() path passing a list argument.

CURRENT_WORKAROUND:
Observability data collected manually from receipt/logs. Longitudinal metrics not maintained.

TARGET_FIX:
Define explicit MissionObservation dataclass (or TypedDict):
  MissionObservation(
    mission_id: str,
    mission_class: str,
    model: str,
    wall_time_s: float,
    attempts: int,
    repairs: int,
    first_attempt_success: bool,
    routing_decision: str,
    fallbacks: list[str],
    test_result: str,
    verifier_result: str,
    human_interventions: int,
    recovery_events: list[str],
    final_verdict: str,
    budget_used_s: float,
    budget_limit_s: float,
  )
mission_obs.observe() accepts ONLY MissionObservation. Validate at call site.
Add typed construction wherever observe() is called.

ACCEPTANCE_TESTS:
- observe() with correct MissionObservation: no crash, data stored
- observe() with a bare list: raises TypeError (explicit, not AttributeError)
- Full mission run: observation data recoverable from observation store

REGRESSION_TESTS:
- All existing autopilot tests still pass

QUALIFICATION_REQUIRED: yes — run 1 full APP007 mission and verify observation data present

BEFORE_METRICS:
- observe() crash rate: 100% (every mission with multi-step routing)

AFTER_METRICS: (to be recorded when QUALIFIED)

EVIDENCE: —

CLOSED_AT_COMMIT: —

---

## FD3 — MISSION_VERDICT_TAXONOMY

DEFECT_ID: FD3
TITLE: Dispatcher verdict taxonomy too coarse — overloads BLOCKED
SEVERITY: P1 (routing decisions depend on verdict; wrong verdict = wrong recovery)
STATUS: OPEN
FIRST_SEEN_MISSION: APP007_MISSION_004 (BLOCKED used for spec/test conflict)
AFFECTED_RUNTIME_PATH: dispatcher_v3.py → verdict → recovery policy

ROOT_CAUSE:
BLOCKED is used for multiple fundamentally different situations:
- spec/test conflict (FD4)
- dependency missing (twscrape absent)
- auth missing
- budget exceeded
- infra failure
Each requires a different recovery action. Using BLOCKED for all means no deterministic recovery.

USER/PRODUCT_IMPACT:
Factory cannot autonomously decide how to recover from a BLOCKED verdict.
Human must inspect the reason. This breaks autonomous long-run operation (FD5).

REPRODUCTION:
Run a mission that fails due to: (a) spec conflict, (b) missing package, (c) budget overrun.
All three return BLOCKED. Recovery policy is identical and wrong for at least two of them.

CURRENT_WORKAROUND:
Supervisor reads logs to determine true BLOCKED reason. Manual routing decision.

TARGET_FIX:
```python
class MissionVerdict(Enum):
    SATISFIED = "SATISFIED"
    VIOLATED = "VIOLATED"       # implementation wrong, retry/repair warranted
    BLOCKED = "BLOCKED"         # catch-all only when cause unknown
    INFRA_FAIL = "INFRA_FAIL"   # process/env failure, not code
    MOTOR_UNAVAILABLE = "MOTOR_UNAVAILABLE"  # model quota/timeout
    DEPENDENCY_MISSING = "DEPENDENCY_MISSING"  # required package not installed
    AUTH_REQUIRED = "AUTH_REQUIRED"    # needs credentials (can't proceed offline)
    AUTH_INVALID = "AUTH_INVALID"      # credentials present but rejected
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"  # wall-time or token budget exceeded
    SPEC_TEST_CONFLICT = "SPEC_TEST_CONFLICT"  # see FD4
```
Recovery policy per verdict:
- VIOLATED → repair_or_new_attempt
- DEPENDENCY_MISSING → resolve_dependency
- AUTH_REQUIRED → wait_for_human (then park mission)
- MOTOR_UNAVAILABLE → try_allowed_fallback
- BUDGET_EXCEEDED → stop_before_next_model_call
- INFRA_FAIL → recover_infrastructure
- SPEC_TEST_CONFLICT → escalate_to_supervisor for contract correction

ACCEPTANCE_TESTS:
- Each verdict type emits distinct string in receipt
- Recovery policy lookup is deterministic per verdict
- No BLOCKED used when more specific verdict applies

REGRESSION_TESTS:
- SATISFIED/VIOLATED still work as before

QUALIFICATION_REQUIRED: yes

BEFORE_METRICS:
- M004: BLOCKED returned; recovery required human inspection

AFTER_METRICS: (to be recorded when QUALIFIED)

EVIDENCE: APP007_MISSION_004 receipt (829s, BLOCKED)

CLOSED_AT_COMMIT: —

---

## FD4 — SPEC_TEST_CONFLICT_DETECTION

DEFECT_ID: FD4
TITLE: Factory cannot detect new-spec ↔ legacy-test conflict; retries blindly
SEVERITY: P1
STATUS: DIAGNOSED
FIRST_SEEN_MISSION: APP007_MISSION_004
AFFECTED_RUNTIME_PATH: dispatcher_v3.py → build_glm → repair_glm → build_claude (all fail)

ROOT_CAUSE:
Three workers (build_glm, repair_glm, build_claude) attempted M004 sequentially.
Each failed with the same ~6 tests from the same legacy test group (tests using call-date prices).
Factory treated each failure as VIOLATED and escalated to next worker.
Factory did not recognize: identical failure signature across independent workers = contract
conflict, not implementation failure.

USER/PRODUCT_IMPACT:
829 seconds consumed on 3 logically identical attempts before BLOCKED was declared.
M004 required supervisor intervention to rewrite the task contract.
This will recur on any future mission where old tests encode old behavior.

REPRODUCTION:
Write a task spec that requires behavioral change B (e.g. use _price_before for returns),
while legacy tests assert old behavior A (e.g. use _price_on_or_after).
Run dispatcher. All workers will fail identical tests. BLOCKED after 3 attempts.

CURRENT_WORKAROUND:
Supervisor identifies conflict, rewrites task contract, launches new attempt.
This is the documented SPEC_TEST_CONFLICT recovery path. Mandatory per FD4 fix.

TARGET_FIX:
After N failures (N ≥ 2) where:
  - same test group fails across all attempts
  - no attempt passes the failing group even partially
  → detect signature: SPEC_TEST_CONFLICT (not VIOLATED)
  → emit diagnostic: which tests fail, which spec requirements conflict with them
  → verdict: SPEC_TEST_CONFLICT (see FD3)
  → do NOT launch another identical worker
  → surface to supervisor with structured conflict report

ACCEPTANCE_TESTS:
- Run 2 build attempts on artificially conflicted spec/test pair
- After 2nd failure: verdict = SPEC_TEST_CONFLICT
- Conflict report identifies: failing test names, spec requirement they conflict with
- 3rd worker is NOT launched

REGRESSION_TESTS:
- Genuine implementation failures (different tests fail each attempt) still → VIOLATED + repair

QUALIFICATION_REQUIRED: yes

BEFORE_METRICS:
- M004: 3 workers × ~276s avg = 829s wasted, same tests failed each time

AFTER_METRICS: (to be recorded when QUALIFIED)

EVIDENCE: APP007_MISSION_004 receipt — BLOCKED (829s, all workers failed)
          Test failures: test_enriched_long_call_win/loss, test_enriched_short_call_win,
          test_unknown_direction, test_non_numeric_close_row_skipped, test_partial_horizon

CLOSED_AT_COMMIT: —

---

## FD5 — LONG_HORIZON_ROLLING_BACKLOG

DEFECT_ID: FD5
TITLE: Campaign terminates prematurely when active backlog is exhausted
SEVERITY: P1
STATUS: OPEN
FIRST_SEEN_MISSION: historical — repeated pattern across runs
AFFECTED_RUNTIME_PATH: autopilot.py → campaign loop → backlog management

ROOT_CAUSE:
Historically Factory has processed a finite batch of missions (~3-5) then declared the
campaign complete (CAMPAIGN_STATE=DONE) even though further productive work exists.
The rolling-backlog instruction is in the prompt but not enforced mechanically.

USER/PRODUCT_IMPACT:
Factory cannot demonstrate LONG_HORIZON_AUTONOMY. Multi-hour unattended runs are not proven.
Each session requires human reinitialization. This is a structural reliability gap.

REPRODUCTION:
Create a campaign with 3 missions. Let Factory run. After 3 missions, observe premature DONE.

CURRENT_WORKAROUND:
Supervisor monitors campaign state, manually re-injects missions when backlog drops.
Current operational rule: when ACTIVE_BACKLOG ≤ 2, generate next 3-5 missions.

TARGET_FIX:
Mechanical backlog refill check at end of each mission:
  if len(active_missions) <= 2:
    generate_next_missions(count=3)
    add_to_backlog(generated)
  CAMPAIGN_STATE = DONE only when:
    - genuine P0/security/provenance blocker
    - irrecoverable infrastructure failure
    - external auth required AND no offline work remains
    - quota exhausted
    - genuine product decision requiring human judgment
    - literally no useful work remains AND generation produced nothing

ACCEPTANCE_TESTS:
- Campaign with 3 initial missions: after mission 2 completes, backlog grows to 5+
- Campaign never sets DONE until stop conditions above are met
- Prove via e1-probe: factory runs for 60+ minutes across 5+ missions unattended

REGRESSION_TESTS:
- Genuine stop conditions still trigger DONE correctly

QUALIFICATION_REQUIRED: yes — proof via real APP007 mission sequence (M001–M007+)

BEFORE_METRICS:
- Historical run duration before premature DONE: ~15-25 minutes
- Historical missions per unattended run: 3-5

AFTER_METRICS: (to be recorded when QUALIFIED)

EVIDENCE: Factory v1 campaign history, e1-probe history

CLOSED_AT_COMMIT: —

---

## FD6 — CROSS_REPO_PREFLIGHT_HARDENING

DEFECT_ID: FD6
TITLE: Relative task_file path silently resolves wrong in cross-repo missions
SEVERITY: P1
STATUS: OPEN
FIRST_SEEN_MISSION: APP007_MISSION_006 (first attempt — PREFLIGHT_FAIL rc=3 in 0.2s)
AFFECTED_RUNTIME_PATH: dispatcher_v3.py → preflight → check_task → task_file resolution

ROOT_CAUSE:
When task_file is a relative path (e.g. "factory/campaigns/.../task.md"), dispatcher_v3.py
resolves it relative to --repo. In cross-repo missions (repo=twitter workspace), the file
exists in the factory workspace, not the twitter workspace → MISSING → PREFLIGHT_FAIL.
check_task.py has no cross-repo resolution, no warning for relative paths.

USER/PRODUCT_IMPACT:
M006 first launch: PREFLIGHT_FAIL (0.2s, 0 model calls). Required manual plan fix.
M007: PREFLIGHT_FAIL due to missing OUTCOME marker (separate but related issue).
Pattern will recur on any future cross-repo mission with a relative task_file path.

REPRODUCTION:
Create a plan with task_file="factory/campaigns/app007/tasks/some_task.md" pointing to a
factory-workspace file. Set repo to twitter workspace. Run dispatcher. PREFLIGHT_FAIL.

CURRENT_WORKAROUND:
Supervisor manually uses absolute paths in all plan files. Lesson documented as invariant.

TARGET_FIX:
- check_task.resolve_task_file(repo, raw_path) → tries absolute, then repo-relative, then factory-root-relative
- _check_task_file() returns RELATIVE_PATH verdict if path is relative (actionable error)
- dispatcher_v3.py uses resolve_task_file() in both preflight and main dispatch path
- ≥6 new tests in tests/test_check_task.py

QUALIFICATION_REQUIRED: yes

BEFORE_METRICS:
- M006 attempt 1: PREFLIGHT_FAIL (relative path), 0 tokens wasted, plan fix required

AFTER_METRICS: (to be recorded when QUALIFIED)

CLOSED_AT_COMMIT: —

---

## FD7 — HUMAN_GATE_DOES_NOT_STALL_CAMPAIGN

DEFECT_ID: FD7
TITLE: WAITING_HUMAN on one branch stalls entire Factory campaign
SEVERITY: P1
STATUS: OPEN
FIRST_SEEN_MISSION: APP007_MISSION_008 (WAITING_HUMAN_VALIDATION)
AFFECTED_RUNTIME_PATH: rolling_backlog.py → generate_next → canonical_roadmap.json

ROOT_CAUSE:
When the product branch reaches WAITING_HUMAN_VALIDATION (M008), the canonical_roadmap.json
had no further entries, so rolling_backlog.generate_next() returned [] and the campaign
effectively paused. A human validation gate on one branch blocked ALL independent Factory
hardening work, even though it should not.

SEMANTICS VIOLATED:
  M008 = WAITING_HUMAN_VALIDATION
  ≠ GLOBAL_CAMPAIGN = IDLE
  ≠ CAMPAIGN_STATE = DONE

Correct semantics:
  M008 waiting → PRODUCT_ACCEPTANCE_BRANCH = WAITING_HUMAN
               → Factory hardening backlog continues unblocked
               → CAMPAIGN_STATE stays RUNNING

USER/PRODUCT_IMPACT:
After M007 ACCEPTED (2026-09-04), Factory stopped completely despite:
- FD2 (observability) still open
- FD6 (cross-repo hardening) not formalized
- FD4 (spec-test conflict) not runtime-automated
- Multi-model routing not proven beyond GLM+Claude routing
- Long-horizon 72h autonomy proof not done

CURRENT_WORKAROUND (partial):
Extended canonical_roadmap.json to include FACTORY_HARDENING_FD2 and FACTORY_HARDENING_FD6
with depends_on: [] so rolling backlog can schedule them while M008 waits.
This is a structural fix for the immediate problem but not the full scheduler fix.

TARGET_FIX (full):
Dual-backlog scheduler:
  while campaign_state == RUNNING:
    mission = next_ready_product_mission()
    if mission is None:
        mission = next_ready_factory_hardening_mission()
    if mission is None:
        mission = next_safe_independent_readiness_mission()
    if mission is None:
        if true_global_hard_stop():
            stop()
        else:
            persist_waiting_state()
WAITING_HUMAN on one branch = not a global hard stop.

ACCEPTANCE_TESTS:
- M008 = WAITING_HUMAN_VALIDATION; FD2 is open with plan_file existing → FD2 gets scheduled
- Campaign state stays RUNNING while M008 waits
- After M008 human PASS, product branch advances without disrupting Factory backlog
- No duplicate missions scheduled

QUALIFICATION_REQUIRED: yes — prove via real run where M008 waits and FD2/FD6 complete

CLOSED_AT_COMMIT: —

---

## LONGITUDINAL METRICS

Per-mission tracking (to be maintained):

| MISSION_ID | CLASS | MODEL | WALL_S | ATTEMPTS | REPAIRS | 1ST_ATTEMPT_OK | VERDICT | HUMAN_INT |
|------------|-------|-------|--------|----------|---------|----------------|---------|-----------|
| APP007_M001 | feature | GLM | 229 | 1 | 0 | YES | ACCEPTED | 1 (commit) |
| APP007_M002 | feature | GLM | 229 | 1 | 0 | YES | ACCEPTED | 1 (commit) |
| APP007_M003 | feature | GLM | 397 | 2 | 1 | NO | ACCEPTED | 0 |
| APP007_M004 | correctness | GLM | 829+ | 3 | 0 | NO | BLOCKED | 1 (contract) |

Aggregates (4 missions):
- VERIFIED_DELIVERY_RATE: 3/4 = 75% (M004 pending re-run)
- FIRST_ATTEMPT_SUCCESS_RATE: 2/4 = 50%
- AVG_ATTEMPTS_PER_MISSION: 1.75 (excl M004 blocked)
- HUMAN_INTERVENTION_RATE: 2/4 = 50%
- SPEC_TEST_CONFLICT_EVENTS: 1 (M004)
