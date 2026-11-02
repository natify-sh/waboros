"""The `jit-x86_64` probe and its registration (S0002 step 2)."""

import platform
import warnings

import pytest

from tests.jit import native
from waboros.targets import base
from waboros.targets.base import TARGETS, Unsupported, target_order
from waboros.targets.jit.x86_64 import JitX86_64Target


def test_probe_succeeds_here() -> None:
    native("jit-x86_64")
    assert JitX86_64Target().probe() is None


def test_probe_refuses_other_architectures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "machine", lambda: "aarch64")
    why = JitX86_64Target().probe()
    assert why is not None and "aarch64" in why


def test_registered_and_selected_by_auto() -> None:
    """Ready since step 3e, jit-aarch64 since 4e (expected failures equal pyc's,
    tests/spec/test_runner.py): `auto` selects the JIT whose probe succeeds."""
    target = TARGETS["jit-x86_64"]()
    assert target.name == "jit-x86_64"
    ready = [n for n in TARGETS if n.startswith("jit-") and TARGETS[n]().probe() is None]
    assert target_order("auto") == (ready[:1] or ["pyc"])
    assert target.supports({}) is not None
    with pytest.raises(Unsupported):
        target.compile({})


def test_aarch64_probe_here_or_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    from waboros.targets.jit.aarch64 import JitAarch64Target

    t = JitAarch64Target()
    if platform.machine().lower() in t.machines:
        native("jit-aarch64")
        assert t.probe() is None
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    assert "not an aarch64 process (x86_64" in str(t.probe())
    assert t.supports({}) is not None
    with pytest.raises(Unsupported):
        t.compile({})


def test_auto_skips_an_incomplete_jit(monkeypatch: pytest.MonkeyPatch) -> None:
    """S0004: `complete = False` keeps `auto` on pyc with the third D31 message, even where the
    probe succeeds; an explicit choice still gives the target (every JIT is complete since 4e:
    the guard is tested on a patched one)."""
    from waboros.targets.jit.aarch64 import JitAarch64Target

    monkeypatch.setattr(JitAarch64Target, "complete", False)

    def probe(name: str) -> str | None:
        return None if name == "jit-aarch64" else "no"

    monkeypatch.setattr(base, "_probe", probe)
    monkeypatch.setattr(base, "_warned", set[str]())
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        assert target_order("auto") == ["pyc"] == target_order("auto")
    msg = "waboros: jit-aarch64 incomplete (its expected failures differ from pyc's); using pyc"
    assert [str(x.message) for x in w] == [msg]
    assert target_order("jit-aarch64") == ["jit-aarch64"]
