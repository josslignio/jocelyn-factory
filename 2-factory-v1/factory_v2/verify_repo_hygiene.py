#!/usr/bin/env python3
"""Juge de FACTORY_REPO_HYGIENE_V1 — un dépôt propre : le code d'un côté, la comptabilité de l'autre.

Ordre propriétaire 21/09 : « tout clean, plus le bordel ». Recompté, jamais cru :
  1. AUCUN checkpoint de mission sur la branche dorée : PLAN.md, REPORT.json, .factory_worktree_meta.json ne sont
     pas dans `git ls-files` du dépôt de contrôle. ATTENTION (essai 1, 22/09) : ils restent COMMITTÉS dans la branche
     candidate (le dispatcher lit REPORT.json AU SHA du candidat, jamais la copie disque — fail-closed) ; c'est la
     FUSION qui ne les fait pas entrer sur la branche dorée (`_merge_candidate` les retire du résultat).
  2. La comptabilité runtime n'est pas versionnée avec le code : `projects/*/evidence/`, `evidence/`,
     `factory_v2/receipts/recovery_allocations/`, `factory_v2/receipts/validation_jobs/`,
     `factory_v2/receipts/weekly_observations/*.json` → 0 fichier suivi (les reçus de jalon nommés dans la roadmap
     restent versionnés : ce sont des livrables).
  3. Les tests et bancs nettoient derrière eux : après les deux bancs de chaos (hermétiques), le dossier temporaire ne
     contient AUCUN nouveau répertoire créé par ces bancs (`chaos_*`, `factory_reload_canary*`) — les autres missions
     en parallèle (qui font tourner la suite complète) n'entrent pas dans le compte.
  4. Le daemon purge : `prune_stale_worktrees` retire les worktrees `.archived-*` de plus de 24 h ; ici, 0 worktree
     archivé de plus de 24 h sous /private/tmp/factory-worktrees.
  5. Garde d'espace disque : `factory_v2/disk_guard.py::ensure_free_gb(path, min_gb)` existe, est appelé avant
     `git worktree add` (dispatcher, promotion) et refuse avec un événement typé `DISK_SPACE_LOW`.
  6. Le dépôt ne porte plus d'archives mortes : 0 fichier suivi sous `**/archive/` hors `campaigns/RUN8/**` (zone gelée).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path


class Unmet(Exception):
    pass


def _ls(repo: Path) -> list[str]:
    return subprocess.run(["git", "ls-files"], cwd=repo, capture_output=True, text=True, timeout=60).stdout.split("\n")


def verify(repo: Path, timeout_s: int) -> dict:
    files = _ls(repo)
    # Dans un worktree de mission, le candidat DOIT committer ses checkpoints (le dispatcher lit
    # REPORT.json au SHA) : le point 1 ne se juge que sur la branche dorée (dépôt de contrôle).
    in_mission_worktree = "factory-worktrees" in str(repo) or (repo / ".factory_worktree_meta.json").is_file()
    if not in_mission_worktree:
        for f in ("PLAN.md", "REPORT.json", ".factory_worktree_meta.json"):
            if f in files:
                raise Unmet(f"{f} est suivi par git à la racine")
    book = [f for f in files if f.startswith(("projects/", "evidence/")) and "/evidence/" in f or f.startswith("evidence/")
            or f.startswith(("factory_v2/receipts/recovery_allocations/", "factory_v2/receipts/validation_jobs/"))
            or re.match(r"factory_v2/receipts/weekly_observations/.*\.json$", f)]
    if book:
        raise Unmet(f"{len(book)} fichiers de comptabilité runtime versionnés (ex. {book[:3]})")
    dead = [f for f in files if "/archive/" in f and not f.startswith("campaigns/RUN8/")]
    if dead:
        raise Unmet(f"{len(dead)} fichiers d'archive morte versionnés (ex. {dead[:3]})")
    dg = repo / "factory_v2" / "disk_guard.py"
    if not dg.is_file() or "def ensure_free_gb(" not in dg.read_text(encoding="utf-8"):
        raise Unmet("factory_v2/disk_guard.py::ensure_free_gb absent")
    src = (repo / "factory" / "bin" / "dispatcher_v3.py").read_text(encoding="utf-8") + \
          (repo / "factory" / "lib" / "worktree_manager.py").read_text(encoding="utf-8")
    if "ensure_free_gb" not in src or "DISK_SPACE_LOW" not in src:
        raise Unmet("la garde d'espace disque n'est pas appelée avant git worktree add")
    wt = Path("/private/tmp/factory-worktrees")
    if wt.is_dir():
        old = [p for p in wt.glob("*.archived-*") if time.time() - p.stat().st_mtime > 86400]
        if old:
            raise Unmet(f"{len(old)} worktrees archivés de plus de 24 h non purgés")
    tmp = Path(tempfile.gettempdir())
    before = {p.name for p in tmp.iterdir()}
    env = {**os.environ, "FACTORY_NO_REAL_ENGINE": "1", "PYTHONPATH": str(repo)}
    for extra in ([], ["--set", "v2"]):
        r = subprocess.run([sys.executable, "factory_v2/verify_chaos_drill.py", "--project-repo", ".", *extra],
                           cwd=repo, capture_output=True, text=True, timeout=timeout_s, env=env)
        if r.returncode != 0:
            raise Unmet(f"banc de chaos {'v2' if extra else 'v1'} rouge pendant le contrôle de fuite")
    # seulement ce que les DEUX bancs de chaos créent eux-mêmes : une mission voisine qui fait
    # tourner la suite complète en parallèle laisse d'autres préfixes (factory-quota-empty…)
    _pfx = ("chaos_drill_env", "chaos_", "factory_reload_canary")
    leaked = sorted(n for n in ({p.name for p in tmp.iterdir()} - before) if n.startswith(_pfx))
    if leaked:
        raise Unmet(f"{len(leaked)} répertoire(s) temporaire(s) laissés par les bancs (ex. {leaked[:3]})")
    return {"tracked_files": len(files), "tmp_leak": 0}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project-repo", default=".")
    ap.add_argument("--timeout-s", type=int, default=3600)
    a = ap.parse_args(argv)
    try:
        r = verify(Path(a.project_repo).resolve(), a.timeout_s)
    except Unmet as exc:
        print(f"REPO_HYGIENE_UNMET: {exc}")
        return 1
    print(f"REPO_HYGIENE_VERIFIED: {json.dumps(r)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
