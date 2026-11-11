# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportArgumentType=false
"""Wasm GC instructions in the front end (S0013, step 1b): decoding, validation, constant
expressions, reference masks. No target runs them yet."""

from typing import Any

import pytest

from tests.test_gc_types import HEAD
from tests.watcache import wat2wasm as wat
from waboros.frontend.binary import Malformed, decode, parse
from waboros.frontend.validate import Invalid, validate
from waboros.targets.jit.common.compiler import SUPPORTED, unsupported
from waboros.targets.pyc import PycTarget

TYPES = """
  (type $s (sub (struct (field i8) (field (mut i32)))))
  (type $a (array (mut i8)))
  (type $sub (sub $s (struct (field i8) (field (mut i32)) (field f64))))
  (type $refs (array (mut anyref)))
"""


def load(text: str) -> dict[str, Any]:
    m = parse(wat(f"(module {text})"))
    validate(m)
    return m


def invalid(body: str, match: str) -> None:
    with pytest.raises(Invalid, match=match):
        load(f"{TYPES} (func (param (ref null $s) (ref null $a) eqref anyref (ref func)) {body})")


def test_decoded_shapes() -> None:
    m = load(f"""{TYPES} (data $d "ab") (elem $e anyref (ref.null any))
      (func (param (ref null $s) (ref null $a) anyref)
        (drop (struct.new_default $s)) (drop (struct.get_u $s 0 (local.get 0)))
        (drop (array.new_fixed $a 3 (i32.const 1) (i32.const 2) (i32.const 3)))
        (drop (array.new_data $a $d (i32.const 0) (i32.const 1)))
        (drop (ref.test (ref null $sub) (local.get 2)))
        (drop (ref.cast (ref $s) (local.get 2)))
        (drop (any.convert_extern (extern.convert_any (local.get 2))))
        (drop (block (result anyref) (br_on_cast_fail 0 anyref (ref null i31) (local.get 2)))))""")
    ops = [i for i in m["insns"][0] if i[0] >> 8 == 0xFB]
    assert ops[:3] == [(0xFB01, 0), (0xFB04, 0, 0), (0xFB08, 1, 3)]
    assert (0xFB09, 1, 0) in ops  # array.new_data $a $d
    assert (0xFB15, 2) in ops and (0xFB16, 0) in ops  # ref.test (ref null $sub), ref.cast (ref $s)
    assert (0xFB19, 3, 0, -18, -20) in ops and (0xFB1B,) in ops  # flags: both nullable
    assert m["gc"] and m["validated"]


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ("(drop (struct.get_s $s 1 (local.get 0)))", "type mismatch"),  # not packed
        ("(drop (struct.get $s 0 (local.get 0)))", "type mismatch"),  # packed: only get_s/u
        ("(struct.set $s 0 (local.get 0) (i32.const 0))", "immutable field"),
        ("(drop (struct.get $s 5 (local.get 0)))", "unknown field"),
        ("(drop (struct.get $s 1 (local.get 1)))", "type mismatch"),  # an array reference
        ("(drop (array.new_data $refs 0 (i32.const 0) (i32.const 0)))", "numeric or vector"),
        (
            "(array.copy $a $refs (local.get 1) (i32.const 0) (local.get 1) (i32.const 0)"
            " (i32.const 0))",
            "array types do not match",
        ),
        ("(drop (ref.test (ref null $s) (local.get 4)))", "type mismatch"),  # func hierarchy
        ("(drop (ref.eq (local.get 3) (local.get 2)))", "type mismatch"),  # anyref is not eqref
        ("(unreachable) (any.convert_extern) (i32.eqz) (drop)", "type mismatch"),  # (ref any)
        ("(unreachable) (extern.convert_any) (i31.get_s) (drop)", "type mismatch"),
        (
            "(drop (block (result eqref) (br_on_cast 0 eqref anyref (local.get 2))))",
            "type mismatch",
        ),  # rt2 is not a subtype of rt1
        (
            "(drop (block (result (ref $s)) (br_on_cast 0 anyref (ref null $s) (local.get 3))"
            " (unreachable)))",
            "type mismatch",
        ),  # the label wants non-null
    ],
)
def test_invalid_instructions(body: str, match: str) -> None:
    invalid(body, match)


def test_cast_flags_and_data_count_are_checked_by_the_reader() -> None:
    with pytest.raises(Malformed, match="cast flags"):
        decode(b"\xfb\x18\x04\x00\x6e\x6e\x0b")
    body = HEAD + b"\x01\x07\x02\x5e\x78\x01\x60\x00\x00\x03\x02\x01\x01"
    code = b"\x41\x00\x41\x00\xfb\x09\x00\x00\x1a\x0b"  # array.new_data without a data count
    with pytest.raises(Malformed, match="data count"):
        parse(body + b"\x0a" + bytes([len(code) + 3, 1, len(code) + 1, 0]) + code)


def test_constant_expressions_allocate() -> None:
    m = load(f"""{TYPES} (global $e externref (ref.null extern))
      (global (ref $s) (struct.new $s (i32.const 1) (i32.const 2)))
      (global (ref $a) (array.new_fixed $a 2 (i32.const 1) (i32.const 2)))
      (global (ref $a) (array.new_default $a (i32.const 4)))
      (global (ref i31) (ref.i31 (i32.const 5)))
      (global anyref (any.convert_extern (global.get $e)))
      (global externref (extern.convert_any (ref.i31 (i32.const 5))))
      (table 1 (ref null $s) (struct.new_default $sub))
      (elem $x (ref any) (item (ref.i31 (i32.const 1))) (item (struct.new_default $s)))""")
    assert m["gc"] and m["validated"] and len(m["globals"]) == 7
    assert m["globals"][1][2][:2] == ("gc", (0xFB00, 0))


@pytest.mark.parametrize(
    "text",
    [
        "(global (ref $s) (struct.new $s (i32.const 1)))",  # too few operands
        "(global (ref $s) (struct.new $s (i32.const 1) (i64.const 2)))",
        "(global (ref $a) (array.new $a (i32.const 1) (i64.const 2)))",
        '(data $d "ab") (global (ref $a) (array.new_data $a $d (i32.const 0) (i32.const 1)))',
        "(global (ref $a) (struct.new_default $s))",  # a struct type, not an array
        "(global (ref null $s) (ref.cast (ref null $s) (ref.null none)))",  # not constant
    ],
)
def test_invalid_constant_expressions(text: str) -> None:
    with pytest.raises(Invalid):
        load(TYPES + text)


def test_refusal_by_the_targets() -> None:
    # a function-only type section, but a GC instruction and a GC constant expression
    for text in (
        "(func (result i32) (i31.get_s (ref.i31 (i32.const 1))))",
        "(global i32 (i32.const 0)) (global funcref (ref.null func))"
        "(func (param eqref) (result i32) (ref.eq (local.get 0) (local.get 0)))",
        # only the conversions, in a constant expression (review of step 1b)
        "(global externref (extern.convert_any (any.convert_extern (ref.null extern))))",
    ):
        m = load(text)
        assert m["gc"] and m["validated"]
        assert unsupported(m, SUPPORTED) == "GC types"
        assert PycTarget().supports(m) == "GC types"


def masks(text: str, f: int = 0) -> tuple[dict[int, int], list[tuple[Any, ...]]]:
    m = load(text)
    return m["refmasks"][f], m["insns"][f]


def at(insns: list[tuple[Any, ...]], op: int) -> list[int]:
    return [i for i, ins in enumerate(insns) if ins[0] == op]


def test_masks_of_a_call() -> None:
    mk, insns = masks("""(type $t (func (param externref i32 anyref) (result i32)))
      (func (param externref anyref) (result i32)
        (call $g (local.get 0) (i32.const 7) (local.get 1)))
      (func $g (type $t) (i32.const 0))""")
    assert mk == {at(insns, 0x10)[0]: 0b101}


def test_masks_across_blocks_and_every_safepoint() -> None:
    mk, insns = masks("""(type $t (func (param anyref) (result i32)))
      (table 1 funcref) (memory 1)
      (func (param externref anyref) (result i32)
        (local.get 0) (i32.const 1)
        (block (result i32)
          (local.get 1) (call $h) (drop)
          (local.get 1) (i32.const 0) (call_indirect (type $t)) (drop)
          (i32.const 1) (memory.grow) (drop)
          (ref.i31 (i32.const 3)) (drop)
          (i32.const 1))
        (i32.add) (drop) (drop) (i32.const 0))
      (func $h (type $t) (i32.const 0))""")
    assert mk[at(insns, 0x10)[0]] == 0b101  # [extern, i32, any]
    assert mk[at(insns, 0x11)[0]] == 0b101  # [extern, i32, any, i32]
    assert mk[at(insns, 0x40)[0]] == 0b001  # memory.grow: [extern, i32, i32]
    assert mk[at(insns, 0xFB1C)[0]] == 0b001  # ref.i31 is a safepoint: [extern, i32, i32]
    assert len(mk) == 4


def test_masks_skip_unreachable_code_and_tail_calls() -> None:
    mk, insns = masks(
        """(func $n (param anyref))
      (func (param anyref)
        (call $n (local.get 0))
        (unreachable)
        (call $n (local.get 0))
        (block (call $n (ref.null any))))
      (func (param anyref) (return_call $n (local.get 0)))""",
        1,
    )
    assert mk == {at(insns, 0x10)[0]: 0b1}
    m = load("(func $n (param anyref)) (func (param anyref) (return_call $n (local.get 0)))")
    assert m["refmasks"] == [{}, {}]  # a tail call is not a safepoint


def test_masks_only_with_an_any_or_extern_type() -> None:
    m = load("(func $n (param funcref) (call $n (local.get 0)))")
    assert m["refmasks"] == [] and not m["gc"]
    m = load("(func $n (param externref) (call $n (local.get 0)))")  # externref counts, not GC
    assert m["refmasks"] == [{1: 0b1}] and not m["gc"]
