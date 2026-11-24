# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportArgumentType=false, reportPrivateUsage=false
"""S0014 step 5, S0015 step 4: every artifact carries the compiler's blob (bridge, tail_switch) of
every convention of its architecture (`.waboros.trampolines`, blobs at the end of `.text`; the
runtime's entry and sp_stub are not in it); `read` requires each to be the target's, so a mismatch
is refused thin and regenerated fat; the runtime runs on its own copy; one copy per store, taken
from the first file, serves two instances (the store rule of a foreign host)."""

import ctypes
import struct
from typing import Any

import pytest

from tests.artifact.test_format import SMALL, emits, explicit, parse_valid, secs, small
from tests.artifact.test_refuse import both, put
from tests.jit import native, native_jit
from tests.jit.test_compiler import Inst, wat
from waboros.frontend.binary import parse
from waboros.frontend.validate import validate
from waboros.runtime import link
from waboros.runtime.store import StoreState
from waboros.targets import base
from waboros.targets.jit.common import artifact as A
from waboros.targets.jit.common import walk
from waboros.targets.jit.common.context import HOST_BRIDGE, TAIL_SWITCH
from waboros.targets.jit.common.instance import _stacks, load_trampolines

JIT = native_jit()
_ = explicit
REC = 28  # bytes per convention record


def records(blob: bytes) -> list[tuple[int, ...]]:
    """(file offset of the record, id, start, size, bridge, tail_switch, restore, ret_at, up)."""
    _, at, _ = secs(blob)[".waboros.trampolines"]
    n = struct.unpack_from("<I", blob, at)[0]
    return [(at + 4 + REC * k, *struct.unpack_from("<B3x5I2H", blob, at + 4 + REC * k))
            for k in range(n)]  # fmt: skip


def text_at(blob: bytes) -> int:
    return secs(blob)[".text"][1]


def test_runs_on_the_process_copy() -> None:
    native(JIT)
    t: Any = base.TARGETS[JIT]()
    v = A.load(small(JIT), target=JIT)
    assert isinstance(v, A.ModuleView) and len(v.code) % 16 == 0
    inst: Any = t.instantiate(v, link.resolve(parse_valid(), {}), StoreState())
    lo, hi = inst.code.addr, inst.code.addr + inst.code.size
    assert inst.tramp is t.trampolines() and ctypes.string_at(lo, len(v.code)) == v.code
    assert not lo <= inst.ctx[HOST_BRIDGE] < hi and not lo <= inst.ctx[TAIL_SWITCH] < hi
    assert inst.call(0, [8]) == [0x6261]


@pytest.mark.parametrize("conv", [0, 1])
@pytest.mark.parametrize("how", ["blob byte", "bridge moved", "tail_switch none",
                                 "restore moved", "restore_ret_at", "restore_up"])  # fmt: skip
def test_a_mismatch_is_refused_thin_and_regenerated_fat(conv: int, how: str) -> None:
    """Any convention (Q4 (b)): the running one and, on x86-64, the other one too. A tail_switch
    of 0xFFFFFFFF (the none value, out of v1 since S0016) is refused on both architectures."""
    native(JIT)
    if conv >= len(records(small(JIT))):
        pytest.skip("one convention on this architecture")

    def patch(b: bytes) -> bytes:
        at, _, start, *_ = r = records(b)[conv]
        if how == "blob byte":
            return put(b, text_at(b) + start + 5, bytes((b[text_at(b) + start + 5] ^ 1,)))
        if how == "bridge moved":
            return put(b, at + 12, struct.pack("<I", r[4] + 4))
        if how == "tail_switch none":
            return put(b, at + 16, struct.pack("<I", 0xFFFFFFFF))
        if how == "restore moved":
            return put(b, at + 20, struct.pack("<I", r[6] + 1))
        return put(b, at + (24 if how == "restore_ret_at" else 26), struct.pack("<H", 1))

    # S0016: the none value left v1, so 0xFFFFFFFF is refused as such, not as a mismatch
    both(patch, "no tail_switch: not v1" if how == "tail_switch none" else "trampolines differ")


def test_layout_is_strict(monkeypatch: pytest.MonkeyPatch) -> None:
    blob = small("jit-x86_64")
    (a0, _, s0, z0, *_), (a1, _, s1, *_) = records(blob)
    tx, gap = text_at(blob), s0 + z0
    for patched, why in [
        (put(blob, a0 - 4, struct.pack("<I", 1)), "not one record per convention"),
        (put(blob, a0, b"\2"), "convention 2 where the format has sysv"),
        (put(blob, a0 + 2, b"\1"), "reserved"),
        (put(blob, a0 + 4, struct.pack("<I", s0 + 1)), "first blob is not at a multiple of 16"),
        (put(blob, a1 + 4, struct.pack("<I", s1 + 16)), "not at the next multiple of 16"),
        (put(blob, tx + gap, b"\1"), "after zero bytes"),
        (put(blob, a0 + 12, struct.pack("<I", s0 + z0)), "not inside its blob"),
        (put(blob, a0 + 16, struct.pack("<I", 0xFFFFFFFE)), "not inside its blob"),
    ]:
        with pytest.raises(A.ArtifactError, match=f"waboros.trampolines: .*{why}"):
            A.read(patched)
    m = parse(SMALL)
    validate(m)
    c = base.TARGETS["jit-x86_64"]().compile(m)
    good = A._sections(c, None, "jit-x86_64")
    monkeypatch.setattr(A, "_sections", emits({**good, "text": good["text"] + bytes(16)}))
    with pytest.raises(A.ArtifactError, match="after the last trampoline"):
        A.read(A.write(c, target="jit-x86_64", module_sha256=bytes(32)))


def test_the_payload_has_the_layout_of_s0015() -> None:
    """40-byte records (the entry and `saved` in the file) are refused on the size, whatever
    their contents: 84 bytes for two conventions where the layout has 60."""
    blob = small("jit-x86_64")
    assert secs(blob)[".waboros.trampolines"][2] == 4 + 2 * REC == 60
    with pytest.raises(A.ArtifactError, match="the payload is 84 bytes, .* has 60"):
        A._trampolines(bytes(64), struct.pack("<I", 2) + bytes(80), "jit-x86_64")


def test_the_file_has_no_runtime_trampolines() -> None:
    """The entry and sp_stub are the runtime's (S0015): not in any `.text`, not in the symbols."""
    native(JIT)
    t: Any = base.TARGETS[JIT]()
    rt, (_, stub), _ = t.runtime()
    b = small(JIT)
    text = secs(b)[".text"]
    assert rt[:stub] not in b[text[1] : text[1] + text[2]]
    assert b"__waboros_entry_" not in b and b"sp_stub" not in b


H = '(import "env" "h" (func $h (param i32) (result i32)))'


def test_one_copy_per_store_from_the_first_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """A foreign host's store rule (S0014 `.waboros.trampolines`), run here: both instances on a
    copy sealed from the first artifact's blob; a tail call from A into B switches contexts
    through that copy's `tail_switch` ($t is entered by a call, so it is the chain's first hop)
    and the walk crosses its `restore` frame and its entry."""
    native(JIT)
    monkeypatch.setenv("WABOROS_JIT_WALK_CHECK", "1")
    t: Any = base.TARGETS[JIT]()
    store, seen = StoreState(), []
    texts = [f"""(module {H} (func $g (param i32) (result i32) (call $h (local.get 0)))
      (func (export "callee") (param i32) (result i32) (return_call $g (local.get 0))))""",
             """(module (import "b" "callee" (func $c (param i32) (result i32)))
      (func $t (param i32) (result i32) (return_call $c (local.get 0)))
      (func (export "tc") (param i32) (result i32) (call $t (local.get 0))))"""]  # fmt: skip
    mods, files = [], []
    for text in texts:
        m = parse(wat(text))
        validate(m)
        mods.append(m)
        files.append(A.write(t.compile(m), target=JIT, module_sha256=bytes(32)))
    run = A.CONVENTIONS[t.convention]
    _, _, start, size, *offs, ret_at, up = next(r for r in records(files[0]) if r[1] == run)
    copy = files[0][text_at(files[0]) + start :][:size]
    assert all(copy in f for f in files[1:])  # every later file carries the same bytes
    tramp = load_trampolines(t.runtime(), (copy, tuple(o - start for o in offs), (ret_at, up)))

    def first_copy(_: object) -> Any:
        return tramp

    monkeypatch.setattr(type(t), "trampolines", first_copy)

    def host(args: Any) -> list[int]:
        st = _stacks(store)
        segs = walk.walk(store, st)
        seen.append([[store.instances.index(f[0]) for f in fr] for fr, _ in segs])
        return [args[0]]

    b = Inst(mods[0], t.instantiate(A.load(files[0], target=JIT), link.resolve(mods[0], {
        "env": {"h": host}}), store))  # fmt: skip
    a = Inst(mods[1], t.instantiate(A.load(files[1], target=JIT), link.resolve(mods[1], {
        "b": link.Provider(b.mod, b.inst)}), store))  # fmt: skip
    for x in (a.inst, b.inst):
        ctx: Any = x.ctx  # type: ignore[attr-defined]
        assert (ctx[HOST_BRIDGE], ctx[TAIL_SWITCH]) == (tramp.bridge, tramp.tail_switch)
    # B's thunk and $g, then (over the restore frame) A's tc and its C entry (S0015; $g is this
    # compiler's, called at its body: D71)
    assert a("tc", 3) == [3] and seen == [[[0] * 2 + [1] * 2]]
    assert (
        ctypes.string_at(tramp.bridge, len(copy[: offs[1] - offs[0]])) == copy[: offs[1] - offs[0]]
    )
