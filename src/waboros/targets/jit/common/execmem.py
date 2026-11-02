"""Executable memory, W^X (R2, S0003): map read-write, write the code, flip to read-execute; a page
is never writable and executable at once. Also address space reserved up front and committed on
demand. POSIX: mmap/mprotect/munmap through ctypes. Windows: VirtualAlloc/VirtualProtect/VirtualFree
(the D23 shape; run by the Windows CI job).

AArch64 (S0004) does not keep the instruction cache coherent with data writes: a sealed area is
invalidated before its first call (Linux `__clear_cache`, macOS `sys_icache_invalidate`, Windows
`FlushInstructionCache`); no flush function means no executable memory (the probe fails, D31).
Linux maps it `PROT_BTI` where the CPU reports BTI (landing pads enforced). macOS arm64 maps
`MAP_JIT` and switches the calling thread between writing and executing
(`pthread_jit_write_protect_np`), as Apple requires; a hardened runtime without the JIT
entitlement refuses the mapping. x86-64 needs none of this."""

import ctypes
import mmap
import os
import platform
import sys
import weakref
from collections.abc import Callable
from typing import Any

ARM64 = platform.machine().lower() in ("aarch64", "arm64")


class ExecMem:
    """A code area of `size` bytes (rounded up to pages): `write` while read-write, then `seal`."""

    def __init__(self, size: int) -> None:
        page = mmap.PAGESIZE
        self.size = max(page, (size + page - 1) // page * page)
        self.addr = _alloc(self.size)
        self.sealed = False
        # unmapped when unreachable: records cross only within a store, which keeps its
        # instances (and so their code areas) alive while any of its code can run
        self._release: weakref.finalize[..., Any] = weakref.finalize(
            self, _free, self.addr, self.size
        )
        self._release.atexit = False  # a daemon thread may still run this code at exit

    def write(self, code: bytes) -> None:
        if self.sealed or not self.addr:
            raise ValueError("code area is sealed or freed")
        if len(code) > self.size:
            raise ValueError("code does not fit")
        _write(self.addr, code)

    def seal(self) -> None:
        """Read-write -> read-execute, then the instruction cache is made coherent."""
        _protect(self.addr, self.size)
        _flush(self.addr, self.size)
        self.sealed = True

    def free(self) -> None:
        self.addr = 0
        self._release()

    def on_free(self, first: Callable[[], None]) -> None:
        _on_free(self, first)


class Reservation:
    """Address space reserved up front (no access), committed read-write from its start on demand;
    it never moves (LESSONS 7). Linear memories (4 GiB + 32 MiB each, Wasmtime's 64-bit defaults)
    and value stacks."""

    def __init__(self, size: int, commit: int = 0) -> None:
        self.size = size
        self.committed = 0
        self.addr = _reserve(size)
        self._release: weakref.finalize[..., Any] = weakref.finalize(self, _free, self.addr, size)
        self._release.atexit = False  # a daemon thread may still use this memory at exit
        self.commit(commit)

    def commit(self, nbytes: int) -> None:
        """Make [addr, addr + nbytes) read-write (rounded up to pages); new pages read as zero."""
        page = mmap.PAGESIZE
        if not 0 <= nbytes <= self.size:
            raise ValueError(f"commit of {nbytes} bytes outside a {self.size}-byte reservation")
        nbytes = (nbytes + page - 1) // page * page
        if nbytes > self.committed:
            _commit(self.addr + self.committed, nbytes - self.committed)
            self.committed = nbytes

    def free(self) -> None:
        self.addr = 0
        self._release()

    def on_free(self, first: Callable[[], None]) -> None:
        _on_free(self, first)


def _on_free(area: "ExecMem | Reservation", first: Callable[[], None]) -> None:
    """`first()` runs right before the area is unmapped (S0006: its fault-registry entry goes
    before the address can be reused)."""
    area._release.detach()  # pyright: ignore[reportPrivateUsage]
    area._release = weakref.finalize(area, _free_after, first, area.addr, area.size)  # pyright: ignore[reportPrivateUsage]
    area._release.atexit = False  # pyright: ignore[reportPrivateUsage]


def _free_after(first: Callable[[], None], addr: int, size: int) -> None:
    try:
        first()
    finally:
        _free(addr, size)


def load(code: bytes) -> ExecMem:
    """Map, write and seal `code`."""
    mem = ExecMem(len(code))
    try:
        mem.write(code)
        mem.seal()
    except BaseException:
        mem.free()
        raise
    return mem


def _flush(addr: int, size: int) -> None:  # x86-64: coherent instruction cache
    pass


def _write(addr: int, code: bytes) -> None:
    ctypes.memmove(addr, code, len(code))


def write_toggled(protect: Any, addr: int, code: bytes) -> None:
    """macOS arm64 MAP_JIT: `protect(0)` makes the pages writable (not executable) for this thread
    only, `protect(1)` switches back, also when the copy raises."""
    protect(0)
    try:
        ctypes.memmove(addr, code, len(code))
    finally:
        protect(1)


if sys.platform == "win32":
    # The D23 shape for Windows; the CI `os` job runs tests/jit on windows-latest.
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.VirtualAlloc.restype = ctypes.c_void_p
    _k32.VirtualAlloc.argtypes = [
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_uint32,
        ctypes.c_uint32,
    ]
    _k32.VirtualProtect.argtypes = [
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    _k32.VirtualFree.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32]

    def _alloc(size: int) -> int:
        addr = _k32.VirtualAlloc(None, size, 0x3000, 0x04)  # MEM_COMMIT|MEM_RESERVE, PAGE_READWRITE
        if not addr:
            raise ctypes.WinError(ctypes.get_last_error())
        return addr

    def _protect(addr: int, size: int) -> None:
        old = ctypes.c_uint32()
        if not _k32.VirtualProtect(addr, size, 0x20, ctypes.byref(old)):  # PAGE_EXECUTE_READ
            raise ctypes.WinError(ctypes.get_last_error())

    def _reserve(size: int) -> int:
        addr = _k32.VirtualAlloc(None, size, 0x2000, 0x01)  # MEM_RESERVE, PAGE_NOACCESS
        if not addr:
            raise ctypes.WinError(ctypes.get_last_error())
        return addr

    def _commit(addr: int, size: int) -> None:
        if not _k32.VirtualAlloc(addr, size, 0x1000, 0x04):  # MEM_COMMIT, PAGE_READWRITE
            raise ctypes.WinError(ctypes.get_last_error())

    def _free(addr: int, size: int) -> None:
        if not _k32.VirtualFree(addr, 0, 0x8000):  # MEM_RELEASE
            raise ctypes.WinError(ctypes.get_last_error())

    if ARM64:
        _k32.GetCurrentProcess.restype = ctypes.c_void_p
        _k32.FlushInstructionCache.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]

        def _flush(addr: int, size: int) -> None:
            if not _k32.FlushInstructionCache(_k32.GetCurrentProcess(), addr, size):
                raise ctypes.WinError(ctypes.get_last_error())

else:
    _libc = ctypes.CDLL(None, use_errno=True)
    _libc.mmap.restype = ctypes.c_void_p
    _libc.mmap.argtypes = [
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_long,  # off_t on LP64 Linux/macOS (the probe refuses 32-bit processes)
    ]
    _libc.mprotect.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
    _libc.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]

    def _oserror() -> OSError:
        err = ctypes.get_errno()
        return OSError(err, f"executable memory: {os.strerror(err)}")

    def _map(size: int, prot: int, flags: int = 0) -> int:
        flags |= mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS
        addr = _libc.mmap(None, size, prot, flags, -1, 0)
        if addr in (None, ctypes.c_void_p(-1).value):
            raise _oserror()
        return addr

    def _alloc(size: int) -> int:
        return _map(size, mmap.PROT_READ | mmap.PROT_WRITE)

    def _reserve(size: int) -> int:
        return _map(size, 0, getattr(mmap, "MAP_NORESERVE", 0))  # PROT_NONE

    def _commit(addr: int, size: int) -> None:
        if _libc.mprotect(addr, size, mmap.PROT_READ | mmap.PROT_WRITE) != 0:
            raise _oserror()

    def _protect(addr: int, size: int) -> None:
        if _libc.mprotect(addr, size, mmap.PROT_READ | mmap.PROT_EXEC) != 0:
            raise _oserror()

    def _free(addr: int, size: int) -> None:
        if _libc.munmap(addr, size) != 0:
            raise _oserror()

    def _symbol(name: str, argtypes: list[Any], *libs: str) -> Any:
        """A C function of the process (or of the first of `libs` that has it), or None."""
        for lib in (None, *libs):
            try:
                f = getattr(ctypes.CDLL(lib), name)
            except (OSError, AttributeError):
                continue
            f.argtypes, f.restype = argtypes, None
            return f
        return None

    if ARM64 and sys.platform == "darwin":
        _MAP_JIT = 0x800
        _jit_wp = _symbol("pthread_jit_write_protect_np", [ctypes.c_int])
        _icache = _symbol(
            "sys_icache_invalidate", [ctypes.c_void_p, ctypes.c_size_t], "libSystem.B.dylib"
        )

        def _alloc(size: int) -> int:
            if _jit_wp is None or _icache is None:
                raise OSError("no pthread_jit_write_protect_np/sys_icache_invalidate")
            return _map(size, mmap.PROT_READ | mmap.PROT_WRITE | mmap.PROT_EXEC, _MAP_JIT)

        def _write(addr: int, code: bytes) -> None:
            write_toggled(_jit_wp, addr, code)

        def _protect(addr: int, size: int) -> None:
            pass  # MAP_JIT pages are switched per thread by _write

        def _flush(addr: int, size: int) -> None:
            _icache(addr, size)

    elif ARM64:
        _clear_cache = _symbol("__clear_cache", [ctypes.c_void_p] * 2, "libgcc_s.so.1")

        def _prot_bti() -> int:
            """PROT_BTI if the CPU has BTI (AT_HWCAP2 = 26, HWCAP2_BTI = 1 << 17), else 0."""
            try:
                _libc.getauxval.restype, _libc.getauxval.argtypes = ctypes.c_ulong, [ctypes.c_ulong]
            except AttributeError:  # not Linux
                return 0
            return 0x10 if _libc.getauxval(26) & 1 << 17 else 0

        _PROT_BTI = _prot_bti()

        def _protect(addr: int, size: int) -> None:
            if _libc.mprotect(addr, size, mmap.PROT_READ | mmap.PROT_EXEC | _PROT_BTI) != 0:
                raise _oserror()

        def _flush(addr: int, size: int) -> None:
            if _clear_cache is None:
                raise OSError("no instruction-cache flush function (__clear_cache)")
            _clear_cache(addr, addr + size)
