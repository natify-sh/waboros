# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false
"""Run CoreMark (wasm) on a JIT target:
python tools/run_coremark_jit.py core.wasm [--target jit-aarch64] (default jit-x86_64)"""

import ctypes
import sys
import time
from collections.abc import Sequence
from typing import Any

from w2py.frontend.binary import parse
from w2py.frontend.validate import validate
from w2py.runtime import link
from w2py.runtime.store import StoreState
from w2py.targets import base
from w2py.targets.base import HostFunc, Raw


def main() -> None:
    name = sys.argv[sys.argv.index("--target") + 1] if "--target" in sys.argv else "jit-x86_64"
    with open(sys.argv[1], "rb") as f:
        wasm = f.read()
    t0 = time.perf_counter()
    box: dict[str, Any] = {}

    def millis(args: Sequence[Raw]) -> list[Raw]:
        return [int((time.perf_counter() - t0) * 1000) & 0xFFFFFFFF]

    def pr(args: Sequence[Raw]) -> list[Raw]:
        p, n = args
        addr = box["inst"].memory.res.addr + p
        sys.stdout.write(ctypes.string_at(addr, n).decode(errors="replace"))
        return []

    t = time.perf_counter()
    mod = parse(wasm)
    validate(mod)
    tg = base.TARGETS[name]()
    c = tg.compile(mod)
    print(f"[w2py {name}] compile {(time.perf_counter() - t) * 1000:.1f} ms")
    wiring: dict[str, HostFunc] = {"millis": millis, "print": pr}
    ext = link.resolve(mod, {"wiring": wiring})
    box["inst"] = inst = tg.instantiate(c, ext, StoreState())
    idx = next(i for n, k, i in mod["exports"] or () if n == "setup" and k == 0)
    t = time.perf_counter()
    inst.call(idx, [])
    print(f"\n[w2py {name}] wall {time.perf_counter() - t:.2f}s")


main()
