"""Independent base-readiness aggregator — FACTORY_AUTONOMOUS_CTO_BASE_READY.

The milestone's acceptance is eleven Operating-Model-015 runtime counters
(projects/factory/STRATEGY.md "Compteurs de readiness finaux"; ROADMAP acceptance
`MANUAL_DAEMON_RESTARTS=0 ... SAFE_SELF_IMPROVEMENT_DECISIONS>=1`) plus a receipt
"produit par un vérificateur indépendant, jamais auto-attesté".  No authored
boolean may move any of them, so this module RECOMPUTES each counter from the
durable evidence the dependency milestones produced, through the independence
channels that already exist — it invents none:

  * IC1  settlement_truth.verify_settlement()  — a post-ledger settlement only
         covers a metric when it re-proves (contract integrity, hash-verified
         mission receipts, lineage, control pin);
  * IC2  the git-tracked settlement ledger (LEDGER_INDEX.json file_sha256) — the
         historical channel for settlements that predate the contract substrate,
         same pre-ledger exemption settlement_truth.settlement_blockers applies;
  * IC3  benchmark MANIFEST sha256 per raw observation — aggregates are recomputed
         from run files, never read from a receipt's own aggregate block;
  * IC4  review_accounts rows: HMAC owner seal (review_dispatch.verify_seal) AND
         a matching supervised launch in the attempt ledger — a row failing
         either is not counted (CODEX-LOT-C37/C39);
  * IC5  git durability — a durable record must be the committed blob, byte for
         byte; anything rewritten after commit is not evidence.

Contract fixed by the task brief, enforced structurally:

  * every metric NAMES its source records and an EXPLICIT observation window;
  * a metric with no independent source is UNKNOWN, and UNKNOWN blocks readiness;
  * `0` always means "0 observed over the named window", never "risk-free" — a
    vacuous window (nothing observable happened in it) is UNKNOWN, not 0;
  * a metric read from a receipt the auditee wrote without one of the channels
    above does not count; a missing dependency receipt yields UNKNOWN, never 0;
  * the aggregate this module returns (and the receipt it writes) is the OUTPUT
    of the recompute — it is never an input; an authored aggregate cannot
    influence grading because grading never reads the receipt.

Read-only by construction except write_receipt(), which the verifier calls on
its own recomputation.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from factory_v2._io import _now

MILESTONE = "FACTORY_AUTONOMOUS_CTO_BASE_READY"
RECEIPT_REL = "factory_v2/receipts/FACTORY_BASE_READY_RECEIPT.json"
LEDGER_INDEX_REL = "factory_v2/receipts/settlements/LEDGER_INDEX.json"
SETTLEMENTS_REL = "factory_v2/receipts/settlements"
BENCH_REL = "factory_v2/benchmarks/CLAUDE_ALONE_VS_GLM_ALONE_VS_FACTORY_BENCHMARK_V1"
SUPERVISOR_RECEIPT_REL = "factory_v2/receipts/committed/SUPERVISOR_RUNTIME_RECEIPT.json"
TRANSITIONS_REL = "factory_v2/receipts/audit_cells/transitions.jsonl"
AUDIT_CELLS_REL = "factory_v2/receipts/audit_cells"
SELF_MOD_LEDGER_REL = "factory_v2/receipts/self_modification_ledger.jsonl"
BENCHMARK_RECEIPT_REL = "factory_v2/receipts/BENCHMARK_RECEIPT.json"

#: name -> (comparator, bound). The ROADMAP acceptance is the authority; the
#: twelfth STRATEGY counter (PARALLEL_PRODUCTS_PROVEN) is scaling, explicitly
#: excluded from base readiness by the roadmap's product_definition note.
EXPECTED: dict[str, tuple[str, int]] = {
    "MANUAL_DAEMON_RESTARTS": ("==", 0),
    "MANUAL_AGENT_HANDOFFS": ("==", 0),
    "MANUAL_AUDIT_TRIGGERS": ("==", 0),
    "ROUTINE_HUMAN_INTERVENTIONS": ("==", 0),
    "FALSE_SETTLEMENTS": ("==", 0),
    "CONTEXT_REEXPLANATIONS": ("==", 0),
    "EXACT_SHA_REVIEWS_AUTOMATED": (">=", 1),
    "MULTI_AI_DECISIONS_SYNTHESIZED": (">=", 1),
    "MATCHED_BENCHMARK_RUNS_PER_ARM": (">=", 3),
    "FACTORY_ADVANTAGE_MEASURED": ("==", 1),
    "SAFE_SELF_IMPROVEMENT_DECISIONS": (">=", 1),
}

SATISFIED = "SATISFIED"
VIOLATED = "VIOLATED"
UNKNOWN = "UNKNOWN"

ARMS = ("claude_alone", "glm_alone", "factory")
SINGLES = ("claude_alone", "glm_alone")


def _load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _git(repo: Path, *args: str):
    try:
        return subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                              text=True, timeout=60)
    except Exception:
        return None


def _head_sha(repo: Path) -> str:
    r = _git(repo, "rev-parse", "HEAD")
    return r.stdout.strip() if r and r.returncode == 0 else ""


def _git_intact(repo: Path, rel: str) -> bool:
    """IC5: the file on disk is byte-identical to its committed HEAD blob.

    A durable record that was rewritten after commit (or never committed) is a
    claim, not evidence.
    """
    p = repo / rel
    if not p.is_file():
        return False
    r = _git(repo, "show", f"HEAD:{rel}")
    if r is None or r.returncode != 0:
        return False
    return p.read_bytes() == r.stdout.encode("utf-8", "surrogateescape")


@dataclass
class MetricResult:
    name: str
    expected: str
    value: int | None = None
    status: str = UNKNOWN
    window: str = ""
    sources: list = field(default_factory=list)
    dependency: str = ""
    coverage: dict = field(default_factory=dict)
    reason: str = ""
    detail: str = ""

    def satisfied(self, value: int) -> None:
        cmp_, bound = EXPECTED[self.name]
        self.value = value
        self.status = (SATISFIED if (value == bound if cmp_ == "==" else value >= bound)
                       else VIOLATED)

    def unknown(self, reason: str) -> None:
        self.value = None
        self.status = UNKNOWN
        self.reason = reason

    def as_dict(self) -> dict:
        d = {
            "expected": self.expected,
            "value": self.value,
            "status": self.status,
            "window": self.window,
            "sources": self.sources,
            "zero_semantics": ("0 means 0 observed over the named window, "
                               "never risk-free"),
        }
        if self.dependency:
            d["dependency"] = self.dependency
            d["coverage"] = self.coverage
        if self.reason:
            d["reason"] = self.reason
        if self.detail:
            d["detail"] = self.detail
        return d


def _sources_for(name: str) -> list:
    """Source records each metric names UP FRONT — even when it lands UNKNOWN,
    the aggregate must still say WHERE the evidence would have come from."""
    shared = {
        "MANUAL_DAEMON_RESTARTS": (SUPERVISOR_RECEIPT_REL,
                                   "git-intact at HEAD (IC5); the recorded window "
                                   "must contain a forced kill"),
        "MANUAL_AGENT_HANDOFFS": (TRANSITIONS_REL,
                                  "git-intact at HEAD (IC5), rows recounted"),
        "MANUAL_AUDIT_TRIGGERS": (f"{AUDIT_CELLS_REL}/** (audit-cell-record/v1)",
                                  "git-intact at HEAD (IC5), manual_triggers "
                                  "recounted"),
        "ROUTINE_HUMAN_INTERVENTIONS": (f"{BENCH_REL}/MANIFEST.json + run observations",
                                        "per-file sha256 vs MANIFEST (IC3), "
                                        "human_interventions summed from raw runs"),
        "FALSE_SETTLEMENTS": ("projects/factory/PROJECT_STATE.json + "
                              "factory_v2/settlement_truth.py",
                              "IC1: every post-cutoff COMPLETE re-verified"),
        "CONTEXT_REEXPLANATIONS": (f"{AUDIT_CELLS_REL}/** (position fingerprints) + "
                                   f"{TRANSITIONS_REL}",
                                   "git-intact at HEAD (IC5); drift across a "
                                   "handoff or a manual transition counts"),
        "EXACT_SHA_REVIEWS_AUTOMATED": ("projects/factory/evidence/review_accounts.jsonl "
                                        "+ attempt_accounts.jsonl",
                                        "HMAC owner seal (IC4) + supervised-launch "
                                        "cross-check"),
        "MULTI_AI_DECISIONS_SYNTHESIZED": (f"{AUDIT_CELLS_REL}/** (cell-synthesis/v1)",
                                           "git-intact at HEAD (IC5)"),
        "MATCHED_BENCHMARK_RUNS_PER_ARM": (f"{BENCH_REL}/MANIFEST.json + observations",
                                           "per-arm run counts recounted from raw "
                                           "files (IC3)"),
        "FACTORY_ADVANTAGE_MEASURED": (f"{BENCH_REL}/MANIFEST.json + observations",
                                       "verified_rate/false_acceptances recomputed "
                                       "per arm from raw runs (IC3)"),
        "SAFE_SELF_IMPROVEMENT_DECISIONS": (SELF_MOD_LEDGER_REL,
                                            "git-intact at HEAD (IC5); a row counts "
                                            "only when validated AND sha256-bound to "
                                            + BENCHMARK_RECEIPT_REL),
    }
    record, verification = shared[name]
    return [{"record": record, "verification": verification}]


def _coverage(repo: Path, project_dir: Path, milestone: str,
              state: dict | None = None) -> dict:
    """Is this dependency milestone's settlement independently coverable?

    Post `introduced_at` (the settlement ledger's own birth): IC1 only —
    verify_settlement must re-prove it. Pre-ledger: IC2 — a ledger entry whose
    recorded file hash still matches the settlement file on disk.
    """
    out = {"milestone": milestone, "covered": False, "channel": "", "detail": ""}
    state = state if state is not None else (_load_json(project_dir / "PROJECT_STATE.json") or {})
    rec = (state.get("completed_milestones", {}) or {}).get(milestone)
    if not isinstance(rec, dict) or rec.get("state") != "COMPLETE":
        out["detail"] = "DEPENDENCY_RECEIPT_MISSING (not recorded COMPLETE)"
        return out
    index = _load_json(repo / LEDGER_INDEX_REL) or {}
    cutoff = str(index.get("introduced_at", ""))
    settled_at = str(rec.get("settled_at", ""))
    if cutoff and settled_at and settled_at < cutoff:
        entry = (index.get("entries", {}) or {}).get(milestone)
        spath = repo / SETTLEMENTS_REL / f"{milestone}.json"
        if not isinstance(entry, dict) or not spath.is_file():
            out["detail"] = "PRE_LEDGER_SETTLEMENT_NOT_IN_LEDGER"
            return out
        digest = hashlib.sha256(spath.read_bytes()).hexdigest()
        if digest != entry.get("file_sha256"):
            out["detail"] = "PRE_LEDGER_SETTLEMENT_TAMPERED_VS_INDEX"
            return out
        entry_json = _load_json(spath) or {}
        if not (entry_json.get("evidence", {}) or {}).get("active_verify"):
            out["detail"] = "PRE_LEDGER_SETTLEMENT_HAS_NO_ACTIVE_VERIFY"
            return out
        out.update(covered=True, channel="settlement_ledger_pre_cutoff",
                   detail=f"ledger entry file_sha256 verified ({digest[:12]})")
        return out
    try:
        from .settlement_truth import verify_settlement
        v = verify_settlement(project_dir, milestone, state=state,
                              run_acceptance=False)
        if v.proven:
            out.update(covered=True, channel="settlement_truth",
                       detail=f"verify_settlement proven (contract {v.contract_id})")
        else:
            out["detail"] = "UNPROVEN_BY_SETTLEMENT_TRUTH: " + ";".join(v.reasons[:2])
    except Exception as exc:                                   # noqa: BLE001
        out["detail"] = f"SETTLEMENT_TRUTH_UNEVALUABLE:{type(exc).__name__}:{exc}"[:200]
    return out


def _require(coverage: dict) -> str:
    return "" if coverage.get("covered") else (
        f"dependency receipt not independently covered: {coverage.get('detail', '?')}")


def _cell_records(repo: Path) -> list[tuple[Path, dict]]:
    """(path, record) for every audit-cell record on disk (schema-filtered)."""
    out = []
    root = repo / AUDIT_CELLS_REL
    if not root.is_dir():
        return out
    for p in sorted(root.rglob("*.json")):
        d = _load_json(p)
        if isinstance(d, dict) and d.get("schema") == "audit-cell-record/v1":
            out.append((p, d))
    return out


def _intact_cells(repo: Path, cell: list) -> list[tuple[Path, dict]]:
    return [(p, d) for p, d in cell
            if _git_intact(repo, p.relative_to(repo).as_posix())]


def _transitions(repo: Path) -> list[dict]:
    rows = []
    p = repo / TRANSITIONS_REL
    if not p.is_file():
        return rows
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _benchmark_observations(repo: Path) -> tuple[dict, list[dict], list[str]]:
    """(manifest, verified observations, excluded-because-hash-mismatch names)."""
    root = repo / BENCH_REL
    manifest = _load_json(root / "MANIFEST.json")
    obs: list[dict] = []
    excluded: list[str] = []
    if not isinstance(manifest, dict):
        return {}, obs, excluded
    for name, expected_sha in sorted((manifest.get("observations") or {}).items()):
        p = root / name
        if not p.is_file():
            excluded.append(f"{name}:ABSENT")
            continue
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        if digest != expected_sha:
            excluded.append(f"{name}:HASH_MISMATCH")
            continue
        d = _load_json(p)
        if isinstance(d, dict):
            obs.append(d)
    return manifest, obs, excluded


def _arm_stats(obs: list[dict]) -> dict[str, dict]:
    stats: dict[str, dict] = {}
    for arm in ARMS:
        runs = [o for o in obs if o.get("arm") == arm]
        verified = sum(1 for o in runs
                       if o.get("declared_success") is True and o.get("ground_truth_rc") == 0)
        false_acc = sum(1 for o in runs
                        if o.get("declared_success") is True and o.get("ground_truth_rc") != 0)
        human = sum(int(o.get("human_interventions") or 0) for o in runs)
        stats[arm] = {"runs": len(runs), "verified": verified,
                      "false_acceptances": false_acc, "human_interventions": human}
    return stats


def _metric_manual_daemon_restarts(repo: Path, m: MetricResult) -> None:
    receipt = _load_json(repo / SUPERVISOR_RECEIPT_REL)
    if not isinstance(receipt, dict) or not _git_intact(repo, SUPERVISOR_RECEIPT_REL):
        m.unknown("SUPERVISOR_RUNTIME_RECEIPT absent or not git-intact — "
                  "no durable record")
        return
    phases = receipt.get("phases", {}) or {}
    kills = [k for k in ("worker_crash", "supervisor_crash") if phases.get(k)]
    forced = any(phases.get(k, {}) and any(str(key).startswith("forced_sigkill")
                                           for key in phases[k]) for k in kills)
    if not forced:
        m.unknown("VACUOUS_WINDOW: no forced kill inside the recorded window — "
                  "a 0 with nothing to observe is not the SPEC §1 counter")
        return
    value = receipt.get("MANUAL_DAEMON_RESTARTS")
    if not isinstance(value, int) or isinstance(value, bool):
        m.unknown("MANUAL_DAEMON_RESTARTS not an integer in the durable record")
        return
    gen = str(receipt.get("generated_at", "?"))
    m.window = (f"supervisor runtime exercise ending {gen}, containing forced "
                f"SIGKILLs ({', '.join(kills)}); manual daemon restarts observed "
                f"over that window: {value}")
    m.detail = (f"KILL_RESTART_RECOVERY={receipt.get('KILL_RESTART_RECOVERY')}, "
                f"DUPLICATE_WRITERS={receipt.get('DUPLICATE_WRITERS')}")
    m.satisfied(value)


def _metric_manual_agent_handoffs(repo: Path, m: MetricResult,
                                  transitions: list) -> None:
    if not _git_intact(repo, TRANSITIONS_REL):
        m.unknown("transitions ledger absent or not git-intact — nothing "
                  "durable to recount")
        return
    if not transitions:
        m.unknown("EMPTY_LEDGER: zero transition rows observed — 0 handoffs "
                  "over an empty window observes nothing")
        return
    manual = sum(1 for t in transitions if t.get("manual") is True)
    first = str(transitions[0].get("recorded_at", "?"))
    last = str(transitions[-1].get("recorded_at", "?"))
    m.window = (f"{len(transitions)} durable cell transitions {first} → {last}; "
                f"manual agent handoffs observed over that window: {manual}")
    m.satisfied(manual)


def _metric_manual_audit_triggers(repo: Path, m: MetricResult,
                                  cell: list) -> None:
    intact = _intact_cells(repo, cell)
    if not intact:
        m.unknown("no git-intact audit-cell record — no trigger window to observe")
        return
    triggers_total = 0
    triggers_manual = 0
    for _, d in intact:
        triggers_total += 1
        if d.get("trigger") in (None, "", "MANUAL"):
            triggers_manual += 1
        triggers_manual += int(d.get("manual_triggers") or 0)
    if triggers_total < 1:
        m.unknown("no trigger record observed — 0 triggers over an empty "
                  "window observes nothing")
        return
    first = min(str(d.get("triggered_at", "?")) for _, d in intact)
    last = max(str(d.get("completed_at", "?")) for _, d in intact)
    m.window = (f"{triggers_total} cell activations {first} → {last}, each "
                f"trigger durable and automated; manual audit triggers observed "
                f"over that window: {triggers_manual}")
    m.satisfied(triggers_manual)


def _metric_routine_human_interventions(repo: Path, m: MetricResult) -> None:
    manifest, obs, excluded = _benchmark_observations(repo)
    if excluded:
        m.sources.append({"record": BENCH_REL,
                          "verification": f"excluded (hash mismatch/absent): "
                                          f"{excluded}"})
    if not obs:
        m.unknown("no hash-verified benchmark observation — no observation window")
        return
    stats = _arm_stats(obs)
    human = sum(s["human_interventions"] for s in stats.values())
    runs = sum(s["runs"] for s in stats.values())
    m.window = (f"matched 3-arm benchmark {manifest.get('workload_id', '?')} "
                f"({runs} runs across {len(stats)} arms, faults "
                f"{', '.join(manifest.get('faults', []) or [])}); routine human "
                f"interventions observed over that window: {human}")
    m.detail = f"per-arm runs: { {a: s['runs'] for a, s in stats.items()} }"
    m.satisfied(human)


def _metric_false_settlements(repo: Path, project_dir: Path, m: MetricResult) -> None:
    index = _load_json(repo / LEDGER_INDEX_REL) or {}
    cutoff = str(index.get("introduced_at", ""))
    state = _load_json(project_dir / "PROJECT_STATE.json") or {}
    if not cutoff:
        m.unknown("settlement ledger has no introduced_at — cutoff undefined")
        return
    from .settlement_truth import verify_settlement
    unproven = []
    checked = 0
    for mid, rec in sorted((state.get("completed_milestones", {}) or {}).items()):
        if not (isinstance(rec, dict) and rec.get("state") == "COMPLETE"):
            continue
        if str(rec.get("settled_at", "")) < cutoff:
            continue
        checked += 1
        v = verify_settlement(project_dir, mid, state=state, run_acceptance=False)
        if not v.proven:
            unproven.append(f"{mid}({'/'.join(v.reasons[:1])})")
    # Reference the enclosing aggregate/receipt timestamp so unchanged evidence
    # yields identical metrics even when recomputations cross a clock tick.
    m.window = (f"[{cutoff} → produced_at (aggregate/receipt timestamp)] — "
                f"{checked} post-cutoff COMPLETE "
                f"settlements re-verified; false settlements observed over that "
                f"window: {len(unproven)}")
    m.detail = ("unproven: " + "; ".join(unproven)) if unproven else "all proven"
    m.satisfied(len(unproven))


def _metric_context_reexplanations(repo: Path, m: MetricResult,
                                   cell: list, transitions: list) -> None:
    intact = _intact_cells(repo, cell)
    if not intact:
        m.unknown("no git-intact audit-cell record — handoff positions unobservable")
        return
    drifted = [str(d.get("cell_id", "?")) for _, d in intact
               if d.get("position_fingerprint_before") != d.get("position_fingerprint_after")]
    manual = sum(1 for t in transitions if t.get("manual") is True)
    first = min(str(d.get("triggered_at", "?")) for _, d in intact)
    last = max(str(d.get("completed_at", "?")) for _, d in intact)
    m.window = (f"{len(intact)} cell activations {first} → {last}: every redirect "
                f"must resume at the exact prior position fingerprint; context "
                f"re-explanations observed over that window: {len(drifted) + manual}")
    m.detail = f"position drift: {drifted or 'none'}; manual transitions: {manual}"
    m.satisfied(len(drifted) + manual)


def _metric_exact_sha_reviews(repo: Path, project_dir: Path, m: MetricResult) -> None:
    from . import review_dispatch
    from .audit_observation_producer import _review_account_supervised
    if not review_dispatch.owner_key_available():
        m.unknown("REVIEW_SEAL_KEY_MISSING: no owner key in this process, so no "
                  "row can be proven — unprovable by construction, not 0")
        return
    rows = review_dispatch.read_reviews(project_dir)
    proven = []
    rejected = []
    for r in rows:
        if not (r.get("read_only") is True and r.get("reviewed_sha") and r.get("verdict")):
            continue
        if not review_dispatch.verify_seal(r):
            rejected.append(f"{r.get('engine')}:UNSEALED")
            continue
        if not _review_account_supervised(r, project_dir):
            rejected.append(f"{r.get('engine')}:NO_SUPERVISED_LAUNCH")
            continue
        proven.append(r)
    if not proven and not rows:
        m.unknown("EMPTY_LEDGER: no review rows at all — 0 reviews over an "
                  "empty window observes nothing")
        return
    starts = [str(r.get("started_at")) for r in proven if r.get("started_at")]
    ends = [str(r.get("ended_at")) for r in proven if r.get("ended_at")]
    window = (f"{min(starts)} → {max(ends)}" if starts and ends else "recorded rows")
    m.window = (f"{len(proven)} proven exact-SHA reviews ({window}); unproven "
                f"rows excluded: {rejected or 'none'}")
    m.detail = ", ".join(sorted({f"{r.get('milestone')}@{str(r.get('reviewed_sha'))[:12]}"
                                 f":{r.get('verdict')}({r.get('engine')})" for r in proven}))
    m.satisfied(len(proven))


def _metric_multi_ai_decisions(repo: Path, m: MetricResult, cell: list) -> None:
    intact = _intact_cells(repo, cell)
    if not intact:
        m.unknown("no git-intact audit-cell record — synthesis unobservable")
        return
    decisions = []
    for _, d in intact:
        s = d.get("synthesis") or {}
        if (isinstance(s, dict) and s.get("schema") == "cell-synthesis/v1"
                and s.get("decision") and s.get("routed_to")
                and "claim" in s and "evidence" in s
                and "counterexample" in s and "next_experiment" in s):
            decisions.append((str(d.get("cell_id", "?")), str(s.get("decision"))))
    first = min(str(d.get("triggered_at", "?")) for _, d in intact)
    last = max(str(d.get("completed_at", "?")) for _, d in intact)
    m.window = (f"{first} → {last}; deterministic multi-AI syntheses observed "
                f"over that window: {len(decisions)}")
    m.detail = ", ".join(f"{cid}:{dec}" for cid, dec in decisions) or "none"
    m.satisfied(len(decisions))


def _metric_matched_runs_per_arm(repo: Path, m: MetricResult) -> None:
    manifest, obs, excluded = _benchmark_observations(repo)
    if excluded:
        m.sources.append({"record": BENCH_REL,
                          "verification": f"excluded (hash mismatch/absent): "
                                          f"{excluded}"})
    if not obs:
        m.unknown("no hash-verified benchmark observation — runs per arm "
                  "unobservable")
        return
    stats = _arm_stats(obs)
    per_arm = {a: s["runs"] for a, s in stats.items()}
    if set(per_arm) != set(ARMS):
        m.unknown(f"arms observed {sorted(per_arm)} != required {sorted(ARMS)}")
        return
    value = min(per_arm.values())
    m.window = (f"matched 3-arm benchmark {manifest.get('workload_id', '?')}; "
                f"hash-verified runs per arm {per_arm}")
    m.satisfied(value)


def _metric_factory_advantage(repo: Path, m: MetricResult) -> None:
    manifest, obs, excluded = _benchmark_observations(repo)
    if excluded:
        m.sources.append({"record": BENCH_REL,
                          "verification": f"excluded (hash mismatch/absent): "
                                          f"{excluded}"})
    if not obs:
        m.unknown("no hash-verified benchmark observation — advantage unmeasurable")
        return
    stats = _arm_stats(obs)
    if set(stats) != set(ARMS):
        m.unknown(f"arms observed {sorted(stats)} != required {sorted(ARMS)}")
        return
    fa_f = stats["factory"]["false_acceptances"]
    vr_f = (stats["factory"]["verified"] / stats["factory"]["runs"]
            if stats["factory"]["runs"] else 0.0)
    fa_s = max(stats[a]["false_acceptances"] for a in SINGLES)
    vr_s = max((stats[a]["verified"] / stats[a]["runs"]) if stats[a]["runs"] else 0.0
               for a in SINGLES)
    factory_wins = fa_f < fa_s and vr_f >= vr_s
    winner = "FACTORY_WINS" if factory_wins else "NO_FACTORY_GAIN"
    m.window = (f"matched 3-arm benchmark {manifest.get('workload_id', '?')} — "
                f"advantage recomputed from {len(obs)} raw runs")
    m.detail = (f"factory verified_rate={vr_f:.2f} fa={fa_f} vs best single "
                f"verified_rate={vr_s:.2f} fa={fa_s} → {winner} (explicit either "
                f"way; a non-gain is a valid measured result)")
    m.satisfied(1)


def _metric_self_improvement_decisions(repo: Path, m: MetricResult) -> None:
    p = repo / SELF_MOD_LEDGER_REL
    if not p.is_file() or not _git_intact(repo, SELF_MOD_LEDGER_REL):
        m.unknown("self-modification ledger absent or not git-intact")
        return
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if not rows:
        m.unknown("EMPTY_LEDGER: zero self-modification rows — nothing observed")
        return
    bound = []
    unbound = []
    receipt = repo / BENCHMARK_RECEIPT_REL
    receipt_sha = ""
    if receipt.is_file():
        receipt_sha = hashlib.sha256(receipt.read_bytes()).hexdigest()
    for r in rows:
        if r.get("validated") is True and str(r.get("benchmark_receipt_sha256", "")) \
                and str(r.get("benchmark_receipt_sha256", "")) == receipt_sha:
            bound.append(r)
        else:
            unbound.append(str(r.get("sha", "?"))[:12])
    first, last = str(rows[0].get("sha", "?"))[:12], str(rows[-1].get("sha", "?"))[:12]
    m.window = (f"self-modification ledger ({len(rows)} rows, candidates "
                f"{first}…{last}); validated decisions sha256-bound to an "
                f"existing benchmark receipt: {len(bound)}")
    m.detail = (f"validated+bound: "
                f"{[str(r.get('decision') or r.get('sha'))[:12] for r in bound]}; "
                f"unbound/unvalidated rows (not counted): {unbound or 'none'}")
    m.satisfied(len(bound))


def aggregate(repo: Path, project_dir: Path | None = None,
              include_context: bool = True) -> dict:
    """Recompute every base-readiness metric. Read-only. Never reads the receipt."""
    repo = Path(repo)
    project_dir = Path(project_dir) if project_dir else repo / "projects" / "factory"
    state = _load_json(project_dir / "PROJECT_STATE.json") or {}

    cell = _cell_records(repo)
    transitions = _transitions(repo)

    sup_cov = _coverage(repo, project_dir, "EXTERNAL_SUPERVISOR_NO_MANUAL_RELAUNCH", state)
    loop_cov = _coverage(repo, project_dir, "AUTOMATED_MULTI_AI_CTO_LOOP_V1", state)
    bench_cov = _coverage(repo, project_dir,
                          "CLAUDE_ALONE_VS_GLM_ALONE_VS_FACTORY_BENCHMARK", state)
    ssi_cov = _coverage(repo, project_dir, "SAFE_SELF_IMPROVEMENT", state)

    results: dict[str, MetricResult] = {}

    def _new(name: str, dependency: str = "", coverage: dict | None = None) -> MetricResult:
        cmp_, bound = EXPECTED[name]
        r = MetricResult(name=name, expected=f"{name}{cmp_}{bound}")
        r.sources = _sources_for(name)
        if dependency:
            r.dependency = dependency
            r.coverage = coverage or {}
        results[name] = r
        return r

    def _covered(name: str, dependency: str, coverage: dict, fn) -> None:
        m = _new(name, dependency, coverage)
        block = _require(coverage)
        if block:
            m.unknown(block)
        else:
            fn(m)

    _covered("MANUAL_DAEMON_RESTARTS", "EXTERNAL_SUPERVISOR_NO_MANUAL_RELAUNCH",
             sup_cov, lambda m: _metric_manual_daemon_restarts(repo, m))
    _covered("MANUAL_AGENT_HANDOFFS", "AUTOMATED_MULTI_AI_CTO_LOOP_V1", loop_cov,
             lambda m: _metric_manual_agent_handoffs(repo, m, transitions))
    _covered("MANUAL_AUDIT_TRIGGERS", "AUTOMATED_MULTI_AI_CTO_LOOP_V1", loop_cov,
             lambda m: _metric_manual_audit_triggers(repo, m, cell))
    _covered("ROUTINE_HUMAN_INTERVENTIONS",
             "CLAUDE_ALONE_VS_GLM_ALONE_VS_FACTORY_BENCHMARK", bench_cov,
             lambda m: _metric_routine_human_interventions(repo, m))
    m = _new("FALSE_SETTLEMENTS")
    _metric_false_settlements(repo, project_dir, m)
    _covered("CONTEXT_REEXPLANATIONS", "AUTOMATED_MULTI_AI_CTO_LOOP_V1", loop_cov,
             lambda m: _metric_context_reexplanations(repo, m, cell, transitions))
    m = _new("EXACT_SHA_REVIEWS_AUTOMATED")
    _metric_exact_sha_reviews(repo, project_dir, m)
    _covered("MULTI_AI_DECISIONS_SYNTHESIZED", "AUTOMATED_MULTI_AI_CTO_LOOP_V1",
             loop_cov, lambda m: _metric_multi_ai_decisions(repo, m, cell))
    _covered("MATCHED_BENCHMARK_RUNS_PER_ARM",
             "CLAUDE_ALONE_VS_GLM_ALONE_VS_FACTORY_BENCHMARK", bench_cov,
             lambda m: _metric_matched_runs_per_arm(repo, m))
    _covered("FACTORY_ADVANTAGE_MEASURED",
             "CLAUDE_ALONE_VS_GLM_ALONE_VS_FACTORY_BENCHMARK", bench_cov,
             lambda m: _metric_factory_advantage(repo, m))
    _covered("SAFE_SELF_IMPROVEMENT_DECISIONS", "SAFE_SELF_IMPROVEMENT", ssi_cov,
             lambda m: _metric_self_improvement_decisions(repo, m))

    blocking = [n for n, r in results.items() if r.status != SATISFIED]
    agg = {
        "schema": "factory-base-ready-aggregate/v1",
        "milestone": MILESTONE,
        "produced_at": _now(),
        "control_sha": _head_sha(repo),
        "decision": "NOT_READY" if blocking else "READY",
        "metrics": {n: results[n].as_dict() for n in EXPECTED},
        "blocking": blocking,
        "unknown_semantics": "UNKNOWN = no independent source; UNKNOWN blocks readiness",
    }
    if include_context:
        agg["context"] = _context(repo, project_dir)
    return agg


def _context(repo: Path, project_dir: Path) -> dict:
    """Context the recompute ran against — reported, never a gate.

    engine_stats (runrecords) denominator counts every attempt; progress_truth
    names which event classes are product progress. Both document the windows
    above; neither can move a metric.
    """
    ctx: dict = {}
    try:
        from . import runrecords
        stats = runrecords.engine_stats()
        engines = stats.get("engines") or {}
        ctx["engine_stats"] = {
            e: {"attempts": v.get("attempts"), "accepted": v.get("accepted")}
            for e, v in engines.items()} or {"note": "no ledger rows"}
    except Exception as exc:                                   # noqa: BLE001
        ctx["engine_stats"] = f"unevaluable: {type(exc).__name__}"
    from . import progress_truth
    ctx["progress_events"] = sorted(progress_truth.PROGRESS_EVENTS)
    return ctx


def write_receipt(repo: Path, agg: dict, out_rel: str = RECEIPT_REL) -> Path:
    """Persist the aggregate as the milestone receipt — the ONLY writer.

    The receipt is the output of a recompute; an authored file at the same path
    has no producer provenance and is simply replaced. Grading never reads it.
    """
    receipt = {
        "schema": "factory-base-ready-receipt/v1",
        "milestone": MILESTONE,
        "produced_by": "factory_v2.base_ready_aggregator.aggregate() — independent "
                       "recompute from durable records; never authored, never an input",
        "produced_at": agg.get("produced_at"),
        "control_sha": agg.get("control_sha"),
        "decision": agg.get("decision"),
        "metrics": agg.get("metrics"),
        "blocking": agg.get("blocking"),
        "unknown_semantics": agg.get("unknown_semantics"),
        "context": agg.get("context"),
    }
    path = Path(repo) / out_rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    return path


def main(argv: list | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    repo = Path(".")
    out = RECEIPT_REL
    no_write = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--project-repo", "-r"):
            i += 1
            repo = Path(argv[i]) if i < len(argv) else repo
        elif a == "--out":
            i += 1
            out = argv[i] if i < len(argv) else out
        elif a == "--no-write":
            no_write = True
        elif a in ("-h", "--help"):
            print(__doc__)
            return 0
        else:
            print(f"unknown argument: {a}", file=sys.stderr)
            return 2
        i += 1
    try:
        agg = aggregate(repo)
    except Exception as exc:                                   # noqa: BLE001
        print(f"AGGREGATE_FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if not no_write:
        try:
            written = write_receipt(repo, agg, out)
            print(f"receipt written: {written}", file=sys.stderr)
        except OSError as exc:
            print(f"RECEIPT_WRITE_FAILED: {exc}", file=sys.stderr)
    print(json.dumps(agg, indent=2, sort_keys=True))
    return 0 if agg["decision"] == "READY" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        raise SystemExit(0)
