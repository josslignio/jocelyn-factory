"""CTOK-001 -- CTOKernel: the single canonical source of Conductor project state.

Merges every `factory/cto/CTO_*` file into one `CTOState` that answers, from
durable state alone (no chat history), the cold-restart questions: what are we
building, why, what architecture was chosen, what's done/proven, what's in
progress, what's next, what's blocking it, and how we'll know it's done.

Hard invariant added by this module: CONTEXTLESS_AGENT_DISPATCH = 0. No mission
may be dispatched with an incomplete CTOContext -- `validate()` is fail-CLOSED
(raises on any doubt), `reconstruct_project_state()` is fail-OPEN (best effort,
logs every gap) for forensic/recovery use only, never as a substitute for a
passing `validate()` before dispatch.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from factory_v2._io import _read_json

logger = logging.getLogger(__name__)

STATE_FILE = "CTO_STATE.json"
STRATEGY_FILE = "CTO_STRATEGY.md"
ARCHITECTURE_FILE = "CTO_ARCHITECTURE.json"
DECISIONS_FILE = "CTO_DECISIONS.jsonl"
LESSONS_FILE = "CTO_LESSONS.jsonl"
REPLAN_LOG_FILE = "CTO_REPLAN_LOG.jsonl"
RESEARCH_REGISTRY_FILE = "CTO_RESEARCH_REGISTRY.json"
STATUS_LATEST_FILE = "CTO_STATUS_LATEST.md"

REQUIRED_SCALAR_FIELDS = (
    "project_id",
    "north_star",
    "product_goal",
    "why",
    "spec_version",
    "architecture_version",
    "roadmap_version",
    "current_milestone",
    "milestone_goal",
    "last_verified_product_sha",
    "last_verified_factory_sha",
    "next_required_action",
)

REQUIRED_LIST_FIELDS = (
    "completed_milestones",
    "active_missions",
    "blocked",
    "next_milestones",
    "architecture_decisions",
    "known_failures",
    "failed_strategies",
    "reuse_decisions",
)

# CTOK-002B: unlike the fields above (an empty list IS a valid answer -- "no known
# blockers yet"), an empty milestone_definition_of_done means nobody can tell when
# the current milestone is done, which is itself an incomplete context. A plain
# REQUIRED_LIST_FIELDS entry can never catch this: CTOKernel.load() always coerces
# the raw JSON value through `list(x or [])`, so missing/null/empty all collapse to
# `[]` -- a valid list either way. This field is checked separately and must be
# non-empty.
REQUIRED_NONEMPTY_LIST_FIELDS = (
    "milestone_definition_of_done",
)

_UNKNOWN_SENTINELS = {"", "unknown", "n/a", "none", "tbd"}

_INVARIANT_COUNTER_FIELDS = (
    "HUMAN_GO_COUNT",
    "LOST_MISSION_COUNT",
    "DUPLICATE_DISPATCH_COUNT",
    "STALE_ACTIVE_COUNT",
)


@dataclass
class CTOContext:
    product_goal: str
    why: str
    target_user: str
    current_architecture: str
    current_milestone: str
    milestone_goal: str
    milestone_definition_of_done: list = field(default_factory=list)
    dependencies: list = field(default_factory=list)
    known_failed_approaches: list = field(default_factory=list)
    relevant_adrs: list = field(default_factory=list)
    allowed_scope: str = ""
    test_oracle: str = ""
    next_milestone: str = ""


@dataclass
class CTOState:
    project_id: str
    north_star: str
    product_goal: str
    why: str
    spec_version: str
    architecture_version: str
    roadmap_version: str
    current_milestone: str
    milestone_goal: str
    milestone_definition_of_done: list = field(default_factory=list)
    completed_milestones: list = field(default_factory=list)
    active_missions: list = field(default_factory=list)
    blocked: list = field(default_factory=list)
    next_milestones: list = field(default_factory=list)
    architecture_decisions: list = field(default_factory=list)   # from CTO_DECISIONS.jsonl
    known_failures: list = field(default_factory=list)           # from CTO_LESSONS.jsonl
    failed_strategies: list = field(default_factory=list)        # from CTO_REPLAN_LOG.jsonl
    reuse_decisions: list = field(default_factory=list)          # from CTO_RESEARCH_REGISTRY.json
    last_verified_product_sha: str = ""
    last_verified_factory_sha: str = ""
    next_required_action: str = ""


@dataclass
class AuthoritySnapshot:
    """CTOK-002B-P2 -- stable authority identifier for a CTO_STATE snapshot.

    The single canonical token source for both ContextCompiler (compile time)
    and dispatcher_v3's pre-spawn barrier -- both MUST call
    CTOKernel.authority_snapshot() rather than hashing these fields themselves,
    or the two sides could silently drift on what "authority" means."""
    project_id: str
    spec_version: str
    architecture_version: str
    roadmap_version: str
    current_milestone: str
    token: str


class CTOContextIncomplete(Exception):
    """Raised by validate() when a required CTOState field is missing/unknown."""


class StaleCTOContext(CTOContextIncomplete):
    """CTOK-002B: raised when a compiled ContextPackage's version markers
    (architecture_version/roadmap_version/spec_version) no longer match a freshly
    reloaded CTOState -- CTO_STATE.json changed during/after compilation. A subclass
    of CTOContextIncomplete so callers that only catch the base exception still
    hard-block (STALE_CTO_CONTEXT_DISPATCH = 0 is a specialization of
    CONTEXTLESS_AGENT_DISPATCH = 0, not an independent gate)."""


class CrossProjectContextLeak(CTOContextIncomplete):
    """CTOK-002B: raised when a compiled ContextPackage's project_id does not match
    the dispatching mission's own declared project_id -- context from one project
    leaking into another's agent. Subclass of CTOContextIncomplete for the same
    reason as StaleCTOContext."""


class CTOInvariantViolation(Exception):
    """Raised by assert_invariants() when a hard Factory invariant is violated."""


def _read_jsonl(path: Path) -> list:
    out: list = []
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return out
    except OSError as exc:
        logger.warning("cto_kernel: cannot read %s: %s", path, exc)
        return out
    for lineno, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            logger.warning("cto_kernel: skipping malformed line %d in %s: %s", lineno, path, exc)
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except OSError as exc:
        logger.warning("cto_kernel: cannot read %s: %s", path, exc)
        return ""


def _is_unknown(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() in _UNKNOWN_SENTINELS
    return False


class CTOKernel:
    def __init__(self, cto_dir: Path):
        self.cto_dir = Path(cto_dir)
        self._raw_architecture: Optional[dict] = None
        self._raw_state: Optional[dict] = None

    # ------------------------------------------------------------------ load
    def load(self) -> CTOState:
        """Read and merge all CTO_*.json/jsonl/md files. Return unified CTOState.

        Flat scalar/list fields come from CTO_STATE.json (v3). architecture_decisions,
        known_failures, failed_strategies and reuse_decisions are re-read LIVE from
        their respective source files on every call, so the kernel never drifts from
        CTO_DECISIONS.jsonl / CTO_LESSONS.jsonl / CTO_REPLAN_LOG.jsonl /
        CTO_RESEARCH_REGISTRY.json even if CTO_STATE.json's own snapshot goes stale.
        """
        state_doc = _read_json(self.cto_dir / STATE_FILE) or {}
        self._raw_state = state_doc
        self._raw_architecture = _read_json(self.cto_dir / ARCHITECTURE_FILE) or {}

        decisions = _read_jsonl(self.cto_dir / DECISIONS_FILE)
        lessons = _read_jsonl(self.cto_dir / LESSONS_FILE)
        replan_log = _read_jsonl(self.cto_dir / REPLAN_LOG_FILE)
        registry = _read_json(self.cto_dir / RESEARCH_REGISTRY_FILE) or {}

        reuse_decisions = []
        for name, info in (registry.get("libraries") or {}).items():
            if not isinstance(info, dict):
                continue
            reuse_decisions.append({
                "library": name,
                "decision": info.get("decision"),
                "status": info.get("status"),
                "license": info.get("license"),
                "use": info.get("use"),
            })

        failed_strategies = [
            {
                "ts": r.get("ts"),
                "trigger": r.get("trigger"),
                "event": r.get("event"),
                "replan_version": r.get("replan_version"),
            }
            for r in replan_log
        ]

        def _get(key, default):
            return state_doc.get(key, default)

        def _list(key):
            # L6_bin_lib_products_1-032: a list field is taken ONLY when the raw
            # JSON value is a list. `list('TBD')` used to yield ['T','B','D']
            # (a "non-empty" DoD) and missing/null collapsed to a valid [].
            # A string or missing value is kept as-is so validate() sees it.
            value = state_doc.get(key)
            return list(value) if isinstance(value, list) else value

        return CTOState(
            project_id=_get("project_id", ""),
            north_star=_get("north_star", ""),
            product_goal=_get("product_goal", ""),
            why=_get("why", ""),
            spec_version=_get("spec_version", ""),
            architecture_version=_get("architecture_version", ""),
            roadmap_version=_get("roadmap_version", ""),
            current_milestone=_get("current_milestone", ""),
            milestone_goal=_get("milestone_goal", ""),
            milestone_definition_of_done=_list("milestone_definition_of_done"),
            completed_milestones=_list("completed_milestones"),
            active_missions=_list("active_missions"),
            blocked=_list("blocked"),
            next_milestones=_list("next_milestones"),
            architecture_decisions=decisions,
            known_failures=lessons,
            failed_strategies=failed_strategies,
            reuse_decisions=reuse_decisions,
            last_verified_product_sha=_get("last_verified_product_sha", ""),
            last_verified_factory_sha=_get("last_verified_factory_sha", ""),
            next_required_action=_get("next_required_action", ""),
        )

    # --------------------------------------------------------- authority_snapshot
    def authority_snapshot(self) -> AuthoritySnapshot:
        """CTOK-002B-P2 -- stable authority identifier for this CTO_STATE. Used by
        ContextCompiler at compile time and by the dispatcher pre-spawn barrier --
        MUST be the same function in both places to prevent token drift over time.
        """
        state = self.load()
        payload = {
            "project_id": state.project_id,
            "spec_version": state.spec_version,
            "architecture_version": state.architecture_version,
            "roadmap_version": state.roadmap_version,
            "current_milestone": state.current_milestone,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        token = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return AuthoritySnapshot(**payload, token=token)

    # -------------------------------------------------------------- validate
    def validate(self, state: CTOState) -> None:
        """Raise CTOContextIncomplete if any required field is empty/unknown.

        Fail-CLOSED: an empty list IS a valid answer ("no known items yet") for
        the required list fields, but `None`/missing is not -- and every
        required scalar field must be a real, non-placeholder value.
        `REQUIRED_NONEMPTY_LIST_FIELDS` (CTOK-002B) is stricter: an empty list
        there means the field's question genuinely has no answer yet.
        """
        missing = []

        for name in REQUIRED_SCALAR_FIELDS:
            value = getattr(state, name, None)
            if _is_unknown(value):
                missing.append(name)

        for name in REQUIRED_LIST_FIELDS:
            value = getattr(state, name, None)
            if value is None or not isinstance(value, list):
                missing.append(name)

        for name in REQUIRED_NONEMPTY_LIST_FIELDS:
            value = getattr(state, name, None)
            # L6_bin_lib_products_1-032: every item must be a real, non-placeholder
            # string — ['TBD'] or [''] is no definition of done.
            if (not isinstance(value, list) or len(value) == 0
                    or not all(isinstance(item, str) and not _is_unknown(item) for item in value)):
                missing.append(name)

        if missing:
            raise CTOContextIncomplete(
                "CTOState incomplete -- CONTEXTLESS_AGENT_DISPATCH would be violated. "
                f"Missing/unknown fields: {', '.join(missing)}. "
                "Run CTOKernel.reconstruct_project_state() to diagnose gaps."
            )

    # --------------------------------------------------------- compile_context
    def compile_context(self, state: CTOState, mission_id: str) -> CTOContext:
        """Build a mission-specific CTOContext from the unified CTOState."""
        plan = self._load_mission_plan(mission_id)
        track = self._track_for_mission(mission_id)

        if track == "twitter":
            target_user = (
                "Traders/analysts seeking financial X accounts with genuine "
                "historical edge above market/sector/momentum/randomness."
            )
        else:
            target_user = (
                "Jocelyn -- sole operator, wants zero-babysitting autonomous "
                "delivery (72h+ unattended runs)."
            )

        dependencies = []
        test_oracle = "python3 -m pytest tests/ -x -q --tb=short"
        allowed_scope = f"component=UNKNOWN for mission {mission_id} -- no plan file found, scope to task_file only"
        if plan:
            dependencies = list(plan.get("dependency_mission_ids") or [])
            test_oracle = plan.get("test_cmd", test_oracle)
            component = plan.get("component")
            repo = plan.get("repo")
            task_file = plan.get("task_file")
            allowed_scope = (
                f"component={component}; repo={repo}; task_file={task_file}"
                if component or repo or task_file
                else allowed_scope
            )

        current_architecture = self._architecture_summary(track)

        known_failed_approaches = [
            lesson.get("failure", "") for lesson in state.known_failures if lesson.get("failure")
        ]
        relevant_adrs = [
            adr.get("id", "") for adr in state.architecture_decisions if adr.get("id")
        ]

        next_milestone = state.next_milestones[0] if state.next_milestones else ""

        return CTOContext(
            product_goal=state.product_goal,
            why=state.why,
            target_user=target_user,
            current_architecture=current_architecture,
            current_milestone=state.current_milestone,
            milestone_goal=state.milestone_goal,
            milestone_definition_of_done=list(state.milestone_definition_of_done),
            dependencies=dependencies,
            known_failed_approaches=known_failed_approaches,
            relevant_adrs=relevant_adrs,
            allowed_scope=allowed_scope,
            test_oracle=test_oracle,
            next_milestone=next_milestone,
        )

    def _load_mission_plan(self, mission_id: str) -> Optional[dict]:
        plan_dir = self.cto_dir.parent / "campaigns" / "factory-vnext" / "plans"
        plan_path = plan_dir / f"{mission_id.lower()}_plan.json"
        doc = _read_json(plan_path)
        if not doc:
            return None
        missions = doc.get("missions") or []
        for m in missions:
            if isinstance(m, dict) and m.get("id") == mission_id:
                return m
        return missions[0] if missions and isinstance(missions[0], dict) else None

    @staticmethod
    def _track_for_mission(mission_id: str) -> str:
        upper = mission_id.upper()
        twitter_prefixes = ("X0", "X1", "XR", "APP007", "M00")
        if any(upper.startswith(p) for p in twitter_prefixes):
            return "twitter"
        return "factory"

    def _architecture_summary(self, track: str) -> str:
        arch = self._raw_architecture or _read_json(self.cto_dir / ARCHITECTURE_FILE) or {}
        domain = arch.get(track) or {}
        if not isinstance(domain, dict) or not domain:
            return f"architecture_version unknown for track={track}"
        lines = []
        for name, meta in sorted(domain.items()):
            if not isinstance(meta, dict):
                continue
            lines.append(f"{name} [{meta.get('status', 'UNKNOWN')}] -- {meta.get('purpose', '')}")
        return "; ".join(lines) if lines else f"no components registered for track={track}"

    # ------------------------------------------------------- assert_invariants
    def assert_invariants(self) -> None:
        """Check all hard invariants. Raise CTOInvariantViolation on violation.

        CONTEXTLESS_AGENT_DISPATCH=0: load()+validate() must not raise here --
        if they do, the invariant is already broken and the CTOContextIncomplete
        is re-raised as-is (it IS the violation). Also enforces the existing
        doctrine zero-counters recorded in CTO_STATUS_LATEST.md.
        """
        state = self.load()
        self.validate(state)

        status_text = _read_text(self.cto_dir / STATUS_LATEST_FILE)
        if not status_text:
            return

        violations = []
        for counter in _INVARIANT_COUNTER_FIELDS:
            match = re.search(rf"^-?\s*{re.escape(counter)}:\s*(-?\d+)\s*$", status_text, flags=re.MULTILINE)
            if match and int(match.group(1)) != 0:
                violations.append(f"{counter}={match.group(1)}")

        if violations:
            raise CTOInvariantViolation(
                f"Hard invariant violated in {STATUS_LATEST_FILE}: {', '.join(violations)}"
            )

    # ------------------------------------------------- reconstruct_project_state
    def reconstruct_project_state(self) -> CTOState:
        """Best-effort reconstruction from all available files. Log every gap.

        Fail-OPEN counterpart to validate(): never raises. Any required field
        that comes back empty/unknown is defaulted and logged as a CTO_GAP so a
        human/agent can see exactly what durable state failed to answer.
        """
        state = self.load()

        for name in REQUIRED_SCALAR_FIELDS:
            value = getattr(state, name, None)
            if _is_unknown(value):
                logger.warning("CTO_GAP: scalar field '%s' missing/unknown -- defaulting to 'UNKNOWN'", name)
                setattr(state, name, "UNKNOWN")

        for name in REQUIRED_LIST_FIELDS:
            value = getattr(state, name, None)
            if value is None or not isinstance(value, list):
                logger.warning("CTO_GAP: list field '%s' missing -- defaulting to []", name)
                setattr(state, name, [])

        return state
