"""AUTOMATED_MULTI_AI_CTO_LOOP_V1 — isolated-but-real activation exercise.

Drives the REAL cell machinery end-to-end against a disposable sandbox:

  1. a real failure signal: two materially different strategies FAIL the same
     frozen fingerprint, written through the real two-phase RunRecords API;
  2. the real trigger detector fires from those records alone (A1 — no manual
     trigger anywhere in the chain);
  3. run_audit_cell executes challenger + reviewer as REAL subprocesses in
     isolated sandboxes (observed argv/pid/birth), under D29 single-flight;
  4. adversarial probes are observed LIVE: capability fail-closed denials,
     credential-leak refusal, single-flight reuse (no duplicate launch),
     deterministic re-synthesis, and peer-consensus-vs-failed-gate ordering;
  5. every receipt boolean is RECOMPUTED from the durable records this
     exercise leaves on disk — nothing is authored.

The sandbox persists under `keep_under` so the raw records stay inspectable
by the independent verifier and reviewers.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import runrecords
from .audit_loop import (
    BudgetExceeded,
    CapabilityDenial,
    CapabilityEnvelope,
    ContractBindingFailure,
    CredentialLeakRefused,
    ProviderExhausted,
    ReviewerInvalidSchema,
    RoleEngineBudget,
    WorkPackage,
    backoff_s,
    capture_position,
    finalize_synthesis,
    observe_continuation,
    record_cell_transition,
    rewrite_last_cell_transition,
    run_audit_cell,
    run_challenger,
    run_reviewer,
    should_trigger_audit,
    synthesize,
    _sha16,
    _utcnow,
    CELL_SCHEMA,
)
from .project_authority import load_authority
from .validation_contract import freeze_contract, load_frozen_contract

REPO_ROOT = Path(__file__).resolve().parent.parent


from ._io import _git as _git_io


def _git_out(repo: Path, *args: str) -> str:
    r = _git_io(repo, *args, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"git {args[:2]} rc={r.returncode}: {r.stderr[-200:]}")
    return r.stdout.strip()


def _make_product_repo(sandbox: Path) -> tuple[Path, Path]:
    """A real disposable git repo + canonical project authority (the 'product').

    The demo project carries a MINIMAL BUT REAL authority trio so the
    production freeze_contract/load_authority/verify_frozen_against_current
    path works exactly as in production (Sol F3: binding before dispatch)."""
    repo = sandbox / "product"
    repo.mkdir(parents=True)
    _git_out(repo, "init", "-q", "--initial-branch=main")
    _git_out(repo, "config", "user.email", "cell-exercise@factory.local")
    _git_out(repo, "config", "user.name", "factory-cell-exercise")
    (repo / "widget.py").write_text("def build():\n    return 'w'\n", encoding="utf-8")
    project = repo / "projects" / "demo"
    project.mkdir(parents=True)
    (project / "PROJECT_STATE.json").write_text(json.dumps({
        "schema": "project-state/v2", "project": "demo",
        "current_milestone": "DEMO_M1",
        "completed_milestones": {"DEMO_M0": {"state": "COMPLETE"}},
        "interrupted_work": None,
    }, indent=1), encoding="utf-8")
    (project / "STRATEGY.md").write_text(
        "# DEMO strategy\nKOL-first demo product; truth over speed.\n", encoding="utf-8")
    (project / "SPEC.md").write_text(
        "# DEMO spec\nThe demo milestone counter-audit cell contract.\n", encoding="utf-8")
    (project / "ROADMAP.yaml").write_text(
        "schema: roadmap/v2\nversion: v1\nproject: demo\nmilestones:\n"
        "- id: DEMO_M1\n  goal: demo gate\n  acceptance:\n  - exercise acceptance A1\n"
        "  product_e2e:\n    test_command: 'true'\n", encoding="utf-8")
    _git_out(repo, "add", "-A")
    _git_out(repo, "commit", "-q", "-m", "demo product base")
    return repo, project / "PROJECT_STATE.json"


def _make_control_root(sandbox: Path) -> Path:
    """Control tree for the exercise: the REAL role runners, byte-verified."""
    ctrl = sandbox / "ctrl"
    fv2 = ctrl / "factory_v2"
    fv2.mkdir(parents=True)
    for name in ("cell_challenger.py", "cell_reviewer.py"):
        src = REPO_ROOT / "factory_v2" / name
        shutil.copyfile(src, fv2 / name)
        if _sha16(src.read_text(encoding="utf-8")) != _sha16((fv2 / name).read_text(encoding="utf-8")):
            raise RuntimeError(f"role runner copy drift: {name}")
    return ctrl


def _record_real_failure_signal(sandbox: Path, ledger: Path) -> dict:
    """Two materially different strategies fail the same frozen fingerprint.

    Real two-phase RunRecords: PREDICTION before each attempt, OBSERVATION
    after, via the production API with the ledger redirected into the sandbox
    (FACTORY_RUNRECORDS_PATH — the anti-pollution contract). The prior env
    value is restored afterwards so a long-lived caller (in-process verifier
    use) can never leak the sandbox ledger path into later real writes.
    """
    prior = os.environ.get("FACTORY_RUNRECORDS_PATH")
    os.environ["FACTORY_RUNRECORDS_PATH"] = str(ledger)
    try:
        return _record_real_failure_signal_inner(sandbox, ledger)
    finally:
        if prior is None:
            os.environ.pop("FACTORY_RUNRECORDS_PATH", None)
        else:
            os.environ["FACTORY_RUNRECORDS_PATH"] = prior


def _record_real_failure_signal_inner(sandbox: Path, ledger: Path) -> dict:
    os.environ["FACTORY_RUNRECORDS_PATH"] = str(ledger)
    fingerprint = "DEMO_EVIDENCE_HASH_DRIFT"
    strategies = ["direct-join", "windowed-backfill"]
    for i, strat in enumerate(strategies):
        aid = f"demo-m1-attempt-{i + 1}"
        runrecords.record_prediction(aid, {
            "project": "demo", "milestone": "DEMO_M1",
            "strategy": strat, "predicted_outcome": "PASS",
            "predicted_duration_s": 30.0})
        runrecords.record_observation(aid, {
            "outcome": "FAIL", "duration_s": 4.0 + i,
            "engine": "local", "strategy": strat,
            "failure_fingerprint": fingerprint,
            "authority_hashes": {"demo": "frozen"}})
    phases = runrecords.read_attempt_phases()
    obs = [p for p in phases if p.get("phase") == "OBSERVATION"]
    fails = [o for o in obs
             if (o.get("observation") or {}).get("outcome") == "FAIL"
             and (o.get("observation") or {}).get("failure_fingerprint") == fingerprint]
    return {
        "failure_count": len(fails),
        "strategies_attempted": sorted({(o.get("observation") or {}).get("strategy")
                                        for o in fails}),
        "fingerprint": fingerprint,
        "ledger": str(ledger),
    }


def _make_fault_control_root(sandbox: Path, name: str,
                             reviewer_body: str = "",
                             challenger_body: str = "") -> Path:
    """A control root whose role runner is FAULT-INJECTED for one branch.

    The healthy control root stays byte-verified; every fault branch gets its
    own clearly-marked disposable root so the injected defect is observable
    black-box through the production role-launch path.
    """
    ctrl = sandbox / f"ctrl-fault-{name}" / "factory_v2"
    ctrl.mkdir(parents=True)
    for base in ("cell_challenger.py", "cell_reviewer.py"):
        shutil.copyfile(REPO_ROOT / "factory_v2" / base, ctrl / base)
    if reviewer_body:
        (ctrl / "cell_reviewer.py").write_text(reviewer_body, encoding="utf-8")
    if challenger_body:
        (ctrl / "cell_challenger.py").write_text(challenger_body, encoding="utf-8")
    return ctrl.parent


_CRASHING_REVIEWER = '''import sys
sys.stderr.write("FAULT_INJECTION: reviewer process dies before any output\\n")
sys.stderr.flush()
import os
os._exit(9)
'''

_SLOW_CHALLENGER = '''import argparse, json, sys, time
from pathlib import Path
ap = argparse.ArgumentParser()
ap.add_argument("--package", required=True); ap.add_argument("--out", required=True)
a = ap.parse_args()
time.sleep(2.0)  # FAULT_INJECTION: exceeds the 0.2s envelope on purpose
Path(a.out).write_text(json.dumps({"schema": "cell-findings/v1", "findings": []}))
'''

_QUARANTINE_REVIEWER = '''import argparse, json, sys
from pathlib import Path
ap = argparse.ArgumentParser()
ap.add_argument("--package", required=True); ap.add_argument("--out", required=True)
a = ap.parse_args()
pkg = json.loads(Path(a.package).read_text())
Path(a.out).write_text(json.dumps({
    "schema": "cell-verdict/v1", "produced_by": "cell_reviewer_FAULT",
    "package_sha": pkg.get("package_sha"), "reviewed_sha": pkg.get("candidate_sha"),
    "verdict": "QUARANTINE",
    "reason": "FAULT_INJECTION: evidence chain not trustworthy"}))
print(json.dumps({"verdict": "QUARANTINE"}))
'''

_REDIRECT_REVIEWER = '''import argparse, hashlib, json, subprocess, sys
from pathlib import Path
ap = argparse.ArgumentParser()
ap.add_argument("--package", required=True); ap.add_argument("--out", required=True)
a = ap.parse_args()
pkg = json.loads(Path(a.package).read_text())
# honest package integrity + exact-SHA binding, then a REDIRECT verdict:
# the approach is answerable but the plan must change (typed Sol-F2 edge)
payload = {k: v for k, v in pkg.items() if k != "package_sha"}
canon = json.dumps(payload, sort_keys=True, separators=(",", ":"))
assert hashlib.sha256(canon.encode()).hexdigest()[:24] == pkg["package_sha"]
repo = next(Path(e).parent for e in pkg["capability"]["allowed_read"]
            if Path(e).name == ".git")
head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                      capture_output=True, text=True).stdout.strip()
assert head == pkg["candidate_sha"], "REDIRECT binds the exact SHA or nothing"
out = {"schema": "cell-verdict/v1", "produced_by": "cell_reviewer_FAULT",
       "package_sha": pkg["package_sha"], "reviewed_sha": head,
       "verdict": "REDIRECT",
       "reason": "FAULT_INJECTION: strategy is wrong for this evidence"}
Path(a.out).write_text(json.dumps(out))
print(json.dumps({"verdict": "REDIRECT"}))
'''


def _run_fault_branches(keep: Path, sandbox: Path, repo: Path,
                        project_state: Path, candidate: str,
                        bundle_evidence: list, healthy_ctrl: Path,
                        project_dir: Path, contract_id: str) -> list[dict]:
    """SOL-R1 continuation edges, fault-injected black-box (roadmap acceptance).

    SIX branches — role crash, budget timeout, reviewer REJECT, reviewer
    REDIRECT, reviewer QUARANTINE, quota/provider exhaustion. Each leaves a
    DURABLE typed transition recording only OBSERVED failure facts, then the
    loop CONTINUES THROUGH THE PRODUCTION PATH: a real subsequent role launch
    (contract-bound, exact position) whose observed identity is bound to the
    transition by observe_continuation (Sol REJECT@f253f439 F1/F2/F3 — no
    authored recovered/marker/all_equal carries any proof weight).
    """
    fault_root = keep / "faults"
    fault_root.mkdir(exist_ok=True)
    ctrl_all = fault_root / "ctrl-all"
    ctrl_all.mkdir(exist_ok=True)
    pos_dir = fault_root / "positions"
    pos_dir.mkdir(exist_ok=True)
    transitions: list[dict] = []
    branch_no = 0

    def _pkg(role, budget_s, inputs=None, extra_read=()):
        return WorkPackage(
            role=role, bundle_sha="f" * 24, authority_hashes={},
            candidate_sha=candidate, budget_s=budget_s,
            contract_id=contract_id,
            allowed_inputs=list(inputs if inputs is not None else bundle_evidence),
            capability=CapabilityEnvelope(
                allowed_tools=frozenset({"python3"}),
                allowed_read={str(repo / ".git")} | set(extra_read),
                allowed_write=frozenset()).as_dict()).finalize()

    def _snapshot(kind: str) -> str:
        pos = capture_position(project_state)
        (pos_dir / f"{branch_no:02d}_{kind}.json").write_text(json.dumps({
            "kind": kind, "position_fingerprint": pos,
            "observed_at": _utcnow()}, indent=1), encoding="utf-8")
        return pos

    def _fail_transition(kind: str, detail: str):
        t = record_cell_transition(ctrl_all, kind=kind, detail=detail,
                                   attempt=1, action="AUTONOMOUS_CONTINUE")
        transitions.append(t)
        return t

    def _continue(kind: str, launch_result: dict, position: str):
        # production continuation: bind the OBSERVED subsequent launch
        transitions[-1] = observe_continuation(
            ctrl_all, kind=kind, next_launch_result=launch_result,
            position_fingerprint=position)

    # ── Branch 1: ROLE_CRASH ───────────────────────────────────────────
    branch_no = 1
    fctrl = _make_fault_control_root(sandbox, "crash",
                                     reviewer_body=_CRASHING_REVIEWER)
    crashed = False
    try:
        run_reviewer(_pkg("reviewer", 30), fctrl, project_dir=project_dir)
    except ReviewerInvalidSchema as exc:
        crashed = True
        crash_detail = f"reviewer rc=9: {exc}"[:200]
    assert crashed, "fault branch 1 did not crash as designed"
    _fail_transition("ROLE_CRASH", crash_detail)
    time.sleep(min(backoff_s(1), 1.0))
    ok = run_reviewer(_pkg("reviewer", 30, inputs=[]), healthy_ctrl,
                      project_dir=project_dir)
    assert ok["single_flight"] == "OWNED" and ok["identity"].get("pid")
    _continue("ROLE_CRASH", ok, _snapshot("ROLE_CRASH"))

    # ── Branch 2: BUDGET_EXCEEDED ──────────────────────────────────────
    branch_no = 2
    fctrl = _make_fault_control_root(sandbox, "slow",
                                     challenger_body=_SLOW_CHALLENGER)
    timed_out = False
    try:
        run_challenger(_pkg("challenger", 0.2, inputs=[]), repo,
                       control_root=fctrl, project_dir=project_dir)
    except BudgetExceeded as exc:
        timed_out = True
        budget_detail = f"challenger: {exc}"[:200]
    assert timed_out, "fault branch 2 did not hit its envelope as designed"
    _fail_transition("BUDGET_EXCEEDED", budget_detail)
    ok = run_challenger(_pkg("challenger", 30, inputs=[]), repo,
                        control_root=healthy_ctrl, project_dir=project_dir)
    _continue("BUDGET_EXCEEDED", ok, _snapshot("BUDGET_EXCEEDED"))

    # ── Branch 3: REVIEWER_REJECT (real black-box, Sol r2 F2a) ─────────
    branch_no = 3
    tampered = fault_root / "tampered_evidence.json"
    tampered.write_text('{"tampered": true}\n', encoding="utf-8")
    bad_evidence = [{"path": str(tampered), "sha16": "0" * 16,
                     "content_snippet": ""}]
    reject_pkg = _pkg("reviewer", 30, inputs=bad_evidence,
                      extra_read={str(tampered)})
    rv = run_reviewer(reject_pkg, healthy_ctrl, project_dir=project_dir)
    assert rv["single_flight"] == "OWNED", rv.get("single_flight")
    assert rv["verdict"]["verdict"] == "REJECT", rv["verdict"]
    assert rv["identity"].get("pid"), "REJECT must come from a real subprocess"
    s = finalize_synthesis(synthesize(
        "f" * 24, gate_failed=False,
        challenger_findings=[{"description": "evidence hash drift",
                              "is_blocking": True}],
        reviewer_verdicts=[rv["verdict"]], rounds_used=1))
    assert s["decision"] == "REPAIR_THEN_RESUBMIT", s["decision"]
    t = _fail_transition(
        "REVIEWER_REJECT",
        "tampered evidence -> REAL reviewer subprocess observed REJECT "
        f"(pid {rv['identity'].get('pid')}, reason "
        f"{str(rv['verdict'].get('reason'))[:80]})")
    t["observed_verdict"] = rv["verdict"]["verdict"]
    t["reviewer_pid"] = rv["identity"].get("pid")
    t["decision"] = s["decision"]
    rewrite_last_cell_transition(ctrl_all, t)
    ok = run_reviewer(_pkg("reviewer", 30, inputs=[]), healthy_ctrl,
                      project_dir=project_dir)
    _continue("REVIEWER_REJECT", ok, _snapshot("REVIEWER_REJECT"))

    # ── Branch 4: REVIEWER_REDIRECT (Sol REJECT@f253f439 F2) ───────────
    branch_no = 4
    fctrl = _make_fault_control_root(sandbox, "redirect",
                                     reviewer_body=_REDIRECT_REVIEWER)
    rd = run_reviewer(_pkg("reviewer", 30, inputs=[]), fctrl,
                      project_dir=project_dir)
    assert rd["verdict"]["verdict"] == "REDIRECT", rd["verdict"]
    assert rd["identity"].get("pid"), "REDIRECT must come from a real subprocess"
    s2 = finalize_synthesis(synthesize(
        "f" * 24, gate_failed=False, challenger_findings=[],
        reviewer_verdicts=[rd["verdict"]], rounds_used=1))
    assert s2["decision"] == "REPAIR_THEN_RESUBMIT", s2["decision"]
    assert s2["routed_to"] == "WRITER", s2["routed_to"]
    t = _fail_transition(
        "REVIEWER_REDIRECT",
        f"REAL reviewer subprocess observed REDIRECT (pid "
        f"{rd['identity'].get('pid')}) -> writer redirected")
    t["observed_verdict"] = rd["verdict"]["verdict"]
    t["reviewer_pid"] = rd["identity"].get("pid")
    t["decision"] = s2["decision"]
    rewrite_last_cell_transition(ctrl_all, t)
    ok = run_challenger(_pkg("challenger", 30, inputs=[]), repo,
                        control_root=healthy_ctrl, project_dir=project_dir)
    _continue("REVIEWER_REDIRECT", ok, _snapshot("REVIEWER_REDIRECT"))

    # ── Branch 5: REVIEWER_QUARANTINE ──────────────────────────────────
    branch_no = 5
    fctrl = _make_fault_control_root(sandbox, "quarantine",
                                     reviewer_body=_QUARANTINE_REVIEWER)
    qv = run_reviewer(_pkg("reviewer", 30, inputs=[]), fctrl,
                      project_dir=project_dir)
    assert qv["verdict"]["verdict"] == "QUARANTINE", qv["verdict"]
    s3 = finalize_synthesis(synthesize(
        "f" * 24, gate_failed=False, challenger_findings=[],
        reviewer_verdicts=[qv["verdict"]], rounds_used=1))
    assert s3["decision"] == "QUARANTINE_ESCALATE", s3["decision"]
    t = _fail_transition(
        "REVIEWER_QUARANTINE",
        "QUARANTINE verdict -> typed owner-sensitive exception path")
    t["observed_verdict"] = qv["verdict"]["verdict"]
    t["reviewer_pid"] = qv["identity"].get("pid")
    t["routed_to"] = s3["routed_to"]
    rewrite_last_cell_transition(ctrl_all, t)
    # other safe work continues while quarantined evidence is preserved
    ok = run_challenger(_pkg("challenger", 30, inputs=[]), repo,
                        control_root=healthy_ctrl, project_dir=project_dir)
    _continue("REVIEWER_QUARANTINE", ok, _snapshot("REVIEWER_QUARANTINE"))

    # ── Branch 6: PROVIDER_EXHAUSTED (wired ledger, Sol r2 F2b) ────────
    branch_no = 6
    budget = RoleEngineBudget(ctrl_all, "challenger-fault", max_attempts=2)
    launch_pids = []
    for _ in range(2):
        res = run_challenger(_pkg("challenger", 30, inputs=[]), repo,
                             control_root=healthy_ctrl,
                             engine_budget=budget, project_dir=project_dir)
        launch_pids.append(res["identity"].get("pid"))
    ledger_state = budget.state()
    assert ledger_state["charged"] == 2, ledger_state
    exhausted = False
    try:
        run_challenger(_pkg("challenger", 30, inputs=[]), repo,
                       control_root=healthy_ctrl,
                       engine_budget=budget, project_dir=project_dir)
    except ProviderExhausted as exc:
        exhausted = True
        exhaustion_detail = str(exc)[:200]
    assert exhausted, "wired budget did not raise ProviderExhausted"
    t = _fail_transition("PROVIDER_EXHAUSTED", exhaustion_detail)
    t["charged_launches"] = len(launch_pids)
    t["budget_ledger"] = str(budget.ledger)
    rewrite_last_cell_transition(ctrl_all, t)
    # bounded backoff then SWITCH ENGINE: a fresh fallback engine lane budget
    time.sleep(min(backoff_s(1), 1.0))
    fallback = RoleEngineBudget(ctrl_all, "challenger-fallback", max_attempts=2)
    ok = run_challenger(_pkg("challenger", 30, inputs=[]), repo,
                        control_root=healthy_ctrl,
                        engine_budget=fallback, project_dir=project_dir)
    _continue("PROVIDER_EXHAUSTED", ok, _snapshot("PROVIDER_EXHAUSTED"))

    return transitions


def run_audit_cell_exercise(keep_under: Path | None = None) -> dict:
    started = time.time()
    keep = Path(keep_under).resolve() if keep_under else Path(
        tempfile.mkdtemp(prefix="factory-audit-cell-exercise-"))
    keep.mkdir(parents=True, exist_ok=True)
    sandbox = keep / "sandbox"
    sandbox.mkdir()
    repo, project_state = _make_product_repo(sandbox)
    ctrl = _make_control_root(sandbox)
    # Sol F3: freeze a REAL contract for the demo milestone through the
    # production path — every dispatch below binds it BY ID pre-spawn.
    import yaml as _yaml
    demo_authority = load_authority(repo / "projects" / "demo")
    _roadmap = _yaml.safe_load(
        (repo / "projects" / "demo" / "ROADMAP.yaml").read_text(encoding="utf-8"))
    _m1 = next(m for m in _roadmap["milestones"] if m["id"] == "DEMO_M1")
    demo_contract = freeze_contract(repo / "projects" / "demo",
                                    demo_authority, _m1)
    demo_contract_id = demo_contract["contract_id"]
    ledger = keep / "runrecords.jsonl"
    failure = _record_real_failure_signal(sandbox, ledger)

    # A1: the trigger fires from the real records alone — nothing manual.
    trigger = should_trigger_audit(
        failure_fingerprint=failure["fingerprint"],
        failure_count=failure["failure_count"],
        strategies_attempted=failure["strategies_attempted"])
    if trigger is None:
        raise RuntimeError("real failure signal did not fire a trigger (A1)")
    candidate = _git_out(repo, "rev-parse", "HEAD")
    evidence = keep / "failure_signal.json"
    evidence.write_text(json.dumps(failure, indent=1), encoding="utf-8")

    cell = run_audit_cell(
        trigger=trigger, milestone_id="DEMO_M1",
        project_dir=repo / "projects" / "demo", repo=repo, control_root=ctrl,
        candidate_sha=candidate, gate_failed=False,
        contract_id=demo_contract_id,
        failure_fingerprint=failure["fingerprint"],
        failure_count=failure["failure_count"],
        strategies_attempted=failure["strategies_attempted"],
        project_state_path=project_state)

    probes: dict[str, dict] = {}

    # A8: capabilities fail closed — unknown tool denied, observed live.
    try:
        CapabilityEnvelope(allowed_tools=frozenset({"python3"})).check(tool="curl")
        probes["capability_unknown_tool_denied"] = {"observed": False,
                                                    "error": "NO_DENIAL"}
    except CapabilityDenial as exc:
        probes["capability_unknown_tool_denied"] = {"observed": True,
                                                    "error": str(exc)[:120]}
    # A8: a read-only role may not write anywhere.
    try:
        CapabilityEnvelope(allowed_tools=frozenset({"python3"})).check(
            tool="python3", writes=("/etc/passwd",))
        probes["capability_readonly_write_denied"] = {"observed": False,
                                                      "error": "NO_DENIAL"}
    except CapabilityDenial as exc:
        probes["capability_readonly_write_denied"] = {"observed": True,
                                                      "error": str(exc)[:120]}
    # A8: credential-shaped keys can never enter a work package.
    try:
        leak = WorkPackage(role="challenger", bundle_sha="x" * 24,
                           budget_s=10, capability={}, allowed_inputs=[],
                           authority_hashes={"api_token": "leak"})
        leak.finalize()
        probes["credential_leak_refused"] = {"observed": False,
                                             "error": "NO_REFUSAL"}
    except CredentialLeakRefused as exc:
        probes["credential_leak_refused"] = {"observed": True,
                                             "error": str(exc)[:120]}

    # D29/A3: the identical review re-requests single-flight -> REUSE, and the
    # durable job shows exactly ONE generation (no duplicate launch ever ran).
    rv_pkg = WorkPackage(
        role="reviewer", bundle_sha=cell["bundle"]["bundle_sha"],
        authority_hashes=cell["bundle"]["authority_hashes"],
        candidate_sha=candidate, budget_s=60,
        contract_id=demo_contract_id,
        allowed_inputs=cell["bundle"]["evidence_files"],
        capability=CapabilityEnvelope(
            allowed_tools=frozenset({"python3"}),
            allowed_read={str(repo / ".git")},
            allowed_write=frozenset()).as_dict()).finalize()
    first = run_reviewer(rv_pkg, ctrl, project_dir=repo / "projects" / "demo")
    second = run_reviewer(rv_pkg, ctrl, project_dir=repo / "projects" / "demo")
    job = json.loads((ctrl / "factory_v2" / "receipts" / "validation_jobs"
                      / f"{first['job_key']}.json").read_text(encoding="utf-8"))
    probes["single_flight_no_duplicate"] = {
        "observed": (first["single_flight"] == "OWNED"
                     and second["single_flight"] == "REUSED"
                     and job["generation"] == 1),
        "first": first["single_flight"], "second": second["single_flight"],
        "job_generation": job["generation"],
    }

    # A4: deterministic synthesis — same recorded inputs, same decision+sha.
    s1 = finalize_synthesis(synthesize(
        cell["bundle"]["bundle_sha"], gate_failed=False,
        challenger_findings=cell["challenger_rounds"][-1]["findings"],
        reviewer_verdicts=[cell["reviewer_rounds"][-1]["verdict"]],
        rounds_used=cell["synthesis"]["rounds_used"]))
    s2 = finalize_synthesis(synthesize(
        cell["bundle"]["bundle_sha"], gate_failed=False,
        challenger_findings=cell["challenger_rounds"][-1]["findings"],
        reviewer_verdicts=[cell["reviewer_rounds"][-1]["verdict"]],
        rounds_used=cell["synthesis"]["rounds_used"]))
    probes["synthesis_deterministic"] = {
        "observed": s1 == s2 and s1["synthesis_sha16"] == cell["synthesis"]["synthesis_sha16"],
        "sha": s1["synthesis_sha16"], "cell_sha": cell["synthesis"]["synthesis_sha16"],
    }

    # A3/PEER_CONSENSUS_OVERRIDES_GATE=0: unanimous peer ACCEPT cannot rescue
    # a FAILED deterministic gate — rule order observed on this very bundle.
    g = finalize_synthesis(synthesize(
        cell["bundle"]["bundle_sha"], gate_failed=True,
        challenger_findings=[],
        reviewer_verdicts=[{"verdict": "ACCEPT"}] * 3,
        rounds_used=1))
    probes["peer_consensus_cannot_override_failed_gate"] = {
        "observed": g["decision"] == "GATE_FAILS_REDIRECT_WRITER",
        "decision": g["decision"],
    }

    # A5: rounds are capped at MAX_ADVERSARIAL_ROUNDS.
    probes["rounds_capped"] = {
        "observed": cell["synthesis"]["rounds_used"] <= 2,
        "rounds_used": cell["synthesis"]["rounds_used"],
    }
    # A6: exact-position resume.
    probes["position_preserved"] = {
        "observed": (cell["position_fingerprint_before"]
                     == cell["position_fingerprint_after"]),
        "before": cell["position_fingerprint_before"],
        "after": cell["position_fingerprint_after"],
    }
    # A2: zero writes attributable to the challenger.
    probes["challenger_read_only"] = {
        "observed": all(
            r["repo_digest_before"] == r["repo_digest_after"]
            and r["repo_digest_before"]
            for r in cell["challenger_rounds"]),
    }
    # A1: real role subprocess identities were observed (never authored).
    probes["real_role_processes"] = {
        "observed": all(
            r["identity"].get("pid") and r["identity"].get("argv")
            for r in cell["challenger_rounds"] + cell["reviewer_rounds"]),
    }
    # A1: no manual trigger / handoff anywhere in the chain.
    probes["zero_manual"] = {
        "observed": cell.get("manual_triggers") == 0
        and cell["redirect"].get("manual") is False,
    }

    # SOL-R1 continuation edges: SIX fault-injected branches, black-box,
    # production-observed continuations + bounded backoff + exact position.
    fault_transitions = _run_fault_branches(
        keep, sandbox, repo, project_state, candidate,
        bundle_evidence=cell["bundle"]["evidence_files"], healthy_ctrl=ctrl,
        project_dir=repo / "projects" / "demo",
        contract_id=demo_contract_id)
    kinds = {t["kind"] for t in fault_transitions}
    required_kinds = {"ROLE_CRASH", "BUDGET_EXCEEDED", "REVIEWER_REJECT",
                      "REVIEWER_REDIRECT", "REVIEWER_QUARANTINE",
                      "PROVIDER_EXHAUSTED"}
    probes["continuation_edges_all_branches"] = {
        "observed": required_kinds <= kinds,
        "kinds": sorted(kinds),
    }
    # Sol F1: continuations are OBSERVED, not authored — every transition
    # carries an observed_continuation bound to a real subsequent launch, and
    # the per-branch position SNAPSHOTS (files on disk, recomputed by the
    # verifier) are all equal to the cell's position fingerprints.
    _snap_files = sorted((keep / "faults" / "positions").glob("*.json"))
    _snap_positions = [json.loads(f.read_text(encoding="utf-8"))
                      .get("position_fingerprint") for f in _snap_files]
    probes["continuations_observed_not_authored"] = {
        "observed": all(
            (t.get("observed_continuation") or {}).get("source")
            == "production_launch"
            and (t["observed_continuation"]).get("next_pid")
            and (t["observed_continuation"]).get("position_fingerprint")
            for t in fault_transitions),
        "count": len(fault_transitions),
    }
    probes["continuation_transitions_durable_bounded"] = {
        "observed": all(
            t.get("schema") == "cell-transition/v1"
            and 0 <= t.get("backoff_s", -1) <= 30.0
            and t.get("manual") is False
            for t in fault_transitions),
        "count": len(fault_transitions),
    }
    probes["continuation_position_preserved"] = {
        "observed": (len(_snap_positions) == len(required_kinds)
                     and len(set(_snap_positions)) == 1
                     and _snap_positions[0]
                     == cell["position_fingerprint_before"]
                     == cell["position_fingerprint_after"]),
        "snapshot_files": [f.name for f in _snap_files],
        "positions": _snap_positions,
    }

    # F2a (r2 REJECT scope): the REVIEWER_REJECT edge was produced by a REAL
    # reviewer subprocess whose OBSERVED verdict drove synthesis — recomputed
    # from the durable transition record, not from the in-memory branch.
    reject_t = next((t for t in fault_transitions
                     if t["kind"] == "REVIEWER_REJECT"), {})
    probes["reviewer_reject_black_box"] = {
        "observed": bool(
            reject_t.get("reviewer_pid")
            and reject_t.get("observed_verdict") == "REJECT"
            and reject_t.get("decision") == "REPAIR_THEN_RESUBMIT"
            and (reject_t.get("observed_continuation") or {}).get("next_pid")),
        "reviewer_pid": reject_t.get("reviewer_pid"),
        "observed_verdict": reject_t.get("observed_verdict"),
        "continuation_pid": (reject_t.get("observed_continuation") or {})
        .get("next_pid"),
    }
    # Sol F2: the REDIRECT edge is exercised through a REAL reviewer
    # subprocess and routes the WRITER through the same production path.
    redirect_t = next((t for t in fault_transitions
                       if t["kind"] == "REVIEWER_REDIRECT"), {})
    probes["reviewer_redirect_edge_exercised"] = {
        "observed": bool(
            redirect_t.get("reviewer_pid")
            and redirect_t.get("observed_verdict") == "REDIRECT"
            and redirect_t.get("decision") == "REPAIR_THEN_RESUBMIT"
            and (redirect_t.get("observed_continuation") or {}).get("next_pid")),
        "reviewer_pid": redirect_t.get("reviewer_pid"),
        "observed_verdict": redirect_t.get("observed_verdict"),
    }
    # Sol F3: pre-spawn contract binding — every cell round AND every fault
    # dispatch carries the frozen contract id; an unbound package is refused
    # BEFORE any spawn (observed live below).
    probes["contract_bound_pre_dispatch"] = {
        "observed": (
            cell.get("contract_id") == demo_contract_id
            and all(r.get("contract_id") == demo_contract_id
                    for r in cell["challenger_rounds"]
                    + cell["reviewer_rounds"])
            and all((t.get("observed_continuation") or {})
                    .get("next_contract_id") == demo_contract_id
                    for t in fault_transitions)),
        "contract_id": demo_contract_id,
    }
    unbound_refused = False
    try:
        run_reviewer(
            WorkPackage(
                role="reviewer", bundle_sha="u" * 24, authority_hashes={},
                candidate_sha=candidate, budget_s=30,
                allowed_inputs=[],
                capability=CapabilityEnvelope(
                    allowed_tools=frozenset({"python3"}),
                    allowed_read={str(repo / ".git")},
                    allowed_write=frozenset()).as_dict()).finalize(),
            ctrl, project_dir=repo / "projects" / "demo")
    except ContractBindingFailure:
        unbound_refused = True
    probes["unbound_dispatch_refused_pre_spawn"] = {
        "observed": unbound_refused,
        "note": "package without contract_id refused before acquire/spawn",
    }

    probes_path = keep / "probes.json"
    probes_path.write_text(json.dumps(probes, indent=1), encoding="utf-8")

    # The receipt is RECOMPUTED from the durable records just written — every
    # boolean below traces to a probe or a record field, never to an author.
    try:
        _state = json.loads((REPO_ROOT / "projects" / "factory" /
                             "PROJECT_STATE.json").read_text(encoding="utf-8"))
        _entry = ((_state.get("validation_contracts") or {})
                  .get("AUTOMATED_MULTI_AI_CTO_LOOP_V1") or {})
        contract_ref = _entry.get("contract_id", "")
        if not isinstance(contract_ref, str):
            contract_ref = ""
    except Exception:
        contract_ref = ""
    receipt = {
        "schema": "audit-loop-receipt/v2",
        "produced_at": _utcnow(),
        "milestone": "AUTOMATED_MULTI_AI_CTO_LOOP_V1",
        "contract_id": contract_ref,
        "cell_schema": CELL_SCHEMA,
        "trigger": {
            "kind": cell["trigger"],
            "source": "real two-phase RunRecords failure signal (2 distinct "
                      "strategies, same frozen fingerprint)",
            "failure_count": failure["failure_count"],
            "strategies": failure["strategies_attempted"],
            "manual": False,
        },
        "candidate_sha": candidate,
        "cell_record": cell["record_path"],
        "cell_id": cell["cell_id"],
        "synthesis_sha16": cell["synthesis"]["synthesis_sha16"],
        "decision": cell["synthesis"]["decision"],
        "fault_transitions": {t["kind"]: t["transition_id"]
                              for t in fault_transitions},
        "probes_path": str(probes_path),
        "checks": {name: bool(p["observed"]) for name, p in probes.items()},
        "exercise_argv": [sys.executable, "-m",
                          "factory_v2.audit_cell_exercise"],
        "wall_s": round(time.time() - started, 2),
    }
    receipt["all_checks_pass"] = all(receipt["checks"].values())
    (keep / "AUDIT_LOOP_RECEIPT.json").write_text(
        json.dumps(receipt, indent=1), encoding="utf-8")
    return receipt


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--keep-under", default="")
    args = ap.parse_args(argv)
    receipt = run_audit_cell_exercise(Path(args.keep_under) if args.keep_under else None)
    print(json.dumps({"all_checks_pass": receipt["all_checks_pass"],
                      "decision": receipt["decision"],
                      "keep": str(Path(receipt["probes_path"]).parent)}))
    return 0 if receipt["all_checks_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
