---
title: The public Python API (phase 6, part 1: the runtime API)
kind: spec
id: S0018
status: draft
superseded_by:
related: [D1, D6, D10, D14, D17, D18, D19, D21, D25, D27, D29, D31, D33, D34, D44, D46, D51, D57, D58, D64, D70, R1, R4, S0001, S0002, S0012, S0013, S0014, S0015, S0017]
owner: maintainer
updated: 2026-11-25
---

# S0018 The public Python API (phase 6, part 1: the runtime API)

## Summary
waboros has no public API today: `src/waboros/__init__.py` exports only `__version__`, and every
caller (the spec runner, the tools, the tests) drives the internal interfaces of
[S0001](S0001-engine-interface.md) and [S0002](S0002-target-interface.md) (`tests/spec/target_engine.py`,
`src/waboros/runtime/store.py`, `src/waboros/runtime/link.py`, `src/waboros/targets/base.py`). This
spec defines the first public surface: `Engine`, `Config`, `Store`, `Module`, `Linker`, `Instance`,
the externs, `Caller`, `Trap`/`TrapCode` and the limits, **in the shape of wasmtime-py 50.0.1**
(ROADMAP.md, phase 6; [D6](../DECISIONS.md)). It is built over the internal engine interface, which
it amends only where a public operation has no internal counterpart yet. WASI and the CLI are out
of scope; each gets its own spec.

Direction (maintainer, 2026-11-25): the API follows wasmtime-py's shape. The one deliberate
extension is deterministic release by context managers, where wasmtime-py does not already have it,
with finalizers as a safety net. Every other difference from wasmtime-py is either something
waboros cannot do yet (an explicit error that names the reason, as [API.md](../API.md) states as its
principle) or an open question below. The API promises nothing that the language-neutral core
cannot give ([D17](../DECISIONS.md); [S0017](S0017-c-reference-loader.md) Decision 1 gives the C
loader no public API yet, so this spec is Python-only). It must still not depend on anything a C
host could not do the same way.

## Goals / Non-goals
* Goals:
  * a user of wasmtime-py recognises every name and signature: where waboros has a name, its
    signature and behaviour are wasmtime-py's, and the differences are listed in the tables below;
  * every public operation works on every target (`pyc`, `jit-x86_64`, `jit-aarch64`) through
    `Target` methods only (CLAUDE.md, "one interface for all targets");
  * deterministic release: `Store.close()` and `with` frees a store's memories, GC heap, code and
    contexts at once (LESSONS 135; ROADMAP "Memory of dropped stores" (c));
  * one error model: traps, host exceptions and engine errors look the same on every target;
  * a statement that 0.x may change (see "Compatibility promise").
* Non-goals:
  * WASI (`WasiConfig`, `Linker.define_wasi`, `Store.set_wasi`, `set_wasi_http`, `ExitTrap`) and the
    CLI: their own specs (ROADMAP phase 6; open question 2 there chooses the WASI host);
  * a drop-in replacement for wasmtime-py (ROADMAP phase 6: "not a drop-in"; see Q1);
  * `wasmtime.component` ([D7](../DECISIONS.md));
  * `SharedMemory`, `Config.shared_memory` and `wasm_threads`: these come with threads, as an
    additive extension ([D58](../DECISIONS.md));
  * the compilation cache on disk and its location: their own spec (ROADMAP, "The micro-compiler in
    the Python package", "Where the cache lives");
  * mixed stores, the per-architecture compatibility rule and foreign compilers' blobs: their own
    spec ([S0015](S0015-boundary-convention.md) "Compatibility"; ROADMAP moved them to the
    micro-compiler item);
  * `mpy` (phase 7) and the C loader's API (S0017 Decision 1).

## Interface

### Module layout: what is public
* `waboros/__init__.py` re-exports the public names and lists them in `__all__`. Nothing else is
  public.
* The implementation lives in private modules named after wasmtime-py's own (`waboros/_config.py`,
  `_engine.py`, `_store.py`, `_module.py`, `_linker.py`, `_instance.py`, `_func.py`, `_memory.py`,
  `_table.py`, `_globals.py`, `_tag.py`, `_types.py`, `_value.py`, `_trap.py`, `_error.py`).
* `waboros.frontend`, `waboros.targets` and `waboros.runtime` stay where they are and are internal.
  This is stated in README.md and API.md and in each package's docstring, and is not shown by a
  rename: a rename would touch every file in `src/`, `tests/` and `tools/` for no behavioural gain.
  An import from them carries no promise.
* Stdlib only ([D1](../DECISIONS.md)). WAT input works only when the optional extra
  `wasm-tools-package` is installed ([D21](../DECISIONS.md)); without it, a `str` argument gets an
  error that names the extra.

### The object model
Columns: the public surface, the wasmtime-py 50.0.1 counterpart (checked by introspection of the
pinned development install, `pyproject.toml`), and the deviation. "—" means none. "Explicit error"
means the call raises the engine error with the reason and never does something different in silence.

| class | public surface | wasmtime-py | deviation |
|---|---|---|---|
| `Config` | setters `wasm_*` (the features of FEATURES.md), `max_wasm_stack`, `epoch_interruption`, `consume_fuel`, `cache`, `signals_based_traps`, `strategy`; `close()`, `with` | the same names (wasmtime-py's `Config` derives from its `Managed` base: `close`, `with`, `__del__`) | `signals_based_traps` selects the bounds mode ("Configuration"); `consume_fuel=True` and `cache=True` give an explicit error until their steps; the options with no meaning here: Q5; target selection: Q3 |
| `Engine` | `Engine(config=None)`, `increment_epoch()`, `is_pulley` (always `False`), `close()`, `with` | the same | — (one target per engine, [D25](../DECISIONS.md)) |
| `Module` | `Module(engine, wasm)` (bytes; `str` is WAT, D21), `from_file`, `validate`, `imports`, `exports`, `serialize`, `deserialize`, `deserialize_file`, `close()`, `with` | the same | `serialize` on a `pyc` engine: explicit error ([S0014](S0014-artifact-format.md) Q6); WAT needs the extra (D21) |
| `Store` | `Store(engine=None, data=None)`, `data`, `gc()`, `set_epoch_deadline`, `set_limits`, `get_fuel`, `set_fuel`, `close()`, `with` | the same, plus `set_wasi`, `set_wasi_http` | `get_fuel`/`set_fuel`: explicit error until fuel exists; WASI out of scope; **extension over wasmtime-py**: `with store.scope():` (GC handle scopes, "GC handles") |
| `Linker` | `Linker(engine)`, `allow_shadowing`, `define`, `define_func`, `define_instance`, `define_module`, `get`, `get_default`, `instantiate`, `instantiate_pre`, `define_unknown_imports_as_traps`, `define_unknown_imports_as_default_values`, `close()`, `with` | the same, plus `define_wasi` | WASI out of scope |
| `InstancePre` | `instantiate(store)`, `module`, `close()`, `with` | the same | — |
| `Instance` | `Instance(store, module, imports)`, `exports(store)` (by name and by index) | the same | — |
| `Func` | `Func(store, ty, func, access_caller=False)`, `type(store)`, `__call__(store, *params)` | the same | value types beyond wasmtime-py's: Q2 |
| `Caller` | `get(name)`, `[name]`; usable as the store argument while the host call runs | the same | — |
| `Memory` | `Memory(store, ty)`, `type`, `size`, `grow`, `read`, `write`, `data_ptr`, `data_len`, `get_buffer_ptr`, `page_size`, `page_size_log2` | the same | `data_ptr` on `pyc` points into a `bytearray` that a grow may move ("Memory access") |
| `Table` | `Table(store, ty, init)`, `type`, `size`, `grow`, `get`, `set` | the same | `pyc` and the JITs cap `grow` at 10 000 000 elements unless `set_limits` says otherwise ([D27](../DECISIONS.md)) |
| `Global` | `Global(store, ty, val)`, `type`, `value`, `set_value` | the same | — |
| `Tag` | `Tag(store, ty)`, `type`, `eq` | the same | — |
| types | `ValType`, `FuncType`, `MemoryType`, `TableType`, `GlobalType`, `TagType`, `Limits`, `ImportType`, `ExportType`, `Val` | the same | the GC, exception and `v128` types: Q2 |
| errors | `Trap` (`message`, `trap_code`, `frames`), `TrapCode`, the engine error (Q1), `Frame` | `Trap`, `TrapCode`, `WasmtimeError`, `Frame`, `ExitTrap` | `frames` is empty until a code-address map exists (FEATURES.md, "traps with frames"); `ExitTrap` comes with WASI; the error's name: Q1 |
| warning | `JitUnavailableWarning` | none | re-exported as D31 decides; it already exists (`targets/base.py`) |
| function | `wat2wasm` | the same | needs the extra (D21) |

`Storelike`: as in wasmtime-py, a `store` argument takes a `Store` or a `Caller` (a `Caller` only
during its host call). `StoreContext` is not provided: wasmtime-py uses it for its own `Store`
plumbing, and it has no counterpart here.

### Values: Python ⇄ wasm
The internal boundary carries unsigned integers and float bit patterns (`Raw`, S0002). The public
layer converts as wasmtime-py does:

| wasm type | to wasm (a parameter, a host result, `Global`/`Table` values) | to Python | note |
|---|---|---|---|
| `i32`, `i64` | `int` or `Val`; out of range: `ValueError` | signed `int` | wasmtime-py wraps through ctypes; a checked range is a deviation recorded in the step's tests |
| `f32`, `f64` | `float` or `Val` | `float` | an f32 NaN payload survives where the host's floats keep it ([D28](../DECISIONS.md), [D46](../DECISIONS.md)) |
| `funcref` | `Func` or `None` | `Func` or `None` | a `Func` of another store: `TypeError` |
| `externref` | any Python object, `None` is null | the object | — |
| `anyref`, `eqref`, `i31ref`, `structref`, `arrayref`, `exnref`, concrete GC types, `v128` | **Q2** | **Q2** | wasmtime-py 50 has no Python form for them (`Val` has no `anyref`; `v128` raises "unimplemented v128 type", `wasmtime/_value.py`) |

Results: `None` for no result, the value for one result, a `list` for more, as in wasmtime-py
`Func.__call__`. Host values given for a reference parameter are type-checked before anything
runs (`TypeError`, D57). A host `int` given for an `anyref` is an i31 in `[-2**30, 2**30)`, else
`ValueError` (S0013 step 3b decision G).

### Errors and exceptions
| what happens | what the user sees | today, internally |
|---|---|---|
| a wasm trap | `Trap` with `trap_code` a `TrapCode` member (wasmtime-py's names; the numeric values are not promised) | `targets.base.Trap(code)` |
| a wasm exception escapes the outermost call | `Trap` with `trap_code == TrapCode.UNCAUGHT_EXCEPTION`, as in wasmtime-py; access to the payload: Q2 | `Trap(UNCAUGHT_EXCEPTION)`, `.exn` = (tag, values) ([S0012](S0012-exceptions.md) Q4) |
| a host function raises | the same exception object comes out of the outermost `Func.__call__`, as in wasmtime-py (its `maybe_raise_last_exn`); wasm code cannot catch it | `HostError` carrier, re-raised at depth 1 (`targets/pyc/__init__.py` `_run`; JIT: D34) |
| an asynchronous exception kills a host callback before it reports its status (JIT) | `RuntimeError("host call interrupted")` | [D34](../DECISIONS.md); on PyPy: [D51](../DECISIONS.md) |
| invalid or malformed module, link failure, instantiation failure, a refused serialized file, an option or feature waboros lacks | the engine error (Q1) carrying the internal exception as `__cause__` | `Malformed`, `Invalid`, `LinkError`, `MemoryError` (S0009 Q6: "phase 6 maps it to the API's error"), `ArtifactError` (`.step`, `.section`, S0017 P2), `Unsupported` |
| a `jit-*` target forced on a machine where it cannot run | the engine error | `Unsupported` (D31, TARGETS.md) |
| `auto` falls back to `pyc` | one `JitUnavailableWarning` per process and message | D31 |
| a store used from a second thread | `RuntimeError("a store is used from one thread only")` | `StoreState.claim` |
| any use after `close()` | `ValueError("already closed")`, wasmtime-py's message | — (new) |
| wrong argument count or type | the engine error (count, as wasmtime-py), `TypeError` (type) | — |

Out-of-memory while reserving or committing stays `MemoryError`. The GC heap's failed commit still
escapes as `OSError` (ROADMAP, "Memory of dropped stores", open item). Step 4 makes it
`MemoryError` first.

### Configuration: public knobs and environment variables
| knob | public (`Config`) | meaning | environment variable today |
|---|---|---|---|
| target | Q3 | one target per engine (D25) | `WABOROS_TARGET` (`targets/base.py`, `target_order`) |
| bounds mode | `signals_based_traps`: unset or `True` is `auto` (guard pages where verified, explicit checks elsewhere; D44); `False` is explicit checks | a property of the compiled code (D29); a compile option per engine, no longer read from the process environment at compile time | `WABOROS_JIT_BOUNDS=auto|guard|explicit` (`jit/common/guard.py`, `mode`) |
| stack | `max_wasm_stack` (bytes) | the JIT's native budget per store ([D10](../DECISIONS.md)); `pyc`: "Stack" below | — (`StoreState.max_wasm_stack`, default 512 KiB in `jit/common/instance.py`) |
| epochs | `epoch_interruption` | "Interruption" | — |
| fuel | `consume_fuel` | explicit error until fuel exists | — |
| features | `wasm_*` | `True` for every feature that FEATURES.md claims on all targets; `False`: Q5 | — |
| cache | `cache` | `False`; `True` gives an explicit error until the cache spec | — |

Precedence: a value set on `Config` wins. A knob left unset takes its environment variable, so the
CI matrices that set `WABOROS_TARGET` and `WABOROS_JIT_BOUNDS` keep working unchanged. Forcing
`guard` on a row where the handler is not verified (Android arm64, S0017 Decision 10) stays
environment-only: it is a development opt-in, not a user setting.

These remain **environment-only, for development**, and are not part of the API:
`WABOROS_JIT_NOPIN`, `WABOROS_JIT_NOLIVE`, `WABOROS_JIT_PAD` and `WABOROS_JIT_LOOP_ALIGN` (they
change code bytes, and the artifact writer refuses to write while they are set: `artifact.SWITCHES`,
S0014 Q11); `WABOROS_PYC_BR` and `WABOROS_BT` (the `pyc` emission studies of
[S0010](S0010-pyc-branch-emission.md)); `WABOROS_PYC_FBITS` (D46: chosen by the host's float
behaviour; the variable only forces it); `WABOROS_JIT_GC_STRESS`, `_GC_VERIFY`, `_GC_POISON` and
`WABOROS_JIT_WALK_CHECK` (test modes); `WABOROS_JIT_ARTIFACT` and `WABOROS_JIT_DUMP` (test paths);
`WABOROS_SLJIT_LIB` (the development targets of S0016). ROADMAP "Next" (6) revisits the
code-changing ones with the final freeze. This spec leaves them as they are.

## Semantics

### Lifetimes, `close()` and finalizers
* **What wasmtime-py already has.** Its `Managed` base gives `close()`, `__enter__`/`__exit__` and a
  `__del__` that closes to `Config`, `Engine`, `Store`, `Module`, `Linker`, `InstancePre`, the type
  objects, `Trap` and `WasmtimeError`. After `close()`, every use raises `ValueError("already
  closed")`, and a second `close()` does nothing (`wasmtime/_managed.py`). `Func`, `Memory`, `Table`,
  `Global`, `Tag`, `Instance` and `Caller` are not `Managed`: they hold their store, and a use after
  the store is closed fails through it. waboros follows this exactly, so `with Store(engine) as
  store:` is wasmtime-py's shape, not an extension.
* **`Store.close()`** (ROADMAP phase 6, LESSONS 135) releases at once everything the store owns:
  each instance's code areas and contexts, linear memories, tables, the GC heap, the value stack
  and the store's trampoline copies. Every later use of the store, of its instances and externs,
  and of its `GcRef`/`ExnRef` handles raises `ValueError`. Called from a host function while that
  store's wasm is on the stack, `close()` raises the engine error ("store is in use") and releases
  nothing. Called from a thread that is not the owner while a call runs, it raises the
  `RuntimeError` of the one-thread rule. The mechanism is a `close()` on `StoreState` that calls a
  `release()` of each `TargetInstance` (an S0002 amendment, step 1). A C host does the same with a
  store-destroy call.
* **Finalizers as the safety net.** A store that is never closed is released when its public
  `Store` object becomes unreachable (`weakref.finalize`, not `__del__`, so that the finalizer never
  sees a half-torn object). The internal objects keep no reference to the public `Store`, `Instance`
  or extern wrappers, so the public objects stay out of the internal reference cycles of LESSONS 135.
  On CPython, the last public reference going away releases the store at once, without waiting for
  the cyclic GC. A user who keeps an `Instance`, a `Func` or a `Memory` keeps its store alive, as in
  wasmtime-py. On PyPy and on a free-threaded build the finalizer runs when the collector gets to it.
  `close()` and `with` are the deterministic path there.
* **`Engine.close()` and `Module.close()`** drop the compiled code the object holds. Instances
  already made keep their own mapped copy, because nothing is shared between stores
  ([D64](../DECISIONS.md)).

### GC handles and scopes (extension over wasmtime-py)
* Today (S0017 Decision 11, implemented in the Python runtime): a GC object or exception given to
  the host is a handle (`GcRef`, `ExnRef` in `jit/common/instance.py`) that is released when the
  host drops it. A weak entry's finalizer queues its slot, and the store drains the queue before a
  collection and before it makes a new handle. A held handle keeps its object alive and keeps its
  identity. `pyc` hands out its Python objects, which Python's own GC owns.
* **`with store.scope():`** (the extension). Every handle the store creates while the scope is open
  is released when the scope closes, whether or not the host still holds it. A use after that raises
  `ValueError`. Scopes nest, and handles made outside any scope behave as today. This gives a
  deterministic release where Python's collector would not (PyPy, free-threaded builds, cycles).
  It is the Wasmtime model (root scopes) that ROADMAP phase 6 asks for, written as a host
  convenience over one mechanism every host can implement: a scope is a mark in the handle table,
  and closing it releases every slot above the mark (a C host: a mark/release pair beside
  `waboros_ref_release`). On `pyc` a scope only invalidates the public wrappers, because there is
  nothing to release.
* Explicit long-lived roots (`unroot`) and generation-checked handles: Q4.

### Threads
* **One thread per store at a time** (ARCHITECTURE.md, "Long calls and the GIL"; D14). Today the
  first entry claims the store atomically, re-entry from a host function on the same thread is
  allowed, and another thread gets `RuntimeError` (`StoreState.claim`). A store built on one thread
  can run on another until its first entry (`StoreState.release`). The API keeps this as it is.
* `Engine` and `Module` may be shared between threads; instantiating one module in several stores
  on several threads is the D58 model. Step 7 adds a stress test before this is claimed. `Linker`
  definitions are not thread-safe, as in Wasmtime.
* `Engine.increment_epoch()` may be called from any thread.
* JIT calls release the GIL (`CFUNCTYPE`, D14). `pyc` calls hold it, with yield points from step 5.
* **Free-threaded CPython** (3.15t is tested, [D19](../DECISIONS.md)): the same rules. The audit of
  process-wide mutable state belongs to the threads spec (ROADMAP open question 8). Until then the API
  claims for 3.15t only what its tests show.
* **PyPy 3.11** is supported (D19), with D51's deviation: an asynchronous exception at an entry or
  an exit can leave the counters raised.

### Interruption: epochs, fuel and the interrupt word
* What exists: an interrupt word per instance that JIT code polls at function entries and loop
  back-edges, trapping with `INTERRUPT` (S0003; `StoreState.interrupt`). `pyc` has no poll: its
  `interrupt` is absent (`PycInstance` defines none; S0002 says "pyc: later").
* **Epochs over the interrupt word** (no change to generated code). The `Engine` keeps the epoch and
  a weak set of its stores. `Store.set_epoch_deadline(n)` records `epoch + n` and clears the store's
  interrupt request. `increment_epoch()` sets the interrupt word of each store whose deadline has
  been reached. A call that starts after its store's deadline sets the word before it enters. The
  trap is `TrapCode.INTERRUPT`, as in Wasmtime. Wasmtime compiles a compare against a deadline into
  the code; the per-store context words `FUEL` and `EPOCH_DEADLINE` exist in ABI v1 but are unused
  (ARTIFACT.md part 3). The host-side check gives the same observable behaviour at no cost per
  back-edge. Without `epoch_interruption=True` the deadline is ignored, as in Wasmtime.
* **`pyc`** needs yield points to observe the word (ARCHITECTURE.md, option 1: a counter on
  back-edges that checks the word and gives the GIL away every N iterations). Step 5 adds them only
  in modules compiled with `epoch_interruption`, and measures them on CoreMark (BENCHMARKS.md).
  Until step 5, `epoch_interruption=True` on a `pyc` engine is an explicit error.
* **Fuel** (`consume_fuel`, `set_fuel`, `get_fuel`) needs counters compiled into the code
  (ARCHITECTURE.md, "Interruption"). It is deferred. Until then: an explicit error.
* Ctrl+C on a worker thread is the CLI's (its own spec).

### Stack
* JIT: `max_wasm_stack` sets the native budget per store (today `StoreState.max_wasm_stack`;
  `jit/common/instance.py` uses 512 KiB when it is 0). Exhaustion is `Trap(STACK_OVERFLOW)`.
* `pyc`: a deep recursion hits Python's limit and becomes `Trap(STACK_OVERFLOW)` (`_run`).
  `max_wasm_stack` is not applied there: D10 and ARCHITECTURE.md ("Calls, stack depth and
  recursion", item 1) decide on a per-store depth counter, but none is implemented. Step 5 adds it,
  measured on CoreMark. Until then the deviation is listed in API.md.

### Limits
`Store.set_limits(memory_size, table_elements, instances, tables, memories)` as in wasmtime-py. The
checks sit in the runtime services that every target already calls (`memory.grow`, `table.grow`,
instantiation), so no generated code changes. A grow over a limit returns −1, and an instantiation
over a limit is the engine error. The `table_elements` default stays at D27's 10 000 000, which is a
deviation from Wasmtime's unlimited default recorded in D27. `set_limits(table_elements=...)` replaces
it, as D27 says.

### Memory access
`read`, `write`, `size`, `grow`, `data_len` and `page_size` work the same on every target. They go
through the S0002 amendment's memory handle methods, never through a target's own class.
`data_ptr`/`get_buffer_ptr`: on a JIT this is the mapping. An i32 memory never moves; an i64 memory
may move on a grow ([D50](../DECISIONS.md)). On `pyc` it is the address of the current `bytearray`,
valid until the next grow, as Wasmtime documents for a memory that may move. No `memoryview` is
handed out: an exported view of a `bytearray` would make a later `memory.grow` fail with
`BufferError`.

### Serialization
* `Module.serialize()` writes a **thin** file (S0014 v1) of the code the module was compiled with,
  for its target and bounds mode, as wasmtime-py writes what it compiled. `Module.deserialize` and
  `deserialize_file` read one with the loader's rules (S0014, "Semantics: the loader"; exact
  `compiler` string, S0015 "Compatibility"). A refused file is the engine error with the
  `ArtifactError` as its cause.
* A fat file (D18) is read by `deserialize` (it regenerates on a mismatch). Writing one has no
  wasmtime-py counterpart: Q6.
* The portable default of S0014 Q12 (explicit bounds for a file meant to travel) is reached through
  the public knob: an engine with `signals_based_traps=False`. The CLI's `compile` applies Q12 in its
  own spec.
* `pyc` modules: S0014 Q6 has not specified their file yet, so `serialize` on a `pyc` engine is an
  explicit error.
* Trust: files from elsewhere are R1's subject. `deserialize` is as unsafe as wasmtime-py's (its
  documentation says the same), and this is stated in the docstring.

### Target selection and the warning
The engine fixes its target when it is created (D25): `auto` by default, `WABOROS_TARGET` when set,
and in future the public knob of Q3. The `JitUnavailableWarning` of D31 is raised at `Engine()`, so
a user can filter it or turn it into an error with the `warnings` module.

### What the core must give (the S0002 amendment, step 1)
The public layer reaches targets only through `Target` and `TargetInstance` methods. For that it
needs, beyond what S0002 has today:
* extern handle operations: a function's call and type by handle (a `funcref` from a table or a
  global, not only an export index); a memory's size, grow, read, write and address; a table's size,
  grow, get and set; a global's get and set (S0002 already lists `global_set` "later (phase 6)"); a
  tag's identity and type;
* externs created by the host (`Memory(store, ty)`, `Table`, `Global`, `Tag`). Two ways: a target
  method each, or a tiny module synthesized by the runtime that exports the object (as `spectest`
  is a module today), with `global_set`/`table_set` for host-given initial values. The second
  changes no target. Step 1 measures both and chooses;
* compile options per engine (bounds mode, epoch yield points on `pyc`), passed to `compile` instead
  of read from the environment;
* `release()` per instance for `Store.close()`;
* the limit hooks in the grow and instantiation services.
Each of these is language-neutral: a C host needs the same operations.

## Placement of the ROADMAP phase-6 items
| item | here | reason |
|---|---|---|
| `Store.close()` and a context manager | in scope, step 4 | wasmtime-py's shape; LESSONS 135 |
| GC handle scopes | in scope, step 4 (`store.scope()`) | the maintainer's extension; S0017 Decision 11 leaves scopes to phase 6 |
| explicit roots (`unroot`), generation checks | Q4 | no wasmtime-py counterpart |
| `Module.serialize`/`deserialize` (thin, JIT) | in scope, step 6 | wasmtime-py names; S0014 implemented |
| fat files from the API | Q6 | no wasmtime-py counterpart |
| `pyc` files | deferred | S0014 Q6 not specified |
| the cache and its location (`.gitignore` rule) | deferred to the cache spec | ROADMAP's proposal is not a decision yet; `Config.cache=True` gives an explicit error |
| S0015's deferred items (per-architecture rule, foreign blobs, flags agreeing within a store) | out | the API has one target per engine (D25) and exposes no mixed stores; ROADMAP moved the rule to the micro-compiler item |
| interrupts and epochs | in scope, step 5 | host-side over the existing word |
| fuel | deferred | needs counters in the code and a measurement |
| `max_wasm_stack` | in scope (JIT now, `pyc` counter in step 5) | D10 |
| target and bounds selection | in scope: bounds via `signals_based_traps`; the target knob is Q3 | D25, D44 |
| `JitUnavailableWarning` | in scope, re-exported | D31 |
| GC and exception references | conversions: Q2 | wasmtime-py has none |
| traps against host exceptions | in scope | D34, "Errors and exceptions" |
| one thread per store | in scope, unchanged | ARCHITECTURE.md |
| free-threaded CPython | the rules hold; the claim waits for the threads spec's audit | ROADMAP open question 8 |
| PyPy | supported, D51 | D19 |
| JIT code visible to debuggers | out | its own item in ROADMAP phase 6 |
| `Trap.frames` | empty list | needs the code-address map |
| a host throwing a wasm exception | deferred | wasmtime-py 50 has no API for it (S0012 Q4) |
| NaN canonicalisation | Q5 (`cranelift_nan_canonicalization`) | S0005 Q5 waits for the `Config` work |
| `Config.isa` (D33) | deferred | no variant above the baseline exists yet ([D42](../DECISIONS.md)) |

## Compatibility promise
* Versions are `0.x` (setuptools-scm). README.md and the package docstring say: **"The API may change
  in any 0.x release before 1.0."** Every change is listed in the release notes.
* Within 0.x the intent, not a promise, is: names and signatures that match wasmtime-py stay
  matched, and a deviation is never silent.
* `waboros.frontend`, `waboros.targets` and `waboros.runtime` carry no promise at any version.
* Serialized files: exact match or refusal (thin), regeneration (fat), as D18 and R4 decide. This
  spec does not widen that.

## Test plan
* `tests/api/`: one file per class. Every test runs on every available target (parametrized `pyc`
  and the host's JIT, both bounds modes on the JIT), with modules written in WAT through the
  development tools (D21).
* **Differential against wasmtime-py** (TESTING.md's API axis), on the same programs: CoreMark, the
  corpus of `tools/run_corpus.py`, and the spec modules of chosen directories. The same calls are made
  through `waboros` and `wasmtime`, comparing results, trap codes, memory hashes, global values and
  the exception type of host errors.
* **API-surface diff** against wasmtime-py 50.0.1 (TESTING.md): `dir()` of every public class.
  Missing names are reported, and the expected ones (WASI, components, `SharedMemory`) are listed in
  a file, as expected spec failures are.
* **wasmtime-py's own test suite through a shim** (TESTING.md): run as a counter, not a gate (Q1).
* Lifetimes: `close()` and `with` release memory. The LESSONS 135 reproduction (N stores with a GC
  heap under `prlimit --data`) passes when each store is closed, and when each is dropped on CPython.
  Use after close raises `ValueError`. A scope invalidates its handles.
* Threads: a store on a second thread raises; `increment_epoch` from another thread stops a long
  call on every target; host exceptions come out as the same object; D34's asynchronous test through
  the public API.
* The spec runner: options in Q7.
* Acceptance of the whole spec: `uv run pytest -q tests/api` passes on every CI row; the
  differential run is green; `python3 tools/check_docs.py` passes.

## Plan of steps
| step | goal | agent | acceptance | size (lines, estimated) |
|---|---|---|---|---|
| 0 | this spec approved; the S0002 amendment written as text (the list in "What the core must give") | orchestrator, maintainer | approval; `python3 tools/check_docs.py` | docs ~150 |
| 1 | S0002 amendment implemented on `pyc` and both JITs: extern handle operations, host externs (the two ways measured), compile options per engine, `release()`, limit hooks | jit-engineer (JIT parts) + implementer (`pyc`), then reviewer | the spec suite at parity on the three targets, both bounds modes; new `tests/test_handles.py`; `pytest -q` | src ~350, tests ~300 |
| 2 | the skeleton: `Config`, `Engine`, `Module`, the types, the errors, `__init__` with `__all__`; WAT through the extra | implementer | `pytest -q tests/api -k "config or engine or module or types"` | src ~600, tests ~400 |
| 3 | `Linker`, `Instance`, `InstancePre`, `Func`, `Caller`, `Memory`, `Table`, `Global`, `Tag`; the value conversions of the table above (Q2's answer included) | implementer | `pytest -q tests/api`; the differential run on CoreMark | src ~900, tests ~700 |
| 4 | `Store.close()`, `with`, the finalizers, `store.scope()`, `Store.gc()`, `set_limits`; the GC heap's `OSError` made `MemoryError` | implementer, then reviewer (release of executable memory and the heap) | the lifetime tests; the LESSONS 135 reproduction | src ~300, tests ~250 |
| 5 | epochs over the interrupt word; `pyc` yield points and depth counter, both measured | implementer, then reviewer | the thread tests; CoreMark with and without `epoch_interruption` on `pyc` in BENCHMARKS.md | src ~250, tests ~150 |
| 6 | `Module.serialize`/`deserialize`/`deserialize_file` (thin, JIT) | implementer | round trip on every JIT row; a refused file is the engine error with `ArtifactError` as cause | src ~120, tests ~150 |
| 7 | the API-surface diff, the wasmtime-py suite counter, the corpus differential, Q7's runner option, the threading stress test | runner + implementer | numbers in TESTING.md; CI job | tests ~400, CI ~40 |
| 8 | docs ("Docs this changes"), a DECISIONS entry | orchestrator | `python3 tools/check_docs.py` | docs ~150 |
| 9 | review without the implementers' reasoning: the whole API against this spec and wasmtime-py | reviewer | findings closed | — |

Total, estimated: about 2 500 lines of source and 2 350 of tests. Each step stops and reports if it
needs more than 120 % of its size.

## Docs this changes
* README.md: a usage example and the 0.x statement.
* [API.md](../API.md): the class table rewritten from this spec's tables. Its "runs with the import
  changed" sentence is replaced by Q1's answer, and its `config.target = "auto"` example by Q3's.
* FEATURES.md, "Runtime features": the API column (done, deferred, deviation) per row. The
  "interruption" row's "`pyc` has yield points" is corrected: there are none yet (see "Contradictions
  found").
* CLAUDE.md, "Against over-engineering": "Do not touch the public API, the CLI or WASI before their
  phase" becomes "…the public API only under an approved phase-6 spec (S0018); the CLI and WASI
  before their phase".
* BRIEF.md: "the public API only in phase 6" points to this spec.
* TESTING.md: the API axis (the shim's counter, the surface diff, `tests/api/`).
* ARCHITECTURE.md, "The object model": `Config` notes ("decide" → this spec).
* S0002: the amendment (step 0) and its Changes line.
* DECISIONS.md: one entry for the API's shape (wasmtime-py's, the one extension, the 0.x statement,
  the answers to the questions below).
* ROADMAP.md phase 6: the items placed as in the table above; open question 4 is answered by Q5.

## Alternatives considered
* **A drop-in replacement for wasmtime-py** (`import waboros as wasmtime`). Cost: every name of the
  pinned version, including WASI, component-model trap codes and C-pointer helpers, follows
  wasmtime-py's releases. Rejected by ROADMAP ("not a drop-in"). Q1 keeps the middle way.
* **A new, Pythonic API** (no `store` argument, properties instead of setters). Rejected: D6 and
  BRIEF.md ask for wasmtime-py's shape, so that users recognise it.
* **The public API as the spec runner's interface.** Rejected for the metric: S0001's four
  operations keep the metric independent of API work (TESTING.md). An extra engine over the public
  API is Q7.
* **Renaming the internal packages to `_targets` etc.** Rejected: churn across `src/`, `tests/` and
  `tools/`, and a conflict with the concurrent work, for no behavioural gain.
* **Epochs compiled into the code** (Wasmtime's way). Rejected for now: a code change in both JITs
  for behaviour the host side gives without one. To revisit if latency measurements show the host
  side is too slow.
* **`memoryview` of linear memory.** Rejected: it breaks `pyc`'s grow (`BufferError`).

## Open questions
Ranked by how much later work depends on them.

1. **How close to wasmtime-py, and the error's name.** Docs disagree: API.md says code for
   wasmtime-py "runs on waboros with the import changed", and TESTING.md runs wasmtime-py's tests
   through `import waboros as wasmtime`; ROADMAP phase 6 says "not a drop-in". Options: (a) the
   shape only: our names, our tests; (b) **signature-compatible wherever a name exists**, with
   wasmtime-py's suite as a counter, not a gate, and the error class named `WasmtimeError` so that
   `except` clauses carry over; (c) a full drop-in. Recommendation: **(b)**, and for the error class,
   either `WasmtimeError` (pure shape) or `WaborosError` with `WasmtimeError` as an alias (API.md's
   idea; then the alias is an extension). Pick one.
2. **Values wasmtime-py cannot express**: `v128`, `anyref`/`eqref`/`i31ref`, structs and arrays,
   `exnref`, and the payload of an uncaught exception. The core supports all of them (FEATURES.md),
   but wasmtime-py 50 (and its `main`, 51.0.0) has no Python form, so any form is an extension.
   Options: (a) none in 0.x: such a parameter or result is an explicit error; (b) **the minimal set**:
   `v128` as an `int` in `[0, 2**128)` (`pyc`'s representation); i31 as an `int` (as the input rule
   of S0013 decision G already reads it); structs, arrays and exceptions as opaque handles named
   after Wasmtime's Rust API (`AnyRef`, `ExnRef`; equality is identity, no field access); `Trap.exn`
   for an uncaught exception; (c) (b) plus field access and host-thrown exceptions. Recommendation:
   **(b)**. Without it, the GC and exception proposals are unreachable from Python.
3. **Choosing the target.** wasmtime-py's `Config.target` is the cross-compilation triple, and
   `strategy` picks the compiler (`auto`, `cranelift`); API.md's `config.target = "auto"` gives the
   first name another meaning. Options: (a) the environment only (`WABOROS_TARGET`), no knob;
   (b) **new values of `strategy`** (`"auto"`, `"pyc"`, `"jit"`) and `target` kept for D35
   cross-compilation (serialize only); (c) API.md's `Config.target` with our names. Also: whether
   `Engine.target` (read-only, what was selected) and `waboros.targets()` (what runs here and why
   not), both listed in API.md, are wanted; each is an extension. Recommendation: **(b)**, plus
   `Engine.target`; `waboros.targets()` waits for the CLI's `targets` command.
4. **Explicit roots for GC handles.** Today: released when dropped; with this spec: scopes. Options:
   (a) nothing more in 0.x; (b) Wasmtime's `ManuallyRooted` model (`root()`/`unroot()`,
   generation-checked handles), which is ROADMAP's "one contract for every host". Recommendation:
   **(a)** in Python, because drop and scopes cover it; (b) when the C loader gets a public API.
5. **Options with no meaning here** (ROADMAP open question 4): `cranelift_*`, `profiler`,
   `debug_info`, `parallel_compilation`, `native_unwind_info`, `macos_use_mach_ports`, the
   `memory_*` reservation knobs, `memory_init_cow`, `strategy="cranelift"`; and `wasm_*=False` for
   a feature we support (Wasmtime then refuses modules that use it; our validator has no feature
   gates). Options: (a) accept and ignore all; (b) **ignore pure hints** (`cranelift_opt_level`,
   `parallel_compilation`, `profiler`, `debug_info`, `native_unwind_info`, `macos_use_mach_ports`,
   `memory_init_cow`) and give an explicit error for anything whose ignoring would change behaviour
   (`wasm_*=False` until the validator has gates, `cranelift_nan_canonicalization=True`, the
   `memory_*` knobs); (c) reject all. Recommendation: **(b)**. It follows API.md's "never silently
   differently".
6. **Writing fat files from the API.** D18 makes a fat file the portable form, but wasmtime-py's
   `serialize()` has no option for it. Options: (a) the CLI only (`waboros compile --fat`);
   (b) `serialize(fat=True)` (an extension). Recommendation: **(a)** in this part, and (b) only if a
   user asks.
7. **The spec suite and the public API.** Options: (a) the runner stays on S0001 only; (b) **also an
   engine `api-<target>`** (an adapter over the public API) run in CI on one row, while the metric
   stays on the internal engines; (c) move the runner onto the public API. Recommendation: **(b)**:
   about 200 lines, and the whole suite then tests the conversions, the linker and the errors.

## Contradictions found while writing
* FEATURES.md, "Runtime features", interruption: "done: … `pyc` has yield points". The code has
  neither yield points nor an `interrupt` in `targets/pyc/` (`StoreState.interrupt`'s docstring and
  S0002 say "pyc: later").
* D10 ("Stack depth is bounded per store with a deterministic `STACK_OVERFLOW` trap") and
  ARCHITECTURE.md's `pyc` depth counter: `pyc` has only the `RecursionError` safety net, and
  `max_wasm_stack` is read only by the JIT (`jit/common/instance.py`).
* API.md ("runs on waboros with the import changed") and TESTING.md's shim against ROADMAP phase 6
  ("not a drop-in"): Q1.
* API.md's `Config.target` against wasmtime-py's `Config.target` (a triple): Q3.
* `jit/common/instance.py` calls 512 KiB "Wasmtime's default `max_wasm_stack`", while
  wasmtime-py 50's `Config.max_wasm_stack` docstring says 2 MB. Check against Wasmtime 50 in step 5.
* S0015 "Compatibility" still says the per-architecture rule is "enabled in phase 6", while ROADMAP
  has moved it to the micro-compiler item.

## Changes
* 2026-11-25: created (draft), with the maintainer's direction of 2026-11-25: wasmtime-py's shape;
  context managers with deterministic release as the one extension (only `store.scope()` turned out
  to be new: wasmtime-py's `Managed` already gives `close`/`with`/`__del__`); other extensions only as
  open questions.
