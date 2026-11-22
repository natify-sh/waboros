# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportPrivateUsage=false, reportUnknownLambdaType=false, reportArgumentType=false
"""Conformance of the boundary convention (docs/specs/S0015, "Test plan"): tail calls across the
boundary in constant stack, statuses through C entries, the walk across modules and hop chains, and
the machine state at the boundary. The wasm-level cases run on the machine's JIT; the cases with
hand-assembled C entries are x86-64 (jit-x86_64 from S0015 step 1; aarch64 from step 3)."""

import ctypes
import sys
from collections.abc import Iterator, Sequence
from typing import Any

import pytest

from tests.jit import native
from tests.jit.test_compiler import JIT, Inst, build, trap_of, wat
from tests.jit.test_tables import provider
from tests.jit.test_tail import PING, PONG, _small_thread
from waboros.runtime.store import StoreState
from waboros.targets.base import Raw, Trap, TrapCode
from waboros.targets.jit.common import instance, walk
from waboros.targets.jit.common.context import HOST_BRIDGE, TRAP_CODE
from waboros.targets.jit.common.execmem import ExecMem, load
from waboros.targets.jit.common.instance import JitFunc, _stacks

X86 = JIT == "jit-x86_64"
M32 = 0xFFFFFFFF


@pytest.fixture(autouse=True)
def public_statuses(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[int]]:
    """Every status that reaches the host is public: no private one (bit 31) leaves a C entry."""
    native(JIT)
    seen: list[int] = []
    real = instance.Instance._error

    def spy(self: Any, status: int) -> BaseException:
        seen.append(status)
        return real(self, status)

    monkeypatch.setattr(instance.Instance, "_error", spy)
    yield seen
    assert all(0 < s < 1 << 31 for s in seen), seen


def func(i: Inst, name: str) -> JitFunc:
    return JitFunc(i.inst, next(x for n, k, x in i.mod["exports"] if n == name and k == 0))


def word(addr: int) -> ctypes.c_uint64:
    return ctypes.c_uint64.from_address(addr)


FOREIGN = (ctypes.c_uint64 * 16)()  # another compiler's context: its HOST_BRIDGE word is not ours


def fake(i: Inst, f: int, code: int) -> None:
    """Import `f`'s function record calls `code` (a hand-assembled C entry, which reads no
    context) on a context of another compiler, so the call takes the C path (D71)."""
    at = i.inst.records[f]  # type: ignore[attr-defined]
    assert FOREIGN[HOST_BRIDGE] != i.inst.ctx[HOST_BRIDGE]  # type: ignore[attr-defined]
    word(at).value, word(at + 16).value = code, ctypes.addressof(FOREIGN)


# ------------------------------------------------------------------ tail calls, constant stack
VIA = PING.replace(
    "(func $ping",
    '(func (export "via_call") (type $p) (call $ping (local.get 0)))\n  (func $ping',
)
TOP = """(module (type $p (func (param i32) (result i32))) (import "a" "ping" (func $p (type $p)))
  (func (export "go") (type $p) (call $p (local.get 0))))"""


@pytest.mark.parametrize("shape", ["host", "direct call", "record"])
def test_ping_pong_runs_in_constant_stack(shape: str) -> None:
    """A `return_call`s an import of B, which `return_call_indirect`s back: 10^6 round trips in
    a 256 KiB thread, A entered from the host (later hops only), from a direct call inside A (a
    restore frame first) and from a third instance through A's C entry."""

    def body() -> None:
        store = StoreState()
        b = build(wat(PONG), store=store)
        a = build(wat(VIA), imports={"b": provider(b)}, store=store)
        c = build(wat(TOP), imports={"a": provider(a)}, store=store)
        run = {"host": lambda n: a("ping", n), "direct call": lambda n: a("via_call", n),
               "record": lambda n: c("go", n)}[shape]  # fmt: skip
        assert run(2_000_000) == [111] and run(2_000_001) == [222]

    _small_thread(body)


def test_a_call_instead_of_a_tail_call_overflows() -> None:
    def body() -> None:
        store = StoreState()
        b = build(wat(PONG.replace("return_call_indirect", "call_indirect")), store=store)
        a = build(wat(PING), imports={"b": provider(b)}, store=store)
        assert trap_of(a, "ping", 2_000_000) is TrapCode.STACK_OVERFLOW

    _small_thread(body)


DEPTH = """(module (type $t (func (param i32) (result i32))) (global $n (mut i32) (i32.const 0))
  (table 1 funcref) (elem (i32.const 0) $f)
  (func $f (export "f") (type $t)
    (global.set $n (i32.add (global.get $n) (i32.const 1)))
    (i32.add (call_indirect (type $t) (local.get 0) (i32.const 0)) (i32.const 1)))
  (func (export "n") (result i32) (global.get $n)))"""


def test_recursion_through_the_own_table_needs_no_c_entry() -> None:
    """A record of the caller's own context is called at its body (S0015, a private choice):
    recursion through call_indirect costs a direct call's native stack, not the C path's 112
    bytes a level, so it reaches Wasmtime's depth (32 bytes a level there)."""
    x86()
    i = build(wat(DEPTH), store=StoreState(max_wasm_stack=128 << 10))
    assert trap_of(i, "f", 0) is TrapCode.STACK_OVERFLOW
    assert i("n")[0] * 32 >= 128 << 10


XA = """(module (type $t (func (param i32) (result i32))) (global $n (mut i32) (i32.const 0))
  (table (export "t") 1 funcref)
  (func (export "f") (type $t) (global.set $n (i32.add (global.get $n) (i32.const 1)))
    (i32.add (call_indirect (type $t) (local.get 0) (i32.const 0)) (i32.const 1)))
  (func (export "n") (result i32) (global.get $n)))"""
XB = """(module (type $t (func (param i32) (result i32))) (import "a" "f" (func $f (type $t)))
  (import "a" "t" (table 1 funcref)) (elem (table 0) (i32.const 0) func $g)
  (func $g (type $t) (i32.add (call $f (local.get 0)) (i32.const 1))))"""


def test_recursion_across_instances_needs_no_c_entry() -> None:
    """A's call_indirect to B, B's call of A's import, alternating: both callees are this
    compiler's (their HOST_BRIDGE word is ours), called at their bodies with a context switch
    (D71), at most 40 bytes a call (32 before S0015; the C path's 112)."""
    x86()
    store = StoreState(max_wasm_stack=128 << 10)
    a = build(wat(XA), store=store)
    build(wat(XB), imports={"a": provider(a)}, store=store)
    assert trap_of(a, "f", 0) is TrapCode.STACK_OVERFLOW
    assert 2 * a("n")[0] * 40 >= 128 << 10


# ------------------------------------------------------------------ statuses through C entries
BOTTOM = """(module (tag $e (export "e") (param i32))
  (import "x" "fail" (func $fail)) (import "x" "irq" (func $irq))
  (func (export "ok") (param i32) (result i32) (i32.add (local.get 0) (i32.const 1)))
  (func (export "trap") (param i32) (result i32) unreachable)
  (func (export "throw") (param i32) (result i32) (throw $e (local.get 0)))
  (func (export "host") (param i32) (result i32) (call $fail) (local.get 0))
  (func (export "intr") (param i32) (result i32) (call $irq) (loop $l (br $l)) (local.get 0)))"""
KINDS = ("ok", "trap", "throw", "host", "intr")


def through(src: str, prefix: str, out: str) -> str:
    """A module importing `prefix<k>` of `src` for each kind, exporting `<out><k>` that calls it
    and `catch_<out><k>` that catches B's tag around it (its payload + 1000)."""
    fs = []
    for k in KINDS:
        fs.append(f'(import "{src}" "{prefix}{k}" (func ${k} (param i32) (result i32)))')
    fs.append(f'(import "{src}" "e" (tag $e (param i32)))')
    for k in KINDS:
        fs.append(f'(func (export "{out}{k}") (param i32) (result i32) (call ${k} (local.get 0)))')
        fs.append(f"""(func (export "catch_{out}{k}") (param i32) (result i32)
          (i32.add (block $c (result i32) (try_table (result i32) (catch $e $c)
            (call ${k} (local.get 0)))) (i32.const 1000)))""")
    fs.append('(export "e" (tag $e))')
    return "(module " + "\n".join(fs) + ")"


@pytest.mark.parametrize("path", ["body", "C entry"])
def test_every_public_status_crosses_one_and_two_instances(path: str) -> None:
    """Through this compiler's bodies (D71) or, with `path` "C entry" (x86-64), through one and
    two C entries: each import's record calls a clobbering C entry of another compiler's context
    that jumps to the exporter's C entry."""
    store = StoreState()

    def fail(args: Sequence[Raw]) -> list[Raw]:
        raise ValueError("boom")

    def irq(args: Sequence[Raw]) -> list[Raw]:
        store.interrupt()
        return []

    b = build(wat(BOTTOM), imports={"x": {"fail": fail, "irq": irq}}, store=store)
    a = build(wat(through("b", "", "via_")), imports={"b": provider(b)}, store=store)
    c = build(wat(through("a", "via_", "via2_")), imports={"a": provider(a)}, store=store)
    if path == "C entry":
        x86()
        for k, kind in enumerate(KINDS):
            fake(a, k, clobbering(func(b, kind)))
            fake(c, k, clobbering(func(a, "via_" + kind)))
    for i, p in ((a, "via_"), (c, "via2_")):  # one C entry below A, two below C
        assert i(p + "ok", 5) == [6] and i("catch_" + p + "ok", 5) == [1006]
        assert trap_of(i, p + "trap", 5) is TrapCode.UNREACHABLE
        with pytest.raises(Trap) as e:
            i(p + "throw", 5)
        assert e.value.code is TrapCode.UNCAUGHT_EXCEPTION and e.value.exn[1] == (5,)  # type: ignore[attr-defined]
        assert i("catch_" + p + "throw", 5) == [1005]  # caught one and two instances up
        with pytest.raises(ValueError, match="boom"):
            i(p + "host", 5)
        assert trap_of(i, p + "intr", 5) is TrapCode.INTERRUPT
        assert i(p + "ok", 6) == [7]  # every instance still usable


# ------------------------------------------------------------------ the walk across modules
GB = """(module (type $s (struct (field i32))) (type $f (func (param i32) (result i32)))
  (table (export "t") 1 funcref)
  (func (export "g") (type $f) (local $r (ref null $s))
    (local.set $r (struct.new $s (i32.const 10)))
    (i32.add (call_indirect (type $f) (local.get 0) (i32.const 0))
      (struct.get $s 0 (local.get $r))))
  (func (export "g2") (type $f) (return_call_indirect (type $f) (local.get 0) (i32.const 0))))"""
GA = """(module (type $s (struct (field i32))) (type $f (func (param i32) (result i32)))
  (import "b" "g" (func $g (type $f))) (import "b" "g2" (func $g2 (type $f)))
  (import "b" "t" (table 1 funcref)) (import "env" "h" (func $h (param i32) (result i32)))
  (func (export "top") (type $f) (local $r (ref null $s))
    (local.set $r (struct.new $s (i32.add (local.get 0) (i32.const 100))))
    (table.set 0 (i32.const 0) (ref.func $back))
    (i32.add (call $g (local.get 0)) (struct.get $s 0 (local.get $r))))
  (func $back (type $f) (local $r (ref null $s))
    (local.set $r (struct.new $s (i32.const 1000)))
    (drop (struct.new $s (i32.const 0)))
    (i32.add (call $h (local.get 0)) (struct.get $s 0 (local.get $r))))
  (func (export "own") (type $f) (local $r (ref null $s))
    (local.set $r (struct.new $s (i32.add (local.get 0) (i32.const 200))))
    (table.set 0 (i32.const 0) (ref.func $back))
    (i32.add (call_indirect (type $f) (local.get 0) (i32.const 0))
      (struct.get $s 0 (local.get $r))))
  (func $hops (export "hops") (param i32) (result i32)
    (table.set 0 (i32.const 0) (ref.func $back2)) (return_call $g2 (local.get 0)))
  (func (export "hopsd") (param i32) (result i32)
    (i32.add (call $hops (local.get 0)) (i32.const 0)))
  (func $back2 (type $f) (local $r (ref null $s))
    (if (local.get 0) (then (return_call $g2 (i32.sub (local.get 0) (i32.const 1)))))
    (local.set $r (struct.new $s (i32.const 2000)))
    (drop (struct.new $s (i32.const 0)))
    (i32.add (call $h (i32.const 7)) (struct.get $s 0 (local.get $r))))
  (elem declare func $back $back2))"""


@pytest.mark.parametrize("path", ["body", "C entry"])
def test_the_walk_crosses_modules_and_hop_chains(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """host -> A -> (record) B -> (call_indirect) A -> host, then A's own-table call_indirect
    (the record path of our own context), a reference live in each frame, and a collection at
    the far end of a cross-instance tail chain, entered from the host and from a direct call:
    under the walk check, with every allocation collecting (moving), the old space poisoned and
    every collection verified. A's records of B's g and g2 call B's bodies (this compiler's
    context, D71) or, with `path` "C entry" (x86-64), a C entry of another compiler's context
    that clobbers what it may and jumps to B's C entry: the chains mix all three paths."""
    if path == "C entry":
        x86()
    for s in ("WALK_CHECK", "GC_STRESS", "GC_POISON", "GC_VERIFY"):
        monkeypatch.setenv(f"WABOROS_JIT_{s}", "1")
    store, seen = StoreState(), []

    def h(args: Sequence[Raw]) -> list[Raw]:
        segs = walk.walk(store, _stacks(store), store.instances[0].tramp)  # type: ignore[attr-defined]
        seen.append([[store.instances.index(f[0]) for f in fr] for fr, _ in segs])
        return [args[0]]

    b = build(wat(GB), store=store)
    a = build(wat(GA), imports={"b": provider(b), "env": {"h": h}}, store=store)
    c = path == "C entry"
    if c:
        fake(a, 0, clobbering(func(b, "g")))
        fake(a, 1, clobbering(func(b, "g2")))
    assert a("top", 5) == [5 + 1000 + 10 + 105]
    # A's thunk and $back, then B's g (and its C entry), then A's top; on x86-64 with top's C
    # entry (called from the host; S0015; aarch64: step 3)
    assert seen[-1] == [[1] * 2 + [0] * (1 + c) + [1] * (1 + X86)]
    assert a("own", 5) == [5 + 1000 + 205]  # A's own table: the body, no C entry between
    assert seen[-1] == [[1] * (3 + X86)]  # the thunk, $back, own (and its C entry)
    assert a("hops", 1001) == [7 + 2000]
    assert seen[-1] == [[1] * (2 + X86)]  # the thunk and $back2: the whole chain hopped away
    # from a direct call: tail_switch's restore frame, then hopsd; $back2 called through A's C
    # entry when the chain crosses the other compiler's C entries
    assert a("hopsd", 1001) == [7 + 2000]
    assert seen[-1] == [[1] * (2 + c) + [1] * (1 + X86)]
    assert _stacks(store).heap.collections > 0  # type: ignore[union-attr]


# ------------------------------------------------------------------ hand-assembled C entries
def asm() -> Any:
    from waboros.targets.jit.x86_64.encoder import Asm

    return Asm()


KEEP: list[ExecMem] = []


def blob(a: Any) -> int:
    KEEP.append(load(a.finish()))
    return KEEP[-1].addr


def x86() -> None:
    if not X86:
        pytest.skip("hand-assembled x86-64 C entries (aarch64: S0015 step 3)")


def returning(rax: int) -> int:
    """A C entry that returns `rax` whole: a status with the upper half set."""
    from waboros.targets.jit.x86_64.encoder import RAX

    a = asm()
    a.endbr64()
    a.mov_imm(RAX, rax, w=True)
    a.ret()
    return blob(a)


G = """(module (import "x" "g" (func $g))
  (func (export "f") (result i32) (call $g) (i32.const 7)))"""


@pytest.mark.parametrize(("rax", "status"), [(0xDEADBEEF00000000, 0), (0xFFFFFFFF00000001, 1)])
def test_a_status_with_an_upper_half_is_its_32_bits(rax: int, status: int) -> None:
    x86()
    g = build(wat(G), imports={"x": {"g": lambda args: []}})
    code = returning(rax)
    fake(g, 0, code)
    if status:
        assert trap_of(g, "f") is TrapCode.UNREACHABLE
    else:
        assert g("f") == [7]
    inst: Any = g.inst
    inst.ctx[TRAP_CODE] = 0
    frame = _stacks(inst.store).vstack.addr
    assert inst.entry(ctypes.addressof(inst.ctx), frame, code) == status  # the runtime's entry
    assert inst.ctx[TRAP_CODE] == status


def clobbering(target: JitFunc | None = None) -> int:
    """A C entry that clobbers every register the boundary lets it (rax rcx rdx rsi rdi r8-r11,
    xmm0-xmm15, the status flags), then jumps to `target`'s C entry on its context (a C sibling
    call: the frame in rsi kept) or returns 0. DF stays 0, MXCSR as it was."""
    from waboros.targets.jit.x86_64.encoder import R8, R9, R10, R11, RAX, RCX, RDI, RDX, RSI

    a = asm()
    a.endbr64()
    a.mov(RAX, RSI, w=True)
    for k, r in enumerate((RCX, RDX, RSI, RDI, R8, R9, R10, R11)):
        a.mov_imm(r, 0x5A5A5A5A00000000 + k, w=True)
    for x in range(16):
        a.movq_to_xmm(x, RCX, True)
    a.alu("cmp", RCX, RDX, w=True)
    a.mov(RSI, RAX, w=True)
    if target is None:
        a.mov_imm(RAX, 0x5A5A5A5A00000000, w=True)  # status 0, the upper half set
        a.ret()
        return blob(a)
    code, _, ctx = target.record()
    a.mov_imm(RDI, ctx, w=True)
    a.mov_imm(RAX, code, w=True)
    a.jmp(RAX)
    return blob(a)


def probe() -> int:
    """A C entry with results (i64 rflags, i64 mxcsr) in its frame."""
    from waboros.targets.jit.x86_64.encoder import RAX, RSI, Mem

    a = asm()
    a.endbr64()
    a.buf += b"\x9c\x58"  # pushfq; pop rax
    a.mov(Mem(RSI), RAX, w=True)
    a.mov_imm(Mem(RSI, 8), 0, w=True)
    a.buf += b"\x0f\xae\x5e\x08"  # stmxcsr [rsi + 8]
    a.alu("xor", RAX, RAX)
    a.ret()
    return blob(a)


LOCS = [0x1001, 0x2003, 0x3005, 0x4007, 0x5009, 0x600B, 0x700D, 0x800F]
P = f"""(module
  (import "x" "f_ret" (func $f_ret (param i32) (result i32)))
  (import "x" "f_trap" (func $f_trap (param i32) (result i32)))
  (import "x" "f_throw" (func $f_throw (param i32) (result i32)))
  (import "x" "probe" (func $probe (result i64 i64)))
  (import "x" "plain" (func $plain (param i32) (result i32)))
  (import "b" "e" (tag $e (param i32)))
  (func $tc (param i32) (result i32) (return_call $f_ret (local.get 0)))  ;; a first hop
  (func $inner (param $w i32) (result i32) (local $p i32) (local $q i32) (local $r i32)
    (local.set $p (i32.const 11)) (local.set $q (i32.const 13)) (local.set $r (i32.const 17))
    (i32.add
      (if (result i32) (i32.eqz (local.get $w)) (then (call $f_ret (i32.const 7)))
        (else (if (result i32) (i32.eq (local.get $w) (i32.const 1))
          (then (call $f_trap (i32.const 7)))
          (else (if (result i32) (i32.eq (local.get $w) (i32.const 2))
            (then (call $f_throw (i32.const 7))) (else (call $tc (i32.const 7))))))))
      (i32.add (i32.mul (local.get $p) (local.get $q)) (local.get $r))))
  (type $ti (func (param i32) (result i32))) (table 1 funcref) (elem (i32.const 0) $inner)
  {
    "".join(
        f'''(func (export "{name}") (param $w i32) (result i32)
    {" ".join(f"(local $l{k} i32)" for k in range(8))}
    {" ".join(f"(local.set $l{k} (i32.const {v}))" for k, v in enumerate(LOCS))}
    (i32.add (block $c (result i32) (try_table (result i32) (catch $e $c)
      ({call} (local.get $w){tail})))
      {"".join(f"(i32.add (i32.mul (local.get $l{k}) (i32.const {k + 2})) " for k in range(8))}
      (i32.const 0){")" * 8}))
  '''
        for name, call, tail in (
            ("outer", "call $inner", ""),
            ("outer_ind", "call_indirect (type $ti)", " (i32.const 0)"),
        )
    )
}
  (func (export "after") (result i64 i64) (drop (call $plain (i32.const 1))) (call $probe)))"""
SUM = sum(v * (k + 2) for k, v in enumerate(LOCS))


def test_a_clobbering_c_entry_keeps_the_pins_and_the_machine_state() -> None:
    """Every caller-saved register of the table clobbered behind a record call: the caller's pins
    and those of the frame above survive a result, a trap, status 14 into a try_table (A3) and
    a first tail-call hop (tail_switch);
    after the call DF is 0 and MXCSR's control bits are 0x1F80."""
    x86()
    store = StoreState()
    b = build(wat(BOTTOM), imports={"x": {"fail": lambda a: [], "irq": lambda a: []}}, store=store)
    dummy = {k: (lambda args: [0]) for k in ("f_ret", "f_trap", "f_throw", "plain")}
    p = build(wat(P), imports={"x": {**dummy, "probe": lambda args: [0, 0]}, "b": provider(b)},
              store=store)  # fmt: skip
    for f, target in enumerate((func(b, "ok"), func(b, "trap"), func(b, "throw"))):
        fake(p, f, clobbering(target))
    fake(p, 3, probe())
    fake(p, 4, clobbering())
    inner = 11 * 13 + 17
    for outer in ("outer", "outer_ind"):  # $inner called directly, then on the record path of
        # our own context (no C entry: the internal convention keeps the pins)
        assert p(outer, 0) == [(8 + inner + SUM) & M32]
        assert trap_of(p, outer, 1) is TrapCode.UNREACHABLE
        assert p(outer, 2) == [(7 + SUM) & M32]  # caught: the payload, then the pins
        assert p(outer, 0) == [(8 + inner + SUM) & M32]
        assert p(outer, 3) == [(8 + inner + SUM) & M32]  # through tail_switch's restore frame
    flags, mxcsr = p("after")
    assert flags & 0x400 == 0 and mxcsr & 0xFFC0 == 0x1F80, (hex(flags), hex(mxcsr))


def test_the_cross_context_body_call_keeps_the_pins() -> None:
    """The same calls into B's own functions: this compiler's context, so B's bodies are called
    with a context switch (D71): the pins survive a result, a trap, status 14 into a try_table
    and a first tail-call hop (tail_switch's same-compiler path)."""
    store = StoreState()
    b = build(wat(BOTTOM), imports={"x": {"fail": lambda a: [], "irq": lambda a: []}}, store=store)
    src = P
    for k, x in (("f_ret", "ok"), ("f_trap", "trap"), ("f_throw", "throw")):
        src = src.replace(f'(import "x" "{k}"', f'(import "b" "{x}"')
    dummy = {"plain": lambda args: [0], "probe": lambda args: [0, 0]}
    p = build(wat(src), imports={"x": dummy, "b": provider(b)}, store=store)
    inner = 11 * 13 + 17
    for outer in ("outer", "outer_ind"):
        assert p(outer, 0) == [(8 + inner + SUM) & M32]
        assert trap_of(p, outer, 1) is TrapCode.UNREACHABLE
        assert p(outer, 2) == [(7 + SUM) & M32]
        assert p(outer, 3) == [(8 + inner + SUM) & M32]


def harness(win64: bool) -> int:
    """`int64_t h(uint64_t *p)` in the host's convention: sets every register the host expects
    kept (SysV rbx rbp r12-r15; Win64 also rsi rdi xmm6-xmm15) to a known value, calls
    p[0] = entry(p[1] = ctx, p[2] = frame, p[3] = C entry), writes those registers to p[4..12),
    the low halves of xmm6-xmm15 to p[12..22) and the status to p[22], puts the host's own
    values back and returns."""
    from waboros.targets.jit.x86_64 import simd
    from waboros.targets.jit.x86_64.encoder import (
        R8,
        R12,
        R13,
        R14,
        R15,
        RAX,
        RBP,
        RBX,
        RCX,
        RDI,
        RDX,
        RSI,
        RSP,
        Mem,
    )  # fmt: skip

    a = asm()
    kept = [RBX, RBP, R12, R13, R14, R15] + ([RSI, RDI] if win64 else [])
    xmm = range(6, 16) if win64 else range(0)
    a.endbr64()
    for r in kept:
        a.push(r)
    a.alu_imm("sub", RSP, 8 + 16 * len(xmm), w=True)  # 0 mod 16 after the pushes and this
    for k, x in enumerate(xmm):
        simd.movups(a, x, Mem(RSP, 8 + 16 * k), True)
    a.mov(RAX, RCX if win64 else RDI, w=True)
    a.mov(Mem(RSP), RAX, w=True)  # p
    for k, r in enumerate(kept):
        a.mov_imm(r, 0x1111111111111111 * (k + 1), w=True)
    for k, x in enumerate(xmm):
        a.mov_imm(RCX, 0x0101010101010101 * (k + 0x20), w=True)
        a.movq_to_xmm(x, RCX, True)
    args = (RCX, RDX, R8) if win64 else (RDI, RSI, RDX)
    for k, r in enumerate(args):
        a.mov(r, Mem(RAX, 8 * (k + 1)), w=True)
    if win64:
        a.alu_imm("sub", RSP, 32, w=True)  # the shadow space
    a.call(Mem(RAX))
    if win64:
        a.alu_imm("add", RSP, 32, w=True)
    a.mov(RCX, Mem(RSP), w=True)
    a.mov(Mem(RCX, 8 * 22), RAX, w=True)
    for k, r in enumerate(kept):
        a.mov(Mem(RCX, 8 * (4 + k)), r, w=True)
    for k, x in enumerate(xmm):
        a.movq_from_xmm(RAX, x, True)
        a.mov(Mem(RCX, 8 * (12 + k)), RAX, w=True)
    for k, x in enumerate(xmm):
        simd.movups(a, x, Mem(RSP, 8 + 16 * k), False)
    a.alu_imm("add", RSP, 8 + 16 * len(xmm), w=True)
    for r in reversed(kept):
        a.pop(r)
    a.ret()
    return blob(a)


def test_the_runtime_entry_keeps_the_hosts_registers() -> None:
    """Behind the runtime's entry a C entry clobbers what SysV lets it (rsi, rdi, xmm6-xmm15
    included): the host's callee-saved registers come back (the Win64 ones on Windows)."""
    x86()
    win64 = sys.platform == "win32"
    inst: Any = build(wat("(module)")).inst
    p = (ctypes.c_uint64 * 23)()
    p[0], p[1] = inst.tramp.entry, ctypes.addressof(inst.ctx)
    p[2], p[3] = _stacks(inst.store).vstack.addr, clobbering()
    h = ctypes.CFUNCTYPE(ctypes.c_int64, ctypes.c_void_p)(harness(win64))
    h(ctypes.addressof(p))
    n = 8 if win64 else 6
    assert p[22] & M32 == 0
    assert list(p[4 : 4 + n]) == [0x1111111111111111 * (k + 1) for k in range(n)]
    if win64:
        assert list(p[12:22]) == [0x0101010101010101 * (k + 0x20) for k in range(10)]
