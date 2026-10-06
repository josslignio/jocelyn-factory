# factory/run/ — moved out of the repo (FACTORY_STATE_OUT_OF_REPO_V1)

The daemon's mutable runtime state (pid file, lock file, log, supervisor state,
quota snapshot, generated metrics reports, ...) is no longer tracked by git and no
longer lives under this directory in the repo. It now lives under the daemon
run-dir:

    ~/Library/Application Support/JocelynFactory/run/   (default)

or wherever `FACTORY_RUN_DIR` / `--run-dir` points (tests and dev always use a
tmp_path fixture; production launchd already passes the path above via
`--run-dir`).

This directory is gitignored (`factory/run/` in `.gitignore`, this README
excepted). A local `factory/run/` may still exist on disk as a leftover from
before the migration, or because some tool still resolves paths relative to the
repo root by mistake — that's a bug to report, not a place to look for the real,
current state.

Migrated once by `factory/bin/migrate_state_out_of_repo.py` (idempotent: copies
existing files into the run-dir if not already there, then `git rm --cached`s the
repo copies — nothing is ever deleted).
