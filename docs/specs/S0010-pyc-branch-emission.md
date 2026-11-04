---
title: pyc - leaving nested blocks without `_br` ladders
kind: spec
id: S0010
status: draft
superseded_by:
related: [D9, D19, D25, D35, D46, S0002]
owner: maintainer
updated: 2026-11-04
---

# S0010 pyc: leaving nested blocks without `_br` ladders

## Summary
Python has no goto and no labelled `break`. `pyc` leaves a nest of wasm blocks by setting the label
register `_br` and testing it at every enclosing level (`if _br == K: _br = 0`, `if not _br:`).
Every one of those tests is executed, so a `br_table` at the bottom of a deep nest walks the whole
ladder each time. [BENCHMARKS.md](../BENCHMARKS.md), "`br_table` (a dense `switch`)", saw this in
2026-10 (~64 tests per VM operation against 5 for the dispatch tree) and left it as "not done".

**The finding (external experiment, maintainer, 2026-11-04, on `phase5`; to be re-measured in step 0,
not yet a BENCHMARKS.md row).** A line profile of CoreMark on `pyc` found that **31 %** of the
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
    the rules that pay. "Pay" is measured per rule in step 0. CoreMark's `f11` takes the shape of
    `examples/f11_hand.py`: the dispatch goes straight to each case, with no ladder.
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

### Rules and where they can be decided
| rule (bropt) | decided at emission from | what changes in the codegen |
|---|---|---|
| **simplify**: drop guards whose result is known; merge `if _br == K: _br = 0` with `if not _br: B` when 0 cannot arrive; drop a store of a value already held | the arrival set at the frame end (`live`/`tg`, `esc`) | `end_frame`/`propagate` emit `if _br == K: _br = 0` and then put the rest *inside* it when 0 cannot arrive. They emit nothing when the set is `{0}`, and no test when it is `{K}` |
| **chain**: consecutive guards become `if/elif` when no arm can fall through with a value that a later test accepts | the arrival set, plus the set of values each guarded region can end with | a per-region record of the `_br` values set inside it (an extension of `esc`); the next guard opens as `elif` when the condition holds. The whole elif tail is checked, not only its first test (LESSONS entry from the experiment) |
| **to_else**: `if C: X` followed by `if not _br: Y` becomes `if C: X else: Y` when entered with 0 and X never falls through with 0 | the frame end of a wasm `if` without `else` whose then-arm is not live at its end | at `end_frame` of such an `if` frame, the parent's `if not _br:` becomes `else:` of the same `if`. `br_if` already does this (lines 495–499) |
| **sink**: the case code that follows a dispatch tree (a lowered `br_table`) moves into the tree's leaves when its end values make every later guard false | the `br_table` leaves (known when it is emitted), and the case regions emitted later | needs **deferred emission**: the leaves are recorded (line positions and indentation) and each case region, once generated, is moved (re-indented) into its leaf. A region that two or more leaves need is copied when it is small (bropt: ≤ 80 AST nodes); otherwise the leaf keeps `_br = K`. This is the switch lowering of BENCHMARKS.md, "`br_table`" |
| **dse**: drop dead stores of `_br` | backward liveness | partly by construction: with sink and chain, many stores are simply not emitted, and the header `_br = 0` goes when no `_br` remains. A general liveness pass is out of scope unless step 0 shows that dse adds a measurable gain beyond the other rules |

**Sink precondition: the same Python loop (a correctness rule, maintainer 2026-11-04).** A moved or
copied region keeps the meaning of its `break` and `continue` only if it runs inside the same
innermost Python `while` as at its original place. In `bropt.py` this holds by construction: the
dispatch tree and the chain of guards are adjacent statements of one list, so a leaf and the region
share their loop. In the codegen the region is re-indented into a leaf, so it must be checked
explicitly: **no `while` may open between the leaf and the point where the region would otherwise be
emitted** (the leaf's loop stack equals the region's). If one does, the region is not sunk, and the
leaf keeps `_br = K` (the guard path, correct as today). A `break` or `continue` in a sunk region
would otherwise leave or restart a different loop: wrong control flow that a test with only one
level of loops does not see. The precondition is asserted at emission, not only tested; `return`
and `return_call` are unaffected.

**Proposal: the minimal set.** Implement *simplify*, *chain* and *to_else* in one step (they need
only the arrival sets), then *sink* (the deferred emission). The first group is cheap and removes
most guards on the common path (static: 635 → 322 with dse, 492 without). Sink is the hand rewrite
of `f11`, which by itself gave +11 % in the experiment. dse is not implemented unless step 0 shows
that `BROPT_DSE=1` adds more than the noise (±3 %) to the full rule set. The order and the cut are
fixed by step 0's per-rule numbers. A rule that contributes less than the noise on CoreMark and on
the switch VM is not implemented, and that is recorded in BENCHMARKS.md.

The rules are confined to the emission of `_br` in `br_code`, `end_frame`, `propagate` and
`br_table`, plus one helper for moving a line region. No new module, no AST, no IR change: pyc
emits text from the wasm structure, as today (D17 is unaffected; HIR/LIR are not involved).

### A knob for bisection and for the differential check
A module global `BRRULES` in `codegen.py`, read from `WABOROS_PYC_BR` the way `BRTREE` reads
`WABOROS_BT` (line 13). It takes a comma-separated rule list; the default is every implemented
rule, and `0` (or empty) gives exactly today's emission. Tests and `tools/fuzz.py` set the global
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
* the max indentation depth (LESSONS 22) of every generated spec, corpus and CoreMark source must not
  increase, and moving a region into a leaf must not exceed the CPython limit of 100 levels or
  the 20 statically nested blocks (LESSONS 2). A move that would cross the limit is not made.

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
  per leaf).

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
     memory, export by export, between two configurations.
3. **Shape**: a test asserts that the source of functions without `_br` under `BRRULES = 0` is
   byte-identical with the default, for CoreMark and the corpus. A one-off command in the step's
   report checks the same over every spec module. A second test asserts, as a regression guard
   rather than a correctness proof, that CoreMark's `_br` count is at most the step's recorded value
   and that `f11` contains no `_br == ` test.
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
deterministic arguments from the seed; a mismatch saves `bad_<seed>.wasm`. It compares two `pyc`
configurations in one process: default against `BRRULES = 0`, or (step 0, before the knob exists)
`pyc` against `pyc` + `bropt.py`. It is used for this spec's acceptance. Comparing a JIT or Wasmtime
with it, and fuzzing for hours as a project-wide campaign, stay in phase 6 unless the maintainer
says otherwise. Random modules rarely nest deeply (in the experiment 15 % were touched), so the
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
  with `BRRULES = 0`):
  * a `switch` with fall-through between cases; cases that end in `br` out of the whole switch, in
    `return`, in `unreachable` and in `br` to an enclosing loop;
  * `br_table` in both lowerings (`BRTREE` tree and `if/elif` chain), with a default inside the nest,
    with repeated targets (copied leaves) and with a target that is a loop;
  * blocks with results and with parameters (multi-value), so that the assignments of `rv` before a
    branch are kept in the moved region;
  * `br_if` to outer blocks with results; wasm `if` with and without `else`, with a then-arm that
    branches out (to_else);
  * a nest deeper than the copy limit, and a nest close to LESSONS 22 (90+ levels), where no move may
    cross 100 levels;
  * an escape through two python loops (`break`, then the loop's `if _br:` ladder);
  * `return_call` inside a moved region (`_TC` wrapper);
  * CoreMark `f11`: the shape test above.

  A planted defect must fail at least one test: the chain rule checking only the first test of the
  appended tail (the experiment's real bug), sink moving a region whose end value is accepted by
  a later guard, and sink moving a region across a `while` (a `break` in the region then leaves the
  wrong loop; the test needs a region with `break`/`continue` and a loop between leaf and region).
* **Speed** (step 0 and the final step; BENCHMARKS.md): CoreMark with alternated rounds against the
  commit before, on CPython 3.11, 3.13 and 3.14, plus one pair on PyPy 3.11 to check it does not
  regress; the switch VM `poc/bench/switch/vm.wasm` through the package's `pyc`; the corpus timings;
  `generate()` time and source size.

## Plan of steps
Steps run on a quiet machine for every measurement: no other agent runs a spec suite or a benchmark
at the same time.

| step | content | agent | acceptance | size (lines) |
|---|---|---|---|---|
| 0 | **Bring in and re-measure.** (a) The experiment goes to `poc/pyc-bropt/` unchanged: `bropt.py`, `site/`, `examples/`, its `tools/` and the write-up as `README.md`. Its absolute `/tmp` path in `run_src.py` is replaced, and `tools/corpus_case.py` resolves the repository root itself. (b) `tools/fuzz.py`, adapted: argument parsing, the configurations of "The fuzz harness", `--fbits`, the bounds. (c) Measurements with the pass applied through `PYC_BROPT=1` and the `PYTHONPATH` hook: CoreMark baseline against the full rule set, alternated, ≥ 5 rounds each, on CPython 3.11, 3.13 and 3.14, plus one PyPy 3.11 pair. Then the per-rule contribution on 3.13: the cumulative order simplify → +chain → +to_else → +sink, ± dse, and each rule left out of the full set. Also the switch VM and the corpus timings; `generate()` time, source size and max indentation for CoreMark, the corpus and the spec modules; the line profile (`tools/lines.py`) before and after. (d) Correctness of the pass in the repository: spec suite in both FBITS modes, corpus, `tools/fuzz.py 1000 1000` | runner (measurements); implementer (a, b); orchestrator checks the numbers itself | the experiment's claims confirmed or corrected in BENCHMARKS.md; the per-rule table fixes the rule set and order of steps 1–3 (Q1). Fuzz 0 mismatches; spec_diff empty | `tools/fuzz.py` ~130; PoC copied (~950, not package code); docs |
| 1 | **Arrival sets: simplify, chain, to_else.** `BRRULES`/`WABOROS_PYC_BR`; per-frame and per-region value sets; `end_frame`/`propagate` emit merged, chained and `else` forms; the header `_br = 0` only when needed | implementer, with a plan approved by the orchestrator first; reviewer after | Test plan "every step"; `BRRULES = 0` output byte-identical to before (sha256 of `generate()` for CoreMark and every spec module); functions without `_br` byte-identical; unit tests except sink's; CoreMark `_br` count ≤ step 0's figure for the same rules; compile budget; CoreMark alternated vs step 0's baseline, recorded | codegen ~120, tests ~200 |
| 2 | **Sink (switch lowering).** Leaf records at `br_table`; each case region is moved or copied into its leaves when the sink preconditions hold, with the copy limit and the indentation limit | implementer, plan approved first; reviewer after (control-flow emission is high-stakes for `pyc`, as the experiment's bug showed) | as step 1, plus the sink unit tests and the planted defects; `f11` has no `_br == ` test; CoreMark alternated on 3.11, 3.13, 3.14 ≥ step 0's "full set" minus noise, or the gap explained | codegen ~150, tests ~150 |
| (3) | **dse**, only if step 0 shows it pays beyond the noise: drop `_br` stores that no guard can read, decided where they are emitted | implementer | as step 1; BENCHMARKS row | ~60 |
| 4 | **Review and close.** A `reviewer` reads steps 1–3 against this spec without the implementers' reasoning; final measurements; `bropt.py` over the new source (remaining `_br`, information); docs; this spec `implemented` | reviewer, then orchestrator | findings closed; BENCHMARKS rows complete; check_docs passes | docs |

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
    phase 6; the project-wide campaign (every target and Wasmtime, fuzz hours as an exit criterion)
    stays in phase 6. Phase 2's "frozen … correctness fixes only" gets a line that S0010 is the
    maintainer's exception for speed;
  * TESTING.md: the tool in the tools table, its bounds and what it compares;
  * CLAUDE.md "Commands": `uv run python tools/fuzz.py N SEED` (maintainer's file, proposed only);
* steps 1–3: BENCHMARKS.md (compile time and CoreMark rows); TARGETS.md (the `pyc` description of
  control flow, if it describes the ladder); LESSONS.md for any trap found; this spec's Changes;
* step 4: this spec `implemented`; `docs/specs/README.md`; DECISIONS.md, only if the maintainer's
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
* **Range tests on ordered label ids** (BENCHMARKS.md, "`br_table`": number the labels so that one
  `if _br <= d` replaces the two tests per level): it halves the tests but keeps the walk, and it is
  not measured. It is subsumed by simplify + chain (one test per level, then elif) and by sink (no
  walk). Not proposed.
* **A flat state machine** (`while True:` + label variable + one `if/elif` level, the Relooper
  fallback of LESSONS 22): it removes nesting entirely, but each dispatch costs a loop iteration plus
  a chain of tests. It is a remedy for depth, not for speed. Out of scope.
* **CPython-version-specific emission** (e.g. forms that only 3.14's specialiser favours): one
  source per interpreter would multiply the test matrix and the future cache key. Rejected (Q5).

## Open questions
* **Q1 The rule set.** Recommendation: simplify + chain + to_else (step 1) and sink (step 2); dse only
  if step 0 shows it adds more than the noise. The final cut follows step 0's per-rule table: a rule
  below the noise on both CoreMark and the switch VM is dropped and recorded as measured and
  rejected.
* **Q2 Copying regions into several leaves.** Sink may copy a small case region into more than one
  leaf. bropt.py's limit was 80 AST nodes and was not tuned. Recommendation: the same limit,
  expressed in generated lines (about 10). It is measured in step 2 only if the source-size budget
  is hit.
* **Q3 `pyc` source identity.** The repository has no stored `pyc` identity list. D46 and S0009 use
  an ad-hoc check: sha256 of `generate()` for CoreMark and every spec module is unchanged. This spec
  breaks that by design for functions that use `_br`. Recommendation:
  * during steps 1–3, the identity check is `BRRULES = 0` (byte-identical to today) plus "functions
    without `_br` are byte-identical";
  * after step 4, later specs take the new default output as their baseline: the baseline is
    re-taken, and no list is created.
* **Q4 Keep the `WABOROS_PYC_BR` knob after step 4?** Recommendation: keep it, like `WABOROS_BT`. It
  is the cheapest way to bisect a future control-flow bug by mixing the old and new emission
  (CLAUDE.md, "Debug by mixing a known-good and a new code generator"), and the fuzz tool's second
  configuration. The cost is that today's ladder code stays and is tested. Alternative: delete it in
  step 4 and keep only `bropt.py` in `poc/` as the oracle.
* **Q5 CPython-version-specific emission.** Recommendation: no. One source for every interpreter
  (3.11–3.15, PyPy, MicroPython flavour), as now.
* **Decided (maintainer, 2026-11-04): the fuzz tool comes now.** `tools/fuzz.py` enters at step 0 as
  a dev tool for this spec's acceptance (bounded modules, `pyc` against `pyc`). The ROADMAP records
  that the tool exists before phase 6, and the project-wide campaign stays in phase 6.

## Changes
* 2026-11-04: created (draft).
* 2026-11-04: the sink precondition "same Python loop" stated as a correctness rule, asserted at
  emission, with a planted defect (maintainer's review).
