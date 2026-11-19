# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportArgumentType=false, reportPrivateUsage=false
"""S0014 step 2.1: the reader accepts exactly the files the writer makes. The section header table,
the symbols and `config_sha256` pinned; one refusal per container rule and per constant-expression
typing rule; a byte-flip loop that checks `write(read(b)) == b` on whatever is accepted."""

import hashlib
import random
import struct
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tests.artifact.test_format import SMALL, build, emits, explicit, put, secs, small, u32
from tests.artifact.test_roundtrip import compile_for
from tests.watcache import wat2wasm as wat
from waboros.frontend.binary import parse
from waboros.frontend.validate import validate
from waboros.targets import base
from waboros.targets.jit.common import artifact as A

ROOT = Path(__file__).resolve().parents[2]
_ = explicit  # the autouse fixture of test_format, imported so that it applies here too
SH = struct.Struct("<IIQQQQIIQQ")  # name type flags addr offset size link info align entsize
F = {"type": 4, "flags": 8, "addr": 16, "offset": 24, "size": 32, "link": 40, "info": 44,
     "align": 48, "entsize": 56}  # fmt: skip


def heads(blob: bytes) -> list[tuple[int, ...]]:
    shoff, n = struct.unpack_from("<QH", blob, 40)[0], struct.unpack_from("<H", blob, 60)[0]
    return [SH.unpack_from(blob, shoff + 64 * i) for i in range(n)]


def with_section(blob: bytes, name: str, data: bytes, before: str, align: int = 8) -> bytes:
    """`blob` with an unknown section inserted before `before`, laid out as the writer would."""
    h, names = heads(blob), list(secs(blob))
    parts = [(n, h[i + 1][1:2] + h[i + 1][2:3] + h[i + 1][8:10], blob[h[i + 1][4] :][: h[i + 1][5]])
             for i, n in enumerate(names)]  # fmt: skip
    # (name, (type, flags, align, entsize), data); link and info are rebuilt below
    info = {n: h[i + 1][7] for i, n in enumerate(names)}
    at = names.index(before)
    parts.insert(at, (name, (1, 0, align, 0), data))
    names.insert(at, name)
    names_tab = bytearray(b"\0")
    so = {}
    for n in names:
        so[n] = len(names_tab)
        names_tab += n.encode() + b"\0"
    out, new = bytearray(blob[:64]), [bytes(64)]
    for n, (typ, fl, al, ent), d in parts:
        if n == ".shstrtab":
            d = bytes(names_tab)
        out += bytes(-len(out) % al)
        link = names.index(".strtab") + 1 if n == ".symtab" else 0
        new.append(SH.pack(so[n], typ, fl, 0, len(out), len(d), link, info.get(n, 0), al, ent))
        out += d
    out += bytes(-len(out) % 8)
    struct.pack_into("<Q", out, 40, len(out))
    struct.pack_into("<HHH", out, 58, 64, len(new), len(new) - 1)
    return bytes(out) + b"".join(new)


def test_unknown_sections_are_skipped_not_interpreted() -> None:
    blob = small(wasm=True)
    for before in (".waboros.types", ".symtab", ".strtab", ".shstrtab", ".waboros.wasm"):
        for align in (1, 8, 64):
            extra = with_section(blob, ".waboros.future", b"\xff" * 13, before, align)
            assert A.read(extra).module["funcs"] == [0]  # the module is read as before
            assert A.write(A.read(extra), target="jit-x86_64", wasm=SMALL) == blob  # not carried
    ok = with_section(blob, ".note.GNU-stack", b"", ".shstrtab", 1)
    assert A.read(ok).module["funcs"] == [0]
    with pytest.raises(A.ArtifactError, match="order"):
        A.read(with_section(blob, ".x", b"1", ".waboros.header"))
    extra = with_section(blob, ".x", b"1234567", ".waboros.types", 8)
    for field, value, why in (("offset", 0, "unknown section"), ("offset", 64, "unknown section"),
                              ("offset", secs(extra)[".text"][1] + 8, "unknown section"),
                              ("align", 3, "unknown section"), ("addr", 8, "unknown section"),
                              ("size", len(extra), "inside the file")):  # fmt: skip
        with pytest.raises(A.ArtifactError, match=why):
            A.read(patch(extra, ".x", field, value))


def test_unknown_section_before_text_is_refused() -> None:
    blob = small(wasm=True)
    with pytest.raises(A.ArtifactError, match="order"):
        A.read(with_section(blob, ".x", b"1", ".text"))
    nobits = with_section(blob, ".x", b"1234", ".strtab")  # SHT_NOBITS takes file space too
    assert A.read(patch(nobits, ".x", "type", 8))


def test_table_initial_value_reads_imported_globals_only() -> None:
    text = """(module (import "m" "g" (global funcref)) (global funcref (ref.null func))
              (table 1 funcref (global.get 0)))"""
    # tables payload: count, type (5), limits (20), has_init, length, opcode, index
    assert A.read(build(text))
    mod_patch(text, "tables", 4 + 5 + 20 + 1 + 4 + 2, u32(1), "global index 1 out of range")


def test_non_nullable_table_needs_an_initial_value() -> None:
    text = "(module (func $f) (table 1 (ref func) (ref.func $f)))"
    mod_patch(text, "tables", 4 + 5 + 20, b"\0", "needs an initial value")


def test_global_get_of_a_mutable_global_is_refused() -> None:
    # the validator refuses such a module, so the mutability byte of the import is patched
    text = '(module (import "m" "g" (global i32)) (global i32 (global.get 0)))'
    mod_patch(text, "imports", 4 + 5 + 5 + 1 + 1, b"\1", "global 0 is mutable")


def test_section_header_table_pinned() -> None:
    blob = small(wasm=True)
    h = heads(blob)
    s, _, _ = A._sections_r(blob)
    names = [".waboros.header", ".text"] + [
        f".waboros.{n}" for n in A.ORDER[2:] if n != "trampolines"
    ]
    names += [".symtab", ".strtab", ".shstrtab"]
    assert len(h) == len(names) + 1 and h[0] == (0,) * 10
    shstr = blob[h[-1][4] : h[-1][4] + h[-1][5]]
    pos = 64
    for i, (n, x) in enumerate(zip(names, h[1:], strict=True), 1):
        assert shstr[x[0] : shstr.index(b"\0", x[0])] == n.encode()
        typ, fl, al, link, info, ent = (1, 0, 8, 0, 0, 0)
        if n == ".text":
            typ, fl, al = 1, 2 | 4, 4096  # SHF_ALLOC | SHF_EXECINSTR
        if n == ".symtab":  # null + wasm[0]; link = .strtab
            typ, link, info, ent = 2, i + 1, 2, 24
        if n in (".strtab", ".shstrtab"):
            typ, al = 3, 1
        pos += -pos % al
        assert x[1:] == (typ, fl, 0, pos, x[5], link, info, al, ent), n
        key = n[9:] if n.startswith(".waboros.") else n[1:]
        assert key == "shstrtab" or blob[pos : pos + x[5]] == s[key]
        pos += x[5]
    assert h[1][4] == 64 and h[2][4] == 4096
    shoff = struct.unpack_from("<Q", blob, 40)[0]
    assert shoff == pos + -pos % 8 and len(blob) == shoff + 64 * len(h)
    assert struct.unpack_from("<HHH", blob, 58) == (64, len(h), len(h) - 1)


def symbols(blob: bytes) -> list[tuple[str, int, int, int, int, int]]:
    h = heads(blob)
    sym, st = h[-3], h[-2]
    names = blob[st[4] : st[4] + st[5]]
    out = []
    for o in range(sym[4], sym[4] + sym[5], 24):
        n, info, other, shndx, value, size = struct.unpack_from("<IBBHQQ", blob, o)
        out.append((names[n : names.index(b"\0", n)].decode(), info, other, shndx, value, size))
    return out


def test_symbols_pinned() -> None:
    imp = build('(module (import "m" "f" (func)) (func (export "g")) (func))')
    s, _, _ = A._sections_r(imp)
    ds, n = struct.unpack_from("<II", s["funcs"], 0)[0], struct.unpack_from("<I", s["funcs"], 8)[0]
    code = [struct.unpack_from("<II", s["funcs"], 12 + 8 * i)[1] for i in range(n)]
    thunk = struct.unpack_from("<I", s["context"], 4 + 4 + 4 * 1 + 4 + 4)[
        0
    ]  # after 1 record, 0 sigs
    ends = sorted([*code, thunk, ds])
    want = [("", 0, 0, 0, 0, 0)]
    want += [
        (f"wasm[{1 + i}]", 2, 0, 2, o, ends[ends.index(o) + 1] - o) for i, o in enumerate(code)
    ]
    want += [("thunk[0]", 2, 0, 2, thunk, ends[ends.index(thunk) + 1] - thunk)]
    assert symbols(imp) == want
    a = symbols(small("jit-aarch64"))  # the mapping symbols of AAELF64: $x at 0, $d at data_start
    assert [x[0] for x in a] == ["", "wasm[0]", "$x"] + ["$d"] * (len(a) == 4)
    assert a[2] == ("$x", 0, 0, 2, 0, 0)
    assert [x[0] for x in symbols(small())] == ["", "wasm[0]"]


def test_config_sha256_is_the_specs_definition(monkeypatch: pytest.MonkeyPatch) -> None:
    def cfg(blob: bytes) -> bytes:
        return A._sections_r(blob)[0]["header"][32:64]

    def lines(**kv: str) -> bytes:  # "name=value\n" sorted by name, one per option
        return hashlib.sha256(
            "".join(f"{k}={v}\n" for k, v in sorted(kv.items())).encode()
        ).digest()

    assert cfg(small()) == lines(bounds="explicit", guard_log2="0", isa="x86-64-v1",
                                 target="jit-x86_64")  # fmt: skip
    assert cfg(small("jit-aarch64")) == lines(bounds="explicit", guard_log2="0", isa="armv8.0-a",
                                              target="jit-aarch64")  # fmt: skip
    monkeypatch.setenv("WABOROS_JIT_BOUNDS", "guard")
    assert cfg(small()) == lines(bounds="guard", guard_log2="25", isa="x86-64-v1",
                                 target="jit-x86_64")  # fmt: skip


# ----------------------------------------------------------------------------- the container
def patch(blob: bytes, section: str, field: str, value: int) -> bytes:
    at = secs(blob)[section][0] + F[field]
    return put(
        blob,
        at,
        struct.pack("<Q", value)[
            : 8 if field in ("flags", "addr", "offset", "size", "align", "entsize") else 4
        ],
    )


def test_container_rules() -> None:
    blob = small(wasm=True)
    s = secs(blob)
    text, tags = s[".text"], s[".waboros.tags"]
    shoff = struct.unpack_from("<Q", blob, 40)[0]
    bad: dict[str, tuple[bytes, str]] = {
        "overlap, the previous section": (patch(blob, ".waboros.types", "offset", text[1]),
                                          "not the format's"),
        "overlap, the ELF header": (patch(blob, ".waboros.header", "offset", 0), "not the format"),
        "overlap, the section headers": (patch(blob, ".shstrtab", "size", len(blob)),
                                         "inside the file"),
        "a byte after the section headers": (blob + b"\0", "after the section header table"),
        "a gap byte": (put(blob, 4095, b"\1"), "padding"),
        "nobits payload": (patch(blob, ".waboros.types", "type", 8), "not the format's"),
        "nobits text": (patch(blob, ".text", "type", 8), "not the format's"),
        "shstrtab not strtab": (patch(blob, ".shstrtab", "type", 1), "not the format's"),
        "strtab not strtab": (patch(blob, ".strtab", "type", 1), "not the format's"),
        "symtab not symtab": (patch(blob, ".symtab", "type", 3), "not the format's"),
        "text flags": (patch(blob, ".text", "flags", 6 | 1), "not the format's"),
        "alignment": (patch(blob, ".waboros.tags", "align", 16), "not the format's"),
        "text alignment": (patch(blob, ".text", "align", 8), "not the format's"),
        "address": (patch(blob, ".waboros.tags", "addr", 8), "not the format's"),
        "link": (patch(blob, ".waboros.tags", "link", 1), "not the format's"),
        "info": (patch(blob, ".waboros.tags", "info", 1), "not the format's"),
        "entsize": (patch(blob, ".waboros.tags", "entsize", 4), "not the format's"),
        "symtab link": (patch(blob, ".symtab", "link", 1), "not the format's"),
        "symtab entsize": (patch(blob, ".symtab", "entsize", 16), "not the format's"),
        "null header": (put(blob, shoff + 40, b"\1"), "null section"),
        "sections swapped": (  # the headers of tags and exports exchanged
            put(put(blob, tags[0], blob[s[".waboros.exports"][0] : s[".waboros.exports"][0] + 64]),
                s[".waboros.exports"][0], blob[tags[0] : tags[0] + 64]), "order"),
    }  # fmt: skip
    for b, why in bad.values():
        with pytest.raises(A.ArtifactError, match=why):
            A.read(b)


def same(a: A.ModuleView, b: A.ModuleView) -> bool:
    """Equal views, except for the header fields the writer regenerates (step 4 checks them)."""
    keep = dict(config_sha256=b"", compiler="", target_version=0, ctx_abi_version=0, isa_level=0,
                isa_features=0, features_required=0, features_optional=0)  # fmt: skip
    return replace(a, **keep) == replace(b, **keep)


def canonical(blob: bytes) -> bytes:
    v = A.read(blob)
    out = A.write(v, target=v.target, module_sha256=v.module_sha256, wasm=v.wasm)
    assert same(A.read(out), v)  # the canonical form reads back to an equal view
    return out


def test_what_the_reader_ignores_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """D62: no loader reads `.waboros.*` flags, `.symtab` or `.strtab`; a file that differs from
    the writer's only there reads, and rewriting it gives the writer's file."""
    blob = small(wasm=True)
    s = secs(blob)
    assert canonical(blob) == blob
    assert canonical(patch(blob, ".waboros.tags", "flags", 2)) == blob  # a flag nobody reads
    for name in (".symtab", ".strtab"):  # a flipped byte of the symbols and of their names
        _, at, size = s[name]
        assert canonical(put(blob, at + size - 2, bytes((blob[at + size - 2] ^ 1,)))) == blob
    assert canonical(patch(blob, ".symtab", "info", 1)) == blob  # first non-local symbol
    extra = A._symtab

    def more(c: Any, mach: int, nimp: int) -> tuple[bytes, bytes]:  # one more symbol, and a name
        sym, tab = extra(c, mach, nimp)
        return sym + struct.pack("<IBBHQQ", len(tab), 0x12, 0, 2, 0, 0), tab + b"added\0"

    monkeypatch.setattr(A, "_symtab", more)
    longer = small(wasm=True)
    assert longer != blob and A.read(longer) == A.read(blob)
    monkeypatch.undo()
    assert canonical(longer) == blob


# ----------------------------------------------------------- constant expression typing
def mod_patch(text: str, section: str, at: int, new: bytes, why: str) -> None:
    blob = build(text)
    assert A.read(blob)
    _, data, _ = secs(blob)[f".waboros.{section}"]
    with pytest.raises(A.ArtifactError, match=why):
        A.read(put(blob, data + at, new))


F32C, FUNC_T, EXT_T = b"\x43\0", b"\x63" + struct.pack("<i", -16), struct.pack("<i", -17)


def test_cexpr_types_against_their_use() -> None:
    # a global of i32 initialised with an f32.const (the opcode byte only: the same width)
    mod_patch("(module (global i32 (i32.const 7)))", "globals", 10, F32C, "does not fit")
    # an operand of i32.add that is not an i32
    mod_patch("(module (global i32 (i32.add (i32.const 1) (i32.const 2))))", "globals", 10, F32C,
              "wrong type")  # fmt: skip
    # global.get of an i32 global into a global declared i64
    mod_patch('(module (import "m" "g" (global i32)) (global i32 (global.get 0)))', "globals", 4,
              b"\x7e", "does not fit")  # fmt: skip
    # a table of funcref whose element type is changed to externref under a ref.func initialiser
    mod_patch("(module (func $f) (table 1 funcref (ref.func $f)))", "tables", 5, EXT_T,
              "does not fit")  # fmt: skip
    # an active segment's offset of the wrong type for a table, and for a memory
    elem = "(module (table 1 funcref) (func $f) (elem (i32.const 0) $f))"
    mod_patch(elem, "segments", 4 + 1 + 5 + 4 + 4, F32C[:1], "does not fit")
    data = '(module (memory 1) (data (i32.const 8) "ab"))'
    mod_patch(data, "segments", 4 + 4 + 1 + 4 + 4, F32C[:1], "does not fit")
    # a 64-bit memory (flags: bit 1 cleared) addressed by an i64.const offset
    mod_patch(
        '(module (memory i64 1) (data (i64.const 8) "ab"))', "memories", 4, b"\0", "does not fit"
    )
    # an element item of externref type under a funcref segment (type of the segment changed)
    items = "(elem (i32.const 0) funcref (ref.null func) (ref.func $f))"
    mod_patch(f"(module (table 2 funcref) (func $f) {items})", "segments", 6, EXT_T, "does not fit")
    # a segment whose element type is not the table's
    mod_patch("(module (table 2 funcref) (func $f) (elem (i32.const 0) $f))", "segments", 6, EXT_T,
              "does not fit")  # fmt: skip


def test_items_1_with_only_function_indices_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    m = parse(wat("(module (table 1 funcref) (func $f) (elem (i32.const 0) $f))"))
    validate(m)
    c = base.TARGETS["jit-x86_64"]().compile(m)
    good = A._sections(c, None)
    seg = u32(1) + b"\0" + FUNC_T + u32(0) + A._cexpr(("i32", 0)) + b"\1" + u32(1)
    seg += A._cexpr(("func", 0)) + u32(0)
    assert good["segments"] != seg
    monkeypatch.setattr(A, "_sections", emits({**good, "segments": seg}))
    with pytest.raises(A.ArtifactError, match="writer uses items 0"):
        A.read(A.write(c, target="jit-x86_64", module_sha256=bytes(32)))


# ---------------------------------------------------------------------------- the byte-flip loop
GC = """(type $s (struct (field i32) (field (mut i64)))) (type $a (array (mut i32)))
  (import "m" "g" (global i32)) (func $f) (elem declare func $f) (table 2 funcref (ref.func $f))
  (global (ref $s) (struct.new $s (global.get 0) (i64.const 2)))
  (global (ref $a) (array.new_fixed $a 2 (i32.const 1) (i32.const 2)))
  (global (ref i31) (ref.i31 (i32.const 3))) (memory 1) (data (i32.const 8) "ab")"""


def payloads(blob: bytes) -> dict[str, bytes]:
    """The payloads that carry the module: all but the symbols and the regenerated header fields."""
    out = A._sections_r(blob)[0]
    out["header"] = out["header"][:32]  # module_sha256: frozen, never regenerated
    return {k: v for k, v in out.items() if k not in ("symtab", "strtab")}


def flip_loop(wasm: bytes, n: int, seed: int, monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Flip bytes of a file, read what is accepted and classify it: written back to the same bytes
    (`exact`); differing only in a place the reader ignores (`ignored`: `.waboros.*` flags, the
    symbols, the header's regenerated fields; every payload that carries the module is equal in the
    flipped and the rewritten file, and the rewritten file reads back to an equal view); an
    unknown section (`unknown`: a flip renamed one; not carried); or `bad`, which must be 0."""
    c = compile_for(wasm, "jit-x86_64", "guard", monkeypatch)
    blob = A.write(c, target="jit-x86_64", wasm=wasm)
    code, hdr = len(A.read(blob).code), range(96, 64 + len(A._sections_r(blob)[0]["header"]))
    rnd = random.Random(seed)
    meta = [i for i in range(len(blob)) if not 4096 <= i < 4096 + code]
    r: dict[str, int] = dict.fromkeys(
        ("flips", "accepted", "exact", "ignored", "unwritable", "unknown", "bad"), 0
    )
    for k in range(n):
        at = rnd.choice(meta) if k % 4 else rnd.randrange(4096, 4096 + code)
        b = bytearray(blob)
        b[at] ^= 1 << rnd.randrange(8) if k % 2 else rnd.randrange(1, 256)
        r["flips"] += 1
        try:
            v = A.read(bytes(b))
        except A.ArtifactError:
            continue
        r["accepted"] += 1
        if any(x not in A.NAMES for x in secs(bytes(b))):
            r["unknown"] += 1
            continue
        try:
            out = A.write(v, target=v.target, module_sha256=v.module_sha256, wasm=v.wasm)
        except ValueError:  # a flipped header flags field, which step 4 refuses
            r["unwritable" if at in hdr else "bad"] += 1
            continue
        if out == bytes(b):
            r["exact"] += 1
        elif same(A.read(out), v) and payloads(out) == payloads(bytes(b)):
            r["ignored"] += 1
        else:
            r["bad"] += 1
    return r


@pytest.mark.parametrize(
    ("what", "iterations"), [("coremark", 3000), ("gc", 3000), ("small", 3000)]
)
def test_byte_flips_never_break_the_round_trip(
    what: str, iterations: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    wasm = {"coremark": lambda: (ROOT / "poc/in/core.wasm").read_bytes(),
            "gc": lambda: wat(f"(module {GC})"), "small": lambda: SMALL}[what]()  # fmt: skip
    r = flip_loop(wasm, iterations, 12345, monkeypatch)
    print(f"{what}: {r}")
    assert (
        r["bad"] == 0
        and r["accepted"] == r["exact"] + r["ignored"] + r["unwritable"] + r["unknown"]
    )
