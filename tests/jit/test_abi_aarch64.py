"""The context ABI on aarch64 (S0004 step 4a, S0003 test plan): functions built by hand from the
backend's frame/call methods, run through the aarch64 entry, bridge, sp stub and tail_switch; on
Linux with BTI the code pages are PROT_BTI, so every landing pad is checked too. Twin of
test_abi.py; the bodies are machine code, so they cannot be shared."""

import ctypes
import sys
import threading
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from tests.jit import native
from tests.jit.test_abi import ODD_TRAP, counter, word
from waboros.runtime.link import host_func
from waboros.runtime.store import StoreState
from waboros.targets.base import HostFunc, Raw, Trap, TrapCode
from waboros.targets.jit.aarch64 import trampolines
from waboros.targets.jit.aarch64.abi import CTX, FRAME, MBASE, MSIZE, assemble
from waboros.targets.jit.aarch64.backend import POOL, Backend, Frame
from waboros.targets.jit.aarch64.encoder import FP, LR, SP, ZR, Asm, Label
from waboros.targets.jit.common.context import HOST_ERROR, INTERRUPT, TAIL_SWITCH, TRAP_CODE, Layout
from waboros.targets.jit.common.execmem import ExecMem, load
from waboros.targets.jit.common.instance import Instance, Memory, Trampolines

Emit = Callable[[Backend, Frame, list[Label]], None]
Funcs = Sequence[tuple[tuple[int, ...], int, int, Emit]]  # (pins, frame slots, results, body)
M32, M64 = 0xFFFFFFFF, (1 << 64) - 1
BTI_C = (0xD503245F).to_bytes(4, "little")
SPARE = 10  # a reserved context word (zero for the runtime): the tests keep a record address there


@pytest.fixture
def tramp() -> Trampolines:
    native("jit-aarch64")
    return trampolines()


def build(
    store: StoreState,
    tramp: Trampolines,
    funcs: Funcs,
    imports: Sequence[tuple[HostFunc, int, Sequence[int]]] = (),
    memory: Memory | None = None,
    thunk: int | None = None,
) -> Instance:
    b = Backend()
    labels = [b.new_label() for _ in funcs]
    table: list[tuple[int, int]] = []
    for label, (pins, nslots, nres, body) in zip(labels, funcs, strict=True):
        table.append((len(b.a.buf), nres))
        f = b.func_begin(label, pins)
        body(b, f, labels)
        b.ret_ok(f)
        b.func_end(f, nslots)
    if thunk is not None:  # a host import's record target, as the last "function"
        table.append((b.host_thunk(thunk), 0))
    inst = Instance(store, tramp, b.a.finish(), table, imports, [memory] if memory else [])
    code = ctypes.string_at(inst.code.addr, len(b.a.buf))
    assert all(code[off : off + 4] == BTI_C for off, _ in table)
    return inst


def test_trampolines_are_landing_pads(tramp: Trampolines) -> None:
    for addr in (tramp.entry, tramp.bridge, tramp.sp_stub, tramp.tail_switch):
        assert ctypes.string_at(addr, 4) == BTI_C
    code, offsets = assemble()
    assert [code[o : o + 4] for o in offsets[:4]] == [BTI_C] * 4


def test_bti_is_enforced_where_the_cpu_has_it(tramp: Trampolines) -> None:
    """A function without a pad, called through a pointer, must die of SIGILL on a BTI CPU with
    PROT_BTI pages: proof that the pads above are checked, not merely present."""
    from waboros.targets.jit.common import execmem

    if not getattr(execmem, "_PROT_BTI", 0):
        cpuinfo = Path("/proc/cpuinfo")
        flags = cpuinfo.read_text().split() if cpuinfo.exists() else []
        assert "bti" not in flags, "the CPU lists bti in /proc/cpuinfo but PROT_BTI is off"
        pytest.skip("no BTI enforcement here (not Linux, or the CPU lacks BTI)")
    import subprocess

    src = ("import ctypes; from waboros.targets.jit.common.execmem import ExecMem, load; "
           "from waboros.targets.jit.aarch64.encoder import Asm; a = Asm(); "
           "a.mov_imm(0, 42, wide=False); a.ret(); m = load(a.finish()); "
           "print(ctypes.CFUNCTYPE(ctypes.c_int)(m.addr)())")  # fmt: skip
    try:  # the same function with its pad first: the child itself must work
        good = subprocess.run(
            [sys.executable, "-c", src.replace("a = Asm(); ", "a = Asm(); a.bti(); ")],
            capture_output=True,
            text=True,
        )
    except OSError as e:  # QEMU user mode without binfmt cannot start a child interpreter
        pytest.skip(f"no child interpreter: {e}")
    run = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True)
    assert good.stdout.strip() == "42" and run.returncode != 0 and "42" not in run.stdout


@pytest.mark.parametrize("wide", [False, True])
@pytest.mark.parametrize("n", range(7))
def test_entry_args_and_results(tramp: Trampolines, n: int, wide: bool) -> None:
    def body(b: Backend, f: Frame, ls: list[Label]) -> None:
        b.a.mov_imm(0, 0)
        for i in range(n):
            b.a.ld(9, FRAME, 8 * i)
            b.a.addsub("add", 0, 0, 9, wide)  # i32: 32-bit ops zero-extend
        b.a.st(0, FRAME)

    inst = build(StoreState(), tramp, [((), 7, 1, body)])
    mask = M64 if wide else M32
    args = [mask - i for i in range(n)]
    assert inst.call(0, args) == [sum(args) & mask]


# status 14 means "an exception is in flight" (S0012): no trap stub returns it
TRAPS = [c for c in TrapCode.__members__.values() if c is not TrapCode.UNCAUGHT_EXCEPTION]


@pytest.mark.parametrize("code", TRAPS, ids=[c.name for c in TRAPS])
def test_each_trap_code_and_instance_still_usable(tramp: Trampolines, code: TrapCode) -> None:
    def trap(b: Backend, f: Frame, ls: list[Label]) -> None:
        b.trap(f, code)

    def ok(b: Backend, f: Frame, ls: list[Label]) -> None:
        b.a.mov_imm(9, 7)
        b.a.st(9, FRAME)

    inst = build(StoreState(), tramp, [((19,), 1, 0, trap), ((), 1, 1, ok)])
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

    return [((19, 2, 20)[: k % 4], 2, 0, link(k)) for k in range(depth)]


def test_trap_three_calls_deep_returns_through_every_frame(tramp: Trampolines) -> None:
    calls: list[str] = []
    unreachable: Emit = lambda b, f, ls: b.trap(f, TrapCode.UNREACHABLE)  # noqa: E731
    inst = build(
        StoreState(),
        tramp,
        chain(3, unreachable),
        [counter(calls, "count"), counter(calls, "after")],
    )
    for _ in range(2):  # and again: nothing was left behind
        calls.clear()
        with pytest.raises(Trap) as exc:
            inst.call(2, [])
        assert exc.value.code is TrapCode.UNREACHABLE and calls == ["count"] * 3


def test_reentry_inner_trap_reaches_the_host_and_50_deep(tramp: Trampolines) -> None:
    caught: list[TrapCode] = []
    store = StoreState()

    def host(args: Sequence[Raw]) -> list[Raw]:
        if args[0] == 100:
            try:
                inst.call(2, [])
            except Trap as e:
                caught.append(e.code)
            return [41]
        assert store.depth == 51 - args[0]
        return [inst.call(1, [args[0] - 1])[0] + 1 if args[0] else 1000]

    def outer(b: Backend, f: Frame, ls: list[Label]) -> None:
        b.call_host(f, word(1, 0), 0)

    def inner(b: Backend, f: Frame, ls: list[Label]) -> None:
        b.trap(f, TrapCode.INTEGER_OVERFLOW)

    funcs = [((19, 2, 3), 1, 1, outer), ((), 1, 0, inner)]
    inst = build(store, tramp, funcs, [(host, 1, [M32])])
    assert inst.call(1, [100]) == [41] and caught == [TrapCode.INTEGER_OVERFLOW]
    assert inst.call(1, [50]) == [1050] and store.depth == 0


@pytest.mark.parametrize("exc", [ValueError("v"), KeyboardInterrupt(), ODD_TRAP])
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("wrapped", [False, True])
def test_host_exception_surfaces_as_itself(
    tramp: Trampolines, exc: BaseException, nested: bool, wrapped: bool
) -> None:
    def raises(args: Sequence[Raw]) -> list[Raw]:
        raise exc

    def outer(args: Sequence[Raw]) -> list[Raw]:
        return inst.call(3, [])

    def call(i: int) -> Emit:
        return lambda b, f, ls: b.call_host(f, word(2, i), 0)

    hosts = [raises, outer] if not wrapped else [host_func(raises), host_func(outer)]
    funcs = [((19,), 1, 0, call(1 if nested else 0)), ((), 1, 0, call(0))]
    inst = build(StoreState(), tramp, funcs, [(hosts[0], 0, []), (hosts[1], 0, [])])
    with pytest.raises(type(exc)) as got:
        inst.call(2, [])
    assert got.value is exc and inst.store.pending is None and inst.store.depth == 0


def machine_host(status_first: bool = True, clobber: bool = True) -> ExecMem:
    """A host in machine code: writes status 0 (or not), then zeroes x0-x17 and the caller-saved
    SIMD registers, as any AAPCS64 callee may (never x18: Windows' TEB)."""
    a = Asm()
    a.bti("c")
    if status_first:
        a.st(ZR, 0, 8 * HOST_ERROR)
    for r in range(18) if clobber else ():
        a.mov_imm(r, 0)
    for v in [*range(8), *range(16, 32)] if clobber else ():
        a.fmov_to(v, ZR)
    a.ret()
    return load(a.finish())


def test_pins_of_every_frame_survive_a_clobbering_host(tramp: Trampolines) -> None:
    frames = [POOL, (2, 19, 8, 3), (4, 5, 24)]
    bit = {(k, r): 1 << (16 * k + i) for k, pins in enumerate(frames) for i, r in enumerate(pins)}

    def frame(k: int) -> Emit:
        def body(b: Backend, f: Frame, ls: list[Label]) -> None:
            for r in frames[k]:
                b.a.mov_imm(r, bit[k, r])
            if k < 2:
                b.call_direct(f, ls[k + 1], 1)
                b.a.ld(0, FRAME, 8)
            else:
                b.call_host(f, word(1, 0), 0)
                b.a.mov_imm(0, 0)
            for r in frames[k]:
                b.a.addsub("add", 0, 0, r)
            b.a.st(0, FRAME)

        return body

    host = machine_host()
    inst = build(
        StoreState(),
        tramp,
        [(frames[k], 2, 1, frame(k)) for k in range(3)],
        [counter([], "unused")],
    )
    inst.ctx[word(1, 0)] = host.addr
    assert inst.call(1, []) == [sum(bit.values())]


def test_a_host_that_never_writes_its_status_fails(tramp: Trampolines) -> None:
    host = machine_host(status_first=False, clobber=False)

    def g(b: Backend, f: Frame, ls: list[Label]) -> None:
        b.call_host(f, word(1, 0), 0)

    store = StoreState()
    inst = build(store, tramp, [((), 1, 1, g)], [counter([], "unused")])
    inst.ctx[word(1, 0)] = host.addr
    with pytest.raises(RuntimeError, match="host call interrupted"):
        inst.call(1, [])
    assert store.depth == 0 and store.pending is None and inst.ctx[HOST_ERROR] == 0


def spin(b: Backend, f: Frame, ls: list[Label]) -> None:
    head = b.new_label()
    b.bind(head)
    b.poll(f)
    b.jump(head)


def test_interrupt_from_another_thread_and_every_context(tramp: Trampolines) -> None:
    store = StoreState()
    a = build(store, tramp, [((19,), 1, 0, spin)])
    other = build(store, tramp, [((), 1, 0, lambda b, f, ls: None)])
    timer = threading.Timer(0.2, store.interrupt)
    timer.start()
    with pytest.raises(Trap) as exc:
        a.call(0, [])
    timer.join()
    assert exc.value.code is TrapCode.INTERRUPT and a.ctx[INTERRUPT] == 0
    assert other.call(0, []) == []  # consumed in every context of the store
    store.interrupt()  # made while idle: stays set for the next call
    with pytest.raises(Trap):
        other.call(0, [])


def recursion(shift: int, nslots: int, pins: tuple[int, ...] = (19,)) -> Funcs:
    def body(b: Backend, f: Frame, ls: list[Label]) -> None:
        b.call_host(f, word(1, 0), 0)
        b.call_direct(f, ls[0], shift)

    return [(pins, nslots, 0, body)]


def run_recursion(
    store: StoreState,
    tramp: Trampolines,
    shift: int = 0,
    nslots: int = 1,
    pins: tuple[int, ...] = (19,),
) -> tuple[int, TrapCode]:
    calls: list[str] = []
    inst = build(store, tramp, recursion(shift, nslots, pins), [counter(calls, "frame")])
    with pytest.raises(Trap) as exc:
        inst.call(1, [])
    return len(calls), exc.value.code


def test_stack_overflows(tramp: Trampolines) -> None:
    """Native: 48-byte frames (record + 3 pins) in a 64 KiB budget; the first JIT entry sets the
    limit at any store depth; a 256 KiB thread caps the default budget. Value stack: 1 MiB."""
    depth, code = run_recursion(StoreState(max_wasm_stack=64 << 10), tramp, pins=(19, 20, 2))
    assert code is TrapCode.STACK_OVERFLOW and 64 << 10 >> 6 < depth <= 64 << 10 >> 5
    store = StoreState()
    store.enter()  # as if another entry of the store were below
    try:
        assert run_recursion(store, tramp)[1] is TrapCode.STACK_OVERFLOW
    finally:
        store.leave()
    out: list[tuple[int, TrapCode]] = []
    old = threading.stack_size(256 << 10)
    try:
        t = threading.Thread(target=lambda: out.append(run_recursion(StoreState(), tramp)))
        t.start()
        t.join()
    finally:
        threading.stack_size(old)
    assert out and out[0][1] is TrapCode.STACK_OVERFLOW
    depth, code = run_recursion(StoreState(), tramp, 100, 200)
    assert code is TrapCode.STACK_OVERFLOW and 1200 < depth <= (1 << 20) // 800


def test_memory_registers_reloaded_after_the_bridge(tramp: Trampolines) -> None:
    mem = Memory(1, None)

    def grow(args: Sequence[Raw]) -> list[Raw]:
        return [mem.grow(args[0]) & M32]

    def body(b: Backend, f: Frame, ls: list[Label]) -> None:
        b.call_host(f, word(1, 0, 1), 0)
        b.a.st(MSIZE, FRAME, 8)  # x25 reloaded from the context after the bridge
        b.a.st(MBASE, FRAME, 16)

    inst = build(StoreState(), tramp, [((19,), 3, 3, body)], [(grow, 1, [M32])], mem)
    assert inst.call(1, [2, 0, 0]) == [1, 3 << 16, mem.res.addr]


def test_host_thunk_through_a_record(tramp: Trampolines) -> None:
    """A record whose code is the host thunk, called with `blr`: the import runs on the caller's
    frame as its slots, x26/x25 come back reloaded, a host exception's status unwinds."""
    mem = Memory(1, None)

    def host(args: Sequence[Raw]) -> list[Raw]:
        if args[0] == 7:
            raise ValueError("seven")
        mem.grow(1)
        return [args[0] * 2]

    def body(b: Backend, f: Frame, ls: list[Label]) -> None:
        b.a.ld(16, CTX, 8 * SPARE)  # the record
        b.a.ld(17, 16)
        b.a.blr(17)
        b.a.cbz(0, f.unwind, wide=False, nz=True)
        b.a.st(MSIZE, FRAME, 8)

    w = word(1, 0, 1)
    inst = build(StoreState(), tramp, [((19,), 2, 2, body)], [(host, 1, [M64])], mem, thunk=w)
    record = (ctypes.c_uint64 * 3)(
        inst.code.addr + inst.funcs[-1][0], 0, ctypes.addressof(inst.ctx)
    )
    inst.ctx[SPARE] = ctypes.addressof(record)
    assert inst.call(1, [21, 0]) == [42, 2 << 16]
    with pytest.raises(ValueError, match="seven"):
        inst.call(1, [7, 0])


def test_tail_switch_keeps_constant_stacks_and_restores_the_context(tramp: Trampolines) -> None:
    """Tail calls between two instances' contexts, A -> B -> A -> ... (`br x17` to tail_switch,
    record in x16): the first switch leaves one restore frame, later ones hop; a 64 KiB native
    budget would overflow if any frame leaked. The direct caller in A finds x28/x26 again."""
    mems = [Memory(1, None), Memory(1, None)]

    def hop(b: Backend, f: Frame, ls: list[Label]) -> None:
        a, done = b.a, b.new_label()
        a.ld(9, FRAME)
        a.cbz(9, done)
        a.addsub_imm("sub", 9, 9, 1)
        a.st(9, FRAME)
        a.ld(16, CTX, 8 * SPARE)  # the other instance's record
        a.pair(True, FP, LR, SP, f.size, "post")  # tear down this frame (no pins)
        a.ld(17, CTX, 8 * TAIL_SWITCH)
        a.br(17)
        b.bind(done)
        a.st(CTX, FRAME, 8)
        a.st(MBASE, FRAME, 16)

    def caller(b: Backend, f: Frame, ls: list[Label]) -> None:
        b.call_direct(f, ls[1], 0)
        b.a.st(CTX, FRAME, 24)  # restored by tail_switch
        b.a.st(MBASE, FRAME, 32)

    store = StoreState(max_wasm_stack=64 << 10)
    a = build(store, tramp, [((19,), 5, 5, caller), ((), 5, 0, hop)], memory=mems[0])
    b = build(store, tramp, [((), 5, 0, hop)], memory=mems[1])
    recs = [(ctypes.c_uint64 * 3)(i.code.addr + i.funcs[-1][0], 0, ctypes.addressof(i.ctx))
            for i in (a, b)]  # fmt: skip
    a.ctx[SPARE], b.ctx[SPARE] = ctypes.addressof(recs[1]), ctypes.addressof(recs[0])
    ctx = [ctypes.addressof(i.ctx) for i in (a, b)]
    base = [m.res.addr for m in mems]
    for n in (1, 2, 100001):  # the last hop runs in B for odd n
        assert a.call(0, [n, 0, 0, 0, 0])[1:] == [ctx[n % 2], base[n % 2], ctx[0], base[0]]


def test_layout_word_used_by_the_tests_is_reserved() -> None:
    assert Layout().memory(0) > SPARE > TAIL_SWITCH
