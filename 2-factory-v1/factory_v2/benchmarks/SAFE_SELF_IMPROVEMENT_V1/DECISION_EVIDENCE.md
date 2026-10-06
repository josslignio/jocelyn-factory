# SAFE_SELF_IMPROVEMENT — decision evidence

This mission wires the CAS-promotion automation plumbing (capability
snapshot, hash-chained self-modification ledger, challenger-automation
receipt) around the already-measured `CHAMPION_VS_RESHAPED_CHALLENGER_V1`
matched benchmark. It does not run a new benchmark: it records the prior
measured decision through the new, independently-verifiable ledger/snapshot
machinery required by `check_self_improvement`.

## Challenger under evaluation

- challenger_sha: `063cbd7112f97c6d3fe08f379eaf927ee2f09011`
- champion_sha (unchanged): `5257c8c9689aba1e85eb7638b621458eb3a067e8`
- benchmark_receipt: `factory_v2/receipts/BENCHMARK_RECEIPT.json`
  (schema `matched-benchmark-receipt/v1`, `decision: REJECT`, see
  `factory_v2/benchmarks/CHAMPION_VS_RESHAPED_CHALLENGER_V1/DECISION_EVIDENCE.md`
  for the metric-level justification: challenger settled 5/6 vs champion 4/6
  and lost fewer launches, but did not dominate on wall-clock time,
  6.92s vs 4.508s — decision rule requires domination on all four axes).

## Decision: REJECT — challenger not promoted

`factory_v2/CHAMPION_POINTER` stays at `5257c8c9689aba1e85eb7638b621458eb3a067e8`.
`promotions` in `CHALLENGER_AUTOMATION_RECEIPT.json` is the empty list: no
compare-and-swap move of the pointer was performed, because none was
warranted. A measured REJECT is a completed experiment, not an omission.

## Capabilities unchanged before/after (D018/D019: no self-widening)

Two capability snapshots were taken with `factory_v2.capability_snapshot`
around the ledgered validation:

- before: `factory_v2/receipts/capability_snapshots/20260913T030334Z_before.json`
  (committed in the commit that also adds the snapshot/ledger producers)
- after: `factory_v2/receipts/capability_snapshots/20260913T030404Z_after.json`
  (committed here, strictly after the `before` commit and after the ledger
  commit)

The four capability fields (`actor_capabilities`, `owner_policy_ids`,
`validator_source_sha256`, `network_policy`) are byte-identical between the
two snapshots; only `taken_at` differs. No engine gained a spec level, no
owner-routing policy id was added or removed, the product validator source
hash is unchanged, and the network policy (`FACTORY_NO_REAL_ENGINE`: no
outbound calls, no real AI engine invocation) is unchanged. This experiment
did not touch `factory_v2/product_verifiers.py`, any owner authority file,
`factory/bin/model_router.py`, or `factory/bin/owner_routing_policy.py`.

## Ledger

`factory_v2/receipts/self_modification_ledger.jsonl` has one row: the
challenger commit `063cbd7112f97c6d3fe08f379eaf927ee2f09011`, marked
`validated: true`, `validated_by: "matched-benchmark judge 2586dc46"`,
bound to `benchmark_receipt_sha256` = sha256 of the committed content of
`factory_v2/receipts/BENCHMARK_RECEIPT.json`. `row_hash` is the sha256 of
the empty previous hash concatenated with the canonical JSON of the row
body — `factory_v2.self_modification_ledger.verify_chain` recomputes this
independently of any stored claim, and `tests/test_self_improvement.py`
proves it detects tampering (flipped field), a false `validated` flag, and
row reordering.

## No self-widening of authority

- The builder did not modify `factory_v2/product_verifiers.py`, any owner
  authority, `factory_v2/verify_factory_hygiene.py`, or any credential.
- The challenger evaluated here (`063cbd71`) was produced and measured by
  an earlier, separate mission (`CHAMPION_VS_RESHAPED_CHALLENGER_V1`); this
  mission only adds the snapshot/ledger/receipt automation layer around
  that already-independent measurement — it does not re-run or re-judge
  the benchmark itself.
- `UNVALIDATED_SELF_MODIFICATION` = 0: the one ledger row is `validated: true`
  and bound to a real, committed benchmark receipt.
