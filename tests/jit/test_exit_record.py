# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
"""The store's exit record (S0013 step 3a.2, context word 14): seen from inside a host function it
names the calling frame exactly as the walk needs it; the site numbers step from it to the entry,
whose saved block holds the enclosing record and its frame; every entry puts back the record of
the host call it is nested in, also when the nested call traps or throws."""

import ctypes
import errno
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from tests.jit import native, native_jit
from tests.jit.test_compiler import Inst, wat
from waboros.frontend.binary import parse
from waboros.frontend.validate import validate
from waboros.runtime import link
from waboros.runtime.store import StoreState
from waboros.targets import base
from waboros.targets.base import Raw, Trap, TrapCode
from waboros.targets.jit.common import gc, instance
from waboros.targets.jit.common.compiler import Compiled

JIT = native_jit()
ROOT = Path(__file__).resolve().parents[2]


def word(addr: int) -> int:
    return ctypes.c_uint64.from_address(addr).value


def compiled(text: str) -> tuple[Any, Any, list[tuple[int, tuple[int, int, int, int, int]]]]:
    """(module, Compiled, its sites sorted by return offset)."""
    native(JIT)
    mod = parse(wat(text))
    validate(mod)
    c = base.TARGETS[JIT]().compile(mod)
    assert isinstance(c, Compiled) and c.stackmaps is not None
    return mod, c, sorted(gc.decode(c.stackmaps).items())


def instantiate(mod: Any, c: Any, store: StoreState, providers: Any) -> Inst:
    return Inst(mod, base.TARGETS[JIT]().instantiate(c, link.resolve(mod, providers), store))


def test_exit_record_names_the_caller_and_survives_nested_entries() -> None:
    mod, c, sites = compiled("""(module (import "env" "h" (func $h (param i32) (result i32)))
      (tag $e)
      (func (export "run") (param i32) (result i32) (call $h (local.get 0)))
      (func (export "trap") unreachable)
      (func (export "throw") (throw $e)))""")
    (call_ret, call), (thunk_ret, thunk) = sites[0], sites[-1]  # run's record call; the thunk
    store, seen, recs = StoreState(), [], [(0, 0, 0)]

    def host(args: Sequence[Raw]) -> list[Raw]:
        n = args[0]
        st, tramp = store.target_state, jit.inst.tramp  # type: ignore[attr-defined]
        rec = tuple(st.exit)  # type: ignore[union-attr]
        sp, pc, frame = rec
        code = jit.inst.code.addr  # type: ignore[attr-defined]
        depth = 3 - n  # run's frame at vstack + 16 d (above the outer call's slots), thunk's + 8
        base_ = st.vstack.addr + 16 * depth  # type: ignore[union-attr]
        assert pc == code + thunk_ret  # the bridge's return address: in the thunk
        assert frame == base_ + 8 * thunk[0] + 8 * call[0]  # thunk shift 0, run's call shift 1
        # the walk: the thunk's return address is run's record call; run's is the entry's
        assert word(sp + thunk[1]) == code + call_ret
        sp_run = sp + thunk[2]
        assert word(sp_run + call[1]) == tramp.entry_ret
        saved = sp_run + call[2] + tramp.saved  # the entry's block: enclosing record, its frame
        assert tuple(word(saved + 8 * i) for i in range(3)) == recs[-1]
        assert word(saved + 24) == base_
        seen.append(sp)
        if n:
            recs.append(rec)
            assert jit("run", n - 1) == [n - 1]
            assert tuple(st.exit) == rec  # type: ignore[union-attr]
            with pytest.raises(Trap) as e:
                jit("trap")
            assert e.value.code == TrapCode.UNREACHABLE and tuple(st.exit) == rec  # type: ignore[union-attr]
            with pytest.raises(Trap) as e:
                jit("throw")
            assert e.value.code == TrapCode.UNCAUGHT_EXCEPTION and tuple(st.exit) == rec  # type: ignore[union-attr]
            recs.pop()
        return [n]

    jit = instantiate(mod, c, store, {"env": {"h": host}})
    assert jit("run", 3) == [3]
    assert len(seen) == 4 and seen == sorted(seen, reverse=True)  # deeper: lower on the stack
    assert tuple(store.target_state.exit)[0] == 0  # type: ignore[union-attr]
    ctx = jit.inst.ctx  # type: ignore[attr-defined]
    assert ctx[14] == ctypes.addressof(store.target_state.exit)  # type: ignore[union-attr]


def test_the_walk_steps_over_a_tail_switch_restore_frame() -> None:
    """A's export tail-calls B's function (another context: tail_switch leaves a restore frame),
    which calls the host: B's frame returns to `restore`, whose frame returns to the entry."""
    mb, cb, sb = compiled("""(module (import "env" "h" (func $h (param i32) (result i32)))
      (func (export "callee") (param i32) (result i32) (call $h (local.get 0))))""")
    ma, ca, _ = compiled("""(module (import "b" "callee" (func $c (param i32) (result i32)))
      (func (export "tc") (param i32) (result i32) (return_call $c (local.get 0))))""")
    (call_ret, call), (thunk_ret, thunk) = sb[0], sb[-1]
    store, hits = StoreState(), []

    def host(args: Sequence[Raw]) -> list[Raw]:
        st, tramp = store.target_state, b.inst.tramp  # type: ignore[attr-defined]
        sp, pc, _ = tuple(st.exit)  # type: ignore[union-attr]
        code = b.inst.code.addr  # type: ignore[attr-defined]
        assert pc == code + thunk_ret and word(sp + thunk[1]) == code + call_ret
        sp_b = sp + thunk[2]
        assert word(sp_b + call[1]) == tramp.restore
        sp_r = sp_b + call[2]  # the restore frame: saved context, then the real return address
        assert word(sp_r) == ctypes.addressof(a.inst.ctx)  # type: ignore[attr-defined]  # A's: the entry's
        assert word(sp_r + 8) == tramp.entry_ret
        assert word(sp_r + 16 + tramp.saved + 24) == st.vstack.addr  # type: ignore[union-attr]
        hits.append(1)
        return [args[0] + 1]

    b = instantiate(mb, cb, store, {"env": {"h": host}})
    a = instantiate(ma, ca, store, {"b": link.Provider(mb, b.inst)})
    assert a("tc", 41) == [42] and hits == [1]


def test_a_libcall_leaves_the_record_of_its_site(monkeypatch: pytest.MonkeyPatch) -> None:
    mod, c, sites = compiled("""(module (memory 1)
      (func (export "g") (result i32) (memory.grow (i32.const 0))))""")
    ((grow_ret, grow),) = sites
    store, hits = StoreState(), []
    real = instance.Memory.grow

    def grow_(self: Any, delta: int) -> int:
        sp, pc, frame = tuple(store.target_state.exit)  # type: ignore[union-attr]
        assert pc == jit.inst.code.addr + grow_ret and grow[0] == 0  # type: ignore[attr-defined]
        assert frame == store.target_state.vstack.addr  # type: ignore[union-attr]
        assert word(sp + grow[1]) == jit.inst.tramp.entry_ret  # type: ignore[attr-defined]
        hits.append(1)
        return real(self, delta)

    monkeypatch.setattr(instance.Memory, "grow", grow_)
    jit = instantiate(mod, c, store, {})
    assert jit("g") == [1] and hits == [1]


def test_stack_info_tool_runs() -> None:
    """tools/stack_info.py calls the entry with a context of its own (CI runs it on every OS)."""
    try:
        r = subprocess.run(
            [sys.executable, str(ROOT / "tools/stack_info.py")],
            capture_output=True, text=True, timeout=120,
        )  # fmt: skip
    except OSError as e:  # QEMU user mode without binfmt: the interpreter cannot be exec'd
        if e.errno != errno.ENOEXEC:
            raise
        pytest.skip("cannot run the interpreter in a subprocess (QEMU user mode)")
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert f"jit ({JIT})" in r.stdout or "jit: " in r.stdout
