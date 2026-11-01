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


def test_compiler_side_has_no_host_specific_imports() -> None:
    """MICRO-COMPILERS.md, "Rules for now": the reader, validator, one-pass core and backends stay
    free of ctypes, mmap and threads, and import nothing from the host side (execmem, instance,
    target), so they can be ported into a wasm micro-compiler."""
    import ast

    files = [*(ROOT / "src/w2py/frontend").glob("*.py")]
    files += [
        ROOT / "src/w2py/targets/jit/common" / f"{n}.py" for n in ("compiler", "backend", "context")
    ]
    files += [
        p
        for p in (ROOT / "src/w2py/targets/jit").glob("*/*.py")
        if p.stem in ("backend", "encoder", "abi")
    ]
    banned = {"ctypes", "mmap", "threading", "_thread"}
    host = {"execmem", "instance", "target"}  # the package's host side (loader, probe, selection)
    for path in files:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else []
            parts: set[str] = set()
            if isinstance(node, ast.ImportFrom):
                names = [node.module] if node.module else []
                parts = {a.name for a in node.names}
            for n in names:
                parts |= set(n.split("."))
            at = (path, getattr(node, "lineno", 0))
            assert not {n.split(".")[0] for n in names} & banned, at
            assert not parts & host, at
