# factory/projects/ — moved out of the repo (FACTORY_STATE_OUT_OF_REPO_V1)

This directory used to hold the legacy V1 per-project campaign state
(`factory-control/`, `job-radar/`: roadmap.jsonl, events.jsonl, evidence/,
candidate_registry.json, locks/, stop_block_counters.json, ...). That state is
runtime/campaign bookkeeping, not source, and is no longer tracked by git.

It now lives under the daemon run-dir:

    ~/Library/Application Support/JocelynFactory/run/projects/   (default)

or wherever `FACTORY_RUN_DIR` / `--run-dir` points. This directory is gitignored
(`factory/projects/` in `.gitignore`, this README excepted).

Note: `projects/<name>/` at the repo root (SPEC.md, STRATEGY.md, ROADMAP.yaml,
PROJECT_STATE.json — the V2 canonical authority for `factory` and
`twitter-radar`) is a completely different, still-tracked directory and is
out of scope for this migration.

Migrated once by `factory/bin/migrate_state_out_of_repo.py` (idempotent: copies
existing files into the run-dir if not already there, then `git rm --cached`s the
repo copies — nothing is ever deleted).
