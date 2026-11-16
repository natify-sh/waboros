"""Root classes that no other GC test holds in isolation (S0013 step 5.5), each with the reference
kept in that one place while collections move everything (stress, poison, verify): an imported
global, a table imported by two instances, a frame of another instance (direct, and through a host
thunk), the globals of an instance whose instantiation failed. The other classes:
`test_gc_collect.py` (exception payload, non-GC instances, handles, tables in batches, passive
segments) and
`test_gc_moving.py` (locals, operands, parameters, globals, nested entries)."""

from typing import Any, cast

import pytest

from tests.jit import native, native_jit
from tests.jit.test_compiler import Inst, build, wat
from waboros.runtime import link
from waboros.runtime.store import StoreState
from waboros.targets.base import Trap
from waboros.targets.jit.common import gc
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
