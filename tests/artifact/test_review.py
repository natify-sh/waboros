# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportArgumentType=false, reportPrivateUsage=false
"""S0014 step 8 review: one refusal per finding (the deep supertype chain, the resume offsets, the
duplicate stack-map sites, the compiler string, allocation without the heap, the limits, a signature
word of another kind), and the reader is not quadratic where the review found it."""

import struct
import time
from typing import Any

import pytest

from tests.artifact.test_format import SHA, SMALL, build, explicit, put, secs, small, u32
from waboros.frontend import types as T
from waboros.frontend.binary import parse
from waboros.frontend.validate import validate
from waboros.targets import base
from waboros.targets.jit.common import artifact as A
from waboros.targets.jit.common import gc

_ = explicit


def chain(depth: int, per: int = 1) -> bytes:
    """A `.waboros.types` payload: `depth` + 1 empty structs, each a subtype of the one before."""
    n = depth + 1
    td: list[Any] = [(("struct", []), (i - 1,) if i else (), False, i, i + 1) for i in range(n)]
    body = b"".join(
        u32(1) + bytes([0, 1 if i else 0]) + b"\0\0" + (u32(i - 1) if i else b"") + b"\x5f" + u32(0)
        for i in range(n)
    )
    keys = b"".join(T.key({"tdefs": td, "tkeys": {}}, i) for i in range(n))
    return u32(n) + body + u32(n) + keys


def test_supertype_chain_is_at_most_63_deep() -> None:
    mod, _ = A._parse_types(A._R(chain(63), "t"))
    assert len(mod["tdefs"]) == 64
    with pytest.raises(A.ArtifactError, match="deeper than 63"):
        A._parse_types(A._R(chain(64), "t"))


def test_matching_along_a_63_chain_is_fast() -> None:
    mod, _ = A._parse_types(A._R(chain(63), "t"))
    env = A._Env(mod["tdefs"], 64)
    t = time.time()
    for _ in range(2000):
        assert env.fits((0, 63), (0, 0))
    assert time.time() - t < 5


def guard_blob(monkeypatch: pytest.MonkeyPatch) -> bytes:
    monkeypatch.setenv("WABOROS_JIT_BOUNDS", "guard")
    return small()


def test_resume_offsets_are_below_data_start(monkeypatch: pytest.MonkeyPatch) -> None:
    blob = guard_blob(monkeypatch)
    _, data, _ = secs(blob)["waboros.traps".join((".", ""))]
    assert struct.unpack_from("<I", blob, data)[0] > 0
    assert A.read(blob)
    with pytest.raises(A.ArtifactError, match="resume offsets"):
        A.read(put(blob, data + 8, u32(0x7FFFFFFF)))


def test_stackmap_sites_are_strictly_increasing() -> None:
    a, b = (8, 0, 4, 8, 0, 0, 0), (8, 0, 4, 8, 0, 0, 0)
    gc.decode(gc.encode([(8, 0, 4, 8, 0, 0, 0), (16, 0, 4, 8, 0, 0, 0)]))
    for sites in ([a, b], [(16, 0, 4, 8, 0, 0, 0), (8, 0, 4, 8, 0, 0, 0)]):
        with pytest.raises(ValueError, match="strictly increasing"):
            gc.decode(gc.encode(sites))


def test_compiler_string_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    m = parse(SMALL)
    validate(m)
    c = base.TARGETS["jit-x86_64"]().compile(m)
    good = A.compiler_string("jit-x86_64")
    ver = good.split(" ")[1]
    assert A.read(A.write(c, target="jit-x86_64", wasm=SMALL))
    for bad in (
        good + "9" * 5000,
        "waboros  jit-x86_64 0",
        f"waboros {ver} jit-x86_64 " + "9" * 10,
    ):
        monkeypatch.setattr(A, "compiler_string", lambda _t, bad=bad: bad)  # pyright: ignore
        blob = A.write(c, target="jit-x86_64", wasm=SMALL)
        with pytest.raises(A.ArtifactError, match="compiler string"):
            A.read(blob)


STRUCT = "(module (type $s (struct)) (global (ref $s) (struct.new $s)))"


def test_allocation_without_the_heap_is_refused() -> None:
    blob = build(STRUCT)
    _, data, _ = secs(blob)[".waboros.context"]
    assert A.read(blob).heap
    with pytest.raises(A.ArtifactError, match="without the heap"):
        A.read(put(blob, data, b"\0"))


@pytest.mark.parametrize(
    ("text", "section", "at", "lim", "why"),
    [
        ("(module (memory 1))", ".waboros.memories", 4, (0, 16, 0, 70000, 0), "limits"),
        ("(module (memory 1))", ".waboros.memories", 4, (1, 16, 0, 5, 3), "limits"),
        ("(module (memory 1))", ".waboros.memories", 4, (3, 16, 0, 1 << 49, 0), "limits"),
        ("(module (table 1 funcref))", ".waboros.tables", 9, (0, 0, 0, 1 << 33, 0), "limits"),
        ("(module (table 1 funcref))", ".waboros.tables", 9, (1, 0, 0, 3, 2), "limits"),
    ],
)  # fmt: skip
def test_limits_are_within_the_validators(text: str, section: str, at: int, lim: tuple[int, ...],
                                          why: str) -> None:  # fmt: skip
    blob = build(text)
    _, data, _ = secs(blob)[section]
    fl, lg, z, mn, mx = lim
    with pytest.raises(A.ArtifactError, match=why):
        A.read(put(blob, data + at, struct.pack("<BBHQQ", fl, lg, z, mn, mx)))


def test_limits_edge_values_are_accepted() -> None:
    blob = build("(module (memory 1))")
    _, data, _ = secs(blob)[".waboros.memories"]
    assert A.read(put(blob, data + 4, struct.pack("<BBHQQ", 1, 16, 0, 65536, 65536)))
    assert A.read(put(blob, data + 4, struct.pack("<BBHQQ", 3, 16, 0, 1 << 48, 1 << 48)))


def test_signature_word_of_a_struct_type_is_refused() -> None:
    blob = build("""(module (type $f (func)) (type $s (struct)) (table 1 funcref)
      (func (call_indirect (type $f) (i32.const 0))))""")
    v = A.read(blob)
    assert len(v.sigs) == 1 and v.module["tdefs"][v.sigs[0]][0][0] == "func"
    _, data, _ = secs(blob)[".waboros.context"]
    at = data + 4 + 4 + 4 * len(v.records) + 4
    assert struct.unpack_from("<I", blob, at)[0] == v.sigs[0]
    other = next(i for i, d in enumerate(v.module["tdefs"]) if d[0][0] == "struct")
    with pytest.raises(A.ArtifactError, match="another kind"):
        A.read(put(blob, at, u32(other)))


def test_membership_scans_are_not_quadratic() -> None:
    env = A._Env([], 0, nfuncs=1 << 20, records=list(range(1 << 20)))
    env.rset = frozenset(env.records or [])
    r = A._R(u32(*range(0, 20000)), "t")
    t = time.time()
    for _ in range(20000):
        env.func_ref(r)
    assert time.time() - t < 5
    assert SHA
