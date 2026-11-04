---
title: sljit backend under our core - the "first step" of SLJIT-BACKEND.md, done and measured
kind: reference
status: draft
updated: 2026-11-04
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
```
