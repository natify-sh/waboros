# pyright: reportPrivateUsage=false, reportUnknownVariableType=false, reportUnknownMemberType=false
"""Inside an Android app process (S0006 step 5): the Android cell of platforms.yml runs this in
CPython's testbed app on an x86_64 emulator; skipped elsewhere. It checks what the former
`tools/android_check.py` checked (the JIT runs in an `untrusted_app` process and `auto` selects
it; CoreMark passes its self-check) and that guard pages work under ART: a guard fault reaches
our handler through libsigchain, whose handler stays the kernel's, so ART's own faults still
reach ART first. The app has no wasmtime (no WAT), so the module is written in binary."""

import contextlib
import ctypes
import io
import platform
import runpy
import sys
import warnings
from pathlib import Path

import pytest

from tests.jit import native_jit
from tests.jit.test_compiler import build, trap_of
from tools.run_coremark_jit import valid
from waboros.frontend.binary import parse
from waboros.frontend.validate import validate
from waboros.targets import base
from waboros.targets.base import JitUnavailableWarning, TrapCode
from waboros.targets.jit.common import guard
from waboros.targets.jit.common.compiler import Compiled

pytestmark = pytest.mark.skipif(sys.platform != "android", reason="an Android app process")
ROOT = Path(__file__).resolve().parents[2]  # the app's copy of the test-sources
# (module (memory 1) (func (export "f") (param i32) (result i32) (i32.load (local.get 0))))
LOAD = bytes.fromhex(
    "0061736d01000000" "01060160017f017f" "03020100" "0503010001" "07050101660000"
    "0a090107002000280200" "0b"
)  # fmt: skip
OOB = TrapCode.MEMORY_OUT_OF_BOUNDS


def _owner(addr: int) -> str:
    """The file of the shared object that holds `addr` (`dladdr`), or empty."""
    dladdr = ctypes.CDLL(None).dladdr
    dladdr.argtypes = [ctypes.c_void_p, ctypes.POINTER(guard._DlInfo)]
    info = guard._DlInfo()
    return (info.fname or b"").decode() if dladdr(addr, ctypes.byref(info)) else ""


def test_the_jit_runs_in_an_app_process_and_auto_selects_it() -> None:
    context = Path("/proc/self/attr/current").read_text().strip("\0\n")
    assert context.startswith("u:r:untrusted_app"), context  # SELinux: an ordinary app
    jit = native_jit()
    assert base.TARGETS[jit]().probe() is None
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        order = base.target_order("auto")
    assert order == [jit]
    assert not [w for w in caught if w.category is JitUnavailableWarning]


def test_guard_faults_reach_us_through_libsigchain(monkeypatch: pytest.MonkeyPatch) -> None:
    if platform.machine() != "x86_64":
        pytest.skip("guard pages on Android x86_64 only (S0006)")
    tgt = base.TARGETS[native_jit()]()
    monkeypatch.setenv("WABOROS_JIT_BOUNDS", "auto")
    assert guard.host_row() == "android" and guard.wanted(tgt)  # installs the handler
    mod = parse(LOAD)
    validate(mod)
    c = tgt.compile(mod)
    assert isinstance(c, Compiled) and c.flags == guard.FLAGS  # guard code
    inst = build(LOAD)
    for _ in range(100):
        assert trap_of(inst, "f", 65533) is OOB
        assert trap_of(inst, "f", 0xFFFFFFFF) is OOB
    assert inst("f", 65532) == [0]
    assert guard.broken() is None  # libsigchain's `sigaction` reports ours as the action
    # the saved action, read in bionic's layout: a handler in a shared object (faulthandler's,
    # which pytest enabled, or debuggerd's), not ours, not SIG_DFL/SIG_IGN
    prev = guard._handler["prev"][0].handler or 0
    assert prev > 1 and prev != guard._handler["addr"] and _owner(prev), hex(prev)
    # the kernel's action, read with a raw rt_sigaction (13 on x86-64; read only): still
    # libsigchain's handler, so ART's special handler runs first and calls ours after it
    assert "libsigchain.so" in Path("/proc/self/maps").read_text(errors="replace")
    kact = (ctypes.c_uint64 * 4)()  # the kernel's struct: handler, flags, restorer, mask
    syscall = ctypes.CDLL(None).syscall
    syscall.restype = ctypes.c_long
    args = (ctypes.c_long(13), ctypes.c_long(guard.SIGSEGV), None, ctypes.byref(kact), 8)
    assert syscall(*args) == 0
    assert kact[0] != guard._handler["addr"]
    assert _owner(kact[0]).endswith("libsigchain.so"), hex(kact[0])


def test_coremark_passes_its_self_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """In the default mode (`auto`: guard pages on the verified row)."""
    jit = native_jit()
    wasm, tool = ROOT / "poc/in/core.wasm", ROOT / "tools/run_coremark_jit.py"
    monkeypatch.setattr(sys, "argv", [tool.name, str(wasm), "--target", jit])
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        runpy.run_path(str(tool), run_name="__main__")
    assert valid(out.getvalue()), out.getvalue()[-700:]
