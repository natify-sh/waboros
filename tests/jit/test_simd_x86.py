# pyright: reportPrivateUsage=false
"""Class-D and class-S SIMD operators on jit-x86_64 (S0005 steps 5d1, 5d2): every operator
against `pyc` with local, constant and computed operands over edge lanes (INT_MIN, all-ones,
0x80..., NaN, +-0, +-inf, denormals, the trunc_sat and convert boundaries); NaN results of float
arithmetic, min and max compare by class (a quiet NaN on both). The SSE2 forms are checked
against llvm-mc."""

import itertools
import struct
from pathlib import Path

import pytest

from tests.jit import native, need
from tests.jit.test_encoder import OBJDUMP, run_isa_check
from tests.jit.test_simd_abi import LLVM_MC, VALS, _computed, _mc, _pair, run
from w2py.frontend import simd as fs
from w2py.targets.base import TrapCode
from w2py.targets.jit.x86_64 import simd as x
from w2py.targets.jit.x86_64.encoder import R9, R12, RAX, RCX, Asm, Mem


def _v(fmt: str, lanes: list[int]) -> int:
    return int.from_bytes(struct.pack(fmt, *lanes), "little")


F32 = [0, 0x80000000, 0x7F800000, 0xFF800000, 0x7FC00000, 0x7FA00001, 0xFFC00000, 0x3F800000,
       0xBFC00000, 1, 0x7F7FFFFF, 0x4B000001,  # then the trunc_sat edges: 2^31, 2^32, -2^31 ...
       0x4F000000, 0x4EFFFFFF, 0x4F800000, 0x4F7FFFFF, 0xCF000000, 0xCF000001, 0xBF7FFFFF,
       0x3F000000]  # fmt: skip
F64 = [0, 1 << 63, 0x7FF0 << 48, 0xFFF0 << 48, 0x7FF8 << 48, 0x7FF4000000000001, 0x3FF0 << 48,
       0xC1E0 << 48, 1, 0x41E0000000000000,  # 2^31 - 1, 2^32 - 1, 2^32 - 0.5, 2^32, ...
       0x41DFFFFFFFC00000, 0x41EFFFFFFFE00000, 0x41EFFFFFFFF00000, 0x41F0 << 48,
       0xC1E0000000200000, 0x3FE8 << 48, 0xBFEFFFFFFFFFFFFF, 0x41DFFFFFFFE00000]  # fmt: skip
V = [*VALS, _v("<16B", [0x80] * 16), _v("<8H", [0x8000, 0x7FFF] * 4), _v("<4I", [0x80000000] * 4),
     _v("<16B", list(range(0x7C, 0x8C))), _v("<8H", [1, 0xFFFF, 0x80, 0xFF7F, 3, 0, 0x8001, 7]),
     *(_v("<4I", (F32 * 2)[i : i + 4]) for i in range(0, len(F32), 3)),
     *(_v("<2Q", (F64 * 2)[i : i + 2]) for i in range(0, len(F64), 2))]  # fmt: skip
COUNTS = [0, 1, 7, 8, 15, 16, 31, 32, 33, 63, 64, 65, 0xFFFFFFFF]
NAN_ARITH = {f"{sh}.{o}" for sh in ("f32x4", "f64x2")
             for o in ("add", "sub", "mul", "div", "sqrt", "min", "max")}  # fmt: skip
NAN_ARITH.update(("f32x4.demote_f64x2_zero", "f64x2.promote_low_f32x4"))


def _nan_class(name: str, v: int) -> int:
    """`v` with each NaN lane replaced by a marker, after checking it is arithmetic (quiet)."""
    w = 4 if name.startswith("f32x4") else 8  # the result's lanes
    bits, out = 8 * w, 0
    m = 23 if w == 4 else 52  # mantissa bits
    exp, quiet = (1 << bits - 1) - (1 << m), 1 << m - 1
    for i in range(16 // w):
        lane = v >> bits * i & (1 << bits) - 1
        if lane & exp == exp and lane & (1 << m) - 1:
            assert lane & quiet, (name, hex(v))
            lane = 1
        out |= lane << bits * i
    return out


def _module() -> tuple[str, dict[str, list[tuple[int, ...]]]]:
    fs_ = ['(func (export "st") (param i32 v128) (v128.store (local.get 0) (local.get 1)))']
    args: dict[str, list[tuple[int, ...]]] = {}

    def f(key: str, sig: str, body: str, cases: list[tuple[int, ...]]) -> None:
        fs_.append(f'(func (export "{key}") {sig} {body})')
        args[key] = cases

    k = f"(v128.const i64x2 {V[-3] & (1 << 64) - 1} {V[-3] >> 64})"
    for op in sorted(x.SUPPORTED - x.MOVES):
        o = fs.OPS[op - fs.BASE]
        n, res = o.name, "v128" if o.res == fs.V else "i32"
        if o.imm == fs.MEM:  # an extending load: edges of memory 0, an offset, a constant
            ok = [(a,) for a in (0, 1, 65527, 65528, 65529, 0xFFFFFFFF)]
            f(n, "(param i32) (result v128)", f"({n} (local.get 0))", ok)
            f(f"{n}/o", "(param i32) (result v128)", f"({n} offset=5 (local.get 0))", ok)
            f(f"{n}/k", "(result v128)", f"({n} (i32.const 65528))", [()])
        elif o.pops == (fs.V, fs.I32):  # a shift: the count local, computed, constant
            sig = "(param v128 i32) (result v128)"
            f(n, sig, f"({n} (local.get 0) (local.get 1))", [(v, c) for v in V for c in COUNTS])
            f(f"{n}/c", sig, f"({n} (local.get 0) {_computed('i32', '(local.get 1)')})",
              [(V[2], c) for c in COUNTS])  # fmt: skip
            f(f"{n}/k", "(param v128) (result v128)", f"({n} (local.get 0) (i32.const 35))",
              [(v,) for v in V])  # fmt: skip
        elif len(o.pops) == 2:
            sig = "(param v128 v128) (result v128)"
            f(n, sig, f"({n} (local.get 0) (local.get 1))", list(itertools.product(V, V)))
            f(f"{n}/k", "(param v128) (result v128)", f"({n} {k} (local.get 0))", [(v,) for v in V])
        else:  # one v128 operand; a scalar result added to a computed value held in tos
            f(n, f"(param v128) (result {res})", f"({n} (local.get 0))", [(v,) for v in V])
            f(f"{n}/k", f"(result {res})", f"({n} {k})", [()])
            if res == "i32":
                f(f"{n}/c", "(param v128 i32) (result i32)",
                  f"(i32.sub {_computed('i32', '(local.get 1)')} ({n} (local.get 0)))",
                  [(v, 0x10000) for v in V])  # fmt: skip
    return "(module (memory 1)\n" + "\n".join(fs_) + ")", args


@pytest.mark.parametrize("pins", ["pinned", "nopin"])
def test_class_d_and_s_operators_match_pyc(pins: str, monkeypatch: pytest.MonkeyPatch) -> None:
    native("jit-x86_64")
    text, cases = _module()
    jit, pyc = _pair(pins, monkeypatch, text)
    for a, v in ((65520, V[3]), (65504, V[12])):  # edge lanes at the end of memory
        assert run(jit, "st", a, v) == run(pyc, "st", a, v) == []
    for key, arglist in cases.items():
        name = key.split("/")[0]
        for args in arglist:
            got, want = run(jit, key, *args), run(pyc, key, *args)
            if name in NAN_ARITH and isinstance(got, list) and isinstance(want, list):
                got, want = [_nan_class(name, g) for g in got], [_nan_class(name, w) for w in want]
            assert got == want, (key, [hex(a) for a in args], got, want)
    assert run(jit, "v128.load8x8_s", 65529) is TrapCode.MEMORY_OUT_OF_BOUNDS
    assert len(x.SUPPORTED) == 226  # every operator but the 10 of class L (S0005)


def _forms() -> tuple[list[bytes], list[str]]:
    """Every SSE2 form the module emits, with its llvm-mc spelling."""
    ours: list[bytes] = []
    text: list[str] = []

    def emit(f: object, *a: object, want: str) -> None:
        asm = Asm()
        f(asm, *a)  # pyright: ignore[reportCallIssue, reportOperatorIssue]
        ours.append(asm.finish())
        text.append(want)

    for name in x.FORMS:
        for d, s in ((0, 1), (5, 2), (3, 3)):
            imm = {0: 0, 5: 4, 3: 2}[d] if name[:3] == "cmp" else None
            imm = 0xFA if name.startswith("pshuf") else imm
            want = f"{name} xmm{d}, xmm{s}" + ("" if imm is None else f", {imm}")
            emit(x.run, ((name, d, s, imm),), want=want)
    for name in x.SHIFTS:
        for d, c in ((0, 8), (5, 31)):
            emit(x.run, ((name, d, c, None),), want=f"{name[:-1]} xmm{d}, {c}")
    for r, xr, size, mn in ((RCX, 1, 1, "pmovmskb ecx"), (RAX, 0, 4, "movmskps eax"),
                            (R9, 5, 8, "movmskpd r9d"), (RAX, 0, 1, "pmovmskb eax")):  # fmt: skip
        emit(x.movmsk, r, xr, size, want=f"{mn}, xmm{xr}")
    for xr, m, t in ((0, Mem(RCX), "rcx"), (5, Mem(R12, 8), "r12 + 8"),
                     (1, Mem(R9, 16, RCX), "r9 + rcx*1 + 16")):  # fmt: skip
        emit(x.movq_load, xr, m, want=f"movq xmm{xr}, qword ptr [{t}]")
    for xr, v in ((3, 0x5555555555555555), (5, 0x4330000000000000)):  # a constant via rcx
        emit(x.run, (("k", xr, v, None),),
             want=f"movabs rcx, {v}\nmovq xmm{xr}, rcx\npunpcklqdq xmm{xr}, xmm{xr}")  # fmt: skip
    return ours, text


def test_sse2_forms_match_llvm_mc() -> None:
    need(LLVM_MC, "llvm-mc")
    ours, text = _forms()
    theirs = _mc("x86_64", [".intel_syntax noprefix", *text])
    want, pos = b"".join(theirs), 0
    for t, o in zip(text, ours, strict=True):  # one form may be several instructions (`k`)
        assert want[pos : pos + len(o)] == o, (t, o.hex(), want[pos : pos + len(o)].hex())
        pos += len(o)
    assert pos == len(want)


def test_isa_check_sse2_forms_pass_v1_and_sse4_does_not(tmp_path: Path) -> None:
    need(OBJDUMP, "GNU objdump")
    p = run_isa_check(tmp_path, b"".join(_forms()[0]))
    assert p.returncode == 0, p.stdout[:2000] + p.stderr
    for name, hexa in (("pshufb", "660f3800c1"), ("roundps", "660f3a08c103"),
                       ("pminsd", "660f3839c1"), ("pmulld", "660f3840c1"),
                       ("pinsrd", "660f3a22c001"), ("pabsb", "660f381cc1"),
                       ("pmovsxbw", "660f3820c1")):  # fmt: skip
        p = run_isa_check(tmp_path, bytes.fromhex(hexa))
        assert p.returncode == 1 and name in p.stdout, p.stdout + p.stderr
