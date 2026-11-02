# pyright: reportPrivateUsage=false
"""SIMD (v128) on x86-64-v1, SSE2 only (docs/specs/S0005): the backend's `simd` method. A v128
lives in two value-stack slots; xmm0-xmm5 are scratch inside one call (xmm6-xmm15 are never
touched: callee-saved on Win64), so nothing vector-sized is live between calls. Encodings go
through the encoder's ModRM core; `encoder.py` does not change. Step 5c1: `v128.load`,
`v128.store` and v128 globals (`v128.const` is the core's: two constant halves)."""

from typing import TYPE_CHECKING

from w2py.targets.jit.common.backend import Dst, Operand, Slot
from w2py.targets.jit.x86_64.encoder import R12, R15, RCX, Asm, Mem

if TYPE_CHECKING:
    from w2py.targets.jit.x86_64.backend import Backend, Frame

LOAD, STORE, CONST = 0xFD000, 0xFD00B, 0xFD00C
SUPPORTED = frozenset((LOAD, STORE, CONST))


def movups(a: Asm, x: int, m: Mem, store: bool) -> None:
    """movups xmm, m128 (0F 10) / movups m128, xmm (0F 11): unaligned, any 16 bytes."""
    a._op(b"\x0f\x11" if store else b"\x0f\x10", x, m)


def _slot(o: Operand | None) -> Mem:
    assert isinstance(o, Slot), o  # a v128 is always in its slots here (S0005)
    return Mem(R12, 8 * o.k)


def emit(
    b: "Backend", f: "Frame", op: int, dst: Dst | None, args: tuple[Operand, ...],
    imm: tuple[int, ...],
) -> None:  # fmt: skip
    a = b.a
    if op in (0x23, 0x24):  # (word, indirect): an imported global is a pointer to the owner's
        g = Mem(R15, 8 * imm[0])
        if imm[1]:
            a.mov(RCX, g, w=True)
            g = Mem(RCX)
        src, to = (g, _slot(dst)) if op == 0x23 else (_slot(args[0]), g)
    elif op == LOAD:  # the address is read (into rcx) before dst is written
        src, to = b._bounds(f, args[0], imm[0], 16, imm[1]), _slot(dst)
    elif op == STORE:
        to = b._bounds(f, args[0], imm[0], 16, imm[1])
        src = _slot(args[1])
    else:
        raise AssertionError(f"SIMD opcode {op:#x}")
    movups(a, 0, src, False)
    movups(a, 0, to, True)
