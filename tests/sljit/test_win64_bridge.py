# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportPrivateUsage=false, reportUnknownLambdaType=false
"""The `win64` blob of `jit-sljit-x86_64` (S0016 "The artifact files of `jit-sljit`"), executed on
Linux: a module compiled by `jit-sljit-x86_64` calls `native/`'s `ms_abi` host function through
the `win64` bridge. The instance is made as usual (its `sysv` trampolines, a Python import); then
its context words HOST_BRIDGE and the import's are patched to the `win64` bridge of a second copy
of the blob and to the C function, so the store's sets and the distinctness check are not part of
this test. Checked: the arguments (rcx, rdx), the stack pointer at the call 16-aligned, the
32-byte shadow space owned by the host (it writes all of it, and the bridge's saved registers and
HOST_ERROR preset survive), the exit record and the walk from it. Skipped without the library."""

import ctypes
from typing import Any

import pytest

from tests.jit.test_compiler import build, wat
from waboros.runtime.store import StoreState
from waboros.targets.base import TARGETS
from waboros.targets.jit.common import walk
from waboros.targets.jit.common.context import HOST_BRIDGE, HOST_ERROR
from waboros.targets.jit.common.instance import _stacks, load_trampolines

NAME = "jit-sljit-x86_64"
if NAME not in TARGETS:
    pytest.skip(f"{NAME} is not registered here (no native/ build)", allow_module_level=True)

from tests.sljit import backend  # noqa: E402

# 8 locals live across the call, more than the pool (S3-S5, R4-R6, R3): R3-R6 are pinned
LOCALS = 8
SETS = "".join(f"(local.set {k + 1} (i64.mul (local.get 0) (i64.const {k + 3})))" for k in range(8))
SUM = "(i64.add " * 8 + "(local.get $r)" + "".join(f" (local.get {k + 1}))" for k in range(8))
SRC = f"""(module (import "env" "h" (func $h (param i64) (result i64)))
  (func (export "f") (param $a i64) (result i64) (local {"i64 " * LOCALS}) (local $r i64)
    {SETS} (local.set $r (call $h (local.get $a))) {SUM}))"""
SEEN = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint64)


def test_win64_has_the_custom_call_and_the_same_tail_switch() -> None:
    sysv, win = (backend.trampoline_blob(c) for c in ("sysv", "win64"))
    call = backend._win64_call(backend.S3)
    assert call == b"\x48\x89\xd9\x48\x89\xf2\x41\xff\xd5"  # mov rcx,rbx; mov rdx,rsi; call r13
    assert win[0][: win[1][1]].count(call) == 1 and call not in sysv[0]
    assert sysv[0][sysv[1][1] :] == win[0][win[1][1] :]  # tail_switch: the same bytes
    assert sysv[1][2] - sysv[1][1] == win[1][2] - win[1][1] and sysv[2] == win[2]


def test_a_win64_host_runs_through_the_win64_bridge() -> None:
    t: Any = TARGETS[NAME]()
    store, seen = StoreState(), []
    inst: Any = build(wat(SRC), NAME, {"env": {"h": lambda a: [a[0]]}}, store).inst
    tramp = load_trampolines(t.runtime(), t.conventions()["win64"])  # kept until the end
    lib = backend.lib
    host = ctypes.cast(lib.waboros_sljit_test_win64_host, ctypes.c_void_p).value
    inst.ctx[HOST_BRIDGE], inst.ctx[inst.layout.import_(0)] = tramp.bridge, host

    def on_call(ctx: int, slots: int, at_call: int) -> None:
        st = _stacks(store)
        seen.append((ctx, slots, at_call, tuple(st.exit), walk.walk(store, st)))
        ctypes.c_uint64.from_address(slots).value += 1000
        ctypes.c_uint64.from_address(ctx + 8 * HOST_ERROR).value = 0

    cb = SEEN(on_call)
    lib.waboros_sljit_test_win64_set.argtypes = [SEEN]
    lib.waboros_sljit_test_win64_set(cb)
    try:
        preset = inst.ctx[HOST_ERROR]
        a = 7
        assert inst.call(1, [a]) == [a + 1000 + sum(a * (k + 3) for k in range(LOCALS))]
        assert inst.ctx[HOST_ERROR] == preset  # the bridge's preset survived the shadow space
    finally:
        lib.waboros_sljit_test_win64_set(SEEN())
    st = _stacks(store)
    [(ctx, slots, at_call, (sp, pc, frame), segs)] = seen
    assert ctx == ctypes.addressof(inst.ctx) and slots == frame and at_call % 16 == 0
    assert sp and at_call < sp and inst.code.addr <= pc < inst.code.addr + inst.code.size
    [(frames, base)] = segs  # the thunk, f and the module's C entry, then the entry
    assert base == st.vstack.addr and [f[0] for f in frames] == [inst] * 3
    assert frames[0][2] == sp and frames[0][3] == frame
