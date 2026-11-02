# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
"""v128 on the machine's JIT target (S0005 step 5c1): two slots per value through the entry, host
functions, calls, blocks, select/drop, locals and globals; `v128.load`/`store` with their bounds.
Results are compared with `pyc` (which claims `simd`); linking cases check exact values. The
SIMD encodings are checked against llvm-mc."""

import itertools
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


def _pair(
    pins: str, monkeypatch: pytest.MonkeyPatch, text: str = MOD
) -> tuple[Inst, Inst]:  # fmt: skip
    if pins == "nopin":
        monkeypatch.setenv("W2PY_JIT_NOPIN", "all")
    w = wat(text)
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


# ------------------------------------------------------------- data movement (step 5c2)
SHAPES = {"i8x16": ("i32", 16), "i16x8": ("i32", 8), "i32x4": ("i32", 4), "i64x2": ("i64", 2),
          "f32x4": ("f32", 4), "f64x2": ("f64", 2)}  # fmt: skip
SCALARS = {"i32": [0, 1, 0x80, 0x1FF, 0x8000, 0xFFFF, 0x7FFFFFFF, 0xFFFFFFFF],
           "i64": [0, 0x80, 0x8000_0000, 1 << 63, M64, 0x0123456789ABCDEF],
           "f32": [0, 0x7FA00001, 0xFFC00000, 0x80000000, 0x3F800000],
           "f64": [0, 0x7FF4000000000001, 1 << 63, 0x3FF0000000000000]}  # fmt: skip
VK = "(v128.const i64x2 0x0123456789abcdef 0x8070605040302010)"
EDGES = [0, 1, 7, 65519, 65520, 65528, 65532, 65534, 65535, 65536, 0xFFFFFFFF]


def _computed(t: str, e: str) -> str:
    """`e` as a computed value (in `tos`), its bits unchanged."""
    if t[0] == "i":
        return f"({t}.xor {e} ({t}.const 0))"
    i = "i32" if t == "f32" else "i64"
    return f"({t}.reinterpret_{i} ({i}.xor ({i}.reinterpret_{t} {e}) ({i}.const 0)))"


def _moves() -> tuple[str, dict[str, list[tuple[int, ...]]]]:
    """Every data-movement operator with constant, local and computed operands, lanes at both
    ends, memory edges and memory 1."""
    fs = ['(func (export "ld") (param i32) (result v128) (v128.load (local.get 0)))',
          '(func (export "st") (param i32 v128) '
          "(v128.store (local.get 0) (local.get 1)))"]  # fmt: skip
    args: dict[str, list[tuple[int, ...]]] = {}

    def f(name: str, sig: str, body: str, cases: list[tuple[int, ...]]) -> None:
        fs.append(f'(func (export "{name}") {sig} {body})')
        args[name] = cases

    for sh, (t, n) in SHAPES.items():
        xs = [(x,) for x in SCALARS[t]]
        f(f"{sh}.splat", f"(param {t}) (result v128)", f"({sh}.splat (local.get 0))", xs)
        f(f"{sh}.splat/c", f"(param {t}) (result v128)",
          f"({sh}.splat {_computed(t, '(local.get 0)')})", xs)  # fmt: skip
        f(f"{sh}.splat/k", "(result v128)", f"({sh}.splat ({t}.const {SCALARS[t][-1]}))", [()])
        for lane in (0, n - 1):
            for sfx in ("_s", "_u") if n > 4 else ("",):
                op = f"{sh}.extract_lane{sfx} {lane}"
                f(f"{op}", f"(param v128) (result {t})", f"({op} (local.get 0))",
                  [(v,) for v in VALS])  # fmt: skip
                f(f"{op}/k", f"(result {t})", f"({op} {VK})", [()])
            op = f"{sh}.replace_lane {lane}"
            sig = f"(param v128 {t}) (result v128)"
            f(op, sig, f"({op} (local.get 0) (local.get 1))", [(A, x) for x in SCALARS[t]])
            f(f"{op}/c", sig, f"({op} (local.get 0) {_computed(t, '(local.get 1)')})",
              [(B, x) for x in SCALARS[t][-2:]])  # fmt: skip
            f(f"{op}/k", "(result v128)", f"({op} {VK} ({t}.const {SCALARS[t][1]}))", [()])
    for bits in (8, 16, 32, 64):
        size, n = bits // 8, 128 // bits
        ok = [(a,) for a in EDGES]
        for op in (f"v128.load{bits}_splat", f"v128.load{bits}_zero"):
            if op.endswith("zero") and bits < 32:
                continue
            f(op, "(param i32) (result v128)", f"({op} (local.get 0))", ok)
            f(f"{op}/o", "(param i32) (result v128)", f"({op} offset=3 (local.get 0))", ok)
            f(f"{op}/k", "(result v128)", f"({op} (i32.const {65536 - size}))", [()])
            f(f"{op}/m1", "(param i32) (result v128)", f"({op} $m1 (local.get 0))", ok)
        for lane in (0, n - 1):
            ld, st = f"v128.load{bits}_lane", f"v128.store{bits}_lane"
            sig = "(param i32 v128) (result v128)"
            f(f"{ld} {lane}", sig, f"({ld} {lane} (local.get 0) (local.get 1))",
              [(a, B) for a in EDGES])  # fmt: skip
            f(f"{ld} {lane}/c", sig, f"({ld} offset=2 {lane} "
              f"{_computed('i32', '(local.get 0)')} {VK})", [(a, 0) for a in EDGES])  # fmt: skip
            f(f"{ld} {lane}/m1", sig, f"({ld} $m1 {lane} (local.get 0) (local.get 1))",
              [(65536 - size, A), (65537 - size, A)])  # fmt: skip
            f(f"st{bits} {lane}", "(param i32 v128)", f"({st} {lane} (local.get 0) (local.get 1))",
              [(a, A) for a in EDGES])  # fmt: skip
            f(f"st{bits} {lane}/k", "(param i32)", f"({st} offset=1 {lane} (local.get 0) {VK})",
              [(a,) for a in (0, 65534 - size, 65535 - size)])  # fmt: skip
    pairs = [(x, y) for x in VALS[1:5] for y in VALS[3:7]]
    for op in ("and", "andnot", "or", "xor"):
        f(f"v128.{op}", "(param v128 v128) (result v128)",
          f"(v128.{op} (local.get 0) (local.get 1))", pairs)  # fmt: skip
        f(f"v128.{op}/k", "(param v128) (result v128)", f"(v128.{op} {VK} (local.get 0))",
          [(v,) for v in VALS])  # fmt: skip
    f("v128.not", "(param v128) (result v128)", "(v128.not (local.get 0))", [(v,) for v in VALS])
    f("v128.not/k", "(result v128)", f"(v128.not {VK})", [()])
    f("v128.bitselect", "(param v128 v128 v128) (result v128)",
      "(v128.bitselect (local.get 0) (local.get 1) (local.get 2))",
      [(x, y, c) for x, y in pairs[:6] for c in (0, M64, A)])  # fmt: skip
    f("v128.bitselect/k", "(param v128) (result v128)",
      f"(v128.bitselect {VK} (local.get 0) (v128.const i64x2 -1 0))", [(B,)])  # fmt: skip
    f("v128.any_true", "(param v128) (result i32)", "(v128.any_true (local.get 0))",
      [(v,) for v in (*VALS, 1 << 64, 1 << 63)])  # fmt: skip
    f("v128.any_true/k", "(result i32)", f"(v128.any_true {VK})", [()])
    return "(module (memory 1) (memory $m1 1)\n" + "\n".join(fs) + ")", args


@pytest.mark.parametrize("pins", ["pinned", "nopin"])
def test_data_movement_matches_pyc(pins: str, monkeypatch: pytest.MonkeyPatch) -> None:
    text, cases = _moves()
    jit, pyc = _pair(pins, monkeypatch, text)
    for name, arglist in cases.items():
        for args in arglist:
            got, want = run(jit, name, *args), run(pyc, name, *args)
            assert got == want, (name, args, got, want)
            if name.startswith("st"):
                for a in (0, 65520):
                    assert run(jit, "ld", a) == run(pyc, "ld", a), (name, args, a)
    assert run(jit, "v128.load8_lane 0", 65536, 0) is TrapCode.MEMORY_OUT_OF_BOUNDS


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
    for d, r in ((0, 1), (5, 2), (2, 0)):
        a = Asm()
        x.andnps(a, d, r)
        ours.append(a.finish())
        text.append(f"andnps xmm{d}, xmm{r}")
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
    names = {"and": "and", "andnot": "bic", "or": "orr", "xor": "eor", "bsl": "bsl"}
    for op, (d, n, m) in itertools.product(names, ((0, 1, 2), (2, 0, 1), (31, 7, 16))):
        a = Asm()
        v.vop(a, op, d, n, m)
        ours.append(a.finish())
        text.append(f"{names[op]} v{d}.16b, v{n}.16b, v{m}.16b")
    for d, n in ((0, 0), (31, 17)):
        a = Asm()
        v.vop(a, "not", d, n)
        ours.append(a.finish())
        text.append(f"mvn v{d}.16b, v{n}.16b")
    for size, t in ((1, "16b"), (2, "8h"), (4, "4s"), (8, "2d")):
        for d, r in ((0, 10), (5, 31), (31, 3)):
            a = Asm()
            v.dup(a, d, r, size)
            ours.append(a.finish())
            g = ("x" if size == 8 else "w") + ("zr" if r == 31 else str(r))
            text.append(f"dup v{d}.{t}, {g}")
    theirs = _mc("aarch64", text)
    assert b"".join(ours) == b"".join(theirs), text
