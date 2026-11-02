# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
"""Tail calls and typed function references on the machine's JIT target (JIT step 3e; aarch64:
step 4e). Results and trap codes are compared with Wasmtime where the module stands alone;
linking cases (another instance, host functions) check exact expectations."""

import importlib.util
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, cast

import pytest

from tests.jit import native, need
from tests.jit.test_compiler import JIT, ROOT, Inst, build, outcome, pins, trap_of, wat
from tests.jit.test_float import Oracle
from tests.jit.test_tables import provider
from waboros.frontend.binary import parse
from waboros.frontend.validate import validate
from waboros.runtime.store import StoreState
from waboros.targets import base
from waboros.targets.base import Raw, Trap, TrapCode
from waboros.targets.jit.common.compiler import Compiled
from waboros.targets.jit.common.context import TAIL_SWITCH
from waboros.targets.jit.common.instance import JitFunc
from waboros.targets.jit.x86_64 import JitX86_64Target
from waboros.targets.jit.x86_64.abi import bridge, entry, sp_stub, tail_switch
from waboros.targets.jit.x86_64.encoder import Asm

__all__ = ["pins"]  # the fixture, used by name


@pytest.fixture(autouse=True)
def _jit() -> None:
    native(JIT)  # only this machine's code may run (x86-64 code is a SIGILL on arm64)


def _config() -> Any:
    import wasmtime

    c = wasmtime.Config()
    c.wasm_tail_call = True
    c.wasm_function_references = True
    return c


def _oracle(wasm: bytes) -> Oracle:
    need(importlib.util.find_spec("wasmtime") is not None, "wasmtime")
    return Oracle(wasm, _config())


def _small_thread(body: Callable[[], None], size: int = 256 << 10) -> None:
    """Run `body` on a thread with a small native stack; re-raise what it raised."""
    out: list[BaseException] = []

    def run() -> None:
        try:
            body()
        except BaseException as e:  # reported in the main thread
            out.append(e)

    old = threading.stack_size(size)
    try:
        t = threading.Thread(target=run)
        t.start()
        t.join()
    finally:
        threading.stack_size(old)
    if out:
        raise out[0]


DEEP = """(module
  (type $t (func (param i64) (result i64)))
  (table $tab 2 funcref)
  (elem (table $tab) (i32.const 0) func $ieven $iodd)
  (elem declare func $reven $rodd)
  (func $count (export "count") (param i64) (result i64)
    (if (result i64) (i64.eqz (local.get 0)) (then (local.get 0))
      (else (return_call $count (i64.sub (local.get 0) (i64.const 1))))))
  (func $plain (export "plain") (param i64) (result i64)
    (if (result i64) (i64.eqz (local.get 0)) (then (local.get 0))
      (else (call $plain (i64.sub (local.get 0) (i64.const 1))))))
  (func $even (export "even") (param i64) (result i64)
    (if (result i64) (i64.eqz (local.get 0)) (then (i64.const 44))
      (else (return_call $odd (i64.sub (local.get 0) (i64.const 1))))))
  (func $odd (param i64) (result i64)
    (if (result i64) (i64.eqz (local.get 0)) (then (i64.const 99))
      (else (return_call $even (i64.sub (local.get 0) (i64.const 1))))))
  (func $ieven (export "ieven") (type $t)
    (if (result i64) (i64.eqz (local.get 0)) (then (i64.const 44))
      (else (return_call_indirect $tab (type $t)
        (i64.sub (local.get 0) (i64.const 1)) (i32.const 1)))))
  (func $iodd (type $t)
    (if (result i64) (i64.eqz (local.get 0)) (then (i64.const 99))
      (else (return_call_indirect $tab (type $t)
        (i64.sub (local.get 0) (i64.const 1)) (i32.const 0)))))
  (func $reven (export "reven") (type $t)
    (if (result i64) (i64.eqz (local.get 0)) (then (i64.const 44))
      (else (return_call_ref $t (i64.sub (local.get 0) (i64.const 1)) (ref.func $rodd)))))
  (func $rodd (type $t)
    (if (result i64) (i64.eqz (local.get 0)) (then (i64.const 99))
      (else (return_call_ref $t (i64.sub (local.get 0) (i64.const 1)) (ref.func $reven)))))
  (func $spin (export "spin") (return_call $spin)))"""


def test_tail_recursion_runs_in_constant_stacks() -> None:
    """1e6 tail calls (direct, mutual, through a table, through references) on a 256 KiB thread:
    1e6 ordinary frames would overflow both the native budget and the 1 MiB value stack."""
    wasm = wat(DEEP)
    ref = _oracle(wasm)  # on this thread: Wasmtime needs more native stack than 256 KiB
    cases = [(name, n) for name in ("count", "even", "ieven", "reven")
             for n in (0, 1, 7, 1_000_000, 1_000_001)]  # fmt: skip
    expected = [ref(name, n) for name, n in cases]

    def body() -> None:
        jit = build(wasm)
        assert trap_of(jit, "plain", 1_000_000) is TrapCode.STACK_OVERFLOW
        assert [outcome(jit, name, n) for name, n in cases] == expected

    _small_thread(body)


def test_tail_call_loop_polls_interrupt() -> None:
    store = StoreState()
    jit = build(wat(DEEP), store=store)
    t = threading.Timer(0.05, store.interrupt)
    t.start()
    start = time.monotonic()
    assert trap_of(jit, "spin") is TrapCode.INTERRUPT
    assert time.monotonic() - start < 5


SHAPES = """(module
  (type $five (func (param i32 i64 f64 i32 i64) (result i64)))
  (type $one (func (param i32) (result i64)))
  (table $tab 2 funcref)
  (elem (table $tab) (i32.const 0) func $wide $narrow)
  (func $wide (type $five)
    (i64.add (i64.add (i64.extend_i32_u (local.get 0)) (local.get 1))
      (i64.add (i64.trunc_f64_s (local.get 2))
        (i64.mul (i64.extend_i32_s (local.get 3)) (local.get 4)))))
  (func $narrow (type $one) (i64.div_u (i64.const 1000) (i64.extend_i32_u (local.get 0))))
  ;; fewer parameters than the callee; pinned locals are the arguments
  (func (export "grow") (param i32) (result i64) (local i32 i64 i32)
    (local.set 1 (i32.mul (local.get 0) (i32.const 3)))
    (local.set 2 (i64.extend_i32_u (i32.add (local.get 0) (i32.const 1))))
    (local.set 3 (i32.sub (i32.const 0) (local.get 0)))
    (return_call $wide (local.get 1) (local.get 2) (f64.const 2.5) (local.get 3) (local.get 2)))
  ;; more parameters than the callee, a value left below the arguments; traps in the callee
  (func (export "shrink") (param i32 i64 i64 i32 i32 i32) (result i64) (local i32)
    (local.set 6 (i32.add (local.get 0) (local.get 5)))
    (i64.const 99)
    (return_call $narrow (local.get 6)))
  ;; the same through the table, with a computed index and arguments
  (func (export "igrow") (param i32 i32) (result i64)
    (return_call_indirect $tab (type $five) (local.get 0) (i64.const -5) (f64.const -7.9)
      (i32.add (local.get 0) (i32.const 1)) (i64.const 3) (i32.and (local.get 1) (i32.const 3))))
  (func (export "ishrink") (param i32 i32) (result i64)
    (return_call_indirect $tab (type $one) (local.get 0) (local.get 1))))"""


def test_return_call_argument_shapes_match_wasmtime(pins: None) -> None:
    """More and fewer arguments than the caller, from pinned locals and computed values, with
    values left on the stack below them; traps of the callee and of the table lookup."""
    wasm = wat(SHAPES)
    jit, ref = build(wasm), _oracle(wasm)
    for x in (0, 1, 5, 0x7FFFFFFF, 0xFFFFFFFF):
        assert outcome(jit, "grow", x) == ref("grow", x), x
        assert outcome(jit, "shrink", x, 1, 2, 3, 4, 0) == ref("shrink", x, 1, 2, 3, 4, 0), x
        for i in (0, 1, 2, 3, 4):  # 1, 3: wrong signature; 2, 3: out of bounds
            assert outcome(jit, "igrow", x, i) == ref("igrow", x, i), (x, i)
            assert outcome(jit, "ishrink", x, i) == ref("ishrink", x, i), (x, i)
    assert outcome(jit, "shrink", 0, 0, 0, 0, 0, 0) is TrapCode.INTEGER_DIVISION_BY_ZERO


# A and B share A's table: element 0 is A's ping, element 1 B's pong. Each reads its own memory
# and global at the end of the chain; `run` reads A's memory and global again after the chain
# returns to its `call` (a direct call: it relies on r15/r13/r14 being A's afterwards).
CROSS_A = """(module
  (type $p (func (param i32 i32) (result i32)))
  (memory 1) (data (i32.const 0) "\\0a")
  (global $g (mut i32) (i32.const 1000))
  (table $t (export "t") 2 funcref)
  (elem (table $t) (i32.const 0) func $ping)
  (func $ping (type $p)
    (if (result i32) (i32.eqz (local.get 0))
      (then (i32.add (i32.load8_u (i32.const 0)) (global.get $g)))
      (else (return_call_indirect $t (type $p)
        (i32.sub (local.get 0) (i32.const 1)) (local.get 1) (i32.const 1)))))
  (func (export "run") (param i32 i32) (result i32)
    (i32.add (call $ping (local.get 0) (local.get 1))
      (i32.add (i32.load8_u (i32.const 0)) (global.get $g))))
  (func (export "run_indirect") (param i32 i32) (result i32)
    (i32.add (call_indirect $t (type $p) (local.get 0) (local.get 1) (i32.const 1))
      (i32.add (i32.load8_u (i32.const 0)) (global.get $g)))))"""
CROSS_B = """(module
  (type $p (func (param i32 i32) (result i32)))
  (import "a" "t" (table $t 2 funcref))
  (import "env" "boom" (func $boom (param i32 i32) (result i32)))
  (memory 1) (data (i32.const 0) "\\64")
  (global $g (mut i32) (i32.const 5000))
  (elem (table $t) (i32.const 1) func $pong)
  (func $pong (type $p)
    (if (result i32) (i32.eqz (local.get 0))
      (then
        (if (i32.eq (local.get 1) (i32.const 1)) (then unreachable))
        (if (i32.eq (local.get 1) (i32.const 2))
          (then (return_call $boom (local.get 0) (local.get 1))))
        (i32.add (i32.load8_u (i32.const 0)) (global.get $g)))
      (else (return_call_indirect $t (type $p)
        (i32.sub (local.get 0) (i32.const 1)) (local.get 1) (i32.const 0))))))"""


def test_tail_calls_across_instances() -> None:
    """A -> B -> A -> ... of return_call_indirect, 1e5 hops on a 256 KiB thread: the shared
    tail_switch trampoline (D30) keeps the native stack constant (a restore frame per hop would
    need 1.6 MiB). The chain ends in A (10 + 1000) or B (100 + 5000); the caller then sees its
    own memory and global. A chain ending in a trap or a host exception unwinds through the
    restore frame, and both instances stay usable."""

    def boom(args: Sequence[Raw]) -> list[Raw]:
        raise ValueError("boom")

    def body() -> None:
        store = StoreState()
        a = build(wat(CROSS_A), store=store)
        b = build(wat(CROSS_B), imports={"a": provider(a), "env": {"boom": boom}}, store=store)
        tramp = cast(Any, base.TARGETS[JIT]()).trampolines()
        for i in (a, b):
            assert cast(Any, i.inst).ctx[TAIL_SWITCH] == tramp.tail_switch
        mine, theirs = 10 + 1000, 100 + 5000
        for n in (0, 1, 2, 3, 100_000, 100_001):
            end = mine if n % 2 == 0 else theirs
            assert a("run", n, 0) == [end + mine], n
            assert a("run_indirect", n, 0) == [(theirs if n % 2 == 0 else mine) + mine], n
        assert trap_of(a, "run", 100_001, 1) is TrapCode.UNREACHABLE
        with pytest.raises(ValueError, match="boom"):
            a("run", 100_001, 2)
        assert a("run", 100_001, 0) == [theirs + mine]

    _small_thread(body)


def test_return_call_to_host_functions() -> None:
    """call and return_call of a host import go through its record, the module's thunk (results,
    trap, exception), as return_call_indirect does. A host function reached by a tail call (the
    thunk, jumped to) re-enters wasm and grows the memory: the caller's direct call site does not
    reload the memory size, so the thunk does."""
    cell: list[Inst] = []

    def h(args: Sequence[Raw]) -> list[Raw]:
        if args[0] == 13:
            raise Trap(TrapCode.UNREACHABLE)
        if args[0] == 14:
            raise ValueError("host")
        if args[0] == 15:
            cell[0]("grow")
        return [(args[0] * 2) & 0xFFFFFFFF]

    jit = build(
        wat("""(module
      (type $h (func (param i32) (result i32)))
      (import "env" "h" (func $h (type $h)))
      (memory 1)
      (table 1 funcref) (elem (i32.const 0) func $h)
      (func (export "grow") (drop (memory.grow (i32.const 1))))
      (func (export "call") (param i32) (result i32) (call $h (local.get 0)))
      (func $direct (export "direct") (param i32) (result i32) (return_call $h (local.get 0)))
      (func $tail (param i32) (result i32)
        (return_call_indirect (type $h) (local.get 0) (i32.const 0)))
      (func (export "indirect") (param i32) (result i32) (call $tail (local.get 0)))
      (func (export "after_grow") (result i32)
        (drop (call $tail (i32.const 15)))
        (i32.load (i32.const 65540)))
      (func (export "after_grow_call") (result i32)
        (drop (call $h (i32.const 15)))
        (i32.load (i32.const 131076)))
      (func (export "after_grow_direct") (result i32)
        (drop (call $direct (i32.const 15)))
        (i32.load (i32.const 196612))))"""),
        imports={"env": {"h": h}},
    )
    cell.append(jit)
    for name in ("call", "direct", "indirect"):
        assert jit(name, 21) == [42]
        assert trap_of(jit, name, 13) is TrapCode.UNREACHABLE
        with pytest.raises(ValueError, match="host"):
            jit(name, 14)
    assert jit("after_grow") == [0]
    assert jit("after_grow_call") == [0]
    assert jit("after_grow_direct") == [0]


# B exports pong and its table; A imports both and puts its ping in the table: ping return_calls
# the imported pong, pong return_call_indirects back into ping (the 3e review's repro).
PONG = """(module
  (type $p (func (param i32) (result i32)))
  (table $t (export "t") 1 funcref)
  (func $pong (export "pong") (type $p)
    (if (result i32) (i32.eqz (local.get 0)) (then (i32.const 222))
      (else (return_call_indirect $t (type $p) (i32.sub (local.get 0) (i32.const 1))
        (i32.const 0))))))"""
PING = """(module
  (type $p (func (param i32) (result i32)))
  (import "b" "pong" (func $pong (type $p)))
  (import "b" "t" (table $t 1 funcref))
  (elem (table $t) (i32.const 0) func $ping)
  (func $ping (export "ping") (type $p)
    (if (result i32) (i32.eqz (local.get 0)) (then (i32.const 111))
      (else (return_call $pong (i32.sub (local.get 0) (i32.const 1)))))))"""


def test_tail_recursion_through_a_function_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """return_call of an imported function of another instance jumps through its record (D30):
    1e6 rounds on a 256 KiB thread give Wasmtime's results, never through Python (the exported
    function's host path is disabled)."""
    need(importlib.util.find_spec("wasmtime") is not None, "wasmtime")
    import wasmtime

    s = wasmtime.Store(wasmtime.Engine(_config()))
    be = wasmtime.Instance(s, wasmtime.Module(s.engine, wat(PONG)), []).exports(s)
    ai = wasmtime.Instance(s, wasmtime.Module(s.engine, wat(PING)), [be["pong"], be["t"]])
    ping = cast(wasmtime.Func, ai.exports(s)["ping"])
    ns = (0, 1, 2, 1000, 1_000_000, 1_000_001)
    expected = [[ping(s, n)] for n in ns]
    assert expected[-2:] == [[111], [222]]

    def no_host_path(self: JitFunc, args: Sequence[Raw]) -> list[Raw]:
        raise AssertionError("an imported JIT function was called through the host bridge")

    def body() -> None:
        store = StoreState()
        b = build(wat(PONG), store=store)
        a = build(wat(PING), imports={"b": provider(b)}, store=store)
        monkeypatch.setattr(JitFunc, "__call__", no_host_path)
        assert [a("ping", n) for n in ns] == expected

    _small_thread(body)


# B owns a memory and a global and grows the memory; A calls it directly (call and return_call)
# and reads its own memory, size and global afterwards.
OWN_B = """(module
  (memory 1) (data (i32.const 0) "\\07")
  (global $g (mut i32) (i32.const 300))
  (func (export "work") (param i32) (result i32)
    (global.set $g (i32.add (global.get $g) (i32.const 1)))
    (i32.add (i32.load8_u (i32.const 0))
      (i32.add (global.get $g)
        (i32.add (i32.mul (memory.grow (local.get 0)) (i32.const 1000))
          (i32.load (i32.sub (i32.mul (memory.size) (i32.const 65536)) (i32.const 4)))))))
  (func (export "trap") (param i32) (result i32) (i32.div_u (i32.const 1) (local.get 0))))"""
OWN_A = """(module
  (type $w (func (param i32) (result i32)))
  (import "b" "work" (func $work (type $w)))
  (import "b" "trap" (func $trap (type $w)))
  (memory 1) (data (i32.const 0) "\\05")
  (global $g (mut i32) (i32.const 40000))
  (func $mine (result i32)
    (i32.add (i32.load8_u (i32.const 0)) (i32.add (global.get $g) (memory.size))))
  (func (export "call") (param i32) (result i32)
    (i32.add (call $work (local.get 0)) (i32.mul (call $mine) (i32.const 100000))))
  (func $tail (param i32) (result i32) (return_call $work (local.get 0)))
  (func (export "tail") (param i32) (result i32)
    (i32.add (call $tail (local.get 0)) (i32.mul (call $mine) (i32.const 100000))))
  (func (export "trap") (param i32) (result i32)
    (i32.add (call $trap (local.get 0)) (call $mine))))"""


def test_direct_call_into_another_instance(pins: None) -> None:
    """A direct call (and a return_call) of an imported function of another instance runs on the
    exporter's context: it sees its own memory and global and grows its own memory; the caller
    then sees its own memory, size and global again. A trap in the callee reaches the caller's
    entry; an import of another store's function is still refused."""
    store = StoreState()
    b = build(wat(OWN_B), store=store)
    a = build(wat(OWN_A), imports={"b": provider(b)}, store=store)
    mine = (5 + 40000 + 1) * 100000
    # B: byte 7, global 300 + k, old size (k pages) * 1000; its new last word is 0
    assert a("call", 1) == [(7 + 301 + 1000 + mine) & 0xFFFFFFFF]
    assert a("tail", 2) == [(7 + 302 + 2000 + mine) & 0xFFFFFFFF]
    assert a("call", 0) == [(7 + 303 + 4000 + mine) & 0xFFFFFFFF]
    assert b("work", 0) == [7 + 304 + 4000]
    assert a("trap", 3) == [0 + 5 + 40000 + 1]
    assert trap_of(a, "trap", 0) is TrapCode.INTEGER_DIVISION_BY_ZERO
    with pytest.raises(ValueError, match="another store"):
        build(wat(OWN_A), imports={"b": provider(b)}, store=StoreState())


TYPED = """(module
  (type $ii (func (param i32) (result i32)))
  (elem declare func $sq)
  (func $sq (type $ii) (i32.mul (local.get 0) (local.get 0)))
  (global $g (mut (ref null $ii)) (ref.func $sq))
  (func $pick (param i32) (result (ref null $ii))
    (select (result (ref null $ii)) (ref.func $sq) (ref.null $ii) (local.get 0)))
  (func (export "set") (param i32) (global.set $g (call $pick (local.get 0))))
  (func (export "call_ref") (param i32 i32) (result i32)
    (call_ref $ii (local.get 0) (call $pick (local.get 1))))
  (func (export "call_ref_global") (param i32) (result i32)
    (call_ref $ii (local.get 0) (global.get $g)))
  (func (export "call_ref_null") (param i32) (result i32)
    (call_ref $ii (local.get 0) (ref.null $ii)))
  (func (export "return_call_ref") (param i32 i32) (result i32)
    (return_call_ref $ii (local.get 0) (call $pick (local.get 1))))
  (func (export "as_non_null") (param i32) (result i32) (local (ref null $ii))
    (local.set 1 (call $pick (local.get 0)))
    (call_ref $ii (i32.const 3) (ref.as_non_null (local.get 1))))
  (func (export "as_non_null_global") (result i32)
    (ref.is_null (ref.as_non_null (global.get $g))))
  (func (export "on_null") (param i32) (result i32)
    (block $null (result i32)
      (i32.const 7)
      (br_on_null $null (call $pick (local.get 0)))
      (drop) (drop) (i32.const 8)))
  (func (export "on_null_local") (param i32 i32) (result i32) (local (ref null $ii))
    (local.set 2 (call $pick (local.get 0)))
    (block $null
      (br_on_null $null (local.get 2))
      (drop)
      (return (call_ref $ii (local.get 1) (local.get 2))))
    (i32.const -1))
  (func (export "on_non_null") (param i32 i32) (result i32)
    (call_ref $ii
      (block $nn (result i32 (ref $ii))
        (local.get 1)
        (br_on_non_null $nn (call $pick (local.get 0)))
        (return (i32.const -1)))))
  (func (export "on_non_null_global") (param i32) (result i32)
    (block $nn (result (ref $ii))
      (br_on_non_null $nn (global.get $g))
      (return (i32.const -2)))
    (drop) (i32.add (local.get 0) (i32.const 1))))"""


def test_typed_function_references_match_wasmtime(pins: None) -> None:
    """call_ref, return_call_ref, ref.as_non_null, br_on_null, br_on_non_null with null and
    non-null references as a computed value, a local, a global and a constant: the same results
    and trap codes (a null reference is NULL_REFERENCE) as Wasmtime."""
    wasm = wat(TYPED)
    jit, ref = build(wasm), _oracle(wasm)
    for which in (1, 0):
        assert jit("set", which) == [] and ref("set", which) == []
        for x in (0, 3, 0xFFFF):
            for name in ("call_ref", "return_call_ref", "on_non_null", "on_null_local"):
                assert outcome(jit, name, x, which) == ref(name, x, which), (name, x, which)
            for name in ("call_ref_global", "call_ref_null", "on_non_null_global"):
                assert outcome(jit, name, x) == ref(name, x), (name, x)
        for name in ("as_non_null", "on_null"):
            assert outcome(jit, name, which) == ref(name, which), (name, which)
        assert outcome(jit, "as_non_null_global") == ref("as_non_null_global")
    assert outcome(jit, "call_ref_null", 1) is TrapCode.NULL_REFERENCE
    assert outcome(jit, "return_call_ref", 1, 0) is TrapCode.NULL_REFERENCE
    assert outcome(jit, "as_non_null", 0) is TrapCode.NULL_REFERENCE


def test_isa_check_on_tail_call_code(tmp_path: Path) -> None:
    """D24: tail calls and typed references emit x86-64-v1 only; so does the trampoline area."""
    native("jit-x86_64")
    from tests.jit.test_encoder import OBJDUMP

    need(OBJDUMP, "GNU objdump")
    a = Asm()
    for win64 in (False, True):
        entry(a, win64)
        bridge(a, win64)
    sp_stub(a)
    tail_switch(a)
    blobs: list[tuple[bytes, int | None]] = [(a.finish(), None)]
    for text in (DEEP, SHAPES, CROSS_A, CROSS_B, TYPED, PONG, PING, OWN_A, OWN_B):
        mod = parse(wat(text))
        validate(mod)
        c = JitX86_64Target().compile(mod)
        assert isinstance(c, Compiled)
        blobs.append((c.code, c.data_start))
    for i, (code, data) in enumerate(blobs):
        blob = tmp_path / f"code{i}.bin"
        blob.write_bytes(code)
        cmd = [sys.executable, str(ROOT / "tools" / "isa_check.py"), str(blob)]
        if data is not None and data < len(code):
            cmd += ["--data", f"{data}:{len(code)}"]
        p = subprocess.run(cmd, capture_output=True, text=True)
        assert p.returncode == 0, p.stdout + p.stderr


def test_real_modules_with_alignment_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    """A `check_align` build checks rsp % 16 at every call site, the host thunk's included:
    compiled modules with host thunks, tail calls, cross-instance switches and nested entries run
    with it on."""
    native("jit-x86_64")
    from tests.jit import test_tables
    from waboros.targets.jit.x86_64 import backend

    thunk_only = wat("""(module (import "env" "h" (func $h)) (table 1 funcref)
      (elem (i32.const 0) func $h))""")  # fmt: skip
    mod = parse(thunk_only)
    validate(mod)
    plain = cast(Compiled, JitX86_64Target().compile(mod)).code
    monkeypatch.setattr(backend.Backend.__init__, "__defaults__", (True,))
    checked = cast(Compiled, JitX86_64Target().compile(mod)).code
    assert len(checked) > len(plain)  # the thunk is the only code: it got the check
    test_return_call_to_host_functions()
    test_tail_calls_across_instances()
    test_tables.test_host_function_in_a_table()
    test_tables.test_table_grow_across_nested_entries()
