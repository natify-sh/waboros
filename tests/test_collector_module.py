# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
"""The collector module (S0013 step 5.2, D54): `collector.wat` assembles to the committed
`collector.wasm`, which has only the structure and instructions its spec allows. It collects heap
images built by the JIT's own `GcHeap` to the same bytes on Wasmtime and on this machine's JIT, and
the 5.1 verifier judges the result (form, digest before = digest after). Malformed heaps trap."""

import ctypes
import importlib.resources
import struct
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from tests.jit import native_jit
from tests.jit.test_gc_heap import heap, mod
from waboros.frontend.binary import parse  # pyright: ignore[reportUnknownVariableType]
from waboros.frontend.validate import validate  # pyright: ignore[reportUnknownVariableType]
from waboros.runtime.store import StoreState
from waboros.targets import base
from waboros.targets.base import Extern, Trap, TrapCode
from waboros.targets.jit.common import gc
from waboros.targets.jit.common.gcverify import check, digest

WAT = Path(__file__).resolve().parents[1] / "src/waboros/runtime/gc/collector.wat"
WASM = (importlib.resources.files("waboros.runtime") / "gc/collector.wasm").read_bytes()
REPORT, OVER, ROOTS = 0x800, 16, 0x1000  # the handle report, its overflow flag, the roots buffer
# MVP control, parametric, variable, memory (no memory.grow 0x40) and numeric opcodes; memory.copy
CONTROL = {*range(0x00, 0x06), *range(0x0B, 0x11), 0x1A, 0x1B, *range(0x20, 0x25)}
ALLOWED = CONTROL | {*range(0x28, 0x40), *range(0x41, 0xC0), 0xFC0A}
TYPES = """(module (type $n (struct (field i32) (field (mut anyref)) (field (mut anyref))))
  (type $e (struct)) (type $a (array (mut anyref))) (type $b (array i8)))"""
I31 = 9 << 1 | 1


def host(h: int) -> int:
    return h << 2 | 2


def test_source_assembles_to_the_committed_bytes() -> None:
    wasmtime = pytest.importorskip("wasmtime")
    assert bytes(wasmtime.wat2wasm(WAT.read_text())) == WASM


def test_structure_and_instructions() -> None:
    m: Any = parse(WASM)
    validate(m)
    assert m["imports"] == [("mem", "gc", "heap", (1, None))]
    assert sorted(n for n, k, _ in m["exports"] if k == 0) == ["begin", "finish", "roots"]
    assert len(m["exports"]) == 3 and not m["mems"] and not m["tables"] and not m["tags"]
    assert not m["datas"] and not m["elem_mode"] and m["start"] is None and not m["gc"]
    ops = {ins[0] for body in m["insns"] for ins in body}
    assert ops <= ALLOWED, sorted(ops - ALLOWED)


class Heap:
    """A from-space [OBJ, cursor) made by the JIT's `GcHeap`, to-space the next MiB: live cycles
    a <-> b and t1 <-> t2, an empty struct e, an i8 array s, a reference array; two dead structs."""

    def __init__(self, big: bool) -> None:
        self.st = StoreState()
        h = self.h = heap(self.st)
        m = mod(TYPES)
        dead = h.new_struct(m, 0, [1, host(7), 0])  # a host word only garbage holds
        e = h.new_struct(m, 1, [])
        a = h.new_struct(m, 0, [5, 0, e])
        b = h.new_struct(m, 0, [6, a, I31])
        h.u32(a + 12, b)
        s = h.new_array(m, 3, 5, vals=[1, 2, 3, 4, 5])
        t1 = h.new_struct(m, 0, [7, 0, 0])  # t1 <-> t2 only through arr: t2 is copied last
        t2 = h.new_struct(m, 0, [8, t1, I31])
        h.u32(t1 + 12, t2)
        arr = h.new_array(m, 2, 7, vals=[a, b, I31, host(3), e, t1, host(20000) if big else 0])
        dead2 = h.new_struct(m, 0, [2, dead, 0])
        self.at = {"a": a, "b": b, "e": e, "arr": arr, "dead2": dead2}
        self.roots = [arr, a, a, I31, host(5), 0, s, b]  # a twice
        lim = h.limit
        h.grow(8)
        self.spaces = [gc.OBJ, h.cursor, lim, h.limit]
        self.view = memoryview((ctypes.c_char * h.limit).from_address(h.base)).cast("B")


Result = tuple[int, list[int], bytes, bytes, int] | str
Plan = list[tuple[list[int], list[list[int] | int]]]  # per collection: spaces, root batches


def collect(read: Callable[[int, int], bytes], write: Callable[[int, bytes], Any],
            call: Callable[..., Any], roots: list[int], plan: Plan) -> Result:  # fmt: skip
    """Per collection: begin; per batch: the roots into ROOTS, `roots(n)`, back; finish. A batch
    lists positions in `roots` (one sent twice is a duplicate root in to-space); an int n calls
    `roots(n)` on the buffer as it is. The last collection's result."""
    roots, cur = list(roots), 0
    for spaces, batches in plan:
        call("begin", *spaces)
        for b in batches:
            if isinstance(b, int):
                call("roots", b)
                continue
            write(ROOTS, struct.pack(f"<{len(b)}Q", *(roots[i] for i in b)))
            call("roots", len(b))
            for i, w in zip(b, struct.unpack(f"<{len(b)}Q", read(ROOTS, 8 * len(b))), strict=True):
                roots[i] = w
        cur = call("finish") & 0xFFFFFFFF
    to, over = plan[-1][0][2], struct.unpack("<I", read(OVER, 4))[0]
    return cur, roots, read(to, cur - to), read(REPORT, 0x800), over


def on_wasmtime(hp: Heap, roots: list[int], plan: Plan) -> Result:
    wt = pytest.importorskip("wasmtime")
    store, h = wt.Store(), hp.h
    mem = wt.Memory(store, wt.MemoryType(wt.Limits(h.mem.pages, None)))
    mem.write(store, ctypes.string_at(h.base, h.types), 0)  # header and records
    mem.write(store, ctypes.string_at(h.base + gc.OBJ, h.limit - gc.OBJ), gc.OBJ)
    ex = wt.Instance(store, wt.Module(store.engine, WASM), [mem]).exports(store)

    def call(name: str, *args: int) -> Any:
        return ex[name](store, *args)

    try:
        return collect(
            lambda o, n: bytes(mem.read(store, o, o + n)),
            lambda o, b: mem.write(store, b, o),
            call, roots, plan,
        )  # fmt: skip
    except wt.Trap as e:
        return e.trap_code.name


def on_jit(hp: Heap, roots: list[int], plan: Plan) -> Result:
    t, m, base_ = base.TARGETS[native_jit()](), cast(Any, parse(WASM)), hp.h.base
    validate(m)
    inst = t.instantiate(t.compile(m), [Extern("memory", None, hp.h.mem)], hp.st)
    idx = {n: i for n, k, i in m["exports"] if k == 0}

    def call(name: str, *args: int) -> int:
        return (inst.call(idx[name], [x & 0xFFFFFFFF for x in args]) or [0])[0]

    try:
        return collect(
            lambda o, n: ctypes.string_at(base_ + o, n),
            lambda o, b: ctypes.memmove(base_ + o, b, len(b)),
            call, roots, plan,
        )  # fmt: skip
    except Trap as e:
        return e.code.name


def both(hp: Heap, roots: list[int], plan: Plan) -> Result:
    """The JIT's result, after Wasmtime's on a copy of the image when wasmtime is installed."""
    try:
        ref = on_wasmtime(hp, roots, plan)
    except pytest.skip.Exception:
        ref = None
    got = on_jit(hp, roots, plan)
    assert ref is None or got == ref
    return got


def bits(report: bytes) -> set[int]:
    return {h for h in range(0x4000) if report[h >> 3] >> (h & 7) & 1}


@pytest.mark.parametrize(
    ("batches", "big"), [([list(range(8))], False), ([[0, 1, 2, 3], [4, 5, 6, 7], [0, 1]], True)]
)
def test_collects_the_live_graph_like_wasmtime(batches: list[list[int] | int], big: bool) -> None:
    hp = Heap(big)
    before = digest(hp.view, hp.h.offsets, hp.roots)
    got = both(hp, hp.roots, [(hp.spaces, batches)])
    assert not isinstance(got, str), got
    cur, roots, _, report, over = got
    assert cur - hp.spaces[2] == 48 + 24 * 5 + 8  # arr; a, b, s, t1, t2; e; not the dead ones
    check(hp.view, hp.h.offsets, hp.spaces[2], cur, roots, {3, 5, 20000})
    assert digest(hp.view, hp.h.offsets, roots) == before
    assert roots[1] == roots[2] and roots[3:6] == [I31, host(5), 0]
    assert bits(report) == {3, 5} and over == big  # not 7 (garbage); 20000 is past the bitmap


def test_a_second_collection_starts_a_new_report() -> None:
    """Back into the old space with only a as a root: a, b and e live, no host word met."""
    hp = Heap(True)
    to_lo, to_hi = hp.spaces[2:]
    before = digest(hp.view, hp.h.offsets, [hp.roots[1]])
    plan: Plan = [(hp.spaces, [list(range(8))]), ([to_lo, to_hi, gc.OBJ, to_lo], [[1]])]
    got = both(hp, hp.roots, plan)
    assert not isinstance(got, str), got
    cur, roots, _, report, over = got
    assert cur - gc.OBJ == 24 + 24 + 8 and not bits(report) and over == 0
    check(hp.view, hp.h.offsets, gc.OBJ, cur, [roots[1]], set())
    assert digest(hp.view, hp.h.offsets, [roots[1]]) == before


def record(hp: Heap, *words: int, at: int | None = None) -> int:
    """Type-info record `words` at `at` (default: the next free type-area offset); `-1` words
    stand for its own offset (the display's last entry)."""
    h = hp.h
    r = h.types if at is None else at
    words = tuple(r if w == -1 else w for w in words)
    ctypes.memmove(h.base + r, struct.pack(f"<{len(words)}I", *words), 4 * len(words))
    h.types = max(h.types, -(-(r + 4 * len(words)) // 8) * 8)
    return r


# Each case breaks one check (S0013 5.2 review): without that check, the module would not trap.
# "solo" cases give a (24 bytes, fields zeroed) the record `words` and send a alone.
SOLO = {
    "rec_below": (1, 0, 8, 0, -1),  # at 0x40, below the type area
    "rec_align": (1, 0, 8, 0, -1),  # at a 4-aligned offset
    "depth": (1, 64, 8, 0, *[0] * 64, -1),
    "display": (1, 0, 8, 0, 0x10000),
    "kind": (3, 0, 8, 0, -1),
    "ssize": (1, 0, 12, 0, -1),
    "es_range": (2, 0, 32, 0, -1),
    "arr_es4": (2, 0, 8, 1, -1),
    "nrefs": (1, 0, 16, 5, -1, 8, 8, 8, 8, 8),
    "off_lt8": (1, 0, 24, 1, -1, 4),
    "off_align": (1, 0, 24, 1, -1, 9),
    "off_end": (1, 0, 24, 1, -1, 24),
    "off_wrap": (1, 0, 24, 1, -1, 0xFFFFFFFC),  # s - 4: the word below to-space
}
OTHER = ["roots_n", "begin_64m", "begin_align", "begin_order", "overlap", "bits", "fwd_align",
         "fwd_range", "fwd_c_range", "fwd_c_align", "from_overrun", "outside", "high", "field",
         "to_hi"]  # fmt: skip


@pytest.mark.parametrize("bad", [*SOLO, *OTHER])
def test_a_malformed_heap_traps(bad: str) -> None:
    hp = Heap(False)
    roots, spaces, h, at = list(hp.roots), list(hp.spaces), hp.h, hp.at
    a = at["a"]
    batches: list[list[int] | int] = [list(range(8))]
    if bad in SOLO:
        where = {"rec_below": 0x40, "rec_align": h.types + 4}.get(bad)
        h.u32(a, record(hp, *SOLO[bad], at=where))
        h.put(a + 8, 16, 0)
        batches = [[1]]
        if bad == "off_wrap":  # the escape the review found: e's new offset written below to_lo
            h.u32(spaces[2] - 4, at["e"])
    elif bad == "roots_n":  # n << 3 wraps without the bound: nothing would be read
        batches = [0x20000000]
    elif bad == "begin_64m":
        spaces[0] = gc.OBJ - 0x10000
    elif bad == "begin_align":
        spaces[3] -= 4
    elif bad == "begin_order":  # to_hi < to_lo, with roots that copy nothing
        spaces[3], batches = spaces[2] - 8, [[3, 4, 5]]
    elif bad == "overlap":
        spaces[2] = spaces[1] - 8
    elif bad == "bits":
        h.u32(a + 4, 2)
    elif bad == "fwd_align":  # a valid-looking empty struct at a + 12, sent as a root
        h.u32(a + 12, h.u32(at["e"]))
        h.u32(a + 16, 0)
        roots[1], batches = a + 12, [[1]]
    elif bad == "fwd_range":  # e is a good object below the from-space
        spaces[0], roots[1], batches = a, at["e"], [[1]]
    elif bad in ("fwd_c_range", "fwd_c_align"):  # a forged forwarding header, after s is copied
        h.u32(a, spaces[3] + 64 if bad == "fwd_c_range" else spaces[2] + 2)
        h.u32(a + 4, 1)
        batches = [[6, 1]]
    elif bad == "from_overrun":  # the last object crosses from_hi
        roots[1], spaces[1], batches = at["dead2"], h.cursor - 8, [[1]]
    elif bad == "outside":
        roots[1] = spaces[3] + 8
    elif bad == "high":
        roots[0] |= 1 << 32
    elif bad == "field":  # b's first reference field outside both spaces
        h.u32(at["b"] + 12, 8)
    else:
        spaces[3] = spaces[2] + 64
    plan: Plan = [(spaces, batches)]
    assert both(hp, roots, plan) == TrapCode.UNREACHABLE.name
