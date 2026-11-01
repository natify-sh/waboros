# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
"""Floats and memory.size/grow on `jit-x86_64` (JIT step 3c). Float operators are compared bit by
bit with Wasmtime, the oracle (values cross as i32/i64 bit patterns, so f32 NaN payloads survive);
a NaN result only has to be a NaN of the kind the spec allows. Conversions are also checked
against exact expectations at every trapping boundary."""

import math
import re
import struct
import subprocess
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from tests.jit import native, need
from tests.jit.test_compiler import M32, M64, ROOT, build, outcome, trap_of, wat
from w2py.frontend.binary import parse
from w2py.frontend.validate import validate
from w2py.runtime import link
from w2py.runtime.store import StoreState
from w2py.targets.base import Raw, TrapCode
from w2py.targets.jit.common.compiler import Compiled
from w2py.targets.jit.x86_64 import JitX86_64Target


@pytest.fixture(autouse=True)
def _jit() -> None:
    native("jit-x86_64")


@pytest.fixture(params=["pinned", "nopin"])
def pins(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    if request.param == "nopin":
        monkeypatch.setenv("W2PY_JIT_NOPIN", "all")
    yield


def f32(x: float) -> int:
    return struct.unpack("<I", struct.pack("<f", x))[0]


def f64(x: float) -> int:
    return struct.unpack("<Q", struct.pack("<d", x))[0]


def val(bits: int, t: str) -> float:
    return struct.unpack(
        "<f" if t == "f32" else "<d", struct.pack("<I" if t == "f32" else "<Q", bits)
    )[0]


E32 = [f32(v) for v in (0.0, -0.0, 1.0, -1.0, 0.5, -0.5, 1.5, 2.5, -2.5, 3.5, -3.5, 0.49999997,
                       2.0**23, 2.0**23 + 1, -(2.0**23) + 0.5, 2.0**24, 3.4028235e38, -1e-45, 1e-45,
                       2.0**31, -(2.0**31), 2.0**32, 2.0**63, -(2.0**63), 2.0**64, math.inf,
                       -math.inf, 0.1, -7.75)] + [0x7FC00000, 0xFFC00000, 0x7FA00001, 0x7F800001,
                                                  0xFFC12345]  # fmt: skip
E64 = [f64(v) for v in (0.0, -0.0, 1.0, -1.0, 0.5, -0.5, 1.5, 2.5, -2.5, 3.5, -3.5,
                       0.49999999999999994, 2.0**52, 2.0**52 + 1, -(2.0**52) + 0.5, 2.0**53,
                       1.7976931348623157e308, 5e-324, -5e-324, 2.0**31, -(2.0**31) - 1,
                       -(2.0**31) - 0.5, 2.0**32, 2.0**63, -(2.0**63), 2.0**64, math.inf, -math.inf,
                       0.1, -7.75)] + [0x7FF8000000000000, 0xFFF8000000000000, 0x7FF4000000000001,
                                       0x7FF0000000000001, 0xFFF800000ABCDEF0]  # fmt: skip
BIN = "add sub mul div min max copysign".split()
UN = "abs neg ceil floor trunc nearest sqrt".split()
CMP = "eq ne lt gt le ge".split()
BITS_OPS = ("abs", "neg", "copysign")  # bit-exact even on NaN


def _float_module(t: str) -> str:
    """Every operator on bit-pattern parameters, in several operand shapes: locals, a constant,
    the tos register and (with nopin) slots."""
    i = "i32" if t == "f32" else "i64"

    def p(k: int) -> str:
        return f"({t}.reinterpret_{i} (local.get {k}))"

    def tos(k: int) -> str:
        return f"({t}.reinterpret_{i} ({i}.xor (local.get {k}) ({i}.const 0)))"

    out = ["(module"]
    for op in BIN:
        shapes = [("", p(0), p(1)), ("/tos", tos(0), tos(1)),
                  ("/c", p(0), f"({t}.const -2.5)"), ("/k", f"({t}.const 0.75)", p(1))]  # fmt: skip
        for sh, x, y in shapes:
            out.append(f'(func (export "{op}{sh}") (param {i} {i}) (result {i}) '
                       f"({i}.reinterpret_{t} ({t}.{op} {x} {y})))")  # fmt: skip
    for op in UN:
        for sh, x in (("", p(0)), ("/tos", tos(0))):
            out.append(f'(func (export "{op}{sh}") (param {i}) (result {i}) '
                       f"({i}.reinterpret_{t} ({t}.{op} {x})))")  # fmt: skip
    for op in CMP:
        out.append(f'(func (export "{op}") (param {i} {i}) (result i32) ({t}.{op} {p(0)} {p(1)}))')
        out.append(f'(func (export "{op}/br") (param {i} {i}) (result i32) '
                   f"(if (result i32) ({t}.{op} {tos(0)} {p(1)}) (then (i32.const 7)) "
                   "(else (i32.const 9))))")  # fmt: skip
        out.append(f'(func (export "{op}/k") (param {i}) (result i32) '
                   f"({t}.{op} ({t}.const 1.5) {p(0)}))")  # fmt: skip
    out.append(")")
    return "\n".join(out)


class Oracle:
    """Wasmtime, called with and returning raw integers."""

    def __init__(self, wasm: bytes, config: Any = None) -> None:
        """`config`: a `wasmtime.Config` (features such as tail calls), else the defaults."""
        import wasmtime

        self.w = wasmtime
        self.store = wasmtime.Store(wasmtime.Engine(config) if config is not None else None)
        inst = wasmtime.Instance(self.store, wasmtime.Module(self.store.engine, wasm), [])
        self.ex = inst.exports(self.store)

    def types(self, name: str) -> tuple[list[str], list[str]]:
        ft: Any = self.ex[name].type(self.store)  # pyright: ignore[reportCallIssue]
        return [str(t) for t in ft.params], [str(t) for t in ft.results]

    def __call__(self, name: str, *args: int) -> list[Raw] | TrapCode:
        f: Any = self.ex[name]
        ts, rs = self.types(name)
        signed = [a - (1 << 32) if t == "i32" and a >= 1 << 31 else
                  a - (1 << 64) if t == "i64" and a >= 1 << 63 else a
                  for a, t in zip(args, ts, strict=True)]  # fmt: skip
        try:
            r = f(self.store, *signed)
        except self.w.Trap as e:
            assert e.trap_code is not None
            return TrapCode[e.trap_code.name]
        r = [r] if len(rs) == 1 else list(r or [])
        if rs and rs[0] in ("f32", "f64"):
            return [f32(v) if rs[0] == "f32" else f64(v) for v in r]
        return [v & (M32 if t == "i32" else M64) for v, t in zip(r, rs, strict=True)]


def _is_nan(bits: int, t: str) -> bool:
    return math.isnan(val(bits, t))


def _same(name: str, got: object, want: object, t: str) -> bool:
    """Bit-exact, except that a NaN result of an arithmetic operator only has to be a quiet NaN
    (spec: canonical or arithmetic; the sign and payload are not fixed)."""
    if got == want:
        return True
    if not (isinstance(got, list) and isinstance(want, list) and len(got) == len(want) == 1):
        return False
    if name.split("/")[0] in BITS_OPS or not _is_nan(want[0], t) or not _is_nan(got[0], t):
        return False
    quiet = 1 << (22 if t == "f32" else 51)
    return bool(got[0] & quiet)


@pytest.mark.parametrize("t", ["f32", "f64"])
def test_float_operators_match_wasmtime(t: str, pins: None) -> None:
    pytest.importorskip("wasmtime")
    wasm = wat(_float_module(t))
    jit, ref = build(wasm), Oracle(wasm)
    edge = E32 if t == "f32" else E64
    checked = 0
    for name, _, _ in jit.mod["exports"]:
        n = len(ref.types(name)[0])
        argsets = [(a,) for a in edge] if n == 1 else [(a, b) for a in edge for b in edge]
        for args in argsets:
            got, want = outcome(jit, name, *args), ref(name, *args)
            assert _same(name, got, want, t), (name, [hex(a) for a in args], got, want)
            checked += 1
    assert checked > 10000


@pytest.mark.parametrize("t", ["f32", "f64"])
def test_operands_in_high_registers(t: str) -> None:
    """Float operands straight from pinned integer locals 6 and 7 (r10/r11): REX bits of movd/movq
    with r8-r15 (review of 3c). Equal use counts pin in order of first use."""
    pytest.importorskip("wasmtime")
    i = "i32" if t == "f32" else "i64"
    ps = " ".join([i] * 8)
    first = " ".join(f"(drop (local.get {k}))" for k in (*range(8), *range(6)))
    a, b = (f"({t}.reinterpret_{i} (local.get {k}))" for k in (6, 7))
    body = {
        "add": f"({t}.add {a} {b})",
        "div": f"({t}.div {b} {a})",
        "min": f"({t}.min {a} {b})",
        "max": f"({t}.max {b} {a})",
        "copysign": f"({t}.copysign {a} {b})",
        "sqrt": f"({t}.sqrt {b})",
        "nearest": f"({t}.nearest {a})",
        "floor": f"({t}.floor {b})",
        "lt": f"({t}.lt {a} {b})",
        "eq": f"({t}.eq {b} {a})",
        "trunc": f"({i}.trunc_sat_{t}_s {a})",
        "truncu": f"({i}.trunc_sat_{t}_u {b})",
    }
    out = ["(module"]
    for name, e in body.items():
        r = (
            "i32" if name in ("lt", "eq") else i
        )  # float results as bits: wasmtime-py quiets f32 sNaN
        e = e if name in ("lt", "eq") or name.startswith("trunc") else f"({i}.reinterpret_{t} {e})"
        out.append(f'(func (export "{name}") (param {ps}) (result {r}) {first} {e})')
    wasm = wat("\n".join(out) + ")")
    jit, ref = build(wasm), Oracle(wasm)
    edge = E32 if t == "f32" else E64
    for name in body:
        for x in edge:
            for y in edge[::3]:
                args = (1, 2, 3, 4, 5, 6, x, y)
                got, want = outcome(jit, name, *args), ref(name, *args)
                assert _same(name, got, want, t), (name, hex(x), hex(y), got, want)


def test_min_max_zero_and_nan() -> None:
    jit = build(wat(_float_module("f64")))
    z, nz, nan = f64(0.0), f64(-0.0), 0x7FF4000000000001
    assert jit("min", z, nz) == [nz] and jit("min", nz, z) == [nz]
    assert jit("max", z, nz) == [z] and jit("max", nz, z) == [z]
    for args in ((nan, z), (z, nan), (nan, nan)):
        for op in ("min", "max"):
            (r,) = jit(op, *args)
            assert _is_nan(r, "f64") and r & (1 << 51), (op, args)


def test_nearest_ties_to_even() -> None:
    j32, j64 = build(wat(_float_module("f32"))), build(wat(_float_module("f64")))
    ties = [(0.5, 0.0), (1.5, 2.0), (2.5, 2.0), (-0.5, -0.0), (-2.5, -2.0), (3.5, 4.0),
            (0.49999997, 0.0), (-0.0, -0.0), (2.0**23 - 0.5, 2.0**23), (-4.5, -4.0)]  # fmt: skip
    for x, want in ties:
        assert j32("nearest", f32(x)) == [f32(want)], x
        assert j64("nearest", f64(x)) == [f64(want)], x
    assert j64("nearest", f64(2.0**52 - 0.5)) == [f64(2.0**52)]
    assert j64("nearest", f64(0.49999999999999994)) == [f64(0.0)]


# --------------------------------------------------------------------------- conversions
def _conv_module() -> str:
    """Every conversion on bit-pattern parameters/results: export `<rt>.<ft>_<s|u>` (trapping
    truncation), `sat.<...>`, `<ft>.<it>_<s|u>` (convert), `demote`, `promote`."""
    it_of = {"f32": "i32", "f64": "i64"}
    out = ["(module"]

    def fn(name: str, p: str, r: str, body: str) -> None:
        out.append(f'(func (export "{name}") (param {p}) (result {r}) {body})')

    for rt in ("i32", "i64"):
        for ft in ("f32", "f64"):
            arg = f"({ft}.reinterpret_{it_of[ft]} (local.get 0))"
            for s in "su":
                fn(f"{rt}.{ft}_{s}", it_of[ft], rt, f"({rt}.trunc_{ft}_{s} {arg})")
                fn(f"sat.{rt}.{ft}_{s}", it_of[ft], rt, f"({rt}.trunc_sat_{ft}_{s} {arg})")
                conv = f"({ft}.convert_{rt}_{s} (local.get 0))"
                fn(f"{ft}.{rt}_{s}", rt, it_of[ft], f"({it_of[ft]}.reinterpret_{ft} {conv})")
    fn("demote", "i64", "i32",
       "(i32.reinterpret_f32 (f32.demote_f64 (f64.reinterpret_i64 (local.get 0))))")  # fmt: skip
    fn("promote", "i32", "i64",
       "(i64.reinterpret_f64 (f64.promote_f32 (f32.reinterpret_i32 (local.get 0))))")  # fmt: skip
    return "\n".join([*out, ")"])


CONV = _conv_module()


def _around(b: float, t: str) -> list[float]:
    """b and its neighbours in format t."""
    pack, unpack, typ = ("<f", "<I", 32) if t == "f32" else ("<d", "<Q", 64)
    bits = struct.unpack(unpack, struct.pack(pack, b))[0]
    out = []
    for d in (-2, -1, 0, 1, 2):
        nb = bits + d
        if 0 <= nb < 1 << typ:
            v = struct.unpack(pack, struct.pack(unpack, nb))[0]
            if not math.isnan(v):
                out.append(v)
    return out


def _expect(rt: str, signed: bool, sat: bool, x: float) -> int | TrapCode:
    bits = 32 if rt == "i32" else 64
    lo, hi = (-(1 << bits - 1), (1 << bits - 1) - 1) if signed else (0, (1 << bits) - 1)
    if math.isnan(x):
        return 0 if sat else TrapCode.BAD_CONVERSION_TO_INTEGER
    if math.isinf(x):
        n = hi + 1 if x > 0 else lo - 1
    else:
        n = math.trunc(x)
    if lo <= n <= hi:
        return n & ((1 << bits) - 1)
    if not sat:
        return TrapCode.INTEGER_OVERFLOW
    return (hi if n > hi else lo) & ((1 << bits) - 1)


def test_every_trapping_conversion_boundary() -> None:
    jit = build(wat(CONV))
    checked = 0
    for sat in (False, True):
        for rt in ("i32", "i64"):
            for ft in ("f32", "f64"):
                for signed in (True, False):
                    name = f"{'sat.' if sat else ''}{rt}.{ft}_{'s' if signed else 'u'}"
                    nb = 32 if rt == "i32" else 64
                    bounds = [0.0, -0.0, -1.0, 1.0, float(1 << nb), float(1 << nb - 1),
                              -float(1 << nb - 1), -float(1 << nb - 1) - 1, math.inf, -math.inf,
                              0.5, -0.5, -0.9999, 123.75]  # fmt: skip
                    xs = [v for b in bounds for v in _around(b, ft)] + [math.nan, -math.nan]
                    for x in xs:
                        enc = f32(x) if ft == "f32" else f64(x)
                        want = _expect(rt, signed, sat, x)
                        got = outcome(jit, name, enc)
                        assert got == (want if isinstance(want, TrapCode) else [want]), (name, x)
                        checked += 1
                    for nan in (0x7FA00001, 0xFFC12345) if ft == "f32" else (0x7FF0000000000001,):
                        want_nan = [0] if sat else TrapCode.BAD_CONVERSION_TO_INTEGER
                        assert outcome(jit, name, nan) == want_nan, (name, hex(nan))
    assert checked > 400


def test_conversions_match_wasmtime() -> None:
    pytest.importorskip("wasmtime")
    wasm = wat(CONV)
    jit, ref = build(wasm), Oracle(wasm)
    ints = [0, 1, M32, 0x7FFFFFFF, 0x80000000, 0x01000001, 0xFFFFFF01, 1 << 63, M64,
            (1 << 63) + 1, (1 << 63) - 1, 0x0020000000000001, 0x8000000000000401,
            0xFFFFFFFFFFFFF801, 0x0000000100000001, 0x8000008000000001,
            0xFFFFFF7FFFFFFFFF]  # fmt: skip
    for name, _, _ in jit.mod["exports"]:
        src = ref.types(name)[0][0]
        if name.startswith(("f32.", "f64.")):
            args = [v & (M32 if src == "i32" else M64) for v in ints]
        else:
            args = E32 if src == "i32" else E64
        for a in args:
            got, want = outcome(jit, name, a), ref(name, a)
            t = "f32" if name in ("demote",) or name.startswith("f32.") else "f64"
            assert _same(name, got, want, t), (name, hex(a), got, want)


# --------------------------------------------------------------------------- memory.size/grow
GROW = """(module
  (import "host" "f" (func $f (param i32) (result i32)))
  (memory (export "mem") 1 5)
  (func (export "size") (result i32) (memory.size))
  (func (export "grow") (param i32) (result i32) (memory.grow (local.get 0)))
  (func (export "grow_then_store") (param i32) (result i32)
    (local i32 i32 i32 i32 i32 i32 i32 i32)
    (local.set 1 (i32.const 11)) (local.set 8 (i32.const 88))
    (drop (memory.grow (local.get 0)))
    (i32.store (i32.const 0x1FFFC) (i32.const 77))   ;; in page 2: valid only after the grow
    (i32.add (i32.add (local.get 1) (local.get 8)) (i32.load (i32.const 0x1FFFC))))
  (func (export "host_grows") (param i32) (result i32)
    (drop (call $f (local.get 0)))                    ;; the host grows the memory
    (i32.store (i32.const 0x2FFFC) (i32.const 5))     ;; page 3: r13/r14 reloaded after the call
    (i32.add (memory.size) (i32.load (i32.const 0x2FFFC))))
  (func $deep (export "deep") (param i32) (result i32)
    (if (result i32) (local.get 0)
      (then (drop (call $inner (i32.sub (local.get 0) (i32.const 1))))
            (i32.load (i32.const 0x3FFFC)))           ;; page 4: grown by a callee
      (else (memory.grow (i32.const 3)))))
  (func $inner (param i32) (result i32) (call $deep (local.get 0)))
)"""


def _echo(args: Sequence[Raw]) -> Sequence[Raw]:
    return [args[0]]


def test_memory_grow_and_size() -> None:
    inst = build(wat(GROW), imports={"host": {"f": _echo}})
    assert inst("size") == [1]
    assert trap_of(inst, "grow_then_store", 0) is TrapCode.MEMORY_OUT_OF_BOUNDS
    assert inst("grow_then_store", 1) == [11 + 88 + 77]  # pinned locals survive the libcall
    assert inst("size") == [2]
    assert inst("grow", 4) == [M32]  # beyond the maximum: -1, size unchanged
    assert inst("grow", 0) == [2]
    assert inst("grow", M32) == [M32]
    assert inst("grow", 3) == [2] and inst("size") == [5]
    assert inst("grow", 1) == [M32]


def test_grow_across_host_call_and_nested_entries() -> None:
    """A host function grows the memory through a nested entry into the same instance; the
    outer frames see the new size (r13/r14 are reloaded after every bridge call)."""
    box: dict[str, Any] = {}

    def host(args: list[int]) -> list[int]:
        return box["inst"]("grow", args[0])

    inst = build(wat(GROW), imports={"host": {"f": host}})
    box["inst"] = inst
    assert trap_of(inst, "host_grows", 0) is TrapCode.MEMORY_OUT_OF_BOUNDS
    assert inst("host_grows", 2) == [3 + 5]
    # a callee two wasm frames down grows; the caller's bounds check sees it
    inst2 = build(wat(GROW), imports={"host": {"f": _echo}})
    assert inst2("deep", 2) == [0]
    assert inst2("size") == [4]


def test_two_instances_share_one_memory() -> None:
    store = StoreState()
    a = build(wat(GROW), imports={"host": {"f": _echo}}, store=store)
    user = wat("""(module (import "env" "mem" (memory 1 5))
      (func (export "size") (result i32) (memory.size))
      (func (export "grow") (param i32) (result i32) (memory.grow (local.get 0)))
      (func (export "load") (param i32) (result i32) (i32.load (local.get 0)))
      (func (export "store") (param i32 i32) (i32.store (local.get 0) (local.get 1))))""")
    b = build(user, imports={"env": link.Provider(a.mod, a.inst)}, store=store)
    assert b("grow", 1) == [1] and a("size") == [2] and b("size") == [2]
    a("grow", 1)
    assert b("size") == [3]
    b("store", 0x2FFF0, 1234)
    assert b("load", 0x2FFF0) == [1234]
    assert trap_of(b, "load", 0x30000) is TrapCode.MEMORY_OUT_OF_BOUNDS


def test_grow_failure_is_not_a_trap() -> None:
    inst = build(wat('(module (memory 0) (func (export "g") (param i32) (result i32) '
                     "(memory.grow (local.get 0))))"))  # fmt: skip
    assert inst("g", 0x10001) == [M32] and inst("g", 0) == [0] and inst("g", 1) == [0]


def test_isa_check_on_float_code(tmp_path: Path) -> None:
    """D24: rounding, min/max and conversions are SSE2 sequences (no roundss/roundsd)."""
    from tests.jit.test_encoder import OBJDUMP

    need(OBJDUMP, "GNU objdump")
    for i, text in enumerate((_float_module("f32"), _float_module("f64"), CONV, GROW)):
        mod = parse(wat(text))
        validate(mod)
        c = JitX86_64Target().compile(mod)
        assert isinstance(c, Compiled)
        blob = tmp_path / f"{i}.bin"
        blob.write_bytes(c.code)
        cmd = [sys.executable, str(ROOT / "tools" / "isa_check.py"), str(blob)]
        if c.data_start < len(c.code):
            cmd += ["--data", f"{c.data_start}:{len(c.code)}"]
        p = subprocess.run(cmd, capture_output=True, text=True)
        assert p.returncode == 0, p.stdout + p.stderr
        # Win64 callee-saved xmm6-15 are never touched: the trampolines need not save them (S0003)
        dis = subprocess.run([str(OBJDUMP), "-D", "-b", "binary", "-m", "i386:x86-64", str(blob)],
                             capture_output=True, text=True, check=True).stdout  # fmt: skip
        assert not re.search(r"%xmm([6-9]|1[0-5])\b", dis), i
