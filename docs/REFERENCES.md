---
title: References
kind: reference
status: current
updated: 2026-11-02
---

# References

Projects worth reading to see **how something is done correctly**. We read, understand and write our
own code; we do not translate other projects line by line, so their licences do not flow into ours.
When an idea comes from one of them, the code says so in a comment (e.g. "bounds check for memory64
as in Liftoff") — that keeps the reasoning traceable. If a snippet is ever copied, it is recorded in a
`THIRD_PARTY_NOTICES` file with its licence; the default is not to copy.

Correctness never rests on the source: it is shown by our own tests — the spec suite, differential runs
against wasmtime, and encodings checked against `objdump` / `llvm-mc` in CI.

| source | what to look at | useful for |
|---|---|---|
| **Winch** — wasmtime's baseline compiler (Rust) | the closest project to our JIT: single-pass wasm → x64 / aarch64; lowering of every operator with Wasmtime's trap semantics; the lazy value stack and register cache; bounds checks; fuel and epoch checks; trap codes | `jit-x86_64`, `jit-aarch64`, LIR design |
| **V8 Liftoff** — V8's baseline wasm compiler (C++) | the same job across **many architectures** (x64, ia32, arm, arm64, riscv, mips64, loong64, ppc64, s390x): how each wasm operator maps onto each ISA; how register state is reconciled at block merges (we avoid it by pinning locals — worth knowing what it costs); memory64 bounds checks | every new architecture |
| **wasm2c** (`wasm-rt` and its helpers) | exact semantics of the awkward operators as C: NaN rules of `min`/`max`, `trunc`, `trunc_sat`, `nearest`, signed division and remainder, rotations, conversions | `pyc` helpers, JIT instruction sequences |
| **The spec reference interpreter** (OCaml, `WebAssembly/spec`) | what an operator means when in doubt | everything |
| **TinyCC** | small per-architecture back ends (`x86_64-gen.c`, `arm64-gen.c`, `riscv64-gen.c`, `arm-gen.c`, `i386-gen.c`): calling conventions, instruction encodings; the `vtop` value stack with `VT_CMP` — the same idea as our lazy entries and fused compare + branch | ABIs and encodings for new architectures |
| **asmjit** | complete encoding tables for x86 and AArch64, including SIMD | encoders, SIMD |
| **DynASM** (LuaJIT) | encoders for x86/x64, ARM, ARM64, PPC and **MIPS** | MIPS (Ingenic X2000), PPC |
| **Cranelift's assemblers and lowering** | encodings for x64, aarch64, riscv64, s390x; x64 and aarch64 SIMD lowering (`cranelift/codegen/src/isa/x64/lower.isle`): which operators are one SSE2 sequence and which need SSE4.1 or fall back to libcalls (read for S0005; V8 enables wasm SIMD on x64 only with SSE4.1, `CpuFeatures::SupportsSimd128`) | encoders, SIMD |
| **MicroPython's native emitters** (`py/emitnative.c`, `py/asmthumb.c`, `py/asmxtensa.c`, `py/asmrv32.c`, …) | code generation for MCU architectures (Thumb, Xtensa, RISC-V 32); how viper types and calls work | the `mpy` target, viper code, future Thumb/Xtensa back ends |
| **CPython's JIT** (`Tools/jit`) | how copy-and-patch is built: stencils, holes, relocations | if copy-and-patch is chosen (TARGETS.md) |
| **wasm-to-asm** (calebsander, TypeScript) | a small readable wasm → x86-64 compiler: the bottom of the operand stack kept in registers, a calling convention without caller-saved registers (it has no traps: not a sandbox) | register allocation ideas |
| **pywasm** | a complete pure-Python interpreter (Wasm 2.0, SIMD, WASI p1) and its spec runner | the test harness, a second oracle, WASI p1 |

## Primary sources (normative)

What an instruction means and how a machine instruction is encoded comes from these, not from the
projects above. Record the edition or revision used in the commit or the spec that relies on it.

| source | what it defines | used for |
|---|---|---|
| **WebAssembly Core Specification 3.0** (W3C; `WebAssembly/spec`, rendered at webassembly.github.io/spec/core) | the opcode table, binary format, validation, execution semantics of every instruction | the front end, the validator, every target |
| **`WebAssembly/spec` `test/core`** at the pinned commit (S0001) | the executable form of the specification | the metric |
| **Wasmtime documentation** (`TrapCode`, feature tiers, `Config` defaults) | behaviour where the specification leaves room (D6) | traps, features |
| **Intel 64 and IA-32 Architectures Software Developer's Manual**; **AMD64 Architecture Programmer's Manual** | x86-64 instruction encodings and semantics (flags, `div` faults, `cvt*` rounding, `lzcnt`/`tzcnt` availability) | `jit-x86_64` encoders and ISA levels (ARTIFACT.md) |
| **Arm Architecture Reference Manual for A-profile** (Arm DDI 0487) | AArch64 encodings and semantics | `jit-aarch64` |
| **The RISC-V Instruction Set Manual**, Volume I (Unprivileged), ratified extensions | RV64 encodings and semantics | `jit-riscv64` |
| **System V AMD64 psABI** (`x86-64-abi`) and the **Microsoft x64 calling convention** | argument registers, callee-saved registers, stack alignment, red zone / shadow space | the SysV and Win64 entry trampolines and host calls of `jit-x86_64` (D23) |
| **AAPCS64** (`ARM-software/abi-aa`) and Apple's arm64 ABI differences | the same for AArch64 (incl. `x18`, Darwin variadics) | `jit-aarch64` |
| **RISC-V psABI** (`riscv-non-isa/riscv-elf-psabi-doc`) | the same for RISC-V | `jit-riscv64` |

Encodings written from these are checked against `objdump` / `llvm-mc` in CI; semantics against the
spec suite and differential runs (TESTING.md).
