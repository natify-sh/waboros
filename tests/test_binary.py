# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
"""The binary reader: malformed input is `Malformed`, never a stray exception or a bomb."""

import tracemalloc

import pytest

from waboros.frontend.binary import ImplLimit, Malformed, decode, parse

HEAD = b"\0asm\1\0\0\0"
TYPE = b"\x01\x04\x01\x60\x00\x00"
FUNC = b"\x03\x02\x01\x00"


def code(body: bytes) -> bytes:  # a code section with one function: no locals, then `body`
    return b"\x0a" + bytes([len(body) + 3, 1, len(body) + 1, 0]) + body


def test_valid_module_parses() -> None:
    m = parse(HEAD + TYPE + FUNC + code(b"\x00\x0b"))
    assert m["types"] == [([], [])] and m["codes"] == [([], b"\x00\x0b")]


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\0asm",
        b"\0asm\2\0\0\0",
        HEAD + b"\x01",  # section without a size
        HEAD + b"\x01\x05\x00",  # size beyond the end
        HEAD + b"\x20\x00",  # unknown section id
        HEAD + FUNC + TYPE,  # sections out of order
        HEAD + TYPE + TYPE,  # duplicate section
        HEAD + TYPE + b"\x00\x01\xc0\x00",  # custom section with an overlong name length
        HEAD + b"\x00\x03\x02\xc3\x28",  # custom section name is not UTF-8
        HEAD + b"\x01\x08\x01\x60\x80\x80\x80\x80\x10\x00",  # u32 with unused bits set
        HEAD + FUNC,  # function without code
        HEAD + b"\x0c\x01\x01",  # data count without data
        HEAD + b"\x06\x06\x01\x7f\x02\x41\x00\x0b",  # mutability byte 2
        HEAD + b"\x05\x03\x01\x08\x00",  # limits flag 8
    ],
)
def test_malformed_module(data: bytes) -> None:
    with pytest.raises(Malformed):
        parse(data)


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"\x00",
        b"\xff\x0b",
        b"\x0b\x00",
        b"\x02\x40\x0b",
        b"\x41\x80\x80\x80\x80\x10\x0b",
        b"\x43\x00\x00\x0b",
    ],
)
def test_malformed_body(body: bytes) -> None:
    with pytest.raises(Malformed):
        decode(body)


def test_unsupported_encoding_is_not_malformed() -> None:
    with pytest.raises(NotImplementedError):
        decode(b"\xfe\x0b")  # the atomics prefix (0xFD, SIMD, is decoded since S0005)


def test_too_many_locals() -> None:
    big = b"\xff\xff\xff\xff\x0f\x7f"  # 2**32-1 locals of i32

    def locals_of(groups: bytes, n: int) -> bytes:
        body = bytes([n]) + groups + b"\x0b"
        return HEAD + TYPE + FUNC + b"\x0a" + bytes([len(body) + 2, 1, len(body)]) + body

    tracemalloc.start()
    with pytest.raises(Malformed):
        parse(locals_of(big + big, 2))
    with pytest.raises(ImplLimit):
        parse(locals_of(big, 1))
    assert tracemalloc.get_traced_memory()[1] < 1 << 20
    tracemalloc.stop()


def test_tag_attribute_must_be_zero() -> None:
    types = b"\x01\x04\x01\x60\x00\x00"
    assert parse(b"\0asm\1\0\0\0" + types + b"\x0d\x03\x01\x00\x00")["tags"] == [0]
    with pytest.raises(Malformed):
        parse(b"\0asm\1\0\0\0" + types + b"\x0d\x03\x01\x01\x00")
