---
title: JIT artifact, ISA levels and the context ABI
kind: reference
status: current
updated: 2026-11-16
---

# JIT artifact, ISA levels and the context ABI

A JIT target's output is plain machine code with a small, fixed boundary. Nothing in it is
Python-specific (measured: the same module compiled under CPython 3.8 and 3.13 gives byte-identical
code), so the artifact is **a language-neutral format**: any host that can map executable memory, call
a C function pointer and pass C callbacks can load and run it (C/C++, Dart FFI, LuaJIT, Ruby Fiddle,
Java FFM, .NET, Deno/Bun, MicroPython's `ffi`). This document is the contract such hosts — and our own
Python loader — rely on. It has three parts, each versioned.

> **Scope (DECISIONS.md D18).** First of all this is the format of the waboros runtime's own cache entries
> and serialized files (`Module.serialize`, `waboros compile`): waboros follows the Wasmtime model, the
> runtime lives in the package and nothing is embedded in the artifact. Loading from other languages
> stays possible because the boundary is neutral, but it is optional.

> The PoC (`poc/w2jit.py`, `w2jit2.py`) predates this contract: its context layout and entry signature
> are a subset of version 1 below and must be brought in line in phase 5.

## 1. ISA levels

An artifact declares the **instruction-set level** it was compiled for; a loader refuses an artifact
the CPU cannot run.

| architecture | baseline (default for portable artifacts) | optional levels / features the compiler may use |
|---|---|---|
| x86-64 | **x86-64-v1**: SSE, SSE2 (every x86-64 CPU) | `x86-64-v2` (SSE3, SSSE3, SSE4.1, SSE4.2, POPCNT), `x86-64-v3` (AVX, AVX2, BMI1/2, LZCNT, FMA, MOVBE), single features by name |
| aarch64 | **ARMv8.0-A** with FP and Advanced SIMD (NEON) | `lse` (v8.1 atomics), `fp16`, `dotprod`, `rcpc`, … by name |
| riscv64 | planned (D48, the third JIT architecture, after `aarch64.wasm`); defines its baseline here before its first release | RVV 1.0 as an optional level |
| later | none planned: every other architecture runs `pyc` (D48) | |

Rules:

* **Portable artifacts** (`waboros compile`, shipped files) use the baseline unless asked otherwise.
  An operation whose natural instruction is above the baseline gets a baseline sequence. Example found
  in the PoC: `f64.trunc` was emitted as `roundsd` (SSE4.1, x86-64-v2); at v1 it needs an SSE2
  sequence (convert-truncate for |x| < 2⁵², otherwise x itself, preserving sign and NaN). The same
  applies to `floor`, `ceil`, `nearest`, `popcnt`, `clz`, `ctz`.
* **Baseline first (D24).** Every operation is implemented at the baseline first; the baseline path is
  mandatory and is what every artifact uses by default. A path above the baseline exists only as an
  **optional, declared variant** next to its baseline twin, added when a measurement shows it pays;
  an above-baseline instruction the PoC already uses is kept this way, not dropped (known so far:
  `roundsd`, SSE4.1 / x86-64-v2, for `f64.trunc` in `poc/w2jit.py` and `poc/w2jit2.py`).
* **Local cache artifacts** may use `isa="native"` once optional variants exist: the features of the
  running CPU, recorded in the artifact and in the cache key, so a cache moved to another machine is
  recompiled, not executed.
* **Checked, not trusted**: CI disassembles generated code (`objdump`) and fails if any instruction is
  above the level the artifact declares.
* `Config.isa` (waboros addition): `"baseline"` (default), a level name, or a level plus feature names
  (explicit opt-in, stage 1 of D33); `"native"` comes later (stage 2) and becomes the default only by a
  separate decision. A requested level the CPU lacks is refused at load, naming the missing features.
* **Detection at load time, stdlib only**: on x86-64 a small `cpuid` stub run through the same
  executable-memory + ctypes path (works on every OS); on aarch64 `getauxval(AT_HWCAP/AT_HWCAP2)` via
  ctypes on Linux/Android, `sysctlbyname("hw.optional.*")` on macOS, `IsProcessorFeaturePresent` on
  Windows. Hosts in other languages do the equivalent.

## 2. Artifact format (version 1)

A little-endian binary container. Self-contained: a host needs **no wasm parser** to instantiate it.
Open before the format is fixed (phase 5): our own container or ELF64 as a container only, as
Wasmtime does; see [ARTIFACT-CONTAINER.md](ARTIFACT-CONTAINER.md) (maintainer's note, draft).

**Header** (fixed size):

| field | type | meaning |
|---|---|---|
| `magic` | 8 bytes | `WABOROS\0` |
| `format_version` | u16 | this document's part 2 version (1) |
| `ctx_abi_version` | u16 | part 3 version (1) |
| `arch` | u16 | 1 = x86-64, 2 = aarch64, 3 = riscv64, … (registry in this file) |
| `isa_level` | u16 | per architecture: 1 = baseline, 2 = v2, 3 = v3, … |
| `isa_features` | u64 | bitset of extra features (registry per architecture) |
| `flags` | u32 | code-affecting options: fuel, epoch/interrupt polling, bounds-check mode, … Bit 0 `BOUNDS_GUARD` and bits 8–15 `GUARD_LOG2` are fixed by S0006; the package computes them (`FLAGS` in `guard.py`) but serializes no artifact before the phase-5 spec |
| `compiler` | str | `waboros <version> <target> <target version>` |
| `module_sha256` | 32 bytes | hash of the wasm bytes |
| `config_sha256` | 32 bytes | hash of every code-affecting option |
| `section_count` | u32 | |

**Sections** (`id: u16`, `length: u32`, payload; unknown ids are skipped):

| id | section | content |
|---|---|---|
| 1 | `CODE` | the machine code, position-independent: internal branches relative, everything absolute through the context register |
| 2 | `ENTRIES` | entry trampolines, one per host calling convention: `{sysv: offset, win64: offset}` on x86-64; `{aapcs64: offset, win-arm64: offset}` on aarch64. The host picks its own |
| 3 | `FUNCS` | per defined function: code offset, type index, frame size (slots), name (if any) |
| 4 | `TYPES` | function types (param/result value types) |
| 5 | `IMPORTS` | function imports: module, name, type index, context slot; memory/table/global imports with their types |
| 6 | `EXPORTS` | name → kind + index |
| 7 | `MEMORIES` | per memory: min, max, page size, 64-bit, shared |
| 8 | `TABLES` | per table: element type, min, max, address type (i32 or i64, S0009) |
| 9 | `GLOBALS` | per global: type, mutability, initial value (or the import it comes from) |
| 10 | `SEGMENTS` | data and element segments: active (target, offset) or passive, bytes / function indices |
| 11 | `START` | start function index, if any |
| 12 | `TRAPMAP` | code offset ranges → wasm function index + wasm byte offset (for `Trap.frames`); with the guard flag also the fault map, `{fault_offset, resume_offset}` per memory instruction ([S0006](specs/S0006-guard-pages.md), "Artifact"). Neither is written to a file yet |
| 13 | `RELOCS` | empty in version 1 (code is position-independent); reserved for copy-and-patch |
| 14 | `WASM` | optional: the original wasm bytes. Its presence makes a **fat** file (below) |
| 15 | `STACKMAPS` | one record per call site of the code, for the frame walk of the GC (S0013, below). Required: a loader refuses a module without it, since every frame of a store must be walkable |
| 0xFFFF | `CHECKSUM` | sha256 of everything before it |

**`STACKMAPS`** (S0013 step 3a; added to version 1 without a bump, as no artifact file has been
written, S0013 Q5). Little-endian: `u32 nsites`; per site, sorted by `ret`: `u32 ret` (the code
offset of the return address of a call), `u32 shift` (slots from this frame's base to the callee's:
0 for a host or service call and in a host thunk), `u16 ret_at` (bytes from the stack pointer at the
call, before x86-64's `call` pushes, to this frame's own return address), `u16 up` (bytes from that
stack pointer to the caller's stack pointer at its own call), `u32 map` (0: no reference slot; k:
the k-th map of the pool); then `u32 nmaps` and per map `u32 nslots` and `ceil(nslots / 32)` u32
words, bit i set when value-stack slot i of the frame (locals first, a v128 two slots) holds a
reference of the any or extern hierarchy at that site. Equal maps are stored once. Every call site
has a record, a module without any reference type too (16 bytes per site: CoreMark 108 sites, 1 736
bytes). The trampolines' own points (the entry's return point and its saved block, `tail_switch`'s
`restore` frame) are part 3's, not this section's.

Strings are UTF-8 with a u32 length prefix. A loader refuses an artifact whose `magic`,
`format_version` major, `ctx_abi_version`, `arch` or ISA it does not support — it never "tries".
`Module.serialize()` writes this format; the cache stores it.

**Thin and fat files (D18).** A thin file (no `WASM` section) is refused on any mismatch. A fat file is
never refused: when the loader cannot use the code — another waboros or target version, another ISA
level, a compiler version on the known-bad list, a format it does not know — it checks that the
embedded wasm matches `module_sha256` and regenerates from it. For this the loader must be able to
read the header and find section 14 in every format version, so the header prefix (`magic`,
`format_version`) and the section framing are frozen across versions.

**Other targets.** Serialized `pyc` and `mpy` modules use the same container with their own
`arch`/`isa` meaning (the bytecode magic / `.mpy` version) and the marshalled code object or `.mpy`
as `CODE`; to settle in phase 2. File names: `<name>.<target>.<ext>`, e.g. `core.jit-x86_64-v1.w2a`,
`core.pyc-cp313.w2a` (`.w2a` is a working extension).

## 3. Context ABI (version 1)

What compiled code expects from its host at run time. Any change to a word's meaning or position is a
new `ctx_abi_version`; new words are only ever appended in reserved space.

**Entry.** Each trampoline in `ENTRIES` is a C function in the host's calling convention:

```c
int32_t entry(uint64_t *ctx, uint64_t *frame, const void *code_of_function);
```

* `frame` points into the host-allocated **value stack**; the arguments are written to `frame[0..n)`,
  the results come back in `frame[0..m)`, one 64-bit slot per value (i32 zero-extended in the low half;
  i64 as is; f32/f64 as bit patterns; v128 takes two slots, the low 64 bits (bytes 0–7) in the first
  slot or word, as for a v128 global; references as pointer-sized values).
* The return value is `0` or a trap code (below). On a trap the machine stack is unwound to the
  trampoline, which returns normally: the host never sees a signal or a non-local jump.
* **Re-entrant**: the trampoline keeps nothing in the context but `TRAP_CODE`, so wasm → host → wasm
  nesting works (the PoC lacked this; LESSONS.md 4).

**Context words** (`uint64_t ctx[]`, 8-byte aligned, at a stable address for the instance's lifetime;
the phase-3 meaning of each word and the register roles are in [S0003](specs/S0003-jit-context-abi.md)):

| index | name | direction | meaning |
|---|---|---|---|
| 0 | `SAVED_SP` | internal | zero (traps return through frames, and the guard-page fault handler resumes at a stub, S0006, so neither uses it); kept reserved |
| 1 | `TRAP_CODE` | code → host | written by the trampoline when it returns a non-zero status |
| 2 | `INTERRUPT` | host → code | non-zero: the code returns `INTERRUPT` at its next poll (any thread may set it) |
| 3 | `FUEL` | both | remaining fuel (signed), when compiled with fuel |
| 4 | `EPOCH_DEADLINE` | host → code | when compiled with epochs |
| 5 | `VSTACK_LIMIT` | host → code | address the value stack must not cross → `STACK_OVERFLOW` |
| 6 | `NSTACK_LIMIT` | host → code | lowest native stack address allowed → `STACK_OVERFLOW` |
| 7 | `HOST_ERROR` | host → code | the status of the current host call: the bridge presets 13, the host function writes its status last (D34) |
| 8 | `HOST_BRIDGE` | host → code | address of the host-call bridge for the host's calling convention |
| 9 | `TAIL_SWITCH` | host → code | address of the cross-context tail-call trampoline (below; D30) |
| 10 | `EXCEPTION` | host → code | address of the store's pending-exception word: the exnref of the exception in flight, valid only while status 14 propagates (S0012); from S0013 step 6.2 it holds 0 or the zero-extended u32 heap offset of the last exception thrown, and the word is a root of every collection |
| 11 | `EXN_REFILL` | host → code | the exception arena's refill service (a host function, signature below; S0012); no code calls it since S0013 step 6.2 (6.3 makes the word reserved) |
| 12, 13 | `ARENA_CURSOR`, `ARENA_LIMIT` | host → code | addresses of the store's arena cursor (next free byte) and limit (end of the current chunk) (S0012); no code reads them since S0013 step 6.2 (6.3 makes the words reserved) |
| 14 | `EXIT_RECORD` | host → code | address of the store's exit record `{sp, pc, frame}` of the innermost host call: the bridge writes it before the host runs (the calling frame's stack pointer at its call, the return address, the frame register) and clears `sp` after; every entry keeps the enclosing record and its frame on the native stack during its call and puts the record back (S0013; an amendment of v1 without a bump, Q5) |
| 15 | reserved | | zero |
| 16 … | per memory *i*: `base`, `size_bytes` (2 words) | host ↔ code | imported memories first; an i32 memory's base never moves (4 GiB + 32 MiB reserved, committed on grow; S0006); an i64 memory's base and `size_bytes` may both change during any host call or libcall (a `memory.grow` that moves it; D50, S0009: an amendment of v1 without a bump), so code keeps neither across a call: code that keeps a memory in registers (x86-64: memory 0 in r13/r14; aarch64: x26/x25) reloads them after every host call, any other memory is read here at each access |
| … | the GC heap: `base`, `size_bytes` (2 words), right after the module's memories | host ↔ code | only in a module that uses GC (GC types or instructions, or a `call_indirect` / `return_call_indirect` to a non-final type; S0013 step 3b, an amendment of v1 without a bump, Q5) or, from S0013 step 6.1, allocates or reads exception objects (a `throw`, or a `try_table` with a `catch` or `catch_ref` clause; a module with only `throw_ref` and `catch_all(_ref)` has none): the store's heap, an i32 memory (4 GiB + 32 MiB reserved, never moves) that `memory.size`, `memory.grow` and the libcall immediates do not count; a module without memories of its own has it as memory 0 (r13/r14, x26/x25). `size_bytes` is the committed end: everything below it is committed |
| … | per table *i*: `base`, `size` (2 words) | host ↔ code | `base`: the element words (one per element), `size`: elements; `table.grow` may move the elements, so code reads both words at each use and never keeps them across a host call. A `funcref` element (and value) is 0 (null) or the address of a function record (below); an `externref` element is 0 (null) or a reference word: a host value is `(h << 2) \| 2`, *h* its 1-based handle in the store (S0013 step 3b; the encoding is the runtime's, no code reads the bits); an `exnref` element is 0 or an exception object's heap offset (S0013 step 6.2) |
| … | per global *i*: 1 word (2 for v128) | both | imported globals: a pointer to the owner's word instead |
| … | per function import *i*: 1 word | host → code | address of a host function (signature below) |
| … | per libcall *j*: 1 word | host → code | a runtime service, same signature as an import, in a fixed order, all six words in every context: 0 `memory.grow`, 1 `table.grow`, 2 `table.init`, 3 `elem.drop`, 4 `memory.init`, 5 `data.drop`; never computation (D25). The operands are in the slots, the instruction's immediates (table, segment) after them; the memory index of `memory.grow` and `memory.init` only when the module has more than one memory. Operands and results have the address width of the memory or table they name (S0009, an amendment of v1 without a bump, Q4): for an i64 memory the delta, the old size and its failure −1 of `memory.grow` and the destination of `memory.init` are 64-bit values (−1 is `2⁶⁴−1`), for an i32 one they are i32 zero-extended as before; `table.grow` and `table.init` of an i64 table the same way (S0009 step 3) |
| … | per signature *k*: 1 word | host → code | the canonical type id of a type the module's `call_indirect`s name (equal for equivalent types of every module of the process, never 0) |
| … | per function record *r*: 3 words | host → code | `{code address, canonical type id, context address}` of every function import and of every function the module can take a reference to (`ref.func`, segments): a defined one; an import from another instance of the same store (its code and context: a direct call); a host function (the module's thunk, which calls the import's word through the bridge). `call_indirect` checks bounds, null and the type id, then calls `code` with r15 = `context` (and that context's memory words); `call` and `return_call` of an import go through the import's record the same way (D32) |
| … | per tag *t*: 1 word | host → code | the tag's identity: non-zero, unique in the process, imported tags first; an imported tag has its exporter's value, so a `catch` compares identities, never indices (S0012) |
| … | the GC group: 8 service words | host → code | a module with the GC heap only (above; S0013 step 3b), runtime services with the libcall signature, in this order (operands, then immediates, in slots): 0 `ALLOC` (`[bytes]`: room past the cursor; the code bumps it when it retries), 1 `ARRAY_NEW` (`[value low, high, n, type]` → object), 2 `ARRAY_NEW_DATA` and 3 `ARRAY_NEW_ELEM` (`[offset, n, type, segment]` → object), 4 `ARRAY_FILL` (`[array, i, value low, high, n, type]`), 5 `ARRAY_INIT_DATA` and 6 `ARRAY_INIT_ELEM` (`[array, d, s, n, type, segment]`), 7 reserved, zero (`FUNC_SUBTYPE` until S0013 step 5.6; no code uses it). An element value is always two words (a scalar's second is 0) |
| … | per RTT *k*: 1 word | host → code | a module with the GC heap only: the offset in the heap of the type-info record of each struct or array type the code allocates inline or casts to (equal for equivalent types of every module of the store), then (S0013 step 6.1) of the exception record of each tag the code throws (by tag index; tags of equal type share the record) |
| … | `IS_SUBTYPE`: 3 words | host → code | a module with the GC heap only (S0013 step 5.6, an amendment of v1 without a bump, Q5: appended, no word moves): a function record `{code, 0, context}` of the collector module's `is_subtype(q, t: i64) -> i32` in the store's collector instance; written when the module's code has a function-subtype site (below), else zero |

Offsets of the variable part follow from the counts in the artifact, in exactly this order.

**The GC heap** (S0013 step 3b; the collector and its regions B come with step 5). Offsets in the
heap: 0-7 zero (null is 0), the allocation cursor (u32) at 8 and its limit (u32) at 12; the rest
of the header page belongs to the collector module (S0013 step 5.2, `runtime/gc/collector.wat`):
at 16 the handle-report overflow flag (u32), 20-2047 reserved, [2 KiB, 4 KiB) the handle report (bit
*h* set for each host word `(h << 2) | 2` a collection found reachable, *h* < 16 384; a larger *h*
sets the flag instead) and [4 KiB, 64 KiB) `ROOTS`, the buffer of 7 680 u64 root words the module
forwards in place (the module traps on a malformed heap, but without a map of object starts it
cannot tell an aligned interior offset from an object start: S0013 Changes, 5.2); type-info
records in [64 KiB, 60 MiB); the **tid map** in [60 MiB, 64 MiB) (S0013 step 5.6): the u32 offset
of the record of process-wide function type id *t* (ids count every function type the JIT sees in
the process) at `60 MiB + 4t` (0: none), for *t* < 2²⁰,
committed whole with the first function-type record (a type whose id would pass the cap fails the
instantiation with "GC type area exhausted"); objects from 64 MiB in two adjacent semispaces `[64 MiB, 64 MiB +
C/2)` and `[64 MiB + C/2, 64 MiB + C)`, both committed (`size_bytes` is `64 MiB + C`), whose
boundary moves when the heap grows, with `64 MiB + C <= 2³² − 128 KiB` (D61). Code allocates by
`cursor + size` against the limit (in 64 bits) and calls `ALLOC` when it does not fit; the cursor
and the limit lie in the active space, and a collection (in `ALLOC` or an array service) moves
them into the other half. A
**reference word** of the any or extern hierarchy is 0 (null), `(v << 1) | 1` for an i31, `(h << 2)
| 2` for a host value (*h* its 1-based handle in the store; at most 2³⁰ − 1, so a word fits 4
bytes), or an object's offset (a multiple of 8). An `externref` element or value is such a word;
an `exnref` is 0 or an exception object's offset (S0013 step 6.2), and slots, globals, table
words and the pending word hold every such word zero-extended to 64 bits.
**Objects**: a u32 type-info offset and a u32 of collector bits (0 while wasm runs); a struct's
fields from 8 in declaration order at their natural alignment (`i8` 1, `i16` 2, `i32`/`f32` 4,
`i64`/`f64` 8, `v128` 16 (unaligned access), an any-, extern- or exn-hierarchy reference 4 (exn
from S0013 step 6.2, 8 before), `funcref` 8), its size a multiple of 8 and at least 8; an array's u32 length at 8 and its elements
from 16. **Type-info records** (u32 words): `kind | final << 8` (1 struct, 2 array, 3 function),
the depth *d*, the struct's size or the array's element size (0 for a function type), the number
of traced reference fields (an array: 1 if its elements are references of the any, extern or exn
hierarchy; 0 for a function type), the display (*d* + 1 record offsets, the last its own), then
the traced fields' offsets. A cast to a final type compares the object's record with the RTT word;
to a non-final one, the display entry at the target's depth. A **function-type record** is written
at instantiation for every function type of a module with the heap that has a supertype or is not
final, its supertypes first, and entered in the tid map; no object has one. An **exception
record** (S0013 step 6.1) is a kind-1 record, final, depth 0, written at instantiation for the
type of each tag a module throws: the object is a struct of the u64 tag identity at 8, then the
tag's parameters at their natural alignment as struct fields; its key is the type's canonical key
prefixed `exn `, so it is a different record from that type's function-type record. Its traced
fields are the parameters of the any, extern and exn hierarchies. **A function-subtype
site** (`call_indirect`, `return_call_indirect`, `ref.test`, `ref.cast`, `br_on_cast(_fail)` to a
concrete function type): the function record's type id (word 1) against the signature word; equal
is a match; else a final target fails, and a non-final one calls `IS_SUBTYPE` with `q` (the
record's id) and `t` (the signature word) as the callee's frame above every slot the site reads
after it (B1): 1 is a match. `is_subtype` answers 0 for an id at or past the cap or without a map
entry (equality was tested already) and traps on a malformed record.

**Host functions** (imports and libcalls) share one C signature in the host's calling convention:
`int32_t f(uint64_t *ctx, uint64_t *slots)` — the arguments in `slots[0..n)`, one 64-bit slot per
value as for entries; the function writes its results to `slots[0..m)`, then, as its last action, its
status to `ctx[HOST_ERROR]`: `0`, a trap code, or `13` `HOST_ERROR` (the host keeps the actual
exception). Its C return value is ignored (D34). Compiled code calls it through the bridge
(`ctx[HOST_BRIDGE]`), which saves the pool registers the host convention lets a callee clobber,
presets `ctx[HOST_ERROR]` to 13, returns the word as the call's status and puts back the word's
previous value (an enclosing host call's preset): a host function that does not finish fails the call.
A host function may enter wasm again; the nested entry's frame starts at `slots + max(n, m)`, right
above the call's slots, so no value-stack slot above them survives the call. Compiled code keeps
nothing it reads after a host call there (S0013 B1; the core asserts it at every call site; an
amendment of version 1 without a bump, as no artifact file has been written, S0013 Q5). The
exception refill (`EXN_REFILL`) does not set the frame base and must not enter wasm; no code calls
it since S0013 step 6.2.

**Exceptions** (S0012, D52; an amendment of version 1 without a bump, as D34). An exception travels
as status `14` returned through frames, as a trap does; inside a `try_table` a call site's non-zero
status goes to the try_table's landing pad, which dispatches only 14 and passes every other status
on. From S0013 step 6.2 (D63) an **exception object** is an object of the GC heap with its tag
type's exception record (above): the u64 tag identity at 8, the payload as struct fields from 16
at their natural alignment; an `exnref` is its offset, null 0. `throw` allocates it as `struct.new` does (the cursor against
the limit, a cold `ALLOC` call when it does not fit, which may collect; the payload is read from
its slots after the call), writes the payload and the identity, stores the offset in the pending
word (word 10's target) and returns 14. A failed allocation ("GC heap out of memory") is status
13, which no pad catches. A landing pad reads the identity and the payload through the heap
memory's words with checked loads and compares the identity in 64 bits. Until step 6.2 objects
were 8-byte words `{tag identity, payload words…}` in a store arena that `throw` bumped through
words 12 and 13, calling `EXN_REFILL` for a new chunk (S0012). At an entry, status 14 is the
host's `Trap(UNCAUGHT_EXCEPTION)` with the tag and the values, read from the object before any
wasm or collection runs; a host function never returns 14 (a re-raised `UNCAUGHT_EXCEPTION` trap is status 13).

**Tail calls** (D30). A `return_call*` puts its arguments at its own frame base, drops its native
frame and jumps, so both stacks stay constant. When the callee's function record names another
context, the code jumps to `ctx[TAIL_SWITCH]` instead, with the record's address and the caller's
return address where the architecture's table below puts them.
The trampoline (one per process and convention, in the host's code area like the bridge) does the
following. If that return address is its own `restore` point, it loads the record's context and jumps
to its code. Otherwise it saves the current context, loads the record's context and memory words, and
calls the code; at `restore` it puts the saved context back, reloads the memory words and returns the
callee's status. Every return matches its call. The restore frame is 16 bytes on both
architectures, the saved context at the stack pointer of the `call`/`blr` and the real return
address 8 above it; the GC's frame walk steps over it by these numbers (S0013).

**Per architecture** (S0004, D38; an amendment of version 1, which phase 5 fixes): what a host that
provides the bridge or the tail-call trampoline itself must follow. Everything else in this part is
the same 64-bit little-endian words for every architecture.

| | x86-64 | aarch64 |
|---|---|---|
| bridge call | `eax` = word, `rdx` = slots; `call` | `x0` = word, `x1` = slots; `blr x16` |
| bridge preserves | pool (`rbx rbp rsi rdi r8–r11`), `r12`–`r15` | `x2`–`x8`, `x19`–`x28`, `x29`, `sp` |
| `tail_switch` | record in `rdx`, return address at `[rsp]` | record in `x16`, return address in `x30` |
| exit record (bridge) | `sp` = rsp + 8 at entry (before the `call` pushed), `pc` = `[rsp]`, `frame` = `r12` | `sp` = sp at the `blr`, `pc` = `x30`, `frame` = `x27` |
| entry's saved block | `[rsp, rsp + 32)` at its `call`: enclosing `{sp, pc, frame}`, then its frame | `[sp + 96, sp + 128)` at its `blr`, the same four words |
| landing pad | `endbr64` | `bti c` (functions, trampolines), `bti j` (jump-table pads) |
| `CODE` alignment | none | load address ≡ 0 mod 4096 (`adrp`) |
| instruction cache | coherent | invalidate the loaded range before the first call |

**Trap codes** (keep the PoC's first six numbers; names are Wasmtime's `TrapCode`):

| code | trap | code | trap |
|---|---|---|---|
| 0 | none | 8 | `BAD_SIGNATURE` |
| 1 | `UNREACHABLE` | 9 | `TABLE_OUT_OF_BOUNDS` |
| 2 | `MEMORY_OUT_OF_BOUNDS` | 10 | `NULL_REFERENCE` |
| 3 | `INTEGER_DIVISION_BY_ZERO` | 11 | `OUT_OF_FUEL` |
| 4 | `INTEGER_OVERFLOW` | 12 | `INTERRUPT` |
| 5 | `STACK_OVERFLOW` | 13 | `HOST_ERROR` (waboros) |
| 6 | `BAD_CONVERSION_TO_INTEGER` | 14 | `UNCAUGHT_EXCEPTION` (inside code: an exception in flight) |
| 7 | `INDIRECT_CALL_TO_NULL` | 15 | `HEAP_MISALIGNED` |
| 16 | `ARRAY_OUT_OF_BOUNDS` (S0013) | 17 | `CAST_FAILURE` (S0013) |
| 18 | `ALLOCATION_TOO_LARGE` (S0013: past Wasmtime's bound for the element size) | | |

**Versioning rules**

* `format_version` and `ctx_abi_version` are independent. Loaders accept exactly the versions they
  know; the compiler can emit older versions while hosts migrate.
* Any change here goes with: the version bump, this document, `DECISIONS.md`, and a test that loads an
  artifact of the old version and checks it is refused (or still runs, if supported).
* A small reference loader in C is part of the test suite and runs in CI from phase 5 (D36), to keep the contract honest outside
  Python.
