"""AST scan: except handlers (and missing-file guards) whose body yields a benign value.

Prints file:line  kind  enclosing_function  value  — raw material for the inventory."""
import ast, sys, pathlib
ROOTS = ["factory_v2", "factory/bin", "factory/lib"]
BENIGN = {"None", "True", "False", "0", "[]", "{}", "''", '""', "()", "set()"}

def lit(node):
    try: return ast.unparse(node)
    except Exception: return "?"

def is_benign_return(stmt):
    if isinstance(stmt, ast.Return):
        v = "None" if stmt.value is None else lit(stmt.value)
        return v in BENIGN or v.startswith(("[]", "{}", "set()", "()"))
    return False

rows = []
for root in ROOTS:
    for p in sorted(pathlib.Path(root).rglob("*.py")):
        if "tests" in p.parts or p.name.startswith("test_"): continue
        try: tree = ast.parse(p.read_text(encoding="utf-8"))
        except Exception as e: rows.append((str(p), 0, "UNPARSEABLE", "-", str(e))); continue
        func_stack = []
        class V(ast.NodeVisitor):
            def visit_FunctionDef(self, n):
                func_stack.append(n.name); self.generic_visit(n); func_stack.pop()
            visit_AsyncFunctionDef = visit_FunctionDef
            def visit_ExceptHandler(self, n):
                body = [s for s in n.body if not isinstance(s, ast.Expr) or not isinstance(getattr(s, "value", None), ast.Constant)]
                fn = func_stack[-1] if func_stack else "<module>"
                exc = lit(n.type) if n.type else "BARE"
                if len(body) == 1 and isinstance(body[0], ast.Pass):
                    rows.append((str(p), n.lineno, "EXCEPT_PASS", fn, exc))
                elif len(body) == 1 and isinstance(body[0], ast.Continue):
                    rows.append((str(p), n.lineno, "EXCEPT_CONTINUE", fn, exc))
                elif len(body) == 1 and is_benign_return(body[0]):
                    rows.append((str(p), n.lineno, "EXCEPT_RETURN", fn, f"{exc} -> {lit(body[0].value) if body[0].value is not None else 'None'}"))
                elif body and is_benign_return(body[-1]) and all(isinstance(s, (ast.Expr, ast.Assign)) for s in body[:-1]):
                    rows.append((str(p), n.lineno, "EXCEPT_LOG_RETURN", fn, f"{exc} -> {lit(body[-1].value) if body[-1].value is not None else 'None'}"))
                self.generic_visit(n)
            def visit_If(self, n):
                # `if not X.exists()/is_file(): return <benign>` guards
                t = lit(n.test)
                if ("exists()" in t or "is_file()" in t or "is_dir()" in t) and t.startswith("not ") and len(n.body) == 1 and is_benign_return(n.body[0]):
                    fn = func_stack[-1] if func_stack else "<module>"
                    rows.append((str(p), n.lineno, "MISSING_FILE_RETURN", fn, f"{t} -> {lit(n.body[0].value) if n.body[0].value is not None else 'None'}"))
                self.generic_visit(n)
        V().visit(tree)
for r in rows:
    print("\t".join(str(x) for x in r))
print(f"# total {len(rows)}", file=sys.stderr)
