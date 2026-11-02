# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false
"""Run CoreMark (wasm) on a JIT target:
python tools/run_coremark_jit.py core.wasm [--target jit-aarch64] (default jit-x86_64)"""

import ctypes
import sys
import time
from collections.abc import Sequence
from typing import Any

from waboros.frontend.binary import parse
from waboros.frontend.validate import validate
from waboros.runtime import link
from waboros.runtime.store import StoreState
from waboros.targets import base
from waboros.targets.base import HostFunc, Raw

GOOD_CRCS = {
    "seedcrc": "0xe9f5",
    "[0]crclist": "0xe714",
    "[0]crcmatrix": "0x1fd7",
    "[0]crcstate": "0x8e3a",
}


def valid(text: str) -> bool:
    """Valid: CoreMark validated, or only its "at least 10 secs" rule failed (the iteration count is
    calibrated, a fast runner can undershoot) and the 2K-performance CRCs are the known ones."""
    if "Correct operation validated" in text:
        return True
    errors = [ln for ln in text.splitlines() if "ERROR!" in ln]
    if len(errors) != 1 or "at least 10 secs" not in errors[0]:
        return False
    got: dict[str, str] = {}
    for ln in text.splitlines():
        k, _, v = ln.partition(":")
        got[k.strip()] = v.strip()
    return all(got.get(k) == v for k, v in GOOD_CRCS.items())


def main() -> None:
    if sys.argv[1] == "--check":  # CI: run_coremark_jit.py --check coremark.txt
        with open(sys.argv[2]) as f:
            sys.exit(0 if valid(f.read()) else 1)
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
    print(f"[waboros {name}] compile {(time.perf_counter() - t) * 1000:.1f} ms")
    wiring: dict[str, HostFunc] = {"millis": millis, "print": pr}
    ext = link.resolve(mod, {"wiring": wiring})
    box["inst"] = inst = tg.instantiate(c, ext, StoreState())
    idx = next(i for n, k, i in mod["exports"] or () if n == "setup" and k == 0)
    t = time.perf_counter()
    inst.call(idx, [])
    print(f"\n[waboros {name}] wall {time.perf_counter() - t:.2f}s")


if __name__ == "__main__":
    main()
