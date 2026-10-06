from __future__ import annotations
import datetime as dt, fcntl, hashlib, json, os, pathlib, subprocess, sys, tempfile

TERMINAL={"done","blocked","blocked_infra","cancelled"}
INFRA_BACKOFF_SEC=[30,120,300]
MAX_STOP_BLOCKS_PER_SESSION=3
DEAUTHORIZED_ACTORS={"ruflo","hermes-agent"}
RUNTIME_RECEIPT_SCHEMA="job-radar-runtime-receipt/v1"
RUNTIME_PROVIDERS=("builder","z","codex")
ORACLE_TIMEOUT_SEC=1800
INDEPENDENT_REVIEW_PROVIDERS=tuple(p for p in RUNTIME_PROVIDERS if p!="builder")
NETWORK_INFRA_REASON_CODE="LIVE_NETWORK_UNAVAILABLE"
PIPELINE_STATUSES=("pending","prepared","spec_ready","plan_accepted","built","tests_pass",
                    "qa_accepted","sha_accepted","retro_done")
RECORD_VERDICTS={
    "plan":{"PLAN_ACCEPT","PLAN_BLOCK"},
    "test":{"PASS","FAIL"},
    "qa":{"QA_ACCEPT","QA_REJECT","QA_BLOCK"},
    "sha":{"MILESTONE_ACCEPT","MILESTONE_REJECT","MILESTONE_BLOCK"},
    "audit":{"AUDIT_ACCEPT","AUDIT_REJECT","AUDIT_BLOCK"},
}
UNBLOCKABLE_STATUSES={"blocked","blocked_infra","plan_blocked"}
MIN_UNBLOCK_REASON_CHARS=20


def commit_policy():
    """ORCH-02: FACTORY_STATE_COMMIT_POLICY selects the state_commit() cadence.

    'durable_only' (default) — only durable milestone transitions (event() calls other
    than the Stop-hook's high-frequency "stop_event") trigger a git commit; stop_event
    still appends to the hash-chained events.jsonl (see event()'s commit=... plumbing)
    but does not commit on its own — the next durable transition's state_commit() picks
    up the accumulated bookkeeping alongside its own change.
    'always' — restores the historical per-event commit behaviour (every event() call
    commits immediately), e.g. for tests asserting the old cadence.
    Read from the environment on every call (not cached at import time) so tests can
    flip it per-case without reimporting this module."""
    return os.environ.get("FACTORY_STATE_COMMIT_POLICY", "durable_only")


def utc():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")

def run(cmd,cwd=None,check=True,env=None):
    cp=subprocess.run(cmd,cwd=cwd,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env)
    if check and cp.returncode:
        raise RuntimeError(f"command failed {cp.returncode}: {' '.join(cmd)}\n{cp.stdout}\n{cp.stderr}")
    return cp

def load_jsonl(path):
    rows=[]
    if not path.exists():return rows
    for n,line in enumerate(path.read_text().splitlines(),1):
        if not line.strip():continue
        try:rows.append(json.loads(line))
        except json.JSONDecodeError as e:raise RuntimeError(f"corrupt JSONL {path}:{n}: {e}")
    return rows

def atomic_jsonl(path,rows):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix=path.name+".",dir=str(path.parent))
    try:
        with os.fdopen(fd,"w") as f:
            for row in rows:f.write(json.dumps(row,sort_keys=True)+"\n")
            f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)

def file_sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def check_actor_authorized(actor):
    if actor and str(actor).strip().lower() in DEAUTHORIZED_ACTORS:
        raise RuntimeError(f"INVALID_REVIEW: actor {actor!r} is deauthorized as an authoritative "
                            "writer/reviewer (JR-LIVE-RECOVERY Phase 9.6 — Ruflo/Hermes-agent cut)")


class LedgerMixin:
    def __init__(self,repo,state):
        self.repo=repo.resolve();self.state=state.resolve()
        self.roadmap=self.state/"roadmap.jsonl"
        self.events=self.state/"events.jsonl"
        self.evidence=self.state/"evidence"
        self.lock=self.state/"locks/factory.lock"
        self.worktrees=self.state/"worktrees"
        self.retros=self.state/"retrospectives"
    def locked(self):
        self.lock.parent.mkdir(parents=True,exist_ok=True)
        fh=open(self.lock,"a+");fcntl.flock(fh,fcntl.LOCK_EX);return fh
    def state_commit(self,msg):
        """Commit factory state (events.jsonl, roadmap.jsonl, evidence/, ...) into the repo.

        ORCH-02: event() calls this for every durable milestone transition (prepare,
        spec_ready, built, tests_pass, sha_accepted, done, unblocked, ...) — that is the
        intended, low-frequency cadence this exists for. The one exception is the
        Claude-Code Stop-hook's "stop_event" (factory/hooks/stop_gate.py ->
        `factoryctl record-stop-event` -> MilestoneMixin.record_stop_event), which fires
        at the end of *every* Claude turn in *any* session: 8 "factory(factory-v1):
        stop_event" commits were observed in an 80-minute window. That churn (a) pollutes
        history with no-op bookkeeping commits, (b) moves HEAD out from under exact-SHA
        receipt validation the instant a stop_event commit lands after a receipt (see
        stop_gate.py's _bookkeeping_only_diff workaround, itself a symptom of this), and
        (c) clutters branch diffs reviewers have to page through. commit_policy() (env
        var FACTORY_STATE_COMMIT_POLICY, default "durable_only") lets callers — currently
        only record_stop_event() — pass commit=False to event() so the event still
        appends to the hash-chained events.jsonl (verify_events() integrity is untouched;
        the append itself is never conditional) without calling this method at all. The
        skipped commit is not lost: git add above stages the whole state dir, so the next
        durable transition's state_commit() bundles the accumulated stop_event/bookkeeping
        writes in with its own change."""
        top=run(["git","rev-parse","--show-toplevel"],cwd=self.state).stdout.strip()
        run(["git","add",str(self.state)],cwd=top)
        cp=run(["git","diff","--cached","--quiet","--",str(self.state)],cwd=top,check=False)
        if cp.returncode:
            run(["git","commit","-m",msg,"--",str(self.state)],cwd=top)
    def rows(self):return load_jsonl(self.roadmap)
    def get(self,mid):
        if not hasattr(self,"_roadmap_cache"):
            self._roadmap_cache={r["id"]:r for r in load_jsonl(self.roadmap)}
        if mid not in self._roadmap_cache:raise KeyError(mid)
        return self._roadmap_cache[mid]
    def set(self,mid,**changes):
        rows=self.rows();found=False
        for r in rows:
            if r["id"]==mid:r.update(changes);found=True
        if not found:raise KeyError(mid)
        atomic_jsonl(self.roadmap,rows)
        self._roadmap_cache={r["id"]:r for r in rows}
    def event(self,mid,stage,actor,evidence=None,sha=None,meta=None,commit=True):
        check_actor_authorized(actor)
        # DEF-06 fix: store evidence path relative to self.state when possible so the
        # event log is portable across clones and machines.
        if evidence is not None:
            p=pathlib.Path(evidence)
            try:
                evidence=str(p.relative_to(self.state))
            except ValueError:
                pass  # outside self.state — keep as-is (e.g. worktree paths)
        rows=load_jsonl(self.events);prev=rows[-1]["event_hash"] if rows else ""
        body={"ts":utc(),"milestone":mid,"stage":stage,"actor":actor,
              "evidence":evidence,"sha":sha,"meta":meta or {},"prev_hash":prev}
        body["event_hash"]=hashlib.sha256(json.dumps(body,sort_keys=True,separators=(",",":")).encode()).hexdigest()
        with open(self.events,"a") as f:
            f.write(json.dumps(body,sort_keys=True)+"\n");f.flush();os.fsync(f.fileno())
        # ORCH-02: the append above is unconditional (hash chain integrity is never
        # affected); only the commit is gated by the caller's commit= flag.
        if commit:
            self.state_commit(f"factory({mid}): {stage}")
    def verify_events(self):
        prev=""
        for n,row in enumerate(load_jsonl(self.events),1):
            got=row["event_hash"];body=dict(row);body.pop("event_hash",None)
            exp=hashlib.sha256(json.dumps(body,sort_keys=True,separators=(",",":")).encode()).hexdigest()
            if got!=exp or body["prev_hash"]!=prev:raise RuntimeError(f"event chain invalid line {n}")
            prev=got
    def verify_evidence(self):
        # DEF-04 fix: a second verify-runtime legitimately overwrites the receipt file and
        # records a NEW event pinning the NEW hash.  Only the LAST event for each evidence
        # path is authoritative; earlier events for the same path are superseded.
        last_pin={}
        for n,row in enumerate(load_jsonl(self.events),1):
            expected=(row.get("meta") or {}).get("artifact_sha256")
            if not expected:continue
            path=row.get("evidence")
            if not path:continue
            last_pin[path]=(n,expected)
        for path,(n,expected) in last_pin.items():
            p=pathlib.Path(path)
            # DEF-06 fix: resolve relative paths (stored after the fix) against self.state;
            # legacy absolute paths are used as-is for backward compatibility.
            if not p.is_absolute():
                p=self.state/p
            if not p.exists():
                raise RuntimeError(f"TAMPERED_EVIDENCE line {n}: {path} referenced by a "
                                    "hash-pinned event is missing (artefact modifié après receipt)")
            if file_sha256(p)!=expected:
                raise RuntimeError(f"TAMPERED_EVIDENCE line {n}: {path} content no longer "
                                    "matches the hash pinned at record-time (artefact modifié "
                                    "après receipt)")
    def git_sha(self,cwd=None):return run(["git","rev-parse","HEAD"],cwd=cwd or self.repo).stdout.strip()
