# pyright: reportPrivateUsage=false
"""SIMD (v128) on x86-64-v1, SSE2 only (docs/specs/S0005): the backend's `simd` method. A v128
lives in two value-stack slots; xmm0-xmm5 are scratch inside one call (xmm6-xmm15 are never
touched: callee-saved on Win64), so nothing vector-sized is live between calls. Encodings go
through the encoder's ModRM core; `encoder.py` does not change.

Data movement (step 5c): 16-byte moves by `movups` (slots are only 8-byte aligned, so no packed
operation takes a memory operand), bitwise operations on xmm0-xmm2, lanes in the slots by GPR
loads and stores (rcx, rdx: scratch as in every method), splat by a multiply with 0x0101...
`v128.const` is the core's (two constant halves)."""

from typing import TYPE_CHECKING

from w2py.targets.jit.common.backend import Dst, Operand, Reg, Slot
from w2py.targets.jit.common.simd import MOVES, shape
from w2py.targets.jit.x86_64.encoder import R12, R15, RCX, RDX, Asm, Mem

if TYPE_CHECKING:
    from w2py.targets.jit.x86_64.backend import Backend, Frame

SUPPORTED = MOVES
SPLAT = {1: 0x0101010101010101, 2: 0x0001000100010001, 4: 0x0000000100000001}


def movups(a: Asm, x: int, m: Mem, store: bool) -> None:
    """movups xmm, m128 (0F 10) / movups m128, xmm (0F 11): unaligned, any 16 bytes."""
    a._op(b"\x0f\x11" if store else b"\x0f\x10", x, m)


def andnps(a: Asm, d: int, s: int) -> None:
    """andnps xmm d, xmm s (0F 55): d = ~d & s."""
    a._op(b"\x0f\x55", d, s)


def _slot(o: Operand | None, disp: int = 0) -> Mem:
    assert isinstance(o, Slot), o  # a v128 is always in its slots here (S0005)
    return Mem(R12, 8 * o.k + disp)


def _ld(a: Asm, r: int, m: Mem, size: int) -> None:
    """r = the `size` bytes at m, zero-extended."""
    if size == 8:
        a.mov(r, m, w=True)
    elif size == 4:
        a.mov(r, m)
    else:
        a.movzx(r, m, 8 * size)


def _st(a: Asm, m: Mem, r: int, size: int) -> None:
    if size == 1:
        a.store8(m, r)
    elif size == 2:
        a.store16(m, r)
    else:
        a.mov(m, r, w=size == 8)


def _copy(a: Asm, src: Mem, to: Mem) -> None:
    if src != to:
        movups(a, 0, src, False)
        movups(a, 0, to, True)


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
        movups(a, 0, g if op == 0x23 else _slot(args[0]), False)
        movups(a, 0, _slot(dst) if op == 0x23 else g, True)
        return
    kind, size, signed = shape(op)
    m = Mem(RCX)  # the memory access, if any: bounds checked, the address read first
    if kind in ("load", "store", "load_splat", "load_zero", "load_lane", "store_lane"):
        m = b._bounds(f, args[0], imm[0], size, imm[1])
    if kind == "load":
        _copy(a, m, _slot(dst))
    elif kind == "store":
        _copy(a, _slot(args[1]), m)
    elif kind in ("splat", "load_splat", "load_zero"):
        if kind == "splat":  # the lane's bits zero-extended into rdx
            b.move(Reg(RDX), args[0], size == 8)
            if size < 4:
                a.movzx(RDX, RDX, 8 * size)
        else:
            _ld(a, RDX, m, size)
        if kind == "load_zero":
            a.mov_imm(_slot(dst, 8), 0, w=True)
        elif size < 8:
            a.mov_imm(RCX, SPLAT[size], w=True)
            a.imul(RDX, RCX, w=True)
        a.mov(_slot(dst), RDX, w=True)
        if kind != "load_zero":
            a.mov(_slot(dst, 8), RDX, w=True)
    elif kind == "extract":  # an i32 result in its 32-bit form: zero-extended (LESSONS 5)
        assert isinstance(dst, Reg)
        v = _slot(args[0], imm[0] * size)
        if size >= 4 or not signed:
            _ld(a, dst.id, v, size)
        else:
            a.movsx(dst.id, v, 8 * size)
    elif kind == "replace":  # the scalar first: it may be in tos (LESSONS 32)
        b.move(Reg(RDX), args[1], size == 8)
        _copy(a, _slot(args[0]), _slot(dst))
        _st(a, _slot(dst, imm[0] * size), RDX, size)
    elif kind == "load_lane":  # dst overlaps the vector operand (one slot up): copied whole
        _ld(a, RDX, m, size)
        _copy(a, _slot(args[1]), _slot(dst))
        _st(a, _slot(dst, imm[2] * size), RDX, size)
    elif kind == "store_lane":
        _ld(a, RDX, _slot(args[1], imm[2] * size), size)
        _st(a, m, RDX, size)
    elif kind == "not":
        for h in (0, 8):
            a.mov(RCX, _slot(args[0], h), w=True)
            a.unary("not", RCX, w=True)
            a.mov(_slot(dst, h), RCX, w=True)
    elif kind in ("and", "andnot", "or", "xor", "bitselect"):
        for i, x in enumerate(args):
            movups(a, i, _slot(x), False)
        r = 0
        if kind == "andnot":  # x & ~y
            andnps(a, 1, 0)
            r = 1
        elif kind == "bitselect":  # (x & c) | (y & ~c)
            a.sse("and", 0, 2, False)
            andnps(a, 2, 1)
            a.sse("or", 0, 2, False)
        else:
            a.sse(kind, 0, 1, False)  # andps / orps / xorps
        movups(a, r, _slot(dst), True)
    elif kind == "any_true":  # the flags of `or` are consumed by the next instruction
        assert isinstance(dst, Reg)
        a.mov(RCX, _slot(args[0]), w=True)
        a.alu("or", RCX, _slot(args[0], 8), w=True)
        a.setcc("ne", RCX)
        a.movzx(dst.id, RCX, 8)
    else:
        raise AssertionError(f"SIMD opcode {op:#x}")
