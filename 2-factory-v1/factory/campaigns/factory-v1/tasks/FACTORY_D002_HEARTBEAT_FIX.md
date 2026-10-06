# FACTORY D002 — Heartbeat Fix in dispatcher_v3.py

## Context

D002 (MEDIUM severity, blocks FOUNDATIONAL_LONG_RUN):
- `factory/bin/dispatcher_v3.py` does NOT write heartbeats during execution
- `factory/lib/heartbeat.py` exists and provides `write_heartbeat(evidence_dir, pid, step, status)`
- `factory/bin/factoryd.py` reads heartbeats to detect stalled workers (via `hb.heartbeat_path(worker["evidence_dir"])`)
- Without heartbeat writes in dispatcher, factoryd cannot distinguish a slow-but-alive worker from a stalled one

## Task

In `factory/bin/dispatcher_v3.py`:

1. Import heartbeat at the top:
   ```python
   from heartbeat import write_heartbeat
   ```

2. Add `write_heartbeat(out_dir, os.getpid(), step_name, "RUNNING")` calls at key execution points:
   - After the run starts (step="start")
   - Before/after each stage in the build loop (step=f"stage_{i}")
   - After tests pass (step="tests_pass")
   - After final verdict (step="verdict")
   
   The `out_dir` is already available in dispatcher_v3 (it's the `--out` directory parent or the evidence dir passed to it).

3. The heartbeat interval should be written at least every 60 seconds during long-running stages. If a stage runs synchronously for >60s without yielding, add a timer or write before and after each stage.

## Acceptance

- `factory/bin/dispatcher_v3.py` imports and calls `write_heartbeat`
- A test `tests/test_dispatcher_heartbeat.py` (or existing test file) verifies that after a dispatcher run, `worker_heartbeat.json` exists in the output directory
- Existing dispatcher tests still pass: `python3 -m pytest tests/test_dispatcher_v3.py -q` (or equivalent)
- `grep -n "write_heartbeat" factory/bin/dispatcher_v3.py` returns ≥3 matches

## Notes

- Do NOT modify `heartbeat.py` — it is QUALIFIED at SHA d6398b21
- `write_heartbeat` signature: `(evidence_dir: str|Path, pid: int, step: str, status: str) -> Path`
- The `out_dir` path in dispatcher_v3 is the directory passed via `--out` flag (receipt path parent)
- Import `os` if not already imported for `os.getpid()`
- This is a Factory infrastructure fix — it goes through the standard candidate/acceptance process
