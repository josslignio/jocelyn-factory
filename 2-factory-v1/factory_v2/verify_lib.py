"""Shared verification primitives for judges and gates.

FACTORY_REPAIR_VERIFY_HARDENING_V1 synthesis deliverable: the recurring shapes
of fail-open judges (length-of-list instead of distinct ids, decorative
digests, inferred exit codes, silent absence) get one canonical, tested
implementation. Every helper FAILS CLOSED: absent/unknown/malformed data is a
typed refusal, never a default success.

Convention: each helper returns a ``(ok: bool, detail: str)`` tuple or raises
``VerificationRefused`` — callers turn the refusal into their own typed
finding (Unmet / Unproven / REJECT_* / FAIL...). No helper ever returns a
"pass by default".
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class VerificationRefused(Exception):
    """Typed fail-closed refusal: the data needed to verify is absent,
    malformed or contradictory — absence of evidence is never evidence."""


def distinct_or_fail(values, *, what: str, minimum: int = 1) -> tuple[int, str]:
    """Count DISTINCT non-empty ids. Duplicates are not additional evidence:
    `[M1, M1, M1]` is one milestone, `["h", "h"] * 20` is one account.

    Returns (distinct_count, detail); refuses (raises VerificationRefused)
    when fewer than `minimum` distinct values are present or when the input is
    not a list."""
    if not isinstance(values, list):
        raise VerificationRefused(f"{what} must be a list, got {type(values).__name__}")
    for value in values:
        if value is not None and not isinstance(value, str):
            raise VerificationRefused(
                f"{what}: unknown identifier type {type(value).__name__}; expected a string")
    ids = [value.strip() for value in values if value is not None and value.strip()]
    distinct = sorted(set(ids))
    if len(distinct) < minimum:
        raise VerificationRefused(
            f"{what}: {len(values)} entries but only {len(distinct)} DISTINCT "
            f"value(s) {distinct[:8]} — the same proof is being counted "
            f"{max(len(ids) // max(len(distinct), 1), 1)}x; duplicates are not "
            "additional evidence")
    return len(distinct), f"{len(distinct)} distinct {what} (from {len(values)} entries)"


def positive_int_or_fail(value, *, what: str, allow_zero: bool = False) -> int:
    """Coerce to a strictly positive (or ≥0) integer. Bools are NOT integers
    here (`True` is not 1 settled milestone); negatives, floats, strings and
    None are refused — `settlements_covered=-1` must not sail through a
    `== 0` check."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise VerificationRefused(
            f"{what} must be an integer, got {value!r} ({type(value).__name__})")
    if value < 0 or (value == 0 and not allow_zero):
        raise VerificationRefused(f"{what} must be > 0, got {value!r}")
    return value


def sha256_of_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()



def sha256_matches_bytes(pinned: str, data: bytes, *, what: str) -> tuple[bool, str]:
    """Confront a pinned digest to the actual bytes. A missing or malformed
    pin is NOT a match: two all-zero digests proved nothing in the wild."""
    pinned = str(pinned or "").strip().lower()
    if not _SHA256_RE.fullmatch(pinned):
        raise VerificationRefused(
            f"{what}: pinned digest {pinned[:16]!r} is absent/malformed — an "
            "unbound artifact has no immutable binding")
    actual = sha256_of_bytes(data)
    if actual != pinned:
        return False, (f"{what}: pinned {pinned[:12]} != actual bytes {actual[:12]}")
    return True, f"{what}: pinned sha256 matches the bytes"


def exit_code_agrees(receipt_code, process_code, *, what: str) -> tuple[bool, str]:
    """A receipt's declared exit code must equal the code the process actually
    exited with. An inferred-from-stdout rc (a «passed» line) is never an
    exit code."""
    if isinstance(receipt_code, bool) or not isinstance(receipt_code, int):
        raise VerificationRefused(
            f"{what}: receipt exit_code {receipt_code!r} is not an integer")
    if isinstance(process_code, bool) or not isinstance(process_code, int):
        raise VerificationRefused(
            f"{what}: observed process exit code {process_code!r} is not an integer")
    if receipt_code != process_code:
        return False, (f"{what}: receipt claims exit_code={receipt_code} but the "
                       f"process exited {process_code} — the receipt does not "
                       "describe the run that happened")
    return True, f"{what}: receipt exit_code agrees with the observed process rc"
