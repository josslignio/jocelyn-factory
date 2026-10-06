# FACTORY PUBLIC STATE-OF-ART COMPARISON

**Produced:** 2026-09-07  
**Milestone:** F3 — FULL_CODEBASE_STATE_OF_ART_AUDIT  
**Method:** Architecture inspection of public codebases + documentation. Research by technical problem, not product name.

---

## Research Method

For each Factory subsystem, the technical problem is stated, then compared against the best public solution available. Decision columns follow the KEEP/REUSE/ADAPT/WRAP/REPLACE/DEMOTE/DELETE/REJECT_EXTERNAL schema.

---

## Framework Summaries

### Temporal (temporal.io)
**Pinned:** `github.com/temporalio/sdk-python` @ `5a1703a30295291f9c944c26ec193d468e200a3b` — `temporalio/common.py` l.36-49 (RetryPolicy dataclass, extrait copié dans `FACTORY_F3_EXTERNAL_RESEARCH_RECEIPT.json`). SHA obtenu par `api.github.com …/git/ref/heads/main`, fichier par `raw.githubusercontent.com` au SHA (session 2026-09-18).

**What it solves:** Durable workflow execution. Workflows are written as normal code (Go/Python/TypeScript); Temporal replays them from event history on restart. Every activity input/output is persisted. Idempotency keys + retry policies per activity.

**Key design:** Workflow = deterministic function. On worker restart, workflow is replayed from event history until current point. State lives in Temporal server's event history DB, not in the worker.

**Relevant to Factory:**
- Restart/recovery: their event-replay model is more durable than Factory's PROJECT_STATE.json approach
- Idempotency: their `workflow_id` + `run_id` pattern for exactly-once execution
- Activity retry: configurable per-activity retry policy with backoff

**Factory vs Temporal recovery:**
- Temporal: replay full event history → restore state at any point
- Factory V2: reload authority from disk → recompute current position
- Temporal's model is stronger (can recover mid-mission), Factory's model is simpler (only recovers between missions at advance() boundaries)

**Decision:** REJECT_EXTERNAL as a dependency. Temporal server is a distributed system; adding it as a dependency at this scale is overcomplicated. ADAPT: adopt their idempotency key concept (`contract_id` in V2) and their activity-level retry policy model (separate from the control loop).

---

### LangGraph (github.com/langchain-ai/langgraph)
**Pinned:** `github.com/langchain-ai/langgraph` @ `c81c13533ee48c1ae0ef2de314737ef0c455f2be` — `libs/checkpoint/langgraph/checkpoint/base/__init__.py` l.177-186 (`class BaseCheckpointSaver(Generic[V])`, extrait copié dans le receipt de recherche).

**What it solves:** Stateful multi-agent graphs with checkpointing. State is a typed dict passed between nodes. Checkpointers (SQLite/Postgres/Redis) save full state at each step for resume/rollback.

**Key design:** Graph nodes = agents or tools. State flows through edges. `interrupt_before`/`interrupt_after` for human-in-the-loop. Thread IDs for parallel execution streams.

**Relevant to Factory:**
- Checkpoint/resume: their `BaseCheckpointSaver` interface is a clean abstraction over state persistence
- Thread isolation: their thread_id concept maps to Factory's mission_id for parallel workers
- Human interrupt: their `interrupt_before` maps to Factory's human gate concept

**Factory vs LangGraph:**
- LangGraph uses Python objects as state; Factory uses JSON files (more durable, cross-process safe)
- LangGraph requires a checkpointer backend; Factory's PROJECT_STATE.json IS the checkpointer
- LangGraph graphs are defined in Python; Factory's roadmap is in YAML (language-agnostic)

**Decision:** REJECT_EXTERNAL as a framework. LangGraph's graph model is a mismatch — Factory's roadmap is a dependency graph defined declaratively, not imperatively in Python. ADAPT: their `BaseCheckpointSaver` interface is worth replicating as a clean abstraction for Project STATE persistence.

---

### OpenHands (github.com/OpenHands/software-agent-sdk)
**Pinned:** `github.com/OpenHands/software-agent-sdk` @ `28e8ed273617992e9556410804f54937cc059878` — `openhands-sdk/openhands/sdk/event/base.py` l.20-28 (`class Event(DiscriminatedUnionMixin, ABC)`, extrait copié dans le receipt). Renommage observé 2026-09-18 : l'ancien monorepo `All-Hands-AI/OpenHands` est scindé — l'app desktop reste `OpenHands/OpenHands`, le SDK agent inspecté ici vit sous `OpenHands/software-agent-sdk`.

**What it solves:** General software engineering agent with micro-agent architecture. Docker sandbox for each task. Planner + Coder + Verifier agents. Observable action space (shell, file edit, browse, Python REPL).

**Key design:** Each task runs in a sandboxed Docker environment. CodeActAgent handles code execution. Runtime isolation is at the process + filesystem level.

**Relevant to Factory:**
- Sandbox/isolation: Docker per task is stronger than git worktrees
- Observable action space: structured action types (rather than free-form subprocess)
- Micro-agent decomposition: planner separates concern from executor

**Factory vs OpenHands:**
- OpenHands: Docker sandbox per task (process + filesystem isolation)
- Factory: git worktrees per mission (git branch isolation only)
- OpenHands has stronger isolation but requires Docker infrastructure
- OpenHands doesn't have Factory's persistent roadmap progression or multi-project portfolio concept

**Decision:** REJECT_EXTERNAL (Docker sandbox). Git worktrees are sufficient at current single-machine scale. Docker adds infra complexity with no proven benefit for Factory's workload (git-centric tasks). KEEP Factory worktrees. NOTE: if Factory scales to untrusted external users, reconsider Docker.

**Reuse candidate:** OpenHands' action type taxonomy (Read/Write/Run/Browse/Patch) is cleaner than dispatcher_v3's ad-hoc subprocess invocation. ADAPT as an internal action schema for mission execution steps.

---

### SWE-agent (github.com/SWE-agent/SWE-agent)
**Pinned:** `github.com/SWE-agent/SWE-agent` @ `3ea751c087f32b16e039a2233dd6eefecef325d5` — `sweagent/run/run_single.py` l.188-201 (`RunSingle.run()`, extrait copié dans le receipt). Anciennement `princeton-nlp/SWE-agent` (redirection GitHub).

**What it solves:** Automated software engineering via the Agent-Computer Interface (ACI). Provides a structured tool set (bash, file_viewer, file_editor, search) for an LLM agent working on a git repository.

**Key design:** ACI wraps filesystem + shell operations in LLM-friendly tools. Per-task Docker environment. Trajectory saving as JSONL for replay/analysis.

**Relevant to Factory:**
- Trajectory saving: their JSONL trajectory format maps directly to Factory's RunRecords concept
- ACI tools: a more principled tool abstraction than raw subprocess
- Per-task isolation: Docker

**Factory vs SWE-agent:**
- SWE-agent: single-task focused, no multi-task orchestration or roadmap
- Factory: multi-milestone, multi-project, multi-AI orchestration
- SWE-agent trajectories map exactly to what Factory's RunRecords need

**Decision:** ADAPT their trajectory schema. SWE-agent's trajectory JSONL (action, observation, timestamp, token_count) is a proven schema for this problem. Extend Factory's runrecords.py with SWE-agent-compatible trajectory fields. REJECT_EXTERNAL as a runtime dependency (they don't orchestrate multi-project).

---

### Goose (github.com/aaif-goose/goose)
**Pinned:** `github.com/aaif-goose/goose` @ `1e83e89f556fc60fd396df1fd0f2992f8b2f2dc1` — `crates/goose/src/agents/agent.rs` l.2044-2052 (`pub async fn reply(… ) -> Result<BoxStream<'_, Result<AgentEvent>>>`, extrait copié dans le receipt). Anciennement `block/goose` (redirection GitHub).

**What it solves:** Local AI coding agent with extensible toolkit. Providers (LLMs) + toolkits (shell, file, git, jira). Session-based, stateless between sessions.

**Key design:** Toolkit plugins define tool functions. LLM decides which tools to call. No built-in state persistence between sessions.

**Relevant to Factory:**
- Toolkit abstraction: their toolkit plugin model maps to Factory's engine selection concept
- Provider abstraction: clean multi-LLM support (Claude/OpenAI/etc.)

**Factory vs Goose:**
- Goose: single session, no cross-session state, no roadmap
- Factory: persistent roadmap, multi-session, multi-AI routing with state continuity

**Decision:** REJECT_EXTERNAL (Goose runtime). Factory's multi-session roadmap continuity is not in Goose's scope. ADAPT: Goose's provider abstraction (separate provider from toolkit) is cleaner than dispatcher_v3's engine-specific code paths. Apply this pattern when factory_v2 grows its executor set.

---

### BMAD (méthodologie — non pinnée)
**Statut de citation :** méthodologie, AUCUN commit SHA — sur 2026-09-18, `bmad-method/bmad-method`, `bmad-code/BMAD-METHOD` et `saqirb/BMAD` renvoient tous 404 (curl). BMAD n'est PAS comptée dans le plancher des ≥8 références pinnées et n'est PAS GitHub Spec Kit (déconflation 2026-09-18 : Spec Kit est un toolkit distinct, voir sa section).

**What it solves:** Structured prompt-based software development methodology. Role-based agents (Business Analyst, Architect, PM, Developer) defined via system prompts + checklists. Stateless session management.

**Relevant to Factory:**
- Role separation: clean separation of BA/Architect/Developer concerns maps to Factory's Claude (architecture) / GLM (implementation) / Codex (validation) split
- Checklist-driven verification: their acceptance checklist pattern maps to Factory's ValidationContract

**Factory vs BMAD:**
- BMAD: methodology layer only, no execution infrastructure
- Factory: full execution infrastructure with runtime

**Decision:** REJECT_EXTERNAL (no runtime value). The methodology ideas are already embodied in Factory's mission structure. ADAPT: BMAD's story template (as a user story → acceptance criteria → checklist) is a clean model for Factory's milestone acceptance array.

---

### MetaGPT (github.com/FoundationAgents/MetaGPT)
**Pinned:** `github.com/FoundationAgents/MetaGPT` @ `11cdf466d042aece04fc6cfd13b28e1a70341b1f` — `metagpt/roles/role.py` l.125-134 (`class Role(BaseRole, SerializationMixin, ContextMixin, BaseModel)`, extrait copié dans le receipt). Anciennement `geekan/MetaGPT` (redirection GitHub).

**What it solves:** Multi-agent software engineering with roles (PM/Architect/Engineer/QA/Reviewer). Shared message blackboard. Code review workflows. Documentation generation.

**Key design:** Each role agent reads the shared memory (blackboard) for its relevant messages and produces outputs. Role agents are sequential in the default pipeline.

**Relevant to Factory:**
- Blackboard memory: their shared `Memory` class maps to Factory's evidence directory concept
- Role separation: clean role API (publish/subscribe to message types)
- Code review role: their `QaEngineer` + `CodeReviewer` maps to Factory's Codex validation role

**Factory vs MetaGPT:**
- MetaGPT: roles are sequential by default, single-project focus
- Factory: multi-AI routing with measured performance, multi-project portfolio
- MetaGPT blackboard is in-memory (not durable across restarts)

**Decision:** REJECT_EXTERNAL (in-memory blackboard fails Factory's durability requirement). ADAPT: MetaGPT's role/message type separation is cleaner than Factory's implicit roles. Apply their message type taxonomy to Factory's CTO_EVENTS.jsonl event schema.

---

### Microsoft AutoGen / Semantic Kernel
**Pinned (successeur):** `github.com/microsoft/agent-framework` @ `fdf7e80ad6c8f6dbdd9b779d5d6923c74d8b583e` — `python/packages/core/agent_framework/_agents.py` l.236-246 (`class SupportsAgentRun(Protocol)`, extrait copié dans le receipt). AutoGen + Semantic Kernel ont convergé dans l'Agent Framework (AutoGen en maintenance).

**What it solves:** AutoGen: multi-agent conversation framework with GroupChat/AgentChat patterns. Agents converse to complete tasks. Semantic Kernel: plugin-based orchestration layer with planners.

**Key design (AutoGen):** GroupChatManager routes messages between agents. Each agent is a ChatAgent with a system prompt + tool registry. Conversation history is the state.

**Relevant to Factory:**
- GroupChat: multi-AI coordination pattern
- Planner (Semantic Kernel): sequential/step-wise plan execution maps to Factory's mission execution

**Factory vs AutoGen:**
- AutoGen: conversation-based, state = message history (not durable)
- Factory: evidence-based, state = JSON files (durable, cross-restart)
- AutoGen has no concept of roadmap progression or multi-project portfolio

**Decision:** REJECT_EXTERNAL (no roadmap, no durability). AutoGen's conversation model is the wrong abstraction for Factory's deterministic roadmap execution. ADAPT: AutoGen's agent selector pattern (select_speaker → routes to agent by role) is worth adopting for Factory's engine selection logic in a V3 dispatcher.

---

### GitHub Spec Kit (github.com/github/spec-kit)
**Pinned:** `github.com/github/spec-kit` @ `5e952140659287106580d32f242ba71365a3862b` — `README.md` l.76-84. Déconflation 2026-09-18 : Spec Kit est SON PROPRE toolkit (`uv tool install specify-cli`), sans rapport avec BMAD (la section « PROJECT_BRAIN_INTENT_PRESERVATION » du receipt de recherche 2026-09-07 les confondait — corrigée). Le flux par fonctionnalité, copié verbatim du README au SHA :

```text
**Constitution once per project; specify → plan → tasks → implement → converge per feature.**

/speckit-constitution Create principles focused on code quality, testing, and maintainability.
/speckit-specify Build a photo organizer with albums grouped by date and a tile preview of each album.
/speckit-plan Use Vite with vanilla JavaScript. Keep images local and store metadata in SQLite.
/speckit-tasks
```

**What it solves:** Spec-driven development tooling. Structured specification format with acceptance criteria, user stories, API contracts.

**Relevant to Factory:**
- Spec format: structured YAML/JSON spec with machine-checkable acceptance criteria
- Contract generation: automatic test generation from spec

**Factory vs Spec Kit:**
- Factory already has structured ROADMAP.yaml with acceptance arrays
- The V2 ValidationContract concept is aligned with Spec Kit's approach

**Decision:** ADAPT their acceptance criteria format. Factory's ROADMAP.yaml acceptance arrays could be more structured (currently free-text). Adopting a schema like `{criterion: string, verifiable: bool, test_command: string}` per acceptance item would make the ValidationContract more rigorous.

---

## Cross-Framework Synthesis

### What Factory does better than all compared frameworks:
1. **Multi-project roadmap progression** — no public framework models a dependency-graph roadmap with topological resolution across multiple projects and a persistent portfolio.
2. **Evidence-based durable state** — using JSON files as the state store (not in-memory, not a server) means Factory survives arbitrary restarts without infrastructure.
3. **Failure fingerprinting + bounded escalation** — FailureDirector's fingerprint-based anti-loop with a 5-step ladder is unique. No public framework has this for coding agent tasks.
4. **Multi-AI measured routing** — model_router + RunRecords for evidence-based engine selection is not present in any compared framework.

### What public frameworks do better than Factory:
1. **Temporal: mid-execution restart** — Factory can only recover BETWEEN missions (at advance() boundaries). Temporal can recover mid-activity. **Impact: if an agent crashes mid-task, Factory loses that run.**
2. **SWE-agent + OpenHands: sandbox isolation** — git worktrees provide branch isolation but not process/filesystem isolation. A malicious or buggy agent can corrupt the host repo.
3. **LangGraph: human-interrupt protocol** — Factory's human gate concept is informal. LangGraph has a formal interrupt_before/after with state rollback.
4. **MetaGPT: role/message typing** — Factory's inter-component communication is informal (JSON files, no schema). MetaGPT's typed message blackboard is cleaner.

### Key Adoption Decisions (to carry into F4):
- ADAPT Temporal's idempotency key concept → already done in contract_id, extend to activity-level
- ADAPT SWE-agent trajectory schema → extend runrecords.py to be trajectory-compatible
- ADAPT LangGraph's BaseCheckpointSaver interface → wrap PROJECT_STATE.json in a clean saver interface
- ADAPT MetaGPT's event type taxonomy → apply to CTO_EVENTS.jsonl schema
- REJECT all as framework dependencies — Factory's requirements (multi-project, durable, multi-AI routing, failure fingerprinting) are not met by any single public framework

---

## Mise à jour 2026-09-12 — références publiques supplémentaires inspectées

**Source :** `.context/factory-tightening-audit-2026-09-12/RESEARCH_EXTERNE_2026-09-12.md` (175
sources, 6 axes : boucles solo, frameworks, dépense, fiabilité, vérification, simplification).
**Note session 3 (2026-09-18) :** ce fichier source n'est PAS dans le dépôt et n'a jamais été
commité (`git log --all --diff-filter=A -- <chemin>` → vide) — les affirmations qui s'y réfèrent
sont des mesures de session 2026-09-12, as-of, non reproductibles depuis le dépôt.

Deux nouvelles références au code source pinné (commit_sha réel, vérifiable) s'ajoutent aux 9 déjà
dans `UPSTREAM_SOURCE_MANIFEST.json` :

- **mini-swe-agent** (`github.com/SWE-agent/mini-swe-agent`, commit `04d809ce`) —
  `src/minisweagent/agents/default.py` : boucle `run()` → `while True: step()`, états de sortie typés
  (`LimitsExceeded`/`TimeExceeded`/`RepeatedFormatError`) au lieu d'un échec générique. Directement
  applicable au défaut DSP-01/ECO-03 (deux sessions Claude tuées par `--max-turns 48` fixe,
  indistinguables d'un vrai échec).
- **SWE-ReX** (`github.com/SWE-agent/SWE-ReX`, commit `5c995c36`) — `BashAction`/`BashObservation`
  typés vs le `subprocess`/`Popen` non typé de `dispatcher_v3.py`.

Deux autres (opencode-anti-loop, ralph-orchestrator — détection de boucle stérile par seuils de
similarité et échecs consécutifs) sont citées par URL dans le rapport mais sans commit pinné
vérifiable indépendamment ; elles ne sont **pas** ajoutées à `UPSTREAM_SOURCE_MANIFEST.json` pour
cette raison — voir `FACTORY_F3_EXTERNAL_RESEARCH_RECEIPT.json`.`update_2026_09_12` pour le détail et
la justification de ne pas les compter.

**Constat confirmé par la recherche externe** : la factory a déjà les briques que ces frameworks
recommandent (cost_no_progress, escalation ladder, verifiers, heartbeat) mais sa boucle EXTERNE
(factoryd) violait les règles que ralph-orchestrator / opencode-anti-loop / mini-swe-agent encodent en
dur : pas d'arrêt/recul sur boucle stérile détectée, pas d'état terminal typé sur épuisement de tours,
écriture d'état avant vérification du droit de lancer. Voir `RAPPORT_FINAL_2026-09-12.md` section 7.

---

## Mise à jour 2026-09-18 — citations pinnées, déconflations, corrections de mesures

Réponse au BLOCK du relecteur Claude sur aa295959 (« moins de 8 références publiques avec une vraie
citation de code ; N/A et (fetched via WebFetch) ne sont pas des commit SHA ; OpenHands, BMAD, Goose,
MetaGPT, AutoGen « conceptuels » ; GitHub Spec Kit confondu avec BMAD ») :

- **10 références pinnées** (repo + commit SHA 40 hex + fichier + extrait ≥ 3 lignes copié verbatim
  depuis `raw.githubusercontent.com` au SHA) : Temporal, LangGraph, SWE-agent, mini-swe-agent
  (04d809ce…), SWE-ReX (5c995c36…), OpenHands Agent SDK, Goose, MetaGPT, Microsoft Agent Framework
  (fdf7e80a…), GitHub Spec Kit. Le détail vit dans `FACTORY_F3_EXTERNAL_RESEARCH_RECEIPT.json`
  (`frameworks_inspected`) et `UPSTREAM_SOURCE_MANIFEST.json` (plus aucun placeholder).
- **Renommages observés** (redirections GitHub suivies au fetch) : princeton-nlp/SWE-agent →
  SWE-agent/SWE-agent ; All-Hands-AI/OpenHands → monorepo scindé (app OpenHands/OpenHands, SDK
  OpenHands/software-agent-sdk — le SDK est celui cité) ; block/goose → aaif-goose/goose ;
  geekan/MetaGPT → FoundationAgents/MetaGPT.
- **Spec Kit ≠ BMAD** : déconflation faite partout (ci-dessus, receipt, manifeste). « Converge » EST
  un concept Spec Kit (flux `implement → converge`, README l.76 au SHA pinné) ; l'ancien bloc
  « Conceptual pattern (BMAD methodology) » dans le receipt est remplacé par l'extrait réel.
- **BMAD** : méthodologie non pinnée (les trois repos candidats 404 au 2026-09-18), explicitement
  non comptée dans le plancher des ≥8.
- **mini-swe-agent** : `src/minisweagent/agents/default.py` mesuré **190 lignes** (wc -l sur le raw
  au SHA) — le « 160 LOC » de 2026-09-07 décrivait le cœur de boucle estimé.
- **RunRecords** : l'affirmation « NOT WIRED / zero runs recorded » (ledger + carte des autorités +
  matrice, 2026-09-07/09-13) est **réfutée par mesure** : appels réels `record_settlement`
  (factory_v2/settlement_ledger.py:272), `record_attempt` (factory_v2/advance/mission_dispatch.py:1159,
  via `rt._record_attempt` — correction session 3 : la citation antérieure « review_dispatch.py »
  était fausse, ce module ne fait que LIRE le ledger dans `_builder_engine_from_runrecords`),
  `record_reproduction_baseline` (runtime.py:2699), `record_cell_transition` (runtime.py:4334,4344) ;
  couverture de régression `tests/test_runrecords_wired.py` (4 tests verts) ; journal vivant
  `<run-dir>/journals/runrecords.jsonl` = **55 990 lignes** (wc -l, 2026-09-18T11:42:32Z, session 3 —
  54 926 puis 55 970 lors des sessions précédentes, le daemon vit : compteur mouvant).
  Résidu vrai : les imports `record_*` de runtime.py:94-98 n'ont pas d'appel dans CE module.

---

*Full decision ledger in FACTORY_CONVERGENCE_LEDGER.json.*
