#!/usr/bin/env python3
"""Gaspillage de quota moteur — minutes-moteur des essais qui n'ont PAS abouti.

Source : `projects/*/evidence/attempt_accounts.jsonl` (lignes TERMINAL). Un essai
« perdu » = essai terminé sans mission_pass qui a consommé du quota modèle.
Cause = exit_class (+ reconciled_reason quand il existe : base périmée,
orphelin tué, coupure quota…).

    python3 factory_v2/quota_waste.py --since-hours 3         # texte
    python3 factory_v2/quota_waste.py --since-hours 24 --json # machine

Rc 0 toujours (rapport) ; --alert-min N → rc 2 si les minutes perdues dépassent N
(le contrôle 20 min l'utilise : > 60 min perdues en 3 h = incident, pas un constat).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from factory_v2.resource_accounting import fold, read_rows

REPO_ROOT = Path(__file__).resolve().parent.parent


def _ts(s) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def collect(project_dirs, since: datetime) -> dict:
    lost = defaultdict(float)          # (engine, cause) -> minutes
    per_engine = defaultdict(float)
    ok_engine = defaultdict(float)
    missions = defaultdict(float)
    n_lost = 0
    for pd in project_dirs:
        f = Path(pd) / "evidence" / "attempt_accounts.jsonl"
        if not f.is_file():
            continue
        # attempt_accounts is an append-only projection ledger.  Fold first so
        # an attribution recovered after a crash replaces the earlier UNKNOWN
        # OPEN/TERMINAL fields instead of being counted as another attempt.
        for r in fold(read_rows(f)):
            if r.get("phase") != "TERMINAL":
                continue
            end = _ts(r.get("ended_at") or r.get("started_at"))
            if end is None or end < since:
                continue
            minutes = float(r.get("duration_s") or 0) / 60.0
            eng = str(r.get("engine") or "?")
            if r.get("mission_pass"):
                ok_engine[eng] += minutes
                continue
            if r.get("consumes_model_quota") is False or eng == "local":
                continue
            cause = str(r.get("exit_class") or "?")
            rr = str(r.get("reconciled_reason") or r.get("refusal_reason") or "").strip()
            if rr:
                cause += " · " + rr[:60]
            lost[(eng, cause)] += minutes
            per_engine[eng] += minutes
            missions[str(r.get("mission") or "?")] += minutes
            n_lost += 1
    return {
        "since": since.isoformat(),
        "lost_minutes_total": round(sum(per_engine.values()), 1),
        "lost_minutes_by_engine": {k: round(v, 1) for k, v in sorted(per_engine.items(), key=lambda kv: -kv[1])},
        "lost_minutes_by_cause": [{"engine": e, "cause": c, "minutes": round(m, 1)}
                                  for (e, c), m in sorted(lost.items(), key=lambda kv: -kv[1])],
        "lost_minutes_by_mission": {k: round(v, 1) for k, v in sorted(missions.items(), key=lambda kv: -kv[1])[:8]},
        "useful_minutes_by_engine": {k: round(v, 1) for k, v in ok_engine.items()},
        "lost_attempts": n_lost,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--since-hours", type=float, default=24.0)
    ap.add_argument("--projects", nargs="*", default=None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--alert-min", type=float, default=None)
    a = ap.parse_args(argv)
    dirs = [Path(p) for p in a.projects] if a.projects else sorted((REPO_ROOT / "projects").glob("*/"))
    rep = collect(dirs, datetime.now(timezone.utc) - timedelta(hours=a.since_hours))
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
    else:
        tot = rep["lost_minutes_total"]
        useful = sum(rep["useful_minutes_by_engine"].values())
        print(f"GASPILLAGE {a.since_hours:g} h : {tot:.0f} min perdues ({rep['lost_attempts']} essais) "
              f"vs {useful:.0f} min utiles — par moteur {rep['lost_minutes_by_engine']}")
        for row in rep["lost_minutes_by_cause"][:6]:
            print(f"  {row['minutes']:6.0f} min  {row['engine']:7s} {row['cause']}")
    if a.alert_min is not None and rep["lost_minutes_total"] > a.alert_min:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
