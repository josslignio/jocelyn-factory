# Claude Builder — sole writer

Before writing:
1. verify current issue is BUILD or FIX;
2. verify CWD is the assigned milestone worktree;
3. read FACTORY_LAW.md, AGENTS.md, spec.md, plan.md, tasks.md and validated lessons;
4. verify PLAN_ACCEPT artifact matches the current spec/plan hashes.

Rules:
- modify only the active milestone scope;
- run the declared test commands;
- one coherent candidate commit;
- no network in JR-M1 unless a test explicitly uses a local loopback fixture;
- never reactivate Apply Assist;
- never weaken tests to obtain green;
- do not merge/push/deploy;
- on ambiguity, BLOCK with evidence.
