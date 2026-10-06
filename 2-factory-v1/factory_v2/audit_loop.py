"""Phase 7: Minimal smart audit/research loop — event-triggered multi-AI counter-audit.

Do not build a huge framework before Twitter Radar. Implement the minimum.

Trigger counter-audit on:
    - repeated equivalent failure after two different strategies
    - SAFETY_STOP
    - promotion/SHA/lease/fencing anomaly
    - roadmap/spec/strategy mutation
    - critical concurrency change
    - unexpected large diff
    - before champion promotion

Evidence bundle:
    Freeze one exact-SHA EvidenceBundle (SHA-addressed, immutable).
    Give the same evidence independently to:
        Claude: architecture/product-intent review
        Z/GLM: implementation/simplicity/performance challenge
        GPT/Codex: adversarial correctness/durability review
    First passes are blind (no anchoring).
    AI conclusions are evidence only — never override deterministic gates.

Research triggers (targeted, not speculative):
    - repeated failure
    - concurrency/durability problem
    - novel expensive subsystem
    - unclear external API
    - likely solved upstream architecture

Research decision types:
    KEEP | REUSE | ADAPT | WRAP | REPLACE | BUILD | DELETE | REJECT_EXTERNAL

Records: source URL, version/commit, license, provenance.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Literal
from factory_v2._io import _atomic_write_text, _now, _now as _utcnow


# ── trigger classification ─────────────────────────────────────────────────────

class AuditTrigger(Enum):
    REPEATED_EQUIVALENT_FAILURE = "REPEATED_EQUIVALENT_FAILURE"
    SAFETY_STOP = "SAFETY_STOP"
    PROMOTION_ANOMALY = "PROMOTION_ANOMALY"
    SHA_ANOMALY = "SHA_ANOMALY"
    LEASE_FENCING_ANOMALY = "LEASE_FENCING_ANOMALY"
    AUTHORITY_MUTATION = "AUTHORITY_MUTATION"
    CRITICAL_CONCURRENCY_CHANGE = "CRITICAL_CONCURRENCY_CHANGE"
    UNEXPECTED_LARGE_DIFF = "UNEXPECTED_LARGE_DIFF"
    PRE_CHAMPION_PROMOTION = "PRE_CHAMPION_PROMOTION"


class ResearchTrigger(Enum):
    REPEATED_FAILURE = "REPEATED_FAILURE"
    CONCURRENCY_DURABILITY = "CONCURRENCY_DURABILITY"
    NOVEL_EXPENSIVE_SUBSYSTEM = "NOVEL_EXPENSIVE_SUBSYSTEM"
    UNCLEAR_EXTERNAL_API = "UNCLEAR_EXTERNAL_API"
    LIKELY_SOLVED_UPSTREAM = "LIKELY_SOLVED_UPSTREAM"


class ResearchDecision(Enum):
    KEEP = "KEEP"
    REUSE = "REUSE"
    ADAPT = "ADAPT"
    WRAP = "WRAP"
    REPLACE = "REPLACE"
    BUILD = "BUILD"
    DELETE = "DELETE"
    REJECT_EXTERNAL = "REJECT_EXTERNAL"


# ── EvidenceBundle ─────────────────────────────────────────────────────────────

@dataclass
class EvidenceBundle:
    """Immutable, SHA-addressed evidence bundle for multi-AI counter-audit.

    Frozen at a specific git SHA. Each AI reviewer receives the same bundle,
    independent of others (no anchoring on first pass).
    """
    bundle_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    frozen_at: str = ""
    git_sha: str = ""
    trigger: str = ""
    milestone_id: str = ""
    mission_id: str = ""
    failure_fingerprint: str = ""
    failure_count: int = 0
    strategies_attempted: list = field(default_factory=list)
    authority_hashes: dict = field(default_factory=dict)
    evidence_files: list = field(default_factory=list)  # list of {path, sha16, content_snippet}
    bundle_sha: str = ""  # sha256 of all fields (content-addressed)

    def compute_bundle_sha(self) -> str:
        payload = {k: v for k, v in asdict(self).items() if k != "bundle_sha"}
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:24]

    def finalize(self) -> "EvidenceBundle":
        self.bundle_sha = self.compute_bundle_sha()
        return self

    def verify_integrity(self) -> bool:
        return self.compute_bundle_sha() == self.bundle_sha


@dataclass
class AuditFinding:
    """One finding from a single AI reviewer — evidence only, not authoritative."""
    finding_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    bundle_id: str = ""
    reviewer: str = ""  # "claude" | "z_glm" | "codex_gpt"
    review_lens: str = ""  # "architecture" | "implementation" | "adversarial"
    produced_at: str = ""
    severity: Literal["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"] = "INFO"
    category: str = ""
    description: str = ""
    evidence: str = ""
    recommendation: str = ""
    confidence: float = 0.0  # 0.0-1.0
    is_blocking: bool = False
    # AI conclusions are evidence only — never override deterministic gates.
    authority: str = "EVIDENCE_ONLY"


@dataclass
class CounterAuditRecord:
    """Record of a triggered counter-audit and its findings."""
    audit_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    triggered_at: str = ""
    trigger: str = ""
    milestone_id: str = ""
    bundle_id: str = ""
    bundle_sha: str = ""
    reviewers: list = field(default_factory=list)  # ["claude", "z_glm", "codex_gpt"]
    findings: list = field(default_factory=list)  # list[AuditFinding]
    consensus: str = ""  # "PROCEED" | "HALT" | "REPAIR_REQUIRED" | "INCONCLUSIVE"
    deterministic_gate_outcome: str = ""  # overrides consensus if set
    completed_at: str = ""


@dataclass
class ResearchRecord:
    """Record of a targeted research decision."""
    research_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    triggered_at: str = ""
    trigger: str = ""
    question: str = ""
    search_order: list = field(default_factory=lambda: [
        "local_repo", "git_history", "research_registry",
        "official_docs", "upstream_source",
    ])
    decision: str = ""  # ResearchDecision.value
    source_url: str = ""
    source_version: str = ""
    source_license: str = ""
    source_provenance: str = ""
    rationale: str = ""
    recorded_at: str = ""


# ── trigger detection ──────────────────────────────────────────────────────────

def should_trigger_audit(
    failure_fingerprint: str,
    failure_count: int,
    strategies_attempted: list,
    last_event_type: str = "",
    diff_size_lines: int = 0,
    is_pre_champion: bool = False,
) -> AuditTrigger | None:
    """Determine if a counter-audit should be triggered. Returns trigger or None."""
    if last_event_type in ("SAFETY_STOP", "SAFETY_QUARANTINE"):
        return AuditTrigger.SAFETY_STOP
    if last_event_type in ("CAS_CONFLICT", "SHA_MISMATCH", "PROMOTED_SHA_MISMATCH"):
        return AuditTrigger.SHA_ANOMALY
    if last_event_type in ("LEASE_VIOLATION", "STALE_GENERATION_WRITE", "FENCING_ANOMALY"):
        return AuditTrigger.LEASE_FENCING_ANOMALY
    if last_event_type in ("REPLAN_PATCH_APPLIED", "AUTHORITY_CHANGE_APPLIED"):
        return AuditTrigger.AUTHORITY_MUTATION
    if is_pre_champion:
        return AuditTrigger.PRE_CHAMPION_PROMOTION
    if failure_count >= 2 and len(strategies_attempted) >= 2:
        return AuditTrigger.REPEATED_EQUIVALENT_FAILURE
    if diff_size_lines > 500:
        return AuditTrigger.UNEXPECTED_LARGE_DIFF
    return None


def should_trigger_research(
    failure_count: int,
    failure_fingerprint: str,
    last_event_type: str = "",
    is_novel_subsystem: bool = False,
    has_external_api: bool = False,
) -> ResearchTrigger | None:
    """Determine if targeted research is warranted. Returns trigger or None."""
    if failure_count >= 2:
        return ResearchTrigger.REPEATED_FAILURE
    if last_event_type in ("LEASE_VIOLATION", "CAS_CONFLICT", "DUPLICATE_DISPATCH"):
        return ResearchTrigger.CONCURRENCY_DURABILITY
    if is_novel_subsystem:
        return ResearchTrigger.NOVEL_EXPENSIVE_SUBSYSTEM
    if has_external_api:
        return ResearchTrigger.UNCLEAR_EXTERNAL_API
    return None


# ── evidence bundle builder ────────────────────────────────────────────────────

def freeze_evidence_bundle(
    trigger: AuditTrigger,
    milestone_id: str,
    project_dir: Path,
    git_sha: str = "",
    mission_id: str = "",
    failure_fingerprint: str = "",
    failure_count: int = 0,
    strategies_attempted: list | None = None,
    evidence_file_paths: list[Path] | None = None,
) -> EvidenceBundle:
    """Freeze an immutable EvidenceBundle at the current git SHA."""
    from .authority_change import _authority_hashes
    project_dir = Path(project_dir)
    authority_hashes = {}
    try:
        authority_hashes = _authority_hashes(project_dir)
    except Exception:
        pass

    evidence_files = []
    for fp in (evidence_file_paths or []):
        try:
            content = fp.read_text(encoding="utf-8")
            evidence_files.append({
                "path": str(fp),
                "sha16": hashlib.sha256(content.encode()).hexdigest()[:16],
                "content_snippet": content[:500],
            })
        except Exception:
            pass

    bundle = EvidenceBundle(
        frozen_at=_now(),
        git_sha=git_sha,
        trigger=trigger.value,
        milestone_id=milestone_id,
        mission_id=mission_id,
        failure_fingerprint=failure_fingerprint,
        failure_count=failure_count,
        strategies_attempted=strategies_attempted or [],
        authority_hashes=authority_hashes,
        evidence_files=evidence_files,
    )
    return bundle.finalize()


# ── persistence ────────────────────────────────────────────────────────────────

def persist_audit_record(record: CounterAuditRecord, project_dir: Path) -> Path:
    """Write CounterAuditRecord to evidence/audits/<audit_id>.json."""
    audit_dir = Path(project_dir) / "evidence" / "audits"
    audit_dir.mkdir(parents=True, exist_ok=True)
    path = audit_dir / f"{record.audit_id}.json"
    data = asdict(record)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


def persist_research_record(record: ResearchRecord, project_dir: Path) -> Path:
    """Write ResearchRecord to evidence/research/<research_id>.json."""
    research_dir = Path(project_dir) / "evidence" / "research"
    research_dir.mkdir(parents=True, exist_ok=True)
    path = research_dir / f"{record.research_id}.json"
    data = asdict(record)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


def load_audit_records(project_dir: Path) -> list[CounterAuditRecord]:
    """Load all audit records for a project."""
    audit_dir = Path(project_dir) / "evidence" / "audits"
    records = []
    if not audit_dir.exists():
        return records
    for f in sorted(audit_dir.glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            records.append(CounterAuditRecord(**{k: v for k, v in d.items()
                                                  if k in CounterAuditRecord.__dataclass_fields__}))
        except Exception:
            pass
    return records


# ── D018/D019 multi-AI CTO cell (AUTOMATED_MULTI_AI_CTO_LOOP_V1) ──────────────
#
# Contract 0a08a045b599b62ea796de35: trigger -> EvidenceBundle freeze ->
# challenger (read-only, isolated) -> reviewer (exact-SHA, single-flight D29) ->
# deterministic synthesis (CLAIM->EVIDENCE->COUNTEREXAMPLE->DECISION->NEXT
# EXPERIMENT) -> writer redirect -> exact-position resume. Max two adversarial
# rounds; typed work packages; non-transitive fail-closed capabilities.

CELL_SCHEMA = "audit-cell-record/v1"
MAX_ADVERSARIAL_ROUNDS = 2
DEFAULT_ENGINE_ATTEMPTS = 8  # wired per-engine quota (SOL-R1 PROVIDER_EXHAUSTED)
REVIEWER_VERDICTS = frozenset({"ACCEPT", "REJECT", "REDIRECT", "QUARANTINE"})

# Default per-engine ceiling for production model reviews when the caller
# passes no RoleEngineBudget (P0 quota: unbounded reviewer launches).
_DEFAULT_MODEL_REVIEWER_ATTEMPTS = 6

_FORBIDDEN_KEY_FRAGMENTS = (
    "credential", "secret", "password", "passwd", "api_key", "apikey",
    "token", "private_key",
)


class CapabilityDenial(Exception):
    """Typed fail-closed denial of an out-of-envelope capability (D018)."""


class CredentialLeakRefused(Exception):
    """A work package may never carry credential-bearing keys (D018 A8)."""


class ReviewerInvalidSchema(Exception):
    """Typed terminal reviewer failure: malformed/unbound verdict (D029 shape)."""


class BudgetExceeded(Exception):
    """A cell action exceeded its declared bounded envelope (A9)."""


class ProviderExhausted(Exception):
    """Typed quota/provider exhaustion for a role engine (SOL-R1).

    Exhaustion is never a silent drop and never an owner question: it yields a
    durable typed transition with a bounded exponential backoff schedule and
    the loop continues other safe work.
    """


class PositionDrift(Exception):
    """Product position fingerprint changed across the cell activation (A6)."""


class ContractBindingFailure(Exception):
    """Typed fail-closed refusal: a role dispatch was attempted without a
    frozen contract bound BY ID with signature + authority verification
    (Sol REJECT@f253f439 F3 — the binding happens BEFORE any spawn)."""


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _sha16(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _scan_credential_keys(payload: dict) -> None:
    """Fail closed if any credential-shaped key appears anywhere in the package."""
    def _walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                kl = str(k).lower()
                if any(frag in kl for frag in _FORBIDDEN_KEY_FRAGMENTS):
                    raise CredentialLeakRefused(
                        f"credential-shaped key refused in work package at {path}.{k} "
                        "(CREDENTIAL_PROPAGATION_BETWEEN_AGENTS=0)")
                _walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                _walk(v, f"{path}[{i}]")
    _walk(payload, "$")


def scrub_env(env: dict | None = None) -> dict:
    """Return a copy of the environment with credential-shaped vars removed.

    Roles never inherit credentials from the controller: propagation is
    structurally impossible, not merely discouraged (D018 A8).
    """
    src = dict(os.environ if env is None else env)
    return {k: v for k, v in src.items()
            if not any(frag in k.lower() for frag in _FORBIDDEN_KEY_FRAGMENTS)}


def bind_frozen_contract(project_dir: Path, contract_id: str) -> dict:
    """Load the frozen contract BY ID through the production loader and verify
    signature + current authority (Sol REJECT@f253f439 F3 / D032 s2-F1).

    Fail closed on ANY error: absent file, id mismatch, signature drift or
    authority drift refuse the dispatch BEFORE any role is spawned. Returns
    the verified contract.
    """
    from .validation_contract import (
        load_frozen_contract, verify_frozen_against_current, contract_signature)
    from .project_authority import load_authority
    if not contract_id:
        raise ContractBindingFailure(
            "dispatch refused: work package carries no contract_id (unbound "
            "dispatch — Sol F3)")
    try:
        contract = load_frozen_contract(Path(project_dir), contract_id)
    except Exception as exc:
        raise ContractBindingFailure(
            f"contract {contract_id!r} not loadable by id through the "
            f"production loader: {exc}") from exc
    if contract.get("contract_id") != contract_id:
        raise ContractBindingFailure(
            f"loaded contract id {contract.get('contract_id')!r} != {contract_id!r}")
    if contract["body"].get("contract_signature") != \
            contract_signature(contract["body"]):
        raise ContractBindingFailure(
            f"contract {contract_id} signature does not recompute — frozen "
            f"semantics tampered")
    drift = verify_frozen_against_current(
        contract, load_authority(Path(project_dir)))
    if drift:
        raise ContractBindingFailure(
            f"contract {contract_id} drifted from current authority: {drift}")
    return contract


@dataclass
class CapabilityEnvelope:
    """Non-transitive, least-privilege, fail-closed capability set (D018).

    Unknown tools, domains, write targets and network use are DENIED — there is
    no wildcard: presence on the exact allowlist is the only grant. A capability
    object never delegates itself; a receiving role reconstructs its envelope
    only from the controller-authored package, never from peer text.
    """
    allowed_tools: frozenset = field(default_factory=frozenset)
    allowed_read: frozenset = field(default_factory=frozenset)
    allowed_write: frozenset = field(default_factory=frozenset)
    allow_network: bool = False

    def check(self, *, tool: str = "", reads: tuple = (), writes: tuple = (),
              network: bool = False) -> None:
        if tool and tool not in self.allowed_tools:
            raise CapabilityDenial(
                f"tool {tool!r} not on the exact allowlist (fail closed)")
        for r in reads:
            if str(r) not in self.allowed_read:
                raise CapabilityDenial(f"read {str(r)!r} not on the allowlist")
        for w in writes:
            if str(w) not in self.allowed_write:
                raise CapabilityDenial(
                    f"write {str(w)!r} not on the allowlist "
                    f"(read-only roles allow no writes)")
        if network and not self.allow_network:
            raise CapabilityDenial("network egress not granted")

    def as_dict(self) -> dict:
        return {
            "allowed_tools": sorted(self.allowed_tools),
            "allowed_read": sorted(self.allowed_read),
            "allowed_write": sorted(self.allowed_write),
            "allow_network": self.allow_network,
        }


@dataclass
class WorkPackage:
    """Typed multi-AI work package (D018 A7): identity, frozen authority
    hashes, allowed inputs/tools, budget, deadline, expected output —
    content-addressed with producer identity and provenance."""
    package_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    role: str = ""                  # "challenger" | "reviewer"
    bundle_id: str = ""
    bundle_sha: str = ""
    authority_hashes: dict = field(default_factory=dict)
    candidate_sha: str = ""         # reviewer packages bind an exact SHA
    contract_id: str = ""           # frozen contract bound BEFORE dispatch (Sol F3)
    created_at: str = field(default_factory=_utcnow)
    deadline_ts: str = ""
    budget_s: float = 0.0
    expected_output: dict = field(default_factory=dict)
    allowed_inputs: list = field(default_factory=list)   # [{path, sha16}]
    capability: dict = field(default_factory=dict)       # CapabilityEnvelope.as_dict
    producer: str = "controller"
    package_sha: str = ""

    def finalize(self) -> "WorkPackage":
        payload = {k: v for k, v in asdict(self).items() if k != "package_sha"}
        _scan_credential_keys(payload)
        if not self.role or self.role not in ("challenger", "reviewer"):
            raise ValueError("typed work package requires role challenger|reviewer")
        if not self.bundle_sha:
            raise ValueError("typed work package requires the frozen bundle_sha")
        if self.role == "reviewer" and not self.candidate_sha:
            raise ValueError("reviewer packages bind an exact candidate SHA")
        if self.budget_s <= 0:
            raise ValueError("bounded budget_s is mandatory (A9)")
        self.package_sha = hashlib.sha256(_canon(payload).encode()).hexdigest()[:24]
        return self

    def verify_integrity(self) -> bool:
        payload = {k: v for k, v in asdict(self).items() if k != "package_sha"}
        try:
            _scan_credential_keys(payload)
        except CredentialLeakRefused:
            return False
        return (hashlib.sha256(_canon(payload).encode()).hexdigest()[:24]
                == self.package_sha)

    def to_envelope(self) -> CapabilityEnvelope:
        c = self.capability or {}
        return CapabilityEnvelope(
            allowed_tools=frozenset(c.get("allowed_tools", [])),
            allowed_read=frozenset(c.get("allowed_read", [])),
            allowed_write=frozenset(c.get("allowed_write", [])),
            allow_network=bool(c.get("allow_network", False)),
        )


def capture_position(project_state_path: Path) -> str:
    """Position fingerprint of the product (A6): canonical digest of the
    roadmap position — project, current milestone, settled count, head SHA."""
    p = Path(project_state_path)
    d = json.loads(p.read_text(encoding="utf-8"))
    pos = {
        "project": d.get("project"),
        "current_milestone": d.get("current_milestone"),
        "settled": sorted((d.get("completed_milestones") or {}).keys()),
        "interrupted": d.get("interrupted_work"),
    }
    return _sha16(_canon(pos))


def _tracked_tree_digest(repo: Path) -> str:
    """Digest over tracked files only — runtime state must not mask a
    challenger write, and runtime noise must not fake one."""
    repo = Path(repo)
    try:
        names = subprocess_run(
            ["git", "-C", str(repo), "ls-files", "-z"]).split("\0")
        names = [n for n in names if n]
        h = hashlib.sha256()
        for n in sorted(names):
            fp = repo / n
            try:
                h.update(n.encode())
                h.update(fp.read_bytes())
            except FileNotFoundError:
                h.update(n.encode())
        return h.hexdigest()[:16]
    except Exception:
        return ""


def subprocess_run(argv: list, timeout_s: float = 60.0,
                   cwd: str | None = None, env: dict | None = None) -> str:
    import subprocess
    r = subprocess.run(argv, capture_output=True, text=True,
                       timeout=timeout_s, cwd=cwd, env=env)
    if r.returncode != 0:
        raise RuntimeError(
            f"command {argv[:3]}... rc={r.returncode}: {r.stderr[-300:]}")
    return r.stdout


def _launch_role(argv: list, cwd: str, env: dict, timeout_s: float) -> tuple[dict, str]:
    """Launch a REAL role subprocess; return (identity, stdout).

    Identity records the observed launched argv, pid, pgid and birth so the
    verifier can recompute that a real process ran (never an authored boolean).
    """
    import subprocess
    p = subprocess.Popen(argv, cwd=cwd, env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, start_new_session=True)
    birth = ""
    try:
        b = subprocess.run(["ps", "-o", "lstart=", "-p", str(p.pid)],
                           capture_output=True, text=True, timeout=10)
        if b.returncode == 0:
            birth = b.stdout.strip()
    except Exception:
        pass
    try:
        out, err = p.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate()
        raise BudgetExceeded(
            f"role subprocess exceeded its {timeout_s}s envelope (A9)")
    if p.returncode != 0:
        raise RuntimeError(
            f"role subprocess rc={p.returncode}: {(err or out)[-300:]}")
    identity = {"argv": list(argv), "pid": p.pid, "pgid": p.pid,
                "birth": birth, "cwd": cwd}
    return identity, out or ""


def _role_script(repo: Path, name: str) -> str:
    p = Path(repo) / "factory_v2" / name
    if not p.exists():
        raise RuntimeError(f"role runner {p} absent from the control tree")
    return str(p)


def run_challenger(package: WorkPackage, repo: Path,
                   control_root: Path | None = None,
                   python: str | None = None,
                   engine_budget: "RoleEngineBudget | None" = None,
                   project_dir: Path | None = None) -> dict:
    """Challenger role: READ-ONLY, isolated copy, zero attributable writes.

    Executes a real subprocess in a fresh mkdtemp sandbox with a scrubbed
    environment; its only write target is inside the sandbox. The product
    repo's tracked-tree digest is taken before and after — equality is the
    zero-writes proof, inequality fails closed (A2).

    engine_budget (SOL-R1): when wired, the launch is gated by the durable
    per-engine attempt ledger — check() raises the typed ProviderExhausted
    BEFORE any launch when the observed allowance is spent, and charge()
    records each REAL launch identity. Exhaustion is an observed runtime
    condition of the ledger, never a caller-authored flag.

    Contract binding (Sol F3): the frozen contract is loaded BY ID, its
    signature recomputed and its authority verified BEFORE any spawn — an
    unbound or drifted package refuses the dispatch, fail closed.
    """
    envelope = package.to_envelope()
    envelope.check(tool="python3", reads=tuple(
        i["path"] for i in package.allowed_inputs), writes=())
    contract = bind_frozen_contract(project_dir, package.contract_id)
    if engine_budget is not None:
        engine_budget.check()
    script = _role_script(control_root if control_root is not None else repo,
                          "cell_challenger.py")
    repo_digest_before = _tracked_tree_digest(repo)
    sandbox = Path(tempfile.mkdtemp(prefix="factory-cell-challenger-"))
    try:
        pkg_path = sandbox / "package.json"
        pkg_path.write_text(json.dumps(asdict(package), indent=1), encoding="utf-8")
        out_path = sandbox / "findings.json"
        argv = [python or sys.executable, "-I", script,
                "--package", str(pkg_path), "--out", str(out_path)]
        started = time.time()
        identity, stdout = _launch_role(argv, str(sandbox), scrub_env(),
                                        package.budget_s)
        if engine_budget is not None:
            engine_budget.charge(identity)
        record_role_launch(control_root if control_root is not None else repo,
                           role="challenger", identity=identity,
                           contract_id=package.contract_id)
        if not out_path.exists():
            raise ReviewerInvalidSchema(
                "challenger produced no findings artifact (typed terminal)")
        raw = out_path.read_text(encoding="utf-8")
        findings = json.loads(raw)
        repo_digest_after = _tracked_tree_digest(repo)
        if repo_digest_before != repo_digest_after or not repo_digest_before:
            raise PositionDrift(
                "challenger attributable write detected: tracked-tree digest "
                f"changed {repo_digest_before}->{repo_digest_after} (A2 fail closed)")
        return {
            "role": "challenger",
            "package_sha": package.package_sha,
            "contract_id": package.contract_id,
            "contract_signature": contract["body"]["contract_signature"],
            "identity": identity,
            "findings": findings,
            "findings_sha16": _sha16(raw),
            "repo_digest_before": repo_digest_before,
            "repo_digest_after": repo_digest_after,
            "sandbox": str(sandbox),
            "wall_s": round(time.time() - started, 2),
        }
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def run_reviewer(package: WorkPackage, control_root: Path,
                 python: str | None = None,
                 engine_budget: "RoleEngineBudget | None" = None,
                 project_dir: Path | None = None) -> dict:
    """Reviewer role: exact-SHA bound verdict under D29 single-flight.

    One identical live review per (candidate_sha, package) identity: later
    callers ATTACH or REUSE, never duplicate. The verdict must be typed and
    bound to the exact candidate SHA; anything else is the typed terminal
    REVIEWER_INVALID_SCHEMA — never ACCEPT, never REJECT (A3).

    engine_budget (SOL-R1): checked BEFORE acquire (an exhausted engine may
    not even queue a job) and charged ONLY on a real OWNED launch — reuse
    of a settled verdict consumes no provider quota.

    Contract binding (Sol F3): verified BEFORE acquire — an unbound or
    drifted package may not even queue a job, and never spawns a role.
    """
    from .single_flight import acquire, mark_running, settle, classify
    envelope = package.to_envelope()
    envelope.check(tool="python3",
                   reads=tuple(i["path"] for i in package.allowed_inputs),
                   writes=())
    contract = bind_frozen_contract(project_dir, package.contract_id)
    if engine_budget is not None:
        engine_budget.check()
    key = validation_key_for_review(package)
    script = _role_script(control_root, "cell_reviewer.py")
    argv = [python or sys.executable, "-I", script,
            "--package", "<isolated-copy>/package.json",
            "--out", "<isolated-copy>/verdict.json"]
    slot = acquire(control_root, key, command=argv,
                   cwd=str(control_root), envelope_s=package.budget_s,
                   owner_note=f"cell-reviewer:{package.package_id}")
    if not slot.get("owned"):
        # D29 single-flight: an equivalent review is live or already settled —
        # attach/reuse, never launch a duplicate.
        job = slot["job"]
        if slot.get("reuse") and (job.get("result") or {}).get("classified") == "PASS":
            verdict = json.loads(
                Path(job["result"]["evidence_path"]).read_text(encoding="utf-8"))
            return {"role": "reviewer", "package_sha": package.package_sha,
                    "contract_id": package.contract_id,
                    "contract_signature": contract["body"]["contract_signature"],
                    "single_flight": "REUSED", "job_key": key,
                    "identity": {"argv": job.get("command"),
                                 "pid": job.get("pid"),
                                 "birth": job.get("birth")},
                    "verdict": verdict}
        return {"role": "reviewer", "package_sha": package.package_sha,
                "contract_id": package.contract_id,
                "single_flight": "ATTACHED", "job_key": key,
                "identity": {"argv": job.get("command"),
                             "pid": job.get("pid"),
                             "birth": job.get("birth")},
                "verdict": None, "pending": True}
    sandbox = Path(tempfile.mkdtemp(prefix="factory-cell-reviewer-"))
    owned_generation = slot["job"].get("generation")
    try:
        pkg_path = sandbox / "package.json"
        pkg_path.write_text(json.dumps(asdict(package), indent=1), encoding="utf-8")
        out_path = sandbox / "verdict.json"
        argv = [python or sys.executable, "-I", script,
                "--package", str(pkg_path), "--out", str(out_path)]
        started = time.time()
        # mark the durable job RUNNING with the real child identity
        import subprocess
        proc = subprocess.Popen(argv, cwd=str(sandbox), env=scrub_env(),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)
        mark_running(control_root, key, proc.pid, proc.pid)
        if engine_budget is not None:
            engine_budget.charge({"argv": list(argv), "pid": proc.pid})
        record_role_launch(control_root, role="reviewer",
                           identity={"argv": list(argv), "pid": proc.pid,
                                     "birth": ""},
                           contract_id=package.contract_id)
        try:
            out, err = proc.communicate(timeout=package.budget_s)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            settle(control_root, key, rc=124, classified="HARNESS_TIMED_OUT",
                   duration_s=time.time() - started,
                   generation=owned_generation)
            raise BudgetExceeded("reviewer exceeded its bounded envelope (A9)")
        rc = proc.returncode
        wall = round(time.time() - started, 2)
        if rc != 0 or not out_path.exists():
            classified = classify(rc, envelope_expired=False, child_alive=False)
            settle(control_root, key, rc=rc, classified=classified,
                   duration_s=wall, generation=owned_generation)
            raise ReviewerInvalidSchema(
                f"reviewer subprocess rc={rc}: {(err or out)[-200:]} "
                f"(typed terminal, not ACCEPT/REJECT)")
        raw = out_path.read_text(encoding="utf-8")
        verdict = json.loads(raw)
        bound = verdict.get("reviewed_sha", "")
        if (verdict.get("verdict") not in REVIEWER_VERDICTS
                or bound != package.candidate_sha):
            settle(control_root, key, rc=1, classified="FAIL", duration_s=wall,
                   generation=owned_generation)
            raise ReviewerInvalidSchema(
                f"verdict {verdict.get('verdict')!r} bound to {bound[:8]!r} != "
                f"candidate {package.candidate_sha[:8]!r} (typed terminal)")
        # persist the verdict artifact durably BEFORE settling the job so the
        # single-flight reuse path points at a durable file, never the sandbox
        keep = control_root / "factory_v2" / "receipts" / "cell_reviews"
        keep.mkdir(parents=True, exist_ok=True)
        dst = keep / f"{key}.json"
        _atomic_write_text(dst, raw)
        settle(control_root, key, rc=0, classified="PASS", duration_s=wall,
               evidence_path=str(dst), generation=owned_generation)
        birth = ""
        try:
            b = subprocess.run(["ps", "-o", "lstart=", "-p", str(proc.pid)],
                               capture_output=True, text=True, timeout=10)
            birth = b.stdout.strip() if b.returncode == 0 else ""
        except Exception:
            pass
        return {"role": "reviewer", "package_sha": package.package_sha,
                "contract_id": package.contract_id,
                "contract_signature": contract["body"]["contract_signature"],
                "single_flight": "OWNED", "job_key": key,
                "identity": {"argv": list(argv), "pid": proc.pid,
                             "pgid": proc.pid, "birth": birth,
                             "cwd": str(sandbox)},
                "verdict": verdict, "verdict_sha16": _sha16(raw),
                "wall_s": wall}
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def _extract_verdict_json(text: str, expected_sha: str) -> "dict | None":
    """Best-effort extraction of a typed reviewer verdict from a real model's
    final text. A live model's output is prose with a JSON tail, never the
    single clean stdout object the deterministic cell_reviewer.py script
    emits — so this scans for the LAST well-formed `{...}` object that
    carries a `verdict` key, preferring one already bound to `expected_sha`
    when more than one candidate is present."""
    import re
    candidates = []
    for m in re.finditer(r"\{[^{}]*\}", text or ""):
        try:
            obj = json.loads(m.group(0))
        except Exception:
            continue
        if isinstance(obj, dict) and "verdict" in obj:
            candidates.append(obj)
    if not candidates:
        return None
    for obj in reversed(candidates):
        if obj.get("reviewed_sha") == expected_sha:
            return obj
    return candidates[-1]


def run_model_reviewer(package: WorkPackage, control_root: Path, *,
                       repo: Path, spec_level: str, project: "str | None" = None,
                       python: str | None = None,
                       engine_budget: "RoleEngineBudget | None" = None,
                       project_dir: Path | None = None) -> dict:
    """Reviewer role via a REAL read-only model subprocess (PRODUCTION ONLY).

    Same single-flight (D29 exactly-one) / contract-binding (Sol F3) / budget
    contract as `run_reviewer`, but the process launched is whatever
    `model_router.route(spec_level, "review", project=project)` selects —
    codex by seed policy, or GLM/Claude read-only under an active owner
    routing override — instead of the deterministic `cell_reviewer.py`
    structural check. `route()` returning a WRITABLE engine for a review task
    is a routing bug, refused here rather than spawned (A3: a reviewer never
    holds write authority). The subprocess's final text is scanned for a
    typed verdict JSON object bound to the exact `candidate_sha`; anything
    else is the typed terminal `ReviewerInvalidSchema` (never a guessed
    ACCEPT/REJECT).

    `audit_cell_exercise.py` and every hermetic test call `run_audit_cell`
    with its default `reviewer_fn=run_reviewer` and never reach this
    function — only the production caller
    (`runtime.drain_pending_audit_cell`) injects it, so every verifier
    re-run and every test stays deterministic and free of model spend.
    """
    import os as _os
    import sys as _sys
    # Hermetic guard (P0 quota, incident 2026-09-10): this is the ONLY other
    # production site that spawns a real model CLI besides AutopilotExecutor.
    # The executor guard alone let a "hermetic" harness reach this function and
    # pay real subscription quota. Refuse the same way, before anything runs.
    if _os.environ.get("FACTORY_NO_REAL_ENGINE") == "1":
        raise CapabilityDenial(
            "REAL_ENGINE_DISABLED: FACTORY_NO_REAL_ENGINE=1 — refusing to "
            "launch a real model reviewer (subscription quota). Use the "
            "deterministic cell_reviewer via run_audit_cell defaults.")
    # Fail-closed accounting: a real launch with no project ledger is exactly
    # the invisible-spend hole. Without project_dir there is no
    # attempt_accounts.jsonl, hence no COST_NO_PROGRESS brake and no budget.
    if project_dir is None:
        raise CapabilityDenial(
            "run_model_reviewer requires project_dir: a real model launch "
            "must be metered in the project's attempt ledger (fail-closed).")
    bin_dir = str(Path(control_root).resolve() / "factory" / "bin")
    if bin_dir not in _sys.path:
        _sys.path.insert(0, bin_dir)
    import model_router as _mr
    from .single_flight import acquire, mark_running, settle, classify
    from .telemetry.adapters import final_text, detect_format

    envelope = package.to_envelope()
    envelope.check(tool="python3",
                   reads=tuple(i["path"] for i in package.allowed_inputs),
                   writes=())
    contract = bind_frozen_contract(project_dir, package.contract_id)

    _verify_exact_clean_checkout(Path(repo), package.candidate_sha)
    choice = _mr.route(spec_level, "review", project=project)
    if not choice.read_only:
        raise CapabilityDenial(
            f"model reviewer route returned a WRITABLE engine {choice.engine!r} "
            f"for task_kind=review — refusing to spawn a writer as reviewer (A3)")
    # Bounded by default (P0 quota): the production caller used to pass no
    # engine_budget, making check()/charge() no-ops — every distinct candidate
    # SHA paid a fresh launch with no ceiling. A default per-engine budget
    # bounds the drain even when the caller forgets.
    budget = engine_budget or RoleEngineBudget(
        control_root, choice.engine, _DEFAULT_MODEL_REVIEWER_ATTEMPTS)
    budget.check()

    # A deterministic structural verdict and a model verdict are distinct
    # authorities. They must never share a cache key: otherwise a prior
    # deterministic PASS could be reused as proof that the model ran.
    key = validation_key_for_review(
        package,
        validator_id=f"model:{choice.engine}:{choice.model}",
        toolchain_id="read-only-model-v1")
    prompt = (
        "CTO read-only review, bound to an exact commit — you may only read; "
        "any write/execute/subagent attempt is denied at the platform level "
        "and recorded.\n"
        f"Repository: {repo}\n"
        f"Candidate SHA (exact, bound — your verdict MUST cite this exact "
        f"value): {package.candidate_sha}\n\n"
        "Inspect the diff/state at this exact commit and decide. Reply with "
        "EXACTLY one JSON object as the LAST thing in your output, no text "
        "after it:\n"
        '{"verdict": "ACCEPT"|"REJECT"|"REDIRECT"|"QUARANTINE", '
        f'"reviewed_sha": "{package.candidate_sha}", "reason": "<short reason>"}}'
    )
    try:
        argv = _mr.cli_args(choice, Path(repo), prompt)
    except _mr.ReadOnlyLaunchNotImplemented as exc:
        raise ReviewerInvalidSchema(
            f"model reviewer launch not implemented for engine "
            f"{choice.engine!r}: {exc}") from exc

    slot = acquire(control_root, key, command=argv,
                   cwd=str(repo), envelope_s=package.budget_s,
                   owner_note=f"model-cell-reviewer:{package.package_id}")
    if not slot.get("owned"):
        job = slot["job"]
        if slot.get("reuse") and (job.get("result") or {}).get("classified") == "PASS":
            verdict = json.loads(
                Path(job["result"]["evidence_path"]).read_text(encoding="utf-8"))
            return {"role": "reviewer", "package_sha": package.package_sha,
                    "contract_id": package.contract_id,
                    "contract_signature": contract["body"]["contract_signature"],
                    "single_flight": "REUSED", "job_key": key,
                    "identity": {"argv": job.get("command"),
                                 "pid": job.get("pid"), "birth": job.get("birth")},
                    "verdict": verdict, "engine": choice.engine}
        return {"role": "reviewer", "package_sha": package.package_sha,
                "contract_id": package.contract_id,
                "single_flight": "ATTACHED", "job_key": key,
                "identity": {"argv": job.get("command"),
                             "pid": job.get("pid"), "birth": job.get("birth")},
                "verdict": None, "pending": True, "engine": choice.engine}

    owned_generation = (slot.get("job") or {}).get("generation")
    started = time.time()
    started_at = _utcnow()
    import subprocess
    # ── common launch gate + accounting + telemetry (no direct unaccounted
    # model Popen remains in audit_loop). The cell's model reviewer spends
    # REAL model quota, so its launch passes through the SAME gate as every
    # other model call: fail-closed check_before_launch BEFORE any spawn
    # (a blocked gate is a typed LaunchBlocked refusal, never a silent
    # skip), a durable OPEN accounting row before execve, lifecycle telemetry
    # (OPEN before / TERMINAL in finally), and a terminal accounting row on
    # every exit path.
    from types import SimpleNamespace as _CellMissionStub
    from . import launch_gate as _launch_gate
    from .telemetry import gate as _telemetry_gate
    _cell_attempt_key = (f"cell-review:{package.package_sha}:"
                         f"{time.time_ns()}")
    _cell_mission = _CellMissionStub(executor={
        "type": "model_review", "task_file": package.contract_id})
    _launch_gate.check_before_launch(
        project_dir,
        mission_id=f"cell-review:{package.bundle_id}",
        milestone=package.bundle_id,
        contract_frozen=True,
        expected_result_defined=True,
        independent_proof_defined=True,
        mission=_cell_mission,
        own_lease_generation=None,
        supervisor_proved_no_model=False)
    _cell_account = _launch_gate.open_account(
        project_dir, attempt_key=_cell_attempt_key,
        project=Path(project_dir).name, milestone=package.bundle_id,
        mission=f"cell-review:{package.bundle_id}", attempt=1,
        engine=choice.engine, model=choice.model, consumes_model_quota=True)
    _cell_t0 = time.monotonic()
    proc = None
    _cell_observed = {"engine_id": choice.engine, "model": choice.model,
                      "observed": True, "argv": list(argv)}
    _cell_done = {"telemetry": False, "account": False}

    def _cell_close(exit_class: str, *, mission_pass: bool,
                    candidate_sha: str = "") -> None:
        """Idempotent terminal accounting row: exactly one per launch."""
        if _cell_done["account"]:
            return
        _cell_done["account"] = True
        _launch_gate.close_account(
            project_dir, _cell_account, exit_class=exit_class,
            mission_pass=mission_pass,
            duration_s=time.monotonic() - _cell_t0,
            candidate_sha=candidate_sha,
            engine_execution=_cell_observed)

    def _cell_telemetry(rc: int, exit_class: str, stdout: str) -> None:
        if _cell_done["telemetry"]:
            return
        _cell_done["telemetry"] = True
        _persist_model_review_telemetry(
            argv, stdout, started_at, time.time() - started, rc, choice,
            package, project_dir,
            (proc.pid if proc is not None else -1), exit_class)

    try:
        with _telemetry_gate.metered_launch(
                project_dir, attempt_key=_cell_attempt_key,
                mission=f"cell-review:{package.bundle_id}",
                milestone=package.bundle_id, engine=choice.engine,
                model=choice.model) as _cell_meter:
            proc = subprocess.Popen(argv, cwd=str(repo), env=scrub_env(),
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE,
                                    text=True, start_new_session=True)
            _cell_observed["pid"] = proc.pid
            _cell_meter.update(rc=None, engine_execution=_cell_observed)
            mark_running(control_root, key, proc.pid, proc.pid)
            if engine_budget is not None:
                engine_budget.charge({"argv": list(argv), "pid": proc.pid})
            record_role_launch(control_root, role="reviewer",
                               identity={"argv": list(argv), "pid": proc.pid,
                                         "birth": ""},
                               contract_id=package.contract_id)
            out = err = ""
            try:
                out, err = proc.communicate(timeout=package.budget_s)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, err = proc.communicate()
                _cell_telemetry(124, "TIMEOUT", out)
                settle(control_root, key, rc=124,
                       classified="HARNESS_TIMED_OUT",
                       duration_s=time.time() - started,
                       generation=owned_generation)
                _cell_close("TIMEOUT", mission_pass=False)
                raise BudgetExceeded(
                    "model reviewer exceeded its bounded envelope (A9)")
            rc = proc.returncode
            _cell_meter.update(
                outcome="SUCCESS" if rc == 0 else "FAILURE", rc=rc,
                engine_execution=_cell_observed)
        wall = round(time.time() - started, 2)
        _cell_telemetry(rc, "SUCCESS" if rc == 0 else "ENGINE_FAILED", out)
        if rc != 0:
            _cell_close("PROVIDER_FAILURE", mission_pass=False)
            classified = classify(rc, envelope_expired=False, child_alive=False)
            settle(control_root, key, rc=rc, classified=classified,
                   duration_s=wall, generation=owned_generation)
            raise ReviewerInvalidSchema(
                f"model reviewer subprocess rc={rc}: {(err or out)[-200:]} "
                f"(typed terminal, not ACCEPT/REJECT)")
        text = final_text(detect_format(argv), out)
        verdict = _extract_verdict_json(text, package.candidate_sha)
        if verdict is None or verdict.get("verdict") not in REVIEWER_VERDICTS \
                or verdict.get("reviewed_sha") != package.candidate_sha:
            _cell_close("PROVIDER_FAILURE", mission_pass=False)
            settle(control_root, key, rc=1, classified="FAIL", duration_s=wall,
                   generation=owned_generation)
            raise ReviewerInvalidSchema(
                f"model reviewer (engine={choice.engine!r}) produced no bound "
                f"typed verdict — typed terminal, not ACCEPT/REJECT")
        keep = Path(control_root) / "factory_v2" / "receipts" / "cell_reviews"
        keep.mkdir(parents=True, exist_ok=True)
        dst = keep / f"{key}.json"
        raw = json.dumps(verdict, sort_keys=True)
        _atomic_write_text(dst, raw)
        settle(control_root, key, rc=0, classified="PASS", duration_s=wall,
               evidence_path=str(dst), generation=owned_generation)
        _cell_close("SUCCESS",
                    mission_pass=verdict.get("verdict") == "ACCEPT",
                    candidate_sha=package.candidate_sha)
        return {"role": "reviewer", "package_sha": package.package_sha,
                "contract_id": package.contract_id,
                "contract_signature": contract["body"]["contract_signature"],
                "single_flight": "OWNED", "job_key": key,
                "identity": {"argv": list(argv), "pid": proc.pid,
                             "pgid": proc.pid, "cwd": str(repo)},
                "verdict": verdict, "verdict_sha16": _sha16(raw),
                "wall_s": wall, "engine": choice.engine,
                "model": choice.model,
                "launch_accounted_as": _cell_attempt_key}
    except BaseException:
        _cell_telemetry(-1, "INTERRUPTED", "")
        _cell_close("KILL", mission_pass=False)
        raise


def _verify_exact_clean_checkout(repo: Path, candidate_sha: str) -> None:
    """Bind reviewer inputs to the exact immutable tree it claims to review."""
    import subprocess
    repo = Path(repo).resolve()
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        text=True, capture_output=True)
    if head.returncode != 0 or head.stdout.strip() != candidate_sha:
        raise ReviewerInvalidSchema(
            "review checkout HEAD does not equal the exact candidate SHA")
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        text=True, capture_output=True)
    if status.returncode != 0 or status.stdout.strip():
        raise ReviewerInvalidSchema(
            "review checkout is dirty; exact-SHA evidence would be ambiguous")


def _persist_model_review_telemetry(argv, stdout: str, started_at: str,
                                    wall_s: float, rc: int, choice,
                                    package: WorkPackage, project_dir: Path,
                                    pid: int, exit_class: str) -> None:
    """Meter every real reviewer launch without letting metering break review."""
    try:
        from .telemetry.ledger import append
        from .telemetry.observe import REVIEW, observe_launch
        obs = observe_launch(
            argv=argv, stdout=stdout, started_at=started_at,
            wall_time_s=wall_s, rc=rc, exit_class=exit_class,
            engine=choice.engine, model_requested=choice.model,
            role="reviewer", lineage=REVIEW,
            project=Path(project_dir).name,
            milestone=package.bundle_id, mission=package.package_id,
            attempt_key=package.package_sha, contract_id=package.contract_id,
            product_sha=package.candidate_sha, task_type="review",
            pid=pid, provenance="LIVE")
        append(obs, project_dir)
    except Exception:
        # Measurement failure must never mutate the verdict or wedge recovery.
        return


# ── SOL-R1 continuation edges: durable typed transitions + bounded backoff ────

TRANSITION_KINDS = frozenset({
    "ROLE_CRASH", "BUDGET_EXCEEDED", "REVIEWER_REJECT",
    "REVIEWER_REDIRECT", "REVIEWER_QUARANTINE", "PROVIDER_EXHAUSTED",
    "POSITION_DRIFT", "CAPABILITY_DENIED", "CREDENTIAL_REFUSED",
})


def backoff_s(attempt: int, base_s: float = 1.0, cap_s: float = 30.0) -> float:
    """Bounded exponential backoff (SOL-R1): never unbounded, never a storm."""
    return min(cap_s, base_s * (2 ** max(0, attempt - 1)))


def record_cell_transition(control_root: Path, *, kind: str, detail: str,
                           attempt: int = 1, action: str = "",
                           recovered: bool | None = None,
                           manual: bool = False,
                           continuation_marker: str = "") -> dict:
    """Persist one durable outgoing transition from a typed failure outcome.

    Every typed failure the cell can observe must leave exactly this record:
    kind, bounded backoff, the autonomous next action, and manual=false —
    the loop's continuation after crash/timeout/verdict/quota is OBSERVABLE
    from disk, not claimed in memory. `continuation_marker` names the next
    loop step observed AFTER the recovery (the proof the loop went on).
    """
    if kind not in TRANSITION_KINDS:
        raise ValueError(f"untyped transition kind {kind!r}")
    transition = {
        "schema": "cell-transition/v1",
        "transition_id": str(uuid.uuid4()),
        "recorded_at": _utcnow(),
        "kind": kind,
        "detail": detail[:400],
        "attempt": int(attempt),
        "backoff_s": backoff_s(attempt),
        "action": action,
        "recovered": recovered,
        "manual": bool(manual),
        "continuation_marker": continuation_marker,
    }
    tdir = Path(control_root) / "factory_v2" / "receipts" / "audit_cells"
    tdir.mkdir(parents=True, exist_ok=True)
    path = tdir / "transitions.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(transition) + "\n")
    return transition


def rewrite_last_cell_transition(control_root: Path, transition: dict) -> None:
    """Enrich the just-appended transition (branch outcome fields observed
    after the initial record: recovery result, routing, schedule). The record
    keeps its transition_id — one durable edge per failure, completed in
    place rather than fragmented across opaque partial rows."""
    tdir = Path(control_root) / "factory_v2" / "receipts" / "audit_cells"
    path = tdir / "transitions.jsonl"
    lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    if not lines:
        raise RuntimeError("no transition to rewrite")
    import json as _json
    last = _json.loads(lines[-1])
    if last.get("transition_id") != transition.get("transition_id"):
        raise RuntimeError("rewrite target is not the last appended transition")
    lines[-1] = _json.dumps(transition)
    _atomic_write_text(path, "\n".join(lines) + "\n")


def record_role_launch(control_root: Path, *, role: str, identity: dict,
                       contract_id: str = "") -> None:
    """Durable record of every REAL role launch (Sol F1 correlation).

    The independent verifier correlates each failure transition's observed
    continuation against these records — a continuation that cannot be found
    in ANY durable record (launch ledger, validation job, engine budget
    ledger) is not a continuation. Written on every successful production
    launch of a role subprocess.
    """
    tdir = Path(control_root) / "factory_v2" / "receipts" / "audit_cells"
    tdir.mkdir(parents=True, exist_ok=True)
    entry = {
        "schema": "cell-role-launch/v1",
        "role": role,
        "pid": identity.get("pid"),
        "argv_head": [str(a) for a in (identity.get("argv") or [])][:4],
        "birth": identity.get("birth", ""),
        "contract_id": contract_id,
        "launched_at": _utcnow(),
    }
    with open(tdir / "launches.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def observe_continuation(control_root: Path, *, kind: str,
                         next_launch_result: dict,
                         position_fingerprint: str) -> dict:
    """Bind a failure transition's continuation to a REAL subsequent launch
    (Sol REJECT@f253f439 F1 — continuations are OBSERVED, never authored).

    Accepts only an actual run_challenger/run_reviewer RESULT dict carrying a
    live subprocess identity (pid+argv); rewrites the most recent transition
    of `kind` that still lacks an `observed_continuation` with: the observed
    next pid/argv head, the completion timestamp and the product position
    fingerprint captured at the same moment. Authored strings such as
    continuation_marker/recovered carry no verification weight — the verifier
    correlates observed_continuation against independently generated durable
    records (validation jobs, engine-budget ledgers, cell identities).
    """
    ident = (next_launch_result or {}).get("identity") or {}
    if not ident.get("pid") or not ident.get("argv"):
        raise ValueError(
            "observe_continuation requires a REAL role-launch result with an "
            "observed subprocess identity (pid+argv) — fabricated or absent "
            "launches are refused (Sol F1)")
    tdir = Path(control_root) / "factory_v2" / "receipts" / "audit_cells"
    path = tdir / "transitions.jsonl"
    lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    target_idx = None
    for i in range(len(lines) - 1, -1, -1):
        t = json.loads(lines[i])
        if t.get("kind") == kind and not t.get("observed_continuation"):
            target_idx = i
            break
    if target_idx is None:
        raise RuntimeError(
            f"no {kind} transition awaiting an observed continuation")
    transition = json.loads(lines[target_idx])
    transition["observed_continuation"] = {
        "source": "production_launch",
        "next_pid": ident.get("pid"),
        "next_argv_head": [str(a) for a in ident.get("argv", [])][:4],
        "next_role": (next_launch_result or {}).get("role", "?"),
        "next_contract_id": (next_launch_result or {}).get("contract_id", ""),
        "observed_at": _utcnow(),
        "position_fingerprint": position_fingerprint,
    }
    lines[target_idx] = json.dumps(transition)
    _atomic_write_text(path, "\n".join(lines) + "\n")
    return transition


class RoleEngineBudget:
    """Wired per-engine attempt budget (SOL-R1 PROVIDER_EXHAUSTED edge).

    Counts REAL role launches through the production path and persists the
    counter durably; when the allowance is spent, the next launch attempt
    raises the typed ProviderExhausted from the WIRED check — exhaustion is
    an observed runtime condition, never a hardcoded flag.
    """

    def __init__(self, control_root: Path, engine: str, max_attempts: int):
        self.control_root = Path(control_root)
        self.engine = str(engine)
        self.max_attempts = int(max_attempts)
        self.ledger = (self.control_root / "factory_v2" / "receipts" /
                       "audit_cells" / f"engine_budget_{self.engine}.json")

    def _read(self) -> dict:
        try:
            d = json.loads(self.ledger.read_text(encoding="utf-8"))
            if d.get("schema") == "cell-engine-budget/v1":
                return d
        except Exception:
            pass
        return {"schema": "cell-engine-budget/v1", "engine": self.engine,
                "max_attempts": self.max_attempts, "charged": 0,
                "launches": []}

    def state(self) -> dict:
        return self._read()

    def check(self) -> None:
        """Raise the typed exhaustion when the observed allowance is spent."""
        s = self._read()
        if s["charged"] >= self.max_attempts:
            raise ProviderExhausted(
                f"{self.engine} engine budget exhausted: charged "
                f"{s['charged']}/{self.max_attempts} real launches (wired "
                f"budget ledger {self.ledger})")

    def charge(self, launch_identity: dict) -> dict:
        """Record one REAL launch; the ledger is the observed counter."""
        s = self._read()
        s["charged"] = int(s.get("charged", 0)) + 1
        s.setdefault("launches", []).append({
            "pid": launch_identity.get("pid"),
            "argv_head": (launch_identity.get("argv") or [""])[:3],
            "at": _utcnow()})
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(self.ledger, json.dumps(s, indent=1))
        return s


def read_cell_transitions(control_root: Path) -> list[dict]:
    path = Path(control_root) / "factory_v2" / "receipts" / "audit_cells" / \
        "transitions.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def validation_key_for_review(
        package: WorkPackage, *,
        validator_id: str = "factory_v2.cell_reviewer",
        toolchain_id: str = "local-isolated-v1") -> str:
    from .single_flight import validation_key
    return validation_key(package.candidate_sha, package.package_sha,
                          "cell-review-v1", validator_id, toolchain_id)


def synthesize(bundle_sha: str, *, gate_failed: bool,
               challenger_findings: list, reviewer_verdicts: list,
               rounds_used: int) -> dict:
    """Deterministic synthesis (A4): a PURE function of its recorded inputs.

   CLAIM -> EVIDENCE -> COUNTEREXAMPLE -> DECISION -> OWNER/WRITER -> NEXT
    EXPERIMENT. The same inputs always produce the same decision and the same
    synthesis_sha. A failed deterministic gate can NEVER be overridden by peer
    consensus (PEER_CONSENSUS_OVERRIDES_GATE=0): rule 1 wins unconditionally.
    """
    blocking = [f for f in challenger_findings
                if isinstance(f, dict) and f.get("is_blocking")]
    verdicts = [v.get("verdict") for v in reviewer_verdicts
                if isinstance(v, dict) and isinstance(v.get("verdict"), str)]
    consensus_accept = bool(verdicts) and all(v == "ACCEPT" for v in verdicts)

    if gate_failed:
        decision = "GATE_FAILS_REDIRECT_WRITER"
        routed_to = "WRITER"
        next_experiment = ("apply the gate-observed correction, resubmit a new "
                           "candidate SHA; peer consensus is evidence only")
    elif "QUARANTINE" in verdicts:
        decision = "QUARANTINE_ESCALATE"
        routed_to = "OWNER_SENSITIVE_EXCEPTION"
        next_experiment = "preserve evidence; fenced quarantine cleared only via safe-clear"
    elif any(v in ("REJECT", "REDIRECT") for v in verdicts):
        decision = "REPAIR_THEN_RESUBMIT"
        routed_to = "WRITER"
        next_experiment = "apply reviewer-required fixes; new candidate SHA; re-review"
    elif blocking and consensus_accept:
        # Ambiguous (A10/D019): conflicting evidence requires >=2 candidate
        # plans; rollouts happen ONLY in isolated worktrees/fixtures.
        decision = "RUN_MINIMAL_EXPERIMENT"
        routed_to = "CONTROLLER"
        next_experiment = ("smallest bounded reversible experiment to break the "
                           "challenger-vs-reviewer disagreement; lookahead never "
                           "mutates the champion")
    else:
        decision = "ACCEPT_CONTINUE"
        routed_to = "WRITER"
        next_experiment = "resume the roadmap at the exact position"

    terminal = (rounds_used >= MAX_ADVERSARIAL_ROUNDS
                or decision in ("GATE_FAILS_REDIRECT_WRITER",
                                "QUARANTINE_ESCALATE", "ACCEPT_CONTINUE"))
    inputs = {
        "bundle_sha": bundle_sha,
        "gate_failed": bool(gate_failed),
        "challenger_findings": challenger_findings,
        "reviewer_verdicts": reviewer_verdicts,
        "rounds_used": int(rounds_used),
    }
    return {
        "schema": "cell-synthesis/v1",
        "claim": f"evidence bundle {bundle_sha} supports continuing the frozen plan",
        "evidence": {
            "challenger_blocking": len(blocking),
            "challenger_total": len(challenger_findings),
            "reviewer_verdicts": verdicts,
            "deterministic_gate_failed": bool(gate_failed),
        },
        "counterexample": blocking[:1] or (
            {"reviewer": verdicts} if any(
                v in ("REJECT", "REDIRECT", "QUARANTINE") for v in verdicts
            ) else None),
        "decision": decision,
        "routed_to": routed_to,
        "next_experiment": next_experiment,
        "rounds_used": int(rounds_used),
        "terminal": terminal,
        "synthesis_inputs_sha16": _sha16(_canon(inputs)),
        "synthesis_sha16": "",
    }


def finalize_synthesis(s: dict) -> dict:
    core = {k: v for k, v in s.items() if k != "synthesis_sha16"}
    s["synthesis_sha16"] = _sha16(_canon(core))
    return s


def run_audit_cell(*, trigger: AuditTrigger, milestone_id: str,
                   project_dir: Path, repo: Path, control_root: Path,
                   candidate_sha: str, gate_failed: bool,
                   failure_fingerprint: str = "", failure_count: int = 0,
                   strategies_attempted: list | None = None,
                   project_state_path: Path | None = None,
                   python: str | None = None,
                   contract_id: str = "",
                   reviewer_fn=None) -> dict:
    """One full cell activation (A1-A6): freeze bundle -> challenger ->
    reviewer -> deterministic synthesis -> writer redirect -> exact-position
    resume, capped at MAX_ADVERSARIAL_ROUNDS.

    contract_id (Sol F3, mandatory): the frozen milestone contract is bound
    BY ID with signature + authority verification before any dispatch; every
    work package carries it and every role launch re-verifies it pre-spawn.

    reviewer_fn: injection seam for the reviewer role, called as
    `reviewer_fn(rv_pkg, control_root, python=python,
    engine_budget=reviewer_budget, project_dir=project_dir)`. Defaults to the
    deterministic `run_reviewer` (cell_reviewer.py structural check) — every
    hermetic caller (audit_cell_exercise.py, every test) relies on this
    default so verification stays reproducible and free of model spend. The
    PRODUCTION caller (runtime.drain_pending_audit_cell) is the only one that
    ever passes something else — a real read-only model reviewer
    (run_model_reviewer), bound via functools.partial to the repo/spec_level
    that call already knows.
    """
    reviewer_fn = reviewer_fn or run_reviewer
    contract = bind_frozen_contract(project_dir, contract_id)
    position_before = capture_position(project_state_path)
    bundle = freeze_evidence_bundle(
        trigger, milestone_id, Path(project_dir),
        git_sha=candidate_sha,
        failure_fingerprint=failure_fingerprint,
        failure_count=failure_count,
        strategies_attempted=strategies_attempted or [])
    base_cap = CapabilityEnvelope(
        allowed_tools=frozenset({"python3"}),
        allowed_read=frozenset(i["path"] for i in bundle.evidence_files)
        | {str(Path(repo) / "factory_v2")},
        allowed_write=frozenset(), allow_network=False)
    challenger_budget = RoleEngineBudget(
        control_root, "cell-challenger", DEFAULT_ENGINE_ATTEMPTS)
    reviewer_budget = RoleEngineBudget(
        control_root, "cell-reviewer", DEFAULT_ENGINE_ATTEMPTS)
    rounds_used = 0
    challenger_results, reviewer_results = [], []
    synthesis = None
    budget_s = 120.0
    for round_no in range(1, MAX_ADVERSARIAL_ROUNDS + 1):
        rounds_used = round_no
        ch_pkg = WorkPackage(
            role="challenger", bundle_id=bundle.bundle_id,
            bundle_sha=bundle.bundle_sha, authority_hashes=bundle.authority_hashes,
            candidate_sha=candidate_sha, budget_s=budget_s,
            contract_id=contract_id,
            deadline_ts=time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                      time.gmtime(time.time() + budget_s)),
            expected_output={"schema": "cell-findings/v1",
                             "list_of": {"description": "str",
                                         "is_blocking": "bool"}},
            allowed_inputs=list(bundle.evidence_files),
            capability=CapabilityEnvelope(
                allowed_tools=base_cap.allowed_tools,
                allowed_read=base_cap.allowed_read,
                allowed_write=frozenset()).as_dict(),
        ).finalize()
        challenger_results.append(
            run_challenger(ch_pkg, repo, control_root=control_root,
                           python=python, engine_budget=challenger_budget,
                           project_dir=project_dir))
        rv_pkg = WorkPackage(
            role="reviewer", bundle_id=bundle.bundle_id,
            bundle_sha=bundle.bundle_sha, authority_hashes=bundle.authority_hashes,
            candidate_sha=candidate_sha, budget_s=budget_s,
            contract_id=contract_id,
            deadline_ts=time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                      time.gmtime(time.time() + budget_s)),
            expected_output={"schema": "cell-verdict/v1",
                             "verdict": sorted(REVIEWER_VERDICTS)},
            allowed_inputs=list(bundle.evidence_files),
            capability=CapabilityEnvelope(
                allowed_tools=base_cap.allowed_tools,
                allowed_read=base_cap.allowed_read | {
                    str(Path(repo) / ".git")},
                allowed_write=frozenset()).as_dict(),
        ).finalize()
        reviewer_results.append(reviewer_fn(rv_pkg, control_root, python=python,
                                            engine_budget=reviewer_budget,
                                            project_dir=project_dir))
        synthesis = finalize_synthesis(synthesize(
            bundle.bundle_sha, gate_failed=gate_failed,
            challenger_findings=challenger_results[-1]["findings"],
            reviewer_verdicts=[reviewer_results[-1]["verdict"]],
            rounds_used=rounds_used))
        if synthesis["terminal"]:
            break
    if synthesis is None:
        raise RuntimeError("cell produced no synthesis (impossible)")
    position_after = capture_position(project_state_path)
    if position_before != position_after:
        raise PositionDrift(
            f"product position drifted across the cell activation "
            f"{position_before}->{position_after} (A6 fail closed)")
    record = {
        "schema": CELL_SCHEMA,
        "cell_id": str(uuid.uuid4()),
        "triggered_at": _utcnow(),
        "trigger": trigger.value,
        "milestone_id": milestone_id,
        "candidate_sha": candidate_sha,
        "contract_id": contract_id,
        "contract_signature": contract["body"]["contract_signature"],
        "gate_failed": bool(gate_failed),
        "bundle": asdict(bundle),
        "challenger_rounds": challenger_results,
        "reviewer_rounds": reviewer_results,
        "synthesis": synthesis,
        "redirect": {
            "routed_to": synthesis["routed_to"],
            "writer_directive": synthesis["next_experiment"],
            "manual": False,
        },
        "position_fingerprint_before": position_before,
        "position_fingerprint_after": position_after,
        "manual_triggers": 0,
        "completed_at": _utcnow(),
    }
    cell_dir = Path(control_root) / "factory_v2" / "receipts" / "audit_cells"
    cell_dir.mkdir(parents=True, exist_ok=True)
    out = cell_dir / f"{record['cell_id']}.json"
    from .project_authority import atomic_write_json
    atomic_write_json(out, record)
    record["record_path"] = str(out)
    return record
