# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportArgumentType=false, reportPrivateUsage=false
"""S0014 step 2: the bytes of each payload, every constant-expression operation, determinism, and a
reader that refuses a malformed file with a message (never another exception)."""

import hashlib
import shutil
import struct
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.artifact.test_roundtrip import norm
from tests.watcache import wat2wasm as wat
from waboros.frontend.binary import parse
from waboros.frontend.types import key
from waboros.frontend.validate import validate
from waboros.targets import base
from waboros.targets.jit.common import artifact as A
from waboros.targets.jit.common import gc

SMALL = wat("""(module (memory 1 2) (global (mut i32) (i32.const 7))
  (func (export "f") (param i32) (result i32) (i32.load (local.get 0)))
  (data (i32.const 8) "ab"))""")
SHA = hashlib.sha256(SMALL).digest()


def small(target: str = "jit-x86_64", wasm: bool = False) -> bytes:
    m = parse(SMALL)
    validate(m)
    c = base.TARGETS[target]().compile(m)
    return A.write(c, target=target, **({"wasm": SMALL} if wasm else {"module_sha256": SHA}))


@pytest.fixture(autouse=True)
def explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WABOROS_JIT_BOUNDS", "explicit")


def u32(*v: int) -> bytes:
    return b"".join(struct.pack("<I", x) for x in v)


def test_pinned_payloads() -> None:
    blob = small()
    s, mach, _ = A._sections_r(blob)
    assert blob[:9] == b"\x7fELF\2\1\1\xcb\1" and mach == 62
    assert struct.unpack_from("<Q", blob, 40)[0] % 8 == 0
    assert s["header"] == blob[64 : 64 + len(s["header"])]
    assert blob[4096 : 4096 + len(s["text"])] == s["text"]
    h = s["header"]  # sha, config sha, abi 1, 0, isa 1, 0, flags 0, 0, three u64 0, the compiler
    assert h[:32] == SHA and h[64:72] == b"\1\0\0\0\1\0\0\0" and h[72:80] == bytes(8)
    assert h[80:104] == bytes(24) and h[104:].endswith(b" jit-x86_64 0")
    assert h[104:108] == u32(len(h) - 108) and h[108:].startswith(b"waboros ")
    func = b"\1\0\0\0\x60" + u32(1) + b"\x7f" + u32(1) + b"\x7f"
    assert s["types"] == u32(1, 1) + func + u32(1) + key(parse_valid(), 0)
    assert s["imports"] == u32(0) and s["tables"] == u32(0) and s["tags"] == u32(0)
    ds = struct.unpack_from("<I", s["funcs"])[0]
    assert s["funcs"] == u32(ds, 0xFFFFFFFF, 1, 0, 0) and ds <= len(s["text"])
    assert s["memories"] == u32(1) + struct.pack("<BBHQQ", 1, 16, 0, 1, 2)
    assert s["globals"] == u32(1) + b"\x7f\1" + u32(6) + b"\x41\0" + u32(7)
    assert s["exports"] == u32(1, 1) + b"f\0" + u32(0)
    off = u32(6) + b"\x41\0" + u32(8)
    assert s["segments"] == u32(0, 1) + b"\0" + u32(0) + off + u32(2) + b"ab"
    assert s["context"] == b"\0\0\0\0" + u32(0, 0, 0, 0, 0)
    assert s["traps"] == u32(0)  # one site: the C entry's call into the body (S0015)
    assert list(gc.decode(s["stackmaps"]).values()) == [(0, 40, 48, 0, 0, 0)]


def parse_valid() -> dict[str, Any]:
    m = parse(SMALL)
    validate(m)
    return m


def test_guard_flags_and_fault_map(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WABOROS_JIT_BOUNDS", "guard")
    s, _, _ = A._sections_r(small())
    assert struct.unpack_from("<I", s["header"], 72)[0] == 1 | 25 << 8
    assert struct.unpack_from("<I", s["traps"])[0] > 0


def test_arm64_machine_and_mapping_symbols() -> None:
    blob = small("jit-aarch64")
    assert A._sections_r(blob)[1] == 183 and b"\0$x\0" in blob and b"\0wasm[0]\0" in blob


CEXPR = """(module (type $s (struct (field i32) (field (mut i64)))) (type $a (array (mut i32)))
  (import "m" "g" (global i32)) (import "m" "g64" (global i64)) (func $f) (elem declare func $f)
  (global i32 (i32.const -5)) (global i64 (i64.const 1234567890123)) (global f32 (f32.const 1.5))
  (global f64 (f64.const -2.25)) (global v128 (v128.const i32x4 1 2 3 4))
  (global i32 (global.get 0))
  (global (ref func) (ref.func $f)) (global funcref (ref.null func))
  (global i32 (i32.mul (i32.add (global.get 0) (i32.const 1)) (i32.sub (i32.const 9)
    (i32.const 3))))
  (global i64 (i64.mul (i64.add (global.get 1) (i64.const 1)) (i64.sub (i64.const 9)
    (i64.const 3))))
  (global (ref i31) (ref.i31 (i32.const 3)))
  (global anyref (any.convert_extern (ref.null extern)))
  (global externref (extern.convert_any (ref.i31 (i32.const 1))))
  (global (ref $s) (struct.new $s (i32.const 1) (i64.const 2)))
  (global (ref $s) (struct.new_default $s))
  (global (ref $a) (array.new $a (i32.const 1) (i32.const 3)))
  (global (ref $a) (array.new_default $a (i32.const 3)))
  (global (ref $a) (array.new_fixed $a 2 (i32.const 1) (i32.const 2))))"""
OPS = [0x41, 0x42, 0x43, 0x44, 0xFD0C, 0x23, 0xD2, 0xD0, 0x6A, 0x6B, 0x6C, 0x7C, 0x7D, 0x7E,
       0xFB1C, 0xFB1A, 0xFB1B, 0xFB00, 0xFB01, 0xFB06, 0xFB07, 0xFB08]  # fmt: skip


def test_every_constant_expression_operation() -> None:
    m: dict[str, Any] = parse(wat(CEXPR))
    validate(m)
    gt = [0x7F, 0x7E, *(t for t, _, _ in m["globals"])]  # two imported globals, then the defined
    env = A._Env(m["tdefs"], len(m["tdefs"]), nfuncs=1, nglobals=len(gt), records=[0], gtypes=gt)
    env.rset = frozenset([0])
    env.ftidx = m["funcs"]
    env.gmut = [0] * len(gt)
    seen: set[int] = set()
    for t, _, e in m["globals"]:
        b = A._cexpr(e)
        assert struct.unpack_from("<I", b)[0] == len(b) - 4
        r = A._R(b, "test")
        assert norm(A._cexpr_r(r, env, t)) == norm(e)  # and the type is the global's
        r.done()
        ops, p = [], 4  # the operation codes: walk the postfix bytes by their immediates
        while p < len(b):
            op = struct.unpack_from("<H", b, p)[0]
            ops.append(op)
            p += 2 + {0x41: 4, 0x43: 4, 0x42: 8, 0x44: 8, 0xFD0C: 16, 0x23: 4, 0xD2: 4, 0xFB00: 4,
                      0xFB01: 4, 0xFB06: 4, 0xFB07: 4, 0xFB08: 8}.get(op, 0)  # fmt: skip
        seen |= set(ops)
    assert seen == set(OPS)
    assert A._cexpr((("i32", 7),)) == u32(6) + b"\x41\0" + u32(7)
    assert A._cexpr((("v128", 1),)) == u32(18) + b"\x0c\xfd" + b"\1" + bytes(15)
    assert A._cexpr((*[("i32", 1)] * 3, ("gc", (0xFB08, 2, 3), 3))).endswith(
        b"\x08\xfb" + u32(2, 3)
    )
    assert A._cexpr((("null", -16),)) == u32(2) + b"\xd0\0"


def test_constant_expression_checks() -> None:
    env = A._Env([(("struct", [(0x7F, 0)]), (), True, 0, 1)], 1, nfuncs=2, nglobals=1)
    for body, why in (
        (b"\x41\0" + u32(1) + b"\x42\0" + u32(1, 0), "exactly one"),  # two values
        (b"\x6a\0", "underflows"), (b"", "exactly one"), (b"\x23\0" + u32(1), "out of range"),
        (b"\xd2\0" + u32(2), "out of range"), (b"\xff\xff", "unknown operation"),
        (b"\x06\xfb" + u32(0), "not a array"), (b"\x41\0", "truncated"),
        (b"\x00\xfb" + u32(0), "underflows"),  # struct.new wants a field
    ):  # fmt: skip
        with pytest.raises(A.ArtifactError, match=why):
            A._cexpr_r(A._R(u32(len(body)) + body, "t"), env)


def test_written_twice_identical() -> None:
    assert small() == small() and small(wasm=True) == small(wasm=True)


def test_writer_refuses_code_changing_switches(monkeypatch: pytest.MonkeyPatch) -> None:
    m = parse(SMALL)
    validate(m)
    c = base.TARGETS["jit-x86_64"]().compile(m)
    for name in A.SWITCHES:
        monkeypatch.setenv(name, "all")
        with pytest.raises(ValueError, match=name):
            A.write(c, target="jit-x86_64", module_sha256=SHA)
        monkeypatch.delenv(name)
    with pytest.raises(ValueError, match="module_sha256"):
        A.write(c, target="jit-x86_64")


# ---------------------------------------------------------------------------- strictness
def secs(blob: bytes) -> dict[str, tuple[int, int, int]]:
    """name -> (header offset, data offset, size) of every section."""
    shoff, shnum = struct.unpack_from("<QH", blob, 40)[0], struct.unpack_from("<H", blob, 60)[0]
    h = [struct.unpack_from("<IIQQQQIIQQ", blob, shoff + 64 * i) for i in range(shnum)]
    names = blob[h[-1][4] : h[-1][4] + h[-1][5]]
    return {
        names[x[0] : names.index(b"\0", x[0])].decode(): (shoff + 64 * i, x[4], x[5])
        for i, x in enumerate(h)
        if i
    }


def put(blob: bytes, at: int, new: bytes) -> bytes:
    return blob[:at] + new + blob[at + len(new) :]


def test_every_prefix_is_refused() -> None:
    blob = small(wasm=True)
    for n in range(len(blob)):
        with pytest.raises(A.ArtifactError):
            A.read(blob[:n])


def emits(sections: dict[str, bytes]) -> Callable[..., dict[str, bytes]]:
    """A stand-in for `A._sections` that makes the writer lay out `sections`."""

    def f(*_: Any) -> dict[str, bytes]:
        return sections

    return f


def test_trailing_bytes_in_each_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """A payload one byte longer, in a file laid out as the writer would (the writer is made to
    emit it), is refused naming the section."""
    m = parse(SMALL)
    validate(m)
    c = base.TARGETS["jit-x86_64"]().compile(m)
    good = A._sections(c, SMALL, "jit-x86_64")
    for name in good:
        if name in ("wasm", "text", "header"):
            continue
        monkeypatch.setattr(A, "_sections", emits({**good, name: good[name] + b"\0"}))
        with pytest.raises(A.ArtifactError, match=f"waboros.{name}"):
            A.read(A.write(c, target="jit-x86_64", wasm=SMALL))


@pytest.mark.parametrize(
    ("section", "at", "new", "why"),
    [
        (".waboros.header", 66, b"\1", "reserved"),
        (".waboros.header", 76, b"\1", "reserved"),
        (".waboros.funcs", 12, u32(9), "type index 9 out of range"),
        (".waboros.funcs", 4, u32(5), "start function 5"),
        (".waboros.exports", 9, b"\7", "bad export kind"),
        (".waboros.exports", 10, u32(3), "index 3 out of range"),
        (".waboros.memories", 5, b"\x11", "bad page size"),
        (".waboros.memories", 4, b"\x04", "not supported"),
        (".waboros.memories", 6, b"\1", "reserved"),
        (".waboros.globals", 5, b"\7", "mutability"),
        (".waboros.globals", 4, b"\x70", "bad value type"),
        (".waboros.segments", 4, u32(9), "count 9"),
        (".waboros.types", 10, b"\1", "reserved"),
        (".waboros.types", 17, b"\x7e", "canonical key"),
        (".waboros.context", 3, b"\1", "reserved"),
        (".waboros.context", 0, b"\2", "not 0 or 1"),
        (".waboros.traps", 0, u32(1), "count 1 is past"),
    ],
)  # fmt: skip
def test_malformed_payloads(section: str, at: int, new: bytes, why: str) -> None:
    blob = small()
    _, data, _ = secs(blob)[section]
    with pytest.raises(A.ArtifactError, match=why):
        A.read(put(blob, data + at, new))


def test_container_refusals() -> None:
    blob, fat = small(), small(wasm=True)
    for at, new, why in ((0, b"\x7fELG", "not a waboros"), (7, b"\xc8", "not a waboros"),
                         (8, b"\0", "format version 0"), (8, b"\2", "format version 2"),
                         (9, b"\1", "padding"), (18, b"\x3e\1", "machine"), (16, b"\2", "field"),
                         (40, struct.pack("<Q", 1 << 40), "inside the file")):  # fmt: skip
        with pytest.raises(A.ArtifactError, match=why):
            A.read(put(blob, at, new))
    s = secs(blob)
    sh = s[".waboros.tags"][0]
    with pytest.raises(A.ArtifactError, match="relocation"):
        A.read(put(blob, sh + 4, u32(4)))  # SHT_RELA
    names = secs(blob)[".shstrtab"][1]
    at = names + blob[names : names + secs(blob)[".shstrtab"][2]].index(b".waboros.tags")
    with pytest.raises(A.ArtifactError, match="missing: tags"):
        A.read(put(blob, at, b".waboros.tagz"))
    with pytest.raises(A.ArtifactError, match="twice"):  # the name of .waboros.types for tags
        A.read(put(blob, sh, blob[s[".waboros.types"][0] : s[".waboros.types"][0] + 4]))
    at = names + blob[names:].index(b".strtab")
    with pytest.raises(A.ArtifactError, match="missing: strtab"):  # renamed: now an unknown one
        A.read(put(blob, at, b".future"))
    wasm_at = secs(fat)[".waboros.wasm"][1]
    with pytest.raises(A.ArtifactError, match="SHA-256"):
        A.read(put(fat, wasm_at + 9, b"\xff"))


@pytest.mark.skipif(not shutil.which("readelf"), reason="no readelf")
def test_readelf_reads_without_warning(tmp_path: Path) -> None:
    f = tmp_path / "small.wbo"
    f.write_bytes(small(wasm=True))
    p = subprocess.run(["readelf", "-aW", str(f)], capture_output=True, text=True, check=False)
    assert p.returncode == 0 and "arning" not in p.stdout + p.stderr and "rror" not in p.stderr


def build(text: str) -> bytes:
    m = parse(wat(text))
    validate(m)
    c = base.TARGETS["jit-x86_64"]().compile(m)
    return A.write(c, target="jit-x86_64", module_sha256=SHA)


def test_element_index_without_a_record_is_refused() -> None:
    blob = build("(module (table 1 funcref) (func $a) (func $b) (elem (i32.const 0) $a))")
    _, data, size = secs(blob)[".waboros.segments"]
    assert A.read(blob)  # $a has a record, $b has none
    with pytest.raises(A.ArtifactError, match="function 1 has no record"):
        A.read(put(blob, data + size - 8, u32(1)))


def test_global_initializer_reads_only_imported_and_earlier_globals() -> None:
    blob = build('(module (import "m" "g" (global i32)) (global i32 (i32.const 1)))')
    _, data, _ = secs(blob)[".waboros.globals"]
    assert A.read(put(blob, data + 10, b"\x23\0" + u32(0)))  # the imported global
    for k in (1, 2):  # itself, and past the end
        with pytest.raises(A.ArtifactError, match="global index"):
            A.read(put(blob, data + 10, b"\x23\0" + u32(k)))


def test_compiler_string_carries_the_targets_own_version() -> None:
    v = A.read(small())
    assert v.target_version == base.TARGETS["jit-x86_64"]().version
