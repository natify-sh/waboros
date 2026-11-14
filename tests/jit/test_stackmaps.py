# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
"""Stack maps of the JIT core (S0013 step 3a.1): the exact map of each call site, the site
records of both backends, the STACKMAPS encoding. Compile only, so both targets run on any host
(D35); the walk that checks the native numbers on running code is step 3a.3."""

from typing import Any

import pytest

from tests.watcache import wat2wasm
from waboros.frontend.binary import parse
from waboros.frontend.validate import validate
from waboros.targets import base
from waboros.targets.jit.common import gc
from waboros.targets.jit.common.compiler import Compiled

TARGETS = ["jit-x86_64", "jit-aarch64"]


def sites(target: str, text: str) -> tuple[Any, list[tuple[int, int, int, int, int]]]:
    """The module and its sites in code order: (shift, ret_at, up, nslots, bits)."""
    mod = parse(wat2wasm(text))
    validate(mod)
    c = base.TARGETS[target]().compile(mod)
    assert isinstance(c, Compiled) and c.stackmaps is not None
    return mod, [s for _, s in sorted(gc.decode(c.stackmaps).items())]


@pytest.mark.parametrize("target", TARGETS)
def test_locals_and_an_operand_below_a_call(target: str) -> None:
    # slots: $a 0, $n 1, $b 2, $v 3-4 (L = 5); the operand $a below the call is slot 5; the
    # argument $b is the callee's parameter, not ours
    _, s = sites(
        target,
        """(module
      (func $g (param externref))
      (func (param $a externref) (param $n i32) (local $b externref) (local $v v128)
        local.get $a local.get $n local.get $b call $g drop drop))""",
    )
    assert [(x[0], x[3], x[4]) for x in s] == [(7, 7, 0b100101)]


@pytest.mark.parametrize("target", TARGETS)
def test_a_v128_between_two_references_takes_two_slots(target: str) -> None:
    mod, s = sites(
        target,
        """(module
      (func $h (param i32))
      (func (param $a externref)
        local.get $a v128.const i64x2 0 0 local.get $a i32.const 1 call $h drop drop drop))""",
    )
    pc = 4  # the call: value positions ref, v128, ref, i32
    assert mod["refmasks"][1][pc] == 0b0101 and mod["v128masks"][1][pc] == 0b0010
    # slots: $a 0 (L = 1); operands ref 1, v128 2-3, ref 4; the i32 argument is the callee's
    assert [(x[0], x[3], x[4]) for x in s] == [(5, 5, 0b10011)]


@pytest.mark.parametrize("target", TARGETS)
def test_a_service_keeps_its_reference_argument(target: str) -> None:
    _, s = sites(
        target,
        """(module (table $t 0 externref)
      (func (param $a externref) (result i32) local.get $a i32.const 1 table.grow $t))""",
    )
    # shift 0; slots: $a, then the init value, the delta and the table immediate
    assert [(x[0], x[3], x[4]) for x in s] == [(0, 4, 0b11)]


@pytest.mark.parametrize("target", TARGETS)
def test_funcref_is_never_marked(target: str) -> None:
    _, s = sites(
        target,
        """(module
      (func $g) (func (param $f funcref) (param $a externref) call $g))""",
    )
    assert [(x[0], x[3], x[4]) for x in s] == [(2, 2, 0b10)]


@pytest.mark.parametrize("target", TARGETS)
def test_throw_refill_is_a_site_over_its_payload(target: str) -> None:
    _, s = sites(
        target,
        """(module (tag $e (param externref))
      (func (param $a externref) local.get $a throw $e))""",
    )
    assert [(x[0], x[3], x[4]) for x in s] == [(0, 3, 0b11)]


@pytest.mark.parametrize("target", TARGETS)
def test_record_call_and_host_thunk(target: str) -> None:
    _, s = sites(
        target,
        """(module (import "m" "h" (func $h (param externref)))
      (func (param $a externref) local.get $a call $h))""",
    )
    (shift, ret_at, up, n, bits), thunk = s
    assert (shift, n, bits) == (1, 1, 0b1)
    assert ret_at == 24 and up == 32  # no pins: x86 pad + r15 and pad; aarch64 record + x28
    assert thunk == (0, 8, 16, 0, 0)


# (ret_at, up) of a direct call, a record call and the host thunk, by the number of pinned i32
# locals: x86-64 pushes the pins plus an alignment pad, aarch64 grows its frame record. Checked
# against a disassembly of every call site of the spec suite (3a.1 review), so a wrong formula in
# a call method fails here before the walk of step 3a.3 exists.
_PINNED = {
    ("jit-x86_64", 1): [(24, 32), (40, 48), (8, 16)],
    ("jit-x86_64", 3): [(40, 48), (56, 64), (8, 16)],
    ("jit-aarch64", 1): [(8, 32), (24, 48), (8, 16)],
    ("jit-aarch64", 3): [(8, 48), (24, 64), (8, 16)],
}


@pytest.mark.parametrize(("target", "k"), sorted(_PINNED))
def test_native_numbers_of_a_frame_with_pins(target: str, k: int) -> None:
    locs = " ".join(f"(local $p{j} i32)" for j in range(k))
    body = " ".join(f"local.get $p{j} i32.const 1 i32.add local.set $p{j}" for j in range(k))
    _, s = sites(
        target,
        f"""(module (import "m" "h" (func $h (param externref))) (func $g)
      (func (param $r externref) (local $i i32) {locs}
        (loop $l call $g local.get $r call $h {body}
          local.get $i i32.const 1 i32.add local.tee $i i32.const 9 i32.lt_u br_if $l)))""",
    )
    assert [(x[1], x[2]) for x in s] == _PINNED[target, k]


@pytest.mark.parametrize("target", TARGETS)
def test_reference_locals_are_never_pinned(target: str) -> None:
    # I1: heavily used externref locals in a loop stay in slots and are marked at every site
    _, s = sites(
        target,
        """(module (func $g)
      (func (param $a externref) (local $b externref) (local $i i32)
        (loop $l local.get $a local.set $b local.get $b local.set $a call $g
          local.get $i i32.const 1 i32.add local.tee $i i32.const 9 i32.lt_u br_if $l)))""",
    )
    assert [(x[3], x[4] & 0b11) for x in s] == [(3, 0b11)]


@pytest.mark.parametrize("target", TARGETS)
def test_a_module_without_references_has_empty_maps(target: str) -> None:
    mod, s = sites(
        target,
        """(module (memory 1) (func $g (param i32))
      (func (param i32) local.get 0 call $g i32.const 1 memory.grow drop))""",
    )
    assert mod["refmasks"] == [] and len(s) == 2 and all(x[4] == 0 for x in s)


def test_encoding_round_trip_and_pool_sharing() -> None:
    big = (1 << 39) | 1  # 40 slots: two words
    raw = [(4, 2, 8, 16, 3, 0b101), (9, 0, 24, 32, 0, 0), (20, 1, 8, 16, 3, 0b101),
           (30, 5, 40, 48, 40, big), (31, 5, 40, 48, 7, 0)]  # fmt: skip
    data = gc.encode(raw)
    assert len(data) == 4 + 16 * len(raw) + 4 + (4 + 4) + (4 + 8)  # two maps in the pool
    assert gc.decode(data) == {
        4: (2, 8, 16, 3, 0b101), 9: (0, 24, 32, 0, 0), 20: (1, 8, 16, 3, 0b101),
        30: (5, 40, 48, 40, big), 31: (5, 40, 48, 0, 0),
    }  # fmt: skip


def test_slot_bits() -> None:
    assert gc.slot_bits(0b101, 0) == 0b101
    assert gc.slot_bits(0b101, 0b010) == 0b1001
    assert gc.slot_bits(0b1001, 0b0110) == 0b100001


# ---- GC reference slots (S0013 step 3b.2; jit-x86_64 behind WABOROS_JIT_GC=1 until it claims gc)
@pytest.fixture
def gc_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WABOROS_JIT_GC", "1")


@pytest.mark.usefixtures("gc_on")
def test_struct_anyref_and_i31_locals_are_marked() -> None:
    # slots: $a 0, $n 1, $s 2, $i 3 (L = 4); the operand $s below the call is slot 4
    _, s = sites(
        "jit-x86_64",
        """(module (type $t (struct (field i32))) (func $g (param i32))
      (func (param $a anyref) (param $n i32) (local $s (ref null $t)) (local $i i31ref)
        local.get $s local.get $n call $g drop))""",
    )
    assert [(x[0], x[3], x[4]) for x in s] == [(5, 5, 0b11101)]


@pytest.mark.usefixtures("gc_on")
def test_a_struct_field_operand_is_marked_at_the_allocation_service() -> None:
    # $a 0 (L = 1); struct.new's operands: the anyref field 1, the i32 field 2 (h = 2), then three
    # scratch slots, the last the ALLOC service's argument: shift 0, 1 + 2 + 3 slots
    _, s = sites(
        "jit-x86_64",
        """(module (type $p (struct (field anyref) (field i32)))
      (func (param $a anyref) (result anyref) (struct.new $p (local.get $a) (i32.const 1))))""",
    )
    assert [(x[0], x[3], x[4]) for x in s] == [(0, 6, 0b11)]


@pytest.mark.usefixtures("gc_on")
def test_a_non_final_call_indirect_has_two_sites() -> None:
    # $a 0 (L = 1); the operand $a below: slot 1; the argument slot 2, the index 3: FUNC_SUBTYPE
    # (shift 0, its two slots from 3) then the call through the record (shift 2, the callee's frame)
    _, s = sites(
        "jit-x86_64",
        """(module (type $f (sub (func (param i32)))) (table 1 funcref)
      (func (param $a anyref)
        local.get $a i32.const 5 i32.const 0 call_indirect (type $f) drop))""",
    )
    assert [(x[0], x[3], x[4]) for x in s] == [(0, 5, 0b11), (2, 2, 0b11)]


@pytest.mark.usefixtures("gc_on")
def test_a_function_cast_at_the_top_of_the_frame_keeps_its_slots_inside() -> None:
    # $r 0, $x 1 (L = 2); ref.test of $r at height 1: FUNC_SUBTYPE's two slots 3-4 are in the frame
    # (the core asserts every call's slots are below L + maxh; the review found slot 4 past it)
    _, s = sites(
        "jit-x86_64",
        """(module (type $F (sub (func)))
      (func (param $r funcref) (param $x anyref) (result i32)
        (ref.test (ref $F) (local.get $r))))""",
    )
    assert [(x[0], x[3], x[4]) for x in s] == [(0, 5, 0b10)]
