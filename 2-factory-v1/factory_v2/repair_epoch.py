"""Immutable repair episodes. E2.

The previous design kept ONE counter per quarantine. When a code fix re-entered a
terminal chain it set status back to ACTIVE but left `iterations` and
`fingerprints` from the old episode — so the next line incremented 10 to 11 and
fell straight back to MAX_ITERATIONS. Observed live on both projects: the re-entry
"worked" and changed nothing.

An episode is identified by everything that makes an attempt comparable:

    repair_epoch_id = hash(control_code_sha + context_manifest_sha
                           + contract_signature + failure_fingerprint)

Each episode owns its own iterations, fingerprints, attempts and terminal. Old
episodes are ARCHIVED, never rewritten — the history of what was actually tried
stays readable.

Changing SHA does not by itself re-open anything. A stop_event, a doc edit or a
commit that does not touch the reproduction opens no new budget. Re-entry requires
a VerifiedFixReceipt: the old fingerprint, the old SHA, the new SHA, the
reproduction RED on the old code and GREEN on the new, and an independent
validation. An incident is closed by proof, not by assertion and not by the passage
of time.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

SCHEMA = "repair-epoch/v1"

# P0-1. v1 receipts were hand-authorable JSON under the PRODUCT project: any field
# the loader did not check was a hole, and it did not check the schema, the verdict,
# the exit codes, the contract, the context or the lineage. v2 receipts are produced
# by the supervisor from its own execution and sealed with a content hash, so the
# schema string is now load-bearing: a v1 document is refused outright.
RECEIPT_SCHEMA = "verified-fix-receipt/v2"

#: The only producer a receipt may name. A receipt is a record of what the
#: supervisor ran, not a claim a worker is allowed to make about itself.
SUPERVISOR_PRODUCER = "supervisor"

#: The only verdict that re-opens an episode. "" and "REJECT" used to pass.
VERDICT_PASS = "PASS"

_HEX40 = re.compile(r"^[0-9a-f]{40}$")

#: Fields that belong to the CURRENT episode. P0-2: production used to mutate the
#: top-level copies of these directly, so `iterations` said 7 while the episode that
#: owned the budget said 3. They now have exactly one mutable home.
EPISODE_OWNED_FIELDS = ("iterations", "fingerprints", "status", "reason",
                        "terminal_code_sha")


def epoch_id(*, control_code_sha: str, context_manifest_sha: str,
             contract_signature: str, failure_fingerprint: str) -> str:
    payload = json.dumps({
        "control_code_sha": control_code_sha,
        "context_manifest_sha": context_manifest_sha,
        "contract_signature": contract_signature,
        "failure_fingerprint": failure_fingerprint,
    }, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


# The receipt lives in verified_fix.py — ONE definition. It was briefly declared in
# both modules, which is precisely the confusion that lets a weaker check win: a
# caller importing the other class would get the other rules.
from .verified_fix import VerifiedFixReceipt  # noqa: E402,F401
from factory_v2._io import _now


def new_episode(eid: str, *, cause: str, code_sha: str, context_sha: str,
                contract_signature: str, fingerprint: str) -> dict:
    return {
        "schema": SCHEMA, "epoch_id": eid, "cause": cause,
        "control_code_sha": code_sha, "context_manifest_sha": context_sha,
        "contract_signature": contract_signature, "failure_fingerprint": fingerprint,
        "iterations": 0, "fingerprints": [], "status": "ACTIVE", "reason": "",
        "terminal_code_sha": "",
        "opened_at": _now(),
    }


def current_episode(chain: dict) -> dict:
    return dict(chain.get("current_episode") or {})


def archived_episodes(chain: dict) -> list:
    return list(chain.get("archived_episodes") or [])


def project(chain: dict) -> dict:
    """Rewrite the top-level fields as a READ-ONLY mirror of the current episode.

    P0-2: the old top-level fields are still read all over the runtime, so they
    cannot simply be deleted — but they may not be a second source of truth either.
    Every durable write goes through here, so `chain["iterations"]` can no longer
    say 7 while the episode holding the budget says 3.
    """
    cur = chain.get("current_episode")
    if not cur:
        return dict(chain)
    out = dict(chain)
    out["iterations"] = int(cur.get("iterations", 0) or 0)
    out["fingerprints"] = list(cur.get("fingerprints") or [])
    out["status"] = cur.get("status") or "ACTIVE"
    out["reason"] = cur.get("reason") or ""
    out["terminal_code_sha"] = cur.get("terminal_code_sha") or ""
    out["episode_projection"] = "read-only mirror of current_episode"
    return out



def open_new_episode(chain: dict, episode: dict, receipt: "VerifiedFixReceipt | None",
                     ) -> tuple[dict, str]:
    """Archive the current episode and start a fresh one at iterations=0.

    Returns (new_chain, reason). Refuses without a valid receipt when the current
    episode is terminal: that refusal is the whole point — otherwise any commit
    would hand the loop a new budget.
    """
    cur = current_episode(chain)
    if cur and cur.get("status") not in ("", "ACTIVE"):
        if receipt is None or not receipt.is_valid():
            return chain, ("NO_VERIFIED_FIX_RECEIPT: a terminal episode re-opens "
                           "only on a reproduction that is RED on the old SHA and "
                           "GREEN on the new, independently validated")
    archived = archived_episodes(chain)
    if cur:
        entry = dict(cur)
        entry["archived_at"] = _now()
        if receipt is not None:
            entry["closed_by_receipt"] = receipt.as_dict()
            entry["superseded_reason"] = "SUPERSEDED_BY_VERIFIED_FIX"
        archived.append(entry)
    out = dict(chain)
    out["current_episode"] = episode
    out["archived_episodes"] = archived
    if receipt is not None:
        # P0-1 requirement 4: state points at the receipt by exact identity, so a
        # later edit of the stored document no longer matches what re-opened this.
        # ONE api end to end. This wrote the bound METHOD `receipt.receipt_id`
        # instead of calling it, so json.dump raised "Object of type method is not
        # JSON serializable" and the re-entry could never be persisted at all — and
        # it wrote `content_sha` while the loader reads `content_hash`, so even a
        # serialisable version would never have matched.
        out["verified_fix_ref"] = {"receipt_id": receipt.receipt_id(),
                                   "content_hash": receipt.content_hash()}
    # The live view the rest of the runtime reads stays in sync with the episode,
    # so a re-entry cannot leave a stale counter behind — the exact defect this
    # replaces.
    return project(out), "OPENED"


def bump(chain: dict, *, max_iterations: int) -> tuple[dict, bool]:
    """Increment the CURRENT episode only. Returns (chain, capped).

    P0-2: this is the production counter. Before, `_run_quarantine_cycle` did its own
    `chain["iterations"] + 1` and this function had no caller at all, so the cap was
    enforced against a number the episode did not own.
    """
    cur = current_episode(chain)
    if not cur:
        return chain, False
    cur["iterations"] = int(cur.get("iterations", 0)) + 1
    capped = cur["iterations"] > max_iterations
    out = dict(chain)
    out["current_episode"] = cur
    return project(out), capped
