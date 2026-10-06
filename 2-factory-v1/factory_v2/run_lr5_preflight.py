"""LR5 REAL harness: produce LR5_PREFLIGHT_RECEIPT.json from an observed run.

Replaces the authored (INVALIDATED_BY_COUNTER_AUDIT) receipt.  Nothing in the receipt
is hand-written except references to what this harness OBSERVED:

  1. a tmp fixture project (non-git product dir — settlement validation runs unpinned
     there by design) with three sequential local-command milestones;
  2. run_controlled_long_run with the REAL factoryd binary (--foreground so the
     spawned process IS the daemon), fault injection ON: real SIGKILL after the first
     proven milestone, restart, restarted=True only on observed post-restart progress;
     every daemon incarnation's pid + birth time recorded;
  3. a CAS double-dispatch probe on a real promotion ledger (first CAS_ALLOWED,
     second CAS_DUPLICATE);
  4. an external-ACCEPTED branch-preservation probe through the REAL dispatcher_v3
     worktree path on a real tmp git repo;
  5. the preflight checks (long_run_preflight.run_preflight) on the fixture;
  6. observation artifacts written to factory_v2/receipts/lr5_evidence/, the receipt
     written to factory_v2/receipts/LR5_PREFLIGHT_RECEIPT.json, and the whole thing
     self-verified in a SEPARATE verify_preflight_receipt.py subprocess (all_pass is
     set from that verdict, never authored).

Usage: python3 -m factory_v2.run_lr5_preflight [--keep-tmp]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

from factory_v2 import event_journal

_REPO = Path(__file__).resolve().parent.parent


from ._io import _git as _git_io, _now


def _git_checked(repo: Path, *args: str) -> str:
    r = _git_io(repo, *args)
    if r.returncode != 0:
        raise subprocess.CalledProcessError(r.returncode, ["git", *args], r.stdout, r.stderr)
    return r.stdout.strip()


def _build_fixture(root: Path) -> Path:
    """Three sequential milestones whose executors create the product artifacts."""
    pd = root / "project"
    product = root / "product"
    product.mkdir(parents=True)
    pd.mkdir(parents=True)
    (pd / "evidence").mkdir()
    (pd / "STRATEGY.md").write_text("# Strategy\nNorth Star: three honest milestones.\n")
    (pd / "SPEC.md").write_text("# Spec\nEach milestone leaves an artifact.\n")

    ms_lines = []
    for i in (1, 2, 3):
        if i == 1:
            deps = "depends_on: {}"
        else:
            deps = ("depends_on:\n    MS%d:\n      required_state: COMPLETE" % (i - 1))
        ms_lines.append(textwrap.dedent("""\
          - id: MS{i}
            goal: milestone {i} leaves its artifact
            deliverables: []
            acceptance: [artifact MS{i}.done exists]
            {deps}
            product_e2e:
              test_command: 'test -f {product}/artifacts/MS{i}.done'
            execution:
              steps:
              - kind: implement
                executor:
                  type: local_command
                  command: 'mkdir -p "{product}/artifacts" && touch "{product}/artifacts/MS{i}.done"'
                  test_cmd: 'test -d "{product}/artifacts"'
        """).format(i=i, deps=deps, product=product))
    (pd / "ROADMAP.yaml").write_text(
        f"project: lr5-harness\nproduct_repo: {product}\nmilestones:\n"
        + "\n".join(ms_lines) + "\n")
    (pd / "PROJECT_STATE.json").write_text(json.dumps({
        "schema": "project-state/v2", "project": "lr5-harness",
        "completed_milestones": {}, "validation_contracts": {},
        "current_milestone": "MS1",
        "missions": {}, "escalation_ladder": {}, "events": [],
    }, indent=2) + "\n")
    return pd


def _controlled_run(pd: Path, root: Path) -> dict:
    from factory_v2.run_controlled_long_run import (ControlledLongRunConfig,
                                                    run_controlled_long_run)
    factoryd = _REPO / "factory" / "bin" / "factoryd.py"
    # `sh -c exec ... > log`: the daemon's chatty -u output goes to a file, so the
    # runner's undrained PIPE can never block the daemon; exec keeps the pid stable
    # for kill/identity observation.
    daemon_cmd = (f"exec '{sys.executable}' -u '{factoryd}' start --foreground "
                  f"--v2-project-dir '{pd}' --run-dir '{root / 'run'}' "
                  f"--poll-interval-s 0.5 --idle-poll-interval-s 1")
    cmd = ["/bin/sh", "-c", f"{daemon_cmd} >> '{root / 'factoryd.log'}' 2>&1"]
    config = ControlledLongRunConfig(
        min_milestones=3, min_engines=1, fault_injection=True,
        timeout_s=360, poll_interval_s=0.5,
        factoryd_cmd=cmd, project_dir=pd, repo_dir=_REPO,
    )
    result = run_controlled_long_run(config)
    return result.as_dict()


def _cas_probe(pd: Path) -> dict:
    from factory_v2.cas_promotion import check_and_record_promotion
    mission = "LR5_HARNESS#DOUBLE_DISPATCH"
    sha = "f" * 40
    ok1, reason1 = check_and_record_promotion(mission, sha, pd)
    ok2, reason2 = check_and_record_promotion(mission, sha, pd)
    return {"mission_id": mission, "integration_sha": sha,
            "first_allowed": ok1, "first_outcome": reason1,
            "second_allowed": ok2, "second_outcome": reason2,
            "observed_at": _now()}


def _branch_probe(root: Path) -> dict:
    """External ACCEPTED branch preserved through the REAL dispatcher worktree path."""
    from unittest import mock
    sys.path.insert(0, str(_REPO / "factory" / "bin"))
    import dispatcher_v3 as d3

    repo = root / "external_repo"
    repo.mkdir()
    for c in (["git", "init", "-q"], ["git", "config", "user.email", "t@t"],
              ["git", "config", "user.name", "t"], ["git", "commit", "-q", "--allow-empty",
              "-m", "base"]):
        subprocess.run(c, cwd=repo, check=True, capture_output=True)

    out_path = root / "branch_receipt.json"
    original_run = d3._run
    _FAKE = json.dumps({"result": "done", "summary": "harness probe"})

    def smart_run(cmd, cwd, timeout):
        if cmd and cmd[0] in ("/bin/sh", "bash", "git"):
            return original_run(cmd, cwd, timeout)
        (Path(cwd) / "mission_output.txt").write_text("built\n")
        return (0, _FAKE, "")

    with mock.patch.object(d3, "_is_factory_repo", lambda repo_root: False), \
         mock.patch.object(d3, "_run", side_effect=smart_run):
        rec = d3._run_in_worktree("LR5EXT", repo, "harness branch probe",
                                  "exit 0", 120, out_path, spec_level="A")
    branches = subprocess.run(["git", "branch", "--list", "candidate/LR5EXT"],
                              cwd=repo, capture_output=True, text=True).stdout.strip()
    return {"mission_id": "LR5EXT", "verdict": rec.get("verdict"),
            "candidate_branch": "candidate/LR5EXT",
            "branch_exists_after": branches.strip() == "candidate/LR5EXT",
            "receipt_path": str(out_path), "observed_at": _now()}


def _derive_human_interventions(pd: Path, started_at: str,
                                daemon_pids: list) -> dict:
    """DERIVE the human-intervention count from observed evidence (Claude
    round-1: a hardcoded 0/[] literal is an authored claim, not an observation).

    Two independent observations:
      1. EVENT LEDGER — any event typed HUMAN*/OWNER* recorded in the project
         state at/after the run window opened;
      2. CONTROLLING TTY — any daemon in the run's identity set attached to a
         terminal (ps -o tty= returning something other than '?') — an
         interactive path into the run.

    Returns the derived record: count, observed events, tty observations, and
    the derivation rule (frozen, re-checkable).
    """
    events_seen = []
    observation_errors = []
    try:
        # STA-04: iter_events reads the append-only journal when enabled, and
        # falls back to state["events"] untouched otherwise -- full history
        # either way, unlike reading state["events"] directly once truncation
        # is turned on.
        # NB: no since_ts filter here -- these events use "at" not "ts" as
        # their timestamp field, and the ">= started_at" check below already
        # does the real filtering against whichever key is present.
        for ev in event_journal.iter_events(pd):
            t = str(ev.get("type") or ev.get("event") or "")
            at = str(ev.get("at") or ev.get("ts") or "")
            if t.upper().startswith(("HUMAN", "OWNER")) and at >= started_at:
                events_seen.append({"type": t, "at": at})
    except Exception as exc:
        # L5_rest_2-005: a source that cannot be read is NOT a zero — an
        # unavailable observation must never be presented as a conclusive
        # measurement of autonomy.
        observation_errors.append(f"event-ledger: {type(exc).__name__}: {exc}")
    tty_seen = []
    for pid in daemon_pids:
        try:
            r = subprocess.run(["ps", "-o", "tty=", "-p", str(pid)],
                               capture_output=True, text=True, timeout=10)
            tty = r.stdout.strip()
            if tty and tty != "?":
                tty_seen.append({"pid": pid, "tty": tty})
        except Exception as exc:
            observation_errors.append(f"ps tty pid={pid}: {type(exc).__name__}: {exc}")
    count = len(events_seen) + len(tty_seen)
    available = not observation_errors
    return {
        # L5_rest_2-005: None (not 0) when either source was unreadable — a
        # verifier requiring == 0 fails closed on None instead of certifying
        # an autonomy it could not observe.
        "routine_human_interventions": count if available else None,
        "observation_available": available,
        "observation_errors": observation_errors,
        "human_intervention_events": events_seen + tty_seen,
        "derivation": ("event-ledger HUMAN*/OWNER* events at/after run start + "
                       "controlling-TTY observations on daemon identities"),
        "observed_from": {"project_state": str(pd / "PROJECT_STATE.json"),
                          "daemon_pids": daemon_pids, "run_started_at": started_at},
    }


def main() -> int:
    ap = argparse.ArgumentParser(prog="run_lr5_preflight")
    ap.add_argument("--keep-tmp", action="store_true")
    ap.add_argument("--supersede-sha", default="",
                    help="explicit CAS replacement of a committed receipt pinned to "
                         "another integration SHA (must equal the committed "
                         "integration_sha exactly)")
    ap.add_argument("--publish", action="store_true",
                    help="write the receipt+evidence into factory_v2/receipts/ (the "
                    "committed audit trail). Default: staging only — a re-run from "
                    "tests/CI must never silently mutate the audited bundle.")
    ap.add_argument("--integration-sha", default="",
                    help="pin the integration SHA explicitly (recommended for "
                    "publishing: the code commit the harness runs at). Required with "
                    "--publish when the tree is dirty.")
    args = ap.parse_args()

    started = _now()
    root = Path(tempfile.mkdtemp(prefix="lr5_harness_"))
    repo_ev_dir = _REPO / "factory_v2" / "receipts" / "lr5_evidence"
    repo_receipt = _REPO / "factory_v2" / "receipts" / "LR5_PREFLIGHT_RECEIPT.json"

    # Publish guards (gate-5 required fixes A+B):
    #   A. publishing must not overwrite a committed receipt pinned to a different
    #      integration SHA;
    #   B. publishing requires an explicitly pinned SHA on a clean factory_v2 tree —
    #      ambient HEAD trust is how a dirty tree could bless itself.
    integration_sha = args.integration_sha.strip()
    if args.publish:
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--", "factory_v2"],
            cwd=str(_REPO), capture_output=True, text=True).stdout.strip()
        if dirty and not integration_sha:
            print(json.dumps({"error": "PUBLISH_REFUSED_DIRTY_TREE_UNPINNED",
                              "detail": "pin --integration-sha explicitly to publish"}))
            return 1
        if not integration_sha:
            integration_sha = _git_checked(_REPO, "rev-parse", "HEAD")
        if repo_receipt.exists():
            existing = json.loads(repo_receipt.read_text(encoding="utf-8"))
            if existing.get("integration_sha") and existing["integration_sha"] != integration_sha:
                supersede = (args.supersede_sha or "").strip()
                if not supersede or supersede != existing["integration_sha"]:
                    print(json.dumps({
                        "error": "PUBLISH_REFUSED_SHA_MISMATCH",
                        "committed": existing.get("integration_sha", "")[:12],
                        "this_run": integration_sha[:12],
                        "detail": "the committed audit trail belongs to another tree; "
                                  "regenerating at a new tip is a compare-and-swap: "
                                  "pass --supersede-sha <committed integration_sha> to "
                                  "replace it explicitly (git history preserves the "
                                  "superseded receipt)"}))
                    return 1
    else:
        integration_sha = integration_sha or _git_checked(_REPO, "rev-parse", "HEAD")

    if args.publish:
        ev_dir = repo_ev_dir
        ev_dir.mkdir(parents=True, exist_ok=True)
    else:
        ev_dir = root / "staged" / "factory_v2" / "receipts" / "lr5_evidence"
        ev_dir.mkdir(parents=True, exist_ok=True)

    pd = _build_fixture(root)

    # 1. Preflight checks on the fixture (manifest reconstruction from disk).
    from factory_v2.long_run_preflight import run_preflight, persist_preflight_result
    pre = run_preflight(pd, repo_dir=_REPO)
    persist_preflight_result(pre, pd)
    if pre.status != "PASS":
        print(json.dumps({"error": "PREFLIGHT_FAILED", "blocked_by": pre.blocked_by}))
        return 1

    # 2. Controlled long run with REAL factoryd + injected SIGKILL + restart.
    run = _controlled_run(pd, root)
    (ev_dir / "long_run_result.json").write_text(json.dumps(run, indent=2) + "\n")
    (ev_dir / "preflight_result.json").write_text(pre.as_json() + "\n")
    if run["status"] != "PASS" or not run["restarted"]:
        print(json.dumps({"error": "CONTROLLED_RUN_FAILED", "result": {
            "status": run["status"], "restarted": run["restarted"],
            "crashed": run["crashed"], "milestones": run["milestones_completed"]}}))
        return 1

    ids = run["daemon_identities"]
    if len(ids) < 2 or ids[0]["pid"] == ids[1]["pid"] or ids[0]["birth"] == ids[1]["birth"]:
        print(json.dumps({"error": "RESTART_IDENTITY_NOT_DISTINCT", "identities": ids}))
        return 1

    # 3. CAS double-dispatch probe on a real ledger.
    cas = _cas_probe(pd)
    (ev_dir / "cas_double_dispatch.json").write_text(json.dumps(cas, indent=2) + "\n")
    if not cas["first_allowed"] or "CAS_DUPLICATE" not in cas["second_outcome"]:
        print(json.dumps({"error": "CAS_PROBE_FAILED", "cas": cas}))
        return 1

    # 4. External ACCEPTED branch preservation through the real dispatcher path.
    branch = _branch_probe(root)
    (ev_dir / "branch_preserved.json").write_text(json.dumps(branch, indent=2) + "\n")
    if branch["verdict"] != "ACCEPTED" or not branch["branch_exists_after"]:
        print(json.dumps({"error": "BRANCH_PROBE_FAILED", "branch": branch}))
        return 1

    # 5. Kill/restart observation artifact from what the run OBSERVED.
    fault = (run["fault_records"] or [{}])[0]
    kill_ev = {
        "pid_a": ids[0]["pid"], "pid_b": ids[1]["pid"],
        "birth_a": ids[0]["birth"], "birth_b": ids[1]["birth"],
        "milestones_before": run["milestones_at_crash"],
        "milestones_after": run["milestones_completed"],
        "fault_record": fault,
        "observed_at": _now(),
    }
    (ev_dir / "kill_restart_recovery.json").write_text(json.dumps(kill_ev, indent=2) + "\n")

    # 6. Capability check recomputed live (never authored).
    sys.path.insert(0, str(_REPO / "factory" / "bin"))
    import model_router as mr
    choice = mr.route("A", "build", engine_hint="codex")
    selected = mr.normalize_engine_id(choice.engine)

    # 6b. The unattended-run ledger: DERIVED from observed evidence —
    # human/owner-typed events in the project state during the run window +
    # controlling-TTY observations on every daemon identity (Claude round-1:
    # an authored 0 is not an observation).
    derived_humans = _derive_human_interventions(
        pd, started, [i.get("pid") for i in ids if i.get("pid")])
    run_record = dict(run, **derived_humans)
    (ev_dir / "long_run_result.json").write_text(json.dumps(run_record, indent=2) + "\n")

    import hashlib as _hl
    def _bind(rel_path: str) -> dict:
        p = ev_dir / Path(rel_path).name
        return {"evidence_path": f"factory_v2/receipts/lr5_evidence/{p.name}",
                "evidence_sha256": _hl.sha256(p.read_bytes()).hexdigest()}

    receipt = {
        "schema": "lr5-preflight-receipt/v2",
        "produced_by": "factory_v2.run_lr5_preflight (real harness)",
        "produced_at": _now(),
        "integration_sha": integration_sha,
        "branch_preserved": True,
        "branch_preserved_detail": (
            "external repo ACCEPTED candidate/LR5EXT still exists after the real "
            "dispatcher_v3._run_in_worktree cleanup path (see lr5_evidence/branch_preserved.json)"),
        "capability_policy_check": {
            "spec_level": "A", "task_kind": "build", "hint": "codex",
            "selected_engine": selected, "reason": choice.reason,
        },
        "kill_restart_recovery": "PASS",
        "kill_restart_detail": (
            f"real SIGKILL of factoryd pid {ids[0]['pid']} after "
            f"{run['milestones_at_crash']}; successor pid {ids[1]['pid']} (distinct "
            f"birth identity); {len(run['milestones_completed'])} milestones total "
            "(see lr5_evidence/kill_restart_recovery.json)"),
        "cas_double_dispatch": "REJECTED",
        "cas_double_dispatch_detail": (
            "second promotion of LR5_HARNESS#DOUBLE_DISPATCH rejected CAS_DUPLICATE "
            "(see lr5_evidence/cas_double_dispatch.json)"),
        "routine_human_interventions": derived_humans["routine_human_interventions"],
        "human_interventions_detail": "DERIVED from observed evidence: "
                                      f"{derived_humans['routine_human_interventions']} "
                                      "HUMAN*/OWNER* events in the project ledger "
                                      "during the run window plus controlling-TTY "
                                      "observations on the daemon identities (see "
                                      "long_run_result.json derivation fields)",
        "observations": {
            "kill_restart_recovery": _bind("kill_restart_recovery.json"),
            "cas_double_dispatch": _bind("cas_double_dispatch.json"),
            "branch_preserved": _bind("branch_preserved.json"),
            "controlled_run": _bind("long_run_result.json"),
        },
        "controlled_run_evidence": "factory_v2/receipts/lr5_evidence/long_run_result.json",
        "preflight_evidence": "factory_v2/receipts/lr5_evidence/preflight_result.json",
    }

    # 7. Self-verify in a SEPARATE verifier process; all_pass comes from its verdict.
    if args.publish:
        receipt_path = repo_receipt
        evidence_root = str(_REPO)
    else:
        receipt_path = root / "staged" / "receipt.json"
        evidence_root = str(root / "staged")
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
    v = subprocess.run([sys.executable, "-m", "factory_v2.verify_preflight_receipt",
                        str(receipt_path), "--evidence-root", evidence_root],
                       cwd=str(_REPO), capture_output=True, text=True)
    receipt["all_pass"] = v.returncode == 0
    receipt["verifier_rc"] = v.returncode
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")

    print(json.dumps({
        "status": "HARNESS_COMPLETE" if v.returncode == 0 else "HARNESS_VERIFIER_FAILED",
        "mode": "PUBLISHED" if args.publish else "STAGED",
        "integration_sha": integration_sha,
        "receipt_path": str(receipt_path),
        "milestones": run["milestones_completed"],
        "daemon_identities": ids,
        "restarted": run["restarted"], "crashed": run["crashed"],
        "verifier_rc": v.returncode, "verifier_tail": v.stdout[-400:],
        "tmp_root": str(root), "started_at": started, "completed_at": _now(),
    }, indent=2))
    if not args.keep_tmp:
        import shutil
        shutil.rmtree(root, ignore_errors=True)
    return 0 if v.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
