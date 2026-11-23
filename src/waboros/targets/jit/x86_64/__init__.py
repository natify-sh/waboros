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

    def conventions(self) -> dict[str, tuple[bytes, tuple[int, ...], tuple[int, int]]]:
        return conventions()

    def runtime(self) -> tuple[bytes, tuple[int, int], int]:
        return runtime()

    def centry(self, code: bytes, stub: int) -> int:
        if code[stub + 11 : stub + 12] != b"\xe9":  # endbr64; lea rdx, [rip + 5]; jmp rel32
            return -1
        return stub + 16 + int.from_bytes(code[stub + 12 : stub + 16], "little", signed=True)

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
def conventions() -> dict[str, tuple[bytes, tuple[int, ...], tuple[int, int]]]:
    """The compiler's blob of every convention, on any host: what an artifact carries (S0014
    `.waboros.trampolines`, S0015): this compiler's bridge then tail_switch (abi.py), with the
    offsets of bridge, tail_switch and `restore` and the restore frame's two numbers."""
    from waboros.targets.jit.x86_64 import abi
    from waboros.targets.jit.x86_64.encoder import Asm

    out: dict[str, tuple[bytes, tuple[int, ...], tuple[int, int]]] = {}
    for name, win64 in (("sysv", False), ("win64", True)):
        a = Asm()
        abi.bridge(a, win64)
        tail = len(a.buf)
        restore = abi.tail_switch(a)
        out[name] = (a.finish(), (0, tail, restore), (abi.RESTORE_RET_AT, abi.RESTORE_UP))
    return out


@cache
def runtime(name: str = JitX86_64Target.convention) -> tuple[bytes, tuple[int, int], int]:
    """The runtime's entry then sp_stub for the convention `name` (`sysv` or `win64`; this
    process's by default), built on any host as the compilers' blobs are (D23: the Win64 entry is
    assembled and checked on Linux too); the offsets of the entry's return point and of sp_stub,
    and ENTRY_SAVED: not in any artifact (S0015)."""
    from waboros.targets.jit.x86_64 import entry
    from waboros.targets.jit.x86_64.encoder import Asm

    if name not in ("sysv", "win64"):
        raise ValueError(f"no x86-64 convention {name!r}")
    a = Asm()
    ret = entry.entry(a, name == "win64")
    stub = len(a.buf)
    entry.sp_stub(a)
    return a.finish(), (ret, stub), entry.ENTRY_SAVED


@cache
def trampolines() -> "Trampolines":
    """This process's trampolines, sealed once by the host side."""
    from waboros.targets.jit.common.instance import load_trampolines

    return load_trampolines(runtime(), conventions()[JitX86_64Target.convention])
