---
title: How other runtimes link separately compiled code (WAMR, Wasmtime, WasmEdge)
kind: reference
status: current
updated: 2026-11-21
---

# How other runtimes link separately compiled code

Research note for D70 (one boundary ABI per architecture, compiler internals private), kept so
the comparison is not redone. The question: can modules compiled separately, by different
compilers or compiler versions of one architecture, run and call each other in one store, and
what does the runtime fix so that they can? The findings come from reading the sources
(2026-11-21, the research done for D70); the paths are given per finding. Nothing here was built
or run.

## WAMR (WebAssembly Micro Runtime)

* **AOT and LLVM JIT functions are plain C-ABI functions.** The first argument is `exec_env`, the
  wasm parameters follow as C arguments, and no register is pinned across functions: the code
  generator keeps the memory base in a value it reloads after a call whenever memory can grow
  (`core/iwasm/compilation/aot_llvm.c`). The host enters them through per-architecture
  assembly that spreads an argument array onto C registers (`core/iwasm/common/arch/invokeNative_*.s`).
* **Fast JIT pins registers**: `rbp` and `r15` on x86-64 hold its execution context and frame, so
  its functions are not C functions; per signature it generates adapters to call LLVM-JIT code and
  back (`core/iwasm/fast-jit/jit_compiler.c`).
* **Calls between modules go through the runtime**: an import of another module is called through
  `aot_invoke_native` (`core/iwasm/aot/aot_runtime.c`), which swaps the `exec_env`'s module
  instance around the call. Multi-module linking is homogeneous in its running mode: every module
  of a link runs in the same mode (all AOT, or all one JIT), never mixed.
* **Compatibility is an exact version**: the loader compares `AOT_CURRENT_VERSION` exactly and
  checks the target's architecture and ABI fields (`core/iwasm/aot/aot_loader.c`,
  `load_target_info_section`); it does not check the CPU features the file was compiled for nor
  the bounds-check mode, and there is no fallback to another mode when a file is refused.
* **GC roots** come from a shadow-stack frame that compiled code writes for each call (values
  spilled to a frame the runtime can scan), not from stack maps of native frames.
* **Tail calls across modules** are not guaranteed to run in constant stack: a `return_call` to an
  import goes through the same runtime call path as a call.

What we take from it: the C-ABI function as the unit of linking (our record's `code`, D70), with
the context first. What we do differently: the frame (value stack) stays the second argument
instead of C arguments, so one signature serves every wasm type; calls between instances are
direct through the record, not through the runtime (D32: about 3 ns against microseconds); roots
come from per-site stack maps (D56), so code writes nothing per call; compilers may mix in a store;
cross-instance tail calls keep constant stack (D30, D70).

## Wasmtime

* A serialized module (`.cwasm`) is accepted only by the exact same Wasmtime version and
  compatible settings (`crates/wasmtime/src/engine/serialization.rs`: the version string and the
  engine's flags and tunables are compared, and a mismatch is an error).
* Code from Cranelift and from Winch does not mix in one engine: Winch's calling convention is
  its own, and the `winch_callable` tunable records which kind of code a module holds, so a module
  is refused by an engine of the other kind.

What we take from it: the exact compiler string as a cache key (S0014 Q3). What we do
differently (D70): compatibility follows the architecture, `format_version` and
`ctx_abi_version`, not the compiler; the exact string stays the cache key only.

## WasmEdge

* The AOT output is a "universal wasm": the original module with the native code in a custom
  section (`lib/loader/ast/section.cpp`). The loader uses the native code only when the version
  and the machine match exactly, and otherwise runs the module in its interpreter.

What we take from it: the same shape as our fat files (D18): the wasm travels with the code and is
the fallback. We regenerate machine code from it instead of interpreting.

## Summary

| runtime | unit of linking | registers pinned across modules | between modules | compatibility | mixing compilers |
|---|---|---|---|---|---|
| WAMR AOT / LLVM JIT | C function, `exec_env` first | none | through the runtime (`aot_invoke_native`) | exact `AOT_CURRENT_VERSION` | no (one mode per link) |
| WAMR Fast JIT | its own convention (`rbp`, `r15`) | yes | adapters per signature | exact version | no |
| Wasmtime | its own convention per compiler | per compiler | direct within one engine | exact version and settings | no (Cranelift or Winch) |
| WasmEdge | wasm + native section | its own | — | exact version and machine | interpreter fallback |
| waboros (D70) | C entry `int32 f(ctx, frame)` per architecture | none at the boundary; each compiler's own inside | direct through the function record | architecture + `format_version` + `ctx_abi_version` | yes, within one architecture |
