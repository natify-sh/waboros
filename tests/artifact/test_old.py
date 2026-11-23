# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportArgumentType=false, reportPrivateUsage=false
"""S0014 step 4.3: the committed v1 artifacts (tests/artifact/data, made once by
tools/make_v1_artifacts.py, never regenerated) are still read, still run, or are refused with the
version message. If a later change bumps `ctx_abi_version` or `format_version` on purpose,
`test_view_runs` must change to expect that refusal (the version message), not be deleted.

S0015 (the boundary convention, v1 amended): `v1-small-*` are pre-S0015 fixtures. Their code calls
records and is entered the old way, so `read` refuses them on the layout alone: the size of
`.waboros.trampolines` (A4), whatever the compiler string says. The fat ones are regenerated from
their wasm and run. `v1-s15-small-*` are the files written by S0015 step 4: read, run, loaded."""

from pathlib import Path
from typing import Any

import pytest

from tests.jit import native, native_jit
from waboros.frontend.binary import parse
from waboros.frontend.validate import validate
from waboros.runtime import link
from waboros.runtime.store import StoreState
from waboros.targets import base
from waboros.targets.jit.common import artifact as A

DATA = Path(__file__).parent / "data"
HOST = native_jit()
ARCHS = ("x86_64", "aarch64")
WASM = (DATA / "v1-small.wasm").read_bytes()
# the payload's size, as `read` says it (S0015 "Versions"): 4 + 40 per convention then, 4 + 28 now
SIZES = {"x86_64": (84, 60), "aarch64": (44, 32)}
EXPORTS = [("mem", 2, 0), ("add", 0, 1), ("load", 0, 2), ("table", 0, 3), ("glob", 0, 4),
           ("bump", 0, 5), ("obj", 0, 6)]  # fmt: skip


@pytest.fixture(autouse=True)
def explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WABOROS_JIT_BOUNDS", "explicit")


def get(arch: str, thin: bool, new: bool = True) -> bytes:
    return (
        DATA / f"{'v1-s15' if new else 'v1'}-small-{arch}{'-thin' if thin else ''}.wbo"
    ).read_bytes()


def layout(arch: str) -> str:
    old, new = SIZES[arch]
    return f"the payload is {old} bytes, the layout of S0015 .* has {new}"


def check_runs(target: str, c: Any) -> None:
    """The exports give the expected results (the module's wat is next to the files)."""
    m = parse(WASM)
    validate(m)
    i = base.TARGETS[target]().instantiate(c, link.resolve(m, {}), StoreState())
    assert i.call(1, [40, 2]) == [42] and i.call(2, []) == [42] and i.call(4, []) == [47]
    assert i.call(3, [1]) == [1] and i.call(6, []) == [7]
    with pytest.raises(base.Trap):
        i.call(3, [0])  # a null element
    assert i.call(5, []) == [6] and i.call(5, []) == [7] and i.global_get(1) == 7


@pytest.mark.parametrize("thin", [True, False])
@pytest.mark.parametrize("arch", ARCHS)
def test_pre_s0015_refused_on_the_layout(
    arch: str, thin: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    b = get(arch, thin, new=False)
    with pytest.raises(A.ArtifactError, match=layout(arch)):
        A.read(b)
    if f"jit-{arch}" != HOST:
        return
    native(HOST)
    # the compiler string passed: the layout decides alone
    compiler = str(A._head(b)[1].compiler)

    def same(_target: str) -> str:
        return compiler

    monkeypatch.setattr(A, "compiler_string", same)
    if thin:
        with pytest.raises(A.ArtifactError, match=layout(arch)):
            A.load(b, target=HOST)
    else:
        check_runs(HOST, A.load(b, target=HOST))


@pytest.mark.parametrize("thin", [True, False])
@pytest.mark.parametrize("arch", ARCHS)
def test_read(arch: str, thin: bool) -> None:
    v = A.read(get(arch, thin))
    assert v.target == f"jit-{arch}" and v.ctx_abi_version == 1 and v.flags == 0
    assert (v.wasm is None) == thin and v.compiler.startswith("waboros ")
    assert v.module["exports"] == EXPORTS
    assert [g[:2] for g in v.module["globals"]] == [(0x7F, 0), (0x7F, 1), ((1, 0), 0)]
    assert v.module["globals"][0][2] == (("i32", 42),)
    assert v.module["mems"] == [(1, None)] and v.module["tables"] == [(2, None)]
    assert len(v.module["datas"]) == 1 and len(v.module["elem_info"]) == 1


@pytest.mark.parametrize("thin", [True, False])
def test_view_runs(thin: bool) -> None:
    """Bypasses `load`'s compiler check: context ABI v1 still runs the code of the file."""
    native(HOST)
    check_runs(HOST, A.read(get(HOST.split("-")[1], thin)))


@pytest.mark.parametrize("arch", ARCHS)
def test_thin_load(arch: str) -> None:
    target, b = f"jit-{arch}", get(arch, True)
    if target != HOST:
        with pytest.raises(A.ArtifactError, match="e_machine"):
            A.load(b, target=target)
        return
    native(HOST)
    if A.read(b).compiler == A.compiler_string(target):
        assert isinstance(A.load(b, target=target), A.ModuleView)
    else:
        with pytest.raises(A.ArtifactError, match="compiler"):
            A.load(b, target=target)


@pytest.mark.parametrize("new", [True, False])
def test_fat_load(new: bool) -> None:
    """Either the view (same compiler) or a module regenerated from the wasm."""
    native(HOST)
    check_runs(HOST, A.load(get(HOST.split("-")[1], False, new), target=HOST))


def patch(b: bytes) -> bytes:
    return b[:8] + b"\2" + b[9:]  # EI_ABIVERSION = 2


@pytest.mark.parametrize("new", [True, False])
@pytest.mark.parametrize("arch", ARCHS)
def test_format_version_2(arch: str, new: bool) -> None:
    target = f"jit-{arch}"
    with pytest.raises(A.ArtifactError, match="format version 2"):
        A.load(patch(get(arch, True, new)), target=target)
    c = A.load(patch(get(arch, False, new)), target=target)
    assert not isinstance(c, A.ModuleView)
    if target == HOST:
        native(HOST)
        check_runs(target, c)
