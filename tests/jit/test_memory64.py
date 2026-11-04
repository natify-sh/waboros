# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
"""i64 memories on the machine's JIT target (S0009 step 2a, "Unit tests, JIT", bounds part):
explicit checks in both bounds modes, the `k <= min` path and the carry-checked one, constant
addresses near 2**64, bulk memory at the 64-bit wrap, mixed i32/i64 copies; results and traps
compared with `pyc` and Wasmtime. The D39 amendment B contract (`a64`, `lo` required) and the
fault map of a guard module with mixed memories are checked without running (both targets)."""

import importlib.util
import inspect
from typing import Any, cast

import pytest

from tests.jit import native, need, test_compiler
from tests.jit.test_compiler import JIT, build, outcome, trap_of, wat
from tests.jit.test_float import Oracle
from tests.jit.test_tables import provider
from waboros.frontend.binary import parse
from waboros.frontend.validate import validate
from waboros.runtime.link import LinkError
from waboros.runtime.store import StoreState
from waboros.targets import base
from waboros.targets.base import TrapCode
from waboros.targets.jit.aarch64.backend import Backend as A64Backend
from waboros.targets.jit.common import guard
from waboros.targets.jit.common.backend import Backend as Protocol
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


def test_grow_results_and_the_step_2a_cap() -> None:
    """memory.grow of an i64 memory returns -1 as an i64; past 65 536 pages (the reservation of
    step 2a, lifted in step 2b) too; a min of 2**48 pages or more, or above that cap, is a
    MemoryError at instantiation."""
    jit = build(wat(TEXT))
    assert jit("grow$m", 3) == [1] and jit("grow$m", 1) == [M64] and jit("size$m") == [4]
    assert jit("grow$m", M64) == [M64] and jit("grow$m", 1 << 48) == [M64]
    assert jit("grow$w", 65534) == [2] and jit("grow$w", 1) == [M64]  # step 2a: the cap
    assert jit("grow$n", 1) == [1] and jit("grow$n", 1) == [0xFFFFFFFF]
    with pytest.raises(MemoryError, match="memory minimum size of 281474976710656 pages exceeds"):
        build(wat("(module (memory i64 0x1_0000_0000_0000))"))
    with pytest.raises(MemoryError, match=f"failed to reserve {65537 << 16} bytes"):
        build(wat("(module (memory i64 65537))"))
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
