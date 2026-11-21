"""Objects move under every way a reference can be live in compiled code (S0013 step 5.4): operand
stack, callee parameters, tail calls, globals, tables, an exnref payload, host to wasm nesting, a
non-final call_indirect, v128 beside references, array.new_fixed, churn, block and loop parameters,
a host handle. Each case computes a checksum of every surviving field; it must equal `pyc`'s, with
at least one collection during the call, under the stress, poison and verify switches (D61). On the
machine's own JIT, in both bounds modes where it has them."""

from typing import Any

import pytest

from tests.jit import native, native_jit
from tests.jit.test_compiler import Inst, build, wat
from waboros.runtime.store import StoreState
from waboros.targets.jit.common.instance import _stacks  # pyright: ignore[reportPrivateUsage]

JIT = native_jit()
BOUNDS = ["guard", "explicit"] if JIT == "jit-x86_64" else ["explicit"]
A3 = """(call $cons (i32.const 1) (call $mk (i32.const 2))) (call $mk (i32.const 3))
  (call $cons (i32.const 4) (call $mk (i32.const 5)))"""
FIXED = " ".join(
    f"(call $cons (i32.const {i}) (call $mk (i32.const {i + 100})))" for i in range(40)
)
W = f"""(module
  (type $p (struct (field (mut i32)) (field (mut anyref))))
  (type $aa (array (mut anyref))) (type $la (array (mut i64)))
  (type $f3 (func (param anyref anyref anyref) (result i32)))
  (type $base (sub (func (param anyref) (result i32))))
  (type $sub (sub $base (func (param anyref) (result i32))))
  (import "env" "cb" (func $cb (param i32) (result i32)))
  (import "env" "cb2" (func $cb2 (param i32) (result i32)))
  (import "env" "hv" (func $hv (result externref)))
  (import "env" "chk" (func $chk (param externref) (result i32)))
  (tag $tg (param anyref anyref))
  (elem $e anyref (item (struct.new $p (i32.const 5) (struct.new $p (i32.const 6) (ref.null any))))
    (item (struct.new $p (i32.const 7) (ref.null any))))
  (global $g (mut anyref) (ref.null any))
  (table $t 4 anyref) (table $ft 1 funcref) (table $ct 2 funcref)
  (elem (table $ft) (i32.const 0) func $s3) (elem (table $ct) (i32.const 0) func $fs $fb)
  (func $mk (param i32) (result (ref $p)) (struct.new $p (local.get 0) (ref.null any)))
  (func $cons (param i32 anyref) (result anyref) (struct.new $p (local.get 0) (local.get 1)))
  (func $val (param anyref) (result i32) (struct.get $p 0 (ref.cast (ref $p) (local.get 0))))
  (func $next (param anyref) (result anyref) (struct.get $p 1 (ref.cast (ref $p) (local.get 0))))
  (func $csum (param $x anyref) (result i32) (local $s i32)
    (block $d (loop $l (br_if $d (ref.is_null (local.get $x)))
      (local.set $s (i32.add (i32.mul (local.get $s) (i32.const 31)) (call $val (local.get $x))))
      (local.set $x (call $next (local.get $x))) (br $l)))
    (local.get $s))
  (func $r2 (param anyref anyref) (result i32)
    (i32.add (call $csum (local.get 0)) (i32.mul (call $csum (local.get 1)) (i32.const 1000))))
  (func $r3 (param anyref anyref anyref) (result i32)
    (i32.add (call $r2 (local.get 0) (local.get 1))
             (i32.mul (call $csum (local.get 2)) (i32.const 1000000))))
  (func $s3 (type $f3) (drop (call $mk (i32.const 99))) (drop (call $mk (i32.const 98)))
    (call $r3 (local.get 0) (local.get 1) (local.get 2)))
  (func $tc (type $f3) (drop (call $mk (i32.const 9)))
    (return_call $s3 (local.get 1) (local.get 2) (local.get 0)))
  (func $tci (type $f3) (drop (call $mk (i32.const 9)))
    (return_call_indirect $ft (type $f3) (local.get 2) (local.get 0) (local.get 1) (i32.const 0)))
  (func $tcr (type $f3) (drop (call $mk (i32.const 9)))
    (return_call_ref $f3 (local.get 1) (local.get 0) (local.get 2) (ref.func $s3)))
  (func $fs (type $sub) (drop (call $mk (i32.const 0))) (call $csum (local.get 0)))
  (func $fb (type $base) (drop (call $mk (i32.const 0)))
    (i32.mul (call $csum (local.get 0)) (i32.const 2)))
  (func $asum (param (ref $aa)) (result i32) (local $i i32) (local $s i32)
    (loop $l (drop (call $mk (i32.const 1)))  ;; collects: the array lives across it
      (local.set $s (i32.add (i32.mul (local.get $s) (i32.const 7))
        (call $csum (array.get $aa (local.get 0) (local.get $i)))))
      (br_if $l (i32.lt_u (local.tee $i (i32.add (local.get $i) (i32.const 1)))
                          (array.len (local.get 0)))))
    (local.get $s))
  (func (export "inner0") (param i32) (result i32)
    (drop (call $mk (i32.const 1))) (drop (call $cons (i32.const 2) (call $mk (i32.const 3))))
    (local.get 0))
  (func (export "inner") (param i32) (result i32) (local $x anyref) (local $s i32)
    (local.set $x (call $cons (i32.const 11) (call $mk (i32.const 12))))
    (local.set $s (call $csum (local.get $x)))
    (drop (call $cb2 (local.get 0)))  ;; host to wasm again: this frame's local is below it
    (i32.add (local.get 0) (i32.sub (call $csum (local.get $x)) (local.get $s))))
  (func (export "mkchain") (result anyref)
    (call $cons (i32.const 1) (call $cons (i32.const 2) (call $mk (i32.const 3)))))
  (func (export "alloc") (drop (call $cons (i32.const 1) (call $mk (i32.const 2)))))
  (func (export "hsum") (param anyref) (result i32) (call $csum (local.get 0)))
  (func (export "id") (param anyref) (result anyref) (drop (call $mk (i32.const 0))) (local.get 0))
  (func (export "t1") (result i32) (call $r3 {A3}))
  (func (export "t2") (result i32) (call $s3 {A3}))
  (func (export "t3a") (result i32) (call $tc {A3}))
  (func (export "t3b") (result i32) (call $tci {A3}))
  (func (export "t3c") (result i32) (call $tcr {A3}))
  (func (export "t4") (result i32)
    (global.set $g (call $cons (i32.const 7) (call $mk (i32.const 8))))
    (drop (call $mk (i32.const 1))) (drop (call $cons (i32.const 2) (call $mk (i32.const 3))))
    (call $csum (global.get $g)))
  (func (export "t5") (result i32)
    (table.set $t (i32.const 2) (call $cons (i32.const 7) (call $mk (i32.const 8))))
    (table.set $t (i32.const 0) (call $mk (i32.const 5)))
    (drop (call $mk (i32.const 1))) (drop (call $cons (i32.const 2) (call $mk (i32.const 3))))
    (call $r2 (table.get $t (i32.const 2)) (table.get $t (i32.const 0))))
  (func (export "t6") (result i32) (local $e exnref)
    (block $h (result anyref anyref exnref)
      (try_table (catch_ref $tg $h)
        (throw $tg (call $cons (i32.const 1) (call $mk (i32.const 2))) (call $mk (i32.const 3))))
      unreachable)
    (local.set $e) (drop) (drop)
    (drop (call $mk (i32.const 9))) (drop (call $mk (i32.const 8)))
    (call $r2 (block $h2 (result anyref anyref)
      (try_table (catch $tg $h2) (throw_ref (local.get $e))) unreachable)))
  (func (export "t7") (result i32) (local $x anyref)
    (local.set $x (call $cons (i32.const 4) (call $mk (i32.const 5))))
    (i32.add (call $r3 (call $mk (i32.const 6))
                       (call $cons (call $cb (i32.const 7)) (call $mk (i32.const 8)))
                       (local.get $x))
             (call $csum (local.get $x))))
  (func (export "t8") (result i32) (local $x anyref)
    (local.set $x (call $cons (i32.const 1) (call $mk (i32.const 2))))
    (call $r3 (call $mk (i32.const 7))
      (struct.new $p (call_indirect $ct (type $base)
                        (call $cons (i32.const 5) (call $mk (i32.const 6))) (i32.const 0))
                     (local.get $x))
      (call $mk (call_indirect $ct (type $base) (local.get $x) (i32.const 1)))))
  (func (export "t9") (result i32) (local $v v128) (local $w v128) (local $x anyref)
    (local.set $v (v128.const i32x4 1 2 3 4))
    (local.set $x (call $cons (i32.const 10) (call $mk (i32.const 20))))
    (local.set $w (i32x4.add (local.get $v) (i32x4.replace_lane 2 (local.get $v)
      (call $csum (call $cons (i32.const 5) (local.get $x))))))
    (i32.add (i32.add (i32x4.extract_lane 0 (local.get $w))
                      (i32.mul (i32x4.extract_lane 1 (local.get $w)) (i32.const 3)))
      (i32.add (i32.add (i32.mul (i32x4.extract_lane 2 (local.get $w)) (i32.const 5))
                        (i32.mul (i32x4.extract_lane 3 (local.get $w)) (i32.const 7)))
               (call $csum (local.get $x)))))
  (func (export "t10") (result i32) (call $asum (array.new_fixed $aa 40 {FIXED})))
  (func (export "t10b") (result i32)  ;; the array services read operands after collecting
    (i32.add (call $asum (array.new $aa (call $cons (i32.const 3) (call $mk (i32.const 4)))
                                        (i32.const 5)))
             (call $asum (array.new_elem $aa $e (i32.const 0) (i32.const 2)))))
  (func (export "t13") (result i32) (local $a (ref $aa)) (local $x anyref)  ;; i31 and host words
    (local.set $x (any.convert_extern (call $hv)))
    (local.set $a (array.new_fixed $aa 3 (ref.i31 (i32.const 5)) (local.get $x)
                                         (struct.new $p (i32.const 9) (ref.i31 (i32.const 6)))))
    (drop (call $mk (i32.const 1))) (drop (call $mk (i32.const 2)))
    (i32.add (i32.add (i31.get_s (ref.cast (ref i31) (array.get $aa (local.get $a) (i32.const 0))))
                      (call $chk (extern.convert_any (array.get $aa (local.get $a) (i32.const 1)))))
      (i32.add (struct.get $p 0 (ref.cast (ref $p) (array.get $aa (local.get $a) (i32.const 2))))
               (i31.get_s (ref.cast (ref i31) (struct.get $p 1 (ref.cast (ref $p)
                 (array.get $aa (local.get $a) (i32.const 2)))))))))
  (func (export "t14") (result i32) (local $x anyref) (local $a (ref $aa))  ;; ref.eq identities
    (local.set $x (call $mk (i32.const 3)))
    (global.set $g (local.get $x)) (table.set $t (i32.const 1) (local.get $x))
    (local.set $a (array.new $aa (local.get $x) (i32.const 2)))
    (drop (call $mk (i32.const 1))) (drop (call $mk (i32.const 2)))
    (i32.add (i32.add (ref.eq (ref.cast (ref eq) (global.get $g))
                              (ref.cast (ref eq) (local.get $x)))
                      (i32.shl (ref.eq (ref.cast (ref eq) (table.get $t (i32.const 1)))
                                       (ref.cast (ref eq) (local.get $x))) (i32.const 1)))
             (i32.shl (ref.eq (ref.cast (ref eq) (array.get $aa (local.get $a) (i32.const 1)))
                              (ref.cast (ref eq) (local.get $x))) (i32.const 2))))
  (func (export "t11") (result i32) (local $i i32) (local $x anyref)
    (local.set $x (call $cons (i32.const 1) (call $cons (i32.const 2) (call $mk (i32.const 3)))))
    (global.set $g (call $cons (i32.const 4) (call $mk (i32.const 5))))
    (loop $l (drop (call $cons (local.get $i) (call $mk (local.get $i))))
      (drop (array.new $la (i64.const 1) (i32.const 8)))
      (br_if $l (i32.lt_u (local.tee $i (i32.add (local.get $i) (i32.const 1))) (i32.const 150))))
    (i32.add (i32.add (call $csum (local.get $x)) (call $csum (global.get $g))) (local.get $i)))
  (func (export "t12") (result i32) (local $n i32) (local $s i32) (local $cur anyref)
    (call $cons (i32.const 1) (call $mk (i32.const 2))) (call $mk (i32.const 3))
    (block (param anyref anyref) (result i32)
      (drop (call $mk (i32.const 9))) (drop (call $mk (i32.const 10))) (call $r2))
    (local.set $s)
    (call $mk (i32.const 0))
    (loop $l (param anyref) (result anyref)
      (drop (call $mk (local.get $n))) (local.set $cur)
      (call $cons (i32.add (call $val (local.get $cur)) (i32.const 1)) (local.get $cur))
      (br_if $l (i32.lt_u (local.tee $n (i32.add (local.get $n) (i32.const 1))) (i32.const 15))))
    (local.set $cur)
    (i32.add (local.get $s) (call $csum (local.get $cur)))))"""
CASES = [
    f"t{k}" for k in ("1", "2", "3a", "3b", "3c", "4", "5", "6", "7", "8", "9", "10", "10b")
] + [
    "t11",
    "t12",
    "t13",
    "t14",
]
HOST = object()


def hv(_: Any) -> list[Any]:
    return [HOST]


def chk(a: Any) -> list[Any]:
    return [int(a[0] is HOST)]


def oracle() -> Inst:
    def cb(a: Any) -> list[Any]:
        return [a[0]]

    return build(wat(W), "pyc", imports={"env": {"cb": cb, "cb2": cb, "hv": hv, "chk": chk}})


def run(mp: pytest.MonkeyPatch, target: str, bounds: str) -> tuple[Inst, Any]:
    """W in a new store of `target`; on the JIT with bounds mode `bounds` and the three switches."""
    mp.setenv("WABOROS_JIT_BOUNDS", bounds)
    for s in ("STRESS", "POISON", "VERIFY"):
        mp.setenv(f"WABOROS_JIT_GC_{s}", "1")
    store, box = StoreState(), list[Inst]()

    def cb(a: Any) -> list[Any]:  # wasm -> host -> wasm (-> host -> wasm: 3 deep)
        return box[0]("inner", a[0])

    def cb2(a: Any) -> list[Any]:  # the innermost call allocates, so it collects
        return box[0]("inner0", a[0])

    imports = {"env": {"cb": cb, "cb2": cb2, "hv": hv, "chk": chk}}
    inst = build(wat(W), target, imports=imports, store=store)
    box.append(inst)
    return inst, (_stacks(store) if target == JIT else None)


@pytest.mark.parametrize("bounds", BOUNDS)
@pytest.mark.parametrize("case", CASES)
def test_references_survive_every_place_they_live(
    monkeypatch: pytest.MonkeyPatch, case: str, bounds: str
) -> None:
    """t1 operands below an allocation, t2 callee parameters, t3a/b/c return_call, _indirect and
    _ref, t4 a global, t5 a table, t6 an exnref payload, t7 host to wasm nesting, t8 a non-final
    call_indirect, t9 v128 beside references, t10 array.new_fixed of 40, t10b array.new and
    array.new_elem, t11 churn, t12 block and loop parameters, t13 i31 and host words in
    an array and a field, t14 `ref.eq` identity of one object in a global, a table, a local and an
    array; t7 nests wasm, host, wasm, host, wasm; t10 and t10b keep their arrays across
    collections."""
    native(JIT)
    jit, st = run(monkeypatch, JIT, bounds)
    n = st.heap.collections
    want = oracle()(case)
    assert jit(case) == want
    assert st.heap.collections > n


def ref_call(i: Inst, name: str, *args: Any) -> list[Any]:  # Inst.__call__ takes integers only
    return i.inst.call(next(x for n, k, x in i.mod["exports"] if n == name and k == 0), list(args))


@pytest.mark.parametrize("bounds", BOUNDS)
def test_a_host_handle_survives_collections(monkeypatch: pytest.MonkeyPatch, bounds: str) -> None:
    """A GcRef held by the host while calls collect keeps its object and its identity."""
    native(JIT)
    jit, st = run(monkeypatch, JIT, bounds)
    pyc = oracle()
    r, p = jit("mkchain")[0], pyc("mkchain")[0]
    n = st.heap.collections
    for _ in range(4):
        jit("alloc")
        assert ref_call(jit, "id", r)[0] is r
    assert st.heap.collections >= n + 8
    got = ref_call(jit, "hsum", r)
    assert got == ref_call(pyc, "hsum", p) and got != [0]
