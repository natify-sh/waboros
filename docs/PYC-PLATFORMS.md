---
title: pyc across Python versions and architectures - a measurement for the CI matrix
kind: reference
status: draft
updated: 2026-11-03
---

# pyc across Python versions and architectures (measured 2026-11-03)

Method: a copy of the working tree (uncommitted edits included; `poc/` left out) in `repo/`; the pinned spec cache reused.
Metric: `python -m tests.spec.run --engine pyc` (54 385 pass / 91 xfail / 1 171 skip is the clean baseline) and `pytest tests -k pyc`.
Raw data stayed in the session scratchpad (not in the repository); the commands below reproduce it.
Native x86_64: CPython 3.11.18, 3.12.16, 3.13.18, 3.14.10, 3.14.10t, 3.15.2, 3.15.2t, PyPy 7/3.11.17.
Other architectures: i686 native (manylinux_2_28_i686 container on the x86_64 kernel, real x87), armv7l / aarch64 / s390x
under qemu-user (manylinux rootfs exported, absolute symlinks made relative, `qemu-X -L rootfs`; spec run `--in-process`
because there is no binfmt for the child processes; in-process vs child gave identical counts on x86_64 3.15).
Not measured: Windows, macOS, musl, Pyodide, Android, iOS (no hardware or emulator here).

## 1. Static audit (src/waboros/targets/pyc/)
All `struct` formats carry an explicit `<` (runtime.py:11-26, simd.py:24-28, codegen.py:47); no `sys.byteorder`, `array`,
`memoryview` casts, `ctypes`, `platform`, time or `sys.maxsize` anywhere in pyc. Host-dependent spots:

| where | what | differs by |
|---|---|---|
| runtime.py:36-41 `_f32` (also codegen.py:1104-1188 demote/convert) | f64->f32 rounding via `unpack('<f', pack('<f', x))`; NaN bits depend on C `PyFloat_Pack4/Unpack4` | CPython version (3.<=13 quiets sNaN in pack; 3.14 keeps it; 3.15 keeps in pack but quiets in unpack on x86_64 only), 32-bit x86 (x87) |
| runtime.py:371-399 `_i32_f32/_f32_i32/_i64_f64/_f64_i64` | sNaN is carried as a double through `unpack('<d')` / returned from C | x87 (i686 returns doubles in st0 and quiets sNaN) |
| runtime.py:15-26 loads (`F64L`, `U32`...) and codegen.py:737-765 | `U32(M,a)`, `M[a]` with a = i32 + offset, up to ~2^33; out of range is expected to raise IndexError / struct.error (mapped in `__init__.py:64`) | Py_ssize_t width: on 32-bit builds a >= 2^31 raises OverflowError, which `_run` does not map to a trap |
| `__init__.py:68` RecursionError -> trap, no `setrecursionlimit` | stack depth | Python version (3.11 vs 3.12+ C recursion), 32-bit; all spec stack tests pass 3.11-3.15 here, nothing seen |
| runtime.py:82-87 `_grow` -> `bytearray.extend` | MemoryError not caught | address space (32-bit); not hit by the spec suite |
| runtime.py:281-306 `_fceil/_ffloor/_ftrunc/_fnearest`, `math.copysign/ldexp/trunc`, `round` | exact IEEE ops in the C library | none seen; `x*1.0` quieting of NaN is hardware-dependent but did not show |
| codegen.py:50 `repr(v)` float literals | shortest round-trip, own dtoa | none |
| store.py:23 `threading.get_ident` | one thread per store | free-threaded builds: 3.14t == 3.14, 3.15t == 3.15 in results |

## 2. Measured: spec metric (fail counts; xfail 91 and skip 1171 identical everywhere)
| cell | fail | cause |
|---|---|---|
| x86_64 CPython 3.11, 3.12, 3.13 | 0 | |
| x86_64 3.14, 3.14t, PyPy 3.11 | 4 | conversions.wast:608,610; simd_conversions.wast:159,163: `f32.demote_f64` of an sNaN keeps the signalling bit (0x7FA00000, expected quiet) |
| x86_64 3.15, 3.15t | 0 | |
| aarch64 (qemu) 3.13, 3.15 | 0 | |
| s390x big-endian (qemu) 3.13 | 0 | |
| s390x 3.14, 3.15 | 4 | same 4 commands as x86_64 3.14 (3.15 on s390x does not quiet in unpack as x86_64 does) |
| armv7l (qemu) 3.13 / 3.14 | 156 / 160 | 156 x `OverflowError ... C ssize_t` (address.wast, memory_trap*, multi-memory/address*, memory_trap*) / + the same 4 demote |
| i686 native 3.13, 3.14 | 244 | 156 x OverflowError (same ids as armv7) + 88 x x87 NaN bit patterns (float_exprs 20, float_memory 16+8, float_misc 16, conversions 8, float_literals 8, select 8, simd_splat 3, local_tee 1) |
| i686 3.15, 3.15t | 310 | the same + 66 more OverflowError (memory_trap*): 3.15 raises OverflowError where 3.13/3.14 raised IndexError |

Pytest `-k pyc` (107 collected): 3.11, 3.12, 3.13, 3.15, 3.15t: 107 pass, 5 skip. 3.14 and 3.14t: 2 fail
(`tests/jit/test_simd_x86.py::test_class_d_and_s_operators_match_pyc`, the same demote sNaN). PyPy: pytest cannot be collected
(`_tracemalloc` missing), not a pyc issue. On the qemu/i686 cells wasmtime is not installable (no wheel / "only 64-bit"), so 6-44 tests
fail with ModuleNotFoundError / RuntimeError (environment, not pyc). Real pyc failures there: 6 x
`test_discarded_values_that_may_trap_still_run[...]` (OverflowError, armv7 and i686) and `test_f32_nan_payload_kept`
(i686 only; **not** marked xfail: 0xFFC00001 instead of 0xFF800001; the ROADMAP names two tests, there are three).
Probe on x86_64 (`nan_probe.py`): `struct.pack('<f', double-sNaN)` gives 7fe00000 on 3.11-3.13, 7fa00000 on 3.14, 3.15, PyPy.
`unpack('<f', 7fa00000)` then repack: 7fe00000 (<=3.13, 3.15), 7fa00000 (3.14); i686 3.13 gives 7fe00000 where the NaN is a plain load (the known x87 case reproduced).

## 3. Which axes matter
* Python 3.11 / 3.12 / 3.13: identical (0 diff). Free-threaded == GIL build for the same minor. Not an axis today.
* Python 3.14 (and PyPy): differs, by one root cause (`_f32` demote of sNaN). Python 3.15 hides it on x86_64 and shows it on s390x.
  The version axis and the architecture axis are not separable for this bug, but it is one bug: an explicit NaN branch in `_f32`
  removes the dependency on the C library in every cell.
* 32-bit (Py_ssize_t 32): two separate bugs, identical on i686 and armv7: OverflowError on addresses >= 2^31 (156 spec commands), and
  (x86 only) x87 quieting (88). armv7 (VFP, no x87) adds nothing over i686. 3.15 widens the OverflowError set by 66.
* Endianness: no difference (s390x == x86_64 apart from the version-specific NaN above).
* aarch64 vs x86_64: no difference measured (3.13, 3.15).
* OS: not measured; no OS-specific code path in pyc (static audit), so any OS difference would have to come from the C library
  (float pack/unpack, libm) - the same class as the NaN case. Windows/macOS arm64/x86_64 are 64-bit IEEE-SSE/NEON like the measured cells.

## 4. Recommendation (minimal, per the maintainer's narrow-matrix rule)
Cells for pyc now:
1. x86_64 Linux, CPython 3.11 (the floor; 3.12 and 3.13 gave identical results).
2. x86_64 Linux, CPython 3.14 (the only measured version that fails on x86_64: demote sNaN; catches regressions the 3.11/3.15 pair would pass).
3. x86_64 Linux, CPython 3.15t (newest, free-threaded; results equal 3.15, threading covered).
4. One i686 cell, CPython 3.13 (the lowest the manylinux i686 image offers; 3.13 == 3.14 there): it is the only cell that shows both 32-bit bugs
   (ssize_t overflow, x87). Add 3.15 on i686 only if the OverflowError fix should be checked against the 3.15 variant (66 extra commands).
Can wait until the platform is taken on: armv7 (strict subset of i686), s390x / any big-endian (no pyc-specific difference; the
s390x 3.14/3.15 failures are the same demote bug), aarch64 and macOS arm64 (0 diff), Windows (not measurable here, no pyc-specific code),
PyPy (same as 3.14; pytest cannot even collect), 3.12 / 3.13 / 3.14t / 3.15 non-t on x86_64 (identical to a listed cell).
The JIT cells are independent: they exercise mmap/ABI/signal handlers per OS and architecture, which the pyc results say nothing about.
They already run the pyc suite as a side effect (31 s native for the spec, ~12 s pytest), so a pyc check on an arch that has a JIT cell costs nothing extra;
the pyc-only cells are just the ones without a JIT (i686 now; armv7, Pyodide, etc. later).
Fixes implied (not done, read-only): (a) map OverflowError like IndexError in `_run` or mask addresses; (b) keep f32 NaN bits out of struct
float round trips (demote and loads) - after (a) and (b) the measured difference set is empty and the 3.14 and i686 cells become regression checks.

## 5. Fixes done (2026-11-03) and what is left
* Demote: `_demote` (runtime.py; used by `f32.demote_f64` and `f32x4.demote_f64x2_zero`) quiets a NaN with `x * 1.0` before the
  C pack. Wasm requires an arithmetic NaN; Wasmtime quiets and keeps the payload, so does this. `_fsqrt` got the same NaN branch
  (PyPy's `math.sqrt` kept the signalling bit: f64.wast:2436,2438 and simd_f64x2_arith.wast:5137,5139, which the table in section 2
  attributed to nothing; they were a fifth and sixth pair hidden next to the demote four). Result: x86_64 3.13, 3.14 and PyPy: 54 385 / 0 / 91.
  The generated code of CoreMark calls neither function, so the hot path is unchanged (pyc CoreMark 98.38, 31.5 s wall, passes its check).
* `OverflowError` is caught with `IndexError`/`struct.error` in `_run` (`__init__.py`). Nothing else in `pyc` raises it: float to int
  conversions test their range and raise `Trap` (`_tr`, `_sat`), float arithmetic overflows to inf, and `ldexp` is only called on a
  value already below 2^53 times a shift for an i64 that fits f32. i686 CPython 3.13 and 3.14: 244 -> 88 failures, 0 of them ssize_t.
* Left: the 88 x87 commands on i686 (3.13 and 3.14 alike; float_exprs 20, float_memory 16+8, float_misc 16, conversions 8,
  float_literals 8, select 8, simd_splat 3, local_tee 1). Probe in the i686 container: `struct`, `memoryview.cast('d')` and
  `array('d')` all turn a signalling NaN into a quiet one when the float object is created or read; no way of making a float
  from bits keeps it. Fixable only by a different representation of the NaN:
  1. carry f32/f64 as ints (bit patterns) on 32-bit x86 only: separate code generation mode for every float op, load, store, select,
     local; 64-bit hosts unchanged (CoreMark cost 0 there), large diff, the second code generator to maintain;
  2. carry f32 as the integer bits everywhere: touches the CoreMark hot path (every f32 op unpacks and packs), not measured, certainly a loss;
  3. accept: document that `pyc` on 32-bit x86 does not preserve signalling NaN payloads, keep the 88 commands and three tests as
     expected failures on that host only (the harness conftest is not part of this step).
