---
title: pyc speed - the `_br` label ladders, a dataflow pass that removes them, and the ceiling of CPython
kind: reference
status: draft
updated: 2026-11-04
---

# `pyc` speed: the `_br` ladders, a pass that removes them, and the CPython ceiling

An experiment for the maintainer (2026-11-04), outside the repository, on `phase5` at `58d729f`.
Nothing in the repository was changed. This folder holds the pass (`bropt.py`), the hook that
applies it to `pyc` without editing it (`site/`), the tools used, and an example.

Asked for: the most speed `pyc` can get on CPython without any JIT.

## Result in one table

| | before | after |
|---|---|---|
| CoreMark, CPython 3.13 (alternated, one core) | ~109 it/s | **~123 it/s (+13 %)** |
| corpus (`tools/run_corpus.py`: hashes, zip, crypto) | | **no change** (within noise; deflate 8K re-measured with 15 reps: 49.0 vs 48.3 ms) |
| spec suite on `pyc` (all claimed directories) | 63 230 pass, 0 fail, 94 xfail | **identical**; `tools/spec_diff.py` 0 differences both ways |
| corpus correctness | 21/21 | **21/21** |
| differential fuzz (`tools/fuzz.py`) | | **1 124 modules, 175 rewritten by the pass, 0 mismatches** |

The ceiling of `pyc` on CPython is about ×1.5 of today, not an order of magnitude (below).

## The finding

A line profile (`tools/lines.py`, `sys.monitoring` LINE events over a whole CoreMark run) showed
**31 % of the executed lines were `_br` guards**. Python has no goto and no labelled break, so a
branch out of nested wasm blocks sets `_br = K` and every enclosing level tests it
(`if _br == K: _br = 0` / `if not _br:`). The worst case is CoreMark's state machine
(`core_state_transition`, `f11`): a `br_table` into ~14 nested blocks walks the whole ladder on every
input character. Rewriting that one function by hand (`examples/f11_hand.py`: the dispatch goes
straight to each case) gave **+11 % on all of CoreMark** by itself.

## The pass (`bropt.py`)

A post-pass over the generated Python AST. It computes, at every statement, the set of values `_br`
may hold (forward dataflow, loops to a fixpoint, "not yet bound" at function entry) and rewrites
only with that knowledge. Rules (each with its precondition in the docstring):

* **simplify**: guards with a known outcome go; unreachable statements go; `if _br == K: _br = 0` +
  `if not _br: B` merge when `_br` cannot be 0 on entry.
* **chain**: consecutive `_br` guards become `if/elif` when no arm of the first chain can fall
  through with a value that makes any test of the second chain true.
* **to_else**: `if C: X` + `if not _br: Y` -> `if C: X else: Y` when entered with 0 and X never falls
  through with 0.
* **sink**: a dispatch tree (a lowered `br_table`) followed by a chain of `_br` guards: an arm moves
  into the leaves where its effective predicate holds (small arms may be copied into several), when
  its own end values make the rest of the chain false. This is the hand rewrite, made general.
* **dse**: backward liveness of `_br`; dead stores go.

On CoreMark the `_br` occurrences fall from 635 to 235; `_br` guards fall from 31 % to 5 % of the
executed lines. Functions where `_br` appears in any other form are left alone.

### Its cost

0.36 s for CoreMark's 3 900 lines, 0.6 s for zip.wasm's 6 200 (CPython 3.13), against ~110 ms for the
rest of `pyc`'s compile. Most of it is `ast.parse`/walks/`ast.unparse`. With the cached code object
(`marshal`, BENCHMARKS.md) it is paid once per module. **Recommendation: put the rules in
`codegen.py` itself** (it knows the control structure when it emits it; no parse/unparse), keep
`bropt.py` as the reference and as an oracle for the codegen version (same source in, same `_br`
behaviour out), and gate it like any `pyc` codegen change (next section).

## Lesson (for LESSONS.md)

> **A `pyc` control-flow rewrite needs the corpus and a differential fuzz, not only the spec
> suite.** The first version of the `chain` rule checked only the first test of the chain it
> appended, but that chain brought its own `elif` tail (`_br == 31 ... 16`); after the merge those
> arms were skipped. The whole spec suite passed; the corpus' `inflate 8K` failed (a state machine
> with ~56 labels). Rule: a change to `pyc`'s control-flow emission runs the spec suite, the corpus
> and `tools/fuzz.py` before review. Found: 2026-11-04, this experiment.

## Differential fuzzing is cheap now (for ROADMAP)

`tools/fuzz.py`: `wasm-tools smith --ensure-termination`, every export called with deterministic
arguments, results, trap codes and memory hash compared between two configurations (here `pyc` and
`pyc` + pass). It is a few dozen lines and runs today; the same harness compares `pyc` with a JIT
or with Wasmtime. ROADMAP moves differential fuzzing to phase 6; this one could start earlier.
Bound the modules (`--max-memory32-bytes 1048576`, `--max-table-elements 1000`, a per-module
alarm): unbounded smith modules got the process OOM-killed. Random modules rarely have deep block
nesting (15 % were touched by the pass), so the corpus stays the stronger test for control flow.

## What `dis` shows (CPython 3.13, adaptive bytecode after a run; `tools/disan.py`)

The hot instructions are mostly specialised (`COMPARE_OP_INT`, `BINARY_OP_ADD_INT`,
`BINARY_SUBSCR_TUPLE_INT` for `struct` results, `LOAD_GLOBAL_MODULE`). What stays generic, in the hot
paths:

* every 32-bit wrap check `x > 0xFFFFFFFF` (`COMPARE_OP`): 3.13 specialises int compares only for
  compact ints (|v| < 2**30), and 0xFFFFFFFF is not one;
* every bitwise operation (`&`, `|`, `<<`, `>>`: `BINARY_OP`): 3.13 specialises only `+ - *`;
* every byte access `M[i]` on the `bytearray` (`BINARY_SUBSCR`).

So the commonest wasm operations land on CPython's unspecialised paths. One codegen idea follows,
not measured: where the operand's range is known (the `E.b` bound), a masked compare can become a
range compare on compact ints, e.g. `((x - 58) & 255) <= 245` with a byte `x` is `not 48 <= x <= 57`
(`COMPARE_OP_INT`, no `&`).

## Measured and rejected (for BENCHMARKS.md, so they are not tried again)

| idea | result |
|---|---|
| `memoryview.cast('I')` instead of `struct.unpack_from` (aligned, inside a function) | ~31 vs ~40 ns per load, but needs a runtime alignment check; with the check or with four shifted views (for unaligned addresses) no faster |
| local aliases of `M`, `U32`, `P32`... as default arguments | within noise |
| wrap as `x & 0xFFFFFFFF`, or `if x >> 32:`, or the walrus expression form | all slower than the statement `if x > 0xFFFFFFFF: x -= 0x100000000` (39 vs 41-47 ns) |
| CPython's experimental JIT (3.14, `PYTHON_JIT=1`) | slower: 103 vs 125 it/s |

CPython version, same generated source, CoreMark: 3.13 ~120, **3.14 ~127 (+5 %)**, **3.15b4 ~138
(+14 %)**. Newer CPythons specialise more (bitwise int ops), which is where `pyc` gains for free.

## The ceiling, and where speed has to come from

After the pass, what remains in the hot paths is the real work: loads and stores, 32-bit
arithmetic with its wrap, loop jumps, each already in the cheapest form found. CPython spends
~40-80 ns per wasm operation; a perfect generator would reach about ×1.5 of today (~170-200 it/s on
CoreMark), not thousands. So:

* the architectures where `pyc` is the only engine today (i686, armv7, ppc64le, s390x, loongarch,
  mips) get their speed from a JIT, i.e. the sljit track (SLJIT-BACKEND.md), not from `pyc`;
* PyPy runs the same generated source ~30× faster (BENCHMARKS.md, PoC `pyc` on PyPy);
* where native code is forbidden (iOS), `pyc` stays the fallback; worth measuring against
  JavaScriptCore's wasm interpreter through `jscontext` (py-wasmhost).

## Reproduce

```sh
export WABOROS=<waboros checkout>  P=<this folder>  # corpus_case.py finds the repository root itself
export PYTHONPATH=$P/site:$WABOROS/src:$WABOROS
# CoreMark source, the pass, the run
python -c "from waboros.targets.pyc import translate; open('core.py','w').write(translate(open('core.wasm','rb').read()))"
python $P/bropt.py core.py core_o.py
python $P/tools/run_src.py core.py ; python $P/tools/run_src.py core_o.py
python $P/tools/lines.py core.py            # the line profile (31 % _br guards)
python $P/tools/disan.py core.py f11,f3     # specialised / generic bytecode
# spec, corpus, fuzz with the pass applied to pyc (no repository change)
PYC_BROPT=1 python -m tests.spec.run --engine pyc --json opt.json     # from $WABOROS
python tools/spec_diff.py base.json opt.json
PYC_BROPT=1 python tools/run_corpus.py --targets pyc
PYC_BROPT=1 python $P/tools/corpus_case.py 'inflate 8K' 10
python $P/tools/fuzz.py 300 1000            # needs wasm-tools on PATH
```
