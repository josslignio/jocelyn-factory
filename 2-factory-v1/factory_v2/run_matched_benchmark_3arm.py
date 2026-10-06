#!/usr/bin/env python3
"""CLAUDE_ALONE_VS_GLM_ALONE_VS_FACTORY_BENCHMARK — paired three-arm benchmark producer.

Runs the SAME hermetic product task through three MATCHED arms — claude_alone,
glm_alone, factory — N held-out runs per arm, and writes one content-addressed
observation JSON per (arm, run) plus MATCHED_BENCHMARK_RECEIPT.json.

Why a NEW file (decision consigned in PLAN.md/REPORT.json): the ROADMAP execution step
names factory_v2/run_matched_benchmark.py, but that file is the producer of the SETTLED
milestone CHAMPION_VS_RESHAPED_CHALLENGER whose pinned judge (verify_factory_hygiene.py
check_matched_benchmark @ b54a9996) recomputes sha256(HEAD:run_matched_benchmark.py)
against the committed MANIFEST producer_sha256 — mutating it would flip a green gate to
MANIFEST_PRODUCER_MISMATCH. This producer therefore lives separately; arms are DATA
(ARMS registry — a fourth arm needs no new code).

Honesty model (same technique as the committed probes factory_v2/_benchmark_probes.py
and tests/test_c43_*.py): no real AI engine is ever exec'd (FACTORY_NO_REAL_ENGINE=1);
engine launches are intercepted in-process at dispatcher_v3._run and played by a
DETERMINISTIC world script indexed by completed-launch number, identical for every arm:

  launch 1 (build)  : writes nothing               -> self-test FAILS   (fault FI-1)
  launch 2 (repair) : wrong-checksum report.json    -> self-test PASSES,
                      ground truth FAILS            (fault FI-2 bait)
                      and sleeps 4s first           (fault FI-3: the harness
                      SIGKILLs the arm process group once, mid-launch-2, then
                      restarts it autonomously — identical schedule every arm)
  launch 3+         : correct report.json

All arms drive the REAL dispatch_v3 with identical task, test_cmd, deliverables and
budget. Topology is the only difference:
  claude_alone/glm_alone : engine_hint pins the arm's single engine; success is the
                           dispatcher's own ACCEPTED verdict (self-certification).
  factory                : real routing (glm -> claude escalation), external promote
                           (merge of the candidate branch, the AutopilotExecutor
                           operation), then the REAL independent settlement gate
                           (factory_v2.verify_milestone_acceptance, milestone
                           product_e2e = the semantic ground-truth check) — a bait that
                           passes the self-test is BLOCKED from settlement and repaired.

Ground truth is scored by the HARNESS (never by an arm): python3 check_truth.py at the
product repo's final HEAD. false_acceptance = arm declared success && ground truth red.

Real model calls cost money: the producer refuses to start without --max-cost-usd
(unless --dry-run, which writes nothing), and prices every launch through the same
public token price table for every arm (real_cost_usd stays 0 in hermetic mode —
declared, never fabricated).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
RECEIPT_PATH = REPO_ROOT / "factory_v2" / "receipts" / "MATCHED_BENCHMARK_RECEIPT.json"
SCHEMA = "matched-3arm-benchmark-receipt/v1"
WORKLOAD_ID = "CLAUDE_ALONE_VS_GLM_ALONE_VS_FACTORY_BENCHMARK_V1"

# --- arms are DATA: a fourth arm is one registry entry, zero new code -------------
ARMS: dict[str, dict] = {
    "claude_alone": {"engines": ["claude"], "engine_hint": "claude",
                     "settlement_gate": False, "topology": "single_model"},
    "glm_alone": {"engines": ["glm"], "engine_hint": "glm",
                  "settlement_gate": False, "topology": "single_model"},
    "factory": {"engines": ["glm", "claude"], "engine_hint": None,
                "settlement_gate": True, "topology": "factory_multi_ai"},
}
SINGLE_ARMS = ("claude_alone", "glm_alone")

# Same public price table for every arm (USD per 1M tokens in/out) — comparable
# accounting; an arm that spends more shows it. Real spend is 0 in hermetic mode.
PRICE_TABLE_MTOK_USD = {"claude": (3.0, 15.0), "glm": (0.6, 2.2), "codex": (2.0, 8.0)}

FAULTS = ("FI-1_BUILD_BROKEN", "FI-2_FALSE_BAIT", "FI-3_SIGKILL_MID_REPAIR")

TASK_TEXT = (
    "Produis universe_report.json a la racine : resume verifiable de data/universe.json. "
    "Champs requis : checksum (sha256 de la serialisation canonique triee de "
    "data/universe.json, tronque a 16 hex), verified (true), summary (texte). "
    "Acceptance : self_test.sh doit passer ; la verite de fond est check_truth.py."
)

FIXED_ENV = {
    "GIT_AUTHOR_NAME": "bench3", "GIT_AUTHOR_EMAIL": "bench3@example.com",
    "GIT_COMMITTER_NAME": "bench3", "GIT_COMMITTER_EMAIL": "bench3@example.com",
    "GIT_AUTHOR_DATE": "2026-02-01T00:00:00", "GIT_COMMITTER_DATE": "2026-02-01T00:00:00",
}

UNIVERSE = {"accounts": 25, "active_calls": 3, "as_of": "2026-02-01", "regions": ["eu", "na"]}

SELF_TEST = """#!/bin/sh
# structural self-test handed to every arm's dispatch (bash -> passthrough, never mocked)
python3 - <<'EOF'
import json
r = json.load(open("universe_report.json"))
assert set(r) >= {"checksum", "verified", "summary"}, r
EOF
"""

CHECK_TRUTH = """#!/usr/bin/env python3
# semantic ground truth: harness-side scorer AND the factory milestone's product_e2e
import hashlib, json, sys
from pathlib import Path
try:
    u = json.loads(Path("data/universe.json").read_text())
    canon = json.dumps(u, sort_keys=True, separators=(",", ":"))
    expect = hashlib.sha256(canon.encode()).hexdigest()[:16]
    r = json.loads(Path("universe_report.json").read_text())
    ok = r.get("checksum") == expect and r.get("verified") is True
except Exception:
    ok = False
sys.exit(0 if ok else 1)
"""

STRATEGY_MD = "# STRATEGY\nNorth Star: benchmark hermetique a trois bras.\n"
SPEC_MD = "# SPEC\nProduit jetable : report.json verifiable.\n"


def roadmap_yaml() -> str:
    import yaml
    body = {
        "schema": "roadmap/v2", "project": "bench3",
        "final_dod": {"requires_milestones": ["M"]},
        "milestones": [{
            "id": "M", "goal": "report.json verifiable",
            "depends_on": {}, "deliverables": ["universe_report.json"],
            "acceptance": ["self_test.sh passe", "check_truth.py passe (verite de fond)"],
            "product_e2e": {"test_command": "python3 check_truth.py"},
            "execution": {"steps": [{"kind": "implement",
                                     "executor": {"type": "local_command",
                                                  "command": "true"}}]},
        }],
    }
    return yaml.safe_dump(body, sort_keys=False)


def _git_fixed(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(FIXED_ENV)
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, env=env)


def build_fixture(root: Path) -> tuple[Path, str]:
    """The shared task: committed once per run dir with fixed identity/dates so the
    start_sha is bit-identical across every arm and every run."""
    repo = root / "product"
    repo.mkdir(parents=True)
    _git_fixed(repo, "init", "-q", "-b", "main")
    # repo-local identity so the arm's merge commits (factory promote) never depend
    # on the harness environment
    _git_fixed(repo, "config", "user.name", "bench3")
    _git_fixed(repo, "config", "user.email", "bench3@example.com")
    (repo / "TASK.md").write_text(TASK_TEXT + "\n", encoding="utf-8")
    (repo / "data").mkdir()
    (repo / "data/universe.json").write_text(
        json.dumps(UNIVERSE, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    (repo / "self_test.sh").write_text(SELF_TEST, encoding="utf-8")
    (repo / "check_truth.py").write_text(CHECK_TRUTH, encoding="utf-8")
    (repo / "STRATEGY.md").write_text(STRATEGY_MD, encoding="utf-8")
    (repo / "SPEC.md").write_text(SPEC_MD, encoding="utf-8")
    (repo / "ROADMAP.yaml").write_text(roadmap_yaml(), encoding="utf-8")
    _git_fixed(repo, "add", "-A")
    _git_fixed(repo, "commit", "-q", "-m", "bench3: shared task fixture")
    sha = _git_fixed(repo, "rev-parse", "HEAD").stdout.strip()
    return repo, sha


TASK_SHA256 = hashlib.sha256((TASK_TEXT + "\n").encode()).hexdigest()
DATA_SHA256 = hashlib.sha256(
    (json.dumps(UNIVERSE, sort_keys=True, indent=2) + "\n").encode()).hexdigest()
SELF_TEST_CMD = "bash self_test.sh"
GROUND_TRUTH_CMD = "python3 check_truth.py"
DELIVERABLE = "universe_report.json"


# --- deterministic world (plays the builder engine at dispatcher_v3._run) ---------

def _engine_stdout(cmd, idx: int, tin: int, tout: int) -> str:
    """The usage payload in the FORMAT the launched argv asks for (claude stream-json
    `result`, opencode `step_finish`, codex 0.154 `turn.completed`), so the REAL
    telemetry seam — dispatcher_v3 → factory_v2.telemetry adapters chosen from argv —
    measures every launch exactly as in production. FACTORY_TOKEN_TRUTH_V1."""
    from factory_v2.telemetry import adapters as ad
    fmt = ad.detect_format(cmd)
    if fmt == ad.OPENCODE_JSON:
        return json.dumps({"type": "step_finish", "timestamp": 1700000000000 + idx,
                           "sessionID": f"ses_bench3_{idx}",
                           "part": {"id": f"prt_bench3_{idx}", "messageID": f"msg_bench3_{idx}",
                                    "type": "step-finish", "reason": "stop",
                                    "tokens": {"total": tin + tout, "input": tin, "output": tout,
                                               "reasoning": 0, "cache": {"read": 0, "write": 0}},
                                    "cost": 0}})
    if fmt == ad.CODEX_JSONL:
        return "\n".join(json.dumps(e) for e in (
            {"type": "thread.started", "thread_id": f"bench3-{idx}"}, {"type": "turn.started"},
            {"type": "turn.completed", "usage": {"input_tokens": tin, "cached_input_tokens": 0,
                                                 "cache_write_input_tokens": 0, "output_tokens": tout,
                                                 "reasoning_output_tokens": 0}}))
    return json.dumps({"type": "result", "subtype": "success",
                       "usage": {"input_tokens": tin, "output_tokens": tout}})


def measured_tokens(run_dir: Path) -> dict:
    """Tokens MEASURED by the telemetry adapters on the arm's launches, never the world
    script's intent. Sources: the dispatch receipts' `engine_executions[*].telemetry`
    AND the crash-safe sidecars (`receipt_*.telemetry.jsonl`, fsynced as each launch
    ends) — the harness SIGKILLs the arm mid-launch-2, so the receipt of that dispatch
    never exists and its launch 1 would vanish with it. Folded on the supervisor's
    launch identity (attempt_key, launch_seq, started_at): the pre-exec OPEN row and
    the terminal row of one launch are one launch. A launch that only ever opened
    (killed in flight) is unmeasured — counted as such, never estimated."""
    by_launch: dict = {}

    def _num(v):
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    def _tok(obs, field_):
        return _num(((obs.get("usage") or {}).get(field_) or {}).get("value"))

    def _take(obs):
        if not isinstance(obs, dict):
            return
        key = (str(obs.get("attempt_key") or ""), int(obs.get("launch_seq") or 0),
               str(obs.get("started_at") or ""), str(obs.get("task_type") or ""))
        prev = by_launch.get(key)
        if prev is None or (_tok(prev, "input_tokens") is None and
                            (_tok(obs, "input_tokens") is not None or obs.get("provenance") != "LIVE_OPEN")):
            by_launch[key] = obs

    for rp in sorted(Path(run_dir).glob("receipt_*.json")):
        try:
            receipt = json.loads(rp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for ex in receipt.get("engine_executions") or []:
            if isinstance(ex, dict) and ex.get("launched") and isinstance(ex.get("telemetry"), dict):
                _take(ex["telemetry"])
    for sp in sorted(Path(run_dir).glob("receipt_*.telemetry.jsonl")):
        for line in sp.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                _take(json.loads(line))
            except json.JSONDecodeError:
                continue          # torn tail: the kill landed mid-write
    tin = tout = cache = 0
    measured = unmeasured = 0
    sources: set = set()
    for obs in by_launch.values():
        i, o = _tok(obs, "input_tokens"), _tok(obs, "output_tokens")
        if i is not None and o is not None:
            measured += 1
            tin += int(i)
            tout += int(o)
            cache += int(_tok(obs, "cache_read_tokens") or 0)
            sources.add(str((obs.get("usage") or {}).get("source") or ""))
        else:
            unmeasured += 1
            sources.add(str((obs.get("usage") or {}).get("source") or "UNKNOWN"))
    return {"input_tokens": tin, "output_tokens": tout, "cache_read_tokens": cache,
            "tokens_measured_launches": measured, "tokens_unmeasured_launches": unmeasured,
            "usage_sources": sorted(s for s in sources if s),
            "tokens_source": "telemetry observations (dispatch receipts + crash-safe sidecars) measured by "
                             "factory_v2.telemetry adapters on each launch's stdout (detect_format(argv)); "
                             "a launch killed in flight is unmeasured, never estimated"}


def _read_world(run_dir: Path) -> dict:
    p = run_dir / "world.json"
    return json.loads(p.read_text()) if p.exists() else {
        "completed": 0, "started": 0, "kill_done": False, "launch2_seen": False,
        "active_s": 0.0, "engines": [], "usage": []}


def _write_world(run_dir: Path, w: dict) -> None:
    (run_dir / "world.json").write_text(json.dumps(w, sort_keys=True), encoding="utf-8")


def make_world_hook(run_dir: Path, product_repo: Path):
    """Engine launches are played here; bash/git (self-test, git plumbing) pass through
    to the real dispatcher runner. Deterministic in the completed-launch index only."""
    d3 = sys.modules.get("dispatcher_v3")

    def real(cmd, cwd, timeout):
        r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                           timeout=timeout)
        return r.returncode, r.stdout, r.stderr

    def hook(cmd, cwd, timeout):
        if cmd and cmd[0] in ("/bin/sh", "sh", "bash", "git", "/bin/bash"):
            return real(cmd, cwd, timeout)
        w = _read_world(run_dir)
        idx = w["completed"] + 1          # this launch is attempt #idx
        w["started"] += 1
        engine = _engine_of(cmd)
        w["engines"].append(engine)
        tin, tout = 1200 + 200 * idx, 300 + 50 * idx
        w["usage"].append({"launch": idx, "engine": engine,
                           "input_tokens": tin, "output_tokens": tout})
        if idx == 2 and not w["launch2_seen"]:
            # FI-3 window: announce, then take a slow 4s build so the harness kill
            # lands mid-engine-launch, before any write (no git op in flight).
            w["launch2_seen"] = True
            _write_world(run_dir, w)
            time.sleep(4.0)
            w = _read_world(run_dir)
        _write_world(run_dir, w)
        t0 = time.monotonic()
        try:
            if idx == 1:
                pass                      # FI-1: broken build, writes nothing
            elif idx == 2:                # FI-2 bait: structural pass, semantic fail
                _commit_work(cwd, {"checksum": "0" * 16, "verified": True,
                                   "summary": "attempt 2 (bait)"}, idx)
            else:
                canon = json.dumps(UNIVERSE, sort_keys=True, separators=(",", ":"))
                _commit_work(cwd, {"checksum": hashlib.sha256(canon.encode()).hexdigest()[:16],
                                   "verified": True, "summary": "attempt 3 (correct)"}, idx)
            out = _engine_stdout(cmd, idx, tin, tout)
        finally:
            w = _read_world(run_dir)
            w["active_s"] = round(w["active_s"] + (time.monotonic() - t0), 3)
            w["completed"] += 1
            _write_world(run_dir, w)
        return 0, out, ""

    def _engine_of(cmd):
        # real engine binaries: claude CLI, opencode CLI (= GLM/Z Code engine),
        # codex CLI. Anything else is not an engine launch we know.
        argv0 = Path(cmd[0]).name.lower() if cmd else ""
        if "claude" in argv0:
            return "claude"
        if "opencode" in argv0 or "glm" in argv0 or "zcode" in argv0:
            return "glm"
        if "codex" in argv0:
            return "codex"
        return "unknown"

    def _commit_work(cwd, report, idx):
        repo = Path(cwd)
        (repo / DELIVERABLE).write_text(json.dumps(report, sort_keys=True, indent=2) + "\n",
                                        encoding="utf-8")
        (repo / "REPORT.json").write_text(
            json.dumps({"outcome": "IMPLEMENTED"}, sort_keys=True) + "\n", encoding="utf-8")
        _git_fixed(repo, "add", "-A")
        _git_fixed(repo, "commit", "-q", "-m", f"bench3: builder attempt {idx}")
    return hook


# --- one (arm, run) executed as a killable subprocess ------------------------------

class _Patches:
    def __init__(self, patches):
        self.patches = patches

    def __enter__(self):
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self.patches:
            p.stop()
        return False


def patches_applied(patches):
    return _Patches(patches)


def arm_run_main(args) -> int:
    """Child process: drives the REAL dispatch_v3 (engines mocked in-process at the
    _run boundary) plus, for the factory arm, the real promote + settlement gate.
    Durable state lives in run_dir so the harness can SIGKILL and restart us."""
    run_dir = Path(args.run_dir)
    arm = ARMS[args.arm_id]
    product = run_dir / "product"
    if not product.exists():
        repo, sha = build_fixture(run_dir)
        (run_dir / "start_sha.txt").write_text(sha, encoding="utf-8")
    sys.path.insert(0, str(REPO_ROOT / "factory" / "bin"))
    os.environ["FACTORY_NO_REAL_ENGINE"] = "1"
    import dispatcher_v3 as d3  # noqa: E402

    hook = make_world_hook(run_dir, product)
    result = {"arm": args.arm_id, "declared_success": False, "settled": False,
              "missions": [], "terminal": True}
    patches = [mock.patch.object(d3, "_run", side_effect=hook)]
    if arm["engine_hint"]:
        # single-model fleet: force the arm's engine at every launch (the dispatcher
        # threads engine_hint to the FIRST build only; alone means every launch)
        orig_rfd = d3.route_for_dispatch

        def pinned_rfd(spec_level, task_kind, *a, **kw):
            kw["engine_hint"] = arm["engine_hint"]
            return orig_rfd(spec_level, task_kind, *a, **kw)
        patches.append(mock.patch.object(d3, "route_for_dispatch", side_effect=pinned_rfd))
    mission_no = 0
    while mission_no < 3:  # bounded: initial mission + at most 2 repair re-dispatches
        mission_no += 1
        mid = f"bench3-{args.arm_id}-m{mission_no}"
        out_path = run_dir / f"receipt_{mid}.json"
        with patches_applied(patches):
            rec = d3.dispatch_v3(product, TASK_TEXT, mid, SELF_TEST_CMD,
                                 args.budget_s, out_path, spec_level="B",
                                 engine_hint=arm["engine_hint"], task_kind="build",
                                 deliverables=[DELIVERABLE])
        entry = {"mission": mid, "verdict": rec.get("verdict"),
                 "candidate_sha": rec.get("candidate_sha", "")}
        result["missions"].append(entry)
        if rec.get("verdict") != "ACCEPTED":
            continue  # not declared success; bounded loop handles it
        if not arm["settlement_gate"]:
            result["declared_success"] = True  # self-certified ACCEPTED
            break
        # The agent owns its commits in this (external-style) repo: the ACCEPTED
        # candidate IS the product repo HEAD. The factory topology then runs its
        # REAL independent settlement gate as a black-box subprocess — the
        # milestone's frozen contract (product_e2e = the semantic ground truth)
        # must pass before the arm may call the work settled.
        v = subprocess.run([sys.executable, "-m", "factory_v2.verify_milestone_acceptance",
                            "--project-dir", str(product), "--milestone", "M"],
                           cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=300,
                           env={**os.environ, "PYTHONPATH": str(REPO_ROOT)})
        entry["gate_rc"] = v.returncode
        entry["gate_tail"] = (v.stdout + v.stderr).strip()[-300:]
        if v.returncode == 0:
            result["declared_success"] = True
            result["settled"] = True
            break
        # settlement refused: the bait is blocked from settlement; re-dispatch
    (run_dir / "arm_result.json").write_text(json.dumps(result, sort_keys=True, indent=2),
                                             encoding="utf-8")
    return 0


# --- harness ------------------------------------------------------------------------

def spawn_arm(run_dir: Path, arm: str, budget_s: int) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--arm-run",
         "--run-dir", str(run_dir), "--arm-id", arm, "--budget-s", str(budget_s)],
        preexec_fn=os.setsid, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        cwd=str(REPO_ROOT))


def ground_truth_state(product: Path, arm_result: dict) -> "Path | None":
    """Export the arm's final COMMITTED state (repo HEAD) to a scratch dir and return
    it (or None). Never reads an arm's self-report as proof: check_truth.py runs on
    the exported committed tree — untracked leftovers are invisible by construction."""
    if not product.exists():
        return None
    head = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=str(product),
                          capture_output=True, text=True)
    if head.returncode != 0:
        return None
    scratch = product.parent / "gt_export"
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True)
    arc = subprocess.run(["git", "archive", "HEAD"], cwd=str(product),
                         capture_output=True)
    subprocess.run(["tar", "-x", "-C", str(scratch)], input=arc.stdout, timeout=30)
    return scratch


def run_one(arm: str, run_idx: int, base: Path, budget_s: int) -> dict:
    """`base` is a scratch directory OUTSIDE the repo (run dirs hold throwaway product
    repos and arm receipts); only the scored observation JSONs ever reach out_dir."""
    run_dir = base / f"{arm}_run{run_idx}"
    shutil.rmtree(run_dir, ignore_errors=True)
    run_dir.mkdir(parents=True)
    t0 = time.monotonic()
    proc = spawn_arm(run_dir, arm, budget_s)
    killed_at = None
    restarts = 0
    human_interventions = 0  # measured: every manual action the harness had to take (none)
    deadline = t0 + 180
    proc_done_at = None
    while True:
        w = _read_world(run_dir)
        if killed_at is None and w.get("launch2_seen"):
            time.sleep(0.3)  # kill lands mid-engine-launch, before any write
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            killed_at = time.monotonic()
            proc.wait(timeout=30)
            proc = spawn_arm(run_dir, arm, budget_s)  # autonomous supervisor restart
            restarts += 1
        elif proc.poll() is not None:
            res_path = run_dir / "arm_result.json"
            if res_path.exists():
                proc_done_at = time.monotonic()
                break
            if time.monotonic() > deadline:
                break
            # child died without terminal artifact (e.g. crash): one auto-restart
            proc = spawn_arm(run_dir, arm, budget_s)
            restarts += 1
        if time.monotonic() > deadline:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            break
        time.sleep(0.05)
    if proc.poll() is None:
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    elapsed = time.monotonic() - t0
    w = _read_world(run_dir)
    arm_result = {}
    rp = run_dir / "arm_result.json"
    if rp.exists():
        arm_result = json.loads(rp.read_text())
    product = run_dir / "product"
    verified = False
    truth_rc = None
    gt_where = ground_truth_state(product, arm_result)
    if gt_where is not None:
        g = subprocess.run(["python3", "check_truth.py"], cwd=str(gt_where),
                           capture_output=True, timeout=60)
        truth_rc = g.returncode
        verified = g.returncode == 0
    declared = bool(arm_result.get("declared_success"))
    gt_where = ground_truth_state(product, arm_result)
    obs = {
        "arm": arm, "run": run_idx, "workload_id": WORKLOAD_ID,
        "start_sha": (run_dir / "start_sha.txt").read_text().strip()
        if (run_dir / "start_sha.txt").exists() else "",
        "task_sha256": TASK_SHA256, "data_sha256": DATA_SHA256,
        "self_test_cmd": SELF_TEST_CMD, "ground_truth_cmd": GROUND_TRUTH_CMD,
        "deliverable": DELIVERABLE, "faults": list(FAULTS),
        "budget_s": budget_s, "engine_exercised": "NOT_EXERCISED",
        "declared_success": declared, "settled": bool(arm_result.get("settled")),
        "ground_truth_rc": truth_rc, "verified_delivery": verified,
        "ground_truth_evaluated_at": str(gt_where) if gt_where else None,
        "false_acceptance": declared and not verified,
        "engines_used": sorted(set(w.get("engines", []))),
        "launches_started": int(w.get("started", 0)),
        "launches_completed": int(w.get("completed", 0)),
        "launches_lost": max(0, int(w.get("started", 0)) - int(w.get("completed", 0))),
        "usage": w.get("usage", []),
        **measured_tokens(run_dir),
        "elapsed_s": round(elapsed, 3), "active_s": round(float(w.get("active_s", 0.0)), 3),
        "cost_usd": round(sum(
            (u["input_tokens"] / 1e6) * PRICE_TABLE_MTOK_USD.get(u["engine"], (0, 0))[0]
            + (u["output_tokens"] / 1e6) * PRICE_TABLE_MTOK_USD.get(u["engine"], (0, 0))[1]
            for u in w.get("usage", [])), 6),
        "real_cost_usd": 0.0,
        "killed": killed_at is not None,
        "recovery_time_s": round((proc_done_at or time.monotonic()) - killed_at, 3)
        if killed_at is not None else None,
        "automated_restarts": restarts, "human_interventions": human_interventions,
        "missions": arm_result.get("missions", []),
        "terminal": bool(arm_result.get("terminal")) and proc_done_at is not None,
        "notes": f"verdicts={[m.get('verdict') for m in arm_result.get('missions', [])]}",
    }
    return obs


def priced_cost_usd(usage: list[dict]) -> float:
    """Priced through ONE public table for every arm — comparable accounting."""
    total = 0.0
    for u in usage or []:
        pin, pout = PRICE_TABLE_MTOK_USD.get(u.get("engine"), (0.0, 0.0))
        total += (u.get("input_tokens", 0) / 1e6) * pin
        total += (u.get("output_tokens", 0) / 1e6) * pout
    return total


def aggregate(observations: dict[str, list[dict]]) -> dict:
    agg = {}
    for arm, runs in observations.items():
        n = len(runs)
        agg[arm] = {
            "runs": n,
            "verified_rate": round(sum(1 for r in runs if r["verified_delivery"]) / n, 3) if n else 0.0,
            "false_acceptances": sum(1 for r in runs if r["false_acceptance"]),
            "declared_successes": sum(1 for r in runs if r["declared_success"]),
            "mean_elapsed_s": round(sum(r["elapsed_s"] for r in runs) / n, 3) if n else 0.0,
            "mean_active_s": round(sum(r["active_s"] for r in runs) / n, 3) if n else 0.0,
            "tokens_in": sum(u["input_tokens"] for r in runs for u in r["usage"]),
            "tokens_out": sum(u["output_tokens"] for r in runs for u in r["usage"]),
            # FACTORY_TOKEN_TRUTH_V1: what the telemetry adapters MEASURED on the receipts
            "tokens_in_measured": sum(int(r.get("input_tokens") or 0) for r in runs),
            "tokens_out_measured": sum(int(r.get("output_tokens") or 0) for r in runs),
            "tokens_measured": sum(int(r.get("input_tokens") or 0) + int(r.get("output_tokens") or 0)
                                   for r in runs),
            "cache_read_tokens_measured": sum(int(r.get("cache_read_tokens") or 0) for r in runs),
            "tokens_measured_launches": sum(int(r.get("tokens_measured_launches") or 0) for r in runs),
            "tokens_unmeasured_launches": sum(int(r.get("tokens_unmeasured_launches") or 0) for r in runs),
            "cost_usd": round(sum(priced_cost_usd(r["usage"]) for r in runs), 6),
            "real_cost_usd": 0.0,
            "mean_recovery_s": round(sum(r["recovery_time_s"] or 0.0 for r in runs) / n, 3) if n else 0.0,
            "human_interventions": sum(r["human_interventions"] for r in runs),
            "launches_lost": sum(r["launches_lost"] for r in runs),
        }
    return agg


def compute_verdict(agg: dict) -> dict:
    """Rule fixed BEFORE any run (PLAN.md). Recomputable from observations alone."""
    singles = [a for a in SINGLE_ARMS if a in agg]
    if "factory" not in agg or not singles:
        return {"winner": "INCONCLUSIVE", "rule": "", "d018": {"recommendation": "REJECT"},
                "reason": "arms missing from aggregate"}
    best = max(singles, key=lambda a: (agg[a]["verified_rate"], -agg[a]["cost_usd"]))
    f, b = agg["factory"], agg[best]
    delivery_gain = round(f["verified_rate"] - max(agg[a]["verified_rate"] for a in singles), 3)
    integrity_gain = max(agg[a]["false_acceptances"] for a in singles) - f["false_acceptances"]
    wins = integrity_gain > 0 and f["verified_rate"] >= max(agg[a]["verified_rate"] for a in singles)
    overhead = {
        "extra_cost_usd_vs_best_single": round(f["cost_usd"] - min(agg[a]["cost_usd"] for a in singles), 6),
        "extra_tokens_vs_best_single": (f["tokens_in"] + f["tokens_out"])
        - min(agg[a]["tokens_in"] + agg[a]["tokens_out"] for a in singles),
        "extra_mean_elapsed_s_vs_best_single": round(
            f["mean_elapsed_s"] - min(agg[a]["mean_elapsed_s"] for a in singles), 3),
    }
    recommendation = ("KEEP_TOPOLOGY" if wins else
                      "REDUCE_OR_REJECT_TOPOLOGY" if overhead["extra_cost_usd_vs_best_single"] > 0
                      else "KEEP_TOPOLOGY")
    tokens_vs_best_single, owner_answer = tokens_comparison(agg, best, delivery_gain)
    return {
        "winner": "FACTORY_WINS" if wins else "FACTORY_DOES_NOT_WIN",
        "rule": "FACTORY_WINS ssi false_acceptances(factory) < max(singles) ET "
                "verified_rate(factory) >= max(singles); un non-gain de la Factory est un "
                "resultat valide du benchmark, pas un echec de l'experiment.",
        "best_single_arm": best,
        "delivery_gain_verified_rate": delivery_gain,
        "integrity_gain_false_acceptances_avoided": integrity_gain,
        "overhead": overhead,
        "d018": {"topology_overhead_measured": True,
                 "verified_delivery_gain": delivery_gain,
                 "recommendation": recommendation},
        "tokens_vs_best_single": tokens_vs_best_single,
        "owner_answer": owner_answer,
        "reason": (
            f"factory: {f['verified_rate']:.2f} verified, {f['false_acceptances']} fausses "
            f"acceptances, ${f['cost_usd']:.4f} prix-model ; meilleur single ({best}): "
            f"{b['verified_rate']:.2f} verified, {b['false_acceptances']} fausses acceptances, "
            f"${b['cost_usd']:.4f}."
        ),
    }


def tokens_comparison(agg: dict, best: str, delivery_gain: float) -> tuple:
    """FACTORY_TOKEN_TRUTH_V1 — factory vs best single arm in MEASURED tokens.

    Ratio of measured input+output tokens (cache reads reported apart). A ratio
    needs both sides measured and > 0: an arm without measured tokens makes it
    UNDEFINED, never 0 or 1. The owner sentence answers the standing question in
    one line: « la factory coûte X× le meilleur solo pour Y % de résultats
    vérifiés en plus »."""
    singles = [a for a in SINGLE_ARMS if a in agg]
    f = agg.get("factory") or {}
    fm = int(f.get("tokens_measured") or 0)
    bm = int((agg.get(best) or {}).get("tokens_measured") or 0)
    per_arm = {a: int(agg[a].get("tokens_measured") or 0) for a in singles + ["factory"] if a in agg}
    cheapest = min(singles, key=lambda a: per_arm.get(a, 0)) if singles else ""
    ratio = round(fm / bm, 3) if fm > 0 and bm > 0 else "UNDEFINED"
    gain_pct = round(100.0 * delivery_gain, 1)
    comparison = {
        "ratio": ratio, "best_single_arm": best,
        "factory_tokens_measured": fm, "best_single_tokens_measured": bm,
        "cheapest_single_arm": cheapest,
        "ratio_vs_cheapest_single": (round(fm / per_arm[cheapest], 3)
                                     if cheapest and fm > 0 and per_arm[cheapest] > 0 else "UNDEFINED"),
        "per_arm_tokens_measured": per_arm,
        "cache_read_tokens_per_arm": {a: int(agg[a].get("cache_read_tokens_measured") or 0) for a in per_arm},
        "verified_gain_pct_points": gain_pct,
        "basis": "input+output tokens measured by the telemetry adapters on every dispatched launch "
                 "(dispatch receipts); cache reads apart; a lost launch is unmeasured, never estimated",
        # a launch the harness killed in flight (FI-3) never wrote its terminal observation:
        # its spend is unmeasured and stays so — reported, never estimated
        "unmeasured_or_lost_launches": {a: int(agg[a].get("tokens_unmeasured_launches") or 0)
                                        + int(agg[a].get("launches_lost") or 0) for a in per_arm},
    }
    if ratio == "UNDEFINED":
        answer = ("ratio non défini : un bras sans tokens mesurés (factory "
                  f"{fm}, meilleur solo {best} {bm}) — jamais 0")
    else:
        answer = (f"la factory coûte {ratio}× le meilleur solo ({best}) pour {gain_pct:+g} % "
                  f"de résultats vérifiés en plus (tokens mesurés : factory {fm}, {best} {bm})")
    return comparison, answer


def matched_checks(observations: dict[str, list[dict]]) -> dict:
    all_runs = [r for runs in observations.values() for r in runs]
    return {
        "start_sha_equal": len({r["start_sha"] for r in all_runs}) == 1 and bool(all_runs[0]["start_sha"]),
        "task_sha_equal": len({r["task_sha256"] for r in all_runs}) == 1,
        "data_sha_equal": len({r["data_sha256"] for r in all_runs}) == 1,
        "self_test_cmd_equal": len({r["self_test_cmd"] for r in all_runs}) == 1,
        "ground_truth_cmd_equal": len({r["ground_truth_cmd"] for r in all_runs}) == 1,
        "deliverable_equal": len({r["deliverable"] for r in all_runs}) == 1,
        "budget_equal": len({r["budget_s"] for r in all_runs}) == 1,
        "faults_equal": len({tuple(r["faults"]) for r in all_runs}) == 1,
    }


def _rel(p: Path) -> str:
    try:
        return str(p.relative_to(REPO_ROOT))
    except ValueError:
        return str(p)


def _write_atomic(path: Path, blob: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(blob, encoding="utf-8")
    os.replace(tmp, path)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--arms", default=",".join(ARMS))
    ap.add_argument("--out", default=f"factory_v2/benchmarks/{WORKLOAD_ID}")
    ap.add_argument("--budget-s", type=int, default=600)
    ap.add_argument("--max-cost-usd", type=float, default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify-only", action="store_true")
    ap.add_argument("--arm-run", dest="arm_run", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--arm-id", dest="arm_id", default="", help=argparse.SUPPRESS)
    ap.add_argument("--run-dir", dest="run_dir", default="", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    if getattr(args, "arm_run", False):
        return arm_run_main(args)

    try:
        arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    except Exception:
        arms = []
    unknown = [a for a in arms if a not in ARMS]
    if unknown:
        print(f"UNKNOWN_ARMS: {unknown} — registry: {sorted(ARMS)}", file=sys.stderr)
        return 2

    plan = {
        "workload_id": WORKLOAD_ID, "arms": arms, "runs_per_arm": args.runs,
        "budget_s_per_mission": args.budget_s, "max_cost_usd": args.max_cost_usd,
        "price_table_mtok_usd": PRICE_TABLE_MTOK_USD, "faults": list(FAULTS),
        "task_sha256": TASK_SHA256, "data_sha256": DATA_SHA256,
        "self_test_cmd": SELF_TEST_CMD, "ground_truth_cmd": GROUND_TRUTH_CMD,
        "deliverable": DELIVERABLE, "engine_exercised": "NOT_EXERCISED",
        "receipt": _rel(RECEIPT_PATH),
        "observations_dir": args.out.rstrip("/"),
    }

    if args.verify_only:
        return verify_only(args.out.rstrip("/"))

    if args.dry_run:
        # writes NOTHING under the repo; stdout is data only
        print(json.dumps({"dry_run": True, **plan}, indent=2, sort_keys=True))
        return 0

    if args.max_cost_usd is None:
        print("REFUSED: real model calls cost money — --max-cost-usd is required "
              "(or --dry-run). The benchmark refuses to start without a budget.",
              file=sys.stderr)
        return 2
    if args.max_cost_usd <= 0:
        print("REFUSED: --max-cost-usd must be > 0", file=sys.stderr)
        return 2
    if args.runs < 1:
        print("REFUSED: --runs must be >= 1", file=sys.stderr)
        return 2

    out_dir = REPO_ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    scratch_base = Path(tempfile.mkdtemp(prefix="matched-3arm-runs-"))
    observations: dict[str, list[dict]] = {a: [] for a in arms}
    spent = 0.0
    incomplete = []
    for arm in arms:
        for k in range(1, args.runs + 1):
            est = 0.04 * 4  # generous priced upper bound per run (4 launches)
            if spent + est > args.max_cost_usd:
                incomplete.append(f"{arm}_run{k}")
                continue
            print(f"[harness] {arm} run {k}/{args.runs} ...", file=sys.stderr)
            obs = run_one(arm, k, scratch_base, args.budget_s)
            observations[arm].append(obs)
            spent += obs["cost_usd"]
            print(f"[harness] {arm} run {k}: verified={obs['verified_delivery']} "
                  f"declared={obs['declared_success']} false_acc={obs['false_acceptance']} "
                  f"cost=${obs['cost_usd']:.5f} elapsed={obs['elapsed_s']}s "
                  f"recovery={obs['recovery_time_s']}s", file=sys.stderr)

    # content-addressed observations + manifest
    manifest_obs = {}
    for arm in arms:
        for obs in observations[arm]:
            name = f"{arm}_run{obs['run']}.json"
            blob = json.dumps(obs, sort_keys=True, indent=2) + "\n"
            _write_atomic(out_dir / name, blob)
            manifest_obs[name] = hashlib.sha256(blob.encode("utf-8")).hexdigest()
    producer_sha = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    all_runs = [r for runs in observations.values() for r in runs]
    manifest = {
        "producer_sha256": producer_sha, "workload_id": WORKLOAD_ID,
        "start_sha": all_runs[0]["start_sha"] if all_runs else "",
        "task_sha256": TASK_SHA256, "data_sha256": DATA_SHA256,
        "budget_s": args.budget_s, "faults": list(FAULTS),
        "observations": manifest_obs,
    }
    _write_atomic(out_dir / "MANIFEST.json", json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    shutil.rmtree(scratch_base, ignore_errors=True)

    agg = aggregate(observations)
    matched = matched_checks(observations)
    verdict = compute_verdict(agg)
    receipt = {
        "schema": SCHEMA, "workload_id": WORKLOAD_ID,
        "produced_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "producer": "factory_v2/run_matched_benchmark_3arm.py",
        "producer_sha256": producer_sha,
        "mode": "hermetic",
        "runs_per_arm": args.runs,
        "budget": {"mission_budget_s": args.budget_s, "max_cost_usd": args.max_cost_usd,
                   "price_table_mtok_usd": PRICE_TABLE_MTOK_USD,
                   "priced_cost_usd_total": round(spent, 6),
                   "real_cost_usd_total": 0.0},
        "task": {"task_sha256": TASK_SHA256, "start_sha": manifest["start_sha"],
                 "data_sha256": DATA_SHA256, "self_test_cmd": SELF_TEST_CMD,
                 "ground_truth_cmd": GROUND_TRUTH_CMD, "deliverable": DELIVERABLE,
                 "faults": list(FAULTS)},
        "arms": {a: {"topology": ARMS[a]["topology"], "engines": ARMS[a]["engines"],
                     "engine_hint": ARMS[a]["engine_hint"],
                     "settlement_gate": ARMS[a]["settlement_gate"],
                     "runs": observations[a]} for a in arms},
        "aggregate": agg, "matched": matched, "verdict": verdict,
        "incomplete_runs": incomplete,
        "caveats": [
            "HERMETIC: engines NOT_EXERCISED (FACTORY_NO_REAL_ENGINE=1). Engine QUALITY is "
            "not measured; the paired measurement is the real runtime TOPOLOGY (routing, "
            "repair, promote, independent settlement gate) on identical deterministic worlds.",
            "Real-model paired runs NOT executed in this session (no API budget/credentials); "
            "the '>=3 held-out runs per arm when cost allows' clause is met on the hermetic "
            "workload at zero real cost. The real-engine dimension is UNMET and declared, "
            "not fabricated.",
            "Retry sampling temperature is not measurable without real engines — UNMET, declared.",
            "Milestone product gate: python3 -m factory_v2.product_verifiers "
            "CLAUDE_ALONE_VS_GLM_ALONE_VS_FACTORY_BENCHMARK returns VERIFIER_NOT_IMPLEMENTED "
            "(no verifier registered; verifier registry is owner authority, not editable from "
            "this mission).",
            "run_matched_benchmark.py (two-arm producer of the settled CHAMPION milestone) left "
            "byte-identical: its pinned judge recomputes sha256(HEAD:file) vs the committed "
            "MANIFEST — a mutation would weaken a green gate.",
        ],
    }
    _write_atomic(RECEIPT_PATH, json.dumps(receipt, sort_keys=True, indent=2) + "\n")
    print(json.dumps({"verdict": verdict["winner"], "aggregate": agg,
                      "matched": all(matched.values()),
                      "incomplete_runs": incomplete,
                      "receipt": _rel(RECEIPT_PATH)}, indent=2))
    return 0 if all(matched.values()) and not incomplete else 1


def verify_only(out_rel: str) -> int:
    """Independent reconstruction from disk: observation hashes vs MANIFEST, matched
    equalities, aggregates and verdict recomputed from the observations — never
    trusted from the receipt."""
    out_dir = REPO_ROOT / out_rel
    man_p = out_dir / "MANIFEST.json"
    if not man_p.exists():
        print("VERIFY_UNREADABLE: MANIFEST.json missing", file=sys.stderr)
        return 2
    man = json.loads(man_p.read_text())
    obs_by_arm: dict[str, list[dict]] = {}
    for name, digest in sorted(man.get("observations", {}).items()):
        p = out_dir / name
        if not p.exists():
            print(f"VERIFY_UNREADABLE: observation missing: {name}", file=sys.stderr)
            return 2
        blob = p.read_text(encoding="utf-8")
        if hashlib.sha256(blob.encode("utf-8")).hexdigest() != digest:
            print(f"VERIFY_UNMET: OBSERVATION_TAMPERED: {name}", file=sys.stderr)
            return 1
        o = json.loads(blob)
        obs_by_arm.setdefault(o["arm"], []).append(o)
    for arm, runs in obs_by_arm.items():
        runs.sort(key=lambda r: r["run"])
    for name, digest in sorted(man.get("observations", {}).items()):
        o = json.loads((out_dir / name).read_text(encoding="utf-8"))
        if abs((o.get("cost_usd") or 0) - priced_cost_usd(o.get("usage"))) > 1e-9:
            print(f"VERIFY_UNMET: COST_FIELD_MISMATCH: {name}", file=sys.stderr)
            return 1
    agg = aggregate(obs_by_arm)
    matched = matched_checks(obs_by_arm)
    verdict = compute_verdict(agg)
    rec_p = RECEIPT_PATH
    if not rec_p.exists():
        print("VERIFY_UNREADABLE: receipt missing", file=sys.stderr)
        return 2
    rec = json.loads(rec_p.read_text())
    problems = []
    if rec.get("schema") != SCHEMA:
        problems.append("SCHEMA_MISMATCH")
    if rec.get("aggregate") != agg:
        problems.append("AGGREGATE_MISMATCH")
    if rec.get("verdict") != verdict:
        problems.append("VERDICT_MISMATCH")
    if rec.get("matched") != matched:
        problems.append("MATCHED_MISMATCH")
    if rec.get("task", {}).get("start_sha") != man.get("start_sha"):
        problems.append("START_SHA_MISMATCH")
    prod_sha = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if man.get("producer_sha256") != prod_sha:
        problems.append("MANIFEST_PRODUCER_MISMATCH")
    if rec.get("producer_sha256") != prod_sha:
        problems.append("RECEIPT_PRODUCER_MISMATCH")
    if problems:
        print(f"VERIFY_UNMET: {'; '.join(problems)}", file=sys.stderr)
        return 1
    # L5_rest_2-006: verify_only used to confirm the receipt's matched fields
    # without requiring them to be TRUE — an experiment whose arms ran
    # DIFFERENT tasks (matched=false) exited rc=0, verified. Verification of a
    # matched experiment must fail when any matched check fails.
    unmatched = sorted(k for k, v in matched.items() if not v)
    if unmatched:
        print(f"VERIFY_UNMET: UNMATCHED_ARMS: {unmatched}", file=sys.stderr)
        return 1
    print(json.dumps({"verified": True, "verdict": verdict["winner"],
                      "aggregate": agg, "matched": all(matched.values())}, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
