# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false
"""Code placement on aarch64 (ROADMAP "Code placement"; BENCHMARKS.md, "Code placement on aarch64"):

    python tools/placement_sweep.py [--target jit-aarch64] [--rounds 5] [--pads 0:128:4]
        [--aligns 16,32,64] [--align-pads 0,20,40,60] [--csv F] [--md F] [--quick]

A configuration is (pad, align): WABOROS_JIT_PAD bytes of `nop` before the first function and loop
headers aligned to WABOROS_JIT_LOOP_ALIGN bytes with `nop` (both read by the aarch64 backend only;
the code area is page-aligned, so offsets are absolute). Every round runs every configuration once,
in a shuffled order, in this process: CoreMark (it/s; its millis() runs SCALE times fast, so a run
takes about 3 s, and the printed rate is scaled back), the scalar SIMD-kernel build (us per
iteration, median of 5 calls) and the corpus (us per call; `corpus` = their geomean). Values are
medians over the rounds. Fails on a CoreMark run that does not validate, a kernel checksum that
differs between configurations, a corpus result that differs from its reference, or a pad that
does not show in the code size. `--quick`: few iterations, for a correctness run under QEMU."""

import argparse
import csv
import ctypes
import gc
import math
import os
import random
import re
import statistics
import sys
import time
from typing import Any

import run_corpus as corpus
import run_simd_bench as kern
from run_coremark_jit import valid

from waboros.frontend.binary import parse
from waboros.frontend.validate import validate
from waboros.runtime import link
from waboros.runtime.store import StoreState
from waboros.targets import base


def coremark(target: str, wasm: bytes, scale: int) -> tuple[float, int]:
    """(it/s, code bytes) of one CoreMark run on a fresh instance."""
    mod = parse(wasm)
    validate(mod)
    tg = base.TARGETS[target]()
    out: list[str] = []
    box: dict[str, Any] = {}
    t0 = time.perf_counter()

    def millis(args: Any) -> list[int]:
        return [int((time.perf_counter() - t0) * 1000 * scale) & 0xFFFFFFFF]

    def pr(args: Any) -> list[int]:
        out.append(ctypes.string_at(box["inst"].memory.res.addr + args[0], args[1]).decode())
        return []

    c = tg.compile(mod)
    ext = link.resolve(mod, {"wiring": {"millis": millis, "print": pr}})
    box["inst"] = inst = tg.instantiate(c, ext, StoreState())
    inst.call(next(i for n, k, i in mod["exports"] if n == "setup" and k == 0), [])
    text = "".join(out)
    m = re.search(r"Iterations/Sec\s*:\s*([\d.]+)", text)
    if not valid(text) or not m:
        sys.exit("CoreMark did not validate:\n" + text)
    box.clear()
    return float(m.group(1)) * scale, len(c.code)  # type: ignore[attr-defined]


def ints(s: str) -> list[int]:
    return list(range(*map(int, s.split(":")))) if ":" in s else [int(x) for x in s.split(",") if x]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="jit-aarch64")
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--pads", default="0:128:4")
    ap.add_argument("--aligns", default="16,32,64")
    ap.add_argument("--align-pads", default="0,20,40,60")
    ap.add_argument("--csv", default="placement.csv")
    ap.add_argument("--md", default="placement.md")
    ap.add_argument("--quick", action="store_true")
    o = ap.parse_args()
    scale, iters, reps = (4, 2000, 20)
    if o.quick:  # timings meaningless; give small --pads/--aligns/--align-pads too
        scale, iters, reps = 100, 2, 1
    pads, aligns, apads = ints(o.pads), ints(o.aligns), ints(o.align_pads)
    assert all(p % 4 == 0 for p in pads + apads) and 0 in pads, "pads: multiples of 4, with 0"
    configs = list(
        dict.fromkeys([(p, 0) for p in pads + apads] + [(p, a) for a in aligns for p in apads])
    )
    root = kern.DIR.parents[2]
    core = (root / "poc/in/core.wasm").read_bytes()
    scalar = (kern.DIR / "kernels.scalar.wasm").read_bytes()
    libs = {lib: (corpus.DIR / f"{lib}.wasm").read_bytes() for lib in ("mono", "hash", "zip")}
    cases = corpus.cases()
    vals: dict[tuple[int, int, str], list[float]] = {}
    size: dict[tuple[int, int], int] = {}
    sums: dict[str, int] = {}
    rows: list[tuple[int, int, int, str, float]] = []
    t_start = time.time()
    for r in range(o.rounds):
        order = configs[:]
        random.Random(r).shuffle(order)
        for pad, align in order:
            os.environ["WABOROS_JIT_PAD"], os.environ["WABOROS_JIT_LOOP_ALIGN"] = (
                str(pad),
                str(align),
            )
            got: dict[str, float] = {}
            got["coremark"], size[(pad, align)] = coremark(o.target, core, scale)
            call = kern.runner(o.target, scalar)
            for k in kern.KERNELS:
                ts: list[float] = []
                for _ in range(5):
                    t = time.perf_counter()
                    sum_ = call(k, iters)
                    ts.append((time.perf_counter() - t) * 1e6 / iters)
                if sums.setdefault(k, sum_) != sum_:
                    sys.exit(f"kernel {k}: checksum {sum_:#x} at pad {pad} align {align}")
                got[k] = statistics.median(ts)
            calls = {lib: corpus.runner(o.target, w) for lib, w in libs.items()}
            for c in cases:
                res, dt = corpus.run_case(calls[c[0]], c, reps)
                if res != c[5]:
                    sys.exit(f"corpus {c[1]}: wrong result at pad {pad} align {align}")
                got["corpus: " + c[1]] = dt
            got["corpus"] = math.exp(
                statistics.fmean(math.log(got["corpus: " + c[1]]) for c in cases)
            )
            for b, v in got.items():
                vals.setdefault((pad, align, b), []).append(v)
                rows.append((r, pad, align, b, v))
            calls.clear()
            gc.collect()  # the instances (CoreMark's closes a cycle) and their reservations
            cm, sz, dt = got["coremark"], size[(pad, align)], time.time() - t_start
            print(
                f"round {r} pad {pad} align {align}: {cm:.0f} it/s, {sz} B, {dt:.0f} s", flush=True
            )
    for p in pads:
        if size[(p, 0)] - size[(0, 0)] != p:
            sys.exit(f"pad {p} did not move the code (is WABOROS_JIT_PAD read by {o.target}?)")
    with open(o.csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["round", "pad", "align", "bench", "value"])
        w.writerows(rows)
    benches = ["coremark", *kern.KERNELS, "corpus"]
    med = {k: statistics.median(v) for k, v in vals.items()}
    gain = lambda b, x, y: (y / x if b == "coremark" else x / y) - 1  # noqa: E731  # y over x
    md = [f"## Code placement, {o.target}, bounds `{os.environ.get('WABOROS_JIT_BOUNDS', 'auto')}`",
          f"{o.rounds} rounds, {len(configs)} configurations, {time.time() - t_start:.0f} s. "
          "CoreMark it/s; kernels us per iteration; corpus geomean of us per call.", "",
          f"### Pad sweep ({pads[0]}..{pads[-1]} bytes)", "",
          "| bench | median | range | best (pad) | worst (pad) | spread between rounds |",
          "|---|---|---|---|---|---|"]  # fmt: skip
    for b in benches:
        ms = {p: med[(p, 0, b)] for p in pads}
        best = max(ms, key=lambda p: gain(b, 1, ms[p]))
        worst = min(ms, key=lambda p: gain(b, 1, ms[p]))
        rng = abs(gain(b, ms[worst], ms[best]))
        spread = statistics.median(
            (max(vals[(p, 0, b)]) - min(vals[(p, 0, b)])) / ms[p] for p in pads
        )
        mid = statistics.median(ms.values())
        md.append(f"| {b} | {mid:.2f} | {rng:.1%} | {ms[best]:.2f} ({best}) ")
        md[-1] += f"| {ms[worst]:.2f} ({worst}) | {spread:.1%} |"
    md += ["", f"### Loop headers aligned, against unaligned at pads {o.align_pads}", "",
           "gain (positive = faster), median over the pads (faster at n of them)", "",
           "| bench | " + " | ".join(f"align {a}" for a in aligns) + " |",
           "|---" * (len(aligns) + 1) + "|"]  # fmt: skip
    for b in benches:
        cells = [[gain(b, med[(p, 0, b)], med[(p, a, b)]) for p in apads] for a in aligns]
        txt = [f"{statistics.median(g):+.1%} ({sum(x > 0 for x in g)}/{len(g)})" for g in cells]
        md.append(f"| {b} | " + " | ".join(txt) + " |")
    cm = [
        f"{statistics.median(size[(p, a)] / size[(p, 0)] - 1 for p in apads):+.1%}" for a in aligns
    ]
    md.append("| CoreMark code size | " + " | ".join(cm) + " |")
    with open(o.md, "w") as f:
        f.write("\n".join(md) + "\n")
    print("\n".join(md))


if __name__ == "__main__":
    main()
