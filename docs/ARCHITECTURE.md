---
title: Architecture
kind: reference
status: current
updated: 2026-11-03
---

# Architecture

## Principles

1. **No runtime dependencies.** At run time waboros uses the Python standard library only (`struct`,
   `mmap`, `ctypes`, `marshal`, `hashlib`, `os`, `threading`). Tools used while *developing* waboros
   (wabt, wasm-tools, wasmtime as a reference, clang for experiments) never reach the user.
2. **Wasmtime is the reference.** Its object model, its API (as wasmtime-py exposes it), its CLI, its
   trap codes and its set of features are what waboros follows. We do not invent what Wasmtime already
   answers; we replace only its compiler (Cranelift / Winch / Pulley) with our targets.
3. **Translation, not interpretation.** Wasm is compiled once into the form the host runs best:
   Python code for an interpreter, machine code where executable memory is allowed. The PoC shows
   why: a pure-Python interpreter (pywasm) runs CoreMark at 0.58 it/s, the translation at 86 it/s,
   the JIT at 15 000 it/s.
4. **Targets are added one at a time** behind one interface, with one test suite. A new architecture
   is a new target, never a change to the others.
5. **Everything works everywhere; speed where the platform allows.** The `pyc` target is the floor
   that every Python can run; JIT targets are taken when the machine, the OS and the module allow it.

## Layers

```
            .wasm / .wat                                   cache (~/.cache/waboros)
                 │                                                  ▲ │
   ┌─────────────▼──────────────┐                                   │ │
   │ frontend                    │ decode · validate · const exprs  │ │
   │  (pure, target-independent) │ names section · features gate    │ │
   └─────────────┬──────────────┘                                   │ │
   ┌─────────────▼──────────────┐                                   │ │
   │ IR + analyses               │ operand stack resolved to trees  │ │
   │  (pure, target-independent) │ structured control tree          │ │
   │                             │ value bounds / wrap analysis     │ │
   │                             │ local use weights (loop depth)   │ │
   │                             │ side-effect order, call graph    │ │
   └─────────────┬──────────────┘                                   │ │
   ┌─────────────▼──────────────┐   Artifact (serializable) ────────┘ │
   │ targets                     │ pyc · mpy · jit-x86_64 · jit-aarch64 · jit-riscv64 (planned, D48)
   └─────────────┬──────────────┘   ◄─────────────────────────────────┘
   ┌─────────────▼──────────────┐
   │ runtime                     │ Engine · Store · Module · Linker · Instance
   │                             │ Func · Memory · Table · Global · Tag · Trap
   └─────────────┬──────────────┘
   ┌─────────────▼──────────────┐
   │ wasi                        │ host for WASI p1 and fused p2 (to decide; fusedwasi is a candidate)
   └─────────────────────────────┘
```

Proposed package layout (the full target; what exists at HEAD is listed after it):

```
waboros/
  frontend/      reader.py  module.py  validate.py  constexpr.py  names.py  wat.py (optional)
  ir/            hir.py (stack → trees, structured control)  analyses.py  opt.py  types.py
    lir.py       (machine-neutral linear form for the JITs, built from HIR)
  targets/
    base.py      Target protocol, Artifact, registry, selection
    pyc/         codegen.py  runtime.py (the helpers emitted into / imported by modules)
    mpy/         codegen.py (flavour of pyc)  native.py  (viper.py: HIR → viper code, later)
    jit/
      common/    asm.py  execmem.py (mmap/mprotect/MAP_JIT/VirtualAlloc)  bridge.py (ctypes entry,
                 host callbacks)  frame.py (value stack, frame overlap)  regalloc.py  traps.py
      x86_64/    encoder.py  abi_sysv.py  abi_win64.py  lower.py
      aarch64/   …
  runtime/       engine.py config.py store.py module.py linker.py instance.py func.py memory.py
                 table.py global_.py tag.py trap.py values.py types.py caller.py
  wasi/          config.py (WasiConfig)  p1.py  p2.py (bindings to the chosen WASI host)
  cache/         store.py keys.py
  cli/           main.py (run, compile, wast, settings, cache)
  __main__.py
```

At HEAD `src/waboros/` holds: `frontend/` (`binary.py` reader, `validate.py`, `simd.py` the one SIMD
operator table with its decoder and validator), `runtime/` (`link.py`, `store.py`), `targets/base.py`
(the `Target` protocol, registry, `auto` selection, `JitUnavailableWarning`), `targets/pyc/`
(`codegen.py`, `runtime.py`, `simd.py`), and `targets/jit/` with `common/` (`compiler.py` the one-pass
core, `backend.py` the backend interface, `context.py`, `simd.py` the core's v128 handling, and the
host side: `execmem.py`, `instance.py`, `target.py` the glue shared by the architectures) and
`x86_64/`, `aarch64/` (each `encoder.py`, `abi.py`, `backend.py`, `simd.py`). The compiler side
(frontend, `compiler.py`, `backend.py`, `context.py`, the `simd.py` files, the backends, encoders and
`abi.py`) imports no `ctypes`, `mmap`, threads or host-side module (`tests/test_source_rules.py`,
MICRO-COMPILERS.md). There is no separate `ir/` yet: the JIT core reads the validated body directly
(D26) and `pyc` has its own tree builder; HIR and its analyses wait for a measurement that needs
them. `mpy`, `cache/`, `wasi/`, `cli/` and the public runtime objects do not exist yet; `jit-riscv64` is planned (D48).

The PoC files map onto this as follows: `w2py.py` holds the reader, the decoder, the stack-to-tree
builder, the value-bounds analysis and the `pyc` code generator in one file; `w2jit.py`/`w2jit2.py`
hold the assembler, the executable-memory and ctypes bridge and the x86-64 lowering. Splitting them
along the lines above is the first step of the roadmap.

## The IR

**Wasm is already the portable, optimised IR.** It is typed, validated and optimised by its producer
(clang `-O3`, rustc: inlining, unrolling, common subexpressions are done before waboros sees it). That is
why waboros needs no LLVM-class optimiser — measured: a baseline JIT with no such optimisations reaches
~80 % of Cranelift on CoreMark. What wasm does not give a code generator directly is computed once, in
a thin IR of our own, and shared by all targets.

**Two levels.**

| level | shared by | form | built from |
|---|---|---|---|
| **HIR** | every target (`pyc`, `mpy`, JITs) | expression trees (the operand stack resolved), **structured** control flow (block / loop / if / br with targets and arities), annotations from the analyses | validated wasm function bodies |
| **LIR** | every JIT architecture | the backend's **call interface**, not a materialised list (D26): machine-neutral operations the shared JIT core calls in one pass; operands *imm*, *reg* (pinned or backend-chosen), *slot*, or *addk* (a pinned i32 plus a constant, only as a `move` source or a memory address; S0007); three-operand; compare-and-branch / compare-and-set as whole operations (no flags); labels, trap points, call sites with their convention | the validated function body (and HIR analyses when they exist), by the shared JIT core |

**The lazy operand stack is the JIT core's HIR-lite.** The shared JIT core builds no HIR trees; it
keeps a lazy operand stack (`common/compiler.py`) whose entries are at most one node deep: a constant,
a local, `tos`, a slot, one pending compare of such operands, or `local + const` (S0007 step 3; S0007
also plans a temporary register). A new entry kind is added only with a measurement that it pays, so any drift
towards trees is a recorded decision, not an accident. Stack resolution exists twice today: `pyc`'s tree
builder (`targets/pyc/codegen.py`) and the JIT core's lazy stack. A real HIR, if a measurement ever
calls for one, replaces both rather than becoming a third.

Per architecture only the implementation of that interface (instruction selection) and the encoder
remain; an allocator over a materialised LIR is added only if a measurement shows it pays (D26). With copy-and-patch
(TARGETS.md) each LIR operation would become a stencil.

**Why structured, not a CFG of basic blocks**: Python (and Lua 5.1) has no `goto`, so `pyc`/`mpy` need
structure; a JIT lowers structure to labels trivially. **Why not SSA**: phi nodes buy GVN/LICM-class
optimisations that pay little on already-optimised wasm, and SSA would have to be deconstructed back
into structured code for Python. Wasm locals are kept as variables; register allocation pins the hot
ones (TARGETS.md).

**Analyses** (annotations on HIR, computed once):

* value bounds and wrap state of integer expressions (when a `& 0xFFFFFFFF` or compare-and-adjust is
  needed — worth ~15 % in `pyc`);
* local use weights by loop depth (which locals to pin in registers), liveness where it helps;
* side-effect order (memory, globals, calls, traps): when a tree may stay lazy and when it must be
  materialised;
* branch targets with arities; trap points with wasm byte offsets (for `TRAPMAP` and `Trap.frames`);
* call graph (leaf functions, recursion, inlining candidates).

**Language-neutral by design (a principle, not a goal).** HIR, LIR and the JIT artifact contain
nothing specific to Python, so other output languages or other host languages stay possible without
redesign — the PoC already drove Lua 5.4 and LuaJIT backends from the same front end. Rules for the
shared layers:

* the **integer model is a parameter of the target**, not a property of the analyses: unbounded ints
  (Python), wrapping int64 (Lua 5.4), doubles / `|0` (LuaJIT, JS), real 32-bit registers (JITs). The
  bounds/wrap analysis answers "where must this target normalise", given that model;
* **f32/f64 constants leave the front end as bit patterns** (ints), not floats (D46), so the shared
  layers hold no NaN-as-double convention; the JIT takes the bits directly and `pyc` turns them into
  its float literals. `pyc` also has a second mode, **bits mode** (`ModGen.fb`, `runtime.FBITS`),
  in which f32/f64 values are ints in every wasm value and arithmetic makes a float inside one
  expression only. It is chosen by a probe at import (a host whose floats quiet a signalling NaN,
  such as 32-bit x86 with the x87 FPU, gets it) or by `WABOROS_PYC_FBITS=1|0`; CI also runs it on
  x86-64. A future `pyc` cache key must include the mode;
* no Python objects, names or semantics in HIR/LIR nodes; whatever a backend needs from its language
  lives in the backend;
* the compiler is written in Python, so for other languages it works ahead of time (generate code or
  an artifact; for JIT artifacts the other language needs only a loader, ARTIFACT.md).

Other languages are **not in scope** now; this only keeps the door open cheaply.

**Optimisations: only those that pay for themselves, each measured.** Constant folding; dead code
after unconditional branches; compare + branch fusion; in-place updates of pinned locals (`x op= y`);
loads straight into a pinned local; and, the one that matters most for JITs, **bounds-check
elimination** — e.g. one check per loop for a pointer that advances monotonically, or for accesses with
a common base and small constant offsets — because where guard pages are unavailable (the rows without a verified handler: Linux musl
aarch64 and Android arm64 today, D44) the package's JIT uses explicit checks, and these checks stay a large part of
the ~20 % gap to Cranelift.

## The object model (after Wasmtime)

| Object | Owns / does | waboros notes |
|---|---|---|
| `Config` | features, target choice, cache, stack limits, fuel/epoch switches | `target='auto'` or a name; Wasmtime's Cranelift options have no meaning here and are accepted and ignored, or rejected (decide) |
| `Engine` | a `Config`; the selected target; the compiled-code registry | one per process is typical; selection is resolved here (see TARGETS.md) |
| `Module` | the validated module, its imports and exports, the target `Artifact` | `Module(engine, wasm)`, `from_file`, `validate`, `serialize`, `deserialize`, `deserialize_file` |
| `Store` | instances, memories, tables, globals, host data, limits, fuel, epoch deadline, the WASI context | every runtime object belongs to one store; host functions receive a `Caller` |
| `Linker` | name → extern resolution; `define`, `define_func`, `define_wasi`, `define_instance`, `define_module`, `instantiate`, `instantiate_pre` | resolution and type checks are target-independent |
| `Instance` | the instantiated module: exports | the target builds the executable form; the runtime wraps it |
| `Func`, `Memory`, `Table`, `Global`, `Tag` | extern values | `Func` calls cross from Python into the target's code; `Memory` must offer `read`/`write`/`data_ptr`/`data_len`/`grow` for all targets |
| `Trap` / `TrapCode` | wasm traps with Wasmtime's codes | targets report a code; the runtime raises `Trap`; `ExitTrap` for `proc_exit` |
| `WasiConfig` | argv, env, preopens, stdio | fed into the WASI host (fusedwasi's `Host` / `State` if it is chosen) |

## The pipeline of a call

1. `Module(engine, wasm)`: frontend → IR → `engine.target.compile(ir, config)` → `Artifact`.
   With the cache on, the artifact is looked up first (key below) and stored after.
2. `linker.instantiate(store, module)`: resolve imports, then `target.instantiate(artifact, store,
   imports)` creates memories, tables, globals, runs active segments and the start function.
3. `instance.exports(store)["f"](store, *args)`: the `Func` converts arguments and enters the target
   (`pyc`: a Python call; JIT: the ctypes entry trampoline). Traps come back as a code, the runtime
   raises `Trap(code)`.

## Memory

All targets must share one `Memory` facade, because hosts (WASI included) read and write guest
memory without knowing the target.

* `pyc`: a `bytearray` subclass (grows in place with `extend`; struct `unpack_from`/`pack_into` on it).
  Temporary `memoryview`s are fine; a long-lived export blocks `extend` on CPython (`BufferError`).
* JIT: a raw mapping whose address the code holds in a register (r13). At HEAD it is a 4 GiB + 32 MiB
  reservation per 32-bit memory in either bounds mode (`MEMORY_RESERVATION`, Wasmtime's 64-bit
  `memory_reservation` plus its guard, S0006) committed on `memory.grow`,
  which never moves the base; `memory.grow` is a libcall through the host bridge (S0003). Growth must not
  move it while code runs: reserve the maximum address range up front (wasm32: 4 GiB + slack on a
  64-bit host; less, with explicit checks, on 32-bit hosts) and commit pages on `memory.grow`
  (`mprotect` / `VirtualAlloc(MEM_COMMIT)` through ctypes). The Python `mmap` module cannot reserve
  without committing portably, so the reservation goes through ctypes.
* `memory64`, `multi-memory`, shared memory (threads) and custom page sizes are properties of the
  facade and of the code generators, not of separate paths.
* Bounds checks: explicit in `pyc`/`mpy`; in the JIT guard pages by default where verified (D44),
  explicit checks elsewhere. **The JIT can use guard pages without a native dependency**
  (measured, `poc/w2jit_guard.py`): an 8 GiB `PROT_NONE` reservation with only the wasm pages
  read/write, and a SIGSEGV handler that is **machine code emitted by the JIT** (a Python handler
  cannot run inside a signal) and installed with `sigaction` through ctypes. It resumes a fault at a
  reserved address raised by this module's code in the out-of-bounds trap stub (it rewrites RIP in the
  `ucontext`, as Wasmtime does), and passes everything else to the previous handler or restores the
  default action. In the package (S0006, complete: steps 1-5 and 7): guard code on Linux glibc (`jit-x86_64` since step 1, `jit-aarch64` since step 2), on Linux musl and Android for `jit-x86_64`, on macOS and on Windows — accesses with `offset + size ≤ 32 MiB` unchecked (stores too, as in Wasmtime: precise on native x86-64; an emulator that splits a store, or Arm hardware, may tear it; D45), a fault map of the instructions
  that touch memory, a handler (`x86_64/abi.py`, `aarch64/abi.py`, registry in `common/guard.py`) that resumes at the
  function's own out-of-bounds stub and chains every other fault. The mode is `auto` by default (D44):
  guard on the verified rows (Linux glibc x86-64 and aarch64, Linux musl x86-64, macOS x86-64 and arm64, Windows x64 and arm64, Android x86-64), explicit elsewhere (Linux musl aarch64, Android arm64), silently; `WABOROS_JIT_BOUNDS=explicit`
  opts out, `=guard` requires it. macOS (both architectures, S0006 step 3) has a handler on SIGSEGV
  and SIGBUS, verified in CI; Windows (S0006 step 4) has a vectored exception handler, and Android x86-64 (step 5) a bionic `sigaction` behind ART's `libsigchain`, verified in CI (`platforms.yml`; D47: a per-OS optimisation starts on one row and grows by exception). CoreMark: +9 % on x86-64 on a quiet machine (BENCHMARKS.md, "Guard
  pages"; +14 % on a loaded one, the PoC measured +20–25 % on another day) and +11 % on native aarch64, 20 % less machine code. The guard flag and the fault map (`TRAPMAP`) are not serialized yet: they go into the phase-5 artifact-format spec (ARTIFACT.md). Explicit checks stay the fallback (larger offsets,
  platforms without the handler yet, `memory64`); risks in RISKS.md, R6.

## State shared across targets

In the PoC each code generator had its own mini-runtime (`pyc`: a `bytearray`, Python globals, a list
of functions; the JIT: a raw mapping, context slots, code addresses). In the package one runtime owns
the state and every target works on it, because modules compiled by different targets can meet in one
`Store` and share a memory or a table. D25 settled the other half: **one target per engine and store**,
so at HEAD the two representations never meet in one store; the table is the shape the runtime keeps
open for it:

| state | one representation for all targets | `pyc` uses it as | JIT uses it as |
|---|---|---|---|
| memory | a mapping reserved at its maximum and committed on `grow` (above) | a buffer: `struct` / slices on it | base address + size in the context |
| table | per entry: type id + a Python callable + a code address (or a call stub) | the callable | the address, the type id for `call_indirect` |
| globals | slots in the context | attributes read through the instance (or cached and written back at the boundary) | context words |
| traps | `Trap` with Wasmtime's `TrapCode` | raised directly | trap code → raised at the entry |

Calls across the boundary work in both directions at the cost of a ctypes crossing (JIT → `pyc` as a
host function through `CFUNCTYPE`, `pyc` → JIT through the entry). To measure before deciding: `pyc`
on a reserved mapping instead of a `bytearray`. The alternative is one target per `Store` (fallback
for the whole store): simpler, but a single unsupported feature then slows every module of the
store: this is the chosen one (D25). Open question 12 in ROADMAP.md keeps only the `pyc` backing-store measurement.

## Calls, stack depth and recursion

Wasmtime traps with `STACK_OVERFLOW` when guest recursion exceeds `max_wasm_stack`. waboros must do the
same on every target, deterministically, and must never let a guest crash the interpreter.

**`pyc` (and `mpy`).** A wasm call is a Python call, so a deep guest recursion meets the interpreter's
limits (`sys.getrecursionlimit()`, 1000 by default, and the C stack). Plan:

1. **Depth counter.** Each translated function increments and decrements a per-store depth counter
   (or the call sites do); past `Config.max_wasm_stack` (converted to a call depth) it raises
   `Trap(STACK_OVERFLOW)`. Cost: two integer operations per call, measure on CoreMark.
2. **Never rely on `sys.setrecursionlimit` globally.** It is process-wide and affects the host
   program. If a deeper limit is wanted, run guest calls on a dedicated thread created with
   `threading.stack_size(...)` and raise the limit only around that call, restoring it after.
3. **Safety net.** `RecursionError` that escapes a guest call is converted to `Trap(STACK_OVERFLOW)`.
4. **Optional trampolined code** for modules that recurse deeply by design: calls become returns to a
   driver loop (slower, but bounded only by memory). A per-module flag, off by default.
5. Re-entrancy (wasm → host → wasm) is allowed: the counter is per store and nests naturally.

**JIT.** Two stacks:

* the value stack (wasm locals and operands, frame overlap as in wasm3): every function's prologue
  compares `frame + frame_size` against a limit in the context (the PoC does this) → trap 5;
* the native stack (return addresses, saved registers): checked against a limit in the context taken
  from the entry `rsp` minus a budget, so the thread's C stack is never exhausted.
* Re-entrancy: **the PoC entry trampoline stored one saved `rsp`**, so a nested entry (wasm → host →
  wasm) overwrote it (LESSONS.md). The package's entry keeps no saved `rsp`: a trap returns through the
  frames (S0003), so nested entries are safe; stack bounds per thread come from the OS (D10).
* v128: **two consecutive slots** (bytes 0-7, then 8-15) in locals, operands, parameters and results.
  Only SIMD operations see the pair; they use vector registers as scratch inside one backend call
  (`simd`, D41, S0005), never across calls, so there is no vector register class and no save around host
  calls. The core itself moves a v128 pair whole through `move_v128` (locals, constants, `select`). In `pyc` a v128 is a Python `int`.
* Trampolines: `abi.assemble()` of an architecture only returns bytes (compiler side); the host side
  (`instance.load_trampolines`) puts them into a code area and seals it, so the compiler side stays free
  of `ctypes`/`mmap`.

## Long calls and the GIL

A single call into wasm can run for a long time (CoreMark's `setup` runs ~20 s). What that means
differs per target:

**JIT: the GIL is released, but the call cannot be interrupted from Python.** The entry is a
`ctypes.CFUNCTYPE`, which releases the GIL for the duration of the call (`PYFUNCTYPE` would keep it);
host callbacks take it back automatically. Measured with the PoC (`w2jit2`, CoreMark): during a 19.6 s
guest call another Python thread kept running (17 175 iterations of a 1 ms sleep loop, ~876/s).
But Python handles signals only between its own bytecodes: a SIGINT sent 3 s into the call did not
stop it — the call returned normally after 20.6 s. Consequences:

* **Interruption is the code's job**, as in Wasmtime: the machine code polls an interrupt/epoch word
  in the context at loop back-edges and function entries (one compare + branch) and traps with
  `INTERRUPT`; `Engine.increment_epoch()` or an `interrupt()` call from any thread sets it. For Ctrl+C
  the CLI (and an opt-in API mode) runs the guest call on a worker thread while the main thread waits
  in a loop it can be interrupted in, and on `KeyboardInterrupt` sets the word.
* **A store is used by one thread at a time** (Wasmtime's rule too): with the GIL released, another
  Python thread could otherwise touch the same memory or store mid-call. The runtime guards it (owner
  thread per store; re-entry from a host callback on the same thread is allowed).

**`pyc` (and `mpy`): the guest is Python bytecode and holds the GIL while it runs.** CPython switches
threads every `sys.getswitchinterval()` (5 ms), so other threads are not frozen, but they compete for
the same GIL: no real parallelism, and an asyncio loop in another thread gets latency. Ctrl+C works,
because signals are handled between bytecodes. Options, all stdlib, to choose from (measure each on
CoreMark):

1. **Yield points** in generated code: the same back-edge counter used for fuel/epochs, every N
   iterations calling `time.sleep(0)` (gives the GIL away) and checking the interrupt word. Removes
   the latency problem, not the lack of parallelism; costs something per back-edge.
2. **A separate interpreter with its own GIL**: CPython 3.12+ subinterpreters (`concurrent.interpreters`
   in 3.14). Real parallelism without processes; sharing linear memory with the host side is not
   checked yet.
3. **Free-threaded CPython** (3.13t / 3.14t): parallel as is; the one-thread-per-store rule becomes
   essential, and atomics of shared memories need real synchronisation.
4. **A separate process** with `multiprocessing.shared_memory` for linear memory: heaviest, works
   everywhere CPython does.

Deep recursion is a separate limit of `pyc` (previous section), not a GIL question.

## Traps

Targets report a numeric trap code that maps to Wasmtime's `TrapCode` (`UNREACHABLE`,
`MEMORY_OUT_OF_BOUNDS`, `INTEGER_DIVISION_BY_ZERO`, `INTEGER_OVERFLOW`, `BAD_CONVERSION_TO_INTEGER`,
`STACK_OVERFLOW`, `INDIRECT_CALL_TO_NULL`, `BAD_SIGNATURE`, `TABLE_OUT_OF_BOUNDS`, `NULL_REFERENCE`,
`OUT_OF_FUEL`, `INTERRUPT`, `UNCAUGHT_EXCEPTION`, …).

* `pyc`: a `Trap` exception raised by the generated code or its helpers; Python errors that mean a
  trap (`struct.error`, `IndexError` on memory, `RecursionError`, and on 32-bit hosts the
  `OverflowError` of an index too large for a C `ssize_t`, mapped to the out-of-bounds trap) are
  converted at the boundary.
* JIT: trap stubs store the code in the context and return it through every native frame (each call
  site checks the status, CET-safe, S0003) to the entry trampoline, which returns it to Python; the
  runtime raises. A frame list for `Trap.frames` needs a code-address → function map
  (kept in the artifact).
* Host exceptions raised inside a host function called from wasm must cross back: `pyc` lets them
  propagate; a JIT callback catches them, stores them in the store and returns a "host error" code
  that unwinds like a trap, after which the runtime re-raises the original exception.

## Interruption: fuel and epochs

Wasmtime offers `consume_fuel` and `epoch_interruption`. Both are counters checked at function entry
and loop back-edges; in `pyc` they cost noticeably, so they are compiled in only when the `Config`
asks (the flag is part of the cache key). In a JIT the check is one decrement + branch on a context
slot.

## Cache

Like Wasmtime's cache (and `Module.serialize`), compiled artifacts are kept on disk.

* **Key**: sha256 of the wasm bytes + waboros version + target id + target environment + every config
  option that changes generated code (features, fuel/epoch, stack limits, bounds-check mode).
  Target environment: for `pyc` the implementation and bytecode magic (`importlib.util.MAGIC_NUMBER`,
  `sys.implementation.cache_tag`); for `mpy` the `.mpy` version and architecture; for a JIT the
  architecture, ABI and CPU features the code assumes.
* **Content**: `pyc` → the marshalled code object (exactly what a `.pyc` holds) plus metadata;
  `mpy` → the `.mpy`; JIT → the artifact of [ARTIFACT.md](ARTIFACT.md) (position-independent code,
  entry trampolines per calling convention, tables, segments; declared ISA level and context ABI).
* **Where**: `$XDG_CACHE_HOME/waboros` (or the platform's equivalent, computed with stdlib only); atomic
  writes (temp file + `os.replace`); a size limit with LRU eviction; `waboros cache` in the CLI.
* `Module.serialize()` / `deserialize()` write and read the same artifact format, so a module can
  also be compiled ahead of time and shipped (`waboros compile`, a file like Wasmtime's `.cwasm`: it
  needs the waboros runtime, nothing is embedded — DECISIONS.md D18). Optionally **fat**: the file
  carries the original wasm and regenerates itself on any mismatch instead of being refused. What to cache versus ship, and the
  trust problem of artifacts from other machines: RISKS.md, R1.
* Measured cost it saves (PoC, CoreMark): `pyc` translation + `compile()` ≈ 111 ms, from cache
  ≈ 0.5 ms; the JIT compiles in ≈ 25 ms.

## Targets and architectures (D48)

Native JIT targets exist for x86-64 and aarch64 and are planned for riscv64 (the first
micro-compiler after `arm64.wasm`, MICRO-COMPILERS.md); the JIT artifact is independent of the
operating system (D23). Every other architecture (s390x, i686 and 32-bit Windows, armv7, ppc64le,
loongarch64, …) runs `pyc`, permanently, with the D31 warning from `auto`. Pyodide is a track of its
own (PYODIDE.md), not a JIT target. `mpy` is separate from this list (D48).

## Cross-compilation

The JIT compiler emits bytes and never executes them while compiling, and a JIT artifact depends on
the architecture and its declared ISA level only (D23, D24). So any host compiles for any `jit-<arch>`
waboros has, as Wasmtime's `--target` does (D35): a portable artifact made on x86-64 for aarch64 is
loaded and run on aarch64 only, the loader refusing another architecture or a missing ISA level.
Only explicit levels are valid for another target (`"native"` is the compiling CPU, D33). Artifact
files come in phase 5; from phase 4 CI compares the aarch64 artifact made on x86-64 with the one made
on the arm64 runner byte for byte. `pyc`/`mpy` artifacts belong to an interpreter version, not a CPU.

## Versioned contracts

Everything waboros generates is a derivative of the wasm and is consumed by code that may come from
another waboros version. Each interface between them is a versioned contract:

| contract | between | where |
|---|---|---|
| JIT artifact format + context ABI + ISA level | the JIT compiler and any loader (Python or another language) | ARTIFACT.md |
| **generated-module ABI** (`pyc`, `mpy`) | a generated Python module and the runtime: how it receives imports and memories, which helpers it calls, how it reports traps, how the runtime finds its exports | **internal** (D18): generated code is used only by the runtime version that made it; an exact match of the waboros version is required, otherwise regenerate |
| cache key fields | a cache entry and the runtime looking it up | "Cache" above |

On a mismatch the runtime regenerates from the wasm when it has it (the cache; a fat file) and refuses
otherwise (a thin file). No compatibility windows are kept (D18; RISKS.md, R4).

## WASI

**The WASI host is not decided.** What the runtime needs from one: a flat function per import that
takes the store's state and a memory, so `Linker.define_wasi()` can bind names to functions.

The candidate is **fusedwasi** (natify-sh/fusedwasi, branch `wasi-p2` at the time of writing): one
flat function per import, in C and in a Python twin, for WASI 0.1 (`wasi_snapshot_preview1`,
`wasi_unstable`) and WASI 0.2 (all imports of the command world and `wasi:http`). **It has not been
used with waboros yet**: only its README and the Python twin's source were read, and one assumption was
checked separately (below). If it is chosen:

* `Linker.define_wasi()` binds the import names to the twin's functions (`partial(fn, host, comp)`);
  the names follow a fixed convention (`wasi:filesystem/types` + `[method]descriptor.read` →
  `wasi_filesystem_types_descriptor_read`).
* The twin takes "any writable buffer" as a memory: the `pyc` `bytearray` fits as is (checked: a
  temporary `memoryview` per call does not block `bytearray.extend` on CPython or PyPy); for a JIT
  the facade exposes the mapping as a buffer.
* `realloc` for p2 is the guest's `cabi_realloc` export of the component, wrapped.
* The component model itself is out of scope: components are fused into one core module first
  (`meld fuse`), which needs **multi-memory** (one memory per component). p1 programs can also go
  through the preview1 adapter + fuse, so one host serves both.
* The Python twin declares `requires-python >= 3.10` and uses `dataclasses`/`typing`, so it would
  set the minimum Python of WASI support and does not run on MicroPython. The host choice must not
  dictate the minimum Python of the whole package (ROADMAP.md, open questions).

## MicroPython

A target of its own (`mpy`), best effort:

* the `pyc` code generator with the MicroPython flavour (no `struct.Struct`, no `int.bit_length` /
  `bit_count`, no `memoryview.cast`; struct calls emitted with their format strings);
* artifacts: source compiled on the device, or `.mpy` made ahead of time with `mpy-cross`
  (69 it/s, 37 KB, 2 ms import for CoreMark); `@micropython.native` per function (94 it/s, faster
  than CPython); `@micropython.viper` is the promising next step (a 32-bit load through `ptr32` costs
  3.4 ns against ~150 ns through `struct`): wasm lowered to viper code lets MicroPython's own emitters
  act as native back ends on every MCU architecture (TARGETS.md);
* limits: small heap and C stack, 31-bit small ints on 32-bit MCUs (i32 values ≥ 2³⁰ become heap
  integers), a native-emitter bug in `mpy-cross` 1.29.0 on very large functions (LESSONS.md);
* the translator itself does not run on MicroPython yet (it imports `marshal`, uses `str.isidentifier`),
  so on-device compilation needs a port; host-side ahead-of-time compilation works today.
