#!/usr/bin/env python3
"""Juge de FACTORY_CORE_REVIEW_V1 — les cœurs ont été relus ligne à ligne, les boucles du 22/09 corrigées, le poids a baissé.

Recompté, jamais cru :
  1. factory_v2/audit/CORE_REVIEW_2026-09/{codex,glm}.json : 2 fichiers, engine distinct, >= --min-findings constats
     chacun ; chaque constat {file, line, kind, severity, evidence, fix} cite un fichier suivi et une ligne qui existe
     (au SHA relu : `reviewed_sha`), kind dans le vocabulaire ; >= 1 constat par boucle du 22/09 (7, ou infirmation explicite).
  2. SUMMARY.json : `base_sha` (SHA de départ) ; `fixes` = les 2 corrections indispensables (ONE_VALIDATION_PER_SHA,
     REVIEW_SINGLE_PATH_QUOTA_GUARD) chacune avec un test `tests/...::nom`
     qui est ROUGE dans un checkout isolé de base_sha (le test y est copié ; l'échec doit être une assertion, pas une
     erreur d'import/collecte) et VERT à HEAD ; `coverage` = chaque fichier .py de factory/ + factory_v2/ classé
     (used / dead / duplicate / replaceable / keep) — couverture complète, pas de quota de constats.
     Trois totaux publiés et recomptés par le juge : `loc_control` (tout factory/ + factory_v2/ à base_sha et à HEAD,
     HEAD < base), `loc_tests` (tests/), `loc_upstream` (dépendances/adaptations déclarées).
     BRICKS.md et CORE_SPEC.md (spécification du noyau minimal) présents et non vides.
  3. Suite complète (exclusions listées dans SUMMARY.json `test_exclusions` avec raison), banc de chaos v1 + v2 verts.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

KINDS = {"LOOP", "SILENT_FAILURE", "DEAD_CODE", "DUPLICATE", "RACE", "HOMEMADE_REPLACEABLE"}
# 22/09 20:45 — liste FERMÉE : seules les 2 réparations sans lesquelles la baseline n'est pas mesurable.
# Les 5 autres boucles du 22/09 partent au challenger sous forme de constats, pas de correctifs.
FIXES = ("REVIEW_SINGLE_PATH_QUOTA_GUARD",)  # 22/09 23:50 : ONE_VALIDATION_PER_SHA renvoyée au challenger (elle contredit le banc chaos v1 réglé, intouchable)
LOOPS_22_09 = ("GATE_REPLAYS_OLD_SHA", "HEAD_PROBE_SILENT", "GREEN_CANDIDATE_DISCARDED_ON_ROTATION",
               "QUARANTINE_NO_EXIT", "DIAGNOSE_WITHOUT_WRITE_SCOPE", "REVIEW_LEGACY_IGNORES_QUOTA",
               "PARK_IGNORES_OWNER_GRANT")
CORE = ("factory/bin/dispatcher_v3.py", "factory/bin/factoryd.py", "factory_v2/runtime.py",
        "factory_v2/authority_gate.py", "factory_v2/settlement_truth.py", "factory/lib/worktree_manager.py")


class Unmet(Exception):
    pass


def _loc(p: Path) -> int:
    return sum(1 for _ in p.open("rb")) if p.is_file() else 0


def _loc_control_at(repo: Path, tree: Path) -> int:
    files = subprocess.run(["git", "ls-files", "factory/*.py", "factory_v2/*.py"], cwd=tree,
                           capture_output=True, text=True).stdout.split()
    return sum(_loc(tree / f) for f in files)


def _line_count_at(repo: Path, sha: str, f: str) -> int:
    r = subprocess.run(["git", "show", f"{sha}:{f}"], cwd=repo, capture_output=True, text=True, timeout=30)
    return len(r.stdout.splitlines()) if r.returncode == 0 else -1


def _check_engine_file(repo: Path, d: Path, engine: str, min_findings: int) -> list:
    p = d / f"{engine}.json"
    if not p.is_file():
        raise Unmet(f"{p.relative_to(repo)} absent")
    data = json.loads(p.read_text(encoding="utf-8"))
    if data.get("engine") != engine:
        raise Unmet(f"{p.name}: engine={data.get('engine')!r} != {engine!r}")
    sha = str(data.get("reviewed_sha") or "")
    if not re.fullmatch(r"[0-9a-f]{7,40}", sha):
        raise Unmet(f"{p.name}: reviewed_sha manquant")
    findings = data.get("findings") or []
    if len(findings) < min_findings:
        raise Unmet(f"{p.name}: {len(findings)} constats < {min_findings}")
    touched = {f.get("file") for f in findings} | set(data.get("files_reviewed_clean") or [])
    miss_core = [c for c in CORE if c not in touched]
    if miss_core:
        raise Unmet(f"{p.name}: cœurs sans constat ni mention 'relu, rien' (files_reviewed_clean) : {miss_core}")
    tracked = set(subprocess.run(["git", "ls-files"], cwd=repo, capture_output=True, text=True).stdout.split())
    for i, f in enumerate(findings):
        for k in ("file", "line", "kind", "severity", "evidence", "fix"):
            if k not in f:
                raise Unmet(f"{p.name}#{i}: champ {k} manquant")
        if f["kind"] not in KINDS:
            raise Unmet(f"{p.name}#{i}: kind {f['kind']!r} hors vocabulaire")
        if f["file"] not in tracked:
            raise Unmet(f"{p.name}#{i}: fichier non suivi {f['file']}")
        n = _line_count_at(repo, sha, f["file"])
        if not (isinstance(f["line"], int) and 1 <= f["line"] <= n):
            raise Unmet(f"{p.name}#{i}: ligne {f['line']} hors de {f['file']} ({n} lignes à {sha[:9]})")
        if len(str(f["evidence"])) < 40:
            raise Unmet(f"{p.name}#{i}: evidence trop courte (scénario concret exigé)")
    covered = {str(f.get("loop_22_09")) for f in findings} | set(data.get("loops_22_09_refuted") or [])
    miss = [l for l in LOOPS_22_09 if l not in covered]
    if miss:
        raise Unmet(f"{p.name}: boucles du 22/09 ni constatées ni infirmées : {miss}")
    return findings


def verify(repo: Path, min_findings: int, timeout_s: int) -> dict:
    d = repo / "factory_v2" / "audit" / "CORE_REVIEW_2026-09"
    n = {e: len(_check_engine_file(repo, d, e, min_findings)) for e in ("codex", "glm")}
    s = d / "SUMMARY.json"
    if not s.is_file():
        raise Unmet("SUMMARY.json absent")
    summ = json.loads(s.read_text(encoding="utf-8"))
    base = str(summ.get("base_sha") or "")
    if not re.fullmatch(r"[0-9a-f]{7,40}", base):
        raise Unmet("SUMMARY.base_sha manquant")
    for f in ("BRICKS.md", "CORE_SPEC.md"):
        if len((d / f).read_text(encoding="utf-8").strip()) < 400 if (d / f).is_file() else True:
            raise Unmet(f"{f} absent ou vide")
    env = {**os.environ, "FACTORY_NO_REAL_ENGINE": "1", "PYTHONPATH": str(repo)}
    fixes = summ.get("fixes") or {}
    import tempfile
    with tempfile.TemporaryDirectory(prefix="core-review-base-") as tmp:
        wt = Path(tmp) / "base"
        r = subprocess.run(["git", "worktree", "add", "--detach", str(wt), base], cwd=repo, capture_output=True, text=True)
        if r.returncode != 0:
            raise Unmet(f"checkout isolé de base_sha impossible : {r.stderr[-200:]}")
        try:
            for k in FIXES:
                t = str((fixes.get(k) or {}).get("test") or "")
                if "::" not in t or not (repo / t.split("::")[0]).is_file():
                    raise Unmet(f"fixes.{k}.test manquant ou fichier absent (tests/x.py::nom)")
                tf = t.split("::")[0]
                (wt / tf).parent.mkdir(parents=True, exist_ok=True)
                (wt / tf).write_bytes((repo / tf).read_bytes())   # même test, deux environnements
                # -o addopts="" : le juge neutralise les options du dépôt (pytest.ini exige --reruns, incompatible
                # avec -p no:rerunfailures) — le candidat ne modifie jamais l'environnement d'examen
                red = subprocess.run([sys.executable, "-m", "pytest", t, "-q", "-p", "no:cacheprovider",
                                      "-p", "no:rerunfailures", "-o", "addopts="],
                                     cwd=wt, capture_output=True, text=True, timeout=timeout_s,
                                     env={**env, "PYTHONPATH": str(wt)})
                if red.returncode == 0:
                    raise Unmet(f"fixes.{k}: {t} est VERT à base_sha {base[:9]} — le défaut n'est pas reproduit")
                if not re.search(r"\b1 failed\b", red.stdout) or "AssertionError" not in red.stdout + red.stderr:
                    raise Unmet(f"fixes.{k}: rouge à base_sha mais pas par assertion (import/collecte/env) : {red.stdout[-200:]}")
                c = subprocess.run([sys.executable, "-m", "pytest", t, "-q", "-p", "no:cacheprovider",
                                    "-p", "no:rerunfailures", "-o", "addopts="],
                                   cwd=repo, capture_output=True, text=True, timeout=timeout_s, env=env)
                if c.returncode != 0 or not re.search(r"1 passed", c.stdout):
                    raise Unmet(f"fixes.{k}: {t} n'est pas vert à HEAD : {c.stdout[-200:]}")
            loc_base = _loc_control_at(repo, wt)
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=repo, capture_output=True)
    loc_head = _loc_control_at(repo, repo)
    lc = summ.get("loc_control") or {}
    if int(lc.get("base", -1)) != loc_base or int(lc.get("head", -1)) != loc_head:
        raise Unmet(f"loc_control déclaré {lc} ≠ recompte base={loc_base} head={loc_head}")
    if loc_head >= loc_base:
        raise Unmet(f"POIDS: contrôle {loc_head} lignes à HEAD >= {loc_base} à base_sha — la relecture doit retirer du code")
    for k in ("loc_tests", "loc_upstream"):
        if k not in summ:
            raise Unmet(f"SUMMARY.{k} manquant (trois totaux publiés)")
    cov = summ.get("coverage") or {}
    files = subprocess.run(["git", "ls-files", "factory/*.py", "factory_v2/*.py"], cwd=repo,
                           capture_output=True, text=True).stdout.split()
    miss = [f for f in files if str(cov.get(f, "")).lower() not in ("used", "dead", "duplicate", "replaceable", "keep")]
    if miss:
        raise Unmet(f"couverture incomplète : {len(miss)} fichiers non classés, ex. {miss[:5]}")
    excl = summ.get("test_exclusions")
    if not isinstance(excl, dict) or any(not str(v).strip() for v in excl.values()):
        raise Unmet("SUMMARY.test_exclusions manquant (fichier → raison) ; vide = aucune exclusion")
    ign = [f"--ignore={f}" for f in excl]
    sb, sa = loc_base, loc_head
    t = subprocess.run([sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider", "-x", *ign],
                       cwd=repo, capture_output=True, text=True, timeout=timeout_s, env=env)
    m = re.search(r"(\d+) passed", t.stdout)
    if t.returncode != 0 or not m:
        raise Unmet(f"suite complète rouge: {t.stdout[-400:]}")
    for extra in ([], ["--set", "v2"]):
        c = subprocess.run([sys.executable, "factory_v2/verify_chaos_drill.py", "--project-repo", ".", *extra],
                           cwd=repo, capture_output=True, text=True, timeout=timeout_s, env=env)
        if c.returncode != 0 or "CHAOS_DRILL_VERIFIED" not in c.stdout:
            raise Unmet(f"banc de chaos {'v2' if extra else 'v1'} rouge: {c.stdout[-300:]}")
    return {"findings": n, "loc_control_base": sb, "loc_control_head": sa, "tests": int(m.group(1))}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project-repo", default=".")
    ap.add_argument("--min-findings", type=int, default=1)
    ap.add_argument("--timeout-s", type=int, default=3000)
    a = ap.parse_args(argv)
    try:
        print("CORE_REVIEW_VERIFIED: " + json.dumps(verify(Path(a.project_repo).resolve(), a.min_findings, a.timeout_s)))
        return 0
    except Unmet as exc:
        print(f"CORE_REVIEW_UNMET: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
