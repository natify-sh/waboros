---
title: Benchmarks
kind: log
status: current
updated: 2026-11-02
---

# Benchmarks

Everything measured during the PoC, with its conditions. Numbers from different sessions differ by up
to ~20 % (shared cloud machine); compare within a session.

## Corpus of freestanding libraries (`poc/bench/corpus/`)

Real libraries compiled to wasm (REAL-PROGRAMS section 1): Monocypher 4.0.2 (BLAKE2b, X25519,
XChaCha20-Poly1305), SHA-256 (B-Con crypto-algorithms), BLAKE3 1.5.4 (portable C), miniz 3.0.2
(deflate, inflate). Built by `poc/bench/corpus/build.sh`: Ubuntu clang 18.1.3, wasm-ld 18,
`--target=wasm32 -O3 -nostdlib -ffunction-sections -fno-builtin -Wl,--gc-sections -Wl,-z,stack-size=1048576`
(BLAKE3 with its SIMD dispatch off, miniz without archive/stdio/time); sources are fetched from
raw.githubusercontent.com and checked against the pinned sha256, the build is byte-reproducible. A driver
per library exports one function per operation plus `put`/`get` (8 bytes per call) over a static I/O buffer
and a 1 MiB bump `malloc`. Runner: `uv run python tools/run_corpus.py --rounds 3`. Every result is
compared with `hashlib` (BLAKE2b, SHA-256), `zlib` (miniz deflate output decompressed by `zlib`, `zlib`
output inflated by miniz), the BLAKE3 test vectors (`test_vectors.json`, 1.5.4) or a pure-Python reference
(RFC 7748 ladder for X25519; XChaCha20-Poly1305 built from the RFC 8439 pieces), on `pyc`, `jit-x86_64`
and Wasmtime: 63 of 63 agree; on `jit-aarch64` under QEMU (`python3 tools/run_arm64.py
tools/run_corpus.py --targets jit-aarch64 --rounds 1`) 21 of 21 agree (no timings).

Conditions: 2026-11-02, the machine below, CPython 3.13.17, wasmtime-py 50.0.1, one session, 3 rounds
alternated, median; microseconds per call of the export (the input transfer is not timed; `pyc` 1
repetition per call, the others 20).

| case | pyc | jit-x86_64 | Wasmtime |
|---|---|---|---|
| BLAKE2b, 1 KiB | 1 117 | 3.3 | 4.5 |
| X25519 (scalar mult) | 59 110 | 182 | 117 |
| XChaCha20-Poly1305, 1000 B | 2 452 | 5.2 | 5.6 |
| SHA-256, 4 KiB | 15 754 | 44.1 | 37.1 |
| BLAKE3, 1 KiB | 1 200 | 4.4 | 3.9 |
| BLAKE3, 8 KiB | 9 787 | 22.2 | 17.3 |
| inflate, 8 KiB (to 8 KiB) | 7 598 | 45.5 | 38.2 |
| deflate, 8 KiB | 62 337 | 487.6 | 364.6 |

The i64-heavy X25519 is 1.55x behind Wasmtime on `jit-x86_64`, the 32-bit hashes and the byte loops of
miniz 1.1-1.4x; BLAKE2b and the AEAD are on par.

**Machine**: cloud container, x86-64 Linux, Intel Xeon @ 2.10 GHz, 2 vCPU.
**Software**: CPython 3.13.17 (also 3.8.20, 3.9.25, 3.10.20 from uv), PyPy 7.3.19 (3.10), MicroPython
1.29.0 unix port (standard build), wasmtime-py 50.0.1, pywasm3 0.5.0, pywasm 2.2.3, lupa 2.8 (Lua 5.4,
5.5, LuaJIT 2.1), mypy/mypyc 2.4.0, warpy (kanaka, 2019) translated with RPython from PyPy `main` under
CPython 2.7.18.

**Workload**: CoreMark 1.0, `poc/in/core.wasm` (zig cc wasm32 -O3, 19 KB); it/s as CoreMark prints it.
Every run listed passed CoreMark's own self-check (crclist 0xe714, crcmatrix 0x1fd7, crcstate 0x8e3a;
crcfinal depends on the iteration count).

## CoreMark

| engine | it/s | notes |
|---|---|---|
| wasmtime (Cranelift) | 16 589 / **19 044** | first / last session |
| **w2jit2: stdlib JIT with registers, CPython 3.13 / 3.8** | 15 088, **15 337 / 15 437** | last session; ~80 % of wasmtime, 6.3× wasm3 |
| **package `jit-x86_64`** (step 3c: one-pass core, explicit bounds checks, stack checks, interrupt polls, trap-by-return) | 16 299 / 16 315 / 15 100 | 2026-10-30, alternated with `w2jit2` in one session: 16 949 / 16 023 / 16 411 (medians 16 299 vs 16 411, −0.7 %, within noise); details below |
| package `jit-x86_64` (step 3d: + tables, references, `call_indirect`, bulk memory) | 16 571 / 15 385 / 15 879 | 2026-10-30, alternated with `w2jit2` in one session: 16 058 / 15 819 / 16 569 (medians 15 879 vs 16 058, −1.1 %, within noise); details below |
| package `jit-x86_64` (step 5: + tail calls, function records; the ISA gate) | 17 663 / 17 526 / 17 648 / 16 513 / 17 231 | 2026-10-31, alternated with `w2jit2` in one session: 17 662 / 17 817 / 18 053 / 17 073 / 17 324 (medians 17 526 vs 17 662, 99.2 %); details in "Package JIT checkpoint (JIT step 5)" |
| w2jit: stdlib JIT, every value in memory | 3 874–3 898 (3.13), 3 882 (3.8) | |
| w2py → PyPy 7.3.19 | 2 873; 2 300–2 900 later | noisy |
| wasm3 (pywasm3) | 1 850–2 035 / **2 433** | first / last session |
| w2luajit → LuaJIT 2.1 | 1 873 | was 67 with `goto`-based loops |
| w2py --mypyc → native extension | 1 734–1 814 | needs `librt` at run time (dropped) |
| w2lua → Lua 5.4 / 5.5 | 271 / 246 | was 244 with `goto`-based loops |
| warpy (RPython, meta-tracing JIT) | 186–193 | MVP-only; patched for 4 post-MVP ops |
| w2py → CPython 3.13 | 78 → 86 → ~100 | first PoC → codegen optimisations → last run |
| package `pyc` (step 5b: + SIMD, S0005) | 110.4 / 107.6 | 2026-11-01, alternated with the code before the step in one session: 107.6 / 107.0 (before) vs 110.4 / 107.6 (after); the generated source of CoreMark is byte-identical, so the change is noise |
| w2py → CPython 3.8 / 3.9 / 3.10 | 86.6 / 81.9 / 82.6 | `--legacy` (no `:=`) on 3.8: 89 |
| w2py → MicroPython 1.29 | .py 66 · .mpy 69 · `@native` 94 | native .mpy needs the big printf function left as bytecode |
| w2py `--trust-align` (memoryview casts) | CPython 74 · PyPy 137 | rejected |
| pywasm 2.2.3 | CPython 0.58 · PyPy 6.1 | PyPy run < 10 s (formally invalid for CoreMark) |

### Package JIT, first CoreMark checkpoint (JIT step 3c)

Conditions: 2026-10-30, the same cloud machine type but 4 vCPU, CPython 3.13.17, CoreMark self-check
passed in every run (crcfinal 0x4983). Runner: `python3 tools/run_coremark_jit.py poc/in/core.wasm` (the package API; host
`millis`/`print` through the host bridge), alternated round by round with `python3 poc/run_jit2.py poc/in/core.wasm`.

| round | package `jit-x86_64` | `w2jit2` (PoC) | package, no `test eax; jnz` after wasm calls |
|---|---|---|---|
| 1 | 16 299 | 16 949 | 16 853 |
| 2 | 16 315 | 16 023 | 15 307 |
| 3 | 15 100 | 16 411 | 15 640 |
| median | **16 299** | 16 411 | 15 640 |

* Trap-by-return (S0003: `test eax, eax; jnz unwind` after every wasm → wasm call) has no measurable
  cost: the build without it (a local patch, not kept) is not faster; the run-to-run noise is ~±5 %.
* Compile: 61–67 ms, 45 396 bytes of code (`w2jit2`: 28–29 ms, 39 357 bytes). The package adds
  stack-limit checks and an interrupt poll per function, the value-stack frame and the reload of the
  memory registers after host calls.

### Package JIT, second CoreMark checkpoint (JIT step 3d)

Conditions: 2026-10-30, 4 vCPU cloud machine, CPython 3.13.17, CoreMark self-check passed in every
run (crcfinal 0x4983 / 0xcc42 by iteration count). Same commands as the first checkpoint, alternated
round by round: `python3 tools/run_coremark_jit.py poc/in/core.wasm`, then
`python3 poc/run_jit2.py poc/in/core.wasm`.

| round | package `jit-x86_64` | `w2jit2` (PoC) |
|---|---|---|
| 1 | 16 571 | 16 058 |
| 2 | 15 385 | 15 819 |
| 3 | 15 879 | 16 569 |
| median | **15 879** | 16 058 |

* CoreMark has no table, `call_indirect` or bulk-memory instruction, so this checks that step 3d
  costs nothing elsewhere: the code is byte-for-byte the size of step 3c's (45 396 bytes, data area
  from 44 872); compile 60–64 ms. The −1.1 % is within the run-to-run noise (~±5 %).

### Package JIT: function imports through their records (JIT step 3f, D32)

Conditions: 2026-10-30, Intel Xeon @ 2.10 GHz, 4 vCPU cloud machine, CPython 3.13.17. HEAD
(the `src/` of the commit that added the one-time JIT fallback warning (step 4, 2026-10-30), exported to a scratch directory and put first on `PYTHONPATH`) and the change,
alternated round by round in one session.

* **1e6 calls of an import from a wasm loop** (a scratch script: `(call $f (local.get 0))` in a
  `loop`, best of 5 runs per round, ns per iteration). A host function (`lambda a: [a[0] & 1]`):
  HEAD 1545 1469 1466 1449 1497 1628 1553 1472 1597, new 1602 1570 1617 1589 1490 1635 1475 1758
  1444: minima 1449 / 1444, medians 1497 / 1589 (the first three rounds) and 1525 / 1540 (the
  last six); no difference beyond the noise of the ctypes callback. The same loop calling a
  function of another JIT instance: HEAD 4757–5057 (through the host bridge and a nested entry),
  new **3** (a direct call with a context switch).
* **CoreMark** (`tools/run_coremark_jit.py poc/in/core.wasm`, self-check passed in every run,
  crcfinal 0x4983): HEAD 14 872 · 14 769 · 14 837, new 15 069 · 16 067 · 14 704 it/s (medians
  14 837 / 15 069: noise). Code 45 396 → 45 579 bytes (a thunk per import, longer import call
  sites).

### Package JIT checkpoint (JIT step 5)

Conditions: 2026-10-31, Intel Xeon Processor @ 2.10 GHz (`/proc/cpuinfo`), 4 vCPU cloud machine,
CPython 3.13.17, CoreMark self-check passed in all 10 runs (crcfinal 0x4983 / 0xcc42 by iteration
count). Alternated round by round in one session: `python3 tools/run_coremark_jit.py
poc/in/core.wasm`, then `cd poc && python3 run_jit2.py in/core.wasm`.

| round | package `jit-x86_64` | `w2jit2` (PoC) |
|---|---|---|
| 1 | 17 663 | 17 662 |
| 2 | 17 526 | 17 817 |
| 3 | 17 648 | 18 053 |
| 4 | 16 513 | 17 073 |
| 5 | 17 231 | 17 324 |
| median | **17 526** | 17 662 |

* Ratio of medians **99.2 %** (target ≥ 95 %). Compile 58–69 ms (`w2jit2` 26–42 ms).
* ISA gate (`tools/isa_all.py`, D24): 379 code blobs (every spec module of `jit-x86_64` plus
  CoreMark), 0 instructions above x86-64-v1; about 32 s locally.

### Package JIT on aarch64 (step 4e): first numbers from the arm64 CI runners

CI run 41346000607 (2026-11-01), one CoreMark run per runner
(`uv run python tools/run_coremark_jit.py poc/in/core.wasm --target jit-aarch64`), self-check
"Correct operation validated" on all three. Single runs on shared CI runners: noisy, not a
comparison between the OSes or with x86-64; no Wasmtime or `pyc` run alternated on the same runner yet.

| runner | CPython | CoreMark it/s | compile |
|---|---|---|---|
| `ubuntu-24.04-arm` (Linux aarch64, Azure) | 3.13.18 | 16 592 | 50 ms |
| `macos-latest` (macOS 26, Apple Silicon) | 3.13.16 | 16 049 | 42 ms |
| `windows-11-arm` (Windows 11 ARM64) | 3.13.17 | 16 545 | 104 ms |

Under QEMU 8.2 user mode the same command passes its self-check (crcfinal 0x5275); QEMU speed says
nothing about hardware. The same run: spec core 20509 pass / 0 fail and `auto` selects
`jit-aarch64` on all three runners.

## Compile and load times (CoreMark)

| step | time |
|---|---|
| `pyc`: parse 0.2 ms · codegen 53 ms · `compile()` 58 ms · exec 0.4 ms | cold ≈ 111 ms (19 KB wasm → 146 KB / 3 871 lines of Python) |
| `pyc` from a cached code object (`marshal.loads` + exec) | 0.5 ms (123 KB) |
| `mpy`: import of source / of `.mpy` | 59 ms / 2 ms (`.mpy` 37 KB bytecode; native `.mpy` 285–312 KB) |
| JIT `w2jit` / `w2jit2` | ~28–31 ms (3.13), ~50 ms (3.8); 71 KB / 39 KB of x86-64 |

## Micro-benchmarks behind the decisions

**Memory access in CPython 3.13** (`poc/bench/mem_access_cpython.py`, ns per op): `M[a]` 35 ·
`Struct.unpack_from` 86 · `memoryview.cast('I')[i]` 69 · `int.from_bytes(slice)` 209 · 4 bytes
composed 253 · `Struct.pack_into` 84 · memoryview store 77 · `(a+v) & 0xFFFFFFFF` 67 · `a+v` 34.

**Masking**: `l = l + 2` 31 ns · `l = (l + 2) & 0xFFFFFFFF` 64 ns · compare-and-adjust forms 45–51 ns
(this is why "single wrap" normalisation replaced masks: 675 → 158 masks in CoreMark, +15 %).

**v128 representation on `pyc`** (S0005, step 5b; `timeit` min of 5 × 100 000, CPython 3.13.17, Xeon 2.1 GHz,
4 cores, one session, 2026-11-01), ns per operation on 16-byte operands: `i32x4.add` on an `int` through
`struct` 976 (the package's generic helper `_i32x4_add`: 1 599) · on `bytes` through `struct` 726 · on a
tuple of lanes 583 · on an `int` by SWAR (`((a & L) + (b & L)) ^ ((a ^ b) & H)`) 131; `v128.xor` on an
`int` 44. `i8x16.add` through the helper 2 375 · `f32x4.add` through the helper 4 631 (each lane through
`_f32_i32` / `_i32_f32`). Correct first: the package uses `struct` for every lane operator and the `int`
for bitwise ones; SWAR for the integer lane operators is the measured optimisation for later.

**mypyc** (ns per op): `Struct.unpack_from` 68 · bytearray composed 159 · `librt` `BytesWriter`
load 11.5 / store 13.5 · `i64` arithmetic 1.8.

**CPython vs MicroPython** (`poc/bench/ops_cpython_vs_micropython.py`, ns per iteration):

| operation | CPython 3.13 | MP bytecode | MP `@native` |
|---|---|---|---|
| arithmetic, values < 256 | 32–36 | 44 | 26 |
| arithmetic, i32-sized values | 56–62 | 46 | 25 |
| `unpack_from` load | 123–131 | 262 | 149 |
| `pack_into` store | 129–143 | 128 | 105 |
| function call | 35–53 | 99 | 57 |
| empty loop | 14 | 24 | 13 |

MicroPython keeps small ints inside the pointer (63 bits on 64-bit unix), CPython allocates every
result above 256: that is why MicroPython wins on arithmetic.

**MicroPython 32-bit load** (`poc/bench/micropython_viper_load.py`): `unpack_from` (native) 158 ns ·
bytes composed (native) 140 ns · **viper `ptr32` 3.4 ns**.

**Machine code from Python** (`poc/bench/jit_loop_feasibility.py`, one loop iteration = i32 add +
i32 load): CPython 3.13 222 ns → mmap+ctypes code 0.66 ns (×338); CPython 3.8 176 → 1.04 ns; PyPy 5.6
→ 0.89 ns.

**Crossing into machine code** (`poc/bench/ctypes_call_cost.py`, ns per call): plain Python call 47 ·
`CFUNCTYPE` 327 · `PYFUNCTYPE` 303 (CPython 3.13); 64 / 277 / 206 (3.8). PyPy's ctypes ≈ 13 µs per
call (use its built-in cffi there).

**Long calls and the GIL** (`w2jit2`, CoreMark `setup`, CPython 3.13): during a 19.6 s guest call another
Python thread ran 17 175 iterations of a `sleep(0.001)` loop (~876/s): the GIL is released. A SIGINT
sent ~3 s into the call did not stop it; the call returned normally after 20.6 s.

**pywasm spec suite**: 54 006 commands, 0 failures, 40 s on CPython 3.13 (assert_invalid /
assert_malformed — 3 956 of them — are not checked by its runner).

## `br_table` (a dense `switch`)

`poc/bench/switch/`: a bytecode VM in C (`vm.c`, a 32-case `switch` that clang lowers to one
`br_table` of 31 entries, 32 targets), built with `clang --target=wasm32 -O2`; `run_switch.py`
runs it on each mode. Millions of VM operations per second; all modes return the same checksum.
CPython 3.13, PyPy 7.3.19, same Xeon 2.1 GHz cloud machine, one session.

| mode | linear chain (before) | after | change |
|---|---|---|---|
| wasmtime (Cranelift) | 433 | — | reference |
| `w2jit2` | 105–132 | **392–406** (jump table) | ×3.5, 92–94 % of wasmtime (was ~25 %) |
| `pyc`, CPython 3.13 | 0.9–1.1 (0.69–0.82 s) | 1.0–1.1 (0.70–0.77 s) (balanced if-tree) | ~+10 %, within noise |
| `pyc`, PyPy 7.3 | 41–44 | 43–47 (balanced if-tree) | ~+5 %, within noise |

CoreMark is unchanged (its tables are small and not hot): `w2jit2` 16 403 / 16 649 it/s without,
16 779 / 16 435 with jump tables; `pyc` still passes CoreMark's self-check.

* **JIT**: `cmp ecx,n; jae default; lea rax,[rip+T]; movsxd rdx,[rax+rcx*4]; add rax,rdx; jmp rax`, a
  table of int32 offsets relative to itself (position-independent) followed by one stub per target.
  Used from 4 non-default entries (`W2PY_JT`, 0 = off). The indirect `jmp` needs `endbr64` at the
  stubs once CET IBT is enforced (RISKS.md, R2).
* **pyc**: dispatch is no longer the cost. Python has no goto, so a `switch` compiled to nested blocks
  leaves through the `_br` flag: after the dispatch every one of the 32 block levels runs two tests
  (`if _br == d` and `if not _br`), ~64 per operation against 5 for the tree. The fix is structural:
  recognise the switch idiom (a `br_table` to the immediately enclosing blocks whose case segments end
  in an unconditional branch) and emit each case body at the leaf of the tree, with no flags; or, more
  cheaply, number labels so that one test `if _br <= d` replaces the two. Not done. (`W2PY_BT`: tree
  above this many runs, 0 = off.)

## meld on the PoC (a real 1.7 MB Rust program)

meld 0.66.0 (pulseengine, Apache-2.0), `meld-core` built for `wasm32-unknown-unknown` with
`opt-level="z"`, LTO, behind a ~40-line wrapper (no imports). Task: fuse the `wasm32-wasip2` build of
meld itself (a 2.9 MB component) into a core module. Same Xeon 2.1 GHz cloud machine, one session.

| run | translate | load + `compile()` | fuse | output |
|---|---|---|---|---|
| wasmtime-py (Cranelift) | — | — | 1.05 s | 2 834 770 bytes, the reference |
| meld p1 CLI under wasmtime (WASI) | — | — | 1.09 s | 2 841 680 bytes (attestation with a wall-clock timestamp) |
| PoC `pyc`, PyPy 7.3.19 | 8.7 s (CPython) | 8.2 s | **44 s** | byte-identical to the reference |
| PoC `pyc`, CPython 3.13 | 8.7 s | 6.1 s | **253 s** | byte-identical to the reference |

The module: 4 336 functions, 1 267 `call_indirect`, `memory.copy` ×940, `memory.fill` ×16, no SIMD,
no multi-value, no `trunc_sat`; translated to 13.6 MB of Python source. The PoC JIT cannot run it yet
(no bulk memory, `memory.grow`, full i64). The fused result has one memory and 38 `wasi:*@0.2.9`
imports. Wrapper and how to reproduce: `poc/bench/meld/`.

## Compile time on large modules

`poc/bench/compile/jit_compile_time.py`: code generation only. The PoC JIT does not cover every opcode
yet (missing: `call_indirect`, i64 arithmetic and conversions, bulk memory, …); functions it cannot
compile are replaced by a trap stub and counted, and the full time is **projected** from the covered
functions by body bytes — an estimate, weakest for QuickJS (10 % covered). Modules from
natify-sh/py-wasmext (`dev` branch) and meld (above). CPython 3.13 and PyPy 7.3.19, Xeon 2.1 GHz,
2 cores, one session. wasmtime-py's Cranelift compiles functions in parallel on both cores.

| module | wasm | JIT covered | **PoC JIT**, CPython (projected) | PoC JIT, PyPy (projected) | wasmtime Cranelift | `pyc`: translate + `compile()` |
|---|---|---|---|---|---|---|
| CoreMark | 19 KB | 100 % | 0.03 s (measured) | — | — | — |
| Lua 5.4 | 321 KB | 54 % | 0.37–0.44 s | 1.54 s | 0.25 s | 0.40 + 1.60 s |
| QuickJS | 1.1 MB | 10 % | 1.4–1.6 s | 2.75 s | 0.58 s | 1.47 + 4.22 s |
| wat2wasm (wabt) | 1.26 MB | 32 % | 0.74–0.78 s | 1.14 s | 0.49 s | 0.80 + 0.97 s |
| wasm2c (wabt) | 1.25 MB | 27 % | 0.66 s | — | — | — |
| meld-core | 1.7 MB | 31 % | 2.07 s | 1.66 s | 0.96 s | 1.98 + 6.49 s |
| CPython 3.13 (WASI, brettcannon/cpython-wasi-build v3.13.0) | 28 MB file, 4.1 MB code, 9 752 functions | 72 % | 5.06 s | — | 3.11 s | 6.6 s, then **fails**: 742 nested blocks > CPython's 100 indentation levels (LESSONS.md 22) |

* The single-pass JIT in Python generates code at **~600–840 KB of wasm per second** — about 1.5–2.5×
  the wall time of Cranelift on two cores. A megabyte-sized module costs ~1–2 s on first load; from the
  cache it is a file read and an `mmap`.
* The JIT rate holds on the largest module (CPython: ~800 KB of code bodies per second, 6.7 MB of
  x86-64 for the covered 72 %).
* PyPy does not help the compiler (warm-up dominates; only the largest module gains).
* `pyc` is dominated by CPython's own `compile()` of the generated source (up to 6.5 s): caching the
  marshalled code object matters more for `pyc` than for the JIT.
* Not yet measured: the full JIT (more opcodes, bounds-check elimination) may be slower per byte;
  **lazy compilation** (a function compiled on its first call, as V8 does with Liftoff) would make the
  first-load cost proportional to the code actually used — ROADMAP.md, open question 13.

## WAMR (iwasm) next to the PoC

`poc/bench/wamr/` (a minimal C host over `libiwasm`). WAMR 2.4.3 runtime for AOT (an `.aot` made by
`wamrc` 2.4.3 is refused by a runtime built from WAMR `main`: "unknown binary version"); fast
interpreter and Fast JIT from WAMR `main` (f8ed1f4). Same Xeon 2.1 GHz cloud machine, one session;
CoreMark passes its self-check in every row.

| engine | CoreMark it/s | switch VM, M ops/s | what it needs at run time |
|---|---|---|---|
| **iwasm AOT** (`wamrc` = LLVM, ahead of time) | **26 016 – 26 349** | **595 – 617** | `libiwasm` 134–244 KB AOT-only (x86-64, stripped) + an `.aot` per module × target × WAMR version |
| wasmtime (Cranelift) | 21 104 | 419 | a 29.7 MB library |
| **PoC JIT (`w2jit2`)** | **16 896** | **406** | the Python standard library |
| iwasm Fast JIT (x86-64 only) | 15 550 | 368 | `libiwasm` with Fast JIT |
| iwasm fast interpreter | 2 410 | 43 | `libiwasm` |

WAMR runtime sizes (x86-64 Linux, `MinSizeRel`, stripped; the CLI shows what a linked program keeps,
the `.so` is the whole archive): AOT only 134 KB CLI / 244 KB `.so`; + WASI 223 / 364 KB; + fast
interpreter 307 / 461 KB; + SIMD 356 / 506 KB.

* LLVM ahead of time beats Cranelift by ~25 % on CoreMark and ~45 % on the switch VM.
* The PoC JIT is ahead of WAMR's Fast JIT — the same class of compiler, written in C — on both.
* WAMR's fast interpreter runs at wasm3's speed (2 433 it/s, earlier table).

## Guard pages in the JIT (`poc/w2jit_guard.py`)

Same session, CoreMark it/s, alternating runs:

| engine | runs | vs wasmtime |
|---|---|---|
| wasmtime, guard pages (default) | 20 246 / 20 247 / 20 787 / 21 218 | 100 % |
| wasmtime, `signals_based_traps=False` (explicit checks) | 16 372 / 17 922 | 81–89 % |
| **PoC JIT, guard pages** (JIT-emitted SIGSEGV handler) | 18 349 / 19 989 / 19 221 / 18 923 / 19 279 | **~91 %** |
| PoC JIT, explicit checks | 15 252 / 15 581 / 15 981 / 16 084 / 16 636 | ~78 % |

Machine code 39 357 → 30 403 bytes. The switch VM (not memory-bound) does not change (397 vs 397–447
M ops/s, wasmtime 432–485). With explicit checks on both sides the PoC JIT and Cranelift are level:
the gap was the bounds checks, not the code.

**Package `jit-x86_64`, S0006 step 1** (`WABOROS_JIT_BOUNDS=guard` vs `explicit`, then the default):
2026-11-02, Intel Xeon @ 2.10 GHz, 4 vCPU VM, CPython 3.13.17, `uv run python
tools/run_coremark_jit.py poc/in/core.wasm`, 7 pairs alternated in one session, every run
"Correct operation validated". Another agent's spec run shared the machine during part of the
session (load average ~2.5), so treat the spread with care.

| mode | runs (it/s) | median | mean |
|---|---|---|---|
| explicit | 13 879 / 13 854 / 13 924 / 13 979 / 14 507 / 14 322 / 13 540 | 13 924 | 14 001 |
| guard | 16 088 / 15 773 / 16 194 / 15 912 / 15 613 / 16 202 / 13 985 | 15 912 | 15 681 |

Guard **+14.3 %** (median; mean +12.0 %; per pair +3.3 to +16.3 %), above S0006's +7.5 % from the
drafter's session: the gain depends on the session and the machine. Code 45 055 → 36 058 bytes
(−20.0 %), 669 fault points (the fault map, 8 bytes each).

**Guard vs explicit, quiet machine (the D44 switch).** 2026-11-02 15:17–15:24 UTC, the same Intel
Xeon @ 2.10 GHz, 4 vCPU VM, CPython 3.13.17, one benchmark process at a time, load average 0.53 at
the start and 1.00 at the end. CoreMark: `tools/run_coremark_jit.py poc/in/core.wasm`, 9 pairs
alternated (explicit first in each), the mode set by `WABOROS_JIT_BOUNDS`, every run "Correct
operation validated". Raw log: the session's `session.log` (scratchpad, not kept in the repository).

| mode | runs (it/s) | median | mean |
|---|---|---|---|
| explicit | 15 581 / 15 324 / 15 742 / 15 072 / 14 183 / 14 702 / 13 486 / 14 825 / 13 240 | 14 824.7 | 14 683.9 |
| guard | 16 265 / 17 721 / 16 331 / 15 879 / 16 211 / 17 091 / 15 486 / 14 412 / 15 407 | 16 211.4 | 16 089.4 |

Guard **+9.35 %** (median; mean +9.57 %). The ranges overlap and pair 8 goes the other way, but
eight pairs of nine favour guard. SIMD benchmark (`tools/run_simd_bench.py --targets jit-x86_64
--rounds 5`, three alternated rounds per mode, median of the three; µs per iteration, explicit →
guard): scalar builds addsat 7.92 → 6.67 (+18.7 %), scan 3.30 → 2.72 (+21.3 %), nearest 1.84 →
1.58 (+16.5 %), matmul 4.14 → 3.74 (+10.7 %), dot and swizzle 0–3 %; SIMD builds addsat 0.51 →
0.43 and nearest 0.63 → 0.56, the others 0–3 %, scan 0.70 → 0.73 (−4.1 %, noise). The corpus of
real libraries was measured too, but its sub-µs rows spread 2× between rounds: not a basis. This
measurement is the condition D44 named for making `auto` (guard on verified rows) the default.

**Checked stores in guard mode (LESSONS 50).** In guard mode a store of 2 bytes or more now keeps
its explicit check, because a straddling store may write part of itself before the fault (QEMU).
2026-11-02 16:52–17:16 UTC, the same Intel Xeon @ 2.10 GHz, 4 vCPU VM, CPython 3.13.17, one
benchmark process at a time, load average 0.4–1.0. Compared: *old guard* (every access unchecked,
the code of the D44 switch, from a worktree), *new guard* (this change), *explicit*. CoreMark:
`tools/run_coremark_jit.py poc/in/core.wasm`, alternated with the order rotated each round, every
run "Correct operation validated". Code: explicit 45 055 bytes; old guard 36 058 (−20.0 %, 669
fault points); new guard 38 457 (−14.6 %, 493 fault points).

| session | mode | runs (it/s) | median | vs explicit (median / mean) |
|---|---|---|---|---|
| 1 (9 rounds) | old guard | 16 898 / 15 862 / 14 738 / 16 147 / 15 727 / 15 900 / 15 229 / 16 547 / 16 028 | 15 899.5 | +9.8 % / +11.0 % |
| 1 | new guard | 15 528 / 15 733 / 15 421 / 14 911 / 14 492 / 15 396 / 14 947 / 16 163 / 14 854 | 15 396.5 | +6.3 % / +6.6 % |
| 1 | explicit | 14 773 / 14 542 / 14 288 / 14 013 / 14 481 / 13 098 / 14 753 / 14 601 / 14 334 | 14 481.2 | — |
| 2 (9 rounds) | old guard | 15 268 / 16 116 / 16 555 / 17 313 / 16 771 / 15 883 / 15 013 / 15 205 / 15 341 | 15 883.1 | +6.7 % / +7.0 % |
| 2 | new guard | 15 245 / 14 302 / 16 801 / 16 480 / 15 563 / 15 562 / 14 942 / 14 451 / 16 207 | 15 561.8 | +4.5 % / +4.1 % |
| 2 | probe (not adopted) | 15 023 / 15 210 / 15 101 / 16 569 / 15 310 / 15 860 / 15 579 / 13 642 / 15 005 | 15 210.3 | +2.1 % / +2.4 % |
| 2 | explicit | 15 175 / 13 869 / 13 662 / 15 768 / 16 221 / 14 578 / 15 084 / 14 781 / 14 893 | 14 893.1 | — |

Checked stores cost **2–3 %** of CoreMark against the old guard code (median −3.2 % and −2.0 %;
new below old in 8 and 7 rounds of 9); guard stays ahead of explicit by **+4.5 to +6.3 %**
(median), down from +6.7 to +9.8 % in the same sessions. *Probe* is a scratch prototype of the
cheaper-looking alternative: no check, a 1-byte read of the store's last byte (`cmp byte
[addr+n-1], 0`) as the fault point before the store, the store itself unmarked (Wasmtime's open
PR 8221 does the same with a full-size load). It was no faster than the check (median −2.3 %
against new guard, 4 rounds of 9 ahead), so it was not adopted.

SIMD benchmark: `tools/run_simd_bench.py --targets jit-x86_64 --rounds 5` per run, 3 and then 9
alternated runs per mode (rotated order). The runs spread up to 2× within one mode (bimodal: e.g.
explicit scalar addsat 7.5–13.7 µs), so the medians are noise; the table gives the **minimum** over
the 9 runs (µs per iteration; the 3-run session agrees within a few percent).

| build | kernel | explicit | old guard | new guard | new vs old |
|---|---|---|---|---|---|
| scalar | addsat | 7.52 | 5.80 | 5.88 | −1.4 % |
| scalar | dot | 2.87 | 2.89 | 2.84 | +1.8 % |
| scalar | scan | 3.16 | 2.57 | 2.57 | 0 % |
| scalar | matmul | 3.89 | 3.53 | 3.62 | −2.5 % |
| scalar | swizzle | 4.78 | 3.99 | 4.51 | −11.5 % |
| scalar | nearest | 1.81 | 1.51 | 1.64 | −7.9 % |
| simd | addsat | 0.47 | 0.38 | 0.39 | −2.6 % |
| simd | dot | 1.06 | 1.04 | 1.04 | 0 % |
| simd | scan | 0.65 | 0.63 | 0.63 | 0 % |
| simd | matmul | 2.21 | 2.18 | 2.20 | −0.9 % |
| simd | swizzle | 6.80 | 6.64 | 6.68 | −0.6 % |
| simd | nearest | 0.60 | 0.53 | 0.56 | −5.4 % |

Load-bound kernels keep their gain (scalar addsat and scan +23–28 % over explicit). Store-heavy
ones lose part of it: scalar swizzle drops from +20 % to +6 % over explicit, and nearest from +20 %
to +10 %. Raw logs: the session's `bench.log`, `bench2.log`, `simd.log` and `simd9.log`
(scratchpad, not kept in the repository).

## `wasm-opt -O3` (binaryen 125) on CoreMark

`wasm-opt --enable-nontrapping-float-to-int --enable-sign-ext --enable-mutable-globals -O3`: 29 → 10
functions (inlining), 8 527 → 8 097 instructions, 19.5 → 18.0 KB. Pairs in one session (it/s):

| engine | baseline | `-O3` | change |
|---|---|---|---|
| iwasm AOT | 24 472 | 23 405 | −4 % |
| wasmtime | 20 710 | 20 950 | +1 % |
| PoC JIT v2 | 16 780 | 16 477 | −2 % (`-Oz` +4 %, `-O4` −7 %) |
| iwasm Fast JIT | 13 540 | 13 213 | −2 % |
| PoC `pyc`, PyPy | 3 140 | 3 094–3 116 | −1 % |
| w2luajit, LuaJIT | 2 645 | 2 678 | +1 % |
| wasm3 | 2 479 | 2 306 | −7 % |
| iwasm fast interpreter | 2 256 | 2 066 | −8 % |
| w2lua, Lua 5.4 | 366 | 428 | +17 % |
| PoC `pyc`, CPython | 139 | 132 | −5 % |
| PoC, MicroPython | 82 | 83 | +1 % |

## Precompiled Lua

w2lua output loaded as source or as `string.dump` bytecode (stripped) inside lupa: load 2.4–2.9 ms →
0.1–0.2 ms (Lua 5.4, 130 → 76 KB), 1.8–2.6 → 0.0–0.1 ms (LuaJIT, 109 → 48 KB); CoreMark unchanged
(Lua 5.4 416–456, LuaJIT 2 547–2 754 it/s).

## `pyc` in the package vs the PoC (phase 0 port)

* Date: 2026-10-30 · commit: the "port PoC front end and pyc" step on `phase0-skeleton` · machine:
  Intel Xeon @ 2.10 GHz, 4 cores (`nproc`) · CPython 3.13.17.
* Commands: `uv run python tools/run_coremark_pyc.py poc/in/core.wasm` vs
  `cd poc && python3 w2py.py in/core.wasm out/core.py && python3 run_pyc.py out/core.pyc`.
* Result (CoreMark it/s, self-check passed, runs alternated): package 90.3, 96.1, 96.0, 98.5, 95.9,
  97.1, 99.7; PoC 99.6, 100.3, 91.0, 96.4, 95.4, 94.3, 101.7. Medians ≈ 97 / 96: same within noise
  (±5 it/s on this machine).
* Conditions: helpers imported from `waboros.targets.pyc.runtime` (D18) instead of pasted into the
  generated module; no measurable cost.

## `pyc`: checked `call_indirect` (table bounds, null, signature)

* Date: 2026-10-30 · machine: Intel Xeon @ 2.10 GHz, 4 cores · CPython 3.13.17 · command:
  `tools/run_coremark_pyc.py poc/in/core.wasm`, HEAD (unchecked) vs the change, from a worktree, alternated.
* Result (it/s): implementer 89.8 / 85.4, 92.9 / 90.4; orchestrator 90.4 / 95.7, 82.9 / 90.8,
  83.7 / 90.6 (HEAD / new). The two series disagree in sign; the run order (HEAD first) and a
  ±5 % noise dominate. Conclusion: no cost measurable on this machine; the check is required by the
  spec (BAD_SIGNATURE, INDIRECT_CALL_TO_NULL, TABLE_OUT_OF_BOUNDS) regardless.

## SIMD benchmark (S0005, `poc/bench/simd/`)

Six kernels in `poc/bench/simd/kernels.c` (u8 saturating add over 4 096 bytes, f32 dot product of 1 024,
i8 scan with `eq` + `bitmask`, 4×4 f32 matrix multiply ×64, byte table lookup with `swizzle` over
4 096 bytes, `f32x4.nearest` over 1 024 with ties), each built twice by `poc/bench/simd/build.sh`:
`clang --target=wasm32 -O3 -nostdlib` (Ubuntu clang 18.1.3, wasm-ld 18) without and with `-msimd128`;
the SIMD build uses `wasm_simd128.h` intrinsics, the scalar one plain loops. Runner:
`uv run python tools/run_simd_bench.py --rounds 5` (the package API for `pyc` and `jit-x86_64`,
wasmtime-py 50.0.1 for Wasmtime); `--ops` lists the v128 operators. Every kernel's checksum is equal on
`pyc`, `jit-x86_64` and Wasmtime for both builds (the runner fails otherwise), and on `jit-aarch64`
under QEMU (`python3 tools/run_arm64.py tools/run_simd_bench.py --targets jit-aarch64,wasmtime,pyc
--rounds 1`; its timings are meaningless and not listed).

Conditions: 2026-11-02, the machine above, CPython 3.13.17, one session; every configuration run once
per round, 5 rounds alternated, median. Microseconds per kernel iteration (one pass over the buffers;
`pyc` ran 5 iterations per call, the others 2 000). The checksum is computed once per call.

| kernel | pyc scalar | pyc SIMD | jit-x86_64 scalar | jit-x86_64 SIMD | Wasmtime scalar | Wasmtime SIMD |
|---|---|---|---|---|---|---|
| u8 `add_sat` | 1 381 | 1 412 | 7.26 | 0.45 | 2.87 | 0.18 |
| f32 dot | 1 006 | 2 792 | 2.87 | 1.49 | 0.74 | 0.18 |
| i8 scan (`eq`+`bitmask`) | 462 | 1 360 | 3.04 | 3.85 | 2.02 | 0.16 |
| 4×4 matmul ×64 | 2 505 | 9 986 | 3.96 | 12.06 | 1.39 | 0.40 |
| `swizzle` lookup | 1 048 | 639 | 4.62 | 6.74 | 2.56 | 0.16 |
| `f32x4.nearest` | 1 208 | 2 473 | 1.87 | 0.59 | 0.52 | 0.16 |

Speed-up of the SIMD build over the scalar build (scalar time / SIMD time):

| kernel | pyc | jit-x86_64 | Wasmtime |
|---|---|---|---|
| u8 `add_sat` | 0.98 | 16.1 | 15.9 |
| f32 dot | 0.36 | 1.9 | 4.1 |
| i8 scan | 0.34 | 0.79 | 12.6 |
| matmul | 0.25 | 0.33 | 3.5 |
| `swizzle` | 1.64 | 0.69 | 16.0 |
| `nearest` | 0.49 | 3.2 | 3.3 |

SIMD instructions in the SIMD build (`--ops`; the scalar build has none): `add_sat` 8
(`i8x16.add_sat_u`, `v128.load`, `v128.store`); dot 21 (`f32x4.add`, `f32x4.mul`,
`f32x4.extract_lane`, `v128.load`, `v128.const`); scan 7 (`i8x16.eq`, `i8x16.bitmask`,
`v128.load8_splat`, `v128.load`); matmul 52 (`f32x4.add`, `f32x4.mul`, `v128.load32_splat`,
`v128.load`, `v128.store`); `swizzle` 13 (`i8x16.swizzle`, `v128.load`, `v128.store`); `nearest` 57
(`f32x4.nearest`, `i8x16.shuffle`, `f32x4.convert_i32x4_s`, `i32x4.trunc_sat_f32x4_s`,
`i32x4.add`, `i32x4.mul`, `i32x4.extract_lane`, `v128.load`, `v128.store`, `v128.const`).

Reading: `pyc` SIMD is slower than scalar on four of six kernels, as expected (a lane operation through
`struct`); it wins only on `swizzle` (1.6×) and ties on `add_sat`.
`jit-x86_64` SIMD is 16× faster than its scalar build on `add_sat` (as fast as ~2.5× Wasmtime's SIMD
time), but slower than its scalar build on scan, matmul and `swizzle`, and 24–42× behind Wasmtime's
SIMD on those (scan 24×, matmul 30×, `swizzle` 42×). The scalar builds on `jit-x86_64` are 1.5–3.9×
behind Wasmtime's scalar builds. Per v128 operator in the hot loop: `add_sat` ≈ 1.8 ns per
vector (3 operators), matmul ≈ 3 ns per operator, scan ≈ 15 ns per vector, `swizzle` ≈ 26 ns per vector.
CoreMark is not affected (no `src/` change in this step).

### Why `jit-x86_64` SIMD is slower than scalar on some kernels (investigation, 2026-11-02)

Method: hot-loop disassembly (`WABOROS_JIT_DUMP` + `objdump`), instruction counts per category, and
controlled experiments. The experiments were patches to the code generator that were never committed;
every experiment's checksums equal the baseline's. Wasmtime's disassembly comes from the ELF of
`Module.serialize()`. Conditions: the tip before the rename (the code is identical after it), 7 alternated
rounds, median, one session, microseconds per inner iteration (a separate session from the table above,
so the `base` column differs from it a little).

Experiments: `m` = v128 local get/set and constants by one 16-byte `movups`, splat built in xmm (`movq` +
`punpcklqdq`); `p` = skip a slot reload right after a store to it (a stand-in for a vector `tos`); `b` / `B` =
no bounds check on v128 accesses / on all accesses; `s` = `swizzle` by `paddusb 0x70` + `pshufb` (x86-64-v2);
`r` = `nearest` by `roundps` (v2); `c` = `popcnt` (v2). Letters combine (`mpsrc` = m+p+s+r+c).

| kernel | scalar | base | m | ms | mc | mr | mp | mps | mpsrc | mpsrcb | mpsrcB | Wasmtime |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| addsat | 7.78 | 0.49 | 0.45 | 0.46 | 0.56 | 0.46 | 0.47 | 0.47 | 0.53 | 0.39 | 0.40 | 0.31 |
| dot | 3.15 | 1.54 | 1.11 | 1.21 | 1.12 | 1.10 | 0.93 | 0.92 | 0.91 | 0.90 | 0.98 | 0.27 |
| scan | 3.20 | 4.01 | 0.72 | 0.79 | 0.45 | 0.67 | 0.62 | 0.62 | 0.49 | 0.39 | 0.48 | 0.22 |
| matmul | 4.22 | 12.03 | 2.25 | 2.20 | 2.19 | 2.19 | 2.17 | 2.20 | 2.19 | 2.23 | 2.22 | 0.48 |
| swizzle | 4.60 | 6.78 | 6.82 | 0.52 | 6.75 | 6.77 | 6.83 | 0.50 | 0.52 | 0.50 | 0.50 | 0.23 |
| nearest | 1.83 | 0.61 | 0.61 | 0.60 | 0.60 | 0.36 | 0.68 | 0.73 | 0.33 | 0.30 | 0.29 | 0.19 |

Instructions per inner iteration, base -> m -> mpsrc: scan 77 -> 73 -> 43; matmul 427 -> 411 -> 407
(slot moves 192, operators 60, memory 24, bounds checks 72, scalar 78); `swizzle` 483 -> 475 -> 115
(the scalar part is 414 = 16 lanes x 6); dot 114 -> 110 -> 109; `add_sat` 65; `nearest` 162 -> 162 -> 86.

Cause: store-to-load forwarding failures. A v128 is written as two 8-byte stores (`get_local`/`set_local` in
`common/simd.py` through two `b.move` of 64-bit halves; constant pairs from `_pair`; splat and `load_splat`:
`imul` then two `mov [slot]`) or as 16 byte stores (the `swizzle` scratch), then read back by one 16-byte
`movups`. Per iteration: matmul 28 such pairs, scan 2, `swizzle` 4, dot 1. With 16-byte moves matmul goes
427 -> 411 instructions and runs 5.4x faster, so the cost is the forwarding stall, not the instruction count.

Ranking by gain per complexity (the investigation's, data not a decision):

1. 16-byte v128 moves in the core and splat in xmm: matmul 5.4x, scan 5.6x, dot 1.4x; matmul and scan then
   beat their scalar builds. About 40-60 lines and one Backend method (a 16-byte slot move / constant write):
   a D39 amendment; no ABI, artifact or ISA change.
2. An x86-64-v2 variant (D42): `swizzle` 13x, `nearest` 1.7x, scan 1.6x on top of 1. Needs its own spec; opt-in.
3. A vector `tos` (S0005 Q3): the proxy `p` gives 0-16 % on top of 1. High complexity, low gain.
4. Bounds checks: 0-20 %; guard pages (D29).
5. The remaining 2-5x to Wasmtime: Wasmtime compiles for the host ISA (AVX/AVX2, `popcnt`, `vpshufb`), has no
   bounds checks (guard pages) and keeps values in xmm registers (dot's accumulator, matmul's `b0`..`b3`):
   register allocation, a design topic of its own.

Open: aarch64 likely has the same pattern (two `str x`, then `ldr q`); unmeasured (QEMU only). Flush and
branch moves still move v128 halves.

### After the 16-byte move fix (2026-11-02)

The fix (S0005, the D39 amendment `move_v128`; LESSONS 44): v128 locals, constants and `select` move as one
16-byte access on both JITs; on `jit-x86_64` splat, `load*_splat`, `load*_zero` and `v128.not` are built in xmm0
and written by one `movups`; on `jit-aarch64` `load*_zero` by `fmov d0` + `str q0`. The `swizzle` scratch is
unchanged (its fix is the v2 `pshufb`, D42), so `swizzle` stays slower than scalar.

Conditions: 2026-11-02, the machine above (4 cores), CPython 3.13.17, one session. `uv run python
tools/run_simd_bench.py --targets jit-x86_64,wasmtime --rounds 5`, three runs of the code before the step (a
worktree of its parent, `PYTHONPATH`) alternated with three runs after it; each run is the median of 5
alternated rounds, the table the median of the three runs. Microseconds per kernel iteration. All checksums OK
in every run; the full runner (`pyc` included) also OK after.

| kernel | jit-x86_64 scalar | jit-x86_64 SIMD before | jit-x86_64 SIMD after | SIMD over scalar, after | Wasmtime SIMD |
|---|---|---|---|---|---|
| u8 `add_sat` | 7.46 | 0.47 | 0.45 | 16.6 | 0.19 |
| f32 dot | 2.87 | 1.49 | 1.03 | 2.8 | 0.18 |
| i8 scan | 3.08 | 3.81 | 0.64 | 4.8 | 0.16 |
| 4×4 matmul ×64 | 3.87 | 12.10 | 2.17 | 1.8 | 0.40 |
| `swizzle` lookup | 4.68 | 6.72 | 6.66 | 0.70 | 0.17 |
| `f32x4.nearest` | 1.80 | 0.63 | 0.59 | 3.1 | 0.14 |

(The scalar and Wasmtime columns: the median of all six runs; the scalar build has no v128 and its code is
byte-identical before and after.) Matmul is 5.6× and scan 6.0× faster, as the experiment `m` predicted
(2.25, 0.72); both now beat their scalar builds. CoreMark on `jit-x86_64`: code byte-identical (D35 lists),
validated before and after (13 434 / 15 734 before, 13 826 / 14 251 after, alternated: noise).
