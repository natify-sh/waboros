# ruff: noqa: E501
"""The JIT's GC heap and its host boundary (S0013 step 3b.1): type-info records keyed by canonical
type, the allocator, the context words of the hidden memory and the GC group, the walk check's
reference words, `externref` as `(h << 2) | 2` and i31 values from the host."""

import ctypes
import dataclasses
import mmap
import struct
from typing import Any

import pytest

from tests.jit import native, native_jit
from tests.watcache import wat2wasm
from waboros.frontend.binary import parse  # pyright: ignore[reportUnknownVariableType]
from waboros.frontend.types import ANY, EXTERN
from waboros.frontend.validate import validate  # pyright: ignore[reportUnknownVariableType]
from waboros.runtime import link
from waboros.runtime.store import StoreState
from waboros.targets import base
from waboros.targets.base import Trap, TrapCode
from waboros.targets.jit.common import gc, walk
from waboros.targets.jit.common.context import FIRST_VAR, GC_SERVICES, Layout
from waboros.targets.jit.common.heap import POISON, GcHeap
from waboros.targets.jit.common.instance import GcRef, Instance, _stacks  # pyright: ignore[reportPrivateUsage]

# externref and anyref as the boundary sees them (`reftypes`: hierarchy, nullable, heap type, ...)
XR, AR = (EXTERN, True, EXTERN, None, 0), (ANY, True, ANY, None, 0)
JITS = ["jit-x86_64", "jit-aarch64"]  # each where it runs (aarch64: natively or under QEMU)


def mod(text: str) -> Any:
    m = parse(wat2wasm(text))
    validate(m)
    return m


def heap(st: Any) -> GcHeap:
    """A heap of a store: a reservation through the JIT's memory code, so only where a JIT runs
    (Pyodide's ctypes cannot pass a 64-bit size to mmap: a fatal error, not an exception)."""
    native(native_jit())
    return GcHeap(st)


def u32s(h: GcHeap, off: int, n: int) -> list[int]:
    return list(struct.unpack_from(f"<{n}I", ctypes.string_at(h.base + off, 4 * n)))


RECA = """(module (rec (type $a (sub (struct (field i32)))) (type $b (sub $a (struct (field i32) (field i32))))))"""
OTHERPOS = """(module (rec (type $pad (sub (struct (field i32)))) (type $a (sub (struct (field i32))))
  (type $b (sub $a (struct (field i32) (field i32))))))"""


def test_records_are_canonical_in_a_store() -> None:
    h = heap(StoreState())
    a, same, other = mod(RECA), mod(RECA), mod(OTHERPOS)
    ra, rb = h.rtt(a, 0), h.rtt(a, 1)
    assert (h.rtt(same, 0), h.rtt(same, 1)) == (ra, rb)  # an equivalent rec group: the same records
    assert h.rtt(other, 1) not in (ra, rb) and h.rtt(other, 2) not in (ra, rb)
    assert gc.TYPES <= ra < gc.OBJ and ra % 8 == 0
    kind, depth, size, nrefs, d0, d1 = u32s(h, rb, 6)
    assert (kind, depth, size, nrefs, d0, d1) == (gc.KIND_STRUCT, 1, 16, 0, ra, rb)  # $b sub $a


def test_record_fields_and_layout() -> None:
    m = mod("""(module (type $s (sub final (struct (field i8) (field i64) (field anyref) (field funcref)
      (field (mut i16)) (field externref) (field v128))))
      (type $a (array (mut (ref null $s)))) (type $f (array funcref)) (type $e (struct)))""")
    offs, size = gc.struct_layout(m, m["tdefs"][0][0][1])
    assert offs == [8, 16, 24, 32, 40, 44, 48] and size == 64
    assert gc.struct_layout(m, [])[1] == 8  # the header: room for the forwarding word
    h = heap(StoreState())
    r = h.rtt(m, 0)
    assert u32s(h, r, 7) == [
        gc.KIND_STRUCT | 1 << 8,
        0,
        64,
        2,
        r,
        24,
        44,
    ]  # final, traced at 24, 44
    fin = 1 << 8  # a type without `sub` is final
    assert u32s(h, h.rtt(m, 1), 4) == [gc.KIND_ARRAY | fin, 0, 4, 1]  # struct refs: 4 bytes, traced
    assert u32s(h, h.rtt(m, 2), 4) == [gc.KIND_ARRAY | fin, 0, 8, 0]  # funcref: a record address
    assert u32s(h, h.rtt(m, 3), 3) == [gc.KIND_STRUCT | fin, 0, 8]


def test_display_of_depth_three() -> None:
    m = mod("""(module (type $t0 (sub (struct))) (type $t1 (sub $t0 (struct (field i32))))
      (type $t2 (sub $t1 (struct (field i32) (field i32)))) (type $t3 (sub final $t2 (struct (field i32) (field i32) (field i32)))))""")
    h = heap(StoreState())
    r3 = h.rtt(m, 3)  # registers its supertypes first
    rs = [h.rtt(m, i) for i in range(4)]
    assert u32s(h, r3, 8)[1] == 3 and u32s(h, r3, 8)[4:] == rs


@pytest.mark.parametrize("poison", [False, True])
def test_allocation_commits_and_grows(monkeypatch: pytest.MonkeyPatch, poison: bool) -> None:
    if poison:
        monkeypatch.setenv("WABOROS_JIT_GC_POISON", "1")
    h = heap(StoreState())
    assert u32s(h, gc.CURSOR, 2) == [gc.OBJ, gc.OBJ] and h.mem.pages == 1  # nothing committed yet
    a = h.alloc(24)
    assert a == gc.OBJ and u32s(h, gc.CURSOR, 2) == [gc.OBJ + 24, gc.OBJ + (1 << 20)]
    assert h.mem.pages << 16 == gc.OBJ + (1 << 20)  # the size explicit checks compare with
    assert ctypes.string_at(h.base + a, 8) == bytes([POISON if poison else 0]) * 8
    big = h.alloc(3 << 20)  # past the first chunk: the limit moves
    assert big == gc.OBJ + 24 and u32s(h, gc.LIMIT, 1)[0] >= big + (3 << 20)
    ctypes.c_uint32.from_address(h.base + gc.CURSOR).value = gc.A_END - 8
    with pytest.raises(MemoryError, match="GC heap out of memory"):
        h.alloc(16)


def test_object_words_for_the_walk_check() -> None:
    native(native_jit())  # the store's JIT stacks are reserved through the same memory code
    st = _stacks(StoreState())
    m = mod(RECA)
    st.heap = heap(None)
    o = st.heap.alloc(16)
    ctypes.c_uint32.from_address(st.heap.base + o).value = st.heap.rtt(m, 0)
    st.externs.append(object())
    for w in (0, 1, 0xFFFFFFFF, 1 << 2 | 2, o):
        assert walk.valid(st, w), w
    for w in (2 << 2 | 2, o + 8, o + 4, gc.OBJ - 8, 1 << 32 | 1, 1 << 33):
        assert not walk.valid(st, w), w
    for w in (gc.OBJ, 1, 0xFFFFFFFF):  # no heap: no object and no i31 (3a's check for such stores)
        assert not walk.valid(_stacks(StoreState()), w), w


def test_records_across_many_pages() -> None:
    """2 000 records of 184 bytes, about 360 KiB: the type area grows by whole 64 KiB pages, on
    every OS page size; then the first object commits everything below it (no hole under the size
    the contexts see: an explicit bounds check that passes reaches a committed page)."""
    fields = "(field anyref) " * 40
    m = mod(
        "(module "
        + " ".join(
            f"(type (struct (field i32) {fields} (field i64) {'(field i8) ' * k}))"
            for k in range(2000)
        )
        + ")"
    )
    h = heap(StoreState())
    offs = [h.rtt(m, i) for i in range(2000)]
    assert offs[-1] - offs[0] > 5 * 64 << 10 and h.mem.pages << 16 >= h.types
    for i in (0, 1, 999, 1999):
        assert u32s(h, offs[i], 6)[3:6] == [40, offs[i], 12]  # 40 traced fields, the first at 12
    h.alloc(8)
    end = h.mem.pages << 16
    assert end == gc.OBJ + (1 << 20)
    for at in range(
        0, end, mmap.PAGESIZE
    ):  # every page below the size is readable (a fault: crash)
        ctypes.c_uint8.from_address(h.base + at).value  # noqa: B018


def test_layout_words() -> None:
    plain = Layout(memories=2, tables=1, globals=3, imports=1, sigs=1, records=2, tags=1)
    g = dataclasses.replace(plain, heap=True, gcsvc=GC_SERVICES, rtts=2)
    assert g.memory(2) == FIRST_VAR + 4  # the heap after the declared memories
    assert g.table(0) == plain.table(0) + 2 and g.tag(0) == plain.tag(0) + 2
    assert g.service(0) == g.tag(1) and g.rtt(0) == g.service(GC_SERVICES)
    assert g.words == g.rtt(2) and plain.words == plain.tag(1) and repr(g) == repr(plain)


def target(name: str) -> Any:
    t = base.TARGETS[name]()
    if t.probe() is not None:
        pytest.skip(f"{name} cannot run here")
    return t


@pytest.mark.parametrize("name", JITS)
def test_an_instance_with_the_heap(name: str) -> None:
    t, store = target(name), StoreState()
    m = mod('(module (func (export "f") (result i32) (i32.const 7)))')
    c = t.compile(m)
    c = dataclasses.replace(c, layout=dataclasses.replace(c.layout, heap=True, gcsvc=GC_SERVICES))
    insts = [t.instantiate(c, [], store) for _ in range(2)]
    h = _stacks(store).heap
    for inst in insts:  # one heap per store, as memory 0 of a module without memories
        assert inst.ctx[FIRST_VAR : FIRST_VAR + 2] == [h.base, 1 << 16]
        assert all(inst.ctx[c.layout.service(j)] for j in range(GC_SERVICES))
        assert inst.call(0, []) == [7]
    h.alloc(8)
    assert all(inst.ctx[FIRST_VAR + 1] == gc.OBJ + (1 << 20) for inst in insts)  # size published


NONFINAL = """(module (type $f (sub (func (result i32)))) (table 1 funcref)
  (func (export "go") (result i32) (call_indirect (type $f) (i32.const 0))))"""


def test_a_non_final_call_indirect_type_needs_gc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WABOROS_JIT_GC", raising=False)  # the 3b.2 switch lets x86-64 compile GC
    m = mod(NONFINAL)
    assert gc.uses_gc(m) and not gc.uses_gc(mod("(module (type $f (sub (func))))"))
    for name in ("jit-x86_64", "jit-aarch64"):  # neither claims gc yet (decision K)
        assert base.TARGETS[name]().supports(m) == "GC types"


@pytest.mark.parametrize("name", JITS)
def test_host_values_cross_as_tagged_words(name: str) -> None:
    t, store = target(name), StoreState()
    m = mod("""(module (func (export "id") (param externref) (result externref) (local.get 0)))""")
    inst = t.instantiate(t.compile(m), link.resolve(m, {}), store)
    o, st = object(), _stacks(store)
    assert inst.call(0, [o]) == [o] and inst.call(0, [None]) == [None]
    w = st.to_raw(XR, o)
    assert w & 3 == 2 and st.externs[(w >> 2) - 1] is o and st.to_raw(XR, o) == w
    for v in (0, -1, (1 << 30) - 1, -(1 << 30)):  # an int is an i31 for an anyref
        r = st.to_raw(AR, v)
        assert r & 1 and r < 1 << 32 and st.from_raw(ANY, r) == v
    for v in (1 << 30, -(1 << 30) - 1):
        with pytest.raises(ValueError, match="i31"):
            st.to_raw(AR, v)
    assert st.from_raw(ANY, st.to_raw(AR, o)) is o  # a host value internalized
    r = st.from_raw(EXTERN, 7)  # an i31 seen as an externref (extern.convert_any)
    assert isinstance(r, GcRef) and r.kind == "i31" and st.from_raw(EXTERN, 7) is r
    assert st.to_raw(XR, r) == 7 and st.to_raw(AR, r) == 7
    with pytest.raises(TypeError):
        _stacks(StoreState()).to_raw(XR, r)  # a handle of another store
    with pytest.raises(RuntimeError, match="not a reference"):
        st.from_raw(ANY, 64)  # an untagged word with no heap (or no object there)


# ---- compiled GC code (S0013 step 3b.2: jit-x86_64 behind WABOROS_JIT_GC=1 until it claims gc)
def gc_jit(monkeypatch: pytest.MonkeyPatch, bounds: str = "guard") -> Any:
    native("jit-x86_64")
    monkeypatch.setenv("WABOROS_JIT_GC", "1")
    monkeypatch.setenv("WABOROS_JIT_BOUNDS", bounds)
    return base.TARGETS["jit-x86_64"]()


def test_gc_results_cross_to_the_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """An anyref result is a `GcRef` or an int for the host, never a raw word (the JIT's `isref`
    once knew only the pre-GC type codes), and comes back as the same object."""
    t, store = gc_jit(monkeypatch), StoreState()
    m = mod("""(module (type $t (struct (field i32)))
      (func (export "s") (result anyref) (struct.new $t (i32.const 4)))
      (func (export "i") (result i31ref) (ref.i31 (i32.const -3)))
      (func (export "is") (param anyref) (result i32) (ref.test (ref $t) (local.get 0))))""")
    inst = t.instantiate(t.compile(m), [], store)
    s = inst.call(0, [])[0]
    assert isinstance(s, GcRef) and s.kind == "struct" and inst.call(0, [])[0] is not s
    assert inst.call(1, []) == [-3] and inst.call(2, [s]) == [1] and inst.call(2, [-3]) == [0]


@pytest.mark.parametrize("bounds", ["guard", "explicit"])
def test_heap_accesses_below_the_size_work_and_past_it_trap(
    monkeypatch: pytest.MonkeyPatch, bounds: str
) -> None:
    """Compiled code reads and writes a word on every OS page below the heap's published size; a
    corrupt reference past it, or far past it, traps (MEMORY_OUT_OF_BOUNDS) instead of crashing, in
    both bounds modes (S0013 3b.1 review: no uncommitted page below the size)."""
    t, store = gc_jit(monkeypatch, bounds), StoreState()
    m = mod("""(module (type $t (struct (field (mut i32))))
      (func (export "get") (param (ref null $t)) (result i32) (struct.get $t 0 (local.get 0)))
      (func (export "set") (param (ref null $t)) (struct.set $t 0 (local.get 0) (i32.const 7)))
      (func (export "new") (result anyref) (struct.new $t (i32.const 1))))""")
    inst = t.instantiate(t.compile(m), [], store)
    inst.call(2, [])  # the first object: the type area and the first chunk committed
    raw = Instance.call  # raw words, past the host boundary's checks
    end = _stacks(store).heap.mem.pages << 16
    for w in range(gc.TYPES, end, mmap.PAGESIZE):  # the field at w + 8 on every page
        raw(inst, 1, [w])
        assert raw(inst, 0, [w]) == [7]
    for w in (end - 8, end, 0xFFFFFFF8):
        for f in (0, 1):
            with pytest.raises(Trap) as e:
                raw(inst, f, [w])
            assert e.value.code is TrapCode.MEMORY_OUT_OF_BOUNDS, (hex(w), f)
    with pytest.raises(Trap) as e:
        raw(inst, 0, [0])
    assert e.value.code is TrapCode.NULL_REFERENCE
