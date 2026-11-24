# pyright: basic, reportArgumentType=false, reportAttributeAccessIssue=false
"""The D39 backend of `jit-sljit-x86_64` (docs/specs/S0016): our one-pass core
(common/compiler.py) with sljit as the encoder, through ctypes and `native/`'s library. Ported
from poc/sljit/sljit-be/waboros_sljit.py (its operators, bounds, floats, SIMD glue) and given
the structure `jit-x86_64` has since S0015.

Internal convention (sljit's SLJIT_CALL_REG_ARG + SLJIT_ENTER_KEEP(3)):
  S0 context, S1 value-stack frame, S2 memory 0's base (kept: shared by caller and callee);
  R0 tos and the status; R1, R2 scratch inside one method;
  pool (pinnable, callee-saved inside this compiler): S3 S4 S5 (saved by sljit's enter) and
  R4 R5 R6 R3 (saved in the function's own frame: `local` = 8 * R pins, nothing else).
Memory 0's size is read from the context at every explicit check.

The module, as jit-x86_64's (S0015): per function and host thunk a 16-byte C-entry stub
(`endbr64; lea rdx, [rip + 5]` as custom bytes, then sljit's `jmp rel32` to centry), the body
right behind it; after the last body a 128-byte `int3` pad (sljit shortens a jump within
+-128 bytes) and the module's shared parts, all sljit functions:
  * centry, a C function `int32_t (ctx, frame, body)`: calls the body (a site, shift 0, no map)
    and takes the private HOP status (a body's return-then-jump) to a C sibling call;
  * xcall (a record of another context of this compiler: S0 saved in its 16-byte frame, the
    callee's S0/S2, its body called) and ccall (another compiler's record: R3-R6 saved in its
    frame, the C entry called with R0 = context, R1 = frame); each call is a site, shift 0, no
    map (S0016 "Frame size": no body frame holds the context or the C-path saves).
The bridge and tail_switch are this compiler's blob (`trampoline_blob`). Frame numbers
(`ret_at`, `up`) come from `waboros_sljit_frame`. Every sljit function starts with `endbr64`
(SLJIT_CONFIG_X86_CET); calls and returns pair up, and SLJIT_CALL_RETURN releases the frame
and jumps with the return address as found (CET shadow stacks).
"""

import ctypes
import struct
from collections.abc import Sequence
from functools import cache
from typing import Any

from tests.sljit import LIB
from waboros.targets.base import TrapCode, Unsupported
from waboros.targets.jit.common import gc as _gc
from waboros.targets.jit.common.backend import AddK, Dst, Imm, Operand, Reg, Slot
from waboros.targets.jit.common.compiler import EXN, SUPPORTED, WIDEOPS
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
    VSTACK_LIMIT,
    Layout,
)
from waboros.targets.jit.common.guard import GUARD
from waboros.targets.jit.x86_64 import encoder as _enc
from waboros.targets.jit.x86_64 import simd as _simd

assert LIB is not None, "tests.sljit registers the target only with its library"
lib = ctypes.CDLL(str(LIB))
P, I32, SW, UW = ctypes.c_void_p, ctypes.c_int32, ctypes.c_int64, ctypes.c_uint64
for _n, _r, _a in [
    ("waboros_sljit_create", P, []),
    ("waboros_sljit_frame", None, [P, ctypes.POINTER(SW), ctypes.POINTER(SW)]),
    ("waboros_sljit_constant", ctypes.c_int, [ctypes.c_char_p, ctypes.POINTER(SW)]),
    ("waboros_sljit_label_addr", UW, [P]),
    ("waboros_sljit_const_addr", UW, [P]),
    ("waboros_sljit_code_size", UW, [P]),
    ("waboros_sljit_error", I32, [P]),
    ("sljit_free_compiler", None, [P]),
    ("sljit_generate_code", P, [P, I32, P]),
    ("sljit_free_code", None, [P, P]),
    ("sljit_emit_enter", I32, [P, I32, I32, I32, I32, I32]),
    ("sljit_emit_return", I32, [P, I32, I32, SW]),
    ("sljit_emit_op0", I32, [P, I32]),
    ("sljit_emit_op1", I32, [P, I32, I32, SW, I32, SW]),
    ("sljit_emit_op2", I32, [P, I32, I32, SW, I32, SW, I32, SW]),
    ("sljit_emit_op2u", I32, [P, I32, I32, SW, I32, SW]),
    ("sljit_emit_op_dst", I32, [P, I32, I32, SW]),
    ("sljit_emit_op_flags", I32, [P, I32, I32, SW, I32]),
    ("sljit_emit_op_addr", P, [P, I32, I32, SW]),
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
    ("sljit_get_register_index", I32, [I32, I32]),
    ("sljit_emit_op_custom", I32, [P, P, ctypes.c_uint32]),
]:
    _f = getattr(lib, _n)
    _f.restype, _f.argtypes = _r, _a


@cache
def g(n: str) -> int:
    """sljit's constant SLJIT_<n> from the library (`waboros_sljit_constant`); `ADD32` etc. is
    the word operation | SLJIT_32."""
    v = SW()
    if lib.waboros_sljit_constant(f"SLJIT_{n}".encode(), ctypes.byref(v)) == 0:
        return v.value
    if n.endswith("32"):
        return g(n[:-2]) | g("32")
    raise KeyError(f"SLJIT_{n}")


R0, R1, R2, R3, R4, R5, R6 = (g(f"R{i}") for i in range(7))
S0, S1, S2, S3, S4, S5 = (g(f"S{i}") for i in range(6))
FR0, FR1, FR2 = g("FR0"), g("FR1"), g("FR2")
SP, IMM, MEM, B32 = g("SP"), g("IMM"), g("MEM"), g("32")
CTX, FRM, MB = S0, S1, S2
POOL = (S3, S4, S5, R4, R5, R6, R3)  # R3 is rcx on x86-64: variable shifts want it, so last
SPOOL = (S3, S4, S5)
RSAVE = (R3, R4, R5, R6)  # what a C entry may clobber of the pool (rcx r8 r11 r10)
MEM0 = Layout().memory(0)  # context word of memory 0's base
M32, M64 = 0xFFFFFFFF, (1 << 64) - 1
CALL_RETURN = g("CALL_RETURN")
REG_ENTER = g("ENTER_REG_ARG") | 3  # SLJIT_ENTER_KEEP(3): S0..S2 shared
SCRATCHES = 7 | (3 << 8)  # R0-R6 and SLJIT_ENTER_FLOAT(3)
HOP = 0x80000001  # private status (S0015, bit 31): a body returns it to its module's centry
STUB = 16  # endbr64 (4), lea rdx, [rip + 5] (7), jmp rel32 (5)
STUB_HEAD = b"\xf3\x0f\x1e\xfa" + b"\x48\x8d\x15" + (5).to_bytes(4, "little")
HEADROOM = 768  # S0015: the most a chain of frames may allocate before the next stack check
LOCALS_OFFSET = 0  # SLJIT_LOCALS_OFFSET on x86-64 SysV (sljitConfigInternal.h)


def _private_simd() -> Any:
    """A private copy of x86_64/simd.py (the package's module is never mutated: a jit-x86_64
    compile may run in another thread) whose frame, context and two scratch registers are
    sljit's physical ones: R12 = S1, R15 = S0, rcx/rdx = R1/R2; xmm0-xmm5 are scratch inside
    one operation, as in simd.py."""
    import importlib.util
    import sys

    name = f"{__name__}._simd_x86_64"
    spec = importlib.util.spec_from_file_location(name, _simd.__file__)
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m  # before running it: its dataclasses look their module up
    spec.loader.exec_module(m)
    ph = lambda r: lib.sljit_get_register_index(0, r)  # noqa: E731  (SLJIT_GP_REGISTER)
    m.R12, m.R15, m.RCX, m.RDX = ph(FRM), ph(CTX), ph(R1), ph(R2)
    return m


_v128 = _private_simd()


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
         "gt": "ORDERED_GREATER", "le": "ORDERED_LESS_EQUAL",
         "ge": "ORDERED_GREATER_EQUAL"}  # fmt: skip
BIN = {"add": "ADD", "sub": "SUB", "mul": "MUL", "and": "AND", "or": "OR", "xor": "XOR",
       "shl": "MSHL", "shr_s": "MASHR", "shr_u": "MLSHR", "rotl": "ROTL",
       "rotr": "ROTR"}  # fmt: skip


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
    """A sljit compiler (made by `waboros_sljit_create`: the baseline CPU word) and the
    label/jump bookkeeping the backend and the trampolines share."""

    def __init__(self) -> None:
        self._c: int | None = None  # made at the first emission: `supports` builds a Backend
        self.marks: list[int] = []  # sljit labels whose offsets the caller wants back
        self.ret_at = self.up = 0  # the current function's frame numbers

    @property
    def c(self) -> int:
        c = self._c
        if c is None:
            c = self._c = int(lib.waboros_sljit_create())
        return c

    def close(self) -> None:
        """Free the compiler (`finish` does; a compile that raises calls this)."""
        if self._c is not None:
            lib.sljit_free_compiler(self._c)
            self._c = None

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

    def call(self, label: Label, ret: bool = False) -> None:
        """A REG_ARG call (or, `ret`, a tail call) to a label of this compiler."""
        t = g("CALL_REG_ARG") | (CALL_RETURN if ret else 0)
        self.link(lib.sljit_emit_call(self.c, t, g("ARGS0(32)")), label)

    def addr(self, r: int, label: Label) -> None:
        """r = the address of `label` (rip-relative: no process address in the code)."""
        self.link(lib.sljit_emit_op_addr(self.c, g("MOV_ADDR"), r, 0), label)

    def mark(self) -> int:
        self.marks.append(lib.sljit_emit_label(self.c))
        return len(self.marks) - 1

    def enter(self, options: int, args: str, scratches: int, saveds: int, local: int) -> None:
        lib.sljit_emit_enter(self.c, options, g(args), scratches, saveds, local)
        ret_at, up = SW(), SW()
        lib.waboros_sljit_frame(self.c, ctypes.byref(ret_at), ctypes.byref(up))
        self.ret_at, self.up = ret_at.value, up.value

    def custom(self, code: bytes) -> None:
        buf = ctypes.create_string_buffer(code, len(code))
        lib.sljit_emit_op_custom(self.c, buf, len(code))

    def generate(self) -> tuple[int, int]:
        """(address of the generated buffer, size); the caller frees with `free`."""
        err = lib.waboros_sljit_error(self.c)
        if err:
            raise RuntimeError(f"sljit compiler error {err}")
        code = lib.sljit_generate_code(self.c, 0, None)
        if not code:
            raise RuntimeError(f"sljit_generate_code failed ({lib.waboros_sljit_error(self.c)})")
        return code, lib.waboros_sljit_code_size(self.c)

    def offsets(self, code: int) -> list[int]:
        return [lib.waboros_sljit_label_addr(m) - code for m in self.marks]

    def free(self, code: int) -> None:
        lib.sljit_free_code(code, None)
        self.close()


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
    record_sites = 3  # own context, this compiler's other contexts (xcall), ccall (D71)
    supported = SUPPORTED | set(WIDEOPS) | _gc.OPS | _v128.SUPPORTED | EXN

    def __init__(self, *, guard: bool = False) -> None:
        super().__init__()
        self.guard = guard
        self.consts: list[tuple[int, int]] = []  # (sljit const, value) patched after generation
        self.offset: list[int] = []  # mark -> code offset, by finish
        self.faults: list[tuple[int, int]] = []
        self._faults: list[tuple[int, Label]] = []
        self._oob: Label | None = None
        self.sites: list[tuple[int, int, int]] = []  # (mark, ret_at, up); offsets in finish
        self._stubs: list[tuple[int, int]] = []  # (stub mark, body mark) of every C entry
        self._centry, self._centry_ret = Label(), Label()
        self._xcall, self._ccall = Label(), Label()
        self._records = False  # a call through a record: the module needs xcall and ccall
        self._centry_at = -1  # centry's mark

    # ------------------------------------------------------------ labels, frames, traps
    def here(self) -> int:
        return self.mark()

    def func_begin(self, label: Label, pins: tuple[int, ...] = (), far: bool = False) -> Frame:
        """A body: REG_ARG|KEEP(3), the S pins saved by sljit, the R pins in its frame (the only
        locals, S0016). The frame is sljit's before the stack check: within the headroom with
        the most that can precede it (ccall's frame and a centry, S0015)."""
        self.bind(label)
        f = Frame(pins)
        ns = sum(r in SPOOL for r in pins)
        assert all(r in SPOOL for r in pins[:ns]), pins
        self.enter(REG_ENTER, "ARGS0(32)", SCRATCHES, 3 + ns, 8 * len(f.rpins))
        if self.up + 48 + 32 > HEADROOM:
            raise Unsupported("jit-sljit-x86_64", "a native frame above the headroom (S0015)")
        for i, r in enumerate(f.rpins):
            self.op1(g("MOV"), (MEM1(SP), 8 * i), (r, 0))
        f.vconst = lib.sljit_emit_const(self.c, g("MOV"), R2, 0, 1 << 20)  # 8 * frame_slots
        lib.sljit_get_local_base(self.c, R1, 0, 0)
        self.cmpj(
            g("LESS"),
            (R1, 0),
            (MEM1(CTX), 8 * NSTACK_LIMIT),
            self._stub(f, TrapCode.STACK_OVERFLOW),
        )
        self.op2(g("ADD"), (R2, 0), (R2, 0), (FRM, 0))
        self.cmpj(
            g("GREATER"),
            (R2, 0),
            (MEM1(CTX), 8 * VSTACK_LIMIT),
            self._stub(f, TrapCode.STACK_OVERFLOW),
        )
        self.poll(f)
        return f

    def func_end(self, f: Frame, frame_slots: int) -> bool:
        self.consts.append((f.vconst, 8 * frame_slots))
        for code, label in f.traps.items():
            self.bind(label)
            self.op1(g("MOV32"), (R0, 0), (IMM, code))
            self.jump(f.unwind)
        self.bind(f.unwind)
        self._rpins_back(f)
        lib.sljit_emit_return(self.c, g("MOV32"), R0, 0)
        return True

    def _rpins_back(self, f: Frame) -> None:
        for i, r in enumerate(f.rpins):
            self.op1(g("MOV"), (r, 0), (MEM1(SP), 8 * i))

    def rewind(self, pos: int) -> None:
        raise AssertionError("sljit branches always reach")

    def _stub(self, f: Frame, code: int) -> Label:
        if code not in f.traps:
            f.traps[code] = Label()
        return f.traps[code]

    def poll(self, f: Frame) -> None:
        self.cmpj(
            g("NOT_EQUAL"), (MEM1(CTX), 8 * INTERRUPT), (IMM, 0), self._stub(f, STATUS_INTERRUPT)
        )

    def trap(self, f: Frame, code: int) -> None:
        self.jump(self._stub(f, code))

    def ret_ok(self, f: Frame) -> None:
        self.op1(g("MOV32"), (R0, 0), (IMM, 0))
        self.jump(f.unwind)

    def finish(self) -> tuple[bytes, int]:
        """Generate, patch the frame-size constants, turn marks into offsets, and check every
        C-entry stub: its bytes, the long jump to centry, the body at stub + 16 (S0016)."""
        code, size = self.generate()
        for k, v in self.consts:
            lib.sljit_set_const(lib.waboros_sljit_const_addr(k), g("MOV"), v, 0)
        self.offset = self.offsets(code)
        self.sites[:] = [(self.offset[m], ra, up) for m, ra, up in self.sites]
        at = lib.waboros_sljit_label_addr
        self.faults = [(at(p) - code, at(s.lab) - code) for p, s in self._faults]
        data = ctypes.string_at(code, size)
        self.free(code)
        centry = self.offset[self._centry_at]
        for stub, body in self._stubs:
            s = self.offset[stub]
            rel = int.from_bytes(data[s + 12 : s + 16], "little", signed=True)
            if (data[s : s + 11] != STUB_HEAD or data[s + 11] != 0xE9 or s + STUB + rel != centry
                    or self.offset[body] != s + STUB):  # fmt: skip
                raise Unsupported("jit-sljit-x86_64", f"the C-entry stub at {s} is not 16 bytes")
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
            self.cmpj(
                _w("EQUAL", wide),
                (R2, 0),
                (IMM, 0),
                self._stub(f, TrapCode.INTEGER_DIVISION_BY_ZERO),
            )
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
                self.cmpj(
                    _w("EQUAL", wide), (R0, 0), (IMM, lo), self._stub(f, TrapCode.INTEGER_OVERFLOW)
                )
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
            a = _enc.Asm()
            a.sse("sqrt", fr, fr, wide)
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
            lib.sljit_emit_fop2(
                self.c, g("SUB_F64" if op == "floor" else "ADD_F64") | fl, FR1, 0, FR1, 0, FR2, 0
            )
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
        lib.sljit_emit_fop1(
            self.c, g("CMP_F64") | (0 if wide else B32) | setflag(t), FR0, 0, FR1, 0
        )
        lib.sljit_emit_op_flags(self.c, g("MOV"), dst.id, 0, t)

    def _fconst(self, fr: int, v: float, w: bool) -> None:
        bits = (
            struct.unpack("<Q", struct.pack("<d", v))[0]
            if w
            else struct.unpack("<I", struct.pack("<f", v))[0]
        )
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
            (
                _,
                src,
            ) = rest.split("_", 1)
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
        self, f: Frame, addr: Operand | AddK, offset: int, size: int, mem: int, a64: bool,
        lo: int,
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
            if mem == MEM0 and ea < 1 << 31:
                res = (MEM1(MB), ea)
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
        offset: int, mem: int, *, a64: bool, lo: int,
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
        a64: bool, lo: int = 0,
    ) -> None:  # fmt: skip
        m = self._access(f, addr, offset, size, mem, a64, lo)
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
        """r = a table index: 64-bit for a table64, else zero-extended (copied even from a
        register)."""
        if isinstance(index, Imm):
            self.op1(g("MOV"), (r, 0), (IMM, s64(index.value & (M64 if a64 else M32))))
        else:
            self.op1(g("MOV" if a64 else "MOV_U32"), (r, 0), _o(index, True))

    def _element(self, f: Frame, word: int, index: Operand, a64: bool) -> tuple[int, int]:
        """Bounds-check index (R1) against the table's size; the element via R2 = base."""
        self._index(R1, index, a64)
        self.cmpj(
            g("GREATER_EQUAL"),
            (R1, 0),
            (MEM1(CTX), 8 * word + 8),
            self._stub(f, TrapCode.TABLE_OUT_OF_BOUNDS),
        )
        self.op1(g("MOV"), (R2, 0), (MEM1(CTX), 8 * word))
        return MEM2(R2, R1), 3

    def table_get(self, f: Frame, dst: Reg, word: int, index: Operand, *, a64: bool) -> None:
        self.op1(g("MOV"), (dst.id, 0), self._element(f, word, index, a64))

    def table_set(
        self, f: Frame, word: int, index: Operand, value: Operand, *, a64: bool
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

    def _end_check(
        self, f: Frame, r: int, limit: tuple[int, int], code: int, wide: bool = False
    ) -> None:
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
        the source; 8 bytes at a time, then single bytes (`unit` 1). The temporary is S2 (memory
        0's base, never an operand), reloaded from the context after: no frame slot (S0016)."""
        t = MB
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
        self.op1(g("MOV"), (MB, 0), (MEM1(CTX), 8 * MEM0))

    def memory_fill(
        self, f: Frame, d: Operand, v: Operand, n: Operand, mem: int, *, a64: bool
    ) -> None:  # fmt: skip
        self._three(d, v, n, a64, False, a64)
        self._end_check(f, R0, (MEM1(CTX), 8 * mem + 8), TrapCode.MEMORY_OUT_OF_BOUNDS, a64)
        self.op2(g("ADD"), (R0, 0), (R0, 0), (MEM1(CTX), 8 * mem))
        self._fill()

    def memory_copy(
        self, f: Frame, d: Operand, s: Operand, n: Operand, dmem: int, smem: int, *,
        a64: tuple[bool, bool],
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
        self, f: Frame, word: int, i: Operand, v: Operand, n: Operand, *, a64: bool
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
        a64: tuple[bool, bool],
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
        """S0008. Every operand is read into a register before a result slot is written (they
        may sit in tos, in the result slots or be imm64): R1, R2, S2 (memory 0's base, never an
        operand, reloaded from the context after: no frame slot, S0016) and R0 last, which may
        hold an operand. The carry pair is ADD/SUB with SLJIT_SET_CARRY then ADDC/SUBC on
        registers: nothing between them can touch the flags. The multiplies are sljit's LMUL
        (R1:R0 = R0 * R1)."""
        if op in ("add128", "sub128"):
            for r, x in zip((R1, R2, MB, R0), args, strict=True):
                self.move(Reg(r), x, True)
            lo, hi = ("ADD", "ADDC") if op == "add128" else ("SUB", "SUBC")
            self.op2(g(lo) | g("SET_CARRY"), (R1, 0), (R1, 0), (MB, 0))
            self.op2(g(hi), (R2, 0), (R2, 0), (R0, 0))
            self.op1(g("MOV"), _o(dst, True), (R1, 0))
            self.op1(g("MOV"), _o(Slot(dst.k + 1), True), (R2, 0))
        else:
            self.move(Reg(MB), args[0], True)
            self.move(Reg(R1), args[1], True)
            self.op1(g("MOV"), (R0, 0), (MB, 0))
            lib.sljit_emit_op0(self.c, g("LMUL_SW" if op == "mul_wide_s" else "LMUL_UW"))
            self.op1(g("MOV"), _o(dst, True), (R0, 0))
            self.op1(g("MOV"), _o(Slot(dst.k + 1), True), (R1, 0))
        self.op1(g("MOV"), (MB, 0), (MEM1(CTX), 8 * MEM0))

    # SIMD: waboros's own x86-64 SSE2 lowering (x86_64/simd.py) run into a side buffer
    # with sljit's physical registers (frame, context; rcx/rdx -> sljit's scratch R1/R2), its bytes
    # inserted by sljit_emit_op_custom. Bounds checks and fault points stay sljit code: the side
    # buffer is flushed before them. xmm0-xmm5 are scratch inside one operation, as in simd.py.
    def _phys(self, r: int) -> int:
        return lib.sljit_get_register_index(0, r)  # SLJIT_GP_REGISTER

    def _flush(self, a: Any) -> None:
        code = a.finish()
        step = 15  # sljit's argument check: x86 1..15 bytes
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

        def bounds(
            f: Frame, addr: Any, offset: int, size: int, mem: int, a64: bool, lo: int
        ) -> Any:
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
        run(px, a)
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
             *, a64: bool, lo: int) -> None:  # fmt: skip
        self._unphys: dict[int, int] = {}
        d, xs = self._mapped(dst), tuple(self._mapped(x) for x in args)
        self._custom(lambda px, a: _v128.emit(px, f, op, d, xs, imm, a64, lo))

    def move_v128(self, dst: Slot, src: Any) -> None:
        self._custom(lambda px, a: _v128.move(a, dst, src))

    # ------------------------------------------------------------ calls
    def _site(self) -> None:
        """The call just emitted is a safepoint (D56): a label at its return address, with the
        current function's frame numbers (sljit pushes nothing around a call)."""
        self.sites.append((self.mark(), self.ret_at, self.up))

    def _status(self, f: Frame) -> None:
        self.cmpj(g("NOT_EQUAL") | B32, (R0, 0), (IMM, 0), f.error)

    def call_direct(self, f: Frame, label: Label, shift_slots: int) -> None:
        if shift_slots:
            self.op2(g("ADD"), (FRM, 0), (FRM, 0), (IMM, 8 * shift_slots))
        self.call(label)
        self._site()
        if shift_slots:
            self.op2(g("SUB"), (FRM, 0), (FRM, 0), (IMM, 8 * shift_slots))
        self._status(f)

    def _call_record(self, f: Frame, shift_slots: int) -> None:
        """Call the record in R1 on D71's three paths, three sites with one map (S0015, S0016):
        our own context: its body behind its 16-byte C entry, as a direct call; another context
        of this compiler (its HOST_BRIDGE word is ours): `xcall`; else `ccall`, the C entry.
        Each helper restores what it changed before it returns, so the status is tested with
        the pins, S0 and S2 back on every path (A3)."""
        self._records = True
        other, foreign, done = Label(), Label(), Label()
        if shift_slots:
            self.op2(g("ADD"), (FRM, 0), (FRM, 0), (IMM, 8 * shift_slots))
        self.cmpj(g("NOT_EQUAL"), (MEM1(R1), 16), (CTX, 0), other)
        self.op1(g("MOV"), (R1, 0), (MEM1(R1), 0))
        self.op2(g("ADD"), (R1, 0), (R1, 0), (IMM, STUB))
        lib.sljit_emit_icall(self.c, g("CALL_REG_ARG"), g("ARGS0(32)"), R1, 0)
        self._site()
        self.jump(done)
        self.bind(other)
        self.op1(g("MOV"), (R2, 0), (MEM1(R1), 16))
        self.op1(g("MOV"), (R2, 0), (MEM1(R2), 8 * HOST_BRIDGE))
        self.cmpj(g("NOT_EQUAL"), (R2, 0), (MEM1(CTX), 8 * HOST_BRIDGE), foreign)
        self.call(self._xcall)
        self._site()
        self.jump(done)
        self.bind(foreign)
        self.call(self._ccall)
        self._site()
        self.bind(done)
        if shift_slots:
            self.op2(g("SUB"), (FRM, 0), (FRM, 0), (IMM, 8 * shift_slots))
        self._status(f)

    def call_record(self, f: Frame, word: int, shift_slots: int) -> None:
        self.op2(g("ADD"), (R1, 0), (CTX, 0), (IMM, 8 * word))
        self._call_record(f, shift_slots)

    def _table_record(self, f: Frame, table: int, sig: int, index: Operand, a64: bool) -> None:
        """R1 = the function record of element `index`: bounds, null, signature (Wasmtime's
        codes, in the spec's order)."""
        self._index(R2, index, a64)
        self.cmpj(
            g("GREATER_EQUAL"),
            (R2, 0),
            (MEM1(CTX), 8 * table + 8),
            self._stub(f, TrapCode.TABLE_OUT_OF_BOUNDS),
        )
        self.op1(g("MOV"), (R1, 0), (MEM1(CTX), 8 * table))
        self.op1(g("MOV"), (R1, 0), (MEM2(R1, R2), 3))
        self.cmpj(g("EQUAL"), (R1, 0), (IMM, 0), self._stub(f, TrapCode.INDIRECT_CALL_TO_NULL))
        self.op1(g("MOV"), (R2, 0), (MEM1(CTX), 8 * sig))
        self.cmpj(g("NOT_EQUAL"), (R2, 0), (MEM1(R1), 8), self._stub(f, TrapCode.BAD_SIGNATURE))

    def call_indirect(
        self, f: Frame, table: int, sig: int, index: Operand, shift_slots: int, *, a64: bool
    ) -> None:  # fmt: skip
        self._table_record(f, table, sig, index, a64)
        self._call_record(f, shift_slots)

    def call_ref(self, f: Frame, ref: Operand, shift_slots: int) -> None:
        self.move(Reg(R1), ref, True)
        self.cmpj(g("EQUAL"), (R1, 0), (IMM, 0), self._stub(f, TrapCode.NULL_REFERENCE))
        self._call_record(f, shift_slots)

    # exceptions (S0012; since D63 `throw` and the clauses are lowered in the core)
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
        lib.sljit_emit_icall(self.c, g("CALL_REG_ARG"), g("ARGS0(32)"), MEM1(CTX), 8 * HOST_BRIDGE)
        self._site()
        self.op1(g("MOV"), (MB, 0), (MEM1(CTX), 8 * MEM0))  # memory.grow may have run
        self._status(f)

    # tail calls: the arguments move down to the frame base, the R pins come back, and a sljit
    # call with SLJIT_CALL_RETURN tears the frame down (S pins, stack) and jumps: the callee
    # returns to our caller, both stacks stay constant
    def _tail(self, f: Frame, src: int, n: int) -> None:
        for j in range(n):
            if src:
                self.op1(g("MOV"), (R2, 0), (MEM1(FRM), 8 * (src + j)))
                self.op1(g("MOV"), (MEM1(FRM), 8 * j), (R2, 0))
        self._rpins_back(f)

    def tail_direct(self, f: Frame, label: Label, src: int, n: int) -> None:
        self._tail(f, src, n)
        self.call(label, ret=True)

    def _tail_record(self, f: Frame, src: int, n: int) -> None:
        """The record in R1 (S0015 "Tail calls across the boundary", forms 1 and 2b). Our
        context: a tail call to its body. Another context: if our return address is this
        module's centry's return point, we were entered through the boundary and owe our caller
        nothing of our convention: return the private HOP status with the record in S2, and
        centry jumps to the record's C entry (a later hop, every `ret` matching its `call`).
        Else the first hop: a tail call into tail_switch (context word 9), the record in R0."""
        other, first = Label(), Label()
        self._tail(f, src, n)
        self.cmpj(g("NOT_EQUAL"), (MEM1(R1), 16), (CTX, 0), other)
        self.op1(g("MOV"), (R1, 0), (MEM1(R1), 0))
        self.op2(g("ADD"), (R1, 0), (R1, 0), (IMM, STUB))
        lib.sljit_emit_icall(self.c, g("CALL_REG_ARG") | CALL_RETURN, g("ARGS0(32)"), R1, 0)
        self.bind(other)
        lib.sljit_emit_op_dst(self.c, g("GET_RETURN_ADDRESS"), R2, 0)
        self.addr(R0, self._centry_ret)
        self.cmpj(g("NOT_EQUAL"), (R0, 0), (R2, 0), first)
        self.op1(g("MOV"), (MB, 0), (R1, 0))
        lib.sljit_emit_return(self.c, g("MOV32"), IMM, s32(HOP))
        self.bind(first)
        self.op1(g("MOV"), (R0, 0), (R1, 0))
        lib.sljit_emit_icall(
            self.c, g("CALL_REG_ARG") | CALL_RETURN, g("ARGS0(32)"), MEM1(CTX), 8 * TAIL_SWITCH
        )

    def tail_indirect(
        self, f: Frame, table: int, sig: int, index: Operand, src: int, n: int, *, a64: bool
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

    # ------------------------------------------------------------ C entries (S0015)
    def c_entry(self) -> int:
        """The 16-byte C entry of the body (or host thunk) that follows: `endbr64; lea rdx,
        [rip + 5]` (custom bytes), then sljit's jump to centry, long by the pad before centry;
        rdx (the C convention's third argument) = the body. Returns its mark; `finish` checks
        the bytes."""
        at = self.mark()
        self.custom(STUB_HEAD[:4])
        self.custom(STUB_HEAD[4:])
        self.link(lib.sljit_emit_jump(self.c, g("JUMP")), self._centry)
        self._stubs.append((at, self.mark()))
        return at

    def c_entry_shared(self) -> int:
        """After the last body: the pad, centry, and (if a record is called) xcall and ccall.
        Returns the number of sites recorded (each shift 0, no map)."""
        for _ in range(128):
            self.custom(b"\xcc")  # sljit shortens a jump within +-128 bytes
        self._centry_at = self.mark()
        self.bind(self._centry)
        # int32_t centry(S0 = ctx, S1 = frame, S2 = body): C, saves S0-S2 (32 bytes); the body
        # keeps S3-S5 and R3-R6 itself
        self.enter(0, "ARGS3(32, P, P, P)", 3, 3, 0)
        self.op1(g("MOV"), (R1, 0), (S2, 0))
        self.op1(g("MOV"), (MB, 0), (MEM1(CTX), 8 * MEM0))
        lib.sljit_emit_icall(self.c, g("CALL_REG_ARG"), g("ARGS0(32)"), R1, 0)
        self.bind(self._centry_ret)
        self._site()
        done = Label()
        self.cmpj(g("NOT_EQUAL") | B32, (R0, 0), (IMM, s32(HOP)), done)
        # the hop: a body of this module left a tail call into another context, the record in
        # S2; a C sibling call to its C entry with this C entry's return address
        self.op1(g("MOV"), (R0, 0), (MEM1(S2), 16))
        self.op1(g("MOV"), (R1, 0), (FRM, 0))
        lib.sljit_emit_icall(self.c, g("CALL") | CALL_RETURN, g("ARGS2(32, W, W)"), MEM1(S2), 0)
        self.bind(done)
        lib.sljit_emit_return(self.c, g("MOV32"), R0, 0)
        if not self._records:
            return 1
        # xcall: the record in R1 is another context of this compiler (D71); S0 is kept in a
        # 16-byte frame, the callee's S0 and S2 set, its body called
        self.bind(self._xcall)
        self.enter(REG_ENTER, "ARGS0(32)", 3, 3, 8)
        self.op1(g("MOV"), (MEM1(SP), 0), (CTX, 0))
        self.op1(g("MOV"), (CTX, 0), (MEM1(R1), 16))
        self.op1(g("MOV"), (MB, 0), (MEM1(CTX), 8 * MEM0))
        self.op1(g("MOV"), (R1, 0), (MEM1(R1), 0))
        self.op2(g("ADD"), (R1, 0), (R1, 0), (IMM, STUB))
        lib.sljit_emit_icall(self.c, g("CALL_REG_ARG"), g("ARGS0(32)"), R1, 0)
        self._site()
        self.op1(g("MOV"), (CTX, 0), (MEM1(SP), 0))
        self.op1(g("MOV"), (MB, 0), (MEM1(CTX), 8 * MEM0))
        lib.sljit_emit_return(self.c, g("MOV32"), R0, 0)
        # ccall: the record in R1 is another compiler's: its C entry (S0015: SysV, int32_t
        # code(ctx, frame)) may clobber R3-R6, which hold pins of the frames above (LESSONS 2);
        # S0-S5 are callee-saved in C. S2 is reloaded: the callee may have grown a memory
        self.bind(self._ccall)
        self.enter(REG_ENTER, "ARGS0(32)", 7, 3, 8 * len(RSAVE))
        for i, r in enumerate(RSAVE):
            self.op1(g("MOV"), (MEM1(SP), 8 * i), (r, 0))
        self.op1(g("MOV"), (R0, 0), (MEM1(R1), 16))
        self.op1(g("MOV"), (R2, 0), (R1, 0))
        self.op1(g("MOV"), (R1, 0), (FRM, 0))
        lib.sljit_emit_icall(self.c, g("CALL"), g("ARGS2(32, W, W)"), MEM1(R2), 0)
        self._site()
        for i, r in enumerate(RSAVE):
            self.op1(g("MOV"), (r, 0), (MEM1(SP), 8 * i))
        self.op1(g("MOV"), (MB, 0), (MEM1(CTX), 8 * MEM0))
        lib.sljit_emit_return(self.c, g("MOV32"), R0, 0)
        return 3

    def host_thunk(self, word: int) -> int:
        """The C entry a host import's record points to, then its body: called like a wasm
        function, it calls the import in context word `word` through the bridge with its frame
        as the slots. Returns the C entry's mark."""
        at = self.c_entry()
        self.enter(REG_ENTER, "ARGS0(32)", 3, 3, 0)
        self.op1(g("MOV"), (R0, 0), (IMM, word))
        self.op1(g("MOV"), (R1, 0), (FRM, 0))
        lib.sljit_emit_icall(self.c, g("CALL_REG_ARG"), g("ARGS0(32)"), MEM1(CTX), 8 * HOST_BRIDGE)
        self._site()
        self.op1(g("MOV"), (MB, 0), (MEM1(CTX), 8 * MEM0))  # a tail-call target's caller does
        lib.sljit_emit_return(self.c, g("MOV32"), R0, 0)  # not reload it (memory.grow)
        return at


def _win64_call(fn: int) -> bytes:
    """`mov rcx, <S0>; mov rdx, <R1>; call <fn>`: the host call of the win64 bridge as custom
    bytes, as sljit is built for SysV on every OS (LESSONS 141); registers from sljit's map."""
    ctx, slots, f = (lib.sljit_get_register_index(0, r) for r in (S0, R1, fn))  # SLJIT_GP_REGISTER

    def mov(d: int, s: int) -> bytes:  # mov r64 d, r64 s (89 /r)
        return bytes((0x48 | (s >> 3) << 2 | d >> 3, 0x89, 0xC0 | (s & 7) << 3 | (d & 7)))

    call = bytes((0x41,) if f >= 8 else ()) + bytes((0xFF, 0xD0 | (f & 7)))  # call r64 (FF /2)
    return mov(1, ctx) + mov(2, slots) + call


@cache
def trampoline_blob(conv: str = "sysv") -> tuple[bytes, tuple[int, ...], tuple[int, int]]:
    """This compiler's blob for `conv` (S0015 `bridge` and `tail_switch`, S0016): the bridge,
    then tail_switch; their offsets and `restore`'s; tail_switch's restore-frame numbers. The
    `win64` bridge differs only at the host call (custom bytes) and keeps its locals above the
    call's 32-byte shadow space; tail_switch is the same bytes in both."""
    home = {"sysv": 0, "win64": 32}[conv]
    e = Emitter()
    # bridge: R0 = the context word of the host function, R1 = its slots. Calls host(ctx,
    # slots) with ctx[HOST_ERROR] preset to 13, returns the word, puts the enclosing preset
    # back; R3-R6 saved for all frames above (LESSONS 2); the store's exit record {the caller's
    # stack pointer at its call, the return address, the frame} written before the host runs
    # and its sp cleared after (S0013 3a). Locals: R3-R6, then the preset (win64: above the
    # shadow space, [SP, SP + 32) at the call, which the host owns).
    bridge = e.mark()
    e.enter(REG_ENTER, "ARGS0(32)", 7, 4, home + 8 * 5)
    for i, r in enumerate(RSAVE):
        e.op1(g("MOV"), (MEM1(SP), home + 8 * i), (r, 0))
    e.op1(g("MOV"), (R2, 0), (MEM1(CTX), 8 * EXIT_RECORD))
    lib.sljit_emit_op_dst(e.c, g("GET_RETURN_ADDRESS"), S3, 0)
    e.op1(g("MOV"), (MEM1(R2), 8), (S3, 0))
    lib.sljit_get_local_base(e.c, S3, 0, e.up - LOCALS_OFFSET)  # the caller's sp at its call
    e.op1(g("MOV"), (MEM1(R2), 0), (S3, 0))
    e.op1(g("MOV"), (MEM1(R2), 16), (FRM, 0))
    e.op1(g("MOV"), (R2, 0), (MEM1(CTX), 8 * HOST_ERROR))
    e.op1(g("MOV"), (MEM1(SP), home + 32), (R2, 0))
    e.op1(g("MOV"), (MEM1(CTX), 8 * HOST_ERROR), (IMM, STATUS_HOST_ERROR))
    e.op1(g("MOV"), (S3, 0), (MEM2(CTX, R0), 3))
    if conv == "win64":
        e.custom(_win64_call(S3))
    else:
        e.op1(g("MOV"), (R0, 0), (CTX, 0))
        lib.sljit_emit_icall(e.c, g("CALL"), g("ARGS2V(P, P)"), S3, 0)
    e.op1(g("MOV"), (R1, 0), (MEM1(CTX), 8 * EXIT_RECORD))  # no wasm frame waits in a host call
    e.op1(g("MOV"), (MEM1(R1), 0), (IMM, 0))
    e.op1(g("MOV_U32"), (R0, 0), (MEM1(CTX), 8 * HOST_ERROR))
    e.op1(g("MOV"), (R1, 0), (MEM1(SP), home + 32))
    e.op1(g("MOV"), (MEM1(CTX), 8 * HOST_ERROR), (R1, 0))
    for i, r in enumerate(RSAVE):
        e.op1(g("MOV"), (r, 0), (MEM1(SP), home + 8 * i))
    lib.sljit_emit_return(e.c, g("MOV32"), R0, 0)
    # tail_switch (S0015 form 2b): reached by a tail call (SLJIT_CALL_RETURN) with R0 = a record
    # of another context, the arguments at S1, its return address the tail caller's. A return
    # address equal to `restore` is a later hop: switch and tail-call the target, the restore
    # frame below already keeps what our caller's caller expects. Else the first hop: the
    # restore frame keeps R3-R6 and S0 (a C entry may clobber R3-R6, another context changes S0)
    # and calls tail_switch itself, so the target, whichever kind, returns to `restore`.
    ts = e.mark()
    entry, hop, foreign = Label(), Label(), Label()
    e.bind(entry)
    e.enter(REG_ENTER, "ARGS0(32)", 7, 3, 8 * (len(RSAVE) + 1))
    nums = (e.ret_at, e.up)
    restore = Label()
    lib.sljit_emit_op_dst(e.c, g("GET_RETURN_ADDRESS"), R1, 0)
    e.addr(R2, restore)
    e.cmpj(g("EQUAL"), (R1, 0), (R2, 0), hop)
    for i, r in enumerate((*RSAVE, CTX)):
        e.op1(g("MOV"), (MEM1(SP), 8 * i), (r, 0))
    e.call(entry)
    e.bind(restore)
    at = e.mark()
    for i, r in enumerate((*RSAVE, CTX)):
        e.op1(g("MOV"), (r, 0), (MEM1(SP), 8 * i))
    e.op1(g("MOV"), (MB, 0), (MEM1(CTX), 8 * MEM0))  # the callee may have grown a memory
    lib.sljit_emit_return(e.c, g("MOV32"), R0, 0)
    e.bind(hop)
    e.op1(g("MOV"), (R1, 0), (MEM1(R0), 16))
    e.op1(g("MOV"), (R2, 0), (MEM1(R1), 8 * HOST_BRIDGE))
    e.cmpj(g("NOT_EQUAL"), (R2, 0), (MEM1(CTX), 8 * HOST_BRIDGE), foreign)
    e.op1(g("MOV"), (CTX, 0), (R1, 0))  # this compiler's context: its body (D71)
    e.op1(g("MOV"), (MB, 0), (MEM1(CTX), 8 * MEM0))
    e.op1(g("MOV"), (R1, 0), (MEM1(R0), 0))
    e.op2(g("ADD"), (R1, 0), (R1, 0), (IMM, STUB))
    lib.sljit_emit_icall(e.c, g("CALL_REG_ARG") | CALL_RETURN, g("ARGS0(32)"), R1, 0)
    e.bind(foreign)  # another compiler's: its C entry, a C sibling call
    e.op1(g("MOV"), (R2, 0), (R0, 0))
    e.op1(g("MOV"), (R0, 0), (R1, 0))
    e.op1(g("MOV"), (R1, 0), (FRM, 0))
    lib.sljit_emit_icall(e.c, g("CALL") | CALL_RETURN, g("ARGS2(32, W, W)"), MEM1(R2), 0)
    try:
        code, size = e.generate()
    except BaseException:
        e.close()
        raise
    offs = e.offsets(code)
    data = ctypes.string_at(code, size)
    e.free(code)
    return data, (offs[bridge], offs[ts], offs[at]), nums
