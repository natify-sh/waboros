---
title: jit-sljit on the boundary - the first second compiler in a mixed store
kind: spec
id: S0016
status: draft
superseded_by:
related: [D23, D30, D35, D39, D47, D56, D59, D60, D61, D64, D66, D67, D68, D70, D71, D73, R2, R3, S0004, S0013, S0014, S0015]
owner: maintainer
updated: 2026-11-24
---

# S0016 jit-sljit on the boundary: the first second compiler in a mixed store

## Summary
ROADMAP "Next" (6), item (a). This spec gives `jit-sljit` the structure the Python JITs have had
since S0015, as the first real second compiler.
* Starting point: `poc/sljit/sljit-be/waboros_sljit.py` implements the D39 backend on sljit through
  ctypes, under our unchanged Python core. It reached spec and CoreMark parity (D68).
* The PoC predates S0014 and S0015, so its call path is not the model.
* What `jit-sljit` gets (maintainer, 2026-11-24): C entries for every function and thunk; D71's
  three record-call paths; compact internal frames; its own bridge and its own `tail_switch`
  (form 2b, a private hop status, return-then-jump); all of it emitted by sljit.

Success is a store mixing `jit-x86_64` (or `jit-aarch64`) modules with `jit-sljit` modules that
runs:
* calls and cross-compiler tail chains in both directions;
* traps, exceptions and interrupts across the boundary;
* the walk and the collector across both compilers' frames;
* artifact files written by `jit-sljit` (its own compiler string and blobs), read back and run.

Placement (maintainer, 2026-11-23):
* sljit is a git submodule; our C lives under `native/`, built with CMake alone, in C99;
* `jit-sljit` is a development and CI target, outside the Python package. "No runtime
  dependencies" holds.

**What it does for the final freeze of v1.** D73's marker and ROADMAP "Next" (6) ask that the
first sljit-based compiler compiles code across the S0015 boundary and that the C loader (D36)
reads and runs the committed files.
* This spec covers the first part with `jit-sljit`: our Python core with sljit as the encoder,
  not the D68 micro-compiler (our core in C). It proves the boundary with a second code generator:
  its C entries, D71's "another compiler → C path" from both sides, cross-compiler hops, and sljit
  code behind `entry` and the walk.
* Item (b), the C loader, remains.
* The marker's "form-2a sibling tail calls and a record with no `tail_switch`" are no longer
  conditions: the "none" value of `tail_switch` leaves v1 ("The none value leaves v1").

The runtime gains a minimal, target-neutral part of phase 6:
* a store holds instances of several JIT targets of one architecture, each loaded under its own
  exact compiler string;
* the walk and the walk check use the store's sets of entry and restore points;
* the GC collector is compiled by the architecture's runtime compiler.

Not changed here:
* the per-architecture loader rule and executing a foreign blob from a file stay in phase 6;
* the context ABI stays v1 as it is;
* the format stays v1, with one value removed under the working freeze (no version bump).

## Goals / Non-goals
* Goals:
  * `jit-sljit` on x86-64 (native) and aarch64 (QEMU with BTI enforced; the native arm64 runner),
    built like `jit-x86_64`/`jit-aarch64`:
    * C entries, D71's three paths, compact internal frames;
    * its own bridge and `tail_switch`;
    * stack maps from one audited place;
    * BTI and CET pads;
    * baseline ISA only;
  * recursion depth equal to the Python JIT of the same architecture, measured in **step 1**;
  * spec parity with that JIT through artifact files, as a behaviour oracle (D68: not bytes), with
    the walk check and poison;
  * mixed-store conformance tests, both directions, both architectures; CoreMark alternated.
* Non-goals:
  * the C reference loader (D36) and the C micro-compiler (D68), ROADMAP "Next" (6) items (b) and
    (c). `native/` gets their place in the layout, not their code;
  * shipping `jit-sljit` or any native code in the Python package;
  * cross-compilation with `jit-sljit` (D35): sljit is built for the host here. Determinism (same
    bytes in two processes, on two CPUs of one ISA) is checked instead;
  * a byte oracle between compilers (D64 item 4, D68).
* Rows: no per-OS code (D47). `jit-sljit` runs on Linux glibc only, and on any other OS its probe
  refuses. x86-64 and aarch64 are ISA targets, not D47 rows: the BTI pads and the baseline CPU
  features are per ISA, and both are required, not optional (BTI: `PROT_BTI` pages fault without
  them; measured, "Measurements").

## Interface

### Where it lives (decided, maintainer 2026-11-23)
```
.gitmodules                    native/third_party/sljit -> zherczeg/sljit, pinned commit
native/                        all our new C sources (the PoC's C stays in poc/)
  CMakeLists.txt               standalone or as a subproject
  third_party/sljit/           the submodule, unmodified
  sljit/waboros_sljit.c        THE translation unit: our sljit defines, #include "sljitLir.c",
                               the baseline CPU features, our functions (frame numbers, the
                               constants table, the code-buffer hooks)
  sljit/waboros_sljit.h        our exported functions
  tests/                       ctest self-tests; tests/consumer/ (the subproject check)
  loader/, micro/              later (D36, D68); not created here
tests/sljit/                   the Python side (development only): backend.py (the D39 backend,
                               ported from the PoC), __init__.py (registers the target when the
                               library exists)
```
* **C build with CMake only.** No Python, no generated sources, no configure-time patching.
  * sljit's constants come from an exported function (`waboros_sljit_constant(name)`), replacing
    the PoC's `consts2.c` → JSON step (which needed a run under QEMU on aarch64).
* **CMake contract** (maintainer):
  * `cmake_minimum_required(VERSION 3.21)`: `PROJECT_IS_TOP_LEVEL` is 3.21, and an embedder's
    `FetchContent_MakeAvailable` with `SOURCE_SUBDIR native` is 3.18. CMake 3.28.3 is installed
    here.
  * Embedding is `FetchContent_Declare` + `FetchContent_MakeAvailable` (`SOURCE_SUBDIR native`;
    FetchContent clones submodules by default), or `add_subdirectory(native)`. We never use
    `FetchContent_Populate`. `find_package`/install config is secondary and not in this spec.
  * It works as a subproject:
    * paths from `CMAKE_CURRENT_SOURCE_DIR`/`_BINARY_DIR` only;
    * no `include_directories`, `add_compile_options` or `CMAKE_C_FLAGS` changes: `target_*`
      properties, `PRIVATE` unless a consumer needs them;
    * options `WABOROS_*`, targets `waboros_*` with `waboros::` aliases;
    * `enable_testing()` (not `include(CTest)`), and tests only when `PROJECT_IS_TOP_LEVEL` or
      `WABOROS_BUILD_TESTS`.
  * **C99** on our targets only: `C_STANDARD 99`, `C_STANDARD_REQUIRED ON`, `C_EXTENSIONS OFF`.
    A later C11 need (atomics for D58) is a question for then.
  * **One translation unit.** sljit's own CMakeLists.txt "is incomplete and is only meant as an
    aid" (global `include_directories`, a test executable), so it is not used, and no separate
    target compiles `sljitLir.c` again.
  * **One library target**, `waboros_sljit` (alias `waboros::sljit`):
    * with `WABOROS_SLJIT_SHARED` (default `PROJECT_IS_TOP_LEVEL`) it is a shared library with
      `POSITION_INDEPENDENT_CODE` for ctypes, exporting sljit's API and ours;
    * otherwise it is static with `SLJIT_CONFIG_STATIC=1`, so sljit's symbols stay internal and
      only `waboros_sljit_*` is visible.
  * The defines:
    * `SLJIT_CONFIG_AUTO=1` and `SLJIT_CONFIG_X86_CET=1` on x86-64;
    * `SLJIT_EXECUTABLE_ALLOCATOR=0` with the PoC's hooks: a writable buffer above 4 GiB, so
      label addresses are rip-relative;
    * `SLJIT_UTIL_STACK=0`;
    * `SLJIT_VERBOSE=0` and `SLJIT_DEBUG=0` (`WABOROS_SLJIT_DEBUG` switches both).
* **Packaging** (maintainer, 2026-11-24): only the **wheel** must carry just the package.
  * The wheel holds `waboros/` and `waboros-<version>.dist-info/` only, as today: setuptools
    packages `waboros` from `src/` (`package-dir`), so nothing under `native/`, `tests/` or `poc/`
    can enter it. No separate check.
  * The sdist may hold everything tracked, `native/` and `poc/` included. It has no `MANIFEST.in`
    (measured with `uv build --sdist`: every tracked file, `poc/` 151 entries), and none is added:
    `poc/` stays in the sdist, so tests that read `poc/**/*.wasm` keep working.
  * The sdist does not carry the submodule's files: setuptools-scm lists tracked files, and a
    submodule is one gitlink entry (expected, not checked). So `native/` cannot be
    built from an sdist, and that is not supported. It is built from a git checkout with
    `--recurse-submodules`, the simplest rule.
* **The Python side.**
  * `tests/sljit/__init__.py` loads the library from `WABOROS_SLJIT_LIB` (default `build/native/`).
  * If it is there, it registers `jit-sljit-<host arch>` in `base.TARGETS` and in the artifact
    reader. `tests/conftest.py` and `tests/spec/run.py` import it, so spec children see the target
    too.
  * Without the library the target is absent and its tests skip. The runtime never names it.
* **Registration rules for a development target** (Target attributes, not names):
  * `complete = False`: `auto` never selects it. `tests/spec/test_runner.py` then expects pyc's
    failures plus `unsupported:` ones only, so it gets `tests/spec/xfail/jit-sljit-<arch>.txt`;
  * `development = True`: `tests/test_features.py::test_every_registered_target_has_a_column`
    compares only non-development targets, and FEATURES.md gets no column, since `jit-sljit` is
    not a package target.
* **CI jobs** (`submodules: true` in `sljit`, `jit` and `arm64`):
  * **`sljit`** (new, ubuntu-latest):
    * the build and ctest;
    * the spec suite on `jit-sljit-x86_64` in-process and through files, with the walk check and
      poison, guard and explicit;
    * `spec_diff` against `jit-x86_64`;
    * the mixed-store tests;
    * the consumer check.
    * Time: `jit-x86_64`'s whole suite takes 20 s wall here (4 vCPU, `tests.spec.run`, measured).
      The walk check, poison and files multiply it, so about 5-8 min for the four runs is expected;
      step 0 measures it.
  * **`jit`** (QEMU, BTI): `apt-get install gcc-aarch64-linux-gnu`, a cross configure with
    `-DCMAKE_SYSTEM_NAME=Linux -DCMAKE_SYSTEM_PROCESSOR=aarch64 -DCMAKE_C_COMPILER=aarch64-linux-gnu-gcc
    "-DCMAKE_CROSSCOMPILING_EMULATOR=qemu-aarch64;-cpu;max;-L;/usr/aarch64-linux-gnu"`, ctest
    under it, then explicit-mode spec and mixed runs under `tools/run_arm64.py`. Expected as long
    as the job's existing `jit-aarch64` QEMU spec run, per mode.
  * **`arm64`** (native, no BTI): the build, and the guard-mode spec and mixed runs.

### Identity
* **Target names** `jit-sljit-x86_64` and `jit-sljit-aarch64`, `version = 1`.
  * The header's compiler target must match `e_machine` (`artifact.py` `_head`), so one name
    cannot serve both machines.
  * Only the host's target is registered. The PoC's `jit-sljit` is retired.
* **Compiler string** `waboros <version> jit-sljit-<arch> 1`.
  * A development build digests the files that make its code. New Target method
    `digest_sources() -> list[Path]`, default empty, folded into `compiler_string`.
  * `jit-sljit` lists `tests/sljit/*.py`, `native/CMakeLists.txt`, `native/sljit/*` and
    `native/third_party/sljit/sljit_src/*`.
* **`HOST_BRIDGE` (word 8)**: the bridge of `jit-sljit`'s own in-process blob copy, one per target
  and process, never another target's. D71's test (equal word 8 = same compiler) holds.
* **`TAIL_SWITCH` (word 9)**: its own `tail_switch`, never 0.
* **The architecture's runtime compiler**: new Target attribute `runtime_compiler`, defaulting to
  the target's own name; `jit-sljit-<arch>` names `jit-<arch>`. `collect.py` compiles the collector
  (D61) with `TARGETS[TARGETS[inst.owner].runtime_compiler]`, not `TARGETS[inst.owner]` (line 40
  today). So a store whose first GC module is `jit-sljit` still runs the runtime's collector, as
  S0015 says.

### The compiler: the structure of `jit-x86_64`, code by sljit
* **Internal convention: the PoC's** `SLJIT_ENTER_REG_ARG | SLJIT_ENTER_KEEP(3)`.
  * S0 is the context, S1 the frame, S2 memory 0's base, all kept from the caller.
  * `SLJIT_R0` holds the status.
  * The pool is S3–S5 (saved by `sljit_emit_enter`) and `SLJIT_R3`–`SLJIT_R6`, which a function
    that pins them saves in its own frame. That makes them callee-saved inside the compiler.
  * On aarch64 the pool choice (S3–S9) is step 5's.
* **Module layout**, as `jit-x86_64`/`jit-aarch64`. Per function and host thunk a fixed **stub**,
  the body right behind it, and one shared **centry** per module after the last body, behind a
  pad:

  | | x86-64 (16 bytes) | aarch64 (12 bytes) |
  |---|---|---|
  | stub | `endbr64; lea rdx, [rip + 5]` (custom bytes) then `sljit_emit_jump` to centry (`jmp rel32`) | `bti jc; adr x2, #8` (custom words) then `b centry` |
  | body | at `code + 16` | at `code + 12`, starting with `bti jc` |

  * The stub's register is the C convention's third argument: centry is a sljit C function
    `SLJIT_ARGS3(32, P, P, P)`, so sljit hands it the body in S2.
  * sljit shortens a jump within ±128 bytes (`detect_near_jump_type`), so a pad of 128 bytes
    (`int3`/zero words) before centry keeps every stub's jump long.
  * The writer's `Target.centry()` reads the jump exactly as `jit-x86_64`'s and `jit-aarch64`'s
    do (the same instruction at the same offset).
  * After generation the backend checks every stub's bytes and refuses the module if one differs.
* **centry**:
  * `enter(0, ARGS3)`, saved S0–S2;
  * moves the body address to a scratch register, loads S2 from the context, and calls the body
    (`SLJIT_CALL_REG_ARG` through the register);
  * the label after that call is a **site with shift 0 and no map** (`c_entry_shared()` returns
    1, as `jit-x86_64`);
  * then the hop test: status equal to `HOP` (`0x80000001`) means the record is in S2, so centry
    sets `SLJIT_R0` = `[S2 + 16]` and `SLJIT_R1` = S1 and emits `SLJIT_CALL | SLJIT_CALL_RETURN`
    with `ARGS2` to `[S2]`: a C sibling call. Otherwise it returns the status.
  * It takes `HOP` only from its own module's bodies (S0015 M5).
  * The frame is 32 bytes on x86-64 and 48 on aarch64 (C function, saveds 3; measured).
* **Record calls: D71's three paths** (`record_sites` = 3, one map):
  * own context (`[rec + 16]` = S0): a `REG_ARG` call to `[rec] + STUB`, as a direct call;
  * same compiler (`[[rec + 16] + 8·HOST_BRIDGE]` = `[S0 + 8·HOST_BRIDGE]`): S0 saved in the
    frame, S0 and S2 the callee's, a `REG_ARG` call to `[rec] + STUB`, then ours back (the PoC's
    `_call_record`);
  * another compiler: a C call to `[rec]` with `SLJIT_R0` = the record's context and `SLJIT_R1` =
    S1 + 8·shift. Around it, `SLJIT_R3`–`SLJIT_R6` are saved and restored on every path before the
    status test (S0015 A3); these are x86-64 `rcx r8 r11 r10`, aarch64 `x3`–`x6`. S0–S5 are
    callee-saved in C.
* **Direct calls** stay `REG_ARG` calls to the body label, as in the PoC.
* **Host calls** go through `jit-sljit`'s bridge, which saves `SLJIT_R3`–`SLJIT_R6` itself (PoC).
  A host thunk's C entry is its stub; behind it the thunk calls the bridge.
* **Tail calls**, as `jit-x86_64` (S0015 forms 1 and 2b; D71):
  * own context: move the arguments down, restore the R pins, `CALL_RETURN` (`REG_ARG`) to
    `[rec] + STUB` or the label;
  * another context, own or other compiler. The body reads its return address
    (`SLJIT_GET_RETURN_ADDRESS`) and compares it with centry's return point (`SLJIT_MOV_ADDR` of
    the label):
    * equal: it was entered through its module's C entry, so it **returns** `HOP` with the record
      in S2 (return-then-jump);
    * otherwise: `CALL_RETURN` to `tail_switch` (context word 9) with the record in `SLJIT_R0`.
* **`tail_switch`** is a sljit `REG_ARG | KEEP(3)` function in the compiler's blob, reached by
  `CALL_RETURN`, so its return address is the tail-caller's.
  * Its prologue makes the restore frame: saved registers plus locals for `SLJIT_R3`–`SLJIT_R6`
    and S0.
  * Its return address equal to its own `restore` label: a later hop. It switches S0 and S2 and
    `CALL_RETURN`s:
    * to `[rec] + STUB` (`REG_ARG`) if word 8 matches;
    * otherwise to the C entry `[rec]` with `SLJIT_R0`/`SLJIT_R1` set.
  * Otherwise, the first hop: it saves the R pins and S0, switches, calls (the same two kinds),
    binds `restore`, restores the R pins and S0, **reloads S2 (memory 0's base) from the restored
    S0**, as `jit-x86_64`'s `tail_switch` reloads its memory registers, and returns the status.
    The later hop's `CALL_RETURN` leaves S2 to the restore point the same way.
  * `restore_ret_at`/`restore_up` are its frame numbers (below).
  * The gate's `pong2` showed the pieces (return-address compare, `CALL_RETURN` after a released
    frame) on x86-64. The whole of it is expected, tested in step 1.
* **Bridge.** As in the PoC, with S0015's duties: exit record `{sp at the call, return address,
  frame}`, `HOST_ERROR` preset to 13 and put back, the word returned.
* **Stack maps.** A label after each call gives `ret`. `ret_at`/`up` come from
  `waboros_sljit_frame(compiler, &ret_at, &up)` in our translation unit:
  * x86-64: `ret_at` = `local_size + GET_SAVED_REGISTERS_SIZE(scratches, saveds − kept, 0)`, `up` =
    `ret_at + 8`;
  * aarch64: `ret_at` = 8, `up` = `local_size` after the prologue.
  * These are sljit's own `SLJIT_GET_RETURN_ADDRESS` expressions, pinned by the submodule commit.
    A ctest checks them against the real return address and stack pointer (shapes below).
  * The PoC's `lib2.c` puts aarch64 `x30` at `size − 8` above 512 bytes. That is **wrong** for
    sljit 39c508d: measured at `[sp + 8]` up to 4096.
  * A public `sljit_get_return_address_offset()` is to be proposed upstream. No patch for S0016.
* **Frame size: D67's pre-pass in the Python core, in step 1** (D67 amended: "with the C core"
  becomes now).
  * The core scans each body once and passes `func_begin` hints: any call (`call_host` included),
    calls through a record, `wide`/v128 scratch, bulk copies, the pins.
  * The Python backends ignore the hints, so their bytes are unchanged (D35 lists).
  * `jit-sljit` sizes `local` = 8 · (R pins + the context save if it calls a record + scratch), 0
    for a simple function.
  * Measured: `REG_ARG|KEEP(3)` with 3 saved registers and no locals is 16 bytes on both
    architectures, `jit-x86_64`'s direct-call cost (D71). The PoC's fixed 9 words gave 96.
* **Headroom (S0015).** `sljit_emit_enter` pushes and allocates before the first check.
  * The worst case before a check is centry's 32/48 bytes plus a body's pushes and `local`, or
    `tail_switch`'s restore frame plus a body.
  * The backend asserts the 768-byte bound and refuses the module (`Unsupported`) above it.
    Expected: no spec module comes near it.
* **Baseline CPU features, no patch.** sljit keeps them in `static cpu_feature_list`, which our
  translation unit can write.
  * The first `sljit_create_compiler` runs `init_compiler()` → `get_cpu_features()` (cpuid)
    unconditionally (`sljitLir.c` 581-584, `sljitNativeX86_common.c` 3237-3239), so a preset
    before it is overwritten. Re-review, measured: preset before create gives `lzcnt`; preset
    after create gives the `bsr` + `cmov` baseline.
  * So `waboros_sljit_create()` wraps `sljit_create_compiler` and assigns `cpu_feature_list =
    CPU_FEATURE_DETECTED | CPU_FEATURE_CMOV` (x86-64-v1) after **every** call. The Python glue
    and later C code create compilers only through it.
  * `CPU_FEATURE_SSE2` exists only under `SLJIT_DETECT_SSE2` (x86-32), so it is not in the value.
  * The result: no LZCNT, TZCNT or AVX; `clz`/`ctz` use `bsr`/`bsf` with `cmov`; the bytes do not
    depend on the host CPU. sljit's AVX paths are reached only with `SLJIT_ENTER_USE_VEX`, which
    we never use.
  * `#error` if `__SHSTK__` (sljit's `rdssp`/`incssp` shadow-stack paths) on x86-64, and if
    `__ARM_FEATURE_ATOMICS` (sljit picks LSE at build time) on aarch64.
  * A ctest checks the emitted bytes of `clz`/`ctz`/`select` (guarding the wrapper), and `tools/isa_all.py` runs on
    `jit-sljit` files.
  * The wasm micro-compiler's further needs (no `cpuid` inline asm, `sljit_sw` independent of the
    host `long`) belong to ROADMAP "Next" (6)(c): to be proposed upstream first (host ≠ target,
    CPU features from the configuration); a fork or build-time patches only if refused.
* **BTI, CET.**
  * x86-64: `SLJIT_CONFIG_X86_CET=1` puts `endbr64` first in every `sljit_emit_enter`; stubs have
    their own.
  * aarch64: sljit emits no BTI, so we add custom words: `bti jc` on stubs (S0015 Q5 (a)), bodies
    (`blr`'d by D71's paths, `br x9`'d by `CALL_RETURN`) and `tail_switch` (`br x9`); `bti c` on the
    bridge (`blr` only). centry gets none: only the stubs' direct `b` reaches it.
  * No `bti j`: `br_table` is a compare tree (PoC `jump_table`), and sljit emits no other indirect
    jump inside a function (expected; the BTI-enforced spec run shows it).
  * No `paciasp`.
* **Machine state (S0015 table).** sljit code:
  * sets no DF and changes no MXCSR or FPCR;
  * uses no x87;
  * emits no VEX (no `SLJIT_ENTER_USE_VEX`; SIMD through our SSE2/NEON bytes, as the PoC), so no
    `vzeroupper` is needed;
  * never touches `x18` (sljit's aarch64 `reg_map` has no 18);
  * writes a frame record with `x29` pointing at it (seen in the dump);
  * requests no `fsaveds`;
  * keeps nothing below the stack pointer (expected; the review checks it).
* **Position independence and determinism**, as in the PoC: no process address in the code,
  rip-relative `SLJIT_MOV_ADDR`; identical bytes from two processes, loaded at two addresses.

### The artifact files of `jit-sljit`
* `Target.conventions()` gives the blob per convention, `tail_switch` and `restore` with their
  numbers (bridge then `tail_switch`), as the Python JITs.
* **x86-64 writes `sysv` and `win64`**, as S0014 says, so D23 holds and no format rule changes.
  * The blobs are bytes; `tail_switch` is identical in both.
  * The `win64` bridge differs only at the host call. sljit's x86-64 build picks one calling
    convention at build time (`_WIN64`), and we use one sljit configuration on every OS
    (LESSONS 141), so that call is custom bytes: `rcx`/`rdx` arguments, an indirect `call`, and a
    sljit label after it for the exit record. About 20 lines.
  * The 32-byte shadow space is `[rsp, rsp + 32)` at the call. So the bridge's own locals (the
    saved R pins and `HOST_ERROR`, at `[SP + 0..40]` in the PoC) move above it, and its frame keeps
    `rsp` 16-aligned at the call.
  * It runs on no OS row here, but it is **executed** on Linux. A ctest calls a host function
    declared `__attribute__((ms_abi))` through the `win64` bridge and checks the arguments, the
    shadow space (the host writes all 32 bytes), the alignment and the exit record. The disassembly
    and ISA checks and the record compare come on top.
  * That is cheaper than a sysv-only file, which would need an S0014 amendment, a refusal rule and
    a refusal-corpus case.
* The symbols are as today (`__waboros_bridge_<conv>`, `__waboros_tail_switch_<conv>`,
  `__waboros_centry_<conv>` found through the stub's jump).
* `features_required`, flags, ISA level: as for the Python JITs, baseline only.

### Runtime and loader (target-neutral)
Each item is data from a `Target` or its `Trampolines`:
1. **Mixed stores in-process.** Instances of different `JitTarget`s already link: both are
   `JitInstance` and records are three words. `_StoreStacks` keeps the set of `Trampolines` of its
   instances, added at instantiation.
2. **The walk's sets** (S0015 walk item 7):
   * entry points: every set member's `entry_ret` (each target seals its own copy of the runtime's
     blob);
   * restore points: `{restore: (restore_ret_at, restore_up)}`.
   * `walk.walk`, `collect.py` and `instance._checked` (the walk check) take the store's set, not
     `inst.tramp`. About 25 lines.
3. **One compiler string per bridge address** (D71's precondition), checked at instantiation.
4. **Loader.**
   * `load(b, target=…)` takes the compiler named in each file. Every file passes the exact-string
     rule and `read`'s compare against its own target's `conventions()`.
   * D70's rule needs no relaxation, and no foreign blob is executed: both compilers are in the
     process.
   * `artifact.TARGETS` (name → `e_machine`, ISA name) comes from Target attributes `e_machine` and
     `isa_name` (about 10 lines).
5. `compiler_string` folds in `digest_sources()`; `runtime_compiler` as above.

ROADMAP phase 6 loses the items done here:
* mixed stores in-process;
* the walk's sets;
* the distinctness check;
* the real-second-compiler test.

It keeps:
* the per-architecture rule;
* the foreign-blob path;
* the trampoline compare at store attach;
* S0015 Q4's flags (here one `WABOROS_JIT_BOUNDS` per process, but the tests also mix modes, which
  Q4 (a) allows).

## Semantics
* **Traps**: status returns through `jit-sljit` frames and C entries, unchanged.
  * Guard-mode fault points are labels on the one faulting instruction, resumed in the same
    artifact.
  * aarch64 keeps the PoC's guard-mode access rules.
* **Exceptions**: status 14 and word 10, passed through; `landing` and `throw_ref` as in the PoC.
* **Interrupts, epoch, stack limits**: polled in prologues and loop heads, as in the PoC.
* **The collector** comes from the runtime's compiler (above), never `jit-sljit`.
* **I1**: no reference in a register at a site; references never pinned (the PoC passed the walk
  check with poison, D68).

## Measurements
Scratch experiments, 2026-11-23/24, sljit 39c508d, not committed. Directory
`/tmp/claude-0/-home-user/08f045a6-cb18-5fbb-bbb0-417b880032a2/scratchpad/s16/`.
* **The PoC does not run on HEAD**: `tests.spec.run --engine jit-sljit core` gives pass 2 027 /
  fail 18 571. Every module fails with `'Backend' object has no attribute 'c_entry'`.
* **The module shape with sljit** (`exp/exp2.c`):
  * three functions as stub + `REG_ARG` body + one shared centry (`ARGS3`) behind a pad;
  * every stub is exactly 16 bytes (x86-64) / 12 (aarch64);
  * the body sits at `code + 16` / `+ 12`;
  * called through each stub as a C function, results right, on x86-64 natively and on aarch64
    under `qemu-aarch64 -cpu max` on `PROT_BTI` pages (stubs and bodies `bti jc`).
* **Frame numbers** (`exp/exp.c`):
  * Setup: 90 shapes per architecture (saveds 2–6, scratches 7, locals 0–4096, C and
    `REG_ARG|KEEP(3)`). The callee reads its return slot and stack pointer; the caller compares.
  * Result: 0 wrong on x86-64 and on aarch64 (QEMU).
  * On aarch64 `ret_at` is 8 at every size.
  * The ctest of step 0 adds `7 | SLJIT_ENTER_FLOAT(3)` scratches and aarch64 saveds up to 10.
* **Frame sizes** (`up`, bytes; sljit rounds frames to 16: re-review, `REG_ARG|KEEP(3)` x86-64
  saveds 4 gives 16, saveds 5 or 6 gives 32, saveds 3 with local 8 gives 16):

| shape | x86-64 | aarch64 |
|---|---|---|
| `REG_ARG|KEEP(3)`, saveds 3, local 0 (a simple body) | 16 | 16 |
| `REG_ARG|KEEP(3)`, saveds 6, local 0 | 32 | 48 |
| C function, saveds 3, local 0 (centry) | 32 | 48 |
| C function, saveds 4 (one pin), local 0 | 48 | 48 |

* **BTI** (QEMU `-cpu max`, `PROT_BTI`):
  * sljit functions with `bti jc` (custom word before `sljit_emit_enter`) tail-call each other 10⁵
    times by `CALL_RETURN` (`ldp x29, x30` … `br x9` in the dump);
  * without the pad: SIGILL.
* **Spec run time**: `uv run python -m tests.spec.run --engine jit-x86_64` takes 19.8 s wall and
  42 s CPU here, 64 229 pass.
* **sdist**: every tracked file today, `poc/` 151 entries, no `MANIFEST.in`.
* **Not measured, expected:**
  * `tail_switch` in sljit as a whole;
  * CoreMark (D68's parity is the baseline: the internal convention is the PoC's);
  * the depth table below;
  * CET/GCS balance on enforcing hardware (none exists in CI).

**Expected depth at 512 KiB** (measured in step 1, recorded beside Wasmtime 50.0.1):

| path | `jit-x86_64` (D71) | `jit-sljit`, expected |
|---|---|---|
| direct recursion, same pins on both | 16 B/level plus its pushed pins | 16 plus the S pins sljit saves, rounded to 16 |
| own-table `call_indirect` | 16 | the same as direct |
| across instances, same compiler | 32 | 16 + the context save in its frame |
| across compilers | 112 a call | its C path: the R saves inside its frame + the callee's centry (32 / 48) |

## Test plan
x86-64 unless marked; "through files" is `WABOROS_JIT_ARTIFACT=1`.
* **Native**: `cmake -S native -B build/native && cmake --build build/native && ctest --test-dir
  build/native`:
  * the frame shapes above;
  * the stub sizes;
  * the baseline bytes of `clz`/`ctz`/`select` (compilers made through `waboros_sljit_create()`);
  * the `win64` bridge executed against an `ms_abi` host function;
  * on aarch64 under the crosscompiling emulator, the BTI ping-pong with its SIGILL control;
  * the consumer project (`add_subdirectory` and `FetchContent_MakeAvailable` with `SOURCE_DIR`
    local), configured with `--warn-uninitialized -Werror=dev`.
* **Without the library**: `pytest -q` passes (the `jit-sljit` tests skip).
* **Spec suite on `jit-sljit`**, in-process and through files, `WABOROS_JIT_WALK_CHECK=1
  WABOROS_JIT_GC_POISON=1`, guard and explicit:
  * 64 229 pass, 0 fail;
  * `tools/spec_diff.py jit-x86_64 jit-sljit-x86_64` empty both ways;
  * aarch64 under `tools/run_arm64.py` (explicit, BTI) and on the arm64 runner (guard), against
    `jit-aarch64`;
  * `gc` and `exceptions` with `WABOROS_JIT_GC_STRESS=1 WABOROS_JIT_GC_VERIFY=1`: 905 pass.
* **Mixed stores**, new `tests/jit/test_mixed.py`. Pairs (`jit-x86_64`, `jit-sljit-x86_64`) and
  (`jit-aarch64`, `jit-sljit-aarch64`), both directions, in-process and through files:
  * calls: imports, `call_indirect` and `call_ref` into the other compiler's table, every value
    type;
  * D71's paths: a spy (or the depth per call) shows each compiler takes the body for its own
    records and the C entry for the other's;
  * tail ping-pong in S0015's three entry shapes:
    * 10⁶ round trips in a 256 KiB thread;
    * the call-instead-of-tail control gives `STACK_OVERFLOW`;
    * the chain covers each compiler's `tail_switch` into the other's C entry, and each centry's
      return-then-jump into the other's;
  * statuses: a trap, `INTERRUPT` set by a thread while the callee loops, a host error, status 14
    caught one instance up in the other compiler and uncaught to the host, through one and two C
    entries. No private status reaches the host (`test_boundary.py`'s spy);
  * walk and GC: host → A → B → A → host import with a reference live in each frame, under the
    walk check; collections started from each compiler's frames and inside a mixed tail chain
    forward every reference;
  * a store whose first GC module is `jit-sljit` runs the collector compiled by `jit-<arch>`;
  * distinct `HOST_BRIDGE` per compiler; a forced equal word is refused at instantiation.
* **Files**:
  * `tests/artifact/` cases for the second compiler string (read, inventory, thin and fat);
  * cross-refusal by compiler string both ways;
  * the `win64` blob's call sequence disassembled;
  * `tools/isa_all.py --target jit-sljit-<arch>`;
  * identical bytes from two processes;
  * `tools/elf_readers.py` on one file.
* **Depth** at 512 KiB until `STACK_OVERFLOW`, beside Wasmtime 50.0.1, in **step 1** (x86-64) and
  step 5 (aarch64): direct, own-table `call_indirect`, across instances, across compilers both
  ways. The acceptance is the `jit-<arch>` figure of D71 for the first three.
* **D67 pre-pass**: D35 lists of both Python JITs equal to HEAD's. Compile time of CoreMark and of
  the corpus (`tools/run_corpus.py` modules), alternated before and after, recorded; the bound is
  the S0011 precedent, a few per cent.
* **CET/GCS: argued and reviewed**, not tested (no enforcing runner, S0015). The argument:
  * calls and returns pair up, and `CALL_RETURN` releases the frame and jumps with the return
    address as found;
  * the hop returns to centry, which jumps;
  * `tail_switch`'s first hop calls and returns.
* **CoreMark** (`tools/run_coremark_jit.py`) alternated with `jit-x86_64`, `taskset -c 2`, several
  rounds, self checks pass. The stubs sit where `jit-x86_64`'s do, but the 128-byte pad before
  centry differs, so the placement is close, not equal (LESSONS 139).
  aarch64 timing on the native runner.

## Plan of steps
Each step stops and reports if it needs more than its size.

| step | goal | agent | acceptance | size (lines) |
|---|---|---|---|---|
| 0 | `native/` with the submodule; `CMakeLists.txt` (one TU, one library target, C99, options, `enable_testing`); `waboros_sljit.c` (defines, buffer hooks, baseline CPU features, `waboros_sljit_frame`, constants); ctest (frames, stubs, baseline bytes, BTI); the consumer; CI build steps, the run time of each measured | implementer | the Native commands and `pytest -q` without the library, x86-64 and under the emulator; `pytest -q` unchanged; `check_docs.py` | C ~250, CMake ~120, CI ~50 |
| 1 | `tests/sljit/` on HEAD's interface, x86-64: stubs, centry, D71's three paths, the hop, `tail_switch` and bridge by sljit, frames from the library; the D67 pre-pass in the core; registration (`complete`, `development`, xfail file); **depth measured** | jit-engineer | the spec suite in-process with walk check and poison, both modes, `spec_diff` empty; D35 lists unchanged; the depth table's x86-64 column at `jit-x86_64`'s figures; compile time recorded | py ~700 changed of ~1 500, core ~80 |
| 2 | Runtime and loader: the store's set, the walk's sets (`walk`, `collect`, `_checked`), the distinctness check, `runtime_compiler`, `artifact.TARGETS` from attributes, `digest_sources()`; the `win64` blob; the none value removed (`read` refuses `0xFFFFFFFF` in a trampoline record, with a test on both architectures) | implementer | `pytest -q` on both architectures; the spec suite through files on the three targets at parity; D35 lists unchanged | src ~80, py ~30, tests ~120 |
| 3 | `tests/jit/test_mixed.py` and the files tests, x86-64 | jit-engineer | the mixed tests in-process and through files, both modes | tests ~380 |
| 4 | Review of 0-3 without the implementers' reasoning: the boundary table against the code, CET balance, I1, headroom, M5, the sets, the collector rule, the baseline bytes | reviewer | findings closed before step 5 | — |
| 5 | aarch64: pads, frames, the pool (S3-S9), guard rules; the mixed tests with `jit-aarch64`; the depth column | jit-engineer | QEMU explicit with BTI, the arm64 runner guard; mixed tests | py ~120 |
| 6 | CoreMark both architectures; BENCHMARKS rows | runner | alternated rounds recorded | — |
| 7 | Docs ("Docs this changes"), the none value's documents | orchestrator | `python3 tools/check_docs.py` | — |
| 8 | Final review | reviewer | findings closed | — |

About 1 100 lines of Python (mostly ported), 250 of C, 120 of CMake, 190 of runtime and loader,
500 of tests.

## Alternatives considered
* **Every function a sljit C function, form 2a only, word 9 = 0** (the first draft).
  * Simpler: no stubs, no hop, no `tail_switch`.
  * But every frame pays the C convention's saved registers: 48 bytes a level with one pin, 32
    with none, against 16 (measured above). That is a third to a half of `jit-x86_64`'s depth.
  * The maintainer rejected that trade for `jit-x86_64` (D71: 112 against 32 bytes). Rejected
    (maintainer, 2026-11-24).
* **Per-function C entries instead of a shared centry.** A body cannot recognise every entry's
  return point: a same-context tail call from body j to body k leaves k with E_j's return point,
  so the chain grows. The shared centry has one return point per module, as `jit-x86_64`.
* **The PoC unchanged plus stubs** (its `tail_switch` in hand-written bytes): `tail_switch` by
  sljit needs no per-ISA bytes. `SLJIT_GET_RETURN_ADDRESS` reads the return address the PoC
  wrote assembly for.
* **A sljit patch for CPU features or `ret_at`**: configure-time patching contradicts the
  CMake-only build, and SLJIT-BACKEND.md says "not forked". The static word and our translation
  unit do it, and upstream proposals come first.
* **A sysv-only x86-64 blob**: an S0014 amendment ("a loader refuses a file with no blob for its
  host convention"), a refusal-corpus case and ARTIFACT.md part 2's trampolines row, for about 20
  lines saved. Rejected for the `win64` blob.
* **Phase 6's whole loader rule now**: not needed while both compilers are in the process, and it
  would open the foreign-blob path before W^X and the instruction cache are specified for it.

## The none value leaves v1 (decided, maintainer 2026-11-24)
With this design no compiler needs a `tail_switch` of "none", so the value is removed from v1 under
the working freeze (D73 marker), with no version bump:
* S0014's trampoline record: every offset lies inside its blob, with no `0xFFFFFFFF` exception;
  `read` refuses `0xFFFFFFFF` (`NO_START`), tested on both architectures (step 2);
* S0015: "word 9 = 0" and form 2a without a `tail_switch` go; a compiler always ships a
  `tail_switch`, and form 2a stays possible only beside one;
* ARTIFACT.md's word-9 row: always a `tail_switch`;
* the final-freeze condition "form-2a sibling tail calls and a record with no `tail_switch`" goes
  (D73 marker, ROADMAP "Next" (6));
* if a compiler ever needs it, it returns as a `features_required` bit (D66).

## Open questions
None left: the maintainer answered the last one (above).

## Docs this changes
In the step that makes each untrue; step 7 for the rest.
* [S0015](S0015-boundary-convention.md):
  * the C-entry paragraph (a sljit compiler's functions are its C entries only in sljit's C
    convention; `jit-sljit` uses stubs and centry);
  * "What sljit-based compilers must add" (frame numbers from our translation unit, the baseline
    CPU word, BTI as measured);
  * Compatibility and its deferred list (items done here);
  * the none value removed (form 2a only beside a `tail_switch`, no word 9 = 0);
  * a Changes line.
* [S0014](S0014-artifact-format.md): the record's offset rule without `0xFFFFFFFF`, `read` refuses it;
  a Changes line.
* [ARTIFACT.md](../ARTIFACT.md):
  * part 2's loader paragraph (a store of several compiler strings, each file under its own);
  * part 3's compiler-private rows gain `jit-sljit`;
  * the word-9 row (always a `tail_switch`).
* [DECISIONS.md](../DECISIONS.md):
  * a new decision recording the design and placement;
  * markers on D67 (the pre-pass now), D70 (in-process mixed stores before phase 6), D61 and D64
    consequence 3 (`runtime_compiler`), D73 (the freeze condition without form 2a and the none record).
* [ROADMAP.md](../ROADMAP.md): "Next" (6) item (a) progress and the remaining condition (b); phase
  6's S0015 list without the items done here.
* [ARCHITECTURE.md](../ARCHITECTURE.md): `native/` (C, CMake only, never in the package);
  development targets; a store of several compilers; the runtime compiler rule.
* [TESTING.md](../TESTING.md):
  * development dependencies (CMake ≥ 3.21, a C99 compiler, `gcc-aarch64-linux-gnu`);
  * the `sljit` job and the new steps of `jit` and `arm64`;
  * `test_mixed.py`, ctest;
  * the development-target registration rules;
  * the CI matrix.
* [TARGETS.md](../TARGETS.md): development targets (not selected by `auto`, not shipped, no
  FEATURES column).
* [SLJIT-BACKEND.md](../SLJIT-BACKEND.md): the boundary paragraph (stubs, centry, D71 paths, its
  `tail_switch`), CPU features, the build under `native/`, "Nothing is built yet".
* `poc/sljit/sljit-be/SLJIT-BACKEND-RESULTS.md`: moved to `tests/sljit/`; `lib2.c`'s aarch64
  branch above 512 is wrong; the PoC runs at its base commit only.
* [BENCHMARKS.md](../BENCHMARKS.md): the depth table, CoreMark, D67's compile time.
* [LESSONS.md](../LESSONS.md):
  * sljit's `REG_ARG` functions are not C entries;
  * sljit shortens jumps within ±128 bytes, so fixed-size stubs need the pad;
  * sljit's aarch64 return address is at `[sp + 8]`;
  * sljit overwrites a `cpu_feature_list` preset at its first `sljit_create_compiler`, so the
    baseline is set after every create.
* [RISKS.md](../RISKS.md) R2: the BTI row for sljit code.
* `.gitmodules`; `pyproject.toml` unchanged (the wheel takes `src/`); no `MANIFEST.in`;
  `CLAUDE.md`'s commands (the native build, the `jit-sljit` spec run).
* FEATURES.md: none (`development` targets have no column; `tests/test_features.py` changes).
* [specs/README.md](README.md): this spec's row.

## Progress
* 2026-11-23: draft.
* 2026-11-24: reworked to the `jit-x86_64` structure (maintainer).
* 2026-11-24: the maintainer's answers: the none value removed, the `win64` blob built, the sdist
  unpruned, the wheel as today (only the package).

## Changes
* 2026-11-23: created (draft), with the maintainer's placement, CMake and C99 decisions.
* 2026-11-24: reworked (maintainer, 2026-11-24, and an independent review):
  * the structure of `jit-x86_64` (stubs, a shared centry, D71's three paths, its own
    `tail_switch` by sljit, hops) replaces "every function a C function, form 2a, word 9 = 0",
    now a rejected alternative;
  * one open question on the "none" value;
  * baseline CPU features without a patch;
  * the collector's runtime compiler;
  * the registration rules of a development target;
  * the `win64` blob;
  * one translation unit and one library target;
  * the CI details;
  * the D67 compile-time measurement;
  * the walk check's set;
  * the freeze wording.
* 2026-11-24: the maintainer's answers: the none value leaves v1 (the open question closed, a step
  and its test added); the `win64` blob confirmed; no `MANIFEST.in`, the wheel as today (no
  separate check), `native/` not buildable from an sdist.
