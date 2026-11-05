"""The `jit-x86_64` target (S0002, S0003): the shared one-pass core (common/compiler.py) driving
the x86-64 backend; the ABI in abi.py and common/instance.py; the glue in common/target.py. A
module with anything the core does not compile yet is `Unsupported` with the first missing
feature (D25)."""

import sys
from functools import cache
from typing import TYPE_CHECKING, Any

from waboros.targets.jit.common.target import JitTarget

if TYPE_CHECKING:
    from waboros.targets.jit.common.instance import Trampolines


class JitX86_64Target(JitTarget):
    name = "jit-x86_64"
    arch = "x86-64"
    machines = ("x86_64", "amd64")

    def probe_code(self) -> bytes:
        from waboros.targets.jit.x86_64.encoder import RAX, Asm

        a = Asm()
        a.endbr64()
        a.mov_imm(RAX, 42)
        a.ret()
        return a.finish()

    def backend(self) -> Any:
        from waboros.targets.jit.x86_64.backend import Backend

        return Backend()

    def trampolines(self) -> "Trampolines":
        return trampolines()

    # S0006 steps 1, 3, 4, 5 and musl
    guard_rows = frozenset({"linux-glibc", "linux-musl", "darwin", "win32", "android"})
    # `auto` gives guard here (D44; CI `jit`, glibc and musl;
    # darwin: CI run 41610392553, `os (macos-26-intel)`;
    # win32: CI run 41640149866, `os (windows-latest)`;
    # android: CI run 41708608127, the `Android` cell of platforms.yml, x86_64 emulator, step 5)
    verified = frozenset({"linux-glibc", "linux-musl", "darwin", "win32", "android"})

    def guard_backend(self) -> Any:
        from waboros.targets.jit.x86_64.backend import Backend

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
        from waboros.targets.jit.x86_64.abi import fault_handler
        from waboros.targets.jit.x86_64.encoder import Asm

        a = Asm()
        fault_handler(
            a, tables, prev, sigaction, reraise, darwin=darwin, windows=windows, bionic=bionic
        )
        return a.finish()


@cache
def trampolines() -> "Trampolines":
    """This process's trampolines, assembled by abi.py and sealed once by the host side."""
    from waboros.targets.jit.common.instance import load_trampolines
    from waboros.targets.jit.x86_64.abi import assemble

    return load_trampolines(*assemble(sys.platform == "win32"))
