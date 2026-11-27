# pyright: basic
"""The ctypes shim over `waboros_loader_shared` (S0017 "The harness"), reader part: an engine that
loads bytes and dumps them. The library is `build/native/libwaboros_loader_shared.so` (or
`WABOROS_LOADER_LIB`, a file or a directory, as `tests/sljit` finds its library: by ELF machine,
refused when older than a `native/` source). The test compiler is a callback into the Python
compiler of a target (the compiler seam, `compiler.h`), until the embedded one exists (D68)."""

import ctypes
import functools
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tests.sljit import MACHINE, ROOT, _machine
from waboros.frontend.binary import parse
from waboros.frontend.validate import validate
from waboros.targets import base
from waboros.targets.jit.common import artifact as A

NAME = "libwaboros_loader_shared.so"
OK, NOT_OURS, REFUSED, NEEDS_COMPILER, E_INTERNAL = 0, 1, 2, 3, 16


def _library() -> Path | None:
    env = os.environ.get("WABOROS_LOADER_LIB")
    dirs = [Path(env)] if env else [ROOT / "build" / "native", ROOT / "build" / "native-a64"]
    found = [d / NAME if d.is_dir() else d for d in dirs]
    found = [p for p in found if p.is_file() and _machine(p) == MACHINE]
    if not found:
        return None
    native = ROOT / "native"
    sources = [native / "CMakeLists.txt", *(native / "loader").iterdir()]
    newest = max((s for s in sources if s.is_file()), key=lambda s: s.stat().st_mtime)
    if newest.stat().st_mtime > found[0].stat().st_mtime:
        raise RuntimeError(
            f"{found[0]} is older than {newest}: rebuild it (cmake --build build/native)"
        )
    return found[0]


LIB = _library()


class _Error(ctypes.Structure):  # waboros_error
    _fields_ = [
        ("code", ctypes.c_int32),
        ("check", ctypes.c_char * 4),
        ("section", ctypes.c_char * 24),
        ("trap", ctypes.c_uint32),
        ("tag", ctypes.c_uint64),
        ("npayload", ctypes.c_uint32),
        ("msg", ctypes.c_char * 200),
    ]


_U8P = ctypes.POINTER(ctypes.c_uint8)
_SIZEP = ctypes.POINTER(ctypes.c_size_t)
_COMPILE = ctypes.CFUNCTYPE(
    ctypes.c_int, ctypes.c_void_p, _U8P, ctypes.c_size_t, ctypes.c_uint32,
    ctypes.POINTER(_U8P), _SIZEP, ctypes.POINTER(_Error),
)  # fmt: skip
_RELEASE = ctypes.CFUNCTYPE(None, ctypes.c_void_p, _U8P)


class _Compiler(ctypes.Structure):  # waboros_compiler
    _fields_ = [
        ("e_machine", ctypes.c_uint16),
        ("compiler", ctypes.c_char_p),
        ("target", ctypes.c_char_p),
        ("compile", _COMPILE),
        ("release", _RELEASE),
        ("self", ctypes.c_void_p),
    ]


@functools.cache
def _lib() -> ctypes.CDLL:
    assert LIB is not None
    lib, vp = ctypes.CDLL(str(LIB)), ctypes.c_void_p
    lib.waboros_engine_new.argtypes = [vp, vp, vp]
    lib.waboros_engine_free.argtypes = [vp]
    lib.waboros_module_load.argtypes = [vp, _U8P, ctypes.c_size_t, vp, ctypes.POINTER(_Error)]
    lib.waboros_module_dump.argtypes = [vp, ctypes.c_char_p, _SIZEP]
    lib.waboros_module_free.argtypes = [vp]
    return lib


@dataclass
class Result:
    code: int
    check: str
    section: str
    msg: str
    dump: str | None  # the inventory dump of an accepted file


_THIN: dict[tuple[bytes, str], bytes] = {}


def compile_thin(wasm: bytes, target: str) -> bytes:
    """What the test compiler returns for a fat file's wasm: the thin file of the Python compiler
    (what `artifact.load` regenerates); the one definition of "compiled", for C and for Python."""
    key = hashlib.sha256(wasm).digest(), target
    if key not in _THIN:
        m = parse(wasm)
        validate(m)
        c: Any = base.TARGETS[target]().compile(m)
        _THIN[key] = A.write(c, target=target, module_sha256=key[0])
    return _THIN[key]


class Engine:
    """A `waboros_engine`; `target` None: no compiler, else the test compiler of that target."""

    def __init__(self, target: str | None = None) -> None:
        self.lib, self.kept, self.comp = _lib(), {}, None
        if target is not None:
            compile_ = _COMPILE(
                lambda _s, w, n, _f, o, on, e: self._compile(target, w, n, o, on, e)
            )
            release = _RELEASE(lambda _s, p: self.kept.pop(ctypes.addressof(p.contents), None))
            name = A.compiler_string(target).encode()
            # kept alive: the engine copies the struct, the callbacks must outlive it
            self.comp = _Compiler(MACHINE, name, target.encode(), compile_, release, None)
        self.eng = ctypes.c_void_p()
        comp = ctypes.byref(self.comp) if self.comp else None
        assert (
            self.lib.waboros_engine_new(comp, ctypes.byref(self.eng), ctypes.byref(_Error())) == OK
        )

    def _compile(self, target: str, wasm: Any, n: int, out: Any, outn: Any, e: Any) -> int:
        try:
            thin = compile_thin(ctypes.string_at(wasm, n), target)
        except BaseException as x:  # an exception must not cross the callback
            e.contents.code, e.contents.msg = E_INTERNAL, repr(x).encode()[:199]
            return E_INTERNAL
        buf = (ctypes.c_uint8 * len(thin)).from_buffer_copy(thin)
        self.kept[ctypes.addressof(buf)] = buf
        out[0], outn[0] = ctypes.cast(buf, _U8P), len(thin)
        return 0

    def load(self, b: bytes) -> Result:
        buf = (ctypes.c_uint8 * len(b)).from_buffer_copy(b)
        mod, err = ctypes.c_void_p(), _Error()
        st = self.lib.waboros_module_load(
            self.eng, buf, len(b), ctypes.byref(mod), ctypes.byref(err)
        )
        if st != OK:
            return Result(st, err.check.decode(), err.section.decode(), err.msg.decode(), None)
        n = ctypes.c_size_t(0)
        self.lib.waboros_module_dump(mod, None, ctypes.byref(n))
        text = ctypes.create_string_buffer(n.value + 1)
        n = ctypes.c_size_t(n.value + 1)
        assert self.lib.waboros_module_dump(mod, text, ctypes.byref(n)) == OK
        self.lib.waboros_module_free(mod)
        return Result(OK, "", "", "", text.raw[: n.value].decode())

    def close(self) -> None:
        self.lib.waboros_engine_free(self.eng)
