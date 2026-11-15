"""The GC heap of a store on the JIT targets (docs/specs/S0013, "JIT: the GC heap"): one per store,
an i32 `Memory` (4 GiB + 32 MiB reserved, never moves) bound into every instance that uses GC as a
hidden memory after its declared memories. It is committed from its start, in 64 KiB pages: the
header page at creation, the type-info area as records are written, the objects at the first
allocation (with the rest of the type-info area below them) and as the heap grows. So every byte
below the size the contexts see is read-write: an explicit bounds check that passes never reaches
an uncommitted page (S0013 3b.1 review).

Objects live in two adjacent semispaces `[OBJ, OBJ + cap/2)` and `[OBJ + cap/2, OBJ + cap)`, both
committed, whose boundary moves when the heap grows (Wasmtime's scheme, D61): the active space is
`[lo, end)`, the cursor bumps in it, and a collection (collect.py) copies it into the other half.

Offsets and layouts are `gc.py`'s (the lowering reads the same records). A reference is a u32
offset; null is 0, an i31 `(v << 1) | 1`, a host value `(h << 2) | 2`.

Test-only switches (S0013 "Test switches"), read when the heap is created:
`WABOROS_JIT_GC_STRESS=1` collects at every allocation that may collect; `WABOROS_JIT_GC_POISON=1`
fills newly committed object space, the from-space and the free part of the active space after a
collection with 0xA5; `WABOROS_JIT_GC_VERIFY=1` checks the heap after every collection."""

import ctypes
import os
import struct
from typing import Any

from waboros.frontend.types import key as types_key
from waboros.frontend.validate import _root  # pyright: ignore[reportPrivateUsage, reportUnknownVariableType]
from waboros.targets.base import Trap, TrapCode
from waboros.targets.jit.common import collect, gc
from waboros.targets.jit.common.instance import Memory

PAGE = 1 << 16
S0 = 1 << 20  # the first semispace size; growth at least doubles it (S0013 Q2, D61)
HALVES = 2 * PAGE  # the object space is a multiple of this: both halves are whole pages
POISON = 0xA5


class GcHeap:
    def __init__(self, store: Any) -> None:
        self.mem = Memory(1, None)  # the header page: null, the cursor and the limit
        self.mem.store = store
        self.base = self.mem.res.addr
        self.u32(gc.CURSOR, gc.OBJ)  # empty: the first allocation commits the first chunk
        self.u32(gc.LIMIT, gc.OBJ)
        self.types = gc.TYPES  # next free byte of the type-info area
        self.records: dict[str, int] = {}  # canonical key -> record offset
        self.offsets: set[int] = set()  # every record offset (the walk check)
        self.display: dict[int, tuple[int, ...]] = {}
        self.kind: dict[int, int] = {}
        self.cap, self.lo, self.end = 0, gc.OBJ, gc.OBJ  # both halves; the active space [lo, end)
        self.collector: Any = None  # (instance, export indices) of collector.wasm (collect.py)
        self.collections = 0
        self.poison = os.environ.get("WABOROS_JIT_GC_POISON") == "1"
        self.stress = os.environ.get("WABOROS_JIT_GC_STRESS") == "1"
        self.verify = os.environ.get("WABOROS_JIT_GC_VERIFY") == "1"

    def u32(self, off: int, v: int | None = None) -> int:
        w = ctypes.c_uint32.from_address(self.base + off)
        if v is not None:
            w.value = v
        return w.value

    def _commit(self, end: int) -> None:
        """Commit up to `end` (rounded up to 64 KiB, so whole pages on every OS) and publish it as
        the memory's size to every context, after the commit (explicit checks compare with it)."""
        end = -(-end // PAGE) * PAGE
        if end > self.mem.pages * PAGE:
            self.mem.res.commit(end)
            self.mem.pages = end // PAGE
            for ctx, word in self.mem.users:
                ctx[word + 1] = end

    def rtt(self, mod: Any, ti: int) -> int:
        """The offset of the type-info record of struct or array type `ti` of `mod`, written on
        first use, its supertype first: equal for equivalent types of every module of the store."""
        k = types_key(mod, ti)
        off = self.records.get(k)
        if off is not None:
            return off
        comp, sups, final = mod["tdefs"][ti][:3]
        sup = self.rtt(mod, sups[0]) if sups else None  # the depth is at most 63 (the spec's limit)
        if comp[0] == "struct":
            offs, size = gc.struct_layout(mod, comp[1])
            refs = [o for o, (st, _) in zip(offs, comp[1], strict=True) if _root(mod, st)]
            kind = gc.KIND_STRUCT
        else:
            assert comp[0] == "array", "no record for a function type before step 5"
            size, refs, kind = gc.storage_size(mod, comp[1][0]), [], gc.KIND_ARRAY
        display = (*(self.display[sup] if sup is not None else ()), self.types)
        nrefs = len(refs) if kind == gc.KIND_STRUCT else int(_root(mod, comp[1][0]))
        words = [kind | final << 8, len(display) - 1, size, nrefs, *display, *refs]
        off, n = self.types, -(-4 * len(words) // 8) * 8
        if off + n > gc.OBJ:
            raise MemoryError("GC type area exhausted")
        self._commit(off + n)
        buf = (ctypes.c_char * n).from_address(self.base + off)
        struct.pack_into(f"<{len(words)}I", buf, 0, *words)
        self.types += n
        self.records[k], self.display[off], self.kind[off] = off, display, kind
        self.offsets.add(off)
        return off

    def alloc(self, size: int, inst: Any = None) -> int:
        """The offset of `size` new bytes (a multiple of 8), as the code's fast path bumps them;
        `room` when they do not fit (a collection only with `inst`, see there)."""
        if self.u32(gc.CURSOR) + size > self.u32(gc.LIMIT):
            self.room(size, inst)
        cur = self.u32(gc.CURSOR)  # after a collection: in the new space
        self.u32(gc.CURSOR, cur + size)
        return cur

    def room(self, r: int, inst: Any = None) -> None:
        """Room for `r` bytes past the cursor, or MemoryError: ALLOC's retry never spins (B7).
        `inst`, the instance whose service asks, may collect (D1); without it (instantiation,
        constant expressions) the heap only grows. The first allocation grows (D2); after a
        collection that leaves more than half live, the heap grows, collecting first when the
        upper half is active, so the lower one is (D3)."""
        if not self.cap:
            self._grow(r)
        elif inst is not None and (self.stress or self.u32(gc.CURSOR) + r > self.end):
            collect.collect(inst, self)
            if 2 * (self.u32(gc.CURSOR) - self.lo + r) > self.cap // 2:
                if self.end > gc.OBJ + self.cap // 2:
                    collect.collect(inst, self)
                self._grow(r)
        elif self.u32(gc.CURSOR) + r > self.end:
            self._grow(r)
        cur = self.u32(gc.CURSOR)
        if cur + r > self.end:
            raise MemoryError("GC heap out of memory")
        self.u32(gc.LIMIT, cur + r if self.stress else self.end)  # stress: the next one is slow

    def _grow(self, r: int) -> None:
        """Move the boundary up so `r` bytes fit past the cursor in the lower half; objects stay
        where they are (`lo` too: with the upper half active and no collection allowed, the old
        upper half becomes part of the lower one, [OBJ, lo) unused until the next collection).
        The heap's end stays below 2**32 (gc.CAP_MAX)."""
        cur = self.u32(gc.CURSOR)
        cap = max(2 * self.cap, 2 * S0, 4 * (cur - self.lo + r), 2 * (cur - gc.OBJ + r))
        cap = min(gc.CAP_MAX, -(-cap // HALVES) * HALVES)
        if cur + r > gc.OBJ + cap // 2:
            raise MemoryError("GC heap out of memory")
        old = max(gc.OBJ, self.mem.pages * PAGE)
        self._commit(gc.OBJ + cap)
        if self.poison and gc.OBJ + cap > old:
            ctypes.memset(self.base + old, POISON, gc.OBJ + cap - old)
        self.cap, self.end = cap, gc.OBJ + cap // 2

    @property
    def cursor(self) -> int:
        return self.u32(gc.CURSOR)

    @property
    def limit(self) -> int:
        return self.u32(gc.LIMIT)

    def new_struct(self, mod: Any, ti: int, vals: list[int] | None) -> int:
        """A struct of type `ti` with the field values `vals` (bit patterns; None: the defaults),
        every field written: the collector never zeroes (S0013 Q2)."""
        fields = mod["tdefs"][ti][0][1]
        offs, size = gc.struct_layout(mod, fields)
        o = self.alloc(size)
        self.u32(o, self.rtt(mod, ti))
        self.u32(o + 4, 0)
        for (st, _), off, v in zip(fields, offs, vals or [0] * len(fields), strict=True):
            self.put(o + off, gc.storage_size(mod, st), v)
        return o

    def put(self, at: int, n: int, v: int) -> None:
        """The low n bytes of `v` at offset `at`, little-endian."""
        ctypes.memmove(self.base + at, (v & ((1 << 8 * n) - 1)).to_bytes(n, "little"), n)

    def new_array(
        self, mod: Any, ti: int, n: int, fill: int = 0, vals: Any = None, inst: Any = None
    ) -> int:  # fmt: skip
        """An array of type `ti`, n elements of `fill` or `vals` (`()`: none written, the caller
        writes them); past Wasmtime's bound, a trap. `inst`: the allocation may collect."""
        st = mod["tdefs"][ti][0][1][0]
        es = gc.storage_size(mod, st)
        if n > gc.limit(st):
            raise Trap(TrapCode.ALLOCATION_TOO_LARGE, "allocation size too large")
        o = self.alloc(-(-(gc.ARRAY_DATA + n * es) // 8) * 8, inst)
        self.u32(o, self.rtt(mod, ti))
        self.u32(o + 4, 0)
        self.u32(o + gc.ARRAY_LEN, n)
        if vals is None:
            self.fill(o + gc.ARRAY_DATA, es, fill, n)
        for k, v in enumerate(vals or ()):
            self.put(o + gc.ARRAY_DATA + k * es, es, v)
        return o

    def fill(self, at: int, es: int, v: int, n: int) -> None:
        """n elements of es bytes, each `v`, from offset `at`: one written, then doubled."""
        if n:
            self.put(at, es, v)
        done = 1
        while done < n:
            k = min(done, n - done)
            ctypes.memmove(self.base + at + done * es, self.base + at, k * es)
            done += k

    def is_object(self, w: int) -> bool:
        """`w` is the offset of an object: in the active space, aligned, its header a record."""
        return self.lo <= w < self.u32(gc.CURSOR) and not w & 7 and self.u32(w) in self.offsets

    def kind_of(self, w: int) -> str:
        return "struct" if self.kind[self.u32(w)] == gc.KIND_STRUCT else "array"
