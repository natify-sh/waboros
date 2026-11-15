"""python simdb.py tools/run_simd_bench.py --targets jit-x86_64,jit-sljit,wasmtime: a tool with jit-sljit registered."""
import os, runpy, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import waboros_sljit  # noqa: F401,E402
runpy.run_path(sys.argv.pop(1), run_name="__main__")
