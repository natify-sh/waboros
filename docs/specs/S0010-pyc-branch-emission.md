---
title: pyc - leaving nested blocks without `_br` ladders
kind: spec
id: S0010
status: implemented
superseded_by:
related: [D9, D19, D25, D35, D46, S0002]
owner: maintainer
updated: 2026-11-05
---

# S0010 pyc: leaving nested blocks without `_br` ladders

## Summary
Python has no goto and no labelled `break`. `pyc` leaves a nest of wasm blocks by setting the label
register `_br` and testing it at every enclosing level (`if _br == K: _br = 0`, `if not _br:`).
Every one of those tests is executed, so a `br_table` at the bottom of a deep nest walks the whole
ladder each time. [BENCHMARKS.md](../BENCHMARKS.md), "`br_table` (a dense `switch`)", saw this in
2026-10 (~64 tests per VM operation against 5 for the dispatch tree) and left it as "not done".

**The finding (external experiment, maintainer, 2026-11-04, on `phase5`; re-measured in step 0:
BENCHMARKS.md, "`pyc`: `_br` ladders (S0010 step 0)").** A line profile of CoreMark on `pyc` found that **31 %** of the
executed lines were `_br` guards. Most of them come from CoreMark's state machine (`f11`,
`core_state_transition`), where a `br_table` dispatches into about 14 nested blocks. Rewriting that
one function by hand gave **+11 %** on all of CoreMark. A post-pass over the generated AST
(`bropt.py`: forward dataflow of the possible values of `_br`, rules *simplify*, *chain*,
*to_else*, *sink*, *dse*) brought the guards down to 5 % of executed lines, with these results:

| claim (external experiment, CPython 3.13, one core, alternated) | value |
|---|---|
| CoreMark | ~109 → **~123 it/s (+13 %)** |
| corpus timings (`tools/run_corpus.py`) | no change beyond noise |
| spec suite on `pyc`, every claimed directory | identical: 63 230 pass / 0 fail / 94 xfail; `tools/spec_diff.py` reports no difference either way |
| corpus correctness | 21/21 |
| differential fuzz (`wasm-tools smith`, `pyc` against `pyc` + pass) | 1 124 modules, 175 rewritten, 0 mismatches |
| cost of the pass | 0.36 s on CoreMark (3 900 lines), 0.6 s on `zip.wasm`; the rest of `pyc`'s compile takes ~110 ms |

The experiment recommends putting the rules into `codegen.py`, which knows the control structure as
it emits it and needs no parse/unparse, and keeping `bropt.py` as the reference and the oracle. This
spec does that. One correction to the experiment's cost note: in the package `pyc` has **no code
cache** (D46, point 5; D9 is not built for `pyc` yet). A post-pass would cost 0.3–0.7 s on every
load, not once per module.

**Static numbers checked while drafting** (2026-11-04, this repository at the S0009 step 2b commit, CPython
3.13.18, `bropt.py` unchanged; the counts are occurrences of `_br` in the source, and say nothing
about speed):

| module | `_br` before | after `bropt.py` | pass time | max indentation (levels) |
|---|---|---|---|---|
| CoreMark (`poc/in/core.wasm`, 3 900 lines; 12 of 30 functions use `_br`) | 635 | 235 | 0.40 s | 14 → 13 |
| corpus `hash.wasm` (2 578 lines) | 60 | 15 | 0.28 s | 9 → 9 |
| corpus `mono.wasm` (4 749 lines) | 64 | 12 | 0.61 s | 10 → 10 |
| corpus `zip.wasm` (6 237 lines) | 1 244 | 576 | 0.68 s | 30 → 30 |

These times include interpreter start-up (~20 ms). `translate()` of CoreMark took 41 ms on the same
run. On CoreMark, the rule subsets (`BROPT_RULES`, then `BROPT_DSE`) leave this many `_br`
occurrences:

| rules | dse off | dse on |
|---|---|---|
| simplify | 587 | 531 |
| simplify, chain | 569 | 498 |
| simplify, to_else | 510 | 369 |
| simplify, sink | 562 | 459 |
| simplify, chain, to_else | 492 | 322 |
| all four | 461 | 235 |

A parse/unparse round trip alone leaves the bytecode of all 30 CoreMark functions identical
(CPython 3.13), so the experiment's numbers measure the rules, not `ast.unparse` reformatting. Its
output also compiles on CPython 3.11.

## Goals / Non-goals
* Goals:
  * `pyc` emits, directly in `codegen.py`, control flow equivalent to what `bropt.py` produces for
    the rules that pay. "Pay" was measured per rule in step 0 and its follow-up: **sink**, plus the
    `_br` stores that are evidently dead at emission (maintainer, 2026-11-05; Q1). CoreMark's `f11`
    takes the shape of `examples/f11_hand.py`: the dispatch goes straight to each case, with no ladder.
  * The behaviour of every module is unchanged: the spec suite, the corpus and the differential fuzz
    agree with today's emission. Functions whose source has no `_br` today come out byte-identical.
  * The CoreMark gain is re-measured and recorded (step 0: the pass; the final step: the codegen).
    The added compile time stays within the budget below.
  * The generated source stays valid for CPython 3.11+ (D19), for PyPy 3.11, and for the
    MicroPython flavour, as it is today (no new syntax). It is the same for every CPython version.
* Non-goals:
  * other `pyc` speed ideas from the experiment (range compares on compact ints, `memoryview`
    casts, local aliases, the wrap form). The ideas measured and rejected only go into BENCHMARKS.md
    (step 0);
  * `mpy`-specific work beyond staying valid (phase 7);
  * the JITs. They have real jumps; their D35 lists do not change;
  * fixing LESSONS 22 (100 indentation levels). The switch lowering may reduce depth, but it is not
    promised and not a goal (step 0 records the max depth);
  * a general AST optimisation pass in the package (Alternatives), and the project-wide
    differential-fuzzing campaign (phase 6, ROADMAP).

## Design

### What the codegen emits today (`src/waboros/targets/pyc/codegen.py`, at the S0009 step 2b commit)
* `FuncGen.prepass` (line 278) marks, for every block, whether it is a branch target and whether all
  its branches come from its own top level.
* `FuncGen.gen` (line 384) chooses a mode for each frame (`Ctl`, line 67):
  * a `block` without results that is targeted only from its top level is `nest`. Its branches turn
    the rest of the block into `if not cond:`, with no `_br`;
  * a targeted `loop` is `loop` (`while True:`); an untargeted one is `inline`;
  * every other `block` and every `if` is `flag`.
* `br_code` (line 315) emits a branch:
  * to the function: `return`;
  * to the innermost python loop: `continue`;
  * otherwise `_br = <frame id>`, followed by `break` when a python loop lies in between.

  It records the id in `esc` of that loop or of the current frame, and returns whether control left
  by a python jump.
* `br` (line 476), `br_if` (line 484) and `br_table` (line 526) use `br_code`:
  * `br_if` opens `if cond:` and, when the branch only set the flag, nests the rest under `else:`;
  * `br_table` builds a balanced `if idx < k:` tree above `BRTREE` runs (`WABOROS_BT`, default 4),
    otherwise an `if/elif` chain, and calls `br_code` at each leaf.
* `end_frame` (line 633) closes a frame. When the frame is its own escape target, it emits
  `if _br == id: _br = 0`, or a bare `_br = 0` when nothing else is pending. `propagate` (line 357)
  then routes the remaining escapes:
  * in a python loop: `if _br: [if _br == id: _br = 0; continue] break`;
  * otherwise: `if not _br:` around the rest of the parent frame.

  Each frame end therefore costs up to two tests, which is the ladder.
* `return` (line 581) is emitted directly. The function header gets `_br = 0` when `uses_br` is set
  (line 619).

**The invariant the design rests on**: outside the guards that `end_frame` and `propagate` emit,
every statement executes with `_br == 0`. At each frame end the codegen knows the values that can
arrive: `0` when the frame is `live` at its end (or `tg`), plus the ids in the frame's `esc`. That
is the information `bropt.py` recovers by dataflow, available here without one, at every point
where today's code emits a guard.

### The rule set (revised by the maintainer, 2026-11-05)
Step 0 and its follow-up (BENCHMARKS.md, "`pyc`: `_br` ladders (S0010 step 0)") measured each of
`bropt.py`'s rules. Sink alone gives the whole gain: the switch VM ×2.72 with sink only against ×2.71
with all four rules, CoreMark +10.1 % against +11.4 % (10 rounds, CPython 3.13; the difference is below
the noise). simplify, chain and to_else, each left out of the full set, change neither benchmark beyond
the noise. With sink alone, dse matters on the switch VM (×2.72 with, ×2.36 without). So:

| rule (bropt) | in this spec |
|---|---|
| **sink**: the case code that follows a dispatch tree (a lowered `br_table`) moves into the tree's leaves | **step 1**, by deferred emission (below) |
| **dse**: drop dead stores of `_br` | **step 1, only the stores that are evidently dead at emission** (below). A general liveness pass is not built; if the evident rule turns out not to be enough (or not trivial), it becomes its own step with its own measurement |
| simplify, chain, to_else | measured and rejected (Alternatives) |

The changes are confined to the emission of `_br` in `br_code`, `br_table` and `end_frame`, plus a
helper that splices lines into the line list. No new module, no AST, no IR change: `pyc` emits text
from the wasm structure, as today (D17 is unaffected; HIR/LIR are not involved).

### Sink at emission
Terms. A **leaf** is an arm of a lowered `br_table` (the `BRTREE` tree or the `if/elif` chain)
whose branch only sets the flag: `br_code` emitted `_br = K` with no `break` (the target frame T has
id K and no Python loop lies between), or emitted nothing (depth 0: T is the innermost frame, which
the leaf reaches with `_br == 0`; today `close` writes `pass` there). The **case region** of T is the
code emitted after T's end in its parent P, up to P's end. Today it runs behind
`if _br == K: _br = 0` and the `if not _br:` that `propagate` opens, after the ladder of every frame
between the leaf and T.

1. **Record, at `br_table`.** For each leaf: the position of its `_br = K` line (or, for a depth-0
   leaf, the position where the arm's `pass` will be), its indentation, T, the innermost Python loop
   (`Ctl` with `pyloop`) at that point, and the position where the dispatch tree ends. Every branch to
   a non-loop frame counts its arrivals (`br_code`: stores with or without `break` and depth-0
   exits), so that at T's end the codegen knows whether the leaves are the only way in.
2. **Capture, at `end_frame` of T.** T is a candidate when it is a `block`, P is a `block` or a
   `loop` (not an `if`, whose arms end at `else`, and not the function), T is not live at its end,
   and its recorded leaves are all its arrivals. The codegen emits exactly as today and notes on P
   where T's guard starts, where the region's body starts and the body's indentation.
3. **Decide, at `end_frame` of P**, after P's nests are closed and before a loop's `break` and the
   pop. The captures of P are taken latest first (a later capture lies inside an earlier one's
   region). A region is sunk when all of these hold:
   * **the same Python loop** (below), asserted;
   * **its end values make every later guard false.** If the region may fall through with 0 (P is
     live at its end, or a branch left P structurally with 0), nothing may remain between the end of
     the leaf's dispatch tree and T's guard: earlier sinks have cut those regions, so falling out of
     the leaf reaches the code that followed the region before. Otherwise the region leaves only by
     `_br = J` to P or a frame around it (every test in between is false for J: they name
     frames inside P or test for 0), by `return`/`raise`, or by `break`/`continue` of the same loop, and no
     condition is needed;
   * **the copy limit** (Q2): with n leaves, the body's lines × (n − 1) ≤ 10 (`SINK_COPY`), and a body
     that holds a leaf record not yet resolved is not copied;
   * **the indentation limit** (below).

   Then the body is re-indented (its indentation stripped, the leaf's prefixed) and replaces each
   leaf's `_br = K` line, or the leaf's `pass`, or is inserted at an empty leaf; positions recorded
   after a splice point shift by the difference, and a leaf record inside the moved body takes its new
   depth. What goes from the original place is T's guard (dead: every `_br = K` store was a leaf) and
   the region. Between them `propagate(T)` routed the escapes that pass through T: when P is not a
   Python loop it emitted only the `if not _br:` that guards the region (those escapes are in P's
   `esc` and P's end routes them, after the cut), so everything from T's guard on goes; when P is a
   Python loop it emitted live routing (`if _br: … continue / break`) for escapes from Python loops
   inside T, and that routing stays (review of step 1: cutting it left the loop with `_br` set).
   Otherwise nothing changes: the region stays behind its guard, as today, and the leaves keep
   `_br = K`.

The emitter stays a flat list of indented lines. When P ends, every frame opened after the dispatch
tree is closed, so no open frame's mark lies after a leaf and `close`'s empty-body test is unchanged
(asserted, and for `dse`'s deletions too). A spliced line contains no newline (asserted). The `local.set` peephole that rewrites the
last emitted line cannot reach into a cut, because a region's values do not survive P's end on the
stack.

**Sink precondition: the same Python loop (a correctness rule, maintainer 2026-11-04).** A moved or
copied region keeps the meaning of its `break` and `continue` only if it runs inside the same
innermost Python `while` as at its original place. In `bropt.py` this holds by construction: the
dispatch tree and the chain of guards are adjacent statements of one list, so a leaf and the region
share their loop. In the codegen the region is re-indented into a leaf, so it must be checked
explicitly: **no `while` may open between the leaf and the point where the region would otherwise be
emitted** (the leaf's loop stack equals the region's). A leaf whose branch needed `break` is never
recorded, so the leaf keeps `_br = K; break` (the guard path, correct as today). At the decision the
innermost loop recorded with the leaf must be the innermost loop around P (`is`); a difference is a
codegen bug and raises `AssertionError` at translation, not a silent fallback. A `break` or
`continue` in a sunk region would otherwise leave or restart a different loop: wrong control flow
that a test with only one level of loops does not see. `return` and `return_call` are unaffected.

**The indentation limit (LESSONS 22).** A sunk body moves to the leaf's depth, which is usually
deeper than the region's. The move is not made when any of its lines would land deeper than
`SINK_MAXIND` = 90 levels. The limit that applies was measured on every supported interpreter
(LESSONS 87; BENCHMARKS.md, "`pyc`: `_br` ladders (S0010 step 1)"): for this shape (nested `if`/`else`
arms, one-line `if`s, inside up to 19 `while` loops) every CPython 3.11–3.15, free-threaded included,
compiles and runs 99 levels, in the main thread and in a thread with a 256 KiB stack; PyPy 3.11
passes 139. The compiler's recursion only binds for `elif` chains of hundreds of arms, and sink never
lengthens a chain (the chain lowering has at most `BRTREE` groups). 90 leaves 9 levels of margin; a
sink that would pass it is not made, and the leaf keeps `_br = K`, so sink never turns a module that
compiles into one that does not. It adds no `while` (the same-loop rule): the static loop nesting
(LESSONS 2) is unchanged, asserted by a test over CoreMark, the corpus and the switch VM.
Step 0 counted +1 level in the switch VM and 38 spec modules, but on `ast.unparse` output, which splits
the codegen's one-line `if c: x`; against the base after the same round trip only 3 spec modules grow
(4 → 5 levels; BENCHMARKS.md). Acceptance: the max depth of CoreMark, every corpus module and the
switch VM does not increase; spec modules whose depth grows are listed in the step's report; no line
beyond 90.

### Dead `_br` stores evident at emission
The codegen records the position of every `_br = J` store emitted without `break` (it shifts with
splices). At `end_frame` of J, before its guard, it scans the line list backwards from the end to J's
first store for a line that tests `_br`: **a store of J with no test of `_br` after it in the line
list is dead.** From such a store control only moves forward to
J's end (no Python loop lies between, or the store would carry a `break`), passes no test, and J's end
clears `_br` to 0; without the store `_br` is already 0 there (the invariant above), so every later
statement sees the same value. The store line is deleted, or becomes `pass` when it was its arm's only
line. When every store of J is dead, J leaves `esc` and its guard is not emitted. A test in a sibling
arm that the store's path never reaches still counts: the rule is conservative and needs no flow
analysis. The header `_br = 0` is emitted only when some `_br` remains in the function. This is the
case of the switch VM after sink: the cases' `_br = 5` exits reach block 5's end with no test between,
so the expected result is no `_br` at all, as with `bropt.py`'s dse.

### A knob for bisection and for the differential check
A module global `BRRULES` in `codegen.py`, read from `WABOROS_PYC_BR` the way `BRTREE` reads
`WABOROS_BT` (line 13). It takes a comma-separated rule list, `sink` and `dse` (the evident dead
stores; it works with or without sink, for bisection); the default is both, and `0` (or empty) gives
exactly today's emission. Tests and `tools/fuzz.py` set the global
directly. The knob is internal: it is not public API and not a `Config` field. With `0`, the output
must stay byte-identical to the code before this spec, which keeps the "old" side of every
differential check available in the package itself (Q4).

### Python 3.11, PyPy and MicroPython
The new emission uses only constructs the codegen already emits: `if/elif/else`, `while True:`,
`break`, `continue`, `return` and `pass`. It has no version branch: the same source on every
interpreter (Q5). Checks:
* the CI matrix (3.11–3.15, 3.15t, PyPy 3.11) runs the spec suite on `pyc` as today;
* step 0 and the last step compile the generated sources of CoreMark and the corpus with
  `micropython` where it is installed. The unix port is not in CI before phase 7, so this is a
  manual check recorded in the step's report;
* the max indentation depth (LESSONS 22): no move puts a line deeper than `SINK_MAXIND` (90 levels),
  so the CPython limit of 100 levels and the 20 statically nested blocks (LESSONS 2) are not reached
  by sink; the depth of CoreMark, the corpus and the switch VM does not increase, and spec modules
  that grow are listed (Design, "The indentation limit").

### The FBITS mode (D46)
The rules concern only control flow, so the `fb` mode changes only the value expressions inside the
moved or copied regions. Both modes are checked: the spec suite with `WABOROS_PYC_FBITS=1` (as CI
does), the corpus and the fuzz run in both modes (`tools/fuzz.py --fbits`). D46's point 5 (a future
`pyc` cache key includes `fb`) gains nothing new. The knob does not need to be part of a cache key,
because production always uses the default.

### Compile-time budget
`translate()` of CoreMark takes ~41–53 ms today (BENCHMARKS.md, "Compile and load times"; 41 ms
measured while drafting), and a cold load ~111 ms. Budget:
* the new emission adds **at most 10 % to `generate()` time** on CoreMark and on `zip.wasm` (the
  largest corpus module and the deepest nest, 1 244 `_br`), median of 5;
* the generated source **does not grow** by more than 5 % in bytes (copies made by sink are bounded
  per leaf). `bropt.py` with sink only grew CoreMark by 5.8 %, mostly by copying guard arms that the
  codegen does not emit; if the budget is still hit, `SINK_COPY` is lowered and that is measured (Q2).

Both are measured in step 0 (today's values) and after each implementation step, and recorded in
BENCHMARKS.md, "Compile and load times".

### How equivalence is checked
1. **Oracle and reference**: `bropt.py` lives in `poc/pyc-bropt/` with the experiment's tools and
   write-up (step 0). It is not imported by the package or by its tests. It runs through
   `PYTHONPATH=poc/pyc-bropt/site` and `PYC_BROPT=1`, as in the experiment.
2. **Behaviour, three ways.** The new codegen is compared with the old codegen (`BRRULES = 0`), and
   with the old codegen plus `bropt.py`. Inputs:
   * **spec**: `tests.spec.run --engine pyc --json` before and after, and `tools/spec_diff.py`
     (empty both ways), in both FBITS modes;
   * **corpus**: `tools/run_corpus.py --targets pyc`, 21/21 against the references;
   * **fuzz**: `tools/fuzz.py` over a fixed seed range. It compares results, trap codes and a hash of
     memory, export by export, between the three configurations (below).
3. **Shape**: a test asserts that the source of functions without `_br` under `BRRULES = 0` is
   byte-identical with the default, for CoreMark and the corpus. A one-off command in the step's
   report checks the same over every spec module. A second test asserts, as a regression guard
   rather than a correctness proof, that CoreMark's `_br` count is at most the step's recorded value
   (`bropt.py` with sink only and its dse leaves 517) and that `f11` has no `_br == K` test for a frame
   K that its state machine's `br_table` targets (its exits after the loop, `_br == 3` and `_br == 4`,
   stay: sink does not remove them).
4. **The `bropt.py` oracle on our output**: running `bropt.py` over the new source should find
   little to do. Its remaining `_br` count is recorded per step. A large gap means a rule missed a
   case; it is information, not a gate.

### The fuzz harness (decided by the maintainer, 2026-11-04)
`tools/fuzz.py` comes into the repository in step 0 as a dev tool, adapted from the experiment's
`tools/fuzz.py`. It runs `wasm-tools smith --ensure-termination` (wasm-tools is already a dev
dependency, `wasm-tools-package==1.262.0.0`) on modules bounded as in the experiment:
* 1 MiB memory, 1 000 table elements, 3–12 functions, 3 000 instructions;
* SIMD, GC, exceptions, threads and memory64 off;
* a 20 s alarm per module.

Unbounded smith modules got the process OOM-killed. Every export is called three times with
deterministic arguments from the seed; a mismatch saves `bad_<seed>.wasm`. In step 0 it compared
`pyc` against `pyc` + `bropt.py`. From step 1 it compares three `pyc` configurations in one process:
the default (the new emission), `BRRULES = 0` (today's emission), and `BRRULES = 0` + `bropt.py` (the
oracle); all three must agree, and it also reports how many functions each changed. It is used for
this spec's acceptance. Comparing a JIT or Wasmtime
with it is a small step right after step 0 (the nightly non-blocking CI job, ROADMAP "Next",
maintainer 2026-11-04); the project-wide campaign with WASI stays in phase 6. Random modules rarely nest deeply (in the experiment 15 % were touched), so the
corpus stays the stronger test of control flow, and the unit tests below add deep nests by hand.

## Test plan
* **Every step**:
  * `uv run pytest -q`; `python3 tools/check_docs.py`; `python3 tools/features.py --check`;
  * `uv run python -m tests.spec.run --engine pyc --json after.json`, with no difference against the
    JSON of the commit before the step (`uv run python tools/spec_diff.py before.json after.json` and
    the reverse). The same with `WABOROS_PYC_FBITS=1`;
  * `uv run python tools/run_corpus.py --targets pyc --rounds 1`: every case agrees;
  * `uv run python tools/fuzz.py 1000 1000` (1 000 modules from seed 1 000) with 0 mismatches, and
    `--fbits` over 300 modules;
  * the JIT D35 lists (`tools/jit_dump.py`, both targets) unchanged, since the JIT is not touched.
* **Unit tests** (`tests/test_pyc_branches.py`, small WAT modules, each compared with Wasmtime and
  with `BRRULES = 0`; where a test is about shape, it also asserts that sink happened or did not, by
  the `_br` count of the function):
  * a `switch` (nested blocks with a `br_table` innermost) whose cases end in `br` out of the whole
    switch, in `return`, in `unreachable` and in `br` to an enclosing loop (`continue`);
  * fall-through between cases (a region that falls into the next case): the next region must stay
    in place or be reached the same way;
  * `br_table` in both lowerings (`BRTREE` tree and `if/elif` chain), with a default inside the nest,
    with a depth-0 target (the empty leaf), with repeated targets (copies within and beyond
    `SINK_COPY`) and with a target that is a loop;
  * a case block that is also reached by a `br`/`br_if` from outside the `br_table` (no sink);
  * blocks with results and with parameters (multi-value), so that the `rv` assignments before a
    branch stay in the leaf and the region reads them;
  * a `switch` inside a loop whose regions use `break`/`continue`, and a `br_table` inside a loop
    inside the case block (the leaf needs `break`: no sink);
  * a nested switch inside a case region (a region that holds leaf records of its own);
  * a nest close to LESSONS 22 (90+ levels), where no move may go past `SINK_MAXIND`;
  * an escape through two python loops (`break`, then the loop's `if _br:` ladder);
  * `return_call` inside a moved region (`_TC` wrapper);
  * dse: a store followed by J's end only (removed), and one with a test of `_br` after it (kept);
  * CoreMark `f11` and the switch VM: the shape test above.

  A planted defect must fail at least one test: (1) the arrival count ignored (a region sunk although
  T is also reached by a branch that is not a leaf); (2) the end-value condition ignored (a region that
  falls through with 0 sunk while an unsunk region lies between the tree and it); (3) sink across a
  `while` (a leaf that needs `break` recorded; the test needs a region with `break`/`continue` and a
  loop between leaf and region, and the assertion of the precondition removed as well); (4) a region
  with an unresolved leaf record copied; (5) dse ignoring a test of `_br` after the store.
* **Speed** (step 0 and the final step; BENCHMARKS.md): CoreMark with alternated rounds against the
  commit before, on CPython 3.11, 3.13 and 3.14, plus one pair on PyPy 3.11 to check it does not
  regress; the switch VM `poc/bench/switch/vm.wasm` through the package's `pyc`; the corpus timings;
  `generate()` time and source size.

## Plan of steps
Steps run on a quiet machine for every measurement: no other agent runs a spec suite or a benchmark
at the same time.

| step | content | agent | acceptance | size (lines) |
|---|---|---|---|---|
| 0 | **Bring in and re-measure** (done 2026-11-05). (a) The experiment goes to `poc/pyc-bropt/` unchanged: `bropt.py`, `site/`, `examples/`, its `tools/` and the write-up as `README.md`. Its absolute `/tmp` path in `run_src.py` is replaced, and `tools/corpus_case.py` resolves the repository root itself. (b) `tools/fuzz.py`, adapted: argument parsing, the configurations of "The fuzz harness", `--fbits`, the bounds. (c) Measurements with the pass applied through `PYC_BROPT=1` and the `PYTHONPATH` hook: CoreMark baseline against the full rule set, alternated, ≥ 5 rounds each, on CPython 3.11, 3.13 and 3.14, plus one PyPy 3.11 pair. Then the per-rule contribution on 3.13: the cumulative order simplify → +chain → +to_else → +sink, ± dse, and each rule left out of the full set. Also the switch VM and the corpus timings; `generate()` time, source size and max indentation for CoreMark, the corpus and the spec modules; the line profile (`tools/lines.py`) before and after. (d) Correctness of the pass in the repository: spec suite in both FBITS modes, corpus, `tools/fuzz.py 1000 1000` | runner (measurements); implementer (a, b); orchestrator checks the numbers itself | the experiment's claims confirmed or corrected in BENCHMARKS.md; the per-rule table fixes the rule set and order of steps 1–3 (Q1). Fuzz 0 mismatches; spec_diff empty | `tools/fuzz.py` ~130; PoC copied (~950, not package code); docs |
| 1 | **Sink and evident dead stores.** `BRRULES`/`WABOROS_PYC_BR` (`sink`, `dse`); leaf records at `br_table`; arrival counts in `br_code`; captures at `end_frame` of a case block and the decision at its parent's end (same loop asserted, end values, `SINK_COPY`, `SINK_MAXIND`); the splice helper with position shifts; dead stores removed at `end_frame` of their target; the header `_br = 0` only when `_br` remains; `tools/fuzz.py` with the three configurations | implementer, with a plan approved by the orchestrator first; reviewer after (control-flow emission is high-stakes for `pyc`, LESSONS 84) | Test plan "every step"; `BRRULES = 0` output byte-identical to before (sha256 of `generate()` for CoreMark and every spec module); functions without `_br` byte-identical; the unit tests and the planted defects; `_br` counts recorded for CoreMark (≤ 517, `bropt.py` sink only with dse), the switch VM (0 expected) and `zip`; the `f11` shape test; max depth (Design); compile and size budgets; CoreMark alternated, ≥ 10 rounds, on 3.11, 3.13 and 3.14 ≥ the follow-up's "sink only" (+10.1 % on 3.13) minus noise, or the gap explained; the switch VM per the follow-up's conditions | codegen ~250 (stop and report past 350), tests ~350, `tools/fuzz.py` ~40 |
| (2) | **General dse**, only if step 1's evident rule leaves a measured gap: the switch VM or CoreMark below `bropt.py` with sink only and dse by more than the noise (10 alternated rounds), or a `_br` count well above it with a line profile showing the stores executed. Its own spec text first (a backward liveness over the emitted structure) | implementer, plan approved first; reviewer after | as step 1; BENCHMARKS row | measured first; ~100 |
| 3 | **Review and close.** A `reviewer` reads steps 1–2 against this spec without the implementers' reasoning; final measurements; `bropt.py` over the new source (remaining `_br`, information); docs; this spec `implemented` | reviewer, then orchestrator | findings closed; BENCHMARKS rows complete; check_docs passes | docs |

A step that needs more than its size stops and reports. **Docs per step** (STANDARDS.md, "Which
document to update"):
* step 0:
  * BENCHMARKS.md: a new section "`pyc`: `_br` ladders (S0010)" with the measured rows and the
    per-rule table, the experiment's "measured and rejected" table (the `memoryview` cast, local
    aliases, the wrap forms, CPython's experimental JIT) and the CPython 3.13/3.14/3.15 numbers for
    the same source, each re-measured or labelled as from the external experiment with its date.
    The "`br_table`" section gets a line that it is taken up by S0010;
  * LESSONS.md: the experiment's lesson (a `pyc` control-flow rewrite needs the corpus and a
    differential fuzz, not only the spec suite; the chain-rule bug that only `inflate 8K` caught);
  * ROADMAP.md: a differential-fuzzing **tool** (`tools/fuzz.py`, `pyc` against `pyc`) exists before
    phase 6, and a nightly non-blocking CI job (findings triaged, not fixed at once) follows step 0;
    the project-wide campaign with WASI stays in phase 6. Phase 2's "frozen … correctness fixes only" gets a line that S0010 is the
    maintainer's exception for speed;
  * TESTING.md: the tool in the tools table, its bounds and what it compares;
  * CLAUDE.md "Commands": `uv run python tools/fuzz.py N SEED` (maintainer's file, proposed only);
* steps 1–2: BENCHMARKS.md (compile time, source size, CoreMark and switch VM rows); TARGETS.md (the
  `pyc` description of control flow, if it describes the ladder); TESTING.md (the fuzz tool's three
  configurations); LESSONS.md for any trap found; this spec's Changes;
* step 3: this spec `implemented`; `docs/specs/README.md`; DECISIONS.md, only if the maintainer's
  answers to Q1–Q5 are decisions worth recording (Q4 and Q5 are candidates).

## Alternatives considered
* **The post-pass in the package** (`bropt.py` after `generate()`): already written and verified
  against the oracle. But it costs 0.3–0.7 s per load against ~110 ms for the whole compile today,
  and every load pays it, since `pyc` has no cache (D46, point 5). It adds a second representation
  (the Python AST) and a general pass to keep correct, rebuilding knowledge the codegen already has.
  Rejected unless step 0 shows that a rule which matters needs the full dataflow. Even then, the
  pass would be confined to that rule and gated by the compile budget.
* **Leave as is**: zero risk, and `pyc` keeps losing about an eighth of CoreMark to the ladders (the
  external claim; step 0 confirms or corrects it). `pyc` is the only engine on i686, armv7, ppc64le,
  s390x, loongarch and mips, and the fallback where native code is forbidden. If step 0 measures
  less than +5 % on CoreMark and nothing on the switch VM, this is the recommendation and the spec
  is closed as `superseded`, with no successor.
* **simplify, chain and to_else in the codegen (the old step 1). Measured and rejected (maintainer,
  2026-11-05).** They need only the arrival sets at frame ends and would have been the cheaper step.
  But sink subsumes their gain: on the switch VM (CPython 3.13, 10 rounds) simplify ×1.20, + chain
  ×1.56, + to_else ×1.52, sink alone ×2.72 against ×2.71 for all four; each of the three left out of
  the full set ×2.65-2.73. On CoreMark (10 rounds) the three give +2.6 %, sink alone +10.1 %, all four
  +11.4 % (the difference below the noise). BENCHMARKS.md, "`pyc`: `_br` ladders (S0010 step 0)".
  Each would add its own control-flow emission to review and fuzz for a gain the measurements do not
  show.
* **Range tests on ordered label ids** (BENCHMARKS.md, "`br_table`": number the labels so that one
  `if _br <= d` replaces the two tests per level): it halves the tests but keeps the walk, and it is
  not measured. It is subsumed by sink (no walk). Not proposed.
* **A flat state machine** (`while True:` + label variable + one `if/elif` level, the Relooper
  fallback of LESSONS 22): it removes nesting entirely, but each dispatch costs a loop iteration plus
  a chain of tests. It is a remedy for depth, not for speed. Out of scope.
* **CPython-version-specific emission** (e.g. forms that only 3.14's specialiser favours): one
  source per interpreter would multiply the test matrix and the future cache key. Rejected (Q5).

## Open questions
* **Q1 The rule set. Decided 2026-11-04 (maintainer): as recommended; revised by the maintainer
  2026-11-05 (below).** simplify + chain + to_else (step 1) and sink (step 2); dse only
  if step 0 shows it adds more than the noise. The final cut follows step 0's per-rule table: a rule
  below the noise on both CoreMark and the switch VM is dropped and recorded as measured and
  rejected.
  Note (step 0, 2026-11-05): sink carries the CoreMark gain (all four rules +10.7 % over 10 rounds on
  3.13; simplify + chain + to_else +2.6 %, and each of the three left out of the full set changes
  CoreMark by ≤ 1.2 %, below the noise). The switch VM was measured only with the full set (×2.3 on
  3.13), not per rule, so the first group's share there is unknown; whether step 1 stays as planned
  is asked of the maintainer separately.
  **Revised (maintainer, 2026-11-05):** after the switch VM per-rule follow-up (sink alone ×2.72,
  all four ×2.71; CoreMark sink alone +10.1 %, all four +11.4 %), sink is implemented first and alone,
  with the `_br` stores that are evidently dead at emission (no general liveness pass; if that is not
  trivial, it becomes its own step with its own measurement). simplify, chain and to_else are recorded
  as measured and rejected (Alternatives).
* **Q2 Copying regions into several leaves. Decided 2026-11-04: as recommended.** Sink may copy a small case region into more than one
  leaf. bropt.py's limit was 80 AST nodes and was not tuned. Recommendation: the same limit,
  expressed in generated lines (about 10): `SINK_COPY`, the body's lines × (copies − 1) ≤ 10. It is
  measured in step 1 only if the source-size budget is hit.
* **Q3 `pyc` source identity. Decided 2026-11-04: as recommended.** The repository has no stored `pyc` identity list. D46 and S0009 use
  an ad-hoc check: sha256 of `generate()` for CoreMark and every spec module is unchanged. This spec
  breaks that by design for functions that use `_br`. Recommendation:
  * during steps 1–2, the identity check is `BRRULES = 0` (byte-identical to today) plus "functions
    without `_br` are byte-identical";
  * after step 3, later specs take the new default output as their baseline: the baseline is
    re-taken, and no list is created.
* **Q4 Keep the `WABOROS_PYC_BR` knob after step 3? Decided 2026-11-04: keep it.** Recommendation: keep it, like `WABOROS_BT`. It
  is the cheapest way to bisect a future control-flow bug by mixing the old and new emission
  (CLAUDE.md, "Debug by mixing a known-good and a new code generator"), and the fuzz tool's second
  configuration. The cost is that today's ladder code stays and is tested. Alternative: delete it in
  step 3 and keep only `bropt.py` in `poc/` as the oracle.
* **Q5 CPython-version-specific emission. Decided 2026-11-04: no.** Recommendation: no. One source for every interpreter
  (3.11–3.15, PyPy, MicroPython flavour), as now.
* **Decided (maintainer, 2026-11-04): the fuzz tool comes now.** `tools/fuzz.py` enters at step 0 as
  a dev tool for this spec's acceptance (bounded modules, `pyc` against `pyc`). The ROADMAP records
  that the tool exists before phase 6, and the project-wide campaign stays in phase 6.

## Changes
* 2026-11-04: created (draft).
* 2026-11-04: the sink precondition "same Python loop" stated as a correctness rule, asserted at
  emission, with a planted defect (maintainer's review).
* 2026-11-04: approved by the maintainer with the recommendations (Q1–Q5); S0010 is the
  maintainer's exception to the `pyc` freeze for speed. Implementation after memory64.
* 2026-11-04: fuzzing as a nightly non-blocking CI job, findings triaged (maintainer); text aligned.
* 2026-11-05: step 0 done: the experiment in `poc/pyc-bropt/`, `tools/fuzz.py`, and the measurements
  (BENCHMARKS.md, "`pyc`: `_br` ladders (S0010 step 0)"): CoreMark with the full rule set +12.8 % on
  CPython 3.11, +10.7 % on 3.13 (10 rounds), +6.8 % on 3.14, −3.0 % (noise) on PyPy; sink carries
  the gain, dse adds nothing beyond the noise; the switch VM ×2.2-2.5 on CPython; the pass costs
  10-15× `generate()`. Spec (both FBITS modes), corpus and fuzz (938 + 281 modules) show no
  difference. The MicroPython compile check was not done (not installed). The experiment's +13 % is
  corrected to +11-13 % on 3.13.
* 2026-11-05: plan changed by the maintainer after the switch VM per-rule follow-up (BENCHMARKS.md):
  step 1 is sink plus the dead `_br` stores evident at emission; simplify, chain and to_else moved to
  Alternatives as measured and rejected; general dse becomes conditional step 2; review is step 3.
  Design describes the deferred emission (leaf records, captures, the decision at the parent's end,
  splicing and re-indentation, `SINK_COPY`, `SINK_MAXIND` = 90, the same-loop assertion) and the
  evident dead-store rule; the indentation acceptance is restated (step 0's +1 level was mostly
  `ast.unparse` splitting one-line `if`s); the fuzz tool compares three configurations; unit tests and
  planted defects are those of sink; budgets enlarged. Q1 revised.
* 2026-11-05: maintainer approved the revised limits: depth may grow within the hard cap of 90 levels
  (no growth on CoreMark, the corpus and the switch VM; spec modules that grow are listed), and the
  source-size budget may rise to +8 % if `SINK_COPY` alone cannot keep +5 %, provided nothing breaks
  (spec suite, corpus, three-way fuzz and the planted defects).
* 2026-11-05: depth rule (maintainer): sink never turns a module that compiles into one that does not, on
  any supported interpreter; the limit measured on each (LESSONS 87) and `SINK_MAXIND` = 90 kept.
* 2026-11-05: step 1 implemented (not yet reviewed): sink and the evident dead stores in `codegen.py`
  (+185 lines), `tests/test_pyc_branches.py` (20 tests; the five planted defects each fail one),
  `tools/fuzz.py` three-way. `WABOROS_PYC_BR=0` is byte-identical to the parent. Spec 63 231 / 0 / 93 in
  both FBITS modes, spec_diff empty, corpus 21/21, fuzz 938 + 281 modules with 0 mismatches. CoreMark
  +9.4 % (3.11), +10.0 % (3.13), +5.7 % (3.14); switch VM ×2.47 (3.13), the same bytecode as `bropt.py`
  with sink only. Gaps against the acceptance: CoreMark keeps 538 `_br`, not ≤ 517 (the rest needs
  general liveness or fall-through copies: conditional step 2); `generate()` +9.6-10.6 % on `zip`, at the
  10 % budget. Two spec modules grow by one level (`align.106`, `align64.106`). BENCHMARKS.md,
  "`pyc`: `_br` ladders (S0010 step 1)".
* 2026-11-05: step 1 review: two defects fixed (LESSONS 88): when P is a Python loop the routing that
  `propagate(T)` emitted for escapes through T stays (only T's guard and the region go); a leaf moved
  with its region takes its new depth. The marks assertion behind "(asserted)" added, for `dse` too.
  The oracle `bropt.py` (all four rules) is itself wrong on some generated modules (LESSONS 89); the
  tool counts that apart. CoreMark +10.2 % and the switch VM ×2.50 on 3.13 after the fix.
* 2026-11-05: second review: a third defect fixed (LESSONS 88): when T emits no line and its region is
  empty, the cut removed the `pass` of a closed nest of P (`IndentationError` on a valid module); every
  cut now refills a suite it emptied (`fill`). `tools/fuzz.py --gen` widened to the shapes listed in
  TESTING.md (after the reviewer's generator). Fuzz coverage: the rules change 0.9 % of smith modules
  (8 of 938) and 40 % of `--gen` modules (4 045 of 10 000, 0 mismatches; 421 of 1 000 with `--fbits`,
  0 mismatches); each of the three review defects, re-planted, is found within 2 000 `--gen` modules.
  Eight planted defects, each failing a unit test. Size against the step's budget: codegen +232 lines
  (budget ~250), tests 457 lines (~350), `tools/fuzz.py` +335 / −36 (~40: the structured generator
  and the per-rule comparison were not in the plan). The default output of CoreMark, the corpus,
  the switch VM and the spec modules did not change in the reviews: the speed figures stand.
* 2026-11-05: implemented (maintainer). CI green on every platform. Accepted against the acceptance:
  CoreMark keeps 538 `_br` (target <= 517; speed equals bropt.py sink-only, the rest needs a
  general liveness pass) and `generate()` on zip.wasm is about +10 % (budget 10 %). A general dse
  stays an optional later step, taken only if a measured gap appears.
