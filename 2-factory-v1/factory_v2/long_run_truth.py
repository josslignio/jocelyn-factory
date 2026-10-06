"""Recompute long-run autonomy claims from supervisor-owned evidence.

CTO DIRECTIVE 033 §4.  `verify_long_run.py` accepted its three headline autonomy
claims straight out of the receipt under verification:

    restart_events[*].recovered_from_canonical_brain_only
    routine_human_interventions
    product_e2e_proven

Our own product_verifiers.py:9 states the doctrine those violate: "A check that
reads a claim out of the claimant's artifact is a shape check."  Arbitrary non-equal
PIDs plus three authored booleans satisfied the old tail, so the verifier proved a
settlement sequence, not an unattended run.

Each function here answers the same question a different way: what does evidence the
claimant did not write say?  Every one fails CLOSED — absence of evidence is never
evidence of autonomy.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


class Unproven(Exception):
    """Typed refusal: the claim cannot be established from independent evidence."""


def _read_jsonl(path: Path) -> list:
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise Unproven(f"supervisor journal unreadable: {path}: {exc}") from exc
    out = []
    for lineno, line in enumerate(lines, 1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            # A durable journal with a torn/corrupt row has unknown coverage.
            raise Unproven(
                f"supervisor journal corrupt: {path}:{lineno}: {exc}") from exc
        if not isinstance(row, dict):
            raise Unproven(
                f"supervisor journal corrupt: {path}:{lineno}: row is not an object")
        out.append(row)
    return out


def recompute_restart_recovery(control_root: Path, run_id: str,
                               restart: dict) -> tuple[bool, str]:
    """A restart is 'recovered from the canonical brain alone' only if the supervisor
    ledger shows, for this run: the old pid died, a NEW pid with a DIFFERENT birth
    identity took over, and forward progress was recorded AFTER that boundary.

    A boolean in the receipt establishes none of that.  PID inequality alone does not
    either: two unrelated numbers are not a restart.
    """
    ledger = _read_jsonl(control_root / ".context" / "external-watchdog" / "EVENTS.jsonl")
    if not ledger:
        raise Unproven(
            "no supervisor event ledger — restart recovery cannot be recomputed, and "
            "an authored flag may not stand in for it")

    rid = str(restart.get("id") or "")
    before = restart.get("observed_pid_before")
    after = restart.get("observed_pid_after")
    if not before or not after or before == after:
        return False, f"restart {rid}: no distinct observed pids"

    scoped = [e for e in ledger if str(e.get("run_id", "")) == run_id]
    # L5_rest_1-020: no event for THIS run means the recovery has no evidence.
    # The old `or ledger` fallback let a run with zero proof be declared
    # recovered from the deaths/spawns/progress of a completely foreign run.
    if not scoped:
        return False, (f"restart {rid}: supervisor ledger has no event for "
                       f"run_id={run_id!r} — recovery cannot be established "
                       f"from another run's evidence")
    death_indexes = [i for i, e in enumerate(scoped)
                     if str(e.get("event", "")).endswith(("_DEATH", "_DIED", "_FENCED"))
                     and str(e.get("pid")) == str(before)]
    birth_indexes = [i for i, e in enumerate(scoped)
                     if str(e.get("pid")) == str(after) and e.get("birth")]
    progress_indexes = [i for i, e in enumerate(scoped)
                        if str(e.get("event", "")) in {
                            "MILESTONE_SETTLED", "MISSION_PASS", "FORWARD_PROGRESS", "V2_ADVANCE"}
                        and str(e.get("pid")) == str(after)]
    for death_i in death_indexes:
        old_birth = scoped[death_i].get("birth")
        for birth_i in birth_indexes:
            new_birth = scoped[birth_i].get("birth")
            if birth_i <= death_i or (old_birth and old_birth == new_birth):
                continue
            if any(progress_i > birth_i for progress_i in progress_indexes):
                return True, (f"restart {rid}: ordered death, distinct rebirth and "
                              "post-restart progress observed")

    missing = []
    if not death_indexes:
        missing.append("old-pid death")
    if not birth_indexes:
        missing.append("new-pid birth identity")
    if not progress_indexes:
        missing.append("post-restart forward progress")
    reason = (", no ".join(missing) if missing else
              "ordered death -> distinct birth -> progress chain")
    return False, (f"restart {rid}: supervisor ledger shows no {reason} "
                   f"— recovery is claimed, not observed")


def recompute_human_interventions(control_root: Path, run_id: str,
                                  window: tuple[str, str]) -> tuple[int, str]:
    """Count owner/control inputs from supervisor-owned events inside the run window.

    The absence of a claimant-authored list is not proof of zero.  If no ledger covers
    the window we refuse rather than return 0 — that refusal is the whole point.
    """
    start, end = window
    ledger = _read_jsonl(control_root / ".context" / "external-watchdog" / "EVENTS.jsonl")
    ledger += _read_jsonl(control_root / ".context" / "glm-primary-overnight"
                          / "control" / "EVENTS.jsonl")
    if not ledger:
        raise Unproven(
            "no supervisor event ledger covers this run window — human interventions "
            "cannot be recomputed, and an authored 0 is not evidence of 0")

    HUMAN = {"OWNER_INPUT", "HUMAN_GO", "OWNER_EXCEPTION", "MANUAL_TRIGGER",
             "MANUAL_AGENT_HANDOFF", "OWNER_QUESTION", "MANUAL_RESTART"}
    covered = []
    for e in ledger:
        ts = str(e.get("ts") or e.get("at") or "")
        if not ts or (start and ts < start):
            continue
        if end and ts > end:
            continue
        event_run = str(e.get("run_id") or "")
        if event_run and event_run != run_id:
            continue
        covered.append(e)
    if not covered:
        raise Unproven(
            f"no supervisor event covers run_id={run_id!r} inside [{start} .. {end}] "
            "— an unobserved intervention count is UNKNOWN, not zero")
    n = sum(str(e.get("event", "")).upper() in HUMAN for e in covered)
    return n, f"{n} owner/control event(s) in [{start} .. {end}] from the supervisor ledger"


def recompute_product_e2e(control_root: Path, milestone_id: str,
                          product_repo: Path, control_sha: str,
                          product_sha: str) -> tuple[bool, str]:
    """Re-run the frozen product verifier at the exact dual-pinned SHAs.

    The receipt's product_e2e_proven boolean is a claim; this executes the check.

    L5_factory_v2_rest_1-023: control_sha/product_sha used to decorate the final
    message only — the verifier ran in whatever trees happened to be checked
    out. A pass is now attributable to the pinned trees or it does not exist:
    the HEAD of BOTH repos is confronted to the pin BEFORE and AFTER the run,
    and any divergence is a typed refusal.
    """
    if not control_sha or not product_sha:
        raise Unproven("product E2E needs both control and product SHAs pinned; "
                       "an unpinned pass proves nothing about which tree passed")
    if not Path(product_repo).is_dir():
        raise Unproven(f"product repo absent at {product_repo}")

    def _head(repo: Path, pin: str, what: str) -> None:
        got = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=60)
        if got.returncode != 0:
            raise Unproven(f"{what} repo unreadable at {repo}: {got.stderr.strip()[:120]}")
        actual = got.stdout.strip()
        if actual != pin:
            raise Unproven(
                f"{what} HEAD {actual[:12]} != pinned {pin[:12]} — the run below "
                f"cannot be attributed to the SHA the receipt certifies")
        dirty = subprocess.run(["git", "-C", str(repo), "diff", "--exit-code", "HEAD", "--"],
                               capture_output=True, text=True, timeout=60)
        if dirty.returncode != 0:
            raise Unproven(f"{what} has dirty/uncommitted bytes or an unreadable diff; "
                           "HEAD alone does not bind the executed tree")

    _head(Path(control_root), control_sha, "control")
    _head(Path(product_repo), product_sha, "product")
    proc = subprocess.run(
        [sys.executable, "-m", "factory_v2.product_verifiers", milestone_id,
         "--project-repo", str(product_repo)],
        capture_output=True, text=True, timeout=1800,
        cwd=str(control_root), env={"PYTHONPATH": str(control_root), "PYTHONDONTWRITEBYTECODE": "1",
                                    "PATH": "/usr/bin:/bin:/usr/local/bin"})
    ok = proc.returncode == 0
    # the trees must not have moved while the verifier ran, otherwise even a
    # green rc belongs to some other pair of trees than the pinned one
    _head(Path(control_root), control_sha, "control")
    _head(Path(product_repo), product_sha, "product")
    tail = (proc.stdout + proc.stderr).strip().splitlines()
    return ok, (f"product verifier {milestone_id} rc={proc.returncode} at "
                f"ctrl@{control_sha[:12]} x prod@{product_sha[:12]}"
                + (" — " + " | ".join(line[:160] for line in tail[-4:]) if tail else ""))
