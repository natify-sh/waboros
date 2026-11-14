# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false
"""Exception handling on the native JIT (S0012 steps 3-4; aarch64 under QEMU, tools/run_arm64.py):
the cases of tests/test_exceptions_pyc.py, and what only the status path and the inline arena can
get wrong (pins across unwinding, v128 payloads, clause order, nested pads, imported tags, the
refill path, no Python on a throw). The spec suite (`exceptions/`) is the metric."""

import sys
import threading
from types import FrameType
from typing import Any

import pytest

from tests.jit import native, native_jit
from tests.jit.test_compiler import Inst, pins, wat
from tests.jit.test_compiler import build as _build
from tests.jit.test_tables import provider
from waboros.frontend.binary import parse
from waboros.frontend.validate import validate
from waboros.runtime.store import StoreState
from waboros.targets import base
from waboros.targets.base import Trap, TrapCode
from waboros.targets.jit.common import instance
from waboros.targets.jit.common.instance import ExnRef, Tag

__all__ = ["pins"]  # the fixture, used by name
JIT = native_jit()


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
    """520 words: the object is 4168 bytes, which no aarch64 add imm12 takes (LESSONS 83); each
    word lands at its own position (a weighted sum), and the cursor moves by the whole object."""
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
    st: Any = store.target_state
    cursor = st.exn[1]
    assert i("go") == [sum((k * k + 1) * (k + 1) for k in range(n)) % 2**64]
    assert st.exn[1] - cursor == 8 * (1 + n)


def test_catch_ref_and_throw_ref_keep_the_object(pins: None) -> None:
    i = build("""
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
""")
    assert i("go", 42) == [42]
    assert i("wrapped") == [8]
    for name in ("null", "null_in_try"):  # a trap: not caught
        assert raises(i, name).code is TrapCode.NULL_REFERENCE


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


def test_the_refill_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """An object here is 4 words: 2048 fit a default chunk; with chunks smaller than an object
    every throw refills with the size the code passes, then allocates again."""
    store = StoreState()
    i = build(LOOP, store=store)
    st: Any = store.target_state  # the JIT's per-store state: the arena's chunks
    first = st.chunks[0]
    n = len(st.chunks)
    assert i("loop", 10000) == [10000 * 10001 // 2]  # 4 words each: 2048 per 8192-word chunk
    assert len(st.chunks) == n + 4 and st.chunks[0] is first  # chunks never move
    monkeypatch.setattr(instance, "ARENA_WORDS", 3)  # smaller than an object: max(3, 4)
    st.exn[2] = st.exn[1]  # the current chunk is full
    n = len(st.chunks)
    assert i("loop", 5) == [15]
    assert len(st.chunks) == n + 5 and all(len(c) == 4 for c in st.chunks[n:])
    e = raises(i, "escape")
    assert e.exn[1] == (5, 6 | 7 << 64)


def test_a_failing_refill_is_a_host_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """The refill service fails once (MemoryError): status 13 with the error pending, so the
    catch_all does not catch it and the entry raises the error itself. Failing once only, so a
    refill that reported 0 would retry, succeed and be caught: a result, not a hang."""
    store = StoreState()
    i = build(
        """
(module (tag $t)
  (func (export "go") (result i32) (block (try_table (catch_all 0) (throw $t))) (i32.const 1)))
""",
        store=store,
    )
    st: Any = store.target_state
    real, calls = st.new_chunk, [0]

    def once(words: int) -> None:
        calls[0] += 1
        if calls[0] == 1:
            raise MemoryError("arena")
        real(words)

    monkeypatch.setattr(st, "new_chunk", once)
    st.exn[2] = st.exn[1]  # the current chunk is full
    with pytest.raises(MemoryError, match="arena"):
        i("go")
    assert calls[0] == 1
    assert i("go") == [1]  # the next refill works: thrown and caught
