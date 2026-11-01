---
title: jit-aarch64, the second architecture (phase 4, integer core)
kind: spec
id: S0004
status: draft
superseded_by:
related: [D10, D14, D23, D24, D25, D26, D29, D30, D31, D32, D34, D35, D36, D37, R2, R3, R6, S0002, S0003]
owner: maintainer
updated: 2026-11-01
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
  macOS arm64 (`MAP_JIT`) and Windows on Arm execution (phase 5, ROADMAP); guard pages (D29, R6);
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
`w2py: jit-aarch64 incomplete (integer core only); using pyc` (a third D31 message). An explicit
`--engine jit-aarch64` / `W2PY_TARGET=jit-aarch64` runs it. This brings back the readiness guard
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
loader adds `PROT_BTI` when `getauxval(AT_HWCAP2) & HWCAP2_BTI`, so the arm64 CI runner (Neoverse
N2, Armv9.0) checks every pad; without it the pads are untested.

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
`CTR_EL0` and skips what `CTR_EL0.IDC/DIC` make unnecessary). Phase 5: macOS `sys_icache_invalidate`
(libSystem) with `MAP_JIT` + `pthread_jit_write_protect_np`; Windows `FlushInstructionCache`.
x86-64 keeps a no-op. A process where no flush function is found fails the probe (D31 warning),
never runs unflushed code. Code areas are written once and never patched (W^X), so a fresh area
needs only this one flush (`ic ivau` is broadcast to every core, inner-shareable). Remaining
theoretical case: another core that prefetched a freed area's address before it was reused; Linux
`membarrier(SYNC_CORE)` closes it — recorded under R2 for phase 5, not done in phase 4.

**Probe** (`JitAarch64Target.probe`): `platform.machine()` in (`aarch64`, `arm64`), 64-bit,
little-endian; `sys.platform == "linux"` (phase 4: macOS and Windows return "jit-aarch64 on
<platform> comes in phase 5" — the D31 warning, `auto` keeps `pyc`); flush function found; then map
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
1497986):

1. **Per-backend opcode set.** `SUPPORTED` is one module-level set (`common/compiler.py:107-110`)
   used by `unsupported()` (`:129-153`) and the x86 target (`x86_64/__init__.py:50-53`). A backend
   declares its set (`Backend.supported`), `unsupported(mod, ops)` takes it.
2. **Far-branch retry.** `compile_module` emits each function once (`compiler.py:236-241`). New:
   `Backend.func_end` may report "branches out of range"; the core calls `b.rewind(pos)` and runs
   `_Func` again with `far=True` passed to `func_begin`. x86-64 never asks (rel32).
3. **Target glue.** `x86_64/__init__.py:23-73` (probe, `supports`, `compile` with `W2PY_JIT_DUMP`,
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
  -triple=aarch64 -show-encoding` (x86-64 and arm64 hosts; `W2PY_JIT_REQUIRE=1` in CI);
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
  ROADMAP): `pytest tests/jit` with `W2PY_JIT_REQUIRE=1`, the spec runs (wasmtime oracle, `pyc`,
  `jit-aarch64`, `auto`), `tools/no_exec.py` fallback, `tools/stack_info.py`, the ISA gate, the
  cross-compilation diff, CoreMark. `macos-latest` (arm64) and `windows-11-arm` join in phase 5; in
  phase 4 they only check that `auto` warns and runs `pyc`.
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
1. Readiness: bring back a `complete = False` guard so `auto` keeps `pyc` on arm64 until parity
   (recommended), or let `auto` pick `jit-aarch64` and fail modules it cannot compile (violates D25)?
2. Phase 4 platforms: Linux only, macOS/Windows refused in the probe until phase 5 (recommended,
   matches ROADMAP), or Windows Arm64 too (same code, `FlushInstructionCache`, runner exists)?
3. Parity step: add a step 4e (floats, tables, references, bulk memory, tail calls; ~600 lines)
   right after the freeze so `auto` selects `jit-aarch64` before phase 5 (recommended), or leave it
   to phase 5's "rest of the opcode set"?
4. `PROT_BTI` on JIT pages where the CPU has BTI (recommended: it is what tests the pads), or pads
   only?
5. Far branches: retry per function (recommended) or always-far forward conditional branches?

## Changes
* 2026-11-01: created (draft).
