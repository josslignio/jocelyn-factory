"""Shared, robust parsers and comparisons — ONE implementation each.

CODE_REVIEW_2026-09 SYNTHESIS item 10 (SHARED-PARSERS) and item 11
(TIME-NORMALIZATION): eight private copies of the same parser diverged
(Markdown fences, JSON extraction, NaN, pytest summaries, argv models) and
instants were sorted as ISO strings. Every site now delegates here; a fix
lands once.

Pure functions, no I/O, no engine. Property tests: tests/test_shared_parsers.py.
"""
from __future__ import annotations

import json
import math
import re
from typing import Iterable, Iterator, Optional

from factory_v2._io import _parse_utc

# ── Markdown fences (CommonMark §4.5) ────────────────────────────────────────
#
# An opening fence is 0-3 spaces then ≥3 backticks or ≥3 tildes. It is closed
# ONLY by a fence of the SAME character, at least as LONG, with nothing but
# whitespace after it. `````` inside a ```` block (or ``` inside ~~~) is text.
# L6_bin_lib_products_1-003 / L6_bin_lib_products_2-004: toggling on any
# three-char run let a quoted heading inside a four-backtick block count as
# a real section.
_FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_FENCE_CLOSE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*$")


def iter_outside_fences(text: str) -> Iterator[str]:
    """Yield the lines of `text` that are NOT inside a fenced code block.

    Fence delimiter lines themselves are never yielded. An unclosed fence
    swallows everything to EOF (CommonMark). A backtick opener whose info
    string contains a backtick is not a fence (CommonMark) — it is text.
    """
    open_char = ""
    open_len = 0
    for line in text.splitlines():
        if open_char:
            m = _FENCE_CLOSE_RE.match(line)
            if m and m.group(1)[0] == open_char and len(m.group(1)) >= open_len:
                open_char, open_len = "", 0
            continue
        m = _FENCE_OPEN_RE.match(line)
        if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
            open_char, open_len = m.group(1)[0], len(m.group(1))
            continue
        yield line


def strip_fences(text: str) -> str:
    """`text` with fenced code blocks (and their delimiters) removed."""
    return "\n".join(iter_outside_fences(text))


# ── JSON object extraction ───────────────────────────────────────────────────

_DECODER = json.JSONDecoder()
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _decode_object_at(text: str, start: int) -> Optional[dict]:
    try:
        obj, _end = _DECODER.raw_decode(text, start)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def extract_json_object(text: str) -> Optional[dict]:
    """The single JSON object in a final message: whole text, a fenced block,
    or the first `{…}` span that decodes as an object.

    L3-005: the old fallback counted braces character by character, so a
    valid object whose STRING VALUE contained `}` (root_cause="unexpected }
    delimiter") decoded alone but not after a line of prose. `raw_decode`
    respects strings and escapes; every `{` is a candidate start.
    """
    if not text or not text.strip():
        return None
    candidate = text.strip()
    fence = _JSON_FENCE_RE.search(candidate)
    if fence:
        candidate = fence.group(1).strip()
    obj = _decode_object_at(candidate, 0)
    if obj is not None:
        return obj
    start = candidate.find("{")
    while start != -1:
        obj = _decode_object_at(candidate, start)
        if obj is not None:
            return obj
        start = candidate.find("{", start + 1)
    return None


# ── Numbers ──────────────────────────────────────────────────────────────────

def is_finite_number(value) -> bool:
    """True for int/float that is finite; False for bool, NaN, ±inf, anything else.

    Python's json accepts NaN/Infinity by default, so `isinstance(x, float)`
    alone lets a non-measure through (L5_factory_v2_rest_1-017,
    L5_factory_v2_rest_2-025).
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def _reject_json_constant(name: str):
    raise ValueError(f"non-standard JSON constant {name!r} refused")


def json_loads_finite(text: str):
    """json.loads that REFUSES NaN / Infinity / -Infinity (ValueError)."""
    return json.loads(text, parse_constant=_reject_json_constant)


# ── pytest output ────────────────────────────────────────────────────────────

_PYTEST_FAILED_RE = re.compile(r"^\s*(?:FAILED|ERROR)\s+(\S+)")
_PYTEST_COUNT_RE = re.compile(r"(\d+)\s+(passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?)\b")


def pytest_failed_node_ids(test_output: str) -> set[str]:
    """Full pytest node IDs of FAILED/ERROR summary lines, parameters included.

    L6_bin_lib_products_1-024: `[\\w/.:_-]+` stopped before `[`, so
    test_add[positive] and test_add[negative] collapsed to one id and a repair
    that CHANGED the failing case looked like it had no effect.
    """
    out: set[str] = set()
    for line in (test_output or "").splitlines():
        m = _PYTEST_FAILED_RE.match(line)
        if not m:
            continue
        node = m.group(1)
        if "::" not in node and "." not in node and "/" not in node:
            continue  # "FAILED" used as a word, not a node id
        out.add(node)
    return out


def pytest_summary_counts(test_output: str) -> dict[str, int]:
    """{'passed': n, 'failed': n, 'error': n, 'skipped': n, ...} from the summary
    footer — each number is bound to the WORD that follows it, never to
    whichever integer happens to sit on the line."""
    counts: dict[str, int] = {}
    for line in reversed((test_output or "").splitlines()):
        found = _PYTEST_COUNT_RE.findall(line)
        if not found:
            continue
        for n, word in found:
            key = {"errors": "error", "warnings": "warning"}.get(word, word)
            counts[key] = counts.get(key, 0) + int(n)
        break
    return counts


# ── argv ─────────────────────────────────────────────────────────────────────

_MODEL_FLAGS = ("--model", "-m")


def model_from_argv(argv: Iterable[str]) -> str:
    """Model named by the client options (`--model X`, `-m X`, `--model=X`,
    `-mX`), else "". Only whole argv tokens are options: the prompt is one
    token and is never parsed (L3-015: Codex launches use `-m`)."""
    toks = list(argv or [])
    for i, tok in enumerate(toks):
        if not isinstance(tok, str):
            continue
        for flag in _MODEL_FLAGS:
            if tok == flag:
                return toks[i + 1] if i + 1 < len(toks) and isinstance(toks[i + 1], str) else ""
            if tok.startswith(flag + "="):
                return tok[len(flag) + 1:]
            if flag == "-m" and tok.startswith("-m") and len(tok) > 2 and not tok.startswith("--"):
                return tok[2:]
    return ""


# ── Instants ─────────────────────────────────────────────────────────────────
#
# SYNTHESIS item 11: ISO-8601 strings sort as INSTANTS only when they share an
# offset. 2026-01-02T00:30:00+02:00 is 30 minutes BEFORE 2026-01-01T23:00:00Z
# but sorts after it. Normalize through _io._parse_utc (aware UTC).

def instant_sort_key(value) -> tuple:
    """Total order on instants: parseable ISO strings by UTC epoch, then
    unreadable values (stable, by their text) — never a TypeError."""
    t = _parse_utc(value) if isinstance(value, str) else None
    if t is None:
        return (1, str(value if value is not None else ""))
    return (0, t.timestamp())


def instant_before(a, b) -> bool:
    """a < b as instants (unreadable values compare after every instant)."""
    return instant_sort_key(a) < instant_sort_key(b)


def latest_instant(values: Iterable[str], default: str = "") -> str:
    """The ORIGINAL text of the newest readable instant in `values`."""
    best, best_key = default, None
    for v in values:
        if not isinstance(v, str) or not v:
            continue
        k = instant_sort_key(v)
        if k[0] != 0:
            continue
        if best_key is None or k > best_key:
            best, best_key = v, k
    return best
