# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false
"""`pyc`: the first trapping instruction wins (S0012 fuzz triage), and generated names never meet
runtime names. Each module is the reduction of a differential-fuzz divergence from Wasmtime."""

import ast
import os
import re
import subprocess
import sys
from typing import Any

import pytest

from tests.watcache import wat2wasm
from waboros.targets.base import Trap, TrapCode
from waboros.targets.pyc import load, runtime, simd, translate

DIV0 = "local.get 0 i32.const 0 i32.div_u "  # a pending trap, below what follows
OOB = "i32.const 0xfffff0 i32.load "  # a load from outside the memory
BODIES = {  # the operands after DIV0; DIV0 comes first in the program, so it must win
    "drop": OOB + "drop drop",
    "select": OOB + "i32.const 1 i32.const 1 select drop drop",
    "local.set": OOB + "local.set 1 drop",
    "local.tee": OOB + "local.tee 1 drop drop",
    "binary": OOB + OOB + "i32.add drop drop",
    "flush_local": "local.get 1 i32.const 0xfffff0 i32.add i32.load i32.const 3 local.set 1"
    " drop drop",
    "call_indirect_arg": OOB + "i32.const 5 call_indirect (type 0) drop drop",
    "store8": "i32.const 0 " + OOB + "i32.store8 drop",
}


def run(body: str) -> Any:
    wat = f"""(module (memory 1) (table 1 funcref) (type (func (param i32) (result i32)))
  (func (export "f") (param i32) (result i32) (local i32) {DIV0} {body} i32.const 0))"""
    return load(wat2wasm(wat), {})["f"](5)


@pytest.mark.parametrize("name", BODIES)
def test_earlier_trap_wins(name: str) -> None:
    with pytest.raises(Trap) as e:
        run(BODIES[name])
    assert e.value.code == TrapCode.INTEGER_DIVISION_BY_ZERO


def test_call_indirect_args_before_table_check() -> None:
    # the argument (a bad load) is evaluated before the index is looked up in the table
    wat = """(module (memory 1) (table 1 funcref) (type (func (param i32) (result i32)))
  (func (export "f") (result i32)
    i32.const 0xfffff0 i32.load i32.const 5 call_indirect (type 0)))"""
    with pytest.raises(Trap) as e:
        load(wat2wasm(wat), {})["f"]()
    assert e.value.code == TrapCode.MEMORY_OUT_OF_BOUNDS


def test_call_indirect_index_trap_is_not_a_table_trap() -> None:
    wat = """(module (memory 1) (table 1 funcref) (type (func (result i32)))
  (func (export "f") (result i32) i32.const 0xfffff0 i32.load8_u call_indirect (type 0)))"""
    with pytest.raises(Trap) as e:
        load(wat2wasm(wat), {})["f"]()
    assert e.value.code == TrapCode.MEMORY_OUT_OF_BOUNDS


def test_store8_address_before_value() -> None:
    wat = """(module (memory 1)
  (func (export "f") (param f32) (result i32)
    i32.const 0xfffff0 i32.load8_s local.get 0 i32.trunc_f32_s i32.store8 i32.const 0))"""
    with pytest.raises(Trap) as e:
        load(wat2wasm(wat), {})["f"](float("nan"))
    assert e.value.code == TrapCode.MEMORY_OUT_OF_BOUNDS


def test_ninth_element_segment_does_not_shadow_a_helper() -> None:
    # segment 8 was `S8`, the runtime's signed-byte reader
    segs = " ".join("(elem func)" for _ in range(9))
    wat = f"""(module (memory 1) (table 1 funcref) {segs}
  (func (export "f") (result i32) (elem.drop 8) i32.const 0 i32.load8_s))"""
    assert load(wat2wasm(wat), {})["f"]() == 0


# ---- the two namespaces: generated names match GEN, no runtime name does
GEN = re.compile(
    r"(?:C|D|ES|G|I|M|RT|T|TG|TX|f|l|p|t|_MAXPAGES)\d*|f\d+_b|instantiate|imports|_e|_br|_w|_W|q\d+_\d+|r\d+_\d+|r"
)
COREMARK = os.path.join(os.path.dirname(__file__), "..", "poc", "in", "core.wasm")


def defined(src: str) -> set[str]:
    d: set[str] = set()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store | ast.Del):
            d.add(n.id)
        elif isinstance(n, ast.FunctionDef):
            d.add(n.name)
            d |= {a.arg for a in n.args.args}
        elif isinstance(n, ast.Global):
            d |= set(n.names)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            d.add(n.name)
    return d


def test_no_runtime_name_is_a_generated_name() -> None:
    clash = [n for n in runtime.__all__ + simd.__all__ if GEN.fullmatch(n)]
    assert not clash


def violations() -> list[str]:
    """Generated names that break the pattern or are runtime names, for the current FBITS mode."""
    n = 40  # more segments, globals, ... than any helper name has digits
    parts = [
        '(import "e" "g" (global $ig (mut i32)))',
        '(import "e" "t" (tag $it (param i32)))',
        '(import "e" "m" (memory 1))',
        "(table $t0 40 funcref) (table $t1 40 funcref) (memory $m1 1) (tag $tg (param i32))",
    ]
    parts += [f"(global $g{i} (mut i32) (i32.const {i}))" for i in range(n)]
    parts += [f'(export "g{i}" (global $g{i}))' for i in range(0, n, 2)]
    parts += [
        "(func $h)",
        '(export "S8" (func $h)) (export "_Mem" (func $h)) (export "M" (func $h))',
    ]
    parts += [f'(elem $e{i} func $h) (data $d{i} "ab")' for i in range(n)]
    body = " ".join(
        f"(table.init $t0 $e{i} (i32.const 0) (i32.const 0) (i32.const 1)) (elem.drop $e{i})"
        f" (memory.init $d{i} (i32.const 0) (i32.const 0) (i32.const 1)) (data.drop $d{i})"
        for i in range(n)
    )
    parts.append(
        f'(func (export "run") (param i32) (local i32 f64) {body} (i32.store $m1 (i32.const 0)'
        " (i32.const 1)) (try_table (catch_all 0) (throw $tg (i32.const 1))))"
    )
    parts.append(  # a loop with a parameter that is branched to (`q`), a wrapping i64 add (`_W`)
        '(func (export "lp") (param i64) (result i64) (local i64) local.get 0 (loop $l (param i64)'
        " (result i64) i64.const 5 i64.add local.tee 1 local.get 1 i64.const 100 i64.lt_u"
        " br_if $l))"
    )
    parts.append(  # an i64 sum that is returned wraps through `_W`
        '(func (export "w") (param i64) (result i64) local.get 0 local.get 0 i64.add)'
    )
    srcs = [translate(wat2wasm("(module " + " ".join(parts) + ")"))]
    assert "_W" in srcs[0] and "q" in srcs[0]
    srcs.append(  # the runtime types of S0013 (`RT`) and the segments of the array operators
        translate(
            wat2wasm(
                "(module (type $s (sub (struct (field i8)))) (type $a (array (mut i8)))"
                ' (data $d "ab") (func (export "f") (result i32) (array.len (array.new_data $a $d'
                " (i32.const 0) (i32.const 2)))))"
            )
        )
    )
    assert "RT0" in srcs[1]
    if os.path.exists(COREMARK):
        with open(COREMARK, "rb") as f:
            srcs.append(translate(f.read()))
    bad: list[str] = []
    rt = set(runtime.__all__) | set(simd.__all__)
    for src in srcs:
        d = defined(src)
        bad += sorted(d & rt) + [x for x in d if not GEN.fullmatch(x)]
    return bad


def test_generated_names_follow_the_pattern() -> None:
    assert not violations()


@pytest.mark.parametrize("fbits", ["0", "1"])
def test_generated_names_follow_the_pattern_in_both_float_modes(fbits: str) -> None:
    code = "from tests.test_pyc_trap_order import violations; print(violations())"
    env = dict(os.environ, WABOROS_PYC_FBITS=fbits)
    root = os.path.join(os.path.dirname(__file__), "..")
    r = subprocess.run(
        [sys.executable, "-c", code], env=env, cwd=root, capture_output=True, text=True
    )
    assert r.stdout.strip() == "[]", r.stderr[-500:] + r.stdout


def test_bool_and_or_do_not_skip_a_trapping_operand() -> None:
    for op in ("and", "or"):
        wat = f"""(module (memory 1)
  (func (export "f") (param i32) (result i32)
    local.get 0 i32.eqz i32.const 0xfffff0 i32.load i32.eqz i32.{op}))"""
        with pytest.raises(Trap) as e:
            load(wat2wasm(wat), {})["f"](5)
        assert e.value.code == TrapCode.MEMORY_OUT_OF_BOUNDS
    wat = """(module (func (export "f") (param i32) (result i32)
    local.get 0 i32.eqz local.get 0 i32.const 0 i32.div_u i32.eqz i32.and))"""
    with pytest.raises(Trap) as e:
        load(wat2wasm(wat), {})["f"](5)
    assert e.value.code == TrapCode.INTEGER_DIVISION_BY_ZERO


def test_table_grow_evaluates_the_initial_value_before_the_count() -> None:
    wat = """(module (memory 1) (table 1 funcref)
  (func (export "f") (result i32)
    ref.null func ref.as_non_null i32.const 0xfffff0 i32.load table.grow 0))"""
    with pytest.raises(Trap) as e:
        load(wat2wasm(wat), {})["f"]()
    assert e.value.code == TrapCode.NULL_REFERENCE
