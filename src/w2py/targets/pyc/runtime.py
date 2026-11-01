# pyright: basic, reportArgumentType=false, reportOptionalMemberAccess=false, reportAttributeAccessIssue=false, reportSelfClsParameterName=false
"""Runtime helpers imported by generated `pyc` modules (D18); ported from the poc/w2py.py prelude."""

import struct as _st, math as _m

from w2py.targets.base import Trap, TrapCode


INF = float("inf")
NAN = float("nan")
_pi = _st.pack_into
_pk = _st.pack
_upk = _st.unpack
U32 = _st.Struct("<I").unpack_from
S32 = _st.Struct("<i").unpack_from
U64 = _st.Struct("<Q").unpack_from
U16 = _st.Struct("<H").unpack_from
S16 = _st.Struct("<h").unpack_from
S8 = _st.Struct("<b").unpack_from
F64L = _st.Struct("<d").unpack_from
P32 = _st.Struct("<I").pack_into
P64 = _st.Struct("<Q").pack_into
P16 = _st.Struct("<H").pack_into
F64S = _st.Struct("<d").pack_into


def F32S(m, a, v):
    if v != v:
        P32(m, a, _i32_f32(v))
        return
    try:
        _pi("<f", m, a, v)
    except OverflowError:
        _pi("<f", m, a, _m.copysign(INF, v))


def _f32(x):
    try:
        return _upk("<f", _pk("<f", x))[0]
    except OverflowError:
        return _m.copysign(INF, x)


def _grow(M, maxpages, n):
    old = len(M) >> 16
    if old + n > maxpages:
        return 0xFFFFFFFF
    M.extend(bytes(n << 16))
    return old


def _mcopy(D, S, d, s, n):
    if d + n > len(D) or s + n > len(S):
        raise Trap(TrapCode.MEMORY_OUT_OF_BOUNDS, "out of bounds memory access")
    D[d : d + n] = S[s : s + n]


def _mfill(M, d, v, n):
    if d + n > len(M):
        raise Trap(TrapCode.MEMORY_OUT_OF_BOUNDS, "out of bounds memory access")
    M[d : d + n] = bytes([v & 255]) * n


def _eseg(T, o, fs):
    o &= 0xFFFFFFFF
    if o + len(fs) > len(T):
        raise Trap(TrapCode.TABLE_OUT_OF_BOUNDS, "out of bounds table access")
    T[o : o + len(fs)] = fs


def _tget(T, i):
    if i >= len(T):
        raise Trap(TrapCode.TABLE_OUT_OF_BOUNDS, "out of bounds table access")
    return T[i]


def _tset(T, i, v):
    if i >= len(T):
        raise Trap(TrapCode.TABLE_OUT_OF_BOUNDS, "out of bounds table access")
    T[i] = v


def _tgrow(TX, n, v):
    old = len(TX.t)
    # the cap is Wasmtime's default table limit (10M elements)
    if old + n > (0xFFFFFFFF if TX.max is None else TX.max) or old + n > 10000000:
        return 0xFFFFFFFF
    TX.t.extend([v] * n)
    return old


def _tfill(T, i, v, n):
    if i + n > len(T):
        raise Trap(TrapCode.TABLE_OUT_OF_BOUNDS, "out of bounds table access")
    T[i : i + n] = [v] * n


def _tcopy(D, S, d, s, n):
    if d + n > len(D) or s + n > len(S):
        raise Trap(TrapCode.TABLE_OUT_OF_BOUNDS, "out of bounds table access")
    D[d : d + n] = S[s : s + n]


def _tinit(T, S, d, s, n):
    if d + n > len(T) or s + n > len(S):
        raise Trap(TrapCode.TABLE_OUT_OF_BOUNDS, "out of bounds table access")
    T[d : d + n] = S[s : s + n]


def _dseg(M, o, d):
    o &= 0xFFFFFFFF
    if o + len(d) > len(M):
        raise Trap(TrapCode.MEMORY_OUT_OF_BOUNDS, "out of bounds memory access")
    M[o : o + len(d)] = d


def _minit(M, D, d, s, n):
    if d + n > len(M) or s + n > len(D):
        raise Trap(TrapCode.MEMORY_OUT_OF_BOUNDS, "out of bounds memory access")
    M[d : d + n] = D[s : s + n]


def _toob():
    raise Trap(TrapCode.TABLE_OUT_OF_BOUNDS, "undefined element")


def _tnull():
    raise Trap(TrapCode.INDIRECT_CALL_TO_NULL, "uninitialized element")


def _tsig():
    raise Trap(TrapCode.BAD_SIGNATURE, "indirect call type mismatch")


def _tnullfn():
    raise Trap(TrapCode.NULL_REFERENCE, "null function reference")


def _nonnull(x):
    if x is None:
        raise Trap(TrapCode.NULL_REFERENCE, "null reference")
    return x


class _Mem(bytearray):
    """A linear memory; `max` is its declared maximum in pages (None: none). Shared by import."""

    max = None


class _Table:
    """An exported table: the list `t` (shared by import) and its declared maximum."""

    def __init__(self, t, max):
        self.t = t
        self.max = max


class _Cell:
    """A global shared between instances (imported, or exported and mutable): value in `.v`."""

    def __init__(self, v):
        self.v = v

    def get(self):
        return self.v


class _Global:
    """An exported global: `.get()` reads the current value."""

    def __init__(self, get):
        self.get = get


class _TC:
    """A pending tail call: function `f` (a body, not a wrapper) and its argument tuple `a`."""

    __slots__ = ("f", "a")

    def __init__(self, f, a):
        self.f = f
        self.a = a


def _pc(x):
    return x.bit_count()


def _bl(x):
    return x.bit_length()


def _clz32(x):
    return 32 - _bl(x)


def _clz64(x):
    return 64 - _bl(x)


def _ctz32(x):
    return _bl(x & -x) - 1 if x else 32


def _ctz64(x):
    return _bl(x & -x) - 1 if x else 64


def _rotl32(x, k):
    return ((x << k) | (x >> (32 - k))) & 0xFFFFFFFF


def _rotr32(x, k):
    return ((x >> k) | (x << (32 - k))) & 0xFFFFFFFF


def _rotl64(x, k):
    return ((x << k) | (x >> (64 - k))) & 0xFFFFFFFFFFFFFFFF


def _rotr64(x, k):
    return ((x >> k) | (x << (64 - k))) & 0xFFFFFFFFFFFFFFFF


def _divs32(a, b):
    a = (a ^ 0x80000000) - 0x80000000
    b = (b ^ 0x80000000) - 0x80000000
    if b == 0:
        raise Trap(TrapCode.INTEGER_DIVISION_BY_ZERO, "integer divide by zero")
    if a == -0x80000000 and b == -1:
        raise Trap(TrapCode.INTEGER_OVERFLOW, "integer overflow")
    q = abs(a) // abs(b)
    return (-q if (a < 0) != (b < 0) else q) & 0xFFFFFFFF


def _rems32(a, b):
    a = (a ^ 0x80000000) - 0x80000000
    b = (b ^ 0x80000000) - 0x80000000
    if b == 0:
        raise Trap(TrapCode.INTEGER_DIVISION_BY_ZERO, "integer divide by zero")
    r = abs(a) % abs(b)
    return (-r if a < 0 else r) & 0xFFFFFFFF


def _divs64(a, b):
    h = 0x8000000000000000
    a = (a ^ h) - h
    b = (b ^ h) - h
    if b == 0:
        raise Trap(TrapCode.INTEGER_DIVISION_BY_ZERO, "integer divide by zero")
    if a == -h and b == -1:
        raise Trap(TrapCode.INTEGER_OVERFLOW, "integer overflow")
    q = abs(a) // abs(b)
    return (-q if (a < 0) != (b < 0) else q) & 0xFFFFFFFFFFFFFFFF


def _rems64(a, b):
    h = 0x8000000000000000
    a = (a ^ h) - h
    b = (b ^ h) - h
    if b == 0:
        raise Trap(TrapCode.INTEGER_DIVISION_BY_ZERO, "integer divide by zero")
    r = abs(a) % abs(b)
    return (-r if a < 0 else r) & 0xFFFFFFFFFFFFFFFF


def _fdiv(a, b):
    try:
        return a / b
    except ZeroDivisionError:
        if a != a or a == 0:
            return NAN
        return _m.copysign(INF, a) * _m.copysign(1.0, b)


def _fsqrt(x):
    if x < 0:
        return NAN
    return _m.sqrt(x)


def _fceil(x):  # NaN: x*1.0 quiets it, as Wasmtime does
    if x != x:
        return x * 1.0
    return _m.copysign(float(_m.ceil(x)), x) if x not in (INF, -INF) and x != 0 else x


def _ffloor(x):
    if x != x:
        return x * 1.0
    return float(_m.floor(x)) if x not in (INF, -INF) and x != 0 else x


def _ftrunc(x):
    if x != x:
        return x * 1.0
    return _m.copysign(float(_m.trunc(x)), x) if x not in (INF, -INF) and x != 0 else x


def _fnearest(x):
    if x != x:
        return x * 1.0
    if x in (INF, -INF) or x == 0:
        return x
    return _m.copysign(float(round(x)), x)


def _fmin(a, b):
    if a != a or b != b:
        return NAN
    if a == b == 0:
        return a if _m.copysign(1, a) < 0 else b
    return a if a < b else b


def _fmax(a, b):
    if a != a or b != b:
        return NAN
    if a == b == 0:
        return b if _m.copysign(1, a) < 0 else a
    return a if a > b else b


_copysign = _m.copysign


def _tr(x, lo, hi):
    if x != x:
        raise Trap(TrapCode.BAD_CONVERSION_TO_INTEGER, "invalid conversion to integer")
    t = _m.trunc(x) if x not in (INF, -INF) else None
    if t is None or not (lo <= t <= hi):
        raise Trap(TrapCode.INTEGER_OVERFLOW, "integer overflow")
    return t


def _trunc_s32(x):
    return _tr(x, -(2**31), 2**31 - 1) & 0xFFFFFFFF


def _trunc_u32(x):
    return _tr(x, 0, 2**32 - 1)


def _trunc_s64(x):
    return _tr(x, -(2**63), 2**63 - 1) & 0xFFFFFFFFFFFFFFFF


def _trunc_u64(x):
    return _tr(x, 0, 2**64 - 1)


def _sat(x, lo, hi):
    if x != x:
        return 0
    if x >= hi:
        return hi
    if x <= lo:
        return lo
    return _m.trunc(x)


def _tsat_s32(x):
    return _sat(x, -(2**31), 2**31 - 1) & 0xFFFFFFFF


def _tsat_u32(x):
    return _sat(x, 0, 2**32 - 1)


def _tsat_s64(x):
    return _sat(x, -(2**63), 2**63 - 1) & 0xFFFFFFFFFFFFFFFF


def _tsat_u64(x):
    return _sat(x, 0, 2**64 - 1)


def _i32_f32(x):
    if x != x:  # an f32 NaN is the double NaN with the payload << 29
        b = _i64_f64(x)
        return (b >> 32) & 0x80000000 | 0x7F800000 | (b >> 29) & 0x7FFFFF
    return _st.unpack("<I", _st.pack("<f", x))[0]


def _f32_i32(x):
    if x & 0x7F800000 == 0x7F800000 and x & 0x7FFFFF:
        return _f64_i64((x & 0x80000000) << 32 | 0x7FF0000000000000 | (x & 0x7FFFFF) << 29)
    return _st.unpack("<f", _st.pack("<I", x))[0]


def _i64_f32(x):  # one rounding: fold the bits below 53 into a sticky bit, then round to f32
    n = abs(x)
    s = n.bit_length() - 53
    if s > 0:
        n = n >> s | (n & ((1 << s) - 1) != 0)
        return _f32(_m.copysign(_m.ldexp(float(n), s), x))
    return _f32(float(x))


def _i64_f64(x):
    return _st.unpack("<Q", _st.pack("<d", x))[0]


def _f64_i64(x):
    return _st.unpack("<d", _st.pack("<Q", x))[0]


_NAMES = [
    n for n in dir() if n.startswith("_") and not n.startswith("__") and n not in ("_st", "_m")
]
__all__ = _NAMES + [
    "Trap",
    "TrapCode",
    "INF",
    "NAN",
    "U32",
    "S32",
    "U64",
    "U16",
    "S16",
    "S8",
    "F64L",
    "P32",
    "P64",
    "P16",
    "F64S",
    "F32S",
]  # pyright: ignore[reportUnsupportedDunderAll]
