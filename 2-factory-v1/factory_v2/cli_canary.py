"""One bounded real-CLI model launch, observed entirely from the supervisor.

Claude and GLM run under CLI subscriptions: there is no per-call billing, so the
cost of this canary is QUOTA, not dollars. Everything the supervisor cannot
observe stays UNKNOWN — never 0, never estimated.

Bounds, all enforced here rather than trusted to the callee:
  * exactly ONE launch. No retry, no fallback to a second model, no loop.
  * a hard timeout, killed by process group.
  * a temporary project directory; nothing touches a real authority.
  * no credential is read, written, or logged; argv is recorded, environment is not.

The receipt is written whatever happens — timeout, quota, crash — because a
canary that only reports success proves nothing about the failure paths.
"""
from __future__ import annotations

import json
import math
import uuid
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .resource_accounting import UNKNOWN
from ._io import _now

SCHEMA = "cli-canary-receipt/v1"
DEFAULT_TIMEOUT_S = 120

#: The smallest possible task: one word back. Enough to prove a real model ran.
CANARY_WORD = "CANARY_OK"
CANARY_PROMPT = ("Reply with exactly one word and nothing else: CANARY_OK")




def _birth_identity(pid: int) -> str:
    """Process birth time — makes the PID unambiguous across reuse."""
    r = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                       capture_output=True, text=True)
    return r.stdout.strip()


def resolve_cli(engine: str = "claude") -> "str | None":
    return shutil.which({"glm": "opencode", "claude": "claude"}.get(engine, ""))


def _valid_number(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0)


def _response_ok(stdout: str, argv=None) -> bool:
    """Did the model answer with EXACTLY the canary word, whatever shape its CLI emits?

    Asking an engine for JSON changes what its stdout IS, and this check had only
    two shapes: bare prose, and a SINGLE JSON object with a `result` field (the
    Claude shape). opencode's `--format json` emits JSON *Lines* — a stream of
    event envelopes whose reply lives in a `text` part — so `json.loads` failed on
    the whole blob, the bare-prose fallback compared the entire stream to one
    word, and a canary whose model had replied correctly was reported
    INVALID_RESPONSE. Found by the first real canary after the argv changed.

    The extracted text is compared for EQUALITY, never containment: `NOT_CANARY_OK`
    contains the word, and so does "CANARY_OK, but the run failed". A canary that
    accepts those is not checking anything. An explicit error envelope or error
    event is refused even when the word is present, because a CLI that says it
    failed has failed.
    """
    from .telemetry import adapters as _ad

    if _stream_reports_error(stdout):
        return False
    if argv:
        try:
            text = _ad.final_text(_ad.detect_format(argv), stdout)
        except Exception:                                    # noqa: BLE001
            text = ""
        if text:
            return text.strip() == CANARY_WORD
    try:
        payload = json.loads(stdout)
    except (ValueError, TypeError):
        return stdout.strip() == CANARY_WORD
    return (isinstance(payload, dict) and payload.get("is_error") is not True
            and str(payload.get("result", "")).strip() == CANARY_WORD)


def _stream_reports_error(stdout: str) -> bool:
    """True when the CLI itself said the run failed.

    Checked before any text comparison: an error envelope that happens to quote
    the canary word back — an error message, a refusal, an echoed prompt — must
    never read as a healthy launch.
    """
    text = (stdout or "").strip()
    if not text:
        return False
    try:                                    # single-object shape (Claude)
        payload = json.loads(text)
    except (ValueError, TypeError):
        pass
    else:
        return isinstance(payload, dict) and payload.get("is_error") is True
    for line in text.splitlines():          # JSON Lines shape (opencode)
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("is_error") is True or str(ev.get("type", "")).endswith("error"):
            return True
        part = ev.get("part")
        if isinstance(part, dict) and str(part.get("type", "")).endswith("error"):
            return True
    return False


def _production_argv(engine: str, model: str, repo: Path) -> list[str]:
    # Reuse the same argv constructor as the production dispatcher. A canary has
    # its own isolated project and budget; this is not a reviewer authorization.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "factory" / "bin"))
    from model_router import EngineChoice, cli_args
    choice = EngineChoice(engine=engine, model=model, cli=engine, role="builder",
                          spec_level="B", task_kind="build", reason="bounded canary",
                          read_only=False)
    return cli_args(choice, repo, CANARY_PROMPT)


def _extract_usage(stdout: str, argv=None) -> dict:
    """Read usage ONLY if the CLI actually reported it.

    A field the CLI does not expose stays UNKNOWN. Converting an absent number to
    zero is the single most tempting lie in this whole system.

    The parsing itself now lives in factory_v2/telemetry/adapters.py, chosen from
    the argv the canary actually launched — the same adapter, from the same
    registry, that the production dispatcher uses. Two parsers for one format is
    two chances to disagree about what a token count means, and the canary's job
    is to tell the truth about the production path, not about a copy of it.
    """
    usage = {"input_tokens": UNKNOWN, "output_tokens": UNKNOWN,
             "cache_read_tokens": UNKNOWN, "cache_write_tokens": UNKNOWN,
             "cost_usd": UNKNOWN, "usage_source": "NOT_EXPOSED_BY_CLI"}
    from .telemetry import adapters as _ad
    from .telemetry.record import value_of
    parsed = _ad.parse_argv_output(argv or ["claude", "-p", "--output-format", "json"],
                                   stdout or "")
    found = False
    for dst, slot in (("input_tokens", parsed.usage.input_tokens),
                      ("output_tokens", parsed.usage.output_tokens),
                      ("cache_read_tokens", parsed.usage.cache_read_tokens),
                      ("cache_write_tokens", parsed.usage.cache_write_tokens)):
        v = value_of(slot)
        if _valid_number(v):
            usage[dst] = v
            found = True
    est = value_of(parsed.money.api_price_estimate_usd)
    if _valid_number(est):
        usage["api_price_estimate_usd"] = est
        found = True
    if found:
        usage["usage_source"] = "CLI_REPORTED"
        usage["usage_format"] = parsed.usage.source
        usage["usage_completeness"] = parsed.usage.completeness
    return usage


def run_canary(*, engine: str = "claude", model: str = "claude-sonnet-5",
               timeout_s: int = DEFAULT_TIMEOUT_S,
               project_dir: "Path | None" = None) -> dict:
    """Launch exactly once. Return an honest receipt whatever happens."""
    from .launch_gate import (close_account, ledger_path, open_account)

    if engine not in ("claude", "glm") or not 0 < timeout_s <= 300:
        raise ValueError("canary requires claude/glm and a timeout in (0, 300]")
    tmp_owned = project_dir is None
    project_dir = Path(project_dir or tempfile.mkdtemp(prefix="cli_canary_"))
    (project_dir / "evidence").mkdir(parents=True, exist_ok=True)

    attempt_key = f"CANARY:{engine}:{model}:{uuid.uuid4().hex}"
    receipt: dict = {
        "schema": SCHEMA, "started_at": _now(), "engine_requested": engine,
        "model_requested": model, "attempt_key": attempt_key,
        "timeout_s": timeout_s, "launches": 0, "fallback_used": False,
        "credentials_recorded": False,
    }

    cli = resolve_cli(engine)
    if not cli:
        receipt.update({"outcome": "CLI_ABSENT", "ended_at": _now(),
                        "detail": f"{engine} not found on PATH",
                        "engine_observed": "", "model_observed": "",
                        **_extract_usage("")})
        return _finish(receipt, project_dir, tmp_owned)

    argv = _production_argv(engine, model, project_dir)
    argv[0] = cli

    # OPEN row is durable before execve — a kill mid-flight must not erase the
    # attempt or the quota it burned.
    account = open_account(project_dir, attempt_key=attempt_key, project="canary",
                           milestone="CLI_CANARY", mission="CANARY#1", attempt=1,
                           engine=engine, model=model)
    receipt["ledger"] = str(ledger_path(project_dir))
    receipt["open_row_durable_before_exec"] = True

    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, cwd=str(project_dir), start_new_session=True)
    except OSError as exc:
        close_account(project_dir, account, exit_class="PROVIDER_FAILURE",
                      mission_pass=False, duration_s=time.monotonic() - t0)
        receipt.update(outcome="SPAWN_FAILED", exit_class="PROVIDER_FAILURE",
                       ended_at=_now(), detail=type(exc).__name__,
                       terminal_row_durable_after_exec=True, **_extract_usage(""))
        return _finish(receipt, project_dir, tmp_owned)
    receipt["launches"] = 1
    receipt["observed_pid"] = proc.pid
    receipt["observed_birth_identity"] = _birth_identity(proc.pid)
    # argv is evidence; the environment is not recorded, so no credential can leak.
    receipt["observed_argv"] = argv[:-1] + ["<prompt>"]
    receipt["observed_argv0"] = cli

    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        out, err = proc.communicate()
    duration = time.monotonic() - t0
    rc = proc.returncode

    usage = _extract_usage(out or "", argv)
    if timed_out:
        outcome, exit_class = "TIMEOUT", "TIMEOUT"
    elif rc == 0 and _response_ok(out or "", argv):
        outcome, exit_class = "SUCCESS", "SUCCESS"
    elif rc == 0:
        outcome, exit_class = "INVALID_RESPONSE", "PROVIDER_FAILURE"
    elif "quota" in (err or "").lower() or "rate" in (err or "").lower():
        outcome, exit_class = "QUOTA", "QUOTA"
    else:
        outcome, exit_class = "PROVIDER_FAILURE", "PROVIDER_FAILURE"

    # The canary is a real launch on real quota, so it produces a real telemetry
    # row. Before this it parsed usage into its own receipt and stopped there:
    # the coverage inventory counted the path as instrumented while the ledger
    # never saw it. Parsing is not persisting.
    from .telemetry import observe as _tel_observe
    observation = _tel_observe.observe_launch(
        argv=argv, stdout=out or "", rc=rc, wall_time_s=duration,
        started_at=receipt["started_at"], engine=engine,
        model_requested=model, role="canary",
        lineage=_tel_observe.PRIMARY, project="canary",
        milestone="CLI_CANARY", mission="CANARY#1", attempt=1,
        attempt_key=attempt_key, task_type="canary", exit_class=exit_class)
    engine_execution = _tel_observe.to_engine_execution(observation, base={
        "engine_id": Path(cli).name, "model": model, "pid": proc.pid,
        "argv": receipt["observed_argv"], "rc": rc,
        "launched": True, "observed": True,
    })

    class _CanaryResult:
        telemetry_observations = [observation.as_dict()]

    close_account(project_dir, account, exit_class=exit_class,
                  mission_pass=(outcome == "SUCCESS"), duration_s=duration,
                  engine_execution=engine_execution, result=_CanaryResult())
    receipt["telemetry_observation_key"] = observation.observation_key

    receipt.update({
        "ended_at": _now(), "duration_s": round(duration, 3), "rc": rc,
        "outcome": outcome, "exit_class": exit_class,
        "engine_observed": Path(cli).name, "model_observed": model,
        "stdout_head": (out or "")[:600],
        "stderr_head": (err or "")[:600],
        "terminal_row_durable_after_exec": True,
        **usage,
    })
    return _finish(receipt, project_dir, tmp_owned)


def _finish(receipt: dict, project_dir: Path, tmp_owned: bool) -> dict:
    receipt["project_dir"] = str(project_dir)
    out = project_dir / "evidence" / "cli_canary_receipt.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        json.dump(receipt, fh, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    receipt["receipt_path"] = str(out)
    return receipt


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="one bounded real-CLI model launch")
    ap.add_argument("--engine", default="claude")
    ap.add_argument("--model", default="claude-sonnet-5")
    ap.add_argument("--timeout-s", type=int, default=DEFAULT_TIMEOUT_S)
    ap.add_argument("--out", default="")
    a = ap.parse_args(argv)
    rec = run_canary(engine=a.engine, model=a.model, timeout_s=a.timeout_s)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(rec, indent=2, sort_keys=True) + "\n",
                               encoding="utf-8")
    print(json.dumps(rec, indent=2, sort_keys=True))
    return 0 if rec.get("outcome") == "SUCCESS" else 1


if __name__ == "__main__":
    sys.exit(main())
