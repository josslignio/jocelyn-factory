#!/usr/bin/env python3
"""Claude Code Stop-hook. Modeled on the upstream hooks protocol (code.claude.com/docs/en/hooks,
checked 2026-07-26): stop_hook_active is primary, this hook's own MAX_BLOCKS is secondary, Claude
Code's internal 8-consecutive-block cap is the last resort (see VENDOR_LOCK.json).

Order rule 5.4/§6: the hook never writes campaign state itself — it only decides block/allow and
delegates the actual write (counter + event) to `factoryctl record-stop-event`, which is the sole
writer under flock. This intentionally deviates from the simpler reference snippet in
FACTORY_VENDORED_CODE.md (which wrote its own counter file directly).

Order §6 decision order implemented here: (1) stop_hook_active -> allow ; (2) valid runtime
receipt for HEAD -> allow ; (3) factory/campaigns/CAMPAIGN_STATE already terminal -> allow ;
(4) otherwise -> factoryctl record-stop-event decision=block ; (5) MAX_STOP_BLOCKS_PER_SESSION
reached (factoryctl reports escalated=true) -> allow the exit rather than looping forever (the
outer campaign driver, not this hook, is responsible for turning that into FAIL/WAITING_*).

FACTORYCTL_STATE_DIR defaults to factory/projects/factory-control — a dedicated project state
dir for the campaign's own self-monitoring, kept structurally separate from
factory/campaigns/<id>/ (order §5.2: campaigns/<id>/ holds ONLY missions+deps, managed solely by
campaign_state.Campaign; projects/<p>/ holds per-project factoryctl.Factory state). D-26
(DECISIONS_AUTONOMOUS.md): the two previously shared the same directory and events.jsonl file,
which are two independently hash-chained, incompatible-schema event logs (Campaign's "mission"/
"stage" rows vs. Factory's "actor"/"milestone" rows) — discovered as a real defect the first
time campaign-run and the Stop-hook both wrote there. FACTORYCTL_CAMPAIGN_ID still defaults to
this repo's real running campaign id (factory-v1) so the hook actually gates by default instead
of silently no-op'ing when no env var is exported — both remain overridable for tests or other
campaigns.
"""
import json
import os
import pathlib
import subprocess
import sys

TERMINAL_CAMPAIGN_STATES = {"WAITING_HUMAN_BOSS_GO", "WAITING_INFRA", "WAITING_HUMAN", "FAIL", "DONE"}

# D-29/D-31: Factory.event() and campaign_state.Campaign.event() both auto-commit into this same
# repo (state_commit()), so HEAD advances past the exact SHA a receipt just certified the instant
# the receipt-recording commit (or any later stop_event/bookkeeping commit) lands — a naive exact-
# HEAD-match then blocks forever, chasing its own tail, regardless of whether any code the oracle
# actually exercises (full pytest suite + BOOTSTRAP_LOCK validity) changed. These prefixes are
# provably outside what that oracle reads: campaign/project state+evidence, and prose docs/reports.
# Anything else (source under factory/bin, factory/hooks, tests/, scripts/, bootstrap/) still
# requires a fresh receipt — this is a denylist-by-default allowlist, not a general loophole.
BOOKKEEPING_SAFE_PREFIXES = (
    "factory/projects/",
    "factory/campaigns/",
    "factory/cto/reports/",   # CTO status reports and history — pure reporting, not oracle-exercised code
    "factory/cto/status/",    # CTO status snapshots — pure reporting
    "projects/",
    "reports/",
    "campaigns/",   # benchmark campaign data (results.jsonl, evidence, P5 analysis) — not oracle-exercised code
    "memory/",      # auto-memory files (user/feedback/project/reference notes) — not oracle-exercised code
    "DECISIONS_AUTONOMOUS.md",
    "LIVE_CHECKPOINT.json",  # administrative session checkpoint — pure state tracking, not oracle-exercised code
    "PLAN.md",               # mission planning checkpoint written by spec_level=A Claude sessions (F-track missions)
    "REPORT.json",           # mission progress/outcome checkpoint written by spec_level=A Claude sessions
    # CTO durable-state files — authoritative project memory, not oracle-exercised by pytest.
    # Updating these (e.g. after mission settlement) must not block the session exit.
    "factory/cto/CTOK_CHAIN_STATE.json",
    "factory/cto/CTO_STATE.json",
    "factory/cto/CTO_EVENTS.jsonl",
    "factory/cto/CTO_STATUS_LATEST.md",
)


def _is_ancestor(repo, older_sha, newer_sha):
    cp = subprocess.run(["git", "merge-base", "--is-ancestor", older_sha, newer_sha], cwd=repo)
    return cp.returncode == 0


def _bookkeeping_only_diff(repo, older_sha, newer_sha):
    if older_sha == newer_sha:
        return True
    if not _is_ancestor(repo, older_sha, newer_sha):
        return False
    cp = subprocess.run(["git", "diff", "--name-only", older_sha, newer_sha],
                         cwd=repo, capture_output=True, text=True)
    if cp.returncode != 0:
        return False
    names = [n for n in cp.stdout.splitlines() if n.strip()]
    return all(any(n == prefix or n.startswith(prefix) for prefix in BOOKKEEPING_SAFE_PREFIXES)
               for n in names)


def _classify_stop_v2(root, payload, session_id):
    """Phase 1: classify stop context using typed stop-gate policy.

    Returns (allowed: bool, reason: str, context_type: str).
    Imports lazily to degrade gracefully if factory_v2 is not on sys.path.
    """
    try:
        sys.path.insert(0, str(root))
        from factory_v2.stop_gate_policy import (
            classify_stop_context, allow_session_exit, record_agent_go_request,
        )
    except ImportError:
        return None, "factory_v2.stop_gate_policy unavailable — falling back to legacy gate", "UNKNOWN"

    # Detect agent prose GO requests in the stop payload (transcript snippet may be present).
    transcript = payload.get("transcript", "") or ""
    go_phrases = [
        "tell me when", "shall i continue", "want me to launch", "do you want me to proceed",
        "when you want me to", "let me know if", "should i proceed",
        "waiting for your", "waiting for go", "need your approval",
    ]
    if any(p in transcript.lower() for p in go_phrases):
        project_dir = root / "projects" / "factory"
        record_agent_go_request(project_dir, session_id, transcript[:200])

    project_dir = root / "projects" / "factory"
    # FACTORY_STATE_OUT_OF_REPO_V1: factoryd.pid is runtime state, not repo state — it
    # moved to the run-dir. Prefer the legacy in-repo path IFF it still exists on disk
    # (pre-migration repos / synthetic test repos), else the real run-dir location.
    _legacy_pid_file = root / "factory" / "run" / "factoryd.pid"
    if _legacy_pid_file.exists():
        pid_file = _legacy_pid_file
    else:
        pid_file = (pathlib.Path(os.environ.get("FACTORY_RUN_DIR", "").strip()
                                  or str(pathlib.Path.home() / "Library/Application Support/JocelynFactory/run"))
                    / "factoryd.pid")
    ctx = classify_stop_context(project_dir, factoryd_pid_file=pid_file)
    allowed, reason = allow_session_exit(ctx)
    return allowed, reason, ctx.context_type.value


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    if payload.get("stop_hook_active"):
        sys.exit(0)

    root = pathlib.Path(os.environ.get("CLAUDE_PROJECT_DIR", "."))
    factoryctl = root / "factory" / "bin" / "factoryctl.py"
    repo = os.environ.get("FACTORYCTL_REPO", str(root))
    campaign_id = os.environ.get("FACTORYCTL_CAMPAIGN_ID", "factory-v1")
    state_dir = os.environ.get("FACTORYCTL_STATE_DIR", str(root / "factory" / "projects" / "factory-control"))
    project = os.environ.get("FACTORYCTL_PROJECT", "")
    session_id = payload.get("session_id") or os.environ.get("CLAUDE_SESSION_ID", "unknown")

    # GAP-023: check canonical APP007 campaign state JSON (factoryd's authority),
    # falling back to the legacy CAMPAIGN_STATE text file if present.
    _app007_cs = root / "factory" / "campaigns" / "app007" / "APP007_CAMPAIGN_STATE.json"
    campaign_state = "RUNNING"
    if _app007_cs.exists():
        try:
            _cs_data = json.loads(_app007_cs.read_text())
            campaign_state = _cs_data.get("state", "RUNNING")
        except Exception:
            pass
    else:
        _legacy = root / "factory" / "campaigns" / "CAMPAIGN_STATE"
        if _legacy.exists():
            campaign_state = _legacy.read_text().strip()
    if campaign_state in TERMINAL_CAMPAIGN_STATES:
        sys.exit(0)  # campaign already reached a terminal state

    # Phase 1: typed stop-gate policy (ONE_TIME_EXTERNAL_MUTATION_GATE vs ROUTINE_RUNTIME_CONTINUATION).
    # A valid runtime receipt is NOT campaign completion (VALID_CHECKPOINT_AS_CAMPAIGN_COMPLETE = 0).
    v2_allowed, v2_reason, v2_ctx = _classify_stop_v2(root, payload, session_id)
    if v2_allowed is not None:
        decision = "allow" if v2_allowed else "block"
        reason = f"[{v2_ctx}] {v2_reason}"
        cp = subprocess.run(
            [sys.executable, str(factoryctl), "record-stop-event",
             "--repo", repo, "--state-dir", state_dir,
             "--campaign-id", campaign_id, "--project", project,
             "--session-id", session_id, "--decision", decision, "--reason", reason],
            capture_output=True, text=True,
        )
        result = json.loads(cp.stdout) if cp.stdout.strip() else {"escalated": True, "block_count": 999}
        if decision == "allow" or result.get("escalated"):
            sys.exit(0)
        print(json.dumps({"decision": "block", "reason": reason}))
        sys.exit(0)

    # Fallback: legacy receipt-based gate (preserved for backwards compatibility).
    sha_cp = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True)
    sha = sha_cp.stdout.strip()
    receipt_path = pathlib.Path(state_dir) / "evidence" / (project or campaign_id) / "runtime-receipt.builder.json"
    ok = False
    if receipt_path.exists():
        try:
            r = json.loads(receipt_path.read_text())
            receipt_sha = r.get("candidate_sha")
            ok = r.get("exit_code") == 0 and (
                receipt_sha == sha or _bookkeeping_only_diff(repo, receipt_sha, sha)
            )
        except Exception:
            ok = False

    decision = "allow" if ok else "block"
    reason = "runtime receipt valid" if ok else f"runtime receipt absent/invalid for {sha[:12]}"
    cp = subprocess.run(
        [sys.executable, str(factoryctl), "record-stop-event",
         "--repo", repo, "--state-dir", state_dir,
         "--campaign-id", campaign_id, "--project", project,
         "--session-id", session_id, "--decision", decision, "--reason", reason],
        capture_output=True, text=True,
    )
    result = json.loads(cp.stdout) if cp.stdout.strip() else {"escalated": True, "block_count": 999}

    if decision == "allow" or result.get("escalated"):
        sys.exit(0)
    print(json.dumps({"decision": "block", "reason": reason}))
    sys.exit(0)


if __name__ == "__main__":
    main()
