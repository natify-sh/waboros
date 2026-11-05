# pyright: basic, reportArgumentType=false, reportOptionalMemberAccess=false, reportAttributeAccessIssue=false, reportSelfClsParameterName=false
"""Wasm -> Python source (ported from poc/w2py.py): operand stack elimination and structured control flow."""

import os
import struct

from waboros.frontend.binary import F32, F64, I32, I64, M32, M64
from waboros.frontend.validate import valtype_key
from waboros.targets.base import Unsupported
from waboros.targets.pyc import runtime as _rt

MAXPAGES64 = (1 << 48) - 1  # the byte size of an i64 memory must fit 64 bits (as in Wasmtime)

BRTREE = int(
    os.environ.get("WABOROS_BT", "4")
)  # br_table: balanced if-tree above this many runs, 0 = chain
# S0010: `_br` rules, comma-separated: `sink` (case regions moved into br_table leaves), `dse` (stores
# of `_br` evidently dead at emission); "0" or empty = the emission before S0010, byte for byte
BRRULES = frozenset(
    r for r in os.environ.get("WABOROS_PYC_BR", "sink,dse").split(",") if r and r != "0"
)
SINK_COPY = 10  # a region copied into n leaves: lines * (n - 1) at most this (S0010 Q2)
SINK_MAXIND = 90  # no moved line deeper than this many levels (CPython allows 100, LESSONS 22)


class E:
    """Symbolic stack value.  b: inclusive upper bound if value is known to lie in [0, b]
    (i.e. it is a canonical unsigned int), None if it may be out of range (needs masking).
    For floats b is unused.  Ls/m/g: what state the expression reads."""

    __slots__ = ("c", "t", "b", "Ls", "m", "g", "k", "w", "x")

    def __init__(s, c, t, b=None, Ls=(), m=False, g=(), k=False, w=None, x=False):
        s.c, s.t, s.b, s.Ls, s.m, s.g, s.k, s.w = c, t, b, frozenset(Ls), m, frozenset(g), k, w
        s.x = x  # may trap by arithmetic (div/rem/trunc): must be evaluated even if discarded
        # w: for a dirty value, 'up' = lies in [0, 2*MOD) / 'down' = lies in (-MOD, MOD):
        #    at most one wrap, so a compare+adjust replaces the (slow) & mask

    @property
    def pure_const(s):
        return not s.Ls and not s.m and not s.g


def dep(*es, **kw):
    Ls, m, g, x = set(), False, set(), False
    for e in es:
        Ls |= e.Ls
        m = m or e.m
        g |= e.g
        x = x or e.x
    return dict(
        Ls=Ls, m=m or kw.get("m", False), g=g | set(kw.get("g", ())), x=x or kw.get("x", False)
    )


def flit(bits, fb=False, w=64):
    """Source for an f32 (w=32) / f64 constant given as its bit pattern (the front end's form)."""
    if fb:  # f32/f64 carried as int bit patterns (ModGen.fb)
        return str(bits)
    if w == 32:  # a float; a NaN is the double NaN with the same sign and payload << 29
        v = struct.unpack("<f", struct.pack("<I", bits))[0]
        if v != v:
            bits = (bits & 0x80000000) << 32 | 0x7FF0000000000000 | (bits & 0x7FFFFF) << 29
            v = struct.unpack("<d", struct.pack("<Q", bits))[0]
    else:
        v = struct.unpack("<d", struct.pack("<Q", bits))[0]
    if v != v:
        return "_f64_i64(%#x)" % struct.unpack("<Q", struct.pack("<d", v))[0]
    if v in (float("inf"), float("-inf")):
        return "INF" if v > 0 else "(-INF)"
    r = repr(v)
    return "(%s)" % r if r.startswith("-") else r


class Ctl:
    __slots__ = (
        "kind",
        "mode",
        "id",
        "res",
        "rv",
        "h",
        "pyloop",
        "nest",
        "esc",
        "has_else",
        "live_then",
        "mark",
        "nests",
        "tg",
        "pv",
        "pin",
        "narr",
        "nbk",
        "zx",
        "leaves",
        "caps",
        "cl",
    )

    def __init__(s, kind, mode, id_, res, rv, h):
        s.kind, s.mode, s.id, s.res, s.rv, s.h = kind, mode, id_, res, rv, h
        s.pyloop = mode == "loop"
        s.nests = []
        s.tg = False
        s.nest = 0
        s.esc = set()
        s.has_else = False
        s.live_then = False
        s.mark = None
        s.pv = []  # python-loop parameter variables (assigned by a br to the loop)
        s.pin = []  # block parameters as they stand on entry (restored for `else`)
        # S0010 (only with BRRULES): branches arriving without a python jump; `_br = id` stores with
        # `break`; a structural exit to this frame with `_br == 0`; leaf records; pending captures
        s.narr = s.nbk = 0
        s.zx = False
        s.leaves = []
        s.caps = []
        s.cl = ()  # try_table: its catch clauses


# ----------------------------------------------------------------------------- function compiler
class FuncGen:
    def __init__(s, mg, fidx, tidx, locs, ins):
        s.mg = mg
        s.fidx = fidx
        s.params, s.results = mg.types[tidx]
        s.ltypes = list(s.params) + locs
        s.ins = ins
        s.fls = {}  # fb mode: (type, bits source) -> the float source it came from (fv/fr)
        s.lines = []
        s.ind = 1
        s.stack = []
        s.tmp = 0
        s.dead = False
        s.globals_set = set()
        s.uses_br = False
        s.ctl = []
        s.tmpty = {}
        s.rvty = {}
        s.uses_w = set()
        s.br = BRRULES  # read once per function (tests change the module global)
        s.recs = []  # S0010 position records [pos, kind, ...]; pos -1 = gone
        s.stores = {}  # frame id -> store records (`_br = id` without break)
        s.dse_on = "dse" in s.br

    # --- emit helpers
    def emit(s, line):
        s.lines.append("    " * s.ind + line)

    def open(s, line):
        s.emit(line)
        s.ind += 1
        return len(s.lines)

    def close(s, mark):
        if len(s.lines) == mark:
            s.emit("pass")
        s.ind -= 1

    # --- S0010: records of positions in s.lines, kept true when lines are spliced
    def rec(s, pos, kind, *a):
        r = [pos, kind, *a]
        s.recs.append(r)
        return r

    def marks_before(s, pos, open_):
        """no open frame (or open nest) began after pos: `close` compares lines with marks, which a
        splice at pos does not shift"""
        return all(
            (F.mark is None or F.mark <= pos) and all(m <= pos for m in F.nests) for F in open_
        )

    def fill(s, pos):
        """after lines were removed at pos: a suite that opened just before pos and lost all its lines
        (a closed nest's `pass` may be what went) gets `pass` back"""
        if pos == 0:
            return
        head = s.lines[pos - 1]
        k = len(head) - len(head.lstrip(" "))
        nxt = s.lines[pos] if pos < len(s.lines) else ""
        if head.endswith(":") and len(nxt) - len(nxt.lstrip(" ")) <= k:
            s.splice(pos, 0, [" " * (k + 4) + "pass"])

    def splice(s, at, n, new):
        """replace lines[at:at+n] by new; records inside the replaced lines are gone, later ones shift"""
        s.lines[at : at + n] = new
        d, end, gone = len(new) - n, at + n, False
        for r in s.recs:
            p = r[0]
            if p >= end:
                r[0] = p + d
            elif p >= at:
                r[0] = -1
                gone = True
        if gone:
            s.recs = [r for r in s.recs if r[0] >= 0]

    def sink(s, cap, may0):
        """S0010: move (or copy) the case region of T, emitted after T's end up to the end of its
        parent (the current frame, nests closed), into the br_table leaves that are T's only arrivals"""
        T, g, ge, b, bind = cap
        lv = T.leaves
        if g[0] < 0 or ge[0] < 0 or b[0] < 0 or any(r[0] < 0 for r in lv):
            return
        loop = next((F for F in reversed(s.ctl) if F.pyloop), None)
        # the same python loop (a correctness rule, S0010): a leaf that needed `break` is not recorded
        for r in lv:
            assert r[4] is loop, "S0010 sink: a leaf and its region in different python loops"
        if may0 and any(r[5][0] != g[0] for r in lv):
            return  # falling through with 0 must reach the code that followed the region
        pre = "    " * bind
        body = s.lines[b[0] :]
        if all(ln == pre + "pass" for ln in body):
            body = []
        if len(lv) > 1 and len(body) * (len(lv) - 1) > SINK_COPY:
            return
        bp = b[0]
        inside = [r for r in s.recs if r[0] >= bp and r[1] not in ("cg", "ce", "cb")]
        if len(lv) > 1 and any(r[1] == "lf" or r[1] == "te" for r in inside):
            return
        top = max(r[3] for r in lv) - bind  # the region moves by up to this many levels
        if body and top + max(map(len, body)) // 4 > SINK_MAXIND:  # a line could pass the limit
            deep = max(len(ln) - len(ln.lstrip(" ")) for ln in body) // 4
            if top + deep > SINK_MAXIND:
                return
        text = "\n" + "\n".join(body) if body else ""
        # every line is indented as the region (or deeper), and no line holds a newline
        assert text.count("\n" + pre) == len(body) == text.count("\n"), "S0010 sink: region lines"
        rel = [(r, r[0] - b[0]) for r in inside if r[0] < len(s.lines)]
        # every frame opened after the dispatch tree is closed (the current one's nests too)
        lo = min(r[0] for r in lv)
        assert s.marks_before(lo, s.ctl[:-1]) and (s.ctl[-1].mark or 0) <= lo, "S0010 sink: marks"
        first = True
        for lf in sorted(lv, key=lambda r: -r[0]):
            pos, ind = lf[0], lf[3]
            sp = "    " * ind
            new = text.replace("\n" + pre, "\n" + sp).split("\n")[1:] if body else []
            if lf[6]:  # a store leaf: its `_br = K` line is replaced
                assert s.lines[pos] == sp + "_br = %d" % T.id, (s.lines[pos], T.id)
                n = 1
                if not new:
                    new = [sp + "pass"]
            else:  # depth 0: nothing was emitted; `close` may have written `pass`
                n = 1 if pos < len(s.lines) and s.lines[pos] == sp + "pass" else 0
                if not n:  # inserted at the end of the leaf's arm: after its header or a statement
                    prev = s.lines[pos - 1]
                    k = (len(prev) - len(prev.lstrip(" "))) // 4
                    assert k >= ind or k == ind - 1 and prev.endswith(":"), (prev, ind)
                    nxt = s.lines[pos] if pos < len(s.lines) else ""
                    assert (len(nxt) - len(nxt.lstrip(" "))) // 4 <= ind, (nxt, ind)
                if n and not new:
                    continue
            s.splice(pos, n, new)
            for r, off in rel:
                if first:
                    r[0] = pos + off
                    if r[1] == "lf":  # a leaf inside the region moves with it: so does its depth
                        r[3] += ind - bind
                else:
                    c = s.rec(pos + off, *r[1:])
                    if r[1] == "st":
                        s.stores.setdefault(r[2], []).append(c)
            first = False
        for r in lv:
            r[0] = -1
        # What goes: T's guard (dead: every `_br = T` store was a leaf, now replaced) and the region.
        # Between them, propagate(T) routed the escapes that pass through T. When P is not a python
        # loop it emitted only `if not _br:`, the region's own guard: those escapes are in P.esc and
        # P's end routes them, after this cut. When P is a python loop it emitted live routing
        # (`if _br: ... continue / break`) for escapes from python loops inside T: it stays.
        gp, keep = (b[0], []) if s.ctl[-1].pyloop else (g[0], [])
        del s.lines[gp:]
        for r in s.recs:
            p = r[0]
            if p > gp or p == gp and r[1] != "te":  # a tree end may be the new end of lines
                r[0] = -1
            elif p >= 0:
                keep.append(r)
        s.recs = keep
        s.fill(gp)
        if s.ctl[-1].pyloop and ge[0] > g[0]:
            at = g[0]
            s.splice(at, ge[0] - at, [])
            s.fill(at)

    def dse(s, F):
        """S0010: a `_br = F.id` store with no test of `_br` after it reaches F's end, where `_br`
        becomes 0, without being read: it is dead (no python loop lies between, or it had `break`)"""
        sts = [r for r in s.stores.pop(F.id, ()) if r[0] >= 0]
        if not sts:
            return
        lo, last, L = min(r[0] for r in sts), -1, s.lines
        for i in range(len(L) - 1, lo, -1):  # the last line that tests `_br`, if after a store
            ln = L[i]
            if "_br" in ln and not ln.lstrip().startswith("_br ="):
                last = i
                break
        dead = sorted((r for r in sts if r[0] > last), key=lambda r: -r[0])
        assert not dead or s.marks_before(dead[-1][0], s.ctl), "S0010 dse: marks"
        for r in dead:
            pos = r[0]
            ln = s.lines[pos]
            ind = (len(ln) - len(ln.lstrip(" "))) // 4
            assert ln == "    " * ind + "_br = %d" % F.id, (ln, F.id)
            prev = s.lines[pos - 1]
            if (len(prev) - len(prev.lstrip(" "))) // 4 == ind - 1:  # the arm's only line
                s.lines[pos] = "    " * ind + "pass"
                r[0] = -1
            else:
                s.splice(pos, 1, [])
        if not F.nbk and len(dead) == len(sts):
            F.esc.discard(F.id)
        for r in sts:  # F has ended: its stores need no position any more
            r[0] = -1
        s.recs = [r for r in s.recs if r[0] >= 0]

    def newtmp(s, t=None):
        s.tmp += 1
        nm = "t%d" % s.tmp
        if t is not None:
            s.tmpty[nm] = t
        return nm

    def lname(s, i):
        return "p%d" % i if i < len(s.params) else "l%d" % i

    # --- f32/f64 carried as int bit patterns (ModGen.fb): arithmetic converts an operand to a float
    # (`fv`) and its result back (`fr`); a bit-exact operation (load, store, select, neg, abs,
    # copysign, reinterpret, constants, locals) never makes a float, so a signalling NaN survives
    # Peephole: `fr` remembers the float source of its bits and `fv` of the same expression uses it,
    # so a*b+c converts once at each end. Exact: an arithmetic result is a quiet NaN or no NaN.
    def fv(s, e):
        if not s.mg.fb:
            return e.c
        f = s.fls.get((e.t, e.c))
        if f is not None:
            return f
        return ("_f32_i32(%s)" if e.t == F32 else "_f64_i64(%s)") % e.c

    def fr(s, c, T):
        if not s.mg.fb:
            return c
        r = ("_i32_f32(%s)" if T == F32 else "_i64_f64(%s)") % c
        s.fls[(T, r)] = c
        return r

    def fconst(s, bits, T):
        """A float constant: its bits, and (unless a NaN, which the host may quiet) its float form."""
        c = flit(bits, True)
        f = flit(bits, False, 32 if T == F32 else 64)
        if not f.startswith("_f64_i64"):
            s.fls[(T, c)] = f
        return c

    # --- normalisation
    def n(s, e):
        """canonical (unsigned, in-range) code for an integer expression"""
        if e.t in (F32, F64) or e.b is not None:
            return e.c
        if e.w:
            MAXS, MODS = (
                ("0xFFFFFFFF", "0x100000000")
                if e.t == I32
                else ("0xFFFFFFFFFFFFFFFF", "0x10000000000000000")
            )
            w = "_w" if e.t == I32 else "_W"
            s.uses_w.add(w)
            if e.w == "up":
                return "(%s if (%s:=%s) <= %s else %s-%s)" % (w, w, e.c, MAXS, w, MODS)
            return "(%s if (%s:=%s) >= 0 else %s+%s)" % (w, w, e.c, w, MODS)
        return "(%s&%s)" % (e.c, "0xFFFFFFFF" if e.t == I32 else "0xFFFFFFFFFFFFFFFF")

    def nb(s, e):
        if e.b is not None:
            return e.b
        return M32 if e.t == I32 else M64

    def sgn(s, e):
        """signed value code"""
        W = 32 if e.t == I32 else 64
        if e.b is not None and e.b < (1 << (W - 1)):
            return e.c
        h = "0x80000000" if W == 32 else "0x8000000000000000"
        return "((%s^%s)-%s)" % (s.n(e), h, h)

    def cond(s, e):
        if e.t == "bool" or e.b is not None:
            return e.c
        return s.n(e)

    def canon(s, e):
        """turn bool into int-typed i32 canonical expression (bool already is an int)"""
        return e

    # --- stack
    def push(s, e):
        s.stack.append(e)

    def pop(s):
        e = s.stack.pop()
        if e.t == "bool":  # keep ints exact (bool breaks CPython's int specialisations)
            e = E("(1 if %s else 0)" % e.c, I32, 1, e.Ls, e.m, e.g, x=e.x)
        return e

    def popc(s):
        """pop a value used only for its truthiness"""
        return s.stack.pop()

    def popn(s, n):
        if n == 0:
            return []
        v = [s.pop() for _ in range(n)]
        v.reverse()
        return v

    def materialize(s, i):
        e = s.stack[i]
        if e.k:
            return  # already a temp / plain name that is not going to change
        t = s.newtmp(e.t)
        s.emit("%s = %s" % (t, e.c))
        s.stack[i] = E(t, e.t, e.b, k=True, w=e.w)

    def flush(s, pred=lambda e: not e.pure_const or True):
        sel = [
            i
            for i, e in enumerate(s.stack)
            if not (e.k or (e.pure_const and not e.x and e.c[:1] not in "pl")) and pred(e)
        ]
        # the first trap wins: whatever below the last one may trap runs before it
        for i in range(sel[-1] + 1 if sel else 0):
            if i in sel or s.stack[i].m or s.stack[i].x:
                s.materialize(i)

    def flush_all(s):
        for i, e in enumerate(s.stack):
            if not e.k and (not e.pure_const or e.x):
                s.materialize(i)

    def flush_mem(s):
        s.flush(lambda e: e.m or e.x)

    def flush_call(s):
        s.flush(lambda e: e.m or e.g or e.x)

    def flush_trapping(s):
        """a branch, return or unreachable discards the stack below its arity: what may trap runs first"""
        s.flush(lambda e: e.m or e.x)

    def flush_local(s, name):
        s.flush(lambda e: name in e.Ls)

    def flush_global(s, gname):
        s.flush(lambda e: gname in e.g or e.x)

    # --- block types
    def bt(s, b):
        if b[0] == "idx":
            return s.mg.types[b[1]]
        return b

    # --- prepass: who targets which block, and how
    def prepass(s):
        info = {}
        st = [-1]
        info[-1] = [False, True]  # targeted, all-toplevel
        for i, ins in enumerate(s.ins):
            op = ins[0]
            if op in (0x02, 0x03, 0x04, 0x1F):
                st.append(i)
                info[i] = [False, True]
                if op == 0x1F:  # a catch clause branches to a label outside the try_table
                    for _, _, lab in ins[2]:
                        info[st[-2 - lab]][0] = True
                        info[st[-2 - lab]][1] = False
            elif op == 0x0B:
                st.pop()
            elif op in (0x0C, 0x0D, 0xD5, 0xD6):
                t = st[-1 - ins[1]]
                info[t][0] = True
                if ins[1] != 0:
                    info[t][1] = False
            elif op == 0x0E:
                for d in ins[1] + [ins[2]]:
                    t = st[-1 - d]
                    info[t][0] = True
                    info[t][1] = False
        return info

    # --- branching
    #
    # Python loops exist only for wasm `loop`s that are branched to.  `block`/`if` exits are realised
    # structurally: a br to the innermost block at its top level turns the rest of the block into
    # `if not cond:`; anything else sets the label register `_br` and the rest of each enclosing
    # construct is guarded (an `else:`/`if not _br:` nest), breaking out of python loops on the way.
    def assign_results(s, T, n, names=None):
        names = T.rv if names is None else names
        if not names:
            return
        vals = s.stack[len(s.stack) - n :] if n else []
        codes = [s.n(v) if v.t in (I32, I64) else v.c for v in vals]
        s.emit("%s = %s" % (", ".join(names), ", ".join(codes)))

    def br_code(s, depth):
        """emit a branch; returns True if control is transferred by a python jump (continue/break/
        return), False if only the `_br` flag was set (caller must guard the following code)"""
        T = s.ctl[-1 - depth]
        if T.kind == "func":
            s.emit_return()
            return True
        if T.kind != "loop":
            s.assign_results(T, len(T.res))
        else:
            s.assign_results(T, len(T.pv), T.pv)
        if depth == 0 and T.kind != "loop":
            T.narr += 1
            T.zx = True
            return False  # rest of T is skipped structurally by the caller
        P = None
        for F in reversed(s.ctl[len(s.ctl) - 1 - depth :]):
            if F.pyloop:
                P = F
                break
        if P is T:  # only possible for a loop target
            s.emit("continue")
            return True
        s.uses_br = True
        s.emit("_br = %d" % T.id)
        T.narr += 1
        if P is not None:
            s.emit("break")
            P.esc.add(T.id)
            T.nbk += 1
            return True
        s.ctl[-1].esc.add(T.id)
        if s.dse_on:
            s.stores.setdefault(T.id, []).append(s.rec(len(s.lines) - 1, "st", T.id))
        return False

    def emit_return(s):
        n = len(s.results)
        if n == 0:
            s.emit("return")
        else:
            vals = s.stack[len(s.stack) - n :]
            codes = [s.n(v) if v.t in (I32, I64) else v.c for v in vals]
            s.emit("return " + ", ".join(codes))

    def nest(s, F, line):
        F.nests.append(s.open(line))

    def propagate(s, F):
        """F was just closed (popped).  Route pending `_br` exits to the new top frame."""
        esc = F.esc - {F.id}
        if not esc:
            return
        P = s.ctl[-1]
        if P.pyloop:
            mk = s.open("if _br:")
            if P.id in esc:
                if esc == {P.id}:
                    s.emit("_br = 0")
                    s.emit("continue")
                else:
                    m2 = s.open("if _br == %d:" % P.id)
                    s.emit("_br = 0")
                    s.emit("continue")
                    s.close(m2)
                    s.emit("break")
            else:
                s.emit("break")
            s.close(mk)
            P.esc |= esc - {P.id}
        else:
            s.nest(P, "if not _br:")
            P.esc |= esc

    # --- main
    def gen(s):
        info = s.prepass()
        ids = iter(range(1, 1 << 30))
        s.ctl.append(Ctl("func", "func", 0, s.results, [], 0))
        ins = s.ins
        i = 0
        N = len(ins)
        deadd = 0
        while i < N:
            x = ins[i]
            op = x[0]
            if s.dead:
                if op in (0x02, 0x03, 0x04, 0x1F):
                    deadd += 1
                    i += 1
                    continue
                if deadd:
                    if op == 0x0B:
                        deadd -= 1
                    i += 1
                    continue
                if op not in (0x0B, 0x05):
                    i += 1
                    continue
            i += 1
            st = s.stack
            if op == 0x0B:  # end
                if len(s.ctl) == 1:
                    if not s.dead and s.results:
                        s.emit_return()
                    break
                s.end_frame()
                continue
            if op == 0x05:  # else
                F = s.ctl[-1]
                if not s.dead:
                    s.assign_results(F, len(F.res))
                    F.live_then = True
                del st[F.h :]
                st.extend(F.pin)
                for mk in reversed(F.nests):
                    s.close(mk)
                F.nests = []
                s.close(F.mark)
                F.mark = s.open("else:")
                F.has_else = True
                s.dead = False
                continue
            if op in (0x02, 0x03, 0x04, 0x1F):
                ps, rs = s.bt(x[1])
                tg, top = info[i - 1]
                if op == 0x04:
                    c = s.popc()
                    cc = s.cond(c)
                if op == 0x02:
                    mode = "nest" if top and not rs else "flag"
                elif op == 0x03:
                    mode = "loop" if tg else "inline"
                else:
                    mode = "flag"
                s.flush_all()
                kind = {2: "block", 3: "loop", 4: "if", 0x1F: "try"}[op]
                if kind == "try" or mode == "loop":
                    s.check_nesting(kind == "try")
                fid = next(ids)
                rv = ["r%d_%d" % (fid, j) for j in range(len(rs))]
                for nm_, t_ in zip(rv, rs):
                    s.rvty[nm_] = t_
                np_ = len(ps)
                F = Ctl(kind, mode, fid, rs, rv, len(st) - np_)
                F.tg = tg
                F.pin = st[len(st) - np_ :]
                if F.pyloop and np_:
                    # a br to this loop re-assigns these; the body sees them as plain temporaries
                    vals = s.popn(np_)
                    F.pv = ["q%d_%d" % (fid, j) for j in range(np_)]
                    s.emit(
                        "%s = %s"
                        % (
                            ", ".join(F.pv),
                            ", ".join(s.n(v) if v.t in (I32, I64) else v.c for v in vals),
                        )
                    )
                    F.pin = [
                        E(q, t_, (M32 if t_ == I32 else M64) if t_ in (I32, I64) else None, k=True)
                        for q, t_ in zip(F.pv, ps)
                    ]
                    st.extend(F.pin)
                s.ctl.append(F)
                if F.pyloop:
                    F.mark = s.open("while True:")
                if kind == "try":
                    F.cl = x[2]
                    F.mark = s.open("try:")
                if kind == "if":
                    F.mark = s.open("if %s:" % cc)
                continue
            if op == 0x0C:  # br
                d = x[1]
                T = s.ctl[-1 - d]
                s.flush_trapping()
                if not (d == 0 and T.mode == "nest"):
                    s.br_code(d)
                else:
                    T.zx = True
                s.dead = True
                continue
            if op == 0x0D:  # br_if
                c = s.popc()
                cc = s.cond(c)
                d = x[1]
                T = s.ctl[-1 - d]
                s.flush_trapping()
                if d == 0 and T.mode == "nest":
                    T.zx = True
                    s.nest(T, "if not %s:" % cc)
                    continue
                if T.kind != "loop" and T.res:
                    s.flush_all()
                m = s.open("if %s:" % cc)
                jumped = s.br_code(d)
                s.close(m)
                if not jumped:
                    s.nest(s.ctl[-1], "else:")
                continue
            if op in (0xD5, 0xD6):  # br_on_null / br_on_non_null: br_if on the reference itself
                v = s.pop()
                s.flush_all()
                t = s.newtmp(v.t)
                s.emit("%s = %s" % (t, v.c))
                ref = E(t, v.t, k=True)
                d = x[1]
                T = s.ctl[-1 - d]
                if op == 0xD5:
                    if d == 0 and T.mode == "nest":
                        T.zx = True
                        s.nest(T, "if %s is not None:" % t)
                        st.append(ref)
                        continue
                else:
                    st.append(ref)  # the branch carries the reference
                m = s.open("if %s is %sNone:" % (t, "" if op == 0xD5 else "not "))
                jumped = s.br_code(d)
                s.close(m)
                if not jumped:
                    s.nest(s.ctl[-1], "else:")
                if op == 0xD5:
                    st.append(ref)
                else:
                    st.pop()
                continue
            if op == 0x0E:  # br_table
                e = s.pop()
                idx = s.n(e)
                s.flush_all()
                if not idx.isidentifier():
                    t = s.newtmp(I32)
                    s.emit("%s = %s" % (t, idx))
                    idx = t
                # runs of consecutive indices with the same target; the last run ([n, 2**32)) is the default
                runs = []
                for k, d in enumerate(list(x[1]) + [x[2]]):
                    if not runs or runs[-1][1] != d:
                        runs.append((k, d))
                # S0010 sink: where the dispatch ends, and the python loop around its leaves
                te = s.rec(len(s.lines), "te") if "sink" in s.br else None
                loop = next((F for F in reversed(s.ctl) if F.pyloop), None)

                def leaf(d):
                    T = s.ctl[-1 - d]
                    if s.br_code(d) or te is None or T.kind != "block":
                        return
                    if any(F.kind == "try" for F in s.ctl[len(s.ctl) - 1 - d :]):
                        return  # a region moved into a try body would be inside the handler's reach
                    pos = len(s.lines) - (1 if d else 0)  # the `_br = K` line, or where `pass` goes
                    T.leaves.append(s.rec(pos, "lf", T, s.ind, loop, te, d != 0))

                if BRTREE and len(runs) > BRTREE:
                    # Python has no computed goto: a balanced tree of `<` tests over the runs, log2(runs)
                    # comparisons instead of a linear chain (the same lowering compilers use for sparse switches)
                    def tree(rs):
                        if len(rs) == 1:
                            leaf(rs[0][1])
                            return
                        h = len(rs) // 2
                        m = s.open("if %s < %d:" % (idx, rs[h][0]))
                        tree(rs[:h])
                        s.close(m)
                        m = s.open("else:")
                        tree(rs[h:])
                        s.close(m)

                    tree(runs)
                    if te is not None:
                        te[0] = len(s.lines)
                    s.dead = True
                    continue
                groups = {}
                for k, d in enumerate(x[1]):
                    if d != x[2]:
                        groups.setdefault(d, []).append(k)
                first = True
                for d, ks in groups.items():
                    if len(ks) == 1:
                        c = "%s == %d" % (idx, ks[0])
                    elif ks == list(range(ks[0], ks[-1] + 1)):
                        c = "%d <= %s <= %d" % (ks[0], idx, ks[-1])
                    else:
                        c = "%s in %r" % (idx, tuple(ks))
                    m = s.open(("if " if first else "elif ") + c + ":")
                    first = False
                    leaf(d)
                    s.close(m)
                if first:
                    leaf(x[2])
                else:
                    m = s.open("else:")
                    leaf(x[2])
                    s.close(m)
                if te is not None:
                    te[0] = len(s.lines)
                s.dead = True
                continue
            if op == 0x08:  # throw
                vals = s.popn(len(s.mg.types[s.mg.tagtypes[x[1]]][0]))
                s.flush_trapping()
                codes = [s.n(v) if v.t in (I32, I64) else v.c for v in vals]
                s.emit("raise _Thrown(TG%d, (%s))" % (x[1], "".join(c + "," for c in codes)))
                s.dead = True
                continue
            if op == 0x0A:  # throw_ref
                v = s.pop()
                s.flush_trapping()
                s.emit("_rethrow(%s)" % v.c)
                s.dead = True
                continue
            if op == 0x0F:
                s.flush_trapping()
                s.emit_return()
                s.dead = True
                continue
            if op == 0x00:
                s.flush_trapping()
                s.emit('raise Trap(TrapCode.UNREACHABLE, "unreachable")')
                s.dead = True
                continue
            if op == 0x01:
                continue
            if s.dead:
                raise AssertionError("dead code leak")
            s.simple(x)
        body = s.lines
        fname = "f%d" % s.fidx
        tc = s.fidx in s.mg.tc
        if tc:
            fname += "_b"
        args = ", ".join(s.lname(j) for j in range(len(s.params)))
        head = ["def %s(%s):" % (fname, args)]
        if s.globals_set:
            head.append("    global " + ", ".join(sorted(s.globals_set)))
        inits = [s.lname(j) for j in range(len(s.params), len(s.ltypes))]
        fl = [s.lname(j) for j in range(len(s.params), len(s.ltypes)) if s.ltypes[j] in (F32, F64)]
        rl = [
            s.lname(j)
            for j in range(len(s.params), len(s.ltypes))
            if s.ltypes[j].__class__ is tuple or s.ltypes[j] in (0x70, 0x6F, 0x69, 0x74)
        ]
        il = [v for v in inits if v not in fl and v not in rl]
        if rl:
            head.append("    " + " = ".join(rl) + " = None")
        if il:
            head.append("    " + " = ".join(il) + " = 0")
        if fl:
            head.append("    " + " = ".join(fl) + (" = 0" if s.mg.fb else " = 0.0"))
        if s.uses_br and (not s.br or "_br" in "\n".join(body)):
            head.append("    _br = 0")
        if not body:
            body = ["    pass"]
        if tc:
            body += [
                "def f%d(%s):" % (s.fidx, args),
                "    r = %s(%s)" % (fname, args),
                "    while r.__class__ is _TC:",
                "        r = r.f(*r.a)",
                "    return r",
            ]
        return "\n".join(head + body) + "\n"

    def check_nesting(s, is_try):
        """CPython allows 20 statically nested blocks (LESSONS 12); a `try` counts, and one is left
        for the `try` of `call_indirect`. Loops are checked only inside a try_table (S0012 Q6)."""
        n = sum(1 for F in s.ctl if F.pyloop or F.kind == "try")
        if n >= 19 and (is_try or any(F.kind == "try" for F in s.ctl)):
            raise Unsupported("pyc", "nesting")

    def handlers(s, F):
        """The `except` clause of a try_table, emitted while F is still the top frame: a branch of a
        clause is a branch from inside F, so the `_br` routing of S0010 applies as it does to a `br`."""
        s.dead = False
        del s.stack[F.h :]
        m0 = s.open("except _Thrown as _e:")
        first = True
        for kind, tag, lab in F.cl:
            if kind < 2:
                m = s.open(("if " if first else "elif ") + "_e.tag is TG%d:" % tag)
            else:
                m = s.open("if True:" if first else "else:")
            first = False
            ts = s.mg.types[s.mg.tagtypes[tag]][0] if tag is not None else []
            if ts:
                names = [s.newtmp(t) for t in ts]
                s.emit("%s, = _e.vals" % ", ".join(names))
                for nm, t in zip(names, ts):
                    s.push(
                        E(nm, t, (M32 if t == I32 else M64) if t in (I32, I64) else None, k=True)
                    )
            if kind & 1:  # the exnref: copied before `_e` is unbound at the end of the clause
                nm = s.newtmp(0x69)
                s.emit("%s = _e" % nm)
                s.emit("%s.__traceback__ = None" % nm)  # no cycle frame -> exnref -> traceback
                s.push(E(nm, 0x69, k=True))
            s.br_code(lab + 1)
            del s.stack[F.h :]
            s.close(m)
            if kind >= 2:
                break
        else:  # no catch_all: the exception goes on
            if first:
                s.emit("raise")
            else:
                m = s.open("else:")
                s.emit("raise")
                s.close(m)
        s.close(m0)
        s.dead = False

    def end_frame(s):
        F = s.ctl[-1]
        live = not s.dead
        if live:
            s.assign_results(F, len(F.res))
        del s.stack[F.h :]
        for mk in reversed(F.nests):
            s.close(mk)
        for cap in reversed(F.caps):  # S0010: the case regions that end here, latest first
            s.sink(cap, live or F.zx)
            cap[1][0] = cap[2][0] = cap[3][0] = -1
        F.caps = []
        if F.kind == "try":
            s.close(F.mark)
            s.handlers(F)
        if F.kind == "if":
            s.close(F.mark)
            if F.rv and not F.has_else:  # the implicit else passes the params through
                m = s.open("else:")
                s.emit(
                    "%s = %s"
                    % (
                        ", ".join(F.rv),
                        ", ".join(s.n(v) if v.t in (I32, I64) else v.c for v in F.pin),
                    )
                )
                s.close(m)
        if F.pyloop:
            s.emit("break")
            s.close(F.mark)
        s.ctl.pop()
        cand = (  # S0010 sink: T = F, its region is what follows in the parent
            F.kind == "block"
            and F.leaves
            and F.narr == len(F.leaves)
            and not live
            and s.ctl[-1].kind in ("block", "loop")
        )
        if not cand:
            for r in F.leaves:
                r[0] = -1
            if F.id in s.stores and F.kind != "loop":
                s.dse(F)
        elif s.dse_on:  # every store of a sink candidate is a leaf, which has its own record
            for r in s.stores.pop(F.id, ()):
                r[0] = -1
        g = s.rec(len(s.lines), "cg") if cand else None
        if F.kind != "loop" and F.id in F.esc:
            if F.esc - {F.id}:
                m = s.open("if _br == %d:" % F.id)
                s.emit("_br = 0")
                s.close(m)
            else:
                s.emit("_br = 0")
        ge = s.rec(len(s.lines), "ce") if cand else None  # the end of T's guard
        s.propagate(F)
        if cand:
            s.ctl[-1].caps.append((F, g, ge, s.rec(len(s.lines), "cb"), s.ind))
        if F.kind == "if":
            after = live or F.live_then or not F.has_else or F.tg
        elif F.kind == "loop":
            after = live
        else:
            after = live or F.tg
        s.dead = not after
        for j, t in enumerate(F.res):
            s.push(E(F.rv[j], t, (M32 if t == I32 else M64) if t in (I32, I64) else None, k=True))

    # --- straight-line ops
    def simple(s, x):
        op = x[0]
        push, pop = s.push, s.pop
        n, nb, sgn = s.n, s.nb, s.sgn
        mg = s.mg
        if 0xFC13 <= op <= 0xFC16:  # wide arithmetic (S0008), with the SIMD helpers
            from waboros.targets.pyc import simd

            return simd.gen_wide(s, x)
        if op >= 0xFD000:
            # SIMD lives in its own module (S0005); imported here because simd imports codegen
            from waboros.targets.pyc import simd

            return simd.gen(s, x)
        if op == 0x1A:
            e = pop()
            if (e.m or e.x) and not e.k:  # a dropped load/div/trunc can still trap
                s.flush_trapping()
                s.emit(e.c)
            return
        if op == 0x1B:
            c = s.popc()
            b = pop()
            a = pop()
            bb = max(a.b, b.b) if a.b is not None and b.b is not None else None
            if any((e.m or e.x) and not e.k for e in (a, b)):  # both arms are always evaluated
                s.flush_trapping()
                ab = []
                for e in (a, b):
                    if not e.k:
                        t = s.newtmp(e.t)
                        s.emit("%s = %s" % (t, e.c))
                        e = E(t, e.t, e.b, k=True, w=e.w)
                    ab.append(e)
                a, b = ab
            push(E("(%s if %s else %s)" % (a.c, s.cond(c), b.c), a.t, bb, **dep(a, b, c)))
            return
        if op == 0x20:
            nm = s.lname(x[1])
            t = s.ltypes[x[1]]
            push(E(nm, t, (M32 if t == I32 else M64) if t in (I32, I64) else None, Ls=[nm]))
            return
        if op in (0x21, 0x22):
            nm = s.lname(x[1])
            v = pop()
            s.flush_local(nm)
            if v.m or v.x:
                s.flush_trapping()
            if v.t in (I32, I64) and v.b is None and v.w:
                MAXS, MODS = (
                    ("0xFFFFFFFF", "0x100000000")
                    if v.t == I32
                    else ("0xFFFFFFFFFFFFFFFF", "0x10000000000000000")
                )
                s.emit("%s = %s" % (nm, v.c))
                s.emit(
                    ("if %s > %s: %s -= %s" if v.w == "up" else "if %s < 0: %s += %s")
                    % ((nm, MAXS, nm, MODS) if v.w == "up" else (nm, nm, MODS))
                )
                if op == 0x22:
                    t = s.ltypes[x[1]]
                    push(E(nm, t, (M32 if t == I32 else M64), Ls=[nm]))
                return
            code = n(v) if v.t in (I32, I64) else v.c
            last = s.lines[-1] if s.lines else ""
            pre = "    " * s.ind + code + " = "
            if v.k and code[:1] == "t" and last.startswith(pre):
                s.lines[-1] = "    " * s.ind + nm + " = " + last[len(pre) :]
            else:
                s.emit("%s = %s" % (nm, code))
            if op == 0x22:
                t = s.ltypes[x[1]]
                push(E(nm, t, (M32 if t == I32 else M64) if t in (I32, I64) else None, Ls=[nm]))
            return
        if op == 0x23:
            g = mg.gname(x[1])
            t = mg.gtypes[x[1]]
            if mg.gmut[x[1]]:
                push(E(g, t, (M32 if t == I32 else M64) if t in (I32, I64) else None, g=[g]))
            else:
                push(E(g, t, (M32 if t == I32 else M64) if t in (I32, I64) else None, k=True))
            return
        if op == 0x24:
            g = mg.gname(x[1])
            v = pop()
            s.flush_global(g)
            if "." not in g:
                s.globals_set.add(g)
            s.emit("%s = %s" % (g, n(v) if v.t in (I32, I64) else v.c))
            return
        if op == 0x41:
            push(E(str(x[1]), I32, x[1]))
            return
        if op == 0x42:
            push(E(str(x[1]), I64, x[1]))
            return
        if op == 0x43:
            push(E(s.fconst(x[1], F32) if s.mg.fb else flit(x[1], False, 32), F32))
            return
        if op == 0x44:
            push(E(s.fconst(x[1], F64) if s.mg.fb else flit(x[1], False, 64), F64))
            return
        if 0x28 <= op <= 0x35:
            a = pop()
            off = x[1]
            ad = n(a) if off == 0 else "%s+%d" % (n(a), off)
            mn = mg.mname(x[3])
            d = dep(a, m=True, x=True)  # a load can trap on a bad address
            L = {
                0x28: ("U32(M,%s)[0]", I32, M32),
                0x29: ("U64(M,%s)[0]", I64, M64),
                0x2A: ("_f32_i32(U32(M,%s)[0])", F32, None),
                0x2B: ("F64L(M,%s)[0]", F64, None),
                0x2C: ("S8(M,%s)[0]", I32, None),
                0x2D: ("M[%s]", I32, 255),
                0x2E: ("S16(M,%s)[0]", I32, None),
                0x2F: ("U16(M,%s)[0]", I32, 0xFFFF),
                0x30: ("S8(M,%s)[0]", I64, None),
                0x31: ("M[%s]", I64, 255),
                0x32: ("S16(M,%s)[0]", I64, None),
                0x33: ("U16(M,%s)[0]", I64, 0xFFFF),
                0x34: ("S32(M,%s)[0]", I64, None),
                0x35: ("U32(M,%s)[0]", I64, M32),
            }[op]
            if s.mg.fb and op in (0x2A, 0x2B):
                L = ("U32(M,%s)[0]" if op == 0x2A else "U64(M,%s)[0]", L[1], L[2])
            push(E(L[0].replace("M", mn) % ad, L[1], L[2], **d))
            return
        if 0x36 <= op <= 0x3E:
            v = pop()
            a = pop()
            off = x[1]
            mn = mg.mname(x[3])
            s.flush_mem()
            ad = n(a) if off == 0 else "%s+%d" % (n(a), off)
            if op == 0x36:
                s.emit("P32(%s,%s,%s)" % (mn, ad, n(v)))
            elif op == 0x37:
                s.emit("P64(%s,%s,%s)" % (mn, ad, n(v)))
            elif op == 0x38:
                s.emit(("P32(%s,%s,%s)" if s.mg.fb else "F32S(%s,%s,%s)") % (mn, ad, v.c))
            elif op == 0x39:
                s.emit(("P64(%s,%s,%s)" if s.mg.fb else "F64S(%s,%s,%s)") % (mn, ad, v.c))
            elif op in (0x3A, 0x3C):
                vc = v.c if v.b is not None and v.b <= 255 else "%s&255" % v.c
                if (a.m or a.x) and (v.m or v.x):  # Python evaluates the value before the index
                    t = s.newtmp()
                    s.emit("%s = %s" % (t, ad))
                    ad = t
                s.emit("%s[%s] = %s" % (mn, ad, vc))
            elif op in (0x3B, 0x3D):
                vc = v.c if v.b is not None and v.b <= 0xFFFF else "%s&0xFFFF" % v.c
                s.emit("P16(%s,%s,%s)" % (mn, ad, vc))
            elif op == 0x3E:
                vc = v.c if v.b is not None and v.b <= M32 else "%s&0xFFFFFFFF" % v.c
                s.emit("P32(%s,%s,%s)" % (mn, ad, vc))
            return
        if op == 0x3F:
            if mg.maddr[x[1]] == I64:
                push(E("(len(%s)>>16)" % mg.mname(x[1]), I64, 1 << 48, m=True))
            else:
                push(E("(len(%s)>>16)" % mg.mname(x[1]), I32, 0xFFFF, m=True))
            return
        if op == 0x40:
            v = pop()
            s.flush_call()
            a64 = mg.maddr[x[1]] == I64
            t = s.newtmp(I64 if a64 else I32)
            s.emit(
                "%s = _grow%s(%s,%s,%s)"
                % (t, "64" if a64 else "", mg.mname(x[1]), mg.mname(x[1], "_MAXPAGES"), n(v))
            )
            push(E(t, I64 if a64 else I32, M64 if a64 else M32, k=True))
            return
        if op in (0x10, 0x11, 0x12, 0x13, 0x14, 0x15):
            base = {0x12: 0x10, 0x13: 0x11, 0x15: 0x14}.get(op, op)
            tail = base != op
            op = base
            if op == 0x10:
                fi = x[1]
                ps, rs = mg.ftype(fi)
                callee = mg.fname(fi)
            else:
                ps, rs = mg.types[x[1]]
                # args (then the reference of call_ref) are evaluated before the table or null check
                chk = s.stack[len(s.stack) - len(ps) - 1 :]
                if any((e.m or e.x) and not e.k for e in (chk if op == 0x14 else chk[:-1])):
                    s.flush_trapping()
                ix = pop()
            args = s.popn(len(ps))
            s.flush_call()
            if op == 0x14:
                callee = s.newtmp()
                s.emit("%s = %s" % (callee, ix.c))
                s.emit("if %s is None: _tnullfn()" % callee)
            if op == 0x11:
                callee = s.newtmp()
                q = s.newtmp()
                ixc = n(ix)
                if ix.m or ix.x:  # a bad address in the index is not an IndexError of the table
                    ixc = s.newtmp()
                    s.emit("%s = %s" % (ixc, n(ix)))
                s.emit("try:")
                s.emit("    %s = T%d[%s]" % (callee, x[2], ixc))
                s.emit("    %s = %s.s" % (q, callee))
                s.emit("except IndexError:")
                s.emit("    _toob()")
                s.emit("except AttributeError:")
                s.emit("    _tnull()")
                s.emit("if %s != %r:" % (q, mg.sig(*mg.types[x[1]])))
                s.emit("    _tsig()")
            ac = ", ".join(n(a) if a.t in (I32, I64) else a.c for a in args)
            if tail:
                # tail call: a function with tail calls returns a _TC that its wrapper runs (constant stack)
                tup = "(%s)" % (ac + "," if ac else "")
                # indirect or imported callee: its `.t` body if it has one (a host callable has none)
                if op in (0x11, 0x14) or fi < s.mg.nimp:
                    s.emit('return _TC(getattr(%s, "t", %s), %s)' % (callee, callee, tup))
                elif fi in s.mg.tc:
                    s.emit("return _TC(%s_b, %s)" % (callee, tup))
                elif any(F.kind == "try" for F in s.ctl):  # the frame is gone before the call runs
                    s.emit("return _TC(%s, %s)" % (callee, tup))
                else:
                    s.emit("return %s(%s)" % (callee, ac))
                s.dead = True
                return
            call = "%s(%s)" % (callee, ac)
            if not rs:
                s.emit(call)
            elif len(rs) == 1:
                t = s.newtmp(rs[0])
                s.emit("%s = %s" % (t, call))
                push(
                    E(
                        t,
                        rs[0],
                        (M32 if rs[0] == I32 else M64) if rs[0] in (I32, I64) else None,
                        k=True,
                    )
                )
            else:
                ts = [s.newtmp(ty) for ty in rs]
                s.emit("%s = %s" % (", ".join(ts), call))
                for t, ty in zip(ts, rs):
                    push(
                        E(t, ty, (M32 if ty == I32 else M64) if ty in (I32, I64) else None, k=True)
                    )
            return
        # ---- integer ops
        if op in (0x45, 0x50):
            a = s.popc()
            if a.t == "bool":
                push(E("(not %s)" % a.c, "bool", 1, **dep(a)))
                return
            push(E("(%s==0)" % n(a), "bool", 1, **dep(a)))
            return
        if 0x46 <= op <= 0x4F or 0x51 <= op <= 0x5A:
            b = pop()
            a = pop()
            k = op - (0x46 if op <= 0x4F else 0x51)
            W = 32 if op <= 0x4F else 64
            h = 0x80000000 if W == 32 else 0x8000000000000000
            pyop = ["==", "!=", "<", "<", ">", ">", "<=", "<=", ">=", ">="][k]
            if k in (2, 4, 6, 8) and (a.pure_const and a.c.isdigit()) != (
                b.pure_const and b.c.isdigit()
            ):
                # x <op>_s C  ->  range tests on the unsigned value; stays on small-int fast paths
                MOD = 1 << W
                HMAX = h - 1
                if a.pure_const and a.c.isdigit():  # C <op> x  -> x <swapped op> C
                    a, b = b, a
                    k = {2: 4, 4: 2, 6: 8, 8: 6}[k]
                x_ = n(a)
                cu = int(b.c)
                cs = cu - MOD if cu >= h else cu
                if k == 6 and cs < HMAX:
                    k, cs = 2, cs + 1  # le c  == lt c+1
                elif k == 8 and cs > -h:
                    k, cs = 4, cs - 1  # ge c  == gt c-1
                if k == 2:
                    c = (
                        ("(%s < %d or %s > %d)" % (x_, cs, x_, HMAX))
                        if cs >= 0
                        else ("(%d < %s < %d)" % (HMAX, x_, cs + MOD))
                    )
                    c = c if cs != 0 else "(%s > %d)" % (x_, HMAX)
                elif k == 4:
                    c = (
                        ("(%d < %s <= %d)" % (cs, x_, HMAX))
                        if cs >= 0
                        else ("(%s <= %d or %s > %d)" % (x_, HMAX, x_, cs + MOD))
                    )
                else:  # unreachable edge (le MAX / ge MIN): always true
                    c = "True"
                if not x_.isidentifier() and " or " in c:
                    t = s.newtmp(I32 if W == 32 else I64)
                    s.flush_all()
                    s.emit("%s = %s" % (t, x_))
                    c = c.replace(x_, t)
                    dd = dep()
                else:
                    dd = dep(a, b)
                push(E(c, "bool", 1, **dd))
                return
            if k in (2, 4, 6, 8):  # signed
                lim = h
                if nb(a) < lim and nb(b) < lim:
                    c = "(%s%s%s)" % (n(a), pyop, n(b))
                else:

                    def fl(e):
                        if e.pure_const and e.c.isdigit():
                            return str(int(e.c) ^ h)
                        return "(%s^%s)" % (n(e), hex(h))

                    c = "(%s%s%s)" % (fl(a), pyop, fl(b))
            else:
                c = "(%s%s%s)" % (n(a), pyop, n(b))
            push(E(c, "bool", 1, **dep(a, b)))
            return
        if 0x67 <= op <= 0x78 or 0x79 <= op <= 0x8A:
            W = 32 if op <= 0x78 else 64
            k = op - (0x67 if W == 32 else 0x79)
            MAX = M32 if W == 32 else M64
            T = I32 if W == 32 else I64
            if k <= 2:
                a = pop()
                f = ["_clz", "_ctz", "_popcnt"][k]
                if k == 2:
                    push(E("_pc(%s)" % n(a), T, W, **dep(a)))
                elif k == 0:
                    push(E("_clz%d(%s)" % (W, n(a)), T, W, **dep(a)))
                else:
                    push(E("_ctz%d(%s)" % (W, n(a)), T, W, **dep(a)))
                return
            if (
                k in (10, 11, 12)
                and len(s.stack) >= 2
                and s.stack[-1].t == "bool"
                and s.stack[-2].t == "bool"
            ):
                b = s.popc()
                a = s.popc()  # bool & bool stays a (pure) bool expression
                o = {10: "and", 11: "or", 12: "!="}[k]
                if k != 12 and (b.x or b.m):  # `and`/`or` would skip a trapping second operand
                    o = "&" if k == 10 else "|"
                push(E("(%s %s %s)" % (a.c, o, b.c), "bool", 1, **dep(a, b)))
                return
            b = pop()
            a = pop()
            d = dep(a, b)
            HALF = 1 << (W - 1)
            MOD = 1 << W

            def isc(e):
                return e.pure_const and e.c.isdigit()

            if k in (3, 5, 10, 11, 12) and isc(a) and not isc(b):
                a, b = b, a  # commutative: const right
            if k == 3 and isc(b) and int(b.c) >= HALF:
                push(
                    E(
                        "(%s-%d)" % (a.c, MOD - int(b.c)),
                        T,
                        None,
                        w="down" if a.b is not None else None,
                        **d,
                    )
                )
                return
            if k == 4 and isc(b) and int(b.c) >= HALF:
                push(
                    E(
                        "(%s+%d)" % (a.c, MOD - int(b.c)),
                        T,
                        None,
                        w="up" if a.b is not None else None,
                        **d,
                    )
                )
                return
            if k == 10 and isc(b) and int(b.c) >= HALF and int(b.c) != MAX:
                push(E("(%s&%d)" % (a.c, int(b.c) - MOD), T, a.b, **d))
                return
            if k in (11, 12) and isc(b) and int(b.c) >= HALF:
                push(E("(%s%s%d)" % (a.c, "|" if k == 11 else "^", int(b.c) - MOD), T, None, **d))
                return
            if k == 3:  # add
                bb = (
                    (a.b + b.b)
                    if a.b is not None and b.b is not None and a.b + b.b <= MAX
                    else None
                )
                if b.pure_const and b.c == "0":
                    push(a)
                    return
                ww = "up" if bb is None and a.b is not None and b.b is not None else None
                push(E("(%s+%s)" % (a.c, b.c), T, bb, w=ww, **d))
                return
            if k == 4:  # sub
                if b.pure_const and b.c == "0":
                    push(a)
                    return
                ww = "down" if a.b is not None and b.b is not None else None
                push(E("(%s-%s)" % (a.c, b.c), T, None, w=ww, **d))
                return
            if k == 5:  # mul
                bb = (
                    (a.b * b.b)
                    if a.b is not None and b.b is not None and a.b * b.b <= MAX
                    else None
                )
                push(E("(%s*%s)" % (a.c, b.c), T, bb, **d))
                return
            if k == 6:
                push(E("_divs%d(%s,%s)" % (W, n(a), n(b)), T, MAX, **dict(d, x=True)))
                return
            if k == 7:
                push(E("(%s//%s)" % (n(a), n(b)), T, nb(a), **dict(d, x=True)))
                return
            if k == 8:
                push(E("_rems%d(%s,%s)" % (W, n(a), n(b)), T, MAX, **dict(d, x=True)))
                return
            if k == 9:
                push(E("(%s%%%s)" % (n(a), n(b)), T, nb(a), **dict(d, x=True)))
                return
            if k == 10:  # and
                if a.b is not None or b.b is not None:
                    bb = min(x_.b for x_ in (a, b) if x_.b is not None)
                    push(E("(%s&%s)" % (a.c, b.c), T, bb, **d))
                else:
                    push(E("(%s&%s)" % (a.c, b.c), T, None, **d))
                return
            if k in (11, 12):
                o = "|" if k == 11 else "^"
                if a.b is not None and b.b is not None:
                    bb = (1 << max(a.b, b.b).bit_length()) - 1
                    push(E("(%s%s%s)" % (a.c, o, b.c), T, bb, **d))
                else:
                    push(E("(%s%s%s)" % (a.c, o, b.c), T, None, **d))
                return
            # shifts / rotates: count
            if b.pure_const and b.c.isdigit():
                cnt = int(b.c) & (W - 1)
                cc = str(cnt)
                cb = cnt
            else:
                cc = "(%s&%d)" % (b.c, W - 1)
                cb = None
            if k == 13:  # shl
                if cb is not None and a.b is not None and (a.b << cb) <= MAX:
                    push(E("(%s<<%s)" % (a.c, cc), T, a.b << cb, **d))
                else:
                    push(E("(%s<<%s)" % (a.c, cc), T, None, **d))
                return
            if k == 14:  # shr_s
                push(
                    E(
                        "(%s>>%s)" % (sgn(a), cc),
                        T,
                        (nb(a) >> cb)
                        if (a.b is not None and a.b < (MAX >> 1)) and cb is not None
                        else None,
                        **d,
                    )
                )
                return
            if k == 15:  # shr_u
                push(E("(%s>>%s)" % (n(a), cc), T, nb(a) >> cb if cb is not None else nb(a), **d))
                return
            if k == 16:
                push(E("_rotl%d(%s,%s)" % (W, n(a), cc), T, MAX, **d))
                return
            if k == 17:
                push(E("_rotr%d(%s,%s)" % (W, n(a), cc), T, MAX, **d))
                return
        # ---- floats
        if 0x5B <= op <= 0x66:
            b = pop()
            a = pop()
            k = (op - 0x5B) % 6
            pyop = ["==", "!=", "<", ">", "<=", ">="][k]
            push(E("(%s%s%s)" % (s.fv(a), pyop, s.fv(b)), "bool", 1, **dep(a, b)))
            return
        if 0x8B <= op <= 0xA6:
            is32 = op <= 0x98
            k = op - (0x8B if is32 else 0x99)
            T = F32 if is32 else F64
            if k <= 6:
                a = pop()
                d = dep(a)
                f = ["abs", "-", "_fceil", "_ffloor", "_ftrunc", "_fnearest", "_fsqrt"][k]
                if s.mg.fb and k <= 1:
                    sg = 0x80000000 if is32 else 1 << 63
                    push(
                        E(
                            "(%s%s%d)" % (a.c, "&" if k == 0 else "^", sg - 1 if k == 0 else sg),
                            T,
                            None,
                            **d,
                        )
                    )
                    return
                if k == 0:
                    c = "abs(%s)" % a.c
                elif k == 1:
                    c = "(-%s)" % a.c
                else:
                    c = "%s(%s)" % (f, s.fv(a))
                if k == 6 and is32:
                    c = "_f32(%s)" % c
                push(E(s.fr(c, T), T, None, **d))
                return
            b = pop()
            a = pop()
            d = dep(a, b)
            if s.mg.fb and k == 13:
                sg = 0x80000000 if is32 else 1 << 63
                push(E("((%s&%d)|(%s&%d))" % (a.c, sg - 1, b.c, sg), T, None, **d))
                return
            ac, bc = s.fv(a), s.fv(b)
            if k == 7:
                c = "(%s+%s)" % (ac, bc)
            elif k == 8:
                c = "(%s-%s)" % (ac, bc)
            elif k == 9:
                c = "(%s*%s)" % (ac, bc)
            elif k == 10:
                c = "_fdiv(%s,%s)" % (ac, bc)
            elif k == 11:
                c = "_fmin(%s,%s)" % (ac, bc)
            elif k == 12:
                c = "_fmax(%s,%s)" % (ac, bc)
            else:
                c = "_copysign(%s,%s)" % (ac, bc)
            if is32 and k in (7, 8, 9, 10):
                c = "_f32(%s)" % c
            push(E(s.fr(c, T), T, None, **d))
            return
        # ---- conversions
        if op == 0xA7:
            a = pop()
            push(E(a.c, I32, a.b if a.b is not None and a.b <= M32 else None, **dep(a)))
            return
        if op in (0xA8, 0xA9, 0xAA, 0xAB, 0xAE, 0xAF, 0xB0, 0xB1):
            a = pop()
            W = 32 if op <= 0xAB else 64
            sg = "s" if op in (0xA8, 0xAA, 0xAE, 0xB0) else "u"
            push(
                E(
                    "_trunc_%s%d(%s)" % (sg, W, s.fv(a)),
                    I32 if W == 32 else I64,
                    M32 if W == 32 else M64,
                    **dep(a, x=True),
                )
            )
            return
        if 0xFC00 <= op <= 0xFC07:
            a = pop()
            k = op - 0xFC00
            W = 32 if k < 4 else 64
            sg = "u" if k & 1 else "s"
            push(
                E(
                    "_tsat_%s%d(%s)" % (sg, W, s.fv(a)),
                    I32 if W == 32 else I64,
                    M32 if W == 32 else M64,
                    **dep(a),
                )
            )
            return
        if op == 0xAC:
            a = pop()
            push(E(sgn(a), I64, a.b if a.b is not None and a.b < 0x80000000 else None, **dep(a)))
            return
        if op == 0xAD:
            a = pop()
            push(E(n(a), I64, nb(a), **dep(a)))
            return
        if op in (0xB2, 0xB4, 0xB7, 0xB9):
            a = pop()
            c = "float(%s)" % sgn(a)
            if op == 0xB4:
                c = "_i64_f32(%s)" % sgn(a)
            elif op == 0xB2:
                c = "_f32(%s)" % c
            T = F32 if op < 0xB7 else F64
            push(E(s.fr(c, T), T, None, **dep(a)))
            return
        if op in (0xB3, 0xB5, 0xB8, 0xBA):
            a = pop()
            c = "float(%s)" % n(a)
            if op == 0xB5:
                c = "_i64_f32(%s)" % n(a)
            elif op == 0xB3:
                c = "_f32(%s)" % c
            T = F32 if op < 0xB7 else F64
            push(E(s.fr(c, T), T, None, **dep(a)))
            return
        if op == 0xB6:
            a = pop()
            push(E(s.fr("_demote(%s)" % s.fv(a), F32), F32, None, **dep(a)))
            return
        if op == 0xBB:
            a = pop()
            push(E(s.fr("(%s*1.0)" % s.fv(a), F64), F64, None, **dep(a)))
            return
        if op == 0xBC:
            a = pop()
            push(E(a.c if s.mg.fb else "_i32_f32(%s)" % a.c, I32, M32, **dep(a)))
            return
        if op == 0xBD:
            a = pop()
            push(E(a.c if s.mg.fb else "_i64_f64(%s)" % a.c, I64, M64, **dep(a)))
            return
        if op == 0xBE:
            a = pop()
            push(E(n(a) if s.mg.fb else "_f32_i32(%s)" % n(a), F32, None, **dep(a)))
            return
        if op == 0xBF:
            a = pop()
            push(E(n(a) if s.mg.fb else "_f64_i64(%s)" % n(a), F64, None, **dep(a)))
            return
        if 0xC0 <= op <= 0xC4:
            a = pop()
            bits, T = {
                0xC0: (8, I32),
                0xC1: (16, I32),
                0xC2: (8, I64),
                0xC3: (16, I64),
                0xC4: (32, I64),
            }[op]
            h = 1 << (bits - 1)
            m = (1 << bits) - 1
            push(E("(((%s&%d)^%d)-%d)" % (a.c, m, h, h), T, None, **dep(a)))
            return
        if op == 0xD0:
            push(E("None", {-16: 0x70, -23: 0x69, -12: 0x74}.get(x[1], 0x6F), k=True))
            return
        if op == 0xD4:
            a = pop()
            push(E("_nonnull(%s)" % a.c, a.t, **dep(a, x=True)))
            return
        if op == 0xD1:
            a = pop()
            push(E("(%s is None)" % a.c, "bool", 1, **dep(a)))
            return
        if op == 0xD2:
            push(E(mg.fname(x[1]), 0x70, k=True))
            return
        if op == 0x25:
            i = pop()
            s.flush_call()
            t = s.newtmp()
            s.emit("%s = _tget(T%d,%s)" % (t, x[1], n(i)))
            push(E(t, mg.ttypes[x[1]], None, k=True))
            return
        if 0xFC0C <= op <= 0xFC11 or op in (0x26, 0xFC0D):
            if op == 0xFC0D:
                s.globals_set.add("ES%d" % x[1])
                s.emit("ES%d = []" % x[1])
                return
            k = len(x) > 1 and x[1] or 0
            args = {
                0x26: 2,
                0xFC0C: 3,
                0xFC0E: 3,
                0xFC0F: 2,
                0xFC10: 0,
                0xFC11: 3,
            }[op]
            a = s.popn(args)
            s.flush_call()
            v = [n(e) if e.t in (I32, I64) else e.c for e in a]
            if op == 0x26:
                s.emit("_tset(T%d,%s,%s)" % (k, v[0], v[1]))
            elif op == 0xFC0C:
                s.emit("_tinit(T%d,ES%d,%s)" % (x[2], k, ",".join(v)))
            elif op == 0xFC0E:
                s.emit("_tcopy(T%d,T%d,%s)" % (k, x[2], ",".join(v)))
            elif op == 0xFC11:
                s.emit("_tfill(T%d,%s)" % (k, ",".join(v)))
            else:
                a64 = mg.taddr[k] == I64
                t = s.newtmp(I64 if a64 else I32)
                if op == 0xFC0F:
                    s.emit("%s = _tgrow%s(TX%d,%s,%s)" % (t, "64" if a64 else "", k, v[0], v[1]))
                else:
                    s.emit("%s = len(T%d)" % (t, k))
                push(E(t, I64 if a64 else I32, M64 if a64 else M32, k=True))
            return
        if op == 0xFC0A:
            nn = pop()
            src = pop()
            dst = pop()
            s.flush_mem()
            s.emit(
                "_mcopy(%s,%s,%s,%s,%s)" % (mg.mname(x[1]), mg.mname(x[2]), n(dst), n(src), n(nn))
            )
            return
        if op == 0xFC08:
            nn = pop()
            src = pop()
            dst = pop()
            mg.dused.add(x[1])
            s.flush_mem()
            s.emit("_minit(%s,D%d,%s,%s,%s)" % (mg.mname(x[2]), x[1], n(dst), n(src), n(nn)))
            return
        if op == 0xFC09:
            mg.dused.add(x[1])
            s.globals_set.add("D%d" % x[1])
            s.emit("D%d = b''" % x[1])
            return
        if op == 0xFC0B:
            nn = pop()
            v = pop()
            dst = pop()
            s.flush_mem()
            s.emit("_mfill(%s,%s,%s,%s)" % (mg.mname(x[1]), n(dst), n(v), n(nn)))
            return
        raise NotImplementedError("opcode 0x%x" % op)


# ----------------------------------------------------------------------------- module compiler
class ModGen:
    def __init__(s, mod, fb=None):
        s.mod = mod
        s.fb = _rt.FBITS if fb is None else fb  # f32/f64 as int bit patterns (host quiets a sNaN)
        s.types = mod["types"]
        im = mod["imports"]
        s.fimports = [i for i in im if i[0] == "func"]
        s.nimp = len(s.fimports)
        s.gimports = [i for i in im if i[0] == "global"]
        s.simd = False  # a SIMD instruction was compiled: the module imports simd's helpers
        s.dused = set()  # data segments that memory.init / data.drop name
        s.tagimports = [i for i in im if i[0] == "tag"]
        s.tagtypes = [i[3] for i in s.tagimports] + mod["tags"]  # type index of every tag
        s.timports = [i for i in im if i[0] == "table"]
        s.mimports = [i for i in im if i[0] == "mem"]
        s.ttypes = mod["ttypes"]
        s.maddr = mod["maddr"]  # address type of every memory / table (0x7F or 0x7E)
        s.taddr = mod["taddr"]
        # functions containing return_call / return_call_indirect: body `f<i>_b` + trampoline wrapper `f<i>`
        s.tc = {
            s.nimp + k
            for k, ins in enumerate(mod["insns"])
            if any(x[0] in (0x12, 0x13, 0x15) for x in ins)
        }
        s.gtypes = [i[3][0] for i in s.gimports] + [g[0] for g in mod["globals"]]
        s.gmut = [i[3][1] for i in s.gimports] + [g[1] for g in mod["globals"]]
        s.ginit = [("i32", 0)] * len(s.gimports) + [g[2] for g in mod["globals"]]
        # a mutable global that is imported or exported is a shared cell `C<i>`; others stay plain `G<i>`
        s.cells = {
            i for i, m in enumerate(s.gmut) if m and (i < len(s.gimports) or s.exported(3, i))
        }

    def exported(s, kind, i):
        return any(k == kind and x == i for _, k, x in s.mod["exports"])

    def ftype(s, fi):
        if fi < s.nimp:
            return s.types[s.fimports[fi][3]]
        return s.types[s.mod["funcs"][fi - s.nimp]]

    def sig(s, ps, rs):
        return ">".join(",".join(str(valtype_key(s.types, t)) for t in ts) for ts in (ps, rs))

    def fname(s, fi):
        if fi < s.nimp:
            return "I%d" % fi
        return "f%d" % fi

    def mname(s, mi, base="M"):
        return base if mi == 0 else "%s%d" % (base, mi)

    def gname(s, gi):
        return "C%d.v" % gi if gi in s.cells else "G%d" % gi

    def val(s, init):
        if init[0] in ("add", "sub", "mul"):
            o = {"add": "+", "sub": "-", "mul": "*"}[init[0]]
            return "((%s %s %s) & %#x)" % (
                s.val(init[2]),
                o,
                s.val(init[3]),
                M32 if init[1] == "i32" else M64,
            )
        if init[0] == "global":
            return s.gname(init[1])
        if init[0] == "func":
            return s.fname(init[1])
        if init[0] in ("f32", "f64"):
            return flit(init[1], s.fb, 32 if init[0] == "f32" else 64)
        return "None" if init[0] == "null" else repr(init[1])

    def gen(s):
        mod = s.mod
        ng = len(s.gimports)
        # D18: helpers live in waboros.targets.pyc.runtime and are imported, not pasted
        out = [
            "from waboros.targets.pyc.runtime import *",
        ]
        nmi = len(s.mimports)
        for k, mem in enumerate(mod["mems"], nmi):
            a64 = s.maddr[k] == I64
            out.append(
                "%s = _mem64(%d)" % (s.mname(k), mem[0])
                if a64
                else "%s = _Mem(%d * 65536)" % (s.mname(k), mem[0])
            )
            out.append("%s.max = %r" % (s.mname(k), mem[1]))
            cap = MAXPAGES64 if a64 else 65536
            out.append(
                "%s = %d" % (s.mname(k, "_MAXPAGES"), cap if mem[1] is None else min(mem[1], cap))
            )
        L = ["def instantiate(imports):"]
        gl = []  # module globals that instantiate assigns
        gi = []  # global initializers: after the function imports (ref.func may name one)
        for i, (t, mut, init) in enumerate(zip(s.gtypes, s.gmut, s.ginit)):
            if i < ng:
                im = s.gimports[i]
                L.append(
                    "    %s = imports[%r][%r]%s"
                    % (
                        "C%d" % i if i in s.cells else "G%d" % i,
                        im[1],
                        im[2],
                        "" if i in s.cells else ".get()",
                    )
                )
                gl.append("C%d" % i if i in s.cells else "G%d" % i)
                continue
            first = init[0] not in ("global", "func", "add", "sub", "mul")
            if i in s.cells:
                out.append("C%d = _Cell(%s)" % (i, s.val(init) if first else "0"))
            else:
                out.append("G%d = %s" % (i, s.val(init) if first else "0"))
                if not first:
                    gl.append("G%d" % i)
            if not first:
                gi.append("    %s = %s" % (s.gname(i), s.val(init)))
        for j in range(len(s.tagimports), len(s.tagtypes)):
            out.append("TG%d = _Tag(%r)" % (j, tuple(mod["types"][s.tagtypes[j]][0])))
        for ti, (mn, mx) in enumerate(mod["tables"]):
            k = len(s.timports) + ti
            big = mn > 10000000  # above D27's cap: a checked allocation (S0009), else as before
            out.append(("T%d = _tnew(%d)" if big else "T%d = [None] * %d") % (k, mn))
            out.append("TX%d = _Table(T%d, %r)" % (k, k, mx))
        for i, im in enumerate(s.timports):
            L.append("    TX%d = imports[%r][%r]" % (i, im[1], im[2]))
            L.append("    T%d = TX%d.t" % (i, i))
            gl += ["TX%d" % i, "T%d" % i]
        for i, im in enumerate(s.tagimports):
            L.append("    TG%d = imports[%r][%r]" % (i, im[1], im[2]))
            gl.append("TG%d" % i)
        for k, im in enumerate(s.mimports):
            mn, mp = s.mname(k), s.mname(k, "_MAXPAGES")
            L.append("    %s = imports[%r][%r]" % (mn, im[1], im[2]))
            if s.maddr[k] == I64:
                L.append(
                    "    %s = %d if %s.max is None else min(%s.max, %d)"
                    % (mp, MAXPAGES64, mn, mn, MAXPAGES64)
                )
            else:
                L.append("    %s = 65536 if %s.max is None else %s.max" % (mp, mn, mn))
            gl += [mn, mp]
        for i in range(s.nimp):
            out.append("I%d = None" % i)
            gl.append("I%d" % i)
        for k, ((locs, _), tidx) in enumerate(zip(mod["codes"], mod["funcs"])):
            out.append(FuncGen(s, s.nimp + k, tidx, locs, mod["insns"][k]).gen())
            out.append("f%d.s = %r" % (s.nimp + k, s.sig(*s.types[tidx])))
            if s.nimp + k in s.tc:
                out.append("f%d.t = f%d_b" % (s.nimp + k, s.nimp + k))
        for i, im in enumerate(s.fimports):
            L.append("    I%d = imports[%r][%r]" % (i, im[1], im[2]))
        L += gi
        for ti, init in enumerate(mod["tinit"]):
            if init is not None:
                k = len(s.timports) + ti
                L.append("    T%d[:] = [%s] * len(T%d)" % (k, s.val(init), k))
        for i, ((ti, off, _, items), mode) in enumerate(
            zip(mod["elem_info"], mod["elem_mode"], strict=True)
        ):
            gl.append("ES%d" % i)
            L.append("    ES%d = [%s]" % (i, ", ".join(s.val(e) for e in items)))
            if mode == "active":
                L.append(
                    "    _eseg%s(T%d, %s, ES%d)"
                    % ("64" if s.taddr[ti] == I64 else "", ti, s.val(off), i)
                )
            if mode != "passive":
                L.append("    ES%d = []" % i)
        if gl:
            L.insert(1, "    global " + ", ".join(gl))
        dg = []
        for i, (off, d) in enumerate(mod["datas"]):
            if off is not None:
                mi = mod["data_mem"][i]
                L.append(
                    "    _dseg%s(%s, %s, %r)"
                    % ("64" if s.maddr[mi] == I64 else "", s.mname(mi), s.val(off), d)
                )
            if i in s.dused:
                dg.append("D%d" % i)
                L.append("    D%d = %r" % (i, d if off is None else b""))
        if dg:
            L.insert(1, "    global " + ", ".join(dg))
        if mod["start"] is not None:
            L.append("    %s()" % s.fname(mod["start"]))
        ex = []
        for n, k, i in mod["exports"]:
            if k == 0:
                ex.append("%r: %s" % (n, s.fname(i)))
            elif k == 1:
                ex.append("%r: TX%d" % (n, i))
            elif k == 2:
                ex.append("%r: %s" % (n, s.mname(i)))
            elif k == 3:
                ex.append(
                    "%r: C%d" % (n, i) if i in s.cells else "%r: _Global(lambda: G%d)" % (n, i)
                )
            elif k == 4:
                ex.append("%r: TG%d" % (n, i))
        L.append("    return {%s}" % ", ".join(ex))
        out.append("\n".join(L))
        if s.simd:
            out.insert(1, "from waboros.targets.pyc.simd import *")
        return "\n\n".join(out) + "\n"


def generate(mod: dict, fb: bool | None = None) -> str:
    """Source for a module that `parse` and `validate` have processed. `fb`: carry f32/f64 as int
    bit patterns (default: only on a host whose floats quiet a signalling NaN, `runtime.FBITS`)."""
    return ModGen(mod, fb).gen()
