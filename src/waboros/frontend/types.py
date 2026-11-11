# pyright: basic, reportArgumentType=false, reportOptionalMemberAccess=false, reportAttributeAccessIssue=false
"""Type definitions of a module: canonical keys and subtyping (docs/specs/S0013, "Types in the front
end"). Reads `mod["tdefs"]` and `mod["types"]` of `frontend.binary.parse`; raises nothing: the
validator (`validate.py`) checks the type section before any key is asked for.

Ideas from the WebAssembly GC spec (iso-recursive types: a rec group is one unit, a reference
inside it is relative) and from wasmparser's canonicalisation (types.rs): the key of a type is its
whole group with in-group references written as positions.
"""

from hashlib import sha256
from typing import Any

from waboros.frontend.binary import NULLABLE

FUNC, EXTERN, EXN, NOEXN = -16, -17, -23, -12
ANY, EQ, I31, STRUCT, ARRAY, NONE = -18, -19, -20, -21, -22, -15
NOFUNC, NOEXTERN = -13, -14

# heap type -> the abstract heap types above it (itself included); the bottoms sit first
_ABOVE = {
    NONE: {NONE, I31, STRUCT, ARRAY, EQ, ANY},
    I31: {I31, EQ, ANY},
    STRUCT: {STRUCT, EQ, ANY},
    ARRAY: {ARRAY, EQ, ANY},
    EQ: {EQ, ANY},
    ANY: {ANY},
    NOFUNC: {NOFUNC, FUNC},
    FUNC: {FUNC},
    NOEXTERN: {NOEXTERN, EXTERN},
    EXTERN: {EXTERN},
    NOEXN: {NOEXN, EXN},
    EXN: {EXN},
}
_BOTTOM = {"func": NOFUNC, "struct": NONE, "array": NONE}
_SUPER = {"func": FUNC, "struct": STRUCT, "array": ARRAY}
# the one-byte nullable reference types as (nullable, heap type)
_NN = {b: (1, h) for h, b in NULLABLE.items()}


def nn(t: Any) -> Any:
    """(nullable, heap type) of a reference type, `t` itself for any other value type."""
    return _NN.get(t, t) if t.__class__ is not tuple else t


def isref(t: Any) -> bool:
    return t.__class__ is tuple or t in _NN


def key(mod: dict[str, Any], i: int) -> str:
    """Canonical key of type `i`: a SHA-256 hex digest, equal for equivalent types of any module,
    the same in every process and on every machine (no shared state, so no race on 3.15t).
    A rec group (its composite types, supertypes and final flags; a reference inside it is
    `("rec", position)`, one outside it the digest of that type) is digested together with the
    position; `repr` of tuples of ints, strings and bools is deterministic. Groups are keyed in
    index order, so a key is never a nested tree (hashing and comparing stay constant-time, no
    recursion)."""
    cache = mod.setdefault("tkeys", {})
    if i in cache:
        return cache[i]
    tdefs = mod["tdefs"]
    for j in range(i + 1):
        if j in cache:
            continue
        gs, ge = tdefs[j][3:]

        def ref(t, gs=gs):
            return ("rec", t - gs) if t >= gs else cache[t]

        def vk(t, ref=ref):
            return (t[0], ref(t[1])) if t.__class__ is tuple and t[1] >= 0 else t

        def fk(f, vk=vk):
            return vk(f[0]), f[1]

        def ck(c, vk=vk, fk=fk):
            if c[0] == "func":
                return ("func", tuple(map(vk, c[1])), tuple(map(vk, c[2])))
            return ("struct", tuple(map(fk, c[1]))) if c[0] == "struct" else ("array", fk(c[1]))

        group = tuple((ck(c), tuple(map(ref, sp)), fin) for c, sp, fin, _, _ in tdefs[gs:ge])
        for t in range(gs, ge):
            cache[t] = sha256(repr((group, t - gs)).encode()).hexdigest()
    return cache[i]


def valtype_key(mod: dict[str, Any], t: Any) -> Any:
    """Structural identity of a value type (`key` of its type index in a reference type)."""
    return (t[0], key(mod, t[1])) if t.__class__ is tuple and t[1] >= 0 else t


def sub_key(mod: dict, a: int, k) -> bool:
    """Type `a` of `mod` is equivalent to, or declared a subtype (transitively) of, the type whose
    key is `k`."""
    while True:
        if key(mod, a) == k:
            return True
        sup = mod["tdefs"][a][1]
        if not sup:
            return False
        a = sup[0]


def _kind(mod: dict, i: int) -> str:
    return mod["tdefs"][i][0][0]


def _heap(ma: dict, a: int, mb: dict, b: int) -> bool:
    """Heap type `a` of `ma` is a subtype of heap type `b` of `mb` (the same module, or linking)."""
    if b >= 0:
        if a >= 0:
            return sub_key(ma, a, key(mb, b))
        return a == _BOTTOM[_kind(mb, b)]
    if a >= 0:
        return b in _ABOVE[_SUPER[_kind(ma, a)]]
    return b in _ABOVE[a]


def match(ma: dict, a, b, mb: dict | None = None) -> bool:
    """Value type `a` (of `ma`) is a subtype of `b` (of `mb`, default `ma`): numeric types are
    equal, a reference type matches by nullability and heap type."""
    if a == b and mb is None:
        return True
    ra, rb = nn(a), nn(b)
    if not (ra.__class__ is tuple and rb.__class__ is tuple):
        return a == b
    if ra[0] and not rb[0]:
        return False
    return _heap(ma, ra[1], mb or ma, rb[1])


def match_field(mod: dict, a: tuple, b: tuple) -> bool:
    """Field `a` is a subtype of field `b`: an immutable field covariantly, a mutable one by
    equivalence."""
    if a[1] != b[1]:
        return False
    if a[1]:
        return match(mod, a[0], b[0]) and match(mod, b[0], a[0])
    return match(mod, a[0], b[0])


def match_comp(mod: dict, a: tuple, b: tuple) -> bool:
    """Composite type `a` extends composite type `b`: a struct with more fields, an array of a
    matching field, a function contravariant in its parameters, covariant in its results."""
    if a[0] != b[0]:
        return False
    if a[0] == "func":
        return (
            len(a[1]) == len(b[1])
            and len(a[2]) == len(b[2])
            and all(match(mod, y, x) for x, y in zip(a[1], b[1], strict=True))
            and all(match(mod, x, y) for x, y in zip(a[2], b[2], strict=True))
        )
    if a[0] == "array":
        return match_field(mod, a[1], b[1])
    return len(a[1]) >= len(b[1]) and all(
        match_field(mod, x, y) for x, y in zip(a[1], b[1], strict=False)
    )
