---
title: Pyodide - running a guest wasm as an Emscripten side module (experiment)
kind: reference
status: draft
updated: 2026-11-01
---

# Pyodide: a guest wasm as an Emscripten side module, loaded with `ctypes.CDLL`

An experiment for ROADMAP open question 7 (Pyodide), by the maintainer: an experiment, not a design
decision. **Nothing here is approved for implementation**; it would be a separate target later. It
shows that in Pyodide a guest module can run at near browser-engine speed in the same process as
Python, entered through `ctypes`, with only the standard library on the Python side. The rewriter
used a dev tool (`wasm-tools`). A real target would emit the bytes itself.

## Result

CoreMark (`poc/in/core.wasm`), Node 22.23.4, Pyodide 314.0.7 from npm (CPython 3.14.2, wasm32), same
machine:

| path | CoreMark it/s |
|---|---|
| `core.wasm` directly through `WebAssembly` in Node (reference) | 25 300 - 25 800 |
| side module in Pyodide via `ctypes.CDLL` (this note) | 23 400 - 24 000 |

That is 92-95 % of the direct run. The CoreMark self-check passes ("Correct operation validated").
These are two runs, not a proper benchmark, so they do not go into BENCHMARKS.md as they are.

## Why `ctypes.CDLL`

In Pyodide, `ctypes.CDLL(path)` is Emscripten's `dlopen`, and that is the only stdlib-only way to put
wasm code into the same process as the interpreter. `dlopen`:

* compiles the module with the browser/Node engine (V8 here: Liftoff, then TurboFan);
* links it to the **main module's** linear memory and function table;
* exposes its exports as C symbols that `ctypes` calls directly.

So guest memory **is** a range of Pyodide's heap. Host functions read it with `ctypes.string_at` and
need no copies or JS proxies. This is the same model as `jit-<arch>`, with a different mechanism:
`dlopen` of wasm code where the JIT does `mmap` of machine code, and V8 compiles instead of our
encoder.

Alternatives compared:

* `js.WebAssembly.instantiate`: a separate instance with its own `WebAssembly.Memory`. Host access
  copies between memories, and every import or export crosses JS and Python proxies.
* A `.so` inside a wheel (`wasm32-emscripten` wheels): the same `dlopen`, but packaged as a CPython
  extension (`PyInit_*`). That is a packaging question, not a different mechanism.

## What an Emscripten side module needs

A side module is an ordinary wasm module with:

1. A custom section **`dylink.0` as the very first section**. Subsection 1 (`WASM_DYLINK_MEM_INFO`)
   is `memsize, memalign, tablesize, tablealign` as LEB128. Here everything is 0, because we
   allocate the guest region ourselves: bytes `01 04 00 00 00 00`.
2. `(import "env" "memory" (memory 0))`: the shared heap.
3. `(import "env" "__indirect_function_table" (table 0 funcref))`: the shared table. Needed for
   host callbacks (below).
4. No imports from foreign modules such as `wiring`: the loader resolves `env` and `GOT.*`
   symbols. Host functions therefore go through the table.

Normally, code from LLVM `-fPIC` also imports `__memory_base` / `__table_base` and adds them at run
time. We do not need that, because the base is known before the module is generated (below).

## The rewrite (core.wasm -> side module)

1. **Guest region.** Inside Pyodide, allocate `17 * 64 KiB` (the module's memory) with
   `ctypes.create_string_buffer` and align the base to 64 KiB. The address is a plain wasm32 address
   in Pyodide's heap.
2. **Bake the base into every access.** `memarg.offset += base` for every load and store. Guest
   addresses (stack pointer, pointers in memory, pointers passed to the host) stay
   **guest-relative**, so no other code changes, and the host adds `base` itself.
3. **Data segments** become active at `i32.const (base + off)` in the imported memory.
4. **Remove** the module's own memory and its `memory` export.
5. **Host imports** `wiring.millis` / `wiring.print` become defined functions **at the same
   indices** (0 and 1): imports always come before defined functions, so replacing them in place
   keeps every `call` index valid. Each stub does `call_indirect` through a mutable global that
   holds a table slot.
6. **`set_hosts(millis_slot, print_slot)`** is exported to fill those globals. It is appended as the
   **last** function: inserting it earlier shifts function indices. That was the first bug here.
7. In Pyodide, a `ctypes.CFUNCTYPE` callback cast to `c_void_p` **is a table index** (14177 and
   14178 in this run), so it goes straight into `call_indirect`. Its wasm signature has to match
   the type used in the `call_indirect`, or the engine traps with "signature mismatch".

CoreMark has no `memory.grow`, `memory.size`, bulk memory, tables or `call_indirect`, so these steps
were enough. The general case is in "Open questions".

### Generator (`gen/mkside.py`)

```python
"""core.wasm -> Emscripten side module (dylink.0) with the guest region baked at BASE.

No isolation: guest addresses are added to BASE through memarg offsets, no bounds checks.
Host imports wiring.millis/print become stubs that call_indirect through slots set by set_hosts().
"""
import re, subprocess, sys

src, base, out, wt = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
wat = subprocess.run([wt, "print", src], check=True, capture_output=True, text=True).stdout

mem_op = re.compile(r'\b((?:i32|i64|f32|f64)\.(?:load|store)[0-9_su]*)(?: offset=(\d+))?')
wat = mem_op.sub(lambda m: f"{m.group(1)} offset={int(m.group(2) or 0) + base}", wat)
wat = re.sub(r'\(data \(;(\d+);\) \(i32\.const (\d+)\)',
             lambda m: f"(data (;{m.group(1)};) (i32.const {int(m.group(2)) + base})", wat)

hdr = '''(module
  (@custom "dylink.0" (before first) "\\01\\04\\00\\00\\00\\00")
  (import "env" "memory" (memory 0))
  (import "env" "__indirect_function_table" (table 0 funcref))'''
wat = wat.replace("(module", hdr, 1)
wat = wat.replace('(import "wiring" "millis" (func (;0;) (type 0) (result i32)))',
                  '(func (;0;) (type 0) (result i32) global.get $p_millis call_indirect (type 0))')
wat = wat.replace('(import "wiring" "print" (func (;1;) (type 1) (param i32 i32)))',
                  '(func (;1;) (type 1) (param i32 i32) local.get 0 local.get 1 '
                  'global.get $p_print call_indirect (type 1))')
wat = wat.replace('  (memory (;0;) 17)\n', '')
wat = wat.replace('(export "memory" (memory 0))',
                  '(global $p_millis (mut i32) i32.const 0)\n'
                  '  (global $p_print (mut i32) i32.const 0)\n'
                  '  (export "set_hosts" (func $set_hosts))')
wat = wat.rstrip()
assert wat.endswith(")")
wat = (wat[:-1] + '  (func $set_hosts (param i32 i32) local.get 0 global.set $p_millis '
       'local.get 1 global.set $p_print)\n)\n')

open(out + ".wat", "w").write(wat)
subprocess.run([wt, "parse", out + ".wat", "-o", out], check=True)
subprocess.run([wt, "validate", out], check=True)
```

Text rewriting is enough for an experiment. A real target does the same while emitting binary:
* add `base` to each memarg;
* rewrite the data-segment offsets;
* replace the import entries with stubs.

### Driver (Node + Pyodide, `run_side.mjs`)

The base is known only inside Pyodide, so there are two steps: allocate the region, then generate
the module. The generator runs on the host here, because there is no `wasm-tools` inside Pyodide.

```js
import { loadPyodide } from "pyodide";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
const py = await loadPyodide();
const base = py.runPython(`
import ctypes
SIZE = 17 * 65536
buf = ctypes.create_string_buffer(SIZE + 65536)
base = (ctypes.addressof(buf) + 65535) & ~65535
base`);
execFileSync(".venv/bin/python", ["gen/mkside.py", "core.wasm", String(base), "side.wasm",
                                  ".venv/bin/wasm-tools"]);
py.FS.writeFile("/tmp/core_side.so", readFileSync("side.wasm"));
py.runPython(`
import ctypes, time
lib = ctypes.CDLL("/tmp/core_side.so")
t0 = time.monotonic()
MILLIS = ctypes.CFUNCTYPE(ctypes.c_int32)
PRINT = ctypes.CFUNCTYPE(None, ctypes.c_int32, ctypes.c_int32)
out = []
@MILLIS
def millis(): return int((time.monotonic() - t0) * 1000) & 0x7fffffff
@PRINT
def pr(p, n): out.append(ctypes.string_at(base + p, n).decode())   # guest ptr + base
ptr = lambda f: ctypes.cast(f, ctypes.c_void_p).value                # = table index
lib.set_hosts.argtypes = [ctypes.c_int32, ctypes.c_int32]
lib.set_hosts(ptr(millis), ptr(pr))
lib.setup.restype = None
lib.setup()
print("".join(out))
`);
```

`buf` has to stay alive while the module is used: it owns the guest region. Callback objects have to
stay alive too: `ctypes` frees the trampoline together with the Python object.

### Reference run (direct `WebAssembly`, `run_direct.mjs`)

```js
import { readFileSync } from "node:fs";
const t0 = performance.now(); let mem; let out = "";
const { instance } = await WebAssembly.instantiate(readFileSync("core.wasm"), { wiring: {
  millis: () => Math.floor(performance.now() - t0),
  print: (p, n) => { out += new TextDecoder().decode(new Uint8Array(mem.buffer, p, n)); } } });
mem = instance.exports.memory; instance.exports.setup();
console.log(out);
```

### Setup used

```
npm install pyodide                          # 314.0.7
python3 -m venv .venv && .venv/bin/pip install wasm-tools-package   # wasm-tools 1.263.0
node run_side.mjs
node run_direct.mjs
```

## Open questions (not yet checked)

1. **Isolation: there is none.** The guest can read and write the whole Pyodide heap: an
   out-of-bounds guest access corrupts the interpreter instead of trapping. That breaks the hard
   rule "never crash the interpreter". A real target needs an explicit bounds check (or a mask, with
   a power-of-two region) on every access. Measure its cost, as was done for the JIT.
2. **Traps.** Not tested. A wasm trap (`unreachable`, division by zero, signature mismatch) unwinds
   as a JS `WebAssembly.RuntimeError` through the `ctypes` frames. Find out whether Pyodide turns it
   into a Python exception or a fatal error. If it is fatal, the target needs the explicit
   status-word scheme of the JIT (D34-like: trap code into a context word, return normally).
3. **`memory.grow` / `memory.size`.** With a baked base, the region must be allocated at the
   module's maximum up front, and `grow` succeeds only inside it. The alternative is PIC: an
   imported `__memory_base` global added to every address, which is slower. Measure it.
4. **Tables and `call_indirect` of the guest.** The guest's table entries have to go into the shared
   table at `__table_base` (from `dylink.0` `tablesize`) with indices rebased. The type check of
   `call_indirect` then runs against the shared table: a guest's out-of-range index can reach
   foreign functions. That is another isolation point.
5. **Globals, multiple memories, exports of memory/table/global.** Not covered by CoreMark.
   Multi-memory means more than one baked region.
6. **D25 parity.** Trap codes and NaN behaviour come from the engine (V8). Check them against `pyc`
   and the spec suite in the same way the JIT targets are checked.
7. **Where the generator runs.** A real target emits the side module from Python inside Pyodide
   (front end + a byte rewriter, no `wasm-tools`) and writes it to the Emscripten FS before
   `CDLL`. Compile cost: V8's, not ours.
8. **Browser vs Node.** Only Node was tried. Browsers restrict synchronous compilation of large
   modules on the main thread (`dlopen` in Pyodide may need its async path or a worker). Check this
   in a browser.
