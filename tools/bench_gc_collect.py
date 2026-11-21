# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownParameterType=false, reportMissingParameterType=false
"""The GC collector's measurements (S0013 step 5.7; BENCHMARKS.md, "GC collector (S0013 step 5)"):
mutator time after collections (locality) and collection pause, separately, for three heaps of
about 10^6 live objects: a linked list walked, a binary tree traversed, a reference array scanned.

    python tools/bench_gc_collect.py --builds cheney=DIR,dfs=DIR [--rounds 7] [--bounds guard]

Each build is a source tree DIR (`DIR/src` goes first on PYTHONPATH of a child process pinned with
`taskset -c 2`), the builds alternated round by round; prints the median (min-max) over the rounds.
`--child` is the one-build measurement (JSON on stdout). Needs `wasm-tools` (tests/watcache)."""

import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

WAT = """(module
  (type $node (struct (field i32) (field (ref null $node))))
  (type $tn (struct (field i32) (field (ref null $tn)) (field (ref null $tn))))
  (type $p (struct (field i32) (field i32)))
  (type $ra (array (mut (ref null $p))))
  (global $L (mut (ref null $node)) (ref.null $node))
  (global $T (mut (ref null $tn)) (ref.null $tn))
  (global $A (mut (ref null $ra)) (ref.null $ra))
  (func (export "mklist") (param $n i32)
    (loop $l (global.set $L (struct.new $node (local.get $n) (global.get $L)))
      (br_if $l (local.tee $n (i32.sub (local.get $n) (i32.const 1))))))
  (func (export "walklist") (result i32) (local $h (ref null $node)) (local $s i32)
    (local.set $h (global.get $L))
    (block $e (loop $w (br_if $e (ref.is_null (local.get $h)))
      (local.set $s (i32.add (local.get $s) (struct.get $node 0 (local.get $h))))
      (local.set $h (struct.get $node 1 (local.get $h))) (br $w)))
    (local.get $s))
  (func $mk (param $d i32) (result (ref null $tn))
    (if (result (ref null $tn)) (i32.eqz (local.get $d)) (then (ref.null $tn))
      (else (struct.new $tn (local.get $d) (call $mk (i32.sub (local.get $d) (i32.const 1)))
                                           (call $mk (i32.sub (local.get $d) (i32.const 1)))))))
  (func (export "mktree") (param $d i32) (global.set $T (call $mk (local.get $d))))
  (func $sum (param $t (ref null $tn)) (result i32)
    (if (result i32) (ref.is_null (local.get $t)) (then (i32.const 0))
      (else (i32.add (i32.add (struct.get $tn 0 (local.get $t))
                              (call $sum (struct.get $tn 1 (local.get $t))))
                     (call $sum (struct.get $tn 2 (local.get $t)))))))
  (func (export "walktree") (result i32) (call $sum (global.get $T)))
  (func (export "mkarr") (param $n i32) (local $i i32)
    (global.set $A (array.new_default $ra (local.get $n)))
    (loop $l (array.set $ra (global.get $A) (local.get $i)
                (struct.new $p (local.get $i) (local.get $i)))
      (br_if $l (i32.lt_u (local.tee $i (i32.add (local.get $i) (i32.const 1))) (local.get $n)))))
  (func (export "scanarr") (result i32) (local $i i32) (local $s i32)
    (loop $l (local.set $s (i32.add (local.get $s)
                (struct.get $p 0 (array.get $ra (global.get $A) (local.get $i)))))
      (br_if $l (i32.lt_u (local.tee $i (i32.add (local.get $i) (i32.const 1)))
                          (array.len (global.get $A)))))
    (local.get $s))
  (func (export "garbage") (param $n i32) (local $s i32)
    (loop $l (local.set $s (i32.add (local.get $s)
        (struct.get $p 1 (struct.new $p (local.get $n) (local.get $s)))))
      (br_if $l (local.tee $n (i32.sub (local.get $n) (i32.const 1))))))
)"""
# (name, build export, argument, live objects, walk export)
LOADS = [
    ("list", "mklist", 10**6, 10**6, "walklist"),
    ("tree", "mktree", 20, 2**20 - 1, "walktree"),
    ("array", "mkarr", 10**6, 10**6, "scanarr"),
]


def child() -> None:
    from tests.watcache import wat2wasm
    from waboros.frontend.binary import parse
    from waboros.frontend.validate import validate
    from waboros.runtime import link
    from waboros.runtime.store import StoreState
    from waboros.targets import base
    from waboros.targets.jit.common import collect, instance

    t: dict[str, Any] = {"pause": 0.0, "coll": 0.0, "n": 0, "in": False, "make": []}
    make: Any = collect._collector  # pyright: ignore[reportPrivateUsage]
    real_collect, real_call, real_make = collect.collect, instance.Instance.call, make

    def timed_collect(inst, heap):
        t["in"], t0 = True, time.perf_counter()
        try:
            real_collect(inst, heap)
        finally:
            t["pause"] += time.perf_counter() - t0
            t["n"] += 1
            t["in"] = False

    def timed_call(self, *a, **k):
        t0 = time.perf_counter()
        try:
            return real_call(self, *a, **k)
        finally:
            if t["in"]:
                t["coll"] += time.perf_counter() - t0

    def timed_make(inst, heap):
        first, t0 = heap.collector is None, time.perf_counter()
        r = real_make(inst, heap)
        if first:
            t["make"].append(time.perf_counter() - t0)
        return r

    collect.collect, instance.Instance.call = timed_collect, timed_call
    collect._collector = timed_make  # pyright: ignore[reportPrivateUsage]
    mod: Any = parse(wat2wasm(WAT))
    validate(mod)
    idx = {n: i for n, k, i in mod["exports"] if k == 0}
    tgt: Any = base.TARGETS["jit-x86_64"]()
    comp = tgt.compile(mod)
    out: dict[str, float] = {}
    for name, mk, arg, live, walk in LOADS:
        inst = tgt.instantiate(comp, link.resolve(mod, {}), StoreState())

        def call(f: str, *a: int, inst: Any = inst) -> Any:
            return inst.call(idx[f], list(a))

        call(mk, arg)
        want = call(walk)
        heap = instance._stacks(inst.store).heap  # pyright: ignore[reportPrivateUsage]
        h0, n0 = heap.collections, t["n"]
        p0, c0 = t["pause"], t["coll"]
        while heap.collections < h0 + 6:  # garbage until six more collections of the live heap
            call("garbage", 10**6)
        k = t["n"] - n0
        out[f"{name} pause ms"] = (t["pause"] - p0) / k * 1e3
        out[f"{name} collector ms"] = (t["coll"] - c0) / k * 1e3
        walks = []
        for _ in range(5):
            t0 = time.perf_counter()
            assert call(walk) == want
            walks.append(time.perf_counter() - t0)
        out[f"{name} walk ns/obj"] = statistics.median(walks) / live * 1e9
    out["compile ms"] = (t["make"][0] - t["make"][1]) * 1e3  # the first store compiles too
    out["instantiate ms"] = statistics.median(t["make"][1:]) * 1e3
    print(json.dumps({"src": instance.__file__, **out}))


def main() -> None:
    if "--child" in sys.argv:
        return child()

    def arg(k: str, d: str) -> str:
        return sys.argv[sys.argv.index(k) + 1] if k in sys.argv else d

    builds = dict(b.split("=") for b in arg("--builds", "").split(","))
    rounds = int(arg("--rounds", "7"))
    res: dict[tuple[str, str], list[float]] = {}
    for _ in range(rounds):
        for name, d in builds.items():
            env = {
                **os.environ,
                "PYTHONPATH": f"{d}/src",
                "WABOROS_JIT_BOUNDS": arg("--bounds", "guard"),
            }
            run = subprocess.run(["taskset", "-c", "2", sys.executable, __file__, "--child"],
                                 env=env, capture_output=True, text=True, check=True)  # fmt: skip
            r = json.loads(run.stdout)
            assert r.pop("src").startswith(d), f"{name} ran {r}"  # the build that runs is DIR's
            for k, v in r.items():
                res.setdefault((k, name), []).append(v)
    print(f"median (min-max) of {rounds} alternated rounds, bounds {arg('--bounds', 'guard')}")
    for k in dict.fromkeys(k for k, _ in res):
        row = ""
        for n in builds:
            v = res[k, n]
            row += f"  {n}: {statistics.median(v):8.3f} ({min(v):.3f}-{max(v):.3f})"
        print(f"{k:22}{row}")


if __name__ == "__main__":
    main()
