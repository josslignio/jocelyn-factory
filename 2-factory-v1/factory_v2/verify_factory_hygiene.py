#!/usr/bin/env python3
"""Independent judges for the factory's own simplification / hygiene milestones.

    python3 factory_v2/verify_factory_hygiene.py <check> --project-repo .

  split-advance      runtime._advance_locked split: no function in factory_v2/runtime.py
                     (or factory_v2/advance/*.py) longer than MAX_FUNC_LINES or above
                     MAX_COMPLEXITY (AST-measured), all previous public names still importable
  io-common          each of the duplicated helpers (_now, _atomic_write, _read_json, _git)
                     is DEFINED at most once across factory/ + factory_v2/ and lives in
                     factory_v2/_io.py
  state-out-of-repo  after a daemon cycle the tracked tree stays clean: no tracked file
                     under RUNTIME_STATE_PATHS is modified; those paths are gitignored or gone
  authority-at-head  factory_v2.authority (or roadmap loader) exposes read-at-HEAD mode and
                     the daemon uses it: a roadmap edit on disk without commit is NOT visible
                     to compile (probe executed in a temp clone)
  metrics-dashboard  factory/run/BUILD_METRICS_LATEST.md carries the per-run sections
                     recomputed from attempt_accounts (launches, success, mean duration,
                     refused-without-engine, parked, quota by engine) and matches recompute
  worktree-autopurge no mission worktree older than 7 days without a live lease under
                     /private/tmp/factory-worktrees, and the purge is scheduled in the daemon
                     (a typed event WORKTREE_PURGED exists in the last 24h or nothing to purge)

Reads code AT HEAD via git show for the AST checks (never the worktree copy). rc 0/1/2.
stdlib only. Written by the CTO operator 2026-09-13.
"""
from __future__ import annotations

import argparse
import ast
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

MAX_FUNC_LINES = 400
MAX_COMPLEXITY = 60
DUP_HELPERS = ("_now", "_atomic_write", "_read_json", "_git")
# PROJECT_STATE.json stays tracked for now (durable authority-adjacent state read by
# settlement); the V1 campaign state, daemon run files and append-only journals go.
RUNTIME_STATE_PATHS = ("factory/run/", "factory/projects/", "factory_v2/receipts/runrecords.jsonl",
                       "factory_v2/receipts/collection_sessions.jsonl", "factory/cto/CTO_EVENTS.jsonl")


class Unreadable(Exception):
    pass


class Unmet(Exception):
    pass


def _git(repo: Path, *a: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True)


def _head_src(repo: Path, rel: str) -> str:
    r = _git(repo, "show", f"HEAD:{rel}")
    if r.returncode:
        raise Unreadable(f"NOT_AT_HEAD: {rel}")
    return r.stdout


def _head_json(repo: Path, rel: str) -> dict:
    try:
        return json.loads(_head_src(repo, rel))
    except json.JSONDecodeError as exc:
        raise Unreadable(f"CORRUPT_JSON: {rel}: {exc}") from exc


def _tracked_py(repo: Path, prefixes) -> list:
    ls = _git(repo, "ls-tree", "-r", "--name-only", "HEAD")
    return [p for p in ls.stdout.splitlines() if p.endswith(".py") and any(p.startswith(x) for x in prefixes)
            and not p.startswith("tests/")]


def _complexity(node: ast.AST) -> int:
    """McCabe-style: 1 + decision points."""
    n = 1
    for sub in ast.walk(node):
        if isinstance(sub, (ast.If, ast.For, ast.While, ast.ExceptHandler, ast.With, ast.Assert, ast.IfExp)):
            n += 1
        elif isinstance(sub, ast.BoolOp):
            n += len(sub.values) - 1
        elif isinstance(sub, ast.comprehension):
            n += 1 + len(sub.ifs)
        elif isinstance(sub, ast.Match):
            n += len(sub.cases)
    return n


MAX_ADVANCE_LOCKED_LINES = 150
MIN_STAGE_MODULES = 5


def check_split_advance(repo: Path) -> dict:
    worst = []
    # the whole runtime package (moving a monolith to another module does not pass);
    # Twitter Radar product code inside the package is a separate milestone (S3),
    # dispatcher/factoryd splits too.
    PRODUCT_CODE = ("factory_v2/tr_", "factory_v2/product_verifiers.py", "factory_v2/verify_tr_",
                    "factory_v2/weekly_observation.py", "factory_v2/run_tr_collection.py", "factory_v2/twitter_radar_long_run.py")
    STANDALONE = ("factory_v2/verify_factory_hygiene.py", "factory_v2/verify_simplification_batch.py",
                  "factory_v2/tr_presentation_verifiers.py")
    # verifier modules are independent judges, not the advance loop this milestone splits
    files = [p for p in _tracked_py(repo, ("factory_v2/",)) if not p.startswith(PRODUCT_CODE) and p not in STANDALONE
             and not Path(p).name.startswith("verify_") and Path(p).name != "factory_verifiers.py"]
    for rel in files:
        tree = ast.parse(_head_src(repo, rel))
        for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            lines = (fn.end_lineno or fn.lineno) - fn.lineno + 1
            cx = _complexity(fn)
            if lines > MAX_FUNC_LINES or cx > MAX_COMPLEXITY:
                worst.append((rel, fn.name, lines, cx))
    if worst:
        raise Unmet("FUNCTION_TOO_LARGE: " + "; ".join(f"{r}:{n} lines={l} cx={c}" for r, n, l, c in worst[:6]))
    src = _head_src(repo, "factory_v2/runtime.py")
    for name in ("_advance_locked", "select_project", "advance"):
        if not re.search(rf"^def {name}\(|^{name} = ", src, flags=re.M) and f"import {name}" not in src and f"{name}," not in src:
            raise Unmet(f"PUBLIC_NAME_LOST: {name} no longer reachable from factory_v2.runtime")
    # A real split: _advance_locked itself is a short orchestrator and the stages live
    # in factory_v2/advance/ as several modules that runtime.py actually calls.
    tree = ast.parse(src)
    adv = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_advance_locked"]
    if adv:
        n = adv[0]
        if (n.end_lineno or n.lineno) - n.lineno + 1 > MAX_ADVANCE_LOCKED_LINES:
            raise Unmet(f"ADVANCE_LOCKED_NOT_SPLIT: {(n.end_lineno or n.lineno) - n.lineno + 1} lines > {MAX_ADVANCE_LOCKED_LINES}")
    stages = [p for p in _tracked_py(repo, ("factory_v2/advance/",)) if not p.endswith("__init__.py")]
    if len(stages) < MIN_STAGE_MODULES:
        raise Unmet(f"STAGES_MISSING: {len(stages)} modules under factory_v2/advance/ (need >= {MIN_STAGE_MODULES})")
    # L5_factory_v2_rest_2-026: "wired" means a real IMPORT in runtime.py's AST —
    # the filename's stem appearing anywhere in the source (five comments
    # mentioning never-imported modules) satisfied the old textual check.
    imported: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.rsplit(".", 1)[-1] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module.rsplit(".", 1)[-1])
            imported |= {a.name for a in node.names}
    called = [p for p in stages if Path(p).stem in imported]
    if len(called) < MIN_STAGE_MODULES:
        raise Unmet(f"STAGES_NOT_WIRED: runtime.py imports only {len(called)} stage modules "
                    f"(textual mentions do not count): "
                    f"{[Path(p).stem for p in stages if Path(p).stem not in imported]}")
    return {"files": len(files), "stages": len(stages), "max_func_lines": MAX_FUNC_LINES, "max_complexity": MAX_COMPLEXITY}


def check_io_common(repo: Path) -> dict:
    defs: dict = {h: [] for h in DUP_HELPERS}
    # standalone judges are executed from `git show` into /tmp and cannot import the
    # package: they keep their own tiny helpers by design
    STANDALONE = ("factory_v2/verify_factory_hygiene.py", "factory_v2/verify_simplification_batch.py",
                  "factory_v2/tr_presentation_verifiers.py")
    for rel in _tracked_py(repo, ("factory/", "factory_v2/")):
        if rel.startswith("factory/campaigns/") or "/evidence/" in rel or rel in STANDALONE:
            continue
        try:
            tree = ast.parse(_head_src(repo, rel))
        except SyntaxError:
            continue
        for n in tree.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in defs:
                defs[n.name].append(rel)
    bad = {h: v for h, v in defs.items() if v != ["factory_v2/_io.py"]}
    if bad:
        raise Unmet("HELPER_NOT_EXACTLY_ONCE_IN_IO: " + json.dumps(bad)[:400])
    importers = 0
    for rel in _tracked_py(repo, ("factory/", "factory_v2/")):
        if rel == "factory_v2/_io.py" or rel.startswith("factory/campaigns/"):
            continue
        txt = _head_src(repo, rel)
        if re.search(r"from\s+(factory_v2\.)?_io\s+import|from\s+\._io\s+import|import\s+factory_v2\._io", txt):
            importers += 1
    if importers < 8:
        raise Unmet(f"IO_MODULE_NOT_ADOPTED: only {importers} modules import from _io (need >= 8, the former duplicates)")
    io_src = _head_src(repo, "factory_v2/_io.py")
    for must in ("os.replace", "fsync", "capture_output", "timeout"):
        if must not in io_src:
            raise Unmet(f"IO_SEMANTICS_MISSING: {must} not found in factory_v2/_io.py")
    return {"helpers": DUP_HELPERS, "importers": importers}


def check_state_out_of_repo(repo: Path) -> dict:
    tracked = set(_git(repo, "ls-tree", "-r", "--name-only", "HEAD").stdout.splitlines())
    still = [p for p in tracked if any(p == x or p.startswith(x) for x in RUNTIME_STATE_PATHS)
             and not p.endswith((".md", ".yaml", ".schema.json"))]
    if still:
        raise Unmet("RUNTIME_STATE_STILL_TRACKED: " + ", ".join(still[:8]))
    ign = _git(repo, "check-ignore", "-q", "factory/run/probe.json")
    if ign.returncode:
        raise Unmet("RUNTIME_STATE_DIR_NOT_IGNORED: factory/run/")
    dirty = [ln[3:] for ln in _git(repo, "status", "--porcelain").stdout.splitlines()
             if ln[:2].strip() and not ln.startswith("??") and any(ln[3:].startswith(x) for x in RUNTIME_STATE_PATHS)]
    if dirty:
        raise Unmet("STATE_PATHS_DIRTY: " + ", ".join(dirty[:8]))
    # every state path must be resolvable to the run-dir by the runtime's own config
    src = _head_src(repo, "factory/bin/factoryd.py") + _head_src(repo, "factory_v2/runtime.py")
    if "Application Support/JocelynFactory" not in src and "FACTORY_RUN_DIR" not in src:
        raise Unmet("RUN_DIR_NOT_CONFIGURABLE: no run-dir resolution found in factoryd/runtime")
    return {"tracked_state_files": 0, "exception": "projects/*/PROJECT_STATE.json stays tracked (durable settlement state)"}


def check_authority_at_head(repo: Path) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        clone = Path(tmp) / "c"
        if subprocess.run(["git", "clone", "-q", "--no-hardlinks", str(repo), str(clone)]).returncode:
            raise Unreadable("CANNOT_CLONE")
        rm = clone / "projects" / "factory" / "ROADMAP.yaml"
        text = rm.read_text(encoding="utf-8")
        rm.write_text(text + "\n- id: PROBE_UNCOMMITTED_MILESTONE\n  goal: probe\n  depends_on: {}\n  deliverables: []\n  acceptance: []\n", encoding="utf-8")
        probe = ("import sys, json; sys.path.insert(0, '.'); from factory_v2 import authority as A; "
                 "ids=[m['id'] for m in A.load_roadmap('projects/factory')['milestones']]; "
                 "spec=A.load_authority_text('projects/factory', 'SPEC.md'); "
                 "strat=A.load_authority_text('projects/factory', 'STRATEGY.md'); "
                 "h=A.authority_hashes('projects/factory'); "
                 "vis = ('PROBE_UNCOMMITTED_MILESTONE' in ids) or ('PROBE_UNCOMMITTED_SPEC_LINE' in spec) or ('PROBE_UNCOMMITTED_STRATEGY_LINE' in strat); "
                 "print(('PROBE_VISIBLE' if vis else 'PROBE_HIDDEN') + ' HASHES=' + json.dumps(h, sort_keys=True))")
        before = subprocess.run([sys.executable, "-c", probe], cwd=clone, capture_output=True, text=True, timeout=120)
        if before.returncode:
            raise Unmet(f"AUTHORITY_LOADER_MISSING: factory_v2.authority.{{load_roadmap,load_authority_text,authority_hashes}} not usable: {before.stderr[-200:]}")
        # now edit SPEC, STRATEGY and one task file on disk without committing
        for rel, line in (("projects/factory/SPEC.md", "PROBE_UNCOMMITTED_SPEC_LINE"),
                          ("projects/factory/STRATEGY.md", "PROBE_UNCOMMITTED_STRATEGY_LINE")):
            f = clone / rel
            f.write_text(f.read_text(encoding="utf-8") + f"\n{line}\n", encoding="utf-8")
        task_files = [p for p in _git(clone, "ls-tree", "-r", "--name-only", "HEAD", "--", "factory/campaigns/factory-vnext/tasks").stdout.splitlines() if p.endswith(".md")]
        if task_files:
            tf = clone / task_files[0]
            tf.write_text(tf.read_text(encoding="utf-8") + "\nPROBE_UNCOMMITTED_TASK_LINE\n", encoding="utf-8")
        after = subprocess.run([sys.executable, "-c", probe], cwd=clone, capture_output=True, text=True, timeout=120)
        if "PROBE_HIDDEN" not in after.stdout:
            raise Unmet("AUTHORITY_READ_FROM_WORKTREE: an uncommitted roadmap/SPEC/STRATEGY edit is visible to the loader")
        hb = before.stdout.split("HASHES=", 1)[-1].strip(); ha = after.stdout.split("HASHES=", 1)[-1].strip()
        if hb != ha or not hb or hb == "{}":
            raise Unmet("AUTHORITY_HASHES_FOLLOW_DISK: authority_hashes changed after uncommitted edits (or are empty)")
        if task_files:
            import hashlib
            head_blob = _head_src(clone, task_files[0])
            expected = hashlib.sha256(head_blob.encode("utf-8")).hexdigest()
            freeze_probe = ("import sys; sys.path.insert(0, '.'); from factory_v2 import authority as A; "
                            "print(A.task_file_hash('projects/factory', %r)); print('LOADED=' + A.load_task_file('projects/factory', %r)[-60:].replace(chr(10), ' '))" % (task_files[0], task_files[0]))
            fz = subprocess.run([sys.executable, "-c", freeze_probe], cwd=clone, capture_output=True, text=True, timeout=120)
            if fz.returncode:
                raise Unmet(f"TASK_FILE_LOADER_MISSING: authority.task_file_hash/load_task_file: {fz.stderr[-160:]}")
            if expected not in fz.stdout:
                raise Unmet("TASK_FILE_HASH_FROM_DISK: authority.task_file_hash must equal sha256 of the HEAD blob")
            if "PROBE_UNCOMMITTED" in fz.stdout:
                raise Unmet("TASK_FILE_LOADED_FROM_DISK: load_task_file must return the HEAD blob")
        r2 = subprocess.run([sys.executable, "-c", probe], cwd=clone, capture_output=True, text=True, timeout=120,
                            env={**os.environ, "FACTORY_AUTHORITY_FROM_WORKTREE": "1"})
        if "PROBE_VISIBLE" not in r2.stdout:
            raise Unmet("WORKTREE_OVERRIDE_MISSING: FACTORY_AUTHORITY_FROM_WORKTREE=1 must expose the on-disk roadmap (tests/dev)")
        # non-git fixture: a project dir outside any git repo falls back to disk, explicitly
        plain = Path(tmp) / "plain" / "projects" / "factory"; plain.mkdir(parents=True)
        (plain / "ROADMAP.yaml").write_text("milestones:\n- id: PLAIN_FIXTURE\n  goal: x\n  depends_on: {}\n  deliverables: []\n  acceptance: []\n", encoding="utf-8")
        r3 = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, %r); from factory_v2 import authority as A; print([m['id'] for m in A.load_roadmap(%r)['milestones']])" % (str(clone), str(plain))],
                            cwd=clone, capture_output=True, text=True, timeout=120)
        if "PLAIN_FIXTURE" not in r3.stdout:
            raise Unmet(f"NON_GIT_FALLBACK_MISSING: a project dir outside git must load from disk: {r3.stderr[-160:]}")
    # every runtime reader of authority goes through the loader — no direct open() of the files
    readers = ("factory/bin/factoryd.py", "factory_v2/runtime.py", "factory_v2/mission_compiler.py",
               "factory_v2/canonical_context.py", "factory_v2/authority_change.py", "factory_v2/validation_contract.py",
               "factory_v2/roadmap_resolver.py")
    direct = []
    for rel in readers:
        if _git(repo, "cat-file", "-e", f"HEAD:{rel}").returncode:
            continue
        txt = _head_src(repo, rel)
        if re.search(r"(open|read_text)\([^\n]*(ROADMAP\.yaml|SPEC\.md|STRATEGY\.md|task_file)", txt) and rel != "factory_v2/authority.py":
            direct.append(rel)
    if direct:
        raise Unmet("DIRECT_AUTHORITY_READ: " + ", ".join(direct))
    src = _head_src(repo, "factory/bin/factoryd.py") + _head_src(repo, "factory_v2/runtime.py")
    if "load_roadmap(" not in src:
        raise Unmet("DAEMON_NOT_USING_LOADER: factoryd/runtime must go through factory_v2.authority.load_roadmap")
    return {"mode": "HEAD", "non_git_fixture": "loader falls back to disk when project_dir is not inside a git repo"}


def _is_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def check_metrics_dashboard(repo: Path) -> dict:
    # since FACTORY_STATE_OUT_OF_REPO_V1 the daemon writes into its run-dir; the
    # in-repo factory/run/ is only a legacy fallback
    run_dir = Path(os.environ.get("FACTORY_RUN_DIR") or (Path.home() / "Library" / "Application Support" / "JocelynFactory" / "run"))
    if not (run_dir / "BUILD_METRICS_LATEST.json").is_file():
        run_dir = Path(repo, "factory", "run")
    md = run_dir / "BUILD_METRICS_LATEST.md"
    js = run_dir / "BUILD_METRICS_LATEST.json"
    if not md.is_file() or not js.is_file():
        raise Unmet("METRICS_MISSING: factory/run/BUILD_METRICS_LATEST.{md,json}")
    data = json.loads(js.read_text(encoding="utf-8"))
    for k in ("generated_at", "window_h", "per_project", "per_engine", "launches", "launches_success",
              "launches_refused_without_engine", "parked_milestones", "settlements", "mean_duration_s",
              "restarts", "loops_detected"):
        if k not in data:
            raise Unmet(f"METRICS_FIELD_MISSING: {k}")
    if (time.time() - os.path.getmtime(js)) > 26 * 3600:
        raise Unmet("METRICS_STALE: older than 26h")
    # recompute launches from the ledgers
    total = success = refused = 0
    per_project: dict = {}
    per_engine: dict = {}
    durations: list = []
    cutoff = time.time() - float(data["window_h"]) * 3600
    import datetime as _dt
    def _ts(iso):
        try:
            return _dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone(_dt.timezone.utc).timestamp()
        except ValueError:
            return None
    settlements = parked = loops = restarts = 0
    for proj in ("factory", "twitter-radar"):
        st = Path(repo, "projects", proj, "PROJECT_STATE.json")
        if st.is_file():
            try:
                events = json.loads(st.read_text(encoding="utf-8")).get("events") or []
            except json.JSONDecodeError:
                events = []
            for ev in events:
                t = _ts(ev.get("ts") or ev.get("at") or ev.get("timestamp") or "")
                if t is None or t < cutoff:
                    continue
                k = ev.get("event") or ev.get("kind") or ""
                settlements += k == "MILESTONE_SETTLED"
                parked += k == "MILESTONE_PARKED"
                loops += k in ("V2_STALL_DETECTED", "GATE_HELD", "MILESTONE_GATE_HELD")
        for line in Path(repo, "projects", proj, "evidence", "attempt_accounts.jsonl").read_text(encoding="utf-8", errors="replace").splitlines() if Path(repo, "projects", proj, "evidence", "attempt_accounts.jsonl").is_file() else []:
            pass
    # L5_factory_v2_rest_2-027: restarts are recomputed from PROJECT_STATE
    # events — the producer's own source (metrics_dashboard._RESTART_EVENTS:
    # RUNTIME_STARTED / CTO_STARTUP_CHECK / DAEMON_STARTED). The old code only
    # recomputed them when ~/Library/Logs/JocelynFactory/factoryd.stdout.log
    # existed: on a machine without that file, restarts silently recomputed to
    # 0 (or worse, matched nothing) instead of being verified.
    for proj in ("factory", "twitter-radar"):
        st = Path(repo, "projects", proj, "PROJECT_STATE.json")
        if not st.is_file():
            continue
        try:
            events = json.loads(st.read_text(encoding="utf-8")).get("events") or []
        except json.JSONDecodeError:
            events = []
        for ev in events:
            t = _ts(ev.get("ts") or ev.get("at") or ev.get("timestamp") or "")
            if t is not None and t >= cutoff and (ev.get("event") or ev.get("kind") or "") in ("RUNTIME_STARTED", "CTO_STARTUP_CHECK", "DAEMON_STARTED"):
                restarts += 1
    for proj in ("factory", "twitter-radar"):
        led = Path(repo, "projects", proj, "evidence", "attempt_accounts.jsonl")
        if not led.is_file():
            continue
        cutoff = time.time() - float(data["window_h"]) * 3600
        for line in led.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("phase") != "TERMINAL":
                continue
            ended = row.get("ended_at") or ""
            ts = _ts(ended)
            if ts is None:
                continue
            if ts < cutoff:
                continue
            if row.get("consumes_model_quota") is False:
                refused += 1
                continue
            total += 1
            pp = per_project.setdefault(proj, {"launches": 0, "success": 0}); pp["launches"] += 1
            eng = str(row.get("engine") or "UNKNOWN")
            pe = per_engine.setdefault(eng, {"launches": 0, "success": 0, "output_tokens": 0}); pe["launches"] += 1
            if _is_num(row.get("output_tokens")):
                pe["output_tokens"] += int(row["output_tokens"])
            if _is_num(row.get("duration_s")):
                durations.append(float(row["duration_s"]))
            if row.get("mission_pass"):
                success += 1; pp["success"] += 1; pe["success"] += 1
    if int(data["launches"]) != total:
        raise Unmet(f"METRICS_MISMATCH: launches declared={data['launches']} recomputed={total}")
    if data.get("per_project") != per_project or data.get("per_engine") != per_engine:
        raise Unmet(f"METRICS_MISMATCH: per_project/per_engine recomputed={json.dumps(per_project)[:160]} / {json.dumps(per_engine)[:160]}")
    exp_mean = round(sum(durations) / len(durations), 3) if durations else None
    if (data.get("mean_duration_s") or None) != exp_mean and not (exp_mean is not None and _is_num(data.get("mean_duration_s")) and abs(float(data["mean_duration_s"]) - exp_mean) < 0.01):
        raise Unmet(f"METRICS_MISMATCH: mean_duration_s declared={data.get('mean_duration_s')} recomputed={exp_mean}")
    if int(data["settlements"]) != settlements or int(data["parked_milestones"]) != parked or int(data["loops_detected"]) != loops or int(data["restarts"]) != restarts:
        raise Unmet(f"METRICS_MISMATCH: settlements/parked/loops/restarts recomputed={settlements}/{parked}/{loops}/{restarts}")
    if int(data["launches_success"]) != success or int(data["launches_refused_without_engine"]) != refused:
        raise Unmet(f"METRICS_MISMATCH: success/refused declared={data['launches_success']}/{data['launches_refused_without_engine']} recomputed={success}/{refused}")
    if not str(data["generated_at"]).endswith("Z"):
        raise Unmet("METRICS_GENERATED_AT_NOT_UTC")
    text = md.read_text(encoding="utf-8")
    for section in ("Vitesse", "Quota", "Reprise", "Boucles"):
        if section.lower() not in text.lower():
            raise Unmet(f"METRICS_SECTION_MISSING: {section}")
    return {"launches": total}


def check_worktree_autopurge(repo: Path) -> dict:
    base = Path("/private/tmp/factory-worktrees")
    old = []
    if base.is_dir():
        for p in base.iterdir():
            if p.is_dir() and (time.time() - p.stat().st_mtime) > 7 * 86400:
                old.append(p.name)
    if old:
        raise Unmet("STALE_WORKTREES: " + ", ".join(old[:6]))
    src = _head_src(repo, "factory/bin/factoryd.py") + _head_src(repo, "factory_v2/runtime.py") + _head_src(repo, "factory/bin/dispatcher_v3.py")
    if "prune_stale_worktrees" not in src:
        raise Unmet("PURGE_NOT_WIRED")
    return {"stale": 0}


def check_tr_test_hygiene(repo: Path) -> dict:
    """AST-scan tests/ for socket/server usage; each such test module must carry the
    needs_socket marker; under TR_FORCE_NO_SOCKET=1 the run must skip exactly them."""
    tests = [p for p in _git(repo, "ls-tree", "-r", "--name-only", "HEAD", "--", "tests").stdout.splitlines() if p.endswith(".py")]
    needs, marked = set(), set()
    SERVER_CALLS = {"bind", "listen", "serve_forever", "start_server", "create_server", "make_server", "HTTPServer",
                    "ThreadingHTTPServer", "TCPServer", "ThreadingTCPServer", "socket", "TestServer", "TestClient", "run_app"}
    for rel in tests:
        txt = _head_src(repo, rel)
        try:
            tree = ast.parse(txt)
        except SyntaxError:
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.Call):
                fn = n.func
                name = fn.attr if isinstance(fn, ast.Attribute) else (fn.id if isinstance(fn, ast.Name) else "")
                if name in SERVER_CALLS:
                    needs.add(rel); break
        if rel not in needs and re.search(r"127\.0\.0\.1|localhost|http://", txt):
            needs.add(rel)
        # the marker must be a real pytest mark (decorator or module-level pytestmark), not a comment
        def _is_mark(node):   # pytest.mark.needs_socket exactly
            return (isinstance(node, ast.Attribute) and node.attr == "needs_socket"
                    and isinstance(node.value, ast.Attribute) and node.value.attr == "mark"
                    and isinstance(node.value.value, ast.Name) and node.value.value.id == "pytest")
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and any(_is_mark(d) or (isinstance(d, ast.Call) and _is_mark(d.func)) for d in n.decorator_list):
                marked.add(rel); break
            if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in n.targets):
                vals = n.value.elts if isinstance(n.value, (ast.List, ast.Tuple)) else [n.value]
                if any(_is_mark(v) or (isinstance(v, ast.Call) and _is_mark(v.func)) for v in vals):
                    marked.add(rel); break
    if rel_conf := "tests/conftest.py":
        conf = _head_src(repo, rel_conf) if _git(repo, "cat-file", "-e", f"HEAD:{rel_conf}").returncode == 0 else ""
        if "needs_socket" not in conf or "SOCKET_BIND_REFUSED" not in conf or "TR_FORCE_NO_SOCKET" not in conf:
            raise Unmet("CONFTEST_HOOK_MISSING: needs_socket / SOCKET_BIND_REFUSED / TR_FORCE_NO_SOCKET")
    unmarked = sorted(needs - marked - {"tests/conftest.py"})
    if unmarked:
        raise Unmet("SOCKET_TEST_UNMARKED: " + ", ".join(unmarked[:6]))
    r = subprocess.run([sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider", "-rs"], cwd=repo,
                       capture_output=True, text=True, timeout=1800, env={**os.environ, "TR_FORCE_NO_SOCKET": "1"})
    failed_sim = set(re.findall(r"^FAILED (\S+)", r.stdout, flags=re.M))
    # only the typed socket skips count; other pre-existing skips (live data missing) are not ours
    skipped_files = set(re.findall(r"SKIPPED \[\d+\] (tests/[^:]+\.py)[^\n]*SOCKET_BIND_REFUSED", r.stdout))
    typed = len(re.findall(r"SKIPPED .*SOCKET_BIND_REFUSED", r.stdout))
    if typed == 0:
        raise Unmet("NO_TYPED_SKIP_OBSERVED under TR_FORCE_NO_SOCKET=1")
    extra = sorted(skipped_files - needs)
    if extra:
        raise Unmet("SKIP_OUTSIDE_SOCKET_TESTS: " + ", ".join(extra[:6]))
    not_skipped = sorted(needs - skipped_files)
    if not_skipped:
        raise Unmet("SOCKET_TEST_NOT_SKIPPED_UNDER_SIMULATION: " + ", ".join(not_skipped[:6]))
    r_full = subprocess.run([sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"], cwd=repo,
                            capture_output=True, text=True, timeout=1800, env={k: v for k, v in os.environ.items() if k != "TR_FORCE_NO_SOCKET"})
    failed_full = set(re.findall(r"^FAILED (\S+)", r_full.stdout, flags=re.M))
    # Pre-existing data-dependent failures are not this milestone's scope; what it must
    # prove is NO REGRESSION: the simulation may only turn socket tests into typed skips.
    new_failures = sorted(failed_sim - failed_full)
    if new_failures:
        raise Unmet("REGRESSION_UNDER_NO_SOCKET: " + ", ".join(new_failures[:6]))
    socket_failures = sorted(f for f in failed_full | failed_sim if f.split("::")[0] in needs)
    if socket_failures:
        raise Unmet("SOCKET_TEST_FAILING: " + ", ".join(socket_failures[:6]))
    return {"socket_tests": len(needs), "skipped_typed": typed, "skipped_files": len(skipped_files),
            "preexisting_failures": len(failed_full)}


PRODUCT_CODE_PREFIXES = ("factory_v2/tr_", "factory_v2/verify_tr_", "factory_v2/weekly_observation.py",
                         "factory_v2/run_tr_collection.py", "factory_v2/twitter_radar_long_run.py")
SHIM_MAX_LINES = 25


def check_product_out_of_runtime(repo: Path) -> dict:
    """Twitter Radar product code lives under products/twitter_radar/, not in the runtime
    package. Entry points referenced by the TR roadmap may remain as thin shims (<= 25
    lines, import + delegate) so authority commands keep working."""
    tracked = _git(repo, "ls-tree", "-r", "--name-only", "HEAD").stdout.splitlines()
    remaining = [p for p in tracked if p.startswith(PRODUCT_CODE_PREFIXES) or p == "factory_v2/product_verifiers.py"]
    moved = [p for p in tracked if p.startswith("products/twitter_radar/") and p.endswith(".py")]
    if len(moved) < 10:
        raise Unmet(f"PRODUCT_PACKAGE_MISSING: {len(moved)} modules under products/twitter_radar/ (need >= 10)")
    fat = []
    for rel in remaining:
        src = _head_src(repo, rel)
        if src.count("\n") > SHIM_MAX_LINES or "products.twitter_radar" not in src:
            fat.append(rel)
    if fat:
        raise Unmet("PRODUCT_CODE_STILL_IN_RUNTIME: " + ", ".join(fat[:6]) + f" (shims must be <= {SHIM_MAX_LINES} lines delegating to products.twitter_radar)")
    runtime_py = [p for p in tracked if p.startswith("factory_v2/") and p.endswith(".py") and not p.startswith(PRODUCT_CODE_PREFIXES) and p != "factory_v2/product_verifiers.py"]
    leaks = []
    for rel in runtime_py:
        txt = _head_src(repo, rel)
        if re.search(r"from\s+\.\s*(tr_[a-z_]+|weekly_observation|run_tr_collection|twitter_radar_long_run)\s+import|import\s+factory_v2\.(tr_|weekly_observation|run_tr_collection|twitter_radar_long_run)", txt):
            leaks.append(rel)
    if leaks:
        raise Unmet("RUNTIME_STILL_IMPORTS_PRODUCT_CODE: " + ", ".join(leaks[:6]))
    back = []
    for rel in moved:
        txt = _head_src(repo, rel)
        if re.search(r"from\s+factory_v2(\.|\s+import)|import\s+factory_v2", txt):
            back.append(rel)
    if back:
        raise Unmet("PRODUCT_PACKAGE_IMPORTS_RUNTIME: " + ", ".join(back[:6]) + " (products.twitter_radar must not depend on factory_v2; shared helpers go to factory_v2/_io or a products/common module)")
    tr_markers = re.compile(r"tr_kol_|tr_call_|weekly_observation|TR_WEEKLY_OPERATE|tr_ticker|twitter_radar|TR_UNIVERSE|tr_radar", re.I)

    def _code_only(src: str) -> str:
        """Source with comments and string literals (docstrings included) removed:
        a comment citing a real incident is not product code."""
        import tokenize
        out = []
        try:
            for tok in tokenize.generate_tokens(io.StringIO(src).readline):
                if tok.type in (tokenize.COMMENT, tokenize.STRING):
                    continue
                out.append(tok.string)
        except (tokenize.TokenError, IndentationError):
            return src
        return " ".join(out)
    hidden = [p for p in runtime_py if p not in ("factory_v2/tr_presentation_verifiers.py", "factory_v2/verify_factory_hygiene.py")
              and len(tr_markers.findall(_code_only(_head_src(repo, p)))) >= 5]
    if hidden:
        raise Unmet("TR_CODE_UNDER_NEW_NAME_IN_RUNTIME: " + ", ".join(hidden[:6]))
    return {"moved_modules": len(moved), "shims": len(remaining)}


CHECKS = {
    "product-out-of-runtime": check_product_out_of_runtime,
    "tr-test-hygiene": check_tr_test_hygiene,
    "split-advance": check_split_advance,
    "io-common": check_io_common,
    "state-out-of-repo": check_state_out_of_repo,
    "authority-at-head": check_authority_at_head,
    "metrics-dashboard": check_metrics_dashboard,
    "worktree-autopurge": check_worktree_autopurge,
}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("check", choices=sorted(CHECKS))
    ap.add_argument("--project-repo", required=True)
    args = ap.parse_args(argv)
    repo = Path(args.project_repo).resolve()
    try:
        res = CHECKS[args.check](repo)
    except Unreadable as exc:
        print(f"{args.check}_UNREADABLE: {exc}", file=sys.stderr)
        return 2
    except Unmet as exc:
        print(f"{args.check}_UNMET: {exc}", file=sys.stderr)
        return 1
    print(f"{args.check}_VERIFIED: {res}")
    return 0



# =========================================================================== CTO decisions 2026-09-13
# Judges for the four "impossible by construction" capabilities. Each names the
# producer artefact a builder must create and recomputes from it; nothing here
# trusts an authored boolean.

def check_adaptive_routing(repo: Path) -> dict:
    """model_router.runrecords_override + route(..., evidence=) with evidence DERIVED
    by factory_v2.routing_evidence.evidence_from_ledger from a fixture ledger the judge
    writes itself: only independently validated settlements count, mission_pass alone
    never does; constants are checked by value; the dispatcher must call the deriver."""
    probe = (
        "import sys, json, tempfile, pathlib\n"
        "sys.path.insert(0, 'factory/bin'); sys.path.insert(0, '.')\n"
        "import model_router as mr\n"
        "from factory_v2 import routing_evidence as re_\n"
        "assert mr.MIN_EVIDENCE_N == 5 and abs(mr.MIN_SUCCESS_GAP - 0.20) < 1e-9, ('CONSTANTS', mr.MIN_EVIDENCE_N, mr.MIN_SUCCESS_GAP)\n"
        "def ledger(rows):\n"
        "    d = pathlib.Path(tempfile.mkdtemp()) / 'proj'; (d / 'evidence').mkdir(parents=True)\n"
        "    with open(d / 'evidence' / 'attempt_accounts.jsonl', 'w') as fh:\n"
        "        for r in rows:\n"
        "            base = {'phase': 'TERMINAL', 'spec_level': 'B', 'task_kind': 'build', 'consumes_model_quota': True}\n"
        "            base.update(r); fh.write(json.dumps(base) + chr(10))\n"
        "    return d\n"
        "strong = ledger([{'engine': 'codex', 'mission_pass': i == 0, 'independently_validated': True} for i in range(8)] + [{'engine': 'claude', 'mission_pass': i < 7, 'independently_validated': True} for i in range(8)])\n"
        "thin = ledger([{'engine': 'codex', 'mission_pass': False, 'independently_validated': False} for _ in range(2)] + [{'engine': 'claude', 'mission_pass': True, 'independently_validated': True} for _ in range(2)])\n"
        "claims = ledger([{'engine': 'codex', 'mission_pass': True, 'independently_validated': False} for _ in range(8)] + [{'engine': 'claude', 'mission_pass': True, 'independently_validated': False} for _ in range(8)])\n"
        "def eng(x): return x[0] if isinstance(x, tuple) else getattr(x, 'engine', str(x))\n"
        "ev_s = re_.evidence_from_ledger(strong, 'B', 'build'); ev_t = re_.evidence_from_ledger(thin, 'B', 'build'); ev_c = re_.evidence_from_ledger(claims, 'B', 'build')\n"
        "assert ev_s['claude']['n'] == 8 and ev_s['claude']['success'] == 7 and ev_s['codex']['success'] == 1, ('EVIDENCE_DERIVATION', ev_s)\n"
        "assert ev_c.get('codex', {}).get('n', 0) == 0 and ev_c.get('codex', {}).get('success', 0) == 0, ('UNVALIDATED_ROWS_COUNTED', ev_c)\n"
        "base = eng(mr.route('B', 'build')); s_ = eng(mr.route('B', 'build', evidence=ev_s)); t_ = eng(mr.route('B', 'build', evidence=ev_t)); c_ = eng(mr.route('B', 'build', evidence=ev_c))\n"
        "print(json.dumps({'base': base, 'strong': s_, 'thin': t_, 'claims': c_}))\n"
    )
    r = subprocess.run([sys.executable, "-c", probe], cwd=repo, capture_output=True, text=True, timeout=180,
                       env={**os.environ, "FACTORY_NO_REAL_ENGINE": "1"})
    if r.returncode:
        raise Unmet(f"ROUTER_PROBE_FAILED: {r.stderr[-300:]}")
    out = json.loads(r.stdout.strip().splitlines()[-1])
    if out["strong"] != "claude":
        raise Unmet(f"EVIDENCE_IGNORED: strong evidence chose {out['strong']}")
    if out["thin"] != out["base"] or out["claims"] != out["base"]:
        raise Unmet(f"WEAK_OR_UNVALIDATED_EVIDENCE_OVERRODE: base={out['base']} thin={out['thin']} claims={out['claims']}")
    spy = (
        "import sys, json, tempfile, pathlib, os\n"
        "sys.path.insert(0, 'factory/bin'); sys.path.insert(0, '.')\n"
        "os.environ['FACTORY_NO_REAL_ENGINE'] = '1'\n"
        "import model_router as mr, dispatcher_v3 as d3\n"
        "seen = {}\n"
        "orig = mr.route\n"
        "def spy_route(*a, **k):\n"
        "    seen['evidence'] = k.get('evidence'); return orig(*a, **k)\n"
        "mr.route = spy_route\n"
        "d = pathlib.Path(tempfile.mkdtemp()) / 'proj'; (d / 'evidence').mkdir(parents=True)\n"
        "with open(d / 'evidence' / 'attempt_accounts.jsonl', 'w') as fh:\n"
        "    for i in range(6): fh.write(json.dumps({'phase': 'TERMINAL', 'spec_level': 'B', 'task_kind': 'build', 'engine': 'claude', 'mission_pass': True, 'independently_validated': True, 'consumes_model_quota': True}) + chr(10))\n"
        "fn = getattr(d3, 'route_for_dispatch', None)\n"
        "assert fn is not None, 'DISPATCHER_ENTRY_MISSING: dispatcher_v3.route_for_dispatch(spec_level, task_kind, project_dir) must exist and be the single routing entry used by dispatch_v3'\n"
        "fn('B', 'build', project_dir=d)\n"
        "ev = seen.get('evidence') or {}\n"
        "assert ev.get('claude', {}).get('n') == 6, ('EVIDENCE_NOT_PASSED', ev)\n"
        "src = open('factory/bin/dispatcher_v3.py').read()\n"
        "import re as _re\n"
        "assert len(_re.findall(r'route_for_dispatch\\(', src)) >= 2, 'ENTRY_NOT_USED_BY_DISPATCH'\n"
        "print('WIRED')\n"
    )
    r2 = subprocess.run([sys.executable, "-c", spy], cwd=repo, capture_output=True, text=True, timeout=180)
    if "WIRED" not in r2.stdout:
        raise Unmet(f"LIVE_PATH_NOT_WIRED: {(r2.stderr or r2.stdout)[-300:]}")
    rec = _head_json(repo, "factory_v2/receipts/ADAPTIVE_ROUTING_RECEIPT.json")
    if rec.get("schema") != "adaptive-routing-receipt/v1" or not isinstance(rec.get("evidence"), dict):
        raise Unmet("ROUTING_RECEIPT_INVALID")
    return out


def check_worker_slots(repo: Path) -> dict:
    """AttemptAccount serialises worker_slot/max_workers (fixture), FACTORY_MAX_WORKERS is read
    by daemon/runtime/launch_gate, run_windows.jsonl rows are structured and daemon-written."""
    probe = (
        "import sys, json, os\n"
        "sys.path.insert(0, '.'); os.environ['FACTORY_MAX_WORKERS'] = '3'\n"
        "from factory_v2 import resource_accounting as ra\n"
        "row = ra.attempt_account_fixture_row() if hasattr(ra, 'attempt_account_fixture_row') else None\n"
        "assert row is not None, 'FIXTURE_HELPER_MISSING: resource_accounting.attempt_account_fixture_row()'\n"
        "assert 'worker_slot' in row and 'max_workers' in row, ('FIELDS', sorted(row)[:30])\n"
        "assert int(row['max_workers']) == 3, ('MAX_WORKERS_ENV_IGNORED', row['max_workers'])\n"
        "print('OK')\n"
    )
    r = subprocess.run([sys.executable, "-c", probe], cwd=repo, capture_output=True, text=True, timeout=120)
    if "OK" not in r.stdout:
        raise Unmet(f"ATTEMPT_ACCOUNT_FIELDS: {(r.stderr or r.stdout)[-300:]}")
    live = (
        "import sys, json, os, tempfile, pathlib\n"
        "sys.path.insert(0, '.'); os.environ['FACTORY_MAX_WORKERS'] = '3'\n"
        "from factory_v2 import launch_gate as lg, run_windows as rw\n"
        "d = pathlib.Path(tempfile.mkdtemp()) / 'proj'; (d / 'evidence').mkdir(parents=True)\n"
        "acct = lg.open_account(d, attempt_key='k1', project='p', mission_id='M#1', milestone='M', attempt=1, consumes_model_quota=False, contract_body={}, cnp={'tripped': False}) if 'mission_id' in lg.open_account.__code__.co_varnames else None\n"
        "rows = [json.loads(l) for l in open(d / 'evidence' / 'attempt_accounts.jsonl')] if (d / 'evidence' / 'attempt_accounts.jsonl').exists() else []\n"
        "assert rows and rows[-1].get('max_workers') == 3 and 'worker_slot' in rows[-1], ('OPEN_ROW', rows[-1:] )\n"
        "run_dir = pathlib.Path(tempfile.mkdtemp()); os.environ['FACTORY_RUN_DIR'] = str(run_dir)\n"
        "rid = rw.open_run(run_dir, pid=os.getpid()); rw.close_run(run_dir, rid)\n"
        "w = [json.loads(l) for l in open(run_dir / 'run_windows.jsonl')]\n"
        "assert w and w[-1]['run_id'] == rid and w[-1]['ended_at'] and w[-1]['max_workers'] == 3, ('WINDOW', w)\n"
        "print('OK2')\n"
    )
    r2 = subprocess.run([sys.executable, "-c", live], cwd=repo, capture_output=True, text=True, timeout=180)
    if "OK2" not in r2.stdout:
        raise Unmet(f"LIVE_WIRING_FAILED: launch_gate.open_account must stamp worker_slot/max_workers and factory_v2.run_windows.open_run/close_run must write the window: {(r2.stderr or r2.stdout)[-300:]}")
    fd = _head_src(repo, "factory/bin/factoryd.py")
    if "run_windows" not in fd:
        raise Unmet("DAEMON_NOT_WIRED: factoryd must call factory_v2.run_windows.open_run/close_run")
    return {"max_workers_env": 3}


def check_matched_benchmark(repo: Path) -> dict:
    """BENCHMARK_RECEIPT.json recomputed from content-addressed observations bound by an
    execution MANIFEST.json (producer sha256 at HEAD, resolved SHAs, per-file sha256);
    champion pointer file read AT HEAD, not from the receipt."""
    import hashlib
    rec = _head_json(repo, "factory_v2/receipts/BENCHMARK_RECEIPT.json")
    if rec.get("schema") != "matched-benchmark-receipt/v1":
        raise Unreadable("BENCHMARK_SCHEMA")
    for k in ("workload_id", "start_sha", "champion_sha", "challenger_sha", "observations_dir", "metrics", "decision",
              "champion_pointer_before", "champion_pointer_after", "decision_evidence_path"):
        if k not in rec:
            raise Unreadable(f"BENCHMARK_FIELD_MISSING: {k}")
    if _git(repo, "cat-file", "-e", "HEAD:factory_v2/run_matched_benchmark.py").returncode:
        raise Unmet("PRODUCER_MISSING: factory_v2/run_matched_benchmark.py")
    for k in ("champion_sha", "challenger_sha"):
        if _git(repo, "cat-file", "-e", f"{rec[k]}^{{commit}}").returncode or _git(repo, "merge-base", "--is-ancestor", rec[k], "HEAD").returncode:
            raise Unmet(f"SHA_NOT_ANCESTOR: {k}={str(rec[k])[:12]}")
    if rec["champion_sha"] == rec["challenger_sha"]:
        raise Unmet("CHAMPION_EQUALS_CHALLENGER")
    obs_dir = str(rec["observations_dir"]).rstrip("/")
    man = _head_json(repo, f"{obs_dir}/MANIFEST.json")
    prod_sha = hashlib.sha256(_head_src(repo, "factory_v2/run_matched_benchmark.py").encode("utf-8")).hexdigest()
    if man.get("producer_sha256") != prod_sha:
        raise Unmet("MANIFEST_PRODUCER_MISMATCH: observations were not produced by the committed producer")
    for k in ("champion_sha", "challenger_sha", "start_sha", "workload_id"):
        if man.get(k) != rec[k]:
            raise Unmet(f"MANIFEST_FIELD_MISMATCH: {k}")
    if not isinstance(man.get("observations"), dict) or not man["observations"]:
        raise Unmet("MANIFEST_OBSERVATIONS_MISSING")
    sides: dict = {"champion": {}, "challenger": {}}
    for name, digest in man["observations"].items():
        blob = _head_src(repo, f"{obs_dir}/{name}")
        if hashlib.sha256(blob.encode("utf-8")).hexdigest() != digest:
            raise Unmet(f"OBSERVATION_TAMPERED: {name}")
        o = json.loads(blob)
        side, probe = o.get("side"), o.get("probe_id")
        if side not in sides or not probe or o.get("start_sha") != rec["start_sha"] or o.get("workload_id") != rec["workload_id"]:
            raise Unmet(f"OBSERVATION_NOT_MATCHED: {name}")
        if o.get("side_sha") != rec[f"{side}_sha"]:
            raise Unmet(f"OBSERVATION_SIDE_SHA_MISMATCH: {name}")
        sides[side][probe] = o
    listed = set(_git(repo, "ls-tree", "-r", "--name-only", "HEAD", "--", obs_dir).stdout.splitlines())
    manifested = {f"{obs_dir}/{name}" for name in man["observations"]} | {f"{obs_dir}/MANIFEST.json"}
    extra = [p for p in listed if p.endswith(".json") and p not in manifested]
    if extra:
        raise Unmet("OBSERVATIONS_OUTSIDE_MANIFEST: " + ", ".join(extra[:4]))
    if not sides["champion"] or set(sides["champion"]) != set(sides["challenger"]) or len(sides["champion"]) < 5:
        raise Unmet("PROBE_SETS_DIFFER_OR_TOO_FEW: both sides need the same >= 5 probes")

    def agg(side):
        rows = sides[side].values()
        return {"probes": len(rows), "settled": sum(1 for o in rows if o.get("settled") is True),
                "wall_s": round(sum(float(o.get("wall_s") or 0) for o in rows), 3),
                "restart_recovered": sum(1 for o in rows if o.get("restart_recovered") is True),
                "launches_lost": sum(int(o.get("launches_lost") or 0) for o in rows)}
    m = {"champion": agg("champion"), "challenger": agg("challenger")}
    if rec["metrics"] != m:
        raise Unmet(f"METRICS_MISMATCH: recomputed={json.dumps(m)}")
    c, ch = m["champion"], m["challenger"]
    dominates = (ch["settled"] >= c["settled"] and ch["wall_s"] <= c["wall_s"] and ch["launches_lost"] <= c["launches_lost"]
                 and ch["restart_recovered"] >= c["restart_recovered"] and ch != c)
    pointer = _head_src(repo, "factory_v2/CHAMPION_POINTER").strip()
    if rec["decision"] == "PROMOTE":
        if not dominates:
            raise Unmet("PROMOTE_WITHOUT_DOMINATION")
        if pointer != rec["challenger_sha"] or rec["champion_pointer_after"] != rec["challenger_sha"]:
            raise Unmet("PROMOTE_POINTER_MISMATCH: factory_v2/CHAMPION_POINTER at HEAD must equal challenger_sha")
    elif rec["decision"] == "REJECT":
        if pointer != rec["champion_pointer_before"] or rec["champion_pointer_after"] != rec["champion_pointer_before"] or pointer != rec["champion_sha"]:
            raise Unmet("CHAMPION_MUTATED_ON_REJECT")
    else:
        raise Unmet("DECISION_INVALID")
    if _git(repo, "cat-file", "-e", f"HEAD:{rec['decision_evidence_path']}").returncode:
        raise Unmet("DECISION_EVIDENCE_MISSING")
    return {"probes": c["probes"], "decision": rec["decision"], "pointer": pointer[:12]}


def check_self_improvement(repo: Path) -> dict:
    """Snapshots before/after committed in that order and identical on capability fields;
    validator sha recomputed; ledger is a hash chain whose every row names the benchmark
    receipt (sha256 at HEAD); promotions are CAS moves of factory_v2/CHAMPION_POINTER
    whose final state is read AT HEAD."""
    import hashlib
    rec = _head_json(repo, "factory_v2/receipts/CHALLENGER_AUTOMATION_RECEIPT.json")
    if rec.get("schema") != "challenger-automation-receipt/v1":
        raise Unreadable("SELF_IMPROVEMENT_SCHEMA")
    for k in ("snapshot_before", "snapshot_after", "ledger", "promotions", "decision", "decision_evidence_path", "benchmark_receipt"):
        if k not in rec:
            raise Unreadable(f"SELF_IMPROVEMENT_FIELD_MISSING: {k}")
    if _git(repo, "cat-file", "-e", "HEAD:factory_v2/capability_snapshot.py").returncode:
        raise Unmet("PRODUCER_MISSING: factory_v2/capability_snapshot.py")
    before, after = _head_json(repo, rec["snapshot_before"]), _head_json(repo, rec["snapshot_after"])
    for k in ("actor_capabilities", "owner_policy_ids", "validator_source_sha256",
              "effective_validator_source_sha256", "network_policy", "taken_at"):
        if k not in before or k not in after:
            raise Unmet(f"SNAPSHOT_FIELD_MISSING: {k}")
    for k in ("actor_capabilities", "owner_policy_ids", "validator_source_sha256",
              "effective_validator_source_sha256", "network_policy"):
        if before[k] != after[k]:
            raise Unmet(f"CAPABILITY_WIDENED: {k} changed between snapshots (D018/D019)")
    cb = _git(repo, "log", "-1", "--format=%H", "--", rec["snapshot_before"]).stdout.strip()
    ca = _git(repo, "log", "-1", "--format=%H", "--", rec["snapshot_after"]).stdout.strip()
    if not cb or not ca or cb == ca or _git(repo, "merge-base", "--is-ancestor", cb, ca).returncode:
        raise Unmet("SNAPSHOT_ORDER: snapshot_before must be committed strictly before snapshot_after")
    # L5_factory_v2_rest_1-010: the digest covers the shim AND its effective
    # targets — recompute it with the producer's own function, never a
    # shim-only sha256 that two differing validator commits can share.
    from factory_v2.capability_snapshot import _validator_source_sha256
    from factory_v2.capability_snapshot import _head_bytes
    shim_digest = hashlib.sha256(_head_bytes(repo, "factory_v2/product_verifiers.py")).hexdigest()
    if after["validator_source_sha256"] != shim_digest:
        raise Unmet("SNAPSHOT_STALE: validator_source_sha256 != sha256(HEAD: shim)")
    if after["effective_validator_source_sha256"] != _validator_source_sha256(repo):
        raise Unmet("SNAPSHOT_STALE: effective_validator_source_sha256 != digest(HEAD: VALIDATOR_SOURCES)")
    bench_sha = hashlib.sha256(_head_src(repo, rec["benchmark_receipt"]).encode("utf-8")).hexdigest()
    rows = [json.loads(l) for l in _head_src(repo, rec["ledger"]).splitlines() if l.strip()]
    prev = ""
    for i, r in enumerate(rows):
        body = {k: v for k, v in r.items() if k != "row_hash"}
        expect = hashlib.sha256((prev + json.dumps(body, sort_keys=True)).encode("utf-8")).hexdigest()
        if r.get("row_hash") != expect:
            raise Unmet(f"LEDGER_CHAIN_BROKEN at row {i}")
        prev = r["row_hash"]
        if r.get("validated") is not True:
            raise Unmet(f"UNVALIDATED_SELF_MODIFICATION: row {i}")
        if r.get("benchmark_receipt_sha256") != bench_sha:
            raise Unmet(f"LEDGER_ROW_NOT_BOUND_TO_BENCHMARK: row {i}")
        if not re.fullmatch(r"[0-9a-f]{40}", str(r.get("sha") or "")) or _git(repo, "merge-base", "--is-ancestor", r["sha"], "HEAD").returncode:
            raise Unmet(f"LEDGER_ROW_SHA_INVALID: row {i}")
    pointer = _head_src(repo, "factory_v2/CHAMPION_POINTER").strip()
    for p in rec["promotions"]:
        sha = str(p.get("sha") or "")
        if not re.fullmatch(r"[0-9a-f]{40}", sha) or not p.get("cas_expected") or p.get("cas_expected") != p.get("cas_observed"):
            raise Unmet("PROMOTION_NOT_EXACT_SHA_CAS")
        if _git(repo, "cat-file", "-e", f"{sha}^{{commit}}").returncode or _git(repo, "merge-base", "--is-ancestor", sha, "HEAD").returncode:
            raise Unmet(f"PROMOTION_SHA_NOT_ANCESTOR: {sha[:12]}")
    if rec["promotions"] and pointer != rec["promotions"][-1]["sha"]:
        raise Unmet("POINTER_NOT_AT_LAST_PROMOTION")
    if rec["decision"] not in ("PROMOTE", "REJECT") or _git(repo, "cat-file", "-e", f"HEAD:{rec['decision_evidence_path']}").returncode:
        raise Unmet("DECISION_EVIDENCE_MISSING")
    return {"ledger_rows": len(rows), "promotions": len(rec["promotions"]), "pointer": pointer[:12]}


CHECKS.update({
    "adaptive-routing": check_adaptive_routing,
    "worker-slots": check_worker_slots,
    "matched-benchmark": check_matched_benchmark,
    "self-improvement": check_self_improvement,
})

RELOAD_SENSITIVE = ("factory_v2/runtime.py", "factory_v2/launch_gate.py", "factory_v2/recovery_allocation.py",
                    "factory_v2/executors.py", "factory_v2/mission_compiler.py", "factory_v2/authority.py",
                    "factory/bin/factoryd.py")


def check_hot_reload(repo: Path) -> dict:
    """factory_v2/hot_reload.py decides, from a settled candidate's touched files, whether
    the in-memory daemon is stale (reload_needed(touched) is True iff one of
    RELOAD_SENSITIVE changed); factoryd schedules RELOAD_SCHEDULED and exits non-zero only
    when nothing is in flight; launchd KeepAlive restarts it. Judged by a hermetic probe."""
    probe = (
        "import sys, json; sys.path.insert(0, '.')\n"
        "from factory_v2 import hot_reload as hr\n"
        "assert hr.reload_needed(['factory_v2/runtime.py']) is True\n"
        "assert hr.reload_needed(['factory/bin/dispatcher_v3.py', 'factory_v2/verify_factory_hygiene.py']) is False\n"
        "assert hr.reload_needed([]) is False\n"
        "assert set(hr.RELOAD_SENSITIVE) >= " + repr(set(RELOAD_SENSITIVE)) + "\n"
        "d = hr.decide(in_flight=1, pending=True); assert d == 'WAIT', d\n"
        "d = hr.decide(in_flight=0, pending=True); assert d == 'RELOAD_NOW', d\n"
        "d = hr.decide(in_flight=0, pending=False); assert d == 'NOOP', d\n"
        "print('OK')\n"
    )
    r = subprocess.run([sys.executable, "-c", probe], cwd=repo, capture_output=True, text=True, timeout=120)
    if "OK" not in r.stdout:
        raise Unmet(f"HOT_RELOAD_MODULE: {(r.stderr or r.stdout)[-300:]}")
    fd = _head_src(repo, "factory/bin/factoryd.py")
    if "hot_reload" not in fd or "RELOAD_SCHEDULED" not in fd:
        raise Unmet("DAEMON_NOT_WIRED: factoryd must consult factory_v2.hot_reload after a factory-project settlement and log RELOAD_SCHEDULED")
    rt = _head_src(repo, "factory_v2/runtime.py")
    if "touched_files" not in rt and "touched_files" not in fd:
        raise Unmet("SETTLEMENT_TOUCHED_FILES_MISSING: the settlement path must expose the candidate's touched files (git diff --name-only base..integration)")
    plist = Path.home() / "Library" / "LaunchAgents" / "com.factory.factoryd.plist"
    if plist.is_file() and "KeepAlive" not in plist.read_text(errors="ignore"):
        raise Unmet("LAUNCHD_KEEPALIVE_MISSING")
    tests = _git(repo, "ls-tree", "-r", "--name-only", "HEAD", "--", "tests").stdout
    if "test_hot_reload" not in tests:
        raise Unmet("HERMETIC_TEST_MISSING: tests/test_hot_reload*.py")
    return {"sensitive": len(RELOAD_SENSITIVE)}


CHECKS["hot-reload"] = check_hot_reload

# =========================================================================== owner console (decision CTO 2026-09-13)
AUTO_RESOLVABLE_KINDS = ("STANDING_CAP_REACHED", "WAITING_FOR_EVIDENCE", "CALENDAR_WAIT", "RELOAD_SCHEDULED",
                         "COST_NO_PROGRESS_TERMINAL", "CNP_REGRANT_WITHHELD", "RECOVERY_LAUNCH_BLOCKED")
VALID_EXCEPTION_KINDS = ("OWNER_DECISION_REQUIRED", "MAINTENANCE_REQUEST", "AUTHORITY_AMENDMENT_PENDING",
                         "VISUAL_VALIDATION", "GRANT_NEEDED", "CREDENTIAL_OR_PAYMENT", "PRODUCT_DECISION")


def check_owner_console(repo: Path) -> dict:
    """factory_v2/owner_console.py is a READ-ONLY producer: run it into a tmp dir and
    recompute every number it prints from the same artefacts."""
    src = _head_src(repo, "factory_v2/owner_console.py")
    if re.search(r"^\s*(from|import)\s+(factory_v2\.)?(runtime|executors|launch_gate|dispatcher_v3|recovery_allocation)\b", src, flags=re.M):
        raise Unmet("CONSOLE_NOT_READ_ONLY: owner_console imports runtime/dispatch modules")
    if re.search(r"\b(open\([^)]*['\"]w|write_text\(|os\.replace\(|json\.dump\()", src) and "--out" not in src:
        raise Unmet("CONSOLE_WRITES_OUTSIDE_OUT")
    with tempfile.TemporaryDirectory() as out:
        r = subprocess.run([sys.executable, "-m", "factory_v2.owner_console", "--out", out, "--projects",
                            "projects/factory", "projects/twitter-radar"], cwd=repo, capture_output=True, text=True, timeout=300)
        if r.returncode:
            raise Unmet(f"CONSOLE_FAILED: rc={r.returncode} {r.stderr[-300:]}")
        js, md = Path(out, "OWNER_CONSOLE.json"), Path(out, "OWNER_CONSOLE.md")
        if not js.is_file() or not md.is_file():
            raise Unmet("CONSOLE_OUTPUT_MISSING")
        data = json.loads(js.read_text(encoding="utf-8"))
        text = md.read_text(encoding="utf-8")
    for k in ("generated_at", "exceptions", "progress", "increments_24h", "health"):
        if k not in data:
            raise Unmet(f"CONSOLE_SECTION_MISSING: {k}")
    for sec in ("Exceptions", "Progression", "Incréments", "Santé"):
        if sec.lower() not in text.lower():
            raise Unmet(f"CONSOLE_MD_SECTION_MISSING: {sec}")
    # exceptions: typed, sourced, none auto-resolvable
    for e in data["exceptions"]:
        if e.get("kind") not in VALID_EXCEPTION_KINDS:
            raise Unmet(f"EXCEPTION_KIND_INVALID: {e.get('kind')} (AUTO_RESOLVABLE_HUMAN_BLOCK must be 0)")
        for k in ("id", "project", "since", "source", "suggested_action"):
            if not e.get(k):
                raise Unmet(f"EXCEPTION_FIELD_MISSING: {k}")
        if not Path(repo, str(e["source"])).exists() and not Path(str(e["source"])).exists():
            raise Unmet(f"EXCEPTION_SOURCE_MISSING: {e['source']}")
    if any(k in json.dumps(data["exceptions"]) for k in AUTO_RESOLVABLE_KINDS):
        raise Unmet("AUTO_RESOLVABLE_SHOWN_AS_EXCEPTION")
    # progress recomputed from PROJECT_STATE
    import yaml as _yaml
    for proj in ("factory", "twitter-radar"):
        st = json.loads(Path(repo, "projects", proj, "PROJECT_STATE.json").read_text(encoding="utf-8"))
        rm = _yaml.safe_load(Path(repo, "projects", proj, "ROADMAP.yaml").read_text(encoding="utf-8"))
        ids = [m["id"] for m in rm["milestones"]]
        complete = sum(1 for i in ids if (st.get("completed_milestones") or {}).get(i, {}).get("state") == "COMPLETE")
        p = (data["progress"] or {}).get(proj)
        if not p or p.get("total") != len(ids) or p.get("complete") != complete:
            raise Unmet(f"PROGRESS_MISMATCH: {proj} recomputed complete={complete}/{len(ids)} vs {p}")
        if "current_milestone" not in p or "in_flight" not in p:
            raise Unmet(f"PROGRESS_FIELD_MISSING: {proj}")
    # increments 24h == MILESTONE_SETTLED events in window
    import datetime as _dt
    cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=24)
    settled = set()
    for proj in ("factory", "twitter-radar"):
        st = json.loads(Path(repo, "projects", proj, "PROJECT_STATE.json").read_text(encoding="utf-8"))
        for ev in st.get("events") or []:
            if (ev.get("event") or ev.get("kind")) == "MILESTONE_SETTLED":
                ts = str(ev.get("ts") or ev.get("at") or ev.get("timestamp") or "")
                try:
                    t = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if t >= cutoff:
                    settled.add((proj, ev.get("milestone")))
    got = {(i.get("project"), i.get("milestone")) for i in data["increments_24h"]}
    if got != settled:
        raise Unmet(f"INCREMENTS_MISMATCH: recomputed={sorted(settled)} got={sorted(got)}")
    for k in ("daemon_pid", "last_heartbeat_age_s", "standing_cap", "quota"):
        if k not in data["health"]:
            raise Unmet(f"HEALTH_FIELD_MISSING: {k}")
    return {"exceptions": len(data["exceptions"]), "increments_24h": len(got)}


CHECKS["owner-console"] = check_owner_console


def _called_names_at_head(repo: Path, rel: str) -> set:
    tree = ast.parse(_head_src(repo, rel))
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            f = n.func
            out.add(f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else ""))
    return out


def check_resilience(repo: Path) -> dict:
    """FACTORY_RESILIENCE_V1 (owner 2026-09-13): a cut is an interruption, never an
    engine failure; checkpoints are frequent and reachable; the quota switch happens
    before the cut; the daemon waits for the network instead of spending.

    Judged from HEAD sources (AST calls, not greps of comments) plus a live drill
    in a temporary git repo run with the module AT HEAD."""
    # 1. wiring: real CALL nodes in the production modules
    need = {
        "factory/bin/dispatcher_v3.py": {"classify_interruption_detail", "wait_for_network", "WorktreeCheckpointer",
                                         "_mark_provider_exhausted", "_resume_preamble",
                                         "write_interruption_marker", "_write_cut_marker",
                                         # Codex re-audit #2 (9/10): execve-time allowance check
                                         "_quota_engine_allowance"},
        "factory_v2/advance/mission_dispatch.py": {"_dispatcher_interruption"},
        "factory/bin/factoryd.py": {"network_available", "connectivity_state"},
        "factory/bin/model_router.py": {"projected_allowance", "_quota_unknown_ration"},
    }
    for rel, names in need.items():
        called = _called_names_at_head(repo, rel)
        missing = sorted(n for n in names if n not in called)
        if missing:
            raise Unmet(f"RESILIENCE_NOT_WIRED: {rel} never calls {missing}")
    # 2. typed exit classes + evidence exclusion
    ra = _head_src(repo, "factory_v2/resource_accounting.py")
    if '"NETWORK"' not in ra or '"QUOTA"' not in ra:
        raise Unmet("EXIT_CLASSES_MISSING_CUT_TYPES")
    # Codex re-audit #2 (finding 7): WAITING_NETWORK is non-progress with a bounded backoff
    fd = _head_src(repo, "factory/bin/factoryd.py")
    stall = fd[max(0, fd.find("_v2_consecutive_blocked += 1") - 700):fd.find("_v2_consecutive_blocked += 1")]
    if '"WAITING_NETWORK"' not in stall or 'if action == "WAITING_NETWORK":' not in fd:
        raise Unmet("WAITING_NETWORK_NOT_COUNTED_OR_UNBOUNDED")
    # Codex re-audit #2 (new finding 1): a second NETWORK_CUT still writes its marker
    dv = _head_src(repo, "factory/bin/dispatcher_v3.py")
    body = dv[dv.find("def _one_launch("):dv.find("DIAGNOSE / REPLAN")]
    if body.count("_write_cut_marker(_resilience.NETWORK_CUT") < 2 or "QUOTA_REFUSED_RC" not in body:
        raise Unmet("SECOND_NETWORK_CUT_UNMARKED_OR_NO_PRELAUNCH_QUOTA_REFUSAL")
    # 3. live drill with the module at HEAD: classify, checkpoint, kill, restore, evidence
    probe = (
        "import sys, json, subprocess, tempfile, pathlib\n"
        "sys.path.insert(0, '.')\n"
        "from factory_v2 import resilience as rs\n"
        "from factory_v2.routing_evidence import evidence_from_ledger\n"
        "assert rs.classify_interruption(1, '', 'usage limit reached') == 'QUOTA_CUT'\n"
        "assert rs.classify_interruption(1, '', 'fetch failed ECONNRESET') == 'NETWORK_CUT'\n"
        "assert rs.classify_interruption(1, '', 'AssertionError') == ''\n"
        "assert rs.classify_interruption(0, '', 'usage limit reached') == ''\n"
        # Codex audit sc2: a cut phrase on STDOUT (model text / test log) is NOT a cut\n"
        "assert rs.classify_interruption(1, 'connection refused', '') == '', 'STDOUT_FALSE_POSITIVE'\n"
        # Codex audit sc2: a cut must not overrule a real test verdict\n"
        "import factory_v2.runtime as _rt\n"
        "class _RT:\n"
        "    ok=False; receipt={'engine_executions':[{'interruption':'NETWORK_CUT'}],'stages':[{'step':'test_2','rc':1}]}\n"
        "assert _rt._dispatcher_interruption(_RT())=='', 'CUT_OVERRULED_TEST_VERDICT'\n"
        # Codex audit sc6: resume marker round-trip + mismatch\n"
        "import tempfile as _tf2, pathlib as _pl2\n"
        "_md=_pl2.Path(_tf2.mkdtemp()); rs.write_interruption_marker(_md,'M#1','b1',rs.NETWORK_CUT)\n"
        "assert rs.consume_interruption_marker(_md,'M#1','zzz')=={}, 'MARKER_MISMATCH_ACCEPTED'\n"
        "rs.write_interruption_marker(_md,'M#1','b1',rs.NETWORK_CUT)\n"
        "assert rs.consume_interruption_marker(_md,'M#1','b1').get('cut')=='NETWORK_CUT' and rs.consume_interruption_marker(_md,'M#1','b1')=={}, 'MARKER_NOT_CONSUMED'\n"
        # Codex audit sc4: offline connectivity state\n"
        "import os as _os2; _os2.environ['FACTORY_FORCE_OFFLINE']='1'\n"
        "assert rs.connectivity_state()=='OFFLINE' and rs.network_available() is False\n"
        # Codex re-audit #2: engine-targeted probe, structural classification, stderr tail only\n"
        "assert rs.engine_reachable('claude') is False and rs.connectivity_state(engine='codex')=='OFFLINE', 'ENGINE_PROBE_MISSING'\n"
        "assert rs.classify_interruption_detail(1,'','unknown error',engine='claude',probe=True)['basis']=='PROBE', 'NO_STRUCTURAL_CUT'\n"
        "assert rs.classify_interruption(1,'','usage limit reached',probe=True)=='QUOTA_CUT'\n"
        "_os2.environ.pop('FACTORY_FORCE_OFFLINE',None)\n"
        "assert rs.classify_interruption(1,'','a'*10+'connection refused'+'b'*(rs.STDERR_TAIL_CHARS+5))=='', 'MID_STDERR_FALSE_POSITIVE'\n"
        # Codex re-audit #3 (finding 3): only the final diagnostic lines are matched\n"
        "_cs=rs.connectivity_state; rs.connectivity_state=lambda *a,**k: 'ENGINE_UP'\n"
        "assert rs.classify_interruption_detail(1,'','API Error: connection refused',engine='claude',probe=True)['basis']==rs.UNCONFIRMED, 'TEXT_ONLY_CUT_NOT_UNCONFIRMED'\n"
        "rs.connectivity_state=_cs\n"
        "_dv=open('factory/bin/dispatcher_v3.py').read(); assert 'UNCONFIRMED_TEXT_ONLY_TWICE' in _dv, 'SECOND_UNCONFIRMED_CUT_NOT_WITHDRAWN'\n"
        # Codex re-audit #4: orphan claim adopted\n"
        "rs.write_interruption_marker(_md,'D#1','b1',rs.NETWORK_CUT,attempt=7); _pp=rs._marker_path(_md,'D#1'); _os2.rename(_pp,_pp.with_name(_pp.name+'.claim-1-1'))\n"
        "assert rs.consume_interruption_marker(_md,'D#1','b1').get('attempt')==7, 'ORPHAN_CLAIM_LOST'\n"
        # Codex re-audit #3 (finding 8): probe demands an HTTP status line, not just TLS\n"
        "import inspect as _ins; _src=_ins.getsource(rs.engine_reachable); assert 'HEAD / HTTP/1.1' in _src and 'startswith(\"HTTP/1.\")' in _src, 'PROBE_WITHOUT_API_REQUEST'\n"
        # Codex re-audit #3 (new): atomic consume — mismatch restores, no claim/tmp residue\n"
        "rs.write_interruption_marker(_md,'C#1','b1',rs.NETWORK_CUT); assert rs.consume_interruption_marker(_md,'C#1','nope')=={} and rs._marker_path(_md,'C#1').exists() and not list(rs._marker_path(_md,'C#1').parent.glob('*.claim-*')) and not list(rs._marker_path(_md,'C#1').parent.glob('*.tmp')), 'MARKER_CONSUME_NOT_ATOMIC'\n"
        "assert rs.classify_interruption(1,'','502 Bad Gateway')=='NETWORK_CUT' and rs.classify_interruption(1,'','stream disconnected')=='NETWORK_CUT', 'PROVIDER_OUTAGE_UNCLASSIFIED'\n"
        # Codex re-audit #2: per-mission marker, identity checked before deletion, attempt+checkpoint carried\n"
        "rs.write_interruption_marker(_md,'A#1','b1',rs.NETWORK_CUT,attempt=2,checkpoint='c0ffee'); rs.write_interruption_marker(_md,'B#1','b1',rs.QUOTA_CUT)\n"
        "assert rs.consume_interruption_marker(_md,'A#1','zzz')=={} and rs._marker_path(_md,'A#1').exists(), 'MARKER_DELETED_ON_MISMATCH'\n"
        "assert rs.consume_interruption_marker(_md,'B#1','b1').get('cut')=='QUOTA_CUT' and rs._marker_path(_md,'A#1').exists(), 'MARKER_SHARED_ACROSS_MISSIONS'\n"
        "_m=rs.consume_interruption_marker(_md,'A#1','b1'); assert _m.get('attempt')==2 and _m.get('checkpoint')=='c0ffee', 'MARKER_LACKS_ATTEMPT_CHECKPOINT'\n"
        # Codex re-audit #2 (finding 10): unknown quota is journaled and capped\n"
        "sys.path.insert(0,'factory/bin'); import model_router as _mr; _jt=_pl2.Path(_tf2.mkdtemp())/'q.jsonl'\n"
        "_mr._quota_unknown_journal=lambda root: _jt; _os2.environ[_mr.QUOTA_UNKNOWN_CAP_ENV]='1'\n"
        "assert _mr._quota_unknown_ration(_jt.parent,'codex')==(True,'') and _mr._quota_unknown_ration(_jt.parent,'codex')[0] is False, 'QUOTA_UNKNOWN_UNCAPPED'\n"
        # Codex re-audit #3 (finding 10): lookup errors are journaled + capped too\n"
        "import factory_v2.quota_observer as _qo3; _ls=_qo3.load_snapshot\n"
        "def _boom(*a,**k): raise OSError('corrupt')\n"
        "_qo3.load_snapshot=_boom; _jt.write_text('')\n"
        "assert _mr._quota_engine_allowance('codex',None,launching=True)==(True,'') and _mr._quota_engine_allowance('codex',None,launching=True)[0] is False, 'LOOKUP_ERROR_FAIL_OPEN_UNCAPPED'\n"
        "assert _mr._quota_engine_allowance('codex',None)==(True,'') and _mr._quota_engine_allowance('codex',None)==(True,''), 'ROUTING_CALL_RATIONED'\n"
        "_qo3.load_snapshot=_ls\n"
        "_os2.environ.pop(_mr.QUOTA_UNKNOWN_CAP_ENV,None)\n"
        "d = pathlib.Path(tempfile.mkdtemp()) / 'r'; d.mkdir()\n"
        "def g(*a): return subprocess.run(['git', '-C', str(d), *a], capture_output=True, text=True, check=True).stdout.strip()\n"
        "g('init', '-q'); g('config', 'user.email', 'j@j'); g('config', 'user.name', 'j')\n"
        "(d / 'a').write_text('1'); g('add', '-A'); g('commit', '-qm', 'b'); head = g('rev-parse', 'HEAD')\n"
        "(d / 'a').write_text('2'); (d / 'u').write_text('x')\n"
        "sha = rs.checkpoint_worktree(d, 'X#1'); assert sha, 'NO_CHECKPOINT'\n"
        "assert g('rev-parse', 'HEAD') == head, 'HEAD_MOVED'\n"
        "assert g('status', '--porcelain').strip(), 'WORKTREE_TOUCHED'\n"
        "assert g('show', sha + ':u') == 'x', 'UNTRACKED_LOST'\n"
        "(d / 'u').unlink(); (d / 'a').write_text('1')\n"
        "assert rs.restore_checkpoint(d, 'X#1') == sha and (d / 'u').read_text() == 'x' and (d / 'a').read_text() == '2', 'RESTORE_FAILED'\n"
        # Codex audit sc1: restore refuses to clobber unrelated live work\n"
        "(d / 'live').write_text('unrelated'); \n"
        "try:\n"
        "    rs.restore_checkpoint(d, 'X#1'); assert False, 'RESTORE_CLOBBERED_LIVE_WORK'\n"
        "except rs.RestoreRefused:\n"
        "    pass\n"
        # Codex re-audit #2 (finding 2): a CAS miss parks the commit under a per-writer ref\n"
        "(d / 'a').write_text('3'); _real=rs._git\n"
        "def _mv(r,*a,**k):\n"
        "    if a[:1]==('update-ref',) and len(a)==4 and a[1]==rs._ref_name('X#1'):\n"
        "        _o=_real(r,'commit-tree',a[3]+'^{tree}','-p',a[3],'-m','other').stdout.strip(); _real(r,'update-ref',a[1],_o,a[3])\n"
        "    return _real(r,*a,**k)\n"
        "rs._git=_mv; assert rs.checkpoint_worktree(d,'X#1')=='' ; rs._git=_real\n"
        "assert g('for-each-ref','--format=%(refname)',rs.REF_PREFIX+'/X_1--lost-*').strip(), 'CAS_MISS_CHECKPOINT_LOST'\n"
        "assert rs.projected_allowance('claude', {'7d': {'utilisation': 0.88}})['allowed'] is False, 'NO_PREEMPTIVE_SWITCH'\n"
        "assert rs.projected_allowance('claude', {})['allowed'] is True, 'UNKNOWN_REFUSED'\n"
        "p = pathlib.Path(tempfile.mkdtemp()); (p / 'evidence').mkdir()\n"
        "rows = [dict(phase='TERMINAL', attempt_key=f'k{i}', engine='claude', spec_level='A', task_kind='build', consumes_model_quota=True, mission_pass=False, independently_validated=False, exit_class=ec) for i, ec in enumerate(['QUOTA', 'NETWORK', 'KILL', 'SEMANTIC_FAILURE'])]\n"
        "(p / 'evidence' / 'attempt_accounts.jsonl').write_text(''.join(json.dumps(r) + chr(10) for r in rows))\n"
        "assert evidence_from_ledger(p, 'A', 'build') == {'claude': {'n': 1, 'success': 0}}, 'CUTS_COUNTED_AS_FAILURES'\n"
        "print('DRILL_OK')\n"
    )
    r = subprocess.run([sys.executable, "-c", probe], cwd=repo, capture_output=True, text=True, timeout=180,
                       env={**os.environ, "FACTORY_NO_REAL_ENGINE": "1"})
    if r.returncode or "DRILL_OK" not in r.stdout:
        raise Unmet(f"RESILIENCE_DRILL_FAILED: {r.stderr[-400:]}")
    # 4. the drills are in the suite and pass
    t = subprocess.run([sys.executable, "-m", "pytest", "-q", "tests/test_resilience.py", "-p", "no:cacheprovider"],
                       cwd=repo, capture_output=True, text=True, timeout=600)
    if t.returncode:
        raise Unmet(f"RESILIENCE_TESTS_RED: {t.stdout[-300:]}")
    return {"wired": sum(len(v) for v in need.values()), "drill": "ok"}


CHECKS["resilience"] = check_resilience



# ── FACTORY_ROBUSTNESS_A (owner 2026-09-17: « lancer tout ça ») ─────────────
def _probe(repo: Path, code: str, timeout: int = 180, env: dict | None = None) -> str:
    r = subprocess.run([sys.executable, "-c", code], cwd=repo, capture_output=True, text=True,
                       timeout=timeout, env={**os.environ, "FACTORY_NO_REAL_ENGINE": "1", **(env or {})})
    if r.returncode != 0:
        raise Unmet(f"DRILL_FAILED: {(r.stderr or r.stdout)[-600:]}")
    return r.stdout


def check_orphan_by_session(repo: Path) -> dict:
    """A1 — orphan engine sweep by SESSION/LEASE membership, not argv regex.
    Every launch records (pid, pgid, session id, birth) in a durable registry the
    daemon reads; a live process is an orphan iff its session belongs to a lease
    that is closed/expired, whatever its command line looks like."""
    src = _head_src(repo, "factory/bin/factoryd.py")
    if "_sweep_orphan_engines" not in src:
        raise Unmet("SWEEPER_MISSING")
    if 're.match(r"^(codex exec|claude -p' in src and "engine_registry" not in src:
        raise Unmet("SWEEP_STILL_ARGV_REGEX_ONLY")
    for rel, names in {"factory/bin/dispatcher_v3.py": {"register_engine_process"},
                       "factory/bin/factoryd.py": {"orphans_from_registry",
                                                   "production_killable"}}.items():
        missing = sorted(n for n in names if n not in _called_names_at_head(repo, rel))
        if missing:
            raise Unmet(f"NOT_WIRED: {rel} never calls {missing}")
    out = _probe(repo, (
        "import sys, os, time, subprocess, tempfile, pathlib, json\n"
        "sys.path.insert(0, '.')\n"
        "from factory_v2 import engine_registry as er\n"
        "d = pathlib.Path(tempfile.mkdtemp())\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], preexec_fn=os.setsid)\n"
        "er.register_engine_process(d, lease_id='L1', mission_id='M#1', pid=p.pid)\n"
        "assert er.orphans_from_registry(d, live_leases={'L1'}, max_age_s=0) == [], 'LIVE_LEASE_FLAGGED'\n"
        # The primitive is PURE membership/age (pinned 6c95148e semantics): a
        # closed lease is flagged IMMEDIATELY, young entry and live launcher
        # notwithstanding. The production guards live one level up.
        "o = er.orphans_from_registry(d, live_leases=set(), max_age_s=0)\n"
        "assert o and o[0]['pid'] == p.pid, 'CLOSED_LEASE_NOT_FLAGGED'\n"
        # Production sweep guard (2026-09-17, moved out of the primitive to
        # keep it pinned-pure): the launcher (this probe) still owns the
        # engine — the sweep killed every fresh build 8 s after execve when
        # these guards were missing. It must be spared; a dead launcher past
        # the grace window is killable.
        "assert er.production_killable(o, max_age_s=3 * 3600) == [], 'LIVE_LAUNCHER_KILLED'\n"
        "recs = er.read_registry(d)\n"
        "for r in recs: r['launcher_pid'] = 999999\n"
        "er.MIN_ORPHAN_AGE_S = 0\n"
        "er.registry_path(d).write_text(chr(10).join(json.dumps(r) for r in recs) + chr(10))\n"
        "o2 = er.orphans_from_registry(d, live_leases=set(), max_age_s=0)\n"
        "k = er.production_killable(o2, max_age_s=3 * 3600)\n"
        "assert k and k[0]['pid'] == p.pid, 'DEAD_LAUNCHER_NOT_KILLABLE'\n"
        "er.kill_orphans(d, k); time.sleep(0.5); assert p.poll() is not None, 'ORPHAN_NOT_KILLED'\n"
        "print('DRILL_OK')\n"))
    if "DRILL_OK" not in out:
        raise Unmet("DRILL_INCOMPLETE")
    return {"registry": "engine_registry", "drill": "ok"}


def check_reload_canary(repo: Path) -> dict:
    """A2 — hot reload runs a CANARY (one hermetic advance() against the new code
    in a subprocess) before sys.exit(3); a failing canary defers the reload and
    records a typed incident instead of restarting into broken code."""
    src = _head_src(repo, "factory/bin/factoryd.py")
    if "RELOAD_NOW" not in src:
        raise Unmet("RELOAD_PATH_MISSING")
    called = _called_names_at_head(repo, "factory/bin/factoryd.py")
    if "reload_canary" not in called:
        raise Unmet("CANARY_NOT_WIRED: factoryd never calls reload_canary()")
    hr = _head_src(repo, "factory_v2/hot_reload.py")
    if "def reload_canary" not in hr or "RELOAD_CANARY_FAILED" not in hr:
        raise Unmet("CANARY_MISSING_OR_UNTYPED")
    out = _probe(repo, (
        "import sys, pathlib, tempfile, subprocess\n"
        "sys.path.insert(0, '.')\n"
        "from factory_v2 import hot_reload as hr\n"
        "ok, why = hr.reload_canary(pathlib.Path('.'), python=sys.executable)\n"
        "assert ok, 'CANARY_FAILS_ON_HEAD: ' + why\n"
        "d = pathlib.Path(tempfile.mkdtemp()); (d / 'factory_v2').mkdir(); (d / 'factory_v2' / '__init__.py').write_text('raise ImportError(\"broken\")')\n"
        "ok2, why2 = hr.reload_canary(d, python=sys.executable)\n"
        "assert not ok2 and 'RELOAD_CANARY_FAILED' in why2, 'BROKEN_TREE_PASSED_CANARY'\n"
        "print('DRILL_OK')\n"))
    if "DRILL_OK" not in out:
        raise Unmet("DRILL_INCOMPLETE")
    return {"canary": "ok"}


def check_portfolio_isolation(repo: Path) -> dict:
    """A3 — a project with unreadable authority/state is refused ALONE (typed
    incident, visible in the owner console); the valid projects keep rotating."""
    out = _probe(repo, (
        "import sys, json, pathlib, tempfile, yaml\n"
        "sys.path.insert(0, '.')\n"
        "from factory_v2.runtime import select_project\n"
        "root = pathlib.Path(tempfile.mkdtemp()); (root / 'projects').mkdir()\n"
        "good = root / 'projects' / 'good'; good.mkdir(); (good / 'evidence').mkdir()\n"
        "(good / 'ROADMAP.yaml').write_text(yaml.safe_dump({'version': 'v3', 'milestones': [{'id': 'A', 'goal': 'g', 'acceptance': ['x'], 'execution': {'steps': [{'kind': 'implement', 'executor': {'type': 'local_command', 'command': 'true'}}]}}]}))\n"
        "(good / 'SPEC.md').write_text('# s'); (good / 'STRATEGY.md').write_text('# s')\n"
        "(good / 'PROJECT_STATE.json').write_text(json.dumps({'project': 'good', 'completed_milestones': {}, 'missions': {}, 'events': []}))\n"
        "bad = root / 'projects' / 'bad'; bad.mkdir(); (bad / 'ROADMAP.yaml').write_text('milestones: [')\n"
        "(root / 'portfolio.json').write_text(json.dumps({'schema': 'portfolio/v1', 'projects': [{'project_dir': 'projects/bad'}, {'project_dir': 'projects/good'}]}))\n"
        "import os; os.environ['FACTORY_AUTHORITY_FROM_WORKTREE'] = '1'\n"
        "r = select_project(root, root / 'portfolio.json', cursor_path=root / 'cursor.json')\n"
        "assert r.kind == 'PROJECT_SELECTED' and r.project_dir and r.project_dir.name == 'good', ('BAD_PROJECT_BLOCKS_PORTFOLIO', r)\n"
        "inc = (root / 'projects' / 'bad' / 'evidence' / 'progress_incidents.jsonl')\n"
        "assert inc.is_file() and 'PROJECT_AUTHORITY_UNREADABLE' in inc.read_text(), 'INCIDENT_NOT_DURABLE'\n"
        "print('DRILL_OK')\n"))
    if "DRILL_OK" not in out:
        raise Unmet("DRILL_INCOMPLETE")
    return {"isolation": "ok"}


def check_intra_project_parallel(repo: Path) -> dict:
    """A4 — two missions of ONE project with disjoint deliverables run at the same
    time: advance() releases the project lock for the duration of the engine
    launch (lease-guarded), so a second advance() can dispatch another milestone."""
    src = _head_src(repo, "factory_v2/advance/mission_dispatch.py")
    if "_parallel_alternate" not in _head_src(repo, "factory_v2/advance/milestone_selection.py"):
        raise Unmet("ALTERNATE_SELECTION_MISSING")
    if "release_project_lock_for_launch" not in src and "lock_released_for_launch" not in src:
        raise Unmet("LOCK_HELD_DURING_LAUNCH: project lock must be released while the engine runs")
    out = _probe(repo, (
        "import sys, json, pathlib, tempfile, yaml, threading, time, os\n"
        "sys.path.insert(0, '.'); os.environ['FACTORY_MAX_WORKERS'] = '3'; os.environ['FACTORY_AUTHORITY_FROM_WORKTREE'] = '1'\n"
        "from factory_v2.runtime import advance\n"
        "pd = pathlib.Path(tempfile.mkdtemp()) / 'p'; pd.mkdir(); (pd / 'evidence').mkdir()\n"
        "steps = lambda cmd: {'steps': [{'kind': 'implement', 'executor': {'type': 'local_command', 'command': cmd}}]}\n"
        "(pd / 'ROADMAP.yaml').write_text(yaml.safe_dump({'version': 'v3', 'milestones': [\n"
        "  {'id': 'A', 'goal': 'g', 'acceptance': ['x'], 'deliverables': ['a.txt'], 'execution': steps('sleep 4; echo a > a.txt')},\n"
        "  {'id': 'B', 'goal': 'g', 'acceptance': ['x'], 'deliverables': ['b.txt'], 'execution': steps('sleep 4; echo b > b.txt')}]}))\n"
        "(pd / 'SPEC.md').write_text('# s'); (pd / 'STRATEGY.md').write_text('# s')\n"
        "(pd / 'PROJECT_STATE.json').write_text(json.dumps({'project': 'p', 'completed_milestones': {}, 'missions': {}, 'events': []}))\n"
        "res = {}\n"
        "def run(k): res[k] = advance(pd)\n"
        "t1 = threading.Thread(target=run, args=('1',)); t1.start(); time.sleep(1.0)\n"
        "t2 = threading.Thread(target=run, args=('2',)); t2.start(); t0 = time.monotonic(); t1.join(); t2.join()\n"
        "acts = sorted(r.action for r in res.values()); mids = sorted(str(r.milestone) for r in res.values())\n"
        "assert 'PROJECT_BUSY' not in acts, ('SECOND_ADVANCE_REFUSED_AS_BUSY', acts)\n"
        "assert mids == ['A', 'B'], ('NOT_TWO_DISTINCT_MILESTONES', mids, acts)\n"
        "print('DRILL_OK')\n"), timeout=300)
    if "DRILL_OK" not in out:
        raise Unmet("DRILL_INCOMPLETE")
    return {"intra_project_parallel": "ok"}


def check_ledger_engine_known(repo: Path) -> dict:
    """A5 — no TERMINAL ledger row may carry engine=UNKNOWN when the launch was
    refused before execve or the argv is known: refusals are typed engine='none'
    with a reason, launches project engine/model from the receipt argv."""
    src = _head_src(repo, "factory_v2/launch_gate.py") + _head_src(repo, "factory_v2/resource_accounting.py")
    if "engine_from_refusal" not in src and "ENGINE_NONE_REFUSED" not in src:
        raise Unmet("REFUSAL_ENGINE_NOT_TYPED")
    out = _probe(repo, (
        "import sys, json, pathlib\n"
        "sys.path.insert(0, '.')\n"
        "from factory_v2 import resource_accounting as ra\n"
        "row = ra.attempt_account_fixture_row()\n"
        "assert row.get('engine') not in ('', 'UNKNOWN', None), 'FIXTURE_ENGINE_UNKNOWN'\n"
        "from factory_v2.launch_gate import engine_for_close\n"
        "assert engine_for_close({'argv_head': ['opencode', 'run', '--model', 'zai-coding-plan/glm-5.3']}) == 'glm'\n"
        "assert engine_for_close({'argv_head': ['codex', 'exec']}) == 'codex'\n"
        "assert engine_for_close({'refusal': 'PREFLIGHT_FAIL'}) == 'none'\n"
        "print('DRILL_OK')\n"))
    if "DRILL_OK" not in out:
        raise Unmet("DRILL_INCOMPLETE")
    rows = []
    p = repo / "projects" / "factory" / "evidence" / "attempt_accounts.jsonl"
    if p.is_file():
        import json as _j
        for ln in p.read_text(encoding="utf-8").splitlines()[-200:]:
            try:
                r = _j.loads(ln)
            except ValueError:
                continue
            if r.get("phase") == "TERMINAL" and r.get("engine") == "UNKNOWN" and (r.get("ended_at") or "") > "2026-09-17T12:00":
                rows.append(r.get("attempt_key", "")[:30])
    if rows:
        raise Unmet(f"UNKNOWN_ENGINE_ROWS_AFTER_FIX: {rows[:5]}")
    return {"ledger_engine": "ok"}


CHECKS["orphan-by-session"] = check_orphan_by_session
CHECKS["reload-canary"] = check_reload_canary
CHECKS["portfolio-isolation"] = check_portfolio_isolation
CHECKS["intra-project-parallel"] = check_intra_project_parallel
CHECKS["ledger-engine-known"] = check_ledger_engine_known


if __name__ == "__main__":
    sys.exit(main())
