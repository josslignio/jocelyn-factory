#!/usr/bin/env python3
"""Juge de FACTORY_TOKEN_TRUTH_V1 — chaque lancement de moteur a un coût mesuré, et la
factory se compare aux moteurs seuls avec des tokens réels.

Ordre propriétaire (toujours demandé, jamais livré) : « la factory dépense-t-elle beaucoup plus
de tokens qu'un GLM seul, un Claude seul ou un Codex seul ? ». Recompté, jamais cru :
  1. ADAPTATEURS : `factory_v2/telemetry/adapters.py` parse les formats ACTUELS des CLI —
     codex `exec --json` (`turn.completed` → `usage.{input_tokens,cached_input_tokens,output_tokens,
     reasoning_output_tokens}`), opencode `run --format json` (parts avec `tokens`), claude
     `stream-json` — prouvé sur `tests/fixtures/token_truth/*.jsonl` (échantillons RÉELS capturés,
     un par moteur) : `parse_*` rend des tokens mesurés, pas UNKNOWN.
  2. FLUX CONSERVÉS : chaque tentative écrit son flux JSON brut sous
     `projects/<p>/evidence/streams/<attempt_key>.jsonl` (bookkeeping, hors capture) — le coût
     est re-dérivable après coup.
  3. COUVERTURE : depuis l'arrivée de ce lot sur la ligne principale, ≥ 90 % des lignes TERMINAL
     de `attempt_accounts.jsonl` (moteurs claude/codex/glm) portent input_tokens ET output_tokens
     numériques (le reste = coupures avant le premier événement, typées).
  4. RAPPORT : `python3 factory_v2/token_cost_report.py --since-hours 24 --json` rend, par jalon
     réglé et par moteur : tokens entrée/cache/sortie, essais, « coût par résultat validé » (SPEC :
     dénominateur nul → non défini, jamais 0).
  5. BENCHMARK APPARIÉ AVEC TOKENS : `factory_v2/receipts/MATCHED_BENCHMARK_RECEIPT.json` (rejoué
     par `run_matched_benchmark_3arm.py`, 3 runs par bras, même charge) porte des tokens > 0 pour
     chaque bras et une comparaison factory vs meilleur solo en tokens ET en résultats vérifiés.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


class Unmet(Exception):
    pass


def _landed_ts(repo: Path) -> float | None:
    out = subprocess.run(["git", "log", "--first-parent", "--diff-filter=A", "--format=%ct", "--",
                          "factory_v2/token_cost_report.py"], cwd=repo, capture_output=True, text=True,
                         timeout=30).stdout.split()
    return float(out[-1]) if out else None


def verify(repo: Path) -> dict:
    env = {**os.environ, "FACTORY_NO_REAL_ENGINE": "1", "PYTHONPATH": str(repo)}
    fx = repo / "tests" / "fixtures" / "token_truth"
    need = {"codex.jsonl", "opencode.jsonl", "claude.jsonl"}
    have = {p.name for p in fx.glob("*.jsonl")} if fx.is_dir() else set()
    if not need <= have:
        raise Unmet(f"échantillons réels manquants dans tests/fixtures/token_truth: {sorted(need - have)}")
    sys.path.insert(0, str(repo))
    from factory_v2.telemetry import adapters as ad  # noqa: E402
    parsers = {"codex.jsonl": ad.parse_codex_jsonl, "opencode.jsonl": ad.parse_opencode_json,
               "claude.jsonl": ad.parse_claude_stream_json}
    for name, fn in parsers.items():
        lines = (fx / name).read_text(encoding="utf-8").splitlines()
        parsed = fn(lines)
        u = parsed.usage
        it, ot = getattr(u, "input_tokens", None), getattr(u, "output_tokens", None)
        vi = getattr(it, "value", it); vo = getattr(ot, "value", ot)
        if not (isinstance(vi, (int, float)) and isinstance(vo, (int, float)) and vi > 0 and vo > 0):
            raise Unmet(f"{name}: l'adaptateur ne mesure pas les tokens (in={vi!r} out={vo!r})")
    rep_py = repo / "factory_v2" / "token_cost_report.py"
    if not rep_py.is_file():
        raise Unmet("factory_v2/token_cost_report.py absent")
    r = subprocess.run([sys.executable, str(rep_py), "--since-hours", "24", "--json"], cwd=repo,
                       capture_output=True, text=True, timeout=120, env=env)
    if r.returncode != 0:
        raise Unmet(f"token_cost_report rc={r.returncode}: {r.stderr[-300:]}")
    rep = json.loads(r.stdout)
    for k in ("by_milestone", "by_engine", "cost_per_validated_result"):
        if k not in rep:
            raise Unmet(f"rapport sans {k}")
    landed = _landed_ts(repo)
    if landed is not None:
        rows = []
        for pd in sorted((repo / "projects").glob("*/")):
            f = pd / "evidence" / "attempt_accounts.jsonl"
            if f.is_file():
                for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        pass
        term = [x for x in rows if x.get("phase") == "TERMINAL" and str(x.get("engine")) in ("claude", "codex", "glm")
                and _ts(x.get("ended_at")) >= landed]
        if term:
            ok = sum(1 for x in term if str(x.get("input_tokens", "")).isdigit() and str(x.get("output_tokens", "")).isdigit())
            if ok < 0.9 * len(term):
                raise Unmet(f"couverture tokens {ok}/{len(term)} < 90 % des essais terminés depuis l'arrivée du lot")
    br = repo / "factory_v2" / "receipts" / "MATCHED_BENCHMARK_RECEIPT.json"
    if not br.is_file():
        raise Unmet("MATCHED_BENCHMARK_RECEIPT.json absent")
    b = json.loads(br.read_text(encoding="utf-8"))
    for arm, a in (b.get("arms") or {}).items():
        runs = a.get("runs") or []
        if len(runs) < 3 or not all(int(x.get("input_tokens") or 0) + int(x.get("output_tokens") or 0) > 0 for x in runs):
            raise Unmet(f"benchmark: bras {arm} sans tokens mesurés sur 3 runs")
    if "tokens_vs_best_single" not in json.dumps(b.get("verdict") or {}):
        raise Unmet("benchmark: pas de comparaison en tokens factory vs meilleur solo dans le verdict")
    return {"fixtures": sorted(need), "report": "ok", "benchmark_arms": sorted((b.get("arms") or {}).keys())}


def _ts(s) -> float:
    try:
        from datetime import datetime, timezone
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except (TypeError, ValueError):
        return 0.0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project-repo", default=".")
    a = ap.parse_args(argv)
    try:
        r = verify(Path(a.project_repo).resolve())
    except Unmet as exc:
        print(f"TOKEN_TRUTH_UNMET: {exc}")
        return 1
    print(f"TOKEN_TRUTH_VERIFIED: {json.dumps(r)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
