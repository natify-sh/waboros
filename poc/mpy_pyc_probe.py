"""`pyc` output on MicroPython (unix port): translate on CPython, run under `micropython`.

    python poc/mpy_pyc_probe.py [--micropython BIN] [--wasm poc/in/core.wasm] [--spec] [NAME ...]

Runs CoreMark and checks its self-check CRCs (exit 1 on failure); with --spec also runs the
assert_return / assert_trap commands of a subset of the converted spec suite (.cache/spec). The
package itself does not import on MicroPython, so the tool gives the generated module what it needs
and nothing else: a stub `waboros.targets.base`, `struct.Struct` and `os.environ` shims, and three
source rewrites that stand for what a MicroPython flavour of the generator/runtime has to do (each
marked FIX below; the findings are in docs/LESSONS.md). Ints and floats of the standard unix build.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from waboros.targets.pyc import translate  # noqa: E402

SPEC = [
    "i32",
    "i64",
    "f32",
    "f64",
    "conversions",
    "memory",
    "call_indirect",
    "address",
    "loop",
    "select",
    "int_exprs",
    "float_exprs",
    "f32_bitwise",
    "float_memory",
    "float_misc",
    "if",
    "br",
]
CRCS = ("[0]crclist       : 0xe714", "[0]crcmatrix     : 0x1fd7", "[0]crcstate      : 0x8e3a")

BASE = """ALLOC_LIMIT = {1: 4294967251, 2: 2147483625, 4: 1073741812, 8: 536870905, 16: 268435452}
class TrapCode:
    pass
for _i, _n in enumerate("UNREACHABLE MEMORY_OUT_OF_BOUNDS INTEGER_DIVISION_BY_ZERO INTEGER_OVERFLOW STACK_OVERFLOW BAD_CONVERSION_TO_INTEGER INDIRECT_CALL_TO_NULL BAD_SIGNATURE TABLE_OUT_OF_BOUNDS NULL_REFERENCE".split()):
    setattr(TrapCode, _n, _i + 1)
class Trap(Exception):
    def __init__(self, code, message=""):
        super().__init__(message)
        self.code = code
"""
# MicroPython's `struct` has no Struct class: a stand-in (the builtin module is found as `ustruct`)
STRUCT = """from ustruct import *
import ustruct as _u
class Struct:
    def __init__(self, fmt): self.f = fmt
    def unpack_from(self, b, o=0): return _u.unpack_from(self.f, b, o)
    def pack_into(self, b, o, *v): _u.pack_into(self.f, b, o, *v)
"""
OS = "from uos import *\nenviron = {}\n"  # FIX: the runtime reads os.environ at import
# the MP side: CoreMark, or the spec commands in <dir>/<name>.json against <dir>/<module>.py
DRIVER = r"""
import sys, time, json
import waboros.targets.pyc.runtime as R
def conv(t, v):
    if t == 'i32': return int(v) & 0xFFFFFFFF
    if t == 'i64': return int(v) & 0xFFFFFFFFFFFFFFFF
    if t == 'f32': return R._f32_i32(int(v))
    if t == 'f64': return R._f64_i64(int(v))
    raise ValueError(t)
def same(t, e, g):
    if t[0] == 'i': return conv(t, g) == conv(t, e)
    if e.startswith('nan:'): return g != g
    x = conv(t, e)
    if x != x or g != g: return x != x and g != g
    return (R._i32_f32(g) == R._i32_f32(x)) if t == 'f32' else (R._i64_f64(g) == R._i64_f64(x))
def spec(name):
    d = json.load(open(sys.argv[2] + '/' + name + '.json')); inst = {}; ok = 0; bad = []
    for kind, mi, field, args, exp, line in d['cmds']:
        try:
            if mi not in inst: inst[mi] = __import__(d['mods'][mi]).instantiate({})
            r = inst[mi][field](*[conv(t, v) for t, v in args])
            if kind == 'assert_trap': raise AssertionError('no trap')
            if kind == 'assert_return' and len(exp) == 1 and exp[0][1] is not None and exp[0][0] in ('i32', 'i64', 'f32', 'f64'):
                if not same(exp[0][0], exp[0][1], r): raise AssertionError('got %r want %s' % (r, exp[0][1]))
            ok += 1
        except Exception as e:
            if kind == 'assert_trap' and not isinstance(e, AssertionError): ok += 1
            else: bad.append('%s:%d %s %s: %s' % (name, line, field, type(e).__name__, str(e)[:60]))
    print('SPEC %-14s pass %4d fail %d' % (name, ok, len(bad)))
    for b in bad[:5]: print('   ', b)
if sys.argv[1] == 'spec':
    for n in sys.argv[3:]: spec(n)
else:
    sys.path.insert(0, sys.argv[2]); t0 = time.ticks_ms()
    m = __import__('core')
    ex = m.instantiate({'wiring': {'millis': lambda: time.ticks_diff(time.ticks_ms(), t0) & 0xFFFFFFFF,
                                   'print': lambda p, n: sys.stdout.write(bytes(m.M[p:p + n]).decode())}})
    ex['setup']()
"""


def fix(src: str) -> str:
    # FIX: MicroPython functions take no attributes: the type key `f.s` (call_indirect) and the tail
    # call body `f.t` go in module dicts instead
    src = re.sub(r"^(f\d+)\.([st]) = ", r"_\2[\1] = ", src, flags=re.M)
    src = re.sub(r"= (t\d+)\.s\b", r"= _s[\1]", src).replace(
        "except AttributeError:", "except KeyError:"
    )
    src = re.sub(r'getattr\((\w+), "t", \1\)', r"_t.get(\1, \1)", src)
    return src.replace("import *\n", "import *\n_s = {}\n_t = {}\n", 1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--micropython", default=os.environ.get("MICROPYTHON") or shutil.which("micropython")
    )
    ap.add_argument("--wasm", default=str(ROOT / "poc/in/core.wasm"))
    ap.add_argument(
        "--spec", action="store_true", help="also run a subset of the converted spec suite"
    )
    ap.add_argument("names", nargs="*", help="spec files (default: a subset)")
    a = ap.parse_args()
    if not a.micropython:
        sys.exit("no micropython binary: --micropython BIN or $MICROPYTHON")
    work = Path(tempfile.mkdtemp(prefix="mpy_pyc_"))
    shim = work / "shim"
    (shim / "waboros/targets/pyc").mkdir(parents=True)
    for p in ("waboros", "waboros/targets", "waboros/targets/pyc"):
        (shim / p / "__init__.py").write_text("")
    (shim / "waboros/targets/base.py").write_text(BASE)
    (shim / "struct.py").write_text(STRUCT)
    (shim / "os.py").write_text(OS)
    rt = (ROOT / "src/waboros/targets/pyc/runtime.py").read_text()
    # FIX: int.bit_count / int.bit_length do not exist on MicroPython
    rt = rt.replace(
        "def _pc(x):\n    return x.bit_count()", 'def _pc(x):\n    return bin(x).count("1")'
    )
    rt = rt.replace(
        "def _bl(x):\n    return x.bit_length()",
        "def _bl(x):\n    return len(bin(x)) - 2 if x else 0",
    )
    rt = rt.replace("n.bit_length()", "_bl(n)")
    (shim / "waboros/targets/pyc/runtime.py").write_text(rt)
    (work / "driver.py").write_text(DRIVER)
    out = work / "out"
    out.mkdir()
    (out / "core.py").write_text(fix(translate(Path(a.wasm).read_bytes())))
    env = dict(os.environ, MICROPYPATH=f"{shim}:{out}")
    mp = [a.micropython, "-X", "heapsize=1024M"]
    r = subprocess.run(
        mp + [str(work / "driver.py"), "coremark", str(out)],
        env=env,
        capture_output=True,
        text=True,
    )
    print(r.stdout + r.stderr, end="")
    ips = re.search(r"Iterations/Sec\s*:\s*([\d.]+)", r.stdout)
    bad = [c for c in CRCS if c not in r.stdout]
    ok = r.returncode == 0 and "Correct operation validated" in r.stdout and not bad
    print(
        f"[mpy] CoreMark {'PASS' if ok else 'FAIL'}: {ips.group(1) if ips else '?'} it/s, missing {bad}"
    )
    if a.spec:
        cache = next(iter(sorted((ROOT / ".cache/spec").glob("*/json"))), None)
        if cache is None:
            sys.exit("no .cache/spec: run tests/spec/fetch.py")
        names = a.names or SPEC
        for n in names:
            cmds, mods = [], []
            for c in json.loads((cache / f"{n}.json").read_text())["commands"]:
                if c["type"] == "module" and c.get("module_type") == "binary":
                    m = f"{n}_{len(mods)}"
                    (out / f"{m}.py").write_text(
                        fix(translate((cache / n / c["filename"]).read_bytes()))
                    )
                    mods.append(m)
                elif (
                    c["type"] in ("assert_return", "assert_trap", "action")
                    and c["action"]["type"] == "invoke"
                ):
                    ac = c["action"]
                    if any(x["type"] not in ("i32", "i64", "f32", "f64") for x in ac["args"]):
                        continue  # references and vectors: not driven here
                    cmds.append(
                        [
                            c["type"],
                            len(mods) - 1,
                            ac["field"],
                            [[x["type"], x.get("value")] for x in ac["args"]],
                            [[x["type"], x.get("value")] for x in c.get("expected", [])],
                            c["line"],
                        ]
                    )
            (out / f"{n}.json").write_text(json.dumps({"mods": mods, "cmds": cmds}))
        r = subprocess.run(
            mp + [str(work / "driver.py"), "spec", str(out)] + names,
            env=env,
            capture_output=True,
            text=True,
        )
        print(r.stdout + r.stderr, end="")
        ok = (
            ok
            and r.returncode == 0
            and "fail 0" in r.stdout
            and r.stdout.count("fail 0") == len(names)
        )
    shutil.rmtree(work)
    return 0 if ok else 1


sys.exit(main())
