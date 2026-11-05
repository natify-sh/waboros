# pyright: basic, reportArgumentType=false, reportOptionalMemberAccess=false, reportAttributeAccessIssue=false, reportSelfClsParameterName=false
"""Fixed-width SIMD (v128) for `pyc` (docs/specs/S0005, "Semantics / pyc").

A v128 value is a Python `int` in [0, 2^128), the 16 bytes little-endian. Bitwise operators work on
the int; every lane operator unpacks it with a `struct` shape, works on Python ints, and joins the
masked lanes back. Float lanes: data movement and sign operators work on the bits; arithmetic uses
the scalar helpers of `runtime` lane by lane (D28: an f32 lane is a double, exact for + - * / sqrt;
NaN payloads as `_f32_i32` / `_i32_f32` carry them).

`gen(s, x)` is the one hook `codegen.FuncGen.simple` calls; the generated module imports the helpers
named `_<shape>_<op>` (every operator of `frontend.simd.OPS` has one, a few are written inline).
"""

import struct
from operator import eq, ge, gt, le, lt, ne

from waboros.frontend.simd import BASE, CONST, I32, I64, LANE, MEM, MEMLANE, OPS, SHUFFLE, V
from waboros.targets.base import Trap, TrapCode
from waboros.targets.pyc import runtime as _rt
from waboros.targets.pyc.codegen import E, dep

_M32, _M64, _M128 = 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF, (1 << 128) - 1
_U = {
    b: struct.Struct("<%d%s" % (128 // b, c))
    for b, c in ((8, "B"), (16, "H"), (32, "I"), (64, "Q"))
}
_S = {
    b: struct.Struct("<%d%s" % (128 // b, c))
    for b, c in ((8, "b"), (16, "h"), (32, "i"), (64, "q"))
}
_REP = {b: sum(1 << (b * i) for i in range(128 // b)) for b in _U}  # 1 in every lane
_IW = {"i8x16": 8, "i16x8": 16, "i32x4": 32, "i64x2": 64}
_FW = {"f32x4": 32, "f64x2": 64}
# float lane: bits -> double, double -> bits, round to the lane's precision
_FM = {
    32: (_rt._f32_i32, _rt._i32_f32, _rt._f32),
    64: (_rt._f64_i64, _rt._i64_f64, lambda x: x),
}
H = {}  # operator name -> helper


def _lanes(v, st):
    return st.unpack(v.to_bytes(16, "little"))


def _join(lanes, b):
    m = (1 << b) - 1
    return int.from_bytes(_U[b].pack(*[x & m for x in lanes]), "little")


def _clamp(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


def _rd(M, a, n):
    if a + n > len(M):
        raise Trap(TrapCode.MEMORY_OUT_OF_BOUNDS, "out of bounds memory access")
    return M[a : a + n]


def _wr(M, a, b):
    if a + len(b) > len(M):
        raise Trap(TrapCode.MEMORY_OUT_OF_BOUNDS, "out of bounds memory access")
    M[a : a + len(b)] = b


# ------------------------------------------------------------------------------ memory
def _memory() -> None:
    H["v128.load"] = lambda M, a: int.from_bytes(_rd(M, a, 16), "little")
    H["v128.store"] = lambda M, a, v: _wr(M, a, v.to_bytes(16, "little"))
    for src in (8, 16, 32):
        for sg in "su":
            st = (_S if sg == "s" else _U)[src]
            n = 64 // src
            H["v128.load%dx%d_%s" % (src, n, sg)] = lambda M, a, st=st, n=n, src=src: _join(
                st.unpack(_rd(M, a, 8) + bytes(8))[:n], 2 * src
            )
    for k in (8, 16, 32, 64):
        m, nb = (1 << k) - 1, k // 8
        H["v128.load%d_splat" % k] = lambda M, a, nb=nb, k=k: (
            int.from_bytes(_rd(M, a, nb), "little") * _REP[k]
        )
        if k >= 32:
            H["v128.load%d_zero" % k] = lambda M, a, nb=nb: int.from_bytes(_rd(M, a, nb), "little")
        H["v128.load%d_lane" % k] = lambda M, a, v, ln, m=m, k=k, nb=nb: (
            v & ~(m << k * ln) | int.from_bytes(_rd(M, a, nb), "little") << k * ln
        )  # fmt: skip
        H["v128.store%d_lane" % k] = lambda M, a, v, ln, m=m, k=k, nb=nb: _wr(
            M, a, ((v >> k * ln) & m).to_bytes(nb, "little")
        )  # fmt: skip


# ------------------------------------------------------------------------------ lane access
def _access() -> None:
    for shape, b in _IW.items():
        m, h = (1 << b) - 1, 1 << (b - 1)
        H[shape + ".splat"] = lambda x, b=b, m=m: (x & m) * _REP[b]
        H[shape + ".extract_lane"] = lambda v, ln, b=b, m=m: (v >> b * ln) & m
        H[shape + ".extract_lane_u"] = H[shape + ".extract_lane"]
        H[shape + ".extract_lane_s"] = lambda v, ln, b=b, m=m, h=h: (
            ((((v >> b * ln) & m) ^ h) - h) & _M32
        )
        H[shape + ".replace_lane"] = lambda v, x, ln, b=b, m=m: (
            v & ~(m << b * ln) | (x & m) << b * ln
        )
    for shape, b in _FW.items():
        fr, to, _ = _FM[b]
        m = (1 << b) - 1
        H[shape + ".splat"] = lambda x, b=b, to=to: to(x) * _REP[b]
        H[shape + ".extract_lane"] = lambda v, ln, b=b, m=m, fr=fr: fr((v >> b * ln) & m)
        H[shape + ".replace_lane"] = lambda v, x, ln, b=b, m=m, to=to: (
            v & ~(m << b * ln) | to(x) << b * ln
        )
    H["i8x16.shuffle"] = lambda a, c, lanes: int.from_bytes(
        bytes((a.to_bytes(16, "little") + c.to_bytes(16, "little"))[i] for i in lanes), "little"
    )
    H["i8x16.swizzle"] = lambda a, c: int.from_bytes(
        bytes(a.to_bytes(16, "little")[i] if i < 16 else 0 for i in c.to_bytes(16, "little")),
        "little",
    )
    H["v128.bitselect"] = lambda a, c, m: c ^ ((a ^ c) & m)


# ------------------------------------------------------------------------------ integer lanes
_IBIN = {  # suffix: (lanes signed?, f(x, y, bits))
    "add": (0, lambda x, y, b: x + y),
    "sub": (0, lambda x, y, b: x - y),
    "mul": (0, lambda x, y, b: x * y),
    "add_sat_s": (1, lambda x, y, b: _clamp(x + y, -(1 << b - 1), (1 << b - 1) - 1)),
    "add_sat_u": (0, lambda x, y, b: _clamp(x + y, 0, (1 << b) - 1)),
    "sub_sat_s": (1, lambda x, y, b: _clamp(x - y, -(1 << b - 1), (1 << b - 1) - 1)),
    "sub_sat_u": (0, lambda x, y, b: _clamp(x - y, 0, (1 << b) - 1)),
    "min_s": (1, lambda x, y, b: min(x, y)),
    "min_u": (0, lambda x, y, b: min(x, y)),
    "max_s": (1, lambda x, y, b: max(x, y)),
    "max_u": (0, lambda x, y, b: max(x, y)),
    "avgr_u": (0, lambda x, y, b: (x + y + 1) >> 1),
    "q15mulr_sat_s": (1, lambda x, y, b: _clamp((x * y + 0x4000) >> 15, -0x8000, 0x7FFF)),
}
for _op, _f in (("eq", eq), ("ne", ne), ("lt", lt), ("gt", gt), ("le", le), ("ge", ge)):
    _IBIN[_op] = (0, lambda x, y, b, f=_f: -f(x, y))
    for _sg in "su":
        _IBIN["%s_%s" % (_op, _sg)] = (_sg == "s", lambda x, y, b, f=_f: -f(x, y))
_IUN = {"abs": (1, abs), "neg": (0, lambda x: -x), "popcnt": (0, int.bit_count)}


def _integer() -> None:
    for shape, b in _IW.items():
        for op, (sg, f) in _IBIN.items():
            st = (_S if sg else _U)[b]
            H["%s.%s" % (shape, op)] = lambda a, c, st=st, f=f, b=b: _join(
                [f(x, y, b) for x, y in zip(_lanes(a, st), _lanes(c, st), strict=True)], b
            )
        for op, (sg, f) in _IUN.items():
            st = (_S if sg else _U)[b]
            H["%s.%s" % (shape, op)] = lambda v, st=st, f=f, b=b: _join(
                [f(x) for x in _lanes(v, st)], b
            )
        H[shape + ".all_true"] = lambda v, b=b: int(all(_lanes(v, _U[b])))
        H[shape + ".bitmask"] = lambda v, b=b: sum(
            (x >> b - 1) << i for i, x in enumerate(_lanes(v, _U[b]))
        )
        for op in ("shl", "shr_s", "shr_u"):
            st, k = (_S if op == "shr_s" else _U)[b], b - 1
            H["%s.%s" % (shape, op)] = lambda v, c, st=st, k=k, b=b, left=op == "shl": _join(
                [x << (c & k) if left else x >> (c & k) for x in _lanes(v, st)], b
            )
    for name in OPS_BY_NAME:  # widening, narrowing and pairwise operators
        shape, _, op = name.partition(".")
        p = op.split("_")
        if shape not in _IW or p[0] not in ("extend", "extmul", "extadd", "narrow"):
            continue
        b, sg = _IW[shape], p[-1] == "s"
        if p[0] == "narrow":
            h = 1 << (b - 1)
            lo, hi = (-h, h - 1) if sg else (0, (1 << b) - 1)
            st = _S[2 * b]
            H[name] = lambda a, c, st=st, lo=lo, hi=hi, b=b: _join(
                [_clamp(x, lo, hi) for x in _lanes(a, st) + _lanes(c, st)], b
            )
            continue
        st, n = (_S if sg else _U)[b // 2], 128 // b
        sl = slice(0, n) if p[1] == "low" else slice(n, 2 * n)
        if p[0] == "extend":
            H[name] = lambda v, st=st, sl=sl, b=b: _join(_lanes(v, st)[sl], b)
        elif p[0] == "extmul":
            H[name] = lambda a, c, st=st, sl=sl, b=b: _join(
                [x * y for x, y in zip(_lanes(a, st)[sl], _lanes(c, st)[sl], strict=True)], b
            )
        else:
            H[name] = lambda v, st=st, b=b, n=n: _join(
                [(w := _lanes(v, st))[2 * i] + w[2 * i + 1] for i in range(n)], b
            )
    H["i32x4.dot_i16x8_s"] = lambda a, c: _join(_dot(_lanes(a, _S[16]), _lanes(c, _S[16])), 32)


def _dot(a, c):
    return [a[2 * i] * c[2 * i] + a[2 * i + 1] * c[2 * i + 1] for i in range(4)]


# ------------------------------------------------------------------------------ float lanes
_FUN = {
    "sqrt": _rt._fsqrt,
    "ceil": _rt._fceil,
    "floor": _rt._ffloor,
    "trunc": _rt._ftrunc,
    "nearest": _rt._fnearest,
}
_FBIN = {
    "add": lambda x, y: x + y,
    "sub": lambda x, y: x - y,
    "mul": lambda x, y: x * y,
    "div": _rt._fdiv,
    "min": _rt._fmin,
    "max": _rt._fmax,
}


def _float() -> None:
    for shape, b in _FW.items():
        fr, to, rnd = _FM[b]
        st = _U[b]
        for op, f in _FUN.items():
            H["%s.%s" % (shape, op)] = lambda v, st=st, f=f, fr=fr, to=to, rnd=rnd, b=b: _join(
                [to(rnd(f(fr(x)))) for x in _lanes(v, st)], b
            )
        for op, f in _FBIN.items():
            H["%s.%s" % (shape, op)] = lambda a, c, st=st, f=f, fr=fr, to=to, rnd=rnd, b=b: _join(
                [
                    to(rnd(f(fr(x), fr(y))))
                    for x, y in zip(_lanes(a, st), _lanes(c, st), strict=True)
                ],
                b,
            )
        for op, f in (("eq", eq), ("ne", ne), ("lt", lt), ("gt", gt), ("le", le), ("ge", ge)):
            H["%s.%s" % (shape, op)] = lambda a, c, st=st, f=f, fr=fr, b=b: _join(
                [-f(fr(x), fr(y)) for x, y in zip(_lanes(a, st), _lanes(c, st), strict=True)], b
            )
        # pmin / pmax pick one of the two inputs bit for bit (a NaN passes through unchanged)
        H[shape + ".pmin"] = lambda a, c, st=st, fr=fr, b=b: _join(
            [y if fr(y) < fr(x) else x for x, y in zip(_lanes(a, st), _lanes(c, st), strict=True)],
            b,
        )
        H[shape + ".pmax"] = lambda a, c, st=st, fr=fr, b=b: _join(
            [y if fr(x) < fr(y) else x for x, y in zip(_lanes(a, st), _lanes(c, st), strict=True)],
            b,
        )
    (f32r, f32w, f32q), (f64r, f64w, _), u32, u64, s32 = _FM[32], _FM[64], _U[32], _U[64], _S[32]
    H["f32x4.demote_f64x2_zero"] = lambda v: _join(
        [f32w(_rt._demote(f64r(x))) for x in _lanes(v, u64)] + [0, 0], 32
    )
    # promote quiets a signalling NaN, as the scalar `f64.promote_f32` does (x * 1.0)
    H["f64x2.promote_low_f32x4"] = lambda v: _join(
        [f64w(f32r(x) * 1.0) for x in _lanes(v, u32)[:2]], 64
    )
    H["i32x4.trunc_sat_f32x4_s"] = lambda v: _join(
        [_rt._tsat_s32(f32r(x)) for x in _lanes(v, u32)], 32
    )
    H["i32x4.trunc_sat_f32x4_u"] = lambda v: _join(
        [_rt._tsat_u32(f32r(x)) for x in _lanes(v, u32)], 32
    )
    H["f32x4.convert_i32x4_s"] = lambda v: _join([f32w(f32q(float(x))) for x in _lanes(v, s32)], 32)
    H["f32x4.convert_i32x4_u"] = lambda v: _join([f32w(f32q(float(x))) for x in _lanes(v, u32)], 32)
    H["i32x4.trunc_sat_f64x2_s_zero"] = lambda v: _join(
        [_rt._tsat_s32(f64r(x)) for x in _lanes(v, u64)] + [0, 0], 32
    )
    H["i32x4.trunc_sat_f64x2_u_zero"] = lambda v: _join(
        [_rt._tsat_u32(f64r(x)) for x in _lanes(v, u64)] + [0, 0], 32
    )
    H["f64x2.convert_low_i32x4_s"] = lambda v: _join(
        [f64w(float(x)) for x in _lanes(v, s32)[:2]], 64
    )
    H["f64x2.convert_low_i32x4_u"] = lambda v: _join(
        [f64w(float(x)) for x in _lanes(v, u32)[:2]], 64
    )


# ------------------------------------------------------------------------------ relaxed SIMD
def _relaxed() -> None:
    """The deterministic profile (S0008, D49): every relaxed parameter 0, so alternative 0.
    madd / nmadd are unfused: the product is rounded to the lane's precision, then added."""
    for r, eq_ in (
        ("i8x16.relaxed_swizzle", "i8x16.swizzle"),
        ("i32x4.relaxed_trunc_f32x4_s", "i32x4.trunc_sat_f32x4_s"),
        ("i32x4.relaxed_trunc_f32x4_u", "i32x4.trunc_sat_f32x4_u"),
        ("i32x4.relaxed_trunc_f64x2_s_zero", "i32x4.trunc_sat_f64x2_s_zero"),
        ("i32x4.relaxed_trunc_f64x2_u_zero", "i32x4.trunc_sat_f64x2_u_zero"),
        *((f"{sh}.relaxed_laneselect", "v128.bitselect") for sh in _IW),
        *((f"{sh}.relaxed_{o}", f"{sh}.{o}") for sh in _FW for o in ("min", "max")),
        ("i16x8.relaxed_q15mulr_s", "i16x8.q15mulr_sat_s"),
    ):
        H[r] = H[eq_]
    for sh in _FW:
        mul, add, sub = H[sh + ".mul"], H[sh + ".add"], H[sh + ".sub"]
        H[sh + ".relaxed_madd"] = lambda a, c, d, mul=mul, add=add: add(mul(a, c), d)
        H[sh + ".relaxed_nmadd"] = lambda a, c, d, mul=mul, sub=sub: sub(d, mul(a, c))
    H["i16x8.relaxed_dot_i8x16_i7x16_s"] = lambda a, c: _join(_dot16(a, c), 16)
    H["i32x4.relaxed_dot_i8x16_i7x16_add_s"] = lambda a, c, d: _join(
        [x + y + z for x, y, z in zip(*_pairs(a, c), _lanes(d, _S[32]), strict=True)], 32
    )


def _pairs(a, c):
    """Even and odd lanes of the 16-bit dot products, signed."""
    w = _lanes(_join(_dot16(a, c), 16), _S[16])
    return w[0::2], w[1::2]


def _dot16(a, c):
    """Eight sums of two products of sign-extended byte pairs (wrapped to 16 bits by `_join`)."""
    x, y = _lanes(a, _S[8]), _lanes(c, _S[8])
    return [x[2 * i] * y[2 * i] + x[2 * i + 1] * y[2 * i + 1] for i in range(8)]


# ------------------------------------------------------------------------------ wide arithmetic
def _add128(a, b, c, d):
    t = ((a | b << 64) + (c | d << 64)) & _M128
    return t & _M64, t >> 64


def _sub128(a, b, c, d):
    t = ((a | b << 64) - (c | d << 64)) & _M128
    return t & _M64, t >> 64


def _mul_wide_u(a, b):
    t = a * b
    return t & _M64, t >> 64


def _mul_wide_s(a, b):
    t = (a - (a >> 63 << 64)) * (b - (b >> 63 << 64)) & _M128
    return t & _M64, t >> 64


WIDE = {"add128": _add128, "sub128": _sub128, "mul_wide_s": _mul_wide_s, "mul_wide_u": _mul_wide_u}


OPS_BY_NAME = [e.name for e in OPS.values()]
_memory()
_access()
_integer()
_float()
_relaxed()

# operators written inline by `gen`: (format, sign mask for abs / neg)
_INL = {
    "v128.not": "(%%s^%d)" % _M128,
    "v128.and": "(%s&%s)",
    "v128.andnot": "(%%s&(%%s^%d))" % _M128,
    "v128.or": "(%s|%s)",
    "v128.xor": "(%s^%s)",
    "v128.any_true": "(%s!=0)",
    "f32x4.abs": "(%%s&%d)" % (_M128 ^ _REP[32] * 0x80000000),
    "f32x4.neg": "(%%s^%d)" % (_REP[32] * 0x80000000),
    "f64x2.abs": "(%%s&%d)" % (_M128 ^ _REP[64] * (1 << 63)),
    "f64x2.neg": "(%%s^%d)" % (_REP[64] * (1 << 63)),
}


def _hname(name: str) -> str:
    return "_" + name.replace(".", "_")


_missing = [n for n in OPS_BY_NAME if n not in H and n not in _INL and n != "v128.const"]
assert not _missing, _missing
for _n, _f in H.items():
    globals()[_hname(_n)] = _f
__all__ = [_hname(n) for n in H] + [  # pyright: ignore[reportUnsupportedDunderAll]
    "_" + n for n in WIDE
]


# ------------------------------------------------------------------------------ code generation
def gen(s, x):
    """Emit one SIMD instruction `x` into the function generator `s` (`codegen.FuncGen`)."""
    s.mg.simd = True
    e = OPS[x[0] - BASE]
    nm = _hname(e.name)
    if s.mg.fb and e.name.split(".")[1] in ("splat", "extract_lane", "replace_lane"):
        # the scalar is already the bit pattern: the integer lane helper of the same width
        nm = _hname(e.name.replace("f32x4", "i32x4").replace("f64x2", "i64x2"))
    if e.imm in (MEM, MEMLANE):
        v = s.pop() if len(e.pops) > 1 else None
        a = s.pop()
        ad = s.n(a) + ("+%d" % x[1] if x[1] else "")
        parts = (
            [s.mg.mname(x[3]), ad]
            + ([v.c] if v else [])
            + (["%d" % x[4]] if e.imm == MEMLANE else [])
        )
        call = "%s(%s)" % (nm, ", ".join(parts))
        if e.res:
            s.push(E(call, V, None, **dep(*([a, v] if v else [a]), m=True, x=True)))
        else:
            s.flush_mem()
            s.emit(call)
        return
    args = s.popn(len(e.pops))
    if e.imm == CONST:
        s.push(E(str(x[1]), V))
        return
    codes = [s.n(a) if t in (I32, I64) else a.c for a, t in zip(args, e.pops, strict=True)]
    if e.imm == LANE:
        codes.append("%d" % x[1])
    elif e.imm == SHUFFLE:
        codes.append(repr(x[1]))
    if e.name in _INL:
        code = _INL[e.name] % tuple(codes)
    else:
        code = "%s(%s)" % (nm, ", ".join(codes))
    if e.name == "v128.any_true":
        s.push(E(code, "bool", 1, **dep(*args)))
        return
    s.push(E(code, e.res, _M32 if e.res == I32 else _M64 if e.res == I64 else None, **dep(*args)))


def gen_wide(s, x):
    """`i64.add128` ... `i64.mul_wide_u` (0xFC13..0xFC16): two i64 results from one helper call."""
    s.mg.simd = True  # the module imports this file's helpers
    name = list(WIDE)[x[0] - 0xFC13]
    args = s.popn(4 if x[0] < 0xFC15 else 2)
    s.flush(lambda e: e.x)  # an entry below that can trap runs before the helper (LESSONS 28, 30)
    ts = [s.newtmp(I64), s.newtmp(I64)]
    s.emit("%s = _%s(%s)" % (", ".join(ts), name, ", ".join(s.n(a) for a in args)))
    for t in ts:
        s.push(E(t, I64, _M64, k=True))
