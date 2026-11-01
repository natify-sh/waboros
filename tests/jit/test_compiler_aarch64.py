"""jit-aarch64 only (S0004 step 4b): a function whose conditional branches do not reach (±1 MiB)
is compiled again in far mode. The operations themselves are tested by test_compiler.py, which
runs on the machine's JIT target."""

import pytest

from tests.jit import native
from tests.jit.test_compiler import ROOT, build, trap_of
from w2py.targets.base import TrapCode
from w2py.targets.jit.aarch64.backend import Backend


@pytest.fixture(autouse=True)
def _jit() -> None:
    native("jit-aarch64")


def _leb(n: int) -> bytes:
    out = bytearray()
    while True:
        b, n = n & 0x7F, n >> 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _sec(sid: int, *items: bytes) -> bytes:
    body = _leb(len(items)) + b"".join(items)
    return bytes([sid]) + _leb(len(body)) + body


def _module(n: int, around: bool = False) -> bytes:
    """(func (export "f") (param $skip i32) (param $p i32) (result i32)
         (block (br_if 0 (local.get $skip))
           n x (local.set $p (i32.add (local.get $p) (i32.load8_u (local.get $p))))
         (local.get $p))
    with every byte of the one-page memory 1: $p grows by one per step and traps once it reaches
    65536; the br_if and every bounds check jump over the whole body. With `around`, "g" (+1)
    comes before it and "h" (a br_table: 10 for 0, else 20) after, so f starts
    at a non-zero offset."""
    step = b"\x20\x01\x20\x01\x2d\x00\x00\x6a\x21\x01"
    code = b"\x00\x02\x40\x20\x00\x0d\x00" + step * n + b"\x0b\x20\x01\x0b"
    data = b"\x00\x41\x00\x0b" + _leb(65536) + b"\x01" * 65536
    bodies, exports = [code], [b"\x01f\x00\x00"]
    if around:
        g = b"\x00\x20\x01\x41\x01\x6a\x0b"
        h = b"\x00\x02\x40\x02\x40\x20\x00\x0e\x01\x00\x01\x0b\x41\x0a\x0f\x0b\x41\x14\x0b"
        bodies, exports = [g, code, h], [b"\x01g\x00\x00", b"\x01f\x00\x01", b"\x01h\x00\x02"]
    return b"".join(
        (
            b"\0asm\1\0\0\0",
            _sec(1, b"\x60\x02\x7f\x7f\x01\x7f"),
            _sec(3, *[b"\x00"] * len(bodies)),
            _sec(5, b"\x00\x01"),
            _sec(7, *exports),
            _sec(10, *[_leb(len(b)) + b for b in bodies]),
            _sec(11, data),
        )
    )


def test_far_branches_compile_again_and_run(monkeypatch: pytest.MonkeyPatch) -> None:
    rewinds: list[int] = []
    rewind = Backend.rewind

    def counted(self: Backend, pos: int) -> None:
        rewinds.append(pos)
        rewind(self, pos)

    monkeypatch.setattr(Backend, "rewind", counted)
    near = build(_module(1000))
    assert rewinds == [] and near("f", 0, 0) == [1000]
    far = build(_module(50_000))  # 7 instructions per step: 1.4 MB of code
    assert rewinds == [0]
    assert far("f", 1, 7) == [7]  # the far br_if
    assert far("f", 0, 0) == [50_000]
    assert trap_of(far, "f", 0, 30_000) is TrapCode.MEMORY_OUT_OF_BOUNDS  # a far trap stub
    assert far("f", 0, 15_536) == [65_536]


def test_a_bytecode_vm_matches_pyc() -> None:
    """A real integer-only program (poc/bench/switch: a 32-case switch, so a jump table, in a
    loop over memory); CoreMark needs floats (step 4e)."""
    wasm = (ROOT / "poc/bench/switch/vm.wasm").read_bytes()
    jit, pyc = build(wasm), build(wasm, "pyc")
    for n in (0, 1, 7, 1000):
        assert jit("run", n) == pyc("run", n), n


def test_far_retry_of_a_function_at_a_non_zero_offset(monkeypatch: pytest.MonkeyPatch) -> None:
    rewinds: list[int] = []
    rewind = Backend.rewind

    def counted(self: Backend, pos: int) -> None:
        rewinds.append(pos)
        rewind(self, pos)

    monkeypatch.setattr(Backend, "rewind", counted)
    m = build(_module(50_000, around=True))
    assert len(rewinds) == 1 and rewinds[0] > 0  # only f, which starts after g
    assert (m("g", 0, 4), m("h", 0, 0), m("h", 1, 0), m("h", 5, 0)) == ([5], [10], [20], [20])
    assert m("f", 1, 7) == [7] and m("f", 0, 15_536) == [65_536]
    assert trap_of(m, "f", 0, 30_000) is TrapCode.MEMORY_OUT_OF_BOUNDS
