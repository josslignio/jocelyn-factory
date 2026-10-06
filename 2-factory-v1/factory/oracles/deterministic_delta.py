# Modeled on SWE-bench/SWE-bench (MIT License) grading.get_resolution_status(), commit
# f7bbbb2ccdf479001d6467c9e34af59e44a840f9, swebench/harness/grading.py.
# See ../../VENDOR_LOCK.json for exact provenance hashes. Modifications: collapsed to a
# single FULL-resolution outcome (fail_to_pass AND pass_to_pass both green), generalized
# from "test name" to "check name" so factory oracles besides pytest can feed it a status_map.
#
# Deliberately NOT vendored: AutoCodeRover's app/agents/agent_reviewer.py — verified it
# returns an LLM verdict, which is unfit as an anti-spoof authority (order rule 7/8). Only
# its baseline-vs-candidate pattern (run before/after, capture stdout/stderr separately) is
# reused, and that pattern lives in factoryctl.py's verify-runtime, not here.
from __future__ import annotations


def evaluate(expected: dict, status_map: dict) -> dict:
    """expected = {"fail_to_pass": [...], "pass_to_pass": [...]} — the product outcomes this
    build MUST turn green, and the ones that must NOT regress.
    status_map = {check_name: "PASSED"|"FAILED"|...} produced by the real run.
    """

    def passed(name: str) -> bool:
        return status_map.get(name) == "PASSED"

    f2p_ok = all(passed(t) for t in expected.get("fail_to_pass", []))
    p2p_ok = all(passed(t) for t in expected.get("pass_to_pass", []))
    return {
        "resolved": bool(f2p_ok and p2p_ok),  # FULL resolution = both green
        "fail_to_pass_ok": f2p_ok,
        "pass_to_pass_ok": p2p_ok,
        "missing_f2p": [t for t in expected.get("fail_to_pass", []) if not passed(t)],
        "regressed_p2p": [t for t in expected.get("pass_to_pass", []) if not passed(t)],
    }
