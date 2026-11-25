# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportPrivateUsage=false, reportAttributeAccessIssue=false, reportUnknownLambdaType=false
"""D75: every compiler's bridge calls a host as host(ctx, slots, word), `word` the context-word
index of the import or libcall (SysV edx, Win64 r8d, AAPCS64 w2). Seen per import (call,
call_indirect, return_call), per libcall (memory.grow) and inside a nested entry, on the
machine's JIT and each development compiler of its architecture, in-process and through files
(WABOROS_JIT_ARTIFACT=1); the Win64 bridges of the x86-64 compilers run on any x86-64 host with a
hand-assembled Win64 host that returns its third argument."""

import ctypes
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.jit.test_compiler import JIT, Inst, build, wat
from waboros.runtime.store import StoreState
from waboros.targets.base import TARGETS
from waboros.targets.jit.common.context import HOST_BRIDGE, HOST_ERROR, LIBCALLS
from waboros.targets.jit.common.instance import HOST, load_trampolines

SRC = (Path(__file__).parents[1] / "artifact" / "data" / "v1-host.wat").read_text()
WORD = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32)
ARCH = TARGETS[JIT]().arch  # type: ignore[attr-defined]
NAMES = [JIT] + [n for n, t in TARGETS.items() if getattr(t(), "development", False)
                 and getattr(t(), "arch", "") == ARCH]  # fmt: skip
KEEP: list[Any] = []


def watch(inst: Any) -> list[tuple[int, int]]:
    """Every import and libcall word of `inst` goes through a host that logs (the word it sits
    in, the word the bridge passed) and then calls the instance's own function."""
    log: list[tuple[int, int]] = []
    lay = inst.layout
    words = [lay.import_(i) for i in range(lay.imports)] + [lay.libcall(j) for j in range(LIBCALLS)]
    for w in words:
        if inst.ctx[w]:
            KEEP.append(cb := WORD(wrap(w, HOST(inst.ctx[w]), log)))
            inst.ctx[w] = ctypes.cast(cb, ctypes.c_void_p).value
    return log


def wrap(w: int, orig: Any, log: list[tuple[int, int]]) -> Callable[[int, int, int], int]:
    def host(ctx: int, slots: int, word: int) -> int:
        log.append((w, word))
        return orig(ctx, slots)

    return host


def host_module(target: str) -> Inst:
    """v1-host.wat with h(x) = x + 1 and k(x) = h(x) + grow(0) through a nested entry."""
    me: list[Inst] = []

    def k(a: Any) -> list[int]:
        return [me[0]("f", a[0])[0] + me[0]("grow", 0)[0]]  # type: ignore[operator]

    me.append(build(wat(SRC), target, {"env": {"h": lambda a: [a[0] + 1], "k": k}}, StoreState()))
    return me[0]


@pytest.fixture(params=["memory", "files"])
def files(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if request.param == "files":
        monkeypatch.setenv("WABOROS_JIT_ARTIFACT", "1")


@pytest.mark.parametrize("target", NAMES)
@pytest.mark.usefixtures("files")
def test_the_bridge_passes_the_word(target: str) -> None:
    b = host_module(target)
    inst: Any = b.inst
    log = watch(inst)
    h, k, grow = inst.layout.import_(0), inst.layout.import_(1), inst.layout.libcall(0)
    assert b("f", 4) == [5] and b("ind", 5) == [6] and b("tail", 6) == [7]
    assert b("grow", 1) == [1]
    assert b("g", 7) == [8 + 2]  # k -> (nested entry) h, grow
    assert log == [(h, h)] * 3 + [(grow, grow), (k, k), (h, h), (grow, grow)]
    assert len({h, k, grow}) == 3


def win64_host() -> int:
    """`endbr64; mov [rdx], r8; mov qword [rcx + 8 * HOST_ERROR], 0; ret`: a Win64 host function
    whose result is its third argument."""
    from tests.jit.test_boundary import blob
    from waboros.targets.jit.x86_64.encoder import R8, RCX, RDX, Asm, Mem

    a = Asm()
    a.endbr64()
    a.mov(Mem(RDX), R8, w=True)
    a.mov_imm(Mem(RCX, 8 * HOST_ERROR), 0, w=True)
    a.ret()
    return blob(a)


@pytest.mark.skipif(ARCH != "x86-64", reason="the Win64 bridges are x86-64's")
@pytest.mark.parametrize("target", NAMES)
@pytest.mark.usefixtures("files")
def test_the_win64_bridge_passes_the_word_in_r8d(target: str) -> None:
    t: Any = TARGETS[target]()
    b = host_module(target)
    inst: Any = b.inst
    KEEP.append(tramp := load_trampolines(t.runtime(), t.conventions()["win64"]))
    h = inst.layout.import_(0)
    inst.ctx[HOST_BRIDGE], inst.ctx[h] = tramp.bridge, win64_host()
    assert b("f", 4) == [h] and b("ind", 5) == [h] and b("tail", 6) == [h]
