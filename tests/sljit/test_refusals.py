# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
"""The refusal paths of the host's `jit-sljit` (S0016): a C-entry stub that is not 16 (x86-64) /
12 (aarch64) bytes, a native frame above the headroom, a stale library, a buffer below 4 GiB or
(aarch64) off a page; and no sljit compiler left behind by `supports` or by a compile that
raises. Skipped without `native/`'s library."""

import ctypes
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from tests.jit.test_compiler import JIT, wat
from waboros.frontend.binary import parse
from waboros.frontend.validate import validate
from waboros.targets.base import TARGETS, Unsupported

NAME = f"jit-sljit-{JIT[4:]}"  # the host's
if NAME not in TARGETS:
    pytest.skip(f"{NAME} is not registered here (no native/ build)", allow_module_level=True)

from tests import sljit  # noqa: E402
from tests.sljit import backend  # noqa: E402

SRC = '(module (func (export "f") (param i32) (result i32) (i32.add (local.get 0) (i32.const 1))))'


def module() -> Any:
    m = parse(wat(SRC))
    validate(m)
    return m


def test_a_stub_whose_jump_sljit_shortened_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the pad before centry a tiny module's stub jump is within +-128 bytes: sljit
    emits the 2-byte form, the body moves off stub + 16, and `finish` refuses the module."""
    if backend.A64:
        pytest.skip("sljit's aarch64 `b` has no short form")
    real = backend.Backend.custom

    def custom(self: backend.Backend, code: bytes) -> None:
        if code != backend.PAD:
            real(self, code)

    monkeypatch.setattr(backend.Backend, "custom", custom)
    with pytest.raises(Unsupported, match="is not 16 bytes"):
        TARGETS[NAME]().compile(module())


def test_a_stub_without_its_second_word_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """`lea rdx` (x86-64) / `adr x2` (aarch64) dropped: the stub's bytes differ, refused."""
    real = backend.Backend.custom

    def custom(self: backend.Backend, code: bytes) -> None:
        if code != backend.STUB_HEAD[4:]:
            real(self, code)

    monkeypatch.setattr(backend.Backend, "custom", custom)
    with pytest.raises(Unsupported, match=f"is not {backend.STUB} bytes"):
        TARGETS[NAME]().compile(module())


def test_a_frame_above_the_headroom_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backend, "HEADROOM", 32)
    with pytest.raises(Unsupported, match="headroom"):
        TARGETS[NAME]().compile(module())


def test_no_compiler_is_left_behind(monkeypatch: pytest.MonkeyPatch) -> None:
    """`supports` builds a backend and never emits: no sljit compiler is made. A compile that
    raises frees the compiler it made (here at the first body's headroom check)."""
    made: list[int] = []
    freed: list[int] = []
    real_create, real_free = backend.lib.waboros_sljit_create, backend.lib.sljit_free_compiler

    class Lib:
        def __getattr__(self, n: str) -> Any:
            return getattr(real_lib, n)

        def waboros_sljit_create(self) -> int:
            made.append(real_create())
            return made[-1]

        def sljit_free_compiler(self, c: int) -> None:
            freed.append(c)
            real_free(c)

    real_lib = backend.lib
    monkeypatch.setattr(backend, "lib", Lib())
    t = TARGETS[NAME]()
    assert t.supports(module()) is None and made == []
    t.compile(module())
    assert len(made) == 1 and freed == made
    monkeypatch.setattr(backend, "HEADROOM", 32)
    with pytest.raises(Unsupported):
        t.compile(module())
    assert len(made) == 2 and freed == made


def test_a_library_older_than_its_sources_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert sljit.LIB is not None
    old = tmp_path / sljit.NAME
    shutil.copy(sljit.LIB, old)
    os.utime(old, (0, 0))
    monkeypatch.setenv("WABOROS_SLJIT_LIB", str(tmp_path))
    with pytest.raises(RuntimeError, match="older than"):
        sljit._library()  # pyright: ignore[reportPrivateUsage]
    os.utime(old)  # now: newer than every source
    assert sljit._library() == old  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("where", ["low", "off a page"])
def test_code_generated_below_4_gib_or_off_a_page_is_refused(
    monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    """Below 4 GiB sljit forms a label address (SLJIT_MOV_ADDR: centry's return point, the
    restore point) as an absolute imm32 on x86-64, which the copy of the code would not
    relocate; on aarch64 one beyond +-1 MiB is `adrp`, page-relative, so the buffer must start
    on a page as the loaded code does. The buffer is freed and the compile refused. Simulated:
    the real buffer reported at another address."""
    if where != "low" and not backend.A64:
        pytest.skip("x86-64's label addresses are rip-relative")
    real_lib, real, freed, low = backend.lib, [], [], 0x10000000

    class Lib:
        def __getattr__(self, n: str) -> Any:
            return getattr(real_lib, n)

        def sljit_generate_code(self, c: int, a: int, b: Any) -> int:
            real.append(real_lib.sljit_generate_code(c, a, b))
            return low if where == "low" else real[-1] + 16

        def sljit_free_code(self, code: int, d: Any) -> None:
            freed.append(code)
            real_lib.sljit_free_code(real[-1], d)

    monkeypatch.setattr(backend, "lib", Lib())
    with pytest.raises(RuntimeError, match="below 4 GiB" if where == "low" else "not on a page"):
        TARGETS[NAME]().compile(module())
    assert len(real) == 1 and real[0] > 0xFFFFFFFF and real[0] % 4096 == 0
    assert freed == [low if where == "low" else real[0] + 16]


def test_a_label_address_beyond_1_mib_is_adrp_on_the_page_the_code_runs_on() -> None:
    """aarch64: a tail site more than 1 MiB before centry gets centry's return point as `adrp` +
    `add` (sljit's SLJIT_MOV_ADDR beyond +-1 MiB), page-relative, right only because the buffer
    starts on a page as the loaded code does (LESSONS 152). A's `f` tail-calls B's `g` (another
    context): entered from the host through A's C entry it returns HOP, so B's host import sees
    the same stack pointer as when `g` is called directly; a wrong return point would take
    tail_switch's first hop instead and leave its restore frame below."""
    if not backend.A64:
        pytest.skip("x86-64's label addresses are rip-relative")
    from tests.jit.test_compiler import build
    from tests.jit.test_tables import provider
    from waboros.runtime.store import StoreState
    from waboros.targets.jit.common.context import EXIT_RECORD

    big = "\n".join(["(local.set 1 (i64.div_s (local.get 1) (local.get 2)))"] * 20000)
    sa = f"""(module (type $t (func (param i32) (result i32))) (import "b" "g" (func $g (type $t)))
      (func (export "f") (type $t) (return_call $g (local.get 0)))
      (func (param i32) (result i32) (local i64 i64) {big} (local.get 0)))"""
    sb = """(module (import "env" "h" (func $h (param i32) (result i32)))
      (func (export "g") (param i32) (result i32) (call $h (local.get 0))))"""
    store, seen = StoreState(), []

    def h(args: Any) -> list[Any]:
        seen.append(ctypes.c_uint64.from_address(b.inst.ctx[EXIT_RECORD]).value)  # type: ignore[attr-defined]
        return [args[0]]

    b = build(wat(sb), NAME, imports={"env": {"h": h}}, store=store)
    a = build(wat(sa), NAME, imports={"b": provider(b)}, store=store)
    (f, _), (g, _) = a.inst.funcs  # type: ignore[attr-defined]
    code = ctypes.string_at(a.inst.code.addr + f, g - f)  # type: ignore[attr-defined]
    words = [int.from_bytes(code[i : i + 4], "little") for i in range(0, len(code), 4)]
    assert any(w & 0x9F000000 == 0x90000000 for w in words)  # adrp
    assert a("f", 3) == [3] and b("g", 4) == [4]
    assert len(seen) == 2 and seen[0] == seen[1]
