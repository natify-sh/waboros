# pyright: basic
"""S0017 step 2: the C store through the shim. One thread per store (a second thread is refused
before anything changes; a store built on one thread runs on another), deep recursion is a
STACK_OVERFLOW trap and never a crash (no fault handler in explicit mode: the limit comes from
`sp_stub` and the thread's stack bounds, on the main thread and on a small thread stack), and
`waboros_store_interrupt` from another thread stops a loop at its next poll."""

import os
import threading
import time
from typing import Any

import pytest

from tests.cloader import shim
from tests.jit import need
from tests.watcache import wat2wasm
from waboros.targets.base import TrapCode
from waboros.targets.jit.common import artifact as A
from waboros.targets.jit.x86_64 import JitX86_64Target

MOD = """(module
  (func $rec (export "rec") (param i32) (result i32)
    local.get 0 i32.const 1 i32.add call $rec)
  (func (export "add") (param i32 i32) (result i32) local.get 0 local.get 1 i32.add)
  (func (export "spin") (loop br 0)))"""


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> Any:
    need(shim.LIB and shim.MACHINE == 62, "build/native's loader library on x86-64 (S0017 step 2)")
    monkeypatch.setenv("WABOROS_JIT_BOUNDS", "explicit")
    eng = shim.Engine("jit-x86_64")
    w = wat2wasm(MOD)
    c: Any = JitX86_64Target().compile(_parse(w))
    mod = eng.load_module(A.write(c, target="jit-x86_64", wasm=w))
    st = shim.Store(eng)
    yield st, mod
    st.close()
    eng.close()


def _parse(w: bytes) -> Any:
    from waboros.frontend.binary import parse
    from waboros.frontend.validate import validate

    m = parse(w)
    validate(m)
    return m


def _in_thread(fn: Any, stack: int = 0) -> Any:
    out: list[Any] = []

    def run() -> None:
        try:
            out.append(fn())
        except BaseException as e:  # noqa: BLE001 - reported to the caller
            out.append(e)

    old = threading.stack_size(stack) if stack else None
    try:
        t = threading.Thread(target=run)
        t.start()
    finally:
        if old is not None:
            threading.stack_size(old)
    t.join()
    return out[0]


def test_another_thread_is_refused(store: Any) -> None:
    st, mod = store
    inst = _in_thread(lambda: st.instantiate(mod))  # built on one thread, its claim released
    add = st.export(inst, "add")
    assert st.call(add, [40, 2], 2)[0] == 42  # this thread is the owner from now on
    e = _in_thread(lambda: st.call(add, [1, 2], 2))
    assert isinstance(e, shim.CError) and e.code == shim.E_THREAD
    assert st.call(add, [1, 2], 2)[0] == 3


@pytest.mark.parametrize("stack", [0, 256 << 10])
def test_deep_recursion_traps(store: Any, stack: int) -> None:
    st, mod = store

    def run() -> Any:
        inst = st.instantiate(mod)
        rec, add = st.export(inst, "rec"), st.export(inst, "add")
        with pytest.raises(shim.CError) as e:
            st.call(rec, [0], 1)
        assert e.value.code == shim.E_TRAP and e.value.trap == TrapCode.STACK_OVERFLOW
        return st.call(add, [2, 3], 2)[0]

    assert (run() if not stack else _in_thread(run, stack)) == 5


def test_interrupt_from_another_thread(store: Any) -> None:
    st, mod = store
    inst = st.instantiate(mod)
    spin, add = st.export(inst, "spin"), st.export(inst, "add")
    done = threading.Event()

    def poke() -> None:
        while not done.wait(0.05):
            st.interrupt()

    t = threading.Thread(target=poke)
    t.start()
    start = time.monotonic()
    try:
        with pytest.raises(shim.CError) as e:
            st.call(spin, [], 0)
    finally:
        done.set()
        t.join()
    assert e.value.code == shim.E_INTERRUPT and time.monotonic() - start < 30
    assert st.call(add, [1, 1], 2)[0] == 2  # the word was cleared at the outermost entry
    assert os.getpid()  # the process is still here
