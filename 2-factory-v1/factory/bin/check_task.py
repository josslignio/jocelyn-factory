#!/usr/bin/env python3
"""check_task — F-NEXT-01 task preflight gate.

Composites every mandatory check required before a builder is allowed to start:

   1. Repo identity     (preflight_repo_identity.py — Law D1)
   2. Task file         (exists, non-empty, ≥ 1 canonical objective/outcome
                         section heading with content — see OUTCOME_HEADINGS)
   3. Mission-ID format (legacy simple, or V2 "<base>#M<positive-int>"
                         as produced by factory_v2 mission_compiler)
  4. Git state         (warn on dirty; fail on unborn HEAD)
  5. Protocol file     (FACTORY_ARM_PROTOCOL.md reachable)
  6. Provider quota    (auth_doctor.sh — Law 23; skippable with --skip-providers)

All checks are run even if an earlier one fails (full report before blocking).
rc=0 only if ALL required checks pass.

Usage:
    python3 factory/bin/check_task.py --repo <path> --task-file TASK.md --mission-id M42
    python3 factory/bin/check_task.py --repo <path> --task-file TASK.md --mission-id M42 --json
    python3 factory/bin/check_task.py --repo <path> --task-file TASK.md --mission-id M42 \\
        --skip-providers
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import preflight_repo_identity as _pri  # noqa: E402

RC_OK = 0
RC_FAIL = 1          # one or more checks failed
RC_USAGE = 2         # bad CLI arguments

# Legacy simple mission id: alphanumeric (+ _/-), 1..64 chars.
MISSION_ID_BASE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")
# V2 compiled mission id: "<milestone_id>#M<step_index>" — exactly ONE '#M'
# suffix followed by a strictly positive integer without leading zeros, the
# exact shape factory_v2/mission_compiler.compile_missions() produces via
# enumerate(steps, 1) (e.g. TR_FACTORY_GLM_CANARY#M1, MID#M12). Anything else
# (other '#', traversal, whitespace) fails closed against these regexes.
MISSION_ID_V2_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}#M[1-9][0-9]{0,11}$")
# base(64) + "#M"(2) + up to 12 step digits.
MISSION_ID_MAX_LEN = 64 + 2 + 12

PROTOCOL_SEARCH = [
    Path(__file__).resolve().parents[2] / "campaigns" / "RUN8" / "FACTORY_ARM_PROTOCOL.md",
    Path(__file__).resolve().parents[2] / "factory" / "FACTORY_ARM_PROTOCOL.md",
]

# Canonical objective/outcome section headings — the EXACT shapes the Factory's
# own producers emit. escalation_ladder.generate_{diagnose,replan,safe_repair}_task
# write "## Objective"/"## Acceptance" sections; the wider task fleet uses the
# English/French variants below. Recognition is STRICT:
#   - a REAL level-2 ATX heading: own line, exactly "##" (0-3 leading spaces),
#     the bare canonical name, nothing else — a word buried in prose, a list
#     item or a deeper heading is not a section;
#   - OUTSIDE fenced code blocks — a heading quoted as documentation is not
#     a section (same rule as factory/lib/task_contract_validator.py);
#   - NON-EMPTY body — at least one non-blank line before the next heading or
#     EOF; a heading with nothing under it states no objective.
# The old substring markers ("OUTCOME" anywhere in the text) let prose
# mentions pass preflight while refusing the tasks our generators actually
# produce ("## Objective") — this list is the coherence contract between
# producer and preflight.
OUTCOME_HEADINGS = (
    "Objective", "Outcome", "OUTCOME", "Goal", "Acceptance",   # generated/EN
    "Objectif", "Résultat", "Resultat", "Acceptation",         # FR
)

_HEADING_RES = {
    name: re.compile(r"^\s{0,3}##\s+" + re.escape(name) + r"\s*$")
    for name in OUTCOME_HEADINGS
}
# Fenced code block opener: 0-3 leading spaces then ``` or ~~~ (3+ chars).
_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
# Any ATX heading (level 1-6) — closes the currently open section.
_ANY_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

try:  # imported as factory.bin.check_task (rare) or run as a plain script
    from factory_v2._io import _git as _git_io
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from factory_v2._io import _git as _git_io  # type: ignore[no-redef]


def _git_rc(repo: Path, *args: str) -> tuple[int, str]:
    try:
        p = _git_io(repo, *args, timeout=30)
        return p.returncode, (p.stdout or "").strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 128, f"GIT_ERROR: {e}"


def _check_repo_identity(repo: Path) -> dict:
    res = _pri.check(repo)
    return {
        "name": "repo_identity",
        "ok": res["ok"],
        "verdict": res["verdict"],
        "detail": res["message"],
    }


def _find_outcome_heading(content: str) -> str | None:
    """Return the first canonical objective/outcome heading carrying non-empty
    section content, or None.

    Walks the file line by line, skipping fenced code blocks. A canonical
    heading opens a section; the section counts only if a non-blank line is
    reached before any other heading (or EOF) closes it. An empty canonical
    section is discarded, not counted.
    """
    from factory_v2.shared_parsers import iter_outside_fences
    current: str | None = None
    has_body = False
    # L6_bin_lib_products_1-003: fence tracking (character + length, CommonMark)
    # lives in factory_v2.shared_parsers — the local toggle closed a ````
    # block on a ``` line and counted a quoted heading as the objective.
    for line in iter_outside_fences(content):
        matched = None
        for name, rex in _HEADING_RES.items():
            if rex.match(line):
                matched = name
                break
        if matched is not None:
            if current is not None and has_body:
                return current  # next canonical heading closes a filled section
            current, has_body = matched, False  # (re)open — empty section dropped
            continue
        if current is None:
            continue
        if _ANY_HEADING_RE.match(line):
            if has_body:
                return current  # any other heading closes the section
            current = None  # the open section was empty — discard it
        elif line.strip():
            has_body = True
    if current is not None and has_body:
        return current
    return None


def _check_task_file(task_file: Path) -> dict:
    if not task_file.is_absolute():
        return {
            "name": "task_file", "ok": False, "verdict": "RELATIVE_PATH",
            "detail": (
                f"task_file is a relative path: {task_file}. "
                f"Cross-repo missions require absolute paths. "
                f"Example fix: use the full /Users/.../factory/campaigns/app007/tasks/*.md path."
            )
        }
    if not task_file.exists():
        return {"name": "task_file", "ok": False, "verdict": "MISSING",
                "detail": f"task file not found: {task_file}"}
    try:
        content = task_file.read_text(encoding="utf-8")
    except OSError as e:
        return {"name": "task_file", "ok": False, "verdict": "UNREADABLE",
                "detail": str(e)}
    if not content.strip():
        return {"name": "task_file", "ok": False, "verdict": "EMPTY",
                "detail": "task file is empty"}
    heading = _find_outcome_heading(content)
    if heading is None:
        return {
            "name": "task_file", "ok": False, "verdict": "NO_OUTCOME_MARKER",
            "detail": ("task file has no canonical objective/outcome section. "
                       "Add a level-2 heading — one of: "
                       + ", ".join(f"'## {n}'" for n in OUTCOME_HEADINGS)
                       + " — on its own line, outside fenced code blocks, "
                         "with non-empty section content."),
        }
    return {"name": "task_file", "ok": True, "verdict": "OK",
            "detail": (f"task file valid ({len(content)} chars, "
                       f"outcome section '## {heading}')")}


def resolve_task_file(repo: Path, raw_path: str) -> Path:
    """Resolve task_file path, preferring absolute. 
    
    If relative, tries:
      1. repo / raw_path  (current dispatcher behavior)
      2. FACTORY_ROOT / raw_path  (factory-workspace relative)
    
    Returns the first path that exists, or repo / raw_path as fallback.
    Emits a warning to stderr if the path is relative (absolute is required for cross-repo).
    """
    p = Path(raw_path)
    if p.is_absolute():
        return p
    
    import sys
    print(
        f"WARNING: task_file is relative ('{raw_path}'). "
        f"Cross-repo missions MUST use absolute paths. "
        f"Attempting resolution...",
        file=sys.stderr
    )
    
    FACTORY_ROOT = Path(__file__).resolve().parent.parent.parent
    
    candidate_repo = repo / p
    candidate_factory = FACTORY_ROOT / p
    
    if candidate_repo.exists():
        return candidate_repo
    if candidate_factory.exists():
        return candidate_factory
    return candidate_repo  # will fail downstream with clear MISSING error


def _check_mission_id(mission_id: str) -> dict:
    """Accept ONLY the legacy simple id or a V2 "<base>#M<positive-int>" id.

    The V2 shape is the real output contract of
    factory_v2/mission_compiler.compile_missions (mission ids like
    TR_FACTORY_GLM_CANARY#M1). Other '#', path traversal, whitespace and
    excessive length are all refused (INVALID_FORMAT).
    """
    valid = (
        isinstance(mission_id, str)
        and len(mission_id) <= MISSION_ID_MAX_LEN
        and bool(MISSION_ID_BASE_RE.match(mission_id)
                 or MISSION_ID_V2_RE.match(mission_id))
    )
    if valid:
        return {"name": "mission_id", "ok": True, "verdict": "OK",
                "detail": f"mission_id={mission_id!r}"}
    return {"name": "mission_id", "ok": False, "verdict": "INVALID_FORMAT",
            "detail": (f"mission_id {mission_id!r} does not match the accepted "
                       f"grammar: legacy [A-Za-z0-9][A-Za-z0-9_-]{{0,63}} or V2 "
                       f"'<base>#M<positive-int>' as compiled by mission_compiler "
                       f"(e.g. TR_FACTORY_GLM_CANARY#M1). Other '#', traversal, "
                       f"whitespace and length > {MISSION_ID_MAX_LEN} are refused.")}


def _check_git_state(repo: Path) -> dict:
    rc_head, head = _git_rc(repo, "rev-parse", "HEAD")
    if rc_head != 0:
        return {"name": "git_state", "ok": False, "verdict": "NO_HEAD",
                "detail": "repository has no commits (unborn HEAD)"}
    rc_status, status_out = _git_rc(repo, "status", "--porcelain")
    dirty_lines = [l for l in status_out.splitlines() if l.strip()]
    if dirty_lines:
        return {
            "name": "git_state", "ok": True, "verdict": "DIRTY_WARN",
            "detail": (f"HEAD={head[:7]} — working tree has {len(dirty_lines)} "
                       f"uncommitted change(s) (warning only; builder will commit its own work)"),
        }
    return {"name": "git_state", "ok": True, "verdict": "CLEAN",
            "detail": f"HEAD={head[:7]}, working tree clean"}


def _check_protocol(repo: Path) -> dict:
    candidates = PROTOCOL_SEARCH + [repo / "FACTORY_ARM_PROTOCOL.md"]
    for p in candidates:
        if p.is_file():
            return {"name": "protocol", "ok": True, "verdict": "FOUND",
                    "detail": str(p)}
    return {"name": "protocol", "ok": False, "verdict": "MISSING",
            "detail": (f"FACTORY_ARM_PROTOCOL.md not found. "
                       f"Checked: {', '.join(str(p) for p in candidates)}")}


def _check_providers(repo: Path) -> dict:
    doctor = HERE / "auth_doctor.sh"
    if not doctor.is_file():
        return {"name": "providers", "ok": False, "verdict": "DOCTOR_MISSING",
                "detail": f"auth_doctor.sh not found at {doctor}"}
    try:
        p = subprocess.run(["bash", str(doctor)],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"name": "providers", "ok": False, "verdict": "DOCTOR_ERROR",
                "detail": str(e)}
    ok = p.returncode == 0 and "DOCTOR_PASS" in p.stdout
    verdict = "PASS" if ok else "FAIL"
    tail = (p.stdout + p.stderr)[-800:]
    return {"name": "providers", "ok": ok, "verdict": verdict, "detail": tail}


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def run_checks(
    repo: Path,
    task_file: Path,
    mission_id: str,
    skip_providers: bool = False,
) -> dict:
    t0 = time.monotonic()
    checks = [
        _check_repo_identity(repo),
        _check_task_file(task_file),
        _check_mission_id(mission_id),
        _check_git_state(repo),
        _check_protocol(repo),
    ]
    if not skip_providers:
        checks.append(_check_providers(repo))

    failures = [c for c in checks if not c["ok"]]
    elapsed = round(time.monotonic() - t0, 2)
    ok = len(failures) == 0
    return {
        "schema": "factory/check_task/v1",
        "ok": ok,
        "verdict": "PREFLIGHT_PASS" if ok else "PREFLIGHT_FAIL",
        "mission_id": mission_id,
        "repo": str(repo.resolve()),
        "task_file": str(task_file.resolve()),
        "elapsed_s": elapsed,
        "checks": checks,
        "failures": [c["name"] for c in failures],
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="check_task — F-NEXT-01 task preflight gate")
    ap.add_argument("--repo", required=True, help="repository root to validate")
    ap.add_argument("--task-file", required=True, help="task description file")
    ap.add_argument("--mission-id", required=True,
                    help="mission identifier (legacy alphanumeric + _/-, or V2 '<base>#M<positive-int>')")
    ap.add_argument("--json", action="store_true", help="machine-readable JSON output")
    ap.add_argument("--skip-providers", action="store_true",
                    help="skip auth_doctor.sh provider check (use in offline tests)")
    args = ap.parse_args(argv)

    repo = Path(args.repo)
    task_file = Path(args.task_file)
    if not task_file.is_absolute():
        task_file = repo / task_file

    result = run_checks(repo, task_file, args.mission_id, args.skip_providers)

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return RC_OK if result["ok"] else RC_FAIL

    # human output
    mark = "PASS" if result["ok"] else "FAIL"
    print(f"[{mark}] {result['verdict']}  (mission={result['mission_id']})")
    for c in result["checks"]:
        tick = "  OK " if c["ok"] else " FAIL"
        warn = " [WARN]" if c["verdict"] == "DIRTY_WARN" else ""
        print(f"  {tick}  {c['name']:<20} {c['verdict']}{warn}")
        if not c["ok"] or c["verdict"] == "DIRTY_WARN":
            for line in c["detail"].splitlines()[:5]:
                print(f"         {line}")

    if not result["ok"]:
        print(f"\nBLOCKED — {len(result['failures'])} check(s) failed: "
              f"{', '.join(result['failures'])}")
        return RC_FAIL
    return RC_OK


if __name__ == "__main__":
    raise SystemExit(main())
