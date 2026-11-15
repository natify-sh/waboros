---
title: sljit backend under our core - the "first step" of SLJIT-BACKEND.md, done and measured
kind: reference
status: draft
updated: 2026-11-16
---

# sljit backend under our core: measured

The "first step" of SLJIT-BACKEND.md was carried out on 2026-11-04: the D39 backend interface
implemented on sljit's API (`waboros_sljit.py`), driven by **our unchanged one-pass core**
(`common/compiler.py`), running in **our unchanged runtime** (`common/instance.py`, same context
ABI, same host functions, same guard registry), on x86-64. Branch `phase5` at `2458d86` (re-checked at `00d07c8`, below); nothing in
the repository was changed (the backend registers a target `jit-sljit` from outside).

Result: **spec parity and speed parity with our hand-written x86-64 backend.** The stop rule
("below about 70 %: find out why") is not reached; it is at about 100 %.

## Correctness

Core spec suite (`tests/spec/run.py --engine jit-sljit core`), both bounds modes, compared with
`tools/spec_diff.py` in both directions:

| | jit-x86_64 | jit-sljit |
|---|---|---|
| pass | 20 509 | 20 509 |
| fail | 0 (+ 89 xfail) | 89 (exactly the 89 xfails of jit-x86_64; no xfail file for the new target) |
| commands passing on one and not the other | | **0 in either direction, explicit and guard** |

Corpus (`tools/run_corpus.py`): all 21 cases OK on `jit-sljit`, both modes.

### Update: `phase5` at `00d07c8` (S0008: relaxed SIMD and wide arithmetic)

The new D39 method `wide` (add128, sub128, mul_wide_s/u) took about 25 lines on sljit: `ADD`/`SUB`
with `SLJIT_SET_CARRY`, then `ADDC`/`SUBC`, and `LMUL_SW`/`LMUL_UW` (R1:R0 = R0 * R1); the operands
are first copied to the native frame so that nothing between the carry producer and its consumer
can touch the flags. With `jit-sljit` claiming everything `jit-x86_64` claims except SIMD, all
spec directories, both bounds modes:

| directory | jit-x86_64 | jit-sljit |
|---|---|---|
| core | 20 509 pass, 89 xfail | 20 509 pass, the same 89 fail |
| bulk-memory | 7 483 pass, 2 xfail | 7 483 pass, the same 2 fail |
| multi-memory | 912 | 912 |
| wide-arithmetic | 109 | 109 |
| simd, relaxed-simd | 25 481 + 79 | not claimed (no v128 in this backend) |

`tools/spec_diff.py` both ways: 0 commands differ, explicit and guard.

The repository itself at `00d07c8`: `pytest` 461 passed, 73 skipped; spec on `jit-x86_64` 54 573 pass,
0 fail, 91 xfail in both bounds modes (54 385 before + 79 relaxed-simd + 109 wide-arithmetic);
`pyc` passes relaxed-simd 79/79 and wide-arithmetic 109/109. `jit-aarch64` not run here (no QEMU on
this machine).

## Speed

CoreMark, 3 alternated rounds, one core (`taskset`), same process setup (`tools/run_coremark_jit.py`):

| mode | jit-x86_64 (median) | jit-sljit (median) | ratio |
|---|---|---|---|
| guard | 18 153 | 18 181 | 100.2 % |
| explicit | 16 280 | 16 121 | 99.0 % |

Raw rounds (it/s): guard x86 18 153 / 17 913 / 18 390, sljit 18 541 / 18 181 / 17 930; explicit
x86 16 048 / 16 389 / 16 280, sljit 16 121 / 15 385 / 16 177. The differences are inside the
round-to-round noise.

Corpus, µs per call, median of 5 alternated rounds (guard): within ±5 % case by case, both ways
(e.g. inflate 8K 49.0 vs 45.5, deflate 8K 371 vs 386, x25519 138 vs 142, sha256 4K 37.3 vs 38.1,
blake3 8K 21.3 vs 21.4).

Compile time (CoreMark, 29 functions): jit-sljit 36-38 ms, jit-x86_64 41-50 ms (sljit encodes in
C; our core and the backend glue are the same Python in both).

Code size (CoreMark): guard 34 808 vs 34 547 bytes; explicit 46 285 vs 43 544 bytes. Guard fault
points: 669 in both.

**Reading.** This confirms the claim of SLJIT-BACKEND.md and makes it stronger than it was written:
the 60 % of pwart and of the naive translator came from what sits above the encoder; with our core
in front, sljit's encoding costs nothing measurable on x86-64.

## What the backend is

`waboros_sljit.py`, about 1 000 lines of Python (half of it the ctypes bindings and plumbing),
against about 1 900 for the x86-64 backend + encoder + trampolines (SIMD excluded in both counts).
It covers everything the core's scalar set needs: integer and float operators, conversions with
their traps and saturation, loads and stores (explicit checks and guard fault points), globals,
`call`/`call_indirect`/`call_ref`/imports through records, host calls through a bridge, tail calls
(constant stack: a million-deep `return_call` chain runs), tables, bulk memory, `br_table`.

The ABI is sljit's own register-argument convention:

* `SLJIT_ENTER_REG_ARG | SLJIT_ENTER_KEEP(3)`: S0 context, S1 value-stack frame, S2 memory 0 base
  are shared by caller and callee (sljit allows at most 3 kept registers, so memory 0's size is
  read from the context at each explicit check rather than kept in a register).
* R0 tos and status, R1/R2 scratch; pool (pinnable) S3-S5 (saved by sljit's enter) and R3-R6
  (saved by the function into its native frame) - 7 pool registers, against 8 on x86-64.
* Trampolines (entry, host bridge, stack-pointer stub) are sljit functions too, so they need no
  per-architecture assembly.
* Tail calls: `sljit_emit_call(SLJIT_CALL_REG_ARG | SLJIT_CALL_RETURN)` tears the frame down and
  jumps. A tail call into a record of another context falls back to an ordinary call (correct;
  only that cross-instance chain grows the native stack - the x86-64 `tail_switch` trick has no
  sljit form yet).
* The frame-size check uses a patchable sljit constant (`sljit_emit_const` / `sljit_set_const`).
* Code is generated into ordinary memory and copied into our W^X area by the runtime: it is
  position independent (relative branches and calls only; C library calls go through an address
  in the frame, never an immediate call target).

## What carries over to other architectures, and what does not

Portable as written: everything above, including the trampolines and tail calls.

To check or add per target (none needs a hand-written encoder):

* **32-bit operations' upper half.** The D39 contract keeps i32 values zero-extended. x86-64 and
  aarch64 do that for 32-bit operations; sljit's 32-bit sign extensions did not (two places now
  add `MOV_U32`, found by the spec suite). On riscv64 (sign-extending 32-bit forms) every 32-bit
  result needs a `MOV_U32` - a flag in the backend.
* **i64 on 32-bit targets**: register pairs (the D39 amendment of SLJIT-BACKEND.md); untouched here.
* **ceil/floor/trunc/nearest/sqrt** go to the C library (`nearbyint` etc.); there is no sljit op.
  Correct, slower than our inline SSE2 sequences; CoreMark does not use them.
* **Guard mode** reuses the x86-64 fault handler (the fault point is a sljit label before the one
  memory instruction). Other architectures: explicit checks only (D29), as planned.
* **SIMD**: not implemented; sljit's SIMD API is partial. The core falls back per module
  (`Unsupported`) as for any backend without SIMD.

## Consequences proposed

1. SLJIT-BACKEND.md's "first step" and stop rule are satisfied: the sljit backend is not a
   slow fallback but our code quality on any sljit target, for the cost of one backend.
2. It strengthens the "Decision asked of the maintainer" (amend D48) with evidence.
3. It is also a cheap **second oracle** on x86-64 and aarch64: two independent encoders under one
   core, run against each other on the spec suite (this is how the two sign-extension bugs above
   were found in minutes).
4. Not a reason to replace our x86-64/aarch64 backends: they stay the byte oracle for the
   micro-compilers, carry SIMD, and the Python twins need them.

## Reproduce

```sh
cd sljit-be
S=<sljit checkout>/sljit_src            # poc-sljit/build.sh clones it at 39c508d1
cc -O2 -fPIC -shared -I$S -DSLJIT_CONFIG_AUTO=1 -w lib2.c -o libsljit2.so
cc -I$S -DSLJIT_CONFIG_AUTO=1 -w consts2.c -o consts2 && ./consts2 > consts2.json
export PYTHONPATH=$PWD/site:<waboros>/src:<waboros>      # site/sitecustomize.py registers jit-sljit
python -m tests.spec.run --engine jit-sljit core           # from the waboros checkout
WABOROS_TOOLS=<waboros>/tools python cm.py core.wasm --target jit-sljit
./bench.sh                                                 # the alternated CoreMark rounds
WABOROS_JIT_WALK_CHECK=1 WABOROS_JIT_GC_POISON=1 python -m tests.spec.run --engine jit-sljit core gc simd relaxed-simd
python simdb.py <waboros>/tools/run_simd_bench.py --targets jit-x86_64,jit-sljit,wasmtime --rounds 7
```

## Update 2026-11-16: safepoints (D56) and SIMD, on the S0013 branch `gc` at `e3bb361`

The backend was brought up to the current D39 interface and given the two things it lacked: site
records for the GC's frame walk (D56) and SIMD. x86-64, native sljit at `39c508d1` through ctypes,
our unchanged core and runtime, nothing in the repository changed (the target is still registered
from outside, `site/sitecustomize.py`).

What changed in `waboros_sljit.py` and `lib2.c`:

* **Site records.** A sljit label right after every call gives its return address. `ret_at` (from
  the stack pointer at the call to this frame's own return address) is sljit's frame size after
  `sljit_emit_enter`: `w2s_ret_at` in `lib2.c` reads it from sljit's internal fields (`local_size`,
  the saved registers, `GET_SAVED_REGISTERS_SIZE`), `up` = `ret_at` + 8. Reading internals is the
  experiment's shortcut; a sljit API (a return-address offset per function) should replace it,
  per architecture. The bridge writes the store's exit record (the caller's stack pointer at its
  call, the return address, the frame register) and clears its `sp` after the host returns; the
  entry keeps the enclosing record and its frame at `[sp, sp + 32)` during its call
  (`Trampolines.saved` = 0) and puts it back. sljit's argument checks forbid a stack-relative access
  past the locals, so the bridge reads the return address through `sljit_get_local_base`.
* **A tail call into another context** is an ordinary call in this backend (no `tail_switch`): a
  real frame the core has no map for. It is recorded as an extra site (shift 0, no map: the
  arguments were already moved to our frame base) and merged into `STACKMAPS` after the core.
* **SIMD.** waboros's own x86-64 SSE2 lowering (`x86_64/simd.py`) runs into a side buffer with
  sljit's physical registers (the frame, the context; its `rcx`/`rdx` mapped to sljit's scratch R1/R2,
  since sljit's R3 is `rcx` and pinnable), and its bytes go in through `sljit_emit_op_custom` (in
  pieces of at most 15 bytes, sljit's argument check on x86). Bounds checks and fault points stay
  sljit code: the side buffer is flushed before them. xmm0-xmm5 are scratch inside one operation,
  as in `simd.py`.
* The D39 arguments added since 2026-11-04 (`a64`, `lo`, S0009) are accepted and refused when set;
  `Frame.error` (S0012) is the status target of every call; `supported` adds `gc.OPS` and
  `simd.SUPPORTED`; the claims add `gc`, `simd`, `relaxedSimd`.

Physical registers on x86-64 SysV (`sljit_get_register_index`): R0 rax, R1 rsi, R2 rdi, R3 rcx,
R4 r8, R5 r11, R6 r10; S0 rbx (context), S1 r15 (value-stack frame), S2 r14 (memory 0 base), S3 r13,
S4 rbp, S5 r12. Our x86-64 backend has the context in r15, the frame in r12, memory 0 in r13/r14.

### Correctness

Every directory both targets claim, both bounds modes, with `WABOROS_JIT_WALK_CHECK=1` and
`WABOROS_JIT_GC_POISON=1` on `jit-sljit` (the walk checked at every host call against the
runtime's own record of its entries):

| directory | jit-x86_64 | jit-sljit (walk check + poison) |
|---|---|---|
| core | 20 598 | 20 549; the other 49 refused: "unsupported: exceptions" (`ref_null.wast` 34, `instance.wast` 15, modules with `exnref`) |
| gc | 797 (1 skip) | 797 (1 skip), v128 arrays included |
| simd | 25 481 (509 skip) | 25 481 (509 skip) |
| relaxed-simd | 79 | 79 |
| bulk-memory | 7 485 | 7 485 |
| multi-memory | 912 | 912 |
| wide-arithmetic | 109 | 109 |

`tools/spec_diff.py` both ways, explicit and guard: 49 commands pass on jit-x86_64 and not on
jit-sljit, all "unsupported: exceptions"; 0 in the other direction. The heap is not collected on
this branch yet (step 5.3), so objects do not move; what D56 asks of a backend (every frame found
by its return address, the roots of its maps) is what the check mode verifies.

### Speed

CoreMark (`bench.sh`, 3 alternated rounds, one core, it/s; median):

| mode | jit-x86_64 | jit-sljit |
|---|---|---|
| explicit | 16 145 | 16 159 (100 %) |
| guard | 18 051 | 18 492 (102 %) |

Compile time (CoreMark): jit-sljit about 40 ms, jit-x86_64 42-52 ms.

SIMD benchmark (`tools/run_simd_bench.py` through `simdb.py`, 7 alternated rounds, one core, µs per
iteration, median; checksums equal on all three):

| kernel | x86-64 scalar | x86-64 simd | sljit scalar | sljit simd | wasmtime scalar | wasmtime simd |
|---|---|---|---|---|---|---|
| addsat | 3.62 | 0.31 | 3.54 | 0.32 | 2.77 | 0.18 |
| dot | 2.56 | 0.94 | 2.55 | 0.95 | 0.70 | 0.16 |
| scan | 1.86 | 0.56 | 1.93 | 0.59 | 1.78 | 0.15 |
| matmul | 3.01 | 2.07 | 3.16 | 2.07 | 1.30 | 0.37 |
| swizzle | 3.41 | 6.05 | 3.29 | 6.00 | 2.66 | 0.15 |
| nearest | 1.11 | 0.45 | 1.71 | 0.43 | 0.47 | 0.13 |

SIMD is at parity, as expected (the same lowering). Scalar `nearest` is slower: this backend rounds
through libm (`nearbyint`); our SSE2 sequence through `sljit_emit_op_custom` would remove it.

### Still open

* Exceptions (`landing`, `exn_match`, `exn_field`, `exn_load`, `throw`, `throw_ref`).
* memory64 / table64 (`a64`).
* A constant-stack tail call across contexts (`tail_switch`).
* A sljit API for the return-address offset instead of internal fields.
* aarch64: no walk check, no measurement (QEMU here; timings need an arm64 runner). The SIMD part
  there is NEON: our `aarch64/simd.py` through `sljit_emit_op_custom` (4-byte pieces).

## Update 2026-11-16 (2): aarch64 under QEMU

The same backend on aarch64: sljit cross-built for the host (`a64/libsljit2.so`,
`a64/consts2.json`), aarch64 CPython 3.13.14 under QEMU 8.2 user mode, `-cpu cortex-a72`
(ARMv8.0, no BTI: below), `qa64.sh`. Correctness only: QEMU timings mean nothing.

What is per ISA, all small and inside `waboros_sljit.py` / `lib2.c`:

* **The frame.** sljit's aarch64 prologue saves x29/x30 with `stp x29, x30, [sp, #-size]!` for
  frames up to 512 bytes, so `ret_at` = 8 and `up` = the frame size; larger frames keep x30 at
  `size - 8`. Locals start at `SLJIT_LOCALS_OFFSET`, which is also the entry's saved block
  (`Trampolines.saved`).
* **SIMD**: our NEON lowering (`aarch64/simd.py`) unchanged, through `sljit_emit_op_custom` one
  4-byte word at a time, with sljit's registers for the context (x19) and the frame (x20). Its
  scratch x9-x13 and x17 hold nothing of ours (sljit's own temporaries are x9/x10; R3-R6 are x3-x6).
  Its load and store forms take only the offsets its own bounds check produces, so a constant
  address from sljit's `_access` goes to the index register.
* **A sljit assertion**: a constant (`sljit_emit_const`) right after a jump fails in
  `sljit_generate_code` on aarch64. The prologue now emits the frame-size constant first.
* **Guard mode (S0006 fault points)**: on aarch64 sljit splits an access the immediate form cannot
  take (a constant address past the scaled `imm12`: an `add` before the `ldr`) and stores an
  immediate through a temporary (`mov` before the `str`). The fault point's label was then on the
  wrong instruction and a guard fault became a segfault (`memory_grow.wast`). Now, in guard mode
  on aarch64, a constant address uses the `[base, index]` form and a stored immediate goes to a
  register before the label. Lesson for every sljit ISA: a fault point needs "one instruction per
  access", and that is a property of sljit's encoding per ISA, to be checked under guard mode.

Correctness, both bounds modes, `WABOROS_JIT_WALK_CHECK=1` and `WABOROS_JIT_GC_POISON=1`:

| directory | jit-aarch64 | jit-sljit on aarch64 |
|---|---|---|
| core | 20 598 | 20 549; the other 49 "unsupported: exceptions" |
| gc | 797 | 797 |
| simd | 25 481 | 25 481 |
| relaxed-simd | 79 | 79 |
| bulk-memory | 7 485 | 7 485 |
| multi-memory | 912 | 912 |
| wide-arithmetic | 109 | 109 |

`tools/spec_diff.py` both ways, explicit and guard: 49 commands pass on jit-aarch64 and not on
jit-sljit (all "unsupported: exceptions"), 0 in the other direction. x86-64 rechecked after these
changes: the same table as above, unchanged.

**Open on aarch64: BTI.** sljit emits no landing pads. Where the CPU has BTI the runtime maps code
`PROT_BTI` (`execmem.py`), and an indirect call into sljit code is then an illegal instruction
(seen under QEMU's default `max` CPU). Needed: `bti c` at every function entry, thunk and
trampoline, and `bti j` at the targets of `br_table`. Probably possible with `sljit_emit_op_custom`
(as our aarch64 backend emits them); not done here.

Reproduce on aarch64:

```sh
S=<sljit checkout>/sljit_src; mkdir -p a64
aarch64-linux-gnu-gcc -O2 -fPIC -shared -I$S -DSLJIT_CONFIG_AUTO=1 -w lib2.c -o a64/libsljit2.so
aarch64-linux-gnu-gcc -I$S -DSLJIT_CONFIG_AUTO=1 -w consts2.c -o a64/consts2
qemu-aarch64 -L /usr/aarch64-linux-gnu a64/consts2 > a64/consts2.json
python3 <waboros>/tools/run_arm64.py -c 1                 # once: the arm64 site (pytest, wasmtime)
WABOROS=<waboros> WABOROS_JIT_WALK_CHECK=1 ./qa64.sh -m tests.spec.run --engine jit-sljit --in-process core gc simd
```
