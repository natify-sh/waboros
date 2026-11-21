---
title: Decisions
kind: log
status: current
updated: 2026-11-21
---

# Decisions

Short records: what was decided, why, and what was rejected. Newest last.

### D1. No runtime dependencies
Status: accepted · Date: 2026-10-26 · Supersedes: — · Evidence: —

The runtime uses the Python standard library only. Native runtimes (wasmtime, wasm3, WAMR/iwasm)
were considered and rejected for the runtime: wasmtime is tens of MB, WAMR does not fit, and any
native piece breaks platforms like Pythonista. Development tools are free to use (D11).

### D2. Translate, do not interpret
Status: accepted · Date: 2026-10-26 · Supersedes: — · Evidence: BENCHMARKS.md, CoreMark

Measured on CoreMark (CPython): pywasm (interpreter) 0.58 it/s, waboros translation 86, so ×150; on PyPy
×400, because a tracing JIT optimises straight code far better than an interpreter loop (warpy, an
RPython interpreter with a meta-tracing JIT: 192 it/s). Superinstructions were considered and do not
apply: the translation already has no dispatch left.

### D3. Python source as the `pyc` target's intermediate form, compiled by the host
Status: accepted · Date: 2026-10-26 · Supersedes: — · Evidence: —

The translator emits Python source and lets `compile()` make the bytecode. Hand-written bytecode would
be fragile across Python versions and gains nothing (CPython's compiler does not optimise, but neither
would we). The same source serves CPython, PyPy and MicroPython (with a flavour). Text format (WAT) as
input to the translator was rejected: the binary is stricter and simpler to parse; `.wat` may be
accepted later through a separate parser.

### D4. Speed on CPython comes from a JIT in Python, not from more Python
Status: accepted · Date: 2026-10-26 · Supersedes: — · Evidence: BENCHMARKS.md, CoreMark

The ceiling of translated Python on CPython is ~1.5–2× the PoC (boxed ints, ~30–60 ns per operation,
`struct` memory access ~70–90 ns). A JIT written in Python, emitting machine code into executable
`mmap` memory and entering it through `ctypes` (both stdlib), reached 15 337 it/s (≈ 80 % of
wasmtime). Accepted with its costs: per-architecture work and platforms that forbid JIT, both handled
by D5.

### D5. Targets behind one interface, chosen automatically, falling back per module
Status: accepted (per-module fallback superseded by D25) · Date: 2026-10-26 · Supersedes: — · Evidence: —

`pyc` always; `mpy` on MicroPython; `jit-<arch>` where the architecture matches and executable memory
works. Architectures are added one by one; a module using a feature a JIT lacks runs on `pyc`.
*Per-module fallback superseded by D25 (one target per engine/store, parity).*

### D6. Follow Wasmtime: model, API, features, traps
Status: accepted · Date: 2026-10-26 · Supersedes: — · Evidence: —

Engine/Store/Module/Linker/Instance and wasmtime-py's names; the `wasmtime` binary's CLI (wasmtime-py
50 has none); Wasmtime's feature switches and trap codes. Wasmtime is the specification of behaviour
wherever the wasm spec leaves room.

### D7. Component model: fused modules only
Status: accepted · Date: 2026-10-26 · Supersedes: — · Evidence: —

No component parser or canonical ABI in waboros. Components are fused with `meld` into one core module;
WASI 0.2 comes from the WASI host (D8). Requires multi-memory. p1 programs may go the same way through the
preview1 adapter.

### D8. WASI host: open
Status: accepted · Date: 2026-10-26 · Supersedes: — · Evidence: —

Not decided. The candidate is the Python twin of fusedwasi (natify-sh/fusedwasi): one function per
import, memories as buffers, the same code as the C headers used by wasmext. It has not been used with
waboros yet. Whatever is chosen, waboros only binds names and memories (`Linker.define_wasi`), and the
choice must not set the minimum Python of the package (3.11, D19).

2026-10-30 (maintainer): fusedwasi 0.1.0a1 is runtime-independent and already works with wasmtime,
wasm3 and wasmext, so it is expected to fit waboros as well; the choice is confirmed in the WASI phase by
running it on waboros.

2026-11-05 (maintainer): fusedwasi 0.1.0b1 is released (PyPI: one `py3-none-any` wheel, Python >= 3.10,
no dependencies; WASI 0.1 implemented over WASI 0.2 with one state; `wasi:sockets` and `wasi:http` still
unstable). 0.1.0b2 followed the same day. Still the candidate until the WASI phase runs it on waboros.

2026-11-05 (maintainer): fusedwasi 0.1.0b3: changes in `wasi:http`, and a draft spec (fusedwasi's own spec number 15) on
what a preopen promises in `wasi:filesystem` and WASI 0.1. On POSIX the path walk (one name at a time
through held descriptors, `O_NOFOLLOW`, `..` handled by the walk) cannot be led out of the preopen
by a race (one 8-second run on the Python twin, 0 escapes; the C twin, which wasmext uses, only
read). On Windows the check and the open are two steps, so another process that can write the
shared folder can race a junction in; closing it needs a walk by handle (`NtCreateFile` with a root
directory), native in C, `ctypes` in Python. A link the guest makes with `symlink-at` (0.2) may point
out; the walk never follows it, another process may. For waboros (the Python twin) this means a
preopen is not a barrier against other processes on Windows until that work is done.

### D9. Cache compiled artifacts
Status: accepted · Date: 2026-10-26 · Supersedes: — · Evidence: —

Keyed by the module hash, waboros version, target and target environment, and code-affecting config. The
same format is `Module.serialize`. Measured: `pyc` cold 111 ms → 0.5 ms cached. Serialized files and
fat files (embedding the wasm): D18.

### D10. Never crash the interpreter on deep recursion
Status: accepted · Date: 2026-10-26 · Supersedes: — · Evidence: —

Stack depth is bounded per store with a deterministic `STACK_OVERFLOW` trap; `sys.setrecursionlimit` is
not changed globally; `RecursionError` is a safety net only (ARCHITECTURE.md).

### D11. wasmtime is a development dependency: the test oracle
Status: accepted · Date: 2026-10-26 · Supersedes: — · Evidence: —

Differential tests run every target against a pinned wasmtime; wabt, wasm-tools, pywasm, meld,
MicroPython and QEMU are development tools too. None of them is a runtime dependency.

### D12. Dropped and parked
Status: accepted (the Lua item superseded by D20) · Date: 2026-10-26 · Supersedes: — · Evidence: —

* **mypyc target** — dropped: needs `librt` at run time (a dependency) and a native build is better
  served by wasm2c/wasmext.
* **Lua / LuaJIT backends** — parked as an experiment proving the IR is language-neutral (LuaJIT
  1 873 it/s); not part of waboros. Superseded by D20 (rejected).
* **`--trust-align` memoryview access** — rejected: no gain on CPython, ×20 slower on PyPy.
* **Function inlining in `pyc`** — opt-in only: neutral on CPython 3.13, ~+5 % on PyPy.
* **pywasm as the base** — rejected as a base (interpreter, ×150–400 slower, needs Python ≥ 3.11 — `match`, `typing.Self` — no
  MicroPython); used as a second oracle and a reference.

### D13. MicroPython where possible
Status: accepted · Date: 2026-10-26 · Supersedes: — · Evidence: —

A separate `mpy` target, best effort, ahead-of-time `.mpy` first; `@native` measured faster than
CPython; viper is the research direction.

### D14. Long calls: JIT releases the GIL and polls for interrupts; `pyc` needs yield points
Status: accepted · Date: 2026-10-26 · Supersedes: — · Evidence: —

Measured: a ctypes `CFUNCTYPE` entry releases the GIL (another thread ran throughout a 19.6 s guest
call) but a SIGINT cannot stop machine code (the call ran to its end). So JIT code polls an
interrupt/epoch word; `pyc` holds the GIL by nature and gets yield points (and, if parallelism is
needed, subinterpreters / free-threaded builds / a process). One thread per store at a time
(ARCHITECTURE.md, "Long calls and the GIL").

### D15. The JIT artifact is a language-neutral, versioned contract
Status: accepted (narrowed by D36) · Date: 2026-10-26 · Supersedes: — · Evidence: —

Compiled code depends on nothing Python-specific (byte-identical output under CPython 3.8 and 3.13), so
its boundary is fixed and versioned in ARTIFACT.md: (1) **ISA levels** — portable artifacts use the
architecture's baseline (x86-64-v1, ARMv8.0-A) with baseline sequences for higher-level instructions;
`native` only for local caches; the loader checks the CPU; (2) **artifact format v1** — self-contained,
with entry trampolines for every calling convention of the architecture, function/trap tables,
segments; (3) **context ABI v1** — entry signature, context words, host-function signature, trap
codes. Hosts in other languages (C, Dart, LuaJIT, …) may load artifacts; a C reference loader keeps the
contract honest. *Narrowed by D18*: the contract is first of all the format of cache entries and
serialized files read by the waboros runtime; other-language loaders and the C loader are optional.

### D16. Our own thin IR; wasm is the portable IR; no LLVM
Status: accepted (LIR as a materialised list superseded by D26) · Date: 2026-10-26 · Supersedes: — · Evidence: —

Wasm already is a typed, validated, producer-optimised IR, so no LLVM-class optimiser is needed (a
baseline JIT reached ~80 % of Cranelift). A thin IR of our own carries what wasm lacks: **HIR** (trees,
structured control flow, analyses) shared by all targets, and **LIR** (machine-neutral linear
operations) shared by all JIT architectures. Structured rather than a CFG (Python has no `goto`); no SSA
(little gain on optimised wasm, and it would have to be deconstructed for Python). Optimisations only
when measured to pay; bounds-check elimination first. LLVM, Cranelift IR and Binaryen IR were rejected
as runtime dependencies (LLVM stays a candidate build-time tool for copy-and-patch stencils).

### D17. The shared layers stay language-neutral; other languages are not in scope
Status: accepted · Date: 2026-10-26 · Supersedes: — · Evidence: —

HIR, LIR and the JIT artifact keep nothing Python-specific: the target's integer model is a parameter of
the analyses, backends hold everything language-specific, artifacts are loadable from any FFI-capable
language. Evidence that it works: the PoC's Lua 5.4 / LuaJIT backends reuse the front end. Only
Python-family targets (`pyc`, `mpy`, JITs loaded from Python) are in scope; the principle is cheap
insurance, not a commitment.

### D18. No embedded runtime: the Wasmtime model, not the wasmext model
Status: accepted (in part superseded by D36) · Date: 2026-10-30 · Supersedes: — · Evidence: —

waboros generates code when a module is loaded, so what it generates is a derivative owned by the engine,
as in Wasmtime — not a product that lives without the tool, as wasm2c + a C build is in wasmext (there
the output must carry its runtime). Consequences:

* **The runtime lives only in the package**: `Store`, memories, tables, globals, traps, WASI, the JIT
  entry and context. Generated code (a `pyc` code object, a JIT artifact) holds code and metadata and
  uses the runtime of its own waboros version. The PoC's `RUNTIME` string emitted into every module is a
  PoC shortcut, not the design.
* **Portable form: a serialized file** (`Module.serialize`, `waboros compile`), like Wasmtime's `.cwasm`;
  it runs only on a waboros runtime. Named `<name>.<target>.<ext>`, the target including what decides
  whether it can run there (`core.jit-x86_64-v1.w2a`, `core.pyc-cp313.w2a`; `.w2a` is a working
  extension until the project has a name). The header, not the file name, is authoritative.
* **Fat files**: optionally the file embeds the original wasm (ARTIFACT.md, section `WASM`). A fat file
  is never refused: on any mismatch — waboros or target version, bytecode magic, ISA level, a known-bad
  compiler version — the runtime regenerates from the embedded wasm (and may cache the result), so the
  file always runs, only slower the first time. A thin file is refused on a mismatch, as in Wasmtime.
* **Version skew (RISKS.md, R4) reduces to one rule**: accept an exact match, otherwise regenerate from
  the wasm (cache: the wasm is at hand; fat file: embedded) or refuse (thin file). No compatibility
  windows and no versioned helper namespaces; the interface between generated code and the runtime is
  internal and may change between any two versions.
* ARTIFACT.md stays the format of cache entries and serialized files; loaders in other languages and
  the C reference loader become optional (D15 narrowed). *Superseded by D36 for these points and the
  two on running only on a waboros runtime and the internal interface.*
* Possible exception, later and optional: a standalone export with minimal embedded helpers for
  MicroPython devices, where the runtime package costs flash.

### D19. Minimum Python 3.11; tested 3.11–3.15, 3.15t and PyPy 3.11
Status: accepted · Date: 2026-10-30 · Supersedes: — · Evidence: —

`requires-python = ">=3.11"`. CI runs CPython 3.11, 3.12, 3.13, 3.14, 3.15, the free-threaded 3.15t,
and PyPy 3.11; actions are pinned by commit SHA. Tooling (ruff, pyright) targets 3.11.

* **Why**: current versions only. The free-threaded build matters for "one thread per store" and long
  calls (ARCHITECTURE.md). wasmtime 50 needs ≥ 3.9 and `wabt-package` ≥ 3.11 anyway, so a lower floor
  would need skip markers for the oracle.
* **Rejected: a 3.8 floor.** It worked in the PoC (the 3.8 measurements stay as history), but EOL
  versions cost CI time and block newer syntax and typing.
* Generated `pyc` code's floor follows the package floor unless a target says otherwise; the
  MicroPython flavour is separate.

### D20. Lua / LuaJIT rejected as a target
Status: accepted · Date: 2026-10-30 · Supersedes: D12 (the Lua item) · Evidence: BENCHMARKS.md, CoreMark

Lua 5.4 and LuaJIT are not waboros targets, now or later. The experiment gave little for its cost
(Lua 5.4 271 it/s, LuaJIT 1 873 it/s, against ~15 000 for the x86-64 JIT and no use from Python);
it stays in `poc/` (`w2lua.py`, `w2luajit.py`, `run_lua.py`) as a record and as evidence for D17.
D17 (language-neutral shared layers) is unchanged.

### D21. No WAT parser of our own; WAT through wasm-tools, optionally
Status: accepted · Date: 2026-10-30 · Supersedes: — · Evidence: —

waboros reads the binary format only and will not contain a text-format (WAT) parser: a full WAT grammar
(folded expressions, every proposal, GC type syntax, annotations) is a project of its own and adds
nothing to the compiler or the targets. Who needs WAT uses `wasm-tools parse`. In phase 6 the public
API may accept WAT when the optional extra `wasm-tools-package` is installed, calling it
automatically; without it, text input gets a clear error (a deliberate deviation from wasmtime-py,
which accepts WAT everywhere). The package keeps no runtime dependency (D1): the extra is optional.
Tests write modules in WAT and build them with `wasm-tools` at development time. The 662 spec
commands that test only WAT parsing (text `assert_malformed`, S0001) stay `skip` permanently: with
the extra they would test wasm-tools, not waboros. This settles D3's "`.wat` may be accepted later".
Known consequences: wasmtime-py's own tests (the API axis in TESTING.md) write modules in WAT, so
that CI job installs the extra; the CLI's `run file.wat` and `wast` need it too; text errors come
from wasm-tools. Where no native wasm-tools wheel runs, a later option is to run `wasm-tools.wasm`
(wasm-tools built for WASI) on waboros itself once WASI works: native wasm-tools if installed, else the
wasm build on our own engine (maintainer: a good idea; phase 6 or later).

### D22. Platform tiers: iOS/Pythonista and MicroPython are tier 3
Status: accepted · Date: 2026-10-30 · Supersedes: — · Evidence: —

The primary platforms are CPython and PyPy on the operating systems of the CI matrix (TESTING.md).
iOS/Pythonista and MicroPython are **tier 3**: best effort, never a reason to constrain the design
or to block a step. On iOS/Pythonista wasm runs through the separate PyPI package `wasmhost`
(JavaScriptCore's `WebAssembly` via JSContext), not through waboros. The `mpy` target (ROADMAP phase 7)
keeps its place in the order of work at tier 3. D1 is unchanged: no runtime dependencies stays a rule
for every platform.

### D23. A JIT target is an architecture, not an OS
Status: accepted · Date: 2026-10-30 · Supersedes: — · Evidence: —

There is one JIT target per architecture and ISA baseline (`jit-x86_64`, `jit-aarch64`,
`jit-riscv64`, …), not one per operating system: the same artifact runs on Linux, macOS, the BSDs and
Windows. The code is position-independent and reaches everything through the context register
(ARTIFACT.md); the OS differences stay at the edges:
* calling convention: one entry trampoline per host convention inside the artifact (`sysv`/`win64`,
  `aapcs64`/`win-arm64`); host calls from wasm go through the same per-convention bridge;
* executable memory (`mmap`, `VirtualAlloc`, `MAP_JIT` + W^X switching) and the guard-page fault
  handler (signals vs vectored exceptions) belong to the loader/runtime, not to the code; explicit
  bounds checks behave the same everywhere;
* the code generator avoids what any supported OS reserves, so one artifact suits all of them: e.g.
  `x18` on AArch64 (Darwin, Windows), and frames that would need stack probes on Windows (machine
  frames stay small; values live on the host-allocated value stack). The context-ABI spec lists
  these rules per architecture.
Rejected: `jit-x86_64-sysv` / `jit-x86_64-win64` as separate targets (TARGETS.md before this): two
code generators or two artifacts for one CPU, with no gain.
Everything platform-specific is resolved by the runtime; the code depends on the architecture (and
its declared ISA level) only. Letting the OS into the code — e.g. using a register that only some
OSes leave free — is discussed only when a measurement shows a substantial gain on real workloads,
and then as an opt-in variant declared in the artifact, the way ISA levels are (ARTIFACT.md): a
generic variant that runs everywhere on the architecture and a flagged one that runs only where its
condition holds (as MicroPython's native `.mpy` has a generic RISC-V target and RISC-V with platform
feature flags).

### D24. Baseline ISA first; anything above it is an optional, declared variant
Status: accepted · Date: 2026-10-30 · Supersedes: — · Evidence: —

JIT code generators target the architecture's minimal baseline (ARTIFACT.md: x86-64-v1, ARMv8.0-A,
and each new architecture's declared baseline), so one compiled artifact runs on every CPU of the
architecture — portability comes almost for free. Extensions (SSE4.1, AVX, BMI, LZCNT, LSE, …) are
added later, operation by operation, only where a measurement shows a substantial gain, and always as
an optional variant behind a level declared in the artifact, next to a baseline path that stays
mandatory. What the PoC already uses above the baseline is kept as such an optional variant rather
than lost (`roundsd` for `f64.trunc`). CI disassembles generated code and fails on any instruction
above the declared level. With D23 (no OS in the code) this keeps an artifact dependent on the
architecture and its declared level only.

### D25. One target per engine and store; `pyc` and the JIT at feature parity
Status: accepted · Date: 2026-10-30 (amended 2026-10-30: no computation helpers) · Supersedes: D5 (per-module fallback) · Evidence: —

Targets are never mixed: the target is chosen per engine, so every module and store of an engine runs
on it (the Wasmtime model: an engine owns the compiler). `auto` chooses from the environment only —
`jit-<arch>` where the architecture is supported and executable memory works, otherwise `pyc` — never
from a module's content. No per-module fallback: memories, tables and globals then never cross two
representations, and one call graph has one trap model. Inside `pyc`, the memory's backing store
follows the environment: an `mmap` buffer where `mmap` is available, a `bytearray` where it is not,
with identical semantics; which one is the default is decided by a measurement (grow without copying,
access speed through `struct`/`memoryview`).
For this to cost nothing, **`pyc` and every `jit-<arch>` support the same features, 1:1**: a feature
is complete when it passes on both. The goal is a complete implementation, not passing tests: a JIT
computes natively (floats and conversions included, at the ISA baseline, D24) and calls into the
Python runtime only for runtime **services** that change engine state (`memory.grow`, `table.grow`,
table/segment initialisation — what Wasmtime's own libcalls do); it never hands a computation it has
not implemented to a Python helper. What it cannot compile yet is `Unsupported`, visible as
`unsupported: <feature>` in its expected failures, and `auto` selects a JIT only when its expected
failures equal `pyc`'s in the claimed groups. `pyc` closes its own gaps (e.g. NaN payloads) rather
than relying on the JIT. CI compares the expected-failures files of `pyc` and each JIT; a difference
is a gap on one side to close, not a permanent state. Tier-3 targets (`mpy`, D22) are exempt.

### D26. The JIT core is one pass; LIR is the backend's call interface
Status: accepted · Date: 2026-10-30 · Supersedes: D16 (LIR as a materialised list) · Evidence: S0002, "Proposed decision" (measured)

The shared JIT core reads each validated function body once — lazy value stack, control stack,
pinned locals, labels — and drives a small per-architecture backend interface (Winch / Liftoff
style, as `poc/w2jit2.py`). The LIR of D16 is that interface, not a list with virtual registers: an
extra LIR build + liveness + linear scan costs at least +50–80 % compile time in Python (measured on
core.wasm, webp_codec.wasm, resvg.wasm; `poc/bench/compile/lir_proto.py`), while what it could buy
is bounded: `w2jit2` is at 80–86 % of Cranelift on CoreMark and guard pages bring ~91 % — the gap is
mostly bounds checks, addressed by guard pages and bounds-check elimination, and wasm arrives already
optimised by its producer. The interface is three-operand, has no flags (compare-and-branch /
compare-and-set are whole operations), and lets each backend own its register roles, so a second
architecture does not inherit x86-isms. An allocator over a materialised LIR is added only if a
measurement on real workloads shows a substantial gain.

### D27. `pyc` caps a table at 10 000 000 elements (deviation from Wasmtime)
Status: accepted · Date: 2026-10-30 · Supersedes: — · Evidence: wasmtime-py 50.0.1 grew a funcref table by 10 000 001 and then to 120 000 001 elements without failing

`table.grow` on `pyc` returns −1 when the new size would exceed 10 000 000 elements (or the table's
max, or 2³²−1). A `pyc` table is a Python list (8 bytes per slot plus the objects), so an unbounded
grow can exhaust the interpreter's memory instead of failing the instruction; Wasmtime has no such
default cap (measured). The cap becomes a store limit (`StoreLimits.table_elements`) in phase 6; the
JIT targets will use the same number until then so `pyc` and JIT behave alike (D25).

**Applied to i64 tables (memory64, S0009 step 1, 2026-11-04):** an i64 table keeps the same cap (`pyc`
`_tgrow64`, −1 as an i64). Wasmtime reports out-of-memory instead of −1 for a grow of an i64 table by
10⁸ or 2⁴⁰ (`table_grow64.wast:46` is a Wasmtime expected failure for that reason); a min that
cannot be allocated is an instantiation error on both. The cap applies to `table.grow` only: no table min is
capped at instantiation, as in Wasmtime, and a min that cannot be allocated (i32 or i64) is
`MemoryError("out of memory (failed to allocate N bytes)")` on every target (maintainer, S0009 step 3). Unlike Wasmtime, both targets allocate a table eagerly (8 bytes per element resident: a min of 10⁸
costs about 800 MB), and a table whose min is above the cap cannot grow at all (`table.grow` of 0
returns −1; Wasmtime returns the size).

### D28. `pyc` keeps floats as Python floats; f32 NaN payloads ride in the double's high bits
Status: accepted · Date: 2026-10-30 · Supersedes: — · Evidence: spec run on pyc +83 (NaN/conversion causes), CoreMark unchanged

f32 and f64 stay Python floats in `pyc` (no f32-as-integer representation: it would make f32
arithmetic 3–5× slower). An f32 NaN is held as the double NaN with the same sign and its 23-bit
payload shifted left by 29, the quiet bit as is; conversions between f32 bits and floats use integer
bit arithmetic on the NaN path only (`struct` `'<f'` quiets signalling NaNs). NaNs are quieted only
where Wasmtime quiets them: arithmetic, promote/demote (`x*1.0`), floor/ceil/trunc/nearest; moves,
loads/stores, reinterpret, select, neg/abs/copysign keep bits exactly. f32 arithmetic is computed in
double and re-rounded to f32 once per operation (proved exact for add/sub/mul/div/sqrt); i64→f32 is
rounded once (sticky bit). A JIT libcall for an f32 operation reuses the same helpers (D25 parity).

### D29. JIT bounds checks: guard pages by default where the handler is verified; explicit checks as the fallback
Status: accepted (its "guard by default where verified" superseded by D43, then restored by D44) · Date: 2026-10-30 · Supersedes: — · Evidence: BENCHMARKS.md "Guard pages in the JIT" (PoC: explicit ~78 %, guard pages ~91 % of wasmtime on CoreMark)

The bounds-check mode is a compile-time property of JIT code, recorded in the artifact and the cache
key (`guard` or `explicit`). As in Wasmtime (`signals_based_traps = true` by default), the default is
`guard` wherever the platform has a verified fault handler; `explicit` is the fallback and the only
mode of a new architecture, of platforms without a handler table entry yet, and of `memory64`.
Guard code never runs without its handler: before the first entry the runtime checks that our
handler is installed and still ours (a cheap `sigaction` query; Windows: the vectored handler) and
otherwise compiles the module `explicit` (never crash the interpreter). Layout as Wasmtime: 4 GiB
reservation per 32-bit memory plus a 32 MiB guard; static offsets beyond the guard keep an explicit
check. `Config.signals_based_traps = False` selects `explicit` (API.md). Implemented in phase 5 (risks:
RISKS.md R6); phase 3 is `explicit` only and must not close this path (trap stubs reachable from a
fault handler, `SAVED_SP` kept, the mode field in the artifact).

### D30. Cross-instance tail calls go through a shared `tail_switch` trampoline (context word 9)
Status: accepted (maintainer, 2026-10-30) · Date: 2026-10-30 · Supersedes: — · Evidence: `tests/jit/test_tail.py` (1e5 A↔B hops on a 256 KiB thread; with the restore frame per hop the same test ends in `STACK_OVERFLOW`)

Tail calls between wasm functions compiled by the JIT never grow either stack, as in Wasmtime.
Deviation (temporary): a `return_call` to a function import is a call plus a return through the host
bridge, so tail recursion through an imported wasm function grows the stack (A `return_call`s an
imported `pong` that `return_call_indirect`s back: Wasmtime runs 10⁶ rounds, both waboros targets trap
`STACK_OVERFLOW` at 10³). It is closed by calling an imported function of another JIT instance of the
same store directly through its function record (closed by D32). A `return_call*`  moves its arguments to its frame
base, drops its native frame and jumps. When the callee's function record has another context, r15
must change, but a direct `call` site in the original caller relies on r15/r13/r14 being its own
afterwards. The jump therefore goes through `tail_switch`, one trampoline per process whose address is
in context word 9 `TAIL_SWITCH`. The first switch of a chain pushes the caller's r15 and makes a real
`call` whose return point (`restore`) pops it and reloads r13/r14. A later switch whose return
address is already `restore` only changes the context and jumps, so A → B → A → … runs in constant
stack. Every `ret` returns to its own `call` (CET shadow stacks, R2). This is an amendment of context
ABI v1 without a version bump: S0003 fixes v1 in phase 5, and word 9 was reserved and zero.
Rejected: (A) a restore frame (push r15, call, pop, ret) emitted at each cross-instance tail call, with
no ABI change. It costs 16 bytes of native stack per hop that does not return, so mutual tail recursion
across instances ends in `STACK_OVERFLOW` after ~32k hops (512 KiB budget). Also rejected: making r15
caller-saved at every call (Cranelift's `tail` convention), which costs a save/restore at every direct
call on the hot path.

### D31. The JIT fallback warns once (`JitUnavailableWarning`)
Status: accepted · Date: 2026-10-30 · Supersedes: — · Evidence: tests/test_fallback.py (MDWE), CI `spec` job

Whenever `auto` selects `pyc` it says so once per process and message (D25):
`warnings.warn(msg, JitUnavailableWarning)`, a `RuntimeWarning` defined in `targets/base.py` (phase 6
re-exports it). Two messages: `waboros: jit-x86_64 unavailable (<reason>); using pyc` when the JIT for
this machine's architecture exists but its probe fails (executable memory forbidden, ...), and
`waboros: no JIT target for <machine>; using pyc` on an architecture without a JIT target (maintainer:
the fallback is reported there too). A third (S0004): `waboros: jit-aarch64 incomplete (integer core
only); using pyc` when the probe succeeds but the target is not `complete` (its expected failures
are not yet `pyc`'s, D25). No message when the target was chosen explicitly; an explicit
`jit-*` that cannot run is an `Unsupported` error. Rejected: `print` to stderr (cannot be filtered or turned into an error, noisy in
libraries); `logging` (silent by default, so the one case where speed silently drops would go unseen).

### D32. Every function import is called through its function record; host imports share the path
Status: accepted · Date: 2026-10-30 · Supersedes: — · Evidence: `tests/jit/test_tail.py` (`test_tail_recursion_through_a_function_import`: 10⁶ ping/pong rounds through an imported function as in Wasmtime, none through Python); BENCHMARKS.md "function imports through their records"

Closes the deviation of D30. Every function import has a function record in the importer's context
(the exporter's `{code, type id, context}` for a function of another JIT instance of the same store,
the module's host thunk otherwise), and `call`/`return_call` of an import go through it as
`call_ref`/`return_call_ref` do (no null check): a call into another instance costs ~3 ns instead of
~4.8 µs through the bridge and a nested entry, and tail recursion through an import runs in constant
stack (`tail_switch` when the context differs). `pyc` closes the same case with `_TC` (a `return_call`
of an import returns its trampolined body). Host imports take the same path, one extra call through
the thunk: the code is compiled before linking, so a call site cannot know the kind of import, and
the measured host-call cost is within the noise of the ctypes callback (~1.45 µs either way).
Rejected: a separate bridge call site for host imports (more code at every import call site, no
measured gain). Context ABI: the records were already a section of v1 (3d); records for imports are an
amendment of v1 without a version bump (S0003: v1 is fixed in phase 5; no artifact is saved yet, and
the import words keep their bridge callbacks, so code compiled before still runs).

### D33. CPU and OS features beyond the baseline: explicit opt-in first, automatic detection later
Status: accepted (maintainer, 2026-11-01) · Date: 2026-11-01 · Supersedes: — · Evidence: —

Refines how the optional variants of D24 are switched on. Stage 1: only an **explicit** request
selects code above the baseline — `Config.isa` set to a level (`"x86-64-v2"`, `"x86-64-v3"`) or a
level plus feature names, like the platform flags of MicroPython's native `.mpy` targets (a generic
RISC-V target runs everywhere, a flagged one only where the feature exists). The default stays
`"baseline"`. A requested level the running CPU lacks is refused at load with an error that names the
missing features (detection by `cpuid` / `getauxval` / `sysctlbyname` / `IsProcessorFeaturePresent`,
ARTIFACT.md part 1) — never executed, never a crash. Stage 2, later: `"native"` (detect and use what
the CPU has, recorded in the artifact and the cache key), first for local cache artifacts only.
Making `"native"` the default, as Wasmtime does, is a separate decision taken only with a measurement
showing the variants pay and CI covering both paths (baseline and variant on the same tests).
The same rule holds for **OS-specific** code, should one ever pay (D23 keeps artifacts independent of
the OS; the maintainer reserved revisiting that if register or ABI limits give a substantial measured
gain): such a variant is never the default, is requested explicitly, is declared in the artifact next
to the ISA level (an OS tag), is refused on another OS, and needs its own decision amending D23.
Rejected for now: automatic detection by default (a cache or artifact compiled on one machine would
need the detection and recompile path to be proven before it is safe).

### D34. Host-call status goes through context word 7, preset to HOST_ERROR
Status: accepted (maintainer, 2026-11-01) · Date: 2026-11-01 · Supersedes: — · Evidence: `tests/jit/test_abi.py::test_async_exceptions_in_host_calls_never_give_wrong_results`

When an asynchronous exception (`KeyboardInterrupt` from a signal handler, `SystemExit`) arrives in a
ctypes callback outside its `try`, ctypes prints "Exception ignored" and leaves the C return value
**uninitialised**: the JIT read whatever was in `eax` as the host call's status — wrong results
reported as success, garbage trap codes, the store's depth left behind (step 6 review: 46 wrong
results out of 223 calls with a 2 ms SIGALRM). So the status no longer travels in the return value.
The bridge presets context word 7 (`HOST_ERROR`, unused until now) to 13 before calling the host
function and returns the word afterwards (putting back the value it found, an enclosing host call's
preset, so nested calls on one context do not see each other's statuses); the callback writes its
results, then its status to word 7 as its very last action. A callback that does not finish leaves 13
with no exception pending: the runtime raises `RuntimeError` "host call interrupted" (the escaped
exception itself is gone: ctypes swallowed it). An exception the callback catches after the host
function returned (in the guard-mode `guard.broken()` check) fails the call too: the status is 13
with that exception pending, never 0 with a stale one left in the store; and the outermost entry
drops a pending exception on exit, so one left by an interrupt right after the entry call returned
cannot surface in a later call (LESSONS 82). Wasmtime keeps the outcome of a host call apart from
the native return value too (the trap or error is recorded in its per-call `CallThreadState`, not
decoded from a register). wasmtime-py's trampoline (`wasmtime/_func.py`, `trampoline`: `caller =
Caller(caller)` runs before its `try`) has the same window and does not close it. This amends context
ABI v1 without a version bump: S0003 fixes v1 in phase 5 and no artifact has been saved yet.

### D35. Cross-compilation: any host compiles JIT artifacts for any architecture waboros has
Status: accepted (maintainer, 2026-11-01) · Date: 2026-11-01 · Supersedes: — · Evidence: —

The JIT compiler is pure Python and only writes bytes; it never runs what it emits while compiling.
With artifacts that depend on the architecture and its declared ISA level only (D23, D24), any host
can compile for any `jit-<arch>`: `jit-aarch64` on an x86-64 machine and the reverse, one artifact
per architecture for every OS. Like Wasmtime's `--target` / `Config.target`: `Config.target` names
the target (API.md); `waboros compile --target jit-aarch64` writes a portable artifact. Rules: a
cross-compiled artifact is never run on the compiling host — the loader refuses another architecture
or a level the CPU lacks (ARTIFACT.md part 1); only an explicit ISA level is allowed (`"native"`
means the compiling CPU, D33); it is tested where it runs (native CI runners or QEMU). From phase 4,
CI checks that an artifact compiled on x86-64 for aarch64 is byte-identical to the one compiled on
the arm64 runner, and runs it there, so the compiler's independence from its host is tested, not
assumed. Saving artifacts to files (format, `waboros compile`, `Module.serialize`) comes in phase 5;
until then cross-compilation exists inside a process only. `pyc` and `mpy` are not cross-compiled in
this sense: a code object belongs to one Python version and implementation (marshal magic), an
`.mpy` file to one `.mpy` format version, so their "target" is the interpreter, not the CPU.

### D36. JIT artifacts are executable from other languages; the C reference loader is part of CI
Status: accepted (maintainer, 2026-11-01) · Date: 2026-11-01 · Supersedes: D18 in part (the bullets "runs only on a waboros runtime", "the interface … is internal and may change between any two versions" and "loaders in other languages and the C reference loader become optional"), and the D15 narrowing · Evidence: —

A JIT artifact talks to its host only through the context words and C-convention functions
(ARTIFACT.md part 3: `entry(ctx, frame, code)`, `host(ctx, slots)` with the status in `ctx[7]` (D34),
records, libcalls), with no Python in it (D15, D23). So a host written in another language (C, Rust,
Go, Dart, LuaJIT, …) can load and run it, and that is now a supported use, not an accident:
* **Versioned, not internal.** From phase 5 the artifact format and the context ABI are a stable,
  versioned contract (`format_version`, `ctx_abi_version`): any change bumps the version (CLAUDE.md
  "The JIT artifact is a contract"), and every loader — waboros's and others — refuses a version it does
  not know. The waboros runtime keeps D18's behaviour on its own side (regenerate from the wasm on a
  mismatch); a foreign host refuses.
* **The runtime services are specified, not only implemented in Python**: `memory.grow`, `table.grow`,
  segment init and drop, the canonical type-id registry and records, stack limits, the interrupt word,
  trap codes — written down in ARTIFACT.md precisely enough to implement without reading our code.
* **A C reference loader is part of the test suite and runs in CI** (phase 5, together with fixing the
  format): it loads artifacts written by waboros, provides the services, runs a set of spec modules and
  CoreMark, and must agree with the Python runtime; it also checks that unknown versions are refused.
  It stays a test tool, not a shipped runtime.
* `pyc` and `mpy` artifacts are interpreter bytecode and run only in their interpreter.
Until phase 5 the contract may still change (D30, D32, D34 were amendments of v1); foreign hosts are
supported from the first fixed version.

### D37. Multi-memory: memory indices in the shared instruction tuples; libcall immediates only with several memories
Status: accepted · Date: 2026-11-01 · Supersedes: — · Evidence: spec `test/core/multi-memory` 912/912 on `pyc` and `jit-x86_64`; code of every single-memory spec module and CoreMark byte-identical before and after (review of the step)

Multi-memory (Wasm 3.0) on both targets together. **Frontend contract** (shared by every target): the
memory index is appended at the end of each instruction tuple, so existing positions do not move —
loads/stores `(op, offset, align, mem)`, `memory.size`/`memory.grow` `(op, mem)`, `memory.init`
`(op, segment, mem)`, `memory.copy` `(op, dst, src)`, `memory.fill` `(op, mem)`; the validator
checks `mem < number of memories`. Memories are numbered imported first, then defined. `pyc` names
memory k `M<k>` (memory 0 stays `M`). The JIT keeps memory 0 in r13/r14; memory i > 0 reads base and
size from its context words at each access, as Wasmtime does for memories it cannot keep in registers.
**Context ABI:** the layout already indexed memories; the `memory.grow` and `memory.init` libcalls get
the memory index as one more slot immediate **only when the module has more than one memory**, and
the runtime decides by the same count — so single-memory code is byte-identical to before (measured).
This is an amendment of context ABI v1 without a version bump, as D30, D32 and D34: no artifact can
have contained more than one memory before, none is saved yet, and S0003 fixes v1 in phase 5 (D36:
from then on such a change bumps the version). Rejected: always passing the index (simpler to state,
one more `mov` at every grow/init site of every module, and every existing artifact's code changes).

### D38. Context ABI v1: a per-architecture table (aarch64)
Status: accepted · Date: 2026-11-01 · Supersedes: — · Evidence: S0004 (approved by the maintainer, 2026-11-01); tests/jit/test_abi_aarch64.py; x86-64 code byte-identical (562 blobs)

The context words, records, libcalls and trap codes stay the same for every architecture (64-bit
little-endian words addressed from the context register). What differs — bridge registers, what the
bridge preserves, `tail_switch`'s record and return-address registers, landing pads, `CODE`
alignment, I-cache maintenance — is a table in ARTIFACT.md part 3 with an x86-64 and an aarch64
column; aarch64's register roles and trampolines are in S0004. This is an amendment of context ABI
v1 without a version bump, as D30, D32, D34 and D37: no aarch64 artifact exists yet, and S0003 fixes
v1 in phase 5 (D36). Rejected: an aarch64-specific copy of part 3 (two texts to keep equal).

### D39. The JIT backend interface is frozen after phase 4
Status: accepted (maintainer, 2026-11-01) · Date: 2026-11-01 · Supersedes: — · Evidence: S0004 step 4d review; two backends (x86-64 full, aarch64 integer core) on one core; x86-64 code and tables byte-identical across phase 4 (1714 modules); aarch64 spec core 5864 pass / 0 fail under QEMU and on ubuntu-24.04-arm

The backend interface of D26 (`common/backend.py`: its methods, operand kinds, the `supported` opcode
set, the far-mode retry `func_end -> bool` + `rewind`) and the `JitTarget` glue (`arch`, `machines`,
`complete`, `probe_code`, `backend`, `trampolines`) are frozen as of the review fixes of phase 4d (2026-11-01). The contract:
the core passes `Imm` (unsigned at the operand's width), `Reg` (`tos` or a pinned pool register) or
`Slot`; every value is zero-extended; `dst` is `tos`, with every other stack entry spilled out of it
but a popped operand possibly still in it; at every edge and call the operand stack is in slots and
only pins are live; no flags live between calls. A backend preserves its pool, frame, context and
memory-0 registers in every method, preserves `tos` in every method that does not write it (except
table/bulk operations and calls), and reads its operands before writing `dst`. "Frozen" means a
change of a method, its operands or this contract needs an amendment of S0004 (or a successor spec)
approved by the maintainer, the change in **both** backends in the same step, x86-64 code
byte-identical unless the amendment says otherwise, and a `reviewer` pass. Adding an opcode to a
backend's `supported` set, or a method only a new opcode reaches, is not a change of the interface.
Deliberately open: a float/SIMD register class (floats are bit patterns in integer registers and
slots today), guard pages instead of explicit bounds checks (D29), fuel/epoch words, what a third
architecture needs (Python backend, copy-and-patch or a wasm micro-compiler, ROADMAP q14), and moving
the opcode sets out of `common/compiler.py`. Measured for that decision (S0004 4d): backend+encoder+
abi ~930 code lines on aarch64 (integer core) vs ~1490 on x86-64 (full); compile 0.19–0.22 vs
0.14–0.18 ms per module.

Amended six times: S0005 (D41) added the `simd` method and the meaning of a v128 operand, in both backends, approved by the maintainer; then S0005 (D39 amendment item 5, Changes of 2026-11-02) added `move_v128`, which moves a v128 slot pair or writes a 128-bit constant as one 16-byte store (the store-forwarding fix, LESSONS 44), in both backends, approved by the maintainer. S0007 (amendment A, step 3) added the operand kind `AddK` (a pinned i32 plus a constant mod 2³²), accepted only by `move` and as the `load`/`store` address, in both backends, approved by the maintainer with x86-64 byte identity waived (amendment F). S0009 (amendment B, step 2a, 2026-11-04; maintainer, Q3 (b)) gave every method that addresses a memory or a table a **required keyword-only** `a64` (the i64 address type; a `(dst, src)` pair for `memory_copy`/`table_copy`), and `load`, `store` and `simd` a required keyword-only `lo` (an i64 memory's declared minimum in bytes, a lower bound of its size at run time; 0 for an i32 memory or a non-memory operator), neither with a default, so a forgotten argument is a `TypeError`, never i32 checks for an i64 memory. With `a64` the address or index operand is a full 64-bit value (a copy's `n` only when both sides are i64) and no `AddK` is passed; with `a64` false the method emits exactly the earlier code. In both backends; the code of every module without an i64 memory is unchanged on both JITs, guard and explicit (D35 lists; CoreMark included). The table methods assert `not a64` until S0009 step 3. S0013 step 3a (2026-11-11, maintainer's Q9 decision of 2026-11-06): every method that emits a call (`call_direct`, `call_record`, `call_indirect`, `call_ref`, `call_host`, `host_thunk`, and `throw` through its refill call) appends a site record `(ret, ret_at, up)` to `Backend.sites`, in both backends; no operand or instruction changes, code byte-identical on both JITs in both bounds modes (D56). S0013 step 5.6 (2026-11-16, maintainer's D6 of S0013 "Step 5 decisions", item (i) of the plan): `func_type(dst: Reg, rec: Operand)` loads word 1 (the canonical type id) of the function record at address `rec` (x86-64 `mov dst, [rec + 8]`, aarch64 `ldr dst, [rec, #8]`; a slot or an immediate goes through `dst` first, never `xzr` as a base), writes only `dst`, no flags; in both backends, tested against llvm-mc; only the code of modules with a function-subtype site changes (D35 lists: the 12 `gc/type-subtyping` modules 29-40, on both JITs and in both bounds modes).

Added without an amendment (the rule above: a method only a new opcode reaches): S0008 (Q2 (a), step 3) added `wide(op, dst: Slot, args)` for the four wide-arithmetic opcodes `0xFC13`-`0xFC16`, in both backends: the low and high 64 bits into slots `dst.k`, `dst.k + 1` (the first operand's slots, each operand read before its slot is written), may write `tos` (the core spills it first), no flags live afterwards, no trap; read by a `reviewer` (S0008 step 4). The code of every module without these opcodes is unchanged on both JITs (D35 lists).

### D40. The project is renamed `waboros` (from `w2py`)
Status: accepted (maintainer, 2026-11-01) · Date: 2026-11-01 · Supersedes: — · Evidence: the name `waboros` is free on PyPI (`https://pypi.org/pypi/waboros/json` -> 404 on 2026-11-01)

`waboros` = "WASM Ouroboros": a compiler that can compile itself, the idea of the micro-compilers
(ROADMAP q14, [MICRO-COMPILERS.md](MICRO-COMPILERS.md)), and close in spirit to Python (snakes).
The rename is **one separate step after SIMD**, before guard pages: the package, the distribution and
import names, the CLI, the docs, CI, the artifact's identifying strings if any. Until that step the
code and docs kept `w2py`. The step uses `waboros` everywhere at once, repository URLs included
(`natify-sh/waboros`), without waiting for the GitHub repository: the maintainer renames it last
(GitHub then redirects the old URLs; the new ones resolve only after that rename).
Packaging lands with the rename (maintainer, 2026-11-02), following the other natify-sh repositories
(py-wasmhost, py-wasmext): a LICENSE (MIT, copyright natify-sh, as fusedwasi), `pyproject.toml` ready for
publishing, the build backend
setuptools + setuptools-scm (version from git tags), and `.github/workflows/coverage.yml`,
`publish.yml` and `.github/dependabot.yml`. The runtime keeps no dependency.
The rename is done (2026-11-02): `w2py` survives only in `poc/` and in the docs that name PoC files or
PoC measurements; the environment variables are `WABOROS_*`, with no aliases (nothing was published). The artifact magic is `WABOROS\0` with no format version bump
(2026-11-02): no artifact is serialized before phase 5.

### D41. SIMD: v128 in two slots, no vector register class, native SSE2/NEON only
Status: accepted (maintainer, 2026-11-01) · Date: 2026-11-01 · Supersedes: — · Evidence: [S0005](specs/S0005-simd.md) (simd/ 25 990 commands; 236 operators: 158 at 1–3 SSE2 instructions, 68 at 4–12, 10 without an SSE2 form; pyc value model timed)

Fixed-width SIMD follows S0005. `pyc` keeps a v128 as a Python `int`; the JITs keep it in two
consecutive slots (the two-slot rule ARTIFACT.md v1 already states; no version bump). The D39
interface is amended by one `simd` method and the v128 operand meaning; vector registers are scratch
inside one call (`xmm0`–`xmm5`; `v0`–`v7`, `v16`–`v31`), so no register class, no trampoline change
and no save around host calls; a vector `tos` only after a SIMD benchmark shows it pays. Every
operator gets native code at the baseline (SSE2, ARMv8.0-A NEON): Cranelift's libcalls for `pshufb`
and vector rounding on SSE2-only CPUs are computation helpers, excluded by D25; an x86-64-v2 variant
only later under D33 with a measured gain. A target claims `simd/` with at most 25 listed failures
and no whole-file, `unsupported:` or `wrong kind` entries; `pyc` claims first, and until the JITs
claim, `auto` keeps the JIT and SIMD modules fail there as today. SIMD code lives in its own modules
(the maintainer's three plus `frontend/simd.py` and `jit/common/simd.py`). Opcodes in the shared
tuples are `0xFD000 + sub`. Relaxed SIMD and a NaN canonicalisation option are later work.

### D42. ISA variants above the baseline will be added, recorded per operator
Status: accepted (maintainer, 2026-11-02) · Date: 2026-11-02 · Supersedes: — · Evidence: [ISA-LEVELS.md](ISA-LEVELS.md) (draft); the WebAssembly SIMD CG's per-ISA mappings (WebAssembly/simd#418)

The JITs will grow variants above the baseline (x86-64-v2/v3, named aarch64 features). How they are
added is fixed now; each variant still needs its own spec. For every ISA-sensitive operator (SIMD
and scalar) we keep the sequence emitted at each level in one per-operator map, generated from the
code's tables (each program carries its ISA level; today all are the baseline), with a status
(implemented, planned, not worth it). A variant enters only with a measurement (D24), opt-in and
declared in the artifact (D33), with the baseline artifact byte-identical, its own ISA gate and QEMU
CPU model, and the same spec parity as `pyc` (D25). Not planned: XOP, ARMv7 NEON, wider vectors;
AVX-512 only on a measurement.

### D43. Guard pages are opt-in; explicit bounds checks stay the default
Status: superseded by D44 (maintainer, 2026-11-02; accepted 2026-11-02) · Date: 2026-11-02 · Supersedes: D29 (only its "guard by default where verified") · Evidence: [S0006](specs/S0006-guard-pages.md) measurements (CoreMark +7.5 % median over 9 alternated pairs, the PoC's own pair +6.9 % on this machine; code −21 %)

Guard pages (S0006) are a JIT option chosen explicitly (`bounds = "guard"` or `"auto"`); the default
stays `explicit`. The rest of D29 holds: the mode is a compile-time property recorded in the artifact
and the cache key, explicit checks are the only mode of a new architecture, of memory64 and of hosts
without a verified handler. Making `guard` (or `auto`) the default needs measurements on several
machines and a new decision.

### D44. Guard pages by default where verified (`bounds = "auto"`); explicit checks by flag
Status: accepted (maintainer, 2026-11-02) · Date: 2026-11-02 · Supersedes: D43 · Evidence: [S0006](specs/S0006-guard-pages.md) step 1 (CoreMark +14.3 % median over 7 alternated pairs on a loaded machine, +7.5 % on a quiet one in the draft; code −20 %); BENCHMARKS.md, "Guard pages" (quiet machine, 9 pairs: +9.35 % median)

As in Wasmtime (`signals_based_traps` on by default), the JIT's default bounds mode becomes `auto`:
guard pages on every (OS, architecture, libc) row whose handler is verified in CI, explicit checks
elsewhere, silently. `bounds = "explicit"` turns guard pages off; `"guard"` requires them. The switch
lands in its own step after step 1's review and a measurement on a quiet machine; until then the
default stays `explicit` (D43). The rest of D29 holds. The handler's chaining follows Wasmtime with
one deliberate difference: a sent signal whose previous action is the default is re-sent with
`raise`, so the process ends as it would without us (S0006 Changes, LESSONS 46).

**Done** (2026-11-02): `auto` is the default; Linux glibc x86-64 is the first verified row (glibc
named by `platform.libc_ver()`, so musl stays explicit); an empty `WABOROS_JIT_BOUNDS` counts as
unset. The switch also brought the Q1 check's rule A (`dladdr` tells the interpreter's handlers
from a library's, so wasmtime above ours under faulthandler is no error) and the handler's refusal
to install in a process of another architecture (S0006 Changes).

**Linux glibc aarch64** (2026-11-02, S0006 step 2): the second row in `verified` (`jit-aarch64`),
so `auto` gives guard pages in an aarch64 Linux glibc process (QEMU user mode included); the
spec suite passes in both modes under QEMU, and CI's native `arm64` runner runs it and
`test_guard.py` in both modes. The row holds once the step's review and that runner confirm it.
Every other aarch64 row stays explicit.

**Linux musl x86-64** (2026-11-02, S0006's musl step): a third row in `verified` (`jit-x86_64`).
musl is detected positively, by its mapped `ld-musl-<arch>.so.1`, because `platform.libc_ver()`
names only glibc. musl's `struct sigaction` and its `sigaction`/`raise` were checked against its
source and agree with glibc's. CI `jit` runs the spec suite and `test_guard.py` in guard mode in a
`python:3.13-alpine` container. Linux musl aarch64 stays explicit: `jit-aarch64` needs libgcc's
`__clear_cache`, which the stock Alpine image lacks (S0004, S0006 Changes).

**macOS x86-64 and arm64** (2026-11-03, S0006 step 3): `darwin` joins `verified` in both targets,
so `auto` gives guard pages on macOS, Intel and Apple Silicon. Evidence: CI run 41610392553, fully
green: the spec suite on `jit-x86_64` (`os (macos-26-intel)`) and `jit-aarch64` (`arm64 (macos-latest)`)
and `test_guard.py` + `test_fault_handler.py` in guard mode on both. The handlers use signals (SIGSEGV and
SIGBUS), not Mach ports (S0006 Q5).

**Windows x64 and arm64** (2026-11-03, S0006 step 4): `win32` joins `verified` in both targets, so
`auto` gives guard pages on Windows. Evidence: CI run 41633456821 (`arm64 (windows-11-arm)` fully green:
the `jit-aarch64` spec suite, `test_guard.py` + `test_fault_handler.py` in guard mode) and CI run
41640149866 (`os (windows-latest)` fully green: the `jit-x86_64` spec suite and the same tests,
including the forced-CET shadow-stack tests and the faulthandler-order test). The handler is a
vectored exception handler, not structured exception handling.

### D45. In guard mode stores rely on guard pages, as in Wasmtime; a split store may tear
Status: accepted (maintainer, 2026-11-02) · Date: 2026-11-02 · Supersedes: — · Evidence: [S0006](specs/S0006-guard-pages.md) Changes; LESSONS 50; BENCHMARKS.md, "Checked stores in guard mode (rejected alternative)"

In guard mode every store of every width is a fault point, as every load is; nothing checks it
explicitly. Wasmtime does the same and accepts that a store straddling the end of memory may
commit its in-bounds part where the hardware allows it (its issue 7237; PR 8221 unmerged;
WebAssembly/design issue 1490 open). Native x86-64 faults are precise, so a trapping store writes
nothing there. An emulator that splits wide stores may tear one: QEMU's TCG split a 16-byte store
in CI (LESSONS 50), and box64 or FEX may too; Arm and RISC-V allow it in hardware. CI's qemu64
steps therefore run in explicit mode. Rejected: checking stores of 2 bytes or more (CoreMark −2
to −3 % against unchecked stores; guard would lead explicit by +4.5 to +6.3 % instead of about
+9 %, with 14.6 % less code instead of 20 %), and a probe, a 1-byte read of the store's last byte
as the fault point (measured, no faster than the check). D44 stands: the default stays `auto`.

**aarch64** (S0006 step 2): the same rule, as the brief decided: every store is a fault point. Under
QEMU 8.2.2 a straddling 16-byte `str q` wrote its first 8 bytes before the trap (scalar and 8-byte
stores wrote nothing). On aarch64 `test_guard.py` records a torn store rather than failing, and
CI's QEMU SIMD step runs in explicit mode. What native Arm hardware does is shown by the `arm64`
runner, not assumed.

### D46. Float constants cross the front end as bit patterns; `pyc` has a mode that carries f32/f64 as ints
Status: accepted (maintainer, 2026-11-03) · Date: 2026-11-03 · Supersedes: — · Evidence: [PYC-PLATFORMS.md](PYC-PLATFORMS.md) sections 5 and 6 (i686: 88 failures -> 0; CoreMark and the 64-bit output unchanged)

On 32-bit x86 the x87 FPU quiets a signalling NaN in any Python float, so `pyc` cannot keep f32/f64 NaN
payloads while those values are floats (LESSONS 63). Decided:
1. The decoder (`frontend/binary.py`, function bodies and constant expressions) yields an f32/f64
   constant as its bit pattern, an int. This keeps the shared layers language-neutral (D17): no NaN-as-double
   convention. The JIT takes the bits directly (its machine code is byte-identical: `tools/jit_dump.py`
   lists of both targets, both bounds modes); `pyc` turns the bits into the same float literal as before,
   so its default source is byte-identical (sha256 over `generate()` for CoreMark and every spec module).
2. `pyc` generates a second mode (`ModGen.fb`): f32/f64 are ints (32/64-bit unsigned) in every wasm
   value; moves, constants, load, store, select, neg, abs, copysign, reinterpret and the SIMD scalar
   boundary work on the bits, arithmetic, comparison, conversion, demote and promote make a float inside
   one expression and convert the result back; inside one expression a bits-to-float-to-bits pair is
   dropped (exact: an arithmetic result is a quiet NaN or no NaN). The host boundary (`Raw`) is bits already.
3. Selection: a behavioural probe at import (`runtime.FBITS`: a signalling NaN must survive the helper round
   trip), overridden by `WABOROS_PYC_FBITS=1|0` and `generate(mod, fb=...)`. Only a host whose floats quiet
   gets the mode by default; CI runs it on x86_64 as well (`ci.yml`).
4. `pyc.load()` (test helper) exposes bit patterns for f32/f64 in this mode.
5. `pyc` has no on-disk cache. A future `pyc` artifact or cache key must include the mode (`fb`), since the
   same wasm gives two different sources.
Rejected: a `float` subclass that carries the bits of a NaN constant (prototype; one more convention for
every consumer of the front end), f32 as bits everywhere (slows the CoreMark path of every host).


### D47. An optimisation that needs code per operating system starts on one row and grows only by exception
Status: accepted (maintainer, 2026-11-03) · Date: 2026-11-03 · Supersedes: — · Evidence: S0006 steps 1-5 (LESSONS 67)

"Per-OS code" differs by operating system, libc or OS-level runtime: signal or exception handling, OS
structures and offsets, OS APIs, process policies such as CET. It does not cover ISA variants (D24, D42)
or code every OS shares.

1. Such an optimisation is first built and verified on **one row**: Linux glibc x86-64 (the fastest
   CI, the reference machine). Every other row keeps the portable path, silently and without a
   warning, as `auto` does for guard pages (D44).
2. It is extended to another row **only when one of the exceptions below holds**; the step's brief
   names which one, with its evidence.
3. A row becomes the default only after it is **verified in CI on that OS** (a real runner, or an
   emulator that shows the behaviour), with a silent fallback to the portable path when the feature
   cannot be installed at run time.
4. Every per-OS row is listed in one table (as S0006's rows), with its CI job.

Exceptions (any one is enough):
* **(a) Required, not faster.** Without the per-OS code the JIT or a hard rule fails on that OS: it
  cannot run at all (macOS `MAP_JIT` and per-thread write protection, Windows `VirtualAlloc`), or it
  breaks "never crash the interpreter", W^X, CET or BTI compliance, or a trap the oracle gives.
  Correctness and safety are not optimisations.
* **(b) Large measured gain on that row.** At least **+10 %** median on CoreMark or on the corpus
  (`tools/run_corpus.py`), measured on that OS's CI runner (alternated rounds, as BENCHMARKS.md
  requires), **and** a stated need on that platform (a user, a maintainer's target, a program that is
  too slow there).
* **(c) Small marginal cost.** The new row reuses existing code with at most about **100 lines**
  specific to it, needs **no new CI infrastructure** (an existing job or container gains a step), and
  adds **no new interaction class** (no new handler mechanism, no new process policy). Example: guard
  pages on Linux musl reused the glibc handler unchanged (one detection function).
* **(d) The maintainer asks for it explicitly**, knowing the cost; the decision record says so.

Not exceptions: "Wasmtime does it" by itself (Wasmtime has a team per platform), completeness of a
table, symmetry between OSes, or a gain measured on another OS.

Existing per-OS work (S0006 on Linux glibc and musl, macOS, Windows and Android x86-64; executable
memory per OS) stays as it is. Briefs and reviews: a brief for anything per-OS names the rows it
touches and, for each row beyond the first, the exception and its evidence; a reviewer treats a per-OS
change without a named exception as out of scope.

### D48. Native JIT targets are x86-64, aarch64 and riscv64
Status: accepted (maintainer, 2026-11-03; amended 2026-11-04: the sljit track) · Date: 2026-11-03 · Supersedes: — · Evidence: [ARTIFACT-CONTAINER.md](ARTIFACT-CONTAINER.md) (Wasmtime 50.0.1 CLI, checked 2026-11-02: `--target i686-...` and `--target armv7-...` give "Support for this target has not been implemented yet"; 32-bit hosts get Pulley, its bytecode interpreter)

**Scope: the JIT under CPython** (desktop, server, mobile). MicroPython (the `mpy` target, phase 7) is a
separate question with its own architectures (Thumb, Xtensa, RISC-V 32 and others, through
MicroPython's own native emitters and viper); this decision does not limit it, and the maintainer may
decide differently there.

1. **Architectures.** A JIT target exists only for x86-64, aarch64 and riscv64: three of
   Cranelift's four 64-bit architectures (x86-64, aarch64, riscv64, s390x). x86-64 and aarch64 are
   done; riscv64 is planned, the first micro-compiler after `aarch64.wasm`
   ([MICRO-COMPILERS.md](MICRO-COMPILERS.md)); ROADMAP phase 8.
   **s390x is excluded by the maintainer** and runs `pyc` (IBM mainframes only). Reasons, recorded, not a plan: the host
   is big-endian and wasm memory is little-endian, so every load and store would byte-swap (s390x
   has byte-reversed `lrv`, `lrvg`, `strv`, `strvg`, `lrvh`, so the cost is small, but it is a new
   axis for the backend interface (D39) and for SIMD lanes); there is no native CI runner, only
   QEMU user-mode, which is slow; Wasmtime's own s390x backend would be the closest reference if
   this is ever reopened. `pyc` passes the spec suite on s390x under QEMU
   ([PYC-PLATFORMS.md](PYC-PLATFORMS.md)); the optional exotic QEMU job keeps covering it.
2. **Operating systems** stay open within those architectures: Linux (glibc, musl), macOS, Windows
   and Android are tested today, and the JIT artifact is OS-independent (D23). D47 governs per-OS
   *optimisations*; D48 is about which architectures get a JIT at all. They do not conflict.
3. **Every other architecture runs `pyc`**: s390x, i686 and 32-bit Windows, armv7, ppc64le,
   loongarch64, mips32el (Ingenic X2000) and the rest. `auto` already falls back with the D31
   warning. No hand-written 32-bit JIT and no LLVM long-tail backend is planned; the one possible
   exception is the sljit track below. `pyc`'s 32-bit
   issues are therefore real work, not stop-gaps (both are fixed: D46 and the OverflowError mapping).
4. **Pyodide** is a separate track with its own way of running code (the guest as an Emscripten
   side module loaded with `ctypes.CDLL`, compiled by the browser's engine, [PYODIDE.md](PYODIDE.md)).
   It is not a JIT target of ours and not part of the architecture list.

LLVM as an optional ahead-of-time tier for speed is a separate question and is not closed by this
decision. Rejected: a 32-bit pathfinder (i686), an LLVM tier for the long tail of architectures.

**Amendment (maintainer, 2026-11-04): the sljit track.** Native hand-written JITs exist only for
x86-64, aarch64 and riscv64. Other architectures run `pyc`; optionally, later, a sljit backend under
our core may give them a JIT: one more implementation of the D39 interface written against sljit's
API, shipped as a micro-compiler (`sljit-<arch>.wasm`) built from the same C/Zig port of the core,
verified behaviourally against `pyc` (spec suite under QEMU), not byte-checked by Python twins. Not
a first priority: it comes after the hand-written micro-compilers; until then it shapes one thing,
the review of the LIR vocabulary (every D39 operation should map onto one to three sljit calls).
The big-endian and 32-bit consequences below hold for the hand-written JITs, not for this track
(sljit has byte-reversed loads and stores; i64 on 32-bit targets needs register pairs, a D39
amendment of that track). Proposal, proof of concept and measurements:
[SLJIT-BACKEND.md](SLJIT-BACKEND.md), `poc/sljit/`.

Consequences (for the hand-written JITs): there is no big-endian JIT host, so the backend interface (D39) never needs
byte-reversed loads and stores and SIMD lanes stay in host order on every JIT target; the JIT CI stays
on native runners (x86-64, arm64) plus QEMU for riscv64 when it comes. The micro-compilers are
`x86_64.wasm`, `aarch64.wasm` and `riscv64.wasm`.

### D49. Relaxed SIMD: the deterministic profile of Wasm 3.0 for 16 operators, madd/nmadd native per target as Wasmtime; each target lowers all 20 itself
Status: accepted (maintainer, 2026-11-04) · Date: 2026-11-04 · Supersedes: — · Evidence: [S0008](specs/S0008-relaxed-simd-wide-arithmetic.md) (Semantics: Wasmtime 50.0.1 measured 2026-11-04, its deterministic switch fuses madd/nmadd on every host and equals the 3.0 profile on the other 16 operators; without the switch 23 928 of 60 000 calls differ from the profile)

1. **16 operators** (swizzle, the four trunc, the four laneselect, min/max of f32x4 and f64x2, q15mulr,
   the two dot products) give the deterministic profile's result (every relaxed parameter 0, alt 0) on
   every target, whatever the host CPU. `pyc` stays a bit-exact oracle for them.
2. **`f32x4`/`f64x2` `relaxed_madd` and `relaxed_nmadd`** follow Wasmtime's native choice per
   architecture: fused on `jit-aarch64` (`fmla`/`fmls`), unfused on `jit-x86_64` (SSE2 has no FMA) and
   on `pyc`. The result differs by target, never by host, so D35 holds. x86-64 FMA comes with
   x86-64-v3 (D42).
3. **No front-end aliasing.** The front end decodes all 20 relaxed opcodes as operations of their own;
   every target (`pyc`, `jit-x86_64`, `jit-aarch64`) maps each of the 20 itself, to its lowering of the
   equivalent operator or to one of 6 compositions. No shared default table. A target keeps the freedom
   to lower a relaxed operator natively when ISA levels are extended.
4. The oracle runs with `wasm_relaxed_simd_deterministic`; madd/nmadd are compared against the unfused
   (or, on aarch64, the fused) reference, not bit for bit against the oracle on x86.

Consequence: **revisit with ISA levels** ([ISA-LEVELS.md](ISA-LEVELS.md)): native results for the other
16 operators (`cvttps2dq`, `minps`/`maxps`, `pshufb` from SSSE3, hardware dot products) and FMA on
x86-64 arrive per target with a declared level, recorded per operator (D42); the front end needs no
change for it. Rejected: aliases in the front end (they erase which instruction was relaxed); unfused
madd everywhere (deviates from Wasmtime on aarch64); fused everywhere (a software FMA on SSE2).

### D50. An i64 memory moves when a grow exceeds its reservation; an i32 memory never moves; context ABI v1 amended in text only
Status: accepted (maintainer, 2026-11-04) · Date: 2026-11-04 · Supersedes: — · Evidence: [S0009](specs/S0009-memory64.md) ("Runtime"; Wasmtime 50.0.1 reservations measured from `/proc/self/maps`; the zero-skipping copy measured: 4 GiB + 64 KiB with 3 written pages, 1.46 s and +1 MiB RSS on the dev VM)

1. **Reservation as Wasmtime 50.0.1** (S0009 Q1 (b)): an i64 memory with a min of at most 4 GiB
   reserves `RESERVATION64` (4 GiB) + `TAIL64` (the 32 MiB guard); a larger min reserves min +
   `GROWTH64` (2 GiB) + `TAIL64`. A grow that fits in the reservation minus the tail commits in place.
   No cap beyond 2⁴⁸ − 1 pages (or the declared maximum).
2. **A grow beyond it moves the memory** (`Memory._move`): a new reservation of the new size +
   `GROWTH64` + `TAIL64` (a failure is −1 with nothing changed); the old contents copied 64 KiB at a
   time, skipping pages that read as zero (the new pages already do); the base and then the size
   rewritten in every bound context (`users`); then the old reservation freed. The rewrite is all or
   nothing: an asynchronous exception (`KeyboardInterrupt`) during it puts every context back on
   (old base, old size), size first, and the `Memory` back on the old reservation, then re-raises: the
   grow did not happen. Both reservations stay mapped while contexts are rewritten (`retired`); only a
   second interruption during the rollback leaves them there (a leak, never an unmap). Every write of
   a context puts the base before the size, and an in-place grow rewrites the base too, so no context
   is ever paired with a size larger than its base's commit (LESSONS 79).
3. **Why it is safe**: a move happens only inside `Memory.grow`, reached from wasm only through the
   host bridge; every bridge call site reloads memory 0's registers and every other memory is read
   from its context words at each access (S0009, "No stale base after a move"). A host thread is
   kept out by the one-thread-per-store rule (S0002, enforced at entry, grow and instantiation).
4. **An i32 memory never moves** (4 GiB + 32 MiB, as S0006), and its code bytes do not change (D35).
5. **Context ABI v1, text amendment without a bump** (S0009 Q4): an i64 memory's `base` and
   `size_bytes` may change during any host call or libcall; code keeps neither across a call. No
   word is added or moved; no artifact is serialized yet.
6. `Reservation` refuses a size ≥ 2⁶³ with `OSError(ENOMEM)` before any mapping call (ctypes
   truncates an oversized `c_size_t`, LESSONS 77).

Rejected (S0009, "Alternatives"): a 65 536-page cap (deviates from Wasmtime); a huge fixed
reservation (address-space exhaustion); copying every byte as Wasmtime does (23.3 s and +4 GiB RSS
for a 4 GiB memory); `mremap` (per-OS code, D47); freeing the old reservation only when the store is
idle (keeps two reservations alive; the thread rule is enforced instead).

### D51. On PyPy an asynchronous exception at a JIT entry or exit may leave the store's counters raised
Status: accepted (maintainer, 2026-11-04) · Date: 2026-11-04 · Supersedes: — · Evidence: LESSONS 82; D34's script (`tests/jit/test_abi.py`, `ASYNC_HOST`), 100 runs at 2 ms on PyPy 3.11 (8.0.1), dev VM

The entry (`Instance.call`, `JitInstance.call` for an exported import, `pyc`'s `_run`) restores the
store's depth, the value-stack `active` count and (outermost) the pending host exception in a
`finally` that makes no call before the restore. On CPython a signal handler runs only at a function's
entry, after a C call and at a backward jump, so a `KeyboardInterrupt` cannot skip the restore
(closed on CPython 3.11-3.15t, LESSONS 82). PyPy runs signal handlers at other points as well: one
was seen at the plain compare in the `finally`, and its ctypes callback wrapper is Python code. There
an interrupt at entry or exit can leave the depth and `active` raised (a later call may then report a
host exception wrapped in `HostError`, and the value stack is not reset for the next outermost
entry); never a wrong result, and the interrupt itself always surfaces. Measured (pass of 100 runs,
explicit / guard): before S0009 step 2b 67 / 82, at 2b 38 / 74, with the fix 63 / 98. Accepted as a
deviation: the stochastic test is skipped on PyPy; the deterministic delivery-point tests, which
model CPython's points, still run there. Closing it needs bookkeeping that does not rely on a
restore in a `finally` (for example reset when the outermost entry finds no JIT frame below): a later
item in ROADMAP.md, phase 5.

### D52. JIT exceptions: status 14 through frames, objects bumped from a store arena in code, context ABI v1 amended
Status: accepted (maintainer, 2026-11-05) · Date: 2026-11-05 · Supersedes: — · Superseded in part by: D63 (the arena, words 11-13, the object and `exnref` encoding, the methods `exn_*` and `throw`; status 14 propagation stays) · Evidence: S0012 steps 3-4; `tests/jit/test_exceptions.py`; BENCHMARKS.md "Exceptions: throw cost"

An exception crosses JIT frames as status 14 (`UNCAUGHT_EXCEPTION`), as traps do (S0012 Q1 A): each
call site's existing `test eax, eax; jnz` goes to the frame's `error` label, which is the epilogue
outside a `try_table` (code without one is byte-identical, D35 lists) and the innermost landing
pad inside one. A pad dispatches only 14; traps, interrupts, host errors and stack overflows pass
through, as in Wasmtime. Every `ret` matches its `call` (R2). The context ABI v1 is amended without
a bump (S0012 Q2 (a), as D34 and D50; no artifact file exists yet): word 10 the address of the
store's pending-exception word, 11 the arena refill service, 12 and 13 the addresses of the store's
arena cursor and limit, one tag-identity word per tag after the records (ARTIFACT.md part 3).

`throw` runs no Python (maintainer, plan review): the code bumps the object `{tag, payload}` from
the store's arena chunk, makes it pending and returns 14; only a full chunk calls the refill
service through the bridge. A first version called a throw service through the bridge for every
throw (2.1-2.8 µs per throw caught one frame up; BENCHMARKS.md). Words 12 and 13 hold addresses,
not the cursor and limit themselves, because every context of a store allocates from one arena: a
per-context copy of the cursor would hand out the same memory twice. Chunks are 64 KiB, never
move, and live until the store is dropped (S0012 Q3 (a): 8·(1 + payload words) bytes per throw; one
ctypes object per throw kept 137 bytes for a one-word payload). That keeps every exception a program
throws, where Wasmtime's collector frees unreachable ones: a deviation until GC (Q3 (c)).

At an entry status 14 becomes `Trap(UNCAUGHT_EXCEPTION)` with `exn = (Tag, values)`; an exnref seen
by the host is an opaque `ExnRef` of its store (S0012 Q4; the public shape is phase 6). A host
function's `Trap(UNCAUGHT_EXCEPTION)` is a host error (status 13), so an exception that left wasm
through a host frame is not caught again below it (wasmtime-py 50 behaves so). Before this step
the host-call status set was `frozenset(TrapCode)`, which had admitted 14 since S0012 step 2
(LESSONS 91).

D39 amendment (S0012 Q5): the frame's `error` label and six backend methods, `landing`,
`exn_match`, `exn_field`, `exn_load`, `throw` and `throw_ref`. `exn_match` and `exn_field` take
no exnref register, unlike S0012's first text: at a pad the result register holds the status and
every pool register may be a pin, so each method re-reads the pending word (maintainer, Q-A (a)).
`throw` is a method because its allocation needs registers the core cannot name. x86-64 has them
since S0012 step 3; aarch64 completes the amendment in step 4 (maintainer, Q-F), with the same
shapes in AArch64 (`cmp w0, #14; b.ne` at a pad, `cbnz w0` after every call to `error`, x9-x12
scratch); its code without exception instructions is unchanged (D35 list), and both JITs claim
`exceptions/`, so the D25 parity test needs no exemption any more (from S0013 step 2 it ignores exactly the JITs' `unsupported: GC types` lines until they claim `gc`, steps 3b-4). Not done: a `throw` caught in the same function as a plain jump. Two tags of
different indices are the same tag when both are imports of one export, so the first matching
clause is not always known at compile time. Nothing measured asks for it.

### D53. JIT memory semantics written once, over a per-architecture low layer
Status: accepted (maintainer, 2026-11-05) · Date: 2026-11-05 · Supersedes: — · Evidence: S0011 steps 1-4; D35 lists; BENCHMARKS.md "JIT low layer" and "S0011 step 4"

The JIT's memory semantics are written once, in `jit/common/lower.py`, and no longer in each
backend (LESSONS 76). The low layer under them is the `ll_*` methods of each backend: about twenty
operations that name the machine registers and fit every immediate to its encoding. The group
covered:
* the bounds checks of loads, stores and SIMD memory operators, i32 and i64, in guard and explicit
  mode;
* the end checks of `memory.copy`, `memory.fill`, `table.copy` and `table.fill`, with the widths
  and order of their operands;
* the table element check.

[S0011](specs/S0011-jit-low-layer.md) is the contract: the operations, the registers each writes,
flags, immediate ranges, and the fault-point rule. The low layer lies below D39 and is not part
of its frozen interface.

The bulk operators take aarch64's order per operator (S0011 amendment B2, maintainer):
* `memory.copy` checks a range, then forms its address, before the other range;
* `table.copy` checks both ranges, then forms both addresses.

So aarch64 code is unchanged, and x86-64 changes in the 70 spec modules with `memory.copy`, each a
reorder of the same instructions. One order for both operators would have changed 106 aarch64
modules.

Elsewhere generated code is byte-identical: CoreMark on both architectures in both bounds modes,
and every other spec module except `address64.0-3`, whose differences the maintainer accepted.
Speed is unchanged by construction (identity); a short `memory.copy` measured 0.99× after the
reorder. Compile time is within the +5 % bound (S0011). The trap code stays at each backend's
plug point, which creates the stub, so the shared layer never sees a frame (S0011 Q5).
`WABOROS_JIT_TWOLEVEL` and the per-backend bodies are removed (Q3). GC is to be written on this
layer.

### D54. Runtime modules written in wasm ship as source and bytes, tied by a test
Status: accepted (maintainer, 2026-11-10) · Date: 2026-11-10 · Supersedes: — · Evidence: S0013 Q4 (revised 2026-11-10); the review of 2026-11-10 and the proposal GC-COLLECTOR-WAT

A small module the runtime itself runs as wasm (the first is S0013's collector) is kept in the
package as its source and its committed `.wasm` side by side; the `.wasm` ships as package data.
The source is in one of two languages, chosen in the step's plan before the code:
* **WAT** (`.wat`), the default for a small module. A test assembles it with the development tools
  (wasmtime-py's `wat2wasm`, directly or through `tests/watcache.py`; the same bytes on every OS,
  S0013 step 5.2).
* **C** (`.c`), when the module is too large or too intricate to keep in WAT (maintainer). A test
  rebuilds it with a pinned toolchain (the PyPI `ziglang` wheel at a fixed version and hash, as
  `poc/bench/corpus/build.sh` does, or a pinned wasi-sdk) and fixed flags that keep the output
  reproducible (no debug info, fixed paths, a fixed target CPU and feature set). The same file is
  also built natively with sanitizers (address, undefined behaviour) and run on the same inputs as
  the wasm build, so memory errors are found in C, not in the JIT. The module's own data and stack
  live inside the memory it is given; where (for S0013, a region of the hidden memory's reservation
  outside every object) is fixed in the step's plan. The C code reaches its memory through a base plus
  u32 offsets, not pointers, so the native and the wasm builds run the same code. Whether the
  pinned toolchain gives the same bytes on every OS is proven once, or the byte-match test runs on
  Linux only.

The same for both:
* the test fails unless the bytes it builds equal the committed `.wasm`;
* a test decodes the module and fails on any instruction outside an allowed list (the core features
  the JITs support; nothing that would need a newer proposal or the GC itself), and checks its
  structure: the imports and exports its spec names, every data segment (and, for C, the stack)
  inside the region the spec gives it, no `memory.grow` unless the spec allows it;
* the module uses core wasm only, has no host import beyond what its spec names, is deterministic
  (D35), and traps on malformed input instead of writing outside its memory;
* nothing is built at install or at import (D1): the toolchains are development tools, and the
  package compiles the bytes with its own JIT, as it compiles any module.

The `.wasm` is the module's single portable form, the same shape as the micro-compilers
(MICRO-COMPILERS.md, JIT-AS-WASM.md): compiled by the JIT at load today, and, once the artifact
format exists (ROADMAP "Next" item 6), it may also ship as a per-architecture AOT artifact made from
those bytes, as the compilers will. The source and the match test stay the source of truth either
way. Another source language needs an amendment of its own (a pinned toolchain, reproducible
output).

Why: a module written once runs on every JIT architecture and ports to JIT-AS-WASM.md unchanged,
and the source is reviewable rather than a binary built elsewhere; the test keeps the two from
drifting. Rejected: building the module at run time (needs a parser or a compiler in the package,
D1); bytes without a source in the tree (unreviewable).

### D55. `pyc` on a small C stack: freeing a long chain of GC objects may crash the host (accepted)
Status: accepted (maintainer, 2026-11-11) · Date: 2026-11-11 · Supersedes: — · Evidence: LESSONS 103; S0013 step 2 CI (Pyodide)

CPython frees a chain of lists recursively in C, and its trashcan bounds that recursion too late for
a small C stack: on Pyodide (or Linux with `ulimit -s 128`) a chain of about 1 000 `pyc` structs
crashes the host when it is freed; on the default 8 MiB stack 3 million is fine. This is a
deliberate exception to "never crash the interpreter", accepted as a platform limitation instead of
a `pyc` object representation that is not nested (one object store per store and integer
references, as the JIT heap), which would rewrite S0013 step 2 and slow `pyc`. Recorded in TARGETS.md
(the Pyodide row); revisited if a real program on Pyodide needs long linked structures.


### D56. JIT roots: a return-address walk from an exit record, a site table in every module; context ABI v1 amended
Status: accepted (maintainer, 2026-11-06 and 2026-11-10: S0013 Q3, Q5, Q9, Q13) · Date: 2026-11-11 · Supersedes: — · Evidence: S0013 step 3a (Changes 3a.1-3a.4); the whole spec suite with `WABOROS_JIT_WALK_CHECK=1` on both JITs, both bounds modes; BENCHMARKS.md, "GC safepoints (S0013 step 3a)"

The JIT's copying collector (S0013) needs precise roots on the native stack, so every frame of a
store must be found without frame pointers or per-call stores:
* **Site records.** Every call site of every module is a safepoint, whatever its types. Each
  backend records per call site the code offset of the return address and two native numbers,
  `ret_at` (where this frame's own return address is) and `up` (where the caller's stack pointer
  is), both from the stack pointer at the call; the shared core adds the slot shift to the callee
  and a bitmap of the frame's reference slots (from the validator's `refmasks` and `v128masks`:
  value positions, which the core turns into slots, a v128 taking two). They form the artifact's
  `STACKMAPS` section (ARTIFACT.md part 2, id 15; two bitmaps per map since S0013 step 7, D65).
  Machine code does not change: the D39 amendment
  is an output of the call methods (`Backend.sites`), not an instruction.
* **Exit record.** Context word 14 (`EXIT_RECORD`) holds, in every context of a store, the address
  of the store's `{sp, pc, frame}`; the bridge writes it before every host call and clears `sp`
  after; every entry keeps the enclosing record and its frame on the native stack while it runs
  and puts the record back. The walk starts there and crosses nested entries by those saved
  blocks and `tail_switch` restore frames by fixed numbers. Cost: about 1 ns per host call (8
  instructions on x86-64, 6 on aarch64) and about 15 (x86-64) or 10 (aarch64) instructions per
  entry, nothing on a call between wasm functions; not measurable on host calls, entries or
  CoreMark.
* **Checked, not trusted.** The walk validates every return address against the store's site
  tables and reads only between the store's native limit and the thread's stack top; an
  inconsistency is a `RuntimeError`, never a guess. `WABOROS_JIT_WALK_CHECK=1` walks at every host
  call and compares the walk with the runtime's own record of its entries; CI runs one spec run
  per architecture with it.
* **ABI v1 amended without a bump** (S0013 Q5, as D34, D50 and D52): no artifact file has been
  written, the trampolines are the runtime's own, word 14 was reserved, and module code is
  byte-identical. The hard rule's test is that a compiled module without its site table is
  refused at instantiation (`ValueError`).

Rejected (S0013 "Alternatives considered"): a frame-pointer chain on x86-64 (a pool register and two
instructions per frame), frame records written on the value stack at each call (stores on every
call, every module's code changes), a shadow stack of references, and a conservative scan (a
collector cannot rewrite a word it is not sure is a reference). Cost accepted: native debuggers and
profilers cannot unwind through JIT frames (ROADMAP.md, phase 6). The bridge's clearing of `sp` is
not observable by any walk while every entry restores the record (LESSONS 105); it is kept as a
defence.

### D57. The JIT GC heap: a hidden i32 memory per store, u32 references; host values type-checked; context ABI v1 amended
Status: accepted (maintainer, 2026-11-10 and 2026-11-13: S0013 Q1, Q5, Q8, Q9, Q10, Q11 and the step-3b plan) · Date: 2026-11-15 · Supersedes: — · Superseded in part by: D61 · Evidence: S0013 step 3b (Changes 3b.1-3b.4); spec `jit-x86_64` 64 229 pass, 0 expected failures, as `pyc`, guard and explicit, with the walk check and with poison; BENCHMARKS.md, "GC (S0013 step 3b)"

* **The heap.** Each store has one GC heap, an i32 `Memory` (4 GiB + 32 MiB reserved, never moves)
  bound into every instance that uses GC as the memory after its declared ones (two context words;
  `memory.size`, `memory.grow` and the libcall immediates do not count it, and a module without a
  memory of its own has it in memory 0's registers). Every heap access is an ordinary `load`,
  `store` or `simd` on it, so S0011's bounds checks and guard fault points cover it unchanged (Q8
  (a)). A reference of the any or extern hierarchy is a u32 word: 0 null, `(v << 1) | 1` an i31,
  `(h << 2) | 2` a host value, otherwise an object's offset (a multiple of 8). The heap's header
  holds the bump cursor and limit; type-info records (kind, final, depth, display, traced fields)
  sit in a permanent area, one per canonical type and store; objects start with their record's
  offset (ARTIFACT.md part 3). Until the collector (S0013 step 5) the heap only grows; superseded
  by D61: since step 5.3 it is collected.
* **Committed from its start** (S0013 3b.1 review, reversing the plan's decision D to commit its
  regions apart): every byte below the size the contexts see is committed, so an explicit bounds
  check that passes never reaches an uncommitted page. Cost: the type-info area's 64 MiB counts
  against a commit limit (Windows' paging file, Linux in strict overcommit) once a store allocates
  its first object; no RSS. Step 5's semispace B, above A's uncommitted tail, brings such a hole
  back; Q8 accepted it there, and step 5's plan decides. Closed by D3 of S0013's "Step 5
  decisions" (D61): the two halves are adjacent and both committed, so there is no hole.
  Superseded by D69 (2026-11-20): the regions are committed apart again, `size_bytes` the highest
  committed end.
* **Lowered once.** The GC instructions are lowered in the shared core (`jit/common/gc.py`) on D39
  methods only (Q9 (a)); a backend only lists the opcodes in its `supported` set. Allocation is a
  bump in code with a cold call to a service when full; casts compare the record (final) or the
  display entry at the target's depth (non-final). Context ABI v1 is amended without a bump (Q5, as
  D34, D50, D52, D56): the heap's two words, then after the tags the GC group (8 service words and
  one RTT word per struct or array type the code allocates or casts to), only in a module that
  uses GC; every other module's words and code are unchanged (D35 lists).
* **Deviation from D25, until step 5**: the subtype test of a function type that is not final
  (`call_indirect`, `return_call_indirect`, a cast to a concrete function type) is a Python
  service, `FUNC_SUBTYPE`, a computation D25 does not let the JIT hand to Python. It ends at step 5,
  when JIT code calls the collector module's `is_subtype` with type-info records for function
  types (Q11, S0013 step 5's acceptance). **Closed** in S0013 step 5.6 (D61, "Function subtyping"):
  JIT code tests equality inline, a final target stops there, and otherwise calls `is_subtype`;
  GC group word 7 is reserved.
* **Host values are type-checked** (S0013 3b.2 review, revising the plan's decision O, which had
  left it to phase 6 as memory-safe: it was not, a wrong word corrupted the heap): a value the host
  gives for a reference parameter, or returns from a host function, must match the type, as
  Wasmtime checks, else `TypeError` before anything runs, on every target. `pyc` checks the same
  types; one difference remains: a value of another store (a GC object, a function or an
  exception) is a `TypeError` on the JIT but passes on `pyc`, whose Python objects belong to no
  store.
* **Handles kept until the store is dropped** (Q10, decision F of the step-3b plan): a GC object
  given to the host is a `GcRef`, an entry of the store's handle table (index, word, one entry per
  object); there is no release, generation count or free list before phase 6, so the table grows
  with the distinct objects a store hands out. A deviation from Wasmtime's scoped roots, recorded
  here as Q10 asked.
* **Not collected yet**: until step 5 an unreachable object is never reclaimed (the heap grows up to
  2 GiB − 32 MiB, then "GC heap out of memory", a host error). Superseded by D61: collected since
  step 5.3, with a live limit of 2 GiB − 32 MiB − 64 KiB.

Rejected (S0013 "Alternatives considered"): references as 64-bit addresses into store chunks (no
containment, no reuse of S0011's checks, a collector that could not be a wasm module); GC as
backend methods (the semantics written twice); a frame-pointer chain for the roots (D56).

### D58. Threads after the artifact format; what they need in the context ABI reserved before it
Status: accepted (maintainer, 2026-11-15: the direction and option (a)) · Date: 2026-11-15 · Supersedes: — · Evidence: ROADMAP "Next" items (6) and (7); ARTIFACT.md part 3 (the libcall block, word layout, code 15 `HEAP_MISALIGNED`, `MEMORIES.shared`); D50; the libcall reservation implemented by S0014 step 0 (2026-11-18, `context.LIBCALL_WORDS`, `tests/jit/test_tables.py`)

Threads (shared memory, the atomic instructions, `memory.atomic.wait32/64`, `memory.atomic.notify`)
come after the artifact format is fixed, as ROADMAP orders them. So that they arrive as an amendment
of context ABI version 1 and not as version 2 (which would refuse every artifact written by then),
the format fixes three things for them:

* **The model: one store per thread**, as in Wasmtime. Each thread instantiates the module in its own
  store and imports the shared memory; only the memory is shared. Everything per store today stays
  as it is (the context, words 10 and 14, the GC heap, the value stack, `TRAP_CODE`, `HOST_ERROR`),
  and CLAUDE.md's "one thread per store" holds as written; what changes is that several stores share
  one memory. A store used by several threads at once (shared-everything threads) would make words
  10 and 14 per thread: that is version 2, out of scope.
* **The libcall block gets reserved space**: 10 words, the 6 used today and 4 zero, so `wait32`,
  `wait64` and `notify` (operands in slots, as every libcall) go in without moving any word after the
  block. Rejected: the number of libcall words recorded in the artifact (option (b)): every loader,
  the C reference loader (D36) included, would lay out a variable block for four words' saving.
* **A shared memory's size may change at any time** (another thread's `memory.grow`), not only during
  a host call or libcall as D50 says for i64 memories: code never keeps it in a register; explicit
  bounds read `size_bytes` at every access (an acquire load where the architecture needs one; an
  aligned 64-bit load on x86-64 needs nothing more); the base never moves (the S0006 reservation);
  guard mode is unchanged.

Needs no reservation: the atomic operations and `atomic.fence` (code generation; aarch64
`ldaxr`/`stlxr` loops at the ARMv8.0 baseline, LSE as an ISA variant, D42), the misaligned-atomic
trap (code 15 exists), `MEMORIES.shared` (exists), a shared memory's `memory.grow` (libcall 0, with
the size rule above).

When: the four libcall words shift every context offset after the block, so the code bytes of
every module whose code names such a word change (D35; 217 of 2 527 compiled modules, S0014 step 0); they go in with the artifact-format spec, after S0013 step 8 (step 6 changes
words 10-13 anyway), and ARTIFACT.md part 3 states the reservation and the size rule then, not
before the code does. Cost: Wasmtime 50 enables threads by default, so a gap from its defaults stays
until threads are done; toolchains emit threads only on request (`-pthread`, wasi-threads).

### D59. The artifact container is ELF64
Status: accepted (maintainer, 2026-11-15) · Date: 2026-11-15 · Supersedes: — · Evidence: ARTIFACT-CONTAINER.md (option (b)); the prototype `poc/artifact/elf64.py` (standard library only), read back by `readelf`, `objdump`/`llvm-objdump`, `llvm-readobj` and pyelftools 0.32 with no warning; Wasmtime's `.cwasm` is an ELF on every OS

The artifact container of format v1 is ELF64: `ET_REL`, a private `EI_OSABI`, no program headers,
the code in `.text` (file offset aligned to 4096, so a loader may map it and aarch64's `adrp` rule
holds), each payload of ARTIFACT.md part 2 as a `.waboros.<name>` section aligned to 8 in a fixed
order, and `.symtab` with one local `STT_FUNC` per function and the entry trampolines as globals
(`__waboros_entry_sysv`, `_win64`, `_aapcs64`). `e_machine` replaces `arch`; ELF's ident and the
OS/ABI replace the magic. Deterministic (fixed order, no timestamps, string tables in first-use
order), so D35's identity holds for the files. The question of ARTIFACT-CONTAINER.md is closed:
options (a), our own container, and (c), our own plus an ELF exporter, are dropped.

Unchanged (ARTIFACT-CONTAINER.md, "What does not change"): our own loader, `mmap` and W^X, no OS
loader and no file needed; thin and fat files (D18); the header fields; the context ABI (part 3);
the ISA policy (part 1); a loader refuses what it does not know.

The package keeps no dependency: its own writer and reader on `struct` (about 200-300 lines with
every section). pyelftools, `readelf`, `llvm-readobj`, `llvm-objdump` and `eu-elflint` are test and
CI tools only, as independent checks of the writer.

Left to the artifact-format spec (ROADMAP "Next" (6)), each to decide there: the `EI_OSABI` value
(64 or above, not Wasmtime's `0xC8`, not 97 or 255); whether `EI_ABIVERSION` or `.waboros.header`
carries `format_version` (one source of truth); little-endian and ELF64 only (D48); the header split
(`format_version`, `ctx_abi_version`, `isa_level`, `isa_features`, the S0006 flags, `compiler`,
`module_sha256`, `config_sha256` in `.waboros.header`; D18's frozen part becomes the ELF header, the
section headers, `.waboros.header` and `.waboros.wasm`); `CHECKSUM` as `.waboros.checksum` or
dropped; `RELOCS` empty (`SHT_RELA` only if ever needed); `pyc`/`mpy` artifacts (`EM_NONE` in the
same container or outside ELF); the file extension (D18's working `.w2a`); S0006 step 6 (the
`BOUNDS_GUARD` flag and the fault map in `.waboros.traps`, the C loader's fault handler).

Consequences: `objdump`, `readelf`, `nm`, `perf` and `gdb` read every artifact with function names;
the C reference loader (D36) reads a fixed ELF64 subset (header, section headers, `.symtab`, its
known sections by name, unknown ones skipped); hosts in other languages and the micro-compilers
(JIT-AS-WASM.md) use a container every language can read; D35 tests gain "written twice and
cross-compiled, byte-identical ELF". Order: after S0013 steps 5-8 and the D58 reservation.

### D60. Micro-compilers and the native runtime in C; a native runtime embeds its own architecture's compiler, the others ship as wasm
Status: accepted (maintainer, 2026-11-15) · Date: 2026-11-15 · Supersedes: — · Evidence: MICRO-COMPILERS.md ("Language choice"), JIT-AS-WASM.md ("Language of the port"), MICRO-COMPILERS-SHIPPING.md, D36, D48

* **Language: C**, for the micro-compilers (the ports of the JIT compilers) and for the native
  runtime and loader (D36). Not Zig, not Rust: C embeds anywhere (every host language has a C FFI,
  every platform a C compiler) and is the language of the C reference loader and of sljit (D48).
  The toolchain that builds it is a separate question (D54's pinned `ziglang` wheel as a C compiler
  stays possible); the source is C.
* **Shipping:** a native runtime built for architecture X embeds the micro-compiler for X compiled
  ahead of time, so it compiles and runs wasm with no wasm engine of its own. It is embedded as
  data: the ELF64 artifact's bytes (D59) carried inside the runtime binary (a byte array or
  `.incbin`), not linked, and loaded by the runtime's own loader like any other artifact. Rejected:
  linking the artifact into the binary with the system linker. It works (the maintainer checked
  on the prototype, 2026-11-15: `ld.bfd`, `ld.gold`, `ld.lld` on x86-64 and `ld.lld` on aarch64
  accept the private `EI_OSABI`, and a C call through `__waboros_entry_sysv` reaches the code), but
  it would need per-artifact global symbol names (two artifacts collide on the trampoline names),
  `SHF_ALLOC` metadata sections with symbols, and `.note.GNU-stack` in every artifact, for nothing
  the embedded-bytes form lacks. The bytes sit in `.rodata` and the loader copies the code into
  executable memory, so the block's alignment does not matter; to map code straight from the
  binary's image one day, embed it 4096-aligned (`.balign 4096` before `.incbin`), since `.text`
  inside the artifact is 4096-aligned only relative to the file's start.
* The micro-compilers of the other architectures are shipped as `.wasm`, optional, for
  cross-compilation (D35): the runtime compiles such a `.wasm` with its own embedded
  compiler, then runs it to produce artifacts for that target. The Python package keeps its
  shipping as MICRO-COMPILERS-SHIPPING.md describes; this decision fixes the language and the shape
  of the native runtime, not the Python side.
* Unchanged: the Python JIT stays the byte oracle of each hand-written port (MICRO-COMPILERS.md).
  A port whose code generation is sljit cannot match it byte for byte; its oracle is behaviour:
  the spec suite, `spec_diff` both ways against the Python JIT of the same architecture, the walk
  check and poison, as the `poc/sljit/sljit-be/` prototype is checked (maintainer, 2026-11-17;
  D64 item 4); whether a port
  replaces its Python backend is still the JIT-AS-WASM decision taken after the artifact format
  (ROADMAP "Next" (6)).

### D61. The JIT GC heap collects: a copying collector in wasm, Wasmtime's semispaces
Status: accepted (maintainer, 2026-11-15: S0013 "Step 5 decisions" D1-D4 and D6, the step-5.3 design note); written in S0013 step 5.3, function subtyping in step 5.6, closed in step 5.7 (the order measured) · Date: 2026-11-15 · Supersedes: S0013's fixed semispaces A and B · Evidence: S0013 Changes 5.3 and 5.6; spec `gc` and `exceptions` with `WABOROS_JIT_GC_STRESS=1 WABOROS_JIT_GC_POISON=1 WABOROS_JIT_GC_VERIFY=1` on both JITs (0 fail); `tests/jit/test_gc_collect.py`; the Kotlin program runs 100 times in one store at 32 MiB of object space, where it ran out of heap at the 19th run before

* **The collector** is `runtime/gc/collector.wasm` (Cheney, S0013 step 5.2), compiled by the
  store's JIT once per process and run as a **nested JIT entry** from the allocating service, its
  frame above the service's slots (ARTIFACT.md "Host functions", B1). Python gathers the roots
  (`jit/common/collect.py`: frames by the walk, reference globals, any- and extern-hierarchy tables
  (exn too from step 6), passive element segments, GC handles, the exception arena's payloads
  until step 6, then the pending-exception word, D63) and writes the forwarded words back. The
  collector's instance is kept out of `store.instances`, so
  `store.interrupt()` never cuts a collection in half.
* **Semispaces as Wasmtime's**: two adjacent halves `[OBJ, OBJ + C/2)` and `[OBJ + C/2, OBJ + C)`,
  both committed, their boundary moving with growth; growing while the upper half is active
  collects first. **Growth**: the first allocation commits 2 MiB; after a collection that leaves
  more than half of a semispace live, `C` becomes `max(2C, 4·(live + request))`; never shrinks.
  **Live limit**: `S_max` = 2 GiB − 32 MiB − 64 KiB (the heap's end stays below 2³², the
  collector's bounds being u32); beyond it "GC heap out of memory", Wasmtime's text.
* **Every allocation that may run out of room collects** (the inline slow path and the array
  services), and services read reference operands and segment items after it.
* **Deviations from Wasmtime**: instantiation and constant expressions never collect (they grow;
  observable only as an earlier "GC heap out of memory"); an allocation with less than 4 KiB of
  value stack left traps `STACK_OVERFLOW` before collecting; a collector trap, a failed
  verification or an exception (KeyboardInterrupt) inside a collection loses the store
  (`RuntimeError`): the heap may be half moved, so nothing runs on it. A lost store refuses every
  entry, instantiation and host access to globals and tables, and a host function that returns
  normally on a lost store (one that caught the error) fails its call, so wasm already running
  does not resume.
* **Host values of `externref`** that a collection did not meet are freed, unless the collector's
  report overflowed (a live handle at or above 16 384); freed numbers are reused lowest first, so
  the table stays flat under churn. `GcRef` handles stay until the store is dropped (phase 6).
* **Function subtyping** (S0013 step 5.6, D6 of "Step 5 decisions", Wasmtime's shape): a
  function-subtype site compares the record's type id with the signature word inline (D39
  amendment `func_type`); a final target stops there, with no call; a non-final one calls the
  collector module's `is_subtype(q, t)` through the `IS_SUBTYPE` record appended after the RTT
  words (ARTIFACT.md part 3; the context ABI v1 amended without a bump, no word moves; GC group word
  7, `FUNC_SUBTYPE`, reserved and zero). `is_subtype` finds both records through a direct map from
  the process-wide type id, `[60 MiB, 64 MiB)` of the heap, and does the display test; an id at or
  past the cap of 2²⁰, or without an entry, answers 0. Records are written for the function types
  that have a supertype or are not final (the only ones a non-equal test can match), not for every
  function type as the plan said, so a GC module with only final function types commits no map.
  Process-wide type ids are never reused (`_TYPE_IDS` only grows), so a map entry never goes stale;
  the cost is the cap, a deviation from Wasmtime (which reuses ids after unregistration and has no
  such cap): the 2²⁰-th distinct function type the JIT sees in the process (in any module, GC or not) makes
  a later GC module that needs its record fail its instantiation
  with "GC type area exhausted". The collector instance is made at the instantiation of the first
  module with a function-subtype site; outside `store.instances`, its native stack limit is set to the store's at every outermost entry (a collection lowers it) and its interrupt word cleared when it is made (LESSONS 117).
* **The work-list order: Cheney** (S0013 step 5.7, measured as Q2 says; BENCHMARKS.md, "GC collector
  (S0013 step 5)"). A depth-first build (an explicit stack of field addresses in the free end of
  to-space, a pre-order copy; correct under stress, poison and verify on `gc` and `exceptions`) was
  alternated with Cheney on a linked list, a binary tree and a reference array (about 10⁶ live
  objects each; mutator walk and pause separately, both bounds modes) and on the Kotlin program,
  with a second Cheney run as the control. It gains on none beyond the spread between rounds (the
  tree walk is 8.5 against 10.2 ns in explicit mode and 10.1 against 7.3 in guard mode; the tree
  pause is 36 % longer in guard; the Kotlin program is equal or slower), and it needs a stack in
  the free end of to-space that Cheney does not. Only Cheney's `collector.wasm` is committed. The
  pause is 25-45 ns per live object; the collector compiles in 5-9 ms once per process and bounds
  mode and instantiates in about 0.3 ms per store; CoreMark is unchanged.

### D62. Statically linked AOT artifacts and static builds: after threads or WASI; three rules in format v1 now
Status: accepted (maintainer, 2026-11-16) · Date: 2026-11-16 · Supersedes: — · Evidence: STATIC-BUILDS.md; the ELF64 prototype checks of D59 and D60 (2026-11-15)

User modules compiled ahead of time are linked into a native binary with the system linker (a
**static build**: the C loader-runtime, C host functions with WASI, and the artifacts, one ordinary
executable or library, as a wasm2c build is), for the platforms where executable memory cannot be
had at run time (an iOS app, strict W^X, firmware). It is **not part of format v1**: it comes after
threads (D58), or after WASI, as an amendment. No extra optimising compiler tier is planned: a
static build uses the code the micro-compiler emits for JIT and AOT, so it brings cheap host calls
and those platforms, not faster compute code (STATIC-BUILDS.md has the expectations and the
measurements). D60's rejection of linking stays for the embedded micro-compiler only.

So that the linked form is an amendment and not a version 2, format v1 keeps three rules: a loader
ignores the `sh_flags` of `.waboros.*` sections and finds them by name (D59); a loader never
depends on global symbol names and finds the entry trampolines through a `.waboros.*` payload
(offsets, as `ENTRIES`), so names can take a per-artifact prefix later (`.symtab` is for tools and
a future linker); unknown sections are skipped (D59), so `.note.GNU-stack` and a future descriptor
section need no version change.

### D63. JIT exceptions in the GC heap: a throw allocates, pads read through the heap, `exnref` a u32 reference
Status: accepted (maintainer, 2026-11-16: the step-6 plan's D1 (b), D2-D5 (a), D6 (b), D7 (a) and the three additions from Wasmtime) · Date: 2026-11-16 · Supersedes: D52 in part (the arena, words 11-13, the object and `exnref` encoding, the D39 methods `exn_match`, `exn_field`, `exn_load` and `throw`) · Evidence: S0013 Changes 6.1, 6.2 and 6.3; spec `gc` and `exceptions` with `WABOROS_JIT_GC_STRESS=1 WABOROS_JIT_GC_POISON=1 WABOROS_JIT_GC_VERIFY=1` on both JITs, both bounds modes (905 pass, 0 fail); `tests/jit/test_exceptions.py`; BENCHMARKS.md "Exceptions in the GC heap (S0013 step 6)" (step 6.5: a throw caught one frame up 14 ns in guard mode against the arena's 15 and Wasmtime's 242; 10⁷ throws grow RSS by 2.1 MiB against the arena's 153 MiB; 1 000 instantiations 271 µs against 385 µs)

S0013 Q7 (b), as Wasmtime: an exception object is an object of the store's GC heap, and an
`exnref` is its u32 offset, a reference like any other (S0013 step 6, `docs/plans/S0013-6-plan.md`).
Status 14 propagation (D52) is unchanged.

* **The object** (D2 (a), D3 (a)): a struct of the u64 tag identity at 8 and the tag's parameters
  at their natural alignment (`gc.exn_layout`), with a kind-1 (struct) record per canonical tag
  type, final, depth 0, keyed `"exn " + types.key`, apart from the type's kind-3 record. The
  collector module and the verifier need no new kind. The identity stays the process-wide u64;
  an imported tag has its exporter's.
* **Which modules get the heap** (D4 (a), D6 (b)): a `throw`, or a `try_table` with a `catch` or
  `catch_ref` clause, gives the module the heap's words and the full GC group, with one RTT word
  per tag it throws; a module with only `throw_ref` and `catch_all(_ref)` reads the pending word
  alone and keeps its layout and code.
* **`throw`** is lowered in the core as `struct.new` is: a bump, a cold `ALLOC` call when it does
  not fit (a safepoint whose map covers the payload, read from its slots after the call, B1), then
  the identity, the pending word and status 14. It runs no Python unless the space is full.
  **A full heap at `throw`** (after collecting and growing to the limit) is "GC heap out of
  memory", status 13: an embedder error no pad catches, never a wasm exception (Wasmtime's
  `GcHeapOutOfMemory`). Under stress every throw collects, so with less than 4 KiB of value stack
  left a throw traps `STACK_OVERFLOW` (D61's deviation now covers `throw`).
* **Landing pads** (D1 (b)) are lowered in the core from existing D39 methods (`ctx_load`,
  checked `load`/`simd` on the hidden memory, `cmp_branch`): every read of the object is bounds
  checked or a guard fault point, as S0013 Q8 (a) asks, and aarch64 is at parity by construction.
  The identity is compared in 64 bits, so a forged or stale identity matches no tag and is never a
  wrong pad. The status register, used as `tos` at the pad, is made 14 again before the jump to
  the enclosing pad. D39 amendment: `exn_match`, `exn_field`, `exn_load` and `throw` leave the
  interface (no code calls them from step 6.2; step 6.3 deleted them from both backends and the
  protocol); `landing` and `throw_ref` stay. `throw_ref` of null traps `NULL_REFERENCE` before anything is stored, as Wasmtime.
* **`exnref` everywhere** is a reference the collector finds: `validate._root` covers the exn
  hierarchy, so exnref slots are in the stack maps, exnref globals and tables are roots, and
  struct fields and array elements of `exnref` are 4 bytes and traced. The validator decides
  whether a module gets masks after its bodies: an exnref needs no declared type (a
  `catch_all_ref` into a block whose `(result exnref)` is inline), so a body that names one turns
  the masks on and the bodies are walked again (review of 6.2). An any- or extern-hierarchy value
  needed no such rule before: without a declared type of those hierarchies a body can only make
  null or an i31, which never move. **The pending word** (word
  10's target) holds 0 or the last exception thrown and is a root (D7 (a)): a defence, since no
  collection happens between a `throw` and its pad or entry; it retains at most one dead object.
* **The host** (D5 (a)): `ExnRef(index, store)` is an entry of the store's handle table shared
  with `GcRef` (re-keyed after every collection); a forged handle or one of another store is a
  `TypeError`. An uncaught exception's payload is read from the object at the entry, before any
  wasm or collection runs, and its references become handles.
* **ABI**: context ABI v1 amended without a bump under S0013 Q5's approval (no artifact file
  exists): word 10's target holds a u32 offset; words 11-13 are reserved and zero since step 6.3,
  which removed the arena, its chunks and its refill service (module code byte-identical, D35).
  A test (t11, `tests/jit/test_exceptions.py`) checks the words are zero in every context and that
  no compiled `exceptions/` module names them in a backend call or addresses them off the context
  register, on both architectures.

### D64. One compiler per Store; nothing shared between Stores; part 3's register table is per compiler
Status: accepted (maintainer, 2026-11-16); superseded in part by D70 (the bullets marked below) · Date: 2026-11-16 · Supersedes: the advice of the first sljit package (freeze part 3's per-architecture table to sljit's registers, or decide hand-written against sljit before freezing part 3) · Evidence: SLJIT-BACKEND.md (sljit keeps the context in rbx and the frame in r15 on x86-64, `jit-x86_64` in r15 and r12)

* *(Superseded by D70: one boundary ABI per Store, not one compiler.)* **A Store uses exactly one compiler.** If the runtime picks a compiler (for example a sljit
  micro-compiler on x86-64), every module, instance and runtime module of that Store goes through
  it; mixing compilers inside a Store is not allowed.
* **Nothing is shared between Stores**: no functions, contexts or trampolines, so Stores with
  different compilers live in one process without conflict.
* Why: two compilers give the same module different machine code and register roles. Calls between
  instances of one Store are direct (a function record with a context switch, `tail_switch`, the
  shared bridge and entry) and need one set of boundary conventions; with one compiler per Store
  that holds by construction, and no table has to be common to all compilers.

Consequences:
1. *(Superseded by D70: the boundary part of the table is per architecture; only the bridge and
   `tail_switch` rows are a compiler's.)* **ARTIFACT.md part 3's "Per architecture" table belongs to a compiler**, keyed by the target in
   the header's `compiler` field. The present table is `jit-x86_64`'s and `jit-aarch64`'s; a sljit
   micro-compiler brings its own. A host that provides the bridge or `tail_switch` follows the table
   of its Store's compiler.
2. *(Superseded by D70 in its compiler clause: another compiler of the same architecture and
   versions loads.)* **A loader refuses, never guesses**: a thin artifact whose compiler target differs from the
   Store's compiler is refused, like an `arch` or ISA mismatch; a fat artifact is regenerated from
   its wasm, as for another target today. The AOT cache key already holds `compiler` and
   `config_sha256`.
3. **Runtime modules** (the collector, D61, and the other D54 modules) are compiled by the Store's
   compiler.
4. **The oracle between compilers is behaviour**: the spec suite, `spec_diff` both ways, the walk
   check, poison. D35 (byte identity, cross-compilation) holds within one compiler, not across.
5. **Static builds (D62)**: trampolines may be one set per compiler (they hold no state), shared by
   Stores of that compiler; state lives in each Store's context.

*(The first rejection below is reversed by D70: compilers mix through a boundary ABI.)*
Rejected: mixing compilers inside a Store; one register table per architecture common to every
compiler (it would force the Python JIT onto sljit's register roles or the reverse, for no gain
once Stores do not mix).

Still open, unaffected: the sljit backend embeds libm and libc addresses as 64-bit immediates
(`_ccall`, `_cmem` in `poc/sljit/sljit-be/`), invalid in a cached or cross-compiled artifact; it is
fixed (inline sequences through `sljit_emit_op_custom`, or calls through context words) before a
sljit micro-compiler emits AOT artifacts (SLJIT-BACKEND.md, check 3).

### D65. JIT liveness maps and the nulling walk
Status: accepted (maintainer, 2026-11-16: S0013 step 7 plan, D1-D9; D1, D2, D7 built in step 7.1, D3-D6 and D8 in step 7.2, D9 in step 7.3; measured in step 7.4) · Date: 2026-11-16 · Supersedes: S0013's "per function, the bitmap of its reference-typed local slots" (never built); narrows S0013 Q3 "Operands" (D3 below) · Evidence: S0013 Changes 7.1, 7.2, 7.3 (the mutation table) and 7.4 (the measurements: BENCHMARKS.md "Liveness (S0013 step 7)": the pass is 0.4-3 % of a GC compile, the nulling 1.2-2.1 µs per frame, a 16 MiB dead local freed; the cost of D2's eager decode: instantiation +34 %); `tests/jit/test_stackmaps.py`, `tests/jit/test_walk.py`, `tests/jit/test_liveness.py`, `tests/jit/test_gc_roots.py` (7.3); the whole spec suite with `WABOROS_JIT_WALK_CHECK=1` on both JITs, both bounds modes, and with the GC switches as well (7.2)

S0013 Q3 has the walk null the reference slots a site does not mark live, so a dead slot never
keeps an offset into the old space. The walk cannot find a site's function, so a per-function
bitmap of reference slots cannot tell it which unmarked slots to null:
* **Two bitmaps per pool map** (plan D1 (a)): `refs`, every reference slot of the frame at the
  site (the full map of steps 3a-6), and `live ⊆ refs`. The 16-byte site record is unchanged;
  reference-free modules keep byte-identical tables (no map). A collection forwards `live` and
  writes 0 into `refs ∖ live` while gathering the roots (the world is stopped; only slots
  `[0, nslots)` of a frame, never a callee's parameters, B1). Before it forwards or nulls a
  frame's slots the gatherer checks that `[base, base + 8·nslots)` lies inside the value stack and
  raises otherwise, as check mode does (step 7.3, a defence against a forged table: a forged shift
  alone is already refused by the walk's entry-base check).
* **Version 1 amended without a bump** (plan D2 (a), as `STACKMAPS` itself, D56, S0013 Q5): no
  artifact file exists. Instantiation decodes the table once, with checks that raise
  `ValueError` (exact length, map index within the pool, no empty map, no bit past `nslots`,
  `live ⊆ refs`), so a step-3a table with a map is refused in the shapes tested (not guaranteed: the table has no
  version field and a parsing 3a table exists, 7.2 review; versioning is left to the artifact
  format's `format_version`) and one without a map runs.
  The decode was done in every instantiation; measured in step 7.4 (BENCHMARKS.md): +125 µs
  (+34 %) for CoreMark, +2.4 ms (+34 %) for the Kotlin program and +19 / +174 KiB per instance.
  Step 7.5 shares it: once per `Compiled` (and per replaced `stackmaps`), the cost is gone
  (S0013 Changes 7.5).
* **Check mode nulls** (plan D7 (a)): `WABOROS_JIT_WALK_CHECK=1` writes 0 into the dead slots at
  every host call and libcall, then validates every `refs` slot, so the whole-suite walk-check
  runs exercise every dead set, modules that never collect included.
* **Operands keep the validator's mask** (plan D3 (b)), **a deviation from S0013 Q3**, whose
  "Operands" rule makes an operand below a call dead when its only consumer is `drop`: that needs
  a stack simulation the core does not have. The cost is retention of a reference below a call
  until its `drop` runs. Revisited only with a measurement that the retention hurts (maintainer).
* **The pass** (plan D4 (a)): `jit/common/liveness.py`, called from the core's `_Func` beside
  `choose_pins`, only for a function with a reference local and a site; it runs again on a
  far-mode retry. Its result is `live` for the locals' slots; `refs` is unchanged.
* **Loops** (plan D5 (a)): every loop's head set starts empty and the whole backward pass repeats
  until no head set changes (the sets only grow); no CFG. The spec suite never needs a second
  pass; the Kotlin corpus program needs at most 3 (S0013 Changes 7.2).
* **Exception edges** (plan D6 (a)): every site in the body of a `try_table` joins the label sets
  of the clauses of every enclosing `try_table` (a pad that misses jumps to the enclosing one),
  services and `is_subtype` calls too, which cannot throw (retention only); `throw` and
  `throw_ref` take the same set.
* **Invariant I5** (S0013): after a site a reference local's slot is read only by a later wasm
  `local.get`; asserted in the core where a site is recorded (every operand-stack entry is a slot)
  and where an allocation's cold site is queued.
* **A bisect switch** (plan D8 (a)): `WABOROS_JIT_NOLIVE=all|<function indices>` gives those
  functions `live = refs` (the step-7.1 maps); no code byte changes.
* **Fuzzing references** (plan D9 (a), widened by the maintainer): `tools/fuzz.py --refs` turns
  on smith's reference types and GC in the cross-target mode (exceptions are on there already);
  what the runner cannot drive (an export with a non-nullable reference parameter) is skipped and
  said so in TESTING.md.

Rejected: a second pool index per site (20-byte records, every module's table changes, CoreMark's
included); a per-function table of reference slots with a function lookup per frame (a new
section, a second search, operands never nullable); the `drop`-consumer rule for operands now
(plan D3 (a): about +50 lines in the validator shared with `pyc`, or a stack-effect table in the
core); the pass in the validator (a forward walk, then a backward one anyway, run for `pyc` too);
per-loop iteration or a worklist over basic blocks (more code, a CFG the core does not build; the
fallback if a module shows a pathological pass count); only call and throw sites joining only up
to the first `catch_all` (a second list of "which sites throw" to keep in step with the
lowering). Wasmtime's maps list only live slots and it never nulls (Cranelift reuses spill slots),
which a fixed slot per local cannot copy.

### D66. Artifact versioning: one `format_version` for the whole file; amendments by feature flags, no per-section versions
Status: accepted (maintainer, 2026-11-17) · Date: 2026-11-17 · Supersedes: — · Evidence: the S0013 7.1/7.2 review (a step-3a `STACKMAPS` table can be built that the new decoder parses); ARTIFACT.md part 2

* A change to the layout of any section (as `STACKMAPS` in S0013 7.1) is a change of the artifact
  format: the whole file's `format_version` is bumped, and a loader refuses a version it does not
  support (part 2). Sections carry **no version field of their own**: one versioning scheme.
* An amendment inside a version (an optional section, a flag a newer writer may set and an older
  loader can ignore or must refuse) is expressed by **feature flags** in the header, as ELF does;
  the flag set and which flags a loader must understand are defined by the artifact-format spec
  (ROADMAP "Next" (6)).
* Rejected: a version per section. It lets an old loader skip one changed section, but adds a
  second versioning scheme for a case that does not arise while waboros writes and reads its own
  artifacts.
* Until the format is frozen no artifact is written to disk, so the amendments of v1 made so far
  (D56, D61, D63, D65) need no flag; the format spec starts from their final form.

### D67. A pre-pass gives `func_begin` the function's needs, so the native frame is sized before the body
Status: accepted (maintainer, 2026-11-17) · Date: 2026-11-17 · Supersedes: — · Evidence: `poc/sljit/sljit-be/SLJIT-BACKEND-RESULTS.md`, "Update 2026-11-16 (3)" (96-byte sljit frames against 32, recursion depth about 5 400 against 16 400 under the default budget)

* A one-pass code generator (sljit, and the C micro-compilers of D60) fixes the frame size when the
  function starts, before its body is generated; the hand-written backends size it after the body
  (or retry in far mode).
* The core therefore scans each function body once before `func_begin` and passes what it needs
  (calls through a record or a host, `wide`/v128 values, bulk memory copies, the pins it uses);
  `func_begin` takes these hints as a D39 amendment, and a backend may ignore them.
* Implemented with the C core (D60), not before: the Python backends do not need it, and their
  machine code must not change for it (D35).

### D68. The micro-compilers are our core in C over sljit; no hand-written port
Status: accepted (maintainer, 2026-11-17) · Date: 2026-11-17 · Supersedes: the hand-written-port plan of MICRO-COMPILERS.md ("First: `aarch64.wasm`, a port of the aarch64 backend") and the byte oracle for the ports (D60's "Unchanged" bullet, MICRO-COMPILERS.md); amends D48 · Evidence: `poc/sljit/sljit-be/SLJIT-BACKEND-RESULTS.md` (third update, 2026-11-16); the orchestrator's check on `gc` at 43114f2, 2026-11-17

* The micro-compilers (D60: C, shipped as `x86_64.wasm`, `aarch64.wasm`, `riscv64.wasm` and
  embedded in the native runtime for its own architecture) are the waboros core ported to C, with
  code generation through sljit (D48's sljit backend), not hand-written ports of the Python
  backends `jit-x86_64` and `jit-aarch64`.
* **Oracle: behaviour, not bytes.** The spec suite on every claimed directory, `spec_diff` both
  ways against the Python JIT of the same architecture, the walk check and poison, CoreMark's self
  check; D35's byte identity holds within one compiler (deterministic output, cross-compilation),
  not across compilers (D64 item 4).
* **Why:** the prototype already covers every D39 method on x86-64 and aarch64 (QEMU). The
  orchestrator's own check on 2026-11-17 (x86-64, `gc` at 43114f2, sljit 39c508d1):
  * the whole spec suite with the walk check and poison: 64 229 pass, 0 fail, in guard and in
    explicit mode, the same as `jit-x86_64`;
  * CoreMark (`taskset -c 2`, two alternated rounds): `jit-sljit` 15 603 / 15 888, `jit-x86_64`
    14 571 / 14 943 iterations/s, both self checks passed.
  One code generator for three architectures (riscv64 for free) instead of three hand-written
  backends; D63 already reduced the per-ISA part to SIMD custom words, `sqrt`, `tail_switch`, the
  frame helper and fault-point guards.
* **Left before the micro-compilers:** the frame size (D67's pre-pass), BTI landing pads on
  aarch64, a sljit API for `ret_at` (read from sljit's internals today), native arm64 timings.
* The Python backends stay: they are the reference behaviour, `pyc`'s fallback partners and the
  targets of the Python package until JIT-AS-WASM's decision (after the artifact format).


### D69. The GC heap's regions are committed apart; `size_bytes` is the highest committed end
Status: accepted (maintainer, 2026-11-20) · Date: 2026-11-20 · Supersedes: D57's "Committed from its start" bullet; amends ARTIFACT.md part 3 (the GC heap's `size_bytes`) without a version bump · Evidence: S0014 step 4.2, BENCHMARKS.md "GC heap: committed bytes"; LESSONS 135

* The store's GC heap commits three regions apart, each from its start in whole 64 KiB pages and
  before anything is written there: the header page (with `ROOTS`) at creation and the type-info
  area after it as records are written; the whole tid map ([60 MiB, 64 MiB), 4 MiB) with the first
  function-type record; the semispaces as today (D61). `Reservation.commit_range` commits a range
  apart from a reservation's contiguous high-water mark.
* The hidden memory's `size_bytes` is the highest committed end and never shrinks; the gaps below
  it (the unused type area, the tid map before a function-type record) are uncommitted. Code is
  unchanged (D35 lists identical on both JITs): it addresses the header, records it reached through
  an object or an RTT word, the tid map only once committed, and objects, never a gap.
* **Why:** a heap committed 66 MiB at its first allocation (64 MiB of it the type area and the tid
  map, using one 64 KiB page in practice); a dropped store waits for the cyclic GC (LESSONS 135),
  and Windows x64 cp315t ran out of commit charge. Now a store with one struct commits 2.1 MiB; 16
  live such stores fit under RLIMIT_DATA + 256 MiB, where before four fit and the fifth failed (with
  `execmem._retry` freeing a dropped store's 66 MiB).
* **Cost:** the hole D57 closed comes back, as S0013 Q8 accepted it for semispace B: a corrupt
  reference (a broken invariant; no wasm program can forge one) into a gap is a bounds trap in guard
  mode and a crash in explicit mode, where before it read or wrote committed garbage. CI's explicit
  stress run of `gc` and `exceptions` turns any missed commit into a crash, not a disguised trap.
  Two defensive checks lose their trap in explicit mode: the collector's `$size` accepts any record
  offset in the type area and `gcverify` (`WABOROS_JIT_GC_VERIFY=1`) reads a root word unchecked,
  so a corrupt header or root into a gap crashes there instead of trapping "GC heap lost" or
  raising `HeapError`.
* **Tid map whole, not by ids (maintainer):** committing only the entries of ids assigned so far
  would need every function record to be made at instantiation, and phase 6's API makes host
  functions outside it; 4 MiB per heap with a function-type record is the price.
* Context ABI v1 amended without a bump (as D34, D50, D52, D56, D57): no word moves, no code
  changes, an older artifact's code addresses only committed bytes.

### D70. One boundary ABI per architecture; compiler internals private
Status: accepted (maintainer, 2026-11-21); the design is [S0015](specs/S0015-boundary-convention.md) (draft) · Date: 2026-11-21 · Supersedes: D64 in part (its first bullet, consequences 1 and 2 in their compiler clause, the rejection of mixing compilers) · Evidence: BENCHMARKS.md, "C-ABI boundary between instances (prototype, 2026-11-21)"; [WAMR-INTEROP.md](WAMR-INTEROP.md)

The maintainer's target: within one architecture, an artifact behaves like an ordinary `.so`. An
x86-64 artifact of `jit-x86_64` and one of the sljit micro-compiler (D60, D68) load and link in
one store, and so do artifacts of different compiler versions; the same holds on aarch64. No
compatibility between architectures is asked for.
* **One ABI per store instead of one compiler per store.** Every function record's `code` is a
  C entry `int32_t f(uint64_t *ctx, uint64_t *frame)` in one fixed convention per architecture:
  SysV on x86-64 on every OS (Windows included: only our code calls it), AAPCS64 on aarch64. The
  status comes back in `eax`/`w0`. Calls through a record (imports of another instance,
  `call_indirect`, `call_ref`, the IS_SUBTYPE record) use it; calls inside a module keep each
  compiler's own convention. Pinned registers, the pool, frame layout and the native frame size
  stay each compiler's own.
* **Trampolines.** `entry` becomes the runtime's, one per architecture and independent of the
  compiler: it calls a C entry. `bridge` and `tail_switch` are private to a compiler, carried in
  its artifacts and reached through each instance's context words 8 and 9 (already per context).
* **The walk interface** is fixed per architecture, not per compiler. Every return address on a
  store's stack is a site in some artifact's `STACKMAPS`, and each C entry's call into its body is
  a site with shift 0 and no map. A C call passes `frame` = the caller's base + 8 · shift. The exit
  record is `{sp at the call, pc, the innermost frame base}`, and the entry's saved block has a
  fixed place. Invariant I1 holds in every compiler: at a site no reference is in a register.
  Foreign frames are opaque except for their site numbers; no frame-pointer chain is needed.
* **Traps** stay status returns through every frame and every C entry; interrupts, fuel and stack
  limits stay context words; the guard fault handler stays per process and reads each artifact's
  fault map.
* **Tail calls across the boundary keep constant stack.** The first hop calls the foreign C entry
  through the compiler's `tail_switch`. A later hop starts in a body whose return address is its
  own module's C entry: that frame is unwound and the foreign C entry is jumped to (a C sibling
  call). Every compiler must do this; a conformance test checks it.
  *Amended (2026-11-21, S0015 re-review):* the requirement is a property: cross-boundary tail
  chains in any mix of compilers run in constant native stack, and every return matches its call
  (CET shadow stacks, Arm GCS). Two forms meet it (S0015, "Tail calls across the boundary"):
  (2a) a direct C sibling jump to the record's C entry, for a compiler whose functions are their
  C entries or whose internals keep only registers the boundary keeps (sljit's
  `SLJIT_CALL_RETURN`); (2b) the first hop through the compiler's `tail_switch`, then later hops
  in which the body *returns* to its own C entry with a private hop status and the C entry jumps
  to the foreign C entry. The "unwound and jumped" wording above describes the prototype, which
  discarded a return address a shadow stack still holds; it is not the required form.
* **Compatibility rule.** S0014 Q3's exact `compiler` string stays the cache key, but it is not
  the compatibility rule. Compatibility follows the architecture, `format_version` and
  `ctx_abi_version`. A loader accepts other compiler versions and other compilers of the same
  architecture under that rule, but thin files of other compiler versions are accepted only from
  phase 6 (Q3 (b), loader policy only; nothing in the format or the ABI blocks it).
  *Amended (maintainer, 2026-11-21, S0015 review):* until phase 6 the waboros loader keeps the
  exact `compiler` string: thin files of another compiler or another compiler version are refused,
  fat files regenerated. The per-architecture rule above is specified now (S0015,
  "Compatibility") and enabled in phase 6 together with mixed stores, which then need a foreign
  compiler's blob to be executed (executable memory, W^X, instruction cache) and the trampoline
  compare to move from `read` to store attach.
* **v1, no bump.** The change alters what a record's `code` means, so the committed v1 files of
  S0014 step 4.3 would no longer run under it. They are regenerated once, without a version
  bump: v1 is not frozen yet (D66: the format spec starts from the final form of v1), and no file
  has been released. *Amended (maintainer, 2026-11-21):* not regenerated. The step-4.3 files stay
  as pre-S0015 fixtures (`test_old` expects the thin ones refused and the fat ones regenerated),
  and new v1 files made after S0015's format step are committed next to them.
* **Scope now:** x86-64 and aarch64, both Python backends, the spec, the conformance test, the v1
  regeneration (amended: the new v1 files). **Deferred:** thin files of other compiler versions (phase 6); a real store
  mixing the Python JIT and sljit (when the micro-compiler exists).

Measured (prototype on `jit-x86_64` alone, every call through a record on the C path; BENCHMARKS.md):
a cross-instance call 2.6 → 4.4 ns, a `call_indirect` 3.0 (the placement control) → 4.5 ns
(corrected 2026-11-21 from "3.8 → 4.5", which compared with the tree without the control),
CoreMark unchanged within its spread; 64 229 spec commands pass under the walk check in both bounds modes.

Rejected:
* **Keep D64**: modules of different compilers in different stores, talking through host
  functions. A call through the host into another store costs about 13.5 µs, about 5 000 times a
  direct cross-instance call, and nothing (memories, tables, GC references) can be shared.
* **One internal register table per architecture for all compilers.** sljit's `ENTER_KEEP(n)`
  shares only S0..S(n-1) with n ≤ 3 (rbx, r15, r14 on x86-64; x19-x21 on aarch64), so our r12,
  r13 and x25-x28 need a patched register map. The set of registers that survive a call must match
  too: sljit clobbers rsi, rdi and r9 (x2-x7 on aarch64), which our pool keeps. It would freeze both
  compilers' internals across versions, which is the opposite of the target.
* **A fast path for records of one's own context** (the prototype's `cabi2`): no gain beyond the
  noise, and two call instructions per site with one map.
* **`ctx_abi_version` 2** for this change: nothing released depends on v1, so a bump would only
  add a refused version to the loaders.

