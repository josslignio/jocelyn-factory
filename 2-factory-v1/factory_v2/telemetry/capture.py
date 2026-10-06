"""Bounded capture of engine output.

Two constraints that are not negotiable on a supervisor that also runs the
drain pipe:

  * **Bounded memory.** A session log can be tens of megabytes. Nothing here
    calls read_text() on an engine log. Lines stream; only counters and a
    per-message fold survive, and that fold is itself capped in adapters.py.
  * **Never block the runner.** Capture is best-effort by construction: an
    unreadable log, a vanished file or a decode error yields a typed ABSENT
    parse, never an exception into the dispatch path. A metrics failure must
    not become a mission failure — and, just as importantly, must not become a
    silent pass either: it becomes a named coverage gap.

`tee_stream` exists for the live path where stdout must reach BOTH the caller
(which needs the text) and the meter. It keeps a bounded tail for the caller
and streams every line to the parser, so a 40 MB log costs a few KB of RAM.
"""
from __future__ import annotations

import re
from collections import deque
from pathlib import Path

from . import adapters
from .record import LOG_UNPARSEABLE, NO_LOG_CAPTURED

#: Bytes of engine stdout retained for human-readable tails elsewhere.
DEFAULT_TAIL_BYTES = 8_000
#: FACTORY_TOKEN_TRUTH_V1 — where a project keeps the RAW engine stream of every
#: launch (`projects/<p>/evidence/streams/<attempt_key>.jsonl`, bookkeeping,
#: never captured as a candidate). The cost of any launch is re-derivable from
#: it after the fact with the same adapters the live path used.
STREAMS_DIR = "streams"


#: Credential shapes that must never reach a conserved stream (telemetry invariant 6:
#: no secret enters telemetry). The counters the adapters read are untouched.
_SECRET_RE = re.compile(rb"AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{36}|sk-[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]+"
                        rb"|-----BEGIN (?:RSA|EC|OPENSSH) PRIVATE KEY-----")


def redact_line(line: bytes) -> bytes:
    """A stream line with credential-shaped tokens replaced by `<redacted:secret>`."""
    return _SECRET_RE.sub(b"<redacted:secret>", line)


def stream_path(project_dir, attempt_key: str, *, launch_seq: int = 1):
    """`<project>/evidence/streams/<attempt_key>.jsonl` — None without a project.

    The attempt key is the file name (only path separators and whitespace are
    replaced). Launch 1 of an attempt is the bare key, so the file the judge
    names exists; a relaunch (resume after a network cut, a repair step) gets
    `.L<seq>` so two sessions never share one file — a cumulative adapter
    (codex) folding two sessions as one would under-count the second.
    """
    if project_dir is None or not attempt_key:
        return None
    safe = "".join("_" if ch in "/\\\x00\n\r\t " else ch for ch in str(attempt_key)).strip("._") or "attempt"
    suffix = "" if int(launch_seq or 1) <= 1 else f".L{int(launch_seq)}"
    return Path(project_dir) / "evidence" / STREAMS_DIR / f"{safe}{suffix}.jsonl"


def persist_stream(path, text) -> bool:
    """Write the whole captured stdout to `path` (only when nothing streamed it
    there already). An empty stdout still leaves an EMPTY file: a launch cut
    before its first event is a typed fact, not a missing one. Never raises."""
    if path is None:
        return False
    p = Path(path)
    try:
        if p.exists():
            return True
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("wb") as fh:
            fh.write(redact_line((text or "").encode("utf-8", errors="replace")))
            fh.flush()
        return True
    except OSError:
        return False
#: Hard cap on lines read from any single log. Passing it is recorded, not hidden.
DEFAULT_MAX_LINES = 2_000_000


def iter_lines(path, *, max_lines: int = DEFAULT_MAX_LINES):
    """Yield lines from a log without ever holding the whole file.

    Decode errors are replaced rather than raised: a log written by a process
    that was SIGKILLed mid-UTF-8-sequence is still evidence for every line
    before the tear.
    """
    p = Path(path)
    try:
        fh = p.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return
    with fh:
        for i, line in enumerate(fh):
            if i >= max_lines:
                return
            yield line


def parse_log_file(path, *, fmt: str = "", argv=None, clean_exit=None):
    """Parse an engine log from disk, bounded, never raising into the caller."""
    p = Path(path)
    if not p.is_file():
        return adapters._absent(NO_LOG_CAPTURED, f"no log at {p}")
    fmt = fmt or adapters.detect_format(argv)
    try:
        if fmt == adapters.CLAUDE_RESULT_JSON:
            # A single JSON object; bounded by the file, which for this format is
            # one result payload rather than a whole transcript.
            head = deque(iter_lines(p, max_lines=10_000))
            return adapters.parse_claude_result_json("".join(head))
        return adapters.parse(fmt, iter_lines(p), clean_exit=clean_exit)
    except Exception as exc:                    # pragma: no cover - defensive
        return adapters._absent(LOG_UNPARSEABLE, f"{type(exc).__name__}: {exc}")


class TailBuffer:
    """A bounded ring of the most recent bytes. The caller's readable tail."""

    def __init__(self, max_bytes: int = DEFAULT_TAIL_BYTES):
        self.max_bytes = max_bytes
        self._chunks: deque = deque()
        self._size = 0

    def add(self, text: str) -> None:
        # max_bytes is a byte/memory bound, including the single-chunk case.
        raw = str(text).encode("utf-8", errors="replace")
        if len(raw) > self.max_bytes:
            raw = raw[-self.max_bytes:]
            text = raw.decode("utf-8", errors="ignore")
            raw = text.encode("utf-8")
        self._chunks.append(text)
        self._size += len(raw)
        while self._size > self.max_bytes and len(self._chunks) > 1:
            self._size -= len(self._chunks.popleft().encode("utf-8"))

    def text(self) -> str:
        joined = "".join(self._chunks).encode("utf-8")[-self.max_bytes:]
        return joined.decode("utf-8", errors="ignore")


def tee_stream(lines, *, fmt: str, clean_exit=None,
               tail_bytes: int = DEFAULT_TAIL_BYTES, sink=None):
    """Stream lines to the parser and to a bounded tail at the same time.

    Returns (ParsedLog, tail_text). `sink` receives every line verbatim when the
    caller also wants the raw stream persisted — capture writes it, capture does
    not decide to keep prompts: the sink is the caller's own log file, and the
    telemetry ledger never receives this text.
    """
    tail = TailBuffer(tail_bytes)

    def _gen():
        for line in lines:
            tail.add(line if line.endswith("\n") else line + "\n")
            if sink is not None:
                try:
                    sink.write(line if line.endswith("\n") else line + "\n")
                except Exception:
                    pass
            yield line

    parsed = adapters.parse(fmt, _gen(), clean_exit=clean_exit)
    return parsed, tail.text()
