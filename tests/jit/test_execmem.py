"""Executable memory is W^X (R2, S0003): never writable and executable at once."""

import ctypes
import gc
import os
from pathlib import Path
from typing import Any, cast

import pytest

from tests.jit import need
from w2py.targets.base import TARGETS
from w2py.targets.jit.common.execmem import ExecMem, load
from w2py.targets.jit.x86_64.encoder import R8, RAX, RCX, RDI, RDX, RSI, Asm

MAPS = Path("/proc/self/maps")


def usable() -> None:
    need(TARGETS["jit-x86_64"]().probe() is None, "x86-64 executable memory")


def perms(lo: int, size: int) -> str | None:
    """The permissions of the mappings overlapping [lo, lo+size) (only ours: the interpreter, e.g.
    PyPy's JIT, may map RWX elsewhere); asserts none is writable and executable."""
    if not MAPS.exists():  # not Linux: the call below is the only check there
        return None
    found: set[str] = set()
    for line in MAPS.read_text().splitlines():
        rng, p = line.split()[:2]
        a, b = (int(x, 16) for x in rng.split("-"))
        if a < lo + size and lo < b:
            assert not ("w" in p and "x" in p), f"writable and executable: {line}"
            found.add(p)
    assert len(found) <= 1, found
    return found.pop() if found else None


def test_w_xor_x() -> None:
    usable()
    a = Asm()
    a.endbr64()
    a.mov_imm(RAX, 7)
    a.ret()
    mem = ExecMem(10)
    addr, size = mem.addr, mem.size
    rw, rx = ("rw-p", "r-xp") if MAPS.exists() else (None, None)
    assert perms(addr, size) == rw
    mem.write(a.finish())
    assert perms(addr, size) == rw
    mem.seal()
    assert perms(addr, size) == rx
    with pytest.raises(ValueError):
        mem.write(b"\xc3")
    assert ctypes.CFUNCTYPE(ctypes.c_int)(mem.addr)() == 7
    mem.free()
    assert perms(addr, size) is None


def test_generated_loop() -> None:
    """sum(a*b for i in range(n)) with a backward jcc: labels resolve to running code."""
    usable()
    n, x = {"nt": (RCX, RDX)}.get(os.name, (RDI, RSI))  # the host's C convention (Win64)
    a = Asm()
    a.endbr64()
    loop, done = a.label(), a.label()
    a.alu("xor", RAX, RAX)  # eax = 0 (64-bit result zero-extended)
    a.test(n, n, w=True)
    a.jcc("le", done, short=True)
    a.bind(loop)
    a.mov(R8, x, w=True)
    a.imul(R8, x, w=True)
    a.alu("add", RAX, R8, w=True)
    a.alu_imm("sub", n, 1, w=True)
    a.jcc("ne", loop)
    a.bind(done)
    a.ret()
    mem = load(a.finish())
    try:
        f = ctypes.CFUNCTYPE(ctypes.c_int64, ctypes.c_int64, ctypes.c_int64)(mem.addr)
        assert f(10, 3) == 90
        assert f(0, 3) == 0
        assert f(1000, -(1 << 20)) == 1000 << 40
    finally:
        mem.free()


def test_code_areas_are_unmapped_with_their_instances() -> None:
    """A dropped store's instances unmap their code areas (before, each stayed mapped: +4 KiB of
    address space per instance, never returned)."""
    usable()
    from tests.jit.test_compiler import build, wat

    src = wat('(module (memory 1) (func (export "f") (result i32) (i32.const 1)))')
    released: list[Any] = []
    for _ in range(50):
        i = build(src)
        assert i("f") == [1]
        released.append(cast(Any, i.inst).code._release)  # the finalizer that unmaps it
        del i
    gc.collect()  # a store and its instances form a cycle
    assert not any(r.alive for r in released)
