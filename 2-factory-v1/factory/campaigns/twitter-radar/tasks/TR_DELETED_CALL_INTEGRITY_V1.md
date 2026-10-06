# TR_DELETED_CALL_INTEGRITY_V1 — a losing call cannot be deleted out of history

## Product outcome

If a KOL deletes a bad call, the track record still shows it, flagged — and the system never
confuses a deletion with an outage, a suspension or a blocked credential.

Authority: `projects/twitter-radar/STRATEGY.md`, `SPEC.md`, `ROADMAP.yaml`. Read from disk.

## Depends on

`TR_WEB_PRODUCT_V1` COMPLETE.

## What exists already — do not rebuild

- `data/corpus/<handle>/posts.jsonl` + `data/corpus/CORPUS_MANIFEST.json` — the corpus is
  READ-ONLY (`ACCEPTED_M008A`). This milestone adds an observation ledger beside it; it never
  rewrites corpus history.
- `scripts/scrape_manager.py` — typed provider states (`terminal`, `blocked_auth`,
  `unavailable`) already distinguish provider failure from absence. Reuse them.

## Required behaviour

### Immutable observation ledger

One append-only record per monitored post: post id, content hash, first-seen timestamp,
last-seen timestamp, and every observation attempt with its outcome. Append-only means an
earlier observation is never edited or removed.

### The state machine — the whole point of this milestone

- A post that fails to appear is `MISSING_UNCONFIRMED`. Always. First time, every time.
- It may only become `DELETED_OR_UNAVAILABLE` after **repeated successful source checks** —
  meaning checks where the provider answered normally and the post was still absent.
- A check performed under cooldown, auth failure, suspension, rate limiting or an incomplete
  API result is **not** a successful check. It cannot advance the state. It is recorded with
  its typed provider reason.
- Reappearance is a legal transition and must be handled: a post seen again returns to
  observed, with the full history preserved.

### Track-record integrity

Deleted or unavailable calls **remain in the historical track record** with a visible deletion
flag. Deletion never erases a losing call and never improves a KOL's statistics. Any metric
that would change when a post disappears is a defect.

### Honesty about intent

The system records observable facts: the post was there, then it was not, after N successful
checks. It **never claims** intent to hide, deception or malice as proven fact.

### Bounded cadence

Recheck cadence is bounded and reserved for the best monitored KOLs. Respect frozen rate
limits (1 req / 2s, max 100/h). No recheck storm.

## Deliverables

- `research/tr_post_integrity_ledger.json` — the append-only observation ledger.
- `research/tr_deletion_report.json` — current per-post state, the successful-check count that
  justified each `DELETED_OR_UNAVAILABLE`, and the typed provider reason for every
  non-advancing check.

## Black-box acceptance

```
PYTHONPATH="${FACTORY_CONTROL_REPO}" python3 -m factory_v2.product_verifiers \
    TR_DELETED_CALL_INTEGRITY_V1 --project-repo . --owner-shortlist 'Frenchie'
```

Supervisor-owned. The verifier requires **observed repeated absences, outage handling and
reappearance over time** — it drives a controlled provider that alternates absence with
failure and requires the state machine to refuse to promote to deletion on failed checks.

## Constraints

- Do not modify Factory source, canonical authority, `PROJECT_STATE.json` or any verifier.
- The corpus is read-only. Never rewrite or prune post history.
- Never write a credential, cookie or token into source, log, receipt or report.
