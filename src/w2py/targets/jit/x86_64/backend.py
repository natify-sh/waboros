"""The x86-64 backend (docs/specs/S0002 D26, S0003): the `common.backend.Backend` interface.

A function: `endbr64`; push its pins (callee-saved for wasm) + pad to 16; check rsp against
NSTACK_LIMIT and r12 + 8 * frame_slots against VSTACK_LIMIT; poll INTERRUPT. A trap jumps to a
cold stub `mov eax, code; jmp unwind`; `unwind` is the epilogue, which keeps eax (R2: no rsp reset).

Roles: rax = tos (and the status), rcx/rdx scratch inside one method, r12 frame, r13/r14 memory 0
base/size, r15 context, POOL pinnable. i32 values stay zero-extended (32-bit forms, LESSONS 5).
Every method that compares emits only `mov` between the `cmp` and its consumer (LESSONS 1).
Everything here is x86-64-v1 (D24): bsr/bsf with an explicit zero case, popcnt by SWAR.
"""

import struct
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import cast

from w2py.targets.base import TrapCode
from w2py.targets.jit.common.backend import NEGATE, SWAP, Dst, Imm, Operand, Reg, Slot
from w2py.targets.jit.common.context import (
    HOST_BRIDGE,
    INTERRUPT,
    NSTACK_LIMIT,
    STATUS_INTERRUPT,
    TAIL_SWITCH,
    VSTACK_LIMIT,
)
from w2py.targets.jit.x86_64.abi import MEM0
from w2py.targets.jit.x86_64.encoder import (
    R8,
    R9,
    R10,
    R11,
    R12,
    R13,
    R14,
    R15,
    RAX,
    RBP,
    RBX,
    RCX,
    RDI,
    RDX,
    RSI,
    RSP,
    Asm,
    Label,
    Mem,
)

POOL = (RBX, RBP, RSI, RDI, R8, R9, R10, R11)  # pinnable; never used by the trampolines' roles
STATUS_MISALIGNED = 255  # test builds only (`check_align`): a call site found rsp % 16 != 0
CC = {"eq": "e", "ne": "ne", "lt_s": "l", "lt_u": "b", "gt_s": "g", "gt_u": "a", "le_s": "le",
      "le_u": "be", "ge_s": "ge", "ge_u": "ae"}  # fmt: skip
SHIFTS = {"shl": "shl", "shr_s": "sar", "shr_u": "shr", "rotl": "rol", "rotr": "ror"}
M32, M64 = 0xFFFFFFFF, (1 << 64) - 1


@dataclass
class Frame:
    pins: tuple[int, ...]
    pad: bool
    unwind: Label
    vcheck: int  # position of the disp32 of `lea rax, [r12 + 8 * frame_slots]`
    traps: dict[int, Label] = field(default_factory=lambda: {})


def _slot(k: int) -> Mem:
    return Mem(R12, 8 * k)


def _imm32(o: Operand, wide: bool) -> int | None:
    """The immediate if it fits the instruction's (sign-extended) imm32 field, else None."""
    if not isinstance(o, Imm):
        return None
    v = o.value & (M64 if wide else M32)
    return v if not wide or v < 1 << 31 or v >= (1 << 64) - (1 << 31) else None


class Backend:
    pool = POOL
    tos = RAX

    def __init__(self, check_align: bool = False) -> None:
        self.a = Asm()
        self.check_align = check_align
        self.tables: list[tuple[Label, list[Label]]] = []

    # ---------------------------------------------------------------- labels, frames, traps
    def new_label(self) -> Label:
        return self.a.label()

    def bind(self, label: Label) -> None:
        self.a.bind(label)

    def jump(self, label: Label) -> None:
        self.a.jmp(label)

    def here(self) -> int:
        return len(self.a.buf)

    def func_begin(self, label: Label, pins: tuple[int, ...] = ()) -> Frame:
        a = self.a
        assert all(r in POOL for r in pins)
        a.bind(label)
        a.endbr64()  # reached through a register: from the entry and from call_indirect
        for r in pins:
            a.push(r)
        pad = len(pins) % 2 == 0  # rsp = 8 mod 16 on entry
        if pad:
            a.alu_imm("sub", RSP, 8, w=True)
        f = Frame(pins, pad, a.label(), 0)
        a.alu("cmp", RSP, Mem(R15, 8 * NSTACK_LIMIT), w=True)
        a.jcc("b", self._stub(f, TrapCode.STACK_OVERFLOW))
        a.lea(RAX, Mem(R12, 1 << 20))  # disp32, patched by func_end
        f.vcheck = len(a.buf) - 4
        a.alu("cmp", RAX, Mem(R15, 8 * VSTACK_LIMIT), w=True)
        a.jcc("a", self._stub(f, TrapCode.STACK_OVERFLOW))
        self.poll(f)
        return f

    def func_end(self, f: Frame, frame_slots: int) -> None:
        """Patch the value-stack check, emit the cold trap stubs and the epilogue."""
        a = self.a
        struct.pack_into("<i", a.buf, f.vcheck, 8 * frame_slots)
        for code, label in f.traps.items():
            a.bind(label)
            a.mov_imm(RAX, code)
            a.jmp(f.unwind)
        a.bind(f.unwind)
        if f.pad:
            a.alu_imm("add", RSP, 8, w=True)
        for r in reversed(f.pins):
            a.pop(r)
        a.ret()

    def _stub(self, f: Frame, code: int) -> Label:
        if code not in f.traps:
            f.traps[code] = self.a.label()
        return f.traps[code]

    def poll(self, f: Frame) -> None:
        """At function entry and loop headers (D14, LESSONS 8)."""
        self.a.alu_imm("cmp", Mem(R15, 8 * INTERRUPT), 0, w=True)
        self.a.jcc("ne", self._stub(f, STATUS_INTERRUPT))

    def trap(self, f: Frame, code: int) -> None:
        self.a.jmp(self._stub(f, code))

    def ret_ok(self, f: Frame) -> None:
        self.a.alu("xor", RAX, RAX)
        self.a.jmp(f.unwind)

    def finish(self) -> tuple[bytes, int]:
        """The code, then the data area (jump tables: 32-bit offsets, never executed)."""
        a = self.a
        data = len(a.buf)
        for table, entries in self.tables:
            a.bind(table)
            for label in entries:
                a.data_rel32(label)
        return a.finish(), data

    # ---------------------------------------------------------------- data
    def _rm(self, o: Operand, scratch: int, wide: bool) -> int | Mem:
        """A register or memory form of `o`; an immediate is loaded into `scratch`."""
        if isinstance(o, Reg):
            return o.id
        if isinstance(o, Slot):
            return _slot(o.k)
        self.a.mov_imm(scratch, o.value & (M64 if wide else M32), w=wide)
        return scratch

    def move(self, dst: Dst, src: Operand, wide: bool) -> None:
        """A register gets the operand's width (32-bit zero-extends); a slot always gets 64 bits."""
        a = self.a
        if isinstance(dst, Reg):
            if isinstance(src, Imm):
                a.mov_imm(dst.id, src.value & (M64 if wide else M32), w=wide)
            elif isinstance(src, Reg):
                if src.id != dst.id:
                    a.mov(dst.id, src.id, w=wide)
            else:
                a.mov(dst.id, _slot(src.k), w=wide)
            return
        m = _slot(dst.k)
        if isinstance(src, Imm):
            v = _imm32(src, True)
            if v is not None:
                a.mov_imm(m, v, w=True)
            else:
                a.mov_imm(RCX, src.value, w=True)
                a.mov(m, RCX, w=True)
        elif isinstance(src, Reg):
            a.mov(m, src.id, w=True)
        elif src.k != dst.k:
            a.mov(RCX, _slot(src.k), w=True)
            a.mov(m, RCX, w=True)

    def binop(self, op: str, dst: Reg, x: Operand, y: Operand, wide: bool) -> None:
        a, d, w = self.a, dst.id, wide
        if op in SHIFTS:  # x86 masks the count by 31 / 63, as wasm does
            if isinstance(y, Imm):
                self.move(dst, x, w)
                n = y.value & (63 if w else 31)
                if n:
                    a.shift(SHIFTS[op], d, n, w=w)
            else:
                self.move(Reg(RCX), y, False)
                self.move(dst, x, w)
                a.shift(SHIFTS[op], d, None, w=w)
            return
        if isinstance(y, Reg) and y.id == d and not (isinstance(x, Reg) and x.id == d):
            if op == "sub":
                self.move(Reg(RCX), y, w)
                y = Reg(RCX)
            else:  # commutative
                x, y = y, x
        self.move(dst, x, w)
        imm = _imm32(y, w)
        if op == "mul":
            if imm is not None:
                a.imul_imm(d, d, imm, w=w)
            else:
                a.imul(d, self._rm(y, RCX, w), w=w)
        elif imm is not None:
            a.alu_imm(op, d, imm, w=w)
        else:
            a.alu(op, d, self._rm(y, RCX, w), w=w)

    def divrem(self, f: Frame, op: str, dst: Reg, x: Operand, y: Operand, wide: bool) -> None:
        """Explicit checks (D10): the CPU faults on a zero divisor and on INT_MIN / -1."""
        a, w, mask = self.a, wide, M64 if wide else M32
        signed, rem = op.endswith("_s"), op.startswith("rem")
        known = y.value & mask if isinstance(y, Imm) else None
        self.move(Reg(RCX), y, w)
        if not known:
            a.test(RCX, RCX, w=w)
            a.jcc("e", self._stub(f, TrapCode.INTEGER_DIVISION_BY_ZERO))
        self.move(Reg(RAX), x, w)
        done = None
        if signed and (known is None or known == mask):
            go = a.label()
            a.alu_imm("cmp", RCX, -1, w=w)
            a.jcc("ne", go, short=True)
            if rem:  # x rem -1 = 0
                a.alu("xor", RDX, RDX)
                done = a.label()
                a.jmp(done, short=True)
            else:
                if w:
                    a.mov_imm(RDX, 1 << 63, w=True)
                    a.alu("cmp", RAX, RDX, w=True)
                else:
                    a.alu_imm("cmp", RAX, 1 << 31)
                a.jcc("e", self._stub(f, TrapCode.INTEGER_OVERFLOW))
            a.bind(go)
        if signed:
            a.cqo() if w else a.cdq()
            a.unary("idiv", RCX, w=w)
        else:
            a.alu("xor", RDX, RDX)
            a.unary("div", RCX, w=w)
        if done:
            a.bind(done)
        res = RDX if rem else RAX
        if dst.id != res:
            a.mov(dst.id, res, w=w)

    def unop(self, op: str, dst: Reg, x: Operand, wide: bool) -> None:
        a, d, w = self.a, dst.id, wide
        bits = 64 if w else 32
        if op in ("clz", "ctz"):  # bsr/bsf leave ZF set and the result undefined on zero
            src = self._rm(x, RCX, w)
            if op == "clz":
                a.bsr(d, src, w=w)
                a.mov_imm(RDX, 2 * bits - 1)  # mov: the flags survive
                a.cmov("e", d, RDX, w=w)
                a.alu_imm("xor", d, bits - 1, w=w)  # 31 - i == 31 ^ i; (63 ^ 31) = 32
            else:
                a.bsf(d, src, w=w)
                a.mov_imm(RDX, bits)
                a.cmov("e", d, RDX, w=w)
        elif op == "popcnt":  # SWAR (x86-64-v1 has no popcnt)
            self.move(dst, x, w)
            k = [0x5555555555555555, 0x3333333333333333, 0x0F0F0F0F0F0F0F0F, 0x0101010101010101]
            if not w:
                k = [v & M32 for v in k]
            a.mov(RCX, d, w=w)
            a.shift("shr", RCX, 1, w=w)
            self._and_k(RCX, k[0], w)
            a.alu("sub", d, RCX, w=w)
            a.mov(RCX, d, w=w)
            self._and_k(d, k[1], w)
            a.shift("shr", RCX, 2, w=w)
            self._and_k(RCX, k[1], w)
            a.alu("add", d, RCX, w=w)
            a.mov(RCX, d, w=w)
            a.shift("shr", RCX, 4, w=w)
            a.alu("add", d, RCX, w=w)
            self._and_k(d, k[2], w)
            if w:
                a.mov_imm(RDX, k[3], w=True)
                a.imul(d, RDX, w=True)
            else:
                a.imul_imm(d, d, k[3])
            a.shift("shr", d, bits - 8, w=w)
        elif op in ("extend8_s", "extend16_s"):
            src = self._rm(x, RCX, w)
            a.movsx(d, src, 8 if op == "extend8_s" else 16, w=w)
        elif op in ("extend32_s", "extend_i32_s"):
            a.movsx(d, self._rm(x, RCX, False), 32)
        else:  # extend_i32_u, wrap: the low 32 bits, zero-extended
            assert op in ("extend_i32_u", "wrap"), op
            if isinstance(x, Reg):  # even when x is d: `move` skips that, the upper half must go
                a.mov(d, x.id, w=False)
            else:
                self.move(dst, x, False)

    def _and_k(self, r: int, k: int, w: bool) -> None:
        if w:
            self.a.mov_imm(RDX, k, w=True)
            self.a.alu("and", r, RDX, w=True)
        else:
            self.a.alu_imm("and", r, k)

    def _cmp(self, cond: str, x: Operand, y: Operand, wide: bool) -> str:
        """Emit the compare (only `mov` may follow before the consumer); the condition to test,
        swapped if the operands were."""
        a, w = self.a, wide
        if isinstance(x, Imm):
            if isinstance(y, Imm):
                self.move(Reg(RCX), x, w)
                x = Reg(RCX)
            else:
                x, y, cond = y, x, SWAP[cond]
        if isinstance(x, Slot) and isinstance(y, Slot):
            self.move(Reg(RCX), x, w)
            x = Reg(RCX)
        lhs = x.id if isinstance(x, Reg) else _slot(x.k)
        imm = _imm32(y, w)
        if imm is not None:
            a.alu_imm("cmp", lhs, imm, w=w)
        else:
            a.alu("cmp", lhs, self._rm(y, RDX, w), w=w)
        return cond

    def cmp_set(self, cond: str, dst: Reg, x: Operand, y: Operand, wide: bool) -> None:
        cond = self._cmp(cond, x, y, wide)
        self.a.setcc(CC[cond], dst.id)
        self.a.movzx(dst.id, dst.id, 8)

    def cmp_branch(self, cond: str, x: Operand, y: Operand, label: Label, wide: bool) -> None:
        cond = self._cmp(cond, x, y, wide)
        self.a.jcc(CC[cond], label)

    def select_cmp(
        self, cond: str, x: Operand, y: Operand, wc: bool, dst: Reg, t: Operand, e: Operand,
        wv: bool,
    ) -> None:  # fmt: skip
        """dst = t if (x cond y) else e; both values are already computed (LESSONS 28)."""
        cond = self._cmp(cond, x, y, wc)
        self.move(Reg(RCX), t, wv)  # mov only from here to the cmov (LESSONS 1)
        src = self._rm(e, RDX, wv)
        self.a.cmov(CC[NEGATE[cond]], RCX, src, w=wv)
        self.move(dst, Reg(RCX), wv)

    def jump_table(self, index: Operand, labels: Sequence[Label], default: Label) -> None:
        """Jump to labels[index], or default when index >= len(labels). From 4 entries on, a
        table of 32-bit offsets in the data area (S0003: never disassembled); each entry lands on
        an `endbr64` pad (CET IBT, R2) that jumps on."""
        a = self.a
        self.move(Reg(RCX), index, False)
        if len(labels) < 4:
            for i, label in enumerate(labels):
                a.alu_imm("cmp", RCX, i)
                a.jcc("e", label)
            a.jmp(default)
            return
        a.alu_imm("cmp", RCX, len(labels))
        a.jcc("ae", default)
        table = a.label()
        a.lea_label(RDX, table)
        a.lea(RDX, Mem(RDX, 0, RCX, 4))
        a.movsx(RCX, Mem(RDX), 32)
        a.lea(RDX, Mem(RDX, 4, RCX))
        a.jmp(RDX)
        pads: dict[Label, Label] = {}
        for label in labels:
            if label not in pads:
                pads[label] = a.label()
                a.bind(pads[label])
                a.endbr64()
                a.jmp(label)
        self.tables.append((table, [pads[label] for label in labels]))

    # ---------------------------------------------------------------- floats
    # Values stay bit patterns in GPRs and slots; xmm0-xmm2 are scratch inside one method, so no
    # xmm register is live across a call (SysV: all caller-saved; Win64 xmm6-15 never touched).
    # Constants go through rcx/rdx (no data-area constants). SSE2 only (x86-64-v1, D24).
    def _xmm(self, x: int, o: Operand, wide: bool) -> None:
        """Load the bits of `o` into xmm `x` (an immediate through rcx)."""
        a = self.a
        if isinstance(o, Slot):
            a.movs(x, _slot(o.k), wide)
        elif isinstance(o, Reg):
            a.movq_to_xmm(x, o.id, wide)
        elif o.value == 0:
            a.sse("xor", x, x, wide)
        else:
            a.mov_imm(RCX, o.value & (M64 if wide else M32), w=wide)
            a.movq_to_xmm(x, RCX, wide)

    def _kxmm(self, x: int, value: float, wide: bool) -> None:
        """xmm `x` = a float constant (through rdx)."""
        bits = struct.unpack("<Q" if wide else "<I", struct.pack("<d" if wide else "<f", value))
        self.a.mov_imm(RDX, bits[0], w=wide)
        self.a.movq_to_xmm(x, RDX, wide)

    def _cvtsi(self, x: int, r: int, double: bool, w: bool) -> None:
        self.a.sse("xor", x, x, double)  # cvtsi2s* merges into x: break the dependency
        self.a.cvtsi2f(x, r, double, w)

    def _sign_only(self, r: int, w: bool) -> None:
        if w:
            self.a.shift("shr", r, 63, w=True)
            self.a.shift("shl", r, 63, w=True)
        else:
            self.a.alu_imm("and", r, 0x80000000)

    def fbinop(self, op: str, dst: Reg, x: Operand, y: Operand, wide: bool) -> None:
        a, d, w = self.a, dst.id, wide
        if op == "copysign":  # bit operations: NaN payloads stay exact
            self.move(Reg(RCX), y, w)
            self.move(dst, x, w)
            if w:
                a.bt_imm("btr", d, 63, w=True)
            else:
                a.alu_imm("and", d, 0x7FFFFFFF)
            self._sign_only(RCX, w)
            a.alu("or", d, RCX, w=w)
            return
        self._xmm(0, x, w)
        if op in ("min", "max"):
            # wasm: NaN if either is NaN; min(-0, +0) = -0, max = +0. minss/maxss return the
            # second operand in both cases, so equal and unordered take their own paths (the
            # same split as Winch's and Liftoff's x64 lowering).
            self._xmm(1, y, w)
            ordered, nan, done = a.label(), a.label(), a.label()
            a.ucomis(0, 1, w)
            a.jcc("ne", ordered, short=True)
            a.jcc("p", nan, short=True)
            a.sse("or" if op == "min" else "and", 0, 1, w)  # equal: only the sign bits differ
            a.jmp(done, short=True)
            a.bind(nan)
            a.sse("add", 0, 1, w)  # a quiet NaN from the NaN operand(s)
            a.jmp(done, short=True)
            a.bind(ordered)
            a.sse(op, 0, 1, w)
            a.bind(done)
        elif isinstance(y, Slot):
            a.sse(op, 0, _slot(y.k), w)
        else:
            self._xmm(1, y, w)
            a.sse(op, 0, 1, w)
        a.movq_from_xmm(d, 0, w)

    def funop(self, op: str, dst: Reg, x: Operand, wide: bool) -> None:
        a, d, w = self.a, dst.id, wide
        if op in ("abs", "neg"):  # bit operations: NaN payloads stay exact
            self.move(dst, x, w)
            if w:
                a.bt_imm("btr" if op == "abs" else "btc", d, 63, w=True)
            else:
                a.alu_imm(
                    "and" if op == "abs" else "xor", d, 0x7FFFFFFF if op == "abs" else 1 << 31
                )
            return
        if op == "sqrt":
            self._xmm(0, x, w)
            a.sse("sqrt", 0, 0, w)
            a.movq_from_xmm(d, 0, w)
            return
        self._round(op, d, x, w)

    def _round(self, op: str, d: int, x: Operand, w: bool) -> None:
        """ceil/floor/trunc/nearest without SSE4.1 `round*` (D24). |x| >= 2**52 (2**23 for f32),
        inf and NaN are integral already: x + 0.0 (quiets a NaN). Otherwise trunc goes through
        cvtt + cvtsi; nearest adds and subtracts 2**52 on |x| (rounds to even by MXCSR); the
        sign of x is put back (so -0.5 -> -0.0); floor/ceil step trunc by 1 toward -inf/+inf."""
        a = self.a
        assert d not in (RCX, RDX)
        big_bits = 0x4330000000000000 if w else 0x4B000000
        self.move(Reg(RCX), x, w)
        a.movq_to_xmm(0, RCX, w)
        a.mov(RDX, RCX, w=w)
        if w:
            a.bt_imm("btr", RDX, 63, w=True)
            a.mov_imm(d, big_bits, w=True)
            a.alu("cmp", RDX, d, w=True)
        else:
            a.alu_imm("and", RDX, 0x7FFFFFFF)
            a.alu_imm("cmp", RDX, big_bits)
        big, done = a.label(), a.label()
        a.jcc("ae", big)  # unsigned bit compare: NaN and inf are above 2**52 too
        if op == "nearest":
            a.movq_to_xmm(1, RDX, w)
            if not w:
                a.mov_imm(d, big_bits)
            a.movq_to_xmm(2, d, w)
            a.sse("add", 1, 2, w)
            a.sse("sub", 1, 2, w)
        else:
            a.cvttf2si(d, 0, w, w)
            self._cvtsi(1, d, w, w)
        a.movq_from_xmm(d, 1, w)
        self._sign_only(RCX, w)
        a.alu("or", d, RCX, w=w)
        if op in ("floor", "ceil"):
            a.movq_to_xmm(1, d, w)
            a.ucomis(1, 0, w)  # trunc(x) vs x
            a.jcc("be" if op == "floor" else "ae", done)
            self._kxmm(2, 1.0, w)
            a.sse("sub" if op == "floor" else "add", 1, 2, w)
            a.movq_from_xmm(d, 1, w)
        a.jmp(done)
        a.bind(big)
        a.sse("xor", 1, 1, w)
        a.sse("add", 0, 1, w)
        a.movq_from_xmm(d, 0, w)
        a.bind(done)

    def fcmp_set(self, cond: str, dst: Reg, x: Operand, y: Operand, wide: bool) -> None:
        """ucomis: unordered sets ZF PF CF, so lt/le are gt/ge swapped (above/above-or-equal are
        false on NaN), eq needs PF clear and ne is true on PF."""
        a, d, w = self.a, dst.id, wide
        if cond in ("lt", "le"):
            x, y, cond = y, x, "gt" if cond == "lt" else "ge"
        self._xmm(0, x, w)
        if isinstance(y, Slot):
            a.ucomis(0, _slot(y.k), w)
        else:
            self._xmm(1, y, w)
            a.ucomis(0, 1, w)
        a.setcc({"eq": "e", "ne": "ne", "gt": "a", "ge": "ae"}[cond], d)
        a.movzx(d, d, 8)
        if cond in ("eq", "ne"):  # only mov/movzx between ucomis and the cmov (LESSONS 1)
            a.mov_imm(RCX, int(cond == "ne"))
            a.cmov("p", d, RCX)

    def convert(self, f: Frame, op: str, dst: Reg, x: Operand) -> None:
        a, d = self.a, dst.id
        rt, name = op.split(".")
        parts = name.split("_")
        if parts[0] == "trunc":
            sat = parts[1] == "sat"
            self._trunc(f, d, x, parts[-2] == "f64", rt == "i64", parts[-1] == "s", sat)
            return
        double = rt == "f64"
        if parts[0] in ("demote", "promote"):
            self._xmm(0, x, not double)
            a.cvtff(0, 0, not double)
            a.movq_from_xmm(d, 0, double)
            return
        src64, signed = parts[1] == "i64", parts[2] == "s"
        self.move(Reg(RCX), x, src64)  # an i32 is zero-extended: its 64-bit value is unsigned
        if not src64:  # i32_s converts the 32-bit register, i32_u the zero-extended 64-bit one
            self._cvtsi(0, RCX, double, not signed)
        elif signed:
            self._cvtsi(0, RCX, double, True)
        else:  # u64: halve with the low bit kept (round to odd), convert, double (Liftoff, wasm2c)
            big, done = a.label(), a.label()
            a.test(RCX, RCX, w=True)
            a.jcc("s", big, short=True)
            self._cvtsi(0, RCX, double, True)
            a.jmp(done, short=True)
            a.bind(big)
            a.mov(RDX, RCX, w=True)
            a.shift("shr", RDX, 1, w=True)
            a.alu_imm("and", RCX, 1)
            a.alu("or", RDX, RCX, w=True)
            self._cvtsi(0, RDX, double, True)
            a.sse("add", 0, 0, double)
            a.bind(done)
        a.movq_from_xmm(d, 0, double)

    def _trunc(
        self, f: Frame, d: int, x: Operand, double: bool, w: bool, signed: bool, sat: bool
    ) -> None:
        """Float -> int, trapping (BAD_CONVERSION_TO_INTEGER on NaN, INTEGER_OVERFLOW out of
        range) or saturating. cvtt* gives INT_MIN for NaN and out-of-range values; only then is
        the input examined (the shape of Cranelift's x64 lowering)."""
        a = self.a
        assert d not in (RCX, RDX)
        bad = None if sat else self._stub(f, TrapCode.BAD_CONVERSION_TO_INTEGER)
        ovf = None if sat else self._stub(f, TrapCode.INTEGER_OVERFLOW)
        done = a.label()
        self._xmm(0, x, double)
        if signed:
            a.cvttf2si(d, 0, double, w)
            a.alu_imm("cmp", d, 1, w=w)  # overflows only for INT_MIN
            a.jcc("no", done)
            a.ucomis(0, 0, double)
            if sat:
                nan = a.label()
                a.jcc("p", nan, short=True)
                a.sse("xor", 1, 1, double)
                a.ucomis(0, 1, double)
                a.jcc("b", done, short=True)  # negative: INT_MIN is right
                a.mov_imm(d, (1 << 63) - 1 if w else (1 << 31) - 1, w=w)
                a.jmp(done, short=True)
                a.bind(nan)
                a.mov_imm(d, 0)
            else:
                assert bad and ovf
                a.jcc("p", bad)
                # INT_MIN is right iff trunc(x) = INT_MIN: x > -2**31 - 1 (f64 -> i32), else
                # x >= INT_MIN (exact in both formats); positive x overflowed
                lower = -(2.0**31) - 1 if double and not w else -(2.0 ** (63 if w else 31))
                self._kxmm(1, lower, double)
                a.ucomis(0, 1, double)
                a.jcc("be" if double and not w else "b", ovf)
                a.sse("xor", 1, 1, double)
                a.ucomis(0, 1, double)
                a.jcc("a", ovf)
        elif not w:  # u32: truncate to 64 bits, valid iff the upper half is 0
            a.cvttf2si(d, 0, double, True)
            a.mov(RCX, d, w=True)
            a.shift("shr", RCX, 32, w=True)
            a.jcc("e", done)
            if sat:  # NaN or x <= -1: 0; else (x >= 2**32) the maximum
                a.sse("xor", 1, 1, double)
                a.ucomis(0, 1, double)
                a.mov_imm(d, 0)
                a.jcc("be", done, short=True)
                a.mov_imm(d, M32)
            else:
                assert bad and ovf
                a.ucomis(0, 0, double)
                a.jcc("p", bad)
                a.jmp(ovf)
        else:  # u64: below 2**63 directly, else subtract 2**63 first and set bit 63 after
            big = a.label()
            self._kxmm(1, 2.0**63, double)
            a.ucomis(0, 1, double)
            if bad:
                a.jcc("p", bad)
            a.jcc("ae", big, short=True)  # not taken on NaN (CF = 1)
            a.cvttf2si(d, 0, double, True)
            a.test(d, d, w=True)  # negative: x <= -1, or NaN (sat)
            if ovf:
                a.jcc("s", ovf)
            else:
                a.jcc("ns", done)
                a.mov_imm(d, 0)
            a.jmp(done)
            a.bind(big)
            a.sse("sub", 0, 1, double)
            a.cvttf2si(d, 0, double, True)
            a.test(d, d, w=True)  # negative: x >= 2**64
            if ovf:
                a.jcc("s", ovf)
            else:
                fits = a.label()
                a.jcc("ns", fits, short=True)
                a.mov_imm(d, M64, w=True)
                a.jmp(done, short=True)
                a.bind(fits)
            a.bt_imm("btc", d, 63, w=True)
        a.bind(done)

    # ---------------------------------------------------------------- memories and context
    def _mem(self, mem: int) -> tuple[int | Mem, int | Mem]:
        """(base, size in bytes) of the memory whose base is context word `mem`: memory 0 is
        pinned in r13/r14; another one is read from its context words at each use (as Wasmtime
        does for a memory it keeps in no register), so it needs no reload after calls."""
        if 8 * mem == MEM0:
            return R13, R14
        return Mem(R15, 8 * mem), Mem(R15, 8 * mem + 8)

    def memory_size(self, dst: Reg, mem: int) -> None:
        self.a.mov(dst.id, self._mem(mem)[1], w=True)
        self.a.shift("shr", dst.id, 16, w=True)

    def _bounds(self, f: Frame, addr: Operand, offset: int, size: int, mem: int) -> Mem:
        """The bounds check (R3: the one place memory safety rests on). The effective address
        addr + offset (< 2**33) and its end are computed in 64 bits, so nothing wraps; the end
        must be <= the memory's size (r14 for memory 0) or the access traps. A register address
        is first copied with a 32-bit move, so its upper half cannot matter. The access is
        [r13 + ...] for memory 0, [rcx + ...] with rcx = base (+ address) for another; rdx is
        free afterwards."""
        a = self.a
        base, limit = self._mem(mem)
        oob = self._stub(f, TrapCode.MEMORY_OUT_OF_BOUNDS)
        if isinstance(addr, Imm):
            ea = (addr.value & M32) + offset
            if ea + size < 1 << 31:
                a.alu_imm("cmp", limit, ea + size, w=True)
                a.jcc("b", oob)
                if isinstance(base, int):
                    return Mem(base, ea)
                a.mov(RCX, base, w=True)
                return Mem(RCX, ea)
            a.mov_imm(RCX, ea, w=True)
            disp = 0
        else:
            self.move(Reg(RCX), addr, False)
            disp = offset
            if offset + size >= 1 << 31:
                a.mov_imm(RDX, offset, w=True)
                a.alu("add", RCX, RDX, w=True)
                disp = 0
        a.lea(RDX, Mem(RCX, disp + size))
        a.alu("cmp", RDX, limit, w=True)
        a.jcc("a", oob)
        if isinstance(base, int):
            return Mem(base, disp, RCX)
        a.alu("add", RCX, base, w=True)
        return Mem(RCX, disp)

    def load(
        self, f: Frame, size: int, signed: bool, wide: bool, dst: Reg, addr: Operand, offset: int,
        mem: int,
    ) -> None:  # fmt: skip
        a, d = self.a, dst.id
        m = self._bounds(f, addr, offset, size, mem)
        if size == 8:
            a.mov(d, m, w=True)
        elif size == 4:
            if signed:
                a.movsx(d, m, 32)
            else:
                a.mov(d, m)
        elif signed:
            a.movsx(d, m, 8 * size, w=wide)
        else:
            a.movzx(d, m, 8 * size)

    def store(
        self, f: Frame, size: int, addr: Operand, offset: int, src: Operand, mem: int
    ) -> None:  # fmt: skip
        a = self.a
        m = self._bounds(f, addr, offset, size, mem)
        imm = _imm32(src, size == 8)
        if imm is not None and size >= 4:
            a.mov_imm(m, imm, w=size == 8)
            return
        r = self._rm(src, RDX, size == 8)
        if not isinstance(r, int):
            a.mov(RDX, r, w=True)
            r = RDX
        if size == 1:
            a.store8(m, r)
        elif size == 2:
            a.store16(m, r)
        else:
            a.mov(m, r, w=size == 8)

    def ctx_load(self, dst: Reg, word: int, indirect: bool) -> None:
        if indirect:
            self.a.mov(RCX, Mem(R15, 8 * word), w=True)
            self.a.mov(dst.id, Mem(RCX), w=True)
        else:
            self.a.mov(dst.id, Mem(R15, 8 * word), w=True)

    def ctx_addr(self, dst: Reg, word: int) -> None:
        """dst = the address of context word `word` (a function record: `ref.func`)."""
        self.a.lea(dst.id, Mem(R15, 8 * word))

    def ctx_store(self, word: int, src: Operand, indirect: bool) -> None:
        r = self._rm(src, RDX, True)
        if not isinstance(r, int):
            self.a.mov(RDX, r, w=True)
            r = RDX
        if indirect:
            self.a.mov(RCX, Mem(R15, 8 * word), w=True)
            self.a.mov(Mem(RCX), r, w=True)
        else:
            self.a.mov(Mem(R15, 8 * word), r, w=True)

    # ---------------------------------------------------------------- tables and bulk memory
    # A table is `base` (element words) and `size` in context words `word`, `word + 1`, read at
    # every use: table.grow may move the elements, and runs only through the host bridge.
    def _u32(self, r: int, o: Operand) -> None:
        """r = the i32 operand, zero-extended (a 32-bit move even from r itself, LESSONS 5)."""
        if isinstance(o, Reg):
            self.a.mov(r, o.id)
        else:
            self.move(Reg(r), o, False)

    def _element(self, f: Frame, word: int, index: Operand) -> Mem:
        """Bounds-check index (rcx) against the table's size; the element's address via rdx."""
        a = self.a
        self._u32(RCX, index)
        a.alu("cmp", RCX, Mem(R15, 8 * word + 8), w=True)
        a.jcc("ae", self._stub(f, TrapCode.TABLE_OUT_OF_BOUNDS))
        a.mov(RDX, Mem(R15, 8 * word), w=True)
        return Mem(RDX, 0, RCX, 8)

    def table_get(self, f: Frame, dst: Reg, word: int, index: Operand) -> None:
        self.a.mov(dst.id, self._element(f, word, index), w=True)

    def table_set(self, f: Frame, word: int, index: Operand, value: Operand) -> None:
        """The index is read (into rcx) before anything is written: either operand may be in
        `rax`. Then `rax` is free unless it holds `value`."""
        elem = self._element(f, word, index)
        if not isinstance(value, Reg):
            self.move(Reg(RAX), value, True)  # a slot or an immediate: rcx/rdx are not touched
            value = Reg(RAX)
        self.a.mov(elem, value.id, w=True)

    def _three(self, x: Operand, y: Operand, z: Operand, wy: bool) -> None:
        """rax = x, rdx = y, rcx = z (i32 zero-extended; y 64-bit if `wy`). rcx and rdx never hold
        operands, so they are written first; rax (which may hold one) last."""
        if wy and isinstance(y, Reg):
            self.a.mov(RDX, y.id, w=True)
        elif wy:
            self.move(Reg(RDX), y, True)
        else:
            self._u32(RDX, y)
        self._u32(RCX, z)
        self._u32(RAX, x)

    def _end_check(self, f: Frame, r: int, size: int | Mem, code: int) -> None:
        """r + rcx (both < 2**32, no wrap in 64 bits) must not exceed `size`."""
        a = self.a
        a.alu("add", r, RCX, w=True)
        a.alu("cmp", r, size, w=True)
        a.jcc("a", self._stub(f, code))
        a.alu("sub", r, RCX, w=True)

    def _copy(self, unit: int) -> None:
        """Copy rcx bytes from [rdx] to [rax] as memmove does: backwards when the destination is
        above the source. 8 bytes at a time, then single bytes (`unit` 1); rsi is the temporary
        (saved: it may hold a pinned local). Plain moves only: no `rep movs` outside x86-64-v1's
        checked allowlist and no direction flag to restore."""
        a = self.a
        a.push(RSI)
        fwd, f1, b8, b1, done = (a.label() for _ in range(5))
        a.alu("cmp", RAX, RDX, w=True)
        a.jcc("be", fwd)
        a.bind(b8)
        a.alu_imm("cmp", RCX, 8, w=True)
        a.jcc("b", b1 if unit == 1 else done)
        a.alu_imm("sub", RCX, 8, w=True)
        a.mov(RSI, Mem(RDX, 0, RCX), w=True)
        a.mov(Mem(RAX, 0, RCX), RSI, w=True)
        a.jmp(b8)
        if unit == 1:
            a.bind(b1)
            a.test(RCX, RCX, w=True)
            a.jcc("e", done)
            a.alu_imm("sub", RCX, 1, w=True)
            a.movzx(RSI, Mem(RDX, 0, RCX), 8)
            a.store8(Mem(RAX, 0, RCX), RSI)
            a.jmp(b1)
        a.bind(fwd)
        a.alu_imm("cmp", RCX, 8, w=True)
        a.jcc("b", f1 if unit == 1 else done)
        a.mov(RSI, Mem(RDX), w=True)
        a.mov(Mem(RAX), RSI, w=True)
        for r in (RAX, RDX, RCX):
            a.alu_imm("sub" if r == RCX else "add", r, 8, w=True)
        a.jmp(fwd)
        if unit == 1:
            a.bind(f1)
            a.test(RCX, RCX, w=True)
            a.jcc("e", done)
            a.movzx(RSI, Mem(RDX), 8)
            a.store8(Mem(RAX), RSI)
            for r in (RAX, RDX, RCX):
                a.alu_imm("sub" if r == RCX else "add", r, 1, w=True)
            a.jmp(f1)
        a.bind(done)
        a.pop(RSI)

    def memory_copy(
        self, f: Frame, d: Operand, s: Operand, n: Operand, dmem: int, smem: int
    ) -> None:  # fmt: skip
        """Both ranges are checked before any byte moves (bulk memory: no partial copy). The two
        memory indices may name the same memory (one imported twice), so the direction is chosen by
        the real addresses in `_copy`, never by the indices."""
        a, oob = self.a, TrapCode.MEMORY_OUT_OF_BOUNDS
        (dbase, dlimit), (sbase, slimit) = self._mem(dmem), self._mem(smem)
        self._three(d, s, n, False)
        self._end_check(f, RAX, dlimit, oob)
        self._end_check(f, RDX, slimit, oob)
        a.alu("add", RAX, dbase, w=True)
        a.alu("add", RDX, sbase, w=True)
        self._copy(1)

    def memory_fill(self, f: Frame, d: Operand, v: Operand, n: Operand, mem: int) -> None:
        a = self.a
        base, limit = self._mem(mem)
        self._three(d, v, n, False)
        self._end_check(f, RAX, limit, TrapCode.MEMORY_OUT_OF_BOUNDS)
        a.alu("add", RAX, base, w=True)
        a.movzx(RDX, RDX, 8)
        a.push(RSI)
        a.mov_imm(RSI, 0x0101010101010101, w=True)
        a.imul(RDX, RSI, w=True)  # the byte in all eight
        a.pop(RSI)
        f8, f1, done = a.label(), a.label(), a.label()
        a.bind(f8)
        a.alu_imm("cmp", RCX, 8, w=True)
        a.jcc("b", f1)
        a.mov(Mem(RAX), RDX, w=True)
        a.alu_imm("add", RAX, 8, w=True)
        a.alu_imm("sub", RCX, 8, w=True)
        a.jmp(f8)
        a.bind(f1)
        a.test(RCX, RCX, w=True)
        a.jcc("e", done)
        a.store8(Mem(RAX), RDX)
        a.alu_imm("add", RAX, 1, w=True)
        a.alu_imm("sub", RCX, 1, w=True)
        a.jmp(f1)
        a.bind(done)

    def table_fill(self, f: Frame, word: int, i: Operand, v: Operand, n: Operand) -> None:
        a = self.a
        self._three(i, v, n, True)
        self._end_check(f, RAX, Mem(R15, 8 * word + 8), TrapCode.TABLE_OUT_OF_BOUNDS)
        a.shift("shl", RAX, 3, w=True)
        a.alu("add", RAX, Mem(R15, 8 * word), w=True)
        loop, done = a.label(), a.label()
        a.bind(loop)
        a.test(RCX, RCX, w=True)
        a.jcc("e", done)
        a.mov(Mem(RAX), RDX, w=True)
        a.alu_imm("add", RAX, 8, w=True)
        a.alu_imm("sub", RCX, 1, w=True)
        a.jmp(loop)
        a.bind(done)

    def table_copy(
        self, f: Frame, dst: int, src: int, d: Operand, s: Operand, n: Operand
    ) -> None:  # fmt: skip
        a, oob = self.a, TrapCode.TABLE_OUT_OF_BOUNDS
        self._three(d, s, n, False)
        self._end_check(f, RAX, Mem(R15, 8 * dst + 8), oob)
        self._end_check(f, RDX, Mem(R15, 8 * src + 8), oob)
        for r, word in ((RAX, dst), (RDX, src)):
            a.shift("shl", r, 3, w=True)
            a.alu("add", r, Mem(R15, 8 * word), w=True)
        a.shift("shl", RCX, 3, w=True)
        self._copy(8)

    # ---------------------------------------------------------------- calls
    def _call(self, f: Frame | None, target: Label | Mem, misaligned: Label | None = None) -> None:
        """`call target`; a `check_align` build first jumps to `misaligned` (default: the frame's
        MISALIGNED stub) if rsp % 16 != 0."""
        if self.check_align:
            self.a.test_imm(RSP, 15)
            self.a.jcc("ne", misaligned or self._stub(cast(Frame, f), STATUS_MISALIGNED))
        self.a.call(target)

    def call_direct(self, f: Frame, label: Label, shift_slots: int) -> None:
        """The callee's frame starts `shift_slots` above ours (frame overlap: its arguments are
        our slots there). A non-zero status unwinds this frame too."""
        a = self.a
        if shift_slots:
            a.lea(R12, Mem(R12, 8 * shift_slots))
        self._call(f, label)
        if shift_slots:
            a.lea(R12, Mem(R12, -8 * shift_slots))
        a.test(RAX, RAX)
        a.jcc("ne", f.unwind)

    def _table_record(self, f: Frame, table: int, sig: int, index: Operand) -> None:
        """rdx = the function record of element `index`: bounds (TABLE_OUT_OF_BOUNDS), null
        (INDIRECT_CALL_TO_NULL), its canonical type id against the signature word (BAD_SIGNATURE),
        in the spec's order (Wasmtime's codes)."""
        a = self.a
        elem = self._element(f, table, index)
        a.mov(RDX, elem, w=True)
        a.test(RDX, RDX, w=True)
        a.jcc("e", self._stub(f, TrapCode.INDIRECT_CALL_TO_NULL))
        a.mov(RCX, Mem(R15, 8 * sig), w=True)
        a.alu("cmp", RCX, Mem(RDX, 8), w=True)
        a.jcc("ne", self._stub(f, TrapCode.BAD_SIGNATURE))

    def _ref_record(self, f: Frame, ref: Operand) -> None:
        """rdx = a function reference (call_ref): null traps with NULL_REFERENCE; its type is
        right by validation."""
        self.move(Reg(RDX), ref, True)
        self.a.test(RDX, RDX, w=True)
        self.a.jcc("e", self._stub(f, TrapCode.NULL_REFERENCE))

    def _call_record(self, f: Frame, shift_slots: int) -> None:
        """Call the record in rdx on its own context: r15 is saved on the native stack (with a
        pad: 16-byte alignment holds), r13/r14 come from the callee's context and are reloaded
        from ours after. As Wasmtime's `call_indirect` lowering (a funcref is a pointer to
        `{code, type, vmctx}`; crates/cranelift/src/func_environ.rs), written anew."""
        a = self.a
        if shift_slots:
            a.lea(R12, Mem(R12, 8 * shift_slots))
        a.push(R15)
        a.alu_imm("sub", RSP, 8, w=True)
        a.mov(R15, Mem(RDX, 16), w=True)
        a.mov(R13, Mem(R15, MEM0), w=True)
        a.mov(R14, Mem(R15, MEM0 + 8), w=True)
        self._call(f, Mem(RDX))
        a.alu_imm("add", RSP, 8, w=True)
        a.pop(R15)
        a.mov(R13, Mem(R15, MEM0), w=True)
        a.mov(R14, Mem(R15, MEM0 + 8), w=True)
        if shift_slots:
            a.lea(R12, Mem(R12, -8 * shift_slots))
        a.test(RAX, RAX)
        a.jcc("ne", f.unwind)

    def call_indirect(
        self, f: Frame, table: int, sig: int, index: Operand, shift_slots: int
    ) -> None:  # fmt: skip
        self._table_record(f, table, sig, index)  # the index is read before r12 moves
        self._call_record(f, shift_slots)

    def call_ref(self, f: Frame, ref: Operand, shift_slots: int) -> None:
        self._ref_record(f, ref)
        self._call_record(f, shift_slots)

    def call_record(self, f: Frame, word: int, shift_slots: int) -> None:
        """A function import, through its record in context word `word` (D30): a function of
        another instance of the store runs on its own context; a host function's record is
        this module's thunk on our context."""
        self.a.lea(RDX, Mem(R15, 8 * word))
        self._call_record(f, shift_slots)

    def null_check(self, f: Frame, x: Operand, code: int) -> None:
        """Trap with `code` if the reference x is null; nothing but rcx is written."""
        r = x.id if isinstance(x, Reg) else RCX
        if not isinstance(x, Reg):
            self.move(Reg(RCX), x, True)
        self.a.test(r, r, w=True)
        self.a.jcc("e", self._stub(f, code))

    # ---------------------------------------------------------------- tail calls
    # The arguments move down to the frame base, the frame is torn down as by the epilogue (pad,
    # pins popped: the caller's pool registers are back), then `jmp`: rsp is the caller's return
    # address again (8 mod 16, as after a call) and r12 is unchanged, so both stacks stay
    # constant and the callee's results land where our caller expects ours. The callee's
    # prologue checks both stack limits and polls INTERRUPT. Every trap check comes before the
    # teardown. As Cranelift's `return_call` (callee-saved restore, outgoing arguments copied
    # over the incoming area, jump) and V8 Liftoff's PrepareTailCall, written anew.
    def _tail(self, f: Frame, src: int, n: int) -> None:
        """Slots src.. -> 0..n (src >= 0: ascending is safe under overlap; rcx only), teardown."""
        for j in range(n):
            if src:
                self.move(Slot(j), Slot(src + j), True)
        if f.pad:
            self.a.alu_imm("add", RSP, 8, w=True)
        for r in reversed(f.pins):
            self.a.pop(r)

    def tail_direct(self, f: Frame, label: Label, src: int, n: int) -> None:
        self._tail(f, src, n)
        self.a.jmp(label)

    def _tail_record(self) -> None:
        """Jump to the record in rdx: directly on our context, else through the tail_switch
        trampoline (abi.py, D30), which restores the context our caller expects."""
        a = self.a
        a.alu("cmp", R15, Mem(RDX, 16), w=True)
        other = a.label()
        a.jcc("ne", other, short=True)
        a.jmp(Mem(RDX))
        a.bind(other)
        a.jmp(Mem(R15, 8 * TAIL_SWITCH))

    def tail_indirect(
        self, f: Frame, table: int, sig: int, index: Operand, src: int, n: int
    ) -> None:  # fmt: skip
        self._table_record(f, table, sig, index)
        self._tail(f, src, n)
        self._tail_record()

    def tail_ref(self, f: Frame, ref: Operand, src: int, n: int) -> None:
        self._ref_record(f, ref)
        self._tail(f, src, n)
        self._tail_record()

    def tail_record(self, f: Frame, word: int, src: int, n: int) -> None:
        self._tail(f, src, n)
        self.a.lea(RDX, Mem(R15, 8 * word))
        self._tail_record()

    def host_thunk(self, word: int) -> int:
        """The code address a function record of an imported function points to: called like a
        wasm function (frame in r12, context in r15), it calls the import in context word `word`
        through the host bridge with its frame as the slots and returns the bridge's status."""
        a = self.a
        at = len(a.buf)
        a.endbr64()  # reached through `call [record]`
        a.alu_imm("sub", RSP, 8, w=True)  # rsp = 8 mod 16 on entry: aligned for the bridge
        a.mov(RDX, R12, w=True)
        a.mov_imm(RAX, word)
        bad = a.label()  # no frame here: a misaligned call returns the status itself
        self._call(None, Mem(R15, 8 * HOST_BRIDGE), bad)
        a.alu_imm("add", RSP, 8, w=True)
        a.mov(R13, Mem(R15, MEM0), w=True)  # as a tail-call target it returns to a caller that
        a.mov(R14, Mem(R15, MEM0 + 8), w=True)  # does not reload them (memory.grow may have run)
        a.ret()
        if self.check_align:
            a.bind(bad)
            a.mov_imm(RAX, STATUS_MISALIGNED)
            a.alu_imm("add", RSP, 8, w=True)
            a.ret()
        return at

    def call_host(self, f: Frame, word: int, slot: int) -> None:
        """Call the libcall in context word `word` through the bridge (imports go through their
        records, D32); its arguments and results are slots `slot`, `slot + 1`, ... of this frame."""
        a = self.a
        a.lea(RDX, Mem(R12, 8 * slot))
        a.mov_imm(RAX, word)
        self._call(f, Mem(R15, 8 * HOST_BRIDGE))
        a.mov(R13, Mem(R15, MEM0), w=True)  # memory.grow may have run
        a.mov(R14, Mem(R15, MEM0 + 8), w=True)
        a.test(RAX, RAX)
        a.jcc("ne", f.unwind)
