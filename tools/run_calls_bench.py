# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false
"""Calls benchmark (S0007, "Test plan"; BENCHMARKS.md): the cost of a wasm->wasm call.

    python tools/run_calls_bench.py [--targets jit-x86_64,wasmtime] [--rounds 5]

poc/bench/calls/calls.wasm: `leaf` calls a 2-argument leaf N times in a loop, `fib` is recursive
fib(27). Prints nanoseconds per call, median of alternated rounds; fails if a target's results
differ from the first target's."""

import sys
import time
from pathlib import Path
from typing import Any

from waboros.frontend import binary
from waboros.frontend.validate import validate
from waboros.runtime import link
from waboros.runtime.store import StoreState
from waboros.targets import base

WASM = Path(__file__).resolve().parents[1] / "poc/bench/calls/calls.wasm"
LEAF_N, FIB_N = 10_000_000, 27
CALLS = {"leaf": LEAF_N, "fib": 2 * 317_811 - 1}  # fib(27) makes 2 * fib(28) - 1 calls


def runner(target: str, wasm: bytes) -> Any:
    """Returns call(name, n) -> result (u32) on the target."""
    if target == "wasmtime":
        import wasmtime

        eng = wasmtime.Engine()
        st = wasmtime.Store(eng)
        ex = wasmtime.Instance(st, wasmtime.Module(eng, wasm), []).exports(st)
        return lambda name, n: ex[name](st, n) & 0xFFFFFFFF
    mod = binary.parse(wasm)
    validate(mod)
    tg = base.TARGETS[target]()
    inst = tg.instantiate(tg.compile(mod), link.resolve(mod, {}), StoreState())
    idx = {n: i for n, k, i in mod["exports"] or () if k == 0}
    return lambda name, n: inst.call(idx[name], [n])[0] & 0xFFFFFFFF


def main() -> None:
    arg = lambda k, d: sys.argv[sys.argv.index(k) + 1] if k in sys.argv else d  # noqa: E731
    targets = arg("--targets", "jit-x86_64,wasmtime").split(",")
    rounds = int(arg("--rounds", "5"))
    wasm = WASM.read_bytes()
    runs = {t: runner(t, wasm) for t in targets}
    got = {t: (c("leaf", 1000), c("fib", 20)) for t, c in runs.items()}
    if len(set(got.values())) != 1:
        sys.exit(f"results differ: {got}")
    times: dict[tuple[str, str], list[float]] = {}
    for _ in range(rounds):  # alternated: every target once per round
        for t, call in runs.items():
            for k, n in (("leaf", LEAF_N), ("fib", FIB_N)):
                t0 = time.perf_counter()
                call(k, n)
                times.setdefault((t, k), []).append((time.perf_counter() - t0) * 1e9 / CALLS[k])
    print(f"ns per call, median of {rounds} alternated rounds (min..max)")
    for t in targets:
        cols = [sorted(times[(t, k)]) for k in CALLS]
        print(f"{t:12} " + "   ".join(f"{k} {c[rounds // 2]:6.2f} ({c[0]:.2f}..{c[-1]:.2f})"
                                       for k, c in zip(CALLS, cols, strict=True)))  # fmt: skip


main()
