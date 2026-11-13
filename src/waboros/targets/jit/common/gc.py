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

from waboros.frontend.validate import _root  # pyright: ignore[reportPrivateUsage, reportUnknownVariableType]

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
