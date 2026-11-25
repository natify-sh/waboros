---
title: The public Python API (phase 6, part 1: the runtime API)
kind: spec
id: S0018
status: draft
superseded_by:
related: [D1, D6, D10, D14, D17, D18, D19, D21, D23, D25, D27, D29, D31, D33, D34, D35, D44, D46, D49, D51, D57, D58, D64, D70, R1, R4, S0001, S0002, S0012, S0013, S0014, S0015, S0017]
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

Direction (maintainer, 2026-11-25): the API follows wasmtime-py's shape. Context managers, `close()`
and finalizers follow wasmtime-py's `Managed` base; the one context-manager extension is
`store.scope()`. The extensions over wasmtime-py are exactly those listed in "Decisions"; every
other difference is something waboros cannot do yet, raised as an explicit error that names the
reason ([API.md](../API.md)'s principle). The API promises nothing that the language-neutral core
cannot give ([D17](../DECISIONS.md)). [S0017](S0017-c-reference-loader.md) Decision 1 gives the C
loader no public API yet, so this spec is Python-only, but every mechanism it needs from the core is
one a C host can implement the same way.

## Decisions
Taken by the maintainer on 2026-11-25 on the draft's nine open questions. The rejected options are
in "Alternatives considered".

1. **Closeness to wasmtime-py** (former Q1, option (b)): the same signatures and behaviour wherever
   waboros has a name; wasmtime-py's own test suite runs through a shim as a **counter, not a
   gate**. The base error class is **`WaborosError`**, with **no `WasmtimeError` alias**, a
   documented deviation (the test shim maps the name on the test side only).
2. **Values wasmtime-py cannot express** (former Q2, (b) with a change): `v128` is returned as
   `bytes` (16 bytes, little-endian, the order in wasm memory) and accepted as a bytes-like object
   of exactly 16 bytes (`bytes`, `bytearray`, `memoryview`) or an `int`; an i31 is an `int` both
   ways; structs, arrays and other non-i31 values of the any hierarchy are opaque `AnyRef` handles,
   and exceptions are opaque `ExnRef` handles; an uncaught wasm exception carries `Trap.exn`. Field
   access and host-thrown wasm exceptions come later, in their own spec.
3. **Choosing the target** (former Q3, (b)): `Config.strategy` takes `"auto"`, `"pyc"` and `"jit"`
   besides wasmtime-py's values; `Config.target` keeps wasmtime-py's meaning (the triple to compile
   for, D35 cross-compilation); what was selected is read from `Engine.strategy` and `Engine.target`
   (Decision 10).
4. **Explicit roots** (former Q4, (b)): Wasmtime's root model now. A handle can be made a
   **`ManuallyRooted`** root by `root()`, released only by `unroot()` (or `Store.close()`), besides
   release-on-drop and `store.scope()`. Every handle is **generation-checked**. This amends S0017
   Decision 11: the C loader gets the same model later.
5. **Options with no meaning here** (former Q5, (b); also answers ROADMAP open question 4): pure
   hints are accepted and silently ignored; an option whose ignoring would change behaviour (for
   example `wasm_simd=False`) raises an explicit error. The bounds mode is wasmtime-py's own
   `signals_based_traps`: `True` gives guard pages where they are supported, `False` explicit
   checks. ROADMAP's question listed `signals_based_traps` as meaningless; this decision gives it a
   meaning.
6. **Serialized files** (former Q6, (b) and (b)): `serialize()` follows S0014 Q12, so a module
   compiled in guard mode is recompiled to explicit checks for the file; `serialize(fat=True)`
   writes a fat file (D18), an extension over wasmtime-py.
7. **The spec suite** (former Q7, (b)): an engine `api-<target>` over the public API runs the suite
   in CI on one row; the metric stays on the internal interface of S0001.
8. **Names of the extensions** (former Q8, (a)): unprefixed, named after Wasmtime's Rust API.
   API.md's "prefixed or in a submodule" rule is amended.
9. **Integer arguments out of range** (former Q9, (b); refined by the maintainer the same day): an
   integer argument may be given in the signed **or** the unsigned range (i32: −2³¹ … 2³²−1, i64:
   −2⁶³ … 2⁶⁴−1) and is converted to the wasm bit pattern (`0xFFFFFFFF` as an i32 is −1); outside
   both ranges (for example `2**32 + 5` for an i32, or `2**100`) it raises `ValueError`. Results come
   back signed, as in wasmtime-py. A documented deviation from wasmtime-py, which wraps any `int`
   silently, listed among the differential run's known differences.

10. **The engine's read-only view of the choice** (the open question Decisions 3 creates, (b) with
    a triple, maintainer 2026-11-25): as Wasmtime separates the compiler (`strategy`) from the
    platform (`target`), `Engine.strategy` is the resolved compiler family (`"jit"` or `"pyc"`) and
    `Engine.target` is the triple compiled for (for example `"x86_64-unknown-unknown"`; the OS part
    is `unknown` because artifacts are OS-independent, D23; `pyc` has none and gives `None`). Each
    name means the same on `Config` and on `Engine`. The internal target names (`jit-x86_64`, the
    compiler string of S0014) are not part of the public API and do not change.

## Goals / Non-goals
* Goals:
  * a user of wasmtime-py recognises every name and signature: where waboros has a name, its
    signature and behaviour are wasmtime-py's, and the differences are listed in the tables below
    (Decision 1);
  * every public operation works on every target (`pyc`, `jit-x86_64`, `jit-aarch64`) through
    `Target` methods only (CLAUDE.md, "one interface for all targets");
  * deterministic release: `Store.close()` and `with` free a store's memories, GC heap, code and
    contexts at once (LESSONS 135; ROADMAP "Memory of dropped stores" (c));
  * one error model: traps, host exceptions and engine errors look the same on every target;
  * the GC and exception proposals reachable from Python (Decision 2);
  * a statement that 0.x may change (see "Compatibility promise").
* Non-goals:
  * WASI (`WasiConfig`, `Linker.define_wasi`, `Store.set_wasi`, `set_wasi_http`, `ExitTrap`) and the
    CLI: their own specs (ROADMAP phase 6; open question 2 there chooses the WASI host);
  * a drop-in replacement for wasmtime-py (ROADMAP phase 6: "not a drop-in"; Decision 1);
  * GC field access and host-thrown wasm exceptions: their own spec (Decision 2);
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
  `_table.py`, `_globals.py`, `_tag.py`, `_types.py`, `_value.py`, `_trap.py`, `_error.py`, and
  `_gcref.py` for `AnyRef`, `ExnRef` and `ManuallyRooted`).
* `waboros.frontend`, `waboros.targets` and `waboros.runtime` stay where they are and are internal.
  This is stated in README.md and API.md and in each package's docstring, and is not shown by a
  rename: a rename would touch every file in `src/`, `tests/` and `tools/` for no behavioural gain.
  An import from them carries no promise.
* Stdlib only ([D1](../DECISIONS.md)). WAT input works only when the optional extra
  `wasm-tools-package` is installed ([D21](../DECISIONS.md)); without it, a `str` argument gets an
  error that names the extra.
* Extension names are unprefixed and taken from Wasmtime's Rust API (Decision 8): `Store.scope`
  (Rust `RootScope`), `AnyRef`, `ExnRef`, `ManuallyRooted`, `root`, `unroot`, `to_rooted`. A later
  wasmtime-py name for the same thing then most likely coincides.

### The object model
Columns: the public surface, the wasmtime-py 50.0.1 counterpart (checked by introspection of the
pinned development install, `pyproject.toml`), and the deviation. "—" means none. "Explicit error"
means the call raises `WaborosError` with the reason and never does something different in silence.
"Extension" marks a name wasmtime-py does not have.

| class | public surface | wasmtime-py | deviation |
|---|---|---|---|
| `Config` | write-only setters: `wasm_*` (the features of FEATURES.md), `max_wasm_stack`, `epoch_interruption`, `consume_fuel`, `cache` (`bool` or a path `str`), `signals_based_traps`, `strategy`, `target`, and the options of "Options with no meaning here"; `close()`, `with` | the same names (wasmtime-py's `Config` derives from its `Managed` base: `close`, `with`, `__del__`); `Engine(config)` consumes it, so a second `Engine(config)` raises `ValueError("already closed")` | the same consumption; `strategy` takes `"auto"`, `"pyc"`, `"jit"` (Decision 3); `signals_based_traps` selects the bounds mode (Decision 5); `consume_fuel=True` and `cache=True` or a path give an explicit error until their steps (`cache=False` is accepted: the cache is off) |
| `Engine` | `Engine(config=None)`, `increment_epoch()`, `is_pulley()` (a method; always `False`), `strategy`, `target` (read-only), `close()`, `with` | the same, without `strategy`/`target` | **extension**: `Engine.strategy`, `Engine.target` (Decision 10); one target per engine ([D25](../DECISIONS.md)) |
| `Module` | `Module(engine, wasm)` (bytes; `str` is WAT, D21), `from_file`, `validate`, `imports`, `exports`, `serialize(fat=False)`, `deserialize`, `deserialize_file`, `close()`, `with` | the same, `serialize()` without `fat` | **extension**: `fat=True` (Decision 6); guard-mode code is recompiled to explicit for the file (Decision 6); `serialize` on a `pyc` engine: explicit error ([S0014](S0014-artifact-format.md) Q6); WAT needs the extra (D21) |
| `Store` | `Store(engine=None, data=None)`, `data`, `gc()`, `set_epoch_deadline`, `set_limits`, `get_fuel`, `set_fuel`, `scope()`, `close()`, `with` | the same, plus `set_wasi`, `set_wasi_http`, without `scope` | **extension**: `with store.scope():` ("GC handles, roots and scopes"); `get_fuel`/`set_fuel`: explicit error until fuel exists; WASI out of scope |
| `Linker` | `Linker(engine)`, `allow_shadowing`, `define`, `define_func`, `define_instance`, `define_module`, `get`, `get_default`, `instantiate`, `instantiate_pre`, `define_unknown_imports_as_traps`, `define_unknown_imports_as_default_values`, `close()`, `with` | the same, plus `define_wasi` | WASI out of scope |
| `InstancePre` | `instantiate(store)`, `module`, `close()`, `with` | the same | — |
| `Instance` | `Instance(store, module, imports)`, `exports(store)` (by name and by index) | the same | — |
| `Func` | `Func(store, ty, func, access_caller=False)`, `type(store)`, `__call__(store, *params)` | the same | the values of Decision 2; integers out of range raise (Decision 9) |
| `Caller` | `get(name)`, `[name]`; usable as the store argument while the host call runs | the same | — |
| `Memory` | `Memory(store, ty)`, `type`, `size`, `grow`, `read`, `write`, `data_ptr`, `data_len`, `get_buffer_ptr`, `page_size`, `page_size_log2` | the same | `data_ptr` on `pyc` points into a `bytearray` that a grow may move ("Memory access") |
| `Table` | `Table(store, ty, init)`, `type`, `size`, `grow`, `get`, `set` | the same | `pyc` and the JITs cap `grow` at 10 000 000 elements unless `set_limits` says otherwise ([D27](../DECISIONS.md)) |
| `Global` | `Global(store, ty, val)`, `type`, `value`, `set_value` | the same | — |
| `Tag` | `Tag(store, ty)`, `type`, `eq` | the same | — |
| GC values | `AnyRef`, `ExnRef` (`root(store)`, `kind` on `AnyRef`), `ManuallyRooted` (`unroot(store)`, `to_rooted(store)`) | none | **extension** (Decisions 2, 4) |
| types | `ValType`, `FuncType`, `MemoryType`, `TableType`, `GlobalType`, `TagType`, `Limits`, `ImportType`, `ExportType`, `Val` | the same | `ValType` and `Val` also cover `v128`, the any and exn hierarchies and concrete GC types (**extension**, Decision 2) |
| errors | `Trap` (`message`, `trap_code`, `frames`, `exn`), `TrapCode`, `WaborosError`, `Frame` (`Managed` in wasmtime-py: `close`, `with`) | `Trap` without `exn`, `TrapCode`, `WasmtimeError`, `Frame`, `ExitTrap` | `WaborosError` instead of `WasmtimeError`, no alias (Decision 1); **extension**: `Trap.exn` (Decision 2); `frames` is empty in this part: FEATURES.md ("traps with frames") plans them from Python tracebacks on `pyc` and from a code-address map on the JIT, a later step; `ExitTrap` comes with WASI |
| warning | `JitUnavailableWarning` | none | re-exported as D31 decides; it already exists (`targets/base.py`) |
| function | `wat2wasm` | the same | needs the extra (D21) |

`Storelike`: as in wasmtime-py, a `store` argument takes a `Store` or a `Caller` (a `Caller` only
during its host call). `StoreContext` is not provided: wasmtime-py uses it for its own `Store`
plumbing, and it has no counterpart here.

### Values: Python ⇄ wasm
The internal boundary carries unsigned integers and float bit patterns (`Raw`, S0002). The public
layer converts:

| wasm type | to wasm (a parameter, a host result, `Global`/`Table` values) | to Python | note |
|---|---|---|---|
| `i32`, `i64` | `int` in the signed or the unsigned range, `[-2**(n-1), 2**n)`, converted to the bit pattern (`0xFFFFFFFF` as an i32 is −1), or `Val`; outside both ranges: `ValueError` | signed `int`, as wasmtime-py | Decision 9. wasmtime-py 50 wraps any `int` silently (measured: an i32 identity function returns 5 for `2**32 + 5`). That is an artifact of its implementation, not a documented rule: `Val.i32` stores the value through a ctypes union field (`ffi.wasmtime_valunion_t(i32=val).i32`, `wasmtime/_value.py`), which truncates. In both ranges the two agree (`2**31` gives −2147483648 on both) |
| `f32`, `f64` | `float` or `Val` | `float` | an f32 NaN payload survives where the host's floats keep it ([D28](../DECISIONS.md), [D46](../DECISIONS.md)) |
| `v128` | a bytes-like object of exactly 16 bytes (`bytes`, `bytearray`, `memoryview`), or an `int` in `[0, 2**128)`; else `ValueError` (wrong length or range) or `TypeError` | `bytes` of length 16, little-endian (byte 0 is the lane byte at the lowest address in wasm memory) | Decision 2; wasmtime-py raises "unimplemented v128 type" (`wasmtime/_value.py`); internally `pyc` holds an `int` and the JITs two slots (S0005), and the conversion is `int.to_bytes(16, "little")` and back |
| `funcref` (and typed function references) | `Func` or `None` | `Func` or `None` | a `Func` of another store: `TypeError` |
| `externref` | any Python object, `None` is null; an `AnyRef` is passed as the any value made external (`extern.convert_any`) | the object; a GC value made external comes out as its `AnyRef`, an i31 included, so that passing it back keeps it a GC value | — |
| `i31ref` and an i31 in `anyref`/`eqref` | `int` in `[-2**30, 2**30)`, else `ValueError` | `int` | Decision 2; as S0013 step 3b decision G: a Python `int` never crosses into an `anyref` as a host value |
| `anyref`, `eqref`, `structref`, `arrayref`, concrete struct and array types | `AnyRef` of the same store, `int` (an i31), a host object (a host value in `anyref`, as S0013 decision E) or `None` | `AnyRef` (struct, array), `int` (i31), the host object, or `None` | Decision 2; `AnyRef.kind` is `"struct"` or `"array"`; no field access |
| `exnref` | `ExnRef` of the same store, or `None` | `ExnRef` or `None` | Decision 2 |

Results: `None` for no result, the value for one result, a `list` for more, as in wasmtime-py
`Func.__call__`. Host values given for a reference parameter are type-checked before anything
runs (`TypeError`, D57). `AnyRef` and `ExnRef` are handles ("GC handles, roots and scopes"). Two
handles that are not `ManuallyRooted` and name the same object are the same Python object (the
store's one-handle-per-object map), so `is` and `==` compare objects; `ManuallyRooted.to_rooted`
returns that shared handle. On `pyc` the handle wraps the Python object `pyc` uses (a struct is a
list, FEATURES.md), with the same public behaviour.

### Errors and exceptions
`WaborosError(Exception)` is the base class of engine errors (Decision 1). `Trap(Exception)` is not
a subclass of it, as `Trap` is not a `WasmtimeError` in wasmtime-py.

| what happens | what the user sees | today, internally |
|---|---|---|
| a wasm trap | `Trap` with `trap_code` a `TrapCode` member (wasmtime-py's names; the numeric values are not promised) | `targets.base.Trap(code)` |
| a wasm exception escapes the outermost call | `Trap` with `trap_code == TrapCode.UNCAUGHT_EXCEPTION`, as in wasmtime-py, and `exn` the `ExnRef` of the exception (Decision 2); its tag and payload are read when field access comes | `Trap(UNCAUGHT_EXCEPTION)`, `.exn` = (tag, values) ([S0012](S0012-exceptions.md) Q4) |
| a host function raises | the same exception object comes out of **every** `Func.__call__` it crosses, nested ones included, as in wasmtime-py (its trampoline stores it in `LAST_EXCEPTION`, and `enter_wasm`/`maybe_raise_last_exn` re-raise it at each call): a host function A that calls wasm, which calls a raising host function B, sees B's exception and can catch it. Wasm code cannot catch it | `HostError` carrier, unwrapped only at depth 1 (`targets/pyc/__init__.py` `_run`; `jit/common/instance.py` `_error` and the exported-import path); a nested caller sees `HostError` today (measured on `pyc` and `jit-x86_64`). The public `Func.__call__` unwraps `HostError` at every depth; no core change |
| an asynchronous exception kills a host callback before it reports its status (JIT) | `RuntimeError("host call interrupted")` | [D34](../DECISIONS.md); on PyPy: [D51](../DECISIONS.md) |
| invalid or malformed module, link failure, instantiation failure, a refused serialized file, an option or feature waboros lacks, an option whose ignoring would change behaviour | `WaborosError` carrying the internal exception as `__cause__` | `Malformed`, `Invalid`, `LinkError`, the `MemoryError` of a memory or table min that cannot be provided at instantiation (S0009 Q6: "phase 6 maps it to the API's error"), `ArtifactError` (`.step`, `.section`, S0017 P2), `Unsupported` |
| `strategy="jit"` on a machine where the JIT cannot run | `WaborosError` | `Unsupported` (D31, TARGETS.md) |
| `auto` falls back to `pyc` | one `JitUnavailableWarning` per process and message | D31 |
| a store used from a second thread | `RuntimeError("a store is used from one thread only")` | `StoreState.claim` |
| any use after `close()`; a handle released by its scope, by `unroot()` or by the store's close | `ValueError("already closed")`, wasmtime-py's message, and `ValueError` for a stale handle (its generation no longer matches) | — (new) |
| wrong argument count; wrong argument type; integer out of range | `WaborosError` (count, as wasmtime-py); `TypeError`; `ValueError` (Decision 9) | — |

Which `MemoryError` goes where: a failure **during instantiation** (a min that cannot be provided,
S0009 Q6) becomes `WaborosError` with the `MemoryError` as its cause, as every instantiation
failure does. A failure **during a call or a host operation** (a reservation or commit while the
guest runs, `Memory.grow` from the host) stays `MemoryError`, as in Python generally. The GC heap's
failed commit still escapes as `OSError` (ROADMAP, "Memory of dropped stores", open item); step 1
makes it `MemoryError`.

### Configuration: public knobs and environment variables
| knob | public (`Config`) | meaning | environment variable today |
|---|---|---|---|
| target | `strategy`: `"auto"` (default), `"pyc"`, `"jit"` (the JIT of this architecture); wasmtime-py's `"cranelift"` raises (Decision 5) | one target per engine (D25); `Engine.strategy` (`"jit"`/`"pyc"`) and `Engine.target` (the triple, `None` for `pyc`) give the result (Decision 10) | `WABOROS_TARGET` (`targets/base.py`, `target_order`) |
| cross-compilation | `target`: a triple whose architecture is `x86_64` or `aarch64` (Decision 3) | the JIT of that architecture compiles; an engine for another architecture than the host's compiles and serializes (D35; files are OS-independent, D23), and instantiating its modules raises `WaborosError`; another triple raises | — |
| bounds mode | `signals_based_traps`: unset or `True` is `auto` (guard pages where verified, explicit checks elsewhere; D44); `False` is explicit checks (Decision 5) | a property of the compiled code (D29); a compile option per engine, no longer read from the process environment at compile time | `WABOROS_JIT_BOUNDS=auto|guard|explicit` (`jit/common/guard.py`, `mode`) |
| stack | `max_wasm_stack` (bytes) | the JIT's native budget per store ([D10](../DECISIONS.md)); `pyc`: "Stack" below | — (`StoreState.max_wasm_stack`, default 512 KiB in `jit/common/instance.py`, Wasmtime 50's default: see "Stack") |
| epochs | `epoch_interruption` | "Interruption" | — |
| fuel | `consume_fuel` | explicit error until fuel exists | — |
| features | `wasm_*` | `True` for every feature FEATURES.md claims on all targets; `False` raises until the validator has feature gates (Decision 5) | — |
| cache | `cache` | `False`; `True` or a path raises until the cache spec | — |

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

### Options with no meaning here (Decision 5)
The `Config` members of wasmtime-py 50 with no waboros meaning:
* **accepted and silently ignored** (pure hints; ignoring them changes no result):
  `cranelift_opt_level`, `cranelift_debug_verifier`, `parallel_compilation`, `profiler`,
  `debug_info`, `native_unwind_info`, `macos_use_mach_ports`, `memory_init_cow`; and the values that
  match what waboros does anyway: `gc_support=True`, `wasm_relaxed_simd_deterministic=True` (the
  deterministic profile, [D49](../DECISIONS.md)), `wasm_component_model*=False`,
  `wasm_stack_switching=False`;
* **an explicit error**, because ignoring them would change behaviour: any `wasm_*=False` for a
  feature waboros supports (for example `wasm_simd=False`; Wasmtime then refuses modules that use
  it, and the validator has no feature gates yet), `wasm_relaxed_simd_deterministic=False`,
  `cranelift_nan_canonicalization=True` (S0005 Q5), `cranelift_flag_enable`, `cranelift_flag_set`,
  `memory_reservation`, `memory_guard_size`, `memory_reservation_for_growth`, `memory_may_move`,
  `gc_support=False`, `wasm_component_model*=True`, `wasm_stack_switching=True`,
  `strategy="cranelift"`.

## Semantics

### Lifetimes, `close()` and finalizers
* **What wasmtime-py already has.** Its `Managed` base gives `close()`, `__enter__`/`__exit__` and a
  `__del__` that closes to `Config`, `Engine`, `Store`, `Module`, `Linker`, `InstancePre`, the type
  objects, `Trap`, `Frame` and `WasmtimeError`. After `close()`, every use raises `ValueError("already
  closed")`, and a second `close()` does nothing (`wasmtime/_managed.py`). `Func`, `Memory`, `Table`,
  `Global`, `Tag`, `Instance` and `Caller` are not `Managed`: a use after the store is closed fails
  when the store is passed to the call. waboros follows this, so `with Store(engine) as store:` is
  wasmtime-py's shape, not an extension.
* **`Store.close()`** (ROADMAP phase 6, LESSONS 135) releases at once everything the store owns:
  each instance's code areas (after `guard.add_code`'s registration is removed with
  `_forget_code`, before the unmapping) and contexts, linear memories, tables, and the per-store
  state of `_StoreStacks` (`jit/common/instance.py`: the GC heap and its collector instance, the
  value stack, the handle and extern tables, the walk's ranges). The trampolines are not the
  store's: in-process they are sealed once per process and target (`trampolines()`, `@cache`, in
  `jit/x86_64/__init__.py`; `load_trampolines`) and stay. Every later use of the store, of its
  instances and externs, and of its `AnyRef`, `ExnRef` and `ManuallyRooted` handles raises
  `ValueError`. The mechanism is a `close()` on `StoreState` that calls a `release()` of each
  `TargetInstance` and of the target's per-store state (the S0002 amendment, step 1). A C host does
  the same with a store-destroy call.
* **`close()` and threads.** The owner thread is recorded for good at the first entry
  (`StoreState.entered`, `store.py`). `close()` from any thread is allowed while no call is active
  on the store, so a store used on a worker can be closed by the thread that made it, and a
  finalizer may run on any thread. While a call is active, `close()` from a host function of that
  store raises `WaborosError` ("store is in use") and releases nothing, and `close()` from another
  thread raises the `RuntimeError` of the one-thread rule. The test "no call active" and the mark
  "closed" are one atomic step under the lock that `claim()` takes (`_CLAIM`), and `claim()`
  refuses a closed store under the same lock, so a close and a first entry on two threads cannot
  both win.
* **Finalizers as the safety net.** A store that is never closed is released when its public
  `Store` object becomes unreachable (`weakref.finalize`, not `__del__`, so that the finalizer never
  sees a half-torn object). The public `Func`, `Memory`, `Table`, `Global`, `Tag` and `Instance`
  hold their target handle, not the `Store`, which is passed to every call, as in wasmtime-py (whose
  objects hold raw C handles). The runtime's own objects do not reference the public `Store`. So in
  the plain case the public `Store` is outside the internal reference cycles of LESSONS 135, and on
  CPython (and on a free-threaded build, which also frees acyclic objects by reference count) the
  last reference going away releases the store at once. **This does not hold** when user objects
  reach back to the `Store`: a host function whose closure captures it, `Store(data=obj)` where
  `obj` refers to the store, an externref holding such an object (kept in the store's extern table),
  or a `Linker` kept after `define(store, …)` if its entries hold the store. Then release waits for
  the cyclic GC, as today. On PyPy it always waits for the collector. `close()` and `with` are the
  deterministic path in every case.
* **`Engine.close()` and `Module.close()`** drop the compiled code the object holds. Instances
  already made keep their own mapped copy, because nothing is shared between stores
  ([D64](../DECISIONS.md)).

### GC handles, roots and scopes
The Wasmtime model (Decision 4), with Python's release-on-drop kept as the default.
* **Today** (S0017 Decision 11, implemented in the Python runtime): a GC object or exception given
  to the host is a handle (`GcRef`, `ExnRef` in `jit/common/instance.py`) that is released when the
  host drops it. A weak entry's finalizer queues its slot, and the store drains the queue before a
  collection and before it makes a new handle. Freed slots are reused lowest first (`free_handles`
  is a heap), and the store keeps one handle per object (`gcrefs`: word → handle). `pyc` hands out
  its Python objects, which Python's own GC owns.
* **Generation-checked handles.** Every slot of the handle table carries a generation, which a
  release increments. A public handle records (slot, generation), and every use compares them; a
  mismatch raises `ValueError`. This is the one mechanism by which a released handle is told from a
  reused slot, for every way of releasing (drop, scope, `unroot`, close), in Python and, later, in C.
* **Release on drop** stays the default for a handle made outside any scope, as today.
* **`with store.scope():`** (the context-manager extension; Rust `RootScope`). Every handle the
  store **creates** while the scope is open is released when the scope closes, whether or not the
  host still holds it; a use after that raises `ValueError`, also when the handle was returned out
  of the scope (Wasmtime's scoped roots behave the same). Scopes nest. The scope records the
  handles it creates. A handle the one-handle-per-object map returns because it existed before the
  scope is not recorded and not released. A later request for the same object makes a new handle.
  This gives a deterministic release where Python's collector would not (PyPy, cycles).
* **Explicit roots.** `ref.root(store)` returns a `ManuallyRooted`: a root in its own slot (outside
  the one-handle-per-object map), released only by `unroot(store)` or `Store.close()`, never by a
  drop or a scope. `ManuallyRooted.to_rooted(store)` returns the ordinary handle of the same object.
  Names (Decision 8): Rust's `Rooted::to_manually_rooted` and `ManuallyRooted::unroot` give the type
  and `unroot`; `root()` is the maintainer's shorter name for the conversion. A `ManuallyRooted`
  that is never unrooted lives until the store closes, as in Wasmtime (a finalizer does not release
  it: that is its purpose). Wasmtime's newest releases rename the type; step 4 checks the name
  against Wasmtime 50's documentation and records it in its report.
* **`pyc`**: the handles wrap Python objects. Releasing one invalidates the public handle by its
  generation, and Python owns the object.
* **The C loader** (S0017 Decision 11: `waboros_ref_release` only) gets the same model later:
  generations, scopes (a mark/release pair over a record of created handles) and manual roots.
  This amends Decision 11 ("Docs this changes").

### Threads
* **One thread per store at a time** (ARCHITECTURE.md, "Long calls and the GIL"; D14). Today the
  first entry claims the store atomically, re-entry from a host function on the same thread is
  allowed, and another thread gets `RuntimeError` (`StoreState.claim`). A store built on one thread
  can run on another until its first entry (`StoreState.release`). The API keeps this as it is.
* `Engine` and `Module` may be shared between threads; instantiating one module in several stores
  on several threads is the D58 model. Step 7 adds a stress test before this is claimed. `Linker`
  definitions are not thread-safe, as in Wasmtime.
* `Engine.increment_epoch()` may be called from any thread.
* JIT calls release the GIL (`CFUNCTYPE`, D14). `pyc` calls hold it; from step 5, modules compiled
  with `epoch_interruption` have yield points, and other `pyc` modules do not.
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
  a weak set of its stores. A store's deadline starts at 0, so with `epoch_interruption=True` and
  no `set_epoch_deadline` the first call traps at once, as Wasmtime 50 does (measured).
  `Store.set_epoch_deadline(n)` records `epoch + n` and clears the store's interrupt request.
  `increment_epoch()` sets the interrupt word of each store whose deadline has been reached. A call
  that starts after its store's deadline sets the word before it enters. Clearing in
  `set_epoch_deadline` and setting in `increment_epoch` both run under the store's
  `interrupt_lock`, so that a tick from another thread is never lost to a clear. The trap is
  `TrapCode.INTERRUPT`, as in Wasmtime. Wasmtime compiles a compare against a deadline into the
  code. The context words `FUEL` and `EPOCH_DEADLINE` (one each per context, ARTIFACT.md part 3)
  exist in ABI v1 but are unused. The host-side check gives the same observable behaviour at no cost
  per back-edge. Without `epoch_interruption=True` the deadline is ignored, as in Wasmtime.
* **`pyc`** needs yield points to observe the word (ARCHITECTURE.md, option 1: a counter on
  back-edges that checks the word and gives the GIL away every N iterations). Step 5 adds them only
  in modules compiled with `epoch_interruption` and measures them on CoreMark (BENCHMARKS.md). This
  answers ROADMAP open question 3 only for interruption: a `pyc` guest compiled without epochs
  keeps the GIL-latency problem of ARCHITECTURE.md, and parallelism (subinterpreters, a process)
  stays open there. Until step 5, `epoch_interruption=True` on a `pyc` engine is an explicit error.
* **Fuel** (`consume_fuel`, `set_fuel`, `get_fuel`) needs counters compiled into the code
  (ARCHITECTURE.md, "Interruption"). It is deferred. Until then: an explicit error.
* Ctrl+C on a worker thread is the CLI's (its own spec).

### Stack
* JIT: `max_wasm_stack` sets the native budget per store (today `StoreState.max_wasm_stack`;
  `jit/common/instance.py` uses 512 KiB when it is 0). That is Wasmtime 50's default, measured: a
  self-recursive function reaches the same depth (32 762) with no setting and with 524 288 bytes,
  and twice that with 1 MiB. wasmtime-py 50's docstring ("defaults to 2MB") is stale. Exhaustion is
  `Trap(STACK_OVERFLOW)`.
* `pyc`: a deep recursion hits Python's limit and becomes `Trap(STACK_OVERFLOW)` (`_run`).
  `max_wasm_stack` is not applied there: D10 and ARCHITECTURE.md ("Calls, stack depth and
  recursion", item 1) decide on a per-store depth counter, but none is implemented. Step 5 adds it,
  measured on CoreMark. Until then the deviation is listed in API.md.

### Limits
`Store.set_limits(memory_size, table_elements, instances, tables, memories)` as in wasmtime-py. The
checks sit in the runtime services that every target already calls (`memory.grow`, `table.grow`,
instantiation), so no generated code changes. A grow over a limit returns −1, and an instantiation
over a limit raises `WaborosError`. The `table_elements` default stays at D27's 10 000 000, which is
a deviation from Wasmtime's unlimited default recorded in D27. `set_limits(table_elements=...)`
replaces it, as D27 says.

### Memory access
`read`, `write`, `size`, `grow`, `data_len` and `page_size` work the same on every target. They go
through the S0002 amendment's memory handle methods, never through a target's own class.
`data_ptr`/`get_buffer_ptr`: on a JIT this is the mapping. An i32 memory never moves; an i64 memory
may move on a grow ([D50](../DECISIONS.md)). On `pyc` it is the address of the current `bytearray`,
valid until the next grow, as Wasmtime documents for a memory that may move. No `memoryview` is
handed out: an exported view of a `bytearray` would make a later `memory.grow` fail with
`BufferError`.

### Serialization
* `Module.serialize()` writes a **thin** file (S0014 v1) for the module's target. Following S0014
  Q12 (Decision 6), the file's code uses **explicit bounds checks**: a module compiled in guard
  mode (`auto` on a verified row, D44) is recompiled with explicit checks for the file, and its own
  in-process code is not changed. A module compiled with explicit checks is written as it is. The
  module therefore keeps its wasm bytes (they are needed for a recompile and for a fat file).
* `serialize(fat=True)` (extension, Decision 6) writes a fat file: the same code plus the wasm
  (D18), which regenerates on any mismatch instead of being refused.
* `Module.deserialize` and `deserialize_file` read either, with the loader's rules (S0014,
  "Semantics: the loader"; exact `compiler` string, S0015 "Compatibility"). A refused thin file
  raises `WaborosError` with the `ArtifactError` as its cause. A file's bounds mode is its own: an
  explicit-checks file runs on every row.
* An engine whose `Config.target` is another architecture (Decision 3) serializes files for it
  (D35).
* `pyc` modules: S0014 Q6 has not specified their file yet, so `serialize` on a `pyc` engine is an
  explicit error.
* Trust: files from elsewhere are R1's subject. `deserialize` is as unsafe as wasmtime-py's (its
  documentation says the same), and this is stated in the docstring.

### Target selection and the warning
The engine fixes its target when it is created (D25): `Config.strategy` (`"auto"` by default), or
`WABOROS_TARGET` when the strategy is left unset. `Engine.strategy` and `Engine.target` are the result (Decision 10). With `"auto"`, the
`JitUnavailableWarning` of D31 is raised at `Engine()`, so a user can filter it or turn it into an
error with the `warnings` module. With `"jit"`, a JIT that cannot run raises `WaborosError` (D31: no
warning when the target was chosen explicitly).

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
  changes no target. Step 1 implements neither until it has reported both with their costs, and the
  orchestrator chooses (CLAUDE.md, "when unsure between two designs");
* resolution per extern: `link.resolve` takes a whole provider per module name (an instance, or a
  mapping of **host functions only**) and rejects any other host import (`link.py`, the `else`
  branch). A `Linker` defines single items of any kind under a module name, mixing instances' exports
  and host objects, so resolution and the type checks move to the extern level, the checks
  unchanged;
* the calling instance for `Caller`: a `HostFunc` receives only the slots (`base.py`). The wrapper
  that the runtime makes per import at instantiation can bind the importing instance. Whether that
  is Wasmtime's caller when the function is reached through a reference from another instance is
  checked in step 3's tests against wasmtime-py;
* compile options per engine (the target, the architecture for cross-compilation, the bounds mode,
  epoch yield points on `pyc`), passed to `compile` instead of read from the environment;
* `release()` per instance and per store state for `Store.close()` (the list under "Lifetimes"),
  `guard`'s code registration removed before unmapping;
* **generation-checked handles**: a generation per handle slot, incremented on release, checked on
  every use; a scope's record of the handles it creates; manual roots in slots of their own,
  outside the one-handle-per-object map ("GC handles, roots and scopes");
* the v128 and GC conversions at the boundary for every target, one public representation
  (Decision 2);
* the limit hooks in the grow and instantiation services;
* the GC heap's failed commit raised as `MemoryError` instead of `OSError`.
Each of these is language-neutral: a C host needs the same operations.

## Placement of the ROADMAP phase-6 items
| item | here | reason |
|---|---|---|
| `Store.close()` and a context manager | in scope, step 4 | wasmtime-py's shape; LESSONS 135 |
| GC handle scopes | in scope, step 4 (`store.scope()`) | the maintainer's extension; S0017 Decision 11 leaves scopes to phase 6 |
| explicit roots, generation checks | in scope, steps 1 and 4 | Decision 4 |
| `Module.serialize`/`deserialize` (thin and fat, JIT) | in scope, step 6 | wasmtime-py names; S0014 implemented; Decision 6 |
| `pyc` files | deferred | S0014 Q6 not specified |
| the cache and its location (`.gitignore` rule) | deferred to the cache spec | the location chain is a proposal (ROADMAP); the `.gitignore` rule is already the maintainer's decision (2026-11-25) and that spec applies it; `Config.cache=True` raises |
| S0015's deferred items (per-architecture rule, foreign blobs, flags agreeing within a store) | out | the API has one target per engine (D25) and exposes no mixed stores; ROADMAP moved the rule to the micro-compiler item |
| interrupts and epochs | in scope, step 5 | host-side over the existing word |
| fuel | deferred | needs counters in the code and a measurement |
| `max_wasm_stack` | in scope (JIT now, `pyc` counter in step 5) | D10 |
| target and bounds selection | in scope: `strategy`, `target`, `signals_based_traps` | D25, D44; Decisions 3 and 5 |
| `waboros.targets()` (API.md) | deferred to the CLI spec, with its `targets` command | Decisions 3 and 10 add `Engine.strategy` and `Engine.target` only |
| `JitUnavailableWarning` | in scope, re-exported | D31 |
| GC and exception references | in scope (opaque handles, i31, `Trap.exn`); field access deferred | Decision 2 |
| traps against host exceptions | in scope | D34, "Errors and exceptions" |
| one thread per store | in scope, unchanged | ARCHITECTURE.md |
| free-threaded CPython | the rules hold; the claim waits for the threads spec's audit | ROADMAP open question 8 |
| PyPy | supported, D51 | D19 |
| JIT code visible to debuggers | out | its own item in ROADMAP phase 6 |
| `Trap.frames` | empty list in this part | FEATURES.md plans `pyc` frames from tracebacks and JIT frames from a code-address map: a later step |
| a host throwing a wasm exception | deferred, its own spec | Decision 2; wasmtime-py 50 has no API for it (S0012 Q4) |
| NaN canonicalisation | `cranelift_nan_canonicalization=True` raises | Decision 5; S0005 Q5 |
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
  and the host's JIT, both bounds modes on the JIT). Modules are written in WAT and built by the
  development tools (D21: `wasm-tools-package` is a development dependency, `pyproject.toml`); a
  test of the WAT path itself is skipped where the tool is absent. Step 2 declares the optional
  extra that D21 promises users.
* **Differential against wasmtime-py** (TESTING.md's API axis), on the same programs: CoreMark, the
  corpus of `tools/run_corpus.py`, and the spec modules of chosen directories. The same calls are made
  through `waboros` and `wasmtime`, comparing results, trap codes, memory hashes, global values and
  the exception type of host errors. **Known differences** are listed in a file, as expected spec
  failures are: integer arguments outside both the signed and the unsigned range (`ValueError` against
  wasmtime-py's silent wrap, for example `2**32 + 5` or `2**100` for an i32, Decision 9; inside
  those ranges results are equal);
  the D27 table cap; a funcref of another store (`TypeError` on our side); the values wasmtime-py
  cannot express (v128, the any and exn hierarchies, Decision 2); `WaborosError` against
  `WasmtimeError` (Decision 1); `wasm_*=False` and the other options that raise (Decision 5).
* Values: v128 round trips as `bytes`, from `bytes`, `bytearray`, `memoryview` and `int`, with the
  lane order checked against a `v128.store` into memory; wrong lengths raise. i31 round trips as
  `int`. An `AnyRef` and an `ExnRef` keep their identity across calls on every target.
* Handles: a scope releases what it created and only that; a handle returned out of a scope raises
  after it; `root`/`unroot`/`to_rooted`; a stale handle raises after its slot is reused (the
  generation check); everything raises after `close()`.
* Host exceptions across nested calls: a host function that calls wasm which calls a raising host
  function catches the original exception on every target, as with wasmtime-py.
* Serialization: a guard-mode module's file has explicit checks (its header's flags); a fat file
  regenerates on a mismatch; a cross-compiled engine (`Config.target`) writes a file that the other
  architecture's runner instantiates (D35's CI rows).
* **API-surface diff** against wasmtime-py 50.0.1 (TESTING.md): `dir()` of every public class.
  Missing names are reported, and the expected ones (WASI, components, `SharedMemory`,
  `WasmtimeError`) are listed in a file, as expected spec failures are.
* **wasmtime-py's own test suite through a shim** (TESTING.md): a counter, not a gate (Decision 1).
  The shim maps `wasmtime.WasmtimeError` to `waboros.WaborosError` on the test side.
* **The spec suite through the public API** (Decision 7): an engine `api-<target>`
  (`tests/spec/api_engine.py`, an S0001 adapter over the public API) runs the suite in CI on one
  row, with the same expected failures as its target. The metric stays on the internal engines.
* Lifetimes: `close()` and `with` release memory. The LESSONS 135 reproduction (N stores with a GC
  heap under `prlimit --data`) passes when each store is closed, and when each is dropped on CPython.
  Use after close raises `ValueError`.
* Threads: a store on a second thread raises; `increment_epoch` from another thread stops a long
  call on every target; host exceptions come out as the same object; D34's asynchronous test through
  the public API; a close/claim race test.
* Acceptance of the whole spec: `uv run pytest -q tests/api` passes on every CI row; the
  differential run shows only the known differences; the `api-<target>` run matches its target's
  expected failures; `python3 tools/check_docs.py` passes.

## Plan of steps
| step | goal | agent | acceptance | size (lines, estimated) |
|---|---|---|---|---|
| 0 | this spec approved; the S0002 amendment written as text (the list in "What the core must give") | orchestrator, maintainer | approval; `python3 tools/check_docs.py` | docs ~150 |
| 1 | S0002 amendment implemented on `pyc` and both JITs: extern handle operations, extern-level resolution, host externs (the two ways reported first, one implemented after the choice), compile options per engine, `release()` with the per-store state, limit hooks, generation-checked handles with scope records and manual-root slots, `OSError` → `MemoryError` | jit-engineer (JIT parts) + implementer (`pyc`), then reviewer | the spec suite at parity on the three targets, both bounds modes; new `tests/test_handles.py`; `pytest -q` | src ~500, tests ~400 |
| 2 | the skeleton: `Config` (with the options of Decision 5), `Engine` (with `target`), `Module`, the types, `WaborosError`, `Trap`, `__init__` with `__all__`; WAT through the extra | implementer | `pytest -q tests/api -k "config or engine or module or types"` | src ~650, tests ~450 |
| 3 | `Linker`, `Instance`, `InstancePre`, `Func`, `Caller`, `Memory`, `Table`, `Global`, `Tag`; the value conversions (Decisions 2 and 9: v128 bytes, i31, `AnyRef`, `ExnRef`, `Trap.exn`); host exceptions unwrapped at every depth; the trap mapping | implementer, then reviewer (the trap and host-exception mapping, GC and exception handles at the JIT boundary) | `pytest -q tests/api`; the differential run on CoreMark | src ~1 000, tests ~800 |
| 4 | `Store.close()` (with the thread rule), `with`, the finalizers, `store.scope()`, `root`/`unroot`/`to_rooted`, `Store.gc()`, `set_limits` | implementer, then reviewer (release of executable memory and the heap) | the lifetime and handle tests; the LESSONS 135 reproduction; a close/claim race test | src ~350, tests ~300 |
| 5 | epochs over the interrupt word; `pyc` yield points and depth counter, both measured | implementer, then reviewer | the thread tests; CoreMark with and without `epoch_interruption` on `pyc` in BENCHMARKS.md | src ~250, tests ~150 |
| 6 | `Module.serialize(fat=…)` with the recompile to explicit, `deserialize`, `deserialize_file`, `Config.target` cross-compilation | implementer | round trip on every JIT row; a guard-mode module's file is explicit; a refused file raises `WaborosError` with `ArtifactError` as cause; a cross-compiled file runs on the other architecture's row | src ~180, tests ~200 |
| 7 | the API-surface diff, the wasmtime-py suite counter, the corpus differential with its known differences, the `api-<target>` engine in CI (Decision 7), the threading stress test | runner + implementer | numbers in TESTING.md; CI job | tests ~600, CI ~50 |
| 8 | docs ("Docs this changes"), a DECISIONS entry | orchestrator | `python3 tools/check_docs.py` | docs ~180 |
| 9 | review without the implementers' reasoning: the whole API against this spec and wasmtime-py | reviewer | findings closed | — |

Total, estimated: about 2 900 lines of source and 2 900 of tests. Each step stops and reports if it
needs more than 120 % of its size.

## Docs this changes
* README.md: a usage example and the 0.x statement.
* [API.md](../API.md): the class table rewritten from this spec's tables; its "runs with the import
  changed" sentence replaced by Decision 1 (same signatures, not a drop-in, `WaborosError` without
  an alias, so it no longer says "the error class may keep the name `WasmtimeError` as an alias");
  its `config.target = "auto"` example replaced by `config.strategy = "auto"` (Decision 3); its
  rule "waboros additions (prefixed or in a submodule…)" amended to "unprefixed, named after
  Wasmtime's Rust API" (Decision 8), and its list of additions (`waboros.targets()`,
  `Config.target`) corrected (Decision 3).
* FEATURES.md, "Runtime features": the API column (done, deferred, deviation) per row. The
  "interruption" row's "`pyc` has yield points" is corrected: there are none yet (see
  "Contradictions found").
* CLAUDE.md, "Against over-engineering": "Do not touch the public API, the CLI or WASI before their
  phase" becomes "…the public API only under an approved phase-6 spec (S0018); the CLI and WASI
  before their phase".
* BRIEF.md: "the public API only in phase 6" points to this spec.
* TESTING.md: the API axis (the shim's counter and its `WasmtimeError` mapping, the surface diff,
  `tests/api/`, the known-differences file, the `api-<target>` engine of Decision 7).
* ARCHITECTURE.md, "The object model": `Config` notes ("`target='auto'`", "decide") replaced by
  Decisions 3 and 5.
* S0002: the amendment (step 0) and its Changes line.
* **S0017**: Decision 11 amended (Decision 4): the C loader's handles get generations, scopes and
  manual roots, to be specified when the C API gains them; until then its `waboros_ref_release`
  stands. This spec does not edit S0017; the orchestrator does, with the maintainer.
* DECISIONS.md: one entry for the API's shape (wasmtime-py's, the extensions of Decisions 2, 3, 4,
  6 and `store.scope()`, the 0.x statement, the nine decisions).
* ROADMAP.md phase 6: the items placed as in the table above; open question 4 answered by
  Decision 5; open question 3 partly answered ("Interruption").

## Alternatives considered
* **A drop-in replacement for wasmtime-py** (`import waboros as wasmtime`; former Q1 (c)). Cost:
  every name of the pinned version, including WASI, component-model trap codes and C-pointer
  helpers, follows wasmtime-py's releases. Rejected (ROADMAP "not a drop-in"; Decision 1).
* **The shape only, with our own tests and no wasmtime-py suite** (former Q1 (a)). Rejected: the
  suite as a counter costs little and measures closeness.
* **`WasmtimeError` as the class name, or `WaborosError` with a `WasmtimeError` alias** (former Q1).
  Rejected (Decision 1): our package does not carry another project's name; the shim maps it on
  the test side.
* **A new, Pythonic API** (no `store` argument, properties instead of setters). Rejected: D6 and
  BRIEF.md ask for wasmtime-py's shape, so that users recognise it.
* **No Python form for v128, GC and exception values** (former Q2 (a)): such parameters an
  explicit error. Rejected: the GC and exception proposals would be unreachable from Python.
* **v128 as an `int`** (former Q2 (b) as first drafted). Rejected for results (Decision 2): `bytes`
  in memory order reads like the memory the value comes from; an `int` stays accepted as input.
* **Field access and host-thrown exceptions now** (former Q2 (c)). Deferred to their own spec.
* **Target selection by environment only, or API.md's `Config.target` with our names** (former Q3
  (a), (c)). Rejected: the first leaves no public knob; the second gives wasmtime-py's
  cross-compilation name another meaning.
* **No explicit roots in 0.x** (former Q4 (a)). Rejected (Decision 4): one root model for every
  host now, so the C loader's later API is the same.
* **Accept and ignore every meaningless option, or reject them all** (former Q5 (a), (c)).
  Rejected: ignoring `wasm_simd=False` would change behaviour silently; rejecting
  `cranelift_opt_level` would break wasmtime-py code for nothing.
* **`serialize()` writes the code it has, guard mode included** (former Q6 (i)(a)), or amending
  S0014 Q12 (c). Rejected (Decision 6): Q12 is decided, and a guard-mode file would run only on rows
  with a verified handler.
* **Fat files from the CLI only** (former Q6 (ii)(a)). Rejected (Decision 6): `serialize(fat=True)`
  is the API's way to write the portable form of D18.
* **The spec runner on S0001 only, or moved onto the public API** (former Q7 (a), (c)). Rejected:
  the first leaves the conversions and the linker untested by the suite; the second ties the
  metric to API work (TESTING.md).
* **Prefixed names or a submodule for extensions** (former Q8 (b), (c)). Rejected (Decision 8):
  Wasmtime's Rust names make a future wasmtime-py name most likely coincide.
* **Wrapping integers silently, as wasmtime-py** (former Q9 (a)). Rejected (Decision 9): it hides
  bugs. A signed-only range was not chosen either: the unsigned spelling of a bit pattern
  (`0xFFFFFFFF`) is common in host code and is what wasmtime-py accepts in practice.
* **A mark in the handle table for scopes.** Rejected: freed slots are reused lowest first, and one
  handle per object means a mark releases handles made before the scope.
* **Renaming the internal packages to `_targets` etc.** Rejected: churn across `src/`, `tests/` and
  `tools/`, and a conflict with the concurrent work, for no behavioural gain.
* **Epochs compiled into the code** (Wasmtime's way). Rejected for now: a code change in both JITs
  for behaviour the host side gives without one. To revisit if latency measurements show the host
  side is too slow.
* **`memoryview` of linear memory.** Rejected: it breaks `pyc`'s grow (`BufferError`).

## Open questions
None open. Settled after the decisions:
1. **`Engine.target` against `Config.target`.** Decision 3 gives `Config.target` wasmtime-py's
   meaning (a triple) and adds a read-only `Engine.target` that names our target (`"jit-x86_64"`).
   One word then has two meanings on two classes. Options: (a) keep both as decided, documented;
   (b) name the read-only attribute after what it holds, `Engine.strategy` (the resolved value of
   the knob that chose it, for example `"jit-x86_64"`); (c) keep `Engine.target` but make it return
   the triple, with the waboros target name in another attribute. **Decided (Decision 10).**

## Contradictions found while writing
* FEATURES.md, "Runtime features", interruption: "done: … `pyc` has yield points". The code has
  neither yield points nor an `interrupt` in `targets/pyc/` (`StoreState.interrupt`'s docstring and
  S0002 say "pyc: later").
* D10 ("Stack depth is bounded per store with a deterministic `STACK_OVERFLOW` trap") and
  ARCHITECTURE.md's `pyc` depth counter: `pyc` has only the `RecursionError` safety net, and
  `max_wasm_stack` is read only by the JIT (`jit/common/instance.py`).
* API.md ("runs on waboros with the import changed") and TESTING.md's shim against ROADMAP phase 6
  ("not a drop-in"): settled by Decision 1.
* API.md's and ARCHITECTURE.md's `Config.target` with our names against wasmtime-py's
  `Config.target` (a triple): settled by Decision 3.
* API.md, "waboros additions (prefixed or in a submodule)", against unprefixed extensions: settled
  by Decision 8.
* BRIEF.md's map of the documents says "D1–D51"; DECISIONS.md runs to D76.
* S0015 "Compatibility" still says the per-architecture rule is "enabled in phase 6", while ROADMAP
  has moved it to the micro-compiler item.

## Changes
* 2026-11-25: created (draft), with the maintainer's direction of 2026-11-25: wasmtime-py's shape;
  context managers with deterministic release as the one extension (only `store.scope()` turned out
  to be new: wasmtime-py's `Managed` already gives `close`/`with`/`__del__`); other extensions only as
  open questions.
* 2026-11-25: review fixes: host exceptions unwrapped at every call depth, as wasmtime-py does; the
  scope mechanism (a record of created handles, not a mark) and its C-API consequence; finalizer
  claims qualified; `close()` and threads; what the core must give extended (extern-level
  resolution, `Caller`, per-store release, `OSError`); trampolines are per process; epoch details
  (deadline 0, the lock); the `MemoryError` mapping; `Config` consumption and `cache`; the
  `max_wasm_stack` default is right (512 KiB measured).
* 2026-11-25: the maintainer's nine decisions written in ("Decisions"); the rejected options moved
  to "Alternatives considered"; the body made consistent with them: `WaborosError`, v128 as
  `bytes`, `AnyRef`/`ExnRef`/`ManuallyRooted` with generation-checked handles, `strategy` and
  `target`, the option lists, `serialize` to explicit and `fat=True`, the `api-<target>` engine,
  `ValueError` for out-of-range integers. One new open question (`Engine.target`). Status stays
  draft until approval.
* 2026-11-25: Decision 9 refined (maintainer): the signed and the unsigned range are both accepted
  and converted to the bit pattern; `ValueError` only outside both.
* 2026-11-25: Decision 10 (maintainer): `Engine.strategy` (`jit`/`pyc`) and `Engine.target` (a triple, as `Config.target`); no open question left.
