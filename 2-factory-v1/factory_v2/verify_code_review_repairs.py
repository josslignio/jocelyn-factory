#!/usr/bin/env python3
"""Juge des réparations issues de la relecture complète du code.

Entrée : `factory/config/code_review_repair_lot1_ids.json` (ids à traiter) et
`factory_v2/audit/CODE_REVIEW_2026-09/REPAIRS.json` :
  [{"id": "L1-003", "status": "FIXED|WONTFIX", "commit": "<40 hex>",
    "test": "tests/test_x.py::test_y", "note": "..."}, ...]

Recompté : chaque id attendu présent une fois ; FIXED → `commit` est un ancêtre
de HEAD, `test` désigne un test existant (fichier::fonction) qui PASSE quand on
l'exécute ; WONTFIX → `note` ≥ 40 caractères ET `test` (le test qui démontre le
faux positif) passe ; ≥ 80 % des ids FIXED.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

HEX40 = re.compile(r"^[0-9a-f]{40}$")


class Unmet(Exception):
    pass


def _ancestor(repo: Path, sha: str) -> bool:
    return subprocess.run(["git", "merge-base", "--is-ancestor", sha, "HEAD"], cwd=repo,
                          capture_output=True).returncode == 0


def verify(repo: Path, ids_file: str, repairs_file: str, min_fixed_ratio: float = 0.8) -> dict:
    ids = json.loads((repo / ids_file).read_text(encoding="utf-8"))
    rp = repo / repairs_file
    if not rp.is_file():
        raise Unmet(f"{repairs_file} absent")
    rows = json.loads(rp.read_text(encoding="utf-8"))
    by = {}
    for r in rows:
        if r.get("id") in by:
            raise Unmet(f"{r.get('id')} en double")
        by[str(r.get("id"))] = r
    missing = [i for i in ids if i not in by]
    if missing:
        raise Unmet(f"ids sans ligne REPAIRS: {missing[:8]}")
    tests_to_run = []
    fixed = 0
    for i in ids:
        r = by[i]
        st = r.get("status")
        t = str(r.get("test") or "")
        if "::" not in t or not (repo / t.split("::")[0]).is_file():
            raise Unmet(f"{i}: test {t!r} introuvable")
        if st == "FIXED":
            fixed += 1
            if not HEX40.match(str(r.get("commit") or "")) or not _ancestor(repo, r["commit"]):
                raise Unmet(f"{i}: commit absent ou pas un ancêtre de HEAD")
        elif st == "WONTFIX":
            if len(str(r.get("note") or "").strip()) < 40:
                raise Unmet(f"{i}: WONTFIX sans justification (≥ 40 caractères)")
        else:
            raise Unmet(f"{i}: status {st!r} hors vocabulaire")
        tests_to_run.append(t)
    if fixed < min_fixed_ratio * len(ids):
        raise Unmet(f"{fixed}/{len(ids)} FIXED < {int(min_fixed_ratio*100)} %")
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *sorted(set(tests_to_run))],
                       cwd=repo, capture_output=True, text=True, timeout=1800,
                       env={**__import__("os").environ, "FACTORY_NO_REAL_ENGINE": "1"})
    if r.returncode != 0:
        raise Unmet(f"tests de réparation rouges: {r.stdout[-400:]}")
    return {"ids": len(ids), "fixed": fixed, "tests": len(set(tests_to_run))}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project-repo", default=".")
    ap.add_argument("--ids-file", default="factory/config/code_review_repair_lot1_ids.json")
    ap.add_argument("--repairs-file", default="factory_v2/audit/CODE_REVIEW_2026-09/REPAIRS.json")
    ap.add_argument("--categories", default="")      # informatif (le lot est fixé par --ids-file)
    ap.add_argument("--bug-high-effort", default="")
    a = ap.parse_args(argv)
    try:
        r = verify(Path(a.project_repo).resolve(), a.ids_file, a.repairs_file)
    except Unmet as exc:
        print(f"CODE_REVIEW_REPAIRS_UNMET: {exc}")
        return 1
    print(f"CODE_REVIEW_REPAIRS_VERIFIED: {json.dumps(r)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
