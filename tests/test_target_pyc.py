# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
"""S0002 step 1: `pyc` behind the Target interface (select, link, instantiate, call)."""

import sys
import threading
from collections.abc import Mapping, Sequence

import pytest

from tests.test_pyc import DIV, SHARE_A, SHARE_B
from waboros.frontend.binary import parse
from waboros.frontend.validate import validate
from waboros.runtime import link
from waboros.runtime.store import StoreState
from waboros.targets import base
from waboros.targets.base import HostFunc, Raw, TargetInstance, Trap, TrapCode

HEAD = "0061736d01000000"
# (module (import "env" "h" (func (result i32))) (func (export "run") (result i32) call 0))
CALL_HOST = bytes.fromhex(
    HEAD + "0105016000017f" + "02090103656e76016800" + "00" + "03020100" + "0707010372756e0001"
    "0a0601040010000b"
)
# imports env.h0, env.h1 (both () -> i32); "a" calls h0, "b" calls h1
TWO_HOSTS = bytes.fromhex(
    HEAD
    + "0105016000017f"
    + "0213"
    + "02"
    + "03656e76026830"
    + "0000"
    + "03656e76026831"
    + "0000"
    + "0303020000"
    + "0709"
    + "02"
    + "01610002"
    + "01620003"
    + "0a0b"
    + "02"
    + "0400"
    + "10000b"
    + "0400"
    + "10010b"
)
# (func (export "id") (param f32) (result f32) local.get 0)
F32_ID = bytes.fromhex(
    HEAD + "01060160017d017d" + "03020100" + "0706010269640000" + "0a0601040020000b"
)


def _make(
    wasm: bytes,
    hosts: Mapping[str, Mapping[str, HostFunc]] | None = None,
    store: StoreState | None = None,
):
    store = store or StoreState()
    mod = parse(wasm)
    validate(mod)
    target = base.select(mod, base.target_order("pyc"))
    ext = link.resolve(mod, hosts or {})
    return mod, target.instantiate(target.compile(mod), ext, store)


def _func(mod: base.ValidModule, inst: TargetInstance, name: str) -> int:
    return next(i for n, k, i in mod["exports"] if n == name and k == 0)


def test_host_indexerror_is_not_a_trap() -> None:
    def h(_: Sequence[Raw]) -> list[Raw]:
        raise IndexError("from the host")

    mod, inst = _make(CALL_HOST, {"env": {"h": h}})
    with pytest.raises(IndexError, match="from the host"):
        inst.call(_func(mod, inst, "run"), [])


def test_valueerror_across_nested_call() -> None:
    def h1(_: Sequence[Raw]) -> list[Raw]:
        raise ValueError("deep")

    def h0(_: Sequence[Raw]) -> list[Raw]:
        return inst.call(_func(mod, inst, "b"), [])  # wasm -> host -> wasm -> host

    store = StoreState()
    mod, inst = _make(TWO_HOSTS, {"env": {"h0": h0, "h1": h1}}, store)
    with pytest.raises(ValueError, match="deep"):
        inst.call(_func(mod, inst, "a"), [])
    assert store.depth == 0


def test_keyboard_interrupt_comes_out_unchanged() -> None:
    def h(_: Sequence[Raw]) -> list[Raw]:
        raise KeyboardInterrupt

    mod, inst = _make(CALL_HOST, {"env": {"h": h}})
    with pytest.raises(KeyboardInterrupt):
        inst.call(_func(mod, inst, "run"), [])


@pytest.mark.parametrize("raises", [False, True])  # the host returns, or raises ValueError
@pytest.mark.parametrize("nested", [False, True])
def test_async_exception_at_every_delivery_point_restores_the_depth(
    nested: bool, raises: bool
) -> None:
    """A signal handler runs at a function's entry or after a C call. Raise KeyboardInterrupt at
    each Python function entry and each builtin's `c_return` of one export call (wasm -> host
    [-> wasm -> host]), in turn (the profiler does not see type calls or backward jumps). Every
    call gives the right result or KeyboardInterrupt, and the store's depth is back to 0 (D34;
    before: one at the entry of `store.leave()` in `_run`'s finally left the depth raised)."""

    def h0(_: Sequence[Raw]) -> list[Raw]:
        r = inst.call(b, []) if nested else [7]
        if raises:
            raise ValueError("h")
        return r

    store = StoreState()
    mod, inst = _make(TWO_HOSTS, {"env": {"h0": h0, "h1": lambda _: [7]}}, store)
    a, b = _func(mod, inst, "a"), _func(mod, inst, "b")
    at = [0, 0, 0]  # the point to raise at, points passed, raised

    def prof(frame: object, ev: str, arg: object) -> None:
        if ev in ("call", "c_return"):
            at[1] += 1
            if at[1] == at[0]:
                sys.setprofile(None)
                at[2] = 1
                raise KeyboardInterrupt

    bad = list[tuple[int, str, int]]()
    while True:
        at[:] = [at[0] + 1, 0, 0]
        sys.setprofile(prof)
        try:
            k = "ok" if inst.call(a, []) == [7] and not raises else "wrong result"
        except KeyboardInterrupt:
            k = "KeyboardInterrupt"
        except ValueError as e:
            k = "ok" if raises and str(e) == "h" else repr(e)
        except BaseException as e:
            k = repr(e)
        finally:
            sys.setprofile(None)
        if not at[2]:
            break
        if k not in ("ok", "KeyboardInterrupt") or store.depth:
            bad.append((at[0], k, store.depth))
            store.depth = 0
    assert at[0] > 5 and not bad, bad


def test_host_result_is_raw() -> None:
    mod, inst = _make(CALL_HOST, {"env": {"h": lambda _: [0xFFFFFFFF]}})
    assert inst.call(_func(mod, inst, "run"), []) == [0xFFFFFFFF]


def test_wasm_trap_codes() -> None:
    _, inst = _make(DIV)
    assert inst.call(0, [7, 2]) == [3]
    with pytest.raises(Trap) as e:
        inst.call(0, [1, 0])
    assert e.value.code is TrapCode.INTEGER_DIVISION_BY_ZERO
    assert TrapCode.INTERRUPT == 12


def test_f32_signalling_nan_survives() -> None:
    _, inst = _make(F32_ID)
    assert inst.call(0, [0x7FA00000]) == [0x7FA00000]


def test_link_errors() -> None:
    a_mod, a_inst = _make(bytes.fromhex(SHARE_A))
    prov = {"A": link.Provider(a_mod, a_inst)}
    assert [e.kind for e in link.resolve(parse(bytes.fromhex(SHARE_B)), prov)] == [
        "memory",
        "global",
    ]
    with pytest.raises(link.LinkError, match="unknown import"):
        link.resolve(parse(bytes.fromhex(SHARE_B)), {})
    immutable = bytes.fromhex(SHARE_B.replace("67037f01", "67037f00"))
    with pytest.raises(link.LinkError, match="incompatible import type"):
        link.resolve(parse(immutable), prov)


@pytest.mark.skipif(sys.platform == "emscripten", reason="Pyodide cannot start threads")
def test_one_thread_per_store() -> None:
    store = StoreState()
    store.enter()
    store.leave()
    err: list[BaseException] = []

    def other() -> None:
        try:
            store.enter()
        except RuntimeError as e:
            err.append(e)

    t = threading.Thread(target=other)
    t.start()
    t.join()
    assert len(err) == 1
