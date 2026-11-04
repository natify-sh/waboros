"""The runtime side of the JIT ABI (docs/specs/S0003), shared by every architecture: the context,
the value stack, both stack limits, the entry call with status -> `Trap`, host callbacks that never
raise, nested entries, interrupt, linear memories (reserved, committed on grow, never moved).

An `Instance` runs code assembled elsewhere (hand-built in the ABI tests); a `JitInstance` runs a
compiled module (common/compiler.py)."""

import ctypes
import mmap
import os
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, cast

from waboros.frontend import validate
from waboros.runtime.link import LinkError
from waboros.runtime.store import StoreState
from waboros.targets.base import Extern, HostError, HostFunc, Raw, Trap, TrapCode
from waboros.targets.jit.common import guard, simd
from waboros.targets.jit.common.compiler import I64, M32, M64, NARROW, Compiled
from waboros.targets.jit.common.context import (
    DATA_DROP,
    ELEM_DROP,
    HOST_BRIDGE,
    HOST_ERROR,
    INTERRUPT,
    LIBCALLS,
    MEMORY_GROW,
    MEMORY_INIT,
    NSTACK_LIMIT,
    RECORD,
    STATUS_HOST_ERROR,
    STATUS_INTERRUPT,
    TABLE_GROW,
    TABLE_INIT,
    TAIL_SWITCH,
    VSTACK_LIMIT,
    Layout,
)
from waboros.targets.jit.common.execmem import ExecMem, Reservation, load

ENTRY = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
HOST = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p)
VSTACK_BYTES = 1 << 20  # per store
# Wasmtime's default `memory_reservation` (crates/environ/src/tunables.rs) and its 64-bit
# `memory_guard_size`, for every memory in either mode, so any memory serves guard code (S0006)
MEMORY_RESERVATION = guard.RESERVATION
MAX_PAGES = 1 << 16
MAX_PAGES64 = (1 << 48) - 1  # an i64 memory's byte size fits the 64-bit size word (S0009)
DEFAULT_BUDGET = 512 << 10  # Wasmtime's default `max_wasm_stack` (D6)
UNKNOWN_STACK_BUDGET = 128 << 10  # when the thread's stack bounds cannot be read or are implausible
PLAUSIBLE_STACK = 1 << 30  # more stack than this below the entry: the bounds are not trusted
TABLE_LIMIT = 10_000_000  # elements: pyc's cap (D27), so both targets fail the same table.grow
_TRAP_STATUSES = frozenset(TrapCode)  # the statuses a host `Trap` may return as itself
_TYPE_IDS: dict[Any, int] = {}  # canonical function type ids of the process (structural keys)
_TYPE_IDS_LOCK = threading.Lock()  # stores on different threads register types concurrently


def type_id(types: Any, ti: int) -> int:
    """The canonical id of function type `ti` of a module: equal for equivalent types of any
    module (never 0)."""
    key: Any = validate.functype_key(types, ti)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    with _TYPE_IDS_LOCK:  # the size read and the insert must be one step: ids stay distinct
        return _TYPE_IDS.setdefault(key, len(_TYPE_IDS) + 1)


def mask(t: Any) -> int:
    return M32 if t in NARROW else M64


def isref(t: Any) -> bool:
    return t.__class__ is tuple or t in (0x70, 0x6F)


def isextern(t: Any) -> bool:
    return t == 0x6F or t.__class__ is tuple and t[1] in (-17, -14)


@dataclass(frozen=True)
class Trampolines:
    """Addresses of an architecture's trampolines for the host's convention."""

    entry: int
    bridge: int
    sp_stub: int
    tail_switch: int
    keep: object = None  # the code area


@dataclass(frozen=True)
class FuncRef:
    """A non-null funcref outside the code: the address of its function record, valid only in
    the store (`_StoreStacks`) that made it."""

    addr: int
    store: object


class _StoreStacks:
    """Per store: the value stack, the JIT entries active on it, and the native limit set by the
    first of them."""

    def __init__(self) -> None:
        self.vstack = Reservation(VSTACK_BYTES, VSTACK_BYTES)
        self.vtop = self.vstack.addr + VSTACK_BYTES
        self.vsp = self.vstack.addr  # next free slot: above the innermost host call's slots
        self.nlimit = 0
        self.active = 0  # JIT entries in progress (wasm -> host -> wasm nests them)
        self.guard = False  # guard-mode code in the store: check the handler at every entry
        # reference values at the boundary (S0002: opaque objects, None = null): an externref is
        # a handle (index + 1) of the object kept here for the store's lifetime; a funcref is a
        # record address, seen by the host as a `FuncRef` (one object per address)
        self.externs: list[object] = []
        self.extern_ids: dict[int, int] = {}
        self.funcrefs: dict[int, FuncRef] = {}

    def to_raw(self, t: Any, v: object) -> int:
        if v is None:
            return 0
        if not isextern(t):
            # only a reference this store handed out: a record of another store would run on that
            # store's context and value stack, a forged address anywhere (LESSONS 35)
            if not isinstance(v, FuncRef) or self.funcrefs.get(v.addr) is not v:  # one object each
                raise TypeError(f"not a function reference of this store: {v!r}")
            return v.addr
        h = self.extern_ids.get(id(v))
        if h is None:
            self.externs.append(v)
            h = self.extern_ids[id(v)] = len(self.externs)
        return h

    def from_raw(self, t: Any, r: int) -> object:
        if not r:
            return None
        if isextern(t):
            return self.externs[r - 1]
        ref = self.funcrefs.get(r)
        if ref is None:
            ref = self.funcrefs[r] = FuncRef(r, self)
        return ref


def load_trampolines(code: bytes, offsets: Sequence[int]) -> Trampolines:
    """Seal an architecture's trampoline blob (its abi.assemble) into one code area, kept for the
    process's lifetime by the cache of the target that calls this."""
    mem = load(code)
    return Trampolines(*(mem.addr + o for o in offsets), keep=mem)


def _stacks(store: StoreState) -> _StoreStacks:
    if not isinstance(store.target_state, _StoreStacks):
        store.target_state = _StoreStacks()
    return store.target_state


_thread = threading.local()


def _stack_low() -> int | None:
    """Lowest address of the current thread's native stack (cached per thread), None if unknown."""
    if not hasattr(_thread, "low"):
        _thread.low = _read_stack()[0]
    return _thread.low


def linux_main_low(high: int, soft: int | None) -> int | None:
    """Lowest usable address of the Linux main thread's stack, which the kernel grows on demand up
    to RLIMIT_STACK below its top `high` (glibc's rule for pthread_getattr_np; musl reports only
    the part mapped so far). `soft` None = unlimited: unknown. One page above the guard is kept
    clear, as on macOS."""
    if soft is None or soft <= 0:
        return None
    return high - min(soft, PLAUSIBLE_STACK) + mmap.PAGESIZE


def _linux_stack_vma_top() -> int | None:
    """End of the `[stack]` mapping: RLIMIT_STACK counts from there, above the args and
    environment, so it is higher than the top pthread_getattr_np reports."""
    try:
        with open("/proc/self/maps") as f:
            for line in f:
                if line.rstrip().endswith("[stack]"):
                    return int(line.split("-", 1)[1].split()[0], 16)
    except (OSError, ValueError, IndexError):
        pass
    return None


def _read_stack() -> tuple[int | None, str]:
    """(low, the rule that gave it)."""
    vp, size_t = ctypes.c_void_p, ctypes.c_size_t
    try:
        if sys.platform == "win32":
            lo, hi = vp(), vp()
            k32 = ctypes.WinDLL("kernel32")
            k32.GetCurrentThreadStackLimits(ctypes.byref(lo), ctypes.byref(hi))
            return lo.value, "GetCurrentThreadStackLimits"
        libc = ctypes.CDLL(None)
        libc.pthread_self.restype = vp
        me = libc.pthread_self()
        if sys.platform == "darwin":
            # stackaddr is the top; the main thread's reported size may exceed its rlimit, so
            # take the smaller; leave one guard's worth (a page) above the guard page as unusable
            libc.pthread_get_stackaddr_np.restype = vp
            libc.pthread_get_stacksize_np.restype = size_t
            libc.pthread_get_stackaddr_np.argtypes = libc.pthread_get_stacksize_np.argtypes = [vp]
            top, size = libc.pthread_get_stackaddr_np(me), libc.pthread_get_stacksize_np(me)
            if libc.pthread_main_np():
                import resource

                soft = resource.getrlimit(resource.RLIMIT_STACK)[0]
                size = min(size, soft) if soft > 0 else size
            return (top - size + mmap.PAGESIZE if top else None), "pthread_get_stack*_np"
        attr = ctypes.create_string_buffer(256)  # >= sizeof(pthread_attr_t) on glibc and musl
        libc.pthread_getattr_np.argtypes = [vp, ctypes.c_char_p]
        if libc.pthread_getattr_np(me, attr) != 0:
            return None, "pthread_getattr_np failed"
        addr, size = vp(), size_t()
        libc.pthread_attr_getstack(attr, ctypes.byref(addr), ctypes.byref(size))
        libc.pthread_attr_destroy(attr)
        if sys.platform.startswith("linux") and threading.get_native_id() == os.getpid():
            import resource

            soft = resource.getrlimit(resource.RLIMIT_STACK)[0]
            finite = None if soft == resource.RLIM_INFINITY else soft
            high = _linux_stack_vma_top() or (addr.value or 0) + size.value
            return linux_main_low(high, finite), "linux main thread: top - RLIMIT_STACK"
        return addr.value, "pthread_getattr_np"
    except (OSError, AttributeError):
        return None, "unavailable"


def native_budget(sp: int, low: int | None, budget: int) -> int:
    """Native stack wasm code may use below `sp` (S0003, D10): never more than half of the
    thread's stack left below `sp`, so a host function called from the deepest wasm frame still
    has at least as much; if the bounds are unknown or implausible (`sp` not within 1 GiB above
    `low`: e.g. an unlimited RLIMIT_STACK on the main thread), at most UNKNOWN_STACK_BUDGET."""
    if low is None or not 0 < sp - low <= PLAUSIBLE_STACK:
        return min(budget, UNKNOWN_STACK_BUDGET)
    return min(budget, (sp - low) // 2)


class Memory:
    """A linear memory: 4 GiB + GUARD of address space reserved (S0006), pages committed on
    grow; the base never changes. `grow` rewrites the size word of every context bound to it.
    An i64 memory (`a64`, S0009) has the same reservation for now, so it is capped at MAX_PAGES
    too (step 2a; step 2b moves it past its reservation instead)."""

    def __init__(self, pages: int, maximum: int | None, *, a64: bool = False) -> None:
        if a64 and pages > MAX_PAGES64:
            raise MemoryError(f"memory minimum size of {pages} pages exceeds memory limits")
        if pages > MAX_PAGES:  # only an i64 memory gets here (validation): step 2a's cap
            raise MemoryError(f"failed to reserve {pages << 16} bytes")
        self.res = Reservation(MEMORY_RESERVATION, pages << 16)
        self.pages = pages
        self.max = MAX_PAGES if maximum is None else min(maximum, MAX_PAGES)
        self.mask = M64 if a64 else M32  # of memory.grow's result (-1 at the address width)
        self.declared = maximum  # the type's maximum, for import checks
        self.store: object = None  # the owning StoreState (set by JitInstance)
        self.users: list[tuple[ctypes.Array[ctypes.c_uint64], int]] = []

    def bind(self, ctx: "ctypes.Array[ctypes.c_uint64]", word: int) -> None:
        ctx[word], ctx[word + 1] = self.res.addr, self.pages << 16
        self.users.append((ctx, word))

    def grow(self, delta: int) -> int:
        """memory.grow: the old size in pages, or -1."""
        old = self.pages
        if delta > self.max - old:
            return -1
        try:
            self.res.commit((old + delta) << 16)
        except OSError:
            return -1
        self.pages = old + delta
        for ctx, word in self.users:
            ctx[word + 1] = self.pages << 16
        return old


class Table:
    """A table: `size` element words (a record address or an extern handle, 0 = null) in a
    ctypes array that grows by reallocation; `grow` rewrites the base and size words of every
    context bound to it (code reads them at each use and never across a host call)."""

    def __init__(self, size: int, maximum: int | None, init: int = 0, store: object = None) -> None:
        self.size, self.declared, self.store = size, maximum, store
        self.max = min(TABLE_LIMIT, M32 if maximum is None else maximum)
        self.buf = (ctypes.c_uint64 * max(size, 1))()
        if init:
            self.buf[:size] = [init] * size
        self.users: list[tuple[ctypes.Array[ctypes.c_uint64], int]] = []

    def bind(self, ctx: "ctypes.Array[ctypes.c_uint64]", word: int) -> None:
        ctx[word], ctx[word + 1] = ctypes.addressof(self.buf), self.size
        self.users.append((ctx, word))

    def grow(self, delta: int, init: int) -> int:
        """table.grow: the old size, or -1 (as u32)."""
        old = self.size
        if delta > self.max - old:
            return M32
        if old + delta > len(self.buf):
            buf = (ctypes.c_uint64 * max(old + delta, 2 * len(self.buf)))()
            ctypes.memmove(buf, self.buf, 8 * old)
            self.buf = buf
        if init:
            self.buf[old : old + delta] = [init] * delta
        self.size = old + delta
        for ctx, word in self.users:
            ctx[word], ctx[word + 1] = ctypes.addressof(self.buf), self.size
        return old

    def write(self, at: int, values: Sequence[int]) -> None:
        """Elements `at`... (table.init, segments): all or nothing."""
        if at + len(values) > self.size:
            raise Trap(TrapCode.TABLE_OUT_OF_BOUNDS, "out of bounds table access")
        self.buf[at : at + len(values)] = list(values)


def _no_service(args: Sequence[Raw]) -> list[Raw]:
    raise RuntimeError("JIT code called a runtime service this instance does not provide")


def _check_store(store: StoreState, kind: str, value: object) -> None:
    """Imports come from the same store only (as Wasmtime): a table, function, memory or global
    of another store would let code run on that store's contexts and value stack."""
    owner = (
        value.inst.store
        if isinstance(value, JitFunc)
        else value.owner.store
        if isinstance(value, GlobalCell) and isinstance(value.owner, Instance)
        else getattr(value, "store", None)
        if isinstance(value, (Table, Memory))
        else None
    )
    if owner is not None and owner is not store:
        raise ValueError(f"{kind} import belongs to another store")


class Instance:
    """`funcs`: (code offset, result count) of each defined function; function indices count the
    imports first. `imports`: (host function, parameter count, result masks)."""

    def __init__(
        self,
        store: StoreState,
        tramp: Trampolines,
        code: bytes,
        funcs: Sequence[tuple[int, int]],
        imports: Sequence[tuple[HostFunc, int, Sequence[int]]] = (),
        memories: Sequence[Memory] = (),
        nglobals: int = 0,
        layout: Layout | None = None,
    ) -> None:
        self.store, self.tramp, self.funcs = store, tramp, funcs
        self.nimports = len(imports)
        self.layout = layout or Layout(
            memories=len(memories), globals=nglobals, imports=len(imports)
        )
        self.ctx = (ctypes.c_uint64 * self.layout.words)()
        self.ctx[HOST_BRIDGE] = tramp.bridge
        self.ctx[TAIL_SWITCH] = tramp.tail_switch
        st = _stacks(store)  # call_indirect may enter this context without `call`: limits now
        self.ctx[NSTACK_LIMIT], self.ctx[VSTACK_LIMIT] = st.nlimit, st.vtop
        self.code: ExecMem = load(code)
        self.entry = ENTRY(tramp.entry)
        self.callbacks = [HOST(self._host(*im)) for im in imports]  # kept alive here
        for i, cb in enumerate(self.callbacks):
            self.ctx[self.layout.import_(i)] = ctypes.cast(cb, ctypes.c_void_p).value or 0
        for i, mem in enumerate(memories):
            mem.bind(self.ctx, self.layout.memory(i))
        if memories:  # delta, then the memory index if there are several (S0003)
            many = len(memories) > 1

            def grow_(a: Sequence[Raw]) -> list[Raw]:  # -1 at the memory's address width
                m = memories[a[1] if many else 0]
                return [m.grow(a[0]) & m.mask]

            grow = HOST(self._host(grow_, 1 + many, [M64]))
            self.callbacks.append(grow)  # a runtime service (libcall), not a computation (D25)
            self.ctx[self.layout.libcall(MEMORY_GROW)] = (
                ctypes.cast(grow, ctypes.c_void_p).value or 0
            )
        self.memories = list(memories)
        self.memory = self.memories[0] if memories else None  # memory 0
        # every libcall word is callable: one the instance does not provide reports an error
        # instead of the bridge calling address 0 (a subclass replaces the ones it serves)
        missing = HOST(self._host(_no_service, 0, []))
        self.callbacks.append(missing)
        for j in range(LIBCALLS):
            if not self.ctx[self.layout.libcall(j)]:
                self.ctx[self.layout.libcall(j)] = ctypes.cast(missing, ctypes.c_void_p).value or 0
        store.instances.append(self)

    def call(self, func_index: int, args: Sequence[Raw]) -> list[Raw]:
        offset, nres = self.funcs[func_index - self.nimports]
        store, st, ctx = self.store, _stacks(self.store), self.ctx
        depth, active = store.depth, st.active  # restored as they were, whatever interrupts us
        try:
            if st.guard:  # records cross within a store: any entry may reach guard code
                guard.check()
            store.enter()
            st.active += 1
            if st.active == 1:  # no JIT frame below, whatever the store's depth
                st.vsp = st.vstack.addr
                nlimit = self._nstack_limit(st.vsp)
                if nlimit != st.nlimit:  # every context: call_indirect switches between them
                    st.nlimit = nlimit
                    for inst in store.instances:
                        if isinstance(inst, Instance):
                            inst.ctx[NSTACK_LIMIT] = nlimit
            frame, n = st.vsp, max(len(args), nres)
            if frame + 8 * n > st.vtop:
                raise Trap(TrapCode.STACK_OVERFLOW, "call stack exhausted")
            slots = (ctypes.c_uint64 * n).from_address(frame)
            slots[: len(args)] = list(args)
            status = self.entry(ctypes.addressof(ctx), frame, self.code.addr + offset)
            if status:
                raise self._error(status)
            return list(slots[:nres])
        finally:
            st.active, store.depth = active, depth

    def _nstack_limit(self, frame: int) -> int:
        """At the first JIT entry on the stack: the stack pointer as wasm code sees it (the
        stub), then `native_budget`."""
        if self.entry(ctypes.addressof(self.ctx), frame, self.tramp.sp_stub):
            raise RuntimeError("stack-pointer stub failed")
        sp = ctypes.c_uint64.from_address(frame).value
        return sp - native_budget(sp, _stack_low(), self.store.max_wasm_stack or DEFAULT_BUDGET)

    def _error(self, status: int) -> BaseException:
        if status == STATUS_HOST_ERROR:
            exc, self.store.pending = self.store.pending, None
            if exc is None:  # the callback never wrote its status (D34)
                exc = RuntimeError(
                    "host call interrupted before it completed (an exception escaped the ctypes "
                    "callback)"
                )
            return exc if self.store.depth == 1 else HostError(exc)
        if status == STATUS_INTERRUPT and _stacks(self.store).active == 1:
            # The outermost JIT entry consumes every interrupt() made so far, in every context of
            # the store (under the store's lock); one made later stays set. A nested entry leaves
            # the word set, so frames below stop at their next poll even if a host catches this.
            with self.store.interrupt_lock:
                for inst in self.store.instances:
                    if isinstance(inst, Instance):
                        inst.ctx[INTERRUPT] = 0
        try:
            return Trap(TrapCode(status))
        except ValueError:
            return RuntimeError(f"JIT code returned status {status}")

    def _host(self, f: HostFunc, nparams: int, masks: Sequence[int]) -> Callable[[int, int], int]:
        """The status of a host call is context word HOST_ERROR, which the bridge presets to 13
        (D34): the callback writes the results, then the status, as its last action. An
        asynchronous exception (KeyboardInterrupt) escaping the callback leaves the preset: the
        call fails (`_error`), never "succeeds" with unwritten results. The C return value is
        ignored (ctypes leaves it undefined when an exception escapes). A host exception waits in
        the store."""
        store, n = self.store, max(nparams, len(masks))

        def serve(slots: int) -> int:
            try:
                vals = (ctypes.c_uint64 * n).from_address(slots)
                res = f(vals[:nparams])
                for i, (r, m) in enumerate(zip(res, masks, strict=True)):
                    vals[i] = r & m
                return 0
            except Trap as e:
                if e.code in _TRAP_STATUSES:
                    return int(e.code)
                store.pending = e  # not a wasm trap code (e.g. 13): a host exception like any other
                return STATUS_HOST_ERROR
            except BaseException as e:
                store.pending = e.exc if isinstance(e, HostError) else e
                return STATUS_HOST_ERROR

        def callback(ctx: int, slots: int) -> int:
            st = _stacks(store)
            saved, st.vsp = st.vsp, slots + 8 * n  # a nested entry starts above these slots
            status = STATUS_HOST_ERROR
            try:
                status = serve(slots)
                why = st.guard and not status and guard.broken()  # back into guard code
                if why:
                    store.pending = RuntimeError(f"{guard.LOST} ({why})")
                    status = STATUS_HOST_ERROR
            except BaseException as e:
                store.pending = store.pending or e
            finally:
                st.vsp = saved
                ctypes.c_uint64.from_address(ctx + 8 * HOST_ERROR).value = status  # last
            return 0

        return callback

    def interrupt(self) -> None:
        """Any thread: the next poll of this instance's code returns INTERRUPT."""
        self.ctx[INTERRUPT] = 1

    def export(self, name: str) -> Extern:
        raise LookupError(f"no export {name}")  # exports come with the compiler (3b)

    def limits(self, name: str) -> tuple[int, int | None]:
        raise LookupError(f"no export {name}")

    def global_get(self, global_index: int) -> Raw:
        raise LookupError(f"no global {global_index}")


@dataclass(frozen=True)
class GlobalCell:
    """An exported global: the address of its 64-bit word, and the instance that owns it."""

    addr: int
    owner: object


class JitFunc:
    """An exported function of a JIT instance: a `HostFunc` for the host, and the contents of its
    function record for an importer or a table element in another instance of the store (D30)."""

    def __init__(self, inst: "JitInstance", idx: int) -> None:
        self.inst, self.idx = inst, idx

    def __call__(self, args: Sequence[Raw]) -> list[Raw]:
        return self.inst.call(self.idx, args)

    def record(self) -> tuple[int, int, int]:
        inst = self.inst
        offset = inst.funcs[self.idx - inst.nimports][0]
        tid = type_id(inst.module["types"], inst.ftypes[self.idx])
        return inst.code.addr + offset, tid, ctypes.addressof(inst.ctx)


def _ref_host(st: _StoreStacks, f: HostFunc, ps: Sequence[Any], rs: Sequence[Any]) -> HostFunc:
    """A host function whose signature has references: they cross as objects (S0002)."""

    def call(args: Sequence[Raw]) -> list[Raw]:
        objs: list[Any] = [
            st.from_raw(t, a) if isref(t) else a for t, a in zip(ps, args, strict=True)
        ]
        res = f(objs)
        return [st.to_raw(t, r) if isref(t) else r for t, r in zip(rs, res, strict=True)]

    return cast(HostFunc, call)


def _v128_host(f: HostFunc, ps: Sequence[Any], rs: Sequence[Any]) -> HostFunc:
    """A host function whose signature has v128: it gets and returns ints, the code two slots."""

    def call(args: Sequence[Raw]) -> list[Raw]:
        return simd.split(rs, f(simd.join(ps, args)))

    return cast(HostFunc, call)


def _masks(ts: Sequence[Any]) -> list[int]:
    """One mask per slot of values of the types `ts`."""
    return [m for t in ts for m in (mask(t),) * (1 + (t == simd.V128))]


class JitInstance(Instance):
    """An instance of a compiled module (S0002 `TargetInstance`): imports wired into the
    context, function records and signature ids written, tables bound, globals initialised,
    element and data segments applied (in that order, as the spec does), the start function
    run."""

    def __init__(
        self,
        store: StoreState,
        tramp: Trampolines,
        c: Compiled,
        imports: Sequence[Extern],
        owner: str,
    ) -> None:
        mod = self.module = c.module
        types = mod["types"]
        self.owner = owner
        st = _stacks(store)
        hosts: list[tuple[HostFunc, int, Sequence[int]]] = []
        self.hosts: list[HostFunc] = []
        memories: list[Memory] = []
        tables: list[Table] = []
        self.gaddr: list[int] = []
        self.cells: list[GlobalCell] = []  # keeps the owners of imported globals alive
        for im, ext in zip(mod["imports"], imports, strict=True):
            _check_store(store, im[0], ext.value)
            if im[0] == "func":
                ps, rs = types[im[3]]
                f = cast(HostFunc, ext.value)
                self.hosts.append(f)
                if any(isref(t) for t in (*ps, *rs)):
                    f = _ref_host(st, f, ps, rs)
                if simd.V128 in (*ps, *rs):
                    f = _v128_host(f, ps, rs)
                hosts.append((f, simd.width(ps), _masks(rs)))
            elif im[0] == "mem":
                memories.append(cast(Memory, ext.value))
                if memories[-1].pages < im[3][0]:  # the i64 bounds check relies on it (S0009)
                    raise LinkError(f"incompatible import type {im[1]}.{im[2]}")
            elif im[0] == "table":
                tables.append(cast(Table, ext.value))
            else:
                self.cells.append(cast(GlobalCell, ext.value))
        for mt in mod["mems"]:
            memories.append(Memory(*mt, a64=mod["maddr"][len(memories)] == I64))
            memories[-1].store = store
        self.ftypes: list[int] = [im[3] for im in mod["imports"] if im[0] == "func"] + mod["funcs"]
        self.gtypes = [im[3][0] for im in mod["imports"] if im[0] == "global"]
        self.gtypes += [g[0] for g in mod["globals"]]
        ng = len(self.gtypes)
        super().__init__(store, tramp, c.code, c.funcs, hosts, memories, ng, c.layout)
        lay, ctx = self.layout, self.ctx
        if c.flags & guard.BOUNDS_GUARD:  # before any of its code can run (S0006)
            guard.add_code(self.code, c.faults)
            for m in memories:
                guard.add_reservation(m.res)
            st.guard = True
        base = ctypes.addressof(ctx)
        self.records = {f: base + 8 * lay.record(r) for r, f in enumerate(c.records)}
        for f, addr in self.records.items():
            v = self.hosts[f] if f < self.nimports else None
            if f >= self.nimports:
                rec = JitFunc(self, f).record()
            elif isinstance(v, JitFunc) and v.inst.store is store:
                rec = v.record()  # a direct call into the exporting instance
            else:  # a host function: the module's thunk calls it through the bridge
                rec = (self.code.addr + c.thunks[f], type_id(types, self.ftypes[f]), base)
            word = (addr - base) // 8
            ctx[word : word + RECORD] = list(rec)
        for k, ti in enumerate(c.sigs):
            ctx[lay.sig(k)] = type_id(types, ti)
        for i in range(ng):
            word = lay.global_(i)
            if i < len(self.cells):
                ctx[word] = self.cells[i].addr
                self.gaddr.append(self.cells[i].addr)
            else:
                t, _, init = mod["globals"][i - len(self.cells)]
                if t == simd.V128:
                    ctx[word : word + 2] = simd.split([t], [self._const(init)])
                else:
                    ctx[word] = self._const(init) & mask(t)
                self.gaddr.append(base + 8 * word)
        for (mn, mx), init in zip(mod["tables"], mod["tinit"], strict=True):
            tables.append(Table(mn, mx, 0 if init is None else self._const(init), store))
        for i, t in enumerate(tables):
            t.bind(ctx, lay.table(i))
        self.tables = tables
        # segments, as the runtime services table.init / memory.init see them (dropped: empty)
        self.elems = [[self._const(e) for e in items] for _, _, _, items in mod["elem_info"]]
        self.datas = [bytes(d) for _, d in mod["datas"]]
        services: list[tuple[int, Callable[[Sequence[Raw]], Sequence[Raw]], int, int]] = []
        if tables:
            services.append((TABLE_GROW, self._table_grow, 3, 1))
        if self.elems:
            services += [(TABLE_INIT, self._table_init, 5, 0), (ELEM_DROP, self._elem_drop, 1, 0)]
        if self.datas:  # data.drop needs no memory (a module may have passive data only)
            services.append((DATA_DROP, self._data_drop, 1, 0))
        if self.datas and memories:
            services.append((MEMORY_INIT, self._memory_init, 4 + (len(memories) > 1), 0))
        for j, fn, nargs, nres in services:  # runtime services (libcalls), not computation (D25)
            cb = HOST(self._host(fn, nargs, [M32] * nres))
            self.callbacks.append(cb)
            ctx[lay.libcall(j)] = ctypes.cast(cb, ctypes.c_void_p).value or 0
        segs: list[tuple[int, Any, Any, Any]] = mod["elem_info"]
        for i, ((ti, off, _, _), mode) in enumerate(zip(segs, mod["elem_mode"], strict=True)):
            if mode == "active":
                tables[ti].write(self._const(off) & M32, self.elems[i])
            if mode != "passive":
                self.elems[i] = []
        for i, ((off, data), mi) in enumerate(zip(mod["datas"], mod["data_mem"], strict=True)):
            if mi is None:
                continue
            o, mem = self._const(off), memories[cast(int, mi)]
            if o + len(data) > mem.pages << 16:
                raise Trap(TrapCode.MEMORY_OUT_OF_BOUNDS, "out of bounds memory access")
            ctypes.memmove(mem.res.addr + o, bytes(data), len(data))
            self.datas[i] = b""
        if mod["start"] is not None:
            self.call(mod["start"], [])

    def _const(self, e: tuple[Any, ...]) -> int:
        k = e[0]
        if k in ("i32", "i64", "f32", "f64", "v128"):
            return e[1]
        if k == "global":
            g: int = e[1]
            return self._read_global(g)
        if k == "func":
            return self.records[e[1]]
        if k == "null":
            return 0
        x, y, m = self._const(e[2]), self._const(e[3]), M32 if e[1] == "i32" else M64
        return (x + y if k == "add" else x - y if k == "sub" else x * y) & m

    # runtime services (libcalls): arguments as the compiler put them in the slots
    def _table_grow(self, a: Sequence[Raw]) -> list[Raw]:
        init, delta, ti = a
        return [self.tables[ti].grow(delta, init)]

    def _table_init(self, a: Sequence[Raw]) -> list[Raw]:
        d, s, n, seg, ti = a
        items = self.elems[seg]
        if s + n > len(items):
            raise Trap(TrapCode.TABLE_OUT_OF_BOUNDS, "out of bounds table access")
        self.tables[ti].write(d, items[s : s + n])
        return []

    def _elem_drop(self, a: Sequence[Raw]) -> list[Raw]:
        self.elems[a[0]] = []
        return []

    def _memory_init(self, a: Sequence[Raw]) -> list[Raw]:
        d, s, n, seg = a[:4]  # then the memory index if there are several (S0003)
        data, memory = self.datas[seg], self.memories[a[4] if len(a) > 4 else 0]
        if s + n > len(data) or d + n > memory.pages << 16:
            raise Trap(TrapCode.MEMORY_OUT_OF_BOUNDS, "out of bounds memory access")
        ctypes.memmove(memory.res.addr + d, data[s : s + n], n)
        return []

    def _data_drop(self, a: Sequence[Raw]) -> list[Raw]:
        self.datas[a[0]] = b""
        return []

    def call(self, func_index: int, args: Sequence[Raw]) -> list[Raw]:
        if func_index < self.nimports:
            depth = self.store.depth  # an exported import: the host function itself (refs: objects)
            try:
                self.store.enter()
                return list(self.hosts[func_index](args))
            except HostError as e:
                if self.store.depth == 1:
                    raise e.exc from None
                raise
            finally:
                self.store.depth = depth
        ps, rs = self.module["types"][self.ftypes[func_index]]
        if not any(isref(t) or t == simd.V128 for t in (*ps, *rs)):
            return super().call(func_index, args)
        st = _stacks(self.store)
        raw = [st.to_raw(t, a) if isref(t) else a for t, a in zip(ps, args, strict=True)]
        res = simd.join(rs, super().call(func_index, simd.split(ps, raw)))
        return [st.from_raw(t, r) if isref(t) else r for t, r in zip(rs, res, strict=True)]  # pyright: ignore[reportReturnType]

    def export(self, name: str) -> Extern:
        exports: list[tuple[str, int, int]] = self.module["exports"]
        for n, kind, idx in exports:
            if n != name:
                continue
            if kind == 0:
                f = self.hosts[idx] if idx < self.nimports else JitFunc(self, idx)
                return Extern("func", self.owner, f)
            if kind == 1:
                return Extern("table", self.owner, self.tables[idx])
            if kind == 2:
                return Extern("memory", self.owner, self.memories[idx])
            if kind == 3:
                return Extern("global", self.owner, GlobalCell(self.gaddr[idx], self))
        raise LookupError(f"no export {name}")

    def limits(self, name: str) -> tuple[int, int | None]:
        exports: list[tuple[str, int, int]] = self.module["exports"]
        for n, k, idx in exports:
            if n == name and k == 1:
                return self.tables[idx].size, self.tables[idx].declared
            if n == name and k == 2:
                return self.memories[idx].pages, self.memories[idx].declared
        raise LookupError(f"no exported memory or table {name}")

    def _read_global(self, g: int) -> int:
        """The value of global g (a v128 from its two words)."""
        if self.gtypes[g] == simd.V128:
            lo, hi = (ctypes.c_uint64 * 2).from_address(self.gaddr[g])
            return lo | hi << 64
        return ctypes.c_uint64.from_address(self.gaddr[g]).value

    def global_get(self, global_index: int) -> Raw:
        r = self._read_global(global_index)
        t = self.gtypes[global_index]
        return _stacks(self.store).from_raw(t, r) if isref(t) else r  # pyright: ignore[reportReturnType]
