"""resilience — interruptions are not engine failures (owner decision 2026-09-13).

Internet drops, power cuts, quota exhaustion and a crashed terminal window are
the interruptions the owner actually lives with. Before this module each one
surfaced as SEMANTIC_FAILURE: charged to the engine's routing evidence, counted
as a failure fingerprint (three of them park a milestone), and the attempt was
restarted from scratch with the worktree's uncommitted work thrown away.

This module gives the dispatcher and the daemon four typed facts:

* classify_interruption(rc, stdout, stderr)  -> "QUOTA_CUT" | "NETWORK_CUT" | "" —
  from the engine's own error text, conservative (unknown is not a cut).
* network_available() / wait_for_network()   -> a real TCP probe to the engine
  endpoints, so a NETWORK_CUT is only declared when we can SEE the network is
  down, and the resume happens the moment it is back.
* Worktree checkpoints (`checkpoint_worktree`, `latest_checkpoint`,
  `restore_checkpoint`): the builder's uncommitted work is captured every few
  minutes as a real, reachable commit under refs/factory-checkpoints/<mission>
  WITHOUT touching the index or the working tree (git stash create + update-ref),
  so a kill at any instant loses at most one interval — and never conflicts
  with the engine's own git commands.
* choose_engine_with_quota(): the pre-emptive switch — an engine whose observed
  utilisation plus the expected cost of one more attempt would cross the refuse
  line is not launched at all; the next allowed engine is, before the cut.

Everything here is best-effort and never raises into a dispatch: a resilience
helper that crashes the launch it protects would be worse than none.
"""
from __future__ import annotations

import os
import re
import socket
import subprocess
import time
from pathlib import Path

from factory_v2._io import _git as _git_io, _now

QUOTA_CUT = "QUOTA_CUT"
NETWORK_CUT = "NETWORK_CUT"
INTERRUPTIONS = frozenset({QUOTA_CUT, NETWORK_CUT})

#: rc the dispatcher returns to its callers when the launch was cut by quota /
#: network and could not be resumed in place. Distinct from every engine rc.
QUOTA_CUT_RC = -4
NETWORK_CUT_RC = -5

_QUOTA_PATTERNS = re.compile(
    r"(usage limit reached|you've hit your (usage|rate|session) limit|session limit\b|rate limit(ed)?\b.*(reached|exceeded)|"
    r"quota (exceeded|exhausted|reached)|insufficient[_ ]quota|"
    r"429\b.*(too many requests|rate)|too many requests|"
    r"limit_reached|resets? (at|in) \d|out of (credits|quota)|"
    r"plan limit|monthly limit|weekly limit|5-hour limit|billing hard limit)",
    re.IGNORECASE)
_NETWORK_PATTERNS = re.compile(
    r"(ENOTFOUND|ECONNRESET|ECONNREFUSED|EAI_AGAIN|ETIMEDOUT|ENETUNREACH|EHOSTUNREACH|"
    r"getaddrinfo|network is unreachable|no route to host|connection (reset|refused|aborted)|"
    r"fetch failed|failed to connect|could not resolve host|"
    r"name resolution|temporary failure in name|socket hang up|"
    r"ssl(v3)? handshake|tls handshake|dns (lookup|resolution) failed|"
    r"unable to connect|network error|APIConnectionError|Connection error|"
    # Codex re-audit #2 (finding 4): provider-side outages and middleboxes are
    # interruptions too — 502/503/504, "overloaded", proxy/captive-portal pages.
    r"\b50[234]\b.*(bad gateway|service unavailable|gateway time-?out|upstream)|"
    r"(bad gateway|service unavailable|gateway time-?out)|overloaded_error|"
    r"api is (temporarily )?(unavailable|overloaded)|internal server error.*retry|"
    r"captive portal|proxy (authentication|error)|407 proxy|"
    r"stream (disconnected|error|closed unexpectedly)|"
    r"request timed out|read timed out|ETIMEDOUT)",
    re.IGNORECASE)
#: only the TAIL of stderr is classified — the diagnostic that killed the CLI is
#: its last words; a cut phrase echoed mid-stream by a test log is not (Codex
#: re-audit #2, finding 3).
STDERR_TAIL_CHARS = 4000
UNCONFIRMED = "TEXT_TAIL_UNCONFIRMED"

#: engine API endpoints — "the ENGINE can be reached" (Codex audit sc4: a cut is
#: about the engine's API, not about raw internet).
ENGINE_ENDPOINTS = (("api.anthropic.com", 443), ("api.openai.com", 443),
                    ("open.bigmodel.cn", 443))
#: the endpoint of the engine ACTUALLY selected for a launch — "the engine can be
#: reached" is judged per engine, with a TLS handshake, not "some engine's port
#: accepts TCP" (Codex re-audit #2, finding 8).
ENGINE_ENDPOINT_BY_ENGINE = {"claude": ("api.anthropic.com", 443),
                             "codex": ("api.openai.com", 443),
                             "glm": ("open.bigmodel.cn", 443)}
#: generic internet reachability, used ONLY to tell "internet down" apart from
#: "internet up but the engine API is down" (API_UNAVAILABLE).
INTERNET_ENDPOINTS = (("1.1.1.1", 443), ("8.8.8.8", 443))

API_UNAVAILABLE = "API_UNAVAILABLE"


def classify_interruption(rc: int, stdout: str = "", stderr: str = "", *,
                          engine: str | None = None, probe: bool = False) -> str:
    """Typed cut ("QUOTA_CUT"/"NETWORK_CUT") or "". See classify_interruption_detail.

    Codex audit (2026-09-13, findings sc2) REJECTED the first version: it scanned
    stdout+stderr with broad patterns, so a RED TEST whose output merely contained
    "connection refused" or "quota exceeded" was misread as a cut and its failure
    fingerprint erased (escaping the MILESTONE_PARKED ladder). Two hardening rules:

    1. Only STDERR is scanned. An engine CLI writes its transport/quota diagnostics
       to stderr; the model's own answer and any test log it echoed are on stdout,
       where "connection refused" is just words. rc must be != 0.
    2. This classifies ONE engine launch's own failure. Whether that cut is allowed
       to overrule an attempt's verdict is decided SEPARATELY and conservatively by
       the caller (dispatcher_v3 only honours it for the build launch that failed;
       mission_dispatch never lets it overrule a test-stage verdict) — so a cut can
       never whitewash a failure a mechanical oracle actually produced.
    """
    return classify_interruption_detail(rc, stdout, stderr, engine=engine, probe=probe)["cut"]


def _structured_error_text(stdout: str) -> str:
    """Text of stream-json ERROR events only (claude `type: result` with
    is_error / subtype error*, codex `type: error`), last 4000 chars."""
    import json as _json
    out = []
    for ln in stdout[-200_000:].splitlines():
        ln_s = ln.strip()
        if not ln_s.startswith("{"):
            continue
        try:
            ev = _json.loads(ln_s)
        except (ValueError, TypeError):
            continue
        if not isinstance(ev, dict):
            continue
        t = str(ev.get("type") or "")
        if t == "result" and (ev.get("is_error") or str(ev.get("subtype") or "").startswith("error")):
            out.append(str(ev.get("result") or ev.get("error") or ""))
        elif t == "error":
            out.append(str(ev.get("message") or ev.get("error") or ev))
    return "\n".join(out)[-4000:]


def classify_interruption_detail(rc: int, stdout: str = "", stderr: str = "", *,
                                 engine: str | None = None, probe: bool = False) -> dict:
    """{"cut": QUOTA_CUT|NETWORK_CUT|"", "basis": "TEXT_TAIL"|"PROBE"|"", "connectivity": state}.

    Codex re-audit #2 (findings 3/4) rejected a purely textual classifier: a test
    writing "connection refused" on stderr could still be misread, and a 502 /
    captive portal with no matching phrase was a SEMANTIC_FAILURE. Two structural
    rules now sit beside the (broadened) patterns:

    * TEXT_TAIL — only the last STDERR_TAIL_CHARS of stderr are scanned: the
      diagnostic that killed the CLI is its last output; a phrase echoed mid-stream
      by a test is not in the tail (and a test verdict is protected separately by
      the caller, see rule 2 above).
    * PROBE — when `probe=True`, a failed launch whose ENGINE endpoint cannot be
      reached (TLS handshake, per selected engine) is a NETWORK_CUT regardless of
      the text — the network is observed, not guessed — and a textual NETWORK_CUT
      is annotated with the observed connectivity so the receipt shows on what
      basis the label rests.
    """
    if rc == 0:
        return {"cut": "", "basis": "", "connectivity": ""}
    text = (stderr or "")[-STDERR_TAIL_CHARS:]     # STDERR TAIL ONLY — rule 1 + finding 3
    cut, basis = "", ""
    if _QUOTA_PATTERNS.search(text):
        cut, basis = QUOTA_CUT, "TEXT_TAIL"
    elif _NETWORK_PATTERNS.search(text):
        cut, basis = NETWORK_CUT, "TEXT_TAIL"
    if not cut:
        # 2026-09-18: stream-json engines (claude -p) report the cut in a
        # STRUCTURED error event on stdout ("You've hit your session limit ·
        # resets 3:50pm", type=result, is_error=true). Only such error events
        # are scanned — never the model's prose or an echoed test log — so a
        # red test saying "quota" on stdout is still not a cut. 4 milestones
        # were parked as SPEC_TEST_CONFLICT/build failed on a plain quota cut.
        err_text = _structured_error_text(stdout or "")
        if err_text:
            if _QUOTA_PATTERNS.search(err_text):
                cut, basis = QUOTA_CUT, "STRUCTURED_ERROR_EVENT"
            elif _NETWORK_PATTERNS.search(err_text):
                cut, basis = NETWORK_CUT, "STRUCTURED_ERROR_EVENT"
    conn = ""
    if probe and cut != QUOTA_CUT:
        conn = connectivity_state(engine=engine)
        if conn != "ENGINE_UP":
            cut, basis = NETWORK_CUT, "PROBE"
        elif cut == NETWORK_CUT:
            # Codex re-audit #4 (finding 3 vs its own new false-negative finding): the
            # text alone can neither be trusted (a semantic diagnostic may end on a
            # network phrase) nor ignored (a real blip may be over by probe time).
            # Resolution is STRUCTURAL, not lexical: a text-only cut with the API
            # reachable is UNCONFIRMED — it earns one measured in-place relaunch,
            # never a terminal "interruption" label (see dispatcher_v3: a second
            # unconfirmed text-only cut is a semantic failure, fingerprinted).
            basis = "TEXT_TAIL_UNCONFIRMED"
    return {"cut": cut, "basis": basis, "connectivity": conn}


def _any_reachable(endpoints, timeout_s: float) -> bool:
    for host, port in endpoints:
        try:
            with socket.create_connection((host, port), timeout=timeout_s):
                return True
        except OSError:
            continue
    return False


def engine_reachable(engine: str | None, timeout_s: float = 3.0) -> bool:
    """TLS handshake with the selected engine's API host (Codex re-audit #2,
    finding 8: TCP to any engine port proved nothing about THIS engine's API).
    Unknown engine → any engine endpoint (legacy behaviour)."""
    import ssl
    if os.environ.get("FACTORY_FORCE_OFFLINE") == "1":
        return False
    ep = ENGINE_ENDPOINT_BY_ENGINE.get((engine or "").lower())
    if ep is None:
        return _any_reachable(ENGINE_ENDPOINTS, timeout_s)
    host, port = ep
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=timeout_s) as raw:
            with ctx.wrap_socket(raw, server_hostname=host) as tls:
                if not tls.version():
                    return False
                # Codex re-audit #3 (finding 8): a TLS handshake can be answered by a
                # middlebox; a light unauthenticated request must reach the API layer.
                # ANY HTTP status (401/404 included) proves the API answers; a captive
                # portal / proxy page without an HTTP/1.x status line does not.
                # Codex re-audit #4 (finding 8, accepted with rationale): the probe
                # answers "is the NETWORK path to this API open?" — credentials and
                # entitlement are NOT probed here on purpose (that would need a secret
                # and a billable call inside a liveness check); an auth/quota problem
                # is diagnosed by the launch itself (QUOTA_CUT or a semantic failure).
                tls.settimeout(timeout_s)
                tls.sendall(f"HEAD / HTTP/1.1\r\nHost: {host}\r\nUser-Agent: factory-probe\r\nConnection: close\r\n\r\n".encode())
                head = tls.recv(64).decode("latin-1", "replace")
                return head.startswith("HTTP/1.") and head[9:12].isdigit()
    except (OSError, ssl.SSLError, ValueError):
        return False


def network_available(timeout_s: float = 3.0, *, engine: str | None = None) -> bool:
    """True only if an ENGINE API endpoint accepts a TCP connection (or, with
    `engine=`, completes a TLS handshake with THAT engine's host).

    Codex audit sc4:86: the first version also probed 1.1.1.1, so "internet up but
    the engine API is down" read as "up" and a NETWORK_CUT resumed instantly into
    the same wall. Now only engine endpoints count as "the engine is reachable".
    FACTORY_FORCE_OFFLINE=1 forces False (tests / drills)."""
    if os.environ.get("FACTORY_FORCE_OFFLINE") == "1":
        return False
    if engine:
        return engine_reachable(engine, timeout_s)
    return _any_reachable(ENGINE_ENDPOINTS, timeout_s)


def internet_available(timeout_s: float = 3.0) -> bool:
    """True if the raw internet is reachable (independent of the engine APIs)."""
    if os.environ.get("FACTORY_FORCE_OFFLINE") == "1":
        return False
    return _any_reachable(INTERNET_ENDPOINTS, timeout_s)


def connectivity_state(timeout_s: float = 3.0, *, engine: str | None = None) -> str:
    """"ENGINE_UP" | "API_UNAVAILABLE" (internet up, engine API down) | "OFFLINE"."""
    if network_available(timeout_s, engine=engine):
        return "ENGINE_UP"
    return API_UNAVAILABLE if internet_available(timeout_s) else "OFFLINE"


def wait_for_network(max_wait_s: int, *, poll_s: int = 15, on_wait=None,
                     engine: str | None = None) -> bool:
    """Block until an ENGINE endpoint is reachable or max_wait_s elapsed.

    Returns True only when the engine API itself is back — not merely the
    internet (Codex audit sc4:86). `on_wait(seconds_left, state)` is called each
    idle poll so the caller can surface API_UNAVAILABLE distinctly from OFFLINE."""
    deadline = time.monotonic() + max(0, max_wait_s)
    while True:
        if network_available(engine=engine):
            return True
        if time.monotonic() >= deadline:
            return False
        if on_wait:
            try:
                on_wait(int(deadline - time.monotonic()), connectivity_state(engine=engine))
            except TypeError:
                try:
                    on_wait(int(deadline - time.monotonic()))
                except Exception:                       # noqa: BLE001
                    pass
            except Exception:                           # noqa: BLE001
                pass
        time.sleep(min(poll_s, max(1, int(deadline - time.monotonic()))))


# ── worktree checkpoints ─────────────────────────────────────────────────────
REF_PREFIX = "refs/factory-checkpoints"
DEFAULT_CHECKPOINT_INTERVAL_S = 180


import tempfile as _tempfile
import threading as _threading

#: one checkpoint at a time PER PROCESS across all missions — the periodic thread
#: and the final stop() checkpoint must never run git add -A into two temp indexes
#: concurrently (Codex audit sc1:143).
_CHECKPOINT_LOCK = _threading.Lock()


def _git_nolocks(repo: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    # Adapter over factory_v2._io._git (single source of truth): injects
    # GIT_OPTIONAL_LOCKS=0 so checkpoint/status probes never take optional locks the
    # running engine's own git commands could collide with (Codex audit sc1:143).
    return _git_io(repo, *args, timeout=timeout, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})


#: module seam: tests/test_resilience.py monkeypatches resilience._git to simulate CAS
#: misses, so production call sites keep resolving the attribute at call time; the
#: no-locks adapter is the single implementation (delegates to factory_v2._io._git).
_git = _git_nolocks


def _ref_name(mission_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", mission_id)
    return f"{REF_PREFIX}/{safe}"


def checkpoint_worktree(repo: Path, mission_id: str, *, note: str = "") -> str:
    """Capture HEAD + index + working tree (tracked AND untracked, like
    `git stash -u`) as a reachable commit under refs/factory-checkpoints/<mission>
    without moving HEAD, the index or any file. Returns the commit sha, or ""
    when there is nothing new to capture or git refused.

    Implemented with `git stash create` (never `stash push`: that would reset
    the working tree under the running engine). Untracked files are included
    by adding them to a TEMPORARY index (GIT_INDEX_FILE) so the engine's real
    index is never touched.
    """
    repo = Path(repo)
    with _CHECKPOINT_LOCK:
        tmp_index = ""
        try:
            status = _git(repo, "status", "--porcelain", "--untracked-files=all")
            if status.returncode != 0 or not status.stdout.strip():
                return ""                    # clean tree: HEAD already is the checkpoint
            head = _git(repo, "rev-parse", "HEAD").stdout.strip()
            # Real missions run inside git WORKTREES, where <repo>/.git is a FILE
            # (gitdir pointer), not a directory — mkstemp(dir=<repo>/.git) would
            # raise NotADirectoryError, swallowed below as a silent "" checkpoint
            # (2026-09-15 drill: every periodic checkpoint of a real factory run
            # was empty). Resolve the actual git dir instead.
            git_dir = _git(repo, "rev-parse", "--absolute-git-dir").stdout.strip() or str(repo / ".git")
            # Random temp index name (Codex audit sc1:143: a PID-only name let the
            # periodic and final checkpoints collide on the same file).
            fd, tmp_index = _tempfile.mkstemp(prefix="factory-ckpt-index-", dir=git_dir)
            os.close(fd)
            os.unlink(tmp_index)             # git wants to create it itself
            env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_INDEX_FILE": tmp_index}
            subprocess.run(["git", "-C", str(repo), "read-tree", "HEAD"], env=env,
                           capture_output=True, text=True, timeout=60, check=True)
            subprocess.run(["git", "-C", str(repo), "add", "-A", "--", "."], env=env,
                           capture_output=True, text=True, timeout=120, check=True)
            tree = subprocess.run(["git", "-C", str(repo), "write-tree"], env=env,
                                  capture_output=True, text=True, timeout=60, check=True).stdout.strip()
            prev = latest_checkpoint(repo, mission_id)
            if prev and _git(repo, "rev-parse", f"{prev}^{{tree}}").stdout.strip() == tree:
                return ""                    # identical to the last checkpoint
            msg = f"factory checkpoint {mission_id} {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {note}".strip()
            commit = subprocess.run(["git", "-C", str(repo), "commit-tree", tree, "-p", head, "-m", msg],
                                    capture_output=True, text=True, timeout=60, check=True).stdout.strip()
            # CAS update-ref (Codex audit sc1:143): only advance the ref if it still
            # points where we read it — two dispatchers on the same mission_id can no
            # longer silently clobber each other's checkpoint. On a CAS miss the commit
            # object still exists (reachable via reflog) — nothing is lost.
            if prev:
                r = _git(repo, "update-ref", _ref_name(mission_id), commit, prev)
            else:
                r = _git(repo, "update-ref", _ref_name(mission_id), commit, "")
            if r.returncode != 0:            # someone else advanced it first
                # Codex re-audit #2 (finding 2): a CAS miss must not orphan this
                # checkpoint — park it under a per-writer ref so it stays reachable
                # and reviewable (refs/factory-checkpoints/<mission>--lost-<pid>-<ts>).
                _git(repo, "update-ref", f"{_ref_name(mission_id)}--lost-{os.getpid()}-{int(time.time())}", commit)
                return ""
            return commit
        except (subprocess.SubprocessError, OSError):
            return ""
        finally:
            if tmp_index:
                for suffix in ("", ".lock"):
                    try:
                        os.unlink(tmp_index + suffix)
                    except OSError:
                        pass


def latest_checkpoint(repo: Path, mission_id: str) -> str:
    r = _git(Path(repo), "rev-parse", "--verify", "--quiet", _ref_name(mission_id))
    return r.stdout.strip() if r.returncode == 0 else ""


def checkpoint_summary(repo: Path, mission_id: str) -> dict:
    """What a resumed builder is told: sha, age, files touched vs its parent."""
    sha = latest_checkpoint(repo, mission_id)
    if not sha:
        return {}
    files = _git(Path(repo), "diff", "--name-status", f"{sha}^", sha).stdout.strip().splitlines()
    when = _git(Path(repo), "log", "-1", "--format=%cI", sha).stdout.strip()
    return {"sha": sha, "committed_at": when, "files": files[:200], "file_count": len(files)}


# ── durable interruption marker (Codex audit sc6) ────────────────────────────
# The resume preamble must fire ONLY when the previous launch of THIS mission at
# THIS base was genuinely cut — never merely because a worktree is dirty after a
# real semantic failure. A cut writes this marker; _resume_preamble consumes it.
_MARKER_DIR = ".factory"


def _marker_path(repo: Path, mission_id: str) -> Path:
    # one file PER MISSION (Codex re-audit #2, new finding 2: a shared path let two
    # missions/worktrees overwrite — and delete — each other's marker)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", mission_id)
    return Path(repo) / _MARKER_DIR / f"resume_marker_{safe}.json"


def write_interruption_marker(repo: Path, mission_id: str, base_sha: str, cut: str, *,
                              attempt: int | None = None, checkpoint: str = "") -> None:
    """Durable, atomic (tmp + rename) record that THIS mission at THIS base — and
    this attempt, with this last checkpoint — was cut (Codex re-audit #2, finding
    11: the preamble must be able to prove which interruption it resumes)."""
    try:
        path = _marker_path(repo, mission_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        # unique temp name (Codex re-audit #3): two attempts of one mission never
        # share a temp file; os.replace makes the publish atomic
        fd, tmp_name = _tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
        os.close(fd)
        tmp = Path(tmp_name)
        tmp.write_text(__import__("json").dumps(
            {"mission_id": mission_id, "base_sha": base_sha, "cut": cut,
             "attempt": attempt, "checkpoint": checkpoint,
             "at": _now()}), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


CLAIM_ADOPT_GRACE_S = 30.0


def _claim_reader_alive(claim_name: str, now: float | None = None) -> bool:
    """True when `<marker>.claim-<pid>-<ms>` is still being read.

    A consume takes milliseconds: a claim younger than CLAIM_ADOPT_GRACE_S whose
    pid exists (our own pid included — a sibling thread may be mid-consume) is
    in use. Older than the grace it is an orphan whatever the pid says (a dead
    reader's pid may have been reused; a reader hung for 30 s has lost its turn).
    Unparseable names are treated as live — never adopt what we cannot judge."""
    try:
        head, stamp = claim_name.rsplit(".claim-", 1)[1].split("-", 1)
        pid, claimed_ms = int(head), int(stamp)
    except (IndexError, ValueError):
        return True
    now = time.time() if now is None else now
    if now - claimed_ms / 1000.0 > CLAIM_ADOPT_GRACE_S:
        return False
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


def consume_interruption_marker(repo: Path, mission_id: str, base_sha: str) -> dict:
    """Return the marker dict iff it matches (mission_id, base_sha), and delete it
    ONLY in that case. {} when absent or mismatched — a mismatch means a different
    mission/base, never a resume, and the other mission's marker is left intact
    (Codex re-audit #2, new finding 2)."""
    path = _marker_path(repo, mission_id)
    # Codex re-audit #3: claim the marker by ATOMIC RENAME first (only one consumer
    # can win; a marker rewritten after our read can never be deleted by us), then
    # validate; on identity mismatch put it back untouched.
    claim = path.with_name(f"{path.name}.claim-{os.getpid()}-{int(time.time() * 1000)}")
    # Codex re-audit #4: a consumer killed between claim and validation leaves an
    # orphan .claim-*; adopt it (rename back) before claiming so no interruption is
    # ever lost — a durable marker must survive the death of its reader.
    # L4_resilience_review-009: a claim is orphaned ONLY when its reader is gone.
    # The claim name carries the reader's pid; while that pid is alive (another
    # process, or another thread of this very process) the claim is in use and
    # adopting it would hand the same marker to two consumers.
    try:
        for orphan in sorted(path.parent.glob(path.name + ".claim-*")):
            if orphan == claim or _claim_reader_alive(orphan.name):
                continue
            if path.exists():
                orphan.unlink()
            else:
                os.rename(orphan, path)
    except OSError:
        pass
    try:
        os.rename(path, claim)
    except OSError:
        return {}
    try:
        data = __import__("json").loads(claim.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict) or data.get("mission_id") != mission_id or data.get("base_sha") != base_sha:
        try:
            if data is not None and not path.exists():
                os.rename(claim, path)           # restore the other mission's marker
            else:
                claim.unlink()
        except OSError:
            pass
        return {}
    try:
        claim.unlink()
    except OSError:
        pass
    return data


class RestoreRefused(RuntimeError):
    """restore_checkpoint refused because it would have clobbered real work."""


def restore_checkpoint(repo: Path, mission_id: str, *, force: bool = False) -> str:
    """Make the worktree EXACTLY the last checkpoint's content (index + working
    tree), without moving HEAD. Returns the checkpoint sha, or "" when there is
    no checkpoint.

    Codex audit sc1:194 REJECTED the first version: `git restore --worktree` left
    the index stale, did NOT remove files created after the checkpoint, and would
    silently overwrite uncommitted work a builder had in the worktree — a hybrid
    state. This version:

    * refuses (RestoreRefused) when the worktree carries uncommitted changes that
      are NOT already captured in this mission's checkpoint — the caller must be
      resuming into a fresh/archived worktree, never overwriting live work
      (pass force=True only when the caller has itself archived that work);
    * uses `git read-tree -u --reset <tree>` so index AND working tree become the
      checkpoint tree atomically and tracked files absent from it are removed.
    """
    repo = Path(repo)
    sha = latest_checkpoint(repo, mission_id)
    if not sha:
        return ""
    tree = _git(repo, "rev-parse", f"{sha}^{{tree}}").stdout.strip()
    if not force:
        dirty = _git(repo, "status", "--porcelain", "--untracked-files=all").stdout.strip()
        if dirty:
            # is the dirty state already exactly this checkpoint? then restoring is a no-op and safe
            cur = checkpoint_worktree(repo, f"__probe__{mission_id}", note="probe")
            probe_tree = _git(repo, "rev-parse", f"{cur}^{{tree}}").stdout.strip() if cur else ""
            _git(repo, "update-ref", "-d", _ref_name(f"__probe__{mission_id}"))
            # L4_resilience_review-008: the refusal used to be conditioned on
            # probe_tree being non-empty — but checkpoint_worktree returns ""
            # whenever a git command fails, and an unobservable worktree state
            # is precisely when restoring would blindly clobber real work.
            if not probe_tree:
                raise RestoreRefused(
                    f"worktree state unobservable (checkpoint probe failed) — refusing "
                    f"to restore {repo} while it carries uncommitted changes")
            if probe_tree != tree:
                raise RestoreRefused(
                    f"worktree has uncommitted work not in checkpoint {sha[:12]} — refusing to "
                    f"clobber it (archive it first, or pass force=True)")
    r = _git(repo, "read-tree", "-u", "--reset", tree)
    if r.returncode != 0:                    # fallback: explicit worktree+index restore
        _git(repo, "restore", "--source", sha, "--staged", "--worktree", "--", ".")
    # read-tree removes TRACKED files absent from the tree but leaves untracked
    # extras created after the checkpoint — remove them too so the worktree is
    # EXACTLY the checkpoint (Codex audit sc1: no hybrid state). Scoped to the
    # worktree; the checkpoint's own files are now tracked, so they are kept.
    # Deliberately NOT `-x` (Codex re-audit #2, finding 1 — accepted with rationale):
    # ignored files are by definition outside the deliverable (venvs, caches,
    # build outputs); `-x` would wipe the builder's environment on every resume
    # and cost minutes to rebuild for zero fidelity gain on what is judged.
    _git(repo, "clean", "-d", "-f", "--", ".")
    return sha


class WorktreeCheckpointer:
    """Background thread: checkpoint the worktree every `interval_s` while an
    engine runs. start()/stop() never raise."""

    def __init__(self, repo: Path, mission_id: str, *, interval_s: int | None = None, on_checkpoint=None):
        import threading
        self.repo, self.mission_id = Path(repo), mission_id
        self.interval_s = int(interval_s or os.environ.get("FACTORY_CHECKPOINT_INTERVAL_S") or DEFAULT_CHECKPOINT_INTERVAL_S)
        self.on_checkpoint = on_checkpoint
        self.shas: list[str] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name=f"ckpt:{mission_id}", daemon=True)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_s):
            sha = checkpoint_worktree(self.repo, self.mission_id, note="periodic")
            if sha:
                self.shas.append(sha)
                if self.on_checkpoint:
                    try:
                        self.on_checkpoint(sha)
                    except Exception:               # noqa: BLE001
                        pass

    def start(self) -> "WorktreeCheckpointer":
        if self.interval_s > 0:
            self._thread.start()
        return self

    def stop(self, *, final: bool = True) -> list[str]:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5)
        if final:
            sha = checkpoint_worktree(self.repo, self.mission_id, note="final")
            if sha:
                self.shas.append(sha)
        return self.shas


# ── pre-emptive quota switch ─────────────────────────────────────────────────
#: expected utilisation one more attempt adds to a provider's tightest window,
#: when the ledger has nothing better to say (a Claude build is ~2-4 % of a
#: weekly window on the owner's plan; Codex ~1 %; GLM ~3 %).
DEFAULT_ATTEMPT_COST = {"claude": 0.03, "codex": 0.01, "glm": 0.03}


def projected_allowance(provider: str, view: dict, *, attempt_cost: float | None = None,
                        refuse_at: float | None = None) -> dict:
    """quota_observer.engine_allowance, but on utilisation + the cost of the
    attempt we are about to launch — so the switch happens BEFORE the cut."""
    from .quota_observer import REFUSE_AT, engine_allowance
    refuse_at = REFUSE_AT if refuse_at is None else refuse_at
    cost = DEFAULT_ATTEMPT_COST.get(provider, 0.03) if attempt_cost is None else attempt_cost
    projected = {}
    for window, entry in (view or {}).items():
        if not isinstance(entry, dict):
            continue
        if window == "_owner_order":
            projected[window] = entry            # carried through untouched (see quota_observer)
            continue
        util = entry.get("utilisation")
        if isinstance(util, (int, float)) and not isinstance(util, bool):
            projected[window] = {**entry, "utilisation": min(1.0, float(util) + cost)}
        else:
            projected[window] = entry
    out = engine_allowance(provider, projected, {"refuse_at": refuse_at})
    out["projected_with_attempt_cost"] = cost
    # Codex audit sc5:259 — "unknown is not a refusal" is kept, but the caller
    # must be able to SEE that a green light rested on no data (no numeric window),
    # so it can log it / ration the first probe rather than treat it as proven room.
    out["quota_known"] = out.get("headroom") is not None
    return out


def choose_engine_with_quota(choice, alternates, merged: dict, *, log=None):
    """Return `choice` if its provider has room for one more attempt; otherwise
    the first alternate that does; otherwise `choice` unchanged (unknown quota is
    never a refusal). `alternates` are EngineChoice-like objects with `.engine`.
    Emits a typed decision dict via `log` when it switches."""
    def _ok(c):
        return projected_allowance(c.engine, (merged or {}).get(c.engine) or {})
    first = _ok(choice)
    if first.get("allowed", True):
        return choice, {"switched": False, "reason": first.get("reason", "")}
    for alt in alternates or ():
        if alt.engine == choice.engine:
            continue
        a = _ok(alt)
        if a.get("allowed", True):
            decision = {"switched": True, "from": choice.engine, "to": alt.engine,
                        "reason": f"PREEMPTIVE_QUOTA_SWITCH: {first.get('reason', '')}"}
            if log:
                try:
                    log(decision)
                except Exception:                       # noqa: BLE001
                    pass
            return alt, decision
    return choice, {"switched": False, "reason": f"no alternate with room: {first.get('reason', '')}"}
