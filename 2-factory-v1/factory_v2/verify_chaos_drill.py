#!/usr/bin/env python3
"""Juge mécanique du banc de chaos (FACTORY_CHAOS_DRILL_V1).

Le banc (`factory_v2/chaos_drill.py`, construit par la mission) fait tourner
`factory_v2.runtime.advance()` sur un projet JETABLE hermétique
(FACTORY_NO_REAL_ENGINE=1, exécuteurs stubs) et injecte, une par une, les pannes
réellement subies du 12 au 19/09. Ce juge RE-EXÉCUTE le banc lui-même (jamais
un reçu écrit d'avance), puis recompte :

  python3 -m factory_v2.chaos_drill --all --out <fichier.json>   (rc 0 attendu)

Reçu attendu (schema "chaos-drill-receipt/v1") :
  {"scenarios": [{"id": "<id de SCENARIOS>", "injected": true,
                  "recovered": true|false, "recovery_cycles": <int>,
                  "recovery_s": <float>, "human_interventions": 0,
                  "evidence": "<événement(s) d'état observés, ≥ 20 caractères>"}, ...],
   "run_at": "...", "control_sha": "<40 hex>"}

Règles : les 12 scénarios présents exactement une fois ; chaque scénario
`injected` et `recovered`, `recovery_s` < RECOVERY_BUDGET_S (900 s = « < 15 min
sans intervention »), `human_interventions == 0` ; `control_sha` == HEAD ;
`tests/test_chaos_drill.py` existe et passe (≥ 12 tests, un par scénario).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

RECOVERY_BUDGET_S = 900.0

#: les pannes réellement subies (12 au 19/09) — une injection chacune
SCENARIOS = (
    "QUOTA_CUT_MID_BUILD",          # session/usage limit pendant un build → bascule moteur, pas d'échec
    "ENGINE_KILLED_BY_SIGNAL",      # kill -15 du moteur (balayage, restart) → interruption reprise
    "RELOAD_DURING_BUILD",          # code daemon changé pendant un build → reload après, jamais pendant
    "REVIEW_BLOCK",                 # relecteur BLOCK → reconstruction réelle, bornée, puis règlement
    "STALE_CONTRACT",               # roadmap modifiée → contrat regelé sans perdre les PASS compatibles
    "ROSTER_IMPOSSIBLE",            # constructeur dans le roster, min > restants → roster auto-dégradé
    "DIRTY_WORKTREE_MERGE",         # état sale (evidence/receipts) au moment de la fusion → fusion quand même
    "ENGINE_REFUSAL",               # le moteur refuse la tâche (filtre) → reformulation/rotation, pas de boucle
    "BUDGET_EXHAUSTED",             # plafond de lancements atteint → parking borné, pas de poll infini
    "MISSING_DELIVERABLE",          # candidat sans livrable → reconstruction, jamais ACCEPTED
    "DAEMON_CRASH_MID_MISSION",     # daemon tué pendant une mission → reprise sans double lancement
    "IDLE_NO_PROGRESS",             # jalon immobile 30 min sans moteur → rotation vers un autre jalon
)
#: FACTORY_CHAOS_DRILL_V2 — the pannes of the night 19→20/09 (each one was a
#: manual redirection; the bench must make every one of them disappear)
SCENARIOS_V2 = (
    "DAEMON_LOOP_SILENT",           # loop iteration stalls > watchdog → exit 3, restart resumes, no double launch
    "ENGINE_BEYOND_BUDGET",         # engine alive past budget_s → tree killed, attempt interrupted, retried (no zombie)
    "RELOAD_RACE_FRESH_SUBMIT",     # RELOAD_NOW in the poll that just submitted an advance → armed, exit only on empty pool
    "IDLE_GUARD_NEVER_LAUNCHED",    # idle guard must never yield a milestone that was never launched
    "INHERITED_WORKTREE_META",      # worktree carrying another mission's meta → recovered, no archive-failure loop
    "STALE_QUARANTINE_AFTER_FIX",   # quarantine whose gate is now green → lifted by the control plane alone
    "HUNG_TEST_SUBPROCESS",         # test_cmd child at 0 % CPU past timeout → process tree killed, clean FAIL, not hours
    "PARKED_WITHOUT_MILESTONE",     # parked mission lacking parked_milestone → derived from key, never reselected
)
SETS = {"v1": SCENARIOS, "v2": SCENARIOS_V2}
HEX40 = re.compile(r"^[0-9a-f]{40}$")


class Unmet(Exception):
    pass


def _head(repo: Path) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
                          text=True, timeout=10).stdout.strip()


def verify(repo: Path, timeout_s: int = 3600, scenario_set: str = "v1") -> dict:
    scenarios = SETS[scenario_set]
    drill = repo / "factory_v2" / "chaos_drill.py"
    if not drill.is_file():
        raise Unmet("factory_v2/chaos_drill.py absent")
    tests_rel = "tests/test_chaos_drill.py" if scenario_set == "v1" else f"tests/test_chaos_drill_{scenario_set}.py"
    tests = repo / tests_rel
    if not tests.is_file():
        raise Unmet(f"{tests_rel} absent")
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "receipt.json"
        env = {**os.environ, "FACTORY_NO_REAL_ENGINE": "1", "PYTHONSAFEPATH": "1",
               "PYTHONPATH": str(repo)}
        cmd = [sys.executable, "-m", "factory_v2.chaos_drill", "--all", "--out", str(out)]
        if scenario_set != "v1":
            cmd += ["--set", scenario_set]        # V1 CLI untouched: --all alone = the 12 pinned scenarios
        r = subprocess.run(cmd, cwd=repo, capture_output=True, text=True,
                           timeout=timeout_s, env=env)
        if r.returncode != 0:
            raise Unmet(f"chaos_drill --all rc={r.returncode}: {(r.stderr or r.stdout)[-600:]}")
        if not out.is_file():
            raise Unmet("le banc n'a pas écrit son reçu")
        d = json.loads(out.read_text(encoding="utf-8"))
    if d.get("schema") != "chaos-drill-receipt/v1":
        raise Unmet("schema du reçu incorrect")
    if d.get("control_sha") != _head(repo):
        raise Unmet("control_sha du reçu ≠ HEAD")
    seen = {}
    for s in d.get("scenarios") or []:
        sid = str(s.get("id"))
        if sid not in scenarios:
            raise Unmet(f"scénario inconnu {sid!r}")
        if sid in seen:
            raise Unmet(f"scénario {sid} en double")
        seen[sid] = s
    missing = [s for s in scenarios if s not in seen]
    if missing:
        raise Unmet(f"scénarios manquants: {missing}")
    failed = []
    for sid, s in seen.items():
        try:
            rs = float(s.get("recovery_s"))
        except (TypeError, ValueError):
            failed.append(f"{sid}: recovery_s non numérique"); continue
        if not s.get("injected"):
            failed.append(f"{sid}: non injecté")
        elif not s.get("recovered"):
            failed.append(f"{sid}: NON résorbé")
        elif rs >= RECOVERY_BUDGET_S:
            failed.append(f"{sid}: résorbé en {rs:.0f}s ≥ {RECOVERY_BUDGET_S:.0f}s")
        if int(s.get("human_interventions") or 0) != 0:
            failed.append(f"{sid}: intervention humaine")
        if len(str(s.get("evidence") or "").strip()) < 20:
            failed.append(f"{sid}: evidence < 20 caractères")
    if failed:
        raise Unmet("; ".join(failed))
    t = subprocess.run([sys.executable, "-m", "pytest", tests_rel, "-q",
                        "-p", "no:cacheprovider"], cwd=repo, capture_output=True, text=True,
                       timeout=timeout_s, env={**os.environ, "FACTORY_NO_REAL_ENGINE": "1"})
    m = re.search(r"(\d+) passed", t.stdout)
    if t.returncode != 0 or not m or int(m.group(1)) < len(scenarios):
        raise Unmet(f"{tests_rel}: rc={t.returncode} {t.stdout[-300:]}")
    return {"scenarios": len(seen), "max_recovery_s": max(float(s["recovery_s"]) for s in seen.values()),
            "tests": int(m.group(1))}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project-repo", default=".")
    ap.add_argument("--timeout-s", type=int, default=3600)
    ap.add_argument("--set", default="v1", choices=sorted(SETS),
                    help="v1 = the 12 pinned scenarios (FACTORY_CHAOS_DRILL_V1); v2 = the night 19→20/09 set")
    a = ap.parse_args(argv)
    try:
        r = verify(Path(a.project_repo).resolve(), a.timeout_s, a.set)
    except Unmet as exc:
        print(f"CHAOS_DRILL_UNMET: {exc}")
        return 1
    print(f"CHAOS_DRILL_VERIFIED: {json.dumps(r)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
