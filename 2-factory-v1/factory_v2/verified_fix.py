"""Supervisor-produced proof that a fix closes an incident. P0-1.

The version this replaces read JSON out of the PRODUCT project. A worker could
write it; `is_valid()` accepted any non-empty `independent_verdict` — including
"REJECT" — ignored the schema field entirely, and the runtime matched fingerprints
by substring. Measured at 2900f4c8:

    FORGED_CLAIMANT_RECEIPT_ACCEPTED=1   REJECT_VERDICT_ACCEPTED=1
    WRONG_SCHEMA_ACCEPTED=1              SUBSTRING_FINGERPRINT_ACCEPTED=1

That is the shape-check doctrine failing in the one place it matters most: a
receipt that reads a claim out of the claimant's artifact. The claimant here is the
thing asking to be let out of quarantine.

So the supervisor now RUNS the reproduction itself, at both SHAs, in detached
worktrees, and records the exit codes and output hashes it observed. Receipts live
in the control repo under a supervisor-owned directory and are referenced from
state by exact id plus content hash, so a product worker can neither write one nor
choose which one applies.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from factory_v2._io import _now

SCHEMA = "verified-fix-receipt/v2"
_REPO_ROOT = Path(__file__).resolve().parent.parent

#: Supervisor-owned. Deliberately NOT under projects/<name>/evidence: anything a
#: product worker can write, a product worker can forge.
STORE = _REPO_ROOT / "factory_v2" / "receipts" / "verified_fixes"

PASS_VERDICT = "PASS"


class ReceiptRefused(Exception):
    """A receipt was rejected. Carries the machine-readable reason."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


@dataclass
class VerifiedFixReceipt:
    schema: str
    old_fingerprint: str
    old_code_sha: str
    fix_code_sha: str
    reproduction_command: str
    contract_signature: str
    context_manifest_sha: str
    old_rc: int
    new_rc: int
    old_output_sha256: str
    new_output_sha256: str
    verdict: str
    produced_by: str
    #: "control-relative" (cwd mapped into each detached worktree) or
    #: "external-product" (separate immutable product checkouts per observation).
    execution_root_kind: str = ""
    #: HEAD of the external product repo the reproduction ran against, or "".
    product_code_sha: str = ""
    observed_at: str = field(
        default_factory=lambda: _now())

    def as_dict(self) -> dict:
        return asdict(self)

    def content_hash(self) -> str:
        return hashlib.sha256(
            json.dumps(self.as_dict(), sort_keys=True).encode()).hexdigest()

    def receipt_id(self) -> str:
        return self.content_hash()[:24]

    def invalid_reason(self) -> str:
        """Empty string when the receipt is a real proof; a typed reason otherwise.

        Every clause is a forgery that was accepted at 2900f4c8.
        """
        if self.schema != SCHEMA:
            return f"WRONG_SCHEMA: {self.schema!r}"
        if self.produced_by != "supervisor":
            return f"NOT_SUPERVISOR_PRODUCED: {self.produced_by!r}"
        if self.verdict != PASS_VERDICT:
            return f"VERDICT_NOT_PASS: {self.verdict!r}"
        if self.old_rc == 0:
            return f"REPRODUCTION_NOT_RED_ON_OLD: old_rc={self.old_rc}"
        if self.new_rc != 0:
            return f"REPRODUCTION_NOT_GREEN_ON_NEW: new_rc={self.new_rc}"
        if (self.old_output_sha256 == self.new_output_sha256
                and self.old_rc == self.new_rc):
            return "VACUOUS: same bytes AND same exit code across both runs"
        if not self.old_code_sha or not self.fix_code_sha:
            return "MISSING_SHA"
        if self.old_code_sha == self.fix_code_sha:
            return "SAME_SHA"
        if not _is_ancestor(self.old_code_sha, self.fix_code_sha):
            return f"OLD_SHA_NOT_ANCESTOR_OF_FIX: {self.old_code_sha[:16]}"
        if not self.reproduction_command:
            return "NO_REPRODUCTION_COMMAND"
        return ""

    def is_valid(self) -> bool:
        return self.invalid_reason() == ""

    def closes(self, *, fingerprint: str, contract_signature: str,
               context_manifest_sha: str, old_code_sha: str,
               fix_code_sha: str) -> str:
        """Empty string when this receipt closes THAT incident; a typed reason otherwise.

        Full equality on the fingerprint, never substring: at 2900f4c8 a receipt
        naming 'x' satisfied 'product_e2e command rc=1'.
        """
        if self.old_fingerprint != fingerprint:
            return f"FINGERPRINT_MISMATCH: {self.old_fingerprint[:80]!r}"
        if contract_signature and self.contract_signature != contract_signature:
            return "CONTRACT_SIGNATURE_MISMATCH"
        if context_manifest_sha and self.context_manifest_sha != context_manifest_sha:
            return "CONTEXT_MANIFEST_MISMATCH"
        if old_code_sha and self.old_code_sha != old_code_sha:
            return f"OLD_SHA_MISMATCH: {self.old_code_sha[:16]}"
        if fix_code_sha and self.fix_code_sha != fix_code_sha \
                and not _is_ancestor(self.fix_code_sha, fix_code_sha):
            return "FIX_NOT_PRESENT_IN_RUNNING_CODE"
        return ""


def context_signature(chain: dict, contract) -> str:
    """Stable digest of the durable context this proof is bound to.

    Two attempts are only comparable when their context is the same, so the receipt
    carries this and the check refuses a receipt produced under a different one.
    """
    payload = json.dumps({
        "contract_id": getattr(contract, "contract_id", ""),
        "contract_sha": getattr(contract, "contract_sha", ""),
        "milestone_id": getattr(contract, "milestone_id", ""),
        "reproduction_command": getattr(contract, "reproduction_command", ""),
        "command_cwd": getattr(contract, "command_cwd", ""),
        "pre_repair_sha": getattr(contract, "pre_repair_sha", ""),
        "epoch": str((chain.get("current_episode") or {}).get("epoch_id", "")),
    }, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _run(command: str, cwd: Path, timeout: int, control_root: Path) -> tuple[int, str]:
    """Run one reproduction with the control repo pinned to `control_root`.

    Pinning matters: without it both runs would import the LIVE factory_v2 and the
    "old SHA" run would execute new code, making the comparison vacuous.
    """
    from .mission_evidence import control_repo_env
    env = control_repo_env()
    env["FACTORY_CONTROL_REPO"] = str(control_root)
    env["PYTHONPATH"] = str(control_root) + os.pathsep + env.get("PYTHONPATH", "")
    try:
        r = subprocess.run(["bash", "-c", f"set -eo pipefail\n{command}"],
                           capture_output=True, text=True, timeout=timeout,
                           cwd=str(cwd), env=env)
        return r.returncode, (r.stdout + r.stderr)
    except subprocess.TimeoutExpired:
        return 124, "TIMEOUT"
    except Exception as exc:                       # noqa: BLE001
        return 125, f"{type(exc).__name__}: {exc}"


def execution_root_for(product_cwd: str, worktree: Path) -> tuple[Path, str]:
    """Where a reproduction runs when the control code is `worktree`.

    A cwd INSIDE the control repo is mapped to the same relative suffix inside the
    detached worktree. Pinning FACTORY_CONTROL_REPO and PYTHONPATH is not enough for
    that case: a relative command such as `python3 repro.py`, or a cwd-relative
    Python import, resolves against the cwd, and the cwd was still the live tree.
    Codex reproduced a VALID receipt for a failing fix commit that way — an
    uncommitted repro.py in the live cwd decided the verdict — and the mirror case,
    a valid fix refused because the live source was dirty-red.

    An EXTERNAL product is classified here, then _observe_at replaces its cwd with
    an isolated checkout. Both arms use the same product commit captured before
    either observation; each arm gets a fresh checkout to isolate generated files.

    Returns (cwd, kind) with kind in {"control-relative", "external-product"}.
    """
    cwd = Path(product_cwd).resolve()
    root = _REPO_ROOT.resolve()
    # A sibling worktree of this control repository is still control code. Its
    # physical path differs, but both checkouts share the same Git common directory.
    # Treating it as an external product would freeze the WRONG control version.
    try:
        def git_dir(at, *args):
            r = subprocess.run(["git", "-C", str(at), "rev-parse", *args],
                               capture_output=True, text=True, timeout=15)
            if r.returncode:
                raise ValueError("not a git worktree")
            p = Path(r.stdout.strip())
            return (p if p.is_absolute() else Path(at) / p).resolve()
        if git_dir(cwd, "--git-common-dir") == git_dir(root, "--git-common-dir"):
            checkout = git_dir(cwd, "--show-toplevel")
            return Path(worktree) / cwd.relative_to(checkout), "control-relative"
    except (OSError, ValueError, subprocess.SubprocessError):
        # Missing/nonexistent paths within control still map into its observation;
        # an existing nested Git repository was already classified independently.
        if cwd == root or root in cwd.parents:
            return Path(worktree) / cwd.relative_to(root), "control-relative"
    return cwd, "external-product"


def product_code_sha(product_cwd: str) -> str:
    """HEAD of an external product repo, or "" when it is not a git checkout."""
    try:
        r = subprocess.run(["git", "-C", str(product_cwd), "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=15)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _product_pin(cwd: str) -> tuple[dict | None, str]:
    """Observe one immutable external product identity BEFORE either comparison arm."""
    try:
        def git(*args):
            result = subprocess.run(["git", "-C", str(cwd), *args],
                                    capture_output=True, text=True, timeout=15)
            if result.returncode:
                raise ValueError("product git identity unavailable")
            return result.stdout.strip()
        root = Path(git("rev-parse", "--show-toplevel")).resolve()
        sha = git("rev-parse", "--verify", "HEAD^{commit}")
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("product HEAD is not an exact commit")
        subdir = str(Path(cwd).resolve().relative_to(root))
        return {"repo": str(root), "sha": sha, "subdir": subdir}, ""
    except Exception as exc:
        return None, f"PRODUCT_IDENTITY_UNPROVEN: {type(exc).__name__}: {exc}"


def _observe_at(sha: str, command: str, product_cwd: str, timeout: int,
                product_pin: dict | None = None) -> tuple[int, str, str]:
    """Observe exact control code and (when external) exact product code.

    Each arm gets its own detached product checkout at the SAME previously observed
    commit. Dirty live files and writes made by the other arm cannot affect this arm.
    No fallback to the live product is allowed if an immutable checkout is unavailable.
    """
    wt = Path(tempfile.mkdtemp(prefix=f"vfx_{sha[:8]}_"))
    product_wt = None
    pin = product_pin
    try:
        add = subprocess.run(["git", "-C", str(_REPO_ROOT), "worktree", "add", "-q",
                              "--detach", str(wt), sha],
                             capture_output=True, text=True, timeout=180)
        if add.returncode != 0:
            return 126, "", f"WORKTREE_FAILED({sha[:12]}): {add.stderr.strip()[:160]}"
        exec_cwd, kind = execution_root_for(product_cwd, wt)
        if kind == "control-relative" and not exec_cwd.is_dir():
            return 126, "", (f"EXECUTION_ROOT_MISSING_AT_SHA({sha[:12]}): "
                             f"{exec_cwd} does not exist in that commit")
        if kind == "external-product":
            if pin is None:
                pin, why = _product_pin(product_cwd)
                if why:
                    return 126, "", why
            product_wt = Path(tempfile.mkdtemp(prefix="vfx_product_"))
            add = subprocess.run(["git", "-C", pin["repo"], "worktree", "add", "-q",
                                  "--detach", str(product_wt), pin["sha"]],
                                 capture_output=True, text=True, timeout=180)
            if add.returncode:
                return 126, "", f"PRODUCT_WORKTREE_FAILED: {add.stderr.strip()[:160]}"
            exec_cwd = product_wt / pin["subdir"]
            if not exec_cwd.is_dir():
                return 126, "", "PRODUCT_CWD_MISSING_AT_SHA"
        rc, out = _run(command, exec_cwd, timeout, wt)
        if product_wt is not None and product_code_sha(str(product_wt)) != pin["sha"]:
            return 126, "", "PRODUCT_HEAD_CHANGED_DURING_REPRODUCTION"
        return rc, out, ""
    finally:
        if product_wt is not None:
            subprocess.run(["git", "-C", pin["repo"], "worktree", "remove", "--force",
                            str(product_wt)], capture_output=True, timeout=120)
            shutil.rmtree(product_wt, ignore_errors=True)
        subprocess.run(["git", "-C", str(_REPO_ROOT), "worktree", "remove",
                        "--force", str(wt)], capture_output=True, timeout=120)
        shutil.rmtree(wt, ignore_errors=True)


def produce_verified_fix(*, contract, old_code_sha: str, fix_code_sha: str,
                         fingerprint: str, context_manifest_sha: str,
                         timeout: int = 900):
    """Observe the FROZEN reproduction at both commits. Returns (receipt|None, why).

    Both runs are made in detached worktrees at their exact SHAs, with the control
    root pinned to that worktree and the product cwd taken from the contract. The
    verdict is computed from the two exit codes observed; nothing is supplied by a
    caller.
    """
    command = getattr(contract, "reproduction_command", "")
    product_cwd = getattr(contract, "command_cwd", "") or str(_REPO_ROOT)
    if not command:
        return None, "CONTRACT_HAS_NO_REPRODUCTION_COMMAND"
    if not old_code_sha or not fix_code_sha:
        return None, "MISSING_SHA"
    if old_code_sha == fix_code_sha:
        return None, "SAME_SHA: nothing to compare"
    if not Path(product_cwd).is_dir():
        return None, f"PRODUCT_CWD_MISSING: {product_cwd}"

    _, kind = execution_root_for(product_cwd, _REPO_ROOT)
    pin = None
    if kind == "external-product":
        pin, why = _product_pin(product_cwd)
        if why:
            return None, why
    old_rc, old_out, why = _observe_at(old_code_sha, command, product_cwd, timeout, pin)
    if why:
        return None, why
    new_rc, new_out, why = _observe_at(fix_code_sha, command, product_cwd, timeout, pin)
    if why:
        return None, why

    r = VerifiedFixReceipt(
        schema=SCHEMA, old_fingerprint=fingerprint, old_code_sha=old_code_sha,
        fix_code_sha=fix_code_sha, reproduction_command=command,
        contract_signature=getattr(contract, "contract_sha", ""),
        context_manifest_sha=context_manifest_sha,
        old_rc=old_rc, new_rc=new_rc,
        old_output_sha256=hashlib.sha256(old_out.encode()).hexdigest(),
        new_output_sha256=hashlib.sha256(new_out.encode()).hexdigest(),
        verdict=(PASS_VERDICT if (old_rc != 0 and new_rc == 0) else "FAIL"),
        produced_by="supervisor",
        execution_root_kind=kind,
        product_code_sha=pin["sha"] if pin else "")
    return r, ""


_ID_RE = re.compile(r"^[0-9a-f]{24}$")
_PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
INDEX_NAME = "INDEX.json"


def _project_dir_for(project_id: str) -> Path:
    """Resolved, contained directory for a project's receipts.

    `project_id` is validated as a plain name and the result is asserted to be
    inside STORE after resolution, so neither '..' nor a symlink can leave it.
    """
    if not _PROJECT_RE.match(project_id or ""):
        raise ReceiptRefused("BAD_PROJECT_ID", repr(project_id))
    d = (STORE / project_id).resolve()
    root = STORE.resolve()
    if d != root and root not in d.parents:
        raise ReceiptRefused("PROJECT_ESCAPES_STORE", str(d))
    return d


def _read_index(project_id: str) -> dict:
    try:
        p = _project_dir_for(project_id) / INDEX_NAME
    except ReceiptRefused:
        return {}
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _publish_index(project_id: str, d: Path, index: dict) -> None:
    """Merge-preserving, serialized publication of INDEX.json (L5_rest_2-016).

    store_receipt used to read the index and later REPLACE it wholesale — a
    second producer's completed store in between was erased from the index,
    and load_referenced then refused the reference that store returned. The
    publish step now merges the caller's snapshot with whatever is CURRENTLY
    on disk under an exclusive per-project lock, so no coexisting receipt is
    ever dropped, whatever the interleaving."""
    import fcntl
    import uuid
    lock = d / ".index.lock"
    ip = d / INDEX_NAME
    itmp = d / f"{INDEX_NAME}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    with open(lock, "a+", encoding="utf-8") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            current = {}
            if ip.is_file():
                try:
                    cur = json.loads(ip.read_text(encoding="utf-8"))
                    if isinstance(cur, dict):
                        current = cur
                except (OSError, json.JSONDecodeError):
                    current = {}
            merged = {**index, **current}
            with itmp.open("w", encoding="utf-8") as fh:
                json.dump(merged, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(itmp, ip)
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def store_receipt(project_id: str, receipt: "VerifiedFixReceipt") -> dict:
    """Persist under the supervisor namespace and RECORD it in the index.

    The index is what makes a reference an authorization. Codex's probe loaded
    receipt_id '../../claimant' with a matching content hash: a claimant naming its
    own file and computing its own digest satisfied every check, because the checks
    only compared the reference against the document the reference pointed at.
    Selection is now bound to a document the supervisor wrote.
    """
    d = _project_dir_for(project_id)
    d.mkdir(parents=True, exist_ok=True)
    rid = receipt.receipt_id()
    chash = receipt.content_hash()
    if not _ID_RE.match(rid):
        raise ReceiptRefused("BAD_RECEIPT_ID", rid)

    p = d / f"{rid}.json"
    tmp = p.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(receipt.as_dict(), fh, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, p)

    index = _read_index(project_id)
    index[rid] = chash
    _publish_index(project_id, d, index)

    return {"receipt_id": rid, "content_hash": chash,
            "path": str(p.relative_to(_REPO_ROOT))}


def load_referenced(project_id: str, ref: dict):
    """Load exactly the receipt the SUPERVISOR INDEXED under this id, or None.

    Four things must agree: the id is a well-formed content-derived id; the index
    the supervisor wrote knows it; the resolved file is inside the store; and the
    document's own recomputed hash equals both the index entry and the reference.
    Any disagreement is a refusal, never a fallback.
    """
    rid = str((ref or {}).get("receipt_id") or "")
    want = str((ref or {}).get("content_hash") or "")
    if not _ID_RE.match(rid) or not re.match(r"^[0-9a-f]{64}$", want):
        return None
    indexed = _read_index(project_id).get(rid)
    if indexed is None or indexed != want:
        return None            # not a receipt this supervisor stored
    try:
        d = _project_dir_for(project_id)
    except ReceiptRefused:
        return None
    p = (d / f"{rid}.json").resolve()
    if d.resolve() not in p.parents or not p.is_file():
        return None            # traversal or symlink out of the namespace
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if data.get("schema") != SCHEMA:
        return None
    try:
        r = VerifiedFixReceipt(**data)
    except TypeError:
        return None
    if r.content_hash() != want or r.receipt_id() != rid:
        return None            # replaced or tampered document
    return r


def _is_ancestor(a: str, b: str) -> bool:
    if not a or not b:
        return False
    r = subprocess.run(["git", "-C", str(_REPO_ROOT), "merge-base",
                        "--is-ancestor", a, b], capture_output=True, timeout=15)
    return r.returncode == 0
