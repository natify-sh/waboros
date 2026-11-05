# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
"""S0010: `pyc` leaves br_table dispatches without `_br` ladders (sink) and drops the `_br` stores
that are evidently dead. Every module runs under the default rules and under `BRRULES` empty (the
emission before S0010) and must give the same results (and Wasmtime's, where it is installed);
shape assertions check that the rule fired, or did not, where that is the point."""

import re
from pathlib import Path
from typing import Any

import pytest

from tests.watcache import wat2wasm
from waboros.frontend.binary import parse
from waboros.frontend.validate import validate
from waboros.targets.pyc import codegen, load

ROOT = Path(__file__).resolve().parents[1]
OFF: frozenset[str] = frozenset()


class rules:
    """`with rules(r, bt):` the codegen's rule set (and the br_table tree threshold) for a block"""

    def __init__(self, r: frozenset[str], bt: int | None = None) -> None:
        self.r, self.bt = r, bt

    def __enter__(self) -> None:
        self.old = codegen.BRRULES, codegen.BRTREE
        codegen.BRRULES = self.r
        if self.bt is not None:
            codegen.BRTREE = self.bt

    def __exit__(self, *_: object) -> None:
        codegen.BRRULES, codegen.BRTREE = self.old


def src(wasm: bytes, r: frozenset[str] = codegen.BRRULES, bt: int | None = None) -> str:
    mod = parse(wasm)
    validate(mod)
    with rules(r, bt):
        return codegen.generate(mod)


def outcomes(
    wasm: bytes, calls: list[tuple[str, tuple[int, ...]]], r: frozenset[str], bt: int | None
) -> list[Any]:
    with rules(r, bt):
        ex = load(wasm, {})
    out: list[Any] = []
    for name, args in calls:
        try:
            out.append(("ok", ex[name](*args)))
        except Exception as e:  # a trap: compare its code
            out.append(("trap", str(getattr(e, "code", type(e).__name__))))
    return out


def wasmtime_values(wasm: bytes, calls: list[tuple[str, tuple[int, ...]]]) -> list[Any] | None:
    try:
        import wasmtime
    except ImportError:
        return None
    eng = wasmtime.Engine()
    st = wasmtime.Store(eng)
    ex = wasmtime.Instance(st, wasmtime.Module(eng, wasm), []).exports(st)
    out: list[Any] = []
    for name, args in calls:
        try:
            v = ex[name](st, *args)  # pyright: ignore
            out.append(("ok", v & 0xFFFFFFFF if isinstance(v, int) else v))
        except wasmtime.Trap:
            out.append("trap")
    return out


def check(wat: str, calls: list[tuple[str, tuple[int, ...]]], bt: int | None = None) -> str:
    """same results with and without the rules (and as Wasmtime); returns the default source"""
    wasm = wat2wasm(wat)
    new, old = outcomes(wasm, calls, codegen.BRRULES, bt), outcomes(wasm, calls, OFF, bt)
    assert new == old
    ref = wasmtime_values(wasm, calls)
    if ref is not None:
        assert [o if o[0] == "ok" else "trap" for o in new] == ref
    return src(wasm, codegen.BRRULES, bt)


def func(source: str, name: str) -> str:
    m = re.search(r"^def " + name + r"\(.*?(?=^def |\Z)", source, re.S | re.M)
    assert m
    return m.group(0)


def depth(source: str) -> int:
    return max((len(ln) - len(ln.lstrip(" "))) // 4 for ln in source.split("\n") if ln.strip())


SWITCH = """(module (func (export "sw") (param $x i32) (result i32) (local $acc i32)
  (block $out
    (block $c5 (block $c4 (block $c3 (block $c2 (block $c1 (block $c0
      (br_table $c0 $c1 $c2 $c3 $c4 $c5 (local.get $x)))
      (local.set $acc (i32.const 10)) (br $out))
      (return (i32.const 11)))
      (unreachable))
      (local.set $acc (i32.const 3)))
      (local.set $acc (i32.add (local.get $acc) (i32.const 40))) (br $out))
    (local.set $acc (i32.const 55)))
  (local.get $acc)))"""


@pytest.mark.parametrize("bt", [None, 0])
def test_switch_cases_end_in_br_return_unreachable_and_fall_through(bt: int | None) -> None:
    s = check(SWITCH, [("sw", (x,)) for x in range(9)], bt)
    old = src(wat2wasm(SWITCH), OFF, bt)
    assert func(s, "f0").count("_br") < func(old, "f0").count("_br")


LOOP = """(module (func (export "lp") (param $n i32) (result i32) (local $i i32) (local $acc i32)
  (block $done
   (loop $top
    (br_if $done (i32.ge_u (local.get $i) (local.get $n)))
    (local.set $i (i32.add (local.get $i) (i32.const 1)))
    (block $next
      (block $c3 (block $c2 (block $c1 (block $c0
        (br_table $c0 $c1 $c2 $c3 $top (i32.rem_u (local.get $i) (i32.const 6))))
        (local.set $acc (i32.add (local.get $acc) (i32.const 1))) (br $next))
        (local.set $acc (i32.mul (local.get $acc) (i32.const 3))) (br $top))
        (br_if $done (i32.gt_u (local.get $acc) (i32.const 100000)))
        (local.set $acc (i32.add (local.get $acc) (i32.const 7))) (br $next))
        (local.set $acc (i32.xor (local.get $acc) (i32.const 5))))
    (local.set $acc (i32.add (local.get $acc) (local.get $i)))
    (br $top)))
  (local.get $acc)))"""


@pytest.mark.parametrize("bt", [None, 0])
def test_switch_in_a_loop_with_continue_and_break(bt: int | None) -> None:
    check(LOOP, [("lp", (n,)) for n in (0, 1, 5, 6, 13, 40, 200)], bt)


# a case block that is also reached by a branch that is not a br_table leaf: its region stays
ARRIVAL = """(module (func (export "ar") (param $x i32) (param $y i32) (result i32) (local $acc i32)
 (block $out
  (block $c1 (block $c0
     (br_if $c0 (local.get $y))
     (local.set $acc (i32.const 1))
     (br_table $c0 $c1 (local.get $x)))
   (local.set $acc (i32.add (local.get $acc) (i32.const 10))) (br $out))
  (local.set $acc (i32.add (local.get $acc) (i32.const 20))))
 (local.get $acc)))"""


def test_case_block_with_another_arrival() -> None:
    check(ARRIVAL, [("ar", (x, y)) for x in range(3) for y in range(2)])


# a region that falls through with 0 while an unsunk region lies between the tree and it
FALL = """(module (func (export "ft") (param $x i32) (param $y i32) (result i32) (local $acc i32)
 (block $c2 (block $c1 (block $c0
     (br_if $c0 (local.get $y))
     (br_table $c0 $c1 $c2 (local.get $x)))
   (local.set $acc (i32.add (local.get $acc) (i32.const 1))) (br $c2))
   (local.set $acc (i32.add (local.get $acc) (i32.const 10))))
 (i32.add (local.get $acc) (i32.const 100))))"""


def test_fall_through_region_with_an_unsunk_region_between() -> None:
    check(FALL, [("ft", (x, y)) for x in range(4) for y in range(2)])


# a leaf that needs `break` (the br_table is in a loop inside the case block): no sink; the regions
# use `continue` of the outer loop, which would restart the inner one if they were moved
LOOPLEAF = """(module (func (export "lb") (param $n i32) (result i32)
 (local $i i32) (local $acc i32) (local $k i32)
 (block $done
  (loop $outer
   (br_if $done (i32.ge_u (local.get $i) (local.get $n)))
   (local.set $i (i32.add (local.get $i) (i32.const 1)))
   (block $c1 (block $c0
     (local.set $k (i32.const 0))
     (loop $inner
       (local.set $k (i32.add (local.get $k) (i32.const 1)))
       (br_if $inner (i32.lt_u (local.get $k) (i32.const 3)))
       (br_table $c0 $c1 (i32.and (i32.add (local.get $i) (local.get $k)) (i32.const 1)))))
     (local.set $acc (i32.add (local.get $acc) (i32.const 100)))
     (br_if $done (i32.gt_u (local.get $acc) (i32.const 5000)))
     (br $outer))
   (local.set $acc (i32.add (local.get $acc) (local.get $k)))
   (br $outer)))
 (local.get $acc)))"""


def test_leaf_inside_another_python_loop_is_not_sunk() -> None:
    check(LOOPLEAF, [("lb", (n,)) for n in (0, 1, 2, 7, 30)])


# repeated targets (tree lowering, copies) and a region holding a nested br_table
COPY = """(module (func (export "cp") (param $x i32) (param $y i32) (result i32) (local $acc i32)
 (block $out
  (block $c2
   (block $c1 (block $c0
      (br_table $c0 $c1 $c0 $c1 $c0 $c2 (local.get $x)))
    (local.set $acc (i32.const 1))
    (br_table $c1 $c2 (local.get $y)))
   (local.set $acc (i32.add (local.get $acc) (i32.const 10)))
   (br $out))
  (local.set $acc (i32.add (local.get $acc) (i32.const 100))))
 (local.get $acc)))"""


def test_copied_regions_and_nested_br_table() -> None:
    check(COPY, [("cp", (x, y)) for x in range(7) for y in range(3)])


MULTI = """(module (func (export "mv") (param $x i32) (result i32) (local $acc i32)
 (block $out (result i32)
  (block $c1 (param i32) (result i32 i32)
   (block $c0 (param i32) (result i32 i32)
     (i32.const 5) (local.get $x) (br_table $c0 $c1 1 (local.get $x)))
   (i32.add) (i32.const 3) (i32.mul) (br $out))
  (i32.sub))
 (local.set $acc) (i32.add (local.get $acc) (i32.const 1))))"""


def test_blocks_with_params_and_results() -> None:
    check(MULTI.replace("(block $c1 (param i32)", "(local.get $x) (block $c1 (param i32)"),
          [("mv", (x,)) for x in range(5)])  # fmt: skip


TWOLOOPS = """(module (func (export "tl") (param $n i32) (result i32)
 (local $i i32) (local $j i32) (local $acc i32)
 (block $out
  (loop $a
   (local.set $j (i32.const 0))
   (loop $b
    (block $c1 (block $c0
      (br_table $c0 $c1 $out (i32.rem_u (i32.add (local.get $i) (local.get $j)) (i32.const 7))))
     (local.set $acc (i32.add (local.get $acc) (i32.const 1)))
     (local.set $j (i32.add (local.get $j) (i32.const 1)))
     (br_if $b (i32.lt_u (local.get $j) (i32.const 4))))
    (local.set $i (i32.add (local.get $i) (i32.const 1)))
    (br_if $a (i32.lt_u (local.get $i) (local.get $n))))))
 (local.get $acc)))"""


def test_escape_through_two_python_loops() -> None:
    check(TWOLOOPS, [("tl", (n,)) for n in (1, 3, 9)])


TAIL = """(module
 (func $g (param i32) (result i32) (i32.mul (local.get 0) (i32.const 2)))
 (func (export "tc") (param $x i32) (result i32)
  (block $c1 (block $c0 (br_table $c0 $c1 (local.get $x)))
   (return_call $g (i32.const 21)))
  (i32.const 7)))"""


def test_return_call_in_a_moved_region() -> None:
    check(TAIL, [("tc", (x,)) for x in range(3)])


# dse: `_br = out` stores reach $out's end with no test between: removed (no `_br` left); and a
# store that a later `if not _br:` reads: kept
def test_dead_stores_removed_and_live_ones_kept() -> None:
    wat = SWITCH.replace("(unreachable)", "(br $out)").replace(
        "(return (i32.const 11))", "(br $out)"
    )
    wat = wat.replace(
        "(local.set $acc (i32.const 3)))", "(local.set $acc (i32.const 3)) (br $out))"
    )
    s = check(wat, [("sw", (x,)) for x in range(9)])
    assert "_br" not in func(s, "f0")
    live = """(module (func (export "lv") (param $x i32) (result i32) (local $acc i32)
     (block $a (block $b (br_if $a (local.get $x)) (local.set $acc (i32.const 5)))
       (local.set $acc (i32.add (local.get $acc) (i32.const 1))))
     (local.get $acc)))"""
    s = check(live, [("lv", (x,)) for x in range(3)])
    assert "_br = " in func(s, "f0")


def deep(n: int, r: int) -> str:
    """n br_if nests (indentation) above a br_table whose first case region has r nested ifs"""
    brifs = "".join(f"(br_if $exit (i32.eq (local.get $y) (i32.const {k + 2})))" for k in range(n))
    ifs = "(local.set $acc (i32.add (local.get $acc) (i32.const 3)))"
    for k in range(r):
        ifs = f"(if (i32.ne (local.get $x) (i32.const {k + 50})) (then {ifs}))"
    return """(module (func (export "dp") (param $x i32) (param $y i32) (result i32)
 (local $acc i32)
 (block $exit
  (block $P
   (block $T1 (block $T0 BRIFS (br_table $T0 $T1 $P (local.get $x)))
    IFS (br $P))
   (local.set $acc (i32.const 7)))
  (local.set $acc (i32.add (local.get $acc) (i32.const 1000))))
 (local.get $acc)))""".replace("BRIFS", brifs).replace("IFS", ifs)


def test_sink_stops_at_the_indentation_limit() -> None:
    """a sink that would put a line past SINK_MAXIND is not made; at the limit it is; both compile
    and run (LESSONS 22: CPython stops at 100 levels)"""
    calls = [("dp", (x, y)) for x in range(4) for y in (0, 1, 2, 5)]
    seen: set[str] = set()
    for n in range(70, 95):
        wat = deep(n, 6)
        s = check(wat, calls)
        sunk = func(s, "f0").count("_br") < func(src(wat2wasm(wat), OFF), "f0").count("_br")
        d = depth(s)
        assert d <= max(codegen.SINK_MAXIND, depth(src(wat2wasm(wat), OFF)))
        if sunk:
            assert d <= codegen.SINK_MAXIND
            seen.add("sunk" if d < codegen.SINK_MAXIND else "at")
        else:
            seen.add("kept")
    assert seen == {"sunk", "at", "kept"}


def loop_nesting(source: str) -> int:
    best, stack = 0, list[int]()
    for ln in source.split("\n"):
        if not ln.strip():
            continue
        ind = len(ln) - len(ln.lstrip(" "))
        while stack and stack[-1] >= ind:
            stack.pop()
        if ln.lstrip().startswith("while "):
            stack.append(ind)
            best = max(best, len(stack))
    return best


MODULES = ["poc/in/core.wasm", "poc/bench/switch/vm.wasm"] + [
    f"poc/bench/corpus/{m}.wasm" for m in ("hash", "mono", "zip")
]


@pytest.mark.parametrize("path", MODULES)
def test_no_new_depth_or_loop_nesting_and_functions_without_br_unchanged(path: str) -> None:
    wasm = (ROOT / path).read_bytes()
    new, old = src(wasm), src(wasm, OFF)
    assert depth(new) <= depth(old)
    assert loop_nesting(new) == loop_nesting(old)  # sink never moves code into another loop
    fo = re.split(r"^(?=def )", old, flags=re.M)
    fn = re.split(r"^(?=def )", new, flags=re.M)
    assert len(fo) == len(fn)
    for a, b in zip(fo, fn, strict=True):
        if "_br" not in a:
            assert a == b


def test_coremark_and_switch_vm_shape() -> None:
    core = src((ROOT / "poc/in/core.wasm").read_bytes())
    assert core.count("_br") <= 538  # S0010 step 1 (bropt.py, sink only with its dse: 517)
    tests = set(re.findall(r"_br == (\d+)", func(core, "f11")))
    assert len(tests) <= 2  # the loop exits after the state machine; no case is behind a test
    assert "_br" not in src((ROOT / "poc/bench/switch/vm.wasm").read_bytes())


def test_region_without_a_guard_at_the_end_of_a_loop() -> None:
    """T's region starts where its guard would be (no guard, no `if not _br:`): found by the
    spec suite"""
    wat = """(module (func (export "f") (param i32) (result i32)
      (loop $l (result i32) (block $b (br_table $l $b (local.get 0))) (i32.const 0))))"""
    s = check(wat, [("f", (1,)), ("f", (5,))])
    assert "_br" not in s


# review of step 1: an escape from a python loop inside T to the loop P around T is routed by
# propagate(T) (`if _br: ... continue`); sinking T's region must keep that routing
ROUTE = """(module (func (export "f") (param $n i32) (result i32) (local $i i32) (local $acc i32)
 (block $done
  (loop $P
   (local.set $i (i32.add (local.get $i) (i32.const 1)))
   (br_if $done (i32.gt_u (local.get $i) (local.get $n)))
   (block $T
    (loop $L
     (br_if $P (i32.eqz (i32.and (local.get $i) (i32.const 1))))
     (br_if $L (i32.gt_u (local.get $i) (i32.const 1000))))
    (br_table $T (local.get $i)))
   (local.set $acc (i32.add (local.get $acc) (i32.const 100)))
   (br $P)))
 (i32.add (local.get $acc) (local.get $i))))"""


@pytest.mark.parametrize("bt", [None, 0])
def test_escape_routed_through_a_sunk_case_block_in_a_loop(bt: int | None) -> None:
    s = check(ROUTE, [("f", (n,)) for n in range(8)], bt)
    assert "if _br" in func(s, "f0")  # the routing stays
    assert s != src(wat2wasm(ROUTE), OFF, bt)  # and the region moved


# review of step 1: a leaf inside a moved region whose target encloses P: when that region is sunk
# later, the leaf has the depth of its new place
NESTED = """(module (func (export "f") (param i32 i32) (result i32) (local i32)
 (block $Q
  (block $T2
   (block $P
    (block $T
     (br_table $T $Q $P $Q $P $Q (local.get 0)))
    (local.set 2 (i32.const 1))
    (br_table $T2 $Q (local.get 1)))
   (local.set 2 (i32.const 2))
   (br $Q))
  (return (i32.add (local.get 2) (i32.const 10))))
 (local.get 2)))"""


NESTED_RET = """(module (func (export "f") (param i32 i32) (result i32) (local i32)
 (block $Q
  (block $P2
   (block $P
    (block $T
     (br_table $T $Q $P $Q $P $Q (local.get 0)))
    (local.set 2 (i32.const 1))
    (br_table $P $Q (local.get 1)))
   (return (i32.add (local.get 2) (i32.const 10))))
  (local.set 2 (i32.const 99)))
 (local.get 2)))"""


@pytest.mark.parametrize("wat", [NESTED, NESTED_RET])
def test_leaf_moved_with_its_region_keeps_its_depth(wat: str) -> None:
    s = check(wat, [("f", (x, y)) for x in range(7) for y in range(3)])
    assert s.count("_br") < src(wat2wasm(wat), OFF).count("_br")


# second review of step 1: T emits no line (its br_table has only depth-0 targets and a plain
# index), the region is empty, and T sits in a nest of P whose only line was `pass`: the cut must
# not leave that suite empty
EMPTY_SUITE = {
    "else": """(module (func (export "f") (param $x i32) (param $c i32) (result i32)
 (block $X
  (block $P
   (br_if $X (local.get $c))
   (block $T (br_table $T (local.get $x))))
  (return (i32.const 1)))
 (i32.const 2)))""",
    "if_not_br": """(module (func (export "f") (param $x i32) (param $c i32) (result i32)
 (block $X
  (block $P
   (block $A (br_if $X (local.get $c)) (br_if $A (local.get $x)))
   (block $T (br_table $T (local.get $x))))
  (return (i32.const 1)))
 (i32.const 2)))""",
    "if_arm": """(module (func (export "f") (param $x i32) (param $c i32) (result i32)
 (block $X
  (if (local.get $c) (then
   (block $P
    (br_if $X (local.get $x))
    (block $T (br_table $T (local.get $x)))))))
 (i32.const 2)))""",
}


@pytest.mark.parametrize("name", sorted(EMPTY_SUITE))
@pytest.mark.parametrize("bt", [None, 0])
def test_cut_never_leaves_a_suite_empty(name: str, bt: int | None) -> None:
    check(EMPTY_SUITE[name], [("f", (x, c)) for x in range(3) for c in range(2)], bt)
