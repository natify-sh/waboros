"""`jit-sljit-<arch>`, the development second compiler (docs/specs/S0016): our one-pass core with
sljit as the encoder (backend.py), through `native/`'s library. Importing this module registers
the host's target in `base.TARGETS` when the library exists (`WABOROS_SLJIT_LIB`, a file or a
directory; default `build/native/`); without it the target is absent and its tests skip. A library
older than any `native/` source is refused (rebuild: `cmake --build build/native`). Never shipped:
the package does not name it. Linux glibc only: x86-64 (step 1) and aarch64 (step 5; under QEMU
the cross build `build/native-a64` is found by its ELF machine)."""

import os
import platform
import sys
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from waboros.targets import base
from waboros.targets.jit import aarch64 as _a64
from waboros.targets.jit import x86_64 as _x64
from waboros.targets.jit.aarch64 import JitAarch64Target
from waboros.targets.jit.x86_64 import JitX86_64Target

if TYPE_CHECKING:
    from waboros.targets.jit.common.instance import Trampolines

ROOT = Path(__file__).resolve().parents[2]
NAME = "libwaboros_sljit.so"


MACHINE = {"x86_64": 62, "amd64": 62, "aarch64": 183, "arm64": 183}.get(platform.machine().lower())


def _machine(p: Path) -> int:
    with p.open("rb") as f:
        return int.from_bytes(f.read(20)[18:20], "little")  # ELF e_machine


def _library() -> Path | None:
    """The library named by WABOROS_SLJIT_LIB, else the first of build/native, build/native-a64
    built for this machine (its ELF e_machine)."""
    env = os.environ.get("WABOROS_SLJIT_LIB")
    dirs = [Path(env)] if env else [ROOT / "build" / "native", ROOT / "build" / "native-a64"]
    found = [d / NAME if d.is_dir() else d for d in dirs]
    found = [p for p in found if p.is_file() and _machine(p) == MACHINE]
    if not found:
        return None
    p = found[0]
    native = ROOT / "native"
    sources = [native / "CMakeLists.txt", *(native / "sljit").iterdir()]
    sources += (native / "third_party" / "sljit" / "sljit_src").glob("*")
    newest = max((s for s in sources if s.is_file()), key=lambda s: s.stat().st_mtime)
    if newest.stat().st_mtime > p.stat().st_mtime:
        raise RuntimeError(f"{p} is older than {newest}: rebuild it (cmake --build build/native)")
    return p


LIB = _library()


class _Sljit:
    """What both `jit-sljit-<arch>` targets share; the architecture's JitTarget follows in the
    bases (its runtime blob, `centry` reader, guard rows of its own)."""

    name: str
    version = 1
    complete = False  # `auto` never selects it
    development = True  # not a package target: no FEATURES.md column (S0016)
    guard_rows = frozenset({"linux-glibc"})
    verified = frozenset({"linux-glibc"})
    _conventions: tuple[str, ...]

    def foreign(self) -> str | None:
        from waboros.targets.jit.common.guard import host_row

        why = cast("str | None", super().foreign())  # type: ignore[misc]
        if why is None and host_row() != "linux-glibc":
            why = f"{self.name} runs on Linux glibc only (S0016), not {host_row()}"
        return why

    def backend(self) -> Any:
        from tests.sljit.backend import Backend

        return Backend()

    def guard_backend(self) -> Any:
        from tests.sljit.backend import Backend

        return Backend(guard=True)

    def conventions(self) -> dict[str, tuple[bytes, tuple[int, ...], tuple[int, int]]]:
        from tests.sljit.backend import trampoline_blob

        return {c: trampoline_blob(c) for c in self._conventions}

    def digest_sources(self) -> list[Path]:
        native = ROOT / "native"
        files = [*(ROOT / "tests" / "sljit").glob("*.py"), native / "CMakeLists.txt"]
        files += (native / "sljit").glob("*")
        files += (native / "third_party" / "sljit" / "sljit_src").glob("*")
        return [f for f in files if f.is_file()]

    def trampolines(self) -> "Trampolines":
        return _trampolines(type(self))

    def compile(self, module: Any) -> object:
        """As `JitTarget.compile` (guard mode, `WABOROS_JIT_DUMP`); the backend's positions are
        sljit labels until `finish`, so the C entries of functions and thunks become offsets
        here. The backend's sljit compiler is freed on every path."""
        import hashlib

        from waboros.targets.jit.common import guard
        from waboros.targets.jit.common.compiler import compile_module

        g = guard.wanted(self)  # type: ignore[arg-type]
        b = self.guard_backend() if g else self.backend()
        try:
            c = compile_module(module, b, self.name)
        finally:
            b.close()  # `finish` freed it unless something raised before
        c.funcs = [(b.offset[p], n) for p, n in c.funcs]
        c.thunks = {i: b.offset[k] for i, k in c.thunks.items()}
        if g:
            c.flags, c.faults = guard.FLAGS, b.faults
        dump = os.environ.get("WABOROS_JIT_DUMP")
        if dump:
            name = f"{hashlib.sha1(c.code).hexdigest()}.{c.data_start}.bin"
            (Path(dump) / name).write_bytes(c.code)
        return c


class JitSljitX86_64Target(_Sljit, JitX86_64Target):
    name = "jit-sljit-x86_64"
    xcall = True  # another context's body through the xcall helper (a frame of its own)
    _conventions = ("sysv", "win64")  # S0014's x86-64 conventions
    _runtime = staticmethod(_x64.runtime)


class JitSljitAarch64Target(_Sljit, JitAarch64Target):
    name = "jit-sljit-aarch64"
    xcall = False  # S0 pushed around that call in the body (S0016)
    _conventions = ("aapcs64",)
    _runtime = staticmethod(_a64.runtime)


@cache
def _trampolines(t: type) -> "Trampolines":
    """This target's own copy of the runtime's blob and of its bridge and tail_switch (S0016:
    word 8 is this compiler's, never another target's)."""
    from waboros.targets.jit.common.instance import load_trampolines

    x = t()
    return load_trampolines(x._runtime(), x.conventions()[x._conventions[0]])


if LIB is not None and sys.platform == "linux":
    for _t in (JitSljitX86_64Target, JitSljitAarch64Target):
        if platform.machine().lower() in _t.machines:
            base.TARGETS[_t.name] = _t
