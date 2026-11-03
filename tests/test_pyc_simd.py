# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownParameterType=false, reportMissingParameterType=false
"""S0005 step 5b: v128 on `pyc` across the boundaries the spec files do not reach (host functions,
exported and imported globals, block types, select, drop, call_indirect) and a few NaN lanes.
Where Wasmtime is installed the same module runs on it through the spec runner's adapter."""

import struct
from collections.abc import Sequence
from typing import Any

import pytest

from tests.hosts import x87_quiets_snan
from tests.spec.target_engine import TargetEngine
from waboros.frontend.binary import parse
from waboros.frontend.validate import validate
from waboros.runtime import link
from waboros.runtime.store import StoreState
from waboros.targets import base

V, I32, F32 = b"\x7b", b"\x7f", b"\x7d"
M128 = (1 << 128) - 1


def leb(n: int) -> bytes:
    out = bytearray()
    while True:
        b, n = n & 0x7F, n >> 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def vec(items: list[bytes]) -> bytes:
    return leb(len(items)) + b"".join(items)


def name(s: str) -> bytes:
    return vec([bytes([c]) for c in s.encode()])


def sec(i: int, body: bytes) -> bytes:
    return bytes([i]) + leb(len(body)) + body


def fd(sub: int, *imm: bytes) -> bytes:
    return b"\xfd" + leb(sub) + b"".join(imm)


def const(v: int) -> bytes:
    return fd(0x0C, v.to_bytes(16, "little"))


def i32c(v: int) -> bytes:
    return b"\x41" + bytes([v & 0x7F])


def get(i: int) -> bytes:
    return b"\x20" + leb(i)


def ty(params: bytes, results: bytes) -> bytes:
    return b"\x60" + vec([bytes([t]) for t in params]) + vec([bytes([t]) for t in results])


def module(
    types: list[bytes],
    funcs: list[tuple[int, list[tuple[int, int]], bytes]],
    exports: Sequence[tuple[str, int, int]] = (),
    imports: Sequence[bytes] = (),
    tables: bytes = b"",
    globals_: Sequence[bytes] = (),
    elem: bytes = b"",
) -> bytes:
    """`funcs`: (type index, locals as (count, type) groups, body without the final end)."""
    out = b"\0asm\1\0\0\0" + sec(1, vec(types))
    if imports:
        out += sec(2, vec(list(imports)))
    out += sec(3, vec([leb(t) for t, _, _ in funcs]))
    if tables:
        out += sec(4, tables)
    if globals_:
        out += sec(6, vec(list(globals_)))
    out += sec(7, vec([name(n) + bytes([k]) + leb(i) for n, k, i in exports]))
    if elem:
        out += sec(9, elem)
    bodies = []
    for _, locs, code in funcs:
        bodies.append(vec([leb(c) + bytes([t]) for c, t in locs]) + code + b"\x0b")
    return out + sec(10, vec([leb(len(b)) + b for b in bodies]))


def fn(name_: str, i: int = 0) -> tuple[str, int, int]:
    return (name_, 0, i)


def engines():
    out = [TargetEngine("pyc")]
    try:
        from tests.spec.wasmtime_engine import WasmtimeEngine
    except ImportError:  # the oracle is a development tool
        return out
    return out + [WasmtimeEngine()]


def run(wasm: bytes, export: str, *args: int) -> list[Any]:
    """The results of `export(*args)` on `pyc` and, if installed, on Wasmtime; they must agree."""
    res = []
    for eng in engines():
        inst = eng.instantiate(eng.load(wasm), {})
        res.append(eng.call(inst, export, list(args)))
    assert all(r == res[0] for r in res), res
    return res[0]


A = int.from_bytes(bytes(range(1, 17)), "little")
B = int.from_bytes(bytes(range(101, 117)), "little")


def test_v128_param_and_result_through_an_export() -> None:
    w = module([ty(V + V, V)], [(0, [], get(0) + get(1) + fd(0xAE))], [fn("add")])
    la, lb = (
        struct.unpack("<4I", A.to_bytes(16, "little")),
        struct.unpack("<4I", B.to_bytes(16, "little")),
    )
    want = struct.pack("<4I", *[(x + y) & 0xFFFFFFFF for x, y in zip(la, lb, strict=True)])
    assert run(w, "add", A, B) == [int.from_bytes(want, "little")]


def test_v128_through_a_host_function() -> None:
    # (import "env" "h" (func (param v128 i32) (result v128))); run(v) = h(v, 3)
    w = module(
        [ty(V + I32, V), ty(V, V)],
        [(1, [], get(0) + i32c(3) + b"\x10\x00")],
        [fn("run", 1)],
        imports=[name("env") + name("h") + b"\x00\x00"],
    )
    seen = []

    def h(args):
        seen.append(args)
        return [args[0] ^ args[1]]

    mod = parse(w)
    validate(mod)
    target = base.select(mod, base.target_order("pyc"))
    inst = target.instantiate(
        target.compile(mod), link.resolve(mod, {"env": {"h": h}}), StoreState()
    )
    assert inst.call(1, [A]) == [A ^ 3]
    assert seen == [[A, 3]]


def test_v128_globals_across_modules() -> None:
    types = [ty(b"", V), ty(V, b"")]
    gdef = V + b"\x01" + const(A) + b"\x0b"
    a = module(
        types,
        [(0, [], b"\x23\x00")],
        [("g", 3, 0), fn("get")],
        globals_=[gdef],
    )
    imp = name("A") + name("g") + b"\x03" + V + b"\x01"
    b = module(
        types,
        [(0, [], b"\x23\x00"), (1, [], get(0) + b"\x24\x00")],
        [fn("read"), fn("write", 1)],
        imports=[imp],
    )
    for eng in engines():
        ia = eng.instantiate(eng.load(a), {})
        ib = eng.instantiate(eng.load(b), {"A": ia})
        assert eng.get_global(ia, "g") == A
        assert eng.call(ib, "read", []) == [A]
        eng.call(ib, "write", [B])
        assert eng.call(ia, "get", []) == [B] and eng.get_global(ia, "g") == B


def test_immutable_v128_global_import() -> None:
    a = module(
        [ty(b"", b"")], [(0, [], b"")], [("g", 3, 0)], globals_=[V + b"\x00" + const(A) + b"\x0b"]
    )
    b = module(
        [ty(b"", V)],
        [(0, [], b"\x23\x00")],
        [fn("read")],
        imports=[name("A") + name("g") + b"\x03" + V + b"\x00"],
    )
    for eng in engines():
        ia = eng.instantiate(eng.load(a), {})
        assert eng.call(eng.instantiate(eng.load(b), {"A": ia}), "read", []) == [A]


BT = {"block": b"\x02", "loop": b"\x03"}


def test_block_params_and_results() -> None:
    # block (param v128 v128) (result v128): type 1; loop (result v128); block (result v128) + br
    t = [ty(V + V, V), ty(V + V, V)]
    body = get(0) + get(1) + b"\x02\x01" + fd(0xAE) + b"\x0b"
    assert run(module(t, [(0, [], body)], [fn("f")]), "f", A, B) == run(
        module([ty(V + V, V)], [(0, [], get(0) + get(1) + fd(0xAE))], [fn("f")]), "f", A, B
    )
    loop = b"\x03\x7b" + const(A) + b"\x0b"
    assert run(module([ty(b"", V)], [(0, [], loop)], [fn("f")]), "f") == [A]
    blk = b"\x02\x7b" + const(B) + b"\x0c\x00" + const(A) + b"\x0b"
    assert run(module([ty(b"", V)], [(0, [], blk)], [fn("f")]), "f") == [B]


def test_if_result_v128_with_and_without_else() -> None:
    body = get(0) + b"\x04\x7b" + const(A) + b"\x05" + const(B) + b"\x0b"
    w = module([ty(I32, V)], [(0, [], body)], [fn("f")])
    assert run(w, "f", 1) == [A] and run(w, "f", 0) == [B]
    # if (param v128) (result v128) with no else: the parameter passes through when false
    body = get(1) + get(0) + b"\x04\x01" + const(A) + fd(0x4E) + b"\x0b"
    w = module([ty(I32 + V, V), ty(V, V)], [(0, [], body)], [fn("f")])
    assert run(w, "f", 1, B) == [A & B] and run(w, "f", 0, B) == [B]


def test_only_a_block_type_mentions_v128() -> None:
    # if (0) { block (result v128) unreachable end; drop }; 7
    body = i32c(0) + b"\x04\x40\x02\x7b\x00\x0b\x1a\x0b" + i32c(7)
    assert run(module([ty(b"", I32)], [(0, [], body)], [fn("f")]), "f") == [7]


def test_select_typed_untyped_and_drop() -> None:
    sel = get(1) + get(2) + get(0) + b"\x1b"
    w = module([ty(I32 + V + V, V)], [(0, [], sel)], [fn("f")])
    assert run(w, "f", 1, A, B) == [A] and run(w, "f", 0, A, B) == [B]
    sel = get(1) + get(2) + get(0) + b"\x1c\x01\x7b"
    w = module([ty(I32 + V + V, V)], [(0, [], sel)], [fn("f")])
    assert run(w, "f", 1, A, B) == [A] and run(w, "f", 0, A, B) == [B]
    w = module([ty(V + V, V)], [(0, [], get(0) + get(1) + b"\x1a")], [fn("f")])
    assert run(w, "f", A, B) == [A]


def test_v128_local_starts_at_zero_and_call_indirect() -> None:
    t = [ty(V, V), ty(I32 + V, V)]
    # f0 = (x) -> x xor 1 ; f1 (i, x) = call_indirect (type 0) x i ; f2 () = local 0 (a v128 local)
    f0 = get(0) + const(1) + fd(0x51)
    f1 = get(1) + get(0) + b"\x11\x00\x00"
    f2 = get(0)
    w = module(
        t + [ty(b"", V)],
        [(0, [], f0), (1, [], f1), (2, [(1, 0x7B)], f2)],
        [fn("f0"), fn("ci", 1), fn("zero", 2)],
        tables=b"\x01\x70\x00\x01",
        elem=b"\x01\x00\x41\x00\x0b\x01\x00",
    )
    assert run(w, "ci", 0, A) == [A ^ 1]
    assert run(w, "zero") == [0]


def _lanes32(v: Any) -> tuple[int, ...]:
    return struct.unpack("<4I", v.to_bytes(16, "little"))


def _is_quiet_nan32(x: int) -> bool:
    return x & 0x7FC00000 == 0x7FC00000


@x87_quiets_snan
def test_nan_lanes() -> None:
    snan = 0x7FA00000
    v = int.from_bytes(struct.pack("<4I", snan, 0x3F800000, 0xFFFFFFFF, 0x7FC00001), "little")
    # data movement and sign operators keep the bits, signalling NaN included
    neg = module([ty(V, V)], [(0, [], get(0) + fd(0xE1))], [fn("f")])
    assert _lanes32(run(neg, "f", v)[0]) == (snan ^ 0x80000000, 0xBF800000, 0x7FFFFFFF, 0xFFC00001)
    splat = module([ty(F32, V)], [(0, [], get(0) + fd(0x13))], [fn("f")])
    assert run(splat, "f", snan) == [snan * (1 + (1 << 32) + (1 << 64) + (1 << 96))]
    # pmin / pmax return an input unchanged
    pmin = module([ty(V + V, V)], [(0, [], get(0) + get(1) + fd(0xEA))], [fn("f")])
    one = int.from_bytes(struct.pack("<4I", *[0x3F800000] * 4), "little")
    assert _lanes32(run(pmin, "f", v, one)[0])[0] == snan
    # arithmetic on a NaN lane gives a quiet NaN; the other lanes are exact
    add = module([ty(V + V, V)], [(0, [], get(0) + get(1) + fd(0xE4))], [fn("f")])
    out = _lanes32(run_pyc(add, "f", v, one))
    assert _is_quiet_nan32(out[0]) and out[1] == 0x40000000 and _is_quiet_nan32(out[3])
    mx = module([ty(V + V, V)], [(0, [], get(0) + get(1) + fd(0xE9))], [fn("f")])
    out = _lanes32(run_pyc(mx, "f", v, one))
    assert out[0] & 0x7F800000 == 0x7F800000 and out[0] & 0x7FFFFF and out[1] == 0x3F800000


def run_pyc(wasm: bytes, export: str, *args: int) -> Any:
    eng = TargetEngine("pyc")
    return eng.call(eng.instantiate(eng.load(wasm), {}), export, list(args))[0]


@pytest.mark.parametrize("sub", [0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9])
def test_f32x4_binary_matches_wasmtime_off_nan(sub: int) -> None:
    a = int.from_bytes(struct.pack("<4f", 1.5, -0.0, 3.0e38, 2.5), "little")
    b = int.from_bytes(struct.pack("<4f", -2.25, 1.0, 3.0e38, 0.0), "little")
    w = module([ty(V + V, V)], [(0, [], get(0) + get(1) + fd(sub))], [fn("f")])
    run(w, "f", a, b)  # raises if the two engines differ
