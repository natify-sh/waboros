# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
"""Live sets of the stack maps (S0013 step 7.2, D65; the plan's t1-t14): per site `refs` (index
4, every reference slot) and `live` (index 5, the reference locals live at the site plus the
operand mask). Compile only, so both targets run on any host."""

import bisect
from typing import Any

import pytest

from tests.jit.test_stackmaps import TARGETS, sites
from tests.spec.fetch import CACHE_DIR
from tests.watcache import wat2wasm
from waboros.frontend.binary import parse
from waboros.frontend.validate import validate
from waboros.targets import base
from waboros.targets.jit.common import gc, liveness, simd
from waboros.targets.jit.common.compiler import Compiled

K = "(func $k)"  # a callee without sites: every site below is the tested function's
E2 = "(param $a externref) (param $b externref)"
E4 = "(param $a externref) (param $b externref) (param $c i32) (param $x externref)"
SUB = "(type $f (sub (func))) (table 1 funcref)"


def maps(target: str, body: str, head: str = "") -> list[tuple[int, int]]:
    """(refs, live) of each site of `(module head $k (func body))`, in code order."""
    _, s = sites(target, f"(module {head} {K} (func {body}))")
    return [(x[4], x[5]) for x in s]


_T5 = f"""{E2} (param $n i32)
  loop $outer
    call $k local.get $a drop ref.null extern local.set $b
    loop $inner call $k local.get $n br_if $inner end
    local.get $b drop local.get $n br_if $outer
  end"""

# name: (fields before $k, the tested function, [(refs, live) per site]); slot = local number
CASES: dict[str, tuple[str, str, list[tuple[int, int]]]] = {
    "t1 straight": ("", f"{E2} call $k local.get $a drop call $k local.get $b drop call $k",
                    [(3, 3), (3, 2), (3, 0)]),
    # the tee kills $a at the first call; at the second its value is an operand (slot 1)
    "t2 tee": ("(func $g (param externref) (result externref) local.get 0)",
               "(param $a externref) (result externref) local.get $a call $g local.tee $a"
               " call $k drop local.get $a", [(1, 0), (3, 3)]),
    # t3: $a read only at the label, $b only on the fall-through, $x by the branch itself
    "t3 br_if": ("", f"{E4} block call $k local.get $c br_if 0 local.get $b drop return end"
                 " local.get $a drop", [(11, 3)]),
    "t3 br_on_null": ("", f"{E4} block call $k local.get $x br_on_null 0 drop local.get $b drop"
                      " return end local.get $a drop", [(11, 11)]),
    "t3 br_on_non_null": ("", f"{E4} block (result externref) call $k local.get $x"
                          " br_on_non_null 0 local.get $b drop return end drop local.get $a drop",
                          [(11, 11)]),
    "t4 br_table": ("", f"{E2} (param $c externref) (param $d externref) (param $i i32)"
                    " block block block call $k local.get $i br_table 0 1 2 end local.get $a drop"
                    " return end local.get $b drop return end local.get $c drop", [(15, 7)]),
    "t5 nested loops": ("", _T5, [(3, 1), (3, 3)]),  # $b written first; $a via both back edges
    **{f"t6 {t}": ("", f"{E2} call $k local.get $b drop call $k {t} local.get $a drop",
                   [(3, 2), (3, 0)]) for t in ["return", "unreachable", "return_call $k"]},
    # t7: in the body $b (normal path) and $a (the handler); after the try_table no H
    **{f"t7 {c}": ("(tag $e)", f"{E2} block $h {rt} try_table ({c} $h) call $k end call $k"
                   f" local.get $b drop return end {dr} local.get $a drop", [(3, 3), (3, 2)])
       for c, rt, dr in [("catch $e", "", ""), ("catch_ref $e", "(result exnref)", "drop"),
                         ("catch_all", "", ""), ("catch_all_ref", "(result exnref)", "drop")]},
    "t7 loop label": ("", "(param $a externref) loop $l local.get $a drop"
                      " try_table (catch_all $l) call $k end end", [(1, 1)]),
    "t7 throw": ("(tag $e)", f"{E2} block $h try_table (catch_all $h) throw $e end end"
                 " local.get $a drop", [(3, 1)]),
    # $a is read only after the pad's label (LESSONS 125: the validator's mask at throw_ref makes
    # it a site too, so this pins the set, not the rule)
    "t7 throw_ref": ("", "(param $a externref) (param $x exnref) block $h call $k"
                     " try_table (catch_all $h) local.get $x throw_ref end end local.get $a drop",
                     [(3, 3)]),
    "t8 enclosing": ("(tag $e)", "(param $a externref) block $h try_table (catch_all $h)"
                     " block $h2 try_table (catch $e $h2) call $k end end end return end"
                     " local.get $a drop", [(1, 1)]),
    # t9: $b read only in the else-arm; $x written in a then-arm and read after it (false path)
    "t9 if": ("", f"{E4} call $k local.get $c if local.get $a drop else local.get $b drop end"
              " call $k local.get $c if local.get $a drop ref.null extern local.set $x end"
              " local.get $x drop", [(11, 11), (11, 9)]),
    # t10: slots $v 0-1, $a 2, $w 3-4, $b 5
    "t10 v128": ("", "(param $v v128) (param $a externref) (local $w v128) (local $b externref)"
                 " call $k local.get $a drop call $k local.get $b drop", [(36, 36), (36, 32)]),
    "t11 br_on_cast": (SUB, f"{E2} (param $x funcref) (result funcref) block $l (result (ref $f))"
                       " local.get $x br_on_cast $l funcref (ref $f) local.get $b drop return end"
                       " local.get $a drop", [(3, 3)]),
    "t11 return_call_indirect": (SUB, f"{E2} i32.const 0 return_call_indirect (type $f)",
                                 [(3, 0)]),
    "t11 return_call_indirect H": (SUB, f"{E2} block $h try_table (catch_all $h) i32.const 0"
                                   " return_call_indirect (type $f) end end local.get $b drop",
                                   [(3, 2)]),
    "t11 struct.new": ("(type $s (struct (field i32)))", "(param $a anyref) (param $b anyref)"
                       " (result anyref) i32.const 1 struct.new $s drop local.get $a", [(3, 1)]),
    "t12 parameter": ("", "(param $p externref) call $k local.get $p drop call $k call $k",
                      [(1, 1), (1, 0), (1, 0)]),
    # $x written from the first handler before any read, then read only in an outer handler
    "t12 exnref": ("", "(local $x exnref) block $h1 (result exnref) try_table"
                   " (catch_all_ref $h1) call $k end ref.null exn end local.set $x block $out"
                   " try_table (catch_all $out) call $k end return end local.get $x throw_ref",
                   [(1, 0), (1, 1)]),
    "t13 no reference local": ("", "(result externref) ref.null extern call $k", [(1, 1)]),
}  # fmt: skip


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("case", sorted(CASES))
def test_exact_live_sets(target: str, case: str) -> None:
    head, body, want = CASES[case]
    assert maps(target, body, head) == want


def test_t5_pass_count() -> None:
    mod: Any = parse(wat2wasm(f"(module {K} (func {_T5}))"))
    validate(mod)
    out, passes = liveness.live(mod["insns"][1], {0: 1, 1: 2}, mod["refmasks"][1])
    assert sorted(out.values()) == [1, 3]
    assert passes == 3 <= 2 + 2  # the inner head grows in the second pass; loop depth 2


@pytest.mark.parametrize("target", TARGETS)
def test_t13_nolive(target: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _, body, want = CASES["t1 straight"]
    for value in ["all", "0,1", "0"]:  # $k is function 0, the tested one 1
        monkeypatch.setenv("WABOROS_JIT_NOLIVE", value)
        assert maps(target, body) == (want if value == "0" else [(3, 3)] * 3)


@pytest.mark.parametrize("target", TARGETS)
def test_t14_spec_cache(target: str) -> None:
    """Every map of the spec modules with masks: `live ⊆ refs` (decode checks it), and `live`
    equals `refs` above the function's locals (operands keep the mask, D65)."""
    files = sorted((CACHE_DIR / "json").rglob("*.wasm"))  # empty without the directory
    if not files:
        pytest.skip("no spec cache (tests/spec/fetch.py)")
    modules = differ = 0
    for f in files:
        try:
            mod: Any = parse(f.read_bytes())
            validate(mod)
            if not mod["refmasks"]:
                continue
            c = base.TARGETS[target]().compile(mod)
        except Exception:  # invalid, malformed or unsupported: the spec runner's business
            continue
        assert isinstance(c, Compiled) and c.stackmaps is not None
        modules += 1
        starts = [pos for pos, _ in c.funcs]  # the defined functions' code, in order
        for ret, (_, _, _, _, refs, live) in gc.decode(c.stackmaps).items():
            k = bisect.bisect_right(starts, ret) - 1  # a host thunk: after them, no map
            (ps, _), (locs, _) = mod["types"][mod["funcs"][k]], mod["codes"][k]
            nloc = sum(1 + (t == simd.V128) for t in [*ps, *locs])
            assert (refs ^ live) >> nloc == 0, (f.name, ret)
            differ += refs != live  # 21 sites of 106 modules (7.2)
    assert modules > 0 and differ > 0, (modules, differ)
