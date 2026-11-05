# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false
"""Exceptions across targets (S0012 step 5): what a host sees of an escaping exception is the same
on every target, and a tag cannot be shared between targets."""

from typing import Any

import pytest

from tests.watcache import wat2wasm
from waboros.frontend.binary import parse
from waboros.frontend.validate import validate
from waboros.runtime import link
from waboros.runtime.store import StoreState
from waboros.targets import base
from waboros.targets.base import Trap, TrapCode

TARGETS = [
    pytest.param(n, marks=pytest.mark.skipif(base.TARGETS[n]().probe() is not None, reason=n))
    for n in ("pyc", "jit-x86_64")
]


def make(target: str, text: str, providers: Any = None, store: StoreState | None = None) -> Any:
    """A `link.Provider` of the module on `target`."""
    mod = parse(wat2wasm(text))
    validate(mod)
    t = base.TARGETS[target]()
    inst = t.instantiate(t.compile(mod), link.resolve(mod, providers or {}), store or StoreState())
    return link.Provider(mod, inst)


def call(p: Any, name: str, *args: Any) -> Any:
    idx = next(i for n, k, i in p.module["exports"] if n == name and k == 0)
    return p.instance.call(idx, list(args))


PAYLOAD = """(module
  (tag $t (param i32 f32 f64 i64 v128))
  (tag $u (param i32))
  (func (export "esc")
    (i32.const -1) (f32.const 1.5) (f64.const -2.5) (i64.const -3)
    (v128.const i64x2 0x1111 0x2222) (throw $t))
  (func (export "mk") (result exnref)
    (block $h (result exnref) (try_table (catch_all_ref $h) (throw $u (i32.const 9))) unreachable))
  (func (export "rethrow") (param exnref) (throw_ref (local.get 0))))"""


@pytest.mark.parametrize("target", TARGETS)
def test_escaping_payload_is_raw_on_every_target(target: str) -> None:
    p = make(target, PAYLOAD)
    with pytest.raises(Trap) as e:
        call(p, "esc")
    assert e.value.code is TrapCode.UNCAUGHT_EXCEPTION
    assert e.value.exn[1] == (
        0xFFFFFFFF,
        0x3FC00000,
        0xC004000000000000,
        2**64 - 3,
        0x1111 | 0x2222 << 64,
    )


@pytest.mark.parametrize("target", TARGETS)
def test_a_non_exnref_argument_is_a_type_error(target: str) -> None:
    p = make(target, PAYLOAD)
    e = call(p, "mk")[0]
    with pytest.raises(Trap) as t:
        call(p, "rethrow", e)
    assert t.value.exn[1] == (9,)
    with pytest.raises(TypeError):
        call(p, "rethrow", 5)


EXPORTER = '(module (tag $t (export "t") (param i32)))'
IMPORTER = '(module (import "a" "t" (tag $t (param i32))) (export "t2" (tag $t)))'


@pytest.mark.parametrize("target", TARGETS)
def test_export_of_a_tag_is_a_tag_extern(target: str) -> None:
    ext = make(target, EXPORTER).instance.export("t")
    assert ext.kind == "tag"


@pytest.mark.parametrize(("first", "second"), [("pyc", "jit-x86_64"), ("jit-x86_64", "pyc")])
def test_a_tag_is_not_shared_between_targets(first: str, second: str) -> None:
    for t in (first, second):
        if base.TARGETS[t]().probe() is not None:
            pytest.skip(t)
    store = StoreState()
    a = make(first, EXPORTER, None, store)
    with pytest.raises(link.LinkError, match="incompatible import type"):
        make(second, IMPORTER, {"a": a}, store)


def legacy(body: bytes) -> bytes:
    """A module with one function whose body is `body` (the legacy exception opcodes)."""
    code = bytes([0]) + body + bytes([0x0B])

    def section(i: int, b: bytes) -> bytes:
        return bytes([i, len(b)]) + b

    return (
        b"\0asm\1\0\0\0"
        + section(1, b"\1\x60\0\0")
        + section(3, b"\1\0")
        + section(10, bytes([1, len(code)]) + code)
    )


@pytest.mark.parametrize("body", [b"\x06\x40\x0b", b"\x09\x00", b"\x06\x40\x18\x00"])
def test_legacy_opcodes_are_a_load_error_without_a_kind(body: bytes) -> None:
    from tests.spec.engine import LoadError
    from tests.spec.target_engine import TargetEngine

    with pytest.raises(LoadError) as e:
        TargetEngine("pyc").load(legacy(body))
    assert e.value.kind is None


def test_wasmtime_start_function_throw_is_uncaught_exception() -> None:
    from tests.spec.engine import Trap as SpecTrap
    from tests.spec.engine import TrapCode as SpecCode
    from tests.spec.wasmtime_engine import WasmtimeEngine

    eng = WasmtimeEngine()
    m = eng.load(wat2wasm("(module (tag $t) (func $s (throw $t)) (start $s))"))
    with pytest.raises(SpecTrap) as e:
        eng.instantiate(m, {})
    assert e.value.code is SpecCode.UNCAUGHT_EXCEPTION
