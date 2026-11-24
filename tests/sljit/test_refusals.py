# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
"""The refusal paths of `jit-sljit-x86_64` (S0016): a C-entry stub that is not 16 bytes, a native
frame above the headroom, a stale library; and no sljit compiler left behind by `supports` or by
a compile that raises. Skipped without `native/`'s library."""

import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from tests.jit.test_compiler import wat
from waboros.frontend.binary import parse
from waboros.frontend.validate import validate
from waboros.targets.base import TARGETS, Unsupported

NAME = "jit-sljit-x86_64"
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
    real = backend.Backend.custom

    def custom(self: backend.Backend, code: bytes) -> None:
        if code != b"\xcc":  # the pad
            real(self, code)

    monkeypatch.setattr(backend.Backend, "custom", custom)
    with pytest.raises(Unsupported, match="is not 16 bytes"):
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
