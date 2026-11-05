# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
"""The one-pass core and a backend (JIT step 3b; S0004 step 4b): wasm modules compiled and run on
the JIT target of this machine (`jit-x86_64`, `jit-aarch64`), results compared with `pyc` on the
same module where convenient."""

import subprocess
import sys
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from tests.jit import native, native_jit, need
from tests.watcache import wat2wasm as wat
from waboros.frontend.binary import parse
from waboros.frontend.validate import validate
from waboros.runtime import link
from waboros.runtime.store import StoreState
from waboros.targets import base
from waboros.targets.base import HostFunc, Raw, Trap, TrapCode, Unsupported
from waboros.targets.jit.common.compiler import Compiled

ROOT = Path(__file__).resolve().parents[2]
M32, M64 = 0xFFFFFFFF, (1 << 64) - 1
JIT = native_jit()


@pytest.fixture(autouse=True)
def _jit() -> None:
    native(JIT)


class Inst:
    def __init__(self, mod: base.ValidModule, inst: base.TargetInstance) -> None:
        self.mod, self.inst = mod, inst

    def __call__(self, name: str, *args: int) -> list[Raw]:
        idx = next(i for n, k, i in self.mod["exports"] if n == name and k == 0)
        ps, _ = link.func_type(self.mod, idx)
        return self.inst.call(
            idx, [a & (M64 if t in (0x7E, 0x7C) else M32) for a, t in zip(args, ps, strict=True)]
        )


def build(
    wasm: bytes,
    target: str = JIT,
    imports: Mapping[str, Any] | None = None,
    store: StoreState | None = None,
) -> Inst:
    mod = parse(wasm)
    validate(mod)
    t = base.TARGETS[target]()
    compiled = t.compile(mod)
    ext = link.resolve(mod, imports or {})
    return Inst(mod, t.instantiate(compiled, ext, store or StoreState()))


def outcome(inst: "Inst", name: str, *args: int) -> list[Raw] | TrapCode:
    try:
        return inst(name, *args)
    except Trap as e:
        return e.code


def trap_of(inst: "Inst", name: str, *args: int) -> TrapCode:
    with pytest.raises(Trap) as e:
        inst(name, *args)
    return e.value.code


@pytest.fixture(params=["pinned", "nopin"])
def pins(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every compare-with-pyc test runs with pinned locals and with all locals in slots."""
    if request.param == "nopin":
        monkeypatch.setenv("WABOROS_JIT_NOPIN", "all")
    yield


# --------------------------------------------------------------------------- integer operators
BIN = "add sub mul div_s div_u rem_s rem_u and or xor shl shr_s shr_u rotl rotr".split()
CMP = "eq ne lt_s lt_u gt_s gt_u le_s le_u ge_s ge_u".split()
I32_UN = "clz ctz popcnt extend8_s extend16_s eqz".split()
I64_UN = "clz ctz popcnt extend8_s extend16_s extend32_s eqz".split()
EDGE32 = [0, 1, 2, 7, 31, 32, 33, 0x7F, 0x80, 0xFF, 0x8000, 0x7FFFFFFF, 0x80000000, M32, 0xFFFFFFFE,
          0x12345678]  # fmt: skip
EDGE64 = [*EDGE32, 63, 64, 65, 0x7FFFFFFFFFFFFFFF, 1 << 63, M64, 0x123456789ABCDEF0, 1 << 32]


def _ops_module(t: str, ks: Sequence[int]) -> str:
    """Each binary op in four operand shapes: param/param (locals), param/const, const/param and
    computed/computed (the tos register and a slot); each unary op on a param and on a computed
    value; each compare as a value and as a branch condition and as a select condition."""
    uns = I32_UN if t == "i32" else I64_UN
    out = ["(module"]
    for op in BIN:
        out.append(f'(func (export "{op}") (param {t} {t}) (result {t}) '
                   f"({t}.{op} (local.get 0) (local.get 1)))")  # fmt: skip
        out.append(f'(func (export "{op}/tos") (param {t} {t}) (result {t}) '
                   f"({t}.{op} ({t}.xor (local.get 0) ({t}.const 0)) "
                   f"({t}.or (local.get 1) ({t}.const 0))))")  # fmt: skip
        for i, k in enumerate(ks):
            out.append(f'(func (export "{op}/c{i}") (param {t}) (result {t}) '
                       f"({t}.{op} (local.get 0) ({t}.const {k})))")  # fmt: skip
            out.append(f'(func (export "{op}/k{i}") (param {t}) (result {t}) '
                       f"({t}.{op} ({t}.const {k}) (local.get 0)))")  # fmt: skip
    for op in uns:
        r = "i32" if op == "eqz" else t
        out.append(f'(func (export "{op}") (param {t}) (result {r}) ({t}.{op} (local.get 0)))')
        out.append(f'(func (export "{op}/tos") (param {t}) (result {r}) '
                   f"({t}.{op} ({t}.add (local.get 0) ({t}.const 0))))")  # fmt: skip
    for op in CMP:
        out.append(f'(func (export "{op}") (param {t} {t}) (result i32) '
                   f"({t}.{op} (local.get 0) (local.get 1)))")  # fmt: skip
        out.append(f'(func (export "{op}/br") (param {t} {t}) (result i32) '
                   f"(block (br_if 0 ({t}.{op} (local.get 0) (local.get 1))) "
                   "(return (i32.const 7))) (i32.const 9))")  # fmt: skip
        out.append(f'(func (export "{op}/if") (param {t} {t}) (result i32) '
                   f"(if (result i32) (i32.eqz ({t}.{op} (local.get 1) (local.get 0))) "
                   "(then (i32.const 3)) (else (i32.const 4))))")  # fmt: skip
        out.append(f'(func (export "{op}/sel") (param {t} {t}) (result {t}) '
                   f"(select (local.get 0) (local.get 1) "
                   f"({t}.{op} (local.get 0) (local.get 1))))")  # fmt: skip
        out.append(f'(func (export "{op}/k") (param {t}) (result i32) '
                   f"({t}.{op} ({t}.const -2) (local.get 0)))")  # fmt: skip
    out.append(")")
    return "\n".join(out)


@pytest.mark.parametrize("t", ["i32", "i64"])
def test_integer_operators_match_pyc(t: str, pins: None) -> None:
    edge = EDGE32 if t == "i32" else EDGE64
    ks = [0, 1, -1, 5, 31, 32, 63, 64, 0x7FFFFFFF, -0x80000000]
    if t == "i64":
        ks += [0x123456789A, -(1 << 63)]
    wasm = wat(_ops_module(t, ks))
    jit, pyc = build(wasm), build(wasm, "pyc")
    names = [n for n, k, _ in jit.mod["exports"] if k == 0]
    checked = 0
    for name in names:
        nparams = len(
            link.func_type(jit.mod, next(i for n, _, i in jit.mod["exports"] if n == name))[0]
        )
        vals = edge if nparams == 1 else edge[::2]
        argsets = [(a,) for a in vals] if nparams == 1 else [(a, b) for a in vals for b in vals]
        for args in argsets:
            want = outcome(pyc, name, *args)
            assert outcome(jit, name, *args) == want, (name, args)
            checked += 1
    assert checked > 5000


@pytest.mark.parametrize("t", ["i32", "i64"])
def test_add_sub_as_lea_match_pyc(t: str, pins: None) -> None:
    """add/sub of a local and an immediate (fits disp32 or not) and add of two registers: the i32
    result must wrap mod 2**32 and stay zero-extended (LESSONS 5). Written for S0007 L (one lea,
    reverted 2026-11-04); kept as edge-case coverage of the add/sub paths."""
    ks = [0, 1, -1, 0x7FFFFFFF, -0x80000000, 0x80000000, M32]
    if t == "i64":
        ks += [-0x80000001, 1 << 32, 0x123456789A, -(1 << 63), (1 << 63) - 1]
    ext = "(i64.extend_i32_u)" if t == "i32" else ""
    funcs = [f'(func (export "r") (param {t} {t}) (result i64) '
             f"({t}.add (local.get 0) ({t}.mul (local.get 1) ({t}.const 3))) {ext})"]  # fmt: skip
    for p, q in ((0, 1), (1, 0), (1, 1)):  # two pinned registers (rbp as base swaps to index)
        funcs.append(f'(func (export "rr{p}{q}") (param {t} {t}) (result i64) '
                     f"({t}.add (local.get {p}) (local.get {q})) {ext})")  # fmt: skip
    for i, k in enumerate(ks):
        for op in ("add", "sub"):
            funcs.append(f'(func (export "{op}{i}") (param {t} {t}) (result i64) '
                         f"({t}.{op} (local.get 0) ({t}.const {k})) {ext})")  # fmt: skip
            funcs.append(f'(func (export "{op}{i}/tos") (param {t} {t}) (result i64) '
                         f"({t}.{op} ({t}.mul (local.get 1) (local.get 0)) ({t}.const {k})) "
                         f"{ext})")  # fmt: skip
    wasm = wat("(module " + "\n".join(funcs) + ")")
    jit, pyc = build(wasm), build(wasm, "pyc")
    edge = EDGE32 if t == "i32" else EDGE64
    for name in [n for n, k, _ in jit.mod["exports"] if k == 0]:
        for a in edge:
            for b in (1, 3, a):
                assert outcome(jit, name, a, b) == outcome(pyc, name, a, b), (name, a, b)


@pytest.mark.parametrize("t,bits", [("i32", 32), ("i64", 64)])
def test_division_edge_cases(t: str, bits: int) -> None:
    jit = build(
        wat(f"""(module
      (func (export "div_s") (param {t} {t}) (result {t}) ({t}.div_s (local.get 0) (local.get 1)))
      (func (export "rem_s") (param {t} {t}) (result {t}) ({t}.rem_s (local.get 0) (local.get 1)))
      (func (export "div_u") (param {t} {t}) (result {t}) ({t}.div_u (local.get 0) (local.get 1)))
      (func (export "rem_u") (param {t} {t}) (result {t}) ({t}.rem_u (local.get 0) (local.get 1)))
      (func (export "div_s_m1") (param {t}) (result {t}) ({t}.div_s (local.get 0) ({t}.const -1)))
      (func (export "rem_s_m1") (param {t}) (result {t}) ({t}.rem_s (local.get 0) ({t}.const -1)))
      (func (export "div_0") (param {t}) (result {t}) ({t}.div_u (local.get 0) ({t}.const 0))))""")
    )
    mask, imin = (1 << bits) - 1, 1 << (bits - 1)
    for op in ("div_s", "rem_s", "div_u", "rem_u"):
        assert trap_of(jit, op, 5, 0) is TrapCode.INTEGER_DIVISION_BY_ZERO
    assert trap_of(jit, "div_s", imin, -1) is TrapCode.INTEGER_OVERFLOW
    assert trap_of(jit, "div_s_m1", imin) is TrapCode.INTEGER_OVERFLOW
    assert jit("rem_s", imin, -1) == [0] and jit("rem_s_m1", imin) == [0]
    assert jit("div_s_m1", 5) == [-5 & mask] and jit("rem_s", -7, 2) == [-1 & mask]
    assert jit("div_u", -1, 2) == [mask >> 1]
    assert trap_of(jit, "div_0", 1) is TrapCode.INTEGER_DIVISION_BY_ZERO


# --------------------------------------------------------------------------- control flow
CONTROL = """(module
  (func (export "table") (param i32) (result i32)
    (block (block (block (block (block (block
      (br_table 0 1 2 3 4 5 (local.get 0)))
      (return (i32.const 10))) (return (i32.const 11))) (return (i32.const 12)))
      (return (i32.const 13))) (return (i32.const 14))) (i32.const 15))
  (func (export "small") (param i32) (result i32)
    (block (block (br_table 1 0 (local.get 0))) (return (i32.const 20))) (i32.const 21))
  (func (export "values") (param i32) (result i32 i64)
    (block $a (result i32 i64)
      (block $b (result i32 i64)
        (block $c (result i32 i64)
          (br_table $a $b $c $b $c (i32.const 1) (i64.const 2) (local.get 0)))
        (drop) (drop) (i32.const 30) (i64.const 31) (br $a))
      (drop) (drop) (i32.const 40) (i64.const 41)))
  (func (export "params") (param i32 i32) (result i32 i32)
    (local.get 0) (local.get 1)
    (block (param i32 i32) (result i32 i32)
      (i32.add) (i32.const 3) (br_if 0 (i32.eqz (local.get 0))) (i32.mul (i32.const 2))))
  (func (export "if_params") (param i32) (result i32)
    (i32.const 10) (i32.const 3) (local.get 0)
    (if (param i32 i32) (result i32) (then (i32.sub)) (else (i32.mul))))
  (func (export "loop_params") (param i32) (result i32)
    (i32.const 0) (local.get 0)
    (loop $l (param i32 i32) (result i32)
      (local.set 0)
      (if (param i32) (result i32) (i32.eqz (local.get 0))
        (then)
        (else (local.get 0) (i32.add) (i32.sub (local.get 0) (i32.const 1)) (br $l)))))
  (func $pair (param i64) (result i64 i32 i64)
    (local.get 0) (i32.wrap_i64 (local.get 0)) (i64.mul (local.get 0) (local.get 0)))
  (func (export "multi") (param i64) (result i64)
    (local i32)
    (call $pair (local.get 0)) (call $pair) (drop) (local.set 1) (drop) (drop)
    (i64.add (i64.extend_i32_u (local.get 1)))
    (i64.add (i64.const 0)))
  (func (export "brif_value") (param i32) (result i32)
    (block (result i32) (i32.const 5) (local.get 0) (br_if 0) (drop) (i32.const 6)))
  (func (export "select") (param i64 i64 i32) (result i64)
    (select (local.get 0) (local.get 1) (local.get 2)))
  (func (export "typed_select") (param i32) (result i32)
    (select (result i32) (i32.const 1) (i32.const 2) (local.get 0)))
  (func (export "nested_return") (param i32) (result i32)
    (block $out (loop (br_if $out (i32.eqz (local.get 0))) (return (i32.const 99))))
    (i32.const 0))
  (func (export "unreachable_tail") (param i32) (result i32)
    (if (local.get 0) (then (unreachable) (i32.const 1) (drop))) (i32.const 2))
)"""


def test_control_flow_matches_pyc(pins: None) -> None:
    wasm = wat(CONTROL)
    jit, pyc = build(wasm), build(wasm, "pyc")
    cases: list[tuple[str, tuple[int, ...]]] = [
        *(("table", (i,)) for i in (0, 1, 2, 3, 4, 5, 6, 100, -1)),
        *(("small", (i,)) for i in (0, 1, 2, -1)),
        *(("values", (i,)) for i in range(7)),
        *(("params", (a, b)) for a in (0, 1, 5) for b in (0, 7)),
        *(("if_params", (i,)) for i in (0, 1)),
        ("loop_params", (100,)), ("loop_params", (0,)),
        ("multi", (3,)), ("multi", (0x1_0000_0005,)),
        ("brif_value", (0,)), ("brif_value", (1,)),
        ("select", (1, 2, 0)), ("select", (1, 2, 5)), ("select", (-1, 2, 1)),
        ("typed_select", (0,)), ("typed_select", (3,)),
        ("nested_return", (0,)), ("nested_return", (3,)),
        ("unreachable_tail", (0,)), ("unreachable_tail", (1,)),
    ]  # fmt: skip
    for name, args in cases:
        assert outcome(jit, name, *args) == outcome(pyc, name, *args), (name, args)
    assert jit("table", 100) == [15] and jit("values", 0) == [1, 2] and jit("values", 1) == [40, 41]
    assert jit("values", 2) == jit("values", 9) == [30, 31]
    assert jit("loop_params", 100) == [5050]


def test_traps_are_never_dropped() -> None:
    """LESSONS 28: an operand that can trap is evaluated even when its value is discarded."""
    jit = build(
        wat("""(module (memory 1)
      (func (export "load") (param i32) (drop (i32.load (local.get 0))))
      (func (export "div") (param i32) (drop (i32.div_u (i32.const 1) (local.get 0))))
      (func (export "sel") (param i32) (result i32)
        (select (i32.const 1) (i32.load (local.get 0)) (i32.const 1)))
      (func (export "br") (param i32) (result i32)
        (block (result i32) (i32.const 1) (i32.rem_s (i32.const 1) (local.get 0)) (br 0))))""")
    )
    assert trap_of(jit, "load", 65536) is TrapCode.MEMORY_OUT_OF_BOUNDS
    assert jit("load", 65532) == []
    assert trap_of(jit, "div", 0) is TrapCode.INTEGER_DIVISION_BY_ZERO
    assert trap_of(jit, "sel", 65534) is TrapCode.MEMORY_OUT_OF_BOUNDS
    assert trap_of(jit, "br", 0) is TrapCode.INTEGER_DIVISION_BY_ZERO


RETURNS = """(module (memory 1)
  (func (export "swap") (param i32 i64) (result i64 i32) (local.get 1) (local.get 0))
  (func (export "swap_ret") (param i32 i32) (result i32 i32)
    (block (local.get 1) (local.get 0) (return)) (unreachable))
  (func (export "rot3") (param i32 i32 i32) (result i32 i32 i32)
    (local.get 2) (local.get 0) (local.get 1))
  (func (export "in_place") (param i32 i32) (result i32 i32) (local.get 0) (local.get 1))
  (func (export "mixed") (param i32 i32) (result i32 i64 i32 i32)
    (i32.add (local.get 0) (local.get 1)) (i64.const -5) (local.get 1)
    (i32.lt_s (local.get 0) (local.get 1)))
  (func (export "below") (param i32 i32) (result i32 i32)
    (i32.mul (local.get 0) (i32.const 3)) (local.get 1) (local.get 0) (return))
  (func (export "load_below") (param i32) (result i32)
    (i32.load (local.get 0)) (i32.const 7) (return))
  (func (export "div_below") (param i32) (result i32)
    (block (result i32) (i32.div_s (i32.const 1) (local.get 0)) (i32.const 7) (return)))
  (func (export "cmp") (param i32 i32) (result i32) (i32.lt_s (local.get 0) (local.get 1)))
  (func (export "high") (param i32 i32 i32) (result i32) (local.get 2))
  (func (export "v128") (param i32) (result v128 i32)
    (i32x4.splat (local.get 0)) (i32.add (local.get 0) (i32.const 1))))"""


def test_returns_match_pyc(pins: None) -> None:
    """S0007 "Returns": results go straight to slots 0..m (a compare on top, a slot source at or
    above m, a v128); with locals in slots, a swap falls back to flush + moves; a trapping entry
    below the results still traps (LESSONS 28)."""
    wasm = wat(RETURNS)
    jit, pyc = build(wasm), build(wasm, "pyc")
    cases: list[tuple[str, tuple[int, ...]]] = [
        ("swap", (3, -9)), ("swap_ret", (1, 2)), ("rot3", (1, 2, 3)), ("in_place", (4, 5)),
        ("mixed", (1, 2)), ("mixed", (5, -2)), ("below", (6, 7)),
        ("load_below", (0,)), ("load_below", (65536,)), ("div_below", (0,)), ("div_below", (2,)),
        ("cmp", (1, 2)), ("cmp", (2, 1)), ("high", (1, 2, 3)), ("v128", (7,)),
    ]  # fmt: skip
    for name, args in cases:
        assert outcome(jit, name, *args) == outcome(pyc, name, *args), (name, args)
    assert jit("rot3", 1, 2, 3) == [3, 1, 2] and jit("swap", 3, 9) == [9, 3]
    assert trap_of(jit, "load_below", 65536) is TrapCode.MEMORY_OUT_OF_BOUNDS
    assert trap_of(jit, "div_below", 0) is TrapCode.INTEGER_DIVISION_BY_ZERO


def test_deep_recursion_is_stack_overflow() -> None:
    jit = build(
        wat("""(module
      (func $f (export "f") (param i64) (result i64)
        (if (result i64) (i64.eqz (local.get 0)) (then (i64.const 0))
          (else (i64.add (i64.const 1) (call $f (i64.sub (local.get 0) (i64.const 1))))))))""")
    )
    assert jit("f", 1000) == [1000]
    assert trap_of(jit, "f", -1) is TrapCode.STACK_OVERFLOW
    assert jit("f", 10) == [10]  # still usable


def test_loop_polls_interrupt() -> None:
    store = StoreState()
    jit = build(wat('(module (func (export "spin") (loop (br 0))))'), store=store)
    t = threading.Timer(0.05, store.interrupt)
    t.start()
    start = time.monotonic()
    assert trap_of(jit, "spin") is TrapCode.INTERRUPT
    assert time.monotonic() - start < 5


# --------------------------------------------------------------------------- memory and globals
MEMORY = """(module (memory 1)
  (data (i32.const 8) "\\01\\80\\ff\\7f\\00\\00\\00\\80")
  (func (export "l8s") (param i32) (result i32) (i32.load8_s offset=8 (local.get 0)))
  (func (export "l8u") (param i32) (result i32) (i32.load8_u offset=8 (local.get 0)))
  (func (export "l16s") (param i32) (result i32) (i32.load16_s offset=8 (local.get 0)))
  (func (export "l16u") (param i32) (result i32) (i32.load16_u offset=8 (local.get 0)))
  (func (export "l32") (param i32) (result i32) (i32.load offset=8 (local.get 0)))
  (func (export "L8s") (param i32) (result i64) (i64.load8_s offset=8 (local.get 0)))
  (func (export "L8u") (param i32) (result i64) (i64.load8_u offset=8 (local.get 0)))
  (func (export "L16s") (param i32) (result i64) (i64.load16_s offset=8 (local.get 0)))
  (func (export "L16u") (param i32) (result i64) (i64.load16_u offset=8 (local.get 0)))
  (func (export "L32s") (param i32) (result i64) (i64.load32_s offset=8 (local.get 0)))
  (func (export "L32u") (param i32) (result i64) (i64.load32_u offset=8 (local.get 0)))
  (func (export "L64") (param i32) (result i64) (i64.load offset=8 (local.get 0)))
  (func (export "s8") (param i32 i64) (i64.store8 (local.get 0) (local.get 1)))
  (func (export "s16") (param i32 i64) (i64.store16 (local.get 0) (local.get 1)))
  (func (export "s32") (param i32 i64) (i64.store32 (local.get 0) (local.get 1)))
  (func (export "s64") (param i32 i64) (i64.store (local.get 0) (local.get 1)))
  (func (export "w8") (param i32 i32) (i32.store8 (local.get 0) (local.get 1)))
  (func (export "w16") (param i32 i32) (i32.store16 (local.get 0) (local.get 1)))
  (func (export "w32") (param i32 i32) (i32.store (local.get 0) (local.get 1)))
  (func (export "big") (param i32) (result i32) (i32.load offset=0xfffffff0 (local.get 0)))
  (func (export "const_addr") (result i64) (i64.load (i32.const 65528)))
  (func (export "const_oob") (result i32) (i32.load (i32.const 65533)))
  (func (export "const_huge") (result i32) (i32.load offset=4 (i32.const -1)))
  (func (export "store_imm") (param i32) (i64.store (local.get 0) (i64.const -2)))
)"""


def test_loads_and_stores_match_pyc(pins: None) -> None:
    wasm = wat(MEMORY)
    jit, pyc = build(wasm), build(wasm, "pyc")
    addrs = [0, 1, 2, 3, 4, 65520, 65524, 65527, 65528, 65530, 65535, 65536, -1, -8]
    loads = "l8s l8u l16s l16u l32 L8s L8u L16s L16u L32s L32u L64 big".split()
    for name in loads:
        for a in addrs:
            assert outcome(jit, name, a) == outcome(pyc, name, a), (name, a)
    for name in ("const_addr", "const_oob", "const_huge"):
        assert outcome(jit, name) == outcome(pyc, name), name
    v = 0x8899AABBCCDDEEFF
    for name in "s8 s16 s32 s64 w8 w16 w32 store_imm".split():
        for a in (100, 65528, 65532, 65535, 65536, -1):
            args = (a,) if name == "store_imm" else (a, v)
            got = outcome(jit, name, *args)
            assert got == outcome(pyc, name, *args), (name, a)
            assert jit("L64", 92) == pyc("L64", 92) and jit("L64", 65520) == pyc("L64", 65520)


ADDK = """(module (memory 1) (data (i32.const 0) "\\01\\02\\03\\04\\05\\06\\07\\08\\09")
  (func (export "o0") (param i32) (result i32) (i32.load (i32.add (local.get 0) (i32.const -4))))
  (func (export "o4") (param i32) (result i32)
    (i32.load offset=4 (i32.add (i32.const -4) (local.get 0))))
  (func (export "s4") (param i32 i32) (result i32)
    (i32.store offset=4 (i32.sub (i32.add (local.get 0) (i32.const 1)) (i32.const 5)) (local.get 1))
    (i32.load (i32.const 0)))
  (func (export "ops") (param i32 i32) (result i32)
    (i32.sub (i32.add (local.get 0) (i32.const 3)) (i32.sub (local.get 1) (i32.const 1))))
  (func (export "sel") (param i32 i32) (result i32)
    (select (i32.add (local.get 0) (i32.const 1)) (i32.add (local.get 1) (i32.const 2))
      (i32.lt_u (i32.mul (local.get 0) (local.get 1)) (i32.const 9))))
  (func (export "tee") (param i32) (result i32)
    (i32.add (i32.add (local.get 0) (i32.const 8))
      (i32.sub (local.get 0) (local.tee 0 (i32.add (local.get 0) (i32.const 4))))))
  (func (export "set") (param i32) (result i32) (local i32)
    (local.set 1 (i32.add (local.get 0) (i32.const -1)))
    (i32.add (local.get 0) (i32.const 1)) (local.set 0 (i32.const 0)) (local.get 1) (i32.add))
  (func (export "br") (param i32) (result i32)
    (block (result i32) (br_if 0 (i32.add (local.get 0) (i32.const 5)) (local.get 0))
      (drop) (i32.store8 (i32.add (local.get 0) (i32.const 1))
        (i32.add (local.get 0) (i32.const 2)))
      (i32.load8_u (i32.add (local.get 0) (i32.const 1))))))"""


def test_addk_wraps_before_the_offset_and_matches_pyc(pins: None) -> None:
    """S0007 K: local + k (k = 2**32 - 4) wraps in 32 bits before the memarg offset is added (k is
    never folded into the offset); ADDK entries as operands of one instruction (LESSONS 32), across
    a tee and a set of their local, as branch values and as store values."""
    wasm = wat(ADDK)
    jit, pyc = build(wasm), build(wasm, "pyc")
    oob = TrapCode.MEMORY_OUT_OF_BOUNDS
    assert trap_of(jit, "o0", 0) is oob and jit("o0", 4) == [0x04030201]
    assert trap_of(jit, "o4", 0) is oob and jit("o4", 4) == [0x08070605]  # 0xFFFFFFFC + 4 = 2**32
    assert trap_of(jit, "s4", 0, 7) is oob and trap_of(pyc, "s4", 0, 7) is oob
    cases = [("o0", (a,)) for a in (0, 3, 4, 8, 65536, 65539, 65540)]
    cases += [("o4", (a,)) for a in (0, 3, 4, 65532, 65536)]
    cases += [("s4", (a, 0x11223344)) for a in (0, 4, 65532, 65533, 65536, M32)]
    cases += [(n, (a, b)) for n in ("ops", "sel") for a in (0, 1, 3, M32) for b in (0, 2, M32)]
    cases += [(n, (a,)) for n in ("tee", "set", "br") for a in (0, 1, 7, 65534, M32)]
    for name, args in cases:
        assert outcome(jit, name, *args) == outcome(pyc, name, *args), (name, args)
    assert jit("s4", 4, 7) == [0x04030201] and jit("o0", 8) == [7]


def test_globals_and_imported_globals() -> None:
    store = StoreState()
    a = build(wat("""(module
      (global $g (export "g") (mut i64) (i64.const -5))
      (global (export "f") (mut f32) (f32.const nan:0x200001))
      (global $c i32 (i32.const 7))
      (func (export "inc") (result i64)
        (global.set $g (i64.add (global.get $g) (i64.extend_i32_u (global.get $c))))
        (global.get $g)))"""),
        store=store)  # fmt: skip
    t = base.TARGETS[JIT]()
    mod = parse(
        wat("""(module (import "a" "g" (global $g (mut i64)))
      (import "a" "f" (global $f (mut f32)))
      (global $h i64 (i64.add (i64.const 1) (i64.const 2)))
      (func (export "dbl") (result i64) (global.set $g (i64.mul (global.get $g) (i64.const 2)))
        (global.get $g))
      (func (export "fbits") (result i32) (i32.reinterpret_f32 (global.get $f)))
      (func (export "h") (result i64) (global.get $h)))""")
    )
    validate(mod)
    ext = link.resolve(mod, {"a": link.Provider(a.mod, a.inst)})
    b = Inst(mod, t.instantiate(t.compile(mod), ext, store))
    assert a("inc") == [2] and b("dbl") == [4] and a("inc") == [11]
    assert a.inst.global_get(0) == 11 and b.inst.global_get(0) == 11
    assert b("fbits") == [0x7FA00001] and b("h") == [3]


def test_wrap_and_extend_clear_the_upper_half() -> None:
    # a computed i64 sits in the result register; wrap/extend_i32_u reuse it (review of 3b)
    wasm = wat("""(module (global $g (mut i32) (i32.const 0))
      (func (export "ret") (param i64 i64) (result i32)
        (i32.wrap_i64 (i64.add (local.get 0) (local.get 1))))
      (func (export "glob") (param i64) (result i32)
        (global.set $g (i32.wrap_i64 (i64.shl (local.get 0) (i64.const 4)))) (global.get $g))
      (func (export "ext") (param i64) (result i64)
        (i64.extend_i32_u (i32.wrap_i64 (i64.mul (local.get 0) (i64.const 3)))))
      (func (export "use") (param i64) (result i64)
        (i64.extend_i32_u
          (i32.add (i32.wrap_i64 (i64.add (local.get 0) (i64.const 0)))
            (i32.const 0)))))""")  # fmt: skip
    for target in (JIT, "pyc"):
        f = build(wasm, target)
        assert f("ret", 0x100000001, 0) == [1], target
        assert f("glob", 0x1000000001) == [0x10], target
        assert f("ext", 0x155555556) == [(0x155555556 * 3) & M32], target
        assert f("use", 0xFFFFFFFF00000002) == [2], target


def test_floats_as_data_keep_bits() -> None:
    wasm = wat("""(module (memory 1)
      (func (export "f32") (result i32) (f32.store (i32.const 4) (f32.const -nan:0x123))
        (i32.load (i32.const 4)))
      (func (export "f64") (param f64) (result i64) (local f64) (local.set 1 (local.get 0))
        (f64.store (i32.const 8) (local.get 1)) (i64.reinterpret_f64 (f64.load (i32.const 8))))
      (func (export "sel") (param f32 f32 i32) (result f32)
        (select (local.get 0) (local.get 1) (local.get 2))))""")
    jit = build(wasm)
    assert jit("f32") == [0xFF800123]  # a signalling NaN keeps its bits (pyc quiets it, D28)
    for bits in (0x7FF0000000000001, 0x3FF0000000000000, 0xFFF8000000000000):
        assert jit("f64", bits) == [bits]
    assert jit("sel", 0x7F800001, 5, 1) == [0x7F800001] and jit("sel", 1, 0x7F800001, 0) == [
        0x7F800001
    ]


# --------------------------------------------------------------------------- host calls
def test_host_calls_keep_pinned_locals() -> None:
    """Eight pinned locals live across host calls and a nested entry (LESSONS 2)."""
    store = StoreState()
    holder: list[Inst] = []

    def host(args: Sequence[Raw]) -> Sequence[Raw]:
        x = args[0]
        if x % 3 == 0:  # re-enter: wasm -> host -> wasm
            return [holder[0]("leaf", x)[0]]
        return [(x * 7) & M32]

    imports: dict[str, Mapping[str, HostFunc]] = {"env": {"h": host}}
    jit = build(wat("""(module (import "env" "h" (func $h (param i32) (result i32)))
      (func (export "leaf") (param i32) (result i32) (i32.add (local.get 0) (i32.const 1000)))
      (func (export "run") (param i32) (result i32)
        (local i32 i32 i32 i32 i32 i32 i32 i32)
        (loop $l
          (local.set 1 (i32.add (local.get 1) (call $h (local.get 0))))
          (local.set 2 (i32.add (local.get 2) (i32.const 2)))
          (local.set 3 (i32.add (local.get 3) (i32.const 3)))
          (local.set 4 (i32.add (local.get 4) (i32.const 4)))
          (local.set 5 (i32.xor (local.get 5) (local.get 0)))
          (local.set 6 (i32.add (local.get 6) (local.get 1)))
          (local.set 7 (i32.sub (local.get 7) (i32.const 1)))
          (local.set 8 (i32.add (local.get 8) (local.get 2)))
          (br_if $l (local.tee 0 (i32.sub (local.get 0) (i32.const 1)))))
        (i32.add (local.get 1) (i32.add (local.get 2) (i32.add (local.get 3)
          (i32.add (local.get 4) (i32.add (local.get 5) (i32.add (local.get 6)
          (i32.add (local.get 7) (local.get 8))))))))))"""),
        imports=imports, store=store)  # fmt: skip
    holder.append(jit)
    assert jit("run", 50) == [_host_reference(50)]


def _host_reference(n: int) -> int:
    l1 = l2 = l3 = l4 = l5 = l6 = l7 = l8 = 0
    x = n
    while True:
        h = x + 1000 if x % 3 == 0 else x * 7
        l1, l2, l3, l4 = (l1 + h) & M32, l2 + 2, l3 + 3, l4 + 4
        l5, l6, l7, l8 = l5 ^ x, (l6 + l1) & M32, (l7 - 1) & M32, (l8 + l2) & M32
        x -= 1
        if not x:
            break
    return (l1 + l2 + l3 + l4 + l5 + l6 + l7 + l8) & M32


def test_host_exception_and_trap_through_compiled_code() -> None:
    def boom(args: Sequence[Raw]) -> Sequence[Raw]:
        raise ValueError("boom")

    def trap(args: Sequence[Raw]) -> Sequence[Raw]:
        raise Trap(TrapCode.UNREACHABLE)

    jit = build(wat("""(module (import "env" "boom" (func $b)) (import "env" "trap" (func $t))
      (func (export "b") (call $b)) (func (export "t") (call $t)) (export "raw" (func $b)))"""),
        imports={"env": {"boom": boom, "trap": trap}})  # fmt: skip
    with pytest.raises(ValueError, match="boom"):
        jit("b")
    with pytest.raises(ValueError, match="boom"):
        jit("raw")
    assert trap_of(jit, "t") is TrapCode.UNREACHABLE


# --------------------------------------------------------------------------- module level
@pytest.mark.parametrize(
    "text,feature",
    [
        ("(memory 1) (memory 1)", "module not fully validated"),  # several memories: compiled
    ],
)
def test_unsupported_names_the_feature(text: str, feature: str) -> None:
    mod = parse(wat(f"(module {text})"))  # not validated: the frontend decodes nothing else
    t = base.TARGETS[JIT]()  # the JIT refuses (v128, GC types, exceptions fail in the reader)
    assert t.supports(mod) == feature
    with pytest.raises(Unsupported) as e:
        t.compile(mod)
    assert e.value.feature == feature


def test_isa_check_on_compiled_code(tmp_path: Path) -> None:
    """D24: the compiled code (data area excluded) has only x86-64-v1 instructions."""
    from tests.jit.test_encoder import OBJDUMP

    if JIT != "jit-x86_64":
        pytest.skip("tools/isa_check.py checks x86-64 code (aarch64: test_isa_aarch64.py)")
    need(OBJDUMP, "GNU objdump")
    mod = parse(
        wat(CONTROL[: CONTROL.rindex(")")] + "(memory 1)" + MEMORY[MEMORY.index("(func") :])
    )
    validate(mod)
    c = base.TARGETS[JIT]().compile(mod)
    assert isinstance(c, Compiled) and c.data_start < len(c.code)  # a jump table is there
    blob = tmp_path / "code.bin"
    blob.write_bytes(c.code)
    cmd = [sys.executable, str(ROOT / "tools" / "isa_check.py"), str(blob)]
    p = subprocess.run(
        [*cmd, "--data", f"{c.data_start}:{len(c.code)}"], capture_output=True, text=True
    )
    assert p.returncode == 0, p.stdout + p.stderr


def test_core_names_no_architecture() -> None:
    """D26: the shared core imports nothing architecture- or target-specific."""
    from waboros.targets.jit.common import compiler  # the installed file: no checkout on Android

    src = Path(compiler.__file__).read_text(encoding="utf-8")
    assert "x86" not in src and "targets.pyc" not in src


def test_instantiating_another_architectures_code_raises() -> None:
    """Compiling for a foreign architecture is allowed (D35); instantiating it must raise, never
    run the code (on a 32-bit or wasm host it crashed the process: platforms run, WAT cache)."""
    other = "jit-aarch64" if JIT == "jit-x86_64" else "jit-x86_64"
    t = base.TARGETS[other]()
    mod = parse(wat('(module (func (export "f") (result i32) i32.const 7))'))
    validate(mod)
    compiled = t.compile(mod)
    with pytest.raises(RuntimeError, match="cannot run here"):
        t.instantiate(compiled, link.resolve(mod, {}), StoreState())
