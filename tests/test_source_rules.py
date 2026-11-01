"""Rules on the package source that hold on every machine."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_no_side_effect_inside_assert() -> None:
    """The far-mode recompile once sat inside an `assert`, so `python -O` stripped it and ran
    unrelated code (review of 4b): no `assert` in the JIT may compile or emit code."""
    import ast

    for path in (ROOT / "src/w2py/targets/jit").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Assert):
                calls = [c for c in ast.walk(node.test) if isinstance(c, ast.Call)]
                names = {getattr(c.func, "attr", getattr(c.func, "id", "")) for c in calls}
                assert not names & {"run", "compile", "emit", "finish", "patch"}, (
                    path,
                    node.lineno,
                )
