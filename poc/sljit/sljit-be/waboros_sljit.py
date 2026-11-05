"""Experiment: a waboros backend (the D39 interface, common/backend.py) written on sljit's API, under
waboros's own one-pass core (common/compiler.py), with its own trampolines; same context ABI,
same runtime (common/instance.py). Registers the target `jit-sljit`.

    import waboros_sljit          # then base.TARGETS["jit-sljit"]

sljit ABI used here (the whole convention is sljit's SLJIT_CALL_REG_ARG + SLJIT_ENTER_KEEP(3)):
  S0 context, S1 value-stack frame, S2 memory 0 base (kept: shared by caller and callee);
  R0 tos and the status; R1, R2 scratch inside one method;
  pool (pinnable, callee-saved for wasm): S3 S4 S5 (saved by sljit's enter) and R4 R5 R6 R3
  (saved by the function's own prologue into its native frame).
Memory 0's size is read from the context at every explicit check (sljit keeps at most three
registers across calls). Guard mode (S0006) reuses the x86-64 fault handler: a fault point is a
sljit label right before the one instruction that touches memory.

x86-64 only as written (relies on 32-bit sljit ops zero-extending, as x86-64 and aarch64 do; a
riscv64 port would add MOV_U32 after them). Not covered: tail calls, tables and bulk memory,
SIMD, f32/f64 min/max/nearest.
"""

import ctypes
import json
import os
import struct
from collections.abc import Sequence
from typing import Any

from waboros.targets import base
from waboros.targets.base import TrapCode
from waboros.targets.jit.common import guard as _guard
from waboros.targets.jit.common.backend import NEGATE, SWAP, AddK, Dst, Imm, Operand, Reg, Slot
from waboros.targets.jit.common.compiler import SUPPORTED, TABLES, WIDEOPS, compile_module
from waboros.targets.jit.common.context import (
    HOST_BRIDGE,
    HOST_ERROR,
    INTERRUPT,
    NSTACK_LIMIT,
    STATUS_HOST_ERROR,
    STATUS_INTERRUPT,
    TRAP_CODE,
    VSTACK_LIMIT,
    Layout,
)
from waboros.targets.jit.common.guard import GUARD
from waboros.targets.jit.x86_64 import JitX86_64Target

HERE = os.path.dirname(os.path.abspath(__file__))
K = json.load(open(os.path.join(HERE, "consts2.json")))
lib = ctypes.CDLL(os.path.join(HERE, os.environ.get("W2S_LIB", "libsljit2.so")))
P, I32, SW, UW = ctypes.c_void_p, ctypes.c_int32, ctypes.c_int64, ctypes.c_uint64
for _n, _r, _a in [
    ("sljit_create_compiler", P, [P]),
    ("sljit_free_compiler", None, [P]),
    ("sljit_generate_code", P, [P, I32, P]),
    ("sljit_free_code", None, [P, P]),
    ("sljit_emit_enter", I32, [P, I32, I32, I32, I32, I32]),
    ("sljit_emit_return", I32, [P, I32, I32, SW]),
    ("sljit_emit_op0", I32, [P, I32]),
    ("sljit_emit_op1", I32, [P, I32, I32, SW, I32, SW]),
    ("sljit_emit_op2", I32, [P, I32, I32, SW, I32, SW, I32, SW]),
    ("sljit_emit_op2u", I32, [P, I32, I32, SW, I32, SW]),
    ("sljit_emit_op_flags", I32, [P, I32, I32, SW, I32]),
    ("sljit_emit_select", I32, [P, I32, I32, I32, SW, I32]),
    ("sljit_emit_label", P, [P]),
    ("sljit_emit_jump", P, [P, I32]),
    ("sljit_emit_call", P, [P, I32, I32]),
    ("sljit_emit_cmp", P, [P, I32, I32, SW, I32, SW]),
    ("sljit_set_label", None, [P, P]),
    ("sljit_emit_icall", I32, [P, I32, I32, I32, SW]),
    ("sljit_get_local_base", I32, [P, I32, SW, SW]),
    ("sljit_emit_const", P, [P, I32, I32, SW, SW]),
    ("sljit_set_const", None, [UW, I32, SW, SW]),
    ("sljit_emit_fop1", I32, [P, I32, I32, SW, I32, SW]),
    ("sljit_emit_fop2", I32, [P, I32, I32, SW, I32, SW, I32, SW]),
    ("sljit_emit_fcopy", I32, [P, I32, I32, I32]),
    ("w2s_code_size", UW, [P]),
    ("w2s_label_addr", UW, [P]),
    ("w2s_const_addr", UW, [P]),
    ("w2s_error", I32, [P]),
    ("sljit_emit_op_addr", P, [P, I32, I32, SW]),
    ("sljit_emit_return_to", I32, [P, I32, SW]),
]:
    _f = getattr(lib, _n)
    _f.restype, _f.argtypes = _r, _a


def g(n: str) -> int:
    k = "SLJIT_" + n
    if k not in K and n.endswith("32"):  # SLJIT_ADD32 etc.: the word op | SLJIT_32
        return K[k[:-2]] | K["SLJIT_32"]
    return K[k]


R0, R1, R2, R3, R4, R5, R6 = (g(f"R{i}") for i in range(7))
S0, S1, S2, S3, S4, S5 = (g(f"S{i}") for i in range(6))
FR0, FR1, FR2 = g("FR0"), g("FR1"), g("FR2")
SP, IMM, MEM, B32 = g("SP"), g("IMM"), g("MEM"), g("32")
CTX, FRM, MB = S0, S1, S2
POOL = (S3, S4, S5, R4, R5, R6, R3)  # R3 is rcx on x86-64: variable shifts want it, so last
SPOOL = (S3, S4, S5)
MEM0 = Layout().memory(0)  # context word of memory 0's base
M32, M64 = 0xFFFFFFFF, (1 << 64) - 1
CALL_RETURN = 0x20000  # SLJIT_CALL_RETURN
REG_ENTER = g("ENTER_REG_ARG") | 3  # SLJIT_ENTER_KEEP(3): S0..S2 shared
NLOCAL = 12  # native frame words: R pins (4), saved context (1), saves around C calls (7)
CTXSAVE, CSAVE = 8 * 4, 8 * 5


def MEM1(r: int) -> int:
    return MEM | r


def MEM2(r: int, i: int) -> int:
    return MEM | r | (i << 8)


def s64(v: int) -> int:
    v &= M64
    return v - (1 << 64) if v >> 63 else v


def s32(v: int) -> int:
    v &= M32
    return v - (1 << 32) if v >> 31 else v


COND = {"eq": "EQUAL", "ne": "NOT_EQUAL", "lt_s": "SIG_LESS", "lt_u": "LESS",
        "gt_s": "SIG_GREATER", "gt_u": "GREATER", "le_s": "SIG_LESS_EQUAL",
        "le_u": "LESS_EQUAL", "ge_s": "SIG_GREATER_EQUAL", "ge_u": "GREATER_EQUAL"}  # fmt: skip
FCOND = {"eq": "ORDERED_EQUAL", "ne": "UNORDERED_OR_NOT_EQUAL", "lt": "ORDERED_LESS",
         "gt": "ORDERED_GREATER", "le": "ORDERED_LESS_EQUAL", "ge": "ORDERED_GREATER_EQUAL"}  # fmt: skip
BIN = {"add": "ADD", "sub": "SUB", "mul": "MUL", "and": "AND", "or": "OR", "xor": "XOR",
       "shl": "MSHL", "shr_s": "MASHR", "shr_u": "MLSHR", "rotl": "ROTL", "rotr": "ROTR"}  # fmt: skip


def setflag(cond: int) -> int:
    """The SLJIT_SET_* flag a compare must set for the condition `cond` to be tested."""
    return g("SET_Z") if cond in (g("EQUAL"), g("NOT_EQUAL")) else (cond & ~1) << 10


def _holds(cond: str, a: int, b: int, wide: bool) -> bool:
    bits = 64 if wide else 32
    m = (1 << bits) - 1
    a, b = a & m, b & m
    if cond.endswith("_s"):
        a, b = a - ((a >> (bits - 1)) << bits), b - ((b >> (bits - 1)) << bits)
    op = cond.split("_")[0]
    return {"eq": a == b, "ne": a != b, "lt": a < b, "gt": a > b, "le": a <= b, "ge": a >= b}[op]


class Label:
    __slots__ = ("lab", "pend")

    def __init__(self) -> None:
        self.lab: int | None = None
        self.pend: list[int] = []


class Frame:
    def __init__(self, pins: tuple[int, ...]) -> None:
        self.rpins = [r for r in pins if r not in SPOOL]
        self.unwind = Label()
        self.traps: dict[int, Label] = {}
        self.vconst = 0


class Emitter:
    """A sljit compiler and the label/jump bookkeeping both the backend and the trampolines use."""

    def __init__(self) -> None:
        self.c = lib.sljit_create_compiler(None)
        self.marks: list[int] = []  # sljit labels whose offsets the caller wants back

    def op1(self, op: int, d: tuple[int, int], s: tuple[int, int]) -> None:
        lib.sljit_emit_op1(self.c, op, d[0], d[1], s[0], s[1])

    def op2(self, op: int, d: tuple[int, int], x: tuple[int, int], y: tuple[int, int]) -> None:
        lib.sljit_emit_op2(self.c, op, d[0], d[1], x[0], x[1], y[0], y[1])

    def new_label(self) -> Label:
        return Label()

    def bind(self, label: Label) -> None:
        label.lab = lib.sljit_emit_label(self.c)
        for j in label.pend:
            lib.sljit_set_label(j, label.lab)
        label.pend.clear()

    def link(self, j: int, label: Label) -> None:
        if label.lab is not None:
            lib.sljit_set_label(j, label.lab)
        else:
            label.pend.append(j)

    def jump(self, label: Label) -> None:
        self.link(lib.sljit_emit_jump(self.c, g("JUMP")), label)

    def cmpj(self, cond: int, x: tuple[int, int], y: tuple[int, int], label: Label) -> None:
        self.link(lib.sljit_emit_cmp(self.c, cond, x[0], x[1], y[0], y[1]), label)

    def mark(self) -> int:
        self.marks.append(lib.sljit_emit_label(self.c))
        return len(self.marks) - 1

    def generate(self) -> tuple[bytes, int, int]:
        """(code bytes, address of the buffer, size); the caller frees with `free`."""
        err = lib.w2s_error(self.c)
        if err:
            raise RuntimeError(f"sljit compiler error {err}")
        code = lib.sljit_generate_code(self.c, 0, None)
        if not code:
            raise RuntimeError(f"sljit_generate_code failed ({lib.w2s_error(self.c)})")
        size = lib.w2s_code_size(self.c)
        return code, size

    def free(self, code: int) -> None:
        lib.sljit_free_code(code, None)
        lib.sljit_free_compiler(self.c)


def _o(o: Operand, wide: bool) -> tuple[int, int]:
    if isinstance(o, Imm):
        return IMM, s64(o.value) if wide else s32(o.value)
    if isinstance(o, Reg):
        return o.id, 0
    return MEM1(FRM), 8 * o.k


def _w(op: str, wide: bool) -> int:
    return g(op) | (0 if wide else B32)


class Backend(Emitter):
    pool = POOL
    tos = R0
    # no tail calls, no tables/bulk memory, no f32/f64 min max nearest (see the module docstring)
    supported = SUPPORTED | set(WIDEOPS)  # the core's scalar set and wide arithmetic (no SIMD)

    def __init__(self, *, guard: bool = False) -> None:
        super().__init__()
        self.guard = guard
        self.consts: list[tuple[int, int]] = []  # (sljit const, value) patched after generation
        self.offset: list[int] = []  # mark -> code offset, by finish
        self.faults: list[tuple[int, int]] = []
        self._faults: list[tuple[int, Label]] = []
        self._oob: Label | None = None
        self.f: Frame | None = None

    # ------------------------------------------------------------ labels, frames, traps
    def here(self) -> int:
        return self.mark()

    def func_begin(self, label: Label, pins: tuple[int, ...] = (), far: bool = False) -> Frame:
        c = self.c
        self.bind(label)
        f = self.f = Frame(pins)
        ns = sum(r in SPOOL for r in pins)
        assert all(r in SPOOL for r in pins[:ns]), pins
        lib.sljit_emit_enter(c, REG_ENTER, g("ARGS0(32)"), 7 | (3 << 8), 3 + ns, 8 * NLOCAL)
        for i, r in enumerate(f.rpins):
            self.op1(g("MOV"), (MEM1(SP), 8 * i), (r, 0))
        lib.sljit_get_local_base(c, R1, 0, 0)
        self.cmpj(g("LESS"), (R1, 0), (MEM1(CTX), 8 * NSTACK_LIMIT), self._stub(f, TrapCode.STACK_OVERFLOW))
        f.vconst = lib.sljit_emit_const(c, g("MOV"), R1, 0, 1 << 20)  # 8 * frame_slots, patched
        self.op2(g("ADD"), (R1, 0), (R1, 0), (FRM, 0))
        self.cmpj(g("GREATER"), (R1, 0), (MEM1(CTX), 8 * VSTACK_LIMIT), self._stub(f, TrapCode.STACK_OVERFLOW))
        self.poll(f)
        return f

    def func_end(self, f: Frame, frame_slots: int) -> bool:
        self.consts.append((f.vconst, 8 * frame_slots))
        for code, label in f.traps.items():
            self.bind(label)
            self.op1(g("MOV32"), (R0, 0), (IMM, code))
            self.jump(f.unwind)
        self.bind(f.unwind)
        for i, r in enumerate(f.rpins):
            self.op1(g("MOV"), (r, 0), (MEM1(SP), 8 * i))
        lib.sljit_emit_return(self.c, g("MOV32"), R0, 0)
        return True

    def rewind(self, pos: int) -> None:
        raise AssertionError("sljit branches always reach")

    def _stub(self, f: Frame, code: int) -> Label:
        if code not in f.traps:
            f.traps[code] = Label()
        return f.traps[code]

    def poll(self, f: Frame) -> None:
        self.cmpj(g("NOT_EQUAL"), (MEM1(CTX), 8 * INTERRUPT), (IMM, 0), self._stub(f, STATUS_INTERRUPT))

    def trap(self, f: Frame, code: int) -> None:
        self.jump(self._stub(f, code))

    def ret_ok(self, f: Frame) -> None:
        self.op1(g("MOV32"), (R0, 0), (IMM, 0))
        self.jump(f.unwind)

    def finish(self) -> tuple[bytes, int]:
        if not self.marks:  # no function: sljit cannot generate an empty buffer
            lib.sljit_emit_enter(self.c, REG_ENTER, g("ARGS0(32)"), 1, 3, 0)
            lib.sljit_emit_return(self.c, g("MOV32"), IMM, 0)
        code, size = self.generate()
        for k, v in self.consts:
            lib.sljit_set_const(lib.w2s_const_addr(k), g("MOV"), v, 0)
        self.offset = [lib.w2s_label_addr(m) - code for m in self.marks]
        self.faults = [(lib.w2s_label_addr(p) - code, lib.w2s_label_addr(s.lab) - code)
                       for p, s in self._faults]  # fmt: skip
        data = ctypes.string_at(code, size)
        self.free(code)
        return data, len(data)

    # ------------------------------------------------------------ data
    def move(self, dst: Dst, src: Operand | AddK, wide: bool) -> None:
        if isinstance(src, AddK):
            r = dst.id if isinstance(dst, Reg) else R1
            self.op2(g("ADD32"), (r, 0), (src.id, 0), (IMM, s32(src.k)))
            if isinstance(dst, Slot):
                self.op1(g("MOV"), _o(dst, True), (R1, 0))
            return
        if isinstance(dst, Reg):
            if isinstance(src, Imm):
                self.op1(g("MOV"), (dst.id, 0), (IMM, s64(src.value & (M64 if wide else M32))))
            elif isinstance(src, Reg):
                if src.id != dst.id:
                    self.op1(g("MOV" if wide else "MOV_U32"), (dst.id, 0), (src.id, 0))
            else:
                self.op1(g("MOV" if wide else "MOV_U32"), (dst.id, 0), _o(src, True))
            return
        if isinstance(src, Imm):
            self.op1(g("MOV"), _o(dst, True), (IMM, s64(src.value)))
        elif isinstance(src, Reg):
            self.op1(g("MOV"), _o(dst, True), (src.id, 0))
        elif src.k != dst.k:
            self.op1(g("MOV"), (R1, 0), _o(src, True))
            self.op1(g("MOV"), _o(dst, True), (R1, 0))

    def binop(self, op: str, dst: Reg, x: Operand, y: Operand, wide: bool) -> None:
        if isinstance(x, Imm) and isinstance(y, Imm):
            self.move(Reg(R1), x, wide)
            x = Reg(R1)
        self.op2(_w(BIN[op], wide), (dst.id, 0), _o(x, wide), _o(y, wide))

    def divrem(self, f: Frame, op: str, dst: Reg, x: Operand, y: Operand, wide: bool) -> None:
        mask = M64 if wide else M32
        signed, rem = op.endswith("_s"), op.startswith("rem")
        known = y.value & mask if isinstance(y, Imm) else None
        self.move(Reg(R2), y, wide)
        if not known:
            self.cmpj(_w("EQUAL", wide), (R2, 0), (IMM, 0), self._stub(f, TrapCode.INTEGER_DIVISION_BY_ZERO))
        self.move(Reg(R0), x, wide)
        done = None
        if signed and (known is None or known == mask):
            go = Label()
            self.cmpj(_w("NOT_EQUAL", wide), (R2, 0), (IMM, -1), go)
            if rem:
                self.op1(g("MOV"), (R1, 0), (IMM, 0))
                done = Label()
                self.jump(done)
            else:
                lo = -(1 << 63) if wide else -(1 << 31)
                self.cmpj(_w("EQUAL", wide), (R0, 0), (IMM, lo), self._stub(f, TrapCode.INTEGER_OVERFLOW))
            self.bind(go)
        self.op1(g("MOV"), (R1, 0), (R2, 0))
        lib.sljit_emit_op0(self.c, _w("DIVMOD_SW" if signed else "DIVMOD_UW", wide))
        if done:
            self.bind(done)
        res = R1 if rem else R0
        if not wide:
            self.op1(g("MOV_U32"), (dst.id, 0), (res, 0))
        elif dst.id != res:
            self.op1(g("MOV"), (dst.id, 0), (res, 0))

    def unop(self, op: str, dst: Reg, x: Operand, wide: bool) -> None:
        if isinstance(x, Imm):
            self.move(Reg(R1), x, True)
            x = Reg(R1)
        d, src = (dst.id, 0), _o(x, wide)
        if op in ("clz", "ctz"):
            self.op1(_w(op.upper(), wide), d, src)
        elif op == "popcnt":
            self._popcnt(dst.id, x, wide)
        elif op in ("extend8_s", "extend16_s"):
            n = "S8" if op == "extend8_s" else "S16"
            self.op1(g(f"MOV_{n}" if wide else f"MOV32_{n}"), d, src)
            if not wide:  # sljit's 32-bit sign extension may fill the upper half
                self.op1(g("MOV_U32"), d, d)
        elif op in ("extend32_s", "extend_i32_s"):
            self.op1(g("MOV_S32"), d, src)
        else:
            assert op in ("extend_i32_u", "wrap"), op
            self.op1(g("MOV_U32"), d, src)

    def _popcnt(self, d: int, x: Operand, w: bool) -> None:
        k = [0x5555555555555555, 0x3333333333333333, 0x0F0F0F0F0F0F0F0F, 0x0101010101010101]
        if not w:
            k = [v & M32 for v in k]
        ADD, SUB, AND, SHR, MUL = (_w(n, w) for n in ("ADD", "SUB", "AND", "MLSHR", "MUL"))
        imm = lambda v: (IMM, s64(v) if w else s32(v))  # noqa: E731
        self.move(Reg(R2), x, w)
        self.op2(SHR, (R1, 0), (R2, 0), (IMM, 1))
        self.op2(AND, (R1, 0), (R1, 0), imm(k[0]))
        self.op2(SUB, (R2, 0), (R2, 0), (R1, 0))
        self.op2(SHR, (R1, 0), (R2, 0), (IMM, 2))
        self.op2(AND, (R1, 0), (R1, 0), imm(k[1]))
        self.op2(AND, (R2, 0), (R2, 0), imm(k[1]))
        self.op2(ADD, (R2, 0), (R2, 0), (R1, 0))
        self.op2(SHR, (R1, 0), (R2, 0), (IMM, 4))
        self.op2(ADD, (R2, 0), (R2, 0), (R1, 0))
        self.op2(AND, (R2, 0), (R2, 0), imm(k[2]))
        self.op2(MUL, (R2, 0), (R2, 0), imm(k[3]))
        self.op2(SHR, (d, 0), (R2, 0), (IMM, (64 if w else 32) - 8))

    def _flags(self, cond: str, x: Operand, y: Operand, wide: bool) -> int:
        """Compare (flags only); the sljit condition to test."""
        if isinstance(x, Imm) and isinstance(y, Imm):
            self.move(Reg(R1), x, wide)
            x = Reg(R1)
        t = g(COND[cond])
        lib.sljit_emit_op2u(self.c, _w("SUB", wide) | setflag(t), *_o(x, wide), *_o(y, wide))
        return t

    def cmp_set(self, cond: str, dst: Reg, x: Operand, y: Operand, wide: bool) -> None:
        t = self._flags(cond, x, y, wide)
        lib.sljit_emit_op_flags(self.c, g("MOV"), dst.id, 0, t)

    def cmp_branch(self, cond: str, x: Operand, y: Operand, label: Label, wide: bool) -> None:
        if isinstance(x, Imm) and isinstance(y, Imm):
            self.move(Reg(R1), x, wide)
            x = Reg(R1)
        self.cmpj(_w(COND[cond], wide), _o(x, wide), _o(y, wide), label)

    def select_cmp(
        self, cond: str, x: Operand, y: Operand, wc: bool, dst: Reg, t: Operand, e: Operand,
        wv: bool,
    ) -> None:  # fmt: skip
        """dst = t if (x cond y) else e. Both values are zero-extended, so a word-sized select."""
        if isinstance(x, Imm) and isinstance(y, Imm):  # R1 would be needed twice: decide now
            self.move(dst, t if _holds(cond, x.value, y.value, wc) else e, wv)
            return
        self.move(Reg(R2), e, wv)
        if isinstance(t, Imm):
            self.move(Reg(R1), t, wv)
            t = Reg(R1)
        tt = _o(t, True)
        tc = self._flags(cond, x, y, wc)
        lib.sljit_emit_select(self.c, tc, dst.id, tt[0], tt[1], R2)

    def jump_table(self, index: Operand, labels: Sequence[Label], default: Label) -> None:
        """A binary search over the index (sljit has no PC-relative table load we can keep
        position independent)."""
        if not labels:
            self.jump(default)
            return
        self.move(Reg(R1), index, False)
        self.cmpj(g("GREATER_EQUAL"), (R1, 0), (IMM, len(labels)), default)

        def tree(lo: int, hi: int) -> None:
            if hi - lo <= 3:
                for i in range(lo, hi - 1):
                    self.cmpj(g("EQUAL"), (R1, 0), (IMM, i), labels[i])
                self.jump(labels[hi - 1])
                return
            mid = (lo + hi) // 2
            left = Label()
            self.cmpj(g("LESS"), (R1, 0), (IMM, mid), left)
            tree(mid, hi)
            self.bind(left)
            tree(lo, mid)

        tree(0, len(labels))

    # ------------------------------------------------------------ floats (bit patterns in GPRs)
    def _fld(self, fr: int, o: Operand, w: bool) -> None:
        if isinstance(o, Slot):
            lib.sljit_emit_fop1(self.c, g("MOV_F64" if w else "MOV_F32"), fr, 0, *_o(o, True))
            return
        if isinstance(o, Imm):
            self.move(Reg(R1), o, w)
            o = Reg(R1)
        lib.sljit_emit_fcopy(self.c, g("COPY_TO_F64") | (0 if w else B32), fr, o.id)

    def _fst(self, d: int, fr: int, w: bool) -> None:
        lib.sljit_emit_fcopy(self.c, g("COPY_FROM_F64") | (0 if w else B32), fr, d)

    def fbinop(self, op: str, dst: Reg, x: Operand, y: Operand, wide: bool) -> None:
        if op in ("min", "max"):
            self._fminmax(op, dst.id, x, y, wide)
            return
        if op == "copysign":
            sign = 1 << (63 if wide else 31)
            m = (lambda v: (IMM, s64(v))) if wide else (lambda v: (IMM, s32(v)))  # noqa: E731
            self.move(Reg(R2), y, wide)
            self.op2(_w("AND", wide), (R2, 0), (R2, 0), m(sign))
            self.move(Reg(R1), x, wide)
            self.op2(_w("AND", wide), (R1, 0), (R1, 0), m(~sign))
            self.op2(_w("OR", wide), (dst.id, 0), (R1, 0), (R2, 0))
            return
        f = {"add": "ADD_F64", "sub": "SUB_F64", "mul": "MUL_F64", "div": "DIV_F64"}[op]
        self._fld(FR0, x, wide)
        self._fld(FR1, y, wide)
        lib.sljit_emit_fop2(self.c, g(f) | (0 if wide else B32), FR0, 0, FR0, 0, FR1, 0)
        self._fst(dst.id, FR0, wide)

    def funop(self, op: str, dst: Reg, x: Operand, wide: bool) -> None:
        sign = 1 << (63 if wide else 31)
        if op in ("abs", "neg"):  # bit operations, exactly wasm's
            self.move(Reg(R1), x, wide)
            v = ~sign if op == "abs" else sign
            self.op2(_w("AND" if op == "abs" else "XOR", wide), (dst.id, 0), (R1, 0),
                     (IMM, s64(v) if wide else s32(v)))  # fmt: skip
            return
        # ceil floor trunc sqrt: the C library, around which every live R register is saved
        self._fld(FR0, x, wide)
        name = ("nearbyint" if op == "nearest" else op) + ("" if wide else "f")
        addr = ctypes.cast(getattr(_LIBM, name), ctypes.c_void_p).value
        self._ccall(addr, g("ARGS1(F64, F64)") if wide else g("ARGS1(F32, F32)"), dst.id)
        self._fst(dst.id, FR0, wide)

    def _ccall(self, addr: int, args: int, dst: int) -> None:
        live = [r for r in (R0, R3, R4, R5, R6) if r != dst]
        for i, r in enumerate(live):
            self.op1(g("MOV"), (MEM1(SP), CSAVE + 8 * i), (r, 0))
        self.op1(g("MOV"), (MEM1(SP), CSAVE + 40), (IMM, s64(addr)))  # never a PC-relative call
        lib.sljit_emit_icall(self.c, g("CALL"), args, MEM1(SP), CSAVE + 40)
        for i, r in enumerate(live):
            self.op1(g("MOV"), (r, 0), (MEM1(SP), CSAVE + 8 * i))

    def fcmp_set(self, cond: str, dst: Reg, x: Operand, y: Operand, wide: bool) -> None:
        self._fld(FR0, x, wide)
        self._fld(FR1, y, wide)
        t = g(FCOND[cond])
        lib.sljit_emit_fop1(self.c, g("CMP_F64") | (0 if wide else B32) | setflag(t), FR0, 0, FR1, 0)
        lib.sljit_emit_op_flags(self.c, g("MOV"), dst.id, 0, t)

    def _fconst(self, fr: int, v: float, w: bool) -> None:
        bits = struct.unpack("<Q", struct.pack("<d", v))[0] if w else struct.unpack("<I", struct.pack("<f", v))[0]
        self.op1(g("MOV"), (R1, 0), (IMM, s64(bits)))
        lib.sljit_emit_fcopy(self.c, g("COPY_TO_F64") | (0 if w else B32), fr, R1)

    def _fcmpj(self, cond: str, a: int, b: int, w: bool, label: Label) -> None:
        t = g(cond)
        lib.sljit_emit_fop1(self.c, g("CMP_F64") | (0 if w else B32) | setflag(t), a, 0, b, 0)
        self.link(lib.sljit_emit_jump(self.c, t), label)

    def convert(self, f: Frame, op: str, dst: Reg, x: Operand) -> None:
        r, _, rest = op.partition(".")
        d = dst.id
        if rest.startswith("convert_"):  # f32/f64 from i32/i64, s/u
            _, src, = rest.split("_", 1)
            ti, sg = src.split("_")
            name = f"CONV_F64_FROM_{'S' if sg == 's' else 'U'}{'32' if ti == 'i32' else 'W'}"
            xo = _o(x, ti == "i64") if not isinstance(x, Imm) else None
            if xo is None:
                self.move(Reg(R1), x, ti == "i64")
                xo = (R1, 0)
            lib.sljit_emit_fop1(self.c, g(name) | (0 if r == "f64" else B32), FR0, 0, *xo)
            self._fst(d, FR0, r == "f64")
            return
        if rest in ("promote_f32", "demote_f64"):
            w = rest == "demote_f64"
            self._fld(FR0, x, w)
            name = "CONV_F64_FROM_F32" if r == "f64" else "CONV_F32_FROM_F64"
            lib.sljit_emit_fop1(self.c, g(name), FR0, 0, FR0, 0)
            self._fst(d, FR0, r == "f64")
            return
        # i32/i64.trunc[_sat]_f32/f64_s/u
        sat = "_sat_" in rest
        fsrc, sg = rest.split("_")[-2:]
        w, n, signed = fsrc == "f64", 64 if r == "i64" else 32, sg == "s"
        self._fld(FR0, x, w)
        nan, low, high, done = Label(), Label(), Label(), Label()
        self._fcmpj("UNORDERED", FR0, FR0, w, nan)
        if signed:
            lo = -(2.0 ** (n - 1)) - 1
            if w and n == 32:
                self._fconst(FR1, lo, w)
                self._fcmpj("ORDERED_LESS_EQUAL", FR0, FR1, w, low)
            else:
                self._fconst(FR1, -(2.0 ** (n - 1)), w)
                self._fcmpj("ORDERED_LESS", FR0, FR1, w, low)
            self._fconst(FR1, 2.0 ** (n - 1), w)
        else:
            self._fconst(FR1, -1.0, w)
            self._fcmpj("ORDERED_LESS_EQUAL", FR0, FR1, w, low)
            self._fconst(FR1, 2.0**n, w)
        self._fcmpj("ORDERED_GREATER_EQUAL", FR0, FR1, w, high)
        fl = 0 if w else B32
        if n == 32 and signed:
            lib.sljit_emit_fop1(self.c, g("CONV_S32_FROM_F64") | fl, d, 0, FR0, 0)
            self.op1(g("MOV_U32"), (d, 0), (d, 0))
        elif n == 32 or signed:  # u32 through i64; s64 directly
            lib.sljit_emit_fop1(self.c, g("CONV_SW_FROM_F64") | fl, d, 0, FR0, 0)
            if n == 32:
                self.op1(g("MOV_U32"), (d, 0), (d, 0))
        else:  # u64: below 2**63 directly, else x - 2**63 with the top bit set back
            big = Label()
            self._fconst(FR2, 2.0**63, w)
            self._fcmpj("ORDERED_GREATER_EQUAL", FR0, FR2, w, big)
            lib.sljit_emit_fop1(self.c, g("CONV_SW_FROM_F64") | fl, d, 0, FR0, 0)
            self.jump(done)
            self.bind(big)
            lib.sljit_emit_fop2(self.c, g("SUB_F64") | fl, FR0, 0, FR0, 0, FR2, 0)
            lib.sljit_emit_fop1(self.c, g("CONV_SW_FROM_F64") | fl, d, 0, FR0, 0)
            self.op2(g("XOR"), (d, 0), (d, 0), (IMM, -(1 << 63)))
        self.jump(done)
        if sat:
            mx = (1 << (n - 1)) - 1 if signed else (1 << n) - 1
            mn = (1 << (n - 1)) if signed else 0  # as the unsigned bits of INT_MIN
            for lab, v in ((nan, 0), (low, mn), (high, mx)):
                self.bind(lab)
                self.op1(g("MOV"), (d, 0), (IMM, s64(v)))
                self.jump(done)
        else:
            self.bind(nan)
            self.jump(self._stub(f, TrapCode.BAD_CONVERSION_TO_INTEGER))
            self.bind(low)
            self.bind(high)
            self.jump(self._stub(f, TrapCode.INTEGER_OVERFLOW))
        self.bind(done)

    # ------------------------------------------------------------ memories, context
    def memory_size(self, dst: Reg, mem: int) -> None:
        self.op1(g("MOV"), (dst.id, 0), (MEM1(CTX), 8 * mem + 8))
        self.op2(g("MLSHR"), (dst.id, 0), (dst.id, 0), (IMM, 16))

    def _access(self, f: Frame, addr: Operand | AddK, offset: int, size: int, mem: int) -> tuple[int, int]:
        """The memory operand of an access; bounds checked (explicit) or a fault point armed
        (guard). R1 holds the index; R2 is free afterwards."""
        limit = (MEM1(CTX), 8 * mem + 8)
        guarded = self.guard and (
            (addr.value & M32) + offset + size <= (1 << 32) + GUARD if isinstance(addr, Imm)
            else offset + size <= GUARD
        )  # fmt: skip
        oob = self._stub(f, TrapCode.MEMORY_OUT_OF_BOUNDS)
        if isinstance(addr, Imm):
            ea = (addr.value & M32) + offset
            if not guarded:
                self.cmpj(g("LESS"), limit, (IMM, ea + size), oob)
            if mem == MEM0 and ea < 1 << 31:
                res = (MEM1(MB), ea)
            else:
                self.op1(g("MOV"), (R1, 0), (IMM, ea))
                res = self._base(R1, mem)
        else:
            if isinstance(addr, AddK):
                self.op2(g("ADD32"), (R1, 0), (addr.id, 0), (IMM, s32(addr.k)))
            else:
                self.op1(g("MOV_U32"), (R1, 0), _o(addr, True))
            if offset:
                self.op2(g("ADD"), (R1, 0), (R1, 0), (IMM, offset))
            if not guarded:
                self.op2(g("ADD"), (R2, 0), (R1, 0), (IMM, size))
                self.cmpj(g("GREATER"), (R2, 0), limit, oob)
            res = self._base(R1, mem)
        if guarded:
            self._oob = oob
        return res

    def _base(self, idx: int, mem: int) -> tuple[int, int]:
        if mem == MEM0:
            return MEM2(MB, idx), 0
        self.op2(g("ADD"), (idx, 0), (idx, 0), (MEM1(CTX), 8 * mem))
        return MEM1(idx), 0

    def _fault_point(self) -> None:
        if self._oob is not None:
            self._faults.append((lib.sljit_emit_label(self.c), self._oob))
            self._oob = None

    def load(
        self, f: Frame, size: int, signed: bool, wide: bool, dst: Reg, addr: Operand | AddK,
        offset: int, mem: int,
    ) -> None:  # fmt: skip
        m = self._access(f, addr, offset, size, mem)
        if size == 8:
            op = "MOV"
        elif size == 4:
            op = "MOV_S32" if signed else "MOV_U32"
        else:
            b = "8" if size == 1 else "16"
            op = (f"MOV_S{b}" if wide else f"MOV32_S{b}") if signed else f"MOV_U{b}"
        self._fault_point()
        self.op1(g(op), (dst.id, 0), m)
        if signed and size < 4 and not wide:  # as in `unop`: keep the i32 zero-extended
            self.op1(g("MOV_U32"), (dst.id, 0), (dst.id, 0))

    def store(
        self, f: Frame, size: int, addr: Operand | AddK, offset: int, src: Operand, mem: int
    ) -> None:  # fmt: skip
        m = self._access(f, addr, offset, size, mem)
        s = _o(src, True)
        if isinstance(src, Slot) or (isinstance(src, Imm) and not -(1 << 31) <= s[1] < 1 << 31):
            self.op1(g("MOV"), (R2, 0), s)  # one instruction touches memory (fault point)
            s = (R2, 0)
        op = {1: "MOV_U8", 2: "MOV_U16", 4: "MOV_U32", 8: "MOV"}[size]
        if size == 4 and s[0] == IMM:
            s = (IMM, s32(s[1]))
        self._fault_point()
        self.op1(g(op), m, s)

    def ctx_load(self, dst: Reg, word: int, indirect: bool) -> None:
        if indirect:
            self.op1(g("MOV"), (R1, 0), (MEM1(CTX), 8 * word))
            self.op1(g("MOV"), (dst.id, 0), (MEM1(R1), 0))
        else:
            self.op1(g("MOV"), (dst.id, 0), (MEM1(CTX), 8 * word))

    def ctx_store(self, word: int, src: Operand, indirect: bool) -> None:
        if indirect:
            self.op1(g("MOV"), (R1, 0), (MEM1(CTX), 8 * word))
            self.op1(g("MOV"), (MEM1(R1), 0), _o(src, True))
        else:
            self.op1(g("MOV"), (MEM1(CTX), 8 * word), _o(src, True))

    def ctx_addr(self, dst: Reg, word: int) -> None:
        self.op2(g("ADD"), (dst.id, 0), (CTX, 0), (IMM, 8 * word))

    def _fminmax(self, op: str, d: int, x: Operand, y: Operand, w: bool) -> None:
        """wasm min/max: NaN if either is NaN (x + y: a quiet NaN); equal operands differ at
        most in the sign bit (or: min, and: max); otherwise the smaller/larger one."""
        fl = 0 if w else B32
        self._fld(FR0, x, w)
        self._fld(FR1, y, w)
        nan, eq, keep, done = Label(), Label(), Label(), Label()
        self._fcmpj("UNORDERED", FR0, FR1, w, nan)
        self._fcmpj("ORDERED_EQUAL", FR0, FR1, w, eq)
        self._fcmpj("ORDERED_LESS" if op == "min" else "ORDERED_GREATER", FR0, FR1, w, keep)
        lib.sljit_emit_fop1(self.c, g("MOV_F64") | fl, FR0, 0, FR1, 0)
        self.bind(keep)
        self._fst(d, FR0, w)
        self.jump(done)
        self.bind(nan)
        lib.sljit_emit_fop2(self.c, g("ADD_F64") | fl, FR0, 0, FR0, 0, FR1, 0)
        self._fst(d, FR0, w)
        self.jump(done)
        self.bind(eq)
        self._fst(R1, FR0, w)
        self._fst(R2, FR1, w)
        self.op2(g("OR" if op == "min" else "AND"), (d, 0), (R1, 0), (R2, 0))
        self.bind(done)

    # ------------------------------------------------------------ tables and bulk memory
    def _element(self, f: Frame, word: int, index: Operand) -> tuple[int, int]:
        """Bounds-check index (R1) against the table's size; the element via R2 = base."""
        self.op1(g("MOV_U32"), (R1, 0), _o(index, True) if not isinstance(index, Imm) else (IMM, index.value & M32))
        self.cmpj(g("GREATER_EQUAL"), (R1, 0), (MEM1(CTX), 8 * word + 8), self._stub(f, TrapCode.TABLE_OUT_OF_BOUNDS))
        self.op1(g("MOV"), (R2, 0), (MEM1(CTX), 8 * word))
        return MEM2(R2, R1), 3

    def table_get(self, f: Frame, dst: Reg, word: int, index: Operand) -> None:
        self.op1(g("MOV"), (dst.id, 0), self._element(f, word, index))

    def table_set(self, f: Frame, word: int, index: Operand, value: Operand) -> None:
        self.op1(g("MOV"), self._element(f, word, index), _o(value, True))

    def _three(self, x: Operand, y: Operand, z: Operand, wy: bool) -> None:
        """R0 = x, R2 = y, R1 = z (i32 zero-extended; y 64-bit if `wy`); R0, which may hold an
        operand, last."""
        self.move(Reg(R2), y, wy)
        self.move(Reg(R1), z, False)
        self.move(Reg(R0), x, False)
        if isinstance(x, Reg) and x.id == R0:
            self.op1(g("MOV_U32"), (R0, 0), (R0, 0))

    def _end_check(self, f: Frame, r: int, limit: tuple[int, int], code: int) -> None:
        """r + R1 (both < 2**32) must not exceed `limit`."""
        self.op2(g("ADD"), (r, 0), (r, 0), (R1, 0))
        self.cmpj(g("GREATER"), (r, 0), limit, self._stub(f, code))
        self.op2(g("SUB"), (r, 0), (r, 0), (R1, 0))

    def _cmem(self, fn: str, args: int, a0: int, a1: int, a2: int) -> None:
        """A C memory function on R0, R1, R2 given in any registers (moved through the frame)."""
        for i, r in enumerate((a0, a1, a2)):
            self.op1(g("MOV"), (MEM1(SP), CTXSAVE + 8 + 8 * i), (r, 0))  # temporaries below CSAVE
        for i, r in enumerate((R0, R1, R2)):
            self.op1(g("MOV"), (r, 0), (MEM1(SP), CTXSAVE + 8 + 8 * i))
        self._ccall(ctypes.cast(getattr(_LIBC, fn), ctypes.c_void_p).value, g(args), -1)

    def memory_fill(self, f: Frame, d: Operand, v: Operand, n: Operand, mem: int) -> None:
        self._three(d, v, n, False)
        self._end_check(f, R0, (MEM1(CTX), 8 * mem + 8), TrapCode.MEMORY_OUT_OF_BOUNDS)
        self.op2(g("ADD"), (R0, 0), (R0, 0), (MEM1(CTX), 8 * mem))
        self._cmem("memset", "ARGS3V(P, 32, W)", R0, R2, R1)

    def memory_copy(self, f: Frame, d: Operand, s: Operand, n: Operand, dmem: int, smem: int) -> None:
        oob = TrapCode.MEMORY_OUT_OF_BOUNDS
        self._three(d, s, n, False)
        self._end_check(f, R0, (MEM1(CTX), 8 * dmem + 8), oob)
        self._end_check(f, R2, (MEM1(CTX), 8 * smem + 8), oob)
        self.op2(g("ADD"), (R0, 0), (R0, 0), (MEM1(CTX), 8 * dmem))
        self.op2(g("ADD"), (R2, 0), (R2, 0), (MEM1(CTX), 8 * smem))
        self._cmem("memmove", "ARGS3V(P, P, W)", R0, R2, R1)

    def table_fill(self, f: Frame, word: int, i: Operand, v: Operand, n: Operand) -> None:
        self._three(i, v, n, True)
        self._end_check(f, R0, (MEM1(CTX), 8 * word + 8), TrapCode.TABLE_OUT_OF_BOUNDS)
        self.op2(g("MSHL"), (R0, 0), (R0, 0), (IMM, 3))
        self.op2(g("ADD"), (R0, 0), (R0, 0), (MEM1(CTX), 8 * word))
        loop, done = Label(), Label()
        self.bind(loop)
        self.cmpj(g("EQUAL"), (R1, 0), (IMM, 0), done)
        self.op1(g("MOV"), (MEM1(R0), 0), (R2, 0))
        self.op2(g("ADD"), (R0, 0), (R0, 0), (IMM, 8))
        self.op2(g("SUB"), (R1, 0), (R1, 0), (IMM, 1))
        self.jump(loop)
        self.bind(done)

    def table_copy(self, f: Frame, dst: int, src: int, d: Operand, s: Operand, n: Operand) -> None:
        oob = TrapCode.TABLE_OUT_OF_BOUNDS
        self._three(d, s, n, False)
        self._end_check(f, R0, (MEM1(CTX), 8 * dst + 8), oob)
        self._end_check(f, R2, (MEM1(CTX), 8 * src + 8), oob)
        for r, word in ((R0, dst), (R2, src), (R1, None)):
            self.op2(g("MSHL"), (r, 0), (r, 0), (IMM, 3))
            if word is not None:
                self.op2(g("ADD"), (r, 0), (r, 0), (MEM1(CTX), 8 * word))
        self._cmem("memmove", "ARGS3V(P, P, W)", R0, R2, R1)

    def wide(self, op: str, dst: Slot, args: tuple[Operand, ...]) -> None:
        """S0008. The operands are first copied to the native frame (they may sit in tos, in the
        result slots or be imm64), so the carry pair is ADD/SUB with SLJIT_SET_CARRY then ADDC/SUBC
        with register and memory operands only: nothing between them can touch the flags. The
        multiplies are sljit's LMUL (R1:R0 = R0 * R1)."""
        t = [CSAVE + 8 * i for i in range(len(args))]
        for off, x in zip(t, args):
            self.op1(g("MOV"), (MEM1(SP), off), _o(x, True))
        if op in ("add128", "sub128"):
            lo, hi = ("ADD", "ADDC") if op == "add128" else ("SUB", "SUBC")
            self.op1(g("MOV"), (R1, 0), (MEM1(SP), t[0]))
            self.op1(g("MOV"), (R2, 0), (MEM1(SP), t[1]))
            self.op2(g(lo) | g("SET_CARRY"), (R1, 0), (R1, 0), (MEM1(SP), t[2]))
            self.op2(g(hi), (R2, 0), (R2, 0), (MEM1(SP), t[3]))
            self.op1(g("MOV"), _o(dst, True), (R1, 0))
            self.op1(g("MOV"), _o(Slot(dst.k + 1), True), (R2, 0))
            return
        self.op1(g("MOV"), (R0, 0), (MEM1(SP), t[0]))
        self.op1(g("MOV"), (R1, 0), (MEM1(SP), t[1]))
        lib.sljit_emit_op0(self.c, g("LMUL_SW" if op == "mul_wide_s" else "LMUL_UW"))
        self.op1(g("MOV"), _o(dst, True), (R0, 0))
        self.op1(g("MOV"), _o(Slot(dst.k + 1), True), (R1, 0))

    def simd(self, *a: Any) -> None:
        raise NotImplementedError

    def move_v128(self, *a: Any) -> None:
        raise NotImplementedError

    # ------------------------------------------------------------ calls
    def _status(self, f: Frame) -> None:
        self.cmpj(g("NOT_EQUAL") | B32, (R0, 0), (IMM, 0), f.unwind)

    def call_direct(self, f: Frame, label: Label, shift_slots: int) -> None:
        if shift_slots:
            self.op2(g("ADD"), (FRM, 0), (FRM, 0), (IMM, 8 * shift_slots))
        self.link(lib.sljit_emit_call(self.c, g("CALL_REG_ARG"), g("ARGS0(32)")), label)
        if shift_slots:
            self.op2(g("SUB"), (FRM, 0), (FRM, 0), (IMM, 8 * shift_slots))
        self._status(f)

    def _call_record(self, f: Frame, shift_slots: int) -> None:
        """Call the record in R1 on its own context (saved in the native frame)."""
        if shift_slots:
            self.op2(g("ADD"), (FRM, 0), (FRM, 0), (IMM, 8 * shift_slots))
        self.op1(g("MOV"), (MEM1(SP), CTXSAVE), (CTX, 0))
        self.op1(g("MOV"), (CTX, 0), (MEM1(R1), 16))
        self.op1(g("MOV"), (MB, 0), (MEM1(CTX), 8 * MEM0))
        lib.sljit_emit_icall(self.c, g("CALL_REG_ARG"), g("ARGS0(32)"), MEM1(R1), 0)
        self.op1(g("MOV"), (CTX, 0), (MEM1(SP), CTXSAVE))
        self.op1(g("MOV"), (MB, 0), (MEM1(CTX), 8 * MEM0))
        if shift_slots:
            self.op2(g("SUB"), (FRM, 0), (FRM, 0), (IMM, 8 * shift_slots))
        self._status(f)

    def call_record(self, f: Frame, word: int, shift_slots: int) -> None:
        self.op2(g("ADD"), (R1, 0), (CTX, 0), (IMM, 8 * word))
        self._call_record(f, shift_slots)

    def _table_record(self, f: Frame, table: int, sig: int, index: Operand) -> None:
        self.op1(g("MOV_U32"), (R2, 0), _o(index, True) if not isinstance(index, Imm) else (IMM, index.value & M32))
        self.cmpj(g("GREATER_EQUAL"), (R2, 0), (MEM1(CTX), 8 * table + 8), self._stub(f, TrapCode.TABLE_OUT_OF_BOUNDS))
        self.op1(g("MOV"), (R1, 0), (MEM1(CTX), 8 * table))
        self.op1(g("MOV"), (R1, 0), (MEM2(R1, R2), 3))
        self.cmpj(g("EQUAL"), (R1, 0), (IMM, 0), self._stub(f, TrapCode.INDIRECT_CALL_TO_NULL))
        self.op1(g("MOV"), (R2, 0), (MEM1(CTX), 8 * sig))
        self.cmpj(g("NOT_EQUAL"), (R2, 0), (MEM1(R1), 8), self._stub(f, TrapCode.BAD_SIGNATURE))

    def call_indirect(self, f: Frame, table: int, sig: int, index: Operand, shift_slots: int) -> None:
        self._table_record(f, table, sig, index)
        self._call_record(f, shift_slots)

    def call_ref(self, f: Frame, ref: Operand, shift_slots: int) -> None:
        self.move(Reg(R1), ref, True)
        self.cmpj(g("EQUAL"), (R1, 0), (IMM, 0), self._stub(f, TrapCode.NULL_REFERENCE))
        self._call_record(f, shift_slots)

    def null_check(self, f: Frame, x: Operand, code: int) -> None:
        self.move(Reg(R1), x, True)
        self.cmpj(g("EQUAL"), (R1, 0), (IMM, 0), self._stub(f, code))

    def call_host(self, f: Frame, word: int, slot: int) -> None:
        self.op1(g("MOV"), (R0, 0), (IMM, word))
        self.op2(g("ADD"), (R1, 0), (FRM, 0), (IMM, 8 * slot))
        lib.sljit_emit_icall(self.c, g("CALL_REG_ARG"), g("ARGS2(32, W, W)"), MEM1(CTX), 8 * HOST_BRIDGE)
        self.op1(g("MOV"), (MB, 0), (MEM1(CTX), 8 * MEM0))
        self._status(f)

    # tail calls: the arguments move down to the frame base, the R pins come back, and a sljit
    # call with SLJIT_CALL_RETURN tears the frame down (S pins, stack) and jumps: the callee
    # returns to our caller, both stacks stay constant
    def _tail(self, f: Frame, src: int, n: int) -> None:
        for j in range(n):
            if src:
                self.op1(g("MOV"), (R2, 0), (MEM1(FRM), 8 * (src + j)))
                self.op1(g("MOV"), (MEM1(FRM), 8 * j), (R2, 0))
        for i, r in enumerate(f.rpins):
            self.op1(g("MOV"), (r, 0), (MEM1(SP), 8 * i))

    def tail_direct(self, f: Frame, label: Label, src: int, n: int) -> None:
        self._tail(f, src, n)
        self.link(lib.sljit_emit_call(self.c, g("CALL_REG_ARG") | CALL_RETURN, g("ARGS0(32)")), label)

    def _tail_record(self, f: Frame, src: int, n: int) -> None:
        """The record in R1: on our context a real tail call; on another one (rare: a call
        across instances) an ordinary call whose results land at our frame base, then return
        (correct, but that chain's native stack grows, unlike the x86-64 tail_switch)."""
        other = Label()
        self._tail(f, src, n)
        self.cmpj(g("NOT_EQUAL"), (MEM1(R1), 16), (CTX, 0), other)
        lib.sljit_emit_icall(self.c, g("CALL_REG_ARG") | CALL_RETURN, g("ARGS0(32)"), MEM1(R1), 0)
        self.bind(other)
        self._call_record(f, 0)
        self.ret_ok(f)

    def tail_indirect(self, f: Frame, table: int, sig: int, index: Operand, src: int, n: int) -> None:
        self._table_record(f, table, sig, index)
        self._tail_record(f, src, n)

    def tail_ref(self, f: Frame, ref: Operand, src: int, n: int) -> None:
        self.move(Reg(R1), ref, True)
        self.cmpj(g("EQUAL"), (R1, 0), (IMM, 0), self._stub(f, TrapCode.NULL_REFERENCE))
        self._tail_record(f, src, n)

    def tail_record(self, f: Frame, word: int, src: int, n: int) -> None:
        self.op2(g("ADD"), (R1, 0), (CTX, 0), (IMM, 8 * word))
        self._tail_record(f, src, n)

    def host_thunk(self, word: int) -> int:
        at = self.mark()
        lib.sljit_emit_enter(self.c, REG_ENTER, g("ARGS0(32)"), 3, 3, 0)
        self.op1(g("MOV"), (R0, 0), (IMM, word))
        self.op1(g("MOV"), (R1, 0), (FRM, 0))
        lib.sljit_emit_icall(self.c, g("CALL_REG_ARG"), g("ARGS2(32, W, W)"), MEM1(CTX), 8 * HOST_BRIDGE)
        self.op1(g("MOV"), (MB, 0), (MEM1(CTX), 8 * MEM0))
        lib.sljit_emit_return(self.c, g("MOV32"), R0, 0)
        return at


_LIBM = ctypes.CDLL("libm.so.6")
_LIBC = ctypes.CDLL(None)


def assemble() -> tuple[bytes, list[int]]:
    """entry, bridge, sp stub, tail switch (never reached: no tail calls here)."""
    e = Emitter()
    c = e.c
    offs = []
    # int32_t entry(uint64_t *ctx, uint64_t *frame, const void *code): S0, S1, S2 = the arguments
    offs.append(e.mark())
    lib.sljit_emit_enter(c, 0, g("ARGS3(32, P, P, P)"), 3, 3, 0)
    e.op1(g("MOV"), (R2, 0), (S2, 0))
    e.op1(g("MOV"), (MB, 0), (MEM1(CTX), 8 * MEM0))
    lib.sljit_emit_icall(c, g("CALL_REG_ARG"), g("ARGS0(32)"), R2, 0)
    done = Label()
    e.cmpj(g("EQUAL") | B32, (R0, 0), (IMM, 0), done)
    e.op1(g("MOV_U32"), (R1, 0), (R0, 0))
    e.op1(g("MOV"), (MEM1(CTX), 8 * TRAP_CODE), (R1, 0))
    e.bind(done)
    lib.sljit_emit_return(c, g("MOV32"), R0, 0)
    # bridge: R0 = context word of the host function, R1 = its slots
    offs.append(e.mark())
    lib.sljit_emit_enter(c, REG_ENTER, g("ARGS2(32, W_R, W_R)"), 7, 4, 8 * 6)
    for i, r in enumerate((R3, R4, R5, R6)):
        e.op1(g("MOV"), (MEM1(SP), 8 * i), (r, 0))
    e.op1(g("MOV"), (R2, 0), (MEM1(CTX), 8 * HOST_ERROR))
    e.op1(g("MOV"), (MEM1(SP), 32), (R2, 0))
    e.op1(g("MOV"), (MEM1(CTX), 8 * HOST_ERROR), (IMM, STATUS_HOST_ERROR))
    e.op1(g("MOV"), (S3, 0), (MEM2(CTX, R0), 3))
    e.op1(g("MOV"), (R0, 0), (CTX, 0))
    lib.sljit_emit_icall(c, g("CALL"), g("ARGS2V(P, P)"), S3, 0)
    e.op1(g("MOV_U32"), (R0, 0), (MEM1(CTX), 8 * HOST_ERROR))
    e.op1(g("MOV"), (R1, 0), (MEM1(SP), 32))
    e.op1(g("MOV"), (MEM1(CTX), 8 * HOST_ERROR), (R1, 0))
    for i, r in enumerate((R3, R4, R5, R6)):
        e.op1(g("MOV"), (r, 0), (MEM1(SP), 8 * i))
    lib.sljit_emit_return(c, g("MOV32"), R0, 0)
    # sp stub: frame[0] = the native stack pointer as a wasm function sees it
    offs.append(e.mark())
    lib.sljit_emit_enter(c, REG_ENTER, g("ARGS0(32)"), 2, 3, 8 * NLOCAL)
    lib.sljit_get_local_base(c, R1, 0, 0)
    e.op1(g("MOV"), (MEM1(FRM), 0), (R1, 0))
    lib.sljit_emit_return(c, g("MOV32"), IMM, 0)
    # tail switch: never reached
    offs.append(e.mark())
    lib.sljit_emit_enter(c, REG_ENTER, g("ARGS0(32)"), 2, 3, 0)
    lib.sljit_emit_return(c, g("MOV32"), IMM, int(TrapCode.UNREACHABLE))
    code, size = e.generate()
    data = ctypes.string_at(code, size)
    offsets = [lib.w2s_label_addr(m) - code for m in e.marks]
    e.free(code)
    return data, offsets


_TRAMP = None


class JitSljitTarget(JitX86_64Target):
    name = "jit-sljit"

    def backend(self) -> Any:
        return Backend()

    def guard_backend(self) -> Any:
        return Backend(guard=True)

    def trampolines(self) -> Any:
        global _TRAMP
        if _TRAMP is None:
            from waboros.targets.jit.common.instance import load_trampolines

            _TRAMP = load_trampolines(*assemble())
        return _TRAMP

    def compile(self, module: Any) -> object:
        b = self.guard_backend() if _guard.wanted(self) else self.backend()
        c = compile_module(module, b, self.name)
        c.funcs = [(b.offset[p], n) for p, n in c.funcs]
        c.thunks = {i: b.offset[k] for i, k in c.thunks.items()}
        if b.guard:
            c.flags, c.faults = _guard.FLAGS, b.faults
        return c


base.TARGETS["jit-sljit"] = JitSljitTarget
