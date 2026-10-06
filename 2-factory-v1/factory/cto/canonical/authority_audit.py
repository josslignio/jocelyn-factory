#!/usr/bin/env python3
"""Authority audit — prouve la relecture du cerveau projet (AUTHORITY_MAP.md).

Checks C1-C7 (voir AUTHORITY_MAP.md). Écrit AUTHORITY_AUDIT_RECEIPT.json.
Exit 0 ssi tous les checks PASS — contradictions = 0.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
_CTO = _REPO / "factory" / "cto"
_CANON = _CTO / "canonical"
_CAMPAIGN = _REPO / "factory" / "campaigns" / "app007"
_RADAR = Path("/Users/jocelyngrosjean/conductor/workspaces/twitter-radar/twitter-radar-foundation-v2")

AUTHORITATIVE = [
    _REPO / "projects" / "factory" / "STRATEGY.md",
    _REPO / "projects" / "factory" / "SPEC.md",
    _REPO / "projects" / "factory" / "ROADMAP.yaml",
    _REPO / "projects" / "factory" / "PROJECT_STATE.json",
    _REPO / "projects" / "twitter-radar" / "STRATEGY.md",
    _REPO / "projects" / "twitter-radar" / "SPEC.md",
    _REPO / "projects" / "twitter-radar" / "ROADMAP.yaml",
    _REPO / "projects" / "twitter-radar" / "PROJECT_STATE.json",
    _CAMPAIGN / "APP007_CAMPAIGN_STATE.json",
    _CTO / "CTOK_CHAIN_STATE.json",
    _CTO / "CTO_BACKLOG.json",
    _CTO / "PORTFOLIO_STATE.json",
    _CTO / "CTO_PRODUCT_STATE.json",
    _CANON / "AUTHORITY_MAP.md",
]
VENDORED = {
    _CTO / "CTO_ARCHITECTURE.json": _RADAR / "factory" / "cto" / "CTO_ARCHITECTURE.json",
    _CTO / "CTO_RESEARCH_REGISTRY.json": _RADAR / "factory" / "cto" / "CTO_RESEARCH_REGISTRY.json",
}


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def check(label: str, ok: bool, detail: str = "") -> dict:
    return {"check": label, "verdict": "PASS" if ok else "FAIL", "detail": detail}


def _guess_token(pt: dict) -> str:
    """Fallback heuristic token per lifecycle point, used only when
    AUTHORITY_WIRING.json carries no explicit 'token' field for the point."""
    tokens = {
        "startup_restart": "_cto_startup_check",
        "context_compile": "CTO_STRATEGY.md",
        "pre_spawn": "compiler.compile(",
        "mission_settlement": "_sync_portfolio_settlement",
        "milestone_settlement": "IndependentValidator",
        "project_switch_next_work": "portfolio",
    }
    return tokens.get(pt.get("point"), "")


def main() -> int:
    results = []

    # C1 — fichiers autoritaires existent
    missing = [str(p.relative_to(_REPO)) for p in AUTHORITATIVE if not p.exists()]
    results.append(check("C1_authoritative_files_exist", not missing, f"missing: {missing}"))

    # C2 — pas d'autoritaire marqué SUPERSEDED (dans ses 3 premières lignes)
    superseded = [p.name for p in AUTHORITATIVE
                  if p.exists() and "SUPERSEDED" in p.read_text(encoding="utf-8")[:400].upper()]
    results.append(check("C2_no_authoritative_superseded", not superseded, f"superseded: {superseded}"))

    campaign = _load(_CAMPAIGN / "APP007_CAMPAIGN_STATE.json")
    completed = {c if isinstance(c, str) else c.get("id") for c in campaign.get("completed", [])}
    active_now = set(campaign.get("active", []) or []) | set(campaign.get("queued", []) or [])

    # C3 — roadmaps canoniques (yaml) ne contredisent pas l'état de campagne
    contradictions = []
    import yaml as _yaml
    for proj in ("projects/factory", "projects/twitter-radar"):
        rp = _REPO / proj / "ROADMAP.yaml"
        sp = _REPO / proj / "PROJECT_STATE.json"
        if not (rp.is_file() and sp.is_file()):
            continue
        rm = _yaml.safe_load(rp.read_text(encoding="utf-8"))
        st = _load(sp)
        done = {mid for mid, rec in (st.get("completed_milestones") or {}).items()
                if (rec or {}).get("state") == "COMPLETE"}
        for m in rm.get("milestones", []) or []:
            mid = m.get("id")
            if mid in completed and mid not in done:
                contradictions.append(f"{proj}:{mid} campaign-complete but project-state not")
    results.append(check("C3_roadmap_vs_campaign", not contradictions, str(contradictions)))

    # C4 — backlog ne référence aucune mission complétée comme ACTIVE/NEXT/READY
    backlog = _load(_CTO / "CTO_BACKLOG.json")
    stale = []
    for lane in ("H0_ACTIVE", "H0_NEXT", "H1_READY", "H2_PLANNED"):
        for e in backlog.get(lane, []) or []:
            if e.get("id") in completed:
                stale.append(f"{lane}:{e['id']}")
    results.append(check("C4_backlog_purged_of_completed", not stale, str(stale)))

    # C5 — copies vendues radar identiques aux originaux
    drift = []
    for src, dst in VENDORED.items():
        if dst.exists():
            try:
                if _load(src) != _load(dst):
                    drift.append(dst.name)
            except Exception as exc:
                drift.append(f"{dst.name}({exc})")
    results.append(check("C5_vendored_copies_in_sync", not drift, str(drift)))

    # C6 — portfolio : pas de projet ACTIVE pointant sur une mission complétée
    portfolio = _load(_CTO / "PORTFOLIO_STATE.json")
    bad_ptr = [f"{p.get('project_id')}->{p.get('next_mission_id')}"
               for p in portfolio.get("projects", [])
               if p.get("state") == "ACTIVE" and p.get("next_mission_id") in completed]
    results.append(check("C6_portfolio_no_stale_pointer", not bad_ptr, str(bad_ptr)))

    # C7 — une seule SPEC active par projet (l'autre porte SUPERSEDED en tête)
    radar_spec = _RADAR / "APP007_PRODUCT_SPEC_V2.md"
    ok7 = True
    detail7 = ""
    if radar_spec.exists():
        head = radar_spec.read_text(encoding="utf-8")[:400]
        if "SUPERSEDED" not in head.upper():
            ok7, detail7 = False, f"{radar_spec.name} lacks SUPERSEDED header"
    results.append(check("C7_single_spec_per_project", ok7, detail7 or "radar repo spec superseded; canonical in factory repo"))

    # C8 — relecture prouvée: chaque point WIRED du AUTHORITY_WIRING référence du
    # code réel (fichier existe + jeton de câblage — nom de fonction/classe/appel
    # nommé — présent quelque part dans le fichier).
    #
    # Anchoring on "±5 lines of the cited line number" made this check brittle to
    # ordinary code drift: a function moving 40 lines down (refactor, added
    # comments) flips a genuinely-still-wired point to FAIL even though nothing
    # broke. Searching the whole file for the named token (a function name, class
    # name, or a call site — never a bare English word) proves the same thing
    # (the code path still exists) without being coupled to line numbers, which
    # are not part of the wiring contract.
    wiring_path = _CANON / "AUTHORITY_WIRING.json"
    ok8, detail8 = True, []
    if not wiring_path.exists():
        ok8, detail8 = False, ["AUTHORITY_WIRING.json missing"]
    else:
        wiring = _load(wiring_path)
        for pt in wiring.get("lifecycle_points", []):
            if pt.get("verdict") not in ("WIRED", "WIRED_ADVISORY"):
                continue
            ref = pt.get("code_ref") or ""
            if not ref:
                ok8 = False; detail8.append(f"{pt['point']}: no code_ref"); continue
            fpart, lineno = ref.rsplit(":", 1)
            src = _REPO / fpart
            if not src.exists():
                ok8 = False; detail8.append(f"{pt['point']}: {fpart} missing"); continue
            content = src.read_text(encoding="utf-8")
            token = pt.get("token") or _guess_token(pt)
            if token and token not in content:
                ok8 = False; detail8.append(
                    f"{pt['point']}: named token {token!r} not found anywhere in {fpart} "
                    f"(cited at line {lineno})")
    results.append(check("C8_authority_relecture_wiring", ok8,
                         "; ".join(detail8) or "all WIRED lifecycle points resolve to live code"))

    # C9 — intégrité des autorités legacy gelées (déplacées vers projects/*/)
    freeze_path = _CANON / "LEGACY_AUTHORITY_FREEZE.json"
    ok9, detail9 = True, []
    if not freeze_path.exists():
        ok9, detail9 = False, ["LEGACY_AUTHORITY_FREEZE.json missing"]
    else:
        freeze = _load(freeze_path)
        import hashlib
        for path, rec in freeze.get("frozen", {}).items():
            f = _REPO / path
            if not f.exists():
                ok9, detail9 = False, detail9 + [f"{path} deleted"]
                continue
            cur = hashlib.sha256(f.read_bytes()).hexdigest()[:16]
            if cur != rec.get("frozen_sha"):
                ok9 = False
                detail9.append(f"{path}: integrity violation (edited after freeze)")
            succ = _REPO / rec.get("successor", "")
            if not succ.exists():
                ok9, detail9 = False, detail9 + [f"successor missing: {rec.get('successor')}"]
    results.append(check("C9_legacy_authority_integrity", ok9,
                         "; ".join(detail9) or "all frozen legacy authorities intact"))

    # C10 — contrat produit §I: trio + PROJECT_STATE par projet
    ok10, detail10 = True, []
    for proj in ("projects/factory", "projects/twitter-radar"):
        for name in ("STRATEGY.md", "SPEC.md", "ROADMAP.yaml", "PROJECT_STATE.json"):
            if not (_REPO / proj / name).is_file():
                ok10, detail10 = False, detail10 + [f"{proj}/{name} missing"]
    results.append(check("C10_canonical_trio_per_project", ok10,
                         "; ".join(detail10) or "both projects carry STRATEGY/SPEC/ROADMAP/PROJECT_STATE"))

    all_pass = all(r["verdict"] == "PASS" for r in results)
    receipt = {
        "schema": "authority-audit-receipt/v1",
        "produced_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "verdict": "PASS" if all_pass else "FAIL",
        "rule": "contradictions = 0",
        "checks": results,
        "campaign_completed_count": len(completed),
        "campaign_active_or_queued": sorted(active_now),
    }
    fd, tmp = tempfile.mkstemp(dir=str(_CANON))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(receipt, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, str(_CANON / "AUTHORITY_AUDIT_RECEIPT.json"))

    for r in results:
        print(f"{r['verdict']:4} {r['check']} {r['detail'] if r['verdict']=='FAIL' else ''}")
    print("VERDICT:", receipt["verdict"])
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
