#!/usr/bin/env python3
"""Juge mécanique d'une recherche approfondie multi-IA (DEEP_RESEARCH_*).

Une recherche approfondie = N moteurs DISTINCTS qui, chacun de leur côté et
avec accès au web, comparent la Factory (ou Twitter Radar) à ce qui existe
dehors — concurrents, builders solo, produits similaires — puis une synthèse
classée qui dit quoi reprendre, avec la métrique attendue. Ce juge ne lit
jamais un « verdict » écrit par un moteur : il recompte.

Contrat des fichiers (répertoire `<dir>` relatif au dépôt) :

  <engine>.json  (un par moteur, ex. glm.json, claude.json)
    {"schema": "deep-research/v1", "engine": "glm", "topic": "...",
     "produced_at": "...", "sources": [
        {"url": "https://...", "title": "...", "kind": "repo|paper|product|post",
         "evidence": "<citation textuelle ≥ 40 caractères>",
         "what_it_solves": "...", "compared_to_ours": "...",
         "decision": "ADOPT|ADAPT|KEEP_OURS|REJECT",
         "adoptable_item": "...", "metric": "<clé de METRICS>",
         "expected_effect": "..."}, ...]}

  SYNTHESIS.json
    {"schema": "deep-research-synthesis/v1", "engine": "...",
     "inputs": ["glm.json", "claude.json"],
     "items": [{"rank": 1, "title": "...", "metric": "<clé>",
                "baseline": <nombre>, "target": <nombre>,
                "sources": ["<url présente dans un fichier moteur>", ...],
                "found_by": ["glm", "claude"], "milestone_proposal": "..."}, ...]}

  SYNTHESIS.md : la liste de courses en français simple (≥ 1 ligne par item).

Règles (toutes recomptées ici) :
  - ≥ `--min-engines` fichiers moteur d'engines DISTINCTS, chacun ≥ `--min-sources`
    sources avec une URL http(s) unique, une citation ≥ 40 caractères, une
    décision du vocabulaire, une métrique du vocabulaire ;
  - synthèse : ≥ `--min-items` items classés 1..n sans trou, métrique du
    vocabulaire, baseline et target numériques, chaque URL citée existe dans
    un fichier moteur, `found_by` ⊆ moteurs présents ;
  - si `--project-dir` est donné : chaque moteur a bien été LANCÉ pour ce
    jalon (une ligne attempt_accounts.jsonl milestone==ID, engine==moteur) —
    un fichier « glm.json » écrit par Claude ne compte pas ;
  - SYNTHESIS.md ≥ `--min-items` lignes non vides.

Sortie : `<ID>_VERIFIED: {...}` rc=0 ou `<ID>_UNMET: <raison>` rc=1.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import re
import sys
from pathlib import Path

if __package__ in (None, ""):  # lancé comme script (python3 factory_v2/verify_deep_research.py) sans PYTHONPATH
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from factory_v2.shared_parsers import is_finite_number, json_loads_finite

METRICS = (
    # Factory
    "time_to_settle_s", "launches_per_settled_milestone", "false_acceptance_rate",
    "engine_kills_per_day", "quota_launches_per_day", "control_loc",
    "test_suite_s", "idle_ratio", "human_interventions_per_week",
    # Twitter Radar
    "call_extraction_precision", "call_extraction_recall", "account_coverage",
    "data_freshness_h", "time_to_decision_s", "kol_ranking_stability",
    "backfill_cost_per_account", "ux_task_completion_s",
)
DECISIONS = ("ADOPT", "ADAPT", "KEEP_OURS", "REJECT")
_URL = re.compile(r"^https?://\S+$")


class Unmet(Exception):
    pass


def _load(p: Path) -> dict:
    try:
        # L5_factory_v2_rest_2-025: NaN/Infinity refused at load (ValueError).
        return json_loads_finite(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Unmet(f"UNREADABLE {p.name}: {exc}")


def _check_engine_file(p: Path, min_sources: int) -> tuple[str, set]:
    d = _load(p)
    if d.get("schema") != "deep-research/v1":
        raise Unmet(f"{p.name}: schema != deep-research/v1")
    engine = str(d.get("engine") or "").lower()
    if engine not in ("glm", "claude", "codex"):
        raise Unmet(f"{p.name}: engine {engine!r} inconnu")
    if p.stem.lower() != engine:
        raise Unmet(f"{p.name}: le nom du fichier doit être <engine>.json ({engine})")
    sources = d.get("sources") or []
    urls: set = set()
    for i, s in enumerate(sources):
        if not isinstance(s, dict):
            raise Unmet(f"{p.name}: source #{i} n'est pas un objet")
        url = str(s.get("url") or "")
        if not _URL.match(url):
            raise Unmet(f"{p.name}: source #{i} URL invalide {url!r}")
        if url in urls:
            raise Unmet(f"{p.name}: URL dupliquée {url}")
        urls.add(url)
        if len(str(s.get("evidence") or "").strip()) < 40:
            raise Unmet(f"{p.name}: source #{i} citation < 40 caractères")
        if str(s.get("decision")) not in DECISIONS:
            raise Unmet(f"{p.name}: source #{i} décision {s.get('decision')!r} hors vocabulaire")
        if str(s.get("metric")) not in METRICS:
            raise Unmet(f"{p.name}: source #{i} métrique {s.get('metric')!r} hors vocabulaire")
        for k in ("title", "what_it_solves", "compared_to_ours", "adoptable_item", "expected_effect"):
            if not str(s.get(k) or "").strip():
                raise Unmet(f"{p.name}: source #{i} champ {k} vide")
    if len(sources) < min_sources:
        raise Unmet(f"{p.name}: {len(sources)} sources < {min_sources}")
    return engine, urls


def _check_synthesis(d: dict, engines: set, urls: set, min_items: int) -> int:
    if d.get("schema") != "deep-research-synthesis/v1":
        raise Unmet("SYNTHESIS.json: schema != deep-research-synthesis/v1")
    items = d.get("items") or []
    if len(items) < min_items:
        raise Unmet(f"SYNTHESIS.json: {len(items)} items < {min_items}")
    ranks = sorted(int(i.get("rank", 0)) for i in items if isinstance(i, dict))
    if ranks != list(range(1, len(items) + 1)):
        raise Unmet(f"SYNTHESIS.json: rangs {ranks} ≠ 1..{len(items)}")
    for it in items:
        if str(it.get("metric")) not in METRICS:
            raise Unmet(f"SYNTHESIS.json: item {it.get('rank')} métrique hors vocabulaire")
        for k in ("baseline", "target"):
            # L5_factory_v2_rest_2-025: NaN/Infinity are floats but not measures.
            if not is_finite_number(it.get(k)):
                raise Unmet(f"SYNTHESIS.json: item {it.get('rank')} {k} non numérique (fini requis)")
        srcs = it.get("sources") or []
        if not srcs:
            raise Unmet(f"SYNTHESIS.json: item {it.get('rank')} sans source")
        for u in srcs:
            if u not in urls:
                raise Unmet(f"SYNTHESIS.json: item {it.get('rank')} cite une URL absente des fichiers moteur: {u}")
        fb = set(str(e).lower() for e in (it.get("found_by") or []))
        if not fb or not fb <= engines:
            raise Unmet(f"SYNTHESIS.json: item {it.get('rank')} found_by {sorted(fb)} ⊄ {sorted(engines)}")
        for k in ("title", "milestone_proposal"):
            if not str(it.get(k) or "").strip():
                raise Unmet(f"SYNTHESIS.json: item {it.get('rank')} champ {k} vide")
    return len(items)


def _engines_launched(project_dir: Path, milestone: str) -> set:
    ledger = project_dir / "evidence" / "attempt_accounts.jsonl"
    out: set = set()
    if not ledger.exists():
        # porte rejouée dans un worktree de validation : evidence/ n'est pas versionné, le registre vit
        # dans le dépôt de contrôle vivant (FACTORY_SUPERVISOR_EVIDENCE_ROOT, posé par authority_gate)
        root = os.environ.get("FACTORY_SUPERVISOR_EVIDENCE_ROOT", "")
        rel = os.path.relpath(project_dir.resolve(), Path.cwd().resolve())
        alt = Path(root) / rel / "evidence" / "attempt_accounts.jsonl" if root and not rel.startswith("..") else None
        if alt is None or not alt.exists():
            return out
        ledger = alt
    for line in ledger.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        mid = str(row.get("milestone") or "")
        # un jalon de recherche par moteur (ID_GLM, ID_CLAUDE…) compte pour le jalon de synthèse ID
        same = mid == milestone or (mid.startswith(milestone + "_") and mid[len(milestone) + 1 :].lower() in ("glm", "claude", "codex"))
        if same and row.get("engine") in ("glm", "claude", "codex"):
            out.add(str(row["engine"]))
    return out


def verify(repo: Path, rel_dir: str, milestone: str, *, min_engines: int, min_sources: int,
           min_items: int, project_dir: Path | None) -> dict:
    d = repo / rel_dir
    if not d.is_dir():
        raise Unmet(f"répertoire absent: {rel_dir}")
    engine_files = sorted(p for p in d.glob("*.json") if p.name != "SYNTHESIS.json")
    engines: set = set()
    urls: set = set()
    for p in engine_files:
        e, u = _check_engine_file(p, min_sources)
        if e in engines:
            raise Unmet(f"moteur {e} en double")
        engines.add(e)
        urls |= u
    if len(engines) < min_engines:
        raise Unmet(f"{len(engines)} moteurs distincts < {min_engines}: {sorted(engines)}")
    if project_dir is not None:
        launched = _engines_launched(project_dir, milestone)
        missing = sorted(engines - launched)
        if missing:
            raise Unmet(f"moteurs jamais lancés pour {milestone} d'après attempt_accounts: {missing}")
    n_items = _check_synthesis(_load(d / "SYNTHESIS.json"), engines, urls, min_items)
    md = d / "SYNTHESIS.md"
    if not md.exists():
        raise Unmet("SYNTHESIS.md absent")
    lines = [ln for ln in md.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if len(lines) < min_items:
        raise Unmet(f"SYNTHESIS.md: {len(lines)} lignes < {min_items}")
    return {"engines": sorted(engines), "sources": len(urls), "items": n_items}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("milestone")
    ap.add_argument("--project-repo", default=".")
    ap.add_argument("--dir", required=True, help="répertoire de la recherche, relatif au dépôt")
    ap.add_argument("--project-dir", default="", help="projects/<p> pour recouper les moteurs lancés")
    ap.add_argument("--min-engines", type=int, default=2)
    ap.add_argument("--min-sources", type=int, default=8)
    ap.add_argument("--min-items", type=int, default=5)
    a = ap.parse_args(argv)
    try:
        r = verify(Path(a.project_repo).resolve(), a.dir, a.milestone,
                   min_engines=a.min_engines, min_sources=a.min_sources,
                   min_items=a.min_items,
                   project_dir=Path(a.project_dir).resolve() if a.project_dir else None)
    except Unmet as exc:
        print(f"{a.milestone}_UNMET: {exc}")
        return 1
    print(f"{a.milestone}_VERIFIED: {json.dumps(r, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
