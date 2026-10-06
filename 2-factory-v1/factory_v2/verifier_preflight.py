"""Cheap VERIFIER_NOT_IMPLEMENTED preflight — before any model is dispatched.

A milestone whose verifier deliberately exits 2 (VERIFIER_NOT_IMPLEMENTED,
`evidence_grading.NOT_IMPLEMENTED`) can never be settled by any amount of
building: no independent observation producer exists yet, so no receipt can
ever be accepted. Dispatching a multi-turn Claude/GLM session against such a
milestone anyway — as happened for FULL_CODEBASE_STATE_OF_ART_AUDIT, three
times, until COST_NO_PROGRESS tripped — burns real quota to learn a fact this
one, near-free subprocess call already knows.

This preflight is a single invocation of the verifier script itself (not a
build, not a model call): the evidence_grading contract guarantees running a
verifier costs no quota by construction.

`check_engine_capacity` (CTO_LENS_RELIABILITY lever 3) is the same posture
applied to a DIFFERENT failure: three Claude launches (1252s, 11.9M tokens
combined) spent real quota to discover that a contract's multi-AI review
requirement named a reviewer engine with no verified launch in this
environment (`ReadOnlyLaunchNotImplemented`) — a fact `model_router` and the
local filesystem already know, for free, before any launch.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .evidence_grading import NOT_IMPLEMENTED

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "factory" / "bin"))
import model_router as _mr

#: Preflight must stay cheap. A verifier that cannot answer within this window
#: is treated as UNOBSERVABLE, not as an excuse to wait longer before refusing.
DEFAULT_TIMEOUT_S = 20

#: check_no_tests_selected must stay a collection-only probe, not a build:
#: keep its own (shorter) timeout so a slow collect phase cannot masquerade
#: as "we ran real tests".
COLLECT_TIMEOUT_S = 10

#: pytest's own signals for "the -k/-m filter matched nothing": exit code 5
#: (ExitCode.NO_TESTS_COLLECTED) and/or one of these substrings on stdout.
_NO_TESTS_MARKERS = ("no tests collected", "no tests ran", "collected 0 items")


def check_verifier_not_implemented(verifier_script: "str | Path",
                                   args: "list[str] | None" = None,
                                   timeout: int = DEFAULT_TIMEOUT_S) -> dict:
    """Run `verifier_script` once and report whether it is VERIFIER_NOT_IMPLEMENTED.

    Never raises: an unobservable verifier (missing, times out, crashes) is
    reported as such rather than treated as either PASS or NOT_IMPLEMENTED —
    a dispatcher must fail closed on a genuinely unknown answer, not guess.
    """
    verifier_path = Path(verifier_script)
    if not verifier_path.is_file():
        return {"observed": False, "rc": None, "not_implemented": False,
                "reason": "PREFLIGHT_SCRIPT_MISSING"}
    argv = [sys.executable, str(verifier_path), *(args or [])]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"observed": False, "rc": None, "not_implemented": False,
               "reason": "PREFLIGHT_TIMEOUT"}
    except OSError as exc:
        return {"observed": False, "rc": None, "not_implemented": False,
               "reason": f"PREFLIGHT_UNOBSERVABLE: {exc}"}
    stderr = r.stderr or ""
    return {"observed": True, "rc": r.returncode,
            "not_implemented": (r.returncode == NOT_IMPLEMENTED
                                and "VERIFIER_NOT_IMPLEMENTED" in stderr),
            "stderr_tail": stderr[-2000:]}


def check_test_command_not_implemented(test_command: str, cwd: "str | Path | None" = None,
                                       timeout: int = DEFAULT_TIMEOUT_S) -> dict:
    """Run the frozen `product_e2e.test_command` once and report VERIFIER_NOT_IMPLEMENTED.

    `test_command` is a shell string (as declared in the frozen validation
    contract), not a Python script — run it the same way the settlement-time
    validator does. Never raises: see `check_verifier_not_implemented`.

    rc==2 alone is not proof: an ordinary command (missing file, bad arg) can
    exit 2 for reasons that have nothing to do with `evidence_grading.
    NOT_IMPLEMENTED`. Classify NOT_IMPLEMENTED only when the process both
    exits 2 AND its stderr carries the `VERIFIER_NOT_IMPLEMENTED` marker that
    `evidence_grading.not_implemented()` writes.
    """
    try:
        r = subprocess.run(test_command, shell=True, cwd=str(cwd) if cwd else None,
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"observed": False, "rc": None, "not_implemented": False,
               "reason": "PREFLIGHT_TIMEOUT"}
    except OSError as exc:
        return {"observed": False, "rc": None, "not_implemented": False,
               "reason": f"PREFLIGHT_UNOBSERVABLE: {exc}"}
    stderr = r.stderr or ""
    return {"observed": True, "rc": r.returncode,
            "not_implemented": (r.returncode == NOT_IMPLEMENTED
                                and "VERIFIER_NOT_IMPLEMENTED" in stderr),
            "stderr_tail": stderr[-2000:]}


def _with_collect_only(test_command: str) -> str:
    """Insert `--collect-only` right after EVERY pytest token, ahead of any
    `-k`/`-m` filters already present, so those filters still apply to the
    collection pass instead of being shadowed by a later flag.

    L5_factory_v2_rest_2-018: a compound command (`pytest a && pytest b`) runs
    the second invocation FOR REAL when only the first token is neutered —
    the promise «never executes a single test body» must hold for every
    invocation in the command string.
    """
    def _after(match: re.Match) -> str:
        return f"{match.group(0)} --collect-only -q"

    # insert after each pytest token itself
    out = re.sub(r"(?<![\w./-])(\S*pytest)(?=\s|$)", _after, test_command)
    if out == test_command:  # no pytest token matched — legacy fallback
        m2 = re.match(r"\s*(\S+)(.*)$", test_command, flags=re.DOTALL)
        if not m2:
            return f"{test_command} --collect-only -q"
        head, rest = m2.group(1), m2.group(2)
        return f"{head} --collect-only -q{rest}"
    return out


def check_no_tests_selected(repo: str | Path, test_cmd: str,
                            timeout: int = COLLECT_TIMEOUT_S) -> dict:
    """Run `test_cmd` in collect-only mode and report whether it selects 0 tests.

    A ROADMAP `product_e2e.test_command` that filters on a `-k`/`-m` substring
    matching no test (a typo, a renamed test, a stale marker) makes the
    milestone impassable by construction: pytest exits rc=5 (no tests
    collected), which the settlement path treats as an ordinary FAIL and
    drives the full retry -> alternate -> diagnose -> replan escalation ladder
    for something a <1s, model-free collection pass already knows.

    This never executes a single test body: `--collect-only` is inserted
    ahead of any existing `-k`/`-m` filter (so the same filter is honoured
    during collection), and the subprocess inherits the caller's environment
    with `FACTORY_NO_REAL_ENGINE=1` forced on, so a `test_cmd` that shells out
    to an engine wrapper still cannot launch a real model. Never raises: see
    `check_verifier_not_implemented`.
    """
    collect_cmd = _with_collect_only(test_cmd)
    # A bare leading `pytest` relies on the console script being on PATH; when
    # it is not, the shell exits 127 and the probe below would silently report
    # no_tests_selected=False (fail-open) for a command that never ran. Fall
    # back to the interpreter running this very process — whenever this check
    # is invoked from a pytest run, `pytest` is importable there by
    # construction. Only the LEADING token is rewritten: a `pytest` appearing
    # after `python -m` is an argument, not a PATH lookup.
    if re.match(r"pytest(?=\s|$)", collect_cmd) and shutil.which("pytest") is None:
        collect_cmd = f'"{sys.executable}" -m pytest{collect_cmd[len("pytest"):]}'
    env = dict(os.environ)
    env["FACTORY_NO_REAL_ENGINE"] = "1"
    try:
        r = subprocess.run(collect_cmd, shell=True, cwd=str(repo) if repo else None,
                           capture_output=True, text=True, timeout=timeout, env=env,
                           check=False)
    except subprocess.TimeoutExpired:
        return {"observed": False, "rc": None, "not_implemented": False,
               "no_tests_selected": False, "reason": "PREFLIGHT_TIMEOUT"}
    except OSError as exc:
        return {"observed": False, "rc": None, "not_implemented": False,
               "no_tests_selected": False,
               "reason": f"PREFLIGHT_UNOBSERVABLE: {exc}"}
    stdout = r.stdout or ""
    no_tests_selected = (r.returncode == 5
                         or any(marker in stdout for marker in _NO_TESTS_MARKERS))
    return {"observed": True, "rc": r.returncode, "not_implemented": False,
            "no_tests_selected": no_tests_selected,
            "detail": stdout[-2000:]}


# ---------------------------------------------------------------------------
# check_engine_capacity — CTO_LENS_RELIABILITY lever 3 (engine capacity)
# ---------------------------------------------------------------------------
#
# Unlike every check above, this one never spawns a subprocess at all — not
# even a cheap local one. model_router.cli_args() already has a verified way
# to answer "would this launch actually work" (claude_restricted_flag_report /
# opencode_restricted_flag_report), but both shell out to `<cli> --help`,
# which is the right cost to pay once, right before a real launch — not the
# right cost to pay here, on every poll, for every role a contract might
# route. This preflight instead reproduces the same three facts using only
# in-process, filesystem-only primitives:
#
#   1. binary on PATH   -> shutil.which(choice.cli)          (model_router.
#      cli_args names the binary per engine: "opencode" for glm, "claude" for
#      claude, "codex" for codex — EngineChoice.cli already carries it)
#   2. required config  -> the GLM/opencode reviewer's committed
#      .opencode/agent/<name>.md (GLM_REVIEW_AGENT / _AGENT_RELPATH),
#      resolved against the contract's product_repo then control_root exactly
#      like model_router._CONTROL_ROOT's fallback (lot 15) — file presence
#      only, not the full permission-denylist parse cli_args performs later
#   3. actor policy     -> model_router.engine_capable(engine, spec_level,
#      task_kind), unchanged from model_router itself
#
# A read-only Codex reviewer is unconditionally reported missing: model_router
# .cli_args() raises ReadOnlyLaunchNotImplemented for EVERY read-only codex
# route (no verified sandbox/network isolation exists), and
# review_dispatch.dispatch_review() separately refuses codex-as-reviewer
# outright (CODEX_REVIEW_FORBIDDEN) — codex can never serve as the multi-AI
# reviewer today, launch or no launch, so this refuses it the same way
# model_router itself would, without needing to call it.

CONTRACT_SPEC_LEVELS = ("S", "A", "B")


def _capacity_gap(choice, repo_path: Path | None,
                  control_root: Path | None) -> str:
    """Empty string when `choice` can be dispatched; otherwise a typed reason.

    Never raises: an unrecognized engine on the EngineChoice (should not
    happen — route() only ever returns canonical engines) is reported as a
    gap rather than left to blow up the caller.
    """
    try:
        engine = _mr.normalize_engine_id(choice.engine) if choice.engine else ""
    except ValueError:
        return f"ENGINE_UNKNOWN: {choice.engine!r}"

    if choice.read_only and engine == "codex":
        # LOT-21: the read-only Codex route exists (`codex exec --json -s read-only
        # --ephemeral ...`). It is only usable when the local CLI documents every
        # flag the restricted launch relies on — same probe model_router uses,
        # `codex exec --help`, no quota spent. A non-empty report = refuse, typed.
        probe = getattr(_mr, "codex_restricted_flag_report", None)
        if probe is None:
            return ("READ_ONLY_LAUNCH_NOT_IMPLEMENTED: model_router exposes no "
                    "codex_restricted_flag_report() — restricted codex launch unverifiable")
        try:
            missing = probe()
        except Exception as exc:  # fail-closed, never raise out of a preflight
            missing = f"probe failed: {exc}"
        if missing:
            return f"READ_ONLY_LAUNCH_NOT_IMPLEMENTED: codex restricted launch unverified ({missing})"

    if not choice.read_only and engine == "codex":
        # CODEX-LOT-C2 (owner opt-in FACTORY_ALLOW_CODEX_BUILDER_B, 2026-09-12):
        # model_router.route() only ever returns a writable Codex EngineChoice
        # for B-level build/repair when that flag is already set, but this
        # preflight is a second, independent gate — same fail-closed contract
        # as the read-only branch above, checked against the `-s
        # workspace-write` launch instead of `-s read-only`. An unverified or
        # not-opted-in writable launch is reported as a capacity gap, never
        # assumed available.
        if not os.environ.get(_mr.ALLOW_CODEX_BUILDER_ENV):
            return ("CODEX_BUILDER_B_NOT_ENABLED: FACTORY_ALLOW_CODEX_BUILDER_B "
                    "is not set — writable codex builder route unavailable")
        probe = getattr(_mr, "codex_writable_flag_report", None)
        if probe is None:
            return ("WRITABLE_LAUNCH_NOT_IMPLEMENTED: model_router exposes no "
                    "codex_writable_flag_report() — writable codex launch unverifiable")
        try:
            missing = probe()
        except Exception as exc:  # fail-closed, never raise out of a preflight
            missing = f"probe failed: {exc}"
        if missing:
            return f"WRITABLE_LAUNCH_NOT_IMPLEMENTED: codex writable launch unverified ({missing})"

    cli = getattr(choice, "cli", "") or ""
    if not cli:
        # Some route() paths (owner-policy overrides, phase-3 windows) build an
        # EngineChoice without `cli`; the canonical engine table in model_router
        # is the source of truth for engine -> binary (glm -> opencode, ...).
        try:
            cli = (getattr(_mr, "_ENGINE_META", {}).get(engine) or {}).get("cli", "") or ""
        except Exception:
            cli = ""
    if not cli:
        return f"NO_CLI_FOR_ENGINE: {engine!r}"
    if shutil.which(cli) is None:
        return f"CLI_BINARY_NOT_ON_PATH: {cli!r}"

    if choice.read_only and engine == "glm":
        return _opencode_agent_config_gap(repo_path, control_root)

    return ""


def _opencode_agent_config_gap(repo_path: Path | None,
                               control_root: Path | None) -> str:
    """Empty string when the committed GLM/opencode reviewer agent config can
    be resolved from `repo_path` or, failing that, `control_root` — the same
    two-location resolution model_router.opencode_restricted_flag_report /
    _CONTROL_ROOT apply at launch time (lot 15). Presence-only: the frontmatter
    permission-denylist parse stays cli_args's job at actual launch time.
    """
    candidates = []
    if repo_path is not None:
        candidates.append(Path(repo_path).joinpath(*_mr._AGENT_RELPATH))
    root = control_root if control_root is not None else _mr._CONTROL_ROOT
    if root is not None:
        candidates.append(Path(root).joinpath(*_mr._AGENT_RELPATH))
    if any(p.is_file() for p in candidates):
        return ""
    where = ", ".join(str(p) for p in candidates) or "<no product_repo/control_root to resolve against>"
    return f"REVIEW_AGENT_CONFIG_MISSING: {where}"


def _resolve_review_choice(spec_level: str, reviewer: dict):
    """Route one declared reviewer, honouring an explicit `engine_hint`.

    Returns (EngineChoice | None, gap). A reviewer that names an engine
    explicitly is checked against ActorCapabilityPolicy DIRECTLY — not via
    route()'s silent hint-rejection fallback (route() would otherwise swap in
    a different engine than the contract named and report success for THAT
    substitute engine, hiding the exact gap this preflight exists to catch).
    """
    # reviewers may be declared as plain engine names (multi_ai_review.reviewers:
    # [codex, claude, glm], 2026-09-17) or as {"engine_hint": ...} dicts
    if isinstance(reviewer, str):
        reviewer = {"engine_hint": reviewer}
    engine_hint = (reviewer or {}).get("engine_hint")
    if not engine_hint:
        return _mr.route(spec_level, _mr.TaskKind.REVIEW), ""
    try:
        canonical = _mr.normalize_engine_id(engine_hint)
    except ValueError:
        return None, f"ENGINE_UNKNOWN: {engine_hint!r}"
    if not _mr.engine_capable(canonical, spec_level, _mr.TaskKind.REVIEW):
        return None, (f"ENGINE_NOT_CAPABLE: {canonical!r} not authorized by "
                      f"ActorCapabilityPolicy for spec_level={spec_level!r} "
                      "task_kind='review'")
    choice = _mr.route(spec_level, _mr.TaskKind.REVIEW, engine_hint=engine_hint)
    if _mr.normalize_engine_id(choice.engine) != canonical:
        # ActorCapabilityPolicy passed above, yet route() still swapped the
        # engine (e.g. an owner-scoped policy window) — the contract named a
        # SPECIFIC engine, so its unavailability is the gap, not whatever
        # engine route() substituted for it.
        return None, f"ENGINE_HINT_REJECTED_BY_ROUTE: {choice.reason}"
    return choice, ""


def check_engine_capacity(contract_body: dict, *,
                          control_root: Path | None = None) -> dict:
    """Would every engine this contract requires actually launch? No launch.

    Reads `contract_body["spec_level"]` (build/repair spec level, default
    "B") and `contract_body["multi_ai_review"]` (the same shape
    runtime.py's settlement path reads: `{"required": bool, "spec_level":
    str, "reviewers": [{"engine_hint": str}, ...]}` — `spec_level` defaults to
    the contract's own, `reviewers` defaults to one default-routed reviewer,
    mirroring the single `review_dispatch.dispatch_review` call runtime.py
    issues today; a contract may name more than one explicit reviewer engine
    for a future multi-reviewer requirement).

    For each role model_router would actually route (build, repair, and — only
    when `multi_ai_review.required` — every declared reviewer), verifies the
    routed EngineChoice can be dispatched: CLI binary on PATH, required
    per-repo config present, ActorCapabilityPolicy authorization. Never
    raises and never launches a subprocess — see module docstring for why
    this deliberately does NOT call model_router.cli_args()/claude_restricted
    _flag_report()/opencode_restricted_flag_report(), which do.

    Returns {"observed": True, "capable": bool, "missing": [{"role", "engine",
    "reason"}, ...], "detail": str}. Fail-closed: any unexpected internal
    error is reported as capable=False, never propagated.
    """
    try:
        contract_body = contract_body or {}
        spec_level = str(contract_body.get("spec_level") or "B")
        if spec_level not in CONTRACT_SPEC_LEVELS:
            spec_level = "B"
        product_repo = contract_body.get("product_repo") or ""
        repo_path = Path(product_repo) if product_repo else None

        missing: list = []

        for role, task_kind in (("build", _mr.TaskKind.BUILD),
                                ("repair", _mr.TaskKind.REPAIR)):
            try:
                choice = _mr.route(spec_level, task_kind)
            except Exception as exc:  # noqa: BLE001
                missing.append({"role": role, "engine": "",
                                "reason": f"ROUTE_ERROR: {exc}"})
                continue
            gap = _capacity_gap(choice, repo_path, control_root)
            if gap:
                missing.append({"role": role, "engine": choice.engine, "reason": gap})

        mai = dict(contract_body.get("multi_ai_review") or {})
        if mai.get("required"):
            review_spec_level = str(mai.get("spec_level") or spec_level)
            if review_spec_level not in CONTRACT_SPEC_LEVELS:
                review_spec_level = spec_level
            reviewers = mai.get("reviewers") or [{}]
            multi = len(reviewers) > 1
            for idx, reviewer in enumerate(reviewers):
                role = f"review[{idx}]" if multi else "review"
                try:
                    choice, gap = _resolve_review_choice(review_spec_level, reviewer or {})
                except Exception as exc:  # noqa: BLE001
                    missing.append({"role": role, "engine": "",
                                    "reason": f"ROUTE_ERROR: {exc}"})
                    continue
                if gap:
                    engine = ((reviewer or {}).get("engine_hint")
                             or (choice.engine if choice else ""))
                    missing.append({"role": role, "engine": engine, "reason": gap})
                    continue
                gap = _capacity_gap(choice, repo_path, control_root)
                if gap:
                    missing.append({"role": role, "engine": choice.engine, "reason": gap})

        capable = not missing
        detail = ("engine capacity satisfied for every routed role" if capable
                  else f"{len(missing)} routed role(s) cannot be dispatched: " +
                       "; ".join(f"{m['role']}={m['engine'] or '?'}:{m['reason']}"
                                for m in missing))
        return {"observed": True, "capable": capable, "missing": missing,
                "detail": detail}
    except Exception as exc:  # noqa: BLE001
        return {"observed": True, "capable": False,
                "missing": [{"role": "internal", "engine": "",
                            "reason": f"ENGINE_CAPACITY_INTERNAL_ERROR: {exc}"}],
                "detail": f"check_engine_capacity raised internally: {exc}"}
