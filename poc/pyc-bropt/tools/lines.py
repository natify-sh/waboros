"""python tools/lines.py core.py: execution count of every source line of a generated CoreMark run
(sys.monitoring LINE events) and the share of _br guards, loads, stores, loop control."""
import sys, time, collections, io, re
src = open(sys.argv[1]).read(); lines = src.splitlines()
code = compile(src, "<pyc>", "exec", optimize=2); ns = {"__name__": "m"}; exec(code, ns)
t0 = time.perf_counter(); out = io.StringIO()
ex = ns["instantiate"]({"wiring": {"millis": lambda: int((time.perf_counter()-t0)*1000)&0xFFFFFFFF, "print": lambda p,n: out.write(bytes(ns["M"][p:p+n]).decode())}})
cnt = collections.Counter()
mon = sys.monitoring; T = 3
mon.use_tool_id(T, "lines")
def on_line(code, line):
    if code.co_filename == "<pyc>": cnt[line] += 1
mon.register_callback(T, mon.events.LINE, on_line)
mon.set_events(T, mon.events.LINE)
ex["setup"]()
mon.set_events(T, 0)
tot = sum(cnt.values())
cat = collections.Counter()
for ln, c in cnt.items():
    s = lines[ln-1].strip()
    k = ("wrapchk" if s.startswith("if ") and "0xFFFFFFFF:" in s or "+= 0x100000000" in s else
         "br-flag" if "_br" in s else
         "store" if s.startswith("P") else
         "loop-ctl" if s in ("continue","break","while True:") else
         "load" if re.search(r"U\d+\(M|S\d+\(M|M\[", s) else "other")
    cat[k] += c
print("total line events", tot)
for k, c in cat.most_common(): print(f"{k:9s} {100*c/tot:5.1f} %")
print("--- top lines")
for ln, c in cnt.most_common(25): print(f"{100*c/tot:5.2f}%  {ln:5d}  {lines[ln-1].strip()[:110]}")
