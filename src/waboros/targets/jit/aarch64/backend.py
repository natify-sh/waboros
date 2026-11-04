"""The aarch64 backend (S0004): the D26 interface, the same opcode set as x86-64 (`supported`,
step 4e). Register roles in abi.py; x0 = tos (and the status); x9-x15 scratch inside one method,
x16/x17 for indirect branches and offsets no immediate takes, v0/v1 for popcnt and floats
(values stay bit patterns in x registers and slots); pool x19-x24 (callee-saved for a host)
first, then x2-x8 (saved by the bridge).

i32 values stay zero-extended: 32-bit (w) forms write the whole register (LESSONS 5). Only
`cmp/cmn/ccmp/fcmp` and `wide`'s `adds/subs` write the flags, each emitted right before its
consumer (LESSONS 1). ARMv8.0 only (D24): division checked explicitly (sdiv/udiv do not trap),
popcnt through NEON, trapping float truncations checked before `fcvtz*`.

Far mode (S0004): a conditional branch reaches ±1 MiB. `func_end` reports a function in which one
does not; compiled again with `far`, every conditional branch to a label becomes `b.!cond` over
an unconditional `b` (±128 MiB). Loops inside one method branch locally and always reach."""

import struct
from collections.abc import Sequence
from dataclasses import dataclass, field

from waboros.targets.base import TrapCode, Unsupported
from waboros.targets.jit.aarch64 import abi
from waboros.targets.jit.aarch64 import simd as v128
from waboros.targets.jit.aarch64.abi import CTX, FRAME, MBASE, MEM0, MSIZE
from waboros.targets.jit.aarch64.encoder import (
    FP,
    LR,
    SP,
    ZR,
    Asm,
    Label,
    OutOfRange,
    bitmask,
    invert,
)
from waboros.targets.jit.common.backend import SWAP, AddK, Dst, Imm, Operand, Reg, Slot
from waboros.targets.jit.common.compiler import SUPPORTED, WIDEOPS
from waboros.targets.jit.common.context import (
    HOST_BRIDGE,
    INTERRUPT,
    NSTACK_LIMIT,
    STATUS_INTERRUPT,
    TAIL_SWITCH,
    VSTACK_LIMIT,
)
from waboros.targets.jit.common.guard import GUARD

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
    supported = SUPPORTED | v128.SUPPORTED | frozenset(WIDEOPS)

    def __init__(self, *, guard: bool = False) -> None:
        self.a = Asm()
        self.far = False
        self.tables: list[tuple[Label, list[Label], int]] = []  # (table, pads, code offset)
        self.guard = guard  # S0006: accesses the reservation's guard catches go unchecked
        self.faults: list[tuple[int, int]] = []  # (fault offset, resume offset), by `finish`
        self._faults: list[tuple[int, Label]] = []
        self._oob: Label | None = None  # the stub of the unchecked access `_bounds` just formed

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
        """Drop the code from `pos` on, with its jump tables and fault points (S0006)."""
        self.a.rewind(pos)
        self.tables = [t for t in self.tables if t[2] < pos]
        self._faults = [x for x in self._faults if x[0] < pos]

    def finish(self) -> tuple[bytes, int]:
        """The code, then the data area: jump tables of 32-bit offsets of their pads from the
        table (never executed)."""
        a = self.a
        data = len(a.buf)
        for table, pads, _ in self.tables:
            a.bind(table)
            a.buf += b"".join(struct.pack("<i", a.pos(p) - a.pos(table)) for p in pads)
        self.faults = [(pos, a.pos(stub)) for pos, stub in self._faults]
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
        if shift_slots:
            self._add_k(FRAME, FRAME, 8 * shift_slots)
        self._add_k(16, CTX, 8 * word)
        self._enter_record(16)
        if shift_slots:
            self._add_k(FRAME, FRAME, -8 * shift_slots)
        self._cbz(0, f.unwind, wide=False, nz=True)

    def _enter_record(self, r: int) -> None:
        """Call the function record at x<r> on its context (x28 saved in a 16-byte frame)."""
        a = self.a
        a.pair(False, CTX, ZR, SP, -16, "pre")
        a.ld(CTX, r, 16)
        abi.reload_memory(a)
        a.ld(17, r)
        a.blr(17)
        a.pair(True, CTX, ZR, SP, 16, "post")
        abi.reload_memory(a)

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

    def _addk(self, d: int, o: AddK) -> None:
        """d = (o.id + o.k) mod 2**32, zero-extended (S0007 K): one 32-bit add/sub of an imm12
        (optionally lsl #12), else the constant through x16 and a 32-bit add. No flags."""
        a, neg = self.a, -o.k & M32
        if _imm12(o.k):
            a.addsub_imm("add", d, o.id, o.k, False)
        elif _imm12(neg):
            a.addsub_imm("sub", d, o.id, neg, False)
        else:
            a.mov_imm(16, o.k, False)
            a.addsub("add", d, o.id, 16, False)

    def move(self, dst: Dst, src: Operand | AddK, wide: bool) -> None:
        """A register gets the operand's width (32-bit zero-extends); a slot always gets 64 bits."""
        if isinstance(src, AddK):
            self._addk(dst.id if isinstance(dst, Reg) else 16, src)
            if isinstance(dst, Slot):
                self._word(False, 16, FRAME, 8 * dst.k)
            return
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

    def wide(self, op: str, dst: Slot, args: tuple[Operand, ...]) -> None:
        """S0008: the operands into registers first (x9-x12; loads and moves leave the flags),
        then adds/adc or subs/sbc back to back (after subs C = NOT borrow, as sbc takes it), or
        mul plus umulh/smulh; both results through x13/x14 into their slots."""
        a = self.a
        rs = [self._r(x, 9 + i, True) for i, x in enumerate(args)]
        if op in ("add128", "sub128"):
            sub = op == "sub128"
            a.addsub("subs" if sub else "adds", 13, rs[0], rs[2])
            a.adc(14, rs[1], rs[3], sub=sub)
        else:
            a.madd(13, rs[0], rs[1], ZR)
            a.mulh(14, rs[0], rs[1], op == "mul_wide_s")
        self._word(False, 13, FRAME, 8 * dst.k)
        self._word(False, 14, FRAME, 8 * dst.k + 8)

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
        self, f: Frame, addr: Operand | AddK, offset: int, size: int, mem: int, a64: bool, lo: int
    ) -> tuple[int, int | None, int]:
        """The bounds check (R3: the one place memory safety rests on). addr + offset (< 2**33)
        and its end are computed in 64 bits, so nothing wraps; the end must be <= the memory's
        size or the access traps. A register address is copied with a 32-bit move first (an
        `AddK` formed by a 32-bit add, the wasm wrap before `offset`), so its upper half cannot
        matter. Returns the access as (base, index register or None, offset). In guard mode
        (S0006) an access the guard catches is formed the same way without the compare and
        branch, and the caller marks its one memory instruction with `_fault_point`."""
        assert self._oob is None, "an unchecked access without its fault point"
        if a64 and not isinstance(addr, Imm):
            return self._bounds64(f, addr, offset, size, mem, lo)
        a = self.a
        base, limit = self._mem(mem)
        oob = self._stub(f, TrapCode.MEMORY_OUT_OF_BOUNDS)
        unchecked = (self.guard and not a64) and (  # i64 (S0009): explicit in every mode
            (addr.value & M32) + offset + size <= (1 << 32) + GUARD
            if isinstance(addr, Imm)
            else offset + size <= GUARD
        )
        if unchecked:  # inside the reservation of 4 GiB + GUARD: the handler resumes at `oob`
            self._oob = oob
        if isinstance(addr, Imm):  # i64 (S0009): without the mask; ending past 2**64: oob
            ea = (addr.value & (M64 if a64 else M32)) + offset
            if ea + size > M64:
                a.b(oob)
                ea = 0
            if unchecked:
                pass
            elif _imm12(ea + size):
                a.addsub_imm("subs", ZR, limit, ea + size)
                self._bcc("lo", oob)
            else:
                a.mov_imm(10, ea + size)
                a.cmp(limit, 10)
                self._bcc("lo", oob)
            if not ea % size and ea // size < 1 << 12:
                return base, None, ea
            a.mov_imm(9, ea)
            return base, 9, 0
        self.move(Reg(9), addr, False)
        if offset:
            self._add_k(9, 9, offset)
        if not unchecked:
            a.addsub_imm("add", 10, 9, size)
            a.cmp(10, limit)
            self._bcc("hi", oob)
        return base, 9, 0

    def _bounds64(
        self, f: Frame, addr: Operand | AddK, offset: int, size: int, mem: int, lo: int
    ) -> tuple[int, int | None, int]:  # fmt: skip
        """The check of an i64 memory (S0009), explicit in every mode. The index r is the 64-bit
        register operand, or x9 loaded from its slot. With k = offset + size <= lo (the declared
        minimum: L - k cannot wrap): `sub x10, L, #k; cmp r, x10; b.hi`. Otherwise `adds x10, r,
        #k; b.hs` (the carry: idx + k >= 2**64) `; cmp x10, L; b.hi`. The access is [base, r]
        or [base, x9] with the offset added; x10, x13 free afterwards (a constant: `_bounds`)."""
        assert not isinstance(addr, (AddK, Imm)), "an AddK is an i32: never an i64 address"
        a = self.a
        base, limit = self._mem(mem)
        oob = self._stub(f, TrapCode.MEMORY_OUT_OF_BOUNDS)
        r = self._r(addr, 9, True)
        if offset + size > M64:  # no index is in bounds (mov_imm would wrap k: never reached)
            a.b(oob)
            offset = 0
        k = offset + size
        if k <= lo:
            self._add_k(10, limit, -k)
            a.cmp(r, 10)
            self._bcc("hi", oob)
            if offset:
                self._add_k(9, r, offset)
                r = 9
        else:
            if _imm12(k):
                a.addsub_imm("adds", 10, r, k)
            else:
                a.mov_imm(16, k)
                a.addsub("adds", 10, r, 16)
            self._bcc("hs", oob)
            a.cmp(10, limit)
            self._bcc("hi", oob)
            if offset:
                a.addsub_imm("sub", 9, 10, size)
                r = 9
        return base, r, 0

    def _fault_point(self) -> None:
        """The next instruction is the access `_bounds` formed: in guard mode, a fault point
        (S0006 fault map); in explicit mode nothing."""
        if self._oob is not None:
            self._faults.append((len(self.a.buf), self._oob))
            self._oob = None

    def load(
        self, f: Frame, size: int, signed: bool, wide: bool, dst: Reg, addr: Operand | AddK,
        offset: int, mem: int, *, a64: bool, lo: int,
    ) -> None:  # fmt: skip
        base, index, off = self._bounds(f, addr, offset, size, mem, a64, lo)
        self._fault_point()
        if index is None:
            self.a.ld(dst.id, base, off, size, signed, wide)
        else:
            self.a.ld_idx(dst.id, base, index, size, signed, wide)

    def store(
        self, f: Frame, size: int, addr: Operand | AddK, offset: int, src: Operand, mem: int, *,
        a64: bool, lo: int,
    ) -> None:  # fmt: skip
        base, index, off = self._bounds(f, addr, offset, size, mem, a64, lo)
        r = self._r(src, 13, size == 8)
        self._fault_point()  # one instruction per store (S0006)
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

    def simd(
        self, f: Frame, op: int, dst: Dst | None, args: tuple[Operand, ...], imm: tuple[int, ...],
        *, a64: bool, lo: int,
    ) -> None:  # fmt: skip
        v128.emit(self, f, op, dst, args, imm, a64, lo)

    def move_v128(self, dst: Slot, src: Slot | Imm) -> None:
        v128.move(self.a, dst, src)

    def ctx_addr(self, dst: Reg, word: int) -> None:
        """dst = the address of context word `word` (a function record: `ref.func`)."""
        self._add_k(dst.id, CTX, 8 * word)

    # ---------------------------------------------------------------- floats
    # Values stay bit patterns in x registers and slots; v0/v1 are scratch inside one method, so
    # no v register is live across a call (v8-v15 never touched: AAPCS64 callee-saved). FP and
    # Advanced SIMD are ARMv8.0 (D24); FPCR is the process's default (round to nearest, no
    # flush-to-zero, NaNs propagated), which is what wasm needs.
    def _v(self, v: int, o: Operand, wide: bool) -> None:
        """v<v> = the bits of `o` (through x9)."""
        self.a.fmov_to(v, self._r(o, 9, wide), wide)

    def fbinop(self, op: str, dst: Reg, x: Operand, y: Operand, wide: bool) -> None:
        a, d, w = self.a, dst.id, wide
        sign = 1 << (63 if w else 31)
        if op == "copysign":  # bit operations: NaN payloads stay exact; y is read first
            a.logic_imm("and", 11, self._r(y, 10, w), sign, w)
            a.logic_imm("and", d, self._r(x, 9, w), sign - 1, w)
            a.logic("orr", d, d, 11, w)
            return
        # fmin/fmax are wasm's min/max: NaN if either is NaN, -0 < +0 (Arm ARM FPMin/FPMax;
        # Cranelift's and Liftoff's arm64 lowering use them directly; fminnm/fmaxnm are not)
        self._v(0, x, w)
        self._v(1, y, w)
        a.fp2(op, 0, 0, 1, w)
        a.fmov_from(d, 0, w)

    def funop(self, op: str, dst: Reg, x: Operand, wide: bool) -> None:
        a, d, w = self.a, dst.id, wide
        sign = 1 << (63 if w else 31)
        if op in ("abs", "neg"):  # bit operations: NaN payloads stay exact
            xr = self._r(x, 9, w)
            if op == "abs":
                a.logic_imm("and", d, xr, sign - 1, w)
            else:
                a.logic_imm("eor", d, xr, sign, w)
            return
        self._v(0, x, w)
        rnd = {"ceil": "frintp", "floor": "frintm", "trunc": "frintz", "nearest": "frintn"}
        a.fp1(rnd.get(op, op), 0, 0, w)
        a.fmov_from(d, 0, w)

    def fcmp_set(self, cond: str, dst: Reg, x: Operand, y: Operand, wide: bool) -> None:
        """fcmp: unordered sets C and V, so mi/ls/gt/ge/eq are false and ne true on NaN."""
        self._v(0, x, wide)
        self._v(1, y, wide)
        self.a.fcmp(0, 1, wide)
        self.a.cset(dst.id, {"eq": "eq", "ne": "ne", "lt": "mi", "le": "ls", "gt": "gt",
                             "ge": "ge"}[cond])  # fmt: skip

    def convert(self, f: Frame, op: str, dst: Reg, x: Operand) -> None:
        a, d = self.a, dst.id
        rt, name = op.split(".")
        parts = name.split("_")
        if parts[0] == "trunc":
            sat, signed = parts[1] == "sat", parts[-1] == "s"
            self._trunc(f, d, x, parts[-2] == "f64", rt == "i64", signed, sat)
            return
        double = rt == "f64"
        if parts[0] in ("demote", "promote"):
            self._v(0, x, not double)
            a.fp1("fcvt", 0, 0, not double)
        else:  # scvtf/ucvtf round once, from the 32- or 64-bit register
            src64, cvt = parts[1] == "i64", "scvtf" if parts[2] == "s" else "ucvtf"
            a.fcvt_int(cvt, self._r(x, 9, src64), 0, src64, double)
        a.fmov_from(d, 0, double)

    def _trunc(
        self, f: Frame, d: int, x: Operand, double: bool, w: bool, signed: bool, sat: bool
    ) -> None:
        """Float -> int. fcvtzs/fcvtzu saturate and give 0 for NaN, which is trunc_sat; the
        trapping forms check first: NaN (BAD_CONVERSION_TO_INTEGER), then the two bounds, both
        exact in either format (INTEGER_OVERFLOW). The shape of Cranelift's aarch64
        `fcvt_to_sint`/`fcvt_to_uint` lowering, written anew."""
        a = self.a
        self._v(0, x, double)
        if not sat:
            a.fcmp(0, 0, double)
            self._bcc("vs", self._stub(f, TrapCode.BAD_CONVERSION_TO_INTEGER))
            n = 64 if w else 32
            # valid: lo < x (exclusive) or lo <= x (inclusive), and x < hi
            if not signed:
                lo, strict = -1.0, True
            elif double and not w:
                lo, strict = -(2.0**31) - 1, True
            else:
                lo, strict = -(2.0 ** (n - 1)), False
            hi = 2.0 ** (n - 1 if signed else n)
            ovf = self._stub(f, TrapCode.INTEGER_OVERFLOW)
            for bound, cond in ((lo, "le" if strict else "lt"), (hi, "ge")):
                fmt = ("<d", "<Q") if double else ("<f", "<I")
                bits = struct.unpack(fmt[1], struct.pack(fmt[0], bound))[0]
                a.mov_imm(9, bits, double)
                a.fmov_to(1, 9, double)
                a.fcmp(0, 1, double)
                self._bcc(cond, ovf)
        a.fcvt_int("fcvtzs" if signed else "fcvtzu", d, 0, w, double)

    # ---------------------------------------------------------------- tables and bulk memory
    # A table is `base` (element words) and `size` in context words `word`, `word + 1`, read at
    # every use: table.grow may move the elements, and runs only through the host bridge.
    # Operands are read into x9 / x13-x15 before anything is written (LESSONS 32).
    def _u32(self, r: int, o: Operand) -> None:
        """x<r> = the i32 operand, zero-extended (a 32-bit move even from a register)."""
        if isinstance(o, Reg):
            self.a.mov(r, o.id, False)
        else:
            self.move(Reg(r), o, False)

    def _element(self, f: Frame, word: int, index: Operand, a64: bool) -> None:
        """x9 = index, bounds-checked against the table's size; x10 = the elements' base. An
        i64 index (S0009) is read by a 64-bit move: `cmp` with the 64-bit size word, no carry."""
        if a64:
            self.move(Reg(9), index, True)
        else:
            self._u32(9, index)
        self._word(True, 10, CTX, 8 * word + 8)
        self.a.cmp(9, 10)
        self._bcc("hs", self._stub(f, TrapCode.TABLE_OUT_OF_BOUNDS))
        self._word(True, 10, CTX, 8 * word)

    def table_get(self, f: Frame, dst: Reg, word: int, index: Operand, *, a64: bool) -> None:
        self._element(f, word, index, a64)
        self.a.ld_idx(dst.id, 10, 9, 8, scaled=True)

    def table_set(
        self, f: Frame, word: int, index: Operand, value: Operand, *, a64: bool
    ) -> None:  # fmt: skip
        self._element(f, word, index, a64)
        self.a.st_idx(self._r(value, 11, True), 10, 9, 8, scaled=True)

    def _three(
        self, x: Operand, y: Operand, z: Operand, wy: bool, wx: bool = False, wz: bool = False
    ) -> None:  # fmt: skip
        """x13 = x, x14 = y, x15 = z (i32 zero-extended; 64-bit if `wx`/`wy`/`wz`)."""
        for r, o, w in ((13, x, wx), (14, y, wy), (15, z, wz)):
            if w:
                self.move(Reg(r), o, True)
            else:
                self._u32(r, o)

    def _end_check(self, f: Frame, r: int, limit: int, code: int, carry: bool = False) -> None:
        """x<r> + x15 must not exceed x<limit>: both < 2**35 (no wrap in 64 bits), or with
        `carry` 64-bit values whose sum may wrap (`adds; b.hs`: out of bounds, S0009)."""
        self.a.addsub("adds" if carry else "add", 9, r, 15)
        if carry:
            self._bcc("hs", self._stub(f, code))
        self.a.cmp(9, limit)
        self._bcc("hi", self._stub(f, code))

    def _copy(self, unit: int) -> None:
        """Copy x15 bytes from [x14] to [x13] as memmove does: backwards when the destination is
        above the source; 8 bytes at a time, then single bytes (`unit` 1); x9 the temporary."""
        a = self.a
        fwd, f1, b8, b1, done = (a.label() for _ in range(5))
        a.cmp(13, 14)
        a.bcc("ls", fwd)
        a.bind(b8)
        a.addsub_imm("subs", ZR, 15, 8)
        a.bcc("lo", b1 if unit == 1 else done)
        a.addsub_imm("sub", 15, 15, 8)
        a.ld_idx(9, 14, 15)
        a.st_idx(9, 13, 15)
        a.b(b8)
        if unit == 1:
            a.bind(b1)
            a.cbz(15, done)
            a.addsub_imm("sub", 15, 15, 1)
            a.ld_idx(9, 14, 15, 1)
            a.st_idx(9, 13, 15, 1)
            a.b(b1)
        a.bind(fwd)
        a.addsub_imm("subs", ZR, 15, 8)
        a.bcc("lo", f1 if unit == 1 else done)
        a.ld(9, 14)
        a.st(9, 13)
        for r in (13, 14, 15):
            a.addsub_imm("sub" if r == 15 else "add", r, r, 8)
        a.b(fwd)
        if unit == 1:
            a.bind(f1)
            a.cbz(15, done)
            a.ld(9, 14, 0, 1)
            a.st(9, 13, 0, 1)
            for r in (13, 14, 15):
                a.addsub_imm("sub" if r == 15 else "add", r, r, 1)
            a.b(f1)
        a.bind(done)

    def memory_copy(
        self, f: Frame, d: Operand, s: Operand, n: Operand, dmem: int, smem: int, *,
        a64: tuple[bool, bool],
    ) -> None:  # fmt: skip
        """Both ranges are checked before any byte moves (no partial copy); the direction is
        chosen by the real addresses (the two indices may name one memory). Each range at its
        own width; `n` is 64-bit only between two i64 memories (S0009)."""
        a, oob, (ad, as_) = self.a, TrapCode.MEMORY_OUT_OF_BOUNDS, a64
        self._three(d, s, n, as_, ad, ad and as_)
        base, limit = self._mem(dmem)  # another memory than 0: x11/x12, read again below
        self._end_check(f, 13, limit, oob, ad)
        a.addsub("add", 13, 13, base)
        base, limit = self._mem(smem)
        self._end_check(f, 14, limit, oob, as_)
        a.addsub("add", 14, 14, base)
        self._copy(1)

    def memory_fill(
        self, f: Frame, d: Operand, v: Operand, n: Operand, mem: int, *, a64: bool
    ) -> None:  # fmt: skip
        a = self.a
        self._three(d, v, n, False, a64, a64)
        base, limit = self._mem(mem)
        self._end_check(f, 13, limit, TrapCode.MEMORY_OUT_OF_BOUNDS, a64)
        a.addsub("add", 13, 13, base)
        a.logic_imm("and", 14, 14, 0xFF, False)
        a.mov_imm(9, 0x0101010101010101)
        a.madd(14, 14, 9, ZR)  # the byte in all eight
        f8, f1, done = a.label(), a.label(), a.label()
        a.bind(f8)
        a.addsub_imm("subs", ZR, 15, 8)
        a.bcc("lo", f1)
        a.st(14, 13)
        a.addsub_imm("add", 13, 13, 8)
        a.addsub_imm("sub", 15, 15, 8)
        a.b(f8)
        a.bind(f1)
        a.cbz(15, done)
        a.st(14, 13, 0, 1)
        a.addsub_imm("add", 13, 13, 1)
        a.addsub_imm("sub", 15, 15, 1)
        a.b(f1)
        a.bind(done)

    def table_fill(
        self, f: Frame, word: int, i: Operand, v: Operand, n: Operand, *, a64: bool
    ) -> None:  # fmt: skip
        a = self.a
        self._three(i, v, n, True, a64, a64)
        self._word(True, 10, CTX, 8 * word + 8)
        self._end_check(f, 13, 10, TrapCode.TABLE_OUT_OF_BOUNDS, a64)
        self._word(True, 10, CTX, 8 * word)
        a.addsub("add", 13, 10, 13, sh="lsl", amount=3)
        loop, done = a.label(), a.label()
        a.bind(loop)
        a.cbz(15, done)
        a.st(14, 13)
        a.addsub_imm("add", 13, 13, 8)
        a.addsub_imm("sub", 15, 15, 1)
        a.b(loop)
        a.bind(done)

    def table_copy(
        self, f: Frame, dst: int, src: int, d: Operand, s: Operand, n: Operand, *,
        a64: tuple[bool, bool],
    ) -> None:  # fmt: skip
        a, oob, (ad, as_) = self.a, TrapCode.TABLE_OUT_OF_BOUNDS, a64
        self._three(d, s, n, as_, ad, ad and as_)  # n: the narrower type, as memory_copy
        for r, word, w in ((13, dst, ad), (14, src, as_)):
            self._word(True, 10, CTX, 8 * word + 8)
            self._end_check(f, r, 10, oob, w)
        for r, word in ((13, dst), (14, src)):
            self._word(True, 10, CTX, 8 * word)
            a.addsub("add", r, 10, r, sh="lsl", amount=3)
        a.bfm("ubfm", 15, 15, 61, 60)  # lsl #3
        self._copy(8)

    # ---------------------------------------------------------------- indirect calls
    def _table_record(self, f: Frame, table: int, sig: int, index: Operand, a64: bool) -> None:
        """x15 = the function record of element `index`: bounds (TABLE_OUT_OF_BOUNDS), null
        (INDIRECT_CALL_TO_NULL), its canonical type id against the signature word (BAD_SIGNATURE),
        in the spec's order (Wasmtime's codes)."""
        a = self.a
        self._element(f, table, index, a64)
        a.ld_idx(15, 10, 9, 8, scaled=True)
        self._cbz(15, self._stub(f, TrapCode.INDIRECT_CALL_TO_NULL))
        self._word(True, 9, CTX, 8 * sig)
        a.ld(10, 15, 8)
        a.cmp(9, 10)
        self._bcc("ne", self._stub(f, TrapCode.BAD_SIGNATURE))

    def _ref_record(self, f: Frame, ref: Operand) -> None:
        """x15 = a function reference (call_ref); null traps with NULL_REFERENCE."""
        self.move(Reg(15), ref, True)
        self._cbz(15, self._stub(f, TrapCode.NULL_REFERENCE))

    def _call_x15(self, f: Frame, shift_slots: int) -> None:
        if shift_slots:
            self._add_k(FRAME, FRAME, 8 * shift_slots)  # x16 at most: the record is in x15
        self._enter_record(15)
        if shift_slots:
            self._add_k(FRAME, FRAME, -8 * shift_slots)
        self._cbz(0, f.unwind, wide=False, nz=True)

    def call_indirect(
        self, f: Frame, table: int, sig: int, index: Operand, shift_slots: int, *, a64: bool
    ) -> None:  # fmt: skip
        self._table_record(f, table, sig, index, a64)  # the index is read before x27 moves
        self._call_x15(f, shift_slots)

    def call_ref(self, f: Frame, ref: Operand, shift_slots: int) -> None:
        self._ref_record(f, ref)
        self._call_x15(f, shift_slots)

    def null_check(self, f: Frame, x: Operand, code: int) -> None:
        """Trap with `code` if the reference x is null; nothing but x9 is written."""
        self._cbz(self._r(x, 9, True), self._stub(f, code))

    # ---------------------------------------------------------------- tail calls
    # The arguments move down to the frame base, the frame is torn down as by the epilogue (pins
    # and {x29, x30} restored: x30 is our caller's return address again, sp as on entry), then a
    # branch: both stacks stay constant and the callee returns to our caller. Every trap check
    # comes before the teardown; the callee's prologue checks the stack limits and polls.
    def _tail(self, f: Frame, src: int, n: int) -> None:
        """Slots src.. -> 0..n (ascending is safe under overlap; x16/x17 only), teardown."""
        for j in range(n):
            if src:
                self.move(Slot(j), Slot(src + j), True)
        self._pins(f, True)
        self.a.pair(True, FP, LR, SP, f.size, "post")

    def tail_direct(self, f: Frame, label: Label, src: int, n: int) -> None:
        self._tail(f, src, n)
        self.a.b(label)

    def _tail_record(self) -> None:
        """Branch to the record in x16: directly on our context, else through tail_switch
        (abi.py, D30), entered by `br x17` with x16 = the record and x30 = the return address."""
        a, other = self.a, self.a.label()
        a.ld(17, 16, 16)
        a.cmp(CTX, 17)
        a.bcc("ne", other)
        a.ld(17, 16)
        a.br(17)
        a.bind(other)
        self._word(True, 17, CTX, 8 * TAIL_SWITCH)
        a.br(17)

    def tail_indirect(
        self, f: Frame, table: int, sig: int, index: Operand, src: int, n: int, *, a64: bool
    ) -> None:  # fmt: skip
        self._table_record(f, table, sig, index, a64)
        self._tail(f, src, n)
        self.a.mov(16, 15)
        self._tail_record()

    def tail_ref(self, f: Frame, ref: Operand, src: int, n: int) -> None:
        self._ref_record(f, ref)
        self._tail(f, src, n)
        self.a.mov(16, 15)
        self._tail_record()

    def tail_record(self, f: Frame, word: int, src: int, n: int) -> None:
        self._tail(f, src, n)
        self._add_k(16, CTX, 8 * word)
        self._tail_record()
