"""A1/A6 reachability gate — CTO DIRECTIVE 033 §3 R1.

Rationale (owner ruling 2026-09-10): AUTOMATED_MULTI_AI_CTO_LOOP_V1 cannot settle
while runtime.advance() can neither trigger the cell nor apply its synthesis.
A1 ("a REAL failure automatically triggers the cell, no manual trigger anywhere")
and A6 ("the writer is redirected automatically and the product resumes at the
EXACT prior position") are properties of the PRODUCTION DISPATCH PATH, not of the
exercise harness. run_audit_cell_exercise() can never satisfy them.

This gate is deliberately STATIC and AST-based, not textual: a comment, a docstring
or a dead string literal naming the cell must not satisfy it. It answers exactly one
question — can the production dispatch module actually reach the cell? — and it fails
closed on any parse or import error.
"""
from __future__ import annotations
import ast
from pathlib import Path

# A1/A6 needs THREE distinct capabilities, not "any mention of the cell".
#
# The first version of this gate accepted ANY ONE of these symbols and therefore
# passed on a half-wired loop: Claude had wired execution and application but no
# trigger, so a real failure still created no request. A gate that accepts a
# disjunction where the requirement is a conjunction is a gate that certifies
# whichever third of the loop you happened to build.
_REQUIRED_CAPABILITIES = {
    # A1 in: a real failure must CREATE a cell request from the dispatch path.
    "trigger": {"persist_audit_cell_request"},
    # A1 out: the cell must actually RUN from the dispatch path.
    "execute": {"run_audit_cell"},
    # A6: the synthesis must be APPLIED back into state.
    # GLM F1: apply_audit_cell_result was a PHANTOM — it exists nowhere in the
    # codebase, so listing it could only ever weaken the gate. A gate may not name a
    # symbol that cannot be defined.
    "apply": {"_apply_pending_audit_cell_result", "mark_result_applied",
              "unapplied_result"},
}
_CELL_SYMBOLS = set().union(*_REQUIRED_CAPABILITIES.values())
_CELL_MODULES = {"audit_loop", "factory_v2.audit_loop"}


def audit_cell_reachable_from_runtime(control_root: Path) -> tuple[bool, str]:
    """(reachable, detail). Fails closed."""
    rt = Path(control_root) / "factory_v2" / "runtime.py"
    if not rt.is_file():
        return False, f"A1/A6: production dispatch module missing: {rt}"
    # FACTORY_SPLIT_ADVANCE_V1 (2026-09-13): the dispatch path = runtime.py + its
    # stage modules under factory_v2/advance/ (same loop, split into named steps).
    sources = [rt] + sorted((Path(control_root) / "factory_v2" / "advance").glob("*.py"))
    nodes = []
    for src in sources:
        try:
            nodes.extend(ast.walk(ast.parse(src.read_text(encoding="utf-8"), filename=str(src))))
        except SyntaxError as e:
            return False, f"A1/A6: {src.name} does not parse ({e}) — failing closed"

    imports, calls = set(), set()
    for node in nodes:
        if isinstance(node, ast.ImportFrom) and node.module:
            mod = node.module.rsplit(".", 1)[-1]
            if node.module in _CELL_MODULES or mod == "audit_loop":
                imports.update(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name in _CELL_MODULES:
                    imports.add(a.name)
        elif isinstance(node, ast.Call):
            f = node.func
            name = getattr(f, "id", None) or getattr(f, "attr", None)
            if name:
                calls.add(name)

    # L5_factory_v2_rest_1-001 (FACTORY_REPAIR_VERIFY_HARDENING_V1): an IMPORT is
    # not a capability. A runtime that imports the three symbols but never CALLS
    # any of them was declared connected — the gate must resolve CALL EXPRESSIONS,
    # exactly like _wiring_gate does. Imports stay in the detail message only.
    missing = {cap: syms for cap, syms in _REQUIRED_CAPABILITIES.items()
               if not (syms & calls)}
    if missing:
        detail = "; ".join(
            f"{cap} (needs one of {sorted(syms)})" for cap, syms in sorted(missing.items()))
        return False, (
            f"A1/A6 FAIL: factory_v2/runtime.py is missing {len(missing)} of the three "
            f"capabilities the loop requires — {detail}. "
            "A1 needs a real failure to CREATE a request AND the cell to RUN from the "
            "dispatch path; A6 needs the synthesis to be APPLIED back into state. "
            "A loop with only some of these is not autonomous: it either never starts, "
            "never runs, or never lands. The exercise harness cannot supply any of "
            "them — they are properties of the production dispatch path.")
    return True, (
        "A1/A6: all three capabilities present in the production dispatch path "
        + ", ".join(f"{cap}={sorted(syms & calls)}"
                    for cap, syms in sorted(_REQUIRED_CAPABILITIES.items())))
