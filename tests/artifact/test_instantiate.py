# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportArgumentType=false
"""S0014 step 3: an instance made from the read form of an artifact (no ValidModule behind it)
behaves as one made from the compiled module; `subtype` comes from the compiler; constant
expressions of any depth evaluate without recursion. `WABOROS_JIT_ARTIFACT=1` runs the whole
spec suite this way (docs/TESTING.md)."""

from typing import Any

import pytest

from tests.jit import native, native_jit
from tests.watcache import wat2wasm as wat
from waboros.frontend.binary import parse
from waboros.frontend.validate import validate
from waboros.runtime import link
from waboros.runtime.store import StoreState
from waboros.targets import base
from waboros.targets.jit.common import artifact

JIT = native_jit()
MOD = """(module
  (type $s (struct (field i32) (field (ref null $s))))
  (type $t (sub (func (result i32))))
  (global $g i32 (i32.add (i32.const 40) (i32.const 2)))
  (global $h i64 (i64.mul (i64.const 3) (i64.const 5)))
  (global $o (ref null $s) (struct.new $s (i32.const 7) (struct.new_default $s)))
  (global $c (mut i32) (i32.const 0))
  (memory 1) (table 3 funcref)
  (func $one (type $t) (i32.const 1)) (func $two (type $t) (i32.const 2))
  (func $init (global.set $c (i32.load (i32.const 8))))
  (elem (i32.const 1) $one $two)
  (data (i32.const 8) "\\2a")
  (start $init)
  (func (export "go") (param i32) (result i32)
    (i32.add (call_indirect (type $t) (local.get 0))
      (i32.add (global.get $c) (struct.get $s 0 (global.get $o)))))
  (func (export "h") (result i64) (global.get $h))
  (func (export "g") (result i32) (global.get $g)))"""


def _compile(wasm: bytes) -> tuple[Any, Any, Any]:
    native(JIT)
    m = parse(wasm)
    validate(m)
    t = base.TARGETS[JIT]()
    return t, m, t.compile(m)


def _view(c: Any) -> artifact.ModuleView:
    return artifact.read(artifact.write(c, target=JIT, module_sha256=bytes(32)))


def test_inventory_only() -> None:
    _, m, c = _compile(wat(MOD))
    v = _view(c)
    assert set(c.module) == set(v.module) == set(artifact.INVENTORY)
    assert "insns" in m and "codes" in m  # the bodies stay with the front end's module
    assert c.subtype and v.subtype  # a call_indirect of a non-final type


def test_subtype_off() -> None:
    _, _, c = _compile(wat('(module (func (export "f") (result i32) (i32.const 1)))'))
    assert not c.subtype and not _view(c).subtype


@pytest.mark.parametrize("via", ["direct", "file"])
def test_instantiate_from_view(via: str, monkeypatch: pytest.MonkeyPatch) -> None:
    t, m, c = _compile(wat(MOD))
    if via == "file":
        monkeypatch.setenv("WABOROS_JIT_ARTIFACT", "1")
    inst = t.instantiate(c, link.resolve(m, {}), StoreState())
    if via == "file":
        assert isinstance(inst.module, dict) and inst.module is not c.module
    assert [inst.call(0 + 3, [i])[0] for i in (1, 2)] == [1 + 42 + 7, 2 + 42 + 7]
    assert inst.call(4, []) == [15] and inst.call(5, []) == [42]
    with pytest.raises(base.Trap):
        inst.call(3, [0])  # a null element


@pytest.mark.parametrize("via", ["direct", "file"])
def test_deep_constant(via: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A 300 000-deep `i32.add` chain put into the compiled module's global: the writer, the
    reader and the instance never recurse (the front end's part: `tests/test_deep_const.py`)."""
    n = 300_000
    t, m, c = _compile(wat('(module (global (export "g") i32 (i32.const 0)))'))
    e: Any = (("i32", 1), *(("i32", 1), ("add", "i32")) * n)
    c.module["globals"][0] = (0x7F, 0, e)
    if via == "file":
        monkeypatch.setenv("WABOROS_JIT_ARTIFACT", "1")
    inst = t.instantiate(c, link.resolve(m, {}), StoreState())
    assert inst.global_get(0) == n + 1
