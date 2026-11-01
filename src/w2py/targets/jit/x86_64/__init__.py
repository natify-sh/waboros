"""The `jit-x86_64` target (S0002, S0003): the shared one-pass core (common/compiler.py) driving
the x86-64 backend; the ABI in abi.py and common/instance.py; the glue in common/target.py. A
module with anything the core does not compile yet is `Unsupported` with the first missing
feature (D25)."""

import sys
from functools import cache
from typing import TYPE_CHECKING, Any

from w2py.targets.jit.common.target import JitTarget

if TYPE_CHECKING:
    from w2py.targets.jit.common.instance import Trampolines


class JitX86_64Target(JitTarget):
    name = "jit-x86_64"
    arch = "x86-64"
    machines = ("x86_64", "amd64")

    def probe_code(self) -> bytes:
        from w2py.targets.jit.x86_64.encoder import RAX, Asm

        a = Asm()
        a.endbr64()
        a.mov_imm(RAX, 42)
        a.ret()
        return a.finish()

    def backend(self) -> Any:
        from w2py.targets.jit.x86_64.backend import Backend

        return Backend()

    def trampolines(self) -> "Trampolines":
        return trampolines()


@cache
def trampolines() -> "Trampolines":
    """This process's trampolines, assembled by abi.py and sealed once by the host side."""
    from w2py.targets.jit.common.instance import load_trampolines
    from w2py.targets.jit.x86_64.abi import assemble

    return load_trampolines(*assemble(sys.platform == "win32"))
