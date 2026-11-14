"""The GC heap of a store on the JIT targets (docs/specs/S0013, "JIT: the GC heap"): one per store,
an i32 `Memory` (4 GiB + 32 MiB reserved, never moves) bound into every instance that uses GC as a
hidden memory after its declared memories. It is committed from its start, in 64 KiB pages: the
header page at creation, the type-info area as records are written, the objects of semispace A at
the first allocation (with the rest of the type-info area below them) and as the heap grows. So
every byte below the size the contexts see is read-write: an explicit bounds check that passes
never reaches an uncommitted page (S0013 3b.1 review). Until the collector (step 5) the heap only
grows.

Offsets and layouts are `gc.py`'s (the lowering reads the same records). A reference is a u32
offset; null is 0, an i31 `(v << 1) | 1`, a host value `(h << 2) | 2`.

Test-only: `WABOROS_JIT_GC_POISON=1` fills newly committed object space with 0xA5 (S0013 "Test
switches": the free part of the active space), so an allocation that skips a field store reads
poison instead of zero."""

import ctypes
import os
import struct
from typing import Any

from waboros.frontend.types import key as types_key
from waboros.frontend.validate import _root  # pyright: ignore[reportPrivateUsage, reportUnknownVariableType]
from waboros.targets.jit.common import gc
from waboros.targets.jit.common.instance import Memory

PAGE = 1 << 16
CHUNK = 1 << 20  # the first object space committed; growth doubles it (S0013 Q2: S from 1 MiB)
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
        self.poison = os.environ.get("WABOROS_JIT_GC_POISON") == "1"

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

    def alloc(self, size: int) -> int:
        """The offset of `size` new bytes (a multiple of 8), as the code's fast path bumps them;
        the heap grows when they do not fit (never a collection before step 5)."""
        cur = self.u32(gc.CURSOR)
        if cur + size > self.u32(gc.LIMIT):
            self.grow(size)
        self.u32(gc.CURSOR, cur + size)
        return cur

    def grow(self, need: int) -> None:
        """The allocation service's slow path at step 3b: commit at least `need` more bytes past
        the cursor (doubling, from 1 MiB) up to the end of semispace A, then move the limit."""
        cur, lim = self.u32(gc.CURSOR), self.u32(gc.LIMIT)
        if cur + need > gc.A_END:
            raise MemoryError("GC heap out of memory")
        new = min(gc.A_END, max(cur + need, lim + max(CHUNK, lim - gc.OBJ)))
        new = -(-new // PAGE) * PAGE
        self._commit(new)
        if self.poison:
            ctypes.memset(self.base + lim, POISON, new - lim)
        self.u32(gc.LIMIT, new)

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
            n = gc.storage_size(mod, st)
            ctypes.memmove(self.base + o + off, (v & ((1 << 8 * n) - 1)).to_bytes(n, "little"), n)
        return o

    def is_object(self, w: int) -> bool:
        """`w` is the offset of an object: in the allocated space, aligned, its header a record."""
        return gc.OBJ <= w < self.u32(gc.CURSOR) and not w & 7 and self.u32(w) in self.offsets

    def kind_of(self, w: int) -> str:
        return "struct" if self.kind[self.u32(w)] == gc.KIND_STRUCT else "array"
