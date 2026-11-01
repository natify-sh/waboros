"""The glue every `jit-<arch>` target shares (S0002, S0004): the probe (map a tiny function W^X and
call it), `supports`, `compile` through the one-pass core with the architecture's backend, and
`instantiate` with its trampolines. An architecture gives its machine names, the probe function,
its `Backend` and its trampolines.

Test-only: `W2PY_JIT_DUMP=<dir>` writes each compiled module's code as `<dir>/<sha1>.<data>.bin`,
where `<data>` is the offset of its data area (for tools/isa_check.py `--data`)."""

import ctypes
import hashlib
import os
import platform
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from w2py.targets.base import Extern, TargetInstance, ValidModule

if TYPE_CHECKING:
    from w2py.runtime.store import StoreState
    from w2py.targets.jit.common.instance import Trampolines


class JitTarget:
    name: str
    arch: str  # in messages: "x86-64", "aarch64"
    machines: tuple[str, ...]  # platform.machine().lower() values of this architecture
    version = 0
    complete = True  # False: `auto` does not select it (D25, S0004)

    def probe_code(self) -> bytes:
        """A function `int f(void)` returning 42."""
        raise NotImplementedError

    def backend(self) -> Any:
        raise NotImplementedError

    def trampolines(self) -> "Trampolines":
        raise NotImplementedError

    def probe(self) -> str | None:
        """None if this architecture's code can be mapped W^X and called here; else the reason."""
        if (
            platform.machine().lower() not in self.machines
            or sys.maxsize < 1 << 32
            or sys.byteorder != "little"
        ):
            bits = sys.maxsize.bit_length() + 1
            return f"not an {self.arch} process ({platform.machine()}, {bits} bit)"
        try:
            from w2py.targets.jit.common.execmem import load

            mem = load(self.probe_code())
            try:
                got = ctypes.CFUNCTYPE(ctypes.c_int)(mem.addr)()
            finally:
                mem.free()
        except Exception as e:
            return f"executable memory unavailable: {e!r}"
        return None if got == 42 else f"probe function returned {got}"

    def supports(self, module: ValidModule) -> str | None:
        from w2py.targets.jit.common.compiler import unsupported

        return unsupported(module, self.backend().supported)

    def compile(self, module: ValidModule) -> object:
        from w2py.targets.jit.common.compiler import compile_module

        c = compile_module(module, self.backend(), self.name)
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

        return JitInstance(store, self.trampolines(), cast(Compiled, compiled), imports, self.name)
