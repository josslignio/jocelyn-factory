"""Challenger role runner — READ-ONLY, isolated, real subprocess (A2).

Launched by factory_v2.audit_loop.run_challenger as:
    python3 -I cell_challenger.py --package <sandbox>/package.json \
            --out <sandbox>/findings.json

Standalone stdlib-only script (isolated mode keeps the product tree and the
controller environment out of scope). It may write ONLY inside its sandbox —
its single output artifact is the findings file. Everything it reports is
recomputable from the package: evidence hashes are recomputed, never trusted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


def _sha16_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _package_integrity(pkg: dict) -> list[str]:
    problems = []
    payload = {k: v for k, v in pkg.items() if k != "package_sha"}
    canon = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    recomputed = hashlib.sha256(canon.encode()).hexdigest()[:24]
    if recomputed != pkg.get("package_sha"):
        problems.append(f"package_sha mismatch {recomputed} != "
                        f"{pkg.get('package_sha')}")
    if pkg.get("role") != "challenger":
        problems.append("role is not challenger")
    for key in ("bundle_sha", "budget_s", "capability", "allowed_inputs"):
        if key not in pkg:
            problems.append(f"missing typed field {key}")
    return problems


def challenge(pkg: dict) -> list[dict]:
    """Deterministic read-only challenge of the frozen evidence.

    Recomputes every evidence hash from the raw bytes on disk; a mismatch is a
    BLOCKING finding (the bundle lied about its evidence). Structural risks
    (repeated failure, strategy monoculture) are non-blocking advisories.
    """
    findings: list[dict] = []
    problems = _package_integrity(pkg)
    if problems:
        return [{"description": f"work package not trustworthy: {p}",
                 "is_blocking": True} for p in problems]
    for inp in pkg.get("allowed_inputs", []):
        path = Path(inp["path"])
        try:
            data = path.read_bytes()
        except OSError as exc:
            findings.append({"description": f"evidence {path.name} unreadable: {exc}",
                             "is_blocking": True})
            continue
        recomputed = _sha16_bytes(data)
        if recomputed != inp.get("sha16"):
            findings.append({
                "description": f"evidence {path.name} hash drift "
                               f"{recomputed} != {inp.get('sha16')}",
                "is_blocking": True})
    return findings


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--package", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    pkg = json.loads(Path(args.package).read_text(encoding="utf-8"))
    findings = challenge(pkg)
    out = {"schema": "cell-findings/v1",
           "produced_by": "cell_challenger",
           "package_sha": pkg.get("package_sha"),
           "bundle_sha": pkg.get("bundle_sha"),
           "findings": findings}
    Path(args.out).write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps({"findings": len(findings),
                      "blocking": sum(1 for f in findings if f["is_blocking"])}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
