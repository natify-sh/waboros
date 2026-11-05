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
GC, threads or memory64; exceptions only in the cross-target mode) with a 20 s alarm each: unbounded
smith modules get the process OOM-killed. Termination is ensured by smith (--ensure-termination).
Needs wasm-tools (the dev group; WASM_TOOLS overrides the lookup).

    python tools/fuzz.py --targets pyc,jit,wasmtime [--minutes M] [--seed S] [--gen] [--out D]

is the cross-target mode (TESTING.md): pyc, jit-x86_64 (guard, explicit) and Wasmtime for M minutes;
a divergence is saved as `div_<seed>.wasm` + `.txt` in DIR and the exit status stays 0."""

import argparse
import ctypes
import gc
import hashlib
import os
import random
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if "--fbits" in sys.argv:
    os.environ["WABOROS_PYC_FBITS"] = "1"  # read when the runtime is imported
sys.path.insert(0, str(ROOT / "poc" / "pyc-bropt"))

import bropt  # noqa: E402  # type: ignore[import-not-found]

from waboros.frontend.binary import parse  # noqa: E402
from waboros.frontend.validate import validate  # noqa: E402
from waboros.runtime import link  # noqa: E402
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


# --- the cross-target mode: pyc, jit-x86_64 (guard, explicit) and Wasmtime ---------------------

EDGE = [0, 1, 2, 3, 7, 8, 16, 100, 255, 256, 65535, 65536, 0x7FFFFFFF, 0x80000000, (1 << 64) - 1]


def _nan_bits(ty: int, v: int) -> int:
    """A NaN of the type with any payload or sign becomes the canonical one: which NaN an operator
    makes is nondeterministic in the spec (the spec runner's `nan:arithmetic`)."""
    if ty == 0x7D and v & 0x7F800000 == 0x7F800000 and v & 0x7FFFFF:
        return 0x7FC00000
    if ty == 0x7C and v & 0x7FF0 << 48 == 0x7FF0 << 48 and v & (1 << 52) - 1:
        return 0x7FF8 << 48
    return v


def _same(a: tuple[Any, ...], b: tuple[Any, ...]) -> bool:
    """Equal outcomes; a v128 result also matches if it does under f32x4 or f64x2 NaN lanes."""
    if a == b:
        return True
    if a[0] != "ok" or b[0] != "ok":
        return False
    for (t, x), (_, y) in zip(a[1], b[1], strict=True):
        if x != y and not (
            t == 0x7B and any(_canon_lanes(x, w) == _canon_lanes(y, w) for w in (32, 64))
        ):
            return False
    return True


def _canon_lanes(v: int, w: int) -> list[int]:
    m, ty = (1 << w) - 1, 0x7D if w == 32 else 0x7C
    return [_nan_bits(ty, v >> i & m) for i in range(0, 128, w)]


class _Side:
    """One target (or Wasmtime) running one module: calls by export name, memory hashes."""

    def __init__(self, name: str, eng: Any, mod: Any, inst: Any, mems: list[str]) -> None:
        self.name, self.eng, self.inst, self.mems = name, eng, inst, mems
        self.mod = mod  # None for Wasmtime, which types its own values

    def call(self, ty: tuple[list[int], list[int]], name: str, args: list[int]) -> tuple[Any, ...]:
        from tests.spec.engine import EngineError, Trap

        try:
            res = self.eng.call(self.inst, name, args)
        except Trap as e:
            return ("trap", e.code.name)
        except EngineError as e:
            return ("exc", type(e).__name__)
        except Exception as e:  # a crash in a target is a finding too
            return ("exc", type(e).__name__)
        return (
            "ok",
            [
                (t, _nan_bits(t, v & (1 << _BITS[t]) - 1) if t in _BITS else int(v is not None))
                for t, v in zip(ty[1], res, strict=True)
            ],  # a reference result: null or not (the handles differ per engine)
        )

    def memhash(self) -> str:
        h = hashlib.sha1()
        for n in self.mems:
            if self.mod is None:  # wasmtime
                m = self.inst.exports(self.eng._store)[n]
                b = m.read(self.eng._store, 0, m.data_len(self.eng._store))
            else:
                v = self.inst.provider[1].export(n).value
                b = ctypes.string_at(v.res.addr, v.pages << 16) if hasattr(v, "res") else bytes(v)
            h.update(len(b).to_bytes(8, "little") + hashlib.sha1(b).digest())
        return h.hexdigest()


_BITS = {0x7F: 32, 0x7E: 64, 0x7D: 32, 0x7C: 64, 0x7B: 128}
SIDES = ["pyc", "jit-guard", "jit-explicit", "wasmtime"]


def _open(name: str, wasm: bytes, mod: Any) -> _Side | str:
    """The side `name` loaded and instantiated, or a string: `skip:<why>` (a target that cannot run
    the module) or `inst:<outcome>` (instantiating trapped or failed)."""
    from tests.spec.engine import EngineError, LoadError, Trap, Unsupported

    wt = name == "wasmtime"
    if wt:
        from tests.spec.wasmtime_engine import WasmtimeEngine

        eng: Any = WasmtimeEngine()
    else:
        from tests.spec.target_engine import TargetEngine

        eng = TargetEngine("pyc" if name == "pyc" else "jit-x86_64")
        os.environ["WABOROS_JIT_BOUNDS"] = name.removeprefix("jit-")
    try:
        m = eng.load(wasm)
        if isinstance(m, Unsupported):
            return "skip:unsupported " + str(m)
        inst = eng.instantiate(m, {})
    except Unsupported as e:
        return "skip:unsupported " + str(e)
    except LoadError as e:
        return "skip:load" if e.kind is None or wt else "inst:load-error " + e.kind
    except Trap as e:
        return "inst:trap " + e.code.name
    except EngineError as e:
        return "inst:" + type(e).__name__
    except Exception as e:  # a crash in a target is a finding too
        return "inst:" + type(e).__name__
    return _Side(name, eng, None if wt else mod, inst, [n for n, k, _ in mod["exports"] if k == 2])


def _args(rnd: random.Random, ps: list[int]) -> list[int] | None:
    out: list[int] = []
    for t in ps:
        if t not in _BITS:
            return None  # a reference type: not driven from here
        w = _BITS[t]
        out.append(rnd.choice(EDGE) & (1 << w) - 1 if t in (0x7F, 0x7E) and rnd.random() < 0.4
                   else rnd.getrandbits(w))  # fmt: skip
    return out


def compare(wasm: bytes, rnd: random.Random, gen: bool, skips: dict[str, int]) -> str | None:
    """Run `wasm` on every side: None if they agree, else the first divergence described."""
    mod = parse(wasm)
    validate(mod)
    sides: list[_Side] = []
    insts: dict[str, str] = {}
    for name in SIDES:
        r = _open(name, wasm, mod)
        if isinstance(r, _Side):
            sides.append(r)
        elif r.startswith("skip:"):
            skips[name] = skips.get(name, 0) + 1
        else:
            insts[name] = r
    if insts and len(set(insts.values())) > 1 or insts and sides:
        return f"instantiate: {insts} (ok on {[s.name for s in sides]})"
    if len(sides) + len(insts) < 2:
        return None
    for name, kind, idx in mod["exports"]:
        if kind != 0 or not sides:
            continue
        ps, rs = link.func_type(mod, idx)
        ty = (ps, rs)
        trials = [list(a) for a in GEN_ARGS] if gen else [_args(rnd, ps) for _ in range(3)]
        for args in trials:
            if args is None:
                break
            got = [(s.name, s.call(ty, name, args), s.memhash()) for s in sides]
            ref = got[-1] if got[-1][0] == "wasmtime" else got[0]
            bad = [g[0] for g in got if not _same(g[1], ref[1]) or g[2] != ref[2]]
            if bad:
                return (
                    f"export {name} args {args}\n"
                    + "\n".join(f"  {n}: {o} mem {h[:12]}" for n, o, h in got)
                    + f"\ndiffering from {ref[0]}: {bad}"
                )
    return None


def cross(minutes: float, seed0: int, gen: bool, out: Path) -> int:
    if not WT:
        sys.exit("wasm-tools not found (uv sync, or set WASM_TOOLS)")
    sys.path.insert(0, str(ROOT))
    out.mkdir(parents=True, exist_ok=True)
    signal.signal(signal.SIGALRM, _alarm)
    end = time.monotonic() + minutes * 60
    n = ok = div = timeouts = 0
    skips: dict[str, int] = {}
    seed = seed0
    while time.monotonic() < end:
        rnd, seed, n = random.Random(seed), seed + 1, n + 1
        s = seed - 1
        if gen:
            r = subprocess.run([WT, "parse", "-o", "/dev/stdout"],
                               input=structured(rnd).encode(), capture_output=True)  # fmt: skip
        else:
            fl = list(FLAGS)
            for k, v in {
                "--simd-enabled": rnd.random() < 0.4, "--memory64-enabled": rnd.random() < 0.25,
                "--max-memories": rnd.choice([1, 1, 3]), "--max-memory64-bytes": 1048576,
                "--wide-arithmetic-enabled": rnd.random() < 0.2, "--canonicalize-nans": True,
                "--exceptions-enabled": True,
            }.items():  # fmt: skip
                if k in fl:
                    fl[fl.index(k) + 1] = str(v).lower()
                else:
                    fl += [k, str(v).lower()]
            r = subprocess.run([WT, "smith", *fl, "-o", "/dev/stdout"],
                               input=rnd.randbytes(4096), capture_output=True)  # fmt: skip
        if r.returncode:
            skips["generator"] = skips.get("generator", 0) + 1
            continue
        signal.alarm(ALARM)
        try:
            d = compare(r.stdout, rnd, gen, skips)
        except TimeoutError:
            timeouts += 1
            continue
        except Exception as e:  # the harness or the front end, not a target
            skips["harness:" + type(e).__name__] = skips.get("harness:" + type(e).__name__, 0) + 1
            continue
        finally:
            signal.alarm(0)
        ok += 1
        if d:
            div += 1
            (out / f"div_{s}.wasm").write_bytes(r.stdout)
            (out / f"div_{s}.txt").write_text(f"seed {s} mode {'gen' if gen else 'smith'}\n{d}\n")
            print("DIVERGENCE seed", s, d.splitlines()[0][:200], flush=True)
        if n % 200 == 0:
            gc.collect()  # the JIT's address-space reservations
            print(f"  ... {n} modules, {div} divergences", flush=True)
    print(
        f"cross-target {'gen' if gen else 'smith'} seeds {seed0}..{seed - 1}: generated {n}, "
        f"compared {ok}, timeouts {timeouts}, skips {skips}, divergences {div} (in {out})"
    )
    return 0  # non-blocking: findings are triaged later (ROADMAP.md)


if __name__ == "__main__":
    if "--targets" in sys.argv:
        ap = argparse.ArgumentParser()
        ap.add_argument("--targets", required=True, help="pyc,jit,wasmtime (all three compared)")
        ap.add_argument("--minutes", type=float, default=5)
        ap.add_argument("--seed", type=int, default=0)
        ap.add_argument("--gen", action="store_true")
        ap.add_argument("--fbits", action="store_true")
        ap.add_argument("--out", type=Path, default=Path("fuzz-out"))
        a = ap.parse_args()
        if set(a.targets.split(",")) != {"pyc", "jit", "wasmtime"}:
            sys.exit("--targets: only pyc,jit,wasmtime is implemented")
        sys.exit(cross(a.minutes, a.seed, a.gen, a.out))
    args = [a for a in sys.argv[1:] if a not in ("--fbits", "--gen")]
    if len(args) != 2:
        sys.exit(__doc__)
    sys.exit(main(int(args[0]), int(args[1]), "--gen" in sys.argv))
