"""Guard pages (docs/specs/S0006): the bounds mode of JIT code, this copy's registry the fault
handler reads, the handler's installation and the check that the chain to it is not broken. Host
side only: the code is the same on every OS (D23); the handler is machine code from the
architecture's emitter (x86_64/abi.py, aarch64/abi.py), never Python (a Python handler would take
the GIL inside a signal).

Chain and claim (Q8, as Wasmtime): every copy of waboros in the process (a vendored copy, a
sub-interpreter) has its own registry and installs its own handler, which saves the action it
replaces. A handler claims a fault only when the PC is a fault point of its own code and the
address lies in one of its own reservations; everything else goes to the saved action. So copies
work in either install order and nested. Windows (step 4) has no signals: the handler is a
vectored exception handler, first in the list, and the OS walks the list, so nothing is chained.

Mode (D44): `WABOROS_JIT_BOUNDS=auto` (the default; empty counts as unset) | `explicit` | `guard`.
`explicit` installs nothing. `guard` compiles guard code wherever the backend has the mode and fails
at instantiation where the handler is not available, never crashes. `auto` gives `guard` only in a
process of the target's architecture on a host row that is verified (`JitTarget.verified`, S0006
"Selection") and once the handler is installed and ours; silently `explicit` otherwise (Q6).

Registry: per kind (code areas, reservations) one table `[capacity, entries...]` of 64-bit words
that never moves, published through one pointer word (`heads`); a full table is replaced by a
larger copy and the old one is kept forever. An entry `{lo, hi, ...}` is written with `hi` last
and removed (in every table) with `hi` zeroed first, before its area is unmapped, so a reused
address is never misclassified. Writers (add, grow, remove) hold one re-entrant lock: removal runs
from finalizers, which may fire on a thread already inside `add`; the handler's reads take none."""

import ctypes
import faulthandler
import functools
import os
import platform
import signal
import sys
import threading
from collections.abc import Sequence
from typing import Any

from waboros.targets.jit.common.execmem import ExecMem, Reservation, load

GUARD_LOG2 = 25
GUARD = 1 << GUARD_LOG2  # 32 MiB, Wasmtime's 64-bit `memory_guard_size` (Q7)
RESERVATION = (1 << 32) + GUARD  # every 32-bit memory, whatever the mode of its users
BOUNDS_GUARD = 1  # artifact header flag bit 0; GUARD_LOG2 in bits 8-15 (S0006 "Artifact")
FLAGS = BOUNDS_GUARD | GUARD_LOG2 << 8
MODES = ("explicit", "guard", "auto")
SIGSEGV = 11
DARWIN = sys.platform == "darwin"
# The signals the handler takes and the libc's sigaction flags. Linux: SIGSEGV; SA_SIGINFO 4,
# SA_ONSTACK 0x08000000. macOS (S0006 step 3; xnu bsd/sys/signal.h): SIGSEGV 11 and SIGBUS 10,
# since a PROT_NONE page (the guard) faults with KERN_PROTECTION_FAILURE, which xnu turns into
# SIGBUS, and an unmapped one with KERN_INVALID_ADDRESS, SIGSEGV (bsd/uxkern/ux_exception.c);
# SA_SIGINFO 0x40, SA_ONSTACK 0x1.
SIGNALS = (SIGSEGV, 10) if DARWIN else (SIGSEGV,)
SA_SIGINFO, SA_ONSTACK = (0x40, 0x1) if DARWIN else (4, 0x08000000)
LOST = (
    "the guard-page fault handler no longer receives "
    + "/".join(signal.Signals(s).name for s in SIGNALS)
    + ": guard-mode code cannot run (S0006 Q1); compile with WABOROS_JIT_BOUNDS=explicit"
)


def mode() -> str:
    m = os.environ.get("WABOROS_JIT_BOUNDS") or "auto"
    if m not in MODES:
        raise ValueError(f"WABOROS_JIT_BOUNDS={m!r}: expected one of {', '.join(MODES)}")
    return m


@functools.cache
def host_row() -> str:
    """This host's row of the S0006 table: `<os>-<libc>` (`linux-glibc`, `linux-musl`). glibc is
    what `platform.libc_ver()` names (glibc's `confstr(_CS_GNU_LIBC_VERSION)`); musl, which has
    no such call, is named by its mapped image (`_libc_of`); anything else is `linux-unknown`,
    so `auto` stays explicit there. Only `glibc` is taken from `libc_ver`: without the confstr it
    scans the executable's bytes and may answer `libc` for any libc."""
    if sys.platform.startswith("linux"):
        libc = platform.libc_ver()[0]
        if libc != "glibc":
            libc = ""
            try:
                with open("/proc/self/maps", errors="replace") as f:  # paths need not be UTF-8
                    libc = _libc_of(f.read())
            except OSError:
                pass
        return "linux-" + (libc or "unknown")
    return sys.platform


def _libc_of(maps: str) -> str:
    """`musl` when this process maps musl's libc, which is also its dynamic linker
    (`/lib/ld-musl-<arch>.so.1`; `libc.musl-<arch>.so.1` is a link to it); else empty. A
    statically linked musl interpreter maps none and stays unknown."""
    for line in maps.splitlines():
        name = line.rpartition("/")[2]
        if "/" in line and name.startswith(("ld-musl-", "libc.musl-")):
            return "musl"
    return ""


def wanted(target: Any) -> bool:
    """Compile guard code for `target` (a JitTarget)? Raises if `guard` is asked of a target that
    has no guard mode."""
    m = mode()
    if m == "explicit":
        return False
    if not target.guard_rows:
        if m == "guard":
            raise ValueError(f"WABOROS_JIT_BOUNDS=guard: {target.name} has no guard mode (S0006)")
        return False
    if m == "auto":
        if host_row() not in target.verified:
            return False
        try:
            install(target)
        except Exception:  # any failure to install: explicit, silently (S0006 "Selection")
            return False
        return broken() is None
    return True


class _Sigaction(ctypes.Structure):
    """The libc `struct sigaction` on x86-64 and aarch64: glibc's and musl's layout (sa_handler,
    a 128-byte mask, sa_flags, sa_restorer; 152 bytes; measured with musl 1.2.6's headers).
    macOS: {sa_handler, u32 sa_mask, int sa_flags}, 16 bytes (xnu bsd/sys/signal.h, the user
    `struct sigaction`; libplatform's `__platform_sigaction` adds the trampoline itself)."""

    _fields_ = (
        [("handler", ctypes.c_void_p), ("mask", ctypes.c_uint32), ("flags", ctypes.c_int)]
        if DARWIN
        else [
            ("handler", ctypes.c_void_p),
            ("mask", ctypes.c_uint64 * 16),
            ("flags", ctypes.c_int),
            ("restorer", ctypes.c_void_p),
        ]
    )


class _Registry:
    WIDTH = (4, 2)  # words per entry: code {lo, hi, map, count}, reservation {lo, hi}

    def __init__(self) -> None:
        self.lock = threading.RLock()  # re-entrant: a finalizer's `remove` inside `add`
        self.heads = (ctypes.c_uint64 * 2)()
        self.tables: tuple[list[Any], list[Any]] = ([], [])  # every table published, kept
        self.next = [0, 0]
        self.free: tuple[list[int], list[int]] = ([], [])  # slots removal gave back
        self.reservations: dict[int, int] = {}  # address -> slot
        for kind in (0, 1):
            self._publish(kind, 16)

    def _publish(self, kind: int, capacity: int) -> None:
        """A table of `capacity` entries with the live entries copied, then the pointer word."""
        w = self.WIDTH[kind]
        table = (ctypes.c_uint64 * (1 + w * capacity))()
        self.tables[kind].append(table)  # a removal from here on (a finalizer) zeroes it too
        if len(self.tables[kind]) > 1:  # under the lock: no other thread removes meanwhile
            old = self.tables[kind][-2]
            ctypes.memmove(table, old, ctypes.sizeof(old))
        table[0] = capacity
        self.heads[kind] = ctypes.addressof(table)

    def add(self, kind: int, words: Sequence[int]) -> int:
        with self.lock:
            slot = self.free[kind].pop() if self.free[kind] else self.next[kind]
            self.next[kind] = max(self.next[kind], slot + 1)
            if slot >= self.tables[kind][-1][0]:
                self._publish(kind, 2 * self.tables[kind][-1][0])
            table, at = self.tables[kind][-1], 1 + self.WIDTH[kind] * slot
            table[at] = words[0]
            for i in range(2, len(words)):
                table[at + i] = words[i]
            table[at + 1] = words[1]  # hi last: the entry is live from here
            return slot

    def remove(self, kind: int, slot: int, *keep: object) -> None:
        """Zero the entry in every table (hi first), then give the slot back. `keep`: objects
        the entry points to, alive until now."""
        w = self.WIDTH[kind]
        with self.lock:
            for table in self.tables[kind]:
                at = 1 + w * slot
                if at + w <= len(table):
                    table[at + 1] = 0
                    for i in (0, *range(2, w)):
                        table[at + i] = 0
            self.free[kind].append(slot)


_registry = _Registry()
_install_lock = threading.Lock()
_handler: dict[str, Any] = {}  # the handler's code area, its address, the saved action, libc


def install(target: Any) -> None:
    """Install the process's fault handler once (S0006 "Installation"), or raise RuntimeError
    where this host has none for `target`'s architecture."""
    # the handler is the target's machine code: never installed in another architecture's
    # process (jit_dump cross-compiles, D35); checked before "already installed", which is the
    # native target's handler (LESSONS 48)
    machine = platform.machine().lower()
    native = machine in target.machines and sys.maxsize > 1 << 32
    if host_row() not in target.guard_rows or not native:
        raise RuntimeError(
            f"guard-mode code (WABOROS_JIT_BOUNDS=guard) cannot run here: {target.name} has "
            f"no fault handler for {host_row()} on {machine} (S0006)"
        )
    if _handler:
        return
    with _install_lock:
        if _handler:
            return
        if sys.platform == "win32":
            _install_vectored(target)
            return
        libc = ctypes.CDLL(None, use_errno=True)
        sigaction = libc.sigaction
        sigaction.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p]
        # one saved action per signal, in SIGNALS order: read by the handler, never freed
        prev = (_Sigaction * len(SIGNALS))()
        for i, sig in enumerate(SIGNALS):
            if sigaction(sig, None, ctypes.byref(prev[i])) != 0:
                raise OSError(ctypes.get_errno(), "sigaction")
        entry, reraise = (
            ctypes.cast(f, ctypes.c_void_p).value or 0 for f in (sigaction, getattr(libc, "raise"))
        )
        code = target.fault_handler(
            ctypes.addressof(_registry.heads), ctypes.addressof(prev), entry, reraise, darwin=DARWIN
        )
        mem: ExecMem = load(code)
        act = _Sigaction()
        act.handler, act.flags = mem.addr, SA_SIGINFO | SA_ONSTACK  # no SA_NODEFER
        for i, sig in enumerate(SIGNALS):
            if sigaction(sig, ctypes.byref(act), None) != 0:
                err = ctypes.get_errno()
                for k in range(i):  # nothing half-installed: a retry would chain to itself
                    sigaction(SIGNALS[k], ctypes.byref(prev[k]), None)
                raise OSError(err, "sigaction")
        # a Python-level handler set before ours is only chained to: not a break later
        py = {sig: signal.getsignal(sig) for sig in SIGNALS}
        _handler.update(code=mem, addr=mem.addr, prev=prev, sigaction=sigaction, py=py)
        _handler["fh"] = faulthandler.is_enabled()  # enabled before ours: it sits below us
        _handler.update(_image_finder(libc))


def _install_vectored(target: Any) -> None:
    """Windows (S0006 step 4): the handler goes first in the process's vectored exception list
    (`AddVectoredExceptionHandler(1, h)`, as Wasmtime), so it sees an access violation before any
    frame-based handler (ctypes' own `__except` included). The OS walks the list, so there is no
    saved action and nothing to chain; only the holder of the returned handle can remove an
    entry, so it stays ours for the process's lifetime (`broken()`)."""
    code = target.fault_handler(ctypes.addressof(_registry.heads), 0, 0, 0, windows=True)
    mem: ExecMem = load(code)
    if sys.platform == "win32":  # for the type checker: WinDLL exists on Windows only
        add = ctypes.WinDLL("kernel32", use_last_error=True).AddVectoredExceptionHandler
        add.restype, add.argtypes = ctypes.c_void_p, [ctypes.c_uint32, ctypes.c_void_p]
        veh = add(1, mem.addr)
        if not veh:
            mem.free()
            raise ctypes.WinError(ctypes.get_last_error())
        _handler.update(code=mem, addr=mem.addr, veh=veh)


class _DlInfo(ctypes.Structure):
    _fields_ = [
        ("fname", ctypes.c_char_p),
        ("fbase", ctypes.c_void_p),
        ("sname", ctypes.c_char_p),
        ("saddr", ctypes.c_void_p),
    ]


def _image_finder(libc: Any) -> dict[str, Any]:
    """`dladdr` and the load address of the interpreter's own image (the executable or
    libpython/libpypy: where faulthandler and the `signal` module's handler live), found through
    `Py_IncRef`; empty where either is missing (then broken() keeps the conservative rule)."""
    try:
        dladdr = libc.dladdr
        dladdr.argtypes = [ctypes.c_void_p, ctypes.POINTER(_DlInfo)]
        for name in ("Py_IncRef", "PyPy_IncRef"):
            if hasattr(libc, name):
                at = ctypes.cast(getattr(libc, name), ctypes.c_void_p).value
                base = _image(dladdr, at or 0)
                return {"dladdr": dladdr, "python": base} if base else {}
    except (AttributeError, OSError):
        pass
    return {}


def _image(dladdr: Any, addr: int) -> int:
    """The load address of the shared object that holds `addr`; 0 for anonymous memory."""
    info = _DlInfo()
    return (info.fbase or 0) if dladdr(addr, ctypes.byref(info)) else 0


def broken() -> str | None:
    """Is the chain to our handler recognisably broken (S0006 Q1, rule A), for any signal it
    takes (SIGSEGV; and SIGBUS on macOS)? Our handler still the action: no. Otherwise these
    break it: the action reset to SIG_DFL/SIG_IGN; a Python-level handler set after ours (it
    only sets a flag, the access faults forever); a handler inside the interpreter's own image
    (`dladdr`): faulthandler, which re-raises instead of chaining, or the `signal` module's;
    faulthandler enabled now but not at our install, with any foreign handler on top
    (faulthandler then sits between it and us). A handler in any other
    shared object (wasmtime, another library) or in anonymous memory (another waboros copy's
    emitted handler, another JIT's) is otherwise assumed to chain, as Wasmtime assumes. Not
    detected: faulthandler already enabled at our install, then disabled and re-enabled while a
    library's handler is above ours (S0006 Changes, RISKS R6). Without `dladdr`: any foreign
    handler with faulthandler enabled counts as a break (a false error rather than a crash)."""
    if not _handler:
        return "not installed"
    if sys.platform == "win32":  # a vectored handler cannot be replaced (RISKS R6)
        return None
    for sig in SIGNALS:
        why = _broken(sig)
        if why:
            return why
    return None


def _broken(sig: int) -> str | None:
    name = signal.Signals(sig).name
    cur = _Sigaction()
    _handler["sigaction"](sig, None, ctypes.byref(cur))
    h = cur.handler or 0
    if h == _handler["addr"]:
        return None
    if h <= 1:
        return f"{name} reset to SIG_DFL/SIG_IGN"
    py = signal.getsignal(sig)
    if callable(py) and py is not _handler["py"][sig]:
        return f"a Python-level {name} handler set after ours"
    if faulthandler.is_enabled() and ("python" not in _handler or not _handler["fh"]):
        return "faulthandler enabled after ours and another handler above ours"
    if "python" not in _handler:
        return None
    if _image(_handler["dladdr"], h) == _handler["python"]:
        return "an interpreter's handler (faulthandler or signal) above ours"
    return None


def check() -> None:
    """Before guard code runs from the host: raise the Q1 error if the chain is broken."""
    why = broken()
    if why:
        raise RuntimeError(f"{LOST} ({why})")


def add_code(code: ExecMem, faults: Sequence[tuple[int, int]]) -> None:
    """Register a code area and its fault map until the area is unmapped."""
    flat = (ctypes.c_uint32 * max(1, 2 * len(faults)))(*(x for p in faults for x in p))
    lo = code.addr
    slot = _registry.add(0, [lo, lo + code.size, ctypes.addressof(flat), len(faults)])
    code.on_free(functools.partial(_registry.remove, 0, slot, flat))


def add_reservation(res: Reservation) -> None:
    """Register a memory's reservation (once) until it is unmapped."""
    with _registry.lock:
        if res.addr in _registry.reservations:
            return
        _registry.reservations[res.addr] = -1
    slot = _registry.add(1, [res.addr, res.addr + res.size])
    _registry.reservations[res.addr] = slot
    res.on_free(functools.partial(_forget, res.addr, slot))


def _forget(addr: int, slot: int) -> None:
    with _registry.lock:
        _registry.remove(1, slot)
        _registry.reservations.pop(addr, None)
