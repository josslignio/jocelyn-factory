#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, pathlib, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
# `memory_store` est un ACCESSOIRE de la même espèce que `tombstone` : il porte la mémoire
# canonique des leçons (factory/memory/lessons.jsonl), mais factoryctl doit rester utilisable sans lui —
# un déploiement minimal peut n'embarquer que factoryctl.py (la porte d'arrêt le construit).
# Même sys.path que tombstone (factory/bin déjà inséré ci-dessus), même garde défensive.
try:
    import memory_store
except Exception as _memory_store_exc:  # noqa: BLE001 — un accessoire absent n'est pas une panne
    memory_store = None
    MEMORY_STORE_UNAVAILABLE = f"{type(_memory_store_exc).__name__}: {_memory_store_exc}"
else:
    MEMORY_STORE_UNAVAILABLE = None
# Racine produit où vit factory/memory/lessons.jsonl : deux niveaux au-dessus de factory/bin (le repo).
# Dérivée de __file__ — NI --repo NI --state-dir (qui désignent le control-repo de la factory).
MEMORY_ROOT=pathlib.Path(__file__).resolve().parents[2]
# Racine du control-repo, pour que `factory_v2` (paquet frère de factory/) soit importable —
# même garde défensive que memory_store : `weekly-refresh-observed` doit échouer typé (rc=2),
# jamais par une ImportError non attrapée, si factory_v2 est absent d'un déploiement minimal.
sys.path.insert(0, str(MEMORY_ROOT))
try:
    from factory_v2.executors import weekly_refresh_observed_cli, weekly_refresh_materialize_cli
except Exception as _weekly_refresh_exc:  # noqa: BLE001
    weekly_refresh_observed_cli = None
    weekly_refresh_materialize_cli = None
    WEEKLY_REFRESH_UNAVAILABLE = f"{type(_weekly_refresh_exc).__name__}: {_weekly_refresh_exc}"
else:
    WEEKLY_REFRESH_UNAVAILABLE = None

try:
    from ledger import (LedgerMixin, utc, run, load_jsonl, atomic_jsonl, file_sha256,
                        check_actor_authorized, TERMINAL, INFRA_BACKOFF_SEC,
                        MAX_STOP_BLOCKS_PER_SESSION, DEAUTHORIZED_ACTORS, RUNTIME_RECEIPT_SCHEMA,
                        RUNTIME_PROVIDERS, ORACLE_TIMEOUT_SEC, INDEPENDENT_REVIEW_PROVIDERS,
                        NETWORK_INFRA_REASON_CODE, PIPELINE_STATUSES, RECORD_VERDICTS,
                        UNBLOCKABLE_STATUSES, MIN_UNBLOCK_REASON_CHARS)
    from receipt import ReceiptMixin, validate_runtime_receipt
    from milestone import MilestoneMixin
except Exception as _core_exc:  # noqa: BLE001 — --help/memory survive a partial deployment
    CORE_UNAVAILABLE = f"{type(_core_exc).__name__}: {_core_exc}"

    class LedgerMixin: pass
    class ReceiptMixin: pass
    class MilestoneMixin: pass
else:
    CORE_UNAVAILABLE = None


class Factory(LedgerMixin, ReceiptMixin, MilestoneMixin):
    def command(self, cmd, cwd=None, check=True, env=None):
        """Stable process seam retained across the split (tests and callers may replace run)."""
        kwargs={"cwd":cwd,"check":check}
        if env is not None:kwargs["env"]=env
        return run(cmd,**kwargs)


def _memory_unavailable(cmd):
    raise SystemExit(f"{cmd}: memory_store unavailable — {MEMORY_STORE_UNAVAILABLE}")

def cmd_lessons():
    """docs/05_MEMORY_IMPROVEMENT.md documente un `factoryctl promote-lesson` qui n'existait pas ;
    la mémoire canonique n'avait ni lecteur ni scripteur en code. Ceci ferme ce gap en déléguant
    à memory_store (le seul module qui sait lire/écrire factory/memory/lessons.jsonl sans perte)."""
    if memory_store is None:_memory_unavailable("lessons")
    lessons,ignored,sources=memory_store.load_lessons(MEMORY_ROOT)
    for l in lessons:
        tag="/".join(str(l[k]) for k in ("severity","area") if l.get(k))
        print(f'{l["id"]}  {tag}' if tag else f'{l["id"]}')
    print(f'{len(lessons)} lesson(s) from {len(sources)} file(s): {", ".join(sources)}')
    if ignored:
        print(f'{len(ignored)} rejected line(s) — never hidden:',file=sys.stderr)
        for ign in ignored:
            print(f'  {ign["path"]}:{ign["lineno"]}: {ign["reason"]}',file=sys.stderr)

def cmd_promote_lesson(lesson_file):
    if memory_store is None:_memory_unavailable("promote-lesson")
    if not lesson_file:raise SystemExit("promote-lesson requires --lesson-file <path>")
    p=pathlib.Path(lesson_file)
    if not p.is_file():raise RuntimeError(f"promote-lesson: lesson file not found: {p}")
    try:lesson=json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:raise RuntimeError(f"promote-lesson: invalid JSON in {p}: {e}")
    if not isinstance(lesson,dict):raise RuntimeError(f"promote-lesson: {p} must hold one JSON object, got {type(lesson).__name__}")
    # DuplicateLessonId n'est PAS rattrapée : memory_store la lève, on laisse son message intact
    # remonter à l'utilisateur — jamais l'avaler dans un message générique.
    print(str(memory_store.append_lesson(MEMORY_ROOT,lesson)))

def cmd_weekly_refresh_observed(a):
    """LOT C17 — déclenche le refresh hebdo réel TR_WEEKLY_DEEP_REFRESH_V1 depuis la
    factory (pas depuis le planificateur produit). N'opère ni sur --repo/--state-dir
    ni sur le lock Factory : c'est un appel autonome, un seul par requête (single-flight
    interne à weekly_refresh_observed_cli), dispatché AVANT la construction Factory —
    exactement comme lessons/promote-lesson."""
    if weekly_refresh_observed_cli is None:
        print(json.dumps({"rc":2,"reason":f"WEEKLY_REFRESH_UNAVAILABLE: {WEEKLY_REFRESH_UNAVAILABLE}"}))
        return 2
    if not a.project:
        print(json.dumps({"rc":2,"reason":"weekly-refresh-observed requires --project <path>"}))
        return 2
    # LOT C23: `--materialize-observation <observation_id>` is a distinct mode
    # (commit + emit v2 receipt for an already-stored, real observation --
    # never re-runs the refresh command) and does not take --refresh-seq.
    if a.materialize_observation:
        result=weekly_refresh_materialize_cli(
            project_dir=pathlib.Path(a.project), observation_id=a.materialize_observation,
            lock_dir=(pathlib.Path(a.lock_dir) if a.lock_dir else None))
        print(json.dumps(result,indent=2,sort_keys=True))
        return int(result.get("rc",2))
    if a.refresh_seq not in (1,2):
        print(json.dumps({"rc":2,"reason":"weekly-refresh-observed requires --refresh-seq 1 or 2"}))
        return 2
    result=weekly_refresh_observed_cli(
        project_dir=pathlib.Path(a.project), refresh_seq=a.refresh_seq,
        scheduled_at=a.scheduled_at, dry_run=bool(a.dry_run),
        lock_dir=(pathlib.Path(a.lock_dir) if a.lock_dir else None))
    print(json.dumps(result,indent=2,sort_keys=True))
    return int(result.get("rc",2))

def main():
    p=argparse.ArgumentParser()
    p.add_argument("command",choices=["status","next","prepare","mark-spec-ready","record","infra-failure","recover","unblock","promote","verify","verify-runtime","record-stop-event","preflight-quota","lessons","promote-lesson","audit-independence","waive-review","check-reviewer-isolation","weekly-refresh-observed"])
    # `weekly-refresh-observed` (LOT C17) opère sur --project (le project_dir canonique,
    # pas le control-repo) et n'a besoin ni de --repo ni de --state-dir — comme lessons/
    # promote-lesson, donc --repo n'est plus required=True au niveau argparse ; les commandes
    # qui en ont réellement besoin l'exigent explicitement juste avant de construire Factory.
    p.add_argument("--repo");p.add_argument("--state-dir")
    p.add_argument("--milestone");p.add_argument("--base",default="HEAD")
    p.add_argument("--kind",choices=["plan","build","test","qa","sha","retro","audit"])
    p.add_argument("--artifact");p.add_argument("--reason")
    p.add_argument("--attempt-id");p.add_argument("--provider");p.add_argument("--error-class")
    p.add_argument("--candidate-sha")
    p.add_argument("--force",action="store_true")
    p.add_argument("--target");p.add_argument("--authorizations")
    # DEF-02 (FIX-5): the tooled exit from `blocked` — who unblocks (--actor), why (--reason,
    # reused from infra-failure), and to which ledger-proven status (--to, confirmed not chosen).
    p.add_argument("--to");p.add_argument("--actor")
    p.add_argument("--campaign-id");p.add_argument("--project");p.add_argument("--session-id")
    p.add_argument("--decision",choices=["block","allow"])
    p.add_argument("--live",action="store_true")
    p.add_argument("--lesson-file")
    p.add_argument("--refresh-seq",type=int,choices=[1,2])
    p.add_argument("--scheduled-at")
    # LOT C23: recovery path -- commit + emit a v2 receipt for an already-stored,
    # real observation (identified by its observation_id) without re-running the
    # refresh command. Mutually exclusive with --refresh-seq/--dry-run in practice
    # (cmd_weekly_refresh_observed checks it first and never reads --refresh-seq
    # when set).
    p.add_argument("--materialize-observation")
    p.add_argument("--dry-run",action="store_true")
    # Test-only override of the single-flight lock's directory (defaults to
    # <control-repo>/factory/run inside weekly_refresh_observed_cli). Production
    # callers (the launchd plist) never pass this — one real lock per control-repo.
    p.add_argument("--lock-dir")
    a=p.parse_args()
    # `lessons`/`promote-lesson` opèrent sur la mémoire canonique du produit (factory/memory/lessons.jsonl
    # à la racine du repo, via __file__) — PAS sur le control-repo de la factory : ni Factory ni
    # lock. Dispatchés AVANT la construction, donc --state-dir n'est pas requis pour eux.
    if a.command=="lessons":cmd_lessons();return
    if a.command=="promote-lesson":cmd_promote_lesson(a.lesson_file);return
    # `weekly-refresh-observed` (LOT C17) opère sur --project, hors Factory/--repo/lock —
    # même statut que lessons/promote-lesson ci-dessus.
    if a.command=="weekly-refresh-observed":raise SystemExit(cmd_weekly_refresh_observed(a))
    if CORE_UNAVAILABLE:
        raise SystemExit(f"factoryctl core modules unavailable: {CORE_UNAVAILABLE}")
    if not a.repo:
        raise SystemExit(f"{a.command} requires --repo")
    # L6_bin_lib_products_1-004: --state-dir is optional for argparse but the
    # core commands build Factory(path, Path(state_dir)) right below — a None
    # there crashed with a raw TypeError instead of a usage error.
    if not a.state_dir:
        raise SystemExit(f"{a.command} requires --state-dir")
    f=Factory(pathlib.Path(a.repo),pathlib.Path(a.state_dir))
    with f.locked():
        if a.command=="status":f.status()
        elif a.command=="next":print(json.dumps(f.next(),indent=2) if f.next() else "")
        elif a.command=="prepare":f.prepare(a.milestone,a.base)
        elif a.command=="mark-spec-ready":f.mark_spec_ready(a.milestone)
        elif a.command=="record":f.record(a.milestone,a.kind,a.artifact)
        elif a.command=="infra-failure":f.infra_failure(a.milestone,a.reason or "unspecified",a.attempt_id,a.provider,a.error_class)
        elif a.command=="recover":f.recover(a.force)
        elif a.command=="unblock":
            if not a.milestone or not a.to or not a.actor or not a.reason:
                raise SystemExit("unblock requires --milestone, --to, --actor and --reason")
            f.unblock(a.milestone,a.to,a.actor,a.reason)
        elif a.command=="promote":f.promote(a.milestone,a.target,a.authorizations)
        elif a.command=="verify":f.verify_events();print("OK")
        elif a.command=="verify-runtime":
            if not a.provider or not a.candidate_sha:raise SystemExit("verify-runtime requires --provider and --candidate-sha")
            f.verify_runtime(a.milestone,a.provider,a.candidate_sha)
        elif a.command=="record-stop-event":
            if not a.campaign_id or not a.session_id or not a.decision:
                raise SystemExit("record-stop-event requires --campaign-id, --session-id, --decision")
            f.record_stop_event(a.campaign_id,a.project,a.session_id,a.decision,a.reason)
        elif a.command=="preflight-quota":
            raise SystemExit(f.preflight_quota(a.live))
        elif a.command=="audit-independence":
            f.audit_independence()
        elif a.command=="waive-review":
            if not a.milestone or not a.actor or not a.reason:
                raise SystemExit("waive-review requires --milestone, --actor and --reason")
            f.waive_review(a.milestone,a.actor,a.reason)
        elif a.command=="check-reviewer-isolation":
            f.check_reviewer_isolation()
if __name__=="__main__":raise SystemExit(main())
