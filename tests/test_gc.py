# ruff: noqa: E501
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false
"""Wasm GC on the targets (S0013): values, casts across modules, conversions, allocating constant
expressions, the array operators, trap order and the allocation limits. The expected values were
checked against Wasmtime 50.0.1. The JITs join at step 3b."""

import sys
from typing import Any

import pytest

from tests.jit import native
from tests.test_exceptions_targets import call, make
from tests.watcache import wat2wasm
from waboros.frontend.binary import parse
from waboros.frontend.validate import Invalid, validate
from waboros.runtime.store import StoreState
from waboros.targets.base import Trap, TrapCode
from waboros.targets.pyc import translate

TARGETS = ["pyc", "jit-x86_64"]
ARRAYS = [  # tests that need array instructions
    "pyc",
    pytest.param("jit-x86_64", marks=pytest.mark.skip(reason="arrays on the JIT: S0013 step 3b.3")),
]


@pytest.fixture(autouse=True)
def _jit(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """A JIT parameter runs only where that JIT runs; GC is compiled through WABOROS_JIT_GC=1 until
    `jit-x86_64` claims it (S0013 step 3b.3)."""
    spec = getattr(request.node, "callspec", None)
    target = spec.params.get("target", "") if spec else ""
    if target.startswith("jit-"):
        native(target)
        monkeypatch.setenv("WABOROS_JIT_GC", "1")


U32 = 0xFFFFFFFF


def trap(p: Any, name: str, *args: Any) -> TrapCode:
    with pytest.raises(Trap) as e:
        call(p, name, *args)
    return e.value.code


def sx(v: int, bits: int) -> int:
    v &= (1 << bits) - 1
    return v - (v >> (bits - 1) << bits)


# ---- every storage type, packed reads
@pytest.mark.parametrize("target", ARRAYS)
def test_every_storage_type_in_structs_and_arrays(target: str) -> None:
    kinds = [  # (storage type, value type, two constants, their raw form)
        ("i8", "i32", "(i32.const 7)", "(i32.const 250)", 7, 250),
        ("i16", "i32", "(i32.const 300)", "(i32.const 65000)", 300, 65000),
        ("i32", "i32", "(i32.const -5)", "(i32.const 9)", U32 - 4, 9),
        ("i64", "i64", "(i64.const -5)", "(i64.const 9)", 2**64 - 5, 9),
        ("f32", "f32", "(f32.const 1.5)", "(f32.const -2.5)", 0x3FC00000, 0xC0200000),
        ("f64", "f64", "(f64.const 1.5)", "(f64.const -2.5)", 0x3FF8 << 48, 0xC004 << 48),
        ("anyref", "anyref", "(ref.null any)", "(ref.i31 (i32.const 3))", None, 3),
    ]
    for st, vt, c1, c2, r1, r2 in kinds:
        get = "get_u" if st in ("i8", "i16") else "get"
        p = make(
            target,
            f"""(module
          (type $s (struct (field (mut {st})) (field (mut {st})))) (type $a (array (mut {st})))
          (func (export "s0") (result {vt}) (struct.{get} $s 0 (struct.new $s {c1} {c2})))
          (func (export "s1") (result {vt}) (local $x (ref $s))
            (local.set $x (struct.new_default $s)) (struct.set $s 1 (local.get $x) {c2})
            (struct.{get} $s 1 (local.get $x)))
          (func (export "a1") (result {vt}) (local $x (ref $a))
            (local.set $x (array.new $a {c1} (i32.const 3)))
            (array.set $a (local.get $x) (i32.const 1) {c2})
            (array.{get} $a (local.get $x) (i32.const 1)))
          (func (export "a0") (result {vt}) (array.{get} $a (array.new $a {c1} (i32.const 3)) (i32.const 2)))
          (func (export "a2") (result {vt})
            (array.{get} $a (array.new_fixed $a 3 {c1} {c2} {c1}) (i32.const 1)))
          (func (export "len") (result i32) (array.len (array.new_default $a (i32.const 5)))))""",
        )
        assert call(p, "len") == [5]
        want = {"s0": r1, "s1": r2, "a1": r2, "a0": r1, "a2": r2}
        assert {f: call(p, f)[0] for f in want} == want, st
    p = make(
        target,
        """(module (type $s (struct (field (mut v128)) (field (mut (ref null $s)))))
      (type $a (array (mut v128)))
      (func (export "v") (result i32) (local $x (ref $s))
        (local.set $x (struct.new $s (v128.const i32x4 1 2 3 4) (ref.null $s)))
        (struct.set $s 0 (local.get $x) (v128.const i32x4 5 6 7 8))
        (i32x4.extract_lane 2 (struct.get $s 0 (local.get $x))))
      (func (export "w") (result i32)
        (i32x4.extract_lane 3 (array.get $a (array.new $a (v128.const i32x4 5 6 7 9) (i32.const 2)) (i32.const 1))))
      (func (export "n") (result i32) (ref.is_null (struct.get $s 1 (struct.new_default $s)))))""",
    )
    assert call(p, "v") == [7] and call(p, "w") == [9] and call(p, "n") == [1]


@pytest.mark.parametrize("target", ARRAYS)
def test_packed_fields_and_elements_sign_and_zero_extend(target: str) -> None:
    p = make(
        target,
        """(module
      (type $s (struct (field (mut i8)) (field (mut i16))))
      (type $a (array (mut i8))) (type $b (array (mut i16)))
      (func (export "sget") (param i32 i32 i32) (result i32) (local $x (ref $s))
        (local.set $x (struct.new $s (local.get 0) (local.get 0)))
        (if (result i32) (local.get 1)
          (then (if (result i32) (local.get 2) (then (struct.get_s $s 1 (local.get $x))) (else (struct.get_s $s 0 (local.get $x)))))
          (else (if (result i32) (local.get 2) (then (struct.get_u $s 1 (local.get $x))) (else (struct.get_u $s 0 (local.get $x)))))))
      (func (export "aget") (param i32 i32 i32) (result i32) (local $x (ref $a)) (local $y (ref $b))
        (local.set $x (array.new $a (local.get 0) (i32.const 3)))
        (local.set $y (array.new $b (local.get 0) (i32.const 3)))
        (if (result i32) (local.get 1)
          (then (if (result i32) (local.get 2) (then (array.get_s $b (local.get $y) (i32.const 2))) (else (array.get_s $a (local.get $x) (i32.const 2)))))
          (else (if (result i32) (local.get 2) (then (array.get_u $b (local.get $y) (i32.const 2))) (else (array.get_u $a (local.get $x) (i32.const 2)))))))
      )""",
    )
    for v in (0x7F, 0x80, 0xFF, 0x7FFF, 0x8000, 0x1FF, 0x12345, U32):
        for fn in ("sget", "aget"):
            for signed in (0, 1):
                for wide in (0, 1):
                    bits = 16 if wide else 8
                    want = sx(v, bits) & U32 if signed else v & ((1 << bits) - 1)
                    assert call(p, fn, v, signed, wide) == [want], (fn, hex(v), signed, wide)


# ---- i31 and ref.eq
@pytest.mark.parametrize("target", TARGETS)
def test_i31_values_wrap_and_compare_by_value(target: str) -> None:
    p = make(
        target,
        """(module
      (type $s (struct))
      (func (export "gs") (param i32) (result i32) (i31.get_s (ref.i31 (local.get 0))))
      (func (export "gu") (param i32) (result i32) (i31.get_u (ref.i31 (local.get 0))))
      (func (export "eq") (param i32 i32) (result i32) (ref.eq (ref.i31 (local.get 0)) (ref.i31 (local.get 1))))
      (func (export "eqnull") (param i32) (result i32) (ref.eq (ref.i31 (local.get 0)) (ref.null eq)))
      (func (export "eqq") (param i32 i32) (result i32)  ;; eqref operands: any value, i31 compared by value
        (ref.eq (block (result eqref) (ref.i31 (local.get 0))) (block (result eqref) (ref.i31 (local.get 1)))))
      (func (export "nulls") (result i32) (ref.eq (ref.null eq) (ref.null eq)))
      (func (export "structs") (param i32) (result i32) (local $a (ref $s)) (local.set $a (struct.new_default $s))
        (ref.eq (local.get $a) (if (result (ref eq)) (local.get 0) (then (local.get $a)) (else (struct.new_default $s)))))
      (func (export "mixed") (result i32) (ref.eq (ref.i31 (i32.const 1)) (struct.new_default $s)))
      (func (export "null_get") (result i32) (i31.get_s (ref.null i31))))""",
    )
    for v in (0, 2**30 - 1, 2**30, 2**31 - 1, 2**31, U32, 5):
        low = v & 0x7FFFFFFF
        assert call(p, "gs", v) == [sx(low, 31) & U32]
        assert call(p, "gu", v) == [low]
    assert call(p, "eq", 0, 2**31) == [1] and call(p, "eq", 2**30 - 1, 2**30 - 1) == [1]
    assert call(p, "eq", 2**30, 2**30 - 1) == [0] and call(p, "eq", 7, 8) == [0]
    assert call(p, "eqq", 1000, 1000) == [1] and call(p, "eqq", 1000, 1001) == [0]
    assert call(p, "eqnull", 0) == [0] and call(p, "nulls") == [1]
    assert call(p, "structs", 1) == [1] and call(p, "structs", 0) == [0]
    assert call(p, "mixed") == [0] and trap(p, "null_get") is TrapCode.NULL_REFERENCE


# ---- casts across modules, display depths, finality
RECA = """(module (rec (type $a (sub (struct (field i32)))) (type $b (sub $a (struct (field i32) (field i32)))))
  (func (export "mk_a") (result anyref) (struct.new $a (i32.const 1)))
  (func (export "mk_b") (result anyref) (struct.new $b (i32.const 1) (i32.const 2)))
  (func (export "is_a") (param anyref) (result i32) (ref.test (ref $a) (local.get 0)))
  (func (export "is_b") (param anyref) (result i32) (ref.test (ref $b) (local.get 0))))"""
OTHERPOS = """(module (rec (type $pad (sub (struct (field i32)))) (type $a (sub (struct (field i32)))) (type $b (sub $a (struct (field i32) (field i32)))))
  (func (export "is_a") (param anyref) (result i32) (ref.test (ref $a) (local.get 0)))
  (func (export "is_b") (param anyref) (result i32) (ref.test (ref $b) (local.get 0))))"""


@pytest.mark.parametrize("target", TARGETS)
def test_casts_follow_canonical_types_across_modules(target: str) -> None:
    store = StoreState()  # objects cross between modules of one store (the JIT checks it)
    a, same, other = (
        make(target, t, store=store) for t in (RECA, RECA.replace("mk_", "mm_"), OTHERPOS)
    )
    oa, ob = call(a, "mk_a")[0], call(a, "mk_b")[0]
    for p, want in ((a, (1, 0, 1, 1)), (same, (1, 0, 1, 1)), (other, (0, 0, 0, 0))):
        assert [call(p, f, o)[0] for f in ("is_a", "is_b") for o in (oa, ob)] == [
            want[0],
            want[2],
            want[1],
            want[3],
        ]


@pytest.mark.parametrize("target", ARRAYS)
def test_display_depths_and_finality(target: str) -> None:
    types = """(type $t0 (sub (struct))) (type $t1 (sub $t0 (struct (field i32))))
      (type $t2 (sub $t1 (struct (field i32) (field i32)))) (type $t3 (sub final $t2 (struct (field i32) (field i32) (field i32))))
      (type $arr (sub (array i32))) (type $arr2 (sub final $arr (array i32)))"""
    mk = [
        "(struct.new_default $t0)",
        "(struct.new $t1 (i32.const 0))",
        "(struct.new $t2 (i32.const 0) (i32.const 0))",
        "(struct.new $t3 (i32.const 0) (i32.const 0) (i32.const 0))",
    ]
    fns = "".join(
        f'(func (export "mk{i}") (result anyref) {m}) (func (export "is{i}") (param anyref) (result i32) (ref.test (ref $t{i}) (local.get 0)))'
        f'(func (export "isn{i}") (param anyref) (result i32) (ref.test (ref null $t{i}) (local.get 0)))'
        f'(func (export "cast{i}") (param anyref) (result i32) (drop (ref.cast (ref $t{i}) (local.get 0))) (i32.const 1))'
        for i, m in enumerate(mk)
    )
    p = make(
        target,
        f"""(module {types} {fns}
      (func (export "mkarr") (result anyref) (array.new_default $arr2 (i32.const 1)))
      (func (export "arr") (param anyref) (result i32) (ref.test (ref $arr) (local.get 0)))
      (func (export "str") (param anyref) (result i32) (ref.test (ref struct) (local.get 0)))
      (func (export "eq") (param anyref) (result i32) (ref.test (ref eq) (local.get 0)))
      (func (export "arrt") (param anyref) (result i32) (ref.test (ref array) (local.get 0))))""",
    )
    objs = [call(p, f"mk{i}")[0] for i in range(4)]
    for i in range(4):
        for j in range(4):
            assert call(p, f"is{j}", objs[i]) == [int(i >= j)], (i, j)
            assert call(p, f"isn{j}", objs[i]) == [int(i >= j)]
            if i >= j:
                assert call(p, f"cast{j}", objs[i]) == [1]
            else:
                assert trap(p, f"cast{j}", objs[i]) is TrapCode.CAST_FAILURE
        assert call(p, f"is{i}", None) == [0] and call(p, f"isn{i}", None) == [1]
    arr = call(p, "mkarr")[0]
    assert call(p, "arr", arr) == [1] and call(p, "str", arr) == [0] and call(p, "arrt", arr) == [1]
    assert (
        call(p, "str", objs[0]) == [1] and call(p, "eq", arr) == [1] and call(p, "is0", arr) == [0]
    )
    assert call(p, "eq", object()) == [0]  # a host value passed as anyref is not an eqref


# ---- call_indirect to a non-final type
FUNCS = """(type $f (sub (func (param i32) (result i32)))) (type $g (sub $f (func (param i32) (result i32))))
  (type $k (sub $g (func (param i32) (result i32)))) (type $h (func (param i32) (result i32)))"""


@pytest.mark.parametrize("target", TARGETS)
def test_call_indirect_to_a_non_final_expected_type(target: str) -> None:
    store = StoreState()
    a = make(
        target,
        f"""(module {FUNCS} (table (export "t") funcref (elem $fg $fh $ff $fk))
      (func $fg (export "fg") (type $g) (i32.add (local.get 0) (i32.const 1)))
      (func $fh (type $h) (i32.add (local.get 0) (i32.const 2)))
      (func $ff (type $f) (i32.add (local.get 0) (i32.const 3)))
      (func $fk (type $k) (i32.add (local.get 0) (i32.const 4)))
      (func (export "cf") (param i32 i32) (result i32) (call_indirect (type $f) (local.get 1) (local.get 0)))
      (func (export "cg") (param i32 i32) (result i32) (call_indirect (type $g) (local.get 1) (local.get 0)))
      (func (export "ch") (param i32 i32) (result i32) (call_indirect (type $h) (local.get 1) (local.get 0))))""",
        store=store,
    )
    ok = {"cf": (0, 2, 3), "cg": (0, 3), "ch": (1,)}  # $k is a subtype of $g, which is one of $f
    for fn, good in ok.items():
        for i in range(4):
            if i in good:
                assert call(a, fn, i, 10) == [11 + i], (fn, i)
            else:
                assert trap(a, fn, i, 10) is TrapCode.BAD_SIGNATURE, (fn, i)
    # a module with no GC type (a non-final type with no supertype) calls A's function: the
    # subtype in the other module passes, an equivalent final type does not
    b = make(
        target,
        """(module (type $f (sub (func (param i32) (result i32))))
      (import "a" "fg" (func $fg (type $f))) (table funcref (elem $fg))
      (func (export "cf") (param i32) (result i32) (call_indirect (type $f) (local.get 0) (i32.const 0))))""",
        {"a": a},
        store,
    )
    assert call(b, "cf", 5) == [6]


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("extra", ["", "(type $S (struct))"])  # with a GC type: reference masks on
def test_return_call_indirect_to_a_non_final_type(target: str, extra: str) -> None:
    p = make(
        target,
        f"""(module (type $F (sub (func (result i32)))) {extra}
      (func $a (type $F) (i32.const 5)) (table $t 2 funcref) (elem (i32.const 0) func $a)
      (func (export "rci") (param i32) (result i32) (return_call_indirect $t (type $F) (local.get 0))))""",
    )
    assert call(p, "rci", 0) == [5]
    assert trap(p, "rci", 1) is TrapCode.INDIRECT_CALL_TO_NULL
    assert trap(p, "rci", 2) is TrapCode.TABLE_OUT_OF_BOUNDS


# ---- conversions
@pytest.mark.parametrize("target", TARGETS)
def test_conversions_round_trip(target: str) -> None:
    p = make(
        target,
        """(module (type $s (struct (field i32)))
      (func (export "rt") (param externref) (result externref) (extern.convert_any (any.convert_extern (local.get 0))))
      (func (export "host_any") (param externref) (result i32) (ref.test (ref any) (any.convert_extern (local.get 0))))
      (func (export "host_eq") (param externref) (result i32) (ref.test (ref eq) (any.convert_extern (local.get 0))))
      (func (export "mk") (result externref) (extern.convert_any (struct.new $s (i32.const 7))))
      (func (export "mk31") (result externref) (extern.convert_any (ref.i31 (i32.const -9))))
      (func (export "back") (param externref) (result i32) (ref.test (ref $s) (any.convert_extern (local.get 0))))
      (func (export "back31") (param externref) (result i32) (i31.get_s (ref.cast (ref i31) (any.convert_extern (local.get 0)))))
      (func (export "id") (result i32) (local $s (ref $s)) (local.set $s (struct.new $s (i32.const 7)))
        (ref.eq (ref.cast (ref $s) (any.convert_extern (extern.convert_any (local.get $s)))) (local.get $s))))""",
    )
    host = object()
    assert call(p, "rt", host)[0] is host and call(p, "rt", None) == [None]
    assert call(p, "host_any", host) == [1] and call(p, "host_eq", host) == [0]
    gc, i31 = call(p, "mk")[0], call(p, "mk31")[0]
    assert (
        call(p, "back", gc) == [1]
        and call(p, "back", host) == [0]
        and call(p, "back31", i31) == [U32 - 8]
    )
    assert call(p, "id") == [1]


@pytest.mark.parametrize("target", TARGETS)
def test_a_host_value_for_an_anyref_is_internalized(target: str) -> None:
    box: list[Any] = [None]

    def h(args: list[Any]) -> list[Any]:
        return [box[0]]

    def k(args: list[Any]) -> list[Any]:
        return [args[0]]

    hosts = {"env": {"h": h, "k": k}}
    p = make(
        target,
        """(module (import "env" "h" (func $h (result anyref)))
      (import "env" "k" (func $k (param anyref) (result anyref)))
      (func (export "eq") (result i32) (ref.test (ref eq) (call $h)))
      (func (export "any") (result i32) (ref.test (ref any) (call $h)))
      (func (export "i31") (result i32) (i31.get_s (ref.cast (ref i31) (call $h))))
      (func (export "echo") (param anyref) (result anyref) (call $k (local.get 0))))""",
        hosts,
    )
    box[0] = object()  # not an int, not a GC object: a host value
    assert call(p, "eq") == [0] and call(p, "any") == [1]
    box[0] = [1, 2]  # a host list is not a struct either
    assert call(p, "eq") == [0] and call(p, "any") == [1]
    box[0] = -7  # an int is an i31 value
    assert call(p, "i31") == [U32 - 6]
    box[0] = -(2**30)
    assert call(p, "i31") == [U32 + 1 - 2**30]
    for v in (2**30, -(2**30) - 1):  # outside the i31 range: an error, not a wrap (S0013 3b.1)
        box[0] = v
        with pytest.raises(ValueError, match="i31"):
            call(p, "any")
        with pytest.raises(ValueError, match="i31"):
            call(p, "echo", v)
    o = object()
    assert call(p, "echo", o)[0] is o and call(p, "echo", None) == [None]  # the object itself


CHECKED = """(module (type $a (sub (struct (field i32)))) (type $b (sub $a (struct (field i32) (field i32))))
  (type $f (func)) (type $g (func (param i32))) (func $ff (type $f)) (func $gg (type $g))
  (elem declare func $ff $gg)
  (func (export "mka") (result anyref) (struct.new $a (i32.const 1)))
  (func (export "mkb") (result anyref) (struct.new $b (i32.const 2) (i32.const 3)))
  (func (export "fa") (param (ref null $a)) (result i32) (struct.get $a 0 (local.get 0)))
  (func (export "fb") (param (ref null $b)) (result i32) (struct.get $b 1 (local.get 0)))
  (func (export "fnn") (param (ref $a)) (result i32) (struct.get $a 0 (local.get 0)))
  (func (export "eq") (param eqref) (result i32) (ref.is_null (local.get 0)))
  (func (export "st") (param structref) (result i32) (ref.is_null (local.get 0)))
  (func (export "i31") (param i31ref) (result i32) (ref.is_null (local.get 0)))
  (func (export "none") (param nullref) (result i32) (ref.is_null (local.get 0)))
  (func (export "getf") (result funcref) (ref.func $ff))
  (func (export "getg") (result funcref) (ref.func $gg))
  (func (export "cf") (param (ref null $f)) (call_ref $f (local.get 0)))
  (func (export "anyf") (param funcref) (result i32) (ref.is_null (local.get 0)))
  (func (export "exn") (param exnref) (result i32) (ref.is_null (local.get 0))))"""


@pytest.mark.parametrize("target", TARGETS)
def test_host_values_are_checked_against_the_parameter_type(target: str) -> None:
    """As Wasmtime: a value that does not match a reference parameter is a `TypeError` before the
    call (S0013 3b.2 review: on the JIT a wrong word corrupts the heap)."""
    p = make(target, CHECKED)
    a, b, f, g = (call(p, n)[0] for n in ("mka", "mkb", "getf", "getg"))
    assert call(p, "fa", b) == [2] and call(p, "fa", a) == [1]  # a subtype for its supertype
    assert call(p, "eq", 5) == [0] and call(p, "eq", a) == [0] and call(p, "st", a) == [0]
    assert call(p, "i31", 5) == [0] and call(p, "none", None) == [1] and call(p, "cf", f) == []
    assert call(p, "anyf", g) == [0] and call(p, "exn", None) == [1]
    bad = [("fb", a), ("fa", 0), ("fnn", None), ("eq", object()), ("st", 5), ("i31", a),
           ("none", 5), ("cf", g), ("fa", object()), ("anyf", object()), ("anyf", 5),
           ("anyf", a), ("exn", object()), ("exn", 5)]  # fmt: skip
    for fn, v in bad:
        with pytest.raises(TypeError):
            call(p, fn, v)
    assert trap(p, "fa", None) is TrapCode.NULL_REFERENCE  # nullable: wasm's own null check


# ---- allocating constant expressions
@pytest.mark.parametrize("target", ARRAYS)
def test_constant_expressions_allocate(target: str) -> None:
    src = """(module (type $s (struct (field i32) (field (mut i8)))) (type $a (array (mut i16)))
      (global $gs (ref $s) (struct.new $s (i32.const 7) (i32.const 0x1FF)))
      (global $gd (ref $s) (struct.new_default $s))
      (global $ga (ref $a) (array.new_fixed $a 3 (i32.const 1) (i32.const 0x10002) (i32.const 3)))
      (global $gn (ref $a) (array.new $a (i32.const 9) (i32.const 4)))
      (global $gz (ref $a) (array.new_default $a (i32.const 2)))
      (global $gi (ref i31) (ref.i31 (i32.const 0x80000005)))
      (global $gx anyref (any.convert_extern (ref.null extern)))
      (table $t 3 (ref $s) (struct.new $s (i32.const 11) (i32.const 1)))
      (elem $e (ref $s) (struct.new $s (i32.const 21) (i32.const 1)) (struct.new_default $s) (global.get $gs))
      (func (export "gs") (result i32) (struct.get $s 0 (global.get $gs)))
      (func (export "gs1") (result i32) (struct.get_u $s 1 (global.get $gs)))
      (func (export "gd") (result i32) (struct.get $s 0 (global.get $gd)))
      (func (export "ga") (param i32) (result i32) (array.get_u $a (global.get $ga) (local.get 0)))
      (func (export "gn") (result i32) (i32.add (array.len (global.get $gn)) (array.get_u $a (global.get $gn) (i32.const 3))))
      (func (export "gz") (result i32) (array.get_u $a (global.get $gz) (i32.const 1)))
      (func (export "gi") (result i32) (i31.get_u (global.get $gi)))
      (func (export "gx") (result i32) (ref.is_null (global.get $gx)))
      (func (export "tab") (param i32) (result i32) (struct.get $s 0 (table.get $t (local.get 0))))
      (func (export "tab_same") (result i32) (ref.eq (table.get $t (i32.const 0)) (table.get $t (i32.const 2))))
      (func (export "elem") (param i32) (result i32) (struct.get $s 0 (array.get $es (array.new_elem $es $e (i32.const 0) (i32.const 3)) (local.get 0))))
      (func (export "elem_g") (result i32) (ref.eq (global.get $gs) (array.get $es (array.new_elem $es $e (i32.const 0) (i32.const 3)) (i32.const 2))))
      (func (export "tinit") (result i32) (table.init $t $e (i32.const 0) (i32.const 0) (i32.const 3)) (struct.get $s 0 (table.get $t (i32.const 0))))
      (type $es (array (ref $s))))"""
    p = make(target, src)
    assert call(p, "gs") == [7] and call(p, "gs1") == [0xFF] and call(p, "gd") == [0]
    assert [call(p, "ga", i)[0] for i in range(3)] == [1, 2, 3]
    assert call(p, "gn") == [4 + 9] and call(p, "gz") == [0]
    assert call(p, "gi") == [5] and call(p, "gx") == [1]
    assert [call(p, "tab", i)[0] for i in range(3)] == [11, 11, 11] and call(p, "tab_same") == [1]
    assert [call(p, "elem", i)[0] for i in range(3)] == [21, 0, 7] and call(p, "elem_g") == [1]
    assert call(p, "tinit") == [21]
    # each instance has its own objects: the globals are evaluated at instantiation
    q = make(target, src)
    assert call(q, "gs") == [7]


# ---- the array operators: overlapping ranges, every operand of its own size (LESSONS 97)
BULK = """(module (type $a (array (mut i32))) (type $b (array (mut i8))) (type $r (array (mut anyref)))
  (data $d "\\01\\02\\03\\04\\05\\06\\07\\08\\09\\0a\\0b") (elem $e anyref (ref.i31 (i32.const 1)) (ref.i31 (i32.const 2)) (ref.i31 (i32.const 3)) (ref.null any) (ref.i31 (i32.const 5)))
  (global $A (mut (ref null $a)) (ref.null $a)) (global $B (mut (ref null $a)) (ref.null $a))
  (global $I (mut (ref null $b)) (ref.null $b)) (global $R (mut (ref null $r)) (ref.null $r))
  (start $init)
  (func $init (global.set $A (array.new_fixed $a 7 (i32.const 0) (i32.const 1) (i32.const 2) (i32.const 3) (i32.const 4) (i32.const 5) (i32.const 6)))
    (global.set $B (array.new_fixed $a 9 (i32.const 10) (i32.const 11) (i32.const 12) (i32.const 13) (i32.const 14) (i32.const 15) (i32.const 16) (i32.const 17) (i32.const 18)))
    (global.set $I (array.new_fixed $b 7 (i32.const 0) (i32.const 1) (i32.const 2) (i32.const 3) (i32.const 4) (i32.const 5) (i32.const 6)))
    (global.set $R (array.new_default $r (i32.const 7))))
  (func (export "get") (param i32 i32) (result i32) (if (result i32) (local.get 0)
     (then (if (result i32) (i32.eq (local.get 0) (i32.const 1)) (then (array.get $a (global.get $A) (local.get 1))) (else (if (result i32) (i32.eq (local.get 0) (i32.const 2)) (then (array.get $a (global.get $B) (local.get 1))) (else (array.get_u $b (global.get $I) (local.get 1)))))))
     (else (i31.get_s (ref.cast (ref i31) (array.get $r (global.get $R) (local.get 1)))))))
  (func (export "len") (param i32) (result i32) (if (result i32) (local.get 0) (then (array.len (global.get $A))) (else (array.len (global.get $B)))))
  (func (export "cp_ab") (param i32 i32 i32) (array.copy $a $a (global.get $A) (local.get 0) (global.get $B) (local.get 1) (local.get 2)))
  (func (export "cp_ba") (param i32 i32 i32) (array.copy $a $a (global.get $B) (local.get 0) (global.get $A) (local.get 1) (local.get 2)))
  (func (export "cp_aa") (param i32 i32 i32) (array.copy $a $a (global.get $A) (local.get 0) (global.get $A) (local.get 1) (local.get 2)))
  (func (export "cp_ii") (param i32 i32 i32) (array.copy $b $b (global.get $I) (local.get 0) (global.get $I) (local.get 1) (local.get 2)))
  (func (export "fill") (param i32 i32 i32) (array.fill $a (global.get $A) (local.get 0) (local.get 1) (local.get 2)))
  (func (export "fill_i") (param i32 i32 i32) (array.fill $b (global.get $I) (local.get 0) (local.get 1) (local.get 2)))
  (func (export "idata") (param i32 i32 i32) (array.init_data $b $d (global.get $I) (local.get 0) (local.get 1) (local.get 2)))
  (func (export "idata32") (param i32 i32 i32) (array.init_data $a $d (global.get $A) (local.get 0) (local.get 1) (local.get 2)))
  (func (export "ielem") (param i32 i32 i32) (array.init_elem $r $e (global.get $R) (local.get 0) (local.get 1) (local.get 2)))
  (func (export "ndata") (param i32 i32) (result i32) (array.len (array.new_data $b $d (local.get 0) (local.get 1))))
  (func (export "nelem") (param i32 i32) (result i32) (array.len (array.new_elem $r $e (local.get 0) (local.get 1)))))"""


def contents(p: Any, which: int, n: int) -> list[int]:
    return [call(p, "get", which, i)[0] for i in range(n)]


@pytest.mark.parametrize("target", ARRAYS)
def test_array_copy_and_fill_with_overlap(target: str) -> None:
    p = make(target, BULK)
    for fn, which, n in (("cp_aa", 1, 7), ("cp_ii", 3, 7)):
        for di, si, k in (
            (2, 1, 5),
            (1, 2, 5),
            (0, 0, 7),
            (3, 3, 0),
            (6, 6, 1),
            (0, 4, 3),
            (4, 0, 3),
        ):
            q = make(target, BULK)
            want = list(range(7))
            want[di : di + k] = list(range(7))[si : si + k]
            call(q, fn, di, si, k)
            assert contents(q, which, n) == want, (fn, di, si, k)
    call(p, "fill", 2, 99, 4)
    assert contents(p, 1, 7) == [0, 1, 99, 99, 99, 99, 6]
    call(p, "fill_i", 1, 0x1FF, 3)  # an i8 element keeps the low 8 bits
    assert contents(p, 3, 7) == [0, 255, 255, 255, 4, 5, 6]
    call(p, "fill", 7, 5, 0)  # the end of the array, nothing to do
    assert trap(p, "fill", 4, 5, 4) is TrapCode.ARRAY_OUT_OF_BOUNDS
    assert trap(p, "fill_i", 5, 5, 3) is TrapCode.ARRAY_OUT_OF_BOUNDS and contents(p, 1, 7)[4] == 99


@pytest.mark.parametrize("target", ARRAYS)
def test_array_operators_check_the_bound_of_the_right_operand(target: str) -> None:
    p = make(target, BULK)  # A has 7 elements, B 9, the data segment 11 bytes, the elem segment 5
    assert call(p, "len", 1) == [7] and call(p, "len", 0) == [9]
    out = TrapCode.ARRAY_OUT_OF_BOUNDS
    assert trap(p, "cp_ab", 0, 0, 8) is out  # the destination (7) is too short
    assert trap(p, "cp_ba", 0, 0, 8) is out  # the source (7) is too short
    call(p, "cp_ab", 0, 2, 7)  # source 2 + 7 = 9 fits B
    assert contents(p, 1, 7) == [12, 13, 14, 15, 16, 17, 18]
    assert trap(p, "cp_ab", 0, 3, 7) is out  # 3 + 7 = 10 > 9: the source
    call(p, "cp_ba", 2, 0, 7)  # destination 2 + 7 = 9 fits B
    assert contents(p, 2, 9) == [10, 11, 12, 13, 14, 15, 16, 17, 18]
    call(p, "cp_ba", 0, 0, 7)
    assert contents(p, 2, 9) == [12, 13, 14, 15, 16, 17, 18, 17, 18]
    assert trap(p, "cp_ba", 3, 0, 7) is out  # 10 > 9: the destination
    # init_data: the array (7), then the segment (11)
    call(p, "idata", 1, 5, 6)
    assert contents(p, 3, 7) == [0, 6, 7, 8, 9, 10, 11]
    assert trap(p, "idata", 2, 5, 6) is out
    assert trap(p, "idata", 1, 6, 6) is TrapCode.MEMORY_OUT_OF_BOUNDS
    call(p, "idata32", 0, 7, 1)  # a 32-bit element reads four bytes, little-endian: 7 + 4 = 11
    assert contents(p, 1, 1) == [0x0B0A0908]
    assert trap(p, "idata32", 0, 8, 1) is TrapCode.MEMORY_OUT_OF_BOUNDS
    # init_elem: the array (7), then the segment (5)
    call(p, "ielem", 2, 0, 5)
    assert [call(p, "get", 0, i)[0] for i in (2, 3, 4, 6)] == [1, 2, 3, 5]
    assert trap(p, "ielem", 3, 0, 5) is out
    assert trap(p, "ielem", 0, 1, 5) is TrapCode.TABLE_OUT_OF_BOUNDS
    # array.new_data / array.new_elem: a segment bound comes before any other
    assert call(p, "ndata", 4, 7) == [7] and call(p, "nelem", 2, 3) == [3]
    assert trap(p, "ndata", 5, 7) is TrapCode.MEMORY_OUT_OF_BOUNDS
    assert trap(p, "nelem", 3, 3) is TrapCode.TABLE_OUT_OF_BOUNDS
    assert trap(p, "nelem", 0, U32) is TrapCode.TABLE_OUT_OF_BOUNDS  # not ALLOCATION_TOO_LARGE
    assert trap(p, "ndata", 0, U32) is TrapCode.MEMORY_OUT_OF_BOUNDS


@pytest.mark.parametrize("target", ARRAYS)
def test_trap_order_is_operands_null_bounds_access(target: str) -> None:
    p = make(
        target,
        """(module (type $s (struct (field (mut i32)))) (type $a (array (mut i32)))
      (func (export "get") (param i32) (result i32) (array.get $a (ref.null $a) (local.get 0)))
      (func (export "set_div") (result i32) (struct.set $s 0 (ref.null $s) (i32.div_u (i32.const 1) (i32.const 0))) (i32.const 0))
      (func (export "get_div") (result i32) (array.get $a (ref.null $a) (i32.div_u (i32.const 1) (i32.const 0))))
      (func (export "len") (result i32) (array.len (ref.null $a)))
      (func (export "sget") (result i32) (struct.get $s 0 (ref.null $s)))
      (func (export "copy") (param i32 i32 i32) (local $x (ref $a)) (local.set $x (array.new_default $a (i32.const 4)))
        (array.copy $a $a (select (result (ref null $a)) (ref.null $a) (local.get $x) (local.get 0)) (i32.const 100)
                          (select (result (ref null $a)) (ref.null $a) (local.get $x) (local.get 1)) (i32.const 100) (i32.const 1)))
      (func (export "nd") (param i32) (result i32) (array.len (array.new_default $a (local.get 0)))))""",
    )
    null = TrapCode.NULL_REFERENCE
    assert trap(p, "get", 100) is null and trap(p, "len") is null and trap(p, "sget") is null
    assert trap(p, "set_div") is TrapCode.INTEGER_DIVISION_BY_ZERO
    assert trap(p, "get_div") is TrapCode.INTEGER_DIVISION_BY_ZERO
    # array.copy: destination null, destination bounds, source null, source bounds (Wasmtime)
    assert trap(p, "copy", 1, 0, 0) is null and trap(p, "copy", 1, 1, 0) is null
    assert trap(p, "copy", 0, 1, 0) is TrapCode.ARRAY_OUT_OF_BOUNDS
    assert trap(p, "copy", 0, 0, 0) is TrapCode.ARRAY_OUT_OF_BOUNDS


# ---- the allocation limits (Wasmtime 50.0.1, found by bisection: the header plus the elements fit
# 32 bits); the largest allowed length would allocate gigabytes, so only the first trap is tested
@pytest.mark.parametrize("target", ARRAYS)
@pytest.mark.parametrize(
    ("elem", "limit"),
    [("i8", 4294967251), ("i16", 2147483625), ("i32", 1073741812), ("f32", 1073741812),
     ("i64", 536870905), ("f64", 536870905), ("v128", 268435452), ("anyref", 1073741812),
     ("(ref null $a)", 1073741812)],
)  # fmt: skip
def test_allocation_too_large(target: str, elem: str, limit: int) -> None:
    p = make(
        target,
        f"""(module (type $a (array (mut {elem})))
      (func (export "new") (param i32) (result i32) (array.len (array.new_default $a (local.get 0))))
      (func (export "zero") (result i32) (array.len (array.new_default $a (i32.const 0)))))""",
    )
    assert trap(p, "new", limit + 1) is TrapCode.ALLOCATION_TOO_LARGE
    assert trap(p, "new", U32) is TrapCode.ALLOCATION_TOO_LARGE
    assert call(p, "zero") == [0]


# ---- the validator: reference operands in unreachable code (checked against Wasmtime 50.0.1)
HEAD = "(type $s (struct (field i32))) (type $f (func (param i32)))"
UNREACHABLE_REFS = [
    ("(unreachable) (ref.as_non_null) (i32.eqz) (drop)", False),
    ("(unreachable) (ref.as_non_null) (drop)", True),
    ("(result i32) (unreachable) (ref.as_non_null)", False),
    ("(result funcref) (unreachable) (ref.as_non_null)", True),
    ("(result i32) (unreachable) (ref.as_non_null) (struct.get $s 0)", True),
    ("(result i32) (unreachable) (ref.as_non_null) (ref.is_null)", True),
    ("(unreachable) (ref.as_non_null) (call_ref $f)", True),
    ("(result anyref) (unreachable) (ref.as_non_null) (any.convert_extern)", True),
    ("(result i32) (unreachable) (ref.as_non_null) (i31.get_s)", True),
    ("(result i32) (unreachable) (ref.as_non_null) (i32.const 1) (i32.const 0) (select)", False),
    ("(local i32) (unreachable) (ref.as_non_null) (local.set 0)", False),
    ("(result i32) (block (result i32) (unreachable) (ref.as_non_null) (br_if 0) (i32.const 1))", False),
    ("(result i32) (block (result i32) (unreachable) (br_on_null 0) (i32.eqz))", False),
    ("(result i32) (block (result i32) (unreachable) (br_on_null 0) (drop) (i32.const 0))", False),
    ("(result i32) (block (result i32) (i32.const 1) (unreachable) (br_on_null 0) (drop) (drop) (i32.const 0))", True),
    ("(result i32) (block (result i32) (i32.const 1) (unreachable) (br_on_null 0) (struct.get $s 0) (i32.add))", True),
    ("(result i32) (block (result i32) (i32.const 7) (unreachable) (br_on_non_null 0) (i32.eqz))", False),
    ("(result (ref any)) (block (result (ref any)) (unreachable) (br_on_non_null 0) (unreachable))", True),
]  # fmt: skip


@pytest.mark.parametrize(("body", "valid"), UNREACHABLE_REFS)
def test_unreachable_reference_operands_have_a_reference_type(body: str, valid: bool) -> None:
    mod = parse(wat2wasm(f"(module {HEAD} (func {body}))"))
    if valid:
        validate(mod)
    else:
        with pytest.raises(Invalid):
            validate(mod)


# ---- a module without GC translates as before
def test_a_module_without_gc_has_no_runtime_types() -> None:
    src = translate(
        wat2wasm("""(module (type $f (func (param i32))) (table 1 funcref)
      (func (export "f") (param i32) (call_indirect (type $f) (local.get 0) (i32.const 0))))""")
    )
    assert "RT" not in src and "_rtt" not in src
    src = translate(wat2wasm("(module (type $f (sub (func))) (func (type $f)))"))
    assert "RT0 = _rtt(" in src  # a non-final type may have a subtype in another module


# ---- instantiation order and deep allocating constant expressions
@pytest.mark.parametrize("target", ARRAYS)
def test_element_expressions_run_before_any_active_write(target: str) -> None:
    # Wasmtime: every segment's items first (an allocation trap), then the active writes
    a = make(
        target,
        '(module (table (export "t") 2 funcref) (func (export "isnull") (result i32) (ref.is_null (table.get 0 (i32.const 0)))))',
    )
    b = """(module (type $a (array i8)) (import "a" "t" (table 2 funcref)) (func $f)
      (elem (table 0) (i32.const 0) func $f)
      (elem anyref (item (array.new_default $a (i32.const -1)))))"""
    with pytest.raises(Trap) as e:
        make(target, b, {"a": a})
    assert e.value.code is TrapCode.ALLOCATION_TOO_LARGE
    assert call(a, "isnull") == [1]  # the imported table was not written
    with pytest.raises(
        Trap
    ) as e:  # the same with a table of its own and an active item that allocates
        make(
            target,
            """(module (type $a (array i8)) (table $t 1 anyref)
          (elem (table $t) (i32.const 0) anyref (item (array.new_default $a (i32.const 1))) (item (ref.null any)))
          (elem anyref (item (array.new_default $a (i32.const -1)))))""",
        )
    assert e.value.code is TrapCode.ALLOCATION_TOO_LARGE


@pytest.mark.parametrize("target", TARGETS)
def test_deeply_nested_constant_expressions(target: str) -> None:
    # Wasmtime accepts at least 20 000; the validator and the generated code must not recurse.
    # Pyodide: CPython frees a chain this deep recursively in C and overflows its small stack (LESSONS 103)
    n = 300 if sys.platform == "emscripten" else 10_000
    e = "(ref.null $l)"
    for _ in range(n):
        e = f"(struct.new $l {e})"
    p = make(
        target,
        f"""(module (type $l (struct (field (ref null $l)))) (global $g (ref null $l) {e})
      (func (export "d") (result i32) (local $x (ref null $l)) (local $n i32)
        (local.set $x (global.get $g))
        (block $b (loop $c (br_if $b (ref.is_null (local.get $x))) (local.set $n (i32.add (local.get $n) (i32.const 1)))
          (local.set $x (struct.get $l 0 (local.get $x))) (br $c)))
        (local.get $n)))""",
    )
    assert call(p, "d") == [n]
