---
title: Guard pages instead of explicit bounds checks on jit-x86_64 and jit-aarch64
kind: spec
id: S0006
status: approved
superseded_by:
related: [D10, D23, D25, D29, D31, D33, D35, D36, D37, D39, D42, R2, R3, R6, S0003, S0004, S0005]
owner: maintainer
updated: 2026-11-02
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
  partial write; `memory_trap.wast` checks that none happens). Loads may be split.
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
   (kernel-generated, so a `kill`/`raise` is never ours); Windows: `EXCEPTION_ACCESS_VIOLATION`;
2. the PC is inside the `CODE` of a loaded guard artifact and its offset is a `fault_offset` of that
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
action happens (core, exit status 139); Windows returns `EXCEPTION_CONTINUE_SEARCH`. A PC in our code
that is not a fault point is **not** ours (a JIT bug crashes, as in Wasmtime, rather than becoming a
trap).

## Semantics

### The waboros runtime's handler
* **Machine code emitted by the runtime** (like the PoC), one per process, in its own sealed code
  area, from a small per-architecture emitter and a per-(OS, architecture) offset table: `si_code`,
  `si_addr`, the PC in `ucontext`/`CONTEXT`. The `ucontext` is the kernel's frame (Linux: the same
  for glibc, musl and bionic); the `struct sigaction` used to install is the libc's (a layout per
  libc). The handler reads only the registry and the context, calls nothing but the previous handler
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
  still our handler (`sigaction(sig, NULL, &cur)`; Windows: the handler is in a list that nobody can
  reorder behind us, so installed once means installed). If it is not, the call raises an error
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
  needed because we resume in the same frame (R3's note on `RtlAddFunctionTable` stays as is). Open:
  under hardware-enforced stack protection in strict mode, changing the context's PC may require the
  resume addresses to be registered with `SetProcessDynamicEHContinuationTargets`; the fault map's
  resume offsets are exactly that list (checked in step 4).
* **Android**: in app processes ART's libsigchain interposes `sigaction`: by its design our handler
  becomes a user handler, called after ART's own (implicit null checks, its stack overflow). Using libc's
  `sigaction` symbol through ctypes is what keeps this working. Checked in the CI `android` job.
* **aarch64 partial stores**: the architecture does not promise that a misaligned store crossing
  into a faulting page writes nothing; QEMU writes nothing (measured) and so does the x86-64 machine.
  `memory_trap.wast` fails if a CPU does; native arm64 runners run the spec suite in guard mode.

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
| Linux glibc | **verified** (D44) | step 2 | `jit`, `arm64` native runner, QEMU |
| Linux musl | later | later | none yet |
| macOS | step 3 | step 3 | `macos-x64`, `macos-pythonorg` |
| Windows | step 4 | later | `windows` |
| Android (bionic, libsigchain) | step 5 | device only | `android` (x86_64 emulator) |

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
  (`tools/run_arm64.py`, `--in-process`) and on the native arm64 runner.
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
3. **macOS x86-64 and arm64**: SIGBUS, Darwin offsets, `MAP_JIT`. Acceptance: the CI jobs run
   `test_guard.py` and the spec suite in guard mode.
4. **Windows x86-64**: the vectored handler; the EH-continuation question answered by a test on a
   CETCOMPAT process. Acceptance: the `windows` job as in 3.
5. **Android x86_64**: the `android` job runs `test_guard.py`; logcat shows no ART complaint.
6. **Artifact**: the flag bits and `TRAPMAP` in the serialized format when phase 5 fixes it;
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
