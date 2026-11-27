# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportArgumentType=false, reportPrivateUsage=false, reportUnknownLambdaType=false, reportAttributeAccessIssue=false
"""The committed v1 artifacts (tests/artifact/data, written once by tools/make_v1_artifacts.py) are
still read, still run, or are refused with the version message. If a later change bumps
`ctx_abi_version` or `format_version` on purpose, `test_view_runs` must change to expect that
refusal (the version message), not be deleted.

`v1-d75-*` (S0017 P1, D75: the bridge passes the import's word): modules `small` and `host`, both
JITs, both bounds modes, fat and thin; read, run (the view on the native architecture), loaded.
`v1-s15-small-x86_64[-thin].wbo` are the two files kept from before D75 (S0017 Decision 7): their
bridge is not this compiler's, so `read` refuses them on the trampoline bytes, and `load`
regenerates the fat one from its wasm. The pre-S0015 record layout (40-byte records) is refused
by `tests/artifact/test_trampolines.py::test_the_payload_has_the_layout_of_s0015`."""

from pathlib import Path
from typing import Any

import pytest

from tests.jit import native, native_jit
from tests.jit.test_host_word import watch
from waboros.frontend.binary import parse
from waboros.frontend.validate import validate
from waboros.runtime import link
from waboros.runtime.store import StoreState
from waboros.targets import base
from waboros.targets.jit.common import artifact as A
from waboros.targets.jit.common import guard

DATA = Path(__file__).parent / "data"
HOST = native_jit()
ARCHS = ("x86_64", "aarch64")
MODS = ("small", "host")
BOUNDS = ("guard", "explicit")
WASM = {m: (DATA / f"v1-{m}.wasm").read_bytes() for m in MODS}
EXPORTS = {
    "small": [("mem", 2, 0), ("add", 0, 1), ("load", 0, 2), ("table", 0, 3), ("glob", 0, 4),
              ("bump", 0, 5), ("obj", 0, 6)],
    "host": [("mem", 2, 0), ("f", 0, 2), ("g", 0, 3), ("ind", 0, 4), ("tail", 0, 5),
             ("grow", 0, 6)],
}  # fmt: skip


@pytest.fixture(autouse=True)
def explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WABOROS_JIT_BOUNDS", "explicit")


def get(mod: str, arch: str, bounds: str, thin: bool) -> bytes:
    return (DATA / f"v1-d75-{mod}-{arch}-{bounds}{'-thin' if thin else ''}.wbo").read_bytes()


def runnable(bounds: str, monkeypatch: pytest.MonkeyPatch) -> None:
    native(HOST)
    if bounds == "guard":
        try:
            guard.install(base.TARGETS[HOST]())
        except RuntimeError as e:
            pytest.skip(str(e))
        monkeypatch.setenv("WABOROS_JIT_BOUNDS", "guard")


def check_runs(mod: str, target: str, c: Any) -> None:
    """The exports give the expected results (the modules' wat is next to the files); `host`'s
    host calls get their import's word (D75), a nested entry included."""
    m = parse(WASM[mod])
    validate(m)
    me: list[Any] = []
    imps = {"env": {"h": lambda a: [a[0] + 1], "k": lambda a: me[0].call(2, [a[0]])}}
    i = base.TARGETS[target]().instantiate(c, link.resolve(m, imps if mod == "host" else {}),
                                           StoreState())  # fmt: skip
    if mod == "host":
        me.append(i)
        log = watch(i)
        assert i.call(2, [4]) == [5] and i.call(4, [5]) == [6] and i.call(5, [6]) == [7]
        assert i.call(3, [7]) == [8] and i.call(6, [1]) == [1]
        h, k, grow = i.layout.import_(0), i.layout.import_(1), i.layout.libcall(0)
        assert log == [(h, h)] * 3 + [(k, k), (h, h), (grow, grow)]
        return
    assert i.call(1, [40, 2]) == [42] and i.call(2, []) == [42] and i.call(4, []) == [47]
    assert i.call(3, [1]) == [1] and i.call(6, []) == [7]
    with pytest.raises(base.Trap):
        i.call(3, [0])  # a null element
    assert i.call(5, []) == [6] and i.call(5, []) == [7] and i.global_get(1) == 7


@pytest.mark.parametrize("thin", [True, False])
@pytest.mark.parametrize("bounds", BOUNDS)
@pytest.mark.parametrize("arch", ARCHS)
@pytest.mark.parametrize("mod", MODS)
def test_read(mod: str, arch: str, bounds: str, thin: bool) -> None:
    v = A.read(get(mod, arch, bounds, thin))
    assert v.target == f"jit-{arch}" and v.ctx_abi_version == 1
    assert (v.flags == 0) == (bounds == "explicit")
    assert (v.wasm is None) == thin and v.compiler.startswith("waboros ")
    assert v.module["exports"] == EXPORTS[mod] and v.module["mems"] == [(1, None)]
    if mod == "host":
        assert v.module["imports"] == [("func", "env", "h", 0), ("func", "env", "k", 1)]
        return
    assert [g[:2] for g in v.module["globals"]] == [(0x7F, 0), (0x7F, 1), ((1, 0), 0)]
    assert v.module["globals"][0][2] == (("i32", 42),)
    assert v.module["tables"] == [(2, None)]
    assert len(v.module["datas"]) == 1 and len(v.module["elem_info"]) == 1


@pytest.mark.parametrize("thin", [True, False])
@pytest.mark.parametrize("bounds", BOUNDS)
@pytest.mark.parametrize("mod", MODS)
def test_view_runs(mod: str, bounds: str, thin: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypasses `load`'s compiler check: context ABI v1 still runs the code of the file."""
    runnable(bounds, monkeypatch)
    check_runs(mod, HOST, A.read(get(mod, HOST.split("-")[1], bounds, thin)))


@pytest.mark.parametrize("bounds", BOUNDS)
@pytest.mark.parametrize("arch", ARCHS)
@pytest.mark.parametrize("mod", MODS)
def test_thin_load(mod: str, arch: str, bounds: str, monkeypatch: pytest.MonkeyPatch) -> None:
    target, b = f"jit-{arch}", get(mod, arch, bounds, True)
    if target != HOST:
        with pytest.raises(A.ArtifactError, match="e_machine"):
            A.load(b, target=target)
        return
    runnable(bounds, monkeypatch)
    if A.read(b).compiler == A.compiler_string(target):
        check_runs(mod, target, A.load(b, target=target))
    else:
        with pytest.raises(A.ArtifactError, match="compiler"):
            A.load(b, target=target)


@pytest.mark.parametrize("mod", MODS)
def test_fat_load(mod: str) -> None:
    """Either the view (same compiler) or a module regenerated from the wasm."""
    native(HOST)
    check_runs(mod, HOST, A.load(get(mod, HOST.split("-")[1], "explicit", False), target=HOST))


def patch(b: bytes) -> bytes:
    return b[:8] + b"\2" + b[9:]  # EI_ABIVERSION = 2


@pytest.mark.parametrize("arch", ARCHS)
@pytest.mark.parametrize("mod", MODS)
def test_format_version_2(mod: str, arch: str) -> None:
    target = f"jit-{arch}"
    with pytest.raises(A.ArtifactError, match="format version 2"):
        A.load(patch(get(mod, arch, "explicit", True)), target=target)
    c = A.load(patch(get(mod, arch, "explicit", False)), target=target)
    assert not isinstance(c, A.ModuleView)
    if target == HOST:
        native(HOST)
        check_runs(mod, target, c)


PRE_D75 = "the sysv trampolines differ from this compiler's"


@pytest.mark.parametrize("thin", [True, False])
def test_pre_d75_refused_on_the_bridge(thin: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    """S0017 Decision 7: the kept pre-D75 files. `read` refuses both on the bridge bytes on any
    host; with the file's own compiler string (so that check passes), `load` still refuses the
    thin one on the bridge bytes and regenerates the fat one, which runs."""
    b = (DATA / f"v1-s15-small-x86_64{'-thin' if thin else ''}.wbo").read_bytes()
    with pytest.raises(A.ArtifactError, match=PRE_D75):
        A.read(b)
    if HOST != "jit-x86_64":
        return
    native(HOST)
    compiler = str(A._head(b)[1].compiler)
    monkeypatch.setattr(A, "compiler_string", lambda _target: compiler)
    if thin:
        with pytest.raises(A.ArtifactError, match=PRE_D75):
            A.load(b, target=HOST)
    else:
        check_runs("small", HOST, A.load(b, target=HOST))
