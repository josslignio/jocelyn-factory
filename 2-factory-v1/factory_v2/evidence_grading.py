"""Typed deferral for capabilities without independent observation producers.

An existing claimant-owned file does not establish an independently observed run.
Keep unimplemented qualification gates closed until their producer and reconstruction
are independently reviewed and frozen before the experiment.
"""
NOT_IMPLEMENTED = 2


def not_implemented(what: str, producer: str) -> tuple[int, str]:
    return NOT_IMPLEMENTED, (
        f"VERIFIER_NOT_IMPLEMENTED: {what} requires independent observations and "
        f"reconstruction; implementation task: {producer}. "
        "This deferral does not implement or qualify the capability.")
