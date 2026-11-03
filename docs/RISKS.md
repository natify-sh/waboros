---
title: Risks and possible solutions
kind: log
status: current
updated: 2026-11-03
---

# Risks and possible solutions

Not decisions: what can go wrong, what we know about it, and the options to choose from when the time
comes. Decided matters live in DECISIONS.md.

## R1. Compiled artifacts moved between machines

**What is possible.** A JIT artifact (ARTIFACT.md) is position-independent, does not depend on the
Python version (byte-identical output under CPython 3.8 and 3.13) or on the OS when it carries both
entry trampolines. It runs on another machine with the same architecture, a CPU at or above its
declared ISA level, and a loader that knows its format and context-ABI versions.

**What to cache, what to ship — options.**

| form | load time | portable across | maintenance |
|---|---|---|---|
| wasm | compile on first use (CoreMark: ~25 ms JIT, ~111 ms `pyc`) | everything | none: it is the input |
| serialized HIR/LIR | still needs code generation (most of the ~25 ms) | architectures | one more format to version, little gain — the wasm already is a compact portable IR (parsing CoreMark takes 0.2 ms) |
| native artifact | ~instant | one architecture + ISA level + format/ABI versions | invalidated by compiler fixes (versions in the cache key) |

Likely shape: local cache = native artifacts (may use `isa="native"`); distribution = wasm, optionally
plus prebuilt baseline artifacts for x86-64 and aarch64 to skip the first compilation — as **fat**
files (D18), which carry the wasm and fall back to compiling it wherever the native code cannot run.
A fat file does not change the trust question: its native code still needs the measures below, while
regenerating from its wasm is always safe.

**Risk: trust.** The wasm sandbox exists only because the compiler inserted the checks (bounds, types,
stack). A native artifact from elsewhere is arbitrary machine code: a tampered or buggy one can do
anything the process can. (Wasmtime has the same property; its Rust `Module::deserialize` is marked
`unsafe`, as far as we know.) Options:

* load foreign artifacts only when explicitly allowed, never implicitly from a shared cache;
* a signature or a trusted hash list (the publisher's key, or the user's own build);
* otherwise always recompile from the wasm, which is safe to accept from anyone;
* the cache directory is per user and written atomically; refuse artifacts whose checksum, versions or
  ISA do not match before mapping anything executable.

**Risk: invalidation.** A compiler bug fixed in version N must not keep running from artifacts made by
N-1: the compiler version and the target version are part of the key and of the header; a list of
known-bad versions can force recompilation.

## R2. Platforms restricting run-time code generation

**Status at HEAD: partly mitigated.** Mitigated on x86-64 Linux: the W^X executable-memory probe and the
fallback to `pyc` with `JitUnavailableWarning` (D31; proved with executable memory forbidden by
`tools/no_exec.py` in the CI `spec` job and `tests/test_fallback.py`); CET IBT (`endbr64` at every
indirectly reachable entry, `backend.py`) and CET shadow stacks (traps return through frames, S0003). aarch64: `bti c` landing pads are emitted and the tests run under QEMU with BTI enforced; PAC is not used (S0004). macOS `MAP_JIT` under a hardened runtime: answered for python.org CPython (the JIT runs, row below). Android: answered for x86_64 in an `untrusted_app` process (the JIT runs, row below); arm64 has no CI job. Open: Windows ACG (the Windows and macOS x64 CI jobs run `tests/jit`).

The direction is clear: more systems forbid writable-and-executable memory, and CPUs add control-flow
integrity that hand-made code must respect.

| platform | restriction | effect on us | possible solution |
|---|---|---|---|
| iOS / iPadOS (Pythonista) | no JIT for third-party apps | JIT impossible | `pyc`; maybe a system engine (R3, JavaScriptCore) |
| macOS on Apple Silicon | executable memory needs `MAP_JIT` and per-thread write/execute switching (`pthread_jit_write_protect_np`); hardened-runtime apps need a JIT entitlement | works if the interpreter binary allows it; depends on how it is signed | ctypes calls for `MAP_JIT` + toggling; probe; fall back to `pyc` |
| macOS arm64, python.org CPython | notarized, hardened-runtime build (`flags=0x10000(runtime)`) | **the JIT runs** (the former CI job `macos-pythonorg`, run 41349579025, python.org 3.13.17 pinned by sha256, PSF-signed): the binary has `com.apple.security.cs.disable-executable-page-protection` (also `disable-library-validation`, `allow-dyld-environment-variables`, `debugger`, `get-task-allow`, `automation.apple-events`; no `allow-jit`), the probe returns None, `auto` selects `jit-aarch64` with no warning and the aarch64 ABI tests pass (56 passed) | if a future python.org build drops that entitlement, the macOS cells of `platforms.yml` (which use the python.org CPython) fail and `auto` falls back to `pyc` with the D31 warning; non-hardened CPython (uv, Homebrew, pyenv) is unaffected |
| Android app process | SELinux policy of `untrusted_app` (anonymous executable memory, `execmem`; apps cannot execute files from their data directory since Android 10, which does not touch our `mmap` path) and the kernel's own limits | **x86_64: allowed** (CI run 41351056918, the former job `android`): in the testbed app (python.org CPython 3.14.10, target API 35, `aosp_atd` x86_64 emulator, SELinux context `u:r:untrusted_app`) the `jit-x86_64` probe succeeds, `auto` selects it with no warning, CoreMark passes its self-check (15 424 it/s on the emulator: not a benchmark) and the JIT tests pass (75 passed, 5 skipped); logcat shows no `execmem` denial. The Android cell of `platforms.yml` keeps running the JIT tests on every push. Termux is a different, permissive environment and proves nothing for apps | probe; fall back; `arm64_v8a` is not checked in CI (no emulator on GitHub arm64 runners), so the aarch64 answer comes from a device |
| Linux, hardened | SELinux `deny_execmem`, PaX/grsecurity `MPROTECT`, systemd `MemoryDenyWriteExecute=yes` | `mmap`/`mprotect` to executable fails | probe; fall back; document for service authors |
| Windows | Arbitrary Code Guard (process mitigation, opt-in) blocks dynamic code | same | probe; fall back |
| Pyodide / browsers | no native code at all | JIT impossible | `pyc`, or the browser's `WebAssembly` (ROADMAP open question 7) |
| x86-64 with CET **IBT** | indirect branch targets must start with `endbr64` | the entry trampoline and anything called through a pointer would fault | emit `endbr64` at every indirectly reachable entry (a no-op on older CPUs) |
| x86-64 with CET **shadow stack** (Linux user space, Windows 11 hardware stack protection) | returns are checked against a hardware stack | the PoC's trap unwinding (reset `rsp`, then `ret`) would fault | unwind by returning through frames (each frame checks a trap flag), or a shadow-stack-aware unwind; never "jump" the stack |
| aarch64 **BTI** / **PAC** | landing pads (`bti c`) for indirect targets; signed return addresses in some processes (arm64e) | indirect entries fault without pads | emit `bti c`; keep return addresses unsigned unless the host process requires PAC |

General answers: W^X always (map writable, write, flip to executable; never both), the probe described
in TARGETS.md, CFI-compatible code from the first version, and `pyc` as the guaranteed fallback.

## R3. A hand-written JIT: correctness and safety

**Status at HEAD: partly mitigated.** The encoder is checked form by form against `llvm-mc` in CI
(`tests/jit/test_encoder.py`), the ISA gate disassembles every generated blob (`tools/isa_all.py`), and
the spec core passes on `jit-x86_64` and `jit-aarch64` like on `pyc`; the ISA gate covers every
compiled module of both targets, SIMD ones included; SIMD code also runs on an SSE2-only CPU under
QEMU (`qemu64`) in CI; the independent final review of phase 3 is done (S0002 step 6) and the SIMD
steps are read by a `reviewer` one by one (S0005). Open: differential fuzzing against wasmtime
(phase 5), and the review of the SIMD steps as a whole (S0005, 5f).

**Risk.** Encoding mistakes, register-allocation mistakes and missed checks are not exceptions but
wrong results or sandbox escapes. The PoC already met three silent bugs (LESSONS.md 1, 2, 5).

**Alternatives and their trade-offs.**

| option | idea | for | against |
|---|---|---|---|
| copy-and-patch | operation stencils in C compiled by clang on our CI, shipped as bytes; the run time only stitches and patches | clang generates the instructions; new architectures and SIMD are cheaper | build infrastructure; relocation formats follow LLVM versions; `musttail`/`preserve_none` not everywhere |
| an assembler library in Python (e.g. PeachPy for x86-64) | we select instructions, the library encodes | fewer encoding bugs | x86-64 only; project activity to check; vendoring |
| generate C at run time + the system compiler | wasm2c on the fly | best code | a compiler is often absent: not universal |
| a JIT library through ctypes (libLLVM, libgccjit) when installed | someone else's back end | code quality | not present everywhere, versions differ: a dependency in disguise |
| a system wasm engine | the browser's `WebAssembly` in Pyodide; JavaScriptCore on Apple platforms through ctypes / objc | no code generation of ours | platform-specific; whether JavaScriptCore runs wasm on iOS without JIT, and how fast, is unverified — worth checking first for Pythonista |
| our JIT with a small trusted core | see below | control, no dependencies | the work stays ours |

**Ways to shrink the risk of our own JIT** (applicable whatever is chosen):

* a table-driven instruction encoder, checked in CI against an external assembler/disassembler
  (`llvm-mc`, `objdump`) for every instruction form it can emit;
* memory safety concentrated in one small, reviewed piece: the bounds check, or an alternative design
  where addresses are masked/zero-extended into a reserved region surrounded by guard pages, so that a
  missed check crashes the process instead of escaping (a crash cannot be turned into a trap from
  Python — a mode to consider for speed, with that trade-off stated);
* differential fuzzing against wasmtime (`wasm-smith`), the spec suite, and mixing a known-good and a
  new code generator per function to bisect failures (how the `printf` bug was found);
* decide on copy-and-patch after `jit-aarch64` exists by hand, with numbers for both.

JIT code registers no Win64 unwind information (`RtlAddFunctionTable`). Harmless today: traps return
through frames (D10) and host exceptions are statuses, so no SEH unwind ever crosses JIT frames; a
debugger or profiler walking the stack may stop there. Needed once a fault handler (R6) unwinds.

## R4. Version skew: output of one waboros version used by another

> **Largely settled by DECISIONS.md D18** (the Wasmtime model): generated code is used only by the
> waboros version that made it; on any mismatch the runtime regenerates from the wasm (the cache, a fat
> file) or refuses (a thin file). The analysis below is kept for the reasons.

Two versions meet: the one that **generated** the code and the one that **runs** it (another machine,
an upgrade, a shared cache, an artifact shipped with a package).

**Per kind of output.**

| output | coupling today (PoC) | coupling in the planned design | what can break |
|---|---|---|---|
| `pyc` / `mpy` module (Python source) | none: the runtime helpers are emitted into the module | the module talks to the runtime (`Store`, the `Memory` facade, imports, traps); to stay small it will want to import shared helpers | a module from version N calling helpers or facades that changed in N+1 |
| `.pyc` code object | the CPython bytecode version (magic number) | same | another Python version: refused by the magic check, must be regenerated |
| `.mpy` | the MicroPython `.mpy` version and architecture | same | another MicroPython release / port |
| JIT artifact | format, context ABI and compiler versions in the header (ARTIFACT.md) | same | a loader that does not know those versions refuses it |

**The silent case: fixed compiler bugs.** An artifact or module generated before a code-generation fix
keeps the bug forever — the code is frozen. Possible answer: a list of known-bad compiler versions in
the runtime that forces regeneration.

**Possible solutions for the generated-module coupling** (ARCHITECTURE.md, "Versioned contracts"):

* (a) **self-contained modules**: helpers always emitted into the module — no coupling, larger output;
* (b) **versioned helper namespaces** (`waboros.rt.v1`, `waboros.rt.v2`, …): the package keeps old helper
  versions side by side; a module imports exactly the one it was generated for;
* (c) **a declared module ABI** (`__waboros_abi__ = N` in the module): the runtime refuses a mismatch and
  regenerates from the wasm.

**Possible general policy: the wasm is the source of truth, everything generated is a derivative.** On
any mismatch — format, ABI, compiler version on the bad list, Python/MicroPython version — regenerate
from the wasm. Regeneration is cheap (CoreMark: ~25 ms JIT, ~111 ms `pyc`), so "accept only what I
know, otherwise recompile" is the simplest robust rule. It needs the wasm at hand:

* cache entries keep the wasm hash, and the wasm stays where it was loaded from;
* or **"fat" artifacts** embed the original wasm: larger, but self-healing under any version;
* long compatibility windows (e.g. the current and previous major format) are needed only for artifacts
  shipped **without** their wasm.

## R6. A signal handler of our own (guard pages in the JIT)

**Status at HEAD: partly mitigated: the default on Linux glibc x86-64 and aarch64, Linux musl x86-64, macOS, Windows x64 and arm64, and Android x86_64 (step 5; the first CI run of the Android cell decides).** S0006 step 1:
guard code on `jit-x86_64`; step 2 the same on `jit-aarch64` (`aarch64/abi.py`, verified under QEMU, the native
runner to confirm). The handler is emitted machine code (`x86_64/abi.py`, `aarch64/abi.py`) reading this copy's registry
(`common/guard.py`), chaining every fault it does not claim (tests: `tests/jit/test_guard.py`, foreign faults
with and without `faulthandler`, a broken chain, a foreign chaining handler, wasmtime above ours, two copies of
the package in either order). Since D44 `auto` is the default: guard on the verified rows (Linux glibc x86-64
and aarch64, Linux musl x86-64, macOS and Windows x64 and arm64),
explicit elsewhere; `WABOROS_JIT_BOUNDS=explicit` installs nothing. macOS x86-64 and arm64 (step 3): a handler
on SIGSEGV and SIGBUS, built from xnu's sources without a macOS machine; CI's macOS steps ran the spec suite and
`test_guard.py` in guard mode and passed (run 41610392553), so the row is verified. **Not detected** (rule A, S0006 Changes):
faulthandler that was already enabled when ours was installed (as under pytest), then disabled and re-enabled
while a library's handler is above ours; a guard fault then reaches faulthandler through that library and ends
the process (139). faulthandler enabled only after ours, under any foreign handler, is detected (the Q1 error). A
straddling store may write its in-bounds part before the fault where a store is split (QEMU's TCG, possibly box64
or FEX; Arm, RISC-V); on x86-64 this is accepted as in Wasmtime (D45, LESSONS 50): native faults are precise, and
CI's qemu64 steps run in explicit mode. aarch64 follows the same rule (D45): under QEMU a straddling 16-byte
store wrote its first 8 bytes, and native Arm hardware is allowed to do the same. Under FEX or box64 on an Arm server an x86-64 Python sees
x86_64 and glibc, the verified row, so `auto` turns guard pages on exactly where a store may tear;
Wasmtime does not detect this either. Open: detect an emulator (`/proc/cpuinfo`, FEX's and box64's
environment variables) and fall back to explicit, or keep it a documented limitation (D45). Windows x64
and arm64 (S0006 step 4): a vectored exception handler (offsets from the Windows SDK headers), verified by CI
runs 41633456821 (`arm64 (windows-11-arm)`) and 41640149866 (`os (windows-latest)`), jobs now in `platforms.yml`; `win32` is a verified
row, so `auto` gives guard there. Android x86_64 (S0006 step 5): bionic's
32-byte `struct sigaction`, installed through ART's libsigchain (below); `tests/jit/test_android.py` in the
Android cell of `platforms.yml`. Open: Linux musl aarch64 (no
`__clear_cache` without libgcc, S0004). Android arm64 stays explicit, a documented limitation (S0006).

Guard pages need a SIGSEGV handler in the process (ARCHITECTURE.md, "Memory"); the PoC emits it as
machine code. Risks and possible answers:

* **Layouts per platform**: `siginfo_t`/`ucontext_t` offsets and the `struct sigaction` layout differ
  per OS, libc and architecture (glibc vs musl, macOS `__darwin_mcontext64`, BSDs; Windows has no
  signals — a vectored exception handler through `AddVectoredExceptionHandler`). Answer: a table per
  platform, verified by a test that traps on purpose; explicit checks where no entry exists.
* **Other handlers in the process**: `faulthandler`, other runtimes (wasmtime-py, wasmext modules,
  JVM/.NET embedded), a later `sigaction` by someone else that replaces ours. Answer: chain to the
  previous handler (the PoC does), re-check at each entry and after each host call (a `sigaction`
  query) and raise when the chain is recognisably broken (SIG_DFL/SIG_IGN, a handler inside the
  interpreter's own image by `dladdr` — faulthandler, the `signal` module —, faulthandler enabled
  after ours under a foreign handler, a Python-level handler set after ours; S0006 Q1, rule A); any other handler is assumed to chain, as Wasmtime assumes;
  never install when the user asks not to (`explicit` installs nothing).
* **Threads**: the handler is per process; the fault address and the code range identify the module,
  so it works from any thread; stack-overflow detection needs `sigaltstack` per thread (not done).
* **A fault not of ours inside our code range** (a bug of the JIT): becomes a wasm trap instead of a
  crash. Answer: only fault points of the fault map with an address inside a reservation are taken
  (S0006); differential tests against Wasmtime.
* **Several modules**: one handler per copy of the package with a table of code ranges (fault maps)
  and reservations; several copies chain (S0006 Q8 as revised; tested with a renamed copy).
* **CET / BTI**: the handler is entered indirectly by the kernel; with IBT enforced it needs `endbr64`
  (R2).
* **Android: ART's libsigchain sits in front** (S0006 step 5). In an app process it interposes `sigaction`
  and owns the kernel's SIGSEGV action: ours becomes its "user" action, called after ART's fault manager
  (which takes only faults in ART's own generated code on a Runnable thread) and debuggerd's GWP-ASan hook.
  This holds only while we call libsigchain's `sigaction` (the one `ctypes.CDLL(None)` finds; a raw
  syscall, or bionic's own symbol, would put ours in front and a later `EnsureFrontOfChain` would make our
  saved action point back into the chain). `test_android.py` checks that the kernel's action is still
  libsigchain's after our install. Residual: the SIG_DFL path makes libsigchain log with a stack unwind
  inside the handler (only when the process is ending); a debuggable app with libsigchain's hooks off
  makes `sigaction` a no-op, which `broken()` reads as SIG_DFL (`auto` gives explicit); rule A has no
  interpreter image there (`CDLL(None)` does not see libpython), so the conservative step-1 rule applies.
* **macOS: Mach exceptions come first.** A fault is an `EXC_BAD_ACCESS` Mach exception before it becomes
  a signal, so a debugger or a crash reporter on a thread or task exception port (Breakpad, Crashpad,
  Sentry's native SDK) sees every guard fault before our handler. One that returns `KERN_FAILURE` lets
  the signal through; one that terminates the process defeats guard mode (`WABOROS_JIT_BOUNDS=explicit`
  is the answer). lldb stops at each guard trap (lldb 15 and later can pass them with
  `settings set platform.plugin.darwin.ignored-exceptions EXC_BAD_ACCESS`; not checked here). Wasmtime
  moved to Mach ports for this reason (its `machports.rs`, issue 2456); we keep signals (S0006 Q5) until a
  user's crash reporter breaks them. Wasmtime itself, with its default Mach ports, installs no signal
  handler, so in one process with it ours stays on top.
* **macOS: sent signals look like faults** (LESSONS 57): xnu gives a sent SIGSEGV/SIGBUS a positive
  `si_code`, and its `si_addr` is the thread's last page-fault address (x86-64 `es.faultvaddr`, arm64
  `es.far`; `bsd/dev/{i386,arm}/unix_signal.c`). For a thread running memory-heavy wasm that address is
  routinely inside a reservation, so a `kill -BUS`/`-SEGV` that lands while the PC is on a fault point
  becomes a `MEMORY_OUT_OF_BOUNDS` trap and the signal is swallowed. Accepted, as in Wasmtime, which
  decides by the PC alone. A previous SIG_DFL/SIG_IGN is always followed by `raise`. For a real fault and SIG_DFL, the pending signal ends the process before the instruction runs again, with the same signal. With SIG_IGN, xnu's `threadsignal` drops the signal, so a real fault runs again forever (a 100 % CPU hang): xnu's behaviour without us too, not a regression (Linux force-unignores a fault signal).
* **Windows: a vectored list instead of a chain** (S0006 step 4). Our handler is added first
  (`AddVectoredExceptionHandler(1, h)`) and only the holder of the returned handle can remove it, so
  nothing can unhook or replace it and `broken()` is always None there; but any handler added later with
  `first = 1` runs before ours. One that returned `EXCEPTION_CONTINUE_EXECUTION` for a guard fault, or
  ended the process, would defeat guard mode; none is known (Wasmtime's claims only faults of its own
  wasm on the thread; ctypes' `__except` is frame-based and comes after every vectored handler).
  **faulthandler is a vectored handler too** (CPython `Modules/faulthandler.c`, 3.13): enabled after
  ours it sits in front, prints "Windows fatal exception: access violation" and a traceback for every
  guard fault, disables its SIGSEGV handler and passes the fault on, so the trap still happens but
  stderr would fill with false "fatal" reports (LESSONS 59). Answer (the maintainer's option A): the
  check before each entry and after each host call sees faulthandler turned on since the last check
  and puts ours in front again (add, then remove the old entry; tested). Enabled before ours (pytest,
  `-X faulthandler`) it stays behind and silent. Still running before ours: faulthandler disabled and
  re-enabled between two checks while it was already on (the state reads the same; the false reports
  return, tested); and any other vectored handler added later with `first = 1`, e.g. another
  runtime's trap handler (Wasmtime's, a .NET or JVM runtime hosted in the process) or another waboros
  copy installed later. Those seen so far pass faults they do not own on, so they cost a call, not a
  trap. A long JIT call on one thread that makes no host calls does not see faulthandler enabled by another thread meanwhile: its guard faults bring the false reports until its next entry or host-call return. Vectored **continue** handlers run after a
  handler returns `EXCEPTION_CONTINUE_EXECUTION`; one that ends the process for every exception (Go's
  runtime does, per Wasmtime's `vectored_exceptions.rs`) would end it on a guard trap. Wasmtime adds a
  continue handler of its own in front for this; we do not (no Go embedding in a Python process seen).
  A debugger gets every guard fault as a first-chance exception before any vectored handler and must
  pass it on unhandled (in WinDbg, e.g., `sxd av`; not checked here), like lldb on macOS.
* **Windows: CFG and CET.** python.exe is neither CFG-instrumented nor CETCOMPAT (read from the PE
  headers of python.org 3.13.9 and 3.14.0, x64 and arm64, and of uv's 3.13.18: no `GUARD_CF`, no
  `IMAGE_DLLCHARACTERISTICS_EX_CET_COMPAT`; the bundled `vcruntime140.dll` has both, as a control), so
  neither CFG nor user shadow stacks are on in a default Python process. Were CFG on, ntdll's call of a
  vectored handler is checked against the CFG bitmap, and `VirtualProtect` to an executable protection
  marks the whole area as valid targets by default (Microsoft, "Memory Protection Constants",
  `PAGE_TARGETS_NO_UPDATE`), which is how `common/execmem.py` seals code. Under user shadow stacks,
  setting a new `Rip` in the context is subject to context-IP validation, and resuming in the same
  frame does not help: CI's `windows-latest` has user CET, and a child with shadow stacks forced on
  (strict, IP validation) died with 0xC0000409 at its first guard trap (run 41633456821; LESSONS 61).
  Answer: under that policy the runtime registers every resume address with
  `SetProcessDynamicEHContinuationTargets` before the code runs and removes them when the code is freed;
  where the policy also says CetDynamicApisOutOfProcOnly (it did there, unasked) or the API is missing,
  guard mode is refused and `auto` gives explicit. A process that turns validation on after our
  install is not covered (the policy is read once). The forced-CET test checks both cases.
* **macOS arm64: `MAP_JIT` write mode and PAC.** The handler's code is `MAP_JIT` memory, which a thread
  cannot execute while it is in write mode (`pthread_jit_write_protect_np(0)`). A fault on such a
  thread (another library's JIT writing its code) cannot run our handler and faults again inside the
  signal delivery; on xnu a fault while its signal is blocked stays pending (`threadsignal`, read, not
  measured), so this may hang rather than crash. Our own write windows are copies into fresh pages and
  cannot fault. The handler writes no JIT memory, only the signal context. The saved PC is unsigned in an
  arm64 process (CPython's builds are arm64 or universal2, not arm64e): its threads run with user JOP
  disabled and sigreturn takes the state as written (`osfmk/arm64/status.c`). An arm64e host would need
  the PC signed; it is not supported.

## R5. Other known risks (detailed elsewhere)

| risk | where |
|---|---|
| scope: feature parity with Wasmtime (SIMD, GC, exceptions, threads, memory64) is large | FEATURES.md, ROADMAP.md |
| `pyc` speed ceiling on CPython (~1.5–2× the PoC at best) | DECISIONS.md D4, BENCHMARKS.md |
| long calls: GIL in `pyc`, uninterruptible machine code | ARCHITECTURE.md, "Long calls and the GIL" |
| deep recursion in `pyc` | ARCHITECTURE.md, "Calls, stack depth and recursion" |
| the WASI host is undecided | ROADMAP.md, open question 2 |
| `mpy-cross` 1.29.0 miscompiles very large native functions | LESSONS.md 17 |
| maintaining several architectures | TARGETS.md, copy-and-patch |
| a host function on a very small thread (a few hundred KiB) needing more than half its stack can overflow the C stack on CPython < 3.14 (no C-stack check), as it would without wasm | S0003 (native limit), step-3a review |
