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
* **f32/f64**: sljit has float registers and ops; NaN bit patterns must be checked per target
  against `pyc` (D46).
* **SIMD**: sljit's SIMD API is small and not on every architecture; v128 goes to the frame and
  lanes are done in scalar code (or a libcall), correct and slow. Acceptable for the tail.
* **Big-endian** (s390x, ppc32/64 BE, mips BE): loads and stores need byte reversal (sljit has
  `SLJIT_REV*` ops). D48's "no big-endian JIT host" holds for the hand-written JITs, not this track.
* **Determinism**: the wasm build has no `cpuid` and fixed configuration, so its output depends
  only on the input and the target; checked above for the simple case (same bytes on three
  engines). D35 (byte-identical cross-compilation) then holds for sljit targets too.
* **Cache flush and W^X** on the target are the loader's job, as for our backends.
* **Licence**: sljit is BSD-2-Clause, compatible with MIT (D40).

## Verification

* The spec suite under QEMU for each sljit target, `pyc` as the oracle (as PYC-PLATFORMS.md does
  for `pyc` today).
* On x86-64 and aarch64 the sljit backend and the hand-written backend can both run the spec suite
  and CoreMark: same results, and the speed gap is the price of the generic encoder.
* Byte determinism: the micro-compiler's output for a sljit target is the same on Wasmtime, our
  JIT and `pyc` (as in table 1).

## Where it fits

* After the micro-compilers exist (`x64.wasm`, `arm64.wasm`, then `riscv64.wasm`): the sljit
  backend is one more micro-compiler, `sljit-<arch>.wasm`, built from the same C sources with a
  different backend. Before that, nothing.
* It does not touch the frozen API/ABI, the artifact format or the Python backends.
* Python twins: none for the sljit backend (sljit itself is the reference for its encodings); the
  oracle is behaviour (spec suite against `pyc`), not bytes.

## First step (when the time comes)

Port the D39 interface's sljit implementation for **x86-64** first, behind our core, and measure
CoreMark against our hand-written x86-64 backend on the same machine. Stop rule: if it is below
about 70 % of our backend, find out why before adding architectures. Then ppc64le or loongarch64
(64-bit, no register pairs), then a 32-bit target (armv7 or i686) with the i64 amendment.

## Decision asked of the maintainer

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
