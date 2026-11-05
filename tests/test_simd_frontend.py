# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportArgumentType=false, reportOptionalSubscript=false, reportUnknownMemberType=false
"""S0005 step 5a: v128 and the 0xFD operators are decoded and validated; targets refuse them."""

import pytest

from tests.test_validate import I32, check, mod, sec
from waboros.frontend import simd
from waboros.frontend.binary import Malformed, decode, parse
from waboros.frontend.validate import Invalid, validate
from waboros.targets import base

V = b"\x7b"
MEM = sec(5, b"\x01\x00\x01")


def fd(sub: int, *imm: bytes) -> bytes:
    """The operator `sub` under the 0xFD prefix, its sub-opcode as a LEB128 u32."""
    leb = bytes([sub]) if sub < 0x80 else bytes([sub & 0x7F | 0x80, sub >> 7])
    return b"\xfd" + leb + b"".join(imm)


CONST = fd(0x0C, bytes(range(16)))  # v128.const 0x0f0e...0100
ADD = fd(0xAE)  # i32x4.add
DROP = b"\x1a"


def test_table_has_the_256_operators() -> None:
    assert len(simd.OPS) == 256  # 236 + the 20 relaxed ones (S0008)
    assert simd.OPS[0x0C].name == "v128.const" and simd.OPS[0xAE].name == "i32x4.add"
    assert simd.OPS[0xFB].name == "f32x4.convert_i32x4_u"
    assert not {0x9A, 0xA2, 0xEE, 0x114} & simd.OPS.keys()  # reserved or past the end


def test_decode_immediates() -> None:
    body = (
        CONST
        + fd(0x0D, bytes(range(16, 32)))  # i8x16.shuffle
        + fd(0x15, b"\x0f")  # i8x16.extract_lane_s 15
        + fd(0x00, b"\x04\x08")  # v128.load align 4 offset 8
        + fd(0x00, b"\x44\x01\x08")  # same on memory 1
        + fd(0x55, b"\x01\x02\x07")  # v128.load16_lane align 1 offset 2 lane 7
        + b"\x0b"
    )
    ins = decode(body)
    assert ins[0] == (0xFD00C, int.from_bytes(bytes(range(16)), "little"))
    assert ins[1] == (0xFD00D, bytes(range(16, 32)))
    assert ins[2] == (0xFD015, 15)
    assert ins[3] == (0xFD000, 8, 4, 0)
    assert ins[4] == (0xFD000, 8, 4, 1)
    assert ins[5] == (0xFD055, 2, 1, 0, 7)


@pytest.mark.parametrize(
    "body",
    [
        fd(0x9A),  # reserved sub-opcode
        fd(0x0C, b"\x00" * 15),  # v128.const cut short
        fd(0x0D, b"\x00" * 15),  # shuffle cut short
        fd(0x15),  # lane index missing
    ],
)
def test_malformed(body: bytes) -> None:
    with pytest.raises(Malformed):
        decode(body + b"\x0b")


RELAXED_ARITY = {  # sub -> operands; the result is always v128
    **dict.fromkeys([0x100, 0x10D, 0x10E, 0x10F, 0x110, 0x111, 0x112], 2),
    **dict.fromkeys(range(0x101, 0x105), 1),
    **dict.fromkeys([*range(0x105, 0x10D), 0x113], 3),
}


def test_relaxed_simd_is_decoded_as_20_operators_of_its_own() -> None:
    assert sorted(RELAXED_ARITY) == list(range(0x100, 0x114))
    for sub, n in RELAXED_ARITY.items():
        assert decode(fd(sub) + b"\x0b") == [(simd.BASE + sub,), (0x0B,)]
        assert simd.OPS[sub].pops == (simd.V,) * n and simd.OPS[sub].res == simd.V
        check(mod(b"", b"", CONST * n + fd(sub) + DROP))  # valid
        with pytest.raises(Invalid):  # one operand short
            check(mod(b"", b"", CONST * (n - 1) + fd(sub) + DROP))
    with pytest.raises(Invalid):  # an i32 for the last v128
        check(mod(b"", b"", CONST + b"\x41\x00" + fd(0x10D) + DROP))
    with pytest.raises(Malformed):
        decode(fd(0x114) + b"\x0b")


def test_wide_arithmetic_decodes_and_validates() -> None:
    for sub, n in ((19, 4), (20, 4), (21, 2), (22, 2)):
        op = b"\xfc" + bytes([sub])
        assert decode(op + b"\x0b") == [(0xFC00 + sub,), (0x0B,)]
        args = b"\x42\x00" * n
        check(mod(b"", b"", args + op + b"\x1a\x1a"))  # two i64 results
        with pytest.raises(Invalid):  # three results are not there
            check(mod(b"", b"", args + op + b"\x1a\x1a\x1a"))
        with pytest.raises(Invalid):  # an i32 operand
            check(mod(b"", b"", args[:-2] + b"\x41\x00" + op + b"\x1a\x1a"))
    for sub in (18, 23):
        with pytest.raises(Malformed):
            decode(b"\xfc" + bytes([sub]) + b"\x0b")


def test_v128_type_and_constant_expression() -> None:
    glob = sec(6, b"\x01" + V + b"\x00" + CONST + b"\x0b")
    m = parse(mod(b"", b"", b"", extra=glob))
    assert m["globals"][0][0] == 0x7B and m["globals"][0][2][0] == "v128"
    validate(m)
    assert m["validated"]
    bad = sec(6, b"\x01\x7f\x00" + CONST + b"\x0b")  # v128.const initialising an i32
    with pytest.raises(Invalid):
        check(mod(b"", b"", b"", extra=bad))


def test_block_type_v128() -> None:
    check(mod(b"", b"", b"\x02" + V + CONST + b"\x0b" + DROP))


@pytest.mark.parametrize(
    "code",
    [
        CONST + CONST + ADD + DROP,
        CONST + fd(0x15, b"\x0f") + DROP,  # i8x16.extract_lane_s 15
        CONST + b"\x41\x00" + fd(0x1C, b"\x03") + DROP,  # i32x4.replace_lane 3
        b"\x00" + ADD + DROP,  # unreachable: polymorphic stack
        b"\x41\x00" + fd(0x00, b"\x04\x00") + DROP,
    ],
)
def test_valid(code: bytes) -> None:
    check(mod(b"", b"", code, extra=MEM))


@pytest.mark.parametrize(
    "code",
    [
        CONST + ADD + DROP,  # one operand missing
        CONST + b"\x41\x00" + ADD + DROP,  # i32 for v128
        CONST + fd(0x15, b"\x10") + DROP,  # lane 16 of 16
        CONST + fd(0x19, b"\x08") + DROP,  # lane 8 of 8
        CONST + CONST + fd(0x0D, bytes(range(32, 16, -1))) + DROP,  # shuffle lane 32
        b"\x41\x00" + fd(0x00, b"\x05\x00") + DROP,  # align 5 > 4
        b"\x41\x00" + CONST + fd(0x57, b"\x04\x00\x02") + DROP,  # load64_lane: align 4, lane 2
        b"\x41\x00" + CONST + fd(0x56, b"\x02\x00\x04") + DROP,  # load32_lane: lane 4 of 4
        b"\x41\x00" + fd(0x00, b"\x04\x00") + b"\x1a" + b"\x1a",  # drop on an empty stack
        CONST + b"\x41\x00" + b"\x6a" + DROP,  # i32.add on a v128
    ],
)
def test_invalid(code: bytes) -> None:
    with pytest.raises(Invalid):
        check(mod(b"", b"", code, extra=MEM))


def test_no_memory_is_invalid() -> None:
    with pytest.raises(Invalid):
        check(mod(b"", b"", b"\x41\x00" + fd(0x00, b"\x04\x00") + DROP))


def test_drop_and_select_are_annotated_for_v128_only() -> None:
    m = parse(mod(b"", V, CONST + CONST + b"\x41\x00\x1b" + CONST + DROP))
    validate(m)
    assert m["insns"][0][3:] == [
        (0x1B, [0x7B]),
        (0xFD00C, m["insns"][0][4][1]),
        (0x1A, 0x7B),
        (0x0B,),
    ]
    m = parse(mod(b"", b"", b"\x41\x00\x41\x00\x41\x00\x1b\x1a"))
    validate(m)
    assert m["insns"][0][3:] == [(0x1B,), (0x1A,), (0x0B,)]
    m = parse(mod(b"", b"", b"\x00" + CONST + b"\x1a\x1a" + b"\x00"))  # polymorphic: stays plain
    validate(m)
    assert (0x1A,) in m["insns"][0]


@pytest.mark.parametrize("name", ["jit-x86_64", "jit-aarch64"])
@pytest.mark.parametrize(
    "data",
    [
        mod(b"", b"", CONST + DROP),  # a v128 operator only
        mod(V, b"", b""),  # a v128 parameter only
        mod(b"", b"", b"", extra=sec(6, b"\x01" + V + b"\x00" + CONST + b"\x0b")),  # a v128 global
    ],
)
def test_jit_targets_accept_v128(name: str, data: bytes) -> None:
    """Since S0005 step 5c1 the JITs compile v128; since 5d3 and 5e2 every operator."""
    m = parse(data)
    validate(m)
    assert base.TARGETS[name]().supports(m) is None
    m = parse(mod(b"", b"", CONST + CONST + b"\xfd\x0e" + DROP))  # i8x16.swizzle: 5d3, 5e2
    validate(m)
    assert base.TARGETS[name]().supports(m) is None


def test_module_without_v128_is_not_refused() -> None:
    m = parse(mod(b"", I32, b"\x41\x00"))
    validate(m)
    assert base.TARGETS["pyc"]().supports(m) is None
