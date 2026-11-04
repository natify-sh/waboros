"""The one-pass JIT core (D26), shared by every architecture: reads each validated function body
once and drives a `Backend` (common/backend.py). Nothing here names a machine register.

Frame (S0003): slot j < L holds local j (parameters first, so the caller's arguments are already
in place); operand-stack entry i lives in slot L + i when it is materialised. Results go to slots
0..m on return. A wasm call puts the callee's frame at the first argument (frame overlap).

The operand stack is lazy: an entry is a constant, a local, the value in the backend's `tos`
register (at most one entry), a materialised slot, a pending compare (only ever on top; it is
consumed whole by `br_if`/`if`/`select`/`eqz`, else turned into a value before anything else is
emitted), or an i32 pinned local plus a constant (`ADDK`, S0007 K: one instruction where it is
consumed). Every instruction that can trap is emitted when it is reached, so `drop`, `select` and
branches never discard a trap (LESSONS 28). At every control-flow edge the whole stack is in
slots, and pinned locals never move, so merges need no reconciliation; a function return instead
moves its results straight into slots 0..m (S0007).

Test-only switch: `WABOROS_JIT_NOPIN=all` or `=<function indices>` compiles those functions without
pinned locals (bisecting a pinning bug; D25 rules out mixing targets per function).
"""

import os
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from waboros.targets.base import TrapCode, Unsupported, ValidModule
from waboros.targets.jit.common import simd
from waboros.targets.jit.common.backend import NEGATE, AddK, Backend, Imm, Operand, Reg, Slot
from waboros.targets.jit.common.context import (
    DATA_DROP,
    ELEM_DROP,
    MEMORY_GROW,
    MEMORY_INIT,
    TABLE_GROW,
    TABLE_INIT,
    Layout,
)

I32, I64, F32, F64 = 0x7F, 0x7E, 0x7D, 0x7C
NUM = (I32, I64, F32, F64)
WIDE = (I64, F64)
NARROW = (I32, F32)  # everything else (i64, f64, references) is a 64-bit value
M32, M64 = 0xFFFFFFFF, (1 << 64) - 1
CONST, LOCAL, TOS, SLOT, CMP, ADDK = range(6)
SLOTE = (SLOT, None)
Entry = tuple[int, Any]

_BIN = "add sub mul div_s div_u rem_s rem_u and or xor shl shr_s shr_u rotl rotr".split()
_CMP = "eq ne lt_s lt_u gt_s gt_u le_s le_u ge_s ge_u".split()
BINOPS = {**{0x6A + i: (n, False) for i, n in enumerate(_BIN)},
          **{0x7C + i: (n, True) for i, n in enumerate(_BIN)}}  # fmt: skip
CMPS = {**{0x46 + i: (n, False) for i, n in enumerate(_CMP)},
        **{0x51 + i: (n, True) for i, n in enumerate(_CMP)}}  # fmt: skip
UNOPS = {0x67: ("clz", False), 0x68: ("ctz", False), 0x69: ("popcnt", False),
         0x79: ("clz", True), 0x7A: ("ctz", True), 0x7B: ("popcnt", True), 0xA7: ("wrap", False),
         0xAC: ("extend_i32_s", True), 0xAD: ("extend_i32_u", True), 0xC0: ("extend8_s", False),
         0xC1: ("extend16_s", False), 0xC2: ("extend8_s", True), 0xC3: ("extend16_s", True),
         0xC4: ("extend32_s", True)}  # fmt: skip
LOADS = {0x28: (4, False, False), 0x29: (8, False, True), 0x2A: (4, False, False),
         0x2B: (8, False, True), 0x2C: (1, True, False), 0x2D: (1, False, False),
         0x2E: (2, True, False), 0x2F: (2, False, False), 0x30: (1, True, True),
         0x31: (1, False, True), 0x32: (2, True, True), 0x33: (2, False, True),
         0x34: (4, True, True), 0x35: (4, False, True)}  # fmt: skip
STORES = {0x36: 4, 0x37: 8, 0x38: 4, 0x39: 8, 0x3A: 1, 0x3B: 2, 0x3C: 1, 0x3D: 2, 0x3E: 4}
REINTERPRET = (0xBC, 0xBD, 0xBE, 0xBF)  # bit patterns: nothing to do
# Floats are bit patterns in the same entries as integers (slots, tos, constants); the backend
# computes each float operation whole (D25: natively), with its own scratch registers.
_FCMP, _FUN = "eq ne lt gt le ge".split(), "abs neg ceil floor trunc nearest sqrt".split()
_FBIN = "add sub mul div min max copysign".split()
FCMPS = {**{0x5B + i: (n, False) for i, n in enumerate(_FCMP)},
         **{0x61 + i: (n, True) for i, n in enumerate(_FCMP)}}  # fmt: skip
FUNOPS = {**{0x8B + i: (n, False) for i, n in enumerate(_FUN)},
          **{0x99 + i: (n, True) for i, n in enumerate(_FUN)}}  # fmt: skip
FBINOPS = {**{0x92 + i: (n, False) for i, n in enumerate(_FBIN)},
           **{0xA0 + i: (n, True) for i, n in enumerate(_FBIN)}}  # fmt: skip
_TRUNCS = [(r, f, s) for r in ("i32", "i64") for f in ("f32", "f64") for s in "su"]
_CONVS = [(r, f, s) for f in ("f32", "f64") for r in ("i32", "i64") for s in "su"]
CONVERTS = {
    **{0xA8 + i + 2 * (r == "i64"): f"{r}.trunc_{f}_{s}" for i, (r, f, s) in enumerate(_TRUNCS)},
    **{0xFC00 + i: f"{r}.trunc_sat_{f}_{s}" for i, (r, f, s) in enumerate(_TRUNCS)},
    **{0xB2 + i + (f == "f64"): f"{f}.convert_{r}_{s}" for i, (r, f, s) in enumerate(_CONVS)},
    0xB6: "f32.demote_f64", 0xBB: "f64.promote_f32",
}  # fmt: skip
CONTROL = (
    0x00,
    0x01,
    0x02,
    0x03,
    0x04,
    0x05,
    0x0B,
    0x0C,
    0x0D,
    0x0E,
    0x0F,
    0x10,
    0x1A,
    0x1B,
    0x12,
    0x13,
    0x14,
    0x15,
    0xD4,
    0xD5,
    0xD6,
)  # tail calls, typed function references
# wide arithmetic (S0008): name and operand count; in a backend's `supported` with its `wide`
WIDEOPS = {0xFC13: ("add128", 4), 0xFC14: ("sub128", 4), 0xFC15: ("mul_wide_s", 2),
           0xFC16: ("mul_wide_u", 2)}  # fmt: skip
TABLES = (0x11, 0x25, 0x26, 0xD0, 0xD1, 0xD2, *range(0xFC08, 0xFC12))  # incl. bulk memory
SUPPORTED = frozenset(
    (*CONTROL, *range(0x20, 0x25), *range(0x41, 0x46), 0x50, *BINOPS, *CMPS, *UNOPS, *LOADS,
     *STORES, *REINTERPRET, *FCMPS, *FUNOPS, *FBINOPS, *CONVERTS, 0x3F, 0x40, *TABLES)
)  # fmt: skip
# abstract heap types a reference may have here: func, extern and their bottom types nofunc,
# noextern (all one word: a record address / host handle, 0 = null)
HEAPS = (-16, -17, -13, -14)


def feature(op: int) -> str:
    """The name an `Unsupported` carries for an opcode this core does not compile (yet)."""
    return f"opcode {op:#x}"


def type_ok(t: Any) -> str | None:
    """None if values of type `t` are compiled here (numbers, function and extern references),
    else the feature."""
    if t in NUM or t in (0x70, 0x6F) or t.__class__ is tuple and (t[1] >= 0 or t[1] in HEAPS):
        return None
    return "simd" if t == 0x7B else "GC types"


def unsupported(mod: ValidModule, ops: frozenset[int]) -> str | None:
    """The first feature of `mod` that a backend compiling the opcodes `ops` cannot, or None. v128
    is compiled where the backend compiles some SIMD operator (S0005)."""
    vec = any(op >= 0xFD000 for op in ops)

    def bad(t: Any) -> str | None:
        return None if vec and t == simd.V128 else type_ok(t)

    if not mod.get("validated"):
        return "module not fully validated"
    if I64 in mod["taddr"]:  # S0009 step 3 removes this
        return "table64"
    imps: list[tuple[Any, ...]] = mod["imports"]
    vts: list[Any] = [t for ps, rs in mod["types"] for t in (*ps, *rs)]
    vts += [g[0] for g in mod["globals"]] + [im[3][0] for im in imps if im[0] == "global"]
    vts += [t for locs, _ in mod["codes"] for t in locs] + list(mod["ttypes"])
    for t in vts:
        if bad(t):
            return bad(t)
    for insns in mod["insns"]:
        for ins in insns:
            op = ins[0]
            if op >= 0xFD000 and not vec:
                return "simd"
            if op not in ops:
                return feature(op)
            ts: Sequence[Any] = ()
            if op in (0x02, 0x03, 0x04) and ins[1][0] != "idx":
                ts = (*ins[1][0], *ins[1][1])
            elif op == 0x1B and len(ins) > 1:  # typed select
                ts = ins[1]
            for t in ts:
                if bad(t):
                    return bad(t)
    return None


@dataclass
class Compiled:
    module: ValidModule
    code: bytes  # the code, then the data area from `data_start` (jump tables)
    data_start: int
    funcs: list[tuple[int, int]]  # (code offset, result slot count) per defined function
    layout: Layout
    records: list[int]  # the function index of each function record (Layout.record)
    sigs: list[int]  # the type index of each signature word (Layout.sig)
    thunks: dict[
        int, int
    ]  # imported function -> code offset of its host thunk (its record if host)
    flags: int = 0  # artifact header flags: guard.BOUNDS_GUARD | GUARD_LOG2 << 8 (S0006)
    faults: list[tuple[int, int]] = field(default_factory=lambda: [])  # S0006 fault map


@dataclass(slots=True)
class _Ctl:
    kind: str  # func block loop if else
    label: Any  # loop: its header; func: the return path
    h: int  # stack height at entry, below the parameters
    nparams: int
    nres: int
    dst: int  # first slot a branch to this label writes its values to
    else_label: Any = None
    used: bool = False


@dataclass
class _Mod:
    b: Backend[Any, Any]
    types: list[tuple[list[Any], list[Any]]]
    ftypes: list[int]  # type index of every function, imports first
    nimp: int
    labels: list[Any]  # of the defined functions
    layout: Layout
    gtypes: list[Any]
    nimpg: int
    records: dict[int, int]  # function index -> record
    sigs: dict[int, int]  # type index -> signature word
    # per memory (imports first): i64 address type, declared minimum in bytes if i64 else 0;
    # per table: i64 index type (D39 amendment B, S0009)
    m64: list[bool] = field(default_factory=list[bool])
    mlo: list[int] = field(default_factory=list[int])
    t64: list[bool] = field(default_factory=list[bool])


def compile_module(mod: ValidModule, b: Backend[Any, Any], target: str) -> Compiled:
    """Compile every function or raise Unsupported with the first missing feature."""
    why = unsupported(mod, b.supported)
    if why:
        raise Unsupported(target, why)
    imps: list[tuple[Any, ...]] = mod["imports"]
    fimps = [im[3] for im in imps if im[0] == "func"]
    gimps = [im[3][0] for im in imps if im[0] == "global"]
    nmem = len(mod["mems"]) + sum(im[0] == "mem" for im in imps)
    # every function a reference can be taken to (the validator checks that ref.func names one
    # of them) and every function import (`call` goes through its record, D30) gets a record;
    # every type call_indirect names a signature word
    inits = [e for _, _, _, items in mod["elem_info"] for e in items]
    inits += [g[2] for g in mod["globals"]] + [e for e in mod["tinit"] if e is not None]
    refd = {e[1] for e in inits if e[0] == "func"} | set(range(len(fimps)))
    refd |= {i[1] for insns in mod["insns"] for i in insns if i[0] == 0xD2}
    sigs = sorted({i[1] for insns in mod["insns"] for i in insns if i[0] in (0x11, 0x13)})
    wide = tuple(len(gimps) + i for i, g in enumerate(mod["globals"]) if g[0] == simd.V128)
    layout = Layout(
        memories=nmem, tables=len(mod["ttypes"]),
        globals=len(gimps) + len(mod["globals"]), imports=len(fimps), sigs=len(sigs),
        records=len(refd), wide=wide,
    )  # fmt: skip
    m = _Mod(
        b, mod["types"], fimps + mod["funcs"], len(fimps), [b.new_label() for _ in mod["funcs"]],
        layout, gimps + [g[0] for g in mod["globals"]], len(gimps),
        {f: r for r, f in enumerate(sorted(refd))}, {t: k for k, t in enumerate(sigs)},
    )  # fmt: skip
    mins = [im[3][0] for im in imps if im[0] == "mem"] + [mt[0] for mt in mod["mems"]]
    m.m64 = [at == I64 for at in mod["maddr"]]
    m.mlo = [mn << 16 if w else 0 for mn, w in zip(mins, m.m64, strict=True)]
    m.t64 = [at == I64 for at in mod["taddr"]]
    nopin = os.environ.get("WABOROS_JIT_NOPIN", "")
    funcs: list[tuple[int, int]] = []
    for k, ((locs, _), insns) in enumerate(zip(mod["codes"], mod["insns"], strict=True)):
        fidx = len(fimps) + k
        pin = not (nopin == "all" or str(fidx) in nopin.split(","))
        pos = b.here()
        funcs.append((pos, simd.width(m.types[m.ftypes[fidx]][1])))
        if not _Func(m, fidx, locs, insns, pin).run(False):  # a branch does not reach
            b.rewind(pos)
            far_ok = _Func(m, fidx, locs, insns, pin).run(True)  # never inside an assert (-O)
            if not far_ok:
                raise Unsupported(target, "function too large for this architecture's branches")
    thunks = {i: b.host_thunk(layout.import_(i)) for i in sorted(refd) if i < len(fimps)}
    code, data = b.finish()
    return Compiled(mod, code, data, funcs, layout, sorted(refd), sigs, thunks)


class _Func:
    def __init__(self, m: _Mod, fidx: int, locs: list[Any], insns: list[Any], pin: bool) -> None:
        self.m, self.b, self.insns = m, m.b, insns
        ps, self.results = m.types[m.ftypes[fidx]]
        self.nparams = len(ps)
        self.ltypes: list[Any] = [*ps, *locs]
        self.slot = [0]  # slot of each local (a v128 takes two), then the count of slots
        for t in self.ltypes:
            self.slot.append(self.slot[-1] + 1 + (t == simd.V128))
        self.L = self.slot.pop()
        self.label = m.labels[fidx - m.nimp]
        self.TOS = Reg(self.b.tos)
        self.pins = self.choose_pins() if pin else {}
        self.st: list[Entry] = []
        self.ctl: list[_Ctl] = []
        self.maxh = 0
        self.dead = False
        self.dead_depth = 0
        self.tos_taken = False  # an operand of the current instruction is in `tos` (LESSONS 32)
        self.f: Any = None  # the backend's frame, from run()

    def choose_pins(self) -> dict[int, int]:
        """Integer locals by uses weighted by loop depth (as w2jit2), as many as the pool holds."""
        w: Counter[int] = Counter()
        depth = 0
        kinds: list[int] = []
        for ins in self.insns:
            op = ins[0]
            if op in (0x02, 0x03, 0x04):
                kinds.append(op)
                depth += op == 0x03
            elif op == 0x0B and kinds:
                depth -= kinds.pop() == 0x03
            elif op in (0x20, 0x21, 0x22) and self.ltypes[ins[1]] in (I32, I64):
                w[ins[1]] += 8 ** min(depth, 5)
        pool = self.b.pool
        return {j: pool[k] for k, j in enumerate(sorted(w, key=lambda j: -w[j])[: len(pool)])}

    # ------------------------------------------------------------------ the lazy stack
    def loc(self, j: int) -> Reg | Slot:
        r = self.pins.get(j)
        return Slot(self.slot[j]) if r is None else Reg(r)

    def opnd(self, e: Entry, i: int) -> Operand:
        k = e[0]
        if k == CONST:
            return Imm(e[1])
        if k == LOCAL:
            return self.loc(e[1])
        if k == TOS:
            return self.TOS
        assert k == SLOT
        return Slot(self.L + i)

    def opnd_k(self, e: Entry, i: int) -> Operand | AddK:
        """`opnd`, an ADDK entry as an `AddK` (only for `move` and a load/store address)."""
        if e[0] == ADDK:
            return AddK(self.pins[e[1][0]], e[1][1])
        return self.opnd(e, i)

    def push(self, e: Entry) -> None:
        st = self.st
        if st and st[-1][0] == CMP:
            self.uncmp()
        st.append(e)
        if len(st) > self.maxh:
            self.maxh = len(st)

    def uncmp(self) -> None:
        """The pending compare on top becomes a value in `tos`."""
        cond, x, y, w = self.st.pop()[1]
        self.spill_tos()
        self.b.cmp_set(cond, self.TOS, x, y, w)
        self.st.append((TOS, None))

    def spill_tos(self) -> None:
        for i, e in enumerate(self.st):
            if e[0] == TOS:
                self.b.move(Slot(self.L + i), self.TOS, True)
                self.st[i] = SLOTE

    def need_tos(self) -> None:
        """Before an operation writes `tos`: nothing on the stack may still need it."""
        if self.st and self.st[-1][0] == CMP:
            self.uncmp()
        self.spill_tos()

    def pop_k(self) -> Operand | AddK:
        """The top operand; an ADDK entry stays an `AddK` (for `move` or a load/store address)."""
        st = self.st
        if st[-1][0] == CMP:
            self.uncmp()
        e = st.pop()
        if e[0] == TOS:
            self.tos_taken = True
        return self.opnd_k(e, len(st))

    def pop(self) -> Operand:
        """The top operand; an ADDK entry is computed into `tos`, or into its own slot when an
        operand of this instruction is in `tos` already (LESSONS 32, S0007 `tos_taken`)."""
        x = self.pop_k()
        if not isinstance(x, AddK):
            return x
        dst: Reg | Slot = Slot(self.L + len(self.st))
        if not self.tos_taken:
            self.spill_tos()
            dst, self.tos_taken = self.TOS, True
        self.b.move(dst, x, False)
        return dst

    def pop_cond(self) -> tuple[str, Operand, Operand, bool]:
        if self.st[-1][0] == CMP:
            c = self.st.pop()[1]
            if self.TOS in (c[1], c[2]):
                self.tos_taken = True
            return c
        return ("ne", self.pop(), Imm(0), False)

    def flush(self) -> None:
        """Everything into its slot: before calls, branches and control-flow merges."""
        st = self.st
        if st and st[-1][0] == CMP:
            self.uncmp()
        for i, e in enumerate(st):
            if e[0] != SLOT:
                self.b.move(Slot(self.L + i), self.opnd_k(e, i), e[0] != ADDK)
                st[i] = SLOTE

    def set_local(self, j: int, tee: bool) -> None:
        src = self.pop_k()
        dst = self.loc(j)
        for i, e in enumerate(self.st):  # earlier reads of local j keep the old value
            if e[0] == LOCAL and e[1] == j or e[0] == ADDK and e[1][0] == j:
                self.b.move(Slot(self.L + i), self.opnd_k(e, i), e[0] == LOCAL)
                self.st[i] = SLOTE
        if src != dst:
            self.b.move(dst, src, self.ltypes[j] in WIDE)
        if tee:
            self.push((LOCAL, j))

    def fold_addk(self, sub: bool) -> bool:
        """i32.add/sub of a pinned local (or an ADDK of one) and a constant becomes one ADDK
        entry (S0007 K); False if the operands are not of that shape."""
        st = self.st
        x, y = st[-2], st[-1]
        if not sub and x[0] == CONST:
            x, y = y, x
        if y[0] != CONST:
            return False
        if x[0] == LOCAL and x[1] in self.pins:
            j, k = x[1], 0
        elif x[0] == ADDK:
            j, k = x[1]
        else:
            return False
        st[-2:] = [(ADDK, (j, (k - y[1] if sub else k + y[1]) & M32))]
        return True

    # ------------------------------------------------------------------ control
    def arity(self, c: _Ctl) -> int:
        return c.nparams if c.kind == "loop" else c.nres

    def needs_moves(self, c: _Ctl) -> bool:
        n = self.arity(c)
        return n > 0 and self.L + len(self.st) - n != c.dst

    def moves(self, c: _Ctl) -> None:
        """The branch values (on top, in slots) to the label's slots; sources are never below
        their destinations, so ascending order is safe."""
        n = self.arity(c)
        src = self.L + len(self.st) - n
        for j in range(n):
            if src + j != c.dst + j:
                self.b.move(Slot(c.dst + j), Slot(src + j), True)

    def block_type(self, bt: Any) -> tuple[list[Any], list[Any]]:
        if bt[0] == "idx":
            idx: int = bt[1]
            return self.m.types[idx]
        return bt

    def enter(self, op: int, bt: Any) -> None:
        b = self.b
        ps, rs = self.block_type(bt)
        cond = self.pop_cond() if op == 0x04 else None
        self.flush()
        h = len(self.st) - simd.width(ps)
        kind = {0x02: "block", 0x03: "loop", 0x04: "if"}[op]
        c = _Ctl(kind, b.new_label(), h, simd.width(ps), simd.width(rs), self.L + h)
        if cond is not None:
            c.else_label = b.new_label()
            b.cmp_branch(NEGATE[cond[0]], cond[1], cond[2], c.else_label, cond[3])
        elif op == 0x03:
            b.bind(c.label)
            b.poll(self.f)
        self.ctl.append(c)

    def else_(self) -> None:
        c = self.ctl[-1]
        if not self.dead:
            self.flush()
            self.b.jump(c.label)
            c.used = True
        self.b.bind(c.else_label)
        c.kind, self.dead = "else", False
        del self.st[c.h :]
        self.st += [SLOTE] * c.nparams

    def ret(self, c: _Ctl) -> None:
        """The results straight into slots 0..n (S0007, "Returns"). The entries below them are
        not flushed: each is a value already computed or a compare, so nothing can trap (LESSONS
        28). Several results fall back to flush + moves when a source is a slot in 0..n other
        than its own (writing one result would overwrite another's source)."""
        st, n = self.st, c.nres
        if n and st[-1][0] == CMP:
            self.uncmp()
        base = len(st) - n
        srcs = [self.opnd_k(st[base + k], base + k) for k in range(n)]
        if any(isinstance(s, Slot) and s.k < n and s.k != k for k, s in enumerate(srcs)):
            self.flush()
            self.moves(c)
            return
        for k, s in enumerate(srcs):  # never writes tos, a pin or a source slot
            self.b.move(Slot(k), s, not isinstance(s, AddK))

    def end(self) -> None:
        c = self.ctl.pop()
        if c.kind == "func":
            if not self.dead:
                self.ret(c)
            self.b.bind(c.label)
            self.b.ret_ok(self.f)
            return
        if not self.dead:
            self.flush()
        if c.kind == "if":  # no else: the false path arrives with the parameters as results
            self.b.bind(c.else_label)
        if c.kind != "loop":
            self.b.bind(c.label)
        self.dead = self.dead and not (c.kind == "if" or (c.kind != "loop" and c.used))
        del self.st[c.h :]
        self.st += [SLOTE] * c.nres
        self.maxh = max(self.maxh, len(self.st))

    def br(self, depth: int) -> None:
        c = self.ctl[-1 - depth]
        if c.kind == "func":  # return: br_if and br_table to it are unchanged (S0007 Q7)
            self.ret(c)
        else:
            self.flush()
            self.moves(c)
        self.b.jump(c.label)
        c.used, self.dead = True, True

    def br_if(self, depth: int) -> None:
        b = self.b
        cond, x, y, w = self.pop_cond()
        self.flush()
        c = self.ctl[-1 - depth]
        c.used = True
        if not self.needs_moves(c):
            b.cmp_branch(cond, x, y, c.label, w)
            return
        skip = b.new_label()
        b.cmp_branch(NEGATE[cond], x, y, skip, w)
        self.moves(c)
        b.jump(c.label)
        b.bind(skip)

    def br_table(self, depths: Sequence[int], default: int) -> None:
        b = self.b
        index = self.pop()
        self.flush()
        stubs: dict[int, tuple[_Ctl, Any]] = {}

        def target(d: int) -> Any:
            c = self.ctl[-1 - d]
            c.used = True
            if not self.needs_moves(c):
                return c.label
            if d not in stubs:
                stubs[d] = (c, b.new_label())
            return stubs[d][1]

        b.jump_table(index, [target(d) for d in depths], target(default))
        for c, label in stubs.values():
            b.bind(label)
            self.moves(c)
            b.jump(c.label)
        self.dead = True

    def call(self, fi: int) -> None:
        m, b = self.m, self.b
        ps, rs = m.types[m.ftypes[fi]]
        n, nres = simd.width(ps), simd.width(rs)
        self.flush()
        h0 = len(self.st) - n
        del self.st[h0:]
        if fi < m.nimp:  # its record: another instance's function, or the host thunk
            b.call_record(self.f, m.layout.record(m.records[fi]), self.L + h0)
        else:
            b.call_direct(self.f, m.labels[fi - m.nimp], self.L + h0)
        self.st += [SLOTE] * nres
        self.maxh = max(self.maxh, h0 + max(n, nres))

    def call_indirect(self, ti: int, table: int) -> None:
        m, b = self.m, self.b
        ps, rs = m.types[ti]
        n, nres = simd.width(ps), simd.width(rs)
        index = self.pop()  # stays valid across flush (br_table does the same)
        self.flush()
        h0 = len(self.st) - n
        del self.st[h0:]
        lay, a64 = m.layout, self.t64(table)
        b.call_indirect(self.f, lay.table(table), lay.sig(m.sigs[ti]), index, self.L + h0, a64=a64)
        self.st += [SLOTE] * nres
        self.maxh = max(self.maxh, h0 + max(n, nres))

    def call_ref(self, ti: int) -> None:
        ps, rs = self.m.types[ti]
        n, nres = simd.width(ps), simd.width(rs)
        ref = self.pop()  # stays valid across flush, as call_indirect's index
        self.flush()
        h0 = len(self.st) - n
        del self.st[h0:]
        self.b.call_ref(self.f, ref, self.L + h0)
        self.st += [SLOTE] * nres
        self.maxh = max(self.maxh, h0 + max(n, nres))

    def tail_call(self, op: int, ins: Any) -> None:
        """return_call / return_call_indirect / return_call_ref: the arguments become the
        callee's frame at our frame base, our native frame is dropped, then a jump (constant
        stacks, S0003 3e). An import is jumped to through its record, as return_call_ref does."""
        m, b = self.m, self.b
        fi: int = ins[1]
        ti = m.ftypes[fi] if op == 0x12 else fi
        n = simd.width(m.types[ti][0])
        x = None if op == 0x12 else self.pop()  # the index or the reference
        self.flush()
        h0 = len(self.st) - n
        src = self.L + h0
        if op == 0x12 and fi < m.nimp:
            b.tail_record(self.f, m.layout.record(m.records[fi]), src, n)
        elif op == 0x12:
            b.tail_direct(self.f, m.labels[fi - m.nimp], src, n)
        elif op == 0x13:
            assert x is not None
            lay = m.layout
            t, a64 = lay.table(ins[2]), self.t64(ins[2])
            b.tail_indirect(self.f, t, lay.sig(m.sigs[ti]), x, src, n, a64=a64)
        else:
            assert x is not None
            b.tail_ref(self.f, x, src, n)
        del self.st[h0:]
        self.dead = True

    def br_on_null(self, depth: int, on_null: bool) -> None:
        """br_on_null: the reference is dropped on the branch, kept otherwise; br_on_non_null:
        it is the last branch value, dropped otherwise. Values are in slots first (flush)."""
        b = self.b
        self.flush()
        ref = Slot(self.L + len(self.st) - 1)
        if on_null:
            self.st.pop()
        c = self.ctl[-1 - depth]
        c.used = True
        cond = "eq" if on_null else "ne"
        if not self.needs_moves(c):
            b.cmp_branch(cond, ref, Imm(0), c.label, True)
        else:
            skip = b.new_label()
            b.cmp_branch(NEGATE[cond], ref, Imm(0), skip, True)
            self.moves(c)
            b.jump(c.label)
            b.bind(skip)
        if on_null:
            self.st.append(SLOTE)
        else:
            self.st.pop()

    def service(self, j: int, nargs: int, imms: Sequence[int], nres: int) -> None:
        """A runtime service through the host bridge (D25): its operands, then the immediates,
        in slots; results replace the operands."""
        self.flush()
        h0 = len(self.st) - nargs
        for k, v in enumerate(imms):
            self.b.move(Slot(self.L + h0 + nargs + k), Imm(v), True)
        self.b.call_host(self.f, self.m.layout.libcall(j), self.L + h0)
        del self.st[h0:]
        self.st += [SLOTE] * nres
        self.maxh = max(self.maxh, h0 + max(nargs + len(imms), nres))

    def mem_imm(self, mi: int) -> list[int]:
        """The memory immediate of memory.grow / memory.init: written only when the module has
        several memories (a one-memory module's code stays as it was; S0003)."""
        return [mi] if self.m.layout.memories > 1 else []

    def a64(self, mi: int) -> tuple[bool, int]:
        """(i64 address type, declared minimum in bytes if i64 else 0) of memory `mi`."""
        return self.m.m64[mi], self.m.mlo[mi]

    def t64(self, ti: int) -> bool:
        return self.m.t64[ti]

    def pop3(self) -> tuple[Operand, Operand, Operand]:
        """Three operands of a bulk operation; `tos` is free afterwards (the backend uses it)."""
        z = self.pop()
        y = self.pop()
        x = self.pop()
        self.need_tos()
        return x, y, z

    # ------------------------------------------------------------------ the pass
    def run(self, far: bool) -> bool:
        """Compile the function; False if the backend asks for far mode (S0004)."""
        b, L, lay = self.b, self.L, self.m.layout
        self.f = f = b.func_begin(self.label, tuple(self.pins.values()), far)
        for j, r in self.pins.items():
            src = Slot(self.slot[j]) if j < self.nparams else Imm(0)
            b.move(Reg(r), src, self.ltypes[j] in WIDE)
        for j in range(self.nparams, len(self.ltypes)):
            if j not in self.pins:
                for k in range(self.slot[j], self.slot[j] + 1 + (self.ltypes[j] == simd.V128)):
                    b.move(Slot(k), Imm(0), True)
        self.ctl.append(_Ctl("func", b.new_label(), 0, 0, simd.width(self.results), 0))
        tos, push, pop = self.TOS, self.push, self.pop
        for ins in self.insns:
            op = ins[0]
            self.tos_taken = False
            if self.dead:  # skip to the end (or else) of the block that became unreachable
                if op in (0x02, 0x03, 0x04):
                    self.dead_depth += 1
                elif op == 0x0B:
                    if self.dead_depth:
                        self.dead_depth -= 1
                    else:
                        self.end()
                elif op == 0x05 and not self.dead_depth:
                    self.else_()
                continue
            if op == 0x20:
                if self.ltypes[ins[1]] == simd.V128:
                    simd.get_local(self, ins[1])
                else:
                    push((LOCAL, ins[1]))
            elif (op == 0x6A or op == 0x6B) and self.fold_addk(op == 0x6B):
                pass
            elif op in BINOPS:
                name, w = BINOPS[op]
                y = pop()
                x = pop()
                self.need_tos()
                if name in ("div_s", "div_u", "rem_s", "rem_u"):
                    b.divrem(f, name, tos, x, y, w)
                else:
                    b.binop(name, tos, x, y, w)
                push((TOS, None))
            elif op == 0x41 or op == 0x42:
                push((CONST, ins[1]))
            elif op in CMPS:
                name, w = CMPS[op]
                y = pop()
                x = pop()
                push((CMP, (name, x, y, w)))
            elif op in (0x21, 0x22):
                if self.ltypes[ins[1]] == simd.V128:
                    simd.set_local(self, ins[1], op == 0x22)
                else:
                    self.set_local(ins[1], op == 0x22)
            elif op in LOADS:
                size, signed, w = LOADS[op]
                addr = self.pop_k()
                self.need_tos()
                a64, lo = self.a64(ins[3])
                b.load(f, size, signed, w, tos, addr, ins[1], lay.memory(ins[3]), a64=a64, lo=lo)
                push((TOS, None))
            elif op in STORES:
                v = pop()
                addr = self.pop_k()
                a64, lo = self.a64(ins[3])
                b.store(f, STORES[op], addr, ins[1], v, lay.memory(ins[3]), a64=a64, lo=lo)
            elif op == 0x45 or op == 0x50:
                if self.st[-1][0] == CMP:
                    cond, x, y, w = self.st[-1][1]
                    self.st[-1] = (CMP, (NEGATE[cond], x, y, w))
                else:
                    push((CMP, ("eq", pop(), Imm(0), op == 0x50)))
            elif op in UNOPS:
                name, w = UNOPS[op]
                x = pop()
                self.need_tos()
                b.unop(name, tos, x, w)
                push((TOS, None))
            elif op in (0x02, 0x03, 0x04):
                self.enter(op, ins[1])
            elif op == 0x0B:
                self.end()
            elif op == 0x0D:
                self.br_if(ins[1])
            elif op == 0x0C:
                self.br(ins[1])
            elif op == 0x05:
                self.else_()
            elif op == 0x10:
                self.call(ins[1])
            elif op == 0x23 or op == 0x24:
                g = ins[1]
                word, indirect = self.m.layout.global_(g), g < self.m.nimpg
                if self.m.gtypes[g] == simd.V128:
                    simd.global_get(self, op, word, indirect)
                elif op == 0x23:
                    self.need_tos()
                    b.ctx_load(tos, word, indirect)
                    push((TOS, None))
                else:
                    b.ctx_store(word, pop(), indirect)
            elif op == 0x1B and len(ins) > 1 and ins[1][0] == simd.V128:
                simd.select(self)
            elif op == 0x1B:  # both values are computed already; 64-bit moves suit every type
                cond, x, y, w = self.pop_cond()
                e = pop()
                t = pop()
                self.need_tos()
                b.select_cmp(cond, x, y, w, tos, t, e, True)
                push((TOS, None))
            elif op == 0x1A and len(ins) > 1:  # annotated: a v128 (S0005)
                simd.drop(self)
            elif op == 0x1A:
                self.st.pop()  # every entry is a computed value or a compare: nothing can trap
            elif op == 0x0E:
                self.br_table(ins[1], ins[2])
            elif op == 0x0F:
                self.br(len(self.ctl) - 1)
            elif op == 0x43:
                push((CONST, ins[1]))
            elif op == 0x44:
                push((CONST, ins[1]))
            elif op in FBINOPS:
                name, w = FBINOPS[op]
                y = pop()
                x = pop()
                self.need_tos()
                b.fbinop(name, tos, x, y, w)
                push((TOS, None))
            elif op in FUNOPS:
                name, w = FUNOPS[op]
                x = pop()
                self.need_tos()
                b.funop(name, tos, x, w)
                push((TOS, None))
            elif op in FCMPS:  # a value, not a pending compare (no fused float branch yet)
                name, w = FCMPS[op]
                y = pop()
                x = pop()
                self.need_tos()
                b.fcmp_set(name, tos, x, y, w)
                push((TOS, None))
            elif op in CONVERTS:
                x = pop()
                self.need_tos()
                b.convert(f, CONVERTS[op], tos, x)
                push((TOS, None))
            elif op == 0x3F:
                self.need_tos()
                b.memory_size(tos, lay.memory(ins[1]))
                push((TOS, None))
            elif op == 0x40:  # delta (+ memory if several) -> old pages or -1
                self.service(MEMORY_GROW, 1, self.mem_imm(ins[1]), 1)
            elif op == 0x00:
                b.trap(f, TrapCode.UNREACHABLE)
                self.dead = True
            elif op == 0x11:
                self.call_indirect(ins[1], ins[2])
            elif op in (0x12, 0x13, 0x15):
                self.tail_call(op, ins)
            elif op == 0x14:
                self.call_ref(ins[1])
            elif op == 0xD4:  # a reference is never a pending compare
                b.null_check(f, self.opnd(self.st[-1], len(self.st) - 1), TrapCode.NULL_REFERENCE)
            elif op == 0xD5 or op == 0xD6:
                self.br_on_null(ins[1], op == 0xD5)
            elif op == 0xD0:
                push((CONST, 0))
            elif op == 0xD1:
                push((CMP, ("eq", pop(), Imm(0), True)))
            elif op == 0xD2:
                self.need_tos()
                b.ctx_addr(tos, self.m.layout.record(self.m.records[ins[1]]))
                push((TOS, None))
            elif op == 0x25:
                x = pop()
                self.need_tos()
                b.table_get(f, tos, self.m.layout.table(ins[1]), x, a64=self.t64(ins[1]))
                push((TOS, None))
            elif op == 0x26:
                v = pop()
                x = pop()
                self.need_tos()
                b.table_set(f, self.m.layout.table(ins[1]), x, v, a64=self.t64(ins[1]))
            elif op == 0xFC10:
                self.need_tos()
                b.ctx_load(tos, self.m.layout.table(ins[1]) + 1, False)
                push((TOS, None))
            elif op == 0xFC11:
                b.table_fill(f, lay.table(ins[1]), *self.pop3(), a64=self.t64(ins[1]))
            elif op == 0xFC0E:
                a2 = (self.t64(ins[1]), self.t64(ins[2]))
                b.table_copy(f, lay.table(ins[1]), lay.table(ins[2]), *self.pop3(), a64=a2)
            elif op == 0xFC0B:
                b.memory_fill(f, *self.pop3(), lay.memory(ins[1]), a64=self.a64(ins[1])[0])
            elif op == 0xFC0A:
                a2 = (self.a64(ins[1])[0], self.a64(ins[2])[0])
                b.memory_copy(f, *self.pop3(), lay.memory(ins[1]), lay.memory(ins[2]), a64=a2)
            elif op == 0xFC0F:  # (init, delta) + table -> old size or -1
                self.service(TABLE_GROW, 2, [ins[1]], 1)
            elif op == 0xFC0C:  # (d, s, n) + segment, table
                self.service(TABLE_INIT, 3, [ins[1], ins[2]], 0)
            elif op == 0xFC0D:
                self.service(ELEM_DROP, 0, [ins[1]], 0)
            elif op == 0xFC08:  # (d, s, n) + segment (+ memory if several)
                self.service(MEMORY_INIT, 3, [ins[1], *self.mem_imm(ins[2])], 0)
            elif op == 0xFC09:
                self.service(DATA_DROP, 0, [ins[1]], 0)
            elif op in REINTERPRET or op == 0x01:
                pass
            elif op in WIDEOPS:  # two i64 results, low first, into the operands' first slots
                name, n = WIDEOPS[op]
                args = [pop() for _ in range(n)][::-1]
                self.need_tos()
                b.wide(name, Slot(L + len(self.st)), tuple(args))
                push(SLOTE)
                push(SLOTE)
            elif op >= 0xFD000:
                simd.op(self, ins)
            else:  # unsupported() already refused it
                raise AssertionError(f"opcode {op:#x}")
        assert not self.ctl
        return b.func_end(f, L + self.maxh)
