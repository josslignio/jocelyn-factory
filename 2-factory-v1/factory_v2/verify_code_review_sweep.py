#!/usr/bin/env python3
"""Juge mécanique d'un lot de relecture complète du code (FACTORY_CODE_REVIEW_SWEEP).

Un lot = une mission Codex (astra) qui lit CHAQUE fichier du lot et écrit
`factory_v2/audit/CODE_REVIEW_2026-09/<lot>.json` :

  {"schema": "code-review-lot/v1", "lot": "<lot>", "engine": "codex",
   "reviewed_sha": "<40 hex>",
   "files_reviewed": [{"path": "...", "lines": <int>, "verdict": "CLEAN|FINDINGS",
                       "summary": "<≥ 20 caractères>"}, ...],
   "findings": [{"id": "L1-001", "file": "...", "line": <int>,
                 "severity": "CRITICAL|HIGH|MEDIUM|LOW",
                 "category": "BUG|DEAD_CODE|DUPLICATE|RACE|ERROR_HANDLING|PERF|COMPLEXITY|TEST_GAP|DOC_DRIFT",
                 "evidence": "<citation textuelle ≥ 20 caractères présente à ±3 lignes>",
                 "why": "...", "recommendation": "...",
                 "fix_effort": "S|M|L"}, ...]}

Recompté ici, jamais cru :
  - `files_reviewed` == exactement la liste du lot (factory/config/code_review_lots_2026-09.json),
    chaque `lines` à ±2 du `wc -l` réel, chaque summary ≥ 20 caractères ;
  - chaque constat : fichier du lot, ligne ≤ longueur, citation présente dans le
    fichier à ±3 lignes de `line`, sévérité/catégorie/effort du vocabulaire,
    `why` et `recommendation` non vides, ids uniques ;
  - ≥ `--min-findings` constats (défaut 1 : un lot de 9 000 lignes sans un seul
    constat n'a pas été lu) ; un fichier `FINDINGS` doit avoir ≥ 1 constat.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SEV = ("CRITICAL", "HIGH", "MEDIUM", "LOW")
CAT = ("BUG", "DEAD_CODE", "DUPLICATE", "RACE", "ERROR_HANDLING", "PERF", "SECURITY",
       "COMPLEXITY", "TEST_GAP", "DOC_DRIFT")
EFF = ("S", "M", "L")
HEX40 = re.compile(r"^[0-9a-f]{40}$")


class Unmet(Exception):
    pass


def _wc(p: Path) -> int:
    return sum(1 for _ in p.open("rb"))


def verify(repo: Path, lot: str, lots_file: str, out_dir: str, min_findings: int) -> dict:
    lots = json.loads((repo / lots_file).read_text(encoding="utf-8"))
    if lot not in lots:
        raise Unmet(f"lot {lot!r} inconnu de {lots_file}")
    expected = sorted(lots[lot])
    p = repo / out_dir / f"{lot}.json"
    if not p.is_file():
        raise Unmet(f"{p.relative_to(repo)} absent")
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise Unmet(f"JSON invalide: {exc}")
    if d.get("schema") != "code-review-lot/v1" or d.get("lot") != lot:
        raise Unmet("schema/lot incorrects")
    if not HEX40.match(str(d.get("reviewed_sha") or "")):
        raise Unmet("reviewed_sha doit être un SHA 40 hex")
    fr = d.get("files_reviewed") or []
    got = sorted(str(f.get("path") or "") for f in fr if isinstance(f, dict))
    if got != expected:
        missing = sorted(set(expected) - set(got)); extra = sorted(set(got) - set(expected))
        raise Unmet(f"couverture: manquants={missing[:6]} en trop={extra[:6]}")
    verdict_by_file = {}
    for f in fr:
        path = repo / str(f.get("path"))
        real = _wc(path) if path.is_file() else -1
        try:
            lines = int(f.get("lines"))
        except (TypeError, ValueError):
            raise Unmet(f"{f.get('path')}: lines non numérique")
        if real < 0 or abs(lines - real) > 2:
            raise Unmet(f"{f.get('path')}: lines={lines} réel={real}")
        if f.get("verdict") not in ("CLEAN", "FINDINGS"):
            raise Unmet(f"{f.get('path')}: verdict hors vocabulaire")
        if len(str(f.get("summary") or "").strip()) < 20:
            raise Unmet(f"{f.get('path')}: summary < 20 caractères")
        verdict_by_file[str(f.get("path"))] = f.get("verdict")
    findings = d.get("findings") or []
    ids = set()
    per_file = {}
    for x in findings:
        if not isinstance(x, dict):
            raise Unmet("constat non objet")
        fid = str(x.get("id") or "")
        if not fid or fid in ids:
            raise Unmet(f"id de constat absent ou dupliqué: {fid!r}")
        ids.add(fid)
        fpath = str(x.get("file") or "")
        if fpath not in verdict_by_file:
            raise Unmet(f"{fid}: fichier hors lot {fpath!r}")
        path = repo / fpath
        try:
            line = int(x.get("line"))
        except (TypeError, ValueError):
            raise Unmet(f"{fid}: line non numérique")
        src = path.read_text(encoding="utf-8", errors="replace").splitlines()
        if not (1 <= line <= len(src)):
            raise Unmet(f"{fid}: line {line} hors fichier ({len(src)} lignes)")
        ev = str(x.get("evidence") or "").strip()
        if len(ev) < 20:
            raise Unmet(f"{fid}: evidence < 20 caractères")
        window = "\n".join(src[max(0, line - 4): line + 3])
        if ev not in window and " ".join(ev.split()) not in " ".join(window.split()):
            raise Unmet(f"{fid}: citation introuvable à ±3 lignes de {fpath}:{line}")
        if x.get("severity") not in SEV or x.get("category") not in CAT or x.get("fix_effort") not in EFF:
            raise Unmet(f"{fid}: severity/category/fix_effort hors vocabulaire")
        if not str(x.get("why") or "").strip() or not str(x.get("recommendation") or "").strip():
            raise Unmet(f"{fid}: why/recommendation vides")
        per_file[fpath] = per_file.get(fpath, 0) + 1
    for fpath, v in verdict_by_file.items():
        if v == "FINDINGS" and per_file.get(fpath, 0) == 0:
            raise Unmet(f"{fpath}: verdict FINDINGS sans constat")
        if v == "CLEAN" and per_file.get(fpath, 0) > 0:
            raise Unmet(f"{fpath}: verdict CLEAN mais {per_file[fpath]} constat(s)")
    if len(findings) < min_findings:
        raise Unmet(f"{len(findings)} constats < {min_findings}")
    return {"files": len(fr), "findings": len(findings),
            "by_severity": {s: sum(1 for x in findings if x.get("severity") == s) for s in SEV}}


def verify_synthesis(repo: Path, lots_file: str, out_dir: str, min_items: int = 10) -> dict:
    lots = json.loads((repo / lots_file).read_text(encoding="utf-8"))
    ids = set()
    for lot in lots:
        p = repo / out_dir / f"{lot}.json"
        if not p.is_file():
            raise Unmet(f"lot {lot} sans fichier de relecture")
        for x in (json.loads(p.read_text(encoding="utf-8")).get("findings") or []):
            ids.add(str(x.get("id")))
    sp = repo / out_dir / "SYNTHESIS.json"
    if not sp.is_file():
        raise Unmet("SYNTHESIS.json absent")
    d = json.loads(sp.read_text(encoding="utf-8"))
    if d.get("schema") != "code-review-synthesis/v1":
        raise Unmet("schema synthèse incorrect")
    top = d.get("top") or []
    if len(top) < min_items:
        raise Unmet(f"{len(top)} items < {min_items}")
    ranks = sorted(int(t.get("rank", 0)) for t in top)
    if ranks != list(range(1, len(top) + 1)):
        raise Unmet("rangs non contigus")
    for t in top:
        fids = t.get("finding_ids") or []
        if not fids or any(f not in ids for f in fids):
            raise Unmet(f"item {t.get('rank')}: finding_ids absents des lots")
        mp = t.get("milestone_proposal") or {}
        if not all(str(mp.get(k) or "").strip() for k in ("id", "goal", "judge")) or not mp.get("deliverables"):
            raise Unmet(f"item {t.get('rank')}: milestone_proposal incomplète")
    md = repo / out_dir / "SYNTHESIS.md"
    if not md.is_file() or len([l for l in md.read_text(encoding="utf-8").splitlines() if l.strip()]) < min_items:
        raise Unmet("SYNTHESIS.md absent ou trop court")
    return {"items": len(top), "finding_ids": len(ids)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("lot", nargs="?", default="")
    ap.add_argument("--synthesis", action="store_true")
    ap.add_argument("--project-repo", default=".")
    ap.add_argument("--lots-file", default="factory/config/code_review_lots_2026-09.json")
    ap.add_argument("--out-dir", default="factory_v2/audit/CODE_REVIEW_2026-09")
    ap.add_argument("--min-findings", type=int, default=1)
    a = ap.parse_args(argv)
    try:
        if a.synthesis:
            r = verify_synthesis(Path(a.project_repo).resolve(), a.lots_file, a.out_dir)
            print(f"CODE_REVIEW_SYNTHESIS_VERIFIED: {json.dumps(r)}")
            return 0
        if not a.lot:
            raise Unmet("lot manquant")
        r = verify(Path(a.project_repo).resolve(), a.lot, a.lots_file, a.out_dir, a.min_findings)
    except Unmet as exc:
        print(f"CODE_REVIEW_{a.lot or 'SYNTHESIS'}_UNMET: {exc}")
        return 1
    print(f"CODE_REVIEW_{a.lot}_VERIFIED: {json.dumps(r)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
