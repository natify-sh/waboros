---
title: Benchmarks
kind: log
status: current
updated: 2026-11-05
---

# Benchmarks

Everything measured during the PoC, with its conditions. Numbers from different sessions differ by up
to ~20 % (shared cloud machine); compare within a session.

## Corpus of freestanding libraries (`poc/bench/corpus/`)

Real libraries compiled to wasm (REAL-PROGRAMS section 1): Monocypher 4.0.2 (BLAKE2b, X25519,
XChaCha20-Poly1305), SHA-256 (B-Con crypto-algorithms), BLAKE3 1.5.4 (portable C), miniz 3.0.2
(deflate, inflate), xxHash 0.8.2 (XXH32, XXH64, XXH3-64, XXH3-128; scalar, no SIMD), QOI (`qoi.h`, content
pinned by sha256, no tag), and a C++ program with real `throw`/`catch` (`cxx_exc.cpp`, below). Built by `poc/bench/corpus/build.sh`: Ubuntu clang 18.1.3 (`cxx_exc.wasm`: Zig 0.17.1, clang 22.1.8), wasm-ld 18,
`--target=wasm32 -O3 -nostdlib -ffunction-sections -fno-builtin -Wl,--gc-sections -Wl,-z,stack-size=1048576`
(BLAKE3 with its SIMD dispatch off, miniz without archive/stdio/time); sources are fetched from
raw.githubusercontent.com and checked against the pinned sha256, the build is byte-reproducible. A driver
per library exports one function per operation plus `put`/`get` (8 bytes per call) over a static I/O buffer
and a 1 MiB bump `malloc`. Runner: `uv run python tools/run_corpus.py --rounds 3`. Every result is
compared with `hashlib` (BLAKE2b, SHA-256), `zlib` (miniz deflate output decompressed by `zlib`, `zlib`
output inflated by miniz), the BLAKE3 test vectors (`test_vectors.json`, 1.5.4) or a pure-Python reference
(RFC 7748 ladder for X25519; XChaCha20-Poly1305 built from the RFC 8439 pieces), values of the reference
xxHash built natively once (`xxh_vectors.json`: 20 lengths from 0 to 8192 B x seeds 0 and 12345 x 4 algorithms,
the three known empty-input values agree with the published ones), and for QOI the FNV-1a of the reference
decoder's pixels plus a decode-then-encode that must return the reference encoder's bytes (`qoi_test.qoi`,
64x48 RGBA, 8312 B), on `pyc`, `jit-x86_64` and Wasmtime: 184 of 184 agree on each (552 of 552), also with
`WABOROS_JIT_BOUNDS=explicit`; on `jit-aarch64` under QEMU (`python3 tools/run_arm64.py
tools/run_corpus.py --targets jit-aarch64 --rounds 1`) 184 of 184 agree (no timings).

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

Rows added 2026-11-09 (same machine and settings, `uv run python tools/run_corpus.py --rounds 3`, guard
bounds; one session, so not comparable with the rows above):

| case | pyc | jit-x86_64 | jit-x86_64 explicit | Wasmtime |
|---|---|---|---|---|
| XXH32, 4 KiB | 409 | 1.6 | 1.2 | 3.7 |
| XXH64, 4 KiB | 385 | 1.3 | 0.9 | 2.8 |
| XXH3-64, 4 KiB | 290 | 1.9 | 1.4 | 4.2 |
| XXH3-128, 4 KiB | 343 | 2.0 | 1.4 | 4.6 |
| QOI decode, 64x48 RGBA | 5 262 | 23.3 | 28.3 | 27.4 |
| QOI decode + encode | 13 019 | 69.0 | 54.9 | 54.5 |
| C++ throw/catch, 13 lines (below) | 637 | 5.0 | 6.6 | 14.2 |

The C++ row (2026-11-09; `explicit` from the separate `WABOROS_JIT_BOUNDS=explicit` run, so only roughly
comparable with the others): `cxx_exc.cpp`, an expression evaluator over 13 lines in which 5 raise
`ParseError` (caught, counted, rethrown with `throw;` and turned into a number three frames up), 1 `DivZero`
and 1 an `Other` caught by `catch (...)`; 116 destructors run during unwinding. The module is built with
`-fwasm-exceptions -mllvm -wasm-use-legacy-eh=false` by Zig 0.17.1's clang 22.1.8 and holds `try_table` (52),
`throw_ref` (38) and `throw` (1), no legacy opcode; 169 KB, 6 WASI imports that only abort paths use
(stubbed by the runner; `proc_exit` traps). The libc++abi exception runtime is built by `build.sh` from the
sources in the Zig wheel (the wheel's own wasm libc++abi has none). 7 throws per call, so about 90 us per throw
on `pyc`, under 1 us on `jit-x86_64`, 2 us on Wasmtime. All results equal the native g++ build on `pyc`,
`jit-x86_64` (guard and explicit), `jit-aarch64` (QEMU) and Wasmtime 50.0.1 with `wasm_exceptions` on.

The "explicit" column is a separate run with `WABOROS_JIT_BOUNDS=explicit` (its Wasmtime column is a
different run of the same thing: 3.2, 2.8, 3.1, 3.4, 17.0, 37.5), so differences of 10-40 % between the two
`jit-x86_64` columns on these 1-30 us calls are noise, not a bounds-check effect.

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
(The macOS and Windows legs of the `arm64` job no longer exist; those platforms run in `platforms.yml`, which does not
measure speed. The Linux leg stays: native CoreMark, guard against explicit, below.)

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
  Taken up by [S0010](specs/S0010-pyc-branch-emission.md) (approved 2026-11-04; step 0 below, "`pyc`: `_br`
  ladders (S0010 step 0)": the switch VM ×2.2-2.5 on CPython with the experiment's pass).

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

**Checked stores in guard mode (rejected alternative; D45, LESSONS 50).** A prototype in which a
store of 2 bytes or more keeps its explicit check in guard mode, because a straddling store may
write part of itself before the fault under QEMU. The maintainer rejected it (D45): stores stay
fault points, as in Wasmtime, so the guard code in use is *old guard* below.
2026-11-02 16:52–17:16 UTC, the same Intel Xeon @ 2.10 GHz, 4 vCPU VM, CPython 3.13.17, one
benchmark process at a time, load average 0.4–1.0. Compared: *old guard* (every access unchecked,
the code of the D44 switch, from a worktree; the code in use), *new guard* (checked stores, rejected),
*explicit*. CoreMark:
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
new below old in 8 and 7 rounds of 9); with them guard would lead explicit by **+4.5 to +6.3 %**
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

With checked stores, load-bound kernels keep their gain (scalar addsat and scan +23–28 % over explicit). Store-heavy
ones lose part of it: scalar swizzle drops from +20 % to +6 % over explicit, and nearest from +20 %
to +10 %. Raw logs: the session's `bench.log`, `bench2.log`, `simd.log` and `simd9.log`
(scratchpad, not kept in the repository).

**Package `jit-aarch64`, S0006 step 2: code size, no speed claim.** 2026-11-02, `poc/in/core.wasm`
compiled by `jit-aarch64` (cross-compiled on the Intel Xeon host; the D35 lists show that QEMU on
arm64 gives the same code), `WABOROS_JIT_BOUNDS=explicit` vs `=guard`: code 37 372 → 29 284 bytes
(−21.6 %), 669 fault points (the fault map, 8 bytes each). The emitted fault handler is 304 bytes
(x86-64: 324). CoreMark under QEMU 8.2.2 user mode (`python3 tools/run_arm64.py
tools/run_coremark_jit.py poc/in/core.wasm --target jit-aarch64`, one run per mode): "Correct operation
validated" in both modes. The it/s (guard 3 606, explicit 2 154) come from TCG emulation and are not a
measurement of either mode.

**Package `jit-aarch64`, guard pages against explicit checks, native.** 2026-11-03, the Linux `arm64`
job of `ci` (GitHub `ubuntu-24.04-arm` runner, CPython 3.13, CI run 41711993940), `tools/run_coremark_jit.py
poc/in/core.wasm --target jit-aarch64`, modes alternated, two runs each, both "Correct operation
validated": guard 19 465 and 19 430 it/s, explicit 17 463 and 17 447 it/s; guard +11.4 % (median of
each mode; the runs of a mode differ by less than 0.2 %). A shared CI runner, not a quiet machine; the
gap is larger than on x86-64 (+9.35 %, step 1) and supports `auto` giving guard pages on aarch64.

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

## Scalar code generation (S0007)

One row per step of [S0007](specs/S0007-scalar-codegen.md), each against the commit before it, alternated in
the same session.

### Step 1: results straight to slots 0..m (2026-11-02)

The change: at a function's `end` and at `return`, the results are moved straight into slots 0..m (a result
in `tos` is one store instead of a store, a load and a store through `rcx`); the entries below them are no
longer flushed. Conditions: 2026-11-02 17:58–18:14 UTC, Intel Xeon @ 2.10 GHz, 4 vCPU VM, CPython 3.13.17,
wasmtime-py 50.0.1, `jit-x86_64` in its default bounds mode (guard, D44), one benchmark process at a time,
load average 0.5–0.8. Baseline: HEAD (`git archive` into a scratch directory, `PYTHONPATH`), alternated run
by run with the step's code.

| benchmark | runs | before | after | change |
|---|---|---|---|---|
| calls, `leaf` (ns per call) | 5 runs × 11 rounds | 2.24 | 2.15 | −4 % |
| calls, `leaf` | 8 runs × 21 rounds | 2.30 | 2.16 | −6 % |
| calls, `fib(27)` (ns per call) | 5 runs × 11 rounds | 3.14 (3.05–3.19) | 2.56 (2.51–2.67) | −18 % |
| calls, `fib(27)` | 8 runs × 21 rounds | 3.22 (3.09–3.72) | 2.60 (2.51–2.78) | −19 % |
| CoreMark (it/s) | 5 | 16 694 (16 458–17 222) | 16 849 (16 469–17 259) | +0.9 % (noise) |

Calls: `tools/run_calls_bench.py`, the median over runs of each run's median; `leaf` was faster after in 12
of the 13 alternated pairs (its run medians overlap: 2.19–2.95 before, 2.08–2.57 after), `fib` in all 13
with no overlap. Wasmtime in the same runs: `leaf` 1.39, `fib` 2.64 ns per call. CoreMark: `tools/
run_coremark_jit.py poc/in/core.wasm`, every run "Correct operation validated"; code 36 058 → 35 788 bytes
(aarch64 39 152 → 38 960).

Scalar kernels (`tools/run_simd_bench.py --targets jit-x86_64 --rounds 5`, 8 alternated runs, median µs per
iteration of the scalar build): addsat 5.82 → 5.94, dot 2.89 → 2.88, scan 2.70 → 2.65, matmul 3.65 → 3.62,
nearest 1.56 → 1.54 (noise), **swizzle 4.09 → 4.49**. Swizzle's code differs only in its return, which runs
once per 2 000 iterations; the other functions' returns shrink, so its loop moves by 66 bytes. A control
(8 alternated runs of four builds) settles it: before 4.09, after 4.61, after with 66 bytes of padding at the
start of the module (swizzle at the baseline's offset) 4.14, after with 2 bytes (its offset ≡ the baseline's
mod 64) 4.06. The loss is code placement, not the change (LESSONS 51).

Corpus (`tools/run_corpus.py --targets jit-x86_64`, 2 alternated runs of 3 rounds each, same session): results
equal; every case within the spread of the two baseline runs (for example x25519 143.4 → 141.9–147.0 µs,
sha256 4096B 42.6 → 41.1–41.9, deflate 8K 430.3–438.2 → 418.7–450.7).

### Step 2: add/sub as one `lea` (L) (2026-11-02)

Reverted 2026-11-04: under the placement control L gave no gain once K was in ("Steps 2 (L) and 3 (K)
re-measured under the placement control", below).

The change (`jit-x86_64` only): `binop` add/sub of a register `x` other than the destination and an immediate
that fits a sign-extended disp32 at the operand's width, or add of two registers, is one `lea` instead of
`mov` + `add`/`sub` (for example `lea eax, [rbx-0x1]`). aarch64 code is unchanged (its `jit_dump` list is
identical to the baseline's); on x86-64, 61 modules change (CoreMark included), exactly the modules where
`binop` takes the new path. Conditions: 2026-11-02 18:39–18:50 UTC, Intel Xeon @ 2.10 GHz, 4 vCPU VM,
CPython 3.13.17, `jit-x86_64` in its default bounds mode (guard, D44), one benchmark process at a time, load
average 0.5–1.0. Baseline: HEAD (`git archive` into a scratch directory, `PYTHONPATH`), alternated run by run
with the step's code.

| benchmark | runs | before | after | change |
|---|---|---|---|---|
| CoreMark (it/s) | 8 pairs | 16 250 (14 623–16 989) | 16 610 (16 313–16 818) | +2.2 % (within the spread) |
| scalar `addsat` (µs per iteration) | 16 pairs × 5 rounds | 5.94 | 5.27 | −11 %, faster in 15 of 16 pairs |
| scalar `scan` | 16 pairs | 2.64 | 2.38 | −10 %, 14 of 16 |
| scalar `nearest` | 16 pairs | 1.55 | 1.46 | −6 %, 12 of 16 |
| scalar `dot`, `matmul` | 16 pairs | 2.91, 3.64 | 2.90, 3.62 | noise |
| scalar `swizzle` | 16 pairs | 4.54 | 3.15 | code placement (below) |
| calls, `leaf` (ns per call) | 16 pairs × 11 rounds | 2.17 | 2.12 | noise |
| calls, `fib(27)` | 16 pairs × 11 rounds | 2.65 | 2.79 | code placement (below) |

Padding control (LESSONS 51): both trees were built with 0, 8, …, 56 bytes of `int3` before the first function
(a scratch patch), alternated, 3 runs per pad. `addsat` after the change is faster at every pad (5.04–5.30 µs
against 5.91–8.64); `scan` (2.26–2.39 against 2.59–2.65) and `nearest` (1.38–1.45 against 1.53–1.58) at 7 of 8
pads. `swizzle` spans 3.82–4.78 before and 3.16–4.56 after, so its gain is placement and is not claimed. `fib`'s
code changes only in two `lea`s, each 1 byte shorter, and it starts 3 bytes earlier. With 3 bytes of padding
(fib back at the baseline's offset), 16 alternated triples gave base 2.60, after 2.75, after+3 2.61: the loss is
placement. JCC erratum check (the code area is page-aligned, so module offsets mod
32 are absolute): in the slow build fib's return `jmp` (run on every call) spans 0x19d–0x1a2, crossing a 32-byte
boundary. In the padded build, as fast as the baseline, it spans 0x1a0–0x1a5. That `jmp` is the only
difference between those two builds: the two `test`+`jne` status checks after the calls cross a boundary in
both, and in neither of them in the baseline. So the padded build has two crossings more than the baseline
and is just as fast; the erratum explains `fib` at most in part. For `swizzle` (ROADMAP, "Code placement", first check), the
inner loop's back edge does not cross a boundary in the slow baseline build (0x2ec7–0x2ed0), but it does in the
fast build after the change (0x2e1c–0x2e25). Both inner loops have three crossing branches. So the back edge
alone does not explain swizzle.

Corpus (`tools/run_corpus.py --targets jit-x86_64`, 2 sets of 4 alternated pairs, 3 rounds each): results equal
in all 16 runs. No case is slower than the baseline beyond the spread. Medians move both ways between the two
sets (for example `inflate 8K` −14 % then +11 %; x25519 #0 −6 % then −5 %), and some runs of both trees are
disturbed by about 2×, so the corpus does not show a change. CoreMark: every run "Correct operation validated".

### Step 1b: a block that ends the function writes its results to slots 0..m (2026-11-02; dropped)

Not merged: no gain beyond code placement (S0007's rule); the numbers are kept for a revisit after
the code-placement work (ROADMAP).

The change (core, both JITs): a block, `if` with `else` or loop at stack height 0 whose `end` only `end`s follow
(its parent the function or such a block) has its results moved straight into slots 0..m, as a return does.
`fib`'s two arms each lose a load and a store (`mov [r12+8],rax; mov rcx,[r12+8]; mov [r12],rcx` becomes
`mov [r12],rax`). 29 spec modules change on each target. CoreMark, the scalar and SIMD kernels and the corpus
compile to byte-identical code on both targets, so their speed cannot change. Conditions: 2026-11-02
19:20–19:25 UTC, Intel Xeon @ 2.10 GHz, 4 vCPU VM, CPython 3.13.17, `jit-x86_64` in its default bounds mode
(guard, D44), one benchmark process at a time, load average 0.3–1.0. Baseline: HEAD (`git archive`, `PYTHONPATH`),
alternated run by run.

| benchmark | runs | before | after | change |
|---|---|---|---|---|
| calls, `fib(27)` (ns per call), as built | 10 pairs × 21 rounds | 2.63 (2.56–2.67) | 2.73 (2.60–2.85) | +4 %, slower in 10 of 10 pairs |
| calls, `fib(27)`, padding sweep 0–56 bytes | 5 × 8 pairs × 21 rounds | 3.04 | 2.95 | −3 %, faster in 27 of 40 pairs |
| calls, `leaf` | the same runs | 1.96 / 2.03 | 2.02 / 2.06 | noise (code unchanged) |
| CoreMark (it/s) | 3 pairs | 17 094 (16 782–17 309) | 17 367 (16 439–17 464) | identical code; every run validated |

Padding control (LESSONS 51): both trees with 0, 8, …, 56 bytes of `int3` before the first function. `fib`
after the change is faster at pads 16–48 (for example pad 32: 3.09 → 2.92, 5 of 5 pairs) and slower or equal at
pads 0 and 8 (pad 0: 2.66 → 2.71, 1 of 5). Placement alone moves `fib` by 15 % (2.66 at pad 0, 3.06 at pad 8, base).
JCC erratum check: the crossings of 32-byte boundaries repeat every 32 bytes (pad 0 and 32 alike: base 3, after
4, the extra one the base case's `jmp`), but pad 0 and pad 32 go opposite ways, so the erratum does not explain
the loss at pad 0. The step does not show a gain beyond placement. Compile time (CoreMark and the three corpus
modules, 3 alternated pairs): 188–198 → 197–203 ms median, minimums equal (182–191 ms).

### Step 3: `local + const` as one operand (K) (2026-11-02)

The change (core and both JITs, D39 amendment A): `i32.add`/`i32.sub` of a pinned i32 local and a constant
stays on the operand stack as one entry and is consumed as the operand `AddK`: a load/store address forms
it in the index register with one `lea ecx, [rbx+k]` (aarch64 `add w9, w19, #k`) instead of `lea eax` +
`mov ecx, eax`; `local.set`/`local.tee` writes it into the local with one `lea`; any other consumer computes
it into `tos`. 47 spec modules change on each target (the same 47, each with such an add/sub; CoreMark
included); CoreMark code 34 963 → 33 993 bytes in guard mode, 43 960 → 42 990 explicit, aarch64 38 960 →
37 372. Conditions: 2026-11-02 19:45–20:25 UTC, Intel Xeon @ 2.10 GHz, 4 vCPU VM, CPython 3.13.17,
`jit-x86_64` in its default bounds mode (guard, D44), one benchmark process at a time, load average
0.8–1.06. Baseline: HEAD (`git archive` into a scratch directory, `PYTHONPATH`, `waboros.__file__` checked),
alternated run by run with the step's code.

| benchmark | runs | before | after | change |
|---|---|---|---|---|
| CoreMark (it/s), as built | 8 pairs | 16 758 (15 604–17 197) | 16 741 (16 324–17 637) | −0.1 %, faster in 5 of 8 (noise) |
| CoreMark, padding sweep 0–56 bytes | 16 pairs | 17 189 (16 523–17 910) | 17 347 (16 693–18 493) | +0.9 %, faster in 10 of 16 (within the spread) |
| scalar `addsat` (µs per iteration) | 16 pairs × 5 rounds | 5.04 (4.97–5.39) | 4.12 (3.94–4.61) | −18 %, faster in 16 of 16 |
| scalar `scan` | 16 pairs | 2.29 (2.25–2.43) | 2.08 (2.03–2.15) | −9 %, 16 of 16 |
| scalar `nearest` | 16 pairs | 1.41 (1.38–1.50) | 1.25 (1.21–1.93) | −11 %, 14 of 16 |
| scalar `matmul` | 16 pairs | 3.54 (3.47–3.88) | 3.41 (3.30–5.97) | −4 %, 12 of 16 |
| scalar `dot` | 16 pairs | 2.87 | 2.86 | noise |
| scalar `swizzle` | 16 pairs | 3.13 (3.05–3.28) | 4.04 (3.71–5.45) | code placement (below) |
| calls, `leaf` / `fib(27)` (ns per call) | 16 pairs × 11 rounds | 1.98 / 2.64 | 1.94 / 2.69 | noise (9 and 6 of 16) |

Padding control (LESSONS 51): both trees with 0, 8, …, 56 bytes of `int3` before the first function (a scratch
patch), alternated, 3 runs of the kernels per pad. `addsat` after the change is faster at every pad (3.92–4.24
against 5.01–5.29 µs), `scan` (2.04–2.14 against 2.25–2.35) and `matmul` (3.38–3.47 against 3.48–3.60) at 8 of 8,
`nearest` at 7 of 8. `swizzle` spans 3.09–4.60 before and 2.68–4.05 after, faster at 4 of 8 pads (pooled 3.54 →
3.25): its loop lost only `mov`s (every `lea eax, [rbx+k]; mov ecx, eax` became `lea ecx, [rbx+k]`), so the
as-built loss is placement and is not counted either way. CoreMark does not gain beyond the spread, as built or
over the pads (the investigation's +6.2 % was K, L and Z together; step 2's L alone gave +2.2 %, within the spread).

Corpus (`tools/run_corpus.py --targets jit-x86_64`, 8 alternated pairs, 3 rounds each): results equal (21 of 21
OK in all 16 runs); every case within the spread (for example x25519 #0 139.5 → 141.1 µs, deflate 8K 396.7 →
406.3 with ranges 386–420 and 381–429, blake3 8192B 20.3 → 20.0). Compile time (CoreMark and the three corpus
modules, `jit-x86_64`, median of 10 compilations per run): 178.6 → 186.9 ms (5 alternated pairs) and 185.7 →
193.6 ms (8 more), +4–5 %, within the +10 % bound; a `pop` without the extra call was measured and did not pay.
Every CoreMark run "Correct operation validated".

### Step 5: a parameter accessed once is not pinned (2026-11-03; dropped)

Not merged: the calls benchmark's `leaf` does not get faster, so by S0007's rule the step is dropped. The
numbers are kept for a revisit.

The change (core, both JITs): `choose_pins` skipped a parameter of weight 1 (one `local.get`/`set`/`tee`,
outside every loop). Such a parameter stayed in its slot. The leaf `$add` of `poc/bench/calls/` lost
`push rbx; push rbp`, the two pops and one load (`mov ebx,[r12]; mov ebp,[r12+8]; lea eax,[rbx+rbp]` became
`mov eax,[r12]; add eax,[r12+8]`): 25 → 21 instructions, 99 → 92 bytes. 319 spec modules changed on each target
(the same 319), exactly the modules where the rule changed some function's pins. CoreMark's code changed too
(x86-64 33 993 → 33 997 bytes in guard mode: a parameter left in its slot loses the K path). Conditions:
2026-11-03 21:15–21:24 UTC, Intel Xeon @ 2.10 GHz, 4 vCPU VM, CPython 3.13.17, `jit-x86_64` in its default
bounds mode (guard, D44), one benchmark process at a time, load average 0.1–1.0. Baseline: HEAD ffd4c9f
(`git archive`, `PYTHONPATH`, `waboros.__file__` checked), alternated run by run with the order swapped each
pair. `tools/run_calls_bench.py --targets jit-x86_64 --rounds 11`, the median of the run medians (range).

| benchmark | runs | before | after | change |
|---|---|---|---|---|
| calls, `leaf` (ns per call), as built | 16 pairs | 2.00 (1.94–2.19) | 2.31 (2.19–3.48) | +16 %, slower in 16 of 16 (placement, below) |
| calls, `leaf`, padding sweep 0–56 bytes | 8 pads × 8 pairs | 2.06 (1.87–3.24) | 2.13 (1.85–2.64) | +3 %, faster in 23 of 64 |
| calls, `leaf`, the rest of the module at the baseline's offsets | 4 pads × 8 pairs | 2.04 (1.94–2.40) | 2.13 (2.02–3.43) | +4 %, faster in 8 of 32 |
| calls, `fib(27)` (code unchanged), as built / sweep / control | 16 / 64 / 32 pairs | 2.77 / 3.11 / 3.12 | 3.15 / 3.10 / 3.11 | placement; equal when placed alike |
| CoreMark (it/s) | 8 pairs | 17 099 (16 850–17 309) | 17 212 (15 795–17 785) | +0.7 %, faster in 6 of 8 (within the spread) |

Padding control (LESSONS 51): both trees had 0, 8, …, 56 bytes of `int3` before the first function (a scratch
patch). Placement alone moves `leaf` from 1.91 to 2.29 ns and `fib` from 2.77 to 3.17 ns in the baseline. The
last control padded the new tree with 7 bytes of `int3` after `$add`. Then the code from the caller on was
byte-identical to the baseline's and at the same offsets, so only `$add`'s body differed. With everything else
equal, removing the two pushes, the two pops and one load did not make the call faster; it was slightly
slower, if anything. Every CoreMark run "Correct operation validated". Spec parity held with the change
(54 385 pass, 0 fail, 91 xfail on `jit-x86_64` in explicit and guard mode and on `jit-aarch64` under QEMU in
both modes; the ISA gates clean).

### Step 7: temporaries in free pool registers (R) (2026-11-03; dropped)

Not merged: with the code placement controlled, neither CoreMark nor `scan` nor `swizzle` gets faster, so by
S0007's rule the step is dropped. The patch is kept outside the repository for a revisit.

The change (core and both JITs, D39 amendments B, C, D): when `tos` spilled, the value went to the first pool
register that no local is pinned to and no stack entry, popped operand or pending compare holds (a new entry
kind `REG`), else to its slot; `flush` still wrote every entry to its slot, so at edges and calls only pins
were live. A function that used temporaries was rewound and compiled once more with the save set pins ∪
temporaries (x86-64 `rewind` became a real truncation). The spec suite has few such spills: 40 modules
changed on each target (the same 40, exactly the modules whose baseline compilation spills `tos`). CoreMark:
6 of its 29 functions used temporaries on x86-64 (the pins take most of the 8-register pool), 8 on aarch64;
code 33 993 → 34 006 bytes in guard mode. The scalar kernels `addsat`, `dot`, `scan` and `swizzle` changed;
`matmul` and `nearest` were byte-identical. Conditions: 2026-11-03 21:45–22:05 UTC, Intel Xeon @ 2.10 GHz,
4 vCPU VM, CPython 3.13.17, `jit-x86_64` in its default bounds mode (guard, D44), one benchmark process at a
time, load average about 1.0. Baseline: HEAD 3f1168c (`git archive`, `PYTHONPATH`, `waboros.__file__`
checked), alternated run by run with the order swapped each pair. Kernels: `tools/run_simd_bench.py
--targets jit-x86_64 --rounds 5`, the median of the run medians (range).

| benchmark | runs | before | after | change |
|---|---|---|---|---|
| CoreMark (it/s), as built | 8 pairs | 15 710 (15 138–16 872) | 16 893 (15 305–18 609) | +7.5 %, faster in 6 of 8 (placement, below) |
| CoreMark, every function at the baseline's offset, pads 0–56 | 16 pairs | 16 930 (15 276–17 680) | 16 512 (14 995–17 498) | −2.5 %, faster in 4 of 16 |
| scalar `swizzle` (µs per iteration), as built | 16 pairs | 3.95 (3.85–5.31) | 2.87 (2.63–3.91) | −27 %, 16 of 16 (placement) |
| scalar `swizzle`, controlled | 32 pairs | 3.31 (2.77–6.14) | 3.41 (2.83–4.72) | +3 %, faster in 19 of 32 (noise) |
| scalar `scan`, as built / controlled | 16 / 32 pairs | 2.08 / 2.08 | 2.08 / 2.09 | noise (8 of 16, 15 of 32) |
| scalar `addsat`, as built / controlled | 16 / 32 pairs | 4.12 / 4.03 | 4.41 / 3.96 | +7 % as built (3 of 16), −2 % controlled (14 of 32) |
| scalar `dot`, `matmul`, `nearest`, controlled | 32 pairs | 2.86, 3.42, 1.28 | 2.88, 3.42, 1.27 | noise |
| compile time, CoreMark + the three corpus modules (ms, median of 10) | 8 pairs | 205.7 (189.7–238.0) | 222.2 (206.2–239.5) | +8.0 %, within the +10 % bound |

Placement control (LESSONS 51): a scratch patch in both trees put every function at the same offset (each
function's start = the sum of the larger of its two sizes before it, plus a pad of 0, 8, …, 56 bytes of `int3`
for the whole module). The sha256 of each function's range was then equal in both trees for every function but
the changed ones. Under it the as-built CoreMark gain and `swizzle`'s −27 % disappear. In a function with few
pins a temporary costs a push and a pop per call in exchange for a store and a load per spill, and in the
changed functions that did not pay. Every CoreMark run "Correct operation validated". Spec parity held with
the change (54 385 pass, 0 fail, 91 xfail on `jit-x86_64` in explicit and guard mode and on `jit-aarch64`
under QEMU in both modes); `tests/jit` passed in both bounds modes, also under QEMU; the ISA gates were clean.

### Steps 2 (L) and 3 (K) re-measured under the placement control (2026-11-03)

Steps 1b, 5 and 7 lost their gains once every function sat at the same offset in both trees, so the two kept
steps were measured again the same way. Trees: HEAD of 2026-11-03, HEAD without L (the `lea` block in
`x86_64/backend.py` removed), HEAD without K (`fold_addk` returns False; its `jit_dump` list is identical to a
full revert's). The placement control was the step 7 patch: each function starts at the sum of the largest of
its three sizes, plus a pad of 0, 8, …, 56 bytes of `int3`. Starts were equal in all trees, and the sha256 of
every unchanged function's range was equal. Conditions: 22:45–23:37 UTC, Intel Xeon @ 2.10 GHz, 4 vCPU VM,
CPython 3.13.17, `jit-x86_64` in guard mode (explicit where marked), one process at a time, load average
0.2–1.0. The three trees ran in alternated triples, the order rotating through all six. Kernels:
`tools/run_simd_bench.py --targets jit-x86_64 --rounds 5`, scalar build. CoreMark:
`tools/run_coremark_jit.py`. Every run validated.

| benchmark | runs | without | with | change |
|---|---|---|---|---|
| L: scalar `addsat` (µs), as built / controlled | 32 / 64 | 4.12 / 4.03 | 4.12 / 4.03 | 0 % (18 of 32, 28 of 64) |
| L: scalar `scan`, `nearest`, `matmul`, controlled | 64 | 2.13, 1.28, 3.46 | 2.12, 1.27, 3.46 | noise (30, 29, 24 of 64; 3–4 of 8 pads) |
| L: CoreMark (it/s), as built | 8 | 16 937 (16 644–17 276) | 16 757 (16 472–17 069) | −1.1 %, faster in 2 of 8 |
| L: CoreMark, controlled | 32 | 17 158 (16 319–17 865) | 17 383 (16 519–18 228) | +1.3 %, faster in 26 of 32 (within the spread) |
| K: scalar `addsat`, as built / controlled | 32 / 64 | 5.11 / 5.15 | 4.12 / 4.03 | −19 % / −22 %, 29 of 32 / 61 of 64, 8 of 8 pads |
| K: scalar `scan`, controlled | 64 | 2.34 | 2.12 | −9 %, 62 of 64, 8 of 8 pads |
| K: scalar `nearest`, controlled | 64 | 1.43 | 1.27 | −11 %, 55 of 64, 7 of 8 pads |
| K: scalar `matmul`, controlled | 64 | 3.59 | 3.46 | −4 %, 54 of 64, 8 of 8 pads |
| K: `addsat`, `scan`, `nearest`, `matmul`, explicit, controlled | 32 | 6.65, 2.85, 1.67, 3.79 | 5.54, 2.52, 1.47, 3.65 | −17, −12, −12, −4 %, 8 of 8 pads each |
| K: CoreMark, as built / controlled | 8 / 32 | 16 718 / 17 115 | 16 757 / 17 383 | +0.2 % (5 of 8) / +1.6 % (23 of 32), within the spread |

K's kernel gains hold under the control. L's do not: with K in place, L no longer changes `addsat` or
`swizzle`; in `scan` and `nearest` it replaces one `add` with one `lea`, and only `dot` and `matmul` lose a
`mov`, so step 2's kernel gains were taken over by K or were placement. `swizzle`'s as-built +22 % (L, code
unchanged) and +26 % (K) are placement; controlled, they are −0.2 % and −8 %. Decision (maintainer,
2026-11-04): K kept, L reverted. The `lea` block is removed from `x86_64/backend.py`; with it, 21 x86-64
spec modules change against HEAD in each bounds mode (the list equals the measured tree's), and aarch64 is
unchanged.

## JIT low layer: reconnaissance (ROADMAP "Next" 3b, 2026-11-04)

The bounds checks of loads, stores and SIMD memory operators (i32 and i64, guard and explicit) written
once in a shared module over a per-architecture low layer, in a scratch prototype behind
`WABOROS_JIT_TWOLEVEL=1` (the design of [S0011](specs/S0011-jit-low-layer.md); `_end_check` and
`_element` not included). Branch `phase5` after S0009 step 4, the dev VM: Intel Xeon @ 2.10 GHz,
4 vCPU, CPython 3.13.17, load average 1-2 (spec suites ran before, not during, the timing).

**Code identity (D35 lists).** `tools/jit_dump.py --target {jit-x86_64,jit-aarch64}` with
`WABOROS_JIT_BOUNDS=guard` and `=explicit`, switch on against off, 5 979 modules (spec cache and
CoreMark): in each of the four cells 4 lists differ, `memory64/address64.0-3` (i64 offsets beyond 2³¹,
formed differently; S0011 "Identity"); **CoreMark identical on both architectures in both modes**, so
its speed is unchanged by construction and no timed run was made (maintainer, 2026-11-04). Spec suite
with the switch: `jit-x86_64` 63 231 pass, 0 fail, 93 xfail in both modes; the memory subset on
`jit-aarch64` under QEMU 10 988 pass, 0 fail, 2 xfail in both modes; `tests/jit` 337 passed.

**Compile time.** CoreMark and the three corpus modules (`hash`, `mono`, `zip`), `jit-x86_64`, median
of 10 compiles per process (scratch `ctime.py`), `taskset -c 2`, 10 pairs alternated (order swapped
each pair):

| mode | runs | switch off (ms) | switch on (ms) | change |
|---|---|---|---|---|
| explicit | 10 pairs | 218.1 (208.0–235.8) | 227.8 (218.2–248.7) | +3.9 % paired median, slower in 8 of 10 |
| guard | 10 pairs | 195.0 (185.5–220.9) | 210.4 (195.3–215.5) | +3.2 % paired median, slower in 9 of 10 |

An earlier set of 6 pairs (explicit, not pinned, load average 1.7) gave `jit-x86_64` +8 % and `jit-aarch64`
(compiled on this host) +2.4 % by medians; the pinned 10-pair set above is the one recorded. S0011 sets
the bound at +5 %.

Not measured (maintainer, 2026-11-04): the `thin` variant (the offset always added into the index; x86-64
CoreMark code 34 023 → 35 151 bytes in guard mode, 43 020 → 43 848 explicit; aarch64 unchanged), which
would need a placement-controlled run.

### S0011 step 1 in the package (2026-11-05)

`WABOROS_JIT_TWOLEVEL=1` against off, the same tree, the dev VM (Intel Xeon @ 2.10 GHz, 4 vCPU,
CPython 3.13.17, load average about 1; another agent's timings ran pinned to core 3). Compile time:
CoreMark and the three corpus modules, median of 10 compiles per process (scratch `ctime.py`),
`taskset -c 2`, 10 pairs alternated (order swapped each pair), aarch64 compiled on this host (D35).
Code: D35 lists as in S0011 "Identity" (CoreMark identical on both targets in both modes).

| target, mode | switch off (ms) | switch on (ms) | paired median | slower in |
|---|---|---|---|---|
| `jit-x86_64` explicit, first build | 193.5 (188.4–204.3) | 206.9 (192.1–240.5) | +5.7 % | 10 of 10 |
| `jit-x86_64` guard, first build | 185.9 (173.0–201.4) | 188.1 (173.0–214.1) | +0.8 % | 6 of 10 |
| `jit-aarch64` explicit, first build | 203.7 (194.6–216.5) | 202.0 (196.7–217.4) | −0.8 % | 3 of 10 |
| `jit-aarch64` guard, first build | 188.6 (178.9–195.6) | 191.3 (179.8–198.6) | +2.0 % | 7 of 10 |
| `jit-x86_64` explicit, final | 208.7 (204.1–223.1) | 217.2 (204.4–234.9) | +1.1 % (medians +4.1 %) | 7 of 10 |
| `jit-x86_64` guard, final | 192.2 (182.2–233.4) | 192.1 (174.8–215.1) | +0.6 % | 5 of 10 |
| `jit-aarch64` explicit, final | 209.1 (195.6–236.5) | 210.4 (201.1–216.6) | +0.7 % | 6 of 10 |
| `jit-aarch64` guard, final | 195.1 (177.5–252.9) | 187.3 (178.8–221.6) | −3.3 % | 4 of 10 |

"Final": `ll_mem` is `_mem` itself and x86-64's `ll_index32` emits its move directly (one Python
frame less each, S0011 Changes). In one process, the switch toggled each round, 30 rounds,
`jit-x86_64`: explicit +4.8 % before those two changes and +2.4 % after (paired medians), guard +1.7 % and
+1.5 %. Within S0011's +5 % bound.

### S0011 step 2 in the package (2026-11-05)

The end checks of bulk memory and tables and the table element check through the shared layer,
`WABOROS_JIT_TWOLEVEL=1` against off, the same tree, the dev VM (Intel Xeon @ 2.10 GHz, 4 vCPU,
CPython 3.13.17), nothing else running (load average about 1). Code: the D35 lists with the switch
off equal HEAD's, and with it on equal HEAD's switch-on lists, on both targets in both modes
(CoreMark identical).

Standard set: CoreMark and the three corpus modules, median of 10 compiles per process (scratch
`ctime.py`), `taskset -c 2`, 10 pairs alternated, aarch64 compiled on this host (D35). The set
reaches the step-2 code once (one `call_indirect` check in `zip`), so the changes are noise:

| target, mode | switch off (ms) | switch on (ms) | paired median | slower in |
|---|---|---|---|---|
| `jit-x86_64` explicit | 270.1 (224.3–618.6) | 258.9 (225.3–535.5) | −7.8 % | 3 of 10 |
| `jit-x86_64` guard | 206.3 (196.6–262.4) | 205.9 (193.0–224.0) | −1.2 % | 4 of 10 |
| `jit-aarch64` explicit | 219.9 (214.3–231.8) | 226.3 (209.7–239.0) | +1.7 % | 6 of 10 |
| `jit-aarch64` guard | 206.3 (190.5–266.6) | 211.7 (189.2–256.0) | −4.3 % | 4 of 10 |

Worst case: a generated module of 300 functions, each with memory.fill, two memory.copy, table.get,
table.set, table.fill, table.copy, a table64 get and a call_indirect (scratch `ctime2.py`). Median of
5 compiles, the switch toggled in one process for 30 rounds (order swapped each round),
`taskset -c 2`:

| target, mode | switch off (ms) | switch on (ms) | paired median | slower in |
|---|---|---|---|---|
| `jit-x86_64` explicit | 165.7 | 155.3 | −3.3 % | 13 of 30 |
| `jit-x86_64` guard | 169.5 | 162.9 | −6.4 % | 11 of 30 |
| `jit-aarch64` explicit | 275.9 | 284.9 | +1.3 % | 18 of 30 |
| `jit-aarch64` guard | 272.3 | 272.9 | +1.9 % | 16 of 30 |

Within S0011's +5 % bound.

### S0011 step 4: the switch-over (2026-11-05)

The per-backend bodies and `WABOROS_JIT_TWOLEVEL` are removed. The bulk operators' operand rules move
into `common/lower.py`, in aarch64's order per operator (S0011 amendment B2, D53). The base is HEAD
before the step. The step's source files were measured in a scratch copy, with the same files as
committed. The dev VM: Intel Xeon @ 2.10 GHz, 4 vCPU, CPython 3.13.17, load average about 1.

**Code (D35 lists, 5 979 modules), step 4 against the base with the switch on:**
* aarch64: identical in both modes.
* x86-64: the 70 modules with `memory.copy` differ, in both modes. Each has the same length and
  the same instructions in another order: the destination's `add rax, base` moves before the
  source check.
* CoreMark: identical on both targets in both modes.

**Short `memory.copy` (x86-64, native).** This is the only x86-64 change, so it is the only one
timed. Each function runs a loop of 2 000 000 iterations:
* `mc0`: a 16-byte `memory.copy` on memory 0;
* `mc1`: the same on memory 1, whose base and size are context words;
* `tc`: a 4-element `table.copy`, as the control. Its bytes are identical before and after.

The WAT:
```
(module (memory 1) (memory $b 1) (table 32 funcref)
 (func (export "mc0") (param i32) (local i32)
   (loop $l (memory.copy (i32.and (local.get 1) (i32.const 255)) (i32.const 1024) (i32.const 16))
     (local.set 1 (i32.add (local.get 1) (i32.const 1))) (br_if $l (i32.ne (local.get 1) (local.get 0)))))
 (func (export "mc1") (param i32) (local i32)
   (loop $l (memory.copy $b $b (i32.and (local.get 1) (i32.const 255)) (i32.const 1024) (i32.const 16))
     (local.set 1 (i32.add (local.get 1) (i32.const 1))) (br_if $l (i32.ne (local.get 1) (local.get 0)))))
 (func (export "tc") (param i32) (local i32)
   (loop $l (table.copy (i32.and (local.get 1) (i32.const 7)) (i32.const 16) (i32.const 4))
     (local.set 1 (i32.add (local.get 1) (i32.const 1))) (br_if $l (i32.ne (local.get 1) (local.get 0))))))
```
Each process builds the module with `tests.jit.test_compiler.build`. It calls each export once with
1 000 iterations, then times 5 calls with 2 000 000 and keeps the median. The processes run
under `taskset -c 2`, base and step alternated, 40 pairs:

| export | base (ns/iteration) | step 4 | paired median | IQR of the ratio |
|---|---|---|---|---|
| `mc0` | 5.146 (min 4.962) | 5.118 (min 4.912) | 0.989 | 0.968-1.013 |
| `mc1` | 4.738 (min 4.561) | 4.737 (min 4.439) | 0.987 | 0.932-1.027 |
| `tc` (control) | 5.720 | 5.719 | 1.000 | 0.976-1.085 |

Earlier sets of 12 pairs (unpinned) and 20 pairs gave 1.006 / 1.006 / 0.981 and 1.000 / 1.013 /
0.996. The reorder costs nothing measurable.

**Compile time.** The standard set (CoreMark, `hash`, `mono`, `zip`), median of 10 compiles per
process (scratch `ctime.py`), `taskset -c 2`, 10 rounds with the order alternated. aarch64 is
compiled on this host (D35). The step is compared with the base, switch off (the +5 % bound's
reference) and switch on:

| target, mode | base, off (ms) | base, on (ms) | step 4 (ms) | against off | against on |
|---|---|---|---|---|---|
| `jit-x86_64` explicit | 220.1 | 221.8 | 219.9 | −0.4 % (slower in 5 of 10) | +0.3 % |
| `jit-x86_64` guard | 194.9 | 196.1 | 195.4 | +1.6 % (6 of 10) | −0.9 % |
| `jit-aarch64` explicit | 230.1 | 221.4 | 239.2 | +0.8 % (6 of 10) | +4.3 % |
| `jit-aarch64` guard | 204.3 | 222.8 | 218.9 | +0.5 % (5 of 10) | +1.0 % |

The "against" columns are paired medians (each round's ratio, then the median); the three ms
columns are medians of their own column, and their ratios do not agree with the paired figures
(aarch64 guard: 218.9 / 204.3 = +7.1 %; base on against off +9 %), which shows how far the
host drifts between rounds. The step-4 review re-measured `jit-aarch64` guard on CoreMark alone
(6 alternated pairs of median-of-10 compiles, `taskset -c 2`): ratios 0.986 to 1.115, median
+2.4 %. Read the bound as met on the paired medians, with a spread of several percent.

The worst case is step 2's module: 300 functions of bulk and table operators, explicit, median
of 5 compiles.
* `jit-x86_64`: +4.7 % against the switch off over 10 rounds (8 slower), then **+1.9 % over 20
  pairs** (13 slower).
* `jit-aarch64`: −0.2 % over 10 rounds.

Within S0011's +5 % bound.

## `pyc`: `_br` ladders (S0010 step 0)

[S0010](specs/S0010-pyc-branch-emission.md) step 0: the experiment's post-pass (`poc/pyc-bropt/bropt.py`)
applied to the package's `pyc` through the `PYTHONPATH=poc/pyc-bropt/site` hook and `PYC_BROPT=1`
(`BROPT_RULES`, `BROPT_DSE` select the rules), against the same tree without it. Branch `phase5` at the S0010
step 0 commit; the dev VM: Intel Xeon @ 2.10 GHz, 4 vCPU, `taskset -c 3`; CPython 3.11.18, 3.13.18, 3.14.10
and PyPy 3.11.17 (PyPy 8.0.1). Base and variant alternated, the order rotated each round. Noise on this VM:
±5-10 % between runs of identical sources. CoreMark: `poc/pyc-bropt/tools/run_src.py` on the source
`translate()` gives, CoreMark's own it/s, every run "Correct operation validated".

**CoreMark, base against the full rule set** (simplify, chain, to_else, sink; dse on), median (min-max):

| Python | rounds | base (it/s) | full set (it/s) | change |
|---|---|---|---|---|
| CPython 3.11.18 | 5 | 127.8 (124.3–130.7) | 144.1 (133.7–146.5) | +12.8 % |
| CPython 3.13.18 | 5 | 121.3 (121.1–129.9) | 136.9 (129.4–143.8) | +12.8 % |
| CPython 3.13.18 | 10 | 130.5 (119.5–139.2) | 144.5 (136.1–148.3) | +10.7 % |
| CPython 3.14.10 | 5 | 135.7 (134.1–138.5) | 144.9 (142.4–149.7) | +6.8 % |
| PyPy 3.11.17 | 5 | 478.0 (446.0–501.5) | 463.6 (455.6–501.6) | −3.0 %, within the ~10 % spread |

**Per rule, CPython 3.13** (cumulative, then each rule left out of the full set). The 10-round set is the
one that decides; the 5-round set is below it for the left-out rows:

| rules | rounds | median (it/s) | vs base | `_br` in the source |
|---|---|---|---|---|
| base | 10 | 130.5 | — | 635 |
| simplify + chain + to_else (dse on) | 10 | 133.9 | +2.6 % | 322 |
| all four (dse on) | 10 | 144.5 | +10.7 % | 235 |
| base | 5 | 129.1 | — | 635 |
| simplify (dse off / on) | 5 | 127.6 / 127.1 | −1.1 / −1.6 % | 587 / 531 |
| simplify + chain (dse off / on) | 5 | 132.7 / 134.2 | +2.8 / +4.0 % | 569 / 498 |
| simplify + chain + to_else (dse off / on) | 5 | 141.6 / 141.3 | +9.7 / +9.4 % | 492 / 322 |
| all four (dse off / on) | 5 | 140.8 / 142.9 | +9.1 / +10.7 % | 461 / 235 |
| full set without simplify / chain / to_else | 5 | 144.6 / 144.3 / 143.0 | +1.2 / +1.0 / +0.1 % vs full | — |
| full set without sink | 5 | 133.2 | −6.8 % vs full | — |

Sink carries the gain (about +8 % of the +10.7 %). Left out of the full set, simplify, chain and to_else each
change CoreMark by at most 1.2 %, below the noise; the 5-round cumulative rows (+9.7 % without sink) are not
confirmed by the 10-round set (+2.6 %). dse adds nothing beyond the noise; it only cuts the `_br` count
(461 → 235 with all four rules).

**Switch VM** (`poc/bench/switch/vm.wasm` through the package's `pyc`, million VM operations per second,
median of 5 sets of 3 runs; every run the same result, `fb62750f`): CPython 3.11 1.11 → 2.81 (×2.5), 3.13
1.00 → 2.28 (×2.3), 3.14 1.23 → 2.68 (×2.2), PyPy 15.2 → 15.7 (×1.03). `_br` occurrences 158 → 0.

**Switch VM per rule** (follow-up, 2026-11-05, CPython 3.13.18, `taskset -c 3`; 12 configurations, one
process each per round, 10 rounds, order rotated each round and reversed on odd rounds; a value is the
median over the rounds of each process's median of 3 timed runs of 2 000 iterations; every run gives
`fb62750f`). Spreads (max − min over the rounds, relative to the median) are 10-30 %:

| rules | M ops/s | vs base | `_br` in the source |
|---|---|---|---|
| base | 0.959 | — | 158 |
| simplify (dse on) | 1.155 | ×1.20 | 96 |
| simplify + chain (dse on) | 1.497 | ×1.56 | 64 |
| simplify + chain + to_else (dse on) | 1.461 | ×1.52 | 64 |
| all four, dse on / off | 2.595 / 2.436 | ×2.71 / ×2.54 | 0 / 95 |
| all four without simplify / chain / to_else (dse on) | 2.542 / 2.618 / 2.594 | ×2.65 / ×2.73 / ×2.71 | 0 |
| all four without sink (dse on) | 1.485 | ×1.55 | 64 |
| **sink only**, dse on / off | 2.611 / 2.265 | **×2.72** / ×2.36 | 0 / 95 |

Sink alone gives the whole gain of the full set (×2.72 against ×2.71); simplify, chain and to_else left out
of the full set change nothing beyond the spread, and without sink the first three reach only ×1.55. With
sink alone, dse matters here (×2.72 against ×2.36): the 95 stores left without it are the `_br = K` /
`_br = 0` pairs and the cases' `_br = 5` exits that sink leaves in the leaves.

**CoreMark, sink only** (same follow-up, CPython 3.13.18, `poc/pyc-bropt/tools/run_src.py`, three sources
alternated, 10 rounds, every run "Correct operation validated"), median (min-max):

| source | it/s | vs base | `_br` | bytes |
|---|---|---|---|---|
| base | 126.2 (110.5–135.2) | — | 635 | 148 123 |
| sink only (dse on) | 139.0 (129.0–143.8) | +10.1 % | 517 | 156 661 (+5.8 %) |
| all four (dse on) | 140.6 (134.5–151.8) | +11.4 % | 235 | 149 367 |

Sink only with dse off leaves 597 `_br` (158 618 bytes); it was not timed. The difference between sink only
and all four (1.3 %) is below the noise. The source growth of sink only (+5.8 %) comes from the pass
copying guard arms into leaves; the codegen does not emit those guards (S0010, Design).

**Decision (maintainer, 2026-11-05):** S0010 implements sink alone, with the dead `_br` stores that are
evident at emission; simplify, chain and to_else are measured and rejected (sink subsumes their gain).

**Corpus** (`tools/run_corpus.py --targets pyc`, CPython 3.13, 5 alternated rounds): 21/21 agree with and
without the pass; per-call times 2-8 % faster, `inflate 8K` −13.6 %, `deflate 8K` 58.0 → 54.6 ms. As a
post-pass at load it costs 10-15× `generate()`: the whole corpus run takes 4 s without and 15 s with it, so
the rules have to live in the codegen (S0010's design).

**Static** (CPython 3.13, median of 3):

| module | `generate()` | pass | `_br` | max indentation (levels) |
|---|---|---|---|---|
| CoreMark | 27 ms | 366 ms | 635 → 235 | 14 → 13 |
| corpus `zip` | 46 ms | 494 ms | 1 244 → 576 | 30 → 30 |
| switch VM | 1.4 ms | 19 ms | 158 → 0 | 9 → 10 |
| 1 648 spec modules that validate and generate | 215 ms (all) | 2.9 s | 8 805 → 8 104 | +1 level in 38 modules (sink) |

Max indentation grows by one level in the switch VM and in 38 of the 1 648 spec modules, all from sink;
step 2 must watch it (LESSONS 22). Re-checked 2026-11-05 on the pass's output (CPython 3.13.18, the cached
spec modules that validate and generate: 2 366 on this run, 20 of them with `_br`): most of the growth is
`ast.unparse` splitting the codegen's one-line `if c: x` into two lines. Against the base after the same
parse/unparse round trip, only `align.106`, `align64.106` and `switch.0` grow (4 → 5 levels); in the switch
VM the deepest lines after the pass are such split one-liners (`l3 -= …`) at the new leaf depth. Source size: CoreMark 148 123 → 149 366 bytes, `zip` 278 682 → 276 388.

**Line profile** (`poc/pyc-bropt/tools/lines.py`, CPython 3.13, one CoreMark run): line events −27 %; `_br`
guards fall from 31.0 % to 5.4 % of the executed lines.

**Correctness of the pass in the repository**: spec suite on `pyc` with the hook 63 231 pass, 0 fail,
93 xfail in both FBITS modes, `tools/spec_diff.py` empty both ways; corpus 21/21; `tools/fuzz.py 1000 1000`:
938 modules run, 129 rewritten by the pass, 0 mismatches; `tools/fuzz.py 300 5000 --fbits`: 281 run,
0 mismatches. The MicroPython compile check was not done (not installed on the VM).

**The experiment's claims** (S0010 "Summary"): CoreMark +13 % is corrected to +11-13 % on CPython 3.13,
+7 % on 3.14 and none on PyPy; the corpus, spec, fuzz and cost claims are confirmed.

**Measured and rejected** (external experiment, 2026-11-04, `poc/pyc-bropt/README.md`; not re-measured
here), so they are not tried again:

| idea | result |
|---|---|
| `memoryview.cast('I')` instead of `struct.unpack_from` (aligned, inside a function) | ~31 vs ~40 ns per load, but needs a runtime alignment check; with the check or with four shifted views (for unaligned addresses) no faster |
| local aliases of `M`, `U32`, `P32`... as default arguments | within noise |
| wrap as `x & 0xFFFFFFFF`, or `if x >> 32:`, or the walrus expression form | all slower than the statement `if x > 0xFFFFFFFF: x -= 0x100000000` (39 vs 41-47 ns) |
| CPython's experimental JIT (3.14, `PYTHON_JIT=1`) | slower: 103 vs 125 it/s |

The same experiment, same generated source, CoreMark by CPython version: 3.13 ~120, 3.14 ~127 (+5 %),
3.15b4 ~138 (+14 %) (external, not re-measured).

## `pyc`: `_br` ladders (S0010 step 1)

[S0010](specs/S0010-pyc-branch-emission.md) step 1: sink and the evidently dead `_br` stores in
`codegen.py` (`WABOROS_PYC_BR`, default `sink,dse`), against the same tree with `WABOROS_PYC_BR=0` (the
emission before S0010, byte-identical to the step's parent for CoreMark, the corpus, the switch VM and every
spec module, both FBITS modes). Branch `phase5`, 2026-11-05, the dev VM, `taskset -c 3`, uv builds of
CPython 3.11.18, 3.13.18, 3.14.10.

**CoreMark** (`poc/pyc-bropt/tools/run_src.py` on the two `translate()` sources, alternated, the order
reversed every other round, 10 rounds; every run "Correct operation validated"), median (min-max):

| Python | `WABOROS_PYC_BR=0` (it/s) | default (it/s) | change | default faster |
|---|---|---|---|---|
| CPython 3.11.18 | 127.4 (101.4–135.9) | 139.3 (127.5–141.8) | +9.4 % | 10 of 10 |
| CPython 3.13.18 | 130.8 (104.5–138.0) | 143.9 (137.4–152.1) | +10.0 % | 10 of 10 |
| CPython 3.14.10 | 138.3 (130.8–142.4) | 146.1 (115.5–156.3) | +5.7 % | 8 of 10 |

On 3.13 this is the gain `bropt.py` with sink only gave (+10.1 %, "`pyc`: `_br` ladders (S0010 step 0)").

**Switch VM** (`poc/bench/switch/vm.wasm`, CPython 3.13.18, 10 rounds of one process per configuration,
median of each process's 3 runs of 2 000 iterations; every run `fb62750f`): 1.010 (0.923–1.046) → 2.494
(2.247–2.691) M ops/s, ×2.47. Head to head with `bropt.py` (sink only, dse on), 6 rounds, per-round
medians: ours 2.05-2.75, the pass's 2.71-2.87. The two sources compile to the **same bytecode** (`co_code` and constants of
`f0` identical on 3.13; the pass's `ast.unparse` changes only line numbers), so the gap is between processes,
not in the code; the follow-up's ×2.72 was measured in another session.

**Static** (CoreMark, the corpus, the switch VM; `generate()` alternated with the rules off and on in one
process, 101 times each, median; two sessions):

| module | `_br` | bytes | max indentation | `generate()` |
|---|---|---|---|---|
| CoreMark | 635 → 538 | 148 123 → 151 486 (+2.3 %) | 14 → 14 | +8.8 / +9.5 % |
| corpus `hash` | 60 → 53 | 96 125 → 95 936 | 9 → 9 | — |
| corpus `mono` | 64 → 60 | 179 632 → 179 567 | 10 → 10 | — |
| corpus `zip` | 1 244 → 978 | 278 682 → 278 309 | 30 → 30 | +9.6 / +10.6 % |
| switch VM | 158 → 0 | 11 301 → 6 931 | 9 → 9 | 1.4 → 2.2 ms |

All modules together (CoreMark, corpus, switch VM, 2 366 spec modules): `_br` 10 966 → 10 069; 13 modules
change, two of them grow by one level of indentation (`align.106`, `align64.106`: 4 → 5). `bropt.py` with sink
only leaves 517 `_br` in CoreMark: the 21 more here are stores that its general liveness proves dead and the
evident rule does not (a store read by a later `if not _br:` on another path), and fall-through cases that it
copies (`f11`: 34 against 29). `generate()` is at the 10 % budget on `zip` (sink's splices and record shifts,
about 4 ms of 49).

**Depth limits measured for the cap** (`SINK_MAXIND` = 90; LESSONS 87): nested `if`/`else` arms of `pyc`'s
shape, with one-line wrap `if`s, inside 0, 5 or 19 `while True:` loops, compile and run at 99 levels on
CPython 3.11.18, 3.12.16, 3.13.18, 3.14.10, 3.14t, 3.15.2 and 3.15t, in the main thread and in a thread with a
256 KiB stack (the parser's limit of 100 binds); PyPy 3.11.17 passes 139. `if/elif` chains under 98 levels:
~5 400 arms on 3.14/3.15 (~550 in a 256 KiB thread); 3.11 in a 256 KiB thread crashes at 1 000 arms and
compiles 300. The 26 sources that the rules change (13 modules, both FBITS modes) and 25 deep-nest modules
around the cap (`tests/test_pyc_branches.py`, sunk, at the cap and kept) compile, and the latter run with the
results of `WABOROS_PYC_BR=0`, on every one of those interpreters. MicroPython is not installed on the VM: not
checked.

**Correctness**: spec suite on `pyc` 63 231 pass, 0 fail, 93 xfail in both FBITS modes, `tools/spec_diff.py`
against `WABOROS_PYC_BR=0` empty both ways; corpus 21/21 (`inflate 8K` included); `tools/fuzz.py` (default,
`WABOROS_PYC_BR=0`, and that + `bropt.py`): `1000 1000` ran 938 modules, the rules changed 8, 0 mismatches;
`300 5000 --fbits` ran 281, changed 1, 0 mismatches. Corpus timings were not re-measured.

**After two reviews** (three defects, LESSONS 88: the routing of escapes through a sunk case block in a
Python loop was cut; leaves moved with their region kept their old depth; a cut could empty a closed nest's
suite; all fixed, the default output of CoreMark, the corpus, the switch VM and every spec module unchanged by
the fixes, so the speed figures stand). Smith reaches the rules in 0.9 % of its modules, so `tools/fuzz.py
--gen` generates the shapes they rewrite (TESTING.md); it compares the default, `sink` alone, `dse` alone and
`bropt.py` with `WABOROS_PYC_BR=0`, with `BRTREE` drawn from {0, 1, 2, 4}:

| run | modules | changed by the rules | mismatches | `bropt.py` wrong |
|---|---|---|---|---|
| `--gen 5000 1` + `--gen 5000 5001` | 10 000 | 4 045 (40 %) | 0 | 1 |
| `--gen 1000 100000 --fbits` | 1 000 | 421 (42 %) | 0 | 1 |
| smith `1000 1000` | 938 | 8 (0.9 %) | 0 | 0 |
| smith `300 5000 --fbits` | 281 | 1 | 0 | 0 |

Each of the three defects, re-planted in a copy of the code, is found by `--gen 2000 1`: the routing cut 3
times, the depth 23 times (an assertion at translation), the empty suite 2 times (`IndentationError`). The
oracle's disagreements are `bropt.py`'s own: with all four rules it gave 3189 instead of 27 773 for `f(1, 0)`
of a module of the generator's first version, and was wrong in 150 of 300 calls of another, where Wasmtime,
`pyc` and `pyc` with S0010's rules agree (LESSONS 89). Spec suite again 63 231 / 0 / 93 in both FBITS modes, spec_diff
empty both ways, corpus 21/21. CoreMark on CPython 3.13 after the fix, 10 alternated rounds: 125.3
(117.7–130.2) → 138.1 (127.1–147.7) it/s, +10.2 %, faster in 10 of 10; switch VM 0.986 → 2.471 M ops/s,
×2.50.

## Code placement on x86-64 (ROADMAP "Code placement", 2026-11-04/05)

Why a loop's speed moves with code elsewhere (LESSONS 51). Host: the dev VM, CPU family 6 model 207
(Emerald Rapids, "Intel Xeon Processor @ 2.10GHz"), 4 vCPU, no PMU in the VM (no performance counters).
CPython 3.13.17, `jit-x86_64` in guard mode, `taskset -c 3`, one process at a time. A scratch patch put
`PAD` bytes of `int3` before the first function (the code area is page-aligned, so offsets are absolute).
Kernels: the scalar build of the SIMD benchmark (`swizzle`, `addsat`, `scan`, `nearest`, `matmul`), µs per
iteration from a scratch driver over `tools/run_simd_bench.py`'s modules; CoreMark:
`tools/run_coremark_jit.py`, every run validated.

**Hypothesis 2, the JCC erratum: refuted on this host.** Model 207 is not in Intel's list for the erratum
(Skylake-derived models only), and builds with identical 32-byte pictures (every branch at the same offset
mod 32) ran 20-50 % apart.

**Hypothesis 1 as stated, 32-byte loop-header alignment: refuted.** Builds with the same header offset mod 32
ran at different speeds, and with headers aligned to 32 `swizzle` still has two speed levels.

**What holds:** the speed follows the hot loop's position mod 64, i.e. how the fetch blocks between taken
branches fall on 64-byte lines. The exact mechanism is not identified (no PMU).

**Sweep of 0..63 pad bytes** (8 sessions, every pad once per session in shuffled order, median of 5 rounds
per run, then the median over the sessions):

| kernel | range over the pads | fastest – slowest |
|---|---|---|
| `swizzle` | 61 % | 2.71 µs (pad 17) – 4.37 µs (pad 47) |
| `nearest` | 27 % | 1.21 µs (pad 13) – 1.55 µs (pad 59) |
| `addsat` | 14 % | 3.89 µs (pad 59) – 4.46 µs (pad 5) |
| `matmul` | 9 % | 3.36 µs (pad 56) – 3.65 µs (pad 41) |
| `scan` | 8 % | 2.04 µs (pad 58) – 2.20 µs (pad 17) |

Within one pad, the spread between sessions is 11-13 %.

**Prototype: inner-loop headers aligned to 64 bytes** (NOP padding; scratch, not in the package), against the
same tree unaligned, alternated, 4 pads (0, 8, 24, 40) each:

| measure | result |
|---|---|
| CoreMark (it/s) | +5.3 % median, faster in 23 of 24 pairs; spread between pads 6.5 % → 2.0 % |
| scalar `swizzle` | 18 % slower: flat at its slow level |
| corpus (`tools/run_corpus.py --targets jit-x86_64`, 21 cases, all agree) | geomean −0.9 % |
| code size, CoreMark | +10.3 % |

Alignment to 32 gave CoreMark about +4 % in 2 sessions (not pursued further).

Decision (maintainer, 2026-11-05): **not implemented**. It fails ROADMAP's rule (a gain over CoreMark, the
kernels and the corpus, beyond the spread): it trades one kernel for another. Still open: aarch64 measured
natively on the CI arm runners (a `PAD` sweep in 4-byte steps over 0..124 bytes, then alignment to 16, 32 and
64); alignment to 32 only if the item is revisited.

## Code placement on aarch64 (method; ROADMAP "Code placement")

The numbers come from the workflow `.github/workflows/placement-arm.yml` (started by hand,
`workflow_dispatch`). It runs on GitHub's `ubuntu-24.04-arm` runner, CPython 3.13,
one job per bounds mode (guard, explicit). Results go to the job summary and to the artifact
`placement-<mode>` (CSV of every value, the summary, `lscpu`). Its tool, `tools/placement_sweep.py`,
compiles with two aarch64-only knobs. Both are off by default, and the code with them unset is
byte-identical (D35 lists, all spec modules and CoreMark, both modes). `WABOROS_JIT_PAD` puts N
bytes of `nop` before the first function. `WABOROS_JIT_LOOP_ALIGN` aligns every loop header (not only
inner ones, unlike the x86-64 prototype) with `nop`. The configurations are pads 0..124 in 4-byte
steps, plus alignment to 16, 32 and 64 at pads 0, 20, 40 and 60, judged against the same pad
unaligned. Each round runs every configuration once, in a shuffled order, in one process; values
are medians over the rounds (5 by default):
* CoreMark it/s (its `millis()` runs 4 times fast, so a run takes about 3 s; the rate is scaled
  back), validated on every run;
* the scalar kernels of `run_simd_bench.py`, µs per iteration, median of 5 calls of 2000 iterations
  each, checksums equal across configurations;
* the corpus of `run_corpus.py`, µs per call over 20 calls, every result checked; the summary gives
  the geomean.

The decision rule is the x86-64 one.

Results of the first run (2026-11-09, `placement-arm` run 42721947272; Neoverse-N2, 4 vCPU, `lscpu` lists
no `bti`; 5 rounds, 44 configurations, 925 s guard, 1017 s explicit). Pad sweep, median over the pads, the
range between the best and the worst pad, and the spread between rounds:

| bench | guard: median | range | spread | explicit: median | range | spread |
|---|---|---|---|---|---|---|
| CoreMark (it/s) | 19 308 | 3.1 % | 0.3 % | 17 440 | 3.3 % | 0.2 % |
| addsat (µs/iter) | 4.40 | 0.1 % | 0.1 % | 6.06 | 4.3 % | 0.1 % |
| dot | 2.60 | 1.4 % | 0.3 % | 2.60 | 2.2 % | 0.5 % |
| scan | 5.74 | 0.1 % | 0.1 % | 5.74 | 0.2 % | 0.1 % |
| matmul | 5.75 | 0.3 % | 0.1 % | 6.01 | 0.8 % | 0.1 % |
| swizzle | 3.31 | 11.6 % | 0.8 % | 4.38 | 12.3 % | 1.0 % |
| nearest | 0.61 | 0.4 % | 0.4 % | 0.89 | 5.2 % | 0.6 % |
| corpus (geomean µs/call) | 9.62 | 2.2 % | 1.4 % | 9.58 | 2.0 % | 1.5 % |

The best pad differs per benchmark and per mode (CoreMark: 68 guard, 44 explicit), so no single pad is a
target. Loop headers aligned, gain over the same pad unaligned (positive = faster), median over pads 0, 20,
40, 60 (faster at n of 4):

| bench | guard 16 | guard 32 | guard 64 | explicit 16 | explicit 32 | explicit 64 |
|---|---|---|---|---|---|---|
| CoreMark | +0.0 % (2) | +1.4 % (4) | +0.5 % (2) | +1.9 % (3) | +1.2 % (4) | +0.5 % (4) |
| addsat | +0.0 % (4) | −0.1 % (0) | +0.0 % (2) | +1.7 % (4) | +1.6 % (3) | +1.6 % (3) |
| dot | +0.0 % (3) | +0.8 % (3) | +0.9 % (3) | −1.0 % (0) | −0.3 % (0) | −0.3 % (1) |
| scan | −0.0 % (2) | +0.1 % (4) | +0.2 % (4) | +0.0 % (3) | −0.1 % (0) | +0.0 % (3) |
| matmul | +0.0 % (2) | +0.0 % (2) | +0.1 % (3) | +0.5 % (4) | +0.4 % (4) | +0.7 % (4) |
| swizzle | +0.1 % (2) | +0.3 % (2) | −0.3 % (1) | −1.3 % (2) | +1.0 % (3) | +1.0 % (3) |
| nearest | −0.1 % (1) | −0.2 % (0) | −0.3 % (0) | +0.1 % (3) | +0.1 % (2) | +0.4 % (3) |
| corpus | +0.3 % (3) | +0.3 % (2) | +0.3 % (2) | +0.5 % (3) | +0.7 % (3) | +0.4 % (3) |
| code size, CoreMark | +2.6 % | +6.0 % | +12.3 % | +1.8 % | +4.5 % | +10.0 % |

Reading: placement moves CoreMark by about 3 % and `swizzle` by about 12 % between pads, ten times the
spread between rounds, so the effect is real on aarch64 too, but smaller than on x86-64 for CoreMark.
Alignment to 32 is the closest to the rule (CoreMark +1.2…1.4 % at every pad, corpus +0.3…0.7 %), yet `dot`
loses in explicit mode (0 of 4) and `nearest` in guard mode (0 of 4), for +4.5…6 % code; no alignment is a
gain over CoreMark, the kernels and the corpus in both modes. By the rule: **not implemented** (the knobs
stay, off by default). Decision (maintainer, 2026-11-10): not implemented, closed for aarch64.

## Exceptions: the status check after a call (S0012 Q1, 2026-11-05)

The cost of option A's check, which every JIT call site already has for traps (`test eax, eax; jnz
unwind` on x86-64). A scratch script (`nocheck_calls.py`, not committed) compiles the calls
benchmark (`poc/bench/calls/calls.wasm`) twice in one process: once as is, once with
`Backend.call_direct` patched to emit no status test. It runs the two alternated, 11 rounds per
run, three runs.

* Machine: the dev VM (Intel Xeon @ 2.10 GHz, 4 vCPU), CPython 3.13.17, `jit-x86_64`, guard mode
  (the default), branch `phase5` at S0011 step 2. Other agents may have been running.
* Command: `uv run python <scratchpad>/nocheck_calls.py 11`, three times.

| run | `leaf` with check | `leaf` without | `fib(27)` with check | `fib(27)` without |
|---|---|---|---|---|
| 1 | 2.155 | 2.055 | 2.886 | 3.357 |
| 2 | 2.053 | 1.970 | 2.728 | 3.261 |
| 3 | 2.024 | 1.929 | 2.658 | 3.204 |

The values are ns per call (median of 11 rounds).

* **Result.** On `leaf`, removing the check saves 0.08-0.13 ns per call (4-6 %). On `fib`,
  removing it makes the code 16-20 % *slower*. The code moved, and `fib` follows code placement
  (LESSONS 51; "Code placement on x86-64" above).
* **Conclusion.** The check costs at most about 0.1 ns, about 0.2 cycles, per call, below the
  placement spread. S0012's option A adds no instruction to it. Options B and C would not remove
  it either while traps return through frames.

## Exceptions: throw cost (S0012 step 3, 2026-11-05)

One `throw` caught D frames up by a `catch` in a wasm loop (`$f0` calls `$f1` … `$f{D-1}` throws an
i32; the payload is added up). The value is ns per throw-and-catch, the best of 5 runs of 20 000
iterations (5 000 at D = 100).
* "before": the plan's prototype, a throw service called through the host bridge for every throw
  (object from a Python-side arena).
* "after": this step, which allocates the object in machine code from the store's arena.

The two were alternated, three rounds, in one session.
* Machine: the dev VM (Intel Xeon @ 2.10 GHz, 4 vCPU), CPython 3.13.17, guard mode (the default),
  wasmtime-py 50.0.1, branch `phase5` at S0012 step 3. Other agents may have been running.
* Command: `uv run python tools/bench_throw.py` (`--targets jit-x86_64,pyc,wasmtime --frames
  1,10,100 --rounds 3` are the defaults; `--nothrow` times the call chain alone). The table below
  was taken with a scratch version of it, run once per target and D, while the "before" build
  still existed. A run of the committed tool afterwards, same machine:
  * throw-and-catch: D = 1 / 10 / 100: jit-x86_64 15 / 28 / 991, pyc 412 / 1357 / 11 536,
    Wasmtime 194 / 487 / 3218;
  * `--nothrow`, jit-x86_64: 2 / 23 / 984.

| D | jit-x86_64 before | jit-x86_64 after | pyc | Wasmtime |
|---|---|---|---|---|
| 1 | 2121 / 2230 / 2139 | 13 / 12 / 12 | 419 / 419 | 192 / 196 |
| 10 | 2450 / 2572 / 2366 | 39 / 40 / 32 | 1329 / 2067 | 956 / 795 |
| 100 | 4041 / 4373 / 4007 | 1021 / 991 / 976 | 11857 / 11446 | 4020 / 5913 |

* **Result.** At one frame a throw costs about 12 ns, 170 times less than through the bridge, and
  16 times less than Wasmtime, whose throw is a runtime call that walks the frames (S0012, option C).
* **The 100-frame number is call depth, not unwinding.** The same chain returning normally costs
  2 / 19 / 1456 ns at D = 1 / 10 / 100 (`--nothrow`). A wasm call
  chain 100 deep is that slow on this machine whether or not it throws.
* **Memory.** Each throw keeps 8·(1 + payload words) bytes until the store is dropped: 16 bytes
  here. One ctypes object per throw (the plan's first prototype) kept 137 bytes.
