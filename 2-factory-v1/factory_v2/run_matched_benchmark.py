#!/usr/bin/env python3
"""CHAMPION_VS_RESHAPED_CHALLENGER — matched benchmark producer.

Runs the SAME set of hermetic probes (factory_v2/_benchmark_probes.py) against two
git SHAs of this repository — the "champion" and the "challenger" — from the same
committed start_sha of a throwaway hermetic test project, and writes one
content-addressed observation JSON per (side, probe). Never launches a real AI
engine (FACTORY_NO_REAL_ENGINE=1 everywhere; probes that must exercise a builder
step monkeypatch dispatcher_v3._run in-process, exactly like tests/test_c43_*.py
and tests/test_c47_*.py already do).

For each side, a temporary `git worktree add --detach <tmp> <sha>` checks out the
code at that sha; the probe driver (factory_v2/_benchmark_probes.py, read from
THIS checkout — never copied into the side worktree, so the driver logic is
byte-identical for both sides) is invoked as a subprocess with
PYTHONPATH=<side worktree> and cwd=<side worktree>, so `import factory_v2...`
resolves to the code AT THAT SHA.

Independently reconstructed by factory_v2/verify_factory_hygiene.py
(check_matched_benchmark, pinned at 2586dc46e2be9da42d519a19e6fcfaae1a805855):
every observation is re-hashed against MANIFEST.json, metrics are recomputed from
the observations (never trusted from this producer's own receipt), and
PROMOTE/REJECT is re-derived from the recomputed metrics.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PROBE_DRIVER = REPO_ROOT / "factory_v2" / "_benchmark_probes.py"
PROBE_IDS = (
    "dispatch_settles",
    "kill_and_resume",
    "quarantine_cycle",
    "contract_refreeze",
    "terminal_state_new_attempt",
    "unchanged_deliverable_reconfirmed",
)


class ArmsNotMatched(RuntimeError):
    pass


def _git_checked(*args: str, cwd: "Path | None" = None) -> str:
    r = subprocess.run(["git", "-C", str(cwd or REPO_ROOT), *args],
                       capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr}")
    return r.stdout.strip()


def resolve_sha(rev: str) -> str:
    return _git_checked("rev-parse", rev)


def make_worktree(sha: str, base: Path) -> Path:
    wt = base / f"wt-{sha[:12]}"
    _git_checked("worktree", "add", "--detach", "-q", str(wt), sha)
    return wt


def remove_worktree(wt: Path) -> None:
    subprocess.run(["git", "-C", str(REPO_ROOT), "worktree", "remove", "--force", str(wt)],
                   capture_output=True, text=True)
    shutil.rmtree(wt, ignore_errors=True)


def run_probe(side: str, side_root: Path, side_sha: str, probe_id: str, workload_id: str) -> dict:
    import os
    env = dict(os.environ)
    env["PYTHONPATH"] = str(side_root)
    env["FACTORY_NO_REAL_ENGINE"] = "1"
    r = subprocess.run(
        [sys.executable, str(PROBE_DRIVER), "--side", side, "--side-root", str(side_root),
         "--side-sha", side_sha, "--probe", probe_id, "--workload", workload_id],
        cwd=str(side_root), env=env,
        capture_output=True, text=True, timeout=180,
    )
    if r.returncode != 0:
        raise RuntimeError(f"probe {side}/{probe_id} failed rc={r.returncode}\n"
                          f"stdout={r.stdout[-2000:]}\nstderr={r.stderr[-2000:]}")
    line = [l for l in r.stdout.splitlines() if l.strip()][-1]
    return json.loads(line)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workload", required=True)
    ap.add_argument("--champion", required=True)
    ap.add_argument("--challenger", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    champion_sha = resolve_sha(args.champion)
    challenger_sha = resolve_sha(args.challenger)
    if champion_sha == challenger_sha:
        print("ARMS_NOT_MATCHED: champion_sha == challenger_sha", file=sys.stderr)
        return 2

    out_dir = REPO_ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    tmp_base = Path(tempfile.mkdtemp(prefix="matched-bench-worktrees-"))
    worktrees = {}
    try:
        worktrees["champion"] = make_worktree(champion_sha, tmp_base)
        worktrees["challenger"] = make_worktree(challenger_sha, tmp_base)

        side_shas = {"champion": champion_sha, "challenger": challenger_sha}
        observations: dict[str, dict] = {}
        start_shas: set[str] = set()
        for side in ("champion", "challenger"):
            for probe_id in PROBE_IDS:
                obs = run_probe(side, worktrees[side], side_shas[side], probe_id, args.workload)
                if obs.get("side_sha") != side_shas[side]:
                    raise ArmsNotMatched(f"{side}/{probe_id}: side_sha mismatch")
                start_shas.add(obs["start_sha"])
                observations[f"{side}_{probe_id}.json"] = obs
                print(f"{side}/{probe_id}: settled={obs['settled']} wall_s={obs['wall_s']} "
                     f"launches_lost={obs['launches_lost']} restart_recovered={obs['restart_recovered']}")

        if len(start_shas) != 1 or not all(start_shas):
            raise ArmsNotMatched(f"ARMS_NOT_MATCHED: divergent start_sha across probes: {start_shas}")
        start_sha = next(iter(start_shas))
    finally:
        for wt in worktrees.values():
            remove_worktree(wt)
        shutil.rmtree(tmp_base, ignore_errors=True)

    # write observation files + manifest (content-addressed)
    manifest_observations = {}
    for name, obs in sorted(observations.items()):
        path = out_dir / name
        blob = json.dumps(obs, sort_keys=True, indent=2) + "\n"
        path.write_text(blob, encoding="utf-8")
        manifest_observations[name] = hashlib.sha256(blob.encode("utf-8")).hexdigest()

    # producer_sha256 in the manifest must match sha256(HEAD:factory_v2/run_matched_benchmark.py)
    # at judge time — hash THIS file's own on-disk bytes (committed unmodified next).
    this_file = Path(__file__).resolve()
    producer_sha256 = hashlib.sha256(this_file.read_bytes()).hexdigest()

    manifest = {
        "producer_sha256": producer_sha256,
        "champion_sha": champion_sha,
        "challenger_sha": challenger_sha,
        "start_sha": start_sha,
        "workload_id": args.workload,
        "observations": manifest_observations,
    }
    (out_dir / "MANIFEST.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n",
                                           encoding="utf-8")

    def agg(side: str) -> dict:
        rows = [observations[f"{side}_{p}.json"] for p in PROBE_IDS]
        return {"probes": len(rows), "settled": sum(1 for o in rows if o.get("settled") is True),
                "wall_s": round(sum(float(o.get("wall_s") or 0) for o in rows), 3),
                "restart_recovered": sum(1 for o in rows if o.get("restart_recovered") is True),
                "launches_lost": sum(int(o.get("launches_lost") or 0) for o in rows)}

    metrics = {"champion": agg("champion"), "challenger": agg("challenger")}
    c, ch = metrics["champion"], metrics["challenger"]
    dominates = (ch["settled"] >= c["settled"] and ch["wall_s"] <= c["wall_s"]
                and ch["launches_lost"] <= c["launches_lost"]
                and ch["restart_recovered"] >= c["restart_recovered"] and ch != c)
    decision = "PROMOTE" if dominates else "REJECT"

    pointer_path = REPO_ROOT / "factory_v2" / "CHAMPION_POINTER"
    pointer_before = pointer_path.read_text(encoding="utf-8").strip() if pointer_path.exists() else champion_sha
    if decision == "PROMOTE":
        pointer_after = challenger_sha
    else:
        pointer_after = pointer_before
    pointer_path.write_text(pointer_after + "\n", encoding="utf-8")

    evidence_path = f"{args.out.rstrip('/')}/DECISION_EVIDENCE.md".replace(str(REPO_ROOT) + "/", "")
    evidence_abs = REPO_ROOT / evidence_path
    evidence_abs.parent.mkdir(parents=True, exist_ok=True)
    evidence_abs.write_text(render_evidence(args.workload, start_sha, champion_sha, challenger_sha,
                                            metrics, decision, dominates), encoding="utf-8")

    receipt = {
        "schema": "matched-benchmark-receipt/v1",
        "workload_id": args.workload,
        "start_sha": start_sha,
        "champion_sha": champion_sha,
        "challenger_sha": challenger_sha,
        "observations_dir": args.out.rstrip("/"),
        "metrics": metrics,
        "decision": decision,
        "champion_pointer_before": pointer_before,
        "champion_pointer_after": pointer_after,
        "decision_evidence_path": evidence_path,
    }
    receipt_path = REPO_ROOT / "factory_v2" / "receipts" / "BENCHMARK_RECEIPT.json"
    receipt_path.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    print(json.dumps({"decision": decision, "metrics": metrics, "pointer_after": pointer_after}, indent=2))
    return 0


def render_evidence(workload_id: str, start_sha: str, champion_sha: str, challenger_sha: str,
                    metrics: dict, decision: str, dominates: bool) -> str:
    c, ch = metrics["champion"], metrics["challenger"]
    lines = [
        f"# CHAMPION_VS_RESHAPED_CHALLENGER — decision evidence",
        "",
        f"workload_id: `{workload_id}`  ",
        f"start_sha (hermetic test project, identical both sides): `{start_sha}`  ",
        f"champion_sha: `{champion_sha}` (runtime d'avant la Ferrari)  ",
        f"challenger_sha: `{challenger_sha}` (HEAD au moment du run)  ",
        "",
        "## Metrics (recomputed identically to the pinned judge's `agg`)",
        "",
        "| side | probes | settled | wall_s (sum) | restart_recovered | launches_lost |",
        "|---|---|---|---|---|---|",
        f"| champion | {c['probes']} | {c['settled']} | {c['wall_s']} | {c['restart_recovered']} | {c['launches_lost']} |",
        f"| challenger | {ch['probes']} | {ch['settled']} | {ch['wall_s']} | {ch['restart_recovered']} | {ch['launches_lost']} |",
        "",
        "## Decision rule",
        "",
        "PROMOTE only if the challenger dominates on ALL four axes (settled >=, "
        "wall_s <=, launches_lost <=, restart_recovered >=) and the two metric rows "
        "are not identical.",
        "",
        f"Domination holds: **{dominates}**.",
        f"Decision: **{decision}**.",
        "",
    ]
    if decision == "REJECT":
        comparisons = []
        for label, key, better in (
                ("settled", "settled", "higher"),
                ("wall_s", "wall_s", "lower"),
                ("restart_recovered", "restart_recovered", "higher"),
                ("launches_lost", "launches_lost", "lower")):
            cv, xv = c[key], ch[key]
            relation = "equal" if xv == cv else (
                "better" if (xv > cv) == (better == "higher") else "worse")
            comparisons.append(f"{label}: challenger={xv}, champion={cv} ({relation})")
        lines.append(
            f"The challenger settled {ch['settled']}/{ch['probes']} probes vs "
            f"{c['settled']}/{c['probes']} for the champion. Observed axes: "
            + "; ".join(comparisons) + ". It therefore did not dominate every axis. "
            "Per the fixed decision rule, "
            "a measured REJECT is a COMPLETED experiment: the champion pointer is left "
            "unchanged."
        )
    else:
        lines.append(
            "The challenger dominates on every declared metric without an acceptance "
            "regression: the champion pointer moves to challenger_sha."
        )
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.exit(main())
