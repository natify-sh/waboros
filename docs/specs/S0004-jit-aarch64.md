---
title: jit-aarch64, the second architecture (phase 4, integer core)
kind: spec
id: S0004
status: implemented
superseded_by:
related: [D10, D14, D23, D24, D25, D26, D29, D30, D31, D32, D34, D35, D36, D37, R2, R3, R6, S0002, S0003]
owner: maintainer
updated: 2026-11-05
---

# S0004 jit-aarch64, the second architecture (phase 4, integer core)

## Summary
Phase 4 (ROADMAP.md) adds `jit-aarch64` to prove that the one-pass core and its backend interface
(D26) are architecture-neutral, then freezes that interface. This spec fixes what the new target
compiles, its register roles and trampolines under the context ABI of
[S0003](S0003-jit-context-abi.md) (reused, not redesigned), the encoder and branch-range rules,
executable memory with I-cache maintenance, the AArch64 semantics the backend must handle, the list
of x86-64 leaks in the shared code, and the tests and CI that prove it. Arm references used: Arm ARM
DDI 0487 (encodings, BTI/PAC, cache maintenance), AAPCS64 and Apple's and Microsoft's arm64 ABI
notes; ideas from Winch and Liftoff arm64 (lowering), Cranelift's aarch64 assembler (immediates).

## Goals / Non-goals
* Goals: the integer core of ROADMAP stage 1 on Linux arm64, passing the same spec commands as
  `jit-x86_64` minus what is declared `unsupported`; one artifact for every OS (D23) at ARMv8.0-A
  (D24); code compiled on x86-64 byte-identical to code compiled on arm64 (D35); a written list of
  what changed in the shared core, and the interface frozen after it.
* Non-goals (phase 4): float arithmetic, tables, references, bulk memory, tail calls (see Scope);
  guard pages (D29, R6);
  optional ISA features (LSE, …; D33); the artifact file (phase 5); copy-and-patch (decided after
  this phase with numbers, R3).

## Interface

### Scope: what `jit-aarch64` compiles in phase 4
Supported opcodes (the rest raise `Unsupported` with the first missing feature, as on x86-64):

| group | opcodes |
|---|---|
| control | `unreachable nop block loop if else end br br_if br_table return call drop select` (typed `select` over numbers) 0x00–0x05, 0x0B–0x10, 0x1A, 0x1B |
| variables | `local.get/set/tee`, `global.get/set` 0x20–0x24 (imported globals by pointer) |
| memory | every load/store 0x28–0x3E (f32/f64 loads and stores are bit-pattern moves), `memory.size`, `memory.grow` (libcall 0), several memories (D37) |
| numbers | `i32/i64.const`, `f32/f64.const` (bit patterns), i32/i64 tests, compares and arithmetic 0x45–0x5A, 0x67–0x8A; `wrap`, `extend_i32_s/u`, sign extensions 0xA7, 0xAC, 0xAD, 0xC0–0xC4; reinterprets 0xBC–0xBF |
| imports | function imports through their records and the host thunk (D32); host functions through the bridge (D34) |

`unsupported` on aarch64 in phase 4: float arithmetic, compares and conversions (incl.
`trunc_sat`), `call_indirect`, tables and `table.*`, `ref.*`, `call_ref`, `br_on_null`/`br_on_non_null`,
`return_call*`, `memory.fill/copy/init`, `data.drop`, `elem.drop`. The xfail file
`tests/spec/xfail/jit-aarch64.txt` is `pyc`'s plus `unsupported: <feature>` lines;
`tests/spec/claims.py` claims `multiMemory` only.

**`auto` does not select `jit-aarch64` until its expected failures equal `pyc`'s (D25).** The target
carries `complete = False`; `target_order("auto")` skips an incomplete JIT and warns once with
`waboros: jit-aarch64 incomplete (its expected failures differ from pyc's); using pyc` (a third D31 message). An explicit
`--engine jit-aarch64` / `WABOROS_TARGET=jit-aarch64` runs it. This brings back the readiness guard
S0002 step 2 had (removed in step 6), as an amendment of S0002.

Cheap to add right after the freeze (not in phase 4's acceptance): floats are easier than on
x86-64 — `frintz/frintm/frintp/frintn` give trunc/floor/ceil/nearest, `fmin/fmax` have wasm's NaN
and ±0 rules, `fcvtzs/fcvtzu` saturate (= `trunc_sat`), all ARMv8.0; tables, references, bulk
memory and tail calls reuse the x86-64 lowering shapes through context words. Estimate ~600 lines.

### Register roles (AArch64, every OS)
| register | role (S0003 x86-64 equivalent) | across a wasm call | across a host call (bridge) |
|---|---|---|---|
| `x28` | context (`r15`) | preserved | preserved (AAPCS64 callee-saved) |
| `x27` | value-stack frame base, slot k = `[x27, #8k]` (`r12`) | moved for frame overlap, restored | preserved |
| `x26`, `x25` | memory 0 base, size (`r13`, `r14`); one `ldp x26, x25, [x28, #128]` loads both | preserved | **reloaded** after the bridge |
| `x0` | status on return; top-of-stack cache in a function (`rax`) | clobbered | clobbered |
| `x1` | bridge argument (slots), scratch | clobbered | clobbered |
| `x9`–`x15` | scratch inside one backend method (`rcx`, `rdx`) | clobbered | clobbered |
| `x16`, `x17` | scratch; **every** indirect `br` goes through them (BTI, below) | clobbered | clobbered |
| `x19`–`x24` | pool, first choice: AAPCS64 callee-saved, a host never touches them | a function saves the pins it uses | preserved by the host |
| `x2`–`x8` | pool, second choice | a function saves the pins it uses | saved by the bridge, all of them (LESSONS 2) |
| `x29`, `x30` | frame pointer, link register: every function and trampoline pushes a frame record `{x29, x30}` and sets `x29 = sp` | preserved | preserved |
| `x18` | **never used** (platform register: Apple, Windows TEB; D23) | — | — |
| `sp` | 16-byte aligned **at all times**, not only at calls (Linux enables SP alignment checks at EL0) | | |
| `v0`–`v7`, `v16`–`v31` | NEON scratch (`popcnt`; floats later) | clobbered | clobbered |
| `v8`–`v15` | never used (low halves callee-saved: no save needed) | — | — |

The pool is 13 registers (x86-64: 8), preferred order `x19…x24, x2…x8` (`choose_pins` takes the
pool in order). Whether 13 pays over 8 is measured on CoreMark on the arm64 runner, not assumed.
Native stack per frame: frame record 16 + pins ≤ 7·16 + 16 for a record call's saved context =
**≤ 144 bytes** (x86-64: 96); the S0003 budget rule (bytes) is unchanged, so the maximum wasm
recursion depth on aarch64 is lower for the same budget. Frames stay below one page (Windows probes).

### Trampolines (one set; AAPCS64 and Windows Arm64 share it)
AAPCS64 (Linux), Apple arm64 and Windows Arm64 agree on everything the trampolines touch: arguments
in `x0`–`x7`, result in `w0`/`x0`, callee-saved `x19`–`x29`, `sp`, `d8`–`d15`; they differ only in
what our code never uses (`x18`, Apple's 128-byte red zone and variadic and sub-word argument rules,
Windows unwind data and stack probes). So **one entry, one bridge, one stub, one `tail_switch` serve
all of them**; the artifact's `ENTRIES` gives the same offset for `aapcs64` and `win-arm64`.

* **Entry** `int32_t entry(x0 = ctx, x1 = frame, x2 = code)`: `bti c`; `stp x29, x30, [sp, #-96]!`;
  `mov x29, sp`; `stp` x19–x28 (five pairs); `x28 ← x0`, `x27 ← x1`, `ldp x26, x25, [x28, #128]`;
  `blr x2`; `cbz w0, done`; `str x0, [x28, #8]`
  (`TRAP_CODE`; w0 was written by a 32-bit op, so x0 is zero-extended); restore; `ret`.
* **Bridge**, reached by wasm code as `ldr x16, [x28, #8*HOST_BRIDGE]; blr x16` with
  **`x0` = context word of the host function, `x1` = slots**: `bti c`; frame record; save `x2`–`x8`
  and `ctx[HOST_ERROR]` (the enclosing call's preset) in 64 bytes; `ctx[HOST_ERROR] ← 13`;
  `ldr x16, [x28, x0, lsl #3]`; `mov x0, x28`; `blr x16`; `ldr w0, [x28, #56]`; put the saved word
  back; restore; `ret` with the status in `w0` (D34; the C return value is ignored). The call site
  then reloads `x26`/`x25` and does `cbnz w0, unwind`.
* **Host thunk** (per module, the record target of a host import): `bti c`; frame record;
  `mov x1, x27`; `mov w0, #word`; bridge call; reload `x26`/`x25` (S0003 3e: it returns to callers
  that do not); `ret`.
* **sp stub**: `bti c; mov x16, sp; str x16, [x27]; mov w0, #0; ret` (frame[0] = sp as a wasm
  function sees it).
* **tail_switch** (D30), entered by `br x17` after the caller dropped its frame, with
  **`x16` = the record** and the caller's return address **in `x30`** (not on the stack, as on
  x86-64): `bti c`; `adr x17, restore; cmp x30, x17; b.eq hop`; `stp x28, x30, [sp, #-16]!`;
  `ldr x28, [x16, #16]`; reload x26/x25; `ldr x17, [x16]; blr x17`; `restore:` `ldp x28, x30,
  [sp], #16`; reload x26/x25; `ret`. `hop:` `ldr x28, [x16, #16]`; reload; `ldr x17, [x16];
  br x17`. The restore frame is 16 bytes and saves `x30`, which `blr` overwrites; every `ret`
  returns to its `bl`/`blr`.

**Traps return through frames** (S0003): a trap point branches to a cold stub
`mov w0, #code; b unwind`; `unwind` is the epilogue (pins, `ldp x29, x30, [sp], #16`, `ret`) with
`w0` intact; after every wasm call `cbnz w0, unwind`. No `sp` reset, no `x30` games: compatible with
Arm GCS (Guarded Control Stack, Armv9.4, Linux 6.13 opt-in), the arm64 analogue of CET shadow
stacks (R2).

**BTI** (R2). `bti c` at every address reached by `blr` or `br x16/x17`: the entry, bridge, sp
stub, `tail_switch`, every function (reached from the entry, records, tail calls) and the host
thunk. Jump-table targets get `bti j` pads. Because every indirect branch uses `x16`/`x17`
(BTYPE 01), `bti c` also accepts the tail-call `br`. `bti` is in the HINT space: a NOP before
ARMv8.5, so the baseline holds. Linux enforces BTI only on pages mapped `PROT_BTI` (0x10); the
loader adds `PROT_BTI` when `getauxval(AT_HWCAP2) & HWCAP2_BTI`. The arm64 Linux CI runner's CPU
has no BTI (`PROT_BTI` reads 0 there), so the pads are proven under QEMU user mode in the x86 `jit`
job, which enforces BTI: a missing pad is a SIGILL.

**PAC: not used.** Return addresses are left unsigned (no `paciasp`/`autiasp`). Reason: wasm
values and locals live on the value stack, never on the native stack, so wasm stores cannot reach a
return address; signing would cost two instructions per frame for no protection. C code built with
`-mbranch-protection=standard` signs its own LR inside its own frames, which does not affect us.
arm64e (pointer-signed function pointers) is used by Apple system binaries only, not by Python;
out of scope.

### Encoder (`x86_64/encoder.py` counterpart, ~300 lines)
Fixed 32-bit little-endian instructions, labels and fixups only (LESSONS 6). Forms: `add/sub(s)`
immediate (imm12, optional `lsl #12`), shifted and extended register (`uxtw` for i32 addresses);
`and/orr/eor/ands` register and bitmask immediate (encoder computes N:immr:imms or reports "not
encodable"); `movz/movn/movk` (a constant takes the shortest of movz/movn + movk, or one `orr` from
`xzr` when it is a bitmask immediate); `madd/msub`, `sdiv/udiv`, `lslv/lsrv/asrv/rorv`,
`ubfm/sbfm` (shift-immediate and extend aliases), `extr`, `clz/rbit`, `csel/csinc/cset`, `ccmp`;
`ldr/str` (b/h/w/x, signed variants) unsigned-offset, register-offset (`lsl` or `uxtw`), `ldur/stur`,
`ldp/stp` (pre/post-index for frames); `b`, `bl`, `b.cond`, `cbz/cbnz`, `br/blr/ret`, `adr`,
`adrp`; `bti c/j`; NEON `fmov` (GPR↔D/S), `cnt`, `addv`; `mrs` is not needed.

Branch ranges: `b`/`bl` ±128 MiB, `b.cond`/`cbz` ±1 MiB, `tbz` ±32 KiB, `adr` ±1 MiB, `adrp`
±4 GiB. Rules: `tbz` only for short skips inside one method; a module whose code exceeds 128 MiB is
`Unsupported`. **A function whose conditional branches do not reach** (body > ~1 MiB of code, e.g.
very large interpreter loops) is compiled again in *far* mode: every conditional branch to a label
becomes `b.!cond +8; b label`. The backend detects it at `func_end` (a fixup out of range) and the
core rewinds the buffer to the function's start and runs that function once more (interface change
2 below). No literal pools: constants are `movz/movk` sequences.

**Jump tables** keep the x86-64 shape: 32-bit entries relative to the table, in the data area after
all code (never disassembled, `data_start` as today); index ≥ n → default via `cmp; b.hs`; then
`adrp x16, T; add x16, x16, :lo12:T; ldrsw x17, [x16, w_i, uxtw #2]; add x16, x16, x17; br x16`;
each entry lands on a `bti j; b label` pad. `adrp` requires the code to be loaded at an address
≡ 0 mod 4096 — every `mmap`/`VirtualAlloc` gives that; ARTIFACT.md states it for aarch64 `CODE`.

**Checked against `llvm-mc -triple=aarch64 -show-encoding`** form by form (registers incl. `sp` vs
`xzr` encodings, every shift/extend, immediate edge cases: bitmask patterns of every element size,
imm12 with and without `lsl #12`, branch offsets at both range ends), like `test_encoder.py`; runs on
the x86-64 `jit` job (LLVM 18 there has the aarch64 target, checked) and on the arm64 job.

### Executable memory and the I-cache
AArch64 does not keep the instruction cache coherent with data writes. `execmem.load` becomes: map
RW, write, `mprotect` RX (plus `PROT_BTI` where supported), then **invalidate the range** before the
first call: Linux: `__clear_cache(start, end)` through ctypes from the process (`CDLL(None)`, else
`libgcc_s.so.1`; it does `dc cvau`/`dsb ish`/`ic ivau`/`dsb ish`/`isb` with the line sizes from
`CTR_EL0` and skips what `CTR_EL0.IDC/DIC` make unnecessary). macOS: `sys_icache_invalidate`
(libSystem) with `MAP_JIT` + `pthread_jit_write_protect_np`; Windows: `FlushInstructionCache`.
x86-64 keeps a no-op. A process where no flush function is found fails the probe (D31 warning),
never runs unflushed code. Code areas are written once and never patched (W^X), so a fresh area
needs only this one flush (`ic ivau` is broadcast to every core, inner-shareable). Remaining
theoretical case: another core that prefetched a freed area's address before it was reused; Linux
`membarrier(SYNC_CORE)` closes it — recorded under R2 for phase 5, not done in phase 4.

**Probe** (`JitAarch64Target.probe`): `platform.machine()` in (`aarch64`, `arm64`), 64-bit,
little-endian; an OS with an executable-memory path (Linux, macOS, Windows; others return
"executable memory unavailable", the D31 warning, and `auto` keeps `pyc`); flush function found; then map
`bti c; mov w0, #42; ret` W^X, flush, call it once.

## Semantics
AArch64 behaviour the backend handles (Arm ARM DDI 0487; wasm semantics as in Wasmtime):

* **Division** does not trap: `sdiv/udiv` by 0 give 0 and `INT_MIN / -1` gives `INT_MIN`. Explicit
  checks give the wasm traps: `cbz y → INTEGER_DIVISION_BY_ZERO` (all four); `div_s`:
  `cmn y, #1; ccmp x, #1, #0, eq; b.vs → INTEGER_OVERFLOW` (x − 1 overflows only for `INT_MIN`).
  `rem_s/rem_u` are `sdiv/udiv` + `msub`; `INT_MIN rem_s -1` then gives 0 with no extra check.
* **Shifts and rotates**: the register forms use the count modulo the width, as wasm does; `rotl` is
  `neg` + `rorv`; constant counts are masked at compile time.
* **clz** is native, **ctz** = `rbit` + `clz` (32 or 64 for 0, as wasm); **popcnt** =
  `fmov d0, x` (i32: `fmov s0, w`), `cnt v0.8b, v0.8b`, `addv b0, v0.8b`,
  `fmov w, s0` (Advanced SIMD is in the baseline).
* **Zero-extension** (LESSONS 5): every 32-bit (`w`) operation clears bits 63:32, so i32 values stay
  zero-extended in registers and slots; `i32.wrap_i64` and `extend_i32_u` are `mov wD, wS` (always
  emitted, also onto the same register); `extend_i32_s` is `sxtw`.
* **Flags** (LESSONS 1): only `cmp/cmn/tst/ccmp` and S-suffixed instructions write NZCV; a constant
  is never loaded with a flag-setting form; compare-and-branch/set/select stay single interface
  calls, as on x86-64. `== 0`/`!= 0` branches use `cbz/cbnz` (no flags).
* **Memory**: explicit bounds checks (D29): `addr (uxtw) + offset + size` in 64 bits, unsigned
  compare with the size word, `b.hi → MEMORY_OUT_OF_BOUNDS`; unaligned loads and stores are allowed
  for normal memory on Linux, macOS and Windows (no `HEAP_MISALIGNED`); no atomics, so the weak
  memory order does not matter yet (threads are a later feature).
* **Stack checks and polls** (S0003): prologue `ldr x16, [x28, #48]; cmp sp, x16; b.lo` →
  `STACK_OVERFLOW`; value stack `x27 + 8·frame_slots` (patched `movz/movk` at `func_end`) against
  word 5; poll `ldr x16, [x28, #16]; cbnz x16 → INTERRUPT` at entry and loop headers.
* **Immediates and offsets**: context words `[x28, #8w]` for w < 4096 and slots `[x27, #8k]` for
  k < 4096 use the scaled imm12 form; beyond, a `movz/movk` offset in a scratch register.

## The x86-64 leaks in the shared code (phase-4 deliverable)
The D26 interface itself held: `Backend` has no flags, no fixed registers, no x86 immediate rules
(`Imm` is the full value; each backend decides what fits). What must change (line numbers at HEAD
of 2026-11-01):

1. **Per-backend opcode set.** `SUPPORTED` is one module-level set (`common/compiler.py:107-110`)
   used by `unsupported()` (`:129-153`) and the x86 target (`x86_64/__init__.py:50-53`). A backend
   declares its set (`Backend.supported`), `unsupported(mod, ops)` takes it.
2. **Far-branch retry.** `compile_module` emits each function once (`compiler.py:236-241`). New:
   `Backend.func_end` may report "branches out of range"; the core calls `b.rewind(pos)` and runs
   `_Func` again with `far=True` passed to `func_begin`. x86-64 never asks (rel32).
3. **Target glue.** `x86_64/__init__.py:23-73` (probe, `supports`, `compile` with `WABOROS_JIT_DUMP`,
   `instantiate`) is generic except three imports; it moves to `common/target.py` (second real use)
   and each architecture gives its probe stub, `Backend` and `trampolines()`.
4. **I-cache flush and `PROT_BTI`** in `common/execmem.py:33-36, 71-80` (`seal`/`load`), no-op on
   x86-64.
5. **Target registry and readiness**: `targets/base.py:108` (`TARGETS`), `:113` (`_MACHINES`
   gains `aarch64`, `arm64`), `:122-141` (`auto` skips `complete = False`, third D31 message);
   `tests/spec/claims.py:23` gets a `jit-aarch64` entry.
6. **Pool order** becomes part of the contract: `choose_pins` (`compiler.py:278-279`) gives the
   most-used locals the first pool registers; aarch64 lists host-preserved ones first.
7. **Tests hard-wired to x86-64**: `tests/jit/test_{abi,compiler,execmem,float,memories,probe,
   tables,tail}.py` import the x86 target, encoder or backend (S0003 says each test runs on every JIT
   target of the machine). They are parametrised over the native JIT target; tests that write host
   functions or probes in machine code get an aarch64 twin (e.g. a host that zeroes `x2`–`x17`).
8. **Tools hard-wired to x86-64**: `tools/isa_check.py:90` (GNU objdump `i386:x86-64`),
   `tools/isa_all.py:22,27`, `tools/run_coremark_jit.py:36`, `tools/stack_info.py:6` gain
   `--target`/`--arch`.
9. **Documents**: ARTIFACT.md part 3 "Tail calls" (`ARTIFACT.md:171-174`: "scratch register
   (x86-64: `rdx`)", "return address on top of the stack") and the bridge's register protocol become
   a per-architecture table (below); S0003's "rsp" wording for words 0 and 6 becomes "sp".

Not leaks (checked): `common/context.py` (layout, libcalls, records), `common/instance.py` (ctypes
types, stack limits, host callbacks, services; only comments say `rsp`), the lazy stack with one
`tos` register, frame overlap, `call_*`/`jump_table`/`select_cmp` signatures.

### Context ABI: neutral, with one per-architecture table
Words, records, libcalls and their slot immediates, word 7 (D34) and the trap codes need no change:
everything is 64-bit little-endian words addressed from the context register. What is per
architecture, and so part of the contract a foreign host (D36) must implement — added to ARTIFACT.md
part 3 as a table, an amendment of v1 without a version bump (v1 is fixed in phase 5):

| | x86-64 | aarch64 |
|---|---|---|
| bridge call | `eax` = word, `rdx` = slots; `call` | `x0` = word, `x1` = slots; `blr x16` |
| bridge preserves | pool (`rbx rbp rsi rdi r8–r11`), r12–r15 | `x2`–`x8`, `x19`–`x28`, `x29`, `sp` |
| `tail_switch` | record in `rdx`, return address at `[rsp]` | record in `x16`, return address in `x30` |
| landing pad | `endbr64` | `bti c` (functions, trampolines), `bti j` (jump-table pads) |
| `CODE` alignment | none | load address ≡ 0 mod 4096 (`adrp`) |

Header: `arch` = 2 (aarch64), `isa_level` = 1 (ARMv8.0-A with FP and Advanced SIMD), `isa_features`
= 0; `ENTRIES` = `{aapcs64: o, win-arm64: o}` (same offset).

## Test plan
* **4a** `uv run pytest tests/jit/test_encoder_aarch64.py` → every form equals `llvm-mc
  -triple=aarch64 -show-encoding` (x86-64 and arm64 hosts; `WABOROS_JIT_REQUIRE=1` in CI);
  `uv run pytest tests/jit -k "entry or trap or reentry or host or interrupt or stack"` on the arm64
  runner with hand-assembled functions (S0003 test plan, aarch64 twins), BTI enforced.
* **4b** on the arm64 runner: `uv run python -m tests.spec.run --engine jit-aarch64` → 0 unexpected;
  `uv run python tools/spec_diff.py pyc jit-aarch64 --allow-unsupported` → only `unsupported`
  differences; `uv run python tools/run_coremark_jit.py poc/in/core.wasm --target jit-aarch64`
  passes its self-check, with a BENCHMARKS.md row alternated with Wasmtime and `pyc` on the same
  runner; `--engine auto` gives `pyc`'s numbers with one `JitUnavailableWarning`; x86-64 spec
  numbers and CoreMark unchanged (shared-core changes must not move them; code byte-identical).
* **ISA gate** `uv run python tools/isa_all.py --target jit-aarch64` → every blob disassembles with
  `llvm-mc --disassemble -triple=aarch64` (hex input; no aarch64 binutils needed on x86-64) into
  mnemonics of an ARMv8.0-A allowlist (`hint #34/#36` = `bti c/j` allowed); a planted `ldadd` (LSE)
  or `crc32x` fails it. Checked here: LLVM 18's disassembler rejects `ldadd` without `+lse`.
* **4c, cross-compilation (D35)**: `tools/jit_dump.py --target jit-aarch64` compiles (never runs)
  every spec module and CoreMark and writes a sha256 list; the x86-64 `jit` job uploads it, the
  arm64 job produces its own and `diff` must be empty; the arm64 job then runs the spec suite from
  that code. The same check runs in reverse for `jit-x86_64`.
* **CI**: a new job on `ubuntu-24.04-arm` (native; QEMU stays for architectures without runners,
  ROADMAP): `pytest tests/jit` with `WABOROS_JIT_REQUIRE=1`, the spec runs (wasmtime oracle, `pyc`,
  `jit-aarch64`, `auto`), `tools/no_exec.py` fallback, `tools/stack_info.py`, the ISA gate, the
  cross-compilation diff, CoreMark. `macos-latest` (arm64) and `windows-11-arm` ran the same job (decision 2 of the Changes); since 2026-11-03 the `arm64` job is Linux only and those two run the harness cells of `platforms.yml` (D47).
* **Without arm64 hardware** (checked in this container, Ubuntu 24.04 x86-64):
  `apt-get install qemu-user libc6-arm64-cross libgcc-s1-arm64-cross`, then
  `uv python install cpython-3.13-linux-aarch64-gnu`, then
  `qemu-aarch64 -L /usr/aarch64-linux-gnu <that python> …` runs aarch64 CPython; a W^X `mmap` +
  `mprotect` + ctypes call of `mov x0, #42; ret` returned 42, and `__clear_cache` is found through
  `CDLL(None)`. Docker `--platform linux/arm64` is not available here (no daemon, no binfmt).
  Caveat: QEMU does not model a stale I-cache, so a missing flush passes there; only the native
  runner proves the flush.

## Plan of steps
| step | content | acceptance | size (lines) |
|---|---|---|---|
| 4a | encoder; trampolines (entry, bridge, sp stub, `tail_switch`, thunk shape); execmem flush + `PROT_BTI`; probe; target glue moved to `common/target.py`; arm64 CI job (tests/jit only) | encoder test vs `llvm-mc`; ABI tests on the arm64 runner; x86-64 suite unchanged | encoder ~300, abi ~180, glue/execmem ~80, tests ~350 |
| 4b | integer-core backend; per-backend opcode set; far-branch retry; `complete` flag; xfail and claims | spec + `spec_diff` + CoreMark on the runner; x86-64 code byte-identical | backend ~700, core ~40, tests ~150 |
| 4c | ISA gate `--target`; `jit_dump.py`; cross-compilation diff both ways; spec and CoreMark in the arm64 job | CI green; BENCHMARKS row | tools ~150, CI ~40 |
| 4d | independent review (reviewer agent) of 4a–4c; the leak list above confirmed against the diff; interface frozen (decision + S0002 change); copy-and-patch decision prepared with numbers | review findings closed; `python3 tools/check_docs.py` | docs |

Each JIT step follows CLAUDE.md: plan approved first, review after.

## Alternatives considered
* **Separate entries/bridges for Linux and Windows Arm64**: not needed, the callee-saved sets are
  equal; two copies would only be a second thing to test.
* **Unwinding by resetting `sp`**: rejected as on x86-64 (S0003; GCS would fault).
* **PAC on return addresses** (`paciasp`/`autiasp`): no gain with two stacks (above), two
  instructions per frame. Revisit only if a host process requires signed returns.
* **Jump-table base by `adr`** (±1 MiB): breaks for modules over 1 MiB of code; `adr` of a near
  label + `movz/movk` distance works anywhere but costs two more instructions. `adrp` + `add` chosen.
* **Always-far conditional branches** instead of the retry: no core change, but one more
  instruction at every forward branch site in every function, for a case that needs > 1 MiB
  functions. **Veneer islands** (V8's assembler): exact but complex in one pass.
* **Upfront far mode from the wasm body size**: needs a safe bytes-per-instruction bound; a loose
  bound puts ordinary large functions in far mode, a tight one is fragile.
* **Own I-cache routine** (`mrs ctr_el0`, `dc`/`ic` loops) emitted as code: it would need a flush
  before it can run (bootstrap), relying on the kernel syncing fresh executable pages; `__clear_cache`
  exists in every glibc and libgcc process we target. Kept as the fallback to consider for musl
  without libgcc.
* **QEMU instead of a native runner**: misses I-cache bugs and is slow; ROADMAP keeps it for
  architectures without runners.
* **A pool of 8 (as x86-64)**: simpler comparison; 13 used first and measured against 8.

## Open questions
Resolved by the maintainer on 2026-11-01 (see Changes); none open.

## Changes
* 2026-11-01: created (draft).
* 2026-11-01: CI job `macos-pythonorg` added (python.org installer CPython, hardened runtime): it
  asserts the JIT runs or `auto` falls back to `pyc` with the D31 reason; installer pinned by sha256 in a follow-up change the same day.
* 2026-11-01: approved by the maintainer with these decisions, which override the draft where it
  differs: (1) a `complete = False` guard keeps `auto` on `pyc` (with the D31 warning) on arm64 until
  `jit-aarch64` fails exactly what `pyc` fails; (2) **all three OSes in phase 4** — Linux
  (`ubuntu-24.04-arm`), macOS arm64 (`macos-latest`: `MAP_JIT`, `pthread_jit_write_protect_np`,
  `sys_icache_invalidate` via ctypes; the probe refuses cleanly under a hardened runtime without the
  entitlement) and Windows on Arm (`windows-11-arm`: `FlushInstructionCache`), each with a CI job;
  (3) **step 4e, parity, right after the freeze** (floats, tables, references, `call_indirect`,
  bulk memory, tail calls, typed function references), so `auto` selects `jit-aarch64` before
  phase 5; (4) `PROT_BTI` on JIT pages only where the CPU reports BTI (`AT_HWCAP2`); `bti` is a HINT
  (a `nop` on ARMv8.0), so it is baseline and the ISA gate allows it, nothing else above ARMv8.0;
  (5) far branches: compile normally and recompile only a function whose conditional branches do
  not reach, in far mode.
* 2026-11-01 (step 4a): encoder (`aarch64/encoder.py`; 28 529 forms equal to `llvm-mc`, plus
  branch-range ends, labels and `mov_imm` sequences checked by disassembly), trampolines and the
  host-thunk shape (`aarch64/abi.py`) exactly as above, executable memory per OS
  (`common/execmem.py`: Linux `__clear_cache` + `PROT_BTI` when `AT_HWCAP2` has BTI; macOS arm64
  `MAP_JIT` mapped read-write-execute, the calling thread switched by
  `pthread_jit_write_protect_np` around the write, `sys_icache_invalidate`; Windows
  `FlushInstructionCache`; x86-64 unchanged), the probe on all three OSes (decision 2), the glue
  in `common/target.py` (`JitTarget`; x86-64 code byte-identical: 562 blobs of the spec modules
  and CoreMark), `jit-aarch64` registered with `complete = False` and the third D31 message, and
  a CI job on each arm64 OS. Differences from the plan above: the `complete` guard came in 4a,
  not 4b (the target is registered now); `aarch64/backend.py` already has the frame, trap, poll
  and call methods of the D26 interface (what the ABI tests drive; 4b adds the operations); a
  constant takes one `orr` only when `movz`/`movn` would need more than one instruction (as
  LLVM's `mov`). Tests: `tests/jit/test_encoder_aarch64.py`, `tests/jit/test_abi_aarch64.py`
  (run here under QEMU 8.2 with BTI enforced: `python3 tools/run_arm64.py -m pytest
  tests/jit/test_abi_aarch64.py`); x86-64-only tests skip on other machines (`tests.jit.native`)
  instead of failing under `WABOROS_JIT_REQUIRE=1`.
* 2026-11-01 (step 4b): the integer-core backend (`aarch64/backend.py`), every operation of the
  Scope table, as specified above (division checks `cbz` + `cmn`/`ccmp`/`b.vs`; `ctz` = `rbit` +
  `clz`; popcnt through `cnt`/`addv`; bounds checks in 64 bits; jump tables by `adrp` + `ldrsw`
  with `bti j` pads; records called with x28 saved in a 16-byte frame). The leaks as fixed:
  (1) `Backend.supported`, `unsupported(mod, ops)`; the sets are `compiler.SUPPORTED` (x86-64)
  and `compiler.INTEGER_CORE` (aarch64); (2) `func_begin(..., far)`, `func_end` returns whether
  every branch reached, `Backend.rewind(pos)`; the aarch64 backend checks its conditional-branch
  fixups at `func_end` (`Asm.out_of_range`) and in far mode emits `b.!cond` over `b` (x86-64
  always returns True); (3) and (4) were done in 4a; the aarch64 `supports`/`compile` stubs are
  removed; (5) `claims.py`: `jit-aarch64` claims bulk memory and multi-memory like the other
  targets (the orchestrator's brief; differs from "multiMemory only" above), bulk memory is
  `unsupported` until 4e; (6) no change (`choose_pins` already takes the pool in order);
  (7) `tests/jit/test_compiler.py` runs on the machine's JIT target (`tests.jit.native_jit`); the
  float, table, tail-call and several-memories tests stay x86-64-only until 4e (they use
  out-of-scope operations); (8) `tools/run_coremark_jit.py --target`; the ISA gate, `isa_all` and
  `stack_info` are step 4c. Checked and not leaks: the core names no register beyond `b.tos`,
  passes immediates whole (`Imm`), and the jump-table format lives in the backend's `finish`.
  x86-64 code byte-identical (1 713 spec modules and CoreMark, sha256 before/after); aarch64
  code compiled on x86-64 equals code compiled under QEMU (D35, 1 244 modules). Results under
  QEMU 8.2 (`--in-process`: no child interpreters there): core 5 864 pass, 0 fail (`pyc`:
  20 509; LESSONS 38), multi-memory 759, bulk memory 343; `tests/spec/xfail/jit-aarch64.txt` is
  `pyc`'s 91 entries plus `unsupported:` ones (13 whole files as `reason~=^unsupported:`);
  `spec_diff pyc jit-aarch64 --allow-unsupported` lists only `unsupported`. Difference from the
  test plan: CoreMark does not compile on the integer core (13 distinct float opcodes in 5 of
  its 29 functions), so it runs only after 4e; a bytecode VM
  (`poc/bench/switch/vm.wasm`, integer-only) matches `pyc` instead
  (`tests/jit/test_compiler_aarch64.py`, also the far-branch retry on a 1.4 MB function).
* 2026-11-01 (step 4b review): the far-mode recompile is a statement, not inside an `assert` (under
  `python -O` it was stripped and the next code ran in its place: a crash); a function that does
  not fit even in far mode, and a module whose `b`/`bl` exceed ±128 MiB, raise `Unsupported` as
  this spec says (`OutOfRange` mapped in the backend's `finish`); `tests/test_source_rules.py` keeps
  compiling/emitting calls out of `assert`s in the JIT.
* 2026-11-01 (step 4c): the aarch64 ISA gate and the cross-compilation check. `tools/isa_check.py
  --arch aarch64` disassembles with `llvm-mc --disassemble -triple=aarch64` (one word a line; the
  invalid-encoding warnings carry the line, so the offender is named). Decision: the allowlist is
  LLVM's own default decoder (ARMv8.0-A only: LSE, CRC32, RDM, dotprod, fp16 arithmetic, RCPC, crypto,
  SVE, MTE, JSCVT, complex come back invalid, checked on LLVM 18) plus two refusals for what LLVM
  decodes under a generic name: `hint #N` except `#34/#36/#38` (`bti c/j/jc`; pointer-authentication
  and `esb` HINTs are refused) and system-register moves it cannot name (`sb`, `cfinv`, `axflag`
  print as `msr S0_...`). `llvm-objdump` is not used. `tools/jit_dump.py --target T OUT` compiles
  (never runs) every `.wasm` of the spec cache plus `spectest.wasm` and CoreMark and writes
  `<path> <sha256 of code> <data_start>` or `rejected` / `unsupported: <feature>`; 5 969 modules, 1 245
  compiled by `jit-aarch64` (1 714 by `jit-x86_64`), identical lists on the x86-64 host and under
  QEMU for both targets (`run_arm64.py tools/jit_dump.py`, no `--in-process` needed: it starts no
  child). `tools/isa_all.py --target jit-aarch64` uses it: 222 distinct blobs, 0 failed, on x86-64.
  CoreMark does not compile on the integer core (`unsupported: opcode 0x63`), so it has no row
  and no hash until step 4e; it is in the lists already and will appear then. CI: the x86-64
  `jit` job runs both ISA gates and uploads its lists (`jit-dump-x86`); the `arm64` job (needs
  `jit`; Linux only for the spec steps, where the spec cache is fetched) runs the spec suite on
  `jit-aarch64`, the aarch64 ISA gate, and diffs its own lists with the artifact for both targets.
  `tools/stack_info.py` reports the JIT part for the machine's own JIT target (QEMU: `jit
  (jit-aarch64): sp ... native budget 512 KiB`). Tests: `tests/jit/test_isa_aarch64.py`.
* 2026-11-01 (step 4d, review fixes): what each arm64 OS runs in CI. Linux (`ubuntu-24.04-arm`):
  `tests/jit` (all but the two x86-64 ISA tests), `stack_info`, the spec suite on `jit-aarch64`,
  `--engine auto` (selects `pyc` with the third D31 message; pyc's numbers), `tools/no_exec.py`
  with `auto` (MDWE, `pyc`), the ISA gate, the D35 diff both ways. macOS and Windows: `tests/jit`
  natively, including `test_compiler` and the far-branch tests, `stack_info`; no spec run there
  yet, because the spec cache needs the converter tools (`wasm-tools` wheels) and they were not
  checked for those runners; the spec result does not depend on the OS, the executable memory
  path does, and `tests/jit` covers it. BTI: the arm64 Linux runner's CPU has no BTI
  (`PROT_BTI` 0), so the x86 `jit` job runs the aarch64 ABI and compiler tests under QEMU user
  mode, which enforces BTI (about 70 s locally). `jit_dump.py` hashes the whole `Compiled`
  (code, data start, functions, records, signatures, thunks, layout), not only the code. The xfail
  reader now accepts an `unsupported: <feature>` entry only for exactly that failure (LESSONS 39).
* 2026-11-01 (step 4d): reviewed; the backend interface is frozen (D39, maintainer). Step 4e (parity) adds opcodes and methods only.
* 2026-11-01 (step 4e, parity): `jit-aarch64` compiles the x86-64 opcode set (`supported =
  compiler.SUPPORTED`; `INTEGER_CORE` removed) with the frozen interface (D39): only methods were
  added, no signature or contract changed, x86-64 code byte-identical (1 714 modules and CoreMark,
  `jit_dump` before/after). Floats: bit patterns in x registers and slots, `fmov` into `v0`/`v1`
  inside one method (`v8`-`v15` never used); `fadd/fsub/fmul/fdiv/fsqrt`; `fmin/fmax` directly
  (Arm's FPMin/FPMax are wasm's min/max: NaN if either is NaN, -0 < +0; `fminnm` is not);
  `frintp/frintm/frintz/frintn` for ceil/floor/trunc/nearest; `fcmp` + `cset` with `mi`/`ls` for
  lt/le (false when unordered); `scvtf/ucvtf` (one rounding), `fcvt` for promote/demote;
  abs/neg/copysign as `and/eor/orr` with bitmask immediates (bits exact). `trunc_sat` is
  `fcvtzs/fcvtzu` alone; the trapping truncations check NaN (`fcmp v0, v0; b.vs` →
  BAD_CONVERSION_TO_INTEGER), then both bounds, exact in either format (→ INTEGER_OVERFLOW), as
  Cranelift's aarch64 lowering. Tables, `call_indirect` (bounds → null → signature, Wasmtime's
  order), `call_ref`, `null_check`, `ctx_addr`, `table.get/set/fill/copy`, `memory.fill/copy`
  (both ranges checked before the first write; memmove direction by the real addresses) in the
  x86-64 shapes with x9-x15 as scratch; the record of an indirect call is kept in x15 so frame and
  slot moves (x16/x17) cannot clobber it. Tail calls: arguments to slots 0.., pins and `{x29, x30}`
  restored (x30 is the caller's return address again), then `b label`, or `br x17` to the record's
  code when its context is ours, else `br x17` to `tail_switch` with x16 = the record (D30, as
  specified above). Encoder: `fp2`, `fp1`, `fcmp`, `fcvt_int`, checked against `llvm-mc` with the
  other forms. Results under QEMU 8.2 (`--in-process`): core 20 509, bulk memory 7 482,
  multi-memory 912 pass, 0 fail, 92 expected failures, exactly `pyc`'s and `jit-x86_64`'s
  (`tests/spec/xfail/jit-aarch64.txt` = `jit-x86_64.txt`); CoreMark passes its self-check; ISA
  gate 561 blobs, 0 failed; D35 lists equal on x86-64 and under QEMU for both targets. So
  `complete = True` (decision 1): `auto` selects `jit-aarch64` on arm64, and the D25 parity test
  covers it. The third D31 message stays for a future incomplete target (no target has it now;
  `test_auto_skips_an_incomplete_jit` patches `complete`). The float, table, tail-call and
  several-memories tests run on the machine's JIT (x86-64 register and objdump tests stay
  x86-only). CI: the x86-64 `jit` job runs the spec suite and `test_tail.py` under QEMU (BTI
  enforced); the `arm64` job runs CoreMark (self-check required) and the spec suite on all three
  OSes, and fails unless `auto` selects `jit-aarch64`.
* 2026-11-01 (step 4e review): no correctness issue (differential float fuzz, bulk/table overlap, tail-call traps, far mode vs Wasmtime aarch64 under QEMU). The float code assumes the default FPCR (round to nearest, FZ=0, DN=0) and never sets it, as the x86-64 code assumes the default MXCSR (S0003); a host import that changes FPCR changes results.
* 2026-11-01: answered — the python.org 3.13.17 macOS CPython (hardened runtime) runs `jit-aarch64`: it carries `disable-executable-page-protection`; probe None, `auto` → `jit-aarch64`, ABI tests pass (RISKS R2). Phase 4 complete.
* 2026-11-02: the D31 message for an incomplete JIT says "its expected failures differ from pyc's"
  instead of "integer core only" (no longer true once floats and the rest landed).
* 2026-11-02: status implemented (phase 4 done: steps 4a-4e, the review, the macOS follow-ups; ROADMAP). The frozen interface of D39 was amended three times: twice by S0005 (the `simd` method and the v128 operand meaning, step 5c1, both backends; then `move_v128`, after the SIMD benchmark investigation) and once by S0007 (amendment A, step 3; see the 2026-11-04 line below); DECISIONS D39.
* 2026-11-02: measured for S0006's musl step: the stock `python:3.13-alpine` arm64 image (Alpine
  3.24.3, musl 1.2.6) has no libgcc, so `__clear_cache` is missing: the probe fails ("no
  instruction-cache flush function") and `auto` gives `pyc` with the D31 warning. After `apk add
  libgcc` the JIT runs (spec suite under QEMU 8.2.2: 54 385 pass, 91 expected failures). The
  "own I-cache routine" alternative above is the open way to musl without libgcc.
* 2026-11-04: D39 amendment A (S0007 step 3): the operand kind `AddK` (a pinned i32 plus a constant mod 2³²) is accepted by the aarch64 `move` and as the `load`/`store` address (one `add`/`sub` into the index scratch `w9`, or into the destination); nothing else of this spec changed.
* 2026-11-04: S0008 step 2 (relaxed SIMD) adds NEON forms `fmla`/`fmls` (vector, `.4s`, `.2d`) and `addp .8h` to `aarch64/simd.py`'s `FORMS`, each checked against `llvm-mc` (`test_neon_forms_match_llvm_mc`); all ARMv8.0-A.
* 2026-11-05: S0012 step 4 (exceptions, D52): the frame's `error` label and the six exception
  methods; the call sites' `cbnz w0` goes to `error` (the epilogue outside a `try_table`, so code
  without one is unchanged). `jit-aarch64` claims `exceptions/`.
