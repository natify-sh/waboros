"""Differential fuzz of `pyc`'s branch emission, on `wasm-tools smith` modules.

    uv run python tools/fuzz.py N SEED [--fbits] [--gen]

Runs N modules from seed SEED (S0010, "The fuzz harness"). Each export is called three times with
arguments drawn from the seed; results, trap codes and a hash of memory are compared between three
`pyc` sources: the default (`codegen.BRRULES`, S0010's rules), the emission before S0010
(`BRRULES` empty, as `WABOROS_PYC_BR=0`) and that source after `poc/pyc-bropt/bropt.py` (the
oracle). A mismatch between the first two, or a source that fails to generate, compile or
instantiate in one of them only, saves `bad_<seed>.wasm` in the current directory and makes the exit
status non-zero. The oracle disagreeing with the emission before S0010 is counted apart and saved
as `oracle_<seed>.wasm`: a defect of `bropt.py` (BENCHMARKS.md, S0010 step 1), not of the rules.
`--fbits` sets WABOROS_PYC_FBITS=1 (floats as bit patterns, D46) for the whole run. `--gen` replaces
smith by a structured generator of the control flow S0010 rewrites (`structured`), draws `BRTREE`
from {0, 1, 2, 4} per module, and also compares each rule alone (`sink`, `dse`) with the emission
before S0010; every module is called on the same 28 argument pairs. Smith modules rarely reach these
shapes.

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
from waboros.targets.pyc import (  # noqa: E402
    _from_raw,
    _run,
    codegen,  # noqa: E402
)

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


def structured(rnd: random.Random) -> str:
    """`f(x, y)`: nested control flow around switches (WAT), terminating by fuel. Adapted from the
    S0010 step 1 reviewer's generator; its knobs are drawn per module so that one run covers
    fall-through and leaving case regions, result-typed case blocks, loops around a switch (a Python
    loop P), `if`/`else` frames, degenerate tables with a plain identifier index, empty regions,
    `return_call` and `unreachable`."""
    fall, swres = rnd.choice([0.2, 0.35, 0.6]), rnd.choice([0.0, 0.3, 0.6])
    wrap, degen = rnd.choice([0.0, 0.25, 0.6]), rnd.choice([0.0, 0.2, 0.5])
    empty, maxd, bud = rnd.choice([0.0, 0.1, 0.3]), 7, rnd.choice([10, 20, 30])
    hollow = rnd.choice([0.0, 0.0, 0.3])  # case regions with no code
    out: list[str] = []
    labels: list[tuple[str, str, int]] = [("$exit", "block", 0)]  # name, kind, arity
    cnt = [0]
    E = out.append

    def lab() -> str:
        cnt[0] += 1
        return f"$L{cnt[0]}"

    def val(ar: int) -> None:
        if ar:
            E("local.get $acc")

    def ar_of(lb: tuple[str, str, int]) -> int:
        return 0 if lb[1] == "loop" else lb[2]

    def pick(ar: int | None = None) -> tuple[str, str, int] | None:
        c = [lb for lb in labels if ar is None or ar_of(lb) == ar]
        return rnd.choice(c) if c else None

    def ending() -> bool:  # True: the code after is dead
        r = rnd.random()
        if r < fall:
            return False
        lb = pick()
        assert lb
        if r < 0.7:
            val(ar_of(lb))
            E(f"br {lb[0]}")
            return True
        if r < 0.8:
            E("local.get $acc return")
            return True
        if r < 0.86:
            E("local.get $acc return_call $h")
            return True
        if r < 0.9:
            E("unreachable")
            return True
        if r < 0.95:
            a = ar_of(lb)
            val(a)
            k = rnd.randint(1, 5)
            E(f"local.get $acc local.get $y i32.add i32.const {k} i32.and i32.eqz br_if {lb[0]}")
            if a:
                E("drop")
            return False
        a = rnd.choice([0, 0, 1])  # a br_table from the region to outer labels of one arity
        c = [lb for lb in labels if ar_of(lb) == a]
        if not c:
            return False
        ts = [rnd.choice(c)[0] for _ in range(rnd.randint(1, 6))]
        val(a)
        E(f"local.get $acc local.get $x i32.xor i32.const {rnd.randint(2, 7)} i32.rem_u")
        E("br_table " + " ".join(ts))
        return True

    def body(d: int, b: list[int], ar: int) -> bool:
        for _ in range(rnd.randint(1, 3)):
            if stmt(d, b):
                return True
        if rnd.random() < 0.3 and ending():
            return True
        val(ar)
        return False

    def switch(d: int, b: list[int]) -> None:
        n = rnd.randint(1, 6)
        ar = 1 if rnd.random() < swres else 0
        cases = [lab() for _ in range(n)]
        lp = None
        if rnd.random() < wrap:  # a loop around the switch: P is a Python loop
            lp = lab()
            E(f"loop {lp}")
            E("local.get $fuel i32.const 1 i32.sub local.tee $fuel i32.eqz br_if $exit")
            labels.append((lp, "loop", 0))
        for k, c in enumerate(reversed(cases)):
            if k and rnd.random() < 0.3:  # a branch in a case block before the next one opens
                lb = pick()
                assert lb
                val(ar_of(lb))
                E(f"local.get $acc local.get $y i32.add i32.const 3 i32.and i32.eqz br_if {lb[0]}")
                if ar_of(lb):
                    E("drop")
            E(f"block {c}" + (" (result i32)" if ar else ""))
            labels.append((c, "block", ar))
        inner, pre = rnd.random(), ""
        if inner < 0.15:  # the dispatch in an `if` arm, or in an extra block
            E("local.get $y i32.const 1 i32.and if")
            pre = "if"
        elif inner < 0.25:
            E("block $pre")
            labels.append(("$pre", "block", 0))
            pre = "blk"
        if rnd.random() < 0.5:
            body(d + 1, b, 0)  # code before the dispatch: loops, branches out of them
        same = [lb[0] for lb in labels if ar_of(lb) == ar]
        ts = [rnd.choice(cases + same) if rnd.random() < 0.8 else rnd.choice(cases)
              for _ in range(rnd.randint(1, 9))]  # fmt: skip
        if ar == 0 and pre == "blk" and rnd.random() < 0.5:
            ts.append("$pre")
        val(ar)
        if rnd.random() < degen:  # a plain identifier index, maybe only a default
            E("local.get $x")
            if rnd.random() < 0.5:
                ts = []
        else:
            E(f"local.get $acc local.get $x i32.add i32.const {rnd.randint(2, 9)} i32.rem_u")
        dflt = cases[0] if not ts and rnd.random() < 0.5 else rnd.choice(ts or cases)
        E("br_table " + " ".join(ts) + " " + dflt)  # cases[0]: the innermost, a depth-0 target
        if pre == "if":
            E("else")
            lb = pick(ar)
            if lb is not None and rnd.random() < 0.5:
                val(ar_of(lb))
                E(f"br {lb[0]}")
            E("end")
            val(ar)
        elif pre == "blk":
            labels.pop()
            E("end")
            if rnd.random() < 0.5:
                body(d + 1, b, 0)
            val(ar)
        for i, _c in enumerate(cases):
            E("end")
            labels.pop()
            if ar:
                E("local.get $acc i32.add local.set $acc")
            if i == len(cases) - 1:
                if rnd.random() >= hollow:
                    body(d + 1, b, 0)
                    if rnd.random() < 0.5:
                        ending()
                break
            if rnd.random() >= hollow and not body(d + 1, b, 0):  # or an empty case region
                ending()
            val(ar)
        if lp:
            labels.pop()
            E("end")

    def stmt(d: int, b: list[int]) -> bool:
        r = rnd.random()
        if rnd.random() < empty:
            return False
        if r < 0.2 or d > maxd or b[0] <= 0:
            E(f"local.get $acc i32.const 3 i32.mul i32.const {rnd.randint(1, 99)} i32.add")
            E("local.set $acc")
            return False
        b[0] -= 1
        if r < 0.5:
            switch(d, b)
            return False
        if r < 0.62:
            nm, ar = lab(), 1 if rnd.random() < 0.25 else 0
            E(f"block {nm}" + (" (result i32)" if ar else ""))
            labels.append((nm, "block", ar))
            body(d + 1, b, ar)
            labels.pop()
            E("end")
            if ar:
                E("local.get $acc i32.add local.set $acc")
            return False
        if r < 0.72:
            nm = lab()
            E(f"loop {nm}")
            E("local.get $fuel i32.const 1 i32.sub local.tee $fuel i32.eqz br_if $exit")
            labels.append((nm, "loop", 0))
            body(d + 1, b, 0)
            labels.pop()
            E("end")
            return False
        if r < 0.82:
            nm, ar = lab(), 1 if rnd.random() < 0.3 else 0
            k = rnd.randint(1, 3)
            E(f"local.get $acc local.get $y i32.add i32.const {k} i32.and if {nm}"
              + (" (result i32)" if ar else ""))  # fmt: skip
            labels.append((nm, "if", ar))
            body(d + 1, b, ar)
            if ar or rnd.random() < 0.6:
                E("else")
                body(d + 1, b, ar)
            labels.pop()
            E("end")
            if ar:
                E("local.get $acc i32.add local.set $acc")
            return False
        lb = pick()
        assert lb
        a = ar_of(lb)
        val(a)
        if r < 0.92:
            k = rnd.randint(1, 7)
            E(f"local.get $acc local.get $y i32.add i32.const {k} i32.and i32.eqz br_if {lb[0]}")
            if a:
                E("drop")
            return False
        E(f"br {lb[0]}")
        return True

    E("block $exit")
    body(1, [rnd.randint(4, bud)], 0)
    E("end")
    text = "\n".join(out)
    return f"""(module
 (func $h (param i32) (result i32) local.get 0 i32.const 7 i32.mul i32.const 1 i32.add)
 (func (export "f") (param $x i32) (param $y i32) (result i32)
 (local $acc i32) (local $fuel i32)
 i32.const 300 local.set $fuel local.get $x local.set $acc
{text}
 local.get $acc))"""


GEN_ARGS = [(x, y) for x in range(7) for y in (0, 1, 2, 77)]


def main(n: int, seed0: int, gen: bool = False) -> int:
    if not WT:
        sys.exit("wasm-tools not found (uv sync, or set WASM_TOOLS)")
    signal.signal(signal.SIGALRM, _alarm)
    bad = skipped = done = touched = sunk = oracle = 0
    rules, bt0 = codegen.BRRULES, codegen.BRTREE
    # the rules as configured, then (--gen) each rule alone; [OFF] is the emission before S0010
    cfgs = [rules] + ([frozenset({"sink"}), frozenset({"dse"})] if gen else [])
    OFF, ORC = len(cfgs), len(cfgs) + 1
    for seed in range(seed0, seed0 + n):
        rnd = random.Random(seed)
        if gen:
            r = subprocess.run([WT, "parse", "-o", "/dev/stdout"],
                               input=structured(rnd).encode(), capture_output=True)  # fmt: skip
            codegen.BRTREE = rnd.choice([0, 1, 2, 4])
        else:
            r = subprocess.run([WT, "smith", *FLAGS, "-o", "/dev/stdout"],
                               input=rnd.randbytes(4096), capture_output=True)  # fmt: skip
        if r.returncode:
            skipped += 1
            continue
        wasm = r.stdout
        try:
            mod = parse(wasm)
            validate(mod)
            codegen.BRRULES = frozenset()
            old = codegen.generate(mod)
        except Exception:
            skipped += 1
            continue
        finally:
            codegen.BRRULES = rules
        srcs: list[str] = []
        try:
            for c in cfgs:
                codegen.BRRULES = c
                srcs.append(codegen.generate(mod))
        except Exception as e:
            print("GENERATE ERROR seed", seed, sorted(c), repr(e)[:200])
            bad += 1
            Path(f"bad_{seed}.wasm").write_bytes(wasm)
            continue
        finally:
            codegen.BRRULES = rules
        srcs.append(old)
        try:
            srcs.append(bropt.optimize(old))
        except Exception as e:
            print("ORACLE ERROR seed", seed, repr(e)[:200])
            srcs.append(old)
            oracle += 1
        touched += srcs[ORC].count("_br") != old.count("_br")
        sunk += srcs[0] != old
        signal.alarm(ALARM)
        try:
            built: list[Any] = []
            for src in srcs:
                try:
                    built.append(build(src))
                except TimeoutError:
                    raise
                except Exception as e:  # a build fails: a verdict only if it fails differently
                    built.append(type(e).__name__)
            kinds = [b if isinstance(b, str) else "" for b in built]
            if kinds[ORC] != kinds[OFF]:
                print("ORACLE BUILD MISMATCH seed", seed, kinds)
                oracle += 1
                Path(f"oracle_{seed}.wasm").write_bytes(wasm)
                built[ORC] = kinds[OFF] or build(srcs[OFF])  # compare the rest without the oracle
            if any(k != kinds[OFF] for k in kinds[:OFF]):
                print("BUILD MISMATCH seed", seed, kinds)
                bad += 1
                Path(f"bad_{seed}.wasm").write_bytes(wasm)
                continue
            if isinstance(built[0], str):
                skipped += 1
                continue
            calls: list[tuple[str, list[Any]]] = []
            for name, kind, idx in mod["exports"] or ():
                if kind != 0 or name not in built[0][1]:  # pyright: ignore[reportOptionalSubscript]
                    continue
                ps = mod["types"][mod["funcs"][idx]][0]
                if gen:
                    calls += [(name, list(a)) for a in GEN_ARGS]
                    continue
                for _trial in range(3):
                    raw = [rnd.getrandbits(32 if t == 0x7F else 64) for t in ps]
                    try:
                        calls.append(
                            (name, [_from_raw(t, x) for t, x in zip(ps, raw, strict=True)])
                        )
                    except Exception:
                        break
            odiff = False
            for name, args in calls:
                res = [repr(outcome(b[0], b[1][name], args)) + memhash(b[2]) for b in built]
                if any(x != res[OFF] for x in res[:OFF]):
                    print("MISMATCH seed", seed, "BRTREE", codegen.BRTREE, name, args, res)
                    bad += 1
                    Path(f"bad_{seed}.wasm").write_bytes(wasm)
                    break
                if res[ORC] != res[OFF] and not odiff:
                    print("ORACLE MISMATCH seed", seed, name, args, res[OFF], res[ORC])
                    oracle += 1
                    odiff = True
                    Path(f"oracle_{seed}.wasm").write_bytes(wasm)
        except TimeoutError:  # timed out: not a verdict on the emission
            skipped += 1
            continue
        finally:
            signal.alarm(0)
            codegen.BRTREE = bt0
        done += 1
        if done % 500 == 0:
            print(f"  ... {done} run, changed {sunk}, mismatches {bad}", flush=True)
    print(
        f"modules run {done}, changed by the rules {sunk}, rewritten by bropt {touched}, "
        f"skipped {skipped}, mismatches {bad}, oracle disagreements {oracle}"
    )
    return 1 if bad else 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a not in ("--fbits", "--gen")]
    if len(args) != 2:
        sys.exit(__doc__)
    sys.exit(main(int(args[0]), int(args[1]), "--gen" in sys.argv))
