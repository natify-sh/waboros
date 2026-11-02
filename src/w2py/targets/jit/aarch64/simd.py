# pyright: reportPrivateUsage=false
"""SIMD (v128) on ARMv8.0-A Advanced SIMD (docs/specs/S0005): the backend's `simd` method. A v128
lives in two value-stack slots; v0-v7 and v16-v31 are scratch inside one call (v8-v15 are never
touched: their low halves are callee-saved, AAPCS64), so nothing vector-sized is live between
calls. Encodings are emitted as words here; `encoder.py` does not change.

Data movement (step 5c): 16-byte moves by `ldr`/`str q`, bitwise operations on v0-v2, lanes in
the slots by GPR loads and stores (x9-x13, x17: scratch as in every method), splat by `dup`.
`v128.const` is the core's (two constant halves)."""

from typing import TYPE_CHECKING

from w2py.targets.jit.aarch64.abi import CTX, FRAME
from w2py.targets.jit.aarch64.encoder import ZR, Asm
from w2py.targets.jit.common.backend import Dst, Operand, Reg, Slot
from w2py.targets.jit.common.simd import MOVES, shape

if TYPE_CHECKING:
    from w2py.targets.jit.aarch64.backend import Backend, Frame

SUPPORTED = MOVES
# Advanced SIMD, 16 bytes (`.16b`): d = n op m; NOT d = ~n; BSL d = (d & n) | (~d & m)
VOP = {"and": 0x4E201C00, "andnot": 0x4E601C00, "or": 0x4EA01C00, "xor": 0x6E201C00,
       "bsl": 0x6E601C00, "not": 0x6E205800}  # fmt: skip


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


def vop(a: Asm, op: str, d: int, n: int, m: int = 0) -> None:
    """and/bic/orr/eor/bsl/not (mvn) vd.16b, vn.16b[, vm.16b] (`andnot` is bic)."""
    a.emit(VOP[op] | m << 16 | n << 5 | d)


def dup(a: Asm, d: int, r: int, size: int) -> None:
    """dup vd.<16b|8h|4s|2d>, w/x r (the low `size` bytes of r into every lane)."""
    a.emit(0x4E000C00 | size << 16 | r << 5 | d)


def _slot(o: Operand | None, disp: int = 0) -> int:
    assert isinstance(o, Slot), o  # a v128 is always in its slots here (S0005)
    return 8 * o.k + disp


def _lane(a: Asm, load: bool, t: int, off: int, size: int, signed: bool = False) -> None:
    """t = / := the `size` bytes at [frame + off] (a multiple of size; through x17 if far)."""
    wide = size == 8 or not signed
    if off // size < 1 << 12:
        (a.ld(t, FRAME, off, size, signed, wide) if load else a.st(t, FRAME, off, size))
    else:
        a.mov_imm(17, off)
        (a.ld_idx(t, FRAME, 17, size, signed, wide) if load else a.st_idx(t, FRAME, 17, size))


def _mem(a: Asm, load: bool, t: int, acc: tuple[int, int | None, int], size: int) -> None:
    """t = / := the `size` bytes of a bounds-checked access (base, index or None, offset)."""
    base, index, off = acc
    if index is None:
        (a.ld(t, base, off, size) if load else a.st(t, base, off, size))
    else:
        (a.ld_idx(t, base, index, size) if load else a.st_idx(t, base, index, size))


def _copy(a: Asm, src: int, to: int) -> None:
    if src != to:
        ldst_q(a, True, 0, FRAME, src)
        ldst_q(a, False, 0, FRAME, to)


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
        return
    kind, size, signed = shape(op)
    acc: tuple[int, int | None, int] = (0, None, 0)  # the memory access, if any
    if kind in ("load", "store", "load_splat", "load_zero", "load_lane", "store_lane"):
        acc = b._bounds(f, args[0], imm[0], size, imm[1])  # the address read first (x9-x12)
    base, index, off = acc
    if kind == "load":
        ldst_q(a, True, 0, base, off, index)
        ldst_q(a, False, 0, FRAME, _slot(dst))
    elif kind == "store":
        ldst_q(a, True, 0, FRAME, _slot(args[1]))
        ldst_q(a, False, 0, base, off, index)
    elif kind in ("splat", "load_splat"):
        if kind == "splat":
            r = b._r(args[0], 10, size == 8)
        else:
            r = 10
            _mem(a, True, r, acc, size)
        dup(a, 0, r, size)
        ldst_q(a, False, 0, FRAME, _slot(dst))
    elif kind == "load_zero":
        _mem(a, True, 10, acc, size)
        _lane(a, False, ZR, _slot(dst, 8), 8)
        _lane(a, False, 10, _slot(dst), 8)
    elif kind == "extract":  # an i32 result as a w register: zero-extended (LESSONS 5)
        assert isinstance(dst, Reg)
        _lane(a, True, dst.id, _slot(args[0], imm[0] * size), size, signed)
    elif kind == "replace":  # the scalar first: it may be in tos (LESSONS 32)
        r = b._r(args[1], 10, size == 8)
        _copy(a, _slot(args[0]), _slot(dst))
        _lane(a, False, r, _slot(dst, imm[0] * size), size)
    elif kind == "load_lane":  # dst overlaps the vector operand (one slot up): copied whole
        _mem(a, True, 10, acc, size)
        _copy(a, _slot(args[1]), _slot(dst))
        _lane(a, False, 10, _slot(dst, imm[2] * size), size)
    elif kind == "store_lane":
        _lane(a, True, 13, _slot(args[1], imm[2] * size), size)
        _mem(a, False, 13, acc, size)
    elif kind in VOP or kind == "bitselect":
        for i, x in enumerate(args):
            ldst_q(a, True, i, FRAME, _slot(x))
        r = 0
        if kind == "bitselect":  # v2 = c: (c & x) | (~c & y)
            vop(a, "bsl", 2, 0, 1)
            r = 2
        else:
            vop(a, kind, 0, 0, 0 if kind == "not" else 1)
        ldst_q(a, False, r, FRAME, _slot(dst))
    elif kind == "any_true":  # the flags of `cmp` are consumed by the next instruction
        assert isinstance(dst, Reg)
        _lane(a, True, 9, _slot(args[0]), 8)
        _lane(a, True, 10, _slot(args[0], 8), 8)
        a.logic("orr", 9, 9, 10)
        a.cmp(9, ZR)
        a.cset(dst.id, "ne")
    else:
        raise AssertionError(f"SIMD opcode {op:#x}")
