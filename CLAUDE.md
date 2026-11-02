# CLAUDE.md

Context and rules for working on w2py with an agent.

**Start with [`BRIEF.md`](BRIEF.md)**: what the project is, what it is not, and the order of work.

## What this is
A WebAssembly **compiler in pure Python** that emits machine code at run time (`jit-<arch>`, through
`mmap` + `ctypes`), with translation to Python (`pyc`) and MicroPython (`mpy`) where machine code is not
possible, and a thin runtime around it. No native dependency. Wasmtime is the test oracle and, later,
the shape of the public API — not the starting point. Read `BRIEF.md`, then `docs/BENCHMARKS.md` and
`docs/ARCHITECTURE.md`; the order of work is in `BRIEF.md`, "Where to start".

The maintainer communicates in Ukrainian; code, comments, commit messages and docs are in English.

## Hard rules
* **No runtime dependencies.** Only the standard library may be imported by the package. Development
  tools (wasmtime as the test oracle, wabt, wasm-tools, pywasm, meld, MicroPython, QEMU) stay in the
  development group or CI.
* **Wasmtime is the oracle, not the goal.** Where the spec leaves room (trap codes, NaN, limits) do
  what Wasmtime does, and test against it. The public API takes the shape of wasmtime-py in phase 6;
  API parity is never worked on before the compiler and the spec tests need it. Record deliberate
  deviations in `docs/DECISIONS.md`.
* **One interface for all targets**, one test suite for all targets. A target never special-cases
  another; the runtime never special-cases a target beyond `Target` methods.
* **Never crash the interpreter.** Deep recursion, bad memory accesses and host exceptions become
  traps or exceptions, on every target.
* **Long calls stay interruptible and do not starve other threads.** JIT code polls the interrupt
  word; `pyc` code has yield points; one thread per store (`docs/ARCHITECTURE.md`, "Long calls and
  the GIL").
* **The JIT artifact is a contract** (`docs/ARTIFACT.md`): any change to the format, the context ABI
  or the ISA policy bumps its version, updates the document and `docs/DECISIONS.md`, and adds a test
  that an old artifact is refused or still runs. Portable artifacts never use instructions above the
  architecture's baseline without a declared level.
* **The IR stays thin** (`docs/ARCHITECTURE.md`, "The IR"): HIR shared by all targets, LIR shared by
  all JIT architectures, structured, no SSA. An optimisation is added only with a measurement showing
  it pays. The shared layers stay language-neutral: the integer model is a target parameter, nothing
  Python-specific in HIR/LIR (DECISIONS.md D17).
* **Measure, do not estimate.** Speed claims come with a CoreMark run (or another benchmark) and go
  into `docs/BENCHMARKS.md` with the conditions; correctness claims come with tests.
* **Semantics first.** A faster code generator that fails a spec test is not faster.

## How work is organised: one orchestrator, one subagent per step

The main session is the **orchestrator**. It does not implement steps itself: it chooses the next
step, writes the brief, hands it to a subagent, **verifies** the result and fixes what the subagent
got wrong. Every step of implementation, investigation or measurement is done by a subagent with a
fresh context, so the main session keeps the whole picture and stays small.

### Which model does what

| role | model | agent (`.claude/agents/`) | typical steps |
|---|---|---|---|
| **orchestrator** (main session) | **Opus** | — | pick the next step from the metric, write briefs, verify, fix small mistakes, keep the docs true, ask the maintainer |
| implementer | **Sonnet** | `implementer` | the spec-failure loop (a group of failing commands → fixed), porting PoC code into the package, the front end, the validator, `pyc`, runtime objects, tests |
| JIT engineer | **Opus** | `jit-engineer` | machine-code generation, the context ABI, registers across calls, the signal handler, a new architecture, bugs that need bisecting |
| reviewer | **Opus** | `reviewer` | an independent read of a high-stakes diff (JIT, ABI, artifact format, traps), with a fresh context and without the implementer's conclusions |
| runner | **Haiku** | `runner` | run the spec suite / benchmarks and report numbers, group failures, update tables in `docs/BENCHMARKS.md`, mechanical edits |

The heavy model plans and checks; the code is written by the model that writes small, direct code;
mechanical work goes to the cheapest. Opus implements only where Sonnet's result would not be
trusted (the JIT), and then always with a plan approved first and a review after.

### A step, from start to end

1. **Choose** (orchestrator). The next step comes from the metric — the largest group of failing spec
   commands for the current target — or from `BRIEF.md`, "Where to start". One step = one goal that a
   command can confirm. If it cannot be stated that way, it is not a step yet.
2. **Brief** (orchestrator), using the template below. A subagent knows only what the brief says.
3. **Do** (subagent). Works only inside the brief's scope. If the step needs more than the scope or
   the budget, **stops and reports** instead of widening it.
4. **Report** (subagent): what changed (files, line counts), the exact commands run and their
   output, numbers before/after, what is left, questions. No claims without the command that shows
   them.
5. **Verify** (orchestrator) — never accept a report as it is:
   * run the acceptance commands **itself** and compare with the report;
   * read the diff: inside the scope? under the budget? no new dependency, no abstraction without a
     second real use, no public-API work before its phase, no unrelated edits or reformatting?
   * docs: `docs/BENCHMARKS.md` for any speed claim, `docs/DECISIONS.md` for any decision,
     `docs/LESSONS.md` for a trap found on the way, the expected-failures file updated;
   * contracts (context ABI, artifact format, Target interface, IR, traps, the engine interface) need an
     approved spec in `docs/specs/` before implementation; docs follow `docs/STANDARDS.md`; the step's
     commit message carries goal, acceptance commands and numbers; `python3 tools/check_docs.py` must pass;
   * for JIT, ABI, artifact-format or trap changes: a `reviewer` subagent reads the diff with no access
     to the implementer's reasoning.
6. **Fix** (orchestrator). A small mistake (≲ 20 lines, obvious) is fixed in the main session. Anything
   larger goes back to a subagent with a precise list of what is wrong; a second failure on the same
   point is escalated to the maintainer.
7. **Close**: one commit per step (what and why, the numbers), then the next step.

### The brief (template)

```
Goal:        <one sentence, as a result a command shows: "spec group i64.* passes on pyc">
Context:     <what exists, which files; what was tried and ruled out>
Read first:  <the 1–3 documents or files that matter; not the whole docs/>
Scope:       <files/directories that may change>      Out of scope: <what must not change>
Budget:      <~N lines of diff; stop and report if more is needed>
Acceptance:  <exact commands + expected result, e.g. pytest -k "i64 and pyc" → 0 failures;
              CoreMark still passes its self-check; BENCHMARKS row added if speed changed>
Docs:        <the documents this step makes untrue and must update (docs/STANDARDS.md, "Which
              document to update"): spec Changes, TARGETS/TESTING/FEATURES, BENCHMARKS, LESSONS…;
              "none" only with the reason>
Report:      files changed, commands run with output, numbers before/after, docs updated,
             open questions
```

### Against over-engineering (applies to every agent)

* The smallest change that makes the acceptance command pass. No code "for later".
* No new module, class, layer, plugin point or config option unless the brief names the need; no
  abstraction before its second real use.
* Do not touch the public API, the CLI or WASI before their phase, even "while you are there".
* No rewrite of working code that the step does not require; no drive-by reformatting.
* When unsure between two designs, implement neither: report both with their costs.

## Working on the code
* The PoC in `poc/` is the reference for how things were done and measured; it is not the package.
  Port from it deliberately (phase 0), keep it runnable for comparisons.
* JIT work: read `docs/RISKS.md` (R2, R3) and `docs/LESSONS.md` items 1–9 first (flags, callee-saved registers across host calls,
  alignment, nested entries, zero-extension). Debug by mixing a known-good and a new code generator
  per function and bisecting — it is how the `printf` bug was found.
* Before implementing something hard (an operator's exact semantics, an ABI, an encoding, a new
  architecture), look at how the projects in `docs/REFERENCES.md` do it. Read and understand, write
  our own — never translate their code line by line; name the source of an idea in a comment.
* Every new opcode or feature: spec tests for it pass on every target that claims it, plus a
  differential run against wasmtime.
* `pyc` codegen: keep generated code valid for the minimum Python (see `docs/ROADMAP.md`, open
  question 1) and for the MicroPython flavour.

## Commands
```
uv sync                                    # dev environment
uv run pytest -q                           # package tests
uv run python tests/spec/fetch.py          # fetch and convert the pinned spec suite
uv run python -m tests.spec.run --engine {wasmtime,pyc,jit-x86_64,auto}   # the spec metric
uv run python tools/spec_diff.py pyc jit-x86_64 --allow-unsupported       # gap between two targets
uv run python tools/run_coremark_pyc.py poc/in/core.wasm   # CoreMark on the package's pyc
uv run python tools/run_coremark_jit.py poc/in/core.wasm   # CoreMark on the package's jit-x86_64
python3 tools/check_docs.py                # documents follow docs/STANDARDS.md
python3 tools/features.py --check          # feature table is consistent
uv run python tools/stack_info.py          # thread stack bounds the JIT uses
uv run python tools/isa_all.py [--target jit-aarch64]   # ISA gate over every compiled spec module, claimed directories or not
uv run python tools/jit_dump.py --target jit-aarch64 OUT.txt   # sha256 list for the D35 cross-compile diff
uv run python tools/isa_all.py             # ISA gate: every JIT blob has only x86-64-v1 instructions (needs GNU objdump)
uv run python tools/no_exec.py -- <cmd>    # run <cmd> with executable memory forbidden (Linux >= 6.3, MDWE)
```

PoC (for comparisons):
```
cd poc
python3 run_jit2.py in/core.wasm           # JIT, CoreMark
python3 w2py.py in/core.wasm out/core.py   # translate (+ out/core.pyc)
python3 run_pyc.py out/core.pyc
python3 ref_run.py in/core.wasm wasmtime   # the reference (wasmtime-py installed)
python3 ref_run.py in/core.wasm wasm3
```
