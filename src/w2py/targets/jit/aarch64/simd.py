# pyright: reportPrivateUsage=false
"""SIMD (v128) on ARMv8.0-A Advanced SIMD (docs/specs/S0005): the backend's `simd` method. A v128
lives in two value-stack slots; v0-v7 and v16-v31 are scratch inside one call (v8-v15 are never
touched: their low halves are callee-saved, AAPCS64), so nothing vector-sized is live between
calls. Encodings are emitted as words here; `encoder.py` does not change. Step 5c1: `v128.load`,
`v128.store` and v128 globals (`v128.const` is the core's: two constant halves)."""

from typing import TYPE_CHECKING

from w2py.targets.jit.aarch64.abi import CTX, FRAME
from w2py.targets.jit.aarch64.encoder import Asm
from w2py.targets.jit.common.backend import Dst, Operand, Slot

if TYPE_CHECKING:
    from w2py.targets.jit.aarch64.backend import Backend, Frame

LOAD, STORE, CONST = 0xFD000, 0xFD00B, 0xFD00C
SUPPORTED = frozenset((LOAD, STORE, CONST))


def ldst_q(a: Asm, load: bool, t: int, base: int, off: int = 0, index: int | None = None) -> None:
    """ldr/str q`t`, [base, #off] (scaled), ldur/stur for a small unaligned offset, or
    [base, index]; any other offset goes through x17."""
    if index is None and off % 16 == 0 and 0 <= off < 16 << 12:
        a.emit((0x3DC00000 if load else 0x3D800000) | off // 16 << 10 | base << 5 | t)
        return
    if index is None and -256 <= off < 256:
        a.emit((0x3CC00000 if load else 0x3C800000) | (off & 0x1FF) << 12 | base << 5 | t)
        return
    if index is None:
        a.mov_imm(17, off)
        index = 17
    else:
        assert off == 0
    a.emit((0x3CE06800 if load else 0x3CA06800) | index << 16 | base << 5 | t)


def _slot(o: Operand | None) -> int:
    assert isinstance(o, Slot), o  # a v128 is always in its slots here (S0005)
    return 8 * o.k


def emit(
    b: "Backend", f: "Frame", op: int, dst: Dst | None, args: tuple[Operand, ...],
    imm: tuple[int, ...],
) -> None:  # fmt: skip
    a = b.a
    if op in (0x23, 0x24):  # (word, indirect): an imported global is a pointer to the owner's
        g, off = CTX, 8 * imm[0]
        if imm[1]:
            b._word(True, 9, CTX, off)
            g, off = 9, 0
        if op == 0x23:
            ldst_q(a, True, 0, g, off)
            ldst_q(a, False, 0, FRAME, _slot(dst))
        else:
            ldst_q(a, True, 0, FRAME, _slot(args[0]))
            ldst_q(a, False, 0, g, off)
    elif op == LOAD:  # the address is read (into x9) before dst is written
        base, index, off = b._bounds(f, args[0], imm[0], 16, imm[1])
        ldst_q(a, True, 0, base, off, index)
        ldst_q(a, False, 0, FRAME, _slot(dst))
    elif op == STORE:
        base, index, off = b._bounds(f, args[0], imm[0], 16, imm[1])
        ldst_q(a, True, 0, FRAME, _slot(args[1]))
        ldst_q(a, False, 0, base, off, index)
    else:
        raise AssertionError(f"SIMD opcode {op:#x}")
