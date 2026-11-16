"""A collection of a store's JIT GC heap (docs/specs/S0013, "Roots and the collector"; D61): the
roots gathered here, forwarded by the collector module (`runtime/gc/collector.wasm`, Cheney's
algorithm) run as a nested JIT entry from the allocating service, then written back.

The collector instance is made per store at its first collection and kept out of
`store.instances`: `store.interrupt()` never reaches its interrupt word, so no poll cuts a
collection in half, and no walk or gathering sees it. Its frame starts at `st.vsp`, above the
service's slots (B1, ARTIFACT.md "Host functions"). From the first call into it until the heap's
words are consistent again the store is marked lost (`st.lost`): a collector trap, a failed
verification or an exception there (KeyboardInterrupt) leaves it so, and the store refuses every
later call. Host values of `externref` the collection did not meet are freed afterwards."""

import ctypes
import heapq
import importlib.resources
from collections.abc import Iterator
from typing import Any

from waboros.frontend.validate import _root  # pyright: ignore[reportPrivateUsage, reportUnknownVariableType]
from waboros.targets.base import Extern, Trap, TrapCode
from waboros.targets.jit.common import gc, gcverify, guard, walk
from waboros.targets.jit.common import instance as inst_
from waboros.targets.jit.common.context import INTERRUPT, NSTACK_LIMIT, VSTACK_LIMIT

REPORT, OVERFLOW, ROOTS, NROOTS = 0x800, 16, 0x1000, 7680  # the module's header regions (5.2)
HANDLES = 16384  # handle numbers the report has a bit for
VSTACK_RESERVE = 4 << 10  # value stack the collector's entries need at most (a deviation, D61)
POISON = 0xA5
LOST = "GC heap lost: a collection did not complete"
_compiled: dict[tuple[str, str], Any] = {}  # (target, bounds mode) -> the module, compiled


def _collector(inst: Any, heap: Any) -> tuple[Any, dict[str, int]]:
    """The store's collector instance and its export indices, made at the first collection."""
    if heap.collector is None:
        from waboros.frontend.binary import parse  # pyright: ignore[reportUnknownVariableType]
        from waboros.frontend.validate import validate  # pyright: ignore[reportUnknownVariableType]
        from waboros.targets.base import TARGETS

        t: Any = TARGETS[inst.owner]()
        key = (inst.owner, guard.mode())
        c = _compiled.get(key)
        if c is None:
            wasm = (importlib.resources.files("waboros.runtime") / "gc/collector.wasm").read_bytes()
            m: Any = parse(wasm)
            validate(m)
            c = _compiled[key] = t.compile(m)
        store = inst.store
        coll = t.instantiate(c, [Extern("memory", None, heap.mem)], store)
        with store.interrupt_lock:  # an interrupt() mid-walk of its copy finishes before this
            store.instances.remove(coll)  # no interrupt, walk or gathering reaches it now
        coll.ctx[INTERRUPT] = 0  # JIT code may call is_subtype before any collection (5.6)
        inst_._stacks(store).walk_ranges = None  # pyright: ignore[reportPrivateUsage]
        heap.collector = (coll, {n: i for n, k, i in c.module["exports"] if k == 0})
    return heap.collector


def _ones(base: int, bits: int) -> Iterator[tuple[int, int]]:
    """Runs (address, words) of the set bits of a frame's map, slot k at base + 8k."""
    k = 0
    while bits:
        z = (bits & -bits).bit_length() - 1  # the zeros below the next run
        bits, k = bits >> z, k + z
        n = (~bits & (bits + 1)).bit_length() - 1  # the run's length
        yield base + 8 * k, n
        bits, k = bits >> n, k + n


def _gather(inst: Any, st: Any, coll: Any) -> tuple[list[tuple[int, int]], list[Any]]:
    """Every root as runs (address, words) of u64 words; a Python list goes through a temporary
    array, returned with it to be written back in place."""
    runs: list[tuple[int, int]] = []
    back: list[Any] = []

    def keep(items: list[int]) -> None:
        a = (ctypes.c_uint64 * len(items))(*items)
        back.append((items, a))
        runs.append((ctypes.addressof(a), len(items)))

    for frames, _ in walk.walk(inst.store, st, inst.tramp):
        for owner, (_, _, _, _, bits), _, base in frames:
            assert owner is not coll, "the walk met a collector frame"
            runs += _ones(base, bits)
    globals_: set[int] = set()
    tables: set[int] = set()
    for i in inst.store.instances:
        if not isinstance(i, inst_.JitInstance):
            continue
        mod: Any = i.module
        for g, t in enumerate(i.gtypes[: len(i.gaddr)]):  # imported: the owner's word
            if _root(mod, t) and i.gaddr[g] not in globals_:
                globals_.add(i.gaddr[g])
                runs.append((i.gaddr[g], 1))
        for tab in i.tables:
            if tab.refs and tab.size and id(tab) not in tables:
                tables.add(id(tab))
                runs.append((ctypes.addressof(tab.buf), tab.size))
        for s, items in enumerate(i.elems):  # passive, not dropped
            if items and _root(mod, mod["elem_info"][s][2]):
                keep(items)
    if st.gc_handles:
        keep(st.gc_handles)
    # the pending word: 0 or the last exception thrown (S0013 step 6, D7: a defence; nothing
    # reads it after a collection while the invariants hold)
    runs.append((ctypes.addressof(st.exn), 1))
    return runs, back


def _words(runs: list[tuple[int, int]]) -> list[int]:
    return [w for a, n in runs for w in (ctypes.c_uint64 * n).from_address(a)]


def _free(st: Any, heap: Any) -> None:
    """Free the host values no root or live object holds (D61), unless the report overflowed;
    their numbers are reused lowest first (`_host_word`)."""
    if heap.u32(OVERFLOW):
        return
    rep = ctypes.string_at(heap.base + REPORT, HANDLES // 8)
    for h, v in enumerate(st.externs, 1):
        if v is not None and (h >= HANDLES or not rep[h >> 3] >> (h & 7) & 1):
            st.externs[h - 1] = None
            st.extern_ids.pop(id(v), None)
            heapq.heappush(st.free_externs, h)


def collect(inst: Any, heap: Any) -> None:
    """Collect `heap` from the service `inst` is serving: the active space into the other half."""
    st = inst_._stacks(inst.store)  # pyright: ignore[reportPrivateUsage]
    if st.lost:
        raise RuntimeError(st.lost)
    sp, _, frame = st.exit
    assert sp and st.vsp > frame, "a collection outside a host call that set the frame base (B1)"
    coll, idx = _collector(inst, heap)
    if st.vsp + VSTACK_RESERVE > st.vtop:  # before anything moves
        raise Trap(TrapCode.STACK_OVERFLOW, "call stack exhausted")
    low = inst_._stack_low()  # pyright: ignore[reportPrivateUsage]
    coll.ctx[NSTACK_LIMIT] = (
        low + (64 << 10) if low is not None else max(0, st.nlimit - (256 << 10))
    )
    coll.ctx[VSTACK_LIMIT] = st.vtop
    # an interrupt() made while the instance was being made (it was in store.instances then) or
    # by any later path is not for the collector: its polls never fire
    coll.ctx[INTERRUPT] = 0
    runs, back = _gather(inst, st, coll)
    half, lo, cur = heap.cap // 2, heap.lo, heap.cursor
    to = gc.OBJ + half if heap.end <= gc.OBJ + half else gc.OBJ
    view: Any = None
    before = ""
    if heap.verify:
        view = memoryview((ctypes.c_char * (gc.OBJ + heap.cap)).from_address(heap.base)).cast("B")
        before = gcverify.digest(view, heap.offsets, _words(runs))

    def call(name: str, *args: int) -> int:
        try:
            return (inst_.Instance.call(coll, idx[name], list(args)) or [0])[0]
        except Trap as e:
            raise RuntimeError(f"{LOST}: the collector trapped ({e})") from e

    st.lost = LOST
    call("begin", lo, cur, to, to + half)
    buf, pieces, used = heap.base + ROOTS, list[tuple[int, int, int]](), 0
    for a, n in runs:  # in batches of at most NROOTS words, each piece moved back after
        while n:
            k = min(n, NROOTS - used)
            ctypes.memmove(buf + 8 * used, a, 8 * k)
            pieces.append((a, used, k))
            a, n, used = a + 8 * k, n - k, used + k
            if used == NROOTS:
                call("roots", used)
                for p, at, m in pieces:
                    ctypes.memmove(p, buf + 8 * at, 8 * m)
                pieces, used = [], 0
    if used:
        call("roots", used)
        for p, at, m in pieces:
            ctypes.memmove(p, buf + 8 * at, 8 * m)
    new = call("finish") & 0xFFFFFFFF
    for items, arr in back:
        items[:] = list(arr)
    heap.lo, heap.end = to, to + half
    heap.u32(gc.CURSOR, new)
    heap.u32(gc.LIMIT, heap.end)
    st.gcrefs = {st.gc_handles[r.index]: r for r in st.gcrefs.values()}  # B5: words moved
    _free(st, heap)
    if heap.poison:
        ctypes.memset(heap.base + lo, POISON, cur - lo)
        ctypes.memset(heap.base + new, POISON, heap.end - new)
    heap.collections += 1
    if heap.verify:
        after = _words(runs)
        live = {h for h, v in enumerate(st.externs, 1) if v is not None}
        gcverify.check(view, heap.offsets, heap.lo, new, after, live)
        if gcverify.digest(view, heap.offsets, after) != before:
            raise RuntimeError("GC verify: the reachable graph changed in a collection")
    st.lost = None
