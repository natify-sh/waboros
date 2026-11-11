"""GC support in the one-pass JIT core (docs/specs/S0013), shared by every architecture. At step
3a: the stack maps. A safepoint is every call site; per site the core knows the slot shift to the
callee's frame and which value-stack slots of this frame hold a reference of the any or extern
hierarchy, the backend where its native frame keeps the return address (`Backend.sites`).

The `STACKMAPS` section (id 15; ARTIFACT.md part 2 from step 3a.4), little-endian: `u32 nsites`;
per site, sorted by `ret`: `u32 ret, u32 shift, u16 ret_at, u16 up, u32 map`; then `u32 nmaps` and
per map `u32 nslots` and `ceil(nslots / 32)` u32 words (bit i = slot i of the frame). `map` 0
means no reference slot, k >= 1 the k-th map of the pool; equal maps are stored once."""

import struct

Site = tuple[int, int, int, int, int, int]  # ret, shift, ret_at, up, nslots, bits


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
