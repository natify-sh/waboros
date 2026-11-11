"""The `jit-aarch64` target (S0004): ARMv8.0-A on Linux, macOS and Windows (one code for all,
D23), glue in common/target.py. Since step 4e it compiles what `jit-x86_64` compiles and fails
exactly what `pyc` fails, so it is `complete`: `auto` selects it on arm64 machines (D25)."""

from functools import cache
from typing import TYPE_CHECKING, Any

from waboros.targets.jit.common.target import JitTarget

if TYPE_CHECKING:
    from waboros.targets.jit.common.instance import Trampolines


class JitAarch64Target(JitTarget):
    name = "jit-aarch64"
    arch = "aarch64"
    machines = ("aarch64", "arm64")
    complete = True

    def probe_code(self) -> bytes:
        from waboros.targets.jit.aarch64.encoder import Asm

        a = Asm()
        a.bti("c")
        a.mov_imm(0, 42, wide=False)
        a.ret()
        return a.finish()

    def backend(self) -> Any:
        from waboros.targets.jit.aarch64.backend import Backend

        return Backend()

    def trampolines(self) -> "Trampolines":
        return trampolines()

    # S0006 steps 2, 3, 4
    guard_rows = frozenset({"linux-glibc", "darwin", "win32"})
    # `auto` gives guard here (D44; CI `arm64`, `jit`;
    # darwin: CI run 41610392553, `arm64 (macos-latest)`;
    # win32: CI run 41633456821, `arm64 (windows-11-arm)`)
    verified = frozenset({"linux-glibc", "darwin", "win32"})

    def guard_backend(self) -> Any:
        from waboros.targets.jit.aarch64.backend import Backend

        return Backend(guard=True)

    def fault_handler(
        self,
        tables: int,
        prev: int,
        sigaction: int,
        reraise: int,
        darwin: bool = False,
        windows: bool = False,
        bionic: bool = False,
    ) -> bytes:
        from waboros.targets.jit.aarch64.abi import fault_handler
        from waboros.targets.jit.aarch64.encoder import Asm

        a = Asm()
        fault_handler(a, tables, prev, sigaction, reraise, darwin=darwin, windows=windows)
        return a.finish()


@cache
def trampolines() -> "Trampolines":
    """This process's trampolines, assembled by abi.py and sealed once by the host side."""
    from waboros.targets.jit.aarch64.abi import ENTRY_SAVED, assemble
    from waboros.targets.jit.common.instance import load_trampolines

    return load_trampolines(*assemble(), ENTRY_SAVED)
