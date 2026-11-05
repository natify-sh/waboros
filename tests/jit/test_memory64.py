# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
"""i64 memories on the machine's JIT target (S0009 step 2a, "Unit tests, JIT", bounds part):
explicit checks in both bounds modes, the `k <= min` path and the carry-checked one, constant
addresses near 2**64, bulk memory at the 64-bit wrap, mixed i32/i64 copies; results and traps
compared with `pyc` and Wasmtime. The D39 amendment B contract (`a64`, `lo` required) and the
fault map of a guard module with mixed memories are checked without running (both targets).
Step 3: i64 tables (access, bulk operations, indirect calls, element offsets, grow results)."""

import errno
import importlib.util
import inspect
import os
import sys
import threading
from typing import Any, cast

import pytest

from tests.jit import native, need, test_compiler
from tests.jit.test_compiler import JIT, Inst, build, outcome, trap_of, wat
from tests.jit.test_float import Oracle
from tests.jit.test_tables import provider
from waboros.frontend.binary import parse
from waboros.frontend.validate import validate
from waboros.runtime.link import LinkError
from waboros.runtime.store import StoreState
from waboros.targets import base
from waboros.targets.base import TrapCode
from waboros.targets.jit.aarch64.backend import Backend as A64Backend
from waboros.targets.jit.common import execmem, guard, instance
from waboros.targets.jit.common.backend import Backend as Protocol
from waboros.targets.jit.common.execmem import Reservation
from waboros.targets.jit.common.instance import Memory
from waboros.targets.jit.x86_64.backend import Backend as X86Backend

M64 = (1 << 64) - 1
pins = test_compiler.pins  # the fixture: pinned locals and all locals in slots
LOADS = {"i32.load": 4, "i64.load": 8, "f32.load": 4, "f64.load": 8, "i32.load8_s": 1,
         "i32.load8_u": 1, "i32.load16_s": 2, "i32.load16_u": 2, "i64.load8_s": 1,
         "i64.load8_u": 1, "i64.load16_s": 2, "i64.load16_u": 2, "i64.load32_s": 4,
         "i64.load32_u": 4}  # fmt: skip
STORES = {"i32.store": ("i32", 4), "i64.store": ("i64", 8), "i32.store8": ("i32", 1),
          "i32.store16": ("i32", 2), "i64.store8": ("i64", 1), "i64.store16": ("i64", 2),
          "i64.store32": ("i64", 4)}  # fmt: skip
# SIMD memory operators: (operator text after the memarg, access size, value operand)
SIMD = {"v128.load": ("", 16, ""), "v128.load8x8_s": ("", 8, ""), "v128.load8_splat": ("", 1, ""),
        "v128.load32_zero": ("", 4, ""),
        "v128.load16_lane": ("3", 2, "(v128.const i64x2 5 6)")}  # fmt: skip
MEMS = {"$m": 1, "$w": 2}  # the i64 memories and their declared minimum in pages ($n is i32)


def _offsets(size: int) -> list[int]:
    """0; both sides of the `k <= min` boundary of each i64 memory (a swapped `lo` would let
    memory 0 use memory 2's larger minimum); 2**31 (disp32); 2**32 + 7; 2**64 - 1."""
    out = [0, 1 << 31, (1 << 32) + 7, M64]
    for pages in MEMS.values():
        out += [(pages << 16) - size, (pages << 16) - size + 1]
    return out


def _module() -> str:
    out = ["(module (memory $m i64 1 4) (memory $n 1 2) (memory $w i64 2)"]
    for m in (*MEMS, "$n"):
        at = "i32" if m == "$n" else "i64"
        out.append(f'(func (export "grow{m}") (param {at}) (result {at}) '
                   f"(memory.grow {m} (local.get 0)))")  # fmt: skip
        out.append(f'(func (export "size{m}") (result {at}) (memory.size {m}))')
        out.append(f'(func (export "fill{m}") (param {at} i32 {at}) '
                   f"(memory.fill {m} (local.get 0) (local.get 1) (local.get 2)))")  # fmt: skip
        out.append(  # a checksum of n bytes from a
            f'(func (export "sum{m}") (param {at} {at}) (result i64) (local i64) '
            f"(block (loop (br_if 1 ({at}.eqz (local.get 1))) "
            "(local.set 2 (i64.add (i64.mul (local.get 2) (i64.const 31)) "
            f"(i64.load8_u {m} (local.get 0)))) "
            f"(local.set 0 ({at}.add (local.get 0) ({at}.const 1))) "
            f"(local.set 1 ({at}.sub (local.get 1) ({at}.const 1))) (br 0))) (local.get 2))"
        )
        for s in (*MEMS, "$n"):  # n is i64 only between two i64 memories (the narrower type)
            sat = "i32" if s == "$n" else "i64"
            nat = "i64" if at == sat == "i64" else "i32"
            out.append(
                f'(func (export "copy{m}{s}") (param {at} {sat} {nat}) '
                f"(memory.copy {m} {s} (local.get 0) (local.get 1) (local.get 2)))"
            )
    out.append('(func (export "put$n") (param i32 i32) (i32.store $n (local.get 0) (local.get 1)))')
    out.append('(func (export "get$n") (param i32) (result i32) (i32.load $n (local.get 0)))')
    for m in MEMS:
        for op, size in LOADS.items():
            t = op[:3]
            r = "i32" if t in ("i32", "f32") else "i64"
            for off in _offsets(size):
                e = f"({op} {m} offset={off} (local.get 0))"
                e = f"({r}.reinterpret_{t} {e})" if t[0] == "f" else e
                out.append(f'(func (export "{op}{m}{off}") (param i64) (result {r}) {e})')
        for op, (t, size) in STORES.items():
            for off in _offsets(size):
                out.append(f'(func (export "{op}{m}{off}") (param i64 {t}) '
                           f"({op} {m} offset={off} (local.get 0) (local.get 1)))")  # fmt: skip
        for op, (lane, size, v) in SIMD.items():
            for off in _offsets(size):
                out.append(
                    f'(func (export "{op}{m}{off}") (param i64) (result i64 i64) (local $v v128) '
                    f"(local.set $v ({op} {m} offset={off} {lane} (local.get 0) {v})) "
                    "(i64x2.extract_lane 0 (local.get $v)) (i64x2.extract_lane 1 (local.get $v)))"
                )
        for off in _offsets(8):
            out.append(f'(func (export "v128.store64_lane{m}{off}") (param i64) '
                       f"(v128.store64_lane {m} offset={off} 1 (local.get 0) "
                       "(v128.const i64x2 0x1111 0x0102030405060708)))")  # fmt: skip
        for off in _offsets(16):
            out.append(f'(func (export "v128.store{m}{off}") (param i64) '
                       f"(v128.store {m} offset={off} (local.get 0) "
                       "(v128.const i64x2 0x0807060504030201 0x100F0E0D0C0B0A09)))")  # fmt: skip
        # the index in `tos` (computed), and as constants near the bound and near 2**64
        out.append(f'(func (export "tos{m}") (param i64 i64) (result i64) '
                   f"(i64.load {m} offset=4 (i64.add (local.get 0) (local.get 1))))")  # fmt: skip
        for k in (0, 65524, 65525, 131060, 131061, M64 - 11, M64 - 3, M64):
            for off in (0, 4):
                out.append(f'(func (export "k{m}{k}+{off}") (result i64) '
                           f"(i64.load {m} offset={off} (i64.const {k})))")  # fmt: skip
    out.append(")")
    return "\n".join(out)


TEXT = _module()


@pytest.fixture(autouse=True)
def _jit() -> None:
    native(JIT)
    need(importlib.util.find_spec("wasmtime") is not None, "wasmtime")


@pytest.fixture(params=["explicit", "guard"])
def mode(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Both bounds modes: i64 accesses are explicit in either (S0009), i32 ones are fault points
    in guard mode."""
    if request.param == "guard":
        row, tgt = guard.host_row(), base.TARGETS[JIT]()
        need(row in cast(Any, tgt).guard_rows, f"a guard-page handler for {row}")
    monkeypatch.setenv("WABOROS_JIT_BOUNDS", request.param)
    return cast(str, request.param)


class Three:
    """The JIT, pyc and Wasmtime on one module: every call must give the same outcome."""

    def __init__(self, wasm: bytes) -> None:
        self.jit, self.pyc, self.ref = build(wasm), build(wasm, "pyc"), Oracle(wasm)

    def __call__(self, name: str, *args: int, ref: bool = True) -> Any:
        """`ref` False: pyc is the reference and Wasmtime does not run it (a store straddling
        the bound: under an emulator Wasmtime may write its in-bounds part first, D45)."""
        if not ref:
            got = outcome(self.jit, name, *args)
            assert got == outcome(self.pyc, name, *args), (name, args, got)
            return got
        want = self.ref(name, *args)
        got = (outcome(self.jit, name, *args), outcome(self.pyc, name, *args))
        assert got == (want, want), (name, args, got, want)
        return want

    def sums(self) -> None:
        for m in (*MEMS, "$n"):
            for at in (0, 65520):
                self(f"sum{m}", at, 64)


def _indices(size_bytes: int, off: int, size: int) -> list[int]:
    """0, the last index in bounds and the first out (where they exist), and 2**63, 2**64 - 1,
    2**64 - size (so that idx + offset + size carries out of 64 bits)."""
    last = size_bytes - off - size
    out = [0, 1 << 63, M64, (1 << 64) - size]
    return out + [i for i in (last, last + 1) if i >= 0]


@pytest.mark.usefixtures("pins")
def test_every_width_at_each_bound_and_offset(mode: str) -> None:
    """Every load width (scalar and SIMD) and every store at the bound, at both sides of the
    `k <= min` boundary, with offsets past 2**31 and 2**32 and up to 2**64 - 1, indices up to
    2**64 - 1; memory 0 ($m) and a memory read from its context words ($w); then again after
    $m has grown above its declared minimum."""
    e = Three(wat(TEXT))
    for grow in (0, 2):
        e("grow$m", grow)
        e("v128.store$m0", 3)
        e("v128.store$w0", 65530)
        for m in MEMS:
            size_bytes = cast(list[int], e(f"size{m}"))[0] << 16
            for op, size in LOADS.items():
                for off in _offsets(size):
                    for i in _indices(size_bytes, off, size):
                        e(f"{op}{m}{off}", i)
            for op, (_, size, _) in SIMD.items():
                for off in _offsets(size):
                    for i in _indices(size_bytes, off, size):
                        e(f"{op}{m}{off}", i)
            for op, (_, size) in STORES.items():
                for off in _offsets(size):
                    for i in _indices(size_bytes, off, size):
                        v = 0x8899AABBCCDDEEFF & ((1 << 8 * size) - 1)
                        e(f"{op}{m}{off}", i, v, ref=not i + off < size_bytes < i + off + size)
            for op, size in (("v128.store64_lane", 8), ("v128.store", 16)):
                for off in _offsets(size):
                    for i in _indices(size_bytes, off, size):
                        e(f"{op}{m}{off}", i, ref=not i + off < size_bytes < i + off + size)
            for x, y in ((0, 0), (size_bytes - 12, 0), (size_bytes - 11, 0), (M64, 13), (M64, 1)):
                e(f"tos{m}", x, y)
            for k in (0, 65524, 65525, 131060, 131061, M64 - 11, M64 - 3, M64):
                for off in (0, 4):
                    e(f"k{m}{k}+{off}")
        e.sums()


@pytest.mark.usefixtures("pins")
def test_bulk_memory_wraps_and_mixed_copies(mode: str) -> None:
    """memory.fill and memory.copy with d + n or s + n wrapping 2**64 trap before writing
    anything; copies between an i32 and an i64 memory check each range at its own width."""
    e = Three(wat(TEXT))
    e("fill$m", 10, 0xAB, 100)
    e("fill$w", 70000, 0xCD, 100)
    e("put$n", 40, 0x12345678)
    big = [0, 1, 10, 65535, 65536, 65537, 1 << 32, (1 << 32) + 1, 1 << 63, M64 - 1, M64]
    for m in MEMS:
        for d in big:
            for n in (0, 1, 2, 100, M64):
                e(f"fill{m}", d, 0x5A, n)
    for dst in (*MEMS, "$n"):
        for src in (*MEMS, "$n"):
            dw, sw = dst != "$n", src != "$n"
            ds = [d for d in big if dw or d <= 0xFFFFFFFF]
            ss = [s for s in big if sw or s <= 0xFFFFFFFF]
            ns = [0, 1, 50, 65536] + ([M64, 1 << 63] if dw and sw else [0xFFFFFFFF])
            for d in ds:
                for s in ss:
                    for n in ns:
                        e(f"copy{dst}{src}", d, s, n)
            e.sums()


def test_grow_results() -> None:
    """memory.grow of an i64 memory returns -1 as an i64 (an i32 one as an i32)."""
    jit = build(wat(TEXT))
    assert jit("grow$m", 3) == [1] and jit("grow$m", 1) == [M64] and jit("size$m") == [4]
    assert jit("grow$m", M64) == [M64] and jit("grow$m", 1 << 48) == [M64]
    assert jit("grow$n", 1) == [1] and jit("grow$n", 1) == [0xFFFFFFFF]
    assert Memory(1, None, a64=True).mask == M64 and Memory(1, None).mask == 0xFFFFFFFF


IMPORTER = """(module (import "a" "m" (memory $m i64 1))
  (func (export "get") (param i64) (result i32) (i32.load offset=65532 (local.get 0))))"""


def test_imported_memory_above_its_declared_minimum(mode: str) -> None:
    """The `k <= min` path uses the import's declared minimum (1 page); the actual memory has 3:
    accesses up to its end pass, the next one traps. An i64 memory smaller than the import's
    minimum is refused at instantiation even past the linker (the check relies on it)."""
    store = StoreState()
    a = build(wat('(module (memory (export "m") i64 3) (func (export "put") (param i64 i32) '
                  "(i32.store (local.get 0) (local.get 1))))"), store=store)  # fmt: skip
    b = build(wat(IMPORTER), imports={"a": provider(a)}, store=store)
    a("put", (3 << 16) - 4, 0x5EED)
    assert b("get", 2 << 16) == [0x5EED] and b("get", 0) == [0]  # 2 pages + 65532: the end
    assert trap_of(b, "get", (2 << 16) + 1) is TrapCode.MEMORY_OUT_OF_BOUNDS
    assert trap_of(b, "get", M64) is TrapCode.MEMORY_OUT_OF_BOUNDS
    mod = parse(wat(IMPORTER.replace("i64 1)", "i64 4)")))
    validate(mod)
    t = base.TARGETS[JIT]()
    ext = base.Extern("memory", JIT, cast(Any, a.inst).memories[0])
    with pytest.raises(LinkError, match="incompatible import type a.m"):
        t.instantiate(t.compile(mod), [ext], store)


MIXED = """(module (memory $a {} 1) (memory $b {} 1)
  (func (export "a") (param {}) (result i32) (i32.load $a offset=8 (local.get 0)))
  (func (export "b") (param {}) (result i32) (i32.load $b offset=8 (local.get 0))))"""


@pytest.mark.parametrize("target", ["jit-x86_64", "jit-aarch64"])
@pytest.mark.parametrize("first", ["i32", "i64"])
def test_guard_module_with_mixed_memories(
    target: str, first: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In a guard module the i32 memory's access is a fault point and the i64 memory's is not
    (compiled for both targets, never run)."""
    other = "i64" if first == "i32" else "i32"
    mod = parse(wat(MIXED.format(first, other, first, other)))
    validate(mod)
    monkeypatch.setenv("WABOROS_JIT_BOUNDS", "guard")
    c = cast(Any, base.TARGETS[target]().compile(mod))
    assert c.flags & guard.BOUNDS_GUARD and len(c.faults) == 1
    monkeypatch.setenv("WABOROS_JIT_BOUNDS", "explicit")
    assert cast(Any, base.TARGETS[target]().compile(mod)).faults == []


@pytest.mark.parametrize("target", ["jit-x86_64", "jit-aarch64"])
@pytest.mark.parametrize("mode", ["guard", "explicit"])
def test_k_at_two_to_the_31_on_a_memory_other_than_0(
    target: str, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """offset + size == 2**31 under a minimum above it, on memory 1 (its limit is a context word,
    no `lea`): compiles on both targets (review of step 2a; x86-64 raised an imm32 error)."""
    mod = parse(
        wat("""(module (memory $a i64 1) (memory $b i64 32768)
  (func (export "f") (param i64) (result i32) (i32.load $b offset=2147483644 (local.get 0))))""")
    )
    validate(mod)
    monkeypatch.setenv("WABOROS_JIT_BOUNDS", mode)
    base.TARGETS[target]().compile(mod)


def test_mixed_memories_trap_in_both_modes(mode: str) -> None:
    for first, other in (("i32", "i64"), ("i64", "i32")):
        jit = build(wat(MIXED.format(first, other, first, other)))
        for name in ("a", "b"):
            assert jit(name, 65524) == [0]
            assert trap_of(jit, name, 65525) is TrapCode.MEMORY_OUT_OF_BOUNDS
        assert trap_of(jit, "a" if first == "i64" else "b", M64) is TrapCode.MEMORY_OUT_OF_BOUNDS


# ---------------------------------------------------------------- table64 (S0009 step 3)
# $t: i64, 5 elements (max 20); $u: i32, 5 elements; the indices straddle size and 2**32 (a
# 32-bit compare would take 2**32 for 0) and reach 2**64 - 1.
TSIZE, M32 = 5, 0xFFFFFFFF
TIDX = [0, 1, TSIZE - 1, TSIZE, 1 << 32, (1 << 32) + 1, 1 << 63, M64]


def _tables() -> str:
    out = [
        """(module (type $ii (func (param i64) (result i64))) (type $v (func))
  (table $t i64 5 20 funcref) (table $u 5 funcref) (table $big i64 1 funcref)
  (global $g i64 (i64.const 3))
  (elem (table $t) (i64.const 1) func $f1 $f2) (elem (table $t) (global.get $g) func $f3)
  (elem (table $u) (i32.const 0) func $f4 $f5) (elem declare func $nop)
  (func $f1 (type $ii) (i64.add (local.get 0) (i64.const 100)))
  (func $f2 (type $ii) (i64.add (local.get 0) (i64.const 200)))
  (func $f3 (type $ii) (i64.add (local.get 0) (i64.const 300)))
  (func $f4 (type $ii) (i64.add (local.get 0) (i64.const 400)))
  (func $f5 (type $ii) (i64.add (local.get 0) (i64.const 500)))
  (func $nop (type $v))
  (func (export "grow$big") (param i64) (result i64)
    (table.grow $big (ref.null func) (local.get 0)))"""
    ]
    for t, at in (("$t", "i64"), ("$u", "i32")):
        out.append(f"""
  (func (export "peek{t}") (param {at}) (result i64)
    (if (result i64) (ref.is_null (table.get {t} (local.get 0))) (then (i64.const -1))
      (else (call_indirect {t} (type $ii) (i64.const 0) (local.get 0)))))
  (func (export "set{t}") (param {at} i32)
    (table.set {t} (local.get 0) (if (result funcref) (local.get 1)
      (then (ref.func $f5)) (else (ref.null func)))))
  (func (export "setnop{t}") (param {at}) (table.set {t} (local.get 0) (ref.func $nop)))
  (func (export "fill{t}") (param {at} {at})
    (table.fill {t} (local.get 0) (ref.func $f2) (local.get 1)))
  (func (export "call{t}") (param {at} i64) (result i64)
    (call_indirect {t} (type $ii) (local.get 1) (local.get 0)))
  (func (export "tcall{t}") (param {at} i64) (result i64)
    (return_call_indirect {t} (type $ii) (local.get 1) (local.get 0)))
  (func (export "tos{t}") (param {at} {at}) (result i64)
    (call_indirect {t} (type $ii) (i64.const 7) ({at}.add (local.get 0) (local.get 1))))
  (func (export "grow{t}") (param {at}) (result {at})
    (table.grow {t} (ref.null func) (local.get 0)))
  (func (export "size{t}") (result {at}) (table.size {t}))""")
        for k in TIDX:
            if at == "i64" or k <= M32:  # the index as an `Imm`
                k_ = f"({at}.const {k})"
                out.append(f'(func (export "k{t}{k}") (result i64) '
                           f"(call_indirect {t} (type $ii) (i64.const 9) {k_}))")  # fmt: skip
                out.append(f'(func (export "kget{t}{k}") (result i32) '
                           f"(ref.is_null (table.get {t} {k_})))")  # fmt: skip
    for d, da in (("$t", "i64"), ("$u", "i32")):
        for s, sa in (("$t", "i64"), ("$u", "i32")):
            na = "i64" if da == sa == "i64" else "i32"  # n: the narrower type
            args = "(local.get 0) (local.get 1) (local.get 2)"
            out.append(f'(func (export "copy{d}{s}") (param {da} {sa} {na}) '
                       f"(table.copy {d} {s} {args}))")  # fmt: skip
    return "\n".join(out) + ")"


TABLES = _tables()


def _dump(e: Three) -> None:
    """Every element of both tables, as the function it holds (or -1 for null)."""
    for t in ("$t", "$u"):
        for i in range(cast(list[int], e(f"size{t}"))[0]):
            e(f"peek{t}", i)


@pytest.mark.usefixtures("pins")
def test_table64_access_bulk_and_calls(mode: str) -> None:
    """table.get/set/fill/copy, call_indirect and return_call_indirect at 0, size - 1, size,
    2**32, 2**63 and 2**64 - 1 with the index in a register, a slot, `tos` and an immediate; null
    and signature traps after the bounds; fills and copies whose end wraps 2**64; copies between
    an i32 and an i64 table (each side at its width, n the narrower); grow results. Tables are
    explicit in either bounds mode."""
    e = Three(wat(TABLES))
    _dump(e)
    for t, lim in (("$t", M64), ("$u", M32)):
        idx = [i for i in TIDX if i <= lim]
        for i in idx:
            e(f"peek{t}", i)
            e(f"call{t}", i, 5)
            e(f"tcall{t}", i, 6)
            e(f"k{t}{i}")
            e(f"kget{t}{i}")
        for x, y in ((0, 1), (TSIZE - 1, 0), (TSIZE, 0), (lim, 1), (lim, 3), (1 << 31, 1 << 31)):
            e(f"tos{t}", x, y)
        e(f"setnop{t}", 0)
        e(f"call{t}", 0, 1)  # BAD_SIGNATURE
        for i in idx:
            e(f"set{t}", i, 1)
        e(f"set{t}", 2, 0)
        e(f"call{t}", 2, 1)  # INDIRECT_CALL_TO_NULL
        _dump(e)
        for i in idx:
            for n in (0, 1, 2, max(TSIZE - i, 0), lim):
                e(f"fill{t}", i, n)
        _dump(e)
    big = [0, 1, 2, 4, 5, 1 << 32, 1 << 63, M64 - 1, M64]
    for d in ("$t", "$u"):
        for s in ("$t", "$u"):
            dw, sw = d == "$t", s == "$t"
            ns = [0, 1, 2, 5] + ([M64, 1 << 63] if dw and sw else [M32])
            for x in (x for x in big if dw or x <= M32):
                for y in (y for y in big if sw or y <= M32):
                    for n in ns:
                        e(f"copy{d}{s}", x, y, n)
            _dump(e)
    for t, lim in (("$t", M64), ("$u", M32)):
        for n in (1, lim, 1 << 32, 100, 0):
            e(f"grow{t}", n)
        _dump(e)


def test_table64_grow_results_and_cap() -> None:
    """-1 at the table's width; the D27 cap of 10M elements (Wasmtime raises out-of-memory
    instead, so pyc is the reference)."""
    e = Three(wat(TABLES))
    assert e.jit("grow$big", 10_000_000) == [M64] == e.pyc("grow$big", 10_000_000)
    assert e.jit("grow$big", 9_999_999) == [1] == e.pyc("grow$big", 9_999_999)
    assert e.jit("grow$t", 100) == [M64] and e.jit("grow$u", 10_000_000) == [M32]


@pytest.mark.parametrize("off", ["(i64.const 0x1_0000_0001)", "(i64.const -1)", "(i64.const 4)"])
def test_table64_active_element_offsets(off: str) -> None:
    """An active segment's i64 offset is not masked to 32 bits: each of these is out of bounds
    (a 32-bit mask would place the first at 1) and traps at instantiation, as on pyc."""
    text = f"""(module (table $t i64 5 funcref) (elem (table $t) {off} func $f $f) (func $f))"""
    for target in ("pyc", JIT):
        with pytest.raises(base.Trap) as ex:
            build(wat(text), target)
        assert ex.value.code is TrapCode.TABLE_OUT_OF_BOUNDS


@pytest.mark.parametrize("n", [1 << 61, M64])
def test_huge_table_minimum_is_out_of_memory(n: int) -> None:
    """No cap at instantiation (as Wasmtime 50.0.1); a minimum that cannot be allocated is
    Wasmtime's out-of-memory error on the JIT and on pyc, never an OverflowError or a crash
    (S0009 "Limits"). Only sizes no host can provide: tables are allocated eagerly, so an i32
    minimum of 2**32 - 1 (32 GiB) could succeed on a large or overcommitting host."""
    for target in ("pyc", JIT):
        with pytest.raises(MemoryError) as ex:
            build(wat(f"(module (table i64 {n} funcref))"), target)
        assert str(ex.value) == f"out of memory (failed to allocate {8 * n} bytes)"


AMENDED = {"load": ("a64", "lo"), "store": ("a64", "lo"), "simd": ("a64", "lo"),
           "memory_fill": ("a64",), "memory_copy": ("a64",), "table_get": ("a64",),
           "table_set": ("a64",), "table_fill": ("a64",), "table_copy": ("a64",),
           "call_indirect": ("a64",), "tail_indirect": ("a64",)}  # fmt: skip


@pytest.mark.parametrize("cls", [Protocol, X86Backend, A64Backend])
def test_address_type_parameters_are_required(cls: type) -> None:
    """D39 amendment B: `a64` (and `lo` where the backend checks an access) are keyword-only
    with no default, so a call site that forgets one fails at once."""
    for name, params in AMENDED.items():
        sig = inspect.signature(getattr(cls, name))
        for p in params:
            q = sig.parameters[p]
            assert q.kind is q.KEYWORD_ONLY and q.default is q.empty, (cls, name, p)
    b = X86Backend() if cls is not A64Backend else A64Backend()
    with pytest.raises(TypeError, match="a64"):
        cast(Any, b).load(None, 4, False, False, None, None, 0, 16)


# ------------------------------------------------------------------ moving (S0009 step 2b)
QEMU = "QEMU_LD_PREFIX" in os.environ  # set by tools/run_arm64.py
MOVE = """(module (import "h" "f" (func $h)) (memory $a (export "a") i64 1) (memory $b i64 1)
  (func $get (param i64) (result i64) (i64.load (local.get 0)))
  (func (export "grow0") (param i64) (result i64) (memory.grow $a (local.get 0)))
  (func (export "grow1") (param i64) (result i64) (memory.grow $b (local.get 0)))
  (func (export "put0") (param i64 i64) (i64.store $a (local.get 0) (local.get 1)))
  (func (export "get0") (param i64) (result i64) (i64.load $a (local.get 0)))
  (func (export "put1") (param i64 i64) (i64.store $b (local.get 0) (local.get 1)))
  (func (export "get1") (param i64) (result i64) (i64.load $b (local.get 0)))
  (func (export "outer") (result i64 i64 i64 i64)
    (i64.store (i64.const 8) (i64.const 77)) (i64.store $b (i64.const 8) (i64.const 78))
    (call $h)
    (i64.load (i64.const 8)) (call $get (i64.const 8)) (i64.load $b (i64.const 8))
    (i64.store (i64.sub (i64.shl (memory.size) (i64.const 16)) (i64.const 8)) (i64.const 79))
    (call $get (i64.sub (i64.shl (memory.size) (i64.const 16)) (i64.const 8)))))"""
NOHOST = MOVE.replace('(import "h" "f" (func $h))', "").replace("(call $h)", "")
IMPORT_A = """(module (import "m" "a" (memory i64 1))
  (func (export "get") (param i64) (result i64) (i64.load (local.get 0)))
  (func (export "grow") (param i64) (result i64) (memory.grow (local.get 0))))"""


@pytest.fixture
def small(monkeypatch: pytest.MonkeyPatch) -> None:
    """An i64 memory of 1 page moves at its 17th and 34th page; i32 memories are unchanged."""
    monkeypatch.setattr(instance, "RESERVATION64", 1 << 20)
    monkeypatch.setattr(instance, "GROWTH64", 1 << 20)
    monkeypatch.setattr(instance, "TAIL64", 1 << 16)


def _mems(i: Inst) -> list[Memory]:
    return cast(Any, i.inst).memories


def _unmapped(res: Reservation, addr: int) -> bool:
    if sys.platform.startswith("linux"):
        with open("/proc/self/maps") as f:
            assert not any(line.startswith(f"{addr:x}-") for line in f), "old reservation mapped"
    return not res._release.alive  # pyright: ignore[reportPrivateUsage]


@pytest.mark.usefixtures("small")
def test_move_from_wasm(mode: str) -> None:
    """Marks and the zeros around them survive both moves of both memories, the new end is
    usable and the next byte traps (JIT, pyc and Wasmtime agree); the old reservation is gone."""
    e = Three(wat(NOHOST))
    marks = [0, 8, 65528, 4 * 65536 + 8]
    for m in "01":
        e(f"put{m}", 0, 0x1111)
        e(f"put{m}", 65528, 0x2222)
        assert e(f"grow{m}", 4) == [1]
        e(f"put{m}", 4 * 65536 + 8, 0x3333)
        mem = _mems(e.jit)[int(m)]
        for delta, pages in ((12, 17), (17, 34)):
            res, addr = mem.res, mem.res.addr
            assert e(f"grow{m}", delta) == [pages - delta] and mem.res.addr != addr
            assert _unmapped(res, addr) and mem.retired == []
            for at in (*marks, *(k + 8 for k in marks), pages * 65536 - 8, pages * 65536 - 7):
                e(f"get{m}", at)
            e(f"put{m}", pages * 65536 - 8, pages)
            e(f"get{m}", pages * 65536 - 8)
        res, addr = mem.res, mem.res.addr
        assert e(f"grow{m}", 1) == [34] and mem.res is res  # within the growth reserve


@pytest.mark.usefixtures("small")
@pytest.mark.parametrize("how", ["wasm", "host"])
def test_move_in_a_nested_entry(mode: str, how: str) -> None:
    """wasm -> host -> (wasm memory.grow | Memory.grow) moves both memories under a suspended
    frame, which then uses memory 0 directly (pinned base and size), through a direct call, and
    memory 1; a second instance importing memory 0 sees the move too."""
    store, box = StoreState(), {}

    def f(args: Any) -> list[int]:
        i = box["i"]
        if how == "wasm":
            assert i("grow0", 20) == [1] and i("grow1", 20) == [1]
        else:
            assert [m.grow(20) for m in _mems(i)] == [1, 1]
        return []

    i = box["i"] = build(wat(MOVE), imports={"h": {"f": f}}, store=store)
    j = build(wat(IMPORT_A), imports={"m": provider(i)}, store=store)
    assert i("outer") == [77, 77, 78, 79]
    assert j("get", 8) == [77] and j("get", 21 * 65536 - 8) == [79]
    assert trap_of(j, "get", 21 * 65536 - 7) is TrapCode.MEMORY_OUT_OF_BOUNDS
    addr = _mems(i)[0].res.addr
    assert j("grow", 30) == [21] and _mems(i)[0].res.addr != addr  # moved by the importer
    assert i("get0", 8) == [77] and i("get0", 51 * 65536 - 8) == [0]


@pytest.mark.usefixtures("small")
def test_guard_registry_and_a_failed_move(mode: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the i32 memory is registered (guard mode); a reservation that fails gives -1 and
    changes nothing."""
    jit = build(wat(MIXED.format("i32", "i64", "i32", "i64")))
    a, b = _mems(jit)
    if mode == "guard":
        assert a.res.addr in guard._registry.reservations  # pyright: ignore[reportPrivateUsage]
    assert b.res.addr not in guard._registry.reservations  # pyright: ignore[reportPrivateUsage]
    ctx, word = b.users[0]
    b.grow(20)
    assert jit("b", 21 * 65536 - 12) == [0] and jit("a", 65524) == [0]
    assert trap_of(jit, "a", 65525) is TrapCode.MEMORY_OUT_OF_BOUNDS
    assert b.res.addr not in guard._registry.reservations  # pyright: ignore[reportPrivateUsage]

    def fail(*_: Any) -> Any:
        raise OSError(errno.ENOMEM, "injected")

    monkeypatch.setattr(instance, "Reservation", fail)
    before = (b.res, b.pages, ctx[word], ctx[word + 1])
    assert b.grow(100) == -1 and before == (b.res, b.pages, ctx[word], ctx[word + 1])
    assert jit("b", 21 * 65536 - 12) == [0]


class Interrupted(list[Any]):
    """`users` whose iteration raises KeyboardInterrupt before item `at`, once."""

    def __init__(self, items: list[Any], at: int) -> None:
        super().__init__(items)
        self.at = at

    def __iter__(self) -> Any:
        for n, x in enumerate(list.__iter__(self)):
            if n == self.at:
                self.at = -1
                raise KeyboardInterrupt
            yield x


@pytest.mark.usefixtures("small")
@pytest.mark.parametrize("at", [0, 1])
def test_an_interrupted_move_changes_nothing(at: int) -> None:
    """A KeyboardInterrupt in the middle of rewriting the contexts puts every one back on the
    old reservation; the next in-place grow and a later move work, and the host and all three
    instances see the same bytes."""
    import ctypes

    store = StoreState()
    i = build(wat(NOHOST), store=store)
    js = [build(wat(IMPORT_A), imports={"m": provider(i)}, store=store) for _ in range(2)]
    mem = _mems(i)[0]
    mem.users = Interrupted(mem.users, at)
    i("put0", 8, 42)
    with pytest.raises(KeyboardInterrupt):
        mem.grow(20)
    assert mem.pages == 1 and mem.retired == []
    assert all((c[w], c[w + 1]) == (mem.res.addr, 1 << 16) for c, w in mem.users)
    assert js[0]("grow", 5) == [1]  # in place
    for j in js:
        assert j("get", 8) == [42] and j("get", 6 * 65536 - 8) == [0]
    i("put0", 6 * 65536 - 8, 7)
    assert [j("get", 6 * 65536 - 8) for j in js] == [[7], [7]]
    assert ctypes.c_uint64.from_address(mem.res.addr + 6 * 65536 - 8).value == 7
    assert js[1]("grow", 20) == [6] and js[0]("get", 8) == [42]  # a move now completes
    assert all((c[w], c[w + 1]) == (mem.res.addr, 26 << 16) for c, w in mem.users)


def test_an_interrupted_entry_restores_the_counters(monkeypatch: pytest.MonkeyPatch) -> None:
    """An asynchronous exception right after `enter()` has counted the entry leaves the store's
    depth and the active count as they were."""
    i = build(wat(NOHOST))
    store = cast(Any, i.inst).store
    enter = StoreState.enter

    def interrupted(self: StoreState) -> None:
        enter(self)
        raise KeyboardInterrupt

    monkeypatch.setattr(StoreState, "enter", interrupted)
    with pytest.raises(KeyboardInterrupt):
        i("get0", 8)
    monkeypatch.undo()
    st = instance._stacks(store)  # pyright: ignore[reportPrivateUsage]
    assert (store.depth, st.active) == (0, 0) and i("get0", 8) == [0]


def _rss() -> int | None:
    try:
        with open("/proc/self/status") as f:
            return next(int(x.split()[1]) << 10 for x in f if x.startswith("VmRSS"))
    except OSError:
        return None


def _fits(pages: int, moving: bool) -> bool:
    """Can this host reserve and commit what an i64 memory of `pages` pages needs, at
    instantiation (`moving` False) or as the target of a move? A container's or Windows' commit
    limit may refuse it; the runtime must then fail cleanly (S0009 Test plan)."""
    m = pages << 16
    fixed = m <= instance.RESERVATION64 and not moving
    size = (instance.RESERVATION64 if fixed else m + instance.GROWTH64) + instance.TAIL64
    try:
        Reservation(size, m).free()
    except (OSError, MemoryError):
        return False
    return True


def _grow_or_nothing(jit: Inst, delta: int, want: int, moves: bool, marks: dict[int, int]) -> bool:
    """grow0 by `delta`: Wasmtime's result (`want`) where the host can provide the memory, else
    -1 with the base, the size, both context words and the marks unchanged. True if it grew."""
    mem = _mems(jit)[0]
    ctx, word = mem.users[0]
    before = (mem.res.addr, mem.pages, ctx[word], ctx[word + 1])
    can = _fits(want + delta, moves)
    got = jit("grow0", delta)
    if not can:
        assert got == [M64] and before == (mem.res.addr, mem.pages, ctx[word], ctx[word + 1])
    else:
        assert got == [want] and (mem.res.addr != before[0]) == moves
    for at, v in marks.items():
        assert jit("get0", at) == [v]
    return can


@pytest.mark.skipif(QEMU, reason="the cheap variant stands in under QEMU")
def test_move_past_4_gib() -> None:
    """Real sizes: 1 page, +65 535 in place, +1 a move to 4 GiB + 64 KiB, +2**20 a move to
    68 GiB of address space; Wasmtime's row (+65 536 -> 1, then +2**20 -> 65 537). The copy skips
    zero pages: RSS stays small. A host that cannot provide a size (a container's or Windows'
    commit limit; probed by `_fits`) must return -1 with nothing changed, on every OS."""
    rss = _rss()
    jit = build(wat(NOHOST))
    jit("put0", 8, 0x5EED)
    marks = {8: 0x5EED}
    steps = [(65535, 1, False), (1, 65536, True), (1 << 20, 65537, True)]
    done = True
    for delta, want, moves in steps:
        if not _grow_or_nothing(jit, delta, want, moves, marks):
            done = False
            break
        end = (want + delta) << 16
        jit("put0", end - 8, want)
        marks[end - 8] = want
        assert trap_of(jit, "get0", end - 7) is TrapCode.MEMORY_OUT_OF_BOUNDS
    other = build(wat(NOHOST))
    if _grow_or_nothing(other, 65536, 1, True, {}):
        _grow_or_nothing(other, 1 << 20, 65537, True, {})
    if rss is not None and done:
        assert cast(int, _rss()) - rss < 64 << 20


def test_limits_rows() -> None:
    """S0009 "Limits", memory rows (Wasmtime 50.0.1 on a 47-bit host); a min this host cannot
    provide (`_fits`) is the clean instantiation error instead."""
    build(wat("(module (memory i64 0 0x1_0000_0000_0000))"))
    for pages in (65537, 1 << 20):
        if _fits(pages, False):
            assert build(wat(f"(module (memory i64 {pages}))"))
        else:
            size = (pages << 16) + instance.GROWTH64 + instance.TAIL64
            with pytest.raises(MemoryError, match=f"failed to reserve {size} bytes"):
                build(wat(f"(module (memory i64 {pages}))"))
    with pytest.raises(
        MemoryError, match=f"failed to reserve {(1 << 48) + (2 << 30) + (32 << 20)}"
    ):
        build(wat("(module (memory i64 0x1_0000_0000))"))
    with pytest.raises(MemoryError, match="memory minimum size of 281474976710656 pages exceeds"):
        build(wat("(module (memory i64 0x1_0000_0000_0000))"))
    jit = build(wat(NOHOST))
    for delta in (1 << 32, (1 << 48) - 2, 1 << 48, 1 << 63):
        assert jit("grow0", delta) == [M64]  # (1 << 48) - 2: by the size check
    assert jit("grow0", 0) == [1]


def test_reservation_size_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """A size ctypes would truncate is refused before any mapping call (LESSONS 77)."""

    def never(size: int) -> int:
        raise AssertionError(f"mapping call for {size} bytes")

    monkeypatch.setattr(execmem, "_reserve", never)
    for size in (1 << 63, (1 << 64) - (1 << 16) + (2 << 30) + (32 << 20), -1):
        with pytest.raises(OSError) as e:
            Reservation(size)
        assert e.value.errno == errno.ENOMEM


# ------------------------------------------------------------------ one thread per store
THREADS = """(module (import "h" "f" (func $h)) (memory (export "a") i64 1)
  (table (export "t") 1 funcref)
  (func (export "outer") (result i64) (call $h) (memory.size))
  (func (export "nop")))"""


def _in_thread(f: Any) -> BaseException | None:
    out: list[BaseException | None] = [None]

    def run() -> None:
        try:
            f()
        except BaseException as e:
            out[0] = e

    t = threading.Thread(target=run)
    t.start()
    t.join()
    return out[0]


def test_another_thread_is_refused_while_a_host_import_runs() -> None:
    """Entry, Memory.grow, Table.grow and instantiation from another thread raise RuntimeError
    before anything changes; the owner thread may grow; the owner stays after the call (S0002)."""
    store, box = StoreState(), cast(dict[str, Any], {})
    st = instance._stacks(store)  # pyright: ignore[reportPrivateUsage]

    def f(args: Any) -> list[int]:
        i, mem, tab = box["i"], _mems(box["i"])[0], box["i"].inst.tables[0]
        tries = [lambda: i("nop"), lambda: mem.grow(1), lambda: tab.grow(1, 0),
                 lambda: build(wat(IMPORT_A), imports={"m": provider(i)}, store=store)]  # fmt: skip
        for t in tries:
            assert isinstance(_in_thread(t), RuntimeError)
        assert (store.depth, st.active, mem.pages, tab.size) == (1, 1, 1, 1)
        assert mem.grow(1) == 1
        return []

    i = box["i"] = build(wat(THREADS), imports={"h": {"f": f}}, store=store)
    assert store.owner is None  # instantiation does not keep the store
    assert i("outer") == [2] and (store.depth, st.active) == (0, 0)
    assert isinstance(_in_thread(lambda: _mems(i)[0].grow(1)), RuntimeError)


def test_first_claim_is_atomic(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two threads that both find a fresh store unowned: exactly one enters, the other gets
    RuntimeError and leaves the winner's counters alone."""
    from waboros.runtime import store as store_mod

    both, lost, inside = threading.Barrier(2, timeout=10), threading.Event(), []

    class Racing:
        """The claim lock, entered only after both threads have passed the unlocked check."""

        lock = threading.Lock()

        def __enter__(self) -> None:
            both.wait()
            self.lock.acquire()

        def __exit__(self, *_: object) -> None:
            self.lock.release()

    store = StoreState()
    st = instance._stacks(store)  # pyright: ignore[reportPrivateUsage]

    def f(args: Any) -> list[int]:
        assert lost.wait(10)
        inside.append((store.depth, st.active))
        return []

    i = build(wat(THREADS), imports={"h": {"f": f}}, store=store)
    monkeypatch.setattr(store_mod, "_CLAIM", Racing())
    errors: list[BaseException | None] = []

    def run() -> None:
        try:
            assert i("outer") == [1]
            errors.append(None)
        except RuntimeError as e:
            errors.append(e)
            lost.set()

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(map(type, errors), key=str) == sorted([type(None), RuntimeError], key=str)
    assert inside == [(1, 1)] and (store.depth, st.active) == (0, 0)
