# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportArgumentType=false, reportPrivateUsage=false, reportUnknownLambdaType=false
"""The refusal corpus of S0017 ("The harness"): the whole-file cases of
`tests/artifact/test_refuse.py`, `test_strict.py`, `test_review.py` (and the table of
`test_format.py::test_malformed_payloads`) rebuilt as files, with the files those tests make under
`monkeypatch` (a patched `compiler_string`, guard mode, a patched writer); the truncations of the
committed small file and of a GC module; the byte flips of CoreMark, a GC module and the small
module (the seed of `test_strict`'s loop); the committed files; and accepted modules for the dump.
Each case records its source: the unmutated file it was made from (S0017 class E2)."""

import random
import struct
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.artifact.test_format import CEXPR, SHA, SMALL, emits, put, secs, small, u32
from tests.artifact.test_refuse import ABI, FEATS, FLAGS, ISA, JIT, OPT, OTHER, REQ
from tests.artifact.test_roundtrip import HAND, compile_for
from tests.artifact.test_steps import CASES as STEPS
from tests.artifact.test_strict import GC, patch, with_section
from tests.spec import fetch
from tests.watcache import wat2wasm as wat
from waboros.frontend import types as T
from waboros.frontend.binary import parse
from waboros.frontend.validate import validate
from waboros.targets import base
from waboros.targets.jit.common import artifact as A

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "tests" / "artifact" / "data"
FLIPS: dict[str, Callable[[], bytes]] = {
    "coremark": lambda: (ROOT / "poc/in/core.wasm").read_bytes(),
    "gc": lambda: wat(f"(module {GC})"),
    "small": lambda: SMALL,
}
H = ".waboros.header"


@dataclass
class Case:
    name: str
    data: bytes
    target: str  # the target `artifact.load` is given
    source: bytes | None  # the unmutated file this was made from


def bld(text: str) -> bytes:
    m = parse(wat(text))
    validate(m)
    return A.write(base.TARGETS[JIT]().compile(m), target=JIT, module_sha256=SHA)


def fat_file(wasm: bytes) -> bytes:
    with pytest.MonkeyPatch.context() as mp:
        return A.write(compile_for(wasm, JIT, "explicit", mp), target=JIT, wasm=wasm)


def _all(f: bytes, patches: list[tuple[int, bytes]]) -> bytes:
    for at, d in patches:
        f = put(f, at, d)
    return f


def _header_cases(add: Callable[..., None]) -> None:
    """test_refuse, test_review (tokens), test_steps: the header, the version, the machine, the
    target, the section table offset, the types before the compatibility check, the fat wasm."""
    thin, fat = small(JIT), small(JIT, wasm=True)
    p = struct.pack
    for kind, f in (("thin", thin), ("fat", fat)):
        fields = [(0, b"X"), (7, b"\0"), (8, b"\0"), (8, b"\2"), (ABI, p("<H", 2)),
                  (ISA, p("<H", 3)), (ISA, p("<H", 0)), (FEATS, p("<Q", 1 << 40)),
                  (FLAGS, p("<I", 2)), (FLAGS, p("<I", 1 << 8)), (REQ, p("<Q", 1)),
                  (OPT, p("<Q", 1 << 63)), (f.index(b"waboros ") + 8, b"9"),
                  *[(40, p("<Q", o)) for o in (1 << 63, (1 << 64) - 1, 1 << 40)]]  # fmt: skip
        for at, d in fields:
            add(f"{kind} {at}:{d.hex()}", put(f, at, d), JIT, f)
        ones = [(OPT, b"\xff" * 8), (FEATS, b"\xff" * 8), (REQ, b"\xff" * 8), (FLAGS, b"\xff" * 4)]
        add(
            f"{kind} optional features all ones", put(f, *ones[0]), JIT, f
        )  # the longest header line
        add(f"{kind} every header number all ones", _all(f, ones), JIT, f)
        other = small(OTHER, wasm=kind == "fat")
        add(f"{kind} compiler target", f, OTHER, f)
        add(f"{kind} another machine", other, JIT, f)
        add(f"{kind} another machine and target", other, OTHER, f)
        ts = secs(f)[".waboros.types"][1]
        add(f"{kind} abi and types", put(put(f, ABI, p("<H", 2)), ts, b"\xff" * 4), JIT, f)
        add(f"{kind} types", put(f, ts, b"\xff" * 4), JIT, f)
    bad = put(fat, fat.index(SMALL) + len(SMALL) - 1, b"c")
    add("fat with another wasm", bad, JIT, fat)
    add("fat with another wasm and version 2", put(bad, 8, b"\2"), JIT, fat)
    for name, (make, target, _step, _sec) in STEPS.items():
        add(f"steps {name}", make(), target, thin)
    with pytest.MonkeyPatch.context() as mp:  # guard files: only a GUARD_LOG2 of 26 is in step 1
        mp.setenv("WABOROS_JIT_BOUNDS", "guard")
        guard = small(JIT), small(JIT, wasm=True)
    for g in guard:
        add("guard log2 26", put(g, FLAGS, p("<I", 1 | 26 << 8)), JIT, g)
    m = parse(SMALL)
    validate(m)
    c = base.TARGETS[JIT]().compile(m)
    ver = A.compiler_string(JIT).split(" ")[1]
    for tokens in (
        A.compiler_string(JIT) + "9" * 5000,
        f"waboros  {JIT} 0",
        f"waboros {ver} {JIT} 9999999999",
    ):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(A, "compiler_string", lambda _t, s=tokens: s)
            add("compiler string tokens", A.write(c, target=JIT, wasm=SMALL), JIT, fat)


def _container_cases(add: Callable[..., None]) -> None:
    """test_strict: unknown sections, the container rules, what the reader ignores, and the payload
    patches of test_format::test_malformed_payloads."""
    fat, thin = small(JIT, wasm=True), small(JIT)
    for before in (".waboros.types", ".symtab", ".strtab", ".shstrtab", ".waboros.wasm"):
        for al in (1, 8, 64):
            extra = with_section(fat, ".waboros.future", b"\xff" * 13, before, al)
            add(f"unknown section before {before} align {al}", extra, JIT, fat)
    add(
        "unknown empty section", with_section(fat, ".note.GNU-stack", b"", ".shstrtab", 1), JIT, fat
    )
    add("unknown before header", with_section(fat, ".x", b"1", ".waboros.header"), JIT, fat)
    add("unknown before text", with_section(fat, ".x", b"1", ".text"), JIT, fat)
    extra = with_section(fat, ".x", b"1234567", ".waboros.types", 8)
    text_at = secs(extra)[".text"][1]
    for f, v in (("offset", 0), ("offset", 64), ("offset", text_at + 8), ("align", 3), ("addr", 8),
                 ("size", len(extra))):  # fmt: skip
        add(f"unknown section {f}={v}", patch(extra, ".x", f, v), JIT, fat)
    nobits = with_section(fat, ".x", b"1234", ".strtab")
    add("nobits unknown section", patch(nobits, ".x", "type", 8), JIT, fat)
    s, shoff = secs(fat), struct.unpack_from("<Q", fat, 40)[0]
    ex, tg = s[".waboros.exports"][0], s[".waboros.tags"][0]
    pt = lambda sec, f, v: patch(fat, sec, f, v)  # noqa: E731
    container = [
        pt(".waboros.types", "offset", s[".text"][1]), pt(".waboros.header", "offset", 0),
        pt(".shstrtab", "size", len(fat)), fat + b"\0", put(fat, 4095, b"\1"),
        pt(".waboros.types", "type", 8), pt(".text", "type", 8), pt(".shstrtab", "type", 1),
        pt(".strtab", "type", 1), pt(".symtab", "type", 3), pt(".text", "flags", 7),
        pt(".waboros.tags", "align", 16), pt(".text", "align", 8), pt(".waboros.tags", "addr", 8),
        pt(".waboros.tags", "link", 1), pt(".waboros.tags", "info", 1),
        pt(".waboros.tags", "entsize", 4), pt(".symtab", "link", 1), pt(".symtab", "entsize", 16),
        put(fat, shoff + 40, b"\1"), put(put(fat, tg, fat[ex : ex + 64]), ex, fat[tg : tg + 64]),
        pt(".waboros.tags", "flags", 2), pt(".symtab", "info", 1),
    ]  # fmt: skip
    for i, b in enumerate(container):
        add(f"container {i}", b, JIT, fat)
    for name in (".symtab", ".strtab"):
        _, at, size = s[name]
        add(f"{name} flipped", put(fat, at + size - 2, bytes((fat[at + size - 2] ^ 1,))), JIT, fat)
    for sec, at, new in [("header", 66, b"\1"), ("header", 76, b"\1"), ("funcs", 12, u32(9)),
                         ("funcs", 4, u32(5)), ("exports", 9, b"\7"), ("exports", 10, u32(3)),
                         ("memories", 5, b"\x11"), ("memories", 4, b"\x04"), ("memories", 6, b"\1"),
                         ("globals", 5, b"\7"), ("globals", 4, b"\x70"), ("segments", 4, u32(9)),
                         ("types", 10, b"\1"), ("types", 17, b"\x7e"), ("context", 3, b"\1"),
                         ("context", 0, b"\2"), ("traps", 0, u32(1))]:  # fmt: skip
        add(f"payload {sec} {at}", put(thin, secs(thin)[f".waboros.{sec}"][1] + at, new), JIT, thin)
    m = parse(SMALL)
    validate(m)
    c = base.TARGETS[JIT]().compile(m)
    for sec in A.ORDER:  # a payload one byte longer, as the writer would lay it out
        if sec not in ("wasm", "text", "header"):
            good = A._sections(c, SMALL, JIT)
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(A, "_sections", emits({**good, sec: good[sec] + b"\0"}))
                add(f"trailing byte in {sec}", A.write(c, target=JIT, wasm=SMALL), JIT, fat)


def _payload_cases(add: Callable[..., None]) -> None:
    """test_review (allocation, limits, the signature word) and test_strict's constant
    expression typing."""

    def mod(text: str, section: str, at: int, new: bytes, name: str) -> None:
        blob = bld(text)
        if at < 0:
            at += secs(blob)[f".waboros.{section}"][2]
        add(name, put(blob, secs(blob)[f".waboros.{section}"][1] + at, new), JIT, blob)

    blob = bld("(module (type $s (struct)) (global (ref $s) (struct.new $s)))")
    add("allocation, heap 1", blob, JIT, blob)
    add("allocation, heap 0", put(blob, secs(blob)[".waboros.context"][1], b"\0"), JIT, blob)
    mem, tab = "(module (memory 1))", "(module (table 1 funcref))"
    for text, sec, at, lim in [
        (mem, "memories", 4, (0, 16, 0, 70000, 0)), (mem, "memories", 4, (1, 16, 0, 5, 3)),
        (mem, "memories", 4, (3, 16, 0, 1 << 49, 0)), (tab, "tables", 9, (0, 0, 0, 1 << 33, 0)),
        (tab, "tables", 9, (1, 0, 0, 3, 2)), (mem, "memories", 4, (1, 16, 0, 65536, 65536)),
        (mem, "memories", 4, (3, 16, 0, 1 << 48, 1 << 48)),
    ]:  # fmt: skip
        mod(text, sec, at, struct.pack("<BBHQQ", *lim), f"limits {sec} {lim}")
    ci = "(type $f (func)) (type $s (struct)) (table 1 funcref)"
    call = bld(f"(module {ci} (func (call_indirect (type $f) (i32.const 0))))")
    v = A.read(call)
    sig = secs(call)[".waboros.context"][1] + 8 + 4 * len(v.records) + 4
    struct_ty = next(i for i, d in enumerate(v.module["tdefs"]) if d[0][0] == "struct")
    add("signature word of a struct type", put(call, sig, u32(struct_ty)), JIT, call)
    f32c, ext, func = b"\x43\0", struct.pack("<i", -17), b"\x63" + struct.pack("<i", -16)
    elem = "(module (table 1 funcref) (func $f) (elem (i32.const 0) $f))"
    imp = '(module (import "m" "g" (global i32)) (global i32 (global.get 0)))'
    two = "(module (table 2 funcref) (func $f) "
    for i, (text, sec, at, new) in enumerate([
            ("(module (global i32 (i32.const 7)))", "globals", 10, f32c),
            ("(module (global i32 (i32.add (i32.const 1) (i32.const 2))))", "globals", 10, f32c),
            (imp, "globals", 4, b"\x7e"), (imp, "imports", 16, b"\1"),
            ("(module (func $f) (table 1 funcref (ref.func $f)))", "tables", 5, ext),
            (elem, "segments", 14, f32c[:1]),
            ('(module (memory 1) (data (i32.const 8) "ab"))', "segments", 17, f32c[:1]),
            ('(module (memory i64 1) (data (i64.const 8) "ab"))', "memories", 4, b"\0"),
            (two + "(elem (i32.const 0) funcref (ref.null func) (ref.func $f)))",
             "segments", 6, ext),
            (two + "(elem (i32.const 0) $f))", "segments", 6, ext),
            ('(module (import "m" "g" (global funcref)) (global funcref (ref.null func))'
             " (table 1 funcref (global.get 0)))", "tables", 4 + 5 + 20 + 1 + 4 + 2, u32(1)),
            ("(module (func $f) (table 1 (ref func) (ref.func $f)))", "tables", 29, b"\0"),
            ("(module (table 1 funcref) (func $a) (func $b) (elem (i32.const 0) $a))",
             "segments", -8, u32(1))]):  # fmt: skip
        mod(text, sec, at, new, f"cexpr typing {i}")
    g = bld('(module (import "m" "g" (global i32)) (global i32 (i32.const 1)))')
    for k in (0, 1, 2):
        at = secs(g)[".waboros.globals"][1] + 10
        add(f"global initializer reads global {k}", put(g, at, b"\x23\0" + u32(k)), JIT, g)
    seg = u32(1) + b"\0" + func + u32(0) + A._cexpr((("i32", 0),)) + b"\1" + u32(1)
    seg += A._cexpr((("func", 0),)) + u32(0)
    m = parse(wat(elem))
    validate(m)
    c = base.TARGETS[JIT]().compile(m)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(A, "_sections", emits({**A._sections(c, None, JIT), "segments": seg}))
        add("items 1 with only function indices", A.write(c, target=JIT, module_sha256=bytes(32)))


def whole_file() -> list[Case]:
    out: list[Case] = []

    def add(name: str, data: bytes, target: str = JIT, src: bytes | None = None) -> None:
        out.append(Case(name, data, target, src))

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("WABOROS_JIT_BOUNDS", "explicit")
        for part in (_header_cases, _container_cases, _payload_cases):
            part(add)
    return out


def truncations() -> Iterator[Case]:
    """Every prefix of the committed small file (thin and fat, this host's, explicit) and of a GC
    module (thin and fat)."""
    arch = JIT.removeprefix("jit-")
    files = {
        f"small {k}": (DATA / f"v1-d75-small-{arch}-explicit{k}.wbo").read_bytes()
        for k in ("-thin", "")
    }
    gc = wat(f"(module {GC})")
    v = A.read(fat_file(gc))
    files["gc thin"] = A.write(v, target=JIT, module_sha256=v.module_sha256)
    files["gc fat"] = fat_file(gc)
    for name, blob in files.items():
        for n in range(len(blob)):
            yield Case(f"{name} prefix {n}", blob[:n], JIT, blob)


def flips(what: str, n: int, seed: int = 12345) -> Iterator[Case]:
    """test_strict's byte-flip loop (the same choice of bytes) over the fat file of a module, and
    over its thin file for every other flip (the thin classes E1 and E2 need thin files)."""
    fat = fat_file(FLIPS[what]())
    v = A.read(fat)
    thin = A.write(v, target=JIT, module_sha256=v.module_sha256)
    rnd, code = random.Random(seed), len(v.code)
    meta = {b: [i for i in range(len(b)) if not 4096 <= i < 4096 + code] for b in (fat, thin)}
    for k in range(n):
        blob = (fat, thin)[k // 2 % 2]
        at = rnd.choice(meta[blob]) if k % 4 else rnd.randrange(4096, 4096 + code)
        b = bytearray(blob)
        b[at] ^= 1 << rnd.randrange(8) if k % 2 else rnd.randrange(1, 256)
        yield Case(f"{what} flip {k} at {at}", bytes(b), JIT, blob)


ABSTRACT = {-12: "noexn", -13: "nofunc", -14: "noextern", -15: "none", -16: "func", -17: "extern",
            -18: "any", -19: "eq", -20: "i31", -21: "struct", -22: "array", -23: "exn"}  # fmt: skip


def subtype_modules() -> list[str]:
    """Constant expressions that use each abstract heap type (and a concrete type) where each of
    its supertypes is expected, nullable and not, in a global, a table's initial value and an
    element item (`ABOVE` of read.c against `types._ABOVE`)."""
    types = "(type $s (struct)) (type $a (array i8)) (type $f (func)) "
    pairs = [(ABSTRACT[a], ABSTRACT[b]) for a, ups in T._ABOVE.items() for b in sorted(ups)]
    pairs += [(f"${k}", up) for k, ups in (("s", "$s struct eq any"), ("a", "$a array eq any"),
                                           ("f", "$f func")) for up in ups.split()]  # fmt: skip
    pairs += [("none", "$s"), ("none", "$a"), ("nofunc", "$f")]
    out: list[str] = []
    for x, y in pairs:
        out += [
            f"(module {types}(global $g (ref null {x}) (ref.null {x})) "
            f"(global (ref null {y}) (global.get $g)))",
            f'(module {types}(import "m" "g" (global (ref {x}))) (global (ref {y}) (global.get 0))'
            f" (global (ref null {y}) (global.get 0)))",
            f'(module {types}(import "m" "g" (global (ref null {x}))) '
            f"(table 1 (ref null {y}) (global.get 0)))",
            f"(module {types}(table 1 (ref null {y})) "
            f"(elem (table 0) (i32.const 0) (ref null {x}) (ref.null {x})))",
        ]
    return out


def modules() -> Iterator[Case]:
    """Accepted files, for the dump: the hand-written modules of `test_roundtrip` (every kind of
    import, segment and GC type), the constant-expression module of `test_format`, and every module
    of the pinned spec suite that the JIT compiles (fat files, as `test_spec_suite`)."""
    hand = [wat(f"(module {text})") for text in HAND.values()] + [wat(CEXPR)]
    for text in subtype_modules():
        try:
            hand.append(wat(text))
        except Exception:  # a combination the assembler refuses
            pass
    spec = sorted((fetch.CACHE_DIR / "json").rglob("*.wasm")) if fetch.CACHE_DIR.is_dir() else []
    for i, wasm in enumerate(hand + [f.read_bytes() for f in spec]):
        try:
            blob = fat_file(wasm)
        except Exception:  # malformed, invalid or not compiled by this target: the suite has such
            continue
        yield Case(f"module {i}", blob, JIT, None)


def committed() -> list[Case]:
    out: list[Case] = []
    for f in sorted(DATA.glob("v1-d75-*.wbo")):
        out.append(Case(f.name, f.read_bytes(), f"jit-{f.name.split('-')[3]}", f.read_bytes()))
    return out
