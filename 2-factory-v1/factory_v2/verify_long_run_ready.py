"""LR5 verifier subprocess: re-verify the 3 LONG_RUN_READY evidence bundles from disk.

Runs as its own process (`python3 -m factory_v2.verify_long_run_ready ...`) so the
party that wants LONG_RUN_READY set can never compute the verdict itself.  Reads
only; writes nothing.  Exit 0 with a final JSON line {\"integration_sha\": ...} when
every check passes; exit 1 with typed findings otherwise.

Checks:
  A. LR5 receipt: schema, integration_sha a real commit in the repo, observation
     evidence paths present, and each referenced artifact parses and satisfies the
     structural expectations (same parser the hardened verify_preflight_receipt uses).
  B. Preflight checks result: status PASS, manifest_sha present.
  C. Controlled long run result: status PASS, fault injection actually crashed the
     daemon, restarted=True backed by >= 2 distinct daemon identities (pid AND birth)
     and at least one post-restart milestone.
  D. Cross-bundle: the milestones counted by the long run appear in the receipt's
     kill/restart observation timeline.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def _fail(findings: list[str]) -> int:
    for f in findings:
        print(f"FAIL: {f}")
    print(f"LONG_RUN_READY_VERIFIER: {len(findings)} finding(s)")
    return 1


def _load(path: Path) -> tuple[dict | None, str]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), ""
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def main() -> int:
    ap = argparse.ArgumentParser(prog="verify_long_run_ready")
    ap.add_argument("--project-dir", required=True)
    ap.add_argument("--repo-dir", required=True)
    ap.add_argument("--receipt", required=True)
    ap.add_argument("--preflight", required=True)
    ap.add_argument("--long-run", required=True)
    args = ap.parse_args()

    repo = Path(args.repo_dir)
    findings: list[str] = []

    # ── A. receipt ────────────────────────────────────────────────────────────
    receipt, err = _load(Path(args.receipt))
    if receipt is None:
        return _fail([f"RECEIPT_UNREADABLE: {err}"])
    if receipt.get("schema") != "lr5-preflight-receipt/v2":
        findings.append(f"RECEIPT_SCHEMA: {receipt.get('schema')!r} != "
                        "'lr5-preflight-receipt/v2'")
    integration_sha = receipt.get("integration_sha", "")
    if not integration_sha:
        findings.append("RECEIPT_INTEGRATION_SHA_EMPTY")
    else:
        rc = subprocess.run(["git", "cat-file", "-e", f"{integration_sha}^{{commit}}"],
                            cwd=str(repo), capture_output=True, timeout=60).returncode
        if rc != 0:
            findings.append(f"RECEIPT_INTEGRATION_SHA_NOT_A_COMMIT: {integration_sha[:12]}")

    obs = receipt.get("observations") or {}
    from factory_v2.verify_preflight_receipt import parse_kill_restart_evidence, parse_cas_evidence
    from factory_v2.verify_lib import VerificationRefused, sha256_matches_bytes
    parsed_kill_restart: dict | None = None
    for claim in ("kill_restart_recovery", "cas_double_dispatch"):
        ref = obs.get(claim) or {}
        path = ref.get("evidence_path", "")
        if not path or not (repo / path).is_file():
            findings.append(f"OBSERVATION_EVIDENCE_MISSING: {claim}: {path!r}")
            continue
        # L5_factory_v2_rest_3-003: the pinned digest is confronted to the BYTES
        # of the artifact — two all-zero (or otherwise wrong) digests used to
        # pass because only "does it parse" was checked.
        try:
            ok, note = sha256_matches_bytes(
                ref.get("evidence_sha256") or "",
                (repo / path).read_bytes(),
                what=f"OBSERVATION_EVIDENCE: {claim}")
            if not ok:
                findings.append(note)
        except VerificationRefused as refused:
            findings.append(f"OBSERVATION_EVIDENCE_UNBOUND: {refused}")
        try:
            if claim == "kill_restart_recovery":
                parsed_kill_restart = parse_kill_restart_evidence(repo / path)
            else:
                parse_cas_evidence(repo / path)
        except Exception as exc:
            findings.append(f"OBSERVATION_EVIDENCE_INVALID: {claim}: {exc}")

    # ── B. preflight checks result ────────────────────────────────────────────
    preflight, err = _load(Path(args.preflight))
    if preflight is None:
        findings.append(f"PREFLIGHT_UNREADABLE: {err}")
    else:
        if preflight.get("status") != "PASS":
            findings.append(f"PREFLIGHT_NOT_PASS: {preflight.get('status')!r}")
        if not preflight.get("manifest_sha"):
            findings.append("PREFLIGHT_MANIFEST_SHA_MISSING")

    # ── C. controlled long run result ─────────────────────────────────────────
    long_run, err = _load(Path(args.long_run))
    if long_run is None:
        findings.append(f"LONG_RUN_UNREADABLE: {err}")
    else:
        if long_run.get("status") != "PASS":
            findings.append(f"LONG_RUN_NOT_PASS: {long_run.get('status')!r}")
        if not long_run.get("crashed"):
            findings.append("LONG_RUN_NO_INJECTED_CRASH")
        if not long_run.get("restarted"):
            findings.append("LONG_RUN_NOT_RESTARTED")
        # L5_factory_v2_rest_3-004: validate the shape before comparing —
        # birth=null or a non-dict identity used to TypeError out of the
        # verifier instead of producing the typed finding.
        raw_ids = long_run.get("daemon_identities")
        ids = [i for i in raw_ids if isinstance(i, dict)] if isinstance(raw_ids, list) else []
        pids = [i.get("pid") for i in ids]
        births = [i.get("birth") for i in ids]
        distinct_pids = {p for p in pids if isinstance(p, int) and not isinstance(p, bool)}
        distinct_births = {b for b in births if isinstance(b, str) and b}
        if len(ids) < 2 or len(distinct_pids) < 2 or len(distinct_births) < 2:
            shown = [b[:24] if isinstance(b, str) else repr(b) for b in births]
            findings.append(
                f"RESTART_IDENTITY_NOT_DISTINCT: pids={pids} births={shown}")
        milestones = long_run.get("milestones_completed") or []
        if not milestones:
            findings.append("LONG_RUN_NO_MILESTONES")
        else:
            # ── D. Cross-bundle ────────────────────────────────────────────
            # L5_factory_v2_rest_3-002: the announced §D check now exists — the
            # milestones the long run counts must appear in the receipt's
            # kill/restart observation timeline (milestones_after). A chronology
            # M1→M1,M2,M3 cannot back a long run claiming unrelated milestones.
            if parsed_kill_restart is None:
                findings.append("CROSS_BUNDLE_NO_TIMELINE: the kill/restart "
                                "observation did not parse — §D cannot run")
            else:
                timeline = {str(m) for m in
                            (parsed_kill_restart.get("milestones_after") or [])}
                missing = [str(m) for m in milestones if str(m) not in timeline]
                if missing:
                    findings.append(
                        f"CROSS_BUNDLE_MILESTONES_NOT_IN_TIMELINE: {missing} "
                        f"absent from kill/restart milestones_after "
                        f"{sorted(timeline)}")

    if findings:
        return _fail(findings)

    print("LONG_RUN_READY_VERIFIER: ALL CHECKS PASS")
    print(json.dumps({"integration_sha": integration_sha}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
