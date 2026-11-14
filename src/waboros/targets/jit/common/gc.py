"""GC support in the one-pass JIT core (docs/specs/S0013), shared by every architecture. At step
3a: the stack maps. A safepoint is every call site; per site the core knows the slot shift to the
callee's frame and which value-stack slots of this frame hold a reference of the any or extern
hierarchy, the backend where its native frame keeps the return address (`Backend.sites`).

The `STACKMAPS` section (id 15; ARTIFACT.md part 2 from step 3a.4), little-endian: `u32 nsites`;
per site, sorted by `ret`: `u32 ret, u32 shift, u16 ret_at, u16 up, u32 map`; then `u32 nmaps` and
per map `u32 nslots` and `ceil(nslots / 32)` u32 words (bit i = slot i of the frame). `map` 0
means no reference slot, k >= 1 the k-th map of the pool; equal maps are stored once."""

import struct
from typing import Any

from waboros.frontend import types as T
from waboros.frontend.validate import _root  # pyright: ignore[reportPrivateUsage, reportUnknownVariableType]
from waboros.targets.base import TrapCode
from waboros.targets.jit.common import compiler as core
from waboros.targets.jit.common import simd
from waboros.targets.jit.common.backend import Imm, Slot
from waboros.targets.jit.common.context import ALLOC, FUNC_SUBTYPE

Site = tuple[int, int, int, int, int, int]  # ret, shift, ret_at, up, nslots, bits

# The GC opcodes (0xFB00 + sub, and ref.eq): a backend compiles GC modules when its `supported`
# set has them all (S0013 3b)
OPS = frozenset((*range(0xFB00, 0xFB1F), 0xD3))

# The heap (S0013 "JIT: the GC heap"; ARTIFACT.md part 3), offsets in the hidden memory: null 0,
# the allocation cursor (u32) at CURSOR and its limit at LIMIT; type-info records from TYPES to
# OBJ; objects from OBJ (semispace A) to A_END
CURSOR, LIMIT = 8, 12
TYPES, OBJ, A_END = 64 << 10, 64 << 20, (2 << 30) + (32 << 20)
# an object: u32 type-info offset, u32 collector bits; a struct's fields from 8, an array's u32
# length at ARRAY_LEN and its elements from ARRAY_DATA
ARRAY_LEN, ARRAY_DATA = 8, 16
# a type-info record (u32 words): kind | final << 8, depth, size (struct) or element size (array),
# number of traced reference fields (an array: 1 if its elements are traced), the display (depth + 1
# record offsets, the last its own), then the offsets of the traced fields
R_KIND, R_DEPTH, R_SIZE, R_NREFS, R_DISPLAY = 0, 4, 8, 12, 16
KIND_STRUCT, KIND_ARRAY = 1, 2
_SIZES = {0x78: 1, 0x77: 2, 0x7F: 4, 0x7D: 4, 0x7E: 8, 0x7C: 8, 0x7B: 16}


def uses_gc(mod: Any) -> bool:
    """The module needs the GC heap and group: GC types or instructions, or a call_indirect /
    return_call_indirect whose expected type is not final (its subtypes may come from another
    module, S0013 Q11)."""
    if mod.get("gc"):
        return True
    open_ = {i for i, td in enumerate(mod.get("tdefs") or ()) if not td[2]}
    insns = (ins for body in mod.get("insns") or () for ins in body)
    return bool(open_) and any(ins[0] in (0x11, 0x13) and ins[1] in open_ for ins in insns)


def storage_size(mod: Any, st: Any) -> int:
    """Bytes of a field or element of storage type `st` in the JIT heap: packed and numeric types
    their size, an any- or extern-hierarchy reference 4 (a u32 word), funcref and exnref 8."""
    n = _SIZES.get(st)
    return n if n is not None else 4 if _root(mod, st) else 8


def struct_layout(mod: Any, fields: Any) -> tuple[list[int], int]:
    """The offset of each field (declaration order, natural alignment, after the 8-byte header)
    and the struct's size, a multiple of 8 and at least 8."""
    at, offs = 8, list[int]()
    for st, _ in fields:
        n = storage_size(mod, st)
        at = -(-at // n) * n
        offs.append(at)
        at += n
    return offs, -(-at // 8) * 8


def slot_bits(refs: int, v128: int) -> int:
    """The validator's mask of value positions (`refs`) in slot numbering: a v128 below takes two
    slots (`v128`: the positions holding one, D41)."""
    if not v128:
        return refs
    out = s = i = 0
    while refs >> i:
        out |= (refs >> i & 1) << s
        s += 1 + (v128 >> i & 1)
        i += 1
    return out


def encode(sites: list[Site]) -> bytes:
    pool: dict[tuple[int, int], int] = {}
    out = [struct.pack("<I", len(sites))]
    for ret, shift, ret_at, up, n, bits in sites:
        assert ret_at < 1 << 16 and up < 1 << 16 and bits >> n == 0
        k = pool.setdefault((n, bits), len(pool) + 1) if bits else 0
        out.append(struct.pack("<IIHHI", ret, shift, ret_at, up, k))
    out.append(struct.pack("<I", len(pool)))
    for n, bits in pool:  # in index order (dicts keep insertion order)
        words = (n + 31) // 32
        out.append(
            struct.pack(f"<I{words}I", n, *((bits >> 32 * w) & 0xFFFFFFFF for w in range(words)))
        )
    return b"".join(out)


def decode(data: bytes) -> dict[int, tuple[int, int, int, int, int]]:
    """ret -> (shift, ret_at, up, nslots, bits); nslots and bits are 0 for map 0."""
    (n,) = struct.unpack_from("<I", data)
    raw = [struct.unpack_from("<IIHHI", data, 4 + 16 * i) for i in range(n)]
    at = 4 + 16 * n
    (nmaps,) = struct.unpack_from("<I", data, at)
    at += 4
    maps = [(0, 0)]
    for _ in range(nmaps):
        (ns,) = struct.unpack_from("<I", data, at)
        words = struct.unpack_from(f"<{(ns + 31) // 32}I", data, at + 4)
        maps.append((ns, sum(w << 32 * i for i, w in enumerate(words))))
        at += 4 + 4 * len(words)
    assert at == len(data), "STACKMAPS: trailing bytes"
    return {ret: (shift, ret_at, up, *maps[k]) for ret, shift, ret_at, up, k in raw}


# ---------------------------------------------------------------------- the lowering (S0013 3b)
# GC instructions on D39 methods only (Q9 (a)): every heap access is a `load`/`store`/`simd` on the
# hidden memory (`lower.bounds`: explicit checks or guard fault points), every address a u32. The
# core names one register, `tos`; a second value lives in a scratch slot above the operands. Null
# is checked on every access (the core keeps no operand types; S0013 3b decision B). Allocation is
# Wasmtime's and V8 Liftoff's bump shape: cursor + size against the limit, a cold call to the ALLOC
# service when full, then the code retries from the slots (no reference survives the call, I4).
# Casts follow the display test of V8's and SpiderMonkey's descriptions: a final type is one compare
# with its record, a non-final one the display entry at the target's depth.
STEP_3B2 = frozenset((*range(0xFB00, 0xFB06), *range(0xFB14, 0xFB1F), 0xD3))  # no arrays yet


def depth(mod: Any, ti: int) -> int:
    d = 0
    while mod["tdefs"][ti][1]:
        ti, d = mod["tdefs"][ti][1][0], d + 1
    return d


def consts(mod: Any) -> list[Any]:
    """Every constant expression of the module (globals, table initializers, element items) and
    every one nested in an allocating one, without recursion."""
    out: list[Any] = []
    todo = [g[2] for g in mod["globals"]] + [e for e in mod["tinit"] if e is not None]
    todo += [e for _, _, _, items in mod["elem_info"] for e in items]
    while todo:
        e = todo.pop()
        out.append(e)
        if e[0] == "gc":
            todo += e[2]
    return out


def _targets(mod: Any) -> set[int]:
    """The type of every allocation and concrete cast target, in code and constants."""
    out = {
        ins[1] if ins[0] < 0xFB18 else ins[4]
        for body in mod.get("insns") or ()
        for ins in body
        if 0xFB00 <= ins[0] <= 0xFB01 or 0xFB14 <= ins[0] <= 0xFB19
    }
    out |= {e[1][1] for e in consts(mod) if e[0] == "gc" and e[1][0] <= 0xFB01}
    return {t for t in out if t >= 0}


def sigs(mod: Any) -> set[int]:
    """Function types that casts name: their signature words (the canonical type id)."""
    return {t for t in _targets(mod) if mod["tdefs"][t][0][0] == "func"}


def rtts(mod: Any) -> list[int]:
    """Struct and array types the module allocates or casts to: one RTT word each."""
    return sorted(t for t in _targets(mod) if mod["tdefs"][t][0][0] != "func")


def subtype(fn: Any, pc: int, h: int, rec: Any, ti: int, top: int) -> Slot:
    """FUNC_SUBTYPE (S0013 Q11, a service until step 5) with its two slots at h, h + 1 (both in the
    frame): the first gets 1 if the function record `rec` has type `ti` or a declared subtype, else
    0; the second keeps the record. A safepoint whose map covers slots [0, h + 2)."""
    b, s, r = fn.b, scratch(fn, h, 0), scratch(fn, h, 1)
    b.move(r, rec, True)
    b.ctx_load(fn.TOS, fn.m.layout.sig(fn.m.sigs[ti]), False)
    b.move(s, fn.TOS, True)
    b.call_host(fn.f, fn.m.layout.service(FUNC_SUBTYPE), s.k)
    fn.site(pc, 0, h + 2, top)
    return s


def take(fn: Any, n: int) -> list[Any]:
    """The top n operands in wasm order, none in `tos` (spilled to its slot first, LESSONS 32), and
    `tos` free afterwards."""
    st = fn.st
    if st and st[-1][0] == core.CMP:
        fn.uncmp()
    base = len(st) - n
    for i in range(base, len(st)):
        if st[i][0] in (core.TOS, core.ADDK):
            fn.b.move(Slot(fn.L + i), fn.opnd_k(st[i], i), st[i][0] != core.ADDK)
            st[i] = core.SLOTE
    ops = [fn.opnd(st[i], i) for i in range(base, len(st))]
    del st[base:]
    fn.need_tos()
    return ops


def scratch(fn: Any, h: int, k: int) -> Slot:
    fn.maxh = max(fn.maxh, h + k + 1)
    return Slot(fn.L + h + k)


def trap(fn: Any, code: int) -> Any:
    """A label that traps with `code`, bound after the body (cold)."""
    if code not in fn.gctraps:
        fn.gctraps[code] = fn.b.new_label()
    return fn.gctraps[code]


def cold(fn: Any) -> None:
    """The cold code of the function, after its body: allocation slow paths (each a safepoint
    reporting to the `error` label it had), then the trap labels."""
    b, f = fn.b, fn.f
    for label, retry, size, arg, pc, error, keep, top in fn.gccold:
        b.bind(label)
        b.move(arg, size, True)
        f.error, outer = error, f.error
        b.call_host(f, fn.m.layout.service(ALLOC), arg.k)
        fn.site(pc, 0, keep, top)
        f.error = outer
        b.jump(retry)
    for code, label in fn.gctraps.items():
        b.bind(label)
        b.trap(f, code)


def _mem(fn: Any) -> int:
    lay = fn.m.layout
    return lay.memory(lay.memories)


def alloc(fn: Any, pc: int, ti: int, size: Any, h: int) -> None:
    """A new object of type `ti` and `size` bytes (an `Imm` or a `Slot`), its header written, its
    offset in `tos`. The operand stack is in slots (a safepoint): slots [0, h) are the frame's."""
    b, f, hw = fn.b, fn.f, _mem(fn)
    rtt, new, arg = scratch(fn, h, 0), scratch(fn, h, 1), scratch(fn, h, 2)
    b.ctx_load(fn.TOS, fn.m.layout.rtt(fn.m.rtts[ti]), False)
    b.move(rtt, fn.TOS, True)
    retry, slow = b.new_label(), b.new_label()
    b.bind(retry)
    b.load(f, 4, False, False, fn.TOS, Imm(0), CURSOR, hw, a64=False, lo=0)
    b.binop("add", fn.TOS, fn.TOS, size, True)  # 64-bit: no wrap
    b.move(new, fn.TOS, True)
    b.load(f, 4, False, False, fn.TOS, Imm(0), LIMIT, hw, a64=False, lo=0)
    b.cmp_branch("gt_u", new, fn.TOS, slow, True)
    b.store(f, 4, Imm(0), CURSOR, new, hw, a64=False, lo=0)
    b.binop("sub", fn.TOS, new, size, True)
    b.store(f, 8, fn.TOS, 0, rtt, hw, a64=False, lo=0)  # {rtt, collector bits 0}
    fn.gccold.append((slow, retry, size, arg, pc, f.error, h + 3, h))


def _field(fn: Any, st: Any, x: Any, off: int, sign: bool) -> None:
    """Push the field of storage type `st` at `x + off` (x non-null)."""
    b, mod = fn.b, fn.m.mod
    if st == 0x7B:
        h = len(fn.st)
        b.simd(fn.f, 0xFD000, Slot(fn.L + h), (x,), (off, _mem(fn)), a64=False, lo=0)
        fn.maxh = max(fn.maxh, h + simd.SCRATCH)
        fn.push(core.SLOTE)
        fn.push(core.SLOTE)
        return
    n = storage_size(mod, st)
    b.load(fn.f, n, sign, n == 8, fn.TOS, x, off, _mem(fn), a64=False, lo=0)
    fn.push((core.TOS, None))


def _set(fn: Any, st: Any, x: Any, off: int, v: Any) -> None:
    if st == 0x7B:
        fn.b.simd(fn.f, 0xFD00B, None, (x, v), (off, _mem(fn)), a64=False, lo=0)
    else:
        fn.b.store(fn.f, storage_size(fn.m.mod, st), x, off, v, _mem(fn), a64=False, lo=0)


def _offset(fn: Any, fields: Any, k: int) -> int:
    offs: list[int] = struct_layout(fn.m.mod, fields)[0]
    return offs[k]


def struct_new(fn: Any, pc: int, ti: int, default: bool) -> None:
    fields = fn.m.mod["tdefs"][ti][0][1]
    offs, size = struct_layout(fn.m.mod, fields)
    fn.flush()  # a safepoint (I2)
    h = len(fn.st)
    k = base = h - (0 if default else sum(1 + (st == 0x7B) for st, _ in fields))
    alloc(fn, pc, ti, Imm(size), h)
    for (st, _), off in zip(fields, offs, strict=True):
        if default:
            for half in range(1 + (st == 0x7B)):
                n = 8 if st == 0x7B else storage_size(fn.m.mod, st)
                fn.b.store(fn.f, n, fn.TOS, off + 8 * half, Imm(0), _mem(fn), a64=False, lo=0)
        else:
            _set(fn, st, fn.TOS, off, Slot(fn.L + k))
            k += 1 + (st == 0x7B)
    del fn.st[base:]
    fn.push((core.TOS, None))


def ref_test(fn: Any, pc: int, x: Any, ht: int, null: bool, fail: Any, h: int) -> None:
    """Fall through when `x` is a `ht` (null too if `null`), else jump to `fail`. Uses `tos` and
    scratch slots from h; a concrete function type calls FUNC_SUBTYPE (the stack in slots)."""
    b, f, mod, tos = fn.b, fn.f, fn.m.mod, fn.TOS
    hit = b.new_label()
    b.cmp_branch("eq", x, Imm(0), hit if null else fail, True)
    kind = mod["tdefs"][ht][0][0] if ht >= 0 else None
    if ht in (T.ANY, T.EXTERN, T.FUNC, T.EXN):
        pass
    elif ht in (T.NONE, T.NOEXTERN, T.NOFUNC, T.NOEXN):
        b.jump(fail)
    elif ht == T.EQ:  # an i31 (x1) or an object (00), not a host value (10)
        b.binop("and", tos, x, Imm(3), False)
        b.cmp_branch("eq", tos, Imm(2), fail, False)
    elif ht == T.I31:
        b.binop("and", tos, x, Imm(1), False)
        b.cmp_branch("eq", tos, Imm(0), fail, False)
    elif kind == "func":  # the record's type id against the signature word (Q11, until step 5)
        b.cmp_branch("eq", subtype(fn, pc, h, x, ht, h), Imm(0), fail, True)
    else:
        hw, rtt = _mem(fn), None
        if ht >= 0:
            rtt = scratch(fn, h, 0)
            b.ctx_load(tos, fn.m.layout.rtt(fn.m.rtts[ht]), False)
            b.move(rtt, tos, True)
        b.binop("and", tos, x, Imm(3), False)
        b.cmp_branch("ne", tos, Imm(0), fail, False)
        b.load(f, 4, False, False, tos, x, 0, hw, a64=False, lo=0)  # its record
        if rtt is None:  # struct or array
            b.load(f, 4, False, False, tos, tos, R_KIND, hw, a64=False, lo=0)
            b.binop("and", tos, tos, Imm(0xFF), False)
            want = KIND_STRUCT if ht == T.STRUCT else KIND_ARRAY
            b.cmp_branch("ne", tos, Imm(want), fail, False)
        elif mod["tdefs"][ht][2]:  # final: its record
            b.cmp_branch("ne", tos, rtt, fail, False)
        else:  # the display entry at the target's depth
            d, rec = depth(mod, ht), scratch(fn, h, 1)
            b.move(rec, tos, True)
            b.load(f, 4, False, False, tos, tos, R_DEPTH, hw, a64=False, lo=0)
            b.cmp_branch("lt_u", tos, Imm(d), fail, False)
            b.load(f, 4, False, False, tos, rec, R_DISPLAY + 4 * d, hw, a64=False, lo=0)
            b.cmp_branch("ne", tos, rtt, fail, False)
    b.bind(hit)


def cast(fn: Any, pc: int, ins: Any) -> None:
    op, b = ins[0], fn.b
    if op in (0xFB14, 0xFB15):  # ref.test (null)
        ht = ins[1]
        if ht >= 0 and fn.m.mod["tdefs"][ht][0][0] == "func":  # FUNC_SUBTYPE: a safepoint
            fn.flush()
        h = len(fn.st)
        (x,) = take(fn, 1)
        fail, done = b.new_label(), b.new_label()
        ref_test(fn, pc, x, ht, op == 0xFB15, fail, h)
        b.move(fn.TOS, Imm(1), False)
        b.jump(done)
        b.bind(fail)
        b.move(fn.TOS, Imm(0), False)
        b.bind(done)
        fn.push((core.TOS, None))
    elif op in (0xFB16, 0xFB17):  # ref.cast (null): the value stays, in its slot
        ht = ins[1]
        fn.flush()
        h = len(fn.st)
        ref_test(fn, pc, Slot(fn.L + h - 1), ht, op == 0xFB17, trap(fn, TrapCode.CAST_FAILURE), h)
    else:  # br_on_cast / br_on_cast_fail: flags, label, source and target heap types
        _, flags, depth_, _, ht = ins
        fn.flush()
        h = len(fn.st)
        c = fn.ctl[-1 - depth_]
        c.used = True
        x = Slot(fn.L + h - 1)
        miss, br = b.new_label(), b.new_label()
        on_fail = op == 0xFB19
        ref_test(fn, pc, x, ht, bool(flags & 2), br if on_fail else miss, h)
        if on_fail:
            b.jump(miss)
            b.bind(br)
        if fn.needs_moves(c):
            fn.moves(c)
        b.jump(c.label)
        b.bind(miss)


def op(fn: Any, ins: Any, pc: int) -> None:
    """One GC instruction (0xFB, or ref.eq)."""
    b, o = fn.b, ins[0]
    if o == 0xD3:  # ref.eq: a lazy compare of two zero-extended u32 words, as i32.eq
        y = fn.pop()
        x = fn.pop()
        fn.push((core.CMP, ("eq", x, y, False)))
    elif o in (0xFB1A, 0xFB1B):  # any.convert_extern, extern.convert_any: the same word
        pass
    elif o == 0xFB1C:  # ref.i31: the low 31 bits, tagged
        (x,) = take(fn, 1)
        b.binop("shl", fn.TOS, x, Imm(1), False)
        b.binop("or", fn.TOS, fn.TOS, Imm(1), False)
        fn.push((core.TOS, None))
    elif o in (0xFB1D, 0xFB1E):  # i31.get_s, i31.get_u
        (x,) = take(fn, 1)
        b.null_check(fn.f, x, TrapCode.NULL_REFERENCE)
        b.binop("shr_s" if o == 0xFB1D else "shr_u", fn.TOS, x, Imm(1), False)
        fn.push((core.TOS, None))
    elif o in (0xFB00, 0xFB01):
        struct_new(fn, pc, ins[1], o == 0xFB01)
    elif o in (0xFB02, 0xFB03, 0xFB04):  # struct.get / get_s / get_u
        fields = fn.m.mod["tdefs"][ins[1]][0][1]
        (x,) = take(fn, 1)
        b.null_check(fn.f, x, TrapCode.NULL_REFERENCE)
        _field(fn, fields[ins[2]][0], x, _offset(fn, fields, ins[2]), o == 0xFB03)
    elif o == 0xFB05:  # struct.set
        fields = fn.m.mod["tdefs"][ins[1]][0][1]
        st = fields[ins[2]][0]
        v = simd._pair(fn) if st == 0x7B else None  # pyright: ignore[reportPrivateUsage]
        ops = take(fn, 1 if v is not None else 2)
        x, v = (ops[0], v) if v is not None else (ops[0], ops[1])
        b.null_check(fn.f, x, TrapCode.NULL_REFERENCE)
        _set(fn, st, x, _offset(fn, fields, ins[2]), v)
    else:
        cast(fn, pc, ins)


def call_open(fn: Any, ti: int, table: int, pc: int, tail: bool) -> None:
    """call_indirect / return_call_indirect to a type that is not final: the element's record,
    bounds and null as the backend checks them, then FUNC_SUBTYPE (a service until step 5, S0013
    Q11), then a call (or tail call) through the record."""
    m, b, f = fn.m, fn.b, fn.f
    ps, rs = m.types[ti]
    n, nres = simd.width(ps), simd.width(rs)
    index = fn.pop()  # stays valid across flush (call_indirect does the same)
    fn.flush()
    h0 = len(fn.st) - n
    b.table_get(f, fn.TOS, m.layout.table(table), index, a64=fn.t64(table))  # index read first
    b.null_check(f, fn.TOS, TrapCode.INDIRECT_CALL_TO_NULL)
    s = subtype(fn, pc, h0 + n, fn.TOS, ti, h0 + n + 1)  # slots h0 + n (the index's), h0 + n + 1
    b.cmp_branch("eq", s, Imm(0), trap(fn, TrapCode.BAD_SIGNATURE), True)
    if tail:
        b.tail_ref(f, Slot(s.k + 1), fn.L + h0, n)
        del fn.st[h0:]
        fn.dead = True
        return
    b.call_ref(f, Slot(s.k + 1), fn.L + h0)
    fn.site(pc, fn.L + h0, h0, h0 + n + 1)
    del fn.st[h0:]
    fn.st += [core.SLOTE] * nres
    fn.maxh = max(fn.maxh, h0 + max(n, nres))
