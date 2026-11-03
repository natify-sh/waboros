# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
"""S0002 step 1: `pyc` behind the Target interface (select, link, instantiate, call)."""

import platform
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


# 32-bit x86 CPython computes in x87 registers, which quiet a signalling NaN on the way through
# struct.unpack("<f") (found by the cibuildwheel i686 cell): pyc does not keep the bits there.
x87_quiets_snan = pytest.mark.xfail(
    platform.machine().lower() in ("i386", "i686", "x86"),
    reason="x87 loads quiet a signalling NaN (pyc f32 values pass through a Python float)",
)


@x87_quiets_snan
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
