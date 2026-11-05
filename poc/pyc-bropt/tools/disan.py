"""python tools/disan.py core.py f11,f3: run CoreMark, then the adaptive (specialised) bytecode of the
named functions: opcode counts and every instruction CPython left generic, with its source line."""
import sys, time, io, dis, collections
src = open(sys.argv[1]).read()
ns = {"__name__": "m"}; exec(compile(src, "<pyc>", "exec", optimize=2), ns)
t0 = time.perf_counter(); out = io.StringIO()
ex = ns["instantiate"]({"wiring": {"millis": lambda: int((time.perf_counter()-t0)*1000)&0xFFFFFFFF, "print": lambda p,n: out.write(bytes(ns["M"][p:p+n]).decode())}})
ex["setup"]()
print([l for l in out.getvalue().splitlines() if "Iterations/Sec" in l])
tot = collections.Counter()
for fn in sys.argv[2].split(","):
    f = ns[fn]; c = collections.Counter()
    for ins in dis.get_instructions(f, adaptive=True):
        c[ins.opname] += 1
    tot.update(c)
    print(f"== {fn}: {sum(c.values())} instrs")
    for k, v in c.most_common(28): print(f"   {v:5d} {k}")
print("---- generic (unspecialized) instructions by source line")
for fn in sys.argv[2].split(","):
    f = ns[fn]
    lines = src.splitlines()
    prev = None
    for ins in dis.get_instructions(f, adaptive=True):
        if ins.opname in ("BINARY_OP", "COMPARE_OP", "BINARY_SUBSCR", "STORE_SUBSCR", "TO_BOOL", "CALL"):
            ln = ins.positions.lineno
            print(f"{fn}:{ln:5d} {ins.opname:14s} {ins.argrepr:6s} | {lines[ln-1].strip()[:90]}")
