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
    e_machine, isa_name = 183, "armv8.0-a"  # the artifact's ELF machine and level-1 ISA (S0014)
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

    def conventions(self) -> dict[str, tuple[bytes, tuple[int, ...], tuple[int, int]]]:
        return conventions()

    def runtime(self) -> tuple[bytes, tuple[int, int], int]:
        return runtime()

    def centry(self, code: bytes, stub: int) -> int:
        w = int.from_bytes(code[stub + 8 : stub + 12], "little")  # bti jc; adr x16; b centry
        if w >> 26 != 5:  # b imm26
            return -1
        return stub + 8 + 4 * ((w & 0x3FFFFFF) - ((w & 0x2000000) << 1))

    convention = "aapcs64"  # Linux, macOS and Windows alike

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
def conventions() -> dict[str, tuple[bytes, tuple[int, ...], tuple[int, int]]]:
    """The compiler's blob (S0014 `.waboros.trampolines`, S0015): this compiler's bridge then
    tail_switch (abi.py), the offsets of bridge, tail_switch and `restore` and the restore
    frame's two numbers."""
    from waboros.targets.jit.aarch64 import abi
    from waboros.targets.jit.aarch64.encoder import Asm

    a = Asm()
    abi.bridge(a)
    tail = len(a.buf)
    restore = abi.tail_switch(a)
    return {"aapcs64": (a.finish(), (0, tail, restore), (abi.RESTORE_RET_AT, abi.RESTORE_UP))}


@cache
def runtime() -> tuple[bytes, tuple[int, int], int]:
    """The runtime's entry then sp_stub (entry.py), the offsets of the entry's return point and of
    sp_stub, and ENTRY_SAVED: not in any artifact (S0015)."""
    from waboros.targets.jit.aarch64 import entry
    from waboros.targets.jit.aarch64.encoder import Asm

    a = Asm()
    ret = entry.entry(a)
    stub = len(a.buf)
    entry.sp_stub(a)
    return a.finish(), (ret, stub), entry.ENTRY_SAVED


@cache
def trampolines() -> "Trampolines":
    """This process's trampolines, sealed once by the host side."""
    from waboros.targets.jit.common.instance import load_trampolines

    return load_trampolines(runtime(), conventions()["aapcs64"])
