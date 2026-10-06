#!/usr/bin/env python3
"""Juge d'un lot d'épuration (FACTORY_CORE_REWRITE_* / FACTORY_DEDUP_*).

Ordre propriétaire 21/09 : la factory doit devenir plus simple, plus légère, plus efficace —
et rien ne doit régresser. Recompté, jamais cru :

  1. POIDS : la somme des lignes des fichiers ciblés (`--targets a.py,b.py`) est <= --max-loc,
     ET le total `git ls-files 'factory/*.py' 'factory_v2/*.py' | xargs wc -l` est <= --max-total
     (le lot ne déplace pas le poids ailleurs).
  2. CONTRAT DE NON-RÉGRESSION : suite complète verte (sûre, mêmes exclusions que la roadmap),
     banc de chaos série 1 (12/12) et série 2 (8/8) verts.
  3. BENCHMARK : `run_matched_benchmark_3arm.py --verify-only` accepte le reçu à HEAD (le champion
     n'est pas dégradé) — sauté avec --skip-benchmark (jamais dans product_e2e).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

IGN = ("--ignore=tests/test_stabilization_proof.py --ignore=tests/test_external_supervisor_production_path.py "
       "--ignore=tests/test_lr4_interprocess.py --ignore=tests/test_p0_supervision_repairs.py "
       "--ignore=tests/test_sol_ladder_fairness.py --ignore=tests/test_telemetry_integration.py "
       "--ignore=tests/test_tr_weekly_refresh_verifier.py").split()


class Unmet(Exception):
    pass


def _loc(repo: Path, files) -> int:
    n = 0
    for f in files:
        p = repo / f
        if p.is_file():
            n += sum(1 for _ in p.open("rb"))
    return n


def _total_loc(repo: Path) -> int:
    out = subprocess.run(["git", "ls-files", "factory/*.py", "factory_v2/*.py"], cwd=repo,
                         capture_output=True, text=True, timeout=30).stdout.split()
    return _loc(repo, out)


MAX_LINE_LEN = 200   # 21/09 : l'essai 1 du runtime « atteignait » le seuil en collant des lignes de 819 caractères


def _longest_line(repo: Path, files) -> tuple[int, str]:
    worst = (0, "")
    for f in files:
        p = repo / f
        if p.is_file():
            for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if len(line) > worst[0]:
                    worst = (len(line), f"{f}:{i}")
    return worst


def verify(repo: Path, targets, max_loc: int, max_total: int, skip_benchmark: bool, timeout_s: int) -> dict:
    env = {**os.environ, "FACTORY_NO_REAL_ENGINE": "1", "PYTHONPATH": str(repo)}
    ln, where = _longest_line(repo, targets)
    if ln > MAX_LINE_LEN:
        raise Unmet(f"FORME: ligne de {ln} caractères ({where}) > {MAX_LINE_LEN} — compresser la mise en forme n'est pas alléger")
    loc = _loc(repo, targets)
    if loc > max_loc:
        raise Unmet(f"POIDS: {loc} lignes sur {targets} > {max_loc}")
    total = _total_loc(repo)
    if total > max_total:
        raise Unmet(f"POIDS TOTAL: {total} lignes (factory/ + factory_v2/) > {max_total} — le poids a été déplacé, pas retiré")
    t = subprocess.run([sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider", "-x", *IGN],
                       cwd=repo, capture_output=True, text=True, timeout=timeout_s, env=env)
    m = re.search(r"(\d+) passed", t.stdout)
    if t.returncode != 0 or not m:
        raise Unmet(f"suite complète rouge: {t.stdout[-400:]}")
    for extra in ([], ["--set", "v2"]):
        c = subprocess.run([sys.executable, "factory_v2/verify_chaos_drill.py", "--project-repo", ".", *extra],
                           cwd=repo, capture_output=True, text=True, timeout=timeout_s, env=env)
        if c.returncode != 0 or "CHAOS_DRILL_VERIFIED" not in c.stdout:
            raise Unmet(f"banc de chaos {'v2' if extra else 'v1'} rouge: {c.stdout[-300:]}")
    bench = "SKIPPED"
    if not skip_benchmark:
        b = subprocess.run([sys.executable, "factory_v2/run_matched_benchmark_3arm.py", "--verify-only"],
                           cwd=repo, capture_output=True, text=True, timeout=timeout_s, env=env)
        if b.returncode != 0:
            raise Unmet(f"benchmark apparié refusé à HEAD: {(b.stdout + b.stderr)[-300:]}")
        bench = "VERIFIED"
    return {"targets_loc": loc, "total_loc": total, "tests": int(m.group(1)), "benchmark": bench}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project-repo", default=".")
    ap.add_argument("--targets", required=True, help="fichiers ciblés, séparés par des virgules")
    ap.add_argument("--max-loc", type=int, required=True)
    ap.add_argument("--max-total", type=int, required=True)
    ap.add_argument("--skip-benchmark", action="store_true")
    ap.add_argument("--timeout-s", type=int, default=3600)
    a = ap.parse_args(argv)
    try:
        r = verify(Path(a.project_repo).resolve(), [x.strip() for x in a.targets.split(",") if x.strip()],
                   a.max_loc, a.max_total, a.skip_benchmark, a.timeout_s)
    except Unmet as exc:
        print(f"CORE_REWRITE_UNMET: {exc}")
        return 1
    print(f"CORE_REWRITE_VERIFIED: {json.dumps(r)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
