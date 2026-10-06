#!/usr/bin/env python3
"""Factory verdict taxonomy — FD3 fix.

Extends the existing BLOCKED verdict with sub-types for external dependency failures.
Workers and missions can import these constants to report precise failure reasons.
dispatcher_v3.py uses classify_blocked_reason() to sub-type BLOCKED verdicts.
"""

DEPENDENCY_MISSING = "DEPENDENCY_MISSING"
AUTH_REQUIRED      = "AUTH_REQUIRED"
AUTH_INVALID       = "AUTH_INVALID"
RATE_LIMITED       = "RATE_LIMITED"
INFRA_FAIL         = "INFRA_FAIL"
VIOLATED           = "VIOLATED"
SPEC_TEST_CONFLICT = "SPEC_TEST_CONFLICT"

# These verdicts are continuable (Factory can continue independent work):
CONTINUABLE_BLOCKED = {DEPENDENCY_MISSING, AUTH_REQUIRED, AUTH_INVALID, RATE_LIMITED, INFRA_FAIL}

# These verdicts require repair:
REPAIR_REQUIRED = {VIOLATED}

# All sub-typed verdicts (superset of BLOCKED):
ALL_TAXONOMY = {DEPENDENCY_MISSING, AUTH_REQUIRED, AUTH_INVALID, RATE_LIMITED, INFRA_FAIL, VIOLATED, SPEC_TEST_CONFLICT}


def classify_blocked_reason(stdout: str, stderr: str, test_output: str = "") -> str:
    """Classify a BLOCKED situation into a precise verdict.

    Returns one of the taxonomy constants, or 'BLOCKED' if unclassified.
    Case-insensitive. Checks stdout, stderr, and test output.
    """
    combined = (stdout + stderr + test_output).lower()

    # Order matters: check most specific patterns first
    if any(p in combined for p in [
        "modulenotfounderror", "no module named", "importerror",
        "dependency_missing", "not installed", "command not found",
    ]):
        return DEPENDENCY_MISSING

    if any(p in combined for p in [
        "auth_required", "login required", "not authenticated",
        "no credentials", "credentials not configured",
    ]):
        return AUTH_REQUIRED

    if any(p in combined for p in [
        "auth_invalid", "invalid credentials", "unauthorized", "401",
        "authentication failed", "bad credentials",
    ]):
        return AUTH_INVALID

    if any(p in combined for p in [
        "rate_limited", "rate limit", "too many requests", "429",
        "ratelimit", "throttled",
    ]):
        return RATE_LIMITED

    if any(p in combined for p in [
        "infra_fail", "connection refused", "network unreachable",
        "connection error", "socket error", "connection reset",
    ]):
        return INFRA_FAIL

    return "BLOCKED"


def extract_failed_tests(test_output: str) -> set[str]:
    """Extract failing test node IDs from pytest -v output.
    Matches lines like: 'FAILED tests/foo.py::TestX::test_y - ...'
    """
    # L6_bin_lib_products_1-024: the old character class stopped before `[`,
    # so test_add[positive] and test_add[negative] were the same id and a
    # repair that changed the failing case read as SPEC_TEST_CONFLICT.
    try:
        from factory_v2.shared_parsers import pytest_failed_node_ids
    except ImportError:  # launched directly from factory/bin: repo root not on sys.path yet
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from factory_v2.shared_parsers import pytest_failed_node_ids
    return pytest_failed_node_ids(test_output)


def _has_pytest_markers(test_output: str) -> bool:
    """True if test_output contains recognizable pytest failure markers."""
    return "FAILED " in test_output or "::test_" in test_output


def real_pytest_report(test_output: str) -> bool:
    """L3-007: recognize a typical pytest report shape — a summary footer line
    (`===== N failed/passed/error[s]
    in X.XXs =====`) or a `short test summary info` block. Bare `FAILED x::y`
    lines (which _has_pytest_markers also matches) are insufficient. This is a
    routing heuristic, not proof that pytest ran: any stdout can be fabricated.
    Execution proof comes from the independently observed test process."""
    import re
    if not test_output:
        return False
    if "short test summary info" in test_output:
        return True
    return bool(re.search(
        r"^\s*=+\s+\d+\s+(failed|passed|error|skipped|deselected|warning|xfailed|xpassed)",
        test_output, re.M))


def detect_spec_test_conflict(test_out_1: str, test_out_2: str) -> bool:
    """Return True if test_1 and test_2 failed on identical test sets.

    Identical failing test set = repair had no effect on failing tests =
    SPEC_TEST_CONFLICT (contract mismatch), not VIOLATED (implementation error).
    Returns False if either output has no failures (edge case: empty set not a conflict).

    Fallback (non-pytest outputs): when pytest extraction is empty on either
    side (e.g. a worker/tool error like 'VERIFIER_NOT_IMPLEMENTED: xyz' rather
    than a pytest report), compare the raw outputs verbatim (stripped). Two
    non-empty, byte-identical outputs are a real SPEC_TEST_CONFLICT — the
    repair produced no observable change. Restricted to outputs with no
    pytest markers, so a truncated/partial pytest report never falls through
    to this path and gets misjudged as a text-identity conflict.
    """
    failed_1 = extract_failed_tests(test_out_1)
    failed_2 = extract_failed_tests(test_out_2)
    if not failed_1 or not failed_2:
        if _has_pytest_markers(test_out_1) or _has_pytest_markers(test_out_2):
            return False
        stripped_1 = test_out_1.strip()
        stripped_2 = test_out_2.strip()
        return bool(stripped_1) and bool(stripped_2) and stripped_1 == stripped_2
    return failed_1 == failed_2


def is_continuable(verdict: str) -> bool:
    """Returns True if this verdict allows continuing independent work."""
    return verdict in CONTINUABLE_BLOCKED


def human_readable(verdict: str) -> str:
    """Returns a short English description of a verdict."""
    _DESCRIPTIONS = {
        DEPENDENCY_MISSING: "package/tool not installed — run pip install or equivalent",
        AUTH_REQUIRED: "auth not configured — human must provide credentials",
        AUTH_INVALID: "auth configured but rejected — rotate or fix credentials",
        RATE_LIMITED: "too many requests — retry after cooldown",
        INFRA_FAIL: "network/infrastructure failure — transient, retry",
        VIOLATED: "test oracle failed — code is wrong, repair needed",
        SPEC_TEST_CONFLICT: "spec and test contract conflict — repair cannot converge; supervisor must rewrite task contract",
        "BLOCKED": "generic block — unclassified reason",
    }
    return _DESCRIPTIONS.get(verdict, f"unknown verdict: {verdict}")

# Verdicts on which a dispatch is over — no repair step follows, the supervisor
# must not relaunch the same mission blindly. Formerly autopilot.TERMINAL_VERDICTS
# (module deleted by FACTORY_SIMPLIFY_S2, 2026-09-13); kept here so the FD9
# qualification contract still names one authoritative set.
TERMINAL_VERDICTS = frozenset({"MOTOR_UNAVAILABLE", "BLOCKED", "REFUSED", "ACCEPTED", "TIMEOUT",
                               "CANDIDATE_CAPTURE_REFUSED", "BUILDER_REPORT_MISSING"})
