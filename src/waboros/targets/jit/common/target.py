"""The glue every `jit-<arch>` target shares (S0002, S0004): the probe (map a tiny function W^X and
call it), `supports`, `compile` through the one-pass core with the architecture's backend, and
`instantiate` with its trampolines. An architecture gives its machine names, the probe function,
its `Backend` and its trampolines; one with guard pages (S0006) also its guard-mode backend, the
fault handler's code and the host rows it has a handler for (`guard_rows`) and where that handler
is verified (`verified`: `auto` picks guard only there).

Test-only: `WABOROS_JIT_DUMP=<dir>` writes each compiled module's code as `<dir>/<sha1>.<data>.bin`,
where `<data>` is the offset of its data area (for tools/isa_check.py `--data`)."""

import ctypes
import hashlib
import os
import platform
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from waboros.targets.base import Extern, TargetInstance, ValidModule

if TYPE_CHECKING:
    from waboros.runtime.store import StoreState
    from waboros.targets.jit.common.instance import Trampolines


class JitTarget:
    name: str
    arch: str  # in messages: "x86-64", "aarch64"
    machines: tuple[str, ...]  # platform.machine().lower() values of this architecture
    version = 0
    complete = True  # False: `auto` does not select it (D25, S0004)
    guard_rows: frozenset[str] = frozenset()  # host rows with a fault handler (S0006)
    verified: frozenset[str] = frozenset()  # rows where WABOROS_JIT_BOUNDS=auto gives guard

    def probe_code(self) -> bytes:
        """A function `int f(void)` returning 42."""
        raise NotImplementedError

    def backend(self) -> Any:
        raise NotImplementedError

    def trampolines(self) -> "Trampolines":
        raise NotImplementedError

    def guard_backend(self) -> Any:
        """The backend in guard mode (S0006); only where `guard_rows` is not empty."""
        raise NotImplementedError

    def fault_handler(
        self, tables: int, prev: int, sigaction: int, reraise: int, darwin: bool = False
    ) -> bytes:
        """The fault handler's code for this host (S0006, x86_64/abi.py `fault_handler`).
        `darwin`: macOS's signal context and two saved actions at `prev`, SIGSEGV's then
        SIGBUS's (step 3)."""
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
            from waboros.targets.jit.common.execmem import load

            mem = load(self.probe_code())
            try:
                got = ctypes.CFUNCTYPE(ctypes.c_int)(mem.addr)()
            finally:
                mem.free()
        except Exception as e:
            return f"executable memory unavailable: {e!r}"
        return None if got == 42 else f"probe function returned {got}"

    def supports(self, module: ValidModule) -> str | None:
        from waboros.targets.jit.common.compiler import unsupported

        return unsupported(module, self.backend().supported)

    def compile(self, module: ValidModule) -> object:
        from waboros.targets.jit.common import guard
        from waboros.targets.jit.common.compiler import compile_module

        if guard.wanted(self):
            b = self.guard_backend()
            c = compile_module(module, b, self.name)
            c.flags, c.faults = guard.FLAGS, b.faults
        else:
            c = compile_module(module, self.backend(), self.name)
        dump = os.environ.get("WABOROS_JIT_DUMP")
        if dump:
            name = f"{hashlib.sha1(c.code).hexdigest()}.{c.data_start}.bin"
            (Path(dump) / name).write_bytes(c.code)
        return c

    def instantiate(
        self, compiled: object, imports: Sequence[Extern], store: "StoreState"
    ) -> TargetInstance:
        from waboros.targets.jit.common import guard
        from waboros.targets.jit.common.compiler import Compiled
        from waboros.targets.jit.common.instance import JitInstance

        if cast(Compiled, compiled).flags & guard.BOUNDS_GUARD:
            guard.install(self)  # before anything of the instance exists; raises where none
        return JitInstance(store, self.trampolines(), cast(Compiled, compiled), imports, self.name)
