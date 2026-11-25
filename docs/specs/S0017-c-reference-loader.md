---
title: The C reference loader
kind: spec
id: S0017
status: draft
superseded_by:
related: [D18, D23, D32, D34, D36, D47, D50, D54, D56, D58, D60, D61, D62, D64, D65, D66, D68, D69, D70, D71, D72, D73, D74, D75, R2, R3, R6, S0001, S0003, S0006, S0013, S0014, S0015, S0016]
owner: maintainer
updated: 2026-11-25
---

# S0017 The C reference loader

## Summary
A loader and runtime for JIT artifacts written in C99 and built with CMake alone (D36, D60;
ROADMAP.md "Next" (6)(b)). It reads format v1 ([S0014](S0014-artifact-format.md)) with the same
checks as the Python reader, and instantiates a module from the file alone. It runs the module
under context ABI v1 with the boundary of [S0015](S0015-boundary-convention.md): its own
`entry`, host functions behind each compiler's bridge (with D75's word argument), traps,
interrupts, stack limits, the frame walk, the GC heap with the collector module, exceptions, and
stores that hold code of several compilers.

The Python loader is the oracle: `targets/jit/common/artifact.py`, `instance.py` and the runtime
services.

There are two builds: one embeds a compiler for its own architecture, the other has none and runs
only AOT files. This spec defines the loader and the **seam** where a compiler plugs in. It does
not define the compiler: D68's micro-compiler does not exist yet (ROADMAP "Next" (6)(c)). The C
loader is the second condition of the final freeze (ARTIFACT.md, Status; D73's amendments).

The maintainer fixed the following before this spec (2026-11-23 and 2026-11-24; ROADMAP "Next"
(6), [MICRO-COMPILERS.md](../MICRO-COMPILERS.md) "The C variant", D72), and it does not reopen
them: C99 and CMake only, no Python to build, under `native/` with S0016's CMake contract; two
builds, with and without an embedded compiler; the GC collector stays a wasm module, and a build
option leaves GC out; instantiation and the segment libcalls in C from the specification (D72);
artifacts architecture-dependent and OS-independent (D23), with nothing blocking 32-bit or MCU
targets; the oracle: the spec suite through files and the refusal corpus agree on both loaders, and
files of both compilers load.

## Decisions
Taken by the maintainer on 2026-11-24 on this draft's questions. The options and their costs are
in "Alternatives considered"; the dates are in "Changes".
1. **What it is**: a test tool for now, with an internal C API: no stability promise, no
   `find_package`, no install. The same sources become the C variant and the product by a later
   decision (D36's marker).
2. **Rows**: Linux glibc, macOS and Windows, on x86-64 and aarch64 (macOS x86-64 and arm64,
   Windows x64 and ARM64), all before the final freeze; Linux musl and Android join by Decision 8. The D47 exception for each is in "Rows".
3. **Harness**: the spec suite and the refusal corpus drive the C loader in-process through a thin
   ctypes shim (the S0001 engines `cloader-<target>`); `waboros_run` (load a file, call an export)
   runs CoreMark and the committed files under ASan/UBSan with no Python in the process; a C wast
   runner comes later, with the embedded compiler.
4. **Host calls** (D75): the bridge passes the context-word index, so host functions are called
   as `host(ctx, slots, word)`. The C loader has one static dispatcher, and the embedder's host
   function is `int32_t f(void *env, uint64_t *slots)`. D75 is implemented first (prerequisite
   P1).
5. **Data in the build**: `collector.wasm` as a committed `.c` array tied to the `.wasm` by a
   test; a collector `.wbo` through CMake's `file(READ … HEX)`; run-time supply stays; S0016's "no
   generated sources" reads "no generator outside CMake".
6. **Refusal agreement**: per corpus file, accept or refuse; for a refused file the S0014 step
   (1-6; for step 6 also the section); for an accepted file the inventory dump. The exceptions are the ones listed in "Designed exceptions to Decision 6". No corpus file may
   be refused through the Python loader's internal-exception path (`cannot load: …` in
   `artifact.load`). The step is carried by `ArtifactError.step` and `.section` in Python
   (prerequisite P2), and by `waboros_error.check` and `.section` in C.

Taken on 2026-11-25 on the revised draft's questions:
7. **Pre-D75 files** (former Open question 1, option (a')): no format bit. The only pre-D75 files
   are our own (the committed fixtures and per-run temporaries; no release and no cache wrote
   any). P1 regenerates the committed v1 set with `tools/make_v1_artifacts.py` (x86-64 and
   aarch64, guard and explicit, thin and fat) and adds a module with a host import that pins the
   D75 bridge. One or two pre-D75 files stay only for the Python loader's refusal of the old
   bridge bytes. The C loader requires files written after D75.
8. **Linux musl and Android are rows** (former Open question 2): musl on x86-64 and aarch64 under
   D47 (c), through `os_posix.c` and a container step; Android on x86-64 and arm64, with bionic's
   `libsigchain` for the handler beside ART and an emulator cell, as the Python runtime's S0006
   rows ("Rows").
9. **The fat-file SHA-256 check is S0014's step 1b** (former Open question 4, (a)): as the code
   does, a docs change in P2.
10. **No guard handler on musl aarch64 and Android arm64** (maintainer, 2026-11-25): as the
    oracle (`guard_rows` of `jit-aarch64`), the C loader refuses guard files there at step 5 and
    runs explicit files; Decision 8's `libsigchain` applies to the rows that have a handler. A
    handler on these rows would have to come to the Python runtime too, so it is a v2 candidate
    for both loaders (ROADMAP "Phase 8"), not a C-only exception.
11. **Handles given to the host are released** (maintainer, 2026-11-25; replaces S0013 Q10's
    "until phase 6" for both loaders): the Python runtime frees a handle when its `GcRef` or
    `ExnRef` is no longer referenced (a weak entry and a finalizer, the slot reused after the
    next safe point), and the C loader by an explicit `waboros_ref_release(store, handle)`. A
    released object is collected unless something else roots it; a held handle keeps its identity.
    Scoped handles (Wasmtime's `RootScope`) stay in phase 6. Implemented first in the Python
    runtime (its own step, before P1), then here.

## Goals / Non-goals
* Goals:
  * the static library `waboros_loader` and the program `waboros_run`, built from the
    repository's C sources with a C99 compiler and CMake 3.21, as a top-level project or a
    subproject, on every row of "Rows";
  * a strict reader that accepts and refuses what `artifact.load` does, at the same S0014 step,
    apart from the designed exceptions;
  * instantiation, calls, host functions, traps, interrupts, stack limits, GC and exceptions that
    agree with the Python runtime on the spec suite through files:
    * `jit-x86_64` and `jit-aarch64` files on every row;
    * `jit-sljit-*` files and mixed stores on Linux glibc (D74: the sljit targets' only row);
  * the runtime scenarios of "Runtime tests" and the committed files ("Test plan", item 5);
  * the compiler seam, specified so that D68's micro-compiler plugs in without a change to the
    loader.
* What "reference" means: correctness and clarity before speed. Each rule cites the S0014, S0015
  or ARTIFACT.md section it implements. There is no fast path without a measurement (CLAUDE.md,
  "Measure, do not estimate").
* Non-goals:
  * a product: a stable C ABI, packaging, install, `find_package` (Decision 1);
  * the public API of phase 6;
  * host functions that no module imports (Wasmtime's `Func::new`): phase 6, through D75's shim
    module, which a build without a compiler gets as one more AOT file;
  * WASI, and any host-function library beyond what the tests need;
  * the compiler (D68), the compiler `.wasm`, cross-compilation, a C wast runner;
  * static builds (D62, [STATIC-BUILDS.md](../STATIC-BUILDS.md));
  * threads: words 6-9 of the libcall block stay zero (D58), and a shared memory is refused;
  * fuel and epochs: words 3 and 4 stay 0, since v1 has no flag for them;
  * `isa_level` above 1 (as the Python loader, S0014 "Known limitations of v1");
  * 32-bit hosts and MCUs: nothing here blocks them ("C99 rules"), but none is built.

### Rows (D47)
Per-OS code here means executable memory and W^X, address-space reservations, stack bounds, locks,
and the fault handler. The first row is Linux glibc x86-64.
* **Executable memory, reservations, stack bounds and locks** on every other OS come under
  **exception (a)**: without them the loader cannot run there at all.
* **The guard handler** on macOS and Windows comes under **exception (d)**: the maintainer's
  Decision 2 asks for these rows, for parity with the Python runtime's verified guard rows
  (S0006). Without the handler, guard files are refused and explicit files run: there is no crash,
  so (a) does not apply.
* **Guard rows are per architecture, as the oracle's `guard_rows`**: `jit-x86_64` has linux-glibc,
  linux-musl, darwin, win32 and android; `jit-aarch64` has linux-glibc, darwin and win32. On Linux
  musl aarch64 and Android arm64 the C loader has no handler and refuses guard files at step 5.
  On a listed row the handler is ON only once item 3 in guard mode has passed in CI on that row
  (D47 rule 3); until then `WABOROS_GUARD` defaults to OFF there.
* The DECISIONS.md record that D47 (d) asks for ("the decision record says so") is written when
  this spec is approved.
* **Linux musl** comes under **exception (c)**: it reuses `os_posix.c` with no line of its own
  beyond the libc's headers, needs no new CI infrastructure (the `python:3.13-alpine` container of
  the `jit` job gains a step; an Alpine container on the `arm64` runner), and adds no interaction
  class. S0006's musl step is the evidence for the handler (the same `sigaction` path, musl 1.2.6).
* **Android** comes under **exception (d)** (Decision 8): the same `os_posix.c`, but the handler
  must be installed through libc's `sigaction` inside an app, where ART's libsigchain interposes
  it, and it needs the NDK build and the Android cell.
* aarch64 is not a D47 row; its differences are per architecture: cache flush, BTI, the PC field.

| row | per-OS code | exception | CI |
|---|---|---|---|
| Linux glibc x86-64 | `os_posix.c`: `mmap`/`mprotect`, `pthread_mutex`, `pthread_getattr_np`, `sigaction(SIGSEGV)`; on the main thread of every Linux row, the top of `[stack]` in `/proc/self/maps` minus `RLIMIT_STACK` (`_stack_bounds`) | the first row | `ci.yml` `cloader` (new), `ubuntu-latest` |
| Linux glibc aarch64 | the same file; `__builtin___clear_cache`, `PROT_BTI` when `AT_HWCAP2` has BTI, the PC from `uc_mcontext.pc` | per architecture | `arm64` native, `jit` (QEMU `-cpu max`, BTI) |
| macOS x86-64 | `os_posix.c` (Apple parts): stack bounds from `pthread_get_stackaddr_np`/`_stacksize_np` and the main thread's rlimit; the handler on `SIGSEGV` and `SIGBUS` | (a) for memory and stack bounds (`pthread_getattr_np` does not exist); (d) for the handler | `platforms.yml` `macos-26-intel` |
| macOS arm64 | as above, plus `MAP_JIT` with `pthread_jit_write_protect_np` around every code write, `sys_icache_invalidate`, the PC by `arm_thread_state64_get_pc` | (a): Apple silicon refuses executable pages without `MAP_JIT` (R2); (d) for the handler | `platforms.yml` `macos-26` |
| Windows x64 | `os_windows.c`: `VirtualAlloc`/`VirtualProtect`/`VirtualFree`, `SRWLOCK`, `GetCurrentThreadStackLimits`, the Win64 `entry`; the vectored handler with CET continuation targets | (a) for memory, locks, stack bounds and `entry` (no `mmap`; a Windows host calls in Win64); (d) for the handler | `platforms.yml` `windows-latest` |
| Windows ARM64 | the same file, `FlushInstructionCache`, the PC from `ContextRecord->Pc` | as Windows x64 | `platforms.yml` `windows-11-arm` |
| Linux musl x86-64 | `os_posix.c` unchanged; the Linux main-thread stack rule matters here too: musl's `pthread_getattr_np` reports only the mapped part of the main stack | (c) | `ci.yml` `jit`, `python:3.13-alpine` container (`apk add build-base cmake`) |
| Linux musl aarch64 | the same; the instruction-cache flush is `__builtin___clear_cache`, which the toolchain resolves at **link time** (libgcc's or compiler-rt's `__clear_cache`, linked statically into the program). S0006's musl aarch64 limit was a **run-time** lookup by the Python runtime in an image without `libgcc_s`; a ctest checks that the flush is present and that code written twice runs the second version. No handler (not an oracle guard row): explicit files only | (c) | `ci.yml` `arm64`, an Alpine container |
| Android x86-64 | `os_posix.c` with bionic: the handler installed through libc's `sigaction` (never a raw syscall), so in an app process libsigchain makes it a user handler called after ART's (S0006 step 5); `broken` also covers the child-process reset that S0006 documents (a `vfork` child sets the handled signals to `SIG_DFL`) | (d) | `platforms.yml` Android cell, x86_64 emulator |
| Android arm64 | the same, with `__builtin___clear_cache`; no handler (not an oracle guard row): explicit files only | (d) | built in the Android cell (NDK cross build); run on an arm64 emulator where the cell's runner has one, else build-only until a runner does: step 9 reports which |

* macOS uses signals, as the Python runtime does (S0006 step 3), not Mach exception ports.
* **What is new on Windows.** The code that ARTIFACT.md's Win64 listing matches already runs on
  the Windows runners in-process: it is the Python runtime's `entry`. So are the bytes of the
  `win64` bridge blob: the runtime assembles its own copy. Three things are new: a copy of a
  file's blob executed as found in the file, the C `entry` bytes, and the dispatcher's call in
  Win64.
* Before step 8, the Win64 `entry` bytes and a file's `win64` bridge with `dispatch` run on Linux
  through an `ms_abi` C function, as `tests/sljit/test_win64_bridge.py` does for the sljit blob
  ("Runtime tests").

## Interface

### Where it lives
```
native/
  CMakeLists.txt          restructured (step 0) + the loader's options and targets
  loader/
    waboros.h             the only public header: the internal C API
    read.c                ELF container and payloads, S0014 steps 1-6, cexpr parse and types,
                          the frozen lookup, the inventory dump
    sha256.c              module_sha256 of a fat file and of the collector file
    instance.c            context layout, linking, instantiation, calls, `dispatch`, services
    gc.c                  heap, type-info and function-type records, GC services, the walk, roots,
                          the collector's driver (absent with WABOROS_WITH_GC=OFF)
    collector_wasm.c      collector.wasm as a committed byte array (Decision 5)
    entry.c               the runtime's `entry` and `sp_stub` per architecture and convention as
                          byte arrays, tied to `entry.py` by a test
    os_posix.c            Linux and macOS: memory, locks, stack bounds, the signal handler
    os_windows.c          Windows: memory, locks, stack bounds, the vectored handler
    compiler.h            the seam
  tools/waboros_run.c     the program: run an export, --dump, --check
  tests/loader/           ctest self-tests (C, no Python) and the libFuzzer target
tests/cloader/            the Python side (tests only): the shim, the engines, the corpus, the
                          Python dumper, `make_files.py`
```
The loader does not depend on sljit. A build without a compiler compiles no sljit source.

### CMake
**Step 0 restructures `native/CMakeLists.txt`.** Today a missing submodule is a `FATAL_ERROR`
whatever is built, and `WABOROS_BUILD_TESTS` requires `WABOROS_SLJIT_SHARED`. After step 0, the
sljit part and its tests are behind `WABOROS_SLJIT`. S0016's contract holds:
* CMake 3.21;
* embedding by `FetchContent_Declare` + `FetchContent_MakeAvailable` (`SOURCE_SUBDIR native`) or
  by `add_subdirectory`, never `FetchContent_Populate`;
* `target_*` properties only; `WABOROS_*` options; `waboros_*` targets with `waboros::` aliases;
* tests only when `PROJECT_IS_TOP_LEVEL` or `WABOROS_BUILD_TESTS`.

| option | default | meaning |
|---|---|---|
| `WABOROS_LOADER` | ON | `waboros_loader` (`waboros::loader`, static) and `waboros_run` |
| `WABOROS_LOADER_SHARED` | `PROJECT_IS_TOP_LEVEL` | `waboros_loader_shared` for the shim (tests; `WINDOWS_EXPORT_ALL_SYMBOLS` on Windows) |
| `WABOROS_WITH_COMPILER` | OFF until D68's compiler exists | link the embedded compiler through the seam |
| `WABOROS_WITH_GC` | ON | the heap, the GC services, the collector's driver; OFF: `heap` 1 files refused at load |
| `WABOROS_GUARD` | ON on the guard rows verified in CI ("Rows") | the fault handler; OFF: `BOUNDS_GUARD` files refused at step 5 |
| `WABOROS_COLLECTOR_WBO` | empty | a collector `.wbo` embedded with `file(READ … HEX)` |
| `WABOROS_SLJIT` | `WABOROS_WITH_COMPILER` or a top-level build | S0016's `waboros_sljit` and its tests |

* Sanitizer and fuzzing flags come from the top-level command line (`CMAKE_C_FLAGS`).
* Compilers: GCC and Clang on Linux (musl: Alpine's GCC), Apple Clang, MSVC on Windows (the
  runners' defaults), and the NDK's Clang through CMake's Android toolchain file (step 9).
  Step 0 confirms that CMake accepts `C_STANDARD 99` with MSVC, with clang-cl as the fallback.

### C99 rules
* `C_STANDARD 99`, `C_EXTENSIONS OFF`, `_DEFAULT_SOURCE`, as S0016. `os_posix.c` alone defines
  `_GNU_SOURCE` before its first include, for `Dl_info`/`dladdr`, `pthread_getattr_np` and
  `REG_RIP`. Warnings are `PRIVATE`:
  `-Wall -Wextra -Wconversion -Wvla` (GNU, Clang) and `/W4` (MSVC).
* No VLAs and no `alloca`: native stack belongs to wasm code (the headroom rule, S0015).
* Every payload field is assembled from bytes (`rd_u32(p)`), never read by casting a file pointer,
  so alignment and strict aliasing cannot break the reader, and it runs on any host.
* Sizes and offsets are `uint64_t`. Every count is checked against the bytes left before use
  (S0014, "Conventions"), and every sum before it is formed. There is no signed overflow, no
  shift by the width, and no `long` (32 bits on Windows).
* Memory shared with code (the context, value stack, memories, tables, heap) is accessed as
  `uint64_t` arrays or with `memcpy`.
* The context is the instance's last member, a flexible array `uint64_t ctx[]`, so `dispatch`
  finds its instance by subtracting `offsetof`.
* One implementation-defined step: an executable address becomes a function pointer by a cast from
  `uintptr_t`. POSIX and Windows define it.
* **Locks** (`os_*.c`: `pthread_mutex_t` or `SRWLOCK`, behind three functions):
  * one per process: the handler's installation, the non-chaining images, and the guard
    registry's writers, since one handler and its registry serve every engine. The handler reads
    the registry with no lock: an entry is published after it is written, and retired with `hi`
    zeroed first (S0006, "Registry");
  * one per engine: the type-id and tag-id registries and the trampoline copies;
  * one per store, as Python's `interrupt_lock`. `waboros_store_interrupt` takes it to walk the
    instance array (which instantiation grows under the same lock), and the outermost entry takes
    it to clear the word.
* **One thread per store** (Python's `StoreState.enter`/`claim`):
  * the owner's thread id is recorded on the first entry, under the engine lock (the claim is
    atomic);
  * an entry, a host-side grow or table operation (`waboros_memory_grow`, `waboros_table_*`) or
    an instantiation from another thread returns `WABOROS_E_THREAD` before anything changes;
  * nesting is allowed;
  * a claim outside an entry (instantiation, a host-side grow) is released, so a store built on
    one thread may run on another.
* `INTERRUPT` (any thread) is an aligned `volatile uint64_t` store, single-copy atomic on x86-64
  and aarch64. C11 atomics wait for D58.
* Every call returns a status and fills a `waboros_error`. Nothing aborts and nothing prints. A
  malformed file, a trap, a wrong value or a failed allocation is a status, never a crash of the
  host process.

### C API (internal; Decision 1)
The API serves the tests and `waboros_run` and changes with them. It is shaped so that a product
can add to it without removing anything.
```c
typedef struct waboros_engine waboros_engine;   /* registries, trampoline copies, handler, locks */
typedef struct waboros_store  waboros_store;    /* one thread at a time */
typedef struct waboros_module waboros_module;   /* a read artifact */
typedef struct waboros_instance waboros_instance;
typedef int32_t (*waboros_host_fn)(void *env, uint64_t *slots);
enum waboros_code {        /* never a trap number: the trap is in `trap` */
  WABOROS_OK, WABOROS_E_NOT_OURS, WABOROS_E_REFUSED, WABOROS_E_NEEDS_COMPILER,
  WABOROS_E_NO_GC_BUILD, WABOROS_E_NO_COLLECTOR, WABOROS_E_LINK, WABOROS_E_TYPE,
  WABOROS_E_ARG, WABOROS_E_THREAD, WABOROS_E_TRAP, WABOROS_E_INTERRUPT, WABOROS_E_EXCEPTION,
  WABOROS_E_HOST, WABOROS_E_LOST, WABOROS_E_RESOURCE, WABOROS_E_INTERNAL };
typedef struct {
  int32_t code;            /* enum waboros_code */
  char check[4];           /* E_NOT_OURS, E_REFUSED, E_NEEDS_COMPILER: the S0014 step as
                              Python's ArtifactError.step: "1", "1b", "2" ... "6" */
  char section[24];        /* step 6: the payload's section name */
  uint32_t trap;           /* E_TRAP: the trap code 1-18 */
  uint64_t tag;            /* E_EXCEPTION: the tag identity */
  uint32_t npayload;       /* E_EXCEPTION: payload slots, kept in the store until the next call */
  char msg[200];
} waboros_error;

int waboros_engine_new(const waboros_compiler *c /* NULL: none */, waboros_engine **, waboros_error *);
int waboros_engine_set_collector(waboros_engine *, const uint8_t *wbo, size_t n, waboros_error *);
int waboros_module_load(waboros_engine *, const uint8_t *b, size_t n, waboros_module **, waboros_error *);
int waboros_module_dump(const waboros_module *, char *buf, size_t *n);
int waboros_store_new(waboros_engine *, const waboros_store_config *, waboros_store **, waboros_error *);
/* host-made externs (spectest's, a host's own) */
int waboros_host_func(waboros_store *, waboros_host_fn, void *env, const waboros_functype *, waboros_extern *, waboros_error *);
int waboros_memory_new(waboros_store *, uint64_t min, uint64_t max /* ~0: none */, int a64, waboros_extern *, waboros_error *);
int waboros_table_new(waboros_store *, const waboros_reftype *, uint64_t min, uint64_t max, uint64_t init, waboros_extern *, waboros_error *);
int waboros_global_new(waboros_store *, const waboros_valtype *, int mut, const uint64_t *v, waboros_extern *, waboros_error *);
int waboros_global_get(waboros_store *, const waboros_extern *, uint64_t *v /* 1 or 2 slots */);
int waboros_global_set(waboros_store *, const waboros_extern *, const uint64_t *v, waboros_error *);
int waboros_memory_data(waboros_store *, const waboros_extern *, uint8_t **base, uint64_t *size);
int waboros_instantiate(waboros_store *, waboros_module *, const waboros_extern *imports, size_t n,
                        waboros_instance **, waboros_error *);
int waboros_export(waboros_instance *, const char *name, size_t len, waboros_extern *);
int waboros_call(waboros_store *, const waboros_extern *func, uint64_t *slots, size_t nslots, waboros_error *);
int waboros_exception_payload(waboros_store *, uint64_t *slots, size_t n);
void waboros_store_interrupt(waboros_store *);              /* any thread */
/* references held by the host (the store's handle table, part 3) */
uint64_t waboros_extern_word(waboros_store *, void *host);  /* (h << 2) | 2 */
void    *waboros_extern_host(waboros_store *, uint64_t word);
uint64_t waboros_rooted(waboros_store *, uint64_t handle);  /* its object's current word */
int      waboros_ref_kind(waboros_store *, uint64_t handle); /* WABOROS_REF_STRUCT, _ARRAY, _I31, _EXN; -1 */
void     waboros_ref_release(waboros_store *, uint64_t handle); /* Decision 11; unknown: no effect */
/* host-side operations: the one-thread rule, a lost store refused */
int waboros_memory_grow(waboros_store *, const waboros_extern *, uint64_t delta, uint64_t *old, waboros_error *);
int waboros_table_grow(waboros_store *, const waboros_extern *, uint64_t delta, uint64_t init, uint64_t *old, waboros_error *);
int waboros_table_get(waboros_store *, const waboros_extern *, uint64_t i, uint64_t *v, waboros_error *);
int waboros_table_set(waboros_store *, const waboros_extern *, uint64_t i, uint64_t v, waboros_error *);
void waboros_host_message(waboros_store *, const char *msg); /* in a host function: its status 13's text */
int  waboros_guard_nonchaining(const void *addr);   /* process-wide: the image holding addr never chains */
```
* **Values** are part-3 slots: an i32 is zero-extended, a v128 takes two slots.
* **References at the API** (arguments, results, globals, tables, a host function's slots), as
  `_StoreStacks.from_raw`/`to_raw`:
  * a funcref is a record address;
  * a host value is a host word `(h << 2) | 2` from `waboros_extern_word`;
  * an i31 of the any hierarchy is its word `(v << 1) | 1` (Python gives an int);
  * any other non-null reference of the any, extern or exn hierarchy (a struct, an array, an
    exception, an i31 seen as an externref) is a **handle** `n << 2`, n from 1. One handle per
    object, as Python's `gcrefs`: the C store (`instance.c`, not the shim) maps each word to its
    handle, gives the same handle each time the object crosses, and remaps after every collection
    (`collect.py`, B5). A handle lives until `waboros_ref_release` (Decision 11); its slot is
    then reused, and the object, if nothing else roots it, is collected. `waboros_ref_kind` gives a handle's kind for the spec runner's
    `NON_NULL` check; a host word is told by its low bits.
  * `waboros_call`, `waboros_global_set` and `waboros_table_set` check every reference before
    anything runs, as `to_raw` does: a record of this store with a matching type, a live host
    word, an i31 in range for a type that takes one, or a handle of this store whose object fits
    the type. Otherwise they return `WABOROS_E_TYPE`.
* `waboros_call` requires `nslots >= max(nparams, nresults)` (two slots per v128), else
  `WABOROS_E_ARG`, and writes the results over the arguments.
* **The exception at the failing entry**: when an entry returns 14, `waboros_call` reads the tag
  identity and the payload from the object at once, before any wasm or collection runs, as
  `_uncaught` does. References in the payload are rooted as handles. The values are kept in the
  store until the next call.
* `waboros_store_config`: `max_wasm_stack` (default 512 KiB, `DEFAULT_BUDGET`) and the value-stack
  size (1 MiB, `VSTACK_BYTES`).

### Host calls (D75)
Every import word and every libcall and GC-service word in use holds the address of one static
function, `int32_t dispatch(uint64_t *ctx, uint64_t *slots, uint32_t word)`. The bridge calls it
in the host's convention, with `word` in `edx` (SysV), `r8d` (Win64) or `w2` (AAPCS64). It does
what `Instance._host` and its callback do (`instance.py`, `_host`):
1. Find the instance from `ctx`. **Best-effort check**: `word` must be an import, libcall or
   service word of the instance's layout, and `ctx[word]` must be `dispatch`. Otherwise it returns
   13: "host call with an invalid word".
   * This cannot reliably catch a pre-D75 bridge. Such a bridge leaves a live value in the
     third-argument register: on aarch64 `w2` is a pool register, on SysV it is the low half of
     `slots`, on Win64 `r8` is the exit record. Since every such word of a context holds
     `dispatch`, a stale value inside the range passes the check and calls the wrong function.
   * **So the C loader requires files written after D75** (Decision 7). No format bit tells them
     apart: the only pre-D75 files are our own fixtures, kept for the Python loader's refusal
     test, and no C test loads them. The check stays as a defence against a corrupt file.
2. Save the store's next frame and set it to `slots + 8 · max(n, m)` (the nested-entry rule, B1).
3. Call the import's `f(env, slots)`, or the loader's own service. For an import whose type has
   references (`_ref_host`), the reference arguments are first put in their API form ("C API";
   a word that is no reference of the store gives 13). On status 0, each reference result is
   checked as `to_raw` checks a `waboros_call` argument and put back as its word; a failed check
   gives 13 with the reason.
4. On status 0, mask each result to its type (`_masks`: i32 and f32 to 32 bits), so slots stay
   zero-extended.
5. Map the status:
   * 0 and the trap codes 1-13 and 15-18 pass (Python's `_TRAP_STATUSES`: every `TrapCode` but
     14);
   * any other value (14, above 18, the private range) becomes 13, with the message "host
     function returned status N" kept in the store (S0015: a host never returns 14, and no
     private status leaves a bridge).
6. When the status is 0 and the store runs guard code: check that the handler chain is still
   intact ("The guard handler", rule `broken`). A broken chain gives 13 and the S0006 message.
7. When the status is still 0 and the store is lost (a nested call's collection failed and the
   host went on, D61): 13 with the lost message, so wasm never resumes on a half-moved heap
   (`callback`, instance.py). There is no check before the call, as in Python: a store becomes
   lost only inside a call, every entry refuses a lost store, and this check stops the return.
8. Restore the store's next frame. As the last action, write the status to `ctx[HOST_ERROR]`
   (D34).

* The text of a 13 is the one the host set with `waboros_host_message` during the call, else
  "host function returned status 13"; `waboros_call` returns it in `msg` (truncated).
* **D34 at the C boundary**: the status is `f`'s return value and `dispatch` writes it, so a host
  that never writes its status cannot exist in C. The hazard moves to the shim: an exception that
  escapes a ctypes callback makes it return 0. **Shim rule**: each callback catches every
  exception (`BaseException`) and returns 13 with `waboros_host_message`. This is a deviation from
  `test_a_host_that_never_writes_its_status_fails`, which maps to a shim test.
* An import from another instance of the store is that instance's record, called directly (D32).
* With `WABOROS_JIT_WALK_CHECK=1`, `dispatch` first runs `walk.check`'s assertions ("The walk,
  roots and a collection"), as `_checked` does.

### The compiler seam (`compiler.h`)
```c
typedef struct waboros_compiler {
    uint16_t e_machine;                  /* the loader's own architecture */
    const char *compiler;                /* its exact `compiler` string (S0014 header) */
    const char *target;                  /* its target token, added to the known targets */
    int  (*compile)(void *self, const uint8_t *wasm, size_t n, uint32_t flags,
                    uint8_t **out, size_t *out_n, waboros_error *e);   /* -> a thin v1 file */
    void (*release)(void *self, uint8_t *out);
    void *self;
} waboros_compiler;
```
* **The output is a file**, and the loader reads it with the same strict reader. This is the one
  path, and the same shape as the wasm build's export (`compile(in_ptr, in_len) -> (out_ptr,
  out_len)`, MICRO-COMPILERS.md requirement 1). An in-memory handoff would be a second path and is
  not added without a measurement.
* The compiler validates the wasm itself. Its file's blob becomes its compiler string's copy, as
  any file's does.
* **The test compiler** (`tests/cloader/`): a seam implementation whose `compile` is a ctypes
  callback into the Python compiler of a given target. It is used by the shim engines and the
  corpus until D68 exists.

### The GC collector: three builds (maintainer, 2026-11-24)
The collector is `runtime/gc/collector.wasm` (D54, D61). It imports the store's heap as `gc.heap`
and exports `begin`, `roots`, `finish` and `is_subtype`.
1. **With the embedded compiler** (`WABOROS_WITH_COMPILER=ON`, `WABOROS_WITH_GC=ON`):
   * the library carries `collector.wasm` as the committed array `collector_wasm.c`; a pytest
     checks that the array equals the `.wasm` (D54's tie);
   * the collector is compiled on first need, once per engine and bounds mode, and kept. "First
     need" is the Python runtime's (S0016, "Runtime and loader"): the instantiation of a module
     with `subtype` 1, or the first collection.
2. **Without a compiler** (`WABOROS_WITH_COMPILER=OFF`, `WABOROS_WITH_GC=ON`): the collector is
   **used only as an AOT file**.
   * The file is a `.wbo` of `collector.wasm` for the loader's architecture, from any compiler of
     it: D68's wasm build on a PC, or today `jit-x86_64`, `jit-aarch64` or `jit-sljit-*`.
   * It is supplied by `waboros_engine_set_collector`, or at build time by
     `WABOROS_COLLECTOR_WBO`.
   * It goes through `waboros_module_load`. Its `module_sha256` must equal the SHA-256 of
     `collector_wasm.c`'s bytes (the array is in this build too, for that hash), and its exports
     are checked.
   * **When a module has `heap` 1 and no collector file is present**, `waboros_instantiate`
     refuses it before anything of the instance is in the store: `WABOROS_E_NO_COLLECTOR`,
     "module needs the GC collector: none supplied (build without a compiler: supply its AOT
     file)". The refusal comes at instantiation, not at the first collection. A module without the
     heap never needs the collector.
3. **Without GC** (`WABOROS_WITH_GC=OFF`): no collector, no heap, no GC services, no collection.
   * A file with `heap` 1 is refused at load: `WABOROS_E_NO_GC_BUILD`, "built without GC".
   * This covers GC types and instructions, a function-subtype `call_indirect`, `throw`, and
     `try_table` with `catch`/`catch_ref`, since exceptions live in the heap (S0013 step 6).
   * A module with only `throw_ref` and `catch_all(_ref)` has no heap and runs (ARTIFACT.md
     part 3).
   * Whether this build stays is Open question 1.

## Semantics

### What is contract and what is runtime-private
The C loader reproduces the **contract** exactly:
* S0014 in full: the order of checks and "Known limitations of v1" included;
* ARTIFACT.md part 3, with D75;
* S0015: `entry`'s five steps, the walk interface, the guard handler contract, and M5 of its
  "Compatibility" (a C entry consumes only its own private status; it holds by construction,
  since a C entry calls only its own module's bodies);
* the heap's code contract.

Where part 3 leaves a choice to the runtime, the C loader copies Python's choice wherever a spec
command or a runtime test could see the difference:

| runtime-private | Python's choice (the oracle) | the C loader |
|---|---|---|
| memory placement | i32: 4 GiB + 32 MiB reserved, committed on grow, never moves; i64: a reservation, moved by a grow past it (D50; `instance.Memory`) | the same on every row |
| memory limits | 65 536 pages (i32), `MAX_PAGES64`; a failed commit makes `memory.grow` return −1 | the same |
| tables | element words reallocated on grow; growth capped at 10 000 000 elements (`TABLE_LIMIT`, D27); a minimum that cannot be allocated is out of memory | the same |
| value stack | 1 MiB per store | the same |
| native budget | `min(max_wasm_stack, half the stack left)`; 128 KiB when the bounds are unknown or implausible (`native_budget`); bounds per OS (`_stack_bounds`) | the same, per row |
| the stack pointer at the first entry | `sp_stub`, a C entry that writes the stack pointer to `frame[0]`, called once per outermost entry | **the same stub bytes**, tied to `entry.py`. The address of a local is wrong under ASan (its fake stack keeps locals on the heap); `__builtin_frame_address` and `_AddressOfReturnAddress` would be two compiler-specific paths |
| `entry` | assembled per target; each target seals its own copy | one copy per process and convention (ARTIFACT.md listings); the walk's entry point is its return point |
| trampolines of a file | never executed: the runtime's `conventions()` | executed: one copy per compiler string **per engine**, shared by its stores ("Loading") |
| type ids, tag ids | process-wide from 1; a function type id below 2²⁰ where the heap's tid map holds it (`gc.TIDS`; `heap.py`: "GC type area exhausted") | per engine from 1, with the same 2²⁰ bound |
| GC heap regions | type area, tid map at 60 MiB (ids below 2²⁰), `ROOTS`, the handle report, semispaces from 64 MiB, committed apart (D69) | the same: `collector.wasm` reads them |
| GC heap growth (`heap.room`, `_grow`) | the first allocation grows; when an allocation does not fit, collect; if more than half of a half is then live, grow, collecting first when the upper half is active; capacity `max(2·cap, 2·S0, 4·(live + r), 2·(cursor − OBJ + r))` rounded to the half unit, capped at `CAP_MAX`; instantiation only grows | the same rule and numbers |
| host values | `(h << 2) \| 2`, from 1, the lowest free number reused, at most 2³⁰ − 1 | the same |
| interrupt | set in every context in `store.instances`, which excludes the collector's; the outermost entry that returns 12 clears every one | the same |
| a lost collection | every later call refused (D61) | `WABOROS_E_LOST` |
| the guard handler | machine code emitted by the runtime | a C function per row ("The guard handler") |

### Loading
**Order** (S0014 "Order of checks", as `artifact.load` runs it):
1. Identification. Otherwise `WABOROS_E_NOT_OURS`, check 1.
2. **The frozen lookup and the fat-file SHA-256**, as `_frozen` does (S0014 step 1b,
   Decision 9):
   * read `e_shoff`, `e_shnum` and `e_shstrndx`. Return "none" (the file counts as thin here) if
     the table, a section, the name table or a name's terminator is outside the file, or if
     `e_shstrndx` is out of range;
   * the names are matched without the layout rules, and **the last** section named
     `.waboros.header` or `.waboros.wasm` wins;
   * no header section means "none";
   * `module_sha256` is the first 32 bytes of the header, or fewer when it is shorter;
   * when `.waboros.wasm` exists and its SHA-256 differs from those bytes, the file is refused:
     ".waboros.wasm: its SHA-256 is not module_sha256 (corrupt file)", check "1b" (P2).
3. `format_version` is 1 (step 2).
4. The container (step 3): every rule of `_sections_r`, including a refusal of an `e_machine` other
   than 62 and 183.
5. The header (step 4), as `_head`:
   * the payload is exact, the reserved fields are zero;
   * the compiler string is "Compiler string, step 4" below;
   * the target is known and matches `e_machine`.
6. Compatibility (step 5):
   * `e_machine` is the host's;
   * `ctx_abi_version` is 1;
   * the compiler string is "Compiler string, step 5" below;
   * `isa_level` is 1 and `isa_features` is 0;
   * `flags` has no bit but 0 and 8-15, and `BOUNDS_GUARD` and a non-zero `GUARD_LOG2` are set
     together;
   * a guard file needs, in `_compatible`'s order, `GUARD_LOG2` of at most 25, a row among its
     architecture's guard rows with the handler built (`WABOROS_GUARD`) and installed, and its
     chain not broken;
   * `features_required` is 0.
7. The payloads (step 6), each with `_R`'s exactness. A failure sets `section`. Strings are
   strict UTF-8 as Python's `bytes.decode()` (`_R.text`): no overlong form, no surrogate
   code point, nothing above U+10FFFF.
8. Only when every payload has passed is the file's trampoline copy adopted ("Trampolines").

**Thin and fat** (D18):
* a thin file is refused at its first failing check;
* a fat file that fails steps 2-6 is compiled from `.waboros.wasm` by the engine's compiler; with
  no compiler it gets `WABOROS_E_NEEDS_COMPILER`, with `check` the failing step;
* a fat file that passes runs as a thin one.

**Compiler string** (S0014 Q3: "Foreign hosts check the contract fields and the target only"):
* **Step 4**, the same as `_head`:
  * ASCII, exactly four tokens separated by single spaces, none empty;
  * the first token is `waboros`;
  * the fourth is digits only, at most 9;
  * the third (the target) is a **known target** whose `e_machine` is the file's.
  The known targets are a build constant: `jit-x86_64` and `jit-sljit-x86_64` (62), `jit-aarch64`
  and `jit-sljit-aarch64` (183), and the embedded compiler's `target`. Python's set is the
  targets registered in its process (`artifact.targets()`): `tests/sljit` registers only the
  host's sljit target, only on Linux, only with the library built. A file of a sljit target that
  Python has not registered is refused by Python at step 4; C refuses the other architecture's at
  step 5 (`e_machine`) and, off Linux, accepts the host architecture's. The corpus writes only
  targets registered in Python's process, and a byte flip cannot turn one target token into
  another known one, so no corpus file meets this; one that does fails outside its class.
* **Step 5**: no further check. The version token (and so the source hash of a development build)
  is not compared: this is the foreign-host rule, and the first designed exception below.

**Trampolines** (S0014, "One copy per store", "A foreign host"; S0015 "Compatibility"):
* **Per engine, one copy per compiler string**, shared by the engine's stores (D64 item 5 allows
  one copy per compiler, shared by stores). A store's sets record the entry point and the
  `restore` points of the copies its instances use.
* The records are checked where Python checks them: in `read`, while reading `.waboros.funcs`
  (after `data_start` and `start`, before its entries), each convention in format order. So a file
  that fails both the trampolines and a later payload is refused on `.waboros.trampolines` by
  both loaders.
* The copy is adopted from the first file of its string, and only after the whole file has passed
  steps 1-6. It is mapped under the row's W^X, with the instruction cache invalidated and
  `PROT_BTI` on aarch64.
* A later file of the string must equal the copy byte for byte for the **running** convention:
  `size`, blob, offsets relative to `start`, the two restore numbers. The **other** conventions
  are only checked well-formed: every offset inside its blob, placement and zero gaps as
  `_trampolines` checks, no `0xFFFFFFFF`. A mismatch is a refusal at step 6, section
  `.waboros.trampolines`.
* Copies are never shared between compiler strings, even with identical bytes (D71): two strings
  get two copies and two `HOST_BRIDGE` addresses.

**Mapping.** A module is mapped executable at its first instantiation and shared by its instances.
`.text` starts at a multiple of 4096.

### Designed exceptions to Decision 6
Each corpus file gets one class, computed from Python's verdict. Every class but `agree` is a
deliberate difference, named here:
* **`agree`**: both loaders give the same verdict and step (and section). For an accepted file,
  the dumps are equal.
* **`E1 compiler`**, a thin file: Python refuses at step 5 on the `compiler` field (its
  exact-string rule, `_compatible`), and C accepts, because the string passed step 4. Examples:
  * the thin half of `test_refuse.py::test_other_compiler_target_and_version`'s `version` case
    (`both`, lines 105-115);
  * a byte flip inside the version token that keeps it ASCII without a space.
  The C dump is compared with the Python dump of `artifact.read(b)`, which applies no step-5 rule.
* **`E2 other convention`**, a thin file: Python refuses at step 6, `.waboros.trampolines`,
  because a non-running convention's blob or record differs from `conventions()`. C accepts,
  because it checks that convention only for being well-formed. `read(b)` refuses too, so the C
  dump is compared with the Python dump of the case's source file (the unmutated file it was made
  from), except the `trampolines` lines of the non-running conventions. A change in the running
  convention, or a malformed record in any convention, is `agree`.
* **`E1-fat`, `E2-fat`**: the same files with `.waboros.wasm`. Python compiles them
  (`artifact.load` compiles a fat file on any failure of steps 2-6), and C accepts them as thin,
  with or without a compiler. The dumps are compared as for E1 and E2.
* **`fat`**: a fat file that fails steps 2-6 for both loaders, at the same step (Python's is the
  `ArtifactError.step` that `load` catches before it compiles). Python compiles it; its dump is `dump(read(write(c)))` of the compiled result `c` (a thin file written by the
  same process). C with the test compiler compiles it too, and its dump must be equal; C without
  a compiler gives `WABOROS_E_NEEDS_COMPILER` with that step as `check`. The corpus runs both
  ways. Example: `test_review.py::test_compiler_string_tokens` writes fat files whose strings
  fail step 4 (a space, a long fourth token).
* A file Python refuses with `cannot load: …` fails the corpus as a Python defect (Decision 6).

### The inventory dump
`waboros_module_dump` (C) and `tests/cloader/dump.py` (Python, from `artifact.read`'s
`ModuleView`) print the same text. Format:
* UTF-8, one item per line, `\n` at the end of each line;
* fields separated by one space;
* integers in decimal; keys, hashes and byte strings in lowercase hex;
* strings in double quotes with `\\`, `\"` and `\xNN` for bytes outside 0x20-0x7E;
* `-` for an absent value;
* the order of S0014's sections, and within each the file's order.
```
header <module_sha256> <config_sha256> abi=<n> isa=<n> flags=<n> isa_features=<n> required=<n> optional=<n> compiler=<str>
machine <n>
type <i> group=<g> final=<0|1> super=<i|-> <comp> key=<hex>
import <i> <str module> <str name> <func|table|memory|global|tag> <desc>
func <i> type=<t> code=<offset>
start <i|-> data_start=<offset>
table <i> <valtype> <limits> init=<cexpr|->
memory <i> <limits>
global <i> <valtype> mut=<0|1> init=<cexpr>
tag <i> type=<t>
export <str> <kind> <i>
elem <i> <active|passive|declarative> <valtype> table=<t|-> offset=<cexpr|-> <funcs|exprs> <items>
data <i> <active|passive> memory=<m|-> offset=<cexpr|-> len=<n> sha256=<hex>
context heap=<0|1> subtype=<0|1> records=<list> sigs=<list> thunks=<list> rtts=<list> thrown=<list>
stackmaps sha256=<hex of the payload> sites=<n> maps=<n>
traps <n> <fault>:<resume> ...
trampolines <conv> start=<n> size=<n> bridge=<n> tail_switch=<n> restore=<n> ret_at=<n> up=<n> sha256=<hex of the blob>
text size=<n> sha256=<hex of the bytes up to the first blob>
wasm <-|sha256=<hex>>
```
Placeholders: `<valtype>` is `i32 i64 f32 f64 v128` or `ref:<null|nonnull>:<heap>` (`<heap>` a
type index or the abstract name: `func`, `extern`, `any`, …); `<comp>` is `func params=<valtypes>
results=<valtypes>`, `struct fields=<fields>` or `array field=<field>`, with `<field>`
`<storage>:<mut 0|1>` (`<storage>` a valtype, `i8` or `i16`); `<desc>` per import kind is
`type=<t>`, `<valtype> <limits>`, `<limits>`, `<valtype> mut=<0|1>` or `type=<t>`; `<limits>` is
`min=<n> max=<n|-> a64=<0|1> shared=<0|1>`; `<cexpr>` is its operations in order as
`<wasm name>` or `<wasm name>:<immediate>[:<immediate>]` (v128 as 32 hex digits), joined by `,`;
`<items>` is function indices or cexprs joined by `;`; any list is joined by `,`, `-` when empty.
The Python dumper is written by the implementer in step 1 (about 150 lines); the C one in step 1
too (about 250).

### Instantiation (S0014 step 7; D72)
The order of `JitInstance.__init__`:
1. Imports are matched by kind, type key and declared subtyping, limits and address type (as
   `link.py`); an import of another store is refused (`WABOROS_E_LINK`).
2. The context is written (part 3; import, libcall and service words hold `dispatch`; words 2-6
   hold the store's values), and the store's sets are extended. The engine refuses two compiler
   strings with one bridge address (D71), which cannot happen with per-string copies.
3. Guard registration: the code range with its fault map, the reservations. On Windows x64 under
   user shadow stacks with context-IP validation, each resume address is registered first with
   `SetProcessDynamicEHContinuationTargets` (guard.py `_eh_policy`/`_eh_targets`; S0006 step 4,
   CI run 41633456821). A process that cannot register them refuses guard files at step 5;
   otherwise the continuation at the resume address ends the process with 0xC0000409.
4. RTT words and type-info records; function-type records; the `IS_SUBTYPE` record (the
   collector: path 1 or 2).
5. Function records, signature words, tag identities.
6. Globals in order; tables with their `init`; every element segment's items.
7. Active element segments in order (bounds, write, drop; declarative ones dropped).
8. Active data segments in order (`MEMORY_OUT_OF_BOUNDS` past the memory; earlier writes stay).
9. The start function.

Constant expressions are evaluated on an explicit stack of 128-bit values; an allocation follows
part 3 and never collects (D61's deviation). The six libcalls and the seven GC services run behind
`dispatch`, in the specification's bounds and trap order. A trap during instantiation leaves the
instance's roots registered (B6).

### Calls, traps, exceptions
* `waboros_call` follows `Instance.call`:
  * claim the store (one thread);
  * refuse a lost store;
  * check the guard chain when the store has guard code;
  * at the outermost entry, reset the value stack and compute `NSTACK_LIMIT` from `sp_stub` and
    the budget, into every context of the store and the collector's;
  * check the value stack, write the arguments, call `entry(ctx, frame, c_entry)`.
* The call of `entry`'s bytes is in a function marked `__attribute__((no_sanitize("function")))`
  (Clang): `-fsanitize=function` reads a type signature before the target of an indirect call,
  and machine code from a file has none.
* The status:
  * a trap code (1-11 and 15-18) gives `WABOROS_E_TRAP`, with `trap` set;
  * 12 gives `WABOROS_E_INTERRUPT`; at the outermost entry, every context's word is cleared under
    the store lock;
  * 13 gives `WABOROS_E_HOST`, with the message of "Host calls";
  * 14 gives `WABOROS_E_EXCEPTION`, read at once ("C API");
  * anything else, the private range included, gives `WABOROS_E_INTERNAL`.
* A host function may call `waboros_call` again: the dispatcher's frame rule and `entry`'s saved
  block make it re-entrant.

### The walk, roots and a collection (`gc.c`, after `walk.py` and `collect.py`)
* **The walk.** From the exit record, frame by frame through each site's `ret_at`, `up` and
  `shift`, across the fixed points: the entry's return point with its saved block, and every
  compiler's `restore` with its numbers. Its checks (`walk.walk`), each failure
  `WABOROS_E_INTERNAL` and never a crash:
  * a return address is a call site of a code range of the store;
  * every stack word is read inside `[nlimit − 4 KiB, stack top)`; unknown stack bounds stop it;
  * a caller's stack pointer is above its callee's, and a saved one above its entry's block;
  * a restore frame does not return to another restore frame;
  * at an entry's return point, the frame base the entry saved is the segment's base;
  * there are no more segments than active entries.
* **The walk check** (`WABOROS_JIT_WALK_CHECK=1`, `walk.check`), at every host call and libcall:
  the exit record was written for this call (`sp` set, `frame` not above the slots); the
  segments' bases are the active entries' frames, outermost first; a frame base with references
  lies in the value stack; dead reference slots are nulled; every reference slot holds a valid
  word (`walk.valid`: null, an i31, a live host word, or a heap object). A failure gives 13 with
  its reason, as `_checked`.
* **Preconditions**, before anything moves (`collect.collect`):
  * the store is not lost;
  * the exit record's `sp` is set and the store's next frame is above its `frame` (a collection
    happens only inside a host call that set the base, B1);
  * the collector exists (path 1 or 2);
  * `next frame + 4 KiB` (`VSTACK_RESERVE`) fits the value stack, else `STACK_OVERFLOW`.
* **The collector's context** (`collect.collect`, D61):
  * `NSTACK_LIMIT` is the thread's stack low + 64 KiB, or `max(0, nlimit − 256 KiB)` when the low
    is unknown;
  * `VSTACK_LIMIT` is the value stack's top;
  * `INTERRUPT` is 0.
  The collector is not in the store's instance array, so no interrupt or walk reaches it.
* **Roots**, as `_gather`. Only references of the any, extern and exn hierarchies are roots
  (`_root`, `frontend/validate.py`); a funcref table, global or segment never is (its words are
  record addresses, which the collector would trap on):
  * every frame's `live` slots (a frame base outside the value stack is `WABOROS_E_INTERNAL`); the
    `refs` slots outside `live` are nulled;
  * such globals of every instance, **each word once**: an imported global is its owner's word,
    gathered once by address;
  * such tables once each, even when several instances import them;
  * passive, not-dropped element segments of such types;
  * the handle table;
  * the pending exception word.
* **The collection**: `begin(lo, cursor, to, to + half)`; then the roots in batches of at most
  7 680 words through `ROOTS` and `roots(n)`, each batch copied back in place; then `finish`.
  The store is marked lost from `begin` until the cursor, limit, `lo` and `end` are written back.
  A collector trap leaves it lost.
* **Host values**: unless the report's overflow flag (heap offset 16) is set, every host value
  whose handle has no report bit (or whose handle is ≥ 16 384) is freed, and its number is reused
  lowest first (`_free`). With the flag set, nothing is freed.
* **Test switches** (the Python names, read at engine creation; S0013):
  * `WABOROS_JIT_WALK_CHECK=1`: walk at every host call and libcall;
  * `WABOROS_JIT_GC_STRESS=1`: collect at every allocation that may collect;
  * `WABOROS_JIT_GC_POISON=1`: fill the freed space and new space with 0xA5.
  `WABOROS_JIT_GC_VERIFY=1` (`gcverify.py`, a graph digest) is **not** implemented in C; its
  checks run on the Python side of the same files.

### Executable memory (`os_*.c`)
* Code is written read-write and then made read-execute (W^X, R2).
  * Linux: `mprotect`; on aarch64 `__builtin___clear_cache` and `PROT_BTI` when `AT_HWCAP2` has
    BTI.
  * macOS arm64: `MAP_JIT`, with `pthread_jit_write_protect_np(0)` and `(1)` around each write,
    then `sys_icache_invalidate`. A signed binary would need the `allow-jit` entitlement; the CI
    binaries are unsigned.
  * Windows: `VirtualAlloc` and `VirtualProtect`, then `FlushInstructionCache`.
* Reservations are `PROT_NONE` / `MEM_RESERVE`, committed by `mprotect` / `MEM_COMMIT`.

### The guard handler (`os_*.c`, S0006's contract)
* **Ours** if and only if all three hold:
  * a hardware fault: Linux `SIGSEGV` with `si_code > 0`; macOS `SIGSEGV` or `SIGBUS`, where
    `si_code` does not tell; Windows `EXCEPTION_ACCESS_VIOLATION`;
  * the PC at a `fault_offset` of a registered code range. The PC comes from `gregs[REG_RIP]` or
    `pc` (Linux), `__ss.__rip` or `arm_thread_state64_get_pc` (macOS), `Rip` or `Pc` (Windows);
  * the fault address inside a registered reservation.
  Recovery sets the PC to `code + resume_offset` and nothing else (Windows:
  `EXCEPTION_CONTINUE_EXECUTION`; macOS arm64: the matching setter of `<mach/arm/thread_status.h>`,
  confirmed in step 7).
* **Otherwise**, chain exactly as S0006 says:
  * a previous `SA_SIGINFO` handler is called with the same three arguments, a plain one with the
    signal number;
  * a previous `SIG_DFL` or `SIG_IGN` is put back with `sigaction` and the handler returns. Only
    in this case is the signal sent again with `raise`: on Linux when `si_code <= 0`, on macOS
    always;
  * Windows returns `EXCEPTION_CONTINUE_SEARCH`.
* **`broken`**, the rule checked at every entry and after a host call (after `guard.broken`):
  * our handler is the current action of each signal: intact;
  * `SIG_DFL` or `SIG_IGN`: broken;
  * a handler inside an image the host has declared non-chaining with
    `waboros_guard_nonchaining` (`dladdr`): broken. The shim declares the Python interpreter's
    image, so faulthandler and the `signal` module's handler count, as in `_broken`;
  * any other handler (another library, another JIT's anonymous code, the Python runtime's own
    emitted handler) is assumed to chain;
  * Windows: a vectored handler cannot be removed, so never broken.
* In the shim process both copies are installed: the Python runtime's (when a test instantiates
  there) and the C loader's. Each chains to the other (S0006, "Chain and claim"), and ctest
  checks both install orders with a second copy of the C handler.
* Windows risk: faulthandler is a vectored handler that `enable()` puts in front of ours, where
  it reports guard faults (LESSONS 59). The Python runtime moves its own handler back to the
  front (`_to_front`); the C loader has no view of faulthandler. The shim's Windows runs disable
  it (`-p no:faulthandler`). Listed under "Risks".

## Test plan

### The harness (Decision 3)
* **The shim** (`tests/cloader/`) loads `waboros_loader_shared` with ctypes and registers the S0001
  engines `cloader-<target>` for each JIT target of the host (the sljit ones where `build/native`
  has them), as `tests/sljit` does (S0016). An engine:
  * `load` compiles the module with `<target>`, writes a fat file, and calls
    `waboros_module_load`. The engine has the test compiler;
  * `instantiate`, `invoke` and `get` call the C API;
  * `spectest`'s memory, table and globals come from `waboros_*_new`; host functions are ctypes
    callbacks of type `waboros_host_fn`.
  * `tests.spec.run` runs each `.wast` in its own child, so a C crash costs one file.
    `spec_diff` works unchanged.
  * Expected failures are the target's, copied to `tests/spec/xfail/cloader-<target>.txt`.
  * `cloader-mixed-<arch>` (Linux glibc) alternates `jit-<arch>` and `jit-sljit-<arch>` modules in
    one store.
* **The refusal corpus** (`tests/cloader/test_corpus.py`, in-process):
  * the cases: every whole-file case of `tests/artifact/test_refuse.py`, `test_strict.py` and
    `test_review.py`; every truncation of the D75 small files (P1) and of a GC module; 3 000 byte
    flips each of CoreMark, a GC module and the small module;
  * each file is checked in a fresh engine that already holds the unmutated file of its compiler
    string, so both trampoline compares see the same reference;
  * it runs once with the test compiler and once without one;
  * each file's class comes from "Designed exceptions to Decision 6", and the test fails on any
    file outside its class;
  * the files are also written out as the fuzzer's seeds;
  * the whole-file cases are rebuilt by `tests/cloader/cases.py` (step 1, about 200 lines,
    estimated), since those tests make many of their files under `monkeypatch` (a patched
    `compiler_string`, guard mode); each case records its source file (for E2);
  * in step 1 it runs on explicit files only: a guard file's step 5 needs the handler (step 4),
    and step 1's engine keeps each compiler string's copy as bytes for the compare and maps
    nothing (executable memory is step 2). Item 4 in full is step 4's acceptance.
* **`waboros_run`** (C, no Python in the process): `waboros_run FILE.wbo EXPORT [ARGS…]
  [--wiring] [--collector F.wbo] [--expect-trap N] [--guard]`, and `--dump` / `--check FILE…`.
  `--wiring` supplies CoreMark's imports, as `tools/run_coremark_jit.py` does: `wiring.millis` (a
  monotonic clock in ms) and `wiring.print(ptr, len)` (bytes of the memory to stdout). Its
  files are written earlier in the same job by `tests/cloader/make_files.py` (about 60 lines,
  step 6), with each row's Python target: CoreMark, `tests/cloader/traps.wat` (one export per
  trap code), the committed files' module, and the collector.
* **Later, not in this spec**: a C wast runner, with the embedded compiler.

### Runtime tests
`tests/cloader/test_runtime.py` re-runs, through the shim, the scenarios of these Python tests
with the same expectations (file `::` test, under `tests/jit/`). The `test_abi.py` ones are
hand-assembled x86-64 (`build` with backend code), so they are rewritten as WAT modules with host
imports (step 3's Python budget); the others already use WAT.
| area | tests |
|---|---|
| nested entries | `test_abi.py::test_reentry_inner_trap_reaches_the_host`, `::test_reentry_50_deep`; `test_exit_record.py::test_exit_record_names_the_caller_and_survives_nested_entries`; `test_gc_nested.py::test_nested_entry_from_a_service` |
| interrupts | `test_abi.py::test_interrupt_from_another_thread`, `::test_interrupt_is_consumed_in_every_context_of_the_store`; `test_gc_collect.py::test_an_interrupt_does_not_cut_a_collection` |
| host errors | `test_abi.py::test_host_exception_surfaces_as_itself` (inside a nested entry), `::test_a_host_that_never_writes_its_status_fails` (as the shim test, "Host calls"); `test_exceptions.py::test_a_host_trap_14_is_a_host_error_never_status_14` |
| stacks | `test_abi.py::test_native_stack_overflow_capped_by_a_small_thread_stack`, `::test_value_stack_overflow` |
| memories, tables | `test_memory64.py::test_move_from_wasm`, `::test_move_in_a_nested_entry`, `::test_table64_grow_results_and_cap`, `::test_huge_table_minimum_is_out_of_memory`; `test_tables.py::test_table_grow_across_nested_entries` |
| references | `test_tables.py::test_externref_round_trip`, `::test_funcref_out_and_back_in`; `test_gc_collect.py::test_handles_keep_their_identity`, `::test_released_handles_are_reclaimed`, `::test_held_handles_survive_the_release_of_others`, `::test_host_values_are_freed_and_reused_lowest_first`; `test_gc_roots.py::test_an_imported_global_is_a_root`, `::test_a_table_imported_by_two_instances_is_visited_once`, `::test_a_failed_instantiation_leaves_its_globals_roots` |
| collector | `test_gc_collect.py::test_growth_doubles_never_shrinks_and_flips`, `::test_a_collector_trap_loses_the_store`, `::test_lost_store_does_not_resume_wasm`, `::test_tables_are_roots_in_batches_and_funcref_tables_are_not` |
| threads | `test_memory64.py::test_another_thread_is_refused_while_a_host_import_runs` |
| boundary | D71's distinctness: a file re-labelled with another compiler version (E1) loads with its own copy, and the two contexts' `HOST_BRIDGE` differ. The Win64 `entry` bytes and a file's `win64` bridge calling `dispatch` through an `ms_abi` function on Linux x86-64, as `tests/sljit/test_win64_bridge.py` does |

### Acceptance commands
1. `cmake -S native -B build/native && cmake --build build/native && ctest --test-dir build/native`:
   0 failures on the six rows of step 0 (Linux glibc, macOS, Windows; musl and Android in step 9).
   ctest covers:
   * the reader on the committed files;
   * `entry` and `sp_stub`, an add and a trap;
   * the handler's chaining (a foreign fault, a sent signal, both install orders);
   * the consumer check with `waboros::loader`.
2. `-DWABOROS_SLJIT=OFF` with no submodule checked out builds the loader.
3. The spec suite at parity, with `WABOROS_JIT_WALK_CHECK=1`:
   * Linux glibc x86-64: `uv run python -m tests.spec.run --engine cloader-jit-x86_64`, and the
     same for `cloader-jit-sljit-x86_64` and `cloader-mixed-x86_64`, guard and explicit, each
     meeting its expected-failures file;
   * `uv run python tools/spec_diff.py jit-x86_64 cloader-jit-x86_64` (Python side with
     `WABOROS_JIT_ARTIFACT=1`) both ways is empty, and likewise for each sljit engine;
   * `gc/` and `exceptions/` also with `WABOROS_JIT_GC_STRESS=1` and `WABOROS_JIT_GC_POISON=1`;
   * Linux aarch64 native: the same for the aarch64 engines;
   * macOS and Windows, both architectures: `cloader-jit-<arch>`, guard and explicit.
4. `uv run pytest tests/cloader/test_corpus.py`: 0 files outside their class, on Linux x86-64
   and aarch64 (the reader is OS-independent). Explicit files from step 1, all files from step 4.
5. **The committed files** (P1's regenerated set, Decision 7), through the shim and `waboros_run`,
   on every row:
   * every file of the set (x86-64 and aarch64 for the host, guard and explicit, thin and fat) is
     read, and its exports run; the host-import module's import gets the right word; where the
     row has no handler ("Rows"), guard files are refused at step 5;
   * the small module has `heap` 1 (a GC struct global): it runs with the collector file
     supplied, and is refused with `WABOROS_E_NO_COLLECTOR` without one (no compiler);
   * a thin file of the other architecture is refused at step 5 (`e_machine`);
   * a fat file whose code fails step 5 (the other architecture's) gets `WABOROS_E_NEEDS_COMPILER`
     without a compiler, and is compiled and run with the test compiler;
   * a copy with `EI_ABIVERSION` 2 is refused at step 2.
   The pre-D75 fixtures are not loaded by the C loader.
6. **The three GC builds**:
   * `WABOROS_WITH_GC=OFF`: a `heap` 1 file is refused at load, and a `throw_ref`-only module runs;
   * no compiler and no collector file: a `heap` 1 module is refused at instantiation, and the
     store stays usable;
   * the collector supplied at run time, and then embedded with `WABOROS_COLLECTOR_WBO`: `gc/`
     runs with the switches;
   * a collector file with another `module_sha256` is refused;
   * a pytest ties `collector_wasm.c` to `collector.wasm`.
7. **CoreMark**: `waboros_run core.<target>.wbo setup --wiring` passes its self-check on every row.
   Its time is alternated with `tools/run_coremark_jit.py` on the same file (BENCHMARKS.md),
   which gains a `.wbo` input in step 6.
8. **Sanitizers** (Linux x86-64): ctest and `waboros_run` (CoreMark, the committed files,
   `traps.wat` in both modes), built with
   `-DCMAKE_C_FLAGS="-fsanitize=address,undefined,function -fno-sanitize-recover=all"` (Clang).
   Guard mode runs with `ASAN_OPTIONS=handle_segv=0`, so ASan does not take the handler's faults.
9. **Fuzzing**: `tests/loader/fuzz_read.c`, libFuzzer over `waboros_module_load` with a fresh
   engine per input (no state carries from one input to the next), seeded by item 4. It runs five
   minutes in `fuzz-nightly.yml`, never blocking.
10. Build sizes ("Measurements to take") recorded in BENCHMARKS.md.
11. `python3 tools/check_docs.py`.

## Plan of steps
Every step stops and reports if it needs more than its size. P1 changes a contract (D75); the
other steps change none. A divergence from Python is a bug in one of the loaders, settled by the
text; if the text does not settle it, the text is amended first (the working freeze: a spec or a
decision, no bump). CLAUDE.md requires a `reviewer` after every JIT, ABI, artifact or trap change,
so P1 and steps 2-9 but 6.1 end with one (step 6: aarch64 execution, the instruction cache, BTI, the
Win64 `ms_abi` test; step 7: the macOS handler, `MAP_JIT`, the PC on arm64).

| step | goal | agent | acceptance | size (lines, estimated) |
|---|---|---|---|---|
| P1 | **D75.** The four targets' bridges pass the word (`edx`/`r8d`/`w2`) in every convention. ARTIFACT.md part 3, S0015, S0003, D34/D32 markers. A word test per import, libcall and nested entry on every JIT target. The committed v1 set regenerated by `tools/make_v1_artifacts.py` (x86-64 and aarch64, guard and explicit, thin and fat) plus a host-import module; one or two pre-D75 files kept (Decision 7) | jit-engineer, then reviewer | the spec suite at parity on the four targets in-process and through files, both modes; `tests/artifact/test_old.py` runs the new set (read, inventory, instantiate, the host import's word, load thin and fat, the version refusals) and refuses the kept pre-D75 thin file(s) on their bridge bytes, regenerating the fat; `tools/jit_dump.py` lists unchanged (its hash covers the `Compiled`, not the trampolines); for each file of `tools/artifact_dump.py OUT DIR` before and after, the `artifact.read` views are equal but for `compiler` (the development digest, `_own_digest`), the trampoline records and the blobs, shown by a comparison whose command and output the report gives (the tool prints one hash per file, so its lists cannot show it); `pytest -q` | src ~60, tests ~170, docs ~40 |
| P2 | **Refusal steps.** `ArtifactError.step` and `.section`, set at the phase boundaries of `load`/`read`; the fat-file SHA-256 check named step 1b in S0014 (Decision 9); no corpus case on the `cannot load:` path | implementer | `tests/artifact/` asserts the step per refusal; `pytest -q` | src ~40, tests ~60 |
| 0 | `native/CMakeLists.txt` restructured; `WABOROS_SLJIT`, `WABOROS_LOADER`, `WABOROS_LOADER_SHARED`; an empty loader and `waboros_run`; the consumer check; MSVC confirmed; a `cloader` CI job on the six runners | implementer | items 1-2 on the six rows | cmake ~100, CI ~70 |
| 1 | The reader (`read.c`, `sha256.c`), the frozen lookup, both dumpers, `--dump`/`--check`, the shim's reader part, the corpus test with its classes and `cases.py` (needs P1 and P2); no executable memory, no handler: copies kept as bytes | implementer | item 4 on explicit files, both architectures | C ~1 350, Python ~600 |
| 2 | Linux: executable memory, `entry`/`sp_stub` bytes tied to `entry.py`, trampoline copies, locks and the store claim, calls without imports; the engines | jit-engineer, then reviewer | `uv run python -m tests.spec.run --engine cloader-jit-x86_64 --xfail tests/cloader/step2-xfail.txt $(cat tests/cloader/step2.txt)` (the `.wast` files whose modules import nothing, listed by the step from the converted JSON) passes, and the same for `cloader-jit-sljit-x86_64`, explicit | C ~900, Python ~300 |
| 3 | Instantiation and linking complete: `dispatch` (D75), host externs, globals, references at the API, libcalls, memory64, interrupts, stack limits, exceptions without the heap | jit-engineer, then reviewer | item 3 (Linux x86-64, explicit) without `gc/` and the heap-using `exceptions/`; `spec_diff` empty; the non-GC runtime tests (the `test_abi.py` ones rewritten as WAT) | C ~1 000, Python ~400 |
| 4 | The guard handler on Linux, `broken`, both install orders | jit-engineer, then reviewer | item 3 in guard mode; the chaining ctests; item 4 in full | C ~300 |
| 5 | GC: heap, records, services, walk, roots, collector driver, `collector_wasm.c`, the switches; the three GC builds | jit-engineer, then reviewer | items 3 (`gc/`, `exceptions/`, switches, walk check) and 6; the GC runtime tests | C ~1 200 |
| 6 | Linux aarch64 (native, QEMU with BTI), mixed stores, `make_files.py`, the committed files, CoreMark (`run_coremark_jit.py` takes a `.wbo`), sanitizers, fuzz job; the `ms_abi` Win64 test | runner + jit-engineer, then reviewer | items 3, 5, 7, 8, 9 on Linux, both architectures | C ~200, Python ~150, CI ~120 |
| 6.1 | Build sizes | runner | a BENCHMARKS.md section; Open question 1 answered from it | tools ~40 |
| 7 | macOS x86-64 and arm64 (`os_posix.c`'s Apple parts) | jit-engineer, then reviewer | items 1, 3, 5, 7 on both macOS runners | C ~300, CI ~30 |
| 8 | Windows x64 and ARM64 (`os_windows.c`, the vectored handler, CET continuation targets, the Win64 `entry`) | jit-engineer, then reviewer | items 1, 3, 5, 7 on both Windows runners; a `/CETCOMPAT` build of `waboros_run` runs the guard traps on x64 | C ~450, CMake ~30, CI ~30 |
| 9 | Linux musl (x86-64, aarch64) and Android (x86-64, arm64): the bionic and musl notes of `os_posix.c`, the NDK build (CMake's Android toolchain file; the shared library for the shim), the CI steps | jit-engineer + runner, then reviewer | musl, both architectures: items 1, 5, 7 and 8's `waboros_run` runs (no sanitizer), and item 3 with `cloader-jit-x86_64` in the x86-64 container. Android x86-64: `tests/cloader/test_android.py` in the cell's app (the shim loads the NDK-built library; guard faults through libsigchain, as `tests/jit/test_android.py`), and `waboros_run` over `adb shell` (no ART) for CoreMark and the committed files. Android arm64: the same where an arm64 emulator runs, else the build | C ~60, CMake ~30, Python ~80, CI ~100 |
| 10 | Docs ("Docs this changes"), a DECISIONS entry | orchestrator | `python3 tools/check_docs.py` | — |
| 11 | Final review without the implementers' reasoning: the whole loader against S0014, S0015 and this spec, C99 conformance | reviewer | findings closed | — |

* **Order**: P1 and P2, then 0. Step 1 needs P1 (the corpus uses the D75 files) and P2 (the
  steps), and no executable memory or handler. Then 2-6 on Linux glibc (item 4 in full after
  step 4), 6.1, 7, 8 and 9.
* **Total** (estimated): about 5 760 lines of C, 1 530 of Python, 160 of CMake and 350 of CI,
  plus P1 and P2.
  This is scaled from the Python sizes: `artifact.py` 1 184, `instance.py` 1 374, and `heap.py`,
  `collect.py`, `walk.py`, `guard.py` and `execmem.py` about 1 460. C needs more lines per rule
  and has no ctypes plumbing.

## Measurements to take
* Load and instantiation time against Python's, for CoreMark and a GC module (BENCHMARKS.md,
  "Instantiation from an artifact").
* CoreMark through the C loader against the Python loader on the same file, alternated, per row.
  The code bytes are the same, so a difference points to a context or placement discrepancy.
* A host call through `dispatch`: the import loop of STATIC-BUILDS.md ("Cheap host calls"), a C
  host against a Python host.
* **Build sizes** (step 6.1; Open question 1):
  * the stripped size (text + data, `size` after `strip`) of a static `waboros_run` in four
    builds: full, no compiler, no GC, neither;
  * at `-Os` (MinSizeRel) and `-O2`, x86-64 and aarch64, LTO
    (`CMAKE_INTERPROCEDURAL_OPTIMIZATION`) on and off;
  * the collector's thin `.wbo` per architecture and bounds mode;
  * recorded in BENCHMARKS.md with the compiler version. Until D68 exists, "full" and "no GC" use
    S0016's `waboros_sljit` as a stand-in for the compiler's sljit part and say so.
  * The orchestrator's inputs (2026-11-24; sljit 39c508d1, gcc 13, the full sljit API as the shared
    library):
    * sljit text 56 KB (x86-64) and 47 KB (aarch64) at MinSizeRel `-Os`, 90 and 80 KB at `-O2`;
    * `collector.wasm` 1 517 bytes, thin `.wbo` 11.4 KB and 11.1 KB (code 3.7 and 3.6 KB);
    * CoreMark 19.5 KB of wasm, thin `.wbo` 53 KB and 48 KB.
  * For scale, in Python lines: the front end 1 962; the JIT's common core (compiler, lower,
    liveness, backend, simd) about 1 760; the sljit backend 1 728 (both architectures); the loader
    and runtime side about 4 000. So the compiler is expected to be sljit plus a C port of about
    5 400 Python lines, against about 4 000 for the loader.

## Risks
* **Two implementations of instantiation** (D72's accepted risk). Covered by items 3-5 and the
  runtime tests. RUNTIME-MODULES.md item 4 names where re-implementations fail: the order of traps.
* **The handlers** (R6):
  * four mechanisms on eight guard rows, Android's beside ART;
  * a second handler beside Python's in the shim process;
  * on Windows, faulthandler in front of ours (LESSONS 59), which the C loader cannot see;
  * under CET, a resume address that is not registered ends the process.
  S0006's chaining is the contract. ctest checks both install orders, `waboros_run` runs guard
  traps with no Python handler, and step 8 runs a `/CETCOMPAT` build.
* **Old bridges**: a pre-D75 file's host call could go to the wrong function (the check is
  best-effort). Decision 7 removes such files from everything but the Python refusal test.
* **Android**: the handler beside ART (libsigchain), and a child process that resets the chain
  (S0006). The arm64 row may stay build-only until a runner has an arm64 emulator.
* **Executing a file's blob** is new on every row (Python never does). Steps 2, 7 and 8 are the
  first runs.
* **The walk and the collector** run across foreign frames: a wrong site number corrupts the heap
  silently. The walk check and poison run in item 3.
* **Hidden Python behaviour**: a choice of `instance.py` that is neither contract nor in the table
  shows up as a `spec_diff` line or a runtime-test failure. It then goes into the table, or into
  part 3.
* **MSVC's C**: clang-cl is the fallback (step 0).

## Alternatives considered
* **Q1, what it is** (decided: test tool). The other option was a product from the start: a
  stable, versioned API and `find_package`, which is phase-6 work done in C.
* **Q2, rows** (decided: six rows, then musl and Android by Decision 8). The other option was
  Linux glibc only. macOS and Windows cost about 300 and 450 lines plus their CI rows and the
  first runs of the file's `win64` blob; musl and Android about 60 lines plus their CI steps.
* **Pre-D75 files** (Decision 7, (a')). The rejected options were a `features_optional` bit that
  the C loader requires, a `features_required` bit (rejected by D75), the compiler version (a
  development string's source hash has no order), and best-effort support of old host calls.
* **Q3, harness** (decided: shim plus `waboros_run`). The other options were:
  * a C line server under the Python runner: about 250 C lines, process isolation, no Python in
    the C process, round trips of about 10-20 µs (estimated);
  * a standalone C wast runner now: about 600 C lines, the runner's semantics written twice, and
    no `spec_diff`.
* **Q4, host calls** (decided: D75). The other options were:
  * a pool of preprocessor-made dispatchers (`WABOROS_HOST_SLOTS`), with a fixed limit;
  * per-import machine-code thunks, per-architecture bytes;
  * the raw part-3 signature, which cannot tell imports apart.
* **Q5, data in the build** (decided). The other option was run-time supply only.
* **Q6, refusal agreement** (decided: accept/refuse, dump, step). Finer check ids and message
  equality were rejected: S0014 does not make them normative.
* **Reuse WAMR pieces**: its AOT format, context and convention are its own
  ([WAMR-INTEROP.md](../WAMR-INTEROP.md)). Only its platform layer fits, and it brings a licence
  and a build for about 600 lines of `os_*.c`.
* **Rust or Zig**: D60 chose C.
* **C generated from the Python loader**: it is ctypes over Python objects, and D36 asks for a
  loader written from the text.
* **Instantiation as a wasm runtime module**: rejected by D72.
* **`entry` as `.S` files**: needs `enable_language(ASM)` and MASM syntax. Byte arrays tied to
  `entry.py` need no assembler (ARTIFACT.md part 1 plans the hex bytes).
* **A local's address instead of `sp_stub`**: wrong under ASan's fake stack ("What is contract").
* **Mach exception ports on macOS**: Wasmtime's default; the oracle uses signals.
* **A separate C runtime for the no-compiler build**: one library with options instead; the seam
  pointer may be NULL.

## Open questions
1. **Are the no-compiler and no-GC builds worth having?** Decided **after step 6.1's
   measurement**.
   * No compiler: kept unless it removes little. It is expected to remove sljit (47-90 KB) plus
     the C core, and it is the build for devices that run only AOT files.
   * No GC: free only if it is just the omission of `gc.c` plus a refusal at load. Its cost is one
     CI row and the `#if`s at the service words and the walk; its gain is `gc.c` (about 1 200
     lines) and the collector's 11 KB file.
   * The options: (a) keep both; (b) keep no-compiler, and drop no-GC if it saves less than an
     agreed share (for example 10 % of the no-compiler build); (c) drop both.
   * Until then, no-GC is built only as the omission of one file.

## Docs this changes
When approved and implemented:
* By P1 (D75):
  * ARTIFACT.md part 3: "Host functions" (`host(ctx, slots, word)`) and a fixed per-architecture
    row;
  * [S0015](S0015-boundary-convention.md): the bridge rows;
  * [S0003](S0003-jit-context-abi.md): the `host` prototype;
  * DECISIONS.md D34 and D32: markers;
  * D75 itself:
    * its Evidence "S0017 Q4 (a)-(c)" becomes "S0017, Alternatives considered (Q4)";
    * "the C loader's dispatcher checks the word's range and `ctx[word]`" becomes
      "best-effort; the C loader requires files written after D75 (S0017 Decision 7)";
  * `tests/artifact/data/README.md`: the regenerated set (made by `tools/make_v1_artifacts.py`,
    with the host-import module), the one or two pre-D75 files kept only for the Python refusal
    test, and the removal of the rest.
* By P2: [S0014](S0014-artifact-format.md) "Order of checks": step 1b, the frozen lookup's rules
  ("Loading", 2), and `ArtifactError.step` and `.section` in "Semantics: the loader".
* [ARTIFACT.md](../ARTIFACT.md):
  * part 1: "Planned: hex bytes" points to `native/loader/entry.c`;
  * part 2: "A loader refuses … whose `compiler` string is not the store's" names the waboros
    loader, with the foreign-host rule and its target check beside it;
  * "Versioning rules": the last bullet names this spec.
* [D70](../DECISIONS.md) and S0015 "Compatibility": the foreign-blob path and the per-architecture
  rule are exercised by the C loader before phase 6 (a foreign host, S0014 Q3). The waboros
  loader's rule is unchanged.
* [ROADMAP.md](../ROADMAP.md): phase 6's deferred list notes that the foreign-blob path already
  runs in the C loader; "Next" (6)(b) is done.
* [S0016](S0016-sljit-on-the-boundary.md) "Where it lives": a marker that "no generated sources"
  reads "no generator outside CMake" (Decision 5), and that `native/` configures without the
  submodule.
* [S0006](S0006-guard-pages.md): the row table gains the C loader's handler on the oracle's
  eight guard rows (none on Linux musl aarch64 and Android arm64, as the Python runtime), and the
  C `broken` rule.
* [RISKS.md](../RISKS.md) R6: the C loader's handlers, the shim process with two copies,
  Windows faulthandler.
* [DECISIONS.md](../DECISIONS.md): D36's marker exists; an entry when the loader is implemented
  (rows, harness, GC builds, Decisions 7-9, Open question 1).
* [TESTING.md](../TESTING.md), [TARGETS.md](../TARGETS.md),
  [MICRO-COMPILERS.md](../MICRO-COMPILERS.md) ("The C variant" links the seam and the three
  collector paths), [BENCHMARKS.md](../BENCHMARKS.md), CLAUDE.md "Commands",
  [LESSONS.md](../LESSONS.md) (traps found on the way).

## Changes
* 2026-11-24: created (draft).
* 2026-11-24: the three paths of the GC collector stated explicitly (maintainer's clarification).
* 2026-11-24: build sizes as a measurement (step 6.1) with the orchestrator's inputs; whether the
  no-compiler and no-GC builds stay is decided after it.
* 2026-11-24: Q1-Q6 decided by the maintainer (Decisions 1-6; the rejected options in
  "Alternatives considered"). The draft had recommended Q2 (a), Q3 a C line server, Q4 a
  dispatcher pool and Q6 (a) alone.
* 2026-11-25: revised to the decisions: a Decisions list, six rows, the internal API with
  `dispatch`, the harness, and the plan with P1, P2 and steps 7 and 8.
* 2026-11-25: revised after an independent review (docs only). Blocking: the compiler-string
  rule stated exactly (step 4 as `_head`, S0014 Q3's target check, no step-5 compare); the designed
  exceptions to Decision 6 (E1, E2, fat); the inventory dump defined; the corpus with and without
  the test compiler. Should-fix: the dispatcher check best-effort (Open question 1) and its steps
  after `_host`; `sp_stub` kept; locks and the one-thread rule; the collection after `collect.py`;
  the C API extended; the plan (P1 before step 1, step 2's command, step 3 to the jit-engineer,
  reviewers, the D35 statement); D47 (a)/(d); the handlers per OS (CET targets, `raise` only after
  `SIG_DFL`/`SIG_IGN`, `broken`, faulthandler, `arm_thread_state64_get_pc`); the trampoline copy
  (adopted after the whole file, per engine, D71's test, M5); the runtime tests; `_frozen`'s rules;
  `ArtifactError.section`; `no_sanitize("function")`; Docs this changes extended; nits.
* 2026-11-25: open question 1 decided (maintainer): no format bit; P1 regenerates the committed set with a
  host import, the old files stay only for the refusal test.
* 2026-11-25: open question 2 decided (maintainer): Linux musl and Android are rows of this spec.
* 2026-11-25: open question 4 decided (maintainer): (a), the fat-file SHA-256 is S0014's step 1b.
* 2026-11-25: revised to Decisions 7-9: the dispatcher text, item 5 and P1 on the regenerated
  set; four new rows (Linux musl and Android, both architectures) with their evidence and a
  step 9; the remaining open question renumbered 1 (the build sizes).
* 2026-11-25: revised after a second independent review (docs only). Blocking: the dispatcher's
  lost-store check after the call, as `callback`; E1-fat and E2-fat classes, the `fat` dump, and
  E2's compare. Should-fix: GC roots only of the any, extern and exn hierarchies; references at
  host functions as `_ref_host`; one handle per object in the C store; the `test_abi.py`
  scenarios rewritten as WAT and the D34 shim rule; step 1 without executable memory or the
  handler, the full corpus after step 4; reviewers after steps 6 and 7; guard rows per
  architecture and D47 rule 3; a process lock for the handler; the walk's checks; C API calls
  (`waboros_ref_kind`, `waboros_host_message`, `waboros_guard_nonchaining`, host-side grow and
  table calls); `_GNU_SOURCE`; the sljit known-target difference; P1's dump acceptance. Nits:
  `check` "1b", the trampoline check at Python's point, the main-thread stack rule on glibc, type
  ids below 2²⁰, the collector hash in path 2, strict UTF-8, `--wiring` and CoreMark's tool,
  `cases.py`, the D47 (d) record at approval, estimates marked.
* 2026-11-25: Decision 10 (no guard handler on musl aarch64 and Android arm64, as the oracle;
  a handler there is a v2 candidate for both loaders).
* 2026-11-25: Decision 11 (handles released: weak in Python, `waboros_ref_release` in C).
* 2026-11-25: Decision 11 implemented in the Python runtime (the oracle); the two tests in the runtime-test table.
