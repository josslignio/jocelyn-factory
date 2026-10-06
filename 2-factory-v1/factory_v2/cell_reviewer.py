"""Reviewer role runner — exact-SHA-bound typed verdict, real subprocess (A3).

Launched by factory_v2.audit_loop.run_reviewer as:
    python3 -I cell_reviewer.py --package <sandbox>/package.json \
            --out <sandbox>/verdict.json

The verdict is bound to the EXACT candidate SHA: the reviewer recomputes the
repo HEAD itself from the git object store and refuses (non-zero exit) when it
does not match the package's candidate_sha. Its verdict is untrusted evidence
for the controller's deterministic synthesis — it can never override a failed
gate. Stdlib-only, writes only inside its sandbox.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path


def _repo_root_from_capability(pkg: dict) -> Path | None:
    """Derive the product repo root from the controller-granted read scope.

    The reviewer reads ONLY what the typed package allows: the .git directory
    granted for SHA binding plus the allowed evidence inputs. No other surface.
    """
    cap = (pkg.get("capability") or {}).get("allowed_read", [])
    for entry in cap:
        p = Path(entry)
        if p.name == ".git" and p.parent.exists():
            return p.parent
    return None


def review(pkg: dict) -> dict:
    findings: list[str] = []
    payload = {k: v for k, v in pkg.items() if k != "package_sha"}
    canon = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    recomputed = hashlib.sha256(canon.encode()).hexdigest()[:24]
    if recomputed != pkg.get("package_sha"):
        return {"verdict": "QUARANTINE",
                "reason": f"package integrity failed {recomputed}"}
    if pkg.get("role") != "reviewer":
        return {"verdict": "QUARANTINE", "reason": "role is not reviewer"}
    candidate = pkg.get("candidate_sha", "")
    repo = _repo_root_from_capability(pkg)
    if repo is None:
        print("no .git read scope granted: cannot bind verdict to exact SHA",
              file=sys.stderr)
        return {"verdict": "REFUSED", "reason": "NO_EXACT_SHA_SCOPE"}
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True, timeout=30)
    if head.returncode != 0:
        print(f"git rev-parse failed: {head.stderr[-200:]}", file=sys.stderr)
        return {"verdict": "REFUSED", "reason": "HEAD_UNREADABLE"}
    observed = head.stdout.strip()
    if observed != candidate:
        # Fail closed: never emit a verdict bound to a SHA we did not observe.
        print(f"HEAD {observed[:12]} != candidate {candidate[:12]}",
              file=sys.stderr)
        return {"verdict": "REFUSED", "reason": "SHA_MISMATCH"}
    for inp in pkg.get("allowed_inputs", []):
        p = Path(inp["path"])
        try:
            data = p.read_bytes()
        except OSError as exc:
            findings.append(f"evidence {p.name} unreadable: {exc}")
            continue
        if hashlib.sha256(data).hexdigest()[:16] != inp.get("sha16"):
            findings.append(f"evidence {p.name} hash drift")
    verdict = "REJECT" if findings else "ACCEPT"
    return {"verdict": verdict, "reason": "" if not findings else "; ".join(findings)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--package", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    pkg = json.loads(Path(args.package).read_text(encoding="utf-8"))
    result = review(pkg)
    if result["verdict"] == "REFUSED":
        # typed refusal: no verdict artifact, non-zero exit — the controller
        # classifies this as REVIEWER_INVALID_SCHEMA, never ACCEPT/REJECT.
        return 3
    out = {"schema": "cell-verdict/v1",
           "produced_by": "cell_reviewer",
           "package_sha": pkg.get("package_sha"),
           "reviewed_sha": pkg.get("candidate_sha"),
           "verdict": result["verdict"],
           "reason": result["reason"]}
    Path(args.out).write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps({"verdict": out["verdict"], "sha": out["reviewed_sha"][:12]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
