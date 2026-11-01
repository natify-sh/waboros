# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
"""Tables, references, call_indirect and bulk memory on `jit-x86_64` (JIT step 3d). Trap codes
and results are compared with Wasmtime (the oracle) where the module stands alone; linking cases
(shared tables, host functions in tables, nested entries) check exact expectations."""

import importlib.util
import random
import subprocess
import sys
import textwrap
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import pytest

from tests.jit import need
from tests.jit.test_compiler import ROOT, Inst, build, outcome, trap_of, wat
from tests.jit.test_float import Oracle
from w2py.runtime import link
from w2py.runtime.store import StoreState
from w2py.targets.base import Raw, Trap, TrapCode
from w2py.targets.jit.common.compiler import Compiled
from w2py.targets.jit.common.context import NSTACK_LIMIT, VSTACK_LIMIT
from w2py.targets.jit.common.instance import FuncRef
from w2py.targets.jit.x86_64 import JitX86_64Target


@pytest.fixture(autouse=True)
def _jit() -> None:
    need(JitX86_64Target().probe() is None, "x86-64 executable memory")


def provider(i: Inst) -> link.Provider:
    return link.Provider(i.mod, i.inst)


def call(i: Inst, name: str, *args: object) -> list[Any]:
    """Arguments as they are (references are objects at the boundary)."""
    idx = next(x for n, k, x in i.mod["exports"] if n == name and k == 0)
    return i.inst.call(idx, args)  # pyright: ignore[reportArgumentType]


INDIRECT = """(module
  (type $ii (func (param i32) (result i32)))
  (type $v (func))
  (table $t 4 funcref)
  (elem (table $t) (i32.const 0) func $double $nop $double)
  (func $double (type $ii) (i32.add (local.get 0) (local.get 0)))
  (func $nop (type $v))
  (func (export "call") (param i32 i32) (result i32)
    (call_indirect $t (type $ii) (local.get 1) (local.get 0)))
  (func (export "deep") (param i32 i32) (result i32)
    (i32.add (i32.const 1) (call_indirect $t (type $ii)
      (i32.add (local.get 1) (i32.const 0)) (i32.add (local.get 0) (i32.const 0))))))"""


def test_call_indirect_traps_match_wasmtime() -> None:
    """In bounds and matching; out of bounds (index 4 and 2**32-1); null (index 3); wrong
    signature (index 1): the trap codes Wasmtime gives."""
    need(_has_wasmtime(), "wasmtime")
    wasm = wat(INDIRECT)
    jit, ref = build(wasm), Oracle(wasm)
    for name in ("call", "deep"):
        for i in (0, 1, 2, 3, 4, 0xFFFFFFFF, 0x80000000):
            assert outcome(jit, name, i, 21) == ref(name, i, 21), (name, i)
    assert outcome(jit, "call", 4, 0) is TrapCode.TABLE_OUT_OF_BOUNDS
    assert outcome(jit, "call", 3, 0) is TrapCode.INDIRECT_CALL_TO_NULL
    assert outcome(jit, "call", 1, 0) is TrapCode.BAD_SIGNATURE
    assert jit("deep", 2, 5) == [11]


def _has_wasmtime() -> bool:
    return importlib.util.find_spec("wasmtime") is not None


TABLE_OPS = """(module
  (type $ii (func (param i32) (result i32)))
  (table $t 3 10 funcref)
  (table $e 2 externref)
  (elem declare func $inc)
  (func $inc (type $ii) (i32.add (local.get 0) (i32.const 1)))
  (func (export "size") (result i32) (table.size $t))
  (func (export "grow") (param i32) (result i32) (table.grow $t (ref.func $inc) (local.get 0)))
  (func (export "set") (param i32) (table.set $t (local.get 0) (ref.func $inc)))
  (func (export "clear") (param i32) (table.set $t (local.get 0) (ref.null func)))
  (func (export "isnull") (param i32) (result i32) (ref.is_null (table.get $t (local.get 0))))
  (func (export "run") (param i32 i32) (result i32)
    (call_indirect $t (type $ii) (local.get 1) (local.get 0)))
  (func (export "fill") (param i32 i32) (table.fill $t (local.get 0) (ref.func $inc) (local.get 1)))
  (func (export "copy") (param i32 i32 i32)
    (table.copy $t $t (local.get 0) (local.get 1) (local.get 2)))
  (func (export "esize") (result i32) (table.size $e)))"""


def test_table_operations_match_wasmtime() -> None:
    """A random sequence of get/set/grow/fill/copy/call: the same results and traps."""
    need(_has_wasmtime(), "wasmtime")
    wasm = wat(TABLE_OPS)
    jit, ref = build(wasm), Oracle(wasm)
    rnd = random.Random(3)
    ops = {"size": 0, "grow": 1, "set": 1, "clear": 1, "isnull": 1, "run": 2, "fill": 2, "copy": 3}
    for step in range(600):
        name = rnd.choice(list(ops))
        args = [rnd.choice([0, 1, 2, 3, 5, 9, 10, 11, 0xFFFFFFFF]) for _ in range(ops[name])]
        assert outcome(jit, name, *args) == ref(name, *args), (step, name, args)
    assert jit("esize") == [2]


def test_externref_round_trip() -> None:
    """Host objects come back as themselves (globals, tables, parameters); null is None."""
    jit = build(
        wat("""(module
      (table $t 2 externref)
      (global $g (export "g") (mut externref) (ref.null extern))
      (func (export "id") (param externref) (result externref) (local.get 0))
      (func (export "keep") (param externref) (global.set $g (local.get 0))
        (table.set $t (i32.const 1) (local.get 0)))
      (func (export "back") (result externref externref)
        (table.get $t (i32.const 0)) (table.get $t (i32.const 1)))
      (func (export "isnull") (param externref) (result i32) (ref.is_null (local.get 0))))""")
    )
    a, b = object(), ["x"]
    assert call(jit, "id", a) == [a] and call(jit, "id", None) == [None]
    call(jit, "keep", b)
    assert call(jit, "back") == [None, b]
    assert jit.inst.global_get(0) is b
    assert call(jit, "isnull", None) == [1] and call(jit, "isnull", a) == [0]


def test_funcref_out_and_back_in() -> None:
    """A funcref returned to the host is an opaque object that works when passed back."""
    jit = build(
        wat("""(module
      (type $ii (func (param i32) (result i32)))
      (table $t 1 funcref)
      (elem declare func $sq)
      (func $sq (type $ii) (i32.mul (local.get 0) (local.get 0)))
      (func (export "get") (result funcref) (ref.func $sq))
      (func (export "run") (param funcref i32) (result i32)
        (table.set $t (i32.const 0) (local.get 0))
        (call_indirect $t (type $ii) (local.get 1) (i32.const 0))))""")
    )
    (f,) = call(jit, "get")
    assert isinstance(f, FuncRef) and call(jit, "get") == [f]
    assert call(jit, "run", f, 7) == [49]
    with pytest.raises(Trap) as e:
        call(jit, "run", None, 7)
    assert e.value.code is TrapCode.INDIRECT_CALL_TO_NULL


SHARED_A = """(module
  (type $ii (func (param i32) (result i32)))
  (memory 1)
  (data (i32.const 0) "\\0a")
  (table $t (export "t") 4 funcref)
  (elem (table $t) (i32.const 0) func $mine)
  (func $mine (type $ii) (i32.add (local.get 0) (i32.load8_u (i32.const 0))))
  (func (export "run") (param i32 i32) (result i32)
    ;; the callee may run on another instance's memory; ours is read again after the call
    (i32.add (call_indirect $t (type $ii) (local.get 1) (local.get 0)) (i32.load8_u (i32.const 0))))
  (func (export "rec") (param i32) (result i32)
    (call_indirect $t (type $ii) (local.get 0) (i32.const 2))))"""
SHARED_B = """(module
  (type $ii (func (param i32) (result i32)))
  (import "a" "t" (table $t 4 funcref))
  (memory 1)
  (data (i32.const 0) "\\64")
  (global $n (mut i32) (i32.const 0))
  (elem (table $t) (i32.const 1) func $theirs $down)
  (func $theirs (type $ii)
    (global.set $n (i32.add (global.get $n) (i32.const 1)))
    (i32.add (local.get 0) (i32.load8_u (i32.const 0))))
  (func $down (type $ii) (call $down (local.get 0)))
  (func (export "count") (result i32) (global.get $n)))"""


def test_shared_table_calls_switch_the_context() -> None:
    """B writes its functions into A's table; A calls them: they see B's memory and globals, A
    sees its own memory after the call. Recursion inside B's function, entered from A's code, ends
    in STACK_OVERFLOW: B's context carries the store's stack limits although the host never entered
    B (LESSONS 31). On a 256 KiB thread the native limit is the one that stops it (the value stack
    would allow ~64k frames, 1 MiB of native stack): without the limit the process would crash."""
    out: list[object] = []

    def scenario() -> None:
        try:
            store = StoreState()
            a = build(wat(SHARED_A), store=store)
            b = build(wat(SHARED_B), imports={"a": provider(a)}, store=store)
            assert a("run", 0, 1) == [1 + 10 + 10]  # A's own function
            assert a("run", 1, 1) == [1 + 100 + 10]  # B's, on B's memory
            ca, cb = (cast(Any, i.inst).ctx for i in (a, b))  # B not entered from the host yet
            assert ca[NSTACK_LIMIT] == cb[NSTACK_LIMIT] != 0
            assert ca[VSTACK_LIMIT] == cb[VSTACK_LIMIT] != 0
            assert trap_of(a, "rec", 5) is TrapCode.STACK_OVERFLOW
            assert a("run", 1, 2) == [2 + 100 + 10] and b("count") == [2]
            out.append("ok")
        except BaseException as e:  # reported in the main thread
            out.append(e)

    old = threading.stack_size(256 << 10)
    try:
        t = threading.Thread(target=scenario)
        t.start()
        t.join()
    finally:
        threading.stack_size(old)
    if out != ["ok"]:
        raise AssertionError(out)


def test_imported_table_and_function_of_another_instance() -> None:
    """A function exported by one JIT instance, imported by another and placed in its table, runs
    directly on the exporter's context."""
    store = StoreState()
    a = build(wat(SHARED_A.replace('(func $mine', '(export "mine" (func $mine)) (func $mine')),
              store=store)  # fmt: skip
    c = build(
        wat("""(module (type $ii (func (param i32) (result i32)))
      (import "a" "mine" (func $m (type $ii)))
      (memory 1) (data (i32.const 0) "\\03")
      (table $t 1 funcref) (elem (table $t) (i32.const 0) func $m)
      (func (export "go") (param i32) (result i32)
        (call_indirect $t (type $ii) (local.get 0) (i32.const 0))))"""),
        imports={"a": provider(a)},
        store=store,
    )
    assert c("go", 5) == [15]  # a's memory: 10


def test_host_function_in_a_table() -> None:
    """An imported host function placed in a table is called through the host path; its trap
    and its exception come out as they would from a direct call."""
    seen: list[Sequence[Raw]] = []

    def add(args: Sequence[Raw]) -> list[Raw]:
        seen.append(list(args))
        if args[0] == 13:
            raise Trap(TrapCode.UNREACHABLE)
        if args[0] == 14:
            raise ValueError("host")
        return [(args[0] + args[1]) & 0xFFFFFFFF]

    jit = build(
        wat("""(module
      (type $f (func (param i32 i32) (result i32)))
      (import "env" "add" (func $add (type $f)))
      (table 2 funcref) (elem (i32.const 1) func $add)
      (func (export "go") (param i32 i32 i32) (result i32)
        (call_indirect (type $f) (local.get 0) (local.get 1) (local.get 2))))"""),
        imports={"env": {"add": add}},
    )
    assert jit("go", 2, 3, 1) == [5] and seen == [[2, 3]]
    assert trap_of(jit, "go", 13, 0, 1) is TrapCode.UNREACHABLE
    with pytest.raises(ValueError, match="host"):
        jit("go", 14, 0, 1)
    assert trap_of(jit, "go", 2, 3, 0) is TrapCode.INDIRECT_CALL_TO_NULL
    assert jit("go", 7, 8, 1) == [15]


def test_table_grow_across_nested_entries() -> None:
    """A host call re-enters wasm, which grows the table (it moves) and fills the new element;
    the outer frame then sees the new size and calls the new element."""
    store = StoreState()
    box: dict[str, Inst] = {}

    def reenter(args: Sequence[Raw]) -> list[Raw]:
        return box["i"]("grow", 1000)

    jit = build(
        wat("""(module
      (type $ii (func (param i32) (result i32)))
      (import "env" "reenter" (func $re (result i32)))
      (table $t 1 funcref)
      (elem declare func $neg)
      (func $neg (type $ii) (i32.sub (i32.const 0) (local.get 0)))
      (func (export "grow") (param i32) (result i32)
        (table.grow $t (ref.func $neg) (local.get 0)))
      (func (export "outer") (result i32 i32 i32)
        (call $re) (table.size $t)
        (call_indirect $t (type $ii) (i32.const 9) (i32.const 1000))))"""),
        imports={"env": {"reenter": reenter}},
        store=store,
    )
    box["i"] = jit
    assert jit("outer") == [1, 1001, (-9) & 0xFFFFFFFF]
    assert jit("grow", 20_000_000) == [0xFFFFFFFF]  # the 10M element cap (D27)


def test_bulk_memory_matches_wasmtime() -> None:
    """memory.fill / memory.copy (overlapping both ways, every tail length) and memory.init /
    data.drop, by random cases against Wasmtime; the memory is compared after each."""
    need(_has_wasmtime(), "wasmtime")
    wasm = wat("""(module (memory 1)
      (data $d "abcdefghijklmnopqrstuvwxyz0123456789")
      (func (export "fill") (param i32 i32 i32)
        (memory.fill (local.get 0) (local.get 1) (local.get 2)))
      (func (export "copy") (param i32 i32 i32)
        (memory.copy (local.get 0) (local.get 1) (local.get 2)))
      (func (export "init") (param i32 i32 i32)
        (memory.init $d (local.get 0) (local.get 1) (local.get 2)))
      (func (export "drop") (data.drop $d))
      (func (export "sum") (param i32 i32) (result i64) (local i64)
        (block (loop (br_if 1 (i32.eqz (local.get 1)))
          (local.set 2 (i64.add (i64.mul (local.get 2) (i64.const 31)) (i64.load8_u (local.get 0))))
          (local.set 0 (i32.add (local.get 0) (i32.const 1)))
          (local.set 1 (i32.sub (local.get 1) (i32.const 1))) (br 0)))
        (local.get 2)))""")  # fmt: skip
    jit, ref = build(wasm), Oracle(wasm)
    rnd = random.Random(5)
    near = [0, 1, 7, 8, 9, 15, 16, 17, 100, 65535, 65536, 65520, 0xFFFFFFFF]
    for step in range(800):
        name = rnd.choice(["fill", "copy", "copy", "init"] + ["drop"] * (step == 700))
        args = [] if name == "drop" else [
            rnd.choice(near) if rnd.random() < 0.2 else rnd.randrange(200) for _ in range(3)
        ]  # fmt: skip
        assert outcome(jit, name, *args) == ref(name, *args), (step, name, args)
        assert jit("sum", 0, 256) == ref("sum", 0, 256), (step, name, args)
        assert jit("sum", 65280, 256) == ref("sum", 65280, 256), (step, name, args)


def test_operands_in_pinned_registers() -> None:
    """Eight hot i32 locals are pinned to rbx rbp rsi rdi r8-r11: table and bulk-memory operands
    come from rsi/rdi (which the copy loop uses as its temporary) and r8-r11, and every pinned
    local survives."""
    locs = " ".join(f"(local.set {i} (i32.add (local.get {i}) (i32.const 0)))" for i in range(8))
    jit = build(
        wat(f"""(module (memory 1)
      (type $ii (func (param i32) (result i32)))
      (table $t 8 funcref) (elem (i32.const 0) func $f0 $f1 $f2 $f3 $f4 $f5 $f6 $f7)
      {" ".join(f"(func $f{k} (type $ii) (i32.add (local.get 0) (i32.const {k * 100})))"
                for k in range(8))}
      (func (export "go") (param i32 i32 i32 i32 i32 i32 i32 i32) (result i32)
        (loop (if (i32.eqz (local.get 0)) (then {locs} {locs})))
        (memory.fill (local.get 2) (local.get 7) (local.get 3))
        (memory.copy (local.get 6) (local.get 2) (local.get 3))
        (table.copy (local.get 4) (local.get 5) (i32.const 1))
        (table.fill (local.get 0) (table.get (local.get 7)) (i32.const 1))
        (i32.add (i32.add (i32.add (local.get 0) (local.get 1))
            (i32.add (local.get 2) (local.get 3)))
          (i32.add (i32.add (local.get 4) (local.get 5))
            (i32.add (i32.load8_u (local.get 6)) (call_indirect (type $ii) (local.get 1)
              (local.get 7)))))))""")
    )  # fmt: skip
    # fill 10 bytes at 40 with 7, copy them to 80; t[3] = t[4]; t[2] = t[7]; call t[7] with 2
    assert jit("go", 2, 2, 40, 10, 3, 4, 80, 7) == [2 + 2 + 40 + 10 + 3 + 4 + 7 + 2 + 700]
    # t[2] is $f7 since the first call
    assert jit("go", 0, 3, 40, 10, 3, 4, 80, 2) == [0 + 3 + 40 + 10 + 3 + 4 + 2 + 3 + 700]


def test_isa_check_on_table_code(tmp_path: Path) -> None:
    """D24: call_indirect, the table operations, the bulk-memory loops and host thunks are
    x86-64-v1."""
    from tests.jit.test_encoder import OBJDUMP
    from w2py.frontend.binary import parse
    from w2py.frontend.validate import validate

    need(OBJDUMP, "GNU objdump")
    bulk = """(module (import "env" "h" (func $h (param i32) (result i32)))
      (memory 1) (data $d "xy") (table 2 funcref) (elem declare func $h)
      (func (param i32) (memory.fill (local.get 0) (local.get 0) (local.get 0))
        (memory.copy (local.get 0) (local.get 0) (local.get 0))
        (memory.init $d (local.get 0) (local.get 0) (local.get 0)) (data.drop $d)
        (table.set (local.get 0) (ref.func $h))))"""
    for text in (TABLE_OPS, bulk):
        mod = parse(wat(text))
        validate(mod)
        c = JitX86_64Target().compile(mod)
        assert isinstance(c, Compiled)
        blob = tmp_path / "code.bin"
        blob.write_bytes(c.code)
        cmd = [sys.executable, str(ROOT / "tools" / "isa_check.py"), str(blob)]
        p = subprocess.run(
            [*cmd, "--data", f"{c.data_start}:{len(c.code)}"], capture_output=True, text=True
        )
        assert p.returncode == 0, p.stdout + p.stderr
    assert c.thunks  # the host function's entry for a table element was checked too


# --------------------------------------------------------------------------- review fixes
# Each operand of each new table / bulk-memory instruction as a constant, a parameter (a local,
# pinned or not) and a computed value (the `tos` register): an operand in `tos` must survive the
# moves of the others (the table.set index was overwritten by its value).
_KINDS = ("const", "param", "comp")
_OPS: dict[str, tuple[str, int]] = {  # instruction template over operands {0} {1} ..., arity
    "tset": ("(table.set $t {0} {1})", 2),
    "tget": ("(drop (table.get $t {0}))", 1),
    "tfill": ("(table.fill $t {0} {1} {2})", 3),
    "tcopy": ("(table.copy $t $t {0} {1} {2})", 3),
    "tinit": ("(table.init $t $e {0} {1} {2})", 3),
    "mfill": ("(memory.fill {0} {1} {2})", 3),
    "mcopy": ("(memory.copy {0} {1} {2})", 3),
    "minit": ("(memory.init $d {0} {1} {2})", 3),
    "icall": ("(global.set $r (call_indirect $t (type $r) {0} {1}))", 2),
}
_REF = {"const": "(ref.null func)", "param": "(local.get 3)", "comp": "(ref.func $f6)"}


def _operand(op: str, pos: int, kind: str) -> str:
    if op in ("tset", "tfill") and pos == 1:
        return _REF[kind]
    j = pos if op != "icall" else 1 - pos  # call_indirect: the argument, then the index
    if kind == "const":
        return f"(i32.const {(1, 2, 1)[pos]})"
    if kind == "param":
        return f"(local.get {j})"
    return f"(i32.add (local.get {j}) (i32.const 0))"


def _combos_module() -> tuple[str, list[str]]:
    import itertools

    funcs, names = [], []
    for op, (tmpl, n) in _OPS.items():
        for combo in itertools.product(_KINDS, repeat=n):
            name = f"{op}-{'-'.join(combo)}"
            ops = [_operand(op, p, k) for p, k in enumerate(combo)]
            funcs.append(f'(func (export "{name}") (param i32 i32 i32) (local funcref)'
                         f" (local.set 3 (ref.func $f5)) {tmpl.format(*ops)})")  # fmt: skip
            names.append(name)
    fs = " ".join(f"(func $f{k} (type $r) (i32.add (local.get 0) (i32.const {k * 100})))"
                  for k in range(8))  # fmt: skip
    text = f"""(module (type $r (func (param i32) (result i32)))
      (memory 1) (data $d "0123456789") (global $r (export "r") (mut i32) (i32.const 0))
      (table $t 8 funcref) (elem (table $t) (i32.const 0) func $f0 $f1 $f2 $f3 $f4 $f5 $f6 $f7)
      (elem $e func $f3 $f5 $f7) {fs} {" ".join(funcs)}
      (func (export "probe") (param i32) (result i32)
        (if (result i32) (ref.is_null (table.get $t (local.get 0))) (then (i32.const -1))
          (else (call_indirect $t (type $r) (i32.const 0) (local.get 0)))))
      (func (export "byte") (param i32) (result i32) (i32.load8_u (local.get 0))))"""
    return text, names


def test_computed_operands_of_table_and_bulk_ops_match_wasmtime() -> None:
    need(_has_wasmtime(), "wasmtime")
    text, names = _combos_module()
    wasm = wat(text)
    for name in names:
        jit, ref = build(wasm), Oracle(wasm)  # fresh state per instruction shape
        for args in ((1, 2, 3), (5, 0, 2), (7, 7, 1), (0, 3, 9), (9, 1, 1)):
            assert outcome(jit, name, *args) == ref(name, *args), (name, args)
            state = [outcome(jit, "probe", i) for i in range(8)]
            assert state == [ref("probe", i) for i in range(8)], (name, args)
            assert [jit("byte", i) for i in range(12)] == [ref("byte", i) for i in range(12)]
            g: Any = ref.ex["r"]
            assert jit.inst.global_get(0) == g.value(ref.store) & 0xFFFFFFFF


def test_table_set_of_a_funcref_parameter_at_a_computed_index() -> None:
    """The reviewer's case: the index in `tos`, the value a funcref parameter (a slot)."""
    jit = build(
        wat("""(module (table $t 4 funcref) (elem declare func $f) (func $f)
      (func (export "go") (param i32 funcref) (result i32 i32)
        (table.set $t (i32.add (local.get 0) (i32.const 1)) (local.get 1))
        (ref.is_null (table.get $t (i32.const 0)))
        (ref.is_null (table.get $t (i32.add (local.get 0) (i32.const 1)))))
      (func (export "f") (result funcref) (ref.func $f)))""")
    )
    (f,) = call(jit, "f")
    assert call(jit, "go", 2, f) == [1, 0]


def test_segment_drops_without_memory_or_table() -> None:
    """data.drop of a passive segment in a module without memory, elem.drop without a table: the
    services exist (a zero libcall word would be a call to address 0); and every libcall word of
    a module without any of them is callable."""
    from w2py.targets.jit.common.context import LIBCALLS

    jit = build(
        wat("""(module (data "x") (elem func $f) (func $f)
      (func (export "d") (data.drop 0) (data.drop 0)) (func (export "e") (elem.drop 0)))""")
    )
    assert jit("d") == [] and jit("e") == []
    bare = build(wat('(module (func (export "n")))'))
    ctx, lay = cast(Any, bare.inst).ctx, cast(Any, bare.inst).layout
    assert all(ctx[lay.libcall(j)] for j in range(LIBCALLS))


def test_references_and_imports_of_another_store_are_refused() -> None:
    """A FuncRef or a table/function/global of another store would run on that store's context
    and value stack: refused at the boundary (as Wasmtime), not trapped inside."""
    s1, s2 = StoreState(), StoreState()
    a = build(wat(SHARED_A.replace("(func $mine", '(export "mine" (func $mine)) (func $mine')
                  .replace('(func (export "rec")', '(global (export "g") i32 (i32.const 1))'
                           ' (elem declare func $mine) (func (export "get") (result funcref)'
                           ' (ref.func $mine)) (func (export "rec")')), store=s1)  # fmt: skip
    b = build(
        wat("""(module (type $ii (func (param i32) (result i32))) (table 1 funcref)
      (func (export "run") (param funcref i32) (result i32)
        (table.set (i32.const 0) (local.get 0))
        (call_indirect (type $ii) (local.get 1) (i32.const 0))))"""),
        store=s2,
    )  # fmt: skip
    (f,) = call(a, "get")
    with pytest.raises(TypeError, match="another store|this store"):
        call(b, "run", f, 1)
    for imp in ('(import "a" "t" (table 4 funcref))', '(import "a" "g" (global i32))',
                '(import "a" "mine" (func (param i32) (result i32)))'):  # fmt: skip
        with pytest.raises(ValueError, match="another store"):
            build(wat(f"(module {imp})"), imports={"a": provider(a)}, store=s2)
    b1 = build(wat('(module (import "a" "t" (table 4 funcref)))'), imports={"a": provider(a)},
               store=s1)  # fmt: skip
    assert b1.inst is not None


def test_a_forged_funcref_is_refused() -> None:
    """Only a reference the store handed out crosses into it: a FuncRef made up by the host (any
    address, the right store) is refused at the boundary; before, call_indirect jumped through it
    (LESSONS 35)."""
    st = StoreState()
    i = build(wat("""(module (type $v (func)) (table 1 funcref) (elem declare func $f)
      (func $f) (func (export "get") (result funcref) (ref.func $f))
      (func (export "set") (param funcref) (table.set (i32.const 0) (local.get 0)))
      (func (export "run") (call_indirect (type $v) (i32.const 0))))"""), store=st)  # fmt: skip
    (real,) = call(i, "get")
    for addr in (0x1000, real.addr + 8):
        with pytest.raises(TypeError, match="this store"):
            call(i, "set", FuncRef(addr, real.store))
    with pytest.raises(TypeError, match="this store"):  # only the very object handed out
        call(i, "set", FuncRef(real.addr, real.store))

    class Evil(FuncRef):  # equality cannot be used to smuggle an address in
        def __eq__(self, other: object) -> bool:
            return True

        def __ne__(self, other: object) -> bool:
            return False

        __hash__ = FuncRef.__hash__

    with pytest.raises(TypeError, match="this store"):
        call(i, "set", Evil(0x1000, real.store))
    call(i, "set", real)
    assert call(i, "run") == []


def test_exit_with_a_daemon_thread_in_jit_code() -> None:
    """Interpreter exit must not unmap code or memory a daemon thread still runs (atexit=False on
    the finalizers); before, the process ended with SIGSEGV (review of step 6)."""
    src = textwrap.dedent(f"""
        import sys, threading, time
        sys.path.insert(0, {str(ROOT)!r})
        from tests.jit.test_compiler import build, wat
        i = build(wat('(module (memory 1) (func (export "spin") (local i32)'
                      ' (loop $l (local.set 0 (i32.load (i32.const 0))) (br $l))))'))
        threading.Thread(target=lambda: i("spin"), daemon=True).start()
        time.sleep(0.3)
    """)
    for _ in range(3):
        p = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True)
        assert p.returncode == 0, (p.returncode, p.stderr[-500:])


def test_type_ids_from_threads_are_distinct() -> None:
    """The canonical id registry is shared by every store: concurrent registrations of distinct
    types never share an id."""
    from w2py.targets.jit.common.instance import type_id

    got: list[list[int]] = [[] for _ in range(8)]
    start = threading.Barrier(8)

    def work(n: int) -> None:
        types = [([0x7F] * (5000 + 1000 * n + i), [0x7E]) for i in range(300)]
        start.wait()
        got[n] = [type_id(types, i) for i in range(len(types))]

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ids = [i for g in got for i in g]
    assert len(ids) == 2400 and len(set(ids)) == 2400
