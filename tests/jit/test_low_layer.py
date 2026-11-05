# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
"""S0011 step 1: the memory bounds checks written once (`jit/common/twolevel.py`) over each
backend's low layer, behind `WABOROS_JIT_TWOLEVEL=1`. Identity: the code with the switch on equals
the code with it off (both targets, cross-compiled, both bounds modes) for CoreMark and for modules
covering every path but the listed differences (S0011, "Identity"). Boundaries: every immediate
range of the low-layer contract (LESSONS 83), run on this machine's JIT with the switch on and
off, the outcomes equal and the traps where the bounds say."""

from pathlib import Path
from typing import Any, cast

import pytest

from tests.jit import native, need
from tests.jit.test_compiler import JIT, ROOT, build, outcome, wat
from waboros.frontend.binary import parse
from waboros.frontend.validate import validate
from waboros.targets import base
from waboros.targets.base import TrapCode
from waboros.targets.jit.common import guard
from waboros.targets.jit.common.backend import Reg

M64 = (1 << 64) - 1
SIZES = {1: "i64.load8_u", 2: "i64.load16_s", 4: "i64.load32_u", 8: "i64.load"}
STORE = {1: "i64.store8", 2: "i64.store16", 4: "i64.store32", 8: "i64.store"}
# i32 offsets: imm12 (and lsl #12) edges, disp32 edges (offset + size against 2**31), up to 2**32-1
OFF32 = [0, 8, 4095, 4096, 4097, 0x5000, 0x5001, 0x7FFFFFF0, 0x7FFFFFF8, 0x7FFFFFFC, 0x80000000,
         0xFFFFFFFF]  # fmt: skip
# constant addresses: the scaled imm12 form's edge (4095 * size), unaligned, the page end, 2**31
CONST = [0, 3, 4092, 4093, 16380, 16384, 32760, 32768, 65528, 65532, 65535]
# constant addresses whose end is at or past 2**31 (checked: explicit mode, or past the guard's
# reach): x86-64 compares through rdx before forming the address (S0011 "Identity"), so these are
# run, not compared
HIGH = [0x7FFFFFF8, 0x7FFFFFFC, 0x80000000, 0xFFFFFFFC]
# i64 constant addresses: in and past the page, from 2**31 up to 2**64 - 8
C64 = [0, 65528, 65536, 1 << 31, 1 << 32, 1 << 63, M64 - 7]
# i64 offsets on a memory of minimum 1 page: the `k <= lo` edge, imm12, disp32, past 2**32 and 2**64
OFF64 = [0, 8, 4095, 4096, 65528, 65529, 65536, 0x7FFFFFF8, 0x80000000, (1 << 32) + 7, M64 - 7, M64]


def _f(name: str, sig: str, body: str) -> str:
    return f'(func (export "{name}") {sig} {body})'


def _text(off64: bool) -> str:
    """Loads and stores of every width on memory 0 ($a) and on a memory read from its context
    words ($b): i32 index, `local + const` index, constant address; with `off64` also the
    constant addresses of HIGH and the i64 memories $c (memory 2) and $d (memory 3)."""
    fs: list[str] = []
    ld, st = "(param i32) (result i64)", "(param i32 i64)"
    for m in ("$a", "$b"):
        for size, op in SIZES.items():
            for off in OFF32:
                fs.append(_f(f"l{m}{size}+{off}", ld, f"({op} {m} offset={off} (local.get 0))"))
                body = f"({STORE[size]} {m} offset={off} (local.get 0) (local.get 1))"
                fs.append(_f(f"s{m}{size}+{off}", st, body))
            body = f"({op} {m} offset=4 (i32.add (local.get 0) (i32.const -8)))"
            fs.append(_f(f"k{m}{size}", ld, body))
            for ea in CONST + HIGH * off64:
                fs.append(_f(f"c{m}{size}@{ea}", "(result i64)", f"({op} {m} (i32.const {ea}))"))
        for off in (0, 4096, 65520, 0x7FFFFFF0):
            body = f"(i64x2.extract_lane 1 (v128.load {m} offset={off} (local.get 0)))"
            fs.append(_f(f"v{m}+{off}", ld, body))
    mems = '(memory $a 1) (memory $b 1) (data (memory $a) (i32.const 0) "\\01\\02\\03\\04")'
    if off64:
        mems += " (memory $c i64 1) (memory $d i64 1)"
        for m in ("$c", "$d"):
            for size, op in SIZES.items():
                for off in OFF64:
                    body = f"({op} {m} offset={off} (local.get 0))"
                    fs.append(_f(f"l{m}{size}+{off}", "(param i64) (result i64)", body))
                for ea in C64:
                    body = f"({op} {m} (i64.const {ea - (1 << 64) if ea >> 63 else ea}))"
                    fs.append(_f(f"c{m}{size}@{ea}", "(result i64)", body))
    return f"(module {mems}\n" + "\n".join(fs) + ")"


# the i64 accesses whose code is the same either way: the `k <= lo` path, and the carry path
# without an offset (with one, aarch64 forms the index by `add`, not `sub`: S0011 "Identity")
I64_SAME = """(module (memory $c i64 1) (memory $d i64 2)
  (func (export "a") (param i64) (result i64) (i64.load $c offset=65528 (local.get 0)))
  (func (export "b") (param i64) (result i64) (i64.load $d offset=8 (local.get 0)))
  (func (export "c") (param i64) (result i64) (i64.load $c (local.get 0)))
  (func (export "d") (param i64 i64) (i64.store32 $d offset=131068 (local.get 0) (local.get 1)))
  (func (export "e") (param i64 i64) (i64.store8 $c offset=4095 (local.get 0) (local.get 1))))"""


def _compiled(target: str, wasm: bytes, twolevel: bool, mp: pytest.MonkeyPatch) -> Any:
    mp.setenv("WABOROS_JIT_TWOLEVEL", "1" if twolevel else "")
    mod = parse(wasm)
    validate(mod)
    c = cast(Any, base.TARGETS[target]().compile(mod))
    return (c.code, c.data_start, c.funcs, getattr(c, "faults", None))


@pytest.mark.parametrize("target", ["jit-x86_64", "jit-aarch64"])
@pytest.mark.parametrize("mode", ["guard", "explicit"])
def test_switch_changes_no_code(target: str, mode: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cross-compiled (D35): no machine is needed. CoreMark, every i32 path, the i64 paths S0011
    keeps identical."""
    monkeypatch.setenv("WABOROS_JIT_BOUNDS", mode)
    core = (Path(ROOT) / "poc/in/core.wasm").read_bytes()
    for wasm in (core, wat(_text(False)), wat(I64_SAME)):
        on = _compiled(target, wasm, True, monkeypatch)
        assert on == _compiled(target, wasm, False, monkeypatch)


@pytest.mark.parametrize("target", ["jit-x86_64", "jit-aarch64"])
@pytest.mark.parametrize("mode", ["guard", "explicit"])
def test_k_at_two_to_the_31_compiles(
    target: str, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:  # fmt: skip
    """offset + size at 2**31 - 1, 2**31 and 2**31 + 1 under a minimum above it, on memory 0 (the
    limit a register: `lea`) and memory 1 (a context word: `sub` takes |k| as an imm32, LESSONS
    83): every form compiles with the switch on, on both targets (`test_two_gib_memories` runs
    them on this machine)."""
    monkeypatch.setenv("WABOROS_JIT_BOUNDS", mode)
    monkeypatch.setenv("WABOROS_JIT_TWOLEVEL", "1")
    fs = "".join(
        f'(func (export "{m}{k}") (param i64) (result i64) (i64.load {m} offset={k - 8} '
        f"(local.get 0)))"
        for m in ("$a", "$b")
        for k in ((1 << 31) - 1, 1 << 31, (1 << 31) + 1)
    )
    mod = parse(wat(f"(module (memory $a i64 32769) (memory $b i64 32769) {fs})"))
    validate(mod)
    base.TARGETS[target]().compile(mod)


@pytest.fixture(params=["guard", "explicit"])
def mode(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    native(JIT)
    if request.param == "guard":
        row, tgt = guard.host_row(), base.TARGETS[JIT]()
        need(row in cast(Any, tgt).guard_rows, f"a guard-page handler for {row}")
    monkeypatch.setenv("WABOROS_JIT_BOUNDS", request.param)
    return cast(str, request.param)


def test_boundaries_match_the_old_code(mode: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every export at the bound's edges, on this machine, switch on and off: the same outcome
    (stores read back by the loads after them), and a trap exactly where the access does not fit."""
    wasm = wat(_text(True))
    monkeypatch.setenv("WABOROS_JIT_TWOLEVEL", "1")
    new = build(wasm)
    monkeypatch.setenv("WABOROS_JIT_TWOLEVEL", "")
    old = build(wasm)
    page = 1 << 16
    calls: list[tuple[str, tuple[int, ...], bool]] = []  # (export, args, out of bounds)
    for m in ("$a", "$b"):
        for size in SIZES:
            for off in OFF32:
                for i in sorted({0, 1, page - off - size, page - off - size + 1, 0xFFFFFFFF}):
                    if 0 <= i < 1 << 32:
                        oob = i + off + size > page
                        calls.append((f"s{m}{size}+{off}", (i, 0x8877665544332211), oob))
                        calls.append((f"l{m}{size}+{off}", (i,), oob))
            for i in (0, 4, 8, 7, page - size + 4, page - size + 5):
                calls.append((f"k{m}{size}", (i,), (i - 8) % (1 << 32) + 4 + size > page))
            for ea in CONST + HIGH:
                calls.append((f"c{m}{size}@{ea}", (), ea + size > page))
        for off in (0, 4096, 65520, 0x7FFFFFF0):
            for i in (0, page - off - 16, page - off - 15):
                if i >= 0:
                    calls.append((f"v{m}+{off}", (i,), i + off + 16 > page))
    for m in ("$c", "$d"):
        for size in SIZES:
            for off in OFF64:
                last = page - off - size
                for i in sorted({0, last, last + 1, M64, (1 << 64) - size}):
                    if 0 <= i <= M64:
                        calls.append((f"l{m}{size}+{off}", (i,), i + off + size > page))
            for ea in C64:
                calls.append((f"c{m}{size}@{ea}", (), ea + size > page))
    for name, args, oob in calls:
        got = outcome(new, name, *args)
        assert got == outcome(old, name, *args), (name, args)
        assert (got == TrapCode.MEMORY_OUT_OF_BOUNDS) == oob, (name, args, got)


S = 32769 << 16  # bytes of a memory of 32769 pages: 2**31 + 65536 (committed lazily)
BIG32 = [(1 << 31) - 8, (1 << 31) - 4, 1 << 31, (1 << 31) + 8, S - 8, S - 7, S - 1, S]
BIG64 = [*BIG32, 1 << 32, 1 << 63, M64 - 7]
KS = [(1 << 31) - 1, 1 << 31, (1 << 31) + 1]  # offset + 8 of the indexed i64 loads


def _big(i64: bool) -> str:
    """Two memories of 32769 pages (memory 0 and one read from its context words): 8-byte stores
    and loads at constant addresses around 2**31 and at the bound; with `i64`, i64 memories and
    also indexed loads whose k = offset + 8 is around 2**31, below the declared minimum."""
    t, fs = ("i64" if i64 else "i32"), list[str]()
    for m in ("$a", "$b"):
        for ea in BIG64 if i64 else BIG32:
            c = f"({t}.const {ea - (1 << 64) if ea >> 63 else ea})"
            fs.append(_f(f"s{m}@{ea}", "(param i64)", f"(i64.store {m} {c} (local.get 0))"))
            fs.append(_f(f"l{m}@{ea}", "(result i64)", f"(i64.load {m} {c})"))
        for k in KS * i64:
            body = f"(i64.load {m} offset={k - 8} (local.get 0))"
            fs.append(_f(f"k{m}{k}", "(param i64) (result i64)", body))
    mems = f"(memory $a {t} 32769) (memory $b {t} 32769)"
    return f"(module {mems}\n" + "\n".join(fs) + ")"


@pytest.mark.parametrize("i64", [False, True], ids=["i32", "i64"])
def test_two_gib_memories(mode: str, i64: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    """In bounds past 2**31, on memory 0 and memory 1: the constant-address form S0011 changed on
    x86-64 and the i64 `k <= lo` path at k = 2**31, switch on and off. Values are read back where
    the access fits, and it traps exactly at the bound."""
    wasm = wat(_big(i64))
    monkeypatch.setenv("WABOROS_JIT_TWOLEVEL", "1")
    new = build(wasm)
    monkeypatch.setenv("WABOROS_JIT_TWOLEVEL", "")
    old = build(wasm)
    for m in ("$a", "$b"):
        for ea in BIG64 if i64 else BIG32:
            v = 0x1122334455667700 | ea & 0xFF
            for inst in (new, old):
                fits = ea + 8 <= S
                want: object = [] if fits else TrapCode.MEMORY_OUT_OF_BOUNDS
                assert outcome(inst, f"s{m}@{ea}", v) == want, (m, ea)
                want = [v] if fits else TrapCode.MEMORY_OUT_OF_BOUNDS
                assert outcome(inst, f"l{m}@{ea}") == want, (m, ea)
        for k in KS * i64:
            for i in (0, S - k, S - k + 1, M64):
                got = outcome(new, f"k{m}{k}", i)
                assert got == outcome(old, f"k{m}{k}", i), (m, k, i)
                assert (got == TrapCode.MEMORY_OUT_OF_BOUNDS) == (i + k > S), (m, k, i, got)


def test_an_i32_address_ignores_the_upper_half(mode: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """LESSONS 5, not relied on: the bounds check reads a register address as 32 bits. Every load
    and store of a register address here first gets bit 32 of that register set (a backend
    wrapper; the address is `tos`, overwritten by the access), as if a producer had left the upper
    half dirty: the outcomes stay those of the clean module, switch on and off."""
    from waboros.targets.jit.aarch64.backend import Backend as A64
    from waboros.targets.jit.x86_64.backend import Backend as X86

    backend = cast(Any, X86 if JIT == "jit-x86_64" else A64)

    def dirty(b: Any, addr: object) -> None:
        if isinstance(addr, Reg):
            if JIT == "jit-x86_64":
                b.a.bt_imm("bts", addr.id, 32, w=True)
            else:
                b.a.logic_imm("orr", addr.id, addr.id, 1 << 32)

    load, store = backend.load, backend.store

    def dirty_load(b: Any, *args: Any, a64: bool, lo: int) -> None:
        dirty(b, args[5])
        load(b, *args, a64=a64, lo=lo)

    def dirty_store(b: Any, *args: Any, a64: bool, lo: int) -> None:
        dirty(b, args[2])
        store(b, *args, a64=a64, lo=lo)

    text = """(module (memory 1) (data (i32.const 0) "0123456789")
  (func (export "l") (param i32) (result i32)
    (i32.load8_u offset=1 (i32.xor (local.get 0) (i32.const 0))))
  (func (export "s") (param i32) (result i32)
    (i32.store8 offset=2 (i32.xor (local.get 0) (i32.const 0)) (i32.const 65))
    (i32.load8_u (i32.const 2))))"""
    wasm = wat(text)
    for switch in ("1", ""):
        monkeypatch.setenv("WABOROS_JIT_TWOLEVEL", switch)
        clean = build(wasm)
        with monkeypatch.context() as mp:
            mp.setattr(backend, "load", dirty_load)
            mp.setattr(backend, "store", dirty_store)
            dirtied = build(wasm)
        for name, i in (("l", 0), ("l", 3), ("l", 65534), ("l", 65535), ("s", 0), ("s", 65533)):
            want = outcome(clean, name, i)
            assert outcome(dirtied, name, i) == want, (switch, name, i)
