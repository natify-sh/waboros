# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportArgumentType=false
"""The canonical type key is the byte encoding of S0014 ("Canonical type key"): the bytes by
hand, and the same equivalence as the SHA-256 of Python's `repr` that it replaced (the oracle
below)."""

import hashlib
import struct
from typing import Any

import pytest

from tests.spec import fetch
from tests.test_gc_types import FUNC0, STRUCT0, func, module, rec, ref, sub
from tests.test_gc_types import struct as struct_
from waboros.frontend.binary import parse
from waboros.frontend.types import key
from waboros.frontend.validate import validate


def sha(body: bytes, k: int = 0) -> bytes:
    return hashlib.sha256(body + struct.pack("<I", k)).digest()


def old_key(mod: dict[str, Any], i: int, memo: dict[int, str]) -> str:
    """The key of before S0014 step 1: SHA-256 hex of `repr` of the group with positions."""
    if i in memo:
        return memo[i]
    td = mod["tdefs"]
    gs, ge = td[i][3:]

    def r(t: int) -> Any:
        return ("rec", t - gs) if t >= gs else old_key(mod, t, memo)

    def vk(t: Any) -> Any:
        return (t[0], r(t[1])) if t.__class__ is tuple and t[1] >= 0 else t

    def fk(f: Any) -> Any:
        return vk(f[0]), f[1]

    def ck(c: Any) -> Any:
        if c[0] == "func":
            return ("func", tuple(map(vk, c[1])), tuple(map(vk, c[2])))
        return ("struct", tuple(map(fk, c[1]))) if c[0] == "struct" else ("array", fk(c[1]))

    g = repr(tuple((ck(c), tuple(map(r, sp)), fin) for c, sp, fin, _, _ in td[gs:ge]))
    h = hashlib.sha256(g.encode())
    for t in range(gs, ge):
        m = h.copy()
        m.update(b",%d" % (t - gs))
        memo[t] = m.hexdigest()
    return memo[i]


def same_equivalence(pairs: set[tuple[str, bytes]]) -> bool:
    return len(pairs) == len({o for o, _ in pairs}) == len({n for _, n in pairs})


def test_bytes_of_a_final_function_type() -> None:
    cg = struct.pack("<IBB", 1, 1, 0) + b"\x60" + struct.pack("<II", 0, 0)
    assert key(module(FUNC0), 0) == sha(cg)


def test_bytes_of_a_group_with_references_and_a_supertype() -> None:
    f = struct.pack
    # type 0 is out of the group; the group is types 1 and 2: $2 is a subtype of $1
    f1 = ((ref(2), 1), (b"\x78", 0))
    m = module(func(), rec(sub(None, struct_(*f1)), sub(1, struct_(*f1, (ref(1, False), 0)))))
    fields = b"\x63" + f("<BI", 1, 1) + b"\x01" + b"\x78\x00"
    g0 = bytes((0, 0)) + b"\x5f" + f("<I", 2) + fields
    g1 = bytes((0, 1)) + f("<BI", 1, 0) + b"\x5f" + f("<I", 3) + fields
    g1 += b"\x64" + f("<BI", 1, 0) + b"\x00"
    cg = f("<I", 2) + g0 + g1
    assert key(m, 1) == sha(cg, 0) and key(m, 2) == sha(cg, 1)
    out = module(func(), func(ref(0)))  # `ref(0)`: a type outside the group, by its key
    cg = f("<IBB", 1, 1, 0) + b"\x60" + f("<I", 1) + b"\x63\x02" + key(out, 0) + f("<I", 0)
    assert key(out, 1) == sha(cg, 0)


@pytest.mark.parametrize(("byte", "code"), [(0x69, -23), (0x74, -12), (0x70, -16), (0x6E, -18)])
def test_abstract_heap_codes_and_shorthand(byte: int, code: int) -> None:
    long = module(func(bytes((0x63, byte))))
    short = module(func(bytes((byte,))))
    cg = struct.pack("<IBB", 1, 1, 0) + b"\x60" + struct.pack("<I", 1)
    cg += b"\x63" + struct.pack("<Bi", 0, code) + struct.pack("<I", 0)
    assert key(long, 0) == key(short, 0) == sha(cg)
    assert key(module(func(bytes((0x64, byte)))), 0) != key(short, 0)


def test_final_nonfinal_and_chains_differ() -> None:
    m = module(sub(None, STRUCT0, True), sub(None, STRUCT0), sub(1, STRUCT0, True), sub(1, STRUCT0))
    ks = [key(m, i) for i in range(4)]
    assert len(set(ks)) == 4 and all(len(k) == 32 for k in ks)


def test_equal_across_modules_and_cached() -> None:
    a = module(func(), rec(sub(None, struct_((ref(1), 0)))))
    b = module(rec(sub(None, struct_((ref(0), 0)))), func())
    assert key(a, 1) == key(b, 0) and key(a, 0) == key(b, 1) and key(a, 1) is key(a, 1)


def test_new_keys_equal_iff_old_keys_equal_in_the_spec_suite() -> None:
    files = sorted(fetch.CACHE_DIR.rglob("*.wasm")) if fetch.CACHE_DIR.is_dir() else []
    if not files:
        pytest.skip("no spec cache: run tests/spec/fetch.py")
    pairs: set[tuple[str, bytes]] = set()
    types = 0
    for f in files:
        try:
            m = parse(f.read_bytes())
            validate(m)
        except Exception:
            continue
        memo: dict[int, str] = {}
        for i in range(len(m["tdefs"])):
            pairs.add((old_key(m, i, memo), key(m, i)))
            types += 1
    assert same_equivalence(pairs)
    assert types > 1000 and len(pairs) > 100
