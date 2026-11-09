# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false
"""SIMD benchmark (S0005, "Measurement plan"; BENCHMARKS.md, "SIMD benchmark"):

    python tools/run_simd_bench.py [--targets pyc,jit-x86_64,wasmtime] [--rounds 3] [--ops]

Runs every kernel of poc/bench/simd/kernels.{scalar,simd}.wasm on each target, prints microseconds
per call (ITERS iterations of the kernel's loop each) and fails on a checksum mismatch between the
targets of one build. `--ops` prints the SIMD instruction count and operators per kernel."""

import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

from waboros.frontend import binary, simd
from waboros.frontend.validate import validate
from waboros.runtime import link
from waboros.runtime.store import StoreState
from waboros.targets import base

DIR = Path(__file__).resolve().parents[1] / "poc/bench/simd"
KERNELS = ("addsat", "dot", "scan", "matmul", "swizzle", "nearest")
ITERS = {"pyc": 5, "jit-aarch64": 1}  # iterations per call; the default is 2000


def runner(target: str, wasm: bytes) -> Any:
    """Returns call(name, iters) -> checksum (u32) on the target, after init()."""
    if target == "wasmtime":
        import wasmtime

        eng = wasmtime.Engine()
        st = wasmtime.Store(eng)
        inst = wasmtime.Instance(st, wasmtime.Module(eng, wasm), [])
        ex = inst.exports(st)
        ex["init"](st)
        return lambda name, n: ex[name](st, n) & 0xFFFFFFFF
    mod = binary.parse(wasm)
    validate(mod)
    tg = base.TARGETS[target]()
    inst = tg.instantiate(tg.compile(mod), link.resolve(mod, {}), StoreState())
    idx = {n: i for n, k, i in mod["exports"] or () if k == 0}
    inst.call(idx["init"], [])
    return lambda name, n: inst.call(idx[name], [n])[0] & 0xFFFFFFFF


def ops(wasm: bytes) -> dict[str, Counter[str]]:
    mod = binary.parse(wasm)
    names = {i: n for n, k, i in mod["exports"] or () if k == 0}
    out: dict[str, Counter[str]] = {}
    for i, (_, body) in enumerate(mod["codes"]):
        c: Counter[str] = Counter()
        for ins in binary.decode(body):
            if ins[0] >= simd.BASE:
                c[simd.OPS[ins[0] - simd.BASE].name] += 1
        out[names.get(i, f"func{i}")] = c
    return out


def main() -> None:
    arg = lambda k, d: sys.argv[sys.argv.index(k) + 1] if k in sys.argv else d  # noqa: E731
    targets = arg("--targets", "pyc,jit-x86_64,wasmtime").split(",")
    rounds = int(arg("--rounds", "3"))
    wasms = {v: (DIR / f"kernels.{v}.wasm").read_bytes() for v in ("scalar", "simd")}
    if "--ops" in sys.argv:
        for v, w in wasms.items():
            for k, c in ops(w).items():
                print(f"{v:6} {k:8} {sum(c.values()):3} simd ops: {', '.join(sorted(c))}")
        return
    runs = {(t, v): runner(t, w) for t in targets for v, w in wasms.items()}
    times: dict[tuple[str, str, str], list[float]] = {}
    for _ in range(rounds):  # alternated: every configuration once per round
        for k in KERNELS:
            for (t, v), call in runs.items():
                n = ITERS.get(t, 2000)
                t0 = time.perf_counter()
                call(k, n)
                dt = (time.perf_counter() - t0) * 1e6 / n
                times.setdefault((t, v, k), []).append(dt)
    bad = 0
    for k in KERNELS:  # checksums depend on n and the buffers: fresh instances, one n for all
        for v, w in wasms.items():
            got = {t: runner(t, w)(k, 2) for t in targets}
            if len(set(got.values())) != 1:
                bad += 1
            print(f"checksum {v:6} {k:8} {'OK ' if len(set(got.values())) == 1 else 'BAD'} {got}")
    print(f"\nus per iteration, median of {rounds} alternated rounds")
    print(f"{'kernel':8} " + " ".join(f"{t + '/' + v:>20}" for t in targets for v in wasms))
    for k in KERNELS:
        row = [sorted(times[(t, v, k)])[rounds // 2] for t in targets for v in wasms]
        print(f"{k:8} " + " ".join(f"{x:20.2f}" for x in row))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
