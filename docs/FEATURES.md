---
title: Features
kind: reference
status: current
updated: 2026-11-01
---

# Features

The goal is everything Wasmtime supports for core WebAssembly. The component model is the
exception: components are supported **fused** into one core module (`meld fuse`), with WASI 0.2
provided by the WASI host (to decide; fusedwasi is the candidate); nothing of `wasmtime.component` is planned.

The checklist below is generated: `python3 tools/features.py` writes `features.json` (the format of
the WebAssembly website's [features.json](https://webassembly.org/features/), pinned in
`docs/upstream/`) and the table. A feature is "yes" on a target only if its spec tests pass there;
"partial" lists the failing tests in `features.json`; "-" is not supported or its spec tests are not
run yet. Wasmtime's column is the upstream data. `pyc` is computed from `tests/spec/xfail/pyc.txt`
and the command counts in `tests/spec/counts.json`; each cell shows `ok/total` commands. Proposal
directories (`test/core/<dir>/`) count only on a target that claims the feature
(`tests/spec/claims.py`); an unclaimed feature is "-". `jit-x86_64` and `jit-aarch64` are computed the same way from their `tests/spec/xfail/<target>.txt`; they claim the same directories as `pyc` (D25 parity). Every registered target has a column (`tests/test_features.py`). `mpy` is not listed yet.

## Proposals

<!-- features:begin -->
| feature | phase | Wasmtime | pyc | jit-x86_64 | jit-aarch64 |
|---|---|---|---|---|---|
| Acquire-Release Atomics (`acquireReleaseAtomics`) | 2 | - | - | - | - |
| JS BigInt to Wasm i64 Integration (`bigInt`) | 5 | - | - | - | - |
| Branch Hinting (`branchHinting`) | 5 | flag | - | - | - |
| Bulk Memory Operations (`bulkMemory`) | 5 | since 0.20 | partial 7483/7485 | partial 7483/7485 | partial 7483/7485 |
| Compact Import Section (`compactImportSection`) | 4 | - | - | - | - |
| Compilation Hints (`compilationHints`) | 2 | - | - | - | - |
| Component Model (`componentModel`) | 1 | - | - | - | - |
| Custom Text Format Annotations (`customAnnotationSyntaxInTheTextFormat`) | 5 | yes | - | - | - |
| Custom Descriptors and JS Interop (`customDescriptors`) | 3 | - | - | - | - |
| Custom Page Sizes (`customPageSizes`) | 3 | flag | - | - | - |
| ESM Integration (`esmIntegration`) | 3 | - | - | - | - |
| Legacy Exception Handling (`exceptions`) | inactive | - | - | - | - |
| Exception Handling with exnref (`exceptionsFinal`) | 5 | since 47 | - | - | - |
| Extended Constant Expressions (`extendedConst`) | 5 | since 25 | yes 346/346 | yes 346/346 | yes 346/346 |
| Extended Name Section (`extendedNameSection`) | 2 | - | - | - | - |
| Flexible Vectors (`flexibleVectors`) | 1 | - | - | - | - |
| Frozen Values (`frozenValues`) | 1 | - | - | - | - |
| Garbage Collection (`gc`) | 5 | since 47 | - | - | - |
| Half Precision (`halfPrecision`) | 1 | - | - | - | - |
| Instrument and Tracing Technology (`instrumentAndTracingTechnology`) | inactive | - | - | - | - |
| JIT Interface (`jitInterface`) | 1 | - | - | - | - |
| JS Primitive Builtins (`jsPrimitiveBuiltins`) | 2 | - | - | - | - |
| JS String Builtins (`jsStringBuiltins`) | 5 | - | - | - | - |
| JS Promise Integration (`jspi`) | 5 | - | - | - | - |
| Memory64 (`memory64`) | 5 | since 30 | - | - | - |
| Memory Control (`memoryControl`) | 1 | - | - | - | - |
| More Array Constructors (`moreArrayConstructors`) | 1 | - | - | - | - |
| Multibyte Array Access (`multibyteArrayAccess`) | 2 | - | - | - | - |
| Multiple Memories (`multiMemory`) | 5 | since 15 | yes 912/912 | yes 912/912 | yes 912/912 |
| Multi-value (`multiValue`) | 5 | since 0.17 | yes 1253/1253 | yes 1253/1253 | yes 1253/1253 |
| Import/Export of Mutable Globals (`mutableGlobals`) | 5 | yes | partial 431/439 | partial 431/439 | partial 431/439 |
| Numeric Values in WAT Data Segments (`numericValuesInWat`) | 2 | - | - | - | - |
| Profiles (`profiles`) | 1 | yes | - | - | - |
| Reference Types (`referenceTypes`) | 5 | since 0.20 | partial 397/431 | partial 397/431 | partial 397/431 |
| Relaxed Dead Code Validation (`relaxedDeadCodeValidation`) | 2 | - | - | - | - |
| Relaxed SIMD (`relaxedSimd`) | 5 | since 15 | - | - | - |
| Rounding Variants (`roundingVariants`) | 2 | - | - | - | - |
| Non-trapping float-to-int Conversions (`saturatedFloatToInt`) | 5 | yes | yes 619/619 | yes 619/619 | yes 619/619 |
| Shared-Everything Threads (`sharedEverythingThreads`) | 1 | - | - | - | - |
| Sign-extension Operators (`signExtensions`) | 5 | yes | yes 876/876 | yes 876/876 | yes 876/876 |
| Fixed-width SIMD (`simd`) | 5 | since 0.33 | yes 25990/25990 | - | - |
| Stack Switching (`stackSwitching`) | 3 | - | - | - | - |
| Reference-Typed Strings (`stringref`) | 1 | - | - | - | - |
| Tail Call (`tailCall`) | 5 | since 22 | yes 187/187 | yes 187/187 | yes 187/187 |
| Threads (`threads`) | 4 | since 15 | - | - | - |
| Type Imports (`typeImports`) | 1 | - | - | - | - |
| Type Reflection for JS API (`typeReflection`) | 1 | - | - | - | - |
| Typed Function References (`typedFunctionReferences`) | 5 | since 47 | yes 127/127 | yes 127/127 | yes 127/127 |
| WebAssembly C and C++ API (`wasmCApi`) | 1 | - | - | - | - |
| Web Content Security Policy (`webContentSecurityPolicy`) | 5 | - | - | - | - |
| Wide Arithmetic (`wideArithmetic`) | 4 | flag | - | - | - |
<!-- features:end -->

**Implementation notes per proposal** (hand-written; Wasmtime's `Config.wasm_*` switch in brackets):

* multi-memory (`wasm_multi_memory`): **needed for fused components** (D7).
* memory64 (`wasm_memory64`): addresses are already Python ints in `pyc`.
* SIMD (`wasm_simd`): `pyc` v128 as an int / 16 bytes, correct first; JIT: SSE2 / NEON at the baseline (D24).
* relaxed SIMD (`wasm_relaxed_simd`, `_deterministic`): follow Wasmtime's deterministic choice when asked.
* tail calls (`wasm_tail_call`): `pyc` uses a trampoline for functions with `return_call*`; the JIT replaces the frame.
* custom page sizes (`wasm_custom_page_sizes`): a property of the `Memory` facade.
* threads (`wasm_threads`, `Config.shared_memory`): `SharedMemory`; `pyc` atomics under the GIL, care for free-threaded builds.
* exceptions (`wasm_exceptions`): `Tag`, `try_table`, `throw`, `exnref`.
* typed function references (`wasm_function_references`): `call_ref`, non-null types.
* GC (`wasm_gc`, `Config.gc_support`): structs/arrays map naturally to Python objects in `pyc`.
* wide arithmetic (`wasm_wide_arithmetic`): easy in `pyc` (big ints).
* stack switching (`wasm_stack_switching`): after everything else.
* component model: out of scope; fused core modules only (D7).


## Runtime features

| feature | Wasmtime API | plan |
|---|---|---|
| interruption of long calls | (epochs; Ctrl+C in the CLI) | JIT: interrupt word polled by the code; `pyc`: yield points (ARCHITECTURE.md, "Long calls and the GIL") |
| fuel | `Config.consume_fuel`, `Store.set_fuel/get_fuel` | counters at entries and back-edges, compiled in on demand |
| epochs | `Config.epoch_interruption`, `Engine.increment_epoch`, `Store.set_epoch_deadline` | as fuel; checked against a context slot |
| limits | `Store.set_limits` | memory/table/instance limits in the runtime |
| stack limit | `Config.max_wasm_stack` | ARCHITECTURE.md, "Calls, stack depth and recursion" |
| serialization | `Module.serialize`, `Module.deserialize(_file)` | the cache artifact format |
| cache | `Config.cache` | ARCHITECTURE.md, "Cache" |
| WASI 0.1 | `Linker.define_wasi`, `WasiConfig` | the WASI host (candidate: fusedwasi `p1` twin) |
| WASI 0.2 (fused) | `Store.set_wasi`, `set_wasi_http` | the WASI host (candidate: fusedwasi `p2` twin); needs multi-memory |
| traps with frames | `Trap.frames`, `Frame` | `pyc`: from Python tracebacks; JIT: code-address map |
| NaN semantics | canonicalization option | `pyc` loses NaN payload bits through Python floats today; decide per spec tests |

## Per-target notes

* **`mpy` (MicroPython)** supports what its interpreter allows: no `SharedMemory`/threads in general,
  big-int i64 (slow on 32-bit MCUs), floats only on ports built with them. Unsupported features are
  reported by `Target.supports`, never mis-executed.
* **JIT targets** may lag the `pyc` target feature by feature; `auto` selects a JIT only when its
  expected failures equal `pyc`'s in the claimed groups, otherwise the engine runs on `pyc` (D25,
  TARGETS.md).
