"""Differential fuzz: pyc vs pyc + bropt on `wasm-tools smith` modules (results, trap codes, memory).

    PYTHONPATH=<waboros>/src python tools/fuzz.py N SEED      (a mismatch saves bad_<seed>.wasm)

Modules are bounded (1 MiB memory, 1000 table elements, 20 s): unbounded smith modules get the
process OOM-killed. Termination is ensured by smith (--ensure-termination)."""
import hashlib, os, random, shutil, signal, subprocess, sys, time

def _alarm(*a):
    raise TimeoutError()

signal.signal(signal.SIGALRM, _alarm)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bropt
from waboros.frontend.binary import parse
from waboros.frontend.validate import validate
from waboros.targets.pyc import _run, _from_raw, _to_raw
from waboros.targets.pyc.codegen import generate
from waboros.runtime.store import StoreState

WT = os.environ.get("WASM_TOOLS") or shutil.which("wasm-tools")
FLAGS = ["--ensure-termination", "--export-everything", "true", "--max-imports", "0", "--min-funcs", "3",
         "--max-funcs", "12", "--max-instructions", "3000", "--simd-enabled", "false",
         "--reference-types-enabled", "false", "--exceptions-enabled", "false", "--gc-enabled", "false",
         "--tail-call-enabled", "true", "--multi-value-enabled", "true", "--max-memories", "1", "--max-memory32-bytes", "1048576", "--max-table-elements", "1000",
         "--threads-enabled", "false", "--memory64-enabled", "false", "--relaxed-simd-enabled", "false"]

def build(src):
    ns = {"__name__": "m"}
    exec(compile(src, "<pyc>", "exec", optimize=2), ns)
    st = StoreState()
    ex = _run(st, ns["instantiate"], {})
    return st, ex, ns

def outcome(st, f, args):
    try:
        return ("ok", _run(st, f, *args))
    except Exception as e:  # trap or other: compare the class and its code
        return ("exc", type(e).__name__, getattr(getattr(e, "code", None), "name", str(e)[:40]))

touched = [0]

def main(n, seed0):
    bad = skipped = done = 0
    for seed in range(seed0, seed0 + n):
        rnd = random.Random(seed)
        data = rnd.randbytes(4096)
        r = subprocess.run([WT, "smith", *FLAGS, "-o", "/dev/stdout"], input=data, capture_output=True)
        if r.returncode:
            skipped += 1; continue
        wasm = r.stdout
        try:
            mod = parse(wasm); validate(mod)
            src = generate(mod)
        except Exception:
            skipped += 1; continue
        try:
            osrc = bropt.optimize(src)
        except Exception as e:
            print("OPTIMIZER ERROR seed", seed, e); bad += 1; continue
        if osrc.count("_br") != src.count("_br"):
            touched[0] += 1
        signal.alarm(20)
        try:
            A = build(src); B = build(osrc)
        except Exception as e:
            skipped += 1; continue
        types = mod["types"]; nimp = 0
        fsig = {}
        for name, kind, idx in mod["exports"] or ():
            if kind == 0:
                fsig[name] = types[mod["funcs"][idx - nimp]] if idx >= nimp else None
        for name, sig in fsig.items():
            if sig is None or name not in A[1]:
                continue
            ps, _ = sig
            for trial in range(3):
                raw = [rnd.getrandbits(64) if t != 0x7F else rnd.getrandbits(32) for t in ps]
                try:
                    args = [_from_raw(t, x) for t, x in zip(ps, raw)]
                except Exception:
                    break
                ra = outcome(A[0], A[1][name], args)
                rb = outcome(B[0], B[1][name], args)
                ma = hashlib.sha1(bytes(A[2]["M"])).hexdigest() if "M" in A[2] else ""
                mb = hashlib.sha1(bytes(B[2]["M"])).hexdigest() if "M" in B[2] else ""
                if repr(ra) != repr(rb) or ma != mb:
                    print("MISMATCH seed", seed, name, ra, rb, ma == mb); bad += 1
                    open(f"bad_{seed}.wasm", "wb").write(wasm)
                    break
        signal.alarm(0)
        done += 1
        if done % 50 == 0:
            print(f"  ... {done} run, rewritten {touched[0]}, mismatches {bad}", flush=True)
    print(f"modules run {done}, rewritten by bropt {touched[0]}, skipped {skipped}, mismatches {bad}")

main(int(sys.argv[1]), int(sys.argv[2]))
