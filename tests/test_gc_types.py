# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportArgumentType=false
"""Wasm GC types in the front end (S0013, step 1a): reading, canonical keys, subtyping."""

from typing import Any

import pytest

from tests.jit.test_compiler import build, outcome
from tests.jit.test_tables import provider
from tests.watcache import wat2wasm
from waboros.frontend.binary import Malformed, parse
from waboros.frontend.types import key, match, sub_key
from waboros.frontend.validate import Invalid, validate
from waboros.runtime.store import StoreState
from waboros.targets import base as target_base
from waboros.targets.base import TrapCode

HEAD = b"\0asm\1\0\0\0"
FUNC0 = b"\x60\x00\x00"
STRUCT0 = b"\x5f\x00"


def ref(i: int, null: bool = True) -> bytes:  # (ref null? $i)
    return bytes([0x63 if null else 0x64, i])


def count(types: bytes) -> int:  # a reference type takes two bytes here
    return len(types) - sum(b in (0x63, 0x64) for b in types)


def func(params: bytes = b"", results: bytes = b"") -> bytes:
    return b"\x60" + bytes([count(params)]) + params + bytes([count(results)]) + results


def struct(*fields: tuple[bytes, int]) -> bytes:  # (storage type, mutable)
    return b"\x5f" + bytes([len(fields)]) + b"".join(t + bytes([m]) for t, m in fields)


def sub(sup: int | None, comp: bytes, final: bool = False) -> bytes:
    return (
        bytes([0x4F if final else 0x50])
        + (b"\x01" + bytes([sup]) if sup is not None else b"\0")
        + comp
    )


def rec(*types: bytes) -> bytes:
    return b"\x4e" + bytes([len(types)]) + b"".join(types)


def module(*entries: bytes) -> dict[str, Any]:
    body = bytes([len(entries)]) + b"".join(entries)
    size = (
        bytes([len(body) & 0x7F | 0x80, len(body) >> 7]) if len(body) > 127 else bytes([len(body)])
    )
    m = parse(HEAD + b"\x01" + size + body)
    validate(m)
    return m


def test_reader_shapes() -> None:
    m = module(
        rec(func(), sub(None, struct((b"\x78", 1), (b"\x7f", 0)))),
        sub(1, struct((b"\x78", 1), (b"\x7f", 0), (b"\x7e", 0))),
        b"\x5e\x77\x01",
    )
    assert m["types"] == [([], []), ([], []), ([], []), ([], [])]
    assert [d[0] for d in m["tdefs"]] == [
        ("func", [], []),
        ("struct", [(0x78, 1), (0x7F, 0)]),
        ("struct", [(0x78, 1), (0x7F, 0), (0x7E, 0)]),
        ("array", (0x77, 1)),
    ]
    assert [d[1:] for d in m["tdefs"]] == [
        ((), True, 0, 2),
        ((), False, 0, 2),
        ((1,), False, 2, 3),
        ((), True, 3, 4),
    ]
    assert m["gc"] and m["validated"] is False  # a struct type: no target runs it yet


def test_func_only_module_stays_validated() -> None:
    assert module(rec(func()), sub(None, func(b"\x7f")))["validated"] is True


def test_singleton_key_is_structural_across_modules() -> None:
    a, b = module(func(b"\x7f", b"\x7e"), func()), module(func(), func(b"\x7f", b"\x7e"))
    assert key(a, 0) == key(b, 1) and key(a, 1) == key(b, 0) and key(a, 0) != key(a, 1)
    hash(key(a, 0))


def test_recursive_keys() -> None:
    loop = rec(func(ref(1)), func(ref(0)))  # two types referring to each other
    a = module(loop)
    b = module(func(), rec(func(ref(2)), func(ref(1))))  # the same group at another position
    assert key(a, 0) == key(b, 1) and key(a, 1) == key(b, 2) and key(a, 0) != key(a, 1)
    # a group is one unit: the same two types in two groups are not equivalent to it
    c = module(rec(func(ref(0))), rec(func(ref(1))))
    assert key(c, 0) == key(c, 1)  # the same singleton group
    assert key(module(rec(func(ref(0)), func(ref(0)))), 0) != key(c, 0)


def test_declared_subtyping() -> None:
    parent = sub(None, struct((b"\x7f", 0)))
    child = sub(0, struct((b"\x7f", 0), (b"\x7e", 1)))
    m = module(parent, child, sub(None, func(ref(0, False)), final=True))
    assert match(m, (0, 1), (1, 0)) and not match(m, (0, 0), (0, 1))
    assert match(m, (0, 1), 0x6D) and match(m, (0, 1), 0x6B)  # eqref, structref
    assert not match(m, (0, 1), 0x6A) and match(m, 0x71, (1, 1))  # not arrayref; nullref
    assert sub_key(m, 1, key(m, 0)) and not sub_key(m, 0, key(m, 1))


def test_abstract_order() -> None:
    m = module(func())
    any_, eq, i31, none, nofunc = 0x6E, 0x6D, 0x6C, 0x71, 0x73
    assert match(m, none, i31) and match(m, i31, eq) and match(m, eq, any_) and match(m, none, any_)
    assert not match(m, any_, eq) and not match(m, i31, none) and not match(m, none, 0x70)
    assert match(m, nofunc, (1, 0)) and match(m, (0, 0), 0x70) and not match(m, 0x70, (1, 0))
    assert not match(m, 0x6F, any_) and match(m, 0x72, 0x6F)  # extern is another hierarchy
    assert not match(m, (1, 0), (0, 0)) and match(m, (0, 0), (1, 0))  # nullability


@pytest.mark.parametrize(
    "entries",
    [
        (sub(None, STRUCT0, final=True), sub(0, STRUCT0)),  # the supertype is final
        (sub(None, STRUCT0), sub(0, func())),  # another kind
        (sub(None, struct((b"\x7f", 1))), sub(0, struct((b"\x7e", 1)))),  # field type
        (sub(None, struct((b"\x7f", 0))), sub(0, struct((b"\x7f", 1)))),  # mutability
        (sub(None, struct((b"\x7f", 0), (b"\x7e", 0))), sub(0, struct((b"\x7f", 0)))),  # width
        (sub(None, struct((ref(0), 1))), sub(0, struct((ref(2), 1))), STRUCT0),  # mutable ref
        (sub(None, func(b"\x7f")), sub(0, func())),  # parameter count
        (rec(sub(0, STRUCT0)),),  # a supertype must be an earlier type
        (rec(func(ref(1))),),  # a reference to a later group
        (sub(None, STRUCT0), sub(None, STRUCT0), b"\x50\x02\x00\x01" + STRUCT0),  # two supertypes
    ],
)
def test_invalid_types(entries: tuple[bytes, ...]) -> None:
    with pytest.raises(Invalid):
        module(*entries)


def test_function_must_use_a_function_type() -> None:
    data = HEAD + b"\x01\x03\x01" + STRUCT0 + b"\x03\x02\x01\x00" + b"\x0a\x04\x01\x02\x00\x0b"
    with pytest.raises(Invalid):
        validate(parse(data))


def test_unknown_heap_type_code_is_malformed() -> None:
    def sec(i: int, body: bytes) -> bytes:
        return bytes([i, len(body)]) + body

    for body in (
        b"\x01\x70\x00\xd0\x7f\x0b",
        b"\x01\x63\x7f\x00\xd0\x70\x0b",
        b"\x01\x63\x58\x00\xd0\x58\x0b",
    ):
        with pytest.raises(Malformed):
            validate(parse(HEAD + sec(6, body)))


def test_keys_of_a_deep_chain_are_cheap() -> None:
    entries = [b"\x60\x00\x00"]
    for i in range(1, 30):  # each type takes the previous one twice: a tree of 2**30 leaves
        entries.append(func(ref(i - 1) + ref(i - 1)))
    mod = module(*entries)
    assert key(mod, 29) != key(mod, 28)
    assert key(module(*entries), 29) == key(mod, 29)


def test_keys_are_the_same_in_another_process() -> None:
    """Generated `pyc` code embeds the keys (`f.s`, the `call_indirect` check): they must not
    depend on the process (no counters, no `id()`)."""
    import subprocess
    import sys

    code = (
        "from tests.test_gc_types import func, module, ref; from waboros.frontend.types import key;"
        "m = module(b'\\x60\\x00\\x00', func(ref(0))); print(key(m, 1))"
    )
    other = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert other.stdout.strip() == key(module(b"\x60\x00\x00", func(ref(0))), 1)


_A = """(module
 (rec (type $a (func (result i32))) (type $b (func (result i32))))
 (type $nf (sub (func (result i32))))
 (table (export "t") 4 funcref)
 (func $f (type $a) i32.const 7)
 (func $g (type $nf) i32.const 8)
 (elem (i32.const 0) $f $g))"""
_CALL = """(module {types}
 (import "a" "t" (table 4 funcref))
 (func (export "go") (result i32) (call_indirect (type {ti}) (i32.const {idx}))))"""
_REC = "(rec (type $a (func (result i32))) (type $b (func (result i32))))"
_REC_SWAPPED = "(rec (type $b (func (result i32))) (type $a (func (result i32))))"


@pytest.mark.parametrize(
    ("types", "ti", "idx", "expected"),
    [
        ("(type $c (func (result i32)))", "$c", 0, TrapCode.BAD_SIGNATURE),
        (_REC, "$a", 0, [7]),
        (_REC, "$b", 0, TrapCode.BAD_SIGNATURE),
        (_REC_SWAPPED, "$b", 0, [7]),
        ("(type $c (func (result i32)))", "$c", 1, TrapCode.BAD_SIGNATURE),
        ("(type $c (sub (func (result i32))))", "$c", 1, [8]),
        (
            "(type $c (sub final (func (result i32))))",
            "$c",
            0,
            TrapCode.BAD_SIGNATURE,
        ),
    ],
)
@pytest.mark.parametrize("target", ("pyc", "jit-x86_64"))
def test_call_indirect_checks_rec_groups_and_finality(
    target: str, types: str, ti: str, idx: int, expected: Any
) -> None:
    if target_base.TARGETS[target]().probe() is not None:
        pytest.skip(target)
    store = StoreState()
    a = build(wat2wasm(_A), target=target, store=store)
    text = _CALL.format(types=types, ti=ti, idx=idx)
    b = build(wat2wasm(text), target=target, imports={"a": provider(a)}, store=store)
    assert outcome(b, "go") == expected
