"""AArch64 instruction encoder (S0004): the A64 forms the phase-4 backend and the trampolines need,
ARMv8.0-A only (D24; `bti` is a HINT, a NOP before ARMv8.5). Fixed 32-bit little-endian words;
branches and PC-relative addresses go through labels and fixups only (LESSONS 6). Every form is
checked against `llvm-mc -triple=aarch64 -show-encoding` (tests/jit/test_encoder_aarch64.py).
Encodings: Arm ARM DDI 0487, section C4 ("A64 instruction set encoding"); the bitmask-immediate
search follows the description there (as Cranelift's `ImmLogic::maybe_from_u64` does), written anew.

Registers are numbers: 0-30 general purpose, `ZR` (31: xzr/wzr) where the field means the zero
register and `SP` (encoded as 31) where it means the stack pointer; passing one where the other is
meant raises. SIMD&FP registers (`fmov`, `cnt`, `addv`, the FP forms) are 0-31. `wide=True`
selects the 64-bit (x) form, else the 32-bit (w) form, whose result is zero-extended into the x
register; `double` selects d (else s) registers."""

import struct
from dataclasses import dataclass

FP, LR, ZR, SP = 29, 30, 31, 32
COND = {"eq": 0, "ne": 1, "hs": 2, "lo": 3, "mi": 4, "pl": 5, "vs": 6, "vc": 7,
        "hi": 8, "ls": 9, "ge": 10, "lt": 11, "gt": 12, "le": 13, "al": 14}  # fmt: skip
SHIFT = {"lsl": 0, "lsr": 1, "asr": 2, "ror": 3}
EXTEND = {"uxtb": 0, "uxth": 1, "uxtw": 2, "uxtx": 3, "sxtb": 4, "sxth": 5, "sxtw": 6, "sxtx": 7}
ADDSUB = {"add": 0, "adds": 1, "sub": 2, "subs": 3}  # op:S
LOGIC = {"and": 0, "orr": 1, "eor": 2, "ands": 3}
DP2 = {"udiv": 2, "sdiv": 3, "lslv": 8, "lsrv": 9, "asrv": 10, "rorv": 11}
CSEL = {"csel": 0, "csinc": 1, "csinv": 2, "csneg": 3}  # op:o2
FP2 = {"mul": 0, "div": 1, "add": 2, "sub": 3, "max": 4, "min": 5}
FP1 = {"sqrt": 3, "frintn": 8, "frintp": 9, "frintm": 10, "frintz": 11}
# branch fixup kinds: (bits of the field, its shift in the word, scale of the offset)
_FIELD = {"b26": (26, 0, 4), "b19": (19, 5, 4), "b14": (14, 5, 4), "adr": (21, 0, 1)}


class OutOfRange(ValueError):
    """A branch or PC-relative fixup whose target is beyond the form's range (S0004: far mode)."""


@dataclass(frozen=True, slots=True)
class Label:
    id: int


def _rz(r: int) -> int:
    """A field where 31 is the zero register."""
    if not 0 <= r <= ZR:
        raise ValueError(f"register {r} is not allowed here (sp?)")
    return r


def _rs(r: int) -> int:
    """A field where 31 is the stack pointer."""
    if r == SP:
        return 31
    if not 0 <= r < ZR:
        raise ValueError(f"register {r} is not allowed here (zr?)")
    return r


def _u(v: int, bits: int, what: str) -> int:
    if not 0 <= v < 1 << bits:
        raise ValueError(f"{what} {v} does not fit {bits} bits")
    return v


def bitmask(value: int, wide: bool) -> int | None:
    """N:immr:imms (13 bits) of a logical immediate, or None if `value` is not encodable: a
    replicated element of 2-64 bits holding one rotated run of ones (not all 0s or all 1s)."""
    width = 64 if wide else 32
    value &= (1 << width) - 1
    if value in (0, (1 << width) - 1):
        return None
    size = width
    while size > 2 and value & ((1 << size // 2) - 1) == value >> size // 2 & (
        (1 << size // 2) - 1
    ):
        size //= 2
    mask = (1 << size) - 1
    elem = value & mask
    if any((value >> i) & mask != elem for i in range(0, width, size)):
        return None
    ones = elem.bit_count()
    for r in range(size):  # rotate right by r until the run starts at bit 0
        if ((elem >> r) | (elem << (size - r))) & mask == (1 << ones) - 1:
            immr = (size - r) % size
            imms = (-size << 1 & 0x3F) | (ones - 1)
            return (size == 64) << 12 | immr << 6 | imms
    return None


def patch(word: int, kind: str, rel: int) -> int:
    """Put the PC-relative offset `rel` (bytes; pages for `adrp`) into the field of `kind`."""
    if kind == "lo12":
        return word | (rel & 0xFFF) << 10
    bits, shift, scale = _FIELD["adr" if kind == "adrp" else kind]
    if rel % scale or not -(1 << bits - 1) <= rel // scale < 1 << bits - 1:
        raise OutOfRange(f"{kind} offset {rel} out of range")
    v = rel // scale & (1 << bits) - 1
    if bits == 21:  # adr/adrp: immlo (2 bits) at 29, immhi (19 bits) at 5
        return word | (v & 3) << 29 | (v >> 2) << 5
    return word | v << shift


class Asm:
    def __init__(self) -> None:
        self.buf = bytearray()
        self._labels: list[int | None] = []
        self._fixups: list[tuple[int, str, Label]] = []

    # --- labels
    def label(self) -> Label:
        self._labels.append(None)
        return Label(len(self._labels) - 1)

    def bind(self, label: Label) -> None:
        if self._labels[label.id] is not None:
            raise ValueError(f"label {label.id} bound twice")
        self._labels[label.id] = len(self.buf)

    def pad_here(self, n: int) -> None:
        """n bytes of `nop`; labels bound here move past them (code-placement study only)."""
        at = len(self.buf)
        self.buf += struct.pack("<I", 0xD503201F) * (n // 4)
        self._labels = [at + n // 4 * 4 if p == at else p for p in self._labels]

    def emit(self, word: int) -> None:
        self.buf += struct.pack("<I", word)

    def _fix(self, word: int, kind: str, label: Label) -> None:
        self._fixups.append((len(self.buf), kind, label))
        self.emit(word)

    def pos(self, label: Label) -> int:
        at = self._labels[label.id]
        assert at is not None, f"label {label.id} not bound"
        return at

    def out_of_range(self, start: int) -> bool:
        """Does a conditional branch (b19, b14) emitted from `start` on miss its bound label?"""
        for pos, kind, label in self._fixups:
            target = self._labels[label.id]
            if pos >= start and kind in ("b19", "b14") and target is not None:
                bits = _FIELD[kind][0] + 1  # signed words -> bytes: field bits + 2 - 1
                if not -(1 << bits) <= target - pos < 1 << bits:
                    return True
        return False

    def rewind(self, start: int) -> None:
        """Drop the code from `start` on: its bytes, its fixups and the labels bound there."""
        del self.buf[start:]
        self._fixups = [x for x in self._fixups if x[0] < start]
        self._labels = [None if p is not None and p >= start else p for p in self._labels]

    def finish(self) -> bytes:
        """Resolve every fixup. `adrp` assumes the code is loaded at an address = 0 mod 4096."""
        for pos, kind, label in self._fixups:
            target = self._labels[label.id]
            if target is None:
                raise ValueError(f"label {label.id} never bound")
            rel = {"adrp": (target >> 12) - (pos >> 12), "lo12": target}.get(kind, target - pos)
            (word,) = struct.unpack_from("<I", self.buf, pos)
            struct.pack_into("<I", self.buf, pos, patch(word, kind, rel))
        return bytes(self.buf)

    def _rr(self, top: int, m: int, mid: int, n: int, d: int) -> None:
        """The common layout: `top` (bits 31-21) | m << 16 | mid << 10 | n << 5 | d."""
        self.emit(top | m << 16 | mid << 10 | n << 5 | d)

    # --- arithmetic and logic
    def addsub_imm(self, op: str, d: int, n: int, imm: int, wide: bool = True) -> None:
        """add/adds/sub/subs d, n, #imm: imm12, or imm12 << 12."""
        sh = imm >= 1 << 12 and not imm & 0xFFF
        imm12 = _u(imm >> 12 if sh else imm, 12, "add/sub immediate")
        rd = _rz(d) if ADDSUB[op] & 1 else _rs(d)
        self._rr(wide << 31 | ADDSUB[op] << 29 | 0x22 << 23 | sh << 22, 0, imm12, _rs(n), rd)

    def addsub(self, op: str, d: int, n: int, m: int, wide: bool = True, sh: str = "lsl",
               amount: int = 0) -> None:  # fmt: skip
        """add/adds/sub/subs d, n, m{, sh #amount} (shifted register; no sp)."""
        assert sh != "ror", "ror is reserved for add/sub"
        top = wide << 31 | ADDSUB[op] << 29 | 0x0B << 24 | SHIFT[sh] << 22
        self._rr(top, _rz(m), _u(amount, 5 + wide, "shift"), _rz(n), _rz(d))

    def addsub_ext(self, op: str, d: int, n: int, m: int, ext: str, amount: int = 0,
                   wide: bool = True) -> None:  # fmt: skip
        """add/adds/sub/subs d, n, m, ext #amount (extended register: n and d may be sp)."""
        rd = _rz(d) if ADDSUB[op] & 1 else _rs(d)
        if amount > 4:
            raise ValueError(f"extend amount {amount} > 4")
        top = wide << 31 | ADDSUB[op] << 29 | 0x59 << 21
        self._rr(top, _rz(m), EXTEND[ext] << 3 | amount, _rs(n), rd)

    def cmp(self, n: int, m: int, wide: bool = True) -> None:
        """subs zr, n, m; with n = sp the extended form (cmp sp, x16)."""
        if n == SP:
            self.addsub_ext("subs", ZR, n, m, "uxtx" if wide else "uxtw", 0, wide)
        else:
            self.addsub("subs", ZR, n, m, wide)

    def logic(self, op: str, d: int, n: int, m: int, wide: bool = True, sh: str = "lsl",
              amount: int = 0) -> None:  # fmt: skip
        """and/orr/eor/ands d, n, m{, sh #amount}."""
        top = wide << 31 | LOGIC[op] << 29 | 0x0A << 24 | SHIFT[sh] << 22
        self._rr(top, _rz(m), _u(amount, 5 + wide, "shift"), _rz(n), _rz(d))

    def logic_imm(self, op: str, d: int, n: int, imm: int, wide: bool = True) -> None:
        """and/orr/eor/ands d, n, #bitmask; ValueError if imm is not a bitmask immediate."""
        enc = bitmask(imm, wide)
        if enc is None:
            raise ValueError(f"{imm:#x} is not a logical immediate")
        rd = _rz(d) if op == "ands" else _rs(d)
        self.emit(wide << 31 | LOGIC[op] << 29 | 0x24 << 23 | enc << 10 | _rz(n) << 5 | rd)

    def mov(self, d: int, n: int, wide: bool = True) -> None:
        """mov d, n: `orr d, zr, n`, or `add d, n, #0` when either is sp. `mov wD, wS` clears
        the upper half (LESSONS 5)."""
        if SP in (d, n):
            self.addsub_imm("add", d, n, 0, wide)
        else:
            self.logic("orr", d, ZR, n, wide)

    def movw(self, op: str, d: int, imm16: int, shift: int = 0, wide: bool = True) -> None:
        """movn/movz/movk d, #imm16, lsl #shift."""
        opc = {"movn": 0, "movz": 2, "movk": 3}[op]
        if shift % 16 or not 0 <= shift < (64 if wide else 32):
            raise ValueError(f"move-wide shift {shift}")
        imm16 = _u(imm16, 16, "imm16")
        self.emit(wide << 31 | opc << 29 | 0x25 << 23 | shift // 16 << 21 | imm16 << 5 | _rz(d))

    def mov_imm(self, d: int, value: int, wide: bool = True) -> None:
        """d = value (of the width; signed or unsigned) by the shortest sequence: movz or movn
        plus movk for each other chunk, or one `orr d, zr, #bitmask`. No flags are written."""
        width = 64 if wide else 32
        value &= (1 << width) - 1
        chunks = [value >> s & 0xFFFF for s in range(0, width, 16)]
        zeros, ones = chunks.count(0), chunks.count(0xFFFF)
        if max(zeros, ones) < len(chunks) - 1 and bitmask(value, wide) is not None:
            self.logic_imm("orr", d, ZR, value, wide)
            return
        inv = ones > zeros  # movn: start from all ones
        skip = 0xFFFF if inv else 0
        first = next((i for i, c in enumerate(chunks) if c != skip), 0)
        self.movw("movn" if inv else "movz", d, chunks[first] ^ skip, 16 * first, wide)
        for i, c in enumerate(chunks):
            if i != first and c != skip:
                self.movw("movk", d, c, 16 * i, wide)

    def madd(self, d: int, n: int, m: int, a: int, wide: bool = True, sub: bool = False) -> None:
        """madd/msub d, n, m, a: d = a ± n * m (mul = madd with a = zr)."""
        self._rr(wide << 31 | 0x1B << 24, _rz(m), sub << 5 | _rz(a), _rz(n), _rz(d))

    def adc(self, d: int, n: int, m: int, wide: bool = True, sub: bool = False) -> None:
        """adc/sbc d, n, m: d = n + m + C, or n - m - 1 + C (C = NOT borrow after subs)."""
        self._rr(wide << 31 | sub << 30 | 0xD0 << 21, _rz(m), 0, _rz(n), _rz(d))

    def mulh(self, d: int, n: int, m: int, signed: bool) -> None:
        """umulh/smulh d, n, m: the high 64 bits of the 128-bit product (64-bit only)."""
        self._rr((0x4DA if signed else 0x4DE) << 21, _rz(m), 0x1F, _rz(n), _rz(d))

    def dp2(self, op: str, d: int, n: int, m: int, wide: bool = True) -> None:
        """udiv sdiv lslv lsrv asrv rorv (shift counts are taken modulo the width)."""
        self._rr(wide << 31 | 0xD6 << 21, _rz(m), DP2[op], _rz(n), _rz(d))

    def dp1(self, op: str, d: int, n: int, wide: bool = True) -> None:
        """rbit, clz."""
        self._rr(wide << 31 | 1 << 30 | 0xD6 << 21, 0, {"rbit": 0, "clz": 4}[op], _rz(n), _rz(d))

    def bfm(self, op: str, d: int, n: int, immr: int, imms: int, wide: bool = True) -> None:
        """sbfm/ubfm (lsl/lsr/asr #k, sxtb/sxth/sxtw, uxtb/uxth are aliases)."""
        top = wide << 31 | {"sbfm": 0, "ubfm": 2}[op] << 29 | 0x26 << 23 | wide << 22
        self._rr(top, _u(immr, 5 + wide, "immr"), _u(imms, 5 + wide, "imms"), _rz(n), _rz(d))

    def extr(self, d: int, n: int, m: int, lsb: int, wide: bool = True) -> None:
        """extr d, n, m, #lsb (ror #k = extr d, n, n, #k)."""
        top = wide << 31 | 0x27 << 23 | wide << 22
        self._rr(top, _rz(m), _u(lsb, 5 + wide, "lsb"), _rz(n), _rz(d))

    def csel(self, op: str, d: int, n: int, m: int, cond: str, wide: bool = True) -> None:
        """csel/csinc/csinv/csneg d, n, m, cond."""
        o = CSEL[op]
        top = wide << 31 | (o >> 1) << 30 | 0xD4 << 21
        self._rr(top, _rz(m), COND[cond] << 2 | (o & 1), _rz(n), _rz(d))

    def cset(self, d: int, cond: str, wide: bool = False) -> None:
        """d = 1 if cond else 0 (csinc d, zr, zr, !cond)."""
        self.csel("csinc", d, ZR, ZR, invert(cond), wide)

    def ccmp(self, n: int, m: int, nzcv: int, cond: str, wide: bool = True, imm: bool = False,
             neg: bool = False) -> None:  # fmt: skip
        """ccmp/ccmn n, m (a register, or #m < 32 with imm=True), #nzcv, cond."""
        rm = _u(m, 5, "ccmp immediate") if imm else _rz(m)
        top = wide << 31 | (not neg) << 30 | 0x1D2 << 21
        self._rr(top, rm, COND[cond] << 2 | imm << 1, _rz(n), _u(nzcv, 4, "nzcv"))

    # --- loads and stores
    @staticmethod
    def _opc(size: int, load: bool, signed: bool, wide: bool) -> int:
        """size:opc (bits 31-30, 23-22) of a load/store of `size` bytes."""
        sz = {1: 0, 2: 1, 4: 2, 8: 3}[size] << 30
        if not load or not signed:
            return sz | load << 22
        if size == 8 or (size == 4 and not wide):
            raise ValueError(f"no signed {size}-byte load to {'x' if wide else 'w'}")
        return sz | (2 if wide else 3) << 22

    def _ldst(self, t: int, base: int, off: int, size: int, load: bool, signed: bool,
              wide: bool) -> None:  # fmt: skip
        top = self._opc(size, load, signed, wide)
        if off >= 0 and not off % size and off // size < 1 << 12:  # scaled unsigned offset
            self._rr(top | 0x39 << 24, 0, off // size, _rs(base), _rz(t))
        elif -256 <= off < 256:  # ldur/stur
            self._rr(top | 0x38 << 24, (off & 0x1FF) >> 4, (off & 15) << 2, _rs(base), _rz(t))
        else:
            raise ValueError(f"offset {off} not encodable for a {size}-byte access")

    def ld(self, t: int, base: int, off: int = 0, size: int = 8, signed: bool = False,
           wide: bool = True) -> None:  # fmt: skip
        """ldr/ldrb/ldrh/ldrsb/ldrsh/ldrsw t, [base, #off]: the scaled form, else ldur."""
        self._ldst(t, base, off, size, True, signed, wide)

    def st(self, t: int, base: int, off: int = 0, size: int = 8) -> None:
        self._ldst(t, base, off, size, False, False, True)

    def _ldst_idx(self, t: int, base: int, index: int, size: int, load: bool, signed: bool,
                  wide: bool, ext: str, scaled: bool) -> None:  # fmt: skip
        top = self._opc(size, load, signed, wide) | 0x38 << 24 | 1 << 21
        option = {"uxtw": 2, "lsl": 3, "sxtw": 6, "sxtx": 7}[ext]
        self._rr(top, _rz(index), option << 3 | scaled << 2 | 2, _rs(base), _rz(t))

    def ld_idx(self, t: int, base: int, index: int, size: int = 8, signed: bool = False,
               wide: bool = True, ext: str = "lsl", scaled: bool = False) -> None:  # fmt: skip
        """t = [base, index{, ext #log2(size) if scaled}] (uxtw/sxtw take a w index)."""
        self._ldst_idx(t, base, index, size, True, signed, wide, ext, scaled)

    def st_idx(self, t: int, base: int, index: int, size: int = 8, ext: str = "lsl",
               scaled: bool = False) -> None:  # fmt: skip
        self._ldst_idx(t, base, index, size, False, False, True, ext, scaled)

    def pair(
        self, load: bool, t1: int, t2: int, base: int, off: int = 0, mode: str = "off"
    ) -> None:
        """ldp/stp of x registers: "off" [base, #off], "pre" [base, #off]!, "post" [base], #off."""
        if off % 8 or not -512 <= off < 512:
            raise ValueError(f"pair offset {off}")
        top = 2 << 30 | 5 << 27 | {"post": 1, "off": 2, "pre": 3}[mode] << 23 | load << 22
        self._rr(top, (off // 8 & 0x7F) >> 1, (off // 8 & 1) << 5 | _rz(t2), _rs(base), _rz(t1))

    # --- branches
    def b(self, label: Label) -> None:
        self._fix(0x14000000, "b26", label)

    def bl(self, label: Label) -> None:
        self._fix(0x94000000, "b26", label)

    def bcc(self, cond: str, label: Label) -> None:
        self._fix(0x54000000 | COND[cond], "b19", label)

    def cbz(self, r: int, label: Label, wide: bool = True, nz: bool = False) -> None:
        self._fix(wide << 31 | 0x34 << 24 | nz << 24 | _rz(r), "b19", label)

    def tbz(self, r: int, bit: int, label: Label, nz: bool = False) -> None:
        """tbz/tbnz: ±32 KiB, short skips inside one method only (S0004)."""
        _u(bit, 6, "bit")
        self._fix(
            (bit >> 5) << 31 | 0x36 << 24 | nz << 24 | (bit & 31) << 19 | _rz(r), "b14", label
        )

    def br(self, r: int) -> None:
        self.emit(0xD61F0000 | _rz(r) << 5)

    def blr(self, r: int) -> None:
        self.emit(0xD63F0000 | _rz(r) << 5)

    def ret(self, r: int = LR) -> None:
        self.emit(0xD65F0000 | _rz(r) << 5)

    def adr(self, d: int, label: Label) -> None:
        self._fix(0x10000000 | _rz(d), "adr", label)

    def adrp(self, d: int, label: Label) -> None:
        self._fix(0x90000000 | _rz(d), "adrp", label)

    def add_lo12(self, d: int, n: int, label: Label) -> None:
        """add d, n, #:lo12:label (after `adrp`)."""
        self._fix(1 << 31 | 0x22 << 23 | _rs(n) << 5 | _rs(d), "lo12", label)

    def bti(self, kind: str = "c") -> None:
        """Landing pad (R2): `c` for blr/br x16/x17 targets, `j` for jump-table pads."""
        self.emit({"c": 0xD503245F, "j": 0xD503249F}[kind])

    # --- Advanced SIMD and FP (baseline)
    def fmov_to(self, v: int, r: int, wide: bool = True) -> None:
        """fmov d<v>, x<r> (s<v>, w<r> if not wide)."""
        self.emit((0x9E670000 if wide else 0x1E270000) | _rz(r) << 5 | _u(v, 5, "v"))

    def fmov_from(self, r: int, v: int, wide: bool = True) -> None:
        """fmov x<r>, d<v> (w<r>, s<v> if not wide)."""
        self.emit((0x9E660000 if wide else 0x1E260000) | _u(v, 5, "v") << 5 | _rz(r))

    def cnt8b(self, d: int, n: int) -> None:
        self.emit(0x0E205800 | _u(n, 5, "v") << 5 | _u(d, 5, "v"))

    def addv8b(self, d: int, n: int) -> None:
        self.emit(0x0E31B800 | _u(n, 5, "v") << 5 | _u(d, 5, "v"))

    def fp2(self, op: str, d: int, n: int, m: int, double: bool) -> None:
        """fadd fsub fmul fdiv fmin fmax d, n, m (s or d registers)."""
        top = 0x1E200800 | double << 22 | FP2[op] << 12
        self._rr(top, _u(m, 5, "v"), 0, _u(n, 5, "v"), _u(d, 5, "v"))

    def fp1(self, op: str, d: int, n: int, double: bool) -> None:
        """fsqrt, frintn/p/m/z d, n; `fcvt` converts n (of the `double` type) to the other type."""
        code = (4 if double else 5) if op == "fcvt" else FP1[op]
        self.emit(0x1E204000 | double << 22 | code << 15 | _u(n, 5, "v") << 5 | _u(d, 5, "v"))

    def fcmp(self, n: int, m: int, double: bool) -> None:
        self.emit(0x1E202000 | double << 22 | _u(m, 5, "v") << 16 | _u(n, 5, "v") << 5)

    def fcvt_int(self, op: str, r: int, v: int, wide: bool, double: bool) -> None:
        """fcvtzs/fcvtzu r, v (saturating, NaN -> 0); scvtf/ucvtf v, r (one rounding)."""
        rmode, code = {"fcvtzs": (3, 0), "fcvtzu": (3, 1), "scvtf": (0, 2), "ucvtf": (0, 3)}[op]
        d, n = (_rz(r), _u(v, 5, "v")) if rmode else (_u(v, 5, "v"), _rz(r))
        self.emit(wide << 31 | 0x1E200000 | double << 22 | rmode << 19 | code << 16 | n << 5 | d)


def invert(cond: str) -> str:
    return [*COND][COND[cond] ^ 1]
