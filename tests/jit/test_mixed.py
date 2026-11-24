# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportPrivateUsage=false, reportUnknownLambdaType=false, reportArgumentType=false, reportAttributeAccessIssue=false
"""Mixed stores (docs/specs/S0016, "Test plan", "Mixed stores"): instances of the machine's JIT and
of a development compiler of the same architecture (`jit-sljit-<arch>`) in one store, both
directions. Calls of every value type through imports, `call_indirect` and `call_ref`; D71's paths
seen in the walk (each compiler calls its own records at their bodies and the other's at their C
entries); tail ping-pong in S0015's three entry shapes; every public status through one and two C
entries, INTERRUPT from a thread included; the walk and the collector, whose compiler the order of
the modules decides. Through files with `WABOROS_JIT_ARTIFACT=1`. Skipped without `native/`'s
library. A forced equal HOST_BRIDGE word: tests/sljit/test_store.py."""

import ctypes
import threading
import time
from collections.abc import Iterator, Sequence
from typing import Any

import pytest

from tests.jit.test_boundary import BOTTOM, GA, GB, KINDS, through
from tests.jit.test_compiler import JIT, build, trap_of, wat
from tests.jit.test_tables import call, provider
from tests.jit.test_tail import PING, PONG, _small_thread
from waboros.runtime.store import StoreState
from waboros.targets.base import TARGETS, Raw, Trap, TrapCode
from waboros.targets.jit.common import instance, walk
from waboros.targets.jit.common.context import HOST_BRIDGE
from waboros.targets.jit.common.instance import _stacks

M32 = 0xFFFFFFFF
# the machine's JIT and each development compiler of its architecture (aarch64: step 5)
PAIRS = [
    (JIT, n)
    for n, t in TARGETS.items()
    if getattr(t(), "development", False) and getattr(t(), "arch", "") == TARGETS[JIT]().arch  # type: ignore[attr-defined]
]
if not PAIRS:
    pytest.skip("no development compiler of this architecture (no native/ build)",
                allow_module_level=True)  # fmt: skip
WAYS = [p for x, d in PAIRS for p in ((x, d), (d, x))]
IDS = [f"{a[4:]}>{b[4:]}" for a, b in WAYS]


def helpers(target: str) -> bool:
    """`jit-sljit` calls another context's record through a module helper with its own frame."""
    return target.startswith("jit-sljit")


@pytest.fixture(autouse=True)
def public_statuses(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every status that reaches the host is public: no private one (bit 31) leaves a C entry."""
    seen: list[int] = []
    real = instance.Instance._error

    def spy(self: Any, status: int) -> BaseException:
        seen.append(status)
        return real(self, status)

    monkeypatch.setattr(instance.Instance, "_error", spy)
    yield
    assert all(0 < s < 1 << 31 for s in seen), seen


def frames(store: StoreState, names: dict[Any, str]) -> tuple[list[tuple[str, str]], list[str]]:
    """The walk from a host call: per frame, innermost first, (instance name, kind), kind being
    `body` below the module's centry, else `centry` and then (`jit-sljit`) `xcall`, `ccall` in
    emission order; and the compilers of the restore frames (tail_switch) the walk crossed."""
    pcs: list[tuple[Any, int]] = []
    hits: list[str] = []
    real, st = walk._site, _stacks(store)
    restore = {TARGETS[n]().trampolines().restore: n for n in TARGETS if n in st.bridges.values()}

    def site(s: Any, stacks: Any, pc: int) -> Any:
        r = real(s, stacks, pc)
        pcs.append((r[0], pc - r[0].code.addr))
        return r

    class Restores(dict[int, tuple[int, int]]):
        def get(self, pc: int, default: Any = None) -> Any:  # type: ignore[override]
            if pc in self:
                hits.append(restore[pc])
            return super().get(pc, default)

    old = st.restores
    walk._site, st.restores = site, Restores(old)
    try:
        walk.walk(store, st)
    finally:
        walk._site, st.restores = real, old
    out = []
    for inst, off in pcs:
        code = ctypes.string_at(inst.code.addr, inst.code.size)
        cen = TARGETS[inst.owner]().centry(code, min(o for o, _ in inst.funcs))
        after = sorted(o for o in inst.walk_sites if o >= cen)
        kind = "body" if off < cen else ("centry", "xcall", "ccall")[after.index(off)]
        out.append((names[inst], kind))
    return out, hits


# ------------------------------------------------------------------ calls, every value type
ALL = """(type $all (func (param i32 i64 f32 f64 v128 externref funcref)
  (result funcref externref v128 f64 f32 i64 i32))) (type $s (struct (field i32)))"""
CB = f"""(module {ALL}
  (table (export "t") 1 (ref null $all))
  (elem (table 0) (i32.const 0) (ref null $all) (ref.func $id))
  (func $id (export "id") (type $all) (local.get 6) (local.get 5) (local.get 4) (local.get 3)
    (local.get 2) (local.get 1) (local.get 0))
  (func (export "sid") (param (ref null $s)) (result (ref null $s))
    (drop (struct.new $s (i32.const 0))) (local.get 0))
  (func (export "iid") (param i31ref) (result i31ref) (local.get 0))
  (func (export "fr") (result funcref) (ref.func $id)))"""
ARGS = " ".join(f"(local.get {k})" for k in range(7))
CA = f"""(module {ALL}
  (import "b" "id" (func $id (type $all))) (import "b" "t" (table $t 1 (ref null $all)))
  (import "b" "sid" (func $sid (param (ref null $s)) (result (ref null $s))))
  (import "b" "iid" (func $iid (param i31ref) (result i31ref)))
  (func (export "imp") (type $all) (call $id {ARGS}))
  (func (export "ind") (type $all) (call_indirect $t (type $all) {ARGS} (i32.const 0)))
  (func (export "ref") (type $all) (call_ref $all {ARGS} (table.get $t (i32.const 0))))
  (func (export "gc") (param i32) (result i32)
    (i32.add (struct.get $s 0 (call $sid (struct.new $s (local.get 0))))
      (i31.get_s (call $iid (ref.i31 (i32.mul (local.get 0) (i32.const 2))))))))"""


@pytest.mark.parametrize(("ta", "tb"), WAYS, ids=IDS)
def test_calls_carry_every_value_type(ta: str, tb: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A calls B of the other compiler through an import, B's table and `call_ref` of B's table
    entry: NaN payloads, upper halves and v128 arrive as they were sent, references as themselves;
    a struct and an i31 made in A come back from B, the struct moved by a collection in B."""
    monkeypatch.setenv("WABOROS_JIT_GC_STRESS", "1")
    store = StoreState()
    b = build(wat(CB), tb, store=store)
    a = build(wat(CA), ta, {"b": provider(b)}, store)
    fr, ext = call(b, "fr")[0], object()
    args = [0x80000001, 0x8000000000000003, 0x7FA00001, 0x7FF4000000000001,
            0x0123456789ABCDEFFEDCBA9876543210, ext, fr]  # fmt: skip
    for how in ("imp", "ind", "ref"):
        got = call(a, how, *args)
        assert got == args[::-1] and got[0] is fr and got[1] is ext, how
        assert call(a, how, 0, 0, 0, 0, 0, None, None)[:2] == [None, None]
    assert a("gc", 21) == [21 + 42]
    assert _stacks(store).heap.collections > 0  # type: ignore[union-attr]


# ------------------------------------------------------------------ D71's paths
PB = """(module (import "env" "h" (func $h (param i32) (result i32)))
  (func (export "g") (param i32) (result i32) (call $h (local.get 0))))"""
PA = """(module (type $t (func (param i32) (result i32)))
  (import "x" "g" (func $gx (type $t))) (import "s" "g" (func $gs (type $t)))
  (import "env" "h" (func $h (type $t))) (table 1 funcref) (elem (i32.const 0) $own)
  (func $own (type $t) (call $h (local.get 0)))
  (func (export "x") (type $t) (call $gx (local.get 0)))
  (func (export "s") (type $t) (call $gs (local.get 0)))
  (func (export "own") (type $t) (call_indirect (type $t) (local.get 0) (i32.const 0))))"""


@pytest.mark.parametrize(("tx", "ts"), PAIRS, ids=[f"{x[4:]}+{s[4:]}" for x, s in PAIRS])
def test_each_compiler_calls_its_own_records_at_the_body(tx: str, ts: str) -> None:
    """D71 in a store of both compilers: a record of this compiler's (its HOST_BRIDGE word is
    ours) is called at its body, through `jit-sljit`'s xcall for another context; another
    compiler's at its C entry (the callee's centry frame), through `jit-sljit`'s ccall; the
    caller's own table at the body, no helper. The walk from B's host import shows which."""
    store, seen = StoreState(), []
    names: dict[Any, str] = {}

    def h(args: Sequence[Raw]) -> list[Raw]:
        seen.append(frames(store, names))
        return [args[0]]

    bs = {t: build(wat(PB), t, {"env": {"h": h}}, store) for t in (tx, ts)}
    imp = {"x": provider(bs[tx]), "s": provider(bs[ts]), "env": {"h": h}}
    a_s = {t: build(wat(PA), t, imp, store) for t in (tx, ts)}
    for t in (tx, ts):
        names[bs[t].inst], names[a_s[t].inst] = "B" + t, "A" + t
    word = {t: a_s[t].inst.ctx[HOST_BRIDGE] for t in (tx, ts)}
    assert word[tx] != word[ts] and bs[tx].inst.ctx[HOST_BRIDGE] == word[tx]
    for ta in (tx, ts):
        a, top = a_s[ta], [("A" + ta, "body"), ("A" + ta, "centry")]
        for exp, tb in (("x", tx), ("s", ts)):
            assert a(exp, 5) == [5]
            cen = [] if tb == ta else [("B" + tb, "centry")]
            helper = [("A" + ta, "xcall" if tb == ta else "ccall")] if helpers(ta) else []
            assert seen[-1] == ([("B" + tb, "body")] * 2 + cen + helper + top, []), (ta, tb)
        assert a("own", 6) == [6]
        assert seen[-1] == ([("A" + ta, "body")] * 2 + top, [])


# ------------------------------------------------------------------ tail ping-pong
HPONG = PONG.replace(  # PONG calling the host at its end
    "(table $t", '(import "env" "h" (func $h (param i32) (result i32)))\n  (table $t'
).replace("(then (i32.const 222))", "(then (call $h (i32.const 222)))")
VIA = PING.replace(
    "(func $ping", '(func (export "via_call") (type $p) (call $ping (local.get 0)))\n  (func $ping'
)
TOP = """(module (type $p (func (param i32) (result i32))) (import "a" "ping" (func $p (type $p)))
  (func (export "go") (type $p) (call $p (local.get 0))))"""


@pytest.mark.parametrize("shape", ["host", "direct call", "record"])
@pytest.mark.parametrize(("ta", "tb"), WAYS, ids=IDS)
def test_ping_pong_across_compilers_runs_in_constant_stack(ta: str, tb: str, shape: str) -> None:
    """A (`ta`) `return_call`s B's import, B (`tb`) `return_call_indirect`s A back: 10^6 round
    trips in a 256 KiB thread, A entered from the host (each centry returns-then-jumps into the
    other's C entry), from a direct call (A's tail_switch hops into B's C entry first) and from C
    (`tb`) through A's C entry. The walk at the end (B's host import) shows the shape."""
    out: list[Any] = []

    def body() -> None:
        store = StoreState()
        names: dict[Any, str] = {}

        def h(args: Sequence[Raw]) -> list[Raw]:
            out.append(frames(store, names))
            return [args[0]]

        b = build(wat(HPONG), tb, {"env": {"h": h}}, store)
        a = build(wat(VIA), ta, {"b": provider(b)}, store)
        c = build(wat(TOP), tb, {"a": provider(a)}, store)
        names.update({b.inst: "B", a.inst: "A", c.inst: "C"})
        run = {"host": lambda n: a("ping", n), "direct call": lambda n: a("via_call", n),
               "record": lambda n: c("go", n)}[shape]  # fmt: skip
        assert run(2_000_000) == [111] and run(2_000_001) == [222]

    _small_thread(body)
    end = [("B", "body"), ("B", "body"), ("B", "centry")]
    c = [("C", "ccall")] * helpers(tb) + [("C", "body"), ("C", "centry")]
    below = {"host": ([], []), "direct call": ([("A", "body"), ("A", "centry")], [ta]),
             "record": (c, [])}  # fmt: skip
    assert out == [(end + below[shape][0], below[shape][1])]


@pytest.mark.parametrize(("ta", "tb"), WAYS, ids=IDS)
def test_a_call_instead_of_a_tail_call_overflows(ta: str, tb: str) -> None:
    def body() -> None:
        store = StoreState()
        b = build(wat(PONG.replace("return_call_indirect", "call_indirect")), tb, store=store)
        a = build(wat(PING), ta, {"b": provider(b)}, store)
        assert trap_of(a, "ping", 2_000_000) is TrapCode.STACK_OVERFLOW

    _small_thread(body)


# ------------------------------------------------------------------ statuses
@pytest.mark.parametrize(("ta", "tb"), WAYS, ids=IDS)
def test_every_public_status_crosses_one_and_two_c_entries(ta: str, tb: str) -> None:
    """B (`tb`) below A (`ta`) below C (`tb`): every call crosses compilers, so one C entry below
    A and two below C. A trap, status 14 uncaught to the host and caught one and two instances up,
    a host error, INTERRUPT; every instance usable after."""
    store = StoreState()

    def fail(args: Sequence[Raw]) -> list[Raw]:
        raise ValueError("boom")

    def irq(args: Sequence[Raw]) -> list[Raw]:
        store.interrupt()
        return []

    b = build(wat(BOTTOM), tb, {"x": {"fail": fail, "irq": irq}}, store)
    a = build(wat(through("b", "", "via_")), ta, {"b": provider(b)}, store)
    c = build(wat(through("a", "via_", "via2_")), tb, {"a": provider(a)}, store)
    assert len(KINDS) == 5
    for i, p in ((a, "via_"), (c, "via2_")):
        assert i(p + "ok", 5) == [6] and i("catch_" + p + "ok", 5) == [1006]
        assert trap_of(i, p + "trap", 5) is TrapCode.UNREACHABLE
        with pytest.raises(Trap) as e:
            i(p + "throw", 5)
        assert e.value.code is TrapCode.UNCAUGHT_EXCEPTION and e.value.exn[1] == (5,)  # type: ignore[attr-defined]
        assert i("catch_" + p + "throw", 5) == [1005]
        with pytest.raises(ValueError, match="boom"):
            i(p + "host", 5)
        assert trap_of(i, p + "intr", 5) is TrapCode.INTERRUPT
        assert i(p + "ok", 6) == [7]


SPIN = """(module (import "x" "entered" (func $e))
  (func (export "spin") (param i32) (result i32) (call $e)
    (loop $l (br_if $l (local.tee 0 (i32.sub (local.get 0) (i32.const 1))))) (i32.const 5)))"""
VIA1 = """(module (import "b" "spin" (func $s (param i32) (result i32)))
  (func (export "spin") (param i32) (result i32) (call $s (local.get 0))))"""


@pytest.mark.parametrize("depth", [1, 2])
@pytest.mark.parametrize(("ta", "tb"), WAYS, ids=IDS)
def test_interrupt_from_a_thread_while_the_callee_loops(ta: str, tb: str, depth: int) -> None:
    """Another thread interrupts the store 20 ms after B (`tb`) starts its loop, below one or two
    C entries. Bounded: a lost interrupt lets the loop end after 2^32 - 1 rounds (seconds) with
    a result, and the test fails instead of hanging."""
    store, entered = StoreState(), threading.Event()
    b = build(wat(SPIN), tb, {"x": {"entered": lambda args: entered.set() or []}}, store)
    top = build(wat(VIA1), ta, {"b": provider(b)}, store)
    if depth == 2:
        top = build(wat(VIA1), tb, {"b": provider(top)}, store)

    def stop() -> None:
        if entered.wait(10):
            time.sleep(0.02)
            store.interrupt()

    t = threading.Thread(target=stop)
    t.start()
    start = time.monotonic()
    try:
        assert trap_of(top, "spin", M32) is TrapCode.INTERRUPT
    finally:
        t.join(10)
    assert time.monotonic() - start >= 0.02  # it was looping when the interrupt came
    assert not t.is_alive() and top("spin", 1) == [5]


# ------------------------------------------------------------------ the walk and the collector
SUB = "(type $o (sub (func))) (func $sub (call_indirect (type $o) (i32.const 0)))"


@pytest.mark.parametrize("trigger", ["subtype site", "first collection"])
@pytest.mark.parametrize(("ta", "tb"), WAYS, ids=IDS)
def test_the_walk_and_the_collector_in_a_mixed_store(
    ta: str, tb: str, trigger: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """host -> A -> B -> A -> host with a reference live in each frame, A's own table, and a
    collection at the end of a tail chain across compilers entered from the host and from a direct
    call; every allocation collects (moving) under the walk check, poison and verify. B (`tb`)
    needs the heap first, but the collector is A's (`ta`): compiled at A's instantiation for its
    function-subtype site, or at the first collection, which A's frames run. Each collection
    after it is started from either compiler's frames."""
    for s in ("WALK_CHECK", "GC_STRESS", "GC_POISON", "GC_VERIFY"):
        monkeypatch.setenv(f"WABOROS_JIT_{s}", "1")
    store, seen = StoreState(), []
    names: dict[Any, str] = {}

    def h(args: Sequence[Raw]) -> list[Raw]:
        seen.append(frames(store, names))
        return [args[0]]

    b = build(wat(GB), tb, store=store)
    src = GA.replace("(elem declare", SUB + " (elem declare") if trigger == "subtype site" else GA
    a = build(wat(src), ta, {"b": provider(b), "env": {"h": h}}, store)
    names.update({a.inst: "A", b.inst: "B"})
    heap: Any = _stacks(store).heap

    def owner() -> str | None:  # the target that compiled the store's collector
        return heap.collector[0].owner if heap.collector else None

    assert owner() == (ta if trigger == "subtype site" else None) and heap.collections == 0
    # A only: the first allocation of a heap does not collect, so A's own table call (two
    # allocations in A) runs the first collection (LESSONS)
    a_body, a_c = [("A", "body")], [("A", "centry")]
    assert a("own", 5) == [5 + 1000 + 205]
    assert seen[-1] == (a_body * 3 + a_c, [])
    assert owner() == ta and heap.collections > 0
    assert a("top", 5) == [5 + 1000 + 10 + 105]  # B's g collects in B's frames
    cross = {"A": [("A", "ccall")] * helpers(ta), "B": [("B", "ccall")] * helpers(tb)}
    assert seen[-1] == (a_body * 2 + a_c + cross["B"] + [("B", "body"), ("B", "centry")]
                        + cross["A"] + a_body + a_c, [])  # fmt: skip
    assert a("hops", 1001) == [7 + 2000]
    assert seen[-1] == (a_body * 2 + a_c, [])  # the chain hopped away from the host's entry
    assert a("hopsd", 1001) == [7 + 2000]
    assert seen[-1] == (a_body * 2 + a_c + a_body + a_c, [ta])  # A's tail_switch's restore
    assert owner() == ta
