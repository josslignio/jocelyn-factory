#!/usr/bin/env python3
"""budget_ledger — F-NEXT-04 quota/budget accounting.

Standalone JSONL-based per-mission budget tracker.

Law 23 (FACTORY_LAW.md):
  "before EACH run and EACH capacity, preflight quota [...] Chaque capacité a un
  plafond dur tokens ET temps. Dépassement → STOP immédiat + WAITING_INFRA avec
  l'état exact, JAMAIS de burn silencieux."

This module:
  - records each model call (tokens_in, tokens_out, wall_s, engine, step)
  - checks hard limits per-mission; raises BudgetExceeded on overage
  - warns at 80% of any limit
  - writes atomically to a .jsonl sidecar file alongside the receipt

Usage (standalone):
    ledger = BudgetLedger(path="mission_M42_budget.jsonl",
                          max_tokens=50000, max_wall_s=3600)
    ledger.record("M42", engine="glm", step="build_glm",
                  tokens_in=4000, tokens_out=1200, wall_s=45.0)
    status = ledger.status("M42")
    ledger.check("M42")   # raises BudgetExceeded if over limit

Importable:
    from budget_ledger import BudgetLedger, BudgetExceeded, BudgetStatus
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional
from factory_v2._io import _now, _now as _now_iso


COST_PER_TOKEN: dict[str, dict[str, float]] = {
    "glm":    {"in": 0.0000003, "out": 0.0000012},
    "zcode":  {"in": 0.0000003, "out": 0.0000012},
    "claude": {"in": 0.000003,  "out": 0.000015},
    "codex":  {"in": 0.000003,  "out": 0.000015},
}

WARN_THRESHOLD = 0.80


class BudgetExceeded(RuntimeError):
    """Raised when a hard limit is hit — caller must STOP immediately."""
    def __init__(self, mission_id: str, dimension: str, used: float, limit: float):
        self.mission_id = mission_id
        self.dimension = dimension
        self.used = used
        self.limit = limit
        super().__init__(
            f"BUDGET_EXCEEDED mission={mission_id} "
            f"{dimension}={used:.1f}/{limit:.1f} "
            f"— STOP immediately (Law 23)"
        )



@dataclass
class BudgetStatus:
    mission_id: str
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_total: int = 0
    wall_s: float = 0.0
    model_calls: int = 0
    cost_usd: float = 0.0
    warnings: list[str] = field(default_factory=list)
    over_budget: bool = False


def _estimate_cost(engine: str, tokens_in: int, tokens_out: int) -> float:
    rates = COST_PER_TOKEN.get(engine, COST_PER_TOKEN["glm"])
    return tokens_in * rates["in"] + tokens_out * rates["out"]


def _atomic_append(path: Path, record: dict) -> None:
    """Append one record to the JSONL ledger without losing concurrent writes.

    L6_bin_lib_products_1-002: this used to read the whole file and REPLACE it
    (atomic for readers) — but two concurrent records that read the same
    version each rewrote the entire file from their own snapshot, silently
    dropping the other call's line and undercounting tokens/costs. An O_APPEND
    write is atomic for the appended line on local filesystems and cannot lose
    a completed write; `_load_records` already skips a torn in-flight line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


def _load_records(path: Path, mission_id: str) -> list[dict]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("mission_id") == mission_id:
            records.append(r)
    return records


class BudgetLedger:
    def __init__(
        self,
        path: str | Path,
        *,
        max_tokens: Optional[int] = None,
        max_wall_s: Optional[float] = None,
        max_cost_usd: Optional[float] = None,
        max_model_calls: Optional[int] = None,
    ) -> None:
        self.path = Path(path)
        self.max_tokens = max_tokens
        self.max_wall_s = max_wall_s
        self.max_cost_usd = max_cost_usd
        self.max_model_calls = max_model_calls

    def record(
        self,
        mission_id: str,
        *,
        engine: str = "glm",
        step: str = "",
        tokens_in: int = 0,
        tokens_out: int = 0,
        wall_s: float = 0.0,
        model_calls: int = 1,
    ) -> BudgetStatus:
        cost = _estimate_cost(engine, tokens_in, tokens_out)
        rec = {
            "ts": _now_iso(),
            "mission_id": mission_id,
            "engine": engine,
            "step": step,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "wall_s": wall_s,
            "model_calls": model_calls,
            "cost_usd": cost,
        }
        _atomic_append(self.path, rec)
        return self.status(mission_id)

    def status(self, mission_id: str) -> BudgetStatus:
        records = _load_records(self.path, mission_id)
        s = BudgetStatus(mission_id=mission_id)
        for r in records:
            s.tokens_in += r.get("tokens_in", 0)
            s.tokens_out += r.get("tokens_out", 0)
            s.tokens_total = s.tokens_in + s.tokens_out
            s.wall_s += r.get("wall_s", 0.0)
            s.model_calls += r.get("model_calls", 0)
            s.cost_usd += r.get("cost_usd", 0.0)

        warnings: list[str] = []
        over = False

        def _check_dim(used: float, limit: Optional[float], name: str) -> None:
            nonlocal over
            if limit is None:
                return
            if used >= limit:
                over = True
            elif limit > 0 and (used / limit) >= WARN_THRESHOLD:
                warnings.append(f"{name} at {(used / limit) * 100:.0f}% of limit ({used:.1f}/{limit:.1f})")

        _check_dim(s.tokens_total, self.max_tokens, "tokens")
        _check_dim(s.wall_s, self.max_wall_s, "wall_s")
        _check_dim(s.cost_usd, self.max_cost_usd, "cost_usd")
        _check_dim(s.model_calls, self.max_model_calls, "model_calls")

        s.warnings = warnings
        s.over_budget = over
        return s

    def check(self, mission_id: str) -> BudgetStatus:
        """Check limits and raise BudgetExceeded if any hard limit is hit.

        Call this before starting each step (Law 23).
        """
        s = self.status(mission_id)
        if not s.over_budget:
            for w in s.warnings:
                print(f"BUDGET_WARN [{mission_id}] {w}", file=sys.stderr)
            return s

        # find the exceeded dimension and raise
        for used, limit, name in [
            (s.tokens_total, self.max_tokens, "tokens"),
            (s.wall_s,       self.max_wall_s, "wall_s"),
            (s.cost_usd,     self.max_cost_usd, "cost_usd"),
            (s.model_calls,  self.max_model_calls, "model_calls"),
        ]:
            if limit is not None and used >= limit:
                raise BudgetExceeded(mission_id, name, float(used), float(limit))

        raise BudgetExceeded(mission_id, "unknown", 0.0, 0.0)  # should not reach

    def remaining(self, mission_id: str) -> dict[str, float | str]:
        """Return remaining budget per dimension. 'unlimited' when no limit set."""
        s = self.status(mission_id)
        def _rem(used: float, limit: Optional[float]) -> float | str:
            return "unlimited" if limit is None else max(0.0, limit - used)
        return {
            "tokens":      _rem(s.tokens_total, self.max_tokens),
            "wall_s":      _rem(s.wall_s, self.max_wall_s),
            "cost_usd":    _rem(s.cost_usd, self.max_cost_usd),
            "model_calls": _rem(s.model_calls, self.max_model_calls),
        }

    def summary(self, mission_id: str) -> str:
        s = self.status(mission_id)
        r = self.remaining(mission_id)
        lines = [f"Budget summary for {mission_id}:"]
        lines.append(f"  tokens:      {s.tokens_total:>8,d}   remaining: {r['tokens']}")
        lines.append(f"  wall_s:      {s.wall_s:>8.1f}s  remaining: {r['wall_s']}")
        lines.append(f"  cost_usd:    ${s.cost_usd:>7.4f}   remaining: {r['cost_usd']}")
        lines.append(f"  model_calls: {s.model_calls:>8d}   remaining: {r['model_calls']}")
        if s.warnings:
            lines.append("  WARNINGS: " + "; ".join(s.warnings))
        if s.over_budget:
            lines.append("  STATUS: OVER_BUDGET")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="budget_ledger — F-NEXT-04 quota accounting")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("record", help="record a model call")
    r.add_argument("--ledger", required=True)
    r.add_argument("--mission-id", required=True)
    r.add_argument("--engine", default="glm")
    r.add_argument("--step", default="")
    r.add_argument("--tokens-in", type=int, default=0)
    r.add_argument("--tokens-out", type=int, default=0)
    r.add_argument("--wall-s", type=float, default=0.0)

    s = sub.add_parser("status", help="show budget status")
    s.add_argument("--ledger", required=True)
    s.add_argument("--mission-id", required=True)
    s.add_argument("--max-tokens", type=int, default=None)
    s.add_argument("--max-wall-s", type=float, default=None)

    args = ap.parse_args(argv)

    if args.cmd == "record":
        ledger = BudgetLedger(args.ledger)
        status = ledger.record(
            args.mission_id,
            engine=args.engine,
            step=args.step,
            tokens_in=args.tokens_in,
            tokens_out=args.tokens_out,
            wall_s=args.wall_s,
        )
        print(f"recorded: tokens={status.tokens_total} wall_s={status.wall_s:.1f} "
              f"cost_usd=${status.cost_usd:.4f}")
        return 0

    if args.cmd == "status":
        ledger = BudgetLedger(
            args.ledger,
            max_tokens=args.max_tokens,
            max_wall_s=args.max_wall_s,
        )
        print(ledger.summary(args.mission_id))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
