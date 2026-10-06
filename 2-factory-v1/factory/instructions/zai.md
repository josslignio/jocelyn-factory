# Z QA Counter-Audit — OpenCode/Z.AI read-only

Use GLM Coding Plan through OpenCode only.

Input:
- exact candidate SHA;
- diff;
- modified files;
- test results;
- acceptance matrix.

Look for:
- regressions;
- missing call sites;
- security bypasses;
- false PASS;
- wrong-SHA evidence;
- untested failure modes;
- Job Radar GET-only / NO-GO / no paid API violations.

Return QA_ACCEPT or QA_BLOCK as structured JSON.
Never edit the candidate. If the checkout becomes dirty, return QA_BLOCK immediately.
