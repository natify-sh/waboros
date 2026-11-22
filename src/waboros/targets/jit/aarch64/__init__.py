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

    def conventions(self) -> dict[str, tuple[bytes, tuple[int, ...], int]]:
        return conventions()

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
def conventions() -> dict[str, tuple[bytes, tuple[int, ...], int]]:
    """The one convention's blob, offsets and ENTRY_SAVED (S0014 `.waboros.trampolines`). Until
    S0015 step 4 one blob holds the runtime's entry and sp_stub (entry.py) with this compiler's
    bridge and tail_switch (abi.py), in the order and with the offsets the format has: entry,
    bridge, sp_stub, tail_switch, the entry's return point, `restore`."""
    from waboros.targets.jit.aarch64 import abi, entry
    from waboros.targets.jit.aarch64.encoder import Asm

    a = Asm()
    ret = entry.entry(a)
    offs = [0, len(a.buf)]
    abi.bridge(a)
    offs.append(len(a.buf))
    entry.sp_stub(a)
    offs.append(len(a.buf))
    restore = abi.tail_switch(a)
    return {"aapcs64": (a.finish(), (*offs, ret, restore), entry.ENTRY_SAVED)}  # tuples: shared


@cache
def trampolines() -> "Trampolines":
    """This process's trampolines, sealed once by the host side."""
    from waboros.targets.jit.aarch64.abi import RESTORE_RET_AT, RESTORE_UP
    from waboros.targets.jit.common.instance import load_trampolines

    return load_trampolines(*conventions()["aapcs64"], (RESTORE_RET_AT, RESTORE_UP))
