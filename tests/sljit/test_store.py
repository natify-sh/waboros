# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportPrivateUsage=false, reportUnknownLambdaType=false, reportAttributeAccessIssue=false
"""S0016 step 2, the target-neutral part: the artifact's machine and ISA name come from Target
attributes; a development build's compiler string folds in `digest_sources()`; a store keeps the
trampolines of every compiler in it (the walk's sets), and two targets sharing one bridge address
are refused at instantiation (D71's precondition); a file of a target this process has not
registered is refused saying so. Skipped without the library; the mixed calls
are tests/jit/test_mixed.py's."""

from pathlib import Path
from typing import Any

import pytest

from tests.jit.test_compiler import JIT, build, wat
from waboros.frontend.binary import parse
from waboros.frontend.validate import validate
from waboros.runtime.store import StoreState
from waboros.targets.base import TARGETS
from waboros.targets.jit.common import artifact as A
from waboros.targets.jit.common.instance import _stacks

NAME = f"jit-sljit-{JIT[4:]}"  # the host's
if NAME not in TARGETS:
    pytest.skip(f"{NAME} is not registered here (no native/ build)", allow_module_level=True)

SRC = wat('(module (func (export "f") (param i32) (result i32) (local.get 0)))')


def test_targets_from_attributes() -> None:
    known = A.targets()
    assert known[NAME] == known[JIT] and known["jit-x86_64"] == (62, "x86-64-v1")
    assert known["jit-aarch64"] == (183, "armv8.0-a") and "pyc" not in known


def test_digest_sources_fold_into_the_compiler_string(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(A, "__version__", "0.0.0")
    monkeypatch.setattr(A, "_own_digest", lambda: "0123456789abcdef")
    assert A.compiler_string(JIT).split(" ")[1] == "0.0.0-0123456789abcdef"
    files = [tmp_path / "a" / "x.c", tmp_path / "b" / "y.py"]
    for f in files:
        f.parent.mkdir()
        f.write_text(f.name)
    monkeypatch.setattr(type(TARGETS[NAME]()), "digest_sources", lambda _: files)
    before = A.compiler_string(NAME)
    v = before.split(" ")[1]
    assert v.startswith("0.0.0-") and len(v) == 22 and v != "0.0.0-0123456789abcdef"
    files[0].write_text("changed")
    A._files_digest.cache_clear()
    assert A.compiler_string(NAME) != before
    monkeypatch.setattr(A, "__version__", "1.2.3")  # a release: its version only
    assert A.compiler_string(NAME) == f"waboros 1.2.3 {NAME} 1"


def test_a_store_keeps_each_compilers_trampolines() -> None:
    store = StoreState()
    a, b = (build(SRC, n, store=store) for n in (JIT, NAME))
    st = _stacks(store)
    ta, tb = (TARGETS[n]().trampolines() for n in (JIT, NAME))
    assert st.entries.keys() == {ta.entry_ret, tb.entry_ret}
    assert st.restores.keys() == {ta.restore, tb.restore}
    assert st.bridges == {ta.bridge: JIT, tb.bridge: NAME}
    assert a("f", 5) == [5] and b("f", 6) == [6]


def test_two_targets_on_one_bridge_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    store = StoreState()
    a = build(SRC, JIT, store=store)
    shared: Any = TARGETS[JIT]().trampolines()
    monkeypatch.setattr(type(TARGETS[NAME]()), "trampolines", lambda _: shared)
    with pytest.raises(RuntimeError, match="share one bridge address"):
        build(SRC, NAME, store=store)
    assert len(store.instances) == 1 and a("f", 3) == [3]


def test_a_file_of_an_unregistered_target_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    t: Any = TARGETS[NAME]()
    m = parse(SRC)
    validate(m)
    b = A.write(t.compile(m), target=NAME, module_sha256=bytes(32))
    assert isinstance(A.read(b), A.ModuleView)
    monkeypatch.delitem(TARGETS, NAME)  # a process without native/'s library
    with pytest.raises(A.ArtifactError, match=f"compiler target {NAME} is not registered"):
        A.read(b)
