"""Small static checks used by the rubric audit: unused imports, long functions, annotation coverage.

    python code/evaluation/lint_check.py [--strict]

Unused imports and unreferenced names are reported with their line; --strict exits non-zero if any are found.
"""

import ast
import sys
from collections.abc import Iterator
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]
SKIP = {"__pycache__", "suite", "cache"}


def py_files() -> list[Path]:
    return [p for p in sorted(CODE.rglob("*.py")) if not (set(p.relative_to(CODE).parts) & SKIP) and p.stat().st_size]


def annotation_names(tree: ast.AST) -> set[str]:
    """Names used inside quoted annotations ("EvidenceStore", "Graph | None"), which imports under TYPE_CHECKING serve."""
    found = set()
    annotations = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            annotations += [a.annotation for a in node.args.args + node.args.kwonlyargs if a.annotation] + [node.returns]
        elif isinstance(node, ast.AnnAssign):
            annotations.append(node.annotation)
    for ann in annotations:
        for sub in ast.walk(ann) if ann is not None else ():
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                try:
                    found |= {n.id for n in ast.walk(ast.parse(sub.value, mode="eval")) if isinstance(n, ast.Name)}
                except SyntaxError:
                    pass
    return found


def unused_imports(tree: ast.AST, source: str) -> list[tuple[str, int]]:
    imported = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                imported[(a.asname or a.name).split(".")[0]] = node.lineno
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                imported[a.asname or a.name] = node.lineno
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {n.value.id for n in ast.walk(tree)
                                                                          if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}
    names |= annotation_names(tree)
    exported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
            if isinstance(node.value, (ast.List, ast.Tuple, ast.Set)):
                exported = {e.value for e in node.value.elts if isinstance(e, ast.Constant)}
    lines = source.splitlines()
    return [(name, line) for name, line in imported.items()
            if name not in names and name not in exported and "noqa" not in lines[line - 1] and name != "annotations"]


def functions(tree: ast.AST) -> Iterator[ast.FunctionDef | ast.AsyncFunctionDef]:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def main() -> int:
    unused, long_fns, annotated, total = [], [], 0, 0
    for p in py_files():
        src = p.read_text(encoding="utf-8-sig")
        tree = ast.parse(src)
        rel = p.relative_to(CODE).as_posix()
        unused += [(rel, n, ln) for n, ln in unused_imports(tree, src)]
        for fn in functions(tree):
            size = (fn.end_lineno or fn.lineno) - fn.lineno + 1
            if size > 60:
                long_fns.append((rel, fn.name, size))
            if rel.startswith("audit/"):
                total += 1
                annotated += bool(fn.returns) or any(a.annotation for a in fn.args.args)
    print(f"files checked: {len(py_files())}")
    print(f"unused imports: {len(unused)}")
    for rel, n, ln in unused:
        print(f"  {rel}:{ln} {n}")
    print(f"functions over 60 lines: {len(long_fns)}")
    for rel, n, size in sorted(long_fns, key=lambda x: -x[2])[:12]:
        print(f"  {rel} {n} ({size} lines)")
    print(f"audit/ functions with any type annotation: {annotated}/{total}")
    return 1 if (unused and "--strict" in sys.argv) else 0


if __name__ == "__main__":
    sys.exit(main())
