"""routing_aggregator — agrège les RunRecords en statistiques de routage.

ADAPTIVE_ROUTING_AGGREGATOR_BUILT : construction seule, aucune bascule du
routage production. Le jalon ADAPTIVE_ROUTING consommera ce summary après les
benchmarks ; ce module ne fait que LIRE, agréger et ÉCRIRE.

Sources (les deux sources d'observation préexistantes du dépôt, rien d'autre) :

  * le grand-livre RunRecords deux-phases — paires PREDICTION/OBSERVATION du
    fichier historiquement nommé ``factory_v2/receipts/runrecords.jsonl``. Ce
    fichier vit hors du dépôt depuis FACTORY_STATE_OUT_OF_REPO_V1 et est résolu
    par ``factory_v2.runrecords._runrecords_path()`` (env
    ``FACTORY_RUNRECORDS_PATH`` > run-dir) : ce module RÉUTILISE ce résolveur,
    donc lit exactement le grand-livre que ``engine_stats()`` et
    ``verify_adaptive_routing`` lisent ;
  * ``projects/*/evidence/attempt_accounts.jsonl`` — UNIQUEMENT pour les
    projets du portfolio (``portfolio.json``, schema ``portfolio/v1``). Tout
    record d'un projet hors portfolio est exclu et compté : un projet de test
    ne doit jamais entrer dans les stats de routage.

Réutilisation (pas de duplication) :

  * ``runrecords.engine_stats()`` fournit la section ``engines`` telle quelle ;
  * ``resource_accounting.read_rows``/``fold`` replient attempt_accounts
    (append-only deux-phases : l'identité vit sur la ligne OPEN, le verdict
    validé sur une ligne ultérieure partageant le attempt_key) ;
  * les coordonnées de routage suivent ``routing_evidence.py`` : lignes
    TERMINAL portant spec_level ET task_kind toutes deux, engine connu et
    distinct de ``none`` ; le succès est ``independently_validated`` — les
    claims du builder (``mission_pass`` seul) ne comptent JAMAIS ;
  * la calibration suit ``verify_adaptive_routing._calibration`` :
    SUCCESS_OUTCOMES = {PASS, ACCEPTED, COMPLETE}.

Sortie ``routing_stats/routing_summary.json`` — schema
``routing-aggregator-summary/v1``, par (engine, spec_level, task_kind) :
lancements, succès validés (dénominateur ``independently_validated``), taux,
tokens et secondes médians, dernière observation, couverture de mesure.
Écriture atomique (tmp+fsync+os.replace+fsync dir via ``factory_v2._io``).

Déterminisme : aucun horodatage mur dans la sortie — le summary décrit les
données, pas le moment de l'écriture. Même entrée → mêmes octets.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from pathlib import Path

from . import runrecords
from ._io import _atomic_write
from .launch_gate import ENGINE_NONE_REFUSED
from .resource_accounting import fold

SUMMARY_SCHEMA = "routing-aggregator-summary/v1"

#: Outcomes that count as success for prediction calibration — same pinned set
#: as verify_adaptive_routing (an imported constant would couple an aggregator
#: to a verifier; the set is pinned in both and asserted equal by tests below).
SUCCESS_OUTCOMES = frozenset({"PASS", "ACCEPTED", "COMPLETE"})

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Engines that are statements about routing GATES, not about an engine's
#: ability (see routing_evidence.py): never evidence for or against a model.
_ENGINES_NEVER_EVIDENCE = frozenset({"", "UNKNOWN", ENGINE_NONE_REFUSED})


def _read_json_lines(path) -> tuple[list[dict], int, int]:
    """Parse a JSONL ledger defensively: (dict rows, unreadable, non_object).

    A torn or corrupt line is not a fact — dropped and COUNTED (the caller
    reports it in ``unreadable``). A valid-JSON line that is not an object
    (``42``, ``[1]``) is dropped and counted separately: ``fold`` and
    ``runrecords`` readers assume dicts, so feeding them one would crash the
    whole aggregation on a single hostile line.
    """
    path = Path(path)
    if not path.is_file():
        return [], 0, 0
    rows: list[dict] = []
    unreadable = 0
    non_object = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            unreadable += 1
            continue
        if not isinstance(rec, dict):
            non_object += 1
            continue
        rows.append(rec)
    return rows, unreadable, non_object


def _measured(value):
    """A real measurement, or None. Strings ("UNKNOWN"), bools, NaN, inf and
    negatives are not measurements — they are reported as unmeasured, never
    averaged in as numbers (same rule as runrecords._measured_cost)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    v = float(value)
    if not math.isfinite(v) or v < 0:
        return None
    return v


def _norm_ts(ts: str) -> str:
    """Normalize trailing +00:00 to Z so max() compares like with like."""
    return ts[:-6] + "Z" if ts.endswith("+00:00") else ts


def _portfolio_entries(portfolio_path) -> tuple[list[tuple[Path, str]], list[str]]:
    """([(project_dir, project_name)], warnings) from a portfolio/v1 file.

    Fail-soft BY DESIGN for an aggregator: a missing or malformed portfolio
    yields an empty list plus a warning — the summary is data description, not
    a control-plane gate, so it must describe "no project included" rather than
    crash. Name = basename of project_dir, the identifier attempt_accounts
    rows carry in their ``project`` field.
    """
    p = Path(portfolio_path)
    if not p.is_file():
        return [], [f"portfolio_unreadable: {p} not found — no project included"]
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [], [f"portfolio_unreadable: {exc}"]
    if not isinstance(data, dict) or data.get("schema") != "portfolio/v1":
        return [], ["portfolio_unreadable: expected schema 'portfolio/v1'"]
    entries = data.get("projects")
    if not isinstance(entries, list):
        return [], ["portfolio_unreadable: 'projects' must be a list"]
    out: list[tuple[Path, str]] = []
    seen: set[str] = set()
    root = p.parent
    for e in entries:
        d = e.get("project_dir") if isinstance(e, dict) else None
        if not d or not str(d).strip():
            continue
        name = Path(str(d)).name
        if not name or name in seen:
            continue
        seen.add(name)
        out.append((root / str(d), name))
    return out, []


def _empty_bucket() -> dict:
    return {"launches": 0, "independently_validated": 0, "validated_successes": 0,
            "durations": [], "input_tokens": [], "output_tokens": [],
            "tokens_measured": 0, "last_observation": ""}


def _accumulate(buckets: dict, engine: str, spec_level: str, task_kind: str,
                row: dict) -> None:
    key = (engine, spec_level, task_kind)
    b = buckets.setdefault(key, _empty_bucket())
    b["launches"] += 1
    if row.get("independently_validated") is True:
        b["independently_validated"] += 1
        if row.get("mission_pass") is True:
            b["validated_successes"] += 1
    d = _measured(row.get("duration_s"))
    if d is not None:
        b["durations"].append(d)
    tin = _measured(row.get("input_tokens"))
    tout = _measured(row.get("output_tokens"))
    if tin is not None:
        b["input_tokens"].append(tin)
    if tout is not None:
        b["output_tokens"].append(tout)
    if tin is not None or tout is not None:
        b["tokens_measured"] += 1
    ts = _norm_ts(str(row.get("ended_at")
                      or row.get("validated_at")
                      or row.get("started_at")
                      or ""))
    if ts and ts > b["last_observation"]:
        b["last_observation"] = ts


def _finalize_bucket(b: dict) -> dict:
    launches = b["launches"]
    validated = b["independently_validated"]
    return {
        "launches": launches,
        "independently_validated": validated,
        "validated_successes": b["validated_successes"],
        "success_rate": (round(b["validated_successes"] / validated, 4)
                         if validated else None),
        "median_duration_s": (round(statistics.median(b["durations"]), 3)
                              if b["durations"] else None),
        "median_input_tokens": (round(statistics.median(b["input_tokens"]), 3)
                                if b["input_tokens"] else None),
        "median_output_tokens": (round(statistics.median(b["output_tokens"]), 3)
                                 if b["output_tokens"] else None),
        "last_observation": b["last_observation"],
        "measurement_coverage": {
            "duration_measured": len(b["durations"]),
            "tokens_measured": b["tokens_measured"],
            "duration_coverage": round(len(b["durations"]) / launches, 4) if launches else 0.0,
            "tokens_coverage": round(b["tokens_measured"] / launches, 4) if launches else 0.0,
        },
    }


def _calibration(rows: list[dict]) -> dict:
    """Prediction calibration over real PREDICTION/OBSERVATION pairs.

    Same rule as verify_adaptive_routing._calibration: an attempt counts when
    BOTH phases exist; a prediction is correct when predicted success matches
    observed success (SUCCESS_OUTCOMES). Orphan predictions (crash between the
    phases) contribute nothing — their outcome was never observed.
    """
    phases: dict[str, dict] = {}
    for r in rows:
        if r.get("phase") in ("PREDICTION", "OBSERVATION") and r.get("attempt_id"):
            phases.setdefault(str(r["attempt_id"]), {})[r["phase"]] = r
    n = correct = 0
    for pair in phases.values():
        pred, obs = pair.get("PREDICTION"), pair.get("OBSERVATION")
        if not pred or not obs:
            continue
        n += 1
        predicted_ok = (pred.get("prediction") or {}).get("outcome") in SUCCESS_OUTCOMES
        observed_ok = (obs.get("observation") or {}).get("outcome") in SUCCESS_OUTCOMES
        if predicted_ok == observed_ok:
            correct += 1
    return {"n_predictions": n, "n_correct": correct}


def _engine_stats(runrecords_path: Path | None) -> dict:
    """runrecords.engine_stats() over the SAME ledger we read.

    engine_stats() resolves its path via FACTORY_RUNRECORDS_PATH internally;
    an explicitly passed ledger is therefore injected through that sanctioned
    override (restored afterwards) — never a duplicated code path.
    """
    if runrecords_path is None:
        return runrecords.engine_stats()
    old = os.environ.get("FACTORY_RUNRECORDS_PATH")
    os.environ["FACTORY_RUNRECORDS_PATH"] = str(runrecords_path)
    try:
        return runrecords.engine_stats()
    finally:
        if old is None:
            os.environ.pop("FACTORY_RUNRECORDS_PATH", None)
        else:
            os.environ["FACTORY_RUNRECORDS_PATH"] = old


def build_summary(repo_root=None, runrecords_path=None,
                  portfolio_path=None) -> dict:
    """Aggregate both observation sources into a routing summary dict.

    Pure function of its inputs (no wall clock): same ledgers + portfolio →
    the same dict, byte for byte once serialized.
    """
    root = Path(repo_root) if repo_root else REPO_ROOT
    ledger = Path(runrecords_path) if runrecords_path else runrecords._runrecords_path()
    pp = Path(portfolio_path) if portfolio_path else root / "portfolio.json"

    entries, warnings = _portfolio_entries(pp)
    names = {name for _, name in entries}

    buckets: dict[tuple[str, str, str], dict] = {}
    unreadable = {"runrecords": 0, "attempt_accounts": 0}
    exclusions = {"non_portfolio_rows": 0, "non_terminal": 0,
                  "missing_routing_coordinates": 0, "engine_excluded": 0,
                  "non_object_lines": 0}

    for pdir, _name in entries:
        rows, unread, nonobj = _read_json_lines(
            pdir / "evidence" / "attempt_accounts.jsonl")
        unreadable["attempt_accounts"] += unread
        exclusions["non_object_lines"] += nonobj
        for row in fold(rows):
            if row.get("project") not in names:
                # a test/ghost project's record — never routing evidence
                exclusions["non_portfolio_rows"] += 1
                continue
            if row.get("phase") != "TERMINAL":
                exclusions["non_terminal"] += 1
                continue
            engine = row.get("engine")
            if not isinstance(engine, str) or engine in _ENGINES_NEVER_EVIDENCE:
                exclusions["engine_excluded"] += 1
                continue
            spec, kind = row.get("spec_level"), row.get("task_kind")
            if not spec or not kind:
                # a row this ledger cannot classify is evidence for NO
                # (spec_level, task_kind) — same rule as routing_evidence.
                exclusions["missing_routing_coordinates"] += 1
                continue
            _accumulate(buckets, engine, str(spec), str(kind), row)

    rr_rows, rr_unread, rr_nonobj = _read_json_lines(ledger)
    unreadable["runrecords"] = rr_unread
    exclusions["non_object_lines"] += rr_nonobj

    try:
        engines = _engine_stats(Path(runrecords_path) if runrecords_path else None)
    except Exception as exc:  # a hostile ledger must not kill the summary
        engines = {}
        warnings.append(f"engine_stats_failed: {type(exc).__name__}: {exc}")

    nested: dict[str, dict[str, dict[str, dict]]] = {}
    last = ""
    for (engine, spec, kind) in sorted(buckets):
        final = _finalize_bucket(buckets[(engine, spec, kind)])
        nested.setdefault(engine, {}).setdefault(spec, {})[kind] = final
        if final["last_observation"] and final["last_observation"] > last:
            last = final["last_observation"]

    return {
        "schema": SUMMARY_SCHEMA,
        "runrecords_path": str(ledger),
        "portfolio": [name for _, name in entries],
        "buckets": nested,
        "engines": engines,
        "prediction_calibration": _calibration(rr_rows),
        "unreadable": unreadable,
        "exclusions": exclusions,
        "data_last_observation": last,
        "warnings": warnings,
    }


def write_summary(summary: dict, out_path) -> Path:
    """Atomic write (tmp+fsync+os.replace+dir fsync — append-safe readers never
    observe a half-written summary; a crash leaves the previous file intact)."""
    out = Path(out_path)
    _atomic_write(out, summary)
    return out


def aggregate(repo_root=None, runrecords_path=None, portfolio_path=None,
              out_path=None) -> Path:
    """Build and write routing_stats/routing_summary.json; returns its path."""
    root = Path(repo_root) if repo_root else REPO_ROOT
    out = Path(out_path) if out_path else root / "routing_stats" / "routing_summary.json"
    return write_summary(build_summary(repo_root=root,
                                       runrecords_path=runrecords_path,
                                       portfolio_path=portfolio_path), out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="routing_aggregator",
        description="Aggregate RunRecords + attempt accounts into "
                    "routing_stats/routing_summary.json (read-only inputs).")
    ap.add_argument("--repo-root", default=None,
                    help="repo root for default portfolio.json and output path")
    ap.add_argument("--out", default=None,
                    help="output path (default <repo-root>/routing_stats/routing_summary.json)")
    ap.add_argument("--runrecords", default=None,
                    help="runrecords ledger path (default: runrecords._runrecords_path())")
    ap.add_argument("--portfolio", default=None,
                    help="portfolio.json path (default <repo-root>/portfolio.json)")
    args = ap.parse_args(argv)
    summary = build_summary(repo_root=args.repo_root, runrecords_path=args.runrecords,
                            portfolio_path=args.portfolio)
    for w in summary.get("warnings", []):
        print(f"warning: {w}", file=sys.stderr)
    out = aggregate(repo_root=args.repo_root, runrecords_path=args.runrecords,
                    portfolio_path=args.portfolio, out_path=args.out)
    try:
        print(out)  # stdout: the output path, data only — diagnostics on stderr
        sys.stdout.flush()
    except BrokenPipeError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
