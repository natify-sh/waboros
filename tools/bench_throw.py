"""Throw cost (S0012; BENCHMARKS.md, "Exceptions: throw cost"): one `throw` caught D frames up.

    python tools/bench_throw.py [--targets jit-x86_64,pyc,wasmtime] [--frames 1,10,100]
                                [--rounds 3] [--nothrow]

The module is built here (no WAT tool needed). `loop(n)` runs n iterations: `$f0` calls `$f1` ...
`$f{D-1}`, which throws its i32 argument; a `try_table` in the loop catches it and adds it up. With
`--nothrow` the last function returns instead (the cost of the call chain alone). Prints ns per
iteration, the best of `--rounds` alternated rounds of 20 000 (5 000 from D = 100); fails if the
targets' results differ. Wasmtime (optional) is wasmtime-py."""

import sys
import time
from collections.abc import Callable

from waboros.frontend import binary
from waboros.frontend.validate import validate
from waboros.runtime import link
from waboros.runtime.store import StoreState
from waboros.targets import base


def leb(v: int) -> bytes:
    out = bytearray()
    while True:
        b, v = v & 0x7F, v >> 7
        out.append(b | (0x80 if v else 0))
        if not v:
            return bytes(out)


def vec(items: list[bytes]) -> bytes:
    return leb(len(items)) + b"".join(items)


def section(sid: int, body: bytes) -> bytes:
    return bytes([sid]) + leb(len(body)) + body


def module(d: int, throw: bool) -> bytes:
    """Types 0 (i32)->() and 1 (i32)->(i32); functions f0..f{d-1}, then `loop`; tag 0 (i32)."""
    types = vec([b"\x60\x01\x7f\x00", b"\x60\x01\x7f\x01\x7f"])
    funcs = vec([b"\x00"] * d + [b"\x01"])
    tags = vec([b"\x00\x00"])
    exports = vec([vec([b"l", b"o", b"o", b"p"]) + b"\x00" + leb(d)])
    bodies = [b"\x00\x20\x00\x10" + leb(k + 1) + b"\x0b" for k in range(d - 1)]
    bodies.append(b"\x00\x20\x00" + (b"\x08\x00" if throw else b"\x1a") + b"\x0b")
    loop = (
        b"\x01\x01\x7f"  # local $s i32
        b"\x03\x40\x02\x7f"  # loop, block (result i32)
        b"\x1f\x40\x01\x00\x00\x00"  # try_table (catch tag 0 -> the block)
        b"\x20\x00\x10\x00\x0b"  # call f0 (n)
        b"\x41\x00\x0b"  # i32.const 0 (no throw), end block
        b"\x20\x01\x6a\x21\x01"  # s += caught
        b"\x20\x00\x41\x01\x6b\x22\x00\x0d\x00\x0b"  # n -= 1, br_if loop
        b"\x20\x01\x0b"
    )
    bodies.append(loop)
    code = vec([leb(len(b)) + b for b in bodies])
    return b"\0asm\1\0\0\0" + b"".join(
        section(i, s) for i, s in ((1, types), (3, funcs), (13, tags), (7, exports), (10, code))
    )


def runner(target: str, wasm: bytes) -> Callable[[int], int]:
    if target == "wasmtime":
        import wasmtime  # pyright: ignore[reportMissingTypeStubs]

        st = wasmtime.Store(wasmtime.Engine())
        f = wasmtime.Instance(st, wasmtime.Module(st.engine, wasm), []).exports(st)["loop"]
        return lambda n: f(st, n) & 0xFFFFFFFF  # pyright: ignore
    mod = binary.parse(wasm)
    validate(mod)
    tg = base.TARGETS[target]()
    inst = tg.instantiate(tg.compile(mod), link.resolve(mod, {}), StoreState())
    idx = next(i for n, k, i in mod["exports"] if k == 0 and n == "loop")
    return lambda n: int(inst.call(idx, [n])[0]) & 0xFFFFFFFF


def main() -> None:
    arg = lambda k, d: sys.argv[sys.argv.index(k) + 1] if k in sys.argv else d  # noqa: E731
    targets = arg("--targets", "jit-x86_64,pyc,wasmtime").split(",")
    frames = [int(x) for x in arg("--frames", "1,10,100").split(",")]
    rounds = int(arg("--rounds", "3"))
    throw = "--nothrow" not in sys.argv
    print(f"ns per {'throw-and-catch' if throw else 'call chain'}, best of {rounds} rounds")
    for d in frames:
        n = 20_000 if d < 100 else 5_000
        runs = {t: runner(t, module(d, throw)) for t in targets}
        got = {t: r(100) for t, r in runs.items()}
        if len(set(got.values())) != 1:
            sys.exit(f"results differ: {got}")
        best = dict.fromkeys(targets, float("inf"))
        for _ in range(rounds):  # alternated: every target once per round
            for t, r in runs.items():
                t0 = time.perf_counter()
                r(n)
                best[t] = min(best[t], (time.perf_counter() - t0) / n * 1e9)
        print(f"D={d:<4}" + "".join(f"  {t} {best[t]:8.0f}" for t in targets))


main()
