"""The `jit-x86_64` target (S0002, S0003): the shared one-pass core (common/compiler.py) driving
the x86-64 backend; the ABI in abi.py and common/instance.py. A module with anything the core does
not compile yet is `Unsupported` with the first missing feature (D25).

Test-only: `W2PY_JIT_DUMP=<dir>` writes each compiled module's code as `<dir>/<sha1>.<data>.bin`,
where `<data>` is the offset of its data area (for tools/isa_check.py `--data`)."""

import ctypes
import hashlib
import os
import platform
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, cast

from w2py.targets.base import Extern, TargetInstance, ValidModule

if TYPE_CHECKING:
    from w2py.runtime.store import StoreState


class JitX86_64Target:
    name = "jit-x86_64"
    version = 0

    def probe(self) -> str | None:
        """None if x86-64 code can be mapped W^X and called here; else the reason."""
        if platform.machine().lower() not in ("x86_64", "amd64") or sys.maxsize < 1 << 32:
            return (
                f"not an x86-64 process ({platform.machine()}, {sys.maxsize.bit_length() + 1} bit)"
            )
        try:
            from w2py.targets.jit.common.execmem import load
            from w2py.targets.jit.x86_64.encoder import RAX, Asm

            a = Asm()
            a.endbr64()
            a.mov_imm(RAX, 42)
            a.ret()
            mem = load(a.finish())
            try:
                got = ctypes.CFUNCTYPE(ctypes.c_int)(mem.addr)()
            finally:
                mem.free()
        except Exception as e:
            return f"executable memory unavailable: {e!r}"
        return None if got == 42 else f"probe function returned {got}"

    def supports(self, module: ValidModule) -> str | None:
        from w2py.targets.jit.common.compiler import unsupported

        return unsupported(module)

    def compile(self, module: ValidModule) -> object:
        from w2py.targets.jit.common.compiler import compile_module
        from w2py.targets.jit.x86_64.backend import Backend

        c = compile_module(module, Backend(), self.name)
        dump = os.environ.get("W2PY_JIT_DUMP")
        if dump:
            name = f"{hashlib.sha1(c.code).hexdigest()}.{c.data_start}.bin"
            (Path(dump) / name).write_bytes(c.code)
        return c

    def instantiate(
        self, compiled: object, imports: Sequence[Extern], store: "StoreState"
    ) -> TargetInstance:
        from w2py.targets.jit.common.compiler import Compiled
        from w2py.targets.jit.common.instance import JitInstance
        from w2py.targets.jit.x86_64.abi import trampolines

        return JitInstance(store, trampolines(), cast(Compiled, compiled), imports, self.name)
