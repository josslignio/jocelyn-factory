#!/usr/bin/env python3
"""CTOK-003-Q — Research/Reuse Gate.

Enforces the Factory invariant MAJOR_BUILD_WITHOUT_RESEARCH_REUSE_DECISION = 0:
no major capability (new subsystem, new scheduler, new persistence layer, new ML
model pipeline, ...) may be built before a ReuseDecision for it is persisted in
factory/cto/CTO_RESEARCH_REGISTRY.json (schema v2, `decisions` list).

ADAPTED from candidate/CTOK003_RESEARCH_REUSE_GATE:factory/lib/reuse_gate.py
(CTOK003_UQ's own prior art — 21/21 tests green in its own session, never merged,
not an ancestor of this branch; see PLAN.md §2.3 decision #1). Net-new relative to
that source: `check_gate_for_dispatch()`, the dispatcher-facing convenience
wrapper that CTOK003_UQ explicitly scoped out (its own decision: STANDALONE_LIB).

Semantics:
- minor capability (rename a variable, add a test, fix a bug, ...) -> non-blocking,
  ReuseGateResult(required=False, decision=None);
- major capability with an existing decision -> ReuseGateResult(required=True, decision);
- major capability with NO decision -> MissingReuseDecision raised (the gate BLOCKS).

`is_major_capability` is fail-closed: it deliberately prefers false-major over
false-minor (the cost of a false-major is registering a decision; the cost of a
false-minor is an unproven build). Explicit escape hatch: MINOR_CAPABILITY_OVERRIDES.

The registry file keeps its legacy `libraries`/`papers` sections (original keys
untouched) — CTOKernel.load() reads `libraries` directly into CTOState.reuse_decisions
(factory/lib/cto_kernel.py); canonical store for this gate is the `decisions` list.
Writes are atomic (tmp + os.replace) and idempotent by capability.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal
from factory_v2._io import _atomic_write_text, _parse_utc

DECISIONS = Literal["REUSE", "ADAPT", "WRAP", "BUILD", "REJECT"]

#: Closed decision vocabulary — anything else is rejected at register() AND at load.
DECISION_VALUES = frozenset({"REUSE", "ADAPT", "WRAP", "BUILD", "REJECT"})

#: Capabilities known to be major (seed = every capability present in the initial
#: registry v2 + the spec's historical examples). Registered capabilities are major
#: by construction (see is_major_capability).
KNOWN_MAJOR_CAPABILITIES = frozenset({
    "spec_driven_development",
    "agent_orchestration",
    "agent_trajectory_recording",
    "cto_state_persistence",
    "social_graph_engine",
    "abnormal_returns",
    "oos_validation",
    "oos_validation_challenger",
    "event_study_library",
    "research_pipeline_framework",
    "financial_sentiment_classification",
    "conductor_research_reuse_gate",
})

#: Single tokens that make a capability major (subsystem-class nouns). Matched on
#: word tokens (split on non-alphanumerics), never on raw substrings — "observer"
#: must NOT match "server".
MAJOR_TOKENS = frozenset({
    "subsystem", "scheduler", "persistence", "pipeline", "engine",
    "orchestration", "orchestrator", "orchestrating", "dispatcher",
    "framework", "kernel", "gateway", "storage", "database", "server",
    "broker", "compiler", "platform", "system", "indexer", "watchdog",
    "balancer", "registry", "repository", "warehouse", "gate",
})

#: Multi-word phrases (space-joined tokens) that make a capability major.
MAJOR_PHRASES = (
    "ml model", "ml pipeline", "model pipeline", "model training",
    "machine learning", "deep learning", "neural net", "llm pipeline",
    "data lake", "event bus", "message queue",
)

#: Explicit escape hatch for capabilities the keyword heuristic over-classifies.
MINOR_CAPABILITY_OVERRIDES = frozenset({
    # none yet — add lowercase capability names here
})

DECISIONS_KEY = "decisions"

DEFAULT_REGISTRY_PATH = Path(__file__).resolve().parents[1] / "cto" / "CTO_RESEARCH_REGISTRY.json"


@dataclass
class ReuseDecision:
    """One persisted research/reuse decision for a capability."""

    capability: str
    decision: DECISIONS
    solution: str                   # e.g. "github/spec-kit"
    reason: str
    license: str                    # e.g. "MIT"
    custom_code_required: str       # what remains to build
    decided_at: str                 # ISO8601
    evidence_links: list            # URLs or file paths
    decided_by: str                 # mission_id that produced this

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> ReuseDecision:
        capability = str(data.get("capability") or "").strip()
        if not capability:
            raise ValueError(f"ReuseDecision missing non-empty 'capability': {data!r}")
        decision = data.get("decision")
        if decision not in DECISION_VALUES:
            raise ValueError(
                f"ReuseDecision for {capability!r}: decision {decision!r} not in "
                f"closed vocabulary {sorted(DECISION_VALUES)}"
            )
        decided_at = str(data.get("decided_at") or "")
        _parse_iso8601(decided_at, context=f"decision {capability!r} decided_at")
        evidence = data.get("evidence_links", [])
        if evidence is None:
            evidence = []
        if not isinstance(evidence, list) or not all(isinstance(e, str) for e in evidence):
            raise ValueError(
                f"ReuseDecision for {capability!r}: 'evidence_links' must be a list of strings"
            )
        return cls(
            capability=capability,
            decision=decision,
            solution=str(data.get("solution") or ""),
            reason=str(data.get("reason") or ""),
            license=str(data.get("license") or ""),
            custom_code_required=str(data.get("custom_code_required") or ""),
            decided_at=decided_at,
            evidence_links=list(evidence),
            decided_by=str(data.get("decided_by") or ""),
        )


@dataclass
class ReuseGateResult:
    """Outcome of check_gate(): is the gate required, and the decision if any."""

    required: bool                        # True if capability is "major"
    decision: ReuseDecision | None     # None if research not done yet (minor only)


class MissingReuseDecision(Exception):
    """Raised by check_gate() for a MAJOR capability with no persisted decision."""

    def __init__(self, capability: str, registry_path: Path):
        self.capability = capability
        self.registry_path = Path(registry_path)
        super().__init__(
            f"MAJOR_BUILD_WITHOUT_RESEARCH_REUSE_DECISION: capability {capability!r} is major "
            f"(new subsystem / scheduler / persistence / ML pipeline class) but has no "
            f"ReuseDecision in {self.registry_path}. Research existing solutions and "
            f"register a decision (REUSE/ADAPT/WRAP/BUILD/REJECT) BEFORE building."
        )


class ReuseRegistryError(Exception):
    """Registry file exists but is unreadable/invalid — the gate fails CLOSED."""


def _parse_iso8601(value: str, context: str) -> dt.datetime:
    t = _parse_utc(value)
    if t is None:
        raise ValueError(f"{context}: not ISO8601: {value!r}")
    return t


def _tokens(capability: str) -> list:
    return [t for t in re.split(r"[^a-z0-9]+", str(capability).strip().lower()) if t]


def _norm(capability: str) -> str:
    """Canonical comparable form: 'Social Graph-Engine' -> 'social graph engine'."""
    return " ".join(_tokens(capability))


def _normalized_set(values) -> frozenset:
    return frozenset(_norm(v) for v in values)


class ResearchReuseGate:
    """Gate guarding major capabilities behind persisted research/reuse decisions."""

    def __init__(self, registry_path: Path):
        self.registry_path = Path(registry_path)

    # ------------------------------------------------------------------ major?

    def is_major_capability(self, capability: str) -> bool:
        """Return True if capability requires a gate (new subsystem, new scheduler,
        new persistence layer, new ML model pipeline, etc.). Fail-closed: prefers
        false-major over false-minor."""
        tokens = _tokens(capability)
        if not tokens:
            return False
        normalized = " ".join(tokens)
        if normalized in _normalized_set(MINOR_CAPABILITY_OVERRIDES):
            return False
        if normalized in _normalized_set(KNOWN_MAJOR_CAPABILITIES):
            return True
        # A capability that already has a registered decision was treated as major.
        if any(_norm(d.capability) == normalized for d in self.load_registry()):
            return True
        if any(token in MAJOR_TOKENS for token in tokens):
            return True
        return any(phrase in normalized for phrase in MAJOR_PHRASES)

    # -------------------------------------------------------------------- gate

    def check_gate(self, capability: str) -> ReuseGateResult:
        """Return existing decision if known; raise MissingReuseDecision if
        major + unknown. Never raises for minor capabilities."""
        decision = self._find(capability)
        if decision is not None:
            return ReuseGateResult(
                required=self.is_major_capability(capability), decision=decision
            )
        if self.is_major_capability(capability):
            raise MissingReuseDecision(capability, self.registry_path)
        return ReuseGateResult(required=False, decision=None)

    # ---------------------------------------------------------------- registry

    def register(self, decision: ReuseDecision) -> None:
        """Persist decision to registry_path (atomic write). Idempotent by
        capability: an existing entry for the same capability is REPLACED in place
        (never duplicated). Unknown top-level keys (legacy `libraries`, `papers`)
        are preserved untouched."""
        # Validate first — an invalid decision must never touch the file on disk.
        ReuseDecision.from_dict(decision.to_dict())
        entries = [d.to_dict() for d in self.load_registry()]
        payload = decision.to_dict()
        normalized = _norm(decision.capability)
        if not normalized:
            raise ValueError("cannot register a decision with an empty capability")
        replaced = False
        for i, existing in enumerate(entries):
            if _norm(existing.get("capability", "")) == normalized:
                entries[i] = payload
                replaced = True
                break
        if not replaced:
            entries.append(payload)

        doc = self._read_document()
        doc[DECISIONS_KEY] = entries
        doc["last_updated"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._atomic_write(doc)

    def load_registry(self) -> list:
        """Read all decisions from CTO_RESEARCH_REGISTRY.json ([] if file absent;
        ReuseRegistryError if present but invalid — fail CLOSED, never silently
        pass a corrupted gate)."""
        path = self.registry_path
        if not path.exists():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ReuseRegistryError(f"{path}: invalid JSON: {exc}") from exc
        except OSError as exc:
            # L6_bin_lib_products_2-003: an unreadable registry (a directory at
            # the registry path, permission error, ...) used to escape as a raw
            # OSError although the dispatch wrapper's contract is Never raises.
            raise ReuseRegistryError(f"{path}: unreadable registry: {exc}") from exc
        if not isinstance(raw, dict):
            raise ReuseRegistryError(f"{path}: top level must be a JSON object, got {type(raw).__name__}")
        entries = raw.get(DECISIONS_KEY, [])
        if entries is None:
            entries = []
        if not isinstance(entries, list):
            raise ReuseRegistryError(f"{path}: '{DECISIONS_KEY}' must be a list")
        decisions = []
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                # L6_bin_lib_products_2-003: a null/non-object entry crashed
                # from_dict with AttributeError — a schema error is a closed
                # registry error, never an uncontrolled exception.
                raise ReuseRegistryError(
                    f"{path}: decisions[{i}]: expected an object, got "
                    f"{type(entry).__name__}")
            try:
                decisions.append(ReuseDecision.from_dict(entry))
            except ValueError as exc:
                raise ReuseRegistryError(f"{path}: decisions[{i}]: {exc}") from exc
        return decisions

    # ----------------------------------------------------------------- private

    def _find(self, capability: str) -> ReuseDecision | None:
        normalized = _norm(capability)
        if not normalized:
            return None
        for d in self.load_registry():
            if _norm(d.capability) == normalized:
                return d
        return None

    def _read_document(self) -> dict:
        path = self.registry_path
        if not path.exists():
            return {"schema": "cto-research-registry/v2"}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ReuseRegistryError(f"{path}: invalid JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise ReuseRegistryError(f"{path}: top level must be a JSON object")
        return raw

    def _atomic_write(self, doc: dict) -> None:
        _atomic_write_text(self.registry_path, json.dumps(doc, indent=2, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- dispatch API

def check_gate_for_dispatch(repo_root: Path, capability: str | None) -> str | None:
    """Dispatcher-facing convenience wrapper (net-new vs. CTOK003_UQ's
    STANDALONE_LIB scope): evaluate the gate for `capability` against
    `repo_root`'s registry and return a blocked_reason string, or None to proceed.

    Never raises: ReuseRegistryError (corrupt registry) is fail-CLOSED and also
    returned as a blocked_reason — a broken registry must never silently open the
    gate. An empty/None capability always returns None (nothing declared to gate).
    """
    if not capability:
        return None
    registry_path = Path(repo_root) / "factory" / "cto" / "CTO_RESEARCH_REGISTRY.json"
    gate = ResearchReuseGate(registry_path)
    try:
        gate.check_gate(capability)
    except MissingReuseDecision as exc:
        return f"MISSING_RESEARCH_REUSE_DECISION: {exc}"
    except ReuseRegistryError as exc:
        return f"RESEARCH_REUSE_REGISTRY_INVALID: {exc}"
    return None
