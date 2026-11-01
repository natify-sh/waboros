# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
"""Several memories on the machine's JIT target: memory 0 stays in registers (x86-64: r13/r14,
aarch64: x26/x25), any other is read from its context words at each access (S0003). Results and
traps are compared with Wasmtime; the sharing case checks exact expectations."""

import importlib.util
import random
from typing import Any, cast

import pytest

from tests.jit import native, need, test_compiler
from tests.jit.test_compiler import JIT, build, outcome, trap_of, wat
from tests.jit.test_float import Oracle
from tests.jit.test_tables import provider
from w2py.frontend.binary import parse
from w2py.frontend.validate import validate
from w2py.runtime.store import StoreState
from w2py.targets import base
from w2py.targets.base import TrapCode

LOADS = ["i32.load", "i64.load", "f32.load", "f64.load", "i32.load8_s", "i32.load8_u",
         "i32.load16_s", "i32.load16_u", "i64.load8_s", "i64.load8_u", "i64.load16_s",
         "i64.load16_u", "i64.load32_s", "i64.load32_u"]  # fmt: skip
pins = test_compiler.pins  # the fixture: pinned locals and all locals in slots
STORES = {"i32.store": "i32", "i64.store": "i64", "i32.store8": "i32", "i32.store16": "i32",
          "i64.store8": "i64", "i64.store16": "i64", "i64.store32": "i64"}  # fmt: skip


def _three() -> str:
    """$m0 1 page, $m1 1..3 pages, $m2 2 pages: every load/store width on $m2 (register address,
    constant address, offset >= 2**31), size/grow of each, copy between and within memories,
    fill/init on $m1, and a checksum of a range of each memory."""
    out = ['(module (memory $m0 1) (memory $m1 1 3) (memory $m2 2)',
           '(data $d (memory $m1) (i32.const 8) "abcdefgh") (data $p "0123456789")']  # fmt: skip
    for op in LOADS:
        t = op[:3]
        r = "i32" if t in ("i32", "f32") else "i64"
        for name, arg, addr in (
            ("", "(param i32)", "(local.get 0)"),
            ("/k", "", "(i32.const 131068)"),
            ("/o", "(param i32)", "offset=0x80000000 (local.get 0)"),
        ):
            e = f"({op} $m2 {addr})"
            e = f"({r}.reinterpret_{t} {e})" if t[0] == "f" else e
            out.append(f'(func (export "{op}{name}") {arg} (result {r}) {e})')
    for op, t in STORES.items():
        out.append(f'(func (export "{op}") (param i32 {t}) ({op} $m2 (local.get 0) (local.get 1)))')
        out.append(f'(func (export "{op}/k") (param {t}) ({op} $m2 (i32.const 131064) '
                   "(local.get 0)))")  # fmt: skip
    for m in ("$m0", "$m1", "$m2"):
        out.append(f'(func (export "size{m}") (result i32) (memory.size {m}))')
        out.append(f'(func (export "grow{m}") (param i32) (result i32) '
                   f"(memory.grow {m} (local.get 0)))")  # fmt: skip
        out.append(
            f'(func (export "sum{m}") (param i32 i32) (result i64) (local i64) '
            "(block (loop (br_if 1 (i32.eqz (local.get 1))) "
            "(local.set 2 (i64.add (i64.mul (local.get 2) (i64.const 31)) "
            f"(i64.load8_u {m} (local.get 0)))) "
            "(local.set 0 (i32.add (local.get 0) (i32.const 1))) "
            "(local.set 1 (i32.sub (local.get 1) (i32.const 1))) (br 0))) (local.get 2))"
        )
        out.append(f'(func (export "fill{m}") (param i32 i32 i32) '
                   f"(memory.fill {m} (local.get 0) (local.get 1) (local.get 2)))")  # fmt: skip
        for s in ("$m0", "$m1", "$m2"):
            out.append(
                f'(func (export "copy{m}{s}") (param i32 i32 i32) '
                f"(memory.copy {m} {s} (local.get 0) (local.get 1) (local.get 2)))"
            )
    out.append('(func (export "init") (param i32 i32 i32) '
               "(memory.init $m1 $p (local.get 0) (local.get 1) (local.get 2)))")  # fmt: skip
    out.append('(func (export "drop") (data.drop $p)))')
    return "\n".join(out)


@pytest.fixture(autouse=True)
def _jit() -> None:
    native(JIT)
    need(importlib.util.find_spec("wasmtime") is not None, "wasmtime")


@pytest.mark.usefixtures("pins")
def test_every_width_on_memory_2_and_each_bound() -> None:
    """Each memory traps at its own bound: $m2 (2 pages) at 131072, $m1 at 65536 until grown,
    $m0 never sees $m1's or $m2's stores."""
    wasm = wat(_three())
    jit, ref = build(wasm), Oracle(wasm)
    end = 2 << 16
    addrs = [0, 1, 7, 65535, 65536, end - 9, end - 8, end - 4, end - 3, end - 2, end - 1, end,
             0x7FFFFFFF, 0xFFFFFFFF]  # fmt: skip
    vals = {"i32": [0x89ABCDEF, 0x7F], "i64": [0x0123456789ABCDEF, 0xFEDCBA9876543210]}
    for op, t in STORES.items():
        for a in addrs:
            for v in vals[t]:
                assert outcome(jit, op, a, v) == ref(op, a, v), (op, a, v)
                for ld in LOADS:
                    assert outcome(jit, ld, a) == ref(ld, a), (op, a, ld)
                    assert outcome(jit, ld + "/o", a) == ref(ld + "/o", a), (op, a, ld)
        assert outcome(jit, op + "/k", 0x55AA) == ref(op + "/k", 0x55AA), op
        assert [outcome(jit, ld + "/k") for ld in LOADS] == [ref(ld + "/k") for ld in LOADS]
    for m in ("$m0", "$m1", "$m2"):
        assert jit(f"sum{m}", 0, 256) == ref(f"sum{m}", 0, 256), m
        assert jit(f"sum{m}", 65280, 256) == ref(f"sum{m}", 65280, 256), m
    assert jit("sum$m0", 0, 256) == jit("sum$m0", 65280, 256) == [0]  # untouched
    assert trap_of(jit, "i32.load", end - 3) is TrapCode.MEMORY_OUT_OF_BOUNDS
    assert jit("grow$m1", 1) == [1] and jit("sum$m1", 65536, 4) == [0]
    assert trap_of(jit, "sum$m1", 2 << 16, 1) is TrapCode.MEMORY_OUT_OF_BOUNDS
    assert trap_of(jit, "sum$m0", 65535, 2) is TrapCode.MEMORY_OUT_OF_BOUNDS


@pytest.mark.usefixtures("pins")
def test_three_memories_match_wasmtime() -> None:
    """A random sequence of size/grow (each memory, $m1 up to its maximum), fill/init on $m1,
    copies between every pair of memories (both directions) and within one (overlapping both
    ways): the same results and traps, the memories compared after each step."""
    wasm = wat(_three())
    jit, ref = build(wasm), Oracle(wasm)
    rnd = random.Random(7)
    ms = ("$m0", "$m1", "$m2")
    near = [0, 1, 7, 8, 9, 16, 17, 100, 65535, 65536, 65520, 131000, 0xFFFFFFFF]
    for step in range(1200):
        k = rnd.randrange(10)
        if k < 5:
            name = f"copy{rnd.choice(ms)}{rnd.choice(ms)}"
        elif k < 7:
            name = rnd.choice(["fill$m1", "fill$m2", "init"])
        elif k < 9:
            name = rnd.choice(["grow$m0", "grow$m1", "grow$m2"])
        else:
            name = "drop" if step > 900 else "init"
        if name.startswith("grow"):
            args = [rnd.choice([0, 1, 2, 0xFFFFFFFF])]
        elif name == "drop":
            args = []
        else:
            args = [rnd.choice(near) if rnd.random() < 0.2 else rnd.randrange(64) for _ in range(3)]
        assert outcome(jit, name, *args) == ref(name, *args), (step, name, args)
        for m in ms:
            assert jit(f"size{m}") == ref(f"size{m}"), (step, m)
            for at in (0, 65472):
                assert jit(f"sum{m}", at, 64) == ref(f"sum{m}", at, 64), (step, name, args, m)


EXPORTER = """(module (memory (export "m0") 1) (memory (export "shared") 1 4)
  (func (export "grow") (param i32) (result i32) (memory.grow 1 (local.get 0))))"""
IMPORTER = """(module (import "a" "shared" (memory $s 1 4))
  (import "a" "grow" (func $agrow (param i32) (result i32))) (memory $own 1)
  (export "own" (memory $own))
  (func (export "grow") (param i32) (result i32) (memory.grow $s (local.get 0)))
  (func (export "agrow") (param i32) (result i32) (call $agrow (local.get 0)))
  (func (export "size") (result i32) (memory.size $s))
  (func (export "put") (param i32 i32) (i32.store $s (local.get 0) (local.get 1)))
  (func (export "get") (param i32) (result i32) (i32.load $s (local.get 0)))
  (func (export "grow_get") (param i32) (result i32)
    (drop (call $agrow (i32.const 1))) (i32.load $s (local.get 0))))"""


def test_two_instances_share_an_imported_second_memory() -> None:
    """The exporter's memory 1 is memory 0 of both importers; growing it through any instance
    (directly, or by a call into the exporter in the middle of a function) is seen by all of
    them at the next access, with no reload; `limits` reports each exported memory."""
    store = StoreState()
    a = build(wat(EXPORTER), store=store)
    x = build(wat(IMPORTER), imports={"a": provider(a)}, store=store)
    y = build(wat(IMPORTER), imports={"a": provider(a)}, store=store)
    assert x("put", 100, 0x1234) is not None and y("get", 100) == [0x1234]
    assert trap_of(y, "get", 65536) is TrapCode.MEMORY_OUT_OF_BOUNDS
    assert x("grow", 1) == [1] and y("size") == [2] and a("grow", 0) == [2]
    y("put", 65536, 7)
    assert x("get", 65536) == [7]
    assert y("grow_get", 2 << 16) == [0] and x("size") == [3]  # grown by the call, then read
    assert x("agrow", 1) == [3] and y("grow", 1) == [0xFFFFFFFF] and y("size") == [4]
    ai, xi = cast(Any, a.inst), cast(Any, x.inst)
    assert ai.limits("shared") == (4, 4) and ai.limits("m0") == (1, None)
    assert xi.limits("own") == (1, None)
    assert xi.export("own").value is not ai.memories[1] and xi.memories[0] is ai.memories[1]


def test_memory_0_code_does_not_depend_on_other_memories() -> None:
    """Memory 0's accesses, size, fill and copy stay in its registers: a module compiles to the same
    bytes whether or not it declares a second (unused) memory."""
    body = """(func (export "f") (param i32 i32) (result i32)
      (i32.store (local.get 0) (i32.load offset=4 (local.get 1)))
      (i64.store8 (i32.const 9) (i64.load16_s (i32.const 70000)))
      (memory.fill (local.get 0) (local.get 1) (i32.const 3))
      (memory.copy (local.get 1) (local.get 0) (i32.const 5))
      (memory.size))"""
    codes = []
    for mems in ("(memory 1)", "(memory 1) (memory 2)"):
        mod = parse(wat(f"(module {mems} {body})"))
        validate(mod)
        codes.append(base.TARGETS[JIT]().compile(mod).code)  # pyright: ignore[reportAttributeAccessIssue]
    assert codes[0] == codes[1]
