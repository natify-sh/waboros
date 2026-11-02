# pyright: reportPrivateUsage=false
"""Integer SIMD operators on jit-aarch64 (S0005 step 5e1): every operator against `pyc` with
local, constant and computed operands over edge lanes (INT_MIN, all-ones, 0x80..., the
saturation and narrowing boundaries) and shift counts at and past the lane width. Runs on an
arm64 machine or under QEMU (`tools/run_arm64.py`). The NEON forms are checked against llvm-mc
and the ISA gate on any host."""

from pathlib import Path

import pytest

from tests.jit import native, need
from tests.jit.test_simd_abi import LLVM_MC, _computed, _mc, _pair, run
from tests.jit.test_simd_x86 import COUNTS, V
from w2py.frontend import simd as fs
from w2py.targets.base import TrapCode
from w2py.targets.jit.aarch64 import simd as v
from w2py.targets.jit.aarch64.encoder import Asm


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
def test_integer_operators_match_pyc(pins: str, monkeypatch: pytest.MonkeyPatch) -> None:
    native("jit-aarch64")
    text, cases = _module()
    jit, pyc = _pair(pins, monkeypatch, text)
    for a, x in ((65520, V[3]), (65504, V[12])):  # edge lanes at the end of memory
        assert run(jit, "st", a, x) == run(pyc, "st", a, x) == []
    for key, arglist in cases.items():
        for args in arglist:
            got, want = run(jit, key, *args), run(pyc, key, *args)
            assert got == want, (key, [hex(a) for a in args], got, want)
    assert run(jit, "v128.load8x8_s", 65529) is TrapCode.MEMORY_OUT_OF_BOUNDS
    assert len(v.SUPPORTED) == 182  # 5e2 adds the float, conversion and shuffle operators


def _forms() -> tuple[bytes, list[str]]:
    """Every NEON form of the programs, with its llvm-mc spelling; `ldr d`; a constant."""
    a, text = Asm(), list[str]()
    for name, (_, t) in v.FORMS.items():
        for d, n, m in ((0, 1, 2), (7, 31, 16), (2, 0, 1)):
            m *= "{m}" in t  # a two-register form has fixed bits there
            v.run(a, ((name, d, n, m),))
            text.append(t.format(d=d, n=n, m=m))
    for t, base, off, index, want in ((0, 9, 8, None, "[x9, #8]"),
                                      (0, 26, 8 * 4095, None, "[x26, #32760]"),
                                      (7, 26, 0, 9, "[x26, x9]")):  # fmt: skip
        v.ld_d(a, t, base, off, index)
        text.append(f"ldr d{t}, {want}")
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
