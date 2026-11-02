---
title: Scalar code generation in both JITs (addressing, returns, temporaries)
kind: spec
id: S0007
status: approved
superseded_by:
related: [D14, D25, D26, D35, D39, D44, S0003, S0004, S0006, R2, R3]
owner: maintainer
updated: 2026-11-02
---

# S0007 Scalar code generation in both JITs (addressing, returns, temporaries)

## Summary
`jit-x86_64` scalar code is 1.5–3.9× slower than Wasmtime on the scalar kernels and 38 % slower on
CoreMark. A measured investigation (2026-11-02; Xeon 2.10 GHz, 4 vCPU, CPython 3.13.17, wasmtime-py
50.0.1, alternated rounds, medians; every variant at 54 385 spec passes / 0 fail) traced the gap to
countable instructions and tested each fix as a throw-away patch. This spec makes three of them
contracts, for both JITs:

1. **Addressing (K, L, Z).** `local + const` stays deferred and becomes one `lea`/`add` into the
   address register; add/sub-immediate into `tos` is one `lea`; a register index needs no 32-bit
   copy in explicit mode. Measured together: CoreMark +6.2 % (14 044 → 14 909 it/s), `addsat`
   −27 % (7.55 → 5.51 µs), `scan` −18 % (3.08 → 2.52), `nearest` −19 % (1.77 → 1.44).
2. **Calls.** A result goes straight to slot 0; a parameter read once is not pinned; call-free
   functions drop the checks they do not need. Measured: dropping the value-stack check and entry
   poll (experiment C) gives −0.35 ns per leaf call (2.24 → 1.89) and −0.6 ns per `fib(27)` call
   (3.08 → 2.45); Wasmtime takes 1.43 / 2.59.
3. **Temporaries in free registers (R).** When `tos` spills, the value goes to a pool register no
   local is pinned to, not to a slot; a function saves only the registers it writes. Measured with
   K, L and Z: CoreMark +9.4 % (15 367); `scan` 2.48 µs (Wasmtime 2.14; 2.02 with guard pages and
   no loop polls as well). R alone, saving all 8 pool registers in every prologue, lost 4.6 %.

Item 1 and item 3 change the frozen backend interface (D39); item 2's checks change S0003 and the
context ABI. All three change the code of nearly every module, so byte identity is not the check:
spec parity, D35 cross-host identity, the ISA gates and the benchmarks are.

## Goals / Non-goals
* Goals: the instruction savings above, in `jit-x86_64` and `jit-aarch64` alike (D39: one step
  changes both backends); spec parity with `pyc` (D25) in `explicit` and `guard` mode; every rule of
  LESSONS 1–9, 32 and 44 still holds; each item lands alone, with its own measurement, and is
  dropped if the measurement shows no gain.
* Non-goals: floats in vector registers (ranking item 5; its own spec); x86-64-v2 rounding (D42);
  loop polls (≤ 1–4 %, kept); a register allocator or any new IR layer (D26); results or arguments in
  registers across calls (a call ABI change, see Alternatives); guard pages themselves (S0006);
  `pyc`.

## Interface

### D39 amendment (both backends, one step each)
**A. Operand kind `AddK`** (`common/backend.py`, next to `Imm`, `Reg`, `Slot`):
```python
@dataclass(frozen=True, slots=True)
class AddK:          # the i32 value (register `id` + k) mod 2**32
    id: int          # a pool register holding a zero-extended i32 (a pinned local)
    k: int           # 0 <= k < 2**32
```
It is accepted in exactly three places; the core never passes it anywhere else:
* `move(dst, AddK, wide=False)`: `dst` gets `(id + k) mod 2³²`, zero-extended. Into a `Reg`: one
  instruction (x86-64 `lea r32, [id + k]` with `k` as a signed disp32; aarch64 `add wd, wn, #imm12`
  (optionally `lsl #12`), or `sub` for a negative `k`, or the constant through `x16` and a 32-bit
  register `add`). Into a `Slot`: the same into the backend's scratch, then a 64-bit store. No flags
  are left live, no pool register but `dst` is written.
* `load(..., addr=AddK, offset, ...)` and `store(..., addr=AddK, ...)`: the effective address is
  `((id + k) mod 2³²) + offset`, the 32-bit sum formed in the backend's index scratch (x86-64 `ecx`,
  aarch64 `w9`) by the same single instruction; this replaces today's 32-bit copy of a register
  index, so the access's safety still does not rest on LESSONS 5 (S0006 Q9 holds). The bounds check
  (explicit mode) and the guard threshold (`offset + size ≤ GUARD`, guard mode) are those of a
  register address with the same `offset`: `k` is never folded into `offset`, because wasm wraps the
  address in 32 bits before adding the offset. The memory-touching instruction is unchanged, so a
  fault point is still one instruction with no push outstanding (S0006).

**B. `Reg` may be a temporary.** The contract sentence "`Reg` (`tos` or a pinned pool register)"
becomes "`Reg` (`tos` or a pool register holding a pinned local or a temporary)". Backends already
treat every pool register alike; no method changes.

**C. `func_begin(label, saved, far)`**: the second argument is the *save set* — every pool register
the function writes (pinned locals and temporaries), in a fixed order — not only the pins. The
prologue saves exactly these and keeps the 16-byte alignment (LESSONS 3); the epilogue restores them.

**D. `rewind(pos)` on x86-64.** Today it asserts (x86-64 never asks for far mode). It becomes the
truncation aarch64 already has (code, labels bound after `pos`, pending fixups, fault-map entries,
jump tables), used by the recompile of item 3.

**E. `func_begin(..., leaf: bool = False)`** (only with Q3 as recommended): `leaf=True` omits the
entry poll and, for a frame of at most H slots, both stack checks; `func_end` then patches nothing.

**F. Byte identity.** D39's "x86-64 code byte-identical unless the amendment says otherwise": this
amendment says otherwise for every step below.

Backend-internal, no interface change: **L** (x86-64 `binop` "add"/"sub" with `x` a register other
than `dst` and `y` an immediate that fits a signed disp32 at the operand's width, or `add` of two
registers, emits one `lea`; aarch64 already emits one three-operand `add`/`sub`), and **Z** (x86-64
explicit mode: a `Reg` address is used as the index directly; see Semantics).

### Core (`common/compiler.py`)
* **Entry kind `ADDK`** `(ADDK, (j, k))`: local *j* (an i32 pinned local) plus `k` mod 2³². Formed by
  `i32.add` of `(LOCAL j, CONST c)` in either order (`k = c`), `i32.sub` of `(LOCAL j, CONST c)`
  (`k = −c mod 2³²`), and `i32.add`/`i32.sub` of `(ADDK (j, k), CONST c)` (`k ± c`). It may sit at
  any depth of the operand stack, like `LOCAL`.
* **Consumers.** `load`/`store` address: passed as `AddK(pin_j, k)`. `local.set`/`local.tee` *t*:
  `move(loc(t), AddK)` (one instruction into a pinned *t*). `flush` (edges, calls): `move(Slot, AddK)`.
  `drop`: discarded (it cannot trap). Any other consumer (`pop()`): materialised into `tos` when no
  operand of the current instruction is in `tos` yet (a per-instruction flag `tos_taken`, set when
  `pop()` returns `tos` or a compare holding it, cleared at each instruction), otherwise into its own
  slot. The flag makes LESSONS 32 explicit instead of relying on pop order.
* **`local.set`/`local.tee` of *j*** spills every entry that reads the old *j* — `LOCAL j` as today
  and now `ADDK (j, ·)` — before the write.
* **Entry kind `REG`** `(REG, r)`: a temporary in pool register *r*. `spill_tos` moves `tos` into the
  first free temporary (64-bit `move`) and falls back to the slot when none is free. The free list
  starts as `pool − pins`; a popped `REG` returns to it at the start of the next instruction, and not
  while a pending compare on the stack names it. `flush` moves `REG` entries to their slots and frees
  them, so at every edge and call the operand stack is in slots and only pins are live (unchanged).
* **Save set and the second pass.** A function is compiled with `saved = pins`; if it used
  temporaries, it is rewound and compiled once more with `saved = pins ∪ used` (in pool order). The
  allocation does not depend on the save set, so the second pass uses the same registers and needs
  no third. Far mode (aarch64) composes: at most four passes.
* **Returns.** At the function's `end` and at a `br` to the function's label (`return`), the result
  entries are moved straight into slots `0..m` from their operands (`tos` → slot 0 is one store),
  instead of `flush` to `L + i` and a slot-to-slot copy through scratch. The entries below the
  results are not flushed (they hold computed values; nothing can trap, LESSONS 28). With several
  results, if a source is a slot in `0..m` other than its own destination, the step falls back to
  today's `flush` + `moves`. `br_if` and `br_table` are unchanged.
* **Pins.** `choose_pins` skips a parameter whose weight is 1 (one access, outside every loop): it
  is read from its slot, saving the prologue load and the save/restore of a register.
* **Call-free functions** (no `call`, `call_indirect`, `call_ref`, `return_call*` and no libcall
  service): no entry poll; and, if `frame_slots ≤ H` (H = 256), no value-stack check and no native
  stack check. The core tells the backend through amendment E.

### Context ABI (only with Q3 as recommended; S0003 and ARTIFACT.md part 3)
* `VSTACK_LIMIT` (word 5): "the highest frame base allowed; at least `8·H` = 2 KiB of the value
  stack stay mapped above it". The runtime writes `vtop − 8·H` and checks entries against that.
* `NSTACK_LIMIT` (word 6): "at least 128 bytes of native stack are usable below it" (a call-free
  frame needs ≤ 80: return address, 8 saved registers, pad). Already true of every host that runs
  host calls from the deepest frame; now written down.
* Interrupt polls (S0003, "Interrupt polling"): "at the entry of every function that contains a call,
  and at every loop header". The bound on the time between polls is unchanged: a call-free function
  without loops runs straight-line code of bounded length.

## Semantics

**What S0003 says today.** Every prologue checks `rsp ≥ ctx[NSTACK_LIMIT]` and
`r12 + 8·frame_slots ≤ ctx[VSTACK_LIMIT]` and polls word 2 at entry (x86-64: 7 instructions; aarch64:
11); results go to `frame[0..m)`; pinned locals are callee-saved, a function pushes those it uses.

**Why a call-free function may skip its checks.** Every frame base is at most the caller's
`r12 + 8·frame_slots(caller)`, which the caller checked against `VSTACK_LIMIT`; with `H` slots mapped
above the limit, a callee of at most `H` slots stays inside the map. The runtime's entry check and a
tail call (whose callee frame starts at the caller's base) keep the same bound; every context of a
store holds the same limit. A call-free function cannot recurse, so its native usage is one frame of
≤ 80 bytes below a caller that passed its check. Loops still poll.

**Invariants each item touches.**

| item | invariant | how it holds |
|---|---|---|
| K | zero-extension (LESSONS 5) | `AddK` is formed by a 32-bit `lea`/`add`, which zero-extends; the index never comes from a 64-bit register unmasked |
| K | wasm address wrap | `k` added in 32 bits; `offset` added in 64 bits after it, as today |
| K | guard thresholds, fault points (S0006) | `offset` unchanged; the access instruction unchanged |
| K | old value of a local (set after get) | `local.set` spills `ADDK (j, ·)` as it spills `LOCAL j` |
| K | a popped operand may still be in `tos` (LESSONS 32) | `tos_taken`: a second popped `ADDK` goes to its slot |
| L | flags (LESSONS 1) | `lea` writes no flags; emitted only inside `binop`, never between a compare and its consumer |
| Z | memory safety without LESSONS 5 | explicit mode only: the check `lea rdx, [idx + off + size]; cmp rdx, r14` is 64-bit, so a dirty upper half traps or (wrapping past 2⁶⁴) addresses inside the memory; guard mode keeps the copy (S0006 Q9) |
| R | callee-saved pool (LESSONS 2) | temporaries are in the save set; never live across a call or host call (flushed first), so the bridge is unchanged |
| R | stack alignment (LESSONS 3) | the pad follows the save set's parity |
| R | fault points (S0006) | temporaries are registers: no push in the body |
| R | store forwarding (LESSONS 44) | slots are still written 64-bit; R removes slot round trips, adds none |
| returns | trap order (LESSONS 28, 30) | only computed entries are skipped; multi-value overlap falls back |
| checks | D10, never crash | the H and 128-byte obligations above; recursion needs a call, which keeps every check |

**Portability.** Nothing here uses a Python-specific feature: one entry kind, one flag, a free list
and a recompile flag; a micro-compiler (MICRO-COMPILERS.md) ports it as written. The IR stays as
D26 has it: one pass per function, plus the rare second pass that far mode already has.

**Trap codes and results** are unchanged; only instruction selection changes. Where wasm leaves
nothing open here, there is nothing to match in Wasmtime beyond the spec suite.

## Test plan
Run for every step, on the step's code and on the baseline in the same session:
```
uv run pytest -q                                                     # 0 failures
uv run python -m tests.spec.run --engine jit-x86_64                  # pass count = baseline, 0 fail
WABOROS_JIT_BOUNDS=guard uv run python -m tests.spec.run --engine jit-x86_64   # same, guard mode
uv run python tools/spec_diff.py pyc jit-x86_64 --allow-unsupported  # no new difference
python3 tools/run_arm64.py -m tests.spec.run --engine jit-aarch64 --in-process   # QEMU parity
uv run python tools/isa_all.py && uv run python tools/isa_all.py --target jit-aarch64
uv run python tools/jit_dump.py OUT-x86.txt; uv run python tools/jit_dump.py --target jit-aarch64 OUT-a64.txt
uv run python tools/run_coremark_jit.py poc/in/core.wasm            # self-check; alternated with baseline
uv run python tools/run_simd_bench.py --targets jit-x86_64,wasmtime  # the six scalar kernels
uv run python tools/run_corpus.py --targets pyc,jit-x86_64          # results equal; µs per call
```
* **D35.** The lists of an x86-64 host and an aarch64 runner stay identical (CI). Against the
  baseline's list, a module may change only if it contains the step's pattern (returns: a function
  with results; K: an i32 add/sub of a pinned local and a constant; R: a `tos` spill); a scratch
  script in the step's report checks this, it is not committed.
* **Differential fuzz** `tests/jit/test_scalar_fuzz.py` (new, ~120 lines): fixed seeds generate WAT
  functions with i32/i64 params and locals, `local ± const` addresses with constants near 0, 2³¹ and
  2³² − 1 and offsets up to and past `GUARD`, loads/stores of every width in a one-page memory (so
  some trap), `local.tee` of a pointer increment while an older read of it is on the stack,
  expressions deep enough to exhaust the free registers, `br`/`br_if` with values, multi-value
  returns of swapped locals, calls between the functions, and a function that is call-free with
  more than H slots. Results and trap codes on each JIT the machine has must equal `pyc`'s; the test
  also runs with `WABOROS_JIT_BOUNDS=guard` where guard mode exists.
* **Unit tests** (`tests/jit/test_compiler.py`): an `AddK` load at `k = 2³² − 4` with a pinned local
  0 wraps to `−4` and traps with offset 0, reads address 0 with offset 4; a call-free function with
  `H + 1` slots keeps its checks; a leaf call at the value-stack limit traps `STACK_OVERFLOW`, not a
  fault; interrupts still stop a `loop` in a call-free function and a loop-free recursion.
* **Calls benchmark** `poc/bench/calls/` + `tools/run_calls_bench.py` (new, ~60 lines): the
  investigation's 2-argument leaf in a loop and `fib(27)`, ns per call, alternated with Wasmtime.
* **BENCHMARKS.md**: one row per step (CoreMark, the six kernels, the calls benchmark, corpus),
  baseline alternated in the same session, conditions stated. A step whose gain is within the
  spread of the alternated runs is not merged.

## Plan of steps
Each step is one commit, measured alone against the commit before it; numbers in brackets are the
investigation's.
1. **Results straight to slot 0** (core). Acceptance: the common commands; calls benchmark leaf and
   `fib` faster than baseline; CoreMark not slower.
2. **L** (x86-64 backend only). Acceptance: common commands; aarch64 lists unchanged; CoreMark and
   kernels not slower, at least one faster.
3. **K** (amendment A; core, both backends). Acceptance: common commands, fuzz, unit tests;
   `addsat`, `scan`, `nearest` faster; CoreMark faster [K+L+Z: +6.2 %]; `matmul` not slower (it was
   3.91 → 3.99 with K+L+Z: watch it).
4. **Z** (x86-64 explicit mode only). Acceptance: common commands; kept only if CoreMark or a kernel
   gains beyond the spread in explicit mode (S0006 Q9 measured no gain in guard mode).
5. **Once-used parameters** (core). Acceptance: common commands; calls benchmark leaf faster.
6. **Call-free functions** (amendment E; core, both backends; S0003, ARTIFACT.md part 3, DECISIONS;
   needs Q3 and Q4 answered). Acceptance: common commands, unit tests; leaf faster [C: −0.35 ns].
7. **R** (amendments B, C, D; core, both backends). Acceptance: common commands, fuzz; compile time
   of `run_corpus.py` modules and CoreMark within +10 % of step 6; CoreMark faster [K+L+Z+R: +9.4 %];
   `scan`, `swizzle` faster [2.48, 3.84 µs].
8. **Review** by a `reviewer` (D39) of steps 3, 6 and 7; S0004 and S0003 Changes lines; status
   `implemented`.

## Alternatives considered
* **A register allocator (linear scan over a materialised LIR).** Covers matmul/dot pressure, but D26
  measured +50–80 % compile time for the LIR build and liveness alone; after K, L, Z, R and guard
  pages the remaining gap is mostly floats and calls, which an integer allocator does not close.
  Revisit only with a measurement on real workloads (D26).
* **Liftoff-style register state across edges** (values stay in registers at merges, reconciled per
  label). Removes the slot traffic at loop back-edges; needs merge moves and state per label in the
  core, against "at every edge the stack is in slots". Later, after R shows how much is left.
* **Several fixed `tos` registers** (a register stack). A special case of R with a fixed save cost;
  R's free list costs the same and adapts to the pins.
* **K as a backend peephole.** The backend sees operands, not the stack, so it cannot defer the add
  across the value computation of a `store`; it would split one decision across D26's boundary.
* **K through `load`/`store` parameters** (`addk: int | None`, the experiment's shape) plus a
  separate `lea32` method: two interface additions for one idea; `AddK` is one, and `move` covers
  `local.set` and `flush`.
* **Patching the prologue for R** instead of a second pass: a variable-length push sequence shifts
  the body (every label and fixup must move), or a maximal region padded with executed NOPs; aarch64
  needs both the frame size and the `stp` count patched. More encoder code than the rewind that far
  mode already needs.
* **Saving every free pool register** (the experiment): −4.6 % on CoreMark. Rejected by measurement.
* **A guard page above the value stack** instead of the check: every slot write becomes a fault point
  with its own resume entry. Rejected.
* **Results and arguments in registers** (Wasmtime: 6 instructions for the leaf against our 25).
  Changes trampolines, host thunks, records and the context ABI's entry contract; its own spec.
* **Interrupts through the native stack limit** (V8's `StackGuard`: an interrupt request moves the
  stack limit so the next stack check fails and its slow path tells the two apart). Removes the entry
  poll from every function, call-free or not (`fib` gains; the leaf rule does not reach it), at the
  price of a host obligation on word 6 and a race between the runtime's writes of the limit and an
  interrupt. Open question Q3(c).

## Open questions
* **Q1. Shape of the K interface.** (a) operand kind `AddK` in `move`/`load`/`store`; (b) an `addk`
  parameter on `load`/`store` and a `lea32` method; (c) a method that forms an address into scratch
  and an operand meaning "the formed address". **Recommendation: (a).**
* **Q2. Z.** (a) explicit mode only, kept only on a measured gain; (b) both modes, relying on
  LESSONS 5 in guard mode (reverses S0006 Q9); (c) not at all. **Recommendation: (a)**; with `auto`
  the default (D44) it matters only where guard pages are unavailable, so (c) is the likely outcome.
* **Q3. The prologue's checks** (C measured −0.35 ns leaf, −0.6 ns `fib`). (a) keep all; (b) the
  call-free rule above: no entry poll in call-free functions (S0003 text only) and no stack checks
  in call-free functions of ≤ H slots (word 5 headroom, word 6 slack: context ABI); (c) (b) and the
  entry poll of every other function moved into the native stack check (V8 model; D14's mechanism
  and word 6's meaning change; the runtime writes the limit under the interrupt lock and re-reads
  word 2 after); (d) (b) without the stack-check part (no ABI change, about half the leaf gain).
  **Recommendation: (b)**, H = 256; (c) as a later step with its own `fib`/CoreMark measurement.
* **Q4. Version of the context ABI change.** (a) an amendment of v1 (precedent: S0003, D38; no
  serialized artifact exists before S0006 step 6); (b) `ctx_abi_version` 2 with a test that a v1
  artifact is refused. **Recommendation: (a)** if ARTIFACT.md part 2 is still unbuilt when step 6
  lands, else (b).
* **Q5. R's save set.** (a) a second pass for functions that used temporaries; (b) prologue patching;
  (c) a pre-scan bounding the temporaries. **Recommendation: (a)**, measured on compile time; (c) only
  if (a) costs more than 10 %.
* **Q6. Pinning threshold.** (a) skip parameters of weight 1; (b) skip every local of weight ≤ 1;
  (c) a cost model (pushes vs uses). **Recommendation: (a)**, widened only on a measurement.
* **Q7. Direct result writes.** (a) function returns only; (b) every branch with values (skip the
  flush of entries the branch discards). **Recommendation: (a)**: block ends already land in place;
  (b) after a count of the moves it would save.

## Changes
* 2026-11-02: created (draft), from the scalar-gap investigation (ranking items 1, 3 and 4).
* 2026-11-02: maintainer decisions: Q4, no version bump: the prologue-check change is an amendment
  of context ABI v1 (as S0003 and D38 did), while the artifact format is not fixed yet; Q5 (b): the
  save set of R comes from a second compilation pass, only for functions that used temporaries (the
  allocation does not depend on the save set, so the second pass reproduces the body); Q1–Q3, Q6, Q7
  as recommended. The spec is approved (with the D39 amendment it describes, byte identity waived).
