---
title: sljit as a backend for the long tail - our core over sljit, as a micro-compiler
kind: reference
status: draft
updated: 2026-11-04
---

# sljit as a backend for the long tail: our core over sljit, as a micro-compiler

A proposal from the maintainer (2026-11-03/04), for the orchestrator. It comes with a proof of
concept ([`poc/sljit/`](../poc/sljit/)) and measurements. **Nothing here changes the current plan or D48's three
hand-written JITs**: it proposes one optional, later track for the architectures we will not write
encoders for ourselves, and asks for an amendment to D48's sentence "no sljit/LLVM long-tail backend
is planned" (a decision for the maintainer).

## The idea in one paragraph

Our frontend and our one-pass core (lazy operand stack, pinned registers, traps, the context ABI)
are ported to C (or Zig) as the micro-compiler (MICRO-COMPILERS.md). Beside the hand-written
backends for x86-64, aarch64 and riscv64, the D39 backend interface gets **one more implementation,
written once against sljit's API**. sljit then emits machine code for every architecture it
supports. The whole micro-compiler is compiled to `wasm32` and runs on `pyc` or on our JIT, so the
long-tail architectures get a JIT the same way the main ones do: a `.wasm` in the wheel, no native
code shipped per platform, and it can compile itself.

```
our frontend + our core (lazy stack, pinned registers, traps, context ABI)      → C/Zig
    └─ backend interface D39 (move, binop, load/store, cmp_branch, call, ...)
         ├─ hand-written: x86-64, aarch64, riscv64   ← the main targets, byte-checked by the Python twins
         └─ one sljit backend: the same D39 implemented on sljit's API
               → i686, armv7, ppc64le/ppc32, mips32/64, loongarch64, s390x, riscv32 ...
the micro-compiler → wasm32 → runs on pyc / our JIT → compiles wasm (and itself) to native code
```

Not pwart, and not my prototype translator: those two are measurement points only (below). The
code quality we want comes from **our core**; sljit only encodes instructions.

**Precedent** (maintainer, 2026-11-15): Samsung's Walrus, a WebAssembly runtime whose JIT is built on
sljit (REFERENCES.md): a reference for the operator mapping on five ISAs and for i64 on 32-bit
targets. Its calls go through C++ helpers, its GC is conservative (bdwgc) and its code is absolute,
so it says nothing about D56 or D59.

## What sljit is

* A C library (zherczeg/sljit; the JIT behind PCRE2) with its own small abstract machine: about 60
  three-operand operations, a dozen integer and float registers split into scratch (`R*`) and saved
  (`S*`), operands "register / immediate / memory with base, index and offset", compare fused into
  jump or flag set, `emit_enter`/`emit_return`/`emit_icall` for the native calling convention.
* **No register allocation, no IR, no optimiser**: every call emits machine code at once. That is
  why it fits under our core: our core already decides where every value lives.
* One C file per architecture (`sljitNative*.c`), about 3-5 kloc each; all architectures share
  `sljitLir.c`. Supported: x86-32/64, ARM v6/v7 (ARM and Thumb-2), ARM64, PPC 32/64, MIPS 32/64,
  RISC-V 32/64, s390x, LoongArch64.

## Why our D39 maps onto sljit directly

D39 is already shaped like sljit: three-operand ops, compares fused with branch or set, operands
"register / constant / slot".

| D39 / core | sljit |
|---|---|
| pool registers (pinned locals, temporaries) | `SLJIT_S*` (saved) and `SLJIT_R*` (scratch) |
| `tos` / scratch | an `R*` register |
| `Slot(k)` | `SLJIT_MEM1(SLJIT_SP), frame_off + 8k` |
| context register (ctx, D34) | one saved register, loaded in `emit_enter` |
| memory base | one saved register (or `ctx` + offset) |
| `cmp_branch` / `cmp_set` | `sljit_emit_cmp` / `sljit_emit_op2u` + `sljit_emit_op_flags` |
| explicit bounds check, trap | compare + jump to a shared trap label (D29: new architectures use explicit checks; no signal handler) |
| direct call / `call_indirect` / libcalls | `sljit_emit_call` / `sljit_emit_icall` |
| trampolines (entry, host bridge) | generated with sljit itself (`emit_enter`, `emit_icall`) |

The context ABI, trap codes, libcalls and artifact format stay the same, so the same loader and the
same behaviour hold on every architecture.

### D39 is our LIR vocabulary; sljit is its second consumer

D39 is in effect our LIR, only not materialised: the core calls the backend's methods in one pass
and the backend encodes at once, so the "LIR" exists as a sequence of calls, not as a stored list
(the lazy operand stack in the core is the level above it, the "HIR-lite"). sljit is an LIR of
almost the same level (an abstract register machine with three-operand ops), so the sljit backend
is a mapping table: one D39 operation → one to three sljit calls. What stays above D39, in the
core, is what sljit does not do: which locals to pin, the lazy stack and temporaries, the frame,
the context ABI, trap codes and libcalls. That is the 60 % vs 82 % gap in the table below.

**Proposal for the LIR-vocabulary work (planned with the artifact format):** when D39 is formalised
as the LIR vocabulary, it becomes the contract for three consumers: the hand-written backends
(x86-64, aarch64, riscv64), the sljit backend, and the core's C/Zig port. The review of that
formalisation should check every operation's mapping onto sljit: an operation that does not map to
one or two sljit calls is a sign it is tied to one ISA, and that is better found before the
vocabulary is frozen.

## Evidence (measured 2026-11-03/04, this machine, x86-64, one core)

### 1. sljit compiles to wasm32 and runs on our runtime with identical output

Patched copy of sljit (two small patches, `poc/sljit/sljit-wasm-host.patch`: a 64-bit `sljit_sw` on a 32-bit
host, and no `cpuid` on a wasm host, i.e. the baseline ISA level), built with `clang
--target=wasm32 -nostdlib`, a bump allocator and three tiny libc shims. A kernel driver
(`drv2.c`) emits a small loop function for each target architecture. Run on three engines:

| sljit target | wasmtime | waboros jit-x86_64 | waboros pyc | identical bytes |
|---|---|---|---|---|
| X86_64 | `fe46ca2a`, 87 B | same | same | yes |
| ARM_64 | `c0b565a1`, 88 B | same | same | yes |
| RISCV_64 | `f20c5135`, 176 B | same | same | yes |

Sizes of the wasm modules (`-O2`, one architecture each, kernel driver / whole API exported):
X86_64 21.6 / 43.7 KB, ARM_64 27.1 / 36.8 KB, RISCV_64 36.9 / 44.4 KB.

Compile throughput for this kernel: native sljit 0.56 us per function; sljit.wasm on Wasmtime
0.4 us, on waboros jit-x86_64 0.5 us, on `pyc` 93 us. So the wasm build loses nothing on our JIT,
and `pyc` is usable for bootstrap.

The native code it emits is correct: the same kernel built natively (`native_bench`) runs and gives
the same result as the C reference (`kern.c`).

### 2. A naive "waboros frontend + sljit" translator already reaches WAMR Fast JIT and pwart

`w2sljit.py`: our frontend (`binary.parse`, `validate`) and a simple one-pass translator calling
sljit through ctypes. It is **not** our core: it pins only five locals to `S1..S5`, keeps stack
depth < 5 in `R2..R6` and the rest in the frame, saves live registers around every call, calls
through a table of addresses, no bounds checks (as guard mode). CoreMark validates; 29 functions,
47 170 bytes of code, 18 ms to compile.

CoreMark, alternated rounds, same machine:

| engine | it/s | % of Wasmtime |
|---|---|---|
| Wasmtime 50.0.1 | 23 546 | 100 |
| **waboros jit-x86_64, guard** | 19 401 | 82.4 |
| waboros frontend + sljit (naive translator) | 14 695 | 62.4 |
| WAMR Fast JIT | 13 943 | 59.2 |
| pwart (sljit-based wasm JIT in C) | 13 791 | 58.6 |

Re-run from a clean build on 2026-11-04: translator 14 626, pwart 13 838 (single runs).

Reading: the gap between 60 % and our 82 % is the core (pinned registers chosen by use, lazy
stack, direct calls, frame layout), not the encoder. sljit's encoding is not the limit; with our
core in front of it the sljit backend should land much nearer our hand-written backends than
pwart. **This is the claim to verify** (see "first step").

## What it would cost

* **The port of the core to C/Zig** is the micro-compiler work that is planned anyway
  (MICRO-COMPILERS.md); the sljit backend adds one D39 implementation on top of it, of the size of
  one of our backends without an encoder (estimate: 1.5-3 kloc of C).
* **i64 on 32-bit targets.** sljit on a 32-bit architecture has 32-bit words only; i64 values need
  register pairs (add/sub with carry, `mul` by parts, shifts by parts, libcalls for div/rem and
  float conversions). This is an amendment to D39 and the main cost of the 32-bit tail; 64-bit
  long-tail targets (ppc64le, loongarch64, mips64, s390x) do not need it.
* **Which 32-bit Arm cores** (maintainer, 2026-11-15): sljit's 32-bit Arm backends are ARM (v6/v7)
  and Thumb-2 (`sljitNativeARM_T2_32.c`), so the track covers Cortex-M3 and up (RP2350, Cortex-M33)
  but not ARMv6-M (Cortex-M0/M0+, RP2040), which has only Thumb-1; RP2040 stays on `mpy`.
* **f32/f64**: sljit has float registers and ops; NaN bit patterns must be checked per target
  against `pyc` (D46).
* **SIMD**: sljit's SIMD API is small and not on every architecture; v128 goes to the frame and
  lanes are done in scalar code (or a libcall), correct and slow. Acceptable for the tail. Walrus has full
  v128 (and relaxed SIMD), but not through sljit's SIMD API: it encodes the arithmetic by hand per
  ISA through `sljit_emit_op_custom` (about 1.5-2.5 kloc per ISA) and uses sljit only for moves and
  lanes; it has no scalar fallback and builds only for x86, x64, ARM, AArch64 and riscv64. So the
  scalar fallback is needed on every sljit target of ours unless we write such encoders (see the
  findings below).
* **Big-endian** (s390x, ppc32/64 BE, mips BE): loads and stores need byte reversal (sljit has
  `SLJIT_REV*` ops). D48's "no big-endian JIT host" holds for the hand-written JITs, not this track.
* **Determinism**: the wasm build has no `cpuid` and fixed configuration, so its output depends
  only on the input and the target; checked above for the simple case (same bytes on three
  engines). D35 (byte-identical cross-compilation) then holds for sljit targets too.
* **Cache flush and W^X** on the target are the loader's job, as for our backends.
* **Licence**: sljit is BSD-2-Clause, compatible with MIT (D40).

## Two checks before the first sljit step (maintainer, 2026-11-15)
This document predates D56 (precise roots) and D59 (ELF64 artifacts, no relocations); both add a
requirement that sljit must meet, checked on every ISA of the track before any backend code:

1. **Site records and stack maps (D56).** sljit builds the frame (`sljit_emit_enter`), not our
   core. Every call needs its return-address offset, `ret_at` and `up`: sljit gives a label right
   after each call and the frame size follows from `emit_enter`'s arguments, so it is probably
   derivable; to be shown per architecture with the walk's check mode (`WABOROS_JIT_WALK_CHECK=1`).
2. **Position independence (D59: `RELOCS` empty).** sljit emits code for the address it will run
   at: some jumps and constants are absolute (`sljit_emit_const`, rewritable jumps,
   `sljit_get_label_addr`), and a `br_table` through a table of label addresses is absolute too. A
   micro-compiler generates code in its own memory and it runs elsewhere (AOT, cross-compilation,
   D35). Options: (a) use only relative jumps and tables of offsets instead of addresses, and check
   that sljit then emits position-independent code on each ISA (start here); (b) generate for the
   final address (breaks AOT and D35); (c) real `SHT_RELA` relocations, which D59 keeps only as a
   last resort.

## sljit built for wasm32 per target (maintainer, 2026-11-15)
Pinned sljit `39c508d` with the patch in `poc/sljit`, the full API exported (as `full_*.wasm` in
`build.sh`), clang wasm32:

| target | `-Os` | `-O2` | `-Os` + gzip -9 |
|---|---|---|---|
| X86_32 | 48.5 KB | 61.8 KB | 17.5 KB |
| X86_64 | 43.7 KB | 59.3 KB | 16.3 KB |
| ARM_64 | 36.8 KB | 59.5 KB | 14.4 KB |
| PPC_32 | 40.5 KB | 56.1 KB | 13.0 KB |
| MIPS_32 | 39.0 KB | 76.5 KB | 14.3 KB |
| MIPS_64 | 41.1 KB | 81.6 KB | 15.4 KB |
| RISCV_32 | 39.4 KB | 58.8 KB | 13.5 KB |
| RISCV_64 | 44.4 KB | 65.3 KB | 15.4 KB |

One target is 37-48 KB at `-Os`; the eight together 333 KB. One module holding several targets
would share the generic part (`sljitLir.c`): estimated (not measured) 250-350 KB, about 100 KB
gzipped, plus our core in C. sljit picks its target at build time and has no namespace, so such a
module builds `sljitLir.c` once per `SLJIT_CONFIG_*` with prefixed symbols.

Five targets do not build, all because sljit assumes the host is the target:
* **ARM_V6, ARM_V7, ARM_THUMB2**: without hardware divide the code calls `__aeabi_uidivmod` /
  `__aeabi_idivmod` and embeds the compiler process's address of them.
* **PPC_64**: inline assembly in the backend.
* **S390X, LoongArch64**: CPU features read with `getauxval` (`sys/auxv.h`) from the CPU it runs on.

So two patches are needed beyond today's: helper calls through context words (as our libcalls),
never an absolute address (the same class as position independence above); CPU features from the
configuration (`isa_level`, `isa_features` of the artifact), not from the host, as the existing
patch already does for x86, ARM64 and RISC-V.

## Findings from the sljit and Walrus sources (2026-11-15)
Read: sljit `23d64b71b8f5` (2026-11-07), Walrus `eb9f2fa304c2` (2026-11-13), which pins sljit
`3907e69005ba` unpatched and `#include`s `sljitLir.c` with `SLJIT_CONFIG_STATIC`. Nothing built or
run; line references were in the research report, the conclusions are here.

* **Supported architectures** also include Alpha (new at sljit HEAD).
* **SIMD:** sljit's SIMD API is moves, lanes, splats, extends and AND/OR/XOR/shuffle, almost no
  arithmetic; on x86 it needs SSE4.1 (above the x86-64 baseline, so a declared ISA level), and there
  is none on MIPS, PPC and Alpha. Our track uses scalar v128 everywhere; sljit's SIMD loads, stores
  and splats can come later as an optimisation.
* **Atomics:** `sljit_emit_atomic_load`/`_store` pairs (an LL/SC or CAS retry loop the caller
  builds every read-modify-write from) and `SLJIT_MEMORY_BARRIER`. 8/16/32/64 bits on x86, arm64
  and Thumb-2, only 32/64 on RISC-V, MIPS, PPC and s390x (8/16 by masking inside the aligned word,
  as Walrus does); no 64-bit atomics on 32-bit targets (per-ISA `op_custom`, or a lock helper on
  mips32/ppc32); the ordering is undocumented and relaxed on the weak-memory ISAs, so every atomic
  op is wrapped in barriers; on x86-32 the barrier needs SSE2 detection, which the wasm host's
  zeroed `cpuid` disables. Estimate about 300 lines. wait/notify are host calls.
* **Traps and bounds:** explicit checks first (as Walrus); a label before each access gives its pc,
  enough for a fault map and guard-page mode later.
* **Frames (D56):** no unwind or CFI information. `ret_at` and `up` follow per ISA from the frame
  `sljit_emit_enter` builds and the public `compiler->local_size` (x86-64: after the pushes and
  the locals; arm64: SP+8 and SP+`local_size`; Thumb-2: the highest slot of the push; riscv64:
  SP+F-8 and SP+F; ppc64 and s390x not checked); about 20 lines per ISA, reproducing sljit
  internals, so tied to a pinned revision and checked with `WABOROS_JIT_WALK_CHECK`. Never emit
  `SLJIT_DIV` on cores without hardware divide (sljit's own soft-divide pushes around a helper).
* **Position independence (D59):** sljit takes the relative form of a jump when the distance fits
  and falls back to absolute forms otherwise (and on x86-64 and arm64 also when the nominal address
  is below 4 GiB); `sljit_emit_const`, rewritable jumps and calls to an immediate address are always
  absolute, and on MIPS32 every `SLJIT_CALL`. The mechanism: `SLJIT_GENERATE_CODE_BUFFER` with an
  `executable_offset` giving a high nominal address; direct calls as relative calls to labels in one
  compiler per module; helpers through `sljit_emit_icall` on a context word; a check after
  generation that refuses any absolute jump form; `br_table` as int32 offsets from a base label in
  a read-only buffer (a compare tree as the fallback). About 150 lines. MIPS branches reach only
  128 KiB: a patch for a relative long branch, or a function-size limit.
* **Host equals target:** x86 (`cpuid`), LoongArch and s390x (`getauxval`), MIPS (`cfc1`) and PPC64
  (inline asm) query the host; `__ARM_FEATURE_ATOMICS`, the IDIV macros, `__riscv_*`, `_CALL_ELF`
  and `__ARCH__` are read at compile time and must be set for the wasm build (ppc64le needs
  `-D_CALL_ELF=2`, else ELFv1 indirect calls). One `SLJIT_FIXED_CPU_FEATURES` patch (constants from
  the configuration, as RISC-V already works) is about 80 lines; ARM's `__aeabi_*divmod` need dummy
  symbols and a backend that never emits `SLJIT_DIV` without IDIV.
* **Every libcall is native code** (i64 div/rem, float-i64 conversions, popcnt, i64 atomics): our
  runtime has no C helpers, so they are emitted once per module with sljit as relative-call stubs
  (about 400 lines). i64 on 32-bit targets in register pairs is about 1.5 kloc (Walrus's size).
* **Tail calls** map onto `SLJIT_CALL_RETURN`, which every backend implements.
* **One module for all ISAs:** sljit has no symbol prefix, but `SLJIT_CONFIG_STATIC` makes the API
  `static`, so one translation unit per ISA (`SLJIT_CONFIG_<X>` + `STATIC` + `#include
  sljitLir.c` + our D39 layer, compiled per configuration since register numbers are macros),
  exported as a table of functions with fixed-width types. Size about the sum of the per-target
  modules plus 10-20 KB of D39 layer per ISA; only our core is shared.

## How the micro-compiler is built (maintainer, 2026-11-15)
* **Statically linked by default:** one C build of our core plus sljit, one `.wasm`. sljit's API is
  small and called for every emitted instruction (`sljit_emit_op2`, `sljit_emit_jump`, ...): in one
  build with LTO those calls inline and the unused API is dropped (faster compiles, smaller module).
  The core and sljit share pointers (`struct sljit_compiler *`, the code buffer), so separate modules
  would need one imported memory and one allocator anyway: static linking with a hand-made ABI in
  between. The same sources build natively for the encoder tests against `llvm-mc` (D60: plain
  `cc core.c sljitLir.c`); AOT and determinism (D35, D54) are the same, one `.wasm` tied by a test.
* **Separate modules joined with `wasm-merge` only for one case:** one micro-compiler for several
  ISAs. sljit picks its target at build time and has no namespace, so a static build would need one
  `sljitLir.c` per target with prefixed symbols (a generated header of
  `#define sljit_emit_op2 x86_64_sljit_emit_op2 ...`); separate `sljit-x86_64.wasm` and
  `sljit-aarch64.wasm` have their own namespaces and merge without renaming, and `wasm-opt` can
  inline across the old boundary afterwards.
* **The boundary in the code is `sljitLir.h` either way**: the core is written against it, so the
  choice is a build decision, taken when a second ISA first goes into the same module.
* **sljit is not forked**: our changes (helper calls through context words, position-independent
  code, CPU features from the configuration) stay a small set of patches over a pinned upstream, as
  `poc/sljit/sljit-wasm-host.patch` already is, so sljit updates from upstream with the patches on
  top.

## Verification

* The spec suite under QEMU for each sljit target, `pyc` as the oracle (as PYC-PLATFORMS.md does
  for `pyc` today).
* On x86-64 and aarch64 the sljit backend and the hand-written backend can both run the spec suite
  and CoreMark: same results, and the speed gap is the price of the generic encoder.
* Byte determinism: the micro-compiler's output for a sljit target is the same on Wasmtime, our
  JIT and `pyc` (as in table 1).

## Where it fits

* After the micro-compilers exist (`x86_64.wasm`, `aarch64.wasm`, then `riscv64.wasm`): the sljit
  backend is one more micro-compiler, `sljit-<arch>.wasm`, built from the same C sources with a
  different backend. Before that, nothing.
* It does not touch the frozen API/ABI, the artifact format or the Python backends.
* Python twins: none for the sljit backend (sljit itself is the reference for its encodings); the
  oracle is behaviour (spec suite against `pyc`), not bytes.

## First step: done as a proof of concept (2026-11-04)

The plan was: port the D39 interface's sljit implementation for **x86-64** first, behind our core,
and measure CoreMark against our hand-written x86-64 backend on the same machine; stop rule: below
about 70 % of our backend, find out why before adding architectures; then ppc64le or loongarch64
(64-bit, no register pairs), then a 32-bit target (armv7 or i686) with the i64 amendment.

The maintainer did it outside the package (`poc/sljit/sljit-be/`: `waboros_sljit.py` registers a
target `jit-sljit` from outside; the core, the runtime, the context ABI and the guard registry are
unchanged; full write-up in `poc/sljit/sljit-be/SLJIT-BACKEND-RESULTS.md`):

* **Correctness: spec parity with `jit-x86_64`.** Every claimed directory but SIMD (core 20 509,
  bulk-memory 7 483, multi-memory 912, wide-arithmetic 109): 0 commands differ in either direction
  (`tools/spec_diff.py`), explicit and guard; the 21 corpus cases pass. The `wide` method took
  about 25 lines (`ADD`/`ADDC` with `SLJIT_SET_CARRY`, `LMUL_SW`/`LMUL_UW`). SIMD is not
  implemented (sljit's SIMD API is partial; the core falls back per module).
* **Speed: about 100 % of our backend** (stop rule not reached). CoreMark, 3 alternated rounds, one
  core: guard 18 153 vs 18 181 it/s (100.2 %), explicit 16 280 vs 16 121 (99.0 %), inside the round
  noise; the corpus within ±5 % case by case; compile time 36-38 ms vs 41-50 ms. No placement
  control (BENCHMARKS.md, S0007): the parity claim holds within the noise, not to a percent.
* **Size of the backend**: about 1 000 lines of Python (half ctypes bindings) against about 1 900
  for the x86-64 backend, encoder and trampolines (SIMD excluded in both). Trampolines and tail
  calls are sljit functions too (no per-architecture assembly). 7 pool registers against 8.
* **Found on the way:** sljit's 32-bit sign extensions break D39's zero-extended i32 invariant
  (LESSONS 5) in two places, fixed with `MOV_U32`; on riscv64 every 32-bit result would need it.
  ceil/floor/trunc/nearest/sqrt go to the C library. Guard mode reuses the x86-64 fault handler.

Reading: the 60 % of pwart and of the naive translator came from what sits above the encoder;
with our core in front, sljit's encoding costs nothing measurable on x86-64. Proposed by the
maintainer's write-up, **deferred (maintainer, 2026-11-04)**: `jit-sljit` as a cheap **second oracle** on x86-64 and aarch64
(two independent encoders under one core, run against each other on the spec suite); it would need
a native `libsljit` in the dev environment. The hand-written backends stay (byte oracle for the
micro-compilers, SIMD, the Python twins). Next steps, when the track starts: a 64-bit
architecture without register pairs (ppc64le or loongarch64), then a 32-bit one with the i64
amendment.

## Decision (maintainer, 2026-11-04: accepted as the D48 amendment, not a first priority)

Amend D48: "Native hand-written JITs exist only for x86-64, aarch64 and riscv64. Other architectures
run `pyc`; **optionally, later, a sljit backend under our core** (a micro-compiler) may give them a
JIT, verified behaviourally against `pyc`, without hand-written encoders."

## The proof of concept (`poc/sljit/`)

`./build.sh [OUT]` (needs git, cc, clang with the wasm32 target, cmake; clones sljit at
`39c508d1` and pwart at `f93b5cf4`) builds everything into `./build`. Checked from scratch on
2026-11-04.

| file | what |
|---|---|
| `build.sh` | the whole build |
| `sljit-wasm-host.patch` | the two patches for a wasm host |
| `lib.c`, `consts.c` | native `libsljit.so` for ctypes, plus `consts.json` (sljit's constants) |
| `drv.c`, `drv2.c`, `libc.c`, `shim/` | sljit as wasm32 modules (`full_*.wasm`, `k_*.wasm`); `drv2.c` also builds `native_bench` |
| `kern.wat`, `kern.c` | the kernel as wasm and as the C reference |
| `compat.py`, `engines.py`, `kbench.py` | identical bytes on Wasmtime / waboros JIT / `pyc`, compile throughput |
| `w2sljit.py` | naive waboros-frontend + sljit translator (measurement only) |
| `pwart/host.c` | CoreMark host for pwart |

Run (with waboros importable; tested against `phase5` on 2026-11-04, which has D46's float
constants as bits; `main` of that date is too old for `w2sljit.py`):

```sh
./build.sh /tmp/pocbuild
cd /tmp/pocbuild
python w2sljit.py core.wasm            # CoreMark through the sljit translator
python /path/to/poc/sljit/compat.py .  # identical bytes on three engines
./native_bench 100000000               # native sljit kernel
./pwart_host core.wasm                 # pwart, from the directory with core.wasm
```

Known PoC limits: `w2sljit.py` covers CoreMark's subset of wasm only (i32/i64, f64 as bits, one
memory, no tables beyond direct calls); x86-64/aarch64 hosts only; the pwart header declares
`pwart_namespace_define_wasm_module` as returning `pwart_module_state *`, but the value is the
state itself (`host.c` casts it).
