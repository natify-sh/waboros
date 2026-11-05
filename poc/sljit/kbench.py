"""The loop kernel (kern.wasm) on wasmtime and waboros jit-x86_64.   uv run python kbench.py kern.wasm"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engines import runner  # noqa: E402

wasm = open(sys.argv[1], "rb").read()
n = 300_000_000
for tgt in ("wasmtime", "jit-x86_64"):
    call = runner(tgt, wasm)
    call("k", 1000)
    ts = []
    for _ in range(5):
        t = time.perf_counter()
        r = call("k", n)
        ts.append(time.perf_counter() - t)
    print(f"{tgt:11} best {min(ts):.3f} s  result {r:08x}")
