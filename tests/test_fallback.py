# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownLambdaType=false
"""`auto` falls back to `pyc` and says so once (S0002 step 4, D25, D31)."""

import subprocess
import sys
import textwrap
import warnings
from pathlib import Path

import pytest

from waboros.targets import base
from waboros.targets.jit.x86_64 import JitX86_64Target

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WABOROS_TARGET", raising=False)
    monkeypatch.setattr(base, "_warned", set())
    monkeypatch.setattr(base, "_probe", lambda name: "no way")
    monkeypatch.setattr(base.platform, "machine", lambda: "x86_64")


def test_warns_once_per_target_and_reason(fresh: None) -> None:
    with pytest.warns(
        base.JitUnavailableWarning, match=r"jit-x86_64 unavailable \(no way\); using pyc"
    ):
        assert base.target_order("auto") == ["pyc"]
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert base.target_order("auto") == ["pyc"]


def test_no_warning_when_chosen(fresh: None, monkeypatch: pytest.MonkeyPatch) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert base.target_order("pyc") == ["pyc"]
        monkeypatch.setenv("WABOROS_TARGET", "pyc")
        assert base.target_order() == ["pyc"]


def test_warns_once_when_no_jit_for_this_machine(
    fresh: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(base.platform, "machine", lambda: "riscv64")
    with pytest.warns(base.JitUnavailableWarning, match=r"no JIT target for riscv64; using pyc"):
        assert base.target_order("auto") == ["pyc"]
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert base.target_order("auto") == ["pyc"]


def test_explicit_jit_with_failing_probe_is_an_error(
    fresh: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(JitX86_64Target, "probe", lambda _self: "no way")
    order = base.target_order("jit-x86_64")
    assert order == ["jit-x86_64"]
    with pytest.raises(base.Unsupported, match="no way"):
        base.select({}, order)


def test_mdwe_forbids_executable_memory() -> None:
    """For real: the kernel refuses mprotect(PROT_EXEC) on written memory (Linux >= 6.3)."""
    if not sys.platform.startswith("linux"):
        pytest.skip("MDWE is Linux-only")
    child = textwrap.dedent("""
        import ctypes, sys, warnings
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(65, 1, 0, 0, 0) != 0:
            sys.exit(77)
        from waboros.targets import base
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            order = base.target_order("auto")
            base.target_order("auto")
        why = base.TARGETS["jit-x86_64"]().probe()
        assert why, why
        assert order == ["pyc"], order
        assert [x.category for x in w] == [base.JitUnavailableWarning], w
        assert why in str(w[0].message)
        from tests.test_target_pyc import F32_ID
        from waboros.frontend.binary import parse
        from waboros.frontend.validate import validate
        from waboros.runtime import link
        from waboros.runtime.store import StoreState
        mod = parse(F32_ID)
        validate(mod)
        t = base.select(mod, order)
        assert t.name == "pyc"
        inst = t.instantiate(t.compile(mod), link.resolve(mod, {}), StoreState())
        assert inst.call(0, [0x3F800000]) == [0x3F800000]
        print("ok")
    """)
    r = subprocess.run([sys.executable, "-c", child], capture_output=True, text=True, cwd=ROOT)
    if r.returncode == 77:
        pytest.skip("PR_SET_MDWE unavailable (needs Linux >= 6.3)")
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stderr
