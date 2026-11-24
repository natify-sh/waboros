---
title: The boundary convention between separately compiled JIT artifacts
kind: spec
id: S0015
status: implemented
superseded_by:
related: [D23, D30, D32, D35, D36, D47, D56, D59, D60, D62, D64, D65, D66, D68, D70, D71, D72, R2, S0003, S0004, S0006, S0013, S0014]
owner: maintainer
updated: 2026-11-24
---

# S0015 The boundary convention between separately compiled JIT artifacts

## Summary
D70 replaces D64's "one compiler per store" with "one ABI per store". Within one architecture,
artifacts from different compilers (`jit-x86_64` and the sljit micro-compiler of D60 and D68) and
from different compiler versions are meant to load and link in one store, the way `.so` files
link. This spec fixes what they share, one table per architecture:
* **the boundary convention:** a function record's `code` is a C entry
  `int32_t f(uint64_t *ctx, uint64_t *frame)` in SysV on x86-64 on every OS and AAPCS64 on
  aarch64, together with the machine state at the boundary;
* the runtime's `entry`, specified by contract (with a reference listing), not carried as bytes
  in artifacts;
* the walk interface, the status return with a reserved private range, the guard handler's
  contract, and tail calls across the boundary in constant stack with balanced calls and returns.

Everything else stays private to each compiler: pinned registers, the pool, the native frame,
calls inside a module, `bridge` and `tail_switch` (carried in its artifacts). The waboros loader
keeps its exact compiler string until phase 6. The per-architecture compatibility rule is
specified here and enabled in phase 6, together with mixed stores ("Compatibility"). Context ABI
and artifact format stay version 1, amended before their freeze ("Versions").

## Goals / Non-goals
* Goals:
  * per architecture, an artifact of any compiler and any compiler version that follows this spec
    can call, and be called by, the artifacts of another in one store, directly through function
    records with no host in between; now: the interface and both Python backends on it;
  * the GC walk and collector cross frames of several compilers;
  * traps, exceptions and interrupts cross the boundary as today;
  * a chain of cross-boundary tail calls runs in constant native stack, with every return matching
    its call.
* Non-goals:
  * compatibility between architectures: an x86-64 artifact never runs on aarch64 (D35:
    cross-compiled files are not run on the compiling host);
  * a stable internal convention: calls inside a module, the registers a compiler pins and its
    frame layout stay free and may change in any release;
  * the public API (phase 6), the C reference loader itself (D36, its own spec), riscv64 (it gets
    its row when its backend exists, D48).
* Deferred to phase 6:
  * the per-architecture compatibility rule in the waboros loader: thin files of another compiler
    or another compiler version, and executing a foreign compiler's blob ("Compatibility");
  * *(Done in-process by S0016: mixed stores of several JIT targets of one architecture, and the
    mixed-compiler test, `tests/jit/test_mixed.py`.)*
* Deferred until the micro-compiler exists (D68): a real store that mixes `jit-x86_64` and sljit
  code. *(Done by S0016 with `jit-sljit`, our Python core over sljit; the D68 micro-compiler stays
  later.)* Until then, a scratch experiment showed the mechanics ("Measurements").
* Rows (D47): no per-OS code beyond what exists, except the Win64 `entry`'s saves of `rsi`, `rdi`
  and `xmm6`–`xmm15`. These fall under **exception (a)**: without them a SysV C entry would
  corrupt registers the Win64 host expects preserved. The boundary is one convention per
  architecture on every OS (D23).

## Interface

### The boundary convention, per architecture
A C entry is a function in this convention, whatever the host OS. On x86-64 it is SysV on Windows
too. **A host never calls a C entry directly**; it calls through the runtime's `entry` (MSVC has
no `sysv_abi`). Only waboros-conforming code calls a C entry: other C entries, the runtime's
`entry`, and a compiler's `tail_switch`. Host functions keep the host's convention behind each
compiler's bridge.

| | x86-64 | aarch64 |
|---|---|---|
| convention | System V AMD64 on Linux, macOS, Windows and Android alike | AAPCS64. Linux, Android, Apple arm64 and Windows Arm64 agree on the registers below; they differ in `x18`, the red zone (Apple: 128 bytes), the frame-pointer requirement (Apple and Windows: mandatory), variadic and sub-word argument rules, unwind data and stack probes (Windows). The table takes the strictest of each |
| arguments | `rdi` = ctx, `rsi` = frame | `x0` = ctx, `x1` = frame |
| result | status in `eax`; the upper half of `rax` is undefined, callers use 32 bits | status in `w0`; the upper half of `x0` is undefined, callers use 32 bits |
| callee-saved | `rbx rbp r12 r13 r14 r15`, `rsp`; no xmm register | `x19`–`x28`, `x29`, `sp`, `d8`–`d15` (the low halves of `v8`–`v15`) |
| caller-saved (a C entry may clobber) | `rax rcx rdx rsi rdi r8`–`r11`, every xmm/ymm/zmm, the status flags | `x0`–`x17`, `x30`, `v0`–`v7`, `v16`–`v31`, the high halves of `v8`–`v15`, NZCV |
| never touched | — | `x18` (the platform register: Apple, Windows TEB; D23) |
| direction flag | DF = 0 at entry and at exit | — |
| FP control | MXCSR control bits (rounding RN, FTZ = 0, DAZ = 0, every exception masked: `0x1F80` with any sticky status) at entry and exit; a C entry that changes them restores them | FPCR at entry and exit: RMode = RN, FZ = 0, FZ16 = 0, DN = 0, AH = FIZ = NEP = 0 (FEAT_AFP), no trap enabled; a C entry that changes it restores it. PSTATE.SM = 0 and ZA off at entry and exit (SME) |
| x87 | control word preserved; x87 register stack empty at entry and exit | — |
| upper vector state | `vzeroupper` before any return or jump out of a C entry whose code used VEX or EVEX (above the baseline, ISA levels v3 and up) | — |
| stack at entry | `rsp` ≡ 8 (mod 16), i.e. 16-aligned at the `call` | `sp` 16-aligned at all times |
| red zone | **none on any OS**: nothing is kept below `rsp` (the Windows vectored handler of S0006 writes below it) | **none on any OS**: nothing is kept below `sp` (Apple's 128 bytes are not used) |
| frame record | — | `x29` is preserved. A C entry that makes a frame pushes a frame record `{x29, x30}` and points `x29` at it before any call (Apple and Windows require a valid chain); the walk does not read it |
| landing pad | `endbr64` first (CET IBT, R2) | `bti jc` first: accepts `blr` and `br` through any register (Q5); the runtime's `entry` too, so a foreign host may `br` to it through any register. A C entry that signs its return address puts `paciasp` after the `bti jc`: `paciasp` alone is only an implicit `bti c` and refuses `br` through a register other than `x16`/`x17` |
| reached by | `call`, or `jmp` with the stack as at a `call` (a sibling call, "Tail calls") | `blr`, or `br` with `x30` as at a `blr`: at a sibling `br`, `x30` holds the plain (unsigned, already authenticated) return address |
| return | `ret` to its own `call` (CET shadow stack) | `ret` to `x30` (Arm GCS) |
| native frames | the part allocated before the first `NSTACK_LIMIT` check stays within the headroom rule (about 768 bytes, with room for the saves around it); larger frames are allocated after the check, below one page per step or with the compiler's own stack probes (Windows): a compiler duty | the same |

### Arguments and the context
```c
/* SysV on x86-64 on every OS (__attribute__((sysv_abi)) on Windows hosts); AAPCS64 on aarch64.
   Called only through the runtime's entry or by conforming code, never directly by a host. */
int32_t f(uint64_t *ctx, uint64_t *frame);
```
* **`frame`** is the callee's frame base on the store's value stack, 8-byte aligned.
  * The arguments are in `frame[0..n)` and the results come back in `frame[0..m)`, one 64-bit slot
    per value, as for the entry (ARTIFACT.md part 3).
  * An i32 is zero-extended to 64 bits; a reference is zero-extended (a heap offset or reference
    word of part 3, or a function record's address); a v128 takes two slots.
  * The callee may write anywhere in `[frame, VSTACK_LIMIT)`. The caller keeps nothing at or above
    `frame` except the arguments, and reads only the results after the call (S0013 B1).
* **`ctx`** is the callee's context (the record's third word), fully initialised as part 3 lays
  it out for the callee's module.
  * Words 8 `HOST_BRIDGE` and 9 `TAIL_SWITCH` point to the copy of the **callee's** compiler.
  * A caller may read word 8 of the callee's context before the call (to recognise its own
    compiler, D71), so every record's context is a readable context laid out as part 3, never a
    placeholder, whichever compiler made it.
  * Words 2–6 (`INTERRUPT`, `FUEL`, `EPOCH_DEADLINE`, `VSTACK_LIMIT`, `NSTACK_LIMIT`) hold the
    store's values in every context code can switch to (LESSONS 31); the host writes them all
    (any thread may set `INTERRUPT`).
  * The exception is the collector's context (D61), which is kept out of `store.instances`. Its
    `INTERRUPT` (and its epoch) stay 0, so a collection is never cut in half, and during a
    collection its stack limits are its own (`collect.py`;
    `tests/jit/test_gc_subtype.py::test_the_collector_follows_the_stores_native_stack_limit`).
  * Word 7 (`HOST_ERROR`) is per context, managed by the context's own bridge.
  * Words 10 (`EXCEPTION`) and 14 (`EXIT_RECORD`) point to the store's pending word and exit
    record.
* **Stack headroom.** A C entry checks `NSTACK_LIMIT` and `VSTACK_LIMIT` itself, in its body's
  prologue, as every function does today. The rule: **no wasm code runs with the stack pointer
  below `NSTACK_LIMIT − 1 KiB`, except a bridge's call into the host.** It covers every decrease
  of the stack pointer before the next check: a caller's saves around a C call, a restore frame,
  a C entry's frame, and the next body's pushes and frame allocation. So every frame lies above
  `nlimit − 4096`, the lowest address the walk reads (D56). The prototype's worst case before a
  check is 224 bytes on x86-64; `jit-aarch64`'s is 240 bytes through a C path (64 of saves, the
  48-byte `centry` frame, a 128-byte body frame) and 256 through `tail_switch`'s first hop (its
  80-byte restore frame instead of the saves).

### The status and the private range
* The return value is `0` or a **public** status of part 3: a trap code, `INTERRUPT` (12),
  `HOST_ERROR` (13) or `14` (an exception in flight, the pending word set). A caller that gets a
  non-zero status passes it on, or dispatches 14 at a landing pad, exactly as after an internal
  call.
* **Reserved private range:** every status with bit 31 set (`0x80000000`–`0xFFFFFFFF`) is
  private to a compiler.
  * Part 3 never assigns a public code there; today's public codes are 0–18, and 255 in test
    builds (`STATUS_MISALIGNED`).
  * A compiler may use a private status only between its own code and its own C entry: the "hop"
    of "Tail calls" below.
  * A C entry treats only its own private value as a hop and passes every other value on
    unchanged.
  * No private value ever leaves a C entry, the runtime's `entry` or a bridge.
  * The scratch gate used `0x7FFF0001`, outside this range; it moves to the range.

### The C entry and the function record
* How a compiler builds the C entry is its own business:
  * the prototype uses a 16-byte stub before each body (`endbr64; lea rdx, [rip + 5]; jmp
    centry`) and one `centry` per module, which saves r12–r15, sets up its pinned registers and
    calls the body ("Measurements");
  * a compiler whose internal convention is the boundary convention (sljit's C functions) needs
    no stub: its function **is** its C entry.
    *(Superseded by S0016 for `jit-sljit`: its `REG_ARG` functions are not C entries, so it has
    `jit-x86_64`'s structure, a stub before each body and one `centry` per module, all emitted by
    sljit. The statement stays true only for a compiler whose functions use sljit's C convention
    inside.)*
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
  entry.
  * **The rule:** on the boundary there is always a C entry; a compiler may bypass it for
    callees it recognises as its own, calling or tail-calling their bodies in its internal
    convention. That is a private choice, as the same-context tail call is (case 1 of "Tail
    calls"), invisible at the boundary since both sides are the same compiler's code; a context
    of another compiler always gets the C path. `jit-x86_64` does it from step 1 (D71), in
    `_call_record`:
    * **its own context** (`[record + 16] == r15`): `code + 16` (the body behind the 16-byte C
      entry) is called as a direct call is, with no saves, so recursion through its own table
      costs a direct call's native stack;
    * **another context of this compiler**: context word 8 (`HOST_BRIDGE`, the address of the
      compiler's private bridge, one copy per compiler) of the record's context equals its own.
      The body is called with a context switch, as before S0015: `r15` saved with a pad, `r15`
      and memory 0's registers the callee's, ours back after; 32 bytes a cross-instance call, as
      before S0015;
    * otherwise the C path below.
    Each record call site then has three calls, all sites of the artifact with the same map (the
    core gives every backend site of one call its map; the backend's `record_sites`).
  * Before the call, the caller saves whatever of its own convention the C entry may clobber. For
    the Python backends that is the pool registers in the caller-saved row, for every frame above,
    as the bridge does today (LESSONS 2): x86-64 `rsi rdi r8`–`r11`; aarch64 `x2`–`x8`.
  * After the call, **before it tests the status and on every return path** (zero, a trap, 14),
    it restores the registers it saved and reloads what a callee may change (memory 0's
    registers: a callee may grow an imported memory).
  * Why on every path: S0012's "State at a landing pad" relies on the callee's epilogue having
    restored the pool on a non-zero status. A C entry does not do that: it keeps only the
    boundary's callee-saved registers, and sljit code clobbers `rsi`, `rdi` and `r9`. So the
    caller's pins and the pins of every frame above must already be back when a trap or an
    exception reaches a landing pad or the caller's unwind.
* Calls inside a module (`call` of a defined function) keep the compiler's internal convention.
* Every defined function and every host thunk has a C entry (Q1). The runtime's `entry` calls only
  C entries.
* A C entry and its internal call site lie in the module's code, below `data_start`, never in a
  trampoline blob.

### The runtime's `entry` (a contract)
```c
/* In the host's convention: x86-64 SysV or Win64, aarch64 AAPCS64. */
int32_t entry(uint64_t *ctx, uint64_t *frame, const void *c_entry);
```
The waboros runtime and every foreign host (D36) provide their own `entry` per architecture and
host convention. Its bytes are not part of any artifact. It must:
1. Save the host's callee-saved registers that the boundary convention does not keep, and those it
   uses itself. On Win64 that adds `rsi`, `rdi` and `xmm6`–`xmm15` (160 bytes), because SysV code
   may clobber them. The Python backends never touch xmm6–15 (S0003); sljit code may.
2. Keep the store's enclosing exit record and the frame at a fixed place on the native stack: the
   **saved block** `{sp, pc, frame}` of the exit record, then `frame`, 32 bytes, at the stack
   pointer of its call to `c_entry`. On x86-64 it is `[rsp, rsp + 32)`; on aarch64
   `[sp + 96, sp + 128)` (as today).
3. Call `c_entry(ctx, frame)` in the boundary convention, with the stack aligned as the table
   requires. The return point of this call is the walk's **entry point**.
4. Put the exit record back from the saved block.
5. Take the status as 32 bits (x86-64 `mov eax, eax`; aarch64 `mov w0, w0`), write it
   zero-extended to `ctx[TRAP_CODE]` when it is non-zero, and return it.

It sets up no pinned register: each compiler's C entry does that. It never sees a private status
(a C entry consumes it). The stack-pointer stub (`sp_stub`, `frame[0]` = the stack pointer as a C
entry sees it on entry) is the runtime's too, written as a C entry.

**Where they live in the package** (step 1):
* Today `entry` and `sp_stub` come out of each backend's `abi.assemble`, which is the compiler's
  side.
* From step 1 they move to a per-architecture runtime module apart from the compilers,
  `targets/jit/<arch>/entry.py`. It assembles `entry` (each host convention) and `sp_stub`, and
  `common/instance.py` maps them once per process.
* `abi.py` keeps only what is private to the Python compiler: its bridge and `tail_switch`. These
  are written into its artifacts (`.waboros.trampolines`), and until phase 6 the runtime also
  runs its own in-process copy of them for its own compiler string, as today.
* The S0006 fault handler stays the runtime's; it moves to the same runtime module or stays where
  it is (step 1 decides by size; it is per process, not per compiler). The reference listing is in
ARTIFACT.md part 3, "Reference listing of the runtime's `entry`".

### `bridge` and `tail_switch`: private to each compiler
* A compiler's bridge (wasm → host) and its `tail_switch` follow its own internal convention: they
  save its pool, read its context register and know its frame register. They are **not** part of
  this interface. They travel in the compiler's artifacts (`.waboros.trampolines`).
* Each instance's context words 8 `HOST_BRIDGE` and 9 `TAIL_SWITCH` point to the copy of its own
  compiler. Both words already exist per context; no word moves. Every compiler ships a
  `tail_switch`, so word 9 is never 0 (S0016: the "none" value left v1).
* **What every bridge must do:**
  * write the store's exit record (word 14) before the host runs and clear its `sp` after, with
    the meaning fixed here:
    * `sp`: the calling frame's stack pointer at its call (x86-64: before the `call` pushed);
    * `pc`: the return address;
    * `frame`: the frame base of the calling wasm frame, the base its site's map refers to;
  * preset `ctx[HOST_ERROR]` to 13, return the word as the status, and put back the enclosing
    preset (part 3, D34): the scratch gate's sljit bridge did both;
  * the rest of part 3's host-function contract, unchanged (the nested entry at
    `slots + max(n, m)`), with one sentence added: a host function writes only `0`, a public trap
    code or `13` into `HOST_ERROR`, so no private status leaves through a bridge.

### The walk interface
The GC's walk (D56, D65) and its check mode must cross frames of any compiler without knowing
their layout. The interface is:
1. **Every return address** on a store's native stack between an entry and the innermost exit
   record is either:
   * a **site** of some artifact's `STACKMAPS` (its `ret`, with `shift`, `ret_at`, `up` and the
     map), or
   * a **fixed point**: the return point of the runtime's `entry` (with its saved block), or the
     `restore` point of some compiler's `tail_switch` (with that compiler's restore-frame numbers
     from `.waboros.trampolines`). A restore frame has shift 0.
2. **A compiler provides `ret_at` and `up` for each site**: the site is the address after the call
   instruction, and the numbers follow from its frame. sljit has no public API for its frame size
   (the scratch gate and `poc/sljit/sljit-be/` read internal fields). *(S0016: `jit-sljit` gets
   them from `waboros_sljit_frame` in our own translation unit, which uses sljit's
   `SLJIT_GET_RETURN_ADDRESS` expressions and is checked by a ctest; an upstream
   `sljit_get_return_address_offset()` is to be proposed, no patch.)*
3. **A C entry's internal call is a site** of its own artifact, with shift 0 and no map
   (`nslots` 0). The prototype's `centry` call is one site per module (`ret_at` 40, `up` 48 on
   x86-64). A compiler whose C entry is the body itself has no internal call and so no such site.
4. **`frame` = the caller's base + 8 · shift**: as a C entry's `frame`, a caller passes its own
   frame base plus 8 times the shift its site record states. That is how the walk finds the
   callee's base.
5. **Invariant I1 in every compiler:** at a site, no reference of the any, extern or exn hierarchy
   and **no value derived from one** (an interior or untagged pointer, a base plus offset) is in a
   register or anywhere but a value-stack slot that the map marks (`refs`, `live`, D65). The
   collector forwards and nulls only slots. A compiler without exact maps refuses (`Unsupported`)
   every module with reference-typed values of the any, extern or exn hierarchy (funcrefs are
   never marked: records live outside the heap).
6. **Foreign frames are opaque:** the walk reads a frame only through its site numbers and its
   map. No frame-pointer chain, no unwind data, no compiler identity per frame.
7. The walk knows the **set** of entry points (the runtime's, one per convention in the process)
   and the set of restore points (one per compiler present in the store, each with its numbers).
   *(S0016: each target seals its own copy of the runtime's blob, so the entry points are one per
   compiler too; the store keeps both sets, added at each instantiation, and `walk.walk`,
   `walk.check` and the collector read them.)*

The GC heap, its object layout, the type-info and RTT records and the collector's root buffer are
part 3's and shared already: every compiler allocates and casts against the same store heap.
Runtime modules (the collector, D61) are compiled by the **runtime's own compiler** for the
architecture. In a phase-6 mixed store they are not compiled by whichever compiler made the
user's modules. This amends D64 consequence 3 ("compiled by the Store's compiler") and D61 ("by
the store's JIT").
*(Superseded by S0016, maintainer 2026-11-24: there is no "runtime's own compiler". The collector
is compiled by any compiler of the architecture, by default the target of the instance that first
needs it: the first module with a function-subtype site at its instantiation, or the instance
whose service runs the first collection. In a mixed store that may be `jit-sljit`'s code. Tested
for both triggers, either compiler first.)*

### Traps, interrupts and the guard handler
* **Traps are status returns** through every frame and every C entry, as today. No signal,
  `longjmp` or unwinder crosses a C entry. An exception is status 14 with the store's pending word
  (word 10).
* **Interrupts, the epoch and the stack limits** are context words the host writes in every
  context of the store; every compiler polls them in its own code. Fuel waits for Q4.
* **The guard fault handler** is per process (S0006). It owns a registry of code ranges, each with
  its artifact's fault map (`.waboros.traps`, `{fault_offset, resume_offset}`), and of reservation
  ranges. Its contract holds for any compiler:
  * a fault point is exactly one instruction;
  * the resume point is in the same artifact;
  * the handler changes only the PC.
  An artifact with `BOUNDS_GUARD` may import a memory only if that memory has a guard of at least
  `2^GUARD_LOG2` (every i32 memory reserves 4 GiB + 32 MiB, so this holds today).

### Tail calls across the boundary
**The property every compiler must have:** a chain of `return_call*` across the boundary, in any
mix of compilers, runs in **constant native stack** (D30; Wasmtime runs 10⁶ hops), and every
return matches its call, so it is safe under CET shadow stacks and Arm GCS (R2). How a compiler
gets there is its own; two forms meet the interface:
1. **Same context:** the compiler's own path. Its internal body is known to it; the prototype
   uses `code + 16`.
2. **Another context:**
   * **(a) A direct C sibling jump**, for a compiler whose functions are their C entries, or whose
     internal convention keeps across calls only registers the boundary also keeps. Its frame is
     torn down and it jumps to the record's C entry with `ctx` and `frame` set and the return
     address as its own caller left it. It needs no private status, but a compiler using it still
     ships a `tail_switch` (S0016: the "none" value left v1, so 2a exists only beside one). sljit
     does this with `SLJIT_CALL_RETURN`; the scratch gate ran 10⁵ such hops between sljit and
     Python-JIT functions in a 256 KiB thread.
   * **(b) First hop, then return-then-jump**, for a compiler whose internals keep registers the
     boundary may clobber (the Python backends' pools):
     * The **first hop** tears down the frame and jumps to the compiler's `tail_switch` (context
       word 9). That trampoline saves what the caller's caller expects of the compiler's
       convention, calls the C entry with `ctx` = the record's context and `frame` = the frame
       base, restores, and returns the status. Its frame is the **restore frame**: the prototype
       saves `rsi rdi r8`–`r11` and a pad, `jit-x86_64` `rsi rdi r8`–`r11` and `r15` (D71), 64
       bytes on x86-64.
     * A **later hop** starts in a body whose return address is the return point of its own
       module's C entry: it was entered through the boundary, so nothing of its compiler's
       convention is owed below it. It returns to its C entry (`ret`, balanced) with its private
       hop status and the record in a register of its choice. The C entry then restores the
       boundary's callee-saved registers and **jumps** to the foreign C entry with `ctx` and
       `frame` set: a C sibling call.
     * So A → B → A → … across compilers holds one restore frame and one C-entry frame at most.
     * `jit-x86_64` (D71): its `tail_switch` also serves a record of another context of the same
       compiler (the `HOST_BRIDGE` test above). The first hop's restore frame keeps `r15` with
       the pool registers (64 bytes either way) and calls the body behind the C entry with the
       context switched; a hop whose return address is already `restore` only switches and jumps
       (to a body of this compiler, or to another compiler's C entry with `ctx` and `frame` set).
       A body entered through its module's C entry still hops by return-then-jump. The hop status
       is returned only to a C entry's return point, so it cannot reach other code.
3. A compiler that turns a cross-boundary tail call into a call grows the stack and fails the
   conformance test ("Test plan").

**Correction to the prototype:** its later hop unwound the C-entry frame with `add rsp` and jumped
from the body, discarding a return address that a CET shadow stack still holds. The
return-then-jump form above keeps every `ret` balanced.

### Threads (D58)
Threads come after the format, as an amendment inside space v1 reserves (D58): one store per
thread, several stores sharing a memory. Calls through records never cross stores, so the boundary
itself does not change; three rules hold for every compiler of an architecture now:
* **One mapping of the atomic operations per architecture.** Wasm atomics are sequentially
  consistent; code of two compilers on one shared memory is only correct if both use the same
  mapping (as C/C++ compilers of one platform must). x86-64: a seq-cst load is a plain `mov`, a
  seq-cst store is `xchg` (or `mov` then `mfence`), a read-modify-write is a `lock`-prefixed
  instruction or `lock cmpxchg` loop. aarch64: a seq-cst load is `ldar`, a store `stlr`, a
  read-modify-write `ldaxr`/`stlxr` or the LSE `*al` forms (LSE only at a declared ISA level). No
  compiler uses the leading- or trailing-fence alternatives. The mapping is part of the boundary
  ABI; changing it is a `ctx_abi_version` bump.
* **A shared memory's size is never kept in a register** (D58), in a body or across a call through
  a record; a compiler reads it from the context at each bounds check.
* **Shared-everything threads** (one store used by several threads, so words 10 and 14 per thread)
  stay out of v1: they are version 2 (D58).
`memory.atomic.wait32/64` and `notify` are libcalls in the four reserved words of the block (D58),
provided by the runtime, so they do not depend on the compiler.

### What sljit-based compilers must add (D60's patch set)
*(Superseded by S0016 where marked: `jit-sljit` needs no sljit patch. Frame numbers come from our
translation unit, the baseline CPU word is set by `waboros_sljit_create()`, and BTI is custom words
as measured.)*
From the scratch gate (x86-64; "Measurements") and SLJIT-BACKEND.md:
* `ret_at` and `up` per site: a public frame-size API or a patch (walk interface item 2); *(S0016:
  `waboros_sljit_frame`, checked by a ctest)*;
* build with `SLJIT_CONFIG_X86_CET=1` (`endbr64` and shadow-stack-correct code);
* a native frame of at most about 768 bytes before the first `NSTACK_LIMIT` check:
  `sljit_emit_enter` allocates `local_size` before any code of ours runs, so a larger frame breaks
  the headroom rule; and frames below one page or stack probes (sljit's SysV build has no probes,
  also when its code runs on Windows), enforced by the core;
* aarch64: a `bti jc` custom word at the start of every C entry (sljit emits no BTI); with Q5 (a)
  no change to the register `SLJIT_CALL_RETURN` jumps through (`x9`);
* one sljit configuration for every OS (LESSONS 141), host calls only through the bridge.

## Compatibility
* **Until phase 6** the waboros loader keeps the **exact `compiler` string** (S0014 Q3 (a)): a thin
  file of another compiler or another compiler version is refused, and a fat file is regenerated
  with the store's compiler. A store then holds one compiler's code, and the trampoline compare
  stays in `read`, against the runtime's own blobs (S0014 "`.waboros.trampolines`"). *(S0016: a
  store may hold instances of several JIT targets of one architecture; `load(b, target=...)`
  takes each file's own target and applies the exact-string rule and `read`'s compare against
  that target's blobs, and no foreign blob is executed, both compilers being in the process.)*
* **Specified now, enabled in phase 6 together with mixed stores:**
  * an artifact is compatible when `e_machine` is the running architecture and `format_version`
    and `ctx_abi_version` are ones the loader reads; the ISA level and features, the flags (Q4)
    and `features_required` are checked as today;
  * the `compiler` string stays the **cache key**, not the compatibility rule;
  * D64's refusal "the compiler target is not the store's" is gone.
* **What enabling it needs** (phase 6; *the walk's sets and the distinctness of bridge addresses
  are done by S0016 in-process, the rest stays*):
  * executing a foreign compiler's blob: mapping it executable under W^X and invalidating the
    instruction cache on aarch64, as for module code;
  * the trampoline compare moving from `read` to **store attach**. The store keeps one copy of
    each compiler's blob per compiler string; the first artifact of a compiler string provides it,
    and every later artifact's blob for that string is compared byte for byte with it;
  * the walk's sets of restore points filled per compiler;
  * a blob copy is never shared between compiler strings, even when its bytes are identical: equal `HOST_BRIDGE` addresses must mean the same compiler string and the same internal convention, which `jit-x86_64`'s same-compiler record path relies on (D71);
  * before a foreign C entry runs in a store, a C entry that takes a private hop status from its
    body must be sure the status came from its own body. Today that holds by construction: a C
    entry calls only its own module's bodies, and every C entry of a store is one compiler's,
    which consumes its own private value (M5 of the step-1 review).

## What changes in the artifact format (S0014)
* **`.waboros.funcs`:** each defined function's code offset is its **C entry**, the one the
  runtime's `entry` calls. The layout is unchanged; the meaning changes. Where the body is, is the
  compiler's business.
* **`.waboros.context`:** each `thunks` offset is the host thunk's C entry.
* **`.waboros.trampolines`** holds **compiler blobs only**, one per host convention; the runtime's
  `entry` and `sp_stub` leave it:
  ```
  u32 n
  n x blob:  u8 id (1 sysv, 2 win64, 3 aapcs64), u8 0, u16 0,
             u32 start, u32 size,            the blob in .text
             u32 bridge,                     offset in .text
             u32 tail_switch, u32 restore,   offsets in .text
             u16 restore_ret_at, u16 restore_up
  ```
  * 28 bytes per blob. x86-64 writes `sysv` then `win64`; aarch64 writes `aapcs64`.
  * `restore_ret_at` is the number of bytes from the stack pointer at `tail_switch`'s call to the
    real return address; `restore_up` the same to the caller's stack pointer: x86-64 56 and 64,
    aarch64 8 and 80.
  * Every offset lies inside its blob; `0xFFFFFFFF` is refused (S0016: no "none" value in v1).
* **`.text`:** the code and data area as today, then each blob at the next multiple of 16 in record
  order, the gaps zero.
* **Symbols:**
  * `wasm[<i>]` at each function's C entry, `thunk[<i>]` at each host thunk's C entry;
  * `__waboros_centry_<conv>` at the module's shared C entry part, up to `data_start` (added at
    step 4: the writer finds it from the first stub's jump, a view carries no field for it);
  * per blob in record order, `__waboros_bridge_<conv>` and, when present,
    `__waboros_tail_switch_<conv>`;
  * the aarch64 mapping symbols as today;
  * **no global symbols** (`__waboros_entry_<conv>` goes with the entry; `sh_info` equals the
    symbol count).
* **`read`** (until phase 6) compares every blob and record with what the architecture's
  `conventions()` gives for this compiler string, as today but over the new fields.
* The inventory table (S0014 "What instantiation reads") splits `Trampolines` into the runtime's
  part (entry, `sp_stub`, `entry_ret`, `saved`: the runtime's own, not read from the file) and the
  compiler's (bridge, `tail_switch`, `restore` and its two numbers).

## What changes in ARTIFACT.md part 3
* A new **"Boundary convention"** section: the table and the argument, context and status rules
  above.
* "Entry": `entry(ctx, frame, c_entry)` is a contract the host fulfils (the five steps above), with
  the reference listing; it sets up no register.
* "Function records": `code` is a C entry, with the cases above.
* "Tail calls": the property and the forms above, in place of the record-in-`rdx`/`x16` jump.
* The trap-code table gains the **reserved private range** (bit 31 set); "Host functions" says a
  host writes only `0`, a public trap code or `13` into `HOST_ERROR`.
* The **"Per architecture"** table splits:
  * **fixed per architecture:** the boundary convention, the exit record's meaning, the entry's
    saved block, landing pads, `CODE` alignment, the instruction cache;
  * **compiler-private rows**, for `jit-x86_64` and `jit-aarch64` as information: bridge call,
    bridge preserves, `tail_switch` registers, the exit record's frame register. A host never
    provides a compiler's bridge or `tail_switch`; they come from the artifact.
* The D64 sentences ("The table is per compiler…", "a host providing the bridge or `tail_switch`
  follows the table of its Store's compiler") are removed. Part 2's loader text says the exact
  compiler string is the waboros loader's rule until phase 6.

## Versions
v1 is amended, not bumped (D70, D66): v1 is not frozen, and no file has been released. A record's
`code` and `.waboros.funcs` change meaning, and `.waboros.trampolines` changes layout, so files
written before this spec cannot run under it.
* The committed files of S0014 step 4.3 stay as **pre-S0015 fixtures**. `tests/artifact/test_old.py`
  changes its expectations, not its cases:
  * the thin files are **refused by `read` on the layout**, whatever the compiler string: the test
    runs with the compiler-string check passed (or patched). The `.waboros.trampolines` payload is
    40 bytes per record before and 28 after, so 84 against 60 bytes on x86-64 and 44 against 32
    on aarch64, and the exact parse fails. The refusal must not rest on an unrelated field
    (CLAUDE.md, "The JIT artifact is a contract");
  * the fat files are regenerated from their wasm and run.
* **New v1 files**, made after step 4 of the plan, are committed next to them with the same tests
  `test_old` runs today (read, inventory, instantiate, load thin and fat, version refusals).

## Measurements
* **Prototype:** [BENCHMARKS.md](../BENCHMARKS.md), "C-ABI boundary between instances (prototype,
  2026-11-21)". Every call through a record takes the C path, on `jit-x86_64` alone.
* Conditions: Intel Xeon @ 2.10 GHz, 4 vCPU, CPython 3.13.17, guard mode, `taskset -c 2`, variants
  alternated round by round, two sessions; `basepad` is the placement control (16 bytes of `int3`
  where the stubs go).

| case | base | basepad (control) | C path | difference |
|---|---|---|---|---|
| `call` of another instance's function | 2.60–2.67 ns | 2.68 ns | 4.37–4.42 ns | +1.7 ns |
| `call_indirect` (own table) | 3.79 ns | 3.00 ns | 4.51–4.57 ns | +1.5 ns against the control |
| CoreMark | 16 443–17 216 it/s (spread 15 620–18 437) | 17 054 | 17 353–18 095 it/s | within the spread; the control moves as much |
| CoreMark code | 34 547 bytes | 35 011 | 35 057 bytes | +1.5 % |
| D64's path: a host function into another store | 13.5 µs | | — | about 5 000 × a direct call |

* Correctness of the prototype:
  * the spec suite with the walk check: 64 229 pass, 0 fail, in guard and in explicit mode;
  * `gc` and `exceptions` with the three GC switches: 905 pass, 0 fail;
  * `tests/jit/test_tail.py` (cross-instance tail hops in a 256 KiB thread) passes.
* **The sljit gate** (a scratch experiment, x86-64, 2026-11-21, not committed). sljit-built C
  entries ran in one store with modules of the prototype, wired by hand: its code is in sljit's
  own memory with absolute addresses, so it is not an artifact. It showed:
  * calls both ways, direct and through `call_indirect`;
  * traps, a host exception and a trap of a Python-JIT callee, back through sljit frames;
  * walks under `WABOROS_JIT_WALK_CHECK=1` across sljit and Python-JIT frames, sljit site
    `ret_at` 24 / `up` 32 (the walk-check run covered calls, traps, the walk and GC);
  * 10⁵ cross-compiler tail hops by `SLJIT_CALL_RETURN` in a 256 KiB thread, run **without** the
    walk check, where a control that calls instead of tail-calling ends in `STACK_OVERFLOW`, as it
    must;
  * collections with a reference live in a sljit frame, through the Python side and through
    sljit's own bridge, forwarded correctly.
  Not tested there: interrupts and fuel, status 14 through sljit, a walk inside a hop chain,
  aarch64.
* **Not measured:**
  * aarch64 (measured since, natively: BENCHMARKS.md, `placement-arm.yml` compare run
    45169731213 on the step-3 commit);
  * the Win64 entry's xmm saves (still not measured);
  * the hop compare on every C-entry return and the return-then-jump hop (the prototype jumped
    from the body; included in the step-1 measurement since, BENCHMARKS.md, "The boundary
    convention on `jit-x86_64`");
  * a store of real sljit artifacts.

## Test plan
* **The spec suite** with `WABOROS_JIT_WALK_CHECK=1`, at 64 229 pass, 0 fail each, with
  `tools/spec_diff.py` empty both ways against before:
  * `jit-x86_64`: guard and explicit modes;
  * `jit-aarch64`: explicit mode under QEMU (`tools/run_arm64.py`), guard mode on the native arm64
    CI runner.
* **`gc` and `exceptions`** with `WABOROS_JIT_GC_STRESS=1 WABOROS_JIT_GC_POISON=1
  WABOROS_JIT_GC_VERIFY=1` and the walk check, in the same modes and places: 905 pass, 0 fail.
* **Conformance tests**, new, in `tests/jit/test_boundary.py`, on the native JIT and under QEMU:
  * **tail-call ping-pong in constant stack:** A `return_call`s an import of B, which
    `return_call_indirect`s back.
    * 10⁶ round trips in a 256 KiB thread, in three entry shapes: A entered from the host, from a
      direct call in A, and through a C entry. Each must finish.
    * A control that calls instead of tail-calling ends in `STACK_OVERFLOW`.
  * **status through a C entry:**
    * every public status of part 3 (a trap, `INTERRUPT`, a host error, exception 14 caught one
      instance up, uncaught to the host) crosses one and two C entries with the right code and the
      pending exception intact;
    * a hand-assembled C entry returning a status with garbage in the upper 32 bits (`rax` =
      `0xDEADBEEF00000000`, then `0xFFFFFFFF00000001`) gives status 0 and status 1, through a
      record call and through the runtime's `entry`;
    * a private status never reaches the host;
  * **a walk across modules:**
    * host → A → (record) B → (`call_indirect`) A → host import, under the walk check, with a
      reference live in each frame: the frames and bases found match the runtime's own record,
      and a collection forwards every frame's references;
    * the same inside a hop chain (a collection at the far end of a cross-instance tail chain);
  * **the boundary registers and machine state:** a hand-assembled C entry clobbers every
    caller-saved register of the table (xmm and v registers included) and is called through a
    record. The caller's pins survive, and DF, MXCSR and FPCR are as the table says after the
    call.
    * The same clobbering C entry also returns a trap, and status 14 into a `try_table` with
      pinned locals: the pins are intact at the landing pad and in every frame the trap unwinds
      (A3).
    * The runtime's Win64 `entry` is tested for `xmm6`–`xmm15` on the Windows CI runner.
* **The pre-S0015 fixtures** (A4): `test_old` asserts that `read` refuses each thin fixture on the
  trampolines payload's size, with the compiler-string check passed or patched.
* **Tests that depend on today's trampolines and offsets:**
  * `tools/stack_info.py` (around lines 73-77) calls the compiler's `entry`/`sp_stub`: it moves to
    the runtime module's, and CI keeps running it;
  * `tests/jit/test_exceptions.py::test_no_module_uses_the_reserved_words` (around lines 650-703)
    and `tests/jit/test_gc_subtype.py::test_no_module_uses_the_reserved_word` spy on
    `abi.assemble`: they follow the split into `entry.py` and `abi.py`;
  * `tests/jit/test_placement_knobs.py` (lines 34-35): the aarch64 offsets move if a per-module
    C entry comes first in the code.
* **CET and GCS balance: not tested on enforcing hardware.** No CI runner enforces CET shadow
  stacks or Arm GCS, and none runs Intel SDE. The balance of every call and return (the hop
  included) is checked by the review (step 6 of the plan). A test under SDE or on enforcing
  hardware is added when such a runner exists.
* **Mixed compilers: moved to phase 6**, with the compatibility rule it tests. A test-only hook
  that loads a second compiler string would exercise a path the loader does not have before then.
  The sljit gate is the evidence until phase 6.
* **Shape tests to update** (they fail on the prototype on shape only: one more site per module,
  one more walk frame per cross call, code offsets moved by the C entries):
  * `tests/jit/test_exit_record.py`: `test_a_libcall_leaves_the_record_of_its_site`,
    `test_exit_record_names_the_caller_and_survives_nested_entries`,
    `test_the_walk_steps_over_a_tail_switch_restore_frame`;
  * `tests/jit/test_liveness.py`: `test_exact_live_sets` (30 cases), `test_t13_nolive`;
  * `tests/jit/test_stackmaps.py`:
    * `test_a_function_cast_at_the_top_of_the_frame_keeps_its_slots_inside`,
      `test_a_module_without_references_has_empty_maps`,
      `test_a_non_final_call_indirect_has_two_sites`,
      `test_a_service_keeps_its_reference_argument`;
    * `test_a_struct_field_operand_is_marked_at_the_allocation_service`,
      `test_a_v128_between_two_references_takes_two_slots`, `test_funcref_is_never_marked`,
      `test_locals_and_an_operand_below_a_call`;
    * `test_native_numbers_of_a_frame_with_pins` (2 cases), `test_record_call_and_host_thunk`,
      `test_reference_locals_are_never_pinned`, `test_struct_anyref_and_i31_locals_are_marked`,
      `test_throw_allocation_is_a_site_over_its_payload`;
  * `tests/jit/test_walk.py`: `test_a_chain_of_direct_calls`,
    `test_frames_of_another_instance_indirect_and_ref_calls`, `test_nested_entries_50_deep`,
    `test_tail_calls_and_a_tail_switch_restore_frame`,
    `test_try_table_frames_libcalls_and_throw_allocations`;
  * expected to change with the trampolines (the prototype kept them, so they were not run
    against it): `tests/jit/test_abi.py`, `tests/jit/test_abi_aarch64.py`,
    `tests/jit/test_tail.py` (the restore frame), `tests/artifact/test_trampolines.py`,
    `tests/artifact/test_strict.py`, `tests/artifact/test_roundtrip.py`.
* **D35:** code bytes change, so the lists of both JITs are regenerated (`tools/jit_dump.py`). The
  cross-compiled identity holds: the aarch64 list from an x86-64 host equals the arm64 runner's.
* **ISA gate:** `tools/isa_all.py` on both JITs (the stubs and C entries are baseline code).
* **CoreMark**, alternated with the code before, `taskset -c 2`, several rounds, with a placement
  control (the code before plus padding where the C entries go; LESSONS 139). The claim is "within
  the spread" unless a difference survives the control. The calls benchmark and the cross-instance
  driver are re-run the same way and recorded in BENCHMARKS.md.
* **v1 files:** `test_old` with the pre-S0015 fixtures' new expectations, and the new v1 files
  with today's cases, on both architectures.

## Plan of steps
The boundary is a contract. x86-64 is reviewed before aarch64 starts, so a defect in the design is
found once. Each step stops and reports if it needs more than its size.

| step | goal | agent | acceptance | size (lines) |
|---|---|---|---|---|
| 1 | x86-64: C entries for every function and host thunk; record calls through them, saving rsi/rdi/r8–r11; records written with C entries; the runtime's SysV and Win64 `entry` by the contract (Win64 saves rsi, rdi, xmm6–15) and `sp_stub`; `tail_switch` as the first hop, later hops by return-then-jump with a status of the private range; the walk with sets of entry and restore points; `test_boundary.py`; the x86-64 shape tests | jit-engineer | the spec suite and GC runs of the Test plan on `jit-x86_64`; `test_boundary.py`; `pytest -q` (the x86-64 pre-S0015 fixtures with `test_old`'s new expectations); D35 list regenerated; ISA gate; CoreMark and the cross-call driver alternated with a placement control, recorded; `platforms.yml` green (Windows x64 and ARM64, macOS x86-64 and arm64, Android x86_64: tests and the spec suite; the Win64 `entry` is D47 exception (a)) | src ~200, tests ~300 |
| 2 | Review of step 1 without the implementer's reasoning: the boundary table against the code (every register a C entry may clobber is saved by every caller; machine state), the call/return balance of the hop, the private range, the walk's sets, the stack headroom bound | reviewer | findings closed before step 3 | — |
| 3 | aarch64: the same (save x2–x8 around record calls; `bti jc` C entries; the entry zero-extends w0 into `TRAP_CODE`; sibling hops); the aarch64 shape tests | jit-engineer | the same on `jit-aarch64`, explicit under QEMU and guard on the arm64 runner; the cross-compiled D35 identity; `platforms.yml` green on every row | src ~180, tests ~120 |
| 4 | Format and loader: `.waboros.funcs` and `thunks` as C entries; `.waboros.trampolines` with compiler blobs only, the symbols, `read`'s compare over the new fields; the inventory; the pre-S0015 fixtures' expectations; the new v1 files | implementer | `tests/artifact/` on both architectures; the spec suite through files (`WABOROS_JIT_ARTIFACT=1`) at parity on both JITs | src ~80, tests ~120 |
| 5 | Docs ("Docs this changes"), and S0014's text | orchestrator | `python3 tools/check_docs.py` | — |
| 6 | Review of steps 3–4 and of the whole for CET/GCS balance | reviewer | findings closed | — |

About 460 lines of source and 540 of tests. Deferred to phase 6, not in this plan: the foreign-blob
execution path, the trampoline compare at store attach, the per-architecture loader rule, and the
mixed-compiler test.

## Alternatives considered
* **Keep D64** (option 1): different compilers in different stores, talking through host
  functions. A call through the host into another store costs about 13.5 µs, about 5 000 times a
  direct cross-instance call (2.6 ns), and memories, tables and GC references cannot be shared.
  Rejected by D70.
* **One internal register table per architecture** (option 2): every compiler uses the same
  context, frame and memory registers and the same set of registers that survive a call.
  * sljit's `SLJIT_ENTER_KEEP(n)` keeps only S0..S(n-1) with n ≤ 3 (rbx, r15, r14 on x86-64 SysV;
    x19–x21 on aarch64; LESSONS 140), so our r12, r13 and x25–x28 would need a patched sljit
    register map.
  * sljit code clobbers rsi, rdi and r9 (x2–x7 on aarch64), which our pool keeps across calls; one
    compiler would lose registers.
  * Moving the Python JIT instead touches about 170 uses of the role constants and every code byte.
  * Above all it freezes both compilers' internals across versions.
  * Rejected by D70.
* **A fast path for records of one's own context** (the prototype's `cabi2`): a `call_indirect`
  into one's own table skips the C entry.
  * Measured 3.45–3.54 ns, against 4.51–4.57 on the C path and 3.00 for the placement control.
  * The cross-instance call slows to 5.04–5.07 ns, and each site needs two call instructions with
    one map.
  * Not done by D70; done privately in `jit-x86_64` in step 1, for its own and every other
    context of the same compiler, for the recursion depth (D71, the maintainer's decisions of
    2026-11-22), measured in BENCHMARKS.md.
* **`ctx_abi_version` 2** instead of amending v1: old files refused by version. Nothing released
  depends on v1, and the pre-S0015 fixtures are refused by `read` on their layout anyway, so a bump
  would only add a refused version to every loader. Rejected (D70, "Versions").
* **The runtime's `entry` as fixed bytes in every artifact** (Q1 (a) of the first draft):
  * foreign hosts would need no assembly;
  * but every file would carry about 150 bytes per convention that no compiler owns, compared by
    every loader.
  * Rejected by the maintainer (2026-11-21) for a contract with a reference listing.
* **Restore frames as sites** (`tail_switch`'s call recorded in a site table of its blob, so the
  walk has no special case): cleaner, but it adds a site table to the trampolines for one call.
  Kept as Q2 (b).
* **aarch64 C entries with `bti c` and sibling jumps only through `x16`/`x17`**: stricter BTI, but
  every sljit `SLJIT_CALL_RETURN` would need a patched jump register. Q5 (b).

## Open questions
1. **Which functions get a C entry.**
   * (a) Every defined function and every host thunk (x86-64: 16 bytes each, CoreMark +1.5 %
     code).
   * (b) Only those with a record, the exports and the start function.
   * Recommendation: **(a)**. The runtime's `entry` and the tests call functions by index, and
     the compiler does not need to know which functions the host will call.
2. **How the walk steps over a restore frame.**
   * (a) Per-compiler numbers in `.waboros.trampolines` (above).
   * (b) `tail_switch`'s call as a site of the blob.
   * Recommendation: **(a)** now, the smallest change; (b) if a third kind of trampoline frame
     appears.
3. **Where the reference listing of the runtime's `entry` lives.**
   * (a) The appendix of this spec, moved to ARTIFACT.md part 3 when implemented.
   * (b) A C file of inline assembly with the C loader.
   * Recommendation: **(a)**. One place states it, and the C loader's spec cites it.
4. **Flags that must agree within a store** (phase 6, when stores mix).
   * (a) The bounds mode and `GUARD_LOG2` may differ per artifact, under the guard rule for
     imported memories. The epoch flag must equal the store's configuration. Fuel waits until
     fuel exists: with it, part 3 defines one fuel cost per operator that every compiler charges,
     and the fuel flag must then match too.
   * (b) All code-affecting flags equal.
   * Recommendation: **(a)**. Bounds checks are local to the code; epochs and fuel are store-wide
     semantics, and fuel across compilers means nothing without one cost model.
5. **aarch64 landing pads for sibling jumps.**
   * (a) Every C entry starts with `bti jc`, so a sibling jump may go through any register.
     sljit needs only the custom word it needs anyway, not a patch of its jump register.
   * (b) `bti c` and sibling jumps only through `x16`/`x17`: stricter, but a sljit patch for
     `SLJIT_CALL_RETURN` (`br x9` today).
   * Recommendation: **(a)**. C entries are legitimate targets of calls already; a `jc` pad adds
     only `br` targets that are C entries.

## Docs this changes
Each is updated in the step that makes it untrue (step 5 for those no code step owns).
* [ARTIFACT.md](../ARTIFACT.md):
  * the introduction (lines 10-15): a host also supplies `entry` as machine code. The reference
    listing (moved to part 3) gains hex bytes for hosts without an assembler (Dart FFI, LuaJIT);
  * part 2: the `ENTRIES` row, the loader paragraph (the compiler-string rule until phase 6, the
    D64 clause), the fat-file text;
  * part 2 `STACKMAPS` (lines 105-127): shift 0 for a C entry's internal call; the CoreMark site
    count, which goes stale; the trampolines' own points move;
  * part 3:
    * the "Boundary convention" section, "Entry" as a contract with the reference listing,
      "Function records", "Tail calls", the private status range in the trap codes;
    * the context rows: word 8 per compiler; word 9 per compiler and possibly 0; word 14's
      `frame` meaning; the memory row (line 185: reloaded after record calls too); the
      `IS_SUBTYPE` row (line 196);
    * "Host functions" (line 254): the bridge's saves become compiler-private, and `HOST_ERROR`
      takes only public values;
    * the split "Per architecture" table: the landing-pad row becomes `bti jc` at C entries, and
      jump-table `bti j` moves to the compiler rows.
* [S0014](S0014-artifact-format.md):
  * `.waboros.funcs`, `.waboros.context` (`thunks`; "at least 18 words (the entry reads memory 0's
    words)", which no longer holds), `.waboros.trampolines`, `.text`, the symbols;
  * the `compiler` field (line 177) and its D64 clause; "What instantiation reads";
  * `read`'s trampoline compare and "Semantics: the loader" (compatibility, phase 6);
  * the foreign-host "one copy per store" rule; Q2 and "Alternatives considered", partly reversed
    for `entry`;
  * the test plan; a Changes line.
* [S0013](S0013-gc.md): the walk section (sets of entry and restore points, C-entry sites, I1 with
  derived pointers) and a Changes line.
* [S0012](S0012-exceptions.md): "State at a landing pad". After a record call the caller has
  restored its pool itself (A3), not the callee's epilogue.
* [S0005](S0005-simd.md) (lines 124 and 299): the xmm6-15 rule and its Win64-entry reason (the
  runtime's Win64 `entry` now saves them).
* [S0003](S0003-jit-context-abi.md) and [S0004](S0004-jit-aarch64.md):
  * the body text ("one entry … `tail_switch` serve all"), the BTI paragraph;
  * the per-frame bounds (≤ 144 / 96 bytes), with the recursion depth through `call_indirect`
    recorded;
  * Changes lines.
* [DECISIONS.md](../DECISIONS.md): D70 if the approval changes it, and "amended by D70" markers
  on:
  * D30 (one `tail_switch` per process; the restore-jump path);
  * D36 (the host supplies `entry`);
  * D56 ("the trampolines are the runtime's own", "restore frames by fixed numbers");
  * D59 (the `__waboros_entry_*` global symbols);
  * D61 (the collector compiled by the runtime's own compiler);
  * D62 rule 2;
  * D64 consequence 3.
* [ARCHITECTURE.md](../ARCHITECTURE.md): lines 301-303, 384 and 418.
* [MICRO-COMPILERS.md](../MICRO-COMPILERS.md) (lines 86-90) and
  [MICRO-COMPILERS-SHIPPING.md](../MICRO-COMPILERS-SHIPPING.md) (line 20): D64's "one compiler per
  store".
* [SLJIT-BACKEND.md](../SLJIT-BACKEND.md) (line 70 and a new paragraph): the sljit compiler's C
  entries, sibling tail calls and the patches listed above.
* [RISKS.md](../RISKS.md): R1 (lines 16-17); R2's BTI row (`bti jc`).
* [STATIC-BUILDS.md](../STATIC-BUILDS.md) (lines 64-75, 85, 94) and
  [ARTIFACT-CONTAINER.md](../ARTIFACT-CONTAINER.md) (lines 73 and 82): the entry symbols and the
  trampolines.
* [ROADMAP.md](../ROADMAP.md):
  * line 302 (D64);
  * phase 6 gains S0015's deferred items: the per-architecture rule, mixed stores, executing a
    foreign blob, the compare at store attach, the walk's restore set, the mixed-compiler test.
* [TESTING.md](../TESTING.md): `tests/jit/test_boundary.py`; the pre-S0015 fixtures; lines 95-101
  (`test_trampolines`: `saved` leaves the file); line 331 (`tools/stack_info.py`).
* [BENCHMARKS.md](../BENCHMARKS.md): the step-1 measurements.
* [LESSONS.md](../LESSONS.md): whatever the steps find.
* `tests/artifact/data/README.md`: the pre-S0015 fixtures and the new v1 files.

## Reference listing of the runtime's `entry`
Moved to [ARTIFACT.md](../ARTIFACT.md) part 3, "Reference listing of the runtime's `entry`", as Q3
(a) says, when the spec was implemented (2026-11-23). It is the contract's one statement.

## Progress
* 2026-11-21: draft.
* 2026-11-22: **approved** by the maintainer, with the open questions' recommendations (Q1-Q5)
  and the threads section; implementation starts with step 1 (x86-64).
* 2026-11-22: step 1 implemented (x86-64), with the plan's answers: the file keeps the 40-byte
  trampoline record until step 4, with the runtime's `entry` and `sp_stub` (`x86_64/entry.py`)
  next to the compiler's bridge and `tail_switch` (`x86_64/abi.py`) in one blob; the walk takes
  the restore numbers from `Trampolines` (single values, no sets before phase 6); the fault
  handler stays in `abi.py`; `test_old` refuses the x86-64 fixtures on the trampoline compare
  until step 4. The spec suite under the walk check 64 229 pass, 0 fail in both bounds modes, `gc`
  and `exceptions` with the GC switches 905, through files at parity; record calls +1.5 to
  +1.8 ns against the placement control, CoreMark within the spread (BENCHMARKS.md, "The
  boundary convention on `jit-x86_64`"). Step 4 notes: `centry` had no symbol of its own (step 4 adds
  one, below); S0014's "at least 18 words" holds for this compiler's
  C entry, not for the boundary.
* 2026-11-22: step 3 implemented (aarch64), with the maintainer's answers to the plan's questions:
  `bti jc` on everything at the boundary (the runtime's `entry`, `sp_stub`, every C entry; `bti c`
  stays on bodies, the bridge and `tail_switch`); a 12-byte C-entry stub (`bti jc; adr x16, body;
  b centry`), the body at `code + 12`; `centry` saves `x25`–`x28` under a frame record (the
  bodies keep `x19`–`x24`), shown by a harness that calls a function whose pins fill the pool
  through the runtime's `entry`; the hop status by an exact 32-bit compare with `0x80000001`;
  `centry`'s return point found by `adrp` + `add` (a body may be farther than `adr`'s 1 MiB);
  `tail_switch`'s restore frame a real frame record with `x2`–`x8` and `x28`, 80 bytes, numbers
  8/80; record calls on three paths (D71), sites (8, size), (24, size + 16), (72, size + 64);
  the headroom 240 / 256 bytes. Under QEMU (BTI enforced) the spec suite with the walk check
  64 229 pass, 0 fail in explicit mode, `spec_diff` against HEAD empty; `gc` and `exceptions`
  with the GC switches 905; through files 64 229. x86-64 code byte-identical (D35). Timing on
  the native runner: `placement-arm.yml`, mode `compare` (BENCHMARKS.md).
* 2026-11-23: step 4 implemented (format and loader), with the maintainer's answers: the file
  carries the 28-byte compiler-blob record (bridge, `tail_switch`, `restore`, the two restore
  numbers; `0xFFFFFFFF` stays the format's "none" for case 2a, but no loader path uses it yet:
  this compiler always has `tail_switch`, and the compare refuses a blob without one) and `.text` holds bridge and
  `tail_switch` only; the runtime's `entry` and `sp_stub` are `Target.runtime()` (not in a file),
  sealed with the compiler's blob into the in-process copy by `load_trampolines`. All symbols are
  local: `centry` **now has one**, `__waboros_centry_<conv>` (`<conv>` the first convention of the
  file, as C entries are one convention on every OS), after the functions and thunks, up to
  `data_start`; the writer finds it from the jump of the first C-entry stub (a view has no field for
  it and the reader still reads no symbol), and `__waboros_entry_*` and the `sp_stub` symbol are
  gone, `sh_info` the symbol count. `read` refuses the 40-byte layout on the payload's size (84
  against 60 bytes on x86-64, 44 against 32 on aarch64), also with the compiler string patched.
  New v1 files `v1-s15-small-*` beside the pre-S0015 ones. Code bytes unchanged (D35 lists equal
  to HEAD's on both targets); the spec suite through files 64 229 pass, 0 fail (BENCHMARKS.md,
  "Artifact size": the CoreMark files are 536 bytes smaller on x86-64, 168 on aarch64).
* 2026-11-23: step 5 (the documents of "Docs this changes") done. Markers of D70 on D30, D36,
  D56, D61 and D64 consequence 3 (D59 and D62 rule 2 had theirs from step 4); ARTIFACT.md's
  introduction, loader paragraph, rows of words 9 and 14, `HOST_ERROR`, the split landing-pad
  row; S0003, S0004, S0013, S0014 body text and Changes lines; ARCHITECTURE, RISKS R1 and R2,
  MICRO-COMPILERS, MICRO-COMPILERS-SHIPPING, SLJIT-BACKEND (a boundary paragraph) and ROADMAP
  (the D64 line, phase 6's deferred items). Found true against the code without change: S0005,
  S0012, TESTING, BENCHMARKS, LESSONS, STATIC-BUILDS, ARTIFACT-CONTAINER and
  `tests/artifact/data/README.md`. Not done by anything yet: the appendix's hex bytes of each
  reference listing (ARTIFACT.md's introduction names it as planned). Word 9 equal to 0 and the
  sljit rows are specified, but no compiler uses them and no test covers them.
* 2026-11-23: step 6 (final review of steps 3-4 and of the whole). No correctness defect found.
  the QEMU child-exec test fix: `tests/jit/test_boundary.py` starts the subprocess of its ISA gate
  through `child()` (a test fix, no code change). Documents: the reference listing moved
  to ARTIFACT.md part 3 and checked against `entry.py` of both architectures (same, but the code
  uses `movups` for the Win64 xmm saves where the listing has `movdqa`); the "Per architecture"
  table split as "What changes in ARTIFACT.md part 3" says; ARTIFACT.md's `ENTRIES` text matched
  to step 4; "Not measured" updated; ROADMAP phase 6 gains the missing deferred items. Status set
  to `implemented`, as S0005 and S0009 are marked. The Changes section has the matching line.

## Changes
* 2026-11-21: created (draft), from D70 and the prototype measured in BENCHMARKS.md.
* 2026-11-21: revised (draft). The maintainer's decisions:
  * the runtime's `entry` is a contract with a reference listing, no longer bytes in artifacts
    (the first draft's Q1 (b)); `.waboros.trampolines` holds compiler blobs only, fully laid out;
  * the step-4.3 files stay as pre-S0015 fixtures, with new v1 files added (no "regenerate once");
  * the exact compiler string stays the loader's rule until phase 6; the per-architecture rule
    is specified for phase 6, with the foreign-blob path and the compare at store attach;
  * the mixed-compiler test moves to phase 6.
  
  From the review:
  * the private status range (bit 31);
  * the machine-state rows (DF, MXCSR/FPCR, x87, no red zone, `vzeroupper`, BTI and PAC, the x29
    rule);
  * the frame, context and stack-headroom rules;
  * Windows calls through `entry` only;
  * D47 exception (a);
  * I1 for derived pointers, refusal without exact maps, C entries below `data_start`, shift 0
    for restore frames;
  * tail calls stated as a property;
  * CET/GCS not tested on enforcing hardware;
  * the `call_indirect` cost against the placement control (+1.5 ns);
  * a review after x86-64;
  * the hop compare unmeasured;
  * the aarch64 entry's zero-extension;
  * the upper-half status test;
  * bounds modes per place;
  * fuel in Q4;
  * more docs listed.

  From the sljit gate: sibling-jump tail calls (case 2a), the `bti jc` choice (Q5), the sljit patch
  list, the bridge duties, and what the gate did not test.
* 2026-11-21: after the re-review (nothing blocking):
  * the appendix: listings one instruction per line in GNU as syntax, the Win64 listing written out
    and assembled, which listings were run;
  * the headroom rule as "no wasm code below `NSTACK_LIMIT − 1 KiB` but a bridge's host call", with
    the 768-byte first-frame bound for sljit;
  * `HOST_ERROR` takes only public values;
  * the unsigned `x30` at a sibling `br`;
  * FEAT_AFP and SME state;
  * the any/extern/exn wording of walk item 5;
  * the `0xFFFFFFFF` exception to the offset rule;
  * the tail hops of the gate ran without the walk check.
* 2026-11-21: after the cross-spec consistency audit:
  * A2: the collector's context is the exception to the store-wide words (D61);
  * A3: a caller restores its saved registers and reloads memory before the status test, on every
    path, with the S0012 consequence and a test;
  * A4: the pre-S0015 thin fixtures are refused by `read` on the layout (84 against 60 bytes on
    x86-64, 44 against 32 on aarch64), independent of the compiler string;
  * A5: `platforms.yml` in the acceptance of steps 1 and 3;
  * runtime modules are compiled by the runtime's own compiler (amends D64 c.3 and D61);
  * `entry` and `sp_stub` move to a runtime module per architecture, `targets/jit/<arch>/entry.py`;
  * "Docs this changes" extended to every document the audit found;
  * the tests that spy on `abi.assemble` or depend on offsets added.
* 2026-11-22: "Threads (D58)" added (one atomics mapping per architecture, a shared memory's
  size never in a register, shared-everything threads stay version 2); approved by the maintainer.
* 2026-11-22: step 1 review (no blocking issue); by the maintainer's decision the own-context
  fast path (the prototype's `cabi2`, D71) is done in step 1: "The C entry and the function
  record", "Alternatives considered"; a phase-6 note on the private hop status (M5).
* 2026-11-22: by the maintainer's decision a record of another context of the same compiler
  (`HOST_BRIDGE` equal) is called at its body too, and `jit-x86_64`'s `tail_switch` serves it
  (D71 accepted): "The C entry and the function record", "Tail calls across the boundary". The
  boundary contract is unchanged.
* 2026-11-22: step 3 (aarch64): the runtime's `entry` starts with `bti jc` (the table's landing-pad
  row, the appendix listing; maintainer's decision); the aarch64 headroom numbers.
* 2026-11-23: step 4 (format and loader): the symbols gain a local `__waboros_centry_<conv>` (the
  step-1 note "`centry` has no symbol" is withdrawn); see Progress.
* 2026-11-23: after step 4 (maintainer): `runtime()` of x86-64 takes the convention and builds
  the SysV and the Win64 entry on any host, as the compiler's blobs are (D23); `test_boundary.py`
  builds and ISA-gates both on Linux, the Win64 one checked for its saves and restores of rsi, rdi and
  xmm6-xmm15,
  so the Win64 entry is not left to the Windows runner alone.
* 2026-11-23: step 5 (docs): see Progress; no change to the contract.
* 2026-11-23: step 6 (final review): the reference listing moved from this spec's appendix to
  ARTIFACT.md part 3 (Q3 (a)); the "Per architecture" table split into fixed and compiler-private
  rows; "Not measured" updated; status `implemented`; see Progress. No change to the contract.
* 2026-11-24 (maintainer): provisions for 32-bit and microcontroller targets, which may come only in
  ABI v2 (ARTIFACT.md part 3). Code addresses (records, `HOST_BRIDGE`, `TAIL_SWITCH`, import words)
  are in the ISA's callable form (bit 0 set on Thumb); the walk clears the ISA's mode bits of a return
  address before matching `ret` (Thumb bit 0; windowed Xtensa bits 31:30, with the host spilling the
  register windows before a walk). The headroom numbers (1 KiB, about 768 bytes, `nlimit − 4096`) are
  the values of x86-64 and aarch64; another architecture states its own in the boundary row. No change
  for x86-64 and aarch64.
* 2026-11-24 (S0016 step 2, maintainer's decision under the working freeze): the "none" value of
  `tail_switch` leaves v1. Word 9 is never 0, case 2a exists only beside a `tail_switch`, and the
  trampoline record has no `0xFFFFFFFF` offset (`read` refuses it). The walk and the walk check
  take the store's sets of entry and restore points (walk item 7), not one instance's.
* 2026-11-24 (S0016 step 7, docs): the C-entry paragraph, walk item 2, the collector paragraph,
  "What sljit-based compilers must add", the deferred lists and "Compatibility" are marked as
  superseded or done in part by S0016 (`jit-sljit` has stubs and a `centry`; frame numbers from
  `native/`; no "runtime's own compiler"; in-process mixed stores). No change to the contract.
