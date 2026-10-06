#!/usr/bin/env python3
"""Coût en tokens de la factory — par jalon réglé et par moteur, recompté depuis le registre.

FACTORY_TOKEN_TRUTH_V1. Source unique : `projects/*/evidence/attempt_accounts.jsonl` (lignes
TERMINAL des moteurs claude/codex/glm, repliées par attempt_key). Chaque ligne porte les tokens
MESURÉS par les adaptateurs (`factory_v2/telemetry/adapters.py`) sur le flux brut du lancement ;
une ligne sans tokens est comptée comme telle, avec sa raison typée — jamais convertie en 0.

    python3 factory_v2/token_cost_report.py --since-hours 24          # texte (propriétaire)
    python3 factory_v2/token_cost_report.py --since-hours 24 --json   # machine (juge, dashboard)

« Coût par résultat validé » suit le contrat de mesure de SPEC.md : tous les appels, échecs,
retries, revues et validations de la fenêtre / résultats validés (jalons settlés indépendamment
dans la fenêtre). Dénominateur nul → `UNDEFINED_ZERO_DENOMINATOR` + coût brûlé affiché, jamais 0.
Un abonnement n'est jamais traduit en prix API : ce rapport ne parle qu'en tokens et en minutes.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from factory_v2.resource_accounting import fold, read_rows

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA = "token-cost-report/v1"
ENGINES = ("claude", "codex", "glm")
#: Les cinq compteurs DISJOINTS du vocabulaire télémétrie (record.py) : leur somme est le total
#: de tokens traités par le fournisseur ; `input_tokens` est l'entrée NON cachée.
TOKEN_FIELDS = ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens", "reasoning_tokens")
UNDEFINED = "UNDEFINED_ZERO_DENOMINATOR"
DEFINED = "DEFINED"
RULE = ("SPEC.md — Contrat de mesure : tous les appels, échecs, retries, revues et validations de la fenêtre / "
        "résultats validés ; dénominateur nul → non défini + coût brûlé affiché, jamais 0")


def _ts(s) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _bucket() -> dict:
    return {"attempts": 0, "attempts_measured": 0, "attempts_unmeasured": 0, "unmeasured_reasons": {},
            "attempts_by_kind": {}, "engine_minutes": 0.0, "tokens_completeness": "COMPLETE",
            "tokens_total": 0, "tokens_floor_rows": 0, **{f: 0 for f in TOKEN_FIELDS}}


def _measured(row: dict) -> bool:
    return _num(row.get("input_tokens")) is not None and _num(row.get("output_tokens")) is not None


def _kind(row: dict) -> str:
    key = str(row.get("attempt_key") or "")
    if key.startswith("review:") or key.startswith("cell-review:"):
        return "review"
    return str(row.get("task_kind") or "build")


def _add(b: dict, row: dict) -> None:
    b["attempts"] += 1
    b["engine_minutes"] = round(b["engine_minutes"] + (_num(row.get("duration_s")) or 0) / 60.0, 2)
    kind = _kind(row)
    b["attempts_by_kind"][kind] = b["attempts_by_kind"].get(kind, 0) + 1
    if not _measured(row):
        b["attempts_unmeasured"] += 1
        reason = str(row.get("usage_source") or "UNKNOWN")
        b["unmeasured_reasons"][reason] = b["unmeasured_reasons"].get(reason, 0) + 1
        b["tokens_completeness"] = "PARTIAL_FLOOR"      # une somme à laquelle il manque un essai est un plancher
        return
    b["attempts_measured"] += 1
    for f in TOKEN_FIELDS:
        v = _num(row.get(f))
        if v is not None:
            b[f] += int(v)
            b["tokens_total"] += int(v)
    if str(row.get("usage_completeness") or "COMPLETE") != "COMPLETE":
        b["tokens_floor_rows"] += 1
        b["tokens_completeness"] = "PARTIAL_FLOOR"


def _finish(b: dict) -> dict:
    b["unmeasured_reasons"] = dict(sorted(b["unmeasured_reasons"].items()))
    b["attempts_by_kind"] = dict(sorted(b["attempts_by_kind"].items()))
    if b["attempts_measured"] == 0:
        b["tokens_completeness"] = "UNKNOWN"
    return b


def project_dirs_of(repo: Path) -> list:
    return sorted(p for p in (repo / "projects").glob("*/") if (p / "evidence" / "attempt_accounts.jsonl").is_file())


def collect(project_dirs, since: datetime, until: datetime | None = None) -> dict:
    """Le rapport, recompté depuis les registres. `since`/`until` en UTC, fenêtre [since, until)."""
    until = until or datetime.now(timezone.utc)
    by_engine = {e: _bucket() for e in ENGINES}
    by_milestone: dict = {}
    total = _bucket()
    settled: dict = {}
    other = 0
    projects = []
    for pd in project_dirs:
        f = Path(pd) / "evidence" / "attempt_accounts.jsonl"
        if not f.is_file():
            continue
        projects.append(Path(pd).name)
        for r in fold(read_rows(f)):
            if r.get("phase") != "TERMINAL":
                continue
            end = _ts(r.get("ended_at") or r.get("validated_at") or r.get("started_at"))
            if end is None or end < since or end >= until:
                continue
            ms = str(r.get("milestone") or "")
            if r.get("independently_validated") and ms:
                at = _ts(r.get("validated_at")) or end
                if since <= at < until:
                    settled[ms] = max(settled.get(ms, ""), at.strftime("%Y-%m-%dT%H:%M:%SZ"))
            eng = str(r.get("engine") or "")
            if eng not in ENGINES:
                other += 1
                continue
            _add(total, r)
            _add(by_engine[eng], r)
            m = by_milestone.setdefault(ms or "(sans jalon)", {"project": Path(pd).name, **_bucket(),
                                                                "by_engine": {}})
            _add(m, r)
            _add(m["by_engine"].setdefault(eng, _bucket()), r)
    for ms, at in settled.items():
        m = by_milestone.setdefault(ms, {"project": "", **_bucket(), "by_engine": {}})
        m["settled"] = True
        m["settled_at"] = at
    for ms, m in by_milestone.items():
        m.setdefault("settled", False)
        m.setdefault("settled_at", "")
        _finish(m)
        m["by_engine"] = {e: _finish(b) for e, b in sorted(m["by_engine"].items())}
    by_milestone = dict(sorted(by_milestone.items(), key=lambda kv: (not kv[1]["settled"], kv[0])))
    for e in ENGINES:
        _finish(by_engine[e])
        by_engine[e]["milestones"] = sum(1 for m in by_milestone.values() if e in m["by_engine"])
    _finish(total)
    n_valid = len(settled)
    numerator = {k: total[k] for k in ("attempts", "attempts_measured", "attempts_unmeasured", "engine_minutes",
                                        "tokens_total", "tokens_completeness", *TOKEN_FIELDS)}
    cost = {"rule": RULE, "validated_results": n_valid, "validated_milestones": sorted(settled),
            "numerator": numerator, "status": DEFINED if n_valid else UNDEFINED}
    if n_valid:
        cost["tokens_per_validated_result"] = round(total["tokens_total"] / n_valid, 1)
        cost["engine_minutes_per_validated_result"] = round(total["engine_minutes"] / n_valid, 2)
        cost["attempts_per_validated_result"] = round(total["attempts"] / n_valid, 2)
    else:
        cost["tokens_per_validated_result"] = UNDEFINED
        cost["burned"] = {"tokens_total": total["tokens_total"], "engine_minutes": total["engine_minutes"],
                          "attempts": total["attempts"],
                          "note": "aucun résultat validé dans la fenêtre : coût brûlé, ratio non défini (jamais 0)"}
    coverage_pct = round(100.0 * total["attempts_measured"] / total["attempts"], 1) if total["attempts"] else None
    return {"schema": SCHEMA, "generated_at": until.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "window": {"since": since.strftime("%Y-%m-%dT%H:%M:%SZ"), "until": until.strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "hours": round((until - since).total_seconds() / 3600.0, 3)},
            "projects": projects, "engines": list(ENGINES), "token_fields": list(TOKEN_FIELDS),
            "coverage": {"terminal_attempts": total["attempts"], "with_tokens": total["attempts_measured"],
                         "pct": coverage_pct, "unmeasured_reasons": total["unmeasured_reasons"],
                         "other_engine_attempts_ignored": other},
            "totals": total, "by_engine": by_engine, "by_milestone": by_milestone,
            "settled_milestones": sorted(settled), "cost_per_validated_result": cost,
            "one_line": one_line_from(total, coverage_pct, cost, (until - since).total_seconds() / 3600.0)}


def one_line_from(total: dict, coverage_pct, cost: dict, hours: float) -> str:
    cov = "n/a" if coverage_pct is None else f"{coverage_pct:g} %"
    head = (f"Tokens {hours:g} h : {total['tokens_total']:,} (entrée {total['input_tokens']:,}, cache "
            f"{total['cache_read_tokens'] + total['cache_write_tokens']:,}, sortie "
            f"{total['output_tokens'] + total['reasoning_tokens']:,}) sur {total['attempts']} essais, "
            f"{total['engine_minutes']:g} min moteur, couverture {cov}").replace(",", " ")
    if cost["status"] == DEFINED:
        tail = (f"jalons réglés {cost['validated_results']} ; coût par résultat validé "
                f"{cost['tokens_per_validated_result']:,.0f} tokens / "
                f"{cost['engine_minutes_per_validated_result']:g} min").replace(",", " ")
    else:
        b = cost["burned"]
        tail = (f"jalons réglés 0 ; coût par résultat validé NON DÉFINI (brûlé : {b['tokens_total']:,} tokens, "
                f"{b['engine_minutes']:g} min, {b['attempts']} essais)").replace(",", " ")
    return f"{head} ; {tail}."


def one_line(report: dict) -> str:
    return str(report.get("one_line") or "")


def to_text(rep: dict) -> str:
    w = rep["window"]
    lines = [f"COÛT EN TOKENS — fenêtre {w['since']} → {w['until']} ({w['hours']:g} h), projets : "
             f"{', '.join(rep['projects']) or 'aucun'}", "",
             "Par moteur (lignes TERMINAL du registre) :",
             "  moteur   essais  mesurés  entrée      cache(lect)  cache(écr)  sortie     raison.  minutes  complétude"]
    for e, b in rep["by_engine"].items():
        lines.append(f"  {e:<8} {b['attempts']:>6}  {b['attempts_measured']:>7}  {b['input_tokens']:>10,}  "
                     f"{b['cache_read_tokens']:>11,}  {b['cache_write_tokens']:>10,}  {b['output_tokens']:>9,}  "
                     f"{b['reasoning_tokens']:>7,}  {b['engine_minutes']:>7g}  {b['tokens_completeness']}"
                     .replace(",", " "))
        if b["unmeasured_reasons"]:
            lines.append(f"           non mesurés : {b['unmeasured_reasons']}")
    lines += ["", "Par jalon (réglés d'abord) :"]
    if not rep["by_milestone"]:
        lines.append("  aucun essai moteur dans la fenêtre")
    for ms, m in rep["by_milestone"].items():
        tag = f"RÉGLÉ {m['settled_at']}" if m["settled"] else "en cours / non réglé"
        lines.append(f"  {ms} — {tag} — {m['attempts']} essais ({m['attempts_measured']} mesurés), "
                     f"{m['tokens_total']:,} tokens, {m['engine_minutes']:g} min".replace(",", " "))
        for e, b in m["by_engine"].items():
            lines.append(f"      {e:<7} {b['attempts']:>3} essais  entrée {b['input_tokens']:,}  cache "
                         f"{b['cache_read_tokens']:,}  sortie {b['output_tokens']:,}  {b['engine_minutes']:g} min"
                         .replace(",", " "))
    c = rep["cost_per_validated_result"]
    lines += ["", f"Coût par résultat validé ({c['status']}) : {rep['one_line']}", f"  règle : {c['rule']}",
              f"Couverture : {rep['coverage']['with_tokens']}/{rep['coverage']['terminal_attempts']} essais avec "
              f"tokens ({rep['coverage']['pct']} %) ; non mesurés : {rep['coverage']['unmeasured_reasons']}"]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--since-hours", type=float, default=24.0)
    ap.add_argument("--json", action="store_true", help="JSON seul sur stdout")
    ap.add_argument("--project-dir", action="append", default=[],
                    help="répertoire projet (défaut : tous les projects/*/ avec un registre)")
    ap.add_argument("--repo", default=str(REPO_ROOT))
    a = ap.parse_args(argv)
    if a.since_hours < 0:
        print("REFUSED: --since-hours doit être >= 0", file=sys.stderr)
        return 2
    dirs = [Path(p) for p in a.project_dir] or project_dirs_of(Path(a.repo).resolve())
    now = datetime.now(timezone.utc)
    rep = collect(dirs, now - timedelta(hours=a.since_hours), now)
    if a.json:
        print(json.dumps(rep, indent=2, sort_keys=True, ensure_ascii=False))
    else:
        print(to_text(rep))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
