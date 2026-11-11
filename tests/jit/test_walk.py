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
from waboros.frontend.binary import parse
from waboros.frontend.validate import validate
from waboros.runtime import link
from waboros.runtime.store import StoreState
from waboros.targets import base
from waboros.targets.base import Raw
from waboros.targets.jit.common import walk
from waboros.targets.jit.common.instance import _stacks  # pyright: ignore[reportPrivateUsage]

JIT = native_jit()


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
    segs = walk.walk(store, st, store.instances[0].tramp)  # type: ignore[attr-defined]
    return [[store.instances.index(f[0]) for f in frames] for frames, _ in segs]


H = '(import "env" "h" (func $h (param i32) (result i32)))'


def recheck(store: StoreState, slots: int) -> None:
    """walk.check from inside a host function: without the frame this call's checked callback
    already gave its own nested entries."""
    st = _stacks(store)
    mine = st.walk_vsp.pop()
    try:
        walk.check(store, st, store.instances[0].tramp, slots)  # type: ignore[attr-defined]
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
    assert jit("f", 7) == [7] and seen == [[[0] * 6]] and checks[0] == 1  # thunk + 5 frames


def test_nested_entries_50_deep(checks: list[int]) -> None:
    store, depth = StoreState(), []

    def host(args: Sequence[Raw]) -> list[Raw]:
        if args[0]:
            return jit("run", args[0] - 1)
        depth.append(shape(store))
        return [0]

    jit = make(f"""(module {H} (func (export "run") (param i32) (result i32)
      (call $h (local.get 0))))""", store, {"env": {"h": host}})  # fmt: skip
    assert jit("run", 49) == [0] and depth == [[[0, 0]] * 50] and checks[0] == 50


def test_tail_calls_and_a_tail_switch_restore_frame(checks: list[int]) -> None:
    store, seen = StoreState(), []
    b = make(f"""(module {H} (func $g (param i32) (result i32) (call $h (local.get 0)))
      (func (export "callee") (param i32) (result i32) (return_call $g (local.get 0))))""",
             store, recorder(store, seen))  # fmt: skip
    a = make("""(module (import "b" "callee" (func $c (param i32) (result i32)))
      (func $t (param i32) (result i32) (return_call $c (local.get 0)))
      (func (export "tc") (param i32) (result i32) (return_call $t (local.get 0))))""",
             store, {"b": link.Provider(b.mod, b.inst)})  # fmt: skip
    assert a("tc", 3) == [3] and seen == [[[0, 0]]]  # B's thunk and $g; A's frames are gone
    assert checks[0] == 1


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
    assert a("f", 5) == [5] and seen == [[[0, 0, 0, 1, 1]]] and checks[0] == 1


def test_try_table_frames_libcalls_and_arena_refills(checks: list[int]) -> None:
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
    assert jit("f", 2) == [2] and seen == [[[0, 0, 0]]] and checks[0] == 2  # grow, then h
    jit("many", 10_000)  # 2 words a throw: arena chunks of 8192 words fill up
    assert checks[0] > 2


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
    h = st.to_raw(0x6F, object())
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


def test_a_module_without_its_site_table_is_refused() -> None:
    mod = parse(wat('(module (tag $t) (func (export "f")))'))
    validate(mod)
    t, store = base.TARGETS[JIT](), StoreState()
    c = t.compile(mod)
    with pytest.raises(ValueError, match="stack-map table"):
        t.instantiate(dataclasses.replace(c, stackmaps=None), [], store)  # type: ignore[type-var]
    assert store.instances == [] and _stacks(store).tags == {}
    t.instantiate(c, [], store)


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
        frames, _ = walk.walk(store, st, tramp)[0]
        _, site, sp, _ = frames[-1]
        nsp = sp + site[2]
        at = ctypes.addressof(st.exit) if bad == "exit record" else nsp + tramp.saved
        word = ctypes.c_uint64.from_address(at)
        old, word.value = word.value, 0x10 if bad == "exit record" else BAD_SAVED[bad](nsp)
        try:
            with pytest.raises(RuntimeError, match="JIT walk"):
                walk.walk(store, st, tramp)
        finally:
            word.value = old
        hits.append(1)
        return [0]

    jit = make(f"""(module {H} (func (export "run") (param i32) (result i32)
      (call $h (local.get 0))))""", store, {"env": {"h": host}})  # fmt: skip
    assert jit("run", 1) == [0] and hits == [1]
