#!/usr/bin/env python3
"""Banc de chaos hermétique (FACTORY_CHAOS_DRILL_V1) — les 12 pannes réelles, résorbées sans humain.

Fait tourner la VRAIE boucle `factory_v2.runtime.advance()` sur un projet JETABLE
créé dans un tmpdir (STRATEGY/SPEC/ROADMAP minimaux, PROJECT_STATE vierge), sous
FACTORY_NO_REAL_ENGINE=1, avec un exécuteur stub injecté (`advance(..., executor=)`)
et — pour les scénarios de revue — la vraie `review_dispatch.dispatch_review`
reroutée sur un runner/route stubs. Injecte une panne à la fois, observe les
événements de PROJECT_STATE.json et mesure que la factory la résorbe SEULE
(0 intervention) en quelques cycles d'advance().

« Résorbé » = le jalon visé finit MILESTONE_SETTLED, ou la factory a rotaté vers
un autre jalon (STUCK_MILESTONE_YIELDED) qui se règle, sans que le même événement
bloquant se répète plus de 3 fois.

Le reçu (schema "chaos-drill-receipt/v1") n'est JAMAIS écrit à la main : il est la
sortie de ce banc exécuté. Rejouer :

    python3 -m factory_v2.chaos_drill --all --out recu.json
    python3 -m factory_v2.chaos_drill --scenario QUOTA_CUT_MID_BUILD --out r.json
"""
from __future__ import annotations

import argparse
import atexit
import importlib.util
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# ── Environnement hermétique du banc (AVANT tout import factory_v2 lourd) ──────
# Le banc EST le superviseur de son tmpdir : il fixe sa propre clé owner (seal
# HMAC des revues), isole runrecords/quota hors du dépôt, et verrouille le
# kill-switch anti-moteur-réel. setdefault : un appelant qui a déjà isolé (tests,
# juge) garde la main ; les enfants (crash-daemon) héritent de tout.
os.environ["FACTORY_NO_REAL_ENGINE"] = "1"
os.environ.setdefault("FACTORY_MAX_WORKERS", "1")
# FACTORY_REPO_HYGIENE_V1 (22/09) : UN dossier hermétique par invocation du banc, hérité par les enfants (crash-daemon,
# scénarios en sous-processus) via FACTORY_CHAOS_HERMETIC_TMP ; seul son créateur le retire à la sortie — 0 fuite
# (mesuré avant : 50 `chaos_drill_env_*` laissés par un seul passage des deux bancs).
_HERMETIC_TMP_ENV = "FACTORY_CHAOS_HERMETIC_TMP"
if os.environ.get(_HERMETIC_TMP_ENV) and Path(os.environ[_HERMETIC_TMP_ENV]).is_dir():
    _HERMETIC_TMP = Path(os.environ[_HERMETIC_TMP_ENV])
else:
    _HERMETIC_TMP = Path(tempfile.mkdtemp(prefix="chaos_drill_env_"))
    os.environ[_HERMETIC_TMP_ENV] = str(_HERMETIC_TMP)
    atexit.register(shutil.rmtree, _HERMETIC_TMP, True)
os.environ.setdefault("FACTORY_RUNRECORDS_PATH", str(_HERMETIC_TMP / "runrecords.jsonl"))
os.environ.setdefault("FACTORY_QUOTA_SNAPSHOT_ROOT", str(_HERMETIC_TMP / "quota"))
if not os.environ.get("FACTORY_OWNER_AUTHORIZATION_KEY"):
    os.environ["FACTORY_OWNER_AUTHORIZATION_KEY"] = "%064x" % random.getrandbits(256)

import yaml  # noqa: E402

from factory_v2.executors import ExecutionResult  # noqa: E402
from factory_v2.project_authority import (  # noqa: E402
    load_project_state, save_project_state)
from factory_v2.runtime import advance as _runtime_advance  # noqa: E402

from .verify_chaos_drill import SCENARIOS, SETS  # les listes du juge, au caractère près

# ── Isolation des stores SUPERVISEUR (pas projet) hors du dépôt ────────────────
# Le banc est le superviseur de son tmpdir : les journaux appartenant au
# superviseur (allocations de récupération, cellule d'audit) ne doivent rien
# écrire dans le dépôt du contrôle — mêmes principes que FACTORY_RUNRECORDS_PATH.
# La cellule d'audit (A1/A6) est une activité ANNEXE d'observation ; sous
# FACTORY_NO_REAL_ENGINE son relecteur plante par construction (CapabilityDenial)
# et son drain écrirait dans factory_v2/receipts/ du dépôt : neutralisée ici,
# elle n'entre dans AUCUN des 12 scénarios (les récupérations mesurées passent
# par la rotation moteur, la garde idle et la reconstruction de revue).
from factory_v2 import recovery_allocation as _ra  # noqa: E402
from factory_v2 import runtime as _rt_mod  # noqa: E402
from factory_v2._io import _now, _now as _now_iso
_ra.STORE = _HERMETIC_TMP / "recovery_allocations"


def _advance_without_audit_cell(*args, **kwargs):
    """Run one drill cycle without leaking its audit isolation to importers."""
    drain = _rt_mod.drain_pending_audit_cell
    _rt_mod.drain_pending_audit_cell = lambda project_dir: None
    try:
        return _runtime_advance(*args, **kwargs)
    finally:
        _rt_mod.drain_pending_audit_cell = drain

MAX_CYCLES = 60                 # borne du banc par scénario (mission)
BLOCKING_EVENT_MAX_REPEATS = 3  # « sans que le même événement bloquant se répète > 3 fois »
ZERO_HEAD = "0" * 40            # head factice du marqueur WAITING_FOR_EVIDENCE (mission sans SHA)


# ═══════════════════════════════ utilitaires ══════════════════════════════════

def _events(pd: Path) -> list:
    return (load_project_state(pd).get("events") or [])


def _has_event(evs, name, **fields) -> bool:
    for e in evs:
        if not isinstance(e, dict) or e.get("event") != name:
            continue
        if all(str(e.get(k)) == str(v) for k, v in fields.items()):
            return True
    return False


def _count_event(evs, name, **fields) -> int:
    n = 0
    for e in evs:
        if isinstance(e, dict) and e.get("event") == name:
            if all(str(e.get(k)) == str(v) for k, v in fields.items()):
                n += 1
    return n


def _chain(evs, *names) -> str:
    """Chaîne lisible « NAME(détail) → … » des n derniers événements demandés."""
    out = []
    for e in evs:
        if not isinstance(e, dict):
            continue
        nm = str(e.get("event") or "")
        if nm not in names:
            continue
        det = (e.get("interruption") or e.get("reason") or "")
        eng = e.get("from_engine") or e.get("engine") or ""
        extra = f"/{e.get('to_engine')}" if e.get("to_engine") else ""
        bit = nm
        if det:
            bit += f"({str(det)[:44]})"
        if eng:
            bit += f"[{eng}{extra}]"
        out.append(bit)
    return " → ".join(out[-6:]) if out else "(aucun)"


def _settled_milestones(evs) -> list:
    return [str(e.get("milestone")) for e in evs
            if isinstance(e, dict) and e.get("event") == "MILESTONE_SETTLED"]


def _git(repo: Path, *args, check=True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", "-C", str(repo), "-c", "user.email=chaos@factory",
                        "-c", "user.name=chaos-drill", *args],
                       capture_output=True, text=True, timeout=60)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {args}: {r.stderr.strip()}")
    return r


# ═════════════════════════ projet jetable + stubs ═════════════════════════════

def make_disposable_project(root: Path, milestone_specs: list, name: str = "proj",
                            product_repo: bool = False) -> tuple:
    """Projet jetable minimal : trio canonique + PROJECT_STATE vierge (+ product
    repo git jetable quand le scénario exige de vrais SHA). `milestone_specs` :
    dicts de roadmap SANS task_file (injecté ici, chemin absolu)."""
    pd = root / name
    (pd / "evidence").mkdir(parents=True)
    (pd / "tasks").mkdir(parents=True)
    (pd / "STRATEGY.md").write_text("# Strategy\nNorth Star: chaos drill.\n")
    (pd / "SPEC.md").write_text("# Spec\n")
    prod = None
    roadmap = {"version": "v3", "project": name, "milestones": []}
    if product_repo:
        prod = root / f"{name}_product"
        (prod / "out").mkdir(parents=True)
        _git(prod, "init", "-q", "-b", "main")
        (prod / "out" / ".keep").write_text("")
        (prod / "README.md").write_text("base\n")
        _git(prod, "add", "-A")
        _git(prod, "commit", "-qm", "base")
        roadmap["product_repo"] = str(prod)
    for spec in milestone_specs:
        spec = dict(spec)
        tf = pd / "tasks" / f"{spec['id']}.md"
        tf.write_text(f"# Task {spec['id']}\nConstruire le livrable du jalon.\n")
        steps = spec.setdefault("execution", {}).setdefault("steps", [])
        for st in steps:
            ex = st.setdefault("executor", {})
            if ex.get("type") == "autopilot":
                ex.setdefault("task_file", str(tf))
        roadmap["milestones"].append(spec)
    (pd / "ROADMAP.yaml").write_text(yaml.safe_dump(roadmap, sort_keys=False))
    save_project_state(pd, {
        "schema": "project-state/v2", "project": name,
        "completed_milestones": {}, "validation_contracts": {},
        "current_milestone": milestone_specs[0]["id"],
        "missions": {}, "escalation_ladder": {}, "events": [],
    })
    return pd, prod


def _autopilot_step() -> dict:
    return {"kind": "implement", "executor": {"type": "autopilot"}}


def _local_milestone(mid: str, extra: dict | None = None) -> dict:
    """Jalon autopilot « sans repo produit » : livrable + e2e dans project_dir."""
    ms = {"id": mid, "goal": f"goal {mid}", "acceptance": [f"accept {mid}"],
          "deliverables": [f"out/{mid}.txt"],
          "product_e2e": {"test_command": f"test -f out/{mid}.txt"},
          "execution": {"steps": [_autopilot_step()]}}
    if extra:
        ms.update(extra)
    return ms


def _product_milestone(mid: str, deliverable: str, extra: dict | None = None) -> dict:
    ms = {"id": mid, "goal": f"goal {mid}", "acceptance": [f"accept {mid}"],
          "deliverables": [deliverable],
          "product_e2e": {"test_command": f"test -f {deliverable}"},
          "execution": {"steps": [_autopilot_step()]}}
    if extra:
        ms.update(extra)
    return ms


class StubExecutor:
    """Exécuteur stub injectable — simule un moteur (build, coupure, kill, refus).

    `script` : callable(mission, attempt, preferred_engine, stub) -> ExecutionResult.
    L'« attempt » et le moteur préféré sont lus de PROJECT_STATE (le vrai état),
    jamais d'un compteur interne : le stub réagit à ce que la factory a décidé.
    """

    def __init__(self, project_dir: Path, script, product_repo: Path | None = None):
        self.pd = Path(project_dir)
        self.prod = product_repo
        self.script = script
        self.calls = 0

    # -- briques de résultats (convention dispatcher_v3 / executors) -----------
    def ok(self, mission, engine="glm", tail="stub build ok", artifact=True,
           shas=("", "")) -> ExecutionResult:
        if self.prod is not None:
            deliverable = (mission.executor.get("deliverables") or ["out/artifact.txt"])[0]
            target = self.prod / deliverable
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"built by {engine} v{self.calls}\n")
            sha = self._commit_product(f"build {mission.id} v{self.calls}")
            shas = (sha, sha)
        elif artifact:
            out = self.pd / "out"
            out.mkdir(exist_ok=True)
            (out / f"{mission.milestone}.txt").write_text(f"done by {engine}\n")
        return ExecutionResult(
            mission_id=mission.id, rc=0, output_tail=tail, engine=engine,
            candidate_sha=shas[0], integration_sha=shas[1],
            engine_execution={"engine_id": engine, "observed": True,
                              "argv0": "chaos-stub", "pid": os.getpid()})

    def commit_without_deliverable(self, mission, deliverable: str,
                                   engine="glm") -> ExecutionResult:
        """PASS de complaisance : commit réel SANS le livrable (scénario 10)."""
        (self.prod / "scratch.txt").write_text(f"rework {self.calls}\n")
        sha = self._commit_product(f"rework sans livrable v{self.calls}")
        return ExecutionResult(
            mission_id=mission.id, rc=0, output_tail="stub claims success (no deliverable)",
            engine=engine, candidate_sha=sha, integration_sha=sha,
            engine_execution={"engine_id": engine, "observed": True,
                              "argv0": "chaos-stub", "pid": os.getpid()})

    def quota_cut(self, mission, engine="glm") -> ExecutionResult:
        """Coupure quota : le marqueur typé que dispatcher_v3 écrit après avoir
        classifié le stderr (« You've hit your usage limit » → QUOTA_CUT)."""
        return ExecutionResult(
            mission_id=mission.id, rc=1,
            output_tail="You've hit your usage limit — resets in 4h", engine=engine,
            receipt={"verdict": "INTERRUPTED",
                     "engine_executions": [{"engine": engine, "interruption": "QUOTA_CUT"}]},
            engine_execution={"engine_id": engine, "observed": True,
                              "argv0": "chaos-stub", "pid": os.getpid()})

    def killed(self, mission, engine="glm") -> ExecutionResult:
        """Moteur tué par signal (balayage/restart) : rc négatif + stage build tué."""
        return ExecutionResult(
            mission_id=mission.id, rc=-15, output_tail=f"{engine} terminated by signal 15",
            engine=engine,
            receipt={"verdict": "INTERRUPTED",
                     "stages": [{"step": f"build_{engine}", "rc": -15}],
                     "engine_executions": [{"engine": engine}]},
            engine_execution={"engine_id": engine, "observed": True,
                              "argv0": "chaos-stub", "pid": os.getpid()})

    def filter_refusal(self, mission, engine="glm") -> ExecutionResult:
        """Le moteur refuse la tâche (filtre de contenu) AVANT tout lancement :
        receipt à engine_executions vide → refus exempt de quota
        (runtime._dispatcher_refused_before_engine), empreinte stable. Le champ
        `engine` reste le moteur sollicité (identité de rotation), seule
        l'exécution observée est absente : rien ne s'est lancé."""
        return ExecutionResult(
            mission_id=mission.id, rc=1, engine=engine,
            output_tail="task rejected by engine content filter",
            receipt={"verdict": "TASK_REFUSED_BY_FILTER",
                     "verdict_reason": "content filter refused this task",
                     "engine_executions": []},
            engine_execution={}, telemetry_observations=[])

    def semantic_fail(self, mission, engine="glm", label="deterministic failure") -> ExecutionResult:
        return ExecutionResult(
            mission_id=mission.id, rc=1, output_tail=f"STUB_FAIL: {label}", engine=engine,
            receipt={"stages": []},
            engine_execution={"engine_id": engine, "observed": True,
                              "argv0": "chaos-stub", "pid": os.getpid()})

    def _commit_product(self, msg: str) -> str:
        _git(self.prod, "add", "-A")
        _git(self.prod, "commit", "-qm", msg)
        return _git(self.prod, "rev-parse", "HEAD").stdout.strip()

    # -- point d'entrée executor ------------------------------------------------
    def execute(self, mission):
        st = load_project_state(self.pd)
        rec = (st.get("missions") or {}).get(mission.id) or {}
        attempt = int(rec.get("attempt_count") or 0) + 1
        preferred = str(rec.get("preferred_engine") or "glm")
        self.calls += 1
        return self.script(mission, attempt, preferred, self)


class ReviewPatch:
    """Reroute la revue multi-IA sur des stubs SANS court-circuiter le vrai
    `dispatch_review` : ledger de revue, comptes launch_gate, seal HMAC et
    télémétrie restent réels (les ACCEPT restent « prouvés » au sens durci).

    `verdict_fn(reviewed_sha, call_index)` → ("ACCEPT"|"BLOCK", raison).
    """

    def __init__(self, verdict_fn, default_engine="claude"):
        from factory_v2 import review_dispatch
        self._rd = review_dispatch
        self._real = review_dispatch.dispatch_review
        self.verdict_fn = verdict_fn
        self.default_engine = default_engine
        self.calls = 0
        self.seen_shas: list = []

    def _route(self, spec_level, task_kind, project=None, exclude_engines=None,
               engine_hint=None):
        eng = engine_hint or self.default_engine
        return SimpleNamespace(engine=eng, model="chaos-stub", role="reviewer",
                               read_only=True, cli="true", effort="")

    def _enter(self):
        real, route, verdict_fn = self._real, self._route, self.verdict_fn
        seen, self_ref = self.seen_shas, self

        def wrapped(project_dir, **kw):
            sha = str(kw.get("reviewed_sha") or "")
            self_ref.calls += 1
            verdict, reason = verdict_fn(sha, self_ref.calls)
            seen.append(sha)

            def runner(argv, cwd=None, capture_output=True, text=True, timeout=0):
                return SimpleNamespace(
                    returncode=0,
                    stdout=f"chaos reviewer transcript\n{verdict} — {reason}\n",
                    pid=os.getpid())

            return real(project_dir, runner=runner, route=route,
                        cli_args=lambda choice, repo, prompt: ["chaos-stub-reviewer"],
                        **kw)

        self._rd.dispatch_review = wrapped

    def __enter__(self):
        self._enter()
        return self

    def __exit__(self, *exc):
        self._rd.dispatch_review = self._real
        return False


# ═══════════════════════════════ boucle ═══════════════════════════════════════

class Run:
    """Boucle d'advance() bornée + observation des événements."""

    def __init__(self, pd: Path, executor=None):
        self.pd = Path(pd)
        self.executor = executor
        self.results: list = []
        self.t0 = time.monotonic()

    def cycle(self):
        r = _advance_without_audit_cell(self.pd, executor=self.executor)
        self.results.append(r)
        return r

    @property
    def cycles(self) -> int:
        return len(self.results)

    @property
    def elapsed_s(self) -> float:
        return time.monotonic() - self.t0

    def events(self) -> list:
        return _events(self.pd)


def _finish(run: Run, sid: str, injected: bool, recovered: bool,
            evidence: str) -> dict:
    return {"id": sid, "injected": bool(injected), "recovered": bool(recovered),
            "recovery_cycles": int(run.cycles), "recovery_s": round(run.elapsed_s, 2),
            "human_interventions": 0, "evidence": evidence}


def _recovered_on_settle(run: Run, target: str) -> tuple:
    """(recovered, evidence) — règlement du jalon visé OU rotation vers un jalon
    qui se règle (STUCK_MILESTONE_YIELDED puis MILESTONE_SETTLED)."""
    evs = run.events()
    chain = _chain(evs, "ATTEMPT_INTERRUPTED", "EMPTY_FINGERPRINT_ENGINE_ROTATION",
                   "MISSION_PASS", "MISSION_FAIL", "MILESTONE_SETTLED",
                   "STUCK_MILESTONE_YIELDED", "IDLE_GUARD_TRIPPED",
                   "MULTI_AI_REVIEW_BLOCKED", "MULTI_AI_REVIEW_ACCEPTED",
                   "MISSION_REBUILD_AFTER_REVIEW_BLOCK", "MULTI_AI_ROSTER_DEGRADED",
                   "MISSION_PASS_ADOPTED_COMPATIBLE_CONTRACT", "AUTHORITY_DRIFT_BLOCKED",
                   "CANDIDATE_DELIVERABLE_MISSING", "LOST_ATTEMPTS_RECONCILED",
                   "ESCALATION_REQUIRED", "LAUNCH_BLOCKED", "MILESTONE_WAITING_FOR_EVIDENCE")
    settled = _settled_milestones(evs)
    if target in settled:
        return True, f"{chain} — jalon {target} réglé"
    for e in evs:
        if isinstance(e, dict) and e.get("event") == "STUCK_MILESTONE_YIELDED":
            rotated = str(e.get("milestone") or "")
            if rotated in settled:
                return True, f"{chain} — rotation vers {rotated}, jalon réglé"
    return False, chain


# ═══════════════════════════════ scénarios ════════════════════════════════════

def _wait_engine_rotation_stub(fail_result):
    """Stub commun : `fail_result(stub, mission, engine)` tant que le moteur
    préféré est le moteur d'origine (glm), succès après la rotation de la factory."""
    def script(mission, attempt, preferred, stub):
        if preferred in ("glm", ""):
            return fail_result(stub, mission, "glm")
        return stub.ok(mission, engine=preferred)
    return script


def scenario_quota_cut_mid_build(root: Path) -> dict:
    sid = "QUOTA_CUT_MID_BUILD"
    pd, _ = make_disposable_project(root, [_local_milestone("A")])
    stub = StubExecutor(pd, _wait_engine_rotation_stub(StubExecutor.quota_cut))
    run = Run(pd, stub)
    injected = False
    for _ in range(MAX_CYCLES):
        run.cycle()
        if _has_event(run.events(), "ATTEMPT_INTERRUPTED", interruption="QUOTA_CUT"):
            injected = True
        rec = load_project_state(pd)["completed_milestones"].get("A", {})
        if rec.get("state") == "COMPLETE":
            break
    recovered, ev = _recovered_on_settle(run, "A")
    ok = (injected and recovered
          and _count_event(run.events(), "ATTEMPT_INTERRUPTED",
                           interruption="QUOTA_CUT") <= BLOCKING_EVENT_MAX_REPEATS)
    return _finish(run, sid, injected, ok, ev)


def scenario_engine_killed_by_signal(root: Path) -> dict:
    sid = "ENGINE_KILLED_BY_SIGNAL"
    pd, _ = make_disposable_project(root, [_local_milestone("A")])
    stub = StubExecutor(pd, _wait_engine_rotation_stub(StubExecutor.killed))
    run = Run(pd, stub)
    injected = False
    for _ in range(MAX_CYCLES):
        run.cycle()
        if _has_event(run.events(), "ATTEMPT_INTERRUPTED", interruption="KILL"):
            injected = True
        rec = load_project_state(pd)["completed_milestones"].get("A", {})
        if rec.get("state") == "COMPLETE":
            break
    recovered, ev = _recovered_on_settle(run, "A")
    ok = (injected and recovered
          and _count_event(run.events(), "ATTEMPT_INTERRUPTED",
                           interruption="KILL") <= BLOCKING_EVENT_MAX_REPEATS)
    return _finish(run, sid, injected, ok, ev)


def scenario_reload_during_build(root: Path) -> dict:
    """Code daemon changé pendant un build : le reload attend (WAIT), ne tue
    jamais la mission en vol, et atterrit après le règlement (RELOAD_NOW)."""
    sid = "RELOAD_DURING_BUILD"
    from factory_v2 import hot_reload
    from factory_v2.mission_evidence import (clear_worker_state, get_process_birth_time,
                                             write_heartbeat)
    bin_dir = _REPO_ROOT / "factory" / "bin"
    if str(bin_dir) not in sys.path:
        sys.path.insert(0, str(bin_dir))
    import factoryd

    pd, _ = make_disposable_project(root, [_local_milestone("A")])
    stub = StubExecutor(pd, lambda m, a, p, s: s.ok(m, engine=p))

    # 1) un build est EN VOL : record de mission + heartbeat vivant (pid réel,
    #    identité de naissance) — c'est ce que factoryd.v2_status_summary observe.
    st = load_project_state(pd)
    st.setdefault("missions", {})["A#M1"] = {"state": "IN_FLIGHT"}
    save_project_state(pd, st)
    write_heartbeat(pd, "A#M1", pid=os.getpid(), phase="build",
                    birth_time=get_process_birth_time(os.getpid()))
    # 2) le code daemon change pendant le build → drain programmé.
    changed = hot_reload.reload_needed(["factory_v2/runtime.py"])
    saved = (factoryd._RELOAD_DRAINING, factoryd._RELOAD_DRAIN_PROJECTS,
             factoryd._RELOAD_DRAIN_STARTED, factoryd._ADVANCE_FUTURES)
    try:
        factoryd._begin_reload_drain([pd])
        in_flight_during = factoryd._reload_in_flight(pd, "MISSION_IN_FLIGHT")
        decision_during = hot_reload.decide(in_flight_during, changed)
        assert decision_during == "WAIT", f"reload pendant le build: {decision_during}"
        assert in_flight_during >= 1, "la mission en vol n'est pas observée"

        # 3) le build se termine (heartbeat retiré), la factory règle le jalon seule.
        clear_worker_state(pd, "A#M1")
        run = Run(pd, stub)
        for _ in range(MAX_CYCLES):
            run.cycle()
            if load_project_state(pd)["completed_milestones"].get("A", {}).get("state") == "COMPLETE":
                break
        settled, ev = _recovered_on_settle(run, "A")
        # 4) plus rien en vol → le reload atterrit MAINTENANT.
        in_flight_after = factoryd._reload_in_flight(None, "MILESTONE_SETTLED")
        decision_after = hot_reload.decide(in_flight_after, changed)
        reload_ready_after = factoryd._advance_parallel(pd, lambda *a, **k: None)
        recovered = (settled and decision_after == "RELOAD_NOW"
                     and reload_ready_after == "RELOAD_READY"
                     and decision_during == "WAIT")
        evidence = (f"in_flight={in_flight_during} → decide=WAIT pendant le build ; "
                    f"{ev} ; après règlement in_flight={in_flight_after} → "
                    f"decide={decision_after}, advance_parallel={reload_ready_after}")
        return _finish(run, sid, changed and in_flight_during >= 1, recovered, evidence)
    finally:
        (factoryd._RELOAD_DRAINING, factoryd._RELOAD_DRAIN_PROJECTS,
         factoryd._RELOAD_DRAIN_STARTED, factoryd._ADVANCE_FUTURES) = saved


def scenario_review_block(root: Path) -> dict:
    sid = "REVIEW_BLOCK"
    pd, prod = make_disposable_project(
        root, [_product_milestone("A", "out/artifact.txt",
                                  {"multi_ai_review": {"required": True}})],
        product_repo=True)

    def verdict_fn(sha, call):
        if not seen:               # le premier candidat est bloqué ; les reconstruits passent
            seen.add(sha)
            return ("BLOCK", "livrable non conforme à l'acceptation (1er candidat)")
        return ("ACCEPT", "candidat reconstruit, livrable vérifié")

    seen: set = set()
    stub = StubExecutor(pd, lambda m, a, p, s: s.ok(m, engine=p), product_repo=prod)
    run = Run(pd, stub)
    with ReviewPatch(verdict_fn):
        for _ in range(MAX_CYCLES):
            run.cycle()
            if load_project_state(pd)["completed_milestones"].get("A", {}).get("state") == "COMPLETE":
                break
    evs = run.events()
    injected = _has_event(evs, "MULTI_AI_REVIEW_BLOCKED")
    recovered, ev = _recovered_on_settle(run, "A")
    ok = (injected and recovered
          and _has_event(evs, "MISSION_REBUILD_AFTER_REVIEW_BLOCK")
          and _count_event(evs, "MULTI_AI_REVIEW_BLOCKED") <= BLOCKING_EVENT_MAX_REPEATS)
    return _finish(run, sid, injected, ok, ev)


def scenario_stale_contract(root: Path) -> dict:
    sid = "STALE_CONTRACT"
    pd, _ = make_disposable_project(root, [
        _local_milestone("A", {"execution": {"steps": [_autopilot_step(),
                                                       _autopilot_step()]}})])
    # deux missions : M1 réussit, M2 échoue au 1er coup (fenêtre entre PASS et settlement)
    def script(mission, attempt, preferred, stub):
        if mission.id.endswith("#M2") and attempt == 1:
            return stub.semantic_fail(mission, engine=preferred, label="étape 2 pas prête")
        return stub.ok(mission, engine=preferred)
    stub = StubExecutor(pd, script)
    run = Run(pd, stub)
    run.cycle()                      # cycle 1 : M1 PASS, M2 FAIL (pas de settlement)
    m1_pass = (load_project_state(pd).get("missions", {}).get("A#M1", {})
               .get("state")) == "PASS"
    # injection : le propriétaire modifie la roadmap (sémantique de validation inchangée)
    roadmap = yaml.safe_load((pd / "ROADMAP.yaml").read_text())
    roadmap["milestones"][0]["goal"] += " (amended by owner)"
    (pd / "ROADMAP.yaml").write_text(yaml.safe_dump(roadmap, sort_keys=False))
    injected = m1_pass and (pd / "ROADMAP.yaml").is_file()
    for _ in range(MAX_CYCLES - 1):
        run.cycle()
        if load_project_state(pd)["completed_milestones"].get("A", {}).get("state") == "COMPLETE":
            break
    evs = run.events()
    recovered, ev = _recovered_on_settle(run, "A")
    ok = (injected and recovered
          and _has_event(evs, "AUTHORITY_DRIFT_BLOCKED")
          and _has_event(evs, "MISSION_PASS_ADOPTED_COMPATIBLE_CONTRACT"))
    return _finish(run, sid, injected, ok, ev)


def scenario_roster_impossible(root: Path) -> dict:
    sid = "ROSTER_IMPOSSIBLE"
    pd, prod = make_disposable_project(
        root, [
            _product_milestone(
                "A", "out/artifact.txt",
                {"multi_ai_review": {"required": True, "min_distinct_accept": 2,
                                     "reviewers": ["glm", "claude"]}}),
            _local_milestone("B"),
        ],
        product_repo=True)
    # builder=glm leaves one eligible reviewer for a frozen threshold of two.
    # The invariant is observed as a typed block; anti-starvation then rotates
    # to independent B instead of weakening the contract at runtime.
    stub = StubExecutor(pd, lambda m, a, p, s: s.ok(m, engine="glm"), product_repo=prod)
    run = Run(pd, stub)
    with ReviewPatch(lambda sha, call: ("ACCEPT", "roster dégradé, relecteur unique")):
        for _ in range(MAX_CYCLES):
            run.cycle()
            if load_project_state(pd)["completed_milestones"].get("B", {}).get("state") == "COMPLETE":
                break
    evs = run.events()
    injected = _has_event(evs, "MULTI_AI_REVIEW_UNAVAILABLE")
    recovered, ev = _recovered_on_settle(run, "B")
    ok = (injected and recovered
          and not _has_event(evs, "MULTI_AI_ROSTER_DEGRADED"))
    return _finish(run, sid, injected, ok, ev)


def scenario_dirty_worktree_merge(root: Path) -> dict:
    sid = "DIRTY_WORKTREE_MERGE"
    spec = importlib.util.spec_from_file_location(
        "chaos_dispatcher_v3", _REPO_ROOT / "factory" / "bin" / "dispatcher_v3.py")
    d3 = importlib.util.module_from_spec(spec)
    sys.modules["chaos_dispatcher_v3"] = d3
    spec.loader.exec_module(d3)

    repo = root / "control_repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "code.py").write_text("VALUE = 1\n")
    (repo / "evidence").mkdir()
    (repo / "evidence" / "receipts.jsonl").write_text('{"base": true}\n')
    (repo / "factory_v2" / "receipts").mkdir(parents=True)
    (repo / "factory_v2" / "receipts" / "x.json").write_text("{}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "checkout", "-qb", "candidate/A#M1")
    (repo / "code.py").write_text("VALUE = 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "candidat accepté A#M1")
    _git(repo, "checkout", "-q", "main")
    # injection : le bookkeeping d'état salit le checkout au moment de la fusion
    (repo / "evidence" / "receipts.jsonl").write_text(
        '{"base": true}\n{"live": "état factory écrit pendant le build"}\n')
    (repo / "factory_v2" / "receipts" / "x.json").write_text('{"dirty": true}\n')
    dirty_before = [ln[3:] for ln in _git(
        repo, "status", "--porcelain", "--untracked-files=no").stdout.splitlines() if ln.strip()]
    injected = bool(dirty_before) and all(
        p.startswith(("evidence/", "factory_v2/receipts/", "projects/",
                      "factory/run/", "factory/cto/")) or p == ".portfolio_cursor.json"
        for p in dirty_before)

    merged = bool(d3._merge_candidate(repo, "candidate/A#M1", "A#M1"))
    code_after = (repo / "code.py").read_text().strip()
    live_kept = "état factory écrit pendant le build" in (repo / "evidence" / "receipts.jsonl").read_text()
    recovered = merged and code_after == "VALUE = 2" and live_kept
    run = Run(root)                  # pas de boucle advance : fusion directe du vrai code
    run.cycle = lambda: None         # type: ignore[method-assign]
    evidence = (f"checkout sale {dirty_before} → _merge_candidate={merged}, "
                f"code.py={code_after!r}, version live du bookkeeping conservée={live_kept}")
    return _finish(run, sid, injected, recovered, evidence)


def scenario_engine_refusal(root: Path) -> dict:
    sid = "ENGINE_REFUSAL"
    pd, _ = make_disposable_project(root, [_local_milestone("A")])

    def script(mission, attempt, preferred, stub):
        if preferred in ("glm", ""):
            return stub.filter_refusal(mission)   # le filtre refuse la tâche
        return stub.ok(mission, engine=preferred)  # reformulé/autre moteur : accepté

    stub = StubExecutor(pd, script)
    run = Run(pd, stub)
    injected = False
    for _ in range(MAX_CYCLES):
        run.cycle()
        evs = run.events()
        if _has_event(evs, "MISSION_FAIL") or "TASK_REFUSED_BY_FILTER" in json.dumps(evs):
            injected = True
        if load_project_state(pd)["completed_milestones"].get("A", {}).get("state") == "COMPLETE":
            break
    evs = run.events()
    # « pas de boucle » : le refus identique s'arrête dès la rotation moteur
    refusals = sum(1 for r in run.results
                   if "TASK_REFUSED_BY_FILTER" in str(getattr(r, "detail", "")))
    recovered, ev = _recovered_on_settle(run, "A")
    ok = (injected and recovered and refusals <= BLOCKING_EVENT_MAX_REPEATS
          and _has_event(evs, "ESCALATION_REQUIRED"))
    return _finish(run, sid, injected, ok, f"refus={refusals} ; {ev}")


def scenario_budget_exhausted(root: Path) -> dict:
    sid = "BUDGET_EXHAUSTED"
    from factory_v2 import launch_gate as _lg
    pd, _ = make_disposable_project(root, [_local_milestone("A"), _local_milestone("B")])

    def script(mission, attempt, preferred, stub):
        if mission.milestone == "A":
            # brûle le plafond de lancements du jalon A (échecs déterministes)
            return stub.semantic_fail(mission, engine=preferred, label="budget burn")
        return stub.ok(mission, engine=preferred)

    stub = StubExecutor(pd, script)
    saved_cap = _lg.DEFAULT_MAX_MODEL_CALLS_PER_MILESTONE
    run = Run(pd, stub)
    try:
        _lg.DEFAULT_MAX_MODEL_CALLS_PER_MILESTONE = 2   # override documenté (env FACTORY_MAX_MODEL_CALLS_PER_MILESTONE)
        injected = False
        for _ in range(MAX_CYCLES):
            run.cycle()
            evs = run.events()
            if _has_event(evs, "LAUNCH_BLOCKED", reason="BUDGET_EXHAUSTED"):
                injected = True
            settled = _settled_milestones(evs)
            if "A" in settled or "B" in settled:
                break
    finally:
        _lg.DEFAULT_MAX_MODEL_CALLS_PER_MILESTONE = saved_cap
    evs = run.events()
    recovered, ev = _recovered_on_settle(run, "A")
    budget_blocks = _count_event(evs, "LAUNCH_BLOCKED", reason="BUDGET_EXHAUSTED")
    ok = (injected and recovered and budget_blocks <= BLOCKING_EVENT_MAX_REPEATS)
    return _finish(run, sid, injected, ok,
                   f"LAUNCH_BLOCKED(BUDGET_EXHAUSTED)×{budget_blocks} ; {ev}")


def scenario_missing_deliverable(root: Path) -> dict:
    sid = "MISSING_DELIVERABLE"
    pd, prod = make_disposable_project(
        root, [_product_milestone("A", "out/artifact.txt")], product_repo=True)

    def script(mission, attempt, preferred, stub):
        if attempt == 1:
            # PASS de complaisance : commit SANS le livrable déclaré
            return stub.commit_without_deliverable(mission, "out/artifact.txt", engine=preferred)
        return stub.ok(mission, engine=preferred)

    stub = StubExecutor(pd, script, product_repo=prod)
    run = Run(pd, stub)
    injected = False
    for _ in range(MAX_CYCLES):
        run.cycle()
        if _has_event(run.events(), "CANDIDATE_DELIVERABLE_MISSING"):
            injected = True
        if load_project_state(pd)["completed_milestones"].get("A", {}).get("state") == "COMPLETE":
            break
    evs = run.events()
    recovered, ev = _recovered_on_settle(run, "A")
    # « jamais ACCEPTED sans livrable » : le commit qui a réglé contient le livrable
    settled_ok = False
    if recovered:
        head = _git(prod, "rev-parse", "HEAD").stdout.strip()
        settled_ok = _git(prod, "cat-file", "-e", f"{head}:out/artifact.txt",
                          check=False).returncode == 0
        recovered = settled_ok
    ok = injected and recovered and settled_ok
    return _finish(run, sid, injected, ok, ev + f" ; livrable présent au commit réglé={settled_ok}")


def _crash_child_main(project_dir: str) -> int:
    """Processus enfant du scénario DAEMON_CRASH : advance() avec un stub qui
    dort dans execute() — tué par le banc pendant la « mission en vol »."""
    pd = Path(project_dir)
    import time as _t

    def script(mission, attempt, preferred, stub):
        _t.sleep(60)                  # le banc SIGKILL avant la fin
        return stub.semantic_fail(mission, engine=preferred)

    stub = StubExecutor(pd, script)
    _advance_without_audit_cell(pd, executor=stub)
    return 0


def scenario_daemon_crash_mid_mission(root: Path) -> dict:
    sid = "DAEMON_CRASH_MID_MISSION"
    import signal
    pd, _ = make_disposable_project(root, [_local_milestone("A")])
    env = {**os.environ, "PYTHONPATH": str(_REPO_ROOT)}

    child = subprocess.Popen(
        [sys.executable, "-c",
         "import factory_v2.chaos_drill as cd; cd._crash_child_main(%r)" % str(pd)],
        cwd=str(_REPO_ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ledger = pd / "evidence" / "attempt_accounts.jsonl"
    opened = False
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and child.poll() is None:
        if ledger.is_file():
            for line in ledger.read_text().splitlines():
                try:
                    if json.loads(line).get("phase") == "OPEN":
                        opened = True
                        break
                except ValueError:
                    continue
        if opened:
            break
        time.sleep(0.05)
    if opened:
        child.kill()                  # SIGKILL : le daemon meurt pendant la mission
    child.wait(timeout=10)
    injected = opened and child.returncode != 0

    # boot suivant : la factory reprend seule — reconciliation + re-dispatch
    stub = StubExecutor(pd, lambda m, a, p, s: s.ok(m, engine=p))
    run = Run(pd, stub)
    for _ in range(MAX_CYCLES):
        run.cycle()
        if load_project_state(pd)["completed_milestones"].get("A", {}).get("state") == "COMPLETE":
            break
    evs = run.events()
    recovered, ev = _recovered_on_settle(run, "A")
    # « sans double lancement » : l'OPEN orphelin fermé exactement une fois,
    # et exactement 2 tentatives au total (1 tuée + 1 reprise) pour A#M1.
    rows = []
    if ledger.is_file():
        for line in ledger.read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    a1_rows = [r for r in rows if str(r.get("mission")) == "A#M1"]
    kills = [r for r in a1_rows if r.get("phase") == "TERMINAL"
             and str(r.get("exit_class")) == "KILL"]
    attempts = {r.get("attempt_key") for r in a1_rows if r.get("attempt_key")}
    no_double = len(kills) == 1 and len(attempts) == 2
    ok = (injected and recovered and no_double
          and _has_event(evs, "LOST_ATTEMPTS_RECONCILED"))
    return _finish(run, sid, injected, ok,
                   f"{ev} ; orphelin fermé KILL×{len(kills)}, tentatives={len(attempts)} "
                   f"(pas de double lancement={no_double})")


_WAITING_E2E = (
    "test -f out/{mid}.txt || exit 1; "
    "echo \"WAITING_FOR_EVIDENCE:$(python3 -c 'import datetime;"
    "print((datetime.datetime.utcnow()+datetime.timedelta(hours=2))"
    ".strftime(\"%Y-%m-%dT%H:%M:%SZ\"))') "
    "verifier=chaos-stub rc=2 head=" + ZERO_HEAD + "\" >&2; exit 2"
)


def _backdate_ledger(pd: Path, seconds: float) -> None:
    """Simule le temps qui passe sans lancement : recule started_at de toutes les
    lignes du ledger de lancements (l'injection du « jalon immobile 30 min »)."""
    from datetime import datetime, timedelta, timezone
    ledger = pd / "evidence" / "attempt_accounts.jsonl"
    if not ledger.is_file():
        return
    old = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    shifted = old.isoformat(timespec="seconds")
    out_lines = []
    for line in ledger.read_text().splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            out_lines.append(line)
            continue
        if row.get("started_at"):
            row["started_at"] = shifted
        out_lines.append(json.dumps(row))
    ledger.write_text("\n".join(out_lines) + "\n")


def scenario_idle_no_progress(root: Path) -> dict:
    sid = "IDLE_NO_PROGRESS"
    waiting = {"product_e2e": {"test_command": _WAITING_E2E.format(mid="A")}}
    pd, _ = make_disposable_project(root, [_local_milestone("A", waiting),
                                           _local_milestone("B")])
    stub = StubExecutor(pd, lambda m, a, p, s: s.ok(m, engine=p))
    run = Run(pd, stub)
    run.cycle()                       # cycle 1 : mission PASS, preuve au calendrier → jalon en attente
    evs = run.events()
    waiting_seen = (_has_event(evs, "MILESTONE_WAITING_FOR_EVIDENCE")
                    or load_project_state(pd)["completed_milestones"]
                    .get("A", {}).get("state") == "WAITING_FOR_EVIDENCE")
    # injection : 31 min sans AUCUN lancement (le temps que la garde idle mesure)
    _backdate_ledger(pd, 31 * 60 + 60)
    injected = waiting_seen
    for _ in range(MAX_CYCLES - 1):
        run.cycle()
        if _settled_milestones(run.events()):
            break
    evs = run.events()
    recovered, ev = _recovered_on_settle(run, "A")
    ok = (injected and recovered and _has_event(evs, "IDLE_GUARD_TRIPPED")
          and _has_event(evs, "STUCK_MILESTONE_YIELDED"))
    return _finish(run, sid, injected, ok, ev)


_SCENARIOS = {
    "QUOTA_CUT_MID_BUILD": scenario_quota_cut_mid_build,
    "ENGINE_KILLED_BY_SIGNAL": scenario_engine_killed_by_signal,
    "RELOAD_DURING_BUILD": scenario_reload_during_build,
    "REVIEW_BLOCK": scenario_review_block,
    "STALE_CONTRACT": scenario_stale_contract,
    "ROSTER_IMPOSSIBLE": scenario_roster_impossible,
    "DIRTY_WORKTREE_MERGE": scenario_dirty_worktree_merge,
    "ENGINE_REFUSAL": scenario_engine_refusal,
    "BUDGET_EXHAUSTED": scenario_budget_exhausted,
    "MISSING_DELIVERABLE": scenario_missing_deliverable,
    "DAEMON_CRASH_MID_MISSION": scenario_daemon_crash_mid_mission,
    "IDLE_NO_PROGRESS": scenario_idle_no_progress,
}


# ═══════════════════════════════ CLI / reçu ═══════════════════════════════════

def _registry() -> dict:
    """Implémentations V1 (ce module) + V2 (`chaos_drill_v2`, chargé à la demande :
    le jeu V1 ne dépend de rien de nouveau)."""
    reg = dict(_SCENARIOS)
    try:
        from .chaos_drill_v2 import _SCENARIOS_V2
        reg.update(_SCENARIOS_V2)
    except ImportError:
        pass
    return reg


def run_scenario(sid: str, keep: bool = False) -> dict:
    reg = _registry()
    known = [s for ids in SETS.values() for s in ids]
    if sid not in reg or sid not in known:
        raise SystemExit(f"scénario inconnu: {sid!r} (attendus: {known})")

    def _run(tmp: Path) -> dict:
        try:
            return reg[sid](tmp)
        except Exception as exc:                   # noqa: BLE001 — reçu honnête, jamais un crash
            return {"id": sid, "injected": False, "recovered": False,
                    "recovery_cycles": 0, "recovery_s": 0.0, "human_interventions": 0,
                    "evidence": f"BENCH_ERROR: {type(exc).__name__}: {exc}"[:400]}

    if keep:   # --keep : le dossier du scénario est laissé pour inspection, par choix explicite de l'appelant
        return _run(Path(tempfile.mkdtemp(prefix=f"chaos_{sid}_")))
    with tempfile.TemporaryDirectory(prefix=f"chaos_{sid}_", ignore_cleanup_errors=True) as td:
        return _run(Path(td))   # FACTORY_REPO_HYGIENE_V1 : retiré même si un enfant a laissé des fichiers


def _head_sha() -> str:
    r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(_REPO_ROOT),
                       capture_output=True, text=True, timeout=15)
    return r.stdout.strip()


def build_receipt(ids: list, keep: bool = False) -> dict:
    entries = []
    for sid in ids:
        entry = run_scenario(sid, keep=keep)
        entries.append(entry)
        print(f"[chaos] {sid}: injected={entry['injected']} recovered={entry['recovered']} "
              f"cycles={entry['recovery_cycles']} {entry['recovery_s']}s", file=sys.stderr)
    return {"schema": "chaos-drill-receipt/v1", "scenarios": entries,
            "run_at": _now_iso(), "control_sha": _head_sha()}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--all", action="store_true", help="exécute les 12 scénarios")
    ap.add_argument("--scenario", action="append", default=[],
                    help="exécute CE scénario uniquement (répétable)")
    ap.add_argument("--out", default="", help="chemin du reçu JSON (défaut: stdout)")
    ap.add_argument("--keep", action="store_true",
                    help="conserve les projets jetables (debug)")
    ap.add_argument("--set", default="", choices=[""] + sorted(SETS),
                    help="jeu de scénarios : v1 (les 12 épinglés, défaut de --all) ou v2 "
                         "(FACTORY_CHAOS_DRILL_V2 : les 8 pannes de la nuit 19→20/09)")
    a = ap.parse_args(argv)
    ids: list = []
    allowed = list(SETS[a.set]) if a.set else [s for ids_ in SETS.values() for s in ids_]
    if a.all:
        ids = list(SETS[a.set or "v1"])     # sans --set : les 12 V1, strictement comme avant
    elif a.scenario:
        ids = list(dict.fromkeys(a.scenario))
        foreign = [s for s in ids if s not in allowed]
        if foreign:
            ap.error(f"scénario(s) hors du jeu {a.set or 'v1+v2'}: {foreign} (attendus: {allowed})")
    else:
        ap.error("préciser --all ou --scenario <ID>")
    receipt = build_receipt(ids, keep=a.keep)
    text = json.dumps(receipt, indent=2, ensure_ascii=False) + "\n"
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)   # --out dans un répertoire absent
        Path(a.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    all_ok = all(s.get("injected") and s.get("recovered") for s in receipt["scenarios"])
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
