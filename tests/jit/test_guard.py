# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
"""Guard pages on the JIT of this machine (S0006 steps 1-4: Linux glibc and musl, macOS, Windows;
jit-x86_64 and jit-aarch64; aarch64 also under QEMU, tools/run_arm64.py): accesses the
reservation's guard catches go unchecked and the emitted fault handler turns their faults into
MEMORY_OUT_OF_BOUNDS at the function's own trap stub. Every trapping access kind at the edge of
memory, scalar and SIMD, on memory 0, memory n and an imported memory; deep frames, inner
entries, threads; and faults that are not ours, which must behave exactly as without the handler
(in subprocesses: the handler stays for the process's lifetime). Windows has a vectored
handler instead of a signal chain: the chain tests are POSIX-only, and the cases that break a
chain must not break it there."""

import io
import os
import subprocess
import sys
import threading
import warnings
from pathlib import Path
from typing import Any, cast

import pytest

from tests.jit import native, need
from tests.jit.test_compiler import JIT, Inst, build, outcome, trap_of, wat
from tests.jit.test_tables import provider
from waboros.frontend.binary import parse
from waboros.frontend.validate import validate
from waboros.runtime.store import StoreState
from waboros.targets import base
from waboros.targets.base import Trap, TrapCode
from waboros.targets.jit.common import guard
from waboros.targets.jit.common.compiler import Compiled

ROOT = Path(__file__).resolve().parents[2]
WIN = sys.platform == "win32"
# signal chains: Windows has a vectored handler list, which nothing can reorder behind us
_POSIX_ONLY = pytest.mark.skipif(WIN, reason="a POSIX signal chain (S0006 step 4)")
OOB = TrapCode.MEMORY_OUT_OF_BOUNDS
PAGE = 65536
SIZES = {"8": 1, "16": 2, "32": 4, "64": 8}
LOADS = {"i32.load": 4, "i64.load": 8, "f32.load": 4, "f64.load": 8, "i32.load8_s": 1,
         "i32.load8_u": 1, "i32.load16_s": 2, "i32.load16_u": 2, "i64.load8_s": 1,
         "i64.load8_u": 1, "i64.load16_s": 2, "i64.load16_u": 2, "i64.load32_s": 4,
         "i64.load32_u": 4}  # fmt: skip
STORES = {"i32.store": 4, "i64.store": 8, "f32.store": 4, "f64.store": 8, "i32.store8": 1,
          "i32.store16": 2, "i64.store8": 1, "i64.store16": 2, "i64.store32": 4}  # fmt: skip
V0 = "(v128.const i64x2 0 0)"
SIMD = {  # name -> (access size, expression of the address `A`, result is v128)
    "v128.load": (16, "(v128.load A)"),
    **{f"v128.load{k}": (8, f"(v128.load{k} A)") for k in
       ("8x8_s", "8x8_u", "16x4_s", "16x4_u", "32x2_s", "32x2_u")},
    **{f"v128.load{b}_splat": (n, f"(v128.load{b}_splat A)") for b, n in SIZES.items()},
    **{f"v128.load{b}_zero": (n, f"(v128.load{b}_zero A)") for b, n in SIZES.items() if n >= 4},
    **{f"v128.load{b}_lane": (n, f"(v128.load{b}_lane 1 A {V0})") for b, n in SIZES.items()},
    "v128.store": (16, f"(v128.store A {V0})"),
    **{f"v128.store{b}_lane": (n, f"(v128.store{b}_lane 1 A {V0})") for b, n in SIZES.items()},
}  # fmt: skip


@pytest.fixture(autouse=True)
def _guard(monkeypatch: pytest.MonkeyPatch) -> None:
    native(JIT)
    tgt = base.TARGETS[JIT]()
    if not sys.platform.startswith(("linux", "darwin", "win32")):
        pytest.skip(f"no guard pages on {sys.platform} yet")
    # under WABOROS_JIT_REQUIRE=1 (the CI `jit` and `arm64` jobs on Linux glibc, the `jit` job's
    # musl container, the macOS and Windows steps) a skip is a failure: the row check or `auto`
    # must not stop engaging unnoticed; `auto` only where the row is verified
    need(guard.host_row() in tgt.guard_rows, f"a guard-page handler for {guard.host_row()}")  # pyright: ignore[reportAttributeAccessIssue]
    if guard.host_row() in tgt.verified:  # pyright: ignore[reportAttributeAccessIssue]
        monkeypatch.setenv("WABOROS_JIT_BOUNDS", "auto")
        need(guard.wanted(tgt), f"`auto` giving guard pages on {guard.host_row()}")
    monkeypatch.setenv("WABOROS_JIT_BOUNDS", "guard")


def compiled(text: str) -> Compiled:
    mod = parse(wat(text))
    validate(mod)
    c = base.TARGETS[JIT]().compile(mod)
    assert isinstance(c, Compiled)
    return c


def _type(op: str) -> str:
    return op[:3]


def _scalar_module(memories: str) -> str:
    """Every scalar load and store on memory `$mx` (declared in `memories`), the address a
    parameter, and memory.grow of it."""
    out = [f"(module {memories}"]
    for op in LOADS:
        out.append(f'(func (export "{op}") (param i32) ({op} $mx (local.get 0)) drop)')
    for op in STORES:
        out.append(f'(func (export "{op}") (param i32) ({op} $mx (local.get 0) '
                   f"({_type(op)}.const 7)))")  # fmt: skip
    out.append('(func (export "grow") (param i32) (result i32) (memory.grow $mx (local.get 0)))')
    return "\n".join(out) + ")"


def _edges(size: int, n: int) -> list[tuple[int, bool]]:
    """(address, traps) around the end of a memory of `size` bytes for an n-byte access."""
    cases = [(0, False), (size - n, False), (size, True), (0xFFFFFFFF, True)]
    return cases + [(size - n + k, True) for k in range(1, n)]


@pytest.mark.parametrize("which", ["memory 0", "memory 1", "imported"])
def test_every_scalar_access_traps_at_the_edge(which: str) -> None:
    imports: dict[str, Any] = {}
    store = StoreState()
    if which == "imported":
        host = build(wat('(module (memory (export "m") 3))'), store=store)
        imports = {"h": provider(host)}
        text, size = _scalar_module('(import "h" "m" (memory $mx 1)) (memory $m1 2)'), 3 * PAGE
    elif which == "memory 0":
        text, size = _scalar_module("(memory $mx 1) (memory $m1 2)"), PAGE
    else:
        text, size = _scalar_module("(memory $m0 1) (memory $mx 2)"), 2 * PAGE
    c = compiled(text)
    assert c.flags == guard.FLAGS and len(c.faults) == len(LOADS) + len(STORES)
    inst = build(wat(text), imports=imports, store=store)
    for op, n in {**LOADS, **STORES}.items():
        for addr, traps in _edges(size, n):
            got = outcome(inst, op, addr)
            assert got == (OOB if traps else []), (which, op, hex(addr), got)
    # memory.grow within the reservation: the new pages work, past them still traps
    assert inst("grow", 1) == [size // PAGE]
    for op, n in {**LOADS, **STORES}.items():
        assert outcome(inst, op, size) == [], op
        assert outcome(inst, op, size + PAGE - n + 1) == OOB, op


def test_simd_accesses_trap_at_the_edge_including_16_bytes() -> None:
    funcs = []
    for i, (name, (_, e)) in enumerate(SIMD.items()):
        for mx in ("0", "1"):
            body = e.replace("A", "(local.get 0)").replace(f"({name}", f"({name} {mx}", 1)
            drop = "" if "store" in name else " drop"
            funcs.append(f'(func (export "{i}/{mx}") (param i32) {body}{drop})')
    text = "(module (memory 1) (memory 2)\n" + "\n".join(funcs) + ")"
    c = compiled(text)
    assert len(c.faults) == 2 * len(SIMD)
    inst = build(wat(text))
    for i, (name, (n, _)) in enumerate(SIMD.items()):
        for mx, size in (("0", PAGE), ("1", 2 * PAGE)):
            for addr, traps in _edges(size, n):
                got = outcome(inst, f"{i}/{mx}", addr)
                assert got == (OOB if traps else []), (name, mx, hex(addr), got)


def test_constant_addresses_and_the_guard_threshold() -> None:
    g = guard.GUARD
    text = f"""(module (memory 1)
      (func (export "k_last") (result i32) (i32.load (i32.const {PAGE - 4})))
      (func (export "k_straddle") (result i32) (i32.load (i32.const {PAGE - 3})))
      (func (export "k_top") (result i32) (i32.load offset={g - 8} (i32.const 0xFFFFFFFF)))
      (func (export "k_over") (result i32) (i32.load offset={g - 2} (i32.const 0xFFFFFFFF)))
      (func (export "unchecked") (param i32) (result i32) (i32.load offset={g - 4} (local.get 0)))
      (func (export "checked") (param i32) (result i32) (i32.load offset={g - 3} (local.get 0)))
      (func (export "v_unchecked") (param i32) (v128.store offset={g - 16} (local.get 0) {V0}))
      (func (export "v_checked") (param i32) (v128.store offset={g - 15} (local.get 0) {V0})))"""
    # unchecked: k_last, k_straddle, k_top (c + off + n = 2**32 + GUARD - 3), unchecked, v_unchecked
    assert len(compiled(text).faults) == 5
    inst = build(wat(text))
    assert inst("k_last") == [0]
    for name in ("k_straddle", "k_top", "k_over"):
        assert trap_of(inst, name) is OOB
    for name in ("unchecked", "checked", "v_unchecked", "v_checked"):
        for addr in (0, PAGE, 0xFFFFFFFF):
            assert trap_of(inst, name, addr) is OOB


LOAD = '(module (memory 1) (func (export "f") (param i32) (result i32) (i32.load (local.get 0))))'


def test_the_modes_and_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """D44: `auto` is the default (unset or empty) and gives guard on a verified row (explicit
    on a row with a handler that CI has not verified yet); `explicit` opts out; anything else is
    an error."""
    verified = guard.host_row() in base.TARGETS[JIT]().verified  # pyright: ignore[reportAttributeAccessIssue]
    for m in (None, "", "auto", "guard"):
        if m is None:
            monkeypatch.delenv("WABOROS_JIT_BOUNDS")
        else:
            monkeypatch.setenv("WABOROS_JIT_BOUNDS", m)
        assert guard.mode() == (m or "auto")
        c = compiled(LOAD)
        assert bool(c.faults) == (c.flags == guard.FLAGS) == (verified or m == "guard"), m
    monkeypatch.setenv("WABOROS_JIT_BOUNDS", "explicit")
    assert (compiled(LOAD).flags, compiled(LOAD).faults) == (0, [])
    monkeypatch.setenv("WABOROS_JIT_BOUNDS", "fast")
    with pytest.raises(ValueError, match="WABOROS_JIT_BOUNDS"):
        compiled(LOAD)


@pytest.mark.parametrize(
    "why", ["unverified row", "unknown libc", "install fails", "foreign process"]
)
def test_auto_is_explicit_silently_where_guard_cannot_be(
    why: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S0006 "Selection": an unverified row, an unnamed libc, a handler that does not install, a
    process of another architecture (cross-compiling): `auto` compiles explicit code, with no
    warning, and it runs; `guard` there compiles but refuses to instantiate."""
    monkeypatch.setenv("WABOROS_JIT_BOUNDS", "auto")
    tgt = type(base.TARGETS[JIT]())
    if why == "unverified row":
        monkeypatch.setattr(tgt, "verified", frozenset())
    elif why == "unknown libc":
        monkeypatch.setattr(guard, "host_row", lambda: "linux-unknown")
    else:  # as if the handler were not installed yet in this process
        monkeypatch.setattr(guard, "_handler", {})
        if why == "install fails":
            monkeypatch.setattr(guard, "load", _no_exec)
        else:
            monkeypatch.setattr(guard.platform, "machine", lambda: "riscv64")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        c = compiled(LOAD)
        assert (c.flags, c.faults) == (0, [])
        assert trap_of(build(wat(LOAD)), "f", 0xFFFFFFFF) is OOB
    if why in ("install fails", "foreign process"):
        assert not guard._handler  # nothing half-installed  # pyright: ignore[reportPrivateUsage]
        monkeypatch.setenv("WABOROS_JIT_BOUNDS", "guard")
        with pytest.raises((OSError, RuntimeError)):
            build(wat(LOAD))


# /proc/self/maps lines of CPython on Alpine 3.24 (x86-64; aarch64 differs in the name only)
# and on Ubuntu 24.04
MUSL_MAPS = """\
5581a3c4e000-5581a3c4f000 r--p 00000000 00:2e 1867 /usr/local/bin/python3.13
7f0de1a2b000-7f0de1a3f000 r--p 00000000 00:2e 1871 /usr/local/lib/libpython3.13.so.1.0
7f0de1d1e000-7f0de1d32000 r--p 00000000 00:2e 512 /lib/ld-musl-x86_64.so.1
7ffd24d47000-7ffd24d67000 rw-p 00000000 00:00 0 [stack]
"""
GLIBC_MAPS = """\
7f3a1c400000-7f3a1c428000 r--p 00000000 08:01 3277 /usr/lib/x86_64-linux-gnu/libc.so.6
7f3a1c6f0000-7f3a1c6f2000 r--p 00000000 08:01 3274 /usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2
"""


def test_musl_is_named_by_its_mapped_image(monkeypatch: pytest.MonkeyPatch) -> None:
    """The musl row (S0006): `platform.libc_ver()` names glibc only; musl is the process's
    mapped `ld-musl-<arch>.so.1`. Fed with recorded maps and a Linux `sys.platform`, so it runs
    on any host (macOS included)."""
    monkeypatch.setattr(guard.sys, "platform", "linux")
    libc_of = guard._libc_of  # pyright: ignore[reportPrivateUsage]
    assert libc_of(MUSL_MAPS) == "musl"
    assert libc_of(MUSL_MAPS.replace("x86_64", "aarch64")) == "musl"
    assert libc_of(GLIBC_MAPS) == ""
    assert libc_of("7f0-7f1 r--p 0 00:00 0 [anon:ld-musl-x86_64.so.1]\n") == ""
    row = guard.host_row.__wrapped__  # uncached
    # libc_ver's byte scan of the executable may say `libc` (no version) on any libc
    for ver in (("", ""), ("libc", "")):
        monkeypatch.setattr(guard.platform, "libc_ver", _returns(ver))
        for maps, want in ((MUSL_MAPS, "linux-musl"), (GLIBC_MAPS, "linux-unknown")):
            monkeypatch.setattr(guard, "open", _reads(maps), raising=False)
            assert row() == want
        monkeypatch.setattr(guard, "open", _no_file, raising=False)
        assert row() == "linux-unknown"
    monkeypatch.setattr(guard.platform, "libc_ver", lambda: ("glibc", "2.39"))
    assert row() == "linux-glibc"


def _returns(value: object) -> Any:
    return lambda: value


def _reads(text: str) -> Any:
    return lambda *args, **kwargs: io.StringIO(text)  # pyright: ignore[reportUnknownLambdaType]


def _no_file(*args: object, **kwargs: object) -> Any:
    raise OSError("no /proc")


def _no_exec(code: bytes) -> Any:
    raise OSError("executable memory refused")


def test_guard_on_a_target_without_it_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = parse(wat("(module (memory 1))"))
    validate(mod)
    tgt = base.TARGETS[JIT]()
    monkeypatch.setattr(type(tgt), "guard_rows", frozenset())  # as a target without the mode
    with pytest.raises(ValueError, match="no guard mode"):
        tgt.compile(mod)


def test_auto_cross_compiles_explicit_with_our_handler_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other architecture's target (jit_dump, D35) gets explicit code under `auto` although
    this process's handler is installed: that handler is not its code (LESSONS 48)."""
    build(wat(LOAD))  # our handler installed
    other = next(n for n in ("jit-x86_64", "jit-aarch64") if n != JIT)
    monkeypatch.setenv("WABOROS_JIT_BOUNDS", "auto")
    mod = parse(wat(LOAD))
    validate(mod)
    c = base.TARGETS[other]().compile(mod)
    assert isinstance(c, Compiled) and (c.flags, c.faults) == (0, [])


RECURSE = """(module (memory 1)
  (func $f (export "f") (param i32 i32) (result i32)
    (if (result i32) (i32.eqz (local.get 0))
      (then (i32.load (local.get 1)))
      (else (i32.add (i32.const 1) (call $f (i32.sub (local.get 0) (i32.const 1)) (local.get 1))))))
  (func $inf (export "inf") (param i32) (result i32) (i32.load (call $inf (local.get 0)))))"""


@pytest.mark.parametrize("around", [False, True])
def test_far_mode_drops_the_fault_points_of_the_first_attempt(
    around: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """aarch64 compiles a function again in far mode (S0004) after `rewind`: the fault points of
    the first attempt go with its code (S0006), so the map holds each access once, sorted, and a
    fault in the far code still resumes at its stub. 80 000 loads of 16 bytes each need far mode
    in guard code (without checks: 28 bytes each in explicit code)."""
    if JIT != "jit-aarch64":
        pytest.skip("x86-64 has no far mode")
    from tests.jit.test_compiler_aarch64 import _module  # pyright: ignore[reportPrivateUsage]
    from waboros.targets.jit.aarch64.backend import Backend

    rewinds: list[int] = []
    rewind = Backend.rewind

    def counted(self: Backend, pos: int) -> None:
        rewinds.append(pos)
        rewind(self, pos)

    monkeypatch.setattr(Backend, "rewind", counted)
    n = 80_000
    mod = parse(_module(n, around))
    validate(mod)
    c = base.TARGETS[JIT]().compile(mod)
    assert isinstance(c, Compiled) and len(rewinds) == 1 and (rewinds[0] > 0) == around
    at = [p for p, _ in c.faults]
    assert len(at) == n and at == sorted(set(at)) and at[-1] < c.data_start
    inst = build(_module(n, around))
    assert inst("f", 1, 7) == [7]  # the far br_if
    assert trap_of(inst, "f", 0, 30_000) is OOB  # 35 536 loads, then the guard
    if around:
        assert inst("g", 0, 4) == [5]


def test_trap_deep_in_recursion_and_stack_overflow() -> None:
    inst = build(wat(RECURSE))
    assert inst("f", 10_000, 0) == [10_000]
    assert trap_of(inst, "f", 10_000, PAGE) is OOB
    assert trap_of(inst, "inf", 0) is TrapCode.STACK_OVERFLOW
    assert inst("f", 100, 8) == [100]  # still usable


def test_trap_in_an_inner_entry_leaves_the_outer_call_running() -> None:
    text = """(module (import "h" "cb" (func $cb (param i32) (result i32))) (memory 1)
      (func (export "inner") (param i32) (result i32) (i32.load (local.get 0)))
      (func (export "outer") (param i32) (result i32)
        (i32.add (call $cb (local.get 0)) (i32.load (i32.const 0)))))"""
    seen: list[Any] = []
    box: dict[str, Inst] = {}

    def cb(args: Any) -> list[int]:
        try:
            box["i"]("inner", args[0])
        except Trap as e:
            seen.append(e.code)
            return [40]
        return [1]

    box["i"] = inst = build(wat(text), imports={"h": {"cb": cb}})
    assert inst("outer", PAGE) == [40] and seen == [OOB]
    assert inst("outer", 0) == [1]


def test_threads_with_one_store_each_trap_in_a_loop() -> None:
    text = """(module (memory 1)
      (func (export "f") (param i32) (result i32) (i32.load (local.get 0))))"""
    wasm, errors = wat(text), []

    def work() -> None:
        try:
            inst = build(wasm, store=StoreState())
            for k in range(300):
                assert trap_of(inst, "f", PAGE + k) is OOB
                assert inst("f", 4 * (k % 100)) == [0]
        except BaseException as e:  # reported by the main thread
            errors.append(e)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors


def test_registry_grows_and_forgets_freed_code_and_memories() -> None:
    """More code areas and reservations than the first tables hold (16), some freed on the way:
    every live instance still traps, a freed one leaves no entry behind."""
    import gc

    wasm = wat(
        '(module (memory 1) (func (export "f") (param i32) (result i32) (i32.load (local.get 0))))'
    )
    reg = guard._registry  # pyright: ignore[reportPrivateUsage]

    def live() -> set[int]:  # `lo` of every live code entry, in the published table
        t = reg.tables[0][-1]
        return {t[1 + 4 * k] for k in range(t[0]) if t[1 + 4 * k + 1]}

    keep = [build(wasm, store=StoreState()) for _ in range(40)]
    freed = {cast(Any, i.inst).code.addr for i in keep[5:30]}
    assert freed <= live()
    del keep[5:30]
    for _ in range(5):  # PyPy runs the finalizers only on the third collection (measured)
        gc.collect()
    assert not freed & live()
    keep += [build(wasm, store=StoreState()) for _ in range(30)]
    for inst in keep:
        assert trap_of(inst, "f", PAGE) is OOB and inst("f", 0) == [0]


def test_a_straddling_store_writes_nothing() -> None:
    """One instruction per store (S0006): a store that crosses into the guard faults before any
    byte of it lands. This holds on native x86-64, whose faults are precise; an emulator that
    splits a wide store (QEMU's `-cpu qemu64`) may tear it, as Arm may (accepted, D45). On
    aarch64 the architecture allows the accessible part to be written, so there the test records
    (a warning naming the torn stores) rather than asserts; a byte written is still the store's
    own (S0006 step 2: QEMU 8.2.2 tore only the 16-byte store, LESSONS 54)."""
    inst = build(
        wat("""(module (memory 1)
      (func (export "s8") (param i32) (i64.store (local.get 0) (i64.const -1)))
      (func (export "s16") (param i32) (v128.store (local.get 0) (v128.const i64x2 -1 -1)))
      (func (export "sl") (param i32) (v128.store64_lane 0 (local.get 0) (v128.const i64x2 -1 -1)))
      (func (export "ld") (param i32) (result i32) (i32.load8_u (local.get 0)))
      (func (export "st") (param i32) (i32.store8 (local.get 0) (i32.const 0))))""")
    )
    torn = []
    for f, n in (("s8", 8), ("s16", 16), ("sl", 8)):
        for k in range(1, n):
            a = PAGE - n + k
            assert trap_of(inst, f, a) is OOB
            got = [inst("ld", b)[0] for b in range(a, PAGE)]
            if any(got):  # recorded on aarch64 only; even then only the store's own bytes
                assert JIT == "jit-aarch64" and set(got) <= {0, 0xFF}, (f, a, got)
                torn.append((f, a))
                for b in range(a, PAGE):  # clean for the next case
                    inst("st", b)
    if torn:
        msg = f"aarch64: a straddling store wrote part of itself (D45): {torn}"
        warnings.warn(msg, stacklevel=1)


def _memory_instructions(code: bytes, end: int, tmp: Path) -> list[int]:
    """Offsets of the instructions that address linear memory: x86-64, a memory operand based on
    r13 (memory 0) or rcx (another memory's base plus the address), `lea` excepted; aarch64
    (llvm-mc), a load or store based on x26 (memory 0) or x11 (another memory's base)."""
    import re

    if JIT == "jit-aarch64":
        return _a64_memory_instructions(code[:end])
    from tests.jit.test_encoder import OBJDUMP

    need(OBJDUMP, "GNU objdump")
    (tmp / "code.bin").write_bytes(code[:end])
    cmd = [str(OBJDUMP), "-D", "-b", "binary", "-m", "i386:x86-64", "-M", "intel"]
    out = subprocess.run([*cmd, str(tmp / "code.bin")], capture_output=True, text=True).stdout
    found = []
    for line in out.splitlines():
        m = re.match(r"\s*([0-9a-f]+):\t[0-9a-f ]+\t(\S+)\s+(.*)", line)
        if m and m.group(2) != "lea" and re.search(r"\[(r13|rcx)\b", m.group(3)):
            found.append(int(m.group(1), 16))
    return found


def _a64_memory_instructions(code: bytes) -> list[int]:
    import re
    import shutil

    mc = shutil.which("llvm-mc")
    need(mc, "llvm-mc")
    words = " ".join(f"0x{b:02x}" for b in code)
    cmd = [str(mc), "-triple=aarch64", "-mattr=+bti", "--disassemble"]
    out = subprocess.run(cmd, input=words, capture_output=True, text=True, check=True).stdout
    lines = [ln.strip() for ln in out.splitlines() if ln.strip() and not ln.strip().startswith(".")]
    assert len(lines) == len(code) // 4, "one line per instruction"
    return [4 * i for i, ln in enumerate(lines) if re.match(r"(ld|st)\S*\s.*\[x(26|11)\b", ln)]


def test_the_fault_map_is_the_instructions_that_touch_memory(tmp_path: Path) -> None:
    """S0006 test plan: objdump finds exactly the fault points, scalar and SIMD, memory 0 and n."""
    simd = [f'(func (export "{i}") (param i32) {e.replace("A", "(local.get 0)")}'
            f'{"" if "store" in name else " drop"})'
            for i, (name, (_, e)) in enumerate(SIMD.items())]  # fmt: skip
    for text in (
        _scalar_module("(memory $mx 1)"),
        _scalar_module("(memory $m0 1) (memory $mx 2)"),
        "(module (memory 1)\n" + "\n".join(simd) + ")",
    ):
        c = compiled(text)
        assert c.faults and [p for p, _ in c.faults] == _memory_instructions(
            c.code, c.data_start, tmp_path
        )


# --------------------------------------------------------------------------- not ours (subprocess)
_SETUP = """
import ctypes, faulthandler, os, signal, sys
from tests.jit.test_compiler import build, wat
from waboros.targets.base import Trap
inst = build(wat('(module (import "h" "cb" (func $cb)) (memory 1) '
                 '(func (export "f") (param i32) (result i32) (i32.load (local.get 0))) '
                 '(func (export "g") (param i32) (result i32) (call $cb) '
                 '(i32.load (local.get 0))))'),
             imports={"h": {"cb": lambda a: (HOOK(), [])[1]}})
HOOK = lambda: None
try:
    inst("f", 65536)
except Trap as e:
    print("trap", e.code.name, flush=True)
"""


def _child(
    body: str, bounds: str = "guard", faulthandler: bool = False, pre: str = ""
) -> "subprocess.CompletedProcess[str]":
    """`pre` runs before the instance of _SETUP exists (before our handler is installed)."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONFAULTHANDLER"}
    env["WABOROS_JIT_BOUNDS"] = bounds
    fh = ["-X", "faulthandler"] if faulthandler else []
    head = "import ctypes, faulthandler, platform, signal\n"
    cmd = [sys.executable, *fh, "-c", head + pre + _SETUP + body]
    try:
        return subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    except OSError as e:  # QEMU user mode without binfmt cannot start a child interpreter
        pytest.skip(f"no child interpreter: {e}")


@pytest.mark.parametrize("fh", [False, True], ids=["plain", "faulthandler"])
@pytest.mark.parametrize("fault", ["ctypes.string_at(16)", "os.kill(os.getpid(), signal.SIGSEGV)"])
def test_a_foreign_fault_behaves_as_without_us(fault: str, fh: bool) -> None:
    """Windows: ctypes' own `__except` turns the access violation into OSError and os.kill
    terminates with code 11, with and without our vectored handler in front."""
    want = _child(f"print('x', flush=True); {fault}", "explicit", fh)
    got = _child(f"print('x', flush=True); {fault}", "guard", fh)
    code = (11 if "kill" in fault else 1) if WIN else -11  # Windows: an uncaught OSError
    assert want.returncode == got.returncode == code, (want, got)
    assert got.stdout == want.stdout == "trap MEMORY_OUT_OF_BOUNDS\nx\n"
    say = "Windows fatal exception: access violation" if WIN else "Segmentation fault"
    if fh and not (WIN and "kill" in fault):  # faulthandler (enabled before us) still prints
        assert say in want.stderr and say in got.stderr
        assert "<string>" in got.stderr
        assert got.stderr.count(say) == want.stderr.count(say)  # not for our guard trap


_DARWIN_ONLY = pytest.mark.skipif(sys.platform != "darwin", reason="SIGBUS is ours on macOS only")


@pytest.mark.parametrize(
    ("how", "fh"),
    [
        ("signal.signal(signal.SIGSEGV, signal.SIG_DFL)", False),
        ("signal.signal(signal.SIGSEGV, signal.SIG_IGN)", False),
        ("faulthandler.disable()", True),  # enabled before ours: it puts SIG_DFL back
        ("faulthandler.enable()", False),
        ("faulthandler.disable(); faulthandler.enable()", True),  # its handler on top again
        ("signal.signal(signal.SIGSEGV, lambda *a: None)", False),  # Python-level
        pytest.param("signal.signal(signal.SIGBUS, signal.SIG_DFL)", False, marks=_DARWIN_ONLY),
        pytest.param("signal.signal(signal.SIGBUS, lambda *a: None)", False, marks=_DARWIN_ONLY),
    ],
)
def test_a_broken_chain_raises_at_the_next_call(how: str, fh: bool) -> None:
    """S0006 Q1 (revised): the cases that recognisably break the chain to our handler. On
    Windows none of them touches our vectored handler: the next guard fault still traps.
    faulthandler enabled after ours sits in front of it and prints a "fatal" traceback for that
    fault, then passes it on (CPython's faulthandler.c; RISKS R6)."""
    if WIN:
        p = _child(f"{how}\ntry:\n    inst('f', 65536)\nexcept Trap as e:\n"
                   "    print('trap', e.code.name)\n", faulthandler=fh)  # fmt: skip
        assert p.returncode == 0 and p.stdout.count("trap MEMORY_OUT_OF_BOUNDS") == 2, p
        if how.endswith("faulthandler.enable()"):
            assert "Windows fatal exception: access violation" in p.stderr, p
        return
    p = _child(f"{how}\ntry:\n    inst('f', 65536)\n"
               "except RuntimeError as e:\n    print('error', e)\n",
               faulthandler=fh)  # fmt: skip
    assert p.returncode == 0, p
    assert (
        "trap MEMORY_OUT_OF_BOUNDS" in p.stdout and "error the guard-page fault handler" in p.stdout
    )


@_POSIX_ONLY
def test_broken_checks_every_signal_we_take(monkeypatch: pytest.MonkeyPatch) -> None:
    """S0006 step 3: with SIGBUS ours too (macOS), a SIGBUS action reset after ours, or a
    Python-level SIGBUS handler, breaks the chain although SIGSEGV is still ours (any host)."""
    import signal

    acts = {guard.SIGSEGV: 0x1000, signal.SIGBUS: 0x1000}

    def query(sig: int, new: None, cur: Any) -> int:
        cur._obj.handler = acts[sig]
        return 0

    monkeypatch.setattr(guard, "SIGNALS", (guard.SIGSEGV, signal.SIGBUS))
    py = {s: signal.getsignal(s) for s in guard.SIGNALS}
    monkeypatch.setattr(guard, "_handler", {"addr": 0x1000, "sigaction": query, "py": py})
    assert guard.broken() is None
    acts[signal.SIGBUS] = 0  # SIG_DFL
    assert guard.broken() == "SIGBUS reset to SIG_DFL/SIG_IGN"
    acts[signal.SIGBUS] = 0x2000
    with monkeypatch.context() as m:
        m.setattr(guard.signal, "getsignal", lambda s: print if s == signal.SIGBUS else py[s])  # pyright: ignore[reportUnknownLambdaType]
        assert guard.broken() == "a Python-level SIGBUS handler set after ours"


@_POSIX_ONLY
def test_install_puts_back_what_it_installed_when_a_later_sigaction_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S0006 step 3: two signals (macOS); the second `sigaction` fails, so the first is put back
    and nothing counts as installed (a retry would otherwise chain to our own handler)."""
    import ctypes
    import signal

    calls: list[tuple[int, bool]] = []

    def fake(sig: int, new: int | None, old: int | None) -> int:
        calls.append((sig, bool(new)))
        if old:
            ctypes.memset(old, 0, ctypes.sizeof(guard._Sigaction))  # pyright: ignore[reportPrivateUsage]
        return -1 if new and sig == signal.SIGBUS else 0

    proto = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
    libc = type("Libc", (), {"sigaction": proto(fake), "raise": proto(fake)})()
    monkeypatch.setattr(guard.ctypes, "CDLL", lambda *a, **k: libc)  # pyright: ignore[reportUnknownLambdaType]
    monkeypatch.setattr(guard, "SIGNALS", (guard.SIGSEGV, signal.SIGBUS))
    monkeypatch.setattr(guard, "_handler", {})
    with pytest.raises(OSError, match="sigaction"):
        guard.install(base.TARGETS[JIT]())
    segv, bus = guard.SIGSEGV, signal.SIGBUS
    assert calls == [(segv, False), (bus, False), (segv, True), (bus, True), (segv, True)]
    assert not guard._handler  # pyright: ignore[reportPrivateUsage]


@_POSIX_ONLY
def test_a_broken_chain_during_a_host_call_fails_that_call() -> None:
    p = _child("HOOK = faulthandler.enable\ntry:\n    inst('g', 65536)\nexcept RuntimeError as e:\n"
               "    print('error', e)\n")  # fmt: skip
    assert p.returncode == 0, p
    assert "error the guard-page fault handler" in p.stdout


_CHAINER = """
from waboros.targets.jit.common.execmem import load
from waboros.targets.jit.common.guard import SA_ONSTACK, SA_SIGINFO, SIGNALS, _Sigaction
libc = ctypes.CDLL(None)
kept = []
for sig in SIGNALS:  # SIGSEGV; and SIGBUS on macOS
    prev, act = _Sigaction(), _Sigaction()
    libc.sigaction(sig, None, ctypes.byref(prev))
    if platform.machine().lower() in ("aarch64", "arm64"):
        from waboros.targets.jit.aarch64.encoder import Asm
        a = Asm()
        a.bti("c")
        a.mov_imm(16, ctypes.addressof(prev))
        a.ld(16, 16)
        a.br(16)  # a foreign handler that passes every fault on
    else:
        from waboros.targets.jit.x86_64.encoder import RAX, Asm, Mem
        a = Asm()
        a.endbr64()
        a.mov_imm(RAX, ctypes.addressof(prev), w=True)
        a.jmp(Mem(RAX))  # a foreign handler that passes every fault on
    code = load(a.finish())
    kept += [prev, code]
    act.handler, act.flags = code.addr, SA_SIGINFO | SA_ONSTACK
    libc.sigaction(sig, ctypes.byref(act), None)
"""


@_POSIX_ONLY
def test_a_foreign_chaining_handler_after_ours_is_no_error() -> None:
    p = _child(_CHAINER + "for a in (65536, 65533):\n    try:\n        inst('f', a)\n"
               "    except Trap as e:\n        print('trap', e.code.name, flush=True)\n"
               "print('x', flush=True)\nctypes.string_at(16)\n")  # fmt: skip
    assert p.returncode == -11, p
    assert p.stdout == "trap MEMORY_OUT_OF_BOUNDS\n" * 3 + "x\n"


_WASMTIME = """
import wasmtime
st = wasmtime.Store()
m = wasmtime.Module(st.engine, '(module (memory 1) (func (export "g") (param i32) (result i32)'
                    ' (i32.load (local.get 0))))')
try:
    wasmtime.Instance(st, m, []).exports(st)["g"](st, 65536)
except wasmtime.Trap:
    print("wasmtime trap", flush=True)
"""


@pytest.mark.parametrize("above", ["wasmtime", "anonymous"])
def test_a_library_handler_after_ours_under_faulthandler_is_no_error(above: str) -> None:
    """Rule A (S0006 Q1): with faulthandler enabled (pytest does it) a handler set after ours
    outside the interpreter's image (wasmtime's, in its shared object; one in anonymous memory, as
    another waboros copy's) is assumed to chain; guard traps keep working."""
    if above == "wasmtime":
        pytest.importorskip("wasmtime")
    elif WIN:
        pytest.skip("a POSIX signal chain (S0006 step 4)")
    p = _child((_WASMTIME if above == "wasmtime" else _CHAINER)
               + "from waboros.targets.jit.common import guard\nprint('broken', guard.broken())\n"
               + "for a in (65536, 65533):\n    try:\n        inst('f', a)\n"
               "    except Trap as e:\n        print('trap', e.code.name, flush=True)\n",
               faulthandler=True)  # fmt: skip
    assert p.returncode == 0, p
    head = "trap MEMORY_OUT_OF_BOUNDS\n" + ("wasmtime trap\n" if above == "wasmtime" else "")
    assert p.stdout == head + "broken None\n" + "trap MEMORY_OUT_OF_BOUNDS\n" * 2


@_POSIX_ONLY
@pytest.mark.parametrize("above", ["wasmtime", "anonymous"])
def test_faulthandler_enabled_after_ours_then_a_library_handler_is_an_error(above: str) -> None:
    """The reviewer's crash: ours, then faulthandler enabled, then a library's handler on top.
    The library chains to faulthandler, which re-raises a guard fault (139): rule A alone saw a
    library handler and let the call run. faulthandler enabled after our install is a break.
    `auto` where the row is verified (its check sees the break too), else `guard`: on an
    unverified row `auto` compiles explicit code, installs no
    handler and checks nothing, so the scenario would not exist (CI run 41604810683)."""
    if above == "wasmtime":
        pytest.importorskip("wasmtime")
    verified = guard.host_row() in base.TARGETS[JIT]().verified  # pyright: ignore[reportAttributeAccessIssue]
    p = _child("faulthandler.enable()\n" + (_WASMTIME if above == "wasmtime" else _CHAINER)
               + "try:\n    inst('f', 65536)\nexcept RuntimeError as e:\n    print('error', e)\n",
               bounds="auto" if verified else "guard")  # fmt: skip
    assert p.returncode == 0, p
    assert "trap MEMORY_OUT_OF_BOUNDS" in p.stdout
    assert "error the guard-page fault handler" in p.stdout, p


@_POSIX_ONLY
def test_faulthandler_back_on_top_of_a_foreign_handler_is_an_error_not_a_crash() -> None:
    """faulthandler, then a chaining foreign handler, then ours; then faulthandler re-enabled on
    top: a guard fault would reach faulthandler (fatal, 139), so the next call must raise."""
    p = _child(
        "faulthandler.disable(); faulthandler.enable()\n"
        "try:\n    inst('f', 65536)\nexcept RuntimeError as e:\n    print('error', e)\n",
        faulthandler=True,
        pre=_CHAINER,
    )
    assert p.returncode == 0, p
    assert (
        "trap MEMORY_OUT_OF_BOUNDS" in p.stdout and "error the guard-page fault handler" in p.stdout
    )


@_POSIX_ONLY
def test_a_python_handler_set_before_ours_is_no_error() -> None:
    """A Python-level handler set before our install is only chained to; a chaining foreign
    handler on top of ours then is no break either."""
    p = _child(
        _CHAINER + "for a in (65536, 65533):\n    try:\n        inst('f', a)\n"
        "    except Trap as e:\n        print('trap', e.code.name, flush=True)\n",
        pre="signal.signal(signal.SIGSEGV, lambda *a: None)\n",
    )
    assert p.returncode == 0, p
    assert p.stdout == "trap MEMORY_OUT_OF_BOUNDS\n" * 3


_COPIES = """
import importlib, sys
sys.path.insert(0, sys.argv[1])
import wasmtime
TEXT = '(module (import "h" "cb" (func $cb (param i32))) (memory 1) ' \\
    '(func (export "f") (param i32) (result i32) (i32.load (local.get 0))) ' \\
    '(func (export "g") (param i32 i32) (result i32) (call $cb (local.get 1)) ' \\
    '(i32.load (local.get 0))))'
def make(pkg, cb):
    m = lambda name: importlib.import_module(pkg + name)
    mod = m(".frontend.binary").parse(bytes(wasmtime.wat2wasm(TEXT)))
    m(".frontend.validate").validate(mod)
    t = m(".targets.base").TARGETS[sys.argv[3]]()
    ext = m(".runtime.link").resolve(mod, {"h": {"cb": cb}})
    inst = t.instantiate(t.compile(mod), ext, m(".runtime.store").StoreState())
    names = {n: i for n, k, i in mod["exports"] if k == 0}
    trap = m(".targets.base").Trap
    def call(name, *args):
        try:
            return inst.call(names[name], list(args))
        except trap as e:
            return e.code.name
    return call
insts = {}
def cb(other):
    def f(args):
        if args[0]:
            print("inner", insts[other]("f", args[0]), flush=True)
        return []
    return f
for pkg, other in (("waboros", "waboros_b"), ("waboros_b", "waboros"))[::int(sys.argv[2])]:
    insts[pkg] = make(pkg, cb(other))
for pkg in insts:
    i = insts[pkg]
    print(pkg, i("f", 65536), i("f", 8), i("g", 65533, 65535), flush=True)
hs = {sys.modules[p + ".targets.jit.common.guard"]._handler["addr"] for p in insts}
print("handlers", len(hs))
"""


@pytest.mark.parametrize("order", [1, -1], ids=["a-first", "b-first"])
def test_two_copies_of_waboros_chain_in_either_order(order: int, tmp_path: Path) -> None:
    """S0006 Q8 (revised): a vendored copy (`waboros_b`, the package renamed) has its own registry
    and handler; each claims only its own faults, in either install order, nested too."""
    import re
    import shutil

    shutil.copytree(
        ROOT / "src/waboros", tmp_path / "waboros_b", ignore=shutil.ignore_patterns("__pycache__")
    )
    for f in (tmp_path / "waboros_b").rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        f.write_text(re.sub(r"\bwaboros(\.| import)", r"waboros_b\1", text), encoding="utf-8")
    env = {**os.environ, "WABOROS_JIT_BOUNDS": "guard"}
    cmd = [sys.executable, "-c", _COPIES, str(tmp_path), str(order), JIT]
    try:
        p = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    except OSError as e:  # QEMU user mode without binfmt cannot start a child interpreter
        pytest.skip(f"no child interpreter: {e}")
    assert p.returncode == 0, p
    lines = sorted(p.stdout.splitlines())
    want = ["handlers 2"] + ["inner MEMORY_OUT_OF_BOUNDS"] * 2
    want += [
        f"{pkg} MEMORY_OUT_OF_BOUNDS [0] MEMORY_OUT_OF_BOUNDS" for pkg in ("waboros", "waboros_b")
    ]
    assert lines == sorted(want), p


def test_explicit_installs_nothing() -> None:
    p = _child("from waboros.targets.jit.common import guard\n"
               "print('installed', bool(guard._handler))",
               "explicit")  # fmt: skip
    assert p.returncode == 0 and p.stdout.endswith("installed False\n"), p


_CET = """
import ctypes, os, site, sys
os.environ["WABOROS_JIT_BOUNDS"] = "guard"
for p in sys.argv[2:]:  # the venv's site-packages (its .pth files too) and the checkout
    site.addsitedir(p)
k32 = ctypes.WinDLL("kernel32")
k32.GetProcessMitigationPolicy.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                                           ctypes.c_size_t]
out = open(sys.argv[1], "w")
# ProcessControlFlowGuardPolicy 7, ProcessUserShadowStackPolicy 15 (winnt.h)
for name, k in (("cfg", 7), ("shadow_stack", 15)):
    f = ctypes.c_uint32()
    ok = k32.GetProcessMitigationPolicy(ctypes.c_void_p(-1), k, ctypes.byref(f), 4)
    print(name, f.value if ok else -1, file=out, flush=True)
from tests.jit.test_compiler import build, wat
from waboros.targets.base import Trap
inst = build(wat('(module (memory 1) (func (export "f") (param i32) (result i32) '
                 '(i32.load (local.get 0))))'))
for a in (65536, 65533):
    try:
        inst("f", a)
    except Trap as e:
        print("trap", e.code.name, file=out, flush=True)
"""


@pytest.mark.skipif(not WIN or JIT != "jit-x86_64", reason="CET shadow stacks: Windows x64")
def test_guard_traps_under_forced_cet_shadow_stacks(tmp_path: Path) -> None:
    """S0006 step 4, the EH-continuation question. python.exe is not CETCOMPAT (python.org 3.13.9
    and 3.14.0, uv's 3.13.18: no IMAGE_DLLCHARACTERISTICS_EX_CET_COMPAT, no /guard:cf), so user
    shadow stacks are off by default. This child is created with them on in strict mode and with
    context-IP validation always on (PROCESS_CREATION_MITIGATION_POLICY2_*, winbase.h): if
    Windows refused the resume address the handler writes into the context, the child would die
    instead of printing two traps. The child is the base interpreter (`sys._base_executable`): a
    venv's python.exe is a launcher that starts the real one as a second process, which would not
    inherit the policy. Skips only where the machine has no user CET
    (`IsUserCetAvailableInEnvironment`); with CET, a child that reads the policy back as off
    fails the test."""
    import ctypes as C
    import site
    import subprocess as sp
    from ctypes import wintypes as W

    k32 = C.WinDLL("kernel32", use_last_error=True)  # pyright: ignore[reportAttributeAccessIssue]
    available = getattr(k32, "IsUserCetAvailableInEnvironment", None)  # Windows 10 2004 and later
    if not (available and available(0)):  # USER_CET_ENVIRONMENT_WIN32_PROCESS (sysinfoapi.h)
        pytest.skip("no user CET on this machine (IsUserCetAvailableInEnvironment: no)")
    size = C.c_size_t()
    k32.InitializeProcThreadAttributeList(None, 1, 0, C.byref(size))
    attrs = C.create_string_buffer(size.value)
    assert k32.InitializeProcThreadAttributeList(attrs, 1, 0, C.byref(size)), C.get_last_error()  # pyright: ignore[reportAttributeAccessIssue]
    policy = (C.c_uint64 * 2)(0, 3 << 28 | 1 << 32)  # shadow stacks strict; set-context IP check
    k32.UpdateProcThreadAttribute.argtypes = [C.c_void_p, W.DWORD, C.c_size_t, C.c_void_p,
                                              C.c_size_t, C.c_void_p, C.c_void_p]  # fmt: skip
    mitigation = 0x20007  # PROC_THREAD_ATTRIBUTE_MITIGATION_POLICY
    assert k32.UpdateProcThreadAttribute(attrs, 0, mitigation, policy, 16, None, None)
    dwords = [(n, W.DWORD) for n in ("x", "y", "xs", "ys", "xc", "yc", "fill", "flags")]
    si = type("SIEX", (C.Structure,), {"_fields_": [
        ("cb", W.DWORD), ("r", C.c_void_p), ("desk", C.c_void_p), ("title", C.c_void_p), *dwords,
        ("show", W.WORD), ("cb2", W.WORD), ("r2", C.c_void_p), ("hin", W.HANDLE),
        ("hout", W.HANDLE), ("herr", W.HANDLE), ("attrs", C.c_void_p)]})()  # fmt: skip
    si.cb, si.attrs = C.sizeof(si), C.addressof(attrs)
    pi = (C.c_void_p * 3)()  # hProcess, hThread, {dwProcessId, dwThreadId}
    out = tmp_path / "out.txt"
    paths = [*site.getsitepackages(), str(ROOT / "src"), str(ROOT)]
    base = getattr(sys, "_base_executable", sys.executable)
    cmd = C.create_unicode_buffer(sp.list2cmdline([base, "-c", _CET, str(out), *paths]))
    k32.CreateProcessW.argtypes = [C.c_void_p, C.c_void_p, C.c_void_p, C.c_void_p, W.BOOL,
                                   W.DWORD, C.c_void_p, C.c_wchar_p, C.c_void_p,
                                   C.c_void_p]  # fmt: skip
    if not k32.CreateProcessW(None, cmd, None, None, False, 0x80000, None, str(ROOT),
                              C.byref(si), pi):  # EXTENDED_STARTUPINFO_PRESENT  # fmt: skip
        pytest.fail(f"CreateProcessW with forced shadow stacks: error {C.get_last_error()}")  # pyright: ignore[reportAttributeAccessIssue]
    k32.WaitForSingleObject.argtypes = [C.c_void_p, W.DWORD]
    timeout = k32.WaitForSingleObject(pi[0], 120_000) != 0  # WAIT_OBJECT_0
    if timeout:
        k32.TerminateProcess(C.c_void_p(pi[0]), 1)
        k32.WaitForSingleObject(pi[0], 10_000)
    status = W.DWORD()
    k32.GetExitCodeProcess(C.c_void_p(pi[0]), C.byref(status))
    for h in pi[:2]:
        k32.CloseHandle(C.c_void_p(h))
    text = out.read_text() if out.exists() else ""
    assert not timeout, f"the child ran for 120 s: {text!r}"
    if "shadow_stack" in text:  # the machine has CET: the forced policy must be on in the child
        assert int(text.split("shadow_stack ")[1].split()[0]) & 1, f"policy not applied: {text!r}"
    traps = text.count("trap MEMORY_OUT_OF_BOUNDS")
    assert status.value == 0 and traps == 2, (hex(status.value), text)
