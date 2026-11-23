---
title: jit-sljit on the boundary - the first second compiler in a mixed store
kind: spec
id: S0016
status: draft
superseded_by:
related: [D23, D30, D35, D39, D47, D56, D59, D60, D64, D67, D68, D70, D71, D73, R2, R3, S0004, S0013, S0014, S0015]
owner: maintainer
updated: 2026-11-23
---

# S0016 jit-sljit on the boundary: the first second compiler in a mixed store

## Summary
ROADMAP "Next" (6), item (a). The sljit backend of the PoC
(`poc/sljit/sljit-be/waboros_sljit.py`: the D39 interface on sljit through ctypes, under our
unchanged Python core; spec and CoreMark parity on x86-64 and on aarch64 under QEMU, D68) moves
onto the S0015 boundary and becomes the first real second compiler. Success is a store that mixes
`jit-x86_64` (or `jit-aarch64`) modules with `jit-sljit` modules and runs:
* calls and cross-compiler tail-call chains in both directions;
* traps, exceptions and interrupts across the boundary;
* the walk and the collector across frames of both compilers;
* artifact files written by `jit-sljit`, with its own compiler string and its own trampolines,
  read back and run.

This is what the final freeze of v1 waits for (D73 marker, ROADMAP "Next" (6)). It exercises four
things no compiler uses today:
* the C entries of a second compiler;
* form-2a sibling tail calls (`SLJIT_CALL_RETURN`);
* a compiler with no `tail_switch` (context word 9 = 0, `0xFFFFFFFF` in `.waboros.trampolines`);
* D71's "another compiler: the C path" on both Python JITs.

Where it lives (maintainer, 2026-11-23):
* sljit is a git submodule at a pinned commit;
* our C sources live under `native/`, built by CMake alone (no Python in the build), in C99;
* `jit-sljit` is a development and CI target: its Python glue is outside `src/`, and nothing
  native enters the Python package (sdist or wheel). "No runtime dependencies" holds.

The runtime gains a minimal, target-neutral part of phase 6:
* a store may hold instances of several JIT targets of one architecture, each loaded under its
  own exact compiler string;
* the walk uses the sets of entry and restore points of the store's targets;
* the "none" path of `tail_switch` becomes real.

The per-architecture loader rule (a file of a compiler this process does not have) and executing
a foreign blob from a file stay in phase 6. The context ABI and the format stay v1, unchanged.

## Goals / Non-goals
* Goals:
  * `jit-sljit` on the S0015 boundary on x86-64 (native) and aarch64 (QEMU with BTI enforced, and
    the native arm64 runner): every function a C entry, form-2a tail calls, no `tail_switch`,
    stack maps whose `ret_at`/`up` come from one audited place, BTI pads;
  * spec parity with the Python JIT of the same architecture through artifact files, as a
    behaviour oracle (D68: not bytes), with the walk check and poison;
  * mixed-store conformance tests in both directions on both architectures;
  * the recursion depth across the boundary, measured against Wasmtime;
  * CoreMark alternated with `jit-x86_64`, recorded.
* Non-goals:
  * the C micro-compiler (our core in C, D68) and the C reference loader (D36): ROADMAP "Next" (6)
    items (b) and (c). `native/` gets their place in the layout, not their code;
  * shipping `jit-sljit` to users, or any native code in the Python package;
  * Windows, macOS and Android rows for `jit-sljit` (D47: Linux glibc x86-64 first, then Linux
    arm64 under the exception below);
  * cross-compilation with `jit-sljit` (D35): sljit here is built for the host, so no D35 list for
    `jit-sljit`; determinism (same bytes in two processes) is checked instead;
  * a byte oracle between compilers (D64 item 4, D68).
* Rows (D47):
  * Linux glibc x86-64: the first row;
  * Linux arm64, under QEMU (BTI) and on `ubuntu-24.04-arm`: **exception (c)**. The code is the same
    Python and C, the existing `jit` and `arm64` jobs gain steps, and there is no new interaction
    class. The BTI pads are **exception (a)**: without them, code mapped `PROT_BTI` faults on the
    first indirect call (measured, "Measurements");
  * no other row. Those targets refuse to probe ("Where it lives").

## Interface

### Where it lives (decided, maintainer 2026-11-23)
```
.gitmodules                     native/third_party/sljit -> zherczeg/sljit, pinned commit
native/                         every C source of the repository, one place
  CMakeLists.txt                the top-level C build; works standalone and as a subproject
  third_party/sljit/            the submodule (unmodified)
  sljit/                        what jit-sljit needs in C
    waboros_sljit.c             one translation unit: our sljit configuration defines, then
                                #include "sljitLir.c", then our functions (frame numbers,
                                constants table, code-buffer hooks)
    waboros_sljit.h             the exported functions
  tests/                        C self-tests run by ctest (frame numbers, BTI pads)
  loader/, micro/               later: the C reference loader (D36) and the micro-compiler (D68);
                                not created by this spec
tests/sljit/                    the Python side of jit-sljit (development only)
  backend.py                    the D39 backend, ported from poc/sljit/sljit-be/waboros_sljit.py
  __init__.py                   registers the target if the native library is found
```
* **The C build is CMake, with no Python in it.** Someone can build `native/` with a C compiler
  and CMake alone, for example to build a C loader later.
  * No generated sources.
  * No configure-time patching.
  * The PoC's `consts2.c` → `consts2.json` step disappears: the library exports sljit's
    constants itself (`waboros_sljit_constant(name)`), so nothing is generated at build time and
    nothing runs under QEMU to produce a file.
* **The CMake contract** (maintainer, 2026-11-23):
  * `cmake_minimum_required(VERSION 3.21)`. It relies on `PROJECT_IS_TOP_LEVEL` (3.21), and an
    embedder's `FetchContent_MakeAvailable` with `SOURCE_SUBDIR native` (3.18). The CI hosts have
    3.28 (measured locally: `cmake --version` 3.28.3; Ubuntu 22.04 ships 3.22).
  * The embedding path is `FetchContent_Declare` + `FetchContent_MakeAvailable` (with
    `SOURCE_SUBDIR native`). `add_subdirectory(native)` works the same way. Our own CMakeLists
    never uses `FetchContent_Populate` (its single-argument form is deprecated since CMake 3.30).
    `find_package`/install config is secondary and not in this spec.
  * It works as a subproject:
    * paths only from `CMAKE_CURRENT_SOURCE_DIR`/`CMAKE_CURRENT_BINARY_DIR`;
    * no `include_directories`, `add_compile_options` or `CMAKE_C_FLAGS` changes: everything is a
      `target_*` property, `PRIVATE` unless a consumer needs it;
    * options prefixed `WABOROS_`;
    * targets prefixed `waboros_`, with `waboros::` aliases;
    * tests only when `PROJECT_IS_TOP_LEVEL` or `WABOROS_BUILD_TESTS`.
  * **sljit is not built through its own CMakeLists.txt.** It says it "is incomplete and is only
    meant as an aid" (sljit 39c508d), sets global `include_directories` and builds only a test
    executable (read in the scratchpad checkout). Our CMakeLists defines the sljit library target
    itself from `third_party/sljit/sljit_src/sljitLir.c` (one translation unit) with our
    configuration defines.
  * **C99** for our sources (maintainer, 2026-11-23), as sljit: `C_STANDARD 99`,
    `C_STANDARD_REQUIRED ON`, `C_EXTENSIONS OFF` on our targets only. If threads (D58) later seem
    to need C11 atomics, that is a question for then.
  * Targets:
    * `waboros_sljit` (static, alias `waboros::sljit`): `sljit/waboros_sljit.c`, which includes
      `sljitLir.c` with `SLJIT_CONFIG_AUTO=1` (the host), `SLJIT_CONFIG_X86_CET=1` on x86-64,
      `SLJIT_EXECUTABLE_ALLOCATOR=0` with our code-buffer hooks (the PoC's `lib2.c`: writable
      memory above 4 GiB, so sljit forms label addresses rip-relative), `SLJIT_UTIL_STACK=0`,
      `SLJIT_VERBOSE=0`, `SLJIT_DEBUG=0` (`WABOROS_SLJIT_DEBUG=ON` switches the last two);
    * `waboros_sljit_shared` (`WABOROS_SLJIT_SHARED`, default `PROJECT_IS_TOP_LEVEL`): the same as
      a shared library for ctypes;
    * `waboros_sljit_selftest` (ctest).
* **Packaging.** The wheel holds only `src/` (setuptools `package-dir`). The sdist today holds
  every tracked file: measured with `uv build --sdist`, it has `poc/` (151 entries), `tests/`,
  `tools/` and `docs/`. A new `MANIFEST.in` therefore prunes `native/`, so the sources never enter
  the sdist. The acceptance lists the tarball. `tests/sljit/` is Python and goes into the sdist
  with the other tests; it imports nothing native unless the library is built.
* **The Python side is outside the package.**
  * `tests/sljit/__init__.py` loads `libwaboros_sljit_shared` from `WABOROS_SLJIT_LIB`, or from
    `build/native/` by default. If the library is found, it registers the target in
    `base.TARGETS` (as the PoC's `sitecustomize.py`).
  * `tests/conftest.py` and `tests/spec/run.py` import it, so spec children see the target too.
  * Without the library the target is absent and its tests skip. That is harness code: the
    runtime never names it.
* **CI jobs**:
  * a new job `sljit` in `ci.yml` (ubuntu-latest), with `submodules: true`:
    * `cmake -S native -B build/native && cmake --build build/native && ctest --test-dir
      build/native`;
    * the spec suite on `jit-sljit` through files with the walk check, in both bounds modes;
    * `spec_diff` both ways against `jit-x86_64`;
    * the mixed-store tests;
  * the job `jit` (QEMU, BTI enforced) gains the cross build with `aarch64-linux-gnu-gcc` and
    `-DCMAKE_SYSTEM_NAME=Linux -DCMAKE_SYSTEM_PROCESSOR=aarch64`, and the same runs under
    `tools/run_arm64.py` in explicit mode;
  * the job `arm64` (`ubuntu-24.04-arm`, no BTI) gains the native build and the guard-mode runs;
  * a CMake subproject check: a 10-line consumer `CMakeLists.txt` in `native/tests/consumer/`
    pulls `native/` with `add_subdirectory` and `FetchContent` (`SOURCE_DIR` local) and links
    `waboros::sljit`, configured with `--warn-uninitialized -Werror=dev`.

### Identity: target names, compiler string, `HOST_BRIDGE`
* **Target names `jit-sljit-x86_64` and `jit-sljit-aarch64`.**
  * The header's `compiler` target must match `e_machine` (`artifact.py`, `_head`: "compiler target
    … does not match the ELF machine"), so one name cannot serve both machines.
  * Only the host's target is registered. The PoC name `jit-sljit` is retired (Q3).
  * `version = 1`.
* **Compiler string** `waboros <version> jit-sljit-<arch> 1`, as for every target (S0014 Q3).
  * A development build digests the sources that make its code. Today that is `frontend/` and
    `targets/jit/` of the package, which does not see `tests/sljit/`, `native/sljit/` or the
    sljit commit.
  * New Target method `digest_sources() -> list[Path]`, default empty: `compiler_string` folds
    those files into the digest. `jit-sljit` lists `tests/sljit/*.py`, `native/sljit/*` and
    `native/third_party/sljit/sljit_src/*`.
  * This is a Target method, not a special case.
* **`HOST_BRIDGE` (word 8)** of every `jit-sljit` context is the address of the bridge in
  `jit-sljit`'s own in-process copy of its blob (`load_trampolines`, one per target and process).
  * That copy is never another target's copy, so D71's test (equal word 8 = same compiler) stays
    true.
  * `jit-sljit` itself never compares word 8: every call through a record takes the C entry
    (below).
* **`TAIL_SWITCH` (word 9) = 0** in every `jit-sljit` context.

### The compiler: every function is a C function (Q1 (a))
The PoC's internal convention is sljit's `SLJIT_ENTER_REG_ARG | SLJIT_ENTER_KEEP(3)`: S0 context,
S1 frame and S2 memory base are kept from the caller, and the R pins are saved by the function that
uses them. That is **not** the boundary convention (arguments in `rdi`/`rsi`, `x0`/`x1`). So the
PoC's functions are not C entries, contrary to the assumption in S0015 and SLJIT-BACKEND.md ("its
function **is** its C entry"). With this spec they become C entries:
* **Every defined function and host thunk** is a sljit C function:
  `sljit_emit_enter(c, 0, SLJIT_ARGS2(32, P, P), scratches, 3 + npins_s, local)`.
  * sljit moves `ctx` and `frame` into S0 and S1. The function loads S2 (memory 0's base) from the
    context.
  * `c_entry()` returns the function's first byte; `c_entry_shared()` returns 0 (no shared part, no
    shift-0 site).
  * The record's `code` is the function itself.
* **Calls.**
  * Direct, record (any context, any compiler), `call_indirect` and `call_ref` are all C calls:
    `SLJIT_R0` = the callee's context (our S0 for a direct call, `[rec + 16]` for a record), `SLJIT_R1` = S1 +
    8 · shift, `SLJIT_CALL` with `SLJIT_ARGS2(32, P, P)` to the label or to `[rec]`.
  * The callee keeps S0–S5 (SysV and AAPCS64 callee-saved), so the caller needs no context save
    and no switch.
  * After a call the caller reloads S2 (a callee may grow an imported memory; S0015) and tests the
    32-bit status.
  * `record_sites` = 1: there is no own-compiler fast path, because own and foreign records cost
    the same C call here. D71's bypass is a `jit-x86_64`/`jit-aarch64` choice and not part of the
    boundary.
* **R pins.** The R registers (`SLJIT_R3`–`SLJIT_R6`: x86-64 `rcx`, `r8`, `r11`, `r10`; aarch64 `x3`–`x6`)
  are caller-saved in the C convention.
  * A function that makes no call keeps them, so leaf code keeps 7 pins on x86-64.
  * A function with calls writes its live R pins to its native frame before each call and reads
    them back after, on every path, before the status test (S0015 A3). The backend does this; it
    knows `f.rpins`.
  * The PoC's prologue save of the caller's R pins goes away.
  * On aarch64, sljit has 10 saved registers, so S3–S9 give 7 pins and no R pins are needed (a pool
    choice of step 4).
* **Tail calls: form 2a everywhere.**
  * `tail_direct`, `tail_record`, `tail_indirect` and `tail_ref` move the arguments to the frame
    base, set `SLJIT_R0` = the callee's context and `SLJIT_R1` = S1, and emit
    `SLJIT_CALL | SLJIT_CALL_RETURN` with `SLJIT_ARGS2(32, P, P)` to the label or `[rec]`.
  * sljit restores the saved registers and the frame and jumps. The callee returns to our caller:
    a C sibling call, the same for our own code and another compiler's.
  * There is no `tail_switch`, no hop and no private status, so `jit-sljit` never produces a
    status with bit 31 set. That makes S0015's M5 condition trivially true on its side: a
    `jit-x86_64` C entry only ever sees hop statuses from its own bodies.
  * The PoC's `tail_switch` (bytes per ISA) is deleted.
* **Bridge.** It stays a sljit `REG_ARG | KEEP(3)` function: a C function's S0–S2 are set when it
  calls the bridge.
  * The exit record (S0015 "What every bridge must do") stays as in the PoC: `sp` at the call, the
    return address, the frame.
  * It presets `HOST_ERROR` to 13, returns the word, and puts back the enclosing preset.
  * It saves nothing of the R registers: a function with calls already holds its R pins in its
    frame.
* **Stack maps.** A label after each call gives `ret`. `ret_at` and `up` come from
  `waboros_sljit_frame(compiler, &ret_at, &up)` in `native/sljit/waboros_sljit.c` (Q2 (a)):
  * x86-64: `ret_at` = `local_size` + `GET_SAVED_REGISTERS_SIZE(scratches, saveds − kept, 0)`,
    `up` = `ret_at` + 8;
  * aarch64: `ret_at` = 8 (`x30` of the frame record at `[sp + 8]`), `up` = `local_size` after the
    prologue.
  * These are the expressions sljit's own `SLJIT_GET_RETURN_ADDRESS` uses
    (`sljit_emit_get_return_address` on x86-64; `[sp + 8]` on aarch64). They live next to
    `#include "sljitLir.c"`, pinned by the submodule commit and checked by the ctest self-test
    against the real return address and stack pointer.
  * The PoC's `lib2.c` keeps `x30` at `size − 8` for aarch64 frames above 512 bytes. That is
    **wrong** for sljit 39c508d: measured, `x30` is at `[sp + 8]` for every local size up to 4096
    ("Measurements"). The PoC never hit it (72-byte frames).
* **Frame size (D67).**
  * The PoC reserves 9 words in every function, giving 96-byte frames against 32 and recursion about
    5 400 deep against 16 400 (SLJIT-BACKEND-RESULTS, update 3).
  * The D67 pre-pass moves earlier, into the Python core: the core scans a body once and passes
    `func_begin` the hints (calls through a record or the host, any call, `wide`/v128 scratch, bulk
    copies, the pins).
  * The Python backends ignore the hints; their code is byte-identical (D35 lists), as D67
    requires. This amends D67's "implemented with the C core, not before" (Q4).
  * The `jit-sljit` frame is then `local` = 8 · (live R pins of a function with calls + scratch
    words it needs), 0 for a leaf without scratch.
* **Headroom (S0015).** `sljit_emit_enter` pushes and allocates before our first check.
  * The worst case is 6 pushes, the return address and `local`. The native frame is bounded by
    design: 4 R-pin words + 4 scratch words + 1 + the v128 scratch the hints ask for.
  * The backend asserts that pushes + `local` ≤ 768 bytes and otherwise refuses the module
    (`Unsupported`). Expected: no spec module comes near it (the PoC's fixed frame was 96).
  * Nothing is kept below the stack pointer: sljit allocates its locals and has no red zone (to be
    confirmed by the review).
* **BTI and CET.**
  * x86-64: `SLJIT_CONFIG_X86_CET=1` makes `sljit_emit_enter` emit `endbr64` for every function,
    with any option (read: `emit_endbranch` in the x86-64 enter).
  * aarch64: sljit emits no BTI (grep in sljit 39c508d). Every function and thunk starts with
    `bti jc` (`0xd50324df`) through `sljit_emit_op_custom` **before** `sljit_emit_enter`. That
    accepts `bl`, `blr` and the `br x9` of `SLJIT_CALL_RETURN` (S0015 Q5 (a)). The bridge gets
    `bti c` (it is only `blr`'d).
  * No `bti j` is needed: `jump_table` is a compare tree in the PoC (no indirect branch), and sljit
    emits no other indirect jump inside a function (expected; the BTI-enforced spec run checks it).
  * No `paciasp` (sljit does not sign return addresses).
* **Machine state (S0015 table).** sljit code:
  * never sets DF and never changes MXCSR or FPCR;
  * uses no x87;
  * emits no VEX (no `SLJIT_ENTER_USE_VEX`; SIMD goes through our SSE2/NEON bytes), so no
    `vzeroupper`;
  * never touches `x18` (sljit's aarch64 `reg_map` has no 18);
  * makes a frame record and points `x29` at it before any call (seen in the dump: `stp x29, x30,
    [sp, #-32]!` … `mov x29, sp`);
  * keeps float saved registers (`d8`–`d15`) only if `fsaveds` > 0, which the backend never asks
    for.

  The review checks each row against the code.
* **Position independence and determinism.** As in the PoC:
  * no process address in the code (SLJIT-BACKEND.md, check 3);
  * the code buffer lies above 4 GiB, so `SLJIT_MOV_ADDR` is rip-relative on x86-64;
  * the test writes the same module in two processes and expects identical bytes, then loads it at
    two addresses.

### The artifact files of `jit-sljit`
* `Target.conventions()` of `jit-sljit` gives its blob per convention: the bridge (sljit code), and
  `tail_switch` and `restore` **none** (`0xFFFFFFFF` in the file, `restore_ret_at` and
  `restore_up` 0).
* **x86-64 conventions: `sysv` only** (Q5 (b)). sljit's SysV build cannot call a Win64 host
  function (LESSONS 141), and `jit-sljit` runs on no Windows row. A loader whose host convention
  has no blob refuses the file (the existing record-count check fails: "not one record per
  convention"). That is a deliberate D23 deviation for a development target, recorded in
  DECISIONS.md; S0014's "x86-64 writes `sysv` then `win64`" becomes "the conventions the compiler
  supports, in that order".
* **Writer and reader: the none path.**
  * `_text` writes `o + x` for every offset, which overflows for none. It writes `0xFFFFFFFF`
    unchanged.
  * `_trampolines` compares `x − start`, which is wrong for none. It compares none as none.
  * Both are about 4 lines (read in `artifact.py`).
* **`load_trampolines`**: a none `tail_switch` gives `Trampolines.tail_switch = 0` (word 9 = 0)
  and `restore = 0`, which the walk never matches.
* `features_required`, flags, ISA level: as for the Python JITs (baseline only; `tools/isa_all.py`
  runs on `jit-sljit` files too).

### What the runtime and loader change (target-neutral)
Each item is data from a `Target` or its `Trampolines`, never a target name:
1. **Mixed stores in-process.**
   * An instance of any `JitTarget` can import another's records: both are `JitInstance`, and the
     records are three words. That already works in-process, as the S0015 gate showed.
   * What changes is the store's view of trampolines: `_StoreStacks` keeps the set of
     `Trampolines` of its instances, added at instantiation.
2. **The walk's sets** (S0015 walk item 7):
   * entry points: the `entry_ret` of every `Trampolines` in the set. Each target seals its own
     copy of the runtime's blob, so there is one per target;
   * restore points: `{restore: (restore_ret_at, restore_up)}` for each with a `tail_switch`.
   * `walk.walk` and `collect.py` take the store's set instead of `inst.tramp`.
   * About 20 lines.
3. **Distinct blobs per compiler string** (D71's precondition): at instantiation the store checks
   that a `bridge` address belongs to one compiler string only. It cannot fail today (one copy per
   target and process); the check guards against a future cache that shares copies.
4. **Loader.**
   * `load(b, target=…)` already takes the compiler. A mixed store from files loads each file with
     the target named in its own compiler string, and each passes the **exact-string** rule and
     `read`'s compare against its own target's `conventions()`.
   * **No relaxation of D70's rule is needed**, and no foreign blob is executed: both compilers are
     in the process.
   * `artifact.TARGETS` (name → `e_machine`, ISA name) is read from Target attributes `e_machine`
     and `isa_name` instead of a fixed dict, so a registered development target is known to the
     reader (about 10 lines).
5. **`compiler_string`** folds in `digest_sources()` (above).

What stays in phase 6, unchanged by this spec:
* the per-architecture compatibility rule (a thin file of a compiler or version this process does
  not have);
* mapping and executing a foreign blob from a file;
* the trampoline compare moved from `read` to store attach;
* S0015 Q4's flags across artifacts. Here both compilers read one `WABOROS_JIT_BOUNDS`, so a store
  has one bounds mode, but the tests also mix modes, which S0015 Q4 (a) allows.

ROADMAP phase 6 loses the items this spec implements (mixed stores in-process, the walk's sets, the
none path, the D71 distinctness check, the real-second-compiler test).

## Semantics
* **Traps** are status returns through `jit-sljit` frames and C calls, unchanged. Guard-mode fault
  points are sljit labels on the one faulting instruction, and the per-process handler resumes in
  the same artifact (S0015).
  * On aarch64 sljit splits some accesses; the PoC's guard-mode rules (a constant address through
    an index register, a stored immediate in a register first) stay.
* **Exceptions**: status 14 and the store's pending word (word 10), passed through unchanged;
  `landing` and `throw_ref` as in the PoC.
* **Interrupts, epoch, stack limits**: polled from the context in the prologue and at loop heads,
  as in the PoC. Fuel waits for S0015 Q4.
* **The collector's context** compiles the collector with the runtime's own compiler
  (`jit-x86_64`/`jit-aarch64`, S0015), never `jit-sljit`.
* **I1**: at every site no reference is in a register (the PoC passed the walk check with poison,
  D68); references are never pinned.

## Measurements
Scratch experiments of 2026-11-23, sljit 39c508d (the scratchpad checkout), not committed. Files
under `/tmp/claude-0/-home-user/08f045a6-cb18-5fbb-bbb0-417b880032a2/scratchpad/s16/`.
* **The PoC does not run on HEAD** (measured, x86-64):
  * `PYTHONPATH=…/s16/sljit-be/site:src:. uv run python -m tests.spec.run --engine jit-sljit core`
    gives pass 2 027, fail 18 571;
  * every module fails with `AttributeError: 'Backend' object has no attribute 'c_entry'`. The 2 027
    are the commands that compile nothing.
  * So the port is step 1, not a copy.
* **`ret_at` and `up` from one expression** (`exp/exp.c`):
  * Setup: 90 frame shapes per architecture (saveds 2–6, 7 scratches, local 0–4096, C functions
    and `REG_ARG|KEEP(3)`). Each callee reads its own return-address slot at the computed offset
    and its stack pointer; the caller compares them with the label after its call and with its own
    stack pointer.
  * Result: 0 wrong on x86-64 natively (`cc -DSLJIT_CONFIG_X86_CET=1`) and on aarch64 (clang
    `--target=aarch64-linux-gnu`, freestanding, `qemu-aarch64 -cpu max`).
  * On aarch64 `ret_at` is 8 for every size, so the PoC's above-512 branch is wrong.
* **Frame sizes (`up`)** of the smallest frames, measured by the same program:

| shape | x86-64 | aarch64 |
|---|---|---|
| C function, saveds 3, local 0 | 32 | 48 |
| C function, saveds 6, local 0 | 64 | 64 |
| C function, saveds 3, local 72 | 112 | — |
| `REG_ARG|KEEP(3)`, saveds 3, local 0 | 16 | 16 |
| `REG_ARG|KEEP(3)`, saveds 6, local 0 | 32 | 48 |

  For comparison, a direct call costs 16 bytes in `jit-x86_64` (D71) and in Wasmtime 32 761
  levels / 512 KiB, and 32 bytes in `jit-aarch64`.
* **BTI** (aarch64, QEMU 8.2 `-cpu max`, pages `PROT_BTI`):
  * A and B are sljit C functions that tail-call each other by `SLJIT_CALL_RETURN` (`br x9`, seen in
    the dump after `ldp x29, x30`), entered by `blr`.
  * With `bti jc` first (custom word before `sljit_emit_enter`), 10⁵ hops return 77.
  * Without it: SIGILL (exit 132).
* **CMake and sljit's build**:
  * `native` is not built yet; `cmake --version` 3.28.3 here;
  * sljit's CMakeLists.txt is test-only (read);
  * the PoC builds with `cc … lib2.c` and the same defines this spec puts in CMake.
* **sdist contents**: measured with `uv build --sdist`, the tarball holds every tracked file
  (`poc/` 151 entries; `MANIFEST.in` and `.gitmodules` absent).
* **Not measured, expected:**
  * CoreMark of C-function `jit-sljit` against the PoC's `REG_ARG` form. Each call now costs three
    more pushes, two moves and a load, and the R pins are spilled around calls in non-leaf
    functions. The PoC's parity (D68) is the baseline;
  * the depth numbers below;
  * x86-64 CET shadow-stack balance (no runner enforces it).

**Expected recursion depth at the default 512 KiB** (from the frame sizes above, less the
headroom; to be measured in step 6, "Test plan"):

| path | x86-64 bytes / level | aarch64 bytes / level |
|---|---|---|
| `jit-sljit` direct, a leaf-like recursive function (saveds 3, no R pins) | 32 (about 16 350 levels; Wasmtime 32 761) | 48 |
| `jit-sljit` own-table `call_indirect` | 32 (Wasmtime 16 380) | 48 |
| a cross-compiler pair (`jit-x86_64` C path, D71, then a `jit-sljit` frame) | the C path's saves and return address plus 32; measured in step 6 | the same with 48 |

## Test plan
Acceptance commands, x86-64 unless marked. "Through files" is `WABOROS_JIT_ARTIFACT=1`.
* **The native build**: `cmake -S native -B build/native && cmake --build build/native && ctest
  --test-dir build/native`.
  * The self-test checks the frame numbers of every shape above against reality.
  * On aarch64 it also runs the BTI ping-pong under QEMU `-cpu max`, with the control that must
    SIGILL.
  * The consumer project configures with `add_subdirectory` and `FetchContent`, with no warning.
* **Packaging**: `uv build`; `tar tzf dist/*.tar.gz | grep -c '/native/'` gives 0; the wheel has no
  file outside `waboros/`; `pytest -q` passes with the library absent (the `jit-sljit` tests skip).
* **The spec suite on `jit-sljit`**, through files, with `WABOROS_JIT_WALK_CHECK=1
  WABOROS_JIT_GC_POISON=1`, guard and explicit:
  * 64 229 pass, 0 fail, as `jit-x86_64`;
  * `tools/spec_diff.py jit-x86_64 jit-sljit-x86_64` empty both ways.
  * aarch64: the same under `tools/run_arm64.py` (explicit, BTI enforced), and guard mode on the
    arm64 runner, against `jit-aarch64`.
  * `gc` and `exceptions` with `WABOROS_JIT_GC_STRESS=1 WABOROS_JIT_GC_VERIFY=1` added: 905 pass.
* **Mixed-store conformance**, new `tests/jit/test_mixed.py`. It is parametrised over the pairs
  (`jit-x86_64`, `jit-sljit-x86_64`) and (`jit-aarch64`, `jit-sljit-aarch64`), both directions
  (A by one compiler, B by the other), in-process and through files:
  * calls: an import of another instance, `call_indirect` and `call_ref` into the other compiler's
    table, results and every value type (i32 zero-extended, i64, f32/f64 bits, v128 two slots,
    references);
  * tail-call ping-pong across compilers, S0015's three entry shapes (from the host, from a direct
    call, through a C entry):
    * 10⁶ round trips in a 256 KiB thread finish;
    * the call-instead-of-tail control ends in `STACK_OVERFLOW`;
    * the chain must include `jit-x86_64`'s `tail_switch` into a sljit C entry and its
      return-then-jump hop into one, and sljit's `CALL_RETURN` into a `jit-x86_64` C entry;
  * statuses: every public status (a trap, `INTERRUPT` set by a thread while a loop runs in the
    callee, a host error, status 14 caught one instance up in the other compiler, uncaught to the
    host) through one and two C entries of either compiler. No private status reaches the host
    (`test_boundary.py`'s spy);
  * the walk and GC: host → A → B → A → host import with a reference live in each frame, under the
    walk check:
    * the frames match the runtime's record, and a collection forwards every frame's references;
    * the same inside a mixed tail chain;
    * the same with the collection started from each compiler's frames;
  * D71 seen from the other side: `jit-x86_64` and `jit-aarch64` take the C path, not the body,
    for a `jit-sljit` record (a spy on the path taken, or the depth per call);
  * `HOST_BRIDGE` distinctness: two compilers in one store, words 8 differ, and a forced equal
    word is refused at instantiation;
  * a `jit-sljit` context has word 9 = 0; its file's trampolines record reads none, round-trips,
    and a corrupted none is refused.
* **Files of `jit-sljit`**:
  * `tests/artifact/` cases for the second compiler string: read, inventory, instantiate thin and
    fat;
  * a thin `jit-sljit` file loaded with `target=jit-x86_64` is refused on the compiler string, and
    the reverse;
  * `tools/isa_all.py --target jit-sljit-<arch>` (baseline only);
  * identical bytes from two processes;
  * the elf readers (`tools/elf_readers.py`) on one file.
* **Recursion depth**, measured at 512 KiB until `STACK_OVERFLOW`, beside Wasmtime 50.0.1, and
  recorded in BENCHMARKS.md:
  * direct recursion;
  * own-table `call_indirect`;
  * across instances of `jit-sljit`;
  * across compilers in each direction.
* **CET/GCS balance: by argument and review, not by test.**
  * Every `jit-sljit` call returns with `ret` to its own call; `SLJIT_CALL_RETURN` restores the
    frame (x86-64 `pop`s, aarch64 `ldp x29, x30`) and jumps, so the callee's `ret` matches the
    caller's call.
  * The bridge is an ordinary call and return.
  * There is no `tail_switch` and no hop.
  * `jit-x86_64`'s hop into a sljit C entry is S0015's, reviewed there.
  * No runner enforces CET or GCS (S0015); the reviewer checks this list.
* **CoreMark** (`tools/run_coremark_jit.py --target jit-sljit-x86_64`), alternated with
  `jit-x86_64`, `taskset -c 2`, several rounds; both self checks pass. The comparison point is
  D68's parity; a placement control is not needed between two compilers. aarch64 timing on the
  native runner (`placement-arm.yml` compare mode or an ad-hoc job).
* **No change to the Python JITs' code**: the D35 lists of `jit-x86_64` and `jit-aarch64` stay equal
  to HEAD's after the core's D67 pre-pass and the loader changes (`tools/jit_dump.py`).

## Plan of steps
The design is a contract change only in the runtime's store and loader; the compiler is a
development target, so the reviews concentrate on the boundary and the runtime. Each step stops
and reports if it needs more than its size.

| step | goal | agent | acceptance | size (lines) |
|---|---|---|---|---|
| 0 | `native/` with the sljit submodule at the pinned commit; `CMakeLists.txt` (targets, options, C99, no globals); `waboros_sljit.c` (config defines, buffer hooks, `waboros_sljit_frame`, the constants table); the ctest self-test (frame numbers; BTI ping-pong on aarch64); the consumer check; `MANIFEST.in` prunes `native/`; CI build steps in `sljit`, `jit` (cross, QEMU) and `arm64` | implementer | the native-build and packaging commands of the Test plan on x86-64 and under QEMU; `pytest -q` unchanged; `check_docs.py` | C ~250, CMake ~120, CI ~40 |
| 1 | `tests/sljit/` on HEAD's D39 interface (`c_entry`, `record_sites`, the S0015 trampolines API), x86-64: every function a C function, C calls everywhere, R pins spilled around calls, form-2a tail calls, no `tail_switch`, the bridge, constants and frame numbers from the native library, `endbr64` by CET config; registration hook | jit-engineer | the spec suite on `jit-sljit-x86_64` in-process with walk check and poison, guard and explicit, `spec_diff` empty against `jit-x86_64` | py ~600 changed of ~1 500 |
| 2 | The D67 pre-pass in the core and `func_begin` hints; `jit-sljit` sizes its frame; the 768-byte assertion | jit-engineer | D35 lists of both Python JITs equal to HEAD's; the step-1 runs unchanged; depth of `jit-sljit` measured | src ~80, py ~40 |
| 3 | Runtime and loader: the store's set of `Trampolines`, the walk's entry and restore sets, `collect.py`, the bridge distinctness check, the none path (writer, reader, `load_trampolines`), `artifact.TARGETS` from Target attributes, `digest_sources()`; sysv-only conventions for `jit-sljit` | implementer | `pytest -q` (artifact tests on both architectures); the spec suite through files on all three targets at parity; D35 lists unchanged | src ~70, tests ~120 |
| 4 | `tests/jit/test_mixed.py` and the `jit-sljit` artifact tests, x86-64 | jit-engineer | the mixed-store tests pass in-process and through files, guard and explicit | tests ~350 |
| 5 | Review of steps 1-4 without the implementers' reasoning: the boundary table against `jit-sljit`'s code, CET balance, I1, headroom, M5, the runtime's sets, the none path | reviewer | findings closed before step 6 | — |
| 6 | aarch64: `bti jc` pads, the aarch64 frame numbers, the pool (S3-S9), guard-mode access rules; the mixed tests with `jit-aarch64`; CoreMark and the depth table on both architectures | jit-engineer, then runner | the Test plan's aarch64 runs (QEMU explicit with BTI, the arm64 runner guard); BENCHMARKS rows | py ~120 |
| 7 | Docs ("Docs this changes"); S0015 and S0014 Changes lines; ROADMAP's phase-6 list; the D73 freeze marker updated | orchestrator | `python3 tools/check_docs.py` | — |
| 8 | Final review of step 6 and of the whole | reviewer | findings closed | — |

About 1 000 lines of Python glue (mostly ported), 250 of C, 120 of CMake, 150 of runtime and
loader source, 470 of tests.

## Alternatives considered
* **Two-level `jit-sljit`, like the Python JITs** (Q1 (b)): bodies keep `REG_ARG|KEEP(3)` behind a
  per-function C entry, own-compiler records call the body (D71's bypass), and cross-compiler tail
  calls hop with a private status.
  * It keeps the PoC's measured CoreMark parity and 16-byte frames.
  * The scratch gate's `pong2` showed the mechanism for one function.
  * Its traps for a whole module:
    * a body must recognise *any* C-entry return point of its compiler. With one C entry per
      function, a same-context tail call from body j to body k leaves k with E_j's return point,
      which k's test against its own E_k misses, so the chain grows;
    * a shared C entry with fixed-size stubs needs jump sizes sljit does not guarantee (it shortens
      jumps that fit), and the bypass needs `body = code + const`;
    * the fixes are a byte-signature test at the return address, or all C entries in one range with
      a range test. Each is new machinery: private statuses, shift-0 sites in the core's count,
      return-point tests.
  * Kept as the measured fallback if step 1's or 6's numbers show the C-function form costs too
    much.
* **The PoC unchanged plus a C-entry stub only for the boundary**, with tail calls into other
  contexts through a `tail_switch` as the PoC has. That fails the goal: it would test neither form
  2a nor word 9 = 0.
* **Patching sljit for a public `sljit_get_return_address_offset`** (Q2 (b)): the clean API, but
  configure-time patching contradicts the CMake-only build unless the patch is upstream or the
  submodule points at a fork (SLJIT-BACKEND.md: "sljit is not forked").
* **`jit-sljit` inside `src/waboros`** with a lazily built library: rejected by the maintainer's
  placement (sources never in the package; no runtime dependency).
* **Phase 6's full loader rule now** (accept any `waboros … jit-*-<arch>` string of the right
  machine): not needed for a mixed store whose compilers are all in the process. It would open
  the foreign-blob path before it is specified for W^X and the instruction cache.

## Open questions
1. **The internal convention of `jit-sljit`.**
   * (a) Every function a sljit C function (above). Pure form 2a, no hop, no private status, no
     C-entry sites, no core change. Cost: x86-64 frames +16 bytes (32 against 16 for a frame with
     3 saveds; measured), aarch64 +32 (48 against 16). A direct call adds 3 pushes and pops, 2 moves
     and a load. CoreMark not measured.
   * (b) Two-level with hops (Alternatives): 16-byte frames and the PoC's speed, at the cost of
     the return-point recognition above, private statuses and a core change for C-entry sites.
   * Recommendation: **(a)**. It is the form S0015, SLJIT-BACKEND.md and the D73 marker describe,
     and the smallest. Step 2 measures the depth and step 6 CoreMark; (b) only if a measurement
     shows (a) does not pay. Note for the maintainer: (a)'s direct recursion is about half of
     Wasmtime's depth on x86-64 (32 against 16 bytes a level).
2. **`ret_at` without reading sljit internals elsewhere.**
   * (a) One function in our translation unit that includes `sljitLir.c`, using the expressions of
     sljit's own `SLJIT_GET_RETURN_ADDRESS`, pinned by the submodule commit and verified by the
     ctest self-test (90 shapes per ISA). We also offer upstream a public
     `sljit_get_return_address_offset()` and switch to it when it lands.
   * (b) Our sljit patch applied at build time, or a fork as the submodule.
   * Recommendation: **(a)**: no patching in the CMake build, and the self-test catches a submodule
     bump that changes the frame. Cost about 30 lines of C and 60 of test.
3. **Target names.**
   * (a) `jit-sljit-x86_64` and `jit-sljit-aarch64`, the host's registered.
   * (b) One name `jit-sljit` with the machine from the host, which needs `_head`'s
     compiler-target/`e_machine` check changed.
   * Recommendation: **(a)**: no reader change, and the names follow `jit-<arch>`.
4. **When the D67 pre-pass comes.**
   * (a) Now in the Python core (step 2), hints ignored by the Python backends, bytes unchanged.
   * (b) With the C core, as D67 says, with `jit-sljit` on a fixed frame until then.
   * Recommendation: **(a)**. A fixed 96-byte frame would make every depth measurement of this
     spec meaningless for the micro-compiler. D67 gets an amendment, and the D35 lists show the
     Python JITs unchanged.
5. **The x86-64 Win64 blob of `jit-sljit`.**
   * (a) A Win64 bridge in our own bytes (the host call with `rcx`/`rdx` and 32 bytes of shadow
     space, about 30 lines with our encoder), so its files keep D23's OS independence but are
     tested nowhere.
   * (b) `sysv` only: its files are refused on Windows hosts, as a D23 deviation for a development
     target.
   * Recommendation: **(b)** now; the C micro-compiler (item (c) of ROADMAP "Next" (6)) solves
     Win64 for real.
6. **How the harness finds the target.**
   * (a) `tests/sljit` imported by `tests/conftest.py` and `tests/spec/run.py`, active when the
     library exists.
   * (b) An environment variable naming development target modules.
   * Recommendation: **(a)**: no new option, and children of the spec runner import it the same way.

## Docs this changes
Each is updated in the step that makes it untrue (step 7 for the rest).
* [S0015](S0015-boundary-convention.md):
  * "The C entry and the function record" and SLJIT-BACKEND.md's boundary paragraph: a
    sljit-based compiler's function is its C entry only if it uses sljit's C convention (the PoC's
    `REG_ARG` functions are not);
  * the sljit patch list ("What sljit-based compilers must add"): `ret_at` through Q2 (a), BTI as
    measured;
  * Compatibility and the deferred list: what moved here;
  * a Changes line.
* [S0014](S0014-artifact-format.md): "x86-64 writes `sysv` then `win64`" becomes "the conventions
  the compiler supports" (Q5); the none path in the writer and reader; a Changes line.
* [ARTIFACT.md](../ARTIFACT.md) part 3: the compiler-private rows gain `jit-sljit` (bridge, no
  `tail_switch`, exit-record frame register S1); part 2: the loader paragraph (one store, several
  exact compiler strings, each file with its own compiler).
* [DECISIONS.md](../DECISIONS.md):
  * a new decision for this design (C functions, names, sysv only, the in-process mixed store);
  * amendment markers on D67 (the pre-pass now), D70 (mixed stores before phase 6, in-process),
    D64 item 5, D23 (the `jit-sljit` deviation).
* [ROADMAP.md](../ROADMAP.md): "Next" (6) item (a) progress; phase 6's S0015 list without the items
  done here; the D73 freeze conditions as met.
* [ARCHITECTURE.md](../ARCHITECTURE.md): `native/` (C sources, CMake only, never in the package)
  and the development target; the store holding several compilers.
* [TESTING.md](../TESTING.md): the development dependencies (CMake ≥ 3.21, a C99 compiler,
  `aarch64-linux-gnu-gcc` for the QEMU job), the `sljit` job and the new steps of `jit` and
  `arm64`, `test_mixed.py`, the ctest self-test, the CI matrix.
* [TARGETS.md](../TARGETS.md): development targets (`jit-sljit-*`, not selected by `auto`, not
  shipped).
* [SLJIT-BACKEND.md](../SLJIT-BACKEND.md): the boundary paragraph (C functions, no stub, no
  `tail_switch`), "Nothing is built yet" replaced; the build section points at `native/`.
* `poc/sljit/sljit-be/SLJIT-BACKEND-RESULTS.md`: a note that the backend moved to `tests/sljit/`
  and that `lib2.c`'s above-512 aarch64 branch is wrong. The PoC stays runnable at its base commit,
  not on HEAD (measured).
* [BENCHMARKS.md](../BENCHMARKS.md): CoreMark and the depth table.
* [LESSONS.md](../LESSONS.md): the PoC's `REG_ARG` functions are not C entries; sljit's aarch64
  return address is always at `[sp + 8]`; what the steps find.
* [RISKS.md](../RISKS.md) R2: the BTI row for sljit code (`bti jc` by custom word).
* `pyproject.toml` (nothing: the wheel takes `src/` only) and a new `MANIFEST.in` (prune `native/`);
  `.gitmodules`; `CLAUDE.md`'s command list (the native build, the `jit-sljit` spec run).
* FEATURES.md: none. `tools/features.py` covers package targets only, and `jit-sljit` is not one.
* [specs/README.md](README.md): this spec's row.

## Progress
* 2026-11-23: draft.

## Changes
* 2026-11-23: created (draft), with the maintainer's decisions of 2026-11-23 on placement (the
  submodule, `native/`, development target only), the CMake-only build (FetchContent as a
  subproject, minimum 3.21) and C99.
