"""Root classes that no other GC test holds in isolation (S0013 step 5.5), each with the reference
kept in that one place while collections move everything (stress, poison, verify): an imported
global, a table imported by two instances, a frame of another instance (direct, and through a host
thunk), the globals of an instance whose instantiation failed. The other classes:
`test_gc_collect.py` (exception payload, non-GC instances, handles, tables in batches, passive
segments) and
`test_gc_moving.py` (locals, operands, parameters, globals, nested entries)."""

import ctypes
from typing import Any, cast

import pytest

from tests.jit import native, native_jit
from tests.jit.test_compiler import Inst, build, wat
from waboros.runtime import link
from waboros.runtime.store import StoreState
from waboros.targets.base import Trap
from waboros.targets.jit.common import gc, walk
from waboros.targets.jit.common import instance as inst_
from waboros.targets.jit.common.instance import _stacks  # pyright: ignore[reportPrivateUsage]

JIT = native_jit()
BOUNDS = ["guard", "explicit"] if JIT == "jit-x86_64" else ["explicit"]
P = "(type $p (struct (field (mut i32)) (field (mut anyref))))"
NEW = "(struct.new $p (i32.const 0) (ref.null any))"
N = 4000  # more than half a batch of 7 680 root words: a table visited twice splits across two
EXPORTER = f"""(module {P}
  (global (export "g") (mut anyref) (ref.null any)) (table (export "t") {N} anyref)
  (func (export "set") (param i32)
    (global.set 0 (struct.new $p (local.get 0) (ref.null any))))
  (func (export "fill") (local $i i32)
    (loop $l (table.set 0 (local.get $i) (struct.new $p (local.get $i) (ref.null any)))
      (br_if $l (i32.lt_u (local.tee $i (i32.add (local.get $i) (i32.const 1))) (i32.const {N})))))
  (func (export "alloc") (drop {NEW})))"""
IMPORTER = f"""(module {P}
  (import "e" "g" (global $g (mut anyref))) (import "e" "t" (table $t {N} anyref))
  (func (export "alloc") (drop {NEW}))
  (func (export "get") (result i32) (struct.get $p 0 (ref.cast (ref $p) (global.get $g))))
  (func (export "sum") (result i32) (local $i i32) (local $s i32)
    (drop {NEW})  ;; collects under stress
    (loop $l (local.set $s (i32.add (local.get $s)
        (struct.get $p 0 (ref.cast (ref $p) (table.get $t (local.get $i))))))
      (br_if $l (i32.lt_u (local.tee $i (i32.add (local.get $i) (i32.const 1))) (i32.const {N}))))
    (local.get $s)))"""
CALLEE = f'(module {P} (func (export "alloc") (drop {NEW}) (drop {NEW})))'
CALLER = f"""(module {P}
  (import "b" "alloc" (func $b)) (import "t" "tail" (func $t)) (import "env" "cb" (func $cb))
  (func $use (param anyref) (result i32) (struct.get $p 0 (ref.cast (ref $p) (local.get 0))))
  (func (export "viaB") (result i32) (local $x anyref)  ;; $x is the only reference to its object
    (local.set $x (struct.new $p (i32.const 5) (ref.null any)))
    (call $b) (call $use (local.get $x)))
  (func (export "viaTail") (result i32) (local $x anyref)  ;; $t tail-calls $b: a restore frame
    (local.set $x (struct.new $p (i32.const 7) (ref.null any)))
    (call $t) (call $use (local.get $x)))
  (func (export "viaHost") (result i32) (local $x anyref)
    (local.set $x (struct.new $p (i32.const 6) (ref.null any)))
    (call $cb) (call $use (local.get $x))))"""
HOLDER = f"""(module {P} (import "a" "t" (table $t 1 funcref)) (memory 1)
  (global $g anyref (struct.new $p (i32.const 77) (ref.null any)))  ;; allocated at instantiation
  (elem (table $t) (i32.const 0) func $f) (data (i32.const 65536) "x")  ;; traps after the elem
  (func $f (result i32) (struct.get $p 0 (ref.cast (ref $p) (global.get $g)))))"""
TAILER = '(module (import "b" "alloc" (func $b)) (func (export "tail") (return_call $b)))'
TABLE = f"""(module {P} (type $ft (func (result i32))) (table (export "t") 1 funcref)
  (func (export "go") (result i32) (drop {NEW}) (call_indirect (type $ft) (i32.const 0))))"""


@pytest.fixture(autouse=True, params=BOUNDS)
def _bounds(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WABOROS_JIT_BOUNDS", request.param)


def setup(mp: pytest.MonkeyPatch) -> StoreState:
    native(JIT)
    for s in ("STRESS", "POISON", "VERIFY"):
        mp.setenv(f"WABOROS_JIT_GC_{s}", "1")
    return StoreState()


def prov(i: Inst) -> link.Provider:
    return link.Provider(i.mod, i.inst)


def test_an_imported_global_is_a_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """A defence, not a reachable state: the exporter's own word is a root too while it is in
    `store.instances`, and instances are never removed from it. Taken out here, the importer's cell
    is the only way to the object, so the test fails if the imported word is not visited."""
    store = setup(monkeypatch)
    e = build(wat(EXPORTER), store=store)
    i = build(wat(IMPORTER), imports={"e": prov(e)}, store=store)
    e("set", 41)
    store.instances.remove(e.inst)
    n = _stacks(store).heap.collections
    for _ in range(3):
        i("alloc")
    assert _stacks(store).heap.collections >= n + 3 and i("get") == [41]


def test_a_table_imported_by_two_instances_is_visited_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The words sent to the collector are the table's once and the global's: a second visit would
    send them again (harmless to the collector, which skips to-space words, but twice the work)."""
    store = setup(monkeypatch)
    e = build(wat(EXPORTER), store=store)
    i = build(wat(IMPORTER), imports={"e": prov(e)}, store=store)
    heap = _stacks(store).heap
    heap.stress = False
    e("fill")
    heap.stress = True
    heap.u32(gc.LIMIT, heap.cursor)  # the next allocation takes the slow path
    n, sent = heap.collections, list[int]()
    real = inst_.Instance.call

    def spy(self: Any, f: int, args: Any) -> Any:
        if heap.collector and self is heap.collector[0] and f == heap.collector[1]["roots"]:
            sent.append(args[0])
        return real(self, f, args)

    monkeypatch.setattr(inst_.Instance, "call", spy)
    assert i("sum") == [N * (N - 1) // 2] and heap.collections == n + 1
    assert N <= sum(sent) <= N + 8  # the table's words and a few others, once


@pytest.mark.parametrize("via", ["viaB", "viaTail", "viaHost"])
def test_a_frame_of_another_instance_and_a_host_thunk_frame(
    monkeypatch: pytest.MonkeyPatch, via: str
) -> None:
    """The caller's local is the only reference; the collection happens in another instance, called
    directly (a function record), after a tail call into it (a restore frame between) or through a
    host function (a thunk frame between)."""
    store = setup(monkeypatch)
    b = build(wat(CALLEE), store=store)

    def cb(_: Any) -> list[Any]:
        return b("alloc")

    t = build(wat(TAILER), imports={"b": prov(b)}, store=store)
    imports: dict[str, Any] = {"b": prov(b), "t": prov(t), "env": {"cb": cb}}
    a = build(wat(CALLER), imports=imports, store=store)
    n = _stacks(store).heap.collections
    assert a(via) == [{"viaB": 5, "viaTail": 7, "viaHost": 6}[via]]
    assert _stacks(store).heap.collections >= n + 2


def test_a_failed_instantiation_leaves_its_globals_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    """The elem segment already wrote the function into the imported table when the data segment
    trapped: called later, it reads a global that only the failed instance holds."""
    store = setup(monkeypatch)
    t = build(wat(TABLE), store=store)
    with pytest.raises(Trap):
        build(wat(HOLDER), imports={"a": prov(t)}, store=store)
    n = _stacks(store).heap.collections
    assert t("go") == [77] and _stacks(store).heap.collections > n


PROBES = f"""(module {P}
  (tag $t1 (param anyref)) (tag $tv (param v128 anyref))
  (global $first (mut exnref) (ref.null exn))
  (func $mk (param i32) (result (ref $p)) (struct.new $p (local.get 0) (ref.null any)))
  (func $val (param anyref) (result i32) (struct.get $p 0 (ref.cast (ref $p) (local.get 0))))
  (func (export "v128tag") (result i32) (local $e exnref)  ;; a ref payload after a v128 one
    (block $h (result v128 anyref exnref)
      (try_table (catch_ref $tv $h)
        (throw $tv (v128.const i32x4 7 7 7 7) (call $mk (i32.const 43))))
      unreachable)
    (local.set $e) (drop) (drop)
    (drop (call $mk (i32.const 1))) (drop (call $mk (i32.const 2)))
    (block $h2 (result v128 anyref)
      (try_table (catch $tv $h2) (throw_ref (local.get $e))) unreachable)
    (call $val) (i32x4.replace_lane 0) (i32x4.extract_lane 0))
  (func (export "keepfirst")  ;; an exnref kept in a global while many others are thrown
    (block $h (result anyref exnref)
      (try_table (catch_ref $t1 $h) (throw $t1 (call $mk (i32.const 44)))) unreachable)
    (global.set $first) (drop))
  (func (export "throws") (param $n i32)
    (loop $l
      (block $h (result anyref)
        (try_table (catch $t1 $h) (throw $t1 (ref.null any))) unreachable)
      (drop)
      (br_if $l (local.tee $n (i32.sub (local.get $n) (i32.const 1))))))
  (func (export "readfirst") (result i32)
    (drop (call $mk (i32.const 1)))
    (block $h (result anyref)
      (try_table (catch $t1 $h) (throw_ref (global.get $first))) unreachable)
    (call $val)))"""
BIG = """(module (type $l (array (mut i64)))
  (global (export "g") (ref $l) (array.new_default $l (i32.const 200000))))"""


def test_a_payload_after_a_v128_parameter(monkeypatch: pytest.MonkeyPatch) -> None:
    """A payload reference after a v128 parameter is traced at its offset in the object."""
    store = setup(monkeypatch)
    i = build(wat(PROBES), store=store)
    n = _stacks(store).heap.collections
    assert i("v128tag") == [43] and _stacks(store).heap.collections > n


def test_a_payload_kept_in_a_global_across_many_throws(monkeypatch: pytest.MonkeyPatch) -> None:
    """An exnref global keeps its object, and the object its payload, while 5 000 more exceptions
    are thrown and collected (S0013 step 6; the arena's chunk walk before it)."""
    store = setup(monkeypatch)
    i = build(wat(PROBES), store=store)
    i("keepfirst")
    i("throws", 5000)
    n = _stacks(store).heap.collections
    assert i("readfirst") == [44] and _stacks(store).heap.collections > n


def test_the_to_space_follows_the_end_not_lo(monkeypatch: pytest.MonkeyPatch) -> None:
    """An instantiation grew the heap with the upper half active (`lo` in the upper half, `end`
    above the old half boundary): the next collection still copies into the lower half."""
    from tests.jit.test_gc_collect import call, make

    i, st = make(monkeypatch, "stress", "poison", "verify")
    h = st.heap
    call(i, "chain", 3)
    while h.lo == gc.OBJ:
        call(i, "chain", 1)  # each collects into the other half
    lo, cap, n = h.lo, h.cap, h.collections
    want = call(i, "sum")
    build(
        wat(BIG), store=cast(Any, i.inst).store
    )  # grows without collecting, the upper half active
    assert h.lo == lo and h.cap > cap and h.collections == n
    call(i, "chain", 2)  # collects from [lo, cursor) of the grown heap
    assert h.collections > n
    assert call(i, "sum") == [(want[0] + 3) & 0xFFFFFFFF]


EXPORTS_EXN = '(module (global (export "g") (mut exnref) (ref.null exn)))'
PLACES = """(module (type $s (struct (field (mut exnref))))
  (import "e" "g" (global $ig (mut exnref)))
  (tag $t (param i32)) (tag $w (param exnref))
  (global $g (mut exnref) (ref.null exn)) (table $tb 1 exnref)
  (func $mk (param i32) (result exnref)
    (block $h (result exnref) (try_table (catch_all_ref $h) (throw $t (local.get 0))) unreachable))
  (func $churn (result i32)  ;; three collections; the pending word no longer the kept one
    (drop (call $mk (i32.const 0)))
    (drop (struct.new $s (ref.null exn))) (drop (struct.new $s (ref.null exn))) (i32.const 0))
  (func $get (param exnref i32) (result i32)  ;; rethrown and caught: the payload
    (block $h (result i32) (try_table (catch $t $h) (throw_ref (local.get 0))) unreachable)
    (i32.add (local.get 1)))
  (func $param (param exnref) (result i32)
    (drop (call $churn)) (call $get (local.get 0) (i32.const 0)))
  (func (export "local") (result i32) (local $e exnref)
    (local.set $e (call $mk (i32.const 1))) (drop (call $churn))
    (call $get (local.get $e) (i32.const 0)))
  (func (export "param") (result i32) (call $param (call $mk (i32.const 2))))
  (func (export "operand") (result i32) (call $get (call $mk (i32.const 3)) (call $churn)))
  (func (export "global") (result i32)
    (global.set $g (call $mk (i32.const 4))) (drop (call $churn))
    (call $get (global.get $g) (i32.const 0)))
  (func (export "imported") (result i32)
    (global.set $ig (call $mk (i32.const 5))) (drop (call $churn))
    (call $get (global.get $ig) (i32.const 0)))
  (func (export "table") (result i32)
    (table.set $tb (i32.const 0) (call $mk (i32.const 6))) (drop (call $churn))
    (call $get (table.get $tb (i32.const 0)) (i32.const 0)))
  (func (export "field") (result i32) (local $x (ref null $s))
    (local.set $x (struct.new $s (call $mk (i32.const 7)))) (drop (call $churn))
    (call $get (struct.get $s 0 (local.get $x)) (i32.const 0)))
  (func (export "payload") (result i32) (local $o exnref)  ;; the inner one only in the outer's
    (block $h (result exnref exnref)
      (try_table (catch_ref $w $h) (throw $w (call $mk (i32.const 8)))) unreachable)
    (local.set $o) (drop) (drop (call $churn))
    (block $h2 (result exnref) (try_table (catch $w $h2) (throw_ref (local.get $o))) unreachable)
    (call $get (i32.const 0))))"""


def test_an_exnref_in_every_place(monkeypatch: pytest.MonkeyPatch) -> None:
    """t3 (S0013 step 6): an exnref is a root or a traced field wherever it is kept (a local, a
    parameter, an operand below a call, a defined and an imported global, a table, a struct field,
    another exception's payload), and is rethrown with its payload after collections. The
    first collection is a throw's, so the pending word (a root) then names another object, and
    three collections leave a stale word in the poisoned half (after two, the object can be back
    at its old offset)."""
    store = setup(monkeypatch)
    e = build(wat(EXPORTS_EXN), store=store)
    i = build(wat(PLACES), imports={"e": prov(e)}, store=store)
    heap = _stacks(store).heap
    names = "local param operand global imported table field payload".split()
    for k, name in enumerate(names, 1):
        n = heap.collections
        assert i(name) == [k] and heap.collections >= n + 3, name


ARRAY = """(module (type $s (struct (field i32))) (type $a (array (mut exnref)))
  (tag $t (param anyref)) (global $arr (mut (ref null $a)) (ref.null $a))
  (func $mk (param i32) (result exnref)
    (block $h (result exnref)
      (try_table (catch_all_ref $h) (throw $t (struct.new $s (local.get 0)))) unreachable))
  (func $val (param exnref) (result i32)
    (block $h (result anyref) (try_table (catch $t $h) (throw_ref (local.get 0))) unreachable)
    (struct.get $s 0 (ref.cast (ref $s))))
  (func $at (param (ref $a) i32) (result i32)
    (call $val (array.get $a (local.get 0) (local.get 1))))
  (func (export "fill") (param $n i32) (local $i i32) (local $x (ref $a))
    (local.set $x (array.new $a (ref.null exn) (local.get $n)))
    (loop $l (array.set $a (local.get $x) (local.get $i) (call $mk (local.get $i)))
      (br_if $l (i32.lt_u (local.tee $i (i32.add (local.get $i) (i32.const 1))) (local.get $n))))
    (global.set $arr (local.get $x)))
  (func (export "churn") (param $n i32)
    (loop $l (drop (call $mk (local.get $n)))
      (br_if $l (local.tee $n (i32.sub (local.get $n) (i32.const 1))))))
  (func (export "sum") (result i32) (local $i i32) (local $s i32) (local $x (ref $a))
    (local.set $x (ref.as_non_null (global.get $arr)))
    (loop $l
      (local.set $s (i32.add (local.get $s) (call $at (local.get $x) (local.get $i))))
      (br_if $l (i32.lt_u (local.tee $i (i32.add (local.get $i) (i32.const 1)))
                          (array.len (local.get $x)))))
    (local.get $s))
  (func (export "ops") (result i32) (local $x (ref $a)) (local $y (ref $a))
    (local.set $x (array.new_fixed $a 3 (call $mk (i32.const 1)) (call $mk (i32.const 2))
                                        (call $mk (i32.const 3))))
    (local.set $y (array.new $a (ref.null exn) (i32.const 5)))
    (array.copy $a $a (local.get $y) (i32.const 1) (local.get $x) (i32.const 0) (i32.const 3))
    (array.fill $a (local.get $y) (i32.const 4) (call $mk (i32.const 40)) (i32.const 1))
    (drop (call $mk (i32.const 0)))
    (i32.add (i32.add (call $at (local.get $y) (i32.const 1))
                      (i32.mul (i32.const 10) (call $at (local.get $y) (i32.const 2))))
             (i32.add (i32.mul (i32.const 100) (call $at (local.get $y) (i32.const 3)))
                      (i32.mul (i32.const 1000) (call $at (local.get $y) (i32.const 4)))))))"""


def test_an_exnref_array(monkeypatch: pytest.MonkeyPatch) -> None:
    """t4 (S0013 "For step 6"): `(array (mut exnref))` of 1 000 caught exceptions, each with a
    struct payload, kept while 10 more are thrown and collected; each element is then rethrown
    and its payload read. Also `array.new_fixed`, `array.copy` and `array.fill` of exnrefs. The
    array is filled without stress (1 000 collections of a growing live set take too long under
    QEMU); every later allocation collects."""
    store = setup(monkeypatch)
    i = build(wat(ARRAY), store=store)
    heap = _stacks(store).heap
    heap.stress = False
    i("fill", 1000)
    heap.stress = True
    heap.u32(gc.LIMIT, heap.cursor)  # the next allocation takes the slow path
    n = heap.collections
    i("churn", 10)
    assert heap.collections >= n + 10
    assert i("sum") == [999 * 1000 // 2] and i("ops") == [40321]


# --- liveness (S0013 step 7.3, D65): a reference local dead at a collection is nulled ----------
# Each case keeps a struct in one local, read (through $f, a cast that traps on null) only on
# the path the tested edge leads to, with a collection between the write and the read (a throw's
# exception object, or $alloc's struct) and no other root naming the struct (LESSONS 122). A
# mutant that drops the edge nulls the slot: the read traps whatever the number of collections.
LHEAD = f"""{P} (tag $e) (tag $e2) (tag $ex (param i32))
  (func $alloc (drop {NEW}))
  (func $athrow (throw $e))
  (func $f (param anyref) (result i32) (struct.get $p 0 (ref.cast (ref $p) (local.get 0))))
  (func $vec (param $v v128) (result i32) (local $w v128) (local $r anyref) (local $i i32)
    (local $s i32) (local.set $w (local.get $v))
    (local.set $r (struct.new $p (i32.const 5) (ref.null any)))
    (loop $l (local.set $s (i32.add (local.get $s) (call $f (local.get $r)))) (call $alloc)
      (br_if $l (i32.lt_u (local.tee $i (i32.add (local.get $i) (i32.const 1))) (i32.const 3))))
    (i32.add (local.get $s) (i32x4.extract_lane 2 (local.get $w))))"""
R2 = (
    "(local $a anyref) (local $b anyref)"
    " (local.set $a (struct.new $p (i32.const 1) (ref.null any)))"
    " (local.set $b (struct.new $p (i32.const 2) (ref.null any)))"
)
R = "(local $r anyref) (local.set $r (struct.new $p (i32.const 7) (ref.null any)))"
LCASES: dict[str, tuple[str, list[tuple[tuple[int, ...], int]]]] = {
    "t17 back edge": ("(local $i i32) (local $s i32) (local $r anyref)"
        " (local.set $r (struct.new $p (i32.const 5) (ref.null any))) (loop $l (local.set $s"
        " (i32.add (local.get $s) (call $f (local.get $r)))) (call $alloc) (br_if $l (i32.lt_u"
        " (local.tee $i (i32.add (local.get $i) (i32.const 1))) (i32.const 3)))) (local.get $s)",
        [((), 15)]),
    "t18 catch": (f"{R} (block $h (try_table (catch $e $h) (call $athrow)) (return (i32.const -1)))"
                  " (call $f (local.get $r))", [((), 7)]),
    "t18 throw": (f"{R} (block $h (try_table (catch_all $h) (throw $ex (i32.const 1)))"
                  " (return (i32.const -1))) (call $f (local.get $r))", [((), 7)]),
    "t19 enclosing": (f"{R} (block $h (try_table (catch $e $h) (block $h2"
                      " (try_table (catch $e2 $h2) (call $athrow))) (return (i32.const -1)))"
                      " (return (i32.const -2)))"
                      " (call $f (local.get $r))", [((), 7)]),
    "t20 br_if": (f"{R2} (block $t (call $alloc) (br_if $t (local.get $c))"
                  " (return (call $f (local.get $b)))) (call $f (local.get $a))",
                  [((0,), 2), ((1,), 1)]),
    "t21 br_table": (f"{R2} (block $l1 (block $l0 (call $alloc) (br_table $l0 $l1 (local.get $c)))"
                     " (return (call $f (local.get $a)))) (call $f (local.get $b))",
                     [((0,), 1), ((1,), 2), ((5,), 2)]),
    "t22 if/else": (f"{R2} (call $alloc) (if (result i32) (local.get $c)"
                    " (then (call $f (local.get $a))) (else (call $f (local.get $b))))",
                    [((0,), 2), ((1,), 1)]),
    # 7.1+7.2 review: the then-arm continues after the end (M_else_swap)
    "t22 then-arm": (f"{R} (if (local.get $c) (then (call $alloc)) (else (local.set $r"
                     " (struct.new $p (i32.const 6) (ref.null any))))) (call $f (local.get $r))",
                     [((1,), 7), ((0,), 6)]),
    "t23 v128": ("(call $vec (v128.const i32x4 1 2 3 4))", [((), 18)]),  # slots: $v 0-1, $w 2-3
    # 7.1+7.2 review: a loop in the try_table body keeps H (M_loop_no_H); a site before the
    # try_table keeps a local only the handler reads (M_H_not_propagated)
    "review loop in body": (f"{R} (block $h (try_table (catch_all $h) (loop (call $athrow)))"
                            " (return (i32.const -1))) (call $f (local.get $r))", [((), 7)]),
    "review before try": (f"{R} (call $alloc) (block $h (try_table (catch $e $h) (call $athrow))"
                          " (return (i32.const -1))) (call $f (local.get $r))", [((), 7)]),
    # t26: $r dead after the call (S1) and no write before the next allocation (S2)
    "t26 inconsistent": (f"{R} (drop (call $f (local.get $r))) (call $alloc) (drop {NEW})"
                         " (i32.const 9)", [((), 9)]),
    # the 7.3 review's surviving mutants: br one level out, an if inside a try_table without H,
    # only the first catch clause joining H, a local.set killing its neighbour slot
    "review br": (f"{R} (block $b (call $alloc) (br $b)) (call $f (local.get $r))", [((), 7)]),
    "review second clause": (f"{R} (block $h2 (block $h1 (try_table (catch $e2 $h1) (catch $e $h2)"
                             " (call $athrow)) (return (i32.const -1))) (return (i32.const -2)))"
                             " (call $f (local.get $r))", [((), 7)]),
    "review if in try": (f"{R} (block $h (try_table (catch_all $h) (if (local.get $c) (then"
                         " (call $athrow)))) (return (i32.const -1))) (call $f (local.get $r))",
                         [((1,), 7)]),
    "review set next slot": ("(local $a anyref) (local $b anyref) (local.set $b (struct.new $p"
                             " (i32.const 2) (ref.null any))) (call $alloc)"
                             " (local.set $a (ref.null any)) (call $f (local.get $b))", [((), 2)]),
}  # fmt: skip


@pytest.mark.parametrize("case", sorted(LCASES))
def test_a_live_local_survives_and_a_dead_one_is_not_needed(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """t17-t23, t26 and the review's three: each edge the pass follows keeps its local's struct
    across a collection; a mutant that drops the edge nulls the slot, and the read traps."""
    store = setup(monkeypatch)
    body, calls = LCASES[case]
    head = "(param $c i32) (result i32)" if calls[0][0] else "(result i32)"
    i = build(wat(f'(module {LHEAD} (func (export "go") {head} {body}))'), store=store)
    for args, want in calls:
        n = _stacks(store).heap.collections
        assert i("go", *args) == [want] and _stacks(store).heap.collections > n, (args, want)


T16 = f"""(module {P} (import "env" "h" (func $h)) (func $alloc (drop {NEW}))
  (func (export "go") (result i32) (local $d anyref) (local $l anyref)
    (local.set $d (struct.new $p (i32.const 1) (ref.null any)))
    (local.set $l (struct.new $p (i32.const 2) (ref.null any)))
    (drop (ref.cast (ref $p) (local.get $d)))  ;; the last read of $d
    (call $alloc) (call $h)  ;; one collection; the host reads the two slots
    (struct.get $p 0 (ref.cast (ref $p) (local.get $l)))))"""
T16C = """(module (import "env" "h" (func $h))
  (func (export "go") (param $d externref) (param $l externref) (result externref)
    (call $h) (local.get $l)))"""


def slots(store: StoreState) -> list[int]:
    """From a host function: slots 0 and 1 of the frame whose map has both, as 64-bit words."""
    st = _stacks(store)
    segs = walk.walk(store, st, store.instances[0].tramp)  # type: ignore[attr-defined]
    base = next(f[3] for fs, _ in segs for f in fs if f[1][4] == 3)
    return [ctypes.c_uint64.from_address(base + 8 * k).value for k in (0, 1)]


@pytest.mark.parametrize("by", ["collection", "check"])
def test_t16_a_dead_slot_reads_null_and_its_live_neighbour_moved(
    monkeypatch: pytest.MonkeyPatch, by: str
) -> None:
    """t16 (M8, M11, M12): after a collection the dead local is 0 and the live one in the next slot
    holds its moved struct; in check mode, without a heap, the dead slot is 0 at any host call."""
    store, seen = setup(monkeypatch), list[list[int]]()

    def h(_: Any) -> list[Any]:
        seen.append(slots(store))
        return []

    host = {"env": {"h": h}}
    if by == "check":
        monkeypatch.setenv("WABOROS_JIT_WALK_CHECK", "1")
        a, b = object(), object()
        i = build(wat(T16C), imports=host, store=store)
        assert cast(Any, i.inst).call(1, [a, b]) == [b] and seen[0][0] == 0
        assert seen[0][1] & 3 == 2 and _stacks(store).externs[(seen[0][1] >> 2) - 1] is b
        return
    i = build(wat(T16), imports=host, store=store)
    st = _stacks(store)
    assert i("go") == [2] and seen[0][0] == 0 and seen[0][1] and walk.valid(st, seen[0][1])
    assert st.heap.lo <= seen[0][1] < st.heap.end


T24 = """(module (type $a (array (mut i8))) (type $s (struct (field i32)))
  (func (export "go") (param $n i32) (local $big (ref null $a))
    (local.set $big (array.new_default $a (i32.const 0x1000000)))
    (drop (array.len (local.get $big)))  ;; its last read
    (loop $l (drop (struct.new $s (local.get $n)))  ;; each collects
      (br_if $l (local.tee $n (i32.sub (local.get $n) (i32.const 1)))))))"""
T25 = f"""(module {P} (func (export "go") (param $x externref) (result i32)
  (drop (ref.is_null (local.get $x))) (drop {NEW}) (i32.const 0)))"""


@pytest.mark.parametrize("nolive", [False, True])
def test_t24_t25_a_dead_local_does_not_retain(
    monkeypatch: pytest.MonkeyPatch, nolive: bool
) -> None:
    """t24: a 16 MiB array held only by a dead local is not copied by the next collection, and the
    heap does not grow; t25: a host value held only by a dead local is freed (D61). With
    WABOROS_JIT_NOLIVE=all (the step-6 maps) both are retained."""
    store = setup(monkeypatch)
    if nolive:
        monkeypatch.setenv("WABOROS_JIT_NOLIVE", "all")
    i = build(wat(T24), store=store)
    heap = _stacks(store).heap
    i("go", 3)
    cap = heap.cap
    i("go", 3)
    assert (heap.cursor - heap.lo >= 1 << 24) == nolive and (heap.cap == cap or nolive)
    o, j = object(), build(wat(T25), store=store)
    n = heap.collections
    assert cast(Any, j.inst).call(0, [o]) == [0] and heap.collections == n + 1
    assert any(v is o for v in _stacks(store).externs) == nolive
