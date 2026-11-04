# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportArgumentType=false
"""S0009 step 1: memory64 and table64 in the front end, the validator, `pyc`, JIT `unsupported`."""

import json
from typing import Any

import pytest

from tests.spec import fetch
from waboros.frontend.binary import Malformed, parse
from waboros.frontend.validate import Invalid, validate
from waboros.targets import base
from waboros.targets.base import Trap, TrapCode
from waboros.targets.pyc import load

M64 = 0xFFFFFFFFFFFFFFFF


def sec(i: int, body: bytes) -> bytes:
    return bytes([i, len(body)]) + body


def module(*parts: bytes) -> bytes:
    return b"\0asm\1\0\0\0" + b"".join(parts)


# (func (param i64) (result i32)) and (func (param i64) (result i64)); a body is `locals code end`
TYPES = sec(1, b"\x02\x60\x01\x7e\x01\x7f\x60\x01\x7e\x01\x7e")
LD, GROW = b"\x00\x20\x00\x2d\x00\x00\x0b", b"\x00\x20\x00\x40\x00\x0b"


def mem64(limits: bytes, data: bytes = b"") -> bytes:
    """One i64 memory; "ld" (i32.load8_u at an i64 address), "grow", "mem" exported."""
    exports = b"\x03\x02ld\x00\x00\x04grow\x00\x01\x03mem\x02\x00"
    code = b"\x02" + bytes([len(LD)]) + LD + bytes([len(GROW)]) + GROW
    return module(
        TYPES,
        sec(3, b"\x02\x00\x01"),
        sec(5, b"\x01" + limits),
        sec(7, exports),
        sec(10, code),
        data,
    )


def check(wasm: bytes) -> dict[str, Any]:
    mod = parse(wasm)
    validate(mod)
    return mod


def test_limits_flags_set_the_address_types() -> None:
    m = check(mem64(b"\x05\x01\x04"))
    assert m["maddr"] == [0x7E] and m["mems"] == [(1, 4)]
    m = check(mem64(b"\x04\x00"))
    assert m["maddr"] == [0x7E] and m["mems"] == [(0, None)]
    assert check(module(sec(5, b"\x02\x00\x01\x01\x01\x02")))["maddr"] == [0x7F, 0x7F]
    t = check(module(sec(4, b"\x01\x70\x04\x03")))
    assert t["taddr"] == [0x7E] and t["tables"] == [(3, None)]


@pytest.mark.parametrize("flag", [2, 3, 6, 7])
def test_shared_limits_stay_unimplemented(flag: int) -> None:
    with pytest.raises(NotImplementedError):
        parse(module(sec(5, bytes([1, flag, 1, 1]))))


def test_limits_flags_from_8_are_malformed() -> None:
    with pytest.raises(Malformed):
        parse(module(sec(5, b"\x01\x08\x01")))


def _body(code: bytes, params: bytes = b"") -> tuple[bytes, bytes, bytes]:
    ty = sec(1, b"\x01\x60" + bytes([len(params)]) + params + b"\x00")
    body = b"\x00" + code + b"\x0b"
    return ty, sec(3, b"\x01\x00"), sec(10, b"\x01" + bytes([len(body)]) + body)


I64MEM, I32MEM = sec(5, b"\x01\x04\x01"), sec(5, b"\x01\x00\x01")


@pytest.mark.parametrize(
    "mem,code,ok",
    [
        (I64MEM, b"\x42\x00\x2d\x00\x00\x1a", True),  # i64 address
        (I64MEM, b"\x41\x00\x2d\x00\x00\x1a", False),  # i32 address on an i64 memory
        (I32MEM, b"\x42\x00\x2d\x00\x00\x1a", False),  # i64 address on an i32 memory
        (I64MEM, b"\x42\x00\x2d\x00\x80\x80\x80\x80\x10\x1a", True),  # offset 2**32 on i64
        (I32MEM, b"\x41\x00\x2d\x00\x80\x80\x80\x80\x10\x1a", False),  # and not on i32
        (I64MEM, b"\x3f\x00\x1a\x42\x00\x40\x00\x1a", True),  # size, grow: [i64] -> [i64]
        (I64MEM, b"\x41\x00\x40\x00\x1a", False),
        (I64MEM, b"\x42\x00\x41\x00\x42\x00\xfc\x0b\x00", True),  # fill [i64 i32 i64]
        (I64MEM, b"\x42\x00\x41\x00\x41\x00\xfc\x0b\x00", False),
        (I64MEM, b"\x42\x00\x42\x00\x42\x00\xfc\x0a\x00\x00", True),  # copy i64 i64 i64
        (I64MEM, b"\x42\x00\x42\x00\x41\x00\xfc\x0a\x00\x00", False),
    ],
)
def test_memory_instruction_types(mem: bytes, code: bytes, ok: bool) -> None:
    ty, fn, cd = _body(code)
    wasm = module(ty, fn, mem, cd)
    if ok:
        check(wasm)
    else:
        with pytest.raises(Invalid):
            check(wasm)


def test_mixed_copy_uses_the_narrower_length() -> None:
    mems = sec(5, b"\x02\x00\x01\x04\x01")  # memory 0 i32, memory 1 i64
    for code, ok in (
        (b"\x41\x00\x42\x00\x41\x00\xfc\x0a\x00\x01", True),  # (dst i32, src i64, n i32)
        (b"\x41\x00\x42\x00\x42\x00\xfc\x0a\x00\x01", False),
    ):
        ty, fn, cd = _body(code)
        if ok:
            check(module(ty, fn, mems, cd))
        else:
            with pytest.raises(Invalid):
                check(module(ty, fn, mems, cd))


def test_i64_table_instructions_and_limits() -> None:
    table = sec(4, b"\x01\x70\x04\x02")
    ty, fn, cd = _body(b"\x42\x00\x25\x00\x1a")  # table.get with an i64 index
    check(module(ty, fn, table, cd))
    ty, fn, cd = _body(b"\x41\x00\x25\x00\x1a")
    with pytest.raises(Invalid):
        check(module(ty, fn, table, cd))
    with pytest.raises(Invalid):  # an i32 table is limited to 2**32-1 elements
        check(module(sec(4, b"\x01\x70\x00\x80\x80\x80\x80\x10")))
    check(module(sec(4, b"\x01\x70\x04\x80\x80\x80\x80\x10")))
    with pytest.raises(Invalid):  # an i64 memory is limited to 2**48 pages
        check(mem64(b"\x04\x81\x80\x80\x80\x80\x80\x80\x01"))
    check(mem64(b"\x04\x80\x80\x80\x80\x80\x80\x40"))


def test_active_data_offset_has_the_address_type() -> None:
    seg32 = sec(11, b"\x01\x00\x41\x00\x0b\x01a")
    seg64 = sec(11, b"\x01\x00\x42\x00\x0b\x01a")
    with pytest.raises(Invalid):
        check(mem64(b"\x04\x01", seg32))
    check(mem64(b"\x04\x01", seg64))


def test_pyc_runs_an_i64_memory() -> None:
    m = load(mem64(b"\x05\x01\x03", sec(11, b"\x01\x00\x42\x02\x0b\x01a")), {})
    assert m["ld"](2) == ord("a") and m["ld"](0) == 0
    with pytest.raises(Trap) as e:
        m["ld"](1 << 40)
    assert e.value.code is TrapCode.MEMORY_OUT_OF_BOUNDS
    with pytest.raises(Trap):
        m["ld"](M64)
    assert m["grow"](2) == 1 and len(m["mem"]) == 3 << 16
    assert not any(m["mem"][1 << 16 :])  # the new pages are zero
    assert m["grow"](1) == M64  # above the declared maximum
    assert m["grow"](1 << 40) == M64


def test_pyc_grow_failure_is_minus_one(monkeypatch: pytest.MonkeyPatch) -> None:
    m = load(mem64(b"\x04\x01"), {})

    def boom(self: object, data: object) -> None:
        raise MemoryError

    monkeypatch.setattr("waboros.targets.pyc.runtime._Mem.extend", boom)
    assert m["grow"](1) == M64
    monkeypatch.undo()
    assert m["grow"](1) == 1 and len(m["mem"]) == 2 << 16


def test_pyc_minimum_that_cannot_be_provided(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(n: int) -> None:
        raise MemoryError

    wasm = mem64(b"\x04\x80\x80\x80\x80\x80\x80\x40")  # 2**48 pages
    assert parse(wasm)  # a definition allocates nothing
    with pytest.raises(MemoryError, match="memory minimum size of 281474976710656 pages exceeds"):
        load(wasm, {})
    monkeypatch.setattr("waboros.targets.pyc.runtime._Mem", boom)
    with pytest.raises(MemoryError, match="failed to allocate 65536 bytes for a memory of 1 pages"):
        load(mem64(b"\x04\x01"), {})


@pytest.mark.parametrize("target", ["jit-x86_64", "jit-aarch64"])
def test_jit_refuses_i64_memories_and_tables(target: str) -> None:
    for wasm, feature in (
        (mem64(b"\x04\x01"), "memory64"),
        (module(sec(4, b"\x01\x70\x04\x01")), "table64"),
    ):
        m = check(wasm)
        t = base.TARGETS[target]()
        assert t.supports(m) == feature
        with pytest.raises(base.Unsupported) as e:
            t.compile(m)
        assert e.value.feature == feature


def test_memory64_rejections_have_the_right_reason() -> None:
    """Q7: NotImplementedError counts as a pass for assert_invalid in the runner, so check the
    reason here: every rejection in memory64/ is Invalid (or Malformed for assert_malformed)."""
    root = fetch.CACHE_DIR
    files = sorted((root / "json" / "memory64").glob("*.json")) if root.is_dir() else []
    if not files:
        pytest.skip("no spec cache: run tests/spec/fetch.py")
    seen = 0
    for jf in files:
        for c in json.loads(jf.read_text(encoding="utf-8"))["commands"]:
            kind = c["type"]
            if kind not in ("assert_invalid", "assert_malformed"):
                continue
            if c.get("module_type") == "text" and "binary_filename" not in c:
                continue
            data = (jf.parent / jf.stem / c.get("binary_filename", c["filename"])).read_bytes()
            want = Invalid if kind == "assert_invalid" else Malformed
            with pytest.raises(want):
                validate(parse(data))
            seen += 1
    assert seen > 300
