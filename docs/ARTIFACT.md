---
title: JIT artifact, ISA levels and the context ABI
kind: reference
status: current
updated: 2026-11-03
---

# JIT artifact, ISA levels and the context ABI

A JIT target's output is plain machine code with a small, fixed boundary. Nothing in it is
Python-specific (measured: the same module compiled under CPython 3.8 and 3.13 gives byte-identical
code), so the artifact is **a language-neutral format**: any host that can map executable memory, call
a C function pointer and pass C callbacks can load and run it (C/C++, Dart FFI, LuaJIT, Ruby Fiddle,
Java FFM, .NET, Deno/Bun, MicroPython's `ffi`). This document is the contract such hosts — and our own
Python loader — rely on. It has three parts, each versioned.

> **Scope (DECISIONS.md D18).** First of all this is the format of the waboros runtime's own cache entries
> and serialized files (`Module.serialize`, `waboros compile`): waboros follows the Wasmtime model, the
> runtime lives in the package and nothing is embedded in the artifact. Loading from other languages
> stays possible because the boundary is neutral, but it is optional.

> The PoC (`poc/w2jit.py`, `w2jit2.py`) predates this contract: its context layout and entry signature
> are a subset of version 1 below and must be brought in line in phase 5.

## 1. ISA levels

An artifact declares the **instruction-set level** it was compiled for; a loader refuses an artifact
the CPU cannot run.

| architecture | baseline (default for portable artifacts) | optional levels / features the compiler may use |
|---|---|---|
| x86-64 | **x86-64-v1**: SSE, SSE2 (every x86-64 CPU) | `x86-64-v2` (SSE3, SSSE3, SSE4.1, SSE4.2, POPCNT), `x86-64-v3` (AVX, AVX2, BMI1/2, LZCNT, FMA, MOVBE), single features by name |
| aarch64 | **ARMv8.0-A** with FP and Advanced SIMD (NEON) | `lse` (v8.1 atomics), `fp16`, `dotprod`, `rcpc`, … by name |
| riscv64 | planned (D48, the third JIT architecture, after `arm64.wasm`); defines its baseline here before its first release | RVV 1.0 as an optional level |
| later | none planned: every other architecture runs `pyc` (D48) | |

Rules:

* **Portable artifacts** (`waboros compile`, shipped files) use the baseline unless asked otherwise.
  An operation whose natural instruction is above the baseline gets a baseline sequence. Example found
  in the PoC: `f64.trunc` was emitted as `roundsd` (SSE4.1, x86-64-v2); at v1 it needs an SSE2
  sequence (convert-truncate for |x| < 2⁵², otherwise x itself, preserving sign and NaN). The same
  applies to `floor`, `ceil`, `nearest`, `popcnt`, `clz`, `ctz`.
* **Baseline first (D24).** Every operation is implemented at the baseline first; the baseline path is
  mandatory and is what every artifact uses by default. A path above the baseline exists only as an
  **optional, declared variant** next to its baseline twin, added when a measurement shows it pays;
  an above-baseline instruction the PoC already uses is kept this way, not dropped (known so far:
  `roundsd`, SSE4.1 / x86-64-v2, for `f64.trunc` in `poc/w2jit.py` and `poc/w2jit2.py`).
* **Local cache artifacts** may use `isa="native"` once optional variants exist: the features of the
  running CPU, recorded in the artifact and in the cache key, so a cache moved to another machine is
  recompiled, not executed.
* **Checked, not trusted**: CI disassembles generated code (`objdump`) and fails if any instruction is
  above the level the artifact declares.
* `Config.isa` (waboros addition): `"baseline"` (default), a level name, or a level plus feature names
  (explicit opt-in, stage 1 of D33); `"native"` comes later (stage 2) and becomes the default only by a
  separate decision. A requested level the CPU lacks is refused at load, naming the missing features.
* **Detection at load time, stdlib only**: on x86-64 a small `cpuid` stub run through the same
  executable-memory + ctypes path (works on every OS); on aarch64 `getauxval(AT_HWCAP/AT_HWCAP2)` via
  ctypes on Linux/Android, `sysctlbyname("hw.optional.*")` on macOS, `IsProcessorFeaturePresent` on
  Windows. Hosts in other languages do the equivalent.

## 2. Artifact format (version 1)

A little-endian binary container. Self-contained: a host needs **no wasm parser** to instantiate it.
Open before the format is fixed (phase 5): our own container or ELF64 as a container only, as
Wasmtime does; see [ARTIFACT-CONTAINER.md](ARTIFACT-CONTAINER.md) (maintainer's note, draft).

**Header** (fixed size):

| field | type | meaning |
|---|---|---|
| `magic` | 8 bytes | `WABOROS\0` |
| `format_version` | u16 | this document's part 2 version (1) |
| `ctx_abi_version` | u16 | part 3 version (1) |
| `arch` | u16 | 1 = x86-64, 2 = aarch64, 3 = riscv64, … (registry in this file) |
| `isa_level` | u16 | per architecture: 1 = baseline, 2 = v2, 3 = v3, … |
| `isa_features` | u64 | bitset of extra features (registry per architecture) |
| `flags` | u32 | code-affecting options: fuel, epoch/interrupt polling, bounds-check mode, … Bit 0 `BOUNDS_GUARD` and bits 8–15 `GUARD_LOG2` are fixed by S0006; the package computes them (`FLAGS` in `guard.py`) but serializes no artifact before the phase-5 spec |
| `compiler` | str | `waboros <version> <target> <target version>` |
| `module_sha256` | 32 bytes | hash of the wasm bytes |
| `config_sha256` | 32 bytes | hash of every code-affecting option |
| `section_count` | u32 | |

**Sections** (`id: u16`, `length: u32`, payload; unknown ids are skipped):

| id | section | content |
|---|---|---|
| 1 | `CODE` | the machine code, position-independent: internal branches relative, everything absolute through the context register |
| 2 | `ENTRIES` | entry trampolines, one per host calling convention: `{sysv: offset, win64: offset}` on x86-64; `{aapcs64: offset, win-arm64: offset}` on aarch64. The host picks its own |
| 3 | `FUNCS` | per defined function: code offset, type index, frame size (slots), name (if any) |
| 4 | `TYPES` | function types (param/result value types) |
| 5 | `IMPORTS` | function imports: module, name, type index, context slot; memory/table/global imports with their types |
| 6 | `EXPORTS` | name → kind + index |
| 7 | `MEMORIES` | per memory: min, max, page size, 64-bit, shared |
| 8 | `TABLES` | per table: element type, min, max, address type (i32 or i64, S0009) |
| 9 | `GLOBALS` | per global: type, mutability, initial value (or the import it comes from) |
| 10 | `SEGMENTS` | data and element segments: active (target, offset) or passive, bytes / function indices |
| 11 | `START` | start function index, if any |
| 12 | `TRAPMAP` | code offset ranges → wasm function index + wasm byte offset (for `Trap.frames`); with the guard flag also the fault map, `{fault_offset, resume_offset}` per memory instruction ([S0006](specs/S0006-guard-pages.md), "Artifact"). Neither is written to a file yet |
| 13 | `RELOCS` | empty in version 1 (code is position-independent); reserved for copy-and-patch |
| 14 | `WASM` | optional: the original wasm bytes. Its presence makes a **fat** file (below) |
| 0xFFFF | `CHECKSUM` | sha256 of everything before it |

Strings are UTF-8 with a u32 length prefix. A loader refuses an artifact whose `magic`,
`format_version` major, `ctx_abi_version`, `arch` or ISA it does not support — it never "tries".
`Module.serialize()` writes this format; the cache stores it.

**Thin and fat files (D18).** A thin file (no `WASM` section) is refused on any mismatch. A fat file is
never refused: when the loader cannot use the code — another waboros or target version, another ISA
level, a compiler version on the known-bad list, a format it does not know — it checks that the
embedded wasm matches `module_sha256` and regenerates from it. For this the loader must be able to
read the header and find section 14 in every format version, so the header prefix (`magic`,
`format_version`) and the section framing are frozen across versions.

**Other targets.** Serialized `pyc` and `mpy` modules use the same container with their own
`arch`/`isa` meaning (the bytecode magic / `.mpy` version) and the marshalled code object or `.mpy`
as `CODE`; to settle in phase 2. File names: `<name>.<target>.<ext>`, e.g. `core.jit-x86_64-v1.w2a`,
`core.pyc-cp313.w2a` (`.w2a` is a working extension).

## 3. Context ABI (version 1)

What compiled code expects from its host at run time. Any change to a word's meaning or position is a
new `ctx_abi_version`; new words are only ever appended in reserved space.

**Entry.** Each trampoline in `ENTRIES` is a C function in the host's calling convention:

```c
int32_t entry(uint64_t *ctx, uint64_t *frame, const void *code_of_function);
```

* `frame` points into the host-allocated **value stack**; the arguments are written to `frame[0..n)`,
  the results come back in `frame[0..m)`, one 64-bit slot per value (i32 zero-extended in the low half;
  i64 as is; f32/f64 as bit patterns; v128 takes two slots, the low 64 bits (bytes 0–7) in the first
  slot or word, as for a v128 global; references as pointer-sized values).
* The return value is `0` or a trap code (below). On a trap the machine stack is unwound to the
  trampoline, which returns normally: the host never sees a signal or a non-local jump.
* **Re-entrant**: the trampoline keeps nothing in the context but `TRAP_CODE`, so wasm → host → wasm
  nesting works (the PoC lacked this; LESSONS.md 4).

**Context words** (`uint64_t ctx[]`, 8-byte aligned, at a stable address for the instance's lifetime;
the phase-3 meaning of each word and the register roles are in [S0003](specs/S0003-jit-context-abi.md)):

| index | name | direction | meaning |
|---|---|---|---|
| 0 | `SAVED_SP` | internal | zero (traps return through frames, and the guard-page fault handler resumes at a stub, S0006, so neither uses it); kept reserved |
| 1 | `TRAP_CODE` | code → host | written by the trampoline when it returns a non-zero status |
| 2 | `INTERRUPT` | host → code | non-zero: the code returns `INTERRUPT` at its next poll (any thread may set it) |
| 3 | `FUEL` | both | remaining fuel (signed), when compiled with fuel |
| 4 | `EPOCH_DEADLINE` | host → code | when compiled with epochs |
| 5 | `VSTACK_LIMIT` | host → code | address the value stack must not cross → `STACK_OVERFLOW` |
| 6 | `NSTACK_LIMIT` | host → code | lowest native stack address allowed → `STACK_OVERFLOW` |
| 7 | `HOST_ERROR` | host → code | the status of the current host call: the bridge presets 13, the host function writes its status last (D34) |
| 8 | `HOST_BRIDGE` | host → code | address of the host-call bridge for the host's calling convention |
| 9 | `TAIL_SWITCH` | host → code | address of the cross-context tail-call trampoline (below; D30) |
| 10–15 | reserved | | zero |
| 16 … | per memory *i*: `base`, `size_bytes` (2 words) | host ↔ code | imported memories first; the base never moves (4 GiB + 32 MiB reserved, committed on grow; S0006); code that keeps a memory in registers (x86-64: memory 0 in r13/r14) reloads them after every host call, any other memory is read here at each access |
| … | per table *i*: `base`, `size` (2 words) | host ↔ code | `base`: the element words (one per element), `size`: elements; `table.grow` may move the elements, so code reads both words at each use and never keeps them across a host call. A `funcref` element (and value) is 0 (null) or the address of a function record (below); an `externref` element is a host handle (0 = null) |
| … | per global *i*: 1 word (2 for v128) | both | imported globals: a pointer to the owner's word instead |
| … | per function import *i*: 1 word | host → code | address of a host function (signature below) |
| … | per libcall *j*: 1 word | host → code | a runtime service, same signature as an import, in a fixed order, all six words in every context: 0 `memory.grow`, 1 `table.grow`, 2 `table.init`, 3 `elem.drop`, 4 `memory.init`, 5 `data.drop`; never computation (D25). The operands are in the slots, the instruction's immediates (table, segment) after them; the memory index of `memory.grow` and `memory.init` only when the module has more than one memory. Operands and results have the address width of the memory or table they name (S0009, an amendment of v1 without a bump, Q4): for an i64 memory the delta, the old size and its failure −1 of `memory.grow` and the destination of `memory.init` are 64-bit values (−1 is `2⁶⁴−1`), for an i32 one they are i32 zero-extended as before; `table.grow` and `table.init` of an i64 table the same way (S0009 step 3) |
| … | per signature *k*: 1 word | host → code | the canonical type id of a type the module's `call_indirect`s name (equal for equivalent types of every module of the process, never 0) |
| … | per function record *r*: 3 words | host → code | `{code address, canonical type id, context address}` of every function import and of every function the module can take a reference to (`ref.func`, segments): a defined one; an import from another instance of the same store (its code and context: a direct call); a host function (the module's thunk, which calls the import's word through the bridge). `call_indirect` checks bounds, null and the type id, then calls `code` with r15 = `context` (and that context's memory words); `call` and `return_call` of an import go through the import's record the same way (D32) |

Offsets of the variable part follow from the counts in the artifact, in exactly this order.

**Host functions** (imports and libcalls) share one C signature in the host's calling convention:
`int32_t f(uint64_t *ctx, uint64_t *slots)` — the arguments in `slots[0..n)`, one 64-bit slot per
value as for entries; the function writes its results to `slots[0..m)`, then, as its last action, its
status to `ctx[HOST_ERROR]`: `0`, a trap code, or `13` `HOST_ERROR` (the host keeps the actual
exception). Its C return value is ignored (D34). Compiled code calls it through the bridge
(`ctx[HOST_BRIDGE]`), which saves the pool registers the host convention lets a callee clobber,
presets `ctx[HOST_ERROR]` to 13, returns the word as the call's status and puts back the word's
previous value (an enclosing host call's preset): a host function that does not finish fails the call.

**Tail calls** (D30). A `return_call*` puts its arguments at its own frame base, drops its native
frame and jumps, so both stacks stay constant. When the callee's function record names another
context, the code jumps to `ctx[TAIL_SWITCH]` instead, with the record's address and the caller's
return address where the architecture's table below puts them.
The trampoline (one per process and convention, in the host's code area like the bridge) does the
following. If that return address is its own `restore` point, it loads the record's context and jumps
to its code. Otherwise it saves the current context, loads the record's context and memory words, and
calls the code; at `restore` it puts the saved context back, reloads the memory words and returns the
callee's status. Every return matches its call.

**Per architecture** (S0004, D38; an amendment of version 1, which phase 5 fixes): what a host that
provides the bridge or the tail-call trampoline itself must follow. Everything else in this part is
the same 64-bit little-endian words for every architecture.

| | x86-64 | aarch64 |
|---|---|---|
| bridge call | `eax` = word, `rdx` = slots; `call` | `x0` = word, `x1` = slots; `blr x16` |
| bridge preserves | pool (`rbx rbp rsi rdi r8–r11`), `r12`–`r15` | `x2`–`x8`, `x19`–`x28`, `x29`, `sp` |
| `tail_switch` | record in `rdx`, return address at `[rsp]` | record in `x16`, return address in `x30` |
| landing pad | `endbr64` | `bti c` (functions, trampolines), `bti j` (jump-table pads) |
| `CODE` alignment | none | load address ≡ 0 mod 4096 (`adrp`) |
| instruction cache | coherent | invalidate the loaded range before the first call |

**Trap codes** (keep the PoC's first six numbers; names are Wasmtime's `TrapCode`):

| code | trap | code | trap |
|---|---|---|---|
| 0 | none | 8 | `BAD_SIGNATURE` |
| 1 | `UNREACHABLE` | 9 | `TABLE_OUT_OF_BOUNDS` |
| 2 | `MEMORY_OUT_OF_BOUNDS` | 10 | `NULL_REFERENCE` |
| 3 | `INTEGER_DIVISION_BY_ZERO` | 11 | `OUT_OF_FUEL` |
| 4 | `INTEGER_OVERFLOW` | 12 | `INTERRUPT` |
| 5 | `STACK_OVERFLOW` | 13 | `HOST_ERROR` (waboros) |
| 6 | `BAD_CONVERSION_TO_INTEGER` | 14 | `UNCAUGHT_EXCEPTION` |
| 7 | `INDIRECT_CALL_TO_NULL` | 15 | `HEAP_MISALIGNED` |

**Versioning rules**

* `format_version` and `ctx_abi_version` are independent. Loaders accept exactly the versions they
  know; the compiler can emit older versions while hosts migrate.
* Any change here goes with: the version bump, this document, `DECISIONS.md`, and a test that loads an
  artifact of the old version and checks it is refused (or still runs, if supported).
* A small reference loader in C is part of the test suite and runs in CI from phase 5 (D36), to keep the contract honest outside
  Python.
