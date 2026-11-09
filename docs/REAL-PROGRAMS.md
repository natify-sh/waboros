---
title: Real programs beyond CoreMark - a corpus, the scalar gap, and work that can run beside phase 5
kind: reference
status: draft
updated: 2026-11-05
---

# Real programs beyond CoreMark: a corpus, the scalar gap, and parallel work

A note from the maintainer's discussion, for the orchestrator. **Nothing here is approved for
implementation.** It records three items that so far exist only in conversation, and which of them
can run in parallel with phase 5 because they touch none of its files (`jit/common/compiler.py`, the
backends, the artifact format).

## What is recorded today and what is not

| item | in the docs |
|---|---|
| differential fuzzing with `wasm-smith` against Wasmtime | yes: ROADMAP phase 5 (a nightly non-blocking CI job, findings triaged) and its exit criteria, TESTING, RISKS. `tools/fuzz.py` exists since S0010 step 0 (2026-11-05), comparing `pyc` with `pyc` + `bropt.py`; the comparison with the JITs and Wasmtime and the CI job are not built yet |
| a corpus of real programs with a check that needs no Wasmtime | yes since 2026-11-02 (section 1): `poc/bench/corpus/`, `tools/run_corpus.py`, TESTING, BENCHMARKS |
| why scalar code on `jit-x86_64` is 1.5-3.9x behind Wasmtime | a measured fact (BENCHMARKS, SIMD benchmark: "The scalar builds on `jit-x86_64` are 1.5-3.9x behind Wasmtime's scalar builds"); its work is [S0007](specs/S0007-scalar-codegen.md) (scalar code generation): implemented: steps 1 and 3 landed; step 2 reverted; steps 1b, 5 and 7 measured and dropped; steps 4 and 6 not done |

What runs today besides the spec suite: CoreMark, the corpus (`poc/bench/corpus`), meld-core without WASI
(`poc/bench/meld`), the SIMD kernels (`poc/bench/simd`), the calls benchmark (`poc/bench/calls`), `vm.wasm`
(`poc/bench/switch`). Nothing with WASI.

## 1. A corpus of freestanding libraries (no WASI)

Programs that do real work, build with the same toolchain as the SIMD kernels
(`clang --target=wasm32 -O3 -nostdlib`, a small `malloc` inside where needed), and whose results can
be checked **without Wasmtime**, against the Python standard library:

| library | work | checked against |
|---|---|---|
| Monocypher (needs no libc) | BLAKE2b, X25519, ChaCha20-Poly1305 | `hashlib.blake2b`; known test vectors; Wasmtime |
| miniz or zlib's inflate/deflate | compression | `zlib.compress` / `zlib.decompress` round trips |
| SHA-256, xxHash, BLAKE3 (portable C) | hashing | `hashlib.sha256`; reference vectors |
| QOI, stb_image (decoding) | image decoding | the reference decoder's output, checksums |

Why these:

* This is the main use case of the project: wasm as a replacement for a native Python extension.
* They stress what CoreMark and the spec suite do not: i64 arithmetic, shifts and rotates, tight
  loops over memory, larger functions.
* The results are self-checking, so they also run where Wasmtime is absent (QEMU jobs, Android).

Layout, as the SIMD kernels: `poc/bench/<name>/` with the sources or a pinned fetch, a `build.sh`,
the built `.wasm` committed, a runner that checks the results on every target and prints timings
(alternated rounds, median, as `tools/run_simd_bench.py`). The same modules join the differential
run below.

Status (2026-11-09): built, in `poc/bench/corpus/` with `tools/run_corpus.py` (results in BENCHMARKS,
"Corpus"): Monocypher (BLAKE2b, X25519, XChaCha20-Poly1305), SHA-256, BLAKE3, miniz (deflate, inflate).
xxHash (XXH32/64/3, checked against vectors of the reference build) and QOI (checksum of the reference
decoder, byte-identical re-encoding) added 2026-11-09. Not yet: stb_image. A C++ program with
`-fwasm-exceptions` was tried and not built: clang 18.1.3 emits the legacy opcodes (`try` 0x06; no option for
`try_table`/`exnref`, `-mllvm -wasm-use-legacy-eh=false` is unknown to it), which are not planned (S0012,
Q8); it also needs libc++abi's `__cxa_*` and the personality, absent freestanding. Sources are fetched with pinned sha256 (not vendored); licences in
`poc/bench/corpus/licenses/`.

## 2. Why scalar code is 1.5-3.9x behind Wasmtime (investigation, no code change)

The same method as the SIMD slowdown investigation (BENCHMARKS, LESSONS 44): hot-loop disassembly of
both engines (`WABOROS_JIT_DUMP` + `objdump`; Wasmtime from the ELF of `Module.serialize()`),
instruction counts by category, and controlled experiments as uncommitted patches whose checksums
must equal the baseline's.

Inputs: the scalar builds of the six SIMD kernels (already measured), CoreMark, and the corpus of
section 1.

Questions to answer with data:

* How much is **bounds checks**? The SIMD investigation's `B` experiment (no bounds checks at all)
  changed little on these kernels, so guard pages (S0006, done: +9 % CoreMark on x86-64) may not
  close this gap; measure on the scalar builds directly.
* How much is **register allocation**: pinned locals versus spills to slots; values live across
  loop back-edges; the lazy value stack flushing at branches.
* How much is **instruction selection**: address modes (`[base + index*scale + disp]` versus separate
  adds), compare-and-branch fusion, `lea` for arithmetic, zero-extension moves after 32-bit ops.
* How much is **loop shape**: back-edge polling of the interrupt word (cost per iteration), block
  layout, loop alignment.
* **Store forwarding and partial registers** (the SIMD lesson): narrow writes read wide, `setcc`
  without a zeroing idiom.

Output: a ranking by gain per complexity, as the SIMD investigation gave, and decisions for the
maintainer. The ranking decides what goes into phase 5 after guard pages, or later; S0007 took the first items.

## 3. Differential fuzzing and the corpus together

Infrastructure beside the code, not in it:

* `wasm-tools smith` modules on every target and on Wasmtime; results compared value by value, NaN
  lanes by class (TESTING, S0005); a divergence is kept and triaged (ROADMAP, "Next"); one that is fixed
  is minimised (`wasm-tools shrink`) and becomes a test.
* The corpus of section 1 and, once WASI is bound (below), real programs, run the same way.
* A nightly CI job with a time budget, never blocking, and its findings triaged before each large spec
  and phase 5 close (ROADMAP phase 5 exit criterion: "every divergence found by the nightly differential
  fuzzing is triaged: fixed, or recorded as a known deviation").

## 4. WASI through fusedwasi, beside phase 5

fusedwasi 0.1.0a1 (PyPI, checked 2026-11-02) does not depend on any runtime object: its Python twin
imports only the standard library; every WASI import is a flat function `fn(guest, ...)` over
`Guest(state, memories, realloc=..., trap=..., exit=..., wait=...)`. A memory is a writable buffer,
a callable returning one, or an object with `read`/`write` and a size. WASI 0.1 is built on the
0.2 host, so one binding serves both.

So binding it needs only the internal engine interface (`instantiate(module, imports)` with
`partial(fn, guest)` per import, and a call of the guest's `cabi_realloc` for p2), **not** the
public wasmtime-py-shaped API of phase 6. This opens the corpus to programs that need WASI: Lua,
QuickJS, PolyBench/C, SQLite, CPython-wasi. And because fusedwasi also runs on Wasmtime, the
differential run uses **the same host on both engines**, so a divergence is the engine's, not the
host's.

**One WASI state per Store, one guest per instance.** fusedwasi has the same two levels as
Wasmtime, and the binding keeps them apart from the start, even before the Store exists:

| Wasmtime | fusedwasi | how many |
|---|---|---|
| `WasiCtx` in the `Store` (`Store.set_wasi(WasiConfig)`) | `State`: args, env, stdio, preopens, descriptor tables, clocks, random | **one per Store** |
| the `Caller`'s instance and its memory | `Guest`: the instance's memories, `cabi_realloc`, the hooks `trap` / `exit` / `wait` | **one per instance** |

`Guest`'s docstring says so: "One for each instance, made once; many guests may point to one state".
So every instance of a Store sees one descriptor table, one stdout and the same preopens: a file
opened by one module is the same fd in another, as in Wasmtime.

Rule for the binding written now:

* the `State` is created outside the binding (today by the harness that plays the Store; later by
  `Store.set_wasi(config)`), and the binding **takes** it;
* the binding creates one `Guest(state, memory)` per instance when it is instantiated, and binds
  each import to `partial(fn, guest)` (`fusedwasi.signatures.find_import(module, name)` gives the
  function); the memory is the target's own (`pyc`: the `bytearray`; JIT: a `memoryview` over the
  mapping, or a callable returning it, so `memory.grow` needs no rebind);
* in phase 6 the binding moves into `Linker.define_wasi()` and the `State` into
  `Store.set_wasi()`, unchanged;
* integers: fusedwasi expects unsigned values (README); the binding masks signed i32/i64 arguments
  where a target passes them signed;
* no locking: one thread per Store (ARCHITECTURE.md, "Long calls and the GIL"), so a `State` shared by
  the instances of one Store is only ever used from one thread.

To decide (ROADMAP q2, D8): fusedwasi as an optional extra (`waboros[wasi]`), vendored, or allowed as
a dependency; the cost of p1 over p2 on a hot `fd_write` loop (measure).

## What can run in parallel with phase 5, and what cannot

| item | parallel? | why |
|---|---|---|
| QEMU smoke of `pyc` on s390x, riscv64, ppc64le | yes (exists: the optional exotic jobs of `platforms.yml`, on `workflow_dispatch`) | CI only; s390x is big-endian and finds byte-order bugs that x86 and ARM never show |
| corpus (section 1) and its runner | yes | `poc/bench/` and `tools/` only |
| scalar-gap investigation (section 2) | yes | no committed code change |
| fuzzing infrastructure (section 3) | yes | `tools/`, CI |
| WASI binding through fusedwasi (section 4) | yes, after the maintainer's decision on the dependency | a new `wasi` area over the internal engine interface |
| micro-compilers, the C reference loader | no | wait for the fixed artifact format (D36) |
| a third architecture (riscv64, D48) | no | the backend interface still changes (GC; the S0011 low layer and exceptions are done); it comes as a micro-compiler, after `arm64.wasm` |
