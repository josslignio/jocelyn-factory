from __future__ import annotations
import datetime as dt, hashlib, json, os, pathlib, sys, uuid
import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
try:
    import tombstone
except Exception as _tombstone_exc:  # noqa: BLE001
    tombstone = None
    TOMBSTONE_UNAVAILABLE = f"{type(_tombstone_exc).__name__}: {_tombstone_exc}"
else:
    TOMBSTONE_UNAVAILABLE = None

from ledger import (file_sha256, run, load_jsonl, utc, check_actor_authorized,
                    TERMINAL, UNBLOCKABLE_STATUSES, RECORD_VERDICTS, PIPELINE_STATUSES,
                    INFRA_BACKOFF_SEC, MIN_UNBLOCK_REASON_CHARS, MAX_STOP_BLOCKS_PER_SESSION,
                    commit_policy)


class MilestoneMixin:
    def status(self):
        self.verify_events()
        self.verify_evidence()
        for r in self.rows():
            extra=""
            if r.get("status")=="waiting_infra":
                extra=f' resume={r.get("resume_stage")} retry_after={r.get("retry_after")}'
            print(f'{r["id"]:6} {r.get("status","pending"):16} repairs={r.get("repairs",0)} infra={r.get("infra_retries",0)}{extra} {r["title"]}')
        if tombstone is None:print(f"tombstone report unavailable: {TOMBSTONE_UNAVAILABLE}")
        else:
            try:print(tombstone.report(self.state),end="")
            except Exception as e:print(f"tombstone report unavailable: {e}")

    def next(self):
        rows=self.rows();done={r["id"] for r in rows if r.get("status")=="done"}
        return next((r for r in rows if r.get("status")=="pending" and set(r.get("deps",[]))<=done),None)

    def ensure_clean_product_root(self):
        dirty=run(["git","status","--porcelain"],cwd=self.repo).stdout.strip()
        if dirty:raise RuntimeError("product root dirty; fail-closed\n"+dirty)

    def _spec_ready_evidence_problems(self,mid,r,wt):
        """DEF-01 (RUN8 P5 FIX-5) — re-derive from disk whether this milestone's `spec_ready`
        proof still holds, instead of trusting the status field."""
        problems=[]
        art=self.art(mid,"spec_ready.json")
        if not art.exists():
            return [f"spec_ready evidence missing: {art}"]
        try:
            payload=json.loads(art.read_text())
        except json.JSONDecodeError as e:
            return [f"spec_ready evidence unreadable ({art}): {e}"]
        for field in ("spec_sha256","plan_sha256","tasks_sha256","acceptance_lock_sha256",
                      "oracle_sha256","toolchain_lock_sha256","network_policy_sha256"):
            if r.get(field) and payload.get(field)!=r.get(field):
                problems.append(f"spec_ready.json {field} disagrees with the roadmap pin")
        if not wt.exists():
            problems.append(f"worktree missing: {wt}")
            return problems
        for name in ("spec","plan","tasks"):
            pin=r.get(f"{name}_sha256")
            if not pin:
                problems.append(f"no {name}_sha256 pinned on the row — spec_ready was never earned")
                continue
            p=wt/"specs"/mid/f"{name}.md"
            if not p.exists():problems.append(f"missing {p}")
            elif file_sha256(p)!=pin:problems.append(f"{name}.md no longer matches its spec_ready pin")
        for field,p in (("acceptance_lock_sha256",wt/"specs"/mid/"acceptance.lock.json"),
                        ("oracle_sha256",wt/"scripts"/"verify_live_product_dod.py"),
                        ("toolchain_lock_sha256",wt/"toolchain-lock.json")):
            pin=r.get(field)
            if not pin:continue
            if not p.exists():problems.append(f"missing {p}, pinned by {field}")
            elif file_sha256(p)!=pin:problems.append(f"{p.name} no longer matches its {field} pin")
        pin=r.get("network_policy_sha256")
        if pin:
            ay=wt/"specs"/mid/"acceptance.yaml"
            policy=((yaml.safe_load(ay.read_text()) or {}).get("network_policy") or {}) if ay.exists() else {}
            got=hashlib.sha256(json.dumps(policy,sort_keys=True).encode()).hexdigest() if policy else None
            if got!=pin:
                problems.append("acceptance.yaml network_policy no longer matches its spec_ready pin")
        return problems

    def _candidate_still_present(self,r,wt):
        """DEF-01 — a milestone at built/tests_pass/qa_accepted/sha_accepted only still holds that
        state if the commit it was accepted on is still on the line the reprise just restored."""
        sha=r.get("candidate_sha")
        if not sha:return True
        if not wt.exists():return False
        return run(["git","merge-base","--is-ancestor",sha,"HEAD"],cwd=wt,check=False).returncode==0

    def prepare(self,mid,base):
        r=self.get(mid);slug=self.repo.name
        # DEF-01 fix: never resurrect a milestone in a terminal state.
        cur=r.get("status") or "pending"
        if cur in TERMINAL:
            hint=(" — leave it with `factoryctl unblock --to <proven status> --actor <who> "
                  "--reason <why>`") if cur in UNBLOCKABLE_STATUSES else ""
            raise RuntimeError(f"prepare refused: {mid} is in terminal status {cur!r}{hint}")
        wt=self.worktrees/slug/mid;branch=f"factory/{mid.lower()}"
        wt.parent.mkdir(parents=True,exist_ok=True)
        if not wt.exists():
            # Fault-injection F011 fix: git worktree prune clears stale entries so the path can
            # be reused, and reusing a surviving branch makes prepare() crash-safe.
            run(["git","worktree","prune"],cwd=self.repo,check=False)
            branch_exists=run(["git","rev-parse","--verify","--quiet",f"refs/heads/{branch}"],
                               cwd=self.repo,check=False).returncode==0
            if branch_exists:
                cp=run(["git","worktree","add",str(wt),branch],cwd=self.repo,check=False)
            else:
                cp=run(["git","worktree","add","-b",branch,str(wt),base],cwd=self.repo,check=False)
            if cp.returncode:raise RuntimeError("worktree creation failed\n"+cp.stdout+cp.stderr)
        base_sha=run(["git","rev-parse",base],cwd=self.repo).stdout.strip()
        run_id=str(uuid.uuid4())
        # DEF-01 fix (RUN8 P5 FIX-5): prepare() keeps acquired state when the proof still holds;
        # falls explicitly to spec_ready (candidate gone) or prepared (proof broken) otherwise.
        acquired=cur not in ("pending","prepared")
        problems=[];candidate_lost=False
        if acquired:
            problems=self._spec_ready_evidence_problems(mid,r,wt)
            candidate_lost=not problems and not self._candidate_still_present(r,wt)
        if acquired and not problems and not candidate_lost:
            self.set(mid,branch=branch,worktree=str(wt),base_sha=base_sha,milestone_run_id=run_id)
            self.event(mid,"prepare_resumed","factoryctl",str(wt),base_sha,
                        meta={"milestone_run_id":run_id,"preserved_status":cur})
            print(wt)
            return
        new="prepared"
        meta={"milestone_run_id":run_id}
        if acquired:
            if candidate_lost:
                new="spec_ready"
                problems=[f"candidate_sha {r.get('candidate_sha')} is no longer reachable in the "
                          "restored worktree's history"]
            meta.update({"state_dropped_from":cur,"state_dropped_to":new,"drop_reasons":problems})
            print(f"STATE_DROPPED {mid}: {cur} -> {new} — the proof of that state is no longer "
                  "valid: "+"; ".join(problems),file=sys.stderr)
        self.set(mid,status=new,branch=branch,worktree=str(wt),base_sha=base_sha,
                  milestone_run_id=run_id,resume_stage=None,retry_after=None)
        self.event(mid,new,"factoryctl",str(wt),base_sha,meta=meta)
        print(wt)

    def art(self,mid,name):return self.evidence/mid/name

    def exact(self,mid,payload):
        expected=self.get(mid).get("candidate_sha");got=payload.get("exact_sha")
        if not expected or got!=expected:raise RuntimeError(f"{mid}: exact SHA mismatch {got} != {expected}")

    def mark_spec_ready(self,mid):
        r=self.get(mid);wt=pathlib.Path(r["worktree"])
        obs=tombstone.observe_entry(r,self.state) if tombstone is not None else {}
        if obs.get("liveness")=="DEAD":raise RuntimeError(f"DEAD_REGISTRY_ENTRY {mid}: worktree gone: {r['worktree']} — "
                                                          f"mark it with `tombstone.py mark`, do not look for the spec")
        paths={n:wt/"specs"/mid/f"{n}.md" for n in ("spec","plan","tasks")}
        for n,p in paths.items():
            if not p.exists() or p.stat().st_size<100:raise RuntimeError(f"missing/incomplete {n}: {p}")
            if "To be produced" in p.read_text():raise RuntimeError(f"placeholder {n}: {p}")
        payload={"milestone":mid,**{f"{n}_sha256":file_sha256(p) for n,p in paths.items()}}
        payload["spec_commit_sha"]=self.git_sha(wt)
        lock_path=wt/"specs"/mid/"acceptance.lock.json"
        # Fault-injection F007 fix: fail closed if the lock file is missing (order rule 4).
        if not lock_path.exists():
            raise RuntimeError(f"missing acceptance.lock.json: {lock_path} (order rule 4 — must "
                                "be committed before spec_ready)")
        payload["acceptance_lock_sha256"]=file_sha256(lock_path)
        # Fault-injection F005 fix: pin the oracle hash at spec_ready to catch oracle edits.
        oracle_path=wt/"scripts"/"verify_live_product_dod.py"
        if oracle_path.exists():
            payload["oracle_sha256"]=file_sha256(oracle_path)
        # Fault-injection F015 fix: pin the toolchain hash at spec_ready.
        toolchain_path=wt/"toolchain-lock.json"
        if toolchain_path.exists():
            payload["toolchain_lock_sha256"]=file_sha256(toolchain_path)
        # Fault-injection F022 fix: pin the network_policy hash at spec_ready.
        acceptance_yaml_path=wt/"specs"/mid/"acceptance.yaml"
        if acceptance_yaml_path.exists():
            policy=(yaml.safe_load(acceptance_yaml_path.read_text()) or {}).get("network_policy") or {}
            if policy:
                payload["network_policy_sha256"]=hashlib.sha256(
                    json.dumps(policy,sort_keys=True).encode()).hexdigest()
        out=self.art(mid,"spec_ready.json");out.parent.mkdir(parents=True,exist_ok=True)
        out.write_text(json.dumps(payload,indent=2))
        self.set(mid,status="spec_ready",**payload)
        self.event(mid,"spec_ready","factoryctl",str(out))

    def record(self,mid,kind,artifact):
        src=pathlib.Path(artifact).resolve();data=json.loads(src.read_text())
        # DEF-02 fix: validate the verdict vocabulary for every kind that reads one.
        # A misspelled verdict is a defective CALL, not failed WORK — fails before touching anything.
        if kind in RECORD_VERDICTS:
            verdict=data.get("verdict") if isinstance(data,dict) else None
            if verdict not in RECORD_VERDICTS[kind]:
                raise RuntimeError(f"INVALID_ARTIFACT ({mid}): unknown {kind} verdict {verdict!r}, "
                                    f"expected one of {sorted(RECORD_VERDICTS[kind])} — rejected as a "
                                    "malformed call, not as a failed review: no repair consumed, no "
                                    "status change, nothing recorded")
        r=self.get(mid);status=r.get("status")
        expected_run=r.get("milestone_run_id")
        if expected_run and data.get("milestone_run_id") and data["milestone_run_id"]!=expected_run:
            raise RuntimeError(f"{mid}: stale artifact, milestone_run_id {data.get('milestone_run_id')} != {expected_run}")
        # Fault-injection F003 fix: qa/sha require milestone_run_id present and current.
        if expected_run and kind in ("qa","sha") and data.get("milestone_run_id")!=expected_run:
            raise RuntimeError(f"{mid}: stale artifact, missing milestone_run_id (expected {expected_run})")
        # L6_bin_lib_products_1-013: the artifact is copied into evidence ONLY
        # AFTER every validation below passes. Copying first let a refused
        # second call (same basename) overwrite a previously ACCEPTED proof,
        # leaving the event chain pointing at bytes that no longer match
        # (TAMPERED_EVIDENCE) — a rejected call corrupting valid state.
        dst=self.art(mid,src.name)
        if kind=="plan":
            if status!="spec_ready" or data.get("verdict") not in {"PLAN_ACCEPT","PLAN_BLOCK"}:raise RuntimeError("invalid plan transition")
            if data.get("spec_sha256")!=r.get("spec_sha256") or data.get("plan_sha256")!=r.get("plan_sha256"):raise RuntimeError("plan hashes mismatch")
            # DEF-08 fix: PLAN_BLOCK now has the same repair-cycle semantics as qa/sha.
            # First block → plan_blocked (unblockable, retry after revision).
            # Second block (repairs already ≥ 1) → blocked (hard stop).
            if data["verdict"]=="PLAN_ACCEPT":
                new="plan_accepted"
            else:
                repairs=r.get("repairs",0)+1;new="blocked" if repairs>=2 else "plan_blocked";self.set(mid,repairs=repairs)
        elif kind=="build":
            if status!="plan_accepted":raise RuntimeError("invalid build transition")
            sha=data.get("exact_sha");wt=pathlib.Path(r["worktree"])
            if sha!=self.git_sha(wt):raise RuntimeError("build SHA != worktree HEAD")
            # Fault-injection F010 fix: a repair must submit a genuinely different candidate.
            if r.get("repairs",0)>0 and sha==r.get("candidate_sha"):
                raise RuntimeError(f"INVALID_REVIEW ({mid}): repair resubmitted identical "
                                    f"candidate_sha {sha} — no new code produced since the last rejection")
            spec_commit_sha=r.get("spec_commit_sha")
            if spec_commit_sha:
                anc=run(["git","merge-base","--is-ancestor",spec_commit_sha,sha],cwd=wt,check=False)
                if anc.returncode!=0:
                    raise RuntimeError(f"INVALID_REVIEW: spec_commit_sha {spec_commit_sha} is not an "
                                        f"ancestor of candidate {sha} (spec-committed-before-build violated)")
            lock_sha=r.get("acceptance_lock_sha256")
            if lock_sha:
                lock_path=wt/"specs"/mid/"acceptance.lock.json"
                if not lock_path.exists() or file_sha256(lock_path)!=lock_sha:
                    raise RuntimeError("INVALID_REVIEW: acceptance.lock.json changed or went missing "
                                        "since spec_ready — requires an explicit return to spec phase")
            # Fault-injection F005 fix: reject if oracle changed after spec_ready.
            oracle_sha=r.get("oracle_sha256")
            if oracle_sha:
                oracle_path=wt/"scripts"/"verify_live_product_dod.py"
                if not oracle_path.exists() or file_sha256(oracle_path)!=oracle_sha:
                    raise RuntimeError("INVALID_REVIEW: scripts/verify_live_product_dod.py changed or "
                                        "went missing since spec_ready — requires an explicit return "
                                        "to spec phase")
            # Fault-injection F015 fix: reject if toolchain changed after spec_ready.
            toolchain_sha=r.get("toolchain_lock_sha256")
            if toolchain_sha:
                toolchain_path=wt/"toolchain-lock.json"
                if not toolchain_path.exists() or file_sha256(toolchain_path)!=toolchain_sha:
                    raise RuntimeError("INVALID_REVIEW: toolchain-lock.json changed or went missing "
                                        "since spec_ready — requires an explicit return to spec phase "
                                        "(toolchain différente)")
            # Fault-injection F022 fix: reject if network_policy changed after spec_ready.
            network_policy_sha=r.get("network_policy_sha256")
            if network_policy_sha:
                acceptance_yaml_path=wt/"specs"/mid/"acceptance.yaml"
                current_policy=None
                if acceptance_yaml_path.exists():
                    current_policy=(yaml.safe_load(acceptance_yaml_path.read_text()) or {}).get("network_policy") or {}
                current_sha=(hashlib.sha256(json.dumps(current_policy,sort_keys=True).encode()).hexdigest()
                             if current_policy else None)
                if current_sha!=network_policy_sha:
                    raise RuntimeError("INVALID_REVIEW: acceptance.yaml network_policy changed or went "
                                        "missing since spec_ready — requires an explicit return to spec "
                                        "phase (policy réseau modifiée)")
            self.set(mid,candidate_sha=sha);new="built"
        elif kind=="test":
            if status!="built":raise RuntimeError("invalid test transition")
            self.exact(mid,data);new="tests_pass" if data.get("verdict")=="PASS" else "blocked"
        elif kind=="qa":
            if status!="tests_pass":raise RuntimeError("invalid QA transition")
            self.exact(mid,data)
            self._require_runtime_receipt(mid,r,data)
            if data.get("verdict")=="QA_ACCEPT":new="qa_accepted"
            else:
                repairs=r.get("repairs",0)+1;new="blocked" if repairs>=2 else "plan_accepted";self.set(mid,repairs=repairs)
        elif kind=="sha":
            if status!="qa_accepted":raise RuntimeError("invalid SHA transition")
            self.exact(mid,data)
            self._require_runtime_receipt(mid,r,data)
            self._require_runtime_sha_agreement(mid,r)
            if data.get("verdict")=="MILESTONE_ACCEPT":new="sha_accepted"
            else:
                repairs=r.get("repairs",0)+1;new="blocked" if repairs>=2 else "plan_accepted";self.set(mid,repairs=repairs)
        elif kind=="retro":
            if status!="sha_accepted":raise RuntimeError("invalid retro transition")
            new="retro_done"
        elif kind=="audit":
            # JR-M0-class milestones: audit path bypasses the code-shaped state machine.
            if status!="pending":raise RuntimeError("invalid audit transition")
            if data.get("verdict")=="AUDIT_ACCEPT":
                plan_path=pathlib.Path(data["plan_review_path"]) if data.get("plan_review_path") else None
                qa_path=pathlib.Path(data["qa_review_path"]) if data.get("qa_review_path") else None
                if not plan_path or not plan_path.exists() or "PLAN_ACCEPT" not in plan_path.read_text():
                    raise RuntimeError("INVALID_REVIEW: audit plan_review_path missing or does not "
                                        "contain PLAN_ACCEPT")
                if not qa_path or not qa_path.exists() or "QA_ACCEPT" not in qa_path.read_text():
                    raise RuntimeError("INVALID_REVIEW: audit qa_review_path missing or does not "
                                        "contain QA_ACCEPT")
                new="retro_done"
            else:
                new="blocked"
        else:raise RuntimeError(kind)
        dst.parent.mkdir(parents=True,exist_ok=True);dst.write_bytes(src.read_bytes())
        self.set(mid,status=new)
        # F006 fix: pin the accepted artifact's own content hash into the tamper-evident event chain.
        self.event(mid,new,kind,str(dst),self.get(mid).get("candidate_sha"),
                   meta={"artifact_sha256":file_sha256(dst)})

    def infra_failure(self,mid,reason,attempt_id=None,provider=None,error_class=None):
        r=self.get(mid)
        if attempt_id:
            for row in load_jsonl(self.events):
                if row.get("stage")=="infra_failure" and row.get("milestone")==mid and (row.get("meta") or {}).get("attempt_id")==attempt_id:
                    print(f"DEDUP already recorded attempt_id={attempt_id}, infra_retries unchanged")
                    return
        n=r.get("infra_retries",0)+1
        resume_stage=r.get("status","pending")
        # L6_bin_lib_products_1-014 : une deuxième panne pendant waiting_infra
        # remplaçait resume_stage par waiting_infra — recover(force=True)
        # restaurait alors ce pseudo-stade au lieu du stade productif initial,
        # et le jalon bouclait sur des reprises improductives. On conserve le
        # stade d'origine et on refuse une cible de reprise non productive.
        if resume_stage=="waiting_infra":
            original=r.get("resume_stage")
            if original and original not in ("waiting_infra","blocked","blocked_infra"):
                resume_stage=original
            else:
                raise RuntimeError(f"{mid}: waiting_infra with no productive resume_stage, refusing to guess")
        if n>=3:
            new="blocked_infra";retry_after=None
        else:
            new="waiting_infra"
            backoff_sec=INFRA_BACKOFF_SEC[min(n-1,len(INFRA_BACKOFF_SEC)-1)]
            retry_after=(dt.datetime.now(dt.timezone.utc)+dt.timedelta(seconds=backoff_sec)).replace(microsecond=0).isoformat().replace("+00:00","Z")
        self.set(mid,infra_retries=n,status=new,resume_stage=resume_stage,retry_after=retry_after,
                  last_provider=provider,last_error_class=error_class)
        self.event(mid,"infra_failure","factoryctl",meta={"count":n,"reason":reason,"attempt_id":attempt_id,
                    "provider":provider,"error_class":error_class,"resume_stage":resume_stage,"retry_after":retry_after})

    def promote(self,mid,target=None,authorizations=None):
        """Factory V1 P0 fix (forensic gap 1): target is mandatory and explicit — never the
        implicit currently-checked-out branch. main additionally requires a human authorization
        file with merge_main=true (order rule 18 / §7.4 / §3)."""
        if not target:
            raise RuntimeError("promote: --target is required (order §7.4: cible explicite obligatoire, "
                                "aucune branche courante implicite)")
        current=run(["git","branch","--show-current"],cwd=self.repo).stdout.strip()
        if current!=target:
            raise RuntimeError(f"promote: repo is on {current!r}, not requested target {target!r} — "
                                "refusing to merge into an implicit branch")
        if target=="main":
            auth=json.loads(pathlib.Path(authorizations).read_text()) if authorizations and pathlib.Path(authorizations).exists() else {}
            if not auth.get("merge_main"):
                raise RuntimeError("WAITING_HUMAN: promote to main requires human-authorizations.json "
                                    "merge_main=true (order rule 18) — none found/true")
        r=self.get(mid)
        if r.get("status")!="retro_done":raise RuntimeError("not ready")
        self._require_runtime_sha_agreement(mid,r)
        # L6_bin_lib_products_1-015: merge the EXACT accepted SHA, never the
        # mutable branch name. The SHA agreement check above pinned what was
        # reviewed; r['branch'] can receive further commits after acceptance
        # and merging it by name would bless unreviewed content. Rows that
        # predate candidate_sha pinning fall back to the branch's CURRENT
        # tip, resolved to a commit — still never a name passed to git merge.
        merge_target=r.get("candidate_sha")
        if not merge_target:
            if not r.get("branch"):
                raise RuntimeError(f"{mid}: neither candidate_sha nor branch pinned — nothing reviewable to merge")
            tip=run(["git","rev-parse","--verify",r["branch"]],cwd=self.repo,check=False)
            merge_target=tip.stdout.strip() if tip.returncode==0 else ""
            if not merge_target:
                raise RuntimeError(f"{mid}: branch {r['branch']!r} unresolvable — refusing to merge")
        self.ensure_clean_product_root()
        cp=run(["git","merge","--no-ff",merge_target,"-m",f"merge {mid} exact-SHA accepted"],cwd=self.repo,check=False)
        if cp.returncode:
            run(["git","merge","--abort"],cwd=self.repo,check=False)
            self.set(mid,status="blocked");self.event(mid,"merge_conflict","factoryctl",meta={"stderr":cp.stderr[-2000:]})
            raise RuntimeError("merge conflict; merge aborted")
        merge_sha=self.git_sha()
        run(["git","worktree","remove","--force",r["worktree"]],cwd=self.repo,check=False)
        run(["git","branch","-D",r["branch"]],cwd=self.repo,check=False)
        self.set(mid,status="done",merge_sha=merge_sha)
        self.event(mid,"done","factoryctl",sha=merge_sha)
        print(merge_sha)

    def record_stop_event(self,campaign_id,project,session_id,decision,reason):
        """Order §5.4/§6 — the Stop-hook never writes progress.json directly; this is the sole
        entrypoint. Tracks a per-session block counter bounded by MAX_STOP_BLOCKS_PER_SESSION.

        ORCH-02: this fires once per Claude turn in every session (8 commits observed in
        80 minutes when each call committed) so, unlike every other self.event() call in
        this file, it does NOT commit by default (commit_policy()=="durable_only", the
        default). The event still appends to the hash-chained events.jsonl and the block
        counter is still written to disk — only the git commit is deferred to the next
        durable transition. Set FACTORY_STATE_COMMIT_POLICY=always to restore the
        historical per-stop commit."""
        counters_path=self.state/"stop_block_counters.json"
        counters=json.loads(counters_path.read_text()) if counters_path.exists() else {}
        key=f"{campaign_id}:{project}:{session_id}"
        n=counters.get(key,0)
        if decision=="block":
            n+=1
        else:
            n=0
        counters[key]=n
        counters_path.parent.mkdir(parents=True,exist_ok=True)
        tmp=counters_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(counters,indent=2));os.replace(tmp,counters_path)
        escalated=n>MAX_STOP_BLOCKS_PER_SESSION
        result={"campaign_id":campaign_id,"project":project,"session_id":session_id,
                "decision":decision,"reason":reason,"block_count":n,
                "max_blocks":MAX_STOP_BLOCKS_PER_SESSION,"escalated":escalated}
        self.event(project or campaign_id,"stop_event","stop-hook",meta=result,
                   commit=(commit_policy()=="always"))
        print(json.dumps(result,indent=2))
        return result

    def preflight_quota(self,live=False,script_path=None):
        """order §7.1 — CLI presence, no paid API keys, toolchain present, optionally real
        headless auth (--live). Wraps factory/bin/auth_doctor.sh rather than duplicating its logic."""
        script=pathlib.Path(script_path) if script_path else pathlib.Path(__file__).resolve().parent/"auth_doctor.sh"
        args=["bash",str(script)]+(["--live"] if live else [])
        cp=run(args,check=False)
        print(cp.stdout)
        if cp.returncode!=0:
            print(cp.stderr,file=sys.stderr)
        ok=cp.returncode==0 and "DOCTOR_PASS" in cp.stdout
        print(f"PREFLIGHT_QUOTA={'PASS' if ok else 'FAIL'}")
        return 0 if ok else 1

    def _last_proven_status(self,mid):
        """DEF-02 (RUN8 P5 FIX-5) — the status this milestone actually held, and proved, just
        before it was blocked, re-derived from the hash-chained ledger. The caller's --to must
        MATCH it — a confirmation, never a choice."""
        prior=None
        for row in load_jsonl(self.events):
            if row.get("milestone")!=mid:continue
            stage=row.get("stage");meta=row.get("meta") or {}
            if stage in PIPELINE_STATUSES and stage!="pending":
                prior=stage
            elif stage=="infra_failure" and meta.get("resume_stage") in PIPELINE_STATUSES:
                prior=meta["resume_stage"]
            elif stage in ("recovered","unblocked") and meta.get("to") in PIPELINE_STATUSES:
                prior=meta["to"]
            elif stage=="prepare_resumed" and meta.get("preserved_status") in PIPELINE_STATUSES:
                prior=meta["preserved_status"]
        return prior

    def unblock(self,mid,to_status,actor,reason):
        """DEF-02 fix (RUN8 P5 FIX-5) — the tooled, traced exit from `blocked`/`blocked_infra`.
        Demands a named actor and a written reason; resume point re-derived from the ledger.
        `repairs` and `infra_retries` are deliberately NOT reset."""
        r=self.get(mid);cur=r.get("status")
        if cur not in UNBLOCKABLE_STATUSES:
            raise RuntimeError(f"unblock refused: {mid} is {cur!r}, not one of "
                                f"{sorted(UNBLOCKABLE_STATUSES)}")
        actor=(actor or "").strip()
        if not actor or actor.lower() in {"factoryctl","factory","unknown","none"}:
            raise RuntimeError("unblock refused: --actor must name who unblocks (a human, or a "
                                "named agent) — factoryctl cannot unblock itself")
        check_actor_authorized(actor)
        reason=(reason or "").strip()
        if len(reason)<MIN_UNBLOCK_REASON_CHARS:
            raise RuntimeError(f"unblock refused: --reason must be a written justification of at "
                                f"least {MIN_UNBLOCK_REASON_CHARS} characters (got {len(reason)}) — "
                                "leaving a serious state has to be argued, and it stays in the ledger")
        prior=self._last_proven_status(mid)
        if not prior:
            raise RuntimeError(f"unblock refused: the ledger proves no status for {mid} before it "
                                "was blocked — refusing to guess where to resume")
        if to_status!=prior:
            raise RuntimeError(f"unblock refused: --to {to_status!r} is not the status {mid} proved "
                                f"before it was blocked ({prior!r}) — unblock resumes exactly where "
                                "the milestone stopped, it never grants a state never earned")
        n=r.get("unblocks",0)+1
        repairs=r.get("repairs",0);infra=r.get("infra_retries",0)
        self.set(mid,status=prior,unblocks=n,unblocked_by=actor,unblocked_reason=reason,
                  unblocked_at=utc(),retry_after=None)
        self.event(mid,"unblocked",actor,meta={"from":cur,"to":prior,"reason":reason,
                    "unblock_count":n,"repairs_preserved":repairs,"infra_retries_preserved":infra})
        print(f"UNBLOCKED {mid}: {cur} -> {prior} by {actor} (unblock #{n}; repairs={repairs} and "
              f"infra_retries={infra} preserved, not reset)")

    def recover(self,force=False):
        changed=0
        now=dt.datetime.now(dt.timezone.utc)
        for r in self.rows():
            if r.get("status")!="waiting_infra":continue
            retry_after=r.get("retry_after")
            if retry_after and not force:
                if dt.datetime.fromisoformat(retry_after.replace("Z","+00:00"))>now:
                    continue
            resume_stage=r.get("resume_stage")
            if not resume_stage:raise RuntimeError(f"{r['id']}: waiting_infra with no resume_stage, refusing to guess")
            self.set(r["id"],status=resume_stage,retry_after=None)
            self.event(r["id"],"recovered","factoryctl",meta={"from":"waiting_infra","to":resume_stage});changed+=1
        print(changed)

    def audit_independence(self):
        """DEF-05 fix — identify milestones that reached sha_accepted/retro_done under the
        runtime-oracle contract (acceptance_lock_sha256 set) without any independent provider
        having recorded a runtime SHA, and without a boss-level waiver on record."""
        from ledger import INDEPENDENT_REVIEW_PROVIDERS
        waived={row.get("milestone") for row in load_jsonl(self.events)
                if row.get("stage")=="independence_waived"}
        violations=[]
        for r in self.rows():
            if not r.get("acceptance_lock_sha256"):continue
            if r.get("status") not in ("sha_accepted","retro_done"):continue
            if r["id"] in waived:continue
            if any(r.get(f"{p}_runtime_sha") for p in INDEPENDENT_REVIEW_PROVIDERS):continue
            violations.append(r["id"])
        if violations:
            print("INDEPENDENCE_VIOLATION: "+", ".join(violations))
            raise SystemExit(1)
        print("audit-independence: OK")

    def check_reviewer_isolation(self):
        """DEF-07 fix — verify that every independent provider used in this state dir has a
        non-empty isolation proof on record (WRITE_RESULT=BLOCKED in reports/).  Makes the GLM
        write-isolation gap an explicit, auditable check rather than a silent assumption."""
        from ledger import INDEPENDENT_REVIEW_PROVIDERS
        # Discover which independent providers have actually recorded a runtime SHA.
        used_providers=set()
        for r in self.rows():
            for p in INDEPENDENT_REVIEW_PROVIDERS:
                if r.get(f"{p}_runtime_sha"):
                    used_providers.add(p)
        if not used_providers:
            print("check-reviewer-isolation: OK (no independent provider used)")
            return
        # Provider → proof filename convention (mirrors existing smoke-test naming).
        proof_names={"z":"SMOKE_ZAI_WRITE_ATTEMPT.txt","codex":"SMOKE_CODEX_WRITE_ATTEMPT.txt"}
        reports_dir=self.repo/"reports"
        gaps=[]
        for prov in sorted(used_providers):
            name=proof_names.get(prov,f"SMOKE_{prov.upper()}_WRITE_ATTEMPT.txt")
            path=reports_dir/name
            if not path.exists() or path.stat().st_size==0:
                gaps.append(f"{prov} (proof missing or empty: {path})")
                continue
            content=path.read_text()
            if "WRITE_RESULT=BLOCKED" not in content:
                gaps.append(f"{prov} (proof found but WRITE_RESULT=BLOCKED absent: {path})")
        if gaps:
            print("ISOLATION_UNPROVEN: "+"; ".join(gaps))
            raise SystemExit(1)
        print("check-reviewer-isolation: OK")

    def waive_review(self,mid,actor,reason):
        """DEF-05 fix — record a boss-level waiver for the independence requirement on a
        milestone that pre-dates the gate.  Requires a named actor and a written reason."""
        actor=(actor or "").strip()
        if not actor:raise RuntimeError("waive-review: --actor required")
        check_actor_authorized(actor)
        reason=(reason or "").strip()
        if len(reason)<MIN_UNBLOCK_REASON_CHARS:
            raise RuntimeError(f"waive-review: --reason must be at least {MIN_UNBLOCK_REASON_CHARS} "
                                f"chars (got {len(reason)}) — the waiver stays in the ledger, it must be argued")
        self.event(mid,"independence_waived",actor,meta={"reason":reason})
        print(f"WAIVED {mid}: independence requirement waived by {actor}")
