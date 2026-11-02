"""Run CoreMark (core.wasm) on the stdlib JIT: python3 run_jit.py core.wasm"""
import sys, time, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from poc.w2jit import JitModule
T0 = time.perf_counter()
jm = None
def millis(): return int((time.perf_counter() - T0) * 1000) & 0xFFFFFFFF
def pr(p, n): sys.stdout.write(bytes(jm.mem[p:p + n]).decode(errors='replace')); sys.stdout.flush()
t = time.perf_counter()
jm = JitModule(open(sys.argv[1], 'rb').read(), {'wiring': {'millis': millis, 'print': pr}})
print('[jit] compile %.1f ms, %d bytes of x86-64' % ((time.perf_counter() - t) * 1000, jm.code_size))
t = time.perf_counter(); jm.call('setup')
print('\n[jit] wall %.2fs' % (time.perf_counter() - t))
