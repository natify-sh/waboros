# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownVariableType=false
import os

import pytest

from w2py.targets.base import TrapCode
from w2py.targets.pyc import Trap, load, translate

COREMARK = os.path.join(os.path.dirname(__file__), "..", "poc", "in", "core.wasm")

# (module (func (export "div") (param i32 i32) (result i32) local.get 0 local.get 1 i32.div_s))
DIV = bytes.fromhex(
    "0061736d0100000001070160027f7f017f030201000707010364697600000a09010700200020016d0b"
)


def test_tiny_module_call_and_trap() -> None:
    ex = load(DIV, {})
    assert ex["div"](7, 2) == 3
    with pytest.raises(Trap):
        ex["div"](1, 0)


def test_generated_source_imports_runtime() -> None:
    src = translate(DIV)
    assert "from w2py.targets.pyc.runtime import" in src
    assert "class Trap" not in src


@pytest.mark.skipif(not os.path.exists(COREMARK), reason="poc/in/core.wasm missing")
def test_coremark_translates_and_compiles() -> None:
    with open(COREMARK, "rb") as f:
        src = translate(f.read())
    compile(src, "<coremark>", "exec")


HEAD = "0061736d01000000"
# A: (memory (export "m") 1) (global (export "g") (mut i32) (i32.const 1))
SHARE_A = HEAD + "05030100" + "01" + "0606017f0141010b" + "070902016d0200" + "01670300"
# B: imports A.m and A.g (mut i32); (func (export "f") (result i32)
#   (i32.store8 (i32.const 0) (i32.const 7)) (global.set 0 (i32.const 9))
#   (i32.load8_u (i32.const 0)))
SHARE_B = (
    HEAD
    + "0105016000017f"
    + "020f02"
    + "0141016d020001"
    + "014101"
    + "67037f01"
    + "03020100"
    + "07050101660000"
    + "0a14011200"
    + "41004107"
    + "3a0000"
    + "410924"
    + "00"
    + "4100"
    + "2d0000"
    + "0b"
)


def test_shared_memory_and_mutable_global_between_instances() -> None:
    a = load(bytes.fromhex(SHARE_A), {})
    b = load(bytes.fromhex(SHARE_B), {"A": {"m": a["m"], "g": a["g"]}})
    assert b["f"]() == 7
    assert a["m"][0] == 7 and a["g"].get() == 9


# (module (type (func (param i32) (result i32))) (type (func (param i32 i32) (result i32)))
#  (func (export "f") (param i32) (result i32) (local i32 i32) ;; sum 0..n, loop with params
#    local.get 0 i32.const 0 loop (type 1) local.set 1 local.set 2
#      local.get 2 i32.const 1 i32.sub  local.get 1 local.get 2 i32.add  local.get 2 br_if 0
#      local.set 1 drop local.get 1 end)
#  (func (export "g") (param i32 i32) (result i32)               ;; if with a param on each arm
#    local.get 0 local.get 1 if (type 0) i32.const 10 i32.add else i32.const 20 i32.sub end))
BLOCK_PARAMS = bytes.fromhex(
    HEAD
    + "010c02" + "60017f017f" + "60027f7f017f"
    + "03030200" + "01"
    + "07090201660000" + "01670001"
    + "0a3502" + "22" + "01027f" + "2000410003012101210220024101" + "6b" + "20012002" + "6a"
    + "20020d00" + "21011a2001" + "0b0b"
    + "10" + "00" + "20002001" + "0400" + "410a6a" + "05" + "41146b" + "0b0b"
)  # fmt: skip


def test_block_params_loop_and_if() -> None:
    ex = load(BLOCK_PARAMS, {})
    assert ex["f"](4) == 10
    assert ex["f"](0) == 0
    assert ex["g"](5, 1) == 15
    assert ex["g"](5, 0) == 0xFFFFFFF1


def _tail_call_module() -> bytes:
    def fn(ret: int, tail: str) -> str:
        return f"00200045044041{ret:02x}0f0b200041016b" + tail + "0b"

    code = "04" + "".join(
        f"{len(b) // 2:02x}{b}"
        for b in (fn(7, "1200"), fn(1, "1202"), fn(0, "1201"), fn(9, "4100130000"))
    )
    return bytes.fromhex(
        "0061736d01000000"
        "0106"
        "0160017f017f"
        "0305"
        "0400000000"
        "0404"
        "01700001"
        "070d"
        "03"
        "01630000"
        "01650001"
        "01690003"
        "0907"
        "01"
        "004100"
        "0b"
        "0103"
        "0a%02x" % (len(code) // 2) + code
    )


def test_tail_calls_run_in_constant_stack() -> None:
    ex = load(_tail_call_module(), {})
    assert ex["c"](1_000_000) == 7  # self return_call
    assert ex["e"](1_000_000) == 1  # mutual even/odd
    assert ex["e"](100_001) == 0
    assert ex["i"](100_000) == 9  # return_call_indirect


def _wasm_module() -> bytes:
    def sec(i: int, body: bytes) -> bytes:
        return bytes([i, len(body)]) + body

    def vec(items: list[bytes]) -> bytes:
        return bytes([len(items)]) + b"".join(items)

    types = vec([b"\x60\x00\x01\x7f", b"\x60\x01\x7f\x01\x7f", b"\x60\x00\x00"])
    funcs = bytes([6, 0, 2, 1, 1, 0, 1])  # f0 t0, f1 t2, f2 t1, f3 t1, f4 t0, f5 t1
    table = vec([b"\x70\x01\x02\x05"])  # funcref, min 2, max 5
    names = ["init", "isnull", "grow", "size", "call"]
    exports = vec([bytes([len(n)]) + n.encode() + bytes([0, i + 1]) for i, n in enumerate(names)])
    elem = vec([b"\x01\x00\x01\x00"])  # passive segment: func 0
    bodies = [
        b"\x41\x07",  # i32.const 7
        b"\x41\x01\x41\x00\x41\x01\xfc\x0c\x00\x00",  # table.init: segment 0, table 0, (1, 0, 1)
        b"\x20\x00\x25\x00\xd1",  # table.get, ref.is_null
        b"\xd0\x70\x20\x00\xfc\x0f\x00",  # table.grow with null
        b"\xfc\x10\x00",  # table.size
        b"\x20\x00\x11\x00\x00",  # call_indirect (type 0) table 0
    ]
    code = vec([bytes([len(b) + 2, 0]) + b + b"\x0b" for b in bodies])
    return (
        b"\0asm\1\0\0\0"
        + sec(1, types)
        + sec(3, funcs)
        + sec(4, table)
        + sec(7, exports)
        + sec(9, elem)
        + sec(10, code)
    )


def test_table_init_get_grow_size_and_bounds() -> None:
    ex = load(_wasm_module(), {})
    assert ex["isnull"](1) == 1
    with pytest.raises(Trap):
        ex["call"](1)  # null element
    ex["init"]()
    assert ex["isnull"](1) == 0
    assert ex["call"](1) == 7
    with pytest.raises(Trap):
        ex["isnull"](2)  # out of bounds table.get
    assert ex["size"]() == 2
    assert ex["grow"](3) == 2
    assert ex["grow"](1) == 0xFFFFFFFF  # above the maximum of 5
    assert ex["size"]() == 5


def test_extended_const_global_initializer() -> None:
    # (global (export "g") i32 (i32.add (i32.const 40) (i32.const 2)))
    ex = load(
        bytes.fromhex(HEAD + "060901" + "7f00" + "41284102" + "6a0b" + "07050101" + "6703" + "00"),
        {},
    )
    assert ex["g"].get() == 42


def test_dropped_out_of_bounds_load_traps() -> None:
    # (memory 1) (func (export "f") (param i32)
    #   (drop (i32.load8_u offset=4294967295 (local.get 0))))
    mod = HEAD + "0105016001" + "7f00" + "03020100" + "05030100" + "01" + "07050101" + "6600" + "00"
    mod += "0a0e010c00" + "20002d00ffffffff0f1a0b"
    with pytest.raises(Trap):
        load(bytes.fromhex(mod), {})["f"](0)


# (func (export "f") (param i32) (result i32)
#   local.get 0 f32.reinterpret_i32 f32.neg i32.reinterpret_f32)
NEG32 = bytes.fromhex(
    "0061736d0100000001060160017f017f03020100" + "07050101660000" + "0a0901070020" + "00be8cbc0b"
)


def test_f32_nan_payload_kept() -> None:
    from w2py.targets.pyc import runtime as rt

    assert load(NEG32, {})["f"](0x7FA00001) == 0xFFA00001  # sNaN stays signalling, payload kept
    assert rt._i32_f32(rt._f32_i32(0x7FA00000)) == 0x7FA00000
    assert rt._i64_f64(rt._f64_i64(0x7FF4000000000001)) == 0x7FF4000000000001
    m = bytearray(8)
    rt.F32S(m, 0, rt._f32_i32(0xFFA00003))
    assert rt._i32_f32(rt._f32_i32(rt.U32(m, 0)[0])) == 0xFFA00003 == rt.U32(m, 0)[0]


def test_float_rounding_helpers() -> None:
    from w2py.targets.pyc import runtime as rt

    assert str(rt._fceil(-0.5)) == "-0.0" and str(rt._ftrunc(-0.5)) == "-0.0"
    assert rt._i64_f64(rt._fnearest(rt._f64_i64(0x7FF4000000000000))) == 0x7FFC000000000000
    assert rt._i64_f64(rt._f64_i64(0x7FF4000000000000) * 1.0) == 0x7FFC000000000000  # promote
    # 2^53 + 2^29 + 1 is just above a f32 tie: single rounding goes up, double rounding down
    x = (1 << 53) + (1 << 29) + 1
    assert rt._i64_f32(x) == float((1 << 53) + (1 << 30))
    assert rt._i64_f32(-x) == -float((1 << 53) + (1 << 30))


def test_discarded_operands_keep_their_traps_and_effects() -> None:
    # (func (export "f") (param i32 i32) local.get 0 local.get 1 i32.div_s drop)
    div = (
        HEAD
        + "010601"
        + "60027f7f00"
        + "03020100"
        + "07050101660000"
        + "0a0a0108"
        + "00200020016d1a0b"
    )
    with pytest.raises(Trap):
        load(bytes.fromhex(div), {})["f"](1, 0)
    # (func (export "f") (param i32 i32) (result i32)
    #   i32.const 1 (i32.div_s (local.get 0) (local.get 1)) i32.const 1 select)  ;; arm not selected
    sel = HEAD + "0107016002" + "7f7f017f" + "03020100" + "07050101660000" + "0a0e010c"
    sel += "00410120002001" + "6d41011b0b"
    with pytest.raises(Trap):
        load(bytes.fromhex(sel), {})["f"](1, 0)
    # (global (mut i32)) (func $h (result i32) i32.const 9 global.set 0 i32.const 0)
    # (func (export "f") (result i32) call $h drop)  ;; the dropped call still runs
    call = HEAD + "010501" + "60000" + "17f" + "03030200" + "00" + "0606017f0141000b"
    call += (
        "070902016600"
        + "01"
        + "01670300"
        + "0a1202"
        + "08004109240041000b"
        + "07"
        + "0010001a41000b"
    )
    ex = load(bytes.fromhex(call), {})
    ex["f"]()
    assert ex["g"].get() == 9


# (type $t (func (param i32) (result i32))) with call_ref / return_call_ref / ref.as_non_null /
# br_on_null / br_on_non_null on (ref $t) and (ref null $t)
TYPED_REFS = bytes.fromhex(
    "0061736d0100000001100360017f017f6000017f60016300017f030807000001020002020730060463616c6c"
    "0001046e756c6c0002026e6e0003047461696c00040662726e756c6c00050962726e6f6e6e756c6c00060905"
    "01030001000a51070700200041016a0b08002000d20014000b08004101d00014000b090041012000d414000b"
    "08002000d20015000b1000024041052000d50014000f0b417f0b110041050264002000d600417f0f0b14000b"
    "001e046e616d65010401000166030b02050100016c060100016c040401000174"
)


def test_typed_function_references() -> None:
    ex = load(TYPED_REFS, {})
    assert ex["call"](4) == 5
    assert ex["tail"](9) == 10
    f = ex["call"]
    assert ex["nn"](f) == 2
    assert ex["brnull"](f) == 6
    assert ex["brnull"](None) == 0xFFFFFFFF
    assert ex["brnonnull"](f) == 6
    assert ex["brnonnull"](None) == 0xFFFFFFFF
    with pytest.raises(Trap, match="null function reference"):
        ex["null"]()
    with pytest.raises(Trap, match="null reference"):
        ex["nn"](None)


# memory.init / data.drop, and an `if` with params but no else
# (module (memory 1) (data $p "abcd") (data (i32.const 0) "xy") ... init, drop, dropa, get, pid)
DATA = bytes.fromhex(
    "0061736d0100000001160460037f7f7f0060000060017f017f60027f7f027f7f0306050001010202"
    "050301000107230504696e697400000464726f7000010564726f7061000203676574000303706964"
    "00040c01020a2f050c00200020012002fc0800000b0500fc09000b0500fc09010b070020002d0000"
    "0b0c0041014102200004030b6a0b0b0e020104616263640041000b027879000b046e616d65090401"
    "000170"
)


def test_memory_init_data_drop_and_if_params_without_else() -> None:
    ex = load(DATA, {})
    assert ex["get"](0) == ord("x")
    assert ex["pid"](0) == 3
    ex["init"](10, 1, 3)
    assert [ex["get"](i) for i in (10, 11, 12)] == [ord("b"), ord("c"), ord("d")]
    with pytest.raises(Trap, match="out of bounds memory access"):
        ex["init"](20, 2, 3)  # source runs past the segment
    with pytest.raises(Trap):
        ex["init"](65535, 0, 2)  # destination runs past memory
    assert ex["get"](65535) == 0  # nothing written before the trap
    ex["drop"]()
    ex["init"](30, 0, 0)  # a dropped segment has length 0
    with pytest.raises(Trap):
        ex["init"](30, 0, 1)
    ex["dropa"]()  # an active segment is dropped after instantiation
    with pytest.raises(Trap):
        ex["init"](30, 0, 1)


# Values below the arity of br / br_if / br_table / return, and before `unreachable`, are discarded
# but still evaluated: a trapping load or division runs. In order: brif (load), brif2 (div),
# brtable, ret, ret2, unr, unr2, unr3; each takes one i32.
DISCARD = bytes.fromhex(
    "0061736d0100000001060160017f017f03090800000000000000000503010001073b080462726966"
    "000005627269663200010762727461626c6500020372657400030472657432000403756e72000504"
    "756e7232000604756e723300070a76081100027f417f280200410520000d000f0b0b1100027f4101"
    "20006e410520000d006a0b0b1200027f417f280200410520000e0100000b0b0a00417f2802004105"
    "0f0b0a00410120006e41050f0b0f00027f417f28020041050c006a0b0b0800410120006e000b0e00"
    "027f410120006e41050c000b0b"
)


@pytest.mark.parametrize(
    "name, arg, ok",
    [
        ("brif", 1, False),
        ("brif", 0, False),
        ("brif2", 1, True),
        ("brif2", 0, False),
        ("brtable", 0, False),
        ("brtable", 3, False),
        ("ret", 0, False),
        ("ret2", 0, False),
        ("ret2", 1, True),
        ("unr", 0, False),
        ("unr2", 0, False),
        ("unr3", 0, False),
        ("unr3", 1, True),
    ],
)
def test_discarded_values_that_may_trap_still_run(name: str, arg: int, ok: bool) -> None:
    ex = load(DISCARD, {})
    if ok:
        assert ex[name](arg) == 5
    else:
        with pytest.raises(Trap):
            ex[name](arg)


def _effect_before_trap(wat: str, imports: dict[str, dict[str, object]] | None = None) -> object:
    from wasmtime import wat2wasm

    return load(wat2wasm(wat), imports or {})  # pyright: ignore[reportArgumentType]


def test_trap_precedes_later_call() -> None:
    seen: list[int] = []
    ex = _effect_before_trap(
        '(module (import "env" "host" (func $host (param i32)))'
        ' (func (export "f") (param i32) (result i32)'
        "  (block (result i32) (i32.div_s (local.get 0) (i32.const 0))"
        " (call $host (local.get 0)))))",
        {"env": {"host": seen.append}},
    )
    with pytest.raises(Trap):
        ex["f"](5)  # pyright: ignore[reportIndexIssue]
    assert seen == []


def test_trap_load_precedes_call() -> None:
    seen: list[int] = []
    ex = _effect_before_trap(
        '(module (import "env" "host" (func $host (param i32))) (memory 1)'
        ' (func (export "f") (param i32) (result i32)'
        "  (block (result i32) (i32.load (local.get 0)) (call $host (local.get 0)))))",
        {"env": {"host": seen.append}},
    )
    with pytest.raises(Trap):
        ex["f"](0x7FFFFFFF)  # pyright: ignore[reportIndexIssue]
    assert seen == []


def test_trap_rem_precedes_global_set() -> None:
    ex = _effect_before_trap(
        '(module (global $g (mut i32) (i32.const 1)) (func (export "f") (param i64)'
        "  (block (result i64) (i64.rem_s (local.get 0) (i64.const 0))"
        " (global.set $g (i32.const 9))) drop)"
        ' (func (export "g") (result i32) (global.get $g)))'
    )
    with pytest.raises(Trap):
        ex["f"](5)  # pyright: ignore[reportIndexIssue]
    assert ex["g"]() == 1  # pyright: ignore[reportIndexIssue]


def test_trap_trunc_precedes_store() -> None:
    ex = _effect_before_trap(
        '(module (memory 1) (func (export "f") (param f32)'
        "  (block (result i32) (i32.trunc_f32_s (local.get 0))"
        " (i32.store8 (i32.const 0) (i32.const 7))) drop)"
        ' (func (export "r") (result i32) (i32.load8_u (i32.const 0))))'
    )
    with pytest.raises(Trap):
        ex["f"](float("nan"))  # pyright: ignore[reportIndexIssue]
    assert ex["r"]() == 0  # pyright: ignore[reportIndexIssue]


def _raw_instance(wat: str, imports: dict[str, dict[str, object]]) -> dict[str, object]:
    """Instantiate without the `load` entry wrappers, so exports keep their `.t` bodies (as when
    two modules are linked in one store)."""
    from wasmtime import wat2wasm

    ns: dict[str, object] = {"__name__": "w2py_pyc_module"}
    exec(compile(translate(bytes(wat2wasm(wat))), "<w2py-pyc>", "exec"), ns)
    return ns["instantiate"](imports)  # pyright: ignore[reportCallIssue]


def test_return_call_through_import_runs_in_constant_stack() -> None:
    b = _raw_instance(
        """(module
      (type $p (func (param i32) (result i32)))
      (table $t (export "t") 1 funcref)
      (func $pong (export "pong") (type $p)
        (if (result i32) (i32.eqz (local.get 0)) (then (i32.const 222))
          (else (return_call_indirect $t (type $p) (i32.sub (local.get 0) (i32.const 1))
                                      (i32.const 0))))))""",
        {},
    )
    a = _raw_instance(
        """(module
      (type $p (func (param i32) (result i32)))
      (import "b" "pong" (func $pong (type $p)))
      (import "b" "t" (table $t 1 funcref))
      (elem (table $t) (i32.const 0) func $ping)
      (func $ping (export "ping") (type $p)
        (if (result i32) (i32.eqz (local.get 0)) (then (i32.const 111))
          (else (return_call $pong (i32.sub (local.get 0) (i32.const 1)))))))""",
        {"b": b},  # pyright: ignore[reportArgumentType]
    )
    assert a["ping"](1_000_000) == 111  # pyright: ignore[reportCallIssue]


def test_return_call_to_host_import_and_multi_value() -> None:
    def host(x: int) -> int:
        if x == 99:
            raise Trap(TrapCode.UNREACHABLE, "boom")
        if x == 98:
            raise KeyError("host")
        return x + 1

    def mv(a: int, b: float, c: float) -> tuple[int, int, float, float]:
        return (7, a + 1, b, c * 2)

    ex = _effect_before_trap(
        """(module
      (import "h" "inc" (func $inc (param i32) (result i32)))
      (import "h" "mv" (func $mv (param i64 f32 f64) (result i32 i64 f32 f64)))
      (func (export "f") (param i32) (result i32) (return_call $inc (local.get 0)))
      (func (export "g") (param i64 f32 f64) (result i32 i64 f32 f64)
        (return_call $mv (local.get 0) (local.get 1) (local.get 2))))""",
        {"h": {"inc": host, "mv": mv}},
    )
    assert ex["f"](4) == 5  # pyright: ignore[reportIndexIssue,reportCallIssue]
    with pytest.raises(Trap, match="boom"):
        ex["f"](99)  # pyright: ignore[reportIndexIssue,reportCallIssue]
    with pytest.raises(KeyError):
        ex["f"](98)  # pyright: ignore[reportIndexIssue,reportCallIssue]
    assert ex["g"](1, 1.5, 2.0) == (7, 2, 1.5, 4.0)  # pyright: ignore[reportIndexIssue,reportCallIssue]


def _sec(sid: int, body: bytes) -> bytes:
    return bytes([sid, len(body)]) + body


def _vec(items: list[bytes]) -> bytes:
    return bytes([len(items)]) + b"".join(items)


def _fn(code: bytes) -> bytes:
    return bytes([len(code) + 1, 0]) + code


def _mm_module(
    imports: bytes,
    mems: list[bytes],
    funcs: list[tuple[str, int, bytes]],
    rest: bytes = b"",
    memex: tuple[tuple[str, int], ...] = (),
):
    """Types: 0 (i32)->i32, 1 (i32 i32)->(), 2 (i32 i32 i32)->(), 3 ()->i32."""
    types = [b"\x60\x01\x7f\x01\x7f", b"\x60\x02\x7f\x7f\x00"]
    types += [b"\x60\x03\x7f\x7f\x7f\x00", b"\x60\x00\x01\x7f"]
    ex = [bytes([len(n)]) + n.encode() + b"\x00" + bytes([i]) for i, (n, _, _) in enumerate(funcs)]
    ex += [bytes([len(n)]) + n.encode() + b"\x02" + bytes([i]) for n, i in memex]
    return (
        bytes.fromhex(HEAD)
        + _sec(1, _vec(types))
        + imports
        + _sec(3, _vec([bytes([t]) for _, t, _ in funcs]))
        + _sec(5, _vec(mems))
        + _sec(7, _vec(ex))
        + rest
        + _sec(10, _vec([_fn(c) for _, _, c in funcs]))
    )


_G = b"\x20\x00"  # local.get 0
_G3 = b"\x20\x00\x20\x01\x20\x02"
# three memories: m0 1 page, m1 1 page (active data "xy" at 0), m2 1..3 pages; passive data "abcd"
MULTI = _mm_module(
    b"",
    [b"\x00\x01", b"\x00\x01", b"\x01\x01\x03"],
    [
        ("cp", 2, _G3 + b"\xfc\x0a\x01\x02\x0b"),  # memory.copy dst m1, src m2
        ("get1", 0, _G + b"\x28\x41\x01\x00\x0b"),
        ("get2", 0, _G + b"\x28\x42\x02\x00\x0b"),
        ("grow2", 0, _G + b"\x40\x02\x0b"),
        ("size2", 3, b"\x3f\x02\x0b"),
        ("fill2", 2, _G3 + b"\xfc\x0b\x02\x0b"),
        ("init2", 2, _G3 + b"\xfc\x08\x01\x02\x0b"),
        ("set2", 1, _G + b"\x20\x01\x36\x42\x02\x00\x0b"),
    ],
    _sec(12, b"\x02"),
    (("m1", 1),),
) + _sec(11, _vec([b"\x02\x01\x41\x00\x0b\x02xy", b"\x01\x04abcd"]))

# imports A.m1 as memory 0 and defines its own memory 1; stores 7 into m0, 9 into m1, loads m1
MULTI_B = _mm_module(
    _sec(2, _vec([b"\x01A\x02m1\x02\x00\x01"])),
    [b"\x00\x01"],
    [
        (
            "w",
            3,
            b"\x41\x00\x41\x07\x3a\x40\x00\x00\x41\x00\x41\x09\x3a\x40\x01\x00"
            b"\x41\x00\x2d\x40\x01\x00\x0b",
        )
    ],
)


def test_three_memories() -> None:
    ex = load(MULTI, {})
    assert ex["get1"](0) == 0x7978  # the active segment went to memory 1
    ex["set2"](0, 0x11223344)
    assert ex["get2"](0) == 0x11223344 and ex["get1"](0) == 0x7978
    ex["cp"](0, 0, 4)
    assert ex["get1"](0) == 0x11223344
    assert ex["size2"]() == 1 and ex["grow2"](1) == 1 and ex["size2"]() == 2
    assert ex["grow2"](5) & 0xFFFFFFFF == 0xFFFFFFFF  # past the maximum of 3
    ex["fill2"](0, 0x55, 2)
    assert ex["get2"](0) == 0x11225555
    ex["init2"](8, 0, 4)
    assert ex["get2"](8) == 0x64636261
    with pytest.raises(Trap):
        ex["cp"](0, 0x20000 - 2, 4)  # source beyond memory 2 (2 pages)


def test_two_instances_share_an_imported_second_memory() -> None:
    a = load(MULTI, {})
    b = load(MULTI_B, {"A": {"m1": a["m1"]}})
    assert b["w"]() == 9
    assert a["get1"](0) == 0x7907  # B's store of 7 at address 0 of its memory 0 landed in A's m1
    assert len(a["m1"]) == 65536
