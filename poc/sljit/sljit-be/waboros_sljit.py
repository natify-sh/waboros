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

x86-64 and aarch64 (relies on 32-bit sljit ops zero-extending, as both do; a riscv64 port would
add MOV_U32 after them).

2026-11-16 (S0013 branch `gc` at e3bb361): safepoints (D56) and SIMD.
  * Site records: a sljit label right after every call is its return address; `ret_at` (from the
    stack pointer at the call to this frame's own return address) is sljit's frame size after
    `sljit_emit_enter`, read by `w2s_ret_at` in lib2.c from sljit's internal fields (a small
    sljit API should replace it); `up` = ret_at + 8. The bridge writes the store's exit record
    {caller's sp at its call, return address, frame} and clears its sp after the host returns; the
    entry keeps the enclosing record and its frame at [sp, sp + 32) during its call
    (Trampolines.saved = 0) and puts the record back.
  * SIMD: waboros's own x86-64 SSE2 lowering (x86_64/simd.py) run into a side buffer with
    sljit's physical registers and inserted with `sljit_emit_op_custom`; bounds checks and fault
    points stay sljit code (the side buffer is flushed before them).
  * The D39 arguments added since 2026-11-04 (`a64`, `lo`, S0009) are accepted and refused when
    set; `Frame.error` (S0012) is the status target of every call.
2026-11-16, aarch64 (under QEMU, `-cpu cortex-a72`): the same backend on the host's sljit (`a64/`,
cross-built). What differs per ISA is small and lives here: the frame's `ret_at`/`up` (lib2.c:
sljit's aarch64 prologue saves x30 at sp + 8 for frames up to 512 bytes), SIMD through
aarch64/simd.py in 4-byte custom words (a constant address becomes an index register: its forms
take only the offsets its own bounds give), the frame-size constant before the first jump of the
prologue (sljit asserts on a constant right after a jump), and in guard mode a constant address
and a stored immediate go to registers first, so that the fault point's label is on the one
instruction that faults (sljit would otherwise put an `add` or a `mov` before it).
2026-11-16 (branch `gc` at bf55c98: S0013 step 6 done, step 7 in progress): every D39 method.
  * Exceptions: since D63 the core lowers `throw` and the clauses on the heap; the backend has
    `landing` and `throw_ref` only (and claims EXN).
  * memory64 / table64 (S0009): `a64` and `lo` taken by every method; an i64 index is checked as
    common/lower.py's `_i64` (no guard for it), the bulk end checks carry out of 64 bits.
  * A constant-stack tail call across contexts: the `tail_switch` trampoline of D30 (16-byte
    restore frame, the walk's `restore`), the one piece written as bytes per ISA (it reads its
    own return address); the caller reaches it by a sljit tail call with the record in R0.
  * No process address in the code: nearest/ceil/floor/trunc on sljit's API (the hand-written
    x86-64 SSE2 sequence, D24), sqrt one custom instruction per ISA, memory.fill/copy and
    table.copy as inline 8-byte loops. The native frame shrank from 12 to 9 words.
Not covered: BTI landing pads on aarch64 (sljit emits none; the runtime maps code PROT_BTI where
the CPU has BTI), a sljit API for `ret_at` (read from sljit's internal fields).
"""

import ctypes
import json
import os
import platform
import struct
from collections.abc import Sequence
from typing import Any

from waboros.targets import base
from waboros.targets.base import TrapCode
from waboros.targets.jit.common import guard as _guard
from waboros.targets.jit.common.backend import NEGATE, SWAP, AddK, Dst, Imm, Operand, Reg, Slot
from waboros.targets.jit.common import gc as _gc
from waboros.targets.jit.aarch64 import encoder as _enc_a64
from waboros.targets.jit.aarch64 import simd as _v128_a64
from waboros.targets.jit.x86_64 import encoder as _enc
from waboros.targets.jit.x86_64 import simd as _v128
from waboros.targets.jit.common.compiler import EXN, SUPPORTED, TABLES, WIDEOPS, compile_module
from waboros.targets.jit.common.context import (
    EXCEPTION,
    EXIT_RECORD,
    HOST_BRIDGE,
    HOST_ERROR,
    INTERRUPT,
    NSTACK_LIMIT,
    STATUS_EXCEPTION,
    STATUS_HOST_ERROR,
    STATUS_INTERRUPT,
    TAIL_SWITCH,
    TRAP_CODE,
    VSTACK_LIMIT,
    Layout,
)
from waboros.targets.jit.common.guard import GUARD
from waboros.targets.jit.aarch64 import JitAarch64Target
from waboros.targets.jit.x86_64 import JitX86_64Target

HERE = os.path.dirname(os.path.abspath(__file__))
ARCH = platform.machine().lower()  # x86_64 or aarch64 (sljit built for the host: lib2.c, consts2.c)
LIBDIR = os.path.join(HERE, "a64") if ARCH in ("aarch64", "arm64") else HERE
K = json.load(open(os.path.join(LIBDIR, "consts2.json")))
lib = ctypes.CDLL(os.path.join(LIBDIR, os.environ.get("W2S_LIB", "libsljit2.so")))
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
    ("w2s_ret_at", SW, [P]),
    ("w2s_up", SW, [P]),
    ("w2s_locals_offset", SW, []),
    ("sljit_get_register_index", I32, [I32, I32]),
    ("sljit_emit_op_custom", I32, [P, P, ctypes.c_uint32]),
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
NLOCAL = 9  # native frame words: R pins (4), saved context (1), scratch (4: `wide`, `_copy`)
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
        self.error = self.unwind  # a call's non-zero status (S0012: a landing pad inside try_table)
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
    supported = SUPPORTED | set(WIDEOPS) | _gc.OPS | _v128.SUPPORTED | EXN  # SIMD by custom bytes per ISA

    def __init__(self, *, guard: bool = False) -> None:
        super().__init__()
        self.guard = guard
        self.consts: list[tuple[int, int]] = []  # (sljit const, value) patched after generation
        self.offset: list[int] = []  # mark -> code offset, by finish
        self.faults: list[tuple[int, int]] = []
        self._faults: list[tuple[int, Label]] = []
        self._oob: Label | None = None
        self.f: Frame | None = None
        self.sites: list[tuple[int, int, int]] = []  # (mark, ret_at, up); marks -> offsets in finish
        self._ret_at = self._up = 0

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
        self._ret_at, self._up = lib.w2s_ret_at(c), lib.w2s_up(c)
        for i, r in enumerate(f.rpins):
            self.op1(g("MOV"), (MEM1(SP), 8 * i), (r, 0))
        # the frame-size constant first: on aarch64 sljit refuses a constant at the address right
        # after a jump (an assertion in sljit_generate_code)
        f.vconst = lib.sljit_emit_const(c, g("MOV"), R2, 0, 1 << 20)  # 8 * frame_slots, patched
        lib.sljit_get_local_base(c, R1, 0, 0)
        self.cmpj(g("LESS"), (R1, 0), (MEM1(CTX), 8 * NSTACK_LIMIT), self._stub(f, TrapCode.STACK_OVERFLOW))
        self.op2(g("ADD"), (R2, 0), (R2, 0), (FRM, 0))
        self.cmpj(g("GREATER"), (R2, 0), (MEM1(CTX), 8 * VSTACK_LIMIT), self._stub(f, TrapCode.STACK_OVERFLOW))
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
        self.sites[:] = [(self.offset[m], ra, up) for m, ra, up in self.sites]
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
        self._fld(FR0, x, wide)
        if op == "sqrt":  # sljit has no square root: one instruction per ISA, as custom bytes
            fr = lib.sljit_get_register_index(1, FR0)  # SLJIT_FLOAT_REGISTER
            if ARCH == "x86_64":
                a = _enc.Asm()
                a.sse("sqrt", fr, fr, wide)
            else:
                a = _enc_a64.Asm()
                a.fp1("sqrt", fr, fr, wide)
            self._flush(a)
            self._fst(dst.id, FR0, wide)
            return
        self._round(op, dst.id, wide)

    def _round(self, op: str, d: int, w: bool) -> None:
        """ceil/floor/trunc/nearest of FR0 on sljit's API, the hand-written x86-64 backend's SSE2
        sequence (D24) on every ISA: |x| >= 2**52 (2**23 for f32), inf and NaN are integral
        already: x + 0.0 (quiets a NaN). Otherwise trunc converts to an integer and back;
        nearest adds and subtracts 2**52 on |x| (rounds to even: the default rounding mode);
        the sign of x is put back (so -0.5 -> -0.0); floor/ceil step trunc by 1 toward
        -inf/+inf. R1 = the bits of x, R2 the result; d (tos or a pool register) is written last."""
        fl = 0 if w else B32
        sign = 1 << (63 if w else 31)
        big_bits = 0x4330000000000000 if w else 0x4B000000
        self._fst(R1, FR0, w)
        self.op2(_w("AND", w), (R2, 0), (R1, 0), (IMM, s64(sign - 1) if w else s32(sign - 1)))
        big, done = Label(), Label()
        if w:
            self.op1(g("MOV"), (d, 0), (IMM, s64(big_bits)))
            self.cmpj(g("GREATER_EQUAL"), (R2, 0), (d, 0), big)  # unsigned: NaN, inf above too
        else:
            self.cmpj(g("GREATER_EQUAL") | B32, (R2, 0), (IMM, big_bits), big)
        if op == "nearest":
            lib.sljit_emit_fcopy(self.c, g("COPY_TO_F64") | fl, FR1, R2)
            self.op1(g("MOV"), (R2, 0), (IMM, s64(big_bits)))  # 2**52 (2**23); R1 keeps x's bits
            lib.sljit_emit_fcopy(self.c, g("COPY_TO_F64") | fl, FR2, R2)
            lib.sljit_emit_fop2(self.c, g("ADD_F64") | fl, FR1, 0, FR1, 0, FR2, 0)
            lib.sljit_emit_fop2(self.c, g("SUB_F64") | fl, FR1, 0, FR1, 0, FR2, 0)
        else:
            lib.sljit_emit_fop1(self.c, g("CONV_SW_FROM_F64") | fl, R2, 0, FR0, 0)
            lib.sljit_emit_fop1(self.c, g("CONV_F64_FROM_SW") | fl, FR1, 0, R2, 0)
        self._fst(R2, FR1, w)
        self.op2(_w("AND", w), (R1, 0), (R1, 0), (IMM, s64(sign) if w else s32(sign)))
        self.op2(_w("OR", w), (R2, 0), (R2, 0), (R1, 0))
        if op in ("floor", "ceil"):
            lib.sljit_emit_fcopy(self.c, g("COPY_TO_F64") | fl, FR1, R2)
            self._fcmpj("ORDERED_LESS_EQUAL" if op == "floor" else "ORDERED_GREATER_EQUAL",
                        FR1, FR0, w, done)  # fmt: skip
            self._fconst(FR2, 1.0, w)
            lib.sljit_emit_fop2(self.c, g("SUB_F64" if op == "floor" else "ADD_F64") | fl, FR1, 0, FR1, 0, FR2, 0)
            self._fst(R2, FR1, w)
        self.jump(done)
        self.bind(big)
        self._fconst(FR1, 0.0, w)
        lib.sljit_emit_fop2(self.c, g("ADD_F64") | fl, FR0, 0, FR0, 0, FR1, 0)
        self._fst(R2, FR0, w)
        self.bind(done)
        self.op1(g("MOV" if w else "MOV_U32"), (d, 0), (R2, 0))

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

    def _access(
        self, f: Frame, addr: Operand | AddK, offset: int, size: int, mem: int, a64: bool = False,
        lo: int = 0,
    ) -> tuple[int, int]:  # fmt: skip
        """The memory operand of an access; bounds checked (explicit) or a fault point armed
        (guard). R1 holds the index; R2 is free afterwards. An i64 memory (S0009, `a64`): the
        index is 64-bit and always checked explicitly, as common/lower.py's `_i64`."""
        limit = (MEM1(CTX), 8 * mem + 8)
        oob = self._stub(f, TrapCode.MEMORY_OUT_OF_BOUNDS)
        if a64 and not isinstance(addr, Imm):
            self.op1(g("MOV"), (R1, 0), _o(addr, True))
            if offset + size > M64:  # no index is in bounds (dead code follows, well-formed)
                self.jump(oob)
                offset = 0
            k = offset + size
            if k <= lo:  # the size L >= lo >= k: L - k cannot wrap; idx > L - k traps
                self.op1(g("MOV"), (R2, 0), limit)
                self.op2(g("SUB"), (R2, 0), (R2, 0), (IMM, s64(k)))
                self.cmpj(g("GREATER"), (R1, 0), (R2, 0), oob)
            else:  # idx + k carrying out of 64 bits traps, then idx + k > L
                self.op1(g("MOV"), (R2, 0), (IMM, s64(k)))  # a register: an immediate add may
                self.op2(g("ADD") | g("SET_CARRY"), (R2, 0), (R1, 0), (R2, 0))  # become a sub
                self.link(lib.sljit_emit_jump(self.c, g("CARRY")), oob)
                self.cmpj(g("GREATER"), (R2, 0), limit, oob)
            if offset:
                self.op1(g("MOV"), (R2, 0), (IMM, s64(offset)))
                self.op2(g("ADD"), (R1, 0), (R1, 0), (R2, 0))
            return self._base(R1, mem)
        guarded = self.guard and not a64 and (
            (addr.value & M32) + offset + size <= (1 << 32) + GUARD if isinstance(addr, Imm)
            else offset + size <= GUARD
        )  # fmt: skip
        if isinstance(addr, Imm):
            ea = (addr.value & (M64 if a64 else M32)) + offset
            if ea + size > M64:  # ends past 2**64: no memory holds it
                self.jump(oob)
                ea = 0
            if not guarded:
                end = ea + size
                if end < 1 << 31:
                    self.cmpj(g("LESS"), limit, (IMM, end), oob)
                else:  # unsigned: through a register (sljit may turn a negative immediate around)
                    self.op1(g("MOV"), (R2, 0), (IMM, s64(end)))
                    self.cmpj(g("LESS"), limit, (R2, 0), oob)
            if mem == MEM0 and ea < 1 << 31 and not (guarded and ARCH != "x86_64"):
                res = (MEM1(MB), ea)
            elif mem == MEM0:  # aarch64, guard: [base, index] is one instruction, so the fault point's
                self.op1(g("MOV"), (R1, 0), (IMM, s64(ea)))  # label is on the faulting one (an offset
                res = MEM2(MB, R1), 0  # the immediate form cannot take becomes an `add` before it)
            else:
                self.op1(g("MOV"), (R1, 0), (IMM, s64(ea)))
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
        offset: int, mem: int, *, a64: bool = False, lo: int = 0,
    ) -> None:  # fmt: skip
        m = self._access(f, addr, offset, size, mem, a64, lo)
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
        self, f: Frame, size: int, addr: Operand | AddK, offset: int, src: Operand, mem: int, *,
        a64: bool = False, lo: int = 0,
    ) -> None:  # fmt: skip
        m = self._access(f, addr, offset, size, mem, a64, lo)
        s = _o(src, True)
        if isinstance(src, Slot) or (isinstance(src, Imm) and (ARCH != "x86_64" or not -(1 << 31) <= s[1] < 1 << 31)):
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

    def func_type(self, dst: Reg, rec: Operand) -> None:
        """dst = the canonical type id of the function record `rec` (its word 1; S0013 5.6)."""
        if not isinstance(rec, Reg):
            self.op1(g("MOV"), (dst.id, 0), _o(rec, True))
            rec = dst
        self.op1(g("MOV"), (dst.id, 0), (MEM1(rec.id), 8))

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
    def _index(self, r: int, index: Operand, a64: bool) -> None:
        """r = a table index: 64-bit for a table64, else zero-extended (copied even from a register)."""
        if isinstance(index, Imm):
            self.op1(g("MOV"), (r, 0), (IMM, s64(index.value & (M64 if a64 else M32))))
        else:
            self.op1(g("MOV" if a64 else "MOV_U32"), (r, 0), _o(index, True))

    def _element(self, f: Frame, word: int, index: Operand, a64: bool = False) -> tuple[int, int]:
        """Bounds-check index (R1) against the table's size; the element via R2 = base."""
        self._index(R1, index, a64)
        self.cmpj(g("GREATER_EQUAL"), (R1, 0), (MEM1(CTX), 8 * word + 8), self._stub(f, TrapCode.TABLE_OUT_OF_BOUNDS))
        self.op1(g("MOV"), (R2, 0), (MEM1(CTX), 8 * word))
        return MEM2(R2, R1), 3

    def table_get(self, f: Frame, dst: Reg, word: int, index: Operand, *, a64: bool = False) -> None:
        self.op1(g("MOV"), (dst.id, 0), self._element(f, word, index, a64))

    def table_set(
        self, f: Frame, word: int, index: Operand, value: Operand, *, a64: bool = False
    ) -> None:  # fmt: skip
        self.op1(g("MOV"), self._element(f, word, index, a64), _o(value, True))

    def _three(self, x: Operand, y: Operand, z: Operand, wx: bool, wy: bool, wz: bool) -> None:
        """R0 = x, R2 = y, R1 = z, each 64-bit if its flag says so, else zero-extended; R0, which
        may hold an operand, last."""
        self.move(Reg(R2), y, wy)
        self.move(Reg(R1), z, wz)
        self.move(Reg(R0), x, wx)
        if isinstance(x, Reg) and x.id == R0 and not wx:
            self.op1(g("MOV_U32"), (R0, 0), (R0, 0))

    def _end_check(self, f: Frame, r: int, limit: tuple[int, int], code: int, wide: bool = False) -> None:
        """r + R1 must not exceed `limit`. Not wide, both are below 2**32 and the sum cannot
        wrap; wide (S0009), a carry out of 64 bits is out of bounds."""
        stub = self._stub(f, code)
        if wide:
            self.op2(g("ADD") | g("SET_CARRY"), (r, 0), (r, 0), (R1, 0))
            self.link(lib.sljit_emit_jump(self.c, g("CARRY")), stub)
        else:
            self.op2(g("ADD"), (r, 0), (r, 0), (R1, 0))
        self.cmpj(g("GREATER"), (r, 0), limit, stub)
        self.op2(g("SUB"), (r, 0), (r, 0), (R1, 0))

    def _fill(self) -> None:
        """R1 bytes at [R0] = the byte of R2: 8 at a time, then single bytes (as the hand-written
        backends; no C library, no process address in the code)."""
        self.op2(g("AND"), (R2, 0), (R2, 0), (IMM, 0xFF))
        self.op2(g("MUL"), (R2, 0), (R2, 0), (IMM, 0x0101010101010101))
        f8, f1, done = Label(), Label(), Label()
        self.bind(f8)
        self.cmpj(g("LESS"), (R1, 0), (IMM, 8), f1)
        self.op1(g("MOV"), (MEM1(R0), 0), (R2, 0))
        self.op2(g("ADD"), (R0, 0), (R0, 0), (IMM, 8))
        self.op2(g("SUB"), (R1, 0), (R1, 0), (IMM, 8))
        self.jump(f8)
        self.bind(f1)
        self.cmpj(g("EQUAL"), (R1, 0), (IMM, 0), done)
        self.op1(g("MOV_U8"), (MEM1(R0), 0), (R2, 0))
        self.op2(g("ADD"), (R0, 0), (R0, 0), (IMM, 1))
        self.op2(g("SUB"), (R1, 0), (R1, 0), (IMM, 1))
        self.jump(f1)
        self.bind(done)

    def _copy(self, unit: int) -> None:
        """R1 bytes from [R2] to [R0] as memmove does: backwards when the destination is above
        the source; 8 bytes at a time, then single bytes (`unit` 1). R3 is the temporary, saved in
        the native frame (it may hold a pinned local)."""
        t = R3
        self.op1(g("MOV"), (MEM1(SP), CSAVE), (t, 0))
        fwd, f1, b8, b1, done = (Label() for _ in range(5))
        self.cmpj(g("LESS_EQUAL"), (R0, 0), (R2, 0), fwd)
        self.bind(b8)
        self.cmpj(g("LESS"), (R1, 0), (IMM, 8), b1 if unit == 1 else done)
        self.op2(g("SUB"), (R1, 0), (R1, 0), (IMM, 8))
        self.op1(g("MOV"), (t, 0), (MEM2(R2, R1), 0))
        self.op1(g("MOV"), (MEM2(R0, R1), 0), (t, 0))
        self.jump(b8)
        if unit == 1:
            self.bind(b1)
            self.cmpj(g("EQUAL"), (R1, 0), (IMM, 0), done)
            self.op2(g("SUB"), (R1, 0), (R1, 0), (IMM, 1))
            self.op1(g("MOV_U8"), (t, 0), (MEM2(R2, R1), 0))
            self.op1(g("MOV_U8"), (MEM2(R0, R1), 0), (t, 0))
            self.jump(b1)
        self.bind(fwd)
        self.cmpj(g("LESS"), (R1, 0), (IMM, 8), f1 if unit == 1 else done)
        self.op1(g("MOV"), (t, 0), (MEM1(R2), 0))
        self.op1(g("MOV"), (MEM1(R0), 0), (t, 0))
        self.op2(g("ADD"), (R0, 0), (R0, 0), (IMM, 8))
        self.op2(g("ADD"), (R2, 0), (R2, 0), (IMM, 8))
        self.op2(g("SUB"), (R1, 0), (R1, 0), (IMM, 8))
        self.jump(fwd)
        if unit == 1:
            self.bind(f1)
            self.cmpj(g("EQUAL"), (R1, 0), (IMM, 0), done)
            self.op1(g("MOV_U8"), (t, 0), (MEM1(R2), 0))
            self.op1(g("MOV_U8"), (MEM1(R0), 0), (t, 0))
            self.op2(g("ADD"), (R0, 0), (R0, 0), (IMM, 1))
            self.op2(g("ADD"), (R2, 0), (R2, 0), (IMM, 1))
            self.op2(g("SUB"), (R1, 0), (R1, 0), (IMM, 1))
            self.jump(f1)
        self.bind(done)
        self.op1(g("MOV"), (t, 0), (MEM1(SP), CSAVE))

    def memory_fill(
        self, f: Frame, d: Operand, v: Operand, n: Operand, mem: int, *, a64: bool = False
    ) -> None:  # fmt: skip
        self._three(d, v, n, a64, False, a64)
        self._end_check(f, R0, (MEM1(CTX), 8 * mem + 8), TrapCode.MEMORY_OUT_OF_BOUNDS, a64)
        self.op2(g("ADD"), (R0, 0), (R0, 0), (MEM1(CTX), 8 * mem))
        self._fill()

    def memory_copy(
        self, f: Frame, d: Operand, s: Operand, n: Operand, dmem: int, smem: int, *,
        a64: tuple[bool, bool] = (False, False),
    ) -> None:  # fmt: skip
        oob = TrapCode.MEMORY_OUT_OF_BOUNDS
        ad, as_ = a64
        self._three(d, s, n, ad, as_, ad and as_)
        self._end_check(f, R0, (MEM1(CTX), 8 * dmem + 8), oob, ad)
        self._end_check(f, R2, (MEM1(CTX), 8 * smem + 8), oob, as_)
        self.op2(g("ADD"), (R0, 0), (R0, 0), (MEM1(CTX), 8 * dmem))
        self.op2(g("ADD"), (R2, 0), (R2, 0), (MEM1(CTX), 8 * smem))
        self._copy(1)

    def table_fill(
        self, f: Frame, word: int, i: Operand, v: Operand, n: Operand, *, a64: bool = False
    ) -> None:  # fmt: skip
        self._three(i, v, n, a64, True, a64)
        self._end_check(f, R0, (MEM1(CTX), 8 * word + 8), TrapCode.TABLE_OUT_OF_BOUNDS, a64)
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

    def table_copy(
        self, f: Frame, dst: int, src: int, d: Operand, s: Operand, n: Operand, *,
        a64: tuple[bool, bool] = (False, False),
    ) -> None:  # fmt: skip
        oob = TrapCode.TABLE_OUT_OF_BOUNDS
        ad, as_ = a64
        self._three(d, s, n, ad, as_, ad and as_)
        self._end_check(f, R0, (MEM1(CTX), 8 * dst + 8), oob, ad)
        self._end_check(f, R2, (MEM1(CTX), 8 * src + 8), oob, as_)
        for r, word in ((R0, dst), (R2, src), (R1, None)):
            self.op2(g("MSHL"), (r, 0), (r, 0), (IMM, 3))
            if word is not None:
                self.op2(g("ADD"), (r, 0), (r, 0), (MEM1(CTX), 8 * word))
        self._copy(8)

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

    # SIMD (experiment): waboros's own x86-64 SSE2 lowering (x86_64/simd.py) run into a side buffer
    # with sljit's physical registers (frame, context; rcx/rdx -> sljit's scratch R1/R2), its bytes
    # inserted by sljit_emit_op_custom. Bounds checks and fault points stay sljit code: the side
    # buffer is flushed before them. xmm0-xmm5 are scratch inside one operation, as in simd.py.
    def _phys(self, r: int) -> int:
        return lib.sljit_get_register_index(0, r)  # SLJIT_GP_REGISTER

    def _flush(self, a: Any) -> None:
        code = a.finish()
        step = 15 if ARCH == "x86_64" else 4  # sljit's argument check: x86 1..15 bytes, aarch64 one word
        for i in range(0, len(code), step):
            piece = code[i : i + step]
            chunk = (ctypes.c_uint32 * 4)()  # 4-byte aligned, as aarch64's check wants
            ctypes.memmove(chunk, piece, len(piece))
            lib.sljit_emit_op_custom(self.c, chunk, len(piece))
        a.buf, a._labels, a._fixups = bytearray(), [], []

    def _custom(self, run: Any) -> None:
        a = _enc.Asm()
        ph = self._phys
        me = self

        def bounds(f: Frame, addr: Any, offset: int, size: int, mem: int, a64: bool, lo: int) -> Any:
            me._flush(a)
            if isinstance(addr, Reg):
                addr = Reg(me._unphys[addr.id])
            elif isinstance(addr, AddK):
                addr = AddK(me._unphys[addr.id], addr.k)
            op, disp = me._access(f, addr, offset, size, mem, a64, lo)
            base, idx = op & 0x7F, (op >> 8) & 0x7F
            return _enc.Mem(ph(base), disp, ph(idx) if idx else None, 1)

        def fault_point() -> None:
            me._flush(a)
            me._fault_point()

        def move(dst: Reg, src: Any, wide: bool) -> None:  # dst: rcx/rdx as patched (physical)
            if isinstance(src, Imm):
                a.mov_imm(dst.id, src.value & (M64 if wide else M32), w=wide)
            elif isinstance(src, Reg):
                a.mov(dst.id, src.id, w=wide)
            else:
                a.mov(dst.id, _enc.Mem(ph(FRM), 8 * src.k), w=wide)

        class PX:
            pass

        px = PX()
        px.a, px._bounds, px._fault_point, px.move = a, bounds, fault_point, move
        saved = {n: getattr(_v128, n) for n in ("R12", "R15", "RCX", "RDX")}
        _v128.R12, _v128.R15, _v128.RCX, _v128.RDX = ph(FRM), ph(CTX), ph(R1), ph(R2)
        try:
            run(px, a)
        finally:
            for n, v in saved.items():
                setattr(_v128, n, v)
        self._flush(a)

    def _custom_a64(self, run: Any) -> None:
        """aarch64: waboros's NEON lowering (aarch64/simd.py) with sljit's physical registers for
        the context and the frame; its scratch x9-x13, x17 hold nothing of ours (sljit's temps are
        x9, x10; R3-R6 are x3-x6; x11-x13 and x17 are sljit registers this backend never uses)."""
        a = _enc_a64.Asm()
        ph = self._phys
        me = self
        frame = ph(FRM)

        def bounds(f: Frame, addr: Any, offset: int, size: int, mem: int, a64: bool, lo: int) -> Any:
            me._flush(a)
            if isinstance(addr, Reg):
                addr = Reg(me._unphys[addr.id])
            elif isinstance(addr, AddK):
                addr = AddK(me._unphys[addr.id], addr.k)
            op, disp = me._access(f, addr, offset, size, mem, a64, lo)
            base, idx = op & 0x7F, (op >> 8) & 0x7F
            if not idx and disp:  # simd.py's forms take [base, #imm] only in the ranges its own bounds
                me.op1(g("MOV"), (R1, 0), (IMM, disp))  # give: a constant address goes to the index
                idx, disp = R1, 0
            return ph(base), (ph(idx) if idx else None), disp

        def fault_point() -> None:
            me._flush(a)
            me._fault_point()

        def word(load: bool, t: int, base: int, off: int, size: int = 8) -> None:
            if off // size < 1 << 12:
                (a.ld(t, base, off, size) if load else a.st(t, base, off, size))
            else:
                a.mov_imm(17, off)
                (a.ld_idx(t, base, 17, size) if load else a.st_idx(t, base, 17, size))

        def r(o: Any, scratch: int, wide: bool, zr: bool = True) -> int:
            if isinstance(o, Reg):
                return o.id
            if isinstance(o, Slot):
                word(True, scratch, frame, 8 * o.k, 8 if wide else 4)
                return scratch
            v = o.value & (M64 if wide else M32)
            if v == 0 and zr:
                return _enc_a64.ZR
            a.mov_imm(scratch, v, wide)
            return scratch

        class PX:
            pass

        px = PX()
        px.a, px._bounds, px._fault_point, px._word, px._r = a, bounds, fault_point, word, r
        saved = {n: getattr(_v128_a64, n) for n in ("CTX", "FRAME")}
        _v128_a64.CTX, _v128_a64.FRAME = ph(CTX), frame
        try:
            run(px, a)
        finally:
            for n, v in saved.items():
                setattr(_v128_a64, n, v)
        self._flush(a)

    def _mapped(self, o: Any) -> Any:
        if isinstance(o, Reg):
            p = self._phys(o.id)
            self._unphys[p] = o.id
            return Reg(p)
        if isinstance(o, AddK):
            p = self._phys(o.id)
            self._unphys[p] = o.id
            return AddK(p, o.k)
        return o

    def simd(self, f: Frame, op: int, dst: Any, args: tuple[Any, ...], imm: tuple[int, ...],
             *, a64: bool = False, lo: int = 0) -> None:  # fmt: skip
        self._unphys: dict[int, int] = {}
        d, xs = self._mapped(dst), tuple(self._mapped(x) for x in args)
        if ARCH == "x86_64":
            self._custom(lambda px, a: _v128.emit(px, f, op, d, xs, imm, a64, lo))
        else:
            self._custom_a64(lambda px, a: _v128_a64.emit(px, f, op, d, xs, imm, a64, lo))

    def move_v128(self, dst: Slot, src: Any) -> None:
        if ARCH == "x86_64":
            self._custom(lambda px, a: _v128.move(a, dst, src))
        else:
            self._custom_a64(lambda px, a: _v128_a64.move(a, dst, src))

    # ------------------------------------------------------------ calls
    def _site(self) -> None:
        """The call just emitted is a safepoint (D56): a label at its return address."""
        self.sites.append((self.mark(), self._ret_at, self._up))

    def _status(self, f: Frame) -> None:
        self.cmpj(g("NOT_EQUAL") | B32, (R0, 0), (IMM, 0), f.error)

    def call_direct(self, f: Frame, label: Label, shift_slots: int) -> None:
        if shift_slots:
            self.op2(g("ADD"), (FRM, 0), (FRM, 0), (IMM, 8 * shift_slots))
        self.link(lib.sljit_emit_call(self.c, g("CALL_REG_ARG"), g("ARGS0(32)")), label)
        self._site()
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
        self._site()
        self.op1(g("MOV"), (CTX, 0), (MEM1(SP), CTXSAVE))
        self.op1(g("MOV"), (MB, 0), (MEM1(CTX), 8 * MEM0))
        if shift_slots:
            self.op2(g("SUB"), (FRM, 0), (FRM, 0), (IMM, 8 * shift_slots))
        self._status(f)

    def call_record(self, f: Frame, word: int, shift_slots: int) -> None:
        self.op2(g("ADD"), (R1, 0), (CTX, 0), (IMM, 8 * word))
        self._call_record(f, shift_slots)

    def _table_record(self, f: Frame, table: int, sig: int, index: Operand, a64: bool = False) -> None:
        self._index(R2, index, a64)
        self.cmpj(g("GREATER_EQUAL"), (R2, 0), (MEM1(CTX), 8 * table + 8), self._stub(f, TrapCode.TABLE_OUT_OF_BOUNDS))
        self.op1(g("MOV"), (R1, 0), (MEM1(CTX), 8 * table))
        self.op1(g("MOV"), (R1, 0), (MEM2(R1, R2), 3))
        self.cmpj(g("EQUAL"), (R1, 0), (IMM, 0), self._stub(f, TrapCode.INDIRECT_CALL_TO_NULL))
        self.op1(g("MOV"), (R2, 0), (MEM1(CTX), 8 * sig))
        self.cmpj(g("NOT_EQUAL"), (R2, 0), (MEM1(R1), 8), self._stub(f, TrapCode.BAD_SIGNATURE))

    def call_indirect(
        self, f: Frame, table: int, sig: int, index: Operand, shift_slots: int, *, a64: bool = False
    ) -> None:  # fmt: skip
        self._table_record(f, table, sig, index, a64)
        self._call_record(f, shift_slots)

    def call_ref(self, f: Frame, ref: Operand, shift_slots: int) -> None:
        self.move(Reg(R1), ref, True)
        self.cmpj(g("EQUAL"), (R1, 0), (IMM, 0), self._stub(f, TrapCode.NULL_REFERENCE))
        self._call_record(f, shift_slots)

    # exceptions (S0012; since S0013 step 6, D63, `throw` and the clauses are lowered in the core
    # on the heap: only the pad and throw_ref are the backend's)
    def landing(self, f: Frame, pad: Label, outer: Label) -> None:
        self.bind(pad)
        self.cmpj(g("NOT_EQUAL") | B32, (R0, 0), (IMM, STATUS_EXCEPTION), outer)

    def throw_ref(self, f: Frame, x: Operand) -> None:
        """Null traps (NULL_REFERENCE, not catchable); else the object becomes the one in flight."""
        self.move(Reg(R1), x, True)  # x may be in R0
        self.cmpj(g("EQUAL"), (R1, 0), (IMM, 0), self._stub(f, TrapCode.NULL_REFERENCE))
        self.op1(g("MOV"), (R2, 0), (MEM1(CTX), 8 * EXCEPTION))
        self.op1(g("MOV"), (MEM1(R2), 0), (R1, 0))
        self.op1(g("MOV32"), (R0, 0), (IMM, STATUS_EXCEPTION))
        self.jump(f.error)

    def null_check(self, f: Frame, x: Operand, code: int) -> None:
        self.move(Reg(R1), x, True)
        self.cmpj(g("EQUAL"), (R1, 0), (IMM, 0), self._stub(f, code))

    def call_host(self, f: Frame, word: int, slot: int) -> None:
        self.op1(g("MOV"), (R0, 0), (IMM, word))
        self.op2(g("ADD"), (R1, 0), (FRM, 0), (IMM, 8 * slot))
        lib.sljit_emit_icall(self.c, g("CALL_REG_ARG"), g("ARGS2(32, W, W)"), MEM1(CTX), 8 * HOST_BRIDGE)
        self._site()
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
        """The record in R1: on our context a tail call straight to its code; on another one a
        tail call into the tail_switch trampoline (D30, `tail_switch` below) with the record in
        R0, our frame torn down as for any tail call, so the caller's return address is where
        the trampoline looks for it. Both stacks stay constant along a chain across contexts."""
        other = Label()
        self._tail(f, src, n)
        self.cmpj(g("NOT_EQUAL"), (MEM1(R1), 16), (CTX, 0), other)
        lib.sljit_emit_icall(self.c, g("CALL_REG_ARG") | CALL_RETURN, g("ARGS0(32)"), MEM1(R1), 0)
        self.bind(other)
        self.op1(g("MOV"), (R0, 0), (R1, 0))
        lib.sljit_emit_icall(self.c, g("CALL_REG_ARG") | CALL_RETURN, g("ARGS1(32, W)"), MEM1(CTX), 8 * TAIL_SWITCH)

    def tail_indirect(
        self, f: Frame, table: int, sig: int, index: Operand, src: int, n: int, *, a64: bool = False
    ) -> None:  # fmt: skip
        self._table_record(f, table, sig, index, a64)
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
        self._ret_at, self._up = lib.w2s_ret_at(self.c), lib.w2s_up(self.c)
        self.op1(g("MOV"), (R0, 0), (IMM, word))
        self.op1(g("MOV"), (R1, 0), (FRM, 0))
        lib.sljit_emit_icall(self.c, g("CALL_REG_ARG"), g("ARGS2(32, W, W)"), MEM1(CTX), 8 * HOST_BRIDGE)
        self._site()
        self.op1(g("MOV"), (MB, 0), (MEM1(CTX), 8 * MEM0))
        lib.sljit_emit_return(self.c, g("MOV32"), R0, 0)
        return at




def assemble() -> tuple[bytes, list[int]]:
    """entry, bridge, sp stub, tail switch (never reached: no tail calls here)."""
    e = Emitter()
    c = e.c
    offs = []
    # int32_t entry(uint64_t *ctx, uint64_t *frame, const void *code): S0, S1, S2 = the arguments
    offs.append(e.mark())
    lib.sljit_emit_enter(c, 0, g("ARGS3(32, P, P, P)"), 3, 3, 32)
    e.op1(g("MOV"), (R2, 0), (MEM1(CTX), 8 * EXIT_RECORD))
    for i in range(3):  # the enclosing host call's exit record {sp, pc, frame}, then our frame (D56)
        e.op1(g("MOV"), (R1, 0), (MEM1(R2), 8 * i))
        e.op1(g("MOV"), (MEM1(SP), 8 * i), (R1, 0))
    e.op1(g("MOV"), (MEM1(SP), 24), (FRM, 0))
    e.op1(g("MOV"), (R2, 0), (S2, 0))
    e.op1(g("MOV"), (MB, 0), (MEM1(CTX), 8 * MEM0))
    lib.sljit_emit_icall(c, g("CALL_REG_ARG"), g("ARGS0(32)"), R2, 0)
    entry_ret = e.mark()
    e.op1(g("MOV"), (R2, 0), (MEM1(CTX), 8 * EXIT_RECORD))
    for i in range(3):  # R0 (the status) untouched
        e.op1(g("MOV"), (R1, 0), (MEM1(SP), 8 * i))
        e.op1(g("MOV"), (MEM1(R2), 8 * i), (R1, 0))
    done = Label()
    e.cmpj(g("EQUAL") | B32, (R0, 0), (IMM, 0), done)
    e.op1(g("MOV_U32"), (R1, 0), (R0, 0))
    e.op1(g("MOV"), (MEM1(CTX), 8 * TRAP_CODE), (R1, 0))
    e.bind(done)
    lib.sljit_emit_return(c, g("MOV32"), R0, 0)
    # bridge: R0 = context word of the host function, R1 = its slots
    offs.append(e.mark())
    lib.sljit_emit_enter(c, REG_ENTER, g("ARGS2(32, W_R, W_R)"), 7, 4, 8 * 6)
    bra, bup, loc = lib.w2s_ret_at(c), lib.w2s_up(c), lib.w2s_locals_offset()
    for i, r in enumerate((R3, R4, R5, R6)):
        e.op1(g("MOV"), (MEM1(SP), 8 * i), (r, 0))
    e.op1(g("MOV"), (R2, 0), (MEM1(CTX), 8 * EXIT_RECORD))  # {caller's sp at its call, pc, frame}
    lib.sljit_get_local_base(c, S3, 0, bra - loc)  # sljit forbids sp-relative access past the locals
    e.op1(g("MOV"), (MEM1(R2), 8), (MEM1(S3), 0))  # the return address (x86-64: pushed; aarch64: x30 saved)
    e.op2(g("ADD"), (S3, 0), (S3, 0), (IMM, bup - bra))  # the caller's stack pointer at its call
    e.op1(g("MOV"), (MEM1(R2), 0), (S3, 0))
    e.op1(g("MOV"), (MEM1(R2), 16), (FRM, 0))
    e.op1(g("MOV"), (R2, 0), (MEM1(CTX), 8 * HOST_ERROR))
    e.op1(g("MOV"), (MEM1(SP), 32), (R2, 0))
    e.op1(g("MOV"), (MEM1(CTX), 8 * HOST_ERROR), (IMM, STATUS_HOST_ERROR))
    e.op1(g("MOV"), (S3, 0), (MEM2(CTX, R0), 3))
    e.op1(g("MOV"), (R0, 0), (CTX, 0))
    lib.sljit_emit_icall(c, g("CALL"), g("ARGS2V(P, P)"), S3, 0)
    e.op1(g("MOV"), (R1, 0), (MEM1(CTX), 8 * EXIT_RECORD))  # no wasm frame waits in a host call
    e.op1(g("MOV"), (MEM1(R1), 0), (IMM, 0))
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
    code, size = e.generate()
    data = ctypes.string_at(code, size)
    offsets = [lib.w2s_label_addr(m) - code for m in e.marks]
    e.free(code)
    data += bytes(-len(data) % 16)
    blob, restore = tail_switch()
    # the walk's table (S0013 3a): the entry's return point and tail_switch's `restore`
    return data + blob, [*(offsets[k] for k in offs), len(data), offsets[entry_ret], len(data) + restore]


def tail_switch() -> tuple[bytes, int]:
    """The cross-context tail-call trampoline (D30, ARTIFACT.md part 3 "Tail calls"), the one
    piece of this backend not written on sljit's API: it must read its own return address. It
    is entered by a sljit tail call (frame torn down, R0 = the record, the caller's return
    address where a call put it: [sp] on x86-64, the link register on aarch64). The first
    switch of a chain leaves the 16-byte restore frame {context, real return address} and calls
    the target, whose return address is `restore`; a later switch whose return address is
    already `restore` only changes the context and memory 0's base and jumps. Only R0-R2 (the
    record, scratch) are touched: R3-R6 and S3-S5 are the caller's caller's again. Returns the
    bytes and the offset of `restore`."""
    ph = lambda r: lib.sljit_get_register_index(0, r)  # noqa: E731
    rec, t, ctx, mb = ph(R0), ph(R1), ph(CTX), ph(MB)
    if ARCH == "x86_64":
        Mem = _enc.Mem
        a = _enc.Asm()
        restore, hop = a.label(), a.label()
        a.endbr64()
        a.lea_label(t, restore)
        a.alu("cmp", t, Mem(_enc.RSP), w=True)
        a.jcc("e", hop)
        a.push(ctx)  # rsp = 0 mod 16: the callee sees 8 mod 16, as after any call
        a.mov(ctx, Mem(rec, 16), w=True)
        a.mov(mb, Mem(ctx, 8 * MEM0), w=True)
        a.call(Mem(rec))
        a.bind(restore)  # the status stays in R0
        a.pop(ctx)
        a.mov(mb, Mem(ctx, 8 * MEM0), w=True)
        a.ret()
        a.bind(hop)
        a.mov(ctx, Mem(rec, 16), w=True)
        a.mov(mb, Mem(ctx, 8 * MEM0), w=True)
        a.jmp(Mem(rec))
    else:
        A = _enc_a64
        a = A.Asm()
        restore, hop = a.label(), a.label()
        a.adr(t, restore)
        a.cmp(A.LR, t)
        a.bcc("eq", hop)
        a.pair(False, ctx, A.LR, A.SP, -16, "pre")
        a.ld(ctx, rec, 16)
        a.ld(mb, ctx, 8 * MEM0)
        a.ld(t, rec)
        a.blr(t)
        a.bind(restore)  # the status stays in R0
        a.pair(True, ctx, A.LR, A.SP, 16, "post")
        a.ld(mb, ctx, 8 * MEM0)
        a.ret()
        a.bind(hop)
        a.ld(ctx, rec, 16)
        a.ld(mb, ctx, 8 * MEM0)
        a.ld(t, rec)
        a.br(t)
    at = a._labels[restore.id]  # both encoders keep a label's offset here
    return a.finish(), at


_TRAMP = None


class JitSljitTarget(JitAarch64Target if ARCH in ("aarch64", "arm64") else JitX86_64Target):  # type: ignore[misc]
    name = "jit-sljit"

    def backend(self) -> Any:
        return Backend()

    def guard_backend(self) -> Any:
        return Backend(guard=True)

    def trampolines(self) -> Any:
        global _TRAMP
        if _TRAMP is None:
            from waboros.targets.jit.common.instance import load_trampolines

            _TRAMP = load_trampolines(*assemble(), saved=lib.w2s_locals_offset())
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
