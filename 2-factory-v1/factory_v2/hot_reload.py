"""Small, deterministic policy for restarting the factory supervisor safely."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

RELOAD_SENSITIVE = (
    "factory_v2/runtime.py",
    "factory_v2/launch_gate.py",
    "factory_v2/recovery_allocation.py",
    "factory_v2/executors.py",
    "factory_v2/mission_compiler.py",
    "factory_v2/run_windows.py",
    "factory_v2/authority.py",
    "factory/bin/factoryd.py",
    # 2026-09-18: imported IN the daemon by review_dispatch (multi-AI review
    # routing) — not only by the dispatcher subprocess
    "factory/bin/model_router.py",
    "factory/bin/owner_routing_policy.py",
)


def reload_needed(touched_files) -> bool:
    """Whether a settled candidate changed daemon code already in memory.

    Any .py under factory_v2/ is daemon code: the advance pipeline
    (factory_v2/advance/*), accounting, resilience... all run inside the daemon
    process. factory/bin/* other than factoryd.py are subprocesses (the
    dispatcher re-reads its code at every launch) and never need a reload.
    """
    touched = set(touched_files or ())
    if touched & set(RELOAD_SENSITIVE):
        return True
    # judges/verifiers (factory_v2/verify_*.py) and benchmarks run as
    # subprocesses from the pinned SHA — not daemon memory
    return any(str(f).endswith(".py") and str(f).startswith("factory_v2/")
               and not Path(str(f)).name.startswith(("verify_", "run_", "test_"))
               for f in touched)


def head_moved_reload_needed(repo_root, boot_head: str) -> "tuple[bool, str, list]":
    """2026-09-17: the daemon only re-loaded after ITS OWN settlements — an
    operator commit at HEAD (a fix to the sweeper, the accounting...) stayed
    invisible until the next factory settlement. Compare the control repo HEAD
    with the one loaded at boot; returns (needed, head_now, touched)."""
    import subprocess
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo_root),
                              capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return False, "", []
    if not head or not boot_head or head == boot_head:
        return False, head, []
    try:
        diff = subprocess.run(["git", "diff", "--name-only", f"{boot_head}..{head}"],
                              cwd=str(repo_root), capture_output=True, text=True,
                              timeout=20).stdout.split()
    except Exception:
        return False, head, []
    return reload_needed(diff), head, diff


def decide(in_flight, pending) -> str:
    """Never restart while work is running; a pending reload otherwise runs now."""
    if in_flight:
        return "WAIT"
    return "RELOAD_NOW" if pending else "NOOP"


# ── FACTORY_RELOAD_CANARY_V1 ──────────────────────────────────────────────────
#
# RELOAD_NOW exits the daemon so launchd restarts it on whatever is at HEAD —
# healthy or not. A commit that does not import (or imports but breaks advance())
# used to convert a routine reload into a factory-wide outage. The canary proves
# the tree FIRST, in a throwaway subprocess, hermetically.

#: Typed failure prefix required by the mission spec and the pinned judge.
RELOAD_CANARY_FAILED = "RELOAD_CANARY_FAILED"

#: The canary child. Run as `python -c <code> <repo_root>` with cwd=repo_root and
#: a PYTHONPATH-free environment, so EVERY factory import resolves from repo_root
#: alone — the daemon's own (in-memory, possibly stale) sys.path is irrelevant,
#: and an inherited PYTHONPATH cannot make a broken tree pass by shadowing it.
_CANARY_CHILD = r'''
import json, os, random, sys, tempfile
from pathlib import Path

repo = Path(sys.argv[1]).resolve()

def _fail(why, code=1):
    print("RELOAD_CANARY_FAILED: " + str(why), file=sys.stderr)
    sys.exit(code)

# Hermetic environment BEFORE any factory import (chaos_drill isolation model:
# the only proven-hermetic way to run advance() without touching the control
# repo or the real run-dir).
work = Path(tempfile.mkdtemp(prefix="factory_reload_canary_"))
import atexit, shutil                                   # noqa: E401,E402
atexit.register(shutil.rmtree, work, True)              # FACTORY_REPO_HYGIENE_V1 : le canari ne laisse rien
os.environ["FACTORY_NO_REAL_ENGINE"] = "1"
os.environ["FACTORY_MAX_WORKERS"] = "1"
os.environ["FACTORY_RUN_DIR"] = str(work / "run")
os.environ["FACTORY_RUNRECORDS_PATH"] = str(work / "runrecords.jsonl")
os.environ["FACTORY_QUOTA_SNAPSHOT_ROOT"] = str(work / "quota")
os.environ["FACTORY_OWNER_AUTHORIZATION_KEY"] = "%064x" % random.getrandbits(256)
for p in (str(repo / "factory" / "bin"), str(repo)):
    if p not in sys.path:
        sys.path.insert(0, p)

try:
    import factory_v2                                   # noqa: F401
except BaseException as exc:                            # noqa: BLE001 — report, never crash through
    _fail("IMPORT: factory_v2: %s: %s" % (type(exc).__name__, exc))
try:
    import factoryd                                     # noqa: F401 — proves the daemon file loads at HEAD
except BaseException as exc:
    _fail("IMPORT: factoryd: %s: %s" % (type(exc).__name__, exc))
try:
    from factory_v2.runtime import advance
except BaseException as exc:
    _fail("IMPORT: factory_v2.runtime.advance: %s: %s" % (type(exc).__name__, exc))

# Supervisor-scoped stores out of the control repo; the audit-cell drain is
# neutralized because under FACTORY_NO_REAL_ENGINE its reviewer fails by
# construction and its drain writes into factory_v2/receipts/ of the repo
# (same rationale as chaos_drill — it is an annex activity, never in scope
# for proving that dispatch+settle works).
try:
    from factory_v2 import recovery_allocation as _ra
    _ra.STORE = work / "recovery_allocations"
except Exception:
    pass
try:
    from factory_v2 import runtime as _rt
    _rt.drain_pending_audit_cell = lambda project_dir: None
except Exception:
    pass

try:
    import yaml
    pd = work / "canary_project"
    (pd / "evidence").mkdir(parents=True)
    (pd / "STRATEGY.md").write_text("# STRATEGY\nNorth Star: reload canary.\n")
    (pd / "SPEC.md").write_text("# SPEC\nCanary throwaway project.\n")
    (pd / "ROADMAP.yaml").write_text(yaml.safe_dump({
        "version": "v3", "project": "reload-canary",
        "final_dod": {"requires_milestones": ["A"]},
        "milestones": [{
            "id": "A", "goal": "hermetic canary milestone",
            "acceptance": ["canary acceptance"],
            "product_e2e": {"test_command": "true"},
            "execution": {"steps": [{"kind": "implement",
                                     "executor": {"type": "local_command",
                                                  "command": "true"}}]},
        }]}))
    (pd / "PROJECT_STATE.json").write_text(json.dumps(
        {"project": "reload-canary", "completed_milestones": {},
         "missions": {}, "events": []}))
except BaseException as exc:
    _fail("SETUP: %s: %s" % (type(exc).__name__, exc))

allowed = {"PROJECT_BUSY", "MISSION_IN_FLIGHT", "MILESTONE_SETTLED"}
actions = []
try:
    for _ in range(8):
        r = advance(pd)
        act = str(getattr(r, "action", ""))
        actions.append(act)
        if act == "PROJECT_COMPLETE":
            sys.stdout.write(json.dumps({"ok": True, "actions": actions}) + "\n")
            sys.stdout.flush()
            sys.exit(0)
        if act not in allowed:
            _fail("UNEXPECTED_ACTION: cycle returned %r (milestone=%r, detail=%r); "
                  "actions=%s" % (act, getattr(r, "milestone", None),
                                  str(getattr(r, "detail", ""))[:200], actions))
except SystemExit:
    raise
except BaseException as exc:
    _fail("EXCEPTION: %s: %s" % (type(exc).__name__, exc))
_fail("NO_PROGRESS: no PROJECT_COMPLETE within %d cycles; actions=%s"
      % (len(actions), actions))
'''


def reload_canary(repo_root, *, python=sys.executable,
                  timeout_s: float = 120) -> "tuple[bool, str]":
    """FACTORY_RELOAD_CANARY_V1 — run one hermetic advance() against the tree at
    ``repo_root`` in a throwaway subprocess, before RELOAD_NOW exits the daemon.

    The child imports ``factory_v2`` AND ``factory/bin/factoryd`` from
    ``repo_root`` only (PYTHONPATH stripped, paths inserted ahead of everything),
    then advances a disposable one-milestone project (``local_command: true``,
    FACTORY_NO_REAL_ENGINE=1) until PROJECT_COMPLETE. Green ⇒ the tree at HEAD
    loads and the core loop settles; launchd can restart onto it.

    Returns ``(ok, why)``. On any failure ``why`` starts with
    ``RELOAD_CANARY_FAILED:`` followed by a typed cause: IMPORT (factory_v2 /
    factoryd / runtime.advance), SETUP, EXCEPTION, UNEXPECTED_ACTION, NO_PROGRESS
    (child-side) ; TIMEOUT, LAUNCH, CHILD_EXIT, NO_RESULT (parent-side).
    """
    repo_root = Path(repo_root)
    if not repo_root.is_dir():
        return False, (f"{RELOAD_CANARY_FAILED}: IMPORT: repo_root is not a "
                       f"directory: {repo_root}")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    try:
        proc = subprocess.run(
            [str(python), "-c", _CANARY_CHILD, str(repo_root)],
            cwd=str(repo_root), env=env, capture_output=True, text=True,
            timeout=max(0.001, float(timeout_s)))
    except subprocess.TimeoutExpired:
        return False, (f"{RELOAD_CANARY_FAILED}: TIMEOUT: canary did not finish "
                       f"within {timeout_s}s — import or advance() hangs at HEAD")
    except OSError as exc:
        return False, (f"{RELOAD_CANARY_FAILED}: LAUNCH: cannot run canary python "
                       f"{python!r}: {exc}")
    if proc.returncode == 0:
        for line in reversed((proc.stdout or "").splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    payload = json.loads(line)
                except ValueError:
                    continue
                if payload.get("ok") is True:
                    return True, ("canary ok: actions="
                                  + ",".join(map(str, payload.get("actions", []))))
        return False, (f"{RELOAD_CANARY_FAILED}: NO_RESULT: canary exited 0 without "
                       f"a result line: {(proc.stdout or '')[-200:]!r}")
    marker = [ln for ln in (proc.stderr or "").splitlines()
              if ln.startswith(RELOAD_CANARY_FAILED)]
    if marker:
        return False, marker[-1][:600]
    tail = ((proc.stderr or "") or (proc.stdout or ""))[-300:]
    return False, (f"{RELOAD_CANARY_FAILED}: CHILD_EXIT rc={proc.returncode}: "
                   f"{tail.strip()!r}")
