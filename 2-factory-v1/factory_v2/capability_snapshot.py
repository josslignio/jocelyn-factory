"""Capability snapshot — frozen record of what the factory's actors, owner
routing policies, product validator and network policy allow, taken before
and after a self-modification experiment (SAFE_SELF_IMPROVEMENT).

D018/D019: a self-modification must not widen capability, network or
validation authority. The snapshot is the evidence: `snapshot()` is a pure
function of the committed repository content (never of local/uncommitted
state), so `before` and `after` can be compared byte-for-byte on their four
capability fields by an independent judge running at HEAD.

Sources (read-only; this module writes nothing to any of them):
- actor_capabilities: factory/bin/model_router.py::_ACTOR_CAPABILITY — the
  ActorCapabilityPolicy table (which spec_levels each canonical engine may
  handle).
- owner_policy_ids: factory/bin/owner_routing_policy.py — POLICY_ID plus
  FULL_ASSIGNMENT_POLICY_IDS (QUOTA_POLICY_ID, PHASE3_POLICY_ID).
- validator_source_sha256: legacy digest of the committed validator shim.
- effective_validator_source_sha256: digest of the shim AND its effective
  targets — factory_verifiers.py and
  products/twitter_radar/product_verifiers.py (VALIDATOR_SOURCES).
- network_policy: static, explicit statement of the factory's network
  posture (no outbound calls, no real AI engine invocation) — the
  FACTORY_NO_REAL_ENGINE convention used across factory_v2.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def _git_out(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    )
    return out.stdout


def _head_bytes(repo: Path, rel_path: str) -> bytes:
    out = subprocess.run(
        ["git", "-C", str(repo), "show", f"HEAD:{rel_path}"],
        capture_output=True, check=True,
    )
    return out.stdout


def _actor_capabilities(repo: Path) -> dict:
    """Frozen ActorCapabilityPolicy table, read from the committed content of
    factory/bin/model_router.py (HEAD) so the snapshot never reflects
    uncommitted edits. Returns {engine: sorted list of spec levels}."""
    src = _head_bytes(repo, "factory/bin/model_router.py").decode("utf-8")
    ns: dict = {}
    marker = "_ACTOR_CAPABILITY: dict[str, set[str]] = {"
    start = src.index(marker) + len(marker) - 1  # keep opening brace
    depth = 0
    end = start
    for i, ch in enumerate(src[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    literal = src[start:end]
    exec(compile("_ACTOR_CAPABILITY = " + literal, "<model_router._ACTOR_CAPABILITY>", "exec"), ns)
    table = ns["_ACTOR_CAPABILITY"]
    return {engine: sorted(levels) for engine, levels in sorted(table.items())}


def _owner_policy_ids(repo: Path) -> list[str]:
    """Sorted, de-duplicated list of every owner-routing policy id known to
    factory/bin/owner_routing_policy.py: POLICY_ID plus every id in
    FULL_ASSIGNMENT_POLICY_IDS (QUOTA_POLICY_ID, PHASE3_POLICY_ID)."""
    src = _head_bytes(repo, "factory/bin/owner_routing_policy.py").decode("utf-8")
    ns: dict = {"__name__": "_owner_routing_policy_snapshot"}
    ids: set[str] = set()
    for name in ("POLICY_ID", "QUOTA_POLICY_ID", "PHASE3_POLICY_ID"):
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith(f"{name} ="):
                exec(compile(stripped, f"<owner_routing_policy.{name}>", "exec"), ns)
                ids.add(ns[name])
                break
    return sorted(ids)


#: The validator surface the digest MUST cover. factory_v2/product_verifiers.py
#: became a 25-line delegation shim (FACTORY_SIMPLIFY_S3): the checks that
#: actually run live in factory_verifiers.py and the TR product verifiers.
#: L5_factory_v2_rest_1-010: hashing the shim alone let two commits whose
#: EFFECTIVE validators differ produce identical snapshots.
VALIDATOR_SOURCES: tuple[str, ...] = (
    "factory_v2/product_verifiers.py",
    "factory_v2/factory_verifiers.py",
    "products/twitter_radar/product_verifiers.py",
)


def _validator_source_sha256(repo: Path) -> str:
    """sha256 over the shim AND every effective verifier it delegates to.

    Deterministic: entries are `sha256(path) = sha256(head-bytes)` joined in
    VALIDATOR_SOURCES order, hashed again. Fails closed (non-zero exit from
    `git show`) if any effective source is missing at HEAD — a silent shim-only
    digest is exactly the defect this repairs."""
    h = hashlib.sha256()
    for rel in VALIDATOR_SOURCES:
        blob = _head_bytes(repo, rel)
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(hashlib.sha256(blob).hexdigest().encode("ascii"))
        h.update(b"\0")
    return h.hexdigest()


def _network_policy() -> dict:
    """Static statement of the factory's network posture. Not derived from
    environment variables (which vary per-process and would make snapshots
    incomparable) — this is the policy the factory is built to enforce:
    no outbound network calls, no real AI engine invocation from a
    self-modification experiment (FACTORY_NO_REAL_ENGINE convention)."""
    return {
        "outbound_network_calls_allowed": False,
        "real_ai_engine_invocation_allowed": False,
        "convention": "FACTORY_NO_REAL_ENGINE",
    }


def snapshot(repo_root: str | Path) -> dict:
    """Pure function of the committed repository content at HEAD."""
    repo = Path(repo_root)
    return {
        "actor_capabilities": _actor_capabilities(repo),
        "owner_policy_ids": _owner_policy_ids(repo),
        # Preserve the public shim field; qualification requires the effective
        # digest as well, so compatibility cannot weaken validation.
        "validator_source_sha256": hashlib.sha256(
            _head_bytes(repo, "factory_v2/product_verifiers.py")).hexdigest(),
        "effective_validator_source_sha256": _validator_source_sha256(repo),
        "network_policy": _network_policy(),
        "taken_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def write_snapshot(repo_root: str | Path, label: str) -> Path:
    repo = Path(repo_root)
    data = snapshot(repo)
    out_dir = repo / "factory_v2" / "receipts" / "capability_snapshots"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"{ts}_{label}.json"
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write a capability snapshot.")
    parser.add_argument("--label", required=True, choices=["before", "after"])
    parser.add_argument("--repo-root", default=".")
    args = parser.parse_args(argv)
    path = write_snapshot(args.repo_root, args.label)
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
