"""Function-type subtyping in JIT code (S0013 step 5.6, D6): the record's type id against the
signature word inline, a final target stops there, otherwise a call to the collector module's
`is_subtype` through the IS_SUBTYPE record (function-type records and the tid map in the heap).
`call_indirect`, `return_call_indirect`, `ref.test`, `ref.cast` and `br_on_cast` to final and
non-final function types, with the functions of another module, against `pyc`."""

from typing import Any, cast

import pytest

from tests.jit import native, native_jit
from tests.jit.test_compiler import Inst, build, outcome, wat
from tests.spec.fetch import CACHE_DIR
from waboros.frontend.binary import parse  # pyright: ignore[reportUnknownVariableType]
from waboros.frontend.validate import validate  # pyright: ignore[reportUnknownVariableType]
from waboros.runtime import link
from waboros.runtime.store import StoreState
from waboros.targets import base
from waboros.targets.base import TrapCode
from waboros.targets.jit.common import gc
from waboros.targets.jit.common.context import GC_RESERVED, NSTACK_LIMIT
from waboros.targets.jit.common.instance import _stacks, type_id  # pyright: ignore[reportPrivateUsage]

JIT = native_jit()
BOUNDS = ["guard", "explicit"] if JIT == "jit-x86_64" else ["explicit"]
# t <- u <- w <- x (depth 3), u <- v (final); o final with no supertype
TYPES = """(type $t (sub (func (result i32)))) (type $u (sub $t (func (result i32))))
  (type $v (sub final $u (func (result i32)))) (type $o (func (result i32)))"""
A = f"""(module {TYPES}
  (type $w (sub $u (func (result i32)))) (type $x (sub $w (func (result i32))))
  (table (export "tab") 6 funcref)
  (func $ft (type $t) (i32.const 1)) (func $fu (type $u) (i32.const 2))
  (func $fv (type $v) (i32.const 3)) (func $fx (type $x) (i32.const 5))
  (func $fo (type $o) (i32.const 6))
  (elem (i32.const 0) func $ft $fu $fv $fx $fo))"""
B = f"""(module {TYPES} (import "a" "tab" (table $tab 6 funcref))
  (func $el (param i32) (result funcref) (table.get $tab (local.get 0)))
  (func (export "call_t") (param i32) (result i32) (call_indirect $tab (type $t) (local.get 0)))
  (func (export "call_u") (param i32) (result i32) (call_indirect $tab (type $u) (local.get 0)))
  (func (export "tail_u") (param i32) (result i32)
    (return_call_indirect $tab (type $u) (local.get 0)))
  (func (export "test_t") (param i32) (result i32) (ref.test (ref $t) (call $el (local.get 0))))
  (func (export "test_u") (param i32) (result i32) (ref.test (ref $u) (call $el (local.get 0))))
  (func (export "test_v") (param i32) (result i32) (ref.test (ref $v) (call $el (local.get 0))))
  (func (export "test_o") (param i32) (result i32)
    (ref.test (ref null $o) (call $el (local.get 0))))
  (func (export "cast_u") (param i32) (result i32)
    (call_ref $u (ref.cast (ref $u) (call $el (local.get 0)))))
  (func (export "cast_v") (param i32) (result i32)
    (call_ref $v (ref.cast (ref $v) (call $el (local.get 0)))))
  (func (export "br_u") (param i32) (result i32)
    (block $l (result (ref $u))
      (br_on_cast $l funcref (ref $u) (call $el (local.get 0)))
      (drop) (return (i32.const -1)))
    (call_ref $u))
  (func (export "brf_t") (param i32) (result i32)
    (block $l (result funcref)
      (call_ref $t (br_on_cast_fail $l funcref (ref $t) (call $el (local.get 0)))) (return))
    (drop) (i32.const -1)))"""
NAMES = ["call_t", "call_u", "tail_u", "test_t", "test_u", "test_v", "test_o", "cast_u",
         "cast_v", "br_u", "brf_t"]  # fmt: skip


@pytest.fixture(autouse=True, params=BOUNDS)
def _bounds(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WABOROS_JIT_BOUNDS", request.param)


def pair(target: str) -> tuple[Inst, Inst]:
    store = StoreState()
    a = build(wat(A), target, store=store)
    return a, build(wat(B), target, {"a": link.Provider(a.mod, a.inst)}, store)


def table(b: Inst) -> list[list[Any]]:
    return [[outcome(b, n, i) for i in range(6)] for n in NAMES]


def test_function_subtypes_across_two_modules_as_pyc() -> None:
    native(JIT)
    got = table(pair(JIT)[1])
    sig, null = TrapCode.BAD_SIGNATURE, TrapCode.INDIRECT_CALL_TO_NULL
    cf = TrapCode.CAST_FAILURE
    assert got[:3] == [[[1], [2], [3], [5], sig, null], *[[sig, [2], [3], [5], sig, null]] * 2]
    assert got[3:7] == [[[1]] * 4 + [[0], [0]], [[0], [1], [1], [1], [0], [0]],
                        [[0], [0], [1], [0], [0], [0]], [[0]] * 4 + [[1], [1]]]  # fmt: skip
    assert got[7:9] == [[cf, [2], [3], [5], cf, cf], [cf, cf, [3], cf, cf, cf]]
    assert got[9:] == [[[-1 & 0xFFFFFFFF], [2], [3], [5], [-1 & 0xFFFFFFFF], [-1 & 0xFFFFFFFF]],
                       [[1], [2], [3], [5], [-1 & 0xFFFFFFFF], [-1 & 0xFFFFFFFF]]]  # fmt: skip
    assert got == table(pair("pyc")[1])


def test_inline_equality_and_final_targets_make_no_call() -> None:
    """With u's tid-map entry pointing at a bad record, every is_subtype call with u as q or t
    traps: the tests that answer inline (q == t, or a final target) still run."""
    native(JIT)
    a, b = pair(JIT)
    heap = _stacks(cast(Any, a.inst).store).heap
    m: Any = b.mod
    heap.u32(gc.TID_MAP + 4 * type_id(m, 1), gc.TYPES + 4)  # misaligned: the module traps
    assert outcome(b, "call_u", 1) == [2] and outcome(b, "test_u", 1) == [1]
    assert outcome(b, "test_v", 1) == [0] and outcome(b, "cast_v", 2) == [3]
    assert outcome(b, "test_o", 1) == [0] and outcome(b, "test_t", 0) == [1]
    for n, i in [("call_u", 3), ("tail_u", 2), ("test_u", 0), ("cast_u", 3), ("br_u", 2),
                 ("test_t", 1)]:  # fmt: skip
        assert outcome(b, n, i) == TrapCode.UNREACHABLE, (n, i)


def test_the_collector_follows_the_stores_native_stack_limit() -> None:
    """The collector instance is outside `store.instances`, yet JIT code calls into it: a new
    outermost entry's native stack limit reaches its context too, else its prologue checks a
    stale one (here: one that every frame is below, so is_subtype would trap STACK_OVERFLOW)."""
    native(JIT)
    _, b = pair(JIT)
    st = _stacks(cast(Any, b.inst).store)
    coll = st.heap.collector[0]
    coll.ctx[NSTACK_LIMIT], st.nlimit = (1 << 64) - 1, 0  # stale; the next entry recomputes
    assert outcome(b, "call_u", 3) == [5] and coll.ctx[NSTACK_LIMIT] == st.nlimit
    coll.ctx[NSTACK_LIMIT] = (1 << 64) - 1  # as a collection leaves it, the store's own unchanged
    assert outcome(b, "call_u", 3) == [5] and coll.ctx[NSTACK_LIMIT] == st.nlimit


def test_no_module_uses_the_reserved_word(monkeypatch: pytest.MonkeyPatch) -> None:
    """No compiled module reads, writes or calls GC group word 7 (FUNC_SUBTYPE until 5.6): every
    context word a backend method names, over the spec's `gc` modules and these two."""
    files = sorted((CACHE_DIR / "json" / "gc").rglob("*.wasm")) if CACHE_DIR.exists() else []
    mods = [wat(A), wat(B)] + [f.read_bytes() for f in files]
    t: Any = base.TARGETS[JIT]()
    words: list[int] = []
    cls: Any = t.backend().__class__
    for name in ("ctx_load", "ctx_store", "ctx_addr", "call_host", "call_record", "tail_record"):
        real = getattr(cls, name)

        def spy(self: Any, *a: Any, real: Any = real, name: str = name) -> Any:
            words.append(a[1] if name in ("call_host", "call_record", "tail_record") else a[0])
            return real(self, *a)

        monkeypatch.setattr(cls, name, spy)
    n = 0
    for w in mods:
        try:
            m: Any = parse(w)
            validate(m)
        except Exception:  # the spec's malformed and invalid modules
            continue
        words.clear()
        c: Any = t.compile(m)
        if c.layout.gcsvc:
            n += 1
            assert c.layout.service(GC_RESERVED) not in words
    assert n >= 2
