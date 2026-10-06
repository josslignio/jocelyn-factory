"""RUN_RECORDS_MINIMUM_WIRED — live recomputation harness.

Spawns REAL subprocess runs of the V2 runtime on a disposable sandbox project,
SIGKILLs one mid-mission, restarts to completion, then the verifier recomputes
the two-phase ledger contract from the append-only file alone:

  - every dispatch wrote PREDICTION BEFORE the attempt (ordering in file);
  - every attempt closed OBSERVATION exactly once (1:1 on attempt_id);
  - a crash between the phases produced exactly one WORKER_DEATH close, once;
  - observations carry engine/duration/authority_hashes/result identity;
  - validated-positive signal exists only via record_settlement records.

The harness builds the sandbox and drives the child processes; all judgment
lives in the verifier, which reads only the ledger + receipts from disk.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

_STRATEGY = "# STRATEGY\nNorth Star: deliver ranked intelligence.\n"
_SPEC = "# SPEC\nThe product answers the canonical query.\n"

_RUNNER = """
import sys, time
sys.path.insert(0, {repo!r})
from pathlib import Path
from factory_v2.runtime import advance
actions = []
deadline = time.monotonic() + 240
while time.monotonic() < deadline:
    r = advance(Path({pdir!r}))
    actions.append(r.action)
    if r.action in ("PROJECT_COMPLETE", "PROJECT_BUSY", "SAFETY_STOP"):
        break
    # MISSION_IN_FLIGHT means a surviving worker still owns the attempt: the
    # honest move is to wait and re-poll, never to force a second dispatch.
    time.sleep(2 if r.action == "MISSION_IN_FLIGHT" else 0.2)
print("CYCLES", actions)
"""


def _roadmap(mid: str, command: str) -> str:
    milestones = [
        {"id": mid + "A",
         "goal": "g", "why_now": "w", "depends_on": {},
         "deliverables": [], "acceptance": ["a"],
         "product_e2e": {"test_command": "true"},
         "execution": {"steps": [{"kind": "implement",
                                  "executor": {"type": "local_command",
                                               "command": command}}]}},
        {"id": mid + "B", "goal": "g", "why_now": "w",
         "depends_on": {mid + "A": {"required_state": "COMPLETE"}},
         "deliverables": [], "acceptance": ["a"],
         "product_e2e": {"test_command": "true"},
         "execution": {"steps": [{"kind": "implement",
                                  "executor": {"type": "local_command",
                                               "command": "true"}}]}},
    ]
    return json.dumps({"schema": "roadmap/v2", "project": mid,
                       "final_dod": {"requires_milestones": [mid + "A", mid + "B"],
                                     "product_definition": "d"},
                       "milestones": milestones})


def run_killed_then_restarted_run(kill_after_s: float = 2.0) -> dict:
    """Drive: kill a real run mid-mission, restart it, return paths for recompute."""
    tmp = Path(tempfile.mkdtemp(prefix="rr_wire_"))
    pdir = tmp / "sandbox"
    pdir.mkdir()
    (pdir / "STRATEGY.md").write_text(_STRATEGY, encoding="utf-8")
    (pdir / "SPEC.md").write_text(_SPEC, encoding="utf-8")
    (pdir / "ROADMAP.yaml").write_text(_roadmap("RRW", f"sleep 8"), encoding="utf-8")
    ledger = tmp / "runrecords.jsonl"
    env = dict(os.environ, FACTORY_RUNRECORDS_PATH=str(ledger))

    child = subprocess.Popen(
        [sys.executable, "-c", _RUNNER.format(repo=str(REPO), pdir=str(pdir))],
        cwd=str(REPO), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, start_new_session=True)
    time.sleep(kill_after_s)  # let it freeze contract + dispatch the sleeping mission
    killed = child.poll() is None
    if killed:
        os.kill(child.pid, signal.SIGKILL)
    child.wait()

    # restart: a COLD process with zero chat memory must complete the run
    restart = subprocess.run(
        [sys.executable, "-c", _RUNNER.format(repo=str(REPO), pdir=str(pdir))],
        cwd=str(REPO), env=env, capture_output=True, text=True, timeout=300)
    return {
        "ledger": str(ledger),
        "pdir": str(pdir),
        "killed_mid_run": killed,
        "restart_rc": restart.returncode,
        "restart_stdout_tail": restart.stdout[-500:],
    }


def read_ledger(ledger: Path) -> list[dict]:
    out = []
    for line in Path(ledger).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out
