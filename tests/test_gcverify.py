"""The test-only heap verifier (S0013 step 5.1): the digest ignores where objects sit and sees every
other change; the form check names the offset and the problem. Synthetic images use `gc.py`'s record
layout; one real image comes from a live JIT store."""

import ctypes
import struct
from typing import Any

import pytest

from tests.jit.test_gc_heap import heap, mod
from waboros.runtime.store import StoreState
from waboros.targets.jit.common import gc
from waboros.targets.jit.common.gcverify import HeapError, check, digest

SREC, AREC = 0x100, 0x140  # a struct: scalar at 8, refs at 12 and 16; an array of traced refs
SIZES = {"a": 24, "b": 24, "c": 32}  # `c` is an array of 4
HANDLES = {3}


class Image:
    """a -> (5, b, c), b -> (7, a, i31), c = [a, b, 0, host 3]; roots a, c, i31, host."""

    def __init__(self, base: int, order: str = "abc", **kw: Any) -> None:
        self.mem, self.at, cur = bytearray(0x1000), dict[str, int](), base
        self.put(SREC, gc.KIND_STRUCT | 1 << 8, 0, 24, 2, SREC, 12, 16)
        self.put(AREC, gc.KIND_ARRAY | 1 << 8, 0, 4, 1, AREC)
        for n in order:
            self.at[n], cur = cur, cur + SIZES[n]
        self.cursor, at = cur, self.at
        scalar, edge, i31 = kw.get("scalar", 5), kw.get("edge", "b"), kw.get("i31", 9 << 1 | 1)
        self.put(at["a"], SREC, 0, scalar, at[edge], at["c"])
        self.put(at["b"], SREC, 0, 7, at["a"], i31)
        self.put(at["c"], AREC, 0, 4, 0, at["a"], at["b"], 0, 3 << 2 | 2)
        self.roots = [at["a"], at["c"], 9 << 1 | 1, 3 << 2 | 2]
        if kw.get("swap"):
            self.roots[:2] = self.roots[1::-1]

    def put(self, at: int, *words: int) -> None:
        struct.pack_into(f"<{len(words)}I", self.mem, at, *words)

    def check(self, cursor: int | None = None, roots: list[int] | None = None) -> None:
        lo = min(self.at.values())
        check(self.mem, {SREC, AREC}, lo, cursor or self.cursor, roots or self.roots, HANDLES)

    def digest(self) -> str:
        return digest(self.mem, {SREC, AREC}, self.roots)


def test_relabelled_copy_has_the_same_digest() -> None:
    one, two = Image(0x400), Image(0x520, "cab")  # other offsets, other order
    one.check()
    two.check()
    assert one.at != two.at and one.digest() == two.digest()


@pytest.mark.parametrize("kw", [{"scalar": 6}, {"edge": "c"}, {"i31": 11 << 1 | 1}, {"swap": 1}])
def test_digest_sees_a_changed_field_edge_i31_or_root_order(kw: dict[str, Any]) -> None:
    assert Image(0x400, **kw).digest() != Image(0x400).digest()


def test_digest_sees_a_changed_array_length_and_host_value() -> None:
    one, two = Image(0x400), Image(0x400)
    two.put(two.at["c"] + 8, 3)  # a shorter array: the same bytes, another graph
    assert one.digest() != two.digest()
    two = Image(0x400)
    two.roots[3] = 2 << 2 | 2
    assert one.digest() != two.digest()


def test_valid_image_passes() -> None:
    Image(0x400).check()


@pytest.mark.parametrize(
    ("obj", "delta", "word", "msg"),
    [
        ("b", 0, 0x777, r"object 0x418: bad record 0x777"),
        ("b", 4, 1, r"object 0x418: collector bits 0x1"),
        ("a", 12, 0x800, r"object 0x400 field 0x40c: reference 0x800 dangles"),
        ("a", 12, 0x420, r"reference 0x420 points into the middle"),
        ("b", 16, 99 << 2 | 2, r"field 0x428: dead host handle 99"),
        ("c", 28, 0x414, r"object 0x430 field 0x44c: reference 0x414"),
    ],
)
def test_form_errors_are_named(obj: str, delta: int, word: int, msg: str) -> None:
    im = Image(0x400)
    im.put(im.at[obj] + delta, word)
    with pytest.raises(HeapError, match=msg):
        im.check()


def test_overrun_and_bad_roots() -> None:
    im = Image(0x400)
    with pytest.raises(HeapError, match=r"object 0x430: size 32 overruns the cursor 0x448"):
        im.check(cursor=im.cursor - 8)
    with pytest.raises(HeapError, match=r"root 1: dead host handle 8"):
        im.check(roots=[im.at["a"], 8 << 2 | 2])
    with pytest.raises(HeapError, match=r"root 0: reference 0x404 points into the middle"):
        im.check(roots=[im.at["a"] + 4])


def test_a_real_image_from_a_jit_store() -> None:
    m = mod("""(module (type $s (struct (field i32) (field (ref null $s))))
      (type $a (array (mut anyref))))""")
    h = heap(StoreState())
    n1 = h.new_struct(m, 0, [1, 0])
    n2 = h.new_struct(m, 0, [2, n1])
    arr = h.new_array(m, 1, 3, vals=[n2, 5 << 1 | 1, 0])
    low = h.mem.res.committed  # the regions committed apart (D69): the gap reads as zeros
    image = ctypes.string_at(h.base, low) + bytes(gc.OBJ - low)
    image += ctypes.string_at(h.base + gc.OBJ, h.cursor - gc.OBJ)
    roots = [arr, n1]
    check(image, h.offsets, gc.OBJ, h.cursor, roots, set())
    assert digest(image, h.offsets, roots) == digest(image, h.offsets, roots)
    assert digest(image, h.offsets, roots[::-1]) != digest(image, h.offsets, roots)
