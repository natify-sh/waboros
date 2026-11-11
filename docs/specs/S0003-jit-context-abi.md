---
title: JIT context ABI and entry (phase 3, x86-64)
kind: spec
id: S0003
status: implemented
superseded_by:
related: [D10, D14, D15, D18, D23, D24, R2, R3, R6, S0002]
owner: maintainer
updated: 2026-11-11
---

# S0003 JIT context ABI and entry (phase 3, x86-64)

## Summary
What `jit-x86_64` code expects from the runtime in phase 3, and what the runtime may rely on: the
context words in use, the entry trampolines, register roles, the two stacks, how traps, host calls
and host exceptions return, interrupt polling, re-entrancy, and the ISA-level rule. It refines
[ARTIFACT.md](../ARTIFACT.md) part 3 (same word numbering, same version 1, which phase 5 fixes);
it is not a new format. The runtime side is [S0002](S0002-target-interface.md).

## Goals / Non-goals
* Goals: every rule LESSONS.md 1–9 and RISKS.md R2/R3 ask for, stated once and testable; an ABI a
  second architecture can follow by filling in its own register table (D23).
* Non-goals, deferred: the artifact file (ARTIFACT.md part 2: header, sections, `TRAPMAP`,
  `RELOCS`, thin/fat files) and serialization — phase 5; v128 slots — stage 2 (multi-memory: done, see Changes; tables, funcref and `call_indirect` are in step 3d, S0002); `FUEL`, `EPOCH_DEADLINE`; guard pages and the signal handler
  (R6) — phase 5; `Trap.frames`; CPU feature detection (phase 3 emits the baseline only); the C
  reference loader (optional, D18).

## Interface

### Context (`uint64_t ctx[]`, one per instance, stable address, 8-byte aligned)
| word | name | phase 3 |
|---|---|---|
| 0 | `SAVED_SP` | **unused, zero**: traps return through frames; kept for the phase-5 fault handler |
| 1 | `TRAP_CODE` | written by the trampoline when it returns a non-zero status |
| 2 | `INTERRUPT` | non-zero → the next poll returns status `INTERRUPT` (12); any thread may set it |
| 3, 4 | `FUEL`, `EPOCH_DEADLINE` | deferred, zero |
| 5 | `VSTACK_LIMIT` | value-stack address a frame must not cross → `STACK_OVERFLOW` |
| 6 | `NSTACK_LIMIT` | lowest native `sp` allowed → `STACK_OVERFLOW`; written by the runtime before each entry |
| 7 | `HOST_ERROR` | status of the current host call: preset to 13 by the bridge, written last by the host function (step 6, D34) |
| 8 | `HOST_BRIDGE` | address of the host-call bridge for the host's convention |
| 9 | `TAIL_SWITCH` | address of the cross-context tail-call trampoline (step 3e, D30) |
| 10 | `EXCEPTION` | address of the store's pending-exception word (S0012) |
| 11 | `EXN_REFILL` | the exception arena's refill service, called through the bridge (S0012) |
| 12, 13 | `ARENA_CURSOR`, `ARENA_LIMIT` | addresses of the store's arena cursor and limit (S0012) |
| 14 | `EXIT_RECORD` | address of the store's exit record `{sp, pc, frame}`: written by the bridge, saved and restored by every entry (S0013) |
| 15 | reserved | zero |
| 16 … | memory *i*: `base`, `size_bytes` | memory 0 also in r13/r14; any other read here at each access |
| … | table *i*: `base`, `size` | element words (record address / extern handle, 0 = null); step 3d |
| … | global *i*: 1 word | defined: the value; imported: a pointer to the owner's word (same target, S0002) |
| … | function import *i*: 1 word | address of a host function (signature below) |
| … | libcall *j*: 1 word | same signature; the runtime services the module uses (`memory.grow`, `table.grow`, segment init — never computation, D25), in a fixed order |
| … | signature *k*, function record *r* | step 3d: canonical type ids for `call_indirect`; `{code, type id, context}` records (ARTIFACT.md part 3) |
| … | tag *t* | the tag's identity, imported tags first (S0012) |

### Calling conventions
```c
int32_t entry(uint64_t *ctx, uint64_t *frame, const void *code);   /* per host convention */
int32_t host (uint64_t *ctx, uint64_t *slots);                     /* imports and libcalls */
```
* `frame[0..n)` holds the arguments, `frame[0..m)` the results after a 0 return: one slot per value,
  i32 zero-extended, i64 as is, f32/f64 bit patterns (ARTIFACT.md).
* Status: `entry` returns `0`, or a trap code of ARTIFACT.md part 3, or `13` `HOST_ERROR`. `host`
  writes the same set to `ctx[HOST_ERROR]` as its last action; its C return value is ignored (D34).
* **`host` is uniform**: the arguments are in `slots[0..n)`, the function writes
  its results to `slots[0..m)`. One C signature for every import makes one bridge per convention
  possible, gives multi-value and a trap/exception status for free, and removes the C-ABI
  zero-extension hazard (LESSONS 5: results come back through slots the runtime masks). ARTIFACT.md
  part 3's typed `ret f(ctx, args…)` is replaced.

### Register roles (x86-64, all conventions)
| register | role | across a wasm call | across a host call (bridge) |
|---|---|---|---|
| `r15` | context | preserved | preserved |
| `r12` | value-stack frame base (slot k = `[r12 + 8k]`) | caller moves it for frame overlap, restores | preserved |
| `r13`, `r14` | memory 0 base, size | preserved | **reloaded** from the context after the bridge |
| `rax` | status on return; top-of-stack cache inside a function | clobbered | clobbered |
| `rcx`, `rdx` | scratch (shift count, `idiv`, bridge arguments) | clobbered | clobbered |
| `rbx rbp rsi rdi r8–r11` | pool: pinned locals (`w2jit2`) | callee-saved (a function pushes the pins it uses) | preserved by the bridge |
| `xmm0–xmm5` | float scratch; floats live in slots | clobbered | clobbered |
| `xmm6–xmm15` | never used in phase 3 (Win64 callee-saved: no save needed) | — | — |

### Trampolines and the host bridge (one of each per convention, in the code area)
* **Entry, SysV** (`rdi`, `rsi`, `rdx`): `endbr64`; push `rbx rbp r12–r15` (+ pad to 16); `r15 ← ctx`,
  `r12 ← frame`, `r13/r14 ←` memory 0 words; `call rdx`; if `eax ≠ 0` store it in `ctx[1]`; pop;
  `ret`. **Win64 shape** (`rcx`, `rdx`, `r8`): also push `rsi rdi` (callee-saved there); the same
  body. The code itself is identical under both (D23). Since S0013 3a the pad is 40 bytes and
  holds, at `[rsp, rsp + 32)` during the call, the enclosing exit record and the frame; the record
  is put back right after the call, before the status test.
* **Bridge**: wasm code sets `eax` = context word index of the import or libcall, `rdx` = slot
  pointer of the arguments, then `call [r15 + 8*HOST_BRIDGE]`. SysV bridge: `endbr64`; push the pool
  registers SysV lets a callee clobber (`rsi rdi r8–r11`; all of them, not only the caller's pins:
  frames above keep pins there too, LESSONS 2); push `ctx[HOST_ERROR]` (an enclosing host call's
  preset); `ctx[HOST_ERROR] ← 13`; `rdi ← r15`, `rsi ← rdx`; `call [r15+8*rax]`; `eax ←
  ctx[HOST_ERROR]`; pop it back into the word; pop; `ret` with the status in `eax`. Win64 bridge: saves `r8–r11` only, passes `rcx`/`rdx`, and
  reserves the 32-byte shadow space. Both write the store's exit record (word 14) through r8/r9
  after pushing them, and clear its `sp` after the host returns (S0013 3a). The wasm call site then
  reloads `r13`/`r14` and does `test eax, eax; jnz unwind`.

## Semantics

**Stack alignment** (LESSONS 3). `rsp ≡ 0 (mod 16)` at every `call` executed in the code area;
each function's prologue pushes its pins plus a pad to keep it. A test build option makes every call
site check it (`test spl, 15; jnz` → trap) and the encoder tests run with it on.

**Two stacks** (ARCHITECTURE.md, "Calls, stack depth and recursion"). Values and locals live on the
**value stack** the runtime maps per store (frame overlap as in `w2jit`); every prologue checks
`r12 + 8·frame_slots ≤ ctx[VSTACK_LIMIT]`. The **native stack** holds only return addresses, pushed
pins and pads (at most 96 bytes per frame with a function-record call in flight — return address,
eight pins, pad, the saved context — plus 16 per `tail_switch` restore frame; never more than one page at once: no stack probes on Windows,
D23; the SysV red zone is never used). Every prologue checks `rsp ≥ ctx[NSTACK_LIMIT]`. The runtime
sets that limit at the first JIT entry of the store on the stack, whatever the store's depth:
`limit = sp − min(max_wasm_stack, (sp − low) / 2)`, `max_wasm_stack` default 512 KiB (Wasmtime's,
D6), `low` read per thread (Linux `pthread_getattr_np` + `pthread_attr_getstack`; macOS
`pthread_get_stackaddr_np` (the top) − `pthread_get_stacksize_np`, the main thread's size capped at
the soft `RLIMIT_STACK`, one page kept clear above the guard page; Windows
`GetCurrentThreadStackLimits`); if `low` cannot be read, or `sp − low` is not within (0, 1 GiB] (e.g.
an unlimited `RLIMIT_STACK` on the main thread), the budget is `min(max_wasm_stack, 128 KiB)`.
Measured sizes per OS: `tools/stack_info.py`, run in CI on Linux (glibc, musl), Windows and macOS. The wasm part never takes more than half of the stack below the
entry, so a host function called from the deepest frame has at least as much (a host that needs more
than its share can still overflow the C stack on CPython < 3.14, which has no C-stack check — as it
would on so small a thread without wasm; RISKS.md R5). `sp` comes from a stub run through the same
entry path, `endbr64; mov [r12], rsp; xor eax, eax; ret`: it writes rsp as a wasm function sees it
to frame[0] (a 32-bit status cannot carry it). The value stack is 1 MiB per store, mapped once,
limit in word 5; a nested entry starts above the host call's slots. Both overflows trap with
`STACK_OVERFLOW` (D10).

**Traps return through frames** (R2: CET shadow stacks; no `rsp` reset). A trap point jumps to a
cold per-function stub `mov eax, code; jmp .unwind`; `.unwind` is the function's epilogue (pops,
`ret`) with `eax` intact. After every wasm → wasm call: `test eax, eax; jnz .unwind`. The trampoline
returns the status to the runtime, which raises `Trap(TrapCode(status))` (S0002). Every `ret` matches
its `call`. Checks the hardware does not do the wasm way are explicit: `idiv` with divisor 0 →
`INTEGER_DIVISION_BY_ZERO`, `INT_MIN / -1` → `INTEGER_OVERFLOW` (`div_s`) or result 0 (`rem_s`),
because the CPU faults there and a fault would end the process (D10).

**Flags** (LESSONS 1). Between a flags producer and its consumer only `mov`/`lea` are emitted; the
backend interface exposes compare-and-branch / compare-and-set / select-on-compare as single
operations (S0002, proposed decision), so the shared core never places code between them.

**Zero-extension** (LESSONS 5). i32 values are kept zero-extended in 64-bit registers and slots:
32-bit operations and moves for i32; an i32 used as an address is already a valid 64-bit offset.

**Host calls and host exceptions.** The runtime makes one ctypes callback per import of type
`CFUNCTYPE(c_int32, c_void_p, c_void_p)`; its whole body is a `try` (a ctypes callback that
raises only prints the error and returns 0, i.e. "success"): read arguments from the slots, call
the `HostFunc`, write results masked to their width, then write the status to `ctx[HOST_ERROR]` as
the last action: 0; on a `Trap` with a wasm trap code that code; on any other `BaseException` (a
`Trap` with another code included) store it in `StoreState` and write 13. An asynchronous exception
that escapes the callback anyway (ctypes then prints it and leaves the C return value undefined)
leaves the bridge's preset 13 with nothing pending: the runtime raises a `RuntimeError` "host call
interrupted" (D34), never a success with unwritten results. The code unwinds as for a trap; the runtime re-raises the stored
exception at the outermost entry. The GIL is released by the entry (`CFUNCTYPE`) and taken back by
callbacks (ARCHITECTURE.md, "Long calls and the GIL").

**Libcalls.** An operation a backend does not implement calls a Python helper through the bridge,
with its own word after the imports; the helper set is shared by all architectures
(`jit/common/libcalls.py`); `memory.grow` is one (the runtime commits pages and rewrites the memory
words; the reload after every bridge call picks them up). Memory never moves under running code
(LESSONS 7): the runtime reserves the maximum up front. An i64 memory (S0009) may move inside a
`memory.grow`, never under running code: every frame is suspended at a bridge call site, which
reloads the words (D50).

**Interrupt polling** (D14, LESSONS 8; on by default, maintainer 2026-10-30 — timeouts are a runtime
timer setting this word; fuel is a separate opt-in, later). At every function entry and loop header:
`cmp qword [r15 + 16], 0; jne` → status 12. `StoreState.interrupt()` sets word 2 in every live
context of the store; the runtime clears it when it raises the `INTERRUPT` trap.

**Re-entrancy** (LESSONS 4). The trampoline keeps nothing in the context except `TRAP_CODE`, so
wasm → host → wasm nests: the inner entry saves and restores the callee-saved registers, and a trap
inside it returns to its own trampoline. The runtime starts a nested frame above the host call's
slots (`slots + 8·max(n, m)` of the import — everything above the arguments is free under frame
overlap) and checks it against the value-stack limit.

**Executable memory** (R2). W^X: map RW (`mmap`), write, flip to RX (`mprotect` via ctypes), never
both; the S0002 probe does exactly this with a `ret` stub and calls it once. `endbr64` (a hint NOP on
CPUs without CET) at every indirectly reached entry: trampolines, bridges, the stack-pointer stub.

**ISA level** (D24). Phase-3 code is x86-64-v1. A declared variant (`roundsd` for `f64.trunc`,
x86-64-v2) is emitted only when the configured level declares it; its baseline twin (SSE2 sequence)
is mandatory. Jump tables are placed in a data area after the code and recorded, so only code is
disassembled. Check: `objdump -D -b binary -m i386:x86-64` of every code range; every mnemonic must
be in the x86-64-v1 allowlist (`endbr64` allowed). Measured on `w2jit2`'s CoreMark code today:
`roundsd` (v2) and jump-table bytes decoded as `(bad)`/`outsb` both appear — the check fails it, as
it should.

**What a second architecture defines** (phase 4), in its own table like the one above: context,
frame, memory base/size, status, scratch and pool registers; registers no supported OS reserves
(AArch64: never `x18`; RISC-V: never `gp`/`tp`); entry and bridge per convention (`aapcs64`,
`win-arm64`; RISC-V psABI) with the callee-saved sets; landing pads (`bti c`) where `endbr64` is
used here; stack alignment (16 on both); compares without flags (RISC-V); division checks (AArch64
and RISC-V do not trap on 0: explicit checks give the wasm traps); shift-count masking; immediate
and offset ranges; its baseline (ARTIFACT.md part 1) and the disassembler allowlist; encoder tests
against `llvm-mc`.

## Test plan
In addition to S0002 steps 2–6, `tests/jit/` (each test runs on every JIT target the machine has):
```
uv run pytest tests/jit -k "entry or trap or reentry or host or interrupt or stack"
```
* entry: results of i32/i64 functions with 0–6 arguments; i32 results zero-extended;
* traps: each code in the phase-3 set returns its status and leaves the instance usable; a trap
  three calls deep returns through every frame (count frames by a host call before the trap);
* re-entry: wasm → host → wasm → trap: the inner `Trap` reaches the host function, which catches it
  and returns normally; the outer call completes; nesting 50 deep;
* host: a host function raising `ValueError` / `KeyboardInterrupt` reaches the caller as itself;
  pinned locals of every frame above survive a host call that clobbers `rsi rdi r8–r11`
  (a host written as machine code that zeroes them);
* interrupt: a `loop` without exit stops with `INTERRUPT` within 50 ms of `interrupt()` from another
  thread; stack: unbounded recursion → `STACK_OVERFLOW` on both stacks (small budgets), process alive;
* ISA: the objdump check on all spec modules and CoreMark; a planted `roundsd` fails it.

## Alternatives considered
* **Typed host signatures** (ARTIFACT.md part 3 as written): one ctypes type per signature, C-ABI
  zero-extension of results, a separate `HOST_ERROR` check after each call, no multi-value path; the
  bridge would have to marshal per signature. Rejected for phase 3, pending approval.
* **Unwind by resetting `rsp` to `SAVED_SP`** (the PoC): faults under CET shadow stacks (R2), needs a
  linked list for nesting. Rejected.
* **Saving only the caller's own pins around host calls**: wrong (LESSONS 2).
* **The runtime passing the native stack limit as an entry argument**: changes the entry signature
  for a value that changes only per outermost entry. Rejected.

## Open questions
* None at approval. Resolved: the ARTIFACT.md part 3 v1 amendments are accepted without a version bump
  (v1 is fixed in phase 5); ARTIFACT.md and DECISIONS.md are updated in the implementation step.
  The native budget is not a fixed 512 KiB everywhere: it is capped by the real stack size of the
  entering thread (macOS secondary threads have 512 KiB in total), and CI checks deep recursion on the
  main and a secondary thread on every OS runner where the JIT runs (Windows x86-64 in phase 3).

## Changes
* 2026-10-30: created (draft).
* 2026-10-30: approved by the maintainer (amendments accepted; budget capped by the thread's stack).
* 2026-10-30: linear memory reserves 4 GiB of address space per 32-bit memory and commits on grow (never moves; Wasmtime `memory_reservation` = 4 GiB, `memory_guard_size` = 32 MiB in crates/environ/src/tunables.rs — the guard comes in phase 5); explicit bounds checks in phase 3; libcalls are runtime services only (maintainer).
* 2026-10-30 (step 3a, after review): native limit rule above (first JIT entry, half of the stack
  below the entry, 128 KiB fallback); the rsp stub writes frame[0]; value stack 1 MiB per store.
  Interrupt: `interrupt()` (any thread, under the store's lock) sets word 2 in every context of the
  store; a nested entry returning INTERRUPT leaves it set so the frames below stop at their next poll;
  the outermost JIT entry returning INTERRUPT clears word 2 in every context under the same lock
  before raising (consuming every request made until then); a request made later, or while no code
  runs, stays set and stops the next call at its first poll. `TRAP_CODE` (word 1) is written on a
  non-zero status only. A host callback returns status 0 only after its results are written; an
  asynchronous exception arriving in the callback's last handler can still escape to ctypes (which
  then leaves the return value undefined; closed by the status word in step 6, D34).
* 2026-10-30: per-OS stack bounds rule with a sanity range and the 128 KiB fallback; `tools/stack_info.py` measures the sizes in CI (Linux glibc: main ≈ 8 MiB, threads 8 MiB, 2 MiB under an unlimited rlimit).
* 2026-10-30: stack sizes measured by `tools/stack_info.py` in CI run 41087438788 (CPython 3.13):
  Linux glibc (RLIMIT_STACK 16 MiB): main 16372 KiB, default thread 16384 KiB, budget 512 KiB.
  Linux musl (python:3.13-alpine): main reported 132 KiB (musl reports only the mapped part of the
  growing main stack) → budget 62 KiB; default thread 1026 KiB, budget 512 KiB. Windows Server 2025:
  main and default thread 2944 KiB, budget 512 KiB; host share at the deepest frame 2.4 MiB.
  macOS 26 x64 (RLIMIT_STACK 8 MiB soft): main reported 16384 KiB, capped to the 8 MiB rlimit;
  default thread 16384 KiB (CPython's own thread size), budget 512 KiB; a 512 KiB thread leaves the
  host 224 KiB, less than this CPython's `repr` of a 900-deep list needs (the test skips that case:
  the host alone crashes there without wasm). Open: the musl main thread could use RLIMIT_STACK.
* 2026-10-30: on the Linux main thread the stack grows on demand up to RLIMIT_STACK, but musl's
  `pthread_getattr_np` reports only the part mapped so far (132 KiB measured on python:3.13-alpine), so
  the JIT takes low = top of the `[stack]` mapping (`/proc/self/maps`) − min(RLIMIT_STACK soft, 1 GiB)
  + one page, as glibc does; an unlimited rlimit counts as unknown (128 KiB budget); the rule
  min(max_wasm_stack, (sp − low)/2) applies as for every thread.

* 2026-10-30 (step 3b, after review): `br_table` jump tables are 32-bit entries relative to the table,
  placed after the function's code, every target an `endbr64` landing pad. Slots and registers hold
  i32 values zero-extended; `i32.wrap_i64` and `i64.extend_i32_u` always emit a 32-bit `mov`, even onto
  the same register (the review found the skipped self-move left the upper half). The bounds check
  zero-extends the address again before use. A function of another JIT instance is imported through
  the host path (no direct cross-instance calls yet); an imported global is shared as a pointer.

* 2026-10-30 (step 3c): floats and `memory.size`/`memory.grow`. Floats stay bit patterns in the
  places integers use (slots, `rax`, constants, f32 zero-extended); each float operation is one
  backend call that loads its operands into `xmm0`–`xmm2`, computes, and moves the result back, so
  no xmm register is live across any call (none to save on SysV, `xmm6`–`xmm15` never touched for
  Win64); constants go through `rcx`/`rdx` (`mov` + `movq`), not a data area. SSE2 only (D24):
  abs/neg/copysign are integer bit operations (NaN bits exact); min/max split ordered (`minss`),
  equal (`orps`/`andps` for ±0) and unordered (`addss`, a quiet NaN) after `ucomiss`;
  trunc/floor/ceil/nearest: |x| ≥ 2^52 (2^23), inf and NaN return x + 0.0, otherwise trunc is
  `cvtt` + `cvtsi2`, nearest is (|x| + 2^52) − 2^52 under round-to-nearest-even, the sign of x is
  put back, floor/ceil step by 1; float → int traps are decided only when `cvtt` returns INT_MIN
  (or, unsigned, a value out of range); u64 ↔ float split at 2^63. Float compares produce 0/1 in
  `tos` (not fused into branches). The context gains a fixed libcall section after the imports:
  one word, libcall 0 = `memory.grow` (`context.MEMORY_GROW`), present in every context and set by
  the runtime when the instance has a memory; it is called through the host bridge with the delta
  in the slot that receives the result (old pages or −1), and the reload of `r13`/`r14` after every
  bridge call makes the new size visible to every frame. `memory.size` is `r14 >> 16`. The runtime
  service lives in `common/instance.py` (no `libcalls.py` module until a second service exists).
* 2026-10-30 (step 3d): tables, `funcref`/`externref`, `call_indirect`, bulk memory. A table element
  and a `funcref` value are one word: 0 or the address of a 3-word function record `{code, canonical
  type id, context}` in the context of the module that took the reference (Wasmtime's `VMFuncRef`
  shape, crates/cranelift/src/func_environ.rs); the variable part gains a signature word per type
  `call_indirect` names and the records, after the libcalls (ARTIFACT.md part 3; same version 1).
  Canonical type ids come from a process-wide registry of structural type keys, written into the
  context by the runtime, so the code holds none. `call_indirect`: index (zero-extended) `≥ size` →
  `TABLE_OUT_OF_BOUNDS`, element 0 → `INDIRECT_CALL_TO_NULL`, record id ≠ signature word →
  `BAD_SIGNATURE` (Wasmtime's codes and order), then `push r15` + pad (alignment holds), r15 and
  r13/r14 from the record's context, `call [record]`, r15 popped and r13/r14 reloaded. Records of
  another JIT instance's function point into that instance (a direct call); a host function's record
  points to a per-module thunk (`endbr64`; pad; bridge call of the import's word with r12 as the
  slots; `ret`). Since code may now enter a context without the runtime's `call`, every context of
  the store gets the stack limits: at creation, and whenever the outermost entry computes a
  different native limit (LESSONS 31). Native (computation): `table.get/set/size/fill/copy`,
  `memory.fill/copy` (bounds checked before any write; 8-byte then byte loops, backwards when the
  destination is above the source; plain moves, rsi saved as the temporary — no `rep movs`/`std`).
  Services (libcalls 1–5): `table.grow` (may reallocate the elements; rewrites `base`/`size` in every
  context bound to the table; 10 000 000-element cap as `pyc`, D27), `table.init`/`elem.drop`,
  `memory.init`/`data.drop` (segments live in the runtime). The services are methods of the
  instance (`common/instance.py`), not a `libcalls.py` module. References cross the host boundary
  as objects (S0002): an externref is a per-store handle of the object (kept for the store's
  lifetime), a funcref a `FuncRef(address)`; a host function whose signature has references gets
  them converted. Readiness: the xfail set still differs from `pyc`'s by tail calls, `call_ref`,
  typed function references and modules without a full validator verdict, so `ready = False` stays.
* 2026-10-30 (step 3d review): every libcall word of every context is callable (one the instance
  does not serve reports a `RuntimeError` through the bridge, never a call to address 0;
  `data.drop` is served without a memory). A `FuncRef` carries its store and is refused by another
  store at the boundary (`TypeError`), and table, function, memory and global imports from another
  store are refused at instantiation (`ValueError`), as Wasmtime refuses cross-store objects: a
  record of another store would run on that store's context and value stack. Externrefs are host
  objects and get a handle in whichever store receives them. The canonical type-id registry is
  updated under a lock. Backend rule: a method reads every operand that may be in `tos` before it
  writes `tos` (LESSONS 32; `table.set` lost its index).
* 2026-10-30 (step 3c review): float code assumes the default MXCSR (round to nearest, no FTZ/DAZ)
  and the entry trampoline does not set it, as Wasmtime does not; a host library that changes MXCSR
  changes `nearest` and subnormal results. Tests: float operands in r10/r11
  (`test_operands_in_high_registers`), no xmm6–15 in the float code (`test_isa_check_on_float_code`).
* 2026-10-30: bounds-check mode (D29, maintainer): phase 3 emits `explicit` only; phase 5 adds `guard`
  as the default where the fault handler is verified, recorded in the artifact. Phase-3 code keeps the
  out-of-bounds trap reachable from a future fault handler and `SAVED_SP` reserved.
* 2026-10-30 (step 3e): tail calls and typed function references; `ready = True` (the expected
  failures equal `pyc`'s; `tests/spec/test_runner.py` checks this while `ready` is set).
  `return_call`, `return_call_indirect` and `return_call_ref` run every trap check first (bounds,
  null, signature). Then they move the arguments from their slots down to the frame base (ascending,
  which is safe under overlap), drop the native frame as the epilogue does (pad, pins popped), and
  jump with rsp = 8 mod 16 and r12 unchanged. Both stacks stay constant; the callee's prologue checks
  both limits and polls INTERRUPT. A record of the same context is jumped to directly. One of another
  context goes through the `tail_switch` trampoline (context word 9, D30), which leaves one restore
  frame per chain, so the caller's direct `call` site finds its r15/r13/r14 again. `return_call` of
  an import is a call and a return (until step 3f, below). The host thunk reloads r13/r14 after
  the bridge: as a tail-call target it returns to a caller that does not reload them. `call_ref` is `call_indirect` without the
  table lookup or the type check; a null gives `NULL_REFERENCE`, as `ref.as_non_null` does (Wasmtime).
  `br_on_null`/`br_on_non_null` compare the reference's slot with 0. Typed references are the same
  word as `funcref`. Tags are read and validated (a type index naming a type without results); they
  have no run-time meaning while tag imports and exception instructions are unsupported on both
  targets.
* 2026-10-30 (step 3f, D32): every function import gets a function record (the records section
  counts the imports too; no new section, same version 1): the exporter's record for a function of
  another JIT instance of the same store, the module's host thunk for a host function. `call` of an
  import is `call_ref` on that record without the null check (r15 pushed with a pad, r15/r13/r14
  from the record's context, `call [record]`, r15 popped and r13/r14 reloaded); `return_call` of an
  import is `return_call_ref` on it (same context: `jmp [record]`, which for a host function is the
  thunk; another context: `tail_switch`). A direct call into another instance therefore runs
  without Python, and tail recursion through an import runs in constant stacks
  (`test_tail_recursion_through_a_function_import`: 10⁶ rounds on a 256 KiB thread, as Wasmtime).
  The import word keeps the host function's address; the bridge is reached through the thunk.
  For an import bound to a JIT function of the same store, the import word still holds a ctypes
  callback and the module a thunk for it; both are unreachable (the record points to the exporter's
  code) and kept: the thunk is compiled before the binding is known, and the layout stays the same.
* 2026-11-01 (step 6, after review): host status word, maintainer-approved (D34): word 7 holds the
  status of the current host call, preset to 13 by the bridge and written last by the callback; the
  bridge puts back the enclosing call's preset; the C return value is ignored. A host `Trap` whose
  code is not a wasm trap code is a host exception. Code areas are unmapped when their instance is
  collected; a `FuncRef` is accepted only if the store handed it out; the `ready` guard is gone.
  The two-stacks bound is stated exactly (96 bytes per frame, + 16 per restore frame).
* 2026-11-01: step 6 done (final review of steps 2–5, its fixes and D34 reviewed); status implemented.
* 2026-11-01 (multi-memory, no layout change, same version 1): a module may have several memories
  (imported first, then defined), each with its two words from word 16. Memory 0 keeps r13/r14 and
  its code is byte-identical to before (all 1 594 single-memory spec modules and CoreMark); an access
  to memory *i* > 0 compares its end with `[r15 + 8·(16+2i) + 8]` and addresses through rcx = the
  base word + the address (as Wasmtime's heaps load base and bound from the memory's
  `VMMemoryDefinition` through the vmctx, `make_heap` in crates/cranelift/src/func_environ.rs), so nothing is reloaded for
  it after a call: a host call or another instance that grows it is seen at the next access.
  `memory.size/fill/copy` take the memory (copy: destination and source may differ). Libcall
  immediates: when the module has **more than one** memory, `memory.grow` gets the memory index in
  the slot after the delta and `memory.init` after the segment index (a one-memory module writes
  neither, so its code is unchanged); the runtime reads them by the same rule. memory64 stays
  unsupported.
* 2026-11-01 (phase 4, step 4a, S0004): a second architecture, no layout change, same version 1.
  The words are architecture-neutral (word 6 is the lowest native `sp`, the stack pointer of any
  architecture); what differs per architecture (bridge registers, what the bridge preserves,
  `tail_switch`'s record and return-address registers, landing pads, `CODE` alignment, I-cache
  maintenance) is a table in ARTIFACT.md part 3. The aarch64 register roles and trampolines are
  in S0004; the x86-64 ones above are unchanged (code byte-identical).
* 2026-11-02 (S0005, 5c1): v128 slots are done: a v128 takes two slots (bytes 0-7 in the first) as a parameter, result or local and two context words as a defined global (an imported one keeps one word, the pointer); `Layout` takes the widths (`wide`). An amendment of version 1 without a bump, as D37 and D38 (no artifact is saved before phase 5).
* 2026-11-04 (S0009 step 2b, D50): an i64 memory moves when a grow exceeds its reservation; its
  `base` and `size_bytes` may change during any host call or libcall (ARTIFACT.md part 3, a text
  amendment of version 1 without a bump). The code obligation is unchanged: memory 0's registers are
  reloaded after every bridge call, every other memory is read at each access.
* 2026-11-05 (S0012 step 3, D52): exceptions on x86-64, an amendment of v1 without a bump. Words
  10 (`EXCEPTION`), 11 (`EXN_REFILL`), 12 and 13 (`ARENA_CURSOR`, `ARENA_LIMIT`: addresses, since the
  arena belongs to the store) and one identity word per tag after the records (ARTIFACT.md part 3).
  An exception is status 14 returned through frames. Every call site's `jnz` goes to the frame's
  `error` label: `unwind` outside a `try_table` (the code of a function without one is unchanged:
  D35 lists), the innermost landing pad inside one; trap stubs and the interrupt poll keep `unwind`.
  At a landing pad only eax (the status) and the pins are live: the operand stack is in slots, r12
  and r13/r14/r15 were restored before the status test, the callee's epilogue popped its pins. A
  pad compares eax with 14, then tries the clauses, each re-reading the pending word through word
  10 (rcx/rdx only). `throw` bumps the object from the arena in code; only a full chunk goes
  through the bridge (word 11). Status 14 at the trampoline becomes `Trap(UNCAUGHT_EXCEPTION)`; a
  host function's `Trap(UNCAUGHT_EXCEPTION)` is a host error (13).
* 2026-11-05 (S0012 step 4, D52): the same on aarch64, no change to the words. A call site's
  `cbnz w0` goes to `error`; at a landing pad only w0 (the status) and the pins are live, x27 and
  x28/x26/x25 restored before the test; a pad does `cmp w0, #14; b.ne`, and the clauses re-read
  word 10 into x9-x10. `throw` uses x9-x12 (x16/x17 for offsets no immediate takes).
* 2026-11-11 (S0013 step 3a.2, an amendment of v1 without a bump, S0013 Q5): context word 14
  `EXIT_RECORD`; the bridge writes the store's exit record `{sp, pc, frame}` before the host and
  clears its `sp` after; the entry keeps the enclosing record and its frame on the native stack
  during its call and puts the record back (x86-64: the pad 8 → 40 bytes, the block at `[rsp, rsp + 32)` in SysV and Win64). Module code is unchanged.
