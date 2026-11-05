# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
"""Wide arithmetic on both JITs (S0008 step 3, the `wide` backend method): every operand kind in
every position (a pinned local or a local slot, `Imm` 0 / small / above imm32 / all ones, `tos`,
a slot), the same local twice, results consumed in both orders, inside loops, an operand in `tos`
below the operands; carries and borrows across 64 bits, INT64_MIN * INT64_MIN and -1 * -1 signed
and unsigned. Each result equals `pyc` and an exact reference; guard and explicit bounds, pinned
and unpinned locals. A planted `xor` between `add` and `adc` must fail the carry case (LESSONS 1).
aarch64 runs on an arm64 machine or under QEMU (`tools/run_arm64.py`)."""

import itertools
import random
import sys

import pytest

from tests.jit import native, need
from tests.jit.test_compiler import build, wat
from waboros.targets import base
from waboros.targets.jit.common import guard

M64, M128 = (1 << 64) - 1, (1 << 128) - 1
OPS = {"i64.add128": 4, "i64.sub128": 4, "i64.mul_wide_s": 2, "i64.mul_wide_u": 2}
# constant operands: 0, small, above imm32, all ones (imm32 -1), 2^31 (not a sign-extended imm32)
KV = {"k0": 0, "k5": 5, "kbig": 0x123456789, "kones": M64, "k80": 0x80000000, "kmin": 1 << 63}
KINDS = {k: f"(i64.const {v:#x})" for k, v in KV.items()}
EDGE = [0, 1, 2, M64, M64 - 1, 1 << 63, (1 << 63) - 1, 0xFFFFFFFF, 1 << 32, 0x123456789ABCDEF0]


def ref(op: str, *a: int) -> list[int]:
    s = [x - (x >> 63 << 64) for x in a]
    if op in ("i64.add128", "i64.sub128"):
        x, y = a[0] | a[1] << 64, a[2] | a[3] << 64
        t = x + y if op == "i64.add128" else x - y
    else:
        t = s[0] * s[1] if op == "i64.mul_wide_s" else a[0] * a[1]
    return [t & M64, (t & M128) >> 64]


def _operand(kind: str, i: int) -> str:
    if kind in KINDS:
        return KINDS[kind]
    if kind == "t":  # computed: in `tos` if it is the last computed operand, else spilled
        return f"(i64.xor (local.get {i}) (i64.const 0))"
    if kind == "s":  # a call result: a slot
        return f"(call $id (local.get {i}))"
    return f"(local.get {i})"


def _module() -> tuple[str, dict[str, tuple[str, list[str]]]]:
    """The text and, per export, (operator, operand kinds) for the generic cases."""
    fs: list[str] = ["(func $id (param i64) (result i64) (local.get 0))"]
    cases: dict[str, tuple[str, list[str]]] = {}
    for op, n in OPS.items():
        ps, res = " ".join(["(param i64)"] * n), "(result i64 i64)"
        kinds = [["l"] * n, ["t"] * n, ["s"] * n]
        kinds += [
            ["l"] * p + [k] + ["l"] * (n - p - 1) for p in range(n) for k in (*KINDS, "t", "s")
        ]
        for ks in kinds:
            name = f"{op}/{','.join(ks)}"
            body = " ".join(_operand(k, i) for i, k in enumerate(ks))
            fs.append(f'(func (export "{name}") {ps} {res} ({op} {body}))')
            cases[name] = (op, ks)
        get = " ".join(f"(local.get {i})" for i in range(n))
        same = " ".join(["(local.get 0)"] * n)
        fs += [
            f'(func (export "{op}/same") (param i64) {res} ({op} {same}))',
            f'(func (export "{op}/swap") {ps} {res} (local i64 i64) ({op} {get})'
            f" (local.set {n}) (local.set {n + 1}) (local.get {n}) (local.get {n + 1}))",
            f'(func (export "{op}/lo") {ps} (result i64) ({op} {get}) (drop))',
            f'(func (export "{op}/hi") {ps} (result i64) (local i64) ({op} {get})'
            f" (local.set {n}) (drop) (local.get {n}))",
            f'(func (export "{op}/diff") {ps} (result i64) ({op} {get}) (i64.sub))',
            f'(func (export "{op}/below") {ps} (result i64 i64 i64)'  # tos below the operands
            f" (i64.xor (local.get 0) (local.get 1)) ({op} {get}))",
        ]
    # loops: a 128-bit accumulator, sum of x * n (unsigned) and minus x * n (signed), n down to 1
    for name, mul, acc in (("loop_u", "i64.mul_wide_u", "i64.add128"),
                           ("loop_s", "i64.mul_wide_s", "i64.sub128")):  # fmt: skip
        fs.append(
            f'(func (export "{name}") (param $x i64) (param $n i64) (result i64 i64)'
            " (local $lo i64) (local $hi i64)"
            f" (loop $l (local.get $lo) (local.get $hi) (local.get $x) (local.get $n) ({mul})"
            f" ({acc}) (local.set $hi) (local.set $lo)"
            " (br_if $l (i64.ne (local.tee $n (i64.sub (local.get $n) (i64.const 1)))"
            " (i64.const 0)))) (local.get $lo) (local.get $hi))"
        )
    fs.append(  # results as the slot operands of the next wide operator, and a load as an operand
        '(func (export "chain") (param i32 i64 i64 i64) (result i64 i64)'
        " (i64.mul_wide_u (local.get 1) (local.get 2)) (local.get 3) (i64.load (local.get 0))"
        " (i64.add128))"
    )
    return (
        '(module (memory 1) (data (i32.const 8) "\\ff\\ff\\ff\\ff\\ff\\ff\\ff\\ff")\n'
        + "\n".join(fs)
        + ")",
        cases,
    )


def _inputs(n: int, rnd: random.Random) -> list[tuple[int, ...]]:
    if n == 2:
        return list(itertools.product(EDGE, EDGE))
    six = [0, 1, M64, 1 << 63, (1 << 63) - 1, 0xFFFFFFFF00000000]
    return list(itertools.product(six, repeat=4)) + [
        tuple(rnd.getrandbits(64) for _ in range(4)) for _ in range(200)
    ]


def _bounds(mode: str, target: str, monkeypatch: pytest.MonkeyPatch) -> None:
    if mode == "guard":
        tgt = base.TARGETS[target]()
        if not sys.platform.startswith(("linux", "darwin", "win32", "android")):
            pytest.skip(f"no guard pages on {sys.platform} yet")
        need(guard.host_row() in tgt.guard_rows, f"a guard-page handler for {guard.host_row()}")  # pyright: ignore[reportAttributeAccessIssue]
    monkeypatch.setenv("WABOROS_JIT_BOUNDS", mode)


@pytest.mark.parametrize("pins", ["pinned", "nopin"])
@pytest.mark.parametrize("mode", ["guard", "explicit"])
@pytest.mark.parametrize("target", ["jit-x86_64", "jit-aarch64"])
def test_wide_matches_pyc(
    target: str, mode: str, pins: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    native(target)
    _bounds(mode, target, monkeypatch)
    if pins == "nopin":
        monkeypatch.setenv("WABOROS_JIT_NOPIN", "all")
    text, cases = _module()
    w = wat(text)
    jit, pyc = build(w, target), build(w, "pyc")
    rnd = random.Random(19)
    for name, (op, ks) in cases.items():
        for args in _inputs(OPS[op], rnd)[:: 1 if ks in (["l"] * 4, ["l"] * 2) else 7]:
            want = ref(op, *(KV.get(k, a) for k, a in zip(ks, args, strict=True)))
            assert jit(name, *args) == pyc(name, *args) == want, (name, [hex(a) for a in args])
    for op, n in OPS.items():
        for args in _inputs(n, rnd)[::5]:
            lo, hi = ref(op, *args)
            for key, want in (("same", ref(op, *[args[0]] * n)), ("swap", [hi, lo]), ("lo", [lo]),
                              ("hi", [hi]), ("diff", [(lo - hi) & M64]),
                              ("below", [args[0] ^ args[1], lo, hi])):  # fmt: skip
                a = args[:1] if key == "same" else args
                got = jit(f"{op}/{key}", *a)
                assert got == pyc(f"{op}/{key}", *a) == want, (op, key, [hex(x) for x in a])
    for x, n in ((3, 10), (M64, 1000), (1 << 63, 77), (0x123456789ABCDEF, 300)):
        su = sum(x * i for i in range(1, n + 1))
        ss = -sum((x - (x >> 63 << 64)) * i for i in range(1, n + 1)) & M128
        assert jit("loop_u", x, n) == pyc("loop_u", x, n) == [su & M64, su >> 64 & M64]
        assert jit("loop_s", x, n) == pyc("loop_s", x, n) == [ss & M64, ss >> 64]
    for a, b, c in ((M64, M64, 1), (1 << 63, 2, M64), (5, 7, 0)):
        t = a * b + (c | M64 << 64)  # the load reads all ones from address 8
        assert jit("chain", 8, a, b, c) == pyc("chain", 8, a, b, c) == [t & M64, t >> 64 & M64]
    assert jit("i64.add128/l,l,l,l", M64, 0, 1, 0) == [0, 1]  # the carry case
    assert jit("i64.sub128/l,l,l,l", 0, 0, 1, 0) == [M64, M64]  # the borrow case
    assert jit("i64.mul_wide_s/l,l", 1 << 63, 1 << 63) == [0, 1 << 62]
    assert jit("i64.mul_wide_u/l,l", 1 << 63, 1 << 63) == [0, 1 << 62]
    assert jit("i64.mul_wide_s/l,l", M64, M64) == [1, 0]
    assert jit("i64.mul_wide_u/l,l", M64, M64) == [1, M64 - 1]


def test_a_planted_flag_clobber_fails_the_carry_case(monkeypatch: pytest.MonkeyPatch) -> None:
    """LESSONS 1: `xor edx, edx` after `mov [k], rcx` (between add and adc) clears CF."""
    from waboros.targets.jit.x86_64 import encoder

    native("jit-x86_64")
    w = wat('(module (func (export "f") (param i64 i64 i64 i64) (result i64 i64)'
            " (i64.add128 (local.get 0) (local.get 1) (local.get 2) (local.get 3))))")  # fmt: skip
    assert build(w, "jit-x86_64")("f", M64, 0, 1, 0) == [0, 1]
    mov = encoder.Asm.mov

    def planted(a: encoder.Asm, dst: object, src: object, w: bool = False) -> None:
        mov(a, dst, src, w)  # pyright: ignore[reportArgumentType]
        if isinstance(dst, encoder.Mem) and src == encoder.RCX:
            a.alu("xor", encoder.RDX, encoder.RDX)

    monkeypatch.setattr(encoder.Asm, "mov", planted)
    assert build(w, "jit-x86_64")("f", M64, 0, 1, 0) == [0, 0]
