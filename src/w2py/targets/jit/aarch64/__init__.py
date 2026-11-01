"""The `jit-aarch64` target (S0004): ARMv8.0-A on Linux, macOS and Windows (one code for all,
D23), glue in common/target.py. Phase 4 compiles the integer core (the backend's `supported`); a
module using anything else is `Unsupported`, and `complete` is False: `auto` keeps `pyc` (D25,
D31) until step 4e."""

from typing import TYPE_CHECKING, Any

from w2py.targets.jit.common.target import JitTarget

if TYPE_CHECKING:
    from w2py.targets.jit.common.instance import Trampolines


class JitAarch64Target(JitTarget):
    name = "jit-aarch64"
    arch = "aarch64"
    machines = ("aarch64", "arm64")
    complete = False

    def probe_code(self) -> bytes:
        from w2py.targets.jit.aarch64.encoder import Asm

        a = Asm()
        a.bti("c")
        a.mov_imm(0, 42, wide=False)
        a.ret()
        return a.finish()

    def backend(self) -> Any:
        from w2py.targets.jit.aarch64.backend import Backend

        return Backend()

    def trampolines(self) -> "Trampolines":
        from w2py.targets.jit.aarch64.abi import trampolines

        return trampolines()
