"""python tools/corpus_case.py 'inflate 8K' [reps]: one case of tools/run_corpus.py on
pyc, checked against its reference; prints OK/BAD and the min/median microseconds per call."""
import os
import statistics
import sys
import types

root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))  # the repository
path = os.path.join(root, "tools", "run_corpus.py")
src = open(path).read().rsplit("\nmain()", 1)[0]  # run_corpus runs main() at import
rc = types.ModuleType("rc")
rc.__file__ = path
exec(compile(src, path, "exec"), rc.__dict__)
c = [c for c in rc.cases() if c[1] == sys.argv[1]][0]
call = rc.runner("pyc", (rc.DIR / f"{c[0]}.wasm").read_bytes())
got, _ = rc.run_case(call, c, 1)
ts = [rc.run_case(call, c, 1)[1] for _ in range(int(sys.argv[2]) if len(sys.argv) > 2 else 5)]
print("OK" if got == c[5] else "BAD", f"{min(ts):.1f} us min, {statistics.median(ts):.1f} median")
