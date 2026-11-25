---
title: The C reference loader
kind: spec
id: S0017
status: draft
superseded_by:
related: [D18, D23, D32, D34, D36, D47, D50, D54, D56, D58, D60, D61, D62, D65, D68, D69, D70, D71, D72, D73, D74, D75, R2, R3, R6, S0001, S0003, S0006, S0013, S0014, S0015, S0016]
owner: maintainer
updated: 2026-11-25
---

# S0017 The C reference loader

## Summary
A loader and runtime for JIT artifacts written in C99 and built with CMake alone (D36, D60;
ROADMAP.md "Next" (6)(b)). It reads format v1 ([S0014](S0014-artifact-format.md)) with the same
checks as the Python reader, instantiates a module from the file alone, and runs it under context
ABI v1 with the boundary of [S0015](S0015-boundary-convention.md): its own `entry`, host functions
behind each compiler's bridge (with D75's word argument), traps, interrupts, stack limits, the frame
walk, the GC heap with the collector module, exceptions, and stores that hold code of several
compilers. The Python loader (`targets/jit/common/artifact.py`, `instance.py` and the runtime
services) is the oracle.

There are two builds: one embeds a compiler for its own architecture, the other has none and runs
only AOT files. This spec defines the loader and the **seam** where a compiler plugs in. It does
not define the compiler, because D68's micro-compiler does not exist yet (ROADMAP "Next" (6)(c)).
The C loader is the second of the final freeze's conditions (ARTIFACT.md, Status; D73's
amendments).

Fixed before this spec by the maintainer (2026-11-23 and 2026-11-24; ROADMAP "Next" (6),
[MICRO-COMPILERS.md](../MICRO-COMPILERS.md) "The C variant", D72), and not reopened:
* C99 and CMake only, with no Python needed to build; under `native/`, following S0016's CMake
  contract;
* two builds, with and without an embedded compiler;
* the GC collector stays a wasm module, and a build option leaves GC out;
* instantiation and the segment libcalls are written in C from the specification (D72);
* artifacts are architecture-dependent and OS-independent (D23); v1 must not block 32-bit or MCU
  targets;
* the oracle: the spec suite through files and the refusal corpus must agree on both loaders,
  and files from both compilers must load.

## Decisions
Taken by the maintainer on 2026-11-24 on this draft's open questions. The options and costs are
in "Changes" and in D36's marker and D75.
1. **What it is** (Q1): a test tool for now, with an internal C API: no stability promise, no
   `find_package` and no install. The same sources become the C variant and the product later,
   by a later decision (D36's marker).
2. **Rows** (Q2): Linux glibc, macOS and Windows, on x86-64 and aarch64 (macOS x86-64 and arm64,
   Windows x64 and ARM64), all before the final freeze. Each row beyond Linux glibc x86-64 comes
   under D47 exception (a) ("Rows").
3. **Harness** (Q3):
   * the spec suite and the refusal corpus drive the C loader in-process through a thin ctypes
     shim: the S0001 engines `cloader-<target>`;
   * `waboros_run` (load a file, call an export) runs CoreMark and the committed files under
     ASan/UBSan with no Python in the process;
   * a C wast runner comes later, with the embedded compiler.
4. **Host calls** (Q4, D75): the bridge passes the context-word index, `host(ctx, slots, word)`.
   The C loader has one static dispatcher, and the embedder writes
   `int32_t f(void *env, uint64_t *slots)`. D75 is implemented first, as prerequisite P1.
5. **Data in the build** (Q5):
   * `collector.wasm` as a committed `.c` array, tied to the `.wasm` by a test;
   * a collector `.wbo` through CMake's `file(READ … HEX)`;
   * run-time supply stays;
   * S0016's rule reads "no generator outside CMake".
6. **Refusal agreement** (Q6): per file, the two loaders agree on:
   * accept or refuse;
   * for a refused file, the S0014 step (1-6; for step 6 also the section name);
   * for an accepted file, the inventory dump.
   No corpus file may be refused through the Python loader's internal-exception path
   (`cannot load: …` in `artifact.load`). The step is carried by Python's `ArtifactError.step`
   (prerequisite P2) and by `waboros_error.check`.

## Goals / Non-goals
* Goals:
  * the C library `waboros_loader` and the program `waboros_run`, built from the repository's C
    sources with a C99 compiler and CMake 3.21, as a top-level project or as a subproject, on the
    six rows;
  * a strict reader that accepts and refuses exactly what `artifact.load` does, at the same S0014
    step. The one exception is the trampoline rule, which differs by design for a foreign host
    (S0014, "One copy per store", "A foreign host");
  * instantiation, calls, host functions, traps, interrupts, stack limits, GC and exceptions that
    agree with the Python runtime on the spec suite through files:
    * `jit-x86_64` and `jit-aarch64` files on every row;
    * `jit-sljit-*` files and mixed stores on Linux glibc, the only row of the sljit targets
      (D74);
  * the committed v1 files run, or are refused, as "Test plan" item 5 says;
  * the compiler seam, specified so that D68's micro-compiler plugs in without a change to the
    loader.
* What "reference" means: correctness and clarity before speed. Each rule cites the section of
  S0014, S0015 or ARTIFACT.md that it implements. There are no fast paths without a measurement
  (CLAUDE.md, "Measure, do not estimate"), and the code is small enough for a reviewer to read
  whole.
* Non-goals:
  * a product: a stable C ABI, packaging, install, `find_package` (Decision 1);
  * the public API of phase 6 (wasmtime-py's shape);
  * host functions that no module imports (Wasmtime's `Func::new`): phase 6, through D75's shim
    module, which a build without a compiler gets as one more AOT file;
  * WASI and any host-function library beyond what the tests need;
  * the compiler (D68), the compiler `.wasm`, cross-compilation, a C wast runner (Decision 3);
  * static builds and linked AOT artifacts (D62, [STATIC-BUILDS.md](../STATIC-BUILDS.md));
  * threads (D58: words 6-8 of the libcall block stay zero, and a shared memory is refused);
  * fuel and epochs (no flag in v1: words 3 and 4 stay 0);
  * `isa_level` above 1 (as the Python loader: S0014, "Known limitations of v1");
  * 32-bit hosts and MCUs: nothing here blocks them ("C99 rules"), but none is built.

### Rows (D47)
Per-OS code here means: executable memory and W^X, address-space reservations, stack bounds, and
the fault handler. The first row is Linux glibc x86-64. Every other row comes under **exception
(a)**: without its per-OS code the loader cannot run there at all, or it breaks W^X, BTI or "never
crash". The maintainer's Decision 2 asks for these rows before the final freeze.

| row | per-OS code | evidence for (a) | CI |
|---|---|---|---|
| Linux glibc x86-64 | `os_posix.c`: `mmap`/`mprotect`, `sigaction(SIGSEGV)`, `pthread_getattr_np` | the first row | `ci.yml` `cloader` (new), `ubuntu-latest` |
| Linux glibc aarch64 | the same file; `__builtin___clear_cache`, `PROT_BTI` when `AT_HWCAP2` has BTI, the PC from `uc_mcontext.pc` | no instruction-cache coherence: code without the flush is wrong (part 3, "Per architecture"); BTI pads enforced | `arm64` native, `jit` (QEMU `-cpu max`, BTI) |
| macOS x86-64 | `os_posix.c` (Apple parts): the handler on `SIGSEGV` and `SIGBUS` with `raise` after chaining (`si_code` does not tell, LESSONS 57); stack bounds by `pthread_get_stackaddr_np`/`_stacksize_np` | a guard fault arrives as `SIGBUS` (S0006 step 3): without it a guard trap crashes; `pthread_getattr_np` does not exist | `platforms.yml` `macos-26-intel` |
| macOS arm64 | as above, plus `MAP_JIT` with `pthread_jit_write_protect_np` around every write of code, and `sys_icache_invalidate` | Apple silicon refuses W+X pages without `MAP_JIT` and switches write protection per thread: without it no code runs (R2) | `platforms.yml` `macos-26` |
| Windows x64 | `os_windows.c`: `VirtualAlloc`/`VirtualProtect`/`VirtualFree`, a vectored handler (`AddVectoredExceptionHandler(1, …)`), `GetCurrentThreadStackLimits`; the Win64 `entry` | no `mmap` and no signals; a SysV C entry clobbers registers Win64 keeps (S0015 exception (a)) | `platforms.yml` `windows-latest` |
| Windows ARM64 | the same file, `FlushInstructionCache`, the PC from `ContextRecord->Pc` | as Windows x64, and no instruction-cache coherence | `platforms.yml` `windows-11-arm` |

* The macOS handler uses signals, as the Python runtime does (S0006 step 3). Mach exception
  ports, which Wasmtime uses by default, are not taken: the oracle uses signals, and two
  mechanisms would double the tests.
* **The Win64 `entry` runs from a C host for the first time.** Python runs the code that
  ARTIFACT.md's Win64 listing matches ("Checked"), and the C bytes are tied to `entry.py` by a
  test. **A file's `win64` blob is executed for the first time**: the Python runtime never
  executes a file's blob. Step 8 is where both are first seen to work.
* Linux musl and Android are not rows of this spec (Open question 2).

## Interface

### Where it lives
```
native/
  CMakeLists.txt          + the loader's options and targets (below)
  loader/
    waboros.h             the only public header: the internal C API below
    read.c                ELF container and payloads, S0014 checks 1-6, cexpr parse and types
    sha256.c              module_sha256 of a fat file and of the collector file
    instance.c            context layout, linking, instantiation (S0014 step 7), calls, the
                          dispatcher, the services
    gc.c                  heap regions, type-info and function-type records, the GC services, the
                          walk, roots, the collector's driver (absent with WABOROS_WITH_GC=OFF)
    collector_wasm.c      collector.wasm as a byte array, committed, tied by a test (Decision 5)
    entry.c               the runtime's `entry` per architecture and convention as byte arrays
                          (ARTIFACT.md part 3 listings), mapped once per process
    os_posix.c            Linux and macOS: reserve/commit/protect/flush, MAP_JIT, PROT_BTI,
                          stack bounds, the signal handler
    os_windows.c          Windows: the same with VirtualAlloc, FlushInstructionCache, the
                          vectored handler, GetCurrentThreadStackLimits
    compiler.h            the seam (below)
  tools/waboros_run.c     the program: run an export, --dump, --check
  tests/loader/           ctest self-tests (C, no Python) and the libFuzzer target
tests/cloader/            the Python side (tests only): the ctypes shim and the S0001 engines
```
The loader does not depend on sljit, and a build without a compiler compiles no sljit source.

### CMake
The S0016 contract holds:
* `cmake_minimum_required(VERSION 3.21)`;
* embedding by `FetchContent_Declare` + `FetchContent_MakeAvailable` (`SOURCE_SUBDIR native`) or
  by `add_subdirectory`, never `FetchContent_Populate`;
* `target_*` properties only; `WABOROS_*` options; `waboros_*` targets with `waboros::` aliases;
* tests only when `PROJECT_IS_TOP_LEVEL` or `WABOROS_BUILD_TESTS`.

| option | default | meaning |
|---|---|---|
| `WABOROS_LOADER` | ON | build `waboros_loader` (`waboros::loader`, static) and `waboros_run` |
| `WABOROS_LOADER_SHARED` | `PROJECT_IS_TOP_LEVEL` | also a shared `waboros_loader_shared` for the ctypes shim (tests only; `WINDOWS_EXPORT_ALL_SYMBOLS` on Windows) |
| `WABOROS_WITH_COMPILER` | OFF until D68's compiler exists, then ON on x86-64 and aarch64 | link the embedded compiler through the seam; OFF: AOT files only |
| `WABOROS_WITH_GC` | ON | the GC heap, the GC services and the collector's driver; OFF: a file with `.waboros.context` `heap` 1 is refused at load |
| `WABOROS_GUARD` | ON on every row of "Rows" | the fault handler; OFF: a `BOUNDS_GUARD` file is refused at load (S0014 check 5) |
| `WABOROS_COLLECTOR_WBO` | empty | a path to the collector's `.wbo`, embedded as data with CMake's `file(READ … HEX)` (Decision 5) |
| `WABOROS_SLJIT` | `WABOROS_WITH_COMPILER` or a top-level build | build S0016's `waboros_sljit`; today the submodule check fails the configure in every case |

* Sanitizer and fuzzing flags come from the top-level command line (`CMAKE_C_FLAGS`), as any
  user's flags do. The project sets none.
* Compilers: GCC and Clang on Linux, Apple Clang on macOS, MSVC on Windows (the runners'
  defaults). Two things are confirmed in step 0, with clang-cl as the fallback: that CMake
  accepts `C_STANDARD 99` with MSVC, and that the loader builds in MSVC's C mode.

### C99 rules
* `C_STANDARD 99`, `C_EXTENSIONS OFF`, `_DEFAULT_SOURCE` on the target, as S0016 does. Warnings
  are `PRIVATE`: `-Wall -Wextra -Wconversion -Wvla` for GNU and Clang, `/W4` for MSVC.
* **No VLAs and no `alloca`.** Native stack belongs to wasm code (the headroom rule, S0015).
* **Reading bytes.** Every payload field is assembled from bytes (`rd_u32(p)`: shifts of
  `uint8_t`). The reader never casts a file pointer to a wider type, so alignment and strict
  aliasing cannot break it, and it runs on any host, a big-endian or 32-bit one included.
* **Arithmetic.** Sizes and offsets are `uint64_t`. Every count is checked against the bytes left
  before it is used or allocated (S0014, "Conventions"), and every sum is checked before it is
  formed. Signed overflow, shifts by the width and `long` (32 bits on Windows) do not appear.
* **Memory shared with code** (the context, value stack, linear memories, tables, GC heap) is
  accessed as `uint64_t` arrays or with `memcpy`, never through a pointer of another type.
* **The context is the last member of the instance**, a C99 flexible array `uint64_t ctx[]`. The
  dispatcher therefore finds its instance from `ctx` by subtracting `offsetof`.
* **The one implementation-defined step**: an address in executable memory becomes a function
  pointer (`entry`) by a cast from `uintptr_t`. POSIX and Windows both define it.
* **Atomic words.** `INTERRUPT`, written by any thread, is an aligned `volatile uint64_t` store,
  which is single-copy atomic on x86-64 and aarch64. C11 atomics wait for D58, as S0016 says.
* Every call into the library returns a status and fills a caller-provided `waboros_error`.
  Nothing aborts and nothing prints. "Never crash the interpreter" becomes "never crash the host
  process": a malformed file, a trap or a failed allocation is a status.

### C API (internal; Decision 1)
No stability promise: the API serves the tests and `waboros_run`, and it changes with them. A
product API is a later decision; this one is shaped so that a product can add to it without
removing anything.
```c
typedef struct waboros_engine waboros_engine;   /* per process: type ids, tag ids, trampolines, handler */
typedef struct waboros_store  waboros_store;    /* one thread at a time */
typedef struct waboros_module waboros_module;   /* a read artifact: metadata + code, not yet mapped */
typedef struct waboros_instance waboros_instance;
typedef int32_t (*waboros_host_fn)(void *env, uint64_t *slots);
typedef struct { int32_t code; uint8_t check; uint32_t trap; char msg[200]; } waboros_error;
/* code: 0 ok, 1 not ours, 2 refused, 3 link, 4 trap, 5 host error, 6 resource,
   7 needs a compiler, 8 unsupported build, 9 internal.
   check: the S0014 step (1-6) of a refusal (codes 1, 2, 7), else 0; msg names the section */

int waboros_engine_new(const waboros_compiler *c /* NULL: none */, waboros_engine **out, waboros_error *e);
int waboros_engine_set_collector(waboros_engine *, const uint8_t *wbo, size_t n, waboros_error *e);
int waboros_module_load(waboros_engine *, const uint8_t *b, size_t n, waboros_module **out, waboros_error *e);
int waboros_module_dump(const waboros_module *, char *buf, size_t n);   /* the canonical inventory */
int waboros_store_new(waboros_engine *, const waboros_store_config *, waboros_store **out, waboros_error *e);
int waboros_host_func(waboros_store *, waboros_host_fn f, void *env, uint32_t type_index_in /* of a module */,
                      const waboros_module *m, waboros_extern *out, waboros_error *e);
int waboros_instantiate(waboros_store *, waboros_module *, const waboros_extern *imports, size_t n,
                        waboros_instance **out, waboros_error *e);
int waboros_export(waboros_instance *, const char *name, size_t len, waboros_extern *out);
int waboros_call(waboros_store *, const waboros_extern *func, uint64_t *slots, size_t nslots, waboros_error *e);
void waboros_store_interrupt(waboros_store *);           /* any thread */
uint64_t waboros_extern_word(waboros_store *, void *host);  /* (h << 2) | 2, part 3 */
void    *waboros_extern_host(waboros_store *, uint64_t word);
int      waboros_exception(waboros_store *, uint64_t *tag_id, uint64_t *slots, size_t n); /* after status 14 */
```
* `waboros_extern` holds a kind and a pointer. A function is the address of its record, as
  everywhere in part 3; a host function is `(f, env, type)` until it is imported. Globals,
  memories and tables are the loader's objects.
* Values are part-3 slots (`frame[0..n)`): an i32 zero-extended, a v128 in two slots,
  references as words. `waboros_call` writes the results over the arguments.
* `waboros_store_config`: `max_wasm_stack` (default 512 KiB, Python's `DEFAULT_BUDGET`) and the
  value-stack size (1 MiB, `VSTACK_BYTES`).
* **Host calls (D75).** Every import word and every libcall and GC-service word in use holds the
  address of the one static function `int32_t dispatch(uint64_t *ctx, uint64_t *slots, uint32_t
  word)`. The bridge calls it in the host's convention, with `word` in `edx` (SysV), `r8d`
  (Win64) or `w2` (AAPCS64). It does what `Instance._host` does:
  1. finds the instance from `ctx`, and checks that `word` is an import, libcall or service word
     of its layout and that `ctx[word]` is `dispatch`. Otherwise it returns 13 with "host call
     with an invalid word": a file whose bridge predates D75 fails here instead of calling the
     wrong function;
  2. sets the store's next frame to `slots + max(n, m)` (the nested-entry rule, B1);
  3. calls the import's `f(env, slots)`, or the loader's own service for a libcall or a GC
     service;
  4. writes the status into `ctx[HOST_ERROR]` as its last action (D34): 0, a public trap code, or
     13 with the message kept in the store.
  A host function returns its status. It never returns 14, and a private status (bit 31) from it
  becomes 13. An import from another instance of the store is that instance's record, called
  directly with no dispatcher (D32).

### The compiler seam (`compiler.h`)
```c
typedef struct waboros_compiler {
    uint16_t e_machine;                  /* the loader's own architecture */
    const char *compiler;                /* its exact `compiler` string (S0014 header) */
    /* wasm bytes -> a thin v1 file; flags as the header's (BOUNDS_GUARD, GUARD_LOG2) */
    int  (*compile)(void *self, const uint8_t *wasm, size_t n, uint32_t flags,
                    uint8_t **out, size_t *out_n, waboros_error *e);
    void (*release)(void *self, uint8_t *out);
    void *self;
} waboros_compiler;
```
* **The output is a file.** The loader reads it with the same strict reader as any other file.
  So there is one path, and the seam is exactly what the wasm build of the same compiler exports
  (`compile(in_ptr, in_len) -> (out_ptr, out_len)`, MICRO-COMPILERS.md requirement 1). An
  in-memory handoff would be a second path, and it is not added without a measurement.
* The compiler validates the wasm itself. Its blob is the first copy for its compiler string,
  like any file's. `WABOROS_WITH_COMPILER=ON` makes the build's compiler the default of
  `waboros_engine_new`.
* Until D68 exists, the fat-file path is tested with a **test compiler**: a seam implementation in
  `tests/cloader/` that calls the Python compiler through ctypes callbacks. It runs in tests only.

### The GC collector: three builds (maintainer, 2026-11-24)
The collector is `runtime/gc/collector.wasm` (D54, D61), an ordinary module. It imports the
store's heap as `gc.heap` and exports `begin`, `roots`, `finish` and `is_subtype`.
1. **With the embedded compiler** (`WABOROS_WITH_COMPILER=ON`, `WABOROS_WITH_GC=ON`):
   * the library carries `collector.wasm` as the committed array `collector_wasm.c`. A pytest
     checks that the array equals the committed `.wasm` (D54's tie);
   * the collector is compiled with the embedded compiler on first need, once per engine and
     bounds mode, and kept;
   * "first need" is the Python runtime's (S0016, "Runtime and loader"): the instantiation of a
     module with `subtype` 1 (the `IS_SUBTYPE` record), or the first collection.
2. **Without a compiler** (`WABOROS_WITH_COMPILER=OFF`, `WABOROS_WITH_GC=ON`): the collector is
   **used only as an AOT file**:
   * the file is a `.wbo` of `collector.wasm` for the loader's architecture, from any compiler of
     that architecture (D68's wasm build on a PC; today `jit-x86_64`, `jit-aarch64` or
     `jit-sljit-*`);
   * it is supplied at run time with `waboros_engine_set_collector`, or at build time with
     `WABOROS_COLLECTOR_WBO=<path>` (embedded as data);
   * either way it goes through `waboros_module_load`: the same checks, and one copy per
     compiler string. Its `module_sha256` must equal the SHA-256 of the committed
     `collector.wasm` (computed from `collector_wasm.c` at startup), and its exports are checked;
   * **when a module needs the GC heap** (`.waboros.context` `heap` 1) **and no collector file is
     present**, `waboros_instantiate` refuses it, before anything of the instance is in the store,
     with code 8: "module needs the GC collector: none supplied (build without a compiler: supply
     its AOT file)". The refusal comes at instantiation, not at the first collection, so a program
     fails at once and not in the middle of a run. A module without the heap never needs the
     collector.
3. **Without GC** (`WABOROS_WITH_GC=OFF`):
   * there is no collector, no GC heap, no GC services and no collection;
   * a file with `heap` 1 is refused at load, with code 8: "built without GC";
   * this covers every module with GC types or instructions, a function-subtype
     `call_indirect`, a `throw`, or a `try_table` with `catch`/`catch_ref`, because exceptions
     live in the GC heap (S0013 step 6);
   * a module with only `throw_ref` and `catch_all(_ref)` has no heap and runs (ARTIFACT.md
     part 3).
   Whether this build stays is decided after the size measurement (Open question 1).

## Semantics

### What is contract and what is runtime-private
The C loader reproduces the **contract** exactly:
* the format: S0014 in full, including the order of checks and "Known limitations of v1";
* the context ABI: ARTIFACT.md part 3, D75's host-call amendment included;
* the boundary: S0015, including `entry`'s five steps, the walk interface and the guard handler
  contract;
* the heap's code contract: offsets 0-15, the formats of objects, type-info, function-type and
  exception records, reference words, RTT words.

Where part 3 leaves a choice to the runtime, the C loader copies the Python runtime's choice
wherever a spec command could see the difference:

| runtime-private | Python's choice (the oracle) | the C loader |
|---|---|---|
| memory placement | i32: 4 GiB + 32 MiB reserved, committed on grow, never moves; i64: a reservation, moved by a grow past it (D50; `instance.Memory`) | the same on every row |
| memory limits | 65 536 pages (i32), `MAX_PAGES64`; a failed commit makes `memory.grow` return −1 | the same |
| table storage, limit | element words reallocated on grow; at most 10 000 000 elements (`TABLE_LIMIT`, D27) | the same |
| value stack | 1 MiB per store (`VSTACK_BYTES`) | the same |
| native budget | `min(max_wasm_stack, half the stack left)`; 128 KiB when the bounds are unknown (`native_budget`); the bounds per OS as in `_stack_bounds` | the same, per row |
| `sp_stub` | assembled and called once per outermost entry | **none**: the address of a local in `waboros_call`, minus 1 KiB. The headroom rule (S0015) leaves more slack than that difference, and the budget is policy |
| `entry` | assembled per target (`entry.py`); each target seals its own copy | one copy per process and convention: the bytes of ARTIFACT.md's listings, tied to `entry.py` by a test; the walk's entry points are its return point |
| trampolines of a file | never executed: the runtime's own `conventions()` (S0014, "The waboros runtime") | **executed**: one copy per compiler string per engine, from the first file of that string; a later file's blob and record for the running convention compared byte for byte, the others only checked well-formed (S0014, "A foreign host") |
| type ids, tag ids | process-wide registries from 1 | per engine, from 1, below 2³² |
| GC heap regions | type area, tid map at 60 MiB, `ROOTS`, the handle report, semispaces from 64 MiB, committed apart (D69) | the same: `collector.wasm` reads them |
| host values | `(h << 2) \| 2`, handles from 1, lowest free number reused, at most 2³⁰ − 1; freed after a collection that did not report them | the same |
| interrupt | set in every context of the store; the outermost entry that returns 12 clears every context's word | the same |
| a lost collection | the store refuses every later call (D61) | the same |
| the guard handler | machine code emitted by the runtime (S0006) | a C function per row with the S0006 contract: which faults are ours, the PC change only, chaining to the previous action, `raise` for a sent signal (always on macOS), `EXCEPTION_CONTINUE_SEARCH` otherwise on Windows. Everything it reads is published after it is written, as in S0006's registry |

### Loading (S0014 checks 1-6)
1. The S0014 order ("Order of checks"), with the fat-file SHA-256 check placed as prerequisite P2
   settles it (Open question 3). Each refusal sets `check` to its step and names the field and
   both values in `msg`.
2. **Compatibility** (step 5), the foreign-host rule (S0014 Q3; D70):
   * `e_machine` is the host's;
   * `format_version` and `ctx_abi_version` are 1;
   * `isa_level` is 1 and `isa_features` is 0 (no feature bit is defined in v1, and the baseline
     needs no detection on any row);
   * every `flags` bit is known; `BOUNDS_GUARD` is accepted only with the row's handler installed
     and `GUARD_LOG2` at most 25;
   * `features_required` is 0;
   * the `compiler` string is well-formed (four non-empty tokens). Its value is not compared: it
     keys the trampoline copy.
3. **Thin and fat** (D18):
   * a thin file is refused at its first failing check;
   * a fat file that fails steps 2-6 is compiled from `.waboros.wasm` when the engine has a
     compiler for the host architecture;
   * without a compiler, such a fat file is refused with code 7 ("fat file cannot be used as is
     and this build has no compiler"; `check` the failing step);
   * a fat file that passes every check runs as a thin one does.
4. **Trampolines** (step 6, foreign host):
   * one copy per compiler string per engine, from the first file with that string, mapped
     executable under the row's W^X; on aarch64 the instruction cache is invalidated and the
     pages use `PROT_BTI` where available;
   * a later file must match the copy byte for byte for the running convention, else it is
     refused (a fat one is compiled, as in 3);
   * no copy is shared between compiler strings (D71).
5. A module is mapped executable at its first instantiation and shared by its instances. `.text`
   starts at a multiple of 4096 (`adrp`, part 3).

### Instantiation (S0014 step 7; D72)
The order of S0014 step 7 and of `JitInstance.__init__`:
1. imports matched by kind, type key and declared subtyping, limits and address type (as
   `link.py`); an import of another store is refused;
2. the context is written (part 3: the import, libcall and service words hold `dispatch`), and
   the store's sets of entry and restore points are extended;
3. guard registration (the code range with its fault map, the memory reservations);
4. RTT words and type-info records; function-type records for the module's non-final or subtyped
   function types; the `IS_SUBTYPE` record (the collector, path 1 or 2 above);
5. function records, signature words, tag identities;
6. globals in order, tables with their `init`, then every element segment's items;
7. active element segments in order (bounds, write, drop; declarative ones dropped);
8. active data segments in order (`MEMORY_OUT_OF_BOUNDS` past the memory; earlier writes stay);
9. the start function.

Constant expressions are evaluated on an explicit stack of 128-bit values (S0014, "Constant
expressions"). An allocation follows part 3 and never collects (D61's deviation). The six
libcalls and the seven GC services run in C behind `dispatch`. `table.init`, `memory.init`,
`elem.drop` and `data.drop` follow the specification's bounds and trap order. A trap during
instantiation leaves the instance's roots registered, as Python does (B6).

### Calls, traps, exceptions
* `waboros_call` follows `Instance.call`:
  * at the outermost entry it resets the value stack and computes `NSTACK_LIMIT` into every
    context of the store and the collector's;
  * it checks the value stack, writes the arguments, and calls `entry(ctx, frame, c_entry)`.
* A non-zero status becomes the error:
  * a trap code (1-18): code 4;
  * 12 `INTERRUPT`;
  * 13: the host's error;
  * 14: an uncaught exception. `waboros_exception` reads the tag identity and the payload slots
    from the object before any wasm runs;
  * anything else, a private status included, is an internal error (code 9).
* **Nesting.** A host function may call `waboros_call` again. The dispatcher's frame rule and
  `entry`'s saved block make this re-entrant (part 3).

### The walk, roots and a collection (`gc.c`)
* **The walk** follows `walk.py` and S0015's walk interface. It starts from the exit record and
  steps through each frame by its site's `ret_at`, `up` and `shift`. It crosses the fixed points:
  the entry's return point with its saved block, and every compiler's `restore` point with its
  two numbers. An unknown return address is an internal error, never a guess.
* **Roots**, as `collect.py` gathers them:
  * the `live` slots of every frame (the `refs` slots that `live` does not mark are nulled);
  * globals and tables of reference types of the any, extern and exn hierarchies;
  * passive element segments of those types;
  * the pending exception word;
  * the handle table.
* **The collection.** The roots go to `ROOTS` in batches through `begin`, `roots(n)` and
  `finish`, on the collector's context, whose `INTERRUPT` stays 0. The store is marked lost from
  the first call until the heap's words are consistent again. Host values that the handle report
  does not mark are freed.
* `WABOROS_JIT_WALK_CHECK=1` makes the C loader walk at every host call and libcall, as the
  Python runtime does.

### Executable memory and the handler (`os_posix.c`, `os_windows.c`)
* **Executable memory.** Code is written read-write, then made read-execute (W^X, R2).
  * Linux: `mprotect`; on aarch64 `__builtin___clear_cache`, and `PROT_BTI` when `AT_HWCAP2` has
    BTI.
  * macOS arm64: `MAP_JIT`, with `pthread_jit_write_protect_np(0)` and `(1)` around each write on
    the writing thread, then `sys_icache_invalidate`. A signed binary would need the
    `allow-jit` entitlement; the CI binaries are not signed.
  * Windows: `VirtualAlloc` and `VirtualProtect`, then `FlushInstructionCache`.
  * Reservations are `PROT_NONE` (`MEM_RESERVE`), committed by `mprotect` (`MEM_COMMIT`).
* **The guard handler** (S0006's contract) is installed at the first guard file and checked at
  every entry, as `guard.check()` does.
  * Linux: `SIGSEGV` with `SA_SIGINFO | SA_ONSTACK` and `si_code > 0`; the PC from
    `uc_mcontext` (`gregs[REG_RIP]`, or `pc` on aarch64); then the fault map and the
    reservations; then the PC moved to `code + resume_offset`, or the fault chained.
  * macOS: `SIGSEGV` and `SIGBUS`; the PC from `uc_mcontext->__ss.__rip` (or `__pc`); `raise`
    after chaining.
  * Windows: a vectored handler, first in the list, for `EXCEPTION_ACCESS_VIOLATION`; the PC
    from `ContextRecord->Rip` (or `->Pc`); `EXCEPTION_CONTINUE_EXECUTION` or `_SEARCH`.
* Another copy of waboros in the process (the Python runtime beside the shim) chains the same way
  (S0006, "Chain and claim"), so either may be installed first.

## Test plan

### The harness (Decision 3)
* **The shim** (`tests/cloader/`) loads `waboros_loader_shared` with ctypes. It registers the
  S0001 engines `cloader-<target>` for each JIT target of the host (sljit ones where
  `build/native` has them), as `tests/sljit` registers its target (S0016). An engine:
  * `load` compiles the module with `<target>`, writes the file (`artifact.write`, fat), and calls
    `waboros_module_load`;
  * `instantiate`, `invoke` and `get` call the C API;
  * host functions (`spectest` and the runner's own) are ctypes callbacks of type
    `waboros_host_fn` with an `env` handle.
  * `tests.spec.run` runs each `.wast` in its own child, so a crash of the C loader costs one
    file, and `spec_diff` works unchanged.
  * Expected failures are its target's, copied to `tests/spec/xfail/cloader-<target>.txt`
    (parity, not incompleteness).
  * `cloader-mixed-<arch>` (Linux glibc) compiles modules alternately with `jit-<arch>` and
    `jit-sljit-<arch>` into one store, as `tests/jit/test_mixed.py` does.
* **The refusal corpus** (`tests/cloader/test_corpus.py`) runs in-process through the shim. It
  builds, as bytes:
  * every whole-file case of `tests/artifact/test_refuse.py`, `test_strict.py` and
    `test_review.py`;
  * every truncation of the D75 small files and of a GC module;
  * 3 000 byte flips each of CoreMark, a GC module and the small module.
  Each file is checked in a store that already holds the unmutated file of its compiler string,
  so the foreign-host compare and Python's `conventions()` compare see the same reference.
  `artifact.load` and `waboros_module_load` must agree on each file as Decision 6 says. A file
  that Python refuses with `cannot load: …` fails the test as a Python defect. The files are also
  written to a directory as the fuzzer's seeds.
* **`waboros_run`** (C, no Python in the process): `waboros_run FILE.wbo EXPORT [ARGS…]
  [--wiring] [--collector F.wbo] [--expect-trap N]`, and `--dump` / `--check FILE…`. It runs
  under ASan and UBSan, which also covers the guard handler with no Python handler beside it.
  The `.wbo` files it runs are written earlier in the same job by `tools/artifact_dump.py`.
* **Later, not in this spec**: a C wast runner, with the embedded compiler, so that compile and
  run are tested with no Python at all.

### Acceptance commands
1. `cmake -S native -B build/native && cmake --build build/native && ctest --test-dir
   build/native` gives 0 failures on all six rows. ctest covers:
   * the reader on the committed files;
   * the entry bytes, an add and a trap through `entry`;
   * the handler's chaining (a foreign fault, a sent signal);
   * the consumer check (`add_subdirectory` and `FetchContent`) with `waboros::loader`.
2. The same with `-DWABOROS_SLJIT=OFF` and no submodule checked out builds the loader.
3. The spec suite, every case at parity, with `WABOROS_JIT_WALK_CHECK=1`:
   * Linux glibc x86-64: `uv run python -m tests.spec.run --engine cloader-jit-x86_64`, the same
     for `cloader-jit-sljit-x86_64` and `cloader-mixed-x86_64`, guard and explicit, each meeting
     its expected-failures file;
   * `uv run python tools/spec_diff.py jit-x86_64 cloader-jit-x86_64` both ways gives no
     difference (the Python side with `WABOROS_JIT_ARTIFACT=1`); likewise each sljit engine
     against its target;
   * `gc/` and `exceptions/` also with S0013's GC switches;
   * Linux aarch64 (native): the same for the aarch64 engines;
   * macOS and Windows, both architectures: `cloader-jit-<arch>` in guard and explicit mode,
     against its expected-failures file.
4. The refusal corpus: `uv run pytest tests/cloader/test_corpus.py` gives 0 disagreements on
   Linux x86-64 and aarch64 (the reader is OS-independent; one run per architecture is enough).
5. **The committed files**, through the shim and through `waboros_run`:
   * the D75 set (P1) is read and run: its host import is called with the right word; its
     exports work;
   * `v1-small` uses a GC struct global (`heap` 1), so the no-compiler build runs it only with the
     collector file supplied, and refuses it without one (the message of path 2);
   * the `v1-s15-*` files (pre-D75 bridges) are read, and their exports run; they make no host
     call, so the dispatcher's check is not reached. A later test of the D75 set covers that
     check with a forged old bridge;
   * `v1-small-<arch>-thin.wbo` (pre-S0015) is refused at step 6, on the size of
     `.waboros.trampolines`;
   * the fat `v1-small-<arch>.wbo` gets code 7 without a compiler, and is compiled and run with
     the test compiler;
   * a copy with `EI_ABIVERSION` 2 is refused at step 2.
6. **The three GC builds**:
   * with `WABOROS_WITH_GC=OFF`: a `heap` 1 file is refused at load (code 8), and a module with
     only `throw_ref` runs;
   * without a compiler and without a collector file: a `heap` 1 module is refused at
     instantiation (code 8, path 2's message), and the store stays usable;
   * with the collector supplied at run time, and again embedded with `WABOROS_COLLECTOR_WBO`:
     `gc/` runs with the GC switches;
   * a collector file with a wrong `module_sha256` is refused;
   * the pytest checks that `collector_wasm.c` equals `collector.wasm`.
7. **CoreMark through a file**: `waboros_run core.<target>.wbo setup --wiring` (`millis` and
   `print` in C) passes its self-check on every row. Its time is recorded, alternated with
   `tools/run_coremark_jit.py` on the same file (BENCHMARKS.md).
8. **Sanitizers**, on Linux x86-64: ctest and `waboros_run` (CoreMark, the committed files, a
   trap per trap code from `tests/cloader/traps.wat`) built with `-DCMAKE_C_FLAGS="-fsanitize=
   address,undefined -fno-sanitize-recover=all"`, in explicit mode. In guard mode the run uses
   `ASAN_OPTIONS=handle_segv=0`, so that ASan does not take the handler's faults.
9. **Fuzzing the reader**: `tests/loader/fuzz_read.c` is a libFuzzer target over
   `waboros_module_load` (no instantiation), seeded from item 4's directory. It runs five
   minutes in `fuzz-nightly.yml`, never blocking; a crash is kept as a run artifact.
10. **Build sizes** ("Measurements to take") recorded in BENCHMARKS.md.
11. `python3 tools/check_docs.py`.

## Plan of steps
Every step stops and reports if it needs more than its size. P1 changes a contract (D75): it gets
a `reviewer` after the jit-engineer. Every other step changes no contract. A divergence from
Python is a bug in one of the two loaders, settled by the S0014/S0015 text; if the text does not
settle it, the text is amended first (the working freeze: a spec or a decision, no bump).

| step | goal | agent | acceptance | size (lines) |
|---|---|---|---|---|
| P1 | **D75 implementation.** The bridges of `jit-x86_64`, `jit-aarch64`, `jit-sljit-x86_64` and `jit-sljit-aarch64` pass the word (`edx`/`r8d`/`w2`) in every convention. ARTIFACT.md part 3 ("Host functions", a fixed per-architecture row), S0015's bridge rows, S0003's `host` prototype, D34/D32 markers. A test of the word for each import, each libcall and a nested entry on every JIT target. The new committed v1 set with a host import; `test_old.py`: the `v1-s15` thin files refused by their bridge bytes, the fat ones regenerated | jit-engineer, then reviewer | the spec suite at parity on the four targets in-process and through files, both modes; the D35 lists change only in the bridges; `pytest -q` | src ~60, tests ~150, docs ~40 |
| P2 | **Refusal steps.** `ArtifactError.step` set at the phase boundaries of `load`/`read`. The fat-file SHA-256 check placed (Open question 3) and S0014 amended to match. Every corpus case refused without the `cannot load:` path | implementer | `tests/artifact/` with a step asserted per refusal case; `pytest -q` | src ~30, tests ~60 |
| 0 | CMake: `WABOROS_SLJIT`, `WABOROS_LOADER`, `WABOROS_LOADER_SHARED`, an empty loader and `waboros_run`, the consumer check; `native/` configures without the submodule; MSVC confirmed; a `cloader` CI job on the six runners (build and ctest) | implementer | items 1-2 on all six rows | cmake ~80, CI ~70 |
| 1 | The reader (`read.c`, `sha256.c`), `--dump`/`--check`, the shim's reader part, the corpus test | implementer | item 4 on both architectures | C ~1 100, Python ~250 |
| 2 | Linux: executable memory, the `entry` bytes tied to `entry.py`, the trampoline copies, calls without imports; the `cloader-<target>` engines | jit-engineer | the MVP directories through `cloader-jit-x86_64` and `cloader-jit-sljit-x86_64`, explicit | C ~800, Python ~250 |
| 3 | Instantiation and linking complete: `dispatch` (D75; needs P1), records, segments, libcalls, memory64, interrupts, stack limits, exceptions without the heap | implementer | item 3 (Linux x86-64, explicit) without `gc/` and the heap-using `exceptions/`; `spec_diff` empty | C ~900 |
| 4 | The guard handler on Linux | jit-engineer | item 3 in guard mode; the chaining ctests | C ~250 |
| 5 | GC: the heap, records, services, walk, roots, the collector driver, `collector_wasm.c`; the three GC builds | jit-engineer | items 3 (`gc/`, `exceptions/`, GC switches, walk check) and 6 | C ~1 100 |
| 6 | Linux aarch64 (native, QEMU with BTI), mixed stores, the committed files, CoreMark, sanitizers, fuzz job | runner + jit-engineer | items 3, 5, 7, 8, 9 on Linux, both architectures | C ~150, CI ~120 |
| 6.1 | Build sizes: four builds × `-Os`/`-O2` × x86-64/aarch64 × LTO on/off, stripped text + data, plus the collector's thin `.wbo` | runner | a BENCHMARKS.md section; Open question 1 answered from it | tools ~40 |
| 7 | macOS x86-64 and arm64: the Apple parts of `os_posix.c` (`MAP_JIT` and write protection, `sys_icache_invalidate`, `SIGBUS`, stack bounds) | jit-engineer | items 1, 3 (`cloader-jit-<arch>`, both modes), 5 and 7 on both macOS runners | C ~250, CI ~30 |
| 8 | Windows x64 and ARM64: `os_windows.c`, the vectored handler, the Win64 `entry` bytes (first run from C), the file's `win64` blob executed for the first time | jit-engineer | items 1, 3, 5 and 7 on both Windows runners | C ~400, CMake ~30, CI ~30 |
| 9 | Docs ("Docs this changes"), DECISIONS entry for the implemented loader | orchestrator | `python3 tools/check_docs.py` | — |
| 10 | Review without the implementers' reasoning: the reader against S0014, the trap order of instantiation, the dispatcher, the handlers per row, the walk and the collector driver, C99 conformance | reviewer | findings closed | — |

The order is P1 and P2 first (Python side, no C yet), then 0-6 on Linux, 6.1, then 7 and 8. P1
may run in parallel with 0-2 but must be done before 3.

In total: about 4 950 lines of C, 500 of Python, 110 of CMake and 250 of CI, plus P1 and P2. This
is an estimate scaled from the Python sizes: `artifact.py` 1 184, `instance.py` 1 374, and
`heap.py`, `collect.py`, `walk.py`, `guard.py` and `execmem.py` together about 1 460. C needs
more lines per rule, but loses Python's ctypes plumbing.

## Measurements to take
* Load time (read and checks) and instantiation time against Python's, for CoreMark and a GC
  module (BENCHMARKS.md, "Instantiation from an artifact", has Python's).
* CoreMark through the C loader against the Python loader on the same file, alternated, on each
  row. No difference is expected (the same code bytes); a difference points to a context or
  memory-placement discrepancy.
* The cost of a host call through `dispatch`: the import loop of STATIC-BUILDS.md ("Cheap host
  calls"), a C host against a Python host.
* **Build sizes** (step 6.1; the maintainer's question, Open question 1).
  * The stripped size (text + data, `size` after `strip`) of a static `waboros_run` linked with
    the loader, in four builds: full (compiler and GC), no compiler, no GC, neither.
  * Each at `-Os` (MinSizeRel) and `-O2`, on x86-64 and aarch64, with LTO
    (`CMAKE_INTERPROCEDURAL_OPTIMIZATION`) on and off.
  * Also the collector's AOT file (thin `.wbo`, each architecture, both bounds modes).
  * Recorded in BENCHMARKS.md with the compiler version. Until D68's compiler exists, the "full"
    and "no GC" rows use S0016's `waboros_sljit` as a stand-in for the compiler's sljit part and
    say so; the C core is added when it exists.
  * The orchestrator's inputs (2026-11-24; sljit 39c508d1, gcc 13, the full sljit API built as
    the shared library):
    * sljit text: 56 KB on x86-64 and 47 KB on aarch64 at MinSizeRel `-Os`; 90 and 80 KB at
      `-O2`;
    * `collector.wasm`, 1 517 bytes, gives a thin `.wbo` of 11.4 KB (x86-64) and 11.1 KB
      (aarch64), of which code is 3.7 and 3.6 KB;
    * CoreMark, 19.5 KB of wasm, gives a thin `.wbo` of 53 KB and 48 KB.
  * For scale, Python line counts:
    * the front end: 1 962;
    * the JIT's common core (compiler, lower, liveness, backend, simd): about 1 760;
    * the sljit backend (both architectures): 1 728;
    * the loader and runtime side: about 4 000.
    So the compiler is expected to be sljit plus a C port of about 5 400 Python lines, against
    about 4 000 for the loader.

## Risks
* **Two implementations of instantiation** (D72's accepted risk). Conformance covers it: items
  3-5. RUNTIME-MODULES.md item 4 names where re-implementations go wrong: the order of traps.
* **The handlers** (R6): four mechanisms on six rows (Linux and macOS signals with different
  signals and PC fields, Windows VEH), and a second handler beside Python's in the shim process.
  S0006's chaining is the contract; ctest checks both install orders, and `waboros_run` runs
  guard traps with no Python handler.
* **Executing a foreign blob** is new on every row: Python never runs a file's trampolines. Steps
  2, 7 and 8 are their first runs on Linux, macOS and Windows. The Win64 blob and the Win64
  `entry` from C have never been executed.
* **The walk and the collector** run across foreign frames: a wrong site number corrupts the heap
  silently. The walk check and poison (S0013) run in item 3.
* **Hidden Python behaviour**: a choice in `instance.py` that is neither contract nor listed
  above shows up as a `spec_diff` line. It goes into the table, or into part 3 if it is contract.
* **MSVC's C**: if the C99 subset does not build, clang-cl is the fallback (step 0).

## Alternatives considered
* **Reuse WAMR (wasm-micro-runtime) pieces**: its AOT format, context and calling convention are
  its own ([WAMR-INTEROP.md](../WAMR-INTEROP.md)). Only its platform layer could be reused, and
  that brings a licence and a build of its own, for about 600 lines of `os_*.c`. Not taken.
* **A loader in Rust or Zig**: D60 chose C.
* **C generated from the Python loader**: the Python loader is ctypes over Python objects, and a
  generator would be larger than the loader. D36 asks for a loader written from the text.
* **Instantiation as a wasm runtime module**: rejected by D72.
* **`entry` as `.S` files**: these need `enable_language(ASM)`, and MASM syntax on Windows.
  Byte arrays tied to `entry.py` need no assembler, and ARTIFACT.md part 1 already plans hex
  bytes of the listings.
* **Harness options**: a C line server under the Python runner, or a standalone C wast runner
  now. Decision 3 chose the shim plus `waboros_run`; the C wast runner comes with the compiler.
* **Host calls without D75**:
  * a pool of preprocessor-made dispatchers (`WABOROS_HOST_SLOTS`);
  * per-import machine-code thunks;
  * the raw part-3 signature.
  Rejected by Decision 4: D75 gives one dispatcher, no limit, and no machine code of the
  loader's own.
* **Mach exception ports on macOS**: Wasmtime's default; not taken (see "Rows").
* **A separate C runtime for the no-compiler build**: one library with options instead. The seam
  is a pointer that may be NULL.

## Open questions
1. **Are the no-compiler and no-GC builds worth having?** The maintainer decided both options;
   this asks only whether to keep them once their size is known. Decided **after step 6.1's
   measurement**.
   * **No compiler**: kept unless the measurement shows it removes little. It is expected to
     remove sljit (47-90 KB of text) plus the C core. It is also the build for devices that run
     only cross-compiled AOT files.
   * **No GC**: free to provide only if `WABOROS_WITH_GC=OFF` is just the omission of `gc.c` and
     a refusal at load. Its cost is one CI build row and the `#if`s at the service words and the
     walk. Its gain is `gc.c` (about 1 100 lines expected) and the collector's 11 KB file.
   * The options:
     * (a) keep both;
     * (b) keep no-compiler, and drop no-GC if it saves less than an agreed share (for example
       10 % of the no-compiler build);
     * (c) drop both.
   * Until the decision, the plan builds no-GC as the omission of one file and nothing more. If
     it turns out to need more, the option waits.
2. **Linux musl and Android**: rows of this spec, or later? Both are verified rows of S0006 for
   the Python runtime (x86-64). For the C loader, musl would come under D47 (c): the same
   `os_posix.c`, a container step. Android would need bionic's `libsigchain` and an emulator cell.
   Not decided.
3. **Where the fat-file SHA-256 check stands in S0014's order** (prerequisite P2).
   `artifact.load` checks a fat file's wasm against `module_sha256` after step 1 and before
   step 2, so a corrupt fat file is refused before anything else; S0014 names the check only
   inside the fat fallback.
   * (a) S0014 names it as step 1b, as the code does: a docs change only.
   * (b) The code moves it into the fallback: a fat file with a corrupt wasm that passes steps
     2-6 would then run.
   * Recommendation: **(a)**. A corrupt file is refused at once, and the C reader reports
     `check` 1 for it.

## Docs this changes
When approved and implemented:
* By P1 (D75):
  * ARTIFACT.md part 3: "Host functions" (`host(ctx, slots, word)`) and a fixed row of the
    per-architecture table;
  * [S0015](S0015-boundary-convention.md): the bridge rows;
  * [S0003](S0003-jit-context-abi.md): the `host` prototype;
  * DECISIONS.md D34 and D32: markers;
  * `tests/artifact/data/README.md`: the D75 set, and the `v1-s15` files' new expectation.
* By P2: [S0014](S0014-artifact-format.md) "Order of checks": step 1b (Open question 3), and
  `ArtifactError.step` named in "Semantics: the loader".
* [ARTIFACT.md](../ARTIFACT.md):
  * part 1: "Planned: hex bytes of each listing" points to `native/loader/entry.c`;
  * part 2: "A loader refuses … whose `compiler` string is not the store's" names the waboros
    loader, and the foreign-host rule of S0014 is added beside it;
  * "Versioning rules": the last bullet names this spec.
* [S0016](S0016-sljit-on-the-boundary.md) "Where it lives": a marker that "no generated sources"
  reads "no generator outside CMake" (Decision 5), and that `native/` configures without the
  submodule when sljit is not needed.
* [S0006](S0006-guard-pages.md): the row table gains the C loader's handler on its six rows.
* [DECISIONS.md](../DECISIONS.md): D36's marker exists; an entry when the loader is implemented
  (the rows, the harness, the GC builds, the answer to Open question 1).
* [TESTING.md](../TESTING.md): the `cloader-*` engines, the refusal corpus, `waboros_run` under
  sanitizers, the fuzz job.
* [TARGETS.md](../TARGETS.md): the C loader's rows.
* [ROADMAP.md](../ROADMAP.md): "Next" (6)(b) done.
* [MICRO-COMPILERS.md](../MICRO-COMPILERS.md): "The C variant" links the seam and the collector's
  three paths.
* [BENCHMARKS.md](../BENCHMARKS.md): the measurements above.
* CLAUDE.md "Commands": the loader's build, its engines and `waboros_run`.
* [LESSONS.md](../LESSONS.md): any trap found on the way.

## Changes
* 2026-11-24: created (draft).
* 2026-11-24: the three paths of the GC collector stated explicitly (maintainer's clarification):
  compiled on first need with the embedded compiler; only an AOT file without one, with a refusal
  at instantiation when none is present; no collector and no heap without GC.
* 2026-11-24: build sizes of the four builds as a measurement (step 6.1), with the orchestrator's
  inputs; Q7: whether the no-compiler and no-GC builds stay is decided after it.
* 2026-11-24: Q1 decided (maintainer): a test tool for now with an internal API; the C loader is a
  future product, not yet fixed. Options were (a) test tool, (b) product with a stable API.
* 2026-11-24: Q2 decided (maintainer): (b), macOS and Windows before the final freeze; the
  recommendation was (a), Linux glibc only. Open: Linux musl and Android.
* 2026-11-24: Q3 decided (maintainer): ctypes for the spec suite and the refusal corpus, plus
  `waboros_run` under sanitizers without Python; the C wast runner later, with the embedded
  compiler. The draft recommended a C line server (`--serve`) under the Python runner.
* 2026-11-24: Q4 decided (maintainer): D75, the bridge passes the import's context-word index.
  The draft recommended a pool of dispatchers; per-import thunks and the raw signature were the
  other options.
* 2026-11-24: Q5 decided (maintainer): a committed array for `collector.wasm`, CMake-generated for
  a `.wbo`, run-time supply kept.
* 2026-11-24: Q6 decided (maintainer): (a) plus the S0014 step of each refusal, and no refusal
  through Python's internal-exception path.
* 2026-11-25: the spec revised to the decisions:
  * a "Decisions" list;
  * Rows for the six rows, with D47 evidence;
  * `os_posix.c` and `os_windows.c`;
  * the internal C API with `dispatch` (D75) and `waboros_error.check`;
  * the harness rewritten (shim engines, corpus in-process, `waboros_run` under sanitizers);
  * the plan with prerequisites P1 (D75) and P2 (refusal steps and the fat-file SHA-256
    position) and steps 7 (macOS) and 8 (Windows);
  * the committed-file expectations (D75 set; `v1-small` needs the collector);
  * the open items reduced to the size question, musl and Android, and the SHA-256 position.
