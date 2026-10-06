"""Independent observation producer for FULL_CODEBASE_STATE_OF_ART_AUDIT.

`verify_audit_receipt.py` used to accept whatever booleans the party being
graded wrote, because no producer recomputed the underlying facts from the
checkout itself. This module is that producer: every fact it returns is
either read straight off git (the control SHA, whether a required artifact
is actually committed at that SHA, a line count recomputed from the blob
content) or pulled from a ledger the claimant does not own (the settlement
ledger, the launch_gate attempt ledger) — never from a JSON file the auditee
wrote about itself.

Nothing here ever returns PASS. It returns an OBSERVATION: a `status` of
SETTLED / PARTIAL / UNMET plus the raw facts that produced it. Whether that
observation satisfies the milestone is `verify_audit_receipt.py`'s job, and
it does that by recomputing this same observation and comparing hashes with
the receipt under review — a receipt that does not match a recomputation is
rejected regardless of what it claims.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AUDIT_DIR = REPO_ROOT / "factory_v2" / "audit"
MILESTONE = "FULL_CODEBASE_STATE_OF_ART_AUDIT"
RECOVERY_DIR = REPO_ROOT / "factory_v2" / "receipts" / "audit_recovery"

#: Artifacts a real audit is required to produce and bind to the exact
#: control SHA it measured. An artifact left over from an earlier audit run
#: (the STALE defect verify_audit_receipt.py's docstring names) still EXISTS
#: on disk but is not bound to the current HEAD, and must not settle it.
REQUIRED_ARTIFACTS = (
    "FACTORY_CONVERGENCE_LEDGER.json",
    "FACTORY_DEAD_DUPLICATE_CODE_REPORT.json",
    "FACTORY_F3_COUNTER_AUDIT_RECEIPT.json",
    "FACTORY_F3_EXTERNAL_RESEARCH_RECEIPT.json",
    "FACTORY_RUNTIME_AUTHORITY_MAP.json",
    "FACTORY_VS_UPSTREAM_MATRIX.json",
    "LICENSE_COMPATIBILITY_LEDGER.json",
    "UPSTREAM_SOURCE_MANIFEST.json",
)

#: Field names a producer may use to bind a JSON artifact to the control SHA
#: it was measured at. The FIRST one present wins.
SHA_FIELDS = ("control_sha", "control_code_sha", "produced_at_sha", "head_sha")

DEFAULT_PROJECT_DIR = REPO_ROOT / "projects" / "factory"


from ._io import _git as _git_io


def _git_safe(repo_root: Path, *args: str, timeout: int = 20):
    try:
        return _git_io(repo_root, *args, timeout=timeout)
    except Exception:
        return None


def current_head_sha(repo_root: Path = REPO_ROOT) -> str:
    """HEAD of `repo_root`, or "" when it cannot be observed. Never a guess."""
    r = _git_safe(repo_root, "rev-parse", "HEAD")
    if r is None or r.returncode != 0:
        return ""
    return r.stdout.strip()


def git_show_ok(repo_root: Path, sha: str, path: str) -> bool:
    """True iff `git show <sha>:<path>` resolves — the reference actually exists
    in the repo at that exact commit, not merely on disk in the working tree."""
    if not sha or not path:
        return False
    r = _git_safe(repo_root, "show", f"{sha}:{path}", timeout=15)
    return bool(r is not None and r.returncode == 0)


def _blob_text(repo_root: Path, sha: str, path: str) -> "str | None":
    r = _git_safe(repo_root, "show", f"{sha}:{path}", timeout=15)
    if r is None or r.returncode != 0:
        return None
    return r.stdout


def measured_python_loc(repo_root: Path, sha: str) -> "int | None":
    """Line count of every `.py` file tracked AT `sha`, recomputed from blob
    content — never read from a claimant-authored total. None when it cannot
    be observed.

    One `git grep -c` call over the whole tree, not one subprocess per file:
    a per-file `git show` loop over a real repo's several thousand `.py`
    files made this "cheap preflight" anything but — this stays a single git
    invocation regardless of repo size.
    """
    if not sha:
        return None
    r = _git_safe(repo_root, "grep", "-c", "", sha, "--", "*.py", timeout=60)
    if r is None:
        return None
    if r.returncode not in (0, 1):     # 1 == "no matching files", still a real answer
        return None
    total = 0
    for line in r.stdout.splitlines():
        _, _, count = line.rpartition(":")
        if count.strip().isdigit():
            total += int(count)
    return total


def _review_account_supervised(row: dict, project_dir: Path) -> bool:
    """True iff `row` (a review_accounts.jsonl line) corresponds to a REAL
    supervised launch recorded independently in the launch_gate attempt
    ledger (`evidence/attempt_accounts.jsonl`) — the same ledger
    `close_account()`/`_project_observed()` write for every model call,
    review or build alike, from OUTSIDE the process that ran it.

    CODEX-LOT-C37 counter-audit finding: `evidence/review_accounts.jsonl` is
    written by `review_dispatch._persist()`, which any process able to
    import `factory_v2.review_dispatch` — including, in principle, a mission
    subprocess with filesystem access to the factory project directory —
    could call directly to fabricate an ACCEPT line without ever actually
    launching a reviewer. `dispatch_review()`'s own real launches ALSO write
    a terminal attempt-ledger row for the same call (`open_account`/
    `close_account`, attempt_key `f"review:{milestone}:{sha[:16]}:..."`,
    `candidate_sha=reviewed_sha`, `engine_execution={"engine_id": engine,
    ...}`, `mission_pass=(verdict=="ACCEPT")`) — a row a forged
    review_accounts.jsonl line has no reason to also have, since producing
    it requires actually going through `launch_gate.open_account`/
    `close_account`. Cross-referencing the two closes that gap: a review
    account with no matching TERMINAL, PASSED attempt-ledger row for the
    SAME (milestone, engine, reviewed_sha) is not trusted as a real launch.
    """
    try:
        from .launch_gate import _accounts as _attempt_accounts
    except Exception:
        return False
    try:
        rows = _attempt_accounts(Path(project_dir))
    except Exception:
        return False
    milestone = row.get("milestone")
    engine = row.get("engine")
    sha = row.get("reviewed_sha")
    if not (milestone and engine and sha):
        return False
    for a in rows:
        if (a.get("milestone") == milestone
                and a.get("engine") == engine
                and a.get("candidate_sha") == sha
                and a.get("phase") == "TERMINAL"
                and bool(a.get("mission_pass")) is True
                and str(a.get("mission") or "").startswith("multi-ai:")):
            return True
    return False


def unproven_review_accounts(repo_root: Path, head: str,
                             project_dir: "Path | None" = None,
                             milestone: str = MILESTONE) -> list:
    """ACCEPT review rows for (`milestone`, `head`) that do NOT count towards
    `observed_reviewers()`, with a typed reason each — REVIEW_ACCOUNT_UNPROVEN
    (CODEX-LOT-C37 counter-audit): either the row carries no valid owner/
    supervisor seal (`review_dispatch.verify_seal()` — a builder process
    without the owner key cannot compute one), or it has no matching
    supervised launch in the attempt ledger (`_review_account_supervised()`).
    A row failing either check is IGNORED by `observed_reviewers()`, never
    silently trusted — this function exists so a caller can see WHY, instead
    of just a shorter reviewer list.
    """
    project_dir = Path(project_dir) if project_dir else DEFAULT_PROJECT_DIR
    if not project_dir.is_dir():
        return []
    try:
        from . import review_dispatch
        rows = review_dispatch.read_reviews(project_dir)
    except Exception:
        return []
    out = []
    for r in rows:
        if not (r.get("milestone") == milestone and r.get("reviewed_sha") == head
                and r.get("read_only") is True and r.get("verdict") == "ACCEPT"
                and r.get("engine")):
            continue
        if not review_dispatch.verify_seal(r):
            out.append({"engine": r.get("engine"), "reason": "REVIEW_ACCOUNT_UNPROVEN",
                       "detail": "UNSEALED_OR_INVALID_SEAL"})
            continue
        if not _review_account_supervised(r, project_dir):
            out.append({"engine": r.get("engine"), "reason": "REVIEW_ACCOUNT_UNPROVEN",
                       "detail": "NO_MATCHING_SUPERVISED_LAUNCH"})
    return out


def observed_reviewers(repo_root: Path, head: str, project_dir: "Path | None" = None,
                       milestone: str = MILESTONE) -> list:
    """Reviewer identities actually observed running, bound to the exact HEAD.

    R2 finding: this used to read `receipts/settlements/<milestone>.json` —
    but that file is written only AFTER this very milestone settles, so
    reading it here to decide whether it CAN settle is circular (it always
    reads either nothing or a stale settlement from a previous cycle). The
    supervisor-observed source that is NOT circular is the review ledger
    `review_dispatch.dispatch_review()` writes independently of settlement
    (`evidence/review_accounts.jsonl`): a real read-only launch, its rc, and
    its verdict, recorded before anyone decides whether the milestone is
    complete. Only ACCEPT verdicts from read-only launches reviewing this
    exact `head` count — never a receipt's self-reported reviewer field.

    CODEX-LOT-C37 counter-audit: a row also only counts when it is PROVEN —
    sealed by the owner/supervisor key (`review_dispatch.verify_seal()`) AND
    cross-referenced against a real supervised launch in the attempt ledger
    (`_review_account_supervised()`). See `unproven_review_accounts()` for
    the excluded rows and why.
    """
    project_dir = Path(project_dir) if project_dir else DEFAULT_PROJECT_DIR
    if not project_dir.is_dir():
        return []
    try:
        from . import review_dispatch
        rows = review_dispatch.read_reviews(project_dir)
    except Exception:
        return []
    reviewers = {
        r.get("engine") for r in rows
        if r.get("milestone") == milestone
        and r.get("reviewed_sha") == head
        and r.get("read_only") is True
        and r.get("verdict") == "ACCEPT"
        and r.get("engine")
        and review_dispatch.verify_seal(r)
        and _review_account_supervised(r, project_dir)
    }
    return sorted(reviewers)


def observed_retry_counts(project_dir: "Path | None", milestone: str = MILESTONE) -> dict:
    """Dispatch-attempt counts for `milestone`, recomputed from the attempt
    ledger — never taken from a receipt's self-reported `retries`/`attempts`."""
    if project_dir is None or not Path(project_dir).is_dir():
        return {"observed": False, "attempts": 0, "engines": []}
    try:
        from .launch_gate import _accounts
        rows = [a for a in _accounts(Path(project_dir)) if a.get("milestone") == milestone]
    except Exception:
        return {"observed": False, "attempts": 0, "engines": []}
    engines = sorted({a.get("engine") for a in rows if a.get("engine")})
    return {"observed": True, "attempts": len(rows), "engines": engines}


def _artifact_sha_field(data) -> str:
    if not isinstance(data, dict):
        return ""
    for f in SHA_FIELDS:
        v = data.get(f)
        if isinstance(v, str) and v:
            return v
    return ""


#: Paths a commit may touch and still leave an audit artifact FRESH: the audit's
#: own artifacts/receipt (a commit cannot name its own hash, so the artifacts
#: legitimately name the commit they were produced against, one step behind),
#: and pure bookkeeping/state paths — the same allowlist the Stop hook uses
#: (factory/hooks/stop_gate.py BOOKKEEPING_SAFE_PREFIXES): the hook commits
#: events.jsonl / candidate_registry.json / stop counters at EVERY session stop,
#: which would otherwise make every audit stale within minutes for reasons that
#: have nothing to do with the audited code. Any commit touching anything else
#: (factory_v2/*.py, factory/bin/*.py, tests/, ROADMAP, ...) breaks the chain.
_AUDIT_FRESHNESS_SAFE_PREFIXES = (
    # LOT C49 (FULL_CODEBASE_AUDIT_RECEIPT_CURRENT#M1, 2026-09-13 00:0xZ): the
    # mission's own bookkeeping — REPORT.json / PLAN.md checkpoints the V4
    # protocol REQUIRES the builder to commit, and the worktree meta the
    # dispatcher writes — is not code. Excluding it forced the builder to leave
    # its final REPORT.json uncommitted to keep the receipt fresh, which the
    # C40 gate then read as BUILDER_DECLARED_PARTIAL at the candidate SHA.
    "REPORT.json",
    "PLAN.md",
    ".factory_worktree_meta.json",
    # 2026-09-17: the worktree-level engine registry (FACTORY_ORPHAN_BY_SESSION)
    # and the factory's receipts/state ledgers land in every capture commit —
    # bookkeeping, not code. Without them every audit candidate went stale at
    # its own capture commit (8 builds lost to SPEC_TEST_CONFLICT).
    "evidence/",
    "factory_v2/receipts/",
    "factory/run/",
    ".portfolio_cursor.json",
    "agent_inbox/",
    "agent_outbox/",
    "factory_v2/audit/",
    "factory_v2/receipts/FULL_CODEBASE_AUDIT_RECEIPT.json",
    "factory/projects/",
    "factory/campaigns/",
    "factory/cto/reports/",
    "factory/cto/status/",
    # projects/<p>/{PROJECT_STATE.json,evidence/,validation_contracts/,...} are state;
    # the AUTHORITY documents under projects/ are NOT (Codex counter-audit 2026-09-16,
    # "l'allowlist accepte davantage que du bookkeeping") — see _AUDIT_FRESHNESS_NEVER_SAFE.
    "projects/",
    "reports/",
    "campaigns/",
    "memory/",
)
#: paths that end the freshness of an audit even though they sit under a safe prefix
_AUDIT_FRESHNESS_NEVER_SAFE = ("ROADMAP.yaml", "SPEC.md", "STRATEGY.md")
_AUDIT_FRESHNESS_MAX_DEPTH = 30


def _path_is_bookkeeping(ln: str) -> bool:
    if any(ln.endswith("/" + n) or ln == n for n in _AUDIT_FRESHNESS_NEVER_SAFE):
        return False
    return any(ln == pfx or ln.startswith(pfx) for pfx in _AUDIT_FRESHNESS_SAFE_PREFIXES)


def _fresh_against_head(repo_root: Path, sha: str, head: str) -> bool:
    """Structural freshness (Codex counter-audit 2026-09-16, BUG 1): an artifact
    produced at `sha` is fresh at `head` iff NOTHING outside bookkeeping changed
    between the two — judged on the diff itself, so the factory's own --no-ff
    merge of the audit candidate (which the old parent-walk refused to cross)
    and any depth of state commits no longer invalidate a current audit, while a
    single code/test/authority change still does, immediately."""
    if not sha or not head:
        return False
    if sha == head:
        return True
    anc = _git_safe(repo_root, "merge-base", "--is-ancestor", sha, head)
    if anc is None or anc.returncode != 0:
        return False
    d = _git_safe(repo_root, "diff", "--name-only", sha, head)
    if d is None or d.returncode != 0:
        return False
    changed = [ln for ln in d.stdout.splitlines() if ln.strip()]
    return all(_path_is_bookkeeping(ln) for ln in changed)


def _freshness_targets(repo_root: Path, head: str) -> set:
    """SHAs a `control_sha` field may legitimately name and still count as
    fresh: HEAD itself, and every ancestor reachable from HEAD through commits
    whose ONLY changes are audit artifacts / the audit receipt / bookkeeping
    state paths (`_AUDIT_FRESHNESS_SAFE_PREFIXES`), up to a bounded depth.

    A commit cannot contain a field equal to its own hash (the hash is
    computed FROM the content), so an artifact naming the exact SHA of the
    commit that adds it is structurally impossible; naming the commit it was
    produced against — with nothing but artifacts/receipt/state committed
    since — is the honest equivalent. The first commit touching anything else
    (code, tests, roadmap) stops the walk, so staleness against real change is
    still caught immediately. Merge commits (>1 parent) stop the walk too.
    """
    targets = {head} if head else set()
    if not head:
        return targets
    cur = head
    for _ in range(_AUDIT_FRESHNESS_MAX_DEPTH):
        r = _git_safe(repo_root, "rev-list", "--parents", "-n", "1", cur)
        if r is None or r.returncode != 0:
            break
        parts = r.stdout.split()
        if len(parts) != 2:          # root commit or merge: no unambiguous single parent
            break
        parent = parts[1]
        d = _git_safe(repo_root, "diff", "--name-only", parent, cur)
        if d is None or d.returncode != 0:
            break
        changed = [ln for ln in d.stdout.splitlines() if ln.strip()]
        if not changed or not all(any(ln == pfx or ln.startswith(pfx)
                                      for pfx in _AUDIT_FRESHNESS_SAFE_PREFIXES)
                                  for ln in changed):
            break
        targets.add(parent)
        cur = parent
    return targets


def observe_artifacts(repo_root: Path, head: str) -> dict:
    fresh = _freshness_targets(repo_root, head)
    out = {}
    for name in REQUIRED_ARTIFACTS:
        rel = f"factory_v2/audit/{name}"
        p = repo_root / rel
        exists_on_disk = p.is_file()
        # "committed at HEAD" is the reference-resolution check the milestone
        # asks for: git show <sha>:<path> must succeed, not merely "a file with
        # this name is present in the working tree".
        committed_at_head = git_show_ok(repo_root, head, rel) if head else False
        bound_sha = ""
        if exists_on_disk and p.suffix == ".json":
            try:
                bound_sha = _artifact_sha_field(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                bound_sha = ""
        out[name] = {
            "path": rel,
            "exists_on_disk": exists_on_disk,
            "committed_at_head": committed_at_head,
            "control_sha_field": bound_sha,
            "bound_to_head": bool(bound_sha) and (bound_sha in fresh
                                                  or _fresh_against_head(repo_root, bound_sha, head)),
        }
    return out


#: Acceptance floor (ROADMAP goal for FULL_CODEBASE_STATE_OF_ART_AUDIT / CTO
#: r2 review): "one GLM review is not enough" — SETTLED requires this many
#: DISTINCT engines to have actually ACCEPTed this exact HEAD, read-only.
MIN_REVIEWERS = 3

#: >= 8 externally-cited sources, each with a real repo + commit_sha — not a
#: bare count, every citation must be independently resolvable in principle.
MIN_REFERENCES = 8

_ALLOWED_DECISIONS = {"KEEP", "REUSE", "ADAPT", "WRAP", "REPLACE", "DEMOTE",
                      "DELETE", "REJECT_EXTERNAL"}


def _artifact_json_at_head(repo_root: Path, head: str, name: str):
    """Parse a required artifact from the exact HEAD blob — never the working
    tree, which can move ahead of (or diverge from) what was actually
    committed. None when the blob is absent or not valid JSON."""
    text = _blob_text(repo_root, head, f"factory_v2/audit/{name}")
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def observed_references(repo_root: Path, head: str) -> dict:
    """Distinct external citations from UPSTREAM_SOURCE_MANIFEST.json, each
    required to name a real repo + commit_sha — a bare count is not a
    citation."""
    data = _artifact_json_at_head(repo_root, head, "UPSTREAM_SOURCE_MANIFEST.json")
    upstreams = (data or {}).get("upstreams") if isinstance(data, dict) else None
    if not isinstance(upstreams, list):
        return {"count": 0, "valid": False}
    valid = [u for u in upstreams
            if isinstance(u, dict) and u.get("repo") and u.get("commit_sha")]
    return {"count": len(valid), "valid": len(valid) >= MIN_REFERENCES}


def decisions_justified(repo_root: Path, head: str) -> dict:
    """Every FACTORY_CONVERGENCE_LEDGER.json decision names a DECISION in the
    contract's enum, a rationale, and a `file` that actually resolves at this
    exact HEAD (`git show <head>:<file>` succeeds) — never a claimant's own
    'reviewed=true' boolean."""
    data = _artifact_json_at_head(repo_root, head, "FACTORY_CONVERGENCE_LEDGER.json")
    decisions = (data or {}).get("decisions") if isinstance(data, dict) else None
    if not isinstance(decisions, list) or not decisions:
        return {"count": 0, "all_justified": False, "unjustified": ["<no decisions>"]}
    unjustified = []
    for d in decisions:
        if not isinstance(d, dict):
            unjustified.append("<non-dict decision entry>")
            continue
        name = d.get("subsystem") or d.get("file") or "<unnamed>"
        decision = d.get("decision")
        rationale = d.get("current_solution") or d.get("advantages") or d.get("rationale")
        file_ref = d.get("file") or ""
        file_ok = bool(file_ref) and git_show_ok(repo_root, head, file_ref)
        if decision not in _ALLOWED_DECISIONS or not rationale or not file_ok:
            unjustified.append(name)
    return {"count": len(decisions), "all_justified": not unjustified,
            "unjustified": unjustified}


def subsystems_covered(repo_root: Path, head: str) -> dict:
    """Every FACTORY_VS_UPSTREAM_MATRIX.json subsystem cites factory files
    that actually resolve at this exact HEAD."""
    data = _artifact_json_at_head(repo_root, head, "FACTORY_VS_UPSTREAM_MATRIX.json")
    matrix = (data or {}).get("matrix") if isinstance(data, dict) else None
    if not isinstance(matrix, list) or not matrix:
        return {"count": 0, "all_resolve": False, "unresolved": ["<no matrix>"]}
    unresolved = []
    for entry in matrix:
        if not isinstance(entry, dict):
            unresolved.append("<non-dict matrix entry>")
            continue
        name = entry.get("subsystem", "<unnamed>")
        files = ((entry.get("factory") or {}).get("files")) or []
        if not files or not all(isinstance(f, str) and git_show_ok(repo_root, head, f)
                                for f in files):
            unresolved.append(name)
    return {"count": len(matrix), "all_resolve": not unresolved, "unresolved": unresolved}


def def001_fixed(project_dir: "Path | None") -> bool:
    """DEF-001 (PREMATURE_IDLE_WITH_OPEN_ROADMAP) must be recorded FIXED in
    the supervisor's own PROJECT_STATE.json — never assumed."""
    if project_dir is None or not Path(project_dir).is_dir():
        return False
    p = Path(project_dir) / "PROJECT_STATE.json"
    try:
        state = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    for d in state.get("canonical_defects", []) or []:
        if isinstance(d, dict) and d.get("id") == "DEF-001":
            return str(d.get("status", "")).upper().startswith("FIXED")
    return False


#: The exact reviewer roster the milestone requires — not merely "any 3
#: distinct engines". CODEX-LOT-C37 counter-audit: the closed universe of
#: canonical engines is exactly {codex, claude, glm} (plus "local", which has
#: no verified review launch) — "any 3 distinct" over that universe used to
#: also accept e.g. {"claude", "glm", "local"}, never actually exercising
#: Codex. MIN_REVIEWERS stays 3 (documented, unchanged) — this just names
#: WHICH three.
REQUIRED_REVIEWER_ENGINES: frozenset = frozenset({"codex", "claude", "glm"})


def build_observation(repo_root: "Path | None" = None,
                      project_dir: "Path | None" = None,
                      head: "str | None" = None,
                      milestone: "str | None" = None,
                      required_reviewer_engines: "frozenset | set | None" = None
                      ) -> dict:
    """Recompute the whole audit observation for `milestone`, now.

    Deterministic in everything except which SHA is currently checked out:
    calling this twice at the same HEAD with the same ledger state produces
    byte-identical output (see `observation_hash`), so a stale or fabricated
    receipt cannot coincidentally match it.

    R2 finding: the previous version declared SETTLED from `all_bound and
    reviewers` alone — any single reviewer, any receipt shape, passed. The
    milestone's own acceptance requires >=3 reviewers, >=8 references, every
    decision justified, every subsystem's citations resolving, the measured
    counters actually observed, and DEF-001 fixed. Every clause below is
    machine-recomputed here, not read from anyone's receipt.

    `milestone` (CODEX-LOT-C37): defaults to `MILESTONE`
    (FULL_CODEBASE_STATE_OF_ART_AUDIT) — every existing call site is
    unaffected. A caller auditing a DIFFERENT milestone's multi-AI review
    requirement may pass its id here; `reviewers_observed`/
    `unproven_reviewers`/`retry_counts_observed` all key off it. The
    artifact/reference/decision/subsystem/DEF-001 clauses below are NOT
    reparametrized — they measure the specific
    `factory_v2/audit/*.json` deliverables this ONE milestone's roadmap
    entry defines, which a genuinely different milestone would need its own
    schema for; only the reviewer-provenance machinery generalizes here.

    `required_reviewer_engines`: defaults to `REQUIRED_REVIEWER_ENGINES`
    ({"codex", "claude", "glm"}) — the EXACT set that must each have a
    proven ACCEPT, not merely `len(reviewers) >= MIN_REVIEWERS`.
    """
    repo_root = Path(repo_root) if repo_root else REPO_ROOT
    project_dir = Path(project_dir) if project_dir else (
        DEFAULT_PROJECT_DIR if DEFAULT_PROJECT_DIR.is_dir() else None)
    milestone = milestone or MILESTONE
    required_reviewer_engines = (frozenset(required_reviewer_engines)
                                 if required_reviewer_engines is not None
                                 else REQUIRED_REVIEWER_ENGINES)
    # `head` override: a verifier may recompute the observation at the SHA a
    # receipt was produced against, provided that SHA is in
    # `_freshness_targets(repo_root, current HEAD)` (only audit/receipt/
    # bookkeeping commits since). Default: the checked-out HEAD.
    head = head or current_head_sha(repo_root)
    artifacts = observe_artifacts(repo_root, head)
    all_bound = bool(head) and all(a["bound_to_head"] and a["committed_at_head"]
                                   for a in artifacts.values())
    reviewers = observed_reviewers(repo_root, head, project_dir, milestone=milestone)
    unproven_reviewers = unproven_review_accounts(repo_root, head, project_dir,
                                                  milestone=milestone)
    retry_counts = observed_retry_counts(project_dir, milestone=milestone)
    loc = measured_python_loc(repo_root, head) if head else None
    references = observed_references(repo_root, head) if head else {"count": 0, "valid": False}
    decisions = (decisions_justified(repo_root, head) if head
                else {"count": 0, "all_justified": False, "unjustified": []})
    subsystems = (subsystems_covered(repo_root, head) if head
                 else {"count": 0, "all_resolve": False, "unresolved": []})
    def001 = def001_fixed(project_dir)

    missing = sorted(name for name, a in artifacts.items()
                     if not (a["bound_to_head"] and a["committed_at_head"]))

    settled_checks = {
        "artifacts_bound_to_head": all_bound,
        "reviewers_min_3_distinct_accept": (
            len(reviewers) >= MIN_REVIEWERS
            and required_reviewer_engines <= set(reviewers)),
        "references_min_8_valid": bool(references.get("valid")),
        "decisions_all_justified": bool(decisions.get("all_justified")),
        "subsystems_all_resolve": bool(subsystems.get("all_resolve")),
        "measured_loc_observed": loc is not None,
        "retry_counts_observed": bool(retry_counts.get("observed")),
        "def001_fixed": def001,
    }
    unmet_clauses = sorted(k for k, v in settled_checks.items() if not v)

    if not head:
        status = "UNMET"
    elif not unmet_clauses:
        status = "SETTLED"
    elif any(a["exists_on_disk"] for a in artifacts.values()):
        status = "PARTIAL"
    else:
        status = "UNMET"

    return {
        "schema": "audit-observation/v1",
        "milestone": milestone,
        "control_sha": head,
        "artifacts": artifacts,
        "missing_or_stale_artifacts": missing,
        "all_artifacts_bound_to_head": all_bound,
        "measured_python_loc": loc,
        "reviewers_observed": reviewers,
        "reviewers_required": sorted(required_reviewer_engines),
        "unproven_reviewers": unproven_reviewers,
        "retry_counts_observed": retry_counts,
        "references_observed": references,
        "decisions_observed": decisions,
        "subsystems_observed": subsystems,
        "def001_fixed": def001,
        "settled_checks": settled_checks,
        "unmet_clauses": unmet_clauses,
        "status": status,
    }


def observation_hash(observation: dict) -> str:
    """Content digest of a recomputed observation. A receipt is accepted only
    when it names this exact digest — recomputed independently, not copied
    from the receipt it is meant to check."""
    payload = json.dumps(observation, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def dedup_key(observation: dict) -> str:
    """Identity of the blocking condition: same SHA, same missing artifacts,
    same milestone → same key. Used to make repeated UNMET observations
    produce at most one durable recovery record instead of one per poll."""
    payload = json.dumps({
        "milestone": observation.get("milestone", MILESTONE),
        "control_sha": observation.get("control_sha", ""),
        "missing_or_stale_artifacts": observation.get("missing_or_stale_artifacts", []),
    }, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def write_recovery_request(observation: dict, *, recovery_dir: "Path | None" = None) -> Path:
    """Durable, typed, deduplicated record of exactly what is missing.

    Idempotent: two calls for the same (milestone, control_sha,
    missing_or_stale_artifacts) write the SAME file, not a new one — the
    silent per-poll retry loop this replaces is precisely one new attempt
    (and, formerly, one new manifest write) per 30s tick with nothing new to
    report.
    """
    recovery_dir = Path(recovery_dir) if recovery_dir else RECOVERY_DIR
    recovery_dir.mkdir(parents=True, exist_ok=True)
    key = dedup_key(observation)
    path = recovery_dir / f"{observation.get('milestone', MILESTONE)}_{key}.json"
    record = {
        "schema": "audit-recovery-work/v1",
        "kind": "DIAGNOSE",
        "milestone": observation.get("milestone", MILESTONE),
        "dedup_key": key,
        "control_sha": observation.get("control_sha", ""),
        "missing_or_stale_artifacts": observation.get("missing_or_stale_artifacts", []),
        "producer_task": ("factory/campaigns/factory-vnext/tasks/"
                          "FULL_CODEBASE_STATE_OF_ART_AUDIT.md"),
        "status": observation.get("status", "UNMET"),
    }
    if not path.is_file():
        path.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    return path
