"""Source trees compared on one machine, alternated round by round (S0015 step 3; the `compare` mode
of .github/workflows/placement-arm.yml):

    python tools/bench_compare.py [--target jit-aarch64] [--rounds 5] [--md F] [--json F] N=SRC..
    python tools/bench_compare.py --pad-control SRC   # a pre-S0015 aarch64 tree -> the control

Every round runs each variant once, in a rotated order, in a fresh interpreter with PYTHONPATH=SRC
(the tools are this checkout's): CoreMark (tools/run_coremark_jit.py; a run that does not validate
fails), the calls benchmark (tools/run_calls_bench.py, `leaf` and `fib`) and two record-call
kernels (`--kernels`: a call of another instance's function, a call_indirect into the caller's own
table; 10^7 calls, ns per call). Prints and writes the median and the range per variant."""

import json
import os
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INC = "(i32.add (local.get 0) (i32.const 1))"
B = f"""(module (func (export "f") (param i32) (result i32) {INC}))"""
A = f"""(module (type $t (func (param i32) (result i32))) (import "b" "f" (func $f (type $t)))
  (table 1 funcref) (elem (i32.const 0) $g) (func $g (type $t) {INC})
  (func (export "cross") (param $n i32) (result i32) (local $s i32)
    (loop $l (local.set $s (call $f (local.get $s)))
      (br_if $l (local.tee $n (i32.sub (local.get $n) (i32.const 1))))) (local.get $s))
  (func (export "indirect") (param $n i32) (result i32) (local $s i32)
    (loop $l (local.set $s (call_indirect (type $t) (local.get $s) (i32.const 0)))
      (br_if $l (local.tee $n (i32.sub (local.get $n) (i32.const 1))))) (local.get $s)))"""


def kernels(target: str) -> None:
    from wasmtime import wat2wasm

    from waboros.frontend.binary import parse
    from waboros.frontend.validate import validate
    from waboros.runtime import link
    from waboros.runtime.store import StoreState
    from waboros.targets import base

    t, store, insts = base.TARGETS[target](), StoreState(), []
    for text, imp in ((B, {}), (A, None)):
        m = parse(wat2wasm(text))
        validate(m)
        ext = link.resolve(m, imp if imp is not None else {"b": link.Provider(*insts[0])})
        insts.append((m, t.instantiate(t.compile(m), ext, store)))
    m, inst = insts[1]
    for name in ("cross", "indirect"):
        idx = next(i for n, k, i in m["exports"] if n == name and k == 0)
        t0 = time.perf_counter()
        assert inst.call(idx, [10**7]) == [10**7]
        print(f"{name} {(time.perf_counter() - t0) * 100:.3f}")  # ns per call


def pad_control(src: str) -> None:
    """The placement control (LESSONS 139) for S0015 step 3: a pre-step-3 aarch64 backend gets
    `nop`s where step 3 puts its C entries, 12 bytes before each body and host thunk and 88 (the
    module's `centry`) after the last; the code is otherwise the base's."""
    p = Path(src) / "waboros/targets/jit/aarch64/backend.py"
    s, at = (
        p.read_text(),
        "    def host_thunk(self, word: int) -> int:\n        at = len(self.a.buf)",
    )
    old = (
        "        return len(self.a.buf)\n\n    def c_entry_shared(self) -> int:\n        return 0\n"
    )
    if old not in s or at not in s:
        sys.exit(f"{p}: not the pre-S0015 aarch64 backend")
    s = s.replace(
        old, old.replace("        return len", "        self.a.pad_here(12)\n        return len")
    )
    s = s.replace("        return 0\n", "        self.a.pad_here(88)\n        return 0\n", 1)
    p.write_text(s.replace(at, at.replace("len(self.a.buf)", "self.c_entry()")))


def run(src: str, target: str) -> dict[str, float]:
    env = {**os.environ, "PYTHONPATH": f"{src}{os.pathsep}{ROOT / 'tools'}"}

    def py(*args: str) -> str:
        p = subprocess.run(
            [sys.executable, *args], cwd=ROOT, env=env, capture_output=True, text=True
        )
        if p.returncode:
            sys.exit(f"{src}: {args[0]} failed\n{p.stdout[-2000:]}{p.stderr[-2000:]}")
        return p.stdout

    out = py("tools/run_coremark_jit.py", "poc/in/core.wasm", "--target", target)
    sys.path.insert(0, str(ROOT / "tools"))
    from run_coremark_jit import valid

    if not valid(out):
        sys.exit(f"{src}: CoreMark did not validate\n{out[-2000:]}")
    got = {"CoreMark (it/s)": float(re.search(r"Iterations/Sec\s*:\s*([\d.]+)", out)[1])}  # type: ignore[index]
    calls = py("tools/run_calls_bench.py", "--targets", target, "--rounds", "1")
    for k in ("leaf", "fib"):
        got[f"calls {k} (ns)"] = float(re.search(rf"{k}\s+([\d.]+)", calls)[1])  # type: ignore[index]
    words = py("tools/bench_compare.py", "--kernels", "--target", target).split()
    for name, ns in zip(words[::2], words[1::2], strict=True):
        got[f"{name} (ns)"] = float(ns)
    return got


def main() -> None:
    arg = lambda k, d: sys.argv[sys.argv.index(k) + 1] if k in sys.argv else d  # noqa: E731
    target = arg("--target", "jit-aarch64")
    if "--kernels" in sys.argv:
        return kernels(target)
    if "--pad-control" in sys.argv:
        return pad_control(arg("--pad-control", ""))
    variants = dict(a.split("=", 1) for a in sys.argv[1:] if "=" in a)
    rounds, names = int(arg("--rounds", "5")), list(variants)
    res: dict[str, dict[str, list[float]]] = {n: {} for n in names}
    for r in range(rounds):
        for n in names[r % len(names) :] + names[: r % len(names)]:
            for k, v in run(variants[n], target).items():
                res[n].setdefault(k, []).append(v)
            print(f"round {r + 1} {n}: {res[n]}", flush=True)
    keys = list(res[names[0]])
    md = [f"{target}, {rounds} alternated rounds, median (min-max)", "",
          "| case | " + " | ".join(names) + " |", "|---" * (len(names) + 1) + "|"]  # fmt: skip
    for k in keys:
        cells = [f"{statistics.median(res[n][k]):.2f} ({min(res[n][k]):.2f}-{max(res[n][k]):.2f})"
                 for n in names]  # fmt: skip
        md.append(f"| {k} | " + " | ".join(cells) + " |")
    print("\n".join(md))
    if "--md" in sys.argv:
        Path(arg("--md", "")).write_text("\n".join(md) + "\n")
    if "--json" in sys.argv:
        Path(arg("--json", "")).write_text(json.dumps(res, indent=1))


main()
