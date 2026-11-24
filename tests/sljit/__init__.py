"""`jit-sljit-<arch>`, the development second compiler (docs/specs/S0016): our one-pass core with
sljit as the encoder (backend.py), through `native/`'s library. Importing this module registers
the host's target in `base.TARGETS` when the library exists (`WABOROS_SLJIT_LIB`, a file or a
directory; default `build/native/`); without it the target is absent and its tests skip. A library
older than any `native/` source is refused (rebuild: `cmake --build build/native`). Never shipped:
the package does not name it. Step 1: x86-64 on Linux glibc only."""

import os
import platform
import sys
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from waboros.targets import base
from waboros.targets.jit.x86_64 import JitX86_64Target, runtime

if TYPE_CHECKING:
    from waboros.targets.jit.common.instance import Trampolines

ROOT = Path(__file__).resolve().parents[2]
NAME = "libwaboros_sljit.so"


def _library() -> Path | None:
    p = Path(os.environ.get("WABOROS_SLJIT_LIB", ROOT / "build" / "native"))
    if p.is_dir():
        p = p / NAME
    if not p.is_file():
        return None
    native = ROOT / "native"
    sources = [native / "CMakeLists.txt", *(native / "sljit").iterdir()]
    sources += (native / "third_party" / "sljit" / "sljit_src").glob("*")
    newest = max((s for s in sources if s.is_file()), key=lambda s: s.stat().st_mtime)
    if newest.stat().st_mtime > p.stat().st_mtime:
        raise RuntimeError(f"{p} is older than {newest}: rebuild it (cmake --build build/native)")
    return p


LIB = _library()


class JitSljitX86_64Target(JitX86_64Target):
    name = "jit-sljit-x86_64"
    version = 1
    complete = False  # `auto` never selects it
    development = True  # not a package target: no FEATURES.md column (S0016)
    guard_rows = frozenset({"linux-glibc"})
    verified = frozenset({"linux-glibc"})

    def foreign(self) -> str | None:
        from waboros.targets.jit.common.guard import host_row

        why = super().foreign()
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

        return {"sysv": trampoline_blob()}  # win64: step 2

    def trampolines(self) -> "Trampolines":
        return _trampolines()

    def compile(self, module: Any) -> object:
        """As `JitTarget.compile` (guard mode, `WABOROS_JIT_DUMP`); the backend's positions are
        sljit labels until `finish`, so the C entries of functions and thunks become offsets
        here. The backend's sljit compiler is freed on every path."""
        import hashlib

        from waboros.targets.jit.common import guard
        from waboros.targets.jit.common.compiler import compile_module

        g = guard.wanted(self)
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


@cache
def _trampolines() -> "Trampolines":
    """This target's own copy of the runtime's blob and of its bridge and tail_switch (S0016:
    word 8 is this compiler's, never another target's)."""
    from waboros.targets.jit.common.instance import load_trampolines

    return load_trampolines(runtime(), JitSljitX86_64Target().conventions()["sysv"])


if (
    LIB is not None
    and sys.platform == "linux"
    and platform.machine().lower() in ("x86_64", "amd64")
):
    base.TARGETS[JitSljitX86_64Target.name] = JitSljitX86_64Target
