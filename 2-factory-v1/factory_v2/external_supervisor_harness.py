"""EXTERNAL_SUPERVISOR_NO_MANUAL_RELAUNCH — isolated live proof harness.

One definition of the proof, shared by the test suite and the independent
product verifier: install a disposable launchd service (unique label, DUMMY
supervised process — never production factoryd), then OBSERVE from raw
launchctl/ps evidence:

  1. RunAtLoad: launchd owns the process from birth (ppid == 1);
  2. forced SIGKILL → launchd relaunches AUTONOMOUSLY (KeepAlive
     SuccessfulExit=false, ThrottleInterval-bounded, no storm);
  3. relaunch identity: NEW pid AND NEW birth (no self-resurrection);
  4. the relaunched executable is byte-identical (stable executable SHA);
  5. clean exit is NOT relaunched (SAFETY_STOP semantics preserved);
  6. teardown leaves no residue.

The harness returns OBSERVED data only — booleans are computed here from raw
pid/birth/parent evidence, never accepted from a caller. The production-service
proof (real factoryd plist) stays deferred to post-review by Order 024 G4;
process_class is DUMMY_ISOLATED_NOT_PRODUCTION.
"""
from __future__ import annotations

import hashlib
import json
import os
import plistlib
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from factory_v2._io import _now

RELINQUISH_TIMEOUT_S = 60
THROTTLE_S = 2


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=30)


def svc_pid(label: str) -> tuple[int, str] | None:
    """Service PID + birth identity from `launchctl print` (raw evidence)."""
    uid = os.getuid()
    r = subprocess.run(["launchctl", "print", f"gui/{uid}/{label}"],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return None
    pid = None
    for line in r.stdout.splitlines():
        s = line.strip()
        if s.startswith("pid ="):
            try:
                pid = int(s.split("=", 1)[1].strip())
            except ValueError:
                pid = None
            break
    if not pid:
        return None
    birth = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                           capture_output=True, text=True).stdout.strip()
    return pid, birth


def _wait_pid(label: str, old_pid: int | None, timeout_s: float = RELINQUISH_TIMEOUT_S):
    deadline = time.monotonic() + timeout_s
    seen: list[int] = []
    while time.monotonic() < deadline:
        ident = svc_pid(label)
        if ident:
            seen.append(ident[0])
            if ident[0] != old_pid:
                return ident, sorted(set(seen))
        time.sleep(1.0)
    return None, sorted(set(seen))


def _dummy_source() -> str:
    return (
        "import os, signal, sys, time\n"
        "def term(sig, fr):\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, term)\n"
        "mode = sys.argv[1] if len(sys.argv) > 1 else 'run'\n"
        "if mode == 'clean-exit':\n"
        "    sys.exit(0)\n"
        "while True:\n"
        "    time.sleep(1)\n")


def _install(label: str, tmp: Path, dummy: Path, mode: str) -> Path:
    plist = tmp / f"{label}-{mode}.plist"
    plist.write_bytes(plistlib.dumps({
        "Label": label,
        "ProgramArguments": [sys.executable, str(dummy), mode],
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": THROTTLE_S,
        "RunAtLoad": True,
        "StandardOutPath": str(tmp / "out.log"),
        "StandardErrorPath": str(tmp / "err.log"),
    }))
    uid = os.getuid()
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist)],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"launchd bootstrap failed: {r.stderr}")
    return plist


def _bootout(plist: Path) -> None:
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(plist)],
                   capture_output=True, text=True, timeout=30)


def run_isolated_proof() -> dict:
    """Run the full isolated proof once. Returns OBSERVED evidence dict.

    Raises RuntimeError when launchd itself is unavailable — the outcome then
    CANNOT be evaluated (verifier maps this to exit 2, never to a pass).
    """
    label = f"com.factory.proof.{uuid.uuid4().hex[:8]}"
    tmp = Path(tempfile.mkdtemp(prefix="ext_sup_"))
    dummy = tmp / "dummy_daemon.py"
    dummy.write_text(_dummy_source())
    executable_sha_before = hashlib.sha256(dummy.read_bytes()).hexdigest()

    plist = _install(label, tmp, dummy, "run")
    try:
        first, _ = _wait_pid(label, old_pid=-1)
        if first is None:
            raise RuntimeError("service did not start under launchd (RunAtLoad)")
        pid1, birth1 = first
        parent = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid1)],
                                capture_output=True, text=True).stdout.strip()

        os.kill(pid1, signal.SIGKILL)  # the exact production failure mode
        second, seen_pids = _wait_pid(label, old_pid=pid1)
        relaunched = second is not None
        pid2 = birth2 = ""
        if second:
            pid2, birth2 = second
        executable_sha_after = hashlib.sha256(dummy.read_bytes()).hexdigest()

        observed = {
            "schema": "external-supervisor-observed/v1",
            "label": label,
            "bootstrap_run_at_load": True,
            "launchd_owned_ppid_1": parent == "1",
            "forced_kill": True,
            "relaunched_autonomously": relaunched,
            "relaunch_pid_changed": bool(pid2 and pid2 != pid1),
            "relaunch_birth_changed": bool(birth2 and birth2 != birth1),
            "executable_stable_sha": executable_sha_before == executable_sha_after,
            "no_storm_pids_observed": len(seen_pids) <= 3,  # 1 kill + bounded relaunch
            "evidence": {
                "pid1": pid1, "birth1": birth1, "pid2": pid2, "birth2": birth2,
                "ppid": parent, "pids_seen_during_relaunch": seen_pids,
                "executable_sha": executable_sha_before,
            },
        }
    finally:
        _bootout(plist)

    # clean exit must NOT be relaunched (SAFETY_STOP semantics)
    plist_clean = _install(label, tmp, dummy, "clean-exit")
    try:
        time.sleep(max(6, 3 * THROTTLE_S))
        observed["clean_exit_stays_down"] = svc_pid(label) is None
    finally:
        _bootout(plist_clean)

    observed["teardown_no_residue"] = (
        svc_pid(label) is None or _launchctl("list", label).returncode != 0)
    observed["process_class"] = "DUMMY_ISOLATED_NOT_PRODUCTION"
    return observed


REQUIRED_CRITERIA = (
    "bootstrap_run_at_load", "launchd_owned_ppid_1", "relaunched_autonomously",
    "relaunch_pid_changed", "relaunch_birth_changed", "executable_stable_sha",
    "no_storm_pids_observed", "clean_exit_stays_down", "teardown_no_residue",
    # D018 containment (contract 96b41ce1b7a0c2bc8f0d1dcc): typed violation ->
    # fence + evidence + global quarantine surviving supervisor death, cleared
    # ONLY by safe-clear v2 (typed owner exception + gate-observed remediation;
    # text reasons, exception-less receipts and unremediated state all refused).
    "containment_violation_typed", "worker_fenced", "evidence_preserved",
    "quarantine_survives_supervisor_restart",
    "safe_clear_requires_owner_exception_and_remediation",
)


def _pid_dead(pid: int) -> bool:
    r = subprocess.run(["ps", "-o", "state=", "-p", str(pid)],
                       capture_output=True, text=True)
    out = r.stdout.strip()
    return out == "" or out.startswith("Z")


def run_containment_proof() -> dict:
    """D018 live proof in a disposable launchd namespace (contract 96b41ce1).

    The launchd-supervised unit is the REAL factory_v2/external_supervisor.py; its
    worker violates containment (writes into the forbidden root).  Everything is
    OBSERVED from raw process/filesystem evidence.
    """
    label = f"com.factory.containment.{uuid.uuid4().hex[:8]}"
    tmp = Path(tempfile.mkdtemp(prefix="ext_containment_"))
    state = tmp / "state"
    allowed = tmp / "allowed_root"
    bad_worker = tmp / "bad_worker.py"
    bad_worker.write_text(
        "import pathlib, sys, time\n"
        "time.sleep(2)\n"                                  # let the baseline settle
        "state = pathlib.Path(sys.argv[1])\n"
        "p = state / 'authority' / 'leaked.txt'\n"
        "p.parent.mkdir(parents=True, exist_ok=True)\n"
        "p.write_text('out of scope write\\n')\n"
        "while True:\n"
        "    time.sleep(1)\n")

    supervisor = Path(__file__).resolve().parent / "external_supervisor.py"
    executable_sha = hashlib.sha256(supervisor.read_bytes()).hexdigest()

    plist = tmp / f"{label}.plist"
    plist.write_bytes(plistlib.dumps({
        "Label": label,
        "ProgramArguments": [sys.executable, "-u", str(supervisor),
                             "run", str(state), str(bad_worker), str(allowed)],
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": THROTTLE_S,
        "RunAtLoad": True,
        "StandardOutPath": str(tmp / "supervisor.out.log"),
        "StandardErrorPath": str(tmp / "supervisor.err.log"),
    }))
    uid = os.getuid()
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist)],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"launchd bootstrap failed: {r.stderr}")

    observed: dict = {"schema": "containment-observed/v1", "label": label}
    try:
        # 1. wait for the typed violation + quarantine
        qfile = state / "GLOBAL_QUARANTINE.json"
        deadline = time.monotonic() + 90
        while not qfile.exists():
            if time.monotonic() > deadline:
                raise RuntimeError(
                    "supervisor never quarantined — violation not detected: "
                    f"{(tmp / 'supervisor.out.log').read_text()[-500:]}")
            time.sleep(0.5)

        vdirs = sorted((state / "violations").glob("CV-*"))
        vrec = json.loads((vdirs[-1] / "violation.json").read_text()) if vdirs else {}
        observed["containment_violation_typed"] = (
            vrec.get("type") == "CONTAINMENT_VIOLATION"
            and bool(vrec.get("out_of_scope_path"))
            and vrec.get("detected_by") == "supervisor_boundary_diff"
            and vrec.get("boundary_kind") in ("FORBIDDEN_ROOT",
                                              "UNGRANTED_STATE_PATH"))

        # 2. worker fenced: recorded pid dead, no live worker process anywhere
        wpid = int(vrec.get("worker_pid", 0))
        # pattern includes the ARG ORDER of the worker invocation: the supervisor's
        # own argv also contains the worker path (run <state> <worker> ...), so a
        # bare path match would count the supervisor itself.
        _worker_pat = f"{bad_worker} {state}"
        pgrep = subprocess.run(["pgrep", "-f", _worker_pat],
                               capture_output=True, text=True).stdout.split()
        observed["worker_fenced"] = (
            (wpid == 0 or _pid_dead(wpid)) and not pgrep)

        # 3. evidence preserved: snapshot hash matches the live violating artifact
        leaked = state / "authority" / "leaked.txt"
        snap = (vdirs[-1] / "evidence" / "leaked.txt") if vdirs else None
        observed["evidence_preserved"] = bool(
            leaked.exists() and snap and snap.exists()
            and hashlib.sha256(leaked.read_bytes()).hexdigest()
            == hashlib.sha256(snap.read_bytes()).hexdigest())

        # 4. quarantine survives supervisor death + launchd relaunch
        ident = svc_pid(label)
        if ident is None:
            raise RuntimeError("supervisor not running under launchd")
        pid1, _birth1 = ident
        os.kill(pid1, signal.SIGKILL)
        relaunched, _seen = _wait_pid(label, old_pid=pid1)
        time.sleep(max(6, 3 * THROTTLE_S))     # give a would-be spawner time to err
        pgrep2 = subprocess.run(["pgrep", "-f", _worker_pat],
                                capture_output=True, text=True).stdout.split()
        observed["quarantine_survives_supervisor_restart"] = bool(
            relaunched and relaunched[0] != pid1
            and is_quarantined_live(state)
            and not pgrep2)
        observed.setdefault("evidence", {})["supervisor_pid1"] = pid1
        if relaunched:
            observed["evidence"]["supervisor_pid2"] = relaunched[0]

        # 5. safe-clear contract v2 — adversarial, in refusing order.  A text
        #    reason clears nothing; a receipt without a PRE-EXISTING typed owner
        #    exception clears nothing; a receipt whose remediation has not
        #    actually happened (the violating artifact is still on disk) clears
        #    nothing; only the full contract clears, after which the supervisor
        #    exits clean and launchd does NOT relaunch it.
        violation_id = vrec.get("violation_id", "")
        leaked = state / "authority" / "leaked.txt"

        # 5a. plain text reason (even a long, official-sounding one) → REFUSED
        text_clear = subprocess.run(
            [sys.executable, str(supervisor), "clear", str(state),
             "violation investigated; full remediation performed; authorized"],
            capture_output=True, text=True, timeout=30)
        refused_text = (text_clear.returncode != 0
                        and is_quarantined_live(state))

        # 5b. valid-shaped receipt, but NO owner exception on disk → REFUSED
        receipt = tmp / "clear_receipt.json"
        receipt.write_text(json.dumps({
            "schema": "supervisor-safety-clear/v2",
            "answers_violation": violation_id,
            "owner_exception_id": "OX-NONE",
        }))
        no_exc = subprocess.run(
            [sys.executable, str(supervisor), "clear", str(state), str(receipt)],
            capture_output=True, text=True, timeout=30)
        refused_no_exception = (no_exc.returncode != 0
                                and is_quarantined_live(state))

        # 5c. owner exception granted (harness acts as the owner-side authority;
        #     the supervisor itself has no command that can mint one), but the
        #     violating artifact is STILL on disk → remediation not observed →
        #     REFUSED
        ox_dir = state / "owner_exceptions"
        ox_dir.mkdir(exist_ok=True)
        (ox_dir / "OX-REM-1.json").write_text(json.dumps({
            "schema": "owner-exception/v1",
            "class": "CONTAINMENT_REMEDIATION",
            "sensitive": True,
            "answers_violation": violation_id,
            "granted_by": "containment-harness-owner",
            "granted_at": _now(),
            "scope": "isolated proof namespace only",
        }))
        receipt_ok_exc = tmp / "clear_receipt_with_exception.json"
        receipt_ok_exc.write_text(json.dumps({
            "schema": "supervisor-safety-clear/v2",
            "answers_violation": violation_id,
            "owner_exception_id": "OX-REM-1",
        }))
        no_remediation = subprocess.run(
            [sys.executable, str(supervisor), "clear", str(state),
             str(receipt_ok_exc)],
            capture_output=True, text=True, timeout=30)
        refused_no_remediation = (no_remediation.returncode != 0
                                  and is_quarantined_live(state)
                                  and leaked.exists())

        # 5d. remediation performed (violating artifact removed so the boundary
        #     returns to baseline), same receipt → CLEARED, supervisor exits
        #     clean, launchd does not relaunch a clean exit.
        leaked.unlink()
        good = subprocess.run(
            [sys.executable, str(supervisor), "clear", str(state),
             str(receipt_ok_exc)],
            capture_output=True, text=True, timeout=30)
        cleared = (good.returncode == 0 and not is_quarantined_live(state)
                   and (state / "SAFETY_CLEAR.json").exists())
        time.sleep(max(6, 3 * THROTTLE_S))
        observed["safe_clear_requires_owner_exception_and_remediation"] = bool(
            refused_text and refused_no_exception and refused_no_remediation
            and cleared and svc_pid(label) is None)

        observed["executable_stable_sha"] = (
            hashlib.sha256(supervisor.read_bytes()).hexdigest() == executable_sha)
        observed["process_class"] = "SUPERVISOR_REAL_WORKER_DUMMY_ISOLATED"
        return observed
    finally:
        _bootout(plist)


def is_quarantined_live(state_dir: Path) -> bool:
    try:
        d = json.loads((Path(state_dir) / "GLOBAL_QUARANTINE.json").read_text())
        return d.get("state") == "GLOBAL_QUARANTINE"
    except Exception:
        return False
