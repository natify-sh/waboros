# pyright: basic, reportArgumentType=false, reportOptionalMemberAccess=false, reportAttributeAccessIssue=false, reportSelfClsParameterName=false
"""Wasm binary front end: byte reader, section parser, function-body decoder (ported from poc/w2py.py).

Every binary-format error is `Malformed`. A valid encoding of something unsupported (SIMD, GC,
exceptions, 64-bit memories, ...) is NotImplementedError, never Malformed: a wrong "malformed" on
a valid module would be a wrong kind.
"""

import struct

from w2py.frontend import simd


def _f32c(raw):
    """An f32 constant as a float; a NaN is the double NaN with the same sign and payload << 29."""
    v = struct.unpack("<f", raw)[0]
    if v == v:
        return v
    i = int.from_bytes(raw, "little")
    q = (i & 0x80000000) << 32 | 0x7FF0000000000000 | (i & 0x7FFFFF) << 29
    return struct.unpack("<d", struct.pack("<Q", q))[0]


M32, M64 = 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF
I32, I64, F32, F64 = 0x7F, 0x7E, 0x7D, 0x7C
TN = {I32: "i32", I64: "i64", F32: "f32", F64: "f64", 0x7B: "v128", 0x70: "ref", 0x6F: "ref"}
UNSUPPORTED_TYPES = frozenset(
    (0x63, 0x64, 0x69, 0x6A, 0x6B, 0x6C, 0x6D, 0x6E, 0x71, 0x72, 0x73, 0x74)
)
UNSUPPORTED_OPS = frozenset((*range(6, 11), 0x18, 0x19, 0x1F, 0xFB, 0xFE))
SIMPLE_OPS = frozenset((0, 1, 5, 0x0B, 0x0F, 0x1A, 0x1B, 0xD1, 0xD3, 0xD4, *range(0x45, 0xC5)))
# Section id -> position; custom sections (0) go anywhere, the data count (12) sits before code (10).
SECTION_RANK = {
    1: 1,
    2: 2,
    3: 3,
    4: 4,
    5: 5,
    13: 5.5,
    6: 6,
    7: 7,
    8: 8,
    9: 9,
    12: 10,
    10: 11,
    11: 12,
}
LOCALS_LIMIT = 50000  # Wasmtime's implementation limit (docs/DECISIONS.md D6)


class Malformed(Exception):
    """The binary does not follow the binary format."""


class ImplLimit(Exception):
    """A well-formed module exceeds a limit of this implementation (the same as Wasmtime's)."""


def _guard(f):
    def g(x):
        try:
            return f(x)
        except (IndexError, struct.error, UnicodeDecodeError) as e:
            raise Malformed(f"{type(e).__name__}: {e}") from None

    return g


def _illegal(op):
    """`op` cannot be decoded here: a known opcode is a gap, anything else is malformed."""
    if (
        op in UNSUPPORTED_OPS
        or op <= 0x15
        or 0x1A <= op <= 0x1C
        or 0x20 <= op <= 0xC4
        or 0xD0 <= op <= 0xD6
        or op == 0xFC
    ):
        raise NotImplementedError("opcode %#x" % op)
    raise Malformed("illegal opcode %#x" % op)


def _reftype(nullable, ht):
    """(ref null func) / (ref null extern) are the bytes 0x70 / 0x6F; any other reference type is
    the tuple (nullable, heap type), the heap type a type index or a negative abstract-type code."""
    if nullable and ht in (-16, -17):
        return 0x70 if ht == -16 else 0x6F
    return (int(nullable), ht)


class R:
    def __init__(s, b, p=0):
        s.b, s.p, s.n = b, p, len(b)  # n: end of what may be read (the enclosing section)

    def u8(s):
        if s.p >= s.n:
            raise Malformed("unexpected end")
        s.p += 1
        return s.b[s.p - 1]

    def leb(s, bits, signed):
        r = 0
        last = (bits + 6) // 7 - 1
        for i in range(last + 1):
            x = s.u8()
            r |= (x & 0x7F) << (7 * i)
            if x < 0x80:
                break
        else:
            raise Malformed("integer representation too long")
        if i == last:  # the unused high bits of the last byte: zero, or the sign if signed
            used = bits - 7 * i
            top = (x & 0x7F) >> (used - 1 if signed else used)
            if top and not (signed and top == 0x7F >> (used - 1)):
                raise Malformed("integer too large")
        if signed and x & 0x40:
            r -= 1 << (7 * (i + 1))
        return r

    def u64(s):  # offsets and limits: the range is checked by the validator, not here
        return s.leb(64, False)

    def u(s):
        if s.p < s.n and s.b[s.p] < 0x80:
            s.p += 1
            return s.b[s.p - 1]
        return s.leb(32, False)

    def s(s, bits):
        return s.leb(bits, True)

    def bytes(s, n):
        if n > s.n - s.p:
            raise Malformed("unexpected end")
        s.p += n
        return s.b[s.p - n : s.p]

    def ht(s):  # heap type: a non-negative index, or a one-byte abstract type
        p = s.p
        v = s.s(33)
        if v < 0 and s.p - p > 1:
            raise Malformed("integer representation too long")
        return v

    def name(s):
        return s.bytes(s.u()).decode()

    def vt(s, refs=False):
        b = s.u8()
        if b in (0x70, 0x6F) or (not refs and b in (I32, I64, F32, F64, 0x7B)):
            return b
        if b in (0x63, 0x64):  # (ref null? ht)
            return _reftype(b == 0x63, s.ht())
        if b in UNSUPPORTED_TYPES:
            raise NotImplementedError("value type %#x" % b)
        raise Malformed("malformed reference type" if refs else "malformed value type")

    def mut(s):
        m = s.u8()
        if m > 1:
            raise Malformed("malformed mutability")
        return m

    def limits(s):
        fl = s.u8()
        if fl > 1:
            if fl < 8:
                raise NotImplementedError("limits flags %d (shared / 64-bit)" % fl)
            raise Malformed("malformed limits flags")
        mn = s.u64()
        return mn, (s.u64() if fl else None)


@_guard
def parse(data):
    if data[:4] != b"\0asm":
        raise Malformed("magic header not detected")
    if data[4:8] != b"\1\0\0\0":
        raise Malformed("unknown binary version")
    r = R(data, 8)
    mod = dict(
        types=[],
        imports=[],
        funcs=[],
        tables=[],
        mems=[],
        globals=[],
        exports=[],
        start=None,
        codes=[],
        datas=[],
        ttypes=[],  # reference type of every table, imported first (for the validator)
        tinit=[],  # initializer expression of every defined table (None: null)
        elem_mode=[],  # "active" / "passive" / "declarative" for every segment
        elem_info=[],  # (table index or None, offset expr or None, reftype, item exprs) of every segment
        data_mem=[],  # memory index of every data segment (None: passive)
        tags=[],  # type index of every defined tag (tag imports are not read yet)
    )
    datacount = None
    rank = 0

    def initexpr():
        st = []  # operand stack of expression trees; add / sub / mul nodes are (name, type, a, b)
        bad = False
        while True:
            op = r.u8()
            if op == 0x0B:
                break
            if op == 0x41:
                st.append(("i32", r.s(32) & M32))
            elif op == 0x42:
                st.append(("i64", r.s(64) & M64))
            elif op == 0x43:
                st.append(("f32", _f32c(r.bytes(4))))
            elif op == 0x44:
                st.append(("f64", struct.unpack("<d", r.bytes(8))[0]))
            elif op == 0x23:
                st.append(("global", r.u()))
            elif op == 0xD2:
                st.append(("func", r.u()))
            elif op == 0xD0:
                st.append(("null", r.ht()))
            elif op == 0xFD:
                st.append(simd.const(r))
            elif op in (0x6A, 0x6B, 0x6C, 0x7C, 0x7D, 0x7E):  # extended constant expressions
                if len(st) < 2:
                    bad = True
                    continue
                b2, a2 = st.pop(), st.pop()
                name = ("add", "sub", "mul")[(op - 0x6A) if op < 0x70 else (op - 0x7C)]
                st.append((name, "i32" if op < 0x70 else "i64", a2, b2))
            else:
                _illegal(op)
        return st[0] if len(st) == 1 and not bad else ("bad",)

    while r.p < len(data):
        sid = r.u8()
        size = r.u()
        end = r.p + size
        if end > len(data):
            raise Malformed("length out of bounds")
        r.n = end
        if sid == 0:
            r.name()
            r.p = end
        else:
            if sid not in SECTION_RANK:
                raise Malformed("malformed section id")
            if SECTION_RANK[sid] <= rank:
                raise Malformed("unexpected content after last section")
            rank = SECTION_RANK[sid]
        if sid == 13:  # tags: attribute 0 (exception), then a type index
            for _ in range(r.u()):
                if r.u8() != 0:
                    raise Malformed(
                        "zero byte expected"
                    )  # tag attribute; the reference interpreter's text
                mod["tags"].append(r.u())
        elif sid == 1:
            for _ in range(r.u()):
                form = r.u8()
                if form in (0x4E, 0x4F, 0x50, 0x5E, 0x5F):
                    raise NotImplementedError("rec / sub / struct / array types")
                if form != 0x60:
                    raise Malformed("malformed function type")
                ps = [r.vt() for _ in range(r.u())]
                rs = [r.vt() for _ in range(r.u())]
                mod["types"].append((ps, rs))
        elif sid == 2:
            for _ in range(r.u()):
                m, n, k = r.name(), r.name(), r.u8()
                if k == 0:
                    mod["imports"].append(("func", m, n, r.u()))
                elif k == 1:
                    mod["ttypes"].append(r.vt(True))
                    mod["imports"].append(("table", m, n, r.limits()))
                elif k == 2:
                    mod["imports"].append(("mem", m, n, r.limits()))
                elif k == 3:
                    t = r.vt()
                    mod["imports"].append(("global", m, n, (t, r.mut())))
                elif k == 4:
                    raise NotImplementedError("tag import")
                else:
                    raise Malformed("malformed import kind")
        elif sid == 3:
            mod["funcs"] = [r.u() for _ in range(r.u())]
        elif sid == 4:
            for _ in range(r.u()):
                init = r.p < r.n and r.b[r.p] == 0x40
                if init:  # 0x40 0x00 reftype limits expr
                    r.p += 1
                    if r.u8() != 0:
                        raise Malformed("malformed table encoding")
                mod["ttypes"].append(r.vt(True))
                mod["tables"].append(r.limits())
                mod["tinit"].append(initexpr() if init else None)
        elif sid == 5:
            for _ in range(r.u()):
                mod["mems"].append(r.limits())
        elif sid == 6:
            for _ in range(r.u()):
                t = r.vt()
                mut = r.mut()
                mod["globals"].append((t, mut, initexpr()))
        elif sid == 7:
            for _ in range(r.u()):
                n = r.name()
                k = r.u8()
                if k > 4:  # 4: a tag, kept as before
                    raise Malformed("malformed export kind")
                mod["exports"].append((n, k, r.u()))
        elif sid == 8:
            mod["start"] = r.u()
        elif sid == 9:
            for _ in range(r.u()):
                fl = r.u()
                if fl > 7:
                    raise Malformed("malformed element segment kind")
                # bit 0: not active; bit 1: explicit table (active) / declarative; bit 2: expressions
                ti = r.u() if fl & 3 == 2 else 0
                off = initexpr() if not fl & 1 else None
                rt = 0x70 if fl & 4 else (0, -16)  # function indices are (ref func)
                if fl & 3:
                    if fl & 4:
                        rt = r.vt(True)
                    elif r.u8() != 0:
                        raise Malformed("malformed element kind")
                if fl & 4:
                    items = [initexpr() for _ in range(r.u())]
                else:
                    items = [("func", r.u()) for _ in range(r.u())]
                mod["elem_info"].append((None if fl & 1 else ti, off, rt, items))
                mod["elem_mode"].append(
                    "active" if not fl & 1 else "declarative" if fl & 2 else "passive"
                )
        elif sid == 10:
            for _ in range(r.u()):
                sz = r.u()
                e = r.p + sz
                if e > end:
                    raise Malformed("unexpected end of section or function")
                r.n = e
                groups = [(r.u(), r.vt()) for _ in range(r.u())]
                total = sum(n for n, _ in groups)  # counted before anything is allocated
                if total > M32:
                    raise Malformed("too many locals")
                if total > LOCALS_LIMIT:
                    raise ImplLimit("too many locals: %d" % total)
                locs = []
                for n, t in groups:
                    locs += [t] * n
                body = data[r.p : e]
                if datacount is None and b"\xfc" in body:  # memory.init / data.drop need the count
                    if any(i[0] in (0xFC08, 0xFC09) for i in decode(body)):
                        raise Malformed("data count section required")
                mod["codes"].append((locs, body))
                r.p = e
                r.n = end
        elif sid == 11:
            for _ in range(r.u()):
                fl = r.u()
                if fl == 0:
                    off = initexpr()
                    mod["datas"].append((off, r.bytes(r.u())))
                    mod["data_mem"].append(0)
                elif fl == 1:
                    mod["datas"].append((None, r.bytes(r.u())))
                    mod["data_mem"].append(None)
                elif fl == 2:
                    mod["data_mem"].append(r.u())
                    off = initexpr()
                    mod["datas"].append((off, r.bytes(r.u())))
                else:
                    raise Malformed("malformed data segment kind")
        elif sid == 12:
            datacount = r.u()
        if r.p != end:
            raise Malformed("section size mismatch")
        r.n = len(data)
    if len(mod["funcs"]) != len(mod["codes"]):
        raise Malformed("function and code section have inconsistent lengths")
    if datacount is not None and datacount != len(mod["datas"]):
        raise Malformed("data count and data section have inconsistent lengths")
    return mod


# ----------------------------------------------------------------------------- instruction decode
MEMOPS = set(range(0x28, 0x3F))


@_guard
def decode(body):
    r = R(body)
    out = []
    depth = 1  # open blocks, the function body itself included
    while r.p < len(body):
        if depth == 0:
            raise Malformed("operators remaining after end of function")
        op = r.u8()
        if op in (0x02, 0x03, 0x04):
            depth += 1
            b = body[r.p]
            if b == 0x40:
                r.p += 1
                bt = ([], [])
            elif b in TN:
                r.p += 1
                bt = ([], [b])
            elif b in (0x63, 0x64):
                bt = ([], [r.vt()])
            else:
                idx = r.s(33)
                if idx < 0:
                    raise (
                        NotImplementedError("block type")
                        if (idx & 0x7F) in UNSUPPORTED_TYPES
                        else Malformed("malformed block type")
                    )
                bt = ("idx", idx)
            out.append((op, bt))
        elif op in (
            0x0C,
            0x0D,
            0x10,
            0x12,
            0x14,
            0x15,
            0xD5,
            0xD6,
            0x20,
            0x21,
            0x22,
            0x23,
            0x24,
            0x25,
            0x26,
            0xD2,
        ):
            out.append((op, r.u()))
        elif op == 0x0E:
            ls = [r.u() for _ in range(r.u())]
            out.append((op, ls, r.u()))
        elif op in (0x11, 0x13):
            ti = r.u()
            tb = r.u()
            out.append((op, ti, tb))
        elif op in MEMOPS:
            al = r.u()
            if al >= 0x80:
                raise Malformed("malformed memop flags")
            mi = 0
            if al & 0x40:
                mi = r.u()
                al &= 0x3F
            off = r.u64()
            out.append((op, off, al, mi))
        elif op in (0x3F, 0x40):
            out.append((op, r.u()))
        elif op == 0x41:
            out.append((op, r.s(32) & M32))
        elif op == 0x42:
            out.append((op, r.s(64) & M64))
        elif op == 0x43:
            out.append((op, _f32c(r.bytes(4))))
        elif op == 0x44:
            out.append((op, struct.unpack("<d", r.bytes(8))[0]))
        elif op == 0x1C:
            out.append((0x1B, [r.vt() for _ in range(r.u())]))
        elif op == 0xD0:
            out.append((op, r.ht()))
        elif op == 0xFC:
            sub = r.u()
            if sub <= 7:
                out.append((0xFC00 + sub,))
            elif sub == 8:
                seg = r.u()
                out.append((0xFC08, seg, r.u()))
            elif sub == 9:
                out.append((0xFC09, r.u()))
            elif sub == 10:
                d = r.u()
                out.append((0xFC0A, d, r.u()))
            elif sub == 11:
                out.append((0xFC0B, r.u()))
            elif sub == 12:  # table.init elem table
                e = r.u()
                out.append((0xFC0C, e, r.u()))
            elif sub == 13:
                out.append((0xFC0D, r.u()))
            elif sub == 14:  # table.copy dst src
                d = r.u()
                out.append((0xFC0E, d, r.u()))
            elif sub < 18:  # table.grow / size / fill
                out.append((0xFC00 + sub, r.u()))
            else:
                raise Malformed("illegal opcode 0xfc %d" % sub)
        elif op == 0xFD:
            out.append(simd.decode(r))
        elif op in SIMPLE_OPS:
            if op == 0x0B:
                depth -= 1
            out.append((op,))
        else:
            _illegal(op)
    if depth:
        raise Malformed("END opcode expected")
    return out
