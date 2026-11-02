# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
"""Differential fuzz of the scalar code generation (S0007, "Test plan"): fixed seeds generate
functions around `local ± const` addresses (constants near 0, 2**31 and 2**32 - 1, offsets up to
and past GUARD) in a one-page memory, `local.tee` of a pointer while older reads of it are on the
stack, deep expressions, branches with values, multi-value returns of swapped locals, calls and a
call-free function of more than 256 slots. Results and trap codes on this machine's JIT (in
explicit and, where it exists, guard mode) must equal `pyc`'s."""

import random

import pytest

from tests.jit.test_compiler import JIT, build, outcome, wat
from waboros.targets import base
from waboros.targets.jit.common import guard

M = 0xFFFFFFFF
K = [0, 1, 3, 4, 8, 0x7FFFFFF8, 0x7FFFFFFF, 0x80000000, 0x80000004, 0xFFFFFFF8, 0xFFFFFFFC, M]
OFFS = [0, 0, 4, 0xFFF8, 0x10000, guard.GUARD - 8, guard.GUARD, guard.GUARD + 8, M]
LOADS32 = ["i32.load", "i32.load8_s", "i32.load8_u", "i32.load16_s", "i32.load16_u"]
LOADS64 = ["i64.load", "i64.load8_u", "i64.load16_s", "i64.load32_s", "i64.load32_u"]
STORES = [("i32.store", 32), ("i32.store8", 32), ("i32.store16", 32), ("i64.store", 64),
          ("i64.store8", 64), ("i64.store16", 64), ("i64.store32", 64)]  # fmt: skip
I32L, I64L = (0, 1, 3, 4), (2, 5)  # params (i32 i32 i64), locals (i32 i32 i64)
PTR, TMP = (0, 1, 0, 1, 0, 1, 3), (3, 4)  # pointers (mostly the params) move by small steps only
ARGS = [0, 4, 100, 0x1234, 0x8000, 0xFFE8, 0xFFFC, 0xFFFF, 0x10000, 0xFFFFFFFC, 0x80000000]


class Gen:
    def __init__(self, seed: int, callee: str | None) -> None:
        self.r, self.callee = random.Random(seed), callee  # called at most once (no blow-up)

    def k(self) -> int:
        return self.r.choice(K) if self.r.random() < 0.1 else self.r.randrange(16)

    def addk(self) -> str:  # local + k, local - k, and one more constant on top of either
        r, j = self.r, self.r.choice(PTR)
        e = f"(i32.{r.choice(['add', 'add', 'add', 'sub'])} (local.get {j}) (i32.const {self.k()}))"
        if r.random() < 0.3:
            e = f"(i32.{r.choice(['add', 'sub'])} {e} (i32.const {self.k()}))"
            e = f"(i32.add (i32.const {self.k()}) {e})" if r.random() < 0.5 else e
        return e if r.random() < 0.9 else f"(local.get {j})"

    def mem(self) -> str:
        o = self.r.choice(OFFS) if self.r.random() < 0.06 else self.r.randrange(8)
        return f"offset={o} {self.addk()}"

    def e32(self, d: int) -> str:
        r = self.r
        c = r.randrange(12 if d < 5 else 3)
        if c == 0:
            return f"(local.get {r.choice(I32L)})"
        if c == 1:
            return f"(i32.const {self.k()})"
        if c == 2:
            return self.addk()
        if c == 3:
            op = r.choice(["add", "sub", "mul", "xor", "shl", "shr_u", "lt_u", "eq"])
            return f"(i32.{op} {self.e32(d + 1)} {self.e32(d + 1)})"
        if c == 4:
            return f"({r.choice(LOADS32)} {self.mem()})"
        if c == 5:
            j = r.choice(PTR)  # an older read of j, then a tee of its increment
            old = r.choice([f"(local.get {j})", f"(i32.add (local.get {j}) (i32.const 8))"])
            return f"(i32.sub {old} (local.tee {j} (i32.add (local.get {j}) (i32.const 4))))"
        if c == 6:
            return f"(local.tee {r.choice(TMP)} {self.e32(d + 1)})"
        if c == 7:
            return f"(i32.wrap_i64 {self.e64(d + 1)})"
        if c == 8:
            return f"(select {self.e32(d + 1)} {self.e32(d + 1)} {self.e32(d + 1)})"
        if c == 9 and self.callee:  # (i32 i64) -> i32 + wrapped i64
            f, self.callee = self.callee, None
            call = f"(call {f} {self.e32(d + 1)} {self.addk()} {self.e64(d + 1)})"
            return f"(i32.add (i32.wrap_i64 {call}))"
        if c == 9:
            return f"(i32.sub (call $swap {self.e32(d + 1)} {self.addk()}))"
        if c == 10:
            return f"(block (result i32) (br_if 0 {self.e32(d + 1)} {self.e32(d + 1)}))"
        return (
            f"(block (result i32) (drop (br_if 0 {self.addk()} {self.e32(d + 1)})) {self.addk()})"
        )

    def e64(self, d: int) -> str:
        r = self.r
        c = r.randrange(5 if d < 5 else 2)
        if c == 0:
            return f"(local.get {r.choice(I64L)})"
        if c == 1:
            return f"(i64.const {self.k() - (1 << 31)})"
        if c == 2:
            return f"(i64.{r.choice(['add', 'mul', 'xor'])} {self.e64(d + 1)} {self.e64(d + 1)})"
        if c == 3:
            return f"(i64.extend_i32_u {self.e32(d + 1)})"
        return f"({r.choice(LOADS64)} {self.mem()})"

    def stmt(self, d: int) -> str:
        r = self.r
        c = r.randrange(6 if d < 2 else 5)
        if c == 0:
            return f"(local.set {r.choice(TMP)} {self.e32(1)})"
        if c == 1:
            return f"(local.set {r.choice(I64L)} {self.e64(1)})"
        if c in (2, 3):
            op, w = r.choice(STORES)
            return f"({op} {self.mem()} {self.e32(1) if w == 32 else self.e64(1)})"
        if c == 4:
            j = r.choice(PTR)
            return f"(local.set {j} (i32.add (local.get {j}) (i32.const {r.randrange(-8, 9)})))"
        return f"(if {self.e32(2)} (then {self.stmt(d + 1)}) (else {self.stmt(d + 1)}))"

    def func(self, name: str) -> str:
        body = " ".join(self.stmt(0) for _ in range(self.r.randrange(2, 7)))
        return (f"(func {name} (export \"{name[1:]}\") (param i32 i32 i64) (result i32 i64) "
                f"(local i32 i32 i64) {body} {self.e32(0)} {self.e64(0)})")  # fmt: skip


def module(seed: int) -> str:
    fs = [Gen(seed * 100 + i, f"$f{i - 1}" if i else None).func(f"$f{i}") for i in range(5)]
    big = " ".join(f"(local.set {j} (i32.add (local.get {j - 1}) (i32.const {j})))"
                   for j in range(1, 300, 7))  # fmt: skip
    deep = "(local.get 1)"  # 28 values on the stack at once, more than the pool has registers
    for i in range(14):
        x = (
            f"(i32.sub (local.get 0) (i32.const {i}))"
            if i % 2
            else "(i32.mul (local.get 0) (local.get 1))"
        )
        deep = f"(i32.add {x} (i32.xor (local.get 1) {deep}))"
    return f"""(module (memory (export "m") 1)
      (func $swap (export "swap") (param i32 i32) (result i32 i32)
        (i32.add (local.get 1) (i32.const -1)) (local.get 0))
      (func (export "deep") (param i32 i32) (result i32) {deep})
      (func (export "swap2") (param i32 i32) (result i32 i32) (local.get 1) (local.get 0))
      (func (export "big") (param i32) (result i32) (local i32 i32) (local i64 i64) (local i32)
        (local i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32 i32)
        (local {" i32" * 280}) {big} (i32.load offset=3 (i32.add (local.get 295) (i32.const -7))))
      (func (export "sum") (result i64) (local i32 i64)
        (loop
          (local.set 1 (i64.add (i64.rotl (local.get 1) (i64.const 1)) (i64.load (local.get 0))))
          (br_if 0 (i32.lt_u (local.tee 0 (i32.add (local.get 0) (i32.const 8)))
            (i32.const 65536))))
        (local.get 1))
      {" ".join(fs)})"""


@pytest.mark.parametrize("bounds", ["explicit", "guard"])
@pytest.mark.parametrize("seed", range(12))
def test_scalar_fuzz_matches_pyc(seed: int, bounds: str, monkeypatch: pytest.MonkeyPatch) -> None:
    if bounds == "guard" and guard.host_row() not in base.TARGETS[JIT]().guard_rows:  # pyright: ignore[reportAttributeAccessIssue]
        pytest.skip(f"no guard mode for {JIT} on {guard.host_row()}")
    monkeypatch.setenv("WABOROS_JIT_BOUNDS", bounds)
    wasm = wat(module(seed))
    jit, pyc = build(wasm), build(wasm, "pyc")
    r = random.Random(seed)
    for a, b in (("swap", (5, 9)), ("swap2", (5, 9)), ("big", (r.choice(ARGS),)), ("deep", (7, 3))):
        assert outcome(jit, a, *b) == outcome(pyc, a, *b), (seed, a)
    for _ in range(24):
        for i in range(5):
            p = r.choice(ARGS[:6]), r.choice(ARGS[: r.choice((6, 11))])
            args = (*p, r.choice(ARGS) | r.randrange(3) << 32)
            assert outcome(jit, f"f{i}", *args) == outcome(pyc, f"f{i}", *args), (seed, i, args)
    assert outcome(jit, "sum") == outcome(pyc, "sum"), seed
