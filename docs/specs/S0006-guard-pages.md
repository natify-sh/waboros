---
title: Guard pages instead of explicit bounds checks on jit-x86_64 and jit-aarch64
kind: spec
id: S0006
status: approved
superseded_by:
related: [D10, D23, D25, D29, D31, D33, D35, D36, D37, D39, D42, R2, R3, R6, S0003, S0004, S0005]
owner: maintainer
updated: 2026-11-03
---

# S0006 Guard pages instead of explicit bounds checks on jit-x86_64 and jit-aarch64

## Summary
D29: JIT code may omit the bounds check of a 32-bit memory access when the memory sits in a
reservation whose inaccessible tail catches every out-of-bounds address, and a fault handler turns
the fault into the `MEMORY_OUT_OF_BOUNDS` trap. This spec fixes the code the JITs emit in that mode
(`guard`), the OS-neutral data the artifact gains (a mode flag and a fault map), the handler as a
**host-side runtime service** specified precisely enough for the D36 C loader, how a mode is selected
and refused, and the steps. The maintainer's binding requirement: **the artifact stays OS-independent
(D23)**. Function code is identical on every OS; the handler, `ucontext`/`CONTEXT` offsets,
`SIGBUS` on macOS and the Windows vectored handler live in the loader/runtime only.

Ideas read, not copied: `poc/w2jit_guard.py` (resume at the module's trap stub, chain to the previous
handler, restore the default action otherwise); Wasmtime main (read 2026-11-02:
`crates/environ/src/tunables.rs` — 4 GiB `memory_reservation` and 32 MiB `memory_guard_size` on
64-bit hosts, 10 MiB / 64 KiB on 32-bit hosts; `runtime/vm/sys/unix/signals.rs` — `SA_SIGINFO |
SA_NODEFER | SA_ONSTACK`, SIGBUS on Apple and FreeBSD, delegation to the previous handler or the
previous disposition; `traphandlers.rs` `test_if_trap` — "ours" is decided by the PC alone: in wasm
code and registered as trapping, otherwise left to crash; `sys/windows/vectored_exceptions.rs` —
`AddVectoredExceptionHandler(1, …)`; `machports.rs` — Mach ports on macOS by default, with a
helper thread).

**Measured for this spec** (2026-11-02, Intel Xeon @ 2.10 GHz, 4 vCPU VM, CPython 3.13.17; scratch
scripts, not in the repo):

| what | result |
|---|---|
| static memory accesses in CoreMark (`poc/in/core.wasm`) | 669: offset 0: 372; end ≤ 4 KiB: 254; end ≤ 32 MiB: 43; beyond: **0** |
| same, the 4 497 spec modules that parse (`.cache/spec/…/json`) | 4 377 (3 230 scalar, 1 147 SIMD); end > 2 GiB: 31 (29 scalar, 2 SIMD, the large-offset tests); all others ≤ 32 MiB |
| CoreMark code, checks removed (`_bounds` without `cmp`/`jcc`) | x86-64 45 055 → 35 808 bytes (−20.5 %); aarch64 (cross-compiled) 39 152 → 30 864 (−21.2 %); fault map 669 entries |
| CoreMark it/s, package `jit-x86_64`, explicit vs checks removed (no handler; CoreMark never goes out of bounds), 9 alternating pairs | explicit median 14 805 (13 572–15 744); without checks median 15 922 (15 281–17 295): **+7.5 %** (means +8.9 %) |
| same session, PoC `run_jit_guard.py` explicit vs `W2PY_GUARD=1`, 3 pairs | 15 071 vs 16 117 (means): **+6.9 %** — not the +20–25 % of the BENCHMARKS row, so the gain depends on the machine |
| one `Instance.call` of a one-load export (`jit-x86_64`) | 4 148 ns; a `sigaction` query through ctypes costs 580 ns (a bare ctypes call 350 ns) |
| x86-64 native: an 8-byte store straddling into `PROT_NONE` | `si_addr` = first byte of the inaccessible page; nothing written |
| QEMU 8.2.2 user mode, aarch64, BTI enforced: loads and stores in the guard and straddling | `si_addr` and `mcontext.fault_address` = first inaccessible byte; `pc` = the faulting instruction; rewriting `pc` resumes there; a straddling store writes nothing; the handler needs `bti c`, the resume point does not (sigreturn is not a branch) |
| CPython `faulthandler` vs a chaining handler | ours installed after `faulthandler.enable()`: guard faults recovered, a foreign fault prints faulthandler's traceback and exits 139, as without us. `faulthandler.enable()` after ours: the next guard fault kills the process. Re-installing ours on top of it: a foreign fault loops (hang). `faulthandler.disable()` after ours resets SIGSEGV to the default, removing ours |

## Goals / Non-goals
* Goals: a `guard` mode on `jit-x86_64` and `jit-aarch64` with spec parity with `explicit` and `pyc`
  (D25); identical function code for every OS; the handler as a specified runtime service; `explicit`
  always available and selected automatically wherever the handler is not verified; a measured
  CoreMark and SIMD gain recorded in BENCHMARKS.md.
* Non-goals: stack-overflow detection by guard pages (prologue checks stay, S0003), traps by
  `SIGFPE`/`SIGILL` (division and `unreachable` stay explicit), memory64 and custom page sizes in
  guard mode, shared memories before threads exist, Mach exception ports, `pyc`/`mpy` (no change),
  the public API (`Config.signals_based_traps` is phase 6, API.md).

## Interface

### Memory reservation (host obligation)
* Every 32-bit memory of a store on a guard-capable host is reserved as `4 GiB + GUARD` bytes
  (`GUARD` = 32 MiB, Wasmtime's 64-bit default), mapped without access; `[base, base + size_bytes)`
  is read-write. The layout is the same whatever the mode of the modules that use the memory, so
  imported, exported and host-created memories need no per-mode handling.
* `memory.grow` commits pages inside the reservation (`mprotect` / `VirtualAlloc(MEM_COMMIT)`),
  **commits before it publishes the new `size_bytes`**, never moves the base, never decommits. A
  memory at 65 536 pages has its whole 4 GiB committed; the `GUARD` tail still catches offsets.
* Multi-memory (D37): one reservation per memory; memory *i* > 0 reads its base from the context at
  each access, as now.
* Shared memories (threads, not implemented): the same reservation; the commit-before-publish rule is
  what makes a racing access trap rather than read uncommitted pages.
* memory64 and memories with a custom page size: always `explicit` (an index can exceed any
  reservation). A 32-bit host has no JIT target; one with a small address space (`RLIMIT_AS`) fails
  the reservation and the module gets `explicit` (Selection).

### Code generation
An access to memory *m* with static offset `off` and access size `n` (1–16) is **unchecked** when
`off + n ≤ GUARD`; for a constant address `c`, when `c + off + n ≤ 2³² + GUARD`. Every other access
keeps today's `_bounds` check unchanged. Then the address is formed exactly as today (x86-64: the index
copied by a 32-bit `mov`, so the safety of the access does not rest on the zero-extension invariant
of LESSONS 5; aarch64: a `uxtw` register offset or a 32-bit `mov`), and the access is emitted.

* **Unchecked**: scalar loads and stores of every width; SIMD `v128.load/store`, `load*x*`,
  `load*_splat`, `load*_zero`, `load*_lane`, `store*_lane`, including 16-byte accesses at the end of
  memory.
* **Stay explicit**: `memory.copy`, `memory.fill`, `memory.init` (both ranges checked before any byte
  moves; their loops push a scratch register); table accesses, `call_indirect`, `table.*`; stack
  limits; division, conversions; offsets above the threshold.
* **One instruction per store.** A store in guard mode is a single instruction; a store the backend
  emits as several instructions keeps the explicit check (a fault after the first would leave a
  partial write; `memory_trap.wast` checks that none happens). Loads may be split. A single
  instruction is precise on native x86-64; an emulator that splits it may tear (D45).
* **The fault point invariant.** Each memory-touching instruction of an unchecked access is a *fault
  point*: a position where the explicit check's `jcc oob` would have been legal — the native stack at
  the function body's depth (no `push` outstanding), every callee-saved register as the epilogue
  expects. Its resume address is the function's `MEMORY_OUT_OF_BOUNDS` stub (`mov eax/w0, 2` then
  `unwind`), created on demand as today. Recovery therefore returns through frames like every other
  trap: no `rsp` reset, so CET shadow stacks stay consistent (R2), `SAVED_SP` stays unused and
  `TRAP_CODE` is written by the trampoline as for every trap (D34).
* **The fault map** is the list of `(fault offset, resume offset)` pairs, both relative to `CODE`,
  appended in emission order, so it is sorted by construction and deterministic (D35); resume
  offsets are label fixups resolved with the code. When aarch64 recompiles a function in far mode
  (`rewind`), the entries recorded since the function's start are dropped with the code.
* The backend interface (D39) is unchanged: `_bounds` and the SIMD memory helpers are private; the
  mode is a backend constructor argument and the fault map a new field of `Compiled` (the
  `jit_dump` digest includes it, so the cross-compile diff covers it).

### Artifact (ARTIFACT.md part 2)
* Header `flags`: bit 0 `BOUNDS_GUARD` (0: every access checked; 1: this spec); bits 8–15
  `GUARD_LOG2`, the guard size the code assumes (25 for 32 MiB), zero when bit 0 is clear. Both are
  in `config_sha256` and the cache key (D29).
* Section 12 `TRAPMAP`: `u32 fault_count`, then `fault_count` entries `{u32 fault_offset, u32
  resume_offset}` sorted by `fault_offset` (strictly increasing), then the frames table that part 2
  already reserves (wasm function index and byte offset, for `Trap.frames`; not specified here). An
  artifact with `BOUNDS_GUARD` and no `TRAPMAP` is malformed. Size: 8 bytes per fault point (CoreMark:
  ~5.4 KB, against 9.2 KB less code). Under an ELF container (ARTIFACT-CONTAINER.md, open) the same
  payload is `.waboros.traps`; this spec does not depend on the choice.
* **Version rules**: this is an **amendment of format version 1 without a bump**, like D30, D32,
  D34, D37 and D38: no artifact is saved to a file before phase 5 fixes the format (D35, D36), and
  this spec lands before that. `ctx_abi_version` is unchanged: no context word changes. Part 3 gains
  the host obligations above (reservation, commit before publish) and the handler contract below as
  the specification of a runtime service (D36). Once the format is fixed, any change here bumps it.

### The handler contract (any host, any language)
A host that runs a `BOUNDS_GUARD` artifact must, for the whole time any of its code can run, have a
process-wide fault handler that does the following. **A fault is ours if and only if**:
1. it is a hardware access fault: `SIGSEGV`, or `SIGBUS` on macOS and the BSDs, with `si_code > 0`
   (kernel-generated, so a `kill`/`raise` is never ours; on macOS `si_code` does not tell, so there
   2 and 3 decide alone, Changes, step 3); Windows: `EXCEPTION_ACCESS_VIOLATION` with its two
   parameters (`STATUS_IN_PAGE_ERROR`, a paging I/O failure, is never ours; step 4);
2. the PC (Windows: `ContextRecord->Rip` / `->Pc`) is inside the `CODE` of a loaded guard artifact and its offset is a `fault_offset` of that
   artifact's `TRAPMAP` (binary search);
3. the fault address (`si_addr`; Windows `ExceptionInformation[1]`) is inside a memory reservation
   `[base, base + 4 GiB + GUARD)` of the process. The whole reservation counts, not only the
   inaccessible part: the reported address is the first inaccessible byte on the machines measured,
   but a straddling access starts in the accessible part.

**Recovery**: set the PC in the saved context to `code_base + resume_offset` and return (Windows:
`EXCEPTION_CONTINUE_EXECUTION`). No other register changes. **Otherwise** the fault is passed on
exactly as if the handler were absent: the previous `SA_SIGINFO` handler is called with the same
three arguments, a previous plain handler with the signal number; a previous `SIG_DFL`/`SIG_IGN` is
put back with `sigaction` and the handler returns, so the instruction faults again and the default
action happens (core, exit status 139). A signal that was sent (`si_code <= 0` on Linux) does not
recur, so it is sent again with `raise` after `sigaction` (LESSONS 46). On macOS `si_code` cannot
tell, so `raise` always follows (LESSONS 57; Changes, step 3). Windows returns
`EXCEPTION_CONTINUE_SEARCH`. A PC in our code
that is not a fault point is **not** ours (a JIT bug crashes, as in Wasmtime, rather than becoming a
trap).

## Semantics

### The waboros runtime's handler
* **Machine code emitted by the runtime** (like the PoC), one per process, in its own sealed code
  area, from a small per-architecture emitter and a per-(OS, architecture) offset table: `si_code`,
  `si_addr`, the PC in `ucontext`/`CONTEXT`. The `ucontext` is the kernel's frame (Linux: the same
  for glibc, musl and bionic); the `struct sigaction` used to install is the libc's (a layout per
  libc; glibc's and musl's are the same on x86-64 and aarch64). The handler reads only the registry and the context, calls nothing but the previous handler
  or libc's `sigaction` (async-signal-safe; never a raw syscall, which would bypass Android's
  libsigchain and needs per-OS numbers), takes no lock, allocates nothing. It starts with `endbr64`
  (x86-64) or `bti c` (aarch64; measured: without it the kernel's entry raises SIGILL under BTI).
  Chaining jumps through `x16` on aarch64 (a `bti c` target accepts `br x16`). The PoC's handler,
  without the registry walk, was 119 bytes.
* **Trade-off of the alternative**, a ctypes `CFUNCTYPE` handler: no machine code to emit and no
  offsets to hard-code (the `ucontext` read through ctypes structures), but it runs Python inside a
  signal: it takes the GIL (a deadlock when the fault interrupts a thread holding a lock the GIL
  holder waits for, e.g. inside `malloc` in a foreign library) and is not async-signal-safe at all.
  A foreign fault, which we must pass on untouched, is exactly the case where that matters.
  Rejected; the emitted handler is small and testable by deliberate faults.
* **Registry**: a process-wide table the handler reads: code areas `{lo, hi, map, count}` and
  reservations `{lo, hi}`, in memory that never moves (a full table is replaced by a larger copy
  through one pointer word; the old one is never freed). Writers hold a `threading.Lock`; an entry is
  written with its `hi` last, and removed (zeroed) before its code area or reservation is unmapped, so
  a reused address is never misclassified. A reader that sees a half-written entry treats the fault
  as foreign, which can only happen to a thread running code it was not synchronised with.
* **Installation**: lazily, once per process, under the install lock: under `auto` at the first
  compile that would give guard code (`auto` needs to know that the handler installed before it
  chooses the mode), under `guard` at the first guard-mode instantiation:
  `sigaction(SIGSEGV)` (and `SIGBUS` on macOS/BSD) with `SA_SIGINFO | SA_ONSTACK`, the previous
  action saved for chaining. No `SA_NODEFER`: a fault inside the handler kills the process. Never
  uninstalled. Windows: `AddVectoredExceptionHandler(1, h)`.
* **Is it still ours?** Before every entry from the host (`Instance.call`, outermost and nested) and
  after every host call returns to guard code, the runtime checks that the current disposition is
  still our handler (`sigaction(sig, NULL, &cur)`). Windows: only the holder of the handle can remove
  our entry from the vectored list, so the check never raises; instead, when faulthandler has been
  enabled since the last check (and so went in front of ours), it adds ours in front again and then
  removes the old entry (option A, Changes; other handlers added in front still run first: RISKS R6,
  LESSONS 59). On POSIX, if it is not ours, the call raises an error
  instead of running guard code (Q1); re-installing on top is wrong (measured: a loop with
  `faulthandler`). Cost: one ctypes call of ~580 ns against a ~4.1 µs entry (Q2).
* **Threads and free-threaded builds**: the handler runs on the faulting thread and uses no
  per-thread state, so any thread and any number of stores work alike; no GIL is involved; the
  registry lock is a plain lock. `sigaltstack` is not needed: our faults happen in wasm code within
  the native-stack budget (S0003), and `SA_ONSTACK` uses an alternate stack where one exists
  (CPython's `faulthandler` installs one).
* **Several waboros copies or sub-interpreters** in one process each install their own handler,
  chained; the "still ours" check of the first then fails (Q8).
* **macOS**: signals, not Mach ports (Q5). PROT_NONE faults arrive as SIGBUS (and SIGSEGV for
  unmapped addresses): both are installed. The handler's code area is `MAP_JIT`; it executes on the
  faulting thread, which is in execute mode except during our own short `write_toggled` copies, where
  no fault can occur. A crash reporter on a Mach exception port sees our faults first; one that
  terminates instead of returning `KERN_FAILURE` defeats guard mode (documented; `explicit` remains).
  lldb stops on `EXC_BAD_ACCESS` (`process handle` to pass it).
* **Windows**: a vectored handler (first in the list), `CONTEXT.Rip`/`Pc`; no unwind tables are
  needed because we resume in the same frame (R3's note on `RtlAddFunctionTable` stays as is). Under
  hardware-enforced stack protection with context-IP validation, the resume addresses must be
  registered with `SetProcessDynamicEHContinuationTargets` (the fault map's resume offsets are exactly
  that list), or the process ends with 0xC0000409 (CI run 41633456821); the runtime registers them
  where the policy asks for it and refuses guard mode where it cannot (Changes, step 4).
* **Android**: in app processes ART's libsigchain interposes `sigaction`: by its design our handler
  becomes a user handler, called after ART's own (implicit null checks, its stack overflow). Using libc's
  `sigaction` symbol through ctypes is what keeps this working. Checked in the CI `android` job.
* **Partial stores**: Arm does not promise that a misaligned store crossing into a faulting page
  writes nothing (nor does RISC-V; Wasmtime issue 7237), and QEMU's TCG splits 16-byte x86 stores
  (LESSONS 50). On x86-64 this is accepted as in Wasmtime (D45: native faults are precise, emulators
  may tear; CI's qemu64 steps run in explicit mode); aarch64 the same (step 2, D45).

### Selection and fallback
* The mode is a compile-time property of the code (D29). Before phase 6 it is chosen by a JIT option
  `bounds = "explicit" | "guard" | "auto"` (environment `WABOROS_JIT_BOUNDS` for tests and CI;
  S0002's interface is unchanged; an empty value counts as unset). **`"auto"` is the default**
  (D44, which superseded D43's opt-in). `"auto"` gives `guard` when all hold: the (OS,
  architecture, libc) row is **verified** in the table below; the process is of the target's
  architecture (a cross-compile gets `explicit`); the handler is installed (or installs) and is
  ours; the embedding has not forbidden handlers. Otherwise `explicit`, **silently** (Q6): it is a
  full mode, not a degraded target, so D31's warning does not apply. `"guard"` where it cannot work
  is an error, never a crash. `"explicit"` installs nothing and is the opt-out.
* The memory reservation (4 GiB + GUARD) is made in **both** modes (Memory reservation above), so a
  failed reservation fails an `explicit` module the same way: it is not a reason to fall back.
* **Verified** means a CI job on that platform runs the fault tests and the spec suite in guard
  mode. Until a row is verified, `auto` there means `explicit`, and `explicit` is the only mode of a
  new architecture (D29). The table (each step flips its rows):

| OS / libc | x86-64 | aarch64 | CI |
|---|---|---|---|
| Linux glibc | **verified** (D44) | **verified** (step 2; the native runner confirms) | `jit` (aarch64 under QEMU), `arm64` native runner |
| Linux musl | **verified** (musl step) | explicit: `jit-aarch64` needs libgcc's `__clear_cache`, absent from the stock image (Changes) | `jit` (`python:3.13-alpine` container) |
| macOS | **verified** (step 3; CI run 41610392553) | **verified** (step 3; CI run 41610392553) | `platforms.yml` (`macOS x86_64`, `macOS arm64`) |
| Windows | **verified** (step 4; CI run 41640149866) | **verified** (step 4; CI run 41633456821) | `platforms.yml` (`Windows x64`, `Windows ARM64`) |
| Android (bionic, libsigchain) | step 5 | explicit (documented limitation) | `android` (x86_64 emulator) |

The BSDs are not a target (TARGETS.md), so they have no row.

* **Artifacts**: a host that cannot run guard code refuses a `BOUNDS_GUARD` artifact as it refuses
  an ISA level (D33, D42): a thin file is refused naming the reason; the cache and fat files are
  regenerated from the wasm as `explicit` (D18). A loaded artifact's mode and `GUARD_LOG2` must
  match the host's reservation; a smaller host guard is a refusal.
* A store holds guard and explicit modules side by side (memory layout is the same); the mode is per
  module.

### Never crash the interpreter
A fault that is not ours reaches the previous handler or the default action exactly as before
(measured with `faulthandler`: same traceback, same 139). Guard code never runs without our handler
current (the entry and host-return checks). A JIT bug that faults outside a fault point or outside a
reservation is a crash, not a trap: the explicit-mode tests and differential runs catch those.

## Test plan
* `uv run python -m tests.spec.run --engine jit-x86_64` with `WABOROS_JIT_BOUNDS=guard` and
  `=explicit`, then `uv run python tools/spec_diff.py pyc <each>`: no difference (D25), both modes
  every claimed directory (`simd/`, `multi-memory/` included). The same for `jit-aarch64` under QEMU
  (`tools/run_arm64.py`, `--in-process`; CI `jit`) and on the native arm64 runner (CI `arm64`,
  Linux). CI runs QEMU's SIMD pytest step in explicit mode: QEMU splits a straddling 16-byte store.
* `tests/jit/test_guard.py` (both architectures; QEMU for aarch64): every load/store width and
  signedness and every SIMD memory operator trapping at `size - n + 1 … size - 1` (straddling),
  `size`, `0xFFFFFFFF + off`, and at `off = GUARD - n` (unchecked) and `GUARD - n + 1` (checked);
  constant addresses; memory *i* > 0 and imported memories; before and after `memory.grow` (newly
  committed pages work, beyond them traps); the instance is usable after each trap; a trap at
  recursion depth 10 000; a trap in an inner entry (wasm → host → wasm) leaves the outer call
  running; 8 threads with one store each trapping in a loop; a fault map equal to the instructions
  that touch memory (disassembled with `objdump`).
* Foreign faults, in subprocesses: `ctypes.string_at(16)` with our handler installed exits with
  `-SIGSEGV`, with and without `faulthandler` enabled before us (traceback printed in the second
  case); `os.kill(os.getpid(), SIGSEGV)` is not ours; `faulthandler.enable()` or `.disable()` after
  our install makes the next entry raise, never crash.
* `uv run python tools/jit_dump.py --target jit-aarch64` on x86-64 and on arm64: identical lists
  with the fault map in the digest (D35); `tools/isa_all.py` unchanged in both modes.
* `uv run python tools/run_coremark_jit.py poc/in/core.wasm` alternated in both modes, both
  architectures, plus the SIMD kernels of S0005: rows in BENCHMARKS.md with conditions.
* The C reference loader (D36), once it exists, implements this handler in C from this text alone and
  runs CoreMark and the fault tests on a guard artifact.

## Plan of steps
1. **Linux x86-64 (glibc)**: reservation 4 GiB + 32 MiB; `bounds` option; fault points and the
   fault map in the x86-64 backend; the registry, the emitted handler, installation, the
   "still ours" checks; `test_guard.py`. Acceptance: spec parity in both modes; `test_guard.py` and the
   foreign-fault tests pass; CoreMark passes its self-check in both modes, BENCHMARKS row added;
   `auto` still `explicit` until the review is done, then the row is verified.
2. **Linux aarch64**: the same in the aarch64 backend (far-mode rewind drops entries) and an aarch64
   handler. Acceptance: QEMU and native-runner spec parity; `jit_dump` lists identical across hosts.
2b. **Linux musl** (added, maintainer 2026-11-02): musl named by its mapped image; x86-64 verified
   in an Alpine container in CI; aarch64 explicit while stock Alpine arm64 has no `__clear_cache`
   (S0004). Acceptance: the container runs the spec suite and `test_guard.py` in guard mode.
3. **macOS x86-64 and arm64**: SIGBUS, Darwin offsets, `MAP_JIT`. Acceptance: the CI jobs run
   `test_guard.py` and the spec suite in guard mode.
4. **Windows x86-64 and arm64** (arm64 joined, maintainer 2026-11-02; done and verified 2026-11-03): the vectored handler; the EH-continuation question answered by a test that forces user
   shadow stacks and context-IP validation on a child python.exe (which is not CETCOMPAT itself).
   Acceptance: the `os (windows-latest)` and `arm64 (windows-11-arm)` jobs as in 3.
5. **Android x86_64**: the Android cell of `platforms.yml` runs `test_guard.py`; logcat shows no ART complaint.
   The cell also takes over what `tools/android_check.py` checked in the former `android` job (the JIT
   works in the app process and `auto` selects it; CoreMark passes its self-check there), and the
   script is then deleted (maintainer, 2026-11-03).
6. **Artifact** (moved to the artifact-format spec of phase 5, see Changes): the flag bits and `TRAPMAP` in the serialized format when phase 5 fixes it;
   ARTIFACT.md parts 2–3, DECISIONS (D29 amended), a test that a guard artifact is refused where
   guard mode is unavailable and regenerated when fat; the C loader's handler (D36).
7. **Review** by a `reviewer` (handler, fault points, registry), SIMD rows, RISKS R6 status.

## Alternatives considered
* **Mach exception ports on macOS** (Wasmtime's default): coexists with crash reporters that use
  ports, but needs a helper thread, `mach_msg`/`thread_set_state` through ctypes and a global code
  lookup; more moving parts than the signal path. Possible later behind its own row.
* **`userfaultfd`** (Linux only): reports missing pages of registered ranges to a thread; it cannot
  redirect the faulting thread to a stub and does not report `PROT_NONE` faults. Not applicable.
* **Unwinding to the entry** (`SAVED_SP`, the PoC's model and Wasmtime's: set PC, SP and FP to the
  entry's catch point): needs a saved stack pointer per nested entry (LESSONS 4) and breaks CET
  shadow stacks; resuming at the function's own stub needs neither.
* **Bounds-check elimination without guards**: one check per group of accesses with the same index
  and different offsets, or hoisted out of loops. Stays portable and OS-free, but needs an analysis
  in the one-pass core (D26) and removes at most part of the measured 8 % / 20 % code. Worth measuring
  for `explicit` mode later; it does not replace this.
* **Masking** the index into a power-of-two reservation: no trap where wasm requires one (wrong
  semantics); only usable as a Spectre hardening next to a check.
* **A precompiled C helper**: a native dependency (D1). Rejected.

## Open questions
* **Q1. The handler was replaced after instantiation** (`faulthandler.enable()`, another library).
  (a) the entry raises an error naming the cause; (b) re-install ours on top (measured: a loop);
  (c) recompile the store's modules `explicit` and swap code under live function records (large).
  **Recommendation: (a).** It refines D29's "otherwise compiles the module explicit", which then
  applies only before instantiation.
* **Q2. How often to check.** (a) every entry and after every host call (~580 ns each, measured);
  (b) outermost entries only; (c) a host-side native pre-entry stub calling `sigaction` (one ctypes
  call; cheaper, more code). **Recommendation: (a) in step 1, measured; (c) only if the cost shows.**
* **Q3. Which addresses are ours.** (a) any reservation of the process (registry); (b) only the
  current store's (needs TLS from machine code or reading the context register out of `ucontext`, one
  more per-OS offset); (c) PC only, as Wasmtime. **Recommendation: (a)** — this relaxes the brief's
  "guard region of the current store".
* **Q4. Fault table placement.** (a) in `TRAPMAP` before the frames table; (b) a new section.
  **Recommendation: (a)**: both map code offsets, and the container question maps it to one ELF section.
* **Q5. macOS: signals or Mach ports.** **Recommendation: signals**; Mach ports only if a user's
  crash reporter breaks it.
* **Q6. Warning when `auto` picks `explicit`.** (a) none; (b) once, like D31. **Recommendation: (a)**,
  with the chosen mode visible on the compiled module.
* **Q7. `GUARD`.** (a) 32 MiB fixed in v1, recorded in `GUARD_LOG2`; (b) configurable now.
  **Recommendation: (a)**; `memory_guard_size` comes with the phase-6 `Config`.
* **Q8. Several waboros copies or sub-interpreters.** (a) unsupported: the second makes the first's
  check fail, so the first raises (Q1); (b) the handler area starts with a tag, a layout version and
  the registry address, so a later copy joins the existing registry instead of installing.
  **Recommendation: (a) in step 1, (b) when someone needs it.**
* **Q9. The 32-bit index copy on x86-64** (`mov ecx, r32` before the access) could go, relying on the
  zero-extension invariant. Measured on 3 runs: no gain (16 173 / 14 998 / 16 105 vs 15 723 / 15 922
  / 16 819 it/s). **Recommendation: keep it.**

## Changes
* 2026-11-02: created (draft).
* 2026-11-02: approved by the maintainer with every recommendation (Q1–Q9), and with guard pages
  **opt-in**: `bounds = "explicit"` stays the default; `"guard"` and `"auto"` are chosen explicitly
  (D43, which supersedes D29's "default where verified"). The measured gain here is +7–9 % on
  CoreMark (not the +20–25 % of the older PoC row); making guard the default needs a measurement on
  several machines and a decision.
* 2026-11-02: Q1 and Q8 revised by the maintainer after checking Wasmtime (source at main and
  measured: each copy of libwasmtime installs its own handler, saves the previous one, claims a fault
  only when this thread is inside its own wasm and the PC is on one of its own trap sites, and chains
  everything else; two copies in one process work in either order, nested too; it never re-checks
  its handler). **Q8**: several waboros copies (sub-interpreters, vendored copies) are supported by
  the same chain-and-claim rule per copy: no process-wide registry. **Q1**: the check before entering
  wasm raises an error only for the cases that break the chain and that are cheap to recognise from
  Python: SIGSEGV/SIGBUS reset to `SIG_DFL`/`SIG_IGN`, `faulthandler` enabled after ours, or a
  Python-level handler on those signals; any other handler is assumed to chain (as in Wasmtime).
* 2026-11-02: step 1 built (Linux glibc x86-64), with the Q1/Q8 revision. As built:
  `WABOROS_JIT_BOUNDS=explicit|guard|auto` (`common/guard.py`), read by `JitTarget.compile`;
  `guard` on a target without the mode (`jit-aarch64` until step 2) is a `ValueError` at compile,
  on a host without a handler a `RuntimeError` at instantiation; `auto` is `explicit` (no row in
  `verified` yet). The backend takes `guard=True`; `_bounds` hands the access to `_guarded` under
  the thresholds above and each access site marks its one memory instruction (`_fault_point`);
  `Compiled.flags` (`1 | 25 << 8`) and `Compiled.faults` stand for the header bits and `TRAPMAP`
  until step 6, and `tools/jit_dump.py` hashes them only when the flags are set, so the explicit
  lists are unchanged line for line. The handler (`x86_64/abi.py` `fault_handler`) is 324 bytes
  (rough PoC: 119), emitted at the first guard instantiation. Deviations, each for the contract's
  "exactly as if the handler were absent": (1) when the saved action is SIG_DFL/SIG_IGN and the
  signal was sent (`si_code <= 0`), the handler also calls libc's `raise(sig)` after `sigaction`,
  because a sent signal does not recur on return (measured: `os.kill(getpid(), SIGSEGV)` was
  survived; LESSONS 46); (2) the registry is per copy of the package (module state), the handler
  embeds its copy's tables, and "ours" is the PC on a fault point of that copy's code with the
  address in that copy's reservations: no per-thread state, since a PC in a copy's code can only
  be that copy's wasm; (3) Linux installs SIGSEGV only. The Q1 check (`guard.broken`, before every
  entry and after every host call that returns success into a store with guard code) raises for
  SIG_DFL/SIG_IGN, a Python-level handler set after ours (a different object from
  `signal.getsignal` at our install), or `faulthandler` enabled while another handler is above ours
  (its handler's address cannot be told from a foreign one, so this errs on the side of a false
  error: e.g. faulthandler enabled, then a chaining foreign handler on top of ours; the step-1
  review found that remembering the handler under ours as faulthandler's let faulthandler, re-enabled
  on top, take a guard fault and end the process with 139). Every memory is reserved
  4 GiB + 32 MiB in either mode; a memory is registered when a guard instance binds it.
* 2026-11-02: the maintainer approved two decisions. (1) **The re-send of a sent signal** (step 1's
  deviation): when the saved action is SIG_DFL/SIG_IGN and `si_code <= 0` (a signal sent with
  `kill`/`raise`, not a fault), the handler restores the action with `sigaction` and calls libc's
  `raise(sig)`, which is async-signal-safe; the signal stays pending until the handler returns, so
  the process ends exactly as it would without us. This deliberately differs from Wasmtime
  (`delegate_signal_to_previous_handler`, `crates/wasmtime/src/runtime/vm/sys/unix/signals.rs`,
  read 2026-11-02), which restores the action and returns: a real fault then repeats and kills the
  process, but a sent SIGSEGV is survived and Wasmtime's own handler stays replaced by SIG_DFL, so
  its next wasm trap kills the process. The handler may therefore call exactly three things: the
  previous handler, `sigaction` and `raise` (LESSONS 46). (2) **`bounds = "auto"` becomes the
  default** (D44, superseding D43) once step 1's review is closed and a measurement on a quiet
  machine confirms the gain; `auto` gives `guard` only on rows verified in CI (Linux glibc x86-64
  first) and `explicit` everywhere else, silently; `bounds = "explicit"` is the opt-out.
* 2026-11-02: **`auto` is the default** (D44), after step 1's review and a measurement on a quiet
  machine (CoreMark +9.35 % median over 9 alternated pairs; BENCHMARKS.md, "Guard pages").
  * The row Linux glibc x86-64 is **verified** (`verified` of `jit-x86_64`); every other row stays
    `explicit`.
  * **glibc detection**: the row is `linux-` plus `platform.libc_ver()[0]`. That function first asks
    glibc's `confstr(_CS_GNU_LIBC_VERSION)`, which musl lacks, so musl (and anything unnamed) gets
    another row and stays `explicit`.
  * `WABOROS_JIT_BOUNDS=` (empty) counts as unset. Any failure to install the handler under `auto`
    (`sigaction`, executable memory) gives `explicit` silently.
  * **Architecture check**: `install` refuses a process that is not of the target's architecture
    (`platform.machine()` among the target's machines, 64-bit). Without it, `auto` in an arm64
    Linux process cross-compiling `jit-x86_64` (`tools/jit_dump.py`, D35) would have installed x86
    machine code as the process's SIGSEGV handler; under `guard` the same mistake waited at
    instantiation (LESSONS 48).
  * **Q1, rule A** (the maintainer): when the current SIGSEGV handler is not ours, `dladdr` names
    the object that holds it. Inside the interpreter's own image (the image of `Py_IncRef`, or
    `PyPy_IncRef`: the executable or libpython/libpypy, where faulthandler and the `signal`
    module's handler live) the chain is broken and the Q1 error is raised. In any other shared
    object (wasmtime), or in anonymous memory (another waboros copy's emitted handler, another
    JIT's), it is assumed to chain, as Wasmtime assumes. SIG_DFL/SIG_IGN and a Python-level
    handler set after ours stay errors. So does faulthandler enabled now but not when ours was
    installed, with any foreign handler on top: faulthandler then sits between that handler and
    ours, and a library's handler chaining to it ends the process on a guard fault (found by the
    step's review: ours, `faulthandler.enable()`, then a wasmtime store gave 139; now the Q1 error).
    Whether faulthandler was enabled is recorded at our install. Where `dladdr` or the interpreter's symbol is missing, the
    step-1 rule stays (faulthandler enabled under any foreign handler is a break). The step-1 rule
    made the default unusable under pytest with wasmtime in the process: pytest enables
    faulthandler, wasmtime installs its handler above ours, and every guard call raised although
    wasmtime chains to us (LESSONS 47).
  * **Residual case, not detected**: faulthandler that was already enabled when ours was installed
    (pytest), then disabled and re-enabled while a library's handler is above ours. The library's
    handler then chains to faulthandler, which re-raises a guard fault instead of chaining, so the
    process ends (139). RISKS R6.
  * CI: `jit` keeps one run of the spec suite and `test_guard.py` with `WABOROS_JIT_BOUNDS=guard`;
    `test` (3.13) runs pytest and `spec` runs the jit-x86_64 spec suite with
    `WABOROS_JIT_BOUNDS=explicit`; the D35 lists of `jit-x86_64` are made with the mode named, in
    both modes, on both hosts.
* 2026-11-02: **stores stay fault points in guard mode; a split store may tear (D45)**. After the
  D44 switch, CI's `jit` job failed `test_simd_abi.py::test_v128_matches_pyc` in the step that runs
  the SIMD tests under `qemu-x86_64 -cpu qemu64,-pni,-ssse3,-sse4.1,-sse4.2,-popcnt` (QEMU 8.2.2,
  TCG): a `v128.store` at 65 521 of a 64 KiB memory trapped, but its first 8 bytes had landed,
  because TCG splits the 16-byte `movdqu` into two 8-byte stores (LESSONS 50). Natively it passes:
  x86-64 faults are precise. A first fix checked every store of 2 bytes or more explicitly in guard
  mode (CoreMark fault points 669 → 493, code 36 058 → 38 457 bytes, CoreMark −2 to −3 %; guard's
  lead over explicit +4.5 to +6.3 % instead of about +9 %; BENCHMARKS.md, "Checked stores in guard
  mode (rejected alternative)"); a probe (a 1-byte read of the store's last byte as the fault point)
  was no faster. The maintainer rejected both and chose Wasmtime's behaviour (D45): stores rely on
  guard pages, tearing under emulators (QEMU TCG, possibly box64 or FEX) and on Arm or RISC-V is
  accepted, and the code generation rule above is unchanged. CI's qemu64 pytest and spec steps run
  with `WABOROS_JIT_BOUNDS=explicit`. aarch64 is decided in step 2.
* 2026-11-02: maintainer: step 6 (the guard flag and `TRAPMAP` in the serialized artifact, the C
  loader's handler) moves to the artifact-format spec of phase 5, done with the container (ELF or
  other, ARTIFACT-CONTAINER.md) and the C loader; this spec closes with steps 2–5 and 7.
* 2026-11-02: **step 2 built (Linux glibc aarch64)**. As built:
  * **Backend.** `Backend(guard=True)` in `aarch64/backend.py`. `_bounds` forms the address as in
    explicit mode (`move(Reg(9), addr)`, then `add x9, x9, #offset`; a constant as an immediate
    offset or through x9). Under the thresholds it drops the `cmp`/`b.cond` and records the stub.
    Each scalar and SIMD access site (`backend.py`, `simd.py`) marks its one memory instruction with
    `_fault_point`. `rewind` drops the fault points at or after the rewound position, so a function
    compiled again in far mode keeps each access once. Explicit code is unchanged: the D35 lists of
    `jit-aarch64` in explicit mode equal HEAD's.
  * **Handler.** `aarch64/abi.py` `fault_handler`, 304 bytes; the contract of the x86-64 one. It
    uses the kernel's offsets: `si_code` 8, `si_addr` 16, `uc_mcontext` at 176, `pc` at 440. It
    starts with `bti c`, writes only x9–x17 and x29 (and x0–x2 before its calls), and never writes
    x18. It chains by `br x16` with x0–x2, sp and x30 untouched. For SIG_DFL/SIG_IGN it calls
    `sigaction` and `raise` under a frame record, with sp 16-aligned.
  * **Rows.** `guard_rows` and `verified` of `jit-aarch64` are `{"linux-glibc"}`, so `auto` gives
    guard in an aarch64 Linux glibc process, QEMU user mode included. `common/guard.py` needed one
    fix: `install` now checks the architecture before "already installed". Before, a process whose
    own handler was installed answered `auto` with guard code for the other architecture's target
    when cross-compiling (LESSONS 48 again; a test covers it).
  * **Stores (D45 for aarch64).** Stores rely on guard pages, as on x86-64. Measured under QEMU
    8.2.2 (TCG, BTI enforced): a straddling 16-byte `str q` at 65 521..65 528 of a 64 KiB memory
    wrote its first 8 bytes before trapping. The 8-byte scalar and lane stores wrote nothing, so
    the "nothing written" row of the table above holds only for widths up to 8. On aarch64,
    `test_a_straddling_store_writes_nothing` records a torn store as a warning instead of failing,
    and still checks that only the store's own bytes landed. CI's QEMU SIMD step runs in explicit
    mode; the native runner shows what the hardware does.
  * **Tests.** `test_guard.py` runs on the JIT of the machine. A far-mode case checks that a
    function compiled again keeps each fault point once, and that a fault in far code still
    resumes. `test_compiler_aarch64`'s far tests pin explicit mode, because their module sizes
    need far mode only with checks.
  * **Results.** Spec under QEMU: 54 385 pass, 0 fail, 91 expected failures in both modes. The D35
    guard list from QEMU on arm64 equals the x86-64 host's. `isa_all --target jit-aarch64` is clean
    in guard mode. CoreMark under QEMU passes its self-check in both modes; code 37 372 → 29 284
    bytes (−21.6 %), 669 fault points (BENCHMARKS.md).
* 2026-11-02: CI's native `ubuntu-24.04-arm` runner, guard mode: `test_a_straddling_store_writes_nothing`
  passed with no torn store recorded (the 16-byte store included), the spec suite and `test_guard.py`
  passed; so that hardware faults before writing, unlike QEMU (LESSONS 54). Arm still allows a tear
  (D45); other cores may differ.
* 2026-11-02: **Linux musl** (its own step, the maintainer's platform decisions of 2026-11-02). As
  built:
  * **Detection.** `host_row()` takes only `glibc` from `platform.libc_ver()`. Without glibc's
    `confstr` that function scans the executable's bytes and may answer `libc`. musl is named
    positively by its mapped image: a `/proc/self/maps` path whose file name starts with
    `ld-musl-` or `libc.musl-` (musl's libc is its dynamic linker). Anything else, a statically
    linked musl interpreter included, is `linux-unknown` and stays explicit. A test feeds recorded
    maps, so it runs on any host.
  * **Checked against musl 1.2.6** (Debian's `musl_1.2.6.orig.tar.gz`; Alpine 3.24's
    `python:3.13-alpine` runs 1.2.6). `struct sigaction` compiled with musl's headers on x86-64:
    152 bytes, `sa_handler` 0, `sa_mask` 8 (128 bytes), `sa_flags` 136, `sa_restorer` 144, as
    glibc's and our `_Sigaction`. aarch64's `bits/signal.h` gives the same generic layout.
    `src/signal/sigaction.c`: the old action gets `sa_handler`, `sa_flags` and the mask, not
    `sa_restorer`. A new one always gets musl's own restorer, so re-installing the saved action
    (the SIG_DFL/SIG_IGN path) works. SIGSEGV takes no lock (only SIGABRT does), and the call is
    a plain `rt_sigaction` after a one-time unmask. `raise` blocks the application signals, calls
    `tkill` and restores the mask. Inside our handler SIGSEGV stays blocked, so the re-sent
    signal waits until the handler returns, as with glibc (LESSONS 46). `sigaction`, `raise`,
    `dladdr`, `sigaltstack` and `Py_IncRef` resolve through `ctypes.CDLL(None)`, so rule A works
    unchanged. The handlers are unchanged (a comment in `x86_64/abi.py`).
  * **Stack.** Nothing changes: S0003 already handles musl's stacks, and `tools/stack_info.py`
    runs in the musl container. The reservations are ordinary `mmap`s placed below the kernel's
    mmap base, which leaves the main stack its RLIMIT_STACK gap, and musl thread stacks are
    fixed mappings. In the container the spec suite and `test_guard.py`, with its fault at
    recursion depth 10 000 and its 8 threads, pass in guard mode.
  * **Rows.** `linux-musl` is in `guard_rows` and `verified` of `jit-x86_64`. Results in
    `python:3.13-alpine` (x86-64, Alpine 3.24.3, CPython 3.13.18): the row is detected and `auto`
    gives guard. The spec suite in guard mode gives 54 385 pass, 0 fail, 91 expected failures,
    as on glibc. `test_guard.py` in guard mode with `WABOROS_JIT_REQUIRE=1`: 40 passed, 2 skipped
    (aarch64's far mode). CI `jit` runs these checks in that container (`MUSL_GUARD` in
    `ci.yml`).
  * **aarch64 musl stays explicit.** The stock `python:3.13-alpine` arm64 image has no libgcc.
    `jit-aarch64` takes its instruction-cache flush from libgcc's `__clear_cache` (S0004), so
    there its probe fails ("no instruction-cache flush function") and `auto` gives `pyc` with
    the D31 warning. With libgcc installed (`apk add libgcc`; wasmtime's musllinux wheel needs it
    too), the same image under QEMU 8.2.2 detects `linux-musl`. With the row enabled for that
    run, the spec suite in guard mode passed (54 385 pass, 0 fail, 91 expected failures).
    `test_guard.py` gave 21 passed and 21 skipped: the subprocess cases need binfmt, and the
    16-byte straddling store tore under TCG, as on glibc (LESSONS 54). The row waits for an
    instruction-cache flush that does not depend on libgcc (S0004 Alternatives, "own I-cache
    routine"), then a CI step on the `arm64` runner.
  * **wasmtime** has musllinux wheels (50.0.1, x86-64 and aarch64), and so does
    `wasm-tools-package`. No test is skipped on musl. The wasmtime wheel's `_libwasmtime.so`
    needs `libgcc_s.so.1`, which the CI step installs (LESSONS 56).
  * **The maintainer's platform decisions (2026-11-02).** Linux musl is its own step (this one).
    Windows arm64 is done together with step 4 (Windows x64). Android arm64 stays explicit, a
    documented limitation: CI has no arm64 emulator, bionic's `struct sigaction` differs, and
    libsigchain sits in between. The BSDs are not a target (TARGETS.md, cibuildwheel's map), so
    they have no row.
* 2026-11-03: **step 3 built (macOS x86-64 and arm64); verification came with the CI run below.** No macOS machine
  was available: every constant comes from Apple's sources (`apple-oss-distributions` on GitHub, `main`,
  read 2026-11-03), is mirrored field by field as ctypes structures in
  `tests/jit/test_fault_handler.py`, and the macOS behaviour itself is unverified until the CI steps
  run. As built:
  * **Constants** (LP64): SIGSEGV 11, SIGBUS 10; `SA_ONSTACK` 0x1, `SA_SIGINFO` 0x40 (`SA_NODEFER`
    0x10, not used) (xnu `bsd/sys/signal.h`). `siginfo_t`: `si_code` at 8, `si_addr` at 24 (after
    `si_pid`, `si_uid`, `si_status`) (`bsd/sys/signal.h`). `ucontext_t`: `uc_onstack`, `uc_sigmask`
    (u32), `uc_stack` (24 bytes), `uc_link`, `uc_mcsize`, then `uc_mcontext` at 48, a **pointer**;
    sigreturn reads the mcontext back through it (`bsd/sys/_types/_ucontext.h`,
    `bsd/dev/{arm,i386}/unix_signal.c`). `__darwin_mcontext64`: `__es` (16 bytes) then `__ss`. On
    x86-64 `__es` is {trapno, cpu, err, `__faultvaddr` at 8} and `__ss.__rip` is at 16 + 128 = 144
    (`bsd/i386/_mcontext.h`, `osfmk/mach/i386/_structs.h`). On arm64 `__es` is {`__far` at 0, esr,
    exception} and `__ss.__pc` is at 16 + 256 = 272, after x0-x28, fp, lr, sp (`bsd/arm/_mcontext.h`,
    `osfmk/mach/arm/_structs.h`). `si_addr` is `__faultvaddr` / `__far` for SIGSEGV and SIGBUS.
    libc's `struct sigaction` is {`sa_handler`, u32 `sa_mask`, int `sa_flags`}, 16 bytes, with no
    restorer (xnu `bsd/sys/signal.h`). libplatform's `__platform_sigaction`
    (`src/setjmp/generic/sigaction.c`) adds `_sigtramp` itself, so re-installing a saved action
    works. A PROT_NONE page faults with `KERN_PROTECTION_FAILURE`, which becomes SIGBUS; an unmapped
    one gets `KERN_INVALID_ADDRESS`, which becomes SIGSEGV (`bsd/uxkern/ux_exception.c`). So both
    signals are installed, with one saved action each.
  * **Handlers.** `fault_handler(..., darwin=True)` in both `abi.py`: the PC behind the
    `uc_mcontext` pointer. The saved action is chosen by the signal: SIGSEGV's, then SIGBUS's, at
    `prev + 16`. x86-64 still writes only rax, rcx and r8-r11; aarch64 only x9-x17 and x29 (x15 holds
    the chosen action), never x18. The Linux bytes are unchanged (the same sha256 as before for both
    architectures). The handlers are 296 bytes (x86-64) and 308 bytes (aarch64), for small immediates.
  * **`si_code` on Darwin** (LESSONS 57): xnu's `sendsig` fills it by signal number, a sent signal
    included. On arm64 every SIGBUS gets `BUS_ADRALN` and every SIGSEGV `SEGV_ACCERR`; on x86-64
    SIGBUS gets `BUS_ADRERR` and SIGSEGV a code from the thread's last exception. So "ours" rests on
    the PC and the address (the `si_code > 0` test stays, as a filter: x86 GP faults have 0). When
    the saved action is SIG_DFL/SIG_IGN, the handler always calls `raise` after `sigaction`. Under
    SIG_DFL a real fault then ends the process from the pending signal before the instruction runs
    again: the same signal and exit status as without us. Under SIG_IGN xnu's `threadsignal` drops
    the signal, so a real foreign fault runs again forever (100 % CPU): xnu's own behaviour without
    us, not a regression (Linux force-unignores a fault signal). `si_addr` of any SIGSEGV/SIGBUS is
    the thread's last page-fault address (`es.faultvaddr`/`es.far`), routinely inside a
    reservation while wasm runs: a `kill -BUS`/`-SEGV` landing while the PC is on a fault point
    becomes a `MEMORY_OUT_OF_BOUNDS` trap and the signal is swallowed. Accepted, as in Wasmtime
    (PC only).
  * **Async-signal safety** (read): libc's `sigaction` is a check plus the `__sigaction` syscall.
    `raise` is `pthread_kill(pthread_self())`, and for the calling thread libpthread takes no lock
    (`_pthread_is_valid`). Neither allocates.
  * **`MAP_JIT`, BTI, PAC.** The handler is loaded like all code, `MAP_JIT` on arm64. It writes only
    the signal context, never JIT memory, so no write toggle is involved. The faulting thread runs
    wasm code, so it is in execute mode. Darwin enforces no BTI on user code, so `bti c` is a
    harmless no-op there. The saved PC is unsigned in an arm64 (not arm64e) process: such threads
    run with user JOP disabled, and `machine_thread_state_convert_from_user`
    (`osfmk/arm64/status.c`) takes the state as is. RISKS R6 has the remaining cases: a thread in
    write mode, Mach exception ports, debuggers.
  * **Wasmtime on macOS** (read, not copied: `crates/wasmtime/src/runtime/vm/sys/unix/machports.rs`
    and `signals.rs`). It uses Mach exception ports by default: a helper thread, adopted from
    SpiderMonkey after a crash reporter (Breakpad) swallowed its faults, issue 2456. Its signal
    fallback installs SIGBUS too and reads `(*uc_mcontext).__ss.__pc`/`__rip`. Q5 stands: we use
    signals; with Mach ports, Wasmtime in-process installs no signal handler, so ours stays on top.
  * **`common/guard.py`**: `SIGNALS` (SIGSEGV; and SIGBUS on macOS), the OS's flags, and its
    `struct sigaction`. `install` saves one action per signal and puts back the ones already
    installed if a later `sigaction` fails. `broken()` checks every signal; on macOS this means two
    `sigaction` queries per check. Rows: `darwin` is in `guard_rows` of both targets; it joined
    `verified` after CI run 41610392553 (below).
  * **Tests.** `test_guard.py` runs on macOS: the fixture requires the row and requires `auto` to
    give guard only where the row is verified. The foreign chaining handler covers every signal we
    take. `test_fault_handler.py` runs anywhere. It checks the offsets against the header mirrors
    and puts both Darwin handlers through llvm-mc: aarch64 byte for byte, x86-64 instruction by
    instruction through GNU objdump. It also calls the machine's handler as a C function with
    fabricated Linux and Darwin contexts and stand-ins for `sigaction`, `raise` and the saved
    handlers, so the Darwin paths run on Linux, natively and under QEMU.
  * **CI**: `os` (`macos-26-intel`) and `arm64` (`macos-latest`) run the spec suite and
    `test_guard.py` with `WABOROS_JIT_BOUNDS=guard` and `WABOROS_JIT_REQUIRE=1`. Once they pass,
    a separate commit adds `darwin` to `verified` (D44's rule; done, below).
* 2026-11-03: **first macOS CI run** (run 41604810683, `macos-26-intel` and `macos-latest`). Guard faults
  work on both: the trap fires as `MEMORY_OUT_OF_BOUNDS`. Three tests failed in the `tests/jit` step,
  all test faults (LESSONS 58), with no change to the handler or to rule A:
  * The musl test read the real `sys.platform` and now fixes it to `linux`.
  * The two "faulthandler enabled after ours" cases ran their child under `auto`, which is explicit
    on the unverified macOS row. They now use `auto` only on a verified row and `guard` otherwise;
    the Linux expectation is unchanged.
  * Simulated on Linux with an empty `verified`, the guard child gets the Q1 error in both cases.
* 2026-11-03: **darwin verified** (CI run 41610392553, fully green: `os (macos-26-intel)` ran the
  `jit-x86_64` spec suite and `test_guard.py` + `test_fault_handler.py` with `WABOROS_JIT_BOUNDS=guard`;
  `arm64 (macos-latest)` the `jit-aarch64` spec suite and the same tests). `darwin` is now in `verified`
  of both targets, so `auto` gives guard pages on macOS, Intel and Apple Silicon. The macOS CI steps
  keep `WABOROS_JIT_BOUNDS=guard` explicit, as the Linux steps do. Step 3 is done; next step 4.
* 2026-11-03: **win32 verified** (CI runs 41633456821 and 41640149866). `arm64 (windows-11-arm)` was
  fully green (the `jit-aarch64` spec suite, `test_guard.py` + `test_fault_handler.py` with
  `WABOROS_JIT_BOUNDS=guard`); `os (windows-latest)` was fully green after the CET fix (the `jit-x86_64`
  spec suite and the same tests, including `test_guard_traps_under_forced_cet_shadow_stacks[in-process]`
  and `[system]` and `test_faulthandler_enabled_after_ours_stays_behind_ours_on_windows`). `win32` is
  now in `verified` of both targets, so `auto` gives guard pages on Windows. Step 4 is done; next step 5.
* 2026-11-03: **step 4 built (Windows x64 and Windows on Arm64).** No Windows machine was available: the
  constants come from the Windows SDK headers as mingw-w64 ships them, the handlers ran off Windows on
  fabricated contexts, and the `win32` row was in `guard_rows` of both targets but not in `verified`
  until CI passed (the entry above). As built:
  * **Constants** (mingw-w64-headers `winnt.h`, `excpt.h`, `winbase.h`, master e3d18511, read
    2026-11-03; every offset also checked with clang's `offsetof` against those headers for
    `x86_64-w64-windows-gnu` and `aarch64-w64-windows-gnu`, with a wrong value as a negative control):
    `EXCEPTION_POINTERS` {`ExceptionRecord` 0, `ContextRecord` 8}; `EXCEPTION_RECORD` {`ExceptionCode`
    0, `NumberParameters` 24, `ExceptionInformation` 32: [0] read 0 / write 1 / execute 8, [1] the
    address}; x64 `CONTEXT.Rip` 0xF8 (after P1Home-P6Home, ContextFlags, MxCsr, six segment words,
    EFlags, Dr0-Dr3, Dr6, Dr7 and the 16 integer registers); `ARM64_NT_CONTEXT.Pc` 0x108 (after ContextFlags, Cpsr,
    X0-X28, Fp, Lr, Sp at 0x100); `STATUS_ACCESS_VIOLATION` 0xC0000005; `EXCEPTION_CONTINUE_EXECUTION`
    -1, `EXCEPTION_CONTINUE_SEARCH` 0. The reservation was already Windows-ready: `VirtualAlloc`
    `MEM_RESERVE` with `PAGE_NOACCESS` for 4 GiB + 32 MiB, `MEM_COMMIT` `PAGE_READWRITE` on grow
    (`common/execmem.py`); nothing changed there.
  * **Handlers.** `fault_handler(..., windows=True)` in both `abi.py` (a keyword added to
    `JitTarget.fault_handler`): `LONG h(EXCEPTION_POINTERS *)`. Ours: an access violation with at
    least two parameters, the PC a fault point of a registered code area, `ExceptionInformation[1]` in
    a registered reservation; then the PC in the context becomes the resume address and it returns -1,
    else 0. No calls (no shadow space, no stack use), no chaining: the OS walks the vectored list.
    x64: the pointer in rcx, kept in rdx; writes only rax, rcx, rdx, r8-r11 (Win64 volatile); 269
    bytes. Arm64: x0 in, x1/x2 take the record and the context, then the Linux path; writes only x0-x2
    and x9-x17, never x18 (the TEB); `bti c` stays (a no-op hint, Windows enforces no BTI); 276
    bytes. The Linux and Darwin bytes are unchanged (same sha256 as before for both architectures and
    both OSes), and so are the D35 lists of both targets in both modes.
  * **Install** (`common/guard.py`): `AddVectoredExceptionHandler(1, h)` once per process, under the
    install lock, after the same row and architecture checks; the handle is kept, never removed.
    `broken()` is None once installed: only the holder of the handle can remove the entry, so Q1 has
    nothing to detect. A handler added in front later could claim or swallow our faults; none that
    does is known, so this is documented (RISKS R6), not detected. faulthandler is such a handler but
    passes the fault on: enabled after ours it prints a false "fatal exception" report for every guard
    fault, the trap still happens (LESSONS 59). No vectored continue handler is added (Wasmtime adds
    one for Go's runtime, whose last continue handler ends the process).
  * **CFG and CET.** The PE headers of python.exe and python3x.dll (python.org 3.13.9 and 3.14.0, x64
    and arm64; uv's python-build-standalone 3.13.18, which the CI runners use) have neither `GUARD_CF`
    nor `IMAGE_DLLCHARACTERISTICS_EX_CET_COMPAT` (the bundled `vcruntime140.dll` has both: the parser's
    control), so a default Python process runs with neither CFG nor user shadow stacks. With CFG on, the
    handler would still be a valid call target: `VirtualProtect` to an executable protection marks the
    area valid by default (Microsoft, "Memory Protection Constants"). Shadow stacks: the resume stays in
    the faulting frame, so no return is skipped; whether context-IP validation accepts a resume address
    in dynamic code without `SetProcessDynamicEHContinuationTargets` is answered by
    `test_guard_traps_under_forced_cet_shadow_stacks`, which starts a child with
    `PROCESS_CREATION_MITIGATION_POLICY2_CET_USER_SHADOW_STACKS_STRICT_MODE` and
    `..._USER_CET_SET_CONTEXT_IP_VALIDATION_ALWAYS_ON` and requires two traps. The child is the base
    interpreter (`sys._base_executable`, the venv's site-packages added by argument): a venv's
    python.exe is a launcher whose second process would not inherit the policy (found by the step's
    review). It skips only where `IsUserCetAvailableInEnvironment(USER_CET_ENVIRONMENT_WIN32_PROCESS
    = 0)` says the machine has no user CET (sysinfoapi.h); with CET, a policy read back as off fails.
  * **Tests.** `test_fault_handler.py`: the Windows offsets against ctypes mirrors of the headers;
    both Windows handlers through llvm-mc (aarch64 byte for byte, x86-64 through GNU objdump), with
    the registers they write; and the machine's handler called on fabricated `EXCEPTION_POINTERS`
    (x86-64 off Windows through a SysV-to-Win64 thunk): ours, an in-page error, too few parameters,
    a PC off a fault point or outside the code, an address outside the reservations, and nothing but
    the PC written. Three injected handler bugs (return value, parameter check, PC offset) each failed
    it. `test_guard.py` runs on Windows: the chain tests are POSIX-only, the foreign-fault test
    expects Windows' own outcome (ctypes' `__except` gives OSError, `os.kill` exits 11) with and without
    us, and the Q1 cases (SIG_DFL, faulthandler enabled after ours, a Python handler) must leave guard
    traps working there.
  * **CI**: the `os` job's macOS steps now run on `windows-latest` too, and the `arm64` job's macOS
    steps on `windows-11-arm`: the spec suite and `test_guard.py` + `test_fault_handler.py` with
    `WABOROS_JIT_BOUNDS=guard` and `WABOROS_JIT_REQUIRE=1`.
  * **Open.** (a) faulthandler enabled after ours: decided below (option A). (b) the resume refused under CET: answered,
    the resume addresses are registered (the 2026-11-03 "first Windows CI run" entry below).
* 2026-11-03: **Windows: our vectored handler goes back in front of faulthandler** (the maintainer's
  option A for open question (a) of step 4). As built (`common/guard.py` `_to_front`, called from
  `broken()`, so before every entry and after every successful host call into a store with guard
  code, where POSIX checks Q1):
  * **Trigger.** faulthandler's state is recorded at install (ours is first then, by
    `AddVectoredExceptionHandler(1, …)`, so nothing more is needed right after install). A check that
    finds faulthandler enabled when it was not before calls `AddVectoredExceptionHandler(1, h)` with the
    same code, then `RemoveVectoredExceptionHandler(old)`, and keeps the new handle, under the install
    lock. Add first, then remove: the list never lacks ours; for a moment it holds it twice, which only
    runs it twice for a foreign fault. A failed add keeps the old entry and the check retries next time.
    Turning faulthandler off is only recorded.
  * **Cost** (reasoned, not measured: no Windows machine here). Each call allocates or frees an entry
    on the process heap and takes the vectored list's lock (ReactOS `sdk/lib/rtl/vectoreh.c`, read
    2026-11-03, as a readable model of ntdll; Windows' own code may differ), plus ~0.3 µs of ctypes each
    (LESSONS 9). The list lock is also taken by every exception dispatch, so doing this on every entry
    (~4 µs) would add a noticeable share and contend with faults on other threads; the trigger makes the
    steady-state cost one `faulthandler.is_enabled()` call.
  * **Not covered.** faulthandler disabled and re-enabled between two checks while it was on at the
    previous one (pytest, `-X faulthandler`): the state reads the same, so it stays in front and the
    false reports return (tested as the residual case). A long JIT call on one thread that makes no host calls does not see faulthandler enabled by another thread meanwhile: its guard faults bring the false reports until its next entry or host-call return. Any other vectored handler
    added in front later (RISKS R6) still runs first.
  * **Tests.** `test_windows_puts_our_handler_back_in_front_when_faulthandler_turns_on` (any host, a
    fake kernel32): the trigger, the add-then-remove order, the kept handle, the retry after a failed
    add; a reversed order fails it. `test_faulthandler_enabled_after_ours_stays_behind_ours_on_windows`
    (Windows CI only): faulthandler enabled before the call and inside a host call, then a guard fault:
    two traps and no "Windows fatal exception" on stderr.
* 2026-11-03: **first Windows CI run** (run 41633456821, the step-4 commit). `arm64 (windows-11-arm)`
  was fully green, the guard-mode spec suite and `test_guard.py` + `test_fault_handler.py` included.
  On `os (windows-latest)` everything in `tests/jit` passed except the forced-CET test, which stopped
  the job before its later guard steps: the runner **has** user CET; the child read its shadow-stack
  policy back as 0x115 (bits 0 EnableUserShadowStack, 2 SetContextIpValidation, 4 strict mode, 8
  CetDynamicApisOutOfProcOnly, the last not asked for), then died with 0xC0000409
  (STATUS_STACK_BUFFER_OVERRUN, CET's fail-fast) and printed no trap. So with context-IP
  validation, Windows refuses to continue at a resume address that is not a registered EH
  continuation target, although the frame and the shadow stack are unchanged (LESSONS 61). Fix, as
  built in `common/guard.py`:
  * **When.** At install on Windows, `GetProcessMitigationPolicy(ProcessUserShadowStackPolicy = 15)`
    (FALSE before Windows 10 20H1: no CET, nothing to do). Bits 0 and 2 both set: every resume
    address must be registered. Bit 8 set too, or no `SetProcessDynamicEHContinuationTargets` in
    kernelbase or kernel32: registration in process is impossible, so `install` raises and guard
    mode is refused (`auto` gives explicit; `guard` is an error at instantiation, never a crash).
    Otherwise nothing is registered: a default python.exe, and Windows on Arm, which has no CET.
    Registering only under validation was chosen over always registering on x64: the call is
    documented for CET processes only, and what it does without shadow stacks is unknown here.
  * **What.** `add_code` registers each distinct resume address of the area (code base + resume
    offset: the `Rip` the handler writes) before any of its code can run, and fails the
    instantiation if the call or any entry's `DYNAMIC_EH_CONTINUATION_TARGET_PROCESSED` bit says
    no, after removing again every entry it had added (earlier chunks and the processed entries of
    the failing one). That error stands under `auto` too: the module is already compiled and the
    instantiation has no wasm to compile it explicit again, and `install` has checked the policy,
    so only an OS failure gets there. When the area is freed, after its registry entry is removed and before it is unmapped, the
    same addresses are removed (Flags 0); a failure there only leaves stale targets behind.
  * **Constants** (mingw-w64-headers, master e3d18511): `PROCESS_DYNAMIC_EH_CONTINUATION_TARGET`
    {`ULONG_PTR TargetAddress`, `ULONG_PTR Flags`} (16 bytes), `DYNAMIC_EH_CONTINUATION_TARGET_ADD` 1,
    `..._PROCESSED` 2 (`winnt.h`); `SetProcessDynamicEHContinuationTargets(HANDLE, USHORT,
    PPROCESS_DYNAMIC_EH_CONTINUATION_TARGET)` (`processthreadsapi.h`, NTDDI_WIN10_VB); the policy
    bits of `PROCESS_MITIGATION_USER_SHADOW_STACK_POLICY` (`winnt.h`);
    `PROCESS_CREATION_MITIGATION_POLICY2_CET_DYNAMIC_APIS_OUT_OF_PROC_ONLY_ALWAYS_OFF` 2 << 48
    (`winbase.h`).
  * **Tests.** The forced-CET test now runs twice. "in-process" also turns the out-of-process-only
    rule off: the child must print `ok [0]` and two traps. "system" leaves it as the system sets it:
    with bit 8 on (as in that run) it must print our refusal and exit 0; with it off, two traps.
    `test_eh_continuation_targets_follow_the_shadow_stack_policy` and
    `test_a_guard_code_area_registers_its_resume_addresses_until_freed` (any host, fakes) cover the
    policy decision, the entries passed, and registration until free.
  * **Review fixes to option A**: `_to_front` reads `faulthandler.is_enabled()` again under the
    lock; the long-call gap is added to "Not covered" above. `install` now builds the whole handler
    state and publishes it with one assignment (`_publish`), on POSIX and Windows alike: its early
    return without the lock, `broken()` and `_to_front` could otherwise see it half filled (a
    KeyError, or no `ehcont` on a free-threaded build).
* 2026-11-03: **CI moved.** The `os` job, the macOS and Windows legs of `arm64`, `macos-pythonorg` and `android` left `ci.yml`; the guard rows of macOS and Windows (x86-64 and arm64) are now verified on every push and pull request by `platforms.yml` (cibuildwheel, the installed wheel): `tests --jit-bounds=guard,explicit` and `ci/harness/spec.py` (the spec suite on `jit-<arch>` in guard and explicit mode). The older entries above name the jobs of their time.
* 2026-11-03: **The handler's memory outlives its copy.** The final review found that a
  sub-interpreter that imported its own copy, installed its handler on top of the main one's and
  ended left that handler first in the chain, reading registry heads, tables and saved actions
  that were ctypes arrays of its module state, freed with the sub-interpreter: the main
  interpreter's next guard fault read freed memory and ended the process (139; LESSONS 64). As
  built: everything the handler reads is allocated on the C heap (`calloc`; Windows `HeapAlloc`)
  and seen through `from_address` views that do not own it, so no interpreter teardown frees it;
  heads, tables and saved actions are never freed, and a fault map is freed only by its code
  area's finalizer after its entry is zeroed. The handler's code area is never unmapped (its
  finalizer is detached). Cost per copy: 16 bytes of heads, the saved actions (one 152-byte
  `sigaction` on Linux, two 16-byte ones on macOS), the first tables (520 + 264 bytes) and every larger table published since (each
  doubles), plus the handler's code page: about 1.1 KiB of heap and one page. The same holds for a
  daemon thread still in guard code while the main interpreter tears its modules down. Also: a
  failed `_registry.add` no longer leaves a reservation marked as registered, and a failure
  between the EH-continuation add and the registry entry removes the targets and the map again.
  `on_free` is the commit point, outside the rollback (the review found that an exception after it
  had registered its finalizer made the rollback and the finalizer both free the map and the
  slot), and the handler's code area is kept before `sigaction` installs it.
  `broken()` is unchanged: a finished copy's handler is anonymous memory, still valid, and chains.
  Tests: `test_a_finished_sub_interpreter_leaves_a_working_handler_chain` (`_testcapi` or
  `_interpreters`; 139 before), `test_a_failed_registration_leaves_nothing_behind`.
