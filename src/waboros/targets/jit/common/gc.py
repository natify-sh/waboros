"""GC support in the one-pass JIT core (docs/specs/S0013), shared by every architecture. At step
3a: the stack maps. A safepoint is every call site; per site the core knows the slot shift to the
callee's frame and which value-stack slots of this frame hold a reference of the any or extern
hierarchy, the backend where its native frame keeps the return address (`Backend.sites`).

The `STACKMAPS` section (id 15; ARTIFACT.md part 2 from step 3a.4), little-endian: `u32 nsites`;
per site, sorted by `ret`: `u32 ret, u32 shift, u16 ret_at, u16 up, u32 map`; then `u32 nmaps` and
per map `u32 nslots`, `ceil(nslots / 32)` u32 words of `refs` (bit i: slot i of the frame holds a
reference) and as many of `live` (a subset; step 7, D65: the walk forwards `live` and nulls the
rest of `refs`). `map` 0 means no reference slot, k >= 1 the k-th map of the pool; equal maps are
stored once."""

import struct
from typing import Any

from waboros.frontend import types as T
from waboros.frontend.validate import _root  # pyright: ignore[reportPrivateUsage, reportUnknownVariableType]
from waboros.targets.base import ALLOC_LIMIT, TrapCode
from waboros.targets.jit.common import compiler as core
from waboros.targets.jit.common import simd
from waboros.targets.jit.common.backend import Imm, Slot
from waboros.targets.jit.common.context import (
    ALLOC,
    ARRAY_FILL,
    ARRAY_INIT_DATA,
    ARRAY_INIT_ELEM,
    ARRAY_NEW,
    ARRAY_NEW_DATA,
    ARRAY_NEW_ELEM,
    EXCEPTION,
    STATUS_EXCEPTION,
)

Site = tuple[int, int, int, int, int, int, int]  # ret, shift, ret_at, up, nslots, refs, live
SiteMap = tuple[int, int, int, int, int, int]  # decoded: shift, ret_at, up, nslots, refs, live

# The GC opcodes (0xFB00 + sub, and ref.eq): a backend compiles GC modules when its `supported`
# set has them all (S0013 3b)
OPS = frozenset((*range(0xFB00, 0xFB1F), 0xD3))

# The heap (S0013 "JIT: the GC heap"; ARTIFACT.md part 3), offsets in the hidden memory: null 0,
# the allocation cursor (u32) at CURSOR and its limit at LIMIT; type-info records from TYPES to
# TID_MAP, the tid map to OBJ; objects from OBJ in two adjacent semispaces of CAP bytes together,
# CAP <= CAP_MAX, so the heap's end stays below 2**32 (the collector's bounds are u32; S0013 5.3,
# D61)
CURSOR, LIMIT = 8, 12
TYPES, OBJ = 64 << 10, 64 << 20
# the tid map (S0013 5.6): [TID_MAP, OBJ), the u32 record offset of function type id t at
# TID_MAP + 4t (0: none), for t < TIDS; type-info records lie in [TYPES, TID_MAP)
TID_MAP, TIDS = 60 << 20, 1 << 20
CAP_MAX = (1 << 32) - OBJ - (128 << 10)
# an object: u32 type-info offset, u32 collector bits; a struct's fields from 8, an array's u32
# length at ARRAY_LEN and its elements from ARRAY_DATA
ARRAY_LEN, ARRAY_DATA = 8, 16
# a type-info record (u32 words): kind | final << 8, depth, size (struct) or element size (array),
# number of traced reference fields (an array: 1 if its elements are traced), the display (depth + 1
# record offsets, the last its own), then the offsets of the traced fields; a function type's record
# (kind 3) has its depth and display only (S0013 5.6)
R_KIND, R_DEPTH, R_SIZE, R_NREFS, R_DISPLAY = 0, 4, 8, 12, 16
KIND_STRUCT, KIND_ARRAY, KIND_FUNC = 1, 2, 3
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


def needs_heap(mod: Any) -> bool:
    """The module gets the heap and the GC group: it uses GC, or it allocates or reads exception
    objects, a `throw` or a `try_table` with a `catch` or `catch_ref` clause (S0013 step 6, D6
    (b)); `throw_ref` and `catch_all(_ref)` read only the pending word."""
    insns = (ins for body in mod.get("insns") or () for ins in body)
    return uses_gc(mod) or any(
        ins[0] == 0x08 or ins[0] == 0x1F and any(c[1] is not None for c in ins[2]) for ins in insns
    )


def thrown(mod: Any) -> list[int]:
    """The tags the module throws: one RTT word each, after the types' (S0013 step 6, D4)."""
    return sorted({ins[1] for body in mod.get("insns") or () for ins in body if ins[0] == 0x08})


def subtype_sites(mod: Any) -> bool:
    """The code calls `is_subtype` (S0013 5.6): a call_indirect / return_call_indirect, ref.test,
    ref.cast or br_on_cast* whose function type is not final."""
    tds = mod.get("tdefs") or ()
    open_ = {i for i, td in enumerate(tds) if td[0][0] == "func" and not td[2]}
    for ins in (ins for body in mod.get("insns") or () for ins in body) if open_ else ():
        op = ins[0]
        if (op in (0x11, 0x13) or 0xFB14 <= op <= 0xFB17) and ins[1] in open_:
            return True
        if op in (0xFB18, 0xFB19) and ins[4] in open_:
            return True
    return False


def storage_size(mod: Any, st: Any) -> int:
    """Bytes of a field or element of storage type `st` in the JIT heap: packed and numeric types
    their size, an any-, extern- or exn-hierarchy reference 4 (a u32 word), funcref 8."""
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


def exn_layout(mod: Any, params: Any) -> tuple[list[int], int]:
    """An exception object's parameter offsets and size: a struct of the u64 tag identity (at 8),
    then the tag's parameters (S0013 step 6, D2)."""
    offs, size = struct_layout(mod, [(0x7E, False), *((p, False) for p in params)])
    return offs[1:], size


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
    pool: dict[tuple[int, int, int], int] = {}
    out = [struct.pack("<I", len(sites))]
    for ret, shift, ret_at, up, n, refs, live in sites:
        assert ret_at < 1 << 16 and up < 1 << 16 and refs >> n == 0 and live & ~refs == 0
        k = pool.setdefault((n, refs, live), len(pool) + 1) if refs else 0
        out.append(struct.pack("<IIHHI", ret, shift, ret_at, up, k))
    out.append(struct.pack("<I", len(pool)))
    for n, refs, live in pool:  # in index order (dicts keep insertion order)
        words = [b >> 32 * i & 0xFFFFFFFF for b in (refs, live) for i in range((n + 31) // 32)]
        out.append(struct.pack(f"<I{len(words)}I", n, *words))
    return b"".join(out)


def decode(data: bytes) -> dict[int, SiteMap]:
    """ret -> (shift, ret_at, up, nslots, refs, live), zeros for map 0. Checked, as instantiation
    reads it (D65): a table in another form (step 3a's, one bitmap per map) raises ValueError."""
    try:
        (n,) = struct.unpack_from("<I", data)
        raw = [struct.unpack_from("<IIHHI", data, 4 + 16 * i) for i in range(n)]
        at = 4 + 16 * n
        (nmaps,) = struct.unpack_from("<I", data, at)
        at += 4
        maps = [(0, 0, 0)]
        for _ in range(nmaps):
            (ns,) = struct.unpack_from("<I", data, at)
            w = (ns + 31) // 32
            words = struct.unpack_from(f"<{2 * w}I", data, at + 4)
            refs, live = (sum(x << 32 * i for i, x in enumerate(words[h : h + w])) for h in (0, w))
            if not refs or refs >> ns or live & ~refs:
                raise ValueError(f"STACKMAPS: map {len(maps)} is not (refs, live ⊆ refs)")
            maps.append((ns, refs, live))
            at += 4 + 8 * w
    except struct.error as e:
        raise ValueError(f"STACKMAPS: truncated ({e})") from None
    if at != len(data) or any(r[4] > nmaps for r in raw):
        raise ValueError("STACKMAPS: trailing bytes or a map index past the pool")
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
    """The type of every allocation the code makes inline and every concrete cast target (a
    constant expression or a service allocates in the runtime, which finds the record itself)."""
    out = {
        ins[1] if ins[0] < 0xFB18 else ins[4]
        for body in mod.get("insns") or ()
        for ins in body
        if ins[0] in (0xFB00, 0xFB01, 0xFB06, 0xFB07, 0xFB08) or 0xFB14 <= ins[0] <= 0xFB19
    }
    return {t for t in out if t >= 0}


def sigs(mod: Any) -> set[int]:
    """Function types that casts name: their signature words (the canonical type id)."""
    return {t for t in _targets(mod) if mod["tdefs"][t][0][0] == "func"}


def rtts(mod: Any) -> list[int]:
    """Struct and array types the module allocates or casts to: one RTT word each."""
    return sorted(t for t in _targets(mod) if mod["tdefs"][t][0][0] != "func")


def subtype(fn: Any, pc: int, h: int, rec: Any, ti: int, top: int, miss: Any) -> None:
    """Fall through if the function record `rec` (non-null) has type `ti` or a declared subtype,
    else jump to `miss` (S0013 5.6, Wasmtime's shape): its type id against the signature word; for
    a non-final `ti`, then `is_subtype(q, t)` of the collector module through the IS_SUBTYPE record,
    its frame (q, t; the result in q) at slots h, h + 1: a safepoint whose map covers [0, h).
    Nothing above h - 1 is read after the call (B1)."""
    b, tos, q = fn.b, fn.TOS, Slot(fn.L + h)
    final = fn.m.mod["tdefs"][ti][2]
    if final:
        q = scratch(fn, h, 0)
    fn.maxh = max(fn.maxh, h + 1 + (not final))
    b.func_type(tos, rec)
    b.move(q, tos, True)
    b.ctx_load(tos, fn.m.layout.sig(fn.m.sigs[ti]), False)
    if final:
        b.cmp_branch("ne", q, tos, miss, True)
        return
    hit = b.new_label()
    b.cmp_branch("eq", q, tos, hit, True)
    b.move(Slot(fn.L + h + 1), tos, True)
    b.call_record(fn.f, fn.m.layout.subtype(), fn.L + h)
    fn.site(pc, fn.L + h, h, top)
    b.cmp_branch("eq", q, Imm(0), miss, True)
    b.bind(hit)


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
    fn.hi = max(fn.hi, h + k + 1)  # for the check in `site` (B1)
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
    for label, retry, size, arg, pc, error, keep, top, hi in fn.gccold:
        b.bind(label)
        b.move(arg, size, True)
        f.error, outer = error, f.error
        b.call_host(f, fn.m.layout.service(ALLOC), arg.k)
        fn.hi = hi  # the scratch slots taken before the allocation (B1)
        fn.site(pc, 0, keep, top)
        f.error = outer
        b.jump(retry)
    for code, label in fn.gctraps.items():
        b.bind(label)
        b.trap(f, code)


def _mem(fn: Any) -> int:
    lay = fn.m.layout
    return lay.memory(lay.memories)


def alloc(fn: Any, pc: int, word: int, size: Any, h: int) -> None:
    """A new object of the type whose RTT is context word `word`, of `size` bytes (an `Imm` or a
    `Slot`), its header written, its offset in `tos`. The operand stack is in slots (a safepoint):
    slots [0, h) are the frame's. ALLOC's argument is the topmost scratch slot: a nested entry's
    frame starts above it, and the retry reads `rtt` and `size` again (B1, ARTIFACT.md "Host
    functions")."""
    b, f, hw = fn.b, fn.f, _mem(fn)
    rtt, new = scratch(fn, h, 0), scratch(fn, h, 1)
    assert isinstance(size, Imm) or size.k >= fn.L + h + 2  # a scratch slot below `arg` (B1)
    arg = scratch(fn, h, 2 if isinstance(size, Imm) else size.k - fn.L - h + 1)
    b.ctx_load(fn.TOS, word, False)
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
    fn.gccold.append((slow, retry, size, arg, pc, f.error, arg.k - fn.L + 1, h, fn.hi))


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
    alloc(fn, pc, fn.m.layout.rtt(fn.m.rtts[ti]), Imm(size), h)
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


def throw(fn: Any, pc: int, t: int) -> None:
    """`throw` (S0013 step 6): a new exception object, allocated as `struct.new` is (its ALLOC
    call a safepoint over the payload, which is read from its slots after it, I4), then its tag
    identity at 8, the pending word (EXCEPTION's target) and status 14 to the frame's error
    label. A failed allocation is status 13, which no pad catches."""
    b, f, tos, lay = fn.b, fn.f, fn.TOS, fn.m.layout
    params = fn.m.tagp[t]
    offs, size = exn_layout(fn.m.mod, params)
    fn.flush()
    h = len(fn.st)
    h0 = k = h - simd.width(params)
    alloc(fn, pc, lay.rtt(fn.m.exn[t]), Imm(size), h)
    for p, off in zip(params, offs, strict=True):
        _set(fn, p, tos, off, Slot(fn.L + k))
        k += 1 + (p == 0x7B)
    new = scratch(fn, h, 1)  # dead after the allocation; below ALLOC's argument
    b.move(new, tos, True)
    b.ctx_load(tos, lay.tag(t), False)
    b.store(f, 8, new, 8, tos, _mem(fn), a64=False, lo=0)
    b.move(tos, new, True)
    b.ctx_store(EXCEPTION, tos, True)  # a zero-extended u32 offset
    b.move(tos, Imm(STATUS_EXCEPTION), False)  # tos is the status register
    b.jump(f.error)
    del fn.st[h0:]
    fn.dead = True


def catch(fn: Any, t: int, dst: int, s: Slot, miss: Any) -> int:
    """A landing pad's clause for tag `t` (S0013 step 6, D1 (b)): to `miss` unless the pending
    object's identity equals the tag's, compared in 64 bits (a forged or stale identity matches
    nothing), else its parameters into slots dst, dst + 1, ...; returns their count. `s` is a
    scratch slot that only the compare uses. Every read is a checked load of the heap."""
    b, f, tos, hw = fn.b, fn.f, fn.TOS, _mem(fn)
    params = fn.m.tagp[t]
    offs, _ = exn_layout(fn.m.mod, params)
    b.ctx_load(tos, EXCEPTION, True)
    b.load(f, 8, False, True, tos, tos, 8, hw, a64=False, lo=0)
    b.move(s, tos, True)
    b.ctx_load(tos, fn.m.layout.tag(t), False)
    b.cmp_branch("ne", s, tos, miss, True)
    k = dst
    for p, off in zip(params, offs, strict=True):
        b.ctx_load(tos, EXCEPTION, True)
        if p == 0x7B:
            b.simd(f, 0xFD000, Slot(k), (tos,), (off, hw), a64=False, lo=0)
            fn.maxh = max(fn.maxh, k - fn.L + simd.SCRATCH)
        else:
            n = storage_size(fn.m.mod, p)
            b.load(f, n, False, n == 8, tos, tos, off, hw, a64=False, lo=0)
            b.move(Slot(k), tos, True)
        k += 1 + (p == 0x7B)
    return k - dst


def ref_test(fn: Any, pc: int, x: Any, ht: int, null: bool, fail: Any, h: int) -> None:
    """Fall through when `x` is a `ht` (null too if `null`), else jump to `fail`. Uses `tos` and
    scratch slots from h; a non-final concrete function type calls `is_subtype` (the stack in
    slots)."""
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
    elif kind == "func":  # the record's type id, then is_subtype if not final (S0013 5.6)
        subtype(fn, pc, h, x, ht, h, fail)
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
        td = fn.m.mod["tdefs"][ht] if ht >= 0 else None
        if td is not None and td[0][0] == "func" and not td[2]:  # is_subtype: a safepoint
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
    elif o < 0xFB14:
        array(fn, pc, ins)
    else:
        cast(fn, pc, ins)


def limit(st: Any) -> int:
    """The longest array of storage type `st` that does not trap ALLOCATION_TOO_LARGE: Wasmtime's
    bound by its element size (every reference 4 bytes), whatever the JIT's own (S0013)."""
    return ALLOC_LIMIT[_SIZES.get(st, 4)]


def _range(fn: Any, a: Any, i: Any, n: Any, log: int, h: int, k: int) -> Slot:
    """Array `a` non-null (NULL_REFERENCE) and i + n within its length (ARRAY_OUT_OF_BOUNDS, in 64
    bits: no wrap); scratch slot h + k gets the address of element i, formed in 32 bits."""
    b, tos, s = fn.b, fn.TOS, scratch(fn, h, k)
    b.null_check(fn.f, a, TrapCode.NULL_REFERENCE)
    b.load(fn.f, 4, False, False, tos, a, ARRAY_LEN, _mem(fn), a64=False, lo=0)
    b.move(s, tos, True)
    b.binop("add", tos, i, n, True)
    b.cmp_branch("gt_u", tos, s, trap(fn, TrapCode.ARRAY_OUT_OF_BOUNDS), True)
    b.binop("shl", tos, i, Imm(log), False)
    b.binop("add", tos, tos, a, False)
    b.binop("add", tos, tos, Imm(ARRAY_DATA), False)
    b.move(s, tos, True)
    return s


def _pad(fn: Any, k: int) -> None:
    """Stack entry k (a scalar value, in its slot) takes two slots, as a v128 would: the service
    reads every element value as two words (the entries above move up one)."""
    for j in range(len(fn.st) - 1, k, -1):
        fn.b.move(Slot(fn.L + j + 1), Slot(fn.L + j), True)
    fn.b.move(Slot(fn.L + k + 1), Imm(0), True)
    fn.st.insert(k + 1, core.SLOTE)
    fn.maxh = max(fn.maxh, len(fn.st))


def array(fn: Any, pc: int, ins: Any) -> None:
    """Array instructions: allocation, element access and length inline; the bulk operators inline
    when they are a plain memory_fill / memory_copy on the heap (1-byte fills, every copy), else
    the GC group's services (S0013 "Services"). Trap order as `pyc` and Wasmtime (LESSONS 102)."""
    b, f, o, tos, mod = fn.b, fn.f, ins[0], fn.TOS, fn.m.mod
    if o == 0xFB0F:  # array.len
        (a,) = take(fn, 1)
        b.null_check(f, a, TrapCode.NULL_REFERENCE)
        b.load(f, 4, False, False, tos, a, ARRAY_LEN, _mem(fn), a64=False, lo=0)
        fn.push((core.TOS, None))
        return
    st = mod["tdefs"][ins[1]][0][1][0]
    es = storage_size(mod, st)
    log, word = es.bit_length() - 1, fn.m.layout.service
    if o in (0xFB0B, 0xFB0C, 0xFB0D, 0xFB0E):  # array.get / get_s / get_u / set
        v = simd._pair(fn) if o == 0xFB0E and st == 0x7B else None  # pyright: ignore[reportPrivateUsage]
        ops = take(fn, 2 + (o == 0xFB0E and v is None))
        a, i = ops[0], ops[1]
        b.null_check(f, a, TrapCode.NULL_REFERENCE)
        b.load(f, 4, False, False, tos, a, ARRAY_LEN, _mem(fn), a64=False, lo=0)
        b.cmp_branch("ge_u", i, tos, trap(fn, TrapCode.ARRAY_OUT_OF_BOUNDS), False)
        b.binop("shl", tos, i, Imm(log), False)  # the address in 32 bits: inside the reservation
        b.binop("add", tos, tos, a, False)
        if o == 0xFB0E:
            _set(fn, st, tos, ARRAY_DATA, v if v is not None else ops[2])
        else:
            _field(fn, st, tos, ARRAY_DATA, o == 0xFB0C)
        return
    fn.flush()  # the rest are safepoints or bulk operators: every operand in its slot
    h = len(fn.st)
    at = [Slot(fn.L + k) for k in range(h)]
    if o in (0xFB06, 0xFB07) and (o == 0xFB07 or es == 1):  # new_default, new of bytes: inline
        n, v = at[h - 1], (Imm(0) if o == 0xFB07 else at[h - 2])
        b.cmp_branch("gt_u", n, Imm(limit(st)), trap(fn, TrapCode.ALLOCATION_TOO_LARGE), True)
        size = scratch(fn, h, 2)  # below ALLOC's argument: read again after the call (B1)
        b.binop("shl", tos, n, Imm(log), True)
        b.binop("add", tos, tos, Imm(ARRAY_DATA + 7), True)
        b.binop("and", tos, tos, Imm(-8 & (1 << 64) - 1), True)
        b.move(size, tos, True)
        alloc(fn, pc, fn.m.layout.rtt(fn.m.rtts[ins[1]]), size, h)
        obj, d = scratch(fn, h, 3), scratch(fn, h, 4)
        b.store(f, 4, tos, ARRAY_LEN, n, _mem(fn), a64=False, lo=0)
        b.move(obj, tos, True)
        b.binop("add", tos, tos, Imm(ARRAY_DATA), False)
        b.move(d, tos, True)
        b.binop("shl", tos, n, Imm(log), True)
        b.move(size, tos, True)  # every element written, defaults included (S0013 Q2)
        b.memory_fill(f, d, v, size, _mem(fn), a64=False)
        b.move(tos, obj, True)
        del fn.st[h - (2 if o == 0xFB06 else 1) :]
        fn.push((core.TOS, None))
    elif o == 0xFB08:  # array.new_fixed: N values from their slots
        nslots = ins[2] * (1 + (st == 0x7B))
        rtt, size = fn.m.layout.rtt(fn.m.rtts[ins[1]]), -(-(ARRAY_DATA + ins[2] * es) // 8) * 8
        alloc(fn, pc, rtt, Imm(size), h)
        b.store(f, 4, tos, ARRAY_LEN, Imm(ins[2]), _mem(fn), a64=False, lo=0)
        for k in range(ins[2]):
            _set(fn, st, tos, ARRAY_DATA + k * es, at[h - nslots + k * (1 + (st == 0x7B))])
        del fn.st[h - nslots :]
        fn.push((core.TOS, None))
    elif o == 0xFB10 and es == 1:  # array.fill of bytes: memory_fill after the checks
        a, i, v, n = at[h - 4 :]
        d = _range(fn, a, i, n, 0, h, 0)
        b.memory_fill(f, d, v, n, _mem(fn), a64=False)
        del fn.st[h - 4 :]
    elif o == 0xFB11:  # array.copy: dst null, dst range, src null, src range, then memmove
        da, di, sa, si, n = at[h - 5 :]
        d, s_ = _range(fn, da, di, n, log, h, 0), _range(fn, sa, si, n, log, h, 1)
        b.binop("shl", tos, n, Imm(log), True)
        nb = scratch(fn, h, 2)
        b.move(nb, tos, True)
        b.memory_copy(f, d, s_, nb, _mem(fn), _mem(fn), a64=(False, False))
        del fn.st[h - 5 :]
    elif o in (0xFB06, 0xFB10):  # array.new, array.fill of wider elements: a service
        if st != 0x7B:
            _pad(fn, h - 2)  # the value: two words
        j, nargs = (ARRAY_NEW, 3) if o == 0xFB06 else (ARRAY_FILL, 5)
        fn.service(pc, j, nargs, [ins[1]], o == 0xFB06, word(j))
    else:  # new_data, new_elem (offset, n); init_data, init_elem (array, d, s, n): services
        j = {0xFB09: ARRAY_NEW_DATA, 0xFB0A: ARRAY_NEW_ELEM, 0xFB12: ARRAY_INIT_DATA}.get(o)
        j = ARRAY_INIT_ELEM if j is None else j
        fn.service(pc, j, 2 if o < 0xFB12 else 4, [ins[1], ins[2]], o < 0xFB12, word(j))


def call_open(fn: Any, ti: int, table: int, pc: int, tail: bool) -> None:
    """call_indirect / return_call_indirect to a type that is not final: the element's record,
    bounds and null as the backend checks them, kept in slot h0 + n (the index's), then `subtype`
    above it (S0013 Q11, 5.6), then a call (or tail call) through the record."""
    m, b, f = fn.m, fn.b, fn.f
    ps, rs = m.types[ti]
    n, nres = simd.width(ps), simd.width(rs)
    index = fn.pop()  # stays valid across flush (call_indirect does the same)
    fn.flush()
    h0 = len(fn.st) - n
    b.table_get(f, fn.TOS, m.layout.table(table), index, a64=fn.t64(table))  # index read first
    b.null_check(f, fn.TOS, TrapCode.INDIRECT_CALL_TO_NULL)
    rec = scratch(fn, h0 + n, 0)  # below is_subtype's frame (B1)
    b.move(rec, fn.TOS, True)
    subtype(fn, pc, h0 + n + 1, fn.TOS, ti, h0 + n + 1, trap(fn, TrapCode.BAD_SIGNATURE))
    if tail:
        b.tail_ref(f, rec, fn.L + h0, n)
        del fn.st[h0:]
        fn.dead = True
        return
    b.call_ref(f, rec, fn.L + h0)
    fn.site(pc, fn.L + h0, h0, h0 + n + 1)
    del fn.st[h0:]
    fn.st += [core.SLOTE] * nres
    fn.maxh = max(fn.maxh, h0 + max(n, nres))
