"""M0.10 (order §4.3) — self-host REPLAY: proves the external bootstrap oracle
(verify_bootstrap_dod.py, the locked root of trust) still PASSes against this real repo's own
current HEAD, not just against the synthetic fixtures in test_bootstrap_dod.py. This is what
distinguishes "the mechanism works on a toy example" from "the mechanism actually re-verifies
this exact, currently-committed candidate" — replayable on demand, any time HEAD moves, as the
order's own M0.4.3/M0.4.4 root-of-trust discipline requires (never the candidate's own verdict).

D021 (2026-09-09): the inner full suite is an expensive validation with a real wall-time
distribution (~230 s warm, ~600 s cold/detached worktree observed). A fixed 300 s envelope
classified a live-but-slow replay as a failure — an envelope observation masquerading as an
assertion failure. Per Defect 021 §3/§5/§6:
  * the envelope is raised to 1800 s with the taxonomy recorded (adaptive bounds from
    validated RunRecords come later — D021 §6, not a fixed known-too-small value);
  * an envelope expiry is reported as a TYPED HARNESS/INDETERMINATE outcome, never
    presented as an assertion failure;
  * the replay is single-flight keyed on (verifier, lock, HEAD, env): concurrent
    replays attach to the live one instead of starting duplicates (D021 §5).
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from factory_v2 import single_flight  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2].parent
VERIFIER = REPO_ROOT / "factory/bootstrap/verify_bootstrap_dod.py"
LOCK = REPO_ROOT / "factory/bootstrap/BOOTSTRAP_LOCK.json"

# Guard: when verify_bootstrap_dod.py runs the test suite in a subprocess, it
# includes this file. The guard prevents infinite recursion: if the env var is
# already set, this test is running as a sub-invocation and must skip.
_GUARD_VAR = "FACTORY_SELF_HOST_REPLAY_GUARD"
_SUBPROCESS_TIMEOUT_S = 1800  # D021: envelope, not assertion bound (observed p100 ≈ 600 s cold)


def test_self_host_replay_passes_against_real_current_head(tmp_path):
    if os.environ.get(_GUARD_VAR):
        import pytest
        pytest.skip("running inside verify_bootstrap_dod subprocess — recursion guard active")

    # D021 §5: replay depth is already guarded by _GUARD_VAR (depth ≤ 1); the
    # single-flight key additionally prevents two concurrent replays of the same
    # (verifier, lock, HEAD) from both paying the full inner suite.
    single_flight.assert_replay_depth_ok()
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT,
                          capture_output=True, text=True, check=True).stdout.strip()
    key = single_flight.validation_key(
        candidate_sha=head,
        contract_id=str(LOCK),
        suite_manifest_sha="bootstrap-dod",
        verifier_sha=str(VERIFIER),
        env_identity=os.environ.get("FACTORY_ENV_ID", "default"),
    )
    slot = single_flight.acquire(REPO_ROOT, key, command=["self-host-replay"],
                                 cwd=str(REPO_ROOT), envelope_s=_SUBPROCESS_TIMEOUT_S,
                                 owner_note="pytest self-host replay")
    if not slot["owned"]:
        if slot.get("reuse"):
            # An identical replay already PROVED PASS for this exact identity.
            assert slot["job"]["result"]["classified"] == "PASS", (
                f"previous identical replay settled {slot['job']['result']}")
            return
        import pytest
        pytest.skip(f"an equivalent self-host replay is RUNNING (pid "
                    f"{slot['job'].get('pid')}) — D021 single-flight attach")

    proof_dir = tmp_path / "m0_self_host_replay_evidence"
    env = {**os.environ, _GUARD_VAR: "1"}
    t0 = time.monotonic()
    cp = None
    timed_out = False
    single_flight.mark_running(REPO_ROOT, key, os.getpid())
    try:
        cp = subprocess.run(
            [sys.executable, str(VERIFIER), "--repo", str(REPO_ROOT), "--candidate-sha", head,
             "--proof-dir", str(proof_dir), "--lock", str(LOCK)],
            capture_output=True, text=True,
            env=env,
            timeout=_SUBPROCESS_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        duration = round(time.monotonic() - t0, 1)
    try:
        if timed_out:
            # D021 §3 taxonomy: envelope expiry is an observation about the harness
            # envelope. classify() with no live child and no progress data is
            # INDETERMINATE — reported as a TYPED harness outcome, never as an
            # assertion failure, and never green.
            classified = single_flight.classify(124, envelope_expired=True,
                                                child_alive=False, child_progressed=False)
            single_flight.settle(REPO_ROOT, key, rc=124, classified=classified,
                                 duration_s=duration)
            import pytest
            pytest.fail(
                f"SELF_HOST_REPLAY_{classified}: inner replay exceeded the "
                f"{_SUBPROCESS_TIMEOUT_S}s envelope after {duration}s — D021 timeout "
                "taxonomy: this is a harness/envelope observation, NOT an assertion "
                "failure; inspect duration history before any retry")
        single_flight.settle(REPO_ROOT, key, rc=cp.returncode,
                             classified=single_flight.classify(cp.returncode),
                             duration_s=duration,
                             evidence_path=str(proof_dir))
        assert cp.returncode == 0, f"self-host replay failed at HEAD {head}:\n{cp.stdout}\n{cp.stderr}"
        pass_file = proof_dir / "M0_SELF_HOST_PASS.json"
        assert pass_file.exists()
        receipt = json.loads(pass_file.read_text())
        assert receipt["all_pass"] is True
        assert receipt["candidate_sha"] == head
        assert all(receipt["checks"].values()), receipt["checks"]
    except Exception:
        # never leave a RUNNING job behind for an aborted replay
        job = single_flight._read(single_flight._job_path(REPO_ROOT, key)) or {}
        if job.get("state") == "RUNNING":
            single_flight.settle(REPO_ROOT, key, rc=1, classified="PROCESS_LOST",
                                 duration_s=duration)
        raise
