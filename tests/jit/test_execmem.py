"""Executable memory is W^X (R2, S0003): never writable and executable at once."""

import ctypes
import gc
import os
import platform
from pathlib import Path
from typing import Any, cast

import pytest

from tests.jit import native
from w2py.targets.base import TARGETS
from w2py.targets.jit.common import execmem
from w2py.targets.jit.common.execmem import ExecMem, load
from w2py.targets.jit.common.target import JitTarget
from w2py.targets.jit.x86_64.encoder import R8, RAX, RCX, RDI, RDX, RSI, Asm

MAPS = Path("/proc/self/maps")


def usable() -> None:
    native("jit-x86_64")


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
    """On this machine's JIT architecture (x86-64, aarch64: also flushed and, with BTI, PROT_BTI,
    which /proc/self/maps does not show)."""
    code = None
    for name in ("jit-x86_64", "jit-aarch64"):
        t = TARGETS[name]()
        assert isinstance(t, JitTarget)
        if platform.machine().lower() in t.machines:
            native(name)
            code = t.probe_code()
    if code is None:
        pytest.skip("no JIT target for this machine")
    mem = ExecMem(10)
    addr, size = mem.addr, mem.size
    rw, rx = ("rw-p", "r-xp") if MAPS.exists() else (None, None)
    assert perms(addr, size) == rw
    mem.write(code)
    assert perms(addr, size) == rw
    mem.seal()
    assert perms(addr, size) == rx
    with pytest.raises(ValueError):
        mem.write(b"\xc3")
    assert ctypes.CFUNCTYPE(ctypes.c_int)(mem.addr)() == 42
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


def flush_name() -> str:
    """The aarch64 flush function of this OS that a test can replace (skips elsewhere)."""
    native("jit-aarch64")
    name = next((n for n in ("_clear_cache", "_icache") if hasattr(execmem, n)), None)
    if name is None:
        pytest.skip("no replaceable flush function on this OS (Windows: FlushInstructionCache)")
    return name


def test_flush_covers_the_written_range_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The instruction cache is invalidated once, after the code is in place, over the whole
    mapped area (S0004)."""
    name = flush_name()
    real = getattr(execmem, name)
    seen: list[tuple[int, int, bytes]] = []
    target = TARGETS["jit-aarch64"]()
    assert isinstance(target, JitTarget)
    code = target.probe_code()

    def spy(addr: int, arg: int) -> None:
        seen.append((addr, arg, ctypes.string_at(addr, len(code))))
        real(addr, arg)

    monkeypatch.setattr(execmem, name, spy)
    mem = load(code)
    end = mem.addr + mem.size if name == "_clear_cache" else mem.size  # (start, end) / (addr, size)
    assert seen == [(mem.addr, end, code)]
    assert ctypes.CFUNCTYPE(ctypes.c_int)(mem.addr)() == 42
    mem.free()


def test_no_flush_function_means_no_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never run unflushed code: without the flush function `load` fails and so does the probe
    (the D31 warning, `auto` keeps pyc)."""
    name = flush_name()
    monkeypatch.setattr(execmem, name, None)
    with pytest.raises(OSError, match="^no "):
        load(b"\x00" * 4)
    why = TARGETS["jit-aarch64"]().probe()
    assert why is not None and why.startswith("executable memory unavailable: OSError('no ")
    if name == "_clear_cache":
        assert "instruction-cache flush function (__clear_cache)" in why


def test_write_protect_toggle_is_restored_when_the_copy_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """macOS arm64 MAP_JIT (S0004): writable for this thread only during the copy, executable
    again afterwards, also when the copy raises. The helper runs on every OS."""
    calls: list[int] = []
    buf = ctypes.create_string_buffer(4)
    execmem.write_toggled(calls.append, ctypes.addressof(buf), b"abcd")
    assert calls == [0, 1] and buf.raw == b"abcd"

    def boom(*args: object) -> None:
        raise MemoryError("copy")

    monkeypatch.setattr(ctypes, "memmove", boom)
    calls.clear()
    with pytest.raises(MemoryError):
        execmem.write_toggled(calls.append, ctypes.addressof(buf), b"wxyz")
    assert calls == [0, 1]
