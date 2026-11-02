# pyright: basic, reportArgumentType=false, reportOptionalMemberAccess=false, reportAttributeAccessIssue=false
"""Module validation (WebAssembly core spec, "Validation" and the appendix algorithm).

`validate(mod)` takes the dict of `frontend.binary.parse`, decodes every function body once (stored
in `mod["insns"]`, so a code generator does not decode again) and raises `Invalid` for a module that
breaks a validation rule. A valid construct this front end cannot validate (GC heap types,
instructions outside the core set) ends validation of that module without a verdict (it
is left to the code generator as before): never `Invalid`, because a wrong "invalid" on a valid
module would be a wrong kind. Constructs the reader does not decode (GC, SIMD,
exceptions, 64-bit memories, some table instructions) are its `NotImplementedError`.

Basic pyright mode: the module dict and the instruction tuples are untyped data of the reader.
"""

from waboros.frontend import simd
from waboros.frontend.binary import decode

I32, I64, F32, F64, FUNCREF, EXTERNREF = 0x7F, 0x7E, 0x7D, 0x7C, 0x70, 0x6F
UNKNOWN = 0  # the type of a value popped from a polymorphic stack
MAX_PAGES, MAX_TABLE = 65536, 0xFFFFFFFF


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
BULK3 = (I32, I32, I32)  # memory.copy / fill / init


def _limits(lim: tuple[int, int | None], cap: int, what: str) -> None:
    mn, mx = lim
    if mn > cap or (mx is not None and mx > cap):
        raise Invalid(f"{what} size must be at most {cap}")
    if mx is not None and mx < mn:
        raise Invalid("size minimum must not be greater than maximum")


def _vt(t, ntypes: int):
    """A type index in a reference type must exist; GC heap types are unsupported."""
    if t.__class__ is tuple:
        if t[1] >= ntypes:
            raise Invalid("unknown type")
        if t[1] < -17:
            raise _Unsupported("heap type")
    return t


def _isref(t) -> bool:
    return t.__class__ is tuple or t in (FUNCREF, EXTERNREF)


def _nn(t):
    """(nullable, heap type) of a reference type: funcref is (1, -16), externref (1, -17)."""
    return (1, -16) if t == FUNCREF else (1, -17) if t == EXTERNREF else t


def functype_key(types: list, i: int, stack: tuple = ()) -> tuple:
    """Structural identity of function type `i` (equal keys: equivalent types, also across modules);
    a reference back to a type being unfolded is ("^", depth)."""
    if i in stack:
        return ("^", len(stack) - stack.index(i))
    st = stack + (i,)
    return tuple(tuple(valtype_key(types, t, st) for t in ts) for ts in types[i])


def valtype_key(types: list, t, stack: tuple = ()):
    if t.__class__ is tuple and t[1] >= 0:
        return (t[0], functype_key(types, t[1], stack))
    return t


def _match(types: list, a, b) -> bool:
    """Value type `a` is a subtype of `b` (spec: matching; function types match if same shape)."""
    if a == b:
        return True
    if not (_isref(a) and _isref(b)):
        return False
    (an, ah), (bn, bh) = _nn(a), _nn(b)
    if an and not bn:
        return False
    return (
        ah == bh
        or (bh == -16 and ah >= 0)
        or (ah >= 0 and bh >= 0 and functype_key(types, ah) == functype_key(types, bh))
    )


def _rmatch(types: list, a: list, b: list) -> bool:
    return len(a) == len(b) and all(_match(types, x, y) for x, y in zip(a, b, strict=True))


def _ref(c, ht: int):
    """Reference type of a `ref.null` heap type (-16: func, -17: extern, else a type index)."""
    if ht >= len(c.types):
        raise Invalid("unknown type")
    if ht < -17:
        raise _Unsupported("heap type")
    return FUNCREF if ht == -16 else EXTERNREF if ht == -17 else (1, ht)


class _Ctx:
    """The validation context of a module (spec: C)."""

    def __init__(self, mod: dict) -> None:
        self.types = mod["types"]
        imps = mod["imports"]
        self.ftidx = [i[3] for i in imps if i[0] == "func"] + mod["funcs"]
        self.funcs = [self.types[t] for t in self.ftidx]
        self.ttypes = mod["ttypes"]
        self.nmems = sum(1 for i in imps if i[0] == "mem") + len(mod["mems"])
        self.gtypes = [i[3][0] for i in imps if i[0] == "global"] + [g[0] for g in mod["globals"]]
        self.gmut = [i[3][1] for i in imps if i[0] == "global"] + [g[1] for g in mod["globals"]]
        self.etypes = [e[2] for e in mod["elem_info"]]
        self.ndatas = len(mod["datas"])
        self.refs: set[int] = set()


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
    if e[1] >= nglobals:
        raise Invalid("unknown global")
    if c.gmut[e[1]]:
        raise Invalid("constant expression required")
    return c.gtypes[e[1]]


def validate(mod: dict) -> None:
    mod["validated"] = False  # True only after a full verdict; JIT targets require it (S0002)
    try:
        _validate(mod)
    except _Unsupported:
        return
    mod["validated"] = True


def _validate(mod: dict) -> None:
    # decode all bodies first: an unsupported instruction wins over any verdict
    mod["insns"] = [decode(body) for _, body in mod["codes"]]
    types = mod["types"]
    nt = len(types)
    for i, (ps, rs) in enumerate(
        types
    ):  # a type outside a rec group sees only itself and earlier ones
        for t in (*ps, *rs):
            _vt(t, i + 1)
    imps = mod["imports"]
    for im in imps:
        if im[0] == "global":
            _vt(im[3][0], nt)
    for g in mod["globals"]:
        _vt(g[0], nt)
    for t in (*mod["ttypes"], *(e[2] for e in mod["elem_info"])):
        _vt(t, nt)
    for locs, _ in mod["codes"]:
        for t in locs:
            _vt(t, nt)
    for i in (*(im[3] for im in imps if im[0] == "func"), *mod["funcs"]):
        if i >= len(types):
            raise Invalid("unknown type")
    c = _Ctx(mod)
    nimpg = sum(1 for i in imps if i[0] == "global")
    for im in imps:
        if im[0] == "table":
            _limits(im[3], MAX_TABLE, "table")
        elif im[0] == "mem":
            _limits(im[3], MAX_PAGES, "memory")
    for lim in mod["tables"]:
        _limits(lim, MAX_TABLE, "table")
    for lim in mod["mems"]:
        _limits(lim, MAX_PAGES, "memory")
    # function references allowed in function bodies: those named outside of them (spec: C.refs)
    for _, _, _, items in mod["elem_info"]:
        c.refs.update(e[1] for e in items if e[0] == "func")
    for e in (*(g[2] for g in mod["globals"]), *mod["tinit"]):
        if e is not None and e[0] == "func":
            c.refs.add(e[1])
    for _, kind, idx in mod["exports"]:
        if kind == 0:
            c.refs.add(idx)
    for i, (t, _, init) in enumerate(mod["globals"]):
        if not _match(c.types, _const(c, init, nimpg + i), t):
            raise Invalid("type mismatch")
    ng = len(c.gtypes)
    for t, init in zip(
        mod["ttypes"][len(c.ttypes) - len(mod["tables"]) :], mod["tinit"], strict=True
    ):
        if init is None:
            if t.__class__ is tuple and not t[0]:  # a non-nullable table needs an initializer
                raise Invalid("type mismatch")
        elif not _match(c.types, _const(c, init, nimpg), t):
            raise Invalid("type mismatch")
    for ti in mod["tags"]:  # an exception tag: a function type without results
        if ti >= nt:
            raise Invalid("unknown type")
        if types[ti][1]:
            raise Invalid("non-empty tag result type")
    seen = set()
    counts = (len(c.funcs), len(c.ttypes), c.nmems, ng, len(mod["tags"]))
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
            if not _match(c.types, rt, c.ttypes[ti]):
                raise Invalid("type mismatch")
            if _const(c, off, ng) != I32:
                raise Invalid("type mismatch")
        for e in items:
            if not _match(c.types, _const(c, e, ng), rt):
                raise Invalid("type mismatch")
    for (off, _), mi in zip(mod["datas"], mod["data_mem"], strict=True):
        if mi is not None:
            if mi >= c.nmems:
                raise Invalid("unknown memory")
            if _const(c, off, ng) != I32:
                raise Invalid("type mismatch")
    nimpf = len(c.funcs) - len(mod["funcs"])
    for k, ((locs, _), insns) in enumerate(zip(mod["codes"], mod["insns"], strict=True)):
        ps, rs = c.funcs[nimpf + k]
        _body(c, ps + locs, len(ps), rs, insns)


def _body(c: _Ctx, lt: list, nparams: int, results: list, insns: list[tuple]) -> None:
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
    ntypes = len(types)

    def pop(expect=UNKNOWN):
        f = ctl[-1]
        if len(vals) == f[3]:
            if f[4]:
                return UNKNOWN
            raise Invalid("type mismatch")
        x = vals.pop()
        if expect and x and not _match(types, x, expect):
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

    def need_mem(mi: int = 0) -> None:
        if mi >= c.nmems:
            raise Invalid("unknown memory")

    for at, ins in enumerate(insns):
        op = ins[0]
        sig = OPSIG.get(op)
        if sig is not None:
            if 0x28 <= op <= 0x3E:
                need_mem(ins[3])
                if ins[2] > ALIGN[op]:
                    raise Invalid("alignment must not be larger than natural")
                if ins[1] > 0xFFFFFFFF:
                    raise Invalid("offset out of range")
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
        elif op in (0x02, 0x03, 0x04):
            if op == 0x04:
                pop(I32)
            bt = ins[1]
            if bt[0] == "idx":
                if bt[1] >= len(types):
                    raise Invalid("unknown type")
                bt = types[bt[1]]
            for t in bt[1]:
                _vt(t, ntypes)
            enter(op, bt[0], bt[1])
        elif op == 0x05:
            f = exit_()
            if f[0] != 0x04:
                raise Invalid("else without if")
            ctl.append([0x05, f[1], f[2], len(vals), False, len(inits)])
            vals.extend(f[1])
        elif op == 0x0B:
            f = exit_()
            if f[0] == 0x04 and not _rmatch(types, f[1], f[2]):
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
            if not _match(types, table(ins[2]), FUNCREF):
                raise Invalid("type mismatch")
            if ins[1] >= len(types):
                raise Invalid("unknown type")
            pop(I32)
            ps, rs = types[ins[1]]
            popn(ps)
            if op == 0x11:
                vals.extend(rs)
            else:
                if not _rmatch(types, rs, results):
                    raise Invalid("type mismatch")
                unreachable()
        elif op == 0x12:
            if ins[1] >= len(funcs):
                raise Invalid("unknown function")
            ps, rs = funcs[ins[1]]
            if not _rmatch(types, rs, results):
                raise Invalid("type mismatch")
            popn(ps)
            unreachable()
        elif op in (0x14, 0x15):
            if ins[1] >= len(types):
                raise Invalid("unknown type")
            pop((1, ins[1]))
            ps, rs = types[ins[1]]
            if op == 0x15 and not _rmatch(types, rs, results):
                raise Invalid("type mismatch")
            popn(ps)
            if op == 0x14:
                vals.extend(rs)
            else:
                unreachable()
        elif op == 0x1A:
            if pop() == simd.V:
                insns[at] = (0x1A, simd.V)  # S0005: a v128 drop is told apart for the JIT slots
        elif op == 0x1B:
            pop(I32)
            if len(ins) == 2:  # typed select
                if len(ins[1]) != 1:
                    raise Invalid("invalid result arity")
                t = _vt(ins[1][0], ntypes)
                pop(t)
                pop(t)
                vals.append(t)
            else:
                a, b = pop(), pop()
                if _isref(a) or _isref(b) or (a and b and a != b):
                    raise Invalid("type mismatch")
                vals.append(a or b)
                if (a or b) == simd.V:
                    insns[at] = (0x1B, [simd.V])  # the typed form, S0005
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
            pop(I32)
            vals.append(t)
        elif op == 0x26:
            t = table(ins[1])
            pop(t)
            pop(I32)
        elif op == 0x3F:
            need_mem(ins[1])
            vals.append(I32)
        elif op == 0x40:
            need_mem(ins[1])
            pop(I32)
            vals.append(I32)
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
        elif op == 0xD4:
            x = pop()
            if x and not _isref(x):
                raise Invalid("type mismatch")
            vals.append((0, _nn(x)[1]) if x else UNKNOWN)
        elif op in (0xD5, 0xD6):
            ts = label(ins[1])
            if op == 0xD5:
                x = pop()
                if x and not _isref(x):
                    raise Invalid("type mismatch")
                popn(ts)
                vals.extend(ts)
                vals.append((0, _nn(x)[1]) if x else UNKNOWN)
            else:
                if not ts or not _isref(ts[-1]):
                    raise Invalid("type mismatch")
                pop((1, _nn(ts[-1])[1]))
                popn(ts[:-1])
                vals.extend(ts[:-1])
        elif op == 0xFC0C:
            if ins[1] >= len(c.etypes):
                raise Invalid("unknown elem segment")
            if not _match(types, c.etypes[ins[1]], table(ins[2])):
                raise Invalid("type mismatch")
            popn(list(BULK3))
        elif op == 0xFC0D:
            if ins[1] >= len(c.etypes):
                raise Invalid("unknown elem segment")
        elif op == 0xFC0E:
            if not _match(types, table(ins[2]), table(ins[1])):
                raise Invalid("type mismatch")
            popn(list(BULK3))
        elif op == 0xFC0F:
            t = table(ins[1])
            pop(I32)
            pop(t)
            vals.append(I32)
        elif op == 0xFC10:
            table(ins[1])
            vals.append(I32)
        elif op == 0xFC11:
            t = table(ins[1])
            pop(I32)
            pop(t)
            pop(I32)
        elif op in (0xFC08, 0xFC09):
            if ins[1] >= c.ndatas:
                raise Invalid("unknown data segment")
            if op == 0xFC08:
                need_mem(ins[2])
                popn(list(BULK3))
        elif op in (0xFC0A, 0xFC0B):
            need_mem(ins[1])
            if op == 0xFC0A:
                need_mem(ins[2])
            popn(list(BULK3))
        elif op >= simd.BASE:
            simd.validate(ins, pop, vals.append, need_mem)
        else:
            raise _Unsupported(f"opcode {op:#x}")
    if ctl:
        raise Invalid("function body not closed")
