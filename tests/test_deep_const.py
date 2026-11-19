"""A constant expression nests as deep as the binary is long (LESSONS 136): the validator, the
parser and the targets take a 300 000-deep `i32.add` chain without Python recursion."""

import sys

import pytest

from tests.jit import native, native_jit
from waboros.frontend.binary import parse  # pyright: ignore[reportUnknownVariableType]
from waboros.frontend.validate import Invalid, validate  # pyright: ignore[reportUnknownVariableType]
from waboros.runtime import link
from waboros.runtime.store import StoreState
from waboros.targets import base

N = 300_000


def _leb(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _sec(i: int, body: bytes) -> bytes:
    return bytes([i]) + _leb(len(body)) + body


def _chain(bottom: bytes = b"\x41\x01") -> bytes:
    """`global (export "g") i32` = bottom, then N times `i32.const 1; i32.add`."""
    expr = bottom + b"\x41\x01\x6a" * N + b"\x0b"
    return b"\0asm\1\0\0\0" + _sec(6, b"\x01\x7f\x00" + expr) + _sec(7, b"\x01\x01g\x03\x00")


def test_deep_valid() -> None:
    validate(parse(_chain()))


def test_deep_type_error_at_the_bottom() -> None:
    with pytest.raises(Invalid, match="type mismatch"):
        validate(parse(_chain(b"\x42\x01")))  # i64.const under the i32.add chain


def _global(name: str, monkeypatch: pytest.MonkeyPatch) -> int:
    if name.endswith("-file"):
        monkeypatch.setenv("WABOROS_JIT_ARTIFACT", "1")
        name = name[:-5]
    if name != "pyc":
        native(name)
    m = parse(_chain())
    validate(m)
    t = base.select(m, base.target_order(name)) if name == "pyc" else base.TARGETS[name]()
    inst = t.instantiate(t.compile(m), link.resolve(m, {}), StoreState())
    return inst.global_get(0)


PYC = pytest.param(
    "pyc",
    marks=[
        pytest.mark.xfail(
            strict=True,
            raises=RecursionError,
            reason="pyc codegen `ModGen.val` recurses (LESSONS 136)",
        ),
        pytest.mark.skipif(
            sys.platform == "emscripten",
            reason="on Pyodide the recursion overflows the JS stack: a fatal error (LESSONS 136)",
        ),
    ],
)


@pytest.mark.parametrize("target", [PYC, native_jit(), native_jit() + "-file"])
def test_deep_end_to_end(target: str, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _global(target, monkeypatch) == N + 1
