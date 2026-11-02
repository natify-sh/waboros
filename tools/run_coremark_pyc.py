"""Run CoreMark (wasm) through the `pyc` target: python tools/run_coremark_pyc.py core.wasm"""

import sys
import time
from typing import Any

from waboros.targets.pyc import load


def main() -> None:
    with open(sys.argv[1], "rb") as f:
        wasm = f.read()
    t0 = time.perf_counter()
    exports: dict[str, Any] = {}

    def millis() -> int:
        return int((time.perf_counter() - t0) * 1000) & 0xFFFFFFFF

    def pr(p: int, n: int) -> None:
        sys.stdout.write(bytes(exports["memory"][p : p + n]).decode(errors="replace"))

    exports.update(load(wasm, {"wiring": {"millis": millis, "print": pr}}))
    t = time.perf_counter()
    exports["setup"]()
    print(f"\n[waboros pyc] wall {time.perf_counter() - t:.2f}s")


main()
