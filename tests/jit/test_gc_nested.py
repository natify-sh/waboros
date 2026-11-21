"""A host call may enter wasm again with a frame that starts right above its argument slots
(ARTIFACT.md "Host functions"), as step 5's collector will from ALLOC: a service that does so must
not change what the code computes (S0013 B1, LESSONS 110). On the machine's own JIT."""

from collections.abc import Sequence
from typing import Any

import pytest

from tests.jit import native, native_jit
from tests.jit.test_compiler import Inst, wat
from waboros.frontend.binary import parse  # pyright: ignore[reportUnknownVariableType]
from waboros.frontend.validate import validate  # pyright: ignore[reportUnknownVariableType]
from waboros.runtime import link
from waboros.runtime.store import StoreState
from waboros.targets import base
from waboros.targets.base import Raw
from waboros.targets.jit.common.instance import JitInstance

JIT = native_jit()
W = """(module
  (type $a (array (mut i32)))
  (type $b (array (mut i8)))
  (type $s (struct (field (mut i32)) (field (mut i32)) (field (mut i32)) (field (mut i32))))
  (func (export "clob") (param i64 i64 i64 i64 i64 i64) (result i64) (local.get 0))
  (func $check (param $x (ref $a)) (result i32) (local $y (ref null $s))
    (local.set $y (struct.new $s (i32.const -1) (i32.const -1) (i32.const -1) (i32.const -1)))
    (array.set $a (local.get $x) (i32.const 99) (i32.const 5))
    (i32.add (i32.add (array.len (local.get $x)) (ref.test (ref $s) (local.get $y)))
             (array.get $a (local.get $x) (i32.const 0))))
  (func (export "default") (result i32) (call $check (array.new_default $a (i32.const 100))))
  (func (export "service") (result i32) (call $check (array.new $a (i32.const 7) (i32.const 100))))
  (func (export "bytes") (result i32) (local $x (ref $b)) (local $y (ref null $s))
    (local.set $x (array.new $b (i32.const 3) (i32.const 100)))
    (local.set $y (struct.new $s (i32.const -1) (i32.const -1) (i32.const -1) (i32.const -1)))
    (array.set $b (local.get $x) (i32.const 99) (i32.const 5))
    (i32.add (i32.add (array.len (local.get $x)) (ref.test (ref $s) (local.get $y)))
             (array.get_u $b (local.get $x) (i32.const 0)))))"""


@pytest.mark.parametrize(
    ("name", "service", "want"),
    [("default", "_alloc", 101), ("bytes", "_alloc", 104), ("service", "_array_new", 108)],
)
def test_nested_entry_from_a_service(
    monkeypatch: pytest.MonkeyPatch, name: str, service: str, want: int
) -> None:
    """Each call of the service first enters wasm, whose arguments land at the slots above the
    service's own: a value the code kept there (B1: `size`) would come back as 16."""
    native(JIT)
    holder: list[Inst] = []
    real = getattr(JitInstance, service)

    def nested(self: JitInstance, a: Sequence[Raw]) -> Any:
        assert holder[0]("clob", *[16] * 6) == [16]
        holder.append(holder[0])  # one more nested entry
        return real(self, a)

    monkeypatch.setattr(JitInstance, service, nested)  # before instantiation binds it
    mod = parse(wat(W))
    validate(mod)
    t = base.TARGETS[JIT]()
    holder.append(Inst(mod, t.instantiate(t.compile(mod), link.resolve(mod, {}), StoreState())))
    assert holder[0](name) == [want]  # length 100 + ref.test 1 + element 0 (0, 3 or 7)
    assert len(holder) > 1  # the service ran, and entered wasm
