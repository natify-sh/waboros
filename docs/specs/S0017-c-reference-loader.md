---
title: The C reference loader
kind: spec
id: S0017
status: draft
superseded_by:
related: [D18, D23, D34, D36, D47, D50, D54, D56, D58, D60, D61, D62, D65, D68, D69, D70, D71, D72, D73, D74, R2, R3, R6, S0001, S0006, S0013, S0014, S0015, S0016]
owner: maintainer
updated: 2026-11-24
---

# S0017 The C reference loader

## Summary
A loader and runtime for JIT artifacts written in C99 and built with CMake alone (D36, D60;
ROADMAP.md "Next" (6)(b)). It reads format v1 ([S0014](S0014-artifact-format.md)) with the same
checks as the Python reader, instantiates a module from the file alone, and runs it under context
ABI v1 with the boundary of [S0015](S0015-boundary-convention.md): its own `entry`, host functions
behind each compiler's bridge, traps, interrupts, stack limits, the frame walk, the GC heap with
the collector module, exceptions, and stores that hold code of several compilers. The Python
loader (`targets/jit/common/artifact.py`, `instance.py` and the runtime services) is the oracle.

The loader has two builds. One embeds a compiler for its own architecture; the other has no
compiler and runs only AOT files. This spec defines the loader and the **seam** where a compiler
plugs in, not the compiler: D68's micro-compiler does not exist yet (ROADMAP "Next" (6)(c)). The
C loader is the second of the three conditions of the final freeze of v1 (ARTIFACT.md, Status;
D73's amendments).

The maintainer has already decided the following (2026-11-23 and 2026-11-24; ROADMAP "Next" (6),
[MICRO-COMPILERS.md](../MICRO-COMPILERS.md) "The C variant", D72). This spec does not reopen them:
* C99 and CMake only, no Python to build, under `native/`, with S0016's CMake contract;
* two builds, with and without an embedded compiler;
* the GC collector stays a wasm module, and a build option leaves GC out;
* instantiation and the segment libcalls are written in C from the specification (D72);
* artifacts are architecture-dependent and OS-independent (D23); v1 must not block 32-bit or MCU
  targets;
* the oracle: the spec suite through files and the refusal corpus must agree on both loaders, and
  files from both compilers must load.

## Goals / Non-goals
* Goals:
  * the C library `waboros_loader` and a small command-line program, built from the repository's
    C sources with a C99 compiler and CMake 3.21, as a top-level project or as a subproject;
  * a strict reader that accepts and refuses exactly what `artifact.read` accepts and refuses,
    except for the trampoline rule, which differs by design for a foreign host (S0014,
    "One copy per store", "A foreign host");
  * instantiation, calls, host functions, traps, interrupts, stack limits, GC and exceptions that
    agree with the Python runtime on the whole spec suite through files, for `jit-x86_64`,
    `jit-aarch64`, `jit-sljit-x86_64` and `jit-sljit-aarch64` files, and on mixed stores;
  * the committed v1 files (`tests/artifact/data/`) run, or are refused, as `test_old.py` expects;
  * the compiler seam, specified so that D68's micro-compiler can be plugged in without changing
    the loader.
* What "reference" means: correctness and clarity come before speed. Each rule cites the section
  of S0014, S0015 or ARTIFACT.md that it implements. There are no fast paths without a
  measurement (CLAUDE.md, "Measure, do not estimate"), and the code is small enough for a
  reviewer to read whole.
* Non-goals:
  * WASI and any host-function library beyond what the tests need;
  * the public API of phase 6 (wasmtime-py's shape), and a stable C ABI for embedders: the C API
    below is the test interface and may change until a later decision;
  * the compiler (D68, ROADMAP "Next" (6)(c)), the compiler `.wasm` and cross-compilation;
  * static builds and linked AOT artifacts (D62, [STATIC-BUILDS.md](../STATIC-BUILDS.md));
  * threads (D58: words 6-8 of the libcall block stay zero, and a shared memory is refused);
  * fuel and epochs (no flag in v1: words 3 and 4 stay 0);
  * `isa_level` above 1 (as the Python loader: S0014, "Known limitations of v1");
  * 32-bit hosts, MCUs and Android in the first version ("Rows"; macOS and Windows are in, Q2).
* Rows (D47): Linux glibc, macOS and Windows, x86-64 and aarch64, before the final freeze (Q2, decided (b));
  the rest of this paragraph predates Q2 and is to be revised. The per-OS code
  (executable memory, reservations, stack bounds, the fault handler) is in one file, so another
  row adds that file's counterpart and touches nothing else.

## Interface

### Where it lives
```
native/
  CMakeLists.txt          + the loader's options and targets (below)
  loader/
    waboros.h             the only public header: the C API below
    read.c                ELF container and payloads, S0014 checks 1-6, cexpr parse and types
    sha256.c              module_sha256 of a fat file; type-key recomputation (tests only)
    instance.c            context layout, linking, instantiation (S0014 step 7), calls, services
    gc.c                  heap regions, type-info and function-type records, the GC services,
                          the walk, roots, the collector's driver (absent with WABOROS_WITH_GC=OFF)
    entry.c               the runtime's `entry` per architecture as byte arrays (ARTIFACT.md part 3
                          listings), mapped once per process
    os_linux.c            reserve/commit/protect/flush, PROT_BTI, stack bounds, the fault handler
    compiler.h            the seam (below)
  tools/waboros_run.c     the command-line program: run an export, --dump, --check, --serve
  tests/loader/           ctest self-tests (C), no Python
tests/cloader/            the Python side (tests only): the S0001 engine over --serve, the corpus
```
The loader does not depend on sljit. A build without a compiler compiles no sljit source.

### CMake
The S0016 contract holds: `cmake_minimum_required(VERSION 3.21)`, `FetchContent_Declare` +
`FetchContent_MakeAvailable` (`SOURCE_SUBDIR native`) or `add_subdirectory`, never
`FetchContent_Populate`, `target_*` properties only, `WABOROS_*` options, `waboros_*` targets
with `waboros::` aliases, tests only when `PROJECT_IS_TOP_LEVEL` or `WABOROS_BUILD_TESTS`.

| option | default | meaning |
|---|---|---|
| `WABOROS_LOADER` | ON | build `waboros_loader` (`waboros::loader`, static) and `waboros_run` |
| `WABOROS_WITH_COMPILER` | OFF until D68's compiler exists, then ON on x86-64 and aarch64 | link the embedded compiler through the seam; OFF: AOT files only |
| `WABOROS_WITH_GC` | ON | the GC heap, the GC services and the collector's driver; OFF: no heap, and a file with `.waboros.context` `heap` 1 is refused at load |
| `WABOROS_GUARD` | ON on rows that have a handler | the fault handler; OFF: a `BOUNDS_GUARD` file is refused at load (S0014 check 5) |
| `WABOROS_COLLECTOR_WBO` | empty | a path to the collector's `.wbo`, embedded in the library as data (below) |
| `WABOROS_SLJIT` | `WABOROS_WITH_COMPILER` or a top-level build | build S0016's `waboros_sljit`; today the submodule check fails the configure unconditionally |

Sanitizers and fuzzing flags come from the top-level command line (`CMAKE_C_FLAGS`), as any
user's flags do. The project sets none.

### C99 rules
* `C_STANDARD 99`, `C_EXTENSIONS OFF`, `_DEFAULT_SOURCE` on the target, as S0016 does. Warnings
  (`-Wall -Wextra -Wconversion -Wvla`) are `PRIVATE` and set only for GNU and Clang.
* **No VLAs and no `alloca`.** Native stack belongs to wasm code (the headroom rule, S0015), so
  every buffer is fixed-size or on the heap.
* **Reading bytes.** Every payload field is assembled from bytes (`rd_u32(p)`: shifts of
  `uint8_t`). The reader never casts a file pointer to a wider type, so alignment and strict
  aliasing cannot break it, and it runs on any host, a big-endian one included (useful for tools).
* **Arithmetic.** Sizes and offsets are `uint64_t`. Every count is checked against the bytes left
  before it is used or allocated (S0014, "Conventions"), and every sum is checked before it is
  formed. Signed overflow, shifts by the width and `long` (LLP64) do not appear.
* **Memory shared with code** (the context, value stack, linear memories, tables, the GC heap) is
  read and written as `uint64_t` arrays or with `memcpy`, never through a pointer of another
  type, so aliasing stays well-defined.
* **The one implementation-defined step**: an address in executable memory becomes a function
  pointer (`entry`) by a cast from `uintptr_t`. POSIX guarantees it, and it is the only place
  that needs it.
* **Atomic words.** `INTERRUPT`, written by any thread, is an aligned `volatile uint64_t` store.
  It is single-copy atomic on x86-64 and aarch64. C11 atomics wait for D58, as S0016 says.
* Every call into the library returns a status and fills a caller-provided `waboros_error`
  (a code and a message). Nothing aborts and nothing prints. "Never crash the interpreter"
  becomes "never crash the host process": a malformed file, a trap or a failed allocation is a
  status.

### C API (test interface, not phase 6)
```c
typedef struct waboros_engine waboros_engine;   /* per process: type ids, tag ids, trampolines, handler */
typedef struct waboros_store  waboros_store;    /* one thread at a time */
typedef struct waboros_module waboros_module;   /* a read artifact: metadata + code, not yet mapped */
typedef struct waboros_instance waboros_instance;
typedef struct { int32_t code; uint32_t trap; char msg[200]; } waboros_error;
/* code: 0 ok, 1 not ours (S0014 check 1), 2 refused (checks 2-6), 3 link, 4 trap,
   5 host error, 6 resource (memory, address space), 7 needs a compiler, 8 unsupported build */

int waboros_engine_new(const waboros_compiler *c /* NULL: none */, waboros_engine **out, waboros_error *e);
int waboros_engine_set_collector(waboros_engine *, const uint8_t *wbo, size_t n, waboros_error *e);
int waboros_module_load(waboros_engine *, const uint8_t *b, size_t n, waboros_module **out, waboros_error *e);
int waboros_store_new(waboros_engine *, const waboros_store_config *, waboros_store **out, waboros_error *e);
int waboros_instantiate(waboros_store *, waboros_module *, const waboros_extern *imports, size_t n,
                        waboros_instance **out, waboros_error *e);
int waboros_export(waboros_instance *, const char *name, size_t len, waboros_extern *out);
int waboros_call(waboros_store *, const waboros_extern *func, uint64_t *slots, size_t nslots, waboros_error *e);
void waboros_store_interrupt(waboros_store *);           /* any thread */
/* host values and exceptions at the boundary: */
uint64_t waboros_extern_word(waboros_store *, void *host);  /* (h << 2) | 2, part 3 */
void    *waboros_extern_host(waboros_store *, uint64_t word);
int      waboros_exception(waboros_store *, uint64_t *tag_id, uint64_t *slots, size_t n); /* after status 14 */
```
* `waboros_extern` is a kind and a pointer or a record address: a function is the address of its
  record, as everywhere in part 3. Globals, memories and tables are the loader's objects.
* Values go in and out as part-3 slots (`frame[0..n)`): an i32 zero-extended, a v128 in two
  slots, references as words. `waboros_call` writes the results over the arguments.
* `waboros_store_config`: `max_wasm_stack` (default 512 KiB, the Python runtime's
  `DEFAULT_BUDGET`) and the value-stack size (1 MiB, `VSTACK_BYTES`).
* A host function is `int32_t f(void *env, uint64_t *slots)`. It writes its results, then
  returns its status: 0, a public trap code, or 13 with the message in the store. Part 3's
  `int32_t f(uint64_t *ctx, uint64_t *slots)` carries no `env`, and the context alone does not
  say which import was called. So each import word points to a **dispatcher**: one of a fixed
  pool of C functions made by the preprocessor, each bound to one `(f, env, nparams, nresults)`
  at instantiation (Q4). The dispatcher does what `Instance._host` does: it sets the store's
  next frame to `slots + max(n, m)` (the nested-entry rule), calls `f`, and writes
  `ctx[HOST_ERROR]` last (D34). An import from another instance of the store is its record and
  calls it directly, with no dispatcher.

### The compiler seam (`compiler.h`)
```c
typedef struct waboros_compiler {
    uint16_t e_machine;                  /* the loader's own architecture */
    const char *compiler;                /* its exact `compiler` string (S0014 header) */
    /* wasm bytes -> a thin v1 file; bounds: 0 explicit, 1 guard (+ guard_log2) */
    int  (*compile)(void *self, const uint8_t *wasm, size_t n, uint32_t flags,
                    uint8_t **out, size_t *out_n, waboros_error *e);
    void (*release)(void *self, uint8_t *out);
    void *self;
} waboros_compiler;
```
* **The output is a file**, and the loader reads it with the same strict reader as any other
  file. So there is one path, and the seam is exactly what the wasm build of the same compiler
  exports (`compile(in_ptr, in_len) -> (out_ptr, out_len)`, MICRO-COMPILERS.md, requirement 1).
  Writing and parsing a file costs a little (measured for Python in BENCHMARKS.md,
  "Instantiation from an artifact"; to be measured for C). An in-memory handoff would be a
  second path and is not added without a measurement.
* The embedded compiler's blob is the first copy for its compiler string, like any file's
  (below). The compiler validates the wasm itself (a foreign host must not need its own
  validator, MICRO-COMPILERS.md).
* With the build option `WABOROS_WITH_COMPILER=ON`, `waboros_engine_new` gets the build's
  compiler by default.

### The GC collector: three builds (maintainer, 2026-11-24)
The collector is `runtime/gc/collector.wasm` (D54, D61), an ordinary module. It imports the
store's heap as `gc.heap` and exports `begin`, `roots`, `finish` and `is_subtype`. In the C
loader it takes one of three paths:
1. **With the embedded compiler** (`WABOROS_WITH_COMPILER=ON`, `WABOROS_WITH_GC=ON`): the loader
   carries `collector.wasm` as data. It compiles the collector with the embedded compiler on
   first need, once per engine and bounds mode, and keeps it. "First need" is the Python
   runtime's (S0016, "Runtime and loader"): the instantiation of a module with `subtype` 1, which
   writes the `IS_SUBTYPE` record, or the first collection. The bytes of `collector.wasm` are
   committed already (D54). Embedding them needs a C array in the tree (Q5).
2. **Without a compiler** (`WABOROS_WITH_COMPILER=OFF`, `WABOROS_WITH_GC=ON`): the collector is
   **used only as an AOT file**. It is a `.wbo` of `collector.wasm` for the loader's
   architecture, from any compiler of that architecture: D68's wasm build on a PC, or today
   `jit-x86_64`, `jit-aarch64` or `jit-sljit-*`. The embedder supplies it in one of two ways:
   * at run time, with `waboros_engine_set_collector` (a file read by the caller);
   * at build time, with `WABOROS_COLLECTOR_WBO=<path>`, which embeds the file's bytes in the
     library as data (Q5).
   Either way the file goes through `waboros_module_load` like any other: the same checks, and
   the same one-copy-per-compiler-string rule. The loader checks that the file is the collector:
   its `module_sha256` equals the SHA-256 of the committed `collector.wasm`, which the build
   records as a constant. Its exports are checked too. **When a module needs the GC heap**
   (`.waboros.context` `heap` 1) **and no collector file is present**, `waboros_instantiate`
   refuses it before anything of the instance is in the store, with code 8 and the message
   "module needs the GC collector: none supplied (build without a compiler: supply its AOT file)".
   The refusal comes at instantiation, not at the first collection, so a program fails at once
   and not in the middle of a run. A module without the heap never needs the collector.
3. **Without GC** (`WABOROS_WITH_GC=OFF`): the build has no collector, no GC heap, no GC services
   and no walk-driven collection. A file with `heap` 1 is refused at load (code 8: "built without
   GC"). This covers every module with GC types or instructions, a function-subtype
   `call_indirect`, a `throw` or a `try_table` with `catch`/`catch_ref`: exceptions live in the
   GC heap (S0013 step 6). A module with only `throw_ref` and `catch_all(_ref)` has no heap and
   runs (ARTIFACT.md part 3, the GC heap row).

## Semantics

### What is contract and what is runtime-private
The C loader reproduces the **contract** exactly:
* the format: S0014 in full, including the check order and "Known limitations of v1";
* the context ABI: ARTIFACT.md part 3, every word and the order of the variable part;
* the boundary: S0015, including the `entry` contract (its five steps), the walk interface and the
  guard handler contract;
* the heap's code contract: heap offsets 0-15, the object, type-info, function-type and
  exception record formats, the reference words, the RTT words.

It **chooses** what part 3 calls the runtime's own, and copies the Python runtime's choice
wherever a spec command would otherwise see a difference:

| runtime-private | Python's choice (the oracle) | the C loader |
|---|---|---|
| memory placement | i32: 4 GiB + 32 MiB reserved, committed on grow, never moves; i64: reservation, moves on a grow past it (D50; `instance.Memory`) | the same on 64-bit Linux |
| memory limits | 65 536 pages (i32), `MAX_PAGES64`; a failed commit makes `memory.grow` return −1 | the same numbers |
| table storage, limit | element words reallocated on grow; at most 10 000 000 elements (`TABLE_LIMIT`, D27) | the same |
| value stack | 1 MiB per store (`VSTACK_BYTES`) | the same |
| native budget | `min(max_wasm_stack, half the stack left)`, 128 KiB when the bounds are unknown (`native_budget`) | the same, from `pthread_getattr_np` |
| `sp_stub` | assembled and called once per outermost entry | **none**: the address of a local in `waboros_call`, minus 1 KiB. The headroom rule (S0015) gives more slack than that difference, and the budget is policy |
| `entry` | assembled per target (`entry.py`); each target seals its own copy | one copy per process, the bytes of ARTIFACT.md's listings; the walk's entry points are that one return point |
| trampolines of a file | never executed: the runtime's own `conventions()` (S0014, "The waboros runtime") | **executed**: one copy per compiler string per engine, from the first file of that string; every later file's blob and record for the running convention compared byte for byte, the others only checked well-formed (S0014, "A foreign host") |
| type ids, tag ids | process-wide registries from 1 (`type_id`, `_TAG_IDS`) | per engine, from 1, below 2³² (part 3's 32-bit provision costs nothing here) |
| the GC heap regions | type area, tid map at 60 MiB, `ROOTS`, the handle report, semispaces from 64 MiB, committed apart (D69) | the same layout: `collector.wasm` reads it (part 3, "The GC heap") |
| host values | `(h << 2) \| 2`, handles from 1, lowest free number reused, at most 2³⁰ − 1; freed after a collection that did not report them | the same |
| interrupt | `INTERRUPT` set in every context of the store; the outermost entry that returns 12 clears every context's word | the same |
| a lost collection | the store refuses every later call (D61) | the same |
| the guard handler | machine code emitted by the runtime (S0006) | a C function. The S0006 contract (which faults are ours, the PC change only, chaining to the previous action, `raise` for a sent signal) is the same. Everything it reads is written before it is published, as S0006's registry is |

### Loading (S0014 checks 1-6)
1. The order of S0014, "Order of checks", each failure with its code and a message that names the
   field and both values.
2. **Compatibility** (check 5), the foreign-host rule (S0014 Q3; D70):
   * `e_machine` is the host's;
   * `format_version` is 1 and `ctx_abi_version` is 1;
   * `isa_level` is 1 and `isa_features` is 0. In v1 no feature bit is defined, and the baseline
     needs no detection on x86-64 or on Linux aarch64;
   * every `flags` bit is known; `BOUNDS_GUARD` only with the handler installed and a
     `GUARD_LOG2` of at most 25;
   * every `features_required` bit is known (none in v1);
   * the `compiler` string is well-formed (four non-empty tokens). Its value is not compared. It
     keys the trampoline copy.
3. **Thin and fat** (D18):
   * a thin file is refused at the first failing check;
   * a fat file that fails checks 2-6 is compiled from `.waboros.wasm` (after `module_sha256` is
     verified) when the engine has a compiler for the host architecture;
   * without a compiler it is refused with code 7 ("fat file cannot be used as is and this build
     has no compiler");
   * a fat file that passes every check runs as a thin one does, its wasm unused.
4. **Trampolines** (check 6, foreign host): the engine keeps one copy per compiler string, mapped
   executable under W^X (and, on aarch64, with the instruction cache invalidated and `PROT_BTI`
   where the CPU has BTI), created from the first file with that string. A later file must match
   it byte for byte for the running convention; a mismatch is a refusal (fat: compiled, as in 3).
   A copy is never shared between compiler strings (D71), so two strings cannot have one bridge
   address.
5. A module is mapped executable once, when it is first instantiated, and the mapping is shared
   by its instances. `.text` starts at a multiple of 4096 (`adrp`, part 3).

### Instantiation (S0014 check 7; D72)
The order is S0014's step 7 and `JitInstance.__init__`'s:
1. imports matched by kind, type key and declared subtyping, limits and address type (as
   `link.py`); a store mismatch refused;
2. the context written (part 3), and the store's sets of entry and restore points extended;
3. guard registration (code range with its fault map, the memory reservations);
4. RTT words and type-info records, function-type records for the module's non-final or
   subtyped function types, and the `IS_SUBTYPE` record (the collector, path 1 or 2 above);
5. function records, signature words, tag identities;
6. globals in order, then tables with their `init`, then every element segment's items;
7. active element segments in order (bounds, write, drop; declarative ones dropped);
8. active data segments in order (`MEMORY_OUT_OF_BOUNDS` past the memory; earlier writes stay);
9. the start function.

Constant expressions are evaluated on an explicit stack of 128-bit values (S0014, "Constant
expressions"); an allocation follows part 3 and never collects (D61's deviation). The six libcalls
and the seven GC services have the part-3 signature and run in C. `table.init`, `memory.init`,
`elem.drop` and `data.drop` follow the specification's bounds and trap order. A trap during
instantiation leaves the instance's roots registered, as in Python (B6).

### Calls, traps, exceptions
* `waboros_call` follows `Instance.call`:
  * at the outermost entry: reset the value stack and compute `NSTACK_LIMIT`, written into every
    context of the store and the collector's;
  * check the value stack, write the arguments, then `entry(ctx, frame, c_entry)`;
  * a non-zero status becomes the error: a trap code (1-18), 12 `INTERRUPT`, 13 the host's error,
    14 the exception (`waboros_exception` reads the tag identity and the payload slots from the
    object before any wasm runs). Any other value is an internal error;
  * a private status (bit 31) never reaches the host (S0015). If one does, it is an internal
    error.
* **Nesting**: a host function may call `waboros_call` again; the dispatcher's frame rule and the
  entry's saved block make it re-entrant (part 3).

### The walk, roots and a collection (`gc.c`)
* The walk follows `walk.py`, as S0015's walk interface describes. It starts from the exit record
  and steps through each frame by its site's `ret_at`, `up` and `shift`. It crosses fixed points:
  the entry's return point with its saved block, and every compiler's `restore` with its two
  numbers. An unknown return address is an internal error, never a guess.
* Roots, as `collect.py` gathers them:
  * the `live` slots of every frame, with the `refs` slots that `live` does not mark nulled;
  * globals and tables of reference types of the any, extern or exn hierarchy;
  * passive element segments of those types;
  * the pending exception word;
  * the handle table of host-held objects.
* They go into `ROOTS` in batches through `begin`, `roots(n)` and `finish`, on the collector's
  context, whose `INTERRUPT` stays 0. The store is marked lost from the first call until the
  heap's words are consistent again. Host values the handle report does not mark are then freed.
* The walk check of the tests (`WABOROS_JIT_WALK_CHECK=1` in Python) is an environment variable
  of the C loader too: walk at every host call and libcall.

### Executable memory and the handler (`os_linux.c`)
* Executable memory is mapped read-write, written, then made read-execute (W^X, R2). On aarch64:
  `__builtin___clear_cache` over the range (GCC and Clang), and `PROT_BTI` when `AT_HWCAP2` has
  BTI. Address space is reserved with `PROT_NONE` and committed with `mprotect`.
* The guard handler implements S0006's contract on Linux:
  * `SIGSEGV` with `SA_SIGINFO | SA_ONSTACK`, `si_code > 0`;
  * the PC from `uc_mcontext` (`gregs[REG_RIP]` on x86-64, `pc` on aarch64) at a fault offset of
    a registered code range;
  * the fault address inside a registered reservation;
  * then the PC set to `code + resume_offset`;
  * otherwise it chains to the saved action as S0006 says.
  It is installed at the first guard file and checked at every entry, as `guard.check()` does.
  Another copy of waboros in the process (the Python runtime in the test harness) chains the
  same way, so either may be installed first (S0006, "Chain and claim").

## Test plan
All acceptance runs are on Linux glibc x86-64 (CI `jit` or a new `cloader` job) and aarch64
(native `arm64` runner; QEMU with BTI in `jit`).

### The harness (Q3)
Three ways to drive the spec suite were considered:

| option | how | cost | for | against |
|---|---|---|---|---|
| (a) ctypes shim | the loader as a shared library; a Python S0001 engine calls it in-process | ~300 Python lines; `WABOROS_LOADER_SHARED` | `spec_diff` for free; fastest to write | the C loader is never run without Python in the process; ASan in CPython needs `LD_PRELOAD` and leak suppressions; a C crash kills the runner; two fault handlers in one process |
| (b) standalone C runner | a Python tool writes every module as `.wbo` plus the commands in a line format; a C program runs them and prints results | ~600 C lines (a second wast-command interpreter: NaN classes, references, `register`, `assert_exhaustion`) | runs on a device or under QEMU-user with no Python | the runner's semantics written twice; no `spec_diff` without a converter |
| (c) **C server, Python runner** | `waboros_run --serve` reads line commands on stdin (`load <path>`, `instantiate <id> <imports>`, `invoke <inst> <export> <slots>`, `get`, `register`), answers with slots or an error code; `tests/cloader/` is an S0001 engine that compiles each module with a Python target, writes the file and drives the server | ~250 C lines in `waboros_run`, ~250 Python | one runner (`tests.spec.run`) and `spec_diff` unchanged; the C side in its own process (ASan, UBSan, crash isolation, QEMU-user); spectest's imports in C (a dozen functions) | about 10-20 µs a round trip, so about 1-2 s per 64 229 commands (an estimate, to be measured) |

Recommendation: **(c)**. The engine is named `cloader-<target>`, for example
`cloader-jit-x86_64` and `cloader-jit-sljit-aarch64`. It is registered by
`tests/cloader/__init__.py` when `build/native` has `waboros_run`, as `tests/sljit` is (S0016).
Its expected failures are its target's, copied: `tests/spec/xfail/cloader-<target>.txt` (parity,
not incompleteness). A mixed mode, `cloader-mixed-<arch>`, compiles each module alternately with
the Python JIT and `jit-sljit` of the architecture into one store, as `tests/jit/test_mixed.py`
does.

### Acceptance commands
1. `cmake -S native -B build/native -DWABOROS_LOADER=ON && cmake --build build/native && ctest
   --test-dir build/native` gives 0 failures. ctest covers the reader on the committed files, the
   entry bytes, an add and a trap through `entry`, the handler's chaining, and the consumer
   check (`add_subdirectory` and `FetchContent`) with the loader target.
2. The same configure with `WABOROS_SLJIT=OFF` and no submodule checked out builds the loader
   (the no-compiler build needs no sljit).
3. `uv run python -m tests.spec.run --engine cloader-jit-x86_64` and `--engine
   cloader-jit-sljit-x86_64` meet their expected-failures files in guard and explicit mode with
   `WABOROS_JIT_WALK_CHECK=1`; `uv run python tools/spec_diff.py jit-x86_64 cloader-jit-x86_64`
   both ways gives no difference (the Python side with `WABOROS_JIT_ARTIFACT=1`). The same for
   aarch64 natively and for `cloader-mixed-<arch>`; `gc/` and `exceptions/` also with the GC
   switches of S0013.
4. **The refusal corpus** (Q6): `tools/refusal_corpus.py OUT/` writes every case of
   `tests/artifact/test_refuse.py`, `test_strict.py` and `test_review.py` that is a whole file,
   every truncation of `v1-s15-small-<arch>-thin.wbo` and of a GC module, and 3 000 byte flips
   each of CoreMark, a GC module and the small module. Each file is written with Python's verdict
   (accepted / refused / not ours / fat: compiled). `waboros_run --check OUT/` must agree on
   every file. For each accepted file, `waboros_run --dump` and `artifact.read` print the same
   canonical inventory (S0014, "What instantiation reads"), and `diff` is empty. Each file is
   checked in a store that already holds the unmutated file of its compiler string, so the
   foreign-host trampoline compare and Python's `conventions()` compare see the same reference.
5. **The committed files**:
   * `v1-s15-small-<arch>.wbo` and `-thin.wbo` are read and run (the exported add, memory,
     table, global, GC struct global), whatever their compiler string;
   * `v1-small-<arch>-thin.wbo` is refused on the size of `.waboros.trampolines`;
   * `v1-small-<arch>.wbo` (fat) is refused with code 7 without a compiler, and compiled and run
     with one;
   * a copy with `EI_ABIVERSION` 2 is refused.
6. **The three GC builds**:
   * with `WABOROS_WITH_GC=OFF`, a `heap` 1 file is refused at load with code 8, and a
     `throw_ref`-only module runs;
   * without a compiler and without a collector file, a `heap` 1 module is refused at
     instantiation with code 8 and the message above, and the store stays usable;
   * with the collector file supplied at run time, and with `WABOROS_COLLECTOR_WBO`, `gc/` runs
     with the GC switches;
   * a collector file with a wrong `module_sha256` is refused.
7. **CoreMark through a file**: `waboros_run core.<target>.wbo --wiring` (`millis` and `print` in
   C) passes its self-check. Its time, alternated with `tools/run_coremark_jit.py` on the same
   file, is recorded in BENCHMARKS.md as a measurement, not a goal.
8. **Sanitizers**: commands 1, 3 (x86-64, one mode) and 4 under
   `-DCMAKE_C_FLAGS="-fsanitize=address,undefined -fno-sanitize-recover=all"`. ASan must not
   intercept the guard handler's faults: run guard mode without ASan, or with
   `ASAN_OPTIONS=handle_segv=0`.
9. **Fuzzing the reader**: `tests/loader/fuzz_read.c`, a libFuzzer target over
   `waboros_module_load` (no instantiation), seeded from the corpus of item 4. Five minutes in the
   nightly job (`fuzz-nightly.yml`), never blocking. A crash or a sanitizer report is kept as a run
   artifact.
10. `python3 tools/check_docs.py`.

## Plan of steps
Every step stops and reports if it needs more than its size. Steps 2-6 change no contract. A
divergence from Python is a bug in one of them, settled by S0014/S0015 text; if the text does not
settle it, the text is amended first.

| step | goal | agent | acceptance | size (lines) |
|---|---|---|---|---|
| 0 | CMake: `WABOROS_SLJIT`, `WABOROS_LOADER`, an empty `waboros_loader`, the consumer check with it; `native/` configures without the submodule when sljit is not needed | implementer | items 1-2 | cmake ~60 |
| 1 | The reader (`read.c`, `sha256.c`), `waboros_run --check/--dump`, the refusal corpus tool | implementer | item 4 on both architectures' files | C ~1 100, Python ~200 |
| 2 | Executable memory, `entry` bytes tied to `entry.py` by a pytest, the trampoline copies, calls without imports: `--serve` and the S0001 engine; spectest in C | jit-engineer | the MVP directories of the suite through `cloader-jit-x86_64`, explicit mode | C ~900, Python ~250 |
| 3 | Instantiation and linking complete: imports, the dispatcher pool, records, segments, libcalls, memory64, interrupts, stack limits, exceptions without the heap | implementer | the whole suite except `gc/` and heap-using `exceptions/`, both compilers, explicit; `spec_diff` empty | C ~900 |
| 4 | The guard handler on Linux | jit-engineer | item 3 in guard mode; the handler's chaining tests (a foreign fault, a sent signal) | C ~250 |
| 5 | GC: the heap, records, services, walk, roots, the collector driver; the three GC builds | jit-engineer | items 3 (`gc/`, `exceptions/`, GC switches, walk check) and 6 | C ~1 100 |
| 6 | aarch64 (native and QEMU with BTI), mixed stores, the committed files, CoreMark, sanitizers, fuzz job | runner + jit-engineer | items 3, 5, 7, 8, 9 on both architectures | C ~150, CI ~120 |
| 6.1 | Build sizes ("Measurements to take"): four builds × `-Os`/`-O2` × x86-64/aarch64 × LTO on/off, stripped text + data, plus the collector's thin `.wbo` | runner | a BENCHMARKS.md section with the table and conditions; Q7 answered from it | tools ~40 |
| 7 | Docs ("Docs this changes"), DECISIONS entry | orchestrator | `python3 tools/check_docs.py` | — |
| 8 | Review without the implementers' reasoning: the reader against S0014, the trap order of instantiation, the handler, the walk and the collector driver, C99 conformance | reviewer | findings closed | — |

About 4 400 lines of C, 450 of Python and 180 of CMake and CI. This is an estimate from the Python
sizes: `artifact.py` 1 184, `instance.py` 1 374, `heap.py`, `collect.py`, `walk.py`, `gc.py`'s
runtime part, `guard.py` and `execmem.py` about 1 500. C costs more lines per rule; Python's
ctypes plumbing goes away.

The compiler seam is defined in step 0 (the header) but has no implementation until D68's
compiler exists. The fat-file compile path is tested in step 6 with a **test compiler**: a seam
implementation in the test harness that asks the Python compiler through `--serve`. It runs in
tests only.

## Measurements to take
* Load time (read and checks) and instantiation time against Python's, for CoreMark and a GC
  module (BENCHMARKS.md, "Instantiation from an artifact" has Python's).
* CoreMark through the C loader against the Python loader on the same file, alternated. No
  difference is expected (same code bytes); a difference points to a context or memory-placement
  discrepancy.
* The cost of a host call through the dispatcher: the import loop of STATIC-BUILDS.md
  ("Cheap host calls"), C host against a Python host.
* **Build sizes** (the maintainer's question: are the no-compiler and no-GC builds worth having?).
  Measure the stripped size (text + data, `size` after `strip`) of a static `waboros_run` linked
  with the loader, in each of four builds:
  * full (compiler and GC);
  * no compiler;
  * no GC;
  * neither.
  Each build is measured at `-Os` (MinSizeRel) and `-O2`, on x86-64 and aarch64, with LTO
  (`CMAKE_INTERPROCEDURAL_OPTIMIZATION`) on and off. Also record the size of the GC collector's
  AOT file (thin `.wbo`, each architecture, both bounds modes). All of it goes into BENCHMARKS.md
  with the compiler version. Until D68's compiler exists, the "full" and "no GC" rows use S0016's
  `waboros_sljit` as a stand-in for the compiler's sljit part and say so. The core in C is added
  when it exists.
  Inputs already measured by the orchestrator (2026-11-24; sljit 39c508d1, gcc 13, the full sljit
  API built as the shared library):
  * sljit's text: 56 KB on x86-64 and 47 KB on aarch64 at MinSizeRel `-Os`; 90 and 80 KB at
    `-O2`;
  * `collector.wasm` is 1 517 bytes; as a thin `.wbo` it is 11.4 KB on x86-64 and 11.1 KB on
    aarch64, of which code is 3.7 and 3.6 KB;
  * CoreMark: 19.5 KB of wasm gives a thin `.wbo` of 53 KB (x86-64) and 48 KB (aarch64).
  For scale, Python line counts: the front end 1 962; the JIT's common core (compiler, lower,
  liveness, backend, simd) about 1 760; the sljit backend 1 728 (both architectures); the
  loader/runtime side about 4 000 (artifact 1 184, instance 1 374, execmem 315, guard 571, heap
  240, collect 200, walk 138, context). The compiler is therefore expected to be sljit plus a
  C port of about 5 400 Python lines, against about 4 000 for the loader. The measurement says
  what each option removes.
* The `--serve` round trip and the suite's wall time.

## Risks
* **Two implementations of instantiation** (D72's accepted risk): conformance covers it, through
  items 3-5. RUNTIME-MODULES.md item 4 says where re-implementations fail: the order of traps.
* **The handler** (R6): a second handler in the test process. S0006's chaining is the contract,
  and step 4 tests both install orders.
* **The walk and the collector** run against foreign frames: a wrong site number corrupts the heap
  silently. The walk check and poison (S0013) run in item 3.
* **Hidden Python behaviour**: a choice of `instance.py` that is neither contract nor in the
  table above shows up as a `spec_diff` line. It goes into the table, or into part 3 if it is
  contract (the working freeze: a spec or a decision, no bump).
* **Executing a foreign blob** is new: Python never runs a file's trampolines. Step 2's first run
  is the first time a file's bridge and `tail_switch` execute as they are.

## Alternatives considered
* **Reuse WAMR (wasm-micro-runtime) pieces** (its AOT loader, its `os_*` platform layer): its AOT
  format, context and calling convention are its own ([WAMR-INTEROP.md](../WAMR-INTEROP.md)). Its
  platform layer is the only part that could be reused, and it brings a licence and a build of its
  own for about 300 lines we need. No.
* **A loader in Rust or Zig**: D60 chose C (it embeds anywhere, and sljit is C).
* **Generate the C from the Python loader**: the Python loader is ctypes over Python objects. A
  generator would be larger than the loader and would read like neither, while the contract is
  written to be implemented from the text (D36).
* **Instantiation as a wasm runtime module**: rejected by D72.
* **`entry` as `.S` files**: needs `enable_language(ASM)` and MASM on Windows later; byte arrays
  tied to `entry.py` by a test need no assembler. ARTIFACT.md part 1 already plans hex bytes of
  the listings.
* **A separate C runtime for the no-compiler build**: one library with options instead; the seam
  is a struct pointer that is NULL.

## Open questions
Ranked; the first two block step 0; Q7 waits for step 6.1's measurement.

1. **What the C loader is: a test tool or a product.** D36 says "It stays a test tool, not a
   shipped runtime". MICRO-COMPILERS.md "The C variant" and ROADMAP "Next" (6) make the same C
   loader a buildable runtime for devices, with a tool to embed a module and the collector.
   * (a) A test tool now, as D36 says. The C API above is internal, and a product is a later
     decision.
   * (b) A product from the start: a stable C API, versioned, installed with `find_package`.
     That is phase-6 work in C.
   * Recommendation: **(a)**, with D36 amended to say the same sources later become the C
     variant. The API here is shaped so that (b) adds to it and removes nothing.
   * **Decided (maintainer, 2026-11-24): (a) for now.** The C loader is a future product, not
     fixed yet: its C API stays internal (no stability promise, no `find_package`/install) until a
     later decision; D36 gets a marker that the same sources become the C variant and the product.
2. **Rows of the first version** (D47).
   * (a) Linux glibc x86-64 and aarch64 only. The freeze condition names the architectures, not
     the OSes.
   * (b) Also macOS and Windows before the final freeze (exception (a): executable memory there
     is required, not faster; about 300 lines each, plus `entry`'s Win64 listing, which has never
     run on Windows: ARTIFACT.md, "Checked").
   * Recommendation: **(a)**; (b) when an embedder needs it.
   * **Decided (maintainer, 2026-11-24): (b).** macOS (x86-64, arm64) and Windows (x64, ARM64)
     are in scope before the final freeze, next to Linux glibc; each beyond Linux under D47
     exception (a) with its evidence. The Goals, Rows, CMake/CI and Plan of steps are to be
     updated for it (open: whether Linux musl and Android come too).
3. **The test harness**: (a), (b) or (c) of "The harness". Recommendation: **(c)**.
   * **Decided (maintainer, 2026-11-24): a compromise.** The spec suite and the refusal corpus
     drive the C loader in-process through a thin ctypes shim (an S0001 engine `cloader-<target>`,
     so `spec_diff` works unchanged; each `.wast` file already runs in its own child process, so a
     crash costs one file); and a small C tool `waboros_run` (load a file, call an export; no
     `--serve` mode) runs CoreMark and the committed v1 files under ASan/UBSan in CI with no Python
     in the process, which also covers the guard handler without Python's own handlers beside it.
     `waboros_run` is the seed of the product's CLI. A C wast runner (option (b)) comes later,
     together with the embedded micro-compiler, when the whole path (compile and run) can be
     tested without Python. "The harness", the Test plan and the Plan of steps are to be revised.
4. **Host functions without closures**.
   * (a) A fixed pool of preprocessor-made dispatchers (`WABOROS_HOST_SLOTS`, default 4 096 per
     process), each bound at instantiation to `(f, env, n, m)`.
   * (b) A per-import machine-code thunk that loads `env`: per-architecture bytes like `entry`.
   * (c) The raw part-3 signature only. The embedder then cannot tell imports apart, nor tell
     the loader where a nested entry's frame starts.
   * Recommendation: **(a)**: portable C, it gives the nested-frame rule its `n`, and a limit
     that is a build option.
5. **How a `.wbo` or `collector.wasm` becomes data in the build.** S0016 says "No Python, no
   generated sources".
   * (a) CMake generates a `.c` byte array at configure time (`file(READ … HEX)`); this is
     CMake, not Python, but it is a generated source.
   * (b) A committed `.c` array of `collector.wasm`, tied to the `.wasm` by a test (D54's
     pattern). This covers path 1 only: a collector `.wbo` depends on the compiler string and
     cannot be committed.
   * (c) Run time only (`waboros_engine_set_collector`).
   * Recommendation: **(b)** for `collector.wasm`, **(a)** for `WABOROS_COLLECTOR_WBO`, with
     S0016's rule read as "no generator outside CMake".
6. **Agreement on refusals: how fine.**
   * (a) Accept or refuse per file, plus the dumped inventory of every accepted file.
   * (b) Also the check number of the first failure (S0014's order), which Python's
     `ArtifactError` does not carry today (about 40 lines in `artifact.py`).
   * Recommendation: **(a)** now; (b) if a disagreement on order ever matters to a host.
7. **Are the no-compiler and no-GC builds worth having?** The maintainer decided both options, and
   this question asks only whether to keep them once their size is known. It is decided **after
   step 6.1's size measurement**.
   * No compiler: kept unless the measurement shows it removes little. Expected to remove sljit
     (47-90 KB of text by the inputs above) plus the C core. It is also the build that runs where
     no compiler fits or none is wanted (cross-compiled AOT files only).
   * No GC: free to provide only if `WABOROS_WITH_GC=OFF` is just the omission of `gc.c` and a
     refusal at load. Its cost is one more CI build row and the `#if`s at the GC service words and
     the walk. Its gain is `gc.c` (about 1 100 lines expected) plus the collector's 11 KB AOT
     file.
   * Options:
     * (a) keep both;
     * (b) keep no-compiler and drop no-GC if it saves less than an agreed size (for example
       10 % of the no-compiler build);
     * (c) drop both.
   * Recommendation: **decide after the measurement**. Until then the step plan builds no-GC as
     the omission of one file, nothing more. If that turns out to need more than that, the option
     waits for the decision.

## Docs this changes
When approved and implemented:
* [ARTIFACT.md](../ARTIFACT.md): part 1's "Planned: hex bytes of each listing" points to
  `native/loader/entry.c`. Part 2's "A loader refuses … whose `compiler` string is not the
  store's" names the waboros loader and adds the foreign-host rule of S0014. The last
  "Versioning rules" bullet names this spec.
* [DECISIONS.md](../DECISIONS.md): an entry recording the decided questions (Q1-Q6). D36 is
  amended by Q1's answer.
* [S0014](S0014-artifact-format.md): Changes line (the C loader reads the format; any reader
  divergence found and closed).
* [S0006](S0006-guard-pages.md): the row table gains the C loader's handler (Linux glibc, both
  architectures).
* [TESTING.md](../TESTING.md): the `cloader-*` engines, the refusal corpus, the C reader's fuzz job.
* [ROADMAP.md](../ROADMAP.md): "Next" (6)(b) done.
* [MICRO-COMPILERS.md](../MICRO-COMPILERS.md): "The C variant" links the seam.
* [BENCHMARKS.md](../BENCHMARKS.md): the measurements above.
* CLAUDE.md "Commands": the loader's build and its spec engine.
* [LESSONS.md](../LESSONS.md): any trap found on the way.

## Changes
* 2026-11-24: created (draft).
* 2026-11-24: the three paths of the GC collector stated explicitly (maintainer's clarification):
  compiled on first need with the embedded compiler; only an AOT file without one, with a refusal
  at instantiation when none is present; no collector and no heap without GC.
* 2026-11-24: build sizes of the four builds as a measurement (step 6.1), with the orchestrator's
  inputs; Q7: whether the no-compiler and no-GC builds stay is decided after it.
* 2026-11-24: Q1 decided (maintainer): a test tool for now with an internal API; the C loader is a future
  product, not yet fixed.
* 2026-11-24: Q2 decided (maintainer): (b), macOS and Windows before the final freeze.
* 2026-11-24: Q3 decided (maintainer): ctypes for the spec suite and the refusal corpus, plus `waboros_run`
  under sanitizers without Python; the C wast runner later, with the embedded compiler.
