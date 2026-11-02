---
title: API and CLI
kind: reference
status: current
updated: 2026-11-02
---

# API and CLI

## Python API: after wasmtime-py

The public names and their methods follow **wasmtime-py 50.0.1** (the version installed when this
was written; checked by introspection), so code written for wasmtime-py runs on waboros with the import
changed. Where waboros cannot or should not do something, it says so explicitly (an error naming the
reason), never silently differently.

```python
from waboros import Engine, Config, Store, Module, Linker, WasiConfig

config = Config()
config.target = "auto"            # waboros: "pyc", "mpy", "jit-x86_64", ... or a list
config.cache = True               # same name as wasmtime-py's Config.cache
engine = Engine(config)
module = Module.from_file(engine, "app.wasm")

linker = Linker(engine)
linker.define_wasi()
store = Store(engine)
wasi = WasiConfig(); wasi.inherit_stdout(); wasi.argv = ["app"]
store.set_wasi(wasi)
instance = linker.instantiate(store, module)
instance.exports(store)["_start"](store)
```

### The names to provide

| wasmtime-py name | members in wasmtime-py 50 | waboros scope |
|---|---|---|
| `Config` | `cache`, `consume_fuel`, `epoch_interruption`, `max_wasm_stack`, `memory_reservation`, `memory_guard_size`, `wasm_*` feature switches (see FEATURES.md), `strategy`, `target`, `cranelift_*`, `profiler`, `debug_info`, `parallel_compilation`, … | feature switches, fuel, epoch, stack, cache: yes. `strategy` / `target` map to our target choice. `signals_based_traps`: `False` selects explicit bounds checks in the JIT (D29). `cranelift_*`, `profiler`, `native_unwind_info`, `macos_use_mach_ports`: accepted with no effect or rejected (decide once, document) |
| `Engine` | `increment_epoch`, `is_pulley` | yes; plus waboros's `target` (read-only, what was selected) |
| `Module` | `Module(engine, wasm)`, `from_file`, `validate`, `imports`, `exports`, `serialize`, `deserialize`, `deserialize_file` | yes; `.wat` input through an optional text parser (wasmtime-py accepts text too) |
| `Store` | `data`, `gc`, `get_fuel`, `set_fuel`, `set_epoch_deadline`, `set_limits`, `set_wasi`, `set_wasi_http` | yes; `set_wasi_http` through the WASI host's `wasi:http` |
| `Linker` | `allow_shadowing`, `define`, `define_func`, `define_instance`, `define_module`, `define_wasi`, `define_unknown_imports_as_traps`, `define_unknown_imports_as_default_values`, `get`, `get_default`, `instantiate`, `instantiate_pre` | yes |
| `InstancePre`, `Instance` | `Instance.exports` | yes |
| `Func`, `Caller`, `FuncType` | `Func.type`, `Caller.get` | yes; host functions as plain callables, `access_caller=True` as in wasmtime-py |
| `Memory`, `MemoryType`, `SharedMemory` | `read`, `write`, `data_ptr`, `data_len`, `get_buffer_ptr`, `grow`, `size`, `type`, `page_size`, `page_size_log2` | yes; `data_ptr` meaningful for JIT memories, emulated (ctypes) for `bytearray` ones |
| `Table`, `TableType`, `Global`, `GlobalType`, `Tag`, `TagType` | `get`, `set`, `grow`, `size`, `type`, `value`, `set_value` | yes |
| `Val`, `ValType`, `Limits`, `ImportType`, `ExportType` | value and type objects | yes |
| `Trap`, `TrapCode`, `ExitTrap`, `Frame`, `WasmtimeError` | `TrapCode` has ~50 codes, most for the component model | yes for core-wasm codes; the error class may keep the name `WasmtimeError` as an alias for drop-in use |
| `WasiConfig` | `argv`, `env`, `inherit_argv`, `inherit_env`, `inherit_stdin/stdout/stderr`, `preopen_dir`, `stdin_file`, `stdout_file`, `stderr_file`, `stdout_custom`, `stderr_custom` | yes, mapped onto the WASI host (fusedwasi's `Host` / `State` if it is chosen) |
| `wat2wasm` | function | optional (needs a WAT parser; could come from a pure-Python one) |
| `wasmtime.component` | component model | **no** — fused/flattened modules only (FEATURES.md) |

waboros additions (prefixed or in a submodule, so they never collide with future wasmtime-py names):
`waboros.targets()` (what is available here and why not), `Config.target`, `Engine.target`, cache
management functions.

## CLI: after the `wasmtime` binary

wasmtime-py 50 ships **no CLI** (`python -m wasmtime` fails: no `__main__`). The model is therefore
the `wasmtime` command itself. Entry points: `waboros …` and `python -m waboros …`.

| command | meaning | as in wasmtime |
|---|---|---|
| `waboros run [opts] file.wasm [args…]` | run a command module (`_start`) or `--invoke name` with arguments | `wasmtime run` |
| `waboros compile [-o out] [--fat] file.wasm` | write the serialized file for a target (`--target`), `<name>.<target>.w2a`; `--fat` embeds the wasm so the file regenerates itself on a mismatch (DECISIONS.md D18) | `wasmtime compile` (`.cwasm`) |
| `waboros wast file.wast` | run a spec-test script (needs a WAT/WAST parser or a `.json` from `wast2json`) | `wasmtime wast` |
| `waboros settings` / `waboros targets` | the targets available on this machine, their features, why others are not | `wasmtime settings` |
| `waboros cache {stats,clear,dir}` | the compilation cache | `wasmtime config` / cache options |

`run` options to keep compatible: `--dir host[::guest]`, `--env NAME=VAL`, `--invoke NAME`,
`--fuel N`, `-W` feature switches (e.g. `-W simd=y`), `-S` WASI options. waboros-specific:
`--target NAME`, `--no-cache`.
