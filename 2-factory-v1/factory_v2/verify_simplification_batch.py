#!/usr/bin/env python3
"""Fail-closed verifier for one simplification batch (FACTORY_SIMPLIFY_<batch>).

Judges the control repo AT HEAD against the frozen manifest
`factory_v2/audit/SIMPLIFICATION_BATCHES.json` (read from the HEAD blob, never
from the worktree, so a builder cannot edit the manifest to fit its work):

  1. every listed path is absent both at HEAD and on disk       -> PATH_STILL_PRESENT
  2. no remaining reference to a deleted module anywhere in the
     tracked tree (imports or path strings), tests included     -> REFERENCE_REMAINS
  3. the frozen blobs of the deleted paths total at least 90 % of the
     batch's expected removal (LOC total reported, not gated)   -> REMOVED_LOC_BELOW_EXPECTED
  4. depends_on batch (if any) already carries a receipt         -> DEPENDENCY_UNVERIFIED

rc=0 writes factory_v2/receipts/SIMPLIFICATION_<batch>_RECEIPT.json (atomic).
rc=1 unmet, rc=2 unreadable.  Written by the CTO operator 2026-09-13, independent
of any builder.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

try:  # imported as factory_v2.<module>
    from ._io import _git
except ImportError:  # standalone judge: executed from `git show <sha>:<file>` into /tmp,
    # where no package exists — it must carry its own helper by design.
    def _git(repo, *args, timeout=60):
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout)


MANIFEST_REL = "factory_v2/audit/SIMPLIFICATION_BATCHES.json"
#: git blob sha of the frozen manifest (authority-pinned 2026-09-13). A builder that
#: edits and commits the manifest changes its blob sha and fails MANIFEST_TAMPERED.
MANIFEST_BLOB_SHA = "cd41dc14a538fa6b84ad90f6182aad92e2c9cf3d"
RECEIPT_DIR = Path("factory_v2/receipts")
#: Reference scan scope: the executable tree only (runtime, scripts, tests, project
#: authority). Prose, campaign archives and hidden benchmark variants are not
#: callers; a name like `dashboard` appearing in a markdown file is not a reference.
REF_SCAN_PATHSPECS = ("factory/", "factory_v2/", "scripts/", "tests/", "projects/")
REF_SCAN_EXTENSIONS = (".py", ".sh", ".zsh", ".yaml", ".yml", ".json", ".toml", ".plist", ".cfg", ".ini")
#: Evidence, receipts, audit artefacts and CTO registries record history as text;
#: they cannot execute code and are never rewritten to erase a path they cite.
EXCLUDED_REF_DIRS = ("archive/", ".context/", "factory_v2/audit/", "factory_v2/receipts/", "factory/cto/")
EXCLUDED_REF_SEGMENTS = ("/evidence/", "/state/", "/run/")
#: Runtime state and journals record history; they are not callers.
EXCLUDED_REF_BASENAMES = ("PROJECT_STATE.json", "DURABLE_HANDOFF.json", "SUPERSEDED_QUARANTINE.json")
EXCLUDED_REF_SUFFIXES = (".jsonl",)
#: forbidden_tokens (batch invariants such as a retired CLI flag) are searched in
#: executable sources only: roadmaps and briefs legitimately NAME the thing removed.
TOKEN_SCAN_EXTENSIONS = (".py", ".sh", ".zsh", ".plist")


class Unreadable(Exception):
    pass


class Unmet(Exception):
    pass


def _load_manifest(repo: Path, expected_blob: str) -> dict:
    shown = _git(repo, "show", f"HEAD:{MANIFEST_REL}")
    if shown.returncode:
        raise Unreadable(f"MANIFEST_NOT_AT_HEAD: {MANIFEST_REL}")
    blob = _git(repo, "rev-parse", f"HEAD:{MANIFEST_REL}")
    if blob.returncode or blob.stdout.strip() != expected_blob:
        raise Unreadable(f"MANIFEST_TAMPERED: blob {blob.stdout.strip()[:12]} != pinned {expected_blob[:12]}")
    try:
        doc = json.loads(shown.stdout)
    except json.JSONDecodeError as exc:
        raise Unreadable(f"MANIFEST_CORRUPT: {exc}") from exc
    if doc.get("schema") != "simplification-batches/v1" or not isinstance(doc.get("batches"), dict):
        raise Unreadable("MANIFEST_SCHEMA_INVALID")
    return doc


def _tracked_files(repo: Path) -> list[str]:
    ls = _git(repo, "ls-tree", "-r", "--name-only", "HEAD")
    if ls.returncode:
        raise Unreadable("CANNOT_LIST_HEAD")
    return [ln for ln in ls.stdout.splitlines() if ln.strip()]


def _measure_loc(repo: Path, scope: list[str]) -> int:
    total = 0
    for rel in _tracked_files(repo):
        if not rel.endswith(".py") or not any(rel.startswith(s) for s in scope):
            continue
        blob = _git(repo, "show", f"HEAD:{rel}")
        if blob.returncode:
            raise Unreadable(f"CANNOT_READ_BLOB: {rel}")
        total += blob.stdout.count("\n")
    return total


def _references(repo: Path, deleted: list[str]) -> list[str]:
    hits: list[str] = []
    for rel in deleted:
        mod = Path(rel).stem
        dotted = rel[:-3].replace("/", ".")            # factory.bin.autopilot
        parent = ".".join(dotted.split(".")[:-1])      # factory.bin
        # Covers: `import mod`, `import factory.bin.mod`, `from mod import`,
        # `from factory.bin.mod import`, `from factory.bin import mod`,
        # `from . import mod`, `from .mod import`, importlib/__import__ strings
        # "mod"/"factory.bin.mod", and the file path itself.
        pattern = (
            rf"(^|[^A-Za-z0-9_.])(import\s+([A-Za-z0-9_.]+\s*,\s*)*({re.escape(dotted)}|{mod})\b"
            rf"|from\s+({re.escape(dotted)}|{mod}|\.+{mod})\s+import"
            rf"|from\s+{re.escape(parent)}\s+import\s+([A-Za-z0-9_,\s]*\b{mod}\b)"
            rf"|from\s+\.+[A-Za-z0-9_.]*\s+import\s+([A-Za-z0-9_,\s()]*\b{mod}\b)"
            rf"|[\"']{re.escape(dotted)}[\"']"
            rf"|{re.escape(rel)}|{re.escape(rel[:-3])}\b)"
            rf"|(import_module|__import__|import_from_path|run_path|spec_from_file_location)\(\s*([A-Za-z_]+\s*=\s*)?[\"'](\.*{mod}|{re.escape(dotted)}|[A-Za-z0-9_./]*{re.escape(rel)})[\"']"
        )
        gg = _git(repo, "grep", "-n", "-I", "-P", pattern, "HEAD", "--", *REF_SCAN_PATHSPECS)
        if gg.returncode not in (0, 1):
            raise Unreadable(
                f"GREP_REFERENCES_UNREADABLE: {rel}: rc={gg.returncode}: {gg.stderr[:160]}")
        for ln in gg.stdout.splitlines():
            # git grep prefixes matches with "HEAD:path:line:".
            path = ln.split(":", 2)[1] if ln.startswith("HEAD:") else ln.split(":", 1)[0]
            if any(path.startswith(x) for x in EXCLUDED_REF_DIRS) or not path.endswith(REF_SCAN_EXTENSIONS):
                continue
            if any(seg in path for seg in EXCLUDED_REF_SEGMENTS) or path.endswith(EXCLUDED_REF_SUFFIXES):
                continue
            if Path(path).name in EXCLUDED_REF_BASENAMES:
                continue
            if path == rel or path == MANIFEST_REL or path.startswith(str(RECEIPT_DIR) + "/SIMPLIFICATION_"):
                continue
            hits.append(ln[:200])
        hits.extend(_ast_import_references(repo, rel, mod, dotted, parent))
    return hits


def _ast_import_references(repo: Path, rel: str, mod: str, dotted: str, parent: str) -> list[str]:
    """L5_factory_v2_rest_3-013: `from pkg import (\n    mod,\n)` spans lines, so
    line-oriented git grep never sees `mod` next to `import`. Every tracked .py
    blob at HEAD containing a parenthesised import is parsed with ast and its
    Import/ImportFrom nodes are resolved against the deleted module."""
    ls = _git(repo, "grep", "-l", "-I", "-P", r"^\s*(from\s+\S+\s+)?import\s*\(", "HEAD", "--",
              *[f"{p}*.py" for p in REF_SCAN_PATHSPECS])
    if ls.returncode not in (0, 1):
        raise Unreadable(f"GREP_AST_REFERENCES_UNREADABLE: {rel}: rc={ls.returncode}: {ls.stderr[:160]}")
    out: list[str] = []
    for entry in ls.stdout.splitlines():
        path = entry.split(":", 1)[1] if entry.startswith("HEAD:") else entry
        if any(path.startswith(x) for x in EXCLUDED_REF_DIRS) or any(seg in path for seg in EXCLUDED_REF_SEGMENTS):
            continue
        if path == rel or path.endswith(EXCLUDED_REF_SUFFIXES) or Path(path).name in EXCLUDED_REF_BASENAMES:
            continue
        blob = _git(repo, "show", f"HEAD:{path}")
        if blob.returncode:
            continue
        try:
            tree = ast.parse(blob.stdout)
        except (SyntaxError, ValueError):
            continue
        pkg_of_path = ".".join(path[:-3].split("/")[:-1])
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any(a.name in (dotted, mod) for a in node.names):
                    out.append(f"HEAD:{path}:{node.lineno}:import {mod} (ast)")
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    up = pkg_of_path.split(".") if pkg_of_path else []
                    up = up[:len(up) - (node.level - 1)] if node.level > 1 else up
                    base = ".".join([*up, base] if base else up)
                names = [a.name for a in node.names]
                if base in (dotted, mod) or (base == parent and mod in names) \
                        or (base and mod in names and f"{base}.{mod}" == dotted):
                    out.append(f"HEAD:{path}:{node.lineno}:from {base} import {mod} (ast)")
    return out


def _fingerprints_at(repo: Path, sha: str, rel: str) -> tuple[str, set[str]]:
    """(blob sha, top-level def/class names) of `rel` at `sha`."""
    blob = _git(repo, "rev-parse", f"{sha}:{rel}")
    src = _git(repo, "show", f"{sha}:{rel}")
    names = set(re.findall(r"^(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)", src.stdout, flags=re.M)) if src.returncode == 0 else set()
    return (blob.stdout.strip() if blob.returncode == 0 else ""), names


def _check_not_moved(repo: Path, manifest: dict, batch: dict) -> None:
    """A deleted module must not survive under another name: no blob at HEAD may
    equal a deleted file's frozen blob, and no top-level def/class that existed
    ONLY in a deleted file at the freeze commit may reappear in a file that did
    not exist at the freeze commit (CODE_MOVED_NOT_DELETED)."""
    base = str(manifest.get("frozen_at_control_sha") or "")
    if not base:
        return
    head_tree = _git(repo, "ls-tree", "-r", "HEAD")
    if head_tree.returncode:
        raise Unreadable("CANNOT_LIST_HEAD")
    head_blobs = {}
    for ln in head_tree.stdout.splitlines():
        meta, _, path = ln.partition("\t")
        head_blobs.setdefault(meta.split()[2], []).append(path)
    base_files = set(_git(repo, "ls-tree", "-r", "--name-only", base).stdout.splitlines())
    tracked_now = set(_tracked_files(repo))
    new_py = [p for p in tracked_now - base_files if p.endswith(".py") and not p.startswith("tests/")]
    deleted_names: set[str] = set()
    for rel in batch["paths"]:
        blob, names = _fingerprints_at(repo, base, rel)
        if blob and blob in head_blobs:
            raise Unmet(f"CODE_MOVED_NOT_DELETED: blob of {rel} tracked at HEAD as {head_blobs[blob][0]}")
        deleted_names |= names
    if not new_py or not deleted_names:
        return
    # names that existed elsewhere at the freeze commit are not evidence of a move
    other_defs = _git(repo, "grep", "-h", "-I", "-P", r"^(def|class)\s+[A-Za-z_][A-Za-z0-9_]*", base, "--",
                      *[f":!{p}" for p in batch["paths"]], "factory/", "factory_v2/", "scripts/")
    common = set(re.findall(r"^(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)", other_defs.stdout, flags=re.M))
    unique = {n for n in deleted_names - common if not n.startswith("_") and len(n) > 6}
    for path in new_py:
        _, names = _fingerprints_at(repo, "HEAD", path)
        hit = unique & names
        if len(hit) >= 3:
            raise Unmet(f"CODE_MOVED_NOT_DELETED: {path} defines {sorted(hit)[:3]} from a deleted module")


def _check_batch_invariants(repo: Path, manifest: dict, batch_id: str, batch: dict, tracked: set) -> None:
    """Every mechanical invariant of one batch at HEAD; re-run for dependencies."""
    for rel in batch["paths"]:
        if rel in tracked:
            raise Unmet(f"PATH_STILL_PRESENT: {rel} tracked at HEAD")
        if (repo / rel).exists():
            raise Unmet(f"PATH_STILL_PRESENT: {rel} exists on disk")
    refs = _references(repo, batch["paths"])
    if refs:
        raise Unmet(f"REFERENCE_REMAINS[{batch_id}]: " + " | ".join(refs[:8]))
    for token in batch.get("forbidden_tokens") or []:
        # `-e` makes option-looking tokens data. rc=1 means no match; rc>1 is UNKNOWN.
        gg = _git(repo, "grep", "-n", "-I", "-F", "-e", token,
                  "HEAD", "--", *REF_SCAN_PATHSPECS)
        if gg.returncode not in (0, 1):
            raise Unreadable(
                f"GREP_FORBIDDEN_TOKEN_UNREADABLE[{batch_id}]: rc={gg.returncode}: "
                f"{gg.stderr[:160]}")
        for ln in gg.stdout.splitlines():
            path = ln.split(":", 2)[1] if ln.startswith("HEAD:") else ln.split(":", 1)[0]
            if not path.endswith(TOKEN_SCAN_EXTENSIONS) or any(path.startswith(x) for x in EXCLUDED_REF_DIRS):
                continue
            if any(seg in path for seg in EXCLUDED_REF_SEGMENTS):
                continue
            raise Unmet(f"FORBIDDEN_TOKEN_REMAINS[{batch_id}]: {token!r}: {ln[:160]}")
    _check_not_moved(repo, manifest, batch)


def _check_dependency_receipt(repo: Path, manifest: dict, dep: str) -> None:
    """The predecessor's receipt must be committed at HEAD, well-formed, BATCH_VERIFIED,
    name exactly the predecessor's frozen paths, and its control_sha must be an
    ancestor of HEAD — a receipt written by hand or for another tree is refused."""
    rel = f"{RECEIPT_DIR}/SIMPLIFICATION_{dep}_RECEIPT.json"
    shown = _git(repo, "show", f"HEAD:{rel}")
    if shown.returncode:
        raise Unmet(f"DEPENDENCY_UNVERIFIED: batch {dep} has no receipt at HEAD")
    try:
        rec = json.loads(shown.stdout)
    except json.JSONDecodeError:
        raise Unmet(f"DEPENDENCY_UNVERIFIED: receipt {dep} corrupt")
    dep_batch = manifest["batches"].get(dep) or {}
    if (rec.get("schema") != "simplification-batch-receipt/v1" or rec.get("result") != "BATCH_VERIFIED"
            or rec.get("batch") != dep or sorted(rec.get("deleted_paths") or []) != sorted(dep_batch.get("paths") or [])):
        raise Unmet(f"DEPENDENCY_UNVERIFIED: receipt {dep} does not match the frozen batch")
    sha = str(rec.get("control_sha") or "")
    anc = _git(repo, "merge-base", "--is-ancestor", sha, "HEAD") if sha else None
    if anc is None or anc.returncode:
        raise Unmet(f"DEPENDENCY_UNVERIFIED: receipt {dep} control_sha {sha[:12]} is not an ancestor of HEAD")
    # A well-formed receipt is necessary, never sufficient: the predecessor's own
    # invariants are replayed at HEAD, so a forged receipt buys nothing.
    try:
        _check_batch_invariants(repo, manifest, dep, dep_batch, set(_tracked_files(repo)))
    except Unmet as exc:
        raise Unmet(f"DEPENDENCY_UNVERIFIED: batch {dep} no longer holds at HEAD: {exc}")


def _write_receipt(repo: Path, batch: str, payload: dict) -> Path:
    out = repo / RECEIPT_DIR / f"SIMPLIFICATION_{batch}_RECEIPT.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, out)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="simplification batch verifier")
    ap.add_argument("batch")
    ap.add_argument("--project-repo", required=True)
    args = ap.parse_args(argv)
    expected_blob = MANIFEST_BLOB_SHA
    repo = Path(args.project_repo).resolve()
    head = _git(repo, "rev-parse", "HEAD")
    if head.returncode:
        print("SIMPLIFICATION_UNREADABLE: cannot resolve HEAD", file=sys.stderr)
        return 2
    control_sha = head.stdout.strip()
    try:
        manifest = _load_manifest(repo, expected_blob)
        batch = manifest["batches"].get(args.batch)
        if not isinstance(batch, dict) or not isinstance(batch.get("paths"), list) or not batch["paths"]:
            raise Unreadable(f"BATCH_UNKNOWN: {args.batch}")
        dep = batch.get("depends_on")
        if dep:
            _check_dependency_receipt(repo, manifest, dep)
        tracked = set(_tracked_files(repo))
        _check_batch_invariants(repo, manifest, args.batch, batch, tracked)
        # Only the tests frozen as removable may disappear: every other test file
        # tracked at the manifest's freeze commit must still be tracked at HEAD.
        base_sha = str(manifest.get("frozen_at_control_sha") or "")
        allowed_missing = set(batch.get("tests_removable") or [])
        # tests removed by an earlier batch whose receipt is committed at HEAD are not
        # this batch's deletions
        for other_id, other in manifest["batches"].items():
            if other_id != args.batch and _git(repo, "cat-file", "-e", f"HEAD:{RECEIPT_DIR}/SIMPLIFICATION_{other_id}_RECEIPT.json").returncode == 0:
                allowed_missing |= set(other.get("tests_removable") or [])
        if not base_sha:
            raise Unreadable("FROZEN_CONTROL_SHA_MISSING: manifest has no frozen_at_control_sha")
        base_ls = _git(repo, "ls-tree", "-r", "--name-only", base_sha, "--", "tests")
        if base_ls.returncode:
            raise Unreadable(f"FROZEN_CONTROL_SHA_UNAVAILABLE: {base_sha[:12]} cannot be read")
        gone = [t for t in base_ls.stdout.splitlines() if t.strip() and t not in tracked and t not in allowed_missing]
        if gone:
            raise Unmet("TEST_DELETED_OUTSIDE_LIST: " + ", ".join(gone[:6]))
        loc_now = _measure_loc(repo, manifest.get("loc_scope") or ["factory/", "factory_v2/"])
        baseline = int(manifest["baseline_loc"])
        expected = int(batch["expected_loc_removed"])
        # The proof of removal is PATH_STILL_PRESENT + REFERENCE_REMAINS +
        # CODE_MOVED_NOT_DELETED above. The global LOC total is reported, not
        # gated: unrelated work lands on the same tree between the freeze and
        # the batch (2026-09-13: +1 700 lines of judges/lots made a true
        # deletion of 2 590 lines look like "not reduced").
        removed_measured = 0
        base_sha = str(manifest.get("frozen_at_control_sha") or "")
        for rel in batch["paths"]:
            blob = _git(repo, "show", f"{base_sha}:{rel}") if base_sha else None
            if blob is not None and blob.returncode == 0:
                removed_measured += blob.stdout.count("\n")
        if removed_measured < int(expected * 0.9):
            raise Unmet(f"REMOVED_LOC_BELOW_EXPECTED: frozen blobs total {removed_measured} < 90% of {expected}")
    except Unreadable as exc:
        print(f"SIMPLIFICATION_UNREADABLE: {exc}", file=sys.stderr)
        return 2
    except Unmet as exc:
        print(f"SIMPLIFICATION_UNMET: {exc}", file=sys.stderr)
        return 1
    out = _write_receipt(repo, args.batch, {
        "schema": "simplification-batch-receipt/v1", "batch": args.batch, "result": "BATCH_VERIFIED",
        "control_sha": control_sha, "deleted_paths": batch["paths"], "loc_baseline": baseline,
        "loc_now": loc_now, "loc_removed_measured": removed_measured, "loc_delta_vs_baseline": baseline - loc_now,
    })
    print(f"BATCH_VERIFIED: {args.batch} deleted={len(batch['paths'])} loc {baseline}->{loc_now}; receipt={out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
