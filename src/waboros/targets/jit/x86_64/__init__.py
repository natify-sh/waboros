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

    def conventions(self) -> dict[str, tuple[bytes, tuple[int, ...], int]]:
        return conventions()

    convention = "win64" if sys.platform == "win32" else "sysv"

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
def conventions() -> dict[str, tuple[bytes, tuple[int, ...], int]]:
    """Every convention's blob, offsets and ENTRY_SAVED, on any host: what an artifact carries
    (S0014 `.waboros.trampolines`) and what this process runs. Until S0015 step 4 one blob holds
    the runtime's entry and sp_stub (entry.py) with this compiler's bridge and tail_switch
    (abi.py), in the order and with the offsets the format has: entry, bridge, sp_stub,
    tail_switch, the entry's return point, `restore`."""
    from waboros.targets.jit.x86_64 import abi, entry
    from waboros.targets.jit.x86_64.encoder import Asm

    out: dict[str, tuple[bytes, tuple[int, ...], int]] = {}
    for name, win64 in (("sysv", False), ("win64", True)):
        a = Asm()
        ret = entry.entry(a, win64)
        offs = [0, len(a.buf)]
        abi.bridge(a, win64)
        offs.append(len(a.buf))
        entry.sp_stub(a)
        offs.append(len(a.buf))
        restore = abi.tail_switch(a)
        out[name] = (a.finish(), (*offs, ret, restore), entry.ENTRY_SAVED)  # tuples: shared
    return out


@cache
def trampolines() -> "Trampolines":
    """This process's trampolines, sealed once by the host side."""
    from waboros.targets.jit.common.instance import load_trampolines
    from waboros.targets.jit.x86_64.abi import RESTORE_RET_AT, RESTORE_UP

    blob, offsets, saved = conventions()[JitX86_64Target.convention]
    return load_trampolines(blob, offsets, saved, (RESTORE_RET_AT, RESTORE_UP))
