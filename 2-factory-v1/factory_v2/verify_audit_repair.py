#!/usr/bin/env python3
"""Juge mécanique de la réparation de l'audit (FULL_CODEBASE_AUDIT_REVIEWED#M1).

Traduit en contrôles recomptés les quatre constats du relecteur Claude
(18/09, BLOCK sur aa295959) — le test_cmd de la mission de réparation doit
être ROUGE avant réparation (sinon le runtime saute l'appel modèle :
MODEL_CALL_SKIPPED_EXISTING_PRODUCT_VALID) et VERT après.

  1. FACTORY_CONVERGENCE_LEDGER.json : pour chaque décision dont `file` existe à
     HEAD, `loc` est à ±10 % de `wc -l` réel ; pour un fichier absent, la
     décision cite `deleted_in_commit` (40 hex) présent dans `git log`.
  2. FACTORY_F3_COUNTER_AUDIT_RECEIPT.json : une clé `def001_root_cause`
     (dict) avec `defect` == PREMATURE_IDLE_AFTER_MILESTONE_SETTLEMENT,
     `root_cause` non vide, `regression_test` = chemin existant sous tests/.
  3. FACTORY_F3_EXTERNAL_RESEARCH_RECEIPT.json : ≥ 8 `frameworks_inspected`
     avec `repo`, `commit_sha` (40 hex), `file` et `code_excerpt` ≥ 3 lignes.
  4. FACTORY_RUNTIME_AUTHORITY_MAP.json ne marque plus backlog_refiller ACTIVE.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

HEX40 = re.compile(r"^[0-9a-f]{40}$")


def _wc(p: Path) -> int:
    try:
        return sum(1 for _ in p.open("rb"))
    except OSError:
        return -1


def check(repo: Path) -> list:
    unmet = []
    aud = repo / "factory_v2" / "audit"
    # 1. ledger LOC
    try:
        ledger = json.loads((aud / "FACTORY_CONVERGENCE_LEDGER.json").read_text())
    except Exception as exc:
        return [f"ledger unreadable: {exc}"]
    bad_loc = []
    for d in ledger.get("decisions") or []:
        f = str(d.get("file") or "")
        p = repo / f
        if f and p.is_file():
            try:
                loc = int(d.get("loc"))
            except (TypeError, ValueError):
                bad_loc.append(f"{f}: loc non numérique"); continue
            real = _wc(p)
            if real > 0 and abs(loc - real) > max(5, 0.10 * real):
                bad_loc.append(f"{f}: loc={loc} réel={real}")
        elif f:
            sha = str(d.get("deleted_in_commit") or "")
            if not HEX40.match(sha):
                bad_loc.append(f"{f}: fichier absent sans deleted_in_commit (40 hex)")
            else:
                r = subprocess.run(["git", "cat-file", "-e", sha], cwd=repo, capture_output=True)
                if r.returncode != 0:
                    bad_loc.append(f"{f}: deleted_in_commit {sha[:12]} inconnu du dépôt")
    if bad_loc:
        unmet.append("LEDGER_LOC: " + "; ".join(bad_loc[:8]))
    # 2. DEF-001 root cause
    try:
        ca = json.loads((aud / "FACTORY_F3_COUNTER_AUDIT_RECEIPT.json").read_text())
    except Exception as exc:
        unmet.append(f"counter-audit unreadable: {exc}"); ca = {}
    rc = ca.get("def001_root_cause") or {}
    if not (isinstance(rc, dict) and rc.get("defect") == "PREMATURE_IDLE_AFTER_MILESTONE_SETTLEMENT"
            and str(rc.get("root_cause") or "").strip()
            and str(rc.get("regression_test") or "").startswith("tests/")
            and (repo / str(rc.get("regression_test"))).is_file()):
        unmet.append("DEF001_ROOT_CAUSE: def001_root_cause {defect, root_cause, regression_test existant} manquant")
    # 3. references
    try:
        rr = json.loads((aud / "FACTORY_F3_EXTERNAL_RESEARCH_RECEIPT.json").read_text())
    except Exception as exc:
        unmet.append(f"research receipt unreadable: {exc}"); rr = {}
    good = 0
    for fw in rr.get("frameworks_inspected") or []:
        if (isinstance(fw, dict) and fw.get("repo") and HEX40.match(str(fw.get("commit_sha") or ""))
                and fw.get("file") and len(str(fw.get("code_excerpt") or "").strip().splitlines()) >= 3):
            good += 1
    if good < 8:
        unmet.append(f"REFERENCES: {good} références avec repo+commit_sha(40 hex)+file+code_excerpt(≥3 lignes) < 8")
    # 4. authority map
    try:
        am = json.loads((aud / "FACTORY_RUNTIME_AUTHORITY_MAP.json").read_text())
        # L5_factory_v2_rest_2-021: parsed, not regex'd — a multi-line ACTIVE
        # entry passed and a compact INACTIVE entry failed on the substring.
        if _backlog_refiller_active(am):
            unmet.append("AUTHORITY_MAP: backlog_refiller encore marqué ACTIVE")
    except (OSError, ValueError) as exc:
        unmet.append(f"authority map unreadable: {exc}")
    return unmet


def _backlog_refiller_active(node, key: str = "") -> bool:
    """True iff some object naming backlog_refiller (by key, `component` or
    `name`) carries status/state == "ACTIVE" (exact word, whole value)."""
    if isinstance(node, dict):
        names = [key] + [str(node.get(k) or "") for k in ("component", "name", "module", "path")]
        if any("backlog_refiller" in n for n in names):
            if any(str(node.get(k) or "").strip() == "ACTIVE" for k in ("status", "state")):
                return True
        return any(_backlog_refiller_active(v, str(k)) for k, v in node.items())
    if isinstance(node, list):
        return any(_backlog_refiller_active(v, key) for v in node)
    return False


def main(argv=None) -> int:
    repo = Path(argv[0] if argv else ".").resolve()
    unmet = check(repo)
    if unmet:
        print("AUDIT_REPAIR_UNMET:")
        for u in unmet:
            print(" -", u)
        return 1
    print("AUDIT_REPAIR_VERIFIED")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
