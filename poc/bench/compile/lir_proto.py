"""Throwaway prototype: what an extra IR pass + register allocator would cost in Python.

For each function that poc/w2jit2.py can compile, time:
  W  w2jit2 single-pass codegen (decode + pinning + emission into Asm), as in jit_compile_time.py
  D  decode alone (poc w2py.decode)              -- shared by both designs
  B  build LIR from decoded ops: operand stack -> virtual registers, one tuple per op
  V  liveness intervals (first def .. last use, locals extended over loops they occur in)
  A  linear-scan allocation onto 11 registers (Poletto-Sarkar, spill furthest end)
  R  rewrite: every LIR operand vreg -> ('r', n) or ('s', slot)
Design (a) costs at least W + B + V + A + R - (the lazy-stack bookkeeping W no longer does);
the instruction-selection walk over LIR is assumed to cost what W's emission costs.
Usage: python3 poc/bench/compile/lir_proto.py a.wasm ...
"""
import sys, time, os
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(_ROOT, 'poc')); sys.path.insert(0, _ROOT)
from w2py import parse, decode
from poc.w2jit import Asm, TRAPS
from poc.w2jit2 import JitModule2

NREG = 11


def effect(x, types, ftypes):
    """(pops, pushes) of a non-control op."""
    op = x[0]
    if op in (0x41, 0x42, 0x43, 0x44, 0x20, 0x23, 0x3F, 0xD0, 0xD2): return 0, 1
    if op in (0x21, 0x24, 0x1A): return 1, 0
    if op in (0x22, 0x45, 0x50, 0x40, 0xD1) or 0x28 <= op <= 0x35: return 1, 1
    if 0x36 <= op <= 0x3E: return 2, 0
    if op == 0x1B: return 3, 1
    if 0x46 <= op <= 0x4F or 0x51 <= op <= 0x66: return 2, 1
    if op in (0x67, 0x68, 0x69, 0x79, 0x7A, 0x7B) or 0x8B <= op <= 0x91 or 0x99 <= op <= 0x9F \
            or 0xA7 <= op <= 0xC4 or 0xFC00 <= op <= 0xFC07: return 1, 1
    if 0x6A <= op <= 0x78 or 0x7C <= op <= 0x8A or 0x92 <= op <= 0x98 or 0xA0 <= op <= 0xA6: return 2, 1
    if op in (0xFC08, 0xFC0A, 0xFC0B): return 3, 0
    if op in (0x01, 0xFC09): return 0, 0
    if op == 0x10: p, r = types[ftypes[x[1]]]; return len(p), len(r)
    if op == 0x11: p, r = types[x[1]]; return len(p) + 1, len(r)
    raise NotImplementedError(hex(op))


def bt_arity(bt, types):
    if bt and bt[0] == 'idx': return len(types[bt[1]][0]), len(types[bt[1]][1])
    return 0, len(bt[1])


def build(ins, nlocals, nparams, nres, types, ftypes):
    """LIR: list of tuples (opname, dst_vregs, src_vregs, extra). Vregs 0..nlocals-1 are locals."""
    lir = []; nv = [nlocals]; st = []; ctl = []; loops = []
    def new():
        v = nv[0]; nv[0] += 1; return v
    # ctl entry: [kind, label, result_vregs, stack_height, lir_start]
    ctl.append(['func', 'Lret', [new() for _ in range(nres)], 0, 0])
    dead = 0
    for x in ins:
        op = x[0]
        if dead:
            if op in (0x02, 0x03, 0x04): dead += 1; continue
            if op == 0x0B or op == 0x05:
                if dead > 1:
                    if op == 0x0B: dead -= 1
                    continue
                dead = 0
            else: continue
        if op in (0x02, 0x03, 0x04):
            np_, nr = bt_arity(x[1], types)
            if op == 0x04: c = st.pop(); lir.append(('brz', (), (c,), len(lir)))
            res = [new() for _ in range(nr)]
            ctl.append([op, len(lir), res, len(st) - np_, len(lir)])
            lir.append(('label', (), (), len(lir)))
        elif op == 0x05:
            c = ctl[-1]
            for v, r in zip(st[len(st) - len(c[2]):], c[2]): lir.append(('mov', (r,), (v,), None))
            del st[c[3]:]; lir.append(('jmp', (), (), c[1])); lir.append(('label', (), (), None))
        elif op == 0x0B:
            c = ctl.pop()
            if c[0] == 'func':
                for v, r in zip(st[len(st) - len(c[2]):], c[2]): lir.append(('mov', (r,), (v,), None))
                lir.append(('ret', (), tuple(c[2]), None)); break
            for v, r in zip(st[len(st) - len(c[2]):], c[2]): lir.append(('mov', (r,), (v,), None))
            del st[c[3]:]; st.extend(c[2]); lir.append(('label', (), (), c[1]))
            if c[0] == 0x03: loops.append((c[4], len(lir)))
        elif op in (0x0C, 0x0D, 0x0E, 0x0F):
            cond = st.pop() if op in (0x0D, 0x0E) else None
            depths = x[1] + [x[2]] if op == 0x0E else ([len(ctl) - 1] if op == 0x0F else [x[1]])
            for d in depths:
                c = ctl[-1 - d]
                if c[0] != 0x03:
                    for v, r in zip(st[len(st) - len(c[2]):], c[2]): lir.append(('mov', (r,), (v,), None))
            lir.append(('br' if cond is None else 'brc', (), (cond,) if cond is not None else (), tuple(depths)))
            if op != 0x0D: dead = 1
        elif op == 0x00: lir.append(('trap', (), (), 1)); dead = 1
        elif op == 0x20: d = new(); lir.append(('mov', (d,), (x[1],), None)); st.append(d)
        elif op == 0x21: lir.append(('mov', (x[1],), (st.pop(),), None))
        elif op == 0x22: lir.append(('mov', (x[1],), (st[-1],), None))
        else:
            p, r = effect(x, types, ftypes)
            src = tuple(st[len(st) - p:]) if p else ()
            if p: del st[len(st) - p:]
            dst = tuple(new() for _ in range(r))
            lir.append((op, dst, src, x[1] if len(x) > 1 else None))
            st.extend(dst)
    return lir, nv[0], loops


def liveness(lir, nv, nlocals, loops):
    start = [-1] * nv; end = [-1] * nv
    for i, (_, dst, src, _e) in enumerate(lir):
        for v in src:
            if start[v] < 0: start[v] = i
            end[v] = i
        for v in dst:
            if start[v] < 0: start[v] = i
            if end[v] < i: end[v] = i
    for i in range(nlocals):                       # params are live from entry
        if start[i] >= 0: start[i] = 0
    # operand-stack vregs never live around a back-edge in wasm 1.0 (no loop params here); a
    # local referenced inside a loop must live over the whole loop (inner loops first)
    for ls, le in sorted(loops, key=lambda t: t[1]):
        seen = set()
        for _, dst, src, _e in lir[ls:le]:
            for v in dst:
                if v < nlocals: seen.add(v)
            for v in src:
                if v < nlocals: seen.add(v)
        for v in seen:
            if start[v] > ls: start[v] = ls
            if end[v] < le: end[v] = le
    return start, end


def linear_scan(start, end, nv):
    order = sorted((v for v in range(nv) if start[v] >= 0), key=start.__getitem__)
    free = list(range(NREG)); active = []; loc = [None] * nv; slots = 0
    for v in order:
        s = start[v]
        keep = []
        for w in active:
            if end[w] < s: free.append(loc[w][1])
            else: keep.append(w)
        active = keep
        if free:
            loc[v] = ('r', free.pop()); active.append(v)
        else:
            far = max(active, key=end.__getitem__)
            if end[far] > end[v]:
                loc[v] = loc[far]; loc[far] = ('s', slots); slots += 1
                active.remove(far); active.append(v)
            else:
                loc[v] = ('s', slots); slots += 1
    return loc, slots


def rewrite(lir, loc):
    out = []
    for name, dst, src, e in lir:
        out.append((name, tuple(loc[v] for v in dst), tuple(loc[v] for v in src), e))
    return out


class Probe(JitModule2):
    """jit_compile_time.py's Probe, keeping the per-function outcome."""
    def __init__(s, wasm):
        mod = s.mod = parse(wasm); s.types = mod['types']
        s.fimports = [im for im in mod['imports'] if im[0] == 'func']
        s.nimp = len(s.fimports); s.gtypes = [g[0] for g in mod['globals']]
        a = s.a = Asm(); nf = s.nimp + len(mod['codes'])
        s.flabel = {fi: a.label() for fi in range(s.nimp, nf)}
        s.trap_label = {k: a.label() for k in TRAPS}
        s.host_off = 32 + 8 * len(s.gtypes)
        for l in s.trap_label.values(): a.bind(l); a.emit(0xCC)
        s.okset = []; s.t_ok = 0.0; s.ok_bytes = 0
        for k, ((locs, body), tidx) in enumerate(zip(mod['codes'], mod['funcs'])):
            fi = s.nimp + k; nb, nfix = len(a.b), len(a.fix); t = time.perf_counter()
            try:
                s.FuncGen(s, a, fi, tidx, locs, body).gen()
                s.t_ok += time.perf_counter() - t; s.okset.append(k); s.ok_bytes += len(body)
            except Exception:
                del a.b[nb:]; del a.fix[nfix:]
                a.bind(s.flabel[fi]); a.jmp(next(iter(s.trap_label.values())))
        a.resolve()


def main():
    for path in sys.argv[1:]:
        data = open(path, 'rb').read()
        best = None
        for rep in range(3):                       # best of 3 for every phase
            p = Probe(data); mod = p.mod; types = mod['types']
            ftypes = [im[3] for im in p.fimports] + list(mod['funcs'])
            T = dict(W=p.t_ok, D=0.0, B=0.0, V=0.0, A=0.0, R=0.0); nlir = nvr = spills = 0
            for k in p.okset:
                locs, body = mod['codes'][k]; tidx = mod['funcs'][k]
                params, results = types[tidx]; nl = len(params) + len(locs)
                t0 = time.perf_counter(); ins = decode(body)
                t1 = time.perf_counter(); lir, nv, loops = build(ins, nl, len(params), len(results), types, ftypes)
                t2 = time.perf_counter(); st_, en = liveness(lir, nv, nl, loops)
                t3 = time.perf_counter(); loc, sl = linear_scan(st_, en, nv)
                t4 = time.perf_counter(); rewrite(lir, loc)
                t5 = time.perf_counter()
                T['D'] += t1 - t0; T['B'] += t2 - t1; T['V'] += t3 - t2; T['A'] += t4 - t3; T['R'] += t5 - t4
                nlir += len(lir); nvr += nv; spills += sl
            if best is None: best = T
            else: best = {k2: min(best[k2], T[k2]) for k2 in T}
        kb = p.ok_bytes / 1024; extra = best['B'] + best['V'] + best['A'] + best['R']
        print('%s: %d/%d funcs, %.0f KB of bodies covered, %d LIR ops, %d vregs, %d spill slots'
              % (os.path.basename(path), len(p.okset), len(mod['codes']), kb, nlir, nvr, spills))
        for k2 in 'WDBVAR':
            print('   %s %7.3f s  %6.0f KB/s' % (k2, best[k2], kb / best[k2] if best[k2] else 0))
        print('   W (single pass)      %.0f KB/s' % (kb / best['W']))
        print('   W + B+V+A+R (LIR)    %.0f KB/s   extra = %.0f %% of W'
              % (kb / (best['W'] + extra), 100 * extra / best['W']))


main()
