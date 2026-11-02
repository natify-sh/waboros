# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportArgumentType=false
import os

import pytest

from waboros.frontend.binary import parse
from waboros.frontend.validate import Invalid, validate

COREMARK = os.path.join(os.path.dirname(__file__), "..", "poc", "in", "core.wasm")
I32, I64 = b"\x7f", b"\x7e"


def sec(i: int, body: bytes) -> bytes:
    return bytes([i, len(body)]) + body


def mod(
    params: bytes, results: bytes, code: bytes, locs: bytes = b"\x00", extra: bytes = b""
) -> bytes:
    """One function of type params -> results with the body `code` (no final `end`)."""
    ty = b"\x01\x60" + bytes([len(params)]) + params + bytes([len(results)]) + results
    body = locs + code + b"\x0b"
    return (
        b"\0asm\1\0\0\0"
        + sec(1, ty)
        + sec(3, b"\x01\x00")
        + extra
        + sec(10, b"\x01" + bytes([len(body)]) + body)
    )


def check(data: bytes) -> None:
    validate(parse(data))


@pytest.mark.parametrize(
    "data",
    [
        mod(b"", I32, b"\x42\x00"),  # i64 where i32 expected
        mod(b"", b"", b"\x41\x00\x42\x00\x6a\x1a"),  # i32.add on i32 i64
        mod(b"", b"", b"\x20\x00\x1a"),  # unknown local
        mod(b"", b"", b"\x1a"),  # drop on an empty stack
        mod(b"", b"", b"\x00\x41\x00\x42\x00\x6a\x1a"),  # unreachable does not excuse a mismatch
        mod(
            b"", b"", b"\x02\x7f\x41\x00\x41\x00\x0e\x01\x01\x00\x0b\x1a"
        ),  # br_table: arities differ
        mod(b"", b"", b"\x0c\x01"),  # unknown label
        mod(b"", b"", b"\x41\x00\x28\x03\x00\x1a"),  # alignment larger than natural
        mod(b"", b"", b"\x41\x00\x28\x02\x00\x1a"),  # no memory
        mod(b"", b"", b"\x41\x00\x24\x00", extra=sec(6, b"\x01\x7f\x00\x41\x00\x0b")),  # immutable
        mod(b"", b"", b"\x41\x00\x04\x40\x41\x00\x0b"),  # if leaves a value
        mod(b"", I32, b"\x41\x00\x04\x7f\x41\x00\x0b"),  # if without else with a result
    ],
)
def test_invalid(data: bytes) -> None:
    with pytest.raises(Invalid):
        check(data)


def test_polymorphic_stack_after_unreachable() -> None:
    check(mod(b"", I32, b"\x00"))  # unreachable: any results
    check(mod(b"", I32, b"\x00\x6a"))  # i32.add pops from the polymorphic stack
    check(mod(b"", b"", b"\x02\x40\x41\x00\x0c\x00\x42\x00\x1a\x0b"))  # dead code after br


def test_multi_value_block_with_type_index() -> None:
    # (type (func (param i32) (result i32 i32))): a block taking and returning values
    ty = b"\x02\x60\x00\x01\x7f\x60\x01\x7f\x02\x7f\x7f"
    body = b"\x00\x41\x01\x02\x01\x41\x02\x0b\x6a\x0b"
    # i32.const 1; block (type 1: (i32) -> (i32 i32)) i32.const 2 end; i32.add
    data = (
        b"\0asm\1\0\0\0"
        + sec(1, ty)
        + sec(3, b"\x01\x00")
        + sec(10, b"\x01" + bytes([len(body)]) + body)
    )
    check(data)


def test_start_function_must_take_nothing() -> None:
    with pytest.raises(Invalid):
        check(mod(b"", I32, b"\x41\x00", extra=sec(8, b"\x00")))


def test_duplicate_export_name() -> None:
    exports = sec(7, b"\x02\x01a\x00\x00\x01a\x00\x00")
    with pytest.raises(Invalid):
        check(mod(b"", b"", b"", extra=exports))


@pytest.mark.skipif(not os.path.exists(COREMARK), reason="poc/in/core.wasm missing")
def test_coremark_validates() -> None:
    with open(COREMARK, "rb") as f:
        m = parse(f.read())
    validate(m)
    assert len(m["insns"]) == len(m["codes"])


def test_table_ops_are_validated() -> None:
    # (table 1 funcref) (func (result i32) (table.size 0)) is valid; table.size 1 is not
    table = sec(4, b"\x01\x70\x00\x01")
    base = sec(1, b"\x01\x60\x00\x01\x7f") + sec(3, b"\x01\x00") + table
    ok = b"\0asm\1\0\0\0" + base + sec(10, b"\x01\x05\x00\xfc\x10\x00\x0b")
    check(ok)
    with pytest.raises(Invalid):
        check(b"\0asm\1\0\0\0" + base + sec(10, b"\x01\x05\x00\xfc\x10\x01\x0b"))
    # table.fill with an i32 where a funcref is expected
    fill = b"\x41\x00\x41\x00\x41\x00\xfc\x11\x00\x41\x00"
    with pytest.raises(Invalid):
        check(
            b"\0asm\1\0\0\0" + base + sec(10, b"\x01" + bytes([len(fill) + 2, 0]) + fill + b"\x0b")
        )


def test_typed_references_are_validated() -> None:
    uninit = bytes.fromhex(
        "0061736d01000000010401600000030201000a0a01080101646f20001a0b"
    )  # (func (local (ref extern)) (drop (local.get 0)))
    with pytest.raises(Invalid, match="uninitialized local"):
        check(uninit)
    funcref_call = bytes.fromhex(
        "0061736d0100000001080260000060017000030201010a08010600200014000b"
    )  # (func (param funcref) (call_ref $t (local.get 0)))
    with pytest.raises(Invalid, match="type mismatch"):
        check(funcref_call)
    set_first = bytes.fromhex(
        "0061736d010000000106016001646f00030201000a0e010c0101646f2000210120011a0b"
    )
    m = parse(set_first)
    validate(m)
    assert m["validated"]


def _tags(results: bytes, tags: bytes, exports: bytes = b"") -> bytes:
    """A module whose type 0 is [] -> results, with a tag section and an export section."""
    return mod(b"", results, b"\x00", extra=sec(13, tags) + sec(7, exports or b"\x00"))


def test_tags_are_validated() -> None:
    m = parse(_tags(b"", b"\x02\x00\x00\x00\x00", b"\x02\x01a\x04\x00\x01b\x04\x01"))
    validate(m)
    assert m["tags"] == [0, 0] and m["validated"]
    for data in (
        _tags(b"", b"\x01\x00\x00", b"\x02\x01a\x04\x00\x01a\x04\x00"),  # duplicate export name
        _tags(b"", b"\x01\x00\x00", b"\x01\x01a\x04\x01"),  # unknown tag
        _tags(b"", b"\x01\x00\x05"),  # unknown type
        _tags(I32, b"\x01\x00\x00"),  # non-empty tag result type
    ):
        with pytest.raises(Invalid):
            check(data)
