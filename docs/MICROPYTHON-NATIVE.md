---
title: MicroPython's native emitters - same design as our core; a reference for encoders (Xtensa above all), not a base for waboros on MicroPython
kind: reference
status: draft
updated: 2026-11-19
---

# MicroPython's native emitters as a reference for our micro-compilers

Maintainer's investigation, 2026-11-19; input for the micro-compiler track (D48, D60, D68) and for
waboros on MicroPython. Checked against micropython `master` at `8901bf2` (2026-11-18), directories
`py/` and `ports/unix`. A record only, not a plan: MicroPython is deferred as a target (ROADMAP,
phase 7), and nothing here is scheduled.

## Summary

MicroPython compiles Python functions to machine code with `@micropython.native` and
`@micropython.viper`. The compiler behind both has the same design as our one-pass core:

* a stack machine compiled in one pass;
* a register cache of the value stack;
* a per-ISA assembler under a shared emitter;
* the runtime reached through a table in a pinned register.

Its assemblers are MIT, like waboros, and small: 400-680 lines each.

* **Use them as a reference** for instruction encoders and calling conventions on small targets.
* **Xtensa above all**: it is the one ISA our sljit track lacks, and `asmxtensa.c` has both of its
  ABIs (call0 and windowed) working on real hardware.
* **Do not base waboros on MicroPython on `viper`.** It has no floats, no 64-bit values on 32-bit
  targets, no AArch64 or MIPS, and compiles on the device. The micro-compiler with the C runtime
  as a natmod (D60) stays the main path.

## What MicroPython has (`py/`)

* **Assemblers per ISA:** `asmxtensa.c` (401 lines), `asmarm.c` (469), `asmx86.c` (545),
  `asmthumb.c` (612), `asmx64.c` (642), `asmrv32.c` (675). These are instruction encoders, the
  level of our `x86_64/encoder.py`.
* **Emitter glue per ISA:** `emitn*.c`, 18-70 lines each, binding an assembler to the shared
  emitter.
* **`emitnative.c` (3 128 lines):** the shared one-pass emitter.
* **Inline assemblers:** `emitinlinethumb.c`, `emitinlinextensa.c`, `emitinlinerv32.c`
  (`@asm_thumb`, `@asm_xtensa`, `@asm_rv32`).

### Which targets have what

`@native` and `@viper` go through the same native emitter. `compile.c` checks one thing for both:
whether `emit_native_table[...]` has an emitter for the current architecture. So the two columns are
always equal. The inline assembler is a separate mechanism with fewer targets.

| architecture | `@native` | `@viper` | inline assembler | sljit |
|---|---|---|---|---|
| x86-64 | yes | yes | no | yes |
| x86 32-bit | yes | yes | no | yes |
| ARM Thumb/Thumb-2 (Cortex-M: RP2040, STM32) | yes | yes | `@asm_thumb` | yes |
| ARM 32-bit, ARM mode | yes | yes | no | yes |
| Xtensa (ESP8266) | yes | yes | `@asm_xtensa` | **no** |
| Xtensa windowed (ESP32, S2, S3) | yes | yes | `@asm_xtensa` | **no** |
| RISC-V 32 (ESP32-C3/C6, RP2350 in RISC-V mode) | yes | yes | `@asm_rv32` | yes |
| AArch64 (arm64 Linux, Apple Silicon, RPi 64-bit) | **no** | **no** | no | yes |
| RISC-V 64 | **no** (the unix port only loads native `.mpy`) | **no** | no | yes |
| MIPS32 (Ingenic X2000) | **no** | **no** | no | yes |
| PowerPC, the webassembly port | no | no | no | PPC yes |

**The two coverages complement each other.** sljit has AArch64, MIPS and RISC-V 64, where
MicroPython has no emitter. MicroPython has Xtensa, where sljit has none.

## The same design as our core

| | MicroPython native | waboros |
|---|---|---|
| input | stack-machine bytecode | wasm, also a stack machine |
| compilation | one pass, no IR | one pass, no IR (D39, S0011) |
| value stack | cached in registers: each slot is `STACK_VALUE`, `STACK_REG` or `STACK_IMM`, materialised lazily (`need_reg_all`, `need_stack_settled`) | `tos`, the register pool, `Imm`/`AddK` operands, spilled to the frame before labels and calls |
| layering | the shared `emitnative.c` over a per-ISA assembler | the shared core over the D39 backend and the per-ISA low layer |
| runtime calls | through `mp_fun_table`, in a pinned register (`REG_FUN_TABLE`: r7 on Thumb, rbp on x86-64) | through context words (the libcall block), the context in a pinned register |
| position independence | the table keeps process addresses out of the code, so it can be saved in a `.mpy` | the same (and the reason the sljit backend's libm/libc immediates were removed on 2026-11-16) |
| saved native code | native `.mpy`, `mpy-cross -march=...` | the AOT artifact, ELF64 (D59, S0014), cross-compiled (D35) |

This confirms the design from Cortex-M0 to x86-64: one pass, a register cache of the stack, and the
runtime through a pinned table.

### What differs

* **Semantics of operations.** `@native` calls the runtime for every operation on Python objects. We
  emit real instructions with wasm semantics. `@viper` is closer to us: machine words for `int`,
  `uint` and `bool`, and raw loads and stores through `ptr8`/`ptr16`/`ptr32`.
* **GC.** MicroPython's collector is conservative and scans the C stack as memory, so it needs no
  stack maps or call-site records. Ours are precise (D56, D65) because our collector moves objects. The comparison,
  and mark-compact as a variant for small memories: [GC-SMALL-MEMORY.md](GC-SMALL-MEMORY.md).
* **Exceptions.** MicroPython uses `nlr`, a non-local jump like `setjmp`/`longjmp` with a buffer in
  the frame. We return a status (trap-by-return) and use landing pads.
* **Traps and memory bounds.** Python has neither; we have explicit checks or guard mode with fault
  points.

## What to take from it

* **Xtensa, for a micro-compiler backend** (no sljit backend exists for it):
  * the encodings, both 2- and 3-byte instructions;
  * `l32r` and the literal pool for constants;
  * both calling conventions: `call0` (ESP8266) and windowed (`entry` / `call8` / `retw`, ESP32).
    The windowed ABI is the hardest part of Xtensa, and it is solved there.
* **Thumb-2, RV32, ARM and x86:** a second, independent reference next to sljit, for example to check
  an encoding or how a branch out of range is handled.
* **The value-stack cache** (`STACK_IMM`, lazy materialisation) on targets with few registers
  (Cortex-M0: eight low registers). It is worth comparing with our pool when a 32-bit backend is
  designed.

**License.** MicroPython is MIT ("Copyright (c) 2013-2026 Damien P. George"), as is waboros. Porting
code is allowed; keep that copyright notice in any file that carries ported code. CLAUDE.md's rule
still applies: read and understand, write our own, name the source; no line-by-line translation.

## What is not there (ours to add)

* **Floating point: none at all.** `viper` has no float type, so the assemblers have no FPU
  instructions. f32/f64 need our own encodings from the manuals: VFP on Cortex-M4F/M7, the
  single-precision FPU on ESP32, F/D on RISC-V. f64 on ESP32 stays in software.
* **64-bit values on 32-bit targets:** i64 in register pairs.
* **Division without hardware** (Cortex-M0): MicroPython calls its runtime; we need a libcall.
* **SIMD.**
* **GC call-site records, stack maps, fault points for guard mode** (D56, D65): our core's own.

## Why not `viper` as the base of waboros on MicroPython

1. **Wasm semantics do not fit.**
   * f32/f64 stay Python objects in viper, which is slow: it is the main load of numeric code.
   * i64 does not fit a viper word on 32-bit targets.
   * Traps (bounds, division by zero, `trunc` overflow) would be explicit Python code.
   * The word size differs per ISA (32 bits on MCUs, 64 on x86-64), so i32 needs masks on x86-64.
2. **No AArch64, RISC-V 64 or MIPS** (table above).
3. **It compiles on the device.**
   * A translated module is large: CoreMark's generated source is about 150 KB. That does not
     compile in an RP2040's 264 KB of RAM.
   * Compiling ahead with `mpy-cross -march=...` gives a native `.mpy`. That needs the same support
     for loading native code as a natmod, so "no C" is no longer an advantage.

### Layers for waboros on MicroPython (proposal)

| layer | where | what it gives |
|---|---|---|
| micro-compiler + C runtime as a natmod (D60) | sljit ISAs where a natmod loads (Thumb-2, ARM, RV32, x86, x86-64; AArch64/MIPS where MicroPython can load native code) | the main path: native speed, full wasm semantics, the same artifact and spec suite |
| a micro-compiler backend for Xtensa, encoder after `asmxtensa.c` | ESP8266, ESP32 | the same as above where sljit has no backend |
| wasm3 as a natmod (natify-sh/micropython-wasm3) | any port until a micro-compiler exists for it | a full wasm interpreter |
| `pyc` for MicroPython (bytecode; optionally `@viper` on integer-only functions where an emitter exists) | where no native code can be loaded | the fallback; viper only as a per-function optimisation |

**Correction on an earlier remark.** In the conversation `@viper` output from `w2py --micropython`
was suggested as a speed path. It stays possible only as the per-function optimisation in the last
row, for the reasons above.

## Status

A record, not a plan (maintainer, 2026-11-19). The `mpy` target is deferred (ROADMAP, phase 7); an
Xtensa backend and how to use `asmxtensa.c` are decided when that phase or a micro-compiler for
Xtensa is taken up.
