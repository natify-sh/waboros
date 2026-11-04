# pyright: reportPrivateUsage=false
"""Relaxed SIMD on both JITs (S0008 step 2, D49): a seeded differential, 500 inputs per operator
(random bits, random floats whose product nearly cancels the addend, special float lanes, byte
patterns), operands as locals and one as a constant. The 16 deterministic operators equal `pyc`
bit for bit. madd/nmadd: `jit-x86_64` equals `pyc` (unfused) and differs from a fused reference
on FLT_MAX * 2 - FLT_MAX; `jit-aarch64` equals the fused reference and differs from `pyc` there.
The fused reference is `math.fma` for f64 where Python >= 3.13, exact `Fraction` arithmetic
rounded once otherwise and always for f32 (an fma in doubles, then to f32, rounds twice). NaN
lanes compare by class. aarch64 runs on an arm64 machine or under QEMU (`tools/run_arm64.py`)."""

import math
import random
import struct
from fractions import Fraction
from typing import Any

import pytest

from tests.jit import native
from tests.jit.test_simd_abi import _pair, run
from tests.jit.test_simd_x86 import F32, F64, _nan_class, _v
from waboros.frontend import simd as fs

OPS = {o.name: len(o.pops) for sub, o in fs.OPS.items() if sub >= 0x100}
MADD = {n for n in OPS if "madd" in n}
FLOATS = MADD | {n for n in OPS if ".relaxed_m" in n}  # min/max too: NaN lanes by class
FMAX = 0x7F7FFFFF  # f32 FLT_MAX: FLT_MAX * 2 + -FLT_MAX is FLT_MAX fused, +inf unfused
K = (0x0123456789ABCDEF_FEDCBA9876543210, 0x3F800000_7F7FFFFF_80000000_C0000000)


def _inputs(rnd: random.Random) -> list[tuple[int, int, int]]:
    bytes_ = (0x00, 0x01, 0x7F, 0x80, 0x81, 0xFF, 0x0F, 0xF0, 0x10)
    out: list[tuple[int, int, int]] = []
    for i in range(500):
        if i % 4 == 0:
            vs = [rnd.getrandbits(128) for _ in range(3)]
        elif i % 4 == 1:  # c close to -(a * b): the fused and unfused results differ often
            fmt, n = rnd.choice((("<4f", 4), ("<2d", 2)))
            a = [rnd.uniform(-4, 4) for _ in range(n)]
            b = [rnd.uniform(-4, 4) for _ in range(n)]
            c = [-x * y * (1 + rnd.uniform(-1e-6, 1e-6)) for x, y in zip(a, b, strict=True)]
            vs = [int.from_bytes(struct.pack(fmt, *t), "little") for t in (a, b, c)]
        elif i % 4 == 2:
            lanes, fmt = rnd.choice(((F32, "<4I"), (F64, "<2Q")))
            vs = [_v(fmt, rnd.choices(lanes, k=16 // struct.calcsize(fmt[-1]))) for _ in range(3)]
        else:
            vs = [_v("<16B", rnd.choices(bytes_, k=16)) for _ in range(3)]
        out.append((vs[0], vs[1], vs[2]))
    return out


def _round(q: Fraction, p: int, emin: int, emax: int) -> float:
    """The nearest (ties to even) number with a p-bit significand and exponent range
    [emin, emax], subnormals included, or an infinity."""
    s, q = (-1.0 if q < 0 else 1.0), abs(q)
    e = q.numerator.bit_length() - q.denominator.bit_length()
    while Fraction(2) ** e > q:
        e -= 1
    while Fraction(2) ** (e + 1) <= q:
        e += 1
    ulp = Fraction(2) ** (max(e, emin) - p + 1)
    v = round(q / ulp) * ulp  # Fraction.__round__: ties to even
    return s * math.inf if v >= Fraction(2) ** (emax + 1) else s * float(v)


def _fused(a: float, b: float, c: float, f32: bool) -> float:
    """fma(a, b, c) rounded once to the lane's format."""
    if not all(map(math.isfinite, (a, b, c))):
        return a * b + c  # a NaN, inf * 0, inf - inf: NaN; else the infinity, exact
    if not f32 and hasattr(math, "fma"):
        try:
            return math.fma(a, b, c)  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType, reportUnknownVariableType]
        except OverflowError:
            pass
    q = Fraction(a) * Fraction(b) + Fraction(c)
    if q == 0:  # -0 only when a * b and c are both -0 (round to nearest)
        return a * b + c
    return _round(q, 24, -126, 127) if f32 else _round(q, 53, -1022, 1023)


def _fused_v(name: str, a: int, b: int, c: int) -> int:
    fmt, f32 = ("<4f", True) if name.startswith("f32x4") else ("<2d", False)
    la, lb, lc = (struct.unpack(fmt, v.to_bytes(16, "little")) for v in (a, b, c))
    sign = -1.0 if "nmadd" in name else 1.0
    r = [_fused(sign * x, y, z, f32) for x, y, z in zip(la, lb, lc, strict=True)]
    return int.from_bytes(struct.pack(fmt, *r), "little")


def _module() -> str:
    fs_: list[str] = []
    for n, k in OPS.items():
        p = " ".join(["(param v128)"] * k)
        ops = " ".join(f"(local.get {i})" for i in range(k))
        fs_.append(f'(func (export "{n}") {p} (result v128) ({n} {ops}))')
        kk = f"(v128.const i64x2 {K[k % 2] & (1 << 64) - 1} {K[k % 2] >> 64})"
        ops = " ".join(kk if i == k // 2 else f"(local.get {i})" for i in range(k))
        fs_.append(f'(func (export "{n}/k") {p} (result v128) ({n} {ops}))')  # a constant operand
    fs_.append('(func (export "st") (param i32 v128) (v128.store (local.get 0) (local.get 1)))')
    return "(module (memory 1)\n" + "\n".join(fs_) + ")"


def _lanes(name: str, vs: list[Any]) -> list[Any]:
    return [_nan_class(name, x) for x in vs]


def _bits(name: str, vs: list[Any]) -> list[Any]:
    return vs


@pytest.mark.parametrize("target", ["jit-x86_64", "jit-aarch64"])
def test_relaxed_simd_differential(target: str, monkeypatch: pytest.MonkeyPatch) -> None:
    native(target)
    jit, pyc = _pair("pinned", monkeypatch, _module())
    fused = target == "jit-aarch64"  # D49: fmla/fmls there, mulps + addps on SSE2
    inputs = _inputs(random.Random(8))
    for name, k in OPS.items():
        for key in (name, f"{name}/k"):
            for args in inputs:
                got, want = run(jit, key, *args[:k]), run(pyc, key, *args[:k])
                assert isinstance(got, list) and isinstance(want, list), (key, got, want)
                cmp = _lanes if name in FLOATS else _bits
                if fused and name in MADD:
                    if key == name:  # the reference takes the operands as passed
                        assert cmp(name, got) == cmp(name, [_fused_v(name, *args)]), (key, args)
                    continue
                assert cmp(name, got) == cmp(name, want), (key, [hex(a) for a in args[:k]])
    # the FLT_MAX case: fused gives FLT_MAX, unfused +inf (relaxed_madd_nmadd.wast's first `either`)
    fmax, inf, nmax = (_v("<4I", [x] * 4) for x in (FMAX, 0x7F800000, FMAX | 1 << 31))
    two = _v("<4I", [0x40000000] * 4)  # 2.0
    for name, a in (("f32x4.relaxed_madd", fmax), ("f32x4.relaxed_nmadd", nmax)):
        assert _fused_v(name, a, two, nmax) == fmax and run(pyc, name, a, two, nmax) == [inf]
        assert run(jit, name, a, two, nmax) == [fmax if fused else inf], (target, name)
    for name in ("f64x2.relaxed_madd", "f64x2.relaxed_nmadd"):  # DBL_MAX * 2 - DBL_MAX
        s = 1 << 63 if "nmadd" in name else 0
        args = (_v("<2Q", [0x7FEFFFFFFFFFFFFF | s] * 2), _v("<2Q", [0x4000 << 48] * 2),
                _v("<2Q", [0xFFEFFFFFFFFFFFFF] * 2))  # fmt: skip
        want = _v("<2Q", [0x7FEFFFFFFFFFFFFF] * 2) if fused else _v("<2Q", [0x7FF0 << 48] * 2)
        assert run(jit, name, *args) == [want], (target, name)
