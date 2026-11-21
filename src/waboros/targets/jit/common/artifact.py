# pyright: basic
"""The JIT artifact of docs/specs/S0014: `write(compiled, ...) -> bytes` makes the ELF64 file,
`read(bytes) -> ModuleView` parses it exactly and refuses what it does not know. Standard library
only, no host code (ctypes, mmap): the micro-compilers (D60) port this file's shape.

The module view carries the keys of the ValidModule that instantiation reads (S0014 "What
instantiation reads: inventory"), as the front end builds them, so `JitInstance` has one path for a
compiled and a loaded module: `module_view` gives `Compiled` the same keys (step 3). `load` is the
loader's rules (S0014 "Semantics: the loader").
"""

import functools
import hashlib
import os
import platform
import struct
from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from waboros import __version__
from waboros.frontend import types as T
from waboros.frontend.binary import NULLABLE
from waboros.frontend.validate import Invalid, _conv, _gcsig
from waboros.targets import base
from waboros.targets.jit.common import gc
from waboros.targets.jit.common.context import GC_SERVICES, Layout

FORMAT_VERSION, OSABI, NO_START = 1, 0xCB, 0xFFFFFFFF
I32, I64, V128 = 0x7F, 0x7E, 0x7B
NUMS = (0x7F, 0x7E, 0x7D, 0x7C, 0x7B)
KINDS = ("func", "table", "mem", "global", "tag")  # import and export kinds 0-4
# target -> (e_machine, isa name of level 1); a new architecture adds a row
TARGETS = {"jit-x86_64": (62, "x86-64-v1"), "jit-aarch64": (183, "armv8.0-a")}
SWITCHES = ("WABOROS_JIT_NOPIN", "WABOROS_JIT_NOLIVE", "WABOROS_JIT_PAD", "WABOROS_JIT_LOOP_ALIGN")
ORDER = ("header", "text", "types", "imports", "funcs", "tables", "memories", "globals", "tags",
         "exports", "segments", "context", "stackmaps", "traps", "trampolines", "wasm")  # fmt: skip
REQUIRED = tuple(n for n in ORDER if n != "wasm")
CONVENTIONS = {"sysv": 1, "win64": 2, "aapcs64": 3}  # ids in `.waboros.trampolines`
BIN = {("add", "i32"): 0x6A, ("sub", "i32"): 0x6B, ("mul", "i32"): 0x6C,
       ("add", "i64"): 0x7C, ("sub", "i64"): 0x7D, ("mul", "i64"): 0x7E}  # fmt: skip
BIN_NAME = {v: k for k, v in BIN.items()}
GC_NONE, GC_TYPE = (0xFB1A, 0xFB1B, 0xFB1C), (0xFB00, 0xFB01, 0xFB06, 0xFB07)  # immediates
GC_POPS = {0xFB06: 2, 0xFB07: 1, 0xFB01: 0}  # operands; struct.new takes one per field
M32, M64 = 0xFFFFFFFF, (1 << 64) - 1
# the type of `ref.null`: the file has no heap type for it, so it fits any nullable reference
NULLREF = "nullref"
# section names in file order (S0014 "File layout")
NAMES = [".text" if n == "text" else f".waboros.{n}" for n in ORDER]
NAMES += [".symtab", ".strtab", ".shstrtab"]
# the module keys instantiation reads (S0014 "What instantiation reads: inventory"): no bodies
INVENTORY = ("tdefs", "types", "tkeys", "imports", "ttypes", "taddr", "maddr", "mems", "funcs",
             "tables", "tinit", "globals", "tags", "exports", "start", "elem_info", "elem_mode",
             "datas", "data_mem")  # fmt: skip


class ArtifactError(ValueError):
    """The file is not a valid v1 artifact; the message names the section and the reason."""


@dataclass
class ModuleView:
    """What `read` returns: the fields of `Compiled` (same names) with `module` a dict of the
    inventory's keys, plus the header. `write(view, ...)` gives back the bytes it was read from."""

    target: str
    target_version: int
    compiler: str
    machine: int
    module_sha256: bytes
    config_sha256: bytes
    ctx_abi_version: int
    isa_level: int
    isa_features: int
    features_required: int
    features_optional: int
    flags: int
    module: dict[str, Any]
    code: bytes
    data_start: int
    funcs: list[tuple[int, int]]  # (code offset, result slots)
    layout: Layout
    records: list[int]
    sigs: list[int]
    thunks: dict[int, int]
    rtts: list[int]
    thrown: list[int]
    heap: bool
    subtype: bool
    faults: list[tuple[int, int]]
    stackmaps: bytes
    wasm: bytes | None
    # the decoded stack maps of the first instantiation (as `Compiled.sites`)
    sites: Any = dc_field(default=None, init=False, repr=False, compare=False)


def module_view(mod: dict[str, Any]) -> dict[str, Any]:
    """The inventory's keys of a ValidModule (shared values, the key cache included): what
    `Compiled.module` holds, so a compiled and a read module give the instance the same keys."""
    mod.setdefault("tkeys", {})
    return {k: mod[k] for k in INVENTORY}


# ------------------------------------------------------------------------------------- writer
def _source_digest(root: Path) -> str:
    """The first 16 hex digits of SHA-256 over the compiler-side sources under `root` (the package
    directory): the front end and the JIT, sorted by relative path, each as path, NUL, bytes."""
    h = hashlib.sha256()
    files = [
        f
        for d in ("frontend", "targets/jit")
        for f in (root / d).rglob("*.py")
        if not f.name.startswith(".") and f.is_file()  # not an editor's lock (a dangling link)
    ]
    for f in sorted(files, key=lambda f: f.relative_to(root).as_posix()):
        h.update(f.relative_to(root).as_posix().encode() + b"\0" + f.read_bytes())
    return h.hexdigest()[:16]


@functools.cache
def _own_digest() -> str:
    return _source_digest(Path(__file__).parents[3])


def compiler_string(target: str) -> str:
    """`waboros <version> <target> <target version>` as this process writes it and the loader
    requires it (Q3). A release writes its version; a development build (a setuptools-scm local
    part `+g...`, a `.devN`, or the fallback 0.0.0 of an uninstalled tree) writes the version
    without its local part, `-` and `_own_digest()`, so an edit to a compiler source changes it."""
    v = __version__
    if "+" in v or ".dev" in v or v == "0.0.0":
        v = f"{v.split('+')[0]}-{_own_digest()}"
    return f"waboros {v} {target} {base.TARGETS[target]().version}"


def _u(fmt: str, *a: int) -> bytes:
    return struct.pack("<" + fmt, *a)


def _str(s: str) -> bytes:
    b = s.encode()
    return _u("I", len(b)) + b


def _vt(t: Any) -> bytes:
    """A value type (or packed storage type) in the form of "`.waboros.types`": a reference is
    always long (0x63/0x64 and the heap type), so equal types have equal bytes."""
    if t in NUMS or t in (0x78, 0x77):
        return bytes((t,))
    if t.__class__ is not tuple:
        t = (1, NULLABLE_BACK[t])
    return _u("Bi", 0x63 if t[0] else 0x64, t[1])


NULLABLE_BACK = {b: h for h, b in NULLABLE.items()}


def _field(f: Any) -> bytes:
    return _vt(f[0]) + bytes((f[1],))


def _limits(mn: int, mx: int | None, a64: bool, log2: int) -> bytes:
    return _u("BBHQQ", (mx is not None) | a64 << 1, log2, 0, mn, mx or 0)


def _cexpr(e: Any) -> bytes:
    """A constant expression: its nodes, already in postfix order, as operations."""
    out = bytearray()
    for n in e:
        k = n[0]
        if k in ("i32", "f32"):
            out += _u("HI", 0x41 if k == "i32" else 0x43, n[1])
        elif k in ("i64", "f64"):
            out += _u("HQ", 0x42 if k == "i64" else 0x44, n[1])
        elif k == "v128":
            out += _u("H", 0xFD0C) + n[1].to_bytes(16, "little")
        elif k in ("global", "func"):
            out += _u("HI", 0x23 if k == "global" else 0xD2, n[1])
        elif k == "null":
            out += _u("H", 0xD0)
        elif k in ("add", "sub", "mul"):
            out += _u("H", BIN[k, n[1]])
        elif k == "gc" and n[1][0] in (*GC_NONE, *GC_TYPE, 0xFB08):
            out += _u("H" + "I" * (len(n[1]) - 1), *n[1])
        else:
            raise ValueError(f"constant expression {k!r} cannot be written")
    return _u("I", len(out)) + bytes(out)


def _types(mod: Any) -> bytes:
    td, groups = mod["tdefs"], []
    for i, (c, sup, fin, gs, _) in enumerate(td):
        if len(sup) > 1:
            raise ValueError("a type with several supertypes cannot be written")
        b = bytes((fin, len(sup))) + _u("H", 0) + b"".join(_u("I", s) for s in sup)
        if c[0] == "func":
            b += b"\x60" + _u("I", len(c[1])) + b"".join(map(_vt, c[1]))
            b += _u("I", len(c[2])) + b"".join(map(_vt, c[2]))
        elif c[0] == "struct":
            b += b"\x5f" + _u("I", len(c[1])) + b"".join(map(_field, c[1]))
        else:
            b += b"\x5e" + _field(c[1])
        if i == gs:
            groups.append([])
        groups[-1].append(b)
    out = [_u("I", len(groups))] + [_u("I", len(g)) + b"".join(g) for g in groups]
    return b"".join(out) + _u("I", len(td)) + b"".join(T.key(mod, i) for i in range(len(td)))


def _conventions(target: str) -> dict[str, tuple[bytes, list[int], int]]:
    """The target's conventions in file order: (blob, offsets, ENTRY_SAVED) from abi.assemble."""
    t: Any = base.TARGETS[target]()
    return t.conventions()


def _text(c: Any, target: str) -> tuple[bytes, bytes, list[tuple[str, int, list[int], int]]]:
    """`.text`: the code and data area padded with zeros to a multiple of 16 (the padding is part
    of the data area), then each convention's blob, as abi.assemble gives it, at the next multiple
    of 16; `.waboros.trampolines`; and (convention, start, offsets, size) per blob."""
    convs = _conventions(target)
    text, pay, at = bytearray(c.code), [_u("I", len(convs))], []
    for name, (blob, offs, saved) in convs.items():
        text += bytes(-len(text) % 16)
        o = len(text)
        text += blob
        pay.append(_u("BBHII", CONVENTIONS[name], 0, 0, o, len(blob)))
        pay.append(_u("7I", *(o + x for x in offs), saved))
        at.append((name, o, offs, len(blob)))
    return bytes(text), b"".join(pay), at


def _sections(c: Any, wasm: bytes | None, target: str) -> dict[str, bytes]:
    mod = c.module
    imps = mod["imports"]
    tt, mt = iter(range(len(mod["ttypes"]))), iter(range(len(mod["maddr"])))
    s: dict[str, bytes] = {"types": _types(mod)}
    s["text"], s["trampolines"], _ = _text(c, target)
    b = [_u("I", len(imps))]
    for k, m, n, d in imps:
        b.append(_str(m) + _str(n) + bytes((KINDS.index(k),)))
        if k in ("func", "tag"):
            b.append(_u("I", d))
        elif k == "table":
            i = next(tt)
            b.append(_vt(mod["ttypes"][i]) + _limits(d[0], d[1], mod["taddr"][i] == I64, 0))
        elif k == "mem":
            b.append(_limits(d[0], d[1], mod["maddr"][next(mt)] == I64, 16))
        else:
            b.append(_vt(d[0]) + bytes((d[1],)))
    s["imports"] = b"".join(b)
    nt = sum(k == "table" for k, *_ in imps)
    nm = sum(k == "mem" for k, *_ in imps)
    nf = sum(k == "func" for k, *_ in imps)
    start = NO_START if mod["start"] is None else mod["start"]
    s["funcs"] = _u("III", c.data_start, start, len(c.funcs))
    s["funcs"] += b"".join(_u("II", t, o) for t, (o, _) in zip(mod["funcs"], c.funcs, strict=True))
    tb = [_u("I", len(mod["tables"]))]
    for k, ((mn, mx), init) in enumerate(zip(mod["tables"], mod["tinit"], strict=True)):
        i = nt + k
        tb.append(_vt(mod["ttypes"][i]) + _limits(mn, mx, mod["taddr"][i] == I64, 0))
        tb.append(b"\0" if init is None else b"\1" + _cexpr(init))
    s["tables"] = b"".join(tb)
    s["memories"] = _u("I", len(mod["mems"])) + b"".join(
        _limits(mn, mx, mod["maddr"][nm + k] == I64, 16) for k, (mn, mx) in enumerate(mod["mems"])
    )
    s["globals"] = _u("I", len(mod["globals"])) + b"".join(
        _vt(t) + bytes((m,)) + _cexpr(e) for t, m, e in mod["globals"]
    )
    s["tags"] = _u("I", len(mod["tags"])) + b"".join(_u("I", t) for t in mod["tags"])
    s["exports"] = _u("I", len(mod["exports"])) + b"".join(
        _str(n) + bytes((k,)) + _u("I", i) for n, k, i in mod["exports"]
    )
    sg = [_u("I", len(mod["elem_info"]))]
    for (ti, off, rt, items), mode in zip(mod["elem_info"], mod["elem_mode"], strict=True):
        m = ("active", "passive", "declarative").index(mode)
        sg.append(bytes((m,)) + _vt(rt))
        if m == 0:
            sg.append(_u("I", ti) + _cexpr(off))
        funcs = all(e[-1][0] == "func" for e in items)
        sg.append(bytes((not funcs,)) + _u("I", len(items)))
        sg += [_u("I", e[0][1]) for e in items] if funcs else [_cexpr(e) for e in items]
    sg.append(_u("I", len(mod["datas"])))
    for (off, data), mi in zip(mod["datas"], mod["data_mem"], strict=True):
        sg.append(b"\1" if mi is None else b"\0" + _u("I", mi) + _cexpr(off))
        sg.append(_u("I", len(data)) + bytes(data))
    s["segments"] = b"".join(sg)
    sub = c.subtype
    lists = (c.records, c.sigs, [c.thunks[i] for i in range(nf)], c.rtts, c.thrown)
    s["context"] = (
        bytes((c.layout.heap, bool(sub))) + _u("H", 0)
        + b"".join(_u("I" * (len(x) + 1), len(x), *x) for x in lists)
    )  # fmt: skip
    if c.stackmaps is None:
        raise ValueError("the compiled module has no stack-map table (STACKMAPS)")
    s["stackmaps"] = bytes(c.stackmaps)
    s["traps"] = _u("I", len(c.faults)) + b"".join(_u("II", *f) for f in c.faults)
    if wasm is not None:
        s["wasm"] = wasm
    return s


def _symbols(c: Any, mach: int, nimp: int, target: str) -> list[tuple[str, int, int, int]]:
    """(name, value, size, info): the locals, then the global entry symbols (S0014 "Symbols")."""
    starts = [(o, f"wasm[{nimp + k}]") for k, (o, _) in enumerate(c.funcs)]
    starts += [(o, f"thunk[{i}]") for i, o in sorted(c.thunks.items())]
    ends = sorted({o for o, _ in starts} | {c.data_start})
    out = [(n, o, ends[ends.index(o) + 1] - o, 2) for o, n in starts]
    at = _text(c, target)[2]
    entries = []
    for conv, o, offs, size in at:
        cuts = [o + x for x in offs[:4]] + [o + size]
        names = ("entry", "bridge", "sp_stub", "tail_switch")
        tramp = [
            (f"__waboros_{n}_{conv}", a, b - a)
            for n, a, b in zip(names, cuts, cuts[1:], strict=False)
        ]
        out += [(n, a, z, 2) for n, a, z in tramp[1:]]
        entries.append((*tramp[0], 0x12))  # STB_GLOBAL, STT_FUNC
    if mach == 183:
        out.append(("$x", 0, 0, 0))
        if at[0][1] > c.data_start:  # the data area (its padding included) is not empty
            out.append(("$d", c.data_start, 0, 0))
        out.append(("$x", at[0][1], 0, 0))
    return out + entries


def _strtab(names: list[str]) -> tuple[bytes, dict[str, int]]:
    tab, off = bytearray(b"\0"), {"": 0}
    for n in names:
        if n not in off:
            off[n] = len(tab)
            tab += n.encode() + b"\0"
    return bytes(tab), off


def _symtab(c: Any, mach: int, nimp: int, target: str) -> tuple[bytes, bytes, int]:
    """The `.symtab` and `.strtab` payloads the file of `c` must have, and the first global."""
    syms = _symbols(c, mach, nimp, target)
    strtab, so = _strtab([n for n, *_ in syms])
    tab = bytes(24) + b"".join(_u("IBBHQQ", so[n], i, 0, 2, v, z) for n, v, z, i in syms)
    return tab, strtab, 1 + sum(i >> 4 == 0 for *_, i in syms)


def write(
    c: Any,
    *,
    target: str,
    module_sha256: bytes | None = None,
    wasm: bytes | None = None,
) -> bytes:
    """The artifact of a `Compiled` (or a `ModuleView`) for the compiler `target` (a key of
    `TARGETS`; `Compiled` does not carry it). The bounds mode is `c.flags` (the writer does not look
    at the host, Q12). The hash of the wasm bytes is `module_sha256`, or taken from `wasm`, which
    makes the file fat (D18). Raises ValueError for a test switch that changes code (Q11)."""
    on = [n for n in SWITCHES if os.environ.get(n)]
    if on:
        raise ValueError(f"cannot write an artifact while {', '.join(on)} is set (S0014 Q11)")
    if target not in TARGETS:
        raise ValueError(f"unknown target {target!r}")
    if wasm is not None:
        module_sha256 = hashlib.sha256(wasm).digest()
    if module_sha256 is None or len(module_sha256) != 32:
        raise ValueError("write needs wasm= or a 32-byte module_sha256=")
    mach, isa = TARGETS[target]
    guard = c.flags & 1
    if c.flags & ~0xFF01 or bool(guard) != bool(c.flags >> 8 & 0xFF):
        raise ValueError(f"invalid artifact flags {c.flags:#x}")
    cfg = f"bounds={'guard' if guard else 'explicit'}\nguard_log2={c.flags >> 8 & 0xFF}\n"
    cfg += f"isa={isa}\ntarget={target}\n"
    compiler = compiler_string(target)
    hdr = module_sha256 + hashlib.sha256(cfg.encode()).digest()
    hdr += _u("HHHHIIQQQ", 1, 0, 1, 0, c.flags, 0, 0, 0, 0) + _str(compiler)
    secs = _sections(c, wasm, target)
    secs["header"] = hdr
    nimp = sum(k == "func" for k, *_ in c.module["imports"])
    symtab, strtab, first = _symtab(c, mach, nimp, target)
    parts = [(".text" if n == "text" else f".waboros.{n}", 1, 6 * (n == "text"), secs[n],
              4096 if n == "text" else 8, 0, 0, 0) for n in ORDER if n in secs]  # fmt: skip
    parts += [(".symtab", 2, 0, symtab, 8, len(parts) + 2, first, 24),
              (".strtab", 3, 0, strtab, 1, 0, 0, 0)]  # fmt: skip
    shstr, sh = _strtab([p[0] for p in parts] + [".shstrtab"])
    parts.append((".shstrtab", 3, 0, shstr, 1, 0, 0, 0))
    out, heads = bytearray(64), [bytes(64)]
    for name, typ, fl, data, al, link, info, ent in parts:
        out += bytes(-len(out) % al)
        heads.append(
            _u("IIQQQQIIQQ", sh[name], typ, fl, 0, len(out), len(data), link, info, al, ent)
        )
        out += data
    out += bytes(-len(out) % 8)
    shoff = len(out)
    out += b"".join(heads)
    ident = b"\x7fELF" + bytes((2, 1, 1, OSABI, FORMAT_VERSION)) + bytes(7)
    elf = _u("HHIQQQIHHHHHH", 1, mach, 1, 0, 0, shoff, 0, 64, 0, 0, 64, len(heads), len(heads) - 1)
    out[:64] = ident + elf
    return bytes(out)


# ------------------------------------------------------------------------------------- reader
class _R:
    """A bounds-checked reader of one payload (or one constant expression inside it)."""

    def __init__(self, b: bytes, what: str, p: int = 0, e: int | None = None) -> None:
        self.b, self.what, self.p = b, what, p
        self.e = len(b) if e is None else e

    def fail(self, msg: str) -> Any:
        raise ArtifactError(f"{self.what}: {msg}")

    def take(self, n: int) -> bytes:
        if n > self.e - self.p:
            self.fail("truncated")
        self.p += n
        return self.b[self.p - n : self.p]

    def u(self, n: int, signed: bool = False) -> int:
        return int.from_bytes(self.take(n), "little", signed=signed)

    def u8(self) -> int:
        return self.u(1)

    def u32(self) -> int:
        return self.u(4)

    def zero(self, n: int, name: str = "reserved field") -> None:
        if self.u(n):
            self.fail(f"{name} is not zero")

    def count(self, size: int) -> int:
        """A count of items of at least `size` bytes each, checked against the bytes left."""
        n = self.u32()
        if n * size > self.e - self.p:
            self.fail(f"count {n} is past the end of the payload")
        return n

    def text(self) -> str:
        try:
            return self.take(self.u32()).decode()
        except UnicodeDecodeError:
            return self.fail("a string is not UTF-8")

    def index(self, n: int, what: str) -> int:
        i = self.u32()
        if i >= n:
            self.fail(f"{what} index {i} out of range ({n})")
        return i

    def done(self) -> None:
        if self.p != self.e:
            self.fail("trailing bytes")


@dataclass
class _Env:
    """What the payloads parsed so far give the ones after (index ranges and kinds)."""

    tdefs: list[Any]
    ntypes: int = 0
    nfuncs: int = 0
    ntables: int = 0
    nmems: int = 0
    nglobals: int = 0
    gmax: int | None = None  # a global's initializer reads imported and earlier globals only
    ntags: int = 0
    records: list[int] | None = None
    gtypes: list[Any] = dc_field(default_factory=list)  # value type of every global so far
    gmut: list[int] = dc_field(default_factory=list)  # mutability of each
    ftidx: list[int] = dc_field(default_factory=list)  # type index of every function so far

    def __post_init__(self) -> None:
        self.mod = {"tdefs": self.tdefs, "types": [()] * len(self.tdefs)}  # what typing reads
        self.ns: Any = SimpleNamespace(mod=self.mod)  # stands in for the validator's context

    def fits(self, t: Any, want: Any) -> bool:
        if t == NULLREF:
            return T.isref(want) and bool(T.nn(want)[0])
        return T.match(self.mod, t, want)

    def func_ref(self, r: _R) -> int:
        """A function index that `ref.func` may name: in range and with a record."""
        i = r.index(self.nfuncs, "function")
        if self.records is not None and i not in self.records:
            r.fail(f"function {i} has no record")
        return i

    def ftype(self, r: _R) -> int:
        i = r.index(self.ntypes, "type")
        if self.tdefs[i][0][0] != "func":
            r.fail(f"type {i} is not a function type")
        return i


def _vt_r(r: _R, ntypes: int | None, pend: list[int], refs: bool = False) -> Any:
    """A value type, as the front end represents it; `ntypes` None: indices are checked later."""
    b = r.u8()
    if b in NUMS and not refs:
        return b
    if b not in (0x63, 0x64):
        return r.fail(f"bad {'reference' if refs else 'value'} type byte {b:#x}")
    h = r.u(4, True)
    if h >= 0:
        if ntypes is None:
            pend.append(h)
        elif h >= ntypes:
            r.fail(f"type index {h} out of range ({ntypes})")
    elif h < -23 or h > -12:
        r.fail(f"bad abstract heap type {h}")
    return NULLABLE[h] if b == 0x63 and h in NULLABLE else (int(b == 0x63), h)


def _parse_types(r: _R) -> tuple[dict[str, Any], list[bytes]]:
    td: list[Any] = []
    types: list[Any] = []
    pend: list[int] = []

    def field() -> Any:
        b = r.b[r.p] if r.p < r.e else -1
        st = r.u8() if b in (0x78, 0x77) else _vt_r(r, None, pend)
        m = r.u8()
        if m > 1:
            r.fail("mutability is not 0 or 1")
        return st, m

    for _ in range(r.count(4)):
        n, gs, group = r.count(5), len(td), []
        if n == 0:
            r.fail("an empty recursion group")
        for _ in range(n):
            fin, nsup = r.u8(), r.u8()
            r.zero(2)
            if fin > 1 or nsup > 1:
                r.fail("final or supertype count is not 0 or 1")
            sup = ()
            if nsup:
                s = r.u32()
                if s >= gs + len(group):
                    r.fail(f"supertype {s} is not before its type {gs + len(group)}")
                sup = (s,)
            form = r.u8()
            if form == 0x60:
                ps = [_vt_r(r, None, pend) for _ in range(r.count(1))]
                c = ("func", ps, [_vt_r(r, None, pend) for _ in range(r.count(1))])
                types.append((c[1], c[2]))
            elif form in (0x5E, 0x5F):
                if form == 0x5F:
                    c = ("struct", [field() for _ in range(r.count(2))])
                else:
                    c = ("array", field())
                types.append(([], []))
            else:
                r.fail(f"bad composite type form {form:#x}")
            group.append((c, sup, bool(fin)))
        td += [(c, s, f, gs, gs + n) for c, s, f in group]
    if r.u32() != len(td):
        r.fail("the key count is not the type count")
    if len(td) * 32 > r.e - r.p:
        r.fail("truncated")
    keys = [r.take(32) for _ in td]
    r.done()
    if any(h >= len(td) for h in pend) or any(s >= len(td) for _, sp, *_ in td for s in sp):
        r.fail("type index out of range")
    mod: dict[str, Any] = {"tdefs": td, "types": types}
    mod["tkeys"] = dict(enumerate(keys))
    check = {"tdefs": td, "tkeys": {}}
    if any(T.key(check, i) != k for i, k in enumerate(keys)):
        r.fail("a stored type key is not the canonical key of its type")
    return mod, keys


def _limits_r(r: _R, page: int) -> tuple[int, int | None, int]:
    fl, log2 = r.u8(), r.u8()
    r.zero(2)
    mn, mx = r.u(8), r.u(8)
    if fl & ~7 or fl & 4:
        r.fail(f"limits flags {fl:#x} are not supported")
    if log2 != page or (not fl & 1 and mx):
        r.fail("bad page size or a maximum without the flag")
    return mn, mx if fl & 1 else None, I64 if fl & 2 else I32


def _cexpr_r(r: _R, env: _Env, want: Any = None) -> Any:
    """One constant expression. Its value type must fit `want` (its use: a global's or table's
    type, an address type; a reference type by subtyping) when given, and the operands of each
    operation must fit its signature. The result is the nodes in the order written (flat, as the
    front end's); the stack holds the value types."""
    n = r.count(1)
    sub = _R(r.b, r.what + " constant expression", r.p, r.p + n)
    r.p += n
    st: list[Any] = []
    out: list[Any] = []

    def pop(*types: Any) -> int:
        """Take the top operands, bottom first, whose types must fit `types`; how many."""
        k = len(types)
        if len(st) < k:
            sub.fail("the operand stack underflows")
        args = st[len(st) - k :]
        del st[len(st) - k :]
        if not all(env.fits(t, w) for t, w in zip(args, types, strict=True)):
            sub.fail("an operand has the wrong type")
        return k

    try:
        while sub.p < sub.e:
            op = sub.u(2)
            if op in (0x41, 0x43):
                out.append(("i32" if op == 0x41 else "f32", sub.u32()))
                st.append(I32 if op == 0x41 else 0x7D)
            elif op in (0x42, 0x44):
                out.append(("i64" if op == 0x42 else "f64", sub.u(8)))
                st.append(I64 if op == 0x42 else 0x7C)
            elif op == 0xFD0C:
                out.append(("v128", sub.u(16)))
                st.append(V128)
            elif op == 0x23:
                i = sub.index(env.nglobals if env.gmax is None else env.gmax, "global")
                if env.gmut[i]:
                    sub.fail(f"global {i} is mutable")
                out.append(("global", i))
                st.append(env.gtypes[i])
            elif op == 0xD2:
                i = env.func_ref(sub)
                out.append(("func", i))
                st.append((0, env.ftidx[i]))
            elif op == 0xD0:
                out.append(("null", None))
                st.append(NULLREF)  # the heap type is not written (no immediate)
            elif op in BIN_NAME:
                t = I32 if BIN_NAME[op][1] == "i32" else I64
                pop(t, t)
                out.append(BIN_NAME[op])
                st.append(t)
            elif op == 0xFB1C:
                out.append(("gc", (op,), pop(I32)))
                st.append((0, T.I31))
            elif op in GC_NONE:  # any.convert_extern, extern.convert_any: the nullability is kept
                if not st:
                    sub.fail("the operand stack underflows")
                x = st.pop()
                dst = T.ANY if op == 0xFB1A else T.EXTERN
                res = NULLABLE[dst] if x == NULLREF else _conv(env.mod, op - 0xFB00, x)
                out.append(("gc", (op,), 1))
                st.append(res)
            elif op in GC_TYPE or op == 0xFB08:
                ti = sub.index(env.ntypes, "type")
                comp = env.tdefs[ti][0]
                want_kind = "struct" if op in (0xFB00, 0xFB01) else "array"
                if comp[0] != want_kind:
                    sub.fail(f"type {ti} is not a {want_kind} type")
                n = sub.u32() if op == 0xFB08 else GC_POPS.get(op, len(comp[1]))
                if n > len(st):
                    sub.fail("the operand stack underflows")
                ins = (op, ti, n) if op == 0xFB08 else (op, ti)
                pops, res = _gcsig(env.ns, ins, n)
                out.append(("gc", ins, pop(*pops)))
                st.append(res[0])
            else:
                sub.fail(f"unknown operation {op:#06x}")
    except Invalid as e:
        sub.fail(f"type mismatch: {e}")
    if len(st) != 1:
        sub.fail("a constant expression must leave exactly one value")
    if want is not None and not env.fits(st[0], want):
        sub.fail("the type of the value does not fit its use")
    return tuple(out)


def _trampolines(text: bytes, b: bytes, target: str) -> bytes:
    """The code and data area of `.text` (up to the first blob), after the blobs are checked: laid
    out as the writer lays them out, and every convention's record and blob equal to what its
    abi.assemble gives (S0014, Q4 (b): on any host, as `compiler` is this process's anyway)."""
    r = _R(b, ".waboros.trampolines")
    convs = _conventions(target)
    if r.u32() != len(convs):
        r.fail(f"not one record per convention of {target} ({', '.join(convs)})")
    first = pos = -1
    for name, (blob, offs, saved) in convs.items():
        cid = r.u8()
        r.zero(3)
        start, size, *rest = (r.u32() for _ in range(9))
        if cid != CONVENTIONS[name]:
            r.fail(f"convention {cid} where the format has {name} ({CONVENTIONS[name]})")
        if first < 0:
            first = start
            if start % 16 or start > len(text):
                r.fail("the first blob is not at a multiple of 16 inside .text")
        elif start != pos + -pos % 16 or any(text[pos:start]):
            r.fail(f"the {name} blob is not at the next multiple of 16, after zero bytes")
        got = (size, text[start : start + size], tuple(x - start for x in rest[:6]), rest[6])
        if got != (len(blob), blob, offs, saved):
            r.fail(f"the {name} trampolines are not this compiler's (corrupt or forged file)")
        pos = start + size
    r.done()
    if pos != len(text):
        r.fail("bytes in .text after the last trampoline")
    return text[:first]


def _ident(b: bytes) -> None:
    if len(b) < 64 or b[:4] != b"\x7fELF" or b[4:7] != b"\x02\x01\x01" or b[7] != OSABI:
        raise ArtifactError("not a waboros artifact (ELF64 little-endian with OS/ABI 0xCB)")


def _sections_r(b: bytes) -> tuple[dict[str, bytes], int, int]:
    """The ELF container's checks; (payload by section name, e_machine, format version). The
    layout must be the writer's: sections in the order of the format, each at the next multiple of
    its alignment, zero gaps, the section header table at the next multiple of 8 and the end of
    the file, every header field as in the table of "File layout"."""
    _ident(b)
    if b[8] != FORMAT_VERSION:
        raise ArtifactError(f"unsupported format version {b[8]} (this reader reads 1)")
    if any(b[9:16]):
        raise ArtifactError("ELF header: identification padding is not zero")
    ty, mach, ver, entry, phoff, shoff, flags, ehs, phes, phn, shes, shn, shx = struct.unpack_from(
        "<HHIQQQIHHHHHH", b, 16
    )
    if (ty, ver, entry, phoff, flags, ehs, phes, phn, shes) != (1, 1, 0, 0, 0, 64, 0, 0, 64):
        raise ArtifactError("ELF header: a field is not the one of format v1")
    if mach not in (62, 183):
        raise ArtifactError(f"ELF header: unknown machine {mach}")
    if shn < 2 or shx != shn - 1 or shoff + 64 * shn > len(b) or shoff % 8:
        raise ArtifactError("ELF header: the section header table is not inside the file")
    if shoff + 64 * shn < len(b):
        raise ArtifactError("bytes after the section header table")
    heads = [struct.unpack_from("<IIQQQQIIQQ", b, shoff + 64 * i) for i in range(shn)]
    if any(heads[0]):
        raise ArtifactError("the null section header is not zero")
    for h in heads[1:]:
        if h[1] in (4, 9):
            raise ArtifactError("a relocation section: v1 has none")
    for h in heads[1:]:
        if h[4] + h[5] > shoff:
            raise ArtifactError("a section is not inside the file before the section headers")
    names = b[heads[shx][4] : heads[shx][4] + heads[shx][5]]
    order: list[str] = []
    for h in heads[1:]:
        end = names.find(b"\0", h[0])
        if end < 0:
            raise ArtifactError("a section name is not terminated")
        n = names[h[0] : end].decode("utf-8", "replace")
        if n in order:
            raise ArtifactError(f"section {n} appears twice")
        order.append(n)
    need = [".text" if n == "text" else f".waboros.{n}" for n in REQUIRED] + NAMES[-3:]
    if missing := [n for n in need if n not in order]:
        short = [n.lstrip(".").removeprefix("waboros.") for n in missing]
        raise ArtifactError(f"required sections missing: {', '.join(short)}")
    known = [n for n in order if n in NAMES]
    if (
        known != [n for n in NAMES if n in known]
        or order[:2] != NAMES[:2]
        or order[-1] != NAMES[-1]
    ):
        raise ArtifactError("the sections are not in the order of the format")
    shstr, so = _strtab(order)
    if names != shstr:
        raise ArtifactError(".shstrtab: not the names of the sections in order")
    pos, secs = 64, {}
    for n, h in zip(order, heads[1:], strict=True):
        text, plain = n == ".text", n.startswith(".waboros.")
        if n not in NAMES:  # unknown: skipped, never read (D62)
            al = h[8] or 1
            if (h[0], h[3], h[4] % al) != (so[n], 0, 0) or al & (al - 1) or h[4] < pos:
                raise ArtifactError(f"{n}: unknown section with a bad name, address, alignment"
                                    " or offset")  # fmt: skip
            if any(b[pos : h[4]]):
                raise ArtifactError(f"{n}: the padding before it is not zero")
            pos = h[4] + h[5]
            continue
        # `.waboros.*` flags and the symbol table's `sh_info` are not read (D62: a later amendment
        # may set them); no loader reads the symbols, so their contents are not checked either
        typ, fl, al, link, info, ent = (
            (1, 6, 4096, 0, 0, 0) if text else (1, h[2], 8, 0, 0, 0) if plain
            else (2, 0, 8, order.index(".strtab") + 1, h[7], 24) if n == ".symtab"
            else (3, 0, 1, 0, 0, 0)
        )  # fmt: skip
        at = pos + -pos % al
        if (h[:5], h[6:]) != ((so[n], typ, fl, 0, at), (link, info, al, ent)):
            raise ArtifactError(f"{n}: its header (name, type, flags, address, offset, link, info,"
                                f" alignment or entry size) is not the format's")  # fmt: skip
        if any(b[pos:at]):
            raise ArtifactError(f"{n}: the padding before it is not zero")
        if n == ".symtab" and h[5] % 24:
            raise ArtifactError(".symtab: the size is not a multiple of 24")
        pos = at + h[5]
        secs[n] = b[at:pos]
    if pos > shoff or any(b[pos:shoff]) or shoff != pos + -pos % 8:
        raise ArtifactError("the section header table does not follow the last section")
    out = {n[9:]: d for n, d in secs.items() if n.startswith(".waboros.")}
    out["text"], out["symtab"], out["strtab"] = secs[".text"], secs[".symtab"], secs[".strtab"]
    return out, mach, b[8]


def _head(b: bytes) -> tuple[dict[str, bytes], SimpleNamespace]:
    """Order steps 1-4: the container's checks and the header parsed exactly. The payloads by
    name, and the header's fields under the names of `ModuleView`."""
    s, mach, _ = _sections_r(b)
    h = _R(s["header"], ".waboros.header")
    msha, csha = h.take(32), h.take(32)
    abi, _, isa, _, flags, _ = h.u(2), h.zero(2), h.u(2), h.zero(2), h.u(4), h.zero(4)
    feats = h.u(8), h.u(8), h.u(8)
    compiler = h.text()
    h.done()
    parts = compiler.split(" ")
    if len(parts) != 4 or parts[0] != "waboros" or not parts[3].isdigit() or not compiler.isascii():
        h.fail("the compiler string is not 'waboros <version> <target> <target version>'")
    if TARGETS.get(parts[2], (0,))[0] != mach:
        h.fail(f"compiler target {parts[2]} does not match the ELF machine {mach}")
    return s, SimpleNamespace(
        target=parts[2], target_version=int(parts[3]), compiler=compiler, machine=mach,
        module_sha256=msha, config_sha256=csha, ctx_abi_version=abi, isa_level=isa, flags=flags,
        isa_features=feats[0], features_required=feats[1], features_optional=feats[2],
    )  # fmt: skip


def read(b: bytes) -> ModuleView:
    """Parse an artifact exactly; raises `ArtifactError` (a ValueError) naming the reason."""
    s, hd = _head(bytes(b))
    mach, msha, csha, abi, isa, flags = (hd.machine, hd.module_sha256, hd.config_sha256,
        hd.ctx_abi_version, hd.isa_level, hd.flags)  # fmt: skip
    feats = hd.isa_features, hd.features_required, hd.features_optional
    mod, _ = _parse_types(_R(s["types"], ".waboros.types"))
    env = _Env(mod["tdefs"], len(mod["tdefs"]))
    td, nt = env.tdefs, env.ntypes
    pend: list[int] = []
    # imports
    r = _R(s["imports"], ".waboros.imports")
    for k in ("imports", "ttypes", "taddr", "maddr", "funcs", "tables", "mems", "globals", "tags",
              "exports", "elem_info", "elem_mode", "datas", "data_mem", "tinit"):  # fmt: skip
        mod[k] = []
    for _ in range(r.count(9)):
        m, n, kind = r.text(), r.text(), r.u8()
        if kind > 4:
            r.fail(f"bad import kind {kind}")
        k = KINDS[kind]
        if k in ("func", "tag"):
            d = env.ftype(r)
            env.ftidx += [d] * (k == "func")
        elif k == "table":
            mod["ttypes"].append(_vt_r(r, nt, pend, True))
            mn, mx, a = _limits_r(r, 0)
            mod["taddr"].append(a)
            d = (mn, mx)
        elif k == "mem":
            mn, mx, a = _limits_r(r, 16)
            mod["maddr"].append(a)
            d = (mn, mx)
        else:
            d = (_vt_r(r, nt, pend), r.u8())
            if d[1] > 1:
                r.fail("mutability is not 0 or 1")
            env.gtypes.append(d[0])
            env.gmut.append(d[1])
        mod["imports"].append((k, m, n, d))
        env.nfuncs += k == "func"
        env.ntables += k == "table"
        env.nmems += k == "mem"
        env.nglobals += k == "global"
        env.ntags += k == "tag"
    r.done()
    nfimp, nimpg = env.nfuncs, env.nglobals
    # funcs
    r = _R(s["funcs"], ".waboros.funcs")
    data_start, start = r.u32(), r.u32()
    code = _trampolines(s["text"], s["trampolines"], hd.target)
    if data_start > len(code):
        r.fail("data_start is past the end of .text")
    funcs, last = [], -1
    for _ in range(r.count(8)):
        t, o = env.ftype(r), r.u32()
        if o <= last or o >= data_start:
            r.fail(f"code offset {o} is not increasing or not below data_start")
        last = o
        mod["funcs"].append(t)
        funcs.append((o, len(td[t][0][2]) + td[t][0][2].count(V128)))
    r.done()
    env.nfuncs += len(funcs)
    env.ftidx += mod["funcs"]
    if start != NO_START and start >= env.nfuncs:
        r.fail(f"start function {start} out of range")
    mod["start"] = None if start == NO_START else start
    r = _R(s["tags"], ".waboros.tags")
    mod["tags"] = [env.ftype(r) for _ in range(r.count(4))]
    r.done()
    env.ntags += len(mod["tags"])
    # context
    r = _R(s["context"], ".waboros.context")
    heap, sub = r.u8(), r.u8()
    r.zero(2)
    if heap > 1 or sub > 1 or (not heap and sub):
        r.fail("heap and subtype are not 0 or 1, or subtype without heap")

    def ids(what: str, n: int, size: int = 4, kind: tuple[str, ...] = ()) -> list[int]:
        v = [r.index(n, what) for _ in range(r.count(size))]
        if any(a >= b for a, b in zip(v, v[1:], strict=False)):
            r.fail(f"{what} indices are not strictly increasing")
        if kind and any(td[i][0][0] not in kind for i in v):
            r.fail(f"{what} of a type of another kind")
        return v

    records = ids("function", env.nfuncs)
    if records[:nfimp] != list(range(nfimp)):
        r.fail("a function import has no record")
    sigs = ids("type", nt)
    thunks = [r.u32() for _ in range(r.count(4))]
    if len(thunks) != nfimp or any(o >= data_start for o in thunks):
        r.fail("the thunks are not one per function import below data_start")
    rtts = ids("type", nt, kind=("struct", "array"))
    thrown = ids("tag", env.ntags)
    r.done()
    if not heap and (rtts or thrown):
        r.fail("rtts or thrown tags without the heap")
    env.records = records
    # tables, memories, globals
    r = _R(s["tables"], ".waboros.tables")
    env.gmax = nimpg  # a table's initial value reads imported globals only
    for _ in range(r.count(26)):
        mod["ttypes"].append(_vt_r(r, nt, pend, True))
        mn, mx, a = _limits_r(r, 0)
        mod["tables"].append((mn, mx))
        mod["taddr"].append(a)
        has = r.u8()
        if has > 1:
            r.fail("has_init is not 0 or 1")
        if not has and not T.nn(mod["ttypes"][-1])[0]:
            r.fail("a table of a non-nullable type needs an initial value")
        mod["tinit"].append(_cexpr_r(r, env, mod["ttypes"][-1]) if has else None)
    r.done()
    env.gmax = None
    env.ntables += len(mod["tables"])
    r = _R(s["memories"], ".waboros.memories")
    for _ in range(r.count(20)):
        mn, mx, a = _limits_r(r, 16)
        mod["mems"].append((mn, mx))
        mod["maddr"].append(a)
    r.done()
    env.nmems += len(mod["mems"])
    r = _R(s["globals"], ".waboros.globals")
    n = r.count(6)
    env.nglobals += n
    for k in range(n):
        env.gmax = nimpg + k  # imported globals and the earlier ones of this module
        t, m = _vt_r(r, nt, pend), r.u8()
        if m > 1:
            r.fail("mutability is not 0 or 1")
        mod["globals"].append((t, m, _cexpr_r(r, env, t)))
        env.gtypes.append(t)
        env.gmut.append(m)
    env.gmax = None
    r.done()
    r = _R(s["exports"], ".waboros.exports")
    lim = (env.nfuncs, env.ntables, env.nmems, env.nglobals, env.ntags)
    for _ in range(r.count(9)):
        n, k = r.text(), r.u8()
        if k > 4:
            r.fail(f"bad export kind {k}")
        mod["exports"].append((n, k, r.index(lim[k], KINDS[k])))
    r.done()
    # segments
    r = _R(s["segments"], ".waboros.segments")
    for _ in range(r.count(6)):
        m = r.u8()
        if m > 2:
            r.fail(f"bad element segment mode {m}")
        rt = _vt_r(r, nt, pend, True)
        ti, off = (r.index(env.ntables, "table"), None) if m == 0 else (None, None)
        if m == 0:
            if not env.fits(rt, mod["ttypes"][ti]):
                r.fail("the element type does not fit its table")
            off = _cexpr_r(r, env, mod["taddr"][ti])
        kind = r.u8()
        if kind > 1:
            r.fail("items is not 0 or 1")
        n = r.count(4 if kind == 0 else 6)
        if kind == 0:
            items = [(("func", env.func_ref(r)),) for _ in range(n)]
            if any(not env.fits((0, env.ftidx[i]), rt) for ((_, i),) in items):
                r.fail("a function does not fit the element type")
        else:
            items = [_cexpr_r(r, env, rt) for _ in range(n)]
            if all(e[-1][0] == "func" for e in items):
                r.fail("items 1 for function indices only: the writer uses items 0")
        mod["elem_info"].append((ti, off, rt, items))
        mod["elem_mode"].append(("active", "passive", "declarative")[m])
    for _ in range(r.count(5)):
        m = r.u8()
        if m > 1:
            r.fail(f"bad data segment mode {m}")
        mi, off = (r.index(env.nmems, "memory"), None) if m == 0 else (None, None)
        if m == 0:
            off = _cexpr_r(r, env, mod["maddr"][mi])
        mod["datas"].append((off, r.take(r.u32())))
        mod["data_mem"].append(mi)
    r.done()
    # stack maps and the fault map
    try:
        gc.decode(s["stackmaps"])
    except ValueError as e:
        raise ArtifactError(f".waboros.stackmaps: {e}") from None
    r = _R(s["traps"], ".waboros.traps")
    faults = [(r.u32(), r.u32()) for _ in range(r.count(8))]
    r.done()
    if faults and not flags & 1:
        r.fail("a fault map without BOUNDS_GUARD")
    fo = [f for f, _ in faults]
    if any(f >= data_start for f in fo) or any(a >= b for a, b in zip(fo, fo[1:], strict=False)):
        r.fail("fault offsets are not increasing and below data_start")
    wasm = s.get("wasm")
    if wasm is not None and hashlib.sha256(wasm).digest() != msha:
        raise ArtifactError(".waboros.wasm: its SHA-256 is not module_sha256")
    layout = Layout(
        memories=env.nmems, tables=env.ntables, globals=env.nglobals, imports=nfimp, sigs=len(sigs),
        records=len(records), tags=env.ntags, heap=bool(heap), rtts=len(rtts) + len(thrown),
        wide=tuple(nimpg + i for i, g in enumerate(mod["globals"]) if g[0] == V128),
        gcsvc=GC_SERVICES if heap else 0,
    )  # fmt: skip
    return ModuleView(
        hd.target, hd.target_version, hd.compiler, mach, msha, csha, abi, isa, *feats, flags, mod,
        code, data_start, funcs, layout, records, sigs, dict(enumerate(thunks)), rtts, thrown,
        bool(heap), bool(sub), faults, s["stackmaps"], wasm,
    )  # fmt: skip


# ------------------------------------------------------------------------------------- loader
def _frozen(b: bytes) -> tuple[bytes, bytes | None] | None:
    """(module_sha256, wasm or None) found as any format version would: through the ELF header,
    the section header table and the names (D62), without the layout rules of `read`; None where
    the file has no findable header section."""
    try:
        shoff = struct.unpack_from("<Q", b, 40)[0]
        shn, shx = struct.unpack_from("<HH", b, 60)
        if shoff + 64 * shn > len(b):
            return None
        heads = [struct.unpack_from("<IIQQQQIIQQ", b, shoff + 64 * i) for i in range(shn)]
        if any(h[4] + h[5] > len(b) for h in heads[1:]):
            return None
        names = b[heads[shx][4] : heads[shx][4] + heads[shx][5]]
        found: dict[bytes, bytes] = {}
        for h in heads[1:]:
            n = names[h[0] : names.index(b"\0", h[0])]
            if n in (b".waboros.header", b".waboros.wasm"):
                found[n] = b[h[4] : h[4] + h[5]]
        return found[b".waboros.header"][:32], found.get(b".waboros.wasm")
    except (struct.error, ValueError, KeyError, IndexError):
        return None


def _no(field: str, got: object, want: str) -> Any:
    raise ArtifactError(f".waboros.header: {field} is {got}, this loader {want}")


def _compatible(v: Any, target: str) -> None:
    """Order step 5 (S0014): the first failing check raises, naming the field and both values."""
    from waboros.targets.jit.common import guard

    t: Any = base.TARGETS[target]()
    mine: list[Any] = [base.TARGETS[n]() for n in TARGETS]  # the target of this host's machine
    mine = [x for x in mine if platform.machine().lower() in x.machines]
    if not mine or TARGETS[mine[0].name][0] != v.machine:
        _no(
            "e_machine", v.machine, f"runs {TARGETS[mine[0].name][0] if mine else 'no JIT machine'}"
        )
    if why := mine[0].foreign():  # a 32-bit process on a 64-bit kernel
        _no("e_machine", v.machine, f"cannot run it in this process ({why})")
    if v.ctx_abi_version != 1:
        _no("ctx_abi_version", v.ctx_abi_version, "reads 1")
    if v.compiler != compiler_string(target):
        _no("compiler", repr(v.compiler), f"requires {compiler_string(target)!r}")
    if v.isa_level != 1:
        _no("isa_level", v.isa_level, f"runs level 1 ({TARGETS[target][1]})")
    if v.isa_features:
        _no("isa_features", hex(v.isa_features), "knows none")
    log2 = v.flags >> 8 & 0xFF
    if v.flags & ~0xFF01 or bool(v.flags & 1) != bool(log2):
        _no("flags", hex(v.flags), "knows bit 0 and GUARD_LOG2 in bits 8-15 only, set together")
    if v.flags & guard.BOUNDS_GUARD:
        if log2 > guard.GUARD_LOG2:
            _no("GUARD_LOG2", log2, f"has a guard of 2^{guard.GUARD_LOG2} at most")
        if not t.guard_rows:
            _no("flags BOUNDS_GUARD", "set", f"has no guard mode for {target}")
        try:
            guard.install(t)
        except (RuntimeError, OSError) as e:
            _no("flags BOUNDS_GUARD", "set", f"cannot run guard code here ({e})")
        why = guard.broken()
        if why:
            _no("flags BOUNDS_GUARD", "set", f"cannot run guard code here ({why})")
    if v.features_required:
        _no("features_required", hex(v.features_required), "knows none")


def load(b: bytes, *, target: str) -> Any:
    """What `JitTarget.instantiate` takes, from the file `b` for the compiler `target`: the
    module view when every rule of "Semantics: the loader" holds, else, for a fat file (D18), the
    wasm compiled by this process; a thin file is refused with `ArtifactError`. A file that is not
    ours is refused either way, and so is a fat file whose wasm is not `module_sha256`."""
    if target not in TARGETS:
        raise ValueError(f"unknown target {target!r}")
    b = bytes(b)
    _ident(b)
    msha, wasm = _frozen(b) or (b"", None)
    if wasm is not None and hashlib.sha256(wasm).digest() != msha:
        raise ArtifactError(".waboros.wasm: its SHA-256 is not module_sha256 (corrupt file)")
    try:
        hd = _head(b)[1]
        _compatible(hd, target)
        return read(b)
    except Exception as e:  # a fat file is never refused for these steps (D18)
        if wasm is None:
            if isinstance(e, ArtifactError):
                raise
            raise ArtifactError(f"cannot load: {e!r}") from e
    from waboros.frontend.binary import parse
    from waboros.frontend.validate import validate

    try:
        m = parse(wasm)
        validate(m)
        return base.TARGETS[target]().compile(m)
    except Exception as e:
        raise ArtifactError(f".waboros.wasm: cannot be compiled by this process: {e}") from e
