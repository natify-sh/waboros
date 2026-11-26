---
title: jit-sljit on the boundary - the first second compiler in a mixed store
kind: spec
id: S0016
status: implemented
superseded_by:
related: [D23, D30, D35, D39, D47, D56, D59, D60, D61, D64, D66, D67, D68, D70, D71, D73, D74, R2, R3, S0004, S0013, S0014, S0015]
owner: maintainer
updated: 2026-11-25
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

**What it does for the final freeze of v1.** The condition (maintainer, 2026-11-24; D73 marker,
ROADMAP "Next" (6)) is the micro-compiler from our C sources (D68) built to wasm and natively, the
C reference loader (D36) with the native compiler embedded (x86-64, aarch64) compiling and running
modules, and the same loader built without the compiler running files cross-compiled by the wasm
build and the committed files.
* This spec is a step towards it, not part of it: `jit-sljit` is our Python core with sljit as the
  encoder, not the D68 micro-compiler. It proves the boundary early with a second code generator:
  its C entries, D71's "another compiler → C path" from both sides, cross-compiler hops, and sljit
  code behind `entry` and the walk; and it is the oracle the C core is later compared with.
* The "none" value of `tail_switch` leaves v1 ("The none value leaves v1"), so the condition no
  longer names form 2a or a record without `tail_switch`.

The runtime gains a minimal, target-neutral part of phase 6:
* a store holds instances of several JIT targets of one architecture, each loaded under its own
  exact compiler string;
* the walk and the walk check use the store's sets of entry and restore points;
* the GC collector stays as today: compiled by the target of the instance that first needs it,
  which may be `jit-sljit` (any compiler of the architecture will do).

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
    bytes in two processes) is checked instead, and the baseline CPU word makes the bytes independent
    of the host CPU (the ctest byte check);
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
  * **C99** on our targets only: `C_STANDARD 99`, `C_STANDARD_REQUIRED ON`, `C_EXTENSIONS OFF`,
    with `_DEFAULT_SOURCE` defined on the target (strict C99 hides `mmap`'s `MAP_ANONYMOUS`).
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
  * `complete = False`: `auto` never selects it. `tests/spec/test_runner.py`'s `_jits` puts
    development targets with the complete ones: they must fail exactly what pyc fails (parity), not
    the incomplete JITs' test, which asserts a non-empty xfail file of `unsupported:` entries (line
    406). `tests/spec/xfail/jit-sljit-<arch>.txt` is a copy of `jit-<arch>.txt`;
  * `tests/spec/claims.py` gets `jit-sljit-<arch>` with the claims of `jit-<arch>`;
  * tools that take `--target` (`tools/isa_all.py`, `tools/jit_dump.py`, `tools/run_coremark_jit.py`)
    import `tests.sljit` and list it in their choices (`tools/features.py` takes no `--target`; a
    development target has no FEATURES column); `isa_all.py` picks its
    aarch64 gate by the target's architecture (`e_machine`), not by the name `jit-aarch64`;
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
  * The rule: the digest covers sources, not the built library, so a stale `build/native/` is not
    detected by it; the harness refuses a library older than any `native/` source (mtime), and CI
    always builds fresh. A tagged release checkout writes the release version without a digest, as
    for every target; `jit-sljit` files are never released, so that case only matters locally.
* **`HOST_BRIDGE` (word 8)**: the bridge of `jit-sljit`'s own in-process blob copy, one per target
  and process, never another target's. D71's test (equal word 8 = same compiler) holds.
* **`TAIL_SWITCH` (word 9)**: its own `tail_switch`, never 0.
* **The GC collector (D61), unchanged** (maintainer, 2026-11-24):
  * `collect._collector` compiles it with `TARGETS[inst.owner]` (cached by target and bounds mode)
    for the instance that first needs the **collector**: the first module with a function-subtype
    site, at its instantiation (`c.subtype`, `instance.py` 1067-1070), or the instance whose service
    runs the first collection (`collect.py` 138). It is not the first module that needs the heap.
    So in a mixed store it may be `jit-sljit`'s code;
  * any compiler of the architecture is acceptable, because the boundary is one per architecture
    (S0015);
  * mixed stores are expected to be rare: the input is wasm in almost all cases.

### The compiler: the structure of `jit-x86_64`, code by sljit
* **Internal convention: the PoC's** `SLJIT_ENTER_REG_ARG | SLJIT_ENTER_KEEP(3)`.
  * S0 is the context, S1 the frame, S2 memory 0's base, all kept from the caller.
  * `SLJIT_R0` holds the status.
  * The pool on x86-64 is S3–S5 (saved by `sljit_emit_enter`) and `SLJIT_R3`–`SLJIT_R6`, which a
    function that pins them saves in its own frame. That makes them callee-saved inside the
    compiler.
  * On aarch64 the pool is S3–S9 (x22–x28, step 5), all saved by `sljit_emit_enter`: no R pin, so
    nothing below saves `SLJIT_R3`–`SLJIT_R6` there.
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
  * After generation the backend checks every stub's bytes, and that each body label is at
    stub + 16 / + 12, and refuses the module if one differs.
* **centry**:
  * `enter(0, ARGS3)`, saved S0–S2;
  * moves the body address to a scratch register, loads S2 from the context, and calls the body
    (`SLJIT_CALL_REG_ARG` through the register);
  * the label after that call is a **site with shift 0 and no map** (`c_entry_shared()` returns
    1, as `jit-x86_64`);
  * then the hop test: the status compared in 32 bits (`SLJIT_32`) with `HOP` (`0x80000001`); equal means the record is in S2, so centry
    sets `SLJIT_R0` = `[S2 + 16]` and `SLJIT_R1` = S1 and emits `SLJIT_CALL | SLJIT_CALL_RETURN`
    with `ARGS2` to `[S2]`: a C sibling call. Otherwise it returns the status.
  * It takes `HOP` only from its own module's bodies (S0015 M5).
  * The frame is 32 bytes on x86-64 and 48 on aarch64 (C function, saveds 3; measured).
* **Record calls: D71's three paths** (*superseded in part by "Frame size" below: the same-compiler
  path goes through `xcall` on x86-64 and pushes S0 in the body on aarch64, the other-compiler path
  through `ccall`, which holds the R3-R6 saves; no body frame has those slots*) (`record_sites` = 3, one map):
  * own context (`[rec + 16]` = S0): a `REG_ARG` call to `[rec] + STUB`, as a direct call;
  * same compiler (`[[rec + 16] + 8·HOST_BRIDGE]` = `[S0 + 8·HOST_BRIDGE]`): S0 saved in the
    frame, S0 and S2 the callee's, a `REG_ARG` call to `[rec] + STUB`, then ours back (the PoC's
    `_call_record`);
  * another compiler: a C call to `[rec]` with `SLJIT_R0` = the record's context and `SLJIT_R1` =
    S1 + 8·shift. Around it, on x86-64, `SLJIT_R3`–`SLJIT_R6` are stored to and reloaded from four
    frame slots (`rcx r8 r11 r10`; nothing on aarch64, whose pool has no R pin), and S2 is reloaded from the context, on
    every path before the status test (S0015 A3). S0–S5 are callee-saved in C.
* **Direct calls** stay `REG_ARG` calls to the body label, as in the PoC.
* **Host calls** go through `jit-sljit`'s bridge, which on x86-64 saves `SLJIT_R3`–`SLJIT_R6`
  itself (PoC).
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
  * Its prologue makes the restore frame: saved registers plus locals for S0 and, on x86-64,
    `SLJIT_R3`–`SLJIT_R6`.
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
* **Frame size: `local` = 8 · (R pins), known at `func_begin` from the pins alone; no pre-pass**
  (maintainer, 2026-11-24, step 1's plan; D67 stays "with the C core").
  * Calls through a record go through two module helpers emitted by sljit next to centry, so no
    body frame holds the context save or the C-path saves:
    * **own context** (`[rec+16] == S0`): the body calls `[rec] + 16` directly (`REG_ARG`), a
      direct call's cost;
    * **same compiler** (equal `HOST_BRIDGE` words): the body calls **`xcall`**
      (`REG_ARG|KEEP(3)`, 8 bytes of locals, a 16-byte frame): it saves S0 in its slot, switches
      S0/S2, calls `[rec] + 16`, restores S0, reloads S2 and returns the status;
    * **another compiler**: the body calls **`ccall`** (32 bytes of locals): it stores
      `SLJIT_R3`–`SLJIT_R6` in its frame, makes the C call through the record (`SLJIT_R0` = `[rec+16]`,
      `SLJIT_R1` = S1), reloads `SLJIT_R3`–`SLJIT_R6` and S2, and returns the status, so every frame's R pins are back
      before the caller tests it.
  * The saves are slots in the helpers' frames, never pushes around a call (a push would move the
    stack pointer the site's `ret_at` is counted from). Each helper's own call is a module site
    (shift 0, no map), like centry's; the body still has D71's three sites.
  * **aarch64 has no `xcall`** (maintainer, 2026-11-24, step 5's depth gate): sljit's aarch64
    frame is a 16-byte record plus locals rounded to 16, so an `xcall` with one 8-byte slot is 32
    bytes and a crossing cost 64 against `jit-aarch64`'s 48. The body's same-compiler path pushes
    S0 itself, as `jit-aarch64` does with x28: `stp x19, xzr, [sp, #-16]!` before the call and
    `ldp x19, xzr, [sp], #16` after (custom words around sljit's `blr`), and that site's numbers
    are `(ret_at + 16, up + 16)`. The walk reads the push like any frame size, so "no push around a
    call" is an x86-64 rule; on aarch64 the push is in the site's numbers. Depth across instances is
    then `jit-aarch64`'s.
  * `wide` and `_copy` take S2 (memory 0's base, never an operand) as their extra temporary and
    reload it from the context afterwards, so a body needs no scratch slots either.
  * Why not slots in every calling frame (the formula this replaces): 8 · (pins + 1 context +
    4 C-path) gave 64 bytes a level for own-table recursion and across instances, against the
    16 / 32 of `jit-x86_64` this spec expects (the depth table below).
  * The core and the Python backends are untouched, so the D35 lists are unchanged trivially.
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
    (LESSONS 141), so that call is custom bytes: `rcx`/`rdx` arguments (and `r8d`, the word, since D75) and an
    indirect `call`.
    About 20 lines. The exit record's `pc` is the bridge caller's return address, as in the `sysv`
    bridge, not a label in the bridge.
  * The 32-byte shadow space is `[rsp, rsp + 32)` at the call. So the bridge's own locals (the
    saved R pins and `HOST_ERROR`, at `[SP + 0..40]` in the PoC) move above it, and its frame keeps
    `rsp` 16-aligned at the call.
  * It runs on no OS row here, but it is **executed** on Linux. The bridge is emitted by
    `tests/sljit/backend.py`, and `native/` has no Python, so the test is a pytest
    (`tests/sljit/test_win64_bridge.py`). It calls an `__attribute__((ms_abi))` function exported by
    the library through the `win64` bridge via ctypes, and checks the arguments, the shadow space
    (the host writes all 32 bytes), the alignment and the exit record. The disassembly and ISA
    checks and the record compare come on top.
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
   * `load(b, target=…)` is unchanged: the caller passes each file's own target, as today, and the
     loader does not dispatch on the file's compiler string. Every file then passes the
     exact-string rule and `read`'s compare against that target's `conventions()`.
   * D70's rule needs no relaxation, and no foreign blob is executed: both compilers are in the
     process.
   * `artifact.TARGETS` (name → `e_machine`, ISA name) comes from Target attributes `e_machine` and
     `isa_name` (about 10 lines).
5. `compiler_string` folds in `digest_sources()`.

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
* **The collector** comes from the target of the instance that first needs it (above),
  `jit-sljit` included.
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
* **Not measured, expected** (measured since: `tail_switch` and the depth in Progress, steps 1, 2
  and 5, CoreMark in step 6, all in BENCHMARKS.md; only the last item stays unmeasured):
  * `tail_switch` in sljit as a whole;
  * CoreMark (D68's parity is the baseline: the internal convention is the PoC's);
  * the depth table below;
  * CET/GCS balance on enforcing hardware (none exists in CI).

**Expected depth at 512 KiB** (measured in step 1, recorded beside Wasmtime 50.0.1; the measured depth of both architectures is in Progress, steps 2 and 5, and in BENCHMARKS.md, "`jit-sljit-x86_64` on the boundary" and "`jit-sljit-aarch64` on the boundary"):

| path | `jit-x86_64` (D71) | `jit-sljit`, expected |
|---|---|---|
| direct recursion, same pins on both | 16 B/level plus its pushed pins | 16 plus the S pins sljit saves, rounded to 16 |
| own-table `call_indirect` | 16 | the same as direct (the own-context path calls directly) |
| across instances, same compiler | 32 | 16 + `xcall`'s 16-byte frame |
| across compilers | 112 a call | `ccall`'s frame (R saves) + the callee's centry (32 / 48) |

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
  * GC in a mixed store, the order of the modules controlling who compiles the collector: one case
    where a `jit-sljit` instance first needs it, one where a `jit-x86_64` instance does, each
    covering both triggers (a function-subtype site at instantiation, and the first collection).
    The test asserts which target compiled the collector, and that collections are correct both
    ways under the walk check, poison and `WABOROS_JIT_GC_VERIFY=1`;
  * distinct `HOST_BRIDGE` per compiler; a forced equal word is refused at instantiation.
* **`tests/jit/test_boundary.py` on `jit-sljit` too**: the clobbering C entry (with a trap and a
  status-14 landing pad, A3), the 32-bit status test, the ping-pong and the walk tests, parametrised
  over the native JIT and the host's `jit-sljit`.
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
* **D35 lists** of both Python JITs equal to HEAD's (the core is not changed). Compile time of
  CoreMark on `jit-sljit-x86_64` beside `jit-x86_64`, alternated, recorded.
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
| 0 | `native/` with the submodule; `CMakeLists.txt` (one TU, one library target, C99, options, `enable_testing`); `waboros_sljit.c` (defines, buffer hooks, baseline CPU features, `waboros_sljit_frame`, constants); ctest (frames, stubs, baseline bytes, BTI); the consumer; CI build steps, the run time of each measured | implementer | the step-0 ctest (frame shapes, baseline bytes, BTI ping-pong under the emulator) and the consumer, x86-64 and aarch64; `pytest -q` unchanged without the library; `check_docs.py` | C ~250, CMake ~120, CI ~50 |
| 1 | `tests/sljit/` on HEAD's interface, x86-64: stubs, centry, D71's three paths, the hop, `tail_switch` and bridge by sljit, frames from the library, the `xcall`/`ccall` helpers; the library's accessors (label address, code size, error) and constants; registration (`complete`, `development`, xfail file, `claims.py`, `_jits`, `test_features.py`); **depth measured** | jit-engineer | the spec suite in-process with walk check and poison, both modes, `spec_diff` empty; `test_boundary.py` on `jit-sljit`; the stub checks; D35 lists unchanged; the depth table's x86-64 column at `jit-x86_64`'s figures; compile time recorded | py ~700 changed of ~1 500, C ~15, no core change |
| 2 | Runtime and loader: the store's set, the walk's sets (`walk`, `collect`, `_checked`), the distinctness check, `artifact.TARGETS` from attributes, `digest_sources()`; the tools' `--target` choices and `isa_all.py`'s gate by architecture; the `win64` blob; the none value removed (`read` refuses `0xFFFFFFFF` in a trampoline record, with a test on both architectures) | jit-engineer | `pytest -q` on both architectures, `tests/sljit/test_win64_bridge.py` included; `tools/isa_all.py --target jit-sljit-x86_64`; the spec suite through files on the three targets at parity; D35 lists unchanged | src ~80, py ~30, tests ~120 |
| 3 | `tests/jit/test_mixed.py` and the files tests, x86-64 | jit-engineer | the mixed tests in-process and through files, both modes | tests ~380 |
| 4 | Review of 0-3 without the implementers' reasoning: the boundary table against the code, CET balance, I1, headroom, M5, the sets, the baseline bytes | reviewer | findings closed before step 5 | — |
| 5 | aarch64: pads, frames, the pool (S3-S9), guard rules; the mixed tests with `jit-aarch64`; the depth column | jit-engineer | QEMU explicit with BTI, the arm64 runner guard; mixed tests | py ~120 |
| 6 | CoreMark both architectures (`tools/run_coremark_jit.py --target jit-sljit-<arch>`); BENCHMARKS rows | runner | alternated rounds recorded | — |
| 7 | Docs ("Docs this changes"), the none value's documents | orchestrator | `python3 tools/check_docs.py` | — |
| 8 | Final review | reviewer | findings closed | — |

About 1 100 lines of Python (mostly ported), 250 of C, 120 of CMake, 190 of runtime and loader,
500 of tests.

## Toward the other sljit architectures
This spec stays on x86-64 and aarch64: the plan of steps does not change. This section lists what
`jit-sljit`, and later the C micro-compiler (D68), would need on the other architectures sljit
supports. The order is D48's (maintainer, 2026-11-25): riscv64 first, then the 32-bit PC targets
(i686, armv7), then the 32-bit microcontrollers in v2; LoongArch64 is not a target, and s390x,
ppc64le and MIPS are not a priority (on request only, later). Their columns below are kept as
research, not as a plan.

Each row is marked:
* **known**: written in a document or decision;
* **read**: read in sljit 39c508d's sources for this spec;
* **measured**: by a run;
* **expected**: not checked.

The riscv64 rows were corrected by a research run on 2026-11-24 (scratch probes and 420 frame shapes
under QEMU user-mode, rv64gc lp64d); a corrected statement is marked **measured under QEMU**.

| item | what each architecture needs | riscv64 | the others | status, source |
|---|---|---|---|---|
| boundary row (S0015 table) | C convention (the `ctx`/`frame` argument registers, status register, callee-saved set, never-touched registers), stack alignment, red zone, frame record, CPU-state rules (FP control, vector state) | LP64D: `a0`/`a1`, status in `a0` (32 bits used), callee-saved `s0`–`s11` and `fs0`–`fs11`, `sp` 16-aligned, no red zone; never touch `gp` and `tp` (`x3`, `x4`: absent from sljit's RISC-V `reg_map`); `fcsr` rounding RNE, no flags required; RVV state off | one row each; s390x and big-endian MIPS/PPC also need byte order (below) | known (S0015 table as the model; the psABIs), read (`reg_map`) |
| landing pads | an indirect-branch pad on C entries if the platform enforces one | none at the baseline. Zicfilp's `lpad` and Zicfiss shadow stacks are optional extensions; a row is added when Linux enforces them for our process | none on the others but x86-32 CET (`endbr32`, sljit's CET config) | expected; known (R2 for BTI and CET) |
| the runtime's `entry` and `sp_stub` | a reference listing per architecture in ARTIFACT.md part 3 (S0015's five steps), assembled by `targets/jit/<arch>/entry.py` | about 40 instructions, with `a0`–`a2` arguments and the saved block at a fixed `sp` offset | the same per architecture | expected (S0015 "The runtime's `entry`") |
| C-entry stub and centry | a fixed-size stub (pad, body address in the C convention's third argument, a jump to centry) | **Not fixed-size with sljit's jumps**: sljit chooses `c.j`, `jal` or `auipc` + `jalr` by distance, so a stub ending in a sljit jump varies. Two forms work: a fixed 20-byte stub made of two sljit `MOV_ADDR` (`auipc` + `addi` each) plus a custom `jr`; or 16 bytes with our own patch of sljit. (The earlier "12 bytes, `jal`" was wrong.) | MIPS branches reach 128 KiB: a long-branch form or a function-size limit | measured under QEMU (riscv64); expected (the others); known (SLJIT-BACKEND.md, "Position independence") |
| headroom | the worst case before the first check (S0015: 1 KiB, a 768-byte first frame) | from sljit's RISC-V prologue: saved registers plus `local`, measured with the frame self-test | the same | expected |
| `ret_at` / `up` | the frame expression next to `#include "sljitLir.c"`, checked by the ctest | `ret_at` = `local_size − 8`, `up` = `local_size` (sljit's RISC-V `SLJIT_GET_RETURN_ADDRESS` reads `[sp + local_size − 8]`); `waboros_sljit_frame` needs a riscv branch; 420 shapes measured | about 20 lines per ISA; Thumb-2: the top slot of the push; ppc64 and s390x not checked | measured under QEMU (riscv64, 420 shapes); known (SLJIT-BACKEND.md "Frames (D56)") |
| `e_machine` | the ELF machine in the header, and the reader's target table | `EM_RISCV` (243) | LoongArch 258, s390x 22, ppc64 21, MIPS 8, ARM 40, i386 3 | known (ELF) |
| container | v1 is ELF64 little-endian only (D73) | fits | s390x and big-endian MIPS/PPC need a big-endian amendment; 32-bit targets need words and pointers of 32 bits in the context and the records (part 3 has 64-bit words), so an amendment by a `features_required` bit (D66) | known (D73, D66) |
| ISA level | the baseline the portable file uses, and the `isa_features` bits above it | the baseline `rv64gc` (IMAFDC). Zba/Zbb/V only at a declared level. sljit picks the riscv features at build time from `__riscv_*` macros, **Zicond included**, so our translation unit pins `SLJIT_CONFIG_RISCV_*` (VECTOR, BITMANIP_A and BITMANIP_B, ICOND off) and has `#error` unless the build is rv64gc lp64d, as the x86-64 CPU word is fixed | LoongArch and s390x query `getauxval` and `cpucfg`, MIPS `cfc1`, PPC64 inline assembly: host = target, so a configuration word replaces each | read (`sljitNativeRISCV_common.c` 27-112; `sljitNativeLOONGARCH_64.c` 399-401, `sljitNativeS390X.c` 332, `sljitNativeMIPS_common.c` 436); known (SLJIT-BACKEND.md "Host equals target"); the riscv64 build-time pick measured under QEMU |
| trampoline record | a convention id per host convention in `.waboros.trampolines` (1 `sysv`, 2 `win64`, 3 `aapcs64` today) | `lp64d`, a new id: an S0014 amendment under the working freeze | one id per psABI | known (S0014) |
| sljit: helper calls | no absolute address in the code (D59); helpers through context words | none needed by the scalar core on riscv64 | ARM v6/v7 without hardware divide calls `__aeabi_*divmod` at an absolute address; MIPS32 makes every `SLJIT_CALL` absolute: helpers through context words, or never emitting `SLJIT_DIV` without IDIV | known (SLJIT-BACKEND.md "Five targets do not build", "Position independence") |
| sljit: 32-bit operations | D39's zero-extended i32 (LESSONS 5) | sljit's RV64 code keeps an i32 sign-extended: `MOV_U32` after every 32-bit result and `MOV_S32` of the operands of compares and div/rem (13 of 25 probe cases were wrong without them); about 20 lines of the backend keep D39's zero-extended i32 as the v1 rule (maintainer, 2026-11-24: confirmed, D73 marker) | the same check per ISA | measured under QEMU (riscv64); known (SLJIT-BACKEND-RESULTS, "What carries over") |
| sljit: i64 on 32-bit targets | register pairs, a D39 amendment | — | ARM32, x86-32, RISC-V32, MIPS32, PPC32: about 1.5 kloc (Walrus's size), plus libcalls for div/rem and conversions | known (SLJIT-BACKEND.md "What it would cost") |
| per-ISA custom words (D68's list) | SIMD lowering, `sqrt`, the `tail_switch` pieces sljit cannot express, the frame helper, fault-point guards | `fsqrt.d`/`fsqrt.s` as one custom word. `MOV_ADDR` is pc-relative (`auipc`), so code is copy-safe with no page rule; the 4 GiB refusal is x86-only. No SIMD at the baseline (RVV is above it): v128 modules are `Unsupported` and run on `pyc` until a scalar v128 lowering exists. `tail_switch` is sljit code (this spec), so nothing per ISA | SIMD by custom words only where we write a lowering (Walrus: 1.5-2.5 kloc per ISA); atomics 8/16 by masking on RISC-V, MIPS, PPC and s390x (about 300 lines) | known (D68; SLJIT-BACKEND.md "SIMD", "Atomics"); expected (riscv64 SIMD) |
| bounds mode | guard pages need a per-architecture and per-OS fault handler (S0006) | explicit checks only (D29 for new architectures); no handler | the same; guard mode later by its own S0006 row | known (SLJIT-BACKEND.md "Traps and bounds", D29) |
| walk and stack maps | site numbers from the frame expression; I1; the walk check | as on aarch64: no new walk code | the same | expected |
| CI rows | an emulator job (D47: not a per-OS row) | QEMU user-mode riscv64 exists for `pyc` (the `exotic` job of `platforms.yml`); it gains the native build (`gcc-riscv64-linux-gnu`, the crosscompiling emulator), ctest and the spec suite in explicit mode; no native runner | the `exotic` jobs exist for ppc64le and s390x | known (TESTING.md CI matrix) |
| Wasmtime parity | the oracle on that host | Cranelift supports riscv64 | s390x Cranelift; LoongArch, ppc64le, MIPS and the 32-bit hosts get Pulley, Wasmtime's interpreter (still an oracle, slow) | known (D48 evidence) |

**riscv64 as the next architecture** (the research of 2026-11-24, measured under QEMU where a row says so; the code size is an estimate):
* **Docs and contracts:** an S0015 row, an ARTIFACT.md part 3 listing of `entry`, an S0014
  convention id (`lp64d`) and `EM_RISCV` (243) in the reader. About 150 lines of documents.
  Nothing in it changes the v1 contract: all of it is additive (`e_machine` 243, the convention
  id, `isa_level` numbering).
* **Runtime (the larger piece):** `targets/jit/riscv64/entry.py` with `entry` and `sp_stub`, the
  riscv64 encoder, a runtime-only base target (a target that carries no compiler), and the
  executable-memory and I-cache flush (phase 8 needs the last for the hand-written JIT anyway).
* **The `jit-sljit` backend:** the frame expression (`waboros_sljit_frame` needs a riscv branch), the
  stub (20 bytes from two `MOV_ADDR` and a custom `jr`, or 16 with our own patch), `fsqrt`, the
  `MOV_U32`/`MOV_S32` rule and the pinned `SLJIT_CONFIG_RISCV_*` defines.
* **CI:** one QEMU job extended.
* **Not needed:** no SIMD (`Unsupported`), no guard pages, no landing pads at the baseline.
* **Total:** about 500-550 lines of code and 150 of documents (above the 300 expected before the
  research). Nothing changes on x86-64 or aarch64.
* **Order:** riscv64 can go in parallel with or after the C-loader work, and it is not a prerequisite
  of the final freeze of v1 (ROADMAP "Next" (6)).
* **Open questions for its spec:** the sign of the C-entry status on riscv64 (a 32-bit result
  leaves `a0` sign-extended), `lpad` (Zicfilp) in C entries, the stub form, and where the
  runtime-only base target lives.
* **Wasmtime as the oracle:** under QEMU.

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
    CPU word, BTI as measured), and walk item 2's "one of D60's sljit patches or an upstream API"
    (our translation unit, an upstream proposal);
  * Compatibility and its deferred list (items done here);
  * the walk section's "runtime modules are compiled by the runtime's own compiler" becomes "by
    any compiler of the architecture (by default the target of the instance that first needs the
    collector)";
  * the none value removed (form 2a only beside a `tail_switch`, no word 9 = 0);
  * a Changes line.
* [S0014](S0014-artifact-format.md): the record's offset rule without `0xFFFFFFFF`, `read` refuses it;
  a Changes line.
* [S0002](S0002-target-interface.md): the Target attributes and method added (`development`,
  `e_machine`, `isa_name`, `digest_sources()`); a Changes line.
* [ARTIFACT.md](../ARTIFACT.md):
  * part 2's loader paragraph (a store of several compiler strings, each file under its own);
  * part 3's compiler-private rows gain `jit-sljit`;
  * the word-9 row (always a `tail_switch`).
* [DECISIONS.md](../DECISIONS.md):
  * a new decision recording the design and placement;
  * markers on D70 (in-process mixed stores before phase 6), D73 (the
    freeze condition without form 2a and the none record);
  * the D61 and D64 consequence 3 markers: "the runtime's own compiler" becomes "any compiler of
    the architecture (by default the target of the instance that first needs the collector)".
* [ROADMAP.md](../ROADMAP.md) phase 8 (more architectures): a pointer line to "Toward the other
  sljit architectures" (riscv64 first, its size).
* [ROADMAP.md](../ROADMAP.md): "Next" (6) item (a) progress and the remaining condition (b); phase
  6's S0015 list without the items done here.
* [ARCHITECTURE.md](../ARCHITECTURE.md): `native/` (C, CMake only, never in the package);
  development targets; a store of several compilers; the collector by any compiler of the
  architecture.
* [TESTING.md](../TESTING.md):
  * development dependencies (CMake ≥ 3.21, a C99 compiler, `gcc-aarch64-linux-gnu`);
  * the `sljit` job and the new steps of `jit` and `arm64`;
  * `test_mixed.py`, ctest;
  * the development-target registration rules;
  * the CI matrix.
* [TARGETS.md](../TARGETS.md): development targets (not selected by `auto`, not shipped, no
  FEATURES column).
* [SLJIT-BACKEND.md](../SLJIT-BACKEND.md): the table rows at lines 70-71 (trampolines; "the C entry
  … the function itself"), the boundary paragraph (stubs, centry, D71 paths, its `tail_switch`),
  CPU features, the build under `native/`, "Nothing is built yet".
* `poc/sljit/sljit-be/SLJIT-BACKEND-RESULTS.md`: a note that the backend moved to `tests/sljit/`,
  that `lib2.c`'s aarch64 branch above 512 is wrong, and that the PoC runs at its base commit only.
  This is a deliberate exception to CLAUDE.md's "keep `poc/` runnable for comparisons": the
  PoC's backend predates S0014/S0015 and cannot run on HEAD (measured), and `jit-sljit` replaces
  it as the comparison point.
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
* 2026-11-24: step 0 done: `native/` (submodule at 39c508d, `CMakeLists.txt`, `sljit/waboros_sljit.{c,h}`,
  `tests/test_native.c`, `tests/consumer/`), ctest on x86-64 and on aarch64 under QEMU with BTI, CI
  build steps in `jit`, `arm64` and the new `sljit` job. Deviations: `waboros_sljit_constant(name,
  &value)` returns 0 or -1 (a name alone has no way to say "unknown"); DEBUG and VERBOSE are PUBLIC
  definitions (they change the layout of `struct sljit_compiler`); the tests need the shared build.
* 2026-11-24: step 1 done (x86-64): `tests/sljit/` (`__init__.py`: the library, the mtime refusal,
  `jit-sljit-x86_64` registered on Linux glibc x86-64; `backend.py`: the PoC's operators with
  stubs, centry, `xcall`/`ccall`, the hop, the bridge and `tail_switch` by sljit, frames from the
  library; `finish` checks every stub); `native/` exports the label, constant, code-size and
  error getters and `SLJIT_GET_RETURN_ADDRESS`/`SLJIT_MOV_ADDR` (LESSONS 146), with a ctest line;
  the registration points; `test_boundary.py` over both compilers (the walk test counts
  `jit-sljit`'s helper frames). No core change; the D35 lists of `jit-x86_64` and `jit-aarch64`
  are byte-identical to HEAD's. Numbers (this machine, the same session):
  * the spec suite, walk check and poison, guard and explicit (separate processes and
    `--in-process`): 64 229 pass, 0 fail; `spec_diff` empty both ways; `gc` + `exceptions` with
    stress and verify: 905 pass;
  * depth until `STACK_OVERFLOW`, levels at 128 KiB / 512 KiB (bytes a level), driver
    `depth.py` in the step's scratch (`DEPTH`, its direct twin, `XA` + `XB` of `test_boundary.py`):

    | recursion | `jit-x86_64` | `jit-sljit-x86_64` | Wasmtime 50.0.1 (512 KiB) |
    |---|---|---|---|
    | direct `call` | 8 189 / 32 765 (16) | 8 190 / 32 766 (16) | 32 762 |
    | `call_indirect`, own table | 8 189 / 32 765 (16) | 8 190 / 32 766 (16) | 16 381 |
    | across instances (calls) | 4 096 / 16 384 (32) | 4 096 / 16 384 (32) | 21 842 |
    | across compilers, either way (calls) | 1 260 / 5 042 (104) | (the same pairs) | |

    The last row is one measurement of mixed pairs, not one per column: a call from `jit-x86_64`
    into `jit-sljit-x86_64` (112 bytes) and back (96 bytes) average 104 bytes a call.

  * CoreMark compile (`TARGETS[t]().compile`, 7 rounds alternated, `taskset -c 2`, median): guard
    41 ms `jit-x86_64` / 42 ms `jit-sljit-x86_64` (code 34 879 / 35 617 bytes); explicit 68 / 63 ms
    (43 876 / 47 094 bytes). CoreMark passes its self-check on `jit-sljit-x86_64` (speed: step 6).
* 2026-11-24: step 2 done (x86-64): the runtime and loader. `_StoreStacks` keeps the store's
  trampolines (`attach`: entry points → the saved block, restore points → their numbers, bridge
  address → target); `walk.walk`, `walk.check`, `_checked` and the collector's gathering read those
  sets instead of one instance's `tramp`. Instantiation refuses two targets sharing one bridge
  address (`RuntimeError`); in-process the target name stands for the compiler string (one
  version per target in a process). `artifact.targets()` comes from `JitTarget.e_machine` and
  `isa_name`; `digest_sources()` is folded into a development build's compiler string (one
  16-digit digest, paths relative to the files' common directory); a file whose compiler target is
  not registered is refused saying so. `jit-sljit-x86_64` writes `sysv` and `win64` blobs: the
  `win64` bridge's host call is custom bytes (`mov rcx, rbx; mov rdx, rsi; call r13`; since D75 also `mov r8d, eax` before the
  call) and its
  locals sit above the shadow space (`tests/sljit/test_win64_bridge.py` executes it against an
  `ms_abi` function of the library, patching `HOST_BRIDGE` and the import word of a normal
  instance). The "none" value left `read` (refused by name, x86-64 and aarch64). The tools take
  `--target jit-sljit-<arch>`; `isa_all.py` picks the aarch64 gate by `e_machine`; `isa_check.py`'s
  allowlist gained `cmpeqss cmpeqsd unpcklps unpcklpd` and objdump's `rex` (baseline SSE/SSE2 that
  sljit emits). Numbers (this machine): through files `jit-x86_64` and `jit-sljit-x86_64` 64 229
  pass, 0 fail each, `spec_diff` empty both ways; `jit-sljit-x86_64` through files with the walk
  check and poison, guard and explicit, 64 229 each; `jit-aarch64` through files under QEMU 64 229;
  `isa_all.py` 954 blobs clean on `jit-sljit-x86_64` (and on `jit-x86_64`; 931 on `jit-aarch64`);
  D35 lists of both Python JITs byte-identical to HEAD's in both modes; `artifact_dump.py` lists
  identical to HEAD's with the version and the package digest pinned.
* 2026-11-24: step 3 done (x86-64): `tests/jit/test_mixed.py` (pairs from `TARGETS`, both
  directions; D71's paths read from the walk, frames labelled by return address against centry;
  the collector's compiler asserted for both triggers; INTERRUPT from a thread bounded by a counted
  loop; the forced equal `HOST_BRIDGE` stays `tests/sljit/test_store.py`'s), `tests/artifact/
  test_sljit.py` (cross-refusal both ways, fat regenerated, the same bytes from two processes),
  `test_roundtrip.py` over the development targets; the `win64` call sequence stays
  `test_win64_bridge.py`'s (bytes and execution, no disassembly). No core change. Numbers (this
  machine): `test_mixed.py` 42 pass in about 1 s (both bounds modes), in-process and through files,
  with and without the walk check, poison and verify; with `tests/artifact` 207 pass in about 70 s
  serially; `tools/elf_readers.py` clean on a `jit-sljit-x86_64` CoreMark file. Controls: a
  same-compiler record sent to ccall fails the D71 test; ccall's site with a wrong frame size
  fails the walk test; the collector always compiled by `jit-x86_64` fails its two sljit-first cases.
* 2026-11-24: step 4's review findings closed (tests and one guard; the generated code unchanged):
  memory 0's base after xcall, ccall and tail_switch's restore (`test_boundary.py` and
  `test_mixed.py`: another instance's memory, a call and a first hop, and an i64 memory moved by
  the callee; each reload removed fails one test, ccall's and the cross-compiler restore's as a
  segfault, LESSONS 151); `Emitter.generate` refuses code generated below 4 GiB (sljit's
  `SLJIT_MOV_ADDR` is then absolute; tested by reporting the buffer at a low address); CI runs
  `tests/sljit`; the native comments (the buffer above 4 GiB checked, not guaranteed; the CPU
  features detected at the first create) and `test_native.c`'s baseline line.
* 2026-11-24: step 5 done (aarch64): `jit-sljit-aarch64` in `tests/sljit/` (a shared `_Sljit`
  mixin over `JitX86_64Target`/`JitAarch64Target`; the library found by its ELF machine, so the
  cross build `build/native-a64` serves `tools/run_arm64.py`). The backend's aarch64 rows:
  * the pool is S3-S9 (x22-x28), all saved by sljit's enter: no R pin, so bodies have no locals
    and ccall, the bridge and tail_switch's restore frame save no R3-R6 (the compiler section says
    so);
  * stubs `bti jc; adr x2, #8; b centry` (12 bytes, checked in `finish`); `bti jc` on bodies,
    host-thunk bodies and tail_switch, `bti c` on the bridge; x18 never used;
  * no `xcall` ("Frame size", the amendment): the body pushes S0 around the call, site
    `(ret_at + 16, up + 16)`;
  * the PoC's guard rules (a constant address through the index register, stored immediates
    through R2), NEON through a private copy of `aarch64/simd.py`, `fsqrt` as one word;
  * `native/`: a page-aligned code buffer on both architectures (sljit's aarch64 `MOV_ADDR` is
    `adrp` beyond +-1 MiB, LESSONS 152; refused off a page) and the constants `SLJIT_S6`-`S9`,
    `SLJIT_LOCALS_OFFSET` (16 on aarch64: the bridge's caller sp).
  Numbers (QEMU 8.2.2 user mode, BTI enforced, this machine):
  * the suite in-process, explicit, with the walk check and poison: 64 229 pass, 0 fail (68 s);
    without the checks 64 229; through files with the checks 64 229; guard with the checks
    64 229, through files 64 229 (68-102 s a run); `spec_diff` against `jit-aarch64` (64 229 in
    each mode) empty both ways for each;
  * `test_mixed.py`, `test_boundary.py`, `tests/sljit`, `tests/artifact/test_sljit.py` in both
    bounds modes with the walk check, poison and verify: 162 pass, 2 skip (x86-64-only);
    `test_mixed.py` through files: 58 pass; a new `test_boundary.py` test asserts the exit
    record's sp 16-byte aligned behind a cross-instance call (on every compiler);
  * `tools/isa_all.py --target jit-sljit-aarch64` through `run_arm64.py`: 953 blobs, 0 failed
    (ARMv8.0-A). `isa_all.py` now starts its children through qemu-aarch64 where binfmt is
    missing, and `isa_check.py` with the host's `python3`;
  * depth (BENCHMARKS): 32 / 32 / 48 bytes direct, own table, across instances, as `jit-aarch64`
    (64 across instances with an `xcall`, before the amendment); 120 across compilers.
  x86-64 unchanged: the `tools/jit_dump.py` lists of `jit-sljit-x86_64`, `jit-x86_64` and
  `jit-aarch64` byte-identical to HEAD's in both modes (so the page-aligned buffer changes no
  byte), the `sysv`/`win64` blobs identical; `jit-sljit-x86_64` in-process with the checks 64 229 in
  both modes, `spec_diff` empty; `pytest -n 4` 1 469 pass. Controls (each reverted): the body's
  `bti jc` removed: SIGILL in the QEMU suite; the push's site numbers without +16: the walk tests
  of `test_boundary.py` (walk check, GC stress, poison, verify) and `test_mixed.py`'s D71 test
  fail (its walk-and-collector test pairs two compilers, so it never takes this path); the `ldp`
  dropped: a segfault in `test_mixed.py`; restoring x9 instead of x19: three `test_boundary.py`
  tests fail (memory 0's base 34, not 17; the pins; a status); an 8-byte push: only the new alignment test fails (QEMU does not check sp,
  LESSONS 154); the reload of S2 after the pushed call removed: the memory-base test fails; a
  buffer off a page: refused (a test). A module with a tail site 1.1 MiB before centry (centry's
  return point as `adrp` + `add`) takes the hop: the host sees the same stack pointer as for a
  direct call; generated 16 bytes off a page with the refusal bypassed, the test fails (32
  bytes apart: tail_switch's restore frame). Wasmtime 50.0.1 under the same QEMU: 32 756 /
  16 378 / 21 838 levels at 512 KiB (BENCHMARKS).
* 2026-11-24: step 6 (x86-64): CoreMark on the native library, 5 rounds alternated, `taskset -c 2`,
  guard: `jit-sljit-x86_64` 15 968 vs `jit-x86_64` 16 236 Iterations/Sec (medians), ratio 0.9835;
  explicit: 13 699 vs 13 986, ratio 0.9795; every run passes its self-check (BENCHMARKS, S0016
  step 6). aarch64 (native arm64 runner, guard, 3 rounds alternated): `jit-sljit-aarch64` 18 497 vs
  `jit-aarch64` 19 561 (medians), ratio 0.946, spread under 0.5 % (BENCHMARKS).
* 2026-11-24: step 7 (docs): S0015 (the C-entry paragraph, walk item 2, the collector paragraph,
  "What sljit-based compilers must add", the deferred lists and "Compatibility" marked as superseded
  or done in part, a Changes line); D61 and D64 consequence 3 (any compiler of the architecture);
  D73 (the zero-extension confirmation) and the new D74; ARTIFACT.md (part 3's zero-extension rule,
  the `jit-sljit-aarch64` note); SLJIT-BACKEND.md (the table row, the boundary paragraph, the PoC
  note); the PoC's SLJIT-BACKEND-RESULTS.md (a note); ROADMAP (the "Next" (6) progress and the
  riscv64 note, phase 6's list, a phase 8 pointer); ARCHITECTURE.md, TARGETS.md, RISKS.md R2;
  this spec's riscv64 rows (corrected by the research), `related` gains D74. The others of "Docs
  this changes" were done in steps 1-6 (S0014, S0002, ARTIFACT.md parts 2-3, D70, TESTING.md,
  BENCHMARKS.md, `.gitmodules`); the four LESSONS entries (155-158) came in step 8's review.
  `CLAUDE.md`'s commands (the native build, the `jit-sljit` spec run) were added in the commit
  after this step, which is outside `docs/`. The spec's status stays `approved` until step 8.
* 2026-11-24: step 8: final review, findings closed: LESSONS 155-158, the amendment markers (S0013,
  S0014, D68's "left" list, ROADMAP, this spec's record-calls bullet), ARTIFACT.md's
  `jit-sljit-aarch64` column, the headroom check of `func_begin` (112 bytes, a foreign compiler's C
  path before a sljit centry), two CI steps (`gc` and `exceptions` under stress on
  `jit-sljit-x86_64`; `test_mixed.py` through files under QEMU), TESTING.md's job descriptions
  and CI matrix. Status `implemented`.

## Changes
* 2026-11-23: created (draft), with the maintainer's placement, CMake and C99 decisions.
* 2026-11-24: reworked (maintainer, 2026-11-24, and an independent review):
  * the structure of `jit-x86_64` (stubs, a shared centry, D71's three paths, its own
    `tail_switch` by sljit, hops) replaces "every function a C function, form 2a, word 9 = 0",
    now a rejected alternative;
  * one open question on the "none" value;
  * baseline CPU features without a patch;
  * the registration rules of a development target;
  * the `win64` blob;
  * one translation unit and one library target;
  * the CI details;
  * the D67 compile-time measurement (removed later: no pre-pass);
  * the walk check's set;
  * the freeze wording.
* 2026-11-24: the maintainer's answers: the none value leaves v1 (the open question closed, a step
  and its test added); the `win64` blob confirmed; no `MANIFEST.in`, the wheel as today (no
  separate check), `native/` not buildable from an sdist.
* 2026-11-24: the collector rule (maintainer): no `runtime_compiler`; the collector stays the
  first GC module's target's, any compiler of the architecture; a mixed-store GC test with either
  compiler first; the D61, D64 and S0015 markers reworded.
* 2026-11-24: the final review's findings: the C-path save slots in the frame formula; the real
  collector rule (the instance that first needs the collector) and its test; the registration
  plumbing (`_jits`, `claims.py`, the tools); the `win64` bridge as an executed pytest, step 2 to
  the jit-engineer; `test_boundary.py` on `jit-sljit`; S0002, the D39 amendment, SLJIT-BACKEND.md
  rows and S0015 walk item 2 in the docs list; `load()` unchanged; nits.
* 2026-11-24: approved by the maintainer.
* 2026-11-24: "Toward the other sljit architectures" added (maintainer): the per-architecture
  gaps beyond x86-64 and aarch64, riscv64 first; the plan of steps is unchanged.
* 2026-11-24: frames without a pre-pass (maintainer, from step 1's plan): calls through a record go
  through two module helpers, `xcall` (same compiler) and `ccall` (another compiler), so a body's
  `local` is 8 · (R pins); the formula with the context and C-path slots in every calling frame is
  dropped (it gave 64 bytes a level against the depth table's 16 / 32); D67 stays "with the C
  core", the D39 amendment and the D67 marker leave the docs list; step 1 adds the library's
  accessors and constants.
* 2026-11-24: aarch64 without `xcall` (maintainer, step 5's depth gate): the same-compiler path
  pushes S0 around the call in the body (16 bytes, in the site's numbers), for parity with
  `jit-aarch64` (48 bytes a crossing, not 64).
* 2026-11-24 (step 7): "Toward the other sljit architectures": the riscv64 rows corrected by the
  research under QEMU (a stub cannot be fixed-size with sljit jumps, the build-time feature pick
  including Zicond, `MOV_U32`/`MOV_S32`, `MOV_ADDR` pc-relative, the riscv branch of
  `waboros_sljit_frame`) and the estimate of about 500-550 lines of code plus 150 of documents;
  the maintainer confirms D39's zero-extended i32 as the v1 rule (D73 marker).
* 2026-11-24 (step 8, the final review): the findings closed as above; `func_begin`'s headroom check
  counts 112 bytes before a sljit centry (the most a foreign compiler's C path pushes, 64, plus
  centry's 48); the "Record calls" bullet is marked superseded by "Frame size"; the `approved` status
  becomes `implemented`.
* 2026-11-25 (S0017 P1, D75): the win64 host call's custom bytes pass the word in `r8d`; the sysv
  and aapcs64 bridges pass it in `edx`/`w2` (`ARGS3V`). Markers at the two descriptions.
* 2026-11-25 (D48 amendment): "Toward the other sljit architectures" states D48's order (riscv64,
  then i686 and armv7, then MCUs in v2); LoongArch64 is not a target, s390x, ppc64le and MIPS are
  not a priority.
