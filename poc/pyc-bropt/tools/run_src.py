"""python tools/run_src.py core.py [--prof]: run a pyc-generated CoreMark source as waboros load() does;
prints OK/BAD (CoreMark self-check) and it/s. Generate the source with waboros.targets.pyc.translate."""
import sys, time, re, io, contextlib, os, tempfile
src = open(sys.argv[1]).read()
code = compile(src, "<pyc>", "exec", optimize=2)
ns = {"__name__": "m"}
exec(code, ns)
t0 = time.perf_counter()
out = io.StringIO()
def millis(): return int((time.perf_counter() - t0) * 1000) & 0xFFFFFFFF
def pr(p, n): out.write(bytes(ns["M"][p:p + n]).decode(errors="replace"))
ex = ns["instantiate"]({"wiring": {"millis": millis, "print": pr}})
sys.setrecursionlimit(100000)
if "--prof" in sys.argv:
    import cProfile, pstats
    prof = os.path.join(tempfile.gettempdir(), "pyc-bropt-prof.out")
    cProfile.run('ex["setup"]()', prof)
    pstats.Stats(prof).sort_stats("tottime").print_stats(14)
else:
    ex["setup"]()
s = out.getvalue()
ok = "Correct operation validated" in s or ("crcstate      : 0x8e3a" in s and "crclist       : 0xe714" in s and "crcmatrix     : 0x1fd7" in s)
m = re.search(r"Iterations/Sec\s*:\s*([\d.]+)", s)
print("OK" if ok else "BAD", m.group(1) if m else "?", "it/s")
if not ok: print(s)
