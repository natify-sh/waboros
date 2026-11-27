"""Executable memory is W^X (R2, S0003): never writable and executable at once."""

import ctypes
import gc
import mmap
import os
import platform
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from tests.jit import native
from waboros.targets.base import TARGETS
from waboros.targets.jit.common import execmem
from waboros.targets.jit.common.execmem import ExecMem, Reservation, load
from waboros.targets.jit.common.target import JitTarget
from waboros.targets.jit.x86_64.encoder import R8, RAX, RCX, RDI, RDX, RSI, Asm

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
    for _ in range(5):  # a store and its instances form a cycle; PyPy finalizes late (LESSONS 49)
        gc.collect()
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
    real = getattr(execmem, name) or execmem._own_flush()  # musl, Android: our own  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage]
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
    (the D31 warning, `auto` keeps pyc). macOS only: Linux has our own routine (below)."""
    name = flush_name()
    if name == "_clear_cache":
        pytest.skip("Linux falls back to our own flush routine")
    monkeypatch.setattr(execmem, name, None)
    with pytest.raises(OSError, match="^no "):
        load(b"\x00" * 4)
    why = TARGETS["jit-aarch64"]().probe()
    assert why is not None and why.startswith("executable memory unavailable: OSError('no ")


def test_the_own_flush_routine_round_trips_through_llvm_mc() -> None:
    """S0004: `icache_flush` is ARMv8.0-A only (plus the `bti c` hint): llvm-mc with no feature
    but BTI decodes every word, and re-encodes the same bytes."""
    import re
    import shutil
    import subprocess

    from waboros.targets.jit.aarch64.abi import icache_flush
    from waboros.targets.jit.aarch64.encoder import Asm as A64

    mc = shutil.which("llvm-mc")
    if not mc:
        pytest.skip("no llvm-mc")
    a = A64()
    icache_flush(a)
    code = a.finish()
    run = [mc, "-triple=aarch64", "-mattr=+bti"]
    words = " ".join(f"0x{b:02x}" for b in code)
    out = subprocess.run([*run, "--disassemble"], input=words, capture_output=True, text=True)
    lines = [ln.strip() for ln in out.stdout.splitlines() if ln.strip()[:1] not in ("", ".")]
    assert not out.stderr and len(lines) == len(code) // 4, out.stderr
    ops = [re.sub(r"\s+", " ", ln.split("//")[0].strip()) for ln in lines]
    for want in ("bti c", "mrs x2, CTR_EL0", "tbnz w2, #28", "dc cvau, x4", "dsb ish",
                 "tbnz w2, #29", "ic ivau, x4", "isb", "ret"):  # fmt: skip
        assert any(op.startswith(want) for op in ops), (want, ops)
    back = subprocess.run([*run, "-show-encoding"], input="\n".join(lines),
                          capture_output=True, text=True).stdout  # fmt: skip
    encs = re.findall(r"encoding: \[([^\]]*)\]", back)
    assert b"".join(bytes(int(x, 16) for x in e.split(",")) for e in encs) == code


def test_the_own_flush_takes_every_path_of_ctr_el0() -> None:
    """Each CTR_EL0 case (clean and invalidate, either skipped by IDC/DIC, both) and line sizes
    of 16 to 128 bytes run over an unaligned range and return: the `mrs` is replaced by a `nop`,
    so the value comes in x2, the third argument."""
    if flush_name() != "_clear_cache":
        pytest.skip("Linux only")
    from waboros.targets.jit.aarch64.abi import MRS_CTR_EL0, icache_flush
    from waboros.targets.jit.aarch64.encoder import Asm as A64

    a = A64()
    icache_flush(a)
    code = a.finish()
    at = code.index((MRS_CTR_EL0 | 2).to_bytes(4, "little"))
    mem = load(code[:at] + (0xD503201F).to_bytes(4, "little") + code[at + 4 :])
    f = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint64)(mem.addr)
    buf = (ctypes.c_uint8 * 8192)()
    lo = ctypes.addressof(buf)
    for idc, dic in ((0, 0), (1, 0), (0, 1), (1, 1)):
        for d, i in ((2, 2), (4, 4), (5, 3)):  # log2 of words: 16, 64, 128 bytes
            f(lo + 3, lo + 8000, idc << 28 | dic << 29 | d << 16 | i)
    mem.free()


def test_code_runs_through_the_own_flush(monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux aarch64 without `__clear_cache` (musl without libgcc, Android): `load` goes through
    our own routine, and the code runs, on this thread and on one that never ran the area's
    earlier code; a wasm module runs end to end. This does not show that the routine flushes:
    every `load` maps a fresh page, which the kernel makes coherent itself when it becomes
    executable (see the rewrite test below)."""
    import threading

    if flush_name() != "_clear_cache":
        pytest.skip("Linux only")
    monkeypatch.setattr(execmem, "_clear_cache", None)
    from waboros.targets.jit.aarch64.encoder import Asm as A64

    def ret(n: int) -> bytes:
        a = A64()
        a.bti("c")
        a.mov_imm(0, n, wide=False)
        a.ret()
        return a.finish()

    got: list[int] = []
    for n in range(1, 41):  # unmapped and mapped again: usually the same address
        mem = load(ret(n))
        f: Any = ctypes.CFUNCTYPE(ctypes.c_int)(mem.addr)
        got.append(f())
        t = threading.Thread(target=lambda: got.append(f()))  # noqa: B023 (joined in the loop)
        t.start()
        t.join()
        mem.free()
    assert got == [n for n in range(1, 41) for _ in (0, 1)]
    assert execmem._own  # the routine was built and used  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage]
    from tests.jit.test_compiler import build

    monkeypatch.setenv("WABOROS_JIT_BOUNDS", "explicit")
    # (module (memory 1) (data (i32.const 8) "\2a")
    #   (func (export "f") (param i32) (result i32) (i32.load8_u (local.get 0))))
    wasm = bytes.fromhex(
        "0061736d01000000" "01060160017f017f" "03020100" "0503010001" "07050101660000"
        "0a090107002000" "2d00000b" "0b07010041080b012a"
    )  # fmt: skip
    assert build(wasm, "jit-aarch64")("f", 8) == [42]


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


def _cycle_garbage() -> list[bool]:
    """Unreachable cycle whose finalizer sets a flag: stands for a dropped store (LESSONS 135)."""
    freed = [False]

    class Cell:
        def __init__(self) -> None:
            self.me = self

        def __del__(self) -> None:
            freed[0] = True

    Cell()
    return freed


@pytest.mark.skipif(sys.platform == "emscripten", reason="no executable memory (Pyodide)")
@pytest.mark.parametrize("what", ["alloc", "reserve", "commit"])
def test_out_of_memory_collects_and_retries_once(
    what: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reservation or commit that fails for lack of memory collects dropped stores and retries
    once (ROADMAP "Memory of dropped stores" (a)); a second failure propagates unchanged."""
    gc.disable()  # only the retry may collect here
    try:
        freed = _cycle_garbage()
        real = getattr(execmem, f"_{what}")
        calls: list[bool] = []

        def flaky(*args: Any) -> Any:
            calls.append(freed[0])
            if len(calls) == 1:
                raise OSError(12, "no memory")  # ENOMEM
            return real(*args)

        monkeypatch.setattr(execmem, f"_{what}", flaky)
        if what == "alloc":
            ExecMem(1).free()
        else:
            execmem.Reservation(1 << 20, 1 << 16).free()
        assert calls == [False, True]  # the second call came after the collection

        calls.clear()

        def fail(*args: Any) -> Any:
            raise MemoryError("x")

        monkeypatch.setattr(execmem, f"_{what}", fail)
        with pytest.raises(MemoryError):
            execmem.Reservation(1 << 20, 1 << 16) if what != "alloc" else ExecMem(1)
    finally:
        gc.enable()


def test_other_errors_are_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def deny(size: int) -> int:
        calls.append(size)
        raise OSError(13, "denied")  # EACCES

    monkeypatch.setattr(execmem, "_alloc", deny)
    with pytest.raises(OSError):
        ExecMem(1)
    assert len(calls) == 1


_LIMITED = """
import gc, resource, sys
from waboros.targets.jit.common import execmem
# the limit counts from what this interpreter already uses: a free-threaded build's allocator
# (mimalloc) reserves so much that it cannot import ctypes under a fixed 1 GiB limit
used = next(int(l.split()[1]) for l in open("/proc/self/status") if l.startswith("VmData:")) << 10
resource.setrlimit(resource.RLIMIT_DATA, (used + (640 << 20), resource.RLIM_INFINITY))
gc.disable()  # as a free-threaded build, which collects rarely (LESSONS 135)
if sys.argv[1] == "off":
    execmem._retry = lambda f, *a: f(*a)
class Dropped:
    def __init__(self):
        self.res, self.me = execmem.Reservation(1 << 28, 1 << 28), self
for _ in range(40):
    Dropped()
"""


@pytest.mark.skipif(not hasattr(os, "fork") or platform.system() != "Linux", reason="RLIMIT_DATA")
def test_dropped_stores_do_not_exhaust_the_data_limit() -> None:
    """40 dropped cycles of 256 MiB committed under RLIMIT_DATA of what the interpreter uses plus
    640 MiB: fails with the retry switched off, passes with it."""
    import subprocess
    import sys

    def run(arg: str) -> int:
        return subprocess.run([sys.executable, "-c", _LIMITED, arg], capture_output=True).returncode

    assert run("off") != 0
    assert run("on") == 0


def test_sizes_round_to_a_16_kib_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """Android 15+ may run with 16 KiB pages. This host may have 4 KiB ones: every size here is a
    multiple of both, so the real mmap/mprotect calls succeed, and the test covers our rounding and
    alignment only (the 16 KiB kernel itself is the Android cell of platforms.yml)."""
    usable()
    monkeypatch.setattr(mmap, "PAGESIZE", 16384)
    assert [ExecMem(n).size for n in (1, 16384, 16385)] == [16384, 16384, 32768]
    mem = ExecMem(1)
    mem.write(b"\xc3" * 4)  # only the permissions are used here: no call
    mem.seal()
    res = Reservation(1 << 20)
    res.commit(1)
    assert res.committed == 16384
    res.commit(16385)
    assert res.committed == 32768
    with pytest.raises(ValueError):
        res.commit_range(4096, 16384)
    res.commit_range(65536, 81920)
    ctypes.memmove(res.addr + 65536, b"\x01", 1)
    assert ctypes.string_at(res.addr + 81920 - 1, 1) == b"\0"
    res.free()


def test_the_own_flush_makes_rewritten_code_visible(monkeypatch: pytest.MonkeyPatch) -> None:
    """One page run, made writable again, rewritten, made executable again and flushed by our
    own routine only: the kernel does not sync it this time (its PG_dcache_clean is set since
    the first time), so a stale instruction cache would run the old code. Best-effort evidence:
    under QEMU (no I-cache model) it proves nothing; natively a no-op flush can fail it, but
    whether stale lines are hit is a matter of timing and of the core (CTR_EL0.DIC/IDC make the
    caches coherent by themselves). The routine's correctness rests on its structure (llvm-mc
    above), the CTR_EL0 path test and the reading against compiler-rt's `__clear_cache`."""
    if flush_name() != "_clear_cache":
        pytest.skip("Linux only")
    monkeypatch.setattr(execmem, "_clear_cache", None)
    from waboros.targets.jit.aarch64.encoder import Asm as A64

    flush = execmem._own_flush()  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage]
    mem = ExecMem(4096)
    f = ctypes.CFUNCTYPE(ctypes.c_int)(mem.addr)
    for n in range(1, 201):
        a = A64()
        a.bti("c")
        a.mov_imm(0, n, wide=False)
        a.ret()
        if n > 1:
            execmem._commit(mem.addr, mem.size)  # read-write, not executable  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage]
        mem.write(a.finish())
        execmem._protect(mem.addr, mem.size)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage]
        flush(mem.addr, mem.addr + mem.size)
        assert f() == n
    mem.free()
