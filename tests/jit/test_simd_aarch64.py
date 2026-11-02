# pyright: reportPrivateUsage=false
"""SIMD operators on jit-aarch64 (S0005 steps 5e1, 5e2): every operator against `pyc` with
local, constant and computed operands over edge lanes (INT_MIN, all-ones, 0x80..., the
saturation and narrowing boundaries, NaN, +-0, +-inf, denormals, the trunc_sat and convert
boundaries) and shift counts at and past the lane width; NaN results of float arithmetic, min
and max compare by class. `shuffle`, `swizzle` and rounding have their own test. Runs on an
arm64 machine or under QEMU (`tools/run_arm64.py`). The NEON forms are checked against llvm-mc
and the ISA gate on any host."""

from pathlib import Path

import pytest

from tests.jit import native, need
from tests.jit.test_simd_abi import LLVM_MC, _computed, _mc, _pair, run
from tests.jit.test_simd_x86 import COUNTS, NAN_ARITH, V, _bounds, _nan_class, _v
from waboros.frontend import simd as fs
from waboros.targets.base import TrapCode
from waboros.targets.jit.aarch64 import simd as v
from waboros.targets.jit.aarch64.encoder import Asm


def _module() -> tuple[str, dict[str, list[tuple[int, ...]]]]:
    fs_ = ['(func (export "st") (param i32 v128) (v128.store (local.get 0) (local.get 1)))']
    args: dict[str, list[tuple[int, ...]]] = {}

    def f(key: str, sig: str, body: str, cases: list[tuple[int, ...]]) -> None:
        fs_.append(f'(func (export "{key}") {sig} {body})')
        args[key] = cases

    k = f"(v128.const i64x2 {V[-3] & (1 << 64) - 1} {V[-3] >> 64})"
    for op in sorted(v.SUPPORTED - v.MOVES):
        o = fs.OPS[op - fs.BASE]
        n, res = o.name, "v128" if o.res == fs.V else "i32"
        if o.imm == fs.SHUFFLE:  # its own test below
            continue
        if o.imm == fs.MEM:  # an extending load: edges of memory 0, an offset, a constant
            ok = [(a,) for a in (0, 1, 65527, 65528, 65529, 0xFFFFFFFF)]
            f(n, "(param i32) (result v128)", f"({n} (local.get 0))", ok)
            f(f"{n}/o", "(param i32) (result v128)", f"({n} offset=5 (local.get 0))", ok)
            f(f"{n}/k", "(result v128)", f"({n} (i32.const 65528))", [()])
            f(f"{n}/kf", "(result v128)", f"({n} (i32.const 65525))", [()])  # unaligned
        elif o.pops == (fs.V, fs.I32):  # a shift: the count local, computed, constant
            sig = "(param v128 i32) (result v128)"
            f(n, sig, f"({n} (local.get 0) (local.get 1))", [(x, c) for x in V for c in COUNTS])
            f(f"{n}/c", sig, f"({n} (local.get 0) {_computed('i32', '(local.get 1)')})",
              [(V[2], c) for c in COUNTS])  # fmt: skip
            for c in (0, 35):
                f(f"{n}/k{c}", "(param v128) (result v128)",
                  f"({n} (local.get 0) (i32.const {c}))", [(x,) for x in V])  # fmt: skip
        elif len(o.pops) == 2:
            sig = "(param v128 v128) (result v128)"
            f(n, sig, f"({n} (local.get 0) (local.get 1))", [(x, y) for x in V for y in V])
            f(f"{n}/k", "(param v128) (result v128)", f"({n} {k} (local.get 0))", [(x,) for x in V])
        else:  # one v128 operand; a scalar result added to a computed value held in tos
            f(n, f"(param v128) (result {res})", f"({n} (local.get 0))", [(x,) for x in V])
            f(f"{n}/k", f"(result {res})", f"({n} {k})", [()])
            if res == "i32":
                f(f"{n}/c", "(param v128 i32) (result i32)",
                  f"(i32.sub {_computed('i32', '(local.get 1)')} ({n} (local.get 0)))",
                  [(x, 0x10000) for x in V])  # fmt: skip
    return "(module (memory 1)\n" + "\n".join(fs_) + ")", args


@pytest.mark.parametrize("pins", ["pinned", "nopin"])
def test_operators_match_pyc(pins: str, monkeypatch: pytest.MonkeyPatch) -> None:
    native("jit-aarch64")
    text, cases = _module()
    jit, pyc = _pair(pins, monkeypatch, text)
    for a, x in ((65520, V[3]), (65504, V[12])):  # edge lanes at the end of memory
        assert run(jit, "st", a, x) == run(pyc, "st", a, x) == []
    for key, arglist in cases.items():
        name = key.split("/")[0]
        for args in arglist:
            got, want = run(jit, key, *args), run(pyc, key, *args)
            if name in NAN_ARITH and isinstance(got, list) and isinstance(want, list):
                got, want = [_nan_class(name, g) for g in got], [_nan_class(name, w) for w in want]
            assert got == want, (key, [hex(a) for a in args], got, want)
    assert run(jit, "v128.load8x8_s", 65529) is TrapCode.MEMORY_OUT_OF_BOUNDS
    assert len(v.SUPPORTED) == 236  # every operator (S0005)


def test_shuffle_swizzle_and_rounding_match_pyc(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shuffles that take every lane index from both operands at every position, random ones and
    one with a constant operand; swizzle over every index byte; rounding over every exponent
    (2^23, 2^52, +-0, ties, NaN, sNaN, inf, denormals): bit for bit."""
    native("jit-aarch64")
    import random

    rnd = random.Random(5)
    pats = [tuple((j + i) % 32 for i in range(16)) for j in range(32)]
    pats += [tuple(rnd.randrange(32) for _ in range(16)) for _ in range(12)] + [(31,) * 16]
    fs_ = ['(func (export "st") (param i32 v128) (v128.store (local.get 0) (local.get 1)))',
           '(func (export "swz") (param v128 v128) (result v128)'
           " (i8x16.swizzle (local.get 0) (local.get 1)))",
           '(func (export "sk") (param v128) (result v128)'
           f" (i8x16.shuffle {' '.join(map(str, pats[-2]))} (v128.const i64x2 7 9)"
           " (local.get 0)))"]  # fmt: skip
    for j, ls in enumerate(pats):
        k = " ".join(map(str, ls))
        fs_.append(f'(func (export "s{j}") (param v128 v128) (result v128)'
                   f" (i8x16.shuffle {k} (local.get 0) (local.get 1)))")  # fmt: skip
    for sh in ("f32x4", "f64x2"):
        for o in ("ceil", "floor", "trunc", "nearest"):
            fs_.append(f'(func (export "{sh}.{o}") (param v128) (result v128)'
                       f" ({sh}.{o} (local.get 0)))")  # fmt: skip
    jit, pyc = _pair("pinned", monkeypatch, "(module (memory 1)\n" + "\n".join(fs_) + ")")
    a, b = _v("<16B", list(range(0x10, 0x20))), _v("<16B", list(range(0xA0, 0xB0)))
    ab = a | b << 128
    for j, ls in enumerate(pats):
        want = sum((ab >> 8 * lane & 255) << 8 * i for i, lane in enumerate(ls))
        assert run(jit, f"s{j}", a, b) == [want], ls
        for args in ((b, a), (V[2], V[3])):
            assert run(jit, f"s{j}", *args) == run(pyc, f"s{j}", *args), ls
    assert run(jit, "sk", a) == run(pyc, "sk", a)
    for i0 in range(0, 256, 16):  # every index byte; >= 16 gives 0
        for idx in (_v("<16B", list(range(i0, i0 + 16))),
                    _v("<16B", list(range(i0 + 15, i0 - 1, -1)))):  # fmt: skip
            assert run(jit, "swz", a, idx) == run(pyc, "swz", a, idx), hex(idx)
    for sh, fmt, vals in (("f32x4", "<4I", _bounds(32, 23)), ("f64x2", "<2Q", _bounds(64, 52))):
        n = 4 if sh == "f32x4" else 2
        for i in range(0, len(vals), n):
            x = _v(fmt, (vals[i : i + n] + vals[:n])[:n])
            for o in ("ceil", "floor", "trunc", "nearest"):
                assert run(jit, f"{sh}.{o}", x) == run(pyc, f"{sh}.{o}", x), (sh, o, hex(x))


def _forms() -> tuple[bytes, list[str]]:
    """Every NEON form of the programs, with its llvm-mc spelling; `ldr d`; a constant."""
    a, text = Asm(), list[str]()
    for name, (_, t) in v.FORMS.items():
        for d, n, m in ((0, 1, 2), (7, 31, 16), (2, 0, 1)):
            m *= "{m}" in t  # a two-register form has fixed bits there
            v.run(a, ((name, d, n, m),))
            text.append(t.format(d=d, n=n, n1=(n + 1) % 32, m=m))
    for t, base, off, index, want in ((0, 9, 8, None, "[x9, #8]"),
                                      (0, 26, 8 * 4095, None, "[x26, #32760]"),
                                      (7, 26, 0, 9, "[x26, x9]")):  # fmt: skip
        v.ld_d(a, t, base, off, index)
        text.append(f"ldr d{t}, {want}")
    a.fmov_to(2, 9)  # the shuffle's lanes
    a.emit(v.INS_D1 | 10 << 5 | 2)
    text += ["fmov d2, x9", "mov v2.d[1], x10"]
    v.run(a, (("k", 3, v.W8, 0),))
    text += ["mov x9, #0x0201", "movk x9, #0x0804, lsl #16", "movk x9, #0x2010, lsl #32",
             "movk x9, #0x8040, lsl #48", "dup v3.2d, x9"]  # fmt: skip
    return a.finish(), text


def test_neon_forms_match_llvm_mc() -> None:
    need(LLVM_MC, "llvm-mc")
    ours, text = _forms()
    assert ours == b"".join(_mc("aarch64", text)), text


def test_isa_gate_neon_forms_pass_v80_and_v81_does_not(tmp_path: Path) -> None:
    """In process (a child aarch64 interpreter does not start under QEMU without binfmt)."""
    need(LLVM_MC, "llvm-mc")
    from tools.isa_check import check_aarch64

    def bad(blob: bytes) -> list[str]:
        (tmp_path / "blob.bin").write_bytes(blob)
        return check_aarch64(str(tmp_path / "blob.bin"), len(blob), [])

    assert bad(_forms()[0]) == []
    for w in (0x6E418400, 0x4E819400):  # sqrdmlah (v8.1 RDM), sdot (v8.2 dot product)
        assert len(bad(w.to_bytes(4, "little"))) == 1
