# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportArgumentType=false
"""S0014 step 2: `read(write(c))` has every field the instance reads (the inventory), and
`write(read(b)) == b`, on every module of the pinned spec suite that a JIT compiles (both targets,
both bounds modes), on CoreMark and on hand-written modules; thin and fat."""

import hashlib
from pathlib import Path
from typing import Any

import pytest

from tests.spec import fetch
from tests.watcache import wat2wasm as wat
from waboros.frontend.binary import parse
from waboros.frontend.types import key
from waboros.frontend.validate import validate
from waboros.targets import base
from waboros.targets.jit.common import artifact

ROOT = Path(__file__).resolve().parents[2]
TARGETS = ("jit-x86_64", "jit-aarch64")
MODES = ("explicit", "guard")
INVENTORY = ("tdefs types imports ttypes taddr maddr mems funcs tables tinit globals tags exports "
             "start elem_info elem_mode datas data_mem").split()  # fmt: skip


def norm(x: Any) -> Any:
    """`ref.null` has no heap type in the file (S0014 "Constant expressions"): ("null", None)."""
    if isinstance(x, tuple | list):
        if x[:1] == ("null",):
            return ("null", None)
        return [norm(i) for i in x]
    return x


def compile_for(wasm: bytes, target: str, mode: str, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("WABOROS_JIT_BOUNDS", mode)
    m = parse(wasm)
    validate(m)
    try:
        return base.TARGETS[target]().compile(m)
    except base.Unsupported:
        return None


def roundtrip(c: Any, wasm: bytes, target: str) -> bytes:
    """Write thin and fat, read both, compare with `c`; returns the fat file."""
    thin = artifact.write(c, target=target, module_sha256=hashlib.sha256(wasm).digest())
    fat = artifact.write(c, target=target, wasm=wasm)
    assert artifact.write(c, target=target, wasm=wasm) == fat  # deterministic
    for blob, w in ((thin, None), (fat, wasm)):
        v = artifact.read(blob)
        mod = c.module
        for k in INVENTORY:
            assert norm(v.module[k]) == norm(mod[k]), k
        assert v.module["tkeys"] == {i: key(mod, i) for i in range(len(mod["tdefs"]))}
        assert (v.code, v.data_start, v.funcs, v.layout) == (
            c.code,
            c.data_start,
            c.funcs,
            c.layout,
        )
        assert (v.records, v.sigs, v.thunks) == (c.records, c.sigs, c.thunks)
        assert (v.rtts, v.thrown, v.flags, v.faults) == (c.rtts, c.thrown, c.flags, c.faults)
        assert v.stackmaps == c.stackmaps and v.wasm == w
        assert v.heap == c.layout.heap and v.subtype == c.subtype
        assert v.target == target and v.module_sha256 == hashlib.sha256(wasm).digest()
        again = artifact.write(v, target=v.target, module_sha256=v.module_sha256, wasm=v.wasm)
        assert again == blob
    return fat


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("target", TARGETS)
def test_spec_suite(target: str, mode: str, monkeypatch: pytest.MonkeyPatch) -> None:
    files = sorted((fetch.CACHE_DIR / "json").rglob("*.wasm")) if fetch.CACHE_DIR.is_dir() else []
    if not files:
        pytest.skip("no spec cache: run tests/spec/fetch.py")
    written = 0
    for f in files + [fetch.CACHE_DIR / "spectest.wasm"]:
        data = f.read_bytes()
        try:
            c = compile_for(data, target, mode, monkeypatch)
        except Exception:  # malformed or invalid: the suite has such modules
            continue
        if c is not None:
            roundtrip(c, data, target)
            written += 1
    print(f"{target} {mode}: {written} modules written and read")
    assert written > 1000


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("target", TARGETS)
def test_coremark(target: str, mode: str, monkeypatch: pytest.MonkeyPatch) -> None:
    data = (ROOT / "poc/in/core.wasm").read_bytes()
    c = compile_for(data, target, mode, monkeypatch)
    fat = roundtrip(c, data, target)
    thin = artifact.write(c, target=target, module_sha256=hashlib.sha256(data).digest())
    print(f"CoreMark {target} {mode}: thin {len(thin)} bytes, fat {len(fat)} bytes")


HAND = {
    "imports": """
      (type $t (func (param i32)))
      (import "m" "f" (func $f (param i32) (result i64)))
      (import "m" "t" (table 1 10 funcref)) (import "m" "mem" (memory 1 2))
      (import "m" "g" (global i32)) (import "m" "g2" (global (mut i64)))
      (import "m" "e" (tag (param i32)))
      (table $own 2 funcref) (memory $m2 3)
      (func (export "go") (param i32) (result i64) (call $f (local.get 0)))
      (export "t" (table 0)) (export "mem" (memory 0)) (export "g" (global 1)) (export "e" (tag 0))
      (export "tag2" (tag $own2)) (tag $own2 (param i64))""",
    "segments": """
      (memory 1) (table 4 funcref) (func $a) (func $b (export "b"))
      (global $g (mut i32) (i32.const 0))
      (elem (i32.const 1) $a $b) (elem func $a) (elem declare func $b)
      (elem (i32.const 0) funcref (ref.func $a) (ref.null func))
      (data (i32.const 8) "abc") (data "passive") (data (i32.const 0) "")
      (start $a)""",
    "gc": """
      (rec (type $s (sub (struct (field i8) (field (mut i32)))))
           (type $r (sub $s (struct (field i8) (field (mut i32)) (field (ref null $s))))))
      (type $a (array (mut i8))) (type $f (func (param (ref null $s)) (result anyref)))
      (type $o (sub (func (param i32)))) (type $o2 (sub $o (func (param i32))))
      (global (ref null $s) (struct.new_default $s))
      (global (ref $a) (array.new_fixed $a 2 (i32.const 1) (i32.const 2)))
      (func (export "f") (param anyref) (result i32) (ref.test (ref null $r) (local.get 0)))
      (func $h (type $o)) (table 1 funcref) (elem (i32.const 0) $h)
      (func (export "g") (param i32) (call_indirect (type $o) (local.get 0) (i32.const 0)))""",
}  # fmt: skip


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("name", HAND)
def test_hand_modules(name: str, target: str, mode: str, monkeypatch: pytest.MonkeyPatch) -> None:
    data = wat(f"(module {HAND[name]})")
    c = compile_for(data, target, mode, monkeypatch)
    if c is None:
        pytest.skip(f"{target} does not compile {name}")
    roundtrip(c, data, target)
