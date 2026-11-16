"""GC microbenchmarks (S0013 step 3b.4; BENCHMARKS.md, "GC (S0013 step 3b)"): ns per iteration of
four loops, each target alternated round by round, the best of `--rounds`.

    python tools/bench_gc.py [--targets jit-x86_64,pyc,wasmtime] [--rounds 5] [--scale 1.0]

* churn: `struct.new` of a two-field struct and a `struct.get`, the object dropped at once;
* list: build a linked list of structs, then walk it (per node, build and walk together);
* casts: `ref.test` to a final type and `ref.cast` to a non-final type at depth 3, on an anyref;
* arrays: `array.fill` of a 4 KiB i8 array and `array.copy` of a 512-element i64 array, per round
  trip (ns per byte-and-element operation pair).

Since step 5 the JIT heap collects, so the loops include their collections (BENCHMARKS.md, "GC
collector (S0013 step 5)"; `tools/bench_gc_collect.py` separates pause and mutator). Wasmtime
(optional) is wasmtime-py with GC on (its default collector).
Needs `wasm-tools` (a development tool) through tests/watcache.py."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.watcache import wat2wasm  # noqa: E402
from waboros.frontend.binary import parse  # noqa: E402
from waboros.frontend.validate import validate  # noqa: E402
from waboros.runtime import link  # noqa: E402
from waboros.runtime.store import StoreState  # noqa: E402
from waboros.targets import base  # noqa: E402

WAT = """(module
  (type $p (struct (field i32) (field i32)))
  (type $node (struct (field i32) (field (ref null $node))))
  (type $t0 (sub (struct))) (type $t1 (sub $t0 (struct (field i32))))
  (type $t2 (sub $t1 (struct (field i32) (field i32))))
  (type $t3 (sub $t2 (struct (field i32) (field i32) (field i32))))
  (type $b (array (mut i8))) (type $w (array (mut i64)))
  (func (export "churn") (param $n i32) (result i32) (local $s i32)
    (loop $l (local.set $s (i32.add (local.get $s)
        (struct.get $p 1 (struct.new $p (local.get $n) (local.get $s)))))
      (br_if $l (local.tee $n (i32.sub (local.get $n) (i32.const 1)))))
    (local.get $s))
  (func (export "list") (param $n i32) (result i32) (local $h (ref null $node)) (local $s i32)
    (loop $l (local.set $h (struct.new $node (local.get $n) (local.get $h)))
      (br_if $l (local.tee $n (i32.sub (local.get $n) (i32.const 1)))))
    (block $e (loop $w (br_if $e (ref.is_null (local.get $h)))
      (local.set $s (i32.add (local.get $s) (struct.get $node 0 (local.get $h))))
      (local.set $h (struct.get $node 1 (local.get $h))) (br $w)))
    (local.get $s))
  (func (export "casts") (param $n i32) (result i32) (local $x anyref) (local $s i32)
    (local.set $x (struct.new $t3 (i32.const 1) (i32.const 2) (i32.const 3)))
    (loop $l
      (local.set $s (i32.add (local.get $s) (i32.add (ref.test (ref $t3) (local.get $x))
        (struct.get $t1 0 (ref.cast (ref $t1) (local.get $x))))))
      (br_if $l (local.tee $n (i32.sub (local.get $n) (i32.const 1)))))
    (local.get $s))
  (func (export "arrays") (param $n i32) (result i32) (local $b (ref $b)) (local $w (ref $w))
    (local.set $b (array.new_default $b (i32.const 4096)))
    (local.set $w (array.new_default $w (i32.const 1024)))
    (loop $l (array.fill $b (local.get $b) (i32.const 0) (local.get $n) (i32.const 4096))
      (array.copy $w $w (local.get $w) (i32.const 0) (local.get $w) (i32.const 512) (i32.const 512))
      (br_if $l (local.tee $n (i32.sub (local.get $n) (i32.const 1)))))
    (array.get_u $b (local.get $b) (i32.const 7)))
)"""
N = {"churn": 1_000_000, "list": 200_000, "casts": 1_000_000, "arrays": 20_000}
SLOW = {"pyc": 50}  # pyc runs 1/50 of the iterations


def ours(name: str):  # type: ignore[no-untyped-def]
    mod = parse(wat2wasm(WAT))
    validate(mod)
    t = base.TARGETS[name]()
    inst = t.instantiate(t.compile(mod), link.resolve(mod, {}), StoreState())
    idx = {n: i for n, k, i in mod["exports"] if k == 0}
    return lambda f, n: inst.call(idx[f], [n])[0] & 0xFFFFFFFF


def wasmtime_runner():  # type: ignore[no-untyped-def]
    import wasmtime

    c = wasmtime.Config()
    c.wasm_gc = True
    c.wasm_function_references = True
    engine = wasmtime.Engine(c)
    store = wasmtime.Store(engine)
    inst = wasmtime.Instance(store, wasmtime.Module(engine, wat2wasm(WAT)), [])
    ex = inst.exports(store)
    return lambda f, n: ex[f](store, n) & 0xFFFFFFFF


def main() -> None:
    args = sys.argv[1:]
    targets = (
        args[args.index("--targets") + 1] if "--targets" in args else "jit-x86_64,pyc,wasmtime"
    ).split(",")
    rounds = int(args[args.index("--rounds") + 1]) if "--rounds" in args else 5
    scale = float(args[args.index("--scale") + 1]) if "--scale" in args else 1.0
    best: dict[tuple[str, str], float] = {}
    results: dict[tuple[str, str], int] = {}
    for _ in range(rounds):
        for t in targets:  # a fresh instance per round
            run = wasmtime_runner() if t == "wasmtime" else ours(t)
            for f, n in N.items():
                n = max(1, int(n * scale) // SLOW.get(t, 1))
                t0 = time.perf_counter()
                r = run(f, n)
                ns = (time.perf_counter() - t0) / n * 1e9
                best[t, f] = min(best.get((t, f), ns), ns)
                results.setdefault((f, str(n)), r)
                if results[f, str(n)] != r:
                    sys.exit(f"{t} {f}({n}) = {r}, another target gave {results[f, str(n)]}")
    print("ns per iteration (best of %d rounds, alternated)" % rounds)
    print("loop    " + "".join(f"{t:>13}" for t in targets))
    for f in N:
        print(f"{f:8}" + "".join(f"{best[t, f]:13.1f}" for t in targets))


if __name__ == "__main__":
    main()
