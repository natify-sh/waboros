# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false
"""Exception handling on `pyc` (S0012 step 2): what only a `try_table` handler can get wrong.
The spec suite (`exceptions/`) is the metric; these cover the host boundary, traps, chains and
nesting."""

import tracemalloc
from typing import Any

import pytest

from tests.watcache import wat2wasm
from waboros.targets.base import Trap, TrapCode, Unsupported
from waboros.targets.pyc import load


def run(text: str, imports: dict[str, Any] | None = None) -> dict[str, Any]:
    return load(wat2wasm(text), imports or {})


def test_payload_of_every_kind_is_caught() -> None:
    ex = run(
        """
(module
  (tag $t (param i32 i64 f32 f64))
  (func (export "go") (result i32 i64 f32 f64)
    (block $h (result i32 i64 f32 f64)
      (try_table (catch $t $h)
        (i32.const -1) (i64.const -2) (f32.const 1.5) (f64.const -2.5) (throw $t))
      unreachable)))
"""
    )
    assert ex["go"]() == (0xFFFFFFFF, 0xFFFFFFFFFFFFFFFE, 1.5, -2.5)


def test_catch_ref_and_throw_ref_keep_the_object() -> None:
    ex = run(
        """
(module
  (tag $t (param i32))
  (func (export "go") (param i32) (result i32)
    (block $outer (result i32)
      (try_table (result i32) (catch $t $outer)
        (block $r (result i32 exnref)
          (try_table (result i32) (catch_ref $t $r)
            (throw $t (local.get 0)))
          (return))
        (throw_ref))
      unreachable))
  (func (export "null") (result i32) (throw_ref (ref.null exn)) (i32.const 0)))
"""
    )
    assert ex["go"](42) == 42
    with pytest.raises(Trap) as e:
        ex["null"]()
    assert e.value.code is TrapCode.NULL_REFERENCE


def test_three_frames_deep_with_live_locals() -> None:
    ex = run(
        """
(module
  (tag $t (param i32))
  (func $c (param i32) (local $k i32)
    (local.set $k (i32.const 5)) (throw $t (i32.add (local.get 0) (local.get $k))))
  (func $b (param i32) (result i32) (local $k i32)
    (local.set $k (i32.const 100)) (call $c (local.get 0)) (local.get $k))
  (func $a (param i32) (result i32) (local $k i32) (local.set $k (i32.const 1000))
    (block $h (result i32)
      (try_table (result i32) (catch $t $h) (call $b (local.get 0))))
    (i32.add (local.get $k)))
  (export "go" (func $a)))
"""
    )
    assert ex["go"](20) == 1025


def test_escaping_exception_is_a_trap_with_the_exception() -> None:
    ex = run('(module (tag $t (param i32)) (func (export "go") (throw $t (i32.const 9))))')
    with pytest.raises(Trap) as e:
        ex["go"]()
    assert e.value.code is TrapCode.UNCAUGHT_EXCEPTION
    assert e.value.exn[1] == (9,)


def test_traps_and_host_errors_are_not_caught() -> None:
    def boom() -> None:
        raise ValueError("host")

    ex = run(
        """
(module
  (import "h" "boom" (func $boom))
  (func (export "trap") (try_table (catch_all 0) (unreachable)))
  (func (export "div") (result i32)
    (block (try_table (catch_all 0) (drop (i32.div_u (i32.const 1) (i32.const 0)))))
    (i32.const 0))
  (func (export "host") (try_table (catch_all 0) (call $boom)))
  (func $rec (result i32) (block (try_table (catch_all 0) (drop (call $rec)))) (i32.const 0))
  (export "rec" (func $rec)))
""",
        {"h": {"boom": boom}},
    )
    with pytest.raises(Trap) as e:
        ex["trap"]()
    assert e.value.code is TrapCode.UNREACHABLE
    with pytest.raises(Trap) as e:
        ex["div"]()
    assert e.value.code is TrapCode.INTEGER_DIVISION_BY_ZERO
    with pytest.raises(ValueError):
        ex["host"]()
    with pytest.raises(Trap) as e:
        ex["rec"]()
    assert e.value.code is TrapCode.STACK_OVERFLOW


def test_exception_through_a_host_function() -> None:
    seen: list[Any] = []
    box: dict[str, Any] = {}

    def host(reraise: int) -> None:
        try:
            box["ex"]["inner"]()
        except Trap as t:
            seen.append(t)
            if reraise:
                raise

    box["ex"] = ex = run(
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
    assert ex["outer"](0) == 1  # the host swallowed it: the outer call goes on
    assert seen[0].code is TrapCode.UNCAUGHT_EXCEPTION and seen[0].exn[1] == (3,)
    with pytest.raises(
        Trap
    ) as e:  # re-raised: a host error, not catchable by the caller's catch_all
        ex["outer"](1)
    assert e.value is seen[1]


def test_return_call_leaves_the_try() -> None:
    ex = run(
        """
(module
  (tag $t)
  (func $thrower (result i32) (throw $t))
  (func (export "go") (result i32)
    (block $h
      (try_table (catch_all $h) (return_call $thrower))
      (return (i32.const 1)))
    (i32.const 2)))
"""
    )
    with pytest.raises(Trap) as e:
        ex["go"]()
    assert e.value.code is TrapCode.UNCAUGHT_EXCEPTION


def test_rethrow_in_a_loop_does_not_grow() -> None:
    ex = run(
        """
(module
  (tag $t)
  (func (export "go") (param $n i32)
    (local $x exnref)
    (block $h (result exnref)
      (try_table (catch_all_ref $h) (throw $t))
      (unreachable))
    (local.set $x)
    (loop $l
      (block $c (result exnref)
        (try_table (catch_all_ref $c) (throw_ref (local.get $x)))
        (unreachable))
      (local.set $x)
      (local.set $n (i32.sub (local.get $n) (i32.const 1)))
      (br_if $l (local.get $n)))))
"""
    )
    ex["go"](100)
    tracemalloc.start()
    ex["go"](1000)
    a = tracemalloc.get_traced_memory()[0]
    ex["go"](100000)
    b = tracemalloc.get_traced_memory()[0]
    tracemalloc.stop()
    assert b - a < 100_000


def nest(n: int) -> str:
    body = "(unreachable)"
    for _ in range(n):
        body = f"(try_table (catch_all 0) {body})"
    return f'(module (func (export "go") {body}))'


def test_nesting_limit() -> None:
    assert run(nest(15))["go"]  # within CPython's 20 statically nested blocks
    with pytest.raises(Unsupported):
        run(nest(40))
