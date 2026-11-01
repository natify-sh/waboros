"""The aarch64 backend (S0004): the D26 interface for the integer core of phase 4 (`supported`;
floats only as bit patterns). Register roles in abi.py; x0 = tos (and the status); x9-x13 scratch
inside one method, x16/x17 for indirect branches and offsets no immediate takes, v0 for popcnt;
pool x19-x24 (callee-saved for a host) first, then x2-x8 (saved by the bridge).

i32 values stay zero-extended: 32-bit (w) forms write the whole register (LESSONS 5). Only
`cmp/cmn/ccmp` write the flags, and each compare is emitted right before its consumer (LESSONS 1).
ARMv8.0 only (D24): division checked explicitly (sdiv/udiv do not trap), popcnt through NEON.

Far mode (S0004): a conditional branch reaches ±1 MiB. `func_end` reports a function in which one
does not; compiled again with `far`, every conditional branch to a label becomes `b.!cond` over
an unconditional `b` (±128 MiB)."""

import struct
from collections.abc import Sequence
from dataclasses import dataclass, field

from w2py.targets.base import TrapCode, Unsupported
from w2py.targets.jit.aarch64 import abi
from w2py.targets.jit.aarch64.abi import CTX, FRAME, MBASE, MEM0, MSIZE
from w2py.targets.jit.aarch64.encoder import FP, LR, SP, ZR, Asm, Label, OutOfRange, bitmask, invert
from w2py.targets.jit.common.backend import SWAP, Dst, Imm, Operand, Reg, Slot
from w2py.targets.jit.common.compiler import INTEGER_CORE
from w2py.targets.jit.common.context import (
    HOST_BRIDGE,
    INTERRUPT,
    NSTACK_LIMIT,
    STATUS_INTERRUPT,
    VSTACK_LIMIT,
)

POOL = (19, 20, 21, 22, 23, 24, 2, 3, 4, 5, 6, 7, 8)
CC = {"eq": "eq", "ne": "ne", "lt_s": "lt", "lt_u": "lo", "gt_s": "gt", "gt_u": "hi", "le_s": "le",
      "le_u": "ls", "ge_s": "ge", "ge_u": "hs"}  # fmt: skip
LOGIC = {"and": "and", "or": "orr", "xor": "eor"}
SHIFTS = {"shl": "lslv", "shr_s": "asrv", "shr_u": "lsrv", "rotr": "rorv"}
M32, M64 = 0xFFFFFFFF, (1 << 64) - 1


def _imm12(v: int) -> bool:
    """Does an add/sub/cmp immediate take v (imm12, optionally shifted by 12)?"""
    return 0 <= v < 1 << 12 or (not v & 0xFFF and 0 <= v < 1 << 24)


@dataclass
class Frame:
    pins: tuple[int, ...]
    size: int  # bytes of the native frame: record + pins, a multiple of 16
    unwind: Label
    vcheck: int  # position of the `movz/movk x16, #8 * frame_slots` pair
    traps: dict[int, Label] = field(default_factory=lambda: {})
    start: int = 0  # code offset of the function


class Backend:
    pool = POOL
    tos = 0
    supported = INTEGER_CORE

    def __init__(self) -> None:
        self.a = Asm()
        self.far = False
        self.tables: list[tuple[Label, list[Label], int]] = []  # (table, pads, code offset)

    def new_label(self) -> Label:
        return self.a.label()

    def bind(self, label: Label) -> None:
        self.a.bind(label)

    def jump(self, label: Label) -> None:
        self.a.b(label)

    def here(self) -> int:
        return len(self.a.buf)

    def _add_k(self, d: int, n: int, k: int) -> None:
        """d = n + k (k may be negative); x16 for a constant that no add/sub immediate takes."""
        op, v = ("add", k) if k >= 0 else ("sub", -k)
        try:
            self.a.addsub_imm(op, d, n, v)
        except ValueError:
            self.a.mov_imm(16, k)
            self.a.addsub("add", d, n, 16)

    def _pins(self, f: Frame, load: bool) -> None:
        a, p = self.a, f.pins
        for i in range(0, len(p) - 1, 2):
            a.pair(load, p[i], p[i + 1], SP, 16 + 8 * i)
        if len(p) % 2:
            (a.ld if load else a.st)(p[-1], SP, 16 + 8 * (len(p) - 1))

    def func_begin(self, label: Label, pins: tuple[int, ...] = (), far: bool = False) -> Frame:
        """Landing pad, frame record + pins (sp stays 16-aligned), both stack checks, poll."""
        a = self.a
        assert all(r in POOL for r in pins)
        self.far = far
        a.bind(label)
        a.bti("c")  # reached by blr from the entry and records, by br x16/x17 from tail calls
        f = Frame(pins, 16 + (len(pins) + 1) // 2 * 16, a.label(), 0, start=a.pos(label))
        a.pair(False, FP, LR, SP, -f.size, "pre")
        a.mov(FP, SP)
        self._pins(f, False)
        a.ld(16, CTX, 8 * NSTACK_LIMIT)
        a.cmp(SP, 16)
        self._bcc("lo", self._stub(f, TrapCode.STACK_OVERFLOW))
        f.vcheck = len(a.buf)
        a.movw("movz", 16, 0)  # 8 * frame_slots, patched by func_end
        a.movw("movk", 16, 0, 16)
        a.addsub("add", 16, FRAME, 16)
        a.ld(17, CTX, 8 * VSTACK_LIMIT)
        a.cmp(16, 17)
        self._bcc("hi", self._stub(f, TrapCode.STACK_OVERFLOW))
        self.poll(f)
        return f

    def func_end(self, f: Frame, frame_slots: int) -> bool:
        """Patch the value-stack check, emit the cold trap stubs and the epilogue (`unwind`, with
        w0 intact). False if a conditional branch does not reach (compile again in far mode)."""
        a = self.a
        k = Asm()
        k.movw("movz", 16, 8 * frame_slots & 0xFFFF)
        k.movw("movk", 16, 8 * frame_slots >> 16, 16)
        a.buf[f.vcheck : f.vcheck + 8] = k.buf
        for code, label in f.traps.items():
            a.bind(label)
            a.mov_imm(0, code, wide=False)
            a.b(f.unwind)
        a.bind(f.unwind)
        self._pins(f, True)
        a.pair(True, FP, LR, SP, f.size, "post")
        a.ret()
        return not a.out_of_range(f.start)

    def rewind(self, pos: int) -> None:
        self.a.rewind(pos)
        self.tables = [t for t in self.tables if t[2] < pos]

    def finish(self) -> tuple[bytes, int]:
        """The code, then the data area: jump tables of 32-bit offsets of their pads from the
        table (never executed)."""
        a = self.a
        data = len(a.buf)
        for table, pads, _ in self.tables:
            a.bind(table)
            a.buf += b"".join(struct.pack("<i", a.pos(p) - a.pos(table)) for p in pads)
        try:
            return a.finish(), data
        except OutOfRange as e:  # code beyond ±128 MiB of a bl/b: refused, not a crash (S0004)
            raise Unsupported("jit-aarch64", f"module too large: {e}") from e

    def _bcc(self, cond: str, label: Label) -> None:
        """b.cond label; in far mode over an unconditional branch."""
        if self.far:
            skip = self.a.label()
            self.a.bcc(invert(cond), skip)
            self.a.b(label)
            self.a.bind(skip)
        else:
            self.a.bcc(cond, label)

    def _cbz(self, r: int, label: Label, wide: bool = True, nz: bool = False) -> None:
        if self.far:
            skip = self.a.label()
            self.a.cbz(r, skip, wide, not nz)
            self.a.b(label)
            self.a.bind(skip)
        else:
            self.a.cbz(r, label, wide, nz)

    def _stub(self, f: Frame, code: int) -> Label:
        if code not in f.traps:
            f.traps[code] = self.a.label()
        return f.traps[code]

    def poll(self, f: Frame) -> None:
        """At function entry and loop headers (D14, LESSONS 8)."""
        self.a.ld(16, CTX, 8 * INTERRUPT)
        self._cbz(16, self._stub(f, STATUS_INTERRUPT), nz=True)

    def trap(self, f: Frame, code: int) -> None:
        self.a.b(self._stub(f, code))

    def ret_ok(self, f: Frame) -> None:
        self.a.mov_imm(0, 0, wide=False)
        self.a.b(f.unwind)

    def call_direct(self, f: Frame, label: Label, shift_slots: int) -> None:
        """The callee's frame starts `shift_slots` above ours (frame overlap). A non-zero status
        unwinds this frame too."""
        if shift_slots:
            self._add_k(FRAME, FRAME, 8 * shift_slots)
        self.a.bl(label)
        if shift_slots:
            self._add_k(FRAME, FRAME, -8 * shift_slots)
        self._cbz(0, f.unwind, wide=False, nz=True)

    def host_thunk(self, word: int) -> int:
        at = len(self.a.buf)
        abi.host_thunk(self.a, word)
        return at

    def call_host(self, f: Frame, word: int, slot: int) -> None:
        """Call the libcall in context word `word` through the bridge; its arguments and results
        are slots `slot`, `slot + 1`, ... of this frame."""
        a = self.a
        self._add_k(1, FRAME, 8 * slot)
        a.mov_imm(0, word, wide=False)
        a.ld(16, CTX, 8 * HOST_BRIDGE)
        a.blr(16)
        abi.reload_memory(a)  # memory.grow may have run
        self._cbz(0, f.unwind, wide=False, nz=True)

    def call_record(self, f: Frame, word: int, shift_slots: int) -> None:
        """A function import, through its record in context word `word` (D30): the callee runs
        on the record's context; x28 is saved on the native stack (16 bytes, sp stays aligned),
        x26/x25 come from the callee's context and are reloaded from ours after (as x86-64)."""
        a = self.a
        if shift_slots:
            self._add_k(FRAME, FRAME, 8 * shift_slots)
        self._add_k(16, CTX, 8 * word)
        a.pair(False, CTX, ZR, SP, -16, "pre")
        a.ld(CTX, 16, 16)
        abi.reload_memory(a)
        a.ld(17, 16)
        a.blr(17)
        a.pair(True, CTX, ZR, SP, 16, "post")
        abi.reload_memory(a)
        if shift_slots:
            self._add_k(FRAME, FRAME, -8 * shift_slots)
        self._cbz(0, f.unwind, wide=False, nz=True)

    # ---------------------------------------------------------------- data
    def _word(self, load: bool, t: int, base: int, off: int, size: int = 8) -> None:
        """ldr/str t, [base, #off]; an offset the scaled form does not take goes through x17."""
        a = self.a
        if off // size < 1 << 12:
            (a.ld(t, base, off, size) if load else a.st(t, base, off, size))
        else:
            a.mov_imm(17, off)
            (a.ld_idx(t, base, 17, size) if load else a.st_idx(t, base, 17, size))

    def _r(self, o: Operand, scratch: int, wide: bool, zr: bool = True) -> int:
        """A register holding `o` (a slot or an immediate goes to `scratch`; i32 from a slot as a
        32-bit load); the zero register for an immediate 0 if `zr`."""
        if isinstance(o, Reg):
            return o.id
        if isinstance(o, Slot):
            self._word(True, scratch, FRAME, 8 * o.k, 8 if wide else 4)
            return scratch
        v = o.value & (M64 if wide else M32)
        if v == 0 and zr:
            return ZR
        self.a.mov_imm(scratch, v, wide)
        return scratch

    def move(self, dst: Dst, src: Operand, wide: bool) -> None:
        """A register gets the operand's width (32-bit zero-extends); a slot always gets 64 bits."""
        if isinstance(dst, Reg):
            if isinstance(src, Imm):
                self.a.mov_imm(dst.id, src.value & (M64 if wide else M32), wide)
            elif isinstance(src, Reg):
                if src.id != dst.id:
                    self.a.mov(dst.id, src.id, wide)
            else:
                self._r(src, dst.id, wide)
        elif not (isinstance(src, Slot) and src.k == dst.k):
            self._word(False, self._r(src, 16, True), FRAME, 8 * dst.k)

    def binop(self, op: str, dst: Reg, x: Operand, y: Operand, wide: bool) -> None:
        a, d, w = self.a, dst.id, wide
        bits = 64 if w else 32
        if isinstance(y, Imm):
            v = y.value & ((1 << bits) - 1)
            neg = -v & ((1 << bits) - 1)
            if op in SHIFTS or op == "rotl":  # constant counts masked here, as wasm does
                xr, n = self._r(x, 9, w), v & (bits - 1)
                if not n:
                    a.mov(d, xr, w)
                elif op == "shl":
                    a.bfm("ubfm", d, xr, bits - n, bits - 1 - n, w)
                elif op in ("shr_u", "shr_s"):
                    a.bfm("ubfm" if op == "shr_u" else "sbfm", d, xr, n, bits - 1, w)
                else:
                    a.extr(d, xr, xr, n if op == "rotr" else bits - n, w)
                return
            if op in ("add", "sub") and (_imm12(v) or _imm12(neg)):
                xr = self._r(x, 9, w, zr=False)
                flip = {"add": "sub", "sub": "add"}
                a.addsub_imm(op if _imm12(v) else flip[op], d, xr, v if _imm12(v) else neg, w)
                return
            if op in LOGIC and bitmask(v, w) is not None:
                a.logic_imm(LOGIC[op], d, self._r(x, 9, w), v, w)
                return
        xr, yr = self._r(x, 9, w), self._r(y, 10, w)
        if op == "mul":
            a.madd(d, xr, yr, ZR, w)
        elif op in ("add", "sub"):
            a.addsub(op, d, xr, yr, w)
        elif op in LOGIC:
            a.logic(LOGIC[op], d, xr, yr, w)
        elif op == "rotl":  # rotl by y = rotr by -y (rorv takes the count modulo the width)
            a.addsub("sub", 11, ZR, yr, w)
            a.dp2("rorv", d, xr, 11, w)
        else:
            a.dp2(SHIFTS[op], d, xr, yr, w)

    def divrem(self, f: Frame, op: str, dst: Reg, x: Operand, y: Operand, wide: bool) -> None:
        """sdiv/udiv do not trap (x / 0 = 0, INT_MIN / -1 = INT_MIN): explicit checks (S0004).
        rem = x - (x / y) * y, which is 0 for INT_MIN rem -1 with no extra check."""
        a, w, mask = self.a, wide, M64 if wide else M32
        signed, rem = op.endswith("_s"), op.startswith("rem")
        known = y.value & mask if isinstance(y, Imm) else None
        xr, yr = self._r(x, 9, w), self._r(y, 10, w)
        if not known:
            self._cbz(yr, self._stub(f, TrapCode.INTEGER_DIVISION_BY_ZERO), w)
        if signed and not rem and (known is None or known == mask):
            a.addsub_imm("adds", ZR, yr, 1, w)  # cmn y, #1: eq iff y == -1
            a.ccmp(xr, 1, 0, "eq", w, imm=True)  # then x - 1 overflows iff x == INT_MIN
            self._bcc("vs", self._stub(f, TrapCode.INTEGER_OVERFLOW))
        div = "sdiv" if signed else "udiv"
        if rem:
            a.dp2(div, 11, xr, yr, w)
            a.madd(dst.id, 11, yr, xr, w, sub=True)
        else:
            a.dp2(div, dst.id, xr, yr, w)

    def unop(self, op: str, dst: Reg, x: Operand, wide: bool) -> None:
        a, d, w = self.a, dst.id, wide
        if op in ("extend_i32_u", "wrap"):  # the low 32 bits, zero-extended, even onto itself
            if isinstance(x, Reg):
                a.mov(d, x.id, False)
            else:
                self.move(dst, x, False)
            return
        xr = self._r(x, 9, w)
        if op == "clz":
            a.dp1("clz", d, xr, w)
        elif op == "ctz":  # 32 / 64 for 0, as wasm
            a.dp1("rbit", 9, xr, w)
            a.dp1("clz", d, 9, w)
        elif op == "popcnt":  # Advanced SIMD is in the ARMv8.0 baseline
            a.fmov_to(0, xr, w)
            a.cnt8b(0, 0)
            a.addv8b(0, 0)
            a.fmov_from(d, 0, False)
        elif op in ("extend8_s", "extend16_s"):
            a.bfm("sbfm", d, xr, 0, 7 if op == "extend8_s" else 15, w)
        else:
            assert op in ("extend32_s", "extend_i32_s"), op
            a.bfm("sbfm", d, xr, 0, 31, True)

    def _cmp(self, cond: str, x: Operand, y: Operand, wide: bool) -> str:
        """Emit the compare (the consumer must follow at once); the A64 condition to test."""
        a, w, mask = self.a, wide, M64 if wide else M32
        if isinstance(x, Imm) and not isinstance(y, Imm):
            x, y, cond = y, x, SWAP[cond]
        xr = self._r(x, 9, w, zr=False)
        if isinstance(y, Imm):
            v, neg = y.value & mask, -y.value & mask
            if _imm12(v):
                a.addsub_imm("subs", ZR, xr, v, w)
                return CC[cond]
            if _imm12(neg) and v != (mask >> 1) + 1:  # cmn: the same flags, except for INT_MIN
                a.addsub_imm("adds", ZR, xr, neg, w)
                return CC[cond]
        a.cmp(xr, self._r(y, 10, w), w)
        return CC[cond]

    def cmp_set(self, cond: str, dst: Reg, x: Operand, y: Operand, wide: bool) -> None:
        self.a.cset(dst.id, self._cmp(cond, x, y, wide))

    def cmp_branch(self, cond: str, x: Operand, y: Operand, label: Label, wide: bool) -> None:
        if cond in ("eq", "ne") and Imm(0) in (x, y):  # cbz/cbnz: no flags
            r = self._r(y if x == Imm(0) else x, 9, wide)
            self._cbz(r, label, wide, cond == "ne")
            return
        self._bcc(self._cmp(cond, x, y, wide), label)

    def select_cmp(
        self, cond: str, x: Operand, y: Operand, wc: bool, dst: Reg, t: Operand, e: Operand,
        wv: bool,
    ) -> None:  # fmt: skip
        """dst = t if (x cond y) else e; both values are already computed (LESSONS 28) and are
        in registers before the compare, so `csel` follows it directly (LESSONS 1)."""
        tr, er = self._r(t, 11, wv), self._r(e, 12, wv)
        self.a.csel("csel", dst.id, tr, er, self._cmp(cond, x, y, wc), wv)

    def jump_table(self, index: Operand, labels: Sequence[Label], default: Label) -> None:
        """Jump to labels[index], or default when index >= len(labels). From 4 entries on, a
        table of 32-bit offsets from the table in the data area (S0004: `adrp`, so the code is
        loaded at an address = 0 mod 4096); each entry lands on a `bti j` pad that jumps on."""
        a = self.a
        ir = self._r(index, 9, False, zr=False)
        if len(labels) < 4:
            for i, label in enumerate(labels):
                a.addsub_imm("subs", ZR, ir, i, False)
                self._bcc("eq", label)
            a.b(default)
            return
        n = len(labels)
        if _imm12(n):
            a.addsub_imm("subs", ZR, ir, n, False)
        else:
            a.mov_imm(10, n, False)
            a.cmp(ir, 10, False)
        self._bcc("hs", default)
        table = a.label()
        a.adrp(16, table)
        a.add_lo12(16, 16, table)
        a.ld_idx(17, 16, ir, 4, signed=True, ext="uxtw", scaled=True)
        a.addsub("add", 16, 16, 17)
        a.br(16)
        pads: dict[Label, Label] = {}
        for label in labels:
            if label not in pads:
                pads[label] = a.label()
                a.bind(pads[label])
                a.bti("j")
                a.b(label)
        self.tables.append((table, [pads[label] for label in labels], len(a.buf)))

    # ---------------------------------------------------------------- memories and context
    def _mem(self, mem: int) -> tuple[int, int]:
        """(base, size in bytes) registers of the memory whose base is context word `mem`:
        memory 0 is pinned in x26/x25; another is read into x11/x12 at each use."""
        if 8 * mem == MEM0:
            return MBASE, MSIZE
        if 8 * mem < 504:
            self.a.pair(True, 11, 12, CTX, 8 * mem)
        else:
            self._word(True, 11, CTX, 8 * mem)
            self._word(True, 12, CTX, 8 * mem + 8)
        return 11, 12

    def memory_size(self, dst: Reg, mem: int) -> None:
        self.a.bfm("ubfm", dst.id, self._mem(mem)[1], 16, 63)  # lsr #16

    def _bounds(
        self, f: Frame, addr: Operand, offset: int, size: int, mem: int
    ) -> tuple[int, int | None, int]:
        """The bounds check (R3: the one place memory safety rests on). addr + offset (< 2**33)
        and its end are computed in 64 bits, so nothing wraps; the end must be <= the memory's
        size or the access traps. A register address is copied with a 32-bit move first, so its
        upper half cannot matter. Returns the access as (base, index register or None, offset)."""
        a = self.a
        base, limit = self._mem(mem)
        oob = self._stub(f, TrapCode.MEMORY_OUT_OF_BOUNDS)
        if isinstance(addr, Imm):
            ea = (addr.value & M32) + offset
            if _imm12(ea + size):
                a.addsub_imm("subs", ZR, limit, ea + size)
            else:
                a.mov_imm(10, ea + size)
                a.cmp(limit, 10)
            self._bcc("lo", oob)
            if not ea % size and ea // size < 1 << 12:
                return base, None, ea
            a.mov_imm(9, ea)
            return base, 9, 0
        if isinstance(addr, Reg):
            a.mov(9, addr.id, False)
        else:
            self._r(addr, 9, False)
        if offset:
            self._add_k(9, 9, offset)
        a.addsub_imm("add", 10, 9, size)
        a.cmp(10, limit)
        self._bcc("hi", oob)
        return base, 9, 0

    def load(
        self, f: Frame, size: int, signed: bool, wide: bool, dst: Reg, addr: Operand, offset: int,
        mem: int,
    ) -> None:  # fmt: skip
        base, index, off = self._bounds(f, addr, offset, size, mem)
        if index is None:
            self.a.ld(dst.id, base, off, size, signed, wide)
        else:
            self.a.ld_idx(dst.id, base, index, size, signed, wide)

    def store(
        self, f: Frame, size: int, addr: Operand, offset: int, src: Operand, mem: int
    ) -> None:  # fmt: skip
        base, index, off = self._bounds(f, addr, offset, size, mem)
        r = self._r(src, 13, size == 8)
        if index is None:
            self.a.st(r, base, off, size)
        else:
            self.a.st_idx(r, base, index, size)

    def ctx_load(self, dst: Reg, word: int, indirect: bool) -> None:
        if indirect:
            self._word(True, 9, CTX, 8 * word)
            self.a.ld(dst.id, 9)
        else:
            self._word(True, dst.id, CTX, 8 * word)

    def ctx_store(self, word: int, src: Operand, indirect: bool) -> None:
        r = self._r(src, 10, True)
        if indirect:
            self._word(True, 9, CTX, 8 * word)
            self.a.st(r, 9)
        else:
            self._word(False, r, CTX, 8 * word)
