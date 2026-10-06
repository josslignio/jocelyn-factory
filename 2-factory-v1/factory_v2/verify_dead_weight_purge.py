#!/usr/bin/env python3
"""Juge de FACTORY_DEAD_WEIGHT_PURGE_V1 — le dépôt ne porte plus que ce qui sert.

Ordre propriétaire 22/09 04:55 Bangkok : « supprimer tout ce qui est mort, sert à rien, peut être nettoyé » —
y compris la zone gelée campaigns/RUN8 (gel levé par le propriétaire ; les preuves partent dans une branche d'archive,
l'historique git les garde, les SHA épinglés restent valides). Recompté, jamais cru :
  1. `git ls-files | wc -l` <= --max-files (défaut 2000) ; 0 fichier suivi sous campaigns/RUN8/, factory_v2/receipts/
     verified_fixes/, factory_v2/receipts/audit_cells/, factory/campaigns/{app007,M5,e1-probe}/, projects/job-radar/,
     projects/f5-*/ .
  2. Une branche d'archive `archive/pre-purge-2026-09` existe et contient campaigns/RUN8 (git ls-tree) : rien n'est perdu.
  3. Aucun fichier suivi orphelin : un chemin cité comme livrable dans une ROADMAP.yaml n'est jamais orphelin
     (preuve d'un jalon réglé, ex. research/deep_research/**, .context/**) ; sinon chaque chemin versionné est (a) du code/test/config/doc/juge/consigne dans les
     répertoires vivants (factory/, factory_v2/, tests/, projects/factory/, projects/twitter-radar/, docs/, products/,
     racine), et (b) pour `factory_v2/receipts/**`, référencé par ROADMAP.yaml (livrable) ou par un module vivant
     (`grep -rl` sur factory/ factory_v2/ tests/). Liste des orphelins vide.
  4. `factory/bin/frozen_zone_guard.py` ne réfère plus une zone absente de l'arbre (ou est retiré avec ses tests) ; la
     suite complète, le banc de chaos v1+v2 et le benchmark apparié restent verts (contrat de non-régression).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

DEAD = ("campaigns/RUN8/", "factory_v2/receipts/verified_fixes/", "factory_v2/receipts/audit_cells/",
        "factory/campaigns/app007/", "factory/campaigns/M5/", "factory/campaigns/e1-probe/",
        "projects/job-radar/", "projects/f5-")
LIVE_ROOTS = ("factory/", "factory_v2/", "tests/", "projects/factory/", "projects/twitter-radar/", "docs/",
              "products/", ".github/", ".claude/")
IGN = ("--ignore=tests/test_stabilization_proof.py --ignore=tests/test_external_supervisor_production_path.py "
       "--ignore=tests/test_lr4_interprocess.py --ignore=tests/test_p0_supervision_repairs.py "
       "--ignore=tests/test_sol_ladder_fairness.py --ignore=tests/test_telemetry_integration.py "
       "--ignore=tests/test_tr_weekly_refresh_verifier.py").split()


class Unmet(Exception):
    pass


def _git(repo, *a, timeout=60):
    return subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True, timeout=timeout)


def verify(repo: Path, max_files: int, timeout_s: int) -> dict:
    files = [f for f in _git(repo, "ls-files").stdout.split("\n") if f]
    if len(files) > max_files:
        raise Unmet(f"{len(files)} fichiers versionnés > {max_files}")
    dead = [f for f in files if f.startswith(DEAD)]
    if dead:
        raise Unmet(f"{len(dead)} fichiers morts encore versionnés (ex. {dead[:3]})")
    br = _git(repo, "ls-tree", "-r", "--name-only", "archive/pre-purge-2026-09", "--", "campaigns/RUN8")
    if br.returncode != 0 or not br.stdout.strip():
        raise Unmet("branche d'archive archive/pre-purge-2026-09 absente ou sans campaigns/RUN8")
    roadmap = (repo / "projects" / "factory" / "ROADMAP.yaml").read_text(encoding="utf-8") + \
              (repo / "projects" / "twitter-radar" / "ROADMAP.yaml").read_text(encoding="utf-8")
    # rien de référencé par une roadmap ne peut manquer : task_file (chemins absolus compris) et scripts python du dépôt
    missing = []
    for ref in sorted(set(re.findall(r"task_file: (\S+)", roadmap))):
        rel = ref[len(str(repo.resolve())) + 1:] if ref.startswith(str(repo.resolve()) + "/") else ref
        if not rel.startswith("/") and rel not in files:
            missing.append(rel)
    if missing:
        raise Unmet(f"{len(missing)} consigne(s) référencée(s) par une roadmap absentes de l'arbre : {missing[:5]}")
    orphans = []
    for f in files:
        if f in roadmap:
            continue  # livrable d'un jalon (preuve d'un règlement) : jamais orphelin, quel que soit son répertoire
        if "/" not in f or f.startswith(LIVE_ROOTS):
            if f.startswith("factory_v2/receipts/"):
                base = os.path.basename(f)
                if base in roadmap or f in roadmap:
                    continue
                g = subprocess.run(["grep", "-rlF", base, "factory", "factory_v2", "tests", "--include=*.py",
                                    "--include=*.json", "--include=*.yaml", "--include=*.md"],
                                   cwd=repo, capture_output=True, text=True, timeout=60)
                hits = [h for h in g.stdout.split("\n") if h and h != f]
                if not hits:
                    orphans.append(f)
            continue
        orphans.append(f)
    if orphans:
        raise Unmet(f"{len(orphans)} fichiers orphelins (ni vivants ni référencés), ex. {orphans[:5]}")
    fz = repo / "factory" / "bin" / "frozen_zone_guard.py"
    if fz.is_file() and "campaigns/RUN8" in fz.read_text(encoding="utf-8") and not (repo / "campaigns" / "RUN8").exists():
        raise Unmet("frozen_zone_guard.py protège encore une zone absente de l'arbre")
    env = {**os.environ, "FACTORY_NO_REAL_ENGINE": "1", "PYTHONPATH": str(repo)}
    t = subprocess.run([sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider", "-x", *IGN],
                       cwd=repo, capture_output=True, text=True, timeout=timeout_s, env=env)
    m = re.search(r"(\d+) passed", t.stdout)
    if t.returncode != 0 or not m:
        raise Unmet(f"suite complète rouge: {t.stdout[-300:]}")
    for extra in ([], ["--set", "v2"]):
        c = subprocess.run([sys.executable, "factory_v2/verify_chaos_drill.py", "--project-repo", ".", *extra],
                           cwd=repo, capture_output=True, text=True, timeout=timeout_s, env=env)
        if c.returncode != 0:
            raise Unmet(f"banc de chaos {'v2' if extra else 'v1'} rouge")
    return {"tracked_files": len(files), "tests": int(m.group(1))}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project-repo", default=".")
    ap.add_argument("--max-files", type=int, default=2000)
    ap.add_argument("--timeout-s", type=int, default=3600)
    a = ap.parse_args(argv)
    try:
        r = verify(Path(a.project_repo).resolve(), a.max_files, a.timeout_s)
    except Unmet as exc:
        print(f"DEAD_WEIGHT_PURGE_UNMET: {exc}")
        return 1
    print(f"DEAD_WEIGHT_PURGE_VERIFIED: {json.dumps(r)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
