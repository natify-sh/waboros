"""python cm.py core.wasm [--target jit-sljit]: tools/run_coremark_jit.py with jit-sljit registered."""
import os, runpy, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import waboros_sljit  # noqa: F401,E402
runpy.run_path(os.path.join(os.environ["WABOROS_TOOLS"], "run_coremark_jit.py"), run_name="__main__")
