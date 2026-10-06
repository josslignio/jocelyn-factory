# CTO_EVENTS.jsonl — moved out of the repo (FACTORY_STATE_OUT_OF_REPO_V1)

`factory/cto/CTO_EVENTS.jsonl` used to be committed here. It is an append-only
event stream written on every mission settlement/stall/escalation/etc., not
source, and is no longer tracked by git.

It now lives under the daemon run-dir's journals/ subdirectory:

    ~/Library/Application Support/JocelynFactory/run/journals/CTO_EVENTS.jsonl

or wherever `FACTORY_RUN_DIR` points. `FACTORY_CTO_EVENTS_PATH` still overrides
it directly, as before (tests always set this to a tmp_path fixture).

Every other file under `factory/cto/` (CTO_STATE.json, CTO_BACKLOG.json,
CTO_DECISIONS.jsonl, canonical/, ...) is durable authority-adjacent state and
stays tracked — this migration is scoped to CTO_EVENTS.jsonl only.

Migrated once by `factory/bin/migrate_state_out_of_repo.py` (idempotent: copies
the existing file into the run-dir if not already there, then `git rm --cached`s
the repo copy — nothing is ever deleted).
