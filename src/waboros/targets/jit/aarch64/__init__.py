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


@cache
def trampolines() -> "Trampolines":
    """This process's trampolines, assembled by abi.py and sealed once by the host side."""
    from waboros.targets.jit.aarch64.abi import assemble
    from waboros.targets.jit.common.instance import load_trampolines

    return load_trampolines(*assemble())
