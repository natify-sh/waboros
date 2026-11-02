---
title: Decisions
kind: log
status: current
updated: 2026-11-02
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
Status: accepted · Date: 2026-10-26 · Supersedes: — · Evidence: —

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
Status: accepted · Date: 2026-10-26 · Supersedes: — · Evidence: —

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
Status: accepted · Date: 2026-10-26 · Supersedes: — · Evidence: —

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
Status: accepted · Date: 2026-10-26 · Supersedes: — · Evidence: —

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
Status: accepted · Date: 2026-10-30 · Supersedes: — · Evidence: —

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
Status: accepted · Date: 2026-10-30 · Supersedes: — · Evidence: BENCHMARKS.md "Guard pages in the JIT" (PoC: explicit ~78 %, guard pages ~91 % of wasmtime on CoreMark)

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
exception itself is gone: ctypes swallowed it). Wasmtime keeps the outcome of a host call apart from
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

Amended twice: S0005 (D41) added the `simd` method and the meaning of a v128 operand, in both backends, approved by the maintainer; then S0005 (D39 amendment item 5, Changes of 2026-11-02) added `move_v128`, which moves a v128 slot pair or writes a 128-bit constant as one 16-byte store (the store-forwarding fix, LESSONS 44), in both backends, approved by the maintainer.

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
Status: accepted (maintainer, 2026-11-02) · Date: 2026-11-02 · Supersedes: D29 (only its "guard by default where verified") · Evidence: [S0006](specs/S0006-guard-pages.md) measurements (CoreMark +7.5 % median over 9 alternated pairs, the PoC's own pair +6.9 % on this machine; code −21 %)

Guard pages (S0006) are a JIT option chosen explicitly (`bounds = "guard"` or `"auto"`); the default
stays `explicit`. The rest of D29 holds: the mode is a compile-time property recorded in the artifact
and the cache key, explicit checks are the only mode of a new architecture, of memory64 and of hosts
without a verified handler. Making `guard` (or `auto`) the default needs measurements on several
machines and a new decision.
