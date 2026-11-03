"""sljit compiled to wasm, run on wasmtime, waboros jit-x86_64 and pyc: same code bytes? compile speed?

    uv run python compat.py BUILD_DIR      (BUILD_DIR holds k_X86_64.wasm, k_ARM_64.wasm, k_RISCV_64.wasm)"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engines import runner  # noqa: E402

B = sys.argv[1]
for arch in ("X86_64", "ARM_64", "RISCV_64"):
    wasm = open(f"{B}/k_{arch}.wasm", "rb").read()
    res = {}
    for tgt in ("wasmtime", "jit-x86_64", "pyc"):
        call = runner(tgt, wasm)
        h = call("bench", 1)
        n = call("len")
        res[tgt] = (h, bytes(call("byte", i) for i in range(n)))
        print(f"{arch:9} {tgt:11} hash {h:08x} len {n}")
    print(f"{arch:9} identical code on all three: {len(set(res.values())) == 1}\n")
wasm = open(f"{B}/k_X86_64.wasm", "rb").read()
for tgt, n in (("wasmtime", 20000), ("jit-x86_64", 20000), ("pyc", 20)):
    call = runner(tgt, wasm)
    call("bench", 5)
    t = time.perf_counter()
    call("bench", n)
    print(f"compile throughput on {tgt:11} {(time.perf_counter() - t) / n * 1e6:8.1f} us per function")
