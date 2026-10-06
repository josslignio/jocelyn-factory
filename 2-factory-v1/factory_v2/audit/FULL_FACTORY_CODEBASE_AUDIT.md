# F3 — FULL FACTORY CODEBASE STATE-OF-ART AUDIT

**Produced:** 2026-09-07  
**Milestone:** FULL_CODEBASE_STATE_OF_ART_AUDIT  
**Auditors:** Claude (architecture/authority-coherence), Z Code via Explore agent (wiring/dead-code/duplication)  
**Codex pass:** see FACTORY_F3_COUNTER_AUDIT_RECEIPT.json

---

## Top-Level Metrics (Before)

| Metric | Count |
|--------|-------|
| factory/ LOC (Python) | ~15,943 |
| factory_v2/ LOC (Python) | ~1,945 |
| Distinct next-work determination paths | 4 |
| Distinct state stores | 9 |
| Distinct retry systems | 3 |
| Distinct validation paths | 3 |
| Active production daemon | factoryd.py (V1) |
| Challenger runtime | factory_v2/runtime.py (V2) |

---

## Evidence Base

- F1: REAL_MULTI_AI_EXECUTION_V2 — COMPLETE (commit e8c82979)
- F2: FOUNDATIONAL_LONG_RUN_TWITTER_RADAR — COMPLETE (commit 4ccc0626)
  - 3 Twitter Radar milestones settled automatically
  - kill+restart recovery proven
  - failure_replan recorded
  - HUMAN_GO=0

The audit is conducted on a runtime PROVEN healthy. This is not speculative.

---

## Subsystem Audit

### 1. Canonical Project Brain

**Files:** `factory_v2/project_authority.py` (111 LOC), `factory/lib/cto_kernel.py` (481 LOC)

**Purpose:** Authority identity anchor — loads and hashes the canonical trio (STRATEGY/SPEC/ROADMAP).

**Current implementation:**
- V2 `ProjectAuthority`: Minimal, clean. Loads project-level trio, computes SHA-256 per file, returns immutable dataclass. Used on EVERY advance() call.
- V1 `CTOKernel`: Loads factory-internal state (CTO_STATE.json + CTO_*.jsonl). Validates 13 cold-restart invariants. Called at daemon boot and pre-dispatch barrier.

**Actual authority:** Both exercised. Different scopes — V2 is project authority, V1 is factory-internal state.

**LOC:** 592 combined

**Tests:** test_cto_loop.py, test_factory_v2.py, test_auth_doctor_live.py

**Known gaps:**
- V1 CTOKernel authority is separate from the project trio loaded by V2. These should be unified under a single canonical authority model.
- No cross-validation: V1 can load stale CTO_STATE.json while V2 loads fresh ROADMAP.yaml — these can diverge.

**Decision:** KEEP both, ADAPT to add cross-hash validation between V1 internal state and V2 project authority on every daemon startup. Long-term: REPLACE V1 CTOKernel with a V2-style authority loader once V2 becomes the champion.

---

### 2. RoadmapResolver / Next-Work Determination

**Files:** `factory_v2/roadmap_resolver.py` (133 LOC), `factory/lib/backlog_refiller.py` (61 LOC), `factory/bin/cto_planner.py` (332 LOC), `factory/bin/campaign_state.py` (243 LOC — `next_mission()`)

**4 distinct next-work paths:**
1. V2 `roadmap_resolver.resolve()` — topological dependency-aware milestone resolution. Correct.
2. V1 `backlog_refiller.select_next_mission()` — queue head (queued[0]). No dependency awareness.
3. V1 `cto_planner.promote_h2_to_h1()` — H1/H2 horizon manager. Triggered post-settlement.
4. Legacy `campaign_state.Campaign.next_mission()` — DAG walk. Deprecated path.

**Authority drift risk:** HIGH. The V2 resolver reads from the canonical ROADMAP.yaml. The V1 backlog_refiller reads from APP007_CAMPAIGN_STATE.json. These can disagree. **[CORRECTION 2026-09-18 (session 3) : l'attribution ci-dessous au chemin V1 comme source de DEF-001 est RÉFUTÉE — voir `FACTORY_F3_COUNTER_AUDIT_RECEIPT.json`.`def001_root_cause` : la cause racine adjudiquée est l'ordre ROADMAP v2 (le fix 829ceb991 ne touche que ROADMAP.yaml/PROJECT_STATE.json/SPEC.md, zéro code ; les modules V1 vivaient encore 6 jours après le fix). Le texte original de 2026-09-07 est conservé comme trace historique.]** ~~V1 path emptied its queue, showed "no work", V2 path was not the live daemon path.~~

**Complexity:** 769 LOC across 4 paths to solve one problem.

**Decision:**
- V2 roadmap_resolver: KEEP — authoritative, correct, 133 LOC
- V1 backlog_refiller: DEMOTE → remove from daemon main loop; keep only as a bridge until V2 daemon exists
- V1 cto_planner: DEMOTE → its H1/H2 horizon concept is superseded by V2 topological resolution. The concept is valuable but the implementation is factoryd-specific.
- Legacy campaign_state.next_mission(): DELETE → deprecated, no caller once backlog_refiller is demoted

**Next-work paths after F4 reshape:** 1 (roadmap_resolver only)

---

### 3. ValidationContract / Mission Completion Contract

**Files:** `factory_v2/validation_contract.py` (87 LOC), `factory/lib/task_contract_validator.py` (239 LOC), `factory/lib/milestone_controller.py` (302 LOC)

**3 distinct validation paths:**
1. V2 frozen contract — hash-anchored, authoritative, pre-build. RAISES on drift.
2. V1 task contract validator — structural validation of task file + plan (ATX headings, required fields). Pre-enqueue. Advisory.
3. V1 milestone controller — post-settlement multi-signal gate. Advisory today.

**Key finding:** Paths 2 and 3 are advisory — they log warnings but do not block settlement. This is the root of multiple FALSE_SATISFIED risks. The V2 path is the only one that RAISES.

**Decision:**
- V2 validation_contract: KEEP
- V2 authority_gate IndependentValidator: KEEP
- V1 task_contract_validator: DEMOTE → advisory structural check is useful at enqueue but should not be a gate. Keep as a linter, not a blocker (it blocks on format, not on correctness).
- V1 milestone_controller: REPLACE with a call to V2 IndependentValidator after V2 becomes champion. Its 302 LOC multi-signal evaluation is partially superseded by the frozen contract approach.

**Validation paths after F4:** 2 (frozen contract pre-build + IndependentValidator post-execution)

---

### 4. MissionCompiler / Context Compiler

**Files:** `factory_v2/mission_compiler.py` (53 LOC), `factory/lib/context_compiler.py` (329 LOC), `factory/lib/context_package.py` (~50 LOC)

**V2 MissionCompiler:** Clean, 53 LOC. Derives mission execution units from roadmap milestone `execution.steps`. Pure function.

**V1 ContextCompiler:** 329 LOC. Builds selective context package from CTO_* files. Filters by mission component/touched_paths to control context size. Used in production by dispatcher_v3. No V2 equivalent.

**Gap:** V2 runtime has no equivalent to the ContextCompiler — it relies on AutopilotExecutor delegating to dispatcher_v3 which calls the V1 ContextCompiler. This is the deepest coupling between V2 and V1.

**Decision:**
- V2 mission_compiler: KEEP — will be extended with execution.steps in ROADMAP.yaml
- V1 context_compiler: KEEP — no equivalent exists, still needed. ADAPT to accept V2 `ProjectAuthority` as authority source (currently reads only CTO_*.jsonl files, not the V2 project trio). This is a concrete gap: context compiled from V1 files while authority is V2.

> **[CORRECTED 2026-09-19 — codex BLOCK 2f0e5bf3]** The parenthetical above and the gap it
> describes were true as of 2026-09-07 and are NO LONGER true at HEAD: the ContextCompiler's V2
> authoritative path exists since F4 (commit c489224e2). `ContextCompiler.__init__(repo_root,
> project_dir=…)` — context_compiler.py:135-140 ('P6 fix: V2 authoritative path') — makes
> `_strategy_text()` read `project_dir/STRATEGY.md` fail-closed (:146-155, FileNotFoundError if
> missing) and `_spec_text()` read `project_dir/SPEC.md` fail-closed (:158-168); the
> `CTO_STRATEGY.md` fallback (:156) is reachable ONLY when `project_dir=None` (legacy path).
> `factory/bin/dispatcher_v3.py:1096` passes `project_dir` on every V2 dispatch
> (`compiler = ContextCompiler(repo_root, project_dir=project_dir)`), and the V2
> AutopilotExecutor passes `--project-dir` (factory_v2/executors.py:691). V2-dispatched workers
> receive V2 canonical context — P6 is closed. grep/sed re-executed 2026-09-19 at HEAD ed8cbfc37.

---

### 5. ModelRouter / Multi-AI Dispatcher

**Files:** `factory/bin/model_router.py` (198 LOC)

**Current state:** Static lookup table: (spec_level, task_kind) → engine. Stall-count escalation ladder. 198 LOC, no disk I/O, pure function.

**Decision:** KEEP in principle, ADAPT to read routing weights from RunRecords (adaptive routing, F7). The static table is correct today because RunRecords don't yet exist — hardcoded is appropriate until F7. No premature removal.

---

### 6. Agent Backends (dispatcher_v3)

**Files:** `factory/bin/dispatcher_v3.py` (918 LOC — largest single file)

**What it does:** CLI dispatch to agent backends (subprocess), per-stage retry (T0/T2/T3), worktree management, heartbeat writes, budget tracking, receipt generation, context package injection.

**Assessment:** dispatcher_v3 is the correct place for these concerns. However at 918 LOC it has accumulated several concerns that could be separated:
- Budget tracking: 60+ LOC inlined, could be a module
- Context injection: calls ContextCompiler (V1-only)
- Worktree management: delegates to WorktreeManager (clean)
- Stage retry logic (T0→T2→T3): 200+ LOC inline decision tree

**Decision:** KEEP the dispatcher approach. ADAPT by extracting the inline stage-retry logic into a separate `retry_policy.py` (reduces dispatcher_v3 from 918 to ~600 LOC). This reduces cognitive complexity without changing semantics.

---

### 7. Execution Backend / Runtime Core

**Files:** `factory_v2/runtime.py` (168 LOC), `factory/bin/autopilot.py` (423 LOC), `factory/bin/dispatcher_v3.py` (918 LOC)

**V2 runtime.py:** The architecturally correct design. Stateless advance(), every call reloads authority, idempotent settlement via contract_id. 168 LOC.

**V1 autopilot.py:** Sequential long-run mission runner. Maintains internal mission list from plan file. Does not reload authority between missions. 423 LOC.

**Gap:** autopilot.py does NOT reload canonical authority between missions. If roadmap changes during a long run, the V1 path continues from its internal mission list — drift is possible. This is the mechanism behind STALE_CTO_CONTEXT_DISPATCH risk.

**Decision:**
- V2 runtime.py: KEEP, PROMOTE to champion when daemon exists
- V1 autopilot.py: DEMOTE → once V2 daemon exists, autopilot.py is redundant. The V2 runtime's `run_until_stable()` covers what autopilot.py does but with authority-reload between cycles.

---

### 8. factoryd Supervisor (Persistent Daemon)

**Files:** `factory/bin/factoryd.py` (1039 LOC)

**Assessment:** The production daemon. No V2 equivalent. It owns: poll loop, worker lifecycle, completion detection, stall recovery, campaign state management, CTO event emission, portfolio transitions.

**Decision:** KEEP, ADAPT. It is the only supervisor. However it is overweight (1039 LOC) — it handles concerns that could be modules:
- The CTO event emission belongs with cto_reporter
- The portfolio logic belongs with long_run_supervisor  
- The startup check should be a pure function

**Target after F4:** factoryd reduced to ~600 LOC (poll loop + lifecycle) with extracted modules.

---

### 9. Process/Worktree Lifecycle

**Files:** `factory/lib/worktree_manager.py` (139 LOC), `factory/lib/worktree_info.py` (~30 LOC)

**Decision:** KEEP — git worktrees are the right isolation primitive at this scale. Docker would add operational complexity with no proven benefit. The implementation is clean and well-tested.

---

### 10. Candidate Lifecycle / Registry / QRTS / CAS

**Files:** `factory/lib/candidate_registry.py` (128 LOC), `factory/lib/qualification_runner.py` (96 LOC)

**Assessment:** The QRTS/CAS concept (qualified runtime test subject + compare-and-swap promotion) is sound. The implementation is clean, flock-protected, fail-closed.

**Gap:** Non-fatal swallowing of registry failures in factoryd (`try: register; except: pass`). A registry failure during dispatch means the promotion pipeline loses track of the candidate — silent data loss.

**Decision:** KEEP concept and implementation. ADAPT: make registry failures non-fatal but LOGGED with explicit warning + INVARIANT violation record rather than silently swallowed.

---

### 11. ArtifactStore / EventStore / AttemptStore

**Finding:** These class names do NOT exist on the main factory/ runtime path. In the main codebase:
- EventStore equivalent: CTO_EVENTS.jsonl (bare JSONL append, no class wrapper)
- ArtifactStore equivalent: per-mission evidence directories (no abstraction)
- AttemptStore equivalent: failure_director.jsonl (FailureDirector class)
- RunRecords: factory_v2/runrecords.py (V2, 64 LOC)

The RUN8/factory_gauntlet has EventStore/ArtifactStore classes — gauntlet-only, not on the production path.

**Decision:** 
- The gauntlet EventStore/ArtifactStore: DEMOTE — gauntlet-specific, not production. Do NOT pull into main codebase.
- CTO_EVENTS.jsonl bare appends: ADAPT → wrap in a thin `EventLog` class (20 LOC) for type safety and test isolation. No behavioral change.
- factory_v2/runrecords.py: KEEP, WIRE into AutopilotExecutor so runs are actually recorded.

---

### 12. FactoryKernel / CTOKernel

**Files:** `factory/lib/cto_kernel.py` (481 LOC)

**Decision:** KEEP — it is the factory-internal state authority. ADAPT to read from the V2 authority hashes as a cross-validation step, so the kernel fails fast if factory-internal state diverges from the canonical project authority.

---

### 13. FailureDirector (Anti-Loop Fingerprints)

**Files:** `factory/lib/failure_director.py` (216 LOC)

**Assessment:** The most Factory-specific logic. Correct principle: fingerprint-based failure dedup + escalation ladder. Clean implementation. No public equivalent that does this for AI coding agent tasks specifically.

**Decision:** KEEP — this is Factory differentiation. No OSS framework provides equivalent failure-fingerprint escalation for coding agent tasks.

---

### 14. LongRunSupervisor / Portfolio

**Files:** `factory/lib/long_run_supervisor.py` (373 LOC)

**Decision:** KEEP core concept. ADAPT: the hardcoded V1 delivery gates (`ctok_chain_complete`, `full_regression_clean`, etc.) need to be replaced with general-purpose resolution from the canonical ROADMAP.yaml V2 resolver. The hardcoded gates are an older form of the same logic that roadmap_resolver now does correctly.

**Target after F4:** long_run_supervisor reduced to ~150 LOC (portfolio transitions + idle detection) with gate logic delegated to roadmap_resolver.

---

### 15. Restart/Recovery Logic

**V1 factoryd:** `_load_or_create_state()` + `_reconcile_stale_active()`. Correct but complex. State is imperative — what matters is what's in supervisor_state.json.

**V2 runtime.py:** Stateless advance() — recovery is implicit (recompute from disk). Simpler and more correct.

**Decision:** KEEP V2's stateless advance() model. When V2 becomes the champion daemon, the V1 restart recovery logic becomes unnecessary. For now, KEEP both during transition.

---

### 16. ConvergenceGate / authority_gate

**Files:** `factory/lib/convergence_gate.py` (408 LOC — advisory), `factory_v2/authority_gate.py` (125 LOC — authoritative)

**Key distinction:** V1 ConvergenceGate is advisory (log + create repair missions). V2 IndependentValidator is authoritative (raises on failure).

**Decision:**
- V2 authority_gate: KEEP — the correct model
- V1 convergence_gate: DEMOTE → its gap-analysis function (SPEC_DRIFT, CODE_NOT_IN_ARCHITECTURE) is valuable but should feed the F8 AUTOMATED_AUDIT_RESEARCH_LOOP, not be a runtime settlement gate. Reduce to a read-only audit tool (not called on every settlement).

---

### 17. RunRecords / Trajectory Schema

**Files:** `factory_v2/runrecords.py` (64 LOC)

**Decision:** KEEP + WIRE NOW. AutopilotExecutor must call `append_run()` after each execution. This is a concrete unwired path that blocks F7 (ADAPTIVE_ROUTING). Blocking gap.

---

### 18. Progress Watchdog / Heartbeat / Stall Detection

**Files:** `factory/lib/progress_watchdog.py` (181 LOC), `factory/lib/heartbeat.py` (29 LOC), `factory/lib/completion_detector.py` (83 LOC)

**Decision:** KEEP — the four-signal composite stall detection (heartbeat + mtime + CTO_EVENTS + git log) is more robust than any public framework. No duplication of ProgressWatchdog with CompletionDetector concerns: they answer different questions (stalled vs done).

---

### 19. Champion/Challenger Automation

**Files:** `factory_v2/verify_benchmark_receipt.py`, `factory/bin/benchmark.py` (313 LOC), `factory/bin/solo_benchmark_score.py` (264 LOC), `campaigns/RUN8/factory_gauntlet/factory_gauntlet/champion.py`

**Decision:** KEEP benchmark.py as the comparison harness. DEMOTE solo_benchmark_score.py — it is the XR04 campaign-specific benchmark, not a general Factory capability. DELETE the gauntlet champion.py — it belongs to the RUN8 campaign, not the main runtime.

---

## Identified Dead / Deprecated Code

1. `factory/bin/campaign_state.py:Campaign.next_mission()` — deprecated, superseded by roadmap_resolver
2. `factory/bin/cto_planner.py` — superseded by roadmap_resolver for dependency resolution
3. `campaigns/RUN8/factory_gauntlet/` — campaign-specific benchmark artifact, not production code
4. `factory/bin/solo_benchmark_score.py` — XR04-specific, not a general capability
5. Legacy `autopilot.py` — will be superseded by V2 runtime once V2 daemon exists
6. Long_run_supervisor hardcoded V1 gates — superseded by ROADMAP.yaml-driven resolution

---

## Key Architectural Findings (Claude authority analysis)

### Finding A: The Two-Track Problem
The factory runs two parallel architectures: V1 (factoryd + autopilot + dispatcher_v3, production) and V2 (runtime.py + modules, challenger). The V2 challenger is architecturally superior in every measured dimension (authoritative gates, stateless recovery, canonical authority loading, 133 LOC vs 769 LOC for roadmap resolution). However it lacks the daemon, context compiler, failure director, and portfolio supervisor that V1 has. The V2 ExecutorAdapter bridges to V1's dispatcher, so V2 inherits V1's backend without replacing it.

**Risk:** This two-track structure can continue indefinitely — V2 keeps getting tested but never promoted because it lacks pieces. The F4 reshape must produce a V2 daemon that replaces factoryd, not just a better runtime module.

### Finding B: Advisory Gates are False Security
V1 has 3 validation paths, all advisory. This means a mission can be ACCEPTED even if the milestone gate fails — the gate logs a warning and continues. The invariant `FALSE_SATISFIED = 0` cannot be maintained with advisory gates. V2's RAISE-on-failure is the correct model. The F4 reshape must wire V2 gates as the authoritative settlement path.

### Finding C: Authority Reloading is Not Enforced in V1
autopilot.py does NOT reload the canonical authority between missions. If the roadmap changes mid-run, the V1 path continues from its internal plan list. **[CORRECTION 2026-09-18 (session 3) : la phrase historique « This caused DEF-001 … indirectly » est RÉFUTÉE par l'adjudication def001_root_cause (cause racine = ordre ROADMAP v2, fix 829ceb99 zéro ligne de code, cf. contre-audit) — l'absence de rechargement d'autorité reste un risque réel de divergence, mais il n'est PAS la cause démontrée de DEF-001.]** The V2 advance() model (reload on EVERY cycle) is the correct fix.

### Finding D: RunRecords Not Wired
RunRecords exist (factory_v2/runrecords.py) but AutopilotExecutor does not call `append_run()`. This means F6/F7 (RunRecords, AdaptiveRouting) are blocked by a wiring gap, not by a missing implementation. This is a 5-LOC fix.

### Finding E: Overengineering in factoryd
factoryd.py at 1039 LOC is the most complex file. It handles poll loop, worker lifecycle, completion detection, stall recovery, campaign state management, CTO event emission, portfolio transitions, AND startup validation. This violates single-responsibility. The F4 reshape should extract these into dedicated modules, reducing factoryd to a thin coordinator (~400 LOC).

### Finding F: Missing V2 Daemon
The biggest structural gap. V2 has no persistent daemon. `run_until_stable()` can run a full project run, but it is synchronous and single-process — no external monitoring, no kill-9 restart, no stall detection. The F4 reshape must produce a minimal `factoryctl.py` daemon that wraps V2's `advance()` in a poll loop with the V1 watchdog and supervisor state.

---

## Summary Decision Table

| Subsystem | Decision | Rationale |
|-----------|----------|-----------|
| ProjectAuthority (V2) | KEEP | Correct, minimal, authoritative |
| CTOKernel (V1) | KEEP + ADAPT | Needed for factory-internal state; add cross-hash validation |
| RoadmapResolver (V2) | KEEP | The only correct next-work path |
| BacklogRefiller (V1) | DEMOTE | Queue-head selection, no dependency awareness |
| CTOPlanner (V1) | DEMOTE | H1/H2 superseded by topological resolver |
| campaign_state.next_mission() | DELETE | Deprecated, no live callers after backlog_refiller demoted |
| ValidationContract (V2) | KEEP | Hash-anchored frozen contract — correct model |
| TaskContractValidator (V1) | DEMOTE | Structural linter, not a gate |
| MilestoneController (V1) | REPLACE | Superseded by V2 IndependentValidator |
| MissionCompiler (V2) | KEEP | Clean, 53 LOC |
| ContextCompiler (V1) | KEEP + ADAPT | No V2 equivalent; ADAPT to accept ProjectAuthority as source |
| ModelRouter | KEEP + ADAPT | Static today; wire to RunRecords for F7 adaptive routing |
| dispatcher_v3 | KEEP + ADAPT | Extract stage-retry logic to retry_policy.py |
| V2 runtime.py | KEEP + PROMOTE | 168 LOC, stateless, authoritative |
| autopilot.py | DEMOTE | Superseded by V2 runtime once V2 daemon exists |
| factoryd.py | KEEP + ADAPT | Only supervisor; extract sub-concerns to reduce LOC |
| WorktreeManager | KEEP | Git worktrees are correct at this scale |
| CandidateRegistry | KEEP + ADAPT | Make failures non-silent |
| Gauntlet EventStore/ArtifactStore | DEMOTE | Gauntlet-only, not production |
| CTO_EVENTS.jsonl | ADAPT | Thin EventLog class wrapper |
| RunRecords | KEEP + WIRE | 5-LOC wiring fix unblocks F6/F7 |
| FailureDirector | KEEP | Factory differentiation, no OSS equivalent |
| LongRunSupervisor | KEEP + ADAPT | Hardcoded gates → ROADMAP.yaml-driven |
| ConvergenceGate (V1) | DEMOTE | Audit tool, not a settlement gate |
| IndependentValidator (V2) | KEEP | Authoritative settlement gate |
| ProgressWatchdog + Heartbeat | KEEP | Robust four-signal detection |
| benchmark.py | KEEP | Comparison harness for F5 |
| solo_benchmark_score.py | DEMOTE | XR04-specific, not general |
| gauntlet champion.py | DELETE | RUN8 campaign artifact |

---

## Metrics: Before vs After Target (Post-F4)

| Metric | Before | Target After F4 |
|--------|--------|----------------|
| Next-work paths | 4 | 1 |
| Validation paths | 3 | 2 |
| State stores | 9 | 7 (2 demoted) |
| Retry systems | 3 | 2 (dispatcher retry + FailureDirector) |
| factoryd.py LOC | 1039 | ~400 |
| dispatcher_v3.py LOC | 918 | ~600 |
| long_run_supervisor.py LOC | 373 | ~150 |
| Total factory/ LOC | ~15,943 | ~13,000 (est.) |
| Deleted dead code | 0 | ~1,500 LOC |
| Advisory gates | 3 | 0 |
| Authoritative gates | 1 | 2 |

---

## Mise à jour 2026-09-12 — audit « resserrer l'entonnoir et la boucle »

**Source :** `.context/factory-tightening-audit-2026-09-12/RAPPORT_FINAL_2026-09-12.md` (dépôt
factory-foundation-v2, branche `factory/e1-probe`), `FIX_PLAN_v2.json` (86 remarques GLM/Sol
intégrées), `FINDINGS_RAW_96.json` (96 findings bruts → 80 clusters), `RESEARCH_EXTERNE_2026-09-12.md`
(175 sources). Sept lentilles rendues sur douze prévues (ECO, DSP, CTL, CPX, STA, GAT, TST) ; cinq
perdues sur limite de session (OBS, HYG, AUT, SCL, RCA).

**AVERTISSEMENT session 3 (2026-09-18, BLOCK codex a4f12607) :** AUCUN de ces fichiers source
n'existe dans le dépôt et aucun n'a jamais été commité (`git log --all --diff-filter=A -- <chemin>`
→ vide pour chacun ; `.context/factory-tightening-audit-2026-09-12/` ne contient que
`RESILIENCE_DRILL.md`). Tout ce qui suit est donc un récit as-of de la session 2026-09-12,
non reproductible depuis le dépôt. Re-mesures fraîches au HEAD de la session 3 : `factory/`
(hors campaigns) = **20 407** lignes Python ; `factory_v2/` = **47 791** lignes Python
(`find <dir> -name '*.py' | xargs wc -l`, exécuté). Le pic de `_advance_locked` (1 252 lignes)
est historiquement exact — vérifié par AST aux commits `3aabb645b584`, `5be3de94b49c`,
`04bd435c4460` (2026-09-11/12) ; au HEAD courant `_advance_locked` = 86 lignes, la logique
advance étant décomposée dans le paquet `factory_v2/advance/` (10 modules, 3 507 lignes mesurées).
Les autres chiffres (99 fichiers/23 811 lignes hors chemin vivant, 5 chaînes d'état, CC=184,
283 tentatives fantômes) restent as-or sources-absentes.

**Chiffres mesurés qui remplacent l'estimation du 09-07** (méthode : AST + git grep sur le tree,
jamais un chiffre lu dans un rapport auto-attesté) :

| Métrique | 2026-09-07 | 2026-09-12 mesuré |
|---|---|---|
| factory/ LOC (Python, hors campaigns/) | ~15 943 | 18 729 |
| factory_v2/ LOC (Python) | ~1 945 | 39 934 |
| Code hors chemin vivant | non mesuré | 99 fichiers / 23 811 lignes (44 %) ; 54 sans importeur ni test |
| Complexité de `runtime._advance_locked` | 168 lignes (stateless, cité KEEP+PROMOTE) | 1 252 lignes, CC=184, 66 if, 28 except |
| Chaînes d'état concurrentes « prochain travail » | 4 (audit initial) | 5 (STA) |

`factory_v2/runtime.py`, classé `KEEP+PROMOTE, 168 LOC, stateless` le 09-07, est devenu le principal
point chaud de complexité en 5 jours (91 commits) — la trajectoire de croissance n'était pas mesurable
au moment du premier audit ; ce n'est pas une erreur du 09-07, c'est un fait nouveau.

**Root cause du blocage de la semaine** (voir rapport section 3) : le daemon écrivait une prédiction
et un lease AVANT le gate de lancement pour la mission `FULL_CODEBASE_STATE_OF_ART_AUDIT` elle-même
(dont deux vérificateurs renvoyaient `VERIFIER_NOT_IMPLEMENTED` par construction), fermait ensuite la
prédiction en faux `WORKER_DEATH` toutes les ~34 s — 283 tentatives fantômes sur 286, 4,8 h de boucle
stérile mesurées dans `runrecords.jsonl`/`factoryd.log`. Correctifs postés dans la nuit et aujourd'hui
(13 + 14 lots, tests de régression nommés par lot) : voir rapport sections 3 et 6. Ce jalon d'audit
lui-même était donc bloqué par le défaut qu'il documente — le producteur d'observations
(`factory_v2/audit_observation_producer.py`, ajouté cette nuit, commit `40f1957d`) le rend
réglable en recomputant les faits depuis le checkout au lieu de croire un booléen auto-attesté.

**Décisions KEEP/ADAPT/DELETE — corrections de forme (2026-09-12)** : 9 décisions du ledger portaient
une valeur composée (`KEEP+ADAPT`, `KEEP+PROMOTE`, `KEEP+WIRE`) hors de l'énum du contrat
(`KEEP,REUSE,ADAPT,WRAP,REPLACE,DEMOTE,DELETE,REJECT_EXTERNAL`), et une entrée
(`ProgressWatchdog+Heartbeat`) citait un `file` combiné non résolvable par `git show`. Narrowées en
place dans `FACTORY_CONVERGENCE_LEDGER.json` (l'intention d'adaptation reste dans les champs
`*_spec`) ; l'entrée combinée est scindée en deux décisions `KEEP` distinctes
(`factory/lib/progress_watchdog.py`, 181 lignes ; `factory/lib/heartbeat.py`, 29 lignes, mesurées
`wc -l`). Sept entrées de `FACTORY_VS_UPSTREAM_MATRIX.json` citaient des chemins non résolvables
(texte de note inline, ou fichiers de production jamais réels — `factory/lib/attempt_store.py`,
`factory/lib/event_store.py` n'existent pas, vérifié par `git ls-files`) ; corrigées vers les fichiers
réels (ex. `factory/bin/dispatcher_v3.py`, `factory/lib/supervisor_state.py`).

**Relecteurs réels de ce cycle** (voir aussi `FACTORY_F3_COUNTER_AUDIT_RECEIPT.json`) : GLM
(`zai-coding-plan/glm-5.3`) relecture adversariale de 11 lots + câblages orchestrateur, verdict
`ACCEPT_WITH_FIXES` à HEAD `f1ff9f70` (`.context/factory-tightening-audit-2026-09-12/REVIEW_FIXES.json`) ;
GLM+Sol audit indépendant 7 lentilles, 96 findings bruts
(`.context/factory-tightening-audit-2026-09-12/AUDITS_RAW_7_LENTILLES_2026-09-12.json`,
`FINDINGS_RAW_96.json`) ; Codex/Sol ordres de contre-audit R1/R2 et pilotage de la reprise nocturne
(13 commits 00:09–04:04) ; Claude (Fable, cette synthèse) + 14 lots d'ouvriers Sonnet, chacun relu par
un passage adversarial séparé. Ces relectures sont des documents datés et vérifiables, mais aucune
n'est encore écrite dans `evidence/review_accounts.jsonl` par `review_dispatch.dispatch_review()` —
le format que `verify_audit_receipt.py` recompute pour la clause `reviewers_min_3_distinct_accept`.
Cette clause reste donc UNMET tant que ce câblage n'existe pas ; voir
`FACTORY_F3_COUNTER_AUDIT_RECEIPT.json`.`update_2026_09_12`.

**Ce qui reste ouvert (non réglé par cette mise à jour)** : le reshape (DELETE/REPLACE effectifs),
la fusion des 5 chaînes d'état, la suppression des 99 fichiers hors chemin vivant, l'écriture réelle
d'une relecture multi-IA dans le ledger machine-lisible. Voir `RAPPORT_FINAL_2026-09-12.md` section 8
pour le plan P0/P1/P2 complet.

---

*This document is produced from the F3 audit. Convergence decisions are formalized in FACTORY_CONVERGENCE_LEDGER.json.*
