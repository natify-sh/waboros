# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportArgumentType=false, reportPrivateUsage=false
"""S0014 step 4.1: the loader's compatibility rules. A thin file that fails is refused with a
message naming the field and both values; a fat file is regenerated from its wasm and runs; a file
that is not ours, or a fat file whose wasm is not `module_sha256`, is refused either way."""

import shutil
import struct
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.artifact.test_format import SMALL, explicit, parse_valid, small
from tests.jit import native, native_jit
from waboros.runtime import link
from waboros.runtime.store import StoreState
from waboros.targets import base
from waboros.targets.jit.common import artifact as A
from waboros.targets.jit.common import guard

JIT = native_jit()
OTHER = "jit-aarch64" if JIT == "jit-x86_64" else "jit-x86_64"
_ = explicit
# file offsets: the header section is at 64, its payload fields at 64 + the spec's offsets
ABI, ISA, FLAGS, FEATS, REQ, OPT = 128, 132, 136, 144, 152, 160


def put(b: bytes, at: int, data: bytes) -> bytes:
    return b[:at] + data + b[at + len(data) :]


def run(c: Any) -> None:
    """The regenerated module runs: f(8) reads the two data bytes `ab`."""
    t = base.TARGETS[JIT]()
    inst = t.instantiate(c, link.resolve(parse_valid(), {}), StoreState())
    assert inst.call(0, [8]) == [0x6261]


def both(patch: Callable[[bytes], bytes], why: str) -> None:
    """The patched thin file is refused with `why` in the message; the fat one is regenerated."""
    with pytest.raises(A.ArtifactError, match=why):
        A.load(patch(small(JIT)), target=JIT)
    c = A.load(patch(small(JIT, wasm=True)), target=JIT)
    assert not isinstance(c, A.ModuleView)
    run(c)


def test_loads_what_it_wrote() -> None:
    native(JIT)
    v = A.load(small(JIT), target=JIT)
    assert isinstance(v, A.ModuleView) and v.compiler == A.compiler_string(JIT)
    assert isinstance(A.load(small(JIT, wasm=True), target=JIT), A.ModuleView)


@pytest.mark.parametrize("fat", [False, True])
@pytest.mark.parametrize("bad", [(0, b"X"), (7, b"\0")])
def test_not_ours_is_refused_even_fat(bad: tuple[int, bytes], fat: bool) -> None:
    with pytest.raises(A.ArtifactError, match="not a waboros artifact"):
        A.load(put(small(JIT, wasm=fat), *bad), target=JIT)


@pytest.mark.parametrize(
    ("at", "val", "why"),
    [(8, 0, "format version 0"), (8, 2, "format version 2")],
)
def test_format_version(at: int, val: int, why: str) -> None:
    native(JIT)
    both(lambda b: put(b, at, bytes((val,))), why)


@pytest.mark.parametrize(
    ("at", "data", "why"),
    [
        (ABI, struct.pack("<H", 2), "ctx_abi_version is 2"),
        (ISA, struct.pack("<H", 3), "isa_level is 3"),
        (ISA, struct.pack("<H", 0), "isa_level is 0"),
        (FEATS, struct.pack("<Q", 1 << 40), "isa_features is 0x"),
        (FLAGS, struct.pack("<I", 2), "flags is 0x2"),
        (FLAGS, struct.pack("<I", 1 << 8), "flags is 0x100"),
        (REQ, struct.pack("<Q", 1), "features_required is 0x1"),
    ],
)
def test_header_fields(at: int, data: bytes, why: str) -> None:
    native(JIT)
    both(lambda b: put(b, at, data), why)


def test_unknown_optional_feature_is_accepted() -> None:
    native(JIT)
    v = A.load(put(small(JIT), OPT, struct.pack("<Q", 1 << 63)), target=JIT)
    assert isinstance(v, A.ModuleView) and v.features_optional == 1 << 63


def test_other_machine() -> None:
    native(JIT)
    blob = small(OTHER)  # cross-compiled (D35): never run on this host
    with pytest.raises(A.ArtifactError, match="e_machine is"):
        A.load(blob, target=JIT)
    with pytest.raises(A.ArtifactError, match="e_machine is"):
        A.load(blob, target=OTHER)
    run(A.load(small(OTHER, wasm=True), target=JIT))  # regenerated for this host


def test_other_compiler_target_and_version() -> None:
    native(JIT)
    with pytest.raises(A.ArtifactError, match="compiler is 'waboros .* " + JIT):
        A.load(small(JIT), target=OTHER)  # written for this host, the loader's target is the other
    assert not isinstance(A.load(small(JIT, wasm=True), target=OTHER), A.ModuleView)

    def version(b: bytes) -> bytes:
        i = b.index(b"waboros ") + 8
        return put(b, i, b"9" if b[i : i + 1] != b"9" else b"8")

    both(version, "compiler is 'waboros 9")


def _guard_file(monkeypatch: pytest.MonkeyPatch, wasm: bool) -> bytes:
    monkeypatch.setenv("WABOROS_JIT_BOUNDS", "guard")
    blob = small(JIT, wasm=wasm)
    monkeypatch.setenv("WABOROS_JIT_BOUNDS", "explicit")  # what the fat file regenerates as
    return blob


@pytest.mark.parametrize("wasm", [False, True])
def test_guard_unavailable(monkeypatch: pytest.MonkeyPatch, wasm: bool) -> None:
    native(JIT)
    blob = _guard_file(monkeypatch, wasm)
    monkeypatch.setattr(guard, "broken", lambda: "forced by the test")
    if wasm:
        run(A.load(blob, target=JIT))
    else:
        with pytest.raises(A.ArtifactError, match="BOUNDS_GUARD is set.*forced by the test"):
            A.load(blob, target=JIT)


@pytest.mark.parametrize("mode", ["auto", "guard"])
def test_guard_file_on_an_opt_in_row(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    """S0017 Decision 10: on a row whose handler is not verified (Android arm64: in
    `guard_rows`, not in `verified`) a thin guard file is refused under `auto` and accepted under
    `guard`, as guard.wanted() decides for compiling. A simulated host: this target takes
    jit-aarch64's rows, the row is `android`, install and the chain check are stubbed."""
    from waboros.targets.jit.aarch64 import JitAarch64Target

    native(JIT)
    blob = _guard_file(monkeypatch, False)
    cls = type(base.TARGETS[JIT]())
    for rows in ("guard_rows", "verified"):
        monkeypatch.setattr(cls, rows, getattr(JitAarch64Target, rows))
    monkeypatch.setattr(guard, "host_row", lambda: "android")

    def install(target: Any) -> None:
        pass

    monkeypatch.setattr(guard, "install", install)
    monkeypatch.setattr(guard, "broken", lambda: None)
    monkeypatch.setenv("WABOROS_JIT_BOUNDS", mode)
    if mode == "auto":
        with pytest.raises(A.ArtifactError, match="BOUNDS_GUARD is set.*opt-in") as e:
            A.load(blob, target=JIT)
        assert e.value.step == "5"
    else:
        assert isinstance(A.load(blob, target=JIT), A.ModuleView)


@pytest.mark.parametrize("wasm", [False, True])
def test_guard_log2_larger_than_the_hosts(monkeypatch: pytest.MonkeyPatch, wasm: bool) -> None:
    native(JIT)
    blob = put(_guard_file(monkeypatch, wasm), FLAGS, struct.pack("<I", 1 | 26 << 8))
    if wasm:
        run(A.load(blob, target=JIT))
    else:
        with pytest.raises(A.ArtifactError, match="GUARD_LOG2 is 26"):
            A.load(blob, target=JIT)


def test_fat_file_with_another_wasm_is_refused() -> None:
    native(JIT)
    blob = small(JIT, wasm=True)
    bad = put(blob, blob.index(SMALL) + len(SMALL) - 1, b"c")  # `ab` is the last data
    assert bad != blob
    with pytest.raises(A.ArtifactError, match="corrupt"):
        A.load(bad, target=JIT)
    with pytest.raises(A.ArtifactError, match="corrupt"):  # also where the rest is refused anyway
        A.load(put(bad, 8, b"\2"), target=JIT)


@pytest.mark.parametrize("wasm", [False, True])
def test_every_prefix_is_refused_by_load(wasm: bool) -> None:
    """Every prefix length through `load` (0.05 s each, measured): refused, never a crash."""
    blob = small(JIT, wasm=wasm)
    for n in range(len(blob)):
        with pytest.raises(A.ArtifactError):
            A.load(blob[:n], target=JIT)


@pytest.mark.parametrize("wasm", [False, True])
def test_huge_section_header_offset(wasm: bool) -> None:
    native(JIT)
    blob = small(JIT, wasm=wasm)
    for off in (1 << 63, (1 << 64) - 1, 1 << 40):
        bad = put(blob, 40, struct.pack("<Q", off))
        with pytest.raises(A.ArtifactError):
            A.load(bad, target=JIT)


def test_compatibility_comes_before_the_payloads() -> None:
    native(JIT)
    blob = small(JIT)
    h = A._sections_r(blob)[0]
    at = blob.index(h["types"])
    bad = put(put(blob, ABI, struct.pack("<H", 2)), at, b"\xff\xff\xff\xff")
    with pytest.raises(A.ArtifactError, match="ctx_abi_version is 2"):
        A.load(bad, target=JIT)
    with pytest.raises(A.ArtifactError, match="types"):  # the payload alone is refused by read
        A.load(put(blob, at, b"\xff\xff\xff\xff"), target=JIT)


def test_fat_file_regenerates_whatever_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    native(JIT)

    def install(t: object) -> None:
        raise MemoryError("x")

    monkeypatch.setattr(guard, "install", install)
    blob = _guard_file(monkeypatch, True)
    run(A.load(blob, target=JIT))  # the guard handler failed with a MemoryError: regenerated
    with pytest.raises(A.ArtifactError, match="cannot load"):
        A.load(_guard_file(monkeypatch, False), target=JIT)  # thin: wrapped, not escaped
    junk = b"\0asm\1\0\0\0\xff"  # a hash that matches, a wasm that does not validate
    m = parse_valid()
    c = base.TARGETS[JIT]().compile(m)
    bad = put(A.write(c, target=JIT, wasm=junk), ABI, struct.pack("<H", 2))
    with pytest.raises(A.ArtifactError, match="cannot be compiled"):
        A.load(bad, target=JIT)


def test_compiler_string(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    s = A.compiler_string(JIT)
    assert s == A.compiler_string(JIT) and len(s.split(" ")) == 4 and s.isascii()
    root = Path(A.__file__).parents[3]
    for d in ("frontend", "targets/jit"):
        shutil.copytree(root / d, tmp_path / d, ignore=shutil.ignore_patterns("__pycache__"))
    before = A._source_digest(tmp_path)
    assert before == A._source_digest(tmp_path) == A._own_digest() and len(before) == 16
    (tmp_path / "frontend" / ".#binary.py").symlink_to("nowhere@host.1")  # an editor's lock
    assert A._source_digest(tmp_path) == before
    with (tmp_path / "frontend" / "binary.py").open("a") as f:
        f.write("# changed\n")
    assert A._source_digest(tmp_path) != before
    # a development build carries the digest, a release its version verbatim
    monkeypatch.setattr(A, "_own_digest", lambda: "0123456789abcdef")
    for v, want in [
        ("0.0.1.dev207+gabc.d20261115", "0.0.1.dev207-0123456789abcdef"),
        ("0.0.0", "0.0.0-0123456789abcdef"),
        ("1.2.3", "1.2.3"),
        ("1.2.3rc1", "1.2.3rc1"),
    ]:
        monkeypatch.setattr(A, "__version__", v)
        assert A.compiler_string(JIT).split(" ")[1] == want
