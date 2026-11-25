# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportPrivateUsage=false
"""S0017 P2: a refusal names the S0014 step ("Order of checks") and the section it concerns."""

import struct
from collections.abc import Callable
from typing import Any

import pytest

from tests.artifact.test_format import put, secs, small, u32
from tests.artifact.test_refuse import ABI, FLAGS, JIT, OTHER, _guard_file
from tests.jit import native
from waboros.targets.jit.common import artifact as A
from waboros.targets.jit.common import guard

H, T, W = ".waboros.header", ".waboros.types", ".waboros.wasm"


def sec(blob: bytes, name: str, off: int = 0) -> int:
    return secs(blob)[name][1] + off


def patch(at: Callable[[bytes], int], new: bytes, fat: bool = False) -> Callable[[], bytes]:
    def make() -> bytes:
        b = small(JIT, wasm=fat)
        return put(b, at(b), new)

    return make


# name -> (file, load target, step, section)
CASES: dict[str, tuple[Callable[[], bytes], str, str, str | None]] = {
    "not ELF": (patch(lambda b: 0, b"X"), JIT, "1", None),
    "not ELF, fat": (patch(lambda b: 0, b"X", True), JIT, "1", None),
    "version 2": (patch(lambda b: 8, b"\2"), JIT, "2", None),
    "ident padding": (patch(lambda b: 9, b"\1"), JIT, "3", None),
    "section table outside": (patch(lambda b: 40, struct.pack("<Q", 1 << 40)), JIT, "3", None),
    "relocation": (patch(lambda b: secs(b)[".waboros.tags"][0] + 4, u32(4)), JIT, "3", None),
    "shstrtab": (patch(lambda b: sec(b, ".shstrtab"), b"\1"), JIT, "3", ".shstrtab"),
    "header reserved": (patch(lambda b: sec(b, H, 66), b"\1"), JIT, "4", H),
    "other machine": (lambda: small(OTHER), JIT, "5", H),
    "other compiler target": (lambda: small(JIT), OTHER, "5", H),
    "ctx_abi_version": (patch(lambda b: ABI, struct.pack("<H", 2)), JIT, "5", H),
    "types payload": (patch(lambda b: sec(b, T), b"\xff\xff\xff\xff"), JIT, "6", T),
    "fat wasm hash": (patch(lambda b: sec(b, W, 9), b"\xff", True), JIT, "1b", W),
}


@pytest.mark.parametrize("name", CASES)
def test_step_and_section(name: str) -> None:
    native(JIT)
    make, target, step, section = CASES[name]
    with pytest.raises(A.ArtifactError) as e:
        A.load(make(), target=target)
    assert (e.value.step, e.value.section) == (step, section)


def test_read_alone_names_the_wasm_hash_a_payload_check() -> None:
    bad = patch(lambda b: sec(b, W, 9), b"\xff", True)()
    with pytest.raises(A.ArtifactError) as e:
        A.read(bad)
    assert (e.value.step, e.value.section) == ("6", W)


def test_1b_comes_before_2_and_5() -> None:
    bad = patch(lambda b: sec(b, W, 9), b"\xff", True)()
    with pytest.raises(A.ArtifactError) as e:
        A.load(put(bad, 8, b"\2"), target=OTHER)
    assert e.value.step == "1b"


def test_guard_flags_are_step_5(monkeypatch: pytest.MonkeyPatch) -> None:
    native(JIT)
    blob = put(_guard_file(monkeypatch, False), FLAGS, struct.pack("<I", 1 | 26 << 8))
    with pytest.raises(A.ArtifactError) as e:
        A.load(blob, target=JIT)
    assert (e.value.step, e.value.section) == ("5", H)


def test_the_two_exits_without_a_step(monkeypatch: pytest.MonkeyPatch) -> None:
    native(JIT)

    def install(t: Any) -> None:
        raise MemoryError("x")

    monkeypatch.setattr(guard, "install", install)
    with pytest.raises(A.ArtifactError, match="cannot load") as e:
        A.load(_guard_file(monkeypatch, False), target=JIT)
    assert e.value.step is None
