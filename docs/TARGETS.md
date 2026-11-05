---
title: Targets
kind: reference
status: current
updated: 2026-11-05
---

# Targets

A target turns the IR of a module into something the host can run, and runs it. Targets are added one
at a time; each one passes the same tests before it is selectable.

## The interface

A sketch of the idea; the interface as implemented is S0002 and `src/waboros/targets/base.py` (`probe()` and
`supports(module)` without `config`; no `serialize`/`deserialize` yet). Everything a target needs comes in,
nothing global:

```python
class Target(Protocol):
    name: str                      # "pyc", "mpy", "jit-x86_64", "jit-aarch64", ...
    version: int                   # bumps invalidate cached artifacts

    def probe(self, config: Config) -> Probe:
        """Can this target run here? (interpreter, architecture, OS, executable memory, CPU
        features). Returns ok/why-not and the environment string that goes into cache keys."""

    def supports(self, module: ModuleIR, config: Config) -> Support:
        """Can it compile *this* module? (features it uses: SIMD, memory64, threads, exceptions,
        GC, ...). A target may support a subset; the engine's target is fixed (D25), so an
        unsupported module is an `Unsupported` error, not a switch to another target."""

    def compile(self, module: ModuleIR, config: Config) -> Artifact: ...
    def serialize(self, artifact: Artifact) -> bytes: ...
    def deserialize(self, data: bytes) -> Artifact: ...

    def instantiate(self, artifact: Artifact, store: Store, imports: ResolvedImports) -> TargetInstance:
        """Create memories/tables/globals (through the runtime's facades), apply segments,
        bind imports, run start; return the callable exports."""
```

Contracts every target keeps:

* **Semantics are the spec's**, checked by the spec test suite (TESTING.md), including NaN and trap
  behaviour; traps are reported with Wasmtime's `TrapCode`.
* **Memory goes through the runtime facade** (`Memory`), so hosts and WASI are target-independent.
* **Calls from Python** take and return Python values as wasmtime-py does (`int` for i32/i64 with
  Wasmtime's signedness conventions, `float`, `None`/objects for references).
* **Host functions** are plain Python callables (receiving a `Caller` when asked for one); their
  exceptions propagate to the caller of the wasm export.
* **Stack depth** is bounded as ARCHITECTURE.md describes; no target may crash the interpreter.
* **Artifacts are self-describing** (target name, version, environment) so a wrong one is refused,
  never executed. JIT artifacts follow [ARTIFACT.md](ARTIFACT.md): declared ISA level, format
  version 1, context ABI version 1.

## Selection

`Config.target` is `"auto"` (default) or a name or a list of names in order of preference.

For `"auto"`, the engine probes in this order and takes the first that is ok:

1. the JIT for this architecture and ABI (`platform.machine()`, `sys.platform`, pointer size), if
   executable memory can be obtained (probe: map RW, write `ret`, flip to RX — `mprotect`, `MAP_JIT`
   + `pthread_jit_write_protect_np` on macOS arm64, `VirtualAlloc`/`VirtualProtect` on Windows — and
   call it once through ctypes), and if the CPU reaches the ISA level the code will be compiled for
   (ARTIFACT.md, part 1);
2. `mpy` on MicroPython;
3. `pyc` (always available).

Platforms that restrict run-time code (and CPU control-flow integrity: CET, BTI) are listed with
possible answers in RISKS.md, R2.

The choice depends on the environment only, never on a module's content (D25): one target per engine
and store, no per-module fallback. A JIT is selected by `auto` only when its expected failures equal
`pyc`'s in the claimed groups. When `auto` ends on `pyc` because no JIT can run here, it warns once
with `JitUnavailableWarning` (D31); an explicit `jit-*` that cannot run is an `Unsupported` error.
`WABOROS_TARGET` (environment) overrides for testing.

## Adding an architecture step by step

The JIT is split into a shared core and per-architecture parts, so an architecture can start small and
grow:

| shared (`jit/common`) | per architecture (`jit/<arch>`) |
|---|---|
| executable memory per OS | instruction encoder |
| ctypes entry trampoline protocol, host callbacks | ABI: argument registers, callee-saved set, stack alignment |
| value-stack frame layout (frame overlap) | register pools (pinned locals, scratch) |
| lowering of HIR to LIR: a machine-neutral sequence of operations with operand kinds (imm / reg / mem) (ARCHITECTURE.md, "The IR") | instruction selection for LIR operations |
| pinned-local choice (uses weighted by loop depth), top-of-stack caching | lowering of memory access (bounds check + addressing modes) |
| labels, fixups, trap stubs, function table, artifact format | relocation/fixup kinds of the ISA |

How others solved the same problems on each architecture (Winch, V8 Liftoff, TinyCC, asmjit, DynASM,
MicroPython's emitters): REFERENCES.md — read to understand, then write our own.

An architecture is added in stages; until it is complete `auto` does not select it (D25):

1. **Integer core**: i32/i64, locals, globals, control flow, calls, memory loads/stores with bounds
   checks, traps. (What `w2jit2` covers on x86-64, minus i64 arithmetic.)
2. **Floats** (f32/f64 incl. NaN rules and conversions), `call_indirect` and tables, `memory.grow`,
   bulk memory, reference types, multi-value, multi-memory, memory64.
3. **SIMD** (and relaxed SIMD), then threads/atomics, exceptions, tail calls, GC/function references.

A feature of a later stage is `unsupported` on that architecture until written; the engine then runs
on `pyc` (D25). `jit-x86_64` and `jit-aarch64` have stages 1 and 2 as far as the core group needs them
(multi-memory included, D37), tail calls and typed function references of stage 3; SIMD (S0005):
`jit-x86_64` compiles every operator with SSE2 only and claims `simd/` (step 5d3); `jit-aarch64`
compiles every operator in ARMv8.0-A NEON and claims `simd/` (step 5e2); memory64 is done on `pyc` (S0009 step 1; S0009 is implemented); the JITs compile i64 memories (step 2a: explicit checks in every mode; step 2b: reserved as Wasmtime, moved by a grow past the reservation, D50) and i64 tables (step 3), and claim `memory64/`; exceptions are done on `pyc` (S0012 step 2: `try_table` is a `try`/`except` of one private class, spec `exceptions/` claimed) and on `jit-x86_64` (step 3: status 14 through frames, landing pads per `try_table`, objects bumped from a store arena in code, D52), not yet on `jit-aarch64` (step 4); GC is not done; relaxed SIMD and wide arithmetic are done on all three targets (S0008 steps 1-3).

**An alternative to evaluate for later architectures: copy-and-patch.** Each wasm operation becomes a
small C "stencil" compiled by clang for every target triple on *our* CI and shipped as bytes in the
pure-Python wheel; at run time Python only stitches stencils and patches holes (relocations). Per
architecture, the Python side is then only the relocation patcher (~100–200 lines) and executable
memory; SIMD stencils written with clang's generic vector types become SSE / NEON / scalar code by
themselves. CPython's own JIT works this way. Costs: clang (pinned version) in our CI, relocation
formats tied to LLVM versions, `musttail`/`preserve_none` not available on every architecture (the
fallback is threaded code returning to a dispatcher). Decide after `jit-aarch64` exists by hand,
with real numbers for both.

## The targets planned

| target | runs on | state in the package / in the PoC | notes |
|---|---|---|---|
| `pyc` | CPython ≥ 3.11 (D19; PoC measured down to 3.8), PyPy | package: core group complete, frozen for new features except the proposals that land on every target together (SIMD, S0005 step 5b: all of `simd/`, 25 990 commands; multi-memory, memory64, relaxed SIMD, wide arithmetic) and S0010, the `_br` ladders, the maintainer's exception for speed (spec core 20509 pass; all claimed directories 63 231 pass, 93 expected failures); a mode that carries f32/f64 as int bit patterns, the default on 32-bit x86 only (D46; i686: 0 spec failures; `WABOROS_PYC_FBITS=1|0`). PoC: CoreMark | floor for everything; artifact = code object; the fastest pure-Python option on PyPy (2 300–2 900 it/s) |
| `mpy` | MicroPython (unix port tested, 1.29.0) | CoreMark | `.mpy` ahead of time or source on device; `@native` (94 it/s); next: viper code, i.e. MicroPython's own emitters as native back ends for every MCU architecture (below) |
| `jit-x86_64` | Linux (glibc, musl), macOS (Intel), Windows, Android (the map below) (app process, CI emulator) | package: the core group at parity with `pyc` (spec core 20509 pass, same 89 expected failures: integers, SSE2 floats, `memory.grow`, tables, `call_indirect`, references, bulk memory, tail calls, function imports through records; CoreMark 17 526 it/s; SIMD: every operator with SSE2 only, `simd/` claimed at parity with `pyc`, 25 481 runnable commands pass, S0005 step 5d3); selected by `auto`, which falls back to `pyc` with a warning (D31). PoC: CoreMark (`w2jit2`, SysV only, 17 662 it/s in the same session) | bounds: guard pages by default on Linux glibc and musl x86-64, macOS, Windows and Android (D44, S0006 steps 3, 4 and 5; `WABOROS_JIT_BOUNDS=explicit` to opt out); explicit checks elsewhere; one artifact for every OS (D23): the Win64 entry trampoline and bridge and a `VirtualAlloc` loader are written; `platforms.yml` runs the tests and the spec suite on Windows x64 and macOS x86-64 (a real Intel runner) and `ci` on Linux |
| `jit-aarch64` | Linux, macOS (Apple Silicon: `MAP_JIT`), Windows on ARM, Android `arm64_v8a` (not checked in CI) | package: parity with `pyc` and `jit-x86_64` (spec 28903 pass in phase 4; 63 231 pass, 93 expected failures over every claimed directory since memory64, S0009), selected by `auto` on arm64; SIMD: every operator in ARMv8.0-A NEON, `simd/` claimed at parity with `pyc` (S0005 5c, 5e1, 5e2); CI on native arm64 runners (job `arm64` of `ci` on Linux; `platforms.yml` on Linux, macOS and Windows on Arm); bounds: guard pages by default on Linux glibc, macOS and Windows on Arm (S0006 steps 2, 3 and 4, D44); explicit checks elsewhere (Linux musl: the JIT needs libgcc's `__clear_cache`, absent from stock Alpine; Android arm64) | the second architecture: proved the backend split, interface frozen after it (D39) |
| `jit-riscv64` | as available | — | the only further JIT architecture (D48); a wasm micro-compiler (MICRO-COMPILERS.md), QEMU user-mode in CI |
| `lua` / `luajit` | Lua 5.4, LuaJIT | CoreMark | **not a waboros target** (D20, rejected; code stays in `poc/`): an experiment showing the IR serves other languages (271 / 1 873 it/s); the shared layers stay language-neutral so such backends remain possible (DECISIONS.md D17) |
| `mypyc` | CPython + mypyc runtime | CoreMark | **dropped**: needs `librt` at run time (a dependency) and is 9× slower than wasm2c for a native build |

**MicroPython's own native emitters as JIT back ends (research).** Our JIT is the same idea as
MicroPython's `@micropython.viper` and `@micropython.asm_thumb` / `asm_xtensa`: machine code emitted
into RAM at run time — they from Python source, we from wasm. Wasm is statically typed with a flat
byte memory, which is almost exactly viper's model (machine-word integers, raw `ptr8/16/32` pointers).
So on MicroPython the `mpy` target can lower HIR to **viper-flavoured Python** and let MicroPython's
emitter produce the machine code: native back ends for the architectures its ports support (x86/x64,
ARM Thumb on Cortex-M, Xtensa on ESP32, RISC-V — per port and release, to check) without an encoder of
ours. Measured: a 32-bit load through viper `ptr32` costs 3.4 ns against ~150 ns through `struct`
(BENCHMARKS.md). The hottest functions could go further through the inline assemblers
(`asm_thumb`, `asm_xtensa`), with MicroPython doing the instruction encoding. Limits to work around:
viper's argument-count and calling-convention limits (to check per release), i64 and floats remaining
objects, `ptr` accesses without bounds checks (we emit the checks), and the `mpy-cross` native bug on
very large functions (LESSONS.md 17).

Where no JIT is allowed at all — iOS (Pythonista), Pyodide, hardened Linux without executable
anonymous memory — `pyc` is the target. In Pyodide the browser's own `WebAssembly` is faster than any
of ours; a thin target calling it through `js` is an option to consider.

**The role of `pyc` as native compilers spread** (maintainer, 2026-11-03): once most targets have
a JIT or a micro-compiler of their own, `pyc` is (1) the internal reference every feature passes on
first and the JITs are held to (D25; Wasmtime stays the external oracle), and (2) the fallback where
no machine code is possible (iOS, strict W^X) or no compiler exists yet (32-bit and the other
architectures, D48, unless the later sljit track gives them a JIT; Pyodide, which has its own track: the guest as an Emscripten side module compiled by the
host's WebAssembly engine, [PYODIDE.md](PYODIDE.md)). So it needs no CI cell per OS and architecture of its own: a few
interpreters, plus the platforms where it is the only target.

The BSDs are not a target (not in cibuildwheel's map below): no CI, no claim; `auto` on them is
whatever the probe finds, and guard pages stay off there.

## Python platforms and what waboros gives them

The platforms CPython ships for (cibuildwheel's `archs`, `main`, 2026-11, as the maintainer's map of
the targets worth having). **Rule (maintainer, 2026-11-02): waboros's platforms are the ones
cibuildwheel supports, iOS excepted, where only `pyc` runs.** Anything outside this map (the BSDs,
for one) is not a target. waboros itself has no wheels (pure Python); this says where a JIT makes sense.

| platform (`archs`) | what waboros gives it |
|---|---|
| Linux / macOS / Windows `x86_64`/`AMD64`, `aarch64`/`arm64`/`ARM64` | JIT (`jit-x86_64`, `jit-aarch64`), CI on native runners: `platforms.yml` (cibuildwheel; Linux glibc and musl x86_64, Linux aarch64, macOS x86_64 and arm64, Windows x64 and ARM64) and `ci` |
| Android `x86_64`, `arm64_v8a` (CPython 3.13+; older Pythons on Android are not a target) | the same backends where the app may map executable memory (our W^X path: write, then switch to execute). The Android cell of `platforms.yml` (`x86_64` only): CPython's testbed app on a managed emulator (KVM) runs the tests that need no wasmtime as an oracle (WAT from the WAT cache, TESTING.md) with `--jit-bounds=guard,explicit`, and `tests/jit/test_android.py`: the JIT runs in the app process (`untrusted_app`), `auto` selects `jit-x86_64` and gives guard pages, guard faults reach our handler through ART's libsigchain, CoreMark validates (S0006 step 5; first shown by a hand-made job, CI run 41351056918, docs/RISKS.md R2). `arm64_v8a`: no job, because the Android emulator has no Linux arm64 build and GitHub's arm64 runners cannot run it (CPython's own CI builds it there and skips the tests); Termux (a shell, a permissive environment, not an app process) would be the manual route |
| iOS (`arm64_iphoneos`, simulators) | `pyc` only: third-party apps get no JIT (`MAP_JIT` needs an entitlement); `auto` falls back with the D31 warning |
| Pyodide `wasm32` | `pyc` today (a cell of `platforms.yml`); later a separate target: the guest as an Emscripten side module loaded with `ctypes.CDLL`, compiled by the browser's engine (experiment: 92–95 % of direct `WebAssembly` on CoreMark, [PYODIDE.md](PYODIDE.md)) |
| Linux `riscv64` | the third architecture — a wasm micro-compiler (ROADMAP q14) |
| every other architecture: Linux `s390x`, `i686`, `armv7l`, `ppc64le`; Windows `x86`; loongarch64, mips32el | `pyc` (D48; optionally, later, the sljit track: SLJIT-BACKEND.md): Wasmtime has no native code generator on the 32-bit ones (they get Pulley); s390x is excluded by the maintainer (big-endian host, QEMU-only CI). CI: `i686` and `armv7l` cells in `platforms.yml` (armv7l native on the arm runner); `ppc64le`, `s390x` and `riscv64` only as optional QEMU jobs started by hand (`exotic`) |
