"""x86-64 instruction encoder (S0002 step 2): the forms the phase-3 core needs (SSE2 scalar
included), x86-64-v1 only (D24). Ported deliberately from `poc/w2jit.py` `Asm`. Jumps and calls
go through labels and fixups only (LESSONS 6). Every form is checked against
`llvm-mc --show-encoding` (tests/jit/test_encoder.py).

Registers are numbers 0-15 (RAX..R15); `w=True` selects the 64-bit form, else 32-bit. Where several
encodings exist, the one llvm-mc picks is emitted (shortest immediate, rax short forms, D1 for a
shift by 1).
"""

import struct
from dataclasses import dataclass

Reg = int
RAX, RCX, RDX, RBX, RSP, RBP, RSI, RDI = range(8)
R8, R9, R10, R11, R12, R13, R14, R15 = range(8, 16)

CC = {
    "o": 0, "no": 1, "b": 2, "ae": 3, "e": 4, "ne": 5, "be": 6, "a": 7,
    "s": 8, "ns": 9, "p": 10, "np": 11, "l": 12, "ge": 13, "le": 14, "g": 15,
}  # fmt: skip
ALU = {"add": 0, "or": 1, "adc": 2, "sbb": 3, "and": 4, "sub": 5, "xor": 6, "cmp": 7}
UNARY = {"not": 2, "neg": 3, "mul": 4, "div": 6, "idiv": 7}  # F7 /ext
SHIFT = {"rol": 0, "ror": 1, "shl": 4, "shr": 5, "sar": 7}
BT = {"bt": 4, "bts": 5, "btr": 6, "btc": 7}  # 0F BA /ext ib; 0F A3+8*(ext-4) /r
SSE = {"sqrt": 0x51, "and": 0x54, "or": 0x56, "xor": 0x57, "add": 0x58, "mul": 0x59, "sub": 0x5C,
       "min": 0x5D, "div": 0x5E, "max": 0x5F}  # fmt: skip
_SCALE = {1: 0, 2: 1, 4: 2, 8: 3}


@dataclass(frozen=True, slots=True)
class Mem:
    """[base + index*scale + disp]."""

    base: Reg
    disp: int = 0
    index: Reg | None = None
    scale: int = 1

    def __post_init__(self) -> None:
        _reg(self.base)
        if self.index is not None:
            _reg(self.index)
        if self.index == RSP:
            raise ValueError("rsp cannot be an index")
        if self.scale not in _SCALE or not -(1 << 31) <= self.disp < (1 << 31):
            raise ValueError(f"bad memory operand {self}")


@dataclass(frozen=True, slots=True)
class Label:
    id: int


def _reg(r: int) -> int:
    if not 0 <= r < 16:
        raise ValueError(f"no register {r}")
    return r


def _signed(v: int, bits: int) -> int:
    """The one immediate convention: a value of the operand width, signed or unsigned (Raw i64 are
    unsigned), in [-2**(bits-1), 2**bits); returned as signed."""
    if not -(1 << bits - 1) <= v < (1 << bits):
        raise ValueError(f"immediate {v:#x} does not fit {bits} bits")
    return v - (1 << bits) if v >= 1 << bits - 1 else v


def _s32(v: int, w: bool) -> int:
    """An immediate for an imm8/imm32 field: 64-bit ops sign-extend it, so it must fit s32."""
    v = _signed(v, 64 if w else 32)
    if not -(1 << 31) <= v < (1 << 31):
        raise ValueError(f"immediate {v:#x} does not fit a sign-extended imm32")
    return v


class Asm:
    def __init__(self) -> None:
        self.buf = bytearray()
        self._labels: list[int | None] = []
        self._fixups: list[tuple[int, int, Label]] = []  # (position, size 1|4, target)

    # --- labels
    def label(self) -> Label:
        self._labels.append(None)
        return Label(len(self._labels) - 1)

    def bind(self, label: Label) -> None:
        if self._labels[label.id] is not None:
            raise ValueError(f"label {label.id} bound twice")
        self._labels[label.id] = len(self.buf)

    def _rel(self, label: Label, size: int) -> None:
        self._fixups.append((len(self.buf), size, label))
        self.buf += bytes(size)

    def data_rel32(self, label: Label) -> None:
        """A 4-byte data field: label - (field address + 4) (jump-table entries)."""
        self._rel(label, 4)

    def finish(self) -> bytes:
        """Resolve every fixup (relative to the end of its field) and return the code."""
        for pos, size, label in self._fixups:
            target = self._labels[label.id]
            if target is None:
                raise ValueError(f"label {label.id} never bound")
            rel = target - (pos + size)
            if not -(1 << 8 * size - 1) <= rel < (1 << 8 * size - 1):
                raise ValueError(f"rel{8 * size} out of range: {rel}")
            self.buf[pos : pos + size] = struct.pack("<b" if size == 1 else "<i", rel)
        return bytes(self.buf)

    # --- the core of every ModRM form
    def _op(
        self,
        opcode: bytes,
        reg: int,
        rm: Reg | Mem,
        w: bool = False,
        pre: bytes = b"",
        byte_regs: tuple[Reg, ...] = (),
    ) -> None:
        """Emit [pre] [REX] opcode ModRM [SIB] [disp]; `reg` is a register or an opcode extension.
        `byte_regs`: 8-bit register operands; spl/bpl/sil/dil need a REX prefix."""
        _reg(reg)
        if not isinstance(rm, Mem):
            _reg(rm)
        b = self.buf
        b += pre
        base, index = (rm.base, rm.index or 0) if isinstance(rm, Mem) else (rm, 0)
        rex = 0x40 | w << 3 | (reg >> 3 & 1) << 2 | (index >> 3 & 1) << 1 | (base >> 3 & 1)
        if rex != 0x40 or any(4 <= r < 8 for r in byte_regs):
            b.append(rex)
        b += opcode
        if not isinstance(rm, Mem):
            b.append(0xC0 | (reg & 7) << 3 | (rm & 7))
            return
        d = rm.disp
        mod = 0 if d == 0 and base & 7 != RBP else 1 if -128 <= d < 128 else 2
        if rm.index is None and base & 7 != RSP:
            b.append(mod << 6 | (reg & 7) << 3 | (base & 7))
        else:
            idx = RSP if rm.index is None else rm.index & 7
            b.append(mod << 6 | (reg & 7) << 3 | 4)
            b.append(_SCALE[rm.scale] << 6 | idx << 3 | (base & 7))
        if mod == 1:
            b += struct.pack("<b", d)
        elif mod == 2:
            b += struct.pack("<i", d)

    def _imm(self, v: int, size: int) -> None:
        self.buf += struct.pack("<b" if size == 1 else "<i", v)

    # --- moves
    def mov(self, dst: Reg | Mem, src: Reg | Mem, w: bool = False) -> None:
        if isinstance(src, Mem):
            assert not isinstance(dst, Mem)
            self._op(b"\x8b", dst, src, w)
        else:
            self._op(b"\x89", src, dst, w)

    def mov_imm(self, dst: Reg | Mem, v: int, w: bool = False) -> None:
        """32-bit: B8+r / C7. 64-bit: a value in [0, 2**32) as the zero-extending 32-bit move,
        a sign-extended imm32 as C7, else movabs (`v` signed or unsigned, see _signed)."""
        if isinstance(dst, Mem):
            self._op(b"\xc7", 0, dst, w)
            self._imm(_s32(v, w), 4)
            return
        v = _signed(v, 64 if w else 32)
        if not w or 0 <= v < (1 << 32):
            self._op_plus_r(0xB8, dst, False)
            self.buf += struct.pack("<I", v & 0xFFFFFFFF)
        elif -(1 << 31) <= v < (1 << 31):
            self._op(b"\xc7", 0, dst, True)
            self._imm(v, 4)
        else:
            self._op_plus_r(0xB8, dst, True)
            self.buf += struct.pack("<Q", v & (1 << 64) - 1)

    def store8(self, dst: Mem, src: Reg) -> None:
        self._op(b"\x88", src, dst, byte_regs=(src,))

    def store16(self, dst: Mem, src: Reg) -> None:
        self._op(b"\x89", src, dst, pre=b"\x66")

    def movzx(self, dst: Reg, src: Reg | Mem, size: int, w: bool = False) -> None:
        """Zero-extend an 8- or 16-bit source."""
        self._op(b"\x0f\xb6" if size == 8 else b"\x0f\xb7", dst, src, w, byte_regs=_b(src, size))

    def movsx(self, dst: Reg, src: Reg | Mem, size: int, w: bool = False) -> None:
        """Sign-extend an 8-, 16- or (64-bit destination only) 32-bit source."""
        if size == 32:
            self._op(b"\x63", dst, src, True)
        else:
            self._op(
                b"\x0f\xbe" if size == 8 else b"\x0f\xbf", dst, src, w, byte_regs=_b(src, size)
            )

    def lea(self, dst: Reg, src: Mem, w: bool = True) -> None:
        self._op(b"\x8d", dst, src, w)

    def lea_label(self, dst: Reg, label: Label) -> None:
        """lea dst, [rip + label] (jump tables, data after the code)."""
        _reg(dst)
        self.buf += bytes([0x48 | (dst >> 3 & 1) << 2, 0x8D, (dst & 7) << 3 | 5])
        self._rel(label, 4)

    def cmov(self, cc: str, dst: Reg, src: Reg | Mem, w: bool = False) -> None:
        self._op(bytes([0x0F, 0x40 | CC[cc]]), dst, src, w)

    def setcc(self, cc: str, dst: Reg) -> None:
        """setcc on the low byte of dst (the rest is unchanged: zero-extend with movzx)."""
        self._op(bytes([0x0F, 0x90 | CC[cc]]), 0, dst, byte_regs=(dst,))

    # --- arithmetic
    def alu(self, op: str, dst: Reg | Mem, src: Reg | Mem, w: bool = False) -> None:
        """add/or/adc/sbb/and/sub/xor/cmp dst, src (one of them a register)."""
        base = ALU[op] << 3
        if isinstance(src, Mem):
            assert not isinstance(dst, Mem)
            self._op(bytes([base | 3]), dst, src, w)
        else:
            self._op(bytes([base | 1]), src, dst, w)

    def alu_imm(self, op: str, dst: Reg | Mem, v: int, w: bool = False) -> None:
        v = _s32(v, w)
        if -128 <= v < 128:
            self._op(b"\x83", ALU[op], dst, w)
            self._imm(v, 1)
            return
        if dst == RAX:
            self.buf += b"\x48" if w else b""
            self.buf.append(ALU[op] << 3 | 5)
        else:
            self._op(b"\x81", ALU[op], dst, w)
        self._imm(v, 4)

    def test(self, dst: Reg | Mem, src: Reg, w: bool = False) -> None:
        self._op(b"\x85", src, dst, w)

    def test_imm(self, dst: Reg | Mem, v: int, w: bool = False) -> None:
        if dst == RAX:
            self.buf += b"\x48\xa9" if w else b"\xa9"
        else:
            self._op(b"\xf7", 0, dst, w)
        self._imm(_s32(v, w), 4)

    def imul(self, dst: Reg, src: Reg | Mem, w: bool = False) -> None:
        self._op(b"\x0f\xaf", dst, src, w)

    def imul_imm(self, dst: Reg, src: Reg | Mem, v: int, w: bool = False) -> None:
        v = _s32(v, w)
        short = -128 <= v < 128
        self._op(b"\x6b" if short else b"\x69", dst, src, w)
        self._imm(v, 1 if short else 4)

    def unary(self, op: str, dst: Reg | Mem, w: bool = False) -> None:
        """not/neg; mul/div/idiv of rdx:rax (or edx:eax) by dst."""
        self._op(b"\xf7", UNARY[op], dst, w)

    def shift(self, op: str, dst: Reg | Mem, count: int | None, w: bool = False) -> None:
        """rol/ror/shl/shr/sar by an immediate in [0, width), or by cl when count is None."""
        if count is not None and not 0 <= count < (64 if w else 32):
            raise ValueError(f"shift count {count}")
        if count is None:
            self._op(b"\xd3", SHIFT[op], dst, w)
        elif count == 1:
            self._op(b"\xd1", SHIFT[op], dst, w)
        else:
            self._op(b"\xc1", SHIFT[op], dst, w)
            self.buf.append(count & 0xFF)

    def bt(self, op: str, dst: Reg | Mem, src: Reg, w: bool = False) -> None:
        self._op(bytes([0x0F, 0xA3 + 8 * (BT[op] - 4)]), src, dst, w)

    def bt_imm(self, op: str, dst: Reg | Mem, bit: int, w: bool = False) -> None:
        if not 0 <= bit < (64 if w else 32):
            raise ValueError(f"bit {bit}")
        self._op(b"\x0f\xba", BT[op], dst, w)
        self.buf.append(bit)

    def bsf(self, dst: Reg, src: Reg | Mem, w: bool = False) -> None:
        self._op(b"\x0f\xbc", dst, src, w)

    def bsr(self, dst: Reg, src: Reg | Mem, w: bool = False) -> None:
        self._op(b"\x0f\xbd", dst, src, w)

    # --- SSE/SSE2 scalar (x86-64-v1). xmm registers are numbers 0-15 like the GPRs; `double`
    # selects the sd form (F2) over the ss form (F3).
    def sse(self, op: str, dst: Reg, src: Reg | Mem, double: bool) -> None:
        """addss/sd, subss/sd, mulss/sd, divss/sd, minss/sd, maxss/sd, sqrtss/sd xmm, xmm/m;
        and/or/xor are andps/orps/xorps (bitwise, the same for both widths)."""
        pre = b"" if op in ("and", "or", "xor") else b"\xf2" if double else b"\xf3"
        self._op(bytes([0x0F, SSE[op]]), dst, src, pre=pre)

    def movs(self, dst: Reg, src: Mem, double: bool) -> None:
        """movss/movsd xmm, m32/m64 (a load)."""
        self._op(b"\x0f\x10", dst, src, pre=b"\xf2" if double else b"\xf3")

    def movq_to_xmm(self, dst: Reg, src: Reg, w: bool) -> None:
        """movd xmm, r32 / movq xmm, r64 (the upper part of xmm is zeroed)."""
        self._op(b"\x0f\x6e", dst, src, w, pre=b"\x66")

    def movq_from_xmm(self, dst: Reg, src: Reg, w: bool) -> None:
        """movd r32, xmm (zero-extends) / movq r64, xmm."""
        self._op(b"\x0f\x7e", src, dst, w, pre=b"\x66")

    def ucomis(self, x: Reg, y: Reg | Mem, double: bool) -> None:
        """ucomiss/ucomisd: ZF PF CF = 111 unordered, 100 equal, 001 below, 000 above."""
        self._op(b"\x0f\x2e", x, y, pre=b"\x66" if double else b"")

    def cvtsi2f(self, dst: Reg, src: Reg, double: bool, w: bool) -> None:
        """cvtsi2ss/cvtsi2sd xmm, r32/r64 (signed; rounds by MXCSR, nearest-even)."""
        self._op(b"\x0f\x2a", dst, src, w, pre=b"\xf2" if double else b"\xf3")

    def cvttf2si(self, dst: Reg, src: Reg, double: bool, w: bool) -> None:
        """cvttss2si/cvttsd2si r32/r64, xmm (truncates; NaN or out of range: INT_MIN)."""
        self._op(b"\x0f\x2c", dst, src, w, pre=b"\xf2" if double else b"\xf3")

    def cvtff(self, dst: Reg, src: Reg, double_src: bool) -> None:
        """cvtsd2ss (from double) / cvtss2sd (from single)."""
        self._op(b"\x0f\x5a", dst, src, pre=b"\xf2" if double_src else b"\xf3")

    def cdq(self) -> None:
        self.buf.append(0x99)

    def cqo(self) -> None:
        self.buf += b"\x48\x99"

    # --- stack and control
    def _op_plus_r(self, opcode: int, r: Reg, w: bool) -> None:
        if _reg(r) >= 8 or w:
            self.buf.append(0x40 | w << 3 | r >> 3)
        self.buf.append(opcode + (r & 7))

    def push(self, r: Reg) -> None:
        self._op_plus_r(0x50, r, False)

    def pop(self, r: Reg) -> None:
        self._op_plus_r(0x58, r, False)

    def call(self, target: Label | Reg | Mem) -> None:
        if isinstance(target, Label):
            self.buf.append(0xE8)
            self._rel(target, 4)
        else:
            self._op(b"\xff", 2, target)

    def jmp(self, target: Label | Reg | Mem, short: bool = False) -> None:
        if isinstance(target, Label):
            self.buf.append(0xEB if short else 0xE9)
            self._rel(target, 1 if short else 4)
        else:
            self._op(b"\xff", 4, target)

    def jcc(self, cc: str, target: Label, short: bool = False) -> None:
        if short:
            self.buf.append(0x70 | CC[cc])
        else:
            self.buf += bytes([0x0F, 0x80 | CC[cc]])
        self._rel(target, 1 if short else 4)

    def ret(self) -> None:
        self.buf.append(0xC3)

    def endbr64(self) -> None:
        """A landing pad for indirectly reached entries (CET IBT, R2); a NOP on older CPUs."""
        self.buf += b"\xf3\x0f\x1e\xfa"


def _b(src: Reg | Mem, size: int) -> tuple[Reg, ...]:
    return (src,) if size == 8 and not isinstance(src, Mem) else ()
