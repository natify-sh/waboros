"""A verifier of a JIT GC heap image (S0013 step 5.1), for tests and `WABOROS_JIT_GC_VERIFY=1`: it
reads a heap image and nothing of the collector, so it can judge it. `check` tests the form of the
active space; `digest` hashes the object graph reachable from the roots independently of where the
objects sit, so a heap before and after a moving collection have the same digest.

The image is any buffer indexed by heap offset (the layouts are `gc.py`'s: an object is a record
offset, the collector bits, then its fields). `records` holds the offsets of the type-info records
(`GcHeap.offsets`); a record is read from the image. A reference word is 0 (null), `(v << 1) | 1`
(an i31), `(h << 2) | 2` (a host value, `h` in `handles`) or the offset of an object."""

import hashlib
import struct
from collections.abc import Collection, Sequence

from waboros.targets.jit.common import gc


class HeapError(Exception):
    """A malformed heap; the message names the offset and the problem."""


class _Image:
    def __init__(self, mem: bytes | bytearray | memoryview, records: Collection[int]) -> None:
        self.mem, self.records = mem, records
        self.cache: dict[int, tuple[int, int, int, list[int]]] = {}

    def u32(self, at: int) -> int:
        if at < 0 or at + 4 > len(self.mem):
            raise HeapError(f"{at:#x}: outside the image")
        return struct.unpack_from("<I", self.mem, at)[0]

    def info(self, o: int) -> tuple[int, int, list[int]]:
        """The record offset, the size and the offsets of the traced words of the object at `o`."""
        r = self.u32(o)
        if r not in self.records:
            raise HeapError(f"object {o:#x}: bad record {r:#x}")
        if r not in self.cache:
            kind, depth, size, nrefs = (self.u32(r + k) for k in (0, 4, 8, 12))
            at = r + gc.R_DISPLAY + 4 * (depth + 1)
            self.cache[r] = (kind & 0xFF, size, nrefs, [self.u32(at + 4 * i) for i in range(nrefs)])
        kind, size, nrefs, fields = self.cache[r]
        if kind == gc.KIND_ARRAY:  # `size` is the element size
            n = self.u32(o + gc.ARRAY_LEN)
            total = -(-(gc.ARRAY_DATA + n * size) // 8) * 8
            fields = [gc.ARRAY_DATA + 4 * k for k in range(n)] if nrefs else []
        elif kind == gc.KIND_STRUCT and size >= 8 and not size & 7:
            total = size
        else:
            raise HeapError(f"object {o:#x}: record {r:#x} has kind {kind} and size {size}")
        if o + total > len(self.mem):
            raise HeapError(f"object {o:#x}: overruns the image")
        return r, total, [o + f for f in fields]


def check(
    mem: bytes | bytearray | memoryview,
    records: Collection[int],
    lo: int,
    cursor: int,
    roots: Sequence[int],
    handles: Collection[int],
) -> None:
    """Walk the active space `[lo, cursor)` object by object (a known record, collector bits 0, the
    size from the record, no overrun of the cursor), then test every traced field and every root."""
    img, starts, o = _Image(mem, records), dict[int, list[int]](), lo
    while o < cursor:
        _, size, fields = img.info(o)
        if img.u32(o + 4):
            raise HeapError(f"object {o:#x}: collector bits {img.u32(o + 4):#x}, not 0")
        if o + size > cursor:
            raise HeapError(f"object {o:#x}: size {size} overruns the cursor {cursor:#x}")
        starts[o] = fields
        o += size

    def ref(w: int, where: str) -> None:
        if w == 0 or w & 1:
            return
        if w & 3 == 2:
            if w >> 2 not in handles:
                raise HeapError(f"{where}: dead host handle {w >> 2} (word {w:#x})")
        elif w not in starts:
            what = "points into the middle of an object" if lo <= w < cursor else "dangles"
            raise HeapError(f"{where}: reference {w:#x} {what}")

    for o, fields in starts.items():
        for at in fields:
            ref(img.u32(at), f"object {o:#x} field {at:#x}")
    for i, w in enumerate(roots):
        ref(w, f"root {i}")


def digest(
    mem: bytes | bytearray | memoryview, records: Collection[int], roots: Sequence[int]
) -> str:
    """SHA-256 (hex) over a breadth-first walk from the roots in order. Each root is hashed by its
    kind; each object by its record offset and its bytes, with every traced word replaced by the
    ordinal of its target (or the null, i31 or host word as it is), so only the graph counts."""
    img, order, queue, h = _Image(mem, records), dict[int, int](), list[int](), hashlib.sha256()

    def enc(w: int) -> int:
        if w == 0 or w & 3:  # null, i31, host value
            return w
        if w not in order:
            order[w] = len(order)
            queue.append(w)
        return (order[w] + 1) << 3 & 0xFFFFFFFF

    for w in roots:
        h.update(struct.pack("<I", enc(w)))
    for o in queue:  # grows while it is walked
        r, size, fields = img.info(o)
        body = bytearray(mem[o : o + size])
        for at in fields:
            struct.pack_into("<I", body, at - o, enc(img.u32(at)))
        h.update(struct.pack("<II", r, size) + body)
    return h.hexdigest()
