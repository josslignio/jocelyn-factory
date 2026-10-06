#!/usr/bin/env python3
"""FACTORY_CONVERGENCE_RESHAPE gate — CONVERGENCE_RESHAPE_RECEIPT.json.

Replaces the 2026-09-12 VERIFIER_NOT_IMPLEMENTED stub. The stub was correct to
refuse: nothing in the old receipt shape could be recomputed independently of
the claimant. This verifier recomputes against two immutable, independently
observable inputs instead of trusting authored booleans:

  * factory_v2/audit/FACTORY_CONVERGENCE_LEDGER.json — the FROZEN audit decision
    list (produced by a prior, separately-settled milestone). The receipt must
    bind to its exact sha256 so a claimant cannot substitute a rewritten ledger.
  * git history between base_sha (pre-reshape) and integration_sha (post-reshape)
    — every DELETE/REPLACE decision must correspond to a real, inspectable diff
    in that range, not an authored "applied: true".

Clauses covered (see .context/factory-tightening-audit-2026-09-12/VERIFIERS_SPEC.md
for the full acceptance-clause mapping):

  1. schema recognised
  2. ledger binding: receipt.ledger_sha256 == sha256(FACTORY_CONVERGENCE_LEDGER.json)
  3. base_sha / integration_sha are real commits, base_sha is an ancestor of
     integration_sha
  4. every ledger decision with decision in {DELETE, REPLACE} has a matching
     receipt.decisions_applied entry whose commit_sha is a real commit inside
     (base_sha, integration_sha] and whose structural git diff matches the
     decision type (DELETE: file present at base_sha, absent at integration_sha;
     REPLACE: file present at both, content differs). Two ledger realities
     extend this clause, both recomputed from git — never from the receipt:
       a. entries carrying subsystem_file + deleted_in_commit (ledger
          update_2026_09_18.deleted_subsystem_fields: `file` names the LIVE
          SUCCESSOR kept resolvable for audit_observation_producer, the deletion
          itself is documented by subsystem_file/deleted_in_commit) are verified
          on subsystem_file: present at deleted_in_commit^, absent at
          deleted_in_commit, absent at integration_sha, deleted_in_commit
          reachable from integration_sha, receipt commit_sha == deleted_in_commit.
       b. a DELETE whose file still exists at integration_sha is accepted ONLY
          as an owner-frozen-zone block, machine-established here: target inside
          factory/bin/frozen_zone_guard.py's zone, freeze in force (campaign
          active), guard.check() ok, file byte-identical to the guard's baseline
          tag at integration_sha, receipt entry declares UNAPPLIED_BLOCKED. The
          owner freeze (Boss decision 2026-08-07) outranks the audit ledger; if
          the freeze is lifted, this acceptance lapses and the gate goes UNMET
          again until the DELETE is actually applied.
  5. LOC is recomputed independently (git ls-tree + git show, not read from the
     receipt); a rise requires a non-empty receipt.loc_justification (presence
     is checked, not the quality of the argument — see VERIFIERS_SPEC "non
     machine-verifiable" section)
  6. no undeclared new top-level module was added under factory/ or factory_v2/
     between base_sha and integration_sha (every added file must be named by a
     decision's file field or a decision's declared companion_files)
  7. test_log is sha256-bound (tamper-evident) and structurally parsed for a
     pytest summary with zero failed/error tests — the receipt's own
     "tests_passed" boolean is never read

Exit 0: all recomputable clauses pass. Exit 1: UNMET, typed reason on stderr.
Exit 2 is reserved for VERIFIER_NOT_IMPLEMENTED and must not occur here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

CONTROL_ROOT = Path(__file__).resolve().parent.parent
LEDGER_PATH = CONTROL_ROOT / "factory_v2" / "audit" / "FACTORY_CONVERGENCE_LEDGER.json"
STRUCTURAL_DECISIONS = ("DELETE", "REPLACE")
TRACKED_DIRS = ("factory", "factory_v2")


class ObservationUnreadable(RuntimeError):
    """A Git observation required by this verdict could not be produced."""


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


try:
    from ._io import _git as _git_io
except ImportError:  # script form (python3 factory_v2/verify_reshape_receipt.py): no parent package
    sys.path.insert(0, str(CONTROL_ROOT))
    from factory_v2._io import _git as _git_io


def _git_rc(repo: Path, *args: str) -> tuple[int, str]:
    try:
        r = _git_io(repo, *args, timeout=60)
        return r.returncode, r.stdout.strip()
    except Exception as exc:
        return 1, str(exc)


def _commit_exists(repo: Path, sha: str) -> bool:
    if not sha:
        return False
    rc, _ = _git_rc(repo, "cat-file", "-e", f"{sha}^{{commit}}")
    return rc == 0


def _is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    rc, _ = _git_rc(repo, "merge-base", "--is-ancestor", ancestor, descendant)
    return rc == 0


def _file_exists_at(repo: Path, sha: str, path: str) -> bool:
    rc, _ = _git_rc(repo, "cat-file", "-e", f"{sha}:{path}")
    return rc == 0


def _show(repo: Path, sha: str, path: str) -> str | None:
    rc, out = _git_rc(repo, "show", f"{sha}:{path}")
    return out if rc == 0 else None


def _tracked_py_files(repo: Path, sha: str, dirs: tuple) -> set:
    out_files = set()
    for d in dirs:
        rc, out = _git_rc(repo, "ls-tree", "-r", "--name-only", sha, "--", d)
        if rc != 0:
            raise ObservationUnreadable(
                f"git ls-tree failed for {sha[:12]}:{d} (rc={rc}): {out[:200]}")
        for line in out.splitlines():
            if line.endswith(".py"):
                out_files.add(line)
    return out_files


def _loc_total(repo: Path, sha: str, dirs: tuple) -> int | None:
    total = 0
    for path in sorted(_tracked_py_files(repo, sha, dirs)):
        text = _show(repo, sha, path)
        if text is None:
            return None
        total += len(text.splitlines())
    return total


def _added_files(repo: Path, base_sha: str, integration_sha: str, dirs: tuple) -> set:
    added = set()
    for d in dirs:
        rc, out = _git_rc(repo, "diff", "--name-status", "--diff-filter=A",
                       base_sha, integration_sha, "--", d)
        if rc != 0:
            raise ObservationUnreadable(
                f"git diff failed for {d} (rc={rc}): {out[:200]}")
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) == 2 and parts[1].endswith(".py"):
                added.add(parts[1])
    return added


def _load_frozen_zone_guard(repo: Path):
    """Load the owner's own guard module — its constants (FROZEN, BASELINE_TAG,
    FACTORY_ACTIVE_STATES) and check() are the authority; duplicating them here
    would let this gate drift from the Boss decision it defers to."""
    guard_path = repo / "factory" / "bin" / "frozen_zone_guard.py"
    if not guard_path.is_file():
        return None
    import importlib.util
    spec = importlib.util.spec_from_file_location("_reshape_gate_frozen_zone_guard", guard_path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:
        return None
    return module


def _owner_frozen_zone_block(repo: Path, integration_sha: str, path: str,
                             entry: dict) -> tuple[bool, str]:
    """A DELETE left unapplied is legitimate only if the owner's frozen zone
    (a Boss authority that outranks the audit ledger) currently forbids writing
    the target. Every element is recomputed here; nothing is trusted from the
    receipt beyond the entry naming the block."""
    status = str(entry.get("status", ""))
    if not status.startswith("UNAPPLIED_BLOCKED"):
        return False, (f"({entry.get('subsystem')}, {path}): DELETE decision but {path!r} still "
                       f"exists at integration_sha and the receipt entry declares no owner block "
                       f"(status={status!r})")
    guard = _load_frozen_zone_guard(repo)
    if guard is None:
        return False, (f"({entry.get('subsystem')}, {path}): DELETE decision but {path!r} still "
                       "exists at integration_sha and factory/bin/frozen_zone_guard.py cannot be "
                       "loaded — an owner block cannot be machine-established")
    frozen = str(getattr(guard, "FROZEN", ""))
    if not frozen or not (path == frozen or path.startswith(frozen + "/")):
        return False, (f"({entry.get('subsystem')}, {path}): DELETE decision but {path!r} still "
                       f"exists at integration_sha and lives outside the frozen zone {frozen!r}")
    campaign_state = guard._factory_campaign_state()
    if campaign_state not in guard.FACTORY_ACTIVE_STATES:
        return False, (f"({entry.get('subsystem')}, {path}): DELETE decision but {path!r} still "
                       f"exists at integration_sha and the freeze is LIFTED (campaign state "
                       f"{campaign_state!r}) — the DELETE is actionable, not blocked")
    check = guard.check()
    if not check.get("ok"):
        return False, (f"({entry.get('subsystem')}, {path}): DELETE decision but {path!r} still "
                       f"exists at integration_sha and the frozen zone itself reports "
                       f"{check.get('verdict')!r} — nothing may be concluded from it")
    baseline_tag = str(getattr(guard, "BASELINE_TAG", ""))
    rc, tag_commit = _git_rc(repo, "rev-parse", "--verify", f"{baseline_tag}^{{commit}}")
    if rc != 0 or not tag_commit:
        return False, (f"({entry.get('subsystem')}, {path}): baseline tag {baseline_tag!r} does "
                       "not resolve — the frozen-zone block cannot be proven")
    baseline_blob = _show(repo, tag_commit, path)
    integration_blob = _show(repo, integration_sha, path)
    if baseline_blob is None or baseline_blob != integration_blob:
        return False, (f"({entry.get('subsystem')}, {path}): DELETE decision but {path!r} at "
                       f"integration_sha is NOT byte-identical to the {baseline_tag} evidence — "
                       "this is not frozen-zone preservation")
    return True, (f"({entry.get('subsystem')}, {path}): DELETE blocked by owner frozen zone "
                  f"({frozen}, campaign {campaign_state!r}, guard FROZEN_ZONE_INTACT, target "
                  f"byte-identical to {baseline_tag})")


def fail(msg: str) -> int:
    print(f"UNMET: {msg}", file=sys.stderr)
    return 1


def verify(receipt_path: Path, repo: Path | None = None) -> int:
    repo = repo or CONTROL_ROOT
    if not receipt_path.is_file():
        return fail(f"receipt not found: {receipt_path}")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return fail(f"receipt is not valid JSON: {exc}")

    if receipt.get("schema") != "convergence-reshape-receipt/v2":
        return fail(f"schema={receipt.get('schema')!r}, expected "
                    "'convergence-reshape-receipt/v2' (v1 predates ledger binding)")

    if not LEDGER_PATH.is_file():
        return fail(f"canonical ledger not found at {LEDGER_PATH} — nothing to bind to")
    ledger_bytes = LEDGER_PATH.read_bytes()
    actual_ledger_sha = _sha256_bytes(ledger_bytes)
    claimed_ledger_sha = str(receipt.get("ledger_sha256", ""))
    if claimed_ledger_sha != actual_ledger_sha:
        return fail(f"ledger_sha256 mismatch: receipt binds {claimed_ledger_sha[:12] or '<empty>'}, "
                    f"actual FACTORY_CONVERGENCE_LEDGER.json is {actual_ledger_sha[:12]}")
    ledger = json.loads(ledger_bytes.decode("utf-8"))
    decisions = ledger.get("decisions") or []
    structural = [d for d in decisions if d.get("decision") in STRUCTURAL_DECISIONS]
    if not structural:
        return fail("ledger carries no DELETE/REPLACE decisions to verify against — "
                    "the ledger this receipt binds to cannot support this milestone")

    base_sha = str(receipt.get("base_sha", ""))
    integration_sha = str(receipt.get("integration_sha", ""))
    if not _commit_exists(repo, base_sha):
        return fail(f"base_sha {base_sha!r} is not a real commit in {repo.name}")
    if not _commit_exists(repo, integration_sha):
        return fail(f"integration_sha {integration_sha!r} is not a real commit in {repo.name}")
    if base_sha != integration_sha and not _is_ancestor(repo, base_sha, integration_sha):
        return fail(f"base_sha {base_sha[:12]} is not an ancestor of "
                    f"integration_sha {integration_sha[:12]}")

    applied = receipt.get("decisions_applied")
    if not isinstance(applied, list):
        return fail("decisions_applied missing or not a list")
    by_key = {}
    for entry in applied:
        key = (entry.get("subsystem"), entry.get("file"))
        by_key[key] = entry

    blocked_notes: list[str] = []
    for decision in structural:
        key = (decision.get("subsystem"), decision.get("file"))
        entry = by_key.get(key)
        if entry is None:
            return fail(f"ledger decision {decision.get('decision')} on "
                        f"{decision.get('subsystem')}/{decision.get('file')} has no matching "
                        "decisions_applied entry")
        commit_sha = str(entry.get("commit_sha", ""))
        if not _commit_exists(repo, commit_sha):
            return fail(f"{key}: commit_sha {commit_sha!r} is not a real commit")
        # L5_factory_v2_rest_3-006: the application commit must sit strictly
        # AFTER base_sha (and reachable from integration_sha). Reachability
        # alone let a deletion be attributed to base_sha itself — where the
        # file still exists.
        if commit_sha == base_sha or not _is_ancestor(repo, base_sha, commit_sha):
            return fail(f"{key}: application commit_sha {commit_sha[:12]} is not "
                        f"strictly after base_sha {base_sha[:12]} — the decision "
                        "cannot be attributed to a commit where the file still "
                        "exists (application window: base < commit ≤ integration)")
        if commit_sha != base_sha and not _is_ancestor(repo, commit_sha, integration_sha):
            return fail(f"{key}: commit_sha {commit_sha[:12]} is not reachable from "
                         f"integration_sha {integration_sha[:12]}")
        path = decision.get("file")
        if decision.get("decision") == "DELETE":
            subsystem_file = decision.get("subsystem_file")
            deleted_in_commit = decision.get("deleted_in_commit")
            if subsystem_file or deleted_in_commit:
                # ledger update_2026_09_18.deleted_subsystem_fields: `file` names
                # the LIVE SUCCESSOR of the deleted subsystem; the structural
                # deletion proof lives on subsystem_file at deleted_in_commit.
                if not (subsystem_file and deleted_in_commit):
                    return fail(f"{key}: DELETE entry carries subsystem_file/deleted_in_commit "
                                "incompletely (ledger update_2026_09_18 convention)")
                if commit_sha != deleted_in_commit:
                    return fail(f"{key}: receipt commit_sha {commit_sha[:12]} does not match "
                                f"ledger deleted_in_commit {deleted_in_commit[:12]}")
                if not _commit_exists(repo, deleted_in_commit):
                    return fail(f"{key}: deleted_in_commit {deleted_in_commit[:12]} is not a "
                                "real commit")
                if not _is_ancestor(repo, deleted_in_commit, integration_sha):
                    return fail(f"{key}: deleted_in_commit {deleted_in_commit[:12]} is not "
                                f"reachable from integration_sha {integration_sha[:12]}")
                if not _file_exists_at(repo, f"{deleted_in_commit}^", subsystem_file):
                    return fail(f"{key}: DELETE names subsystem_file {subsystem_file!r} but it "
                                f"did not exist at {deleted_in_commit[:12]}^ — no real deletion "
                                "to observe")
                if _file_exists_at(repo, deleted_in_commit, subsystem_file):
                    return fail(f"{key}: subsystem_file {subsystem_file!r} is still present at "
                                f"deleted_in_commit {deleted_in_commit[:12]} — that commit does "
                                "not delete it")
                if _file_exists_at(repo, integration_sha, subsystem_file):
                    return fail(f"{key}: DELETE decision but subsystem_file {subsystem_file!r} "
                                f"still exists at integration_sha {integration_sha[:12]}")
            else:
                existed_before = _file_exists_at(repo, base_sha, path)
                exists_after = _file_exists_at(repo, integration_sha, path)
                if not existed_before:
                    return fail(f"{key}: DELETE decision but {path!r} did not exist at "
                                f"base_sha {base_sha[:12]}")
                if exists_after:
                    ok, note = _owner_frozen_zone_block(repo, integration_sha, path, entry)
                    if not ok:
                        return fail(note)
                    blocked_notes.append(note)
        else:  # REPLACE
            before = _show(repo, base_sha, path)
            after = _show(repo, integration_sha, path)
            if before is None:
                return fail(f"{key}: REPLACE decision but {path!r} did not exist at "
                            f"base_sha {base_sha[:12]}")
            if after is None:
                return fail(f"{key}: REPLACE decision but {path!r} no longer exists at "
                            f"integration_sha {integration_sha[:12]}")
            if before == after:
                return fail(f"{key}: REPLACE decision but {path!r} is byte-identical "
                            f"between base_sha and integration_sha — no replacement observed")

    try:
        loc_before = _loc_total(repo, base_sha, TRACKED_DIRS)
        loc_after = _loc_total(repo, integration_sha, TRACKED_DIRS)
    except ObservationUnreadable as exc:
        return fail(f"Git observation unavailable: {exc}")
    if loc_before is None or loc_after is None:
        return fail("could not recompute LOC from git history for base_sha/integration_sha")
    if loc_after >= loc_before:
        justification = str(receipt.get("loc_justification", "")).strip()
        if len(justification) < 20:
            return fail(f"LOC not reduced (before={loc_before}, recomputed after={loc_after}) "
                        "and loc_justification is missing or too short — presence, not quality, "
                        "is machine-checked here (see VERIFIERS_SPEC)")

    # L5_factory_v2_rest_3-008: only entries that belong to the FROZEN ledger
    # authorize new files — an applied entry whose (subsystem, file) key is in
    # no ledger decision used to declare surprise.py as expected.
    ledger_keys = {(d.get("subsystem"), d.get("file")) for d in decisions}
    declared_new = set()
    for entry in applied:
        key = (entry.get("subsystem"), entry.get("file"))
        if key not in ledger_keys:
            continue  # not a frozen-ledger decision: authorizes nothing
        if entry.get("file"):
            declared_new.add(entry["file"])
        for companion in entry.get("companion_files") or []:
            declared_new.add(companion)
    try:
        added = _added_files(repo, base_sha, integration_sha, TRACKED_DIRS)
    except ObservationUnreadable as exc:
        return fail(f"Git observation unavailable: {exc}")
    undeclared = added - declared_new
    if undeclared:
        return fail(f"undeclared new file(s) added outside any ledger decision — looks like a "
                    f"new subsystem: {sorted(undeclared)}")

    test_log_path = receipt.get("test_log_path", "")
    test_log_sha256 = str(receipt.get("test_log_sha256", ""))
    if not test_log_path:
        return fail("receipt names no test_log_path — the test-suite claim cannot be checked")
    log_file = repo / test_log_path if not Path(test_log_path).is_absolute() else Path(test_log_path)
    if not log_file.is_file():
        return fail(f"test_log_path {test_log_path!r} does not exist")
    log_bytes = log_file.read_bytes()
    actual_log_sha = _sha256_bytes(log_bytes)
    if test_log_sha256 != actual_log_sha:
        return fail(f"test_log_sha256 mismatch: receipt binds {test_log_sha256[:12] or '<empty>'}, "
                    f"actual log is {actual_log_sha[:12]} — the log was edited after the receipt "
                    "was written, or never matched it")
    log_text = log_bytes.decode("utf-8", errors="replace")
    # L5_factory_v2_rest_3-007: EVERY occurrence of the counters is examined —
    # re.search stopped at the first one, so a neutral preamble ('0 failed,
    # 0 errors') masked a failing final summary ('1 failed, 1 error,
    # 12 passed').
    fail_counts = [int(m.group(1)) for m in re.finditer(r"(\d+)\s+failed", log_text)]
    error_counts = [int(m.group(1)) for m in re.finditer(r"(\d+)\s+error", log_text)]
    pass_counts = [int(m.group(1)) for m in re.finditer(r"(\d+)\s+passed", log_text)]
    if any(n > 0 for n in fail_counts):
        return fail(f"test log shows failed test(s): {fail_counts} "
                    f"({max(fail_counts)} at worst)")
    if any(n > 0 for n in error_counts):
        return fail(f"test log shows error(s): {error_counts} "
                    f"({max(error_counts)} at worst)")
    if not pass_counts or max(pass_counts) < 1:
        return fail("test log has no parseable 'N passed' summary — cannot confirm the suite ran")

    blocked_line = (f", {len(blocked_notes)} DELETE blocked by owner frozen zone "
                    "(machine-verified)") if blocked_notes else ""
    print(f"CONVERGENCE_RESHAPE_VERIFIER: PASS — {len(structural)} decisions verified"
          f"{blocked_line}, LOC {loc_before}->{loc_after}, {max(pass_counts)} tests passed")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="verify_reshape_receipt")
    ap.add_argument("receipt_path")
    ap.add_argument("--repo-root", default=None,
                     help="git repo to check commits against (default: this verifier's own repo)")
    args = ap.parse_args(argv)
    repo = Path(args.repo_root) if args.repo_root else None
    return verify(Path(args.receipt_path), repo=repo)


if __name__ == "__main__":
    sys.exit(main())
