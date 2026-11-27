"""Measure the native stack of the main thread and of a default thread (D10, S0003), and what the
JIT computes from it. Stdlib only:  PYTHONPATH=src python tools/stack_info.py

The bounds are read here independently of the JIT (pthread_getattr_np / pthread_get_stackaddr_np /
GetCurrentThreadStackLimits); the JIT's `low` and native budget come from its own functions.
`sp` is read by the JIT's stack-pointer stub through its entry when the probe of this machine's own
JIT target (jit-x86_64 or jit-aarch64) succeeds; elsewhere the JIT part is not reported.
`libc` is empty on musl (platform.libc_ver knows glibc only)."""

import ctypes
import platform
import sys
import sysconfig
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

VP, SIZE_T = ctypes.c_void_p, ctypes.c_size_t
KIB = 1024


def bounds() -> tuple[int, int] | str:
    """(low, high) of the current thread's stack, or why not."""
    try:
        if sys.platform == "win32":
            lo, hi = VP(), VP()
            ctypes.WinDLL("kernel32").GetCurrentThreadStackLimits(
                ctypes.byref(lo), ctypes.byref(hi)
            )
            return lo.value or 0, hi.value or 0
        libc = ctypes.CDLL(None)
        libc.pthread_self.restype = VP
        me = libc.pthread_self()
        if sys.platform == "darwin":
            for fn in (libc.pthread_get_stackaddr_np, libc.pthread_get_stacksize_np):
                fn.argtypes, fn.restype = (
                    [VP],
                    (VP if fn is libc.pthread_get_stackaddr_np else SIZE_T),
                )
            top = libc.pthread_get_stackaddr_np(me)
            return top - libc.pthread_get_stacksize_np(me), top
        attr = ctypes.create_string_buffer(256)
        libc.pthread_getattr_np.argtypes = [VP, VP]
        libc.pthread_attr_getstack.argtypes = [VP, ctypes.POINTER(VP), ctypes.POINTER(SIZE_T)]
        libc.pthread_attr_destroy.argtypes = [VP]
        if libc.pthread_getattr_np(me, attr) != 0:
            return "pthread_getattr_np failed"
        addr, size = VP(), SIZE_T()
        libc.pthread_attr_getstack(attr, ctypes.byref(addr), ctypes.byref(size))
        libc.pthread_attr_destroy(attr)
        return addr.value or 0, (addr.value or 0) + size.value
    except (OSError, AttributeError) as e:
        return f"unavailable: {e!r}"


def jit_view() -> str:
    try:
        from waboros.targets.base import TARGETS
        from waboros.targets.jit.common import instance
        from waboros.targets.jit.common.context import EXIT_RECORD
        from waboros.targets.jit.common.target import JitTarget

        mine = [
            t
            for t in (make() for make in TARGETS.values())
            if isinstance(t, JitTarget) and platform.machine().lower() in t.machines
        ]
        if not mine:
            return f"jit: no JIT target for {platform.machine()}"
        if mine[0].probe() is not None:
            return f"jit: {mine[0].name} probe failed, no sp"
        t = mine[0].trampolines()
        ctx, frame = (ctypes.c_uint64 * 18)(), (ctypes.c_uint64 * 1)()
        exit_record = (ctypes.c_uint64 * 3)()  # every entry saves and restores it (S0013 3a)
        ctx[EXIT_RECORD] = ctypes.addressof(exit_record)
        instance.ENTRY(t.entry)(ctypes.addressof(ctx), ctypes.addressof(frame), t.sp_stub)
        sp, (low, rule) = frame[0], instance._read_stack()  # pyright: ignore[reportPrivateUsage]
        budget = instance.native_budget(sp, low, instance.DEFAULT_BUDGET)
        below = "?" if low is None else f"{(sp - low) // KIB} KiB"
        low_s = "None" if low is None else f"{low:#x}"
        return (
            f"jit ({mine[0].name}): sp {sp:#x} low {low_s} (below sp {below})"
            f" native budget {budget // KIB} KiB rule: {rule}"
        )
    except Exception as e:  # report, never fail: this is a measurement tool
        return f"jit: {e!r}"


def report(name: str) -> None:
    b = bounds()
    if isinstance(b, str):
        print(f"{name}: {b}")
    else:
        print(f"{name}: low {b[0]:#x} high {b[1]:#x} size {(b[1] - b[0]) // KIB} KiB")
    print(f"{name}: {jit_view()}")


def main() -> None:
    ft = bool(sysconfig.get_config_var("Py_GIL_DISABLED"))
    print(f"platform {platform.platform()} machine {platform.machine()}")
    print(f"python {sys.version.split()[0]} {sys.implementation.name} free-threaded {ft}")
    print(f"libc {' '.join(platform.libc_ver()) or 'unknown'}")
    try:
        import resource

        soft, hard = resource.getrlimit(resource.RLIMIT_STACK)
        show = lambda v: "unlimited" if v == resource.RLIM_INFINITY else f"{v // KIB} KiB"  # noqa: E731
        print(f"RLIMIT_STACK soft {show(soft)} hard {show(hard)}")
    except ImportError:
        print("RLIMIT_STACK n/a")
    print(f"threading.stack_size() {threading.stack_size()}")
    report("main thread")
    t = threading.Thread(target=report, args=("default thread",))
    t.start()
    t.join()


if __name__ == "__main__":
    main()
