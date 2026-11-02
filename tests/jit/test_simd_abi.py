# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
"""v128 on the machine's JIT target (S0005 step 5c1): two slots per value through the entry, host
functions, calls, blocks, select/drop, locals and globals; `v128.load`/`store` with their bounds.
Results are compared with `pyc` (which claims `simd`); linking cases check exact values. The
SIMD encodings are checked against llvm-mc."""

import re
import shutil
import subprocess
from collections.abc import Sequence
from typing import Any

import pytest

from tests.jit import native, need
from tests.jit.test_compiler import JIT, Inst, build, wat
from tests.jit.test_tables import provider
from w2py.runtime.store import StoreState
from w2py.targets.base import Raw, Trap, TrapCode
from w2py.targets.jit.common import simd
from w2py.targets.jit.common.context import Layout

M64, M128 = (1 << 64) - 1, (1 << 128) - 1
A, B = 0x0123456789ABCDEF_FEDCBA9876543210, 0xFFFFFFFF_00000000_80000000_7FFFFFFF
VALS = [0, 1, A, B, M128, 1 << 127, M64, M64 << 64]

MOD = """(module
  (type $vv (func (param v128 i32) (result i32 v128)))
  (import "env" "h" (func $h (param i32 v128 f64) (result v128 i32)))
  (memory $m0 1) (memory $m1 1)
  (table 2 funcref) (elem (i32.const 0) $swap $h)
  (global $g (export "g") (mut v128) (v128.const i64x2 7 8))
  (global $i (mut i32) (i32.const 3))
  (func $swap (type $vv) (local.get 1) (local.get 0))
  (func (export "id") (param v128 i32 v128 f64) (result f64 v128 i32 v128)
    (local.get 3) (local.get 2) (local.get 1) (local.get 0))
  (func (export "const") (result v128 v128)
    (v128.const i64x2 0x0123456789abcdef 0xfedcba9876543210) (v128.const i32x4 -1 0 -1 0))
  (func (export "ld") (param i32) (result v128) (v128.load (local.get 0)))
  (func (export "ld/k") (result v128) (v128.load offset=65520 (i32.const 0)))
  (func (export "ld/o") (param i32) (result v128) (v128.load offset=4 align=1 (local.get 0)))
  (func (export "ld/c") (param i32) (result v128) (v128.load (i32.add (local.get 0) (i32.const 1))))
  (func (export "st") (param i32 v128) (v128.store (local.get 0) (local.get 1)))
  (func (export "st/c") (param i32)
    (v128.store offset=1 (i32.sub (local.get 0) (i32.const 1)) (v128.const i64x2 -1 5)))
  (func (export "m1") (param i32 v128) (result v128)
    (v128.store $m1 (local.get 0) (local.get 1)) (v128.load $m1 offset=8 (local.get 0)))
  (func (export "sel") (param v128 v128 i32) (result v128)
    (select (local.get 0) (local.get 1) (local.get 2)))
  (func (export "sel/cmp") (param v128 v128 i32) (result v128)
    (select (local.get 0) (local.get 1) (i32.lt_s (local.get 2) (i32.const 5))))
  (func (export "sel/k") (param v128 i32) (result v128)
    (select (v128.const i64x2 1 2) (local.get 0) (local.get 1)))
  (func (export "sel/e") (param v128 i32) (result v128)
    (select (result v128) (local.get 0) (v128.const i64x2 3 4) (local.get 1)))
  (func (export "drop") (param v128 i32) (result i32)
    (drop (local.get 0)) (drop (v128.const i64x2 0 0)) (local.get 1))
  (func (export "tee") (param v128) (result v128 v128) (local v128 i64)
    (local.set 2 (i64.const 9))
    (local.tee 1 (v128.const i64x2 5 6)) (local.set 1 (local.get 0)) (local.get 1))
  (func (export "zero") (param i32) (result v128 i32) (local v128 i32) (local.get 1) (local.get 2))
  (func (export "block") (param v128 i32) (result v128)
    (local.get 0)
    (block $b (param v128) (result v128)
      (loop $l (param v128) (result v128)
        (br_if $b (i32.eqz (local.get 1)))
        (local.set 1 (i32.sub (local.get 1) (i32.const 1)))
        (drop) (v128.load (local.get 1)) (br $l))))
  (func (export "if") (param v128 i32) (result v128)
    (local.get 0)
    (if (param v128) (result v128) (local.get 1) (then) (else (drop) (v128.const i64x2 1 1))))
  (func (export "gget") (result v128) (global.get $g))
  (func (export "gset") (param v128) (global.set $g (local.get 0)))
  (func (export "call") (param v128 i32) (result i32 v128) (call $swap (local.get 0) (local.get 1)))
  (func (export "calli") (param v128 i32) (result i32 v128)
    (call_indirect (type $vv) (local.get 0) (local.get 1) (i32.const 0)))
  (func (export "tail") (param v128 i32) (result i32 v128)
    (return_call $swap (local.get 0) (local.get 1)))
  (func (export "host") (param i32 v128) (result v128 i32)
    (call $h (local.get 0) (local.get 1) (f64.const 2.5)))
  (func $rec (export "rec") (param i32 v128) (result v128)
    (if (result v128) (i32.eqz (local.get 0)) (then (local.get 1))
      (else (call $rec (i32.sub (local.get 0) (i32.const 1))
        (v128.load (i32.and (local.get 0) (i32.const 0xff))))))))"""

ARGS: dict[str, list[tuple[int, ...]]] = {
    "id": [(a, 7, b, 0x400921FB54442D18) for a in VALS[:4] for b in VALS[3:]],
    "const": [()], "ld/k": [()], "gget": [()],
    "ld": [(0,), (16,), (65520,), (65521,), (0xFFFFFFFF,)],
    "ld/o": [(0,), (65516,), (65517,)],
    "ld/c": [(0,), (65519,), (65520,)],
    "st": [(a, v) for a in (0, 3, 65520, 65521) for v in VALS[2:4]],
    "st/c": [(1,), (65520,), (65521,)],
    "m1": [(0, A), (65520, B), (65513, A)],
    "sel": [(a, b, c) for a in VALS[2:4] for b in VALS[4:6] for c in (0, 1, 1 << 31)],
    "sel/cmp": [(A, B, c) for c in (4, 5, -1)],
    "sel/k": [(A, 0), (A, 2)], "sel/e": [(B, 0), (B, 9)],
    "drop": [(A, 5)], "tee": [(A,), (B,)], "zero": [(3,)],
    "block": [(A, 0), (A, 3)], "if": [(B, 0), (B, 1)],
    "call": [(A, 9)], "calli": [(B, 1)], "tail": [(A, 2)],
    "host": [(4, A), (5, B)],
    "rec": [(3, A), (40, B)],
}  # fmt: skip


@pytest.fixture(autouse=True)
def _jit() -> None:
    native(JIT)


def _host(args: Sequence[Raw]) -> Sequence[Raw]:
    x, v, f = args
    return [(v << 8 | v >> 120) & M128 ^ x ^ f, x * 3]


def run(i: Inst, name: str, *args: int) -> list[Raw] | TrapCode:
    idx = next(x for n, k, x in i.mod["exports"] if n == name and k == 0)
    try:
        return list(i.inst.call(idx, list(args)))
    except Trap as e:
        return e.code


def _pair(pins: str, monkeypatch: pytest.MonkeyPatch) -> tuple[Inst, Inst]:
    if pins == "nopin":
        monkeypatch.setenv("W2PY_JIT_NOPIN", "all")
    w = wat(MOD)
    imports: dict[str, Any] = {"env": {"h": _host}}
    out = []
    for target in (JIT, "pyc"):
        i = build(w, target, imports)
        for a in range(0, 256, 8):  # a pattern in memory 0
            run(i, "st", a, int.from_bytes(bytes(j & 255 for j in range(a, a + 16)), "little"))
        out.append(i)
    return out[0], out[1]


@pytest.mark.parametrize("pins", ["pinned", "nopin"])
def test_v128_matches_pyc(pins: str, monkeypatch: pytest.MonkeyPatch) -> None:
    jit, pyc = _pair(pins, monkeypatch)
    for name, cases in ARGS.items():
        for args in cases:
            got, want = run(jit, name, *args), run(pyc, name, *args)
            assert got == want, (name, args, got, want)
            if name.startswith("st"):  # what was written, read back on both
                for a in (0, 16, 65520):
                    assert run(jit, "ld", a) == run(pyc, "ld", a), (name, args, a)
            if name == "gget":
                assert jit.inst.global_get(0) == pyc.inst.global_get(0)
    assert run(jit, "ld", 65521) is TrapCode.MEMORY_OUT_OF_BOUNDS


def test_deep_recursion_with_v128_is_stack_overflow() -> None:
    jit = build(wat(MOD), JIT, {"env": {"h": _host}})
    assert run(jit, "rec", 1 << 30, A) is TrapCode.STACK_OVERFLOW
    assert run(jit, "rec", 0, A) == [A]  # still usable


def test_v128_globals_link_across_instances() -> None:
    store = StoreState()
    a = build(wat("""(module
      (global (export "m") (mut v128) (v128.const i64x2 1 2))
      (global (export "k") v128 (v128.const i64x2 -1 3))
      (global i32 (i32.const 5))
      (global (export "n") (mut v128) (v128.const i64x2 4 5))
      (func (export "inc")
        (global.set 0 (v128.const i64x2 7 9))))"""), JIT, store=store)  # fmt: skip
    b = build(wat("""(module
      (import "a" "m" (global $m (mut v128)))
      (import "a" "k" (global $k v128))
      (global $c v128 (global.get $k))
      (global $i (mut i64) (i64.const 11))
      (func (export "get") (result v128 v128 v128 i64)
        (global.get $m) (global.get $k) (global.get $c) (global.get $i))
      (func (export "set") (param v128) (global.set $m (local.get 0))))"""),
        JIT, {"a": provider(a)}, store)  # fmt: skip
    k = M64 | 3 << 64
    assert run(b, "get") == [1 | 2 << 64, k, k, 11]
    run(a, "inc")
    assert run(b, "get") == [7 | 9 << 64, k, k, 11]
    assert run(b, "set", B) == []
    assert a.inst.global_get(0) == B and b.inst.global_get(0) == B
    assert b.inst.global_get(2) == k and a.inst.global_get(3) == 4 | 5 << 64


def test_split_join_and_layout_widths() -> None:
    ts = [0x7F, simd.V128, 0x7C, simd.V128]
    vals = [5, A, 9, B]
    assert simd.width(ts) == 6 and simd.width([0x7F, 0x7E]) == 2
    assert simd.split(ts, vals) == [5, A & M64, A >> 64, 9, B & M64, B >> 64]
    assert simd.join(ts, simd.split(ts, vals)) == vals
    lay = Layout(globals=4, imports=1, wide=(1, 2))
    plain = Layout(globals=4, imports=1)
    assert [lay.global_(i) - plain.global_(0) for i in range(4)] == [0, 1, 3, 5]
    assert lay.import_(0) == plain.import_(0) + 2 and repr(lay) == repr(plain)


# ------------------------------------------------------------------------------- encodings
LLVM_MC = shutil.which("llvm-mc")


def _mc(triple: str, lines: list[str]) -> list[bytes]:
    assert LLVM_MC
    p = subprocess.run(
        [LLVM_MC, f"--triple={triple}", "--show-encoding"],
        input="\n".join(lines) + "\n", capture_output=True, text=True,
    )  # fmt: skip
    assert p.returncode == 0 and not p.stderr, p.stderr[:2000]
    return [
        bytes(int(x, 16) for x in e.split(","))
        for e in re.findall(r"encoding: \[([^\]]*)\]", p.stdout)
    ]


def test_x86_forms_match_llvm_mc() -> None:
    need(LLVM_MC, "llvm-mc")
    from w2py.targets.jit.x86_64 import simd as x
    from w2py.targets.jit.x86_64.encoder import R12, R13, R15, RCX, Asm, Mem

    forms = [(Mem(R12, 8), "r12 + 8"), (Mem(R15, 8 * 300), "r15 + 2400"), (Mem(RCX), "rcx"),
             (Mem(R13, 16, RCX), "r13 + rcx*1 + 16"),
             (Mem(RCX, -0x80000000), "rcx - 2147483648")]  # fmt: skip
    ours, text = [], []
    for reg in (0, 5):
        for m, t in forms:
            for store in (False, True):
                a = Asm()
                x.movups(a, reg, m, store)
                ours.append(a.finish())
                ops = (f"xmmword ptr [{t}]", f"xmm{reg}")
                text.append("movups " + ", ".join(ops[::-1] if not store else ops))
    assert ours == _mc("x86_64", [".intel_syntax noprefix", *text]), text


def test_aarch64_forms_match_llvm_mc() -> None:
    need(LLVM_MC, "llvm-mc")
    from w2py.targets.jit.aarch64 import simd as v
    from w2py.targets.jit.aarch64.encoder import Asm

    ours: list[bytes] = []
    text: list[str] = []
    for load in (False, True):
        op = "ldr" if load else "str"
        for t, base, off, index, want in (
            (0, 27, 0, None, f"{op} q0, [x27]"),
            (0, 28, 16 * 4095, None, f"{op} q0, [x28, #65520]"),
            (7, 27, 8, None, f"{op[:2]}u{op[2:]} q7, [x27, #8]"),
            (0, 9, -256, None, f"{op[:2]}u{op[2:]} q0, [x9, #-256]"),
            (31, 26, 0, 9, f"{op} q31, [x26, x9]"),
        ):  # fmt: skip
            a = Asm()
            v.ldst_q(a, load, t, base, off, index)
            ours.append(a.finish())
            text.append(want)
        a = Asm()  # an offset no immediate takes: through x17
        v.ldst_q(a, load, 0, 27, 8 * 5001)
        ours.append(a.finish())
        text.append(f"mov x17, #{8 * 5001}\n{op} q0, [x27, x17]")
    theirs = _mc("aarch64", text)
    assert b"".join(ours) == b"".join(theirs), text
