"""The JIT context ABI (docs/specs/S0003, test plan of step 3a): functions built by hand from the
backend's ABI methods, run through the entry trampoline of this machine's convention."""

import ctypes
import importlib.util
import json
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import cast

import pytest

from tests.jit import need
from w2py.runtime.link import host_func
from w2py.runtime.store import StoreState
from w2py.targets.base import HostFunc, Raw, Trap, TrapCode
from w2py.targets.jit.common.context import HOST_ERROR, INTERRUPT, TRAP_CODE, Layout
from w2py.targets.jit.common.execmem import load
from w2py.targets.jit.common.instance import (
    DEFAULT_BUDGET,
    Instance,
    Memory,
    Trampolines,
    linux_main_low,
    native_budget,
)
from w2py.targets.jit.x86_64 import JitX86_64Target
from w2py.targets.jit.x86_64.abi import bridge, entry, trampolines
from w2py.targets.jit.x86_64.backend import Backend, Frame
from w2py.targets.jit.x86_64.encoder import (
    R8,
    R9,
    R10,
    R11,
    R12,
    R14,
    RAX,
    RBP,
    RBX,
    RCX,
    RDI,
    RSI,
    Asm,
    Label,
    Mem,
)

Emit = Callable[[Backend, Frame, list[Label]], None]
M32, M64 = 0xFFFFFFFF, (1 << 64) - 1
ENDBR64 = b"\xf3\x0f\x1e\xfa"
POOL = (RBX, RBP, RSI, RDI, R8, R9, R10, R11)


@pytest.fixture
def tramp() -> Trampolines:
    need(JitX86_64Target().probe() is None, "x86-64 executable memory")
    return trampolines()


def build(
    store: StoreState,
    tramp: Trampolines,
    funcs: Sequence[tuple[tuple[int, ...], int, int, Emit]],  # (pins, frame slots, results, body)
    imports: Sequence[tuple[HostFunc, int, Sequence[int]]] = (),
    memory: Memory | None = None,
) -> Instance:
    b = Backend(check_align=True)  # every call site checks rsp % 16 (S0003, "Stack alignment")
    labels = [b.new_label() for _ in funcs]
    table: list[tuple[int, int]] = []
    for label, (pins, nslots, nres, body) in zip(labels, funcs, strict=True):
        table.append((len(b.a.buf), nres))
        f = b.func_begin(label, pins)
        body(b, f, labels)
        b.ret_ok(f)
        b.func_end(f, nslots)
    inst = Instance(store, tramp, b.a.finish(), table, imports, memory)
    code = ctypes.string_at(inst.code.addr, len(b.a.buf))
    assert all(code[off : off + 4] == ENDBR64 for off, _ in table)
    return inst


def word(nimports: int, i: int, memories: int = 0) -> int:
    return Layout(memories=memories, imports=nimports).import_(i)


def nop(b: Backend, f: Frame, ls: list[Label]) -> None:
    pass


def counter(log: list[str], name: str) -> tuple[HostFunc, int, list[int]]:
    """An import without parameters or results that logs its calls."""

    def host(args: Sequence[Raw]) -> list[Raw]:
        log.append(name)
        return []

    return host, 0, []


def test_trampolines_are_landing_pads(tramp: Trampolines) -> None:
    for addr in (tramp.entry, tramp.bridge, tramp.sp_stub, tramp.tail_switch):
        assert ctypes.string_at(addr, 4) == ENDBR64
    for win64 in (False, True):  # the Win64 shape assembles everywhere; it runs on Windows CI
        a = Asm()
        entry(a, win64)
        bridge(a, win64)
        assert a.finish().count(ENDBR64) == 2


@pytest.mark.parametrize("wide", [False, True])
@pytest.mark.parametrize("n", range(7))
def test_entry_args_and_results(tramp: Trampolines, n: int, wide: bool) -> None:
    def body(b: Backend, f: Frame, ls: list[Label]) -> None:
        b.a.mov_imm(RAX, 0)
        for i in range(n):
            b.a.alu("add", RAX, Mem(R12, 8 * i), w=wide)  # i32: 32-bit ops zero-extend
        b.a.mov(Mem(R12), RAX, w=True)

    inst = build(StoreState(), tramp, [((), 7, 1, body)])
    mask = M64 if wide else M32
    args = [mask - i for i in range(n)]
    assert inst.call(0, args) == [sum(args) & mask]


TRAPS = [*TrapCode.__members__.values()]


@pytest.mark.parametrize("code", TRAPS, ids=[c.name for c in TRAPS])
def test_each_trap_code_and_instance_still_usable(tramp: Trampolines, code: TrapCode) -> None:
    def trap(b: Backend, f: Frame, ls: list[Label]) -> None:
        b.trap(f, code)

    def ok(b: Backend, f: Frame, ls: list[Label]) -> None:
        b.a.mov_imm(Mem(R12), 7, w=True)

    inst = build(StoreState(), tramp, [((RBX,), 1, 0, trap), ((), 1, 1, ok)])
    with pytest.raises(Trap) as exc:
        inst.call(0, [])
    assert exc.value.code is code and inst.ctx[TRAP_CODE] == code
    assert inst.call(1, []) == [7]


def chain(depth: int, last: Emit) -> list[tuple[tuple[int, ...], int, int, Emit]]:
    """Function k: host `count`, call k+1 (one slot up), host `after`; the last runs `last`."""

    def link(k: int) -> Emit:
        def body(b: Backend, f: Frame, ls: list[Label]) -> None:
            b.call_host(f, word(2, 0), 0)
            if k + 1 < depth:
                b.call_direct(f, ls[k + 1], 1)
                b.call_host(f, word(2, 1), 0)
            else:
                last(b, f, ls)

        return body

    return [((RBX, RSI)[: k % 3], 2, 0, link(k)) for k in range(depth)]


def test_trap_three_calls_deep_returns_through_every_frame(tramp: Trampolines) -> None:
    calls: list[str] = []

    def unreachable(b: Backend, f: Frame, ls: list[Label]) -> None:
        b.trap(f, TrapCode.UNREACHABLE)

    hosts = [counter(calls, "count"), counter(calls, "after")]
    inst = build(StoreState(), tramp, chain(3, unreachable), hosts)
    for _ in range(2):  # and again: nothing was left behind
        calls.clear()
        with pytest.raises(Trap) as exc:
            inst.call(2, [])
        assert exc.value.code is TrapCode.UNREACHABLE and calls == ["count"] * 3


def test_reentry_inner_trap_reaches_the_host(tramp: Trampolines) -> None:
    caught: list[TrapCode] = []

    def host(args: Sequence[Raw]) -> list[Raw]:
        try:
            inst.call(2, [])
        except Trap as e:
            caught.append(e.code)
        return [41]

    def outer(b: Backend, f: Frame, ls: list[Label]) -> None:
        b.call_host(f, word(1, 0), 0)
        b.a.alu_imm("add", Mem(R12), 1, w=True)

    def inner(b: Backend, f: Frame, ls: list[Label]) -> None:
        b.trap(f, TrapCode.INTEGER_OVERFLOW)

    inst = build(
        StoreState(), tramp, [((RBX,), 1, 1, outer), ((), 1, 0, inner)], [(host, 0, [M32])]
    )
    assert inst.call(1, []) == [42] and caught == [TrapCode.INTEGER_OVERFLOW]


def test_reentry_50_deep(tramp: Trampolines) -> None:
    store = StoreState()

    def host(args: Sequence[Raw]) -> list[Raw]:
        assert store.depth == 51 - args[0]
        return [inst.call(1, [args[0] - 1])[0] + 1 if args[0] else 1000]

    def g(b: Backend, f: Frame, ls: list[Label]) -> None:
        b.call_host(f, word(1, 0), 0)

    inst = build(store, tramp, [((RSI, RDI, R8), 1, 1, g)], [(host, 1, [M32])])
    assert inst.call(1, [50]) == [1050] and store.depth == 0


# a Trap whose code is no wasm trap (13 is HOST_ERROR's status) is a host exception, not a status
ODD_TRAP = Trap(cast(TrapCode, 13), "trap 13")


@pytest.mark.parametrize("exc", [ValueError("v"), KeyboardInterrupt(), IndexError("i"), ODD_TRAP])
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("wrapped", [False, True])  # the runtime's HostError wrapper, or plain
def test_host_exception_surfaces_as_itself(
    tramp: Trampolines, exc: BaseException, nested: bool, wrapped: bool
) -> None:
    def raises(args: Sequence[Raw]) -> list[Raw]:
        raise exc

    def outer(args: Sequence[Raw]) -> list[Raw]:
        return inst.call(3, [])  # the inner entry raises HostError; it passes through here

    def call0(b: Backend, f: Frame, ls: list[Label]) -> None:
        b.call_host(f, word(2, 0), 0)

    def call1(b: Backend, f: Frame, ls: list[Label]) -> None:
        b.call_host(f, word(2, 1), 0)

    hosts = [raises, outer] if not wrapped else [host_func(raises), host_func(outer)]
    funcs = [((RBX,), 1, 0, call1 if nested else call0), ((), 1, 0, call0)]
    inst = build(StoreState(), tramp, funcs, [(hosts[0], 0, []), (hosts[1], 0, [])])
    with pytest.raises(type(exc)) as got:
        inst.call(2, [])
    assert got.value is exc and inst.store.pending is None and inst.store.depth == 0


def test_pins_of_every_frame_survive_a_clobbering_host(tramp: Trampolines) -> None:
    """A host written as machine code zeroes every register its convention lets it clobber."""
    clobber = [R8, R9, R10, R11] + ([] if sys.platform == "win32" else [RSI, RDI])
    a = Asm()
    a.endbr64()
    ctx = {"win32": RCX}.get(sys.platform, RDI)  # the first C argument
    a.mov_imm(Mem(ctx, 8 * HOST_ERROR), 0, w=True)  # the status word (D34), before ctx is lost
    for r in clobber:
        a.mov_imm(r, 0)
    a.mov_imm(RAX, 77)  # the C return value is ignored
    a.ret()
    host_code = load(a.finish())
    frames = [POOL, (RSI, RDI, R8, RBX), (R9, R10, R11)]
    bit = {(k, r): 1 << (8 * k + i) for k, pins in enumerate(frames) for i, r in enumerate(pins)}

    def frame(k: int) -> Emit:
        def body(b: Backend, f: Frame, ls: list[Label]) -> None:
            pins = frames[k]
            for r in pins:
                b.a.mov_imm(r, bit[k, r], w=True)
            if k < 2:
                b.call_direct(f, ls[k + 1], 1)
                b.a.mov(RAX, Mem(R12, 8), w=True)
            else:
                b.call_host(f, word(1, 0), 0)
                b.a.mov_imm(RAX, 0)
            for r in pins:
                b.a.alu("add", RAX, r, w=True)
            b.a.mov(Mem(R12), RAX, w=True)

        return body

    funcs = [(frames[k], 2, 1, frame(k)) for k in range(3)]
    inst = build(StoreState(), tramp, funcs, [counter([], "unused")])
    inst.ctx[word(1, 0)] = host_code.addr
    assert inst.call(1, []) == [sum(bit.values())]


def test_interrupt_from_another_thread(tramp: Trampolines) -> None:
    store = StoreState()

    def spin(b: Backend, f: Frame, ls: list[Label]) -> None:
        head = b.new_label()
        b.a.bind(head)
        b.poll(f)
        b.a.jmp(head)

    inst = build(store, tramp, [((RBX,), 1, 0, spin), ((), 1, 0, nop)])
    sent: list[float] = []

    def stop() -> None:
        time.sleep(0.2)
        sent.append(time.perf_counter())
        store.interrupt()

    t = threading.Thread(target=stop)
    t.start()
    with pytest.raises(Trap) as exc:
        inst.call(0, [])
    elapsed = time.perf_counter() - sent[0]
    t.join()
    assert exc.value.code is TrapCode.INTERRUPT and elapsed < 0.05
    assert inst.ctx[INTERRUPT] == 0 and inst.call(1, []) == []  # cleared with the trap


def test_interrupt_is_consumed_in_every_context_of_the_store(tramp: Trampolines) -> None:
    store = StoreState()

    def spin(b: Backend, f: Frame, ls: list[Label]) -> None:
        head = b.new_label()
        b.a.bind(head)
        b.poll(f)
        b.a.jmp(head)

    a = build(store, tramp, [((), 1, 0, spin)])
    b = build(store, tramp, [((), 1, 0, nop)])
    timer = threading.Timer(0.1, store.interrupt)
    timer.start()
    with pytest.raises(Trap) as exc:
        a.call(0, [])
    timer.join()
    assert exc.value.code is TrapCode.INTERRUPT and b.call(0, []) == []
    store.interrupt()  # made while idle: stays set for the next call
    with pytest.raises(Trap):
        b.call(0, [])


def recursion(shift: int, nslots: int) -> list[tuple[tuple[int, ...], int, int, Emit]]:
    def body(b: Backend, f: Frame, ls: list[Label]) -> None:
        b.call_host(f, word(1, 0), 0)
        b.call_direct(f, ls[0], shift)

    return [((RBX,), nslots, 0, body)]


def run_native_recursion(store: StoreState, tramp: Trampolines) -> tuple[int, TrapCode]:
    calls: list[str] = []
    inst = build(store, tramp, recursion(0, 1), [counter(calls, "frame")])
    with pytest.raises(Trap) as exc:
        inst.call(1, [])
    return len(calls), exc.value.code


def test_native_stack_overflow_small_budget(tramp: Trampolines) -> None:
    depth, code = run_native_recursion(StoreState(max_wasm_stack=64 << 10), tramp)
    assert code is TrapCode.STACK_OVERFLOW and 64 << 10 >> 5 < depth <= 64 << 10 >> 4


def test_native_limit_set_by_the_first_jit_entry_at_any_store_depth(tramp: Trampolines) -> None:
    store = StoreState()
    store.enter()  # as if another entry of the store were below
    try:
        assert run_native_recursion(store, tramp)[1] is TrapCode.STACK_OVERFLOW
    finally:
        store.leave()


DEEPEST_HOST = """
import sys, threading
from tests.jit.test_abi import build, recursion
from w2py.runtime.store import StoreState
from w2py.targets.base import Trap
from w2py.targets.jit.common import instance
from w2py.targets.jit.x86_64.abi import trampolines
depth, kib, jit = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3] == "jit"
x = []
for _ in range(depth):
    x = [x]
def host(args):
    try:
        repr(x)  # deep C recursion (at the deepest wasm frame)
    except RecursionError:
        pass
    return []
def run():
    if not jit:
        host([])
        print("HOST_OK")
        return
    store = StoreState()
    inst = build(store, trampolines(), recursion(0, 1), [(host, 0, [])])
    try:
        inst.call(1, [])
    except Trap as e:
        print(e.code.name)
    low, limit = instance._stack_low(), store.target_state.nlimit
    share = (limit - low) >> 10
    print(f"low {low:#x} limit {limit:#x} host share {share} KiB", file=sys.stderr)
threading.stack_size(kib << 10)
t = threading.Thread(target=run)
t.start()
t.join()
"""


def child(depth: int, kib: int, mode: str) -> "subprocess.CompletedProcess[str]":
    cmd = [sys.executable, "-c", DEEPEST_HOST, str(depth), str(kib), mode]
    root = str(Path(__file__).resolve().parents[2])
    return subprocess.run(cmd, capture_output=True, text=True, cwd=root, timeout=120)


@pytest.mark.parametrize("depth,kib", [(300, 256), (900, 512)])
def test_host_at_the_deepest_frame_keeps_half_the_stack(
    tramp: Trampolines, depth: int, kib: int
) -> None:
    """A host function with deep C recursion runs at the deepest wasm frame: the budget takes at
    most half of the thread's stack (D10). Calibrated first: the same host must run on its own in
    a thread of half the size minus 32 KiB (CPython's C frames differ per build and OS); if it
    cannot, the host does not fit its share and the case says nothing about our bounds."""
    alone = child(depth, kib // 2 - 32, "alone")
    if (alone.returncode, alone.stdout.strip()) != (0, "HOST_OK"):
        pytest.skip(f"this CPython's repr of depth {depth} needs > {kib // 2 - 32} KiB of stack")
    run = child(depth, kib, "jit")
    assert (run.returncode, run.stdout.strip()) == (0, "STACK_OVERFLOW"), run.stderr[-500:]
    print(run.stderr.strip())  # the bounds, visible with -s / in the CI log on failure


def test_native_stack_overflow_capped_by_a_small_thread_stack(tramp: Trampolines) -> None:
    """The default budget (512 KiB) is more than this thread's whole stack: the cap must hold."""
    out: list[object] = []
    old = threading.stack_size(256 << 10)
    try:
        t = threading.Thread(target=lambda: out.append(run_native_recursion(StoreState(), tramp)))
        t.start()
        t.join()
    finally:
        threading.stack_size(old)
    assert out and out[0][1] is TrapCode.STACK_OVERFLOW  # pyright: ignore[reportIndexIssue]


def test_value_stack_overflow(tramp: Trampolines) -> None:
    calls: list[str] = []
    inst = build(StoreState(), tramp, recursion(100, 200), [counter(calls, "frame")])
    with pytest.raises(Trap) as exc:
        inst.call(1, [])
    assert exc.value.code is TrapCode.STACK_OVERFLOW and 1200 < len(calls) <= (1 << 20) // 800


def test_memory_reserved_committed_on_grow_never_moves(tramp: Trampolines) -> None:
    mem = Memory(1, None)

    def grow(args: Sequence[Raw]) -> list[Raw]:
        return [mem.grow(args[0]) & M32]

    def body(b: Backend, f: Frame, ls: list[Label]) -> None:
        b.call_host(f, word(1, 0, 1), 0)
        b.a.mov(Mem(R12, 8), R14, w=True)  # r14 reloaded from the context after the bridge

    inst = build(StoreState(), tramp, [((RBX,), 2, 2, body)], [(grow, 1, [M32])], mem)
    base, w = mem.res.addr, Layout(memories=1, imports=1).memory(0)
    assert (inst.ctx[w], inst.ctx[w + 1]) == (base, 1 << 16)
    ctypes.memmove(base + 0xFFFF, b"\x5a", 1)
    assert inst.call(1, [2, 0]) == [1, 3 << 16]
    assert inst.ctx[w] == base and ctypes.string_at(base + 0xFFFF, 1) == b"\x5a"
    ctypes.memmove(base + (3 << 16) - 1, b"\x01", 1)  # the new last byte is committed
    assert ctypes.string_at(base + (2 << 16), 1) == b"\0"
    assert inst.call(1, [1 << 16, 0]) == [M32, 3 << 16] and Memory(1, 2).grow(2) == -1
    with pytest.raises(ValueError):
        mem.res.commit(mem.res.size + 1)


def test_linux_main_thread_budget_from_rlimit() -> None:
    """musl reports only the mapped part of the main stack (132 KiB measured in CI); the kernel
    grows it to RLIMIT_STACK, so the budget follows the rlimit; unlimited -> unknown (128 KiB)."""
    high = 0x7FFF_F000_0000
    sp = high - (8 << 10)
    reported = high - (132 << 10)
    assert native_budget(sp, reported, DEFAULT_BUDGET) == 62 << 10  # the old result
    assert native_budget(sp, linux_main_low(high, 16 << 20), DEFAULT_BUDGET) == 512 << 10
    assert linux_main_low(high, None) is None
    assert native_budget(sp, linux_main_low(high, None), DEFAULT_BUDGET) == 128 << 10


def test_a_host_that_never_writes_its_status_fails(tramp: Trampolines) -> None:
    """As when an asynchronous exception escapes a ctypes callback before its last action: the
    status word keeps the bridge's preset 13 and the call fails, whatever the C return value
    (D34; before, a 0 return was success with unwritten results)."""
    a = Asm()
    a.endbr64()
    a.alu("xor", RAX, RAX)
    a.ret()
    host_code = load(a.finish())

    def g(b: Backend, f: Frame, ls: list[Label]) -> None:
        b.a.mov_imm(Mem(R12), 5, w=True)
        b.call_host(f, word(1, 0), 0)

    store = StoreState()
    inst = build(store, tramp, [((), 1, 1, g)], [counter([], "unused")])
    inst.ctx[word(1, 0)] = host_code.addr
    with pytest.raises(RuntimeError, match="host call interrupted"):
        inst.call(1, [])
    assert store.depth == 0 and store.pending is None and inst.ctx[HOST_ERROR] == 0


ASYNC_HOST = """
import json, signal
from tests.jit.test_compiler import build, wat
from w2py.runtime.store import StoreState
from w2py.targets.jit.common.instance import _stacks

def tick(*a):
    if st.depth:  # only inside a call: the harness below must not be hit
        raise KeyboardInterrupt

st = StoreState()
i = build(wat('''(module (import "env" "h" (func $h (param i32) (result i32)))
  (func (export "f") (param i32) (result i32) (local i32)
    (loop $l (local.set 1 (call $h (local.get 1)))
      (local.set 0 (i32.sub (local.get 0) (i32.const 1))) (br_if $l (local.get 0)))
    (local.get 1)))'''), imports={"env": {"h": lambda a: [a[0] + 1]}}, store=st)
seen = {}
signal.signal(signal.SIGALRM, tick)
signal.setitimer(signal.ITIMER_REAL, 0.002, 0.002)
for _ in range(300):
    try:
        k = "ok" if i("f", 1000) == [1000] else "wrong result"
    except KeyboardInterrupt:
        k = "KeyboardInterrupt"
    except RuntimeError as e:
        k = "interrupted" if "host call interrupted" in str(e) else repr(e)
    except BaseException as e:
        k = repr(e)
    seen[k] = seen.get(k, 0) + 1
signal.setitimer(signal.ITIMER_REAL, 0)
signal.signal(signal.SIGALRM, signal.SIG_IGN)
print(json.dumps([seen, st.depth, _stacks(st).active]))
"""


@pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="needs SIGALRM timers")
def test_async_exceptions_in_host_calls_never_give_wrong_results(tramp: Trampolines) -> None:
    """KeyboardInterrupt every 2 ms while wasm calls a host function in a loop: every call gives
    the right result or an exception, never a wrong result or a stray status, and the store's
    bookkeeping is back to zero (D34; before: wrong results, garbage statuses)."""
    need(importlib.util.find_spec("wasmtime") is not None, "wasmtime (wat2wasm)")
    root = str(Path(__file__).resolve().parents[2])
    run = subprocess.run(
        [sys.executable, "-c", ASYNC_HOST], capture_output=True, text=True, cwd=root, timeout=300
    )
    assert run.returncode == 0, run.stderr[-1000:]
    seen, depth, active = json.loads(run.stdout)
    print(seen)
    assert set(seen) <= {"ok", "KeyboardInterrupt", "interrupted"}, seen
    assert (depth, active) == (0, 0)
