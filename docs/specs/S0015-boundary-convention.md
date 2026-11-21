---
title: The boundary convention between separately compiled JIT artifacts
kind: spec
id: S0015
status: draft
superseded_by:
related: [D23, D30, D32, D35, D36, D56, D59, D60, D62, D64, D65, D66, D68, D70, R2, S0003, S0004, S0006, S0013, S0014]
owner: maintainer
updated: 2026-11-21
---

# S0015 The boundary convention between separately compiled JIT artifacts

## Summary
D70 replaces D64's "one compiler per store" with "one ABI per store". Within one architecture,
artifacts from different compilers (`jit-x86_64` and the sljit micro-compiler of D60 and D68) and
from different compiler versions load and link in one store, the way `.so` files link. This spec
fixes what they share, one table per architecture:
* **the boundary convention:** a function record's `code` is a C entry
  `int32_t f(uint64_t *ctx, uint64_t *frame)` in SysV on x86-64 on every OS and AAPCS64 on
  aarch64;
* the runtime's `entry`, one per architecture and independent of the compiler;
* the walk interface, the status return, the guard handler's contract and tail calls across the
  boundary in constant stack.

Everything else stays private to each compiler: pinned registers, the pool, the native frame,
calls inside a module, `bridge` and `tail_switch` (carried in its artifacts). Context ABI and
artifact format stay version 1, amended before their freeze ("Versions").

## Goals / Non-goals
* Goals:
  * per architecture, an artifact of any compiler and any compiler version that follows this spec
    calls, and is called by, the artifacts of another in one store, directly through function
    records, with no host in between;
  * the GC walk and collector cross frames of several compilers;
  * traps, exceptions and interrupts cross the boundary as today;
  * a chain of cross-boundary tail calls runs in constant native stack;
  * both Python backends follow the convention.
* Non-goals:
  * compatibility between architectures: an x86-64 artifact never runs on aarch64 (D35:
    cross-compiled files are not run on the compiling host);
  * a stable internal convention: calls inside a module, the registers a compiler pins and its
    frame layout stay free and may change in any release;
  * the public API (phase 6), the C reference loader itself (D36, its own spec), riscv64 (it gets
    its row when its backend exists, D48).
* Deferred:
  * thin files of **other compiler versions**: accepted from phase 6 (S0014 Q3 (b)). The format
    and the ABI here allow it; it is loader policy only;
  * a real store that mixes `jit-x86_64` and sljit code: when the micro-compiler exists (D68).
    Until then the test plan simulates it ("Test plan").
* Rows (D47): no per-OS code beyond what exists, plus the Win64 entry's xmm saves ("The runtime's
  `entry`"). The boundary is one convention per architecture on every OS (D23).

## Interface

### The boundary convention, per architecture
A C entry is a function in this convention, whatever the host OS. On x86-64 it is SysV on Windows
too: only waboros code (C entries, the runtime's `entry`, a compiler's `tail_switch`) calls a C
entry, never a host C compiler. Host functions keep the host's convention behind each compiler's
bridge.

| | x86-64 | aarch64 |
|---|---|---|
| convention | System V AMD64, on Linux, macOS, Windows and Android alike | AAPCS64 (Linux, Android; Apple arm64 and Windows Arm64 agree on everything used here) |
| arguments | `rdi` = ctx, `rsi` = frame | `x0` = ctx, `x1` = frame |
| result | status in `eax`; the upper half of `rax` is undefined, callers test 32 bits | status in `w0`; the upper half of `x0` is undefined |
| callee-saved | `rbx rbp r12 r13 r14 r15`, `rsp`; no xmm register (SysV) | `x19`–`x28`, `x29`, `sp`, `d8`–`d15` (low halves of `v8`–`v15`) |
| caller-saved (a C entry may clobber) | `rax rcx rdx rsi rdi r8`–`r11`, every xmm, the flags | `x0`–`x17`, `x30`, `v0`–`v7`, `v16`–`v31` and the high halves of `v8`–`v15`, the flags |
| never touched | — | `x18` (the platform register: Apple, Windows TEB; D23) |
| stack at entry | `rsp` ≡ 8 (mod 16) (16-aligned at the `call`); no red zone below `rsp` is relied on across a call | `sp` 16-aligned at all times |
| landing pad | `endbr64` first (CET IBT, R2) | `bti c` first (BTI; reached by `blr` from any register, or by `br x16` / `br x17`) |
| reached by | `call`, or `jmp` with the stack as at a `call` (a sibling call, "Tail calls") | `blr`, or `br x16`/`br x17` with `x30` as at a `blr` |
| return | `ret` to its own `call` (CET shadow stack) | `ret` to `x30` (Arm GCS) |
| native frames | below one page each (Windows stack probes, as S0003) | the same |

A C entry may use the native stack down to the store's native limit. It checks
`NSTACK_LIMIT` and `VSTACK_LIMIT` itself, in its body's prologue, as every function does today.

### The C entry and the function record
```c
int32_t f(uint64_t *ctx, uint64_t *frame);
```
* `ctx` is the callee's context (the record's third word); `frame` is the callee's frame base on
  the store's value stack. The arguments are in `frame[0..n)` and the results come back in
  `frame[0..m)`, one 64-bit slot per value, as for the entry (ARTIFACT.md part 3).
* The return value is `0` or a status code of part 3: a trap, `INTERRUPT`, `HOST_ERROR` or `14`
  (an exception in flight, the pending word set). A caller that gets a non-zero status passes it
  on, or dispatches 14 at a landing pad, exactly as after an internal call.
* How a compiler builds the C entry is its own business. The prototype uses a 16-byte stub before
  each body (`endbr64; lea rdx, [rip + 5]; jmp centry`) and one `centry` per module, which saves
  r12–r15, sets up its pinned registers and calls the body ("Measurements").
* **Function records** keep their three words `{code, canonical type id, context}`; `code` is now
  the callee's C entry:
  * a defined function: its C entry;
  * an import from another instance of the store: the exporter's record (its C entry and its
    context: a direct call, whichever compiler made it);
  * a host import: the C entry of the importing module's host thunk, on the importer's context;
  * the `IS_SUBTYPE` record (S0013 step 5.6): `{the collector's is_subtype C entry, 0, the
    collector's context}`.
* **Every call through a record** (`call` and `return_call` of an import, `call_indirect`,
  `return_call_indirect`, `call_ref`, `return_call_ref`, a function-subtype site) uses the C
  entry. The caller first saves whatever of its own convention the C entry may clobber. In
  practice that is its pool registers in the caller-saved row, for every frame above, as the
  bridge does today (LESSONS 2): x86-64 `rsi rdi r8`–`r11`; aarch64 `x2`–`x8`. It reloads after
  the call what a callee may have changed, as today (memory 0's registers: a callee may grow an
  imported memory).
* Calls inside a module (`call` of a defined function) keep the compiler's internal convention.
* Every defined function and every host thunk has a C entry (Q2). The runtime's `entry` calls
  only C entries.

### The runtime's `entry`
```c
int32_t entry(uint64_t *ctx, uint64_t *frame, const void *c_entry);
```
One per architecture and host convention (x86-64: SysV and Win64; aarch64: one), the same bytes
whatever compiler made the callee. In the host's convention it:
1. saves the host's callee-saved registers that the boundary convention does not keep. Win64
   adds `rsi`, `rdi` and `xmm6`–`xmm15` (160 bytes), because SysV code may clobber them. The
   Python backends never touch xmm6–15 (S0003), but sljit code may;
2. keeps the store's enclosing exit record and the frame at a fixed place on the native stack.
   This is the **saved block**, at its call: x86-64 `[rsp, rsp + 32)`, aarch64 `[sp + 96, sp + 128)`
   (`{sp, pc, frame}`, then the frame), as today;
3. calls `c_entry(ctx, frame)` in the boundary convention;
4. puts the exit record back and writes a non-zero status to `ctx[TRAP_CODE]`, as today.

It sets up no pinned register: each compiler's C entry does that. The stack-pointer stub
(`sp_stub`) becomes a C entry of the runtime's blob as well. The return point of the `call` in
step 3 is the walk's **entry point** for its blob. Where the bytes live for a foreign host is Q1.

### `bridge` and `tail_switch`: private to each compiler
* A compiler's bridge (wasm → host) and its `tail_switch` (the first cross-context hop of a tail
  call, below) follow its own internal convention: they save its pool, read its context register
  and know its frame register. They are **not** part of this interface.
* They travel in the compiler's artifacts (`.waboros.trampolines`, "What changes in the artifact
  format"). Each instance's context words 8 `HOST_BRIDGE` and 9 `TAIL_SWITCH` point to the copy of
  its own compiler. Both words already exist per context; no word moves.
* **What every bridge must do for the walk:** write the store's exit record (word 14) before the
  host runs and clear its `sp` after, with the meaning fixed here:
  * `sp`: the calling frame's stack pointer at its call (x86-64: before the `call` pushed);
  * `pc`: the return address;
  * `frame`: the frame base of the calling wasm frame, the base its site's map refers to.
* The rest of the host-function contract of part 3 is unchanged: the preset of `HOST_ERROR`, the
  status in the word, the nested entry at `slots + max(n, m)`.

### The walk interface
The GC's walk (D56, D65) and its check mode must cross frames of any compiler without knowing
their layout. The interface is:
1. **Every return address** on a store's native stack between an entry and the innermost exit
   record is either:
   * a **site** of some artifact's `STACKMAPS` (its `ret`, with `shift`, `ret_at`, `up` and the
     map), or
   * a **fixed point**: the return point of the runtime's `entry` (with its saved block), or the
     `restore` point of some compiler's `tail_switch` (with that compiler's restore-frame numbers
     from `.waboros.trampolines`).
2. **A C entry's internal call is a site** of its own artifact: shift 0, no map (`nslots` 0). The
   prototype's `centry` call is one site per module, with `ret_at` 40 and `up` 48 on x86-64.
3. **`frame` = the caller's base + 8 · shift**: a caller passes, as a C entry's `frame`, its own
   frame base plus 8 times the shift its site record states. That is how the walk finds the
   callee's base today.
4. **Invariant I1 in every compiler:** at a site, no reference of the any, extern or exn hierarchy
   is in a register. Every live reference is in a value-stack slot of a frame at a slot the map
   marks (`refs`, `live`, D65). The collector forwards and nulls only slots.
5. **Foreign frames are opaque:** the walk reads a frame only through its site numbers and its
   map. No frame-pointer chain, no unwind data, no compiler identity per frame.
6. The walk knows the **set** of entry points (one per runtime blob, normally one per process
   and convention) and the set of restore points (one per compiler present in the store, each
   with its numbers), instead of one of each.
The GC heap, its object layout, the type-info and RTT records and the collector's root buffer are
part 3's and shared already: every compiler allocates and casts against the same store heap.
Runtime modules (the collector, D61) are compiled by the runtime's own compiler for the
architecture; they are artifacts like any other.

### Traps, interrupts and the guard handler
* **Traps are status returns** through every frame and every C entry, as today. No signal,
  `longjmp` or unwinder crosses a C entry. An exception is status 14 with the store's pending word
  (word 10).
* **Interrupts, fuel, the epoch and the stack limits** are context words that the host writes in
  every context of the store (words 2–6); every compiler polls them in its own code. A store's
  artifacts must agree on the semantics flags (fuel, epoch: Q4).
* **The guard fault handler** is per process (S0006). It owns a registry of code ranges, each
  with its artifact's fault map (`.waboros.traps`, `{fault_offset, resume_offset}`), and
  reservation ranges. Its contract holds for any compiler:
  * a fault point is exactly one instruction;
  * the resume point is in the same artifact;
  * the handler changes only the PC.
  An artifact with `BOUNDS_GUARD` may import a memory only if that memory has a guard of at least
  `2^GUARD_LOG2` (every i32 memory reserves 4 GiB + 32 MiB, so this holds today).

### Tail calls across the boundary
A `return_call*` whose record names another context cannot jump to the callee's body: the
callee's C entry is in another convention, and the caller's caller expects the caller's
convention back. Constant stack is still required (D30; Wasmtime runs 10⁶ hops):
1. **Same context:** the compiler's own path. Its internal body is known to it; the prototype
   uses `code + 16`.
2. **First hop:** the caller tears down its frame and jumps to its compiler's `tail_switch`
   (context word 9). That trampoline saves what the caller's caller expects of the caller's
   convention (the prototype: `rsi rdi r8`–`r11` and a pad, 64 bytes on x86-64), calls the C entry
   with `ctx` = the record's context and `frame` = the frame base, restores and returns the status.
   Its frame is the **restore frame**; the numbers are in `.waboros.trampolines`.
3. **Later hops:** a body whose return address is the return point of its own module's C entry
   was entered through the boundary, so nothing of its compiler's convention is owed below it. It
   returns to its C entry (`ret`, balanced for CET and GCS) with a status private to the compiler
   that means "hop" and the record in a register of its choice. The C entry then restores the
   boundary's callee-saved registers and **jumps** to the foreign C entry with `ctx` and `frame`
   set: a C sibling call. The stack below is the original C caller's, so A → B → A → … across
   compilers holds one restore frame and one C-entry frame at most.
4. **Every compiler must do 2 and 3.** One that degrades a cross-boundary tail call to a call
   grows the stack and fails the conformance test ("Test plan").
The private "hop" status never leaves the module: the C entry consumes it, and the caller of a C
entry sees only part 3's statuses.

**Correction to the prototype:** its later hop unwound the C-entry frame with `add rsp` and jumped
from the body, discarding a return address that the CET shadow stack still holds, so it is not
shadow-stack safe. The return-then-jump form above is.

## What changes in the artifact format (S0014)
* **`.waboros.funcs`:** each defined function's code offset is its **C entry**, the one the
  runtime's `entry` calls. The layout is unchanged; the meaning changes. Where the body is, is the
  compiler's business.
* **`.waboros.context`:** each `thunks` offset is the host thunk's C entry.
* **`.waboros.trampolines`:**
  * per convention, a compiler blob `{id, start, size, bridge, tail_switch, restore,
    restore_ret_at, restore_up}`. The two numbers are bytes from the stack pointer at
    `tail_switch`'s call to the real return address, and to the caller's stack pointer: today
    fixed at 8 and 16, the prototype 56 and 64;
  * then the runtime blob of the architecture: `{id, start, size, entry, sp_stub, entry_ret,
    saved}`, with bytes fixed by this spec per architecture and convention (Q1 (a));
  * a compiler blob is compared only with the store's copy for **the same compiler string**; the
    runtime blob with the loader's own.
  * This changes the section's layout, so it is part of the v1 amendment ("Versions").
* **The loader's compatibility rule** (S0014 "Semantics: the loader", step 5):
  * an artifact is compatible when `e_machine` is the running architecture and `format_version` and
    `ctx_abi_version` are ones the loader reads; the ISA level and features, the flags and
    `features_required` are checked as today;
  * the `compiler` string stays the **cache key**, not the compatibility rule;
  * until phase 6 the waboros runtime still refuses a thin file whose compiler **version** differs
    from one it knows (S0014 Q3 (a) as policy), but it no longer refuses a file **because its
    compiler target differs from the store's**: D64's refusal is removed.
* **One copy per store** becomes **one copy per compiler per store**: the store keeps the runtime
  blob once, and each compiler blob once per compiler string. Its instances' words 8 and 9 point
  into that copy.
* **The walk** takes its entry points from the runtime blob and its restore points, with their
  numbers, from each compiler blob in the store.
* The inventory table (S0014 "What instantiation reads") gains the runtime blob and the
  per-compiler restore numbers. `Trampolines` splits into the runtime's part and the compiler's.

## What changes in ARTIFACT.md part 3
* A new **"Boundary convention"** table: the one under "Interface" above.
* "Entry": `entry(ctx, frame, c_entry)` is the runtime's, calls a C entry and sets up no
  register.
* "Function records": `code` is a C entry, with the cases above.
* "Tail calls": the three cases above, in place of the record-in-`rdx`/`x16` jump.
* The **"Per architecture"** table splits:
  * **fixed per architecture:** the boundary convention, the exit record's meaning, the entry's
    saved block, landing pads, `CODE` alignment, the instruction cache;
  * **compiler-private rows**, for `jit-x86_64` and `jit-aarch64` as information: bridge call,
    bridge preserves, `tail_switch` registers, the exit record's frame register. A host never
    provides a compiler's bridge or `tail_switch`; they come from the artifact.
* The D64 sentences ("The table is per compiler…", "a host providing the bridge or `tail_switch`
  follows the table of its Store's compiler") are removed. The part 2 loader text loses "or whose
  `compiler` target is not the Store's compiler (D64)" and its fat-file counterpart.

## Versions
v1 is amended, not bumped (D70, D66). A record's `code` and `.waboros.funcs` change meaning, and
`.waboros.trampolines` changes layout, so files written before this spec cannot run under it. The
only such files are the committed test files of S0014 step 4.3; they are regenerated once (step
3), and `tests/artifact/test_old.py` keeps testing the regenerated ones. The reason is recorded
in the test data's README: v1 was not frozen, and no file had been released.

## Measurements
[BENCHMARKS.md](../BENCHMARKS.md), "C-ABI boundary between instances (prototype, 2026-11-21)". The
prototype sends **every** call through a record down the C path, on `jit-x86_64` alone.
* Conditions: Intel Xeon @ 2.10 GHz, 4 vCPU, CPython 3.13.17, guard mode, `taskset -c 2`, variants
  alternated round by round, two sessions; `basepad` is the placement control (16 bytes of
  `int3` where the stubs go).

| case | base | C path | difference |
|---|---|---|---|
| `call` of another instance's function | 2.60–2.67 ns | 4.37–4.42 ns | +1.7 ns |
| `call_indirect` (own table) | 3.79 ns | 4.51–4.57 ns | +0.7 ns |
| CoreMark | 16 443–17 216 it/s (spread 15 620–18 437) | 17 353–18 095 it/s | within the spread; `basepad` moves as much |
| CoreMark code | 34 547 bytes | 35 057 bytes | +1.5 % |
| D64's path: a host function into another store | 13.5 µs | — | about 5 000 × a direct call |

* Correctness of the prototype:
  * the spec suite with the walk check: 64 229 pass, 0 fail, in guard and in explicit mode;
  * `gc` and `exceptions` with the three GC switches: 905 pass, 0 fail;
  * `tests/jit/test_tail.py`, cross-instance tail hops in a 256 KiB thread: passes.
* Not measured: aarch64; the Win64 entry's xmm saves; the hop through the return-then-jump form
  (the prototype jumped from the body).

## Test plan
* **The spec suite** with `WABOROS_JIT_WALK_CHECK=1`, in guard and explicit mode, on `jit-x86_64`
  and on `jit-aarch64` under QEMU (`tools/run_arm64.py`). Expected: 64 229 pass, 0 fail on each,
  `tools/spec_diff.py` empty both ways against before.
* **`gc` and `exceptions`** with `WABOROS_JIT_GC_STRESS=1 WABOROS_JIT_GC_POISON=1
  WABOROS_JIT_GC_VERIFY=1` and the walk check, both modes, both JITs: 905 pass, 0 fail.
* **Conformance tests**, new, in `tests/jit/test_boundary.py`, on the native JIT and under QEMU:
  * **tail-call ping-pong in constant stack:** A `return_call`s an import of B, which
    `return_call_indirect`s back. 10⁶ round trips in a 256 KiB thread, with three entry shapes: A
    entered from the host, from a direct call in A, and through a C entry. Each must finish; a
    compiler that degrades to a call ends in `STACK_OVERFLOW`;
  * **status through a C entry:** every status of part 3 (a trap, `INTERRUPT`, a host error,
    exception 14 caught one instance up, uncaught to the host) crosses one and two C entries with
    the right code and with the pending exception intact;
  * **a walk across modules:** host → A → (record) B → (`call_indirect`) A → host import, under
    the walk check, with a reference live in each frame. The frames and bases found match the
    runtime's own record, and a collection forwards every frame's references;
  * **the boundary registers:** a test C entry assembled by hand (not by either backend) that
    clobbers every caller-saved register of the table, xmm and v registers included, is called
    through a record: the caller's pins survive. The same through the runtime's Win64 entry for
    xmm6–xmm15 (on Windows CI);
  * **mixed compilers, simulated:** two modules compiled separately and written as thin files
    with different compiler strings (the second by a test-only target name) are instantiated in
    one store from their files and call each other in all directions. Both are still made by the
    same backend; a real mixed store waits for the micro-compiler.
* **Shape tests to update** (all fail on the prototype on shape only: one more site per module,
  one more walk frame per cross call, code offsets moved by the C entries):
  * `tests/jit/test_exit_record.py`: `test_a_libcall_leaves_the_record_of_its_site`,
    `test_exit_record_names_the_caller_and_survives_nested_entries`,
    `test_the_walk_steps_over_a_tail_switch_restore_frame`;
  * `tests/jit/test_liveness.py`: `test_exact_live_sets` (30 cases), `test_t13_nolive`;
  * `tests/jit/test_stackmaps.py`: `test_a_function_cast_at_the_top_of_the_frame_keeps_its_slots_inside`,
    `test_a_module_without_references_has_empty_maps`, `test_a_non_final_call_indirect_has_two_sites`,
    `test_a_service_keeps_its_reference_argument`,
    `test_a_struct_field_operand_is_marked_at_the_allocation_service`,
    `test_a_v128_between_two_references_takes_two_slots`, `test_funcref_is_never_marked`,
    `test_locals_and_an_operand_below_a_call`, `test_native_numbers_of_a_frame_with_pins` (2
    cases), `test_record_call_and_host_thunk`, `test_reference_locals_are_never_pinned`,
    `test_struct_anyref_and_i31_locals_are_marked`, `test_throw_allocation_is_a_site_over_its_payload`;
  * `tests/jit/test_walk.py`: `test_a_chain_of_direct_calls`,
    `test_frames_of_another_instance_indirect_and_ref_calls`, `test_nested_entries_50_deep`,
    `test_tail_calls_and_a_tail_switch_restore_frame`,
    `test_try_table_frames_libcalls_and_throw_allocations`;
  * expected to change with the trampolines (not run against the prototype, which kept them):
    `tests/jit/test_abi.py`, `tests/jit/test_abi_aarch64.py`, `tests/jit/test_tail.py` (the restore
    frame), `tests/artifact/test_trampolines.py`, `tests/artifact/test_strict.py`,
    `tests/artifact/test_roundtrip.py`.
* **D35:** code bytes change, so the lists of both JITs are regenerated (`tools/jit_dump.py`).
  The cross-compiled identity holds: the aarch64 list from an x86-64 host equals the arm64
  runner's.
* **ISA gate:** `tools/isa_all.py` on both JITs (the stubs and C entries are baseline code).
* **CoreMark**, alternated with the code before, `taskset -c 2`, several rounds, with a placement
  control (the code before plus padding where the C entries go; LESSONS 139). The claim is
  "within the spread" unless a difference survives the control.
* **The calls benchmark** and the cross-instance driver of the BENCHMARKS section, re-run after
  step 1 with the final code, the numbers recorded there.
* **The committed v1 files** regenerated once (`tools/make_v1_artifacts.py`), the data README
  stating why; `tests/artifact/test_old.py` kept and passing on both architectures.

## Plan of steps
The boundary is a contract: the reviewer reads steps 1–3 (step 5). Each step stops and reports
if it needs more than its size.

| step | goal | agent | acceptance | size (lines) |
|---|---|---|---|---|
| 1 | x86-64: C entries for every function and host thunk; record calls through them, saving rsi/rdi/r8–r11; records written with C entries; the runtime's SysV and Win64 `entry` (Win64 saves xmm6–15) and `sp_stub` calling C entries; `tail_switch` as the first hop, later hops by return-then-jump; the walk with sets of entry and restore points; the conformance tests; the x86-64 shape tests updated | jit-engineer | the spec suite and GC runs of the Test plan on `jit-x86_64`; `test_boundary.py`; `pytest -q` (the x86-64 v1 files skipped in `test_old` with a pointer to step 3); D35 list regenerated; ISA gate; CoreMark and the cross-call driver alternated with a placement control, recorded | src ~200, tests ~300 |
| 2 | aarch64: the same (save x2–x8 around record calls; `bti c` C entries; sibling hops by `br x16`); aarch64 shape tests | jit-engineer | the same on `jit-aarch64` under QEMU; the cross-compiled D35 identity | src ~180, tests ~120 |
| 3 | Format and loader: `.waboros.funcs`/`thunks` as C entries, `.waboros.trampolines` (compiler blobs with restore numbers, the runtime blob), one copy per compiler per store, D64's refusal removed, the simulated mixed-compiler test; the v1 files regenerated once | implementer | `tests/artifact/` (with `test_old` on the regenerated files, both architectures); the spec suite through files (`WABOROS_JIT_ARTIFACT=1`) at parity on both JITs | src ~120, tests ~150 |
| 4 | Docs ("Docs this changes"), and S0014's text | orchestrator | `python3 tools/check_docs.py` | — |
| 5 | Review of steps 1–3 without the implementers' reasoning: the boundary table against the code (every register a C entry may clobber is saved by every caller), CET/GCS balance of the hop, the walk's sets, the loader rule | reviewer | findings closed | — |

About 500 lines of source and 570 of tests in total.

## Alternatives considered
* **Keep D64** (option 1): different compilers in different stores, talking through host
  functions. A call through the host into another store costs about 13.5 µs, about 5 000 times a
  direct cross-instance call (2.6 ns), and memories, tables and GC references cannot be shared.
  Rejected by D70.
* **One internal register table per architecture** (option 2): every compiler uses the same
  context, frame and memory registers and the same set of registers that survive a call.
  * sljit's `SLJIT_ENTER_KEEP(n)` keeps only S0..S(n-1) with n ≤ 3 (rbx, r15, r14 on x86-64
    SysV; x19–x21 on aarch64; LESSONS 140), so our r12, r13 and x25–x28 would need a patched
    sljit register map.
  * sljit code clobbers rsi, rdi and r9 (x2–x7 on aarch64), which our pool keeps across calls; one
    compiler would lose registers.
  * Moving the Python JIT instead touches about 170 uses of the role constants and every code byte.
  * Above all it freezes both compilers' internals across versions.
  * Rejected by D70.
* **A fast path for records of one's own context** (the prototype's `cabi2`): a `call_indirect`
  into one's own table skips the C entry. Measured 3.45–3.54 ns against 4.51–4.57 on the C path,
  but the control `basepad` gives 3.00, so the gain is within the placement noise, while the
  cross-instance call slows to 5.04–5.07 ns. It also needs two call instructions per site with
  one map. Not done (D70); a compiler may add it later, privately, with a measurement.
* **`ctx_abi_version` 2** instead of amending v1: old files refused by version. Nothing released
  depends on v1 and the only old files are test data, so a bump would only add a refused version
  to every loader. Rejected (D70, "Versions").
* **Restore frames as sites** instead of numbers per compiler: `tail_switch`'s call recorded in a
  site table of the trampoline blob, so the walk has no special case. Cleaner, but it adds a site
  table to the trampolines for one call; kept as Q3 (b).

## Open questions
1. **Where a foreign host (D36) gets the runtime's `entry`.**
   * (a) Every artifact carries the runtime blob for its architecture, with bytes fixed by this
     spec and compared by loaders, as S0014 Q2 (b) does today.
   * (b) Foreign hosts assemble it from this spec's description.
   * (c) A separate per-architecture runtime file.
   * Recommendation: **(a)**. It keeps the foreign host's rule ("the first artifact provides the
     store's copy") and adds about 150 bytes per convention per file.
2. **Which functions get a C entry.**
   * (a) Every defined function and every host thunk (x86-64: 16 bytes each, CoreMark +1.5 %
     code).
   * (b) Only those with a record, the exports and the start function.
   * Recommendation: **(a)**. The runtime's `entry` and the tests call functions by index, and
     the compiler does not need to know which functions the host will call.
3. **How the walk steps over a restore frame.**
   * (a) Per-compiler numbers in `.waboros.trampolines` (above).
   * (b) `tail_switch`'s call as a site of the trampoline blob.
   * Recommendation: **(a)** now, the smallest change; (b) if a third kind of trampoline frame
     appears.
4. **Flags that must agree within a store.**
   * (a) The semantics flags (fuel, epoch) must equal the store's configuration; the bounds mode
     and `GUARD_LOG2` may differ per artifact, under the guard rule for imported memories.
   * (b) All code-affecting flags equal.
   * Recommendation: **(a)**. Bounds checks are local to the code; fuel and epochs are store-wide
     semantics.

## Docs this changes
When approved and implemented:
* [ARTIFACT.md](../ARTIFACT.md):
  * part 2: the `ENTRIES` row, the loader paragraph (D64 clause), fat-file text;
  * part 3: "Entry", "Function records", "Tail calls", the boundary table, the split "Per
    architecture" table.
* [S0014](S0014-artifact-format.md): `.waboros.funcs`, `.waboros.context` (`thunks`),
  `.waboros.trampolines`, "What instantiation reads", "Semantics: the loader" (steps 5 and 6), the
  test plan; a Changes line.
* [S0003](S0003-jit-context-abi.md) and [S0004](S0004-jit-aarch64.md): Changes lines (records hold
  C entries; the entry is the runtime's).
* [SLJIT-BACKEND.md](../SLJIT-BACKEND.md): the sljit compiler's C entries (`sljit_emit_enter` with
  the C convention) and hops.
* [MICRO-COMPILERS.md](../MICRO-COMPILERS.md): D64's "one compiler per store" wording, where it
  appears.
* [TESTING.md](../TESTING.md): `tests/jit/test_boundary.py`.
* [BENCHMARKS.md](../BENCHMARKS.md): the step-1 measurements.
* [LESSONS.md](../LESSONS.md): whatever the steps find.
* `tests/artifact/data/README.md`: the regeneration and its reason.

## Progress
* 2026-11-21: draft; awaiting the maintainer's approval.

## Changes
* 2026-11-21: created (draft), from D70 and the prototype measured in BENCHMARKS.md.
