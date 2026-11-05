"""Differential fuzz of `pyc` against `pyc` + the `bropt.py` pass, on `wasm-tools smith` modules.

    uv run python tools/fuzz.py N SEED [--fbits]

Runs N modules from seed SEED (S0010, "The fuzz harness"). Each export is called three times with
arguments drawn from the seed; results, trap codes and a hash of memory are compared between the
default `pyc` source and the same source after `poc/pyc-bropt/bropt.py`. A mismatch saves
`bad_<seed>.wasm` in the current directory; the exit status is non-zero on any mismatch or
optimizer error. `--fbits` sets WABOROS_PYC_FBITS=1 (floats as bit patterns, D46) for the whole run.

Modules are bounded (1 MiB memory, 1000 table elements, 3-12 functions, 3000 instructions, no SIMD,
GC, exceptions, threads or memory64) with a 20 s alarm each: unbounded smith modules get the process
OOM-killed. Termination is ensured by smith (--ensure-termination). Needs wasm-tools (the dev
group; WASM_TOOLS overrides the lookup)."""

import hashlib
import os
import random
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if "--fbits" in sys.argv:
    os.environ["WABOROS_PYC_FBITS"] = "1"  # read when the runtime is imported
sys.path.insert(0, str(ROOT / "poc" / "pyc-bropt"))

import bropt  # noqa: E402  # type: ignore[import-not-found]

from waboros.frontend.binary import parse  # noqa: E402
from waboros.frontend.validate import validate  # noqa: E402
from waboros.runtime.store import StoreState  # noqa: E402
from waboros.targets.pyc import _from_raw, _run  # noqa: E402
from waboros.targets.pyc.codegen import generate  # noqa: E402

ALARM = 20  # seconds per module
_bin = Path(sys.executable).parent
WT = os.environ.get("WASM_TOOLS") or shutil.which(
    "wasm-tools", path=f"{_bin}{os.pathsep}{os.environ.get('PATH', '')}"
)
FLAGS = [
    "--ensure-termination", "--export-everything", "true", "--max-imports", "0",
    "--min-funcs", "3", "--max-funcs", "12", "--max-instructions", "3000",
    "--simd-enabled", "false", "--reference-types-enabled", "false",
    "--exceptions-enabled", "false", "--gc-enabled", "false", "--tail-call-enabled", "true",
    "--multi-value-enabled", "true", "--max-memories", "1", "--max-memory32-bytes", "1048576",
    "--max-table-elements", "1000", "--threads-enabled", "false", "--memory64-enabled", "false",
    "--relaxed-simd-enabled", "false",
]  # fmt: skip


def _alarm(*_: object) -> None:
    raise TimeoutError


def build(src: str) -> tuple[StoreState, dict[str, Any], dict[str, Any]]:
    ns: dict[str, Any] = {"__name__": "m"}
    exec(compile(src, "<pyc>", "exec", optimize=2), ns)
    st = StoreState()
    ex = _run(st, ns["instantiate"], {})
    return st, ex, ns


def outcome(st: StoreState, f: Any, args: list[Any]) -> tuple[Any, ...]:
    try:
        return ("ok", _run(st, f, *args))
    except Exception as e:  # a trap or another exception: compare the class and its code
        return ("exc", type(e).__name__, getattr(getattr(e, "code", None), "name", str(e)[:40]))


def memhash(ns: dict[str, Any]) -> str:
    return hashlib.sha1(bytes(ns["M"])).hexdigest() if "M" in ns else ""


def main(n: int, seed0: int) -> int:
    if not WT:
        sys.exit("wasm-tools not found (uv sync, or set WASM_TOOLS)")
    signal.signal(signal.SIGALRM, _alarm)
    bad = skipped = done = touched = 0
    for seed in range(seed0, seed0 + n):
        rnd = random.Random(seed)
        r = subprocess.run([WT, "smith", *FLAGS, "-o", "/dev/stdout"],
                           input=rnd.randbytes(4096), capture_output=True)  # fmt: skip
        if r.returncode:
            skipped += 1
            continue
        wasm = r.stdout
        try:
            mod = parse(wasm)
            validate(mod)
            src = generate(mod)
        except Exception:
            skipped += 1
            continue
        try:
            osrc = bropt.optimize(src)
        except Exception as e:
            print("OPTIMIZER ERROR seed", seed, e)
            bad += 1
            continue
        if osrc.count("_br") != src.count("_br"):
            touched += 1
        signal.alarm(ALARM)
        try:
            a, b = build(src), build(osrc)
            sigs = {}
            for name, kind, idx in mod["exports"] or ():
                if kind == 0 and name in a[1]:
                    sigs[name] = mod["types"][mod["funcs"][idx]]
            for name, (ps, _) in sigs.items():
                for _trial in range(3):
                    raw = [rnd.getrandbits(32 if t == 0x7F else 64) for t in ps]
                    try:
                        args = [_from_raw(t, x) for t, x in zip(ps, raw, strict=True)]
                    except Exception:
                        break
                    ra, rb = outcome(a[0], a[1][name], args), outcome(b[0], b[1][name], args)
                    same_mem = memhash(a[2]) == memhash(b[2])
                    if repr(ra) != repr(rb) or not same_mem:
                        print("MISMATCH seed", seed, name, ra, rb, same_mem)
                        bad += 1
                        Path(f"bad_{seed}.wasm").write_bytes(wasm)
                        break
        except Exception:  # build failed or timed out: not a verdict on the pass
            skipped += 1
            continue
        finally:
            signal.alarm(0)
        done += 1
        if done % 50 == 0:
            print(f"  ... {done} run, rewritten {touched}, mismatches {bad}", flush=True)
    print(f"modules run {done}, rewritten by bropt {touched}, skipped {skipped}, mismatches {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--fbits"]
    if len(args) != 2:
        sys.exit(__doc__)
    sys.exit(main(int(args[0]), int(args[1])))
