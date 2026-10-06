from __future__ import annotations
import hashlib, json, os, pathlib, shutil, signal, subprocess, sys, tempfile, time
import yaml
from ledger import (file_sha256, utc,
                    RUNTIME_RECEIPT_SCHEMA, NETWORK_INFRA_REASON_CODE,
                    INDEPENDENT_REVIEW_PROVIDERS, ORACLE_TIMEOUT_SEC)


def validate_runtime_receipt(receipt,expected_candidate_sha,expected_lock_sha256=None,
                              expected_oracle_sha256=None,expected_mission_id=None,
                              expected_toolchain_lock_sha256=None,
                              expected_network_policy_mode=None,
                              expected_network_policy_sha256=None):
    """INVALID_REVIEW unless the receipt is real, pinned to this exact candidate/oracle/lock/
    mission, and reports a clean run. A bare {"verdict": ...} JSON with no receipt is never
    sufficient — the caller must not have anything to pass here in that case, which itself
    raises."""
    problems=[]
    if not isinstance(receipt,dict):
        return ["INVALID_REVIEW: no runtime-receipt provided"]
    if receipt.get("schema")!=RUNTIME_RECEIPT_SCHEMA:
        problems.append(f"bad receipt schema {receipt.get('schema')!r}")
    if receipt.get("candidate_sha")!=expected_candidate_sha:
        problems.append(f"receipt candidate_sha {receipt.get('candidate_sha')!r} != {expected_candidate_sha!r}")
    if expected_lock_sha256 and receipt.get("acceptance_lock_sha256")!=expected_lock_sha256:
        problems.append("receipt acceptance_lock_sha256 does not match the locked contract")
    if expected_oracle_sha256 and receipt.get("oracle_sha256")!=expected_oracle_sha256:
        problems.append("receipt oracle_sha256 does not match the committed oracle script")
    # Fault-injection F004 fix: candidate_sha/lock/oracle hashes can all genuinely coincide
    # across two DIFFERENT missions — require mission_id whenever the caller supplies one.
    if expected_mission_id and receipt.get("mission_id")!=expected_mission_id:
        problems.append(f"receipt mission_id {receipt.get('mission_id')!r} != {expected_mission_id!r}")
    # Fault-injection F015 fix: a receipt's declared toolchain_lock_sha256 must match what is
    # actually committed at candidate_sha whenever the milestone's worktree has a toolchain-lock.json.
    if expected_toolchain_lock_sha256 and receipt.get("toolchain_lock_sha256")!=expected_toolchain_lock_sha256:
        problems.append(f"receipt toolchain_lock_sha256 {receipt.get('toolchain_lock_sha256')!r} != "
                         f"{expected_toolchain_lock_sha256!r} (toolchain différente)")
    # Fault-injection F022 fix: network_policy_sha256 must match the milestone's pinned policy.
    if expected_network_policy_sha256 and receipt.get("network_policy_sha256")!=expected_network_policy_sha256:
        problems.append(f"receipt network_policy_sha256 {receipt.get('network_policy_sha256')!r} != "
                         f"{expected_network_policy_sha256!r} (policy réseau modifiée)")
    # Fault-injection F018/F019 fix: LIVE_NETWORK_UNAVAILABLE exemption only for allowlist mode.
    network_reason_claimed=receipt.get("reason_code")==NETWORK_INFRA_REASON_CODE
    if network_reason_claimed and expected_network_policy_mode not in (None,"allowlist"):
        problems.append(f"receipt reason_code={NETWORK_INFRA_REASON_CODE!r} is not a legitimate "
                         f"exemption under network_policy mode={expected_network_policy_mode!r} "
                         "(no real external network dependency — réponse réseau partielle)")
        network_reason_claimed=False
    if receipt.get("exit_code")!=0 and not network_reason_claimed:
        problems.append(f"receipt exit_code={receipt.get('exit_code')!r} != 0")
    for field,expected in (("downloads_passed",4),("cv_personalization_pass",True),
                           ("letter_personalization_pass",True),("cross_fit_pass",True),
                           ("truth_gate_pass",True),("master_hashes_unchanged",True)):
        if field in receipt and receipt[field]!=expected:
            problems.append(f"receipt {field}={receipt[field]!r} != {expected!r}")
    if not receipt.get("oracle_sha256"):
        problems.append("receipt missing oracle_sha256 (anti-spoofing field)")
    if expected_mission_id and not receipt.get("mission_id"):
        problems.append("receipt missing mission_id (anti-spoofing field)")
    return problems


class ReceiptMixin:
    def _require_runtime_receipt(self,mid,r,data):
        """JR-LIVE-RECOVERY Phase 9.3/9.4 — a milestone that opted into the runtime-oracle
        contract cannot reach qa_accepted/sha_accepted on a bare declarative verdict.

        Fault-injection F001 fix: independently recomputes the real oracle hash from the
        milestone's own worktree at record-time and pins the receipt to it."""
        lock_sha=r.get("acceptance_lock_sha256")
        if not lock_sha:
            return
        receipt=data.get("runtime_receipt")
        if receipt is None and data.get("runtime_receipt_path"):
            receipt=json.loads(pathlib.Path(data["runtime_receipt_path"]).read_text())
        wt=pathlib.Path(r["worktree"]) if r.get("worktree") else None
        oracle_path=wt/"scripts"/"verify_live_product_dod.py" if wt else None
        if not oracle_path or not oracle_path.exists():
            raise RuntimeError(f"INVALID_REVIEW ({mid}): cannot verify runtime receipt — "
                                f"milestone worktree/oracle missing at {oracle_path}")
        expected_oracle_sha256=file_sha256(oracle_path)
        # F015 fix: recompute fresh from the worktree.
        toolchain_path=wt/"toolchain-lock.json"
        expected_toolchain_lock_sha256=file_sha256(toolchain_path) if toolchain_path.exists() else None
        # F019 fix: read the milestone's own committed network_policy mode fresh from its worktree.
        acceptance_yaml_path=wt/"specs"/mid/"acceptance.yaml"
        expected_network_policy_mode=None
        if acceptance_yaml_path.exists():
            acceptance_spec=yaml.safe_load(acceptance_yaml_path.read_text()) or {}
            expected_network_policy_mode=acceptance_spec.get("network_policy",{}).get("mode")
        # F022 fix: keyed off the value pinned onto the roadmap row at spec_ready time,
        # not a fresh re-read — a tampered acceptance.yaml must be judged against the LOCKED contract.
        expected_network_policy_sha256=r.get("network_policy_sha256")
        problems=validate_runtime_receipt(receipt,r.get("candidate_sha"),lock_sha,expected_oracle_sha256,mid,
                                           expected_toolchain_lock_sha256,expected_network_policy_mode,
                                           expected_network_policy_sha256)
        if problems:
            raise RuntimeError(f"INVALID_REVIEW ({mid}): "+"; ".join(problems))
        # Fault-injection F018 fix: route LIVE_NETWORK_UNAVAILABLE through infra_failure().
        if isinstance(receipt,dict) and receipt.get("exit_code")!=0 and receipt.get("reason_code")==NETWORK_INFRA_REASON_CODE:
            self.infra_failure(mid,"live network unavailable during runtime-oracle verification "
                                f"(reason_code={NETWORK_INFRA_REASON_CODE}) — retriable per order "
                                "§2 network_policy, not a product failure",
                                provider=None,error_class="LIVE_NETWORK_UNAVAILABLE")
            raise RuntimeError(f"WAITING_INFRA ({mid}): live network unavailable during "
                                "runtime-oracle verification — retriable infra condition (order §2 "
                                "network_policy), not PRODUCT_FAIL")

    def _require_runtime_sha_agreement(self,mid,r):
        """JR-LIVE-RECOVERY Phase 9.5 — all recorded runtime SHAs must equal candidate_sha.

        Fault-injection F009 fix: also fails closed if the milestone opted into the runtime-oracle
        contract but no canonical provider has recorded a matching runtime SHA at all.
        Fault-injection F016 fix: builder self-verification alone does not satisfy order rule 2."""
        from ledger import RUNTIME_PROVIDERS
        candidate=r.get("candidate_sha")
        recorded={p:r.get(f"{p}_runtime_sha") for p in RUNTIME_PROVIDERS if r.get(f"{p}_runtime_sha")}
        mismatched={p:sha for p,sha in recorded.items() if sha!=candidate}
        if mismatched:
            raise RuntimeError(f"INVALID_REVIEW ({mid}): runtime SHA disagreement vs candidate "
                                f"{candidate}: {mismatched}")
        if r.get("acceptance_lock_sha256") and not recorded:
            raise RuntimeError(f"INVALID_REVIEW ({mid}): no provider has recorded an "
                                "independently-verified runtime SHA for this candidate "
                                "(verify-runtime never run)")
        independent_recorded={p:sha for p,sha in recorded.items() if p in INDEPENDENT_REVIEW_PROVIDERS}
        if r.get("acceptance_lock_sha256") and not independent_recorded:
            self.infra_failure(mid,"reviewer indisponible: no independent provider (z/codex) has "
                                "recorded a runtime-verified SHA for this candidate — builder "
                                "self-verification does not satisfy order rule 2",
                                provider="builder" if recorded else None,
                                error_class="NO_INDEPENDENT_REVIEWER")
            raise RuntimeError(f"WAITING_INFRA ({mid}): reviewer indisponible — no independent "
                                "provider (z/codex) has verified this candidate; builder alone is "
                                "not a valid independent reviewer (order rule 2)")

    def verify_runtime(self,mid,provider,candidate_sha):
        """JR-LIVE-RECOVERY Phase 9.2 + order §9 (clean-room) — runs FROM the factory process,
        in a disposable worktree pinned to the exact candidate SHA, with HOME/TMPDIR/
        XDG_CACHE_HOME isolated. workspace_tree_before must equal workspace_tree_after.

        F008 fix: uses --ignored so any tree mutation (tracked or not) is caught.
        F017 fix: start_new_session=True puts oracle + spawned processes in one process group,
        torn down unconditionally; ORACLE_TIMEOUT_SEC bounds a hung oracle."""
        r=self.get(mid)
        # D-36 fix: run_root must be outside any git-tracked tree.
        run_root=pathlib.Path(tempfile.mkdtemp(prefix=f"factory-runtime-verify-{mid}-{provider}-"))
        scratch=run_root/"worktree";home=run_root/"home";tmp=run_root/"tmp"
        cache=run_root/"cache";out_root=run_root/"output"
        for d in (home,tmp,cache,out_root):d.mkdir(parents=True,exist_ok=True)
        cp=self.command(["git","worktree","add","--detach",str(scratch),candidate_sha],cwd=self.repo,check=False)
        if cp.returncode:raise RuntimeError("verify-runtime: disposable worktree creation failed\n"+cp.stdout+cp.stderr)
        try:
            head=self.git_sha(scratch)
            if head!=candidate_sha:
                raise RuntimeError(f"verify-runtime: worktree HEAD {head} != requested candidate {candidate_sha}")
            tree_before=self.command(["git","status","--porcelain","--ignored"],cwd=scratch).stdout.strip()
            if tree_before:raise RuntimeError("verify-runtime: disposable worktree is not clean\n"+tree_before)
            oracle_path=scratch/"scripts"/"verify_live_product_dod.py"
            if not oracle_path.exists():raise RuntimeError(f"verify-runtime: {oracle_path} missing at this SHA")
            oracle_sha256=file_sha256(oracle_path)
            out_path=out_root/provider/"runtime-receipt.json"
            clean_env={"PATH":os.environ.get("PATH",""),"HOME":str(home),
                       "TMPDIR":str(tmp),"XDG_CACHE_HOME":str(cache),
                       "PYTHONDONTWRITEBYTECODE":"1","PYTEST_ADDOPTS":"-p no:cacheprovider"}
            # USER is passed through: the oracle's test suite exercises dispatcher.clean_env()
            # which must forward USER so `claude -p` can authenticate (test_f1_p7 proves this).
            if os.environ.get("USER"):
                clean_env["USER"]=os.environ["USER"]
            proc=subprocess.Popen([sys.executable,"-u",str(oracle_path),"--emit-json",str(out_path)],
                                   cwd=scratch,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                                   env=clean_env,start_new_session=True)
            pgid=proc.pid
            try:
                oracle_stdout,oracle_stderr=proc.communicate(timeout=ORACLE_TIMEOUT_SEC)
                timed_out=False
            except subprocess.TimeoutExpired:
                timed_out=True
                oracle_stdout=oracle_stderr=""
            finally:
                for sig in (signal.SIGTERM,signal.SIGKILL):
                    try:
                        os.killpg(pgid,sig)
                    except ProcessLookupError:
                        break
                    time.sleep(0.3)
                try:
                    proc.wait(timeout=5)
                except (subprocess.TimeoutExpired,ValueError):
                    pass
            if timed_out:
                self.infra_failure(mid,f"oracle runtime-verification timed out after "
                                    f"{ORACLE_TIMEOUT_SEC}s (hung process, or a port held by a "
                                    "leaked prior attempt) — retriable",
                                    provider=provider,error_class="ORACLE_TIMEOUT")
                raise RuntimeError(f"WAITING_INFRA ({mid}): verify-runtime oracle timed out after "
                                    f"{ORACLE_TIMEOUT_SEC}s — retriable infra condition, not a code failure")
            oracle_returncode=proc.returncode
            tree_after=self.command(["git","status","--porcelain","--ignored"],cwd=scratch).stdout.strip()
            (self.evidence/mid).mkdir(parents=True,exist_ok=True)
            (self.evidence/mid/f"runtime-receipt.{provider}.stdout.log").write_text(oracle_stdout+oracle_stderr)
            if tree_after!=tree_before:
                raise RuntimeError("REVIEWER_WRITE_VIOLATION: disposable worktree mutated during "
                                    f"verify-runtime (workspace_tree_before != workspace_tree_after)\n{tree_after}")
            if not out_path.exists():
                raise RuntimeError(f"verify-runtime: oracle produced no receipt (exit={oracle_returncode})\n"
                                    +oracle_stdout[-4000:]+oracle_stderr[-4000:])
            receipt=json.loads(out_path.read_text())
            if receipt.get("oracle_sha256")!=oracle_sha256:
                raise RuntimeError("verify-runtime: receipt oracle_sha256 does not match the committed oracle "
                                   "at this SHA — INVALID_REVIEW")
            # L6_bin_lib_products_1-017: the receipt's exit_code is CONFRONTED to
            # the oracle process's real returncode — an oracle writing
            # exit_code=0 into its receipt then exiting rc=7 used to be stamped
            # z_runtime_sha and recorded as runtime_verified.
            if receipt.get("exit_code")!=oracle_returncode:
                raise RuntimeError(f"verify-runtime: receipt exit_code "
                                   f"{receipt.get('exit_code')!r} != oracle process "
                                   f"returncode {oracle_returncode!r} — the receipt "
                                   "does not describe the run that happened "
                                   "(INVALID_REVIEW)")
            # F004 fix: stamp which mission this receipt was produced for.
            receipt["mission_id"]=mid
            # F015 fix: stamp the toolchain actually present in the disposable worktree.
            toolchain_path=scratch/"toolchain-lock.json"
            if toolchain_path.exists():
                receipt["toolchain_lock_sha256"]=file_sha256(toolchain_path)
            # FIX3 (RUN8 P5): stamp network_policy_sha256 unconditionally — factoryctl either
            # computes the value itself or the receipt carries none.
            acceptance_yaml_path=scratch/"specs"/mid/"acceptance.yaml"
            policy=None
            if acceptance_yaml_path.exists():
                policy=(yaml.safe_load(acceptance_yaml_path.read_text()) or {}).get("network_policy") or {}
            if policy:
                receipt["network_policy_sha256"]=hashlib.sha256(
                    json.dumps(policy,sort_keys=True).encode()).hexdigest()
            else:
                receipt.pop("network_policy_sha256",None)
            pinned=self.evidence/mid/f"runtime-receipt.{provider}.json"
            pinned.parent.mkdir(parents=True,exist_ok=True)
            pinned.write_text(json.dumps({**receipt,"pinned_by":"factoryctl","provider":provider},indent=2))
            self.set(mid,**{f"{provider}_runtime_sha":receipt.get("candidate_sha")})
            # F006 fix: pin the pinned receipt file itself into the tamper-evident event chain.
            self.event(mid,f"runtime_verified_{provider}","factoryctl",str(pinned),receipt.get("candidate_sha"),
                       meta={"exit_code":receipt.get("exit_code"),"provider":provider,
                             "artifact_sha256":file_sha256(pinned)})
            print(json.dumps(receipt,indent=2))
            return receipt
        finally:
            self.command(["git","worktree","remove","--force",str(scratch)],cwd=self.repo,check=False)
            shutil.rmtree(run_root,ignore_errors=True)
