# pyright: basic, reportArgumentType=false, reportOptionalMemberAccess=false, reportAttributeAccessIssue=false
"""Module validation (WebAssembly core spec, "Validation" and the appendix algorithm).

`validate(mod)` takes the dict of `frontend.binary.parse`, decodes every function body once (stored
in `mod["insns"]`, so a code generator does not decode again) and raises `Invalid` for a module that
breaks a validation rule. A valid construct this front end cannot validate (instructions outside
the set it knows) ends validation of that module without a verdict (it is left to the code
generator as before): never `Invalid`, because a wrong "invalid" on a valid module would be a wrong
kind. Constructs the reader does not decode are its `NotImplementedError`. A module with GC types
or instructions is validated in full (`mod["gc"]`: no target runs it yet, they refuse it; S0013).

Basic pyright mode: the module dict and the instruction tuples are untyped data of the reader.
"""

from waboros.frontend import simd
from waboros.frontend.binary import NULLABLE, decode
from waboros.frontend.types import (
    ANY,
    EXTERN,
    I31,
    isref,
    match,
    match_comp,
    nn,
    top,
)

I32, I64, F32, F64, FUNCREF, EXTERNREF = 0x7F, 0x7E, 0x7D, 0x7C, 0x70, 0x6F
EXNREF, NOEXNREF = 0x69, 0x74  # (ref null exn), (ref null noexn); heap types -23 and -12
EXN = (0, -23)  # (ref exn)
UNKNOWN = 0  # the type of a value popped from a polymorphic stack
MAX_PAGES, MAX_TABLE = 65536, 0xFFFFFFFF
MAX_PAGES64, MAX_TABLE64 = 1 << 48, 0xFFFFFFFFFFFFFFFF  # i64 memories and tables (memory64)


class Invalid(Exception):
    """The module is well-formed but breaks a validation rule."""


class _Unsupported(Exception):
    """A construct the validator does not cover: stop validating this module."""


# op -> (operand types, top of the stack first; result type or 0). Plain numeric, constant, load and
# store instructions: one table, one loop.
OPSIG: dict[int, tuple[tuple[int, ...], int]] = {
    0x41: ((), I32),
    0x42: ((), I64),
    0x43: ((), F32),
    0x44: ((), F64),
    0x45: ((I32,), I32),
    0x50: ((I64,), I32),
    0xA7: ((I64,), I32),
}
# loads and stores: op -> (type, natural alignment as a power of two)
LOADS = {
    0x28: (I32, 2),
    0x29: (I64, 3),
    0x2A: (F32, 2),
    0x2B: (F64, 3),
    0x2C: (I32, 0),
    0x2D: (I32, 0),
    0x2E: (I32, 1),
    0x2F: (I32, 1),
    0x30: (I64, 0),
    0x31: (I64, 0),
    0x32: (I64, 1),
    0x33: (I64, 1),
    0x34: (I64, 2),
    0x35: (I64, 2),
}
STORES = {
    0x36: (I32, 2),
    0x37: (I64, 3),
    0x38: (F32, 2),
    0x39: (F64, 3),
    0x3A: (I32, 0),
    0x3B: (I32, 1),
    0x3C: (I64, 0),
    0x3D: (I64, 1),
    0x3E: (I64, 2),
}
ALIGN = {op: a for op, (_, a) in (*LOADS.items(), *STORES.items())}
for _op, (_t, _a) in LOADS.items():
    OPSIG[_op] = ((I32,), _t)
for _op, (_t, _a) in STORES.items():
    OPSIG[_op] = ((_t, I32), 0)


def _numeric() -> None:
    def add(first: int, last: int, pops: tuple[int, ...], res: int) -> None:
        for op in range(first, last + 1):
            OPSIG[op] = (pops, res)

    for base, t in ((0x46, I32), (0x51, I64)):  # eq .. ge_u: 10 comparisons
        add(base, base + 9, (t, t), I32)
    add(0x5B, 0x60, (F32, F32), I32)
    add(0x61, 0x66, (F64, F64), I32)
    add(0x67, 0x69, (I32,), I32)
    add(0x6A, 0x78, (I32, I32), I32)
    add(0x79, 0x7B, (I64,), I64)
    add(0x7C, 0x8A, (I64, I64), I64)
    add(0x8B, 0x91, (F32,), F32)
    add(0x92, 0x98, (F32, F32), F32)
    add(0x99, 0x9F, (F64,), F64)
    add(0xA0, 0xA6, (F64, F64), F64)
    # conversions: (first, last, operand, result), and the two sign-extension groups
    for first, last, a, r in (
        (0xA8, 0xA9, F32, I32),
        (0xAA, 0xAB, F64, I32),
        (0xAC, 0xAD, I32, I64),
        (0xAE, 0xAF, F32, I64),
        (0xB0, 0xB1, F64, I64),
        (0xB2, 0xB3, I32, F32),
        (0xB4, 0xB5, I64, F32),
        (0xB6, 0xB6, F64, F32),
        (0xB7, 0xB8, I32, F64),
        (0xB9, 0xBA, I64, F64),
        (0xBB, 0xBB, F32, F64),
        (0xBC, 0xBC, F32, I32),
        (0xBD, 0xBD, F64, I64),
        (0xBE, 0xBE, I32, F32),
        (0xBF, 0xBF, I64, F64),
        (0xC0, 0xC1, I32, I32),
        (0xC2, 0xC4, I64, I64),
        (0xFC00, 0xFC01, F32, I32),  # saturating truncation
        (0xFC02, 0xFC03, F64, I32),
        (0xFC04, 0xFC05, F32, I64),
        (0xFC06, 0xFC07, F64, I64),
    ):
        add(first, last, (a,), r)


_numeric()


def _limits(lim: tuple[int, int | None], cap: int, what: str) -> None:
    mn, mx = lim
    if mn > cap or (mx is not None and mx > cap):
        raise Invalid(f"{what} size must be at most {cap}")
    if mx is not None and mx < mn:
        raise Invalid("size minimum must not be greater than maximum")


_OK_HEAP = (-16, -17, -23, -12)  # heap types every target runs: func extern exn noexn


# instructions that call, throw, or that either JIT lowers to a host call or service: a safepoint
# whose operand stack gets a reference mask (a superset is harmless; tail calls are not safepoints)
_SAFEPOINTS = frozenset((0x08, 0x0A, 0x10, 0x11, 0x14, 0x40, *range(0xFC08, 0xFC10), 0xFC11))
PACKED = (0x78, 0x77)  # storage types i8, i16


def _root(mod: dict, t) -> bool:
    """`t` is a reference type of the any or the extern hierarchy (what a collector must find)."""
    return isref(t) and top(mod, nn(t)[1]) in (ANY, EXTERN)


def _vt(t, mod: dict):
    """A type index in a reference type must exist. A GC heap type marks the module `gc`: no
    target runs it yet (S0013)."""
    if isref(t):
        h = nn(t)[1]
        if h >= len(mod["types"]):
            raise Invalid("unknown type")
        if h < 0 and h not in _OK_HEAP:
            mod["gc"] = True
        if _root(mod, t):
            mod["anyext"] = True
    return t


def _rmatch(mod: dict, a: list, b: list) -> bool:
    return len(a) == len(b) and all(match(mod, x, y) for x, y in zip(a, b, strict=True))


def _ref(c, ht: int, null: bool = True):
    """Reference type of a heap type (a type index or an abstract type), nullable or not."""
    if ht >= len(c.types):
        raise Invalid("unknown type")
    if ht < 0 and ht not in _OK_HEAP:
        c.mod["gc"] = True
    t = NULLABLE.get(ht, (1, ht)) if null else (0, ht)
    if _root(c.mod, t):
        c.mod["anyext"] = True
    return t


class _Ctx:
    """The validation context of a module (spec: C)."""

    def __init__(self, mod: dict) -> None:
        self.mod = mod
        self.types = mod["types"]
        imps = mod["imports"]
        self.ftidx = [i[3] for i in imps if i[0] == "func"] + mod["funcs"]
        self.funcs = [self.types[t] for t in self.ftidx]
        self.tags = [i[3] for i in imps if i[0] == "tag"] + mod["tags"]  # type index of every tag
        self.ttypes = mod["ttypes"]
        self.maddr = mod["maddr"]  # address type of every memory / table
        self.taddr = mod["taddr"]
        self.nmems = sum(1 for i in imps if i[0] == "mem") + len(mod["mems"])
        self.gtypes = [i[3][0] for i in imps if i[0] == "global"] + [g[0] for g in mod["globals"]]
        self.gmut = [i[3][1] for i in imps if i[0] == "global"] + [g[1] for g in mod["globals"]]
        self.etypes = [e[2] for e in mod["elem_info"]]
        self.ndatas = len(mod["datas"])
        self.refs: set[int] = set()
        self.masks = False  # record reference masks (`mod["refmasks"]`)


def _comp(mod: dict, x: int, kind: str) -> tuple:
    """Composite type `x`, which must be a `kind` ("struct" / "array") type."""
    if x >= len(mod["types"]):
        raise Invalid("unknown type")
    comp = mod["tdefs"][x][0]
    if comp[0] != kind:
        raise Invalid(f"{kind} type expected")
    return comp


def _unpack(s):
    return I32 if s in PACKED else s


def _defaultable(s) -> bool:
    return s.__class__ is not tuple or bool(s[0])  # not a non-nullable reference


def _conv(mod: dict, sub: int, x):
    """Result of any.convert_extern (26) / extern.convert_any (27) on an operand of type `x`
    (UNKNOWN: unreachable code, then `(ref any)` / `(ref extern)`, as Wasmtime); the nullability
    is kept."""
    src, dst = (0x6F, ANY) if sub == 26 else (0x6E, EXTERN)
    if not x:
        return (0, dst)
    if not match(mod, x, src):
        raise Invalid("type mismatch")
    return NULLABLE[dst] if nn(x)[0] else (0, dst)


def _gcsig(c: _Ctx, ins: tuple, room: int):
    """(operand types, bottom first; result types) of a 0xFB instruction whose typing does not
    depend on the operand: not ref.test / ref.cast / br_on_cast* or the conversions. `room`
    bounds the operands array.new_fixed lists (more cannot be popped from a finite stack)."""
    m, sub, a = c.mod, ins[0] - 0xFB00, ins[1:]
    m["gc"] = True
    if sub == 15:  # array.len
        return [0x6A], [I32]
    if sub == 28:
        return [I32], [(0, I31)]
    if sub in (29, 30):
        return [0x6C], [I32]
    if sub <= 5:
        fs = _comp(m, a[0], "struct")[1]
        if sub <= 1:
            if sub and not all(_defaultable(f[0]) for f in fs):
                raise Invalid("field type is not defaultable")
            return ([] if sub else [_unpack(f[0]) for f in fs]), [(0, a[0])]
        if a[1] >= len(fs):
            raise Invalid("unknown field")
        st, mut = fs[a[1]]
        if sub == 5:
            if not mut:
                raise Invalid("immutable field")
            return [(1, a[0]), _unpack(st)], []
        if (sub == 2) == (st in PACKED):  # get: unpacked only; get_s / get_u: packed only
            raise Invalid("type mismatch")
        return [(1, a[0])], [_unpack(st)]
    st, mut = _comp(m, a[0], "array")[1]
    e, ref, new = _unpack(st), (1, a[0]), [(0, a[0])]
    if sub in (11, 12, 13):
        if (sub == 11) == (st in PACKED):
            raise Invalid("type mismatch")
        return [ref, I32], [e]
    if sub in (14, 16, 17, 18, 19) and not mut:
        raise Invalid("immutable array")
    if sub in (9, 18) and isref(st):
        raise Invalid("array type is not numeric or vector")
    if sub in (9, 18) and a[1] >= c.ndatas:
        raise Invalid("unknown data segment")
    if sub in (10, 19):
        if a[1] >= len(c.etypes):
            raise Invalid("unknown elem segment")
        if not match(m, c.etypes[a[1]], st):
            raise Invalid("type mismatch")
    if sub == 6:
        return [e, I32], new
    if sub == 7:
        if not _defaultable(st):
            raise Invalid("array type is not defaultable")
        return [I32], new
    if sub == 8:
        return [e] * min(a[1], room), new
    if sub in (9, 10):
        return [I32, I32], new
    if sub == 14:
        return [ref, I32, e], []
    if sub == 16:
        return [ref, I32, e, I32], []
    if sub == 17:
        src = _comp(m, a[1], "array")[1][0]
        if not match(m, src, st):
            raise Invalid("array types do not match")
        return [ref, I32, (1, a[1]), I32, I32], []
    return [ref, I32, I32, I32], []  # init_data, init_elem


def _frefs(e: tuple):
    """The function indices a constant expression names (spec: C.refs)."""
    if e[0] == "func":
        yield e[1]
    elif e[0] == "gc":
        for a in e[2]:
            yield from _frefs(a)


def _const(c: _Ctx, e: tuple, nglobals: int):
    """Type of a constant expression (parser tuples); only globals below `nglobals` are visible."""
    k = e[0]
    if k == "bad":
        raise Invalid("type mismatch")
    if k in ("add", "sub", "mul"):
        t = I32 if e[1] == "i32" else I64
        if _const(c, e[2], nglobals) != t or _const(c, e[3], nglobals) != t:
            raise Invalid("type mismatch")
        return t
    if k == "i32":
        return I32
    if k == "i64":
        return I64
    if k == "f32":
        return F32
    if k == "f64":
        return F64
    if k == "v128":
        return simd.V
    if k == "func":
        if e[1] >= len(c.funcs):
            raise Invalid("unknown function")
        return (0, c.ftidx[e[1]])
    if k == "null":
        return _ref(c, e[1])
    if k == "gc":  # struct.new .. extern.convert_any; the reader took the operands
        c.mod["gc"] = True  # no target evaluates these yet: every one refuses the module
        sub = e[1][0] - 0xFB00
        if sub in (26, 27):
            return _conv(c.mod, sub, _const(c, e[2][0], nglobals))
        pops, res = _gcsig(c, e[1], len(e[2]))
        for a, t in zip(e[2], pops, strict=True):
            if not match(c.mod, _const(c, a, nglobals), t):
                raise Invalid("type mismatch")
        return res[0]
    if e[1] >= nglobals:
        raise Invalid("unknown global")
    if c.gmut[e[1]]:
        raise Invalid("constant expression required")
    return c.gtypes[e[1]]


def _ftype(mod: dict, i: int):
    """Parameter and result types of type `i`, which must be a function type."""
    if i >= len(mod["types"]):
        raise Invalid("unknown type")
    if mod["tdefs"][i][0][0] != "func":
        raise Invalid("function type expected")
    return mod["types"][i]


def _check_types(mod: dict) -> None:
    """The type section (spec: GC, "Types" validation): references to types of the own or an
    earlier rec group, supertypes defined earlier and not final, and a subtype that matches its
    supertype. Two passes: matching reads supertype chains, which the first pass makes safe."""
    tdefs = mod["tdefs"]
    for i, (comp, sup, _, _, ge) in enumerate(tdefs):
        if comp[0] == "func":
            ts = (*comp[1], *comp[2])
        else:
            ts = [f[0] for f in comp[1]] if comp[0] == "struct" else [comp[1][0]]
        for t in ts:
            if isref(t) and nn(t)[1] >= ge:
                raise Invalid("unknown type")
        if len(sup) > 1:
            raise Invalid("multiple supertypes")
        if sup and sup[0] >= i:
            raise Invalid("unknown type")
    for comp, sup, _, _, _ in tdefs:
        if sup:
            if tdefs[sup[0]][2]:
                raise Invalid("sub type: supertype is final")
            if not match_comp(mod, comp, tdefs[sup[0]][0]):
                raise Invalid("sub type does not match super type")


def validate(mod: dict) -> None:
    mod["validated"] = False  # True only after a full verdict; JIT targets require it (S0002)
    # struct / array types, declared supertypes, GC heap types and instructions: validated in
    # full, but no target runs them yet and each refuses `gc` (S0013)
    mod["gc"] = any(c[0] != "func" or sp for c, sp, _, _, _ in mod["tdefs"])
    mod["anyext"] = any(c[0] != "func" for c, _, _, _, _ in mod["tdefs"])  # for refmasks
    try:
        _validate(mod)
    except _Unsupported:
        return
    mod["validated"] = True


def _validate(mod: dict) -> None:
    # decode all bodies first: an unsupported instruction wins over any verdict
    mod["insns"] = [decode(body) for _, body in mod["codes"]]
    types = mod["types"]
    _check_types(mod)
    for ps, rs in types:
        for t in (*ps, *rs):
            _vt(t, mod)
    imps = mod["imports"]
    for im in imps:
        if im[0] == "global":
            _vt(im[3][0], mod)
    for g in mod["globals"]:
        _vt(g[0], mod)
    for t in (*mod["ttypes"], *(e[2] for e in mod["elem_info"])):
        _vt(t, mod)
    for locs, _ in mod["codes"]:
        for t in locs:
            _vt(t, mod)
    for i in (*(im[3] for im in imps if im[0] == "func"), *mod["funcs"]):
        _ftype(mod, i)
    c = _Ctx(mod)
    nimpg = sum(1 for i in imps if i[0] == "global")
    tl = [im[3] for im in imps if im[0] == "table"] + mod["tables"]
    ml = [im[3] for im in imps if im[0] == "mem"] + mod["mems"]
    for lim, at in zip(tl, c.taddr, strict=True):
        _limits(lim, MAX_TABLE if at == I32 else MAX_TABLE64, "table")
    for lim, at in zip(ml, c.maddr, strict=True):
        _limits(lim, MAX_PAGES if at == I32 else MAX_PAGES64, "memory")
    # function references allowed in function bodies: those named outside of them (spec: C.refs)
    for _, _, _, items in mod["elem_info"]:
        for e in items:
            c.refs.update(_frefs(e))
    for e in (*(g[2] for g in mod["globals"]), *mod["tinit"]):
        if e is not None:
            c.refs.update(_frefs(e))
    for _, kind, idx in mod["exports"]:
        if kind == 0:
            c.refs.add(idx)
    for i, (t, _, init) in enumerate(mod["globals"]):
        if not match(c.mod, _const(c, init, nimpg + i), t):
            raise Invalid("type mismatch")
    ng = len(c.gtypes)
    for t, init in zip(
        mod["ttypes"][len(c.ttypes) - len(mod["tables"]) :], mod["tinit"], strict=True
    ):
        if init is None:
            if t.__class__ is tuple and not t[0]:  # a non-nullable table needs an initializer
                raise Invalid("type mismatch")
        elif not match(c.mod, _const(c, init, nimpg), t):
            raise Invalid("type mismatch")
    for ti in c.tags:  # an exception tag: a function type without results
        if _ftype(mod, ti)[1]:
            raise Invalid("non-empty tag result type")
    seen = set()
    counts = (len(c.funcs), len(c.ttypes), c.nmems, ng, len(c.tags))
    for name, kind, idx in mod["exports"]:
        if name in seen:
            raise Invalid("duplicate export name")
        seen.add(name)
        if idx >= counts[kind]:
            raise Invalid("unknown export index")
    if mod["start"] is not None:
        if mod["start"] >= len(c.funcs):
            raise Invalid("unknown function")
        if c.funcs[mod["start"]] != ([], []):
            raise Invalid("start function")
    for ti, off, rt, items in mod["elem_info"]:
        if ti is not None:
            if ti >= len(c.ttypes):
                raise Invalid("unknown table")
            if not match(c.mod, rt, c.ttypes[ti]):
                raise Invalid("type mismatch")
            if _const(c, off, ng) != c.taddr[ti]:
                raise Invalid("type mismatch")
        for e in items:
            if not match(c.mod, _const(c, e, ng), rt):
                raise Invalid("type mismatch")
    for (off, _), mi in zip(mod["datas"], mod["data_mem"], strict=True):
        if mi is not None:
            if mi >= c.nmems:
                raise Invalid("unknown memory")
            if _const(c, off, ng) != c.maddr[mi]:
                raise Invalid("type mismatch")
    nimpf = len(c.funcs) - len(mod["funcs"])
    c.masks = bool(mod["anyext"])  # an any- or extern-hierarchy type is declared
    mod["refmasks"] = []
    for k, ((locs, _), insns) in enumerate(zip(mod["codes"], mod["insns"], strict=True)):
        ps, rs = c.funcs[nimpf + k]
        masks = _body(c, ps + locs, len(ps), rs, insns)
        if c.masks:
            mod["refmasks"].append(masks)


def _body(c: _Ctx, lt: list, nparams: int, results: list, insns: list[tuple]) -> dict[int, int]:
    """Validate a function body. Returns its reference masks: for each safepoint (`_SAFEPOINTS`,
    every 0xFB instruction) at instruction index `pc` of reachable code, a bitmask with bit i set
    when operand-stack position i (from the bottom of the function's stack, across all open
    blocks, before the instruction pops) holds an any- or extern-hierarchy reference. Empty if
    `c.masks` is off."""
    masks: dict[int, int] = {}
    types, funcs, ttypes, gtypes = c.types, c.funcs, c.ttypes, c.gtypes
    nl = len(lt)
    vals: list = []
    ctl: list[list] = [
        [0x02, [], results, 0, False, 0]
    ]  # frames: opcode, start, end, height, unreachable, height of `inits`
    # local initialisation: a local of a non-nullable type is set only after a local.set / tee; the
    # sets of a block are undone when it ends (spec appendix, "initialization of locals")
    init = [j < nparams or lt[j].__class__ is not tuple or bool(lt[j][0]) for j in range(nl)]
    inits: list[int] = []

    def pop(expect=UNKNOWN):
        f = ctl[-1]
        if len(vals) == f[3]:
            if f[4]:
                return UNKNOWN
            raise Invalid("type mismatch")
        x = vals.pop()
        if expect and x and not match(c.mod, x, expect):
            raise Invalid("type mismatch")
        return x

    def popn(ts: list) -> list:
        return [pop(t) for t in reversed(ts)][::-1]

    def label(n: int) -> list:
        if n >= len(ctl):
            raise Invalid("unknown label")
        f = ctl[-1 - n]
        return f[1] if f[0] == 0x03 else f[2]

    def enter(op: int, ps: list, rs: list) -> None:
        popn(ps)
        ctl.append([op, ps, rs, len(vals), False, len(inits)])
        vals.extend(ps)

    def exit_() -> list:
        f = ctl[-1]
        popn(f[2])
        if len(vals) != f[3]:
            raise Invalid("type mismatch")
        while len(inits) > f[5]:
            init[inits.pop()] = False
        return ctl.pop()

    def unreachable() -> None:
        f = ctl[-1]
        del vals[f[3] :]
        f[4] = True

    def table(t: int):
        if t >= len(ttypes):
            raise Invalid("unknown table")
        return ttypes[t]

    def need_mem(mi: int = 0) -> int:  # the address type of memory `mi`
        if mi >= c.nmems:
            raise Invalid("unknown memory")
        return c.maddr[mi]

    for pc, ins in enumerate(insns):
        op = ins[0]
        if c.masks and (op in _SAFEPOINTS or 0xFB00 <= op < 0xFC00) and not any(f[4] for f in ctl):
            masks[pc] = sum(1 << i for i, t in enumerate(vals) if _root(c.mod, t))
        sig = OPSIG.get(op)
        if sig is not None:
            if 0x28 <= op <= 0x3E:
                at = need_mem(ins[3])
                if ins[2] > ALIGN[op]:
                    raise Invalid("alignment must not be larger than natural")
                if ins[1] > 0xFFFFFFFF and at == I32:
                    raise Invalid("offset out of range")
                if at != I32:  # the address is the last operand type
                    sig = (sig[0][:-1] + (at,), sig[1])
            fr = ctl[-1]
            for t in sig[0]:
                if len(vals) > fr[3]:
                    x = vals.pop()
                    if x != t and x:
                        raise Invalid("type mismatch")
                elif not fr[4]:
                    raise Invalid("type mismatch")
            if sig[1]:
                vals.append(sig[1])
        elif op == 0x20:
            if ins[1] >= nl:
                raise Invalid("unknown local")
            if not init[ins[1]]:
                raise Invalid("uninitialized local")
            vals.append(lt[ins[1]])
        elif op in (0x21, 0x22):
            if ins[1] >= nl:
                raise Invalid("unknown local")
            pop(lt[ins[1]])
            if not init[ins[1]]:
                init[ins[1]] = True
                inits.append(ins[1])
            if op == 0x22:
                vals.append(lt[ins[1]])
        elif op in (0x02, 0x03, 0x04, 0x1F):
            if op == 0x04:
                pop(I32)
            bt = ins[1]
            if bt[0] == "idx":
                bt = _ftype(c.mod, bt[1])
            for t in bt[1]:
                _vt(t, c.mod)
            if op == 0x1F:  # each clause: what it passes must match its label (outside the block)
                for kind, tag, lab in ins[2]:
                    if tag is not None and tag >= len(c.tags):
                        raise Invalid("unknown tag")
                    ts = [*types[c.tags[tag]][0]] if tag is not None else []
                    if kind & 1:
                        ts.append(EXN)
                    if not _rmatch(c.mod, ts, label(lab)):
                        raise Invalid("type mismatch")
            enter(op, bt[0], bt[1])
        elif op == 0x08:
            if ins[1] >= len(c.tags):
                raise Invalid("unknown tag")
            popn(types[c.tags[ins[1]]][0])
            unreachable()
        elif op == 0x0A:
            pop(EXNREF)
            unreachable()
        elif op == 0x05:
            f = exit_()
            if f[0] != 0x04:
                raise Invalid("else without if")
            ctl.append([0x05, f[1], f[2], len(vals), False, len(inits)])
            vals.extend(f[1])
        elif op == 0x0B:
            f = exit_()
            if f[0] == 0x04 and not _rmatch(c.mod, f[1], f[2]):
                raise Invalid("type mismatch")
            vals.extend(f[2])
        elif op == 0x0C:
            popn(label(ins[1]))
            unreachable()
        elif op == 0x0D:
            pop(I32)
            ts = label(ins[1])
            popn(ts)
            vals.extend(ts)
        elif op == 0x0E:
            pop(I32)
            arity = len(label(ins[2]))
            for n in ins[1]:
                ts = label(n)
                if len(ts) != arity:
                    raise Invalid("type mismatch")
                vals.extend(popn(ts))
            popn(label(ins[2]))
            unreachable()
        elif op == 0x0F:
            popn(results)
            unreachable()
        elif op == 0x00:
            unreachable()
        elif op == 0x01:
            pass
        elif op == 0x10:
            if ins[1] >= len(funcs):
                raise Invalid("unknown function")
            popn(funcs[ins[1]][0])
            vals.extend(funcs[ins[1]][1])
        elif op in (0x11, 0x13):
            if not match(c.mod, table(ins[2]), FUNCREF):
                raise Invalid("type mismatch")
            ps, rs = _ftype(c.mod, ins[1])
            pop(c.taddr[ins[2]])
            popn(ps)
            if op == 0x11:
                vals.extend(rs)
            else:
                if not _rmatch(c.mod, rs, results):
                    raise Invalid("type mismatch")
                unreachable()
        elif op == 0x12:
            if ins[1] >= len(funcs):
                raise Invalid("unknown function")
            ps, rs = funcs[ins[1]]
            if not _rmatch(c.mod, rs, results):
                raise Invalid("type mismatch")
            popn(ps)
            unreachable()
        elif op in (0x14, 0x15):
            ps, rs = _ftype(c.mod, ins[1])
            pop((1, ins[1]))
            if op == 0x15 and not _rmatch(c.mod, rs, results):
                raise Invalid("type mismatch")
            popn(ps)
            if op == 0x14:
                vals.extend(rs)
            else:
                unreachable()
        elif op == 0x1A:
            if pop() == simd.V:
                insns[pc] = (0x1A, simd.V)  # S0005: a v128 drop is told apart for the JIT slots
        elif op == 0x1B:
            pop(I32)
            if len(ins) == 2:  # typed select
                if len(ins[1]) != 1:
                    raise Invalid("invalid result arity")
                t = _vt(ins[1][0], c.mod)
                pop(t)
                pop(t)
                vals.append(t)
            else:
                a, b = pop(), pop()
                if isref(a) or isref(b) or (a and b and a != b):
                    raise Invalid("type mismatch")
                vals.append(a or b)
                if (a or b) == simd.V:
                    insns[pc] = (0x1B, [simd.V])  # the typed form, S0005
        elif op in (0x23, 0x24):
            if ins[1] >= len(gtypes):
                raise Invalid("unknown global")
            if op == 0x23:
                vals.append(gtypes[ins[1]])
            else:
                if not c.gmut[ins[1]]:
                    raise Invalid("immutable global")
                pop(gtypes[ins[1]])
        elif op == 0x25:
            t = table(ins[1])
            pop(c.taddr[ins[1]])
            vals.append(t)
        elif op == 0x26:
            t = table(ins[1])
            pop(t)
            pop(c.taddr[ins[1]])
        elif op == 0x3F:
            vals.append(need_mem(ins[1]))
        elif op == 0x40:
            at = need_mem(ins[1])
            pop(at)
            vals.append(at)
        elif op == 0xD0:
            vals.append(_ref(c, ins[1]))
        elif op == 0xD1:
            if pop() in (I32, I64, F32, F64):
                raise Invalid("type mismatch")
            vals.append(I32)
        elif op == 0xD2:
            if ins[1] >= len(funcs):
                raise Invalid("unknown function")
            if ins[1] not in c.refs:
                raise Invalid("undeclared function reference")
            vals.append((0, c.ftidx[ins[1]]))
        elif op == 0xD3:  # ref.eq
            c.mod["gc"] = True
            popn([0x6D, 0x6D])
            vals.append(I32)
        elif 0xFB14 <= op <= 0xFB17:  # ref.test / ref.cast (null)
            c.mod["gc"] = True
            rt = _ref(c, ins[1], op in (0xFB15, 0xFB17))
            x = pop()
            if x and not (isref(x) and top(c.mod, nn(x)[1]) == top(c.mod, ins[1])):
                raise Invalid("type mismatch")
            vals.append(I32 if op < 0xFB16 else rt)
        elif op in (0xFB18, 0xFB19):  # br_on_cast / br_on_cast_fail flags label rt1 rt2
            c.mod["gc"] = True
            fl, lab = ins[1], ins[2]
            rt1, rt2 = _ref(c, ins[3], fl & 1), _ref(c, ins[4], fl & 2)
            ts = label(lab)
            if not ts or not isref(ts[-1]) or not match(c.mod, rt2, rt1):
                raise Invalid("type mismatch")
            diff = _ref(c, ins[3], fl & 1 and not fl & 2)  # rt1 without what rt2 accepts
            if not match(c.mod, rt2 if op == 0xFB18 else diff, ts[-1]):
                raise Invalid("type mismatch")
            popn([*ts[:-1], rt1])
            vals.extend(ts[:-1])
            vals.append(diff if op == 0xFB18 else rt2)
        elif op in (0xFB1A, 0xFB1B):  # any.convert_extern / extern.convert_any
            c.mod["gc"] = True
            vals.append(_conv(c.mod, op - 0xFB00, pop()))
        elif 0xFB00 <= op < 0xFC00:
            pops, res = _gcsig(c, ins, len(vals) - ctl[-1][3] + 1)
            popn(pops)
            vals.extend(res)
        elif op == 0xD4:
            x = pop()
            if x and not isref(x):
                raise Invalid("type mismatch")
            vals.append((0, nn(x)[1]) if x else UNKNOWN)
        elif op in (0xD5, 0xD6):
            ts = label(ins[1])
            if op == 0xD5:
                x = pop()
                if x and not isref(x):
                    raise Invalid("type mismatch")
                popn(ts)
                vals.extend(ts)
                vals.append((0, nn(x)[1]) if x else UNKNOWN)
            else:
                if not ts or not isref(ts[-1]):
                    raise Invalid("type mismatch")
                pop((1, nn(ts[-1])[1]))
                popn(ts[:-1])
                vals.extend(ts[:-1])
        elif op == 0xFC0C:
            if ins[1] >= len(c.etypes):
                raise Invalid("unknown elem segment")
            if not match(c.mod, c.etypes[ins[1]], table(ins[2])):
                raise Invalid("type mismatch")
            at = c.taddr[ins[2]]
            popn([at, I32, I32])
        elif op == 0xFC0D:
            if ins[1] >= len(c.etypes):
                raise Invalid("unknown elem segment")
        elif op == 0xFC0E:
            if not match(c.mod, table(ins[2]), table(ins[1])):
                raise Invalid("type mismatch")
            ad, as_ = c.taddr[ins[1]], c.taddr[ins[2]]
            popn([ad, as_, I32 if I32 in (ad, as_) else I64])
        elif op == 0xFC0F:
            t = table(ins[1])
            at = c.taddr[ins[1]]
            pop(at)
            pop(t)
            vals.append(at)
        elif op == 0xFC10:
            table(ins[1])
            vals.append(c.taddr[ins[1]])
        elif op == 0xFC11:
            t = table(ins[1])
            at = c.taddr[ins[1]]
            pop(at)
            pop(t)
            pop(at)
        elif op in (0xFC08, 0xFC09):
            if ins[1] >= c.ndatas:
                raise Invalid("unknown data segment")
            if op == 0xFC08:
                popn([need_mem(ins[2]), I32, I32])
        elif op == 0xFC0A:  # memory.copy: the length has the narrower address type
            ad, as_ = need_mem(ins[1]), need_mem(ins[2])
            popn([ad, as_, I32 if I32 in (ad, as_) else I64])
        elif op == 0xFC0B:
            at = need_mem(ins[1])
            popn([at, I32, at])
        elif 0xFC13 <= op <= 0xFC16:  # wide arithmetic: two i64 results (S0008)
            for _ in range(4 if op < 0xFC15 else 2):
                pop(I64)
            vals.extend((I64, I64))
        elif op >= simd.BASE:
            simd.validate(ins, pop, vals.append, need_mem)
        else:
            raise _Unsupported(f"opcode {op:#x}")
    if ctl:
        raise Invalid("function body not closed")
    return masks
