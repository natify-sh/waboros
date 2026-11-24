# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
"""The frame walk and its check mode (S0013 step 3a.3, jit/common/walk.py), on the machine's own
JIT: every test runs with WABOROS_JIT_WALK_CHECK=1, so each host call and libcall is checked, and
host functions also count the frames of each segment themselves."""

import ctypes
import dataclasses
from collections.abc import Callable, Sequence
from typing import Any

import pytest

from tests.jit import native, native_jit
from tests.jit.test_compiler import Inst, wat
from tests.jit.test_stackmaps import form_3a
from waboros.frontend.binary import parse
from waboros.frontend.types import EXTERN
from waboros.frontend.validate import validate
from waboros.runtime import link
from waboros.runtime.store import StoreState
from waboros.targets import base
from waboros.targets.base import Raw
from waboros.targets.jit.common import gc, walk
from waboros.targets.jit.common.compiler import Compiled
from waboros.targets.jit.common.instance import _stacks  # pyright: ignore[reportPrivateUsage]

JIT = native_jit()
# Frame counts include one for each C entry a call went through: from the host, not into a body
# of this compiler (S0015, D71).


@pytest.fixture(autouse=True)
def checks(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    native(JIT)
    monkeypatch.setenv("WABOROS_JIT_WALK_CHECK", "1")
    n, real = [0], walk.check

    def counted(*a: Any) -> None:
        n[0] += 1
        real(*a)

    monkeypatch.setattr(walk, "check", counted)
    return n


def make(text: str, store: StoreState, providers: Any = None) -> Inst:
    mod = parse(wat(text))
    validate(mod)
    t = base.TARGETS[JIT]()
    return Inst(mod, t.instantiate(t.compile(mod), link.resolve(mod, providers or {}), store))


def shape(store: StoreState) -> list[list[int]]:
    """Frames per segment (innermost first) and the instances' order, as ids."""
    st = _stacks(store)
    segs = walk.walk(store, st)
    return [[store.instances.index(f[0]) for f in frames] for frames, _ in segs]


H = '(import "env" "h" (func $h (param i32) (result i32)))'


def recheck(store: StoreState, slots: int) -> None:
    """walk.check from inside a host function: without the frame this call's checked callback
    already gave its own nested entries."""
    st = _stacks(store)
    mine = st.walk_vsp.pop()
    try:
        walk.check(store, st, slots)
    finally:
        st.walk_vsp.append(mine)


def recorder(store: StoreState, seen: list[Any]) -> Any:
    def host(args: Sequence[Raw]) -> list[Raw]:
        seen.append(shape(store))
        return [args[0]]

    return {"env": {"h": host}}


def test_a_chain_of_direct_calls(checks: list[int]) -> None:
    store, seen = StoreState(), []
    fn = "(func $f{} (param i32) (result i32) (call $f{} (local.get 0)))"
    calls = "".join(fn.format(i, i + 1) for i in range(4))
    jit = make(f"""(module {H} {calls} (func $f4 (param i32) (result i32) (call $h (local.get 0)))
      (export "f" (func $f0)))""", store, recorder(store, seen))  # fmt: skip
    # the thunk (a record of our own context: no C entry), 5 frames, f0's C entry
    assert jit("f", 7) == [7] and seen == [[[0] * 7]] and checks[0] == 1


def test_nested_entries_50_deep(checks: list[int]) -> None:
    store, depth = StoreState(), []

    def host(args: Sequence[Raw]) -> list[Raw]:
        if args[0]:
            return jit("run", args[0] - 1)
        depth.append(shape(store))
        return [0]

    jit = make(f"""(module {H} (func (export "run") (param i32) (result i32)
      (call $h (local.get 0))))""", store, {"env": {"h": host}})  # fmt: skip
    assert jit("run", 49) == [0] and depth == [[[0] * 3] * 50] and checks[0] == 50


def test_tail_calls_and_a_tail_switch_restore_frame(checks: list[int]) -> None:
    store, seen = StoreState(), []
    b = make(f"""(module {H} (func $g (param i32) (result i32) (call $h (local.get 0)))
      (func (export "callee") (param i32) (result i32) (return_call $g (local.get 0))))""",
             store, recorder(store, seen))  # fmt: skip
    a = make("""(module (import "b" "callee" (func $c (param i32) (result i32)))
      (func $t (param i32) (result i32) (return_call $c (local.get 0)))
      (func (export "tc") (param i32) (result i32) (return_call $t (local.get 0)))
      (func (export "viaCall") (param i32) (result i32) (call $t (local.get 0))))""",
             store, {"b": link.Provider(b.mod, b.inst)})  # fmt: skip
    # B's thunk and $g (and B's C entry); A's frames are gone: entered through its C entry, $t
    # hops (S0015)
    assert a("tc", 3) == [3] and seen == [[[0] * 3]]
    # $t entered by a call: a restore frame between B's frames and A's caller of $t (B's body
    # called by tail_switch: this compiler's context, no C entry; D71)
    assert a("viaCall", 4) == [4] and seen[1] == [[0] * 2 + [1] * 2]
    assert checks[0] == 2


def test_frames_of_another_instance_indirect_and_ref_calls(checks: list[int]) -> None:
    store, seen = StoreState(), []
    b = make(f"""(module {H} (table 1 funcref) (elem (i32.const 0) $k)
      (func $k (param i32) (result i32) (call $h (local.get 0)))
      (func (export "g") (param i32) (result i32)
        (call_indirect (param i32) (result i32) (local.get 0) (i32.const 0))))""",
             store, recorder(store, seen))  # fmt: skip
    a = make("""(module (import "b" "g" (func $g (param i32) (result i32)))
      (type $t (func (param i32) (result i32))) (elem declare func $a2)
      (func $a2 (param i32) (result i32) (call $g (local.get 0)))
      (func (export "f") (param i32) (result i32) (call_ref $t (local.get 0) (ref.func $a2))))""",
             store, {"b": link.Provider(b.mod, b.inst)})  # fmt: skip
    # the entry goes through a C entry; A's call of B's g (this compiler's context, D71), B's
    # own call_indirect and A's own call_ref call the body (S0015)
    want = [0] * 3 + [1] * 3
    assert a("f", 5) == [5] and seen == [[want]] and checks[0] == 1


def test_try_table_frames_libcalls_and_throw_allocations(checks: list[int]) -> None:
    store, seen = StoreState(), []
    jit = make(f"""(module {H} (memory 1) (tag $e (param externref))
      (func $g (param i32) (result i32) (drop (memory.grow (i32.const 0))) (call $h (local.get 0)))
      (func (export "f") (param i32) (result i32)
        (drop (block $c (result externref)
          (try_table (catch $e $c) (drop (call $g (local.get 0))) (throw $e (ref.null extern)))
          (unreachable)))
        (local.get 0))
      (func (export "many") (param $n i32) (local externref)
        (loop $l
          (drop (block $c (result externref)
            (try_table (catch $e $c) (throw $e (local.get 1))) (unreachable)))
          (br_if $l (local.tee $n (i32.sub (local.get $n) (i32.const 1)))))))""",
               store, recorder(store, seen))  # fmt: skip
    # grow, h, then ALLOC of the first exception object (it commits the heap)
    assert jit("f", 2) == [2] and seen == [[[0] * 4]] and checks[0] == 3
    jit("many", 100_000)  # 24 bytes a throw: the 1 MiB semispace fills, ALLOC collects
    assert checks[0] > 3 and _stacks(store).heap.collections > 0


def test_reference_slots_are_checked(checks: list[int]) -> None:
    store, hits = StoreState(), []

    def host(args: Sequence[Raw]) -> list[Raw]:
        st = _stacks(store)
        externs, st.externs = st.externs, []  # the handle in the marked slots is no longer live
        with pytest.raises(RuntimeError, match="not a reference"):
            recheck(store, st.exit[2])
        st.externs = externs
        recheck(store, st.exit[2])  # and live again: fine
        hits.append(1)
        return [args[0]]

    jit = make(f"""(module {H} (func (export "f") (param $x externref) (result externref)
      (local.get $x) (drop (call $h (i32.const 1)))))""", store, {"env": {"h": host}})  # fmt: skip
    assert jit("f", 0) == [0]  # null: valid
    st = _stacks(store)
    h = st.to_raw((EXTERN, True, EXTERN, None, 0), object())
    assert h & 3 == 2  # a host value: (h << 2) | 2 (S0013 3b)
    assert jit.inst.call(0 + 1, [h]) == [h] and hits == [1, 1]  # type: ignore[attr-defined]


def test_a_broken_record_is_an_error(checks: list[int]) -> None:
    store = StoreState()

    def host(args: Sequence[Raw]) -> list[Raw]:
        st = _stacks(store)
        rec = list(st.exit)
        recheck(store, rec[2])
        st.exit[0] = 0
        with pytest.raises(RuntimeError, match="not written"):
            recheck(store, rec[2])
        st.exit[0], st.exit[1] = rec[0], rec[1] + 1  # a return address that is no call site
        with pytest.raises(RuntimeError, match="not a call site"):
            recheck(store, rec[2])
        st.exit[1] = rec[1]
        st.walk_vsp.insert(0, 8)  # an entry the walk does not find
        with pytest.raises(RuntimeError, match="entries"):
            recheck(store, rec[2])
        del st.walk_vsp[0]
        st.exit[2] += 8  # a frame base that is not its entry's
        with pytest.raises(RuntimeError, match="not its entry"):
            recheck(store, rec[2] + 8)
        st.exit[2] = rec[2]
        return [args[0]]

    jit = make(f'(module {H} (func (export "f") (param i32) (result i32) (call $h (local.get 0))))',
               store, {"env": {"h": host}})  # fmt: skip
    assert jit("f", 1) == [1]


def test_a_module_without_its_site_table_or_with_a_3a_one_is_refused() -> None:
    mod = parse(
        wat('(module (tag $t) (func (export "f") (param externref) (call 0 (ref.null extern))))')
    )
    validate(mod)
    t, store = base.TARGETS[JIT](), StoreState()
    c = t.compile(mod)
    assert isinstance(c, Compiled) and c.stackmaps is not None  # one map: step 3a's form differs
    old = form_3a([(r, *x[:5]) for r, x in gc.decode(c.stackmaps).items()])
    for bad, match in [(None, "stack-map table"), (old, "STACKMAPS")]:  # D2: never a guess
        with pytest.raises(ValueError, match=match):
            t.instantiate(dataclasses.replace(c, stackmaps=bad), [], store)
    assert store.instances == [] and _stacks(store).tags == {}
    t.instantiate(c, [], store)


def forged(text: str, store: StoreState, providers: Any = None, kill: int = 0) -> Inst:
    """An instance whose maps say slot `kill` is dead wherever they mark it (step 7.1 has no
    liveness yet; the walk's nulling is tested through a forged table)."""
    mod = parse(wat(text))
    validate(mod)
    t = base.TARGETS[JIT]()
    c = t.compile(mod)
    assert isinstance(c, Compiled) and c.stackmaps is not None
    s = [(r, *x[:5], x[5] & ~(1 << kill)) for r, x in gc.decode(c.stackmaps).items()]
    c = dataclasses.replace(c, stackmaps=gc.encode(s))
    return Inst(mod, t.instantiate(c, link.resolve(mod, providers or {}), store))


@pytest.mark.parametrize("kill", [0, 63])  # 63: no slot, the control
@pytest.mark.parametrize("by", ["check", "collection"])
def test_a_dead_slot_is_nulled(
    by: str, kill: int, checks: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, o = StoreState(), object()
    if by == "check":  # D7: at every host call, no heap needed
        jit = forged(f"""(module {H} (func (export "f") (param $x externref) (result externref)
          (drop (call $h (i32.const 1))) (local.get $x)))""",
                     store, {"env": {"h": list[Raw]}}, kill)  # fmt: skip
        out = jit.inst.call(1, [o])  # type: ignore[attr-defined]
        assert out == [None if kill == 0 else o] and checks[0] == 1
        return
    monkeypatch.setenv("WABOROS_JIT_WALK_CHECK", "0")  # only the collection nulls
    monkeypatch.setenv("WABOROS_JIT_GC_STRESS", "1")  # every allocation collects
    jit = forged("""(module (type $p (struct (field i32)))
      (func (export "f") (result i32) (local $s (ref null $p))
        (local.set $s (struct.new $p (i32.const 7))) (drop (struct.new $p (i32.const 8)))
        (ref.is_null (local.get $s))))""", store, None, kill)  # fmt: skip
    assert jit("f") == [kill == 0] and checks[0] == 0


BAD_SAVED: dict[
    str, Callable[[int], int]
] = {  # the innermost entry's saved sp; nsp: the stack pointer at that entry's call
    "low": lambda nsp: 0x10,
    "high": lambda nsp: 0x7FF000000000,
    "far above": lambda nsp: nsp + 0x1000000,
    "below its entry": lambda nsp: nsp - 0x100,
}


@pytest.mark.parametrize("bad", [*BAD_SAVED, "exit record"])
def test_a_corrupt_record_is_an_error_not_a_crash(bad: str) -> None:
    store, hits = StoreState(), []

    def host(args: Sequence[Raw]) -> list[Raw]:
        if args[0]:
            return jit("run", args[0] - 1)
        st = _stacks(store)
        tramp = store.instances[0].tramp  # type: ignore[attr-defined]
        frames, _ = walk.walk(store, st)[0]
        _, site, sp, _ = frames[-1]
        nsp = sp + site[2]
        at = ctypes.addressof(st.exit) if bad == "exit record" else nsp + tramp.saved
        word = ctypes.c_uint64.from_address(at)
        old, word.value = word.value, 0x10 if bad == "exit record" else BAD_SAVED[bad](nsp)
        try:
            with pytest.raises(RuntimeError, match="JIT walk"):
                walk.walk(store, st)
        finally:
            word.value = old
        hits.append(1)
        return [0]

    jit = make(f"""(module {H} (func (export "run") (param i32) (result i32)
      (call $h (local.get 0))))""", store, {"env": {"h": host}})  # fmt: skip
    assert jit("run", 1) == [0] and hits == [1]


@pytest.mark.parametrize("what", ["shift", "nslots"])
def test_a_frame_outside_the_value_stack_is_an_error_not_a_write(
    what: str, checks: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gatherer checks a frame's slots against the value stack before it forwards or nulls
    any (as walk.check). A forged shift alone is refused earlier: the walk checks each segment's
    outermost base against its entry's, and shifts are unsigned, so every base lies between the
    entry's and the innermost one. A forged `nslots` with a dead bit 2 MiB up reaches the check."""
    monkeypatch.setenv("WABOROS_JIT_WALK_CHECK", "0")  # only the collection walks
    monkeypatch.setenv("WABOROS_JIT_GC_STRESS", "1")
    text = """(module (type $p (struct (field i32)))
      (func $g (drop (struct.new $p (i32.const 8)))) (func (export "f") (result i32)
        (local $s (ref null $p)) (local.set $s (struct.new $p (i32.const 7))) (call $g)
        (struct.get $p 0 (local.get $s))))"""
    mod = parse(wat(text))
    validate(mod)
    t, far = base.TARGETS[JIT](), 1 << 18
    c = t.compile(mod)
    assert isinstance(c, Compiled) and c.stackmaps is not None

    def forge(sh: int, ra: int, up: int, n: int, refs: int, live: int) -> gc.SiteMap:
        if refs and what == "shift":
            return sh + (sh > 0), ra, up, n, refs, live  # the call site of $g: f's base moves
        if refs:
            return sh, ra, up, far + 1, refs | 1 << far, live
        return sh, ra, up, n, refs, live

    s = [(r, *forge(*x)) for r, x in gc.decode(c.stackmaps).items()]
    c = dataclasses.replace(c, stackmaps=gc.encode(s))
    jit = Inst(mod, t.instantiate(c, [], StoreState()))
    match = "not its entry" if what == "shift" else "outside the value stack"
    with pytest.raises(RuntimeError, match=match):
        jit("f")


def test_the_site_table_is_decoded_once_per_table() -> None:
    mod = parse(wat('(module (func (export "f") (result i32) (i32.const 1)))'))
    validate(mod)
    t = base.TARGETS[JIT]()
    c = t.compile(mod)
    assert isinstance(c, Compiled) and c.stackmaps is not None
    a, b = (t.instantiate(c, [], StoreState()) for _ in range(2))
    assert a.walk_sites is b.walk_sites  # type: ignore[attr-defined]
    c.stackmaps = b"\1"  # replaced after the first instantiation: decoded afresh, refused
    with pytest.raises(ValueError, match="STACKMAPS"):
        t.instantiate(c, [], StoreState())
