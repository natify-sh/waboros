"""Experiment: waboros front end + sljit as the code generator, for CoreMark's subset of wasm.

    python w2sljit.py core.wasm [--debug]

Every function is compiled by sljit to a C function `sljit_sw f(sljit_sw *args)`; calls go through a
table of addresses; memory is a fixed mapping whose base sits in S0; the five most used locals are
pinned to S1..S5; operand-stack depth d lives in R2+d for d < 5, deeper entries in the frame. No
bounds checks (as in guard mode). x86-64 / aarch64 hosts only. Not waboros code: a measurement.
"""

import ctypes
import json
import mmap
import os
import struct
import sys
import time

from waboros.frontend import binary
from waboros.frontend.validate import validate

HERE = os.path.dirname(os.path.abspath(__file__))  # build dir: libsljit*.so and consts.json next to this file
K = json.load(open(os.path.join(HERE, "consts.json")))
DEBUG = "--debug" in sys.argv
lib = ctypes.CDLL(os.path.join(HERE, "libsljit_dbg.so" if DEBUG else "libsljit.so"))

P, I32, SW = ctypes.c_void_p, ctypes.c_int32, ctypes.c_int64
for name, res, args in [
    ("sljit_create_compiler", P, [P]),
    ("sljit_free_compiler", None, [P]),
    ("sljit_emit_enter", I32, [P, I32, I32, I32, I32, I32]),
    ("sljit_emit_return_void", I32, [P]),
    ("sljit_emit_return", I32, [P, I32, I32, SW]),
    ("sljit_emit_op0", I32, [P, I32]),
    ("sljit_emit_op1", I32, [P, I32, I32, SW, I32, SW]),
    ("sljit_emit_op2", I32, [P, I32, I32, SW, I32, SW, I32, SW]),
    ("sljit_emit_op2u", I32, [P, I32, I32, SW, I32, SW]),
    ("sljit_emit_op_flags", I32, [P, I32, I32, SW, I32]),
    ("sljit_emit_label", P, [P]),
    ("sljit_emit_jump", P, [P, I32]),
    ("sljit_emit_cmp", P, [P, I32, I32, SW, I32, SW]),
    ("sljit_set_label", None, [P, P]),
    ("sljit_emit_icall", I32, [P, I32, I32, I32, SW]),
    ("sljit_get_local_base", I32, [P, I32, SW, SW]),
    ("sljit_emit_fop1", I32, [P, I32, I32, SW, I32, SW]),
    ("sljit_emit_fop2", I32, [P, I32, I32, SW, I32, SW, I32, SW]),
    ("sljit_emit_fcopy", I32, [P, I32, I32, I32]),
    ("sljit_generate_code", P, [P, I32, P]),
    ("w2s_code_size", ctypes.c_uint64, [P]),
]:
    fn = getattr(lib, name)
    fn.restype, fn.argtypes = res, args

g = lambda n: K["SLJIT_" + n]  # noqa: E731
R = [g(f"R{i}") for i in range(7)]
S = [g(f"S{i}") for i in range(6)]
FR0, FR1, SP, IMM = g("FR0"), g("FR1"), g("SP"), g("IMM")
MEM, MEM0 = g("MEM"), g("MEM0()")
MEM1 = lambda r: MEM | r  # noqa: E731
F32 = g("32")
NSTACKREG, NPIN = 5, 5
ARGS1_W_P = g("ARGS1(W, P)")

CMP = {  # opcode: (set flag, condition)
    0x46: ("SET_Z", "EQUAL"), 0x47: ("SET_Z", "NOT_EQUAL"),
    0x48: ("SET_SIG_LESS", "SIG_LESS"), 0x49: ("SET_LESS", "LESS"),
    0x4A: ("SET_SIG_GREATER", "SIG_GREATER"), 0x4B: ("SET_GREATER", "GREATER"),
    0x4C: ("SET_SIG_LESS_EQUAL", "SIG_LESS_EQUAL"), 0x4D: ("SET_LESS_EQUAL", "LESS_EQUAL"),
    0x4E: ("SET_SIG_GREATER_EQUAL", "SIG_GREATER_EQUAL"), 0x4F: ("SET_GREATER_EQUAL", "GREATER_EQUAL"),
}  # fmt: skip
NEG = {"EQUAL": "NOT_EQUAL", "NOT_EQUAL": "EQUAL", "SIG_LESS": "SIG_GREATER_EQUAL",
       "SIG_GREATER_EQUAL": "SIG_LESS", "LESS": "GREATER_EQUAL", "GREATER_EQUAL": "LESS",
       "SIG_GREATER": "SIG_LESS_EQUAL", "SIG_LESS_EQUAL": "SIG_GREATER",
       "GREATER": "LESS_EQUAL", "LESS_EQUAL": "GREATER"}  # fmt: skip
BIN32 = {0x6A: "ADD32", 0x6B: "SUB32", 0x6C: "MUL32", 0x71: "AND32", 0x72: "OR32", 0x73: "XOR32",
         0x74: "MSHL32", 0x75: "MASHR32", 0x76: "MLSHR32"}  # fmt: skip
LOADS = {0x28: "MOV32", 0x2B: "MOV", 0x2D: "MOV32_U8", 0x2C: "MOV32_S8", 0x2E: "MOV32_S16",
         0x2F: "MOV32_U16", 0x29: "MOV"}  # fmt: skip
STORES = {0x36: "MOV32", 0x37: "MOV", 0x39: "MOV", 0x3A: "MOV_U8", 0x3B: "MOV_U16"}
FBIN = {0xA0: "ADD_F64", 0xA1: "SUB_F64", 0xA2: "MUL_F64", 0xA3: "DIV_F64"}
FCMP = {0x61: "F_EQUAL", 0x62: "F_NOT_EQUAL", 0x63: "F_LESS", 0x64: "F_GREATER"}


class Fn:
    def __init__(self, mod, fi, ty, code, insns, table_addr, glob_addr, mem_base):
        self.mod, self.fi, self.insns = mod, fi, insns
        self.params, self.results = ty
        self.nlocals = len(self.params) + len(code[0])
        self.table_addr, self.glob_addr, self.mem_base = table_addr, glob_addr, mem_base
        # pin the most used locals
        w = [0] * self.nlocals
        for ins in insns:
            if ins[0] in (0x20, 0x21, 0x22):
                w[ins[1]] += 1
        order = sorted(range(self.nlocals), key=lambda j: -w[j])
        self.pins = {j: S[1 + k] for k, j in enumerate(order[:NPIN]) if w[j]}
        maxh = 64
        self.loc_off = 0
        self.spill_off = self.loc_off + 8 * self.nlocals
        self.save_off = self.spill_off + 8 * maxh
        self.args_off = self.save_off + 8 * maxh
        self.ftmp_off = self.args_off + 8 * 16
        self.frame = self.ftmp_off + 16
        self.c = lib.sljit_create_compiler(None)

    # locations
    def L(self, j):
        r = self.pins.get(j)
        return (r, 0) if r else (MEM1(SP), self.loc_off + 8 * j)

    def D(self, d):
        return (R[2 + d], 0) if d < NSTACKREG else (MEM1(SP), self.spill_off + 8 * d)

    def op1(self, op, dst, src):
        lib.sljit_emit_op1(self.c, g(op), *dst, *src)

    def op2(self, op, dst, a, b):
        lib.sljit_emit_op2(self.c, g(op), *dst, *a, *b)

    def imm(self, v):
        return (IMM, v)

    def to_f(self, fr, loc):
        if loc[0] & MEM:
            lib.sljit_emit_fop1(self.c, g("MOV_F64"), fr, 0, *loc)
        else:
            lib.sljit_emit_fcopy(self.c, g("COPY_TO_F64"), fr, loc[0])

    def from_f(self, loc, fr):
        if loc[0] & MEM:
            lib.sljit_emit_fop1(self.c, g("MOV_F64"), *loc, fr, 0)
        else:
            lib.sljit_emit_fcopy(self.c, g("COPY_FROM_F64"), fr, loc[0])

    def addr(self, a_loc, offset):
        """R1 = mem_base + zero-extended a; returns the memory operand."""
        self.op1("MOV_U32", (R[1], 0), a_loc)
        self.op2("ADD", (R[1], 0), (R[1], 0), (S[0], 0))
        return (MEM1(R[1]), offset)

    def compile(self):
        c = self.c
        lib.sljit_emit_enter(c, 0, ARGS1_W_P, 7 | (2 << 8), 6, self.frame)
        # S0 = args pointer on entry: copy params, zero the other locals, then S0 = memory base
        for j in range(self.nlocals):
            src = (MEM1(S[0]), 8 * j) if j < len(self.params) else self.imm(0)
            self.op1("MOV", self.L(j), src)
        self.op1("MOV", (S[0], 0), self.imm(self.mem_base))
        self.h = 0
        self.ctl = [["func", None, [], 0, len(self.results)]]  # kind, label, jumps, height, arity
        self.pending = None  # (cond, a, b) for a compare on top
        self.dead = 0
        for ins in self.insns:
            self.ins(ins)
        code = lib.sljit_generate_code(c, 0, None)
        if not code:
            raise RuntimeError(f"function {self.fi}: sljit error")
        self.size = lib.w2s_code_size(c)
        lib.sljit_free_compiler(c)
        return code

    def materialize(self):
        if self.pending:
            (flag, cond), a, b, d = self.pending
            self.pending = None
            lib.sljit_emit_op2u(self.c, g("SUB32") | g(flag), *a, *b)
            lib.sljit_emit_op_flags(self.c, g("MOV32"), *self.D(d), g(cond))

    def moves_to(self, height, arity):
        for i in range(arity):
            src, dst = self.h - arity + i, height + i
            if src != dst:
                self.op1("MOV", self.D(dst), self.D(src))

    def br(self, depth):
        t = self.ctl[-1 - depth]
        if t[0] == "loop":
            self.moves_to(t[3], 0)
            lib.sljit_set_label(lib.sljit_emit_jump(self.c, g("JUMP")), t[1])
        elif t[0] == "func":
            self.ret()
        else:
            self.moves_to(t[3], t[4])
            t[2].append(lib.sljit_emit_jump(self.c, g("JUMP")))

    def ret(self):
        if self.results:
            lib.sljit_emit_return(self.c, g("MOV"), *self.D(self.h - 1))
        else:  # every function is declared `sljit_sw f(sljit_sw *)`
            lib.sljit_emit_return(self.c, g("MOV"), IMM, 0)

    def ins(self, ins):
        op, c = ins[0], self.c
        if self.dead:
            if op in (0x02, 0x03, 0x04):
                self.dead += 1
            elif op == 0x0B:
                self.dead -= 1
                if self.dead == 0:
                    self.end_block()
            return
        if self.pending and op not in (0x0D, 0x45):
            self.materialize()
        if op in (0x02, 0x03):  # block, loop
            params, results = ins[1]
            h = self.h - len(params)
            if op == 0x02:
                self.ctl.append(["block", None, [], h, len(results)])
            else:
                self.ctl.append(["loop", lib.sljit_emit_label(c), [], h, len(params)])
        elif op == 0x0B:  # end
            self.end_block()
        elif op == 0x0C:
            self.br(ins[1])
            self.dead = 1
        elif op == 0x0D:  # br_if
            if self.pending:
                (flag, cond), a, b, d = self.pending
                self.pending = None
                self.h -= 1
                t = self.ctl[-1 - ins[1]]
                if (t[0] == "block" and t[4] == 0) or t[0] == "loop":
                    j = lib.sljit_emit_cmp(c, g(cond) | F32, *a, *b)
                    (t[2].append(j) if t[0] == "block" else lib.sljit_set_label(j, t[1]))
                    return
                skip = lib.sljit_emit_cmp(c, g(NEG[cond]) | F32, *a, *b)
            else:
                self.h -= 1
                skip = lib.sljit_emit_cmp(c, g("EQUAL") | F32, *self.D(self.h), *self.imm(0))
            self.br(ins[1])
            lib.sljit_set_label(skip, lib.sljit_emit_label(c))
        elif op == 0x0E:  # br_table
            labels, default = ins[1], ins[2]
            self.h -= 1
            idx = self.D(self.h)
            self.op1("MOV32", (R[0], 0), idx)
            for i, dep in enumerate(labels):
                skip = lib.sljit_emit_cmp(c, g("NOT_EQUAL") | F32, R[0], 0, *self.imm(i))
                self.br(dep)
                lib.sljit_set_label(skip, lib.sljit_emit_label(c))
            self.br(default)
            self.dead = 1
        elif op == 0x0F:
            self.ret()
            self.dead = 1
        elif op == 0x10:
            self.call(ins[1])
        elif op == 0x1A:
            self.h -= 1
        elif op == 0x1B:  # select
            self.h -= 3
            a, b, cnd = self.D(self.h), self.D(self.h + 1), self.D(self.h + 2)
            j = lib.sljit_emit_cmp(c, g("NOT_EQUAL") | F32, *cnd, *self.imm(0))
            self.op1("MOV", a, b)
            lib.sljit_set_label(j, lib.sljit_emit_label(c))
            self.h += 1
        elif op == 0x20:
            self.op1("MOV", self.D(self.h), self.L(ins[1]))
            self.h += 1
        elif op == 0x21:
            self.h -= 1
            self.op1("MOV", self.L(ins[1]), self.D(self.h))
        elif op == 0x22:
            self.op1("MOV", self.L(ins[1]), self.D(self.h - 1))
        elif op == 0x23:
            self.op1("MOV32", self.D(self.h), (MEM0, self.glob_addr + 8 * ins[1]))
            self.h += 1
        elif op == 0x24:
            self.h -= 1
            self.op1("MOV32", (MEM0, self.glob_addr + 8 * ins[1]), self.D(self.h))
        elif op in LOADS:
            m = self.addr(self.D(self.h - 1), ins[1])
            self.op1(LOADS[op], self.D(self.h - 1), m)
        elif op in STORES:
            self.h -= 2
            v = self.D(self.h + 1)
            if v[0] & MEM:  # memory to memory: through R0
                self.op1("MOV", (R[0], 0), v)
                v = (R[0], 0)
            m = self.addr(self.D(self.h), ins[1])
            self.op1(STORES[op], m, v)
        elif op in (0x41, 0x42):
            self.op1("MOV", self.D(self.h), self.imm(ins[1] & (2**64 - 1)))
            self.h += 1
        elif op == 0x44:
            self.op1("MOV", self.D(self.h), self.imm(ins[1]))
            self.h += 1
        elif op == 0x45:  # eqz
            if self.pending:
                (flag, cond), a, b, d = self.pending
                inv = NEG[cond]
                flag = "SET_Z" if inv in ("EQUAL", "NOT_EQUAL") else "SET_" + inv
                self.pending = ((flag, inv), a, b, d)
            else:
                d = self.h - 1
                self.pending = (("SET_Z", "EQUAL"), self.D(d), self.imm(0), d)
        elif op in CMP:
            self.h -= 1
            d = self.h - 1
            self.pending = (CMP[op], self.D(d), self.D(self.h), d)
        elif op in BIN32:
            self.h -= 1
            self.op2(BIN32[op], self.D(self.h - 1), self.D(self.h - 1), self.D(self.h))
        elif op in (0x6E, 0x6F, 0x6D, 0x70):  # div_u, rem_s, div_s, rem_u
            self.h -= 1
            self.op1("MOV32", (R[0], 0), self.D(self.h - 1))
            self.op1("MOV32", (R[1], 0), self.D(self.h))
            kind = {0x6E: "DIV_U32", 0x6D: "DIV_S32", 0x6F: "DIVMOD_S32", 0x70: "DIVMOD_U32"}[op]
            lib.sljit_emit_op0(c, g(kind))
            self.op1("MOV32", self.D(self.h - 1), (R[1] if op in (0x6F, 0x70) else R[0], 0))
        elif op == 0xC0:
            self.op1("MOV32_S8", self.D(self.h - 1), self.D(self.h - 1))
        elif op == 0xC1:
            self.op1("MOV32_S16", self.D(self.h - 1), self.D(self.h - 1))
        elif op in FBIN:
            self.h -= 1
            self.to_f(FR0, self.D(self.h - 1))
            self.to_f(FR1, self.D(self.h))
            lib.sljit_emit_fop2(c, g(FBIN[op]), FR0, 0, FR0, 0, FR1, 0)
            self.from_f(self.D(self.h - 1), FR0)
        elif op == 0x9A:
            self.to_f(FR0, self.D(self.h - 1))
            lib.sljit_emit_fop1(c, g("NEG_F64"), FR0, 0, FR0, 0)
            self.from_f(self.D(self.h - 1), FR0)
        elif op == 0x9D:  # f64.trunc (values in range here)
            self.to_f(FR0, self.D(self.h - 1))
            lib.sljit_emit_fop1(c, g("CONV_SW_FROM_F64"), R[0], 0, FR0, 0)
            lib.sljit_emit_fop1(c, g("CONV_F64_FROM_SW"), FR0, 0, R[0], 0)
            self.from_f(self.D(self.h - 1), FR0)
        elif op in FCMP:
            self.h -= 1
            self.to_f(FR0, self.D(self.h - 1))
            self.to_f(FR1, self.D(self.h))
            lib.sljit_emit_fop1(c, g("CMP_F64") | g("SET_" + FCMP[op]), FR0, 0, FR1, 0)
            lib.sljit_emit_op_flags(c, g("MOV32"), *self.D(self.h - 1), g(FCMP[op]))
        elif op == 0xB8:  # f64.convert_i32_u
            self.op1("MOV_U32", (R[0], 0), self.D(self.h - 1))
            lib.sljit_emit_fop1(c, g("CONV_F64_FROM_SW"), FR0, 0, R[0], 0)
            self.from_f(self.D(self.h - 1), FR0)
        elif op in (0xFC02, 0xFC03):  # i32.trunc_sat_f64_s/u (values in range here)
            self.to_f(FR0, self.D(self.h - 1))
            lib.sljit_emit_fop1(c, g("CONV_SW_FROM_F64"), R[0], 0, FR0, 0)
            self.op1("MOV32", self.D(self.h - 1), (R[0], 0))
        else:
            raise NotImplementedError(hex(op))

    def end_block(self):
        t = self.ctl.pop()
        if t[0] == "func":
            if not self.dead:
                self.ret()
            return
        if t[0] == "block" and t[2]:
            lab = lib.sljit_emit_label(self.c)
            for j in t[2]:
                lib.sljit_set_label(j, lab)
        self.h = t[3] + (t[4] if t[0] == "block" else len(self.mod_results_of_loop(t)))
        self.dead = 0

    def mod_results_of_loop(self, t):
        return []  # CoreMark's loops have no results

    def call(self, fidx):
        ty = self.mod["types"][self.all_types[fidx]]
        n, nres = len(ty[0]), len(ty[1])
        base = self.h - n
        for i in range(n):
            self.op1("MOV", (MEM1(SP), self.args_off + 8 * i), self.D(base + i))
        live = [d for d in range(min(base, NSTACKREG))]
        for d in live:
            self.op1("MOV", (MEM1(SP), self.save_off + 8 * d), self.D(d))
        lib.sljit_get_local_base(self.c, R[0], 0, self.args_off)
        lib.sljit_emit_icall(self.c, g("CALL"), ARGS1_W_P, MEM0, self.table_addr + 8 * fidx)
        for d in live:
            self.op1("MOV", self.D(d), (MEM1(SP), self.save_off + 8 * d))
        self.h = base
        if nres:
            self.op1("MOV", self.D(self.h), (R[0], 0))
            self.h += 1


def main():
    path = sys.argv[1]
    mod = binary.parse(open(path, "rb").read())
    validate(mod)
    imports = [i for i in mod["imports"] if i[0] == "func"]
    nimp = len(imports)
    all_types = [i[3] for i in imports] + list(mod["funcs"])
    # memory: 17 pages + 64 KiB slack, never moved
    pages = mod["mems"][0][0]
    mem = mmap.mmap(-1, pages * 65536 + 65536)
    mem_base = ctypes.addressof(ctypes.c_char.from_buffer(mem))
    for (_, off), data in mod["datas"]:
        mem[off : off + len(data)] = data
    globs = (ctypes.c_int64 * max(1, len(mod["globals"])))()
    for i, (_, _, init) in enumerate(mod["globals"]):
        globs[i] = init[1]
    table = (ctypes.c_uint64 * len(all_types))()
    # host imports: millis, print, as `sljit_sw f(sljit_sw *args)`
    HOST = ctypes.CFUNCTYPE(ctypes.c_int64, ctypes.POINTER(ctypes.c_int64))
    t0 = time.monotonic()
    out = []

    def millis(a):
        return int((time.monotonic() - t0) * 1000) & 0xFFFFFFFF

    def prnt(a):
        p, n = a[0] & 0xFFFFFFFF, a[1] & 0xFFFFFFFF
        sys.stdout.write(bytes(mem[p : p + n]).decode("latin-1"))
        return 0

    hosts = {"millis": HOST(millis), "print": HOST(prnt)}
    for i, imp in enumerate(imports):
        table[i] = ctypes.cast(hosts[imp[2]], ctypes.c_void_p).value
    tc = time.perf_counter()
    total = 0
    for k, (code, insns) in enumerate(zip(mod["codes"], mod["insns"])):
        fi = nimp + k
        f = Fn(mod, fi, mod["types"][all_types[fi]], code, insns, ctypes.addressof(table),
               ctypes.addressof(globs), mem_base)
        f.all_types = all_types
        table[fi] = f.compile()
        total += f.size
    print(f"[w2sljit] compiled {len(mod['codes'])} functions, {total} bytes of code in "
          f"{(time.perf_counter() - tc) * 1e3:.1f} ms", flush=True)
    setup = dict((n, i) for n, k, i in mod["exports"] if k == 0)["setup"]
    fn = HOST(table[setup])
    args = (ctypes.c_int64 * 1)()
    fn(args)
    sys.stdout.flush()


if __name__ == "__main__":
    main()
