# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownLambdaType=false
"""Exception handling on the native JIT (S0012 steps 3-4; aarch64 under QEMU, tools/run_arm64.py):
the cases of tests/test_exceptions_pyc.py, and what only the status path and the exception objects
in the GC heap (S0013 step 6) can get wrong (pins across unwinding, v128 payloads, clause order,
nested pads, imported tags, a full heap, forged identities, no Python on a throw). The spec suite
(`exceptions/`) is the metric."""

import ctypes
import importlib.resources
import os
import struct
import sys
import threading
from pathlib import Path
from types import FrameType
from typing import Any, cast

import pytest

from tests.jit import native, native_jit
from tests.jit.test_compiler import Inst, pins, wat
from tests.jit.test_compiler import build as _build
from tests.jit.test_tables import call, provider
from waboros.frontend.binary import parse
from waboros.frontend.types import key
from waboros.frontend.validate import validate
from waboros.runtime.store import StoreState
from waboros.targets import base
from waboros.targets.base import Trap, TrapCode
from waboros.targets.jit.common import gc
from waboros.targets.jit.common.instance import ExnRef, GcRef, Tag

__all__ = ["pins"]  # the fixture, used by name
JIT = native_jit()
BOUNDS = ["guard", "explicit"] if JIT == "jit-x86_64" else ["explicit"]  # QEMU tears (D45)
SWITCHES = ("WABOROS_JIT_GC_STRESS", "WABOROS_JIT_GC_POISON", "WABOROS_JIT_GC_VERIFY")
# under the stress switch every throw collects (Python runs, and filling a heap takes minutes)
STRESSED = pytest.mark.skipif(
    os.environ.get("WABOROS_JIT_GC_STRESS") == "1", reason="every throw collects under STRESS"
)


@pytest.fixture(autouse=True)
def _jit() -> None:
    native(JIT)


def build(text: str, imports: Any = None, store: StoreState | None = None) -> Inst:
    return _build(wat(text), JIT, imports, store)


def raises(i: Inst, name: str, *args: int) -> Trap:
    with pytest.raises(Trap) as e:
        i(name, *args)
    return e.value


def test_payload_words_land_in_the_label_slots(pins: None) -> None:
    i = build("""
(module
  (tag $t (param i32 i64 v128 f32 i32))
  (func (export "go") (result i32 i64 i64 i64 i32 i32)
    (local $v v128) (local $f f32) (local $k i32)
    (block $h (result i32 i64 v128 f32 i32)
      (try_table (catch $t $h)
        (i32.const -1) (i64.const -2) (v128.const i64x2 0x1111 0x2222) (f32.const 1.5)
        (i32.const 7) (throw $t))
      unreachable)
    (local.set $k) (local.set $f) (local.set $v)
    (i64x2.extract_lane 0 (local.get $v)) (i64x2.extract_lane 1 (local.get $v))
    (i32.reinterpret_f32 (local.get $f)) (local.get $k)))
""")
    assert i("go") == [0xFFFFFFFF, 2**64 - 2, 0x1111, 0x2222, 0x3FC00000, 7]


def test_an_object_larger_than_an_add_immediate() -> None:
    """520 words: the object is 4176 bytes, which no aarch64 add imm12 takes (LESSONS 83); each
    word lands at its own position (a weighted sum), and the heap's cursor moves by the whole
    object (the first allocation grows the heap, it never collects)."""
    n, store = 520, StoreState()
    sets = "".join(f"(local.set {k})" for k in reversed(range(n)))
    acc = "".join(f"(i64.add (i64.mul (local.get {k}) (i64.const {k + 1})))" for k in range(n))
    i = build(
        f"""
(module (tag $t (param {"i64 " * n}))
  (func (export "go") (result i64) (local {"i64 " * n})
    (block $h (result {"i64 " * n})
      (try_table (catch $t $h) {"".join(f"(i64.const {k * k + 1})" for k in range(n))} (throw $t))
      unreachable)
    {sets} (i64.const 0) {acc}))
""",
        store=store,
    )
    heap: Any = cast(Any, store.target_state).heap
    cursor = heap.cursor
    assert i("go") == [sum((k * k + 1) * (k + 1) for k in range(n)) % 2**64]
    assert heap.cursor - cursor == 8 * (2 + n)  # header, identity, payload


def test_catch_ref_and_throw_ref_keep_the_object(pins: None) -> None:
    store = StoreState()
    i = build(
        """
(module
  (tag $t (param i32))
  (tag $w (param exnref i32))
  (func (export "go") (param i32) (result i32)
    (block $outer (result i32)
      (try_table (result i32) (catch $t $outer)
        (block $r (result i32 exnref)
          (try_table (result i32) (catch_ref $t $r) (throw $t (local.get 0)))
          (return))
        (throw_ref))
      unreachable))
  (func (export "wrapped") (result i32)  ;; an exnref in a payload, rethrown after unwrapping
    (block $o (result i32)
      (try_table (catch $t $o)
        (block $w (result exnref i32)
          (block $r (result exnref)
            (try_table (catch_all_ref $r) (throw $t (i32.const 8))) unreachable)
          (try_table (param exnref) (catch $w $w) (throw $w (i32.const 1))) unreachable)
        (drop) (throw_ref))
      unreachable))
  (func (export "null") (result i32) (throw_ref (ref.null exn)) (i32.const 0))
  (func (export "null_in_try") (result i32)
    (block (try_table (catch_all 0) (throw_ref (ref.null exn)))) (i32.const 0)))
""",
        store=store,
    )
    assert i("go", 42) == [42]
    assert i("wrapped") == [8]
    pending = cast(Any, store.target_state).exn
    last = pending[0]
    for name in ("null", "null_in_try"):  # a trap before anything is stored: not caught
        assert raises(i, name).code is TrapCode.NULL_REFERENCE and pending[0] == last


def test_three_frames_deep_with_live_pins(pins: None) -> None:
    i = build("""
(module
  (tag $t (param i32))
  (func $c (param i32) (local $k i32) (local $j i64)
    (local.set $k (i32.const 5)) (local.set $j (i64.const 77))
    (throw $t (i32.add (local.get 0) (local.get $k))))
  (func $b (param i32) (result i32) (local $k i32) (local $q i32) (local $r i32)
    (local.set $k (i32.const 100)) (local.set $q (i32.const 3)) (local.set $r (i32.const 4))
    (call $c (local.get 0)) (i32.add (local.get $k) (i32.add (local.get $q) (local.get $r))))
  (func $a (param i32) (result i32) (local $k i32) (local $m i32) (local $n i32)
    (local.set $k (i32.const 1000)) (local.set $m (i32.const 20000))
    (local.set $n (i32.const 300000))
    (block $h (result i32)
      (try_table (result i32) (catch $t $h) (call $b (local.get 0))))
    (i32.add (local.get $k)) (i32.add (local.get $m)) (i32.add (local.get $n)))
  (export "go" (func $a)))
""")
    assert i("go", 20) == [321025]


def test_clauses_are_tried_in_order(pins: None) -> None:
    i = build("""
(module (tag $t (param i32))
  (func (export "go") (result i32)
    (block $all
      (block $one (result i32)
        (try_table (catch $t $one) (catch_all $all) (throw $t (i32.const 4))) (i32.const 0))
      (return))
    (i32.const 99)))
""")
    assert i("go") == [4]


def test_fifty_nested_try_tables(pins: None) -> None:
    body = "(throw $t (i32.const 1))"
    for _ in range(50):  # each level's clause does not match: on to the enclosing pad
        body = f"(try_table (catch $x $h) {body})"
    i = build(f"""
(module (tag $t (param i32)) (tag $x (param i32))
  (func (export "go") (result i32)
    (block $h (result i32) (try_table (catch $t $h) {body}) (i32.const 0))))
""")
    assert i("go") == [1]


def test_a_call_after_the_try_table_is_not_under_its_handler(pins: None) -> None:
    i = build("""
(module (tag $t)
  (func $throw (throw $t))
  (func (export "go") (result i32)
    (block $h (try_table (catch_all $h)) (call $throw) (return (i32.const 1)))
    (i32.const 2)))
""")
    assert raises(i, "go").code is TrapCode.UNCAUGHT_EXCEPTION


def test_escaping_exception_is_a_trap_with_the_exception() -> None:
    i = build("""
(module (tag $t (param i32 v128))
  (func (export "go") (throw $t (i32.const 9) (v128.const i64x2 1 2))))
""")
    e = raises(i, "go")
    assert e.code is TrapCode.UNCAUGHT_EXCEPTION
    tag, vals = e.exn
    assert isinstance(tag, Tag) and vals == (9, 1 | 2 << 64)


def test_exnref_reaches_the_host_as_an_object() -> None:
    i = build("""
(module (tag $t)
  (func (export "go") (result exnref)
    (block $h (result exnref) (try_table (catch_all_ref $h) (throw $t)) unreachable)))
""")
    assert isinstance(i("go")[0], ExnRef)


def test_traps_host_errors_interrupts_are_not_caught(pins: None) -> None:
    store = StoreState()

    def boom(args: Any) -> list[int]:
        raise ValueError("host")

    i = build(
        """
(module
  (import "h" "boom" (func $boom))
  (func (export "trap") (try_table (catch_all 0) (unreachable)))
  (func (export "div") (result i32)
    (block (try_table (catch_all 0) (drop (i32.div_u (i32.const 1) (i32.const 0)))))
    (i32.const 0))
  (func (export "host") (try_table (catch_all 0) (call $boom)))
  (func $rec (result i32) (block (try_table (catch_all 0) (drop (call $rec)))) (i32.const 0))
  (export "rec" (func $rec))
  (func (export "spin") (result i32)
    (block (try_table (catch_all 0) (loop $l (br $l)))) (i32.const 1)))
""",
        {"h": {"boom": boom}},
        store,
    )
    assert raises(i, "trap").code is TrapCode.UNREACHABLE
    assert raises(i, "div").code is TrapCode.INTEGER_DIVISION_BY_ZERO
    assert raises(i, "rec").code is TrapCode.STACK_OVERFLOW
    with pytest.raises(ValueError):
        i("host")
    t = threading.Timer(0.05, store.interrupt)
    t.start()
    assert raises(i, "spin").code is TrapCode.INTERRUPT


def test_exception_through_a_host_function() -> None:
    seen: list[Any] = []
    box: dict[str, Any] = {}

    def host(args: Any) -> list[int]:
        try:
            box["i"]("inner")
        except Trap as t:
            seen.append(t)
            if args[0]:
                raise
        return []

    box["i"] = i = build(
        """
(module
  (import "h" "host" (func $host (param i32)))
  (tag $t (param i32))
  (func (export "inner") (throw $t (i32.const 3)))
  (func (export "outer") (param i32) (result i32)
    (block $h
      (try_table (catch_all $h) (call $host (local.get 0)))
      (return (i32.const 1)))
    (i32.const 2)))
""",
        {"h": {"host": host}},
    )
    assert i("outer", 0) == [1]  # the host swallowed it: the outer call goes on
    assert seen[0].code is TrapCode.UNCAUGHT_EXCEPTION and seen[0].exn[1] == (3,)
    assert raises(i, "outer", 1) is seen[1]  # re-raised: a host error, not catchable


def test_a_host_trap_14_is_a_host_error_never_status_14() -> None:
    """A raw HostFunc (no `link` wrapper, which would make every exception a HostError)."""

    def host(args: Any) -> list[int]:
        raise Trap(TrapCode.UNCAUGHT_EXCEPTION, "forged")

    mod = parse(
        wat("""
(module (import "h" "f" (func $f))
  (func (export "go") (result i32) (block (try_table (catch_all 0) (call $f))) (i32.const 1)))
""")
    )
    validate(mod)
    t = base.TARGETS[JIT]()
    i = Inst(mod, t.instantiate(t.compile(mod), [base.Extern("func", None, host)], StoreState()))
    e = raises(i, "go")
    assert e.exn is None and str(e) == "forged"


def test_return_call_leaves_the_try() -> None:
    i = build("""
(module
  (tag $t)
  (func $thrower (result i32) (throw $t))
  (func (export "go") (result i32)
    (block $h
      (try_table (catch_all $h) (return_call $thrower))
      (return (i32.const 1)))
    (i32.const 2)))
""")
    assert raises(i, "go").code is TrapCode.UNCAUGHT_EXCEPTION


def test_across_instances_and_imported_tag_aliases(pins: None) -> None:
    store = StoreState()
    a = build(
        """
(module (tag $t (export "t") (param i32))
  (func (export "throw") (param i32) (throw $t (local.get 0))))
""",
        store=store,
    )
    b = build(
        """
(module
  (import "a" "t" (tag $t (param i32)))
  (import "a" "t" (tag $u (param i32)))
  (import "a" "throw" (func $throw (param i32)))
  (type $f (func (param i32)))
  (table funcref (elem $throw))
  (tag $own (param i32))
  (func $own (param i32) (throw $own (local.get 0)))
  (elem declare func $own)
  (func (export "call") (param i32) (result i32)  ;; $u aliases $t: the first clause matches
    (block $h (result i32)
      (try_table (catch $own $h) (catch $u $h) (call $throw (local.get 0))) (i32.const -1)))
  (func (export "indirect") (param i32) (result i32)
    (block $h (result i32)
      (try_table (catch $t $h) (call_indirect (type $f) (local.get 0) (i32.const 0)))
      (i32.const -1)))
  (func (export "ref") (param i32) (result i32)
    (block $h (result i32)
      (try_table (catch $t $h) (call_ref $f (local.get 0) (ref.func $own))) (i32.const -1)))
  (func $tail (param i32) (return_call $throw (local.get 0)))
  (func (export "tail") (param i32) (result i32)
    (block $h (result i32)
      (try_table (catch $t $h) (call $tail (local.get 0))) (i32.const -1))))
""",
        {"a": provider(a)},
        store,
    )
    assert b("call", 5) == [5] and b("indirect", 6) == [6] and b("tail", 8) == [8]
    e = raises(b, "ref", 7)  # $own is not $t: escapes
    assert e.code is TrapCode.UNCAUGHT_EXCEPTION and e.exn[1] == (7,)


LOOP = """
(module (tag $t (param i32 v128))
  (func $thrower (param i32) (throw $t (local.get 0) (v128.const i64x2 3 4)))
  (func (export "loop") (param $n i32) (result i32) (local $s i32)
    (loop $l
      (block $h (result i32 v128)
        (try_table (catch $t $h) (call $thrower (local.get $n))) unreachable)
      (drop) (local.set $s (i32.add (local.get $s)))
      (br_if $l (local.tee $n (i32.sub (local.get $n) (i32.const 1)))))
    (local.get $s))
  (func (export "escape") (throw $t (i32.const 5) (v128.const i64x2 6 7))))
"""


@STRESSED
def test_a_throw_runs_no_python() -> None:
    i = build(LOOP)
    i("loop", 10)

    def calls(n: int) -> int:
        c = [0]

        def count(frame: FrameType, event: str, arg: object) -> None:
            if event == "call":
                c[0] += 1

        sys.setprofile(count)
        try:
            assert i("loop", n) == [n * (n + 1) // 2]
        finally:
            sys.setprofile(None)
        return c[0]

    # the entry's own calls are the same for 1 and 2000 throws (their number depends on the
    # runtime and on a tracer such as coverage); per throw there would be 2000 or more
    assert calls(2000) - calls(1) < 50


FULL = """(module (tag $t (param i32)) (table $k 0 exnref)
  (func (export "go") (result i32) (local $i i32) (local $e exnref)
    (block $h
      (try_table (catch_all $h)
        (loop $l  ;; each exception kept in the table: the heap fills up
          (block $c (result i32 exnref)
            (try_table (catch_ref $t $c) (throw $t (local.get $i))) unreachable)
          (local.set $e) (drop)
          (drop (table.grow $k (local.get $e) (i32.const 1)))
          (local.set $i (i32.add (local.get $i) (i32.const 1)))
          (br $l)))
      (return (i32.const 1)))
    (i32.const 2)))"""


@STRESSED
def test_a_full_heap_at_throw_is_an_error_no_pad_catches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Approval note, addition 1 (Wasmtime's GcHeapOutOfMemory): a `throw` whose allocation finds
    the heap full after collecting and growing to its limit fails with "GC heap out of memory",
    status 13, which the enclosing `catch_all` does not catch: the entry raises it. Live
    exceptions are kept in an exnref table (a root) until a 1 MiB semispace is full."""
    monkeypatch.setattr(gc, "CAP_MAX", 2 << 20)
    store = StoreState()
    i = build(FULL, store=store)
    with pytest.raises(MemoryError, match="GC heap out of memory"):
        i("go")
    heap = cast(Any, store.target_state).heap
    assert heap.cap == 2 << 20 and heap.collections >= 1


def test_a_forged_identity_matches_no_tag() -> None:
    """Approval note, addition 3: the pad compares the full 64-bit identity read from the heap, so
    an object whose identity differs from the tag's in the high or the low half only (a probe
    writes it through the host's handle) is caught by no `catch $t`: the `catch_all` takes it."""
    store = StoreState()
    i = build(
        """
(module (tag $t (param i32))
  (func (export "make") (result exnref)
    (block $h (result exnref) (try_table (catch_all_ref $h) (throw $t (i32.const 5))) unreachable))
  (func (export "rethrow") (param exnref) (result i32)
    (block $all
      (block $h (result i32)
        (try_table (catch $t $h) (catch_all $all) (throw_ref (local.get 0))) unreachable)
      (return))
    (i32.const -1)))
""",
        store=store,
    )
    st: Any = store.target_state
    e = i("make")[0]
    k = next(x for n, _, x in i.mod["exports"] if n == "rethrow")

    def rethrow() -> list[Any]:
        return i.inst.call(k, [e])

    assert isinstance(e, ExnRef) and rethrow() == [5]
    at = st.heap.base + st.gc_handles[e.index] + 8
    ident = ctypes.c_uint64.from_address(at).value
    for forged in (ident | 1 << 32, ident ^ 1):
        ctypes.c_uint64.from_address(at).value = forged
        assert rethrow() == [0xFFFFFFFF]
    ctypes.c_uint64.from_address(at).value = ident
    assert rethrow() == [5]


def test_a_module_that_throws_or_catches_by_tag_gets_the_heap() -> None:
    """t5 (S0013 step 6.1, D6 (b)): a `throw` gives the store the heap, its header page and first
    type-info page committed (128 KiB) and no object space until the first throw commits the
    semispaces (6.2). A `catch` clause alone gets the heap and no RTT word; `throw_ref` and
    `catch_all` get none."""
    store, other = StoreState(), StoreState()
    i = build(THROWS, store=store)
    st: Any = store.target_state
    assert st.heap.mem.pages << 16 == 128 << 10 and st.heap.cap == 0
    assert i("go") == [7] and st.heap.cap > 0
    lay: Any = build(CATCHES).inst.layout  # pyright: ignore[reportAttributeAccessIssue]
    build(CLEANUP, store=other)
    assert lay.heap and lay.rtts == 0 and cast(Any, other.target_state).heap is None


THROWS = """(module (tag $t (param i32)) (func (export "go") (result i32)
  (block $h (result i32) (try_table (catch $t $h) (throw $t (i32.const 7))) unreachable)))"""
CATCHES = "(module (tag $t) (func (block $h (try_table (catch $t $h)))))"
CLEANUP = "(module (func (param exnref) (try_table (catch_all 0) (throw_ref (local.get 0)))))"


def test_a_thrown_tag_type_has_one_struct_record_apart_from_its_function_record() -> None:
    """S0013 step 6.1 (D2, D3): two thrown tags of one type share one kind-1 record, final,
    depth 0, of `gc.exn_layout` (the identity at 8, the externref traced, the funcref not), under
    a key distinct from the type's own kind-3 record (a non-final function type, S0013 5.6)."""
    store = StoreState()
    a = "(i32.const 0) (ref.null extern) (f64.const 0) (ref.null func) (v128.const i64x2 0 0)"
    t = "(module (type $f (sub (func (param i32 externref f64 funcref v128))))"
    t += f"(tag $t (type $f)) (tag $u (type $f)) (tag $c (param i64)) (func (throw $t {a}))"
    i = build(t + f"(func (throw $u {a})))", store=store)
    jit, heap = cast(Any, i.inst), cast(Any, store.target_state).heap
    assert gc.exn_layout(i.mod, i.mod["types"][0][0]) == ([16, 20, 24, 32, 48], 64)
    r, fr = heap.records["exn " + key(i.mod, 0)], heap.records[key(i.mod, 0)]
    assert jit.layout.rtts == 2 and [jit.ctx[jit.layout.rtt(k)] for k in (0, 1)] == [r, r]
    words = struct.unpack_from("<6I", ctypes.string_at(heap.base + r, 24))
    assert words == (gc.KIND_STRUCT | 1 << 8, 0, 64, 1, r, 20)
    assert heap.kind[r] == gc.KIND_STRUCT and r in heap.offsets and fr not in heap.offsets
    assert ctypes.c_uint32.from_address(heap.base + fr).value == gc.KIND_FUNC  # not final


INLINE = """(module (tag $t (param i32))
  (func $g (block $h (result i32) (try_table (catch $t $h) (throw $t (i32.const 5))) unreachable)
    (drop))
  (func (export "go") (param $n i32) (result i32)
    (block $c (result i32)
      (try_table (catch $t $c)
        (block $l (result exnref)  ;; the module's only exnref type: inline
          (try_table (catch_all_ref $l) (throw $t (i32.const 77))) unreachable)
        (loop $lp (call $g) (br_if $lp (local.tee $n (i32.sub (local.get $n) (i32.const 1)))))
        (throw_ref))
      unreachable)))"""


@pytest.mark.parametrize("switched", [False, True])
def test_an_exnref_of_an_inline_block_type_is_in_the_maps(
    monkeypatch: pytest.MonkeyPatch, switched: bool
) -> None:
    """6.2 review: an exnref whose only type is a block's inline `(result exnref)` (LLVM's cleanup
    shape) is marked at the call sites below it, so the collections of the throws in `$g` move
    the object it names (the masks were decided before the bodies turned them on: the old object,
    status 14 without an exception, or another exception's payload 5)."""
    for s in SWITCHES:
        monkeypatch.setenv(s, "1" if switched else "0")
    store = StoreState()
    i = build(INLINE, store=store)
    assert i.mod["refmasks"]
    assert i("go", 5 if switched else 200_000) == [77]
    assert cast(Any, store.target_state).heap.collections > 0


def test_a_missed_clause_keeps_a_value_below_the_outer_label(pins: None) -> None:
    """The scratch slot of a pad's identity compare is the try_table's entry height: the inner
    clause misses, and the value under the outer pad's label (42, from a call) is still there when
    the outer pad catches (9). With the slot at the label's own position it would be lost (M15)."""
    i = build("""
(module (tag $t1 (param i32)) (tag $t2 (param i32))
  (func $id (param i32) (result i32) (local.get 0))
  (func (export "go") (result i32)
    (block $c (result i32)
      (call $id (i32.const 42))
      (block $m (result i32)
        (try_table (catch $t2 $m) (try_table (catch $t1 $c) (throw $t2 (i32.const 9))))
        unreachable)
      i32.add)))
""")
    assert i("go") == [51]


@pytest.mark.parametrize("bounds", BOUNDS)
def test_a_pad_read_out_of_the_heap_traps(monkeypatch: pytest.MonkeyPatch, bounds: str) -> None:
    """Every read of the pending object is a checked load (D1 (b)): a handle forged to an offset
    past the heap's committed end, or near 2**32, traps MEMORY_OUT_OF_BOUNDS in both bounds
    modes, and a trap is not caught by the `catch_all` beside the `catch $t`."""
    monkeypatch.setenv("WABOROS_JIT_BOUNDS", bounds)
    store = StoreState()
    i = build(
        """
(module (tag $t (param i32))
  (func (export "make") (result exnref)
    (block $h (result exnref) (try_table (catch_all_ref $h) (throw $t (i32.const 5))) unreachable))
  (func (export "rethrow") (param exnref) (result i32)
    (block $all
      (block $h (result i32)
        (try_table (catch $t $h) (catch_all $all) (throw_ref (local.get 0))) unreachable)
      (return))
    (i32.const -1)))
""",
        store=store,
    )
    st: Any = store.target_state
    k = next(x for n, _, x in i.mod["exports"] if n == "rethrow")
    e: Any = i("make")[0]
    old = st.gc_handles[e.index]
    for bad in (0xFFFFFFF0, st.heap.mem.pages << 16):
        del st.gcrefs[st.gc_handles[e.index]]
        st.gc_handles[e.index], st.gcrefs[bad] = bad, e
        with pytest.raises(Trap) as t:
            i.inst.call(k, [e])
        assert t.value.code is TrapCode.MEMORY_OUT_OF_BOUNDS
    del st.gcrefs[st.gc_handles[e.index]]
    st.gc_handles[e.index], st.gcrefs[old] = old, e
    assert i.inst.call(k, [e]) == [5]


@pytest.mark.parametrize("target", ["jit-x86_64", "jit-aarch64"])  # compiled only, never run
def test_no_module_uses_the_reserved_words(monkeypatch: pytest.MonkeyPatch, target: str) -> None:
    """t11 (S0013 steps 6.3-6.4): words 11-13 (the arena until then) are zero in every context,
    and no code names them: in a backend call, off the context register, or off a register the
    context was copied or added into (x86-64 `mov`/`lea`/`add`, aarch64 `mov`/`add`). Spied
    while compiling the spec's `exceptions/` and `gc/` modules, CoreMark and the collector in
    both bounds modes, and while assembling the trampolines and the fault handlers (the process
    built its own before the spies). As test_gc_subtype.py checks GC group word 7."""
    from tests.spec.fetch import CACHE_DIR
    from waboros.targets.jit.aarch64 import abi, encoder
    from waboros.targets.jit.x86_64 import abi as xabi
    from waboros.targets.jit.x86_64 import encoder as x86

    monkeypatch.setenv("WABOROS_JIT_GC_STRESS", "1")  # the second throw collects
    store = StoreState()
    assert build(THROWS, store=store)("go") == build(THROWS, store=store)("go") == [7]
    st: Any = store.target_state
    for c in [cast(Any, x).ctx for x in store.instances] + [st.heap.collector[0].ctx]:
        assert c[11] == c[12] == c[13] == 0

    x, M, A = target == "jit-x86_64", x86.Mem, encoder.Asm
    C: Any = x86.R15 if x else abi.CTX
    words, seen, alias = list[int](), set[int](), dict[Any, int]()  # register -> context + k

    def spy(owner: Any, name: str, see: Any) -> None:
        real = getattr(owner, name)
        monkeypatch.setattr(owner, name, lambda *a, **k: see(*a, **k) or real(*a, **k))

    def at(r: Any, d: int, n: int = 1) -> None:  # n words at [r + d]
        o = 0 if r == C else alias.get(r)
        words.extend(() if o is None else range((o + d) // 8, (o + d) // 8 + n))

    def put(d: Any, r: Any = None, k: int = 0) -> None:  # d = r + k, or d = anything (no r)
        o = 0 if r == C else alias.get(r)
        if o is not None and k:
            at(r, k)  # an address computed
        if d != C:
            alias.pop(d, None) if o is None else alias.update({d: o + k})

    def imm(s: Any, op: str, d: Any, n: Any, k: int, w: bool = True) -> None:
        put(d, n if op in ("add", "sub") else None, k if op == "add" else -k)

    def pair(s: Any, ld: bool, r: int, u: int, b: int, o: int = 0, mode: str = "off") -> None:
        at(b, o, 2 * (mode == "off"))
        _ = ld and (put(r), put(u))

    t: Any = base.TARGETS[target]()
    for name in ("call_host", "call_record", "tail_record"):  # a word the bridge or a call reads
        spy(t.backend().__class__, name, lambda s, f, w, *a: words.append(w))
    if x:
        spy(M, "__post_init__", lambda m: at(m.base, m.disp))
        spy(
            x86.Asm,
            "mov",
            lambda s, d, r, w=0: isinstance(d, M) or put(d, (r, None)[isinstance(r, M)]),
        )
        spy(
            x86.Asm, "lea", lambda s, d, m, w=1: put(d, (m.base, None)[m.index is not None], m.disp)
        )
        spy(x86.Asm, "alu_imm", lambda s, op, d, v, w=0: isinstance(d, M) or imm(s, op, d, d, v))
        spy(x86.Asm, "alu", lambda s, op, d, r, w=0: isinstance(d, M) or op == "cmp" or put(d))
        spy(x86.Asm, "mov_imm", lambda s, d, v, w=0: isinstance(d, M) or put(d))
        spy(x86.Asm, "pop", lambda s, r: put(r))
    else:
        spy(A, "mov", lambda s, d, n, wide=True: put(d, n))
        spy(A, "addsub_imm", imm)
        spy(A, "mov_imm", lambda s, d, *a, **k: put(d))
        spy(A, "_ldst", lambda s, r, b, o, n, ld, *a: at(b, o) or ld and put(r))
        spy(A, "_ldst_idx", lambda s, r, b, i, n, ld, *a: ld and put(r))
        spy(A, "pair", pair)
    if x:  # the spies see an address computed off a copy of the context
        a = x86.Asm()
        a.mov(x86.RCX, C, w=True)
        a.alu_imm("add", x86.RCX, 8, w=True)
        a.mov(x86.RCX, M(x86.RCX, 88), w=True)
    else:
        e = A()
        e.addsub_imm("add", 11, C, 96)
        e.ld(10, 11)
    assert 12 in words
    ROOT = Path(__file__).resolve().parents[2]
    files = [
        f for d in ("exceptions", "gc") for f in sorted((CACHE_DIR / "json" / d).rglob("*.wasm"))
    ]
    spec = len(files)
    core = ROOT / "poc/in/core.wasm"  # not in the wheel harness's copy of the tests
    wasm = [wat(THROWS), wat(CATCHES), wat(CLEANUP)] + [f.read_bytes() for f in files]
    wasm += [core.read_bytes()] if core.exists() else []
    wasm.append((importlib.resources.files("waboros.runtime") / "gc/collector.wasm").read_bytes())
    n = 0
    for bounds in ("guard", "explicit"):
        monkeypatch.setenv("WABOROS_JIT_BOUNDS", bounds)
        for w in wasm:
            try:
                m: Any = parse(w)
                validate(m)
            except Exception:  # the spec's malformed and invalid modules
                continue
            words.clear()
            alias.clear()
            t.compile(m)
            n += 1
            assert not {11, 12, 13} & set(words)
            seen |= set(words)
    # without the fetched spec suite and the PoC (the wheel harness: LESSONS 124), the local
    # modules and the installed collector still run, 4 modules in each bounds mode
    assert n > (250 if spec else 7) and {8, 10} <= seen  # the bridge and the pending word
    builds: list[Any] = [abi.assemble]
    if x:
        builds = [lambda: xabi.assemble(False), lambda: xabi.assemble(True)]
    for h in [{}, {"darwin": True}, {"windows": True}] + [{"bionic": True}] * x:
        builds.append(lambda h=h: t.fault_handler(1, 2, 3, 4, **h))
    seen.clear()
    for make in builds:
        words.clear()
        alias.clear()
        make()
        assert not {11, 12, 13} & set(words)
        seen |= set(words)
    assert {7, 14} <= seen  # the bridge's HOST_ERROR, the entry's EXIT_RECORD


# ------------------------------------------------------------------ S0013 step 6.4: t1-t10
def switched(mp: pytest.MonkeyPatch) -> None:
    """Every allocation collects, the from-space is poisoned, every collection verified."""
    for s in SWITCHES:
        mp.setenv(s, "1")


T1 = """(module (type $s (struct (field i32)))
  (tag $t (param i32 f32 i64 f64 v128 anyref funcref exnref externref)) (tag $e)
  (func $f) (elem declare func $f)
  (func (export "go") (param externref) (result exnref anyref funcref exnref)
    (local $in exnref) (local $s anyref)
    (local.set $in (block $h (result exnref) (try_table (catch_all_ref $h) (throw $e)) unreachable))
    (local.set $s (struct.new $s (i32.const 7)))
    (block $c (result exnref)
      (try_table (catch_all_ref $c)
        (throw $t (i32.const -2) (f32.const 1.5) (i64.const 0x0102030405060708) (f64.const 2.5)
          (v128.const i64x2 10 11) (local.get $s) (ref.func $f) (local.get $in) (local.get 0)))
      unreachable)
    (local.get $s) (ref.func $f) (local.get $in)))"""


def test_the_object_and_its_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """t1: each value at its `exn_layout` offset (natural alignment), the identity at 8, the
    header the tag type's record: kind 1, final, depth 0, 88 bytes, the anyref, exnref and
    externref traced (not the funcref)."""
    switched(monkeypatch)
    store, host = StoreState(), object()
    i = build(T1, store=store)
    st, jit = cast(Any, store.target_state), cast(Any, i.inst)
    e, s, f, inner = call(i, "go", host)
    ti = i.mod["tags"][0]
    assert gc.exn_layout(i.mod, i.mod["types"][ti][0]) == ([16, 20, 24, 32, 48, 64, 72, 80, 84], 88)
    r = st.heap.records["exn " + key(i.mod, ti)]
    obj = ctypes.string_at(st.heap.base + st.gc_handles[e.index], 88)
    assert struct.unpack_from("<IIQifQd", obj) == (r, 0, jit.ctx[jit.layout.tag(0)], -2, 1.5,
                                                   0x0102030405060708, 2.5)  # fmt: skip
    *v, ext = struct.unpack_from("<QQI4xQII", obj, 48)
    assert v == [10, 11, st.gc_handles[s.index], f.addr, st.gc_handles[inner.index]]
    assert ext & 3 == 2 and st.externs[(ext >> 2) - 1] is host
    rec = struct.unpack_from("<8I", ctypes.string_at(st.heap.base + r, 32))
    assert rec == (gc.KIND_STRUCT | 1 << 8, 0, 88, 3, r, 64, 80, 84)


T2 = """(module (type $s (struct (field i32)))
  (tag $t (param anyref externref exnref)) (tag $e (param i32))
  (func $thr (param anyref externref exnref) (throw $t (local.get 0) (local.get 1) (local.get 2)))
  (func $c (param anyref externref exnref) (call $thr (local.get 0) (local.get 1) (local.get 2)))
  (func $b (param anyref externref exnref) (call $c (local.get 0) (local.get 1) (local.get 2)))
  (func (export "go") (param $x externref) (param $deep i32) (result i32 externref)
    (local $s (ref null $s)) (local $in exnref) (local $k i32) (local $pa anyref) (local $pe exnref)
    (local.set $k (i32.const 1000))  ;; an i32 local: pinned in a register across the throws
    (local.set $in (block $h (result exnref)
      (try_table (catch_all_ref $h) (throw $e (i32.const 31))) unreachable))
    (local.set $s (struct.new $s (i32.const 7)))
    (block $c (result anyref externref exnref)
      (try_table (catch $t $c)
        (if (local.get $deep) (then (call $b (local.get $s) (local.get $x) (local.get $in)))
          (else (call $thr (local.get $s) (local.get $x) (local.get $in)))))
      unreachable)
    (local.set $pe) (local.set $x) (local.set $pa)
    (drop (struct.new $s (i32.const 0)))
    (block $r (result i32) (try_table (catch $e $r) (throw_ref (local.get $pe))) unreachable)
    (i32.add (local.get $k))
    (i32.add (i32.mul (i32.const 100)
      (ref.eq (ref.cast (ref null eq) (local.get $pa)) (local.get $s))))
    (i32.add (i32.mul (i32.const 10000) (struct.get $s 0 (ref.cast (ref $s) (local.get $pa)))))
    (local.get $x)))"""


@pytest.mark.parametrize("deep", [0, 1])
def test_a_throw_that_collects_keeps_its_payload(
    pins: None, monkeypatch: pytest.MonkeyPatch, deep: int
) -> None:
    """t2: the payload (a struct, a host value, an exnref) is read from its slots after the throw's
    allocation collected, caught one frame up or three; every throw collects (the fast path
    checks the limit), and the struct is the same object (`ref.eq`) after more collections."""
    switched(monkeypatch)
    store, host = StoreState(), object()
    i = build(T2, store=store)
    heap = cast(Any, store.target_state).heap
    n = heap.collections
    assert call(i, "go", host, deep) == [71131, host]
    assert heap.collections - n >= 3  # two throws and two structs; the first grows the heap


def test_t6_across_instances_under_the_switches(monkeypatch: pytest.MonkeyPatch) -> None:
    """t6: the imported-tag aliases, every throw collecting."""
    switched(monkeypatch)
    test_across_instances_and_imported_tag_aliases(None)


T7 = """(module (type $s (struct (field i32)))
  (import "h" "f" (func $f)) (tag $t (param i32 i32 f64 v128 anyref))
  (func (export "outer") (call $f))
  (func (export "inner") (throw $t (i32.const -5) (i32.const 7) (f64.const 2.5)
    (v128.const i64x2 1 2) (struct.new $s (i32.const 99))))
  (func (export "alloc") (drop (struct.new $s (i32.const 0))))
  (func (export "read") (param anyref) (result i32)
    (struct.get $s 0 (ref.cast (ref $s) (local.get 0)))))"""


def test_an_uncaught_payload_outlives_collections(monkeypatch: pytest.MonkeyPatch) -> None:
    """t7: an inner entry's `Trap.exn` has every value at its offset, and its anyref is a handle
    that still reads its field after the host ran wasm that collected."""
    switched(monkeypatch)
    store, box, seen = StoreState(), dict[str, Any](), list[Any]()

    def host(_: Any) -> list[int]:
        with pytest.raises(Trap) as t:
            box["i"]("inner")
        heap = cast(Any, store.target_state).heap
        n = heap.collections
        box["i"]("alloc")
        box["i"]("alloc")
        seen.extend([t.value, heap.collections - n, call(box["i"], "read", t.value.exn[1][4])])
        return []

    box["i"] = i = build(T7, {"h": {"f": host}}, store)
    assert i("outer") == []
    t, collected, field = seen
    f64 = struct.unpack("<Q", struct.pack("<d", 2.5))[0]
    assert t.code is TrapCode.UNCAUGHT_EXCEPTION and t.exn[1][:4] == (
        2**32 - 5,
        7,
        f64,
        1 | 2 << 64,
    )
    assert isinstance(t.exn[1][4], GcRef) and collected >= 2 and field == [99]


T8 = """(module (type $s (struct (field i32))) (tag $t (param i32 anyref))
  (func (export "make") (param i32) (result exnref)
    (block $h (result exnref)
      (try_table (catch_all_ref $h) (throw $t (local.get 0) (struct.new $s (local.get 0))))
      unreachable))
  (func (export "alloc") (drop (struct.new $s (i32.const 0))))
  (func (export "rethrow") (param exnref) (result i32)
    (block $h (result i32 anyref) (try_table (catch $t $h) (throw_ref (local.get 0))) unreachable)
    (struct.get $s 0 (ref.cast (ref $s))) (i32.add)))"""


def test_an_exnref_kept_by_the_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """t8: an `ExnRef` the host keeps across collections is rethrown with its tag and payload;
    a forged handle or one of another store is a `TypeError`."""
    switched(monkeypatch)
    store = StoreState()
    i = build(T8, store=store)
    heap = cast(Any, store.target_state).heap
    [e] = call(i, "make", 21)
    n = heap.collections
    for _ in range(3):
        i("alloc")
    assert heap.collections >= n + 3 and call(i, "rethrow", e) == [42]
    other = call(build(T8), "make", 1)[0]
    for bad in (ExnRef(e.index, e.store), other):
        with pytest.raises(TypeError):
            call(i, "rethrow", bad)
    assert call(i, "rethrow", e) == [42]


T9 = """(module (tag $t (param i32 i32)) (tag $x (param i32)) (tag $y)
  (func $thr (throw $t (i32.const -1) (i32.const 5)))
  (func $g  ;; every clause of both pads misses: on to the caller's pad
    (block $a (result i32)
      (try_table (catch $x $a) (block $b (try_table (catch $y $b) (call $thr)))) unreachable)
    (drop))
  (func (export "same") (result i64)  ;; the inner pad misses, the enclosing one catches
    (block $c (result i32 i32)
      (try_table (catch $t $c)
        (block $m (result i32) (try_table (catch $x $m) (call $thr)) unreachable) (drop))
      unreachable)
    (drop) (i64.extend_i32_u))
  (func (export "caller") (result i32)  ;; the payload's slot is the result, copied in 64 bits
    (block $c (result i32 i32) (try_table (catch $t $c) (call $g)) unreachable)
    (drop)))"""


def test_a_miss_passes_the_exception_on(pins: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """t9: a pad whose clauses all miss passes status 14 to the enclosing pad of its function, and
    from the last one to the caller's; an i32 payload is zero-extended in its slot (the second
    i32 beside it is not read with it), as a result and through `i64.extend_i32_u`. Built twice:
    a missed tag's identity, left in the status register by a wrong pad, could be 14 by chance
    in one of them (the full-heap test is t9's third case)."""
    switched(monkeypatch)
    for _ in range(2):
        i = build(T9)
        assert i("same") == i("caller") == [0xFFFFFFFF]


T10 = """(module (type $s (struct (field i32))) (type $f (sub (func (param anyref))))
  (tag $t (type $f))
  (func (export "go") (result i32)
    (block $h (result anyref)
      (try_table (catch $t $h) (throw $t (struct.new $s (i32.const 3)))) unreachable)
    (struct.get $s 0 (ref.cast (ref $s)))))"""


def test_a_tag_type_with_a_function_record_throws(monkeypatch: pytest.MonkeyPatch) -> None:
    """t10: the tag's type is a non-final function type, which has its own kind-3 record (S0013
    5.6): its exception objects have the `exn` record, so a collection over them does not trap
    (a kind-3 header would lose the store). Another module of the store writes the kind-3
    record first."""
    switched(monkeypatch)
    store = StoreState()
    build("(module (type (sub (func (param anyref)))) (type (struct)))", store=store)
    i = build(T10, store=store)
    st = cast(Any, store.target_state)
    assert [i("go") for _ in range(3)] == [[3]] * 3
    assert st.heap.collections >= 5 and not st.lost  # the first allocation grows the heap
