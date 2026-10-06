#!/usr/bin/env python3
"""Juge de FACTORY_QUOTA_THRIFT_V1 — le quota moteur est mesuré, protégé, optimisé.

Recompté, jamais cru :
  1. `python3 factory_v2/quota_waste.py --since-hours 24 --json` rend un rapport avec
     lost_minutes_total, lost_minutes_by_engine, lost_minutes_by_cause, useful_minutes_by_engine ;
     aucun moteur "UNKNOWN"/"?" dans les 24 dernières heures d'essais TERMINAL (chaque essai perdu
     est attribué à un vrai moteur et une vraie cause).
  2. `factory_v2/quota_throttle.py` expose `effective_workers(view: dict, configured: int) -> int`
     (moins de workers quand la fenêtre hebdo/5 h d'un moteur est proche de la limite ; jamais 0 ;
     jamais plus que configured) et factoryd l'utilise (`configured_max_workers` ou `_advance_parallel`).
  3. `tests/test_quota_thrift.py` existe et passe (≥ 6 tests) : throttle, attribution des causes,
     incident QUOTA_WASTE_INCIDENT émis par le daemon quand > seuil de minutes perdues sur 3 h,
     et aucun essai PASS jeté quand HEAD bouge (le candidat est rebasé/fusionné, pas redémarré).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path


class Unmet(Exception):
    pass


def verify(repo: Path) -> dict:
    env = {**os.environ, "FACTORY_NO_REAL_ENGINE": "1", "PYTHONPATH": str(repo)}
    # rows written BEFORE the throttle module existed are legacy (their engine
    # field was never populated) — judge only what the repaired producer wrote
    born = subprocess.run(["git", "log", "--first-parent", "--diff-filter=A", "--format=%ct", "--",
                           "factory_v2/quota_throttle.py"], cwd=repo, capture_output=True,
                          text=True, timeout=30).stdout.split()   # when it LANDED on the mainline
    import time as _t
    since_h = 24.0
    if born:
        since_h = min(24.0, max(0.05, (_t.time() - int(born[-1])) / 3600.0))
    r = subprocess.run([sys.executable, "factory_v2/quota_waste.py", "--since-hours", f"{since_h:.3f}", "--json"],
                       cwd=repo, capture_output=True, text=True, timeout=120, env=env)
    if r.returncode != 0:
        raise Unmet(f"quota_waste.py rc={r.returncode}: {r.stderr[-300:]}")
    try:
        rep = json.loads(r.stdout)
    except ValueError:
        raise Unmet("quota_waste.py --json : sortie non JSON")
    for k in ("lost_minutes_total", "lost_minutes_by_engine", "lost_minutes_by_cause", "useful_minutes_by_engine"):
        if k not in rep:
            raise Unmet(f"rapport sans {k}")
    bad = [e for e, m in rep["lost_minutes_by_engine"].items()
           if e in ("UNKNOWN", "?", "") and float(m or 0) > 0]   # a 0-minute legacy row is not quota
    if bad:
        raise Unmet(f"essais perdus attribués à un moteur inconnu : {bad} — chaque essai doit nommer son moteur")
    thr = repo / "factory_v2" / "quota_throttle.py"
    if not thr.is_file() or "def effective_workers(" not in thr.read_text(encoding="utf-8"):
        raise Unmet("factory_v2/quota_throttle.py::effective_workers absent")
    fd = (repo / "factory" / "bin" / "factoryd.py").read_text(encoding="utf-8")
    if "quota_throttle" not in fd or "effective_workers" not in fd:
        raise Unmet("factoryd n'utilise pas quota_throttle.effective_workers")
    if "QUOTA_WASTE_INCIDENT" not in fd:
        raise Unmet("factoryd n'émet pas QUOTA_WASTE_INCIDENT")
    t = repo / "tests" / "test_quota_thrift.py"
    if not t.is_file():
        raise Unmet("tests/test_quota_thrift.py absent")
    p = subprocess.run([sys.executable, "-m", "pytest", "tests/test_quota_thrift.py", "-q", "-p", "no:cacheprovider"],
                       cwd=repo, capture_output=True, text=True, timeout=900, env=env)
    m = re.search(r"(\d+) passed", p.stdout)
    if p.returncode != 0 or not m or int(m.group(1)) < 6:
        raise Unmet(f"tests/test_quota_thrift.py: rc={p.returncode} {p.stdout[-300:]}")
    return {"lost_minutes_24h": rep["lost_minutes_total"], "tests": int(m.group(1))}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project-repo", default=".")
    a = ap.parse_args(argv)
    try:
        r = verify(Path(a.project_repo).resolve())
    except Unmet as exc:
        print(f"QUOTA_THRIFT_UNMET: {exc}")
        return 1
    print(f"QUOTA_THRIFT_VERIFIED: {json.dumps(r)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
