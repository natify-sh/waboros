"""The JIT GC heap collects (S0013 step 5.3, D61): Wasmtime-style semispaces and their growth, the
collector as a nested entry (atomic, not interruptible, a trap loses the store), host values freed
lowest first, GcRef identity across moves, no collection while instantiating, the array
services collecting, and one test per root class and per check of the collection (the review's
tests of step 5.3 among them). On the machine's own JIT."""

import ctypes
from typing import Any, cast

import pytest

from tests.jit import native, native_jit
from tests.jit.test_compiler import Inst, build, wat
from waboros.runtime.store import StoreState
from waboros.targets.base import Trap, TrapCode
from waboros.targets.jit.common import collect, gc
from waboros.targets.jit.common import instance as inst_
from waboros.targets.jit.common.heap import GcHeap
from waboros.targets.jit.common.instance import _stacks  # pyright: ignore[reportPrivateUsage]

JIT = native_jit()
SWITCHES = ("WABOROS_JIT_GC_STRESS", "WABOROS_JIT_GC_POISON", "WABOROS_JIT_GC_VERIFY")
W = """(module
  (type $p (struct (field (mut i32)) (field (mut anyref))))
  (type $l (array (mut i64))) (type $aa (array (mut anyref))) (type $b (array (mut i8)))
  (import "env" "poke" (func $poke))
  (global $keep (mut anyref) (ref.null any))
  (elem $e anyref (item (struct.new $p (i32.const 5) (ref.null any)))
    (item (ref.i31 (i32.const 6))))
  (data $d "\\01\\02\\03\\04\\05\\06\\07\\08")
  (func $new (param i32) (result anyref) (struct.new $p (local.get 0) (global.get $keep)))
  (func (export "chain") (param $n i32)
    (loop $l (global.set $keep (call $new (local.get $n)))
      (br_if $l (local.tee $n (i32.sub (local.get $n) (i32.const 1))))))
  (func (export "sum") (result i32) (local $x anyref) (local $s i32)
    (local.set $x (global.get $keep))
    (block $d (loop $l (br_if $d (ref.is_null (local.get $x)))
      (local.set $s (i32.add (local.get $s) (struct.get $p 0 (ref.cast (ref $p) (local.get $x)))))
      (local.set $x (struct.get $p 1 (ref.cast (ref $p) (local.get $x)))) (br $l)))
    (local.get $s))
  (func (export "keep") (result anyref) (global.get $keep))
  (func (export "churn") (param $n i32)
    (loop $l (drop (array.new $l (i64.const 1) (i32.const 100)))
      (br_if $l (local.tee $n (i32.sub (local.get $n) (i32.const 1))))))
  (func (export "ident") (param anyref) (result anyref)
    (drop (call $new (i32.const 0))) (local.get 0))
  (func (export "ext") (param externref) (global.set $keep (any.convert_extern (local.get 0)))
    (drop (call $new (i32.const 0))))
  (func (export "intr") (call $poke)  ;; no call between: a callee's entry polls
    (drop (struct.new $p (i32.const 1) (ref.null any))) (loop $l (br $l)))
  (func $rec (export "rec") (param i32) (result i32)  ;; allocates at the deepest frame
    (if (result i32) (local.get 0) (then (call $rec (i32.sub (local.get 0) (i32.const 1))))
      (else (drop (call $new (i32.const 1))) (i32.const 0))))
  (func (export "services") (result i32)
    (local $v (ref $p)) (local $a (ref $aa)) (local $e (ref $aa))
    (local.set $v (struct.new $p (i32.const 40) (ref.null any)))
    (local.set $a (array.new $aa (local.get $v) (i32.const 3)))  ;; B2: the value after the move
    (local.set $e (array.new_elem $aa $e (i32.const 0) (i32.const 2)))  ;; B3: items after it
    (i32.add (i32.add (ref.eq (local.get $v)
                              (ref.cast (ref $p) (array.get $aa (local.get $a) (i32.const 2))))
      (struct.get $p 0 (ref.cast (ref $p) (array.get $aa (local.get $e) (i32.const 0)))))
      (i32.add (i31.get_s (ref.cast (ref i31) (array.get $aa (local.get $e) (i32.const 1))))
        (array.get_u $b (array.new_data $b $d (i32.const 0) (i32.const 8)) (i32.const 7))))))"""
CONSTS = """(module (type $p (struct (field (mut i32)) (field (mut anyref))))
  (global (export "g") anyref
    (struct.new $p (i32.const 9) (struct.new $p (i32.const 8) (ref.null any)))))"""


def make(mp: pytest.MonkeyPatch, *on: str) -> tuple[Inst, Any]:
    """An instance of W in a new store, with the switches `on` (read when its heap is made)."""
    native(JIT)
    for s in SWITCHES:
        mp.setenv(s, "1" if s.split("_")[-1].lower() in on else "0")
    store = StoreState()

    def poke(_: Any) -> list[Any]:
        store.interrupt()
        return []

    inst = build(wat(W), imports={"env": {"poke": poke}}, store=store)
    return inst, _stacks(store)


def call(i: Inst, name: str, *args: Any) -> list[Any]:
    return i.inst.call(next(x for n, k, x in i.mod["exports"] if n == name and k == 0), list(args))


def test_growth_doubles_never_shrinks_and_flips(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[int, int]] = []
    real = GcHeap.room

    def record(h: GcHeap, r: int, i: Any = None) -> None:
        real(h, r, i)
        seen.append((h.lo, h.cap))

    monkeypatch.setattr(GcHeap, "room", record)
    i, st = make(monkeypatch)
    for n in (1000, 60000, 120000):
        call(i, "chain", n)  # n structs of 16 bytes live, on top of the earlier chains
    call(i, "churn", 30000)  # 24 MB of garbage: collections that do not grow, both halves used
    assert call(i, "sum") == [sum(n * (n + 1) // 2 for n in (1000, 60000, 120000)) & 0xFFFFFFFF]
    assert st.heap.collections and [c for _, c in seen] == sorted(c for _, c in seen)
    assert seen[-1][1] >= 4 << 20 and {lo == gc.OBJ for lo, _ in seen} == {True, False}
    for (_, c0), (lo, c1) in zip(seen, seen[1:], strict=False):
        assert c1 == c0 or lo == gc.OBJ  # growth only with the lower half active (D3)


def test_out_of_memory_at_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gc, "CAP_MAX", 8 << 20)  # halves of 4 MiB
    i, st = make(monkeypatch)
    call(i, "chain", 200000)  # 3.2 MB live
    assert st.heap.cap == 8 << 20
    i, st = make(monkeypatch)
    with pytest.raises(MemoryError, match="GC heap out of memory"):
        call(i, "chain", 300000)
    assert not st.lost


def test_a_collector_trap_loses_the_store(monkeypatch: pytest.MonkeyPatch) -> None:
    i, st = make(monkeypatch, "stress")
    call(i, "chain", 3)
    st.heap.u32(st.gc_handles[call(i, "keep")[0].index], 0x1234)  # no record there
    with pytest.raises(RuntimeError, match="the collector trapped"):
        call(i, "churn", 1)
    with pytest.raises(RuntimeError, match=collect.LOST):
        call(i, "sum")


def test_an_interrupt_does_not_cut_a_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    i, st = make(monkeypatch, "stress", "verify")
    call(i, "chain", 2)
    n = st.heap.collections
    with pytest.raises(Trap) as e:
        call(i, "intr")
    assert e.value.code == TrapCode.INTERRUPT and st.heap.collections == n + 1 and not st.lost
    assert call(i, "sum") == [3]


def test_deep_recursion_collects(monkeypatch: pytest.MonkeyPatch) -> None:
    i, _ = make(monkeypatch)
    lo, hi = 1, 1 << 20  # the smallest depth that overflows, without collections
    while lo < hi:
        mid = (lo + hi) // 2
        try:
            call(i, "rec", mid)
            lo = mid + 1
        except Trap:
            hi = mid
    i, st = make(monkeypatch, "stress")
    call(i, "chain", 1)
    n = st.heap.collections
    assert call(i, "rec", lo * 9 // 10) == [0] and st.heap.collections == n + 1


def test_host_values_are_freed_and_reused_lowest_first(monkeypatch: pytest.MonkeyPatch) -> None:
    i, st = make(monkeypatch, "stress", "verify")
    for _ in range(3000):
        call(i, "ext", object())
    assert len(st.externs) <= 3 and len(st.extern_ids) <= 2
    filler = object()  # held by nothing; the next value's handle is above the report
    for h in st.free_externs:
        st.externs[h - 1] = filler
    st.free_externs.clear()
    st.externs += [filler] * collect.HANDLES
    big = object()
    call(i, "ext", big)
    assert st.extern_ids[id(big)] > collect.HANDLES and all(st.externs)  # overflow: nothing freed
    call(i, "ext", None)  # big is garbage now: everything but the live values is freed
    assert st.externs.count(None) >= collect.HANDLES and not st.extern_ids
    first = object()
    call(i, "ext", first)
    assert st.externs[0] is first and big not in st.externs and st.extern_ids[id(first)] == 1


def test_handles_keep_their_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    i, st = make(monkeypatch, "stress", "poison", "verify")
    call(i, "chain", 1)
    r = call(i, "keep")[0]
    words = set[int]()
    for _ in range(6):  # each call collects: the object moves, the halves alternate
        assert call(i, "ident", r)[0] is r
        words.add(st.gc_handles[r.index])
    assert len(words) == 2 and set(st.gcrefs) == set(st.gc_handles)
    assert all(call(i, "ident", None)[0] is None for _ in range(2))


def test_instantiation_never_collects(monkeypatch: pytest.MonkeyPatch) -> None:
    i, st = make(monkeypatch, "stress")
    h = st.heap
    while h.lo == gc.OBJ:  # each allocation collects, into the other half
        call(i, "chain", 1)
    lo, cap, n = h.lo, h.cap, h.collections
    assert lo == gc.OBJ + cap // 2
    h.u32(gc.CURSOR, h.end)  # full (the rest is not walked again: the store ends here)
    g = build(wat(CONSTS), store=cast(Any, i.inst).store)
    assert h.collections == n and h.lo == lo and h.cap > cap and h.end > h.cursor
    assert g.inst.global_get(0) is not None


def test_array_services_collect(monkeypatch: pytest.MonkeyPatch) -> None:
    i, st = make(monkeypatch, "stress", "poison", "verify")
    call(i, "chain", 1)
    n = st.heap.collections
    assert call(i, "services") == [1 + 5 + 6 + 8]
    assert st.heap.collections == n + 4  # struct.new, array.new, array.new_elem, array.new_data


# The independent review of step 5.3 (moved here as written, typing aside; see the interrupt test)
def fn(i: Inst, name: str, *args: Any) -> list[Any]:
    return call(i, name, *args)


def store_of(i: Inst) -> StoreState:
    return cast(Any, i.inst).store


NOGC = """(module (import "env" "cb" (func $cb))
  (global $g (mut externref) (ref.null extern))
  (table $t 2 externref)
  (func (export "f") (param externref) (result externref) (call $cb) (local.get 0))
  (func (export "set") (param externref)
    (global.set $g (local.get 0)) (table.set $t (i32.const 1) (local.get 0)))
  (func (export "g") (result externref) (global.get $g))
  (func (export "t") (result externref) (table.get $t (i32.const 1))))"""


def test_nongc_frames_globals_tables_keep_host_values(monkeypatch: pytest.MonkeyPatch) -> None:
    i, st = make(monkeypatch, "stress", "poison", "verify")
    store = store_of(i)

    def cb(_: Any) -> list[Any]:
        fn(i, "churn", 2)
        return []

    a = build(wat(NOGC), imports={"env": {"cb": cb}}, store=store)
    v, w = object(), object()
    fn(a, "set", w)
    n = st.heap.collections
    assert fn(a, "f", v)[0] is v
    assert st.heap.collections > n
    assert fn(a, "g")[0] is w and fn(a, "t")[0] is w


EXN = """(module
  (type $p (struct (field i32)))
  (tag $t (param anyref))
  (func (export "f") (result i32) (local $e exnref)
    (block $h (result anyref exnref)
      (try_table (catch_ref $t $h) (throw $t (struct.new $p (i32.const 42)))) unreachable)
    (local.set $e) (drop)
    (drop (struct.new $p (i32.const 7)))  ;; collects under stress
    (drop (struct.new $p (i32.const 8)))
    (struct.get $p 0 (ref.cast (ref $p)
      (block $h2 (result anyref)
        (try_table (catch $t $h2) (throw_ref (local.get $e))) unreachable)))))"""


def test_arena_payload_is_a_root(monkeypatch: pytest.MonkeyPatch) -> None:
    i, st = make(monkeypatch, "stress", "poison")
    e = build(wat(EXN), store=store_of(i))
    n = st.heap.collections
    assert fn(e, "f") == [42]
    assert st.heap.collections >= n + 2


def test_interrupt_while_the_collector_is_instantiated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Another thread's store.interrupt() in the window where the collector instance is in
    store.instances (simulated by calling it from inside the collector's instantiation)."""
    i, st = make(monkeypatch, "stress")
    store = store_of(i)
    real = inst_.JitInstance._const  # pyright: ignore[reportPrivateUsage]
    seen: list[Any] = []

    def const(self: Any, e: Any) -> int:
        if (
            self.store is store
            and not seen
            and any(n == "begin" for n, _, _ in self.module["exports"])
        ):
            seen.append(self)
            store.interrupt()
        return real(self, e)

    monkeypatch.setattr(inst_.JitInstance, "_const", const)
    # W's passive segment allocated at instantiation, so this first call collects and makes the
    # collector (the review's version expected the second call to): it completes, and the
    # interrupt, meant for the program, stops the next call at its entry
    fn(i, "chain", 1)
    assert seen
    try:
        fn(i, "chain", 1)
    except Trap:
        pass  # the interrupt meant for the program is fine
    assert not st.lost, st.lost
    assert fn(i, "sum") == [1]


def test_the_collector_leaves_the_store_under_the_interrupt_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """store.interrupt() walks its copy of store.instances under interrupt_lock, so removing the
    collector under the same lock means a walk that still saw it finishes before the remove, and
    the clear before each collection then wipes what it set (re-review of 5.3)."""
    i, st = make(monkeypatch, "stress")
    store = store_of(i)
    held: list[bool] = []

    class Watched(list[Any]):
        def remove(self, x: Any) -> None:
            held.append(store.interrupt_lock.locked())
            super().remove(x)

    monkeypatch.setattr(store, "instances", Watched(store.instances))
    fn(i, "chain", 1)
    assert held == [True]
    assert not st.lost, st.lost


HOSTCATCH = """(module (import "env" "cb" (func $cb))
  (type $p (struct (field (mut i32))))
  (func (export "run") (result i32) (local $x (ref $p))
    (local.set $x (struct.new $p (i32.const 11)))
    (call $cb)
    (struct.get $p 0 (local.get $x))))"""


def test_lost_store_does_not_resume_wasm(monkeypatch: pytest.MonkeyPatch) -> None:
    i, st = make(monkeypatch, "stress")
    fn(i, "chain", 3)
    k = fn(i, "keep")[0]
    caught: list[Any] = []

    def cb(_: Any) -> list[Any]:
        st.heap.u32(st.gc_handles[k.index], 0x1234)  # a corrupt word: the collector traps
        try:
            fn(i, "churn", 1)
        except Exception as e:  # a host that tolerates errors
            caught.append(e)
        return []

    h = build(wat(HOSTCATCH), imports={"env": {"cb": cb}}, store=store_of(i))
    with pytest.raises(RuntimeError, match=collect.LOST):
        fn(h, "run")  # the host returned normally: the bridge fails the call
    assert caught and st.lost
    with pytest.raises(RuntimeError, match=collect.LOST):
        cast(Any, h.inst).global_get(0)  # nor host access to globals or tables
    with pytest.raises(RuntimeError, match=collect.LOST):
        build(wat(HOSTCATCH), imports={"env": {"cb": cb}}, store=store_of(i))


def test_deepest_recursion_collects(monkeypatch: pytest.MonkeyPatch) -> None:
    i, _ = make(monkeypatch)
    lo, hi = 1, 1 << 20
    while lo < hi:
        mid = (lo + hi) // 2
        try:
            fn(i, "rec", mid)
            lo = mid + 1
        except Trap:
            hi = mid
    i, st = make(monkeypatch, "stress")
    fn(i, "chain", 1)
    try:
        r = fn(i, "rec", lo - 1)
    except Exception as e:
        pytest.fail(f"depth {lo - 1}: {type(e).__name__}: {e}; lost={st.lost}")
    assert r == [0]


# One test per check of the collection (S0013 5.3 review): each fails when the check is removed
TABLES = """(module (type $p (struct (field i32))) (tag $t)
  (table $f 1 funcref) (table $a 8000 anyref) (table $x 1 exnref) (elem declare func $one)
  (func $one (result i32) (i32.const 1))
  (func (export "setup") (local $i i32) (local $e exnref)
    (table.set $f (i32.const 0) (ref.func $one))
    (loop $l (table.set $a (local.get $i) (struct.new $p (local.get $i)))
      (br_if $l (i32.lt_u (local.tee $i (i32.add (local.get $i) (i32.const 1))) (i32.const 8000))))
    (local.set $e (block $h (result exnref) (try_table (catch_all_ref $h) (throw $t)) unreachable))
    (table.set $x (i32.const 0) (local.get $e)))
  (func (export "check") (result i32) (local $i i32) (local $s i32)
    (drop (struct.new $p (i32.const -1)))  ;; collects under stress
    (loop $l (local.set $s (i32.add (local.get $s)
        (struct.get $p 0 (ref.cast (ref $p) (table.get $a (local.get $i))))))
      (br_if $l (i32.lt_u (local.tee $i (i32.add (local.get $i) (i32.const 1))) (i32.const 8000))))
    (i32.add (i32.add (local.get $s) (call_indirect $f (result i32) (i32.const 0)))
      (ref.is_null (table.get $x (i32.const 0))))))"""


def test_tables_are_roots_in_batches_and_func_exn_tables_are_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """8 000 table roots cross ROOTS' 7 680-word bound; funcref and exnref words (addresses
    above 2**32) sent as roots would make the collector trap."""
    i, st = make(monkeypatch, "poison", "verify")
    t = build(wat(TABLES), store=store_of(i))
    fn(t, "setup")
    st.heap.stress = True  # from here: one collection in `check`, with 8 000 table roots
    st.heap.u32(gc.LIMIT, st.heap.cursor)
    sent: list[int] = []
    real = inst_.Instance.call

    def spy(self: Any, f: int, args: Any) -> Any:
        if (
            st.heap.collector
            and self is st.heap.collector[0]
            and f == st.heap.collector[1]["roots"]
        ):
            sent.append(args[0])
        return real(self, f, args)

    monkeypatch.setattr(inst_.Instance, "call", spy)
    assert fn(t, "check") == [8000 * 7999 // 2 + 1]
    assert len(sent) == 2 and sent[0] == collect.NROOTS and 8000 < sum(sent) < 2 * collect.NROOTS


def test_too_little_value_stack_traps_before_anything_moves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    i, st = make(monkeypatch, "stress", "verify")
    call(i, "chain", 2)
    n, lo = st.heap.collections, st.heap.lo
    monkeypatch.setattr(collect, "VSTACK_RESERVE", 1 << 30)
    with pytest.raises(Trap) as e:
        call(i, "chain", 1)
    assert e.value.code == TrapCode.STACK_OVERFLOW and st.heap.collections == n
    assert st.heap.lo == lo and not st.lost
    monkeypatch.setattr(collect, "VSTACK_RESERVE", 4 << 10)
    assert call(i, "sum") == [3]


def _after_finish(monkeypatch: pytest.MonkeyPatch, st: Any, then: Any) -> None:
    """Run `then()` right after each `finish` of the collector."""
    real = inst_.Instance.call

    def spy(self: Any, f: int, args: Any) -> Any:
        r = real(self, f, args)
        if (
            st.heap.collector
            and self is st.heap.collector[0]
            and f == st.heap.collector[1]["finish"]
        ):
            then()
        return r

    monkeypatch.setattr(inst_.Instance, "call", spy)


def test_verify_compares_the_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    i, st = make(monkeypatch, "stress", "verify")
    call(i, "chain", 1)
    keep: int = cast(Any, i.inst).gaddr[0]  # the address of global $keep

    def flip() -> None:  # a scalar of the moved object: the heap's form stays good
        st.heap.u32(ctypes.c_uint64.from_address(keep).value + 8, 99)

    _after_finish(monkeypatch, st, flip)
    with pytest.raises(RuntimeError, match="reachable graph changed"):
        call(i, "chain", 1)
    assert st.lost


def test_poison_fills_the_from_space(monkeypatch: pytest.MonkeyPatch) -> None:
    i, st = make(monkeypatch, "stress", "poison")
    call(i, "chain", 5)
    spaces: list[tuple[int, int]] = []
    _after_finish(monkeypatch, st, lambda: spaces.append((st.heap.lo, st.heap.cursor)))
    call(i, "chain", 1)
    lo, cur = spaces[0]  # at `finish`: the from-space, before the heap's words move
    assert lo != st.heap.lo and cur > lo
    assert ctypes.string_at(st.heap.base + lo, cur - lo) == bytes([0xA5]) * (cur - lo)


def test_an_exception_in_the_critical_section_loses_the_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    i, st = make(monkeypatch, "stress")
    call(i, "chain", 2)
    real = inst_.Instance.call

    def spy(self: Any, f: int, args: Any) -> Any:
        if (
            st.heap.collector
            and self is st.heap.collector[0]
            and f == st.heap.collector[1]["roots"]
        ):
            raise KeyboardInterrupt
        return real(self, f, args)

    monkeypatch.setattr(inst_.Instance, "call", spy)
    with pytest.raises(KeyboardInterrupt):
        call(i, "chain", 1)
    monkeypatch.setattr(inst_.Instance, "call", real)
    assert st.lost
    with pytest.raises(RuntimeError, match=collect.LOST):
        call(i, "sum")


def test_an_interrupt_during_a_collection_is_not_the_collectors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """store.interrupt() from another thread while the collector runs (here: before its `roots`)
    reaches the program's code only."""
    i, st = make(monkeypatch, "stress", "verify")
    call(i, "chain", 2)
    real = inst_.Instance.call

    def spy(self: Any, f: int, args: Any) -> Any:
        if (
            st.heap.collector
            and self is st.heap.collector[0]
            and f == st.heap.collector[1]["roots"]
        ):
            store_of(i).interrupt()
        return real(self, f, args)

    monkeypatch.setattr(inst_.Instance, "call", spy)
    with pytest.raises(Trap) as e:
        call(i, "intr")  # its allocation collects, then its loop polls
    assert e.value.code == TrapCode.INTERRUPT and not st.lost
