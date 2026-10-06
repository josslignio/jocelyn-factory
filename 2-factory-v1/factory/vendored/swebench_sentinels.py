# Vendored from SWE-bench/SWE-bench (MIT License), commit f7bbbb2ccdf479001d6467c9e34af59e44a840f9.
# Source: swebench/harness/constants/__init__.py (sentinel strings) +
#         swebench/harness/grading.py (get_logs_eval's bad-code-then-sentinel logic).
# See ../../VENDOR_LOCK.json for exact provenance hashes. Modifications: renamed to this
# module's own names, reduced to the sentinel constants + extraction function actually used here.
from __future__ import annotations

START_TEST_OUTPUT = ">>>>> Start Test Output"
END_TEST_OUTPUT = ">>>>> End Test Output"

# Upstream infra-failure sentinels that must be rejected BEFORE parsing (equivalent to
# upstream's bad-log short-circuit in get_logs_eval / get_eval_type).
BAD_CODES = (
    ">>>>> Patch Apply Failed",
    ">>>>> Reset Failed",
    ">>>>> Tests Errored",
    ">>>>> Tests Timed Out",
)


def extract_test_output(log_text: str) -> str | None:
    """Return ONLY the text between the two sentinels.

    None if a sentinel is missing or an infra-failure code is present — fail-closed: a
    truncated or spoofed log must never be accepted as a valid test output section.
    """
    if any(code in log_text for code in BAD_CODES):
        return None
    if START_TEST_OUTPUT not in log_text or END_TEST_OUTPUT not in log_text:
        return None
    return log_text.split(START_TEST_OUTPUT, 1)[1].split(END_TEST_OUTPUT, 1)[0]
