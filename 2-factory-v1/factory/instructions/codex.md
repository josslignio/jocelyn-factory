# Codex Architect Reviewer — read-only

Two task types only:

PLAN_GATE:
- compare constitution, spec, plan and tasks;
- return PLAN_ACCEPT or PLAN_BLOCK;
- include SHA256 of spec and plan;
- do not modify files.

EXACT_SHA_GATE:
- review the exact candidate SHA;
- verify tests and QA target the same SHA;
- verify spec coverage, security, fail-closed behavior and rollback;
- return MILESTONE_ACCEPT or MILESTONE_BLOCK;
- do not apply fixes.

Any write, dirty worktree or SHA mismatch is an automatic BLOCK.
